from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from llm_hud.cli import _version, build_parser
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


if __name__ == "__main__":
    unittest.main()
