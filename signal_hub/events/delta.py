"""Delta engine: snapshot n-1 vs snapshot n -> typed events (design section 3, lane L4).

Pure functions. No I/O, no clock of its own -- `now` is always injected, per the
design's non-negotiable rule that no lane calls `datetime.now()`.

SNAPSHOT PAYLOAD CONTRACT
-------------------------
L4 is contract-driven: it never imports an adapter. These are the payload shapes it
destructures, and the single place L7's tail-drift check should compare against.

  nflverse_schedules  {"games": [{"game_id", "season", "week", "game_type",
                                  "gameday", "gametime", "weekday",
                                  "away_team", "home_team",
                                  "away_score", "home_score", "overtime"}, ...]}
      Scores are None/"" until the game is settled; both present == final. This is
      how nfldata's games.csv works -- it carries no status column.

  nflverse_player_stats {"stats": [{"player_id", "player_name", "team", "game_id",
                                    "season", "week", "game_type",
                                    "passing_yards", "passing_tds",
                                    "rushing_yards", "rushing_tds",
                                    "receiving_yards", "receiving_tds"}, ...]}

  trend_indicator     {"generated_at", "rankings": [{"opportunity", "score",
                                                     "confidence": "low|medium|high"}, ...]}
      Verbatim the shape of ~/trend-indicator/state/rankings.json. signal-hub owns
      zero scoring math; it only diffs the artifact.

Numeric fields are read tolerantly (int, float, "312", "" and None all handled), so a
CSV adapter that does not cast cannot break the engine.

EVENT IDENTITY
--------------
`event_id = sha256(type + "|" + "|".join(sorted(identity)))[:16]` -- an identity hash,
never a content hash. `ts` and every mutable payload value are excluded on purpose:

  game_final         (game_id)
  player_stat_line   (player_id, game_id)   <- NOT the stat values
  demand_rank_delta  (opportunity, utc_date_bucket)

nflverse revises stat lines after the fact; a correction re-derives the same event_id,
hits the seen-index, and does not fire a second content task. That is the single most
important dedup decision in the design.

The three MVP types above are the ones the three MVP rules consume. `game_started`
(design section 3 table) is deliberately not emitted at MVP: it is the one transient
transition, nothing depends on it, and a missed tick would silently drop it anyway.

COLD START
----------
`prev is None` yields NO events. A first-ever snapshot has no delta, and games.csv
carries decades of already-final games -- diffing against nothing would fire hundreds
of content tasks on install. The first tick seeds the baseline; the second tick works.
Same reason a per-opportunity score with no previous value stays silent.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

__all__ = [
    "Event",
    "EVENT_TYPES",
    "IDENTITY_TUPLES",
    "compute_event_id",
    "derive_identity",
    "diff",
    "diff_snapshots",
    "diff_schedules",
    "diff_player_stats",
    "diff_trend_indicator",
]

TYPE_GAME_FINAL = "game_final"
TYPE_PLAYER_STAT_LINE = "player_stat_line"
TYPE_DEMAND_RANK_DELTA = "demand_rank_delta"

EVENT_TYPES = (TYPE_GAME_FINAL, TYPE_PLAYER_STAT_LINE, TYPE_DEMAND_RANK_DELTA)

#: Documentation of the identity tuple per type (design section 3). The values are the
#: names of what goes into the hash, in the order `derive_identity` rebuilds them.
IDENTITY_TUPLES = {
    TYPE_GAME_FINAL: ("game_id",),
    TYPE_PLAYER_STAT_LINE: ("player_id", "game_id"),
    TYPE_DEMAND_RANK_DELTA: ("opportunity", "date_bucket"),
}

SOURCE_SCHEDULES = "nflverse_schedules"
SOURCE_PLAYER_STATS = "nflverse_player_stats"
SOURCE_TREND_INDICATOR = "trend_indicator"

#: Statuses whose payload is usable as a diff operand (design section 2b).
#: An UNCHANGED n-1 is still-current; an UNCHANGED n means a 304, so nothing moved.
_USABLE_PREV_STATUSES = frozenset({"OK", "UNCHANGED"})
_USABLE_CURR_STATUSES = frozenset({"OK"})

#: Emission floor for demand_rank_delta. R003 gates at delta >= 0.15; this lower floor
#: only suppresses per-tick jitter so the log stays readable, and is injectable.
DEFAULT_MIN_DEMAND_DELTA = 0.05

#: trend-indicator publishes a confidence BAND ("low"/"medium"/"high"); the event schema
#: wants a float < 1.0 for derived events. Rules can predicate on either.
CONFIDENCE_BY_BAND = {"low": 0.3, "medium": 0.6, "high": 0.9}
DEFAULT_DERIVED_CONFIDENCE = 0.3

#: A settled nflverse fact.
SETTLED_CONFIDENCE = 1.0

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _as_utc(moment: datetime) -> datetime:
    """Naive datetimes are treated as UTC; everything else is converted."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def iso_z(moment: datetime) -> str:
    """Format an instant the way every signal-hub artifact spells it."""
    return _as_utc(moment).strftime(_TS_FORMAT)


def date_bucket(moment: datetime) -> str:
    return _as_utc(moment).strftime("%Y-%m-%d")


def _text(value: Any) -> str:
    """Identity fields must be non-empty strings; anything else is unusable."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _num(value: Any, default: float | int | None = None) -> Any:
    """Tolerant numeric read: int/float pass through, "312" casts, ""/None/junk -> default."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return int(text)
        except ValueError:
            try:
                return float(text)
            except ValueError:
                return default
    return default


def _flag(value: Any) -> bool:
    """CSV booleans arrive as True/False, 1/0 or "1"/"0"/"TRUE"."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return False


def _rows(payload: Any, key: str) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get(key)
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _entity(kind: str, ident: str, name: str | None = None) -> dict:
    entity: dict[str, Any] = {"kind": kind, "id": ident}
    if name:
        entity["name"] = name
    return entity


def _first_entity_id(entities: Sequence[dict], kind: str) -> str:
    for entity in entities:
        if isinstance(entity, dict) and entity.get("kind") == kind:
            return _text(entity.get("id"))
    return ""


# ---------------------------------------------------------------------------
# event identity + wire schema
# ---------------------------------------------------------------------------

def compute_event_id(event_type: str, identity: Iterable[str]) -> str:
    """sha256 over type + the sorted identity tuple, truncated to 16 hex chars.

    Sorting is what the design specifies, and it makes the id independent of the order
    the identity parts happen to be listed in.
    """
    parts = sorted(_text(part) for part in identity)
    digest_input = "|".join([event_type, *parts]).encode("utf-8")
    return hashlib.sha256(digest_input).hexdigest()[:16]


def derive_identity(event_type: str, entities: Sequence[dict], payload: dict) -> tuple[str, ...]:
    """Rebuild the identity tuple from a serialized event.

    The wire schema deliberately carries no `identity` field -- it stays exactly the
    design's seven keys -- so every identity part must remain recoverable from
    `entities` + `payload`. That is why demand_rank_delta carries `date_bucket` in its
    payload: it makes the day bucket auditable instead of implicit in `ts`.
    """
    if event_type == TYPE_GAME_FINAL:
        return (_first_entity_id(entities, "game"),)
    if event_type == TYPE_PLAYER_STAT_LINE:
        return (
            _first_entity_id(entities, "player"),
            _first_entity_id(entities, "game"),
        )
    if event_type == TYPE_DEMAND_RANK_DELTA:
        return (
            _first_entity_id(entities, "opportunity"),
            _text((payload or {}).get("date_bucket")),
        )
    raise ValueError("unknown event type: %r" % (event_type,))


@dataclass(frozen=True)
class Event:
    """One typed event. `event_id` is derived, never passed in by a caller.

    `identity` is an in-memory field only -- `to_dict()` emits exactly the seven keys
    of the design's schema, and `from_dict()` re-derives the identity so a round trip
    can verify the id rather than trust it.
    """

    type: str
    ts: str
    source: str
    entities: list[dict]
    payload: dict
    identity: tuple[str, ...]
    confidence: float = SETTLED_CONFIDENCE
    event_id: str = field(default="", compare=True)

    def __post_init__(self) -> None:
        if not self.event_id:
            object.__setattr__(
                self, "event_id", compute_event_id(self.type, self.identity)
            )

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "type": self.type,
            "ts": self.ts,
            "source": self.source,
            "confidence": self.confidence,
            "entities": self.entities,
            "payload": self.payload,
        }

    def to_json(self) -> str:
        """One ASCII line, no trailing newline. ensure_ascii keeps the JSONL log
        byte-safe regardless of what a player's name contains."""
        return json.dumps(self.to_dict(), ensure_ascii=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, record: dict, verify: bool = True) -> "Event":
        event_type = record["type"]
        entities = list(record.get("entities") or [])
        payload = dict(record.get("payload") or {})
        identity = derive_identity(event_type, entities, payload)
        stored_id = _text(record.get("event_id"))
        expected = compute_event_id(event_type, identity)
        if verify and stored_id and stored_id != expected:
            raise ValueError(
                "event_id %r does not match identity %r (expected %r)"
                % (stored_id, identity, expected)
            )
        return cls(
            type=event_type,
            ts=record.get("ts", ""),
            source=record.get("source", ""),
            entities=entities,
            payload=payload,
            identity=identity,
            confidence=record.get("confidence", SETTLED_CONFIDENCE),
            event_id=stored_id or expected,
        )


# ---------------------------------------------------------------------------
# nflverse_schedules -> game_final
# ---------------------------------------------------------------------------

def _game_scores(row: dict) -> tuple[Any, Any]:
    return _num(row.get("away_score")), _num(row.get("home_score"))


def _is_final(row: dict) -> bool:
    """Both scores present == settled. One present is a partial/malformed row."""
    away, home = _game_scores(row)
    return away is not None and home is not None


def _index_games(payload: Any) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for row in _rows(payload, "games"):
        game_id = _text(row.get("game_id"))
        if game_id:
            index.setdefault(game_id, row)
    return index


def diff_schedules(prev_payload: Any, curr_payload: Any, now: datetime) -> list[Event]:
    """Emit `game_final` for every game that flipped to settled since the last snapshot."""
    if prev_payload is None:
        return []
    previous = _index_games(prev_payload)
    events: list[Event] = []
    for game_id, row in _index_games(curr_payload).items():
        if not _is_final(row):
            continue
        before = previous.get(game_id)
        if before is not None and _is_final(before):
            continue  # already settled -- a later change is a correction, never a refire
        away_score, home_score = _game_scores(row)
        away_team = _text(row.get("away_team"))
        home_team = _text(row.get("home_team"))
        if away_score > home_score:
            winner, loser = away_team or None, home_team or None
        elif home_score > away_score:
            winner, loser = home_team or None, away_team or None
        else:
            winner, loser = None, None
        entities = [_entity("game", game_id)]
        if away_team:
            entities.append(_entity("team", away_team))
        if home_team:
            entities.append(_entity("team", home_team))
        events.append(
            Event(
                type=TYPE_GAME_FINAL,
                ts=iso_z(now),
                source=SOURCE_SCHEDULES,
                confidence=SETTLED_CONFIDENCE,
                entities=entities,
                payload={
                    "game_id": game_id,
                    "game_type": _text(row.get("game_type")) or None,
                    "season": _num(row.get("season")),
                    "week": _num(row.get("week")),
                    "gameday": _text(row.get("gameday")) or None,
                    "away_team": away_team or None,
                    "away_score": away_score,
                    "home_team": home_team or None,
                    "home_score": home_score,
                    "winner": winner,
                    "loser": loser,
                    "margin": abs(away_score - home_score),
                    "overtime": _flag(row.get("overtime")),
                },
                identity=(game_id,),
            )
        )
    return events


# ---------------------------------------------------------------------------
# nflverse_player_stats -> player_stat_line
# ---------------------------------------------------------------------------

def _stat_keys(payload: Any) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for row in _rows(payload, "stats"):
        player_id = _text(row.get("player_id"))
        game_id = _text(row.get("game_id"))
        if player_id and game_id:
            keys.add((player_id, game_id))
    return keys


def diff_player_stats(prev_payload: Any, curr_payload: Any, now: datetime) -> list[Event]:
    """Emit `player_stat_line` the first time a (player, game) row appears.

    Fires on APPEARANCE, not on value change, so a revised stat line is silent here
    and identical-by-id even if something forces a re-derive.
    """
    if prev_payload is None:
        return []
    seen = _stat_keys(prev_payload)
    events: list[Event] = []
    for row in _rows(curr_payload, "stats"):
        player_id = _text(row.get("player_id"))
        game_id = _text(row.get("game_id"))
        if not player_id or not game_id:
            continue
        key = (player_id, game_id)
        if key in seen:
            continue
        seen.add(key)  # a duplicated row inside one payload emits once
        passing_yards = _num(row.get("passing_yards"), 0)
        rushing_yards = _num(row.get("rushing_yards"), 0)
        receiving_yards = _num(row.get("receiving_yards"), 0)
        passing_tds = _num(row.get("passing_tds"), 0)
        rushing_tds = _num(row.get("rushing_tds"), 0)
        receiving_tds = _num(row.get("receiving_tds"), 0)
        team = _text(row.get("team")) or _text(row.get("recent_team"))
        entities = [_entity("player", player_id, _text(row.get("player_name")) or None)]
        if team:
            entities.append(_entity("team", team))
        entities.append(_entity("game", game_id))
        events.append(
            Event(
                type=TYPE_PLAYER_STAT_LINE,
                ts=iso_z(now),
                source=SOURCE_PLAYER_STATS,
                confidence=SETTLED_CONFIDENCE,
                entities=entities,
                payload={
                    "game_type": _text(row.get("game_type")) or None,
                    "season": _num(row.get("season")),
                    "week": _num(row.get("week")),
                    "passing_yards": passing_yards,
                    "passing_tds": passing_tds,
                    "rushing_yards": rushing_yards,
                    "rushing_tds": rushing_tds,
                    "receiving_yards": receiving_yards,
                    "receiving_tds": receiving_tds,
                    # derived so R002 can predicate on it without arithmetic in the DSL
                    "total_tds": passing_tds + rushing_tds + receiving_tds,
                },
                identity=key,
            )
        )
    return events


# ---------------------------------------------------------------------------
# trend_indicator -> demand_rank_delta
# ---------------------------------------------------------------------------

def _index_rankings(payload: Any) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for row in _rows(payload, "rankings"):
        name = _text(row.get("opportunity"))
        if name:
            index.setdefault(name, row)
    return index


def diff_trend_indicator(
    prev_payload: Any,
    curr_payload: Any,
    now: datetime,
    min_delta: float = DEFAULT_MIN_DEMAND_DELTA,
) -> list[Event]:
    """Emit `demand_rank_delta` when an opportunity's score moves past the noise floor.

    Identity is (opportunity, UTC date) -- so the second move of the same day
    re-derives the first move's id and is suppressed by the seen-index. The day bucket
    IS the throttle, encoded in the identity rather than bolted on afterwards.

    Drops are emitted too (negative delta); R003 gates on `delta >= 0.15`, so direction
    is a rule concern, not an engine concern.
    """
    if prev_payload is None:
        return []
    previous = _index_rankings(prev_payload)
    bucket = date_bucket(now)
    events: list[Event] = []
    for name, row in _index_rankings(curr_payload).items():
        before = previous.get(name)
        if before is None:
            continue  # no baseline for this opportunity yet
        curr_score = _num(row.get("score"))
        prev_score = _num(before.get("score"))
        if curr_score is None or prev_score is None:
            continue
        change = curr_score - prev_score
        if abs(change) < min_delta:
            continue
        band = _text(row.get("confidence")).lower()
        events.append(
            Event(
                type=TYPE_DEMAND_RANK_DELTA,
                ts=iso_z(now),
                source=SOURCE_TREND_INDICATOR,
                confidence=CONFIDENCE_BY_BAND.get(band, DEFAULT_DERIVED_CONFIDENCE),
                entities=[_entity("opportunity", name)],
                payload={
                    "opportunity": name,
                    "score": curr_score,
                    "previous_score": prev_score,
                    "delta": change,
                    "date_bucket": bucket,
                    "confidence_band": band or None,
                },
                identity=(name, bucket),
            )
        )
    return events


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

DIFFERS: dict[str, Callable[[Any, Any, datetime], list[Event]]] = {
    SOURCE_SCHEDULES: diff_schedules,
    SOURCE_PLAYER_STATS: diff_player_stats,
    SOURCE_TREND_INDICATOR: diff_trend_indicator,
}


def diff(source: str, prev_payload: Any, curr_payload: Any, now: datetime) -> list[Event]:
    """Diff two payloads for `source`. An unknown source (a stub, a future adapter)
    is silent rather than an error -- inputs always produce outputs, and the output
    of a source with no delta function is legitimately nothing."""
    differ = DIFFERS.get(source)
    if differ is None:
        return []
    return differ(prev_payload, curr_payload, now)


def _parse_captured_at(snapshot: dict) -> datetime | None:
    raw = _text(snapshot.get("captured_at"))
    if not raw:
        return None
    try:
        return _as_utc(datetime.strptime(raw, _TS_FORMAT))
    except ValueError:
        try:
            return _as_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
        except ValueError:
            return None


def diff_snapshots(prev_snapshot: Any, curr_snapshot: Any, now: datetime) -> list[Event]:
    """Snapshot-level entry point: status and ordering guards, then `diff`.

    Guards, all of which resolve to silence rather than an exception:
      - no previous snapshot            -> cold-start baseline
      - current not OK (ERROR/STALE/SKIPPED/UNCHANGED) -> a broken or 304 upstream
        produces silence, not garbage events
      - previous not OK/UNCHANGED       -> no trustworthy baseline
      - sources disagree                -> caller wiring bug
      - current captured before previous -> never diff backwards; clock skew or a
        manual replay must not re-fire a settled transition
    """
    if not isinstance(curr_snapshot, dict):
        return []
    if _text(curr_snapshot.get("status")) not in _USABLE_CURR_STATUSES:
        return []
    source = _text(curr_snapshot.get("source"))
    if not isinstance(prev_snapshot, dict):
        return []
    if _text(prev_snapshot.get("source")) != source:
        return []
    if _text(prev_snapshot.get("status")) not in _USABLE_PREV_STATUSES:
        return []
    prev_at = _parse_captured_at(prev_snapshot)
    curr_at = _parse_captured_at(curr_snapshot)
    if prev_at and curr_at and curr_at < prev_at:
        return []
    return diff(source, prev_snapshot.get("payload"), curr_snapshot.get("payload"), now)
