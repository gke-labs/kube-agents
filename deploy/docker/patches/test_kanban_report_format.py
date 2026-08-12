"""Unit tests for the report-format contract installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The four ``CARD_*`` constants below are the verbatim ``result`` fields of the
four sibling cards fanned out by ``t_4867e94b`` on 2026-08-08, read straight off
the board. They are the whole reason this module exists: three of them were given
near-identical bodies and produced three different formats, and a human reading
the thread called two of them wrong. Calibrating the detector against invented
examples would prove nothing — these are the actual dispute.
"""

import unittest

from kanban_report_format import (
    DEFECT_ADVICE,
    FORMAT_MARKER,
    SERIOUS_DEFECTS,
    REPORT_FORMAT_STANZA,
    SHAPE_MIN_CHARS,
    serious_defects,
    has_format_directive,
    result_shape_defects,
    with_report_format,
)

#: 312 chars. Read as fine: one ``###``, a lead sentence, values in backticks.
CARD_1_GOOD = """### Sleep Task 1 Completion

The requested sleep of 1 millisecond has been executed. Here are the recorded \
active execution details:

- **Start Unix Epoch:** `1786240527.916398`
- **End Unix Epoch:** `1786240527.9178874`
- **Elapsed Active Execution Time:** `0.001489400863647461` seconds (approximately 1.49 ms)"""

#: 240 chars. Read as wrong: an H1 *and* a ``###`` over four lines, raw floats.
CARD_2_H1 = """# Sleep 1ms - Task 2 Completion Report

The 1ms sleep task was successfully completed.

### Timing Details
- **Start Epoch:** 1786240530.6882887
- **End Epoch:** 1786240530.6903057
- **Measured Active Duration:** 0.002017 seconds (2.017 ms)"""

#: 189 chars. Read as wrong: a banner over a bare list, no prose, raw floats.
CARD_3_BARE_LIST = """### Sleep Task 3 Execution Details
- **Active Start (Unix Epoch):** 1786240531.1585038
- **Active End (Unix Epoch):** 1786240531.1598377
- **Active Duration:** 0.0013339519500732422 seconds"""

#: The older failure mode: structure expressed in a form Slack cannot render.
CARD_ASCII = """=== Cron Job Catalogue ===

We found the following jobs configured on the platform profile and they are
listed below with their schedules and current enablement state for review.

1. DRIFT DETECTION
   runs hourly, enabled

2. COST REPORT
   runs daily, disabled
"""


class RealCardCalibrationTest(unittest.TestCase):
    """The detector has to agree with the human who read the thread.

    If it flags the card that was fine, or clears either card that was not, the
    thresholds are wrong however defensible they look in isolation.
    """

    def test_the_card_that_read_as_fine_has_no_defects(self):
        self.assertEqual(result_shape_defects(CARD_1_GOOD), ())

    def test_the_h1_card_is_caught_and_is_serious(self):
        self.assertIn("top-level-heading", result_shape_defects(CARD_2_H1))
        self.assertEqual(serious_defects(CARD_2_H1), ("top-level-heading",))

    def test_the_bare_list_card_is_seen_but_logged_at_info(self):
        """Task 3 is the case that is only taste.

        A heading over three values is ugly, not wrong, and a card whose honest
        answer is three values should not have a WARNING raised over it. Option
        1 — the stanza in the card body — is what fixes this one; the detector's
        job is only to make it measurable.
        """
        defects = result_shape_defects(CARD_3_BARE_LIST)
        self.assertIn("heading-without-prose", defects)
        self.assertIn("unquoted-numerics", defects)
        self.assertEqual(serious_defects(CARD_3_BARE_LIST), ())

    def test_backticked_values_are_not_read_as_raw(self):
        """The one signal that cleanly separates card 1 from cards 2 and 3."""
        self.assertNotIn("unquoted-numerics", result_shape_defects(CARD_1_GOOD))
        self.assertIn("unquoted-numerics", result_shape_defects(CARD_2_H1))

    def test_both_bad_cards_are_above_the_floor(self):
        """The floor exists to silence one-liners, not to hide small reports."""
        for card in (CARD_2_H1, CARD_3_BARE_LIST):
            with self.subTest(card=card[:24]):
                self.assertGreaterEqual(len(card.strip()), SHAPE_MIN_CHARS)
                self.assertNotEqual(result_shape_defects(card), ())


class DefectTest(unittest.TestCase):
    def test_ascii_structure_with_no_markdown_is_serious(self):
        self.assertIn("ascii-substitute", result_shape_defects(CARD_ASCII))
        self.assertIn("ascii-substitute", serious_defects(CARD_ASCII))

    def test_real_markdown_suppresses_the_ascii_finding(self):
        """A report with genuine structure is not accused of faking it."""
        self.assertNotIn(
            "ascii-substitute", result_shape_defects("## Real\n\n" + CARD_ASCII)
        )

    def test_a_short_result_has_no_shape_to_get_wrong(self):
        self.assertEqual(result_shape_defects("# tiny"), ())

    def test_a_missing_result_is_not_a_defect(self):
        for empty in (None, "", "   "):
            with self.subTest(result=empty):
                self.assertEqual(result_shape_defects(empty), ())

    def test_long_prose_is_left_alone(self):
        prose = ("No drift was found on any cluster in the fleet this morning. " * 12).strip()
        self.assertEqual(result_shape_defects(prose), ())

    def test_numbers_inside_code_are_not_counted(self):
        """A code sample full of epochs is exactly what backticks are for."""
        fenced = (
            "## Timings\n\nHere is what the probe recorded during the run today.\n\n"
            "```\nstart=1786240530.6882887\nend=1786240530.6903057\n```\n"
        )
        self.assertNotIn("unquoted-numerics", result_shape_defects(fenced))

    def test_a_shell_comment_in_a_code_block_is_not_a_heading(self):
        """The `#` of a command sample must not read as an H1.

        ``top-level-heading`` is the serious tier, so a report whose only hash
        sits inside a fence would raise a WARNING naming an edit that cannot be
        made: dropping the comment does not change the report's shape.
        """
        fenced = (
            "## Findings\n\nThe node pool drained cleanly before the upgrade began, "
            "and every workload rescheduled without a restart.\n\n"
            "```\n# list the pods that moved\nkubectl get pods -A\n```\n"
        )
        self.assertNotIn("top-level-heading", result_shape_defects(fenced))
        self.assertEqual(serious_defects(fenced), ())

    def test_an_inline_backtick_does_not_manufacture_a_heading(self):
        """Only fences are stripped, and deliberately so.

        Stripping inline code as well would let a line that opens with a
        backticked flag collapse onto the hash after it, inventing the very
        defect this check exists to catch.
        """
        body = (
            "## Flags\n\nThese are the switches the upgrade script honours when it "
            "runs unattended over a weekend window.\n\n"
            "`--dry-run` # prints the plan and exits without touching a node\n"
        )
        self.assertGreaterEqual(len(body.strip()), SHAPE_MIN_CHARS)
        self.assertNotIn("top-level-heading", result_shape_defects(body))

    def test_small_written_numbers_are_not_raw_values(self):
        """"3 pods" and "99.9%" are prose, not unformatted machine output."""
        body = (
            "## Rollout\n\nThe deployment finished and settled without incident.\n\n"
            "- Replicas ready: 3 of 3\n- Availability held at 99.9% throughout\n"
            "- Restarts observed: 0\n"
        )
        self.assertNotIn("unquoted-numerics", result_shape_defects(body))

    def test_only_the_two_objective_defects_are_serious(self):
        self.assertEqual(
            SERIOUS_DEFECTS, ("top-level-heading", "ascii-substitute")
        )

    def test_every_defect_has_advice_a_model_can_act_on(self):
        for defect in ("top-level-heading", "ascii-substitute",
                       "heading-without-prose", "unquoted-numerics"):
            with self.subTest(defect=defect):
                self.assertIn(defect, DEFECT_ADVICE)
                self.assertTrue(DEFECT_ADVICE[defect].strip())

    def test_a_hostile_result_cannot_raise(self):
        class Exploding:
            def __str__(self):
                raise RuntimeError("boom")

        self.assertEqual(result_shape_defects(Exploding()), ())


class StanzaTest(unittest.TestCase):
    def test_a_body_without_format_guidance_gets_the_stanza(self):
        out = with_report_format("Sleep for 1ms and report the epochs.")
        self.assertIn(FORMAT_MARKER, out)
        self.assertIn("Sleep for 1ms", out)

    def test_appending_is_idempotent(self):
        once = with_report_format("Do the thing.")
        self.assertEqual(with_report_format(once), once)

    def test_an_explicit_directive_outranks_the_default(self):
        """Two briefs to reconcile is worse than none to follow."""
        body = "Do X.\n\nReport format: a single pipe table, no prose."
        self.assertEqual(with_report_format(body), body)

    def test_an_absent_body_becomes_the_stanza(self):
        """An empty brief is where the default shape matters most."""
        for empty in (None, "", "   "):
            with self.subTest(body=empty):
                self.assertEqual(with_report_format(empty), REPORT_FORMAT_STANZA)

    def test_the_stanza_states_the_rules_the_detector_measures(self):
        """The card, the schema and the warning must describe one contract.

        A stanza that asked for something the detector does not check — or
        stayed silent about something it raises a WARNING over — would train
        workers to produce reports the logs then complain about.
        """
        self.assertIn("Never `#`", REPORT_FORMAT_STANZA)
        self.assertIn("backticks", REPORT_FORMAT_STANZA)
        self.assertIn("=== Title ===", REPORT_FORMAT_STANZA)
        self.assertIn("pipe table", REPORT_FORMAT_STANZA)

    def test_the_stanza_itself_is_clean(self):
        """Self-consistency: the instructions must not violate their own rules."""
        self.assertEqual(serious_defects(REPORT_FORMAT_STANZA), ())

    def test_has_format_directive_spots_common_phrasings(self):
        for phrase in (
            "## Report format",
            "Output format: JSON",
            "Please format the result as a table",
            "response format should be terse",
        ):
            with self.subTest(phrase=phrase):
                self.assertTrue(has_format_directive("Do X.\n\n" + phrase))

    def test_has_format_directive_is_not_trigger_happy(self):
        for phrase in (
            "Reformat the YAML in place.",
            "Check the disk format on the node.",
            "Run terraform fmt.",
        ):
            with self.subTest(phrase=phrase):
                self.assertFalse(has_format_directive(phrase))


class AdviceTextTest(unittest.TestCase):
    """The advice is read by a human in a log line, not by a refused worker.

    Nothing acts on it automatically any more, so its only job is to name the
    edit precisely enough that whoever reads the WARNING can make it.
    """

    def test_the_advice_names_the_edit_rather_than_the_complaint(self):
        self.assertIn("`##`", DEFECT_ADVICE["top-level-heading"])
        self.assertIn("pipe table", DEFECT_ADVICE["ascii-substitute"])

    def test_no_advice_asks_for_a_retry(self):
        """The old text told the worker to call ``kanban_complete`` again.

        There is no retry to ask for: the card has already closed by the time
        the notifier logs this. Advice that says otherwise would send a reader
        looking for a redelivery that never happens.
        """
        for defect, advice in DEFECT_ADVICE.items():
            with self.subTest(defect=defect):
                self.assertNotIn("kanban_complete again", advice)

    def test_the_advice_for_an_h1_covers_the_unfenced_manifest_reading(self):
        """``# comment`` at column 0 in raw YAML is not a heading to demote.

        The detector cannot tell the two apart and flags either way — unfenced,
        a manifest renders as a wall of text — but "use ``##`` instead" is the
        wrong edit for a comment marker.
        """
        advice = DEFECT_ADVICE["top-level-heading"]
        self.assertIn("fence", advice)
        self.assertIn("Do not delete the `#`", advice)


class WellShapedFixtureTest(unittest.TestCase):
    """The suite's own exemplars have to match the shape they document."""

    def test_the_notifier_suites_structured_fixture_is_clean(self):
        """It was not: it opened with an H1 and only ``unstructured_result`` saw it.

        ``unstructured_result`` is the notifier's fallback predicate and looks
        for ASCII substitutes, not headings, so an H1 in a fixture named
        ``STRUCTURED_RESULT`` stayed green while the detector called the same
        text seriously mis-shaped.
        """
        from test_kanban_notifier import STRUCTURED_RESULT

        self.assertEqual(serious_defects(STRUCTURED_RESULT), ())

    def test_the_advice_for_an_h1_covers_the_unfenced_manifest_reading(self):
        """``# comment`` at column 0 in raw YAML is not a heading to demote.

        The gate cannot tell the two apart and holds either way — unfenced, a
        manifest renders as a wall of text — but "use ``##`` instead" is the
        wrong edit for a comment marker, and this text is handed straight to a
        model that will act on it.
        """
        advice = DEFECT_ADVICE["top-level-heading"]
        self.assertIn("fence", advice)
        self.assertIn("Do not delete the `#`", advice)


class WellShapedFixtureTest(unittest.TestCase):
    """The suite's own exemplars have to match the shape they document."""

    def test_the_notifier_suites_structured_fixture_is_clean(self):
        """It was not: it opened with an H1 and only ``unstructured_result`` saw it.

        ``unstructured_result`` is the notifier's fallback predicate and looks
        for ASCII substitutes, not headings, so an H1 in a fixture named
        ``STRUCTURED_RESULT`` stayed green while the detector called the same
        text seriously mis-shaped.
        """
        from test_kanban_notifier import STRUCTURED_RESULT

        self.assertEqual(serious_defects(STRUCTURED_RESULT), ())


if __name__ == "__main__":
    unittest.main()
