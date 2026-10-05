"""Host tests for the KAGE_SLACK_UX answer-fold patch. No Hermes install required.

Run: python3 -m pytest deploy/docker/patches/test_slack_ux_answer.py

The notifier fixture is ``_send_event`` as ``apply_kanban_progress_lines`` and
``apply_slack_ux_incident`` leave it, which is where the anchor lives. The
runtime is driven with a stub of the SlackAdapter surface it reaches.
"""

import ast
import asyncio
import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[2] / "agents" / "platform" / "scripts"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SCRIPTS))

import apply_slack_ux_answer as applier
import apply_slack_ux_incident as incident_applier
import kanban_notifier
import slack_presenter as presenter
import slack_ux_answer as runtime
import slack_ux_incident as incident
import verify_slack_ux_answer
import verify_slack_ux_incident
from test_slack_ux_incident import NOTIFIER

CHANNEL = "C0KAGE"
THREAD_TS = "1700000000.000100"
POSTED_TS = "1700000000.000200"
METADATA = {"thread_id": THREAD_TS}
HEADLINE = "Checkout is slow because the payments pool is at its limit."
WHY = "The pool has 4 nodes, all above 90% CPU."
STEPS = "- Scale the pool to 6 nodes.\n- Then watch p99 latency."
ANSWER = f"{HEADLINE} {WHY}\n\n{STEPS}"
REST = f"{WHY}\n\n{STEPS}"


class _Client:
    def __init__(self, log, fail=False):
        self.log = log
        self.fail = fail

    async def chat_postMessage(self, **kwargs):
        if self.fail:
            raise RuntimeError("invalid_blocks")
        self.log.append(("chat_postMessage", kwargs))
        return {"ts": POSTED_TS}


def _slack_unfurl_kwargs(extra):
    """Found through ``_Adapter``'s module, the way the runtime finds the Slack adapter module's."""
    return {"unfurl_links": False}


class _Adapter:
    """The SlackAdapter surface the folder reaches."""

    def __init__(self, extra=None, fail_post=False, blocked=False):
        self.log = []
        self.extra = {"rich_blocks": True} if extra is None else extra
        self.fail_post = fail_post
        self.blocked = blocked
        self._bot_message_ts = set()
        self.config = SimpleNamespace(extra=self.extra)
        self.trimmed = 0

    def _extra_flag(self, key):
        return bool(self.extra.get(key))

    def _outbound_blocked(self, chat_id, label):
        return SimpleNamespace(success=False, error="blocked") if self.blocked else None

    async def _dm_target(self, chat_id, metadata):
        return chat_id

    def _metadata_team_id(self, metadata):
        return "T0KAGE"

    def _resolve_thread_ts(self, reply_to, metadata):
        return (metadata or {}).get("thread_id") or reply_to

    def _workspace_message_marker(self, team_id, ts):
        return f"{team_id}:{ts}"

    def _client_for(self, chat_id, metadata):
        return _Client(self.log, self.fail_post)

    def format_message(self, content):
        return f"mrkdwn({content})"

    def _append_feedback_block(self, blocks):
        return [*blocks, {"type": "actions"}] if self._extra_flag("feedback_buttons") else blocks

    async def stop_typing(self, chat_id, metadata=None):
        self.log.append(("stop_typing", chat_id))

    def _trim_bot_message_timestamps(self):
        self.trimmed += 1

    async def send(self, chat_id, content, metadata=None):
        self.log.append(("send", chat_id, content, metadata))
        return SimpleNamespace(success=True, message_id="1700000000.000900")


def _render_blocks(markdown, mrkdwn_fn=None):
    """Stands in for block_kit.render_blocks: one section per paragraph, a ``|`` paragraph a table."""
    fmt = mrkdwn_fn or (lambda s: s)
    return [
        {"type": "table"} if p.startswith("|") else {"type": "section", "text": {"type": "mrkdwn", "text": fmt(p)}}
        for p in markdown.split("\n\n") if p
    ]


BLOCK_KIT = SimpleNamespace(render_blocks=_render_blocks, sanitize_blocks=lambda blocks: blocks)


class SplitTest(unittest.TestCase):
    def test_the_first_sentence_leads_and_the_rest_follows(self):
        self.assertEqual(runtime.split(ANSWER), ([(HEADLINE, False)], REST))

    def test_the_headline_is_split_answers(self):
        headline, _rest = runtime.split(ANSWER)
        self.assertEqual("".join(text for text, _code in headline), presenter.split_answer(ANSWER)[0])

    def test_a_bold_lead_folds_without_its_markers(self):
        # The shape the worker report stanza asks for (kanban_report_format.py).
        for answer in ("**No pods are failing.** All 12 are Running.", "**No pods are failing.**\nAll 12 are Running."):
            with self.subTest(answer):
                self.assertEqual(runtime.split(answer), ([("No pods are failing.", False)], "All 12 are Running."))

    def test_a_bold_run_cut_at_the_lead_reopens_in_the_rest(self):
        self.assertEqual(
            runtime.split("**Two pods fail. Both exit 137.** Raise the limit."),
            ([("Two pods fail.", False)], "**Both exit 137.** Raise the limit."),
        )

    def test_a_bold_run_cut_at_a_wrap_reopens_on_the_next_line(self):
        self.assertEqual(
            runtime.split("**Two pods fail.\nBoth exit 137.** Raise the limit."),
            ([("Two pods fail.", False)], "**Both exit 137.** Raise the limit."),
        )

    def test_a_one_word_bold_answer_before_a_colon_folds(self):
        self.assertEqual(
            runtime.split("**No**: all 12 are Running. Two restarted today."),
            ([("No: all 12 are Running.", False)], "Two restarted today."),
        )

    def test_a_later_bold_label_on_the_line_does_not_refuse_the_lead(self):
        answers = {
            "after a bold sentence": (
                "**All 3 nodes are Ready.** Load \u2014 **cpu**: 40%, **mem**: 60%.",
                ([("All 3 nodes are Ready.", False)], "Load \u2014 **cpu**: 40%, **mem**: 60%."),
            ),
            "after a one-word answer": (
                "**No**: all 12 are Running. **Two** \u2014 restarted.",
                ([("No: all 12 are Running.", False)], "**Two** \u2014 restarted."),
            ),
        }
        for what, (answer, expected) in answers.items():
            with self.subTest(what):
                self.assertEqual(runtime.split(answer), expected)

    def test_a_lead_opening_on_a_triple_backtick_span_folds(self):
        self.assertEqual(
            runtime.split("```kubectl``` is deprecated. Use the plugin."),
            ([("kubectl", True), (" is deprecated.", False)], "Use the plugin."),
        )

    def test_a_code_span_in_the_lead_stays_code(self):
        headline, rest = runtime.split("**One pod is failing: `web-1` is CrashLoopBackOff.** It exits 137.")
        self.assertEqual(
            headline, [("One pod is failing: ", False), ("web-1", True), (" is CrashLoopBackOff.", False)]
        )
        self.assertEqual(rest, "It exits 137.")
        self.assertEqual(
            runtime.blocks_answer(headline, [])[0]["elements"][0]["elements"][1],
            {"type": "text", "text": "web-1", "style": {"bold": True, "code": True}},
        )

    def test_the_rest_keeps_its_line_breaks(self):
        answers = {
            "a table": ("Three nodes are hot.\n", "| node | cpu |\n|---|---|\n| a | 95% |"),
            "a quote": ("The pool is full. ", "Two lines say so.\n> quoted line\n> another"),
            "labels": ("Pod restarted.\n", "Node: a\nCause: OOM"),
        }
        for what, (lead, rest) in answers.items():
            with self.subTest(what):
                self.assertEqual(runtime.split(lead + rest), ([(lead.strip(), False)], rest))

    def test_answers_the_headline_cannot_carry_whole_are_refused(self):
        refused = {
            "a heading": "## Summary\n\nCheckout is slow. The pool is full.",
            "a quote": "> Checkout is slow.\n\nThe pool is full.",
            "a table row": "| pod | state |\n| --- | --- |\n\nAll Running.",
            "a list item": "- Checkout is slow. The pool is full.",
            "a numbered item": "1. Checkout is slow. The pool is full.",
            "a code fence": "```\nkubectl get pods\n```\n\nThat lists them.",
            "a link": "See [the runbook](https://example.com/runbook). It covers this.",
            "a bare url": "The dashboard is https://example.com/d. It shows the spike.",
            "a mention": "<@U123> owns this pool. Ask them first.",
            "a wrapped sentence": "Checkout is slow because the\npayments pool is full. Scale it.",
            "a bold label alone": "**No pods are failing**: all 12 are Running.",
            "a bold label and more": "**No pods are failing**: all 12 are Running. Two restarted today.",
            "a bold label and a dash": "**Memory check** \u2014 no node is under pressure. All 3 checked.",
            "an overlong sentence": ("word " * 40).strip() + ". Then more.",
            "a single sentence": "Checkout is healthy.",
            "nothing": "",
        }
        for what, answer in refused.items():
            with self.subTest(what), self.assertLogs(runtime.logger) as logs:
                self.assertIsNone(runtime.split(answer))
            self.assertIn("fold is refused", logs.output[0])

    def test_an_answer_past_the_fold_limit_is_refused(self):
        long = HEADLINE + " " + "x" * runtime.FOLD_TEXT_MAX
        with self.assertLogs(runtime.logger):
            self.assertIsNone(runtime.split(long))


class TrailingQuestionTest(unittest.TestCase):
    def test_a_closing_question_is_split_off_as_written(self):
        cases = {
            "after a sentence": ("The pool is full. Want me to scale it?", ("The pool is full.", "Want me to scale it?")),
            "on its own line": (f"{REST}\n\nShould I open a PR?", (REST, "Should I open a PR?")),
            "alone": ("Want me to watch it?", ("", "Want me to watch it?")),
            "soft-wrapped": (
                f"{REST}\n\nWant me to scale the pool\nto 6 nodes?",
                (REST, "Want me to scale the pool to 6 nodes?"),
            ),
            "wrapped after a sentence": (
                "All above 90% CPU. Want me to scale\nthe pool?",
                ("All above 90% CPU.", "Want me to scale the pool?"),
            ),
            "after a line that ends": ("The pool is full.\nWant me to scale it?", ("The pool is full.", "Want me to scale it?")),
            "holding i.e.": (
                f"{REST}\n\nWant me to scale it, i.e. add two nodes?",
                (REST, "Want me to scale it, i.e. add two nodes?"),
            ),
            "holding approx. before a number": (
                "The pool is full. Want me to scale it to approx. 6 nodes?",
                ("The pool is full.", "Want me to scale it to approx. 6 nodes?"),
            ),
            "holding pod no. 3": ("It restarted. Want me to watch pod no. 3?", ("It restarted.", "Want me to watch pod no. 3?")),
            "a stop in a code span": (
                "It restarted. Want me to roll back to `v1.2`?",
                ("It restarted.", "Want me to roll back to `v1.2`?"),
            ),
            "after a bold sentence": ("**Done.** Open a PR?", ("**Done.**", "Open a PR?")),
            "under label lines": (
                "Node: a\nCause: OOM\nWant me to restart it?",
                ("Node: a\nCause: OOM", "Want me to restart it?"),
            ),
            "under a count": ("Replicas: `3/3`\nWant me to watch it?", ("Replicas: `3/3`", "Want me to watch it?")),
            "wrapped after e.g.": (
                f"{REST}\n\nWant me to scale it, e.g.\nadd two nodes?",
                (REST, "Want me to scale it, e.g. add two nodes?"),
            ),
        }
        for what, (rest, expected) in cases.items():
            with self.subTest(what):
                self.assertEqual(runtime.trailing_question(rest), expected)

    def test_anything_else_stays_in_the_fold(self):
        for rest in (
            REST,
            "- Is it the pool?",
            "> Why did it fail?",
            "Did it fail? It did.",
            "- Is it the pool or\n  the node?",
            "> Why did it\n> fail?",
        ):
            with self.subTest(rest):
                self.assertEqual(runtime.trailing_question(rest), (rest, ""))


class AdapterForTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"KAGE_SLACK_UX": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def wrap(self, adapter, platform="slack", kind="completed", sub=None):
        sub = {"chat_id": CHANNEL, "thread_id": THREAD_TS} if sub is None else sub
        return runtime.adapter_for(adapter, platform, SimpleNamespace(kind=kind), SimpleNamespace(), sub)

    def test_a_completed_card_on_slack_is_wrapped(self):
        adapter = _Adapter()
        self.assertIsNot(self.wrap(adapter), adapter)

    def test_everything_else_keeps_the_adapter(self):
        cases = {
            "another platform": {"platform": "google_chat"},
            "another kind": {"kind": "gave_up"},
            "no chat": {"sub": {"thread_id": THREAD_TS}},
            "no subscription": {"sub": "not a dict"},
        }
        for what, kwargs in cases.items():
            adapter = _Adapter()
            with self.subTest(what):
                self.assertIs(self.wrap(adapter, **kwargs), adapter)

    def test_flag_off_keeps_the_adapter(self):
        adapter = _Adapter()
        with mock.patch.dict(os.environ, {"KAGE_SLACK_UX": "0"}):
            self.assertIs(self.wrap(adapter), adapter)

    def test_an_adapter_not_rendering_rich_blocks_is_kept(self):
        for extra in ({}, {"rich_blocks": True, "markdown_blocks": True}):
            adapter = _Adapter(extra=extra)
            with self.subTest(extra):
                self.assertIs(self.wrap(adapter), adapter)
        bare = SimpleNamespace(send=None)
        self.assertIs(self.wrap(bare), bare)


class SendTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"KAGE_SLACK_UX": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.block_kit = mock.patch.object(runtime, "_load_block_kit", return_value=BLOCK_KIT)
        self.block_kit.start()
        self.addCleanup(self.block_kit.stop)
        modules = mock.patch.dict(
            sys.modules, {"gateway": types.ModuleType("gateway"), "gateway.kanban_notifier": kanban_notifier}
        )
        modules.start()
        self.addCleanup(modules.stop)

    def send(self, adapter, content=ANSWER, chat_id=CHANNEL):
        wrapped = runtime.adapter_for(
            adapter, "slack", SimpleNamespace(kind="completed"), SimpleNamespace(), {"chat_id": CHANNEL}
        )
        return asyncio.run(wrapped.send(chat_id, content, metadata=METADATA))

    def test_the_answer_posts_once_bold_then_folded(self):
        adapter = _Adapter()
        result = self.send(adapter)
        self.assertEqual([entry[0] for entry in adapter.log], ["chat_postMessage", "stop_typing"])
        post = adapter.log[0][1]
        self.assertEqual(post["channel"], CHANNEL)
        self.assertEqual(post["thread_ts"], THREAD_TS)
        self.assertTrue(post["mrkdwn"])
        self.assertNotIn("reply_broadcast", post)
        self.assertEqual(post["text"], f"mrkdwn({ANSWER})")
        headline, fold = post["blocks"]
        self.assertEqual(
            headline["elements"][0]["elements"], [{"type": "text", "text": HEADLINE, "style": {"bold": True}}]
        )
        self.assertEqual(fold["type"], "container")
        self.assertEqual(fold["title"], {"type": "plain_text", "text": runtime.FOLD_TITLE})
        self.assertTrue(fold["is_collapsible"] and fold["default_collapsed"])
        self.assertEqual(fold["child_blocks"], _render_blocks(REST, adapter.format_message))
        self.assertTrue(result.success)
        self.assertEqual(result.message_id, POSTED_TS)
        self.assertEqual(adapter._bot_message_ts, {f"T0KAGE:{POSTED_TS}", f"T0KAGE:{THREAD_TS}"})
        self.assertEqual(adapter.trimmed, 1)

    def test_the_post_carries_upstreams_broadcast_and_feedback_settings(self):
        adapter = _Adapter(extra={"rich_blocks": True, "reply_broadcast": True, "feedback_buttons": True})
        self.send(adapter)
        post = adapter.log[0][1]
        self.assertTrue(post["reply_broadcast"])
        self.assertIs(post["unfurl_links"], False)
        self.assertEqual([block["type"] for block in post["blocks"]], ["rich_text", "container", "actions"])

    def test_a_closing_question_posts_after_the_fold(self):
        question = "Want me to scale the pool to 6 nodes?"
        adapter = _Adapter()
        self.send(adapter, f"{ANSWER}\n\n{question}")
        post = adapter.log[0][1]
        self.assertEqual(post["text"], f"mrkdwn({ANSWER}\n\n{question})")
        _headline, fold, after = post["blocks"]
        self.assertEqual(fold["child_blocks"], _render_blocks(REST, adapter.format_message))
        self.assertEqual(after, _render_blocks(question, adapter.format_message)[0])

    def test_an_answer_that_is_only_a_lead_and_a_question_has_no_fold(self):
        adapter = _Adapter()
        self.send(adapter, f"{HEADLINE} Want me to watch it?")
        self.assertEqual([block["type"] for block in adapter.log[0][1]["blocks"]], ["rich_text", "section"])

    def test_a_top_level_chat_posts_without_a_thread(self):
        adapter = _Adapter()
        wrapped = runtime.adapter_for(
            adapter, "slack", SimpleNamespace(kind="completed"), SimpleNamespace(), {"chat_id": CHANNEL}
        )
        asyncio.run(wrapped.send(CHANNEL, ANSWER, metadata={}))
        self.assertNotIn("thread_ts", adapter.log[0][1])
        self.assertEqual([entry[0] for entry in adapter.log], ["chat_postMessage"])

    def test_a_refused_answer_takes_the_upstream_send(self):
        adapter = _Adapter()
        with self.assertLogs(runtime.logger):
            self.send(adapter, content="Checkout is healthy.")
        self.assertEqual(adapter.log, [("send", CHANNEL, "Checkout is healthy.", METADATA)])

    def test_a_table_in_the_rest_takes_the_upstream_send(self):
        adapter = _Adapter()
        answer = f"{HEADLINE}\n\n| node | cpu |\n| --- | --- |"
        with self.assertLogs(runtime.logger) as logs:
            self.send(adapter, content=answer)
        self.assertEqual([entry[0] for entry in adapter.log], ["send"])
        self.assertIn("table", logs.output[0])

    def test_a_report_with_options_takes_the_upstream_send_whatever_it_opens_on(self):
        adapter = _Adapter()
        report = (
            "**Checkout is down because the pool is exhausted.** It has 4 nodes.\n\n"
            "## What to do\n\n- **Option A (scale):** add two nodes.\n- **To authorize:** reply 'apply'"
        )
        self.assertTrue(kanban_notifier.actionable_report(report))
        with self.assertLogs(runtime.logger) as logs:
            self.send(adapter, content=report)
        self.assertEqual(adapter.log, [("send", CHANNEL, report, METADATA)])
        self.assertIn("options", logs.output[0])

    def test_a_failed_post_takes_the_upstream_send(self):
        adapter = _Adapter(fail_post=True)
        with self.assertLogs(runtime.logger, "WARNING"):
            result = self.send(adapter)
        self.assertEqual(adapter.log, [("send", CHANNEL, ANSWER, METADATA)])
        self.assertEqual(result.message_id, "1700000000.000900")

    def test_a_blocked_chat_takes_the_upstream_send(self):
        adapter = _Adapter(blocked=True)
        self.send(adapter)
        self.assertEqual([entry[0] for entry in adapter.log], ["send"])

    def test_another_chat_takes_the_upstream_send(self):
        adapter = _Adapter()
        self.send(adapter, chat_id="C0OTHER")
        self.assertEqual(adapter.log, [("send", "C0OTHER", ANSWER, METADATA)])

    def test_a_failed_alert_edit_falls_back_to_the_upstream_send(self):
        # The answer folder sits inside the incident editor; a triage report opens with a heading.
        adapter = _Adapter()
        report = "## What's wrong\n\nCheckout is slow."
        folder = runtime.adapter_for(
            adapter, "slack", SimpleNamespace(kind="completed"), SimpleNamespace(), {"chat_id": CHANNEL}
        )
        editor = incident._AlertEditor(folder, CHANNEL, THREAD_TS, {}, [], "text")
        with self.assertLogs(incident.logger, "WARNING"), self.assertLogs(runtime.logger):
            asyncio.run(editor.send(CHANNEL, report, metadata=METADATA))
        incident._edited.clear()
        self.assertEqual(adapter.log, [("send", CHANNEL, report, METADATA)])


class ApplierTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "gateway").mkdir()
        (self.root / applier.RELATIVE).write_text(NOTIFIER)

    def tearDown(self):
        self.tmp.cleanup()

    def run_send(self, source, adapter):
        stub = types.ModuleType("gateway")
        stub.slack_ux_incident = incident
        stub.slack_ux_answer = runtime
        modules = {"gateway": stub, "gateway.slack_ux_incident": incident, "gateway.slack_ux_answer": runtime}
        with mock.patch.dict(sys.modules, modules):
            namespace = {}
            exec(compile(source, "notifier", "exec"), namespace)  # noqa: S102 — the fixture under test
        watcher = namespace["Watcher"](adapter, SimpleNamespace(result=ANSWER), {"chat_id": CHANNEL})
        asyncio.run(watcher._send_event(SimpleNamespace(kind="completed"), "msg"))
        return namespace["calls"]

    def test_the_answer_adapter_sits_inside_the_incident_one(self):
        incident_applier.apply(self.root)
        applier.apply(self.root)
        patched = (self.root / applier.RELATIVE).read_text()
        self.assertIn(applier.BUILD_MARKER, patched)
        verify_slack_ux_incident.check_notifier(self.root)
        verify_slack_ux_answer.check_notifier(self.root)

    def test_flag_off_the_patched_notifier_delivers_through_its_own_adapter(self):
        incident_applier.apply(self.root)
        applier.apply(self.root)
        patched = (self.root / applier.RELATIVE).read_text()
        adapter = _Adapter()
        with mock.patch.dict(os.environ, {"KAGE_SLACK_UX": "0"}):
            self.assertEqual(self.run_send(patched, adapter), [adapter])

    def test_flag_on_the_patched_notifier_delivers_through_the_folder(self):
        incident_applier.apply(self.root)
        applier.apply(self.root)
        patched = (self.root / applier.RELATIVE).read_text()
        adapter = _Adapter()
        with mock.patch.dict(os.environ, {"KAGE_SLACK_UX": "1"}):
            [delivered] = self.run_send(patched, adapter)
        self.assertIsInstance(delivered, runtime._AnswerFolder)

    def test_the_progress_lines_verifier_still_finds_the_runner_arguments(self):
        # verify_kanban_progress_lines runs against /opt/hermes at import, so its pattern is read from source.
        source = (HERE / "verify_kanban_progress_lines.py").read_text()
        [node] = [
            n.value for n in ast.parse(source).body
            if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "RUNNER_ARGS" for t in n.targets)
        ]
        runner_args = eval(compile(ast.Expression(node), "RUNNER_ARGS", "eval"), {"re": re})  # noqa: S307
        incident_applier.apply(self.root)
        applier.apply(self.root)
        self.assertRegex((self.root / applier.RELATIVE).read_text(), runner_args)

    def test_it_needs_the_incident_patch_first(self):
        with self.assertRaises(SystemExit):
            applier.apply(self.root)

    def test_the_answer_verifier_rejects_the_incident_patch_alone(self):
        incident_applier.apply(self.root)
        with self.assertRaises(SystemExit):
            verify_slack_ux_answer.check_notifier(self.root)

    def test_a_second_run_refuses(self):
        incident_applier.apply(self.root)
        applier.apply(self.root)
        with self.assertRaises(SystemExit):
            applier.apply(self.root)


#: The SlackAdapter surface slack_ux_answer calls, with upstream's signatures.
ADAPTER_SOURCE = """
def _slack_unfurl_kwargs(extra):
    return {}


class SlackAdapter:
    def __init__(self):
        self._bot_message_ts = set()

    def _extra_flag(self, key, default=False): ...
    def _outbound_blocked(self, chat_id, what): ...
    async def _dm_target(self, chat_id, metadata): ...
    @staticmethod
    def _metadata_team_id(metadata): ...
    def _resolve_thread_ts(self, reply_to=None, metadata=None): ...
    def _client_for(self, chat_id, metadata): ...
    @staticmethod
    def _workspace_message_marker(team_id, message_id): ...
    def format_message(self, content): ...
    def _append_feedback_block(self, blocks): ...
    async def stop_typing(self, chat_id, metadata=None): ...
    def _trim_bot_message_timestamps(self): ...
"""


class AdapterCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / verify_slack_ux_answer.ADAPTER).parent.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, source):
        (self.root / verify_slack_ux_answer.ADAPTER).write_text(source)
        verify_slack_ux_answer.check_adapter(self.root)

    def test_upstream_signatures_pass(self):
        self.check(ADAPTER_SOURCE)

    def test_a_signature_that_cannot_take_the_call_fails(self):
        drifts = {
            "stop_typing": ("stop_typing(self, chat_id, metadata=None)", "stop_typing(self, chat_id, meta=None)"),
            "_client_for": ("_client_for(self, chat_id, metadata)", "_client_for(self, chat_id, metadata, team)"),
            "_workspace_message_marker": ("@staticmethod\n    def _workspace_message_marker(team_id",
                                          "def _workspace_message_marker(team_id"),
            "_slack_unfurl_kwargs": ("_slack_unfurl_kwargs(extra)", "_slack_unfurl_kwargs()"),
        }
        for name, (old, new) in drifts.items():
            with self.subTest(name), self.assertRaisesRegex(SystemExit, f"{name}.* no longer accepts"):
                self.assertIn(old, ADAPTER_SOURCE)
                self.check(ADAPTER_SOURCE.replace(old, new))

    def test_a_member_turned_property_fails(self):
        source = ADAPTER_SOURCE.replace("    def format_message", "    @property\n    def format_message")
        with self.assertRaisesRegex(SystemExit, "format_message is now decorated"):
            self.check(source)


if __name__ == "__main__":
    unittest.main()
