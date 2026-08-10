"""Line-oriented TOML editing that preserves unrelated content.

Limitation: quoted table names that contain "]" (e.g. ["a]b"]) are not
recognized. Every edit is re-parsed with tomllib and verified against the
requested change, so an unsupported construct fails loudly instead of
corrupting the file.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass


TABLE_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")
ARRAY_TABLE_RE = re.compile(r"^\s*\[\[([^\]]+)\]\]\s*(?:#.*)?$")


@dataclass(frozen=True)
class Assignment:
    start: int
    end: int
    indent: str


def _table_name(line: str) -> str | None:
    stripped = line.rstrip("\r\n")
    if ARRAY_TABLE_RE.match(stripped):
        return None
    match = TABLE_RE.match(stripped)
    return match.group(1).strip() if match else None


def _is_table_header(line: str) -> bool:
    stripped = line.rstrip("\r\n")
    return bool(TABLE_RE.match(stripped) or ARRAY_TABLE_RE.match(stripped))


def _table_bounds(lines: list[str], table: str) -> tuple[int, int] | None:
    start: int | None = None
    for index, line in enumerate(lines):
        if start is None:
            if _table_name(line) == table:
                start = index
            continue
        if _is_table_header(line):
            return start, index
    if start is None:
        return None
    return start, len(lines)


def _bracket_balance(text: str) -> int:
    balance = 0
    quote: str | None = None
    escaped = False
    in_comment = False
    for char in text:
        if in_comment:
            if char == "\n":
                in_comment = False
            continue
        if quote:
            if quote == '"' and escaped:
                escaped = False
                continue
            if quote == '"' and char == "\\":
                escaped = True
                continue
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
        elif char == "#":
            in_comment = True
        elif char == "[":
            balance += 1
        elif char == "]":
            balance -= 1
    return balance


def _find_assignment(lines: list[str], table: str, key: str) -> Assignment | None:
    bounds = _table_bounds(lines, table)
    if not bounds:
        return None
    start, end = bounds
    pattern = re.compile(rf"^(\s*){re.escape(key)}\s*=")
    for index in range(start + 1, end):
        match = pattern.match(lines[index])
        if not match:
            continue
        assignment_end = index + 1
        text = lines[index]
        if "[" in text:
            while _bracket_balance(text) > 0 and assignment_end < end:
                text += lines[assignment_end]
                assignment_end += 1
        return Assignment(index, assignment_end, match.group(1))
    return None


def _format_array(key: str, values: list[str], indent: str = "") -> str:
    encoded = ", ".join(json.dumps(value) for value in values)
    return f"{indent}{key} = [{encoded}]\n"


def _table_value(parsed: dict, table: str) -> object:
    node: object = parsed
    for part in table.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def set_array(text: str, table: str, key: str, values: list[str]) -> str:
    lines = text.splitlines(keepends=True)
    assignment = _find_assignment(lines, table, key)
    if assignment:
        lines[assignment.start : assignment.end] = [
            _format_array(key, values, assignment.indent)
        ]
    else:
        bounds = _table_bounds(lines, table)
        if bounds:
            start, end = bounds
            while end > start + 1 and not lines[end - 1].strip():
                end -= 1
            lines.insert(end, _format_array(key, values))
        else:
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += "\n"
            if lines and lines[-1].strip():
                lines.append("\n")
            lines.extend([f"[{table}]\n", _format_array(key, values)])
    result = "".join(lines)
    parsed_table = _table_value(tomllib.loads(result), table)
    if not isinstance(parsed_table, dict) or parsed_table.get(key) != values:
        raise ValueError(f"failed to set {table}.{key}; the file was left unchanged")
    return result


def remove_key(text: str, table: str, key: str) -> str:
    lines = text.splitlines(keepends=True)
    assignment = _find_assignment(lines, table, key)
    if not assignment:
        return text
    del lines[assignment.start : assignment.end]
    result = "".join(lines)
    parsed_table = _table_value(tomllib.loads(result), table)
    if isinstance(parsed_table, dict) and key in parsed_table:
        raise ValueError(f"failed to remove {table}.{key}; the file was left unchanged")
    return result
