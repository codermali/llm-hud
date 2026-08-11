from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from llm_hud.providers.claude import ClaudeProvider, render
from tests.support import Environment


class ClaudeProviderTests(unittest.TestCase):
    def test_install_render_and_uninstall(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            state = root / "state"
            with Environment(
                LLM_HUD_HOME=str(root),
                LLM_HUD_CLAUDE_SETTINGS=str(settings),
                LLM_HUD_STATE_DIR=str(state),
            ):
                provider = ClaudeProvider()
                result = provider.install("/opt/llm-hud")
                self.assertEqual(result.status, "installed")
                configured = json.loads(settings.read_text())
                self.assertEqual(
                    configured["statusLine"]["command"],
                    "/opt/llm-hud render claude",
                )
                self.assertNotIn("refreshInterval", configured["statusLine"])
                installation_state = json.loads(
                    (state / "providers" / "claude.json").read_text()
                )
                self.assertEqual(installation_state["schema"], 2)
                self.assertEqual(
                    installation_state["installed_status_line"],
                    configured["statusLine"],
                )

                raw = json.dumps(
                    {
                        "model": {"display_name": "Opus"},
                        "workspace": {"current_dir": str(root / "project")},
                        "rate_limits": {
                            "five_hour": {"used_percentage": 24},
                            "seven_day": {"used_percentage": 41},
                        },
                    }
                ).encode()
                self.assertEqual(
                    render(raw, color=False),
                    "Claude · Opus · ~/project\n"
                    "5h  ████████░░   76%    7d  ██████░░░░   59%",
                )

                self.assertEqual(provider.install("/opt/llm-hud").status, "installed")
                self.assertEqual(provider.uninstall().status, "uninstalled")
                self.assertNotIn("statusLine", json.loads(settings.read_text()))

    def test_existing_status_line_is_delegated_and_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            settings.write_text(
                json.dumps(
                    {
                        "theme": "dark",
                        "statusLine": {
                            "type": "command",
                            "command": "printf existing",
                            "padding": 2,
                        },
                    }
                )
            )
            with Environment(
                LLM_HUD_CLAUDE_SETTINGS=str(settings),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                provider = ClaudeProvider()
                provider.install("/opt/llm-hud")
                output = render(b'{"model":{"display_name":"Sonnet"}}', color=False)
                self.assertEqual(
                    output,
                    "existing\nClaude · Sonnet\n"
                    "5h  ░░░░░░░░░░    --    7d  ░░░░░░░░░░    --   "
                    "waiting for first response",
                )
                provider.uninstall()
                restored = json.loads(settings.read_text())
                self.assertEqual(restored["theme"], "dark")
                self.assertEqual(restored["statusLine"]["command"], "printf existing")

    def test_install_preserves_settings_file_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            settings.write_text("{}")
            os.chmod(settings, 0o644)
            with Environment(
                LLM_HUD_CLAUDE_SETTINGS=str(settings),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                ClaudeProvider().install("/opt/llm-hud")
                self.assertEqual(stat.S_IMODE(settings.stat().st_mode), 0o644)

    def test_install_shell_quotes_the_renderer_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command_path = root / "bin with space" / "llm-hud's"
            command_path.parent.mkdir()
            command_path.write_text("#!/bin/sh\nprintf '%s' quoted\n")
            os.chmod(command_path, 0o755)
            settings = root / "settings.json"
            with Environment(
                LLM_HUD_CLAUDE_SETTINGS=str(settings),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                result = ClaudeProvider().install(str(command_path))

            self.assertEqual(result.status, "installed")
            configured = json.loads(settings.read_text())
            command = configured["statusLine"]["command"]
            completed = subprocess.run(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, "quoted")

    def test_uninstall_does_not_replace_later_user_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            with Environment(
                LLM_HUD_CLAUDE_SETTINGS=str(settings),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                provider = ClaudeProvider()
                provider.install("/opt/llm-hud")
                payload = json.loads(settings.read_text())
                payload["statusLine"]["command"] = "my-new-footer"
                settings.write_text(json.dumps(payload))
                result = provider.uninstall()
                self.assertEqual(result.status, "skipped")
                self.assertEqual(
                    json.loads(settings.read_text())["statusLine"]["command"],
                    "my-new-footer",
                )

    def test_uninstall_does_not_replace_a_later_non_command_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            settings.write_text(
                json.dumps(
                    {
                        "statusLine": {
                            "type": "command",
                            "command": "printf prior",
                            "padding": 2,
                        }
                    }
                )
            )
            with Environment(
                LLM_HUD_CLAUDE_SETTINGS=str(settings),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                provider = ClaudeProvider()
                provider.install("/opt/llm-hud")
                payload = json.loads(settings.read_text())
                payload["statusLine"]["padding"] = 9
                settings.write_text(json.dumps(payload))

                reinstall = provider.install("/opt/llm-hud")
                result = provider.uninstall()

            self.assertEqual(reinstall.status, "skipped")
            self.assertEqual(result.status, "skipped")
            self.assertEqual(json.loads(settings.read_text())["statusLine"]["padding"], 9)

    def test_schema_one_state_is_safely_reconstructed_on_uninstall(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            settings.write_text(
                json.dumps(
                    {
                        "statusLine": {
                            "type": "command",
                            "command": "printf prior",
                            "padding": 2,
                        }
                    }
                )
            )
            state_dir = root / "state"
            with Environment(
                LLM_HUD_CLAUDE_SETTINGS=str(settings),
                LLM_HUD_STATE_DIR=str(state_dir),
            ):
                provider = ClaudeProvider()
                provider.install("/opt/llm-hud")
                state_path = state_dir / "providers" / "claude.json"
                state = json.loads(state_path.read_text())
                state["schema"] = 1
                state.pop("installed_status_line")
                state_path.write_text(json.dumps(state))

                result = provider.uninstall()

            self.assertEqual(result.status, "uninstalled")
            restored = json.loads(settings.read_text())
            self.assertEqual(restored["statusLine"]["command"], "printf prior")
            self.assertEqual(restored["statusLine"]["padding"], 2)

    def test_corrupt_state_blocks_install_without_touching_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            settings.write_text('{"theme": "dark"}\n')
            state_path = root / "state" / "providers" / "claude.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text("{broken")
            with Environment(
                LLM_HUD_CLAUDE_SETTINGS=str(settings),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                result = ClaudeProvider().install("/opt/llm-hud")

            self.assertEqual(result.status, "error")
            self.assertIn("invalid installation state", result.message)
            self.assertEqual(settings.read_text(), '{"theme": "dark"}\n')
            self.assertEqual(state_path.read_text(), "{broken")

    def test_incomplete_state_blocks_install_without_being_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            settings.write_text("{}\n")
            state_path = root / "state" / "providers" / "claude.json"
            state_path.parent.mkdir(parents=True)
            incomplete = {
                "schema": 2,
                "settings_path": str(settings),
                "original_present": False,
                "original_status_line": None,
                "installed_command": "/opt/llm-hud render claude",
                "installed_status_line": {"type": "command", "command": "wrong"},
            }
            state_path.write_text(json.dumps(incomplete))
            with Environment(
                LLM_HUD_CLAUDE_SETTINGS=str(settings),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                result = ClaudeProvider().install("/opt/llm-hud")

            self.assertEqual(result.status, "error")
            self.assertIn("inconsistent", result.message)
            self.assertEqual(json.loads(state_path.read_text()), incomplete)

    def test_future_state_schema_blocks_uninstall(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            state_dir = root / "state"
            with Environment(
                LLM_HUD_CLAUDE_SETTINGS=str(settings),
                LLM_HUD_STATE_DIR=str(state_dir),
            ):
                provider = ClaudeProvider()
                provider.install("/opt/llm-hud")
                before = settings.read_text()
                state_path = state_dir / "providers" / "claude.json"
                state = json.loads(state_path.read_text())
                state["schema"] = 999
                state_path.write_text(json.dumps(state))
                result = provider.uninstall()

            self.assertEqual(result.status, "error")
            self.assertIn("newer than supported", result.message)
            self.assertEqual(settings.read_text(), before)
            self.assertEqual(json.loads(state_path.read_text())["schema"], 999)

    def test_state_for_another_settings_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.json"
            second = root / "second.json"
            state_dir = root / "state"
            with Environment(
                LLM_HUD_CLAUDE_SETTINGS=str(first),
                LLM_HUD_STATE_DIR=str(state_dir),
            ):
                ClaudeProvider().install("/opt/llm-hud")
            with Environment(
                LLM_HUD_CLAUDE_SETTINGS=str(second),
                LLM_HUD_STATE_DIR=str(state_dir),
            ):
                result = ClaudeProvider().install("/opt/llm-hud")

            self.assertEqual(result.status, "error")
            self.assertIn("not current path", result.message)
            self.assertFalse(second.exists())

    def test_render_ignores_invalid_state_and_still_outputs_hud(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "state" / "providers" / "claude.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text("invalid")
            with Environment(
                LLM_HUD_CLAUDE_SETTINGS=str(root / "settings.json"),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                output = render(b'{"model":{"display_name":"Opus"}}', color=False)

            self.assertTrue(output.startswith("Claude · Opus\n"))

    def test_install_and_uninstall_preserve_a_relative_settings_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "dotfiles" / "claude.json"
            target.parent.mkdir()
            original = {"theme": "dark"}
            target.write_text(json.dumps(original))
            os.chmod(target, 0o640)
            settings = root / "settings.json"
            settings.symlink_to(Path("dotfiles") / "claude.json")
            state_dir = root / "state"
            with Environment(
                LLM_HUD_CLAUDE_SETTINGS=str(settings),
                LLM_HUD_STATE_DIR=str(state_dir),
            ):
                provider = ClaudeProvider()
                self.assertEqual(provider.install("/opt/llm-hud").status, "installed")
                self.assertTrue(settings.is_symlink())
                self.assertIn("statusLine", json.loads(target.read_text()))
                state = json.loads(
                    (state_dir / "providers" / "claude.json").read_text()
                )
                self.assertEqual(state["settings_path"], str(target.resolve()))
                self.assertEqual(provider.uninstall().status, "uninstalled")

            self.assertTrue(settings.is_symlink())
            self.assertEqual(json.loads(target.read_text()), original)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)

    def test_retargeted_settings_symlink_is_not_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.json"
            first.write_text("{}")
            settings = root / "settings.json"
            settings.symlink_to("first.json")
            state_dir = root / "state"
            with Environment(
                LLM_HUD_CLAUDE_SETTINGS=str(settings),
                LLM_HUD_STATE_DIR=str(state_dir),
            ):
                provider = ClaudeProvider()
                provider.install("/opt/llm-hud")
                second = root / "second.json"
                second.write_text(first.read_text())
                settings.unlink()
                settings.symlink_to("second.json")
                before = second.read_text()
                result = provider.uninstall()

            self.assertEqual(result.status, "error")
            self.assertIn("not current path", result.message)
            self.assertEqual(second.read_text(), before)

    def test_dangling_settings_symlink_does_not_create_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            settings.symlink_to("missing.json")
            state_dir = root / "state"
            with Environment(
                LLM_HUD_CLAUDE_SETTINGS=str(settings),
                LLM_HUD_STATE_DIR=str(state_dir),
            ):
                result = ClaudeProvider().install("/opt/llm-hud")

            self.assertEqual(result.status, "error")
            self.assertTrue(settings.is_symlink())
            self.assertFalse((state_dir / "providers" / "claude.json").exists())

    def test_failed_settings_write_rolls_back_new_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            with Environment(
                LLM_HUD_CLAUDE_SETTINGS=str(root / "settings.json"),
                LLM_HUD_STATE_DIR=str(state_dir),
            ):
                with mock.patch(
                    "llm_hud.providers.claude.atomic_write_json",
                    side_effect=OSError("simulated settings failure"),
                ):
                    result = ClaudeProvider().install("/opt/llm-hud")

            self.assertEqual(result.status, "error")
            self.assertIn("simulated settings failure", result.message)
            self.assertFalse((state_dir / "providers" / "claude.json").exists())


if __name__ == "__main__":
    unittest.main()
