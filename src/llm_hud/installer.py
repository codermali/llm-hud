from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path

from llm_hud.runtime import (
    RUNTIME_CONTENT,
    Activation,
    RuntimeLayoutError,
    RuntimeMetadata,
    embedded_runtime_version,
    initialize_layout,
    install_staged_runtime,
    read_activation,
    source_digest,
)


STAGING_PREFIX = ".llm-hud-stage-"


def _metadata(path: Path, description: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as error:
        raise RuntimeLayoutError(f"cannot inspect {description} {path}: {error}") from error


def _validate_python_cache(path: Path) -> None:
    metadata = _metadata(path, "Python cache")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeLayoutError(f"Python cache is not a regular directory: {path}")
    try:
        entries = list(os.scandir(path))
    except OSError as error:
        raise RuntimeLayoutError(f"cannot inspect Python cache {path}: {error}") from error
    for entry in entries:
        entry_path = Path(entry.path)
        entry_metadata = _metadata(entry_path, "Python cache entry")
        if (
            not stat.S_ISREG(entry_metadata.st_mode)
            or entry_metadata.st_nlink != 1
            or entry_path.suffix not in (".pyc", ".pyo")
        ):
            raise RuntimeLayoutError(
                f"Python cache contains an unsafe non-bytecode entry: {entry_path}"
            )


def _copy_regular_file(source: Path, destination: Path) -> None:
    before = _metadata(source, "runtime source file")
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
    ):
        raise RuntimeLayoutError(
            f"runtime source file is not a single-link regular file: {source}"
        )
    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    destination_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    source_descriptor = -1
    destination_descriptor = -1
    try:
        source_descriptor = os.open(source, source_flags)
        opened = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise RuntimeLayoutError(f"runtime source changed while opening: {source}")
        executable = bool(opened.st_mode & 0o111)
        destination_descriptor = os.open(destination, destination_flags, 0o600)
        os.fchmod(destination_descriptor, 0o700 if executable else 0o600)
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            remaining = memoryview(chunk)
            while remaining:
                written = os.write(destination_descriptor, remaining)
                if written <= 0:
                    raise OSError("short write while staging runtime")
                remaining = remaining[written:]
    except OSError as error:
        raise RuntimeLayoutError(
            f"cannot copy runtime source file {source}: {error}"
        ) from error
    finally:
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        if source_descriptor >= 0:
            os.close(source_descriptor)


def _copy_directory(source: Path, destination: Path) -> None:
    metadata = _metadata(source, "runtime source directory")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeLayoutError(f"runtime source is not a regular directory: {source}")
    try:
        destination.mkdir(mode=0o700)
        entries = sorted(os.scandir(source), key=lambda entry: entry.name)
    except OSError as error:
        raise RuntimeLayoutError(
            f"cannot copy runtime source directory {source}: {error}"
        ) from error
    for entry in entries:
        source_path = Path(entry.path)
        destination_path = destination / entry.name
        entry_metadata = _metadata(source_path, "runtime source entry")
        if stat.S_ISLNK(entry_metadata.st_mode):
            raise RuntimeLayoutError(f"runtime source entry is a symlink: {source_path}")
        if stat.S_ISDIR(entry_metadata.st_mode):
            if entry.name == "__pycache__":
                _validate_python_cache(source_path)
                continue
            _copy_directory(source_path, destination_path)
        elif stat.S_ISREG(entry_metadata.st_mode):
            if source_path.suffix in (".pyc", ".pyo"):
                raise RuntimeLayoutError(
                    f"runtime source contains bytecode outside __pycache__: {source_path}"
                )
            _copy_regular_file(source_path, destination_path)
        else:
            raise RuntimeLayoutError(f"runtime source entry is not regular: {source_path}")


def copy_runtime_source(source: Path, staging: Path) -> None:
    source = Path(source)
    staging = Path(staging)
    for name in RUNTIME_CONTENT:
        source_path = source / name
        destination_path = staging / name
        metadata = _metadata(source_path, "runtime source")
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeLayoutError(f"runtime source path is a symlink: {source_path}")
        if stat.S_ISDIR(metadata.st_mode):
            _copy_directory(source_path, destination_path)
        elif stat.S_ISREG(metadata.st_mode):
            _copy_regular_file(source_path, destination_path)
        else:
            raise RuntimeLayoutError(f"runtime source path is not regular: {source_path}")


def _remove_staging(
    root: Path,
    staging: Path,
    *,
    root_identity: tuple[int, int],
    staging_identity: tuple[int, int],
) -> None:
    try:
        root_metadata = root.lstat()
        metadata = staging.lstat()
    except FileNotFoundError:
        return
    except OSError:
        return
    if (
        staging.parent != root
        or not staging.name.startswith(STAGING_PREFIX)
        or stat.S_ISLNK(root_metadata.st_mode)
        or not stat.S_ISDIR(root_metadata.st_mode)
        or (root_metadata.st_dev, root_metadata.st_ino) != root_identity
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino) != staging_identity
    ):
        return
    try:
        shutil.rmtree(staging)
    except OSError:
        pass


def install_runtime_from_source(
    source: Path, root: Path
) -> tuple[RuntimeMetadata, Activation]:
    source = Path(source)
    root = Path(root)
    source_metadata = _metadata(source, "runtime source root")
    if stat.S_ISLNK(source_metadata.st_mode) or not stat.S_ISDIR(
        source_metadata.st_mode
    ):
        raise RuntimeLayoutError(f"runtime source root is not a directory: {source}")

    expected_content_sha256 = source_digest(source)
    version = embedded_runtime_version(source)
    initialize_layout(root)
    try:
        root = root.resolve(strict=True)
        root_metadata = root.lstat()
    except (OSError, ValueError, RuntimeError) as error:
        raise RuntimeLayoutError(f"cannot resolve install root {root}: {error}") from error
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise RuntimeLayoutError(f"install root is not a regular directory: {root}")
    root_identity = (root_metadata.st_dev, root_metadata.st_ino)
    current = read_activation(root)
    expected_active = current.active if current is not None else None
    try:
        staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=root))
    except OSError as error:
        raise RuntimeLayoutError(f"cannot create runtime staging directory: {error}") from error
    staging_metadata = _metadata(staging, "runtime staging directory")
    if stat.S_ISLNK(staging_metadata.st_mode) or not stat.S_ISDIR(
        staging_metadata.st_mode
    ):
        raise RuntimeLayoutError(f"runtime staging path is not a directory: {staging}")
    staging_identity = (staging_metadata.st_dev, staging_metadata.st_ino)
    try:
        copy_runtime_source(source, staging)
        return install_staged_runtime(
            root,
            staging,
            version,
            expected_content_sha256=expected_content_sha256,
            expected_active=expected_active,
        )
    finally:
        _remove_staging(
            root,
            staging,
            root_identity=root_identity,
            staging_identity=staging_identity,
        )
