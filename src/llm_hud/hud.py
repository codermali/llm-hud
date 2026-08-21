from __future__ import annotations

import math
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath

from llm_hud.paths import home_dir


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
BAR_WIDTH = 10
# Claude Code refreshes rate limits only when an API response arrives, so a
# window can sit unchanged for minutes.  Mark one stale once it has outlived
# several status-line ticks, which is long enough that the number on screen
# no longer describes the current session.
STALE_AFTER_SECONDS = 120.0
WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


@dataclass(frozen=True)
class UsageWindow:
    label: str
    used: float | None = None
    resets_at: float | None = None
    age_seconds: float | None = None


@dataclass(frozen=True)
class HudSnapshot:
    provider: str
    model: str | None = None
    effort: str | None = None
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
    path_value = os.path.expanduser(value)
    # Keep an explicit override in the caller's path syntax. Converting it to
    # the host's concrete Path first would turn `/home/...` into `\home\...`
    # on Windows before the pure-path comparison below.
    home_override = os.environ.get("LLM_HUD_HOME")
    home_value = (
        os.path.expanduser(home_override) if home_override else str(home_dir())
    )

    # cwd is upstream data, so its syntax need not match the host running the HUD
    # (tests, remote sessions, and WSL can all cross that boundary). Pure paths let
    # us interpret Windows paths consistently on every platform. Prefer an
    # explicitly POSIX cwd over a Windows-shaped local home so `/tmp` remains
    # `/tmp` when the portable test suite runs on Windows.
    if WINDOWS_DRIVE_RE.match(path_value) or path_value.startswith(("\\\\", "//")):
        path_type = PureWindowsPath
    elif path_value.startswith("/"):
        path_type = PurePosixPath
    elif WINDOWS_DRIVE_RE.match(home_value) or home_value.startswith(("\\\\", "//")):
        path_type = PureWindowsPath
    else:
        path_type = PurePosixPath

    path = path_type(path_value)
    home = path_type(home_value)
    try:
        relative = path.relative_to(home)
    except ValueError:
        return path.as_posix()
    return "~" if not relative.parts else f"~/{relative.as_posix()}"


def _percent_value(value: float | None) -> int | None:
    """Rounded percentage; color thresholds must follow the displayed number."""
    # NaN survives the min/max clamp and would crash int(); the render hot
    # path must degrade to missing data instead.
    if value is None or math.isnan(value):
        return None
    value = max(0.0, min(100.0, value))
    if value >= 99.5:
        return 100
    if value <= 0.5:
        return 0
    return int(f"{value:.0f}")


def _percent(value: float | None) -> str:
    rounded = _percent_value(value)
    return "--" if rounded is None else f"{rounded}%"


def _used_color(value: float | None) -> str:
    rounded = _percent_value(value)
    if rounded is None:
        return "2"
    if rounded <= 40:
        return "32"
    if rounded <= 70:
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


def _now() -> float:
    return time.time()


def _age_text(seconds: float) -> str:
    """A one-unit observation age; the marker only has to convey the order."""
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"·{max(1, minutes)}m"
    hours = minutes // 60
    return f"·{hours}h" if hours < 24 else f"·{hours // 24}d"


def _window_segment(
    window: UsageWindow,
    color: bool,
    now: float,
    show_age: bool = True,
    show_reset: bool = True,
    label_width: int = 2,
) -> str:
    used = window.used
    if used is not None and math.isnan(used):
        used = None
    # Past its reset time the window has rolled over, so the last observation
    # describes a window that no longer exists.  Report missing data rather
    # than a percentage that is certainly wrong.
    expired = window.resets_at is not None and now > window.resets_at
    if expired:
        used = None
    stale = (
        not expired
        and window.age_seconds is not None
        and window.age_seconds >= STALE_AFTER_SECONDS
    )
    if used is None:
        filled = 0
    else:
        bounded = max(0.0, min(100.0, used))
        filled = max(0, min(BAR_WIDTH, int(bounded / 10 + 0.5)))
    # A stale window drops to the dim tone so the usage colors only ever speak
    # for a current observation.
    tone = "2" if stale else _used_color(used)
    bar = _style("█" * filled, tone, color) + _style("░" * (BAR_WIDTH - filled), "2", color)
    percent = _style(f"{_percent(used):>4}", tone, color)
    # A stale window trades "used" for the age marker, so it occupies the same
    # width as the current one it replaces and going stale never reflows the
    # row.  Suppressing the marker then genuinely buys columns back.
    if used is not None and not stale:
        percent = f"{percent} {_style('used', '2', color)}"
    parts = [f"{window.label:<{label_width}}", bar, percent]
    if expired:
        if show_reset:
            parts.append(_style("↻ pending", "2", color))
        return "  ".join(parts)
    reset = _reset_text(window) if show_reset else None
    if reset:
        parts.append(_style(f"↻ {reset}", "2", color))
    if stale and show_age:
        parts.append(_style(_age_text(window.age_seconds), "2", color))
    return "  ".join(parts)


def render_hud(snapshot: HudSnapshot, color: bool = True) -> str:
    columns = snapshot.columns if snapshot.columns and snapshot.columns > 0 else 120
    provider = _sanitize(snapshot.provider)
    model = _sanitize(snapshot.model) if snapshot.model else None
    effort = _sanitize(snapshot.effort) if snapshot.effort else None
    # The effort qualifies the model rather than standing alone, so it joins
    # that field instead of earning its own separator.
    if model and effort:
        model = f"{model} {effort}"
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

    now = _now()
    width = max([2, *(_display_width(w.label) for w in snapshot.windows)])

    def segments(**options: bool) -> list[str]:
        return [
            _window_segment(window, color, now, label_width=width, **options)
            for window in snapshot.windows
        ]

    # Shed the least load-bearing field first, and only give every window its
    # own line once nothing else can go.  The reset time is supplementary,
    # while the age marker reports that the number beside it may already be
    # wrong, so the marker outlives the timestamp by one step.
    for options in (
        {},
        {"show_reset": False},
        {"show_reset": False, "show_age": False},
    ):
        row = "    ".join(segments(**options))
        if _display_width(row) <= columns:
            return f"{header}\n{row}"
    return "\n".join([header, *segments()])
