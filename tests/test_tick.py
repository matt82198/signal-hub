"""L7 integration: the orchestrated tick (design sections 2c, 3, 4, 5, 6).

Every test here drives the WHOLE pipeline -- due policy, adapters, snapshot
store, delta engine, event log + seen-index, rule engine, queue, status,
rotation -- against the REAL rule files in ``rules/``.  Nothing is mocked
except the two seams the design makes mandatory: ``now`` (FrozenClock) and
``http_get`` / ``runner`` / ``file_reader``.  No test touches the network.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from signal_hub import tick as tick_mod
from signal_hub.adapters.base import HttpResponse
from signal_hub.clock import FrozenClock
from signal_hub.snapshots import SnapshotStore

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_DIR = REPO_ROOT / "rules"

# Kickoff 2026-09-14 20:15 ET == 2026-09-15 00:15Z; T0 sits inside the window.
T0 = datetime(2026, 9, 15, 3, 30, 0, tzinfo=timezone.utc)

SCHED_HEADER = (
    "game_id,season,game_type,week,gameday,gametime,"
    "away_team,away_score,home_team,home_score"
)
GAME_NOT_FINAL = "2026_02_GB_CHI,2026,REG,2,2026-09-14,20:15,GB,,CHI,"
GAME_BEARS_WIN = "2026_02_GB_CHI,2026,REG,2,2026-09-14,20:15,GB,17,CHI,24"
GAME_PRE_FINAL = "2026_01_MIN_CHI,2026,PRE,1,2026-09-14,19:00,MIN,10,CHI,31"

STATS_HEADER = (
    "player_id,player_display_name,position,season,week,season_type,game_id,"
    "team,opponent_team,completions,attempts,passing_yards,passing_tds,"
    "carries,rushing_yards,rushing_tds,receptions,receiving_yards,"
    "receiving_tds,special_teams_tds"
)
STAT_BIG_LINE = (
    "00-0036322,Justin Fields,QB,2026,2,REG,2026_02_GB_CHI,CHI,GB,"
    "24,33,312,3,7,44,0,0,0,0,0"
)

RANKINGS = {
    "generated_at": "2026-09-15T00:00:00Z",
    "rankings": [
        {"opportunity": "bears-highlights", "score": 0.71, "confidence": "high"}
    ],
}


def sched_csv(*rows):
    return ("\n".join([SCHED_HEADER, *rows]) + "\n").encode("utf-8")


def stats_csv(*rows):
    return ("\n".join([STATS_HEADER, *rows]) + "\n").encode("utf-8")


def make_http(sched_body, stats_status=404, stats_body=b""):
    """Injected http_get.  Raises on any URL the pipeline should not request."""
    calls = []

    def http_get(url, headers=None, timeout=30):
        calls.append(url)
        if "games.csv" in url:
            return HttpResponse(200, {"ETag": 'W/"sched-1"'}, sched_body)
        if "stats_player_week" in url:
            return HttpResponse(stats_status, {"ETag": 'W/"stats-1"'}, stats_body)
        raise AssertionError("unexpected URL requested: %s" % url)

    http_get.calls = calls
    return http_get


def ok_runner(cmd, cwd=None):
    return 0


def rankings_reader(path):
    return RANKINGS


def run(root, clock, http_get, **kwargs):
    kwargs.setdefault("runner", ok_runner)
    kwargs.setdefault("file_reader", rankings_reader)
    kwargs.setdefault("rules_dir", RULES_DIR)
    return tick_mod.run_tick(root, now=clock, http_get=http_get, **kwargs)


def seed_baseline(root, clock, csv_bytes, include_preseason=False):
    """Tick once to lay down the cold-start baseline snapshots."""
    return run(root, clock, make_http(csv_bytes), include_preseason=include_preseason)


def pending(root):
    d = Path(root) / "queue" / "pending"
    return sorted(p.name for p in d.glob("*.task.json")) if d.is_dir() else []


def read_status(root):
    return json.loads((Path(root) / "state" / "hub-status.json").read_text("utf-8"))


# ---------------------------------------------------------------------------
# cold start
# ---------------------------------------------------------------------------

def test_cold_start_captures_but_emits_no_events(tmp_path):
    clock = FrozenClock(T0)
    result = run(tmp_path, clock, make_http(sched_csv(GAME_BEARS_WIN)))

    assert result.halted is False
    # A first-ever snapshot has no delta: games.csv carries decades of settled
    # games and diffing against nothing would fire hundreds of tasks on install.
    assert result.events == []
    assert result.enqueued == []
    assert pending(tmp_path) == []
    store = SnapshotStore(tmp_path / "state")
    assert store.latest("nflverse_schedules").status == "OK"


def test_cold_start_still_writes_status_and_heartbeat(tmp_path):
    clock = FrozenClock(T0)
    run(tmp_path, clock, make_http(sched_csv(GAME_NOT_FINAL)))

    status = read_status(tmp_path)
    assert status["mode"] in ("game_window", "idle")
    assert status["rules"]["loaded"] == 6
    assert status["rules"]["invalid"] == 0
    # Heartbeat is written at the END of a completed tick, never at the start.
    assert (tmp_path / "state" / ".signal-hub-heartbeat").is_file()


# ---------------------------------------------------------------------------
# the end-to-end proof: R001 fires exactly once on a Bears final win
# ---------------------------------------------------------------------------

def test_e2e_bears_win_fires_r001_once_and_second_tick_refires_nothing(tmp_path):
    clock = FrozenClock(T0)
    seed_baseline(tmp_path, clock, sched_csv(GAME_NOT_FINAL))

    clock.advance(timedelta(minutes=20))
    result = run(tmp_path, clock, make_http(sched_csv(GAME_BEARS_WIN)))

    # -- events -----------------------------------------------------------
    finals = [e for e in result.events if e.type == "game_final"]
    assert len(finals) == 1
    event = finals[0]
    assert event.payload["winner"] == "CHI"
    assert event.payload["game_type"] == "REG"

    logged = json.loads(
        (tmp_path / "state" / "events" / "2026-09-15.jsonl")
        .read_text("utf-8")
        .strip()
        .splitlines()[-1]
    )
    assert logged["event_id"] == event.event_id
    seen_ids = (tmp_path / "state" / ".events-seen" / "2026-09-15.ids").read_text("utf-8")
    assert event.event_id in seen_ids

    # -- rule fired -------------------------------------------------------
    fired = [a for a in result.actions if a["rule_id"] == "R001-bears-game-final-win"]
    assert len(fired) == 1

    # -- task file on disk, correct schema --------------------------------
    names = pending(tmp_path)
    assert len(names) == 1
    task = json.loads((tmp_path / "queue" / "pending" / names[0]).read_text("utf-8"))
    assert set(task) >= {
        "task_id", "created_at", "expires_at", "rule_id", "event_id",
        "kind", "priority", "status", "args",
    }
    assert task["rule_id"] == "R001-bears-game-final-win"
    assert task["event_id"] == event.event_id
    assert task["kind"] == "instantiate_template"
    assert task["status"] == "pending"
    assert task["priority"] == 90
    assert task["args"]["template"] == "gamehighlight"
    assert task["args"]["account"] == "ballmoments_main"
    assert task["args"]["needs_footage"] is True
    assert task["args"]["fills_hints"]["game"] == "2026_02_GB_CHI"
    # created_at + ttl 2d == expires_at
    assert task["created_at"] == "2026-09-15T03:50:00Z"
    assert task["expires_at"] == "2026-09-17T03:50:00Z"

    # -- second tick: dedup, nothing refires -------------------------------
    clock.advance(timedelta(hours=7))
    again = run(tmp_path, clock, make_http(sched_csv(GAME_BEARS_WIN)))
    assert again.events == []
    assert again.actions == []
    assert again.enqueued == []
    assert pending(tmp_path) == names

    status = read_status(tmp_path)
    assert status["queue"]["pending"] == 1
    assert status["rules"]["loaded"] == 6


def test_status_json_is_coherent_after_a_firing_tick(tmp_path):
    clock = FrozenClock(T0)
    seed_baseline(tmp_path, clock, sched_csv(GAME_NOT_FINAL))
    clock.advance(timedelta(minutes=20))
    run(tmp_path, clock, make_http(sched_csv(GAME_BEARS_WIN)))

    status = read_status(tmp_path)
    assert status["last_tick"] == "2026-09-15T03:50:00Z"
    assert status["mode"] == "game_window"
    assert status["events_today"] >= 1
    assert status["events_by_type"]["game_final"] == 1
    assert status["rules"]["fired_today"] == 1
    assert status["queue"]["pending"] == 1
    assert status["queue"]["claimed"] == 0
    assert status["queue"]["oldest_pending_age_s"] == 0
    assert status["sources"]["nflverse_schedules"]["status"] == "OK"
    assert status["sources"]["nflverse_schedules"]["age_s"] == 0
    assert isinstance(status["alarms"], list)
    assert isinstance(status["tick_ms"], int)


# ---------------------------------------------------------------------------
# dry run
# ---------------------------------------------------------------------------

def test_dry_run_computes_the_fire_but_writes_nothing(tmp_path):
    clock = FrozenClock(T0)
    seed_baseline(tmp_path, clock, sched_csv(GAME_NOT_FINAL))
    before = _fingerprint(tmp_path)

    clock.advance(timedelta(minutes=20))
    result = run(tmp_path, clock, make_http(sched_csv(GAME_BEARS_WIN)), dry_run=True)

    assert result.dry_run is True
    assert [a["rule_id"] for a in result.actions] == ["R001-bears-game-final-win"]
    assert result.enqueued == []
    assert pending(tmp_path) == []
    assert not (tmp_path / "queue" / "pending").exists() or pending(tmp_path) == []
    # A dry run must leave the hub exactly as it found it -- snapshots, event
    # log, seen-index, throttle state, status and heartbeat all untouched.
    assert _fingerprint(tmp_path) == before


def test_dry_run_then_real_run_still_fires(tmp_path):
    clock = FrozenClock(T0)
    seed_baseline(tmp_path, clock, sched_csv(GAME_NOT_FINAL))
    clock.advance(timedelta(minutes=20))
    http = make_http(sched_csv(GAME_BEARS_WIN))
    run(tmp_path, clock, http, dry_run=True)
    result = run(tmp_path, clock, http)
    assert len(result.enqueued) == 1


def _fingerprint(root):
    root = Path(root)
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = path.read_bytes()
    return out


# ---------------------------------------------------------------------------
# failure isolation -- one source dying must not kill the tick
# ---------------------------------------------------------------------------

def test_one_source_failing_does_not_kill_the_tick(tmp_path):
    clock = FrozenClock(T0)

    def http_get(url, headers=None, timeout=30):
        if "games.csv" in url:
            raise OSError("simulated DNS failure")
        return HttpResponse(404, {}, b"")

    result = run(tmp_path, clock, http_get)

    assert result.captured["nflverse_schedules"] == "ERROR"
    assert result.captured["nflverse_player_stats"] == "ERROR"
    # trend_indicator uses the subprocess seam and is unaffected.
    assert result.captured["trend_indicator"] == "OK"
    store = SnapshotStore(tmp_path / "state")
    bad = store.latest("nflverse_schedules")
    assert bad.status == "ERROR" and bad.error  # inputs always produce outputs
    assert read_status(tmp_path)["sources"]["trend_indicator"]["status"] == "OK"


def test_adapter_returning_garbage_is_recorded_not_raised(tmp_path):
    clock = FrozenClock(T0)

    def http_get(url, headers=None, timeout=30):
        if "games.csv" in url:
            return HttpResponse(200, {}, b"not,a,schedule\n1,2,3\n")
        return HttpResponse(404, {}, b"")

    result = run(tmp_path, clock, http_get)
    assert result.captured["nflverse_schedules"] == "ERROR"
    assert result.errors  # surfaced, not swallowed


def test_broken_trend_runner_yields_stale_and_no_events(tmp_path):
    clock = FrozenClock(T0)

    def bad_runner(cmd, cwd=None):
        return 3

    result = run(
        tmp_path, clock, make_http(sched_csv(GAME_NOT_FINAL)), runner=bad_runner
    )
    assert result.captured["trend_indicator"] == "STALE"
    assert result.events == []


# ---------------------------------------------------------------------------
# HALT sentinel
# ---------------------------------------------------------------------------

def test_halt_sentinel_short_circuits_before_any_capture(tmp_path):
    (tmp_path / "state").mkdir(parents=True)
    (tmp_path / "state" / ".HALT").write_text(
        json.dumps({"reason": "maintenance"}), encoding="utf-8"
    )
    clock = FrozenClock(T0)
    http = make_http(sched_csv(GAME_BEARS_WIN))
    result = run(tmp_path, clock, http)

    assert result.halted is True
    assert result.halt_reason == "maintenance"
    assert http.calls == []
    assert not (tmp_path / "state" / ".signal-hub-heartbeat").exists()


# ---------------------------------------------------------------------------
# preseason gate (design risk 2)
# ---------------------------------------------------------------------------

def test_preseason_rows_are_dropped_by_default(tmp_path):
    clock = FrozenClock(T0)
    seed_baseline(tmp_path, clock, sched_csv(GAME_NOT_FINAL))
    clock.advance(timedelta(minutes=20))
    result = run(tmp_path, clock, make_http(sched_csv(GAME_NOT_FINAL, GAME_PRE_FINAL)))

    assert result.events == []
    store = SnapshotStore(tmp_path / "state")
    phases = {g["phase"] for g in store.latest("nflverse_schedules").payload["games"]}
    assert phases == {"REG"}


def test_include_preseason_flows_events_but_fires_no_task(tmp_path):
    clock = FrozenClock(T0)
    seed_baseline(
        tmp_path, clock, sched_csv(GAME_NOT_FINAL), include_preseason=True
    )
    clock.advance(timedelta(minutes=20))
    result = run(
        tmp_path,
        clock,
        make_http(sched_csv(GAME_NOT_FINAL, GAME_PRE_FINAL)),
        include_preseason=True,
    )

    # The whole pipeline is exercised on preseason data...
    assert [e.type for e in result.events] == ["game_final"]
    assert result.events[0].payload["game_type"] == "PRE"
    # ...and every MVP rule filters to REG/POST, so no content task fires.
    assert result.enqueued == []
    assert pending(tmp_path) == []


# ---------------------------------------------------------------------------
# R002 through the same pipe (proves the player-stats leg end to end)
# ---------------------------------------------------------------------------

def test_big_stat_line_fires_r002_with_rendered_fills(tmp_path):
    clock = FrozenClock(T0)
    seed_baseline_http = make_http(
        sched_csv(GAME_NOT_FINAL), stats_status=200, stats_body=stats_csv()
    )
    run(tmp_path, clock, seed_baseline_http)

    # The player-stats fast phase opens at window close + 30 min (design 2c),
    # so the second capture has to land after 04:45Z, not merely "later".
    clock.advance(timedelta(hours=2))
    result = run(
        tmp_path,
        clock,
        make_http(
            sched_csv(GAME_NOT_FINAL), stats_status=200, stats_body=stats_csv(STAT_BIG_LINE)
        ),
    )

    lines = [e for e in result.events if e.type == "player_stat_line"]
    assert len(lines) == 1
    assert lines[0].payload["passing_yards"] == 312
    fired = [a for a in result.actions if a["rule_id"] == "R002-big-stat-line"]
    assert len(fired) == 1
    task = json.loads(
        (tmp_path / "queue" / "pending" / pending(tmp_path)[0]).read_text("utf-8")
    )
    assert task["args"]["template"] == "goodperformance"
    assert task["args"]["fills_hints"]["player"] == "Justin Fields"
    assert "312" in task["args"]["fills_hints"]["headline"]


# ---------------------------------------------------------------------------
# cadence: a source that is not due is not fetched
# ---------------------------------------------------------------------------

def test_sources_not_due_are_skipped(tmp_path):
    clock = FrozenClock(T0)
    run(tmp_path, clock, make_http(sched_csv(GAME_NOT_FINAL)))

    clock.advance(timedelta(minutes=1))
    http = make_http(sched_csv(GAME_BEARS_WIN))
    result = run(tmp_path, clock, http)

    assert http.calls == []  # nothing is due one minute later
    assert result.captured == {}
    assert result.events == []


def test_mode_flips_to_idle_outside_the_game_window(tmp_path):
    clock = FrozenClock(T0)
    run(tmp_path, clock, make_http(sched_csv(GAME_NOT_FINAL)))
    assert read_status(tmp_path)["mode"] == "game_window"

    clock.advance(timedelta(hours=9))
    run(tmp_path, clock, make_http(sched_csv(GAME_NOT_FINAL)))
    assert read_status(tmp_path)["mode"] == "idle"


# ---------------------------------------------------------------------------
# rules isolation + rotation + notify
# ---------------------------------------------------------------------------

def test_a_malformed_rule_file_is_counted_not_fatal(tmp_path):
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "R001-bears-game-final-win.json").write_text(
        (RULES_DIR / "R001-bears-game-final-win.json").read_text("utf-8"), encoding="utf-8"
    )
    (rules / "R999-broken.json").write_text("{ not json", encoding="utf-8")

    clock = FrozenClock(T0)
    root = tmp_path / "hub"
    run(root, clock, make_http(sched_csv(GAME_NOT_FINAL)), rules_dir=rules)
    clock.advance(timedelta(minutes=20))
    result = run(root, clock, make_http(sched_csv(GAME_BEARS_WIN)), rules_dir=rules)

    assert result.rules_invalid == 1
    assert result.rules_loaded == 1
    assert len(result.enqueued) == 1  # the good rule still fired
    status = read_status(root)
    assert status["rules"]["invalid"] == 1
    assert "invalid_rules" in status["alarms"]


def test_notify_action_appends_to_inbox_out(tmp_path):
    clock = FrozenClock(T0)
    run(tmp_path, clock, make_http(sched_csv(GAME_NOT_FINAL)))

    clock.advance(timedelta(hours=7))
    spiked = {
        "generated_at": "2026-09-15T10:00:00Z",
        "rankings": [
            {"opportunity": "bears-highlights", "score": 0.95, "confidence": "high"}
        ],
    }
    result = run(
        tmp_path,
        clock,
        make_http(sched_csv(GAME_NOT_FINAL)),
        file_reader=lambda path: spiked,
    )

    assert [e.type for e in result.events] == ["demand_rank_delta"]
    assert [a["rule_id"] for a in result.actions] == ["R003-demand-spike"]
    inbox = (tmp_path / "state" / "INBOX-OUT.md").read_text("utf-8")
    assert "bears-highlights" in inbox
    # notify is a task too, not a side channel (design section 5)
    task = json.loads(
        (tmp_path / "queue" / "pending" / pending(tmp_path)[0]).read_text("utf-8")
    )
    assert task["kind"] == "notify"


def test_tick_log_is_appended_and_rotates(tmp_path):
    clock = FrozenClock(T0)
    for _ in range(3):
        run(tmp_path, clock, make_http(sched_csv(GAME_NOT_FINAL)))
        clock.advance(timedelta(hours=7))
    log = (tmp_path / "state" / "TICK.log").read_text("utf-8")
    assert log.count("TICK ") >= 3


def test_snapshot_retention_is_pruned_each_tick(tmp_path):
    clock = FrozenClock(T0)
    store = SnapshotStore(tmp_path / "state", now=clock)
    for _ in range(4):
        store.write("nflverse_schedules", payload={"games": []})
        clock.advance(timedelta(seconds=1))
    run(tmp_path, clock, make_http(sched_csv(GAME_NOT_FINAL)), keep_snapshots=2)
    assert len(store.paths("nflverse_schedules")) == 2


# ---------------------------------------------------------------------------
# the 304 path -- an UNCHANGED snapshot must not blind the delta engine
# ---------------------------------------------------------------------------

def test_unchanged_snapshot_does_not_swallow_the_next_delta(tmp_path):
    clock = FrozenClock(T0)
    seed_baseline(tmp_path, clock, sched_csv(GAME_NOT_FINAL))

    def not_modified(url, headers=None, timeout=30):
        if "games.csv" in url:
            return HttpResponse(304, {}, b"")
        return HttpResponse(404, {}, b"")

    clock.advance(timedelta(minutes=20))
    mid = run(tmp_path, clock, not_modified)
    assert mid.captured["nflverse_schedules"] == "UNCHANGED"
    assert mid.events == []

    clock.advance(timedelta(minutes=20))
    result = run(tmp_path, clock, make_http(sched_csv(GAME_BEARS_WIN)))
    # The baseline is snapshot n-1 with a usable payload, NOT the 304.
    assert [e.type for e in result.events] == ["game_final"]
    assert len(result.enqueued) == 1
