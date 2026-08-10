from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from llm_hud.hud import HudSnapshot, UsageWindow, render_hud
from llm_hud.paths import claude_settings_path, provider_state_path
from llm_hud.providers.base import Provider, ProviderCapabilities, Result
from llm_hud.storage import atomic_write_json, read_json


def _is_llm_hud_command(command: object) -> bool:
    return isinstance(command, str) and "llm-hud" in command and "render claude" in command


def _load_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Claude settings must be a JSON object: {path}")
    return payload


def _remaining(window: object) -> float | None:
    if not isinstance(window, dict):
        return None
    value = window.get("used_percentage")
    if not isinstance(value, (int, float)):
        return None
    return max(0.0, min(100.0, 100.0 - float(value)))


def _resets_at(window: object) -> float | None:
    if not isinstance(window, dict):
        return None
    value = window.get("resets_at")
    return float(value) if isinstance(value, (int, float)) else None


def snapshot_from_payload(payload: dict[str, Any]) -> HudSnapshot:
    model = payload.get("model")
    model_name = model.get("display_name") if isinstance(model, dict) else None

    workspace = payload.get("workspace")
    workspace_dir = workspace.get("current_dir") if isinstance(workspace, dict) else None
    cwd = workspace_dir if isinstance(workspace_dir, str) else payload.get("cwd")
    if not isinstance(cwd, str):
        cwd = None

    terminal = payload.get("terminal")
    columns = terminal.get("columns") if isinstance(terminal, dict) else None
    if not isinstance(columns, int):
        columns = None

    limits = payload.get("rate_limits")
    limits = limits if isinstance(limits, dict) else {}
    windows = tuple(
        UsageWindow(label, _remaining(limits.get(key)), _resets_at(limits.get(key)))
        for key, label in (("five_hour", "5h"), ("seven_day", "7d"))
    )
    return HudSnapshot(
        provider="Claude",
        model=model_name if isinstance(model_name, str) else None,
        cwd=cwd,
        windows=windows,
        columns=columns,
    )


def render_footer(payload: dict[str, Any], color: bool = True) -> str:
    return render_hud(snapshot_from_payload(payload), color=color)


def _delegate_output(raw: bytes, state: dict[str, Any]) -> str:
    original = state.get("original_status_line")
    if not isinstance(original, dict):
        return ""
    command = original.get("command")
    if not isinstance(command, str) or not command or _is_llm_hud_command(command):
        return ""
    try:
        completed = subprocess.run(
            command,
            shell=True,
            input=raw,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.decode("utf-8", errors="replace").rstrip("\r\n")


def render(raw: bytes, color: bool | None = None) -> str:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if color is None:
        color = not bool(os.environ.get("NO_COLOR"))
    footer = render_footer(payload, color=color)
    state = read_json(provider_state_path("claude"), {})
    delegated = _delegate_output(raw, state if isinstance(state, dict) else {})
    return f"{delegated}\n{footer}" if delegated else footer


class ClaudeProvider(Provider):
    id = "claude"
    command = "claude"
    capabilities = ProviderCapabilities(
        integration="command",
        custom_renderer=True,
        persistent_metrics=("model", "cwd", "quota"),
    )

    def install(self, command_path: str) -> Result:
        settings_path = claude_settings_path()
        state_path = provider_state_path(self.id)
        try:
            settings = _load_settings(settings_path)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            return Result(self.id, "error", str(error))

        installed_command = f"{command_path} render claude"
        existing_state = read_json(state_path, {})
        current = settings.get("statusLine")
        current_command = current.get("command") if isinstance(current, dict) else None

        if isinstance(existing_state, dict) and existing_state.get("installed_command"):
            original_present = bool(existing_state.get("original_present"))
            original_status_line = existing_state.get("original_status_line")
        elif _is_llm_hud_command(current_command):
            original_present = False
            original_status_line = None
        else:
            original_present = "statusLine" in settings
            original_status_line = current

        configured = dict(current) if isinstance(current, dict) else {}
        configured.update({"type": "command", "command": installed_command})
        configured.setdefault("refreshInterval", 30)

        state = {
            "schema": 1,
            "settings_path": str(settings_path),
            "original_present": original_present,
            "original_status_line": original_status_line,
            "installed_command": installed_command,
        }
        try:
            atomic_write_json(state_path, state)
            settings["statusLine"] = configured
            atomic_write_json(settings_path, settings)
        except OSError as error:
            return Result(self.id, "error", str(error))
        return Result(self.id, "installed", f"configured {settings_path}")

    def uninstall(self) -> Result:
        settings_path = claude_settings_path()
        state_path = provider_state_path(self.id)
        state = read_json(state_path, {})
        if not isinstance(state, dict) or not state:
            return Result(self.id, "skipped", "no installation state")
        try:
            settings = _load_settings(settings_path)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            return Result(self.id, "error", str(error))

        current = settings.get("statusLine")
        current_command = current.get("command") if isinstance(current, dict) else None
        if current_command != state.get("installed_command"):
            return Result(
                self.id,
                "skipped",
                "statusLine changed after installation; left it untouched",
            )
        if state.get("original_present"):
            settings["statusLine"] = state.get("original_status_line")
        else:
            settings.pop("statusLine", None)
        try:
            atomic_write_json(settings_path, settings)
            state_path.unlink(missing_ok=True)
        except OSError as error:
            return Result(self.id, "error", str(error))
        return Result(self.id, "uninstalled", f"restored {settings_path}")

    def configured(self) -> tuple[bool, str]:
        path = claude_settings_path()
        try:
            settings = _load_settings(path)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            return False, str(error)
        value = settings.get("statusLine")
        command = value.get("command") if isinstance(value, dict) else None
        return _is_llm_hud_command(command), str(path)

    def render(self, raw: bytes, no_color: bool = False) -> str:
        return render(raw, color=False if no_color else None)
