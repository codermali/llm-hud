from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from llm_hud import __version__
from llm_hud.providers import (
    PROVIDER_IDS,
    RENDER_PROVIDER_IDS,
    provider_by_id,
    providers,
)


# Keep in sync with llm_hud.runtime.LAUNCHER_STATE_NAME (pinned by a test);
# importing the runtime module here would slow every render tick.
_LAUNCHER_STATE_NAME = ".llm-hud-launcher-state.json"


def _managed_launcher_path() -> str | None:
    """The external launcher recorded for a managed runtime dispatch.

    Under the versioned layout the dispatcher sets argv[0] to
    <root>/bin/llm-hud, which is not on PATH; the launcher state file next to
    it names the PATH-visible launcher that provider configs should record.
    """
    argv0 = Path(sys.argv[0])
    if argv0.name != "llm-hud":
        return None
    state_path = argv0.parent.parent / _LAUNCHER_STATE_NAME
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    launcher = payload.get("launcher_path") if isinstance(payload, dict) else None
    if not isinstance(launcher, str) or not launcher:
        return None
    path = Path(launcher)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        return None
    return str(path)


def _command_path() -> str:
    override = os.environ.get("LLM_HUD_COMMAND_PATH")
    if override:
        return str(Path(override).expanduser())
    managed = _managed_launcher_path()
    if managed:
        return managed
    installed = shutil.which("llm-hud")
    if installed:
        return installed
    return str(Path(sys.argv[0]).expanduser().resolve())


def _selected(value: str, include_unavailable: bool = False):
    selected = providers() if value == "all" else [provider_by_id(value)]
    if include_unavailable or value != "all":
        return selected
    return [provider for provider in selected if provider.available()]


def _print_result(result) -> bool:
    print(f"[{result.status}] {result.provider}: {result.message}")
    return not result.failed


def command_install(args: argparse.Namespace) -> int:
    selected = _selected(args.provider)
    if not selected:
        print("No supported coding-agent CLI was detected.")
        return 1
    ok = True
    for provider in selected:
        if not provider.available():
            print(f"note: the {provider.id} CLI was not detected; configuring it anyway")
        ok = _print_result(provider.install(_command_path())) and ok
    return 0 if ok else 1


def command_uninstall(args: argparse.Namespace) -> int:
    ok = True
    for provider in _selected(args.provider, include_unavailable=True):
        result = provider.forget() if args.forget else provider.uninstall()
        ok = _print_result(result) and ok
    return 0 if ok else 1


def _probe_version(command: str) -> tuple[str, bool, str | None]:
    """Return the version, whether the CLI answered, and a failure detail."""
    try:
        completed = subprocess.run(
            [command, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "unknown", False, "version probe timed out"
    except OSError:
        return "unknown", False, "version probe failed"
    output = completed.stdout.strip() or completed.stderr.strip()
    version = output.splitlines()[0] if output else "unknown"
    if completed.returncode != 0:
        return version, False, f"version probe exited {completed.returncode}"
    if not output:
        return version, False, "version probe returned no output"
    return version, True, None


def command_doctor(_: argparse.Namespace) -> int:
    healthy = True
    for provider in providers():
        executable = provider.executable()
        available = executable is not None
        configured, detail = provider.configured()
        if provider.capabilities.integration == "builtin":
            state = "built in" if configured else "not available"
        else:
            state = "configured" if configured else "not configured"
        if executable is None:
            installed, executable_healthy = "not installed", False
        else:
            installed, executable_healthy, probe_error = _probe_version(executable)
            if probe_error:
                installed = f"{installed} ({probe_error})"
        print(f"{provider.id}: {installed}; {state}; {detail}")
        if configured and not available:
            healthy = False
        elif available and (not configured or not executable_healthy):
            healthy = False
    return 0 if healthy else 1


def command_render(args: argparse.Namespace) -> int:
    provider = provider_by_id(args.provider)
    raw = sys.stdin.buffer.read()
    output = provider.render(raw, no_color=args.no_color)
    print(output)
    return 0


def command_providers(_: argparse.Namespace) -> int:
    for provider in providers():
        state = "detected" if provider.available() else "not detected"
        capabilities = provider.capabilities
        persistent = ",".join(capabilities.persistent_metrics) or "-"
        on_demand = ",".join(capabilities.on_demand_metrics) or "-"
        print(
            f"{provider.id}\t{state}\t{capabilities.integration}"
            f"\t{persistent}\t{on_demand}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm-hud",
        description="Configure a compact HUD in supported coding-agent CLIs.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    install = subparsers.add_parser("install", help="configure detected agent CLIs")
    install.add_argument("--provider", choices=("all", *PROVIDER_IDS), default="all")
    install.set_defaults(handler=command_install)

    uninstall = subparsers.add_parser("uninstall", help="restore agent HUD settings")
    uninstall.add_argument("--provider", choices=("all", *PROVIDER_IDS), default="all")
    uninstall.add_argument(
        "--forget",
        action="store_true",
        help="abandon saved installation state without touching configs",
    )
    uninstall.set_defaults(handler=command_uninstall)

    doctor = subparsers.add_parser("doctor", help="check provider integrations")
    doctor.set_defaults(handler=command_doctor)

    render_parser = subparsers.add_parser("render", help="render a provider HUD")
    render_parser.add_argument("provider", choices=RENDER_PROVIDER_IDS)
    render_parser.add_argument("--no-color", action="store_true")
    render_parser.set_defaults(handler=command_render)

    providers_parser = subparsers.add_parser("providers", help="list supported providers")
    providers_parser.set_defaults(handler=command_providers)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.handler(args))
