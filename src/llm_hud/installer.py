from __future__ import annotations

import argparse
import hashlib
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

from llm_hud._platform import set_descriptor_mode
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
LAUNCHER_STATE_SCHEMA = 1
DISPATCHER_SOURCE = Path("scripts") / "llm-hud-dispatcher"
CONTROL_SOURCE = Path("scripts") / "runtime_control.py"
CONTROL_DESTINATION = Path("control") / "runtime_control.py"
DISPATCHER_DESTINATION = Path("bin") / "llm-hud"
MANAGED_LAUNCHER_MARKER = "# llm-hud-managed-launcher-v1"
MAX_LAUNCHER_SIZE = 64 * 1024
LAUNCHER_TEMP_PREFIX = ".llm-hud-launcher-"


@dataclass(frozen=True)
class StableTools:
    dispatcher: str
    control: str
    dispatcher_sha256: str
    control_sha256: str


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
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    except OSError as error:
        raise RuntimeLayoutError(
            f"cannot write install marker {marker}: {error}"
        ) from error
    finally:
        os.close(descriptor)
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


def _read_launcher_content(path: Path) -> bytes | None:
    """Read an existing external launcher; None when absent."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RuntimeLayoutError(
            f"cannot inspect external launcher {path}: {error}"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeLayoutError(f"external launcher is not a regular file: {path}")
    if metadata.st_size > MAX_LAUNCHER_SIZE:
        raise RuntimeLayoutError(f"external launcher is too large: {path}")
    try:
        return path.read_bytes()
    except OSError as error:
        raise RuntimeLayoutError(
            f"cannot read external launcher {path}: {error}"
        ) from error


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


def _write_launcher_file(launcher: Path, content: bytes) -> None:
    _require_regular_directory(launcher.parent, "external launcher directory")
    descriptor, temp_name = tempfile.mkstemp(
        prefix=LAUNCHER_TEMP_PREFIX,
        dir=launcher.parent,
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            set_descriptor_mode(handle.fileno(), 0o755)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temp, launcher)
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
    for description, text in (
        ("stable dispatcher", tools.dispatcher),
        ("stable runtime control", tools.control),
    ):
        try:
            compile(text, description, "exec", dont_inherit=True)
        except (SyntaxError, ValueError) as error:
            raise RuntimeLayoutError(f"invalid {description}: {error}") from error
    return tools


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


def _install_stable_tools_unlocked(
    root: Path,
    tools: StableTools,
) -> None:
    _require_regular_directory(root / "bin", "stable bin directory")
    _require_regular_directory(root / "control", "stable control directory")
    try:
        control_path = root / CONTROL_DESTINATION
        dispatcher_path = root / DISPATCHER_DESTINATION
        _sweep_tool_temps(control_path.parent, CONTROL_DESTINATION.name)
        _sweep_tool_temps(dispatcher_path.parent, DISPATCHER_DESTINATION.name)
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
        # 0.1.x recorded the frozen protocol hashes here; the tools are now
        # simply refreshed with every install.
        (root / STABLE_STATE_NAME).unlink(missing_ok=True)
    except OSError as error:
        raise RuntimeLayoutError(f"cannot install stable runtime tools: {error}") from error


def install_stable_tools(
    source: Path,
    root: Path,
    *,
    expected_active: str,
) -> None:
    source = Path(source)
    root = Path(root).resolve(strict=True)
    tools = load_stable_tools(source)
    with RuntimeLock(root):
        current = read_activation(root)
        actual_active = current.active if current is not None else None
        if actual_active != expected_active:
            raise RuntimeLayoutError(
                f"active runtime changed from {expected_active!r} to {actual_active!r}"
            )
        _install_stable_tools_unlocked(root, tools)


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
    _preflight_external_launcher(root, launcher)
    with RuntimeLock(root):
        current = read_activation(root)
        actual_active = current.active if current is not None else None
        if actual_active != expected_active:
            raise RuntimeLayoutError(
                f"active runtime changed from {expected_active!r} to {actual_active!r}"
            )
        _preflight_external_launcher(root, launcher)
        try:
            _write_launcher_file(launcher, candidate)
            # A plain pointer so a managed dispatch can find the PATH-visible
            # launcher (cli._managed_launcher_path); 0.1.x files carried more
            # fields, which readers ignore.
            atomic_write_json(
                root / LAUNCHER_STATE_NAME,
                {
                    "schema": LAUNCHER_STATE_SCHEMA,
                    "launcher_path": str(launcher),
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
) -> tuple[RuntimeMetadata, Activation]:
    source = Path(source)
    root = Path(root)
    tools = load_stable_tools(source)
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
) -> tuple[RuntimeMetadata, Activation]:
    source = Path(source)
    root = Path(root)
    launcher = Path(launcher)
    python = Path(python)
    _preflight_external_launcher(root, launcher)
    activation_before = _read_existing_activation(root)
    metadata, activation = install_versioned_runtime(
        source,
        root,
        python,
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
