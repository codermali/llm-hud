from llm_hud.providers.base import Provider
from llm_hud.providers.claude import ClaudeProvider
from llm_hud.providers.codex import CodexProvider
from llm_hud.providers.kimi import KimiProvider


def providers() -> list[Provider]:
    return [ClaudeProvider(), CodexProvider(), KimiProvider()]


def provider_by_id(provider_id: str) -> Provider:
    for provider in providers():
        if provider.id == provider_id:
            return provider
    raise KeyError(provider_id)
