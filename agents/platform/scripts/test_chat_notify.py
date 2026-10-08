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
                mock.patch.object(skv, "_gateway_api_token", return_value=""), \
                mock.patch.object(skv, "_ensure_session_row"), \
                mock.patch.object(skv, "_create_gateway_session", return_value=True), \
                mock.patch.object(skv, "_run_relay_turn", return_value="composed report"), \
                mock.patch.object(skv, "_lookup_session_routing", return_value=("", "", "")), \
                mock.patch.object(skv, "_lookup_platform_threads", return_value={}), \
                mock.patch.object(skv, "_slack_audit_headline", return_value=None), \
                mock.patch.object(skv, "_send_to_chat", return_value=skv.ALERT_SENT_WITHOUT_THREAD), \
                self.assertLogs(skv.logger, level="WARNING") as logs:
            result = skv.relay_cron_report("sess", "audit", "job", "Audit", "report")
        self.assertEqual(result, (None, "", []))
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


if __name__ == "__main__":
    unittest.main()


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
        self.assertEqual(argv, ["a2a", "notify", "--platform", "slack", "--thread", "1700000000.000100", "--", "drift on prod"])

    def test_slack_threads_come_from_the_answer_or_the_ts(self):
        self.assertEqual(chat_notify.thread_from_response("slack", {"message_id": "2.2", "thread_id": "1.1"}), "1.1")
        # The Hermes path names only the message; on Slack its ts is the root.
        self.assertEqual(chat_notify.thread_from_response("slack", {"message_id": "2.2"}), "2.2")

    def test_blocks_command(self):
        self.assertEqual(
            chat_notify.blocks_command("slack", "1.1", "3 findings", "/tmp/b.json"),
            ["a2a", "notify", "--platform", "slack", "--blocks-file", "/tmp/b.json", "--thread", "1.1", "--", "3 findings"],
        )
        self.assertNotIn("--thread", chat_notify.blocks_command("slack", "", "x", "/tmp/b.json"))

    def _gateway_post(self, returncode, stdout, thread="", blocks=None):
        seen = {}

        def run(argv, **_kwargs):
            path = argv[argv.index("--blocks-file") + 1]
            with open(path, encoding="utf-8") as handle:
                seen["blocks"] = json.load(handle)
            seen["path"], seen["argv"] = path, argv
            return subprocess.CompletedProcess(args=argv, returncode=returncode, stdout=stdout, stderr="refused")

        blocks = blocks or [{"type": "section", "text": {"type": "mrkdwn", "text": "*3 findings*"}}]
        with mock.patch.object(self.skv.subprocess, "run", side_effect=run):
            post = self.skv._post_audit_blocks_via_gateway("platform", "fleet-audit", blocks, "3 findings", thread, 30)
        return post, seen, blocks

    def test_the_gateway_card_carries_nothing_to_click(self):
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": "*3 findings*"},
             "accessory": {"type": "button", "action_id": "x", "text": {"type": "plain_text", "text": "Act"}}},
            {"type": "actions", "elements": [
                {"type": "button", "action_id": "kage_audit.choice.0", "text": {"type": "plain_text", "text": "Look at the first one"}},
                {"type": "button", "text": {"type": "plain_text", "text": "Ledger issue #7 ↗"}, "url": "https://github.com/o/r/issues/7"},
            ]},
        ]
        out = self.skv._without_interaction(blocks)
        self.assertNotIn("actions", [b["type"] for b in out])
        self.assertNotIn("accessory", out[0])
        self.assertEqual(out[-1]["type"], "context")
        self.assertIn("<https://github.com/o/r/issues/7|Ledger issue #7 ↗>", out[-1]["elements"][0]["text"])
        self.assertNotIn("kage_audit.choice.0", json.dumps(out))

    def test_the_gateway_path_sends_the_card_without_its_buttons(self):
        card = [
            {"type": "section", "text": {"type": "mrkdwn", "text": "*3 findings*"}},
            {"type": "actions", "elements": [
                {"type": "button", "action_id": "kage_audit.choice.0", "text": {"type": "plain_text", "text": "Look"}}]},
        ]
        _post, seen, _ = self._gateway_post(0, json.dumps({"message_id": "1.5", "thread_id": "1.5"}), blocks=card)
        self.assertNotIn("actions", [b["type"] for b in seen["blocks"]])

    def test_the_cli_gives_up_before_the_subprocess_bound(self):
        _post, seen, _ = self._gateway_post(0, json.dumps({"message_id": "1.5", "thread_id": "1.5"}))
        argv = seen["argv"]
        self.assertIn("--timeout", argv)
        self.assertLess(int(argv[argv.index("--timeout") + 1].rstrip("s")), 30)

    def test_audit_blocks_go_through_the_gateway_and_thread_on_its_answer(self):
        post, seen, blocks = self._gateway_post(0, json.dumps({"message_id": "1.5", "thread_id": "1.5"}))
        self.assertEqual(post, self.skv.AuditPost("1.5"))
        self.assertEqual(seen["blocks"], self.skv._without_interaction(blocks))
        self.assertEqual(seen["argv"][:4], ["a2a", "notify", "--platform", "slack"])
        self.assertFalse(os.path.exists(seen["path"]), "the blocks file was left behind")

    def test_audit_blocks_reply_on_a_known_thread(self):
        post, seen, _ = self._gateway_post(0, json.dumps({"message_id": "1.6", "thread_id": "1.1"}), thread="1.1")
        self.assertEqual(post, self.skv.AuditPost("1.1"))
        self.assertIn("--thread", seen["argv"])

    def test_audit_blocks_fall_back_to_text_on_a_refusal_or_an_unknown_outcome(self):
        for code in (1, chat_notify.NOTIFY_OUTCOME_UNKNOWN):
            post, seen, _ = self._gateway_post(code, "")
            self.assertIsNone(post, f"exit {code}")
            self.assertFalse(os.path.exists(seen["path"]))

    def test_the_audit_report_takes_the_gateway_when_slack_is_routed(self):
        # No broker relay under next (slack_blocks_post is not configured),
        # so without the routed check the report would never try blocks.
        headline = self.skv.AuditHeadline(text="3 findings", issue={"number": 1}, ref=None)
        with mock.patch.dict(os.environ, SLACK_ROUTED), \
                mock.patch.object(self.skv.slack_blocks_post, "configured", return_value=False), \
                mock.patch.object(self.skv.slack_audit_report, "blocks_from_issue", return_value=([{"type": "divider"}], "t")), \
                mock.patch.object(self.skv, "_post_audit_blocks_via_gateway", return_value=self.skv.AuditPost("9.9")) as via:
            post = self.skv._post_audit_blocks("platform", "fleet-audit", headline, "m", "", "", float("inf"))
        self.assertEqual(post, self.skv.AuditPost("9.9"))
        via.assert_called_once()
