"""signal-hub rules engine (design section 4).

Events in, fired actions out.  Rules are *data files* under ``rules/``; this
package is the small interpreter that reads them:

* :mod:`paths`      - dotted lookup with a MISSING sentinel
* :mod:`predicates` - the comparison/boolean DSL
* :mod:`template`   - ``{{event.path}}`` substitution in actions

Hard constraint: there is no ``eval``, ``exec``, ``compile`` or ``ast`` anywhere
in this package, and no rule file can introduce one.  A rule expresses
comparisons over event data and nothing else.

Actions are returned as data.  Dispatching them (writing task files into
``queue/``) belongs to the queue lane, not here.
"""

from .errors import (
    DurationError,
    PathError,
    PredicateError,
    RulesError,
    RuleValidationError,
    TemplateError,
)
from .paths import MISSING, is_path, resolve_path, validate_path
from .predicates import COMPARATORS, LOGICAL, OPERATORS, compile_predicate
from .template import collect_placeholders, render_template

__all__ = [
    "COMPARATORS",
    "DurationError",
    "LOGICAL",
    "MISSING",
    "OPERATORS",
    "PathError",
    "PredicateError",
    "RuleValidationError",
    "RulesError",
    "TemplateError",
    "collect_placeholders",
    "compile_predicate",
    "is_path",
    "render_template",
    "resolve_path",
    "validate_path",
]
