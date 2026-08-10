from __future__ import annotations

import os
from pathlib import Path


def home_dir() -> Path:
    return Path(os.environ.get("LLM_HUD_HOME", Path.home())).expanduser()


def state_dir() -> Path:
    override = os.environ.get("LLM_HUD_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return home_dir() / ".config" / "llm-hud"


def provider_state_path(provider_id: str) -> Path:
    return state_dir() / "providers" / f"{provider_id}.json"


def claude_settings_path() -> Path:
    override = os.environ.get("LLM_HUD_CLAUDE_SETTINGS")
    if override:
        return Path(override).expanduser()
    return home_dir() / ".claude" / "settings.json"


def codex_config_path() -> Path:
    override = os.environ.get("LLM_HUD_CODEX_CONFIG")
    if override:
        return Path(override).expanduser()
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home).expanduser() / "config.toml"
    return home_dir() / ".codex" / "config.toml"
