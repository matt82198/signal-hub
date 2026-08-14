"""Exception types for the signal-hub rules engine.

Every error carries a *location* — a dotted breadcrumb such as
``on.where.all[1].any[0]`` — so a malformed rule file produces a message a human
can act on without opening a debugger.
"""


class RulesError(Exception):
    """Base class for every rules-engine failure."""

    def __init__(self, message, location=None):
        self.message = message
        self.location = location
        super().__init__(self.__str__())

    def __str__(self):
        if self.location:
            return "%s: %s" % (self.location, self.message)
        return self.message


class PathError(RulesError):
    """A path operand is not a well-formed ``event.``-rooted lookup."""


class PredicateError(RulesError):
    """A predicate node is structurally invalid or exceeds a resource bound."""


class TemplateError(RulesError):
    """An action template holds a malformed or oversized placeholder."""


class DurationError(RulesError):
    """A ttl or throttle window is not a recognised duration."""


class RuleValidationError(RulesError):
    """A rule file does not satisfy the rule schema."""

    def __init__(self, message, location=None, rule_id=None, source=None):
        self.rule_id = rule_id
        self.source = source
        super().__init__(message, location)

    def __str__(self):
        parts = []
        if self.source:
            parts.append(str(self.source))
        if self.location:
            parts.append(self.location)
        parts.append(self.message)
        return ": ".join(parts)
