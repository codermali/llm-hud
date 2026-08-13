from __future__ import annotations

from pathlib import Path
from typing import Any

from llm_hud import _tomllib as tomllib
from llm_hud.paths import (
    codex_config_path,
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
    atomic_write_provider_state,
    atomic_write_text,
    read_provider_state,
    read_text_snapshot,
    resolve_file_target,
    restore_provider_state,
    validate_state_path,
    validate_text_snapshot,
)
from llm_hud.toml_edit import remove_key, set_array


HUD_ITEMS = [
    "model-with-reasoning",
    "current-dir",
    "five-hour-limit",
    "weekly-limit",
    "context-remaining",
]
# Previously-managed items that installs must scrub from status_line; none now.
OBSOLETE_ITEMS: list[str] = []
# State ABI: rollback switches only the runtime, never provider state, so a
# release must keep reading every schema the previous release wrote.  Bump the
# written schema only together with tests/test_state_abi.py.
STATE_SCHEMAS = frozenset((1,))
_CONFLICT_MESSAGE = conflict_message("status_line")


def _matches_original(
    present: bool, items: list[str], state: dict[str, Any]
) -> bool:
    """The live config holds exactly the pre-installation status_line."""
    return present == bool(state.get("original_present")) and items == state.get(
        "original_items"
    )


def _load_config_snapshot(
    *, target: Path | None = None
) -> tuple[str, dict[str, Any], bytes | None]:
    path = codex_config_path()
    target = target or resolve_file_target(path)
    text, snapshot = read_text_snapshot(target)
    if text.startswith("\ufeff"):
        # Surface the actual problem instead of a parser error pointing at
        # line 1 column 1.
        raise ValueError(
            "the file begins with a UTF-8 byte order mark, which TOML "
            "forbids; save it without a BOM and retry"
        )
    return text, tomllib.loads(text), snapshot


def _load_config(*, target: Path | None = None) -> tuple[str, dict[str, Any]]:
    text, parsed, _ = _load_config_snapshot(target=target)
    return text, parsed


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


def _recover_interrupted(
    state_path: Path, journal_path: Path, present: bool, items: list[str]
) -> None:
    """Recover an interrupted transaction from the live config content."""

    def config_matches_installed(state: dict[str, Any]) -> bool:
        installed = state.get("installed_items")
        return present and isinstance(installed, list) and items == installed

    recover_interrupted_transaction(
        state_path,
        journal_path,
        config_matches_installed,
        write_state=atomic_write_provider_state,
        restore_state=restore_provider_state,
    )


def _validate_installation_state(state: dict[str, Any]) -> None:
    if not isinstance(state.get("original_present"), bool):
        raise StateFileError("installation state has no valid original_present flag")
    for key in ("original_items", "installed_items"):
        value = state.get(key)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise StateFileError(f"installation state has no valid {key}")


class CodexProvider(TransactionalProvider):
    id = "codex"
    command = "codex"
    capabilities = ProviderCapabilities(
        integration="native",
        custom_renderer=False,
        persistent_metrics=(
            "model",
            "cwd",
            "five-hour-quota",
            "weekly-quota",
            "context",
        ),
    )

    def _install_locked(self, command_path: str) -> Result:
        del command_path
        path = codex_config_path()
        state_path = provider_state_path(self.id)
        journal_path = provider_journal_path(self.id)
        try:
            config_target = resolve_file_target(path)
            text, parsed, config_snapshot = _load_config_snapshot(
                target=config_target
            )
            current_present, current = _status_line(parsed)
        except (OSError, tomllib.TOMLDecodeError, ValueError) as error:
            return Result(self.id, "error", f"cannot read {path}: {error}")

        try:
            _recover_interrupted(state_path, journal_path, current_present, current)
            existing_state = read_provider_state(
                state_path, supported_schemas=STATE_SCHEMAS
            )
            if existing_state is not None:
                validate_state_path(existing_state, "config_path", path)
                _validate_installation_state(existing_state)
        except (OSError, StateFileError) as error:
            return Result(self.id, "error", str(error))
        if existing_state is not None:
            restored = _restore_items(current, existing_state)
            if restored is None and _matches_original(
                current_present, current, existing_state
            ):
                # Interrupted install or manual revert: the config is exactly
                # the pre-install state, so configuring it again is safe.
                restored = list(existing_state["original_items"])
            if restored is None:
                return Result(self.id, "conflict", _CONFLICT_MESSAGE)
            original_present = bool(existing_state.get("original_present"))
            original_items = restored
        else:
            original_present = current_present
            original_items = current

        installed = _with_hud(original_items)
        state = {
            "schema": 1,
            "config_path": str(config_target),
            "original_present": original_present,
            "original_items": original_items,
            "installed_items": installed,
        }
        try:
            updated = set_array(text, "tui", "status_line", installed)
        except (tomllib.TOMLDecodeError, ValueError) as error:
            return Result(self.id, "error", str(error))

        def write_config() -> None:
            atomic_write_text(
                path,
                updated,
                expected_target=config_target,
                expected_content=config_snapshot,
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
        return Result(self.id, "installed", f"configured {path}")

    def _uninstall_locked(self) -> Result:
        path = codex_config_path()
        state_path = provider_state_path(self.id)
        journal_path = provider_journal_path(self.id)
        try:
            config_target = resolve_file_target(path)
            text, parsed, config_snapshot = _load_config_snapshot(
                target=config_target
            )
            present, current = _status_line(parsed)
        except (OSError, tomllib.TOMLDecodeError, ValueError) as error:
            return Result(self.id, "error", f"cannot read {path}: {error}")
        try:
            _recover_interrupted(state_path, journal_path, present, current)
            state = read_provider_state(state_path, supported_schemas=STATE_SCHEMAS)
            if state is not None:
                validate_state_path(state, "config_path", path)
                _validate_installation_state(state)
        except (OSError, StateFileError) as error:
            return Result(self.id, "error", str(error))
        if state is None:
            return Result(self.id, "skipped", "no installation state")
        if not present:
            # An absent status_line restores the pre-install state only when
            # none existed before install; discarding the restore record while
            # original items are saved must stay an explicit --forget decision.
            if state.get("original_present"):
                return Result(self.id, "conflict", _CONFLICT_MESSAGE)
            try:
                validate_text_snapshot(
                    path,
                    expected_target=config_target,
                    expected_content=config_snapshot,
                )
                state_path.unlink(missing_ok=True)
            except ContentChangedError as error:
                return Result(self.id, "conflict", str(error))
            except OSError as error:
                return Result(self.id, "error", str(error))
            return Result(self.id, "uninstalled", "status line already removed")

        restored = _restore_items(current, state)
        if restored is None:
            if _matches_original(present, current, state):
                try:
                    validate_text_snapshot(
                        path,
                        expected_target=config_target,
                        expected_content=config_snapshot,
                    )
                    state_path.unlink(missing_ok=True)
                except ContentChangedError as error:
                    return Result(self.id, "conflict", str(error))
                except OSError as error:
                    return Result(self.id, "error", str(error))
                return Result(
                    self.id,
                    "uninstalled",
                    "status_line already restored; removed installation state",
                )
            return Result(self.id, "conflict", _CONFLICT_MESSAGE)
        try:
            if not state.get("original_present") and not restored:
                updated = remove_key(text, "tui", "status_line")
            else:
                updated = set_array(text, "tui", "status_line", restored)
        except (tomllib.TOMLDecodeError, ValueError) as error:
            return Result(self.id, "error", str(error))

        def write_config() -> None:
            atomic_write_text(
                path,
                updated,
                expected_target=config_target,
                expected_content=config_snapshot,
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
        return Result(self.id, "uninstalled", f"restored {path}")

    def configured(self) -> tuple[bool, str]:
        path = codex_config_path()
        problem = self._interrupted_journal_problem()
        if problem is not None:
            return False, problem
        try:
            _, parsed = _load_config()
            _, items = _status_line(parsed)
        except (OSError, tomllib.TOMLDecodeError, ValueError) as error:
            return False, str(error)
        try:
            state = read_provider_state(
                provider_state_path(self.id), supported_schemas=STATE_SCHEMAS
            )
            if state is not None:
                validate_state_path(state, "config_path", path)
                _validate_installation_state(state)
        except StateFileError as error:
            return False, str(error)
        detail = (
            f"{path}; checked base user config only; project .codex/config.toml "
            "and --profile layers may override it at runtime"
        )
        if items[: len(HUD_ITEMS)] != HUD_ITEMS:
            return False, detail
        if state is None:
            return (
                False,
                f"LLM HUD status_line is present in {path}, but installation "
                "state is missing; run llm-hud install to repair it",
            )
        if _restore_items(items, state) is None:
            return False, f"status_line changed after installation in {path}"
        return True, detail
