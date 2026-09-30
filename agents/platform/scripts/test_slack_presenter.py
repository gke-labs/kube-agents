"""Unit tests for slack_presenter — the reactions an ask gets.

Run: python3 -m pytest agents/platform/scripts/test_slack_presenter.py
"""

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


if __name__ == "__main__":
    unittest.main()
