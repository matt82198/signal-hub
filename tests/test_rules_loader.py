"""Tests for rule file loading and validation (design section 4).

Rules are data files.  The loader's job is to turn a directory of JSON into
compiled Rule objects, and to turn anything malformed into an *actionable*
error that names the file, the location inside it, and what was expected -
without ever letting one bad rule stop the others.
"""

import json
import os
import tempfile
import unittest

from signal_hub.rules_engine import Rule, RuleValidationError, load_rules, parse_rule


GOOD = {
    "rule_id": "R900-example",
    "enabled": True,
    "why": "Exercises every field the schema allows.",
    "on": {
        "type": "player_stat_line",
        "where": {"all": [
            {"in": ["event.payload.game_type", ["REG", "POST"]]},
            {">=": ["event.payload.passing_yards", 300]},
        ]},
    },
    "throttle": {"key": ["rule_id", "event.entities.player.id"], "window": "1d"},
    "action": {
        "kind": "enqueue_task",
        "priority": 70,
        "ttl": "3d",
        "task": {
            "kind": "instantiate_template",
            "template": "goodperformance",
            "account": "ballmoments_main",
            "fills_hints": {"player": "{{event.entities.player.name}}"},
        },
    },
}


def clone(**overrides):
    data = json.loads(json.dumps(GOOD))
    for key, value in overrides.items():
        if value is _REMOVE:
            data.pop(key, None)
        else:
            data[key] = value
    return data


class _Remove:
    pass


_REMOVE = _Remove()


class LoaderTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "rules")
        os.makedirs(self.dir)

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        text = data if isinstance(data, str) else json.dumps(data)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        return path

    def parse(self, data, name="R900-example.json"):
        return parse_rule(data, source=name)

    def error(self, data, name="R900-example.json"):
        with self.assertRaises(RuleValidationError) as ctx:
            self.parse(data, name)
        return str(ctx.exception)


class ValidRuleTests(LoaderTestCase):
    def test_parses_every_field(self):
        rule = self.parse(GOOD)
        self.assertIsInstance(rule, Rule)
        self.assertEqual(rule.rule_id, "R900-example")
        self.assertTrue(rule.enabled)
        self.assertEqual(rule.event_type, "player_stat_line")
        self.assertEqual(rule.action_kind, "enqueue_task")
        self.assertEqual(rule.priority, 70)
        self.assertEqual(rule.ttl, "3d")
        self.assertEqual(rule.ttl_seconds, 259200)
        self.assertEqual(rule.throttle.window, "1d")
        self.assertEqual(rule.throttle.key, ("rule_id", "event.entities.player.id"))

    def test_predicate_is_compiled_and_usable(self):
        rule = self.parse(GOOD)
        event = {
            "type": "player_stat_line",
            "payload": {"game_type": "REG", "passing_yards": 312},
        }
        self.assertTrue(rule.matches(event))
        event["payload"]["passing_yards"] = 120
        self.assertFalse(rule.matches(event))

    def test_matches_requires_the_declared_type(self):
        rule = self.parse(GOOD)
        event = {"type": "game_final", "payload": {"game_type": "REG", "passing_yards": 312}}
        self.assertFalse(rule.matches(event))

    def test_where_is_optional(self):
        rule = self.parse(clone(on={"type": "game_final"}))
        self.assertTrue(rule.matches({"type": "game_final"}))
        self.assertFalse(rule.matches({"type": "game_started"}))

    def test_throttle_is_optional(self):
        rule = self.parse(clone(throttle=_REMOVE))
        self.assertIsNone(rule.throttle)

    def test_enabled_defaults_to_true(self):
        self.assertTrue(self.parse(clone(enabled=_REMOVE)).enabled)

    def test_disabled_rules_still_parse(self):
        self.assertFalse(self.parse(clone(enabled=False)).enabled)

    def test_forever_ttl_has_no_seconds(self):
        action = clone()["action"]
        action["ttl"] = "forever"
        self.assertIsNone(self.parse(clone(action=action)).ttl_seconds)

    def test_notes_field_is_allowed(self):
        rule = self.parse(clone(notes="gamehighlight is status:design; tasks are candidates."))
        self.assertIn("status:design", rule.notes)

    def test_notify_action_kind(self):
        rule = self.parse(clone(action={
            "kind": "notify",
            "priority": 40,
            "ttl": "1d",
            "task": {"kind": "notify", "target": "state/INBOX-OUT.md", "message": "hi"},
        }))
        self.assertEqual(rule.action_kind, "notify")


class ThrottleSpecTests(LoaderTestCase):
    def test_key_resolves_against_an_event(self):
        rule = self.parse(GOOD)
        event = {"entities": [{"kind": "player", "id": "00-0036322"}]}
        self.assertEqual(rule.throttle.resolve(event, rule.rule_id), "R900-example|00-0036322")

    def test_unresolvable_key_is_none(self):
        rule = self.parse(GOOD)
        self.assertIsNone(rule.throttle.resolve({"entities": []}, rule.rule_id))

    def test_non_string_key_parts_are_json_encoded_deterministically(self):
        rule = self.parse(clone(throttle={"key": ["event.payload.passing_yards"], "window": "1d"}))
        self.assertEqual(rule.throttle.resolve({"payload": {"passing_yards": 312}}, "R"), "312")

    def test_literal_key_parts_are_used_verbatim(self):
        rule = self.parse(clone(throttle={"key": ["ballmoments_main"], "window": "1d"}))
        self.assertEqual(rule.throttle.resolve({}, "R"), "ballmoments_main")


class MalformedRuleTests(LoaderTestCase):
    def test_top_level_must_be_an_object(self):
        self.assertIn("object", self.error([GOOD]))
        self.assertIn("object", self.error("not-a-rule"))

    def test_rule_id_is_required(self):
        self.assertIn("rule_id", self.error(clone(rule_id=_REMOVE)))

    def test_rule_id_must_be_a_safe_token(self):
        for bad in ("", "has space", "../escape", "a/b", "x" * 200, 7):
            self.assertTrue(self.error(clone(rule_id=bad)), repr(bad))

    def test_why_is_required(self):
        message = self.error(clone(why=_REMOVE))
        self.assertIn("why", message)

    def test_unknown_top_level_key_is_rejected(self):
        message = self.error(clone(wheer={"a": 1}))
        self.assertIn("wheer", message)
        self.assertIn("unknown", message.lower())

    def test_unknown_action_key_is_rejected(self):
        action = clone()["action"]
        action["priorty"] = 70
        self.assertIn("priorty", self.error(clone(action=action)))

    def test_unknown_on_key_is_rejected(self):
        self.assertIn("filter", self.error(clone(on={"type": "x", "filter": {}})))

    def test_on_type_is_required(self):
        self.assertIn("type", self.error(clone(on={"where": {"==": ["event.type", "x"]}})))

    def test_action_is_required(self):
        self.assertIn("action", self.error(clone(action=_REMOVE)))

    def test_action_kind_must_be_known(self):
        action = clone()["action"]
        action["kind"] = "exec_shell"
        message = self.error(clone(action=action))
        self.assertIn("exec_shell", message)
        self.assertIn("enqueue_task", message)

    def test_task_kind_must_be_known(self):
        action = clone()["action"]
        action["task"]["kind"] = "run_python"
        self.assertIn("run_python", self.error(clone(action=action)))

    def test_notify_action_requires_a_notify_task(self):
        action = clone()["action"]
        action["kind"] = "notify"
        message = self.error(clone(action=action))
        self.assertIn("notify", message)

    def test_enqueue_task_rejects_a_notify_task(self):
        action = clone()["action"]
        action["task"]["kind"] = "notify"
        self.assertTrue(self.error(clone(action=action)))

    def test_priority_must_be_in_range(self):
        for bad in (-1, 101, "70", 70.5, True):
            action = clone()["action"]
            action["priority"] = bad
            self.assertTrue(self.error(clone(action=action)), repr(bad))

    def test_ttl_must_be_a_duration(self):
        action = clone()["action"]
        action["ttl"] = "3 days"
        self.assertIn("3 days", self.error(clone(action=action)))

    def test_throttle_window_must_be_declared(self):
        message = self.error(clone(throttle={"key": ["rule_id"], "window": "2d"}))
        self.assertIn("2d", message)

    def test_throttle_key_must_be_a_non_empty_string_list(self):
        for bad in ([], "rule_id", [1], [""], ["x"] * 20, {"a": 1}):
            self.assertTrue(
                self.error(clone(throttle={"key": bad, "window": "1d"})), repr(bad)
            )

    def test_predicate_errors_carry_a_location(self):
        message = self.error(clone(on={
            "type": "x",
            "where": {"all": [{"nope": ["event.type", 1]}]},
        }))
        self.assertIn("on.where.all[0]", message)
        self.assertIn("nope", message)

    def test_template_errors_are_reported(self):
        action = clone()["action"]
        action["task"]["fills_hints"]["player"] = "{{os.environ}}"
        message = self.error(clone(action=action))
        self.assertIn("os.environ", message)

    def test_error_names_the_source_file(self):
        message = self.error(clone(why=_REMOVE), name="R900-example.json")
        self.assertIn("R900-example.json", message)


class InjectionShapedRuleTests(LoaderTestCase):
    def test_code_shaped_strings_are_inert_data(self):
        action = clone()["action"]
        action["task"]["template"] = "__import__('os').system('calc')"
        action["task"]["account"] = "'; DROP TABLE rules; --"
        rule = self.parse(clone(action=action))
        self.assertEqual(rule.task["template"], "__import__('os').system('calc')")

    def test_code_shaped_literals_in_predicates_are_inert(self):
        rule = self.parse(clone(on={
            "type": "player_stat_line",
            "where": {"==": ["event.payload.note", "__import__('os').system('calc')"]},
        }))
        self.assertFalse(rule.matches({"type": "player_stat_line", "payload": {"note": "x"}}))
        self.assertTrue(rule.matches({
            "type": "player_stat_line",
            "payload": {"note": "__import__('os').system('calc')"},
        }))

    def test_dunder_paths_do_not_reach_attributes(self):
        rule = self.parse(clone(on={
            "type": "player_stat_line",
            "where": {"!=": ["event.__class__.__base__", None]},
        }))
        self.assertFalse(rule.matches({"type": "player_stat_line"}))


class DirectoryLoadTests(LoaderTestCase):
    def test_loads_every_json_file(self):
        self.write("R900-example.json", GOOD)
        self.write("R901-other.json", clone(rule_id="R901-other"))
        result = load_rules(self.dir)
        self.assertEqual([r.rule_id for r in result.rules], ["R900-example", "R901-other"])
        self.assertEqual(result.errors, [])

    def test_rules_are_sorted_by_rule_id(self):
        self.write("R902-c.json", clone(rule_id="R902-c"))
        self.write("R900-a.json", clone(rule_id="R900-a"))
        self.write("R901-b.json", clone(rule_id="R901-b"))
        self.assertEqual(
            [r.rule_id for r in load_rules(self.dir).rules], ["R900-a", "R901-b", "R902-c"]
        )

    def test_one_bad_rule_never_stops_the_others(self):
        self.write("R900-example.json", GOOD)
        self.write("R901-broken.json", {"rule_id": "R901-broken"})
        self.write("R902-junk.json", "{not json")
        result = load_rules(self.dir)
        self.assertEqual([r.rule_id for r in result.rules], ["R900-example"])
        self.assertEqual(len(result.errors), 2)
        self.assertTrue(all(isinstance(e, RuleValidationError) for e in result.errors))

    def test_rule_id_must_match_the_filename(self):
        self.write("R900-example.json", clone(rule_id="R999-different"))
        result = load_rules(self.dir)
        self.assertEqual(result.rules, [])
        self.assertIn("filename", str(result.errors[0]).lower())

    def test_non_json_files_are_ignored(self):
        self.write("R900-example.json", GOOD)
        self.write("README.md", "not a rule")
        self.write("notes.txt", "also not a rule")
        result = load_rules(self.dir)
        self.assertEqual(len(result.rules), 1)
        self.assertEqual(result.errors, [])

    def test_subdirectories_are_ignored(self):
        os.makedirs(os.path.join(self.dir, "archive.json"))
        self.write("R900-example.json", GOOD)
        result = load_rules(self.dir)
        self.assertEqual(len(result.rules), 1)
        self.assertEqual(result.errors, [])

    def test_missing_directory_is_an_error_not_a_crash(self):
        result = load_rules(os.path.join(self.tmp.name, "nope"))
        self.assertEqual(result.rules, [])
        self.assertEqual(len(result.errors), 1)

    def test_empty_directory_loads_nothing_quietly(self):
        result = load_rules(self.dir)
        self.assertEqual(result.rules, [])
        self.assertEqual(result.errors, [])

    def test_enabled_rules_helper(self):
        self.write("R900-example.json", GOOD)
        self.write("R901-off.json", clone(rule_id="R901-off", enabled=False))
        result = load_rules(self.dir)
        self.assertEqual(len(result.rules), 2)
        self.assertEqual([r.rule_id for r in result.enabled_rules], ["R900-example"])

    def test_by_id_lookup(self):
        self.write("R900-example.json", GOOD)
        self.assertEqual(load_rules(self.dir).by_id["R900-example"].priority, 70)

    def test_utf8_bom_is_tolerated(self):
        path = os.path.join(self.dir, "R900-example.json")
        with open(path, "wb") as handle:
            handle.write(b"\xef\xbb\xbf" + json.dumps(GOOD).encode("utf-8"))
        self.assertEqual(len(load_rules(self.dir).rules), 1)


class HostileFileTests(LoaderTestCase):
    def test_oversized_rule_file_is_rejected(self):
        big = clone()
        big["notes"] = "x" * 200000
        self.write("R900-example.json", big)
        result = load_rules(self.dir)
        self.assertEqual(result.rules, [])
        self.assertEqual(len(result.errors), 1)

    def test_json_nesting_bomb_is_rejected(self):
        self.write("R900-example.json", "[" * 100000 + "]" * 100000)
        result = load_rules(self.dir)
        self.assertEqual(result.rules, [])
        self.assertEqual(len(result.errors), 1)

    def test_predicate_nesting_bomb_is_rejected(self):
        node = {"==": ["event.type", "x"]}
        for _ in range(500):
            node = {"not": node}
        self.write("R900-example.json", clone(on={"type": "x", "where": node}))
        result = load_rules(self.dir)
        self.assertEqual(result.rules, [])
        self.assertIn("depth", str(result.errors[0]).lower())

    def test_invalid_utf8_is_an_error_not_a_crash(self):
        path = os.path.join(self.dir, "R900-example.json")
        with open(path, "wb") as handle:
            handle.write(b'{"rule_id": "R900-example", "why": "\xff\xfe bad bytes"}')
        result = load_rules(self.dir)
        self.assertEqual(result.rules, [])
        self.assertEqual(len(result.errors), 1)


if __name__ == "__main__":
    unittest.main()
