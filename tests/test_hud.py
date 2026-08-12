from __future__ import annotations

import unittest
from datetime import datetime
from unittest import mock

from llm_hud.hud import ANSI_RE, HudSnapshot, UsageWindow, render_hud
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
                "5h  ████████░░   76% used    7d  ██████░░░░   59% used",
            )

    def test_explicit_home_does_not_use_host_path_semantics(self):
        snapshot = HudSnapshot(provider="Claude", cwd="/explicit/home/project")
        with (
            Environment(LLM_HUD_HOME="/explicit/home"),
            mock.patch(
                "llm_hud.hud.home_dir",
                side_effect=AssertionError("explicit home must retain its syntax"),
            ),
        ):
            self.assertEqual(render_hud(snapshot, color=False), "Claude · ~/project")

    def test_windows_path_inside_home_uses_forward_slashes(self):
        snapshot = HudSnapshot(
            provider="Claude", cwd=r"C:\Users\Example\Desktop\llm-hud"
        )
        with Environment(LLM_HUD_HOME=r"c:\users\example"):
            self.assertEqual(
                render_hud(snapshot, color=False),
                "Claude · ~/Desktop/llm-hud",
            )

    def test_windows_path_outside_home_keeps_drive_and_uses_forward_slashes(self):
        snapshot = HudSnapshot(provider="Claude", cwd=r"D:\work\llm-hud")
        with Environment(LLM_HUD_HOME=r"C:\Users\Example"):
            self.assertEqual(
                render_hud(snapshot, color=False),
                "Claude · D:/work/llm-hud",
            )

    def test_windows_unc_paths_are_compacted_and_normalized(self):
        snapshot = HudSnapshot(
            provider="Claude", cwd=r"\\server\share\Users\Example\llm-hud"
        )
        with Environment(LLM_HUD_HOME=r"\\server\share\Users\Example"):
            self.assertEqual(render_hud(snapshot, color=False), "Claude · ~/llm-hud")

        snapshot = HudSnapshot(provider="Claude", cwd=r"\\server\other\llm-hud")
        with Environment(LLM_HUD_HOME=r"\\server\share\Users\Example"):
            self.assertEqual(
                render_hud(snapshot, color=False),
                "Claude · //server/other/llm-hud",
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
            ["Claude", "5h  ████████░░   76% used", "7d  ██████░░░░   59% used"],
        )

    def test_color_output_wraps_fields_in_balanced_ansi_codes(self):
        snapshot = HudSnapshot(
            provider="Claude",
            model="Opus",
            windows=(UsageWindow("5h", 76), UsageWindow("7d", 12)),
        )
        colored = render_hud(snapshot, color=True)
        self.assertIn("\x1b[1;38;5;208mClaude\x1b[0m", colored)
        self.assertIn("\x1b[32m", colored)  # <= 40% used is green
        self.assertIn("\x1b[31m", colored)  # > 70% used is red
        resets = colored.count("\x1b[0m")
        self.assertEqual(colored.count("\x1b["), resets * 2)
        # Stripping the codes yields exactly the uncolored rendering.
        self.assertEqual(
            ANSI_RE.sub("", colored), render_hud(snapshot, color=False)
        )

    def test_reset_times_render_per_window_format(self):
        stamp = 1900000000.0
        local = datetime.fromtimestamp(stamp).astimezone()
        snapshot = HudSnapshot(
            provider="Claude",
            windows=(
                UsageWindow("5h", 76, resets_at=stamp),
                UsageWindow("7d", 59, resets_at=stamp),
            ),
        )
        rendered = render_hud(snapshot, color=False)
        self.assertIn(f"↻ {local.strftime('%H:%M')}", rendered)
        self.assertIn(f"↻ {local.strftime('%a %H:%M')}", rendered)

    def test_invalid_reset_timestamp_is_omitted(self):
        snapshot = HudSnapshot(
            provider="Claude",
            windows=(UsageWindow("5h", 76, resets_at=1e300),),
        )
        self.assertNotIn("↻", render_hud(snapshot, color=False))

    def test_control_characters_are_stripped_from_upstream_fields(self):
        snapshot = HudSnapshot(
            provider="Claude",
            model="Opus\x1b]0;pwned\x07",
            cwd="/tmp/dir\nname",
        )
        self.assertEqual(
            render_hud(snapshot, color=False), "Claude · Opus]0;pwned · /tmp/dirname"
        )

    def test_wide_characters_count_as_two_columns(self):
        # 10 CJK chars = 20 display columns; len() would count 10.
        snapshot = HudSnapshot(provider="Claude", cwd="/" + "中" * 10, columns=24)
        rendered = render_hud(snapshot, color=False)
        self.assertTrue(rendered.startswith("Claude · …"))
        self.assertIn("中", rendered)

    def test_long_paths_are_truncated_keeping_the_tail(self):
        snapshot = HudSnapshot(
            provider="Claude",
            model="Opus",
            cwd="/very/long/path/to/some/deeply/nested/project",
            columns=40,
        )
        rendered = render_hud(snapshot, color=False)
        self.assertEqual(rendered, "Claude · Opus · …e/deeply/nested/project")
        self.assertLessEqual(len(rendered), 40)


if __name__ == "__main__":
    unittest.main()
