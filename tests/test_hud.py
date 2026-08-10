from __future__ import annotations

import unittest

from llm_hud.hud import HudSnapshot, UsageWindow, render_hud
from tests.support import Environment


class HudTests(unittest.TestCase):
    def test_renders_graphical_usage_windows(self):
        snapshot = HudSnapshot(
            provider="Claude",
            model="Opus",
            cwd="/Users/mali/Desktop/llm-hud",
            windows=(UsageWindow("5h", 76), UsageWindow("7d", 59)),
        )
        with Environment(LLM_HUD_HOME="/Users/mali"):
            self.assertEqual(
                render_hud(snapshot, color=False),
                "Claude · Opus · ~/Desktop/llm-hud\n"
                "5h  ████████░░   76%    7d  ██████░░░░   59%",
            )

    def test_pending_state_is_explicit(self):
        snapshot = HudSnapshot(
            provider="Claude",
            model="Sonnet",
            windows=(UsageWindow("5h"), UsageWindow("7d")),
        )
        self.assertEqual(
            render_hud(snapshot, color=False),
            "Claude · Sonnet\n"
            "5h  ░░░░░░░░░░    --    7d  ░░░░░░░░░░    --   "
            "waiting for first response",
        )

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
