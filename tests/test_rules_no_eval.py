"""The no-eval guarantee, enforced in code rather than asserted in a comment.

The whole reason signal-hub's rules are a tiny hand-written interpreter instead
of "just eval a Python expression" is that rule files are data on disk: cheap to
add, edited without review, and read by a scheduled task running as the user.

So the property "a rule file cannot express computation" is not a style
preference - it is the security boundary of this lane, and it is checked three
ways:

* a source scan (no eval/exec/compile/import machinery in the package at all),
* an AST scan (the same, immune to the scan being fooled by formatting),
* and behavioural tests (code-shaped rule content stays inert data end to end).

If a future change needs one of the banned names, that is a design conversation,
not a test to relax.
"""

import ast
import datetime
import json
import os
import re
import tempfile
import unittest

import signal_hub.rules_engine as rules_engine
from signal_hub.rules_engine import RuleEngine, ThrottleState, load_rules, parse_rule

PACKAGE_DIR = os.path.dirname(os.path.abspath(rules_engine.__file__))

#: Names that can turn data into execution.  None of them belong in this package.
BANNED_NAMES = frozenset({
    "eval",
    "exec",
    "compile",
    "__import__",
    "getattr",
    "setattr",
    "delattr",
    "globals",
    "locals",
    "vars",
    "input",
    "breakpoint",
    "memoryview",
})

#: Modules that can execute, deserialise into objects, or reach the network.
BANNED_MODULES = frozenset({
    "ast",
    "code",
    "codeop",
    "ctypes",
    "importlib",
    "marshal",
    "multiprocessing",
    "pickle",
    "runpy",
    "shelve",
    "socket",
    "subprocess",
    "sys",
    "types",
    "urllib",
})

#: Everything the package is allowed to import.  Stdlib only, and a short list.
ALLOWED_IMPORTS = frozenset({"datetime", "fnmatch", "json", "operator", "os", "re"})

BANNED_SOURCE_PATTERNS = [
    r"\beval\s*\(",
    r"\bexec\s*\(",
    # bare compile(), but not the stdlib re.compile() used for the placeholder scan
    r"(?<![\w.])compile\s*\(",
    r"\bliteral_eval\b",
    r"\b__import__\b",
    r"\bos\.system\b",
    r"\bos\.popen\b",
    r"\bos\.exec",
    r"\bos\.spawn",
    r"\b__globals__\b",
    r"\b__builtins__\b",
    r"\b__subclasses__\b",
]


def _is_re_compile(node):
    """The one legitimate 'compile': re.compile() for the placeholder scanner."""
    return (
        node.attr == "compile"
        and isinstance(node.value, ast.Name)
        and node.value.id == "re"
    )


def package_sources():
    for name in sorted(os.listdir(PACKAGE_DIR)):
        if name.endswith(".py"):
            path = os.path.join(PACKAGE_DIR, name)
            with open(path, "r", encoding="utf-8") as handle:
                yield name, handle.read()


class SourceScanTests(unittest.TestCase):
    def test_the_package_has_modules_to_scan(self):
        self.assertGreaterEqual(len(list(package_sources())), 8)

    def test_no_execution_primitives_appear_in_the_source(self):
        for name, source in package_sources():
            for pattern in BANNED_SOURCE_PATTERNS:
                self.assertIsNone(
                    re.search(pattern, source), "%s matches %s" % (name, pattern)
                )

    def test_sources_are_ascii(self):
        for name in sorted(os.listdir(PACKAGE_DIR)):
            if name.endswith(".py"):
                with open(os.path.join(PACKAGE_DIR, name), "rb") as handle:
                    self.assertTrue(handle.read().isascii(), name)


class AstScanTests(unittest.TestCase):
    def trees(self):
        for name, source in package_sources():
            yield name, ast.parse(source, filename=name)

    def test_no_banned_names_are_referenced(self):
        for name, tree in self.trees():
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    self.assertNotIn(node.id, BANNED_NAMES, "%s uses %s" % (name, node.id))
                if isinstance(node, ast.Attribute) and not _is_re_compile(node):
                    self.assertNotIn(
                        node.attr, BANNED_NAMES, "%s uses .%s" % (name, node.attr)
                    )

    def test_imports_are_a_short_stdlib_allowlist(self):
        for name, tree in self.trees():
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".")[0]
                        self.assertNotIn(root, BANNED_MODULES, "%s imports %s" % (name, root))
                        self.assertIn(root, ALLOWED_IMPORTS, "%s imports %s" % (name, root))
                if isinstance(node, ast.ImportFrom) and node.level == 0:
                    root = (node.module or "").split(".")[0]
                    self.assertNotIn(root, BANNED_MODULES, "%s imports from %s" % (name, root))
                    self.assertIn(root, ALLOWED_IMPORTS, "%s imports from %s" % (name, root))

    def test_no_dynamic_attribute_access_on_event_data(self):
        # The resolver must use subscripting only; an ast.Attribute whose value
        # is a variable named like event data would be a hole.
        for name, source in package_sources():
            self.assertNotIn("getattr", source, name)


class InertRuleContentTests(unittest.TestCase):
    """Code-shaped strings in a rule file stay strings, end to end."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "rules")
        os.makedirs(self.dir)
        self.canary = os.path.join(self.tmp.name, "PWNED")
        self.clock = lambda: datetime.datetime(
            2026, 9, 14, 23, 41, tzinfo=datetime.timezone.utc
        )

    def hostile_rule(self, payload_string):
        return {
            "rule_id": "R999-hostile",
            "why": "Hostile fixture: every string here must stay data.",
            "on": {"type": "player_stat_line", "where": {"==": ["event.payload.x", 1]}},
            "throttle": {"key": ["rule_id"], "window": "1d"},
            "action": {
                "kind": "enqueue_task",
                "priority": 50,
                "ttl": "1d",
                "task": {
                    "kind": "instantiate_template",
                    "template": payload_string,
                    "account": payload_string,
                },
            },
        }

    def run_rule(self, rule_data):
        path = os.path.join(self.dir, "R999-hostile.json")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(rule_data, handle)
        result = load_rules(self.dir)
        self.assertEqual(result.errors, [], "rule should load: %s" % result.errors)
        state = ThrottleState.load(os.path.join(self.tmp.name, "s.jsonl"), now=self.clock)
        engine = RuleEngine(result.rules, state, now=self.clock)
        return engine.evaluate([
            {"event_id": "e1", "type": "player_stat_line", "payload": {"x": 1}}
        ]).actions

    def test_python_injection_stays_a_string(self):
        payload = "__import__('os').system('echo pwned > %s')" % self.canary
        actions = self.run_rule(self.hostile_rule(payload))
        self.assertEqual(actions[0]["task"]["template"], payload)
        self.assertFalse(os.path.exists(self.canary))

    def test_format_string_injection_stays_a_string(self):
        payload = "{0.__class__.__mro__[1].__subclasses__()}"
        actions = self.run_rule(self.hostile_rule(payload))
        self.assertEqual(actions[0]["task"]["template"], payload)

    def test_shell_injection_stays_a_string(self):
        payload = "; rm -rf / #"
        self.assertEqual(self.run_rule(self.hostile_rule(payload))[0]["task"]["template"], payload)

    def test_path_traversal_in_a_task_field_stays_a_string(self):
        payload = "../../../../Windows/System32/calc.exe"
        self.assertEqual(self.run_rule(self.hostile_rule(payload))[0]["task"]["template"], payload)

    def test_dunder_predicate_paths_cannot_reach_python_objects(self):
        rule = self.hostile_rule("plain")
        rule["on"]["where"] = {
            "any": [
                {"!=": ["event.__class__", None]},
                {"!=": ["event.__class__.__mro__", None]},
                {"!=": ["event.payload.__class__.__base__.__subclasses__", None]},
                {"!=": ["event.__init__.__globals__", None]},
            ]
        }
        # Every path misses, every comparison against MISSING is False, so the
        # rule does not fire - and crucially, nothing raised on the way.
        self.assertEqual(self.run_rule(rule), [])

    def test_a_rule_id_cannot_escape_the_rules_directory(self):
        rule = self.hostile_rule("plain")
        rule["rule_id"] = "../../evil"
        path = os.path.join(self.dir, "R999-hostile.json")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(rule, handle)
        result = load_rules(self.dir)
        self.assertEqual(result.rules, [])
        self.assertEqual(len(result.errors), 1)


class ShippedRulesAreInertTests(unittest.TestCase):
    def test_no_shipped_rule_contains_execution_shaped_syntax(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        rules_dir = os.path.join(repo_root, "rules")
        for name in os.listdir(rules_dir):
            with open(os.path.join(rules_dir, name), "r", encoding="utf-8") as handle:
                text = handle.read()
            for banned in ("__import__", "eval(", "exec(", "os.system", "subprocess"):
                self.assertNotIn(banned, text, "%s contains %s" % (name, banned))


if __name__ == "__main__":
    unittest.main()
