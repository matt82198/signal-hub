"""The signal-hub predicate DSL: a tiny interpreter over JSON data.

Grammar (every node is a JSON object with exactly one key)::

    node     := {"all": [node, ...]}          # every child true
              | {"any": [node, ...]}          # some child true
              | {"not": node}                 # or {"not": [node]}
              | {<comparator>: [operand, operand]}
    operand  := "event.<dotted.path>"         # resolved against the event
              | [operand, ...]                # list literal, elements resolved
              | {"lit": <any JSON>}           # escape: take this verbatim
              | <any other JSON scalar>       # literal

    comparator := "==" | "!=" | ">" | ">=" | "<" | "<=" | "in" | "contains" | "matches"

Rules:

* ``matches`` is **fnmatch glob, not regex** - no ReDoS surface.
* A path that resolves to nothing yields ``MISSING``, and *every* comparison
  involving ``MISSING`` is ``False``.  Absent data can never satisfy a rule.
* Type mismatches are ``False``, never exceptions.  ``True`` is not ``1``.
* There is no ``eval``, no ``exec``, no ``ast``, no attribute access, no
  arithmetic and no function calls.  A rule file cannot express computation.

A rule is compiled once at load time into a closure tree; compilation is where
every structural error and resource bound is enforced, so evaluation is a hot
loop that cannot raise.
"""

import fnmatch
import operator

from .errors import PathError, PredicateError
from .paths import MISSING, is_path, resolve_path, validate_path

#: Resource bounds.  Rule files are untrusted data from disk.
MAX_DEPTH = 12
MAX_NODES = 256
MAX_CHILDREN = 64
MAX_LIST_LITERAL = 128
MAX_GLOB_LENGTH = 128
MAX_GLOB_STARS = 8
MAX_GLOB_SUBJECT = 4096

COMPARATORS = ("==", "!=", ">", ">=", "<", "<=", "in", "contains", "matches")
LOGICAL = ("all", "any", "not")
OPERATORS = tuple(sorted(COMPARATORS + LOGICAL))

_ORDER_OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}


# --------------------------------------------------------------------------
# value helpers
# --------------------------------------------------------------------------

def _is_number(value):
    """True for real numbers.  ``bool`` is deliberately excluded."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _eq_core(left, right):
    """Equality that refuses to conflate ``True`` with ``1`` and never raises."""
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    try:
        return bool(left == right)
    except Exception:
        return False


def _eq(left, right):
    if left is MISSING or right is MISSING:
        return False
    return _eq_core(left, right)


def _ne(left, right):
    if left is MISSING or right is MISSING:
        return False
    return not _eq_core(left, right)


def _ordered(op):
    func = _ORDER_OPS[op]

    def compare(left, right):
        if left is MISSING or right is MISSING:
            return False
        if _is_number(left) and _is_number(right):
            pass
        elif isinstance(left, str) and isinstance(right, str):
            pass
        else:
            return False
        try:
            return bool(func(left, right))
        except Exception:
            return False

    return compare


def _member_of(value, container):
    """``value in container`` for lists, strings and dict keys.  Never raises."""
    if value is MISSING or container is MISSING:
        return False
    if isinstance(container, str):
        return isinstance(value, str) and value in container
    if isinstance(container, dict):
        return any(_eq_core(value, key) for key in container)
    if isinstance(container, (list, tuple)):
        return any(_eq_core(value, item) for item in container)
    return False


def _contains(container, value):
    return _member_of(value, container)


def _matches(subject, pattern):
    """fnmatch glob.  Bounded on both sides so a hostile rule cannot burn a tick."""
    if subject is MISSING or pattern is MISSING:
        return False
    if not isinstance(subject, str) or not isinstance(pattern, str):
        return False
    if len(pattern) > MAX_GLOB_LENGTH or pattern.count("*") > MAX_GLOB_STARS:
        return False
    if len(subject) > MAX_GLOB_SUBJECT:
        return False
    try:
        return bool(fnmatch.fnmatchcase(subject, pattern))
    except Exception:
        return False


_COMPARATOR_FUNCS = {
    "==": _eq,
    "!=": _ne,
    ">": _ordered(">"),
    ">=": _ordered(">="),
    "<": _ordered("<"),
    "<=": _ordered("<="),
    "in": _member_of,
    "contains": _contains,
    "matches": _matches,
}


# --------------------------------------------------------------------------
# compilation
# --------------------------------------------------------------------------

def compile_predicate(node, location="where", max_depth=MAX_DEPTH):
    """Validate *node* and return ``callable(event) -> bool``.

    Raises :class:`PredicateError` with an actionable location on anything
    malformed.  The returned callable never raises, whatever the event holds.
    """
    budget = {"nodes": 0}
    return _compile_node(node, location, 1, budget, max_depth)


def _spend(budget, location):
    budget["nodes"] += 1
    if budget["nodes"] > MAX_NODES:
        raise PredicateError(
            "predicate has too many nodes (max %d)" % MAX_NODES, location
        )


def _compile_node(node, location, depth, budget, max_depth):
    if depth > max_depth:
        raise PredicateError(
            "predicate nesting exceeds the maximum depth of %d" % max_depth, location
        )
    _spend(budget, location)

    if not isinstance(node, dict):
        raise PredicateError(
            "predicate must be a JSON object with exactly one key (got %s)"
            % type(node).__name__,
            location,
        )
    if len(node) != 1:
        raise PredicateError(
            "predicate object must have exactly one key, got %d: %s"
            % (len(node), sorted(node)),
            location,
        )

    op, argument = next(iter(node.items()))
    if op in ("all", "any"):
        return _compile_junction(op, argument, location, depth, budget, max_depth)
    if op == "not":
        return _compile_not(argument, location, depth, budget, max_depth)
    if op in COMPARATORS:
        return _compile_comparison(op, argument, location, depth, budget)
    raise PredicateError(
        "unknown operator %r; valid operators are: %s" % (op, ", ".join(OPERATORS)),
        location,
    )


def _compile_junction(op, argument, location, depth, budget, max_depth):
    if not isinstance(argument, list) or not argument:
        raise PredicateError(
            "%r expects a non-empty list of predicates (got %s)"
            % (op, type(argument).__name__),
            location,
        )
    if len(argument) > MAX_CHILDREN:
        raise PredicateError(
            "too many children for %r: %d (max %d)" % (op, len(argument), MAX_CHILDREN),
            location,
        )
    children = [
        _compile_node(child, "%s.%s[%d]" % (location, op, index), depth + 1, budget, max_depth)
        for index, child in enumerate(argument)
    ]
    if op == "all":
        def evaluate_all(event):
            for child in children:
                if not child(event):
                    return False
            return True
        return evaluate_all

    def evaluate_any(event):
        for child in children:
            if child(event):
                return True
        return False
    return evaluate_any


def _compile_not(argument, location, depth, budget, max_depth):
    if isinstance(argument, list):
        if len(argument) != 1:
            raise PredicateError(
                "'not' expects exactly one child predicate, got %d" % len(argument), location
            )
        argument = argument[0]
    elif not isinstance(argument, dict):
        raise PredicateError(
            "'not' expects exactly one child predicate (got %s)" % type(argument).__name__,
            location,
        )
    child = _compile_node(argument, "%s.not" % location, depth + 1, budget, max_depth)

    def evaluate_not(event):
        return not child(event)
    return evaluate_not


def _compile_comparison(op, argument, location, depth, budget):
    if not isinstance(argument, list):
        raise PredicateError(
            "operator %r expects a list of exactly 2 operands (got %s)"
            % (op, type(argument).__name__),
            location,
        )
    if len(argument) != 2:
        raise PredicateError(
            "operator %r expects a list of exactly 2 operands, got %d"
            % (op, len(argument)),
            location,
        )
    if op == "matches" and isinstance(argument[1], str) and not is_path(argument[1]):
        _validate_glob(argument[1], "%s.%s[1]" % (location, op))

    left = _compile_operand(argument[0], "%s.%s[0]" % (location, op), depth + 1, budget)
    right = _compile_operand(argument[1], "%s.%s[1]" % (location, op), depth + 1, budget)
    func = _COMPARATOR_FUNCS[op]

    def evaluate(event):
        return bool(func(left(event), right(event)))
    return evaluate


def _validate_glob(pattern, location):
    if len(pattern) > MAX_GLOB_LENGTH:
        raise PredicateError(
            "glob pattern is %d characters (max %d)" % (len(pattern), MAX_GLOB_LENGTH),
            location,
        )
    if pattern.count("*") > MAX_GLOB_STARS:
        raise PredicateError(
            "glob pattern has %d '*' wildcards (max %d)"
            % (pattern.count("*"), MAX_GLOB_STARS),
            location,
        )


def _compile_operand(value, location, depth, budget):
    if depth > MAX_DEPTH:
        raise PredicateError("operand nesting exceeds the maximum depth", location)
    _spend(budget, location)

    if isinstance(value, dict):
        if len(value) == 1 and "lit" in value:
            literal = value["lit"]
            return lambda event, literal=literal: literal
        raise PredicateError(
            "object operands are not allowed; wrap a literal as {\"lit\": ...}", location
        )

    if isinstance(value, list):
        if len(value) > MAX_LIST_LITERAL:
            raise PredicateError(
                "list operand has %d items (max %d)" % (len(value), MAX_LIST_LITERAL),
                location,
            )
        parts = [
            _compile_operand(item, "%s[%d]" % (location, index), depth + 1, budget)
            for index, item in enumerate(value)
        ]
        return lambda event: [part(event) for part in parts]

    if is_path(value):
        try:
            validate_path(value)
        except PathError as exc:
            raise PredicateError("invalid path %r: %s" % (value, exc.message), location) from None
        return lambda event, path=value: resolve_path(event, path)

    return lambda event, literal=value: literal
