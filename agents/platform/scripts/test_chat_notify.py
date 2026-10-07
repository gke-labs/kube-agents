"""Tests for chat_notify: the switch from `hermes send` to the gateway's chat.notify route."""

import json
import os
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
            thread = self.skv._post_initial_alert("google_chat", "Warning: pod crashlooping")
        self.assertEqual(run.call_args.args[0][:4], ["a2a", "notify", "--platform", "google_chat"])
        self.assertEqual(thread, "spaces/H/threads/REAL")

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

    def test_the_reconcile_notice_posts_through_the_gateway(self):
        import cluster_agent_reconcile as rec

        with mock.patch.dict(os.environ, ROUTED), \
                mock.patch.object(rec, "enabled_chat_platforms", return_value=["google_chat"]), \
                mock.patch.object(rec.subprocess, "run") as run:
            rec._notify("created 1 profile(s): demo")
        self.assertEqual(run.call_args.args[0][:4], ["a2a", "notify", "--platform", "google_chat"])


if __name__ == "__main__":
    unittest.main()
