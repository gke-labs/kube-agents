"""Tests for chat_message, today's Slack presentation as kube-agents.chat/v1 messages.

The shared cases in tests/conformance/chat/ run through the emitter in
deploy/docker/patches/test_chat_conformance.py; these cover the composers' own
rules and keep the bridge's Go port on the same answers.
"""

import ast
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chat_message as cm
import slack_presenter as sp

#: The Go port's shared cases (a2a/hermes-bridge/chat.go), whose expected values are this module's.
BRIDGE_CASES = Path(__file__).resolve().parents[3] / "a2a" / "hermes-bridge" / "testdata" / "chat_cases.json"


def _row(task_id, status, **kw):
    return SimpleNamespace(task_id=task_id, title=kw.pop("title", task_id), lines=kw.pop("lines", []),
                           status=status, steps=kw.pop("steps", 0), note=kw.pop("note", ""),
                           result=kw.pop("result", ""), **kw)


class StandaloneTest(unittest.TestCase):
    def test_imports_nothing_from_the_gateway_or_slack(self):
        tree = ast.parse(Path(cm.__file__).read_text())
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        self.assertEqual(
            roots - {"__future__", "re", "collections", "typing", "chat_notify", "slack_moments", "slack_presenter",
                     "slack_status"},
            set(),
        )


class BridgeCasesTest(unittest.TestCase):
    """The Go port's cases hold this module's answers, so the two cannot drift apart."""

    def test_the_bridge_cases_are_the_presenters_answers(self):
        cases = json.loads(BRIDGE_CASES.read_text(encoding="utf-8"))
        for case in cases["split_answer"]:
            with self.subTest(split=case["in"][:40]):
                self.assertEqual(sp.split_answer(case["in"]), (case["headline"], case["body"]))
        for case in cases["plain"]:
            with self.subTest(plain=case["in"]):
                self.assertEqual(sp._plain(case["in"]), case["out"])
        for case in cases["ask_kind"]:
            with self.subTest(ask=case["in"]):
                self.assertEqual(cm.ask_kind(case["in"]), case["kind"])
        self.assertGreater(len(cases["split_answer"]), 0)


class CommandWordTest(unittest.TestCase):
    """The gateway's command matchers (a2a/lib/chattext), each as the gateway reads it."""

    def test_commands(self):
        for text in ("stop", "Cancel!", " abort. ", "what's happening?", "any update", "status", "/session",
                     "/session off", "delegate: check seeded-a", "is it doing anything", "how is it going"):
            with self.subTest(text=text):
                self.assertTrue(cm.command_word(text))

    def test_not_commands(self):
        for text in ("stop the drain on seeded-a", "seeded-a", "Try again", "delegate", "delegated tasks",
                     "/sessions", "any update to the config should be reverted, it is long enough now", "continue"):
            with self.subTest(text=text):
                self.assertFalse(cm.command_word(text))


class AnswerTest(unittest.TestCase):
    def test_the_rest_is_the_detail_and_a_long_one_goes_under_why(self):
        self.assertEqual(cm.answer_message("Yes. It restarts."),
                         {"schema": cm.SCHEMA, "headline": "Yes.", "detail": "It restarts."})
        long = "X" + "x" * cm.DETAIL_MAX
        self.assertEqual(cm.answer_message(f"Yes. {long}")["fold"], {"title": "why", "text": long})
        self.assertIsNone(cm.answer_message("Yes. X" + "x" * cm.FOLD_TEXT_MAX))
        self.assertIsNone(cm.answer_message("```\nkubectl get pods\n```"))


class PlanTest(unittest.TestCase):
    def test_mark_follows_the_rows(self):
        running, pending = _row("a", "in_progress"), _row("b", "pending")
        done, failed = _row("c", "complete"), _row("d", "error")
        archived = _row("e", "error", archived=True)
        self.assertEqual(cm.plan_mark("fleet", [running, pending]), {"ask": "fleet", "settle": "blocked"})
        self.assertEqual(cm.plan_mark("fleet", [running, done]), {"ask": "fleet"})
        self.assertEqual(cm.plan_mark("fleet", [done, failed]), {"settle": "failed"})
        self.assertEqual(cm.plan_mark("fleet", [done, archived]), {"settle": "done"}, "an archive is not a failure")
        self.assertIsNone(cm.plan_mark("", [running]), "no recorded kind: a running plan leaves the mark alone")
        self.assertIsNone(cm.plan_mark("fleet", []))

    def test_each_step_is_one_line(self):
        rows = [_row("a", "in_progress", lines=["two\nlines"], note="two\nlines", steps=1)]
        self.assertEqual(cm.plan_message(None, rows)["plan"]["items"][0]["steps"], ["two lines"])

    def test_text_is_unescaped(self):
        rows = [_row("a", "in_progress", title="R&D <!here>", note="x", lines=["x"], steps=1)]
        self.assertEqual(cm.plan_text(None, rows), "R&D <!here>\n◌ x")


class ChoicesTest(unittest.TestCase):
    def test_a_report_choice_the_gateway_would_act_on_stays_in_the_detail(self):
        chat = cm.report_message("3 findings", choices=["Look at seeded-a", "stop"], detail="checked 3")
        self.assertEqual(chat["choices"], [{"text": "Look at seeded-a", "primary": True}])
        self.assertEqual(chat["detail"], "checked 3\nstop")

    def test_a_needs_you_reply_names_the_card(self):
        chat = cm.needs_you_message("Which cluster?\n- seeded-a\n- seeded-b", "t_1")
        self.assertEqual(chat["choices"][0], {"text": "seeded-a", "reply": "seeded-a\n\n(Clicked on the question from card t_1.)"})
        self.assertNotIn("choices", cm.needs_you_settled(chat))

    def test_a_severity_alone_is_the_rows_text(self):
        chat = cm.report_message("x", rows=[{"severity": "high"}])
        self.assertEqual(chat["rows"], [{"text": "`high`"}])

    def test_an_after_past_five_lines_keeps_the_first_five(self):
        chat = cm.report_message("x", after_rows="\n".join(f"line {i}" for i in range(7)))
        self.assertEqual(chat["after"], [f"line {i}" for i in range(5)])

    def test_a_triage_reply_starts_with_its_label(self):
        triage = {"headline": "Down.", "fold_title": "why", "links": [],
                  "choices": [("Roll back (recommended)", "apply Option A: Roll back", True),
                              ("Scale", "apply Option B: Scale", False)]}
        chat = cm.triage_message(triage, "report")
        self.assertEqual(chat["choices"], [
            {"text": "Roll back", "reply": "Roll back — apply Option A", "primary": True},
            {"text": "Scale", "reply": "Scale — apply Option B"},
        ])
        self.assertIsNone(cm.triage_message(triage, "x" * (cm.FOLD_TEXT_MAX + 1)))
        self.assertTrue(cm.triage_text(triage, "report").startswith("Down.\nRoll back — apply Option A\n"))

    def test_failure_offers_try_again_only_when_asked(self):
        self.assertEqual(cm.failure_message("It failed. Retry?", True)["choices"], [{"text": "Try again"}])
        self.assertNotIn("choices", cm.failure_message("It failed. Retry?", False))
        self.assertIsNone(cm.failure_message("It failed. X" + "x" * cm.DETAIL_MAX, True))


class MarkTest(unittest.TestCase):
    def test_tokens_only(self):
        self.assertEqual(cm.mark("fleet", "done"), {"ask": "fleet", "settle": "done"})
        self.assertEqual(cm.mark("Fleet!", "maybe"), {})


if __name__ == "__main__":
    unittest.main()
