from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from llm_hud.paths import provider_journal_path, provider_state_path
from llm_hud.storage import (
    ContentChangedError,
    ProviderLock,
    read_provider_journal,
)


RESULT_STATUSES = frozenset(
    ("installed", "uninstalled", "skipped", "builtin", "conflict", "forgotten", "error")
)


@dataclass(frozen=True)
class Result:
    provider: str
    status: str
    message: str

    def __post_init__(self) -> None:
        if self.status not in RESULT_STATUSES:
            raise ValueError(f"unknown result status: {self.status}")

    @property
    def failed(self) -> bool:
        """Statuses that must make the CLI exit non-zero."""
        return self.status in ("error", "conflict")


@dataclass(frozen=True)
class ProviderCapabilities:
    integration: Literal["command", "native", "builtin"]
    persistent_metrics: tuple[str, ...]
    on_demand_metrics: tuple[str, ...] = ()


def conflict_message(field: str) -> str:
    """The refusal shown when a managed field was customized after install."""
    return (
        f"{field} was customized after installation; left it untouched "
        "(run llm-hud uninstall --forget to abandon the saved state)"
    )


def recover_interrupted_transaction(
    state_path: Path,
    journal_path: Path,
    config_matches_installed: Callable[[dict[str, Any]], bool],
    *,
    write_state: Callable[[Path, Any], None],
    restore_state: Callable[[Path, dict[str, Any] | None], None],
) -> None:
    """Commit or roll back a provider transaction interrupted mid-write.

    A journal exists only between the first and last write of an
    install/uninstall; whether the configuration write completed — decided by
    ``config_matches_installed`` against the journaled state — chooses the
    direction, so recovery is idempotent under repeated crashes.  The state
    writers are parameters for the same reason as in the transaction
    functions: each provider module must stay the single seam through which
    all of its state writes flow, recovery included.
    """
    journal = read_provider_journal(journal_path)
    if journal is None:
        return
    previous = journal.get("previous_state")
    pending = journal.get("pending_state")
    if journal.get("op") == "install" and isinstance(pending, dict):
        if config_matches_installed(pending):
            # The configuration write completed: commit the pending state.
            write_state(state_path, pending)
        else:
            restore_state(
                state_path, previous if isinstance(previous, dict) else None
            )
        journal_path.unlink(missing_ok=True)
        return
    if journal.get("op") == "uninstall" and isinstance(previous, dict):
        if config_matches_installed(previous):
            # The configuration restore never happened: abort the uninstall.
            write_state(state_path, previous)
        else:
            state_path.unlink(missing_ok=True)
        journal_path.unlink(missing_ok=True)
        return
    # Unrecognizable journal content: roll back and rely on state healing.
    restore_state(
        state_path, previous if isinstance(previous, dict) else None
    )
    journal_path.unlink(missing_ok=True)


def run_install_transaction(
    provider_id: str,
    state_path: Path,
    journal_path: Path,
    *,
    previous_state: dict[str, Any] | None,
    pending_state: dict[str, Any],
    write_state: Callable[[Path, Any], None],
    restore_state: Callable[[Path, dict[str, Any] | None], None],
    write_config: Callable[[], None],
) -> Result | None:
    """Write journal, then state, then configuration; roll back on failure.

    Returns ``None`` once the transaction committed so the caller supplies its
    own success Result.  The state writers are parameters instead of imports
    so each provider module stays the single seam through which all of its
    state writes flow.
    """
    journal = {
        "schema": 1,
        "op": "install",
        "previous_state": previous_state,
        "pending_state": pending_state,
    }
    state_written = False
    try:
        write_state(journal_path, journal)
        write_state(state_path, pending_state)
        state_written = True
        write_config()
    except OSError as error:
        try:
            if state_written:
                restore_state(state_path, previous_state)
            journal_path.unlink(missing_ok=True)
        except OSError as rollback_error:
            return Result(
                provider_id,
                "error",
                f"{error}; also failed to restore installation state: "
                f"{rollback_error}",
            )
        status = "conflict" if isinstance(error, ContentChangedError) else "error"
        return Result(provider_id, status, str(error))
    try:
        journal_path.unlink(missing_ok=True)
    except OSError:
        pass  # the next operation recovers a stale journal
    return None


def run_uninstall_transaction(
    provider_id: str,
    state_path: Path,
    journal_path: Path,
    *,
    previous_state: dict[str, Any],
    write_state: Callable[[Path, Any], None],
    write_config: Callable[[], None],
) -> Result | None:
    """Write journal, restore configuration, then drop state and journal.

    Returns ``None`` once the transaction committed so the caller supplies its
    own success Result.  ``write_state`` is a parameter for the same reason as
    in ``run_install_transaction``.
    """
    journal = {
        "schema": 1,
        "op": "uninstall",
        "previous_state": previous_state,
        "pending_state": None,
    }
    try:
        write_state(journal_path, journal)
        write_config()
        state_path.unlink(missing_ok=True)
        journal_path.unlink(missing_ok=True)
    except ContentChangedError as error:
        try:
            journal_path.unlink(missing_ok=True)
        except OSError as cleanup_error:
            return Result(
                provider_id,
                "error",
                f"{error}; also failed to clear transaction journal: "
                f"{cleanup_error}",
            )
        return Result(provider_id, "conflict", str(error))
    except OSError as error:
        return Result(provider_id, "error", str(error))
    return None


class Provider:
    id: str
    command: str
    capabilities: ProviderCapabilities

    def executable(self) -> str | None:
        override = os.environ.get(f"LLM_HUD_{self.id.upper()}_BIN")
        if override is not None:
            return override or None
        return shutil.which(self.command)

    def available(self) -> bool:
        return self.executable() is not None

    def install(self, command_path: str) -> Result:
        raise NotImplementedError

    def uninstall(self) -> Result:
        raise NotImplementedError

    def forget(self) -> Result:
        """Abandon saved installation state without touching provider config."""
        path = provider_state_path(self.id)
        journal_path = provider_journal_path(self.id)
        journal_removed = False
        try:
            with ProviderLock(path):
                try:
                    journal_path.unlink()
                    journal_removed = True
                except FileNotFoundError:
                    pass
                path.unlink()
        except FileNotFoundError:
            if journal_removed:
                return Result(
                    self.id,
                    "forgotten",
                    f"abandoned interrupted transaction journal {journal_path}; "
                    "no installation state",
                )
            return Result(self.id, "skipped", "no installation state")
        except OSError as error:
            return Result(self.id, "error", str(error))
        if journal_removed:
            return Result(
                self.id,
                "forgotten",
                f"abandoned installation state {path} and its interrupted "
                "transaction journal",
            )
        return Result(self.id, "forgotten", f"abandoned installation state {path}")

    def configured(self) -> tuple[bool, str]:
        raise NotImplementedError

    def render(self, raw: bytes, no_color: bool = False) -> str:
        del raw, no_color
        raise NotImplementedError(f"{self.id} does not support external HUD rendering")


class TransactionalProvider(Provider):
    """A provider whose install/uninstall are journaled config transactions."""

    def install(self, command_path: str) -> Result:
        try:
            with ProviderLock(provider_state_path(self.id)):
                return self._install_locked(command_path)
        except OSError as error:
            return Result(self.id, "error", str(error))

    def _install_locked(self, command_path: str) -> Result:
        raise NotImplementedError

    def uninstall(self) -> Result:
        try:
            with ProviderLock(provider_state_path(self.id)):
                return self._uninstall_locked()
        except OSError as error:
            return Result(self.id, "error", str(error))

    def _uninstall_locked(self) -> Result:
        raise NotImplementedError

    def _interrupted_journal_problem(self) -> str | None:
        """configured() must fail while a transaction awaits recovery."""
        journal_path = provider_journal_path(self.id)
        if not journal_path.is_file():
            return None
        return (
            f"interrupted transaction journal {journal_path}; "
            "run llm-hud install or uninstall to recover it"
        )
