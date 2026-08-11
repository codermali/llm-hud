from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
