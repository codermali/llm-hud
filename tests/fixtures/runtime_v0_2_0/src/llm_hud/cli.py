from __future__ import annotations

import sys


def main() -> int:
    if sys.argv[1:] == ["--version"]:
        print("llm-hud 0.2.0")
        return 0
    print("v0.2.0 fixture")
    return 0
