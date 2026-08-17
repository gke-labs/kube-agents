"""Tests for the shared `send_notification` delivery path.

The module is shared by two very differently-privileged profiles — the Platform
Agent through platform_control, every Cluster Agent through the single-tool
notify server — so the tests here cover the behavior both of them depend on:
that a report reaches chat at all, and that it threads under the alert it
answers rather than landing in the home channel.
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import notify_delivery


def _env():
    return {"HOME": "/tmp"}


class TestSessionKvHeaders(unittest.TestCase):
    """The Session KV server rejects an unauthenticated caller with a 401.

    Every call site swallows that: `resolve_thread` fails open to the home
    channel and `store_incident` is non-fatal by design, so a missing token
    costs every alert-driven report its thread and stores no incident at all —
    silently. Hence a test on the header itself, plus one per calling profile on
    the config that has to carry the value into that subprocess.
    """

    def setUp(self):
        self._saved = os.environ.get("SESSION_KV_API_KEY")

    def tearDown(self):
        os.environ.pop("SESSION_KV_API_KEY", None)
        if self._saved is not None:
            os.environ["SESSION_KV_API_KEY"] = self._saved

    def test_the_configured_token_becomes_a_bearer_header(self):
        os.environ["SESSION_KV_API_KEY"] = "test-session-kv-key"
        headers = notify_delivery.session_kv_headers({"Content-Type": "application/json"})
        self.assertEqual(headers["Authorization"], "Bearer test-session-kv-key")
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_an_unset_token_sets_no_header(self):
        os.environ.pop("SESSION_KV_API_KEY", None)
        self.assertNotIn("Authorization", notify_delivery.session_kv_headers())

    def test_the_cluster_config_passes_the_key_into_the_notify_server(self):
        """Hermes hands a stdio MCP server only the keys named in `env`, so the
        header above is empty in a Cluster Agent unless its config lists this
        one. The platform side has the same assertion in
        test_platform_mcp_server.py against platform_control."""
        import yaml

        config_path = Path(__file__).resolve().parents[2] / "cluster" / "config.yaml"
        config = yaml.safe_load(config_path.read_text())
        env = config["mcp_servers"]["notify"]["env"]
        self.assertEqual(env.get("SESSION_KV_API_KEY"), "${SESSION_KV_API_KEY}")


class TestResolveThread(unittest.TestCase):

    def _metadata(self, meta, status=200):
        resp = MagicMock(status=status)
        resp.read.return_value = __import__("json").dumps(meta).encode()
        cm = MagicMock()
        cm.__enter__.return_value = resp
        return cm

    def test_a_recorded_thread_becomes_a_hermes_target(self):
        with patch("notify_delivery.urllib.request.urlopen",
                   return_value=self._metadata({"chat_id": "c1", "thread_id": "t1", "platform": "google_chat"})):
            chat_id, thread_id, target = notify_delivery.resolve_thread("k8s-evt-1", ["google_chat"])
        self.assertEqual((chat_id, thread_id), ("c1", "t1"))
        self.assertEqual(target, "google_chat:c1:t1")

    def test_the_watcher_is_not_a_chat_platform(self):
        # session_kv_server records "k8s-watcher" as the session's origin. Used
        # as a platform it produces `hermes send --to k8s-watcher:...`, which
        # has no adapter and delivers nothing.
        with patch("notify_delivery.urllib.request.urlopen",
                   return_value=self._metadata({"chat_id": "c1", "thread_id": "t1", "platform": "k8s-watcher"})):
            _, _, target = notify_delivery.resolve_thread("k8s-evt-1", ["google_chat"])
        self.assertEqual(target, "google_chat:c1:t1")

    def test_an_unreachable_session_kv_fails_open(self):
        # A report in the home channel beats no report, which is the failure
        # this module exists to end.
        with patch("notify_delivery.urllib.request.urlopen", side_effect=OSError("connection refused")):
            self.assertEqual(notify_delivery.resolve_thread("k8s-evt-1", ["slack"]), (None, None, None))

    def test_no_session_id_resolves_to_nothing_without_a_request(self):
        with patch("notify_delivery.urllib.request.urlopen") as urlopen:
            self.assertEqual(notify_delivery.resolve_thread("", ["slack"]), (None, None, None))
        urlopen.assert_not_called()


class TestDeliverNotification(unittest.TestCase):

    def setUp(self):
        # Fail the config read so `enabled_platforms` takes the env fallback,
        # which is the shape both the Platform Agent (no `platforms:` block) and
        # a scaffolded Cluster Agent actually run in.
        self.config_path = "/nonexistent/config.yaml"
        self._saved = {k: os.environ.get(k) for k in
                       ("SLACK_BOT_TOKEN", "SLACK_HOME_CHANNEL", "GOOGLE_CHAT_PROJECT_ID", "GOOGLE_CHAT_HOME_CHANNEL")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def _deliver(self, session_id="", thread=(None, None, None)):
        with patch("notify_delivery.resolve_thread", return_value=thread), \
             patch("notify_delivery.store_incident") as store, \
             patch("notify_delivery.subprocess.run",
                   return_value=MagicMock(stdout="ok", stderr="")) as run:
            out = notify_delivery.deliver_notification(
                "the report", session_id, config_path=self.config_path, run_env=_env)
        return out, run, store

    def test_a_threaded_report_is_sent_to_that_thread(self):
        os.environ["GOOGLE_CHAT_HOME_CHANNEL"] = "spaces/HOME"
        out, run, store = self._deliver("k8s-evt-1", ("c1", "t1", "google_chat:c1:t1"))
        self.assertEqual(run.call_args[0][0], ["hermes", "send", "--to", "google_chat:c1:t1", "the report"])
        self.assertIn("SUCCESS", out)
        # The follow-up "apply" reply arrives as fresh chat ingress on the front
        # door, and the stored incident is the only way that turn sees the RCA.
        store.assert_called_once_with("c1", "t1", "the report")

    def test_an_unthreaded_report_falls_back_to_the_home_channel(self):
        os.environ["GOOGLE_CHAT_HOME_CHANNEL"] = "spaces/HOME"
        out, run, store = self._deliver()
        self.assertEqual(run.call_args[0][0][3], "google_chat:spaces/HOME")
        self.assertIn("SUCCESS", out)
        # No thread means no incident to key on; storing under a null thread
        # would make the next reply pick up an unrelated report.
        store.assert_not_called()

    def test_a_dead_platform_does_not_stop_the_other(self):
        os.environ["SLACK_HOME_CHANNEL"] = "C123"
        os.environ["GOOGLE_CHAT_HOME_CHANNEL"] = "spaces/HOME"
        with patch("notify_delivery.resolve_thread", return_value=(None, None, None)), \
             patch("notify_delivery.subprocess.run",
                   side_effect=[subprocess.CalledProcessError(1, "hermes", stderr="slack is down"),
                                MagicMock(stdout="ok", stderr="")]):
            out = notify_delivery.deliver_notification(
                "the report", config_path=self.config_path, run_env=_env)
        self.assertIn("ERROR: Failed to send notification to slack", out)
        self.assertIn("SUCCESS: Notification posted to google_chat", out)

    def test_the_model_is_told_when_nothing_was_sent(self):
        # Silence here is the #630 failure in miniature: the agent believes it
        # reported and moves on.
        with patch("notify_delivery.enabled_platforms", return_value=[]), \
             patch("notify_delivery.resolve_thread", return_value=(None, None, None)), \
             patch("notify_delivery.subprocess.run") as run:
            out = notify_delivery.deliver_notification(
                "the report", config_path=self.config_path, run_env=_env)
        run.assert_not_called()
        self.assertIn("ERROR", out)


if __name__ == "__main__":
    unittest.main()
