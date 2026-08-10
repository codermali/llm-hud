from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path

from llm_hud.providers.codex import CodexProvider, HUD_ITEMS
from llm_hud.toml_edit import set_array
from tests.support import Environment


def status_line(path: Path):
    return tomllib.loads(path.read_text())["tui"].get("status_line")


class CodexProviderTests(unittest.TestCase):
    def test_configures_current_native_hud_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text(
                'model = "gpt-5.5"\n\n[tui]\n'
                'notifications = false\n'
                'status_line = ["current-dir", "git-branch", "five-hour-limit"]\n'
            )
            with Environment(
                LLM_HUD_CODEX_CONFIG=str(config),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                provider = CodexProvider()
                self.assertEqual(provider.install("ignored").status, "installed")
                self.assertEqual(status_line(config), [*HUD_ITEMS, "git-branch"])
                self.assertNotIn("five-hour-limit", status_line(config))
                self.assertEqual(provider.install("ignored").status, "installed")
                self.assertEqual(status_line(config).count("weekly-limit"), 1)

                self.assertEqual(provider.uninstall().status, "uninstalled")
                self.assertEqual(
                    status_line(config),
                    ["current-dir", "git-branch", "five-hour-limit"],
                )
                self.assertIn('model = "gpt-5.5"', config.read_text())

    def test_restores_unset_status_line_to_codex_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text("[tui]\nnotifications = true\n")
            with Environment(
                LLM_HUD_CODEX_CONFIG=str(config),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                provider = CodexProvider()
                provider.install("ignored")
                self.assertEqual(status_line(config), HUD_ITEMS)
                provider.uninstall()
                parsed = tomllib.loads(config.read_text())
                self.assertNotIn("status_line", parsed["tui"])
                self.assertTrue(parsed["tui"]["notifications"])

    def test_uninstall_preserves_items_added_later(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text('[tui]\nstatus_line = ["current-dir"]\n')
            with Environment(
                LLM_HUD_CODEX_CONFIG=str(config),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                provider = CodexProvider()
                provider.install("ignored")
                items = status_line(config) + ["git-branch"]
                config.write_text(set_array(config.read_text(), "tui", "status_line", items))
                provider.uninstall()
                self.assertEqual(status_line(config), ["current-dir", "git-branch"])

    def test_unwritable_table_reports_error_without_touching_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            original = '[[tui]]\nname = "x"\n'
            config.write_text(original)
            with Environment(
                LLM_HUD_CODEX_CONFIG=str(config),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                result = CodexProvider().install("ignored")
            self.assertEqual(result.status, "error")
            self.assertEqual(config.read_text(), original)

    def test_uninstall_leaves_changed_managed_fields_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text('[tui]\nstatus_line = ["current-dir"]\n')
            with Environment(
                LLM_HUD_CODEX_CONFIG=str(config),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                provider = CodexProvider()
                provider.install("ignored")
                changed = [item for item in status_line(config) if item != "weekly-limit"]
                config.write_text(set_array(config.read_text(), "tui", "status_line", changed))
                result = provider.uninstall()
                self.assertEqual(result.status, "skipped")
                self.assertEqual(status_line(config), changed)


if __name__ == "__main__":
    unittest.main()
