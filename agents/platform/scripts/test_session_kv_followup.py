"""The quiet follow-up and the triage watchdog in session_kv_server.

The event watcher sends ``kind: k8s-event-followup`` on an alert's own
session once the object has emitted nothing for its quiet period; the server
posts one line into the alert's thread and onto its card, writes no ledger
row, claims no quota and starts no turn. The handshake that lets the watcher
send it at all is the ``features`` list on the event path's ``injected``
answer. The watchdog is the same thread's line for an alert whose card never
came.

Imports the sibling module's harness (temp database, API key, the mcp stub) so
this file needs none of its own.
"""

import json
import os
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from datetime import datetime, timezone
from unittest.mock import patch

import test_session_kv_server as harness  # noqa: F401  (database path, mcp stub)

import session_kv_server
from test_session_kv_server import API_KEY, AUTH_HEADERS

LAST_SEEN = "2026-08-10T15:28:52Z"
FIRST_SEEN = "2026-08-10T15:27:02Z"
FOLLOWUP = {
    "kind": "k8s-event-followup", "reason": "CrashLoopBackOff", "namespace": "gke-gmp-system",
    "kind_of_object": "Pod", "name": "collector-rvqvj", "uid": "bca7210d", "count": 3,
    "first_seen": FIRST_SEEN, "last_seen": LAST_SEEN, "cluster": "ap-ap-deploy-test",
    "message": "no new events for this object since the last sighting", "type": "Warning",
}


def _client():
    from fastapi.testclient import TestClient

    return TestClient(session_kv_server.app, headers=AUTH_HEADERS)


class FollowupInjectTest(unittest.TestCase):
    def setUp(self):
        os.environ["SESSION_KV_API_KEY"] = API_KEY
        session_kv_server.init_db()
        self.client = _client()

    def tearDown(self):
        os.environ.pop("SESSION_KV_API_KEY", None)

    def _route(self, session_id, platform="slack", thread="1760000000.000100"):
        session_kv_server._ensure_session_row(session_id, "platform", "")
        with patch.object(session_kv_server, "_slack_home_channel", return_value="C0HOME"):
            session_kv_server._register_session_routing(session_id, platform, thread)

    def test_the_event_answer_advertises_the_followup_feature(self):
        payload = {"reason": "BackOff", "namespace": "payments", "kind_of_object": "Pod", "name": "api-1",
                   "message": "Back-off restarting failed container app in pod api-1", "type": "Warning"}
        with patch.object(session_kv_server, "trigger_agent_troubleshooter"):
            resp = self.client.post("/sessions/k8s-evt-feat/inject", json={"message": json.dumps(payload)})
        self.assertEqual(resp.json(), {"status": "injected", "features": ["followup"]})
        self.assertIn("followup", session_kv_server.INJECT_RESPONSE_FEATURES)
        self.assertIn("k8s-event-followup", session_kv_server.INJECT_KINDS_SUPPORTED)
        # /healthz advertises the same list, unauthenticated: what the watcher's
        # sweep asks before each tick's follow-ups.
        from fastapi.testclient import TestClient

        health = TestClient(session_kv_server.app).get("/healthz").json()
        self.assertIn("followup", health["features"])

    def test_a_followup_lands_in_the_alerts_thread_and_on_its_card(self):
        self._route("k8s-evt-quiet")
        with patch.object(session_kv_server, "_send_to_chat", return_value="1760000000.000100") as send, \
             patch.object(session_kv_server, "_comment_alert_card", return_value=True) as comment, \
             patch.object(session_kv_server, "record_intercepted_event") as ledger, \
             patch.object(session_kv_server, "trigger_agent_troubleshooter") as trigger:
            resp = self.client.post("/sessions/k8s-evt-quiet/inject", json={"message": json.dumps(FOLLOWUP)})
        self.assertEqual(resp.json(), {"status": "followup"})
        ledger.assert_not_called()
        trigger.assert_not_called()
        platform, text, chat_id, thread_id = send.call_args.args
        self.assertEqual((platform, chat_id, thread_id), ("slack", "C0HOME", "1760000000.000100"))
        self.assertIn("Quiet since 15:28 UTC", text)
        self.assertIn("`gke-gmp-system/collector-rvqvj`", text)
        self.assertIn("`ap-ap-deploy-test`", text)
        self.assertIn("first at 15:27 UTC, 3 seen", text)
        self.assertIn("was not re-read", text)
        self.assertEqual(comment.call_args.args, ("k8s-evt-quiet", text))

    def test_an_unrouted_session_posts_nothing(self):
        with patch.object(session_kv_server, "_send_to_chat") as send, \
             patch.object(session_kv_server, "_comment_alert_card") as comment:
            resp = self.client.post("/sessions/k8s-evt-nowhere/inject", json={"message": json.dumps(FOLLOWUP)})
        self.assertEqual(resp.json(), {"status": "unrouted"})
        send.assert_not_called()
        comment.assert_not_called()

    def test_the_message_reads_without_stamps_or_object(self):
        bare = {"kind": "k8s-event-followup", "namespace": "payments", "name": "api", "cluster": "prod-a",
                "reason": "OOMKilled", "last_seen": LAST_SEEN}
        text = session_kv_server._followup_message("k8s-evt-bare", bare,
                                                   now=datetime(2026, 8, 10, 15, 45, tzinfo=timezone.utc))
        self.assertIn("`payments/api` on `prod-a` for 16 min", text)
        self.assertIn("Quiet since 15:28 UTC", text)
        # No stamp at all still reads as a sentence.
        self.assertTrue(session_kv_server._followup_message("k8s-evt-none", {"kind": "k8s-event-followup"}).startswith(
            f"{session_kv_server.FOLLOWUP_EMOJI} **Quiet:**"))

    def test_the_followup_counts_against_no_quota(self):
        self._route("k8s-evt-budget")
        before = self.client.get("/v1/alert-quota").json()
        with patch.object(session_kv_server, "_send_to_chat", return_value="t"), \
             patch.object(session_kv_server, "_comment_alert_card", return_value=False):
            self.client.post("/sessions/k8s-evt-budget/inject", json={"message": json.dumps(FOLLOWUP)})
        self.assertEqual(self.client.get("/v1/alert-quota").json(), before)


class TriageWatchdogTest(unittest.TestCase):
    def setUp(self):
        session_kv_server.init_db()

    def _store_report(self, chat_id, thread_id):
        with closing(sqlite3.connect(session_kv_server.SESSION_KV_DB_PATH)) as conn:
            conn.execute("INSERT OR REPLACE INTO incidents (chat_id, thread_id, report) VALUES (?, ?, ?)",
                         (chat_id, thread_id, "## What's wrong\n\nx"))
            conn.commit()

    def test_says_so_when_no_card_and_no_report_exist(self):
        with patch.object(session_kv_server, "_card_for_alert_session", return_value=None), \
             patch.object(session_kv_server, "_send_to_chat", return_value="t") as send:
            session_kv_server._triage_watchdog("k8s-evt-lost", "slack", "C0HOME", "t1")
        platform, text, chat_id, thread_id = send.call_args.args
        self.assertEqual((platform, chat_id, thread_id), ("slack", "C0HOME", "t1"))
        self.assertIn("No triage for this alert in 60 min", text)
        self.assertNotIn("log says why", text)

    def test_stays_quiet_when_a_card_was_filed(self):
        with patch.object(session_kv_server, "_card_for_alert_session", return_value="t_1"), \
             patch.object(session_kv_server, "_send_to_chat") as send:
            session_kv_server._triage_watchdog("k8s-evt-ok", "slack", "C0HOME", "t2")
        send.assert_not_called()

    def test_stays_quiet_when_a_report_reached_the_thread(self):
        self._store_report("C0HOME", "t3")
        with patch.object(session_kv_server, "_card_for_alert_session", return_value=None), \
             patch.object(session_kv_server, "_send_to_chat") as send:
            session_kv_server._triage_watchdog("k8s-evt-done", "slack", "C0HOME", "t3")
        send.assert_not_called()

    def test_stays_quiet_when_the_board_cannot_be_read(self):
        with patch.object(session_kv_server, "_card_for_alert_session", return_value=session_kv_server.BOARD_UNREADABLE), \
             patch.object(session_kv_server, "_send_to_chat") as send:
            session_kv_server._triage_watchdog("k8s-evt-locked", "slack", "C0HOME", "t6")
        send.assert_not_called()
        with patch("stall_watch.card_for_session", side_effect=RuntimeError("database is locked")), \
             patch.object(session_kv_server, "_send_to_chat") as send:
            self.assertEqual(session_kv_server._card_for_alert_session("k8s-evt-locked"), session_kv_server.BOARD_UNREADABLE)
            self.assertFalse(session_kv_server._comment_alert_card("k8s-evt-locked", "x"))

    def test_arming_takes_the_interval_and_skips_an_unthreaded_alert(self):
        with patch.object(session_kv_server.threading, "Timer") as timer:
            session_kv_server._schedule_triage_watchdog("s1", "slack", "C0HOME", "t4")
            timer.assert_called_once()
            self.assertEqual(timer.call_args.args[0], session_kv_server.TRIAGE_WATCHDOG_SECONDS)
            self.assertEqual(timer.call_args.kwargs["args"], ("s1", "slack", "C0HOME", "t4"))
            timer.return_value.start.assert_called_once()
            timer.reset_mock()
            session_kv_server._schedule_triage_watchdog("s2", "slack", "C0HOME", "")
            timer.assert_not_called()
        with patch.object(session_kv_server, "TRIAGE_WATCHDOG_SECONDS", 0), \
             patch.object(session_kv_server.threading, "Timer") as timer:
            session_kv_server._schedule_triage_watchdog("s3", "slack", "C0HOME", "t5")
            timer.assert_not_called()

    def test_the_troubleshooter_arms_it_once_the_alert_is_routed(self):
        for session_created in (True, False):
            with self.subTest(session_created=session_created), \
                 patch.object(session_kv_server, "enabled_chat_platforms", return_value=["slack"]), \
                 patch.object(session_kv_server, "get_active_platform", return_value="slack"), \
                 patch.object(session_kv_server, "_post_initial_alert", return_value="1760000000.000200"), \
                 patch.object(session_kv_server, "_slack_home_channel", return_value="C0HOME"), \
                 patch.object(session_kv_server, "_create_gateway_session", return_value=session_created), \
                 patch.object(session_kv_server, "_start_agent_turn") as turn, \
                 patch.object(session_kv_server, "_schedule_triage_watchdog") as arm:
                session_kv_server._ensure_session_row("k8s-evt-arm", "platform", "")
                session_kv_server.trigger_agent_troubleshooter("k8s-evt-arm", "alert", {"reason": "BackOff"}, None)
            # Armed whether or not the gateway accepted the session: a turn that
            # never starts is the case the watchdog exists for.
            arm.assert_called_once_with("k8s-evt-arm", "slack", "C0HOME", "1760000000.000200")
            self.assertEqual(turn.call_count, 1 if session_created else 0)

    def test_a_non_integer_watchdog_knob_falls_back_instead_of_failing_the_import(self):
        # The module is imported afresh in a child with the bad value set: the
        # constant is read at import, so only an import shows whether a bad
        # value fails it. A child that cannot import the module for another
        # reason (no mcp distribution) is this environment's limit, not a
        # verdict.
        env = dict(os.environ, TRIAGE_WATCHDOG_SECONDS="1h", SESSION_KV_DB_PATH=session_kv_server.SESSION_KV_DB_PATH,
                   PYTHONPATH=os.path.dirname(session_kv_server.__file__))
        probe = subprocess.run(
            [sys.executable, "-c", "import session_kv_server as s; print(s.TRIAGE_WATCHDOG_SECONDS)"],
            env=env, capture_output=True, text=True, timeout=120,
        )
        if probe.returncode != 0 and "mcp" in probe.stderr:
            self.skipTest("the child could not import the mcp distribution")
        self.assertEqual(probe.returncode, 0, probe.stderr[-800:])
        self.assertEqual(probe.stdout.strip(), str(session_kv_server.DEFAULT_TRIAGE_WATCHDOG_SECONDS))
        self.assertIn("is not an integer; falling back to", probe.stderr)


class TriagePromptStampTest(unittest.TestCase):
    PAYLOAD = {"reason": "BackOff", "namespace": "payments", "kind_of_object": "Pod", "name": "api-1",
               "message": "Back-off restarting failed container app in pod api-1", "last_seen": LAST_SEEN}

    def test_asks_for_a_fresh_read_and_a_state_stamp(self):
        body = session_kv_server._triage_task_body(self.PAYLOAD)
        self.assertIn("**Last seen by the watcher:** 15:28 UTC", body)
        self.assertIn("Read the object's current state before you conclude anything", body)
        self.assertIn("`kubectl get` and `kubectl describe` on payments/Pod/api-1", body)
        self.assertIn("State at <HH:MM> UTC: <Short 1-sentence description of the problem>", body)
        self.assertIn("'**No action needed:** payments/Pod/api-1 has recovered'", body)
        self.assertIn("leave the 'To authorize:' bullet off entirely", body)
        # Still the three-section template the notifier and the bench contract key on.
        self.assertEqual(body.count("\n## "), 3)
        self.assertIn("**To authorize:** reply **'apply'**", body)

    def test_a_payload_without_a_stamp_still_reads(self):
        body = session_kv_server._triage_task_body({k: v for k, v in self.PAYLOAD.items() if k != "last_seen"})
        self.assertIn("**Last seen by the watcher:** a few minutes ago", body)
        self.assertEqual(session_kv_server._hhmm_utc("not a time"), "")
        self.assertEqual(session_kv_server._hhmm_utc("2026-08-10T15:28:52.123456789Z"), "15:28 UTC")


if __name__ == "__main__":
    unittest.main()
