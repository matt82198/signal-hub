"""Tests for signal_hub.due -- game-window-aware per-source cadence (design 2c)."""
import unittest
from datetime import datetime, timedelta, timezone

from signal_hub import due

UTC = timezone.utc
# Sunday slate: kickoffs 17:00Z and 20:25Z on 2026-09-13.
K1 = datetime(2026, 9, 13, 17, 0, 0, tzinfo=UTC)
K2 = datetime(2026, 9, 13, 20, 25, 0, tzinfo=UTC)
KICKOFFS = [K1, K2]


class TestGameWindow(unittest.TestCase):
    def test_window_bounds(self):
        now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
        win = due.game_window(KICKOFFS, now)
        self.assertEqual(win, (K1 - timedelta(minutes=15), K2 + timedelta(hours=4)))

    def test_no_kickoffs_today_is_none(self):
        now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)  # Monday
        self.assertIsNone(due.game_window(KICKOFFS, now))

    def test_other_day_kickoffs_filtered_out(self):
        saturday = datetime(2026, 9, 12, 18, 0, 0, tzinfo=UTC)
        other = [datetime(2026, 9, 12, 18, 30, 0, tzinfo=UTC)] + KICKOFFS
        win = due.game_window(other, saturday)
        self.assertEqual(
            win,
            (
                datetime(2026, 9, 12, 18, 15, 0, tzinfo=UTC),
                datetime(2026, 9, 12, 22, 30, 0, tzinfo=UTC),
            ),
        )

    def test_empty_kickoff_list_is_none(self):
        now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
        self.assertIsNone(due.game_window([], now))


class TestInGameWindow(unittest.TestCase):
    def setUp(self):
        self.win = (K1 - timedelta(minutes=15), K2 + timedelta(hours=4))

    def test_inside(self):
        self.assertTrue(due.in_game_window(K1, self.win))

    def test_boundaries_inclusive(self):
        self.assertTrue(due.in_game_window(self.win[0], self.win))
        self.assertTrue(due.in_game_window(self.win[1], self.win))

    def test_outside(self):
        before = self.win[0] - timedelta(minutes=1)
        after = self.win[1] + timedelta(minutes=1)
        self.assertFalse(due.in_game_window(before, self.win))
        self.assertFalse(due.in_game_window(after, self.win))

    def test_none_window_means_wide_always_in(self):
        # Missing/stale schedule falls back WIDE: fail toward more data.
        anytime = datetime(2026, 9, 14, 3, 0, 0, tzinfo=UTC)
        self.assertTrue(due.in_game_window(anytime, None))


class TestCadenceTable(unittest.TestCase):
    def test_design_2c_values(self):
        c = due.CADENCES
        self.assertEqual(c["nflverse_schedules"].game, timedelta(minutes=15))
        self.assertEqual(c["nflverse_schedules"].idle, timedelta(hours=6))
        self.assertEqual(c["nflverse_player_stats"].game, timedelta(minutes=30))
        self.assertEqual(c["nflverse_player_stats"].idle, timedelta(hours=24))
        self.assertEqual(c["trend_indicator"].game, timedelta(hours=6))
        self.assertEqual(c["trend_indicator"].idle, timedelta(hours=6))


class TestIsDueSchedules(unittest.TestCase):
    def setUp(self):
        self.win = (K1 - timedelta(minutes=15), K2 + timedelta(hours=4))

    def test_never_captured_is_due(self):
        self.assertTrue(due.is_due("nflverse_schedules", None, K1, self.win))

    def test_in_window_15_min_cadence(self):
        now = K1 + timedelta(minutes=30)
        fresh = now - timedelta(minutes=10)
        stale = now - timedelta(minutes=15)
        self.assertFalse(due.is_due("nflverse_schedules", fresh, now, self.win))
        self.assertTrue(due.is_due("nflverse_schedules", stale, now, self.win))

    def test_idle_6h_cadence(self):
        now = datetime(2026, 9, 13, 8, 0, 0, tzinfo=UTC)  # before window
        self.assertFalse(
            due.is_due("nflverse_schedules", now - timedelta(hours=5), now, self.win)
        )
        self.assertTrue(
            due.is_due("nflverse_schedules", now - timedelta(hours=6), now, self.win)
        )

    def test_missing_schedule_polls_fast(self):
        # window=None -> wide fallback: fast cadence at any hour.
        now = datetime(2026, 9, 14, 3, 0, 0, tzinfo=UTC)
        self.assertTrue(
            due.is_due("nflverse_schedules", now - timedelta(minutes=15), now, None)
        )

    def test_unknown_source_raises(self):
        with self.assertRaises(KeyError):
            due.is_due("reddit_hot", None, K1, self.win)


class TestIsDueTrendIndicator(unittest.TestCase):
    def test_6h_cadence_window_irrelevant(self):
        win = (K1 - timedelta(minutes=15), K2 + timedelta(hours=4))
        now = K1 + timedelta(minutes=30)  # mid-window
        self.assertFalse(
            due.is_due("trend_indicator", now - timedelta(hours=5), now, win)
        )
        self.assertTrue(
            due.is_due("trend_indicator", now - timedelta(hours=6), now, win)
        )


class TestIsDuePlayerStats(unittest.TestCase):
    """Design: every 30 min, starting at window close + 30 min, until the
    week's rows appear; idle every 24 h."""

    def setUp(self):
        self.win = (K1 - timedelta(minutes=15), K2 + timedelta(hours=4))
        self.close = self.win[1]

    def test_idle_before_fast_phase(self):
        now = K1 + timedelta(hours=1)  # games in progress: stats not out yet
        last = now - timedelta(hours=2)
        self.assertFalse(due.is_due("nflverse_player_stats", last, now, self.win))

    def test_fast_phase_starts_at_close_plus_30(self):
        now = self.close + timedelta(minutes=30)
        last = now - timedelta(minutes=30)
        self.assertTrue(due.is_due("nflverse_player_stats", last, now, self.win))

    def test_not_yet_fast_just_before_close_plus_30(self):
        now = self.close + timedelta(minutes=29)
        last = now - timedelta(hours=2)
        self.assertFalse(due.is_due("nflverse_player_stats", last, now, self.win))

    def test_fast_phase_30_min_cadence(self):
        now = self.close + timedelta(hours=2)
        self.assertFalse(
            due.is_due(
                "nflverse_player_stats", now - timedelta(minutes=20), now, self.win
            )
        )
        self.assertTrue(
            due.is_due(
                "nflverse_player_stats", now - timedelta(minutes=30), now, self.win
            )
        )

    def test_rows_present_ends_fast_phase(self):
        now = self.close + timedelta(hours=2)
        last = now - timedelta(hours=1)
        self.assertFalse(
            due.is_due(
                "nflverse_player_stats", last, now, self.win, rows_present=True
            )
        )

    def test_rows_present_still_idle_24h_cadence(self):
        now = self.close + timedelta(hours=2)
        last = now - timedelta(hours=24)
        self.assertTrue(
            due.is_due(
                "nflverse_player_stats", last, now, self.win, rows_present=True
            )
        )

    def test_wide_fallback_polls_fast(self):
        now = datetime(2026, 9, 14, 3, 0, 0, tzinfo=UTC)
        self.assertTrue(
            due.is_due(
                "nflverse_player_stats", now - timedelta(minutes=30), now, None
            )
        )

    def test_idle_24h(self):
        now = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)  # no window that day
        win = None
        # No-games-today from a FRESH schedule is represented by a window
        # computed for that day: use game_window on the real kickoffs.
        win = due.game_window(KICKOFFS, now)  # None -> but schedule fresh
        # Explicit idle check via the fresh-schedule entry point:
        self.assertFalse(
            due.is_due_fresh(
                "nflverse_player_stats", now - timedelta(hours=23), now, KICKOFFS
            )
        )
        self.assertTrue(
            due.is_due_fresh(
                "nflverse_player_stats", now - timedelta(hours=24), now, KICKOFFS
            )
        )


class TestIsDueFresh(unittest.TestCase):
    """is_due_fresh: kickoffs from a fresh schedule; no games today == idle."""

    def test_no_games_today_is_idle_not_wide(self):
        now = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
        self.assertFalse(
            due.is_due_fresh(
                "nflverse_schedules", now - timedelta(hours=1), now, KICKOFFS
            )
        )

    def test_game_day_uses_window(self):
        now = K1 + timedelta(minutes=30)
        self.assertTrue(
            due.is_due_fresh(
                "nflverse_schedules", now - timedelta(minutes=15), now, KICKOFFS
            )
        )


class TestDueSources(unittest.TestCase):
    def test_returns_only_due_sources(self):
        now = K1 + timedelta(minutes=30)  # in window
        win = due.game_window(KICKOFFS, now)
        last = {
            "nflverse_schedules": now - timedelta(minutes=20),   # due (>=15m)
            "nflverse_player_stats": now - timedelta(hours=1),   # idle mid-window
            "trend_indicator": now - timedelta(hours=1),         # not due (<6h)
        }
        self.assertEqual(due.due_sources(last, now, win), ["nflverse_schedules"])

    def test_never_captured_all_due(self):
        now = K1
        win = due.game_window(KICKOFFS, now)
        last = {s: None for s in due.CADENCES}
        self.assertEqual(sorted(due.due_sources(last, now, win)),
                         sorted(due.CADENCES))


if __name__ == "__main__":
    unittest.main()
