"""Tests for {{event.path}} action templating (design section 4).

Substitution only.  No arithmetic, no function calls, no code, and never a
second pass over substituted output.
"""

import unittest

from signal_hub.rules_engine import TemplateError, collect_placeholders, render_template


EVENT = {
    "type": "player_stat_line",
    "entities": [
        {"kind": "player", "id": "00-0036322", "name": "Justin Fields"},
        {"kind": "game", "id": "2026_02_CHI_DET"},
    ],
    "payload": {
        "passing_yards": 312,
        "passing_tds": 3,
        "flag": True,
        "ratio": 0.5,
        "nothing": None,
        "tags": ["a", "b"],
    },
}


class RenderTests(unittest.TestCase):
    def test_plain_strings_pass_through(self):
        out, missing = render_template("goodperformance", EVENT)
        self.assertEqual(out, "goodperformance")
        self.assertEqual(missing, [])

    def test_single_placeholder_string(self):
        out, missing = render_template("{{event.entities.player.name}}", EVENT)
        self.assertEqual(out, "Justin Fields")
        self.assertEqual(missing, [])

    def test_embedded_placeholders(self):
        out, _ = render_template(
            "{{event.payload.passing_yards}} yds, {{event.payload.passing_tds}} TD", EVENT
        )
        self.assertEqual(out, "312 yds, 3 TD")

    def test_whitespace_inside_braces_is_tolerated(self):
        out, _ = render_template("{{ event.entities.game.id }}", EVENT)
        self.assertEqual(out, "2026_02_CHI_DET")

    def test_lone_placeholder_preserves_the_native_type(self):
        self.assertEqual(render_template("{{event.payload.passing_yards}}", EVENT)[0], 312)
        self.assertEqual(render_template("{{event.payload.flag}}", EVENT)[0], True)
        self.assertEqual(render_template("{{event.payload.ratio}}", EVENT)[0], 0.5)
        self.assertEqual(render_template("{{event.payload.tags}}", EVENT)[0], ["a", "b"])
        self.assertIsNone(render_template("{{event.payload.nothing}}", EVENT)[0])

    def test_embedded_non_strings_render_as_json_scalars(self):
        out, _ = render_template("f={{event.payload.flag}} n={{event.payload.nothing}}", EVENT)
        self.assertEqual(out, "f=true n=null")

    def test_nested_structures_are_rendered(self):
        out, missing = render_template(
            {
                "template": "goodperformance",
                "fills_hints": {
                    "player": "{{event.entities.player.name}}",
                    "game": "{{event.entities.game.id}}",
                },
                "tags": ["{{event.type}}", "static"],
                "needs_footage": True,
                "priority": 70,
            },
            EVENT,
        )
        self.assertEqual(out["fills_hints"]["player"], "Justin Fields")
        self.assertEqual(out["fills_hints"]["game"], "2026_02_CHI_DET")
        self.assertEqual(out["tags"], ["player_stat_line", "static"])
        self.assertIs(out["needs_footage"], True)
        self.assertEqual(out["priority"], 70)
        self.assertEqual(missing, [])

    def test_render_does_not_mutate_the_source(self):
        source = {"a": "{{event.type}}"}
        render_template(source, EVENT)
        self.assertEqual(source, {"a": "{{event.type}}"})

    def test_dict_keys_are_never_templated(self):
        out, _ = render_template({"{{event.type}}": "x"}, EVENT)
        self.assertEqual(list(out.keys()), ["{{event.type}}"])


class MissingPathTests(unittest.TestCase):
    def test_missing_renders_empty_and_is_reported(self):
        out, missing = render_template("yards: {{event.payload.receiving_yards}}", EVENT)
        self.assertEqual(out, "yards: ")
        self.assertEqual(missing, ["event.payload.receiving_yards"])

    def test_lone_missing_placeholder_renders_none(self):
        out, missing = render_template("{{event.payload.receiving_yards}}", EVENT)
        self.assertIsNone(out)
        self.assertEqual(missing, ["event.payload.receiving_yards"])

    def test_missing_paths_are_deduped_and_ordered(self):
        out, missing = render_template(
            {"a": "{{event.nope}}", "b": "{{event.nope}} {{event.also}}"}, EVENT
        )
        self.assertEqual(missing, ["event.nope", "event.also"])


class InjectionTests(unittest.TestCase):
    """Substituted values are data.  They are never re-scanned or executed."""

    def test_substituted_output_is_not_rescanned(self):
        event = {"payload": {"name": "{{event.payload.secret}}", "secret": "leaked"}}
        out, _ = render_template("{{event.payload.name}}", event)
        self.assertEqual(out, "{{event.payload.secret}}")
        self.assertNotIn("leaked", str(out))

    def test_code_shaped_values_stay_strings(self):
        event = {"payload": {"name": "__import__('os').system('calc')"}}
        out, _ = render_template("hi {{event.payload.name}}", event)
        self.assertEqual(out, "hi __import__('os').system('calc')")

    def test_code_shaped_template_literals_stay_literal(self):
        out, _ = render_template("${os.system('calc')}", EVENT)
        self.assertEqual(out, "${os.system('calc')}")

    def test_percent_and_format_syntax_is_inert(self):
        out, _ = render_template("100% {name} {0} %s", EVENT)
        self.assertEqual(out, "100% {name} {0} %s")

    def test_single_braces_are_left_alone(self):
        out, _ = render_template("{event.type}", EVENT)
        self.assertEqual(out, "{event.type}")


class PlaceholderValidationTests(unittest.TestCase):
    def test_collect_placeholders_walks_the_whole_structure(self):
        found = collect_placeholders(
            {"a": "{{event.type}}", "b": ["{{event.payload.x}}", 1], "c": 2}
        )
        self.assertEqual(found, ["event.type", "event.payload.x"])

    def test_non_path_placeholder_is_rejected(self):
        with self.assertRaises(TemplateError) as ctx:
            render_template("{{os.system}}", EVENT)
        self.assertIn("event.", str(ctx.exception))

    def test_malformed_path_placeholder_is_rejected(self):
        with self.assertRaises(TemplateError):
            render_template("{{event..payload}}", EVENT)

    def test_collect_placeholders_rejects_bad_paths_too(self):
        with self.assertRaises(TemplateError):
            collect_placeholders({"a": "{{nope}}"})


class ResourceBoundTests(unittest.TestCase):
    def test_over_long_template_strings_are_rejected(self):
        with self.assertRaises(TemplateError):
            render_template("x" * 100000, EVENT)

    def test_too_many_placeholders_are_rejected(self):
        with self.assertRaises(TemplateError):
            render_template("{{event.type}}" * 200, EVENT)

    def test_deeply_nested_action_objects_are_rejected(self):
        node = "{{event.type}}"
        for _ in range(50):
            node = {"a": node}
        with self.assertRaises(TemplateError) as ctx:
            render_template(node, EVENT)
        self.assertIn("depth", str(ctx.exception).lower())

    def test_huge_action_objects_are_rejected(self):
        with self.assertRaises(TemplateError):
            render_template({str(i): i for i in range(5000)}, EVENT)


if __name__ == "__main__":
    unittest.main()
