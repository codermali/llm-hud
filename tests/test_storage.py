from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from llm_hud.storage import (
    ContentChangedError,
    ProviderLock,
    StateFileError,
    atomic_write_json,
    atomic_write_text,
    read_provider_state,
)


def file_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class AtomicWriteTests(unittest.TestCase):
    def test_text_preserves_existing_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("{}")
            os.chmod(path, 0o644)
            atomic_write_text(path, "{}\n")
            if os.name != "nt":
                self.assertEqual(file_mode(path), 0o644)

    def test_atomic_rename_requests_a_parent_directory_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with mock.patch("llm_hud.storage.fsync_directory") as sync_directory:
                atomic_write_text(path, "{}\n")
            sync_directory.assert_called_once_with(path.parent.resolve())

    def test_json_mode_none_preserves_existing_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("{}")
            os.chmod(path, 0o644)
            atomic_write_json(path, {"a": 1}, mode=None)
            if os.name != "nt":
                self.assertEqual(file_mode(path), 0o644)

    def test_json_defaults_to_private_mode_for_new_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            atomic_write_json(path, {"a": 1})
            if os.name != "nt":
                self.assertEqual(file_mode(path), 0o600)

    def test_text_follows_relative_symlink_without_replacing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "managed" / "settings.json"
            target.parent.mkdir()
            target.write_text("old")
            os.chmod(target, 0o640)
            link = root / "settings.json"
            link.symlink_to(Path("managed") / "settings.json")
            original_link = link.readlink()

            atomic_write_text(link, "new")

            self.assertTrue(link.is_symlink())
            self.assertEqual(link.readlink(), original_link)
            self.assertEqual(target.read_text(), "new")
            if os.name != "nt":
                self.assertEqual(file_mode(target), 0o640)

    def test_text_follows_a_symlink_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_text("old")
            middle = root / "middle"
            middle.symlink_to("target")
            link = root / "link"
            link.symlink_to("middle")

            atomic_write_text(link, "new")

            self.assertTrue(link.is_symlink())
            self.assertTrue(middle.is_symlink())
            self.assertEqual(target.read_text(), "new")

    def test_dangling_symlink_is_rejected_without_replacing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            link = root / "settings.json"
            link.symlink_to("missing.json")

            with self.assertRaisesRegex(OSError, "cannot resolve symlink"):
                atomic_write_text(link, "new")

            self.assertTrue(link.is_symlink())
            self.assertFalse((root / "missing.json").exists())

    def test_changed_symlink_target_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first"
            first.write_text("first")
            second = root / "second"
            second.write_text("second")
            link = root / "link"
            link.symlink_to("second")

            with self.assertRaisesRegex(OSError, "file target changed"):
                atomic_write_text(link, "new", expected_target=first.resolve())

            self.assertEqual(first.read_text(), "first")
            self.assertEqual(second.read_text(), "second")

    def test_expected_content_rejects_a_concurrent_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text('{"theme":"light"}\n')
            snapshot = path.read_bytes()
            path.write_text('{"theme":"dark","external":true}\n')

            with self.assertRaisesRegex(ContentChangedError, "changed"):
                atomic_write_text(
                    path,
                    '{"managed":true}\n',
                    expected_content=snapshot,
                )

            self.assertEqual(
                path.read_text(), '{"theme":"dark","external":true}\n'
            )

    def test_expected_absence_rejects_a_concurrently_created_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text('{"external":true}\n')

            with self.assertRaisesRegex(ContentChangedError, "changed"):
                atomic_write_text(path, '{"managed":true}\n', expected_content=None)

            self.assertEqual(path.read_text(), '{"external":true}\n')

    def test_non_regular_target_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            directory_target = root / "target"
            directory_target.mkdir()
            link = root / "link"
            link.symlink_to("target")

            with self.assertRaisesRegex(OSError, "not a regular file"):
                atomic_write_text(link, "new")

            self.assertTrue(link.is_symlink())

    @unittest.skipIf(os.name == "nt", "Windows has no POSIX zero-mode equivalent")
    def test_zero_mode_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private"
            path.write_text("old")
            os.chmod(path, 0o000)

            atomic_write_text(path, "new")

            self.assertEqual(file_mode(path), 0o000)


class ProviderStateTests(unittest.TestCase):
    def test_missing_state_is_distinct_from_invalid_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "provider.json"
            self.assertIsNone(
                read_provider_state(path, supported_schemas=frozenset((1,)))
            )

            for content in ("not json", "[]", "{}", '{"schema": true}'):
                with self.subTest(content=content):
                    path.write_text(content)
                    with self.assertRaises(StateFileError):
                        read_provider_state(path, supported_schemas=frozenset((1,)))

    def test_non_utf8_state_is_invalid_rather_than_a_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "provider.json"
            path.write_bytes(b'{"schema": 1, "note": "\xff\xfe"}')
            with self.assertRaisesRegex(StateFileError, "invalid installation state"):
                read_provider_state(path, supported_schemas=frozenset((1,)))

    def test_future_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "provider.json"
            path.write_text('{"schema": 3}')
            with self.assertRaisesRegex(StateFileError, "newer than supported"):
                read_provider_state(path, supported_schemas=frozenset((1, 2)))

    def test_state_symlink_is_never_followed_for_reads_or_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            external = root / "external.json"
            external.write_text('{"schema": 1}\n')
            state = root / "state.json"
            state.symlink_to("external.json")

            with self.assertRaisesRegex(StateFileError, "symlink"):
                read_provider_state(state, supported_schemas=frozenset((1,)))
            with self.assertRaisesRegex(OSError, "symlink"):
                atomic_write_json(
                    state, {"schema": 1, "changed": True}, follow_symlinks=False
                )

            self.assertTrue(state.is_symlink())
            self.assertEqual(external.read_text(), '{"schema": 1}\n')


class ProviderLockTests(unittest.TestCase):
    def test_second_provider_operation_cannot_enter_the_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "providers" / "claude.json"
            with ProviderLock(state):
                with self.assertRaisesRegex(OSError, "operation is in progress"):
                    with ProviderLock(state, timeout=0):
                        self.fail("second provider lock must not be acquired")

    def test_provider_lock_refuses_a_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            providers = Path(directory) / "providers"
            providers.mkdir()
            target = providers / "external"
            target.write_text("")
            (providers / "claude.lock").symlink_to(target.name)

            with self.assertRaisesRegex(OSError, "provider lock"):
                with ProviderLock(providers / "claude.json", timeout=0):
                    self.fail("symlink lock must not be acquired")


if __name__ == "__main__":
    unittest.main()
