"""Event log tests (design section 3, lane L4): append-only date-partitioned JSONL."""

from datetime import datetime, timezone

import pytest

from signal_hub.events import delta
from signal_hub.events.log import EventLog


NOW = datetime(2026, 9, 14, 23, 41, 0, tzinfo=timezone.utc)
NEXT_DAY = datetime(2026, 9, 15, 0, 3, 0, tzinfo=timezone.utc)


def frozen(moment):
    return lambda: moment


def make_event(now=NOW, player_id="00-0036322", game_id="2026_02_CHI_DET", **payload):
    prev = {"stats": []}
    curr = {
        "stats": [
            {
                "player_id": player_id,
                "player_name": "Justin Fields",
                "team": "CHI",
                "game_id": game_id,
                "game_type": "REG",
                "passing_yards": 312,
                "passing_tds": 3,
                **payload,
            }
        ]
    }
    return delta.diff_player_stats(prev, curr, now)[0]


def make_log(tmp_path, clock=NOW):
    return EventLog(tmp_path / "state", clock=frozen(clock))


# --------------------------------------------------------------------------

def test_append_creates_a_date_partitioned_file(tmp_path):
    log = make_log(tmp_path)
    log.append(make_event())
    expected = tmp_path / "state" / "events" / "2026-09-14.jsonl"
    assert expected.is_file()


def test_partition_follows_the_event_ts_not_the_wall_clock(tmp_path):
    """A tick that starts at 23:59 and emits events stamped 23:59 must not scatter
    them across two files."""
    log = make_log(tmp_path, clock=NEXT_DAY)
    log.append(make_event(now=NOW))
    assert (tmp_path / "state" / "events" / "2026-09-14.jsonl").is_file()
    assert not (tmp_path / "state" / "events" / "2026-09-15.jsonl").exists()


def test_append_is_one_json_line_per_event(tmp_path):
    log = make_log(tmp_path)
    log.append(make_event())
    log.append(make_event(game_id="2026_03_CHI_GB"))
    raw = (tmp_path / "state" / "events" / "2026-09-14.jsonl").read_bytes()
    assert raw.count(b"\n") == 2
    assert raw.endswith(b"\n")


def test_append_writes_lf_only_and_ascii_only(tmp_path):
    """Binary append: no CRLF translation on Windows, no mojibake in a name field."""
    log = make_log(tmp_path)
    log.append(make_event(player_name="Jose Pená"))
    raw = (tmp_path / "state" / "events" / "2026-09-14.jsonl").read_bytes()
    assert b"\r" not in raw
    raw.decode("ascii")


def test_appended_record_matches_the_design_schema(tmp_path):
    log = make_log(tmp_path)
    event = make_event()
    log.append(event)
    [record] = log.read_day("2026-09-14")
    assert list(record) == [
        "event_id",
        "type",
        "ts",
        "source",
        "confidence",
        "entities",
        "payload",
    ]
    assert record["event_id"] == event.event_id


def test_append_never_rewrites_history(tmp_path):
    """Append-only: an existing partition is extended, never truncated."""
    log = make_log(tmp_path)
    log.append(make_event())
    reopened = make_log(tmp_path)
    reopened.append(make_event(game_id="2026_03_CHI_GB"))
    assert len(log.read_day("2026-09-14")) == 2


def test_append_many_returns_the_events_it_wrote(tmp_path):
    log = make_log(tmp_path)
    events = [make_event(), make_event(game_id="2026_03_CHI_GB")]
    assert log.append_many(events) == 2
    assert len(log.read_day("2026-09-14")) == 2


def test_read_day_of_a_missing_partition_is_empty(tmp_path):
    assert make_log(tmp_path).read_day("2020-01-01") == []


def test_read_day_skips_a_torn_trailing_line(tmp_path):
    """A crash mid-append can leave a partial line; forensics must survive it."""
    log = make_log(tmp_path)
    log.append(make_event())
    path = tmp_path / "state" / "events" / "2026-09-14.jsonl"
    with open(path, "ab") as handle:
        handle.write(b'{"event_id": "truncated"')
    records = log.read_day("2026-09-14")
    assert len(records) == 1
    assert log.read_day("2026-09-14", count_errors=True)[1] == 1


def test_read_day_skips_blank_lines(tmp_path):
    log = make_log(tmp_path)
    log.append(make_event())
    path = tmp_path / "state" / "events" / "2026-09-14.jsonl"
    with open(path, "ab") as handle:
        handle.write(b"\n   \n")
    assert len(log.read_day("2026-09-14")) == 1


def test_days_are_listed_in_chronological_order(tmp_path):
    log = make_log(tmp_path)
    log.append(make_event(now=NEXT_DAY))
    log.append(make_event(now=NOW))
    log.append(make_event(now=datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)))
    assert log.days() == ["2026-08-01", "2026-09-14", "2026-09-15"]


def test_iter_events_walks_partitions_in_order(tmp_path):
    log = make_log(tmp_path)
    log.append(make_event(now=NOW))
    log.append(make_event(now=NEXT_DAY, game_id="2026_03_CHI_GB"))
    tss = [record["ts"] for record in log.iter_events()]
    assert tss == ["2026-09-14T23:41:00Z", "2026-09-15T00:03:00Z"]


def test_iter_events_can_be_bounded_by_day(tmp_path):
    log = make_log(tmp_path)
    log.append(make_event(now=NOW))
    log.append(make_event(now=NEXT_DAY, game_id="2026_03_CHI_GB"))
    assert len(list(log.iter_events(since="2026-09-15"))) == 1
    assert len(list(log.iter_events(until="2026-09-14"))) == 1


def test_count_for_day(tmp_path):
    log = make_log(tmp_path)
    log.append(make_event())
    log.append(make_event(game_id="2026_03_CHI_GB"))
    assert log.count(day="2026-09-14") == 2
    assert log.count(day="2026-09-15") == 0


def test_prune_drops_partitions_past_the_retention_horizon(tmp_path):
    """Design section 6: prune partitions older than 90 days."""
    log = make_log(tmp_path)
    log.append(make_event(now=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)))
    log.append(make_event(now=NOW))
    removed = log.prune(keep_days=90)
    assert removed == ["2026-01-01"]
    assert log.days() == ["2026-09-14"]


def test_prune_keeps_the_boundary_day_and_drops_the_one_before_it(tmp_path):
    log = make_log(tmp_path)  # clock is 2026-09-14; 90 days back is 2026-06-16
    log.append(make_event(now=datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)))
    log.append(make_event(now=datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)))
    assert log.prune(keep_days=90) == ["2026-06-15"]


def test_prune_uses_the_injected_clock(tmp_path):
    log = make_log(tmp_path, clock=datetime(2027, 1, 1, tzinfo=timezone.utc))
    log.append(make_event(now=NOW))
    assert log.prune(keep_days=90) == ["2026-09-14"]


def test_root_directory_is_created_lazily_not_at_construction(tmp_path):
    log = make_log(tmp_path)
    assert not (tmp_path / "state" / "events").exists()
    log.append(make_event())
    assert (tmp_path / "state" / "events").is_dir()


def test_clock_must_be_injected(tmp_path):
    """No lane calls datetime.now() (design section 7)."""
    with pytest.raises(TypeError):
        EventLog(tmp_path / "state")
