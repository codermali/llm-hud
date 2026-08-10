from __future__ import annotations

from llm_hud.providers.base import Provider, ProviderCapabilities, Result


class KimiProvider(Provider):
    id = "kimi"
    command = "kimi"
    capabilities = ProviderCapabilities(
        integration="builtin",
        custom_renderer=False,
        persistent_metrics=("model", "cwd", "context"),
        on_demand_metrics=("quota",),
    )

    def install(self, command_path: str) -> Result:
        del command_path
        if not self.available():
            return Result(self.id, "error", "kimi command was not detected")
        return Result(
            self.id,
            "builtin",
            "uses Kimi's built-in toolbar; quota remains available through /usage",
        )

    def uninstall(self) -> Result:
        return Result(self.id, "skipped", "no Kimi settings were changed")

    def configured(self) -> tuple[bool, str]:
        if not self.available():
            return False, "kimi command was not detected"
        return True, "built-in toolbar; quota is available on demand through /usage"
