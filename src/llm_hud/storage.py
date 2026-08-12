from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any


class StateFileError(ValueError):
    """An installation state file is present but unsafe to use."""


def fsync_directory(path: Path) -> None:
    """Persist a rename when the platform supports directory fsync."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # Some filesystems do not implement directory fsync. The file itself
        # was already synced before rename, so retain the portable fallback.
        pass
    finally:
        os.close(descriptor)


def resolve_file_target(path: Path, *, follow_symlinks: bool = True) -> Path:
    """Resolve an atomic-write target while enforcing its symlink policy."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        try:
            if not follow_symlinks:
                return path.parent.resolve(strict=True) / path.name
            return path.resolve(strict=False)
        except (OSError, ValueError, RuntimeError) as error:
            raise OSError(f"cannot resolve file target {path}: {error}") from error
    except OSError as error:
        raise OSError(f"cannot inspect file target {path}: {error}") from error

    if not follow_symlinks:
        if stat.S_ISLNK(metadata.st_mode):
            raise OSError(f"refusing to use symlink as managed state: {path}")
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"file target is not a regular file: {path}")
        try:
            # Never resolve the final path component in no-follow mode. If it
            # is swapped for a symlink after lstat(), os.replace() will replace
            # that link instead of following it to an external target.
            return path.parent.resolve(strict=True) / path.name
        except (OSError, ValueError, RuntimeError) as error:
            raise OSError(f"cannot resolve file target {path}: {error}") from error

    if stat.S_ISLNK(metadata.st_mode):
        try:
            target = path.resolve(strict=True)
            target_metadata = target.stat()
        except (OSError, ValueError, RuntimeError) as error:
            raise OSError(f"cannot resolve symlink target {path}: {error}") from error
        if not stat.S_ISREG(target_metadata.st_mode):
            raise OSError(f"symlink target is not a regular file: {path} -> {target}")
        return target

    if not stat.S_ISREG(metadata.st_mode):
        raise OSError(f"file target is not a regular file: {path}")
    try:
        return path.resolve(strict=True)
    except (OSError, ValueError, RuntimeError) as error:
        raise OSError(f"cannot resolve file target {path}: {error}") from error


def read_provider_state(
    path: Path, *, supported_schemas: frozenset[int]
) -> dict[str, Any] | None:
    """Read provider state without treating corruption as an empty state."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise StateFileError(f"cannot inspect installation state {path}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise StateFileError(f"refusing symlink installation state: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise StateFileError(f"installation state is not a regular file: {path}")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise StateFileError(
                    f"installation state is not a regular file: {path}"
                )
            payload = json.load(handle)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise StateFileError(f"invalid installation state {path}: {error}") from error
    except OSError as error:
        raise StateFileError(f"cannot read installation state {path}: {error}") from error

    if not isinstance(payload, dict):
        raise StateFileError(f"installation state must be a JSON object: {path}")
    schema = payload.get("schema")
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise StateFileError(f"installation state has no valid schema: {path}")
    if schema not in supported_schemas:
        newest = max(supported_schemas)
        if schema > newest:
            detail = f"schema {schema} is newer than supported schema {newest}"
        else:
            detail = f"schema {schema} is not supported"
        raise StateFileError(f"installation state {path} {detail}")
    return payload


def validate_state_path(state: dict[str, Any], key: str, current: Path) -> Path:
    """Reject state belonging to a different provider configuration file."""
    saved = state.get(key)
    if not isinstance(saved, str) or not saved:
        raise StateFileError(f"installation state has no valid {key}")
    try:
        saved_path = Path(saved).expanduser().resolve(strict=False)
        current_path = resolve_file_target(current.expanduser())
    except (OSError, ValueError, RuntimeError) as error:
        raise StateFileError(f"cannot resolve installation state {key}: {error}") from error
    if saved_path != current_path:
        raise StateFileError(
            f"installation state targets {saved_path}, not current path {current_path}"
        )
    return current_path


def atomic_write_text(
    path: Path,
    content: str,
    mode: int | None = None,
    *,
    follow_symlinks: bool = True,
    expected_target: Path | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    target = resolve_file_target(path, follow_symlinks=follow_symlinks)
    if expected_target is not None:
        try:
            expected = expected_target.resolve(strict=False)
        except (OSError, ValueError, RuntimeError) as error:
            raise OSError(f"cannot resolve expected file target: {error}") from error
        if target != expected:
            raise OSError(f"file target changed from {expected} to {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    existing_mode: int | None = None
    try:
        target_metadata = target.lstat()
        if not follow_symlinks and stat.S_ISLNK(target_metadata.st_mode):
            raise OSError(f"refusing to use symlink as managed state: {path}")
        existing_mode = stat.S_IMODE(target_metadata.st_mode)
    except OSError:
        if target.exists() or target.is_symlink():
            raise

    target_mode = (
        mode
        if mode is not None
        else existing_mode
        if existing_mode is not None
        else 0o600
    )
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            os.fchmod(handle.fileno(), target_mode)
            handle.flush()
            os.fsync(handle.fileno())
        if not follow_symlinks:
            try:
                final_metadata = target.lstat()
            except FileNotFoundError:
                final_metadata = None
            if final_metadata is not None and stat.S_ISLNK(final_metadata.st_mode):
                raise OSError(f"refusing to replace symlink managed state: {path}")
            if final_metadata is not None and not stat.S_ISREG(final_metadata.st_mode):
                raise OSError(f"managed state target is not a regular file: {path}")
        os.replace(temp_path, target)
        fsync_directory(target.parent)
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass


def atomic_write_json(
    path: Path,
    payload: Any,
    mode: int | None = 0o600,
    *,
    follow_symlinks: bool = True,
    expected_target: Path | None = None,
) -> None:
    """Write JSON atomically; mode=None keeps the file's existing permissions."""
    atomic_write_text(
        path,
        json.dumps(payload, indent=2) + "\n",
        mode=mode,
        follow_symlinks=follow_symlinks,
        expected_target=expected_target,
    )


def atomic_write_provider_state(path: Path, payload: Any) -> None:
    """Write application-owned state without following a final symlink."""
    atomic_write_json(path, payload, follow_symlinks=False)


def restore_provider_state(path: Path, payload: dict[str, Any] | None) -> None:
    """Roll back a state write after its matching configuration write failed."""
    if payload is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write_provider_state(path, payload)
