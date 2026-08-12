from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
import sys
from dataclasses import dataclass
from pathlib import Path

from llm_hud.runtime import (
    ACTIVATION_NAME,
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
    activate,
    embedded_runtime_version,
    finalize_runtime,
    initialize_layout,
    read_activation,
    restore_activation,
    source_digest,
    validate_staged_runtime,
)
from llm_hud.storage import atomic_write_json, atomic_write_text, fsync_directory


STAGING_PREFIX = ".llm-hud-stage-"
# Staging directories younger than this may belong to a live concurrent
# install; older ones were orphaned by a crash and are safe to remove.
STAGING_MAX_AGE_SECONDS = 3600.0
STABLE_STATE_SCHEMA = 1
LAUNCHER_STATE_SCHEMA = 1
DISPATCHER_SOURCE = Path("scripts") / "llm-hud-dispatcher"
CONTROL_SOURCE = Path("scripts") / "runtime_control.py"
CONTROL_DESTINATION = Path("control") / "runtime_control.py"
DISPATCHER_DESTINATION = Path("bin") / "llm-hud"
_SHA256 = frozenset("0123456789abcdef")
MANAGED_LAUNCHER_MARKER = "# llm-hud-managed-launcher-v1"
MAX_LAUNCHER_SIZE = 64 * 1024
LAUNCHER_TEMP_PREFIX = ".llm-hud-launcher-"
CLAIM_TEMP_PREFIX = ".llm-hud-claim-"
STABLE_V1_DISPATCHER_SHA256 = (
    "79157470c26c620581cf69d701434e2ff2c0d6fd21744e8731b6eec99c7c7a2a"
)
STABLE_V1_CONTROL_SHA256 = (
    "77ed3c4ad64577040f3984c814cd803ba9d706134fd2807bff760ff2e3633040"
)
LEGACY_FLAT_DISPATCHER_SHA256 = (
    "e4cdde9c0b70b8cf507b4f7aae24327486256390096d55ad0f0c6aa76c6ca922"
)


@dataclass(frozen=True)
class StableProtocol:
    """One frozen revision of the stable dispatcher and control pair."""

    dispatcher_sha256: str
    control_sha256: str


# Every stable protocol revision ever shipped, oldest first.  Changing the
# stable tools means freezing their new hashes here as the next revision;
# installs recorded under any older revision migrate by file replacement,
# while unknown hashes (a newer llm-hud, or tampering) are refused.  Every
# revision must keep the dispatcher-facing interface: control.run(root, argv)
# and control.ControlError.
STABLE_PROTOCOLS: dict[int, StableProtocol] = {
    1: StableProtocol(
        dispatcher_sha256=STABLE_V1_DISPATCHER_SHA256,
        control_sha256=STABLE_V1_CONTROL_SHA256,
    ),
}
CURRENT_STABLE_PROTOCOL = 1


def _protocol_for_state(state: dict[str, object]) -> int | None:
    """The registered protocol revision a stable state file records."""
    pair = (state["dispatcher_sha256"], state["control_sha256"])
    for number, protocol in STABLE_PROTOCOLS.items():
        if pair == (protocol.dispatcher_sha256, protocol.control_sha256):
            return number
    return None


@dataclass(frozen=True)
class StableTools:
    dispatcher: str
    control: str
    dispatcher_sha256: str
    control_sha256: str


@dataclass(frozen=True)
class FileSnapshot:
    content: bytes
    device: int
    inode: int

    @property
    def sha256(self) -> str:
        return _sha256(self.content)


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


def _root_entries(
    root: Path,
    *,
    ignored: frozenset[str] = frozenset(),
) -> dict[str, tuple[int, int, int, int]]:
    try:
        entries = list(os.scandir(root))
    except OSError as error:
        raise RuntimeLayoutError(f"cannot inspect install root {root}: {error}") from error
    snapshot: dict[str, tuple[int, int, int, int]] = {}
    for entry in entries:
        if entry.name in ignored:
            continue
        path = Path(entry.path)
        metadata = _metadata(path, "install root entry")
        snapshot[entry.name] = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_nlink,
        )
    return snapshot


def _legacy_install_root_is_safe(
    root: Path,
    *,
    ignored: frozenset[str] = frozenset(),
) -> bool:
    if set(_root_entries(root, ignored=ignored)) != set(RUNTIME_CONTENT):
        return False
    source_digest(root)
    if embedded_runtime_version(root) != "0.1.0":
        return False
    launcher = _metadata(root / DISPATCHER_DESTINATION, "legacy dispatcher")
    if not stat.S_ISREG(launcher.st_mode) or launcher.st_nlink != 1:
        return False
    return (
        _sha256(_read_regular_bytes(root / DISPATCHER_DESTINATION, "legacy dispatcher"))
        == LEGACY_FLAT_DISPATCHER_SHA256
    )


def _unlink_matching_file(path: Path, identity: tuple[int, int]) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        return
    if (metadata.st_dev, metadata.st_ino) != identity:
        return
    try:
        path.unlink()
    except OSError:
        pass


def _claim_marker_exclusively(
    root: Path,
    marker: Path,
    baseline: dict[str, tuple[int, int, int, int]],
) -> None:
    descriptor, temp_name = tempfile.mkstemp(prefix=CLAIM_TEMP_PREFIX, dir=root)
    temp = Path(temp_name)
    temp_identity: tuple[int, int] | None = None
    marker_created = False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        payload = f"{INSTALL_MARKER_VALUE}\n".encode("utf-8")
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short write while claiming install root")
            remaining = remaining[written:]
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        temp_identity = (metadata.st_dev, metadata.st_ino)
        if _root_entries(root, ignored=frozenset({temp.name})) != baseline:
            raise RuntimeLayoutError("install root changed while claiming ownership")
        try:
            os.link(temp, marker, follow_symlinks=False)
            marker_created = True
        except FileExistsError as error:
            raise RuntimeLayoutError(
                f"install marker appeared while claiming ownership: {marker}"
            ) from error
        except OSError as error:
            raise RuntimeLayoutError(f"cannot create install marker {marker}: {error}") from error
        if _root_entries(
            root,
            ignored=frozenset({temp.name, marker.name}),
        ) != baseline:
            raise RuntimeLayoutError("install root changed while claiming ownership")
        try:
            temp.unlink()
        except FileNotFoundError:
            marker_metadata = marker.lstat()
            if (
                (marker_metadata.st_dev, marker_metadata.st_ino) != temp_identity
                or marker_metadata.st_nlink != 1
            ):
                raise RuntimeLayoutError(
                    "ownership claim changed before it could be committed"
                )
        temp_identity = None
        fsync_directory(root)
    finally:
        if temp_identity is not None:
            if marker_created:
                _unlink_matching_file(marker, temp_identity)
            _unlink_matching_file(temp, temp_identity)
        try:
            os.close(descriptor)
        except OSError:
            pass


def _lock_recovery_file(
    path: Path,
    *,
    expected_links: int,
) -> int | None:
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        before = path.lstat()
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
    except OSError:
        return None
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != expected_links
        or not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != expected_links
        or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        os.close(descriptor)
        return None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    except OSError:
        os.close(descriptor)
        return None
    return descriptor


def _read_locked_file(descriptor: int, limit: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _recover_orphaned_claim_temp(root: Path, *, allow_legacy: bool) -> None:
    try:
        entries = list(os.scandir(root))
    except OSError as error:
        raise RuntimeLayoutError(f"cannot inspect install root {root}: {error}") from error
    matches = [
        Path(entry.path)
        for entry in entries
        if entry.name.startswith(CLAIM_TEMP_PREFIX)
    ]
    if len(matches) != 1:
        return
    temp = matches[0]
    try:
        metadata = temp.lstat()
    except OSError:
        return
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        return
    descriptor = _lock_recovery_file(temp, expected_links=1)
    if descriptor is None:
        return
    try:
        expected = f"{INSTALL_MARKER_VALUE}\n".encode("utf-8")
        content = _read_locked_file(descriptor, len(expected))
        if content != expected:
            return
        remaining = _root_entries(root, ignored=frozenset({temp.name}))
        if remaining and not (
            allow_legacy
            and _legacy_install_root_is_safe(root, ignored=frozenset({temp.name}))
        ):
            return
        identity = (metadata.st_dev, metadata.st_ino)
        _unlink_matching_file(temp, identity)
        if temp.exists() or temp.is_symlink():
            raise RuntimeLayoutError(
                f"cannot recover interrupted ownership claim: {temp}"
            )
        fsync_directory(root)
    finally:
        os.close(descriptor)


def claim_install_root(root: Path, *, allow_legacy: bool = False) -> None:
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
    try:
        marker.lstat()
    except FileNotFoundError:
        content = None
    except OSError as error:
        raise RuntimeLayoutError(f"cannot inspect install marker {marker}: {error}") from error
    else:
        _recover_linked_claim_temp(root, marker)
        content = _read_regular_bytes(marker, "install marker")
    if content is not None:
        if content != f"{INSTALL_MARKER_VALUE}\n".encode("utf-8"):
            raise RuntimeLayoutError(f"unrecognized install marker: {marker}")
        return
    _recover_orphaned_claim_temp(root, allow_legacy=allow_legacy)
    baseline = _root_entries(root)
    if baseline and not (allow_legacy and _legacy_install_root_is_safe(root)):
        raise RuntimeLayoutError(f"refusing non-empty unmanaged install root: {root}")
    try:
        _claim_marker_exclusively(root, marker, baseline)
    except RuntimeLayoutError as error:
        # A concurrent installer may have won the claim race; accept the root
        # when its valid marker landed, otherwise surface the refusal.
        try:
            content = _read_regular_bytes(marker, "concurrent install marker")
        except RuntimeLayoutError:
            raise error
        if content != f"{INSTALL_MARKER_VALUE}\n".encode("utf-8"):
            raise error
        return
    if _read_regular_bytes(marker, "install marker") != (
        f"{INSTALL_MARKER_VALUE}\n".encode("utf-8")
    ):
        raise RuntimeLayoutError(f"cannot verify install marker: {marker}")


def _read_regular_bytes(
    path: Path,
    description: str,
    *,
    expected_links: int = 1,
) -> bytes:
    before = _metadata(path, description)
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != expected_links
    ):
        raise RuntimeLayoutError(
            f"{description} is not a single-link regular file: {path}"
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != expected_links
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise RuntimeLayoutError(f"{description} changed while opening: {path}")
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


def _recover_linked_claim_temp(root: Path, marker: Path) -> None:
    try:
        marker_metadata = marker.lstat()
    except OSError:
        return
    if not stat.S_ISREG(marker_metadata.st_mode) or marker_metadata.st_nlink != 2:
        return
    content = _read_regular_bytes(
        marker,
        "interrupted install marker",
        expected_links=2,
    )
    if content != f"{INSTALL_MARKER_VALUE}\n".encode("utf-8"):
        return
    matches: list[Path] = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    for entry in entries:
        if not entry.name.startswith(CLAIM_TEMP_PREFIX):
            continue
        path = Path(entry.path)
        try:
            metadata = path.lstat()
        except OSError:
            continue
        if (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_nlink == 2
            and (metadata.st_dev, metadata.st_ino)
            == (marker_metadata.st_dev, marker_metadata.st_ino)
        ):
            matches.append(path)
    if len(matches) != 1:
        return
    descriptor = _lock_recovery_file(matches[0], expected_links=2)
    if descriptor is None:
        return
    remaining = _root_entries(
        root,
        ignored=frozenset({marker.name, matches[0].name}),
    )
    try:
        if remaining and not _legacy_install_root_is_safe(
            root,
            ignored=frozenset({marker.name, matches[0].name}),
        ):
            return
        try:
            matches[0].unlink()
        except OSError as error:
            raise RuntimeLayoutError(
                f"cannot recover interrupted install marker {marker}: {error}"
            ) from error
        fsync_directory(root)
    finally:
        os.close(descriptor)


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


def _legacy_sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _legacy_launcher_content(python: Path, command: Path) -> str:
    return (
        "#!/bin/sh\n"
        f"exec {_legacy_sh_quote(str(python))} "
        f"{_legacy_sh_quote(str(command))} \"$@\"\n"
    )


def _known_launcher(content: bytes, command: Path) -> bool:
    try:
        text = content.decode("utf-8")
    except UnicodeError:
        return False
    if any(character in text for character in ("\0", "\r")):
        return False
    lines = text.splitlines(keepends=True)
    if len(lines) not in (2, 3) or any(not line.endswith("\n") for line in lines):
        return False
    if lines[0] != "#!/bin/sh\n":
        return False
    if len(lines) == 3:
        if lines[1] != f"{MANAGED_LAUNCHER_MARKER}\n":
            return False
        command_line = lines[2]
    else:
        command_line = lines[1]
    try:
        arguments = shlex.split(command_line, posix=True)
    except ValueError:
        return False
    if len(lines) == 3:
        if (
            len(arguments) != 6
            or arguments[0] != "exec"
            or arguments[2:4] != ["-I", "-B"]
            or arguments[5] != "$@"
        ):
            return False
        python = Path(arguments[1])
        installed_command = Path(arguments[4])
    else:
        if len(arguments) != 4 or arguments[0] != "exec" or arguments[3] != "$@":
            return False
        python = Path(arguments[1])
        installed_command = Path(arguments[2])
    if not python.is_absolute() or installed_command != command:
        return False
    canonical = (
        managed_launcher_content(python, command)
        if len(lines) == 3
        else _legacy_launcher_content(python, command)
    )
    return text == canonical


def _read_file_snapshot(
    path: Path,
    description: str,
    *,
    expected_links: int = 1,
) -> FileSnapshot | None:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RuntimeLayoutError(f"cannot inspect {description} {path}: {error}") from error
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != expected_links
    ):
        raise RuntimeLayoutError(
            f"{description} is not a single-link regular file: {path}"
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != expected_links
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise RuntimeLayoutError(f"{description} changed while opening: {path}")
        content = os.read(descriptor, MAX_LAUNCHER_SIZE + 1)
        if len(content) > MAX_LAUNCHER_SIZE:
            raise RuntimeLayoutError(f"{description} is too large: {path}")
        return FileSnapshot(content, opened.st_dev, opened.st_ino)
    except OSError as error:
        raise RuntimeLayoutError(f"cannot read {description} {path}: {error}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_launcher_state(root: Path) -> dict[str, object] | None:
    path = root / LAUNCHER_STATE_NAME
    snapshot = _read_file_snapshot(path, "launcher state")
    if snapshot is None:
        return None
    try:
        payload = json.loads(snapshot.content)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeLayoutError(f"invalid launcher state {path}: {error}") from error
    expected = {
        "schema",
        "launcher_path",
        "current_sha256",
        "pending_sha256",
    }
    schema = payload.get("schema") if isinstance(payload, dict) else None
    current = payload.get("current_sha256") if isinstance(payload, dict) else False
    pending = payload.get("pending_sha256") if isinstance(payload, dict) else False
    if (
        not isinstance(payload, dict)
        or set(payload) != expected
        or isinstance(schema, bool)
        or schema != LAUNCHER_STATE_SCHEMA
        or not isinstance(payload.get("launcher_path"), str)
        or not (current is None or _valid_sha256(current))
        or not (pending is None or _valid_sha256(pending))
    ):
        raise RuntimeLayoutError(f"unsupported launcher state: {path}")
    return payload


def _recover_linked_launcher_temp(
    launcher: Path,
    state: dict[str, object],
    command: Path,
    candidate_sha256: str,
    *,
    recover: bool,
) -> FileSnapshot | None:
    if state["pending_sha256"] is None:
        return None
    try:
        metadata = launcher.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 2:
        return None
    snapshot = _read_file_snapshot(
        launcher,
        "pending external launcher",
        expected_links=2,
    )
    assert snapshot is not None
    allowed = {
        state["current_sha256"],
        state["pending_sha256"],
        candidate_sha256,
    }
    if snapshot.sha256 not in allowed or not _known_launcher(snapshot.content, command):
        return None
    matches: list[Path] = []
    try:
        entries = list(os.scandir(launcher.parent))
    except OSError:
        return None
    for entry in entries:
        if not entry.name.startswith(LAUNCHER_TEMP_PREFIX):
            continue
        path = Path(entry.path)
        try:
            candidate_metadata = path.lstat()
        except OSError:
            continue
        if (
            stat.S_ISREG(candidate_metadata.st_mode)
            and candidate_metadata.st_nlink == 2
            and (candidate_metadata.st_dev, candidate_metadata.st_ino)
            == (snapshot.device, snapshot.inode)
        ):
            matches.append(path)
    if len(matches) != 1:
        return None
    if not recover:
        return snapshot
    try:
        matches[0].unlink()
    except OSError as error:
        raise RuntimeLayoutError(
            f"cannot recover pending external launcher {launcher}: {error}"
        ) from error
    fsync_directory(launcher.parent)
    return _read_file_snapshot(launcher, "recovered external launcher")


def _preflight_external_launcher(
    root: Path,
    launcher: Path,
    candidate: bytes,
    *,
    recover_interrupted: bool = False,
) -> FileSnapshot | None:
    root = root.resolve(strict=True)
    launcher = _canonical_file_path(launcher, "external launcher")
    command = root / DISPATCHER_DESTINATION
    if launcher.is_relative_to(root):
        raise RuntimeLayoutError("external launcher must be outside the install root")
    state = _read_launcher_state(root)
    candidate_sha256 = _sha256(candidate)
    if state is not None and state["launcher_path"] != str(launcher):
        raise RuntimeLayoutError(
            f"launcher state targets {state['launcher_path']}, not {launcher}"
        )
    linked_snapshot = None
    if state is not None:
        linked_snapshot = _recover_linked_launcher_temp(
            launcher,
            state,
            command,
            candidate_sha256,
            recover=recover_interrupted,
        )
    snapshot = linked_snapshot or _read_file_snapshot(launcher, "external launcher")
    if state is None:
        if snapshot is not None and not _known_launcher(snapshot.content, command):
            raise RuntimeLayoutError(f"refusing to replace unmanaged launcher: {launcher}")
        return snapshot
    if snapshot is None:
        return None
    allowed = {
        state["current_sha256"],
        state["pending_sha256"],
        candidate_sha256,
    }
    if snapshot.sha256 not in allowed or not _known_launcher(snapshot.content, command):
        raise RuntimeLayoutError(f"installed launcher was modified: {launcher}")
    return snapshot


def _write_launcher_file(
    launcher: Path,
    content: bytes,
    expected: FileSnapshot | None,
) -> None:
    _require_regular_directory(launcher.parent, "external launcher directory")
    descriptor, temp_name = tempfile.mkstemp(
        prefix=LAUNCHER_TEMP_PREFIX,
        dir=launcher.parent,
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            os.fchmod(handle.fileno(), 0o755)
            handle.flush()
            os.fsync(handle.fileno())
        current = _read_file_snapshot(launcher, "external launcher")
        if current != expected:
            raise RuntimeLayoutError("external launcher changed during installation")
        try:
            if expected is None:
                os.link(temp, launcher, follow_symlinks=False)
                temp.unlink()
            else:
                os.replace(temp, launcher)
        except FileExistsError as error:
            raise RuntimeLayoutError(
                f"external launcher appeared during installation: {launcher}"
            ) from error
        except OSError as error:
            raise RuntimeLayoutError(
                f"cannot replace external launcher {launcher}: {error}"
            ) from error
        fsync_directory(launcher.parent)
    finally:
        try:
            temp.unlink()
        except OSError:
            pass


def _decode_stable_source(path: Path, description: str) -> str:
    try:
        return _read_regular_bytes(path, description).decode("utf-8")
    except UnicodeError as error:
        raise RuntimeLayoutError(f"{description} is not UTF-8: {path}") from error


def load_stable_tools(source: Path) -> StableTools:
    dispatcher = _decode_stable_source(
        source / DISPATCHER_SOURCE, "stable dispatcher source"
    )
    control = _decode_stable_source(
        source / CONTROL_SOURCE, "stable runtime control source"
    )
    tools = StableTools(
        dispatcher=dispatcher,
        control=control,
        dispatcher_sha256=_sha256(dispatcher.encode("utf-8")),
        control_sha256=_sha256(control.encode("utf-8")),
    )
    current = STABLE_PROTOCOLS[CURRENT_STABLE_PROTOCOL]
    if (
        tools.dispatcher_sha256 != current.dispatcher_sha256
        or tools.control_sha256 != current.control_sha256
    ):
        raise RuntimeLayoutError(
            "stable tool sources do not match stable protocol "
            f"v{CURRENT_STABLE_PROTOCOL}; freeze the changed sources as the "
            "next revision in STABLE_PROTOCOLS"
        )
    for description, text in (
        ("stable dispatcher", tools.dispatcher),
        ("stable runtime control", tools.control),
    ):
        try:
            compile(text, description, "exec", dont_inherit=True)
        except (SyntaxError, ValueError) as error:
            raise RuntimeLayoutError(f"invalid {description}: {error}") from error
    return tools


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and not (set(value) - _SHA256)
    )


def _read_stable_state(path: Path) -> dict[str, object] | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RuntimeLayoutError(
            f"cannot inspect stable tools state {path}: {error}"
        ) from error
    raw = _read_regular_bytes(path, "stable tools state")
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeLayoutError(f"invalid stable tools state {path}: {error}") from error
    expected = {"schema", "dispatcher_sha256", "control_sha256"}
    schema = payload.get("schema") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or set(payload) != expected
        or isinstance(schema, bool)
        or schema != STABLE_STATE_SCHEMA
        or not _valid_sha256(payload.get("dispatcher_sha256"))
        or not _valid_sha256(payload.get("control_sha256"))
    ):
        raise RuntimeLayoutError(f"unsupported stable tools state: {path}")
    return payload


def _existing_file_sha256(path: Path, description: str) -> str | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RuntimeLayoutError(f"cannot inspect {description} {path}: {error}") from error
    return _sha256(_read_regular_bytes(path, description))


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


def _preflight_stable_directories(root: Path) -> None:
    for relative, description in (
        (Path("bin"), "stable bin directory"),
        (Path("control"), "stable control directory"),
    ):
        path = root / relative
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise RuntimeLayoutError(f"cannot inspect {description} {path}: {error}") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeLayoutError(f"{description} is not a regular directory: {path}")


def _handle_stable_tool_temps(
    root: Path,
    tools: StableTools,
    *,
    recover: bool,
) -> dict[Path, set[str]]:
    ignored: dict[Path, set[str]] = {}
    for directory, prefix, expected, description in (
        (
            root / CONTROL_DESTINATION.parent,
            f".{CONTROL_DESTINATION.name}.",
            tools.control.encode("utf-8"),
            "stable runtime control",
        ),
        (
            root / DISPATCHER_DESTINATION.parent,
            f".{DISPATCHER_DESTINATION.name}.",
            tools.dispatcher.encode("utf-8"),
            "stable dispatcher",
        ),
    ):
        try:
            entries = list(os.scandir(directory))
        except FileNotFoundError:
            continue
        except OSError as error:
            raise RuntimeLayoutError(
                f"cannot inspect {description} directory {directory}: {error}"
            ) from error
        changed = False
        for entry in entries:
            if not entry.name.startswith(prefix):
                continue
            path = Path(entry.path)
            try:
                metadata = path.lstat()
            except OSError as error:
                raise RuntimeLayoutError(
                    f"cannot inspect interrupted {description} file {path}: {error}"
                ) from error
            if metadata.st_size > len(expected):
                raise RuntimeLayoutError(
                    f"refusing unrecognized interrupted {description} file: {path}"
                )
            try:
                content = _read_regular_bytes(path, f"interrupted {description}")
            except RuntimeLayoutError as error:
                raise RuntimeLayoutError(
                    f"refusing unsafe interrupted {description} file: {path}"
                ) from error
            if not expected.startswith(content):
                raise RuntimeLayoutError(
                    f"refusing unrecognized interrupted {description} file: {path}"
                )
            if recover:
                try:
                    path.unlink()
                except OSError as error:
                    raise RuntimeLayoutError(
                        f"cannot recover interrupted {description} file {path}: {error}"
                    ) from error
                changed = True
            else:
                ignored.setdefault(directory, set()).add(entry.name)
        if changed:
            fsync_directory(directory)
    return ignored


def _preflight_stable_tools(
    root: Path,
    tools: StableTools,
    *,
    allow_legacy_dispatcher: bool,
    recover_temps: bool = False,
) -> None:
    _preflight_stable_directories(root)
    ignored = _handle_stable_tool_temps(
        root,
        tools,
        recover=recover_temps,
    )
    state = _read_stable_state(root / STABLE_STATE_NAME)
    dispatcher_path = root / DISPATCHER_DESTINATION
    control_path = root / CONTROL_DESTINATION
    dispatcher_hash = _existing_file_sha256(
        dispatcher_path, "installed stable dispatcher"
    )
    control_hash = _existing_file_sha256(
        control_path, "installed stable runtime control"
    )
    if state is None:
        allowed_dispatchers: set[str | None] = {None, tools.dispatcher_sha256}
        if allow_legacy_dispatcher:
            allowed_dispatchers.add(LEGACY_FLAT_DISPATCHER_SHA256)
        if dispatcher_hash not in allowed_dispatchers:
            raise RuntimeLayoutError(
                f"refusing to replace unmanaged stable dispatcher: {dispatcher_path}"
            )
        if control_hash not in (None, tools.control_sha256):
            raise RuntimeLayoutError(
                f"refusing to replace unmanaged stable runtime control: {control_path}"
            )
        control_directory = root / CONTROL_DESTINATION.parent
        try:
            entries = {entry.name for entry in control_directory.iterdir()}
        except FileNotFoundError:
            entries = set()
        except OSError as error:
            raise RuntimeLayoutError(
                f"cannot inspect stable control directory {control_directory}: {error}"
            ) from error
        if entries - {CONTROL_DESTINATION.name} - ignored.get(control_directory, set()):
            raise RuntimeLayoutError(
                f"refusing non-empty unmanaged stable control directory: {control_directory}"
            )
        return

    installed_protocol = _protocol_for_state(state)
    if installed_protocol is None:
        raise RuntimeLayoutError(
            "installed stable tools use an unsupported protocol (possibly "
            "written by a newer llm-hud); rerun the newest installer"
        )
    recorded = STABLE_PROTOCOLS[installed_protocol]
    current = STABLE_PROTOCOLS[CURRENT_STABLE_PROTOCOL]
    # A crash mid-migration can leave either revision on disk; accept both so
    # the installer can finish replacing them with the current protocol.
    allowed_dispatchers = {recorded.dispatcher_sha256, current.dispatcher_sha256}
    allowed_controls = {recorded.control_sha256, current.control_sha256}
    if dispatcher_hash is not None and dispatcher_hash not in allowed_dispatchers:
        raise RuntimeLayoutError(
            f"installed stable dispatcher was modified: {dispatcher_path}"
        )
    if control_hash is not None and control_hash not in allowed_controls:
        raise RuntimeLayoutError(
            f"installed stable runtime control was modified: {control_path}"
        )


def _install_stable_tools_unlocked(
    root: Path,
    tools: StableTools,
    *,
    allow_legacy_dispatcher: bool,
) -> None:
    _preflight_stable_tools(
        root,
        tools,
        allow_legacy_dispatcher=allow_legacy_dispatcher,
        recover_temps=True,
    )
    _require_regular_directory(root / "bin", "stable bin directory")
    _require_regular_directory(root / "control", "stable control directory")
    try:
        control_path = root / CONTROL_DESTINATION
        dispatcher_path = root / DISPATCHER_DESTINATION
        if _existing_file_sha256(control_path, "installed stable runtime control") \
            != tools.control_sha256:
            atomic_write_text(
                control_path,
                tools.control,
                mode=0o600,
                follow_symlinks=False,
            )
        if _existing_file_sha256(dispatcher_path, "installed stable dispatcher") \
            != tools.dispatcher_sha256:
            atomic_write_text(
                dispatcher_path,
                tools.dispatcher,
                mode=0o700,
                follow_symlinks=False,
            )
        atomic_write_json(
            root / STABLE_STATE_NAME,
            {
                "schema": STABLE_STATE_SCHEMA,
                "dispatcher_sha256": tools.dispatcher_sha256,
                "control_sha256": tools.control_sha256,
            },
            follow_symlinks=False,
        )
    except OSError as error:
        raise RuntimeLayoutError(f"cannot install stable runtime tools: {error}") from error


def install_stable_tools(
    source: Path,
    root: Path,
    *,
    expected_active: str,
    allow_legacy_dispatcher: bool = False,
) -> None:
    source = Path(source)
    root = Path(root).resolve(strict=True)
    tools = load_stable_tools(source)
    _preflight_stable_tools(
        root, tools, allow_legacy_dispatcher=allow_legacy_dispatcher
    )
    with RuntimeLock(root):
        current = read_activation(root)
        actual_active = current.active if current is not None else None
        if actual_active != expected_active:
            raise RuntimeLayoutError(
                f"active runtime changed from {expected_active!r} to {actual_active!r}"
            )
        _install_stable_tools_unlocked(
            root, tools, allow_legacy_dispatcher=allow_legacy_dispatcher
        )


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
    candidate = managed_launcher_content(
        python, root / DISPATCHER_DESTINATION
    ).encode("utf-8")
    _preflight_external_launcher(root, launcher, candidate)
    with RuntimeLock(root):
        current = read_activation(root)
        actual_active = current.active if current is not None else None
        if actual_active != expected_active:
            raise RuntimeLayoutError(
                f"active runtime changed from {expected_active!r} to {actual_active!r}"
            )
        snapshot = _preflight_external_launcher(
            root,
            launcher,
            candidate,
            recover_interrupted=True,
        )
        state_path = root / LAUNCHER_STATE_NAME
        pending_state = {
            "schema": LAUNCHER_STATE_SCHEMA,
            "launcher_path": str(launcher),
            "current_sha256": snapshot.sha256 if snapshot is not None else None,
            "pending_sha256": _sha256(candidate),
        }
        try:
            atomic_write_json(
                state_path,
                pending_state,
                follow_symlinks=False,
            )
            _write_launcher_file(launcher, candidate, snapshot)
            atomic_write_json(
                state_path,
                {
                    **pending_state,
                    "current_sha256": _sha256(candidate),
                    "pending_sha256": None,
                },
                follow_symlinks=False,
            )
        except OSError as error:
            raise RuntimeLayoutError(f"cannot install external launcher: {error}") from error


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
    candidate = managed_launcher_content(python, command).encode("utf-8")
    snapshot = _read_file_snapshot(launcher, "external launcher")
    if snapshot is not None and not _known_launcher(snapshot.content, command):
        raise RuntimeLayoutError(f"refusing to replace unmanaged launcher: {launcher}")
    _write_launcher_file(launcher, candidate, snapshot)


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


def _prune_inactive_runtimes(root: Path, *, lock_timeout: float = 10.0) -> None:
    """Remove finalized releases no longer reachable as active or previous."""
    with RuntimeLock(root, timeout=lock_timeout):
        activation = read_activation(root)
        if activation is None:
            return
        keep = {activation.active}
        if activation.previous is not None:
            keep.add(activation.previous)
        versions = root / VERSIONS_DIR_NAME
        try:
            entries = list(os.scandir(versions))
        except OSError:
            return
        changed = False
        for entry in entries:
            if entry.name in keep:
                continue
            path = Path(entry.path)
            try:
                metadata = path.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                continue
            shutil.rmtree(path, ignore_errors=True)
            changed = True
        if changed:
            fsync_directory(versions)


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


def _validate_frozen_control(control: str) -> str:
    current = STABLE_PROTOCOLS[CURRENT_STABLE_PROTOCOL]
    if _sha256(control.encode("utf-8")) != current.control_sha256:
        raise RuntimeLayoutError(
            "stable runtime control does not match stable protocol "
            f"v{CURRENT_STABLE_PROTOCOL}; freeze the changed source as the "
            "next revision in STABLE_PROTOCOLS"
        )
    try:
        compile(control, "stable runtime control", "exec", dont_inherit=True)
    except (SyntaxError, ValueError) as error:
        raise RuntimeLayoutError(f"invalid stable runtime control: {error}") from error
    return control


def _default_frozen_control() -> str:
    source_root = Path(__file__).resolve().parents[2]
    return _validate_frozen_control(
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
    control = _validate_frozen_control(control)
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


def install_runtime_from_source(
    source: Path,
    root: Path,
    python: Path | None = None,
    *,
    stable_control: str | None = None,
) -> tuple[RuntimeMetadata, Activation]:
    source = Path(source)
    root = Path(root)
    python = Path(sys.executable) if python is None else Path(python)
    if stable_control is None:
        stable_control = _default_frozen_control()
    else:
        stable_control = _validate_frozen_control(stable_control)
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
        activation = activate(
            root,
            metadata.release_id,
            expected_active=expected_active,
        )
        return metadata, activation
    finally:
        _remove_staging(
            root,
            staging,
            root_identity=root_identity,
            staging_identity=staging_identity,
        )


def install_versioned_runtime(
    source: Path,
    root: Path,
    python: Path | None = None,
    *,
    allow_legacy_dispatcher: bool = False,
) -> tuple[RuntimeMetadata, Activation]:
    source = Path(source)
    root = Path(root)
    tools = load_stable_tools(source)
    _preflight_stable_tools(
        root, tools, allow_legacy_dispatcher=allow_legacy_dispatcher
    )
    activation_before = _read_existing_activation(root)
    metadata, activation = install_runtime_from_source(
        source,
        root,
        python,
        stable_control=tools.control,
    )
    try:
        install_stable_tools(
            source,
            root,
            expected_active=metadata.release_id,
            allow_legacy_dispatcher=allow_legacy_dispatcher,
        )
    except Exception as error:
        _restore_after_install_failure(
            root,
            activation_before,
            metadata.release_id,
            error,
        )
        raise
    return metadata, activation


def _read_existing_activation(root: Path) -> Activation | None:
    try:
        (root / ACTIVATION_NAME).lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RuntimeLayoutError(f"cannot inspect activation record: {error}") from error
    return read_activation(root)


def _restore_after_install_failure(
    root: Path,
    previous: Activation | None,
    expected_active: str,
    failure: Exception,
) -> None:
    if previous is None:
        return
    try:
        restore_activation(root, previous, expected_active=expected_active)
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
    *,
    allow_legacy_dispatcher: bool = False,
) -> tuple[RuntimeMetadata, Activation]:
    source = Path(source)
    root = Path(root)
    launcher = Path(launcher)
    python = Path(python)
    candidate_launcher = managed_launcher_content(
        python, root.resolve(strict=True) / DISPATCHER_DESTINATION
    ).encode("utf-8")
    _preflight_external_launcher(root, launcher, candidate_launcher)
    activation_before = _read_existing_activation(root)
    metadata, activation = install_versioned_runtime(
        source,
        root,
        python,
        allow_legacy_dispatcher=allow_legacy_dispatcher,
    )
    root = root.resolve(strict=True)
    try:
        _smoke_test_dispatcher(root, python, metadata.version)
        install_external_launcher(
            root,
            launcher,
            python,
            expected_active=metadata.release_id,
        )
    except Exception as error:
        _restore_after_install_failure(
            root,
            activation_before,
            metadata.release_id,
            error,
        )
        raise
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
    parser.add_argument("--allow-legacy-root", action="store_true")
    parser.add_argument("--allow-legacy-dispatcher", action="store_true")
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
            claim_install_root(args.root, allow_legacy=args.allow_legacy_root)
        metadata, _ = install_complete(
            args.source,
            args.root,
            args.launcher,
            args.python,
            allow_legacy_dispatcher=args.allow_legacy_dispatcher,
        )
    except (OSError, RuntimeLayoutError) as error:
        print(f"llm-hud installer: {error}", file=sys.stderr)
        return 1
    print(metadata.release_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
