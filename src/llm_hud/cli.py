from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from llm_hud import __version__
from llm_hud.providers import provider_by_id, providers


PROVIDER_IDS = tuple(provider.id for provider in providers())


def _command_path() -> str:
    override = os.environ.get("LLM_HUD_COMMAND_PATH")
    if override:
        return str(Path(override).expanduser())
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
    return result.status != "error"


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
        ok = _print_result(provider.uninstall()) and ok
    return 0 if ok else 1


def _version(command: str) -> str:
    try:
        completed = subprocess.run(
            [command, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    output = completed.stdout.strip() or completed.stderr.strip()
    return output.splitlines()[0] if output else "unknown"


def command_doctor(_: argparse.Namespace) -> int:
    healthy = True
    for provider in providers():
        available = provider.available()
        configured, detail = provider.configured()
        if provider.capabilities.integration == "builtin":
            state = "built in" if configured else "not available"
        else:
            state = "configured" if configured else "not configured"
        installed = _version(provider.command) if available else "not installed"
        print(f"{provider.id}: {installed}; {state}; {detail}")
        if available and not configured:
            healthy = False
    return 0 if healthy else 1


def command_render(args: argparse.Namespace) -> int:
    provider = provider_by_id(args.provider)
    raw = sys.stdin.buffer.read()
    try:
        output = provider.render(raw, no_color=args.no_color)
    except NotImplementedError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(output)
    return 0


def command_providers(_: argparse.Namespace) -> int:
    for provider in providers():
        state = "detected" if provider.available() else "not detected"
        print(f"{provider.id}\t{state}\t{provider.capabilities.integration}")
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
    uninstall.set_defaults(handler=command_uninstall)

    doctor = subparsers.add_parser("doctor", help="check provider integrations")
    doctor.set_defaults(handler=command_doctor)

    render_parser = subparsers.add_parser("render", help="render a provider HUD")
    render_parser.add_argument("provider", choices=PROVIDER_IDS)
    render_parser.add_argument("--no-color", action="store_true")
    render_parser.set_defaults(handler=command_render)

    providers_parser = subparsers.add_parser("providers", help="list supported providers")
    providers_parser.set_defaults(handler=command_providers)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.handler(args))
