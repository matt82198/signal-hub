"""Seen-index: `state/.events-seen/<YYYY-MM-DD>.ids` (design section 3, lane L4).

One event_id per line. The last 35 days are loaded into a set at tick start; that set
is what turns "this row is in the snapshot again" into "already handled".

Two deliberate properties:

* **Lazy load.** An index that has not been loaded reports every id as new, which would
  re-fire every trigger in the system. Asking any question loads it first, so there is
  no ordering a caller can get wrong.
* **Prune floor.** `prune` refuses a horizon shorter than the load window. Deleting a
  partition the window still reads would silently un-see live ids -- exactly the kind
  of quiet retention bug that only shows up as duplicate content tasks weeks later.

The seen-index bounds re-emission; it is NOT the action-side guarantee. Exactly-once
in ACTION comes from the rules layer's permanent (rule_id, event_id) gate, which is
independent by design (section 4).
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

__all__ = ["SeenIndex"]

#: Design section 3: "load the last 35 days into a set at tick start".
DEFAULT_WINDOW_DAYS = 35
#: Design section 6: partitions older than this are dead weight.
DEFAULT_KEEP_DAYS = 90


class SeenIndex:
    """Date-partitioned set of event ids already recorded.

    `clock` is a required injected callable (design section 7: no lane calls
    ambient time); the window and the prune horizon are both clock-relative and
    have to be testable at any date.
    """

    def __init__(
        self,
        state_root: str | os.PathLike,
        *,
        clock: Callable[[], datetime],
        window_days: int = DEFAULT_WINDOW_DAYS,
    ):
        self.root = Path(state_root)
        self.dir = self.root / ".events-seen"
        self.window_days = window_days
        self._clock = clock
        self._ids: set[str] = set()
        self._loaded = False

    # -- state ------------------------------------------------------------
    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def ids(self) -> frozenset[str]:
        self._ensure_loaded()
        return frozenset(self._ids)

    def _today(self) -> date:
        return self._clock().date()

    def path_for(self, day) -> Path:
        key = day.isoformat() if isinstance(day, (date, datetime)) else str(day)
        return self.dir / ("%s.ids" % key[:10])

    def days(self) -> list[str]:
        if not self.dir.is_dir():
            return []
        return sorted(p.stem for p in self.dir.glob("*.ids"))

    # -- load -------------------------------------------------------------
    def load(self) -> int:
        """Read the last `window_days` partitions into memory. Idempotent."""
        self._ids = set()
        today = self._today()
        horizon = (today - timedelta(days=self.window_days)).isoformat()
        for day in self.days():
            if day < horizon:
                continue
            self._ids.update(self._read(self.path_for(day)))
        self._loaded = True
        return len(self._ids)

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    @staticmethod
    def _read(path: Path) -> set[str]:
        if not path.is_file():
            return set()
        found: set[str] = set()
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:  # a torn final line with no newline is still an id
                token = line.strip()
                if token:
                    found.add(token)
        return found

    # -- queries ----------------------------------------------------------
    def has(self, event_id: str) -> bool:
        self._ensure_loaded()
        return event_id in self._ids

    def __contains__(self, event_id: object) -> bool:
        return isinstance(event_id, str) and self.has(event_id)

    def __len__(self) -> int:
        self._ensure_loaded()
        return len(self._ids)

    # -- writes -----------------------------------------------------------
    def mark(self, event_id: str) -> bool:
        """Record an id in today's partition. Returns False if it was already known,
        in which case nothing is written -- a duplicate tick costs no bytes."""
        if not event_id or not event_id.strip():
            raise ValueError("refusing to mark an empty event_id")
        event_id = event_id.strip()
        self._ensure_loaded()
        if event_id in self._ids:
            return False
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.path_for(self._today())
        with open(path, "ab") as handle:
            handle.write((event_id + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        self._ids.add(event_id)
        return True

    def mark_many(self, event_ids: Iterable[str]) -> list[str]:
        """Mark several ids; returns the ones that were actually new, in order."""
        return [event_id for event_id in event_ids if self.mark(event_id)]

    # -- retention --------------------------------------------------------
    def prune(self, keep_days: int = DEFAULT_KEEP_DAYS) -> list[str]:
        """Delete partitions older than `keep_days`. Refuses to prune inside the load
        window, which would un-see live ids and re-fire settled triggers."""
        if keep_days < self.window_days:
            raise ValueError(
                "keep_days=%d is inside the %d-day load window; pruning it would "
                "un-see live event ids" % (keep_days, self.window_days)
            )
        cutoff = (self._today() - timedelta(days=keep_days)).isoformat()
        removed = []
        for day in self.days():
            if day < cutoff:
                self.path_for(day).unlink()
                removed.append(day)
        return removed
