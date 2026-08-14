"""Dotted path resolution over event dicts.

The whole language rests on this module being *data-only*: it uses ``dict``
key lookup and ``list`` indexing and nothing else.  It never touches an
attribute, never imports by name, never compiles a string.  A segment like
``__class__`` is therefore an ordinary mapping key that almost always misses.

``event.entities.<kind>.<field>`` is sugar for "the first entity of that kind",
because rules read far better as ``event.entities.player.id`` than as
``event.entities.0.id`` (entity order is not a contract).
"""

from .errors import PathError

#: Every path is rooted at this token.
ROOT = "event"

#: Resource bounds.  A rule file is data from disk; treat it as untrusted.
MAX_PATH_LENGTH = 200
MAX_PATH_SEGMENTS = 16


class _Missing:
    """Sentinel for "this path resolved to nothing".

    Distinct from ``None`` (which is a legitimate JSON value) and falsey so a
    careless ``if value:`` still does the safe thing.
    """

    __slots__ = ()
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self):
        return "MISSING"

    def __bool__(self):
        return False


MISSING = _Missing()


def is_path(value):
    """True when *value* is a string the DSL should resolve rather than take literally.

    The bare token ``"event"`` is deliberately *not* a path: a rule addresses a
    field, never the whole event, so ``"event"`` stays an ordinary string literal.
    """
    return isinstance(value, str) and value.startswith(ROOT + ".")


def validate_path(path):
    """Raise :class:`PathError` unless *path* is a well-formed lookup.

    Called at rule-load time so a typo is a loud startup error, not a rule that
    silently never fires.
    """
    if not isinstance(path, str):
        raise PathError("path must be a string, got %s" % type(path).__name__)
    if len(path) > MAX_PATH_LENGTH:
        raise PathError(
            "path is %d characters (max %d): %r" % (len(path), MAX_PATH_LENGTH, path[:60])
        )
    segments = path.split(".")
    if segments[0] != ROOT:
        raise PathError("path must start with 'event', got %r" % path)
    if len(segments) < 2:
        raise PathError("path must address a field below 'event', got %r" % path)
    if len(segments) - 1 > MAX_PATH_SEGMENTS:
        raise PathError(
            "path has %d segments (max %d): %r" % (len(segments) - 1, MAX_PATH_SEGMENTS, path)
        )
    for index, segment in enumerate(segments):
        if not segment:
            raise PathError("path has an empty segment at position %d: %r" % (index, path))
    return segments


def _first_of_kind(items, kind):
    for item in items:
        if isinstance(item, dict) and item.get("kind") == kind:
            return item
    return MISSING


def resolve_path(event, path):
    """Resolve *path* against *event*, returning ``MISSING`` rather than raising.

    Lenient by design: the evaluator calls this once per operand per event and a
    ragged payload must never abort a tick.  Structural validation happens once,
    at load time, in :func:`validate_path`.
    """
    if not is_path(path):
        return MISSING

    current = event
    for segment in path.split(".")[1:]:
        if not segment:
            return MISSING
        if isinstance(current, dict):
            if segment in current:
                current = current[segment]
            else:
                return MISSING
        elif isinstance(current, list):
            if segment.isascii() and segment.isdigit():
                index = int(segment)
                if index >= len(current):
                    return MISSING
                current = current[index]
            else:
                current = _first_of_kind(current, segment)
                if current is MISSING:
                    return MISSING
        else:
            # Scalars, strings and bytes are terminal.  Never index a string:
            # "event.type.0" must miss, not yield a character.
            return MISSING
    return current
