#!/usr/bin/env python3
"""Unit tests for the pull-request template check.

Run: cd scripts && python3 -m unittest test_check_pr_template

Each rule is pinned in both directions on a throwaway template -- the shapes
it must catch, and the ordinary template prose it must leave alone -- and one
test runs against the committed template so that the file CI reads is the
file these rules were written for. The parser's shape (a heading is a
section, a heading inside a fence is not, a fence closes on its own marker, a
comment is what ``<!-- -->`` encloses) is pinned too, because it is the shape
the two readers use, and a check that parsed the template differently would
pass a section they fail.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import check_pr_template as check

# Literals below are the shapes under test, not text the template may carry.
PLAIN = "## Summary\n\n<!--\nOne paragraph.\n-->\n\n## Testing\n\n<!-- What you ran. -->\n\n-\n"
CONDITIONAL_OK = (
    "## Bug Fix\n\n<!--\nRequired when this PR fixes a bug.\n"
    'Otherwise write "Not a bug fix."\n\n- Why it shipped.\n-->\n\n-\n'
)
CONDITIONAL_NO_ANSWER = "## Roster\n\n<!--\nOnly when this PR adds a roster line.\n-->\n\n-\n"
CONDITIONAL_SPLIT_ANSWER = (
    "## Roster\n\n<!--\nRequired when this PR adds a roster line. "
    'Otherwise write "Not a\nroster change."\n-->\n\n-\n'
)


def section(comment: str, heading: str = "R") -> str:
    return f"## {heading}\n\n<!--\n{comment}\n-->\n\n-\n"


def problems_of(text: str) -> list[str]:
    return [message for _heading, message in check.problems(text)]


class SectionsTest(unittest.TestCase):
    def test_h2_through_h6_are_sections_and_h1_is_not(self) -> None:
        text = "# Title\n\n## A\n\nbody\n\n### B\n\n###### C\n"
        self.assertEqual([h for h, _ in check.sections(text)], ["A", "B", "C"])

    def test_heading_inside_fence_is_not_a_section(self) -> None:
        text = "## A\n\n```\n## not a heading\n```\n\n~~~\n### nor this\n~~~\n\n## B\n"
        self.assertEqual([h for h, _ in check.sections(text)], ["A", "B"])

    def test_a_fence_closes_on_the_marker_that_opened_it(self) -> None:
        # As both readers do: a `~~~` line inside a ``` block is text, so the
        # block runs to the next ```, and the heading after it is a section.
        text = "## A\n\n```\n~~~\n## still fenced\n```\n\n## B\n"
        self.assertEqual([h for h, _ in check.sections(text)], ["A", "B"])

    def test_content_runs_to_the_next_heading(self) -> None:
        text = "## A\n\n<!-- one -->\n\n-\n\n## B\n\n<!-- two -->\n"
        sections = dict(check.sections(text))
        self.assertIn("<!-- one -->", sections["A"])
        self.assertNotIn("<!-- two -->", sections["A"])

    def test_comments_are_what_the_markers_enclose(self) -> None:
        self.assertEqual(
            check.comments("x\n<!--\nfirst\n-->\ny\n<!-- second -->\n"),
            ["\nfirst\n", " second "],
        )


class FirstSentenceTest(unittest.TestCase):
    def test_ends_at_the_first_sentence_end(self) -> None:
        self.assertEqual(check.first_sentence("\nOne. Two.\n"), "One.")

    def test_a_colon_does_not_end_a_sentence(self) -> None:
        self.assertEqual(
            check.first_sentence("Required: an empty section is not an answer. Three forms:"),
            "Required: an empty section is not an answer.",
        )

    def test_a_short_label_stays_with_the_sentence_after_it(self) -> None:
        self.assertEqual(check.first_sentence("Note: required when X. More."), "Note: required when X.")
        self.assertEqual(check.first_sentence("Optional: fill in when X."), "Optional: fill in when X.")

    def test_leading_list_markers_are_dropped(self) -> None:
        self.assertEqual(check.first_sentence("- Required when X. Y."), "Required when X.")


class RemovalRuleTest(unittest.TestCase):
    def test_each_removal_phrase_fails(self) -> None:
        for phrase in (
            "Otherwise delete this section.",
            "Otherwise, remove this section.",
            "If not, omit this section.",
            "Delete the section when it does not apply.",
            "Drop this section otherwise.",
            "Skip this section unless this PR adds a roster case.",
            "Leave this section out when this PR adds no roster case.",
            "Erase this whole section when it does not apply.",
            "This section can be removed when this PR adds no roster case.",
            "The heading should be deleted otherwise.",
            "Show the red run and the misses. Otherwise just delete it.",
            "Otherwise, feel free to delete.",
            "If not, delete them.",
            "If not delete this section.",
            "If not applicable delete this section.",
            "Delete the sections that do not apply to this PR.",
            "Remove the headings that do not apply.",
            'Delete the "Roster Admission" section if this PR adds no case.',
            "Delete the Bug Fix: Preventing Recurrence section otherwise.",
            "This section is removed when the PR adds no roster case.",
            "This section will be deleted when it does not apply.",
            "Otherwise, leave it out.",
            "Otherwise it can be deleted.",
            "If this PR does not add a roster case, delete this section.",
            "When this PR is not a bug fix, remove this section.",
            "If this PR does not fix a bug delete this section.",
            "For a docs-only change that does not touch an agent, delete this section.",
            "Remove if not applicable.",
            "Delete if this PR is not a bug fix.",
            "Skip unless this PR adds a roster case.",
            "Delete any sections that do not apply to this PR.",
            "Delete every section that does not apply.",
            "Delete the above section if this PR adds no case.",
            "Show the red run and the misses. Otherwise delete it when the PR adds no roster case.",
        ):
            with self.subTest(phrase=phrase):
                self.assertEqual(problems_of(section(phrase)), [check.REMOVAL_MESSAGE])

    def test_a_negated_instruction_is_not_a_removal(self) -> None:
        for phrase in (
            'Do not delete this section; write "Not applicable" instead.',
            "Required: do not skip this section.",
            "Never remove this heading.",
            "Don't drop this section.",
            "Answer without removing the section.",
            "Rather than delete this section, write the one-line answer.",
            'Write "Not a bug fix." rather than remove the section.',
            "Under no circumstances delete this section.",
            "Do not, under any circumstances, delete this section.",
            "There is no reason to delete this section.",
            'Write the one-line answer, never "delete this section".',
            "If this PR adds no case, do not, under any circumstances, delete this section.",
            "You should not delete this section.",
            "Take care not to delete this section.",
            "Be careful not to remove this heading.",
        ):
            with self.subTest(phrase=phrase):
                self.assertNotIn(check.REMOVAL_MESSAGE, problems_of(section(phrase)))

    def test_removing_something_else_is_not_removing_the_section(self) -> None:
        for phrase in (
            "Remove the block below before posting.",
            "Delete these lines once you have read them.",
            "If the run is linked, paste the URL; otherwise drop the link and quote the line.",
            "Keep the bullets that apply; otherwise cut them.",
            "If not, skip the screenshot.",
            "Otherwise, remove the `expected_fail` flag from the case.",
            "Otherwise skip to Testing.",
            "Cut the section down to one paragraph.",
            "Strip the section of anything a changelog would carry.",
            "Skip this section's checklist when nothing ran.",
            "Remove the bullets in this section that do not apply.",
            "Delete the placeholder text under this heading before posting.",
            "Skip this section\u2019s checklist when nothing ran.",
        ):
            with self.subTest(phrase=phrase):
                self.assertEqual(problems_of(section(phrase)), [])

    def test_removal_phrase_outside_a_comment_is_not_read(self) -> None:
        # The check reads instructions to the author, which live in comments;
        # a body-side mention is a different thing and not this check's call.
        text = "## Notes\n\nDo not delete this section of the design.\n"
        self.assertEqual(problems_of(text), [])


class ConditionRuleTest(unittest.TestCase):
    def test_conditional_with_a_one_line_answer_is_clean(self) -> None:
        self.assertEqual(problems_of(CONDITIONAL_OK), [])

    def test_conditional_without_an_answer_fails(self) -> None:
        self.assertEqual(check.problems(CONDITIONAL_NO_ANSWER), [("Roster", check.NO_ANSWER_MESSAGE)])

    def test_answer_split_across_a_line_break_fails(self) -> None:
        self.assertEqual(
            check.problems(CONDITIONAL_SPLIT_ANSWER), [("Roster", check.SPLIT_ANSWER_MESSAGE)]
        )

    def test_each_condition_signal_is_recognised(self) -> None:
        for opener in (
            "Required when this PR adds a roster line.",
            "Only when this PR adds a roster line.",
            "Only if this PR adds a roster line.",
            "This section applies only when this PR adds a roster case.",
            "Applies if this PR adds a roster line.",
            "Optional: fill in when this PR adds a roster case.",
            "Optional unless this PR adds a roster case.",
            "Note: required when this PR adds a roster case.",
            "Roster Admission: required when this PR adds a roster case.",
            "Fill this in when this PR adds a roster case.",
            "Complete this section only if this PR adds a roster case.",
            "Needed when this PR adds a roster case.",
            "Not required for a docs change.",
            "- Required when this PR adds a roster case.",
            "This section only applies when this PR adds a roster case.",
            "Only fill in when this PR adds a roster case.",
            "Only for roster admissions.",
            "Applies only to pull requests that add a roster case.",
        ):
            with self.subTest(opener=opener):
                self.assertEqual(problems_of(section(opener)), [check.NO_ANSWER_MESSAGE])

    def test_ordinary_instructions_are_not_conditions(self) -> None:
        for sentence in (
            "Required: an empty section is not an answer.",
            "Only what a reviewer needs to know, not a list for the changelog.",
            "Required, even for a docs change.",
            "Needed for the reviewer to judge blast radius.",
            "Fill in for every PR.",
            "Answer for a human, not for a changelog.",
            "Applies to every PR, including docs; if nothing ran, say so.",
            "If this PR closes an issue, link it.",
            "When in doubt, write more.",
            "If the install is shared with other agents, take the lease first.",
            "What you ran, and what it said.",
            "Name the optional values the chart gains.",
            "A screenshot is optional for a change with no graphical surface.",
            "Link the issue unless it is private.",
            "Name a file only when the reviewer needs to know why it matters.",
            "A screenshot is required when the change touches the console.",
            "A lease is needed when the install is shared.",
        ):
            with self.subTest(sentence=sentence):
                self.assertEqual(problems_of(section(sentence)), [])

    def test_a_signal_deeper_than_the_first_sentence_is_not_read(self) -> None:
        # The template's own Self-Review comment says '"No findings" is ... a
        # complete answer only when you also say what you looked for'.
        text = section('What you ran.\n"None" is a complete answer only when there were none.')
        self.assertEqual(problems_of(text), [])

    def test_a_condition_in_a_later_comment_is_read(self) -> None:
        text = "## R\n\n<!-- Red run, misses. -->\n\n<!-- Only when this PR adds a roster case. -->\n"
        self.assertEqual(problems_of(text), [check.NO_ANSWER_MESSAGE])

    def test_a_sections_comments_are_read_together_for_the_answer(self) -> None:
        text = '## R\n\n<!-- Required when this PR adds a roster line. -->\n\n<!-- Otherwise write "No." -->\n'
        self.assertEqual(problems_of(text), [])

    def test_curly_single_and_backtick_quotes_count_as_an_answer(self) -> None:
        for quoted in ("“Not a bug fix.”", "'Not a bug fix.'", "‘Not a bug fix.’", "`Not a bug fix.`"):
            with self.subTest(quoted=quoted):
                text = section(f"Required when this PR fixes a bug. Otherwise write {quoted}")
                self.assertEqual(problems_of(text), [])

    def test_answer_forms_next_to_the_canonical_one_count(self) -> None:
        for answer in (
            'If not, write "Not a bug fix."',
            'Otherwise write the line "Not a bug fix."',
            'Otherwise write exactly "Not a bug fix."',
            'Otherwise say "Not a bug fix."',
            'Otherwise write: "Not a bug fix."',
            'Otherwise answer "Not a bug fix."',
        ):
            with self.subTest(answer=answer):
                self.assertEqual(problems_of(section(f"Required when this PR fixes a bug. {answer}")), [])

    def test_unconditional_sections_are_clean(self) -> None:
        self.assertEqual(problems_of(PLAIN), [])

    def test_a_section_with_no_comment_is_clean(self) -> None:
        self.assertEqual(problems_of("## A\n\n-\n"), [])


class ShapeRuleTest(unittest.TestCase):
    def test_a_heading_inside_a_comment_fails_as_unbalanced_markers(self) -> None:
        # The readers split the section at the inner heading, so neither half
        # holds a balanced comment and the instruction inside is read by nobody.
        text = "## A\n\n<!--\n## Example\nOnly when it applies. Otherwise delete this section.\n-->\n"
        self.assertEqual(
            check.problems(text),
            [("A", check.UNBALANCED_MESSAGE), ("Example", check.UNBALANCED_MESSAGE)],
        )

    def test_a_fence_line_inside_a_comment_fails(self) -> None:
        # One fence line in a comment opens a fence nothing closes, so every
        # later heading is hidden from the readers and from this check.
        text = (
            "## R\n<!--\nOpen the transcript with\n```\non its own line.\n-->\n"
            "## S\n<!--\nOnly when this PR touches the chart.\n-->\n"
        )
        self.assertEqual(
            check.problems(text),
            [("R", check.LINE_IN_COMMENT_MESSAGE), ("R", check.OPEN_FENCE_MESSAGE)],
        )

    def test_a_fenced_pair_inside_a_comment_fails(self) -> None:
        text = "## R\n<!--\nFor example:\n```\nmake test\n```\n-->\n## S\n<!-- ok -->\n"
        self.assertEqual(check.problems(text), [("R", check.LINE_IN_COMMENT_MESSAGE)])

    def test_a_heading_shaped_line_in_a_comment_inside_a_body_fence_is_fine(self) -> None:
        # The readers keep the comment whole there too, so there is nothing to report.
        self.assertEqual(problems_of("## R\n\n```\n<!--\n## x\n-->\n```\n"), [])

    def test_a_fence_in_the_body_is_fine(self) -> None:
        text = "## R\n\n<!-- Paste the command. -->\n\n```\nmake test\n```\n\n## S\n"
        self.assertEqual(problems_of(text), [])

    def test_a_removal_before_the_first_heading_is_read(self) -> None:
        text = "# Pull request\n\n<!-- Delete any sections that do not apply to this PR. -->\n\n## Summary\n\n<!-- One paragraph. -->\n"
        self.assertEqual(check.problems(text), [(check.PREAMBLE_HEADING, check.REMOVAL_MESSAGE)])
        self.assertEqual([h for h, _ in check.sections(text)], ["Summary"])

    def test_a_condition_before_the_first_heading_is_not_a_section(self) -> None:
        text = "<!-- Optional: read the rules first. -->\n\n## Summary\n\n<!-- One paragraph. -->\n"
        self.assertEqual(problems_of(text), [])

    def test_a_byte_order_mark_does_not_hide_the_first_heading(self) -> None:
        text = "\ufeff## Summary\n\n<!-- Otherwise delete this section. -->\n"
        self.assertEqual(check.problems(text), [("Summary", check.REMOVAL_MESSAGE)])

    def test_a_fence_before_the_first_heading_is_reported_as_open(self) -> None:
        self.assertEqual(
            check.problems("# Title\n\n```\n\n## A\n\n<!-- x -->\n"),
            [(check.NO_SECTIONS_HEADING, check.OPEN_FENCE_MESSAGE)],
        )

    def test_a_template_with_no_section_fails(self) -> None:
        self.assertEqual(
            check.problems("# Title\n\nprose only\n"),
            [(check.NO_SECTIONS_HEADING, check.NO_SECTIONS_MESSAGE)],
        )

    def test_committed_template_is_clean(self) -> None:
        text = (check.REPO / check.TEMPLATE).read_text(encoding="utf-8")
        self.assertEqual(check.problems(text), [])
        self.assertGreater(len(check.sections(text)), 1)


class MainTest(unittest.TestCase):
    def run_main(self, text: str | None) -> tuple[int, str, Path]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "draft.md"
            if text is not None:
                path.write_text(text, encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = check.main([str(path)])
        return code, out.getvalue(), path

    def test_clean_template_passes_and_counts_sections(self) -> None:
        code, out, _ = self.run_main(PLAIN + CONDITIONAL_OK)
        self.assertEqual(code, 0)
        self.assertIn("3 sections", out)

    def test_failing_template_names_the_file_the_section_and_the_remedy(self) -> None:
        code, out, path = self.run_main(PLAIN + CONDITIONAL_NO_ANSWER)
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)
        self.assertIn(str(path), out)
        self.assertIn("'Roster'", out)
        self.assertIn('Otherwise write "', out)
        self.assertIn("kube-agents-bot", out)

    def test_every_problem_is_listed_not_just_the_first(self) -> None:
        code, out, _ = self.run_main(CONDITIONAL_NO_ANSWER + CONDITIONAL_SPLIT_ANSWER)
        self.assertEqual(code, 1)
        self.assertIn(check.NO_ANSWER_MESSAGE, out)
        self.assertIn(check.SPLIT_ANSWER_MESSAGE, out)

    def test_missing_template_fails(self) -> None:
        code, out, _ = self.run_main(None)
        self.assertEqual(code, 1)
        self.assertIn("MISSING", out)


if __name__ == "__main__":
    unittest.main()
