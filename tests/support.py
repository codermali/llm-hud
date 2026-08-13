from __future__ import annotations

import os
from pathlib import Path


_UNSET = object()


class Environment:
    """Set environment variables for a block; a value of None unsets one."""

    def __init__(self, **values: str | None):
        self.values = values
        self.previous: dict[str, str | None] = {}

    def __enter__(self):
        for key, value in self.values.items():
            self.previous[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return self

    def __exit__(self, exc_type, exc, traceback):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def activate(
    root: Path,
    release_id: str,
    *,
    expected_active: str | None | object = _UNSET,
    lock_timeout: float = 10.0,
):
    """Test adapter for the installer's lock-scoped activation primitive."""
    import llm_hud.runtime as runtime

    with runtime.RuntimeLock(root, timeout=lock_timeout):
        if expected_active is _UNSET:
            return runtime._activate_with_replaced_unlocked(root, release_id)[0]
        return runtime._activate_with_replaced_unlocked(
            root,
            release_id,
            expected_active=expected_active,
        )[0]


def activate_with_replaced(
    root: Path,
    release_id: str,
    *,
    expected_active: str | None | object = _UNSET,
    lock_timeout: float = 10.0,
):
    """Test adapter exposing the prior record used for install recovery."""
    import llm_hud.runtime as runtime

    with runtime.RuntimeLock(root, timeout=lock_timeout):
        if expected_active is _UNSET:
            return runtime._activate_with_replaced_unlocked(root, release_id)
        return runtime._activate_with_replaced_unlocked(
            root,
            release_id,
            expected_active=expected_active,
        )


def restore_activation(
    root: Path,
    replaced_activation,
    *,
    expected_active: str,
    lock_timeout: float = 10.0,
):
    import llm_hud.runtime as runtime

    runtime.validate_release_id(expected_active)
    with runtime.RuntimeLock(root, timeout=lock_timeout):
        return runtime._restore_activation_unlocked(
            root,
            replaced_activation,
            expected_active=expected_active,
        )


def clear_activation(
    root: Path,
    *,
    expected_active: str,
    lock_timeout: float = 10.0,
) -> None:
    import llm_hud.runtime as runtime

    runtime.validate_release_id(expected_active)
    with runtime.RuntimeLock(root, timeout=lock_timeout):
        runtime._clear_activation_unlocked(root, expected_active=expected_active)


def finalize_runtime(
    root: Path,
    staging: Path,
    version: str,
    *,
    expected_content_sha256: str | None = None,
    lock_timeout: float = 10.0,
):
    import llm_hud.runtime as runtime

    with runtime.RuntimeLock(root, timeout=lock_timeout):
        return runtime._finalize_runtime_unlocked(
            root,
            staging,
            version,
            expected_content_sha256=expected_content_sha256,
        )


def install_stable_tools(
    source_or_tools,
    root: Path,
    *,
    expected_active: str,
) -> None:
    import llm_hud.installer as installer

    root = Path(root).resolve(strict=True)
    tools = (
        installer._validate_stable_tools(source_or_tools)
        if isinstance(source_or_tools, installer.StableTools)
        else installer.load_stable_tools(Path(source_or_tools))
    )
    with installer.RuntimeLock(root):
        current = installer.read_activation(root)
        actual_active = current.active if current is not None else None
        if actual_active != expected_active:
            raise installer.RuntimeLayoutError(
                f"active runtime changed from {expected_active!r} "
                f"to {actual_active!r}"
            )
        installer._install_stable_tools_unlocked(root, tools)


def install_external_launcher(
    root: Path,
    launcher: Path,
    python: Path,
    *,
    expected_active: str,
) -> None:
    import llm_hud.installer as installer

    root = Path(root).resolve(strict=True)
    launcher = installer._canonical_file_path(Path(launcher), "external launcher")
    python = installer._validate_launcher_path(Path(python), "Python interpreter")
    installer._preflight_external_launcher(root, launcher)
    with installer.RuntimeLock(root):
        installer._install_external_launcher_unlocked(
            root,
            launcher,
            python,
            expected_active=expected_active,
        )


def install_runtime_from_source(
    source: Path,
    root: Path,
    python: Path | None = None,
    *,
    stable_control: str | None = None,
):
    import llm_hud.installer as installer

    metadata, activation, _ = installer._install_runtime_from_source(
        source,
        root,
        python,
        stable_control=stable_control,
    )
    return metadata, activation


def install_versioned_runtime(
    source: Path,
    root: Path,
    python: Path | None = None,
):
    import llm_hud.installer as installer

    metadata, activation, _ = installer._install_versioned_runtime(
        source,
        root,
        python,
    )
    return metadata, activation
