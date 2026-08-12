#!/usr/bin/env python3
"""Verify the Python wheel and sdist contain compatibility and license files."""

from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path
from typing import TypeVar


METADATA_FIELDS = frozenset(
    (
        "License-Expression: MIT",
        "License-File: LICENSE",
        "License-File: src/llm_hud/_vendor/tomli/LICENSE",
    )
)
WHEEL_PACKAGE_FILES = frozenset(
    (
        "llm_hud/_tomllib.py",
        "llm_hud/_vendor/__init__.py",
        "llm_hud/_vendor/tomli/__init__.py",
        "llm_hud/_vendor/tomli/_parser.py",
        "llm_hud/_vendor/tomli/_re.py",
        "llm_hud/_vendor/tomli/_types.py",
        "llm_hud/_vendor/tomli/LICENSE",
    )
)
SDIST_PACKAGE_FILES = frozenset(
    (
        "LICENSE",
        "src/llm_hud/_tomllib.py",
        "src/llm_hud/_vendor/__init__.py",
        "src/llm_hud/_vendor/tomli/__init__.py",
        "src/llm_hud/_vendor/tomli/_parser.py",
        "src/llm_hud/_vendor/tomli/_re.py",
        "src/llm_hud/_vendor/tomli/_types.py",
        "src/llm_hud/_vendor/tomli/LICENSE",
    )
)
Value = TypeVar("Value")


def _one(values: list[Value], description: str) -> Value:
    if len(values) != 1:
        raise ValueError(f"expected exactly one {description}, found {len(values)}")
    return values[0]


def _require_files(names: set[str], required: frozenset[str], description: str) -> None:
    missing = sorted(required - names)
    if missing:
        raise ValueError(f"{description} is missing: {', '.join(missing)}")


def _require_metadata(content: str, description: str) -> None:
    fields = frozenset(content.splitlines())
    missing = sorted(METADATA_FIELDS - fields)
    if missing:
        raise ValueError(f"{description} is missing: {', '.join(missing)}")


def _verify_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        _require_files(names, WHEEL_PACKAGE_FILES, "wheel")
        metadata = _one(
            [name for name in names if name.endswith(".dist-info/METADATA")],
            "wheel metadata file",
        )
        _one(
            [name for name in names if name.endswith(".dist-info/licenses/LICENSE")],
            "project license in the wheel",
        )
        _one(
            [
                name
                for name in names
                if name.endswith(
                    ".dist-info/licenses/src/llm_hud/_vendor/tomli/LICENSE"
                )
            ],
            "vendored Tomli license in wheel metadata",
        )
        _require_metadata(archive.read(metadata).decode("utf-8"), "wheel metadata")


def _verify_sdist(path: Path) -> None:
    with tarfile.open(path, "r:gz") as archive:
        archived_names = set(archive.getnames())
        roots = {name.split("/", 1)[0] for name in archived_names if name}
        root = _one([Path(name) for name in roots], "sdist root directory").as_posix()
        prefix = f"{root}/"
        names = {
            name[len(prefix) :]
            for name in archived_names
            if name.startswith(prefix)
        }
        _require_files(names, SDIST_PACKAGE_FILES, "sdist")
        member = archive.getmember(f"{root}/PKG-INFO")
        handle = archive.extractfile(member)
        if handle is None:
            raise ValueError("cannot read sdist PKG-INFO")
        _require_metadata(handle.read().decode("utf-8"), "sdist metadata")


def main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        print("usage: verify_distribution.py DIST_DIR", file=sys.stderr)
        return 2
    dist = Path(arguments[0])
    try:
        wheel = _one(sorted(dist.glob("*.whl")), "wheel")
        sdist = _one(sorted(dist.glob("*.tar.gz")), "sdist")
        _verify_wheel(wheel)
        _verify_sdist(sdist)
    except (OSError, UnicodeError, ValueError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(f"distribution verification failed: {error}", file=sys.stderr)
        return 1
    print(f"verified {wheel.name} and {sdist.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
