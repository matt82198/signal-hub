"""signal-hub events package (design section 3, lane L4).

Three parts, in the order a tick uses them:

  delta.py  snapshot n-1 vs snapshot n -> typed Events (pure, clock injected)
  log.py    append-only date-partitioned JSONL event log
  seen.py   the seen-index: has this event_id ever been recorded?

The guarantee is at-least-once in the log, exactly-once in ACTION. `record_events`
(log.py) writes the log line first and marks the id second, so a crash between the two
duplicates a log line rather than losing an event; action-side exactly-once is the
rules layer's permanent (rule_id, event_id) gate, which is independent of this package.
"""

from signal_hub.events.delta import (
    EVENT_TYPES,
    IDENTITY_TUPLES,
    Event,
    compute_event_id,
    derive_identity,
    diff,
    diff_player_stats,
    diff_schedules,
    diff_snapshots,
    diff_trend_indicator,
)
from signal_hub.events.log import EventLog, record_events
from signal_hub.events.seen import SeenIndex

__all__ = [
    "EventLog",
    "SeenIndex",
    "record_events",
    "EVENT_TYPES",
    "IDENTITY_TUPLES",
    "Event",
    "compute_event_id",
    "derive_identity",
    "diff",
    "diff_player_stats",
    "diff_schedules",
    "diff_snapshots",
    "diff_trend_indicator",
]
