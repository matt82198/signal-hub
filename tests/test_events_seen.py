"""Seen-index tests (design section 3, lane L4): `state/.events-seen/<day>.ids`."""

from datetime import datetime, timedelta, timezone

import pytest

from signal_hub.events.seen import SeenIndex


NOW = datetime(2026, 9, 14, 23, 41, 0, tzinfo=timezone.utc)


def frozen(moment):
    return lambda: moment


def make_index(tmp_path, clock=NOW, **kw):
    return SeenIndex(tmp_path / "state", clock=frozen(clock), **kw)


# --------------------------------------------------------------------------

def test_mark_then_has(tmp_path):
    index = make_index(tmp_path)
    assert index.has("a3f91c2e8b7d4506") is False
    assert index.mark("a3f91c2e8b7d4506") is True
    assert index.has("a3f91c2e8b7d4506") is True


def test_mark_writes_one_id_per_line_in_a_day_partition(tmp_path):
    index = make_index(tmp_path)
    index.mark("aaaa")
    index.mark("bbbb")
    path = tmp_path / "state" / ".events-seen" / "2026-09-14.ids"
    assert path.read_bytes() == b"aaaa\nbbbb\n"


def test_marking_twice_writes_once(tmp_path):
    """A duplicate tick must be free, not append-amplifying."""
    index = make_index(tmp_path)
    assert index.mark("aaaa") is True
    assert index.mark("aaaa") is False
    path = tmp_path / "state" / ".events-seen" / "2026-09-14.ids"
    assert path.read_bytes() == b"aaaa\n"


def test_ids_survive_a_restart(tmp_path):
    make_index(tmp_path).mark("aaaa")
    assert make_index(tmp_path).has("aaaa") is True


def test_index_loads_lazily_so_a_caller_cannot_forget_to_load(tmp_path):
    """An unloaded index would report every id as new and re-fire the world. The only
    safe default is that the first question triggers the load."""
    make_index(tmp_path).mark("aaaa")
    fresh = make_index(tmp_path)
    assert fresh.loaded is False
    assert fresh.has("aaaa") is True
    assert fresh.loaded is True


def test_load_reports_how_many_ids_it_read(tmp_path):
    writer = make_index(tmp_path)
    writer.mark("aaaa")
    writer.mark("bbbb")
    assert make_index(tmp_path).load() == 2


def test_load_is_idempotent(tmp_path):
    make_index(tmp_path).mark("aaaa")
    index = make_index(tmp_path)
    assert index.load() == 1
    assert index.load() == 1
    assert len(index.ids) == 1


def test_load_spans_the_retention_window(tmp_path):
    old = make_index(tmp_path, clock=NOW - timedelta(days=30))
    old.mark("thirty-days-old")
    assert make_index(tmp_path).has("thirty-days-old") is True


def test_load_window_is_bounded_and_the_horizon_is_documented(tmp_path):
    """35 days back is the load window; older ids fall out of the set on purpose --
    nothing in a snapshot diff can resurface a transition that old."""
    ancient = make_index(tmp_path, clock=NOW - timedelta(days=40))
    ancient.mark("forty-days-old")
    assert make_index(tmp_path).has("forty-days-old") is False


def test_load_window_is_injectable(tmp_path):
    ancient = make_index(tmp_path, clock=NOW - timedelta(days=40))
    ancient.mark("forty-days-old")
    assert make_index(tmp_path, window_days=60).has("forty-days-old") is True


def test_load_tolerates_blank_and_whitespace_lines(tmp_path):
    seen_dir = tmp_path / "state" / ".events-seen"
    seen_dir.mkdir(parents=True)
    (seen_dir / "2026-09-14.ids").write_bytes(b"aaaa\n\n  bbbb  \n\n")
    index = make_index(tmp_path)
    assert index.load() == 2
    assert index.has("bbbb") is True


def test_load_tolerates_a_torn_final_line(tmp_path):
    """A crash mid-append can leave an id with no newline; it is still a real id."""
    seen_dir = tmp_path / "state" / ".events-seen"
    seen_dir.mkdir(parents=True)
    (seen_dir / "2026-09-14.ids").write_bytes(b"aaaa\nbbbb")
    assert make_index(tmp_path).has("bbbb") is True


def test_load_ignores_unrelated_files(tmp_path):
    seen_dir = tmp_path / "state" / ".events-seen"
    seen_dir.mkdir(parents=True)
    (seen_dir / "2026-09-14.ids").write_bytes(b"aaaa\n")
    (seen_dir / "README.txt").write_bytes(b"not an id file\n")
    assert make_index(tmp_path).load() == 1


def test_mark_many_returns_the_newly_marked_ids(tmp_path):
    index = make_index(tmp_path)
    index.mark("aaaa")
    assert index.mark_many(["aaaa", "bbbb", "cccc", "bbbb"]) == ["bbbb", "cccc"]


def test_marks_land_in_the_partition_for_the_injected_clock(tmp_path):
    make_index(tmp_path).mark("aaaa")
    make_index(tmp_path, clock=NOW + timedelta(days=1)).mark("bbbb")
    seen_dir = tmp_path / "state" / ".events-seen"
    assert sorted(p.name for p in seen_dir.glob("*.ids")) == [
        "2026-09-14.ids",
        "2026-09-15.ids",
    ]


def test_days_are_listed_chronologically(tmp_path):
    make_index(tmp_path, clock=NOW + timedelta(days=1)).mark("bbbb")
    make_index(tmp_path).mark("aaaa")
    assert make_index(tmp_path).days() == ["2026-09-14", "2026-09-15"]


def test_prune_drops_partitions_past_the_retention_horizon(tmp_path):
    make_index(tmp_path, clock=NOW - timedelta(days=200)).mark("ancient")
    make_index(tmp_path).mark("recent")
    index = make_index(tmp_path)
    assert index.prune(keep_days=90) == [(NOW - timedelta(days=200)).strftime("%Y-%m-%d")]
    assert index.days() == ["2026-09-14"]


def test_prune_never_touches_the_live_load_window(tmp_path):
    make_index(tmp_path, clock=NOW - timedelta(days=30)).mark("in-window")
    assert make_index(tmp_path).prune(keep_days=90) == []


def test_prune_rejects_a_horizon_shorter_than_the_load_window(tmp_path):
    """Pruning inside the window would silently un-see live ids and re-fire tasks."""
    index = make_index(tmp_path)
    with pytest.raises(ValueError):
        index.prune(keep_days=10)


def test_directory_is_created_lazily(tmp_path):
    index = make_index(tmp_path)
    assert not (tmp_path / "state" / ".events-seen").exists()
    index.mark("aaaa")
    assert (tmp_path / "state" / ".events-seen").is_dir()


def test_contains_is_the_same_question_as_has(tmp_path):
    index = make_index(tmp_path)
    index.mark("aaaa")
    assert "aaaa" in index
    assert "bbbb" not in index


def test_empty_ids_are_rejected(tmp_path):
    index = make_index(tmp_path)
    with pytest.raises(ValueError):
        index.mark("")


def test_clock_must_be_injected(tmp_path):
    with pytest.raises(TypeError):
        SeenIndex(tmp_path / "state")
