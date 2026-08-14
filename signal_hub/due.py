"""Per-source due policy: game-window-aware cadence (design section 2c).

One 5-minute tick fires ``python -m signal_hub tick``; which sources
actually fetch is decided here, against the last capture time per source.

A game window is ``[earliest kickoff today - 15 min, latest kickoff today
+ 4 h]``, computed each tick from the freshest ``nflverse_schedules``
snapshot.  If that snapshot is missing or stale the fallback is the WIDE
window (always poll fast), never the narrow one: fail toward more data.
Callers signal "missing/stale schedule" by passing ``window=None`` to
``is_due``; ``is_due_fresh`` is the entry point for a KNOWN-fresh kickoff
list, where "no games today" correctly means idle rather than wide.

All functions are pure: ``now`` is always an argument, never read here.

Cadence table (design 2c):
    source                  game window        idle
    nflverse_schedules      every 15 min       every 6 h
    nflverse_player_stats   every 30 min,      every 24 h
                            from close+30m,
                            until rows appear
    trend_indicator         every 6 h          every 6 h
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Mapping, Optional

Window = tuple[datetime, datetime]

PRE_KICKOFF = timedelta(minutes=15)
POST_KICKOFF = timedelta(hours=4)
STATS_LAG = timedelta(minutes=30)  # stats fast phase starts at close + 30 min


@dataclass(frozen=True)
class Cadence:
    game: timedelta  # interval during the (wide or actual) game window
    idle: timedelta  # interval outside it


CADENCES: dict[str, Cadence] = {
    "nflverse_schedules": Cadence(timedelta(minutes=15), timedelta(hours=6)),
    "nflverse_player_stats": Cadence(timedelta(minutes=30), timedelta(hours=24)),
    "trend_indicator": Cadence(timedelta(hours=6), timedelta(hours=6)),
}


def game_window(kickoffs: Iterable[datetime], now: datetime) -> Optional[Window]:
    """Today's game window from a kickoff list, or None if no games today.

    "Today" is the UTC date of ``now``; kickoffs on other days are ignored.
    """
    today = [k for k in kickoffs if k.date() == now.date()]
    if not today:
        return None
    return (min(today) - PRE_KICKOFF, max(today) + POST_KICKOFF)


def in_game_window(now: datetime, window: Optional[Window]) -> bool:
    """Inclusive containment; ``window=None`` means WIDE (always inside).

    None here encodes a missing/stale schedule snapshot -- the fallback is
    always the wide window, never the narrow one.
    """
    if window is None:
        return True
    return window[0] <= now <= window[1]


def _interval(
    source: str,
    now: datetime,
    window: Optional[Window],
    rows_present: bool,
) -> timedelta:
    cadence = CADENCES[source]  # KeyError for unknown sources, on purpose
    if source == "nflverse_player_stats":
        # Fast phase: from window close + 30 min until the week's rows appear.
        # Wide fallback (window None) polls fast, like everything else.
        if rows_present:
            return cadence.idle
        if window is None:
            return cadence.game
        if now >= window[1] + STATS_LAG:
            return cadence.game
        return cadence.idle
    if in_game_window(now, window):
        return cadence.game
    return cadence.idle


def is_due(
    source: str,
    last_capture: Optional[datetime],
    now: datetime,
    window: Optional[Window],
    rows_present: bool = False,
) -> bool:
    """Should this source fetch this tick?

    ``window=None`` means the schedule snapshot is missing or stale: wide
    fallback, fast cadence.  ``rows_present`` ends the player-stats fast
    phase once the week's stat rows have appeared.
    """
    interval = _interval(source, now, window, rows_present)
    if last_capture is None:
        return True
    return now - last_capture >= interval


def is_due_fresh(
    source: str,
    last_capture: Optional[datetime],
    now: datetime,
    kickoffs: Iterable[datetime],
    rows_present: bool = False,
) -> bool:
    """`is_due` for a KNOWN-fresh schedule: no games today -> idle, not wide.

    With a fresh kickoff list, an empty window is real information ("no
    games today"), so the idle cadence applies -- unlike ``window=None``
    in ``is_due``, which encodes ignorance and fails toward polling fast.
    """
    window = game_window(kickoffs, now)
    if window is None:
        cadence = CADENCES[source]  # validate source even when never captured
        if last_capture is None:
            return True
        return now - last_capture >= cadence.idle
    return is_due(source, last_capture, now, window, rows_present)


def due_sources(
    last_captures: Mapping[str, Optional[datetime]],
    now: datetime,
    window: Optional[Window],
    rows_present: bool = False,
) -> list[str]:
    """The subset of known sources due this tick, in CADENCES order."""
    return [
        source
        for source in CADENCES
        if source in last_captures
        and is_due(source, last_captures[source], now, window, rows_present)
    ]
