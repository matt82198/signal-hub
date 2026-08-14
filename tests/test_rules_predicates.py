"""Tests for the signal-hub predicate DSL evaluator (design section 4).

Operators: == != > >= < <= in contains matches all any not.
Operands: literal JSON, or an "event."-prefixed path.  MISSING never satisfies
anything.  There is no eval, exec, or regex anywhere in the language.
"""

import unittest

from signal_hub.rules_engine import MISSING, PredicateError, compile_predicate


EVENT = {
    "event_id": "a3f91c2e8b7d4506",
    "type": "player_stat_line",
    "confidence": 1.0,
    "entities": [
        {"kind": "player", "id": "00-0036322", "name": "Justin Fields"},
        {"kind": "team", "id": "CHI"},
        {"kind": "game", "id": "2026_02_CHI_DET"},
    ],
    "payload": {
        "passing_yards": 312,
        "passing_tds": 3,
        "rushing_yards": 44,
        "game_type": "REG",
        "winner": "CHI",
        "tags": ["primetime", "divisional"],
        "flag": True,
        "note": "Fields threw for 312",
    },
}


def ev(node, event=EVENT):
    return compile_predicate(node)(event)


class ComparatorTests(unittest.TestCase):
    def test_equality(self):
        self.assertTrue(ev({"==": ["event.payload.winner", "CHI"]}))
        self.assertTrue(ev({"==": ["CHI", "event.payload.winner"]}))
        self.assertFalse(ev({"==": ["event.payload.winner", "DET"]}))
        self.assertTrue(ev({"==": ["event.payload.passing_tds", 3]}))

    def test_inequality(self):
        self.assertTrue(ev({"!=": ["event.payload.winner", "DET"]}))
        self.assertFalse(ev({"!=": ["event.payload.winner", "CHI"]}))

    def test_ordering(self):
        self.assertTrue(ev({">=": ["event.payload.passing_yards", 300]}))
        self.assertTrue(ev({">": ["event.payload.passing_yards", 311]}))
        self.assertFalse(ev({">": ["event.payload.passing_yards", 312]}))
        self.assertTrue(ev({"<=": ["event.payload.rushing_yards", 44]}))
        self.assertTrue(ev({"<": ["event.payload.rushing_yards", 100]}))

    def test_ordering_on_floats_and_ints_mixes(self):
        self.assertTrue(ev({">=": ["event.confidence", 0.9]}))
        self.assertTrue(ev({">=": ["event.payload.passing_yards", 300.0]}))

    def test_ordering_on_strings(self):
        self.assertTrue(ev({">": ["event.payload.game_type", "PRE"]}))

    def test_in_against_literal_list(self):
        self.assertTrue(ev({"in": ["event.payload.game_type", ["REG", "POST"]]}))
        self.assertFalse(ev({"in": ["event.payload.game_type", ["PRE"]]}))

    def test_in_against_resolved_list(self):
        self.assertTrue(ev({"in": ["primetime", "event.payload.tags"]}))
        self.assertFalse(ev({"in": ["snow", "event.payload.tags"]}))

    def test_in_against_string_is_substring(self):
        self.assertTrue(ev({"in": ["312", "event.payload.note"]}))
        self.assertFalse(ev({"in": ["999", "event.payload.note"]}))

    def test_in_against_dict_is_key_membership(self):
        self.assertTrue(ev({"in": ["winner", "event.payload"]}))
        self.assertFalse(ev({"in": ["loser", "event.payload"]}))

    def test_contains_is_the_mirror_of_in(self):
        self.assertTrue(ev({"contains": ["event.payload.tags", "divisional"]}))
        self.assertFalse(ev({"contains": ["event.payload.tags", "snow"]}))
        self.assertTrue(ev({"contains": ["event.payload.note", "Fields"]}))

    def test_matches_is_glob_not_regex(self):
        self.assertTrue(ev({"matches": ["event.entities.game.id", "2026_02_CHI_*"]}))
        self.assertTrue(ev({"matches": ["event.entities.team.id", "CH?"]}))
        self.assertFalse(ev({"matches": ["event.entities.team.id", "DET*"]}))
        # A regex that would match under re. must NOT match under fnmatch.
        self.assertFalse(ev({"matches": ["event.entities.team.id", "^CHI$"]}))
        self.assertFalse(ev({"matches": ["event.payload.note", "Fields.*312"]}))

    def test_matches_is_case_sensitive(self):
        self.assertFalse(ev({"matches": ["event.entities.team.id", "chi*"]}))

    def test_matches_on_non_strings_is_false(self):
        self.assertFalse(ev({"matches": ["event.payload.passing_yards", "3*"]}))


class MissingSemanticsTests(unittest.TestCase):
    """Absent data can never accidentally satisfy a rule."""

    def test_every_comparator_is_false_against_missing(self):
        for op, args in [
            ("==", ["event.payload.nope", 1]),
            ("!=", ["event.payload.nope", 1]),
            (">", ["event.payload.nope", 1]),
            (">=", ["event.payload.nope", 1]),
            ("<", ["event.payload.nope", 1]),
            ("<=", ["event.payload.nope", 1]),
            ("in", ["event.payload.nope", ["a"]]),
            ("in", ["a", "event.payload.nope"]),
            ("contains", ["event.payload.nope", "a"]),
            ("matches", ["event.payload.nope", "*"]),
        ]:
            self.assertFalse(ev({op: args}), "%s %r" % (op, args))

    def test_missing_never_equals_missing(self):
        self.assertFalse(ev({"==": ["event.payload.nope", "event.payload.also_nope"]}))

    def test_missing_never_equals_none(self):
        self.assertFalse(ev({"==": ["event.payload.nope", None]}))

    def test_explicit_null_is_comparable(self):
        event = {"payload": {"x": None}}
        self.assertTrue(ev({"==": ["event.payload.x", None]}, event))

    def test_not_of_missing_comparison_is_true(self):
        # This is how a rule expresses "field is absent OR not this value".
        self.assertTrue(ev({"not": {"in": ["event.payload.nope", ["PRE"]]}}))


class TypeSafetyTests(unittest.TestCase):
    def test_mismatched_types_never_raise_and_are_false(self):
        self.assertFalse(ev({">": ["event.payload.game_type", 300]}))
        self.assertFalse(ev({"<": ["event.payload.passing_yards", "REG"]}))
        self.assertFalse(ev({">=": ["event.payload.tags", 3]}))

    def test_booleans_are_not_numbers(self):
        self.assertFalse(ev({"==": ["event.payload.flag", 1]}))
        self.assertTrue(ev({"==": ["event.payload.flag", True]}))
        self.assertFalse(ev({">=": ["event.payload.flag", 0]}))

    def test_in_membership_does_not_conflate_true_and_one(self):
        self.assertFalse(ev({"in": ["event.payload.flag", [1]]}))
        self.assertTrue(ev({"in": ["event.payload.flag", [True]]}))


class LogicalTests(unittest.TestCase):
    def test_all(self):
        self.assertTrue(
            ev({"all": [
                {"in": ["event.payload.game_type", ["REG", "POST"]]},
                {">=": ["event.payload.passing_yards", 300]},
            ]})
        )
        self.assertFalse(
            ev({"all": [
                {"in": ["event.payload.game_type", ["REG", "POST"]]},
                {">=": ["event.payload.passing_yards", 400]},
            ]})
        )

    def test_any(self):
        self.assertTrue(
            ev({"any": [
                {">=": ["event.payload.passing_yards", 300]},
                {">=": ["event.payload.rushing_yards", 100]},
            ]})
        )
        self.assertFalse(
            ev({"any": [
                {">=": ["event.payload.receiving_yards", 100]},
                {">=": ["event.payload.rushing_yards", 100]},
            ]})
        )

    def test_not_accepts_a_node_or_a_single_element_list(self):
        self.assertTrue(ev({"not": {"==": ["event.payload.winner", "DET"]}}))
        self.assertTrue(ev({"not": [{"==": ["event.payload.winner", "DET"]}]}))
        self.assertFalse(ev({"not": {"==": ["event.payload.winner", "CHI"]}}))

    def test_the_r002_shaped_nesting(self):
        node = {"all": [
            {"in": ["event.payload.game_type", ["REG", "POST"]]},
            {"any": [
                {">=": ["event.payload.passing_yards", 300]},
                {">=": ["event.payload.rushing_yards", 100]},
                {">=": ["event.payload.receiving_yards", 100]},
                {">=": ["event.payload.total_tds", 3]},
            ]},
        ]}
        self.assertTrue(ev(node))

    def test_short_circuit_does_not_hide_errors(self):
        # A false first clause must not stop the second from being compiled.
        with self.assertRaises(PredicateError):
            compile_predicate({"all": [
                {"==": ["event.type", "nope"]},
                {"bogus": ["event.type", 1]},
            ]})


class LiteralEscapeTests(unittest.TestCase):
    """{"lit": X} forces X to be data even when it looks like a path."""

    def test_lit_wraps_a_path_shaped_string(self):
        event = {"payload": {"x": "event.payload.secret"}}
        self.assertTrue(ev({"==": ["event.payload.x", {"lit": "event.payload.secret"}]}, event))
        # Without the escape the right operand would resolve (to MISSING here).
        self.assertFalse(ev({"==": ["event.payload.x", "event.payload.secret"]}, event))

    def test_lit_may_hold_any_json(self):
        self.assertTrue(ev({"==": ["event.payload.tags", {"lit": ["primetime", "divisional"]}]}))


class MalformedPredicateTests(unittest.TestCase):
    def _err(self, node):
        with self.assertRaises(PredicateError) as ctx:
            compile_predicate(node)
        return str(ctx.exception)

    def test_node_must_be_a_single_key_object(self):
        self.assertIn("object", self._err("event.payload.x"))
        self.assertIn("object", self._err([{"==": ["a", "b"]}]))
        self.assertIn("exactly one", self._err({}))
        self.assertIn("exactly one", self._err({"==": ["a", "b"], "!=": ["a", "b"]}))

    def test_unknown_operator_lists_the_valid_ones(self):
        msg = self._err({"regex": ["event.payload.note", ".*"]})
        self.assertIn("unknown operator 'regex'", msg)
        self.assertIn("matches", msg)

    def test_comparator_arity(self):
        self.assertIn("exactly 2", self._err({">=": ["event.payload.x"]}))
        self.assertIn("exactly 2", self._err({">=": ["event.payload.x", 1, 2]}))
        self.assertIn("list", self._err({">=": {"a": 1}}))

    def test_logical_operators_need_a_non_empty_list(self):
        self.assertIn("non-empty", self._err({"all": []}))
        self.assertIn("non-empty", self._err({"any": []}))
        self.assertIn("list", self._err({"all": {"==": ["a", "b"]}}))

    def test_not_takes_exactly_one_child(self):
        self.assertIn("exactly one", self._err({"not": [{"==": ["a", "b"]}, {"==": ["c", "d"]}]}))
        self.assertIn("exactly one", self._err({"not": []}))

    def test_object_operands_must_use_the_lit_escape(self):
        msg = self._err({"==": ["event.payload.x", {"passing_yards": 300}]})
        self.assertIn("lit", msg)

    def test_bad_path_operand_reports_the_path(self):
        msg = self._err({"==": ["event..payload", 1]})
        self.assertIn("event..payload", msg)

    def test_error_carries_a_location(self):
        with self.assertRaises(PredicateError) as ctx:
            compile_predicate({"all": [{"any": [{"nope": [1, 2]}]}]}, location="on.where")
        self.assertIn("on.where.all[0].any[0]", str(ctx.exception))


class ResourceBoundTests(unittest.TestCase):
    """Hostile rule files must be rejected, not stack-overflow the tick."""

    def test_deep_nesting_is_capped(self):
        node = {"==": ["event.type", "x"]}
        for _ in range(200):
            node = {"not": node}
        with self.assertRaises(PredicateError) as ctx:
            compile_predicate(node)
        self.assertIn("depth", str(ctx.exception).lower())

    def test_deep_all_nesting_is_capped(self):
        node = {"==": ["event.type", "x"]}
        for _ in range(50):
            node = {"all": [node]}
        with self.assertRaises(PredicateError):
            compile_predicate(node)

    def test_wide_nodes_are_capped(self):
        node = {"any": [{"==": ["event.type", str(i)]} for i in range(5000)]}
        with self.assertRaises(PredicateError) as ctx:
            compile_predicate(node)
        self.assertIn("too", str(ctx.exception).lower())

    def test_glob_patterns_are_length_capped(self):
        with self.assertRaises(PredicateError):
            compile_predicate({"matches": ["event.payload.note", "*" * 200]})

    def test_glob_star_count_is_capped(self):
        with self.assertRaises(PredicateError) as ctx:
            compile_predicate({"matches": ["event.payload.note", "*a" * 40]})
        self.assertIn("glob", str(ctx.exception).lower())

    def test_matches_against_huge_subject_is_bounded(self):
        event = {"payload": {"note": "a" * 100000}}
        self.assertFalse(ev({"matches": ["event.payload.note", "*b*"]}, event))

    def test_literal_lists_are_length_capped(self):
        with self.assertRaises(PredicateError):
            compile_predicate({"in": ["event.type", list(range(5000))]})


class EvaluationNeverRaisesTests(unittest.TestCase):
    def test_compiled_predicate_tolerates_junk_events(self):
        pred = compile_predicate({"all": [
            {">=": ["event.payload.passing_yards", 300]},
            {"in": ["event.payload.game_type", ["REG"]]},
        ]})
        for junk in ({}, {"payload": None}, {"payload": []}, {"payload": "x"}, None, [], 7):
            self.assertFalse(pred(junk))

    def test_missing_sentinel_is_never_returned_by_a_predicate(self):
        pred = compile_predicate({"==": ["event.payload.nope", "event.payload.nope"]})
        self.assertIsNot(pred({}), MISSING)
        self.assertIsInstance(pred({}), bool)


if __name__ == "__main__":
    unittest.main()
