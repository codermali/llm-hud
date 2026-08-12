"""Line-oriented TOML editing that preserves unrelated content.

Table headers and key assignments are only recognized on lines that start
outside strings and outside an open bracketed value, so text that merely looks
like TOML syntax inside a multiline string or array is left alone. Every edit is
re-parsed and compared against the whole original document, so an edit that
changes anything beyond the requested key fails instead of reaching disk.

Tables may exist either as a bracketed header ([tui]) or as root-level dotted
assignments (tui.status_line = [...]); edits keep whichever style the document
already uses.

Limitation: quoted names (["a.b"] headers or "tui".status_line assignments) are
not recognized; such a table is treated as absent, which surfaces as a
duplicate-table parse error rather than a silent write to the wrong place.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

from llm_hud import _tomllib as tomllib

TABLE_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")
ARRAY_TABLE_RE = re.compile(r"^\s*\[\[([^\]]+)\]\]\s*(?:#.*)?$")


@dataclass(frozen=True)
class Assignment:
    start: int
    end: int
    indent: str
    dotted: bool = False


def _statement_starts(lines: list[str]) -> list[bool]:
    """Flag lines that begin a statement rather than continue a value."""
    starts: list[bool] = []
    quote: str | None = None
    depth = 0
    for line in lines:
        starts.append(quote is None and depth == 0)
        index = 0
        while index < len(line):
            char = line[index]
            if quote is None:
                if char == "#":
                    break
                if line.startswith('"""', index) or line.startswith("'''", index):
                    quote = line[index : index + 3]
                    index += 3
                    continue
                if char in ('"', "'"):
                    quote = char
                elif char == "[":
                    depth += 1
                elif char == "]":
                    depth = max(0, depth - 1)
                index += 1
                continue
            if len(quote) == 3:
                if quote == '"""' and char == "\\":
                    index += 2
                    continue
                if line.startswith(quote, index):
                    quote = None
                    index += 3
                    continue
                index += 1
                continue
            if quote == '"' and char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
        if quote is not None and len(quote) == 1:
            quote = None
    return starts


def _table_name(line: str) -> str | None:
    stripped = line.rstrip("\r\n")
    if ARRAY_TABLE_RE.match(stripped):
        return None
    match = TABLE_RE.match(stripped)
    return match.group(1).strip() if match else None


def _is_table_header(line: str) -> bool:
    stripped = line.rstrip("\r\n")
    return bool(TABLE_RE.match(stripped) or ARRAY_TABLE_RE.match(stripped))


def _table_bounds(lines: list[str], starts: list[bool], table: str) -> tuple[int, int] | None:
    start: int | None = None
    for index, line in enumerate(lines):
        if not starts[index]:
            continue
        if start is None:
            if _table_name(line) == table:
                start = index
            continue
        if _is_table_header(line):
            return start, index
    if start is None:
        return None
    return start, len(lines)


def _find_assignment(
    lines: list[str], starts: list[bool], table: str, key: str
) -> Assignment | None:
    bounds = _table_bounds(lines, starts, table)
    if not bounds:
        return None
    start, end = bounds
    pattern = re.compile(rf"^(\s*){re.escape(key)}\s*=")
    for index in range(start + 1, end):
        if not starts[index]:
            continue
        match = pattern.match(lines[index])
        if not match:
            continue
        assignment_end = index + 1
        while assignment_end < end and not starts[assignment_end]:
            assignment_end += 1
        return Assignment(index, assignment_end, match.group(1))
    return None


def _root_dotted_assignment(
    lines: list[str], starts: list[bool], table: str, key: str
) -> Assignment | None:
    """Find a root-level dotted assignment such as `table.key = value`.

    Only statements before the first table header qualify: the same text under
    a [section] header would belong to that section, not to `table`.
    """
    pattern = re.compile(
        rf"^(\s*){re.escape(table)}\s*\.\s*{re.escape(key)}\s*="
    )
    for index, line in enumerate(lines):
        if not starts[index]:
            continue
        if _is_table_header(line):
            return None
        match = pattern.match(line)
        if not match:
            continue
        end = index + 1
        while end < len(lines) and not starts[end]:
            end += 1
        return Assignment(index, end, match.group(1), dotted=True)
    return None


def _root_dotted_table_end(
    lines: list[str], starts: list[bool], table: str
) -> int | None:
    """Index just past the last root-level `table.* = ...` assignment."""
    bare_key = r"[A-Za-z0-9_-]+"
    dotted_tail = rf"{bare_key}(?:\s*\.\s*{bare_key})*"
    pattern = re.compile(
        rf"^\s*{re.escape(table)}\s*\.\s*{dotted_tail}\s*="
    )
    last_end: int | None = None
    for index, line in enumerate(lines):
        if not starts[index]:
            continue
        if _is_table_header(line):
            break
        if not pattern.match(line):
            continue
        end = index + 1
        while end < len(lines) and not starts[end]:
            end += 1
        last_end = end
    return last_end


def _newline(lines: list[str]) -> str:
    return "\r\n" if any(line.endswith("\r\n") for line in lines) else "\n"


def _format_array(key: str, values: list[str], indent: str = "", newline: str = "\n") -> str:
    encoded = ", ".join(json.dumps(value) for value in values)
    return f"{indent}{key} = [{encoded}]{newline}"


def _semantic_equal(left: object, right: object) -> bool:
    """Compare parsed TOML values while treating matching NaNs as equal."""
    if isinstance(left, float) and isinstance(right, float):
        if math.isnan(left) and math.isnan(right):
            return True
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _semantic_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _semantic_equal(left_value, right_value)
            for left_value, right_value in zip(left, right)
        )
    return left == right


def _verify(original: str, result: str, table: str, key: str, values: list[str] | None) -> None:
    """Fail unless `result` differs from `original` only in table.key."""
    expected = tomllib.loads(original)
    node = expected
    for part in table.split("."):
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    if values is None:
        node.pop(key, None)
    else:
        node[key] = values
    if not _semantic_equal(tomllib.loads(result), expected):
        action = "remove" if values is None else "set"
        raise ValueError(
            f"refusing to {action} {table}.{key}: the edit would change unrelated "
            "content; the file was left unchanged"
        )


def set_array(text: str, table: str, key: str, values: list[str]) -> str:
    lines = text.splitlines(keepends=True)
    starts = _statement_starts(lines)
    newline = _newline(lines)
    assignment = _find_assignment(lines, starts, table, key) or _root_dotted_assignment(
        lines, starts, table, key
    )
    if assignment:
        name = f"{table}.{key}" if assignment.dotted else key
        lines[assignment.start : assignment.end] = [
            _format_array(name, values, assignment.indent, newline)
        ]
    else:
        bounds = _table_bounds(lines, starts, table)
        dotted_end = (
            None if bounds else _root_dotted_table_end(lines, starts, table)
        )
        if bounds:
            start, end = bounds
            while end > start + 1 and not lines[end - 1].strip():
                end -= 1
            if end and not lines[end - 1].endswith(("\n", "\r")):
                lines[end - 1] += newline
            lines.insert(end, _format_array(key, values, newline=newline))
        elif dotted_end is not None:
            # The table exists only as root-level dotted assignments; adding a
            # [table] header would redefine it, so extend the dotted block.
            if dotted_end and not lines[dotted_end - 1].endswith(("\n", "\r")):
                lines[dotted_end - 1] += newline
            lines.insert(
                dotted_end, _format_array(f"{table}.{key}", values, newline=newline)
            )
        else:
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += newline
            if lines and lines[-1].strip():
                lines.append(newline)
            lines.extend([f"[{table}]{newline}", _format_array(key, values, newline=newline)])
    result = "".join(lines)
    _verify(text, result, table, key, values)
    return result


def remove_key(text: str, table: str, key: str) -> str:
    lines = text.splitlines(keepends=True)
    starts = _statement_starts(lines)
    assignment = _find_assignment(lines, starts, table, key) or _root_dotted_assignment(
        lines, starts, table, key
    )
    if not assignment:
        return text
    del lines[assignment.start : assignment.end]
    result = "".join(lines)
    _verify(text, result, table, key, None)
    return result
