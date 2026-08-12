#!/usr/bin/env python3
"""Measure per-tick `render claude` latency of an installed launcher.

Usage:
    python3 scripts/bench_render.py /path/to/bin/llm-hud

Produce an isolated install first, e.g.:
    LLM_HUD_INSTALL_DIR=/tmp/hud/root LLM_HUD_BIN_DIR=/tmp/hud/bin \
    LLM_HUD_STATE_DIR=/tmp/hud/state \
    LLM_HUD_CLAUDE_SETTINGS=/tmp/hud/claude.json \
    LLM_HUD_CODEX_CONFIG=/tmp/hud/codex.toml \
    LLM_HUD_CLAUDE_BIN=1 ./install.sh

Baseline (2026-08, Apple Silicon, APFS, warm cache): ~38 ms per tick, of
which the control layer's full runtime digest costs ~1.7 ms and interpreter
startup ~7.5 ms; the rest is module imports. That measurement is why dispatch
still verifies the full digest: skipping it would require a stable-protocol
revision to save under 5% of the tick. Re-measure here before revisiting.
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    launcher = sys.argv[1]
    payload = json.dumps(
        {
            "model": {"display_name": "Opus"},
            "rate_limits": {
                "five_hour": {"used_percentage": 24},
                "seven_day": {"used_percentage": 41},
            },
        }
    ).encode("utf-8")
    times: list[float] = []
    for _ in range(17):
        start = time.perf_counter()
        subprocess.run(
            [launcher, "render", "claude"],
            input=payload,
            stdout=subprocess.DEVNULL,
            check=True,
        )
        times.append((time.perf_counter() - start) * 1000.0)
    warm = times[2:]
    print(
        f"render claude per tick over {len(warm)} runs: "
        f"median {statistics.median(warm):.1f} ms, "
        f"min {min(warm):.1f} ms, max {max(warm):.1f} ms"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
