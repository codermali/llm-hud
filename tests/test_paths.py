from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from llm_hud.paths import home_dir
from tests.support import Environment


class HomePathTests(unittest.TestCase):
    def test_explicit_home_does_not_query_the_system_home(self):
        with (
            Environment(LLM_HUD_HOME="/explicit/home"),
            mock.patch("llm_hud.paths.Path.home", side_effect=RuntimeError("no home")),
        ):
            self.assertEqual(home_dir(), Path("/explicit/home"))

    def test_empty_home_override_uses_the_system_home(self):
        expected = Path("/system/home")
        with (
            Environment(LLM_HUD_HOME=""),
            mock.patch("llm_hud.paths.Path.home", return_value=expected),
        ):
            self.assertEqual(home_dir(), expected)


if __name__ == "__main__":
    unittest.main()
