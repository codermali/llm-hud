from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from pathlib import Path
from types import TracebackType
from typing import Any

from llm_hud._platform import (
    set_descriptor_mode,
    try_lock_descriptor,
    unlock_descriptor,
)


class StateFileError(ValueError):
    """An installation state file is present but unsafe to use."""


class ContentChangedError(OSError):
    """A file no longer contains the content an atomic update was based on."""


_EXPECTED_CONTENT_UNSET = object()


class ProviderLock:
    """Serialize llm-hud state/config transactions for one provider.

    The lock file lives beside the provider state.  Keeping this lock separate
    from the provider configuration is important: editors commonly replace
    configuration files, which would silently discard a lock held on the old
    inode. External editors do not participate in this lock.
    """

    def __init__(self, state_path: Path, timeout: float = 10.0) -> None:
        self.path = state_path.with_suffix(".lock")
        self.timeout = timeout
        self._descriptor: int | None = None

    def __enter__(self) -> ProviderLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as error:
            raise OSError(f"cannot open provider lock {self.path}: {error}") from error
        try:
            descriptor_metadata = os.fstat(descriptor)
            path_metadata = self.path.lstat()
        except OSError as error:
            os.close(descriptor)
            raise OSError(f"cannot verify provider lock {self.path}: {error}") from error
        if (
            not stat.S_ISREG(descriptor_metadata.st_mode)
            or stat.S_ISLNK(path_metadata.st_mode)
            or (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
            != (path_metadata.st_dev, path_metadata.st_ino)
        ):
            os.close(descriptor)
            raise OSError(
                f"provider lock is not a regular managed file: {self.path}"
            )
        try:
            set_descriptor_mode(descriptor, 0o600)
        except OSError as error:
            os.close(descriptor)
            raise OSError(
                f"cannot secure provider lock {self.path}: {error}"
            ) from error
        deadline = time.monotonic() + max(0.0, self.timeout)
        while True:
            try:
                try_lock_descriptor(descriptor)
                self._descriptor = descriptor
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(descriptor)
                    raise OSError(
                        f"another provider operation is in progress: {self.path}"
                    ) from None
                time.sleep(0.05)
            except OSError as error:
                os.close(descriptor)
                raise OSError(
                    f"cannot lock provider operations {self.path}: {error}"
                ) from error

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        if self._descriptor is not None:
            try:
                unlock_descriptor(self._descriptor)
            finally:
                os.close(self._descriptor)
                self._descriptor = None


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

    # O_NONBLOCK keeps a FIFO swapped in after the lstat from blocking the
    # open; the fstat identity check below then rejects it. Reads from a
    # regular file are unaffected.
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or (
                opened.st_dev,
                opened.st_ino,
            ) != (metadata.st_dev, metadata.st_ino):
                raise StateFileError(
                    f"installation state changed while reading: {path}"
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


JOURNAL_SCHEMAS = frozenset((1,))


def read_provider_journal(path: Path) -> dict[str, Any] | None:
    """Read a pending provider transaction journal; None when absent."""
    journal = read_provider_state(path, supported_schemas=JOURNAL_SCHEMAS)
    if journal is None:
        return None
    if journal.get("op") not in ("install", "uninstall"):
        raise StateFileError(f"installation journal has an invalid op: {path}")
    for key in ("previous_state", "pending_state"):
        value = journal.get(key)
        if value is not None and not isinstance(value, dict):
            raise StateFileError(f"installation journal has an invalid {key}: {path}")
    return journal


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


def read_text_snapshot(path: Path) -> tuple[str, bytes | None]:
    """Read UTF-8 text and retain the bytes for a late pre-commit check.

    ``None`` distinguishes an absent file from an existing empty file.  The
    caller should pass the byte snapshot back to ``atomic_write_*`` so changes
    observed before the final replacement are rejected. This is not a portable
    compare-and-swap: an external writer can still race the interval between
    the final check and ``os.replace()``.
    """
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return "", None
    return content.decode("utf-8"), content


def validate_text_snapshot(
    path: Path,
    *,
    expected_target: Path,
    expected_content: bytes | None,
    follow_symlinks: bool = True,
) -> None:
    """Reject a target or content change observable at the time of this check."""
    try:
        expected = expected_target.resolve(strict=False)
    except (OSError, ValueError, RuntimeError) as error:
        raise OSError(f"cannot resolve expected file target: {error}") from error
    target = resolve_file_target(path, follow_symlinks=follow_symlinks)
    if target != expected:
        raise ContentChangedError(
            f"configuration target changed while updating {path}; "
            "left it untouched; retry the command"
        )
    try:
        current_content = target.read_bytes()
    except FileNotFoundError:
        current_content = None
    if current_content != expected_content:
        raise ContentChangedError(
            f"configuration changed while updating {path}; "
            "left it untouched; retry the command"
        )


def atomic_write_text(
    path: Path,
    content: str,
    mode: int | None = None,
    *,
    follow_symlinks: bool = True,
    expected_target: Path | None = None,
    expected_content: bytes | None | object = _EXPECTED_CONTENT_UNSET,
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
        # ``content`` already carries the caller's chosen newline style.  In
        # particular, the TOML editor preserves CRLF input; allowing Windows
        # text mode to translate it again would turn CRLF into CRCRLF and make
        # the next parse fail.
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            set_descriptor_mode(handle.fileno(), target_mode)
            handle.flush()
            os.fsync(handle.fileno())
        if expected_content is not _EXPECTED_CONTENT_UNSET:
            if expected_content is not None and not isinstance(
                expected_content, bytes
            ):
                raise TypeError("expected_content must be bytes or None")
            # Resolve again as late as practical. This detects a symlink
            # retarget or content replacement that happened after the provider
            # took its snapshot but before this check. An external writer can
            # still race this check and the final os.replace() below.
            validate_text_snapshot(
                path,
                expected_target=target,
                expected_content=expected_content,
                follow_symlinks=follow_symlinks,
            )
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
    expected_content: bytes | None | object = _EXPECTED_CONTENT_UNSET,
) -> None:
    """Write JSON atomically; mode=None keeps the file's existing permissions."""
    atomic_write_text(
        path,
        json.dumps(payload, indent=2) + "\n",
        mode=mode,
        follow_symlinks=follow_symlinks,
        expected_target=expected_target,
        expected_content=expected_content,
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
