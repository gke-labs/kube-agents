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
CHAT_CASES = Path(__file__).resolve().parents[3] / "tests" / "conformance" / "chat"


def _case(name):
    """A shared case from tests/conformance/chat/, whose input is the fixture these tests read."""
    return json.loads((CHAT_CASES / f"{name}.json").read_text(encoding="utf-8"))


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
            roots - {"__future__", "re", "unicodedata", "urllib", "collections", "typing", "chat_notify", "slack_moments", "slack_presenter",
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
        for case in cases["answer_chat"]:
            with self.subTest(answer=case["in"][:40]):
                self.assertEqual(cm.answer_message(case["in"]), case["chat"])
        self.assertGreater(len(cases["split_answer"]), 0)
        self.assertGreater(len(cases["answer_chat"]), 0)


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

    def test_an_answer_asking_which_option_offers_them(self):
        """The payments-api answer: options as choices, the explanation under why, the question in sight."""
        chat = cm.answer_message(_case("answer-offers-its-options")["input"]["markdown"])
        self.assertEqual(chat["headline"], "The payments-api workload is CrashLooping.")
        self.assertEqual(
            chat["choices"],
            [{"text": "Fix the command", "primary": True}, {"text": "Increase the memory limit"},
             {"text": "Change the workload image and logic"}],
        )
        self.assertTrue(chat["detail"].startswith("Due to an OOMKill (exit code 137)"))
        self.assertTrue(chat["detail"].endswith("Which of these options would you like to proceed with?"))
        self.assertIn("1. **Fix the command** — If the container's purpose", chat["detail"])
        self.assertNotIn("## Root Cause", chat["detail"])
        self.assertEqual(chat["fold"]["title"], "why")
        self.assertTrue(chat["fold"]["text"].startswith("## Root Cause"))
        self.assertIn("## Evidence", chat["fold"]["text"])
        self.assertIn("`sleep infinity`", chat["fold"]["text"], "the options' full text stays under why")

    def test_choices_the_gateway_would_read_as_commands_keep_the_plain_layout(self):
        chat = cm.answer_message("Two ways.\n\n1. **stop**\n2. **Keep going**\n\nWhich do you want?")
        self.assertNotIn("choices", chat)
        self.assertEqual(chat["detail"], "1. **stop**\n2. **Keep going**\n\nWhich do you want?")

    def test_a_detail_keeps_its_markdown(self):
        md = _case("answer-markdown-detail")["input"]["markdown"]
        chat = cm.answer_message(md)
        self.assertEqual(chat["headline"], "checkout-gateway restarts on an OOM.")
        self.assertIn("## Evidence\n- Last state:", chat["detail"])
        self.assertIn("  ```yaml\n", chat["detail"])
        self.assertNotIn("choices", chat)


class NextStepTest(unittest.TestCase):
    def test_mock_07(self):
        """The declared step is the one choice, not primary, and its line is not in the detail."""
        chat = cm.answer_message(_case("answer-declares-next-step")["input"]["markdown"])
        self.assertEqual(chat["choices"], [{"text": "upgrade plan for seeded-c", "next": True}])
        self.assertTrue(chat["detail"].endswith("are current."))

    def test_a_long_rest_folds_and_keeps_the_step(self):
        long = "X" + "x" * cm.DETAIL_MAX
        chat = cm.answer_message(f"Behind. {long}\n\n[next: upgrade plan for seeded-c]")
        self.assertEqual((chat["fold"]["text"], chat["choices"]),
                         (long, [{"text": "upgrade plan for seeded-c", "next": True}]))

    def test_options_win_over_a_step(self):
        chat = cm.answer_message("Two ways.\n\n1. **Raise it**\n2. **Roll back**\n\nWhich do you want?\n\n[next: raise it]")
        self.assertEqual(chat["choices"], [{"text": "Raise it"}, {"text": "Roll back"}])

    def test_a_command_word_step_is_no_button_and_no_text(self):
        self.assertEqual(cm.answer_message("Running.\n\n[next: stop]"), {"schema": cm.SCHEMA, "headline": "Running."})

    def test_a_refused_step_is_not_laid_out_as_text(self):
        chat = cm.answer_message("seeded-c is behind.\n\n[next: a]\n[next: b]")
        self.assertNotIn("choices", chat)
        self.assertNotIn("[next:", str(chat))

    def test_a_failure_reply_offers_its_step_in_place_of_try_again(self):
        self.assertEqual(cm.failure_message("I couldn't. Check it there?", True, "check seeded-a")["choices"],
                         [{"text": "check seeded-a", "next": True}])
        self.assertEqual(cm.failure_message("I couldn't. Retry?", True)["choices"], [{"text": cm.OFFER_LABEL}])
        self.assertNotIn("choices", cm.failure_message("I couldn't. Retry?", False, "check seeded-a"))


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

    def test_an_option_with_inner_space_keeps_its_buttons(self):
        chat = cm.needs_you_message("Which one?\n- Scale  to 3\n- Leave\tit", "t_1")
        self.assertEqual([c["text"] for c in chat["choices"]], ["Scale to 3", "Leave it"])
        self.assertEqual(chat["choices"][0]["reply"], "Scale to 3\n\n(Clicked on the question from card t_1.)")

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

    def test_a_link_whose_label_names_another_host_is_dropped(self):
        # The validator refuses the whole message for it; the composer drops the one link.
        chat = cm.report_message("x", links=[
            ("https://grafana.example/d/x", "https://console.cloud.google.com/a"),
            ("**https**://console.cloud.google.com", "https://console.cloud.google.com/b"),
            ("Read https://console.CLOUD.google.com.", "https://console.cloud.google.com:443/c"),
            ("https://good.example@evil.example", "https://evil.example/d"),
            ("Dashboard", "https://grafana.example/e"),
        ])
        self.assertEqual([link["url"] for link in chat["links"]], [
            "https://console.cloud.google.com/b", "https://console.cloud.google.com:443/c", "https://grafana.example/e",
        ])

    def test_the_host_check_reads_the_label_as_clipped(self):
        # Clipped, the label's host ends mid-name, which the validator reads as another host.
        url = "https://monitoring-dashboards.observability.internal.example.com/d/abc"
        chat = cm.report_message("x", links=[(f"Grafana restarts dashboard (prod): {url}", url)])
        self.assertNotIn("links", chat)

    def test_failure_offers_try_again_only_when_asked(self):
        self.assertEqual(cm.failure_message("It failed. Retry?", True)["choices"], [{"text": "Try again"}])
        self.assertNotIn("choices", cm.failure_message("It failed. Retry?", False))
        self.assertIsNone(cm.failure_message("It failed. X" + "x" * cm.DETAIL_MAX, True))


class PrOpenedTest(unittest.TestCase):
    def test_a_merge_request_reads_as_one(self):
        mr = "https://gitlab.com/platform/infra/-/merge_requests/12"
        chat = cm.pr_message(mr, "infra", "12", f"I opened a merge request: {mr}")
        self.assertEqual(chat["headline"], "I opened merge request !12 in infra. It's yours to review.")
        self.assertEqual(chat["note"], "I opened an MR !12")
        self.assertEqual(chat["links"], [
            {"text": "Open MR ↗", "url": mr, "primary": True},
            {"text": "Files changed ↗", "url": mr + "/diffs"},
        ])
        self.assertTrue(cm.pr_text(mr, "infra", "12", "x").startswith("I opened merge request !12 in infra."))

    def test_a_pull_request_keeps_its_layout(self):
        pr = "https://github.com/acme/fleet-config/pull/412"
        chat = cm.pr_message(pr, "fleet-config", "412", f"Opened {pr} for review.")
        self.assertEqual(chat["headline"], "I opened PR #412 in fleet-config. It's yours to review.")
        self.assertEqual(chat["note"], "Opened PR #412 for review.")
        self.assertEqual([link["text"] for link in chat["links"]], ["Open PR ↗", "Files changed ↗"])
        self.assertEqual(chat["links"][1]["url"], pr + "/files")


class MarkTest(unittest.TestCase):
    def test_tokens_only(self):
        self.assertEqual(cm.mark("fleet", "done"), {"ask": "fleet", "settle": "done"})
        self.assertEqual(cm.mark("Fleet!", "maybe"), {})


if __name__ == "__main__":
    unittest.main()
