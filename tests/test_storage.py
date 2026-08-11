from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from llm_hud.storage import (
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
            self.assertEqual(file_mode(path), 0o644)

    def test_json_mode_none_preserves_existing_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("{}")
            os.chmod(path, 0o644)
            atomic_write_json(path, {"a": 1}, mode=None)
            self.assertEqual(file_mode(path), 0o644)

    def test_json_defaults_to_private_mode_for_new_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            atomic_write_json(path, {"a": 1})
            self.assertEqual(file_mode(path), 0o600)


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

    def test_future_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "provider.json"
            path.write_text('{"schema": 3}')
            with self.assertRaisesRegex(StateFileError, "newer than supported"):
                read_provider_state(path, supported_schemas=frozenset((1, 2)))


if __name__ == "__main__":
    unittest.main()
