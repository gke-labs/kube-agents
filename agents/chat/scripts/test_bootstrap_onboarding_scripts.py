"""Unit tests for the no-LLM onboarding cron scripts.

Run: python3 -m unittest agents/chat/scripts/test_bootstrap_onboarding_scripts.py

Covers the deterministic decision + I/O logic of:
  - bootstrap_delivery.py  (no_agent delivery of INVENTORY.md, claimed once)
  - bootstrap_scan_gate.py (files the sweep as a kanban task listing the
                            ready Cluster Agents, whose cards the hand-off
                            files; stops re-filing)

The in-process job removal in bootstrap_delivery._retire_jobs imports
cron.jobs, which is unavailable here; its import is guarded, so it is a no-op
unless a test puts a stand-in module in sys.modules.

bootstrap_delivery imports sandbox_exec from agents/platform/scripts, which the
image copies into the same directory as the chat scripts. The delivery tests
pin sandbox_enabled() rather than trusting that this machine has no
/etc/hermes/config.yaml.

The theme of these tests is that onboarding happens ONCE. Each stage is
therefore checked twice: once for doing its job, and once for refusing to do it
again.
"""

import contextlib
import errno
import importlib.util
import http.server
import io
import json
import threading
import urllib.error
import os
import shutil
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
from datetime import datetime, timezone
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.absolute()))
# The gate imports cluster_agent_profile, which the image copies beside it from here.
sys.path.insert(1, str(Path(__file__).resolve().parents[2] / "platform" / "scripts"))
# Where each fleet audit's check roster lives (audit_report.AUDITS).
FLEET_AUDIT_SCRIPTS = Path(__file__).resolve().parents[2] / "platform" / "skills" / "fleet-audit" / "scripts"
CLUSTER_INVENTORY_SOP = Path(__file__).resolve().parents[2] / "platform" / "governance" / "cluster_inventory_audit_sop.md"

import bootstrap_delivery  # noqa: E402
import bootstrap_handoff  # noqa: E402
import bootstrap_scan_gate  # noqa: E402
import chat_notify  # noqa: E402
import cluster_agent_profile  # noqa: E402
import findings_queue  # noqa: E402
import inventory_findings  # noqa: E402
import oobe  # noqa: E402
import profile_scaffold  # noqa: E402
import sandbox_exec  # noqa: E402
from cluster_agent_reconcile import SCAFFOLD_ARTIFACTS  # noqa: E402

INVENTORY = "INVENTORY.md"
DELIVERED = "INVENTORY.delivered.md"
ALIGNED = ".user_aligned"
COMPLETED = ".bootstrap_completed"
SCAN_FILED = ".bootstrap_scan_filed"
SHOWN = "INVENTORY.shown.json"
SHOWN_DELIVERED = "INVENTORY.shown.delivered.json"
SHOWN_RECORD = {
    "items": [
        {"class": "critical", "ids": ["sa-key.acme.prod.app.sa1", "sa-key.acme.prod.app.sa2"]},
        {"class": "critical", "ids": ["crashloop.acme.prod.app.api"]},
    ]
}
ITEMS = "INVENTORY.items.json"
SCORES = "INVENTORY.scores.json"
SCORE = {
    "rubric": {"B": 3, "L": 6, "detect": 3, "recover": 2, "C": 1.0},
    "recommendation": {"action": "add a readinessProbe", "rationale": "traffic", "risk": "5xx"},
    "remediation": {"kind": "manifest", "path": "k8s/api.yaml", "note": "add the probe"},
    "verification": {"kind": "kubectl", "command": "kubectl get deploy api", "still_failing_when": "empty"},
}
# Two findings on acme/prod and one on acme/dev, as `extract` writes them, and their scores.
EXTRACTED = {
    "items": [
        {"id": "f001", "check": "probes-readiness", "project": "acme", "cluster": "prod",
         "namespace": "payments", "object": "api", "title": "no readinessProbe on api", "line": 3},
        {"id": "f002", "check": "probes-readiness", "project": "acme", "cluster": "prod",
         "namespace": "payments", "object": "web", "title": "no readinessProbe on web", "line": 4},
        {"id": "f003", "check": "probes-readiness", "project": "acme", "cluster": "dev",
         "namespace": "payments", "object": "api", "title": "no readinessProbe on api", "line": 5},
    ]
}
SCORED = {"scores": {"f001": SCORE, "f002": SCORE, "f003": SCORE}, "complete_clusters": ["acme/prod"]}


def delivered(report: str) -> str:
    """What delivery prints for a report with no oobe marker beside it: the report, then the audits note."""
    note = f"{bootstrap_delivery.AUDITS_RUNNING_NOTE} {bootstrap_delivery.AUDITS_HOME_SENTENCE}"
    return report.rstrip("\n") + "\n\n" + note + "\n"


class DeliveryDecisionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_not_awaiting_when_nothing_present(self):
        self.assertFalse(bootstrap_delivery._awaiting_delivery(self.d))

    def test_not_awaiting_when_only_inventory(self):
        (self.d / INVENTORY).write_text("x")
        self.assertFalse(bootstrap_delivery._awaiting_delivery(self.d))

    def test_awaiting_once_a_human_is_present(self):
        # The report is not part of this check: with the sandbox on it is on
        # another pod, and main() reads it only after this says yes.
        (self.d / ALIGNED).touch()
        self.assertTrue(bootstrap_delivery._awaiting_delivery(self.d))


class DeliveryMainTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)
        patcher = mock.patch.object(sandbox_exec, "sandbox_enabled", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = bootstrap_delivery.main(self.d)
        return rc, buf.getvalue()

    def test_silent_when_not_ready(self):
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertFalse((self.d / COMPLETED).exists())

    def test_emits_verbatim_and_concludes_once(self):
        report = "# GKE Environment Discovery Report\n\n| Cluster | ... |\n"
        (self.d / INVENTORY).write_text(report, encoding="utf-8")
        (self.d / ALIGNED).touch()

        rc, out = self._run()
        self.assertEqual(rc, 0)
        # The report byte-for-byte, no reformatting, then the audits note.
        self.assertEqual(out, delivered(report))
        # Concluded: completion marked, report moved out of the scan gate's way.
        self.assertTrue((self.d / COMPLETED).exists())
        self.assertFalse((self.d / INVENTORY).exists())

    def test_delivered_report_is_kept_for_resending(self):
        # A fleet-wide sweep is expensive and a chat message is easy to lose.
        # Concluding onboarding must not destroy the only copy of the report.
        report = "# Report\n"
        (self.d / INVENTORY).write_text(report, encoding="utf-8")
        (self.d / ALIGNED).touch()
        self._run()
        self.assertEqual((self.d / DELIVERED).read_text(encoding="utf-8"), report)

    def test_second_run_is_silent(self):
        (self.d / INVENTORY).write_text("report", encoding="utf-8")
        (self.d / ALIGNED).touch()
        self._run()  # first delivery
        rc, out = self._run()  # second tick
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_claim_is_atomic_so_a_racing_run_stays_silent(self):
        """Two runs, one report: only one may reach stdout.

        The scheduled tick and the plugin's trigger_job can overlap. Simulate
        the loser of that race by staging the claim marker as if the winner had
        just taken it, with everything else still saying "ready to deliver".
        """
        (self.d / INVENTORY).write_text("report", encoding="utf-8")
        (self.d / ALIGNED).touch()
        self.assertTrue(bootstrap_delivery._claim_delivery(self.d))  # winner
        self.assertFalse(bootstrap_delivery._claim_delivery(self.d))  # loser

        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        # The winner still owns the report: the loser must not archive it.
        self.assertTrue((self.d / INVENTORY).exists())

    def test_unreadable_report_leaves_state_untouched(self):
        # Nothing was delivered, so nothing may be marked delivered — otherwise
        # a transient read error silently costs the user the whole report.
        (self.d / INVENTORY).mkdir()  # a directory: open() raises OSError
        (self.d / ALIGNED).touch()
        rc, out = self._run()
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertFalse((self.d / COMPLETED).exists())

    def test_a_report_past_the_cap_is_refused_not_cut(self):
        (self.d / INVENTORY).write_bytes(b"x" * (bootstrap_delivery.REPORT_MAX_BYTES + 1))
        (self.d / ALIGNED).touch()
        rc, out = self._run()
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertFalse((self.d / COMPLETED).exists())
        self.assertTrue((self.d / INVENTORY).exists())

    def test_a_report_at_the_cap_is_delivered(self):
        report = "x" * bootstrap_delivery.REPORT_MAX_BYTES
        (self.d / INVENTORY).write_text(report, encoding="utf-8")
        (self.d / ALIGNED).touch()
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(out, delivered(report))


# The plugin that writes what the conversation path reads, loaded from its file.
PLUGIN_PATH = Path(__file__).resolve().parents[1] / "defaults" / "plugins" / "bootstrap_onboarding" / "plugin.py"
CONVERSATION = {
    "platform": "slack",
    "conversation": "slack:D0DM/1792185148.859139",
    "context_id": "ctx-21f41a7dc20859301d2307b8",
    "bound_at": 1792185149.0,
}
NEXT_ENV = {
    "A2A_NOTIFY_CONVERSATIONS": "slack",
    "A2A_NOTIFY_PLATFORM": "",
    "NATS_URL": "nats://nats.kubeagents-system:4222",
    "A2A_BUS_USER": "agent-platform",
    "KAGE_SLACK_UX": "false",
}
REFUSED = json.dumps({"error": "not a live conversation with that context"}) + "\n"
POSTED_ANSWER = json.dumps({"message_id": "1792185200.000100", "thread_id": "1792185148.859139"}) + "\n"


class DeliveryToConversationTest(unittest.TestCase):
    """Under spec.mode next: the report goes into the conversation the plugin recorded."""

    REPORT = "# GKE Environment Discovery Report\n\nTwo things need you.\n"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)
        (self.d / bootstrap_delivery.CONVERSATION_FILE).write_text(json.dumps(CONVERSATION), encoding="utf-8")
        (self.d / bootstrap_delivery.CONVERSATION_ALIGNED_MARKER).touch()
        (self.d / INVENTORY).write_text(self.REPORT, encoding="utf-8")
        for patcher in (
            mock.patch.object(sandbox_exec, "sandbox_enabled", return_value=False),
            mock.patch.dict(os.environ, NEXT_ENV),
            mock.patch.object(bootstrap_delivery.time, "sleep"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.calls = []
        self.answers = []

    def _answer(self, returncode=0, stdout=POSTED_ANSWER, stderr=""):
        self.answers.append((returncode, stdout, stderr))

    def _fake_run(self, argv, **_kwargs):
        self.calls.append(argv)
        if "--chat-file" in argv:
            with open(argv[argv.index("--chat-file") + 1], encoding="utf-8") as handle:
                self.card = json.load(handle)
        returncode, stdout, stderr = self.answers.pop(0) if self.answers else (0, POSTED_ANSWER, "")
        if isinstance(returncode, BaseException):
            raise returncode
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    def _run(self):
        buf = io.StringIO()
        with mock.patch.object(bootstrap_delivery.subprocess, "run", side_effect=self._fake_run), \
                contextlib.redirect_stdout(buf):
            rc = bootstrap_delivery.main(self.d)
        self.assertEqual(buf.getvalue(), "", "the conversation path never prints the report")
        return rc

    def _attempts(self):
        return bootstrap_delivery._read_attempts(self.d)

    def test_the_plugin_and_delivery_name_the_same_files(self):
        spec = importlib.util.spec_from_file_location("_bootstrap_onboarding_plugin", PLUGIN_PATH)
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
        self.assertEqual(plugin.CONVERSATION_FILE, bootstrap_delivery.CONVERSATION_FILE)
        self.assertEqual(plugin.CONVERSATION_ALIGNED_MARKER, bootstrap_delivery.CONVERSATION_ALIGNED_MARKER)
        self.assertEqual(plugin.CONVERSATION_ATTEMPTS_FILE, bootstrap_delivery.CONVERSATION_ATTEMPTS_FILE)
        self.assertEqual(plugin.CONVERSATION_DONE_FILE, bootstrap_delivery.CONVERSATION_DONE_FILE)

    def test_the_report_and_its_note_post_into_the_conversation_once(self):
        self.assertEqual(self._run(), 0)
        (argv,) = self.calls
        self.assertEqual(
            argv[:9],
            ["a2a", "notify", "--platform", "slack", "--conversation", CONVERSATION["conversation"],
             "--context", CONVERSATION["context_id"], "--timeout"],
        )
        self.assertNotIn("--update", argv)
        text = argv[argv.index("--") + 1]
        self.assertTrue(text.startswith(self.REPORT.rstrip("\n")))
        self.assertIn(bootstrap_delivery.AUDITS_NO_HOME_SENTENCE, text)
        self.assertNotIn(bootstrap_delivery.AUDITS_HOME_SENTENCE, text)
        self.assertTrue((self.d / COMPLETED).exists())
        self.assertEqual((self.d / DELIVERED).read_text(encoding="utf-8"), self.REPORT)
        # Kept as onboarding's destination after delivery.
        self.assertTrue((self.d / bootstrap_delivery.CONVERSATION_FILE).exists())
        self.assertEqual(self._run(), 0)
        self.assertEqual(len(self.calls), 1)

    def test_a_gateway_with_a_home_channel_keeps_the_home_sentence(self):
        with mock.patch.dict(os.environ, {"A2A_NOTIFY_PLATFORM": "slack"}):
            self._run()
        text = self.calls[0][self.calls[0].index("--") + 1]
        self.assertIn(bootstrap_delivery.AUDITS_HOME_SENTENCE, text)

    def test_no_home_is_said_on_every_path(self):
        # With no home channel, later summaries post nowhere; the note says so whatever the stage did.
        for state in (
            {},
            {oobe.STATE_FIRED: list(oobe.FIRST_RUN_AUDITS), oobe.STATE_DONE: True},
            {oobe.STATE_HELD: {a: oobe.HOLD_PAUSED for a in oobe.FIRST_RUN_AUDITS}, oobe.STATE_DONE: True},
            {oobe.STATE_DONE: True, oobe.STATE_SKIPPED: True, oobe.STATE_REASON: oobe.SKIP_NOT_NEW, oobe.STATE_AT: 1},
        ):
            with self.subTest(state=state):
                oobe.write_state(self.d, state)
                note = bootstrap_delivery._audits_note(self.d, False, home=False)
                self.assertEqual(note.count(bootstrap_delivery.AUDITS_NO_HOME_SENTENCE), 1)
                self.assertNotIn(bootstrap_delivery.AUDITS_HOME_SENTENCE, note)

    def test_no_home_is_said_beside_listed_summaries(self):
        oobe.write_state(self.d, {oobe.STATE_FIRED: ["compliance-audit"], oobe.STATE_DONE: True})
        with mock.patch.object(bootstrap_delivery, "_lost_audits", return_value=[("compliance-audit", False)]):
            note = bootstrap_delivery._audits_note(self.d, False, home=False)
        self.assertIn("not posted to the chat: `compliance-audit`.", note)
        self.assertTrue(note.endswith(bootstrap_delivery.AUDITS_NO_HOME_SENTENCE))

    def test_the_card_posts_with_its_update_key_then_the_note(self):
        import inventory_presenter

        card = ({"schema": "kube-agents.chat/v1", "headline": "2 findings"}, "2 findings")
        with mock.patch.dict(os.environ, {"KAGE_SLACK_UX": "true"}), \
                mock.patch.object(inventory_presenter, "chat", return_value=card):
            self._run()
        report, note = self.calls
        self.assertEqual(report[report.index("--update") + 1], bootstrap_delivery.CONVERSATION_REPORT_UPDATE)
        self.assertIn("--chat-file", report)
        self.assertEqual(self.card, card[0])
        self.assertEqual(report[report.index("--") + 1], "2 findings")
        self.assertNotIn("--chat-file", note)
        self.assertNotIn("--update", note)
        self.assertIn(bootstrap_delivery.AUDITS_NO_HOME_SENTENCE, note[note.index("--") + 1])

    def test_a_failed_note_after_the_card_keeps_the_report_delivered(self):
        import inventory_presenter

        card = ({"schema": "kube-agents.chat/v1"}, "2 findings")
        self._answer()
        self._answer(1, REFUSED)
        with mock.patch.dict(os.environ, {"KAGE_SLACK_UX": "true"}), \
                mock.patch.object(inventory_presenter, "chat", return_value=card):
            self._run()
        self.assertTrue((self.d / COMPLETED).exists())
        self.assertEqual(self._attempts(), {})

    def test_a_refusal_releases_the_claim_and_waits_before_the_next_post(self):
        self._answer(1, REFUSED)
        with mock.patch.object(bootstrap_delivery, "_mark_shown") as mark_shown:
            self._run()
        mark_shown.assert_not_called()
        self.assertFalse((self.d / COMPLETED).exists())
        self.assertTrue((self.d / INVENTORY).exists())
        attempts = self._attempts()
        self.assertEqual(attempts["attempts"], 1)
        self.assertIn("not a live conversation", attempts["last_reason"])
        # Inside the wait: no post.
        self._run()
        self.assertEqual(len(self.calls), 1)
        # Past it: posted, and onboarding is done.
        attempts["last_at"] -= bootstrap_delivery.CONVERSATION_RETRY_WAITS_SECONDS[0] + 1
        (self.d / bootstrap_delivery.CONVERSATION_ATTEMPTS_FILE).write_text(json.dumps(attempts), encoding="utf-8")
        self._run()
        self.assertEqual(len(self.calls), 2)
        self.assertTrue((self.d / COMPLETED).exists())

    def test_past_the_last_attempt_the_report_waits(self):
        record = {"attempts": bootstrap_delivery.CONVERSATION_MAX_ATTEMPTS, "last_reason": "refused", "last_at": 0}
        (self.d / bootstrap_delivery.CONVERSATION_ATTEMPTS_FILE).write_text(json.dumps(record), encoding="utf-8")
        self._run()
        self.assertEqual(self.calls, [])
        self.assertFalse((self.d / COMPLETED).exists())
        self.assertTrue((self.d / INVENTORY).exists())

    def test_a_text_post_that_failed_is_retried(self):
        # The text path accepts a possible second copy over a lost report.
        for stdout, stderr in ((json.dumps({"error": "post failed: ratelimited"}) + "\n", ""),
                               ("", "a2a: notify: flush: nats: connection closed"),
                               ("", "a2a: notify: nats: authorization violation")):
            with self.subTest(stderr=stderr or stdout):
                (self.d / bootstrap_delivery.CONVERSATION_ATTEMPTS_FILE).unlink(missing_ok=True)
                self._answer(1, stdout, stderr)
                with mock.patch.object(bootstrap_delivery, "_mark_shown") as mark_shown:
                    self._run()
                mark_shown.assert_not_called()
                self.assertFalse((self.d / COMPLETED).exists())
                self.assertTrue((self.d / INVENTORY).exists())
                self.assertFalse((self.d / bootstrap_delivery.CONVERSATION_DONE_FILE).exists())
                self.assertEqual(self._attempts()["attempts"], 1)

    def test_a_text_post_that_may_have_landed_keeps_the_claim(self):
        for answer in (subprocess.TimeoutExpired(["a2a"], 105), chat_notify.NOTIFY_OUTCOME_UNKNOWN, 2):
            with self.subTest(answer=answer):
                (self.d / COMPLETED).unlink(missing_ok=True)
                if (self.d / DELIVERED).exists():
                    (self.d / DELIVERED).replace(self.d / INVENTORY)
                self._answer(answer, "", "no answer")
                with mock.patch.object(bootstrap_delivery, "_mark_shown", return_value=True) as mark_shown:
                    self._run()
                mark_shown.assert_called_once()
                self.assertTrue((self.d / COMPLETED).exists())
                self.assertFalse((self.d / INVENTORY).exists())
                self.assertEqual(self._attempts()["attempts"], 0)
                self.assertIn("may have landed", self._attempts()["last_reason"])
                self.assertEqual(
                    (self.d / bootstrap_delivery.CONVERSATION_DONE_FILE).read_text(encoding="utf-8").strip(),
                    bootstrap_delivery.MAYBE_POSTED,
                )

    def _card(self):
        import inventory_presenter

        card = ({"schema": "kube-agents.chat/v1", "headline": "2 findings"}, "2 findings")
        return mock.patch.dict(os.environ, {"KAGE_SLACK_UX": "true"}), \
            mock.patch.object(inventory_presenter, "chat", return_value=card)

    def test_a_card_that_may_have_landed_is_sent_again_with_its_key(self):
        flag, chat = self._card()
        with flag, chat:
            for answer in (chat_notify.NOTIFY_OUTCOME_UNKNOWN, subprocess.TimeoutExpired(["a2a"], 105),
                           1):
                with self.subTest(answer=answer):
                    self.calls.clear()
                    (self.d / bootstrap_delivery.CONVERSATION_ATTEMPTS_FILE).unlink(missing_ok=True)
                    self._answer(answer, "", "no answer")
                    self._run()
                    self.assertFalse((self.d / COMPLETED).exists())
                    self.assertTrue((self.d / INVENTORY).exists())
                    self.assertEqual(self._attempts()["attempts"], 1)
            # The next attempt carries the same key, so a card that landed is edited.
            record = self._attempts()
            record["last_at"] -= bootstrap_delivery.CONVERSATION_RETRY_WAITS_SECONDS[0] + 1
            (self.d / bootstrap_delivery.CONVERSATION_ATTEMPTS_FILE).write_text(json.dumps(record), encoding="utf-8")
            self.calls.clear()
            self._run()
        report = self.calls[0]
        self.assertEqual(report[report.index("--update") + 1], bootstrap_delivery.CONVERSATION_REPORT_UPDATE)
        self.assertTrue((self.d / COMPLETED).exists())

    def test_an_edit_that_failed_counts_as_posted(self):
        # The gateway holds the key, so the card from an earlier attempt is in the chat.
        flag, chat = self._card()
        self._answer(1, json.dumps({"error": "edit failed: message_not_found"}) + "\n")
        with flag, chat:
            self._run()
        self.assertTrue((self.d / COMPLETED).exists())
        self.assertFalse((self.d / INVENTORY).exists())
        self.assertEqual(
            (self.d / bootstrap_delivery.CONVERSATION_DONE_FILE).read_text(encoding="utf-8").strip(),
            bootstrap_delivery.POSTED,
        )

    def test_a_posted_report_records_its_outcome(self):
        self._run()
        self.assertEqual(
            (self.d / bootstrap_delivery.CONVERSATION_DONE_FILE).read_text(encoding="utf-8").strip(),
            bootstrap_delivery.POSTED,
        )

    def _claim(self, age):
        (self.d / COMPLETED).touch()
        then = time.time() - age
        os.utime(self.d / COMPLETED, (then, then))

    def test_a_claim_with_no_outcome_is_released_once_stale(self):
        self._claim(bootstrap_delivery.STALE_CLAIM_SECONDS - 30)
        self._run()
        self.assertTrue((self.d / COMPLETED).exists(), "a run may still be posting")
        self._claim(bootstrap_delivery.STALE_CLAIM_SECONDS + 1)
        with mock.patch.object(bootstrap_delivery, "_retire_jobs") as retire:
            self._run()
        retire.assert_not_called()
        self.assertFalse((self.d / COMPLETED).exists())
        self.assertEqual(self._attempts()["attempts"], 1)
        self.assertEqual(self.calls, [])

    def test_a_claim_with_an_outcome_retires_on_schedule(self):
        (self.d / bootstrap_delivery.CONVERSATION_DONE_FILE).write_text("posted\n", encoding="utf-8")
        self._claim(bootstrap_delivery.RETIRE_AFTER_SECONDS + 1)
        with mock.patch.object(bootstrap_delivery, "_retire_jobs") as retire:
            self._run()
        retire.assert_called_once()
        self.assertTrue((self.d / COMPLETED).exists())

    def test_oobe_waits_for_the_outcome_before_it_retires(self):
        oobe.write_state(self.d, {oobe.STATE_DONE: True})
        self._claim(bootstrap_delivery.RETIRE_AFTER_SECONDS + 1)
        self.assertFalse(oobe.finished(self.d, time.time()))
        (self.d / bootstrap_delivery.CONVERSATION_DONE_FILE).write_text("posted\n", encoding="utf-8")
        self.assertTrue(oobe.finished(self.d, time.time()))

    def test_retirement_outlasts_the_report_and_the_note(self):
        self.assertGreater(bootstrap_delivery.RETIRE_AFTER_SECONDS, 2 * bootstrap_delivery.CONVERSATION_POST_MAX_SECONDS)
        self.assertGreater(bootstrap_delivery.STALE_CLAIM_SECONDS, bootstrap_delivery.CONVERSATION_POST_MAX_SECONDS)

    def test_the_last_failed_attempt_is_logged_for_an_operator(self):
        record = {"attempts": bootstrap_delivery.CONVERSATION_MAX_ATTEMPTS - 1, "last_reason": "refused", "last_at": 0}
        (self.d / bootstrap_delivery.CONVERSATION_ATTEMPTS_FILE).write_text(json.dumps(record), encoding="utf-8")
        self._answer(1, REFUSED)
        self._run()
        log = (self.d / oobe.STAGE_LOG).read_text(encoding="utf-8")
        self.assertIn(f"not posted after {bootstrap_delivery.CONVERSATION_MAX_ATTEMPTS} attempts", log)

    def test_a_report_past_one_post_is_refused_before_the_claim(self):
        (self.d / INVENTORY).write_text("x" * (bootstrap_delivery.CONVERSATION_REPORT_MAX_BYTES + 1), encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = bootstrap_delivery.main(self.d)
        self.assertEqual(rc, 1)
        self.assertEqual(buf.getvalue(), "")
        self.assertFalse((self.d / COMPLETED).exists())
        self.assertIn("larger than", self._attempts()["last_reason"])

    def test_the_size_limit_counts_undecodable_bytes_as_posted(self):
        # Each invalid byte decodes to a three-byte U+FFFD on the way to argv.
        (self.d / INVENTORY).write_bytes(b"\xff" * (bootstrap_delivery.CONVERSATION_REPORT_MAX_BYTES // 2))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bootstrap_delivery.main(self.d), 1)
        self.assertFalse((self.d / COMPLETED).exists())

    def test_the_gateway_answers_delivery_reads_are_the_gateway_s(self):
        # The gateway and the agent ship in different images, so these texts cannot change.
        source = (Path(__file__).resolve().parents[3] / "a2a" / "gateway" / "notify.go").read_text(encoding="utf-8")
        self.assertIn(f'"{bootstrap_delivery.NOTIFY_EDIT_FAILED_PREFIX} "', source)

    def test_a_stale_claim_after_a_recorded_attempt_is_not_counted_twice(self):
        self._claim(bootstrap_delivery.STALE_CLAIM_SECONDS + 1)
        record = {"attempts": 2, "last_reason": "refused", "last_at": time.time()}
        (self.d / bootstrap_delivery.CONVERSATION_ATTEMPTS_FILE).write_text(json.dumps(record), encoding="utf-8")
        self._run()
        self.assertFalse((self.d / COMPLETED).exists())
        self.assertEqual(self._attempts()["attempts"], 2)

    def test_the_outcome_is_recorded_before_the_note_goes(self):
        flag, chat = self._card()
        seen = []

        def run(argv, **kwargs):
            if "--chat-file" not in argv:
                seen.append((self.d / bootstrap_delivery.CONVERSATION_DONE_FILE).exists())
            return self._fake_run(argv, **kwargs)

        with flag, chat, mock.patch.object(bootstrap_delivery.subprocess, "run", side_effect=run), \
                contextlib.redirect_stdout(io.StringIO()):
            bootstrap_delivery.main(self.d)
        self.assertEqual(seen, [True])

    def test_an_absent_route_is_retried_then_released(self):
        delays = bootstrap_delivery.time.sleep
        for _ in range(len(chat_notify.NOTIFY_ROUTE_RETRY_DELAYS_SECONDS) + 1):
            self._answer(chat_notify.NOTIFY_ROUTE_UNAVAILABLE, "", "no gateway")
        self._run()
        self.assertEqual(len(self.calls), len(chat_notify.NOTIFY_ROUTE_RETRY_DELAYS_SECONDS) + 1)
        self.assertEqual([c.args[0] for c in delays.call_args_list], list(chat_notify.NOTIFY_ROUTE_RETRY_DELAYS_SECONDS))
        self.assertFalse((self.d / COMPLETED).exists())
        self.assertEqual(self._attempts()["attempts"], 1)

    def test_the_card_marks_findings_before_the_note_goes(self):
        flag, chat = self._card()
        order = []
        real = self._fake_run

        def run(argv, **kwargs):
            order.append("card" if "--chat-file" in argv else "note")
            return real(argv, **kwargs)

        with flag, chat, mock.patch.object(bootstrap_delivery.subprocess, "run", side_effect=run), \
                mock.patch.object(bootstrap_delivery, "_mark_shown", side_effect=lambda *a: order.append("mark") or True), \
                contextlib.redirect_stdout(io.StringIO()):
            bootstrap_delivery.main(self.d)
        self.assertEqual(order, ["card", "mark", "note"])

    def test_findings_are_marked_shown_only_after_the_post(self):
        order = []
        self.answers = []
        with mock.patch.object(bootstrap_delivery, "_mark_shown", side_effect=lambda *a: order.append("mark") or True), \
                mock.patch.object(bootstrap_delivery, "_notify_conversation",
                                  side_effect=lambda *a, **k: order.append("post") or (bootstrap_delivery.POSTED, "")):
            self._run()
        self.assertEqual(order, ["post", "mark"])

    def test_a_child_env_without_the_route_claims_nothing(self):
        for name, value in (("A2A_NOTIFY_CONVERSATIONS", ""), ("A2A_NOTIFY_CONVERSATIONS", "google_chat"),
                            ("NATS_URL", ""), ("A2A_BUS_USER", "")):
            with self.subTest(name=name, value=value), mock.patch.dict(os.environ, {name: value, "NATS_USER": ""}):
                self.assertEqual(self._run(), 0)
                self.assertEqual(self.calls, [])
                self.assertFalse((self.d / COMPLETED).exists())
                self.assertEqual(self._attempts()["attempts"], 0)
                self.assertIn("the report waits", self._attempts()["last_reason"])

    def test_a_block_does_not_restart_the_wait_after_an_attempt(self):
        record = {"attempts": 1, "last_reason": "refused", "last_at": 1000.0}
        (self.d / bootstrap_delivery.CONVERSATION_ATTEMPTS_FILE).write_text(json.dumps(record), encoding="utf-8")
        with mock.patch.dict(os.environ, {"NATS_URL": ""}):
            self._run()
        self.assertEqual(self._attempts()["last_at"], 1000.0)
        self.assertIn("NATS_URL", self._attempts()["last_reason"])

    def test_the_same_block_is_not_rewritten_each_tick(self):
        with mock.patch.dict(os.environ, {"NATS_URL": ""}):
            self._run()
            first = (self.d / bootstrap_delivery.CONVERSATION_ATTEMPTS_FILE).stat().st_mtime_ns
            with mock.patch.object(bootstrap_delivery.time, "time", return_value=time.time() + 60):
                self._run()
        self.assertEqual((self.d / bootstrap_delivery.CONVERSATION_ATTEMPTS_FILE).stat().st_mtime_ns, first)

    def test_a_marker_without_its_conversation_never_prints_the_report(self):
        (self.d / bootstrap_delivery.CONVERSATION_FILE).unlink()
        self._run()
        self.assertEqual(self.calls, [])
        self.assertFalse((self.d / COMPLETED).exists())
        self.assertIn(bootstrap_delivery.CONVERSATION_FILE, self._attempts()["last_reason"])

    def test_the_conversation_wins_over_a_job_origin_marker(self):
        (self.d / ALIGNED).touch()
        self._run()
        self.assertEqual(len(self.calls), 1)

    def test_gateway_error_reads_only_an_answer_that_posted_nothing(self):
        self.assertEqual(bootstrap_delivery._gateway_error(REFUSED), "not a live conversation with that context")
        self.assertEqual(bootstrap_delivery._gateway_error(json.dumps({"message_id": "1", "error": "partly"})), "")
        self.assertEqual(bootstrap_delivery._gateway_error("not json"), "")
        self.assertEqual(bootstrap_delivery._gateway_error(""), "")


class DeliveryMarksShownTest(unittest.TestCase):
    """The report's items are marked shown as the first report, after the claim and before stdout."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)
        patcher = mock.patch.object(sandbox_exec, "sandbox_enabled", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.d / INVENTORY).write_text("# Report\n", encoding="utf-8")
        (self.d / ALIGNED).touch()
        self.out = io.StringIO()
        self.posts = []

        def post(endpoint, finding_id, body):
            # What had reached stdout when the mark was sent.
            self.posts.append((finding_id, body, self.out.getvalue()))

        self.post = mock.patch.object(bootstrap_delivery, "_post_surfaced", side_effect=post)
        self.post.start()
        self.addCleanup(self.post.stop)

    def _run(self):
        err = io.StringIO()
        with contextlib.redirect_stdout(self.out), contextlib.redirect_stderr(err):
            rc = bootstrap_delivery.main(self.d)
        return rc, self.out.getvalue(), err.getvalue()

    def _shown(self, record=SHOWN_RECORD):
        (self.d / SHOWN).write_text(record if isinstance(record, str) else json.dumps(record), encoding="utf-8")

    def test_the_names_are_the_ones_select_writes_and_the_queue_accepts(self):
        self.assertEqual(bootstrap_delivery.SHOWN_NAME, Path(inventory_findings.DEFAULT_SHOWN_PATH).name)
        self.assertEqual(
            (bootstrap_delivery.SHOWN_ITEMS, bootstrap_delivery.SHOWN_CLASS, bootstrap_delivery.SHOWN_IDS),
            (inventory_findings.SHOWN_ITEMS, inventory_findings.SHOWN_CLASS, inventory_findings.SHOWN_IDS),
        )
        self.assertIn(bootstrap_delivery.PUBLISHER, findings_queue.PACED_PUBLISHERS)

    def test_every_row_is_marked_with_one_run_before_the_report_goes_out(self):
        self._shown()
        rc, out, _ = self._run()
        self.assertEqual((rc, out), (0, delivered("# Report\n")))
        self.assertEqual(
            [finding_id for finding_id, _, _ in self.posts],
            ["sa-key.acme.prod.app.sa1", "sa-key.acme.prod.app.sa2", "crashloop.acme.prod.app.api"],
        )
        bodies = [body for _, body, _ in self.posts]
        self.assertEqual({body["publisher"] for body in bodies}, {"first_report"})
        self.assertEqual({body["added_class"] for body in bodies}, {"critical"})
        self.assertEqual(len({body["run"] for body in bodies}), 1)
        self.assertEqual({before for _, _, before in self.posts}, {""})
        self.assertFalse((self.d / SHOWN).exists())
        self.assertEqual(json.loads((self.d / SHOWN_DELIVERED).read_text(encoding="utf-8")), SHOWN_RECORD)

    def test_a_queue_that_does_not_answer_is_asked_once_and_the_report_still_goes_out(self):
        self._shown()
        self.post.stop()
        with mock.patch.object(
            bootstrap_delivery, "_post_surfaced", side_effect=urllib.error.URLError("connection refused")
        ) as post:
            rc, out, err = self._run()
        self.post.start()
        self.assertEqual((rc, out), (0, delivered("# Report\n")))
        post.assert_called_once()
        self.assertIn("did not answer", err)
        self.assertTrue((self.d / COMPLETED).exists())
        self.assertTrue((self.d / SHOWN_DELIVERED).exists())

    def test_a_row_the_queue_refuses_does_not_stop_the_others(self):
        self._shown()
        self.post.stop()
        refused = urllib.error.HTTPError("u", 404, "Not Found", {}, io.BytesIO(b'{"error": "not found"}'))
        with mock.patch.object(bootstrap_delivery, "_post_surfaced", side_effect=[refused, None, None]) as post:
            rc, out, err = self._run()
        self.post.start()
        self.assertEqual((rc, out), (0, delivered("# Report\n")))
        self.assertEqual(post.call_count, 3)
        self.assertIn("could not mark sa-key.acme.prod.app.sa1 shown: 404", err)

    def test_a_refusal_whose_body_cannot_be_read_does_not_stop_the_report(self):
        self._shown()
        self.post.stop()
        refused = urllib.error.HTTPError("u", 503, "Unavailable", {}, io.BytesIO(b""))
        refused.read = mock.Mock(side_effect=TimeoutError("read timed out"))
        with mock.patch.object(bootstrap_delivery, "_post_surfaced", side_effect=[refused, None, None]) as post:
            rc, out, err = self._run()
        self.post.start()
        self.assertEqual((rc, out), (0, delivered("# Report\n")))
        self.assertEqual(post.call_count, 3)
        self.assertIn("could not mark sa-key.acme.prod.app.sa1 shown: 503", err)

    def test_no_shown_file_marks_nothing_and_delivers(self):
        rc, out, err = self._run()
        self.assertEqual((rc, out), (0, delivered("# Report\n")))
        self.assertEqual(self.posts, [])
        self.assertIn("no INVENTORY.shown.json", err)
        self.assertFalse((self.d / SHOWN_DELIVERED).exists())

    def test_an_unreadable_shown_file_marks_nothing_and_delivers(self):
        for record in ("{not json", json.dumps({"items": [{"ids": ["x"]}]}), json.dumps([1])):
            with self.subTest(record=record):
                (self.d / COMPLETED).unlink(missing_ok=True)
                (self.d / INVENTORY).write_text("# Report\n", encoding="utf-8")
                self.out = io.StringIO()
                self._shown(record)
                rc, out, err = self._run()
                self.assertEqual((rc, out), (0, delivered("# Report\n")))
                self.assertEqual(self.posts, [])
                self.assertIn("is not readable", err)

    def test_a_failure_reading_the_shown_file_never_blocks_delivery(self):
        self._shown()
        with mock.patch.object(bootstrap_delivery, "_read_shown", side_effect=RuntimeError("boom")):
            rc, out, err = self._run()
        self.assertEqual((rc, out), (0, delivered("# Report\n")))
        self.assertIn("marking nothing: boom", err)
        # Set aside all the same, so a later report is not marked with these ids.
        self.assertFalse((self.d / SHOWN).exists())
        self.assertTrue((self.d / SHOWN_DELIVERED).exists())

    def test_a_report_listing_no_item_marks_nothing(self):
        self._shown({"items": []})
        rc, out, _ = self._run()
        self.assertEqual((rc, out), (0, delivered("# Report\n")))
        self.assertEqual(self.posts, [])
        self.assertTrue((self.d / SHOWN_DELIVERED).exists())

    def test_a_racing_run_marks_nothing(self):
        self._shown()
        self.assertTrue(bootstrap_delivery._claim_delivery(self.d))
        rc, out, _ = self._run()
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(self.posts, [])
        self.assertTrue((self.d / SHOWN).exists())

    def test_the_mark_reaches_the_queue_with_the_pods_key(self):
        received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append((self.path, self.headers.get("Authorization"), body))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.post.stop()
        self._shown({"items": [{"class": "critical", "ids": ["a/b"]}]})
        endpoint = f"http://127.0.0.1:{server.server_port}"
        with mock.patch.dict(os.environ, {"SESSION_KV_ENDPOINT": endpoint, "SESSION_KV_API_KEY": "k"}):
            rc, out, _ = self._run()
        self.post.start()
        self.assertEqual((rc, out), (0, delivered("# Report\n")))
        [(path, auth, body)] = received
        self.assertEqual((path, auth), ("/v1/findings/a%2Fb/surfaced", "Bearer k"))
        self.assertEqual((body["publisher"], body["added_class"]), ("first_report", "critical"))


class DeliveryRegistersFindingsTest(unittest.TestCase):
    """The sweep's findings are registered from this pod before the claim, since the worker's terminal may not reach the queue."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)
        patcher = mock.patch.object(sandbox_exec, "sandbox_enabled", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.d / INVENTORY).write_text("# Report\n", encoding="utf-8")
        (self.d / ALIGNED).touch()
        self.sent = []

        def post(endpoint, batch, scope):
            # Whether the claim had been taken when the batch was sent.
            self.sent.append(([f["object"] for f in batch], scope, (self.d / COMPLETED).exists()))
            return {"results": []}

        self.post = mock.patch.object(inventory_findings, "post_batch", side_effect=post)
        self.post.start()
        self.addCleanup(self.post.stop)
        marks = mock.patch.object(bootstrap_delivery, "_post_surfaced")
        marks.start()
        self.addCleanup(marks.stop)

    def _write(self, items=EXTRACTED, scores=SCORED):
        if items is not None:
            (self.d / ITEMS).write_text(json.dumps(items), encoding="utf-8")
        if scores is not None:
            (self.d / SCORES).write_text(json.dumps(scores), encoding="utf-8")

    def _run(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = bootstrap_delivery.main(self.d)
        return rc, out.getvalue(), err.getvalue()

    def test_the_names_are_the_ones_extract_and_the_worker_write(self):
        self.assertEqual(bootstrap_delivery.ITEMS_NAME, Path(inventory_findings.DEFAULT_ITEMS_PATH).name)
        self.assertEqual(bootstrap_delivery.SCORES_NAME, Path(inventory_findings.DEFAULT_SCORES_PATH).name)

    def test_every_cluster_is_registered_before_the_claim_with_its_scope(self):
        self._write()
        rc, out, _ = self._run()
        self.assertEqual((rc, out), (0, delivered("# Report\n")))
        self.assertEqual(
            self.sent,
            [
                (["api"], None, False),
                (["api", "web"], {"project": "acme", "cluster": "prod", "complete": True}, False),
            ],
        )

    def test_registration_records_that_the_fleet_was_checked(self):
        self._write()
        self._run()
        self.assertTrue((self.d / bootstrap_delivery.FINDINGS_REGISTERED_MARKER).exists())

    def test_a_clean_fleet_records_the_check_with_nothing_to_register(self):
        self._write(items={"items": []}, scores=None)
        self._run()
        self.assertEqual(self.sent, [])
        self.assertTrue((self.d / bootstrap_delivery.FINDINGS_REGISTERED_MARKER).exists())

    def test_a_queue_that_does_not_answer_records_no_check(self):
        self._write()
        with mock.patch.object(inventory_findings, "post_batch", side_effect=OSError("down")):
            self._run()
        self.assertFalse((self.d / bootstrap_delivery.FINDINGS_REGISTERED_MARKER).exists())

    def test_a_refused_batch_records_no_check(self):
        self._write()
        refused = urllib.error.HTTPError("http://q", 400, "bad", None, None)
        with mock.patch.object(inventory_findings, "post_batch", side_effect=refused):
            self._run()
        self.assertFalse((self.d / bootstrap_delivery.FINDINGS_REGISTERED_MARKER).exists())

    def test_the_check_marker_is_the_one_the_board_reads(self):
        import board_snapshot  # agents/platform/scripts, beside this script in the image

        self.assertEqual(bootstrap_delivery.FINDINGS_REGISTERED_MARKER, board_snapshot.FINDINGS_REGISTERED_MARKER)

    def test_a_hand_off_that_filed_no_ranking_card_registers_nothing(self):
        # A no-coverage sweep writes the report itself; the items and scores are the last sweep's.
        self._write()
        (self.d / bootstrap_handoff.HANDOFF_MARKER).write_text(
            f"sweep=t_sweep\ntask_id={bootstrap_handoff.NO_RANKING}\nfiled_at=1\n", encoding="utf-8"
        )
        rc, out, err = self._run()
        self.assertEqual((rc, out), (0, delivered("# Report\n")))
        self.assertEqual(self.sent, [])
        self.assertIn("filed no prioritization card", err)

    def test_an_unreadable_hand_off_marker_registers_nothing(self):
        self._write()
        (self.d / bootstrap_handoff.HANDOFF_MARKER).write_bytes(b"sweep=t_sweep\ntask_id=\xff\xfe\n")
        rc, out, err = self._run()
        self.assertEqual((rc, out), (0, delivered("# Report\n")))
        self.assertEqual(self.sent, [])
        self.assertIn("could not be read; registering nothing", err)

    def test_a_hand_off_that_filed_a_ranking_card_registers_its_batch(self):
        self._write()
        (self.d / bootstrap_handoff.HANDOFF_MARKER).write_text(
            "sweep=t_sweep\ntask_id=t_rank\nfiled_at=1\n", encoding="utf-8"
        )
        rc, out, _ = self._run()
        self.assertEqual((rc, out), (0, delivered("# Report\n")))
        self.assertEqual([objects for objects, _, _ in self.sent], [["api"], ["api", "web"]])

    def test_a_row_the_queue_reports_suppressed_is_not_marked_shown(self):
        # Sandboxed, `select` never learned the user dismissed it, so the report lists it.
        self._write()
        (self.d / SHOWN).write_text(json.dumps(SHOWN_RECORD), encoding="utf-8")
        dismissed = "sa-key.acme.prod.app.sa1"
        self.post.stop()
        with mock.patch.object(
            inventory_findings,
            "post_batch",
            side_effect=[{"results": [{"id": dismissed, "outcome": "suppressed"}]}, {"results": []}],
        ), mock.patch.object(bootstrap_delivery, "_post_surfaced") as mark:
            rc, out, err = self._run()
        self.post.start()
        self.assertEqual((rc, out), (0, delivered("# Report\n")))
        self.assertEqual(
            [call.args[1] for call in mark.call_args_list],
            ["sa-key.acme.prod.app.sa2", "crashloop.acme.prod.app.api"],
        )
        self.assertIn(f"not marking {dismissed} shown", err)

    def test_a_clean_fleet_registers_nothing_and_needs_no_scores(self):
        self._write(items={"items": []}, scores=None)
        rc, out, err = self._run()
        self.assertEqual((rc, out), (0, delivered("# Report\n")))
        self.assertEqual(self.sent, [])
        self.assertNotIn("registering nothing", err)

    def test_missing_or_unusable_files_register_nothing_and_deliver(self):
        cases = (
            (None, SCORED, "no INVENTORY.items.json"),
            (EXTRACTED, None, "no INVENTORY.scores.json"),
            ([1], SCORED, "registering nothing"),
            (EXTRACTED, {"scores": {"f001": SCORE}}, "unscored: f002, f003"),
        )
        for items, scores, expected in cases:
            with self.subTest(expected=expected):
                for name in (ITEMS, SCORES, COMPLETED):
                    (self.d / name).unlink(missing_ok=True)
                (self.d / INVENTORY).write_text("# Report\n", encoding="utf-8")
                self._write(items, scores)
                rc, out, err = self._run()
                self.assertEqual((rc, out), (0, delivered("# Report\n")))
                self.assertEqual(self.sent, [])
                self.assertIn(expected, err)

    def test_a_queue_that_does_not_answer_is_asked_once_and_the_report_still_goes_out(self):
        self._write()
        self.post.stop()
        with mock.patch.object(
            inventory_findings, "post_batch", side_effect=urllib.error.URLError("connection refused")
        ) as post:
            rc, out, err = self._run()
        self.post.start()
        self.assertEqual((rc, out), (0, delivered("# Report\n")))
        post.assert_called_once()
        self.assertIn("did not answer", err)

    def test_a_cluster_the_queue_refuses_does_not_stop_the_others(self):
        self._write()
        self.post.stop()
        refused = urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(b""))
        with mock.patch.object(inventory_findings, "post_batch", side_effect=[refused, {"results": []}]) as post:
            rc, out, err = self._run()
        self.post.start()
        self.assertEqual((rc, out), (0, delivered("# Report\n")))
        self.assertEqual(post.call_count, 2)
        self.assertIn("refused acme/dev's findings: 400", err)

    def test_a_racing_run_that_loses_the_claim_only_registers_again(self):
        self._write()
        self.assertTrue(bootstrap_delivery._claim_delivery(self.d))
        rc, out, _ = self._run()
        self.assertEqual((rc, out), (0, ""))


class RetireRunTest(unittest.TestCase):
    """The onboarding jobs are removed by a run after the one that delivers.

    Hermes discards the output of a run whose job is removed while it runs, so
    a delivery run that removed its own job would post nothing.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)
        patcher = mock.patch.object(sandbox_exec, "sandbox_enabled", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.removed = []
        self.fail_on = set()
        cron = types.ModuleType("cron")
        jobs = types.ModuleType("cron.jobs")
        jobs.remove_job = self._remove_job
        cron.jobs = jobs
        modules = mock.patch.dict(sys.modules, {"cron": cron, "cron.jobs": jobs})
        modules.start()
        self.addCleanup(modules.stop)

    def _remove_job(self, job_id):
        if job_id in self.fail_on:
            raise RuntimeError("store locked")
        self.removed.append(job_id)
        return True

    def _run(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = bootstrap_delivery.main(self.d)
        return rc, out.getvalue(), err.getvalue()

    def _age_claim(self, seconds):
        then = time.time() - seconds
        os.utime(self.d / COMPLETED, (then, then))

    def test_the_delivering_run_removes_no_job(self):
        (self.d / INVENTORY).write_text("# Report\n", encoding="utf-8")
        (self.d / ALIGNED).touch()
        rc, out, _ = self._run()
        self.assertEqual((rc, out), (0, delivered("# Report\n")))
        self.assertTrue((self.d / COMPLETED).exists())
        self.assertEqual(self.removed, [])

    def test_a_later_run_removes_both_jobs_delivery_last(self):
        (self.d / ALIGNED).touch()
        (self.d / COMPLETED).touch()
        self._age_claim(bootstrap_delivery.RETIRE_AFTER_SECONDS)
        rc, out, _ = self._run()
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(self.removed, [bootstrap_delivery.SCAN_JOB_ID, bootstrap_delivery.DELIVERY_JOB_ID])

    def test_a_fresh_claim_is_left_to_the_run_that_took_it(self):
        # A racing run that took the claim moments ago may still be delivering.
        (self.d / ALIGNED).touch()
        (self.d / COMPLETED).touch()
        self._age_claim(bootstrap_delivery.RETIRE_AFTER_SECONDS - 60)
        rc, out, _ = self._run()
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(self.removed, [])

    def test_a_failed_removal_is_reported_and_the_other_is_still_tried(self):
        (self.d / COMPLETED).touch()
        self._age_claim(bootstrap_delivery.RETIRE_AFTER_SECONDS)
        self.fail_on = {bootstrap_delivery.SCAN_JOB_ID}
        rc, out, err = self._run()
        self.assertEqual((rc, out), (0, ""))
        self.assertIn(f"could not remove {bootstrap_delivery.SCAN_JOB_ID}", err)
        self.assertEqual(self.removed, [bootstrap_delivery.DELIVERY_JOB_ID])

    def test_the_retire_run_reads_nothing_and_archives_nothing(self):
        (self.d / INVENTORY).write_text("# Another report\n", encoding="utf-8")
        (self.d / ALIGNED).touch()
        (self.d / COMPLETED).touch()
        self._age_claim(bootstrap_delivery.RETIRE_AFTER_SECONDS)
        with mock.patch.object(bootstrap_delivery, "_read_report") as read:
            rc, out, _ = self._run()
        self.assertEqual((rc, out), (0, ""))
        read.assert_not_called()
        self.assertTrue((self.d / INVENTORY).exists())


class DeliveryFromSandboxTest(unittest.TestCase):
    """With the shell sandbox on, the report is on the sandbox pod.

    The prioritization worker writes INVENTORY.md through its terminal, which
    is the sandbox; this pod holds only the markers. A report placed in the
    local data dir must not be what gets delivered.
    """

    SANDBOX_REPORT = "/opt/data/INVENTORY.md"
    SANDBOX_DELIVERED = "/opt/data/INVENTORY.delivered.md"
    SANDBOX_SHOWN = "/opt/data/INVENTORY.shown.json"
    SANDBOX_ITEMS = "/opt/data/INVENTORY.items.json"
    SANDBOX_SCORES = "/opt/data/INVENTORY.scores.json"
    _REAL_READ_BYTES = staticmethod(sandbox_exec.read_bytes)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)
        (self.d / ALIGNED).touch()
        # The report is the mock's return value; the files beside it are `self.beside`.
        self.shown = None
        self.beside = {}
        self.read = self._patch("read_bytes", return_value=b"# Sandbox report\n", side_effect=self._read)
        self.run_ = self._patch(
            "run", return_value=subprocess.CompletedProcess(["mv"], 0, stdout="", stderr="")
        )
        self._patch("sandbox_enabled", return_value=True)

    def _read(self, path, **_kw):
        if path == self.SANDBOX_REPORT:
            return mock.DEFAULT
        if path == self.SANDBOX_SHOWN:
            return self.shown
        return self.beside.get(path)

    def _patch(self, name, **kwargs):
        patcher = mock.patch.object(sandbox_exec, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def _run(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = bootstrap_delivery.main(self.d)
        return rc, out.getvalue(), err.getvalue()

    def test_delivers_the_sandbox_copy_and_archives_it_there(self):
        (self.d / INVENTORY).write_text("# Stale local copy\n", encoding="utf-8")
        rc, out, _ = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(out, delivered("# Sandbox report\n"))
        self.assertTrue((self.d / COMPLETED).exists())
        report_read, items_read, shown_read = self.read.call_args_list
        self.assertEqual(report_read.args, (self.SANDBOX_REPORT,))
        self.assertEqual(report_read.kwargs["max_bytes"], bootstrap_delivery.REPORT_MAX_BYTES + 1)
        self.assertEqual(items_read.args, (self.SANDBOX_ITEMS,))
        self.assertEqual(shown_read.args, (self.SANDBOX_SHOWN,))
        # The read runs as read_bytes' own default login; the rename as the
        # terminal's, which owns the sandbox's /opt/data.
        self.assertNotIn("principal", report_read.kwargs)
        # No shown file, so only the report is set aside.
        self.run_.assert_called_once()
        self.assertEqual(
            self.run_.call_args.args[0], ["/bin/mv", "-f", "--", self.SANDBOX_REPORT, self.SANDBOX_DELIVERED]
        )
        self.assertEqual(self.run_.call_args.kwargs["principal"], sandbox_exec.TERMINAL_PRINCIPAL)
        # The local copy is not the sandbox's to archive.
        self.assertTrue((self.d / INVENTORY).exists())
        self.assertFalse((self.d / DELIVERED).exists())

    def test_the_sandbox_shown_file_is_marked_and_archived_there(self):
        self.shown = json.dumps(SHOWN_RECORD).encode("utf-8")
        with mock.patch.object(bootstrap_delivery, "_post_surfaced") as post:
            rc, out, _ = self._run()
        self.assertEqual((rc, out), (0, delivered("# Sandbox report\n")))
        self.assertEqual(post.call_count, 3)
        self.assertEqual(
            [c.args[0] for c in self.run_.call_args_list],
            [
                ["/bin/mv", "-f", "--", self.SANDBOX_REPORT, self.SANDBOX_DELIVERED],
                ["/bin/mv", "-f", "--", self.SANDBOX_SHOWN, "/opt/data/INVENTORY.shown.delivered.json"],
            ],
        )
        self.assertEqual(
            {c.kwargs["principal"] for c in self.run_.call_args_list}, {sandbox_exec.TERMINAL_PRINCIPAL}
        )

    def test_the_sweeps_findings_are_read_from_the_sandbox_and_registered(self):
        self.beside = {
            self.SANDBOX_ITEMS: json.dumps(EXTRACTED).encode("utf-8"),
            self.SANDBOX_SCORES: json.dumps(SCORED).encode("utf-8"),
        }
        with mock.patch.object(inventory_findings, "post_batch") as post:
            rc, out, _ = self._run()
        self.assertEqual((rc, out), (0, delivered("# Sandbox report\n")))
        self.assertEqual(post.call_count, 2)
        reads = [c.args[0] for c in self.read.call_args_list]
        self.assertEqual(reads[1:3], [self.SANDBOX_ITEMS, self.SANDBOX_SCORES])
        self.assertEqual(
            {c.kwargs["max_bytes"] for c in self.read.call_args_list[1:3]}, {bootstrap_delivery.BATCH_MAX_BYTES + 1}
        )

    def test_an_unreachable_sandbox_after_the_claim_still_delivers(self):
        def read(path, **_kw):
            if path == self.SANDBOX_SHOWN:
                raise sandbox_exec.SandboxUnavailable("gone")
            return b"# Sandbox report\n"

        self.read.side_effect = read
        rc, out, err = self._run()
        self.assertEqual((rc, out), (0, delivered("# Sandbox report\n")))
        self.assertIn("could not read INVENTORY.shown.json", err)

    def test_no_human_means_no_sandbox_read(self):
        (self.d / ALIGNED).unlink()
        rc, out, _ = self._run()
        self.assertEqual((rc, out), (0, ""))
        self.read.assert_not_called()

    def test_already_delivered_means_no_sandbox_read(self):
        (self.d / COMPLETED).touch()
        rc, out, _ = self._run()
        self.assertEqual((rc, out), (0, ""))
        self.read.assert_not_called()

    def test_no_report_in_the_sandbox_is_a_silent_run(self):
        self.read.return_value = None
        rc, out, _ = self._run()
        self.assertEqual((rc, out), (0, ""))
        self.assertFalse((self.d / COMPLETED).exists())
        self.run_.assert_not_called()

    def test_an_unreachable_sandbox_is_a_silent_retry(self):
        # A non-zero exit becomes a failure alert in the user's chat on every
        # tick, and a sandbox restart is not the user's problem.
        for exc in (sandbox_exec.SandboxUnavailable("ssh: connect refused"),
                    subprocess.TimeoutExpired(["ssh"], 30)):
            with self.subTest(exc=type(exc).__name__):
                self.read.side_effect = exc
                rc, out, err = self._run()
                self.assertEqual((rc, out), (0, ""))
                self.assertIn("did not answer", err)
                self.assertFalse((self.d / COMPLETED).exists())
                self.run_.assert_not_called()

    def test_a_sandbox_report_that_cannot_be_read_is_a_failure(self):
        # The local path exits 1 on an unreadable report; the sandbox path must
        # not read the same state as "not written yet" and wait on it forever.
        self.read.side_effect = self._REAL_READ_BYTES
        for returncode, stderr in (
            (sandbox_exec._READ_UNREADABLE, ""),
            (sandbox_exec._READ_INCOMPLETE, "mktemp: No space left on device"),
        ):
            with self.subTest(returncode=returncode):
                self.run_.return_value = subprocess.CompletedProcess(
                    ["sh"], returncode, stdout="", stderr=stderr
                )
                rc, out, err = self._run()
                self.assertEqual((rc, out), (1, ""))
                self.assertIn("could not read INVENTORY.md", err)
                self.assertIn(self.SANDBOX_REPORT, err)
                self.assertFalse((self.d / COMPLETED).exists())

    def test_an_ssh_failure_the_transport_does_not_name_is_a_silent_retry(self):
        # A rolling sandbox is not the user's problem, whichever message ssh
        # prints for it.
        self.read.side_effect = self._REAL_READ_BYTES
        self.run_.return_value = subprocess.CompletedProcess(
            ["sh"], 255, stdout="", stderr="mux_client_request_session: read from master failed: Broken pipe"
        )
        rc, out, err = self._run()
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("did not answer", err)
        self.assertFalse((self.d / COMPLETED).exists())

    def test_nothing_at_the_sandbox_path_is_a_silent_run(self):
        self.read.side_effect = self._REAL_READ_BYTES
        self.run_.return_value = subprocess.CompletedProcess(
            ["sh"], sandbox_exec._READ_ABSENT, stdout="", stderr=""
        )
        rc, out, err = self._run()
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertFalse((self.d / COMPLETED).exists())

    def test_an_unreadable_sandbox_config_is_a_failure(self):
        sandbox_exec.sandbox_enabled.side_effect = sandbox_exec.SandboxMisconfigured("bad yaml")
        rc, out, _ = self._run()
        self.assertEqual((rc, out), (1, ""))
        self.read.assert_not_called()
        self.assertFalse((self.d / COMPLETED).exists())

    def test_a_sandbox_report_past_the_cap_is_refused(self):
        self.read.return_value = b"x" * (bootstrap_delivery.REPORT_MAX_BYTES + 1)
        rc, out, _ = self._run()
        self.assertEqual((rc, out), (1, ""))
        self.assertFalse((self.d / COMPLETED).exists())
        self.run_.assert_not_called()

    def test_bytes_that_are_not_utf8_are_still_delivered(self):
        self.read.return_value = b"caf\xe9 report\n"
        rc, out, _ = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(out, delivered("caf\ufffd report\n"))

    def test_a_failed_rename_still_counts_as_delivered(self):
        for outcome in (
            {"return_value": subprocess.CompletedProcess(["mv"], 1, stdout="", stderr="Permission denied")},
            {"side_effect": sandbox_exec.SandboxUnavailable("gone")},
        ):
            with self.subTest(outcome=outcome):
                (self.d / COMPLETED).unlink(missing_ok=True)
                self.run_.reset_mock(return_value=True, side_effect=True)
                self.run_.configure_mock(**outcome)
                rc, out, err = self._run()
                self.assertEqual((rc, out), (0, delivered("# Sandbox report\n")))
                self.assertTrue((self.d / COMPLETED).exists())
                self.assertIn("could not archive INVENTORY.md in the sandbox", err)

    def test_a_racing_run_does_not_touch_the_sandbox_report(self):
        self.assertTrue(bootstrap_delivery._claim_delivery(self.d))
        rc, out, _ = self._run()
        self.assertEqual((rc, out), (0, ""))
        self.run_.assert_not_called()


class ScanGateTest(unittest.TestCase):
    """The scan gate files the sweep as a kanban task for the `platform` profile.

    The Chat Agent profile this cron runs on holds no terminal/gcloud, so the
    sweep cannot execute here; the gate's whole job is to decide whether to file
    the card. It must stay silent on stdout either way — `deliver: local` plus
    empty output means the scheduler never posts anything for this job.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)
        self.filed = []
        self._orig = bootstrap_scan_gate.file_scan_task

        def _fake_file(data_dir):
            self.filed.append(1)
            bootstrap_scan_gate._mark_filed(data_dir, "t_test")
            return "t_test"

        bootstrap_scan_gate.file_scan_task = _fake_file
        # Where the pod keeps the profiles, relative to the data dir the gate is given.
        profiles = mock.patch.object(cluster_agent_profile, "PROFILES_BASE", self.d / "profiles")
        profiles.start()
        self.addCleanup(profiles.stop)

    def tearDown(self):
        bootstrap_scan_gate.file_scan_task = self._orig
        self._tmp.cleanup()

    def _run(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = bootstrap_scan_gate.main(self.d)
        return rc, buf.getvalue().strip()

    def _cluster_agent(self, project, cluster, location, registered=True, artifacts=SCAFFOLD_ARTIFACTS, name=None):
        # Named and stamped by the functions create_profile uses, so the fixture
        # carries the identity block the reconcile actually writes.
        name = name or cluster_agent_profile.profile_name(project, cluster, location)
        home = cluster_agent_profile.profile_home(name)
        home.mkdir(parents=True)
        cluster_agent_profile._inject_cluster_identity(home, project, cluster, location)
        if registered:
            (home / profile_scaffold.PROFILE_MARKER).write_text("")
        for artifact in artifacts:
            (home / artifact).write_text("")
        return name

    @staticmethod
    def _step_2(body):
        return body[body.index("**Step 2") : body.index("**Step 3")]

    def test_files_task_when_no_inventory(self):
        self.assertFalse(bootstrap_scan_gate.should_skip(self.d))
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.filed), 1)
        self.assertEqual(out, "")  # never speaks to the user

    def test_skips_when_inventory_present(self):
        (self.d / INVENTORY).write_text("x")
        self.assertTrue(bootstrap_scan_gate.should_skip(self.d))
        _, out = self._run()
        self.assertEqual(self.filed, [])
        self.assertEqual(out, "")

    def test_skips_when_completed(self):
        # Even after INVENTORY.md is removed at cleanup, completion keeps the
        # scan from being filed again.
        (self.d / COMPLETED).touch()
        self.assertTrue(bootstrap_scan_gate.should_skip(self.d))
        _, out = self._run()
        self.assertEqual(self.filed, [])
        self.assertEqual(out, "")

    def test_files_only_once_across_ticks(self):
        """The regression this whole marker exists for.

        The job runs every 60 seconds and the sweep takes minutes, so between
        filing the card and the report appearing there are many ticks in which
        neither INVENTORY.md nor .bootstrap_completed exists. Without a marker
        of its own the gate re-files a fleet-wide scan on every one of them.
        """
        for _ in range(5):
            self._run()
        self.assertEqual(len(self.filed), 1)

    def test_filed_marker_records_the_card(self):
        # Written with the card id so an operator debugging a stalled onboarding
        # knows which card to open, not merely that one exists somewhere.
        self._run()
        self.assertIn("t_test", (self.d / SCAN_FILED).read_text(encoding="utf-8"))

    def test_skips_while_sweep_is_in_flight(self):
        (self.d / SCAN_FILED).write_text("task_id=t_test\n")
        self.assertTrue(bootstrap_scan_gate.should_skip(self.d))
        _, out = self._run()
        self.assertEqual(self.filed, [])
        self.assertEqual(out, "")

    def test_no_marker_when_the_board_refuses_the_card(self):
        # A marker written after a failed create would silence discovery
        # forever — the one failure mode worse than repeating it.
        bootstrap_scan_gate.file_scan_task = self._orig
        self._run()  # hermes_cli.kanban is unavailable here, so the create fails
        self.assertFalse((self.d / SCAN_FILED).exists())
        self.assertFalse(bootstrap_scan_gate.should_skip(self.d))  # retries next tick

    def test_skips_while_prioritization_is_in_flight(self):
        # New window: the sweep is done and the report is not written yet.
        # INVENTORY.md alone no longer covers it, because ranking is its own
        # card and takes its own time.
        (self.d / "INVENTORY.raw.md").write_text("findings")
        self.assertTrue(bootstrap_scan_gate.should_skip(self.d))
        _, out = self._run()
        self.assertEqual(self.filed, [])
        self.assertEqual(out, "")

    def test_body_leaves_the_raw_file_and_ranking_to_the_hand_off(self):
        """The worker lists the fleet and completes; bootstrap_handoff.py does the rest.

        Asked to wait, compile and hand off, the worker had no tool that waits,
        typed the raw file without its findings block, and filed the ranking card
        without its key.
        """
        body = bootstrap_scan_gate._task_body()
        self.assertIn(f"do not write `{bootstrap_scan_gate.RAW_INVENTORY_PATH}`", body)
        self.assertIn("do not file a ranking card", body)
        self.assertIn("Do not wait for the per-cluster cards", body)
        self.assertNotIn("sleep 60", body)
        self.assertNotIn(bootstrap_scan_gate.PRIORITIZE_IDEMPOTENCY_KEY, body)

    def test_child_cards_are_pointed_at_the_per_cluster_audit_sop(self):
        """Without the SOP path the child body is written freehand.

        Observed: four per-cluster cards completed in under two minutes each,
        every one with no `metadata` at all, and the fleet report that followed
        named zero problems on a fleet that had them.
        """
        body = bootstrap_scan_gate._task_body()
        for path in bootstrap_scan_gate.CLUSTER_AUDIT_INSTRUCTIONS_PATHS:
            self.assertIn(path, body)

    def test_raw_and_delivered_paths_are_distinct(self):
        # Same file for both would make the delivery job fire on the unranked
        # sweep output — the pre-prioritization behaviour, silently restored.
        self.assertEqual(bootstrap_scan_gate.RAW_INVENTORY_PATH, "/opt/data/INVENTORY.raw.md")
        self.assertNotEqual(
            bootstrap_scan_gate.RAW_INVENTORY_PATH, bootstrap_scan_gate.INVENTORY_PATH
        )

    def test_card_is_idempotent_and_pins_absolute_output_path(self):
        # The key is the board-side backstop behind the filed marker; the
        # absolute path is what keeps the report in the Chat Agent's home where
        # the delivery job reads it.
        self.assertEqual(bootstrap_scan_gate.SCAN_IDEMPOTENCY_KEY, "bootstrap-inventory-scan")
        self.assertEqual(bootstrap_scan_gate.SCAN_ASSIGNEE, "platform")
        self.assertEqual(bootstrap_scan_gate.INVENTORY_PATH, "/opt/data/INVENTORY.md")
        self.assertIn("/opt/data/INVENTORY.md", bootstrap_scan_gate._task_body())

    def test_body_leaves_cluster_agents_to_the_gate_and_audits_the_rest(self):
        """The sweep must not leave a hole.

        Cluster Agents are pinned read-only to one cluster each. Which clusters
        get one is reconcile's decision, and the gate files their cards, so the
        body audits only what the roster missed.
        """
        body = bootstrap_scan_gate._task_body()
        self.assertIn(bootstrap_scan_gate.RECONCILE_SCRIPT_NAME, body)  # roster first
        self.assertIn("audit the clusters the list does not cover", body)  # no silent hole
        self.assertIn("do not fan out; the gate has", body)  # the gate files one card per cluster
        # The fleet list is how the hand-off names a cluster nobody reported on.
        self.assertIn("`fleet`", body)
        self.assertNotIn("aggregation card", body)
        self.assertIn("metadata", body)  # structured child results

    def test_step_2_lists_each_cluster_agent_the_gate_files(self):
        """The gate reads the roster because the sweep's worker cannot (#1872).

        The worker's terminal runs in the shell sandbox, which has no `hermes`,
        and whose /opt/data/profiles is a mirror without any config.yaml: read
        there, the roster is either an error or the mirror's directories.
        """
        short = self._cluster_agent("proj", "prod", "us-east4")
        # Long enough that profile_name() truncates and hashes it: the key is the
        # profile name, as the assignee is, not the stamped identity.
        hashed = self._cluster_agent(
            "proj", "a-cluster-name-long-enough-to-be-hashed", "us-central1-a"
        )
        self.assertNotIn("us-central1-a", hashed)
        step2 = self._step_2(bootstrap_scan_gate._task_body())
        prefix = bootstrap_scan_gate.CLUSTER_IDEMPOTENCY_KEY_PREFIX
        agents = {a["name"]: a for a in bootstrap_scan_gate.cluster_agents()}
        self.assertEqual(sorted(agents), sorted([short, hashed]))
        self.assertEqual(agents[short]["key"], f"{prefix}{short}")
        self.assertEqual(agents[hashed]["key"], f"{prefix}{hashed}")
        self.assertEqual(agents[short]["title"], "Report cluster inventory: `prod` (`proj`, `us-east4`)")
        self.assertIn(f"`{short}`", step2)
        self.assertIn(f"`{hashed}`", step2)
        self.assertNotIn("/opt/hermes", step2)
        self.assertNotIn("profile list", step2)

    def test_same_named_clusters_in_two_projects_get_distinct_keys(self):
        # The board matches an idempotency key alone, whatever the assignee, so a
        # shared key hands the second Cluster Agent the first one's card.
        self._cluster_agent("proj-a", "prod", "us-central1")
        self._cluster_agent("proj-b", "prod", "us-central1")
        keys = [a["key"] for a in bootstrap_scan_gate.cluster_agents()]
        self.assertEqual(len(keys), 2)
        self.assertEqual(len(set(keys)), 2)

    def test_identities_that_join_to_one_string_get_distinct_keys(self):
        # `proj-a`/`b` and `proj`/`a-b` hyphen-join to the same string, so a key
        # built from the identity gives both clusters one card. profile_name()
        # joins them the same way; the second profile here stands for one whose
        # directory name differs, which the key must follow.
        first = self._cluster_agent("proj-a", "b", "us-central1")
        second = self._cluster_agent("proj", "a-b", "us-central1", name="cluster-proj-a-b-us-central1-2")
        prefix = bootstrap_scan_gate.CLUSTER_IDEMPOTENCY_KEY_PREFIX
        self.assertEqual(
            [a["key"] for a in bootstrap_scan_gate.cluster_agents()], [f"{prefix}{first}", f"{prefix}{second}"]
        )

    def test_step_2_leaves_out_what_is_not_a_cluster_agent(self):
        # `default` and `platform` share the directory; stamped and scaffolded here so
        # that only the reserved-name rule can be what drops them. An unstamped profile
        # has no cluster to name on its card, and the reconcile skips it for the same reason.
        for reserved, cluster in (("default", "a"), ("platform", "b")):
            home = self.d / "profiles" / reserved
            home.mkdir(parents=True)
            cluster_agent_profile._inject_cluster_identity(home, "p", cluster, "l")
            for marker in (profile_scaffold.PROFILE_MARKER, *SCAFFOLD_ARTIFACTS):
                (home / marker).write_text("")
        unstamped = self.d / "profiles" / "cluster-p-unstamped-l"
        unstamped.mkdir()
        for marker in (profile_scaffold.PROFILE_MARKER, *SCAFFOLD_ARTIFACTS):
            (unstamped / marker).write_text("")
        with contextlib.redirect_stderr(io.StringIO()):
            step2 = self._step_2(bootstrap_scan_gate._task_body())
            self.assertEqual(bootstrap_scan_gate.cluster_agents(), [])
        self.assertIn("    (none)", step2)
        self.assertIn("If the list above is `(none)`", step2)

    def test_the_sweep_is_told_not_to_file_cluster_cards(self):
        # The gate files them: asked to make the calls itself, the worker once
        # completed saying it had fanned out to seven clusters and had filed none.
        self._cluster_agent("proj", "prod", "us-east4")
        step2 = self._step_2(bootstrap_scan_gate._task_body())
        self.assertIn("Do not create cluster cards", step2)
        self.assertNotIn("kanban_create(", step2)

    def test_step_2_leaves_out_a_profile_whose_scaffold_did_not_finish(self):
        # Hermes never registered the first, so a card assigned to it is never
        # dispatched; the second stopped before USER.md, so its worker blocks at
        # preflight. platform_control's list_cluster_profiles leaves out both.
        ready = self._cluster_agent("proj", "prod", "us-east4")
        unregistered = self._cluster_agent("proj", "stray", "us-east4", registered=False)
        half_built = self._cluster_agent("proj", "half", "us-east4", artifacts=())
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            names = [a["name"] for a in bootstrap_scan_gate.cluster_agents()]
        self.assertEqual(names, [ready])
        self.assertIn(f"{unregistered}: scaffold not finished", stderr.getvalue())
        self.assertIn(f"{half_built}: scaffold not finished", stderr.getvalue())

    def test_an_unreadable_roster_files_the_solo_sweep(self):
        # A scripts directory without cluster_agent_profile must not fail the cron
        # run. The card then reads as having no Cluster Agents, the same answer the
        # gate gives when the reconcile script itself is absent.
        with mock.patch.dict(sys.modules, {"cluster_agent_profile": None}), \
                contextlib.redirect_stderr(io.StringIO()):
            step2 = self._step_2(bootstrap_scan_gate._task_body())
        self.assertIn("    (none)", step2)

    def test_an_unreadable_profile_does_not_drop_the_others(self):
        # A config.yaml that parses to a list makes read_cluster_identity raise.
        good = self._cluster_agent("proj", "prod", "us-east4")
        bad = cluster_agent_profile.profile_home("cluster-broken")
        bad.mkdir(parents=True)
        for marker in (profile_scaffold.PROFILE_MARKER, *SCAFFOLD_ARTIFACTS):
            (bad / marker).write_text("")
        (bad / "config.yaml").write_text("- a\n")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            names = [a["name"] for a in bootstrap_scan_gate.cluster_agents()]
        self.assertEqual(names, [good])
        self.assertIn("cluster-broken: ", stderr.getvalue())
        self.assertNotIn("cluster-broken: scaffold not finished", stderr.getvalue())

    def test_a_profile_the_gate_cannot_stat_does_not_drop_the_others(self):
        # Python 3.11 to 3.13, the image's included, re-raise an is_file() that
        # fails with EACCES; 3.14 answers False, so mode bits alone would not
        # reach the raise on every interpreter this suite runs under.
        good = self._cluster_agent("proj", "prod", "us-east4")
        locked = self._cluster_agent("proj", "locked", "us-east4")
        locked_home = cluster_agent_profile.profile_home(locked)
        is_file = Path.is_file

        def _is_file(path):
            if locked_home in path.parents:
                raise PermissionError(errno.EACCES, "Permission denied", str(path))
            return is_file(path)

        stderr = io.StringIO()
        with mock.patch.object(Path, "is_file", _is_file), contextlib.redirect_stderr(stderr):
            names = [a["name"] for a in bootstrap_scan_gate.cluster_agents()]
        self.assertEqual(names, [good])
        self.assertIn(f"skipping Cluster Agent {locked}: [Errno {errno.EACCES}] Permission denied", stderr.getvalue())

    def test_the_card_lists_the_roster_the_reconcile_left(self):
        """The roster is read after the gate's own reconcile, not before it.

        On a fresh install no profile exists until that reconcile creates it, so
        a list taken any earlier is empty and no Cluster Agent card is filed.
        """
        bootstrap_scan_gate.file_scan_task = self._orig
        script = self.d / "scripts" / bootstrap_scan_gate.RECONCILE_SCRIPT_NAME
        script.parent.mkdir()
        script.touch()
        created = []

        def fake_reconcile(cmd, **kwargs):
            created.append(self._cluster_agent("proj", "prod", "us-east4"))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        filed = []
        kanban = types.ModuleType("hermes_cli.kanban")
        kanban.run_slash = lambda cmd: filed.append(cmd) or '{"id": "t_real"}'
        modules = {"hermes_cli": types.ModuleType("hermes_cli"), "hermes_cli.kanban": kanban}
        with mock.patch.object(bootstrap_scan_gate.subprocess, "run", fake_reconcile), \
                mock.patch.dict(sys.modules, modules), \
                contextlib.redirect_stderr(io.StringIO()):
            self._run()
        self.assertEqual(len(filed), 1)
        args = shlex.split(filed[0])
        self.assertIn(f"`{created[0]}`", args[args.index("--body") + 1])

    def test_body_forbids_improvising_around_a_failed_step(self):
        """The 32-call roster loop is what this prevents.

        When the roster command returned 127 the worker did not stop: it tried ls,
        sqlite3, four python sqlite attempts against three databases, five metadata
        server probes, and re-ran the reconcile script five times — half the sweep,
        for an answer ("no cluster agents") that is also the safe default.
        """
        body = bootstrap_scan_gate._task_body()
        self.assertIn("treat its answer as empty", body)
        self.assertIn("do not improvise", body.lower())
        self.assertIn("Discovery steps run ONCE", body)

    def test_reconcile_runs_before_the_sweep_is_filed(self):
        """A sweep filed against a not-yet-reconciled roster gets no Cluster Agent cards.

        `cluster-agent-reconcile` is on `11 * * * *` and this gate is on
        `* * * * *`, so on a fresh install the gate reaches an empty roster up to
        59 minutes before anything populates it — and the marker it writes makes
        that solo sweep the only one that ever runs.
        """
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(bootstrap_scan_gate.subprocess, "run", fake_run), \
                mock.patch.object(Path, "exists", lambda self: True):
            self.assertTrue(bootstrap_scan_gate.ensure_cluster_agents(self.d))
        self.assertEqual(len(calls), 1)
        # Resolved under the data dir, not a hardcoded /opt/data: `agentHome`
        # moves the tree, and a path that misses silently files a solo sweep.
        self.assertIn(str(self.d / "scripts" / bootstrap_scan_gate.RECONCILE_SCRIPT_NAME), calls[0])

    def test_the_gate_asks_the_reconcile_for_an_exit_code_that_means_something(self):
        """Without `--require-create-pass` the exit code is always 0.

        The script is a cron producer and swallows every failure — a `gcloud
        container clusters list` that 403s is logged and the run exits 0. The gate
        would then reset the attempt count and file a solo sweep that
        `.bootstrap_scan_filed` makes permanent, which is the failure this whole
        arm exists to prevent.
        """
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(bootstrap_scan_gate.subprocess, "run", fake_run), \
                mock.patch.object(Path, "exists", lambda self: True):
            bootstrap_scan_gate.ensure_cluster_agents(self.d)
        self.assertIn("--require-create-pass", calls[0])

    def test_a_failed_reconcile_defers_the_sweep_without_marking_it(self):
        # EXIT_CREATE_PASS_SKIPPED: the CREATE direction did not run, so the roster
        # is not reconciled and the sweep must not be filed against it.
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 3, "", "boom")

        with mock.patch.object(bootstrap_scan_gate.subprocess, "run", fake_run), \
                mock.patch.object(Path, "exists", lambda self: True):
            self.assertFalse(bootstrap_scan_gate.ensure_cluster_agents(self.d))

    def test_reconcile_stops_blocking_onboarding_after_repeated_failure(self):
        """A reconcile that can never succeed must not hold onboarding shut.

        No IAM to list clusters is a permanent condition on some installs. A
        solo sweep is a worse report; no report at all is none.
        """
        stale = time.time() - bootstrap_scan_gate.RECONCILE_GIVE_UP_SECONDS - 1
        (self.d / bootstrap_scan_gate.RECONCILE_ATTEMPTS_MARKER).write_text(
            f"{bootstrap_scan_gate.MAX_RECONCILE_ATTEMPTS}\n{stale}\n"
        )
        with mock.patch.object(Path, "exists", lambda self: True):
            self.assertTrue(bootstrap_scan_gate.ensure_cluster_agents(self.d))

    def test_the_attempt_ceiling_alone_does_not_give_up_in_the_first_minutes(self):
        """The count is exhausted but the streak is young: keep waiting.

        The gate ticks every minute with no backoff, so five attempts is five
        minutes — and IAM propagation on a fresh install routinely takes longer
        than that. Giving up there files the solo sweep that
        ``.bootstrap_scan_filed`` makes permanent.
        """
        (self.d / bootstrap_scan_gate.RECONCILE_ATTEMPTS_MARKER).write_text(
            f"{bootstrap_scan_gate.MAX_RECONCILE_ATTEMPTS}\n{time.time()}\n"
        )

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 3, "", "no IAM to list clusters")

        with mock.patch.object(bootstrap_scan_gate.subprocess, "run", fake_run), \
                mock.patch.object(Path, "exists", lambda self: True):
            self.assertFalse(bootstrap_scan_gate.ensure_cluster_agents(self.d))

    def test_a_counter_written_before_the_clock_existed_still_gives_up(self):
        # An install upgraded mid-streak carries a single-line marker. It must not
        # win another 30 minutes of waiting from the upgrade alone.
        (self.d / bootstrap_scan_gate.RECONCILE_ATTEMPTS_MARKER).write_text(
            f"{bootstrap_scan_gate.MAX_RECONCILE_ATTEMPTS}\n"
        )
        with mock.patch.object(Path, "exists", lambda self: True):
            self.assertTrue(bootstrap_scan_gate.ensure_cluster_agents(self.d))

    def test_the_first_failure_stamps_the_streak_start(self):
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 3, "", "boom")

        with mock.patch.object(bootstrap_scan_gate.subprocess, "run", fake_run), \
                mock.patch.object(Path, "exists", lambda self: True):
            bootstrap_scan_gate.ensure_cluster_agents(self.d)
            first = bootstrap_scan_gate._reconcile_since(self.d)
            self.assertIsNotNone(first)
            # A later tick extends the streak rather than restarting its clock.
            bootstrap_scan_gate.ensure_cluster_agents(self.d)
        self.assertEqual(bootstrap_scan_gate._reconcile_since(self.d), first)
        self.assertEqual(bootstrap_scan_gate._reconcile_attempts(self.d), 2)

    def test_a_reconcile_that_times_out_defers_the_sweep(self):
        # subprocess.run raising is the timeout path; it must read as "not yet",
        # not as "no Cluster Agents exist".
        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, bootstrap_scan_gate.RECONCILE_TIMEOUT_SECONDS)

        with mock.patch.object(bootstrap_scan_gate.subprocess, "run", fake_run), \
                mock.patch.object(Path, "exists", lambda self: True):
            self.assertFalse(bootstrap_scan_gate.ensure_cluster_agents(self.d))
        self.assertEqual(bootstrap_scan_gate._reconcile_attempts(self.d), 1)

    def test_the_reconcile_ceiling_follows_the_declared_cap(self):
        # The ceiling is the reconcile's own listing budget for spec.scope.maxProjects plus
        # the settle time, read through the reconcile's functions; without a declaration to
        # read it is the constant, which is the default cap's value.
        import cluster_agent_reconcile as rec
        seen: dict = {}

        def fake_run(cmd, **kwargs):
            seen["timeout"] = kwargs.get("timeout")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(rec.SCOPE_FILE_ENV, None)
            self.assertEqual(bootstrap_scan_gate._reconcile_timeout_seconds(), bootstrap_scan_gate.RECONCILE_TIMEOUT_SECONDS)
            scope = self.d / "scope.json"
            scope.write_text('{"present": true, "projects": [], "maxProjects": 500}')
            os.environ[rec.SCOPE_FILE_ENV] = str(scope)
            expected = int(rec._list_budget_seconds(500) + rec._prune_budget_seconds(500, 0)) + bootstrap_scan_gate.RECONCILE_SETTLE_SECONDS
            self.assertGreater(expected, bootstrap_scan_gate.RECONCILE_TIMEOUT_SECONDS)
            self.assertEqual(bootstrap_scan_gate._reconcile_timeout_seconds(), expected)
            with mock.patch.object(bootstrap_scan_gate.subprocess, "run", fake_run), \
                    mock.patch.object(Path, "exists", lambda self: True):
                bootstrap_scan_gate.ensure_cluster_agents(self.d)
            self.assertEqual(seen["timeout"], expected)
            # The profiles on the volume feed the prune budget: 200 of them at the default
            # cap lift the ceiling past the constant by the describes they cost.
            on_volume = self.d / "profiles"
            for i in range(200):
                (on_volume / f"cluster-{i}").mkdir(parents=True)
            scope.write_text('{"present": true, "projects": [], "maxProjects": 100}')
            with mock.patch.object(cluster_agent_profile, "PROFILES_BASE", on_volume):
                with_profiles = int(rec._list_budget_seconds(100) + rec._prune_budget_seconds(100, 200)) + bootstrap_scan_gate.RECONCILE_SETTLE_SECONDS
                self.assertGreater(with_profiles, bootstrap_scan_gate.RECONCILE_TIMEOUT_SECONDS)
                self.assertEqual(bootstrap_scan_gate._reconcile_timeout_seconds(), with_profiles)
            shutil.rmtree(on_volume)
            scope.write_text('{"present": true, "projects": [], "maxProjects": 100}')
            self.assertEqual(bootstrap_scan_gate._reconcile_timeout_seconds(), bootstrap_scan_gate.RECONCILE_TIMEOUT_SECONDS)
            self.assertEqual(int(rec._list_budget_seconds(rec.RESOLVED_SET_CAP) + rec._prune_budget_seconds(rec.RESOLVED_SET_CAP, 0))
                             + bootstrap_scan_gate.RECONCILE_SETTLE_SECONDS, bootstrap_scan_gate.RECONCILE_TIMEOUT_SECONDS)

    def test_a_successful_reconcile_clears_the_attempt_count(self):
        (self.d / bootstrap_scan_gate.RECONCILE_ATTEMPTS_MARKER).write_text("3")

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(bootstrap_scan_gate.subprocess, "run", fake_run), \
                mock.patch.object(Path, "exists", lambda self: True):
            self.assertTrue(bootstrap_scan_gate.ensure_cluster_agents(self.d))
        self.assertEqual(bootstrap_scan_gate._reconcile_attempts(self.d), 0)

    def test_a_concurrent_reconcile_defers_the_sweep(self):
        # The gate fires every minute and a reconcile takes tens of seconds, so overlap
        # is the normal case. Mutual exclusion lives in the reconcile script, which the
        # hourly cron job also runs; the gate only has to read its refusal.
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd, bootstrap_scan_gate.RECONCILE_ALREADY_RUNNING, "", "")

        with mock.patch.object(bootstrap_scan_gate.subprocess, "run", fake_run), \
                mock.patch.object(Path, "exists", lambda self: True):
            self.assertFalse(bootstrap_scan_gate.ensure_cluster_agents(self.d))

    def test_losing_the_race_does_not_spend_an_attempt(self):
        # Contention says nothing about the roster. Counting it would spend the ceiling
        # that exists for a reconcile which genuinely cannot succeed.
        started = time.time()
        bootstrap_scan_gate._record_reconcile_attempt(self.d, 2, started)

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd, bootstrap_scan_gate.RECONCILE_ALREADY_RUNNING, "", "")

        with mock.patch.object(bootstrap_scan_gate.subprocess, "run", fake_run), \
                mock.patch.object(Path, "exists", lambda self: True):
            self.assertFalse(bootstrap_scan_gate.ensure_cluster_agents(self.d))
        self.assertEqual(bootstrap_scan_gate._reconcile_attempts(self.d), 2)
        self.assertEqual(bootstrap_scan_gate._reconcile_since(self.d), started)

    def test_body_forbids_creating_profiles_by_hand(self):
        """The regression that made the first roster fix worse than the bug.

        reconcile is permanently prune-only on any deployment where it cannot
        list clusters (it runs with a cwd outside CREDENTIAL_PROXY_WORKSPACE_ROOT
        and the gcloud call 403s), so the roster is always empty. Told that
        reconcile "ensures every managed cluster has an agent", the worker read an
        empty roster as damage and called cluster_agent_profile.py create directly
        — around the management-cluster guard that only lives inside reconcile.
        The next reconcile run pruned it. Create, prune, repeat: arm 1b ran 102
        shell calls against arm 1a's 65.
        """
        body = bootstrap_scan_gate._task_body()
        self.assertIn("do not repair or delete a profile", body)
        self.assertIn("may immediately prune", body)
        self.assertIn("not yours to fix", body)

    def test_the_body_does_not_promise_the_roster_was_reconciled(self):
        # The gate files the card on the give-up path too, after MAX_RECONCILE_ATTEMPTS
        # failures over RECONCILE_GIVE_UP_SECONDS. A body asserting the roster is current
        # is false there, and it tells the worker to trust a roster that may be empty —
        # then `.bootstrap_scan_filed` makes the thin report permanent.
        body = bootstrap_scan_gate._task_body()
        self.assertNotIn("already reconciled", body)
        self.assertNotIn("exited 0", body)
        self.assertIn("may be empty or incomplete", body)
        # The degradation has to reach the user: the report is delivered verbatim.
        self.assertIn("names each one as lacking an agent", body)
        self.assertIn("complete the card anyway", body)

    def test_body_degrades_when_no_cluster_agents_exist(self):
        # A single-cluster install reconciles to an empty roster, and that is the
        # supported answer. The same card must still produce a report there rather
        # than fanning out to an empty roster and writing nothing.
        body = bootstrap_scan_gate._task_body()
        self.assertIn("there are no Cluster Agents", body)
        self.assertIn("audit every cluster yourself in Step 3", body)
        # The workload checks live only in the single-cluster SOP now, so the solo
        # walk has to be sent there or it produces a topology table with empty
        # workload columns — the empty report this card exists to prevent.
        self.assertIn("single-cluster audit SOP", body)
        # Topology is that SOP's Step 2, so a range starting at 3 leaves the fleet
        # table's K8s version, node pool and Workload Identity columns unsourced on
        # the one path where no Cluster Agent supplies them.
        self.assertIn("Steps 2 to 4 of the single-cluster audit SOP", body)

    def test_cluster_cards_are_keyed_by_profile_name(self):
        # The gate re-files a missing card every tick until the hand-off; the
        # key is what keeps a repeat from duplicating one.
        name = self._cluster_agent("proj", "prod", "us-east4")
        keys = [a["key"] for a in bootstrap_scan_gate.cluster_agents()]
        self.assertEqual(keys, [f"{bootstrap_scan_gate.CLUSTER_IDEMPOTENCY_KEY_PREFIX}{name}"])

    def test_a_filed_sweep_runs_the_hand_off_instead_of_filing(self):
        (self.d / SCAN_FILED).write_text("task_id=t_sweep\nfiled_at=1\n")
        with mock.patch.object(bootstrap_scan_gate.bootstrap_handoff, "hand_off") as hand_off:
            rc, out = self._run()
        self.assertEqual((rc, out, self.filed), (0, "", []))
        hand_off.assert_called_once_with(
            self.d, self.d / SCAN_FILED, bootstrap_scan_gate._parse_task_id, roster=bootstrap_scan_gate.cluster_agents
        )

    def test_filing_the_sweep_files_the_cluster_cards_in_the_same_tick(self):
        with mock.patch.object(bootstrap_scan_gate.bootstrap_handoff, "hand_off") as hand_off:
            self._run()
        self.assertEqual(len(self.filed), 1)
        hand_off.assert_called_once()
        self.assertIs(hand_off.call_args.kwargs["roster"], bootstrap_scan_gate.cluster_agents)

    def test_a_failing_hand_off_does_not_fail_the_cron_run(self):
        (self.d / SCAN_FILED).write_text("task_id=t_sweep\nfiled_at=1\n")
        with mock.patch.object(bootstrap_scan_gate.bootstrap_handoff, "hand_off", side_effect=TypeError("bad metadata")):
            rc, out = self._run()
        self.assertEqual((rc, out), (0, ""))

    def test_no_hand_off_once_the_report_is_delivered(self):
        (self.d / SCAN_FILED).write_text("task_id=t_sweep\nfiled_at=1\n")
        (self.d / COMPLETED).write_text("")
        with mock.patch.object(bootstrap_scan_gate.bootstrap_handoff, "hand_off") as hand_off:
            self._run()
        hand_off.assert_not_called()

    def test_parses_task_id_from_either_response_shape(self):
        # --json is what we ask for, but run_slash hands back stdout and stderr
        # together, and an older board may not support --json on create at all.
        parse = bootstrap_scan_gate._parse_task_id
        self.assertEqual(parse('{"id": "t_abc", "status": "ready"}'), "t_abc")
        self.assertEqual(parse('warning: noise\n{"id": "t_abc"}\n'), "t_abc")
        self.assertEqual(parse("Created t_abc  (ready, assignee=platform)"), "t_abc")
        self.assertIsNone(parse("kanban create: board unavailable"))


class ExcludedClustersParagraphTest(unittest.TestCase):
    """#3081: the sweep is told which clusters the install excludes, so its
    "audit every cluster with no Cluster Agent" does not reach exactly those.
    Read as the fleet_scope tool reads them: the snapshot, else the render."""

    EXCLUDE = {"projects": [], "clusters": [
        {"projectId": "ka-test", "location": "us-central1", "clusterName": "lane-b"},
        {"projectId": "ka-test", "location": "us-east1-b", "clusterName": "kyber-test"},
        {"projectId": "ka-test", "location": "us-central1"},
    ]}

    def setUp(self):
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("KUBEAGENTS_SCOPE_FILE", None)

    def _dir(self, snapshot=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data_dir = Path(tmp.name)
        if snapshot is not None:
            (data_dir / bootstrap_scan_gate.SCOPE_SNAPSHOT_NAME).write_text(snapshot, encoding="utf-8")
        return data_dir

    def _snapshot(self, exclude):
        return json.dumps({"declared": {"projects": [], "folders": [], "organizations": [], "sharedVpcHosts": [],
                                        "metricsScopes": [], "exclude": exclude},
                           "projects": [{"id": "ka-test", "via": ["management"], "outcome": "ok", "state": "in-scope"}],
                           "present": True, "boundary": True})

    def test_the_excluded_clusters_are_named_and_kept_out_of_step_3(self):
        data_dir = self._dir(self._snapshot(self.EXCLUDE))
        paragraph = bootstrap_scan_gate._excluded_clusters_paragraph(data_dir)
        self.assertIn("`ka-test/us-central1/lane-b`, `ka-test/us-east1-b/kyber-test`", paragraph)
        self.assertIn("do not audit them in Step 3", paragraph)
        self.assertNotIn("ka-test/us-central1`", paragraph)
        with mock.patch.object(bootstrap_scan_gate, "_data_dir", return_value=data_dir), \
                mock.patch.object(bootstrap_scan_gate, "cluster_agents", return_value=[]):
            self.assertIn(paragraph, bootstrap_scan_gate._task_body())

    def test_with_no_snapshot_the_render_s_exclusions_are_named(self):
        # The gate files the sweep without a snapshot when it gives up on the
        # reconcile; the operator's render still names the exclusions.
        data_dir = self._dir()
        render = data_dir / "scope.json"
        render.write_text(json.dumps({"present": True, "projects": [], "exclude": self.EXCLUDE}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"KUBEAGENTS_SCOPE_FILE": str(render)}):
            paragraph = bootstrap_scan_gate._excluded_clusters_paragraph(data_dir)
        self.assertIn("`ka-test/us-central1/lane-b`", paragraph)

    def test_an_unreadable_render_sends_the_worker_to_the_tool(self):
        data_dir = self._dir()
        with mock.patch.dict(os.environ, {"KUBEAGENTS_SCOPE_FILE": str(data_dir / "absent.json")}):
            paragraph = bootstrap_scan_gate._excluded_clusters_paragraph(data_dir)
        self.assertIn("could not be read", paragraph)
        self.assertIn("`fleet_scope`", paragraph)

    def test_no_scope_or_no_exclusion_names_nothing(self):
        self.assertEqual(bootstrap_scan_gate._excluded_clusters_paragraph(self._dir()), "")
        self.assertEqual(bootstrap_scan_gate._excluded_clusters_paragraph(self._dir(self._snapshot({"projects": [], "clusters": []}))), "")


class ScopeGapParagraphTest(unittest.TestCase):
    """The sweep is told which projects in scope the reconcile could not list."""

    def _with_snapshot(self, snapshot):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data_dir = Path(tmp.name)
        if snapshot is not None:
            (data_dir / bootstrap_scan_gate.SCOPE_SNAPSHOT_NAME).write_text(snapshot, encoding="utf-8")
        return data_dir

    def test_no_snapshot_names_nothing(self):
        self.assertEqual(bootstrap_scan_gate._scope_gap_paragraph(self._with_snapshot(None)), "")

    def test_an_unreadable_snapshot_names_nothing(self):
        self.assertEqual(bootstrap_scan_gate._scope_gap_paragraph(self._with_snapshot("{nope")), "")

    def test_the_task_body_speaks_of_one_project_unless_the_snapshot_names_more(self):
        # An install with no scope renders the prompt it rendered before scopes existed.
        data_dir = self._with_snapshot(None)
        with mock.patch.object(bootstrap_scan_gate, "_data_dir", return_value=data_dir):
            body = bootstrap_scan_gate._task_body()
        self.assertIn("Audit every cluster the project has", body)
        self.assertIn("If you cannot list the project's clusters at all", body)
        self.assertIn("holds the `RECONCILE_EXCLUDE` opt-out and the create/prune rules", body)
        self.assertNotIn("projects in scope", body)
        self.assertNotIn("the scope and its exclusions", body)
        data_dir = self._with_snapshot('{"projects": [{"id": "mgmt", "outcome": "ok"}, {"id": "other", "outcome": "ok"}]}')
        with mock.patch.object(bootstrap_scan_gate, "_data_dir", return_value=data_dir):
            body = bootstrap_scan_gate._task_body()
        self.assertIn("Audit every cluster the projects in scope have", body)
        self.assertIn("If you cannot list a project's clusters at all", body)
        self.assertIn("holds the scope and its exclusions and the create/prune rules", body)

    def test_the_paragraph_names_the_container_and_counts_members_past_a_limit(self):
        import json as _json
        rows = [{"id": "mgmt", "outcome": "ok"}] + [{"id": f"p-{i:03d}", "outcome": "over-cap", "via": ["folders/9"]} for i in range(25)]
        snap = _json.dumps({"projects": rows, "containers": [{"id": "folders/9", "outcome": "over-cap", "projects": 25}]})
        paragraph = bootstrap_scan_gate._scope_gap_paragraph(self._with_snapshot(snap))
        # Over-cap is a successful lookup past the cap, named as such, never as unresolved.
        self.assertNotIn("could not resolve", paragraph)
        self.assertIn("It resolved `folders/9` (25 project(s)) past its listing cap", paragraph)
        self.assertIn("name the container as over the cap", paragraph)
        self.assertEqual(paragraph.count("`p-"), bootstrap_scan_gate.SCOPE_GAP_NAMED_LIMIT)
        self.assertIn(f"and {25 - bootstrap_scan_gate.SCOPE_GAP_NAMED_LIMIT} more", paragraph)
        self.assertLess(len(paragraph), 2500)

    def test_a_declared_container_makes_the_prompt_multi_project_even_with_one_row(self):
        snap = '{"projects": [{"id": "mgmt", "outcome": "ok"}], "containers": [{"id": "folders/9", "outcome": "unreachable", "projects": 0}]}'
        data_dir = self._with_snapshot(snap)
        with mock.patch.object(bootstrap_scan_gate, "_data_dir", return_value=data_dir):
            body = bootstrap_scan_gate._task_body()
        self.assertIn("Audit every cluster the projects in scope have", body)
        self.assertIn("could not resolve `folders/9`", body)

    def test_an_unresolved_container_with_no_members_is_still_named(self):
        # First run under a declaration whose folder could not be resolved: no member rows,
        # but a whole folder is missing from the roster and the sweep must hear it.
        snap = '{"projects": [{"id": "mgmt", "outcome": "ok"}, {"id": "p2", "outcome": "ok"}], "containers": [{"id": "folders/9", "outcome": "unreachable", "projects": 0}]}'
        paragraph = bootstrap_scan_gate._scope_gap_paragraph(self._with_snapshot(snap))
        self.assertIn("could not resolve `folders/9` (unreachable, 0 project(s) carried)", paragraph)
        self.assertIn("Every project it did resolve was listed.", paragraph)
        # And a single-project install with a resolved container and nothing unlisted stays silent.
        snap = '{"projects": [{"id": "mgmt", "outcome": "ok"}], "containers": [{"id": "folders/9", "outcome": "ok", "projects": 0}]}'
        self.assertEqual(bootstrap_scan_gate._scope_gap_paragraph(self._with_snapshot(snap)), "")

    def test_a_single_project_install_gets_no_paragraph_whatever_its_outcome(self):
        # The give-up path on a scope-less install: one project, not ok. The prompt stays main's.
        data_dir = self._with_snapshot('{"projects": [{"id": "mgmt", "outcome": "unreachable"}]}')
        self.assertEqual(bootstrap_scan_gate._scope_gap_paragraph(data_dir), "")
        with mock.patch.object(bootstrap_scan_gate, "_data_dir", return_value=data_dir):
            body = bootstrap_scan_gate._task_body()
        self.assertNotIn("projects in scope", body)
        self.assertIn("Audit every cluster the project has", body)

    def test_a_snapshot_whose_projects_is_not_a_list_names_nothing(self):
        for body in ('{"projects": null}', '{"projects": 3}', '{"projects": "x"}', '[1]'):
            self.assertEqual(bootstrap_scan_gate._scope_gap_paragraph(self._with_snapshot(body)), "", body)

    def test_all_ok_names_nothing(self):
        snap = '{"projects": [{"id": "a", "outcome": "ok"}, {"id": "b", "outcome": "ok"}]}'
        self.assertEqual(bootstrap_scan_gate._unlisted_projects(self._with_snapshot(snap)), [])

    def test_unlisted_projects_are_named_with_their_outcome_in_sorted_order(self):
        snap = ('{"projects": [{"id": "zeta", "outcome": "denied"}, {"id": "ok-one", "outcome": "ok"},'
                ' {"id": "alpha", "outcome": "unreachable"}]}')
        data_dir = self._with_snapshot(snap)
        self.assertEqual(bootstrap_scan_gate._unlisted_projects(data_dir),
                         [("alpha", "unreachable"), ("zeta", "denied")])
        paragraph = bootstrap_scan_gate._scope_gap_paragraph(data_dir)
        self.assertIn("`alpha` (unreachable), `zeta` (denied)", paragraph)
        self.assertIn("not fully covered", paragraph)

    def test_the_task_body_carries_the_paragraph_when_a_project_is_unlisted(self):
        snap = '{"projects": [{"id": "mgmt", "outcome": "ok"}, {"id": "locked", "outcome": "denied"}]}'
        data_dir = self._with_snapshot(snap)
        with mock.patch.object(bootstrap_scan_gate, "_data_dir", return_value=data_dir):
            body = bootstrap_scan_gate._task_body()
        self.assertIn("`locked` (denied)", body)
        # It sits between the roster caveat and Step 2, which lists the roster.
        self.assertLess(body.index("`locked` (denied)"), body.index("**Step 2"))


class FirstRunAuditsTest(unittest.TestCase):
    """The scan leaves the first-run audits' checks to them, and the delivery says so."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)
        self.relay_db = self.d / "session_kv.db"
        patcher = mock.patch.dict(os.environ, {bootstrap_delivery.SESSION_KV_DB_ENV: str(self.relay_db)})
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _slug_table() -> set[str]:
        """The backticked slugs in the `check slug` column of the cluster inventory SOP's Step 5 table."""
        text = CLUSTER_INVENTORY_SOP.read_text(encoding="utf-8")
        rows = [line for line in text[text.index("## Step 5"):].splitlines() if line.startswith("|")]
        header = next(i for i, line in enumerate(rows) if "check slug" in line)
        slugs = set()
        for line in rows[header + 2:]:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) != 2:
                break
            slugs.update(re.findall(r"`([a-z0-9-]+)`", cells[1]))
        return slugs

    def _note(self, state=None, no_coverage=False) -> str:
        """The note for oobe's ``state``."""
        if state is not None:
            oobe.write_state(self.d, state)
        return bootstrap_delivery._audits_note(self.d, no_coverage)

    def _skipped(self, reason: str) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            oobe.skip(self.d, reason, 1000.0)

    def test_the_slug_table_names_no_check_a_first_run_audit_owns(self):
        sys.path.insert(0, str(FLEET_AUDIT_SCRIPTS))
        try:
            import audit_report
        finally:
            sys.path.remove(str(FLEET_AUDIT_SCRIPTS))
        owned = {check for audit in oobe.FIRST_RUN_AUDITS for check in audit_report.AUDITS[audit].checks}
        slugs = self._slug_table()
        self.assertIn("probes-startup", slugs)  # the parse found the table
        self.assertIn("static-sa-key", slugs)  # Step 4's per-pod check; the cluster setting is the audit's
        self.assertIn("added-capabilities", slugs)  # privileged-container covers only an added SYS_ADMIN
        self.assertEqual(sorted(slugs & owned), [])

    def test_no_marker_says_the_audits_will_run(self):
        # The stage writes its marker on its first tick after the ranking card, which delivery can beat.
        self.assertEqual(
            self._note(), f"{bootstrap_delivery.AUDITS_RUNNING_NOTE} {bootstrap_delivery.AUDITS_HOME_SENTENCE}"
        )

    def test_a_skip_says_the_report_does_not_cover_the_checks(self):
        for reason in (oobe.SKIP_NOT_NEW, oobe.SKIP_UNSETTLED, oobe.SKIP_NO_SWEEP):
            with self.subTest(reason=reason):
                self._skipped(reason)
                self.assertEqual(
                    self._note(),
                    f"The audits did not run as part of onboarding: {reason}. {bootstrap_delivery.AUDITS_NOT_COVERED_NOTE}",
                )

    def test_a_skip_an_older_stage_recorded_for_no_repository_says_the_report_covers_them(self):
        # Kept through an upgrade: that build's scan ran the audits' workload checks itself.
        (self.d / SCAN_FILED).write_text("task_id=t_sweep\nfiled_at=900\n", encoding="utf-8")
        self._skipped(bootstrap_delivery.LEGACY_NO_REPOSITORY_REASON)
        self.assertEqual(
            self._note(),
            f"The audits did not run as part of onboarding: {bootstrap_delivery.LEGACY_NO_REPOSITORY_REASON}. "
            f"{bootstrap_delivery.AUDITS_COVERED_NOTE}",
        )
        # A report no cluster's audit reached covers nothing, whatever the older stage recorded.
        self.assertEqual(
            self._note(no_coverage=True),
            f"The audits did not run as part of onboarding: {bootstrap_delivery.LEGACY_NO_REPOSITORY_REASON}. "
            f"{bootstrap_delivery.AUDITS_UNCHECKED_SCHEDULED_NOTE}",
        )

    def test_a_sweep_filed_after_an_older_skip_does_not_say_the_report_covers_them(self):
        # Onboarding repeated with the stage's marker kept: the new scan left those checks to the audits.
        self._skipped(bootstrap_delivery.LEGACY_NO_REPOSITORY_REASON)
        (self.d / SCAN_FILED).write_text("task_id=t_sweep2\nfiled_at=2000\n", encoding="utf-8")
        self.assertEqual(
            self._note(),
            f"The audits did not run as part of onboarding: {bootstrap_delivery.LEGACY_NO_REPOSITORY_REASON}. "
            f"{bootstrap_delivery.AUDITS_NOT_COVERED_NOTE}",
        )

    def test_the_not_covered_note_does_not_say_the_report_covers_them(self):
        self.assertNotIn("covers", bootstrap_delivery.AUDITS_NOT_COVERED_NOTE)
        # The skips come a day or more after the sweep, when the scheduled audits have usually run,
        # so the note speaks for this report, not for what the audits have done.
        self.assertIn(
            f"This report does not cover {inventory_findings.AUDIT_FAMILY_WORDS}", bootstrap_delivery.AUDITS_NOT_COVERED_NOTE
        )
        for note in (bootstrap_delivery.AUDITS_UNCHECKED_SCHEDULED_NOTE, bootstrap_delivery.AUDITS_NONE_STARTED_NOTE):
            self.assertNotIn("nothing has checked", note.lower())
            self.assertIn("this report", note.lower())

    def test_the_covered_note_claims_only_the_older_scans_own_checks(self):
        covered = bootstrap_delivery.AUDITS_COVERED_NOTE.split("so this report covers them", 1)[0]
        self.assertNotIn("RBAC", covered)
        self.assertIn(f"not the rest of {inventory_findings.AUDIT_FAMILY_WORDS}", bootstrap_delivery.AUDITS_COVERED_NOTE)

    def test_no_other_note_names_a_check_outside_the_posture_lines_words(self):
        # The current scan's report names its checks and the audits' areas; a note listing workload
        # checks by name would be an exclusion list the scan's table and the audit rosters outgrow.
        old_scan = ("probe", "requests and limits", "autoscaling", "NetworkPolicy", "pod security", "Workload checks")
        notes = {
            name: value
            for name, value in vars(bootstrap_delivery).items()
            if name.startswith("AUDITS_") and isinstance(value, str) and name != "AUDITS_COVERED_NOTE"
        }
        self.assertIn("AUDITS_STARTED_NOTE", notes)
        self.assertIn("AUDITS_LOST_NOTE", notes)
        for name, value in notes.items():
            for words in old_scan:
                with self.subTest(note=name, words=words):
                    self.assertNotIn(words, value)

    def test_the_posture_lines_checks_are_the_slug_table(self):
        self.assertEqual({slug for slug, _ in inventory_findings.SCAN_CHECKS}, self._slug_table())

    def test_every_first_run_audit_has_an_area(self):
        self.assertEqual(set(inventory_findings.AUDIT_FAMILIES), set(oobe.FIRST_RUN_AUDITS))

    def test_a_no_coverage_report_after_another_skip_points_at_the_schedules(self):
        self._skipped(oobe.SKIP_NOT_NEW)
        self.assertEqual(
            self._note(no_coverage=True),
            f"The audits did not run as part of onboarding: {oobe.SKIP_NOT_NEW}. "
            f"{bootstrap_delivery.AUDITS_UNCHECKED_SCHEDULED_NOTE}",
        )

    def test_the_delivery_reads_no_coverage_off_the_hand_off_marker(self):
        (self.d / INVENTORY).write_text("# Report\n", encoding="utf-8")
        (self.d / ALIGNED).touch()
        (self.d / bootstrap_handoff.HANDOFF_MARKER).write_text(
            f"sweep=t_sweep\ntask_id={bootstrap_handoff.NO_RANKING}\nfiled_at=1\n", encoding="utf-8"
        )
        (self.d / SCAN_FILED).write_text("task_id=t_sweep\nfiled_at=1\n", encoding="utf-8")
        self._skipped(oobe.SKIP_NOT_NEW)
        self.assertEqual(
            self._deliver(),
            f"# Report\n\nThe audits did not run as part of onboarding: {oobe.SKIP_NOT_NEW}. "
            f"{bootstrap_delivery.AUDITS_UNCHECKED_SCHEDULED_NOTE}\n",
        )

    def test_a_chain_under_way_says_the_audits_are_running(self):
        note = self._note({oobe.STATE_FIRED: ["fleet-wide-cost-analysis"], oobe.STATE_DONE: False})
        self.assertEqual(note, f"{bootstrap_delivery.AUDITS_RUNNING_NOTE} {bootstrap_delivery.AUDITS_HOME_SENTENCE}")

    def test_a_finished_chain_says_which_audits_the_stage_started(self):
        note = self._note({oobe.STATE_FIRED: list(oobe.FIRST_RUN_AUDITS), oobe.STATE_DONE: True})
        self.assertIn("The security and RBAC, reliability, capacity and cost audits started as part of onboarding.", note)
        # The onboarding sentence names the audits by area; ids appear only where a request names one.
        self.assertNotIn("`", note)
        self.assertTrue(note.endswith(bootstrap_delivery.AUDITS_HOME_SENTENCE))
        self.assertNotIn("Did not run", note)

    def test_held_and_given_up_audits_are_named_as_not_started(self):
        note = self._note({
            oobe.STATE_FIRED: ["fleet-wide-cost-analysis", "stockout-prevention"],
            oobe.STATE_HELD: {"compliance-audit": oobe.HOLD_DISABLED},
            oobe.STATE_GAVE_UP: ["obtainability-audit"],
            oobe.STATE_DONE: True,
        })
        self.assertIn("The capacity and cost audits started as part of onboarding.", note)
        self.assertIn(
            "Did not run as part of onboarding: the security and RBAC audit (disabled), "
            f"the reliability audit ({bootstrap_delivery.AUDITS_GAVE_UP_REASON}).", note
        )

    def test_a_chain_where_every_audit_was_held_or_given_up_says_none_started(self):
        for state in (
            {oobe.STATE_HELD: {a: oobe.HOLD_PAUSED for a in oobe.FIRST_RUN_AUDITS}, oobe.STATE_DONE: True},
            {oobe.STATE_GAVE_UP: list(oobe.FIRST_RUN_AUDITS), oobe.STATE_DONE: True},
        ):
            with self.subTest(state=state):
                note = self._note(state)
                self.assertTrue(note.startswith(bootstrap_delivery.AUDITS_NONE_STARTED_NOTE))
                self.assertIn("Did not run as part of onboarding: the security and RBAC audit (", note)
                self.assertNotIn(bootstrap_delivery.AUDITS_HOME_SENTENCE, note)

    def test_a_chain_under_way_names_a_held_audit_as_not_started(self):
        note = self._note({oobe.STATE_FIRED: ["fleet-wide-cost-analysis"],
                           oobe.STATE_HELD: {"compliance-audit": oobe.HOLD_MISSING}, oobe.STATE_DONE: False})
        self.assertEqual(
            note,
            f"{bootstrap_delivery.AUDITS_RUNNING_NOTE} Did not run as part of onboarding: the security and RBAC audit "
            f"({oobe.HOLD_MISSING}). {bootstrap_delivery.AUDITS_HOME_SENTENCE}",
        )

    def _run(self, audit: str, claimed_at: float, outcome: str | None, columns: bool = True,
             status: str = "completed") -> None:
        """An ended run of ``audit`` in the Platform Agent's cron store, ending a minute after its claim."""
        cron = self.d / oobe.PROFILES_DIR / oobe.PLATFORM_PROFILE / oobe.CRON_DIR
        cron.mkdir(parents=True, exist_ok=True)
        extra = ", finished_at TEXT, delivery_outcome TEXT" if columns else ""
        with sqlite3.connect(cron / oobe.EXECUTIONS_DB) as conn:
            conn.execute(f"CREATE TABLE IF NOT EXISTS executions (job_id TEXT, status TEXT, claimed_at TEXT{extra})")
            stamp = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()  # noqa: E731
            if columns:
                conn.execute("INSERT INTO executions VALUES (?, ?, ?, ?, ?)",
                             (audit, status, stamp(claimed_at), stamp(claimed_at + 60), outcome))
            else:
                conn.execute("INSERT INTO executions VALUES (?, ?, ?)", (audit, status, stamp(claimed_at)))

    def _relay(self, audit: str, posted_at: float, run: str = "r1") -> None:
        """The chat relay's record that it posted ``audit``'s report at ``posted_at``, as session-kv writes it.

        ``run`` is the stamp of the report record it marked posted (``_stamp``), or a placeholder: the
        note does not read it.
        """
        with sqlite3.connect(self.relay_db) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS audit_report_runs (profile TEXT NOT NULL, job_id TEXT NOT NULL, "
                         "run TEXT NOT NULL, posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
                         "PRIMARY KEY (profile, job_id))")
            conn.execute("INSERT OR REPLACE INTO audit_report_runs VALUES ('platform', ?, ?, ?)",
                         (audit, run, datetime.fromtimestamp(posted_at, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")))

    @staticmethod
    def _stamp(at: float) -> str:
        """The ``run`` stamp the audit's ``start`` gives a report record written at ``at``."""
        sys.path.insert(0, str(FLEET_AUDIT_SCRIPTS))
        try:
            import audit_report
        finally:
            sys.path.remove(str(FLEET_AUDIT_SCRIPTS))
        return datetime.fromtimestamp(at, timezone.utc).strftime(audit_report.REPORT_STAMP_FORMAT)

    def _filed(self, at: float) -> None:
        (self.d / SCAN_FILED).write_text(f"task_id=t_sweep\nfiled_at={at}\n", encoding="utf-8")

    def _deliver(self) -> str:
        """What the text path prints: the report and its note."""
        (self.d / INVENTORY).write_text("# Report\n", encoding="utf-8")
        (self.d / ALIGNED).touch()
        out = io.StringIO()
        with (
            mock.patch.object(sandbox_exec, "sandbox_enabled", return_value=False),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(bootstrap_delivery.main(self.d), 0)
        return out.getvalue()

    @staticmethod
    def _lost(note: str) -> str:
        """The audits the note lists as not posted."""
        return note.partition("not posted to the chat:")[2].partition(". To see them")[0]

    def _finished_chain(self, outcome: str | None) -> dict:
        """All four audits marked and completed after their marks, each run's delivery ending ``outcome``."""
        now = time.time()
        marks = {a: now - 3600 + i * 600 for i, a in enumerate(oobe.FIRST_RUN_AUDITS)}
        for audit in oobe.FIRST_RUN_AUDITS:
            self._run(audit, marks[audit] + 30, outcome)
        return {oobe.STATE_FIRED: list(oobe.FIRST_RUN_AUDITS), oobe.STATE_MARKS: marks, oobe.STATE_DONE: True}

    def test_audits_that_finished_before_a_home_channel_are_listed_with_a_request(self):
        note = self._note(self._finished_chain(bootstrap_delivery.DELIVERY_FAILED))
        self.assertIn("The security and RBAC, reliability, capacity and cost audits started as part of onboarding.", note)
        # No home-channel promise beside summaries that were not posted.
        self.assertNotIn(bootstrap_delivery.AUDITS_HOME_SENTENCE, note)
        self.assertIn(
            "These ran, but their summaries were not posted to the chat: "
            "`fleet-wide-cost-analysis`, `compliance-audit`, `obtainability-audit`, `stockout-prevention`. "
            "To see them, send me: `Show me the latest report of each of these fleet audits from the report store: "
            "fleet-wide-cost-analysis, compliance-audit, obtainability-audit, stockout-prevention.`", note)
        self.assertNotIn("Ask me", note)

    def test_a_skip_names_the_scheduled_runs_that_lost_their_summaries(self):
        # SKIP_NOT_NEW fires a day after the sweep; the daily audits ran meanwhile with no home channel.
        now = time.time()
        self._filed(now - 2 * 86400)
        self._run("compliance-audit", now - 86400, bootstrap_delivery.DELIVERY_FAILED)
        self._run("stockout-prevention", now - 80000, "suppressed")
        self._skipped(oobe.SKIP_NOT_NEW)
        note = self._note()
        self.assertTrue(note.startswith(f"The audits did not run as part of onboarding: {oobe.SKIP_NOT_NEW}."))
        self.assertEqual(self._lost(note), " `compliance-audit`")
        # Every "did not run" is scoped to onboarding, so it holds beside an audit that ran on its schedule.
        self.assertEqual(note.count("did not run"), note.count("did not run as part of onboarding"))

    def test_held_and_given_up_audits_that_ran_on_schedule_are_named(self):
        # A held audit re-enabled by an operator, and a given-up one whose schedule recovered.
        now = time.time()
        self._filed(now - 7200)
        self._run("compliance-audit", now - 3000, bootstrap_delivery.DELIVERY_FAILED)
        self._run("obtainability-audit", now - 2000, bootstrap_delivery.DELIVERY_FAILED)
        note = self._note({
            oobe.STATE_FIRED: ["fleet-wide-cost-analysis"],
            oobe.STATE_HELD: {"compliance-audit": oobe.HOLD_DISABLED},
            oobe.STATE_GAVE_UP: ["obtainability-audit"],
            oobe.STATE_DONE: True,
        })
        self.assertEqual(self._lost(note), " `compliance-audit`, `obtainability-audit`")
        # The stage sentence reports the stage, so it does not contradict the list.
        self.assertIn("Did not run as part of onboarding: the security and RBAC audit (disabled)", note)
        self.assertNotIn("Not run", note)

    def test_the_other_shipped_fleet_audits_are_named(self):
        now = time.time()
        self._filed(now - 7200)
        self._run("gce-compute-fleet-audit", now - 3000, bootstrap_delivery.DELIVERY_FAILED)
        self._run("compliance-audit", now - 2000, bootstrap_delivery.DELIVERY_FAILED)
        note = self._note({oobe.STATE_FIRED: [], oobe.STATE_HELD: {a: oobe.HOLD_PAUSED for a in oobe.FIRST_RUN_AUDITS},
                           oobe.STATE_DONE: True})
        # First-run audits first, in chain order, then the rest in roster order.
        self.assertEqual(self._lost(note), " `compliance-audit`, `gce-compute-fleet-audit`")
        self.assertIn("report store: compliance-audit, gce-compute-fleet-audit.`", note)

    def test_a_run_that_ended_before_the_sweep_is_not_this_onboardings(self):
        now = time.time()
        self._run("compliance-audit", now - 7200, bootstrap_delivery.DELIVERY_FAILED)
        self._filed(now - 3600)
        self._skipped(oobe.SKIP_NOT_NEW)
        self.assertNotIn("not posted", self._note())

    def test_a_failed_run_whose_notice_was_not_posted_is_named_apart_with_no_request(self):
        # A failed run wrote no report, so the store request cannot fetch what was lost.
        now = time.time()
        self._filed(now - 7200)
        self._run("ai-security-audit", now - 3000, bootstrap_delivery.DELIVERY_FAILED, status="failed")
        self._run("compliance-audit", now - 2000, bootstrap_delivery.DELIVERY_FAILED)
        self._skipped(oobe.SKIP_NOT_NEW)
        note = self._note()
        self.assertEqual(self._lost(note), " `compliance-audit`")
        self.assertIn("report store: compliance-audit.`", note)
        self.assertTrue(note.endswith(
            "These failed, and their failure notices were not posted to the chat: `ai-security-audit`."
        ))
        self.assertNotIn("before anyone", note)  # nothing checks the crash against when the chat was opened

    def test_a_crashed_run_does_not_hide_an_earlier_lost_summary(self):
        # The earlier run's report is in the store; the crash after it is named, and so is that report.
        now = time.time()
        self._filed(now - 7200)
        self._run("compliance-audit", now - 3000, bootstrap_delivery.DELIVERY_FAILED)
        self._run("compliance-audit", now - 1000, bootstrap_delivery.DELIVERY_FAILED, status="failed")
        self._run("obtainability-audit", now - 2000, bootstrap_delivery.DELIVERY_FAILED, status="failed")
        self._run("obtainability-audit", now - 900, bootstrap_delivery.DELIVERY_FAILED, status="failed")
        self._skipped(oobe.SKIP_NOT_NEW)
        note = self._note()
        self.assertEqual(self._lost(note), " `compliance-audit`")
        self.assertIn("report store: compliance-audit.`", note)
        self.assertTrue(note.endswith(
            "their failure notices were not posted to the chat: `compliance-audit`, `obtainability-audit`."))

    def test_a_posted_failure_notice_does_not_hide_an_earlier_lost_summary(self):
        # The relay posted the crash's failure notice, not the earlier run's report, which is still in the store.
        now = time.time()
        self._filed(now - 7200)
        self._run("compliance-audit", now - 3000, bootstrap_delivery.DELIVERY_FAILED)
        self._run("compliance-audit", now - 1000, bootstrap_delivery.DELIVERY_FAILED, status="failed")
        self._relay("compliance-audit", now - 940)
        self._skipped(oobe.SKIP_NOT_NEW)
        note = self._note()
        self.assertEqual(self._lost(note), " `compliance-audit`")
        self.assertNotIn("failure notices", note)

    def test_a_consumed_failure_notice_settles_no_earlier_run(self):
        # Oldest first: a lost summary, a delivered run that posted nothing (a silence marker the scheduler
        # let through) after its `start` wrote the record, a crash before `start` whose notice posted. The
        # notice replaced the relay's one row for the job with the record's stamp and its own time, which
        # says nothing about the run between, so the lost summary is named. The same row is left when the
        # delivered run did post; naming it then costs one question, not a report.
        now = time.time()
        self._filed(now - 7200)
        self._run("compliance-audit", now - 3000, bootstrap_delivery.DELIVERY_FAILED)
        self._run("compliance-audit", now - 2000, "delivered")
        self._run("compliance-audit", now - 1000, bootstrap_delivery.DELIVERY_FAILED, status="failed")
        self._relay("compliance-audit", now - 940, run=self._stamp(now - 1990))
        self._skipped(oobe.SKIP_NOT_NEW)
        note = self._note()
        self.assertEqual(self._lost(note), " `compliance-audit`")
        self.assertNotIn("failure notices", note)

    def test_without_a_relay_record_a_crashed_run_whose_notice_was_delivered_does_not_hide_an_earlier_lost_summary(self):
        # No session_kv.db to ask: the crashed run's delivered failure notice stands, and the earlier run's
        # report, still in the store, is named.
        now = time.time()
        self._filed(now - 7200)
        self._run("compliance-audit", now - 3000, bootstrap_delivery.DELIVERY_FAILED)
        self._run("compliance-audit", now - 1000, "delivered", status="failed")
        self._skipped(oobe.SKIP_NOT_NEW)
        note = self._note()
        self.assertEqual(self._lost(note), " `compliance-audit`")
        self.assertNotIn("failure notices", note)

    def test_a_scheduled_run_in_flight_does_not_hide_an_earlier_lost_summary(self):
        # The running row is not an ended run; the completed one before it still lost its summary.
        state = self._finished_chain(bootstrap_delivery.DELIVERY_FAILED)
        cron = self.d / oobe.PROFILES_DIR / oobe.PLATFORM_PROFILE / oobe.CRON_DIR
        with sqlite3.connect(cron / oobe.EXECUTIONS_DB) as conn:
            conn.execute("INSERT INTO executions VALUES ('compliance-audit', 'running', ?, NULL, NULL)",
                         (datetime.fromtimestamp(time.time() - 30, timezone.utc).isoformat(),))
        self.assertIn("`compliance-audit`", self._lost(self._note(state)))

    def test_outcomes_are_read_by_name(self):
        for outcome, lost in (("failed", True), ("delivered", False), ("queued", False), ("not_configured", False),
                              ("suppressed", False), ("suppressed_acked", False), (None, False), ("new-value", True)):
            with self.subTest(outcome=outcome):
                self._tmp.cleanup()
                self.d.mkdir()
                note = self._note(self._finished_chain(outcome))
                self.assertEqual("not posted" in note, lost)

    def test_a_relay_post_settles_a_failed_outcome_as_posted(self):
        # The plugin answers an error for a post that reached one platform and missed another.
        state = self._finished_chain(bootstrap_delivery.DELIVERY_FAILED)
        self._relay("compliance-audit", state[oobe.STATE_MARKS]["compliance-audit"] + 60)
        lost = self._lost(self._note(state))
        self.assertNotIn("`compliance-audit`", lost)
        self.assertIn("`stockout-prevention`", lost)

    def test_a_later_delivered_run_with_no_relay_post_does_not_hide_a_lost_summary(self):
        # A silence marker the scheduler let through is recorded `delivered`, and the relay posts nothing.
        state = self._finished_chain(bootstrap_delivery.DELIVERY_FAILED)
        self._relay("compliance-audit", state[oobe.STATE_MARKS]["compliance-audit"] - 86400)
        self._run("compliance-audit", time.time() - 10, "delivered")
        self.assertIn("`compliance-audit`", self._lost(self._note(state)))

    def test_a_later_delivered_run_the_relay_posted_clears_an_earlier_failure(self):
        state = self._finished_chain(bootstrap_delivery.DELIVERY_FAILED)
        self._run("compliance-audit", time.time() - 20, "delivered")
        self._relay("compliance-audit", time.time() - 10)
        lost = self._lost(self._note(state))
        self.assertNotIn("`compliance-audit`", lost)
        self.assertIn("`stockout-prevention`", lost)

    def test_with_no_relay_record_a_later_delivered_run_clears_an_earlier_failure(self):
        state = self._finished_chain(bootstrap_delivery.DELIVERY_FAILED)
        self._run("compliance-audit", time.time() - 10, "delivered")
        note = self._note(state)
        self.assertNotIn("`compliance-audit`", self._lost(note))
        self.assertIn("audits note takes each run's delivery outcome as it is", (self.d / oobe.STAGE_LOG).read_text())

    def test_a_run_going_at_the_mark_counts_by_its_finish(self):
        now = time.time()
        # Claimed before the mark, finished after it: the stage took it as the audit's answer.
        self._run("compliance-audit", now - 960, bootstrap_delivery.DELIVERY_FAILED)
        note = self._note({oobe.STATE_FIRED: ["compliance-audit"], oobe.STATE_MARKS: {"compliance-audit": now - 930},
                           oobe.STATE_DONE: True})
        self.assertEqual(self._lost(note), " `compliance-audit`")

    def test_a_suppressed_acked_run_is_silent(self):
        now = time.time()
        self._run("compliance-audit", now - 1200, bootstrap_delivery.DELIVERY_FAILED)
        self._run("compliance-audit", now - 600, "suppressed_acked")
        note = self._note({oobe.STATE_FIRED: ["compliance-audit"], oobe.STATE_MARKS: {"compliance-audit": now - 1300},
                           oobe.STATE_DONE: True})
        self.assertEqual(self._lost(note), " `compliance-audit`")

    def test_a_mark_that_is_not_a_number_reads_as_no_mark(self):
        now = time.time()
        self._run("compliance-audit", now - 600, bootstrap_delivery.DELIVERY_FAILED)
        for mark in ("1790000000", None, [1]):
            with self.subTest(mark=mark):
                note = self._note({oobe.STATE_FIRED: ["compliance-audit"], oobe.STATE_MARKS: {"compliance-audit": mark},
                                   oobe.STATE_DONE: True})
                self.assertEqual(self._lost(note), " `compliance-audit`")

    def test_a_later_silent_run_does_not_hide_the_lost_summary(self):
        # Day 2: the first run's summary was refused; the next scheduled run found nothing new.
        now = time.time()
        self._run("compliance-audit", now - 86400, bootstrap_delivery.DELIVERY_FAILED)
        for outcome in ("suppressed", None):
            self._run("compliance-audit", now - 600, outcome)
        note = self._note({oobe.STATE_FIRED: ["compliance-audit"], oobe.STATE_MARKS: {"compliance-audit": now - 86400 - 60},
                           oobe.STATE_DONE: True})
        self.assertEqual(self._lost(note), " `compliance-audit`")

    def test_each_outcome_lists_the_audit_by_its_class(self):
        # (listed when it is the only run, listed when it follows a refused run)
        expected = {
            "failed": (True, True),
            "suppressed": (False, True),
            "suppressed_acked": (False, True),
            None: (False, True),
            "delivered": (False, False),
            "queued": (False, False),
            "not_configured": (False, False),
            # A value Hermes does not write today reads as lost, so a renamed failure still names the audit.
            "a-value-hermes-added-later": (True, True),
        }
        for outcome, (alone, after_failed) in expected.items():
            with self.subTest(outcome=outcome):
                for runs, listed in (
                    ([(1.0, 2.0, outcome, "completed")], alone),
                    ([(1.0, 2.0, "failed", "completed"), (3.0, 4.0, outcome, "completed")], after_failed),
                ):
                    # No relay record, so each outcome is read as it stands.
                    got = bootstrap_delivery._lost_audits(("compliance-audit",), {"compliance-audit": runs}, {}, None)
                    self.assertEqual(got, [("compliance-audit", False)] if listed else [])

    def test_a_run_from_before_the_mark_is_not_the_first_run(self):
        now = time.time()
        self._run("compliance-audit", now - 86400, bootstrap_delivery.DELIVERY_FAILED)
        note = self._note({oobe.STATE_FIRED: ["compliance-audit"], oobe.STATE_MARKS: {"compliance-audit": now - 900},
                           oobe.STATE_DONE: True})
        self.assertIn("The security and RBAC audit started as part of onboarding.", note)
        self.assertNotIn("not posted", note)

    def test_a_store_without_the_outcome_column_lists_nothing(self):
        now = time.time()
        self._run("compliance-audit", now - 600, None, columns=False)
        note = self._note({oobe.STATE_FIRED: ["compliance-audit"], oobe.STATE_MARKS: {"compliance-audit": now - 900},
                           oobe.STATE_DONE: True})
        self.assertNotIn("not posted", note)
        # The stage log says why no unposted summary can be named; stderr is dropped on exit 0.
        self.assertIn("no delivery_outcome column", (self.d / oobe.STAGE_LOG).read_text())

    def test_an_unreadable_store_says_the_records_could_not_be_read(self):
        cron = self.d / oobe.PROFILES_DIR / oobe.PLATFORM_PROFILE / oobe.CRON_DIR
        cron.mkdir(parents=True)
        (cron / oobe.EXECUTIONS_DB).write_text("not a database", encoding="utf-8")
        note = self._note({oobe.STATE_FIRED: list(oobe.FIRST_RUN_AUDITS), oobe.STATE_DONE: True})
        self.assertIn("The security and RBAC, reliability, capacity and cost audits started as part of onboarding.", note)
        self.assertIn("I could not read the audit run records", note)
        self.assertIn("report store: fleet-wide-cost-analysis, compliance-audit, obtainability-audit, "
                      "stockout-prevention, security-patch-orchestrator, fleet-consistency-drift, ai-security-audit, "
                      "gcp-networking-fabric-audit, gce-compute-fleet-audit.`", note)
        self.assertIn("cannot read", (self.d / oobe.STAGE_LOG).read_text())

    def test_an_unreadable_sweep_marker_keeps_the_coverage_caveat_on_the_legacy_skip(self):
        # The legacy skip's own marker read raised past _audits_note, and the whole-note fallback lost the caveat.
        (self.d / SCAN_FILED).write_bytes(b"task_id=t_\xff\nfiled_at=1\n")
        self._skipped(bootstrap_delivery.LEGACY_NO_REPOSITORY_REASON)
        note = self._note()
        self.assertTrue(note.startswith(f"The audits did not run as part of onboarding: "
                                        f"{bootstrap_delivery.LEGACY_NO_REPOSITORY_REASON}."))
        self.assertIn(bootstrap_delivery.AUDITS_NOT_COVERED_NOTE, note)
        self.assertNotIn("I could not read the audit run records", note)

    def test_an_unreadable_sweep_marker_reads_every_run(self):
        now = time.time()
        (self.d / SCAN_FILED).write_bytes(b"task_id=t_\xff\nfiled_at=1\n")
        self._run("compliance-audit", now - 600, bootstrap_delivery.DELIVERY_FAILED)
        self._skipped(oobe.SKIP_NOT_NEW)
        self.assertEqual(self._lost(self._note()), " `compliance-audit`")

    def test_a_note_that_raises_still_delivers_the_report_before_any_claim_is_lost(self):
        with mock.patch.object(bootstrap_delivery, "_stage_sentences", side_effect=RuntimeError("boom")):
            out = self._deliver()
        self.assertIn("# Report", out)
        self.assertIn("I could not read the audit run records", out)
        self.assertIn("the audits note failed", (self.d / oobe.STAGE_LOG).read_text())

    def test_a_raise_reading_the_runs_keeps_the_stage_sentence(self):
        self._skipped(oobe.SKIP_NOT_NEW)
        with mock.patch.object(bootstrap_delivery, "_relay_posts", side_effect=PermissionError("denied")):
            note = self._note()
        self.assertIn(bootstrap_delivery.AUDITS_NOT_COVERED_NOTE, note)
        self.assertIn("I could not read the audit run records", note)
        self.assertIn("could not tell which summaries were posted", (self.d / oobe.STAGE_LOG).read_text())

    def test_the_note_is_built_before_the_claim(self):
        claimed = []
        real = bootstrap_delivery._claim_delivery
        with mock.patch.object(bootstrap_delivery, "_claim_delivery",
                               side_effect=lambda d: claimed.append(True) or real(d)), \
                mock.patch.object(bootstrap_delivery, "_safe_audits_note",
                                  side_effect=lambda *a, **k: self.assertEqual(claimed, []) or "NOTE-BUILT-FIRST"):
            self.assertIn("NOTE-BUILT-FIRST", self._deliver())

    def test_the_text_delivery_carries_the_lost_summaries(self):
        oobe.write_state(self.d, self._finished_chain(bootstrap_delivery.DELIVERY_FAILED))
        self._filed(1)
        self.assertIn("stockout-prevention.`\n", self._deliver())

    def test_the_text_delivery_closes_with_the_skip(self):
        self._filed(1)
        self._skipped(oobe.SKIP_NOT_NEW)
        self.assertEqual(
            self._deliver(),
            f"# Report\n\nThe audits did not run as part of onboarding: {oobe.SKIP_NOT_NEW}. "
            f"{bootstrap_delivery.AUDITS_NOT_COVERED_NOTE}\n",
        )

    def test_the_relay_record_it_reads_is_the_one_session_kv_writes(self):
        server = (Path(__file__).resolve().parents[2] / "platform" / "scripts" / "session_kv_server.py").read_text()
        table = server[server.index("CREATE TABLE IF NOT EXISTS audit_report_runs"):]
        table = table[:table.index(")")]
        for column in ("profile", "job_id", "posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"):
            self.assertIn(column, table)
        self.assertIn(f'os.getenv("{bootstrap_delivery.SESSION_KV_DB_ENV}", '
                      f'"{bootstrap_delivery.DEFAULT_SESSION_KV_DB_PATH}")', server)

    def test_the_request_covers_every_shipped_fleet_audit(self):
        # A fleet audit added to the roster, or to the first-run chain, needs a request the note can send.
        roster = json.loads((Path(__file__).resolve().parents[2] / "platform" / "cron" / "jobs.json").read_text())
        jobs = roster["jobs"] if isinstance(roster, dict) else roster
        shipped = {j["id"] for j in jobs if "fleet-audit" in (j.get("skills") or []) and j.get("deliver") == "chat"}
        checked = set(bootstrap_delivery._checked_audits())
        self.assertEqual(checked, shipped | set(oobe.FIRST_RUN_AUDITS))
        # Each one the store request asks for has a report in the store.
        sys.path.insert(0, str(FLEET_AUDIT_SCRIPTS))
        try:
            import audit_report
        finally:
            sys.path.remove(str(FLEET_AUDIT_SCRIPTS))
        self.assertEqual(sorted(checked - set(audit_report.AUDITS)), [])

    def test_the_request_is_the_nightly_cases_prompt(self):
        # bench/tasks/fleet-audit-request-line grades the request on a live install; the two texts stay one.
        import yaml

        case = Path(__file__).resolve().parents[3] / "bench" / "tasks" / "fleet-audit-request-line" / "task.yaml"
        data = yaml.safe_load(case.read_text(encoding="utf-8"))
        self.assertEqual(data["prompt"], bootstrap_delivery._request(bootstrap_delivery._checked_audits()))

if __name__ == "__main__":
    unittest.main()
