"""Append-only event log: `state/events/<YYYY-MM-DD>.jsonl` (design section 3, lane L4).

One JSON object per line, UTF-8 (in practice ASCII -- `Event.to_json` escapes), LF only.

Each append is a single binary `write()` of a complete line followed by `flush` +
`fsync`. Binary mode is deliberate: text mode on Windows would translate "\\n" to CRLF
and the log would stop being byte-identical across platforms. A single write to a
handle opened in append mode is the strongest atomicity the filesystem offers, and it
is enough here because a tick is single-writer by construction (one scheduled task,
`.HALT` sentinel, no concurrent producers).

Partitions are keyed on the EVENT's `ts`, not on the wall clock, so a tick that spans
midnight does not scatter one batch across two files. They self-rotate by date and are
pruned past 90 days (design section 6).

Reads tolerate a torn trailing line. A crash between the write and the fsync can leave
a partial record, and forensics on the other 40 lines matters more than strictness.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from signal_hub.events.delta import Event

__all__ = ["EventLog", "record_events"]

#: Design section 6: event partitions older than this are forensics nobody reads.
DEFAULT_KEEP_DAYS = 90


def _day_key(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


class EventLog:
    """Append-only JSONL event log rooted at a signal-hub `state/` directory.

    `clock` is a required injected callable returning the current datetime -- no lane
    calls `datetime.now()` (design section 7), and a log whose retention is untestable
    at a date boundary is not shippable.
    """

    def __init__(self, state_root: str | os.PathLike, *, clock: Callable[[], datetime]):
        self.root = Path(state_root)
        self.dir = self.root / "events"
        self._clock = clock

    # -- paths ------------------------------------------------------------
    def path_for(self, day: Any) -> Path:
        return self.dir / ("%s.jsonl" % _day_key(day))

    def days(self) -> list[str]:
        """Every partition present, chronologically (which is also lexically)."""
        if not self.dir.is_dir():
            return []
        return sorted(p.stem for p in self.dir.glob("*.jsonl"))

    # -- writes -----------------------------------------------------------
    def append(self, event: Event) -> Path:
        """Append one event. Durable on return (flushed and fsynced)."""
        day = event.ts[:10] if event.ts else _day_key(self._clock())
        path = self.path_for(day)
        self.dir.mkdir(parents=True, exist_ok=True)
        line = (event.to_json() + "\n").encode("utf-8")
        with open(path, "ab") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        return path

    def append_many(self, events: Iterable[Event]) -> int:
        return sum(1 for event in events if self.append(event))

    # -- reads ------------------------------------------------------------
    def read_day(self, day: Any, count_errors: bool = False):
        """Records for one partition. Malformed lines are skipped, never raised."""
        path = self.path_for(day)
        records: list[dict] = []
        errors = 0
        if path.is_file():
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        errors += 1
                        continue
                    if isinstance(record, dict):
                        records.append(record)
                    else:
                        errors += 1
        if count_errors:
            return records, errors
        return records

    def iter_events(self, since: Any = None, until: Any = None) -> Iterator[dict]:
        """Walk records across partitions in chronological order. `since`/`until` are
        inclusive day keys."""
        low = _day_key(since) if since is not None else None
        high = _day_key(until) if until is not None else None
        for day in self.days():
            if low is not None and day < low:
                continue
            if high is not None and day > high:
                continue
            yield from self.read_day(day)

    def count(self, day: Any = None) -> int:
        if day is not None:
            return len(self.read_day(day))
        return sum(1 for _ in self.iter_events())

    def event_ids(self, since: Any = None) -> set[str]:
        """Ids actually present in the log -- the rebuild path if a seen-index file is
        lost or truncated."""
        return {
            record["event_id"]
            for record in self.iter_events(since=since)
            if isinstance(record.get("event_id"), str)
        }

    # -- retention --------------------------------------------------------
    def prune(self, keep_days: int = DEFAULT_KEEP_DAYS) -> list[str]:
        """Delete partitions older than `keep_days` from the injected clock. Returns
        the day keys removed."""
        cutoff = _day_key(self._clock().date() - timedelta(days=keep_days))
        removed = []
        for day in self.days():
            if day < cutoff:
                self.path_for(day).unlink()
                removed.append(day)
        return removed


def record_events(log: EventLog, seen, events: Iterable[Event]) -> list[Event]:
    """Record every not-yet-seen event, and return the ones actually recorded.

    WRITE ORDER (design section 3, verbatim): append the JSONL line, flush, THEN mark
    the id in the seen-index.

    A crash between the two re-emits next tick: a duplicate log line carrying the same
    event_id. The reverse order would instead LOSE the event permanently -- marked seen,
    never logged, never re-derivable. Duplication is recoverable and cheap; loss is
    neither. So the honest guarantee is at-least-once in the log, exactly-once in
    ACTION, because the rules layer dedups on (rule_id, event_id) independently.

    An id already in the seen-index is skipped entirely -- no second log line -- which
    is what makes a duplicate tick free.
    """
    recorded: list[Event] = []
    for event in events:
        if seen.has(event.event_id):
            continue
        log.append(event)
        seen.mark(event.event_id)
        recorded.append(event)
    return recorded
