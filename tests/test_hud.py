from __future__ import annotations

import math
import unittest
from datetime import datetime
from unittest import mock

from llm_hud.hud import (
    ANSI_RE,
    STALE_AFTER_SECONDS,
    HudSnapshot,
    UsageWindow,
    render_hud,
)
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

    def test_bar_color_agrees_with_the_displayed_percent_at_thresholds(self):
        # The color follows the rounded label: "70% used" is never red and
        # "71% used" always is; likewise for green/yellow at 40.
        cases = (
            (40.4, "32", " 40%"),
            (40.6, "33", " 41%"),
            (70.4, "33", " 70%"),
            (70.6, "31", " 71%"),
        )
        for used, tone, label in cases:
            with self.subTest(used=used):
                snapshot = HudSnapshot(
                    provider="Claude", windows=(UsageWindow("5h", used),)
                )
                rendered = render_hud(snapshot, color=True)
                self.assertIn(f"\x1b[{tone}m{label}\x1b[0m", rendered)
                self.assertIn(f"\x1b[{tone}m█", rendered)

    def test_nan_usage_degrades_to_missing_data_instead_of_crashing(self):
        snapshot = HudSnapshot(
            provider="Claude",
            windows=(UsageWindow("5h", math.nan), UsageWindow("7d", 41)),
        )
        self.assertEqual(
            render_hud(snapshot, color=False),
            "Claude\n5h  ░░░░░░░░░░    --    7d  ████░░░░░░   41% used",
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

    def test_fresh_observations_render_unchanged(self):
        snapshot = HudSnapshot(
            provider="Claude",
            windows=(UsageWindow("5h", 76, age_seconds=STALE_AFTER_SECONDS - 1),),
        )
        self.assertEqual(
            render_hud(snapshot, color=False),
            "Claude\n5h  ████████░░   76% used",
        )

    def test_stale_observations_are_marked_with_their_age(self):
        snapshot = HudSnapshot(
            provider="Claude",
            windows=(UsageWindow("5h", 76, age_seconds=8 * 60),),
        )
        self.assertEqual(
            render_hud(snapshot, color=False),
            "Claude\n5h  ████████░░   76%  ·8m",
        )

    def test_stale_observations_drop_the_usage_color(self):
        window = UsageWindow("5h", 76, age_seconds=8 * 60)
        rendered = render_hud(HudSnapshot(provider="Claude", windows=(window,)))
        # 31 is the over-70% red the same window earns while it is current.
        self.assertNotIn("\033[31m", rendered)
        fresh = render_hud(
            HudSnapshot(provider="Claude", windows=(UsageWindow("5h", 76),))
        )
        self.assertIn("\033[31m", fresh)

    def test_age_marker_uses_one_unit(self):
        for seconds, expected in (
            (STALE_AFTER_SECONDS, "·2m"),
            (59 * 60, "·59m"),
            (60 * 60, "·1h"),
            (23 * 3600, "·23h"),
            (50 * 3600, "·2d"),
        ):
            with self.subTest(seconds=seconds):
                snapshot = HudSnapshot(
                    provider="Claude",
                    windows=(UsageWindow("5h", 76, age_seconds=seconds),),
                )
                self.assertIn(expected, render_hud(snapshot, color=False))

    def test_window_past_its_reset_time_reports_missing_data(self):
        snapshot = HudSnapshot(
            provider="Claude",
            windows=(UsageWindow("5h", 76, resets_at=1000.0, age_seconds=9999.0),),
        )
        with mock.patch("llm_hud.hud._now", return_value=1001.0):
            rendered = render_hud(snapshot, color=False)
        self.assertEqual(rendered, "Claude\n5h  ░░░░░░░░░░    --  ↻ pending")

    def test_window_before_its_reset_time_keeps_its_usage(self):
        snapshot = HudSnapshot(
            provider="Claude", windows=(UsageWindow("5h", 76, resets_at=1000.0),)
        )
        with mock.patch("llm_hud.hud._now", return_value=999.0):
            rendered = render_hud(snapshot, color=False)
        self.assertIn("76% used", rendered)
        self.assertNotIn("pending", rendered)

    def test_narrowing_sheds_fields_before_it_spends_lines(self):
        stamp = 1900000000.0
        windows = (
            UsageWindow("5h", 76, resets_at=stamp, age_seconds=8 * 60),
            UsageWindow("7d", 59, resets_at=stamp, age_seconds=8 * 60),
        )

        def rendered(columns):
            return render_hud(
                HudSnapshot(provider="Claude", windows=windows, columns=columns),
                color=False,
            )

        full = rendered(76)
        self.assertEqual(len(full.split("\n")), 2)
        self.assertIn("↻", full)
        self.assertIn("·8m", full)

        # The reset time goes first; the marker saying the number may be stale
        # is worth more than the timestamp beside it.
        without_reset = rendered(60)
        self.assertEqual(len(without_reset.split("\n")), 2)
        self.assertNotIn("↻", without_reset)
        self.assertIn("·8m", without_reset)

        without_age = rendered(50)
        self.assertEqual(len(without_age.split("\n")), 2)
        self.assertNotIn("·8m", without_age)

        # Only once nothing is left to shed does each window take a line, and
        # a line with room for every field gets them back.
        wrapped = rendered(40)
        self.assertEqual(len(wrapped.split("\n")), 3)
        self.assertIn("↻", wrapped)
        self.assertIn("·8m", wrapped)

        # A line too narrow even for that sheds again rather than letting the
        # terminal wrap it a second time.
        cramped = rendered(30)
        self.assertEqual(len(cramped.split("\n")), 3)
        self.assertNotIn("↻", cramped)
        self.assertIn("·8m", cramped)

    def test_a_reset_window_drops_its_pending_marker_when_narrowed(self):
        snapshot = HudSnapshot(
            provider="Claude",
            windows=(UsageWindow("5h", 76, resets_at=1000.0),),
            columns=25,
        )
        with mock.patch("llm_hud.hud._now", return_value=1001.0):
            self.assertEqual(
                render_hud(snapshot, color=False), "Claude\n5h  ░░░░░░░░░░    --"
            )

    def test_narrow_terminals_drop_the_age_marker_before_wrapping(self):
        windows = (
            UsageWindow("5h", 76, age_seconds=8 * 60),
            UsageWindow("7d", 59, age_seconds=8 * 60),
        )
        wide = render_hud(HudSnapshot(provider="Claude", windows=windows, columns=80))
        self.assertIn("·8m", wide)
        self.assertEqual(len(wide.split("\n")), 2)

        # One column short of the marked row: the ages go, the single row stays.
        narrow = render_hud(
            HudSnapshot(provider="Claude", windows=windows, columns=51)
        )
        self.assertNotIn("·8m", narrow)
        self.assertEqual(len(narrow.split("\n")), 2)

        # Too narrow even without them, so each window earns its own line.
        cramped = render_hud(
            HudSnapshot(provider="Claude", windows=windows, columns=30)
        )
        self.assertEqual(len(cramped.split("\n")), 3)
        self.assertIn("·8m", cramped)

    def test_effort_qualifies_the_model_without_its_own_separator(self):
        snapshot = HudSnapshot(
            provider="Claude", model="Opus", effort="high", cwd="/home/example/p"
        )
        with Environment(LLM_HUD_HOME="/home/example"):
            self.assertEqual(render_hud(snapshot, color=False), "Claude · Opus high · ~/p")

    def test_effort_without_a_model_is_not_shown(self):
        snapshot = HudSnapshot(provider="Claude", effort="high")
        self.assertEqual(render_hud(snapshot, color=False), "Claude")

    def test_effort_widens_the_prefix_the_path_is_truncated_against(self):
        without = HudSnapshot(
            provider="Claude", model="Opus", cwd="/very/long/path/to/a/project", columns=40
        )
        with_effort = HudSnapshot(
            provider="Claude",
            model="Opus",
            effort="high",
            cwd="/very/long/path/to/a/project",
            columns=40,
        )
        for snapshot in (without, with_effort):
            rendered = render_hud(snapshot, color=False)
            self.assertLessEqual(len(rendered), 40)
        self.assertLess(
            len(render_hud(with_effort, color=False).split(" · ")[-1]),
            len(render_hud(without, color=False).split(" · ")[-1]),
        )

    def test_window_labels_align_to_the_widest_label(self):
        snapshot = HudSnapshot(
            provider="Claude",
            windows=(UsageWindow("5h", 76), UsageWindow("ctx", 18)),
        )
        rendered = render_hud(snapshot, color=False)
        self.assertIn("5h   ███", rendered)
        self.assertIn("ctx  ██░", rendered)

    def test_a_wrapped_window_still_fits_the_terminal(self):
        stamp = 1900000000.0
        windows = (
            UsageWindow("5h", 76, resets_at=stamp, age_seconds=8 * 60),
            UsageWindow("7d", 59, resets_at=stamp, age_seconds=8 * 60),
        )
        for columns in range(22, 46):
            with self.subTest(columns=columns):
                rendered = render_hud(
                    HudSnapshot(provider="Claude", windows=windows, columns=columns),
                    color=False,
                )
                for line in rendered.split("\n")[1:]:
                    self.assertLessEqual(len(line), columns)


if __name__ == "__main__":
    unittest.main()
