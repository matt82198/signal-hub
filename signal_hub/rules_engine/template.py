"""``{{event.path}}`` substitution for rule actions.

Substitution only.  No arithmetic, no function calls, no filters, no code - the
same resolver the predicates use, applied to the strings inside an action's
``task`` block.

Two properties are load-bearing:

* **Single pass.**  Substituted output is never re-scanned, so a value that
  itself contains ``{{...}}`` stays literal instead of becoming a second-order
  injection.
* **Missing is visible.**  A placeholder whose path resolves to nothing renders
  empty and is reported back to the caller, so a half-filled task is a
  measurable signal rather than a silent lie.
"""

import json
import re

from .errors import PathError, TemplateError
from .paths import MISSING, is_path, resolve_path, validate_path

#: Resource bounds.  Rule files are untrusted data from disk.
MAX_TEMPLATE_STRING = 4096
MAX_PLACEHOLDERS = 16
MAX_TEMPLATE_DEPTH = 8
MAX_TEMPLATE_NODES = 256

_PLACEHOLDER = re.compile(r"\{\{([^{}]*)\}\}")


def _scalar_to_text(value):
    """Render a resolved value for embedding inside a larger string."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def _placeholders(text, location):
    """Return the validated paths in *text*, or raise :class:`TemplateError`."""
    if len(text) > MAX_TEMPLATE_STRING:
        raise TemplateError(
            "template string is %d characters (max %d)" % (len(text), MAX_TEMPLATE_STRING),
            location,
        )
    matches = list(_PLACEHOLDER.finditer(text))
    if len(matches) > MAX_PLACEHOLDERS:
        raise TemplateError(
            "template string holds %d placeholders (max %d)"
            % (len(matches), MAX_PLACEHOLDERS),
            location,
        )
    paths = []
    for match in matches:
        path = match.group(1).strip()
        if not is_path(path):
            raise TemplateError(
                "placeholder %r must be a path starting with 'event.'" % path, location
            )
        try:
            validate_path(path)
        except PathError as exc:
            raise TemplateError(
                "placeholder %r is not a valid path: %s" % (path, exc.message), location
            ) from None
        paths.append(path)
    return matches, paths


def _render_string(text, event, location, missing):
    matches, paths = _placeholders(text, location)
    if not matches:
        return text

    # A string that is exactly one placeholder keeps the resolved value's native
    # JSON type, so `"priority": "{{event.payload.rank}}"` yields a number.
    only = matches[0]
    if len(matches) == 1 and only.start() == 0 and only.end() == len(text):
        value = resolve_path(event, paths[0])
        if value is MISSING:
            _record_missing(missing, paths[0])
            return None
        return value

    pieces = []
    cursor = 0
    for match, path in zip(matches, paths):
        pieces.append(text[cursor:match.start()])
        value = resolve_path(event, path)
        if value is MISSING:
            _record_missing(missing, path)
            pieces.append("")
        else:
            pieces.append(_scalar_to_text(value))
        cursor = match.end()
    pieces.append(text[cursor:])
    return "".join(pieces)


def _record_missing(missing, path):
    if path not in missing:
        missing.append(path)


def _walk(value, event, location, depth, budget, missing, collect):
    if depth > MAX_TEMPLATE_DEPTH:
        raise TemplateError(
            "action nesting exceeds the maximum depth of %d" % MAX_TEMPLATE_DEPTH, location
        )
    budget["nodes"] += 1
    if budget["nodes"] > MAX_TEMPLATE_NODES:
        raise TemplateError(
            "action has too many nodes (max %d)" % MAX_TEMPLATE_NODES, location
        )

    if isinstance(value, str):
        if collect:
            _, paths = _placeholders(value, location)
            for path in paths:
                _record_missing(missing, path)
            return value
        return _render_string(value, event, location, missing)

    if isinstance(value, dict):
        # Keys are structure, not content: they are never templated.
        return {
            key: _walk(
                item, event, "%s.%s" % (location, key), depth + 1, budget, missing, collect
            )
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [
            _walk(item, event, "%s[%d]" % (location, index), depth + 1, budget, missing, collect)
            for index, item in enumerate(value)
        ]

    return value


def render_template(value, event, location="action"):
    """Render *value* against *event*.

    Returns ``(rendered, missing_paths)``.  *rendered* is a fresh structure; the
    input is never mutated.  *missing_paths* lists, in first-seen order, the
    placeholders that resolved to nothing.
    """
    missing = []
    rendered = _walk(value, event, location, 1, {"nodes": 0}, missing, collect=False)
    return rendered, missing


def collect_placeholders(value, location="action"):
    """Return every placeholder path in *value*, validating each one.

    Used at rule-load time so a typo in ``fills_hints`` is a startup error
    rather than an empty field on a task nobody can fill.
    """
    found = []
    _walk(value, None, location, 1, {"nodes": 0}, found, collect=True)
    return found
