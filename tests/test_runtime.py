from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import llm_hud.runtime as runtime_module
from llm_hud.runtime import (
    ACTIVATION_NAME,
    INSTALL_MARKER_NAME,
    INSTALL_MARKER_VALUE,
    LAYOUT_MARKER_NAME,
    RUNTIME_MARKER_NAME,
    Activation,
    RuntimeLayoutError,
    RuntimeLock,
    activate,
    finalize_runtime,
    format_activation,
    initialize_layout,
    install_staged_runtime,
    parse_activation,
    read_activation,
    rollback,
    runtime_path,
    source_digest,
    validate_release_id,
    validate_runtime,
)


def initialize_owned_layout(root: Path) -> None:
    (root / INSTALL_MARKER_NAME).write_text(f"{INSTALL_MARKER_VALUE}\n")
    initialize_layout(root)


def stage_runtime(root: Path, label: str, version: str) -> Path:
    path = root / f".llm-hud-stage-{label}"
    (path / "bin").mkdir(parents=True)
    (path / "src" / "llm_hud").mkdir(parents=True)
    launcher = path / "bin" / "llm-hud"
    launcher.write_text(f"#!/bin/sh\necho {label}\n")
    os.chmod(launcher, 0o755)
    (path / "src" / "llm_hud" / "cli.py").write_text(f"cli {label}")
    (path / "src" / "llm_hud" / "_version.py").write_text(
        f'__version__ = "{version}"\n'
    )
    (path / "README.md").write_text(f"readme {label}")
    (path / "LICENSE").write_text("license")
    (path / "pyproject.toml").write_text(f'version = "{version}"\n')
    return path


def create_runtime(root: Path, label: str, version: str = "1.0.0") -> str:
    metadata = finalize_runtime(root, stage_runtime(root, label, version), version)
    return metadata.release_id


class ReleaseIdTests(unittest.TestCase):
    def test_accepts_a_bounded_single_path_component(self):
        self.assertEqual(validate_release_id("0.2.0-ab12+local"), "0.2.0-ab12+local")

    def test_rejects_path_traversal_and_shell_syntax(self):
        for value in ("", ".", "..", "../x", "a/b", "/tmp/x", "a b", "$(id)"):
            with self.subTest(value=value), self.assertRaises(RuntimeLayoutError):
                validate_release_id(value)


class ActivationTests(unittest.TestCase):
    def test_round_trip_uses_one_canonical_line(self):
        value = Activation("2.0.0-b", "1.0.0-a")
        self.assertEqual(parse_activation(format_activation(value)), value)
        self.assertEqual(
            format_activation(Activation("1.0.0-a")),
            "llm-hud-activation-v1 1.0.0-a -\n",
        )

    def test_rejects_noncanonical_or_self_referential_records(self):
        for text in (
            "llm-hud-activation-v1 a a\n",
            "llm-hud-activation-v1  a -\n",
            "llm-hud-activation-v1 a -",
            "wrong a -\n",
            "llm-hud-activation-v1 ../a -\n",
        ):
            with self.subTest(text=text), self.assertRaises(RuntimeLayoutError):
                parse_activation(text)

    def test_activation_and_rollback_are_atomic_state_transitions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_owned_layout(root)
            first = create_runtime(root, "first")
            second = create_runtime(root, "second", "2.0.0")

            self.assertEqual(activate(root, first), Activation(first))
            self.assertEqual(activate(root, second), Activation(second, first))
            before = (root / ACTIVATION_NAME).read_bytes()
            self.assertEqual(activate(root, second), Activation(second, first))
            self.assertEqual((root / ACTIVATION_NAME).read_bytes(), before)
            self.assertEqual(rollback(root), Activation(first, second))
            self.assertEqual(read_activation(root), Activation(first, second))

    def test_invalid_runtime_does_not_change_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_owned_layout(root)
            first = create_runtime(root, "first")
            activate(root, first)
            before = (root / ACTIVATION_NAME).read_bytes()
            (runtime_path(root, "broken") / "bin").mkdir(parents=True)

            with self.assertRaises(RuntimeLayoutError):
                activate(root, "broken")

            self.assertEqual((root / ACTIVATION_NAME).read_bytes(), before)

    def test_broken_old_previous_does_not_block_a_new_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_owned_layout(root)
            first = create_runtime(root, "first", "1.0.0")
            second = create_runtime(root, "second", "2.0.0")
            third = create_runtime(root, "third", "3.0.0")
            activate(root, first)
            activate(root, second)
            (runtime_path(root, first) / "README.md").write_text("tampered")

            self.assertEqual(activate(root, third), Activation(third, second))

    def test_broken_active_can_be_repaired_without_preserving_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_owned_layout(root)
            first = create_runtime(root, "first", "1.0.0")
            second = create_runtime(root, "second", "2.0.0")
            activate(root, first)
            (runtime_path(root, first) / "README.md").write_text("tampered")

            self.assertEqual(activate(root, second), Activation(second))

    def test_repair_preserves_only_a_healthy_previous_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_owned_layout(root)
            first = create_runtime(root, "first", "1.0.0")
            second = create_runtime(root, "second", "2.0.0")
            third = create_runtime(root, "third", "3.0.0")
            activate(root, first)
            activate(root, second)
            (runtime_path(root, second) / "README.md").write_text("tampered")

            self.assertEqual(activate(root, third), Activation(third, first))
            activate(root, first)
            (runtime_path(root, first) / "README.md").write_text("also tampered")
            (runtime_path(root, third) / "README.md").write_text("tampered too")
            fourth = create_runtime(root, "fourth", "4.0.0")
            self.assertEqual(activate(root, fourth), Activation(fourth))

    def test_repairing_directly_to_the_previous_does_not_self_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_owned_layout(root)
            first = create_runtime(root, "first", "1.0.0")
            second = create_runtime(root, "second", "2.0.0")
            activate(root, first)
            activate(root, second)
            (runtime_path(root, second) / "README.md").write_text("tampered")

            self.assertEqual(activate(root, first), Activation(first))

    def test_broken_previous_blocks_rollback_without_changing_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_owned_layout(root)
            first = create_runtime(root, "first", "1.0.0")
            second = create_runtime(root, "second", "2.0.0")
            activate(root, first)
            activate(root, second)
            (runtime_path(root, first) / "README.md").write_text("tampered")
            before = (root / ACTIVATION_NAME).read_bytes()

            with self.assertRaises(RuntimeLayoutError):
                rollback(root)

            self.assertEqual((root / ACTIVATION_NAME).read_bytes(), before)

    def test_broken_current_runtime_can_roll_back_to_a_healthy_previous(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_owned_layout(root)
            first = create_runtime(root, "first", "1.0.0")
            second = create_runtime(root, "second", "2.0.0")
            activate(root, first)
            activate(root, second)
            (runtime_path(root, second) / "README.md").write_text("tampered")

            self.assertEqual(rollback(root), Activation(first, second))
            self.assertEqual(read_activation(root), Activation(first, second))

    def test_expected_active_prevents_lost_update(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_owned_layout(root)
            first = create_runtime(root, "first", "1.0.0")
            second = create_runtime(root, "second", "2.0.0")
            activate(root, first)
            before = (root / ACTIVATION_NAME).read_bytes()

            with self.assertRaisesRegex(RuntimeLayoutError, "active runtime changed"):
                activate(root, second, expected_active="9.9.9-missing")

            self.assertEqual((root / ACTIVATION_NAME).read_bytes(), before)

    def test_failed_activation_replace_keeps_the_old_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_owned_layout(root)
            first = create_runtime(root, "first", "1.0.0")
            second = create_runtime(root, "second", "2.0.0")
            activate(root, first)
            before = (root / ACTIVATION_NAME).read_bytes()
            with mock.patch(
                "llm_hud.runtime.atomic_write_text", side_effect=OSError("simulated")
            ):
                with self.assertRaisesRegex(RuntimeLayoutError, "simulated"):
                    activate(root, second)
            self.assertEqual((root / ACTIVATION_NAME).read_bytes(), before)

    def test_activation_symlink_is_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_owned_layout(root)
            release_id = create_runtime(root, "first")
            external = root / "external"
            external.write_text("keep")
            (root / ACTIVATION_NAME).symlink_to("external")

            with self.assertRaises(RuntimeLayoutError):
                activate(root, release_id)

            self.assertEqual(external.read_text(), "keep")

    def test_unrecognized_layout_marker_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / INSTALL_MARKER_NAME).write_text(f"{INSTALL_MARKER_VALUE}\n")
            (root / LAYOUT_MARKER_NAME).write_text("future-layout\n")
            with self.assertRaises(RuntimeLayoutError):
                initialize_layout(root)

    def test_layout_cannot_claim_an_unowned_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(RuntimeLayoutError, "not owned"):
                initialize_layout(root)
            self.assertFalse((root / LAYOUT_MARKER_NAME).exists())

    def test_layout_cannot_claim_preexisting_reserved_paths(self):
        for relative, is_directory in (
            ("versions", True),
            ("control", True),
            (ACTIVATION_NAME, False),
            (".llm-hud-update.lock", False),
            (".llm-hud-stable.json", False),
            (".llm-hud-launcher-state.json", False),
        ):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / INSTALL_MARKER_NAME).write_text(
                    f"{INSTALL_MARKER_VALUE}\n"
                )
                reserved = root / relative
                if is_directory:
                    reserved.mkdir()
                else:
                    reserved.write_text("user data")

                with self.assertRaisesRegex(RuntimeLayoutError, "refusing to claim"):
                    initialize_layout(root)

                self.assertFalse((root / LAYOUT_MARKER_NAME).exists())
                self.assertTrue(reserved.exists())

    def test_wrong_or_symlinked_ownership_marker_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / INSTALL_MARKER_NAME
            marker.write_text("wrong\n")
            with self.assertRaises(RuntimeLayoutError):
                initialize_layout(root)
            marker.unlink()
            external = root / "external"
            external.write_text(f"{INSTALL_MARKER_VALUE}\n")
            marker.symlink_to("external")
            with self.assertRaises(RuntimeLayoutError):
                initialize_layout(root)
            self.assertEqual(external.read_text(), f"{INSTALL_MARKER_VALUE}\n")

    def test_new_versions_directory_requests_a_parent_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / INSTALL_MARKER_NAME).write_text(f"{INSTALL_MARKER_VALUE}\n")
            with mock.patch("llm_hud.runtime.fsync_directory") as sync_directory:
                initialize_layout(root)
            sync_directory.assert_called_once_with(root)


class RuntimeSourceTests(unittest.TestCase):
    def test_source_digest_is_stable_and_changes_with_content_or_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("src", "bin"):
                (root / name).mkdir()
                (root / name / "file").write_text(name)
            for name in ("README.md", "LICENSE", "pyproject.toml"):
                (root / name).write_text(name)

            first = source_digest(root)
            self.assertEqual(first, source_digest(root))
            (root / "src" / "file").write_text("changed")
            second = source_digest(root)
            self.assertNotEqual(first, second)
            os.chmod(root / "src" / "file", 0o755)
            self.assertNotEqual(second, source_digest(root))

    def test_finalize_rejects_staging_outside_the_owned_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            root.mkdir()
            initialize_owned_layout(root)
            outside = base / ".llm-hud-stage-outside"
            outside.mkdir()

            with self.assertRaisesRegex(RuntimeLayoutError, "outside"):
                finalize_runtime(root, outside, "1.0.0")

    def test_install_staged_runtime_seals_and_activates_under_one_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_owned_layout(root)
            staging = stage_runtime(root, "candidate", "1.2.3")
            digest = source_digest(staging)

            metadata, activation = install_staged_runtime(
                root,
                staging,
                "1.2.3",
                expected_content_sha256=digest,
                expected_active=None,
            )

            self.assertEqual(activation, Activation(metadata.release_id))
            self.assertFalse(staging.exists())
            self.assertEqual(validate_runtime(root, metadata.release_id), metadata)

    def test_runtime_tree_and_directories_are_synced_before_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_owned_layout(root)
            staging = stage_runtime(root, "candidate", "1.2.3")
            events: list[str] = []
            original_activate = runtime_module._activate_unlocked
            original_rename = runtime_module.os.rename

            def record_activate(*args, **kwargs):
                events.append("activate")
                return original_activate(*args, **kwargs)

            with mock.patch(
                "llm_hud.runtime._fsync_runtime_tree",
                side_effect=lambda path: events.append("sync:tree"),
            ), mock.patch(
                "llm_hud.runtime.os.rename",
                side_effect=lambda source, destination: (
                    events.append("rename"),
                    original_rename(source, destination),
                )[1],
            ), mock.patch(
                "llm_hud.runtime._fsync_directory_required",
                side_effect=lambda path: events.append(f"sync:{path.name}"),
            ), mock.patch(
                "llm_hud.runtime._activate_unlocked", side_effect=record_activate
            ):
                install_staged_runtime(root, staging, "1.2.3")

            self.assertEqual(
                events,
                [
                    "sync:tree",
                    "rename",
                    "sync:versions",
                    f"sync:{root.name}",
                    "activate",
                ],
            )

    def test_expected_content_mismatch_leaves_staging_and_activation_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_owned_layout(root)
            staging = stage_runtime(root, "candidate", "1.2.3")

            with self.assertRaisesRegex(RuntimeLayoutError, "does not match"):
                install_staged_runtime(
                    root, staging, "1.2.3", expected_content_sha256="0" * 64
                )

            self.assertTrue(staging.is_dir())
            self.assertFalse((staging / RUNTIME_MARKER_NAME).exists())
            self.assertIsNone(read_activation(root))

    def test_source_digest_rejects_top_level_and_nested_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("src", "bin"):
                (root / name).mkdir()
                (root / name / "file").write_text(name)
            for name in ("README.md", "LICENSE", "pyproject.toml"):
                (root / name).write_text(name)
            external = root / "external"
            external.write_text("external")

            (root / "README.md").unlink()
            (root / "README.md").symlink_to("external")
            with self.assertRaisesRegex(RuntimeLayoutError, "symlink"):
                source_digest(root)
            (root / "README.md").unlink()
            (root / "README.md").write_text("readme")
            (root / "src" / "file").unlink()
            (root / "src" / "file").symlink_to(Path("..") / ".." / "external")
            with self.assertRaisesRegex(RuntimeLayoutError, "symlink"):
                source_digest(root)

    def test_sealing_rejects_python_cache_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_owned_layout(root)
            staging = stage_runtime(root, "candidate", "1.2.3")
            cache = staging / "src" / "llm_hud" / "__pycache__"
            cache.mkdir()
            (cache / "module.pyc").write_bytes(b"cache")
            with self.assertRaisesRegex(RuntimeLayoutError, "Python cache"):
                finalize_runtime(root, staging, "1.2.3")

    def test_generated_python_cache_does_not_invalidate_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_owned_layout(root)
            release_id = create_runtime(root, "first")
            runtime = runtime_path(root, release_id)
            cache = runtime / "src" / "llm_hud" / "__pycache__"
            cache.mkdir()
            (cache / "cli.cpython-312.pyc").write_bytes(b"generated cache")

            validate_runtime(root, release_id)

            (cache / "unexpected.txt").write_text("not generated bytecode")
            with self.assertRaisesRegex(RuntimeLayoutError, "digest"):
                validate_runtime(root, release_id)

    def test_staging_and_sealed_runtime_reject_extra_top_level_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_owned_layout(root)
            staging = stage_runtime(root, "candidate", "1.2.3")
            (staging / "unexpected").write_text("not covered by the digest")
            with self.assertRaisesRegex(RuntimeLayoutError, "top-level shape"):
                finalize_runtime(root, staging, "1.2.3")

            release_id = create_runtime(root, "first")
            runtime = runtime_path(root, release_id)
            (runtime / "unexpected").write_text("tampered")
            with self.assertRaisesRegex(RuntimeLayoutError, "top-level shape"):
                validate_runtime(root, release_id)

    def test_runtime_digest_and_marker_schema_are_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_owned_layout(root)
            release_id = create_runtime(root, "first")
            runtime = runtime_path(root, release_id)
            (runtime / "README.md").write_text("tampered")
            with self.assertRaisesRegex(RuntimeLayoutError, "digest"):
                validate_runtime(root, release_id)

            # Restore with a fresh candidate, then prove JSON true is not schema 1.
            second = create_runtime(root, "second", "2.0.0")
            marker = runtime_path(root, second) / RUNTIME_MARKER_NAME
            payload = json.loads(marker.read_text())
            payload["schema"] = True
            marker.write_text(json.dumps(payload))
            with self.assertRaisesRegex(RuntimeLayoutError, "unsupported"):
                validate_runtime(root, second)

            third = create_runtime(root, "third", "3.0.0")
            marker = runtime_path(root, third) / RUNTIME_MARKER_NAME
            payload = json.loads(marker.read_text())
            payload["unexpected"] = True
            marker.write_text(json.dumps(payload))
            with self.assertRaisesRegex(RuntimeLayoutError, "unsupported"):
                validate_runtime(root, third)

            fourth = create_runtime(root, "fourth", "4.0.0")
            marker = runtime_path(root, fourth) / RUNTIME_MARKER_NAME
            payload = json.loads(marker.read_text())
            payload["version"] = "9.9.9"
            marker.write_text(json.dumps(payload))
            with self.assertRaisesRegex(RuntimeLayoutError, "fields do not match"):
                validate_runtime(root, fourth)

    def test_staged_code_version_must_match_requested_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_owned_layout(root)
            staging = stage_runtime(root, "candidate", "2.0.0")
            with self.assertRaisesRegex(RuntimeLayoutError, "expected 1.0.0"):
                finalize_runtime(root, staging, "1.0.0")
            self.assertTrue(staging.exists())


class RuntimeLockTests(unittest.TestCase):
    def test_second_operation_and_activation_time_out_while_lock_is_held(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_owned_layout(root)
            release_id = create_runtime(root, "first")
            with RuntimeLock(root):
                with self.assertRaisesRegex(RuntimeLayoutError, "in progress"):
                    with RuntimeLock(root, timeout=0):
                        self.fail("second lock must not be acquired")
                with self.assertRaisesRegex(RuntimeLayoutError, "in progress"):
                    activate(root, release_id, lock_timeout=0)


if __name__ == "__main__":
    unittest.main()
