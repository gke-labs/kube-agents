#!/usr/bin/env python3
"""Tests for pr_triggers.py.

The load-bearing properties, in the order they would hurt if they broke:

* **Quoting the trigger is not using it.** Every document that explains this
  feature contains the command; so does every review comment that discusses it.
  Fenced blocks and inline code spans have to come out first, and the fence
  parser has to be CommonMark's rather than the obvious non-greedy regex.
* **Only self-authored markers count as an answer.** A marker scan that trusted
  any comment would let anybody suppress a request by pasting the string.
* **A mention is a whole handle.** `@kube-agents-bot-2` is not `@kube-agents-bot`,
  and an email address is not a mention of its local part.
* **The fence parser agrees with `audit_report.py`'s.** They are two copies of
  one hardened parser; `FenceParserAgreementTest` is what keeps them one.
"""

import importlib.util
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import forge  # noqa: E402
import pr_triggers  # noqa: E402

SELF = "kube-agents-bot"


def comment(author, body, node_id="n1"):
    return forge.Comment(
        node_id=node_id, author=author, body=body, can_write=True, created_at=""
    )


class FindTriggerTest(unittest.TestCase):
    def _find(self, body, self_login=SELF):
        return pr_triggers.find_trigger(body, self_login, "IC_1", "reviewer")

    def test_a_slash_command_on_its_own_line_fires(self):
        trigger = self._find("/agent why this value?")
        self.assertIsNotNone(trigger)
        self.assertEqual(trigger.kind, "slash")
        self.assertEqual(trigger.request, "why this value?")

    def test_an_indented_command_still_fires(self):
        """A reviewer replying inside a list indents, and means it."""
        trigger = self._find("- context\n    /agent bump to 4")
        self.assertIsNotNone(trigger)
        self.assertEqual(trigger.request, "bump to 4")

    def test_a_mid_sentence_command_does_not_fire(self):
        self.assertIsNone(self._find("we should /agent revert this at some point"))

    def test_a_command_inside_a_fenced_block_does_not_fire(self):
        body = "Type this:\n\n```\n/agent bump to 4\n```\n\nand it runs."
        self.assertIsNone(self._find(body))

    def test_three_fences_do_not_leak_the_middle_block(self):
        """The bug a non-greedy ```…``` regex has, in the direction that matters."""
        body = "```\na\n```\ntext\n```\n/agent do it\n"
        self.assertIsNone(self._find(body))

    def test_a_four_space_indented_fence_is_not_a_closer(self):
        """CommonMark's indentation bound — dropping it ends the block early."""
        body = "```\ninside\n    ```\n/agent do it\n```\n"
        self.assertIsNone(self._find(body))

    def test_a_tilde_fence_is_honoured(self):
        self.assertIsNone(self._find("~~~\n/agent do it\n~~~"))

    def test_an_unterminated_fence_runs_to_the_end(self):
        self.assertIsNone(self._find("```\n/agent do it"))

    def test_a_longer_closer_closes_and_a_shorter_one_does_not(self):
        self.assertIsNone(self._find("````\n/agent do it\n```\nstill inside"))
        self.assertIsNotNone(self._find("```\nfenced\n````\n/agent do it"))

    def test_a_mention_fires(self):
        trigger = self._find(f"@{SELF} can you look at this?")
        self.assertIsNotNone(trigger)
        self.assertEqual(trigger.kind, "mention")
        self.assertEqual(trigger.request, "")

    def test_the_bot_suffix_spelling_of_a_mention_also_fires(self):
        """GitHub shows an App as `@x` in some views and `@x[bot]` in others."""
        self.assertIsNotNone(self._find(f"@{SELF}[bot] please look"))

    def test_a_mention_is_case_insensitive(self):
        self.assertIsNotNone(self._find("@Kube-Agents-Bot please look"))

    def test_a_longer_handle_is_not_a_mention_of_the_shorter_one(self):
        self.assertIsNone(self._find(f"@{SELF}-2 please look"))

    def test_an_email_address_is_not_a_mention(self):
        self.assertIsNone(self._find(f"mail me at someone@{SELF}.example"))

    def test_a_mention_inside_an_inline_code_span_does_not_fire(self):
        self.assertIsNone(self._find(f"mention `@{SELF}` to wake it"))

    def test_a_mention_inside_a_fenced_block_does_not_fire(self):
        self.assertIsNone(self._find(f"```\n@{SELF} do it\n```"))

    def test_a_command_wins_over_a_mention_in_the_same_comment(self):
        trigger = self._find(f"@{SELF}\n/agent bump to 4")
        self.assertEqual(trigger.kind, "slash")
        self.assertEqual(trigger.request, "bump to 4")

    def test_several_command_lines_are_one_request(self):
        trigger = self._find("/agent\n/agent bump to 4")
        self.assertEqual(trigger.request, "bump to 4")

    def test_a_bare_command_with_no_request_still_fires(self):
        trigger = self._find("/agent")
        self.assertIsNotNone(trigger)
        self.assertEqual(trigger.request, "")
        self.assertEqual(trigger.summary, "(no request text)")

    def test_a_word_starting_with_agent_is_not_the_command(self):
        self.assertIsNone(self._find("/agentic thoughts here"))

    def test_crlf_line_endings_still_anchor(self):
        self.assertIsNotNone(self._find("context\r\n/agent bump to 4\r\n"))

    def test_backticks_around_the_request_are_stripped(self):
        trigger = self._find("/agent `bump to 4`")
        self.assertEqual(trigger.request, "bump to 4")

    def test_no_self_login_disables_mentions_but_not_commands(self):
        self.assertIsNone(self._find(f"@{SELF} hi", self_login=""))
        self.assertIsNotNone(self._find("/agent hi", self_login=""))

    def test_an_empty_body_is_not_a_trigger(self):
        self.assertIsNone(self._find(""))

    def test_the_summary_is_bounded(self):
        trigger = self._find("/agent " + "x" * 5000)
        self.assertEqual(len(trigger.summary), pr_triggers.MAX_REQUEST_CHARS)


class HandledNodeIdsTest(unittest.TestCase):
    def test_a_self_authored_marker_marks_a_request_answered(self):
        comments = [comment(SELF, "Done.\n\n<!-- agent-answered:IC_1 -->")]
        self.assertEqual(pr_triggers.handled_node_ids(comments, SELF), {"IC_1"})

    def test_a_refusal_marker_counts_too(self):
        comments = [comment(SELF, "<!-- agent-refused:IC_2 -->")]
        self.assertEqual(pr_triggers.handled_node_ids(comments, SELF), {"IC_2"})

    def test_a_marker_pasted_by_someone_else_is_ignored(self):
        """Otherwise anyone could suppress a request by quoting the string."""
        comments = [comment("attacker", "<!-- agent-answered:IC_1 -->")]
        self.assertEqual(pr_triggers.handled_node_ids(comments, SELF), set())

    def test_the_bot_suffix_does_not_break_self_recognition(self):
        comments = [comment(f"{SELF}[bot]", "<!-- agent-answered:IC_1 -->")]
        self.assertEqual(pr_triggers.handled_node_ids(comments, SELF), {"IC_1"})

    def test_author_case_does_not_break_self_recognition(self):
        comments = [comment("Kube-Agents-Bot", "<!-- agent-answered:IC_1 -->")]
        self.assertEqual(pr_triggers.handled_node_ids(comments, SELF), {"IC_1"})

    def test_several_markers_in_one_comment_are_all_read(self):
        comments = [
            comment(SELF, "<!-- agent-answered:IC_1 -->\n<!-- agent-answered:IC_2 -->")
        ]
        self.assertEqual(
            pr_triggers.handled_node_ids(comments, SELF), {"IC_1", "IC_2"}
        )

    def test_base64ish_node_ids_survive_the_pattern(self):
        node = "PRRC_kwDOA_b-c=="
        comments = [comment(SELF, pr_triggers.marker(node))]
        self.assertEqual(pr_triggers.handled_node_ids(comments, SELF), {node})

    def test_whitespace_inside_the_marker_is_tolerated(self):
        comments = [comment(SELF, "<!--   agent-answered : IC_1   -->")]
        self.assertEqual(pr_triggers.handled_node_ids(comments, SELF), {"IC_1"})

    def test_no_comments_means_nothing_handled(self):
        self.assertEqual(pr_triggers.handled_node_ids([], SELF), set())

    def test_a_self_comment_with_no_marker_handles_nothing(self):
        comments = [comment(SELF, "just a status update")]
        self.assertEqual(pr_triggers.handled_node_ids(comments, SELF), set())

    def test_the_marker_builder_round_trips_through_the_scanner(self):
        built = pr_triggers.marker("IC_9")
        self.assertEqual(
            pr_triggers.handled_node_ids([comment(SELF, built)], SELF), {"IC_9"}
        )


class FenceParserAgreementTest(unittest.TestCase):
    """`pr_triggers.strip_fenced_blocks` must not drift from `audit_report`'s.

    Delete this test — and the copy — when `audit_report.py` migrates onto this
    module.
    """

    CORPUS = (
        "",
        "plain text",
        "```\nfenced\n```\nafter",
        "```\na\n```\ntext\n```\ndangling\n",
        "```\ninside\n    ```\nstill inside\n```\nafter",
        "~~~\ntilde\n~~~\nafter",
        "````\nlong\n```\nstill inside\n````\nafter",
        "   ```\nthree spaces opens\n   ```\nafter",
        "    ```\nfour spaces does not open\n    ```\nafter",
        "```python\ncode\n```\nafter",
        "```\nunterminated",
        "a\r\nb",
    )

    @classmethod
    def setUpClass(cls):
        here = Path(__file__).resolve().parent
        path = here.parent / "skills" / "fleet-audit" / "scripts" / "audit_report.py"
        # Asserted rather than skipped: a moved audit_report.py silently
        # disabling the drift guard is the failure this test exists to prevent.
        assert path.exists(), f"audit_report.py not found at {path}"
        spec = importlib.util.spec_from_file_location("_audit_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.audit = module

    def test_fence_stripping_agrees(self):
        for text in self.CORPUS:
            with self.subTest(text=text):
                self.assertEqual(
                    pr_triggers.strip_fenced_blocks(text),
                    self.audit.strip_fenced_blocks(text),
                )

    def test_inline_code_stripping_agrees(self):
        for text in ("`a`", "``a`b``", "no code", "`unterminated", "a `b` c `d` e"):
            with self.subTest(text=text):
                self.assertEqual(
                    pr_triggers.strip_inline_code(text),
                    self.audit.strip_inline_code(text),
                )


if __name__ == "__main__":
    unittest.main()
