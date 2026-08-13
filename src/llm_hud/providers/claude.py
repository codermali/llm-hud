from __future__ import annotations

import json
import math
import os
import shlex
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Any

from llm_hud.hud import HudSnapshot, UsageWindow, render_hud
from llm_hud.paths import (
    claude_settings_path,
    provider_journal_path,
    provider_state_path,
)
from llm_hud.providers.base import (
    ProviderCapabilities,
    Result,
    TransactionalProvider,
    conflict_message,
    recover_interrupted_transaction,
    run_install_transaction,
    run_uninstall_transaction,
)
from llm_hud.storage import (
    ContentChangedError,
    StateFileError,
    atomic_write_json,
    atomic_write_provider_state,
    read_provider_state,
    read_text_snapshot,
    resolve_file_target,
    restore_provider_state,
    validate_state_path,
    validate_text_snapshot,
)


# State ABI: rollback switches only the runtime, never provider state, so a
# release must keep reading every schema the previous release wrote.  Bump the
# written schema only together with tests/test_state_abi.py.
STATE_SCHEMAS = frozenset((1, 2))
_CONFLICT_MESSAGE = conflict_message("statusLine")


def _matches_original(settings: dict[str, Any], state: dict[str, Any]) -> bool:
    """The live settings hold exactly the pre-installation statusLine."""
    present = "statusLine" in settings
    if present != bool(state.get("original_present")):
        return False
    return not present or settings.get("statusLine") == state.get(
        "original_status_line"
    )


def _recover_interrupted(
    state_path: Path, journal_path: Path, settings: dict[str, Any]
) -> None:
    """Recover an interrupted transaction from the live settings content."""

    def config_matches_installed(state: dict[str, Any]) -> bool:
        installed = _installed_status_line_from_state(state)
        return installed is not None and settings.get("statusLine") == installed

    recover_interrupted_transaction(
        state_path,
        journal_path,
        config_matches_installed,
        write_state=atomic_write_provider_state,
        restore_state=restore_provider_state,
    )


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


def _load_settings_snapshot(
    path: Path, *, target: Path | None = None
) -> tuple[dict[str, Any], bytes | None]:
    target = target or resolve_file_target(path)
    text, snapshot = read_text_snapshot(target)
    if snapshot is None:
        return {}, None
    if text.startswith("\ufeff"):
        # Surface the actual problem instead of a parser error pointing at
        # line 1 column 1.
        raise ValueError(
            f"Claude settings {path} begin with a UTF-8 byte order mark; "
            "save the file without a BOM and retry"
        )
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError(f"Claude settings must be a JSON object: {path}")
    return payload, snapshot


def _load_settings(path: Path, *, target: Path | None = None) -> dict[str, Any]:
    settings, _ = _load_settings_snapshot(path, target=target)
    return settings


def _configured_status_line(
    value: object, command: str, *, drop_refresh_interval: bool = False
) -> dict[str, Any]:
    configured = dict(value) if isinstance(value, dict) else {}
    configured.update({"type": "command", "command": command})
    if drop_refresh_interval:
        configured.pop("refreshInterval", None)
    return configured


def _status_line_command(command_path: str) -> str:
    # Claude runs status-line commands through Git Bash on Windows. Forward
    # slashes avoid Git Bash interpreting an unquoted backslash as an escape.
    if os.name == "nt":
        command_path = command_path.replace("\\", "/")
    return shlex.join((command_path, "render", "claude"))


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


def _used(window: object) -> float | None:
    if not isinstance(window, dict):
        return None
    value = window.get("used_percentage")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric):
        return None
    return max(0.0, min(100.0, numeric))


def _resets_at(window: object) -> float | None:
    if not isinstance(window, dict):
        return None
    value = window.get("resets_at")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


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

    # rate_limits appears only for Claude subscription accounts and only after
    # the first response; each window may be independently absent.  Absent data
    # renders nothing rather than a misleading placeholder.
    limits = payload.get("rate_limits")
    limits = limits if isinstance(limits, dict) else {}
    windows = tuple(
        UsageWindow(label, _used(limits[key]), _resets_at(limits[key]))
        for key, label in (("five_hour", "5h"), ("seven_day", "7d"))
        if key in limits
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


def _kill_delegated(process: subprocess.Popen) -> None:
    """Terminate a timed-out delegated command together with its descendants.

    Killing only the direct child leaves the user's actual status-line program
    running: orphaned on POSIX, and on Windows still holding the inherited
    stdout pipe open, which would block past the documented five-second bound.
    """
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        process.kill()


def _delegate_output(raw: bytes, state: dict[str, Any], timeout: float = 5.0) -> str:
    original = state.get("original_status_line")
    if not isinstance(original, dict):
        return ""
    command = original.get("command")
    if (
        not isinstance(command, str)
        or not command
        or _is_llm_hud_command(
            command, installed_command=state.get("installed_command")
        )
    ):
        return ""
    delegated_command: str | list[str] = command
    use_shell = True
    extra: dict[str, Any] = {"start_new_session": True}
    if os.name == "nt":
        bash = shutil.which("bash")
        if bash is None:
            return ""
        delegated_command = [bash, "-c", command]
        use_shell = False
        extra = {}
    try:
        process = subprocess.Popen(
            delegated_command,
            shell=use_shell,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            **extra,
        )
    except OSError:
        return ""
    try:
        stdout, _ = process.communicate(input=raw, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        _kill_delegated(process)
        try:
            process.communicate(timeout=1)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        return ""
    return stdout.decode("utf-8", errors="replace").rstrip("\r\n")


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


class ClaudeProvider(TransactionalProvider):
    id = "claude"
    command = "claude"
    capabilities = ProviderCapabilities(
        integration="command",
        custom_renderer=True,
        persistent_metrics=("model", "cwd", "quota"),
    )

    def _install_locked(self, command_path: str) -> Result:
        settings_path = claude_settings_path()
        state_path = provider_state_path(self.id)
        journal_path = provider_journal_path(self.id)
        try:
            settings_target = resolve_file_target(settings_path)
            settings, settings_snapshot = _load_settings_snapshot(
                settings_path, target=settings_target
            )
        except (OSError, json.JSONDecodeError, ValueError) as error:
            return Result(self.id, "error", str(error))

        installed_command = _status_line_command(command_path)
        try:
            _recover_interrupted(state_path, journal_path, settings)
            existing_state = read_provider_state(
                state_path, supported_schemas=STATE_SCHEMAS
            )
            if existing_state is not None:
                validate_state_path(existing_state, "settings_path", settings_path)
                _validate_installation_state(existing_state)
        except (OSError, StateFileError) as error:
            return Result(self.id, "error", str(error))
        current = settings.get("statusLine")
        current_command = current.get("command") if isinstance(current, dict) else None

        if existing_state is not None:
            previous_installed = _installed_status_line_from_state(existing_state)
            assert previous_installed is not None
            if current != previous_installed and not _matches_original(
                settings, existing_state
            ):
                return Result(self.id, "conflict", _CONFLICT_MESSAGE)
            # current matches either the installed value (normal reconfigure)
            # or the pre-install original (interrupted install or manual
            # revert); both are safe to configure from the saved original.
            original_present = bool(existing_state.get("original_present"))
            original_status_line = existing_state.get("original_status_line")
        elif _is_llm_hud_command(
            current_command, installed_command=installed_command
        ):
            # Recognize our own renderer even under a custom launcher name so
            # a reinstall after --forget cannot record it as the original.
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
        def write_config() -> None:
            settings["statusLine"] = configured
            atomic_write_json(
                settings_path,
                settings,
                mode=None,
                expected_target=settings_target,
                expected_content=settings_snapshot,
            )

        failure = run_install_transaction(
            self.id,
            state_path,
            journal_path,
            previous_state=existing_state,
            pending_state=state,
            write_state=atomic_write_provider_state,
            restore_state=restore_provider_state,
            write_config=write_config,
        )
        if failure is not None:
            return failure
        return Result(self.id, "installed", f"configured {settings_path}")

    def _uninstall_locked(self) -> Result:
        settings_path = claude_settings_path()
        state_path = provider_state_path(self.id)
        journal_path = provider_journal_path(self.id)
        try:
            settings_target = resolve_file_target(settings_path)
            settings, settings_snapshot = _load_settings_snapshot(
                settings_path, target=settings_target
            )
        except (OSError, json.JSONDecodeError, ValueError) as error:
            return Result(self.id, "error", str(error))
        try:
            _recover_interrupted(state_path, journal_path, settings)
            state = read_provider_state(state_path, supported_schemas=STATE_SCHEMAS)
            if state is not None:
                validate_state_path(state, "settings_path", settings_path)
                _validate_installation_state(state)
        except (OSError, StateFileError) as error:
            return Result(self.id, "error", str(error))
        if state is None:
            return Result(self.id, "skipped", "no installation state")

        current = settings.get("statusLine")
        installed = _installed_status_line_from_state(state)
        assert installed is not None
        if current != installed:
            if _matches_original(settings, state):
                try:
                    validate_text_snapshot(
                        settings_path,
                        expected_target=settings_target,
                        expected_content=settings_snapshot,
                    )
                    state_path.unlink(missing_ok=True)
                except ContentChangedError as error:
                    return Result(self.id, "conflict", str(error))
                except OSError as error:
                    return Result(self.id, "error", str(error))
                return Result(
                    self.id,
                    "uninstalled",
                    "statusLine already restored; removed installation state",
                )
            return Result(self.id, "conflict", _CONFLICT_MESSAGE)
        if state.get("original_present"):
            settings["statusLine"] = state.get("original_status_line")
        else:
            settings.pop("statusLine", None)

        def write_config() -> None:
            atomic_write_json(
                settings_path,
                settings,
                mode=None,
                expected_target=settings_target,
                expected_content=settings_snapshot,
            )

        failure = run_uninstall_transaction(
            self.id,
            state_path,
            journal_path,
            previous_state=state,
            write_state=atomic_write_provider_state,
            write_config=write_config,
        )
        if failure is not None:
            return failure
        return Result(self.id, "uninstalled", f"restored {settings_path}")

    def configured(self) -> tuple[bool, str]:
        path = claude_settings_path()
        problem = self._interrupted_journal_problem()
        if problem is not None:
            return False, problem
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
