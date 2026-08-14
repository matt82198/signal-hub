"""Tests for the rule evaluation engine (design section 4).

Events in, fired actions out.  The engine matches, gates, renders and *returns*
actions as data - it never writes a task file.  Dispatch is the queue lane's
job; keeping the boundary here is what makes the whole trigger layer testable
with dict fixtures and a frozen clock.
"""

import datetime
import json
import os
import tempfile
import unittest

from signal_hub.rules_engine import RuleEngine, ThrottleState, parse_rule


def utc(*args):
    return datetime.datetime(*args, tzinfo=datetime.timezone.utc)


class FrozenClock:
    def __init__(self, moment):
        self.moment = moment

    def __call__(self):
        return self.moment

    def advance(self, **kwargs):
        self.moment = self.moment + datetime.timedelta(**kwargs)
        return self.moment


STAT_RULE = {
    "rule_id": "R002-big-stat-line",
    "why": "Standout lines are the per-player content trigger.",
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
            "fills_hints": {
                "player": "{{event.entities.player.name}}",
                "headline": "{{event.payload.passing_yards}} yds",
            },
        },
    },
}

GAME_RULE = {
    "rule_id": "R001-bears-game-final-win",
    "why": "A Bears win is the primary highlight trigger.",
    "on": {"type": "game_final", "where": {"==": ["event.payload.winner", "CHI"]}},
    "throttle": {"key": ["rule_id", "event.entities.game.id"], "window": "forever"},
    "action": {
        "kind": "enqueue_task",
        "priority": 90,
        "ttl": "2d",
        "task": {"kind": "instantiate_template", "template": "gamehighlight"},
    },
}


def stat_event(event_id="evt-1", player="00-0036322", yards=312, game_type="REG"):
    return {
        "event_id": event_id,
        "type": "player_stat_line",
        "ts": "2026-09-14T23:41:00Z",
        "entities": [
            {"kind": "player", "id": player, "name": "Justin Fields"},
            {"kind": "game", "id": "2026_02_CHI_DET"},
        ],
        "payload": {"passing_yards": yards, "game_type": game_type},
    }


def game_event(event_id="evt-g", winner="CHI"):
    return {
        "event_id": event_id,
        "type": "game_final",
        "entities": [{"kind": "game", "id": "2026_02_CHI_DET"}],
        "payload": {"winner": winner},
    }


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = os.path.join(self.tmp.name, "state", ".rules-fired.jsonl")
        self.clock = FrozenClock(utc(2026, 9, 14, 23, 41, 0))

    def engine(self, *rule_dicts):
        rules = [parse_rule(data) for data in (rule_dicts or (STAT_RULE,))]
        state = ThrottleState.load(self.state_path, now=self.clock)
        return RuleEngine(rules, state, now=self.clock)


class FiringTests(EngineTestCase):
    def test_a_matching_event_fires_one_action(self):
        result = self.engine().evaluate([stat_event()])
        self.assertEqual(len(result.actions), 1)
        action = result.actions[0]
        self.assertEqual(action["rule_id"], "R002-big-stat-line")
        self.assertEqual(action["event_id"], "evt-1")
        self.assertEqual(action["event_type"], "player_stat_line")
        self.assertEqual(action["kind"], "enqueue_task")
        self.assertEqual(action["priority"], 70)

    def test_a_non_matching_event_fires_nothing(self):
        result = self.engine().evaluate([stat_event(yards=120)])
        self.assertEqual(result.actions, [])

    def test_a_preseason_event_fires_nothing(self):
        result = self.engine().evaluate([stat_event(game_type="PRE")])
        self.assertEqual(result.actions, [])

    def test_an_unrelated_event_type_fires_nothing(self):
        self.assertEqual(self.engine().evaluate([game_event()]).actions, [])

    def test_the_task_body_is_rendered(self):
        action = self.engine().evaluate([stat_event()]).actions[0]
        self.assertEqual(action["task"]["template"], "goodperformance")
        self.assertEqual(action["task"]["fills_hints"]["player"], "Justin Fields")
        self.assertEqual(action["task"]["fills_hints"]["headline"], "312 yds")
        self.assertEqual(action["missing_fills"], [])

    def test_missing_fills_are_reported_not_hidden(self):
        event = stat_event()
        del event["entities"][0]["name"]
        action = self.engine().evaluate([event]).actions[0]
        self.assertIsNone(action["task"]["fills_hints"]["player"])
        self.assertEqual(action["missing_fills"], ["event.entities.player.name"])

    def test_the_rule_template_is_not_mutated_by_rendering(self):
        engine = self.engine()
        engine.evaluate([stat_event()])
        self.assertEqual(
            engine.rules[0].task["fills_hints"]["player"], "{{event.entities.player.name}}"
        )

    def test_expiry_is_computed_from_the_injected_clock(self):
        action = self.engine().evaluate([stat_event()]).actions[0]
        self.assertEqual(action["fired_at"], "2026-09-14T23:41:00Z")
        self.assertEqual(action["expires_at"], "2026-09-17T23:41:00Z")
        self.assertEqual(action["ttl"], "3d")

    def test_a_rule_without_a_ttl_has_no_expiry(self):
        rule = json.loads(json.dumps(STAT_RULE))
        del rule["action"]["ttl"]
        action = self.engine(rule).evaluate([stat_event()]).actions[0]
        self.assertIsNone(action["expires_at"])

    def test_actions_are_plain_json_data(self):
        actions = self.engine().evaluate([stat_event()]).actions
        self.assertEqual(json.loads(json.dumps(actions)), actions)

    def test_the_engine_dispatches_nothing(self):
        # ACTIONS ARE RETURNED, NOT EXECUTED.  Nothing outside the throttle log
        # may appear on disk - no queue/, no task file, no side channel.
        self.engine().evaluate([stat_event()])
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["state"])
        self.assertEqual(os.listdir(os.path.dirname(self.state_path)), [".rules-fired.jsonl"])


class MultipleRuleTests(EngineTestCase):
    def test_each_rule_sees_every_event(self):
        result = self.engine(STAT_RULE, GAME_RULE).evaluate([stat_event(), game_event()])
        self.assertEqual(
            [action["rule_id"] for action in result.actions],
            ["R002-big-stat-line", "R001-bears-game-final-win"],
        )

    def test_two_rules_can_fire_on_one_event(self):
        second = json.loads(json.dumps(STAT_RULE))
        second["rule_id"] = "R900-shadow"
        second["throttle"]["key"] = ["rule_id", "event.entities.player.id"]
        result = self.engine(STAT_RULE, second).evaluate([stat_event()])
        self.assertEqual(len(result.actions), 2)

    def test_rules_are_evaluated_in_rule_id_order(self):
        second = json.loads(json.dumps(STAT_RULE))
        second["rule_id"] = "R000-first"
        result = self.engine(STAT_RULE, second).evaluate([stat_event()])
        self.assertEqual(
            [action["rule_id"] for action in result.actions], ["R000-first", "R002-big-stat-line"]
        )

    def test_disabled_rules_never_fire(self):
        disabled = json.loads(json.dumps(STAT_RULE))
        disabled["enabled"] = False
        result = self.engine(disabled).evaluate([stat_event()])
        self.assertEqual(result.actions, [])


class DuplicateGateTests(EngineTestCase):
    def test_the_same_event_twice_in_one_batch_fires_once(self):
        result = self.engine().evaluate([stat_event(), stat_event()])
        self.assertEqual(len(result.actions), 1)
        self.assertEqual(result.stats["duplicate"], 1)

    def test_the_same_event_in_a_later_tick_fires_nothing(self):
        self.engine().evaluate([stat_event()])
        result = self.engine().evaluate([stat_event()])
        self.assertEqual(result.actions, [])
        self.assertEqual(result.stats["duplicate"], 1)

    def test_a_revised_stat_line_does_not_fire_again(self):
        # Same identity hash, corrected yardage: nflverse revises stat lines and
        # the action side must stay exactly-once.
        self.engine().evaluate([stat_event(yards=312)])
        self.clock.advance(days=1)
        result = self.engine().evaluate([stat_event(yards=317)])
        self.assertEqual(result.actions, [])

    def test_the_gate_outlives_the_declared_window(self):
        self.engine().evaluate([stat_event()])
        self.clock.advance(days=400)
        self.assertEqual(self.engine().evaluate([stat_event()]).actions, [])


class ThrottleGateTests(EngineTestCase):
    def test_a_second_event_for_the_same_player_is_throttled(self):
        result = self.engine().evaluate([stat_event("evt-1"), stat_event("evt-2")])
        self.assertEqual(len(result.actions), 1)
        self.assertEqual(result.stats["throttled"], 1)

    def test_a_different_player_is_not_throttled(self):
        result = self.engine().evaluate(
            [stat_event("evt-1"), stat_event("evt-2", player="00-0000001")]
        )
        self.assertEqual(len(result.actions), 2)

    def test_the_throttle_lapses_at_the_window_boundary(self):
        self.engine().evaluate([stat_event("evt-1")])
        self.clock.advance(days=1)
        self.assertEqual(len(self.engine().evaluate([stat_event("evt-2")]).actions), 1)

    def test_a_forever_window_never_lapses(self):
        self.engine(GAME_RULE).evaluate([game_event("evt-g1")])
        self.clock.advance(days=400)
        result = self.engine(GAME_RULE).evaluate([game_event("evt-g2")])
        self.assertEqual(result.actions, [])

    def test_a_rule_without_a_throttle_only_has_the_duplicate_gate(self):
        rule = json.loads(json.dumps(STAT_RULE))
        del rule["throttle"]
        result = self.engine(rule).evaluate([stat_event("evt-1"), stat_event("evt-2")])
        self.assertEqual(len(result.actions), 2)

    def test_an_unresolvable_throttle_key_fails_closed(self):
        event = stat_event()
        event["entities"] = [{"kind": "game", "id": "2026_02_CHI_DET"}]
        result = self.engine().evaluate([event])
        self.assertEqual(result.actions, [])
        self.assertEqual(result.stats["unresolved_key"], 1)


class DryRunTests(EngineTestCase):
    def test_dry_run_returns_actions_without_recording(self):
        engine = self.engine()
        result = engine.evaluate([stat_event()], record=False)
        self.assertEqual(len(result.actions), 1)
        self.assertFalse(os.path.exists(self.state_path))

    def test_dry_run_does_not_arm_the_gates(self):
        engine = self.engine()
        engine.evaluate([stat_event()], record=False)
        self.assertEqual(len(engine.evaluate([stat_event()]).actions), 1)


class HostileEventTests(EngineTestCase):
    def test_junk_events_are_skipped_and_counted(self):
        result = self.engine().evaluate(
            [None, [], "x", 7, {}, {"type": "player_stat_line"}, {"event_id": "x"}]
        )
        self.assertEqual(result.actions, [])
        self.assertEqual(result.stats["invalid_events"], 7)

    def test_a_junk_event_never_stops_the_good_ones(self):
        result = self.engine().evaluate([None, stat_event(), "junk"])
        self.assertEqual(len(result.actions), 1)
        self.assertEqual(result.stats["invalid_events"], 2)

    def test_a_ragged_payload_never_raises(self):
        for payload in (None, [], "x", 7, {"passing_yards": "many"}):
            event = stat_event()
            event["payload"] = payload
            self.assertEqual(self.engine().evaluate([event]).actions, [])

    def test_ragged_entities_never_raise(self):
        for entities in (None, "x", 7, [None, 3], {}):
            event = stat_event()
            event["entities"] = entities
            self.engine().evaluate([event])

    def test_code_shaped_event_data_is_inert(self):
        event = stat_event()
        event["entities"][0]["name"] = "{{event.payload.passing_yards}}"
        action = self.engine().evaluate([event]).actions[0]
        self.assertEqual(
            action["task"]["fills_hints"]["player"], "{{event.payload.passing_yards}}"
        )

    def test_an_empty_batch_is_fine(self):
        result = self.engine().evaluate([])
        self.assertEqual(result.actions, [])
        self.assertEqual(result.stats["events"], 0)

    def test_no_rules_is_fine(self):
        state = ThrottleState.load(self.state_path, now=self.clock)
        engine = RuleEngine([], state, now=self.clock)
        self.assertEqual(engine.evaluate([stat_event()]).actions, [])


class StatsTests(EngineTestCase):
    def test_counters_add_up(self):
        result = self.engine().evaluate(
            [stat_event("evt-1"), stat_event("evt-2"), stat_event("evt-1"), stat_event(yards=10)]
        )
        stats = result.stats
        self.assertEqual(stats["events"], 4)
        self.assertEqual(stats["matched"], 3)
        self.assertEqual(stats["fired"], 1)
        self.assertEqual(stats["duplicate"], 1)
        self.assertEqual(stats["throttled"], 1)
        self.assertEqual(stats["suppressed"], 2)

    def test_per_rule_counters(self):
        result = self.engine(STAT_RULE, GAME_RULE).evaluate([stat_event(), game_event()])
        by_rule = result.stats["by_rule"]
        self.assertEqual(by_rule["R002-big-stat-line"]["fired"], 1)
        self.assertEqual(by_rule["R001-bears-game-final-win"]["fired"], 1)

    def test_every_loaded_rule_appears_in_the_counters(self):
        # Design section 8, risk 8: a rule that never fires must be visible.
        result = self.engine(STAT_RULE, GAME_RULE).evaluate([])
        self.assertEqual(sorted(result.stats["by_rule"]), [
            "R001-bears-game-final-win", "R002-big-stat-line"
        ])
        self.assertEqual(result.stats["by_rule"]["R001-bears-game-final-win"]["fired"], 0)

    def test_stats_are_plain_json_data(self):
        stats = self.engine().evaluate([stat_event()]).stats
        self.assertEqual(json.loads(json.dumps(stats)), stats)


class SingleEventApiTests(EngineTestCase):
    def test_evaluate_event_returns_actions(self):
        actions = self.engine().evaluate_event(stat_event())
        self.assertEqual(len(actions), 1)

    def test_evaluate_event_tolerates_junk(self):
        self.assertEqual(self.engine().evaluate_event(None), [])


class ClockInjectionTests(EngineTestCase):
    def test_clock_is_required(self):
        state = ThrottleState.load(self.state_path, now=self.clock)
        with self.assertRaises(TypeError):
            RuleEngine([], state)


if __name__ == "__main__":
    unittest.main()
