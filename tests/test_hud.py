from __future__ import annotations

import unittest

from llm_hud.hud import HudSnapshot, UsageWindow, render_hud
from tests.support import Environment


class HudTests(unittest.TestCase):
    def test_renders_graphical_usage_windows(self):
        snapshot = HudSnapshot(
            provider="Claude",
            model="Opus",
            cwd="/home/example/Desktop/llm-hud",
            windows=(UsageWindow("5h", 76), UsageWindow("7d", 59)),
        )
        with Environment(LLM_HUD_HOME="/home/example"):
            self.assertEqual(
                render_hud(snapshot, color=False),
                "Claude · Opus · ~/Desktop/llm-hud\n"
                "5h  ████████░░   76%    7d  ██████░░░░   59%",
            )

    def test_windows_without_data_render_placeholder_bars(self):
        snapshot = HudSnapshot(
            provider="Claude",
            model="Sonnet",
            windows=(UsageWindow("5h"), UsageWindow("7d")),
        )
        self.assertEqual(
            render_hud(snapshot, color=False),
            "Claude · Sonnet\n"
            "5h  ░░░░░░░░░░    --    7d  ░░░░░░░░░░    --",
        )

    def test_no_windows_renders_the_header_only(self):
        snapshot = HudSnapshot(provider="Claude", model="Sonnet")
        self.assertEqual(render_hud(snapshot, color=False), "Claude · Sonnet")

    def test_narrow_terminals_put_each_window_on_its_own_line(self):
        snapshot = HudSnapshot(
            provider="Claude",
            windows=(UsageWindow("5h", 76), UsageWindow("7d", 59)),
            columns=30,
        )
        self.assertEqual(
            render_hud(snapshot, color=False).splitlines(),
            ["Claude", "5h  ████████░░   76%", "7d  ██████░░░░   59%"],
        )


if __name__ == "__main__":
    unittest.main()
