"""Unit tests for slack_presenter — answer layout, buttons and reactions.

Run: python3 -m pytest agents/platform/scripts/test_slack_presenter.py
"""

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import slack_presenter as sp


class FlagTest(unittest.TestCase):
    def test_off_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(sp.enabled())

    def test_on_values(self):
        for value in ("1", "true", "TRUE", " yes ", "on"):
            with mock.patch.dict(os.environ, {"KAGE_SLACK_UX": value}):
                self.assertTrue(sp.enabled(), value)

    def test_other_values_are_off(self):
        for value in ("", "0", "false", "off", "no", "enabled"):
            with mock.patch.dict(os.environ, {"KAGE_SLACK_UX": value}):
                self.assertFalse(sp.enabled(), value)


class ArrivalReactionTest(unittest.TestCase):
    CASES = (
        # question / check -> eyes
        ("is checkout-gateway restarting? it looks like it's crashlooping?", "eyes"),
        ("why is the node pool at 90%?", "eyes"),
        ("check the ingress certs on seeded-a", "eyes"),
        ("<@U0KAGE> what version is seeded-b on", "eyes"),
        # change -> hammer_and_wrench
        ("fix it", "hammer_and_wrench"),
        ("can you fix the crashlooping pod?", "hammer_and_wrench"),
        ("bump the image to 1.4.2", "hammer_and_wrench"),
        ("roll back checkout to yesterday's release", "hammer_and_wrench"),
        ("please rollback the deploy", "hammer_and_wrench"),
        ("open a PR for the memory limit", "hammer_and_wrench"),
        ("open a pull request with that change", "hammer_and_wrench"),
        ("scale the web deployment down to 2", "hammer_and_wrench"),
        ("upgrade seeded-c to 1.31", "hammer_and_wrench"),
        # board -> clipboard
        ("board", "clipboard"),
        ("what's running right now?", "clipboard"),
        ("status of the fleet audit", "clipboard"),
        # incident -> rotating_light
        ("checkout is down", "rotating_light"),
        ("we're paging on payments", "rotating_light"),
        ("sev1 outage in us-east4", "rotating_light"),
        ("payments pods in crashloopbackoff", "rotating_light"),
        # no match -> eyes
        ("thanks", "eyes"),
        ("", "eyes"),
        (None, "eyes"),
    )

    def test_keyword_map(self):
        for text, expected in self.CASES:
            with self.subTest(text=text):
                self.assertEqual(sp.arrival_reaction(text), expected)

    def test_word_boundaries(self):
        # "prefix", "downstream", "statusline" and "scaled-up names" are not the keywords.
        self.assertEqual(sp.arrival_reaction("the prefix looks odd"), "eyes")
        self.assertEqual(sp.arrival_reaction("downstream latency"), "eyes")
        self.assertEqual(sp.arrival_reaction("the statusline plugin"), "eyes")

    def test_change_beats_incident_and_question(self):
        self.assertEqual(sp.arrival_reaction("prod is down, roll back now"), "hammer_and_wrench")
        self.assertEqual(sp.arrival_reaction("should we scale down?"), "hammer_and_wrench")


class SettleReactionTest(unittest.TestCase):
    def test_outcomes(self):
        self.assertEqual(sp.settle_reaction("done"), "white_check_mark")
        self.assertEqual(sp.settle_reaction("blocked"), "double_vertical_bar")
        self.assertEqual(sp.settle_reaction("failed"), "x")
        self.assertIsNone(sp.settle_reaction("cancelled"))

    def test_kanban_kinds(self):
        self.assertEqual(sp.settle_for_kanban_kind("completed"), "done")
        self.assertEqual(sp.settle_for_kanban_kind("blocked"), "blocked")
        self.assertEqual(sp.settle_for_kanban_kind("review_requested"), "blocked")
        self.assertEqual(sp.settle_for_kanban_kind("gave_up"), "failed")
        self.assertEqual(sp.settle_for_kanban_kind("block_loop_detected"), "blocked")
        # Retried by the dispatcher, or bookkeeping: nothing has settled.
        for kind in ("crashed", "timed_out", "status", "heartbeat", "archived", "unblocked"):
            self.assertIsNone(sp.settle_for_kanban_kind(kind), kind)

    def test_no_reaction_is_a_removal(self):
        names = set(sp.SETTLE_REACTIONS.values()) | {
            sp.REACTION_QUESTION, sp.REACTION_CHANGE, sp.REACTION_BOARD, sp.REACTION_INCIDENT,
        }
        self.assertEqual(len(names), 7)


class SplitAnswerTest(unittest.TestCase):
    def test_first_sentence_is_headline(self):
        md = "checkout-gateway is crashlooping on an OOM. It hit its 256Mi limit.\n\nRaise it to 512Mi."
        headline, body = sp.split_answer(md)
        self.assertEqual(headline, "checkout-gateway is crashlooping on an OOM.")
        self.assertEqual(body, ["It hit its 256Mi limit.", "Raise it to 512Mi."])

    def test_markdown_stripped_from_headline(self):
        headline, body = sp.split_answer("## **Yes**, [seeded-a](https://x) is `healthy`\n- one\n- two")
        self.assertEqual(headline, "Yes, seeded-a is healthy")
        self.assertEqual(body, ["- one\n- two"])

    def test_empty(self):
        self.assertEqual(sp.split_answer(""), ("", []))
        self.assertEqual(sp.split_answer("   \n\n "), ("", []))

    def test_code_fence_not_split(self):
        md = "Here it is.\n\n```\na\n\nb\n```\n\nDone."
        _, body = sp.split_answer(md)
        self.assertEqual(body, ["```\na\n\nb\n```", "Done."])

    def test_leading_code_block_has_no_headline(self):
        headline, body = sp.split_answer("```\nkubectl get pods\n```")
        self.assertEqual(headline, "")
        self.assertEqual(body, ["```\nkubectl get pods\n```"])

    def test_an_abbreviation_does_not_end_the_headline(self):
        for line in (
            "Pods fail readiness, e.g. checkout-gateway in ns. prod.",
            "Pods fail readiness, e.g. Checkout in prod.",
        ):
            self.assertEqual(sp.split_answer(line), (line, []))

    def test_a_headline_is_not_cut_after_etc(self):
        headline, _body = sp.split_answer("Deployments, StatefulSets, etc. All fail readiness. Node-a is cordoned.")
        self.assertEqual(headline, "Deployments, StatefulSets, etc. All fail readiness.")

    def test_a_soft_wrapped_first_sentence_is_the_whole_headline(self):
        md = "payments-api keeps crashing: secret\npayments-db-creds is missing. Since 14:02.\n- one"
        headline, body = sp.split_answer(md)
        self.assertEqual(headline, "payments-api keeps crashing: secret payments-db-creds is missing.")
        self.assertEqual(body, ["Since 14:02.\n- one"])

    def test_a_fence_under_the_first_line_is_not_joined_into_the_headline(self):
        md = "payments-api is crashlooping with:\n```\nOOMKilled. Exit 137\n```\nSince 14:02."
        headline, body = sp.split_answer(md)
        self.assertEqual(headline, "payments-api is crashlooping with:")
        self.assertEqual(body, ["```\nOOMKilled. Exit 137\n```\nSince 14:02."])

    def test_a_leading_code_span_is_not_a_fence(self):
        md = "```payments-api``` is crashlooping on OOM.\n\nRaise the limit."
        headline, body = sp.split_answer(md)
        self.assertEqual(headline, "payments-api is crashlooping on OOM.")
        self.assertEqual(body, ["Raise the limit."])

    def test_a_period_inside_a_code_span_does_not_end_the_headline(self):
        headline, body = sp.split_answer("The pod logs `connection refused. Retrying` on every start. Since 14:02.")
        self.assertEqual(headline, "The pod logs connection refused. Retrying on every start.")
        self.assertEqual(body, ["Since 14:02."])

    def test_fallback_text_keeps_a_plain_headline_as_given(self):
        headline, _body = sp.split_answer("`__init__.py` is missing.")
        self.assertEqual(sp.fallback_text(headline), "*__init__.py is missing.*")

    def test_a_leading_heading_is_not_joined_to_the_next_line(self):
        headline, body = sp.split_answer("##\tSummary\nCheckout is down. Since 14:02.")
        self.assertEqual(headline, "Summary")
        self.assertEqual(body, ["Checkout is down. Since 14:02."])

    def test_bold_around_the_first_sentence_leaves_no_stray_markers(self):
        headline, body = sp.split_answer("**Checkout is crashlooping on OOM. Raise the limit.**")
        self.assertEqual(headline, "Checkout is crashlooping on OOM.")
        self.assertEqual(body, ["**Raise the limit.**"])
        headline, body = sp.split_answer("**Checkout is down.** Raise the limit.")
        self.assertEqual(headline, "Checkout is down.")
        self.assertEqual(body, ["Raise the limit."])
        self.assertEqual(sp.fallback_text(headline), "*Checkout is down.*")

    def test_an_in_word_double_marker_is_not_rebalanced_as_bold(self):
        headline, body = sp.split_answer("The env var DB__HOST is unset. Pods crashloop.")
        self.assertEqual(headline, "The env var DB__HOST is unset.")
        self.assertEqual(body, ["Pods crashloop."])
        headline, body = sp.split_answer("Memory is 2**20 bytes. Raise it.")
        self.assertEqual(headline, "Memory is 2**20 bytes.")
        self.assertEqual(body, ["Raise it."])

    def test_plain_keeps_paired_in_word_double_markers(self):
        self.assertEqual(sp.split_answer("DB__HOST or DB__PORT is unset.")[0], "DB__HOST or DB__PORT is unset.")
        self.assertEqual(sp._plain("Set DB__HOST and DB__PORT"), "Set DB__HOST and DB__PORT")
        self.assertEqual(sp._plain("2**20 and 2**30 bytes"), "2**20 and 2**30 bytes")
        self.assertEqual(sp._plain("**Checkout** and __payments__ are down"), "Checkout and payments are down")

    def test_plain_strips_in_word_star_bold_but_not_underscore_bold(self):
        self.assertEqual(sp._plain("**Pod**s are down"), "Pods are down")
        self.assertEqual(sp._plain("It re**start**ed twice"), "It restarted twice")
        self.assertEqual(sp._plain("**3**x faster"), "3x faster")
        self.assertEqual(sp._plain("snake__case__name"), "snake__case__name")
        self.assertEqual(sp._plain("x**2 and y**2"), "x**2 and y**2")
        headline, body = sp.split_answer("**Pod**s are down. Restart them.")
        self.assertEqual(headline, "Pods are down.")
        self.assertEqual(body, ["Restart them."])

    def test_more_abbreviations_do_not_end_the_headline(self):
        for line in ("Node pool np-1 at rev. 7 is cordoned.", "Certs expired Sept. 30 on seeded-a."):
            self.assertEqual(sp.split_answer(line), (line, []))

    def test_plain_leaves_code_spans_and_globs_alone(self):
        self.assertEqual(sp._plain("`__init__.py` is missing"), "__init__.py is missing")
        self.assertEqual(sp._plain("Delete `__pycache__` and **this**"), "Delete __pycache__ and this")
        self.assertEqual(sp._plain("Remove *.log,*.tmp"), "Remove *.log,*.tmp")

    def test_clip_keeps_the_start_of_a_long_word(self):
        clipped = sp._clip("apply Option B: " + "x" * 120, sp.BUTTON_TEXT_MAX)
        self.assertTrue(clipped.startswith("apply Option B: xxx"))
        self.assertLessEqual(len(clipped), sp.BUTTON_TEXT_MAX)
        self.assertEqual(sp._clip("word " * 30, 20), "word word word…")

    def test_plain_keeps_a_literal_star_and_drops_markup(self):
        self.assertEqual(sp._plain("Scale replicas 2*3 → 6"), "Scale replicas 2*3 → 6")
        self.assertEqual(sp._plain("Delete *.tmp under /var/cache"), "Delete *.tmp under /var/cache")
        self.assertEqual(sp._plain("**Bold**, *italic*, _also_ and `code`"), "Bold, italic, also and code")
        self.assertEqual(
            sp._plain("see [logs](https://x/query=(k8s_container)) now"), "see logs now"
        )

    def test_a_numbered_line_is_not_cut_at_its_number(self):
        headline, body = sp.split_answer("1. Checkout is crashlooping on OOM. It hit 256Mi.")
        self.assertEqual(headline, "Checkout is crashlooping on OOM.")
        self.assertEqual(body, ["It hit 256Mi."])

    def test_leading_tilde_block_has_no_headline(self):
        headline, body = sp.split_answer("~~~\nkubectl get pods\n~~~")
        self.assertEqual(headline, "")
        self.assertEqual(body, ["~~~\nkubectl get pods\n~~~"])

    def test_long_headline_clipped(self):
        headline, _ = sp.split_answer("word " * 100)
        self.assertLessEqual(len(headline), sp.HEADLINE_MAX)
        self.assertTrue(headline.endswith("…"))


class ButtonsTest(unittest.TestCase):
    def test_link_button(self):
        self.assertEqual(
            sp._button("Open PR ↗", "kage.link.0", url="https://github.com/o/r/pull/1"),
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Open PR ↗", "emoji": True},
                "action_id": "kage.link.0",
                "url": "https://github.com/o/r/pull/1",
            },
        )
        self.assertRegex("kage.link.0", sp.LINK_ACTION_ID_PATTERN)

    def test_choice_button_value_is_label(self):
        button = sp._button("Raise to 512Mi", "triage.choice.0", value="Raise to 512Mi")
        self.assertEqual(button["value"], "Raise to 512Mi")
        self.assertNotIn("url", button)
        self.assertRegex(button["action_id"], sp.CHOICE_ACTION_ID_PATTERN)
        self.assertIsNone(sp.LINK_ACTION_ID_PATTERN.search(button["action_id"]))

    def test_buttons_wrap_at_five(self):
        rows = sp._actions([sp._button(str(i), f"kage.choice.{i}", value=str(i)) for i in range(7)])
        self.assertEqual([len(b["elements"]) for b in rows], [5, 2])
        self.assertEqual({b["type"] for b in rows}, {"actions"})

    def test_long_label_clipped(self):
        button = sp._button("word " * 40, "kage.choice.0", value="word " * 40)
        self.assertLessEqual(len(button["text"]["text"]), sp.BUTTON_TEXT_MAX)
        self.assertEqual(button["value"], "word " * 40)


class FallbackTextTest(unittest.TestCase):
    def test_same_layout_as_mrkdwn(self):
        text = sp.fallback_text("Two findings.", links=[("Open PR", "https://p")], choices=["Yes", "No"])
        self.assertEqual(text, "*Two findings.*\n<https://p|Open PR>\nReply with one of: Yes · No")

    def test_labels_cannot_mention_anyone(self):
        text = sp.fallback_text("h", links=[("<!here>", "https://p")], choices=["<@U1> & <!channel>", "No"])
        self.assertNotIn("<!", text)
        self.assertNotIn("<@", text)
        self.assertIn("&lt;@U1&gt; &amp; &lt;!channel&gt;", text)

    def test_a_link_url_cannot_end_the_link_early(self):
        text = sp.fallback_text("h", links=[("Logs", "https://p/?q=a|b>c<d&e")])
        self.assertIn("<https://p/?q=a%7Cb%3Ec%3Cd&amp;e|Logs>", text)

    def test_an_entity_in_a_link_url_is_kept_as_written(self):
        text = sp.fallback_text("h", links=[("Logs", "https://p/?q=a&lt;b")])
        self.assertIn("<https://p/?q=a&amp;lt;b|Logs>", text)


class LinkAckTest(unittest.TestCase):
    def test_ack_does_nothing_else(self):
        ack = mock.AsyncMock()
        body = mock.Mock()
        asyncio.run(sp.ack_link_click(ack, body, {"action_id": "kage.link.0"}))
        ack.assert_awaited_once_with()
        self.assertEqual(body.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
