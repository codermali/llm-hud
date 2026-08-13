from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from llm_hud import _tomllib as tomllib
import llm_hud.providers.codex as codex_module
from llm_hud.providers.codex import CodexProvider, HUD_ITEMS
from llm_hud.toml_edit import set_array
from tests.support import Environment


def status_line(path: Path):
    return tomllib.loads(path.read_text())["tui"].get("status_line")


class CodexProviderTests(unittest.TestCase):
    def test_persistent_metrics_cover_every_configured_quota_field(self):
        # The providers listing must reflect HUD_ITEMS: five-hour and weekly
        # quotas are both configured, so both belong in the metadata.
        self.assertEqual(
            CodexProvider.capabilities.persistent_metrics,
            ("model", "cwd", "five-hour-quota", "weekly-quota", "context"),
        )

    def test_crlf_config_survives_reinstall_and_uninstall(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            original = b'[tui]\r\nstatus_line = ["current-dir"]\r\n'
            config.write_bytes(original)
            with Environment(
                LLM_HUD_CODEX_CONFIG=str(config),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                provider = CodexProvider()
                installed = provider.install("ignored")
                self.assertEqual(installed.status, "installed", installed.message)
                self.assertNotIn(b"\r\r\n", config.read_bytes())
                reinstalled = provider.install("ignored")
                self.assertEqual(
                    reinstalled.status,
                    "installed",
                    reinstalled.message,
                )
                uninstalled = provider.uninstall()
                self.assertEqual(
                    uninstalled.status,
                    "uninstalled",
                    uninstalled.message,
                )

            self.assertEqual(config.read_bytes(), original)

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
                self.assertEqual(status_line(config).count("five-hour-limit"), 1)
                self.assertEqual(provider.install("ignored").status, "installed")
                self.assertEqual(status_line(config).count("weekly-limit"), 1)

                self.assertEqual(provider.uninstall().status, "uninstalled")
                self.assertEqual(
                    status_line(config),
                    ["current-dir", "git-branch", "five-hour-limit"],
                )
                self.assertIn('model = "gpt-5.5"', config.read_text())

    def test_configures_a_dotted_form_config_without_rewriting_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text('tui.status_line = ["model"]\nmodel = "gpt-5"\n')
            with Environment(
                LLM_HUD_CODEX_CONFIG=str(config),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                provider = CodexProvider()
                self.assertEqual(provider.install("ignored").status, "installed")
                self.assertNotIn("[tui]", config.read_text())
                self.assertEqual(status_line(config), [*HUD_ITEMS, "model"])
                self.assertEqual(provider.uninstall().status, "uninstalled")

            self.assertEqual(status_line(config), ["model"])
            self.assertIn('model = "gpt-5"', config.read_text())

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

    def test_non_bmp_status_line_items_survive_install_and_uninstall(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text(
                '[tui]\nstatus_line = ["current-dir", "emoji-😀"]\n',
                encoding="utf-8",
            )
            with Environment(
                LLM_HUD_CODEX_CONFIG=str(config),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                provider = CodexProvider()
                installed = provider.install("ignored")
                self.assertEqual(installed.status, "installed", installed.message)
                self.assertEqual(status_line(config), [*HUD_ITEMS, "emoji-😀"])
                uninstalled = provider.uninstall()
                self.assertEqual(uninstalled.status, "uninstalled", uninstalled.message)
            self.assertEqual(status_line(config), ["current-dir", "emoji-😀"])

    def test_deleted_status_line_with_saved_original_is_a_conflict(self):
        # The saved original can only be abandoned by an explicit --forget;
        # uninstall must not treat a user-deleted status_line as "restored".
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
                config.write_text("[tui]\n")
                result = provider.uninstall()
                self.assertEqual(result.status, "conflict", result.message)
                self.assertEqual(config.read_text(), "[tui]\n")
                self.assertTrue(
                    (root / "state" / "providers" / "codex.json").is_file()
                )
                # Reverting the deletion makes the saved original usable again.
                config.write_text('[tui]\nstatus_line = ["current-dir"]\n')
                self.assertEqual(provider.install("ignored").status, "installed")
                self.assertEqual(provider.uninstall().status, "uninstalled")
            self.assertEqual(status_line(config), ["current-dir"])

    def test_bom_config_reports_the_bom_instead_of_a_parse_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            original = "\ufeff[tui]\n".encode("utf-8")
            config.write_bytes(original)
            with Environment(
                LLM_HUD_CODEX_CONFIG=str(config),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                result = CodexProvider().install("ignored")
            self.assertEqual(result.status, "error")
            self.assertIn("byte order mark", result.message)
            self.assertEqual(config.read_bytes(), original)

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

    def test_reinstall_keeps_later_items_out_of_the_managed_set(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text('[tui]\nstatus_line = ["current-dir"]\n')
            state_dir = root / "state"
            with Environment(
                LLM_HUD_CODEX_CONFIG=str(config),
                LLM_HUD_STATE_DIR=str(state_dir),
            ):
                provider = CodexProvider()
                provider.install("ignored")
                config.write_text(
                    set_array(
                        config.read_text(),
                        "tui",
                        "status_line",
                        [*status_line(config), "git-branch"],
                    )
                )

                self.assertEqual(provider.install("ignored").status, "installed")
                state = json.loads(
                    (state_dir / "providers" / "codex.json").read_text()
                )
                self.assertEqual(
                    state["original_items"], ["current-dir", "git-branch"]
                )
                self.assertEqual(provider.uninstall().status, "uninstalled")

            self.assertEqual(status_line(config), ["current-dir", "git-branch"])

    def test_reinstall_preserves_later_items_when_status_line_was_originally_absent(
        self,
    ):
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
                config.write_text(
                    set_array(
                        config.read_text(),
                        "tui",
                        "status_line",
                        [*status_line(config), "git-branch"],
                    )
                )
                provider.install("ignored")
                provider.uninstall()

            self.assertEqual(status_line(config), ["git-branch"])

    def test_reinstall_does_not_readd_a_managed_item_removed_later(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text('[tui]\nstatus_line = ["current-dir"]\n')
            state_dir = root / "state"
            with Environment(
                LLM_HUD_CODEX_CONFIG=str(config),
                LLM_HUD_STATE_DIR=str(state_dir),
            ):
                provider = CodexProvider()
                provider.install("ignored")
                changed = [
                    item for item in status_line(config) if item != "weekly-limit"
                ]
                config.write_text(
                    set_array(config.read_text(), "tui", "status_line", changed)
                )
                state_path = state_dir / "providers" / "codex.json"
                state_before = state_path.read_bytes()

                result = provider.install("ignored")

            self.assertEqual(result.status, "conflict")
            self.assertEqual(status_line(config), changed)
            self.assertEqual(state_path.read_bytes(), state_before)

    def test_reverted_status_line_heals_on_reinstall_and_uninstall(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            original = '[tui]\nstatus_line = ["current-dir"]\n'
            config.write_text(original)
            state_path = root / "state" / "providers" / "codex.json"
            with Environment(
                LLM_HUD_CODEX_CONFIG=str(config),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                provider = CodexProvider()
                provider.install("ignored")
                config.write_text(original)  # the user reverted our edit
                self.assertEqual(provider.install("ignored").status, "installed")
                self.assertEqual(status_line(config), HUD_ITEMS)

                config.write_text(original)  # reverted again
                self.assertEqual(provider.uninstall().status, "uninstalled")
                self.assertFalse(state_path.exists())
                self.assertEqual(config.read_text(), original)

    def test_crash_between_state_and_config_writes_is_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            original = '[tui]\nstatus_line = ["current-dir"]\n'
            config.write_text(original)
            providers_dir = root / "state" / "providers"
            with Environment(
                LLM_HUD_CODEX_CONFIG=str(config),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                provider = CodexProvider()
                # Crash after the journal and state writes, before the config.
                with mock.patch(
                    "llm_hud.providers.codex.atomic_write_text",
                    side_effect=KeyboardInterrupt,
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        provider.install("ignored")
                self.assertTrue((providers_dir / "codex.journal.json").exists())
                self.assertEqual(config.read_text(), original)

                # The next install recovers the journal and completes.
                self.assertEqual(provider.install("ignored").status, "installed")
                self.assertFalse((providers_dir / "codex.journal.json").exists())
                self.assertEqual(status_line(config), HUD_ITEMS)

                self.assertEqual(provider.uninstall().status, "uninstalled")
                self.assertEqual(config.read_text(), original)

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
                self.assertEqual(result.status, "conflict")
                self.assertIn("--forget", result.message)
                self.assertEqual(status_line(config), changed)

    def test_install_rejects_a_config_edit_observed_before_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            original = 'model = "gpt-5"\n'
            external = 'model = "gpt-5.5"\nexternal_edit = true\n'
            config.write_text(original)
            providers_dir = root / "state" / "providers"
            state_path = providers_dir / "codex.json"
            original_write = codex_module.atomic_write_provider_state

            def write_state(path, payload):
                original_write(path, payload)
                if path == state_path:
                    config.write_text(external)

            with Environment(
                LLM_HUD_CODEX_CONFIG=str(config),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                with mock.patch(
                    "llm_hud.providers.codex.atomic_write_provider_state",
                    side_effect=write_state,
                ):
                    result = CodexProvider().install("ignored")

            self.assertEqual(result.status, "conflict")
            self.assertIn("retry", result.message)
            self.assertEqual(config.read_text(), external)
            self.assertFalse(state_path.exists())
            self.assertFalse((providers_dir / "codex.journal.json").exists())

    def test_uninstall_rejects_a_config_edit_observed_before_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text('model = "gpt-5"\n')
            providers_dir = root / "state" / "providers"
            journal_path = providers_dir / "codex.journal.json"
            original_write = codex_module.atomic_write_provider_state
            with Environment(
                LLM_HUD_CODEX_CONFIG=str(config),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                provider = CodexProvider()
                self.assertEqual(provider.install("ignored").status, "installed")

                def write_journal(path, payload):
                    original_write(path, payload)
                    if path == journal_path:
                        config.write_text(
                            f'external_edit = true\n{config.read_text()}'
                        )

                with mock.patch(
                    "llm_hud.providers.codex.atomic_write_provider_state",
                    side_effect=write_journal,
                ):
                    result = provider.uninstall()

            parsed = tomllib.loads(config.read_text())
            self.assertEqual(result.status, "conflict")
            self.assertTrue(parsed["external_edit"])
            self.assertEqual(parsed["tui"]["status_line"], HUD_ITEMS)
            self.assertTrue((providers_dir / "codex.json").exists())
            self.assertFalse(journal_path.exists())

    def test_restored_config_change_before_state_cleanup_keeps_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            original = '[tui]\nstatus_line = ["current-dir"]\n'
            config.write_text(original)
            state_path = root / "state" / "providers" / "codex.json"
            with Environment(
                LLM_HUD_CODEX_CONFIG=str(config),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                provider = CodexProvider()
                self.assertEqual(provider.install("ignored").status, "installed")
                installed = config.read_bytes()
                config.write_text(original)
                validate_snapshot = codex_module.validate_text_snapshot

                def restore_hud_then_validate(path, **kwargs):
                    config.write_bytes(installed)
                    return validate_snapshot(path, **kwargs)

                with mock.patch(
                    "llm_hud.providers.codex.validate_text_snapshot",
                    side_effect=restore_hud_then_validate,
                ):
                    result = provider.uninstall()

            self.assertEqual(result.status, "conflict")
            self.assertIn("retry", result.message)
            self.assertTrue(state_path.exists())
            self.assertEqual(status_line(config), HUD_ITEMS)

    def test_future_state_schema_blocks_install_without_touching_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            original = '[tui]\nstatus_line = ["current-dir"]\n'
            config.write_text(original)
            state_path = root / "state" / "providers" / "codex.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({"schema": 999}))
            with Environment(
                LLM_HUD_CODEX_CONFIG=str(config),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                result = CodexProvider().install("ignored")

            self.assertEqual(result.status, "error")
            self.assertIn("newer than supported", result.message)
            self.assertEqual(config.read_text(), original)
            self.assertEqual(json.loads(state_path.read_text())["schema"], 999)

    def test_malformed_state_fields_block_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text("[tui]\n")
            state_path = root / "state" / "providers" / "codex.json"
            state_path.parent.mkdir(parents=True)
            malformed = {
                "schema": 1,
                "config_path": str(config),
                "original_present": False,
                "original_items": [],
                "installed_items": [42],
            }
            state_path.write_text(json.dumps(malformed))
            with Environment(
                LLM_HUD_CODEX_CONFIG=str(config),
                LLM_HUD_STATE_DIR=str(root / "state"),
            ):
                result = CodexProvider().install("ignored")

            self.assertEqual(result.status, "error")
            self.assertIn("installed_items", result.message)
            self.assertEqual(json.loads(state_path.read_text()), malformed)

    def test_state_for_another_config_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.toml"
            second = root / "second.toml"
            state_dir = root / "state"
            with Environment(
                LLM_HUD_CODEX_CONFIG=str(first),
                LLM_HUD_STATE_DIR=str(state_dir),
            ):
                CodexProvider().install("ignored")
            with Environment(
                LLM_HUD_CODEX_CONFIG=str(second),
                LLM_HUD_STATE_DIR=str(state_dir),
            ):
                result = CodexProvider().install("ignored")

            self.assertEqual(result.status, "error")
            self.assertIn("not current path", result.message)
            self.assertFalse(second.exists())

    def test_install_and_uninstall_preserve_a_relative_config_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "dotfiles" / "codex.toml"
            target.parent.mkdir()
            original = '[tui]\nnotifications = true\n'
            target.write_text(original)
            config = root / "config.toml"
            config.symlink_to(Path("dotfiles") / "codex.toml")
            state_dir = root / "state"
            with Environment(
                LLM_HUD_CODEX_CONFIG=str(config),
                LLM_HUD_STATE_DIR=str(state_dir),
            ):
                provider = CodexProvider()
                self.assertEqual(provider.install("ignored").status, "installed")
                self.assertTrue(config.is_symlink())
                self.assertEqual(status_line(target), HUD_ITEMS)
                state = json.loads(
                    (state_dir / "providers" / "codex.json").read_text()
                )
                self.assertEqual(state["config_path"], str(target.resolve()))
                self.assertEqual(provider.uninstall().status, "uninstalled")

            self.assertTrue(config.is_symlink())
            self.assertEqual(target.read_text(), original)

    def test_configured_discloses_unchecked_runtime_config_layers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text(
                set_array("", "tui", "status_line", HUD_ITEMS)
            )
            with Environment(LLM_HUD_CODEX_CONFIG=str(config)):
                configured, detail = CodexProvider().configured()

            self.assertTrue(configured)
            self.assertIn("base user config", detail)
            self.assertIn(".codex/config.toml", detail)
            self.assertIn("--profile", detail)


if __name__ == "__main__":
    unittest.main()
