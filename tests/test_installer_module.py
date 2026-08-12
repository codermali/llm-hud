from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import llm_hud.installer as installer_module
from llm_hud._version import __version__
from llm_hud.installer import (
    STAGING_PREFIX,
    STABLE_V1_CONTROL_SHA256,
    STABLE_V1_DISPATCHER_SHA256,
    claim_install_root,
    install_checkout_launcher,
    install_complete,
    install_runtime_from_source,
    install_stable_tools,
    install_versioned_runtime,
)
from llm_hud.runtime import (
    INSTALL_MARKER_NAME,
    INSTALL_MARKER_VALUE,
    RuntimeLayoutError,
    activate,
    initialize_layout,
    read_activation,
    runtime_path,
    validate_runtime,
)


ROOT = Path(__file__).resolve().parents[1]


def owned_root(path: Path) -> None:
    (path / INSTALL_MARKER_NAME).write_text(f"{INSTALL_MARKER_VALUE}\n")


def copy_runtime_checkout(destination: Path) -> None:
    for name in ("src", "bin"):
        shutil.copytree(ROOT / name, destination / name)
    for name in ("README.md", "LICENSE", "pyproject.toml"):
        shutil.copy2(ROOT / name, destination / name)


class RuntimeInstallerTests(unittest.TestCase):
    def test_stable_protocol_v1_bytes_are_frozen(self):
        tools = installer_module.load_stable_tools(ROOT)

        self.assertEqual(tools.dispatcher_sha256, STABLE_V1_DISPATCHER_SHA256)
        self.assertEqual(tools.control_sha256, STABLE_V1_CONTROL_SHA256)
        current = installer_module.STABLE_PROTOCOLS[
            installer_module.CURRENT_STABLE_PROTOCOL
        ]
        self.assertEqual(tools.dispatcher_sha256, current.dispatcher_sha256)
        self.assertEqual(tools.control_sha256, current.control_sha256)

    def test_registered_older_stable_protocol_is_migrated(self):
        old_dispatcher = "#!/bin/sh\nexec echo old protocol\n"
        old_control = "raise SystemExit('old control')\n"
        old = installer_module.StableProtocol(
            dispatcher_sha256=hashlib.sha256(
                old_dispatcher.encode("utf-8")
            ).hexdigest(),
            control_sha256=hashlib.sha256(old_control.encode("utf-8")).hexdigest(),
        )
        tools = installer_module.load_stable_tools(ROOT)
        for label, dispatcher_content in (
            ("clean", old_dispatcher),
            # A crash mid-migration may have replaced only one file already.
            ("mid-migration", tools.dispatcher),
        ):
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory) / "runtime"
                    root.mkdir()
                    owned_root(root)
                    initialize_layout(root)
                    (root / "bin").mkdir()
                    (root / "control").mkdir()
                    dispatcher_path = root / "bin" / "llm-hud"
                    dispatcher_path.write_text(dispatcher_content)
                    dispatcher_path.chmod(0o700)
                    (root / "control" / "runtime_control.py").write_text(old_control)
                    (root / ".llm-hud-stable.json").write_text(
                        json.dumps(
                            {
                                "schema": 1,
                                "dispatcher_sha256": old.dispatcher_sha256,
                                "control_sha256": old.control_sha256,
                            }
                        )
                    )

                    with mock.patch.dict(
                        installer_module.STABLE_PROTOCOLS, {0: old}
                    ):
                        metadata, _ = install_versioned_runtime(ROOT, root)

                    self.assertEqual(
                        dispatcher_path.read_text(), tools.dispatcher
                    )
                    self.assertEqual(
                        (root / "control" / "runtime_control.py").read_text(),
                        tools.control,
                    )
                    state = json.loads((root / ".llm-hud-stable.json").read_text())
                    self.assertEqual(
                        state["dispatcher_sha256"], tools.dispatcher_sha256
                    )
                    self.assertEqual(state["control_sha256"], tools.control_sha256)
                    validate_runtime(root, metadata.release_id)

    def test_prune_removes_only_unreferenced_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            owned_root(root)
            initialize_layout(root)
            versions = root / "versions"
            names = (
                "0.1.0-aaaaaaaaaaaa",
                "0.1.0-bbbbbbbbbbbb",
                "0.1.0-cccccccccccc",
            )
            for name in names:
                (versions / name).mkdir()
            (root / "activation").write_text(
                "llm-hud-activation-v1 0.1.0-bbbbbbbbbbbb 0.1.0-cccccccccccc\n"
            )

            installer_module._prune_inactive_runtimes(root)

            remaining = {path.name for path in versions.iterdir()}
            self.assertEqual(
                remaining, {"0.1.0-bbbbbbbbbbbb", "0.1.0-cccccccccccc"}
            )

    def test_stale_staging_directories_are_swept_on_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            owned_root(root)
            initialize_layout(root)
            stale = root / f"{STAGING_PREFIX}stale"
            stale.mkdir()
            (stale / "leftover").write_text("partial copy\n")
            old = 4000.0  # well past STAGING_MAX_AGE_SECONDS
            os.utime(stale, (old, old))
            fresh = root / f"{STAGING_PREFIX}fresh"
            fresh.mkdir()

            install_versioned_runtime(ROOT, root)

            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())

    def test_concurrent_claims_have_one_winner_and_losers_accept(self):
        with tempfile.TemporaryDirectory() as directory:
            for label, marker_content, succeeds in (
                ("valid-winner", f"{INSTALL_MARKER_VALUE}\n", True),
                ("foreign-marker", "someone else\n", False),
            ):
                with self.subTest(label=label):
                    root = Path(directory) / label
                    root.mkdir()
                    marker = root / INSTALL_MARKER_NAME

                    def racing_open(
                        path,
                        flags,
                        *args,
                        original=os.open,
                        content=marker_content,
                        target=marker,
                        **kwargs,
                    ):
                        # The concurrent winner lands its marker between the
                        # empty check and our O_EXCL create.
                        if (
                            Path(path).name == INSTALL_MARKER_NAME
                            and flags & os.O_EXCL
                            and not target.exists()
                        ):
                            target.write_text(content)
                        return original(path, flags, *args, **kwargs)

                    with mock.patch("os.open", side_effect=racing_open):
                        if succeeds:
                            claim_install_root(root)
                            self.assertEqual(
                                marker.read_text(), f"{INSTALL_MARKER_VALUE}\n"
                            )
                        else:
                            with self.assertRaisesRegex(
                                RuntimeLayoutError, "unrecognized install marker"
                            ):
                                claim_install_root(root)

    def test_unregistered_stable_protocol_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            owned_root(root)
            initialize_layout(root)
            (root / "bin").mkdir()
            (root / "control").mkdir()
            foreign = "#!/bin/sh\nexec echo foreign\n"
            dispatcher_path = root / "bin" / "llm-hud"
            dispatcher_path.write_text(foreign)
            dispatcher_path.chmod(0o700)
            (root / "control" / "runtime_control.py").write_text(foreign)
            digest = hashlib.sha256(foreign.encode("utf-8")).hexdigest()
            (root / ".llm-hud-stable.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "dispatcher_sha256": digest,
                        "control_sha256": digest,
                    }
                )
            )

            with self.assertRaisesRegex(
                RuntimeLayoutError, "unsupported protocol"
            ):
                install_versioned_runtime(ROOT, root)

            self.assertEqual(dispatcher_path.read_text(), foreign)

    def test_claim_refuses_a_nonempty_unmanaged_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "populated"
            root.mkdir()
            (root / "sentinel").write_text("keep")

            with self.assertRaisesRegex(RuntimeLayoutError, "non-empty"):
                claim_install_root(root)

            self.assertFalse((root / INSTALL_MARKER_NAME).exists())
            self.assertEqual((root / "sentinel").read_text(), "keep")

    def test_claim_rejects_home_inside_the_python_trust_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                with self.assertRaisesRegex(RuntimeLayoutError, "unsafe install root"):
                    claim_install_root(home)
            self.assertFalse((home / INSTALL_MARKER_NAME).exists())

    def test_claim_is_idempotent_on_an_owned_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()

            claim_install_root(root)
            (root / "versions").mkdir()  # later content must not block reclaim
            claim_install_root(root)

            self.assertEqual(
                (root / INSTALL_MARKER_NAME).read_text(),
                f"{INSTALL_MARKER_VALUE}\n",
            )

    def test_complete_install_writes_and_runs_the_external_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "runtime space's"
            launcher = base / "bin space's" / "llm-hud"
            root.mkdir()
            owned_root(root)

            metadata, _ = install_complete(
                ROOT, root, launcher, Path(sys.executable)
            )

            completed = subprocess.run(
                [str(launcher), "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), f"llm-hud {metadata.version}")
            pointer = json.loads(
                (root / ".llm-hud-launcher-state.json").read_text()
            )
            self.assertEqual(pointer["launcher_path"], str(launcher.resolve()))

    def test_foreign_external_launcher_types_are_never_replaced(self):
        for kind in ("file", "directory", "symlink", "hardlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                root = base / "runtime"
                launcher = base / "bin" / "llm-hud"
                root.mkdir()
                launcher.parent.mkdir()
                owned_root(root)
                external = base / "external"
                external.write_text("keep")
                if kind == "file":
                    launcher.write_text("user command")
                elif kind == "directory":
                    launcher.mkdir()
                elif kind == "symlink":
                    launcher.symlink_to(external)
                else:
                    os.link(external, launcher)

                with self.assertRaises(RuntimeLayoutError):
                    install_complete(ROOT, root, launcher, Path(sys.executable))

                self.assertFalse((root / "activation").exists())
                if kind == "file":
                    self.assertEqual(launcher.read_text(), "user command")
                elif kind == "directory":
                    self.assertTrue(launcher.is_dir())
                else:
                    self.assertEqual(external.read_text(), "keep")

    def test_legacy_two_line_launcher_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "runtime"
            launcher = base / "bin" / "llm-hud"
            root.mkdir()
            launcher.parent.mkdir()
            owned_root(root)
            legacy = (
                "#!/bin/sh\n"
                f"exec '{sys.executable}' "
                f"'{root.resolve() / 'bin' / 'llm-hud'}' \"$@\"\n"
            )
            launcher.write_text(legacy)
            launcher.chmod(0o755)

            with self.assertRaisesRegex(RuntimeLayoutError, "unmanaged launcher"):
                install_complete(ROOT, root, launcher, Path(sys.executable))

            self.assertEqual(launcher.read_text(), legacy)

    def test_modified_external_launcher_blocks_reinstall_before_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "runtime"
            launcher = base / "bin" / "llm-hud"
            root.mkdir()
            owned_root(root)
            install_complete(ROOT, root, launcher, Path(sys.executable))
            launcher.write_text("user changed this")
            activation_before = (root / "activation").read_bytes()

            with self.assertRaisesRegex(RuntimeLayoutError, "unmanaged launcher"):
                install_complete(ROOT, root, launcher, Path(sys.executable))

            self.assertEqual(launcher.read_text(), "user changed this")
            self.assertEqual((root / "activation").read_bytes(), activation_before)

    def test_deleted_managed_external_launcher_is_repaired(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "runtime"
            launcher = base / "bin" / "llm-hud"
            root.mkdir()
            owned_root(root)
            install_complete(ROOT, root, launcher, Path(sys.executable))
            launcher.unlink()

            install_complete(ROOT, root, launcher, Path(sys.executable))

            self.assertTrue(launcher.is_file())
            self.assertEqual(
                subprocess.run(
                    [str(launcher), "--version"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=15,
                    check=False,
                ).returncode,
                0,
            )

    def test_stale_launcher_state_is_ignored_and_rewritten(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "runtime"
            launcher = base / "bin" / "llm-hud"
            root.mkdir()
            owned_root(root)
            install_complete(ROOT, root, launcher, Path(sys.executable))
            state_path = root / ".llm-hud-launcher-state.json"
            # 0.1.x wrote extra bookkeeping fields; they no longer matter.
            state_path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "launcher_path": str(base / "other"),
                        "current_sha256": "0" * 64,
                        "pending_sha256": "1" * 64,
                    }
                )
            )

            install_complete(ROOT, root, launcher, Path(sys.executable))

            pointer = json.loads(state_path.read_text())
            self.assertEqual(pointer["launcher_path"], str(launcher.resolve()))
            self.assertNotIn("current_sha256", pointer)

    def test_checkout_launcher_does_not_mark_or_modify_the_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = Path(directory) / "bin" / "llm-hud"
            before = (ROOT / "bin" / "llm-hud").read_bytes()

            install_checkout_launcher(
                launcher, Path(sys.executable), ROOT / "bin" / "llm-hud"
            )

            self.assertEqual((ROOT / "bin" / "llm-hud").read_bytes(), before)
            self.assertFalse((ROOT / ".llm-hud-install-root").exists())
            self.assertEqual(
                subprocess.run(
                    [str(launcher), "--version"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=15,
                    check=False,
                ).returncode,
                0,
            )

    def test_checkout_launcher_must_be_outside_the_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory) / "checkout"
            checkout.mkdir()
            command = checkout / "bin" / "llm-hud"
            launcher = checkout / "local-bin" / "llm-hud"

            with self.assertRaisesRegex(RuntimeLayoutError, "outside the source"):
                install_checkout_launcher(
                    launcher,
                    Path(sys.executable),
                    command,
                )

            self.assertFalse(launcher.exists())

    def test_newline_launcher_path_is_rejected_before_runtime_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "runtime"
            launcher = base / "bad\npath" / "llm-hud"
            root.mkdir()
            owned_root(root)

            with self.assertRaisesRegex(RuntimeLayoutError, "single-line"):
                install_complete(ROOT, root, launcher, Path(sys.executable))

            self.assertFalse((root / "activation").exists())
            self.assertFalse((root / ".llm-hud-layout").exists())

    def test_external_launcher_must_be_outside_the_install_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            owned_root(root)

            with self.assertRaisesRegex(RuntimeLayoutError, "outside the install"):
                install_complete(
                    ROOT,
                    root,
                    root / "versions" / "candidate" / "src" / "llm-hud",
                    Path(sys.executable),
                )

            self.assertFalse((root / "activation").exists())

    def test_versioned_install_adds_a_working_stable_dispatcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            owned_root(root)

            metadata, activation = install_versioned_runtime(ROOT, root)

            self.assertEqual(activation.active, metadata.release_id)
            dispatcher = root / "bin" / "llm-hud"
            completed = subprocess.run(
                [sys.executable, str(dispatcher), "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), f"llm-hud {__version__}")

    def test_reinstall_does_not_rewrite_frozen_stable_v1_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            owned_root(root)
            install_versioned_runtime(ROOT, root)
            paths = (
                root / "bin" / "llm-hud",
                root / "control" / "runtime_control.py",
            )
            identities = {
                path: (path.stat().st_dev, path.stat().st_ino) for path in paths
            }

            install_versioned_runtime(ROOT, root)

            self.assertEqual(
                {
                    path: (path.stat().st_dev, path.stat().st_ino)
                    for path in paths
                },
                identities,
            )

    def test_managed_launcher_ignores_pythonpath_before_stable_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "runtime"
            launcher = base / "bin" / "llm-hud"
            poison = base / "poison"
            sentinel = base / "pythonpath-ran"
            root.mkdir()
            poison.mkdir()
            owned_root(root)
            install_complete(ROOT, root, launcher, Path(sys.executable))
            (poison / "pathlib.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(sentinel)!r}).write_text('ran')\n"
                "raise RuntimeError('PYTHONPATH shadow')\n"
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(poison)

            completed = subprocess.run(
                [str(launcher), "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
                env=environment,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), f"llm-hud {__version__}")
            self.assertFalse(sentinel.exists())
            self.assertTrue((root / ".llm-hud-stable.json").is_file())

    def test_partial_stable_v1_install_is_repaired_from_frozen_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            owned_root(root)
            initialize_layout(root)
            tools = installer_module.load_stable_tools(ROOT)
            (root / "control").mkdir()
            (root / "control" / "runtime_control.py").write_text(tools.control)

            metadata, _ = install_versioned_runtime(ROOT, root)

            current = read_activation(root)
            assert current is not None
            self.assertEqual(current.active, metadata.release_id)
            self.assertEqual(
                hashlib.sha256((root / "bin" / "llm-hud").read_bytes()).hexdigest(),
                STABLE_V1_DISPATCHER_SHA256,
            )
            self.assertTrue((root / ".llm-hud-stable.json").is_file())

    def test_interrupted_stable_temp_is_recovered_only_when_recognized(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            tools = installer_module.load_stable_tools(ROOT)
            for label, content, succeeds in (
                ("recognized", tools.control[:200], True),
                ("foreign", "user data", False),
            ):
                with self.subTest(label=label):
                    root = base / label
                    root.mkdir()
                    owned_root(root)
                    initialize_layout(root)
                    (root / "control").mkdir()
                    orphan = root / "control" / ".runtime_control.py.interrupted"
                    orphan.write_text(content)

                    if succeeds:
                        install_versioned_runtime(ROOT, root)
                        self.assertFalse(orphan.exists())
                        self.assertTrue((root / ".llm-hud-stable.json").exists())
                    else:
                        with self.assertRaisesRegex(RuntimeLayoutError, "unrecognized"):
                            install_versioned_runtime(ROOT, root)
                        self.assertEqual(orphan.read_text(), "user data")
                        self.assertFalse((root / "activation").exists())

    def test_foreign_root_dispatcher_is_rejected_before_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            (root / "bin").mkdir(parents=True)
            owned_root(root)
            foreign = root / "bin" / "llm-hud"
            foreign.write_text("user command")

            with self.assertRaisesRegex(RuntimeLayoutError, "unmanaged"):
                install_versioned_runtime(ROOT, root)

            self.assertEqual(foreign.read_text(), "user command")
            self.assertFalse((root / "activation").exists())
            self.assertFalse((root / ".llm-hud-layout").exists())

    def test_stable_tools_cas_does_not_overwrite_a_newer_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "runtime"
            changed_source = base / "changed-source"
            root.mkdir()
            changed_source.mkdir()
            owned_root(root)
            first, _ = install_versioned_runtime(ROOT, root)
            dispatcher_before = (root / "bin" / "llm-hud").read_bytes()
            copy_runtime_checkout(changed_source)
            with (changed_source / "README.md").open("a") as handle:
                handle.write("\nnew active\n")
            second, _ = install_runtime_from_source(changed_source, root)

            with self.assertRaisesRegex(RuntimeLayoutError, "active runtime changed"):
                install_stable_tools(
                    ROOT,
                    root,
                    expected_active=first.release_id,
                )

            self.assertEqual((root / "bin" / "llm-hud").read_bytes(), dispatcher_before)
            current = read_activation(root)
            assert current is not None
            self.assertEqual(current.active, second.release_id)

    def test_modified_stable_control_is_not_overwritten_on_reinstall(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            owned_root(root)
            install_versioned_runtime(ROOT, root)
            control = root / "control" / "runtime_control.py"
            control.write_text("user modification")
            activation_before = (root / "activation").read_bytes()

            with self.assertRaisesRegex(RuntimeLayoutError, "was modified"):
                install_versioned_runtime(ROOT, root)

            self.assertEqual(control.read_text(), "user modification")
            self.assertEqual((root / "activation").read_bytes(), activation_before)

    def test_changed_stable_protocol_is_rejected_before_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "runtime"
            source = base / "changed-source"
            launcher = base / "bin" / "llm-hud"
            root.mkdir()
            source.mkdir()
            owned_root(root)
            install_complete(ROOT, root, launcher, Path(sys.executable))
            copy_runtime_checkout(source)
            shutil.copytree(ROOT / "scripts", source / "scripts")
            with (source / "scripts" / "runtime_control.py").open("a") as handle:
                handle.write("\n# an unversioned protocol change\n")
            activation_before = (root / "activation").read_bytes()

            with self.assertRaisesRegex(RuntimeLayoutError, "protocol v1"):
                install_complete(source, root, launcher, Path(sys.executable))

            self.assertEqual((root / "activation").read_bytes(), activation_before)

    def test_symlinked_stable_directory_is_rejected_before_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "runtime"
            launcher = base / "bin" / "llm-hud"
            root.mkdir()
            owned_root(root)
            install_complete(ROOT, root, launcher, Path(sys.executable))
            control = root / "control"
            external = root / "external-control"
            control.rename(external)
            control.symlink_to(external.name, target_is_directory=True)
            activation_before = (root / "activation").read_bytes()

            with self.assertRaisesRegex(RuntimeLayoutError, "control directory"):
                install_complete(ROOT, root, launcher, Path(sys.executable))

            self.assertEqual((root / "activation").read_bytes(), activation_before)

    def test_installs_from_a_checkout_while_filtering_python_caches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            owned_root(root)

            metadata, activation = install_runtime_from_source(ROOT, root)

            self.assertEqual(activation.active, metadata.release_id)
            runtime = runtime_path(root, metadata.release_id)
            self.assertEqual(validate_runtime(root, metadata.release_id), metadata)
            self.assertFalse(any(runtime.rglob("__pycache__")))
            self.assertFalse(any(runtime.rglob("*.pyc")))
            self.assertFalse(any(root.glob(f"{STAGING_PREFIX}*")))

    def test_reinstall_is_idempotent_and_does_not_self_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            owned_root(root)
            first_metadata, first_activation = install_runtime_from_source(ROOT, root)
            activation_bytes = (root / "activation").read_bytes()

            second_metadata, second_activation = install_runtime_from_source(ROOT, root)

            self.assertEqual(second_metadata, first_metadata)
            self.assertEqual(second_activation, first_activation)
            self.assertIsNone(second_activation.previous)
            self.assertEqual((root / "activation").read_bytes(), activation_bytes)
            self.assertEqual(len(list((root / "versions").iterdir())), 1)

    def test_unsafe_source_leaves_existing_activation_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "runtime"
            source = base / "source"
            root.mkdir()
            source.mkdir()
            owned_root(root)
            install_runtime_from_source(ROOT, root)
            before = (root / "activation").read_bytes()
            copy_runtime_checkout(source)
            external = base / "external"
            external.write_text("outside")
            (source / "README.md").unlink()
            (source / "README.md").symlink_to(external)

            with self.assertRaisesRegex(RuntimeLayoutError, "symlink"):
                install_runtime_from_source(source, root)

            self.assertEqual((root / "activation").read_bytes(), before)
            self.assertEqual(external.read_text(), "outside")
            self.assertFalse(any(root.glob(f"{STAGING_PREFIX}*")))

    def test_changed_staging_is_rejected_before_candidate_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            owned_root(root)
            original_copy = installer_module.copy_runtime_source

            def alter_staging_after_copy(source: Path, staging: Path) -> None:
                original_copy(source, staging)
                (staging / "README.md").write_text("changed after source digest\n")

            with mock.patch(
                "llm_hud.installer.copy_runtime_source",
                side_effect=alter_staging_after_copy,
            ), mock.patch(
                "llm_hud.installer._smoke_test_runtime_candidate"
            ) as smoke:
                with self.assertRaisesRegex(RuntimeLayoutError, "digest"):
                    install_runtime_from_source(ROOT, root)

            smoke.assert_not_called()
            self.assertFalse((root / "activation").exists())

    def test_unrunnable_candidate_does_not_replace_the_active_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "runtime"
            source = base / "broken-source"
            launcher = base / "bin" / "llm-hud"
            root.mkdir()
            source.mkdir()
            owned_root(root)
            first, _ = install_complete(
                ROOT,
                root,
                launcher,
                Path(sys.executable),
            )
            copy_runtime_checkout(source)
            shutil.copytree(ROOT / "scripts", source / "scripts")
            (source / "README.md").write_text("a distinct broken release\n")
            (source / "src" / "llm_hud" / "cli.py").write_text(
                "raise RuntimeError('broken candidate import')\n"
            )
            activation_before = (root / "activation").read_bytes()

            with self.assertRaisesRegex(RuntimeLayoutError, "candidate smoke test"):
                install_complete(
                    source,
                    root,
                    launcher,
                    Path(sys.executable),
                )

            self.assertEqual((root / "activation").read_bytes(), activation_before)
            self.assertEqual(read_activation(root).active, first.release_id)
            running = subprocess.run(
                [str(launcher), "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(running.returncode, 0, running.stderr)
            self.assertEqual(running.stdout.strip(), f"llm-hud {__version__}")

    def test_post_activation_failure_restores_the_previous_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "runtime"
            source = base / "second-source"
            launcher = base / "bin" / "llm-hud"
            root.mkdir()
            source.mkdir()
            owned_root(root)
            first, _ = install_complete(
                ROOT,
                root,
                launcher,
                Path(sys.executable),
            )
            copy_runtime_checkout(source)
            shutil.copytree(ROOT / "scripts", source / "scripts")
            (source / "README.md").write_text("a distinct healthy release\n")
            activation_before = (root / "activation").read_bytes()

            with mock.patch(
                "llm_hud.installer._smoke_test_dispatcher",
                side_effect=RuntimeLayoutError("simulated post-activation failure"),
            ):
                with self.assertRaisesRegex(RuntimeLayoutError, "simulated"):
                    install_complete(
                        source,
                        root,
                        launcher,
                        Path(sys.executable),
                    )

            self.assertEqual((root / "activation").read_bytes(), activation_before)
            current = read_activation(root)
            assert current is not None
            self.assertEqual(current.active, first.release_id)
            running = subprocess.run(
                [str(launcher), "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(running.returncode, 0, running.stderr)

    def test_real_dispatch_is_preflighted_before_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "runtime"
            source = base / "path-sensitive-source"
            launcher = base / "bin" / "llm-hud"
            root.mkdir()
            source.mkdir()
            owned_root(root)
            first, _ = install_complete(
                ROOT,
                root,
                launcher,
                Path(sys.executable),
            )
            copy_runtime_checkout(source)
            shutil.copytree(ROOT / "scripts", source / "scripts")
            (source / "README.md").write_text("a distinct path-sensitive release\n")
            (source / "src" / "llm_hud" / "_version.py").write_text(
                "import sys\n"
                "if '.llm-hud-stage-' not in sys.argv[0]:\n"
                "    raise RuntimeError('only the staging path works')\n"
                "__version__ = '0.1.0'\n"
            )
            activation_before = (root / "activation").read_bytes()

            with self.assertRaisesRegex(RuntimeLayoutError, "dispatch preflight"):
                install_complete(
                    source,
                    root,
                    launcher,
                    Path(sys.executable),
                )

            self.assertEqual((root / "activation").read_bytes(), activation_before)
            current = read_activation(root)
            assert current is not None
            self.assertEqual(current.active, first.release_id)
            running = subprocess.run(
                [str(launcher), "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(running.returncode, 0, running.stderr)

    def test_bytecode_outside_cache_is_rejected_before_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "runtime"
            source = base / "source"
            root.mkdir()
            source.mkdir()
            owned_root(root)
            copy_runtime_checkout(source)
            (source / "src" / "unexpected.pyc").write_bytes(b"bytecode")

            with self.assertRaisesRegex(RuntimeLayoutError, "outside __pycache__"):
                install_runtime_from_source(source, root)

            self.assertFalse((root / "activation").exists())
            self.assertFalse((root / ".llm-hud-layout").exists())

    def test_cache_with_non_bytecode_content_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "runtime"
            source = base / "source"
            root.mkdir()
            source.mkdir()
            owned_root(root)
            copy_runtime_checkout(source)
            cache = source / "src" / "llm_hud" / "__pycache__"
            cache.mkdir(exist_ok=True)
            (cache / "sentinel.txt").write_text("not generated bytecode")

            with self.assertRaisesRegex(RuntimeLayoutError, "unsafe non-bytecode"):
                install_runtime_from_source(source, root)

            self.assertIsNone(read_activation(root))

    def test_source_hard_link_is_rejected_without_touching_external_file(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "runtime"
            source = base / "source"
            root.mkdir()
            source.mkdir()
            owned_root(root)
            copy_runtime_checkout(source)
            external = base / "external"
            external.write_text("outside")
            (source / "README.md").unlink()
            os.link(external, source / "README.md")

            with self.assertRaisesRegex(RuntimeLayoutError, "regular"):
                install_runtime_from_source(source, root)

            self.assertEqual(external.read_text(), "outside")
            self.assertFalse((root / "activation").exists())
            self.assertFalse((root / ".llm-hud-layout").exists())

    def test_cleanup_does_not_delete_a_replacement_staging_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            owned_root(root)

            def replace_staging(source: Path, staging: Path) -> None:
                del source
                staging.rename(staging.with_name(f"{staging.name}-original"))
                staging.mkdir()
                (staging / "sentinel").write_text("replacement")
                raise RuntimeLayoutError("simulated copy failure")

            with mock.patch(
                "llm_hud.installer.copy_runtime_source",
                side_effect=replace_staging,
            ):
                with self.assertRaisesRegex(RuntimeLayoutError, "simulated"):
                    install_runtime_from_source(ROOT, root)

            replacements = [
                path
                for path in root.glob(f"{STAGING_PREFIX}*")
                if (path / "sentinel").exists()
            ]
            self.assertEqual(len(replacements), 1)
            self.assertEqual((replacements[0] / "sentinel").read_text(), "replacement")

    def test_concurrent_activation_is_not_overwritten_after_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "runtime"
            changed_source = base / "changed-source"
            root.mkdir()
            changed_source.mkdir()
            owned_root(root)
            first_metadata, _ = install_runtime_from_source(ROOT, root)
            copy_runtime_checkout(changed_source)
            with (changed_source / "README.md").open("a") as handle:
                handle.write("\nchanged runtime\n")
            second_metadata, _ = install_runtime_from_source(changed_source, root)
            original_copy = installer_module.copy_runtime_source

            def activate_during_copy(source: Path, staging: Path) -> None:
                activate(root, first_metadata.release_id)
                original_copy(source, staging)

            with mock.patch(
                "llm_hud.installer.copy_runtime_source",
                side_effect=activate_during_copy,
            ):
                with self.assertRaisesRegex(RuntimeLayoutError, "active runtime changed"):
                    install_runtime_from_source(changed_source, root)

            current = read_activation(root)
            assert current is not None
            self.assertEqual(current.active, first_metadata.release_id)
            self.assertEqual(current.previous, second_metadata.release_id)


if __name__ == "__main__":
    unittest.main()
