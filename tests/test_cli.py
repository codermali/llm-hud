from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from llm_hud.cli import _version, build_parser, command_doctor
from llm_hud.providers.claude import ClaudeProvider
from tests.support import Environment


def stub_executable(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    os.chmod(path, 0o755)
    return path


class VersionTests(unittest.TestCase):
    def test_reads_version_from_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            path = stub_executable(Path(directory), "agent", "echo 'agent 1.2.3'")
            self.assertEqual(_version(str(path)), "agent 1.2.3")

    def test_falls_back_to_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            path = stub_executable(Path(directory), "agent", "echo 'agent 9.9' >&2")
            self.assertEqual(_version(str(path)), "agent 9.9")

    def test_keeps_only_the_first_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = stub_executable(Path(directory), "agent", "printf 'v1\\nextra\\n'")
            self.assertEqual(_version(str(path)), "v1")

    def test_missing_command_is_unknown(self):
        self.assertEqual(_version("/nonexistent/agent-cli"), "unknown")


class InstallCommandTests(unittest.TestCase):
    def test_explicit_provider_warns_when_cli_is_absent(self):
        parser = build_parser()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = parser.parse_args(["install", "--provider", "codex"])
            with Environment(
                LLM_HUD_CODEX_BIN="",
                LLM_HUD_CODEX_CONFIG=str(root / "config.toml"),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    args.handler(args)
        self.assertIn("codex CLI was not detected", buffer.getvalue())


class RenderCommandTests(unittest.TestCase):
    def test_render_only_exposes_custom_renderer_providers(self):
        parser = build_parser()
        parser.parse_args(["render", "claude"])
        for provider_id in ("codex", "kimi"):
            with self.subTest(provider=provider_id):
                buffer = io.StringIO()
                with contextlib.redirect_stderr(buffer):
                    with self.assertRaises(SystemExit) as caught:
                        parser.parse_args(["render", provider_id])
                self.assertEqual(caught.exception.code, 2)


class CommandPathTests(unittest.TestCase):
    def test_launcher_state_name_matches_the_runtime_constant(self):
        from llm_hud import cli, runtime

        self.assertEqual(cli._LAUNCHER_STATE_NAME, runtime.LAUNCHER_STATE_NAME)

    def test_managed_dispatch_records_the_external_launcher(self):
        from llm_hud.cli import _command_path

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            (root / "bin").mkdir(parents=True)
            launcher = stub_executable(Path(directory), "llm-hud", "exit 0")
            (root / ".llm-hud-launcher-state.json").write_text(
                '{"schema": 1, "launcher_path": "%s", '
                '"current_sha256": null, "pending_sha256": null}' % launcher
            )
            dispatcher = root / "bin" / "llm-hud"
            with Environment(LLM_HUD_COMMAND_PATH=None):
                with mock.patch.object(sys, "argv", [str(dispatcher), "install"]):
                    self.assertEqual(_command_path(), str(launcher))

                # Without launcher state, fall back to which()/argv[0].
                (root / ".llm-hud-launcher-state.json").unlink()
                with mock.patch.object(sys, "argv", [str(dispatcher), "install"]):
                    self.assertNotEqual(_command_path(), str(launcher))


class UninstallCommandTests(unittest.TestCase):
    def test_conflict_exits_nonzero_and_forget_clears_state(self):
        parser = build_parser()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            state_path = root / "state" / "providers" / "claude.json"
            with Environment(
                LLM_HUD_CLAUDE_SETTINGS=str(settings),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                ClaudeProvider().install("/opt/llm-hud")
                payload = json.loads(settings.read_text())
                payload["statusLine"]["command"] = "my-new-footer"
                settings.write_text(json.dumps(payload))

                args = parser.parse_args(["uninstall", "--provider", "claude"])
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    conflict_exit = args.handler(args)
                self.assertEqual(conflict_exit, 1)
                self.assertIn("[conflict]", buffer.getvalue())

                args = parser.parse_args(
                    ["uninstall", "--provider", "claude", "--forget"]
                )
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    forget_exit = args.handler(args)
                self.assertEqual(forget_exit, 0)
                self.assertIn("[forgotten]", buffer.getvalue())
                self.assertFalse(state_path.exists())
                self.assertEqual(
                    json.loads(settings.read_text())["statusLine"]["command"],
                    "my-new-footer",
                )


class DoctorCommandTests(unittest.TestCase):
    def test_missing_claude_launcher_is_unhealthy_and_uses_cli_override_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claude_cli = stub_executable(
                root, "claude-stub", "echo 'Claude Code 9.9.9'"
            )
            launcher = stub_executable(root, "llm-hud", "exit 0")
            with Environment(
                LLM_HUD_CLAUDE_BIN=str(claude_cli),
                LLM_HUD_CODEX_BIN="",
                LLM_HUD_KIMI_BIN="",
                LLM_HUD_CLAUDE_SETTINGS=str(root / "settings.json"),
                LLM_HUD_CODEX_CONFIG=str(root / "config.toml"),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                ClaudeProvider().install(str(launcher))
                launcher.unlink()
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    result = command_doctor(None)  # type: ignore[arg-type]

            self.assertEqual(result, 1)
            self.assertIn("Claude Code 9.9.9", buffer.getvalue())
            self.assertIn("launcher does not exist", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
