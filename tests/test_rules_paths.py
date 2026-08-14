"""Tests for the signal-hub predicate DSL path resolver (design section 4).

Paths are data, never code: dotted lookup over dicts/lists only, with a MISSING
sentinel for anything absent.  Nothing here may ever reach an attribute.
"""

import unittest

from signal_hub.rules_engine import MISSING, PathError, is_path, resolve_path, validate_path


EVENT = {
    "event_id": "a3f91c2e8b7d4506",
    "type": "player_stat_line",
    "ts": "2026-09-14T23:41:00Z",
    "source": "nflverse_player_stats",
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
        "nested": {"deep": {"value": 7}},
    },
}


class IsPathTests(unittest.TestCase):
    def test_only_event_prefixed_strings_are_paths(self):
        self.assertTrue(is_path("event.payload.passing_yards"))
        self.assertFalse(is_path("event"))
        self.assertFalse(is_path("eventful.thing"))
        self.assertFalse(is_path("CHI"))
        self.assertFalse(is_path(312))
        self.assertFalse(is_path(None))
        self.assertFalse(is_path(["event.x"]))


class ResolveTests(unittest.TestCase):
    def test_top_level_scalar(self):
        self.assertEqual(resolve_path(EVENT, "event.type"), "player_stat_line")
        self.assertEqual(resolve_path(EVENT, "event.confidence"), 1.0)

    def test_nested_payload(self):
        self.assertEqual(resolve_path(EVENT, "event.payload.passing_yards"), 312)
        self.assertEqual(resolve_path(EVENT, "event.payload.nested.deep.value"), 7)

    def test_entity_kind_sugar_picks_first_of_kind(self):
        self.assertEqual(resolve_path(EVENT, "event.entities.player.id"), "00-0036322")
        self.assertEqual(resolve_path(EVENT, "event.entities.player.name"), "Justin Fields")
        self.assertEqual(resolve_path(EVENT, "event.entities.team.id"), "CHI")
        self.assertEqual(resolve_path(EVENT, "event.entities.game.id"), "2026_02_CHI_DET")

    def test_entity_kind_sugar_returns_the_entity_itself(self):
        self.assertEqual(resolve_path(EVENT, "event.entities.team"), {"kind": "team", "id": "CHI"})

    def test_first_of_kind_when_duplicates(self):
        event = {
            "entities": [
                {"kind": "player", "id": "first"},
                {"kind": "player", "id": "second"},
            ]
        }
        self.assertEqual(resolve_path(event, "event.entities.player.id"), "first")

    def test_numeric_list_index(self):
        self.assertEqual(resolve_path(EVENT, "event.entities.0.id"), "00-0036322")
        self.assertEqual(resolve_path(EVENT, "event.entities.2.kind"), "game")

    def test_out_of_range_index_is_missing(self):
        self.assertIs(resolve_path(EVENT, "event.entities.99.id"), MISSING)

    def test_negative_index_is_not_supported(self):
        # "-1" is not all-digits, so it degrades to a kind lookup and misses.
        self.assertIs(resolve_path(EVENT, "event.entities.-1.id"), MISSING)

    def test_bare_event_is_not_a_path(self):
        # A rule addresses a field, never the whole event.
        self.assertIs(resolve_path(EVENT, "event"), MISSING)

    def test_missing_keys_resolve_to_missing(self):
        self.assertIs(resolve_path(EVENT, "event.payload.receiving_yards"), MISSING)
        self.assertIs(resolve_path(EVENT, "event.nope"), MISSING)
        self.assertIs(resolve_path(EVENT, "event.entities.coach.id"), MISSING)

    def test_walking_past_a_scalar_is_missing_not_an_error(self):
        self.assertIs(resolve_path(EVENT, "event.type.length"), MISSING)
        self.assertIs(resolve_path(EVENT, "event.confidence.0"), MISSING)

    def test_strings_are_never_indexed(self):
        self.assertIs(resolve_path(EVENT, "event.type.0"), MISSING)

    def test_missing_is_falsey_and_reprs_clearly(self):
        self.assertFalse(bool(MISSING))
        self.assertEqual(repr(MISSING), "MISSING")


class InjectionShapedPathTests(unittest.TestCase):
    """Dunder segments must be plain mapping keys, never attribute access."""

    def test_dunder_segments_do_not_reach_attributes(self):
        for hostile in (
            "event.__class__",
            "event.__class__.__mro__",
            "event.payload.__class__.__base__",
            "event.__init__.__globals__.os",
            "event.entities.__len__",
            "event.payload.__dict__",
        ):
            self.assertIs(resolve_path(EVENT, hostile), MISSING, hostile)

    def test_dunder_segment_that_is_a_real_key_returns_the_data(self):
        event = {"payload": {"__class__": "just-a-string"}}
        self.assertEqual(resolve_path(event, "event.payload.__class__"), "just-a-string")

    def test_code_shaped_segments_are_inert(self):
        for hostile in (
            "event.payload.__import__('os').system('calc')",
            "event.payload.;DROP TABLE rules",
            "event.payload.{{event.type}}",
        ):
            self.assertIs(resolve_path(EVENT, hostile), MISSING, hostile)

    def test_resolving_against_a_non_dict_event_never_raises(self):
        for junk in (None, [], "string", 3, object()):
            self.assertIs(resolve_path(junk, "event.payload.x"), MISSING)


class ValidatePathTests(unittest.TestCase):
    def test_accepts_well_formed_paths(self):
        validate_path("event.payload.passing_yards")
        validate_path("event.type")

    def test_rejects_the_bare_root(self):
        with self.assertRaises(PathError) as ctx:
            validate_path("event")
        self.assertIn("below 'event'", str(ctx.exception))

    def test_rejects_wrong_prefix(self):
        with self.assertRaises(PathError) as ctx:
            validate_path("rules.payload.x")
        self.assertIn("must start with 'event'", str(ctx.exception))

    def test_rejects_empty_segments(self):
        with self.assertRaises(PathError):
            validate_path("event..payload")
        with self.assertRaises(PathError):
            validate_path("event.payload.")

    def test_rejects_over_long_paths(self):
        with self.assertRaises(PathError) as ctx:
            validate_path("event." + ".".join(["a"] * 64))
        self.assertIn("segment", str(ctx.exception).lower())

    def test_rejects_huge_path_strings(self):
        with self.assertRaises(PathError):
            validate_path("event." + "a" * 5000)

    def test_rejects_non_strings(self):
        with self.assertRaises(PathError):
            validate_path(42)


if __name__ == "__main__":
    unittest.main()
