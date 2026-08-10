from __future__ import annotations

import tomllib
import unittest

from llm_hud.toml_edit import remove_key, set_array


class SetArrayTests(unittest.TestCase):
    def test_inserts_into_table_followed_by_array_of_tables(self):
        text = '[tui]\nnotifications = true\n\n[[experiments]]\nname = "a"\n'
        result = set_array(text, "tui", "status_line", ["current-dir"])
        parsed = tomllib.loads(result)
        self.assertEqual(parsed["tui"]["status_line"], ["current-dir"])
        self.assertNotIn("status_line", parsed["experiments"][0])

    def test_replaces_multiline_array(self):
        text = '[tui]\nstatus_line = [\n  "a",\n  "b",\n]\nnotifications = true\n'
        result = set_array(text, "tui", "status_line", ["c"])
        parsed = tomllib.loads(result)
        self.assertEqual(parsed["tui"]["status_line"], ["c"])
        self.assertTrue(parsed["tui"]["notifications"])

    def test_creates_missing_table(self):
        result = set_array('model = "x"\n', "tui", "status_line", ["a"])
        parsed = tomllib.loads(result)
        self.assertEqual(parsed["tui"]["status_line"], ["a"])
        self.assertEqual(parsed["model"], "x")

    def test_inserts_before_trailing_blank_lines(self):
        text = "[tui]\nnotifications = true\n\n[other]\nkey = 1\n"
        result = set_array(text, "tui", "status_line", ["a"])
        self.assertIn('notifications = true\nstatus_line = ["a"]\n', result)

    def test_rejects_name_that_only_exists_as_array_of_tables(self):
        with self.assertRaises((tomllib.TOMLDecodeError, ValueError)):
            set_array('[[tui]]\nname = "x"\n', "tui", "status_line", ["a"])


class RemoveKeyTests(unittest.TestCase):
    def test_removes_only_target_key(self):
        text = '[tui]\nstatus_line = ["a"]\nnotifications = true\n'
        result = remove_key(text, "tui", "status_line")
        parsed = tomllib.loads(result)
        self.assertNotIn("status_line", parsed["tui"])
        self.assertTrue(parsed["tui"]["notifications"])

    def test_missing_key_returns_text_unchanged(self):
        text = "[tui]\n"
        self.assertEqual(remove_key(text, "tui", "status_line"), text)


if __name__ == "__main__":
    unittest.main()
