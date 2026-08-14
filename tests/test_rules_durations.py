"""Tests for ttl / throttle-window parsing and clock bucketing.

Every time-dependent value in the rules engine goes through here, against an
injected clock.  A system untestable at 11:59 PM Sunday is not shippable.
"""

import datetime
import unittest

from signal_hub.rules_engine import (
    DurationError,
    WINDOWS,
    bucket_id,
    format_timestamp,
    parse_duration,
    parse_window,
)


def utc(*args):
    return datetime.datetime(*args, tzinfo=datetime.timezone.utc)


class ParseDurationTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual(parse_duration("30m"), 1800)
        self.assertEqual(parse_duration("1h"), 3600)
        self.assertEqual(parse_duration("6h"), 21600)
        self.assertEqual(parse_duration("1d"), 86400)
        self.assertEqual(parse_duration("2d"), 172800)
        self.assertEqual(parse_duration("3d"), 259200)
        self.assertEqual(parse_duration("7d"), 604800)
        self.assertEqual(parse_duration("1w"), 604800)

    def test_forever_has_no_duration(self):
        self.assertIsNone(parse_duration("forever"))

    def test_rejects_junk(self):
        for junk in ("", "d", "3", "3y", "-1d", "1.5h", "1 d", "1D", "1h30m", None, 3, "3s"):
            with self.assertRaises(DurationError, msg=repr(junk)):
                parse_duration(junk)

    def test_rejects_zero(self):
        with self.assertRaises(DurationError):
            parse_duration("0d")

    def test_rejects_absurd_magnitudes(self):
        with self.assertRaises(DurationError):
            parse_duration("9" * 12 + "d")

    def test_error_names_the_offender(self):
        with self.assertRaises(DurationError) as ctx:
            parse_duration("3y")
        self.assertIn("3y", str(ctx.exception))


class ParseWindowTests(unittest.TestCase):
    def test_accepts_only_the_declared_windows(self):
        self.assertEqual(sorted(WINDOWS), ["1d", "1h", "6h", "7d", "forever"])
        for window in WINDOWS:
            self.assertEqual(parse_window(window), window)

    def test_rejects_durations_that_are_not_windows(self):
        for junk in ("2d", "30m", "1w", "3h", "", None, "FOREVER"):
            with self.assertRaises(DurationError, msg=repr(junk)):
                parse_window(junk)

    def test_error_lists_the_valid_windows(self):
        with self.assertRaises(DurationError) as ctx:
            parse_window("2d")
        self.assertIn("forever", str(ctx.exception))


class BucketTests(unittest.TestCase):
    def test_forever_is_one_bucket_for_all_time(self):
        self.assertEqual(
            bucket_id("forever", utc(2026, 9, 14)), bucket_id("forever", utc(2099, 1, 1))
        )
        self.assertEqual(bucket_id("forever", utc(2026, 9, 14)), "forever")

    def test_bucket_is_prefixed_by_its_window(self):
        self.assertTrue(bucket_id("1d", utc(2026, 9, 14, 12)).startswith("1d:"))

    def test_same_day_shares_a_bucket(self):
        self.assertEqual(
            bucket_id("1d", utc(2026, 9, 14, 0, 0, 0)),
            bucket_id("1d", utc(2026, 9, 14, 23, 59, 59)),
        )

    def test_utc_midnight_is_the_day_boundary(self):
        self.assertNotEqual(
            bucket_id("1d", utc(2026, 9, 14, 23, 59, 59)),
            bucket_id("1d", utc(2026, 9, 15, 0, 0, 0)),
        )

    def test_hour_boundary(self):
        self.assertEqual(
            bucket_id("1h", utc(2026, 9, 14, 13, 0, 0)),
            bucket_id("1h", utc(2026, 9, 14, 13, 59, 59)),
        )
        self.assertNotEqual(
            bucket_id("1h", utc(2026, 9, 14, 13, 59, 59)),
            bucket_id("1h", utc(2026, 9, 14, 14, 0, 0)),
        )

    def test_six_hour_buckets_align_to_utc_quarters(self):
        self.assertEqual(
            bucket_id("6h", utc(2026, 9, 14, 12, 0, 0)),
            bucket_id("6h", utc(2026, 9, 14, 17, 59, 59)),
        )
        self.assertNotEqual(
            bucket_id("6h", utc(2026, 9, 14, 11, 59, 59)),
            bucket_id("6h", utc(2026, 9, 14, 12, 0, 0)),
        )

    def test_seven_day_buckets_are_stable_and_advance(self):
        self.assertEqual(
            bucket_id("7d", utc(2026, 9, 14)), bucket_id("7d", utc(2026, 9, 15))
        )
        self.assertNotEqual(
            bucket_id("7d", utc(2026, 9, 14)), bucket_id("7d", utc(2026, 9, 30))
        )

    def test_naive_datetimes_are_rejected(self):
        with self.assertRaises(DurationError):
            bucket_id("1d", datetime.datetime(2026, 9, 14))

    def test_non_utc_offsets_are_normalised(self):
        eastern = datetime.timezone(datetime.timedelta(hours=-4))
        self.assertEqual(
            bucket_id("1d", datetime.datetime(2026, 9, 14, 20, 0, tzinfo=eastern)),
            bucket_id("1d", utc(2026, 9, 15, 0, 0)),
        )

    def test_unknown_window_is_rejected(self):
        with self.assertRaises(DurationError):
            bucket_id("2d", utc(2026, 9, 14))


class FormatTimestampTests(unittest.TestCase):
    def test_basic_iso_utc_with_z(self):
        self.assertEqual(format_timestamp(utc(2026, 9, 14, 23, 41, 0)), "2026-09-14T23:41:00Z")

    def test_sub_second_precision_is_dropped(self):
        self.assertEqual(
            format_timestamp(utc(2026, 9, 14, 23, 41, 0, 987654)), "2026-09-14T23:41:00Z"
        )

    def test_offsets_are_converted_to_utc(self):
        eastern = datetime.timezone(datetime.timedelta(hours=-4))
        self.assertEqual(
            format_timestamp(datetime.datetime(2026, 9, 14, 19, 41, tzinfo=eastern)),
            "2026-09-14T23:41:00Z",
        )

    def test_naive_datetimes_are_rejected(self):
        with self.assertRaises(DurationError):
            format_timestamp(datetime.datetime(2026, 9, 14))


if __name__ == "__main__":
    unittest.main()
