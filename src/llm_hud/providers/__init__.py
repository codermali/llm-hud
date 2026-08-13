from importlib import import_module

from llm_hud.providers.base import Provider


# Keys double as provider ids and module names (pinned by tests). Modules are
# imported on demand so the "render claude" status-line tick does not pay for
# the other providers' imports, notably codex's TOML machinery.
_PROVIDER_CLASSES = {
    "claude": "ClaudeProvider",
    "codex": "CodexProvider",
    "kimi": "KimiProvider",
}


def _load(provider_id: str) -> Provider:
    module = import_module(f"llm_hud.providers.{provider_id}")
    return getattr(module, _PROVIDER_CLASSES[provider_id])()


def providers() -> list[Provider]:
    return [_load(provider_id) for provider_id in _PROVIDER_CLASSES]


def provider_by_id(provider_id: str) -> Provider:
    if provider_id not in _PROVIDER_CLASSES:
        raise KeyError(provider_id)
    return _load(provider_id)
