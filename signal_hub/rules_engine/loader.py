"""Loading and validating ``rules/*.json`` (design section 4).

Rules are committed source; ``state/`` is not.  So the loader treats a rule file
the way a compiler treats a source file: strict schema, unknown keys rejected,
and errors that name the file, the location inside it, and what was expected.

One malformed rule never stops the others.  :func:`load_rules` returns the rules
it could build *and* the errors it hit, so the tick can run the good rules and
still raise ``rules.invalid`` in ``hub-status.json``.
"""

import json
import os
import re

from .durations import parse_duration, parse_window
from .errors import (
    DurationError,
    PathError,
    PredicateError,
    RuleValidationError,
    TemplateError,
)
from .paths import MISSING, is_path, resolve_path, validate_path
from .predicates import compile_predicate
from .template import collect_placeholders

#: A rule is a page of JSON.  Anything larger is a mistake or an attack.
MAX_RULE_FILE_BYTES = 65536
MAX_WHY_LENGTH = 300
MAX_NOTES_LENGTH = 2000
MAX_THROTTLE_KEY_PARTS = 8

RULE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

RULE_KEYS = ("rule_id", "enabled", "why", "notes", "on", "throttle", "action")
ON_KEYS = ("type", "where")
THROTTLE_KEYS = ("key", "window")
ACTION_KEYS = ("kind", "priority", "ttl", "task")

#: What the hub decides to do.  Dispatch belongs to the queue lane, not here.
ACTION_KINDS = ("enqueue_task", "notify")

#: The task kinds the queue contract defines at MVP (design section 5).
TASK_KINDS = ("instantiate_template", "run_workflow", "notify")

#: notify is a task too, not a side channel - so the two vocabularies pair up.
ALLOWED_TASK_KINDS = {
    "enqueue_task": ("instantiate_template", "run_workflow"),
    "notify": ("notify",),
}

DEFAULT_PRIORITY = 50

#: The token in a throttle key that means "this rule's id".
RULE_ID_TOKEN = "rule_id"


class ThrottleSpec:
    """A rule's declared throttle: a resolved key, bucketed by a window."""

    __slots__ = ("key", "window")

    def __init__(self, key, window):
        self.key = tuple(key)
        self.window = window

    def resolve(self, event, rule_id):
        """Return the throttle key for *event*, or ``None`` if it cannot be built.

        ``None`` means "fail closed": the engine declines to fire rather than
        firing unthrottled, because an unresolvable key is exactly the case
        where a rule would otherwise spam.
        """
        parts = []
        for part in self.key:
            if part == RULE_ID_TOKEN:
                parts.append(rule_id)
            elif is_path(part):
                value = resolve_path(event, part)
                if value is MISSING:
                    return None
                parts.append(_key_text(value))
            else:
                parts.append(part)
        return "|".join(parts)

    def __repr__(self):
        return "ThrottleSpec(key=%r, window=%r)" % (self.key, self.window)


def _key_text(value):
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


class Rule:
    """A compiled rule: type match, predicate, throttle, and an action template."""

    __slots__ = (
        "rule_id", "enabled", "why", "notes", "event_type", "predicate",
        "throttle", "action_kind", "priority", "ttl", "ttl_seconds", "task",
        "source", "raw",
    )

    def __init__(
        self,
        rule_id,
        enabled,
        why,
        event_type,
        action_kind,
        priority,
        task,
        notes=None,
        predicate=None,
        throttle=None,
        ttl=None,
        ttl_seconds=None,
        source=None,
        raw=None,
    ):
        self.rule_id = rule_id
        self.enabled = enabled
        self.why = why
        self.notes = notes
        self.event_type = event_type
        self.predicate = predicate
        self.throttle = throttle
        self.action_kind = action_kind
        self.priority = priority
        self.ttl = ttl
        self.ttl_seconds = ttl_seconds
        self.task = task
        self.source = source
        self.raw = raw

    def matches(self, event):
        """True when *event* is this rule's type and satisfies its predicate."""
        if not isinstance(event, dict) or event.get("type") != self.event_type:
            return False
        if self.predicate is None:
            return True
        try:
            return bool(self.predicate(event))
        except Exception:
            # Compilation validated the shape; a runtime failure here means
            # ragged event data, which must never abort a tick.
            return False

    def __repr__(self):
        return "Rule(%r, on=%r, enabled=%r)" % (self.rule_id, self.event_type, self.enabled)


class LoadResult:
    """Everything :func:`load_rules` found: the good rules and the bad files."""

    __slots__ = ("rules", "errors")

    def __init__(self, rules, errors):
        self.rules = rules
        self.errors = errors

    @property
    def enabled_rules(self):
        return [rule for rule in self.rules if rule.enabled]

    @property
    def by_id(self):
        return {rule.rule_id: rule for rule in self.rules}

    def __repr__(self):
        return "LoadResult(rules=%d, errors=%d)" % (len(self.rules), len(self.errors))


# --------------------------------------------------------------------------
# validation helpers
# --------------------------------------------------------------------------

def _fail(message, location=None, rule_id=None, source=None):
    raise RuleValidationError(message, location=location, rule_id=rule_id, source=source)


def _require_object(value, location, ctx):
    if not isinstance(value, dict):
        _fail("expected a JSON object, got %s" % type(value).__name__, location, **ctx)
    return value


def _reject_unknown(value, allowed, location, ctx):
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        _fail(
            "unknown key(s) %s; allowed keys are: %s"
            % (", ".join(repr(key) for key in unknown), ", ".join(allowed)),
            location,
            **ctx
        )


def parse_rule(data, source=None, expected_rule_id=None):
    """Validate *data* and return a compiled :class:`Rule`.

    Raises :class:`RuleValidationError` naming the file and the location of the
    first problem.  Fail on the first error rather than collecting them: a rule
    file is short, and a cascade of derived complaints is less useful than one
    precise one.
    """
    ctx = {"source": source}
    if not isinstance(data, dict):
        _fail("rule must be a JSON object, got %s" % type(data).__name__, None, **ctx)

    rule_id = data.get("rule_id")
    if not isinstance(rule_id, str) or not RULE_ID_RE.match(rule_id):
        _fail(
            "'rule_id' must be a short token matching %s, got %r"
            % (RULE_ID_RE.pattern, rule_id),
            "rule_id",
            **ctx
        )
    ctx["rule_id"] = rule_id
    if expected_rule_id is not None and rule_id != expected_rule_id:
        _fail(
            "'rule_id' %r does not match the filename stem %r; one rule per file, "
            "named for its id" % (rule_id, expected_rule_id),
            "rule_id",
            **ctx
        )

    _reject_unknown(data, RULE_KEYS, None, ctx)

    why = data.get("why")
    if not isinstance(why, str) or not why.strip() or len(why) > MAX_WHY_LENGTH:
        _fail(
            "'why' is required: one line (max %d chars) saying what this rule is for"
            % MAX_WHY_LENGTH,
            "why",
            **ctx
        )

    notes = data.get("notes")
    if notes is not None and (not isinstance(notes, str) or len(notes) > MAX_NOTES_LENGTH):
        _fail("'notes' must be a string of at most %d chars" % MAX_NOTES_LENGTH, "notes", **ctx)

    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        _fail("'enabled' must be true or false, got %r" % (enabled,), "enabled", **ctx)

    event_type, predicate = _parse_on(data.get("on"), ctx)
    throttle = _parse_throttle(data.get("throttle"), ctx)
    action_kind, priority, ttl, ttl_seconds, task = _parse_action(data.get("action"), ctx)

    return Rule(
        rule_id=rule_id,
        enabled=enabled,
        why=why,
        notes=notes,
        event_type=event_type,
        predicate=predicate,
        throttle=throttle,
        action_kind=action_kind,
        priority=priority,
        ttl=ttl,
        ttl_seconds=ttl_seconds,
        task=task,
        source=source,
        raw=data,
    )


def _parse_on(on, ctx):
    if on is None:
        _fail("'on' is required: which event type this rule listens to", "on", **ctx)
    _require_object(on, "on", ctx)
    _reject_unknown(on, ON_KEYS, "on", ctx)

    event_type = on.get("type")
    if not isinstance(event_type, str) or not event_type:
        _fail("'on.type' must be an event type string, got %r" % (event_type,), "on.type", **ctx)

    where = on.get("where")
    if where is None:
        return event_type, None
    try:
        return event_type, compile_predicate(where, location="on.where")
    except PredicateError as exc:
        _fail(exc.message, exc.location, **ctx)


def _parse_throttle(throttle, ctx):
    if throttle is None:
        return None
    _require_object(throttle, "throttle", ctx)
    _reject_unknown(throttle, THROTTLE_KEYS, "throttle", ctx)

    key = throttle.get("key")
    if (
        not isinstance(key, list)
        or not key
        or len(key) > MAX_THROTTLE_KEY_PARTS
        or not all(isinstance(part, str) and part for part in key)
    ):
        _fail(
            "'throttle.key' must be a list of 1..%d non-empty strings (the literal "
            "'rule_id', a path such as 'event.entities.player.id', or a constant), got %r"
            % (MAX_THROTTLE_KEY_PARTS, key),
            "throttle.key",
            **ctx
        )
    for index, part in enumerate(key):
        if is_path(part):
            try:
                validate_path(part)
            except PathError as exc:
                _fail(exc.message, "throttle.key[%d]" % index, **ctx)

    try:
        window = parse_window(throttle.get("window"))
    except DurationError as exc:
        _fail(exc.message, "throttle.window", **ctx)
    return ThrottleSpec(key, window)


def _parse_action(action, ctx):
    if action is None:
        _fail("'action' is required: what to emit when this rule fires", "action", **ctx)
    _require_object(action, "action", ctx)
    _reject_unknown(action, ACTION_KEYS, "action", ctx)

    kind = action.get("kind")
    if kind not in ACTION_KINDS:
        _fail(
            "'action.kind' must be one of: %s; got %r" % (", ".join(ACTION_KINDS), kind),
            "action.kind",
            **ctx
        )

    priority = action.get("priority", DEFAULT_PRIORITY)
    if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 100:
        _fail(
            "'action.priority' must be an integer 0..100, got %r" % (priority,),
            "action.priority",
            **ctx
        )

    ttl = action.get("ttl")
    ttl_seconds = None
    if ttl is not None:
        try:
            ttl_seconds = parse_duration(ttl)
        except DurationError as exc:
            _fail(exc.message, "action.ttl", **ctx)

    task = action.get("task")
    if task is None:
        _fail("'action.task' is required: the task body to emit", "action.task", **ctx)
    _require_object(task, "action.task", ctx)

    task_kind = task.get("kind")
    if task_kind not in TASK_KINDS:
        _fail(
            "'action.task.kind' must be one of: %s; got %r" % (", ".join(TASK_KINDS), task_kind),
            "action.task.kind",
            **ctx
        )
    if task_kind not in ALLOWED_TASK_KINDS[kind]:
        _fail(
            "action kind %r requires a task of kind %s, got %r"
            % (kind, " or ".join(repr(k) for k in ALLOWED_TASK_KINDS[kind]), task_kind),
            "action.task.kind",
            **ctx
        )

    # Validate every {{event.path}} now, so a typo in fills_hints is a startup
    # error rather than an empty field on a task nobody can fill.
    try:
        collect_placeholders(task, location="action.task")
    except TemplateError as exc:
        _fail(exc.message, exc.location, **ctx)

    return kind, priority, ttl, ttl_seconds, task


# --------------------------------------------------------------------------
# directory loading
# --------------------------------------------------------------------------

def _read_rule_file(path, name):
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise RuleValidationError("cannot stat rule file: %s" % exc, source=name) from None
    if size > MAX_RULE_FILE_BYTES:
        raise RuleValidationError(
            "rule file is %d bytes (max %d)" % (size, MAX_RULE_FILE_BYTES), source=name
        )
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise RuleValidationError("cannot read rule file: %s" % exc, source=name) from None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RuleValidationError("rule file is not valid UTF-8: %s" % exc, source=name) from None
    try:
        return json.loads(text)
    except ValueError as exc:
        raise RuleValidationError("rule file is not valid JSON: %s" % exc, source=name) from None
    except RecursionError:
        raise RuleValidationError(
            "rule file JSON is nested too deeply to parse", source=name
        ) from None


def load_rules(rules_dir):
    """Load every ``*.json`` in *rules_dir*, collecting rules and errors separately."""
    rules = []
    errors = []
    try:
        names = sorted(os.listdir(rules_dir))
    except OSError as exc:
        return LoadResult(
            [], [RuleValidationError("cannot read rules directory: %s" % exc, source=rules_dir)]
        )

    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(rules_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            data = _read_rule_file(path, name)
            rule = parse_rule(data, source=name, expected_rule_id=name[:-len(".json")])
        except RuleValidationError as exc:
            errors.append(exc)
            continue
        rule.source = path
        rules.append(rule)

    rules.sort(key=lambda rule: rule.rule_id)
    return LoadResult(rules, errors)
