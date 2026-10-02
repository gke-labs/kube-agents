"""Host tests for the KAGE_SLACK_UX incident-triage patch. No Hermes install required.

Run: python3 -m pytest deploy/docker/patches/test_slack_ux_incident.py

The notifier fixture is ``_send_event`` as ``apply_kanban_progress_lines``
leaves it, which is where the anchor lives. With the flag off the patched
call site hands ``_progress_deliver`` the notifier's own adapter, which is the
flag-off identity for this surface. The runtime is driven with a stub adapter
and a routing database written the way ``session_kv_server`` writes it.
"""

import asyncio
import json
import os
import re
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[2] / "agents" / "platform" / "scripts"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SCRIPTS))

import slack_presenter as presenter

import apply_slack_ux_incident as applier
import kanban_notifier
import slack_ux_clicks as clicks
import slack_ux_incident as runtime
from apply_kanban_progress_lines import SEND_PATCHED
import kanban_progress_lines

CHANNEL = "C0KAGE"
ALERT_TS = "1700000000.000100"
LOGS_URL = "https://console.cloud.google.com/logs/query;query=x"
WORKLOADS_URL = "https://console.cloud.google.com/kubernetes/workload/overview"

REPORT = (
    "## What's wrong\n\n"
    "payments-api in seeded-debug keeps crashing: secret payments-db-creds has been missing since 14:02. "
    "Nothing else changed.\n\n"
    "## Why\n\n"
    "- The container exits reading `payments-db-creds`, which **no longer exists**.\n\n"
    "## What to do\n\n"
    "- **Option A (Roll back to 14:02):** Revert the Deployment to revision 7.\n"
    "- **Option B (Restore the secret):** Recreate payments-db-creds from the GitOps repo.\n"
    "- ✅ **Recommended: Option B** — it fixes the cause rather than hiding it.\n"
    "- **To authorize:** reply **'apply'** to open a GitOps Pull Request with the recommended fix, "
    "or name one directly with **'apply Option A'** / **'apply Option B'**.\n\n"
    f"🔗 [GKE Workloads]({WORKLOADS_URL}) | [Cloud Logs]({LOGS_URL})\n"
)

SINGLE = (
    "## What's wrong\n\nThe quota for n2 CPUs in us-central1 is exhausted.\n\n"
    "## Why\n\n- Scale-up fails with QUOTA_EXCEEDED.\n\n"
    "## What to do\n\n"
    "- **Proposed fix (Lower the node pool max):** Set max nodes to 4 in the GitOps repo.\n"
    "- **To authorize:** reply **'apply'** to open a GitOps Pull Request with this fix.\n"
)

NOTIFIER = '''\
"""Fixture standing in for gateway/kanban_watchers_notifier.py after apply_kanban_progress_lines."""
calls = []


async def _progress_deliver(runner, adapter, sub, kind, ev, msg, metadata, header, board, title=None):
    calls.append(adapter)


class Watcher:
    def __init__(self, adapter, task, sub):
        self.runner = object()
        self.adapter = adapter
        self.task = task
        self.sub = sub
        self.platform_str = "slack"
        self.progress_header = ""
        self.board_slug = "default"
        self.title = "t1"

    async def _send_event(self, ev, msg):
        sub, adapter = self.sub, self.adapter
        metadata = {}
''' + SEND_PATCHED


class _Client:
    def __init__(self, log, fail=False):
        self.log = log
        self.fail = fail

    async def chat_update(self, **kwargs):
        if self.fail:
            raise RuntimeError("message_not_found")
        self.log.append(("chat_update", kwargs))


class _Adapter:
    def __init__(self, fail_update=False):
        self.log = []
        self.fail_update = fail_update
        self.name = "slack-adapter"

    def _client_for(self, chat_id, metadata):
        self.client_metadata = metadata
        return _Client(self.log, self.fail_update)

    async def send(self, chat_id, content, metadata=None):
        self.log.append(("send", chat_id, content, metadata))
        return SimpleNamespace(success=True, message_id="1700000000.000900")


def _texts(block):
    return "".join(e.get("text", "") for s in block["elements"] for e in s["elements"])


def _render_blocks(markdown, mrkdwn_fn=None):
    """Stands in for block_kit.render_blocks: one section per paragraph, a ``---`` paragraph a divider."""
    fmt = mrkdwn_fn or (lambda s: s)
    return [
        {"type": "divider"} if p == "---" else {"type": "section", "text": {"type": "mrkdwn", "text": fmt(p)}}
        for p in markdown.split("\n\n") if p
    ]


BLOCK_KIT = SimpleNamespace(render_blocks=_render_blocks, sanitize_blocks=lambda blocks: blocks)


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        runtime._edited.clear()
        self.block_kit = mock.patch.object(runtime, "_load_block_kit", return_value=BLOCK_KIT)
        self.block_kit.start()
        self.addCleanup(self.block_kit.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "session_kv.db")
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("CREATE TABLE session_metadata (session_id TEXT PRIMARY KEY, metadata TEXT)")
            conn.execute(
                "CREATE TABLE incidents (chat_id TEXT NOT NULL, thread_id TEXT NOT NULL, report TEXT, "
                "PRIMARY KEY (chat_id, thread_id))"
            )
            conn.commit()
        self.route("k8s-evt-0000abcd", "slack", CHANNEL, ALERT_TS)
        gateway = types.ModuleType("gateway")
        self.modules = mock.patch.dict(
            sys.modules, {"gateway": gateway, "gateway.kanban_notifier": kanban_notifier}
        )
        self.modules.start()
        self.env = mock.patch.dict(os.environ, {"KAGE_SLACK_UX": "1", "SESSION_KV_DB_PATH": self.db})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.modules.stop()
        self.tmp.cleanup()

    def route(self, session_id, platform, chat_id, thread_id):
        meta = {"platform": platform, "chat_id": chat_id, "thread_id": thread_id}
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT OR REPLACE INTO session_metadata VALUES (?, ?)", (session_id, json.dumps(meta)))
            conn.commit()

    def wrap(self, adapter, platform="slack", kind="completed", result=REPORT, thread=ALERT_TS):
        return runtime.adapter_for(
            adapter, platform, SimpleNamespace(kind=kind), SimpleNamespace(result=result),
            {"chat_id": CHANNEL, "thread_id": thread},
        )

    def deliver(self, adapter, result=REPORT):
        wrapped = self.wrap(adapter, result=result)
        self.assertIsNot(wrapped, adapter)
        res = asyncio.run(wrapped.send(CHANNEL, "the report", metadata={"thread_id": ALERT_TS}))
        return wrapped, res

    def test_flag_on_the_patched_notifier_edits_the_alert_through_the_real_deliver(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "gateway").mkdir()
            (root / applier.RELATIVE).write_text(NOTIFIER)
            applier.apply(root)
            patched = (root / applier.RELATIVE).read_text()
        namespace = {}
        with mock.patch.dict(sys.modules, {"gateway.slack_ux_incident": runtime}):
            exec(compile(patched, "notifier", "exec"), namespace)  # noqa: S102 — the fixture under test
        namespace["_progress_deliver"] = kanban_progress_lines.deliver
        adapter = _Adapter()
        sub = {"chat_id": CHANNEL, "thread_id": ALERT_TS, "task_id": "t1", "platform": "slack"}
        watcher = namespace["Watcher"](adapter, SimpleNamespace(result=REPORT), sub)
        watcher.runner = SimpleNamespace()  # the real deliver keeps its rolling-message map on it
        asyncio.run(watcher._send_event(SimpleNamespace(kind="completed", id=1), "the report"))
        self.assertEqual([e[0] for e in adapter.log], ["chat_update"])
        self.assertEqual(adapter.log[0][1]["ts"], ALERT_TS)

    def test_a_proposed_fix_beside_lettered_options_keeps_the_threaded_reply(self):
        report = REPORT.replace("- ✅ **Recommended", "- **Proposed fix (Restart):** Restart the pods.\n- ✅ **Recommended")
        self.assertIsNone(runtime.parse_triage(report))

    def test_a_proposed_fix_in_another_shape_beside_lettered_options_keeps_the_threaded_reply(self):
        for shape in (
            "- **Proposed fix** (Restart): Restart the pods.",
            "+ **Proposed fix (Restart)**: Restart the pods.",
            "1. **Proposed fix (Restart):** Restart the pods.",
            "**Proposed fix (Restart):** Restart the pods.",
            "- **proposed fix (Restart)**: Restart the pods.",
            "- **PROPOSED FIX (Restart)**: Restart the pods.",
            "- **Proposed fix:** Restart the pods.",
            "### Proposed fix\nRestart the pods.",
            "```\n- **Proposed fix (Restart):** Restart the pods.\n```",
        ):
            with self.subTest(shape=shape):
                report = REPORT.replace("- ✅ **Recommended", shape + "\n\n- ✅ **Recommended")
                self.assertIsNone(runtime.parse_triage(report))

    def test_two_proposed_fixes_without_options_keep_the_threaded_reply(self):
        for second in (
            "- **Proposed fix (Raise the quota):** Request 64 more CPUs.\n",
            "- **Proposed fix:** Request 64 more CPUs.\n",
            "- **proposed fix (Raise the quota):** Request 64 more CPUs.\n",
        ):
            with self.subTest(second=second):
                report = SINGLE.replace("- **To authorize:**", second + "- **To authorize:**")
                self.assertIsNone(runtime.parse_triage(report))

    def test_an_option_title_keeps_its_in_word_double_markers(self):
        triage = runtime.parse_triage(REPORT.replace("Roll back to 14:02", "Set DB__HOST and DB__PORT"))
        self.assertEqual(triage["choices"][0][0], "apply Option A: Set DB__HOST and DB__PORT")

    def test_link_labels_are_plain_and_reach_the_fallback_text(self):
        report = REPORT.replace("[GKE Workloads]", "[**GKE Workloads**]")
        triage = runtime.parse_triage(report)
        self.assertEqual(triage["links"][0][0], "GKE Workloads")
        text = runtime.fallback_text(triage)
        self.assertIn("|GKE Workloads>", text)
        self.assertIn("|Cloud Logs>", text)

    def test_flag_off_returns_the_adapter(self):
        adapter = _Adapter()
        with mock.patch.dict(os.environ, {"KAGE_SLACK_UX": "0"}):
            self.assertIs(self.wrap(adapter), adapter)

    def test_only_a_triage_report_for_an_open_slack_alert_is_taken(self):
        adapter = _Adapter()
        self.assertIs(self.wrap(adapter, platform="google_chat"), adapter)
        self.assertIs(self.wrap(adapter, kind="gave_up"), adapter)
        self.assertIs(self.wrap(adapter, result="All pods healthy; nothing to do."), adapter)
        self.assertIs(self.wrap(adapter, thread="1700000000.000555"), adapter)

    def test_a_cron_report_thread_is_never_edited(self):
        adapter = _Adapter()
        self.route("cron-daily-audit", "slack", CHANNEL, "1700000000.000777")
        self.assertIs(self.wrap(adapter, thread="1700000000.000777"), adapter)

    def test_a_thread_that_already_has_a_report_keeps_the_reply(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT INTO incidents VALUES (?, ?, ?)", (CHANNEL, ALERT_TS, "first"))
            conn.commit()
        adapter = _Adapter()
        self.assertIs(self.wrap(adapter), adapter)

    def test_the_alert_is_edited_into_the_triage(self):
        adapter = _Adapter()
        wrapped, res = self.deliver(adapter)
        self.assertEqual([e[0] for e in adapter.log], ["chat_update"])
        update = adapter.log[0][1]
        self.assertEqual((update["channel"], update["ts"]), (CHANNEL, ALERT_TS))
        self.assertTrue(res.success)
        self.assertEqual(res.message_id, ALERT_TS)
        self.assertEqual(wrapped.name, "slack-adapter")

        headline, actions, fold = update["blocks"]
        self.assertEqual(
            _texts(headline),
            "payments-api in seeded-debug keeps crashing: secret payments-db-creds has been missing since 14:02.",
        )
        buttons = actions["elements"]
        self.assertEqual(
            [b["text"]["text"] for b in buttons],
            ["apply Option A: Roll back to 14:02", "apply Option B: Restore the secret",
             "GKE Workloads ↗", "Cloud Logs ↗"],
        )
        self.assertEqual([b.get("style") for b in buttons], [None, "primary", None, None])
        self.assertEqual([b.get("url") for b in buttons[2:]], [WORKLOADS_URL, LOGS_URL])
        for button in buttons[:2]:
            self.assertRegex(button["action_id"], presenter.CHOICE_ACTION_ID_PATTERN)
        for button in buttons[2:]:
            self.assertRegex(button["action_id"], presenter.LINK_ACTION_ID_PATTERN)
        self.assertEqual(fold["type"], "container")
        self.assertEqual(fold["title"]["text"], "why · what each option does")
        self.assertIs(fold["is_collapsible"], True)
        self.assertIs(fold["default_collapsed"], True)
        self.assertIn("apply Option A", update["text"])
        # The whole report is in the text, the only place a thread read-back finds it.
        self.assertTrue(update["text"].endswith(REPORT.strip()))
        self.assertEqual(adapter.client_metadata, {"thread_id": ALERT_TS})

    def test_the_message_text_is_the_report_as_mrkdwn(self):
        adapter = _Adapter()
        adapter.format_message = lambda s: "fmt:" + s
        self.deliver(adapter)
        self.assertIn("\n\nfmt:" + REPORT.strip(), adapter.log[0][1]["text"])

    def test_the_fold_is_the_plugins_rendering_of_the_whole_report(self):
        adapter = _Adapter()
        adapter.format_message = lambda s: "fmt:" + s
        self.deliver(adapter)
        fold = adapter.log[0][1]["blocks"][-1]
        self.assertEqual(fold["child_blocks"], _render_blocks(REPORT.strip(), adapter.format_message))

    def test_a_fold_the_plugin_cannot_render_keeps_the_reply(self):
        adapter = _Adapter()
        empty = SimpleNamespace(render_blocks=lambda md, mrkdwn_fn=None: None, sanitize_blocks=lambda b: b)
        with mock.patch.object(runtime, "_load_block_kit", return_value=empty), self.assertLogs(runtime.logger) as logs:
            self.assertIs(self.wrap(adapter), adapter)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("the fold is refused: the Slack plugin rendered no blocks", logs.output[0])

    def test_a_fold_with_a_block_slack_has_not_kept_in_one_keeps_the_reply(self):
        adapter = _Adapter()
        with_table = SimpleNamespace(
            render_blocks=lambda md, mrkdwn_fn=None: [*_render_blocks(md, mrkdwn_fn), {"type": "table", "rows": []}],
            sanitize_blocks=lambda b: b,
        )
        with mock.patch.object(runtime, "_load_block_kit", return_value=with_table), self.assertLogs(runtime.logger) as logs:
            self.assertIs(self.wrap(adapter), adapter)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("the fold is refused: block types outside FOLD_CHILD_TYPES: table", logs.output[0])

    def test_the_fold_stops_at_a_closing_divider(self):
        adapter = _Adapter()
        self.deliver(adapter, result=REPORT + "\n---\n")
        fold = adapter.log[0][1]["blocks"][-1]
        self.assertEqual(fold["child_blocks"], _render_blocks(REPORT.strip()))

    def test_a_note_after_the_divider_is_in_the_text_not_the_fold(self):
        adapter = _Adapter()
        note = "Nothing was changed on the cluster."
        self.deliver(adapter, result=f"{REPORT}\n---\n\n{note}\n")
        update = adapter.log[0][1]
        self.assertEqual(update["blocks"][-1]["child_blocks"], _render_blocks(REPORT.strip()))
        self.assertTrue(update["text"].endswith(note))

    def test_a_divider_inside_a_code_fence_does_not_cut_the_fold(self):
        adapter = _Adapter()
        report = REPORT.replace("## What to do", "```yaml\n---\nkind: Secret\n```\n\n## What to do")
        self.deliver(adapter, result=report)
        fold = adapter.log[0][1]["blocks"][-1]
        self.assertEqual(fold["child_blocks"], _render_blocks(report.strip()))

    def test_a_divider_before_the_options_keeps_the_reply_and_says_why(self):
        adapter = _Adapter()
        report = REPORT.replace("## What to do", "---\n\n## What to do")
        with self.assertLogs(runtime.logger) as logs:
            self.assertIs(self.wrap(adapter, result=report), adapter)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("the fold is refused: a divider comes before the last option or link", logs.output[0])

    def test_a_click_sends_apply_option_as_the_clicker(self):
        adapter = _Adapter()
        self.deliver(adapter)
        blocks = adapter.log[0][1]["blocks"]
        button = blocks[1]["elements"][1]
        action = {"action_id": button["action_id"], "text": button["text"], "value": button["value"]}
        self.assertTrue(clicks._shown_text(action).startswith("apply Option B"))
        answered = clicks.answered_blocks(
            blocks, lambda a: bool(presenter.CHOICE_ACTION_ID_PATTERN.search(a)), "✓ picked"
        )
        kept = [e["text"]["text"] for b in answered if b["type"] == "actions" for e in b["elements"]]
        self.assertEqual(kept, ["GKE Workloads ↗", "Cloud Logs ↗"])

    def test_an_alert_is_edited_once_even_when_no_row_is_stored(self):
        adapter = _Adapter()
        self.deliver(adapter)
        # No incidents row was written, as when POST /v1/incidents fails.
        self.assertIs(self.wrap(adapter), adapter)

    def test_a_failed_edit_leaves_the_alert_open(self):
        self.deliver(_Adapter(fail_update=True))
        adapter = _Adapter()
        self.deliver(adapter)
        self.assertEqual([e[0] for e in adapter.log], ["chat_update"])

    def test_not_recommended_is_not_the_recommendation(self):
        report = REPORT.replace(
            "- ✅ **Recommended: Option B**",
            "- ❌ **Not Recommended: Option A** — it hides the cause.\n- ✅ **Recommended: Option B**",
        )
        triage = runtime.parse_triage(report)
        self.assertEqual([rec for _label, rec in triage["choices"]], [False, True])

    def test_a_stray_heading_between_options_still_shows_every_option(self):
        for cut in ("# undo it with kubectl rollout undo\n", "````\n```\n# undo\n```\n````\n"):
            with self.subTest(cut=cut):
                report = REPORT.replace("- **Option B", cut + "- **Option B")
                self.assertEqual([rec for _label, rec in runtime.parse_triage(report)["choices"]], [False, True])

    def test_a_whats_wrong_opening_with_a_code_span_still_gives_the_headline(self):
        lines = REPORT.split("\n")
        at = next(i for i, line in enumerate(lines) if "What's wrong" in line) + 1
        lines.insert(at, "```payments-api``` is crashlooping on OOM.\n")
        triage = runtime.parse_triage("\n".join(lines))
        self.assertEqual(triage["headline"], "payments-api is crashlooping on OOM.")

    def test_a_line_opening_with_a_code_span_is_not_a_fence(self):
        report = REPORT.replace("- ✅ **Recommended", "```kubectl rollout undo``` is the command.\n- ✅ **Recommended")
        triage = runtime.parse_triage(report)
        self.assertEqual([rec for _label, rec in triage["choices"]], [False, True])
        self.assertEqual([label for label, _url in triage["links"]], ["GKE Workloads", "Cloud Logs"])

    def test_an_option_named_only_in_a_heading_keeps_the_threaded_reply(self):
        report = REPORT.replace("- ✅ **Recommended", "### Option C (Drain the node)\nmoves the pods.\n- ✅ **Recommended")
        self.assertIsNone(runtime.parse_triage(report))

    def test_a_stray_heading_after_the_options_keeps_links_and_the_recommendation(self):
        triage = runtime.parse_triage(REPORT.replace("- ✅ **Recommended", "# see the runbook\n- ✅ **Recommended"))
        self.assertEqual([rec for _label, rec in triage["choices"]], [False, True])
        self.assertEqual([label for label, _url in triage["links"]], ["GKE Workloads", "Cloud Logs"])

    def test_a_link_emoji_inside_prose_is_not_the_links_line(self):
        report = REPORT.replace(
            "from the GitOps repo.", "from the GitOps repo per 🔗 [the runbook](https://example.com/runbook)."
        )
        triage = runtime.parse_triage(report)
        self.assertEqual([label for label, _url in triage["links"]], ["GKE Workloads", "Cloud Logs"])

    def test_emphasis_closed_before_the_colon_is_still_the_recommendation(self):
        report = REPORT.replace("**Recommended: Option B**", "**Recommended**: Option B")
        self.assertEqual([rec for _label, rec in runtime.parse_triage(report)["choices"]], [False, True])

    def test_an_unclosed_fence_between_options_keeps_the_reply(self):
        report = REPORT.replace("- **Option B", "```\nkubectl rollout undo deploy/payments-api\n- **Option B")
        self.assertIsNone(runtime.parse_triage(report))

    def test_a_longer_fence_is_closed_only_by_one_as_long(self):
        report = REPORT.replace("- **Option B", "````\n```\n````\n- **Option B")
        triage = runtime.parse_triage(report)
        self.assertEqual([rec for _label, rec in triage["choices"]], [False, True])
        self.assertEqual(len(triage["links"]), 2)

    def test_a_url_slack_would_refuse_is_dropped_not_sent(self):
        url = "https://console.cloud.google.com/logs/query;query=" + "a" * runtime.BUTTON_URL_MAX
        triage = runtime.parse_triage(REPORT.replace(LOGS_URL, url))
        self.assertEqual(triage["links"], [("GKE Workloads", WORKLOADS_URL)])

    def test_two_what_to_do_sections_keep_the_reply(self):
        second = "\n## What to do (cluster B)\n\n- **Option C (Drain the node):** moves the pods.\n"
        self.assertIsNone(runtime.parse_triage(REPORT + second))

    def test_a_failed_edit_falls_back_to_the_reply(self):
        adapter = _Adapter(fail_update=True)
        _wrapped, res = self.deliver(adapter)
        self.assertEqual(adapter.log, [("send", CHANNEL, "the report", {"thread_id": ALERT_TS})])
        self.assertEqual(res.message_id, "1700000000.000900")

    def test_the_single_fix_shape_gets_one_apply_button(self):
        adapter = _Adapter()
        self.deliver(adapter, result=SINGLE)
        _headline, actions, fold = adapter.log[0][1]["blocks"]
        self.assertEqual([b["text"]["text"] for b in actions["elements"]], ["apply: Lower the node pool max"])
        self.assertEqual(actions["elements"][0]["style"], "primary")
        self.assertEqual(fold["title"]["text"], "why · what the fix does")

    def test_a_report_with_no_parseable_option_keeps_the_reply(self):
        adapter = _Adapter()
        vague = "## What's wrong\n\nIt broke.\n\n## What to do\n\n- **To authorize:** reply **'apply'**.\n"
        self.assertTrue(kanban_notifier.actionable_report(vague))
        self.assertIs(self.wrap(adapter, result=vague), adapter)

    def test_a_title_with_parentheses_is_kept_whole(self):
        triage = runtime.parse_triage(REPORT.replace("Restore the secret", "Scale to 4 (from 2)"))
        self.assertEqual(triage["choices"][1][0], "apply Option B: Scale to 4 (from 2)")
        single = runtime.parse_triage(SINGLE.replace("Lower the node pool max", "Cap at 4 (was 8)"))
        self.assertEqual(single["choices"], [("apply: Cap at 4 (was 8)", True)])

    def test_only_the_links_line_becomes_buttons(self):
        prose = REPORT.replace(
            "Recreate payments-db-creds from the GitOps repo.",
            "Recreate it per [the runbook](https://example.com/runbook).",
        )
        triage = runtime.parse_triage(prose)
        self.assertEqual([label for label, _ in triage["links"]], ["GKE Workloads", "Cloud Logs"])

    def test_a_thread_is_matched_exactly_not_by_substring(self):
        self.route("k8s-evt-0000beef", "slack", CHANNEL, "1700000000.0001")
        self.assertTrue(runtime.is_open_alert(CHANNEL, ALERT_TS, self.db))
        self.assertFalse(runtime.is_open_alert(CHANNEL, "1700000000.00010", self.db))
        self.assertFalse(runtime.is_open_alert("C0OTHER", ALERT_TS, self.db))

    def test_a_code_block_in_the_report_is_not_a_heading(self):
        fenced = REPORT.replace(
            "Revert the Deployment to revision 7.\n",
            "Revert the Deployment to revision 7.\n```\n# in seeded-debug\nkubectl rollout undo deploy/payments-api\n```\n",
        )
        triage = runtime.parse_triage(fenced)
        self.assertEqual([c for c, _ in triage["choices"]],
                         ["apply Option A: Roll back to 14:02", "apply Option B: Restore the secret"])
        self.assertEqual(len(triage["links"]), 2)

    def test_a_fenced_option_shape_is_not_an_option(self):
        quoted = REPORT.replace(
            "## What to do\n\n",
            "## What to do\n\n```\n- **Option A (<Action Title>):** <what it does>\n```\n",
        )
        triage = runtime.parse_triage(quoted)
        self.assertEqual(triage["choices"][0][0], "apply Option A: Roll back to 14:02")

    def test_a_link_url_keeps_its_parentheses(self):
        url = "https://console.cloud.google.com/logs/query;query=(severity>=ERROR)"
        triage = runtime.parse_triage(REPORT.replace(LOGS_URL, url))
        self.assertEqual(triage["links"][1], ("Cloud Logs", url.replace(">", "%3E")))

    def test_a_link_that_gets_a_button_is_in_the_fallback_text_too(self):
        url = "https://console.cloud.google.com/logs/query;query=a|b<c>d"
        triage = runtime.parse_triage(REPORT.replace(LOGS_URL, url))
        encoded = "https://console.cloud.google.com/logs/query;query=a%7Cb%3Cc%3Ed"
        self.assertEqual(triage["links"][1], ("Cloud Logs", encoded))
        self.assertIn(f"<{encoded}|Cloud Logs>", runtime.fallback_text(triage))

    def test_the_url_limit_is_measured_after_encoding(self):
        base = "https://console.cloud.google.com/logs/query;query="
        fits = base + "a" * (runtime.BUTTON_URL_MAX - len(base))
        triage = runtime.parse_triage(REPORT.replace(LOGS_URL, fits))
        self.assertEqual(triage["links"][1], ("Cloud Logs", fits))
        # At the limit as written, two over once ">" becomes "%3E".
        pushed = base + ">" + "a" * (runtime.BUTTON_URL_MAX - len(base) - 1)
        triage = runtime.parse_triage(REPORT.replace(LOGS_URL, pushed))
        self.assertEqual(triage["links"], [("GKE Workloads", WORKLOADS_URL)])

    def test_a_url_holding_an_entity_opens_the_same_link_from_the_button_and_the_text(self):
        url = "https://console.cloud.google.com/logs/query;query=a&lt;b&c"
        adapter = _Adapter()
        self.deliver(adapter, result=REPORT.replace(LOGS_URL, url))
        update = adapter.log[0][1]
        buttons = [b for block in update["blocks"] if block["type"] == "actions" for b in block["elements"]]
        button = next(b for b in buttons if b["text"]["text"].startswith("Cloud Logs"))
        target = re.search(r"<([^|>]+)\|Cloud Logs>", update["text"]).group(1)
        # Slack decodes these three entities in mrkdwn text, and nothing in a button's url.
        opened = target.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        self.assertEqual(button["url"], url)
        self.assertEqual(opened, button["url"])

    def test_an_already_encoded_url_is_not_encoded_again(self):
        url = "https://console.cloud.google.com/logs/query;query=severity%3E%3DERROR"
        triage = runtime.parse_triage(REPORT.replace(LOGS_URL, url))
        self.assertEqual(triage["links"][1], ("Cloud Logs", url))

    def test_a_decorated_heading_still_gives_the_headline(self):
        for heading in ("## 🚨 What's wrong?", "## What is wrong"):
            triage = runtime.parse_triage(REPORT.replace("## What's wrong", heading))
            self.assertTrue(triage["headline"].startswith("payments-api in seeded-debug keeps crashing"))

    def test_no_whats_wrong_sentence_keeps_the_reply(self):
        self.assertIsNone(runtime.parse_triage(REPORT.replace("## What's wrong", "## Summary")))
        self.assertIsNone(runtime.parse_triage(REPORT.replace("## What's wrong\n\n", "## What's wrong\n\n### Symptom\n\n")))

    def test_an_option_that_does_not_parse_keeps_the_reply(self):
        uneven = REPORT.replace("- **Option B (Restore the secret):**", "- **Option B** (Restore the secret):")
        self.assertIsNone(runtime.parse_triage(uneven))
        adapter = _Adapter()
        self.assertIs(self.wrap(adapter, result=uneven), adapter)

    def test_button_text_fits_slack(self):
        long_title = "x" * 120
        triage = runtime.parse_triage(REPORT.replace("Restore the secret", long_title))
        blocks = runtime.blocks_triage(triage, [])
        for button in blocks[1]["elements"]:
            self.assertLessEqual(len(button["text"]["text"]), presenter.BUTTON_TEXT_MAX)


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
        stub.slack_ux_incident = runtime
        with mock.patch.dict(sys.modules, {"gateway": stub, "gateway.slack_ux_incident": runtime}):
            namespace = {}
            exec(compile(source, "notifier", "exec"), namespace)  # noqa: S102 — the fixture under test
        watcher = namespace["Watcher"](adapter, SimpleNamespace(result=REPORT), {"chat_id": CHANNEL})
        asyncio.run(watcher._send_event(SimpleNamespace(kind="completed"), "msg"))
        return namespace["calls"]

    def test_flag_off_the_patched_notifier_delivers_through_its_own_adapter(self):
        applier.apply(self.root)
        patched = (self.root / applier.RELATIVE).read_text()
        self.assertIn(applier.BUILD_MARKER, patched)
        adapter = _Adapter()
        with mock.patch.dict(os.environ, {"KAGE_SLACK_UX": "0"}):
            self.assertEqual(self.run_send(patched, adapter), [adapter])
            self.assertEqual(self.run_send(NOTIFIER, adapter), [adapter])

    def test_a_second_run_refuses(self):
        applier.apply(self.root)
        with self.assertRaises(SystemExit):
            applier.apply(self.root)


if __name__ == "__main__":
    unittest.main()
