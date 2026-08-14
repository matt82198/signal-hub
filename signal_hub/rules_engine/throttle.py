"""The two firing gates, persisted as one append-only JSONL file.

Design section 4, "Throttle / dedup":

1. **Unconditional** ``(rule_id, event_id)`` - never fire the same rule for the
   same event twice, ever.  This is what makes the *action* side exactly-once
   even though the event log is only at-least-once.
2. **Declared throttle** ``(rule_id, resolved key, window bucket)`` - stops
   "12 rushing-yard leaders on Sunday" becoming 12 tasks for the same player.

Deliberately never pruned.  Both gates are derived from the same records, and
gate 1 is permanent, so dropping an old line would silently re-arm a rule for an
event it already fired on.  One line per fire is a few kilobytes a season.

Corruption is tolerated, not fatal: a half-written final line (a crash mid-append)
is skipped and counted, and the surviving records still gate correctly.
"""

import json
import os

from .durations import bucket_id, format_timestamp

#: A single record is small; anything larger is corruption, not data.
MAX_LINE_BYTES = 8192

_SEPARATOR = "\x1f"


class ThrottleState:
    """In-memory gate sets backed by an append-only JSONL file."""

    def __init__(self, path, *, now, records=None, corrupt_lines=0):
        if not callable(now):
            raise TypeError("now must be a callable returning an aware UTC datetime")
        self.path = path
        self._now = now
        self._fired = set()
        self._throttled = set()
        self.count = 0
        self.corrupt_lines = corrupt_lines
        for record in records or ():
            self._index(record)

    # -- loading ---------------------------------------------------------

    @classmethod
    def load(cls, path, *, now):
        """Read *path* if it exists, skipping unparseable lines."""
        records = []
        corrupt = 0
        try:
            handle = open(path, "r", encoding="utf-8", errors="replace", newline="")
        except FileNotFoundError:
            return cls(path, now=now)
        except OSError as exc:
            raise OSError("cannot read throttle state %s: %s" % (path, exc)) from None
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                record = _parse_record(line)
                if record is None:
                    corrupt += 1
                else:
                    records.append(record)
        return cls(path, now=now, records=records, corrupt_lines=corrupt)

    def _index(self, record):
        self._fired.add((record["rule_id"], record["event_id"]))
        key = record.get("throttle_key")
        bucket = record.get("bucket")
        if isinstance(key, str) and isinstance(bucket, str):
            self._throttled.add(_throttle_index(record["rule_id"], key, bucket))
        self.count += 1

    # -- gates -----------------------------------------------------------

    def is_duplicate(self, rule_id, event_id):
        """Gate 1: has this rule already fired for this exact event?"""
        return (rule_id, event_id) in self._fired

    def bucket(self, window):
        """The current bucket id for *window*, on the injected clock."""
        return bucket_id(window, self._now())

    def is_throttled(self, rule_id, throttle_key, window):
        """Gate 2: has this rule already fired for this key in this window?"""
        if window is None or throttle_key is None:
            return False
        return _throttle_index(rule_id, throttle_key, self.bucket(window)) in self._throttled

    # -- recording -------------------------------------------------------

    def record(self, rule_id, event_id, throttle_key=None, window=None):
        """Close both gates for this firing and append the record to disk."""
        moment = self._now()
        entry = {
            "rule_id": rule_id,
            "event_id": event_id,
            "throttle_key": throttle_key,
            "window": window,
            "bucket": bucket_id(window, moment) if window is not None else None,
            "fired_at": format_timestamp(moment),
        }
        self._append(entry)
        self._index(entry)
        return entry

    def _append(self, entry):
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        # ensure_ascii keeps the state file ASCII whatever a player's name holds.
        line = json.dumps(entry, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        with open(self.path, "a", encoding="ascii", newline="\n") as handle:
            # A preceding crash can leave a truncated line with no newline; a
            # leading newline keeps our record parseable instead of merging into it.
            if handle.tell() and _needs_leading_newline(self.path):
                handle.write("\n")
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def _needs_leading_newline(path):
    try:
        with open(path, "rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            if size == 0:
                return False
            handle.seek(size - 1)
            return handle.read(1) != b"\n"
    except OSError:
        return False


def _throttle_index(rule_id, throttle_key, bucket):
    return _SEPARATOR.join((rule_id, throttle_key, bucket))


def _parse_record(line):
    """Return a well-formed record, or ``None`` if the line is corrupt."""
    if len(line) > MAX_LINE_BYTES:
        return None
    try:
        record = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(record, dict):
        return None
    if not isinstance(record.get("rule_id"), str) or not isinstance(record.get("event_id"), str):
        return None
    return record
