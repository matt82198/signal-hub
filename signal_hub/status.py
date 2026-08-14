"""
L6 Status — hub-status.json writer with atomic writes, freshness tracking, and alarms.

Writes hub-status.json every tick with: generated_at, last_tick, tick_ms, mode,
per-source freshness, events, rules, queue, alarms.
Atomic write via .tmp + os.replace.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def write_hub_status(
    state_dir: Path,
    status: dict,
    now: datetime = None,
    last_tick: datetime = None,
) -> None:
    """
    Write hub-status.json atomically with computed freshness and alarms.

    Args:
        state_dir: Path to state/ directory
        status: Status dict with keys:
            - mode: "game_window" or "idle"
            - sources: {source_name: {last_ok: ISO ts, status: OK|UNCHANGED|...}}
            - events_today: int
            - events_by_type: {type: count}
            - rules: {loaded, invalid, fired_today, throttled_today}
            - queue: {pending, claimed, oldest_pending_age_s, expired_today}
            - alarms: list (will be computed if empty)
            - tick_ms: int
        now: Injected clock (defaults to now)
        last_tick: Tick start time (defaults to now)
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if last_tick is None:
        last_tick = now

    state_dir.mkdir(parents=True, exist_ok=True)

    # Build output object
    output = {
        "generated_at": now.replace(microsecond=0).isoformat().replace("+00:00", "") + "Z",
        "last_tick": last_tick.replace(microsecond=0).isoformat().replace("+00:00", "") + "Z",
        "tick_ms": status.get("tick_ms", 0),
        "mode": status.get("mode", "unknown"),
        "sources": {},
        "events_today": status.get("events_today", 0),
        "events_by_type": status.get("events_by_type", {}),
        "rules": status.get("rules", {}),
        "queue": status.get("queue", {}),
        "alarms": [],
    }

    # Compute per-source freshness (age_s)
    for source_name, source_data in status.get("sources", {}).items():
        source_entry = {
            "last_ok": source_data.get("last_ok"),
            "status": source_data.get("status", "UNKNOWN"),
        }

        # Compute age_s from last_ok
        if source_data.get("last_ok"):
            last_ok_str = source_data["last_ok"].replace("Z", "+00:00")
            try:
                last_ok = datetime.fromisoformat(last_ok_str)
                age_s = int((now - last_ok).total_seconds())
                source_entry["age_s"] = age_s
            except ValueError:
                source_entry["age_s"] = 0

        output["sources"][source_name] = source_entry

    # Compute alarms
    alarms = set()

    # Alarm: any invalid rules
    if status.get("rules", {}).get("invalid", 0) > 0:
        alarms.add("invalid_rules")

    # Alarm: queue depth high (> 20 pending)
    if status.get("queue", {}).get("pending", 0) > 20:
        alarms.add("queue_depth_high")

    # Alarm: expired tasks (> 0)
    if status.get("queue", {}).get("expired_today", 0) > 0:
        alarms.add("expired_tasks")

    # Alarm: source age past 3x cadence (hardcoded thresholds for MVP)
    source_cadence_hours = {
        "nflverse_schedules": 0.25,  # 15 min
        "nflverse_player_stats": 0.5,  # 30 min
        "trend_indicator": 6,
        "platform_analytics": 6,
        "reddit": 6,
    }

    for source_name, source_data in output.get("sources", {}).items():
        age_s = source_data.get("age_s", 0)
        cadence_hours = source_cadence_hours.get(source_name, 6)
        threshold_s = int(cadence_hours * 3600 * 3)  # 3x cadence

        if age_s > threshold_s and source_data.get("status") != "SKIPPED":
            alarms.add(f"source_stale_{source_name}")

    # Alarm: heartbeat old (> 15 min, checked externally but flagged here if detected)
    # (This would be set by tick runner if detected)

    output["alarms"] = sorted(list(alarms))

    # Write atomically: .tmp -> state_dir
    tmp_dir = state_dir / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    tmp_path = tmp_dir / "hub-status.json"
    final_path = state_dir / "hub-status.json"

    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(output, f, separators=(",", ":"), indent=None)
        try:
            os.fsync(f.fileno())
        except (AttributeError, OSError):
            pass

    os.replace(str(tmp_path), str(final_path))
