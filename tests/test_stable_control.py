from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from llm_hud.runtime import (
    ACTIVATION_NAME,
    INSTALL_MARKER_NAME,
    INSTALL_MARKER_VALUE,
    MAX_MANAGED_TEXT_SIZE,
    RUNTIME_MARKER_NAME,
    Activation,
    RuntimeLayoutError,
    activate,
    finalize_runtime,
    initialize_layout,
    read_activation,
    runtime_path,
    source_digest,
    validate_runtime,
)


ROOT = Path(__file__).resolve().parents[1]
DISPATCHER_SOURCE = ROOT / "scripts" / "llm-hud-dispatcher"
CONTROL_SOURCE = ROOT / "scripts" / "runtime_control.py"
PREVIOUS_RUNTIME_FIXTURE = ROOT / "tests" / "fixtures" / "runtime_v0_2_0"
PREVIOUS_RUNTIME_RELEASE = "0.2.0-98de8d3e9431"


def install_stable_control(root: Path) -> Path:
    (root / INSTALL_MARKER_NAME).write_text(f"{INSTALL_MARKER_VALUE}\n")
    initialize_layout(root)
    (root / "bin").mkdir()
    (root / "control").mkdir()
    dispatcher = root / "bin" / "llm-hud"
    control = root / "control" / "runtime_control.py"
    shutil.copyfile(DISPATCHER_SOURCE, dispatcher)
    shutil.copyfile(CONTROL_SOURCE, control)
    dispatcher.chmod(0o755)
    control.chmod(0o755)
    return dispatcher


def stage_runtime(root: Path, label: str, version: str) -> Path:
    path = root / f".llm-hud-stage-{label}"
    package = path / "src" / "llm_hud"
    package.mkdir(parents=True)
    (path / "bin").mkdir()
    launcher = path / "bin" / "llm-hud"
    launcher.write_text("#!/usr/bin/env python3\nraise SystemExit('not dispatched')\n")
    launcher.chmod(0o755)
    (package / "__init__.py").write_text("")
    (package / "_version.py").write_text(f'__version__ = "{version}"\n')
    (package / "cli.py").write_text(
        "from __future__ import annotations\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "def main():\n"
        f"    print(json.dumps({{'label': {label!r}, 'arguments': sys.argv[1:], "
        "'pid': os.getpid()}, sort_keys=True))\n"
        "    return 0\n"
    )
    (path / "README.md").write_text(f"readme {label}\n")
    (path / "LICENSE").write_text("license\n")
    (path / "pyproject.toml").write_text(f'version = "{version}"\n')
    return path


def create_runtime(root: Path, label: str, version: str) -> str:
    return finalize_runtime(root, stage_runtime(root, label, version), version).release_id


def run_dispatcher(
    dispatcher: Path, *arguments: str
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(dispatcher), *arguments],
        cwd="/",
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=15,
        check=False,
    )


def load_installed_control(root: Path):
    path = root / "control" / "runtime_control.py"
    name = f"_test_stable_control_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class StableDispatcherTests(unittest.TestCase):
    def test_v0_2_0_fixture_keeps_its_released_digest(self):
        marker = json.loads(
            (PREVIOUS_RUNTIME_FIXTURE / RUNTIME_MARKER_NAME).read_text()
        )

        self.assertEqual(marker["release_id"], PREVIOUS_RUNTIME_RELEASE)
        self.assertEqual(
            source_digest(PREVIOUS_RUNTIME_FIXTURE), marker["content_sha256"]
        )

    def test_current_control_dispatches_and_rolls_back_to_v0_2_0_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            dispatcher = install_stable_control(root)
            shutil.copytree(
                PREVIOUS_RUNTIME_FIXTURE,
                root / "versions" / PREVIOUS_RUNTIME_RELEASE,
            )
            current = create_runtime(root, "current", "0.3.0")
            activate(root, PREVIOUS_RUNTIME_RELEASE)
            activate(root, current)

            rolled_back = run_dispatcher(dispatcher, "rollback")

            self.assertEqual(rolled_back.returncode, 0, rolled_back.stderr)
            self.assertEqual(read_activation(root).active, PREVIOUS_RUNTIME_RELEASE)
            dispatched = run_dispatcher(dispatcher, "--version")
            self.assertEqual(dispatched.returncode, 0, dispatched.stderr)
            self.assertEqual(dispatched.stdout.strip(), "llm-hud 0.2.0")

    def test_dispatches_arguments_from_a_path_with_spaces_and_quotes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "install space's"
            root.mkdir()
            dispatcher = install_stable_control(root)
            release_id = create_runtime(root, "first", "1.0.0")
            activate(root, release_id)

            result = run_dispatcher(dispatcher, "render", "a b", "quote's")

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["label"], "first")
            self.assertEqual(payload["arguments"], ["render", "a b", "quote's"])

    def test_rolls_back_without_importing_a_broken_active_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            dispatcher = install_stable_control(root)
            first = create_runtime(root, "first", "1.0.0")
            second = create_runtime(root, "second", "2.0.0")
            activate(root, first)
            activate(root, second)
            (runtime_path(root, second) / "src" / "llm_hud" / "cli.py").write_text(
                "this is not Python"
            )

            result = run_dispatcher(dispatcher, "rollback")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"from {second} to {first}", result.stdout)
            self.assertEqual(read_activation(root), Activation(first, second))
            dispatched = run_dispatcher(dispatcher, "doctor")
            self.assertEqual(dispatched.returncode, 0, dispatched.stderr)
            self.assertEqual(json.loads(dispatched.stdout)["label"], "first")

    def test_rejects_a_damaged_previous_runtime_without_changing_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            dispatcher = install_stable_control(root)
            first = create_runtime(root, "first", "1.0.0")
            second = create_runtime(root, "second", "2.0.0")
            activate(root, first)
            activate(root, second)
            (runtime_path(root, first) / "README.md").write_text("tampered")
            before = (root / ACTIVATION_NAME).read_bytes()

            result = run_dispatcher(dispatcher, "rollback")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("digest", result.stderr)
            self.assertEqual((root / ACTIVATION_NAME).read_bytes(), before)

    def test_rejects_a_symlinked_activation_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            dispatcher = install_stable_control(root)
            release_id = create_runtime(root, "first", "1.0.0")
            activate(root, release_id)
            activation = root / ACTIVATION_NAME
            external = root / "external-activation"
            external.write_bytes(activation.read_bytes())
            activation.unlink()
            activation.symlink_to(external.name)

            result = run_dispatcher(dispatcher, "doctor")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("activation record", result.stderr)
            self.assertEqual(external.read_text(), f"llm-hud-activation-v1 {release_id} -\n")

    def test_rejects_a_noncanonical_activation_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            dispatcher = install_stable_control(root)
            release_id = create_runtime(root, "first", "1.0.0")
            activate(root, release_id)
            activation = root / ACTIVATION_NAME
            activation.write_text(f"llm-hud-activation-v1  {release_id} -\n")

            malformed = run_dispatcher(dispatcher, "doctor")

            self.assertNotEqual(malformed.returncode, 0)
            self.assertIn("invalid activation", malformed.stderr)

    def test_rejects_a_symlinked_stable_control(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            dispatcher = install_stable_control(root)
            control = root / "control" / "runtime_control.py"
            external = root / "external-control.py"
            external.write_bytes(control.read_bytes())
            control.unlink()
            control.symlink_to(external)

            result = run_dispatcher(dispatcher, "rollback")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("stable runtime control", result.stderr)

    def test_rejects_a_symlinked_ownership_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            dispatcher = install_stable_control(root)
            marker = root / INSTALL_MARKER_NAME
            external = root / "external-marker"
            external.write_bytes(marker.read_bytes())
            marker.unlink()
            marker.symlink_to(external.name)

            result = run_dispatcher(dispatcher, "doctor")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("install marker", result.stderr)


class RuntimeValidatorParityTests(unittest.TestCase):
    def test_package_and_stable_control_apply_the_same_marker_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            install_stable_control(root)
            release_id = create_runtime(root, "current", "1.0.0")
            control = load_installed_control(root)
            marker = runtime_path(root, release_id) / RUNTIME_MARKER_NAME
            canonical = marker.read_bytes()
            maximum_size = canonical + b" " * (
                MAX_MANAGED_TEXT_SIZE - len(canonical)
            )

            def altered(**updates: object) -> bytes:
                payload = json.loads(canonical)
                payload.update(updates)
                return (json.dumps(payload, sort_keys=True) + "\n").encode()

            fixtures = (
                ("canonical", canonical, True),
                ("maximum-size", maximum_size, True),
                ("too-large", maximum_size + b" ", False),
                ("malformed-json", b"{\n", False),
                ("boolean-schema", altered(schema=True), False),
                ("unexpected-field", altered(unexpected=True), False),
                ("invalid-release", altered(release_id="../escape"), False),
                ("invalid-version", altered(version="latest"), False),
                ("invalid-digest", altered(content_sha256="A" * 64), False),
            )

            self.assertEqual(control.MAX_MANAGED_TEXT_SIZE, MAX_MANAGED_TEXT_SIZE)
            for label, content, expected in fixtures:
                with self.subTest(label=label):
                    marker.write_bytes(content)
                    try:
                        validate_runtime(root, release_id)
                    except RuntimeLayoutError:
                        package_accepts = False
                    else:
                        package_accepts = True
                    try:
                        control._validate_runtime(root, release_id)
                    except control.ControlError:
                        stable_control_accepts = False
                    else:
                        stable_control_accepts = True

                    self.assertEqual(package_accepts, expected)
                    self.assertEqual(stable_control_accepts, expected)


class StableRollbackTests(unittest.TestCase):
    def test_rollback_respects_the_shared_runtime_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            install_stable_control(root)
            first = create_runtime(root, "first", "1.0.0")
            second = create_runtime(root, "second", "2.0.0")
            activate(root, first)
            activate(root, second)
            control = load_installed_control(root)

            with control.RuntimeLock(root):
                with self.assertRaisesRegex(control.ControlError, "in progress"):
                    control.rollback(root, lock_timeout=0)

            self.assertEqual(read_activation(root), Activation(second, first))

    def test_activation_compare_and_swap_detects_in_place_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            install_stable_control(root)
            first = create_runtime(root, "first", "1.0.0")
            second = create_runtime(root, "second", "2.0.0")
            activate(root, first)
            activate(root, second)
            control = load_installed_control(root)
            current, snapshot = control._read_activation(root)
            changed = control.Activation(current.previous, current.active)
            (root / ACTIVATION_NAME).write_text(control.format_activation(changed))

            with self.assertRaisesRegex(control.ControlError, "changed during rollback"):
                control._atomic_write_activation(root, current, snapshot)

            self.assertEqual(
                (root / ACTIVATION_NAME).read_text(),
                control.format_activation(changed),
            )

    def test_symlinked_lock_is_rejected_without_touching_its_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir()
            install_stable_control(root)
            first = create_runtime(root, "first", "1.0.0")
            second = create_runtime(root, "second", "2.0.0")
            activate(root, first)
            activate(root, second)
            lock = root / ".llm-hud-update.lock"
            lock.unlink()
            external = root / "external-lock"
            external.write_text("keep")
            lock.symlink_to(external.name)
            control = load_installed_control(root)

            with self.assertRaisesRegex(control.ControlError, "lock"):
                control.rollback(root)

            self.assertEqual(external.read_text(), "keep")
            self.assertEqual(read_activation(root), Activation(second, first))


if __name__ == "__main__":
    unittest.main()
