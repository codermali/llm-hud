from importlib import import_module

from llm_hud.providers.base import Provider


# Keys double as provider ids and module names. Values hold the class name and
# whether the provider exposes the external render command. Provider modules
# stay lazily imported so a Claude render tick does not load Codex's TOML code.
_PROVIDER_SPECS: dict[str, tuple[str, bool]] = {
    "claude": ("ClaudeProvider", True),
    "codex": ("CodexProvider", False),
    "kimi": ("KimiProvider", False),
}
PROVIDER_IDS = tuple(_PROVIDER_SPECS)
RENDER_PROVIDER_IDS = tuple(
    provider_id
    for provider_id, (_, supports_render) in _PROVIDER_SPECS.items()
    if supports_render
)


def _load(provider_id: str) -> Provider:
    module = import_module(f"llm_hud.providers.{provider_id}")
    class_name, _ = _PROVIDER_SPECS[provider_id]
    return getattr(module, class_name)()


def providers() -> list[Provider]:
    return [_load(provider_id) for provider_id in PROVIDER_IDS]


def provider_by_id(provider_id: str) -> Provider:
    if provider_id not in _PROVIDER_SPECS:
        raise KeyError(provider_id)
    return _load(provider_id)
