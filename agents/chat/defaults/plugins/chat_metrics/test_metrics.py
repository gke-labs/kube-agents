"""The chat_metrics plugin: inbound chat messages by platform, served on a metrics-only listener.

Run: cd agents/chat/defaults/plugins && python3 -m pytest chat_metrics/test_metrics.py -q
"""

import os
import re
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_metrics import metrics  # noqa: E402
from chat_metrics import plugin  # noqa: E402

_SERIES_LINE = re.compile(r'^kubeagents_chat_messages_inbound_total\{platform="([a-z]+)"\} (\d+)$', re.M)


def _event(platform="google_chat", text="hi"):
    return SimpleNamespace(source=SimpleNamespace(platform=SimpleNamespace(value=platform)), text=text)


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.registry = metrics.ChatMetrics()

    def series(self):
        return dict(_SERIES_LINE.findall(self.registry.render()))

    def test_an_inbound_message_counts_under_its_platform(self):
        self.registry.record_inbound("google_chat")
        self.registry.record_inbound("google_chat")
        self.registry.record_inbound("slack")
        self.assertEqual({"googlechat": "2", "slack": "1"}, self.series())

    def test_every_platform_the_status_names_has_a_spelling_and_the_rest_is_other(self):
        for hermes_name in ("google_chat", "slack", "teams", "api_server"):
            self.registry.record_inbound(hermes_name)
        self.registry.record_inbound("telegram")
        self.registry.record_inbound("")
        self.registry.record_inbound("sl\"ack\nx")
        got = self.series()
        self.assertEqual({"googlechat", "slack", "teams", "api", "other"}, set(got))
        self.assertEqual("3", got["other"])
        self.assertNotIn("telegram", self.registry.render())

    def test_the_exposition_names_the_family_and_the_start_time_once(self):
        text = self.registry.render()
        self.assertIn("# TYPE kubeagents_chat_messages_inbound_total counter", text)
        self.assertEqual(1, text.count("# TYPE process_start_time_seconds gauge"))
        self.assertRegex(text, r"(?m)^process_start_time_seconds \d+\.\d+$")
        self.assertEqual(text, self.registry.render(), "two renders of an idle registry differ")


class HookTest(unittest.TestCase):
    def setUp(self):
        self.registry = metrics.ChatMetrics()
        self.patched = mock.patch.object(plugin, "REGISTRY", self.registry)
        self.patched.start()
        self.addCleanup(self.patched.stop)

    def test_the_hook_counts_the_events_platform_and_rewrites_nothing(self):
        self.assertIsNone(plugin.count_inbound(_event("slack"), gateway=None, session_store=None))
        self.assertIsNone(plugin.count_inbound(_event("google_chat"), None, None))
        self.assertEqual({"slack": "1", "googlechat": "1"}, dict(_SERIES_LINE.findall(self.registry.render())))

    def test_an_event_without_a_source_counts_as_other_and_never_raises(self):
        self.assertIsNone(plugin.count_inbound(SimpleNamespace(text="x"), None, None))
        self.assertIsNone(plugin.count_inbound(None, None, None))
        self.assertEqual({"other": "2"}, dict(_SERIES_LINE.findall(self.registry.render())))

    def test_register_wires_the_dispatch_hook(self):
        ctx = mock.Mock()
        with mock.patch.object(plugin, "start_listener") as start:
            plugin.register(ctx)
        ctx.register_hook.assert_called_once_with("pre_gateway_dispatch", plugin.count_inbound)
        start.assert_called_once()

    def test_the_listener_starts_in_the_gateway_process_alone(self):
        """Every hermes process in the container discovers plugins; only the gateway,
        told apart by its command line, binds the port, so a CLI call beside it logs
        no bind failure. Hermes starts discovery on a background thread before the
        gateway's modules load, so the test is of argv, not of imports."""
        gateway_argv = ["/usr/local/bin/hermes", "--profile", "default", "gateway", "run"]
        with mock.patch.object(plugin, "_listener", None), mock.patch.object(metrics, "start_listener") as start, \
                mock.patch.dict(os.environ, {metrics.METRICS_PORT_ENV: "9097"}), mock.patch.object(sys, "argv", gateway_argv):
            plugin.start_listener()
            start.assert_called_once_with(9097, plugin.REGISTRY)
        for argv in (["/usr/local/bin/hermes", "-p", "default", "chat", "-q", "hi"],
                     ["/usr/local/bin/hermes", "gateway", "status"],
                     ["/usr/local/bin/hermes", "--profile", "gateway"],
                     ["python3", "leader_elect.py"]):
            with mock.patch.object(plugin, "_listener", None), mock.patch.object(metrics, "start_listener") as start, \
                    mock.patch.dict(os.environ, {metrics.METRICS_PORT_ENV: "9097"}), mock.patch.object(sys, "argv", argv):
                plugin.start_listener()
                start.assert_not_called()

    def test_a_sibling_process_logs_its_subcommand_and_not_its_arguments(self):
        """A `hermes send` carries the destination and the message text in argv;
        the log line names the subcommand alone."""
        argv = ["/usr/local/bin/hermes", "-p", "default", "send", "--json", "--to", "google_chat:spaces/XYZ", "the secret text"]
        with mock.patch.object(plugin, "_listener", None), mock.patch.object(metrics, "start_listener") as start, \
                mock.patch.dict(os.environ, {metrics.METRICS_PORT_ENV: "9097"}), mock.patch.object(sys, "argv", argv), \
                self.assertLogs("hermes.plugin.chat_metrics", level="INFO") as logs:
            plugin.start_listener()
        start.assert_not_called()
        self.assertEqual(1, len(logs.output))
        self.assertIn("(hermes send)", logs.output[0])
        self.assertNotIn("secret", logs.output[0])
        self.assertNotIn("spaces/XYZ", logs.output[0])
        self.assertEqual("gateway", plugin.subcommand_of(["hermes", "--profile=default", "gateway", "run"]))
        self.assertEqual("no subcommand", plugin.subcommand_of(["hermes", "--version"]))

    def test_the_command_line_reading_matches_hermes_own(self):
        """Without Hermes' classifier on the path the plugin reads argv the same way
        gateway/status.py does: `gateway run`, bare `gateway`, profile flags skipped."""
        cases = {
            ("hermes", "gateway", "run"): True,
            ("hermes", "gateway"): True,
            ("hermes", "--profile", "gateway", "gateway", "run"): True,
            ("hermes", "-p=default", "gateway", "run"): True,
            ("hermes", "gateway", "restart"): False,
            ("hermes", "gateway", "status"): False,
            ("hermes", "chat"): False,
            ("python3",): False,
        }
        with mock.patch.dict(sys.modules, {"gateway": None, "gateway.status": None}):
            for argv, want in cases.items():
                self.assertEqual(want, plugin.in_gateway_process(list(argv)), argv)


class ListenerTest(unittest.TestCase):
    def test_the_listener_serves_metrics_unauthenticated_and_nothing_else(self):
        registry = metrics.ChatMetrics()
        registry.record_inbound("slack")
        server = metrics.start_listener(0, registry)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        port = server.server_port
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as response:
            self.assertEqual(200, response.status)
            self.assertIn("text/plain", response.headers.get("Content-Type", ""))
            body = response.read().decode("utf-8")
        self.assertIn('kubeagents_chat_messages_inbound_total{platform="slack"} 1', body)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5)
        self.assertEqual(404, caught.exception.code)

    def test_the_port_comes_from_the_operators_variable_and_defaults_off(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(metrics.listener_port_from_env())
        with mock.patch.dict(os.environ, {"CHAT_METRICS_PORT": "9097"}):
            self.assertEqual(9097, metrics.listener_port_from_env())
        with mock.patch.dict(os.environ, {"CHAT_METRICS_PORT": "nonsense"}):
            self.assertIsNone(metrics.listener_port_from_env())

    def test_an_occupied_port_is_logged_not_raised(self):
        first = metrics.start_listener(0, metrics.ChatMetrics())
        self.addCleanup(first.shutdown)
        self.addCleanup(first.server_close)
        with self.assertLogs(metrics.logger, level="WARNING"):
            self.assertIsNone(metrics.start_listener(first.server_port, metrics.ChatMetrics()))

    def test_a_scrape_runs_off_the_gateways_thread(self):
        server = metrics.start_listener(0, metrics.ChatMetrics())
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.assertTrue(any(t.daemon and t.name.startswith(metrics.LISTENER_THREAD_NAME) for t in threading.enumerate()))


if __name__ == "__main__":
    unittest.main()
