"""Tests for signal_hub.snapshots -- atomic snapshot store (design 2b)."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from signal_hub.clock import FrozenClock
from signal_hub.snapshots import SnapshotStore, STATUSES

T0 = datetime(2026, 8, 13, 14, 2, 0, tzinfo=timezone.utc)


class SnapshotCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.clock = FrozenClock(T0)
        self.store = SnapshotStore(self.root, now=self.clock)


class TestWrite(SnapshotCase):
    def test_layout_and_filename(self):
        path = self.store.write("nflverse_schedules", payload={"games": []})
        self.assertEqual(
            path,
            self.root / "snapshots" / "nflverse_schedules" / "20260813T140200Z.json",
        )
        self.assertTrue(path.is_file())

    def test_document_shape(self):
        path = self.store.write(
            "nflverse_schedules", payload={"a": 1}, etag='W/"abc123"'
        )
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(doc["source"], "nflverse_schedules")
        self.assertEqual(doc["schema_version"], 1)
        self.assertEqual(doc["captured_at"], "2026-08-13T14:02:00Z")
        self.assertEqual(doc["status"], "OK")
        self.assertEqual(doc["etag"], 'W/"abc123"')
        self.assertEqual(doc["payload"], {"a": 1})

    def test_no_tmp_droppings_after_write(self):
        self.store.write("src", payload={})
        leftovers = [p for p in self.root.rglob("*.tmp")]
        self.assertEqual(leftovers, [])

    def test_invalid_status_rejected(self):
        with self.assertRaises(ValueError):
            self.store.write("src", payload={}, status="WEIRD")

    def test_all_declared_statuses(self):
        self.assertEqual(
            set(STATUSES), {"OK", "UNCHANGED", "STALE", "SKIPPED", "ERROR"}
        )

    def test_error_snapshot_written_with_error_text_no_payload(self):
        # Inputs always produce outputs: an ERROR snapshot still lands on disk.
        path = self.store.write("src", status="ERROR", error="boom: 503")
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(doc["status"], "ERROR")
        self.assertEqual(doc["error"], "boom: 503")
        self.assertNotIn("payload", doc)

    def test_error_status_requires_error_text(self):
        with self.assertRaises(ValueError):
            self.store.write("src", status="ERROR")

    def test_utf8_payload_round_trips(self):
        path = self.store.write("src", payload={"name": "Björn"})
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(doc["payload"]["name"], "Björn")

    def test_same_second_writes_get_unique_monotonic_names(self):
        p1 = self.store.write("src", payload={"n": 1})
        p2 = self.store.write("src", payload={"n": 2})  # clock not advanced
        self.assertNotEqual(p1, p2)
        self.assertLess(p1.name, p2.name)  # lexical order preserved
        self.assertEqual(self.store.latest("src").payload, {"n": 2})


class TestAccessors(SnapshotCase):
    def test_latest_previous_empty(self):
        self.assertIsNone(self.store.latest("src"))
        self.assertIsNone(self.store.previous("src"))

    def test_latest_and_previous(self):
        self.store.write("src", payload={"n": 1})
        self.clock.advance(timedelta(minutes=5))
        self.store.write("src", payload={"n": 2})
        latest = self.store.latest("src")
        prev = self.store.previous("src")
        self.assertEqual(latest.payload, {"n": 2})
        self.assertEqual(latest.status, "OK")
        self.assertEqual(latest.captured_at, T0 + timedelta(minutes=5))
        self.assertEqual(prev.payload, {"n": 1})
        self.assertEqual(prev.captured_at, T0)

    def test_previous_none_with_single_snapshot(self):
        self.store.write("src", payload={})
        self.assertIsNotNone(self.store.latest("src"))
        self.assertIsNone(self.store.previous("src"))

    def test_sources_are_isolated(self):
        self.store.write("a", payload={"src": "a"})
        self.clock.advance(timedelta(minutes=1))
        self.store.write("b", payload={"src": "b"})
        self.assertEqual(self.store.latest("a").payload, {"src": "a"})
        self.assertEqual(self.store.latest("b").payload, {"src": "b"})

    def test_lexical_order_across_day_boundary(self):
        self.clock.set(datetime(2026, 12, 31, 23, 59, 0, tzinfo=timezone.utc))
        self.store.write("src", payload={"n": 1})
        self.clock.advance(timedelta(minutes=2))  # rolls into 2027-01-01
        self.store.write("src", payload={"n": 2})
        names = [p.name for p in self.store.paths("src")]
        self.assertEqual(names, sorted(names))
        self.assertEqual(self.store.latest("src").payload, {"n": 2})


class TestRetention(SnapshotCase):
    def test_default_keep_is_200(self):
        self.assertEqual(self.store.keep, 200)

    def test_prune_keeps_newest_n(self):
        store = SnapshotStore(self.root, now=self.clock, keep=3)
        for n in range(5):
            store.write("src", payload={"n": n})
            self.clock.advance(timedelta(minutes=5))
        removed = store.prune("src")
        self.assertEqual(len(removed), 2)
        remaining = store.paths("src")
        self.assertEqual(len(remaining), 3)
        payloads = [json.loads(p.read_text(encoding="utf-8"))["payload"]["n"]
                    for p in remaining]
        self.assertEqual(payloads, [2, 3, 4])  # newest 3 survive

    def test_prune_noop_under_limit(self):
        self.store.write("src", payload={})
        self.assertEqual(self.store.prune("src"), [])

    def test_prune_all_covers_every_source(self):
        store = SnapshotStore(self.root, now=self.clock, keep=1)
        for src in ("a", "b"):
            for n in range(3):
                store.write(src, payload={"n": n})
                self.clock.advance(timedelta(minutes=5))
        removed = store.prune_all()
        self.assertEqual(removed, 4)
        self.assertEqual(len(store.paths("a")), 1)
        self.assertEqual(len(store.paths("b")), 1)

    def test_prune_missing_source_is_empty(self):
        self.assertEqual(self.store.prune("never_written"), [])


if __name__ == "__main__":
    unittest.main()
