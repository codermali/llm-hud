from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from llm_hud.cli import (
    PROVIDER_IDS,
    RENDER_PROVIDER_IDS,
    _probe_version,
    build_parser,
    command_doctor,
)
from llm_hud.providers import provider_by_id, providers
from llm_hud.providers.codex import HUD_ITEMS
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
            self.assertEqual(_probe_version(str(path))[0], "agent 1.2.3")

    def test_falls_back_to_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            path = stub_executable(Path(directory), "agent", "echo 'agent 9.9' >&2")
            self.assertEqual(_probe_version(str(path))[0], "agent 9.9")

    def test_keeps_only_the_first_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = stub_executable(Path(directory), "agent", "printf 'v1\\nextra\\n'")
            self.assertEqual(_probe_version(str(path))[0], "v1")

    def test_missing_command_is_unknown(self):
        self.assertEqual(_probe_version("/nonexistent/agent-cli")[0], "unknown")

    def test_nonzero_version_command_is_reported_as_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = stub_executable(
                Path(directory), "agent", "echo 'broken version' >&2; exit 7"
            )
            self.assertEqual(
                _probe_version(str(path))[0],
                "broken version",
            )

    def test_empty_successful_version_command_remains_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = stub_executable(Path(directory), "agent", "exit 0")
            self.assertEqual(_probe_version(str(path))[0], "unknown")

    def test_non_utf8_version_output_is_replaced_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = stub_executable(Path(directory), "agent", "printf '\\377\\n'")

            version, healthy, detail = _probe_version(str(path))

        self.assertEqual(version, "\ufffd")
        self.assertTrue(healthy)
        self.assertIsNone(detail)


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


class ProviderRegistryTests(unittest.TestCase):
    def test_provider_id_literals_match_the_registered_providers(self):
        self.assertEqual(PROVIDER_IDS, tuple(p.id for p in providers()))
        self.assertEqual(
            RENDER_PROVIDER_IDS,
            tuple(p.id for p in providers() if p.capabilities.custom_renderer),
        )

    def test_provider_by_id_matches_ids_and_rejects_unknown_ones(self):
        for provider_id in PROVIDER_IDS:
            self.assertEqual(provider_by_id(provider_id).id, provider_id)
        with self.assertRaises(KeyError):
            provider_by_id("no-such-provider")

    def test_render_tick_does_not_import_toml_machinery(self):
        # codex needs a TOML parser; pulling it in from the "render claude"
        # hot path would slow every status-line refresh.
        script = (
            "import sys\n"
            "import llm_hud.cli\n"
            "from llm_hud.providers import provider_by_id\n"
            "output = provider_by_id('claude').render(sys.stdin.buffer.read())\n"
            "assert output\n"
            "loaded = sorted(name for name in sys.modules if 'toml' in name)\n"
            "assert not loaded, loaded\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            environment["LLM_HUD_STATE_DIR"] = str(root / "state")
            environment["LLM_HUD_CLAUDE_SETTINGS"] = str(root / "settings.json")
            completed = subprocess.run(
                [sys.executable, "-c", script],
                input=b'{"model": {"display_name": "Opus"}}',
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())


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
    def test_configured_provider_with_missing_cli_is_unhealthy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text(
                f"[tui]\nstatus_line = {json.dumps(HUD_ITEMS)}\n",
                encoding="utf-8",
            )
            with Environment(
                LLM_HUD_CLAUDE_BIN="",
                LLM_HUD_CODEX_BIN="",
                LLM_HUD_KIMI_BIN="",
                LLM_HUD_CLAUDE_SETTINGS=str(root / "settings.json"),
                LLM_HUD_CODEX_CONFIG=str(config),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    result = command_doctor(None)  # type: ignore[arg-type]

        self.assertEqual(result, 1)
        self.assertIn("codex: not installed; configured", buffer.getvalue())

    def test_missing_unconfigured_providers_do_not_make_doctor_unhealthy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with Environment(
                LLM_HUD_CLAUDE_BIN="",
                LLM_HUD_CODEX_BIN="",
                LLM_HUD_KIMI_BIN="",
                LLM_HUD_CLAUDE_SETTINGS=str(root / "settings.json"),
                LLM_HUD_CODEX_CONFIG=str(root / "config.toml"),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    result = command_doctor(None)  # type: ignore[arg-type]

        self.assertEqual(result, 0)
        self.assertIn("claude: not installed; not configured", buffer.getvalue())
        self.assertIn("codex: not installed; not configured", buffer.getvalue())
        self.assertIn("kimi: not installed; not available", buffer.getvalue())

    def test_empty_successful_version_probe_is_unhealthy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_cli = stub_executable(root, "codex-stub", "exit 0")
            config = root / "config.toml"
            config.write_text('[tui]\nstatus_line = ["current-dir"]\n')
            with Environment(
                LLM_HUD_CLAUDE_BIN="",
                LLM_HUD_CODEX_BIN=str(codex_cli),
                LLM_HUD_KIMI_BIN="",
                LLM_HUD_CODEX_CONFIG=str(config),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    result = command_doctor(None)  # type: ignore[arg-type]

            self.assertEqual(result, 1)
            self.assertIn(
                "unknown (version probe returned no output)",
                buffer.getvalue(),
            )
            self.assertIn("configured", buffer.getvalue())

    def test_failed_version_probe_is_unhealthy_even_when_configured(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_cli = stub_executable(
                root, "codex-stub", "echo broken >&2; exit 1"
            )
            config = root / "config.toml"
            config.write_text("[tui]\nstatus_line = [\"current-dir\"]\n")
            with Environment(
                LLM_HUD_CLAUDE_BIN="",
                LLM_HUD_CODEX_BIN=str(codex_cli),
                LLM_HUD_KIMI_BIN="",
                LLM_HUD_CODEX_CONFIG=str(config),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    result = command_doctor(None)  # type: ignore[arg-type]

            self.assertEqual(result, 1)
            self.assertIn("broken (version probe exited 1)", buffer.getvalue())
            self.assertIn("configured", buffer.getvalue())

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
