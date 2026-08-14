"""Tests for the two firing gates (design section 4, "Throttle / dedup").

Gate 1 is unconditional and permanent: (rule_id, event_id) fires once, forever.
Gate 2 is the rule's declared throttle: (rule_id, resolved key, window bucket).

Both are backed by one append-only JSONL file and an injected clock.
"""

import datetime
import json
import os
import tempfile
import unittest

from signal_hub.rules_engine import ThrottleState


def utc(*args):
    return datetime.datetime(*args, tzinfo=datetime.timezone.utc)


class FrozenClock:
    """Injectable clock.  Nothing in the engine may call datetime.now()."""

    def __init__(self, moment):
        self.moment = moment

    def __call__(self):
        return self.moment

    def advance(self, **kwargs):
        self.moment = self.moment + datetime.timedelta(**kwargs)
        return self.moment


class ThrottleTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "state", ".rules-fired.jsonl")
        self.clock = FrozenClock(utc(2026, 9, 14, 23, 41, 0))

    def load(self):
        return ThrottleState.load(self.path, now=self.clock)


class DuplicateGateTests(ThrottleTestCase):
    def test_unseen_pair_is_not_a_duplicate(self):
        state = self.load()
        self.assertFalse(state.is_duplicate("R002", "abc123"))

    def test_recorded_pair_is_a_duplicate(self):
        state = self.load()
        state.record("R002", "abc123")
        self.assertTrue(state.is_duplicate("R002", "abc123"))

    def test_gate_is_scoped_per_rule(self):
        state = self.load()
        state.record("R002", "abc123")
        self.assertFalse(state.is_duplicate("R001", "abc123"))

    def test_gate_is_scoped_per_event(self):
        state = self.load()
        state.record("R002", "abc123")
        self.assertFalse(state.is_duplicate("R002", "def456"))

    def test_gate_is_permanent(self):
        state = self.load()
        state.record("R002", "abc123")
        self.clock.advance(days=4000)
        self.assertTrue(state.is_duplicate("R002", "abc123"))

    def test_gate_survives_a_restart(self):
        self.load().record("R002", "abc123")
        self.assertTrue(self.load().is_duplicate("R002", "abc123"))

    def test_revised_event_with_the_same_id_never_fires_twice(self):
        # nflverse revises stat lines; the identity hash is stable, so the
        # second sighting must hit this gate.
        state = self.load()
        state.record("R002-big-stat-line", "a3f91c2e8b7d4506")
        self.clock.advance(days=2)
        self.assertTrue(state.is_duplicate("R002-big-stat-line", "a3f91c2e8b7d4506"))


class DeclaredThrottleTests(ThrottleTestCase):
    def test_no_window_means_no_throttle(self):
        state = self.load()
        self.assertFalse(state.is_throttled("R002", "R002|player-1", None))

    def test_unseen_key_is_not_throttled(self):
        state = self.load()
        self.assertFalse(state.is_throttled("R002", "R002|player-1", "1d"))

    def test_recorded_key_is_throttled_inside_the_window(self):
        state = self.load()
        state.record("R002", "evt-1", throttle_key="R002|player-1", window="1d")
        self.assertTrue(state.is_throttled("R002", "R002|player-1", "1d"))

    def test_a_different_key_is_not_throttled(self):
        state = self.load()
        state.record("R002", "evt-1", throttle_key="R002|player-1", window="1d")
        self.assertFalse(state.is_throttled("R002", "R002|player-2", "1d"))

    def test_throttle_lapses_at_the_bucket_boundary(self):
        state = self.load()
        state.record("R002", "evt-1", throttle_key="R002|player-1", window="1d")
        self.clock.advance(minutes=18)  # still 2026-09-14
        self.assertTrue(state.is_throttled("R002", "R002|player-1", "1d"))
        self.clock.advance(minutes=2)  # 2026-09-15T00:01Z, new bucket
        self.assertFalse(state.is_throttled("R002", "R002|player-1", "1d"))

    def test_hour_window_boundary(self):
        state = self.load()
        state.record("R00X", "evt-1", throttle_key="R00X|k", window="1h")
        self.clock.advance(minutes=10)
        self.assertTrue(state.is_throttled("R00X", "R00X|k", "1h"))
        self.clock.advance(minutes=10)
        self.assertFalse(state.is_throttled("R00X", "R00X|k", "1h"))

    def test_forever_window_never_lapses(self):
        state = self.load()
        state.record("R001", "evt-1", throttle_key="R001|2026_02_CHI_DET", window="forever")
        self.clock.advance(days=4000)
        self.assertTrue(state.is_throttled("R001", "R001|2026_02_CHI_DET", "forever"))

    def test_throttle_survives_a_restart(self):
        self.load().record("R002", "evt-1", throttle_key="R002|p1", window="1d")
        self.assertTrue(self.load().is_throttled("R002", "R002|p1", "1d"))

    def test_windows_do_not_bleed_into_each_other(self):
        state = self.load()
        state.record("R002", "evt-1", throttle_key="R002|p1", window="1h")
        self.assertFalse(state.is_throttled("R002", "R002|p1", "1d"))


class PersistenceTests(ThrottleTestCase):
    def test_parent_directories_are_created(self):
        self.load().record("R002", "evt-1")
        self.assertTrue(os.path.isfile(self.path))

    def test_records_are_one_json_object_per_line(self):
        state = self.load()
        state.record("R002", "evt-1", throttle_key="R002|p1", window="1d")
        state.record("R002", "evt-2", throttle_key="R002|p2", window="1d")
        with open(self.path, "r", encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines() if line]
        self.assertEqual(len(lines), 2)
        record = json.loads(lines[0])
        self.assertEqual(record["rule_id"], "R002")
        self.assertEqual(record["event_id"], "evt-1")
        self.assertEqual(record["throttle_key"], "R002|p1")
        self.assertEqual(record["window"], "1d")
        self.assertEqual(record["fired_at"], "2026-09-14T23:41:00Z")
        self.assertTrue(record["bucket"].startswith("1d:"))

    def test_the_log_is_append_only(self):
        state = self.load()
        state.record("R002", "evt-1")
        second = self.load()
        second.record("R002", "evt-2")
        with open(self.path, "r", encoding="utf-8") as handle:
            self.assertEqual(len([x for x in handle.read().splitlines() if x]), 2)

    def test_record_returns_the_persisted_record(self):
        record = self.load().record("R002", "evt-1", throttle_key="R002|p1", window="1d")
        self.assertEqual(record["event_id"], "evt-1")

    def test_missing_file_loads_empty(self):
        state = self.load()
        self.assertEqual(state.count, 0)
        self.assertEqual(state.corrupt_lines, 0)

    def test_ascii_is_written_even_for_non_ascii_keys(self):
        key = "R002|Jos\u00e9 \u00c1lvarez"  # escaped: this file stays ASCII
        self.load().record("R002", "evt-1", throttle_key=key, window="1d")
        with open(self.path, "rb") as handle:
            raw = handle.read()
        self.assertTrue(raw.isascii())
        self.assertTrue(self.load().is_throttled("R002", key, "1d"))

    def test_no_carriage_returns_are_written(self):
        self.load().record("R002", "evt-1")
        with open(self.path, "rb") as handle:
            self.assertNotIn(b"\r", handle.read())


class CorruptStateTests(ThrottleTestCase):
    def _write(self, text):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    def test_junk_lines_are_skipped_and_counted(self):
        self._write(
            '{"rule_id": "R002", "event_id": "good"}\n'
            "not json at all\n"
            "[1, 2, 3]\n"
            '"a bare string"\n'
            "\n"
            '{"rule_id": "R002", "event_id": "also-good"}\n'
        )
        state = self.load()
        self.assertTrue(state.is_duplicate("R002", "good"))
        self.assertTrue(state.is_duplicate("R002", "also-good"))
        self.assertEqual(state.count, 2)
        self.assertEqual(state.corrupt_lines, 3)

    def test_records_missing_fields_are_skipped(self):
        self._write('{"rule_id": "R002"}\n{"event_id": "x"}\n{}\n')
        state = self.load()
        self.assertEqual(state.count, 0)
        self.assertEqual(state.corrupt_lines, 3)

    def test_non_string_fields_are_skipped(self):
        self._write('{"rule_id": 7, "event_id": "x"}\n')
        self.assertEqual(self.load().count, 0)

    def test_a_truncated_final_line_does_not_lose_earlier_records(self):
        self._write('{"rule_id": "R002", "event_id": "good"}\n{"rule_id": "R00')
        state = self.load()
        self.assertTrue(state.is_duplicate("R002", "good"))
        self.assertEqual(state.corrupt_lines, 1)

    def test_appending_after_a_truncated_line_stays_parseable(self):
        self._write('{"rule_id": "R002", "event_id": "good"}\n{"rule_id": "R00')
        self.load().record("R002", "evt-2")
        state = self.load()
        self.assertTrue(state.is_duplicate("R002", "evt-2"))

    def test_oversized_lines_are_skipped_not_loaded(self):
        self._write('{"rule_id": "R002", "event_id": "%s"}\n' % ("x" * 200000))
        state = self.load()
        self.assertEqual(state.count, 0)
        self.assertEqual(state.corrupt_lines, 1)


class ClockInjectionTests(ThrottleTestCase):
    def test_clock_is_required(self):
        with self.assertRaises(TypeError):
            ThrottleState.load(self.path)

    def test_the_injected_clock_is_the_only_time_source(self):
        state = self.load()
        state.record("R002", "evt-1", throttle_key="R002|p1", window="1d")
        with open(self.path, "r", encoding="utf-8") as handle:
            record = json.loads(handle.readline())
        self.assertEqual(record["fired_at"], "2026-09-14T23:41:00Z")


if __name__ == "__main__":
    unittest.main()
