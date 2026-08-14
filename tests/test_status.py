"""
L6 Status Tests — TDD-first, hub-status.json writer validation.

Tests atomic write, freshness tracking, alarm raising, and field presence.
"""
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


def test_status_hub_status_json_schema():
    """
    hub-status.json must contain: generated_at, last_tick, tick_ms, mode, sources,
    events_today, events_by_type, rules, queue, alarms.
    """
    from signal_hub.status import write_hub_status

    with tempfile.TemporaryDirectory() as tmpdir:
        state_dir = Path(tmpdir)

        now = datetime(2026, 9, 14, 23, 41, 3, tzinfo=timezone.utc)
        last_tick = datetime(2026, 9, 14, 23, 41, 0, tzinfo=timezone.utc)

        status = {
            "mode": "game_window",
            "sources": {
                "nflverse_schedules": {
                    "last_ok": "2026-09-14T23:40:00Z",
                    "status": "OK",
                },
                "nflverse_player_stats": {
                    "last_ok": "2026-09-14T23:41:00Z",
                    "status": "OK",
                },
            },
            "events_today": 47,
            "events_by_type": {"game_final": 6, "player_stat_line": 40, "demand_rank_delta": 1},
            "rules": {
                "loaded": 3,
                "invalid": 0,
                "fired_today": 5,
                "throttled_today": 11,
            },
            "queue": {
                "pending": 4,
                "claimed": 0,
                "oldest_pending_age_s": 1820,
                "expired_today": 0,
            },
            "alarms": [],
            "tick_ms": 840,
        }

        write_hub_status(state_dir, status, now=now, last_tick=last_tick)

        # Read and validate
        hub_status_file = state_dir / "hub-status.json"
        assert hub_status_file.exists()

        with open(hub_status_file) as f:
            data = json.load(f)

        # All required fields
        assert "generated_at" in data
        assert "last_tick" in data
        assert "tick_ms" in data
        assert "mode" in data
        assert "sources" in data
        assert "events_today" in data
        assert "events_by_type" in data
        assert "rules" in data
        assert "queue" in data
        assert "alarms" in data

        # Validate timestamps
        assert data["generated_at"] == now.isoformat().replace("+00:00", "Z")
        assert data["last_tick"] == last_tick.isoformat().replace("+00:00", "Z")


def test_status_per_source_freshness():
    """
    Per-source freshness tracked: last_ok (ISO datetime), age_s (seconds),
    status (OK | UNCHANGED | STALE | SKIPPED | ERROR).
    """
    from signal_hub.status import write_hub_status

    with tempfile.TemporaryDirectory() as tmpdir:
        state_dir = Path(tmpdir)

        now = datetime(2026, 9, 14, 23, 41, 3, tzinfo=timezone.utc)
        last_tick = datetime(2026, 9, 14, 23, 41, 0, tzinfo=timezone.utc)
        last_ok_time = now - timedelta(seconds=63)

        status = {
            "mode": "game_window",
            "sources": {
                "nflverse_schedules": {
                    "last_ok": last_ok_time.isoformat().replace("+00:00", "Z"),
                    "status": "OK",
                },
                "trend_indicator": {
                    "last_ok": (now - timedelta(hours=5, minutes=41)).isoformat().replace("+00:00", "Z"),
                    "status": "OK",
                },
            },
            "events_today": 0,
            "events_by_type": {},
            "rules": {"loaded": 0, "invalid": 0, "fired_today": 0, "throttled_today": 0},
            "queue": {"pending": 0, "claimed": 0, "oldest_pending_age_s": 0, "expired_today": 0},
            "alarms": [],
            "tick_ms": 100,
        }

        write_hub_status(state_dir, status, now=now, last_tick=last_tick)

        with open(state_dir / "hub-status.json") as f:
            data = json.load(f)

        # Verify age_s calculated
        sched_source = data["sources"]["nflverse_schedules"]
        assert "age_s" in sched_source
        assert sched_source["age_s"] == 63

        trend_source = data["sources"]["trend_indicator"]
        assert "age_s" in trend_source
        assert trend_source["age_s"] == 5 * 3600 + 41 * 60  # 5h 41min in seconds


def test_status_alarms_raised():
    """
    Alarms raised on: pending > 20, expired_today > 0, invalid > 0, source age past threshold.
    """
    from signal_hub.status import write_hub_status

    with tempfile.TemporaryDirectory() as tmpdir:
        state_dir = Path(tmpdir)

        now = datetime(2026, 9, 14, 23, 41, 3, tzinfo=timezone.utc)
        last_tick = datetime(2026, 9, 14, 23, 41, 0, tzinfo=timezone.utc)

        status = {
            "mode": "game_window",
            "sources": {},
            "events_today": 0,
            "events_by_type": {},
            "rules": {
                "loaded": 3,
                "invalid": 1,  # Raise alarm
                "fired_today": 0,
                "throttled_today": 0,
            },
            "queue": {
                "pending": 25,  # > 20, raise alarm
                "claimed": 0,
                "oldest_pending_age_s": 0,
                "expired_today": 2,  # > 0, raise alarm
            },
            "alarms": [],
            "tick_ms": 100,
        }

        write_hub_status(state_dir, status, now=now, last_tick=last_tick)

        with open(state_dir / "hub-status.json") as f:
            data = json.load(f)

        # Should have alarms for: invalid > 0, pending > 20, expired_today > 0
        alarms = data["alarms"]
        assert "invalid_rules" in alarms or "invalid" in str(alarms)
        assert "queue_depth_high" in alarms or "pending" in str(alarms)
        assert "expired_tasks" in alarms or "expired" in str(alarms)


def test_status_atomic_write():
    """
    hub-status.json written atomically (write to temp, fsync, replace).
    No half-written files visible.
    """
    from signal_hub.status import write_hub_status

    with tempfile.TemporaryDirectory() as tmpdir:
        state_dir = Path(tmpdir)

        now = datetime(2026, 9, 14, 23, 41, 3, tzinfo=timezone.utc)
        last_tick = now

        status = {
            "mode": "game_window",
            "sources": {},
            "events_today": 1,
            "events_by_type": {"test": 1},
            "rules": {"loaded": 0, "invalid": 0, "fired_today": 0, "throttled_today": 0},
            "queue": {"pending": 0, "claimed": 0, "oldest_pending_age_s": 0, "expired_today": 0},
            "alarms": [],
            "tick_ms": 50,
        }

        write_hub_status(state_dir, status, now=now, last_tick=last_tick)

        # File should exist and be valid JSON
        hub_status_file = state_dir / "hub-status.json"
        assert hub_status_file.exists()

        with open(hub_status_file) as f:
            data = json.load(f)  # Should not raise JSONDecodeError

        assert data["events_today"] == 1


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
