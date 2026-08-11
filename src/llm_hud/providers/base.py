from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Result:
    provider: str
    status: str
    message: str


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

    def configured(self) -> tuple[bool, str]:
        raise NotImplementedError

    def render(self, raw: bytes, no_color: bool = False) -> str:
        del raw, no_color
        raise NotImplementedError(f"{self.id} does not support external HUD rendering")
