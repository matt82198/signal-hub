"""Duration parsing and clock bucketing for ttls and throttle windows.

Everything time-dependent in this package goes through here, and everything
here takes the moment as an argument.  No function in this module reads the
system clock: game windows, throttle buckets and ``expires_at`` are all
time-dependent, and a system untestable at 11:59 PM Sunday is not shippable.

Buckets are epoch-aligned integers (``epoch_seconds // window_seconds``), which
makes the 1h/6h/1d boundaries land on UTC hour/quarter-day/midnight without any
calendar arithmetic, and makes "same bucket" a cheap set membership test.
"""

import datetime
import re

from .errors import DurationError

#: The windows a rule's ``throttle.window`` may declare (design section 4).
WINDOWS = {
    "1h": 3600,
    "6h": 21600,
    "1d": 86400,
    "7d": 604800,
    "forever": None,
}

DEFAULT_WINDOW = "1d"

#: ttls are freer than windows: any whole number of minutes/hours/days/weeks.
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
_DURATION_RE = re.compile(r"^([0-9]{1,6})([mhdw])$")

FOREVER = "forever"


def parse_duration(text):
    """Return *text* as whole seconds, or ``None`` for ``"forever"``.

    ``"30m"``, ``"6h"``, ``"3d"``, ``"1w"``.  Anything else raises
    :class:`DurationError` naming the offending value.
    """
    if text == FOREVER:
        return None
    if not isinstance(text, str):
        raise DurationError("duration must be a string, got %s" % type(text).__name__)
    match = _DURATION_RE.match(text)
    if not match:
        raise DurationError(
            "invalid duration %r; expected <number><m|h|d|w> (for example '3d') or 'forever'"
            % text
        )
    amount = int(match.group(1))
    if amount <= 0:
        raise DurationError("duration must be positive, got %r" % text)
    return amount * _UNIT_SECONDS[match.group(2)]


def parse_window(text):
    """Return *text* if it is a legal throttle window, else raise.

    Windows are a closed set on purpose.  A rule that wants "every 37 minutes"
    is describing a schedule, not a throttle, and should be rejected loudly.
    """
    if isinstance(text, str) and text in WINDOWS:
        return text
    raise DurationError(
        "invalid throttle window %r; valid windows are: %s"
        % (text, ", ".join(sorted(WINDOWS)))
    )


def _as_utc(moment):
    if not isinstance(moment, datetime.datetime):
        raise DurationError("expected a datetime, got %s" % type(moment).__name__)
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise DurationError(
            "expected a timezone-aware datetime; the injected clock must return UTC"
        )
    return moment.astimezone(datetime.timezone.utc)


def bucket_id(window, moment):
    """Return the bucket identifier for *window* at *moment*, e.g. ``"1d:20711"``.

    Two moments in the same bucket throttle each other; two moments in different
    buckets do not.  ``"forever"`` is a single bucket for all time.
    """
    window = parse_window(window)
    moment = _as_utc(moment)
    seconds = WINDOWS[window]
    if seconds is None:
        return FOREVER
    return "%s:%d" % (window, int(moment.timestamp()) // seconds)


def format_timestamp(moment):
    """Render *moment* as basic ISO UTC (``2026-09-14T23:41:00Z``).

    Second precision, ``Z`` suffix, lexically sortable - the same timestamp
    shape the snapshot store uses so every artifact in the system sorts alike.
    """
    return _as_utc(moment).strftime("%Y-%m-%dT%H:%M:%SZ")


def shift(moment, seconds):
    """Return *moment* advanced by *seconds*, in UTC."""
    return _as_utc(moment) + datetime.timedelta(seconds=seconds)
