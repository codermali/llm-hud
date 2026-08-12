from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Literal

from llm_hud.paths import provider_journal_path, provider_state_path
from llm_hud.storage import ProviderLock


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
    custom_renderer: bool
    persistent_metrics: tuple[str, ...]
    on_demand_metrics: tuple[str, ...] = ()


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
        try:
            with ProviderLock(path):
                provider_journal_path(self.id).unlink(missing_ok=True)
                path.unlink()
        except FileNotFoundError:
            return Result(self.id, "skipped", "no installation state")
        except OSError as error:
            return Result(self.id, "error", str(error))
        return Result(self.id, "forgotten", f"abandoned installation state {path}")

    def configured(self) -> tuple[bool, str]:
        raise NotImplementedError

    def render(self, raw: bytes, no_color: bool = False) -> str:
        del raw, no_color
        raise NotImplementedError(f"{self.id} does not support external HUD rendering")
