"""Delta engine tests (design section 3, lane L4).

The delta engine is a pure function of two snapshot payloads plus an injected clock,
so every test here is dict fixtures -- no tempdir, no network, no wall clock.

The load-bearing property under test is IDENTITY STABILITY: a revised/corrected
source row must re-derive the SAME event_id so it hits the seen-index instead of
firing a second content task.
"""

from datetime import datetime, timezone

import pytest

from signal_hub.events import delta


NOW = datetime(2026, 9, 14, 23, 41, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 9, 15, 2, 5, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# fixtures shaped like the design's snapshot payload contract (delta module docstring)
# --------------------------------------------------------------------------

def game(game_id="2026_02_CHI_DET", **over):
    row = {
        "game_id": game_id,
        "season": 2026,
        "week": 2,
        "game_type": "REG",
        "gameday": "2026-09-14",
        "gametime": "20:15",
        "weekday": "Sunday",
        "away_team": "CHI",
        "home_team": "DET",
        "away_score": None,
        "home_score": None,
        "overtime": False,
    }
    row.update(over)
    return row


def schedules(*rows):
    return {"games": list(rows)}


def stat(player_id="00-0036322", game_id="2026_02_CHI_DET", **over):
    row = {
        "player_id": player_id,
        "player_name": "Justin Fields",
        "team": "CHI",
        "game_id": game_id,
        "season": 2026,
        "week": 2,
        "game_type": "REG",
        "passing_yards": 312,
        "passing_tds": 3,
        "rushing_yards": 44,
        "rushing_tds": 0,
        "receiving_yards": 0,
        "receiving_tds": 0,
    }
    row.update(over)
    return row


def stats(*rows):
    return {"stats": list(rows)}


def rankings(*pairs, confidence="medium"):
    return {
        "generated_at": "2026-09-14T18:02:00Z",
        "rankings": [
            {"opportunity": name, "score": score, "confidence": confidence}
            for name, score in pairs
        ],
    }


# --------------------------------------------------------------------------
# event_id: identity hashing
# --------------------------------------------------------------------------

def test_event_id_is_16_hex_chars_of_sha256_over_type_and_sorted_identity():
    eid = delta.compute_event_id("game_final", ("2026_02_CHI_DET",))
    assert len(eid) == 16
    assert all(c in "0123456789abcdef" for c in eid)


def test_event_id_identity_order_does_not_matter():
    """Design says 'sorted entity ids joined' -- tuple order must not change the id."""
    a = delta.compute_event_id("player_stat_line", ("00-0036322", "2026_02_CHI_DET"))
    b = delta.compute_event_id("player_stat_line", ("2026_02_CHI_DET", "00-0036322"))
    assert a == b


def test_event_id_differs_by_type_for_the_same_identity():
    assert delta.compute_event_id("game_final", ("G1",)) != delta.compute_event_id(
        "game_started", ("G1",)
    )


def test_event_id_excludes_ts_and_payload():
    early = delta.diff_schedules(
        schedules(game()), schedules(game(away_score=24, home_score=21)), NOW
    )[0]
    late = delta.diff_schedules(
        schedules(game()), schedules(game(away_score=27, home_score=21)), LATER
    )[0]
    assert early.ts != late.ts
    assert early.payload != late.payload
    assert early.event_id == late.event_id


# --------------------------------------------------------------------------
# game_final
# --------------------------------------------------------------------------

def test_game_final_fires_when_scores_appear():
    events = delta.diff_schedules(
        schedules(game()), schedules(game(away_score=24, home_score=21)), NOW
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.type == "game_final"
    assert ev.source == "nflverse_schedules"
    assert ev.confidence == 1.0
    assert ev.ts == "2026-09-14T23:41:00Z"
    assert ev.identity == ("2026_02_CHI_DET",)
    assert ev.payload["winner"] == "CHI"
    assert ev.payload["loser"] == "DET"
    assert ev.payload["margin"] == 3
    assert ev.payload["away_score"] == 24
    assert ev.payload["home_score"] == 21
    assert ev.payload["game_type"] == "REG"
    kinds = {e["kind"]: e["id"] for e in ev.entities}
    assert kinds["game"] == "2026_02_CHI_DET"
    assert {e["id"] for e in ev.entities if e["kind"] == "team"} == {"CHI", "DET"}


def test_game_final_home_win_and_tie():
    home = delta.diff_schedules(
        schedules(game()), schedules(game(away_score=10, home_score=31)), NOW
    )[0]
    assert home.payload["winner"] == "DET"
    assert home.payload["margin"] == 21
    tie = delta.diff_schedules(
        schedules(game()), schedules(game(away_score=17, home_score=17)), NOW
    )[0]
    assert tie.payload["winner"] is None
    assert tie.payload["margin"] == 0


def test_game_final_does_not_refire_when_already_final_in_previous():
    final = schedules(game(away_score=24, home_score=21))
    assert delta.diff_schedules(final, final, NOW) == []


def test_game_final_score_correction_does_not_refire():
    """A stat/score correction on an already-final game emits nothing at all."""
    before = schedules(game(away_score=24, home_score=21))
    corrected = schedules(game(away_score=24, home_score=20))
    assert delta.diff_schedules(before, corrected, NOW) == []


def test_game_final_correction_would_carry_the_same_id_if_it_did_refire():
    """Second line of defence: even a forced re-derive hits the same seen-index entry."""
    a = delta.diff_schedules(
        schedules(game()), schedules(game(away_score=24, home_score=21)), NOW
    )[0]
    b = delta.diff_schedules(
        schedules(game()), schedules(game(away_score=24, home_score=20)), LATER
    )[0]
    assert a.event_id == b.event_id


def test_game_final_missing_previous_snapshot_is_a_baseline_not_a_flood():
    """Cold start: no n-1 means no delta. Otherwise the first tick fires every
    historical final in games.csv."""
    assert delta.diff_schedules(None, schedules(game(away_score=24, home_score=21)), NOW) == []


def test_game_final_row_absent_from_previous_but_final_now_does_fire():
    prev = schedules(game("2026_01_GB_MIN", away_score=7, home_score=3))
    curr = schedules(
        game("2026_01_GB_MIN", away_score=7, home_score=3),
        game(away_score=24, home_score=21),
    )
    events = delta.diff_schedules(prev, curr, NOW)
    assert [e.payload["game_id"] for e in events] == ["2026_02_CHI_DET"]


def test_game_final_ignores_half_scored_rows():
    """One score present and one missing is a malformed/partial row, not a final."""
    assert delta.diff_schedules(schedules(game()), schedules(game(away_score=24)), NOW) == []


def test_game_final_tolerates_csv_string_numerics():
    ev = delta.diff_schedules(
        schedules(game(away_score="", home_score="")),
        schedules(game(away_score="24", home_score="21")),
        NOW,
    )[0]
    assert ev.payload["away_score"] == 24
    assert ev.payload["winner"] == "CHI"


def test_game_final_skips_rows_without_a_game_id():
    curr = schedules(game(game_id="", away_score=24, home_score=21))
    assert delta.diff_schedules(schedules(game(game_id="")), curr, NOW) == []


def test_preseason_still_produces_an_event_rules_do_the_filtering():
    ev = delta.diff_schedules(
        schedules(game(game_type="PRE")),
        schedules(game(game_type="PRE", away_score=17, home_score=10)),
        NOW,
    )[0]
    assert ev.payload["game_type"] == "PRE"


# --------------------------------------------------------------------------
# player_stat_line
# --------------------------------------------------------------------------

def test_player_stat_line_fires_when_the_row_first_appears():
    events = delta.diff_player_stats(stats(), stats(stat()), NOW)
    assert len(events) == 1
    ev = events[0]
    assert ev.type == "player_stat_line"
    assert ev.source == "nflverse_player_stats"
    assert ev.confidence == 1.0
    assert ev.identity == ("00-0036322", "2026_02_CHI_DET")
    assert ev.payload["passing_yards"] == 312
    assert ev.payload["total_tds"] == 3
    assert ev.payload["game_type"] == "REG"
    by_kind = {e["kind"]: e for e in ev.entities}
    assert by_kind["player"]["id"] == "00-0036322"
    assert by_kind["player"]["name"] == "Justin Fields"
    assert by_kind["team"]["id"] == "CHI"
    assert by_kind["game"]["id"] == "2026_02_CHI_DET"


def test_player_stat_line_total_tds_sums_all_three_phases():
    ev = delta.diff_player_stats(
        stats(),
        stats(stat(passing_tds=1, rushing_tds=1, receiving_tds=2)),
        NOW,
    )[0]
    assert ev.payload["total_tds"] == 4


def test_player_stat_line_correction_does_not_refire():
    """THE dedup case: nflverse revises stat lines after the fact."""
    prev = stats(stat(passing_yards=298))
    curr = stats(stat(passing_yards=312))
    assert delta.diff_player_stats(prev, curr, NOW) == []


def test_player_stat_line_identity_is_stable_across_a_correction():
    first = delta.diff_player_stats(stats(), stats(stat(passing_yards=298)), NOW)[0]
    redone = delta.diff_player_stats(stats(), stats(stat(passing_yards=312)), LATER)[0]
    assert first.event_id == redone.event_id
    assert first.payload["passing_yards"] != redone.payload["passing_yards"]


def test_player_stat_line_same_player_different_game_is_a_different_event():
    a = delta.diff_player_stats(stats(), stats(stat()), NOW)[0]
    b = delta.diff_player_stats(stats(), stats(stat(game_id="2026_03_CHI_GB")), NOW)[0]
    assert a.event_id != b.event_id


def test_player_stat_line_missing_previous_snapshot_emits_nothing():
    assert delta.diff_player_stats(None, stats(stat()), NOW) == []


def test_player_stat_line_skips_rows_missing_identity_fields():
    curr = stats(stat(player_id=""), stat(game_id=None), stat(player_id="00-0000001"))
    events = delta.diff_player_stats(stats(), curr, NOW)
    assert [e.identity[0] for e in events] == ["00-0000001"]


def test_player_stat_line_duplicate_rows_in_one_payload_emit_once():
    events = delta.diff_player_stats(stats(), stats(stat(), stat()), NOW)
    assert len(events) == 1


def test_player_stat_line_tolerates_string_and_missing_numerics():
    ev = delta.diff_player_stats(
        stats(),
        stats(stat(passing_yards="312", rushing_yards=None, receiving_yards="")),
        NOW,
    )[0]
    assert ev.payload["passing_yards"] == 312
    assert ev.payload["rushing_yards"] == 0
    assert ev.payload["receiving_yards"] == 0


# --------------------------------------------------------------------------
# demand_rank_delta
# --------------------------------------------------------------------------

def test_demand_rank_delta_fires_on_a_score_move():
    events = delta.diff_trend_indicator(
        rankings(("nfl-shorts-general", 0.40)),
        rankings(("nfl-shorts-general", 0.62)),
        NOW,
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.type == "demand_rank_delta"
    assert ev.source == "trend_indicator"
    assert ev.identity == ("nfl-shorts-general", "2026-09-14")
    assert ev.payload["score"] == 0.62
    assert ev.payload["previous_score"] == 0.40
    assert round(ev.payload["delta"], 6) == 0.22
    assert ev.payload["date_bucket"] == "2026-09-14"
    assert ev.payload["confidence_band"] == "medium"
    assert ev.confidence == 0.6
    assert [e["kind"] for e in ev.entities] == ["opportunity"]


def test_demand_rank_delta_confidence_bands_map_to_floats():
    for band, expected in (("low", 0.3), ("medium", 0.6), ("high", 0.9)):
        ev = delta.diff_trend_indicator(
            rankings(("o", 0.1), confidence=band),
            rankings(("o", 0.5), confidence=band),
            NOW,
        )[0]
        assert ev.confidence == expected
        assert ev.confidence < 1.0


def test_demand_rank_delta_ignores_moves_below_the_noise_floor():
    assert (
        delta.diff_trend_indicator(rankings(("o", 0.40)), rankings(("o", 0.42)), NOW) == []
    )


def test_demand_rank_delta_noise_floor_is_injectable():
    events = delta.diff_trend_indicator(
        rankings(("o", 0.40)), rankings(("o", 0.42)), NOW, min_delta=0.01
    )
    assert len(events) == 1


def test_demand_rank_delta_fires_on_drops_too_rules_gate_the_direction():
    ev = delta.diff_trend_indicator(rankings(("o", 0.80)), rankings(("o", 0.40)), NOW)[0]
    assert ev.payload["delta"] < 0


def test_demand_rank_delta_new_opportunity_has_no_baseline_and_stays_silent():
    assert delta.diff_trend_indicator(rankings(), rankings(("brand-new", 0.90)), NOW) == []


def test_demand_rank_delta_identity_buckets_by_utc_day_not_by_score():
    """Two moves on the same UTC day re-derive the same id -> the seen-index
    suppresses the second, which is the throttle the identity tuple encodes."""
    morning = delta.diff_trend_indicator(
        rankings(("o", 0.20)), rankings(("o", 0.50)), datetime(2026, 9, 14, 6, 0, tzinfo=timezone.utc)
    )[0]
    evening = delta.diff_trend_indicator(
        rankings(("o", 0.50)), rankings(("o", 0.80)), datetime(2026, 9, 14, 22, 0, tzinfo=timezone.utc)
    )[0]
    next_day = delta.diff_trend_indicator(
        rankings(("o", 0.80)), rankings(("o", 0.20)), datetime(2026, 9, 15, 1, 0, tzinfo=timezone.utc)
    )[0]
    assert morning.event_id == evening.event_id
    assert morning.event_id != next_day.event_id


def test_demand_rank_delta_missing_previous_snapshot_emits_nothing():
    assert delta.diff_trend_indicator(None, rankings(("o", 0.9)), NOW) == []


def test_demand_rank_delta_skips_unusable_rows():
    prev = rankings(("o", 0.1), ("keeper", 0.1))
    curr = {
        "rankings": [
            {"opportunity": "", "score": 0.9},
            {"opportunity": "o", "score": "not-a-number"},
            {"opportunity": "keeper", "score": 0.9, "confidence": "high"},
        ]
    }
    events = delta.diff_trend_indicator(prev, curr, NOW)
    assert [e.identity[0] for e in events] == ["keeper"]


# --------------------------------------------------------------------------
# dispatch + snapshot-level guards
# --------------------------------------------------------------------------

def test_diff_dispatches_by_source_name():
    events = delta.diff(
        "nflverse_schedules",
        schedules(game()),
        schedules(game(away_score=24, home_score=21)),
        NOW,
    )
    assert [e.type for e in events] == ["game_final"]


def test_diff_of_an_unknown_source_is_silent_not_an_error():
    assert delta.diff("platform_analytics", {"a": 1}, {"a": 2}, NOW) == []


def _snap(source, payload, captured_at, status="OK"):
    return {
        "source": source,
        "schema_version": 1,
        "captured_at": captured_at,
        "status": status,
        "payload": payload,
    }


def test_diff_snapshots_emits_for_two_ok_snapshots():
    prev = _snap("nflverse_schedules", schedules(game()), "2026-09-14T23:00:00Z")
    curr = _snap(
        "nflverse_schedules",
        schedules(game(away_score=24, home_score=21)),
        "2026-09-14T23:41:00Z",
    )
    assert [e.type for e in delta.diff_snapshots(prev, curr, NOW)] == ["game_final"]


@pytest.mark.parametrize("status", ["ERROR", "STALE", "SKIPPED", "UNCHANGED"])
def test_diff_snapshots_is_silent_when_the_current_snapshot_is_not_ok(status):
    """A broken or unchanged upstream produces silence, not garbage events."""
    prev = _snap("nflverse_schedules", schedules(game()), "2026-09-14T23:00:00Z")
    curr = _snap(
        "nflverse_schedules",
        schedules(game(away_score=24, home_score=21)),
        "2026-09-14T23:41:00Z",
        status=status,
    )
    assert delta.diff_snapshots(prev, curr, NOW) == []


def test_diff_snapshots_uses_an_unchanged_previous_as_still_current():
    """A 304 on n-1 means its payload is still the baseline (design section 2b)."""
    prev = _snap("nflverse_schedules", schedules(game()), "2026-09-14T23:00:00Z", "UNCHANGED")
    curr = _snap(
        "nflverse_schedules",
        schedules(game(away_score=24, home_score=21)),
        "2026-09-14T23:41:00Z",
    )
    assert len(delta.diff_snapshots(prev, curr, NOW)) == 1


def test_diff_snapshots_refuses_reordered_snapshots():
    """Lexical filename order is chronological, but a clock skew or a manual replay
    must never diff backwards -- that would re-fire a settled transition."""
    older = _snap("nflverse_schedules", schedules(game()), "2026-09-14T23:00:00Z")
    newer = _snap(
        "nflverse_schedules",
        schedules(game(away_score=24, home_score=21)),
        "2026-09-14T23:41:00Z",
    )
    assert delta.diff_snapshots(newer, older, NOW) == []


def test_diff_snapshots_refuses_mismatched_sources():
    prev = _snap("nflverse_schedules", schedules(game()), "2026-09-14T23:00:00Z")
    curr = _snap("nflverse_player_stats", stats(stat()), "2026-09-14T23:41:00Z")
    assert delta.diff_snapshots(prev, curr, NOW) == []


def test_diff_snapshots_with_no_previous_is_a_baseline():
    curr = _snap(
        "nflverse_schedules",
        schedules(game(away_score=24, home_score=21)),
        "2026-09-14T23:41:00Z",
    )
    assert delta.diff_snapshots(None, curr, NOW) == []


# --------------------------------------------------------------------------
# wire schema
# --------------------------------------------------------------------------

def test_to_dict_matches_the_design_schema_exactly():
    ev = delta.diff_player_stats(stats(), stats(stat()), NOW)[0]
    d = ev.to_dict()
    assert list(d) == ["event_id", "type", "ts", "source", "confidence", "entities", "payload"]
    assert d["event_id"] == ev.event_id


def test_to_json_is_one_ascii_line():
    ev = delta.diff_player_stats(
        stats(), stats(stat(player_name="Jose Pená")), NOW
    )[0]
    line = ev.to_json()
    assert "\n" not in line
    line.encode("ascii")


def test_from_dict_round_trips_and_re_derives_identity():
    ev = delta.diff_player_stats(stats(), stats(stat()), NOW)[0]
    back = delta.Event.from_dict(ev.to_dict())
    assert back.event_id == ev.event_id
    assert back.identity == ev.identity
    assert back.to_dict() == ev.to_dict()


def test_from_dict_round_trips_a_demand_event_via_its_payload_date_bucket():
    ev = delta.diff_trend_indicator(rankings(("o", 0.1)), rankings(("o", 0.9)), NOW)[0]
    back = delta.Event.from_dict(ev.to_dict())
    assert back.identity == ("o", "2026-09-14")
    assert back.event_id == ev.event_id


def test_from_dict_rejects_a_tampered_event_id():
    d = delta.diff_player_stats(stats(), stats(stat()), NOW)[0].to_dict()
    d["event_id"] = "0" * 16
    with pytest.raises(ValueError):
        delta.Event.from_dict(d)


def test_from_dict_can_skip_verification_for_forensic_reads():
    d = delta.diff_player_stats(stats(), stats(stat()), NOW)[0].to_dict()
    d["event_id"] = "0" * 16
    assert delta.Event.from_dict(d, verify=False).event_id == "0" * 16


def test_naive_now_is_treated_as_utc():
    ev = delta.diff_player_stats(stats(), stats(stat()), datetime(2026, 9, 14, 23, 41, 0))[0]
    assert ev.ts == "2026-09-14T23:41:00Z"
