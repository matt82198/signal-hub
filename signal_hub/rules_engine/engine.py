"""The rule evaluation engine: events in, fired actions out (design section 4).

For each event, in rule_id order, every enabled rule runs four checks:

1. **Type** - does this rule listen to this event type?
2. **Predicate** - does the event satisfy ``on.where``?
3. **Duplicate gate** - has this rule already fired for this exact event, ever?
4. **Declared throttle** - has it already fired for this key in this window?

Only then does it render the action.  **Actions are returned as data, never
executed.** Nothing in this module writes a task file, touches ``queue/``, runs
a subprocess or sends a notification - the queue lane owns dispatch.  That
boundary is what lets the entire trigger layer be tested with dict fixtures and
a frozen clock, and it is asserted in the tests.

Nothing here reads the system clock: the caller injects ``now``.
"""

from .durations import format_timestamp, shift
from .errors import TemplateError
from .template import render_template

_COUNTERS = ("matched", "fired", "duplicate", "throttled", "unresolved_key", "errors")


class EvaluationResult:
    """The actions a batch produced, plus the counters ``hub-status.json`` wants."""

    __slots__ = ("actions", "stats")

    def __init__(self, actions, stats):
        self.actions = actions
        self.stats = stats

    def __repr__(self):
        return "EvaluationResult(actions=%d, fired=%d)" % (
            len(self.actions), self.stats.get("fired", 0)
        )


class RuleEngine:
    """Matches events against compiled rules and emits action data."""

    def __init__(self, rules, throttle_state, *, now):
        if not callable(now):
            raise TypeError("now must be a callable returning an aware UTC datetime")
        self.rules = sorted(rules, key=lambda rule: rule.rule_id)
        self.throttle = throttle_state
        self._now = now

    # -- public API ------------------------------------------------------

    def evaluate(self, events, record=True):
        """Evaluate *events* in order and return an :class:`EvaluationResult`.

        With ``record=False`` the gates are consulted but never armed - a dry
        run that shows what *would* fire without spending the one-shot budget.
        """
        stats = self._fresh_stats()
        actions = []
        for event in events:
            stats["events"] += 1
            if not _is_valid_event(event):
                stats["invalid_events"] += 1
                continue
            actions.extend(self._evaluate_one(event, stats, record))
        stats["suppressed"] = (
            stats["duplicate"] + stats["throttled"] + stats["unresolved_key"]
        )
        return EvaluationResult(actions, stats)

    def evaluate_event(self, event, record=True):
        """Evaluate a single event and return its fired actions."""
        return self.evaluate([event], record=record).actions

    # -- internals -------------------------------------------------------

    def _fresh_stats(self):
        return {
            "events": 0,
            "invalid_events": 0,
            "matched": 0,
            "fired": 0,
            "duplicate": 0,
            "throttled": 0,
            "unresolved_key": 0,
            "suppressed": 0,
            "errors": 0,
            # Every loaded rule appears, including ones that never fire: a rule
            # firing zero times is exactly what rule-sprawl review needs to see.
            "by_rule": {
                rule.rule_id: {name: 0 for name in _COUNTERS} for rule in self.rules
            },
        }

    def _evaluate_one(self, event, stats, record):
        actions = []
        event_id = event["event_id"]
        for rule in self.rules:
            if not rule.enabled or not rule.matches(event):
                continue
            self._count(stats, rule, "matched")

            if self.throttle.is_duplicate(rule.rule_id, event_id):
                self._count(stats, rule, "duplicate")
                continue

            key, window = self._throttle_key(rule, event)
            if key is _UNRESOLVED:
                # Fail closed.  An unresolvable throttle key is precisely the
                # case where a rule would otherwise fire unthrottled and spam.
                self._count(stats, rule, "unresolved_key")
                continue
            if self.throttle.is_throttled(rule.rule_id, key, window):
                self._count(stats, rule, "throttled")
                continue

            action = self._render(rule, event, stats)
            if action is None:
                continue
            if record:
                self.throttle.record(rule.rule_id, event_id, throttle_key=key, window=window)
            self._count(stats, rule, "fired")
            actions.append(action)
        return actions

    def _throttle_key(self, rule, event):
        if rule.throttle is None:
            return None, None
        key = rule.throttle.resolve(event, rule.rule_id)
        if key is None:
            return _UNRESOLVED, rule.throttle.window
        return key, rule.throttle.window

    def _render(self, rule, event, stats):
        moment = self._now()
        try:
            task, missing = render_template(rule.task, event, location="action.task")
        except TemplateError:
            # The loader validated every placeholder, so this is defence in
            # depth: count it, skip the rule, never abort the tick.
            self._count(stats, rule, "errors")
            return None
        return {
            "rule_id": rule.rule_id,
            "event_id": event["event_id"],
            "event_type": event["type"],
            "kind": rule.action_kind,
            "priority": rule.priority,
            "ttl": rule.ttl,
            "fired_at": format_timestamp(moment),
            "expires_at": (
                format_timestamp(shift(moment, rule.ttl_seconds))
                if rule.ttl_seconds is not None
                else None
            ),
            "task": task,
            "missing_fills": missing,
        }

    def _count(self, stats, rule, name):
        stats[name] += 1
        stats["by_rule"][rule.rule_id][name] += 1


class _Unresolved:
    __slots__ = ()

    def __repr__(self):
        return "UNRESOLVED"


_UNRESOLVED = _Unresolved()


def _is_valid_event(event):
    """An event needs a string id and a string type; anything else is not one."""
    return (
        isinstance(event, dict)
        and isinstance(event.get("event_id"), str)
        and isinstance(event.get("type"), str)
    )
