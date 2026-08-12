from __future__ import annotations

import math
import unittest

from llm_hud import _tomllib as tomllib
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

    def test_file_without_trailing_newline(self):
        result = set_array("[tui]\nnotifications = true", "tui", "status_line", ["a"])
        parsed = tomllib.loads(result)
        self.assertEqual(parsed["tui"]["status_line"], ["a"])
        self.assertTrue(parsed["tui"]["notifications"])

    def test_leaves_lookalike_lines_inside_multiline_strings_alone(self):
        text = (
            '[tui]\nbanner = """\nstatus_line = ["decoy"]\n"""\n'
            'status_line = ["old"]\n'
        )
        parsed = tomllib.loads(set_array(text, "tui", "status_line", ["new"]))
        self.assertEqual(parsed["tui"]["banner"], 'status_line = ["decoy"]\n')
        self.assertEqual(parsed["tui"]["status_line"], ["new"])

    def test_reinstall_with_identical_values_does_not_touch_strings(self):
        # The post-edit check must compare the whole document: when the key
        # already holds the requested values, a key-only check sees no change.
        text = '[tui]\nbanner = """\nstatus_line = ["decoy"]\n"""\nstatus_line = ["a"]\n'
        parsed = tomllib.loads(set_array(text, "tui", "status_line", ["a"]))
        self.assertEqual(parsed["tui"]["banner"], 'status_line = ["decoy"]\n')

    def test_fake_table_header_inside_multiline_string(self):
        text = '[tui]\nbanner = """\n[art]\n"""\nx = 1\n'
        parsed = tomllib.loads(set_array(text, "tui", "status_line", ["a"]))
        self.assertEqual(parsed["tui"]["banner"], "[art]\n")
        self.assertEqual(parsed["tui"]["status_line"], ["a"])
        self.assertEqual(parsed["tui"]["x"], 1)

    def test_nested_array_element_on_its_own_line(self):
        text = '[tui]\nlayout = [\n["left", "right"]\n]\n'
        parsed = tomllib.loads(set_array(text, "tui", "status_line", ["a"]))
        self.assertEqual(parsed["tui"]["layout"], [["left", "right"]])
        self.assertEqual(parsed["tui"]["status_line"], ["a"])

    def test_replaces_root_dotted_assignment_in_place(self):
        text = 'model = "gpt-5"\ntui.status_line = [\n  "old",\n]\ntui.notifications = true\n'
        result = set_array(text, "tui", "status_line", ["new"])
        self.assertIn('tui.status_line = ["new"]\n', result)
        self.assertNotIn("[tui]", result)
        parsed = tomllib.loads(result)
        self.assertEqual(parsed["tui"]["status_line"], ["new"])
        self.assertTrue(parsed["tui"]["notifications"])
        self.assertEqual(parsed["model"], "gpt-5")

    def test_extends_a_table_that_exists_only_in_dotted_form(self):
        text = 'tui.notifications = true\nmodel = "gpt-5"\n'
        result = set_array(text, "tui", "status_line", ["a"])
        self.assertIn('tui.status_line = ["a"]\n', result)
        self.assertNotIn("[tui]", result)
        parsed = tomllib.loads(result)
        self.assertEqual(parsed["tui"]["status_line"], ["a"])
        self.assertTrue(parsed["tui"]["notifications"])

    def test_dotted_lookalike_under_a_header_is_not_the_root_table(self):
        text = '[server]\ntui.status_line = ["x"]\n'
        result = set_array(text, "tui", "status_line", ["a"])
        parsed = tomllib.loads(result)
        self.assertEqual(parsed["server"]["tui"]["status_line"], ["x"])
        self.assertEqual(parsed["tui"]["status_line"], ["a"])

    def test_crlf_line_endings_are_preserved(self):
        result = set_array('[tui]\r\nstatus_line = ["a"]\r\nx = 1\r\n', "tui", "status_line", ["b"])
        self.assertNotIn("\n", result.replace("\r\n", ""))
        self.assertEqual(tomllib.loads(result)["tui"]["status_line"], ["b"])

    def test_unrelated_nested_nan_is_preserved(self):
        text = 'metrics = [{ sample = nan }]\n[tui]\nnotifications = true\n'
        result = set_array(text, "tui", "status_line", ["a"])
        parsed = tomllib.loads(result)
        self.assertTrue(math.isnan(parsed["metrics"][0]["sample"]))
        self.assertEqual(parsed["tui"]["status_line"], ["a"])
        self.assertTrue(parsed["tui"]["notifications"])


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

    def test_does_not_delete_lookalike_line_inside_multiline_string(self):
        text = '[tui]\nbanner = """\nstatus_line = ["decoy"]\n"""\nstatus_line = ["a"]\nx = 1\n'
        parsed = tomllib.loads(remove_key(text, "tui", "status_line"))
        self.assertEqual(parsed["tui"]["banner"], 'status_line = ["decoy"]\n')
        self.assertNotIn("status_line", parsed["tui"])
        self.assertEqual(parsed["tui"]["x"], 1)

    def test_removes_root_dotted_assignment(self):
        text = 'tui.status_line = ["a"]\ntui.notifications = true\n'
        parsed = tomllib.loads(remove_key(text, "tui", "status_line"))
        self.assertNotIn("status_line", parsed["tui"])
        self.assertTrue(parsed["tui"]["notifications"])

    def test_unrelated_nested_nan_is_preserved(self):
        text = (
            'metrics = { sample = nan }\n'
            '[tui]\nstatus_line = ["a"]\nnotifications = true\n'
        )
        result = remove_key(text, "tui", "status_line")
        parsed = tomllib.loads(result)
        self.assertTrue(math.isnan(parsed["metrics"]["sample"]))
        self.assertNotIn("status_line", parsed["tui"])
        self.assertTrue(parsed["tui"]["notifications"])


if __name__ == "__main__":
    unittest.main()
