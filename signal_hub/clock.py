"""The single injectable time seam for signal-hub (design section 7).

Every time-dependent module accepts a ``now`` callable returning an aware
UTC datetime instead of reading the wall clock itself.  ``system_now`` is
the only place in the package that touches the real clock; tests inject
``FrozenClock``.  Game windows, throttle buckets, expiries and freshness
alarms all hang off this seam.

Timestamp conventions (design section 2b):
  compact: 20260813T140200Z  (basic ISO, UTC; lexical sort == chronological)
  iso:     2026-08-13T14:02:00Z
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

COMPACT_FMT = "%Y%m%dT%H%M%SZ"
ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def system_now() -> datetime:
    """Real wall clock. The ONLY ambient clock call in signal_hub."""
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime) -> datetime:
    """Return ``dt`` normalized to UTC. Naive datetimes are rejected."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime not allowed; pass an aware datetime")
    return dt.astimezone(timezone.utc)


def ts_compact(dt: datetime) -> str:
    """Aware datetime -> ``20260813T140200Z`` (snapshot filename stamp)."""
    return ensure_utc(dt).strftime(COMPACT_FMT)


def parse_compact(ts: str) -> datetime:
    """``20260813T140200Z`` -> aware UTC datetime."""
    return datetime.strptime(ts, COMPACT_FMT).replace(tzinfo=timezone.utc)


def iso_utc(dt: datetime) -> str:
    """Aware datetime -> ``2026-08-13T14:02:00Z`` (JSON body stamp)."""
    return ensure_utc(dt).strftime(ISO_FMT)


def parse_iso(ts: str) -> datetime:
    """``2026-08-13T14:02:00Z`` -> aware UTC datetime."""
    return datetime.strptime(ts, ISO_FMT).replace(tzinfo=timezone.utc)


class FrozenClock:
    """Deterministic ``now()`` for tests: callable, advanceable, settable."""

    def __init__(self, start: datetime) -> None:
        self._now = ensure_utc(start)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        self._now = self._now + delta
        return self._now

    def set(self, dt: datetime) -> datetime:
        self._now = ensure_utc(dt)
        return self._now
