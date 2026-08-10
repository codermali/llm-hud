from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass


TABLE_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")


@dataclass(frozen=True)
class Assignment:
    start: int
    end: int
    indent: str


def _table_name(line: str) -> str | None:
    match = TABLE_RE.match(line.rstrip("\r\n"))
    return match.group(1).strip() if match else None


def _table_bounds(lines: list[str], table: str) -> tuple[int, int] | None:
    start: int | None = None
    for index, line in enumerate(lines):
        name = _table_name(line)
        if start is None and name == table:
            start = index
            continue
        if start is not None and name is not None:
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
            _, end = bounds
            lines.insert(end, _format_array(key, values))
        else:
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += "\n"
            if lines and lines[-1].strip():
                lines.append("\n")
            lines.extend([f"[{table}]\n", _format_array(key, values)])
    result = "".join(lines)
    tomllib.loads(result)
    return result


def remove_key(text: str, table: str, key: str) -> str:
    lines = text.splitlines(keepends=True)
    assignment = _find_assignment(lines, table, key)
    if not assignment:
        return text
    del lines[assignment.start : assignment.end]
    result = "".join(lines)
    tomllib.loads(result)
    return result
