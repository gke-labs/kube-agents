"""Tests for chat_notify: the switch from `hermes send` to the gateway's chat.notify route."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chat_notify  # noqa: E402
import chat_platforms  # noqa: E402

ROUTED = {chat_notify.NOTIFY_PLATFORM_ENV: "google_chat"}


class CommandTest(unittest.TestCase):
    def test_unrouted_targets_keep_hermes_send(self):
        with mock.patch.dict(os.environ, {chat_notify.NOTIFY_PLATFORM_ENV: ""}):
            self.assertEqual(
                chat_notify.command("google_chat", "hi"),
                ["hermes", "send", "--json", "--to", "google_chat", "hi"],
            )
            self.assertEqual(
                chat_notify.command("slack:C1:123.4", "hi", json_output=False, hermes_bin="/opt/hermes"),
                ["/opt/hermes", "send", "--to", "slack:C1:123.4", "hi"],
            )

    def test_the_routed_platform_goes_through_the_gateway(self):
        with mock.patch.dict(os.environ, ROUTED):
            self.assertEqual(
                chat_notify.command("google_chat", "alert"),
                ["a2a", "notify", "--platform", "google_chat", "--", "alert"],
            )
            # A threaded target replies on the thread; the chat id is not
            # forwarded, because the thread names its own space.
            self.assertEqual(
                chat_notify.command("google_chat:spaces/H:spaces/H/threads/T", "follow-up", json_output=False),
                ["a2a", "notify", "--platform", "google_chat", "--thread", "spaces/H/threads/T", "--", "follow-up"],
            )
            # A chat id with no thread is a new thread in the home channel.
            self.assertEqual(
                chat_notify.command("google_chat:spaces/H", "x"),
                ["a2a", "notify", "--platform", "google_chat", "--", "x"],
            )

    def test_text_that_looks_like_a_flag_stays_text(self):
        # "--" ends the CLI's flags, so a bullet, a rule, a negative number, or
        # a message that is exactly a valid flag cannot redirect the post or
        # make the CLI read stdin.
        with mock.patch.dict(os.environ, ROUTED):
            for text in ("- pod crashlooping", "--- Daily report", "-1 nodes down", "--thread=spaces/X/threads/Y", "-"):
                self.assertEqual(chat_notify.command("google_chat", text)[-2:], ["--", text])

    def test_an_update_key_rides_the_gateway_path_only(self):
        # The event alert's key, which lets its triage edit it in place under
        # next; hermes send has no such key.
        with mock.patch.dict(os.environ, ROUTED):
            self.assertEqual(
                chat_notify.command("google_chat", "alert", update=chat_notify.alert_update_key("k8s-evt-1")),
                ["a2a", "notify", "--platform", "google_chat", "--update", "alert-k8s-evt-1", "--", "alert"],
            )
        with mock.patch.dict(os.environ, {chat_notify.NOTIFY_PLATFORM_ENV: ""}):
            self.assertEqual(
                chat_notify.command("google_chat", "alert", update=chat_notify.alert_update_key("k8s-evt-1")),
                ["hermes", "send", "--json", "--to", "google_chat", "alert"],
            )

    def test_a_caller_deadline_becomes_the_cli_wait(self):
        with mock.patch.dict(os.environ, ROUTED):
            argv = chat_notify.command("google_chat", "x", wait_seconds=12.7)
        self.assertEqual(argv[4:6], ["--timeout", "12s"])

    def test_the_kill_timeout_covers_the_cli_for_a_routed_platform_only(self):
        with mock.patch.dict(os.environ, ROUTED):
            self.assertEqual(chat_notify.subprocess_timeout("google_chat", 30), chat_notify.NOTIFY_SUBPROCESS_TIMEOUT_SECONDS)
            self.assertEqual(chat_notify.subprocess_timeout("google_chat:spaces/H:spaces/H/threads/T", None),
                             chat_notify.NOTIFY_SUBPROCESS_TIMEOUT_SECONDS)
            self.assertEqual(chat_notify.subprocess_timeout("slack", 30), 30)
            self.assertIsNone(chat_notify.subprocess_timeout("slack", None))

    def test_only_the_routed_platform_is_rerouted(self):
        with mock.patch.dict(os.environ, ROUTED):
            self.assertEqual(chat_notify.command("slack", "x")[:2], ["hermes", "send"])

    def test_a_blank_value_routes_nothing(self):
        with mock.patch.dict(os.environ, {chat_notify.NOTIFY_PLATFORM_ENV: "  "}):
            self.assertFalse(chat_notify.routes("google_chat"))
            self.assertFalse(chat_notify.routes(""))


class ThreadFromResponseTest(unittest.TestCase):
    def test_the_gateways_thread_wins(self):
        resp = {"message_id": "spaces/H/messages/M1.M1", "thread_id": "spaces/H/threads/T9"}
        self.assertEqual(chat_notify.thread_from_response("google_chat", resp), "spaces/H/threads/T9")

    def test_hermes_google_chat_thread_is_derived_from_the_message(self):
        resp = {"message_id": "spaces/H/messages/M1.M1"}
        self.assertEqual(chat_notify.thread_from_response("google_chat", resp), "spaces/H/threads/M1")

    def test_other_platforms_use_the_message_id(self):
        self.assertEqual(chat_notify.thread_from_response("slack", {"message_id": "123.456"}), "123.456")

    def test_no_message_no_thread(self):
        self.assertEqual(chat_notify.thread_from_response("google_chat", {}), "")
        self.assertEqual(chat_notify.thread_from_response("google_chat", None), "")


class EnabledPlatformsTest(unittest.TestCase):
    """The managed scope says the Hermes platform is off under next; the routed platform still counts."""

    def test_managed_false_does_not_hide_the_routed_platform(self):
        with mock.patch.object(chat_platforms, "_platforms_enabled_in", return_value={"google_chat": False, "slack": True}), \
                mock.patch.dict(os.environ, ROUTED):
            self.assertEqual(chat_platforms.enabled_chat_platforms(), ["google_chat", "slack"])

    def test_without_the_route_managed_false_still_wins(self):
        with mock.patch.object(chat_platforms, "_platforms_enabled_in", return_value={"google_chat": False, "slack": True}), \
                mock.patch.dict(os.environ, {chat_notify.NOTIFY_PLATFORM_ENV: ""}):
            self.assertEqual(chat_platforms.enabled_chat_platforms(), ["slack"])


def _completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


class SessionKVCallersTest(unittest.TestCase):
    """The alert path and the cron relay post through the gateway under next, and read its thread."""

    @classmethod
    def setUpClass(cls):
        # The server opens its SQLite file at import; point it somewhere
        # writable first, as test_session_kv_server.py does. Not a skip on
        # failure: a caller test that skips is the gap this file exists for.
        os.environ.setdefault("SESSION_KV_DB_PATH", os.path.join(tempfile.mkdtemp(), "sessions.db"))
        import session_kv_server  # noqa: F401
        cls.skv = sys.modules["session_kv_server"]

    def test_the_alert_goes_through_the_gateway_and_threads_on_its_answer(self):
        answer = json.dumps({"message_id": "spaces/H/messages/X.X", "thread_id": "spaces/H/threads/REAL"})
        with mock.patch.dict(os.environ, ROUTED), \
                mock.patch.object(self.skv.subprocess, "run", return_value=_completed(answer)) as run:
            thread = self.skv._post_initial_alert("google_chat", "Warning: pod crashlooping", "k8s-evt-9")
        self.assertEqual(run.call_args.args[0][:4], ["a2a", "notify", "--platform", "google_chat"])
        self.assertEqual(thread, "spaces/H/threads/REAL")
        # Its own update key, so the triage can edit this alert in place.
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("--update") + 1], chat_notify.alert_update_key("k8s-evt-9"))

    def test_the_relay_replies_on_a_known_thread_through_the_gateway(self):
        with mock.patch.dict(os.environ, ROUTED), \
                mock.patch.object(self.skv.subprocess, "run", return_value=_completed('{"message_id":"m"}')) as run:
            thread = self.skv._send_to_chat("google_chat", "report", "spaces/H", "spaces/H/threads/T")
        self.assertIn("--thread", run.call_args.args[0])
        self.assertEqual(thread, "spaces/H/threads/T")

    def test_no_answer_in_time_is_treated_as_sent(self):
        # Outcome unknown: the alert may be in the channel, so the alert path
        # must not fall through and send it again.
        err = subprocess.CalledProcessError(chat_notify.NOTIFY_OUTCOME_UNKNOWN, ["a2a"], stderr="no answer")
        with mock.patch.dict(os.environ, ROUTED), \
                mock.patch.object(self.skv.subprocess, "run", side_effect=err):
            self.assertEqual(self.skv._post_initial_alert("google_chat", "Warning"), self.skv.ALERT_SENT_WITHOUT_THREAD)

    def test_the_relay_treats_exit_3_as_sent_without_a_thread(self):
        err = subprocess.CalledProcessError(chat_notify.NOTIFY_OUTCOME_UNKNOWN, ["a2a"], stderr="no answer")
        with mock.patch.dict(os.environ, ROUTED), \
                mock.patch.object(self.skv.subprocess, "run", side_effect=err):
            self.assertEqual(self.skv._send_to_chat("google_chat", "report"), self.skv.ALERT_SENT_WITHOUT_THREAD)

    def test_the_relay_keeps_a_known_thread_on_exit_3(self):
        # A reply into a thread the report already has keeps that thread when
        # the outcome is unknown, as it does on success, so the leg is still
        # registered and its incident row updated.
        err = subprocess.CalledProcessError(chat_notify.NOTIFY_OUTCOME_UNKNOWN, ["a2a"], stderr="no answer")
        with mock.patch.dict(os.environ, ROUTED), \
                mock.patch.object(self.skv.subprocess, "run", side_effect=err):
            self.assertEqual(self.skv._send_to_chat("google_chat", "report", "spaces/H", "spaces/H/threads/T"),
                             "spaces/H/threads/T")

    def test_the_relay_files_an_exit_3_leg_as_delivered_and_does_not_log_it_undelivered(self):
        skv = self.skv
        with mock.patch.object(skv, "enabled_chat_platforms", return_value=["google_chat"]), \
                mock.patch.object(skv, "_ensure_session_row"), \
                mock.patch.object(skv, "_lookup_session_routing", return_value=("", "", "")), \
                mock.patch.object(skv, "_lookup_platform_threads", return_value={}), \
                mock.patch.object(skv, "_slack_audit_headline", return_value=None), \
                mock.patch.object(skv, "_send_to_chat", return_value=skv.ALERT_SENT_WITHOUT_THREAD), \
                self.assertLogs(skv.logger, level="WARNING") as logs:
            result = skv.relay_cron_report("sess", "audit", "job", "Audit", "report")
        self.assertEqual(result, (None, []))
        self.assertFalse([line for line in logs.output if "not delivered" in line], logs.output)
        self.assertTrue([line for line in logs.output if "treated as delivered" in line], logs.output)

    def test_the_alert_waits_out_a_route_that_is_briefly_not_there(self):
        unavailable = subprocess.CalledProcessError(chat_notify.NOTIFY_ROUTE_UNAVAILABLE, ["a2a"], stderr="no responders")
        answer = _completed(json.dumps({"message_id": "spaces/H/messages/X", "thread_id": "spaces/H/threads/T"}))
        with mock.patch.dict(os.environ, ROUTED), \
                mock.patch.object(self.skv.time, "sleep") as sleep, \
                mock.patch.object(self.skv.subprocess, "run", side_effect=[unavailable, unavailable, answer]) as run:
            thread = self.skv._post_initial_alert("google_chat", "Warning")
        self.assertEqual(thread, "spaces/H/threads/T")
        self.assertEqual(run.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], list(chat_notify.NOTIFY_ROUTE_RETRY_DELAYS_SECONDS[:2]))

    def test_the_alert_gives_up_once_the_route_stays_away(self):
        unavailable = subprocess.CalledProcessError(chat_notify.NOTIFY_ROUTE_UNAVAILABLE, ["a2a"], stderr="no responders")
        with mock.patch.dict(os.environ, ROUTED), \
                mock.patch.object(self.skv.time, "sleep"), \
                mock.patch.object(self.skv.subprocess, "run", side_effect=unavailable) as run:
            self.assertIsNone(self.skv._post_initial_alert("google_chat", "Warning"))
        self.assertEqual(run.call_count, len(chat_notify.NOTIFY_ROUTE_RETRY_DELAYS_SECONDS) + 1)

    def test_a_refusal_is_not_retried(self):
        err = subprocess.CalledProcessError(1, ["a2a"], stderr="refused")
        with mock.patch.dict(os.environ, ROUTED), \
                mock.patch.object(self.skv.time, "sleep") as sleep, \
                mock.patch.object(self.skv.subprocess, "run", side_effect=err) as run:
            self.assertIsNone(self.skv._post_initial_alert("google_chat", "Warning"))
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()

    def test_a_refusal_is_a_failure(self):
        err = subprocess.CalledProcessError(1, ["a2a"], stderr="route not armed")
        with mock.patch.dict(os.environ, ROUTED), \
                mock.patch.object(self.skv.subprocess, "run", side_effect=err):
            self.assertIsNone(self.skv._post_initial_alert("google_chat", "Warning"))

    def test_the_alert_paths_platform_list_counts_the_routed_platform(self):
        # session_kv_server keeps its own copy of the resolution; the alert
        # path reads this one, not chat_platforms'.
        # Slack stays on, so the default (google_chat) cannot answer for the
        # routing: an empty resolution falls back to it on its own.
        with mock.patch.object(self.skv, "_platforms_enabled_in", return_value={"google_chat": False, "slack": True}), \
                mock.patch.dict(os.environ, ROUTED):
            self.assertEqual(self.skv.enabled_chat_platforms(), ["google_chat", "slack"])
        with mock.patch.object(self.skv, "_platforms_enabled_in", return_value={"google_chat": False, "slack": True}), \
                mock.patch.dict(os.environ, {chat_notify.NOTIFY_PLATFORM_ENV: ""}):
            self.assertEqual(self.skv.enabled_chat_platforms(), ["slack"])

    def test_a_non_object_answer_does_not_raise_into_the_relay(self):
        for stdout in ('["m"]', '"m"', "7"):
            with mock.patch.dict(os.environ, ROUTED), \
                    mock.patch.object(self.skv.subprocess, "run", return_value=_completed(stdout)):
                self.assertIsNone(self.skv._send_to_chat("google_chat", "report"), stdout)

    def test_today_still_runs_hermes_send(self):
        with mock.patch.dict(os.environ, {chat_notify.NOTIFY_PLATFORM_ENV: ""}), \
                mock.patch.object(self.skv.subprocess, "run",
                                  return_value=_completed('{"message_id":"spaces/H/messages/M.M"}')) as run:
            thread = self.skv._post_initial_alert("google_chat", "Warning")
        self.assertEqual(run.call_args.args[0][:2], ["hermes", "send"])
        self.assertEqual(thread, "spaces/H/threads/M")


class OtherCallersTest(unittest.TestCase):
    """send_notification and the Cluster Agent reconcile notice reroute the same way."""

    def test_send_notification_posts_through_the_gateway(self):
        import platform_mcp_server as mcp_server

        with mock.patch.dict(os.environ, ROUTED), \
                mock.patch.object(mcp_server, "_run_env", return_value={}), \
                mock.patch.object(mcp_server.subprocess, "run", return_value=_completed("{}")) as run:
            result = mcp_server.send_notification("- the node pool is out of capacity", session_id="")
        self.assertIn("SUCCESS", result)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ["a2a", "notify", "--platform", "google_chat"])
        self.assertEqual(argv[-2:], ["--", "- the node pool is out of capacity"])
        self.assertIs(run.call_args.kwargs.get("stdin"), subprocess.DEVNULL)

    def test_send_notification_reports_exit_3_as_may_have_posted(self):
        import platform_mcp_server as mcp_server

        err = subprocess.CalledProcessError(chat_notify.NOTIFY_OUTCOME_UNKNOWN, ["a2a"], stderr="no answer")
        with mock.patch.dict(os.environ, ROUTED), \
                mock.patch.object(mcp_server, "_run_env", return_value={}), \
                mock.patch.object(mcp_server.subprocess, "run", side_effect=err):
            result = mcp_server.send_notification("x", session_id="")
        self.assertIn("may have posted", result)
        self.assertNotIn("ERROR", result)

    def test_the_reconcile_notice_waits_for_the_cli(self):
        import cluster_agent_reconcile as rec

        with mock.patch.dict(os.environ, ROUTED), \
                mock.patch.object(rec, "enabled_chat_platforms", return_value=["google_chat"]), \
                mock.patch.object(rec.subprocess, "run") as run:
            rec._notify("created 1 profile(s): demo")
        self.assertGreaterEqual(run.call_args.kwargs["timeout"], chat_notify.NOTIFY_SUBPROCESS_TIMEOUT_SECONDS)

    def test_the_reconcile_notice_posts_through_the_gateway(self):
        import cluster_agent_reconcile as rec

        with mock.patch.dict(os.environ, ROUTED), \
                mock.patch.object(rec, "enabled_chat_platforms", return_value=["google_chat"]), \
                mock.patch.object(rec.subprocess, "run") as run:
            rec._notify("created 1 profile(s): demo")
        self.assertEqual(run.call_args.args[0][:4], ["a2a", "notify", "--platform", "google_chat"])




SLACK_ROUTED = {chat_notify.NOTIFY_PLATFORM_ENV: "slack"}


class SlackRouteTest(unittest.TestCase):
    """Slack's half: the same switch, a ts for a thread, and Block Kit through the gateway."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("SESSION_KV_DB_PATH", os.path.join(tempfile.mkdtemp(), "sessions.db"))
        import session_kv_server  # noqa: F401
        cls.skv = sys.modules["session_kv_server"]

    def test_a_slack_target_goes_through_the_gateway_with_its_ts(self):
        with mock.patch.dict(os.environ, SLACK_ROUTED):
            argv = chat_notify.command("slack:C0HOME:1700000000.000100", "drift on prod")
        self.assertEqual(argv, ["a2a", "notify", "--platform", "slack", "--thread", "C0HOME/1700000000.000100", "--", "drift on prod"])

    def test_a_slack_thread_carries_its_own_channel(self):
        # A today-era DM thread: the gateway refuses a non-home channel, so the
        # follow-up is not posted into home.
        with mock.patch.dict(os.environ, SLACK_ROUTED):
            argv = chat_notify.command("slack:D0DM:1700000000.000200", "follow-up")
        self.assertEqual(argv[argv.index("--thread") + 1], "D0DM/1700000000.000200")

    def test_slack_threads_come_from_the_answer_or_the_ts(self):
        self.assertEqual(chat_notify.thread_from_response("slack", {"message_id": "2.2", "thread_id": "1.1"}), "1.1")
        # The Hermes path names only the message; on Slack its ts is the root.
        self.assertEqual(chat_notify.thread_from_response("slack", {"message_id": "2.2"}), "2.2")

    def test_chat_command(self):
        self.assertEqual(
            chat_notify.chat_command("slack", "1.1", "3 findings", "/tmp/c.json"),
            ["a2a", "notify", "--platform", "slack", "--chat-file", "/tmp/c.json", "--thread", "1.1", "--", "3 findings"],
        )
        self.assertNotIn("--thread", chat_notify.chat_command("slack", "", "x", "/tmp/c.json"))
        argv = chat_notify.chat_command("slack", "", "x", "/tmp/c.json", 27.5)
        self.assertEqual(argv[argv.index("--timeout") + 1], "27s")

    CHAT = {"schema": "kube-agents.chat/v1", "headline": "Security audit: 2 critical findings",
            "choices": [{"text": "Look at the first one", "primary": True}]}

    def _gateway_post(self, returncode, stdout, thread=""):
        seen = {}

        def run(argv, **_kwargs):
            path = argv[argv.index("--chat-file") + 1]
            with open(path, encoding="utf-8") as handle:
                seen["chat"] = json.load(handle)
            seen["path"], seen["argv"] = path, argv
            return subprocess.CompletedProcess(args=argv, returncode=returncode, stdout=stdout, stderr="refused")

        with mock.patch.object(self.skv.subprocess, "run", side_effect=run):
            post = self.skv._post_audit_chat_via_gateway("platform", "fleet-audit", self.CHAT, "3 findings", thread, 30)
        return post, seen

    def test_the_gateway_card_keeps_its_buttons(self):
        # The gateway draws a chat message's choices itself, so nothing is stripped.
        _post, seen = self._gateway_post(0, json.dumps({"message_id": "1.5", "thread_id": "1.5"}))
        self.assertEqual(seen["chat"], self.CHAT)

    def test_the_cli_gives_up_before_the_subprocess_bound(self):
        _post, seen = self._gateway_post(0, json.dumps({"message_id": "1.5", "thread_id": "1.5"}))
        argv = seen["argv"]
        self.assertIn("--timeout", argv)
        self.assertLess(int(argv[argv.index("--timeout") + 1].rstrip("s")), 30)

    def test_the_audit_card_goes_through_the_gateway_and_threads_on_its_answer(self):
        post, seen = self._gateway_post(0, json.dumps({"message_id": "1.5", "thread_id": "1.5"}))
        self.assertEqual(post, self.skv.AuditPost("1.5"))
        self.assertEqual(seen["argv"][:4], ["a2a", "notify", "--platform", "slack"])
        self.assertEqual(seen["argv"][-2:], ["--", "3 findings"])
        self.assertFalse(os.path.exists(seen["path"]), "the chat file was left behind")

    def test_the_audit_card_replies_on_a_known_thread(self):
        post, seen = self._gateway_post(0, json.dumps({"message_id": "1.6", "thread_id": "1.1"}), thread="1.1")
        self.assertEqual(post, self.skv.AuditPost("1.1"))
        self.assertIn("--thread", seen["argv"])

    def test_the_audit_card_falls_back_to_text_on_a_refusal_or_an_unknown_outcome(self):
        for code in (1, chat_notify.NOTIFY_OUTCOME_UNKNOWN):
            post, seen = self._gateway_post(code, "")
            self.assertIsNone(post, f"exit {code}")
            self.assertFalse(os.path.exists(seen["path"]))

    def test_the_audit_report_takes_the_gateway_as_a_chat_message_when_slack_is_routed(self):
        # No broker relay under next (slack_blocks_post is not configured),
        # so without the routed check the report would never try a card.
        headline = self.skv.AuditHeadline(text="3 findings", issue={"number": 1}, ref=None)
        with mock.patch.dict(os.environ, SLACK_ROUTED), \
                mock.patch.object(self.skv.slack_blocks_post, "configured", return_value=False), \
                mock.patch.object(self.skv.slack_audit_report, "blocks_from_issue") as blocks, \
                mock.patch.object(self.skv.slack_audit_report, "chat_from_issue", return_value=(self.CHAT, "t")), \
                mock.patch.object(self.skv, "_post_audit_chat_via_gateway", return_value=self.skv.AuditPost("9.9")) as via:
            post = self.skv._post_audit_blocks("platform", "fleet-audit", headline, "m", "", "", float("inf"))
        self.assertEqual(post, self.skv.AuditPost("9.9"))
        self.assertEqual(via.call_args.args[2:4], (self.CHAT, "t"))
        blocks.assert_not_called()

    def test_the_audit_report_keeps_block_kit_when_slack_is_not_routed(self):
        headline = self.skv.AuditHeadline(text="3 findings", issue={"number": 1}, ref=None)
        with mock.patch.dict(os.environ, {chat_notify.NOTIFY_PLATFORM_ENV: ""}), \
                mock.patch.object(self.skv.slack_blocks_post, "configured", return_value=True), \
                mock.patch.object(self.skv.slack_audit_report, "chat_from_issue") as chat, \
                mock.patch.object(self.skv.slack_audit_report, "blocks_from_issue", return_value=([{"type": "divider"}], "t")), \
                mock.patch.object(self.skv.slack_blocks_post, "post", return_value="4.4") as post_blocks:
            post = self.skv._post_audit_blocks("platform", "fleet-audit", headline, "m", "C1", "", float("inf"))
        self.assertEqual(post, self.skv.AuditPost("4.4"))
        self.assertEqual(post_blocks.call_args.args[:3], ("C1", "t", [{"type": "divider"}]))
        chat.assert_not_called()

class ConversationRouteTest(unittest.TestCase):
    """The route the hermes-bridge records, and the post into that conversation."""

    ROUTE = {"platform": "slack", "conversation": "slack:C0HOME/1.1", "context_id": "ctx-1", "ask": "question"}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = os.path.join(tmp.name, "session_kv.db")
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE session_metadata (session_id TEXT PRIMARY KEY, metadata TEXT)")
            conn.execute("INSERT INTO session_metadata VALUES (?, ?)",
                         ("a2a-ctx-1", json.dumps({"conversation_route": self.ROUTE, "other": 1})))
            conn.execute("INSERT INTO session_metadata VALUES (?, ?)",
                         ("a2a-ctx-2", json.dumps({"conversation_route": {**self.ROUTE, "context_id": ""}})))
            conn.execute("INSERT INTO session_metadata VALUES (?, ?)", ("a2a-ctx-3", "not json"))
        patcher = mock.patch.dict(os.environ, {chat_notify.SESSION_KV_DB_ENV: self.db})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_recorded_route_is_read_without_its_ask(self):
        self.assertEqual(
            chat_notify.conversation_route("a2a-ctx-1"),
            {"platform": "slack", "conversation": "slack:C0HOME/1.1", "context_id": "ctx-1"},
        )

    def test_no_usable_route_is_empty(self):
        for session in ("a2a-ctx-2", "a2a-ctx-3", "a2a-ctx-absent", ""):
            with self.subTest(session=session):
                self.assertEqual(chat_notify.conversation_route(session), {})
        with mock.patch.dict(os.environ, {chat_notify.SESSION_KV_DB_ENV: self.db + ".absent"}):
            self.assertEqual(chat_notify.conversation_route("a2a-ctx-1"), {})

    def test_conversation_command(self):
        self.assertEqual(
            chat_notify.conversation_command("slack", "slack:D0DM/1.1", "ctx-1", "-- the report"),
            ["a2a", "notify", "--platform", "slack", "--conversation", "slack:D0DM/1.1", "--context", "ctx-1",
             "--", "-- the report"],
        )
        argv = chat_notify.conversation_command("slack", "slack:D0DM/1.1", "ctx-1", "x", "/tmp/c.json", "key", 60)
        self.assertEqual(argv[argv.index("--chat-file") + 1], "/tmp/c.json")
        self.assertEqual(argv[argv.index("--update") + 1], "key")
        self.assertEqual(argv[argv.index("--timeout") + 1], "60s")
        argv = chat_notify.conversation_command("slack", "slack:D0DM/1.1", "ctx-1", "x", timeout_s=0.5)
        self.assertEqual(argv[argv.index("--timeout") + 1], "1s")

    def test_the_bus_and_the_served_platform_come_from_the_env(self):
        with mock.patch.dict(os.environ, {chat_notify.NOTIFY_CONVERSATIONS_ENV: " slack ", "NATS_URL": "nats://n:4222",
                                          "A2A_BUS_USER": "agent", "NATS_USER": ""}):
            self.assertEqual(chat_notify.gateway_platform(), "slack")
            self.assertEqual(chat_notify.bus_unreachable_reason(), "")
            with mock.patch.dict(os.environ, {"A2A_BUS_USER": "", "NATS_USER": "legacy"}):
                self.assertEqual(chat_notify.bus_unreachable_reason(), "")
            with mock.patch.dict(os.environ, {"A2A_BUS_USER": ""}):
                self.assertIn("no bus identity", chat_notify.bus_unreachable_reason())
            with mock.patch.dict(os.environ, {"NATS_URL": ""}):
                self.assertIn("NATS_URL", chat_notify.bus_unreachable_reason())


if __name__ == "__main__":
    unittest.main()
