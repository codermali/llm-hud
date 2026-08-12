from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from llm_hud.hud import HudSnapshot, UsageWindow, render_hud
from llm_hud.paths import claude_settings_path, provider_state_path
from llm_hud.providers.base import Provider, ProviderCapabilities, Result
from llm_hud.storage import (
    StateFileError,
    atomic_write_json,
    atomic_write_provider_state,
    read_provider_state,
    resolve_file_target,
    restore_provider_state,
    validate_state_path,
)


STATE_SCHEMAS = frozenset((1, 2))


def _command_argv(command: object) -> tuple[str, str, str] | None:
    if not isinstance(command, str):
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if len(argv) != 3 or argv[1:] != ["render", "claude"]:
        return None
    return argv[0], argv[1], argv[2]


def _is_llm_hud_command(
    command: object, *, installed_command: object = None
) -> bool:
    argv = _command_argv(command)
    if argv is None:
        return False
    if isinstance(installed_command, str) and command == installed_command:
        return True
    return Path(argv[0]).name == "llm-hud"


def _launcher_problem(command: object) -> str | None:
    argv = _command_argv(command)
    if argv is None:
        return "statusLine command is not a valid LLM HUD renderer command"
    executable = argv[0]
    if os.sep not in executable and (os.altsep is None or os.altsep not in executable):
        resolved = shutil.which(executable)
        return None if resolved is not None else f"launcher is not on PATH: {executable}"
    path = Path(executable).expanduser()
    if not path.exists():
        return f"launcher does not exist: {path}"
    if not path.is_file():
        return f"launcher is not a regular file: {path}"
    if not os.access(path, os.X_OK):
        return f"launcher is not executable: {path}"
    return None


def _load_settings(path: Path, *, target: Path | None = None) -> dict[str, Any]:
    target = target or resolve_file_target(path)
    if not target.exists():
        return {}
    with target.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Claude settings must be a JSON object: {path}")
    return payload


def _configured_status_line(
    value: object, command: str, *, drop_refresh_interval: bool = False
) -> dict[str, Any]:
    configured = dict(value) if isinstance(value, dict) else {}
    configured.update({"type": "command", "command": command})
    if drop_refresh_interval:
        configured.pop("refreshInterval", None)
    return configured


def _installed_status_line_from_state(state: object) -> dict[str, Any] | None:
    if not isinstance(state, dict):
        return None
    command = state.get("installed_command")
    if not isinstance(command, str) or not command:
        return None
    installed = state.get("installed_status_line")
    if state.get("schema") == 2:
        return dict(installed) if isinstance(installed, dict) else None
    # Schema 1 did not store the complete installed value, but it can be
    # reconstructed deterministically from the saved original and command.
    # Schema-1 installs also stripped refreshInterval, so the reconstruction
    # must strip it too or existing installs would stop matching on disk.
    if state.get("schema") == 1:
        return _configured_status_line(
            state.get("original_status_line"), command, drop_refresh_interval=True
        )
    return None


def _validate_installation_state(state: dict[str, Any]) -> None:
    if not isinstance(state.get("original_present"), bool):
        raise StateFileError("installation state has no valid original_present flag")
    if "original_status_line" not in state:
        raise StateFileError("installation state has no original_status_line")
    installed = _installed_status_line_from_state(state)
    if installed is None:
        raise StateFileError("installation state is incomplete")
    if _command_argv(state.get("installed_command")) is None:
        raise StateFileError("installation state has an invalid installed_command")
    if state["schema"] == 2 and (
        installed.get("type") != "command"
        or installed.get("command") != state.get("installed_command")
    ):
        raise StateFileError("installation state has inconsistent installed_status_line")


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


def _terminal_columns(payload: dict[str, Any]) -> int | None:
    # Claude Code passes the terminal size to the status line command through
    # the COLUMNS/LINES environment variables; the JSON payload has no
    # terminal field.  The payload lookup stays as a fallback for callers that
    # still provide one.
    raw = os.environ.get("COLUMNS")
    if raw is not None:
        try:
            columns = int(raw)
        except ValueError:
            pass
        else:
            if columns > 0:
                return columns
    terminal = payload.get("terminal")
    columns = terminal.get("columns") if isinstance(terminal, dict) else None
    if isinstance(columns, bool) or not isinstance(columns, int):
        return None
    return columns


def snapshot_from_payload(payload: dict[str, Any]) -> HudSnapshot:
    model = payload.get("model")
    model_name = model.get("display_name") if isinstance(model, dict) else None

    workspace = payload.get("workspace")
    workspace_dir = workspace.get("current_dir") if isinstance(workspace, dict) else None
    cwd = workspace_dir if isinstance(workspace_dir, str) else payload.get("cwd")
    if not isinstance(cwd, str):
        cwd = None

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
        columns=_terminal_columns(payload),
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
    try:
        state = read_provider_state(
            provider_state_path("claude"), supported_schemas=STATE_SCHEMAS
        )
        if state is not None:
            validate_state_path(state, "settings_path", claude_settings_path())
            _validate_installation_state(state)
    except StateFileError:
        state = None
    delegated = _delegate_output(raw, state or {})
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
            settings_target = resolve_file_target(settings_path)
            settings = _load_settings(settings_path, target=settings_target)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            return Result(self.id, "error", str(error))

        installed_command = shlex.join((command_path, "render", "claude"))
        try:
            existing_state = read_provider_state(
                state_path, supported_schemas=STATE_SCHEMAS
            )
            if existing_state is not None:
                validate_state_path(existing_state, "settings_path", settings_path)
                _validate_installation_state(existing_state)
        except StateFileError as error:
            return Result(self.id, "error", str(error))
        current = settings.get("statusLine")
        current_command = current.get("command") if isinstance(current, dict) else None

        if existing_state is not None:
            previous_installed = _installed_status_line_from_state(existing_state)
            assert previous_installed is not None
            if current != previous_installed:
                return Result(
                    self.id,
                    "skipped",
                    "statusLine changed after installation; left it untouched",
                )
            original_present = bool(existing_state.get("original_present"))
            original_status_line = existing_state.get("original_status_line")
        elif _is_llm_hud_command(current_command):
            original_present = False
            original_status_line = None
        else:
            original_present = "statusLine" in settings
            original_status_line = current

        configured = _configured_status_line(current, installed_command)

        state = {
            "schema": 2,
            "settings_path": str(settings_target),
            "original_present": original_present,
            "original_status_line": original_status_line,
            "installed_command": installed_command,
            "installed_status_line": configured,
        }
        state_written = False
        try:
            atomic_write_provider_state(state_path, state)
            state_written = True
            settings["statusLine"] = configured
            atomic_write_json(
                settings_path,
                settings,
                mode=None,
                expected_target=settings_target,
            )
        except OSError as error:
            if state_written:
                try:
                    restore_provider_state(state_path, existing_state)
                except OSError as rollback_error:
                    return Result(
                        self.id,
                        "error",
                        f"{error}; also failed to restore installation state: "
                        f"{rollback_error}",
                    )
            return Result(self.id, "error", str(error))
        return Result(self.id, "installed", f"configured {settings_path}")

    def uninstall(self) -> Result:
        settings_path = claude_settings_path()
        state_path = provider_state_path(self.id)
        try:
            state = read_provider_state(state_path, supported_schemas=STATE_SCHEMAS)
            if state is not None:
                settings_target = validate_state_path(
                    state, "settings_path", settings_path
                )
                _validate_installation_state(state)
        except StateFileError as error:
            return Result(self.id, "error", str(error))
        if state is None:
            return Result(self.id, "skipped", "no installation state")
        try:
            settings = _load_settings(settings_path, target=settings_target)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            return Result(self.id, "error", str(error))

        current = settings.get("statusLine")
        installed = _installed_status_line_from_state(state)
        assert installed is not None
        if current != installed:
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
            atomic_write_json(
                settings_path,
                settings,
                mode=None,
                expected_target=settings_target,
            )
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
        try:
            state = read_provider_state(
                provider_state_path(self.id), supported_schemas=STATE_SCHEMAS
            )
            if state is not None:
                validate_state_path(state, "settings_path", path)
                _validate_installation_state(state)
        except StateFileError as error:
            return False, str(error)

        installed_command = state.get("installed_command") if state is not None else None
        if not _is_llm_hud_command(
            command, installed_command=installed_command
        ):
            return False, f"LLM HUD statusLine command was not found in {path}"
        if state is None:
            return (
                False,
                f"LLM HUD command is present in {path}, but installation state is missing; "
                "run llm-hud install to repair it",
            )
        installed = _installed_status_line_from_state(state)
        if value != installed:
            return False, f"statusLine changed after installation in {path}"
        problem = _launcher_problem(command)
        if problem is not None:
            return False, f"{problem}; configured in {path}"
        return True, f"{path}; launcher is executable"

    def render(self, raw: bytes, no_color: bool = False) -> str:
        return render(raw, color=False if no_color else None)
