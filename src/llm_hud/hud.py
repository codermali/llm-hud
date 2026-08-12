from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
BAR_WIDTH = 10


@dataclass(frozen=True)
class UsageWindow:
    label: str
    used: float | None = None
    resets_at: float | None = None


@dataclass(frozen=True)
class HudSnapshot:
    provider: str
    model: str | None = None
    cwd: str | None = None
    windows: tuple[UsageWindow, ...] = ()
    columns: int | None = None


def _style(text: str, code: str, enabled: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if enabled else text


def _sanitize(value: str) -> str:
    """Strip control characters from upstream-supplied display fields."""
    return CONTROL_RE.sub("", value)


def _char_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def _display_width(value: str) -> int:
    return sum(_char_width(char) for char in ANSI_RE.sub("", value))


def _truncate_left(value: str, width: int) -> str:
    """Keep the right end of value within width display columns."""
    if _display_width(value) <= width:
        return value
    kept: list[str] = []
    used = 1  # the ellipsis
    for char in reversed(value):
        char_width = _char_width(char)
        if used + char_width > width:
            break
        kept.append(char)
        used += char_width
    return "…" + "".join(reversed(kept))


def _compact_path(value: str | None) -> str | None:
    if not value:
        return None
    path = Path(value).expanduser()
    home = Path(os.environ.get("LLM_HUD_HOME", Path.home())).expanduser()
    try:
        relative = path.relative_to(home)
    except ValueError:
        return str(path)
    return "~" if not relative.parts else f"~/{relative}"


def _percent(value: float | None) -> str:
    if value is None:
        return "--"
    value = max(0.0, min(100.0, value))
    if value >= 99.5:
        return "100%"
    if value <= 0.5:
        return "0%"
    return f"{value:.0f}%"


def _used_color(value: float | None) -> str:
    if value is None:
        return "2"
    if value <= 40:
        return "32"
    if value <= 70:
        return "33"
    return "31"


def _reset_text(window: UsageWindow) -> str | None:
    if window.resets_at is None:
        return None
    try:
        reset = datetime.fromtimestamp(window.resets_at).astimezone()
    except (OSError, OverflowError, ValueError):
        return None
    return reset.strftime("%H:%M" if window.label == "5h" else "%a %H:%M")


def _window_segment(window: UsageWindow, color: bool) -> str:
    if window.used is None:
        filled = 0
    else:
        bounded = max(0.0, min(100.0, window.used))
        filled = max(0, min(BAR_WIDTH, int(bounded / 10 + 0.5)))
    tone = _used_color(window.used)
    bar = _style("█" * filled, tone, color) + _style("░" * (BAR_WIDTH - filled), "2", color)
    percent = _style(f"{_percent(window.used):>4}", tone, color)
    if window.used is not None:
        percent = f"{percent} {_style('used', '2', color)}"
    parts = [f"{window.label:<2}", bar, percent]
    reset = _reset_text(window)
    if reset:
        parts.append(_style(f"↻ {reset}", "2", color))
    return "  ".join(parts)


def render_hud(snapshot: HudSnapshot, color: bool = True) -> str:
    columns = snapshot.columns if snapshot.columns and snapshot.columns > 0 else 120
    provider = _sanitize(snapshot.provider)
    model = _sanitize(snapshot.model) if snapshot.model else None
    cwd = _compact_path(_sanitize(snapshot.cwd) if snapshot.cwd else None)
    if cwd:
        prefix = _display_width(provider)
        if model:
            prefix += _display_width(model) + 3
        available = columns - prefix - 3
        cwd = _truncate_left(cwd, available) if available >= 2 else None

    header_parts = [_style(provider, "1;38;5;208", color)]
    if model:
        header_parts.append(model)
    if cwd:
        header_parts.append(_style(cwd, "2", color))
    header = " · ".join(header_parts)

    if not snapshot.windows:
        return header

    segments = [_window_segment(window, color) for window in snapshot.windows]
    row = "    ".join(segments)
    if _display_width(row) <= columns:
        return f"{header}\n{row}"
    return "\n".join([header, *segments])
