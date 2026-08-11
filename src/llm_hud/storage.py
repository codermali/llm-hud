from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any


class StateFileError(ValueError):
    """An installation state file is present but unsafe to use."""


def read_provider_state(
    path: Path, *, supported_schemas: frozenset[int]
) -> dict[str, Any] | None:
    """Read provider state without treating corruption as an empty state."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as error:
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


def validate_state_path(state: dict[str, Any], key: str, current: Path) -> None:
    """Reject state belonging to a different provider configuration file."""
    saved = state.get(key)
    if not isinstance(saved, str) or not saved:
        raise StateFileError(f"installation state has no valid {key}")
    try:
        saved_path = Path(saved).expanduser().resolve(strict=False)
        current_path = current.expanduser().resolve(strict=False)
    except (OSError, ValueError, RuntimeError) as error:
        raise StateFileError(f"cannot resolve installation state {key}: {error}") from error
    if saved_path != current_path:
        raise StateFileError(
            f"installation state targets {saved_path}, not current path {current_path}"
        )


def atomic_write_text(path: Path, content: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode: int | None = None
    try:
        existing_mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        pass

    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode if mode is not None else existing_mode or 0o600)
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass


def atomic_write_json(path: Path, payload: Any, mode: int | None = 0o600) -> None:
    """Write JSON atomically; mode=None keeps the file's existing permissions."""
    atomic_write_text(path, json.dumps(payload, indent=2) + "\n", mode=mode)
