from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
BAR_WIDTH = 10


@dataclass(frozen=True)
class UsageWindow:
    label: str
    remaining: float | None = None
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


def _remaining_color(value: float | None) -> str:
    if value is None:
        return "2"
    if value >= 60:
        return "32"
    if value >= 30:
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
    if window.remaining is None:
        filled = 0
    else:
        bounded = max(0.0, min(100.0, window.remaining))
        filled = max(0, min(BAR_WIDTH, int(bounded / 10 + 0.5)))
    tone = _remaining_color(window.remaining)
    bar = _style("█" * filled, tone, color) + _style("░" * (BAR_WIDTH - filled), "2", color)
    percent = _style(f"{_percent(window.remaining):>4}", tone, color)
    parts = [f"{window.label:<2}", bar, percent]
    reset = _reset_text(window)
    if reset:
        parts.append(_style(f"↻ {reset}", "2", color))
    return "  ".join(parts)


def _visible_length(value: str) -> int:
    return len(ANSI_RE.sub("", value))


def render_hud(snapshot: HudSnapshot, color: bool = True) -> str:
    header_parts = [_style(snapshot.provider, "1;38;5;208", color)]
    if snapshot.model:
        header_parts.append(snapshot.model)
    cwd = _compact_path(snapshot.cwd)
    if cwd:
        header_parts.append(_style(cwd, "2", color))
    header = " · ".join(header_parts)

    if not snapshot.windows:
        return header

    segments = [_window_segment(window, color) for window in snapshot.windows]
    row = "    ".join(segments)
    columns = snapshot.columns if snapshot.columns and snapshot.columns > 0 else 120
    if _visible_length(row) <= columns:
        return f"{header}\n{row}"
    return "\n".join([header, *segments])
