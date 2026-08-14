"""Crash, replay and dedup tests across delta + log + seen (design section 3, lane L4).

Design risk 4: "dedup correctness is the top correctness risk". These are the
adversarial cases -- a correction, a reordered pair, a missing previous, a duplicate
tick, and a crash landing in the one-instruction gap between the log append and the
seen-mark.

THE RECOVERY ORDER, and why: append the JSONL line first, mark the id second.

  crash after log, before mark   -> the event is re-emitted next tick: one duplicate
                                    log line, same event_id, nothing lost.
  crash after mark, before log   -> (rejected) the event is marked handled but was
                                    never written. It is invisible to the log, to
                                    forensics, and to any rebuild -- permanently lost.

Duplication is recoverable and cheap; loss is neither. So the log is at-least-once and
ACTION is exactly-once, enforced downstream by the rules layer's permanent
(rule_id, event_id) gate -- never by claiming a filesystem pipeline delivers once.
"""

from datetime import datetime, timedelta, timezone

import pytest

from signal_hub.events import delta
from signal_hub.events.log import EventLog, record_events
from signal_hub.events.seen import SeenIndex


TICK_1 = datetime(2026, 9, 14, 23, 41, 0, tzinfo=timezone.utc)
TICK_2 = datetime(2026, 9, 15, 0, 11, 0, tzinfo=timezone.utc)
TICK_3 = datetime(2026, 9, 15, 0, 41, 0, tzinfo=timezone.utc)


class Clock:
    """Injected, movable, never wall-clock."""

    def __init__(self, moment):
        self.moment = moment

    def __call__(self):
        return self.moment


def pipeline(tmp_path, moment):
    clock = Clock(moment)
    return clock, EventLog(tmp_path / "state", clock=clock), SeenIndex(
        tmp_path / "state", clock=clock
    )


def snapshot(source, payload, captured_at, status="OK"):
    return {
        "source": source,
        "schema_version": 1,
        "captured_at": captured_at,
        "status": status,
        "payload": payload,
    }


def stat_snapshot(captured_at, passing_yards=312, rows=1, **over):
    stats = []
    for i in range(rows):
        row = {
            "player_id": "00-003632%d" % i,
            "player_name": "Player %d" % i,
            "team": "CHI",
            "game_id": "2026_02_CHI_DET",
            "game_type": "REG",
            "passing_yards": passing_yards,
            "passing_tds": 3,
        }
        row.update(over)
        stats.append(row)
    return snapshot("nflverse_player_stats", {"stats": stats}, captured_at)


EMPTY_STATS = snapshot("nflverse_player_stats", {"stats": []}, "2026-09-14T23:11:00Z")


def log_lines(tmp_path, day="2026-09-14"):
    path = tmp_path / "state" / "events" / ("%s.jsonl" % day)
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --------------------------------------------------------------------------
# the happy path, then the same input again
# --------------------------------------------------------------------------

def test_a_tick_records_its_events(tmp_path):
    clock, log, seen = pipeline(tmp_path, TICK_1)
    events = delta.diff_snapshots(EMPTY_STATS, stat_snapshot("2026-09-14T23:41:00Z"), clock())
    recorded = record_events(log, seen, events)
    assert len(recorded) == 1
    assert len(log_lines(tmp_path)) == 1
    assert seen.has(recorded[0].event_id)


def test_a_duplicate_tick_records_nothing_new(tmp_path):
    """The scheduled task fires every 5 minutes against snapshots that did not move."""
    clock, log, seen = pipeline(tmp_path, TICK_1)
    prev, curr = EMPTY_STATS, stat_snapshot("2026-09-14T23:41:00Z")
    record_events(log, seen, delta.diff_snapshots(prev, curr, clock()))
    clock.moment = TICK_2
    again = record_events(log, seen, delta.diff_snapshots(prev, curr, clock()))
    assert again == []
    assert len(log_lines(tmp_path)) == 1


def test_a_duplicate_tick_is_still_free_after_a_restart(tmp_path):
    clock, log, seen = pipeline(tmp_path, TICK_1)
    prev, curr = EMPTY_STATS, stat_snapshot("2026-09-14T23:41:00Z")
    record_events(log, seen, delta.diff_snapshots(prev, curr, clock()))
    _, log2, seen2 = pipeline(tmp_path, TICK_2)  # fresh process, nothing in memory
    assert record_events(log2, seen2, delta.diff_snapshots(prev, curr, TICK_1)) == []
    assert len(log_lines(tmp_path)) == 1


# --------------------------------------------------------------------------
# corrections
# --------------------------------------------------------------------------

def test_a_revised_stat_line_never_reaches_the_log_twice(tmp_path):
    """nflverse revises stat lines after the fact. Both defences hold: the delta engine
    does not re-emit, and the id would collide in the seen-index if it did."""
    clock, log, seen = pipeline(tmp_path, TICK_1)
    first = stat_snapshot("2026-09-14T23:41:00Z", passing_yards=298)
    corrected = stat_snapshot("2026-09-15T00:11:00Z", passing_yards=312)
    original = record_events(log, seen, delta.diff_snapshots(EMPTY_STATS, first, clock()))
    clock.moment = TICK_2
    revision = record_events(log, seen, delta.diff_snapshots(first, corrected, clock()))
    assert len(original) == 1
    assert revision == []
    assert len(log_lines(tmp_path)) == 1


def test_a_correction_that_is_forced_through_the_diff_still_dedups(tmp_path):
    """Belt and braces: hand the recorder the re-derived event directly, as a replayed
    or manually re-diffed snapshot pair would."""
    clock, log, seen = pipeline(tmp_path, TICK_1)
    first = stat_snapshot("2026-09-14T23:41:00Z", passing_yards=298)
    corrected = stat_snapshot("2026-09-15T00:11:00Z", passing_yards=312)
    record_events(log, seen, delta.diff_snapshots(EMPTY_STATS, first, clock()))
    clock.moment = TICK_2
    forced = delta.diff_snapshots(EMPTY_STATS, corrected, clock())
    assert len(forced) == 1
    assert record_events(log, seen, forced) == []
    assert len(log_lines(tmp_path)) == 1


def test_action_fires_exactly_once_across_a_correction(tmp_path):
    """The rules layer's permanent (rule_id, event_id) gate, simulated. This is what
    'exactly-once in action' actually rests on."""
    clock, log, seen = pipeline(tmp_path, TICK_1)
    fired = set()
    actions = []

    def fire(rule_id, events):
        for event in events:
            key = (rule_id, event.event_id)
            if key in fired:
                continue
            fired.add(key)
            actions.append(key)

    first = stat_snapshot("2026-09-14T23:41:00Z", passing_yards=298)
    corrected = stat_snapshot("2026-09-15T00:11:00Z", passing_yards=312)
    fire("R002", record_events(log, seen, delta.diff_snapshots(EMPTY_STATS, first, clock())))
    clock.moment = TICK_2
    fire("R002", delta.diff_snapshots(EMPTY_STATS, corrected, clock()))  # replayed
    assert len(actions) == 1


# --------------------------------------------------------------------------
# crash between the two writes
# --------------------------------------------------------------------------

def test_crash_between_log_append_and_seen_mark_loses_nothing(tmp_path):
    clock, log, seen = pipeline(tmp_path, TICK_1)
    events = delta.diff_snapshots(EMPTY_STATS, stat_snapshot("2026-09-14T23:41:00Z"), clock())

    def die(_event_id):
        raise KeyboardInterrupt("power cut between the log append and the seen mark")

    seen.mark = die
    with pytest.raises(KeyboardInterrupt):
        record_events(log, seen, events)

    assert len(log_lines(tmp_path)) == 1  # the log line survived the crash
    _, log2, seen2 = pipeline(tmp_path, TICK_2)
    assert seen2.load() == 0  # the id was never marked


def test_the_tick_after_that_crash_re_emits_at_least_once(tmp_path):
    clock, log, seen = pipeline(tmp_path, TICK_1)
    prev, curr = EMPTY_STATS, stat_snapshot("2026-09-14T23:41:00Z")
    events = delta.diff_snapshots(prev, curr, clock())
    seen.mark = lambda _id: (_ for _ in ()).throw(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        record_events(log, seen, events)

    # next tick, fresh process
    _, log2, seen2 = pipeline(tmp_path, TICK_2)
    replayed = record_events(log2, seen2, delta.diff_snapshots(prev, curr, TICK_1))
    lines = log_lines(tmp_path)
    assert len(replayed) == 1
    assert len(lines) == 2  # at-least-once in the log: a duplicate line, by design
    assert lines[0] == lines[1]  # byte-identical, same event_id -- action dedups on it

    # and it settles: the tick after that adds nothing
    _, log3, seen3 = pipeline(tmp_path, TICK_3)
    assert record_events(log3, seen3, delta.diff_snapshots(prev, curr, TICK_1)) == []
    assert len(log_lines(tmp_path)) == 2


def test_the_duplicate_log_line_still_fires_the_action_only_once(tmp_path):
    """The residual known gap (design risk 4) is harmless precisely because the action
    side dedups on event_id independently of the log."""
    clock, log, seen = pipeline(tmp_path, TICK_1)
    prev, curr = EMPTY_STATS, stat_snapshot("2026-09-14T23:41:00Z")
    seen.mark = lambda _id: (_ for _ in ()).throw(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        record_events(log, seen, delta.diff_snapshots(prev, curr, clock()))
    _, log2, seen2 = pipeline(tmp_path, TICK_2)
    record_events(log2, seen2, delta.diff_snapshots(prev, curr, TICK_1))

    ids = [record["event_id"] for record in log2.read_day("2026-09-14")]
    assert len(ids) == 2
    assert len(set(ids)) == 1


def test_a_crash_partway_through_a_batch_keeps_the_earlier_events(tmp_path):
    """Per-event ordering, not per-batch: whatever got through is durable."""
    clock, log, seen = pipeline(tmp_path, TICK_1)
    events = delta.diff_snapshots(EMPTY_STATS, stat_snapshot("2026-09-14T23:41:00Z", rows=3), clock())
    assert len(events) == 3
    real_mark = seen.mark
    calls = {"n": 0}

    def flaky(event_id):
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt("crash on the third event")
        return real_mark(event_id)

    seen.mark = flaky
    with pytest.raises(KeyboardInterrupt):
        record_events(log, seen, events)
    assert len(log_lines(tmp_path)) == 3  # all three log lines were written first

    _, log2, seen2 = pipeline(tmp_path, TICK_2)
    replayed = record_events(log2, seen2, delta.diff_snapshots(EMPTY_STATS, stat_snapshot("2026-09-14T23:41:00Z", rows=3), TICK_1))
    assert len(replayed) == 1  # only the un-marked third event comes back
    assert len(log_lines(tmp_path)) == 4


def test_a_lost_seen_index_can_be_rebuilt_from_the_log(tmp_path):
    """The log is the durable record, which is the whole reason it is written first."""
    clock, log, seen = pipeline(tmp_path, TICK_1)
    record_events(log, seen, delta.diff_snapshots(EMPTY_STATS, stat_snapshot("2026-09-14T23:41:00Z", rows=2), clock()))
    for partition in (tmp_path / "state" / ".events-seen").glob("*.ids"):
        partition.unlink()

    _, log2, seen2 = pipeline(tmp_path, TICK_2)
    assert seen2.load() == 0
    assert len(seen2.mark_many(sorted(log2.event_ids()))) == 2
    assert record_events(log2, seen2, delta.diff_snapshots(EMPTY_STATS, stat_snapshot("2026-09-14T23:41:00Z", rows=2), TICK_1)) == []


# --------------------------------------------------------------------------
# snapshot pathologies
# --------------------------------------------------------------------------

def test_reordered_snapshots_record_nothing(tmp_path):
    """A replay or a clock skew hands the engine (n, n-1). Diffing backwards would
    re-open a settled transition."""
    clock, log, seen = pipeline(tmp_path, TICK_1)
    older = EMPTY_STATS
    newer = stat_snapshot("2026-09-14T23:41:00Z")
    assert record_events(log, seen, delta.diff_snapshots(newer, older, clock())) == []
    assert log_lines(tmp_path) == []


def test_a_missing_previous_snapshot_records_nothing(tmp_path):
    clock, log, seen = pipeline(tmp_path, TICK_1)
    assert record_events(log, seen, delta.diff_snapshots(None, stat_snapshot("2026-09-14T23:41:00Z"), clock())) == []
    assert log_lines(tmp_path) == []


def test_an_error_snapshot_records_nothing(tmp_path):
    """A broken upstream produces silence, not garbage events (design section 2a)."""
    clock, log, seen = pipeline(tmp_path, TICK_1)
    broken = snapshot("nflverse_player_stats", None, "2026-09-14T23:41:00Z", status="ERROR")
    assert record_events(log, seen, delta.diff_snapshots(EMPTY_STATS, broken, clock())) == []
    assert log_lines(tmp_path) == []


def test_a_gap_of_hours_still_yields_the_terminal_events(tmp_path):
    """Design risk 6: state-diff, not stream. A sleeping machine misses ticks, not
    outcomes -- game_final is visible in the snapshot whenever the machine wakes."""
    clock, log, seen = pipeline(tmp_path, TICK_1 + timedelta(hours=9))
    before = snapshot(
        "nflverse_schedules",
        {"games": [{"game_id": "2026_02_CHI_DET", "game_type": "REG",
                    "away_team": "CHI", "home_team": "DET"}]},
        "2026-09-14T14:00:00Z",
    )
    after = snapshot(
        "nflverse_schedules",
        {"games": [{"game_id": "2026_02_CHI_DET", "game_type": "REG",
                    "away_team": "CHI", "home_team": "DET",
                    "away_score": 24, "home_score": 21}]},
        "2026-09-15T08:41:00Z",
    )
    recorded = record_events(log, seen, delta.diff_snapshots(before, after, clock()))
    assert [event.payload["winner"] for event in recorded] == ["CHI"]


def test_two_demand_moves_on_one_day_record_once(tmp_path):
    """The (opportunity, utc-date) identity IS the daily throttle."""
    clock, log, seen = pipeline(tmp_path, TICK_1)

    def ranks(score, captured_at):
        return snapshot(
            "trend_indicator",
            {"rankings": [{"opportunity": "nfl-shorts-general", "score": score,
                           "confidence": "medium"}]},
            captured_at,
        )

    morning = ranks(0.20, "2026-09-14T06:00:00Z")
    midday = ranks(0.55, "2026-09-14T12:00:00Z")
    evening = ranks(0.90, "2026-09-14T18:00:00Z")
    clock.moment = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    first = record_events(log, seen, delta.diff_snapshots(morning, midday, clock()))
    clock.moment = datetime(2026, 9, 14, 18, 0, tzinfo=timezone.utc)
    second = record_events(log, seen, delta.diff_snapshots(midday, evening, clock()))
    assert len(first) == 1
    assert second == []

    # a new UTC day is a new bucket, so the signal is not suppressed forever
    clock.moment = datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc)
    tomorrow = ranks(0.30, "2026-09-15T06:00:00Z")
    assert len(record_events(log, seen, delta.diff_snapshots(evening, tomorrow, clock()))) == 1


def test_every_logged_record_verifies_against_its_own_identity(tmp_path):
    """End-to-end integrity: nothing in the pipeline writes an id that its entities and
    payload cannot re-derive."""
    clock, log, seen = pipeline(tmp_path, TICK_1)
    record_events(log, seen, delta.diff_snapshots(EMPTY_STATS, stat_snapshot("2026-09-14T23:41:00Z", rows=3), clock()))
    records = log.read_day("2026-09-14")
    assert len(records) == 3
    for record in records:
        delta.Event.from_dict(record)  # raises if the id does not match the identity
