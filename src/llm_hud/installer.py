from __future__ import annotations

import argparse
from collections.abc import Callable
import hashlib
import json
import os
import secrets
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
import sys
from dataclasses import dataclass
from pathlib import Path

from llm_hud._platform import set_descriptor_mode
from llm_hud.runtime import (
    INSTALL_MARKER_NAME,
    INSTALL_MARKER_VALUE,
    RUNTIME_CONTENT,
    LAUNCHER_STATE_NAME,
    STABLE_STATE_NAME,
    VERSIONS_DIR_NAME,
    Activation,
    RuntimeLayoutError,
    RuntimeLock,
    RuntimeMetadata,
    _activate_with_replaced_unlocked,
    _clear_activation_unlocked,
    _restore_activation_unlocked,
    embedded_runtime_version,
    finalize_runtime,
    initialize_layout,
    read_activation,
    source_digest,
    validate_release_id,
    validate_runtime,
    validate_staged_runtime,
)
from llm_hud.storage import atomic_write_text, fsync_directory


STAGING_PREFIX = ".llm-hud-stage-"
# Staging directories younger than this may belong to a live concurrent
# install; older ones were orphaned by a crash and are safe to remove.
STAGING_MAX_AGE_SECONDS = 3600.0
RUNTIME_TRASH_PREFIX = ".llm-hud-trash-"
RUNTIME_TRASH_RECORD_SUFFIX = ".record"
RUNTIME_TRASH_PAYLOAD_SUFFIX = ".runtime"
RUNTIME_TRASH_RECORD_VALUE = "llm-hud-runtime-trash-v1"
RUNTIME_TRASH_TOKEN_BYTES = 16
LAUNCHER_STATE_SCHEMA = 1
DISPATCHER_SOURCE = Path("scripts") / "llm-hud-dispatcher"
CONTROL_SOURCE = Path("scripts") / "runtime_control.py"
CONTROL_DESTINATION = Path("control") / "runtime_control.py"
DISPATCHER_DESTINATION = Path("bin") / "llm-hud"
MANAGED_LAUNCHER_MARKER = "# llm-hud-managed-launcher-v1"
MAX_LAUNCHER_SIZE = 64 * 1024
MAX_STABLE_TOOL_SIZE = 64 * 1024
LAUNCHER_TEMP_PREFIX = ".llm-hud-launcher-"


@dataclass(frozen=True)
class StableTools:
    dispatcher: str
    control: str
    dispatcher_sha256: str
    control_sha256: str


@dataclass(frozen=True)
class _FileSnapshot:
    content: bytes | None
    identity: tuple[int, int] | None
    mode: int | None


@dataclass(frozen=True)
class _StableToolsSnapshot:
    control: _FileSnapshot
    dispatcher: _FileSnapshot
    stable_state: _FileSnapshot


def _metadata(path: Path, description: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as error:
        raise RuntimeLayoutError(f"cannot inspect {description} {path}: {error}") from error


def _dangerous_install_roots() -> set[Path]:
    candidates = [Path("/")]
    home_value = os.environ.get("HOME")
    if home_value:
        home = Path(home_value)
        candidates.extend(
            (home, home / ".local", home / ".local/share", home / ".local/bin")
        )
    resolved: set[Path] = set()
    for candidate in candidates:
        try:
            resolved.add(candidate.resolve(strict=False))
        except (OSError, ValueError, RuntimeError):
            continue
    return resolved


def _remove_failed_install_marker(
    root: Path,
    marker: Path,
    identity: tuple[int, int],
) -> None:
    """Remove an incomplete marker only while it is still the file we created."""
    try:
        metadata = marker.lstat()
    except OSError:
        return
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino) != identity
    ):
        return
    try:
        marker.unlink()
    except OSError:
        return
    fsync_directory(root)


def claim_install_root(root: Path) -> None:
    root = _validate_launcher_path(Path(root), "install root")
    metadata = _metadata(root, "install root")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeLayoutError(f"install root is not a regular directory: {root}")
    try:
        canonical_root = root.resolve(strict=True)
    except (OSError, ValueError, RuntimeError) as error:
        raise RuntimeLayoutError(f"cannot resolve install root {root}: {error}") from error
    canonical_root = _validate_launcher_path(canonical_root, "canonical install root")
    if canonical_root in _dangerous_install_roots():
        raise RuntimeLayoutError(f"refusing unsafe install root: {canonical_root}")
    root = canonical_root
    marker = root / INSTALL_MARKER_NAME
    payload = f"{INSTALL_MARKER_VALUE}\n".encode("utf-8")
    try:
        marker.lstat()
    except FileNotFoundError:
        content = None
    except OSError as error:
        raise RuntimeLayoutError(f"cannot inspect install marker {marker}: {error}") from error
    else:
        content = _read_regular_bytes(marker, "install marker")
    if content is not None:
        if content != payload:
            raise RuntimeLayoutError(f"unrecognized install marker: {marker}")
        return
    try:
        entries = list(os.scandir(root))
    except OSError as error:
        raise RuntimeLayoutError(f"cannot inspect install root {root}: {error}") from error
    if entries:
        raise RuntimeLayoutError(f"refusing non-empty unmanaged install root: {root}")
    # O_EXCL guarantees exactly one concurrent claimer creates the marker;
    # losers accept the root when the winner's valid marker landed.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(marker, flags, 0o600)
    except FileExistsError:
        if _read_regular_bytes(marker, "concurrent install marker") != payload:
            raise RuntimeLayoutError(f"unrecognized install marker: {marker}")
        return
    except OSError as error:
        raise RuntimeLayoutError(
            f"cannot create install marker {marker}: {error}"
        ) from error
    identity: tuple[int, int] | None = None
    failure: OSError | None = None
    try:
        opened = os.fstat(descriptor)
        identity = (opened.st_dev, opened.st_ino)
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("write returned no data")
            remaining = remaining[written:]
        os.fsync(descriptor)
    except OSError as error:
        failure = error
    try:
        os.close(descriptor)
    except OSError as error:
        if failure is None:
            failure = error
    if failure is not None:
        if identity is not None:
            _remove_failed_install_marker(root, marker, identity)
        raise RuntimeLayoutError(
            f"cannot write install marker {marker}: {failure}"
        ) from failure
    fsync_directory(root)


def _read_regular_bytes(path: Path, description: str) -> bytes:
    before = _metadata(path, description)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise RuntimeLayoutError(f"{description} is not a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as error:
        raise RuntimeLayoutError(f"cannot read {description} {path}: {error}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _validate_launcher_path(path: Path, description: str) -> Path:
    value = str(path)
    if not path.is_absolute() or any(character in value for character in ("\0", "\r", "\n")):
        raise RuntimeLayoutError(f"{description} must be an absolute single-line path")
    return path


def _canonical_file_path(path: Path, description: str) -> Path:
    path = _validate_launcher_path(path, description)
    try:
        canonical = path.parent.resolve(strict=False) / path.name
    except (OSError, ValueError, RuntimeError) as error:
        raise RuntimeLayoutError(f"cannot resolve {description} {path}: {error}") from error
    return _validate_launcher_path(canonical, f"canonical {description}")


def managed_launcher_content(python: Path, command: Path) -> str:
    python = _validate_launcher_path(Path(python), "Python interpreter")
    command = _validate_launcher_path(Path(command), "launcher command")
    return (
        "#!/bin/sh\n"
        f"{MANAGED_LAUNCHER_MARKER}\n"
        f"exec {shlex.quote(str(python))} -I -B "
        f"{shlex.quote(str(command))} \"$@\"\n"
    )


def _known_launcher(content: bytes, command: Path) -> bool:
    try:
        text = content.decode("utf-8")
    except UnicodeError:
        return False
    lines = text.splitlines(keepends=True)
    if (
        len(lines) != 3
        or lines[0] != "#!/bin/sh\n"
        or lines[1] != f"{MANAGED_LAUNCHER_MARKER}\n"
    ):
        return False
    try:
        arguments = shlex.split(lines[2], posix=True)
    except ValueError:
        return False
    if (
        len(arguments) != 6
        or arguments[0] != "exec"
        or arguments[2:4] != ["-I", "-B"]
        or arguments[5] != "$@"
    ):
        return False
    python = Path(arguments[1])
    if not python.is_absolute() or Path(arguments[4]) != command:
        return False
    return text == managed_launcher_content(python, command)


def _snapshot_regular_file(
    path: Path,
    description: str,
    *,
    maximum_size: int | None = None,
) -> _FileSnapshot:
    """Read a regular file and retain the identity needed for a later CAS."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return _FileSnapshot(content=None, identity=None, mode=None)
    except OSError as error:
        raise RuntimeLayoutError(f"cannot inspect {description} {path}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeLayoutError(f"{description} is not a regular file: {path}")
    if maximum_size is not None and metadata.st_size > maximum_size:
        raise RuntimeLayoutError(f"{description} is too large: {path}")

    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        identity = (opened.st_dev, opened.st_ino)
        if not stat.S_ISREG(opened.st_mode) or identity != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise RuntimeLayoutError(f"{description} changed while reading: {path}")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if maximum_size is not None and size > maximum_size:
                raise RuntimeLayoutError(f"{description} is too large: {path}")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            (after.st_dev, after.st_ino) != identity
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
        ):
            raise RuntimeLayoutError(f"{description} changed while reading: {path}")
        return _FileSnapshot(
            content=b"".join(chunks),
            identity=identity,
            mode=stat.S_IMODE(opened.st_mode),
        )
    except OSError as error:
        raise RuntimeLayoutError(f"cannot read {description} {path}: {error}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_launcher_content(path: Path) -> bytes | None:
    """Read an existing external launcher; None when absent."""
    return _snapshot_regular_file(
        path,
        "external launcher",
        maximum_size=MAX_LAUNCHER_SIZE,
    ).content


def _preflight_external_launcher(root: Path, launcher: Path) -> None:
    """Only absent launchers and our own managed form may be replaced."""
    root = root.resolve(strict=True)
    launcher = _canonical_file_path(launcher, "external launcher")
    if launcher.is_relative_to(root):
        raise RuntimeLayoutError("external launcher must be outside the install root")
    content = _read_launcher_content(launcher)
    if content is not None and not _known_launcher(
        content, root / DISPATCHER_DESTINATION
    ):
        raise RuntimeLayoutError(f"refusing to replace unmanaged launcher: {launcher}")


def _assert_unchanged_snapshot(
    path: Path,
    expected: _FileSnapshot,
    description: str,
    *,
    maximum_size: int | None = None,
) -> None:
    if _snapshot_regular_file(
        path,
        description,
        maximum_size=maximum_size,
    ) != expected:
        raise RuntimeLayoutError(f"{description} changed while updating: {path}")


def _atomic_replace_bytes(
    path: Path,
    content: bytes,
    *,
    mode: int,
    temp_prefix: str,
    expected: _FileSnapshot,
    description: str,
    maximum_size: int | None = None,
    installed_result: list[_FileSnapshot] | None = None,
) -> _FileSnapshot:
    """Replace a file only while its byte/identity snapshot is unchanged."""
    _require_regular_directory(path.parent, f"{description} directory")
    descriptor, temp_name = tempfile.mkstemp(prefix=temp_prefix, dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            set_descriptor_mode(handle.fileno(), mode)
            handle.flush()
            os.fsync(handle.fileno())
            temp_metadata = os.fstat(handle.fileno())
            installed = _FileSnapshot(
                content=content,
                identity=(temp_metadata.st_dev, temp_metadata.st_ino),
                mode=stat.S_IMODE(temp_metadata.st_mode),
            )
        _assert_unchanged_snapshot(
            path,
            expected,
            description,
            maximum_size=maximum_size,
        )
        if installed_result is not None:
            # Record the exact temp inode before replace. If replace reports an
            # ambiguous failure, rollback can still distinguish our file from
            # a concurrent occupant of the same path.
            installed_result.append(installed)
        try:
            os.replace(temp, path)
        except OSError as error:
            raise RuntimeLayoutError(f"cannot replace {description} {path}: {error}") from error
        observed = _snapshot_regular_file(
            path,
            description,
            maximum_size=maximum_size,
        )
        if observed != installed:
            raise RuntimeLayoutError(f"{description} changed after replacement: {path}")
        fsync_directory(path.parent)
        return installed
    finally:
        try:
            temp.unlink()
        except OSError:
            pass


def _write_launcher_file(
    launcher: Path,
    content: bytes,
    *,
    expected: _FileSnapshot | None = None,
    mode: int = 0o755,
    installed_result: list[_FileSnapshot] | None = None,
) -> _FileSnapshot:
    if expected is None:
        expected = _snapshot_regular_file(
            launcher,
            "external launcher",
            maximum_size=MAX_LAUNCHER_SIZE,
        )
    return _atomic_replace_bytes(
        launcher,
        content,
        mode=mode,
        temp_prefix=LAUNCHER_TEMP_PREFIX,
        expected=expected,
        description="external launcher",
        maximum_size=MAX_LAUNCHER_SIZE,
        installed_result=installed_result,
    )


def _decode_stable_source(path: Path, description: str) -> str:
    try:
        return _read_regular_bytes(path, description).decode("utf-8")
    except UnicodeError as error:
        raise RuntimeLayoutError(f"{description} is not UTF-8: {path}") from error


def _validate_stable_tools(tools: StableTools) -> StableTools:
    for description, text, declared_sha256 in (
        ("stable dispatcher", tools.dispatcher, tools.dispatcher_sha256),
        ("stable runtime control", tools.control, tools.control_sha256),
    ):
        if not isinstance(text, str):
            raise RuntimeLayoutError(f"invalid {description}: expected text")
        encoded = text.encode("utf-8")
        if len(encoded) > MAX_STABLE_TOOL_SIZE:
            raise RuntimeLayoutError(f"invalid {description}: exceeds size limit")
        actual_sha256 = _sha256(encoded)
        if declared_sha256 != actual_sha256:
            raise RuntimeLayoutError(f"invalid {description}: SHA256 mismatch")
        try:
            compile(text, description, "exec", dont_inherit=True)
        except (SyntaxError, ValueError) as error:
            raise RuntimeLayoutError(f"invalid {description}: {error}") from error
    return tools


def _stable_tool_matches(
    snapshot: _FileSnapshot,
    expected_sha256: str,
    expected_mode: int,
) -> bool:
    return (
        snapshot.content is not None
        and _sha256(snapshot.content) == expected_sha256
        and (os.name == "nt" or snapshot.mode == expected_mode)
    )


def load_stable_tools(source: Path) -> StableTools:
    dispatcher = _decode_stable_source(
        source / DISPATCHER_SOURCE, "stable dispatcher source"
    )
    control = _decode_stable_source(
        source / CONTROL_SOURCE, "stable runtime control source"
    )
    return _validate_stable_tools(
        StableTools(
            dispatcher=dispatcher,
            control=control,
            dispatcher_sha256=_sha256(dispatcher.encode("utf-8")),
            control_sha256=_sha256(control.encode("utf-8")),
        )
    )


def _require_regular_directory(path: Path, description: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir(mode=0o700)
        except OSError as error:
            raise RuntimeLayoutError(f"cannot create {description} {path}: {error}") from error
        return
    except OSError as error:
        raise RuntimeLayoutError(f"cannot inspect {description} {path}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeLayoutError(f"{description} is not a regular directory: {path}")


def _sweep_tool_temps(directory: Path, name: str) -> None:
    """Remove write temporaries orphaned by an interrupted install."""
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    for entry in entries:
        if entry.name.startswith(f".{name}."):
            try:
                Path(entry.path).unlink()
            except OSError:
                pass


def _snapshot_stable_tools(root: Path) -> _StableToolsSnapshot:
    return _StableToolsSnapshot(
        control=_snapshot_regular_file(
            root / CONTROL_DESTINATION,
            "installed stable runtime control",
        ),
        dispatcher=_snapshot_regular_file(
            root / DISPATCHER_DESTINATION,
            "installed stable dispatcher",
        ),
        stable_state=_snapshot_regular_file(
            root / STABLE_STATE_NAME,
            "legacy stable state",
        ),
    )


def _install_stable_tools_unlocked(
    root: Path,
    tools: StableTools,
) -> tuple[_StableToolsSnapshot, _StableToolsSnapshot]:
    _require_regular_directory(root / "bin", "stable bin directory")
    _require_regular_directory(root / "control", "stable control directory")
    before = _snapshot_stable_tools(root)
    control_path = root / CONTROL_DESTINATION
    dispatcher_path = root / DISPATCHER_DESTINATION
    stable_state_path = root / STABLE_STATE_NAME
    control_installation: list[_FileSnapshot] = []
    dispatcher_installation: list[_FileSnapshot] = []
    stable_state_removed = False
    after = before
    try:
        _sweep_tool_temps(control_path.parent, CONTROL_DESTINATION.name)
        _sweep_tool_temps(dispatcher_path.parent, DISPATCHER_DESTINATION.name)
        if not _stable_tool_matches(before.control, tools.control_sha256, 0o600):
            _atomic_replace_bytes(
                control_path,
                tools.control.encode("utf-8"),
                mode=0o600,
                temp_prefix=f".{CONTROL_DESTINATION.name}.",
                expected=before.control,
                description="installed stable runtime control",
                installed_result=control_installation,
            )
        if not _stable_tool_matches(
            before.dispatcher,
            tools.dispatcher_sha256,
            0o700,
        ):
            _atomic_replace_bytes(
                dispatcher_path,
                tools.dispatcher.encode("utf-8"),
                mode=0o700,
                temp_prefix=f".{DISPATCHER_DESTINATION.name}.",
                expected=before.dispatcher,
                description="installed stable dispatcher",
                installed_result=dispatcher_installation,
            )
        # 0.1.x recorded the frozen protocol hashes here; the tools are now
        # simply refreshed with every install.
        if before.stable_state.content is not None:
            _assert_unchanged_snapshot(
                stable_state_path,
                before.stable_state,
                "legacy stable state",
            )
            stable_state_path.unlink()
            stable_state_removed = True
            fsync_directory(root)
        after = _StableToolsSnapshot(
            control=(
                control_installation[-1]
                if control_installation
                else before.control
            ),
            dispatcher=(
                dispatcher_installation[-1]
                if dispatcher_installation
                else before.dispatcher
            ),
            stable_state=_FileSnapshot(None, None, None),
        )
    except Exception as failure:
        partial = _StableToolsSnapshot(
            control=(
                control_installation[-1]
                if control_installation
                else before.control
            ),
            dispatcher=(
                dispatcher_installation[-1]
                if dispatcher_installation
                else before.dispatcher
            ),
            stable_state=(
                _FileSnapshot(None, None, None)
                if stable_state_removed
                else before.stable_state
            ),
        )
        try:
            _restore_stable_tools_unlocked(root, before, partial)
        except RuntimeLayoutError as rollback_error:
            raise RuntimeLayoutError(
                f"cannot install stable runtime tools ({failure}); rollback incomplete: "
                f"{rollback_error}"
            ) from failure
        if isinstance(failure, OSError):
            raise RuntimeLayoutError(
                f"cannot install stable runtime tools: {failure}"
            ) from failure
        raise
    return before, after


def install_stable_tools(
    source_or_tools: Path | StableTools,
    root: Path,
    *,
    expected_active: str,
) -> None:
    root = Path(root).resolve(strict=True)
    tools = (
        _validate_stable_tools(source_or_tools)
        if isinstance(source_or_tools, StableTools)
        else load_stable_tools(Path(source_or_tools))
    )
    with RuntimeLock(root):
        current = read_activation(root)
        actual_active = current.active if current is not None else None
        if actual_active != expected_active:
            raise RuntimeLayoutError(
                f"active runtime changed from {expected_active!r} to {actual_active!r}"
            )
        _install_stable_tools_unlocked(root, tools)


def _restore_file_snapshot(
    path: Path,
    previous: _FileSnapshot,
    installed: _FileSnapshot,
    description: str,
    *,
    maximum_size: int | None = None,
) -> None:
    """Restore our replacement, but never overwrite a later path occupant."""
    current = _snapshot_regular_file(
        path,
        description,
        maximum_size=maximum_size,
    )
    if current == previous:
        return
    if current != installed:
        raise RuntimeLayoutError(
            f"{description} changed before rollback; left it untouched: {path}"
        )
    if previous.content is None:
        _assert_unchanged_snapshot(
            path,
            installed,
            description,
            maximum_size=maximum_size,
        )
        try:
            path.unlink()
            fsync_directory(path.parent)
        except OSError as error:
            raise RuntimeLayoutError(
                f"cannot remove new {description} {path}: {error}"
            ) from error
        return
    if previous.mode is None:
        raise RuntimeLayoutError(f"invalid previous {description} snapshot: {path}")
    _atomic_replace_bytes(
        path,
        previous.content,
        mode=previous.mode,
        temp_prefix=f".{path.name}.rollback-",
        expected=installed,
        description=description,
        maximum_size=maximum_size,
    )


def _restore_stable_tools_unlocked(
    root: Path,
    previous: _StableToolsSnapshot,
    installed: _StableToolsSnapshot,
) -> None:
    rollback_errors: list[str] = []
    for path, before, after, description in (
        (
            root / STABLE_STATE_NAME,
            previous.stable_state,
            installed.stable_state,
            "legacy stable state",
        ),
        (
            root / DISPATCHER_DESTINATION,
            previous.dispatcher,
            installed.dispatcher,
            "installed stable dispatcher",
        ),
        (
            root / CONTROL_DESTINATION,
            previous.control,
            installed.control,
            "installed stable runtime control",
        ),
    ):
        try:
            _restore_file_snapshot(
                path,
                before,
                after,
                description,
            )
        except (OSError, RuntimeLayoutError) as error:
            rollback_errors.append(str(error))
    if rollback_errors:
        raise RuntimeLayoutError(
            "stable tools rollback incomplete: " + "; ".join(rollback_errors)
        )


def _write_launcher_state(
    path: Path,
    content: bytes,
    *,
    expected: _FileSnapshot,
    installed_result: list[_FileSnapshot],
) -> _FileSnapshot:
    return _atomic_replace_bytes(
        path,
        content,
        mode=0o600,
        temp_prefix=f".{path.name}.",
        expected=expected,
        description="launcher state",
        installed_result=installed_result,
    )


def _install_external_launcher_unlocked(
    root: Path,
    launcher: Path,
    python: Path,
    *,
    expected_active: str,
) -> None:
    """Install launcher and pointer while the caller holds ``RuntimeLock``."""
    candidate = managed_launcher_content(
        python, root / DISPATCHER_DESTINATION
    ).encode("utf-8")
    current = read_activation(root)
    actual_active = current.active if current is not None else None
    if actual_active != expected_active:
        raise RuntimeLayoutError(
            f"active runtime changed from {expected_active!r} to {actual_active!r}"
        )
    _preflight_external_launcher(root, launcher)
    launcher_before = _snapshot_regular_file(
        launcher,
        "external launcher",
        maximum_size=MAX_LAUNCHER_SIZE,
    )
    if launcher_before.content is not None and not _known_launcher(
        launcher_before.content,
        root / DISPATCHER_DESTINATION,
    ):
        raise RuntimeLayoutError(f"refusing to replace unmanaged launcher: {launcher}")
    state_path = root / LAUNCHER_STATE_NAME
    state_before = _snapshot_regular_file(state_path, "launcher state")
    state_payload = {
        "schema": LAUNCHER_STATE_SCHEMA,
        "launcher_path": str(launcher),
    }
    state_candidate = (json.dumps(state_payload, indent=2) + "\n").encode("utf-8")
    launcher_installation: list[_FileSnapshot] = []
    state_installation: list[_FileSnapshot] = []
    try:
        if launcher_before.content != candidate:
            _write_launcher_file(
                launcher,
                candidate,
                expected=launcher_before,
                installed_result=launcher_installation,
            )
        # A plain pointer so a managed dispatch can find the PATH-visible
        # launcher (cli._managed_launcher_path); 0.1.x files carried more
        # fields, which readers ignore.
        _write_launcher_state(
            state_path,
            state_candidate,
            expected=state_before,
            installed_result=state_installation,
        )
    except Exception as failure:
        rollback_errors: list[str] = []
        if state_installation:
            try:
                _restore_file_snapshot(
                    state_path,
                    state_before,
                    state_installation[-1],
                    "launcher state",
                )
            except RuntimeLayoutError as error:
                rollback_errors.append(str(error))
        if launcher_installation:
            try:
                _restore_file_snapshot(
                    launcher,
                    launcher_before,
                    launcher_installation[-1],
                    "external launcher",
                    maximum_size=MAX_LAUNCHER_SIZE,
                )
            except RuntimeLayoutError as error:
                rollback_errors.append(str(error))
        if rollback_errors:
            raise RuntimeLayoutError(
                f"cannot install external launcher ({failure}); rollback incomplete: "
                + "; ".join(rollback_errors)
            ) from failure
        if isinstance(failure, OSError):
            raise RuntimeLayoutError(
                f"cannot install external launcher: {failure}"
            ) from failure
        raise


def install_external_launcher(
    root: Path,
    launcher: Path,
    python: Path,
    *,
    expected_active: str,
) -> None:
    root = Path(root).resolve(strict=True)
    launcher = _canonical_file_path(Path(launcher), "external launcher")
    python = _validate_launcher_path(Path(python), "Python interpreter")
    _preflight_external_launcher(root, launcher)
    with RuntimeLock(root):
        _install_external_launcher_unlocked(
            root,
            launcher,
            python,
            expected_active=expected_active,
        )


def install_checkout_launcher(launcher: Path, python: Path, command: Path) -> None:
    launcher = _canonical_file_path(Path(launcher), "external launcher")
    python = _validate_launcher_path(Path(python), "Python interpreter")
    command = _validate_launcher_path(Path(command), "launcher command")
    try:
        checkout_root = command.parent.parent.resolve(strict=True)
    except (OSError, ValueError, RuntimeError) as error:
        raise RuntimeLayoutError(
            f"cannot resolve checkout root for {command}: {error}"
        ) from error
    if launcher.is_relative_to(checkout_root):
        raise RuntimeLayoutError("external launcher must be outside the source checkout")
    content = _read_launcher_content(launcher)
    if content is not None and not _known_launcher(content, command):
        raise RuntimeLayoutError(f"refusing to replace unmanaged launcher: {launcher}")
    _write_launcher_file(launcher, managed_launcher_content(python, command).encode("utf-8"))


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
            or entry_path.suffix not in (".pyc", ".pyo")
        ):
            raise RuntimeLayoutError(
                f"Python cache contains an unsafe non-bytecode entry: {entry_path}"
            )


def _copy_regular_file(source: Path, destination: Path) -> None:
    before = _metadata(source, "runtime source file")
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise RuntimeLayoutError(
            f"runtime source file is not a regular file: {source}"
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
        if not stat.S_ISREG(opened.st_mode):
            raise RuntimeLayoutError(f"runtime source is not a regular file: {source}")
        executable = bool(opened.st_mode & 0o111)
        destination_descriptor = os.open(destination, destination_flags, 0o600)
        set_descriptor_mode(destination_descriptor, 0o700 if executable else 0o600)
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


def _sweep_stale_staging(root: Path) -> None:
    """Remove staging directories orphaned by an interrupted install."""
    cutoff = time.time() - STAGING_MAX_AGE_SECONDS
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    changed = False
    for entry in entries:
        if not entry.name.startswith(STAGING_PREFIX):
            continue
        path = Path(entry.path)
        try:
            metadata = path.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            continue
        if metadata.st_mtime > cutoff:
            continue
        shutil.rmtree(path, ignore_errors=True)
        changed = True
    if changed:
        fsync_directory(root)


def _trash_paths(versions: Path, token: str) -> tuple[Path, Path]:
    stem = f"{RUNTIME_TRASH_PREFIX}{token}"
    return (
        versions / f"{stem}{RUNTIME_TRASH_RECORD_SUFFIX}",
        versions / f"{stem}{RUNTIME_TRASH_PAYLOAD_SUFFIX}",
    )


def _trash_token(name: str, suffix: str) -> str | None:
    if not name.startswith(RUNTIME_TRASH_PREFIX) or not name.endswith(suffix):
        return None
    token = name[len(RUNTIME_TRASH_PREFIX) : -len(suffix)]
    if len(token) != RUNTIME_TRASH_TOKEN_BYTES * 2:
        return None
    try:
        bytes.fromhex(token)
    except ValueError:
        return None
    return token


def _trash_record_payload(release_id: str, token: str) -> str:
    return f"{RUNTIME_TRASH_RECORD_VALUE} {token} {release_id}\n"


def _same_directory(path: Path, identity: tuple[int, int]) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        not stat.S_ISLNK(metadata.st_mode)
        and stat.S_ISDIR(metadata.st_mode)
        and (metadata.st_dev, metadata.st_ino) == identity
    )


def _managed_trash_payload(
    versions: Path, token: str
) -> tuple[Path | None, tuple[int, int] | None] | None:
    record, payload = _trash_paths(versions, token)
    try:
        raw = _read_regular_bytes(record, "runtime trash record").decode("utf-8")
    except (OSError, RuntimeLayoutError, UnicodeError):
        return None
    parts = raw.removesuffix("\n").split(" ")
    if (
        len(parts) != 3
        or parts[0] != RUNTIME_TRASH_RECORD_VALUE
        or parts[1] != token
        or raw != _trash_record_payload(parts[2], token)
    ):
        return None
    try:
        validate_release_id(parts[2])
        metadata = payload.lstat()
    except FileNotFoundError:
        return None, None
    except (OSError, RuntimeLayoutError):
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        return None
    return payload, (metadata.st_dev, metadata.st_ino)


def _remove_managed_runtime_trash(versions: Path) -> None:
    """Best-effort removal of trash proven to have been created by this installer."""
    try:
        entries = list(os.scandir(versions))
    except OSError:
        return
    tokens = {
        token
        for entry in entries
        if (token := _trash_token(entry.name, RUNTIME_TRASH_RECORD_SUFFIX))
        is not None
    }
    changed = False
    for token in tokens:
        managed = _managed_trash_payload(versions, token)
        if managed is None:
            continue
        payload, payload_identity = managed
        record, _ = _trash_paths(versions, token)
        if payload is None or payload_identity is None:
            try:
                record.unlink()
            except OSError:
                pass
            else:
                changed = True
            continue
        if not _same_directory(payload, payload_identity):
            continue
        try:
            shutil.rmtree(payload)
        except OSError:
            try:
                payload.lstat()
            except FileNotFoundError:
                try:
                    record.unlink()
                except OSError:
                    pass
                changed = True
            except OSError:
                pass
            continue
        try:
            record.unlink()
        except OSError:
            # A missing record makes the empty trash name inert. Never delete
            # an unmarked directory merely because its name resembles ours.
            pass
        changed = True
    if changed:
        fsync_directory(versions)


def _move_runtime_to_trash(
    versions: Path,
    path: Path,
    release_id: str,
    *,
    expected_identity: tuple[int, int],
) -> None:
    """Atomically free a release ID before its best-effort recursive removal."""
    if not _same_directory(path, expected_identity):
        return
    for _ in range(8):
        token = secrets.token_hex(RUNTIME_TRASH_TOKEN_BYTES)
        record, payload = _trash_paths(versions, token)
        try:
            record.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            return
        else:
            continue
        try:
            payload.lstat()
        except FileNotFoundError:
            break
        except OSError:
            return
    else:
        return
    try:
        atomic_write_text(
            record,
            _trash_record_payload(release_id, token),
            mode=0o600,
            follow_symlinks=False,
        )
    except OSError:
        return
    try:
        os.rename(path, payload)
    except OSError:
        try:
            record.unlink()
        except OSError:
            pass
        return
    fsync_directory(versions)
    if not _same_directory(payload, expected_identity):
        # The release name changed identity after validation. Never recurse
        # into that replacement; make it inert by dropping our record.
        try:
            path.lstat()
        except FileNotFoundError:
            try:
                os.rename(payload, path)
            except OSError:
                pass
        except OSError:
            pass
        try:
            record.unlink()
        except OSError:
            pass
        fsync_directory(versions)
        return
    try:
        shutil.rmtree(payload)
    except OSError:
        return
    try:
        record.unlink()
    except OSError:
        pass
    fsync_directory(versions)


def _prune_inactive_runtimes(root: Path, *, lock_timeout: float = 10.0) -> None:
    """Move unreachable releases aside, then remove their trash best-effort."""
    with RuntimeLock(root, timeout=lock_timeout):
        activation = read_activation(root)
        if activation is None:
            return
        keep = {activation.active}
        if activation.previous is not None:
            keep.add(activation.previous)
        versions = root / VERSIONS_DIR_NAME
        _remove_managed_runtime_trash(versions)
        try:
            entries = list(os.scandir(versions))
        except OSError:
            return
        for entry in entries:
            if entry.name in keep:
                continue
            try:
                release_id = validate_release_id(entry.name)
            except RuntimeLayoutError:
                continue
            path = Path(entry.path)
            try:
                metadata = path.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                continue
            try:
                validate_runtime(root, release_id)
            except RuntimeLayoutError:
                continue
            _move_runtime_to_trash(
                versions,
                path,
                release_id,
                expected_identity=(metadata.st_dev, metadata.st_ino),
            )


def _smoke_test_runtime_candidate(
    staging: Path,
    python: Path,
    version: str,
) -> None:
    try:
        completed = subprocess.run(
            [
                str(python),
                "-I",
                "-B",
                str(staging / DISPATCHER_DESTINATION),
                "--version",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeLayoutError(f"runtime candidate smoke test failed: {error}") from error
    expected = f"llm-hud {version}"
    if completed.returncode != 0 or completed.stdout.strip() != expected:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
        raise RuntimeLayoutError(
            f"runtime candidate smoke test failed: expected {expected!r}; {detail}"
        )


def _validate_control_source(control: str) -> str:
    try:
        compile(control, "stable runtime control", "exec", dont_inherit=True)
    except (SyntaxError, ValueError) as error:
        raise RuntimeLayoutError(f"invalid stable runtime control: {error}") from error
    return control


def _default_control_source() -> str:
    source_root = Path(__file__).resolve().parents[2]
    return _validate_control_source(
        _decode_stable_source(
            source_root / CONTROL_SOURCE,
            "stable runtime control source",
        )
    )


def _preflight_runtime_release(
    root: Path,
    release_id: str,
    python: Path,
    version: str,
    control: str,
) -> None:
    control = _validate_control_source(control)
    try:
        completed = subprocess.run(
            [
                str(python),
                "-I",
                "-B",
                "-c",
                control,
                str(root),
                "--preflight-release",
                release_id,
                "--version",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeLayoutError(f"runtime dispatch preflight failed: {error}") from error
    expected = f"llm-hud {version}"
    if completed.returncode != 0 or completed.stdout.strip() != expected:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
        raise RuntimeLayoutError(
            f"runtime dispatch preflight failed: expected {expected!r}; {detail}"
        )


def _install_runtime_from_source(
    source: Path,
    root: Path,
    python: Path | None = None,
    *,
    stable_control: str | None = None,
    after_activation: Callable[[Path, RuntimeMetadata], None] | None = None,
) -> tuple[RuntimeMetadata, Activation, Activation | None]:
    source = Path(source)
    root = Path(root)
    python = Path(sys.executable) if python is None else Path(python)
    if stable_control is None:
        stable_control = _default_control_source()
    else:
        stable_control = _validate_control_source(stable_control)
    source_metadata = _metadata(source, "runtime source root")
    if stat.S_ISLNK(source_metadata.st_mode) or not stat.S_ISDIR(
        source_metadata.st_mode
    ):
        raise RuntimeLayoutError(f"runtime source root is not a directory: {source}")

    expected_content_sha256 = source_digest(source)
    version = embedded_runtime_version(source)
    initialize_layout(root)
    _sweep_stale_staging(root)
    try:
        with RuntimeLock(root):
            _remove_managed_runtime_trash(root / VERSIONS_DIR_NAME)
    except RuntimeLayoutError:
        pass  # trash collection is best effort and must not block an install
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
        validate_staged_runtime(
            staging,
            version,
            expected_content_sha256=expected_content_sha256,
        )
        _smoke_test_runtime_candidate(staging, python, version)
        metadata = finalize_runtime(
            root,
            staging,
            version,
            expected_content_sha256=expected_content_sha256,
        )
        _preflight_runtime_release(
            root,
            metadata.release_id,
            python,
            metadata.version,
            stable_control,
        )
        with RuntimeLock(root):
            activation, replaced_activation = _activate_with_replaced_unlocked(
                root,
                metadata.release_id,
                expected_active=expected_active,
            )
            if replaced_activation is None and expected_active is not None:
                # A same-release reinstall is a no-op activation. Preserve its
                # exact record so a later failure does not clear a valid install.
                replaced_activation = activation
            try:
                if after_activation is not None:
                    after_activation(root, metadata)
            except Exception as error:
                _restore_after_install_failure_unlocked(
                    root,
                    replaced_activation,
                    metadata.release_id,
                    error,
                )
                raise
        return metadata, activation, replaced_activation
    finally:
        _remove_staging(
            root,
            staging,
            root_identity=root_identity,
            staging_identity=staging_identity,
        )


def install_runtime_from_source(
    source: Path,
    root: Path,
    python: Path | None = None,
    *,
    stable_control: str | None = None,
) -> tuple[RuntimeMetadata, Activation]:
    metadata, activation, _ = _install_runtime_from_source(
        source,
        root,
        python,
        stable_control=stable_control,
    )
    return metadata, activation


def _install_versioned_runtime(
    source: Path,
    root: Path,
    python: Path | None = None,
    *,
    after_stable_tools: Callable[[Path, RuntimeMetadata], None] | None = None,
) -> tuple[RuntimeMetadata, Activation, Activation | None]:
    source = Path(source)
    root = Path(root)
    tools = load_stable_tools(source)

    def finish_install(resolved_root: Path, metadata: RuntimeMetadata) -> None:
        previous_tools, installed_tools = _install_stable_tools_unlocked(
            resolved_root,
            tools,
        )
        try:
            if after_stable_tools is not None:
                after_stable_tools(resolved_root, metadata)
        except Exception as failure:
            try:
                _restore_stable_tools_unlocked(
                    resolved_root,
                    previous_tools,
                    installed_tools,
                )
            except RuntimeLayoutError as rollback_error:
                raise RuntimeLayoutError(
                    f"installation failed ({failure}); restoring the stable tools "
                    f"also failed: {rollback_error}"
                ) from failure
            raise

    return _install_runtime_from_source(
        source,
        root,
        python,
        stable_control=tools.control,
        after_activation=finish_install,
    )


def install_versioned_runtime(
    source: Path,
    root: Path,
    python: Path | None = None,
) -> tuple[RuntimeMetadata, Activation]:
    metadata, activation, _ = _install_versioned_runtime(source, root, python)
    return metadata, activation


def _restore_after_install_failure_unlocked(
    root: Path,
    replaced_activation: Activation | None,
    expected_active: str,
    failure: Exception,
) -> None:
    if replaced_activation is None:
        try:
            _clear_activation_unlocked(root, expected_active=expected_active)
        except RuntimeLayoutError as clear_error:
            raise RuntimeLayoutError(
                f"installation failed ({failure}); clearing the new activation "
                f"also failed: {clear_error}"
            ) from failure
        return
    try:
        _restore_activation_unlocked(
            root,
            replaced_activation,
            expected_active=expected_active,
        )
    except RuntimeLayoutError as restore_error:
        raise RuntimeLayoutError(
            f"installation failed ({failure}); restoring the previous runtime also "
            f"failed: {restore_error}"
        ) from failure


def _smoke_test_dispatcher(root: Path, python: Path, version: str) -> None:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        completed = subprocess.run(
            [
                str(python),
                "-I",
                "-B",
                str(root / DISPATCHER_DESTINATION),
                "--version",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeLayoutError(f"stable dispatcher smoke test failed: {error}") from error
    expected = f"llm-hud {version}"
    if completed.returncode != 0 or completed.stdout.strip() != expected:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
        raise RuntimeLayoutError(
            f"stable dispatcher smoke test failed: expected {expected!r}; {detail}"
        )


def install_complete(
    source: Path,
    root: Path,
    launcher: Path,
    python: Path,
) -> tuple[RuntimeMetadata, Activation]:
    source = Path(source)
    root = Path(root)
    launcher = _canonical_file_path(Path(launcher), "external launcher")
    python = _validate_launcher_path(Path(python), "Python interpreter")
    _preflight_external_launcher(root, launcher)

    def finish_install(resolved_root: Path, metadata: RuntimeMetadata) -> None:
        _smoke_test_dispatcher(resolved_root, python, metadata.version)
        _install_external_launcher_unlocked(
            resolved_root,
            launcher,
            python,
            expected_active=metadata.release_id,
        )

    metadata, activation, _ = _install_versioned_runtime(
        source,
        root,
        python,
        after_stable_tools=finish_install,
    )
    root = root.resolve(strict=True)
    try:
        _prune_inactive_runtimes(root)
    except RuntimeLayoutError:
        pass  # garbage collection is best effort; the install succeeded
    return metadata, activation


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-hud-installer")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--checkout", action="store_true")
    parser.add_argument("--claim-root", action="store_true")
    return parser


def main(arguments: list[str] | None = None) -> int:
    args = build_parser().parse_args(arguments)
    try:
        if args.checkout:
            install_checkout_launcher(
                args.launcher,
                args.python,
                args.root / DISPATCHER_DESTINATION,
            )
            print("checkout")
            return 0
        if args.claim_root:
            claim_install_root(args.root)
        metadata, _ = install_complete(
            args.source,
            args.root,
            args.launcher,
            args.python,
        )
    except (OSError, RuntimeLayoutError) as error:
        print(f"llm-hud installer: {error}", file=sys.stderr)
        return 1
    print(metadata.release_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
