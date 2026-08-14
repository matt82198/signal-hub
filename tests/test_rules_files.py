"""The three MVP rule files are data, so they get tested like data.

Design section 4, "The 3 MVP rules":

  R001-bears-game-final-win  game_final, CHI won, REG/POST -> gamehighlight task
  R002-big-stat-line         300 pass / 100 rush / 100 rec / 3 TD -> goodperformance task
  R003-demand-spike          demand delta >= 0.15 and score >= 0.6 -> steer notify

Every rule filters out preseason (design section 8, risk 2): PRE stat lines are
backups and PRE rosters churn, so the pipeline may run end-to-end in August
without firing a single content task.
"""

import json
import os
import unittest

from signal_hub.rules_engine import load_rules

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES_DIR = os.path.join(REPO_ROOT, "rules")

EXPECTED_IDS = [
    "R001-bears-game-final-win",
    "R002-big-stat-line",
    "R003-demand-spike",
]


def game_final(winner="CHI", game_type="REG", game_id="2026_02_CHI_DET"):
    return {
        "event_id": "e-" + game_id,
        "type": "game_final",
        "entities": [{"kind": "game", "id": game_id}, {"kind": "team", "id": "CHI"}],
        "payload": {
            "winner": winner,
            "game_type": game_type,
            "opponent": "DET",
            "final_score": "27-17",
        },
    }


def stat_line(game_type="REG", **stats):
    payload = {"game_type": game_type}
    payload.update(stats)
    return {
        "event_id": "e-stat",
        "type": "player_stat_line",
        "entities": [
            {"kind": "player", "id": "00-0036322", "name": "Justin Fields"},
            {"kind": "game", "id": "2026_02_CHI_DET"},
        ],
        "payload": payload,
    }


def demand(delta=0.2, score=0.7, **extra):
    payload = {"delta": delta, "score": score}
    payload.update(extra)
    return {
        "event_id": "e-demand",
        "type": "demand_rank_delta",
        "entities": [{"kind": "opportunity", "id": "bears-highlights"}],
        "payload": payload,
    }


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.result = load_rules(RULES_DIR)

    def test_the_shipped_rules_all_load(self):
        self.assertEqual(self.result.errors, [])
        self.assertEqual([rule.rule_id for rule in self.result.rules], EXPECTED_IDS)

    def test_all_three_are_enabled(self):
        self.assertEqual(len(self.result.enabled_rules), 3)

    def test_every_rule_explains_itself(self):
        # Design section 8, risk 8: rule sprawl is the failure mode, so a rule
        # without a one-line reason for existing does not ship.
        for rule in self.result.rules:
            self.assertTrue(rule.why.strip(), rule.rule_id)

    def test_every_rule_declares_a_throttle(self):
        for rule in self.result.rules:
            self.assertIsNotNone(rule.throttle, rule.rule_id)

    def test_files_are_named_for_their_rule_id(self):
        on_disk = sorted(n for n in os.listdir(RULES_DIR) if n.endswith(".json"))
        self.assertEqual(on_disk, ["%s.json" % rule_id for rule_id in EXPECTED_IDS])

    def test_files_are_ascii_and_lf(self):
        for name in os.listdir(RULES_DIR):
            path = os.path.join(RULES_DIR, name)
            with open(path, "rb") as handle:
                raw = handle.read()
            self.assertTrue(raw.isascii(), name)
            self.assertNotIn(b"\r", raw, name)

    def test_files_are_pretty_printed_json(self):
        for name in os.listdir(RULES_DIR):
            with open(os.path.join(RULES_DIR, name), "r", encoding="utf-8") as handle:
                text = handle.read()
            json.loads(text)
            self.assertIn("\n", text, name)


class RuleTestCase(unittest.TestCase):
    rule_id = None

    def setUp(self):
        self.rule = load_rules(RULES_DIR).by_id[self.rule_id]


class R001Tests(RuleTestCase):
    rule_id = "R001-bears-game-final-win"

    def test_fires_on_a_bears_regular_season_win(self):
        self.assertTrue(self.rule.matches(game_final()))

    def test_fires_in_the_postseason(self):
        self.assertTrue(self.rule.matches(game_final(game_type="POST")))

    def test_silent_when_the_bears_lose(self):
        self.assertFalse(self.rule.matches(game_final(winner="DET")))

    def test_silent_in_the_preseason(self):
        self.assertFalse(self.rule.matches(game_final(game_type="PRE")))

    def test_silent_when_the_phase_is_unknown(self):
        event = game_final()
        del event["payload"]["game_type"]
        self.assertFalse(self.rule.matches(event))

    def test_silent_when_the_winner_is_unknown(self):
        event = game_final()
        del event["payload"]["winner"]
        self.assertFalse(self.rule.matches(event))

    def test_ignores_other_event_types(self):
        event = game_final()
        event["type"] = "game_started"
        self.assertFalse(self.rule.matches(event))

    def test_throttled_per_game_forever(self):
        self.assertEqual(self.rule.throttle.window, "forever")
        self.assertEqual(
            self.rule.throttle.resolve(game_final(), self.rule.rule_id),
            "R001-bears-game-final-win|2026_02_CHI_DET",
        )

    def test_enqueues_a_gamehighlight_template_task(self):
        self.assertEqual(self.rule.action_kind, "enqueue_task")
        self.assertEqual(self.rule.task["kind"], "instantiate_template")
        self.assertEqual(self.rule.task["template"], "gamehighlight")
        self.assertEqual(self.rule.task["fallback_template"], "playernarrative")
        self.assertIs(self.rule.task["needs_footage"], True)
        self.assertEqual(self.rule.ttl, "2d")


class R002Tests(RuleTestCase):
    rule_id = "R002-big-stat-line"

    def test_fires_past_300_passing_yards(self):
        self.assertTrue(self.rule.matches(stat_line(passing_yards=312)))

    def test_fires_past_100_rushing_yards(self):
        self.assertTrue(self.rule.matches(stat_line(rushing_yards=105)))

    def test_fires_past_100_receiving_yards(self):
        self.assertTrue(self.rule.matches(stat_line(receiving_yards=141)))

    def test_fires_at_three_touchdowns(self):
        self.assertTrue(self.rule.matches(stat_line(total_tds=3)))

    def test_thresholds_are_inclusive(self):
        self.assertTrue(self.rule.matches(stat_line(passing_yards=300)))
        self.assertTrue(self.rule.matches(stat_line(rushing_yards=100)))

    def test_silent_below_every_threshold(self):
        self.assertFalse(
            self.rule.matches(
                stat_line(
                    passing_yards=299, rushing_yards=99, receiving_yards=99, total_tds=2
                )
            )
        )

    def test_silent_in_the_preseason_however_big_the_line(self):
        self.assertFalse(self.rule.matches(stat_line(game_type="PRE", passing_yards=400)))

    def test_silent_when_the_phase_is_unknown(self):
        event = stat_line(passing_yards=400)
        del event["payload"]["game_type"]
        self.assertFalse(self.rule.matches(event))

    def test_silent_on_an_empty_stat_line(self):
        self.assertFalse(self.rule.matches(stat_line()))

    def test_throttled_per_player_per_day(self):
        self.assertEqual(self.rule.throttle.window, "1d")
        self.assertEqual(
            self.rule.throttle.resolve(stat_line(passing_yards=312), self.rule.rule_id),
            "R002-big-stat-line|00-0036322",
        )

    def test_enqueues_a_goodperformance_template_task(self):
        self.assertEqual(self.rule.action_kind, "enqueue_task")
        self.assertEqual(self.rule.task["template"], "goodperformance")
        self.assertEqual(self.rule.priority, 70)
        self.assertEqual(self.rule.ttl, "3d")


class R003Tests(RuleTestCase):
    rule_id = "R003-demand-spike"

    def test_fires_on_a_real_spike(self):
        self.assertTrue(self.rule.matches(demand(delta=0.2, score=0.7)))

    def test_thresholds_are_inclusive(self):
        self.assertTrue(self.rule.matches(demand(delta=0.15, score=0.6)))

    def test_silent_below_the_delta_threshold(self):
        self.assertFalse(self.rule.matches(demand(delta=0.14, score=0.9)))

    def test_silent_below_the_score_threshold(self):
        self.assertFalse(self.rule.matches(demand(delta=0.9, score=0.59)))

    def test_silent_without_a_delta(self):
        event = demand()
        del event["payload"]["delta"]
        self.assertFalse(self.rule.matches(event))

    def test_fires_when_no_phase_is_carried(self):
        # Demand events come from trend-indicator and carry no game phase; the
        # filter excludes PRE rather than requiring REG/POST, so a phaseless
        # event is not silently suppressed.
        self.assertNotIn("game_type", demand()["payload"])
        self.assertTrue(self.rule.matches(demand()))

    def test_silent_on_a_preseason_flavoured_spike(self):
        self.assertFalse(self.rule.matches(demand(game_type="PRE")))

    def test_notifies_rather_than_enqueueing_content(self):
        self.assertEqual(self.rule.action_kind, "notify")
        self.assertEqual(self.rule.task["kind"], "notify")
        self.assertEqual(self.rule.task["target"], "state/INBOX-OUT.md")

    def test_throttled_per_opportunity_per_day(self):
        self.assertEqual(self.rule.throttle.window, "1d")
        self.assertEqual(
            self.rule.throttle.resolve(demand(), self.rule.rule_id),
            "R003-demand-spike|bears-highlights",
        )


class CrossRuleTests(unittest.TestCase):
    def setUp(self):
        self.rules = load_rules(RULES_DIR).rules

    def test_no_rule_fires_on_a_preseason_event(self):
        preseason = [
            game_final(game_type="PRE"),
            stat_line(game_type="PRE", passing_yards=400, rushing_yards=200, total_tds=5),
            demand(delta=0.9, score=0.9, game_type="PRE"),
        ]
        for event in preseason:
            for rule in self.rules:
                self.assertFalse(rule.matches(event), "%s / %s" % (rule.rule_id, event["type"]))

    def test_each_rule_owns_a_distinct_event_type(self):
        types = [rule.event_type for rule in self.rules]
        self.assertEqual(sorted(types), ["demand_rank_delta", "game_final", "player_stat_line"])

    def test_no_rule_matches_a_junk_event(self):
        for junk in ({}, {"type": None}, {"type": "player_stat_line"}, None, [], "x"):
            for rule in self.rules:
                self.assertFalse(rule.matches(junk), rule.rule_id)


if __name__ == "__main__":
    unittest.main()
