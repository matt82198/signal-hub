"""Tests for signal_hub.clock -- the single injectable time seam."""
import pathlib
import unittest
from datetime import datetime, timedelta, timezone

from signal_hub import clock


class TestSystemNow(unittest.TestCase):
    def test_returns_aware_utc_datetime(self):
        now = clock.system_now()
        self.assertIsInstance(now, datetime)
        self.assertIsNotNone(now.tzinfo)
        self.assertEqual(now.utcoffset(), timedelta(0))


class TestEnsureUtc(unittest.TestCase):
    def test_rejects_naive_datetime(self):
        with self.assertRaises(ValueError):
            clock.ensure_utc(datetime(2026, 8, 13, 14, 2, 0))

    def test_converts_other_zone_to_utc(self):
        est = timezone(timedelta(hours=-5))
        dt = datetime(2026, 8, 13, 9, 2, 0, tzinfo=est)
        out = clock.ensure_utc(dt)
        self.assertEqual(out.utcoffset(), timedelta(0))
        self.assertEqual(out.hour, 14)

    def test_utc_passes_through(self):
        dt = datetime(2026, 8, 13, 14, 2, 0, tzinfo=timezone.utc)
        self.assertEqual(clock.ensure_utc(dt), dt)


class TestCompactTimestamp(unittest.TestCase):
    def test_format(self):
        dt = datetime(2026, 8, 13, 14, 2, 0, tzinfo=timezone.utc)
        self.assertEqual(clock.ts_compact(dt), "20260813T140200Z")

    def test_round_trip(self):
        dt = datetime(2026, 9, 14, 23, 41, 0, tzinfo=timezone.utc)
        self.assertEqual(clock.parse_compact(clock.ts_compact(dt)), dt)

    def test_lexical_sort_is_chronological(self):
        dts = [
            datetime(2025, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 13, 9, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 13, 14, 2, 0, tzinfo=timezone.utc),
            datetime(2026, 11, 2, 1, 30, 0, tzinfo=timezone.utc),
        ]
        stamps = [clock.ts_compact(d) for d in dts]
        self.assertEqual(stamps, sorted(stamps))

    def test_parse_rejects_garbage(self):
        with self.assertRaises(ValueError):
            clock.parse_compact("not-a-timestamp")


class TestIsoTimestamp(unittest.TestCase):
    def test_format(self):
        dt = datetime(2026, 8, 13, 14, 2, 0, tzinfo=timezone.utc)
        self.assertEqual(clock.iso_utc(dt), "2026-08-13T14:02:00Z")

    def test_round_trip(self):
        dt = datetime(2026, 9, 14, 23, 41, 3, tzinfo=timezone.utc)
        self.assertEqual(clock.parse_iso(clock.iso_utc(dt)), dt)


class TestFrozenClock(unittest.TestCase):
    def test_returns_start_time(self):
        start = datetime(2026, 8, 13, 14, 2, 0, tzinfo=timezone.utc)
        frozen = clock.FrozenClock(start)
        self.assertEqual(frozen(), start)
        self.assertEqual(frozen(), start)  # frozen: stable across calls

    def test_advance(self):
        start = datetime(2026, 8, 13, 14, 2, 0, tzinfo=timezone.utc)
        frozen = clock.FrozenClock(start)
        frozen.advance(timedelta(minutes=5))
        self.assertEqual(frozen(), start + timedelta(minutes=5))

    def test_set(self):
        start = datetime(2026, 8, 13, 14, 2, 0, tzinfo=timezone.utc)
        later = datetime(2026, 9, 14, 23, 41, 0, tzinfo=timezone.utc)
        frozen = clock.FrozenClock(start)
        frozen.set(later)
        self.assertEqual(frozen(), later)

    def test_rejects_naive_start(self):
        with self.assertRaises(ValueError):
            clock.FrozenClock(datetime(2026, 8, 13, 14, 2, 0))


class TestNoAmbientClockOutsideSeam(unittest.TestCase):
    """Design 7: no lane calls datetime.now()/Date.now outside clock.py."""

    def test_no_datetime_now_outside_clock_module(self):
        pkg = pathlib.Path(clock.__file__).parent
        offenders = []
        for py in pkg.rglob("*.py"):
            if py.name == "clock.py":
                continue
            text = py.read_text(encoding="utf-8")
            if "datetime.now(" in text or "datetime.utcnow(" in text:
                offenders.append(str(py))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
