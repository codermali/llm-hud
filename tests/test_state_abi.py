"""Pins the provider-state upgrade compatibility contract.

A release must keep reading every state schema the previous release wrote
(N-1). Bumping a written schema is a conscious act: keep the old schema
readable, update the pins here, and describe the migration in the commit.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from llm_hud.providers import claude, codex
from llm_hud.providers.claude import ClaudeProvider, render
from llm_hud.providers.codex import CodexProvider
from llm_hud.storage import JOURNAL_SCHEMAS
from tests.support import Environment


# Schemas written by the previous release (v0.4.0), plus the schemas written by
# the current tree. Advance these pins after every release: the current runtime
# must continue to read N-1 state during an upgrade.
PREVIOUS_RELEASE_WRITES = {"claude": 2, "codex": 1}
CURRENT_WRITES = {"claude": 2, "codex": 1}


class StateAbiTests(unittest.TestCase):
    def test_previous_release_schemas_stay_readable(self):
        for provider_id, module in (("claude", claude), ("codex", codex)):
            self.assertIn(
                PREVIOUS_RELEASE_WRITES[provider_id],
                module.STATE_SCHEMAS,
                provider_id,
            )
        self.assertEqual(JOURNAL_SCHEMAS, frozenset((1,)))

    def test_written_schemas_are_pinned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text("[tui]\n")
            with Environment(
                LLM_HUD_CLAUDE_SETTINGS=str(root / "settings.json"),
                LLM_HUD_CODEX_CONFIG=str(config),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                ClaudeProvider().install("/opt/llm-hud")
                CodexProvider().install("ignored")
            providers_dir = root / "state" / "providers"
            for provider_id, schema in CURRENT_WRITES.items():
                written = json.loads(
                    (providers_dir / f"{provider_id}.json").read_text()
                )
                self.assertEqual(written["schema"], schema, provider_id)

    def test_future_schema_state_keeps_render_alive_and_blocks_writes(self):
        original_config = '[tui]\nstatus_line = ["current-dir"]\n'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "settings.json"
            config = root / "config.toml"
            config.write_text(original_config)
            providers_dir = root / "state" / "providers"
            providers_dir.mkdir(parents=True)
            future = json.dumps({"schema": 99})
            (providers_dir / "claude.json").write_text(future)
            (providers_dir / "codex.json").write_text(future)
            with Environment(
                LLM_HUD_CLAUDE_SETTINGS=str(settings),
                LLM_HUD_CODEX_CONFIG=str(config),
                LLM_HUD_STATE_DIR=str(root / "state"),
                COLUMNS=None,
            ):
                # The hot path must stay alive on state it cannot read.
                output = render(b'{"model":{"display_name":"Opus"}}', color=False)
                self.assertEqual(output, "Claude · Opus")

                claude_result = ClaudeProvider().uninstall()
                codex_result = CodexProvider().uninstall()
                configured, detail = ClaudeProvider().configured()

            for result in (claude_result, codex_result):
                self.assertEqual(result.status, "error")
                self.assertIn("newer than supported", result.message)
            self.assertFalse(configured)
            self.assertIn("newer than supported", detail)
            self.assertFalse(settings.exists())
            self.assertEqual(config.read_text(), original_config)
            self.assertEqual((providers_dir / "claude.json").read_text(), future)
            self.assertEqual((providers_dir / "codex.json").read_text(), future)


if __name__ == "__main__":
    unittest.main()
