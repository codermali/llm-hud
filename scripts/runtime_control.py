#!/usr/bin/env python3
"""Stable dispatch control for a versioned llm-hud install.

This module intentionally imports no llm_hud package code until dispatch has
validated the selected immutable runtime.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


INSTALL_MARKER = ".llm-hud-install-root"
INSTALL_MARKER_VALUE = "llm-hud-install-root-v1\n"
LAYOUT_MARKER = ".llm-hud-layout"
LAYOUT_MARKER_VALUE = "llm-hud-versioned-layout-v1\n"
ACTIVATION_FILE = "activation"
ACTIVATION_PREFIX = "llm-hud-activation-v1"
RUNTIME_MARKER = ".llm-hud-runtime.json"
VERSIONS_DIRECTORY = "versions"
EMPTY_ACTIVATION_SLOT = "-"
CONTROL_RELATIVE = Path("control") / "runtime_control.py"
RUNTIME_CONTENT = ("src", "bin", "README.md", "LICENSE", "pyproject.toml")
MAX_MANAGED_TEXT_SIZE = 64 * 1024
RELEASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
VERSION = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\Z"
)


class ControlError(ValueError):
    """The managed runtime layout is absent, damaged, or unsafe."""


@dataclass(frozen=True)
class Activation:
    active: str

    def __post_init__(self) -> None:
        validate_release_id(self.active)


def validate_release_id(value: object) -> str:
    if not isinstance(value, str) or not RELEASE_ID.fullmatch(value):
        raise ControlError(f"invalid release id: {value!r}")
    return value


def validate_version(value: object) -> str:
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise ControlError(f"invalid runtime version: {value!r}")
    return value


def _require_directory(path: Path, description: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ControlError(f"cannot inspect {description} {path}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ControlError(f"{description} is not a regular directory: {path}")


def _read_owned_text(path: Path, description: str) -> str:
    try:
        before = path.lstat()
    except OSError as error:
        raise ControlError(f"cannot inspect {description} {path}: {error}") from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ControlError(f"{description} is not a regular managed file: {path}")
    # O_NONBLOCK keeps a FIFO swapped in after the lstat from blocking the
    # open; the fstat identity check below then rejects it. Reads from a
    # regular file are unaffected.
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (before.st_dev, before.st_ino):
            raise ControlError(f"{description} changed while reading: {path}")
        if opened.st_size > MAX_MANAGED_TEXT_SIZE:
            raise ControlError(f"{description} is too large: {path}")
        chunks: list[bytes] = []
        remaining = MAX_MANAGED_TEXT_SIZE + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > MAX_MANAGED_TEXT_SIZE:
            raise ControlError(f"{description} is too large: {path}")
        return payload.decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise ControlError(f"cannot read {description} {path}: {error}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _require_install_ownership(root: Path) -> None:
    if not root.is_absolute():
        raise ControlError(f"install root must be absolute: {root}")
    _require_directory(root, "install root")
    marker = _read_owned_text(root / INSTALL_MARKER, "install marker")
    if marker != INSTALL_MARKER_VALUE:
        raise ControlError(f"install root is not owned by llm-hud: {root}")


def _require_layout(root: Path) -> None:
    _require_install_ownership(root)
    marker = _read_owned_text(root / LAYOUT_MARKER, "layout marker")
    if marker != LAYOUT_MARKER_VALUE:
        raise ControlError(f"missing or unrecognized runtime layout in {root}")
    _require_directory(root / VERSIONS_DIRECTORY, "versions directory")


def _require_stable_control(root: Path) -> None:
    _require_directory(root / "control", "stable control directory")
    expected = root / CONTROL_RELATIVE
    actual = Path(os.path.abspath(__file__))
    if actual != expected:
        raise ControlError(f"stable runtime control is not installed at {expected}")
    try:
        metadata = actual.lstat()
    except OSError as error:
        raise ControlError(f"cannot inspect stable runtime control {actual}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ControlError(f"stable runtime control is not a regular managed file: {actual}")


def parse_activation(text: str) -> Activation:
    parts = text.removesuffix("\n").split(" ")
    if len(parts) != 3 or parts[0] != ACTIVATION_PREFIX:
        raise ControlError("invalid activation record")
    active = validate_release_id(parts[1])
    legacy_previous = parts[2]
    if legacy_previous != EMPTY_ACTIVATION_SLOT:
        validate_release_id(legacy_previous)
        if legacy_previous == active:
            raise ControlError("active and previous releases must differ")
    if text != f"{ACTIVATION_PREFIX} {active} {legacy_previous}\n":
        raise ControlError("activation record is not in canonical format")
    return Activation(active)


def _read_activation(root: Path) -> Activation:
    text = _read_owned_text(root / ACTIVATION_FILE, "activation record")
    return parse_activation(text)


def _read_content_file(path: Path) -> tuple[bytes, bool]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ControlError(f"runtime file is not a regular file: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), bool(metadata.st_mode & 0o111)
    except OSError as error:
        raise ControlError(f"cannot read runtime file {path}: {error}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _source_digest(root: Path) -> str:
    digest = hashlib.sha256()
    files: list[Path] = []
    for name in RUNTIME_CONTENT:
        candidate = root / name
        try:
            candidate_metadata = candidate.lstat()
        except OSError as error:
            raise ControlError(f"runtime source is missing: {candidate}") from error
        if stat.S_ISLNK(candidate_metadata.st_mode):
            raise ControlError(f"runtime source path is a symlink: {candidate}")
        if stat.S_ISDIR(candidate_metadata.st_mode):
            try:
                descendants = candidate.rglob("*")
                for path in descendants:
                    metadata = path.lstat()
                    relative = path.relative_to(root)
                    in_python_cache = "__pycache__" in relative.parts
                    if stat.S_ISLNK(metadata.st_mode):
                        raise ControlError(f"runtime source path is a symlink: {path}")
                    if stat.S_ISREG(metadata.st_mode):
                        is_bytecode = path.suffix in (".pyc", ".pyo")
                        if in_python_cache and is_bytecode:
                            continue
                        if is_bytecode:
                            raise ControlError(
                                f"runtime source contains bytecode outside __pycache__: {path}"
                            )
                        files.append(path)
                    elif not stat.S_ISDIR(metadata.st_mode):
                        raise ControlError(f"runtime source path is not regular: {path}")
            except OSError as error:
                raise ControlError(
                    f"cannot inspect runtime source {candidate}: {error}"
                ) from error
        elif stat.S_ISREG(candidate_metadata.st_mode):
            files.append(candidate)
        else:
            raise ControlError(f"runtime source path is not regular: {candidate}")
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        name = relative.as_posix().encode("utf-8")
        content, executable = _read_content_file(path)
        if os.name == "nt":
            # Windows has no portable POSIX execute bit. Keep the frozen
            # runtime digest ABI by treating its one canonical launcher as
            # executable, matching release archives created on POSIX.
            executable = relative.as_posix() == "bin/llm-hud"
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(b"\x01" if executable else b"\x00")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _embedded_version(root: Path) -> str:
    path = root / "src" / "llm_hud" / "_version.py"
    content, _ = _read_content_file(path)
    try:
        tree = ast.parse(content.decode("utf-8"), filename=str(path))
    except (UnicodeDecodeError, SyntaxError) as error:
        raise ControlError(f"cannot parse runtime version {path}: {error}") from error
    values: list[object] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "__version__":
            values.append(node.value.value if isinstance(node.value, ast.Constant) else None)
    if len(values) != 1:
        raise ControlError(f"runtime version assignment is missing or ambiguous: {path}")
    return validate_version(values[0])


def _runtime_path(root: Path, release_id: str) -> Path:
    return root / VERSIONS_DIRECTORY / validate_release_id(release_id)


def _validate_runtime(root: Path, release_id: str) -> Path:
    _require_layout(root)
    path = _runtime_path(root, release_id)
    _require_directory(path, "runtime")
    expected = set(RUNTIME_CONTENT) | {RUNTIME_MARKER}
    try:
        actual = {entry.name for entry in path.iterdir()}
    except OSError as error:
        raise ControlError(f"cannot inspect runtime contents {path}: {error}") from error
    if actual != expected:
        raise ControlError(f"runtime has an invalid top-level shape: {path}")

    marker = _read_owned_text(path / RUNTIME_MARKER, "runtime marker")
    try:
        payload = json.loads(marker)
    except json.JSONDecodeError as error:
        raise ControlError(f"invalid runtime marker {path}: {error}") from error
    expected_keys = {"schema", "release_id", "version", "content_sha256"}
    schema = payload.get("schema") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or set(payload) != expected_keys
        or isinstance(schema, bool)
        or schema != 1
    ):
        raise ControlError(f"unsupported runtime marker: {path}")
    marker_release = validate_release_id(payload.get("release_id"))
    version = validate_version(payload.get("version"))
    content_sha256 = payload.get("content_sha256")
    if not isinstance(content_sha256, str) or not SHA256.fullmatch(content_sha256):
        raise ControlError(f"invalid runtime digest in marker: {path}")
    if marker_release != release_id:
        raise ControlError(f"runtime marker release id does not match {path}")
    if release_id != f"{version}-{content_sha256[:12]}":
        raise ControlError(f"runtime marker fields do not match {path}")

    for relative in ("bin/llm-hud", "src/llm_hud/cli.py", "src/llm_hud/_version.py"):
        required = path / relative
        try:
            metadata = required.lstat()
        except OSError as error:
            raise ControlError(f"runtime file is missing: {required}") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ControlError(f"runtime file is not a regular managed file: {required}")
    launcher = path / "bin" / "llm-hud"
    if not os.access(launcher, os.X_OK):
        raise ControlError(f"runtime launcher is not executable: {launcher}")
    if _source_digest(path) != content_sha256:
        raise ControlError(f"runtime content digest does not match: {path}")
    if _embedded_version(path) != version:
        raise ControlError(f"runtime code version does not match marker: {path}")
    return path


def _dispatch_runtime(root: Path, runtime: Path, arguments: Sequence[str]) -> int:
    if any(name == "llm_hud" or name.startswith("llm_hud.") for name in sys.modules):
        raise ControlError("runtime package was loaded before active runtime validation")
    source = runtime / "src"
    sys.path.insert(0, str(source))
    previous_argv = sys.argv
    sys.argv = [str(root / "bin" / "llm-hud"), *arguments]
    try:
        cli = importlib.import_module("llm_hud.cli")
        main = getattr(cli, "main", None)
        if not callable(main):
            raise ControlError(f"active runtime has no callable CLI entry point: {runtime}")
        return int(main())
    finally:
        sys.argv = previous_argv


def dispatch(root: Path, arguments: Sequence[str]) -> int:
    root = Path(root)
    _require_layout(root)
    activation = _read_activation(root)
    runtime = _validate_runtime(root, activation.active)
    return _dispatch_runtime(root, runtime, arguments)


def preflight(root: Path, release_id: str, arguments: Sequence[str]) -> int:
    """Dispatch an immutable release without changing the activation record."""
    root = Path(root)
    _require_layout(root)
    runtime = _validate_runtime(root, release_id)
    return _dispatch_runtime(root, runtime, arguments)


def run(root: Path, arguments: Sequence[str]) -> int:
    root = Path(root)
    _require_stable_control(root)
    return dispatch(root, arguments)


def _main(arguments: Sequence[str]) -> int:
    if len(arguments) < 1:
        raise ControlError("usage: runtime_control.py INSTALL_ROOT [COMMAND ...]")
    if len(arguments) >= 3 and arguments[1] == "--preflight-release":
        return preflight(Path(arguments[0]), arguments[2], arguments[3:])
    return run(Path(arguments[0]), arguments[1:])


if __name__ == "__main__":
    try:
        raise SystemExit(_main(sys.argv[1:]))
    except ControlError as error:
        print(f"llm-hud: {error}", file=sys.stderr)
        raise SystemExit(1) from None
