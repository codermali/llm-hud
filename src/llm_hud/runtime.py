from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from llm_hud._platform import try_lock_descriptor, unlock_descriptor
from llm_hud.storage import atomic_write_json, atomic_write_text, fsync_directory


LAYOUT_MARKER_NAME = ".llm-hud-layout"
LAYOUT_MARKER_VALUE = "llm-hud-versioned-layout-v1"
INSTALL_MARKER_NAME = ".llm-hud-install-root"
INSTALL_MARKER_VALUE = "llm-hud-install-root-v1"
ACTIVATION_NAME = "activation"
ACTIVATION_PREFIX = "llm-hud-activation-v1"
RUNTIME_MARKER_NAME = ".llm-hud-runtime.json"
LOCK_NAME = ".llm-hud-update.lock"
VERSIONS_DIR_NAME = "versions"
CONTROL_DIR_NAME = "control"
STABLE_STATE_NAME = ".llm-hud-stable.json"
LAUNCHER_STATE_NAME = ".llm-hud-launcher-state.json"
NO_PREVIOUS = "-"
_RELEASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_VERSION = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\Z"
)
RUNTIME_CONTENT = ("src", "bin", "README.md", "LICENSE", "pyproject.toml")
_UNSET = object()


class RuntimeLayoutError(ValueError):
    """The managed runtime layout is absent, damaged, or unsafe."""


def validate_release_id(value: object) -> str:
    if not isinstance(value, str) or not _RELEASE_ID.fullmatch(value):
        raise RuntimeLayoutError(f"invalid release id: {value!r}")
    if value in (".", ".."):
        raise RuntimeLayoutError(f"invalid release id: {value!r}")
    return value


def validate_version(value: object) -> str:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise RuntimeLayoutError(f"invalid runtime version: {value!r}")
    return value


@dataclass(frozen=True)
class Activation:
    active: str
    previous: str | None = None

    def __post_init__(self) -> None:
        validate_release_id(self.active)
        if self.previous is not None:
            validate_release_id(self.previous)
            if self.previous == self.active:
                raise RuntimeLayoutError("active and previous releases must differ")


@dataclass(frozen=True)
class RuntimeMetadata:
    release_id: str
    version: str
    content_sha256: str

    def __post_init__(self) -> None:
        validate_release_id(self.release_id)
        validate_version(self.version)
        if not isinstance(self.content_sha256, str) or not _SHA256.fullmatch(
            self.content_sha256
        ):
            raise RuntimeLayoutError("runtime digest must be a lowercase SHA-256")

    def as_json(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "release_id": self.release_id,
            "version": self.version,
            "content_sha256": self.content_sha256,
        }


def format_activation(value: Activation) -> str:
    previous = value.previous if value.previous is not None else NO_PREVIOUS
    return f"{ACTIVATION_PREFIX} {value.active} {previous}\n"


def parse_activation(text: str) -> Activation:
    parts = text.removesuffix("\n").split(" ")
    if len(parts) != 3 or parts[0] != ACTIVATION_PREFIX:
        raise RuntimeLayoutError("invalid activation record")
    previous = None if parts[2] == NO_PREVIOUS else parts[2]
    value = Activation(parts[1], previous)
    if text != format_activation(value):
        raise RuntimeLayoutError("activation record is not in canonical format")
    return value


def _read_owned_text(path: Path, description: str) -> str | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RuntimeLayoutError(f"cannot inspect {description} {path}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeLayoutError(f"{description} is not a regular managed file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeError) as error:
        raise RuntimeLayoutError(f"cannot read {description} {path}: {error}") from error


def _require_install_ownership(root: Path) -> None:
    try:
        metadata = root.lstat()
    except OSError as error:
        raise RuntimeLayoutError(f"cannot inspect install root {root}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeLayoutError(f"install root is not a managed directory: {root}")
    marker = _read_owned_text(root / INSTALL_MARKER_NAME, "install marker")
    if marker != f"{INSTALL_MARKER_VALUE}\n":
        raise RuntimeLayoutError(f"install root is not owned by llm-hud: {root}")


def _require_layout(root: Path) -> None:
    _require_install_ownership(root)
    marker = _read_owned_text(root / LAYOUT_MARKER_NAME, "layout marker")
    if marker != f"{LAYOUT_MARKER_VALUE}\n":
        raise RuntimeLayoutError(f"missing or unrecognized runtime layout in {root}")
    versions = root / VERSIONS_DIR_NAME
    try:
        metadata = versions.lstat()
    except OSError as error:
        raise RuntimeLayoutError(
            f"cannot inspect versions directory {versions}: {error}"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeLayoutError(f"versions path is not a managed directory: {versions}")


def initialize_layout(root: Path) -> None:
    _require_install_ownership(root)
    versions = root / VERSIONS_DIR_NAME
    try:
        versions_metadata = versions.lstat()
    except FileNotFoundError:
        versions_metadata = None
    if versions_metadata is not None and (
        stat.S_ISLNK(versions_metadata.st_mode)
        or not stat.S_ISDIR(versions_metadata.st_mode)
    ):
        raise RuntimeLayoutError(f"versions path is not a directory: {versions}")

    marker_path = root / LAYOUT_MARKER_NAME
    marker = _read_owned_text(marker_path, "layout marker")
    if marker is None:
        reserved = (
            versions,
            root / CONTROL_DIR_NAME,
            root / ACTIVATION_NAME,
            root / LOCK_NAME,
            root / STABLE_STATE_NAME,
            root / LAUNCHER_STATE_NAME,
        )
        for path in reserved:
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise RuntimeLayoutError(
                    f"cannot inspect reserved runtime path {path}: {error}"
                ) from error
            raise RuntimeLayoutError(
                f"refusing to claim pre-existing reserved runtime path: {path}"
            )
        try:
            atomic_write_text(
                marker_path,
                f"{LAYOUT_MARKER_VALUE}\n",
                mode=0o600,
                follow_symlinks=False,
            )
        except OSError as error:
            raise RuntimeLayoutError(
                f"cannot write runtime layout marker {marker_path}: {error}"
            ) from error
    elif marker != f"{LAYOUT_MARKER_VALUE}\n":
        raise RuntimeLayoutError(f"unrecognized runtime layout marker: {marker_path}")

    if versions_metadata is None:
        try:
            versions.mkdir(mode=0o700)
        except FileExistsError:
            metadata = versions.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeLayoutError(f"versions path is not a directory: {versions}")
        except OSError as error:
            raise RuntimeLayoutError(
                f"cannot create versions directory {versions}: {error}"
            ) from error
        fsync_directory(root)


def runtime_path(root: Path, release_id: str) -> Path:
    return root / VERSIONS_DIR_NAME / validate_release_id(release_id)


def _require_runtime_shape(path: Path, *, sealed: bool) -> None:
    expected = set(RUNTIME_CONTENT)
    if sealed:
        expected.add(RUNTIME_MARKER_NAME)
    try:
        actual = {entry.name for entry in path.iterdir()}
    except OSError as error:
        raise RuntimeLayoutError(f"cannot inspect runtime contents {path}: {error}") from error
    if actual != expected:
        unexpected = sorted(actual - expected)
        missing = sorted(expected - actual)
        details: list[str] = []
        if unexpected:
            details.append(f"unexpected: {', '.join(unexpected)}")
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        raise RuntimeLayoutError(
            f"runtime has an invalid top-level shape: {path} ({'; '.join(details)})"
        )


def _write_runtime_metadata(path: Path, metadata: RuntimeMetadata) -> None:
    try:
        path_metadata = path.lstat()
    except OSError as error:
        raise RuntimeLayoutError(f"cannot inspect runtime directory {path}: {error}") from error
    if stat.S_ISLNK(path_metadata.st_mode) or not stat.S_ISDIR(path_metadata.st_mode):
        raise RuntimeLayoutError(f"runtime path is not a directory: {path}")
    try:
        atomic_write_json(
            path / RUNTIME_MARKER_NAME,
            metadata.as_json(),
            follow_symlinks=False,
        )
    except OSError as error:
        raise RuntimeLayoutError(f"cannot write runtime marker in {path}: {error}") from error


def _read_runtime_metadata(path: Path) -> RuntimeMetadata:
    raw = _read_owned_text(path / RUNTIME_MARKER_NAME, "runtime marker")
    if raw is None:
        raise RuntimeLayoutError(f"runtime marker is missing: {path}")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeLayoutError(f"invalid runtime marker {path}: {error}") from error
    expected_keys = {"schema", "release_id", "version", "content_sha256"}
    schema = payload.get("schema") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or set(payload) != expected_keys
        or isinstance(schema, bool)
        or schema != 1
    ):
        raise RuntimeLayoutError(f"unsupported runtime marker: {path}")
    try:
        return RuntimeMetadata(
            release_id=payload.get("release_id"),
            version=payload.get("version"),
            content_sha256=payload.get("content_sha256"),
        )
    except (TypeError, RuntimeLayoutError) as error:
        raise RuntimeLayoutError(f"invalid runtime marker {path}: {error}") from error


def validate_runtime(root: Path, release_id: str) -> RuntimeMetadata:
    _require_layout(root)
    path = runtime_path(root, release_id)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RuntimeLayoutError(f"cannot inspect runtime {path}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeLayoutError(f"runtime is not a managed directory: {path}")
    _require_runtime_shape(path, sealed=True)
    runtime_metadata = _read_runtime_metadata(path)
    if runtime_metadata.release_id != release_id:
        raise RuntimeLayoutError(f"runtime marker release id does not match {path}")
    expected_release_id = (
        f"{runtime_metadata.version}-{runtime_metadata.content_sha256[:12]}"
    )
    if release_id != expected_release_id:
        raise RuntimeLayoutError(f"runtime marker fields do not match {path}")
    for relative in ("bin/llm-hud", "src/llm_hud/cli.py", "src/llm_hud/_version.py"):
        required = path / relative
        try:
            required_metadata = required.lstat()
        except OSError as error:
            raise RuntimeLayoutError(f"runtime file is missing: {required}") from error
        if stat.S_ISLNK(required_metadata.st_mode) or not stat.S_ISREG(
            required_metadata.st_mode
        ):
            raise RuntimeLayoutError(f"runtime file is not regular: {required}")
    launcher = path / "bin" / "llm-hud"
    if not os.access(launcher, os.X_OK):
        raise RuntimeLayoutError(f"runtime launcher is not executable: {launcher}")
    actual_digest = source_digest(path)
    if actual_digest != runtime_metadata.content_sha256:
        raise RuntimeLayoutError(f"runtime content digest does not match: {path}")
    if embedded_runtime_version(path) != runtime_metadata.version:
        raise RuntimeLayoutError(f"runtime code version does not match marker: {path}")
    return runtime_metadata


def read_activation(root: Path) -> Activation | None:
    _require_layout(root)
    raw = _read_owned_text(root / ACTIVATION_NAME, "activation record")
    return None if raw is None else parse_activation(raw)


def _activate_unlocked(
    root: Path,
    release_id: str,
    *,
    expected_active: str | None | object = _UNSET,
) -> Activation:
    _require_layout(root)
    validate_runtime(root, release_id)
    current = read_activation(root)
    actual_active = current.active if current is not None else None
    if expected_active is not _UNSET:
        if expected_active is not None:
            validate_release_id(expected_active)
        if actual_active != expected_active:
            raise RuntimeLayoutError(
                f"active runtime changed from {expected_active!r} to {actual_active!r}"
            )
    previous: str | None = None
    if current is not None:
        if current.active == release_id:
            return current
        try:
            validate_runtime(root, current.active)
        except RuntimeLayoutError:
            if current.previous is not None and current.previous != release_id:
                try:
                    validate_runtime(root, current.previous)
                except RuntimeLayoutError:
                    pass
                else:
                    previous = current.previous
        else:
            previous = current.active
    updated = Activation(
        active=release_id,
        previous=previous,
    )
    try:
        atomic_write_text(
            root / ACTIVATION_NAME,
            format_activation(updated),
            mode=0o600,
            follow_symlinks=False,
        )
    except OSError as error:
        raise RuntimeLayoutError(f"cannot write activation record: {error}") from error
    return updated


def activate(
    root: Path,
    release_id: str,
    *,
    expected_active: str | None | object = _UNSET,
    lock_timeout: float = 10.0,
) -> Activation:
    with RuntimeLock(root, timeout=lock_timeout):
        return _activate_unlocked(
            root, release_id, expected_active=expected_active
        )


def restore_activation(
    root: Path,
    previous: Activation,
    *,
    expected_active: str,
    lock_timeout: float = 10.0,
) -> Activation:
    """Restore an exact earlier activation after a failed installation."""
    validate_release_id(expected_active)
    with RuntimeLock(root, timeout=lock_timeout):
        current = read_activation(root)
        actual_active = current.active if current is not None else None
        if actual_active != expected_active:
            raise RuntimeLayoutError(
                f"active runtime changed from {expected_active!r} "
                f"to {actual_active!r}"
            )
        validate_runtime(root, previous.active)
        try:
            atomic_write_text(
                root / ACTIVATION_NAME,
                format_activation(previous),
                mode=0o600,
                follow_symlinks=False,
            )
        except OSError as error:
            raise RuntimeLayoutError(
                f"cannot restore activation record: {error}"
            ) from error
        return previous


def _rollback_unlocked(
    root: Path, *, expected_active: str | None | object = _UNSET
) -> Activation:
    current = read_activation(root)
    actual_active = current.active if current is not None else None
    if expected_active is not _UNSET:
        if expected_active is not None:
            validate_release_id(expected_active)
        if actual_active != expected_active:
            raise RuntimeLayoutError(
                f"active runtime changed from {expected_active!r} to {actual_active!r}"
            )
    if current is None or current.previous is None:
        raise RuntimeLayoutError("no previous runtime is available")
    validate_runtime(root, current.previous)
    updated = Activation(active=current.previous, previous=current.active)
    try:
        atomic_write_text(
            root / ACTIVATION_NAME,
            format_activation(updated),
            mode=0o600,
            follow_symlinks=False,
        )
    except OSError as error:
        raise RuntimeLayoutError(f"cannot write activation record: {error}") from error
    return updated


def rollback(
    root: Path,
    *,
    expected_active: str | None | object = _UNSET,
    lock_timeout: float = 10.0,
) -> Activation:
    with RuntimeLock(root, timeout=lock_timeout):
        return _rollback_unlocked(root, expected_active=expected_active)


def _read_source_file(path: Path) -> tuple[bytes, bool]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RuntimeLayoutError(f"cannot open runtime source file {path}: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeLayoutError(f"runtime source file is not regular: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), bool(metadata.st_mode & 0o111)
    except OSError as error:
        raise RuntimeLayoutError(f"cannot read runtime source file {path}: {error}") from error
    finally:
        os.close(descriptor)


def _fsync_regular_file(path: Path) -> None:
    # Windows rejects fsync() on a read-only CRT descriptor. Staged runtime
    # files are private copies owned by the installer, so open them read/write
    # there while retaining the narrower POSIX access mode.
    access = os.O_RDWR if os.name == "nt" else os.O_RDONLY
    flags = access | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RuntimeLayoutError(f"cannot open runtime file for sync {path}: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeLayoutError(f"runtime file is not a regular file: {path}")
        os.fsync(descriptor)
    except OSError as error:
        raise RuntimeLayoutError(f"cannot sync runtime file {path}: {error}") from error
    finally:
        os.close(descriptor)


def _fsync_directory_required(path: Path) -> None:
    if os.name == "nt":
        try:
            metadata = path.lstat()
        except OSError as error:
            raise RuntimeLayoutError(
                f"cannot inspect runtime directory for sync {path}: {error}"
            ) from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeLayoutError(f"runtime path is not a directory: {path}")
        # Windows does not expose a portable directory handle that os.fsync()
        # accepts. Runtime files are still flushed before each atomic rename.
        return
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RuntimeLayoutError(
            f"cannot open runtime directory for sync {path}: {error}"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeLayoutError(f"runtime path is not a directory: {path}")
        os.fsync(descriptor)
    except OSError as error:
        raise RuntimeLayoutError(f"cannot sync runtime directory {path}: {error}") from error
    finally:
        os.close(descriptor)


def _fsync_runtime_tree(path: Path) -> None:
    directories = [path]
    try:
        entries = list(path.rglob("*"))
    except OSError as error:
        raise RuntimeLayoutError(f"cannot enumerate staged runtime {path}: {error}") from error
    for entry in entries:
        try:
            metadata = entry.lstat()
        except OSError as error:
            raise RuntimeLayoutError(
                f"cannot inspect staged runtime entry {entry}: {error}"
            ) from error
        relative = entry.relative_to(path)
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeLayoutError(f"staged runtime entry is a symlink: {entry}")
        if stat.S_ISDIR(metadata.st_mode):
            if entry.name == "__pycache__":
                raise RuntimeLayoutError(
                    f"staged runtime contains a Python cache: {entry}"
                )
            directories.append(entry)
        elif stat.S_ISREG(metadata.st_mode):
            if "__pycache__" in relative.parts or entry.suffix in (".pyc", ".pyo"):
                raise RuntimeLayoutError(
                    f"staged runtime contains Python bytecode: {entry}"
                )
            _fsync_regular_file(entry)
        else:
            raise RuntimeLayoutError(f"staged runtime entry is not regular: {entry}")
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        _fsync_directory_required(directory)


def embedded_runtime_version(root: Path) -> str:
    path = root / "src" / "llm_hud" / "_version.py"
    content, _ = _read_source_file(path)
    try:
        tree = ast.parse(content.decode("utf-8"), filename=str(path))
    except (UnicodeDecodeError, SyntaxError) as error:
        raise RuntimeLayoutError(f"cannot parse runtime version {path}: {error}") from error
    values: list[object] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "__version__":
            values.append(node.value.value if isinstance(node.value, ast.Constant) else None)
    if len(values) != 1:
        raise RuntimeLayoutError(f"runtime version assignment is missing or ambiguous: {path}")
    return validate_version(values[0])


def source_digest(root: Path, *, reject_python_cache: bool = False) -> str:
    """Hash canonical runtime content, excluding normal generated bytecode caches."""
    digest = hashlib.sha256()
    files: list[Path] = []
    for name in RUNTIME_CONTENT:
        candidate = root / name
        try:
            candidate_metadata = candidate.lstat()
        except OSError as error:
            raise RuntimeLayoutError(f"runtime source is missing: {candidate}") from error
        if stat.S_ISLNK(candidate_metadata.st_mode):
            raise RuntimeLayoutError(f"runtime source path is a symlink: {candidate}")
        if stat.S_ISDIR(candidate_metadata.st_mode):
            for path in candidate.rglob("*"):
                metadata = path.lstat()
                relative = path.relative_to(root)
                in_python_cache = "__pycache__" in relative.parts
                if stat.S_ISLNK(metadata.st_mode):
                    raise RuntimeLayoutError(
                        f"runtime source path is a symlink: {path}"
                    )
                if stat.S_ISREG(metadata.st_mode):
                    is_bytecode = path.suffix in (".pyc", ".pyo")
                    if in_python_cache and is_bytecode:
                        if reject_python_cache:
                            raise RuntimeLayoutError(
                                f"runtime source contains a Python cache: {path}"
                            )
                        continue
                    if is_bytecode:
                        raise RuntimeLayoutError(
                            f"runtime source contains bytecode outside __pycache__: {path}"
                        )
                    files.append(path)
                elif not stat.S_ISDIR(metadata.st_mode):
                    raise RuntimeLayoutError(
                        f"runtime source path is not regular: {path}"
                    )
                elif reject_python_cache and path.name == "__pycache__":
                    raise RuntimeLayoutError(
                        f"runtime source contains a Python cache: {path}"
                    )
        elif stat.S_ISREG(candidate_metadata.st_mode):
            files.append(candidate)
        else:
            raise RuntimeLayoutError(f"runtime source path is not regular: {candidate}")
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        name = relative.as_posix().encode("utf-8")
        content, executable = _read_source_file(path)
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(b"\x01" if executable else b"\x00")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def validate_staged_runtime(
    staging: Path,
    version: str,
    *,
    expected_content_sha256: str | None = None,
) -> RuntimeMetadata:
    version = validate_version(version)
    _require_runtime_shape(staging, sealed=False)
    embedded_version = embedded_runtime_version(staging)
    if embedded_version != version:
        raise RuntimeLayoutError(
            f"staged runtime version is {embedded_version}, expected {version}"
        )
    digest = source_digest(staging, reject_python_cache=True)
    if expected_content_sha256 is not None:
        if not _SHA256.fullmatch(expected_content_sha256):
            raise RuntimeLayoutError(
                "expected content digest must be a lowercase SHA-256"
            )
        if digest != expected_content_sha256:
            raise RuntimeLayoutError(
                "staged runtime digest does not match the expected value"
            )
    release_id = validate_release_id(f"{version}-{digest[:12]}")
    return RuntimeMetadata(release_id, version, digest)


def _finalize_runtime_unlocked(
    root: Path,
    staging: Path,
    version: str,
    *,
    expected_content_sha256: str | None = None,
) -> RuntimeMetadata:
    _require_layout(root)
    version = validate_version(version)
    try:
        root_path = root.resolve(strict=True)
        staging_parent = staging.parent.resolve(strict=True)
        staging_metadata = staging.lstat()
    except (OSError, ValueError, RuntimeError) as error:
        raise RuntimeLayoutError(
            f"cannot inspect staged runtime {staging}: {error}"
        ) from error
    if staging_parent != root_path or not staging.name.startswith(".llm-hud-stage-"):
        raise RuntimeLayoutError(f"staged runtime is outside the install root: {staging}")
    if stat.S_ISLNK(staging_metadata.st_mode) or not stat.S_ISDIR(
        staging_metadata.st_mode
    ):
        raise RuntimeLayoutError(f"staged runtime is not a regular directory: {staging}")
    metadata = validate_staged_runtime(
        staging,
        version,
        expected_content_sha256=expected_content_sha256,
    )
    release_id = metadata.release_id
    _write_runtime_metadata(staging, metadata)
    _require_runtime_shape(staging, sealed=True)
    _fsync_runtime_tree(staging)
    destination = runtime_path(root, release_id)
    try:
        destination_metadata = destination.lstat()
    except FileNotFoundError:
        destination_metadata = None
    except OSError as error:
        raise RuntimeLayoutError(f"cannot inspect runtime destination: {error}") from error
    if destination_metadata is not None:
        existing = validate_runtime(root, release_id)
        if existing != metadata:
            raise RuntimeLayoutError(f"release id collision at {destination}")
        try:
            shutil.rmtree(staging)
        except OSError as error:
            raise RuntimeLayoutError(
                f"cannot remove duplicate staged runtime {staging}: {error}"
            ) from error
        _fsync_directory_required(root)
        return existing
    try:
        os.rename(staging, destination)
    except OSError as error:
        raise RuntimeLayoutError(
            f"cannot finalize runtime {staging} as {destination}: {error}"
        ) from error
    _fsync_directory_required(destination.parent)
    _fsync_directory_required(root)
    return validate_runtime(root, release_id)


def finalize_runtime(
    root: Path,
    staging: Path,
    version: str,
    *,
    expected_content_sha256: str | None = None,
    lock_timeout: float = 10.0,
) -> RuntimeMetadata:
    with RuntimeLock(root, timeout=lock_timeout):
        return _finalize_runtime_unlocked(
            root,
            staging,
            version,
            expected_content_sha256=expected_content_sha256,
        )


def install_staged_runtime(
    root: Path,
    staging: Path,
    version: str,
    *,
    expected_content_sha256: str | None = None,
    expected_active: str | None | object = _UNSET,
    lock_timeout: float = 10.0,
) -> tuple[RuntimeMetadata, Activation]:
    with RuntimeLock(root, timeout=lock_timeout):
        metadata = _finalize_runtime_unlocked(
            root,
            staging,
            version,
            expected_content_sha256=expected_content_sha256,
        )
        activation = _activate_unlocked(
            root, metadata.release_id, expected_active=expected_active
        )
        return metadata, activation


class RuntimeLock:
    def __init__(self, root: Path, timeout: float = 10.0) -> None:
        self.root = root
        self.timeout = timeout
        self._descriptor: int | None = None

    def __enter__(self) -> RuntimeLock:
        _require_install_ownership(self.root)
        path = self.root / LOCK_NAME
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as error:
            raise RuntimeLayoutError(f"cannot open update lock {path}: {error}") from error
        try:
            descriptor_metadata = os.fstat(descriptor)
            path_metadata = path.lstat()
        except OSError as error:
            os.close(descriptor)
            raise RuntimeLayoutError(f"cannot verify update lock {path}: {error}") from error
        if (
            not stat.S_ISREG(descriptor_metadata.st_mode)
            or stat.S_ISLNK(path_metadata.st_mode)
            or (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
            != (path_metadata.st_dev, path_metadata.st_ino)
        ):
            os.close(descriptor)
            raise RuntimeLayoutError(f"update lock is not a regular managed file: {path}")
        deadline = time.monotonic() + max(0.0, self.timeout)
        while True:
            try:
                try_lock_descriptor(descriptor)
                self._descriptor = descriptor
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(descriptor)
                    raise RuntimeLayoutError("another runtime operation is in progress")
                time.sleep(0.05)
            except OSError as error:
                os.close(descriptor)
                raise RuntimeLayoutError(
                    f"cannot lock runtime operations {path}: {error}"
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
