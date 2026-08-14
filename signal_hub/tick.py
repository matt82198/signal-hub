"""The orchestrated tick: one pass of the whole pipeline (lane L7).

    HALT? -> due sources -> adapters -> snapshots -> delta -> event log + seen
          -> rules -> queue -> status -> rotation -> heartbeat

This module is the ONLY place the lanes meet.  Every lane below it is
contract-driven and knows nothing about the others; the wiring, the ordering
and the failure isolation live here.

Three seams are injected all the way down (design section 7): ``now``,
``http_get`` and ``runner``/``file_reader``.  Nothing in this file reads the
ambient wall clock or opens a socket on its own -- ``system_now`` and
``urllib_http_get`` are defaults a caller can always replace, and no test
uses either.  (The clock-seam guard in ``tests/test_clock.py`` greps the whole
package for the ambient call, so even naming it in prose trips the check.)

FAILURE ISOLATION
-----------------
One source failing must not kill the tick.  Every capture runs inside its own
guard; the failure becomes an ERROR snapshot (inputs always produce outputs --
a missing file means the tick never ran, a louder and different failure than a
source being down) plus a line in ``state/TICK.log`` and an entry in
``TickResult.errors``.  The remaining sources, the rules and the status write
all still happen.

DRY RUN
-------
``dry_run=True`` runs every stage and lands NOTHING: no snapshot, no event log
line, no seen-index mark, no throttle record, no task file, no status, no
heartbeat.  The current payloads are diffed in memory against what is already
on disk, so a dry run is repeatable and can be run against a live hub without
disturbing it.  That is a stronger and more useful guarantee than "queue writes
suppressed", and it is what makes ``--dry-run`` safe to hand a user.

ORDERING NOTE (honest)
----------------------
The rule engine arms both firing gates (``(rule_id, event_id)`` and the
declared throttle) as it decides to fire, and the task file is written
immediately afterwards.  A crash in between therefore loses a task rather than
duplicating one.  That direction is deliberate: sports content is perishable
and ``expires_at`` already bounds a late task, whereas a duplicate content task
is visible to a human as a mistake.  Enqueue failures are counted in
``TickResult.errors`` and logged, never swallowed.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from signal_hub import due as due_mod
from signal_hub import rotate as rotate_mod
from signal_hub.adapters import base as adapter_base
from signal_hub.adapters import nflverse_player_stats, nflverse_schedules
from signal_hub.adapters.trend_indicator import TrendIndicatorAdapter
from signal_hub.clock import iso_utc, parse_iso, system_now
from signal_hub.events import EventLog, SeenIndex, diff_snapshots, record_events
from signal_hub.queue import enqueue_task
from signal_hub.rules_engine import RuleEngine, ThrottleState, load_rules
from signal_hub.snapshots import SnapshotStore
from signal_hub.status import write_hub_status

__all__ = [
    "SOURCES",
    "TickResult",
    "default_file_reader",
    "default_runner",
    "nfl_season",
    "offline_http_get",
    "run_tick",
]

#: The sources with a cadence.  The two stubs (platform_analytics, reddit) have
#: no cadence entry, so they are never due and never touched -- a stub that
#: raises NotImplementedError every five minutes is noise, not a signal.
SOURCES = ("nflverse_schedules", "nflverse_player_stats", "trend_indicator")

HALT_FILE = ".HALT"
HEARTBEAT_FILE = ".signal-hub-heartbeat"
CAPTURE_STATE_FILE = "last-capture.json"
THROTTLE_FILE = ".rules-fired.jsonl"
TICK_LOG = "TICK.log"
INBOX_OUT = "INBOX-OUT.md"

#: A schedule snapshot older than this is not trustworthy enough to narrow the
#: game window with.  Missing or stale both resolve to the WIDE window: fail
#: toward more data, never toward less (design section 2c).
#:
#: A day, not an hour: games.csv carries the WHOLE season, so a snapshot taken
#: yesterday still knows today's kickoffs exactly, and flex scheduling moves
#: games by days rather than minutes.  A tighter horizon would make the hub
#: permanently "wide" during the off-season -- the idle cadence is 6 h, so a
#: 7-hour-old schedule snapshot is the NORMAL state, and treating it as stale
#: would silently delete the whole idle cadence.
SCHEDULE_TRUST_AGE = timedelta(hours=24)

#: nfldata spells kickoffs in US Eastern with no offset column, and stdlib
#: zoneinfo has no IANA database on Windows.  A fixed EDT offset is close
#: enough because the window is padded by 15 min in front and 4 h behind: an
#: hour of DST error is absorbed, and every error direction widens the window.
EASTERN_TO_UTC = timedelta(hours=4)

DEFAULT_KEEP_SNAPSHOTS = 200


# ---------------------------------------------------------------------------
# result
# ---------------------------------------------------------------------------

@dataclass
class TickResult:
    """Everything one tick did.  Returned even when the tick failed or halted."""

    root: Path
    started_at: datetime
    tick_ms: int = 0
    mode: str = "idle"
    dry_run: bool = False
    halted: bool = False
    halt_reason: Optional[str] = None
    captured: dict = field(default_factory=dict)      # source -> snapshot status
    events: list = field(default_factory=list)        # Event objects derived
    recorded: list = field(default_factory=list)      # Events actually logged
    actions: list = field(default_factory=list)       # rule engine action dicts
    enqueued: list = field(default_factory=list)      # task ids written
    errors: list = field(default_factory=list)        # human-readable failures
    rules_loaded: int = 0
    rules_invalid: int = 0
    status: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.halted


# ---------------------------------------------------------------------------
# real-world seams (never used by a test)
# ---------------------------------------------------------------------------

def default_runner(cmd, cwd=None) -> int:
    """Run a child process and return its exit code.  Output is discarded --
    the artifact the adapter reads afterwards is the real result."""
    return subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode


def default_file_reader(path):
    """Read a JSON artifact belonging to another repo.  Read-only, always."""
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def offline_http_get(url, headers=None, timeout=adapter_base.DEFAULT_TIMEOUT):
    """An http_get that refuses to leave the machine.  Powers ``--offline``,
    which is how the pipeline is exercised without a network."""
    raise OSError("offline mode: refusing to fetch %s" % url)


def nfl_season(now: datetime) -> int:
    """The season a moment belongs to.  A season spans a new year, so anything
    before March still belongs to the previous season's stat asset."""
    return now.year if now.month >= 3 else now.year - 1


# ---------------------------------------------------------------------------
# small filesystem helpers
# ---------------------------------------------------------------------------

def _read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json_atomic(path: Path, doc) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, ensure_ascii=True, indent=1, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _snapshot_dict(snapshot) -> Optional[dict]:
    """A `Snapshot` dataclass in the wire shape `diff_snapshots` destructures."""
    if snapshot is None:
        return None
    return {
        "source": snapshot.source,
        "status": snapshot.status,
        "captured_at": iso_utc(snapshot.captured_at),
        "payload": snapshot.payload,
    }


def _latest_usable(store: SnapshotStore, source: str) -> Optional[dict]:
    """The newest snapshot that still carries a payload.

    NOT simply the newest snapshot.  A 304 writes an UNCHANGED snapshot with no
    payload, and the design says the delta engine must treat snapshot(n-1) as
    still-current -- so the baseline has to skip back over UNCHANGED/ERROR/STALE
    entries.  Taking the literal newest would make a single 304 silently swallow
    the next real transition, which is the exact bug this walk exists to avoid.
    """
    for path in reversed(store.paths(source)):
        try:
            snapshot = store.read(path)
        except (OSError, ValueError, KeyError):
            continue
        if snapshot.status == "OK" and snapshot.payload is not None:
            return _snapshot_dict(snapshot)
    return None


# ---------------------------------------------------------------------------
# game window
# ---------------------------------------------------------------------------

def kickoffs(payload) -> list:
    """Kickoff instants from a schedule payload, best effort and never raising."""
    out = []
    if not isinstance(payload, dict):
        return out
    for game in payload.get("games") or []:
        if not isinstance(game, dict):
            continue
        day = (game.get("gameday") or "").strip()
        clock = (game.get("gametime") or "").strip() or "13:00"
        try:
            naive = datetime.strptime("%s %s" % (day, clock[:5]), "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        out.append(naive.replace(tzinfo=timezone.utc) + EASTERN_TO_UTC)
    return out


def _window(schedule_doc, now: datetime):
    """Today's game window, or ``None`` meaning "wide -- poll fast".

    ``None`` is returned for a missing, stale or unusable schedule snapshot.
    That is the design's fallback: ignorance widens the window, it never
    narrows it.
    """
    if not schedule_doc:
        return None
    try:
        captured = parse_iso(schedule_doc["captured_at"])
    except (KeyError, ValueError):
        return None
    if now - captured > SCHEDULE_TRUST_AGE:
        return None
    return due_mod.game_window(kickoffs(schedule_doc.get("payload")), now)


def _latest_final(payload):
    """(season, week) of the newest settled game, or None.  Feeds the
    player-stats fast phase: rows for that week are what it is waiting for."""
    best = None
    if not isinstance(payload, dict):
        return None
    for game in payload.get("games") or []:
        if not isinstance(game, dict) or not game.get("final"):
            continue
        key = (game.get("season"), game.get("week"))
        if not all(isinstance(part, int) for part in key):
            continue
        if best is None or key > best:
            best = key
    return best


def _rows_present(stats_doc, target) -> bool:
    """Have the week's stat rows appeared yet?  Unknown resolves to False,
    which keeps the fast cadence -- fail toward more data."""
    if target is None or not stats_doc:
        return False
    payload = stats_doc.get("payload") or {}
    lines = payload.get("stat_lines")
    rows = lines.values() if isinstance(lines, dict) else (lines or [])
    season, week = target
    return any(
        isinstance(row, dict) and row.get("season") == season and row.get("week") == week
        for row in rows
    )


# ---------------------------------------------------------------------------
# capture + preseason gate
# ---------------------------------------------------------------------------

def _capture(source, *, moment, http_get, runner, file_reader, etag, season):
    """Run one adapter and return the base result envelope.  Never raises."""
    try:
        if source == "nflverse_schedules":
            return nflverse_schedules.fetch(http_get, etag=etag)
        if source == "nflverse_player_stats":
            return nflverse_player_stats.fetch(http_get, season, etag=etag)
        if source == "trend_indicator":
            doc = TrendIndicatorAdapter().capture(moment, runner, file_reader)
            return {
                "status": doc.get("status", "ERROR"),
                "etag": doc.get("etag"),
                "payload": doc.get("payload"),
                "error": doc.get("error"),
            }
    except Exception as exc:  # an injected callable may raise anything
        return adapter_base.error_result(
            "%s capture raised %s: %s" % (source, type(exc).__name__, exc)
        )
    return adapter_base.error_result("no adapter wired for source %r" % source)


def filter_preseason(source: str, payload):
    """Drop PRE rows unless the caller opted in (design risk 2).

    Preseason is the data that exists RIGHT NOW, and running the pipeline on it
    is how August proves September.  But preseason lines are backups and the
    rosters churn, so by default it never reaches a snapshot at all -- filtering
    at CAPTURE rather than at the rules means the preseason noise is not even in
    the forensic record, and ``--include-preseason`` turns the whole pipe on
    without any rule change.
    """
    if not isinstance(payload, dict):
        return payload
    if source == "nflverse_schedules" and isinstance(payload.get("games"), list):
        kept = [g for g in payload["games"] if _phase_of(g) != "PRE"]
        out = dict(payload)
        out["games"] = kept
        out["row_count"] = len(kept)
        return out
    if source == "nflverse_player_stats" and isinstance(payload.get("stat_lines"), dict):
        kept = {k: v for k, v in payload["stat_lines"].items() if _phase_of(v) != "PRE"}
        out = dict(payload)
        out["stat_lines"] = kept
        out["row_count"] = len(kept)
        return out
    return payload


def _phase_of(row):
    if not isinstance(row, dict):
        return None
    for key in ("phase", "game_type", "season_type"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return None


# ---------------------------------------------------------------------------
# the tick
# ---------------------------------------------------------------------------

def run_tick(
    root,
    *,
    now: Callable[[], datetime] = system_now,
    http_get: Optional[Callable] = None,
    runner: Optional[Callable] = None,
    file_reader: Optional[Callable] = None,
    rules_dir=None,
    include_preseason: bool = False,
    dry_run: bool = False,
    season: Optional[int] = None,
    keep_snapshots: int = DEFAULT_KEEP_SNAPSHOTS,
    monotonic: Callable[[], float] = time.monotonic,
) -> TickResult:
    """Run one complete pass.  Always returns a :class:`TickResult`."""
    root = Path(root)
    state_dir = root / "state"
    queue_dir = root / "queue"
    rules_dir = Path(rules_dir) if rules_dir else root / "rules"
    http_get = http_get or adapter_base.urllib_http_get
    runner = runner or default_runner
    file_reader = file_reader or default_file_reader

    started = now()
    result = TickResult(root=root, started_at=started, dry_run=dry_run)
    elapsed_start = monotonic()

    def log(line):
        if dry_run:
            return
        try:
            rotate_mod.append_line(state_dir / TICK_LOG, line, now=now())
        except OSError as exc:
            result.errors.append("TICK.log write failed: %s" % exc)

    # -- 0. HALT ---------------------------------------------------------
    halt = state_dir / HALT_FILE
    if halt.exists():
        doc = _read_json(halt, default={}) or {}
        result.halted = True
        result.halt_reason = doc.get("reason") if isinstance(doc, dict) else None
        log("HALT %s reason=%s" % (iso_utc(started), result.halt_reason))
        return result

    store = SnapshotStore(state_dir, now=now, keep=keep_snapshots)
    event_log = EventLog(state_dir, clock=now)
    seen = SeenIndex(state_dir, clock=now)

    # -- 1. rules (loaded first: an invalid rule must be visible even if every
    #       source is down) ---------------------------------------------------
    loaded = load_rules(str(rules_dir))
    result.rules_loaded = len(loaded.rules)
    result.rules_invalid = len(loaded.errors)
    for error in loaded.errors:
        result.errors.append("rule invalid: %s" % error)
        log("RULE-INVALID %s" % error)

    # -- 2. due policy ---------------------------------------------------
    capture_state = _read_json(state_dir / CAPTURE_STATE_FILE, default={}) or {}
    last_captures = {}
    for source in SOURCES:
        raw = (capture_state.get(source) or {}).get("last_capture")
        try:
            last_captures[source] = parse_iso(raw) if raw else None
        except ValueError:
            last_captures[source] = None

    schedule_doc = _latest_usable(store, "nflverse_schedules")
    stats_doc = _latest_usable(store, "nflverse_player_stats")
    window = _window(schedule_doc, started)
    result.mode = "game_window" if due_mod.in_game_window(started, window) else "idle"
    rows_present = _rows_present(
        stats_doc, _latest_final((schedule_doc or {}).get("payload"))
    )
    due = due_mod.due_sources(last_captures, started, window, rows_present)

    # -- 3. capture -> snapshot -> delta ---------------------------------
    season = season if season is not None else nfl_season(started)
    events = []
    for source in due:
        etag = (capture_state.get(source) or {}).get("etag")
        moment = now()
        outcome = _capture(
            source,
            moment=moment,
            http_get=http_get,
            runner=runner,
            file_reader=file_reader,
            etag=etag,
            season=season,
        )
        status = outcome.get("status") or "ERROR"
        payload = outcome.get("payload")
        error = outcome.get("error")
        if status == "OK" and not include_preseason:
            payload = filter_preseason(source, payload)
        if status == "ERROR" and not error:
            error = "%s returned ERROR without a reason" % source
        result.captured[source] = status
        if error:
            result.errors.append("%s: %s" % (source, error))

        prev_doc = _latest_usable(store, source)
        curr_doc = {
            "source": source,
            "status": status,
            "captured_at": iso_utc(moment),
            "payload": payload,
        }
        if not dry_run:
            try:
                store.write(
                    source,
                    payload=payload,
                    status=status,
                    etag=outcome.get("etag"),
                    error=error,
                )
            except (OSError, ValueError) as exc:
                result.errors.append("snapshot write failed for %s: %s" % (source, exc))

            entry = dict(capture_state.get(source) or {})
            entry["last_capture"] = iso_utc(moment)
            entry["status"] = status
            if status in ("OK", "UNCHANGED"):
                entry["last_ok"] = iso_utc(moment)
            if outcome.get("etag"):
                entry["etag"] = outcome["etag"]
            capture_state[source] = entry

        try:
            events.extend(diff_snapshots(prev_doc, curr_doc, moment))
        except Exception as exc:  # a ragged payload must never abort a tick
            result.errors.append("delta failed for %s: %s" % (source, exc))
        log("CAPTURE %s status=%s due=1" % (source, status))

    if not dry_run and due:
        try:
            _write_json_atomic(state_dir / CAPTURE_STATE_FILE, capture_state)
        except OSError as exc:
            result.errors.append("last-capture write failed: %s" % exc)

    result.events = events

    # -- 4. event log + seen-index ---------------------------------------
    if dry_run:
        fresh, batch = [], []
        for event in events:
            if event.event_id in batch or seen.has(event.event_id):
                continue
            batch.append(event.event_id)
            fresh.append(event)
        result.recorded = fresh
    else:
        try:
            result.recorded = record_events(event_log, seen, events)
        except OSError as exc:
            result.errors.append("event log write failed: %s" % exc)
            result.recorded = []
    for event in result.recorded:
        log("EVENT %s %s %s" % (event.type, event.event_id, event.source))

    # -- 5. rules -> actions ---------------------------------------------
    throttle = ThrottleState.load(str(state_dir / THROTTLE_FILE), now=now)
    engine = RuleEngine(loaded.rules, throttle, now=now)
    evaluation = engine.evaluate(
        [event.to_dict() for event in result.recorded], record=not dry_run
    )
    result.actions = evaluation.actions

    # -- 6. dispatch -> queue ---------------------------------------------
    for action in result.actions:
        log("FIRE %s event=%s kind=%s" % (action["rule_id"], action["event_id"], action["kind"]))
        if dry_run:
            continue
        task = action.get("task") or {}
        try:
            task_id = enqueue_task(
                queue_dir,
                rule_id=action["rule_id"],
                event_id=action["event_id"],
                kind=task.get("kind") or action["kind"],
                priority=action["priority"],
                ttl=action["ttl"] or "1d",
                args={k: v for k, v in task.items() if k != "kind"},
                now=now(),
            )
            result.enqueued.append(task_id)
        except Exception as exc:
            result.errors.append(
                "enqueue failed for %s/%s: %s" % (action["rule_id"], action["event_id"], exc)
            )
            continue
        if action["kind"] == "notify":
            _append_inbox(state_dir, task.get("message") or "", now(), result)

    # -- 7. status --------------------------------------------------------
    result.tick_ms = int((monotonic() - elapsed_start) * 1000)
    result.status = _build_status(
        state_dir=state_dir,
        queue_dir=queue_dir,
        capture_state=capture_state,
        result=result,
        moment=started,
        evaluation=evaluation,
        event_log=event_log,
    )
    if not dry_run:
        try:
            write_hub_status(state_dir, result.status, now=now(), last_tick=started)
        except OSError as exc:
            result.errors.append("hub-status write failed: %s" % exc)

    # -- 8. rotation ------------------------------------------------------
    if not dry_run:
        for prune, label in (
            (lambda: store.prune_all(), "snapshots"),
            (lambda: event_log.prune(), "event log"),
            (lambda: seen.prune(), "seen-index"),
        ):
            try:
                prune()
            except (OSError, ValueError) as exc:
                result.errors.append("%s prune failed: %s" % (label, exc))

    log(
        "TICK %s mode=%s due=%d events=%d fired=%d enqueued=%d errors=%d ms=%d"
        % (
            iso_utc(started), result.mode, len(due), len(result.recorded),
            len(result.actions), len(result.enqueued), len(result.errors),
            result.tick_ms,
        )
    )

    # -- 9. heartbeat, LAST ------------------------------------------------
    # Written only at the END of a completed tick.  A heartbeat written up
    # front leaves a crashed tick looking healthy while capture is silently
    # dead -- the backup-fleet lesson, verbatim.
    if not dry_run:
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / HEARTBEAT_FILE).write_text(
                str(int(started.timestamp())), encoding="ascii"
            )
        except OSError as exc:
            result.errors.append("heartbeat write failed: %s" % exc)

    return result


def _append_inbox(state_dir: Path, message: str, moment: datetime, result: TickResult):
    """R003's steer note.  `notify` is a task too, so this is an ADDITION to the
    queue entry, never a replacement for it (design section 5)."""
    if not message:
        return
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        with open(state_dir / INBOX_OUT, "a", encoding="utf-8") as handle:
            handle.write("- %s %s\n" % (iso_utc(moment), message))
    except OSError as exc:
        result.errors.append("INBOX-OUT append failed: %s" % exc)


def _build_status(
    *, state_dir, queue_dir, capture_state, result, moment, evaluation, event_log
):
    sources = {}
    for source in SOURCES:
        entry = capture_state.get(source) or {}
        sources[source] = {
            "last_ok": entry.get("last_ok"),
            "status": result.captured.get(source, entry.get("status", "SKIPPED")),
        }

    by_type = {}
    for event in result.recorded:
        by_type[event.type] = by_type.get(event.type, 0) + 1

    try:
        events_today = event_log.count(day=moment.strftime("%Y-%m-%d"))
    except OSError:
        events_today = len(result.recorded)

    return {
        "tick_ms": result.tick_ms,
        "mode": result.mode,
        "sources": sources,
        "events_today": events_today,
        "events_by_type": by_type,
        "rules": {
            "loaded": result.rules_loaded,
            "invalid": result.rules_invalid,
            "fired_today": evaluation.stats.get("fired", 0),
            "throttled_today": evaluation.stats.get("throttled", 0),
        },
        "queue": _queue_stats(queue_dir, moment),
        "alarms": [],
    }


def _queue_stats(queue_dir: Path, moment: datetime) -> dict:
    def count(box):
        d = queue_dir / box
        return len(list(d.glob("*.task.json"))) if d.is_dir() else 0

    oldest = 0
    pending_dir = queue_dir / "pending"
    if pending_dir.is_dir():
        ages = []
        for path in pending_dir.glob("*.task.json"):
            doc = _read_json(path, default={}) or {}
            raw = doc.get("created_at")
            try:
                ages.append(int((moment - parse_iso(raw)).total_seconds()))
            except (TypeError, ValueError):
                continue
        oldest = max(ages) if ages else 0

    expired = 0
    failed_dir = queue_dir / "failed"
    if failed_dir.is_dir():
        today = moment.strftime("%Y-%m-%d")
        for path in failed_dir.glob("*.task.json"):
            doc = _read_json(path, default={}) or {}
            if doc.get("reason") == "expired" and str(doc.get("expires_at", ""))[:10] <= today:
                expired += 1

    return {
        "pending": count("pending"),
        "claimed": count("claimed"),
        "oldest_pending_age_s": oldest,
        "expired_today": expired,
    }
