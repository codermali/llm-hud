from __future__ import annotations

import tomllib
from typing import Any

from llm_hud.paths import codex_config_path, provider_state_path
from llm_hud.providers.base import Provider, ProviderCapabilities, Result
from llm_hud.storage import atomic_write_json, atomic_write_text, read_json
from llm_hud.toml_edit import remove_key, set_array


HUD_ITEMS = [
    "model-with-reasoning",
    "current-dir",
    "weekly-limit",
    "context-remaining",
]
OBSOLETE_ITEMS = ["five-hour-limit"]


def _load_config() -> tuple[str, dict[str, Any]]:
    path = codex_config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    return text, tomllib.loads(text)


def _status_line(parsed: dict[str, Any]) -> tuple[bool, list[str]]:
    tui = parsed.get("tui")
    if not isinstance(tui, dict) or "status_line" not in tui:
        return False, []
    value = tui.get("status_line")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("tui.status_line must be an array of strings")
    return True, list(value)


def _with_hud(items: list[str]) -> list[str]:
    managed = set(HUD_ITEMS + OBSOLETE_ITEMS)
    extras = [item for item in items if item not in managed]
    return [*HUD_ITEMS, *extras]


def _restore_items(current: list[str], state: dict[str, Any]) -> list[str] | None:
    installed = state.get("installed_items")
    original = state.get("original_items")
    if not isinstance(installed, list) or not isinstance(original, list):
        return None
    projection = [item for item in current if item in installed]
    if projection != installed:
        return None
    extras = [item for item in current if item not in installed and item not in original]
    return [*original, *extras]


class CodexProvider(Provider):
    id = "codex"
    command = "codex"
    capabilities = ProviderCapabilities(
        integration="native",
        custom_renderer=False,
        persistent_metrics=("model", "cwd", "weekly-quota", "context"),
    )

    def install(self, command_path: str) -> Result:
        del command_path
        path = codex_config_path()
        state_path = provider_state_path(self.id)
        try:
            text, parsed = _load_config()
            current_present, current = _status_line(parsed)
        except (OSError, tomllib.TOMLDecodeError, ValueError) as error:
            return Result(self.id, "error", f"cannot read {path}: {error}")

        existing_state = read_json(state_path, {})
        if isinstance(existing_state, dict) and existing_state.get("installed_items"):
            original_present = bool(existing_state.get("original_present"))
            original_items = existing_state.get("original_items", [])
        else:
            original_present = current_present
            original_items = current

        installed = _with_hud(current)
        state = {
            "schema": 1,
            "config_path": str(path),
            "original_present": original_present,
            "original_items": original_items,
            "installed_items": installed,
        }
        try:
            updated = set_array(text, "tui", "status_line", installed)
            atomic_write_json(state_path, state)
            atomic_write_text(path, updated)
        except (OSError, tomllib.TOMLDecodeError) as error:
            return Result(self.id, "error", str(error))
        return Result(self.id, "installed", f"configured {path}")

    def uninstall(self) -> Result:
        path = codex_config_path()
        state_path = provider_state_path(self.id)
        state = read_json(state_path, {})
        if not isinstance(state, dict) or not state:
            return Result(self.id, "skipped", "no installation state")
        try:
            text, parsed = _load_config()
            present, current = _status_line(parsed)
        except (OSError, tomllib.TOMLDecodeError, ValueError) as error:
            return Result(self.id, "error", f"cannot read {path}: {error}")
        if not present:
            state_path.unlink(missing_ok=True)
            return Result(self.id, "uninstalled", "status line already removed")

        restored = _restore_items(current, state)
        if restored is None:
            return Result(
                self.id,
                "skipped",
                "status_line changed after installation; left it untouched",
            )
        try:
            if not state.get("original_present") and not restored:
                updated = remove_key(text, "tui", "status_line")
            else:
                updated = set_array(text, "tui", "status_line", restored)
            atomic_write_text(path, updated)
            state_path.unlink(missing_ok=True)
        except (OSError, tomllib.TOMLDecodeError) as error:
            return Result(self.id, "error", str(error))
        return Result(self.id, "uninstalled", f"restored {path}")

    def configured(self) -> tuple[bool, str]:
        path = codex_config_path()
        try:
            _, parsed = _load_config()
            _, items = _status_line(parsed)
        except (OSError, tomllib.TOMLDecodeError, ValueError) as error:
            return False, str(error)
        return items[: len(HUD_ITEMS)] == HUD_ITEMS, str(path)
