from __future__ import annotations

import os
from pathlib import Path


def home_dir() -> Path:
    override = os.environ.get("LLM_HUD_HOME")
    return Path(override).expanduser() if override else Path.home()


def state_dir() -> Path:
    override = os.environ.get("LLM_HUD_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return home_dir() / ".config" / "llm-hud"


def provider_state_path(provider_id: str) -> Path:
    return state_dir() / "providers" / f"{provider_id}.json"


def provider_journal_path(provider_id: str) -> Path:
    """A pending install/uninstall transaction journal for one provider."""
    return state_dir() / "providers" / f"{provider_id}.journal.json"


def provider_observation_path(provider_id: str) -> Path:
    """Where a provider records what it last saw, for staleness reporting."""
    return state_dir() / "observations" / f"{provider_id}.json"


def claude_settings_path() -> Path:
    override = os.environ.get("LLM_HUD_CLAUDE_SETTINGS")
    if override:
        return Path(override).expanduser()
    # Claude Code documents CLAUDE_CONFIG_DIR as its configuration root.
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir).expanduser() / "settings.json"
    return home_dir() / ".claude" / "settings.json"


def codex_config_path() -> Path:
    override = os.environ.get("LLM_HUD_CODEX_CONFIG")
    if override:
        return Path(override).expanduser()
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home).expanduser() / "config.toml"
    return home_dir() / ".codex" / "config.toml"
