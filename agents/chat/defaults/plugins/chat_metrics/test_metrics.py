"""The chat_metrics plugin: inbound chat messages by platform, served on a metrics-only listener.

Run: cd agents/chat/defaults/plugins && python3 -m pytest chat_metrics/test_metrics.py -q
"""

import os
import re
import sqlite3
import sys
import tempfile
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
_SESSIONS_LINE = re.compile(r'^kubeagents_chat_sessions_total\{platform="([a-z]+)"\} (\d+)$', re.M)


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

    def test_a_new_session_counts_under_its_platform(self):
        self.registry.record_session("google_chat")
        self.registry.record_session("slack")
        self.registry.record_session("slack")
        self.registry.record_session("telegram")
        self.assertEqual({"googlechat": "1", "slack": "2", "other": "1"}, dict(_SESSIONS_LINE.findall(self.registry.render())))
        self.assertEqual({}, self.series(), "a session is not a message")

    def test_the_module_holds_the_one_registry_the_listener_serves(self):
        # session_store counts sessions on it from its own hook, so it must
        # be the object plugin.py serves rather than one of its own.
        self.assertIs(metrics.REGISTRY, plugin.REGISTRY)
        self.assertIsInstance(metrics.REGISTRY, metrics.ChatMetrics)

    def test_the_exposition_names_the_family_and_the_start_time_once(self):
        text = self.registry.render()
        self.assertIn("# TYPE kubeagents_chat_messages_inbound_total counter", text)
        self.assertIn("# TYPE kubeagents_chat_sessions_total counter", text)
        self.assertEqual(1, text.count("# TYPE process_start_time_seconds gauge"))
        self.assertRegex(text, r"(?m)^process_start_time_seconds \d+\.\d+$")
        self.assertEqual(text, self.registry.render(), "two renders of an idle registry differ")


_ERRORED_LINE = re.compile(r'^kubeagents_chat_turns_errored_total\{platform="([a-z]+)"\} (\d+)$', re.M)
_NEGATIVE_LINE = re.compile(r'^kubeagents_chat_feedback_negative_total\{platform="([a-z]+)"\} (\d+)$', re.M)


class TurnErrorTest(unittest.TestCase):
    """A synchronous turn the gateway failed to complete counts under its
    platform, with the same spellings the inbound counter uses."""

    def test_a_failed_turn_counts_under_its_platform(self):
        registry = metrics.ChatMetrics()
        registry.record_turn_error("slack")
        registry.record_turn_error("google_chat")
        registry.record_turn_error("google_chat")
        registry.record_turn_error("made_up")
        exposition = registry.render()
        self.assertIn("# TYPE kubeagents_chat_turns_errored_total counter", exposition)
        self.assertEqual([("googlechat", "2"), ("other", "1"), ("slack", "1")], _ERRORED_LINE.findall(exposition))

    def test_an_idle_registry_renders_the_family_with_no_series(self):
        exposition = metrics.ChatMetrics().render()
        self.assertIn("# TYPE kubeagents_chat_turns_errored_total counter", exposition)
        self.assertEqual([], _ERRORED_LINE.findall(exposition))


_GAVE_UP_LINE = re.compile(r"^kubeagents_kanban_tasks_gave_up_total (\d+)$", re.M)


class KanbanGaveUpTest(unittest.TestCase):
    """Cards whose breaker tripped since the listener started, read off the
    board at scrape time. Hermes parks such a card in status blocked and
    appends a gave_up event to task_events, so the read is the distinct
    task_ids with a gave_up event: a baseline at first read, a seen set after,
    so a card counts once however many times it gives up, stays counted after
    the workspace GC deletes it, and a card that gave up before this process
    started is not this process's."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "kanban.db"
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL)")
            conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, kind TEXT NOT NULL)")
        self.counter = metrics.KanbanGaveUpCounter(board=lambda: self.db)

    def card(self, card, status, *events):
        """A card at `status` with `events` appended, the way the dispatcher writes them."""
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO tasks (id, status) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET status = excluded.status", (card, status))
            for kind in events:
                conn.execute("INSERT INTO task_events (task_id, kind) VALUES (?, ?)", (card, kind))

    def give_up(self, card):
        self.card(card, "blocked", "gave_up")

    def delete(self, card):
        with sqlite3.connect(self.db) as conn:
            conn.execute("DELETE FROM task_events WHERE task_id = ?", (card,))
            conn.execute("DELETE FROM tasks WHERE id = ?", (card,))

    def test_cards_that_gave_up_before_the_first_read_are_the_baseline(self):
        self.give_up("old")
        self.assertEqual(0, self.counter.count())
        self.assertEqual(0, self.counter.count())

    def test_a_card_whose_breaker_trips_after_the_first_read_counts_once(self):
        self.counter.count()
        self.card("t1", "running", "created", "claimed")
        self.assertEqual(0, self.counter.count())
        self.give_up("t1")
        self.assertEqual(1, self.counter.count())
        # Unblocked and given up again: the same card, still one.
        self.card("t1", "ready", "unblocked")
        self.give_up("t1")
        self.assertEqual(1, self.counter.count())

    def test_a_blocked_card_without_a_gave_up_event_does_not_count(self):
        self.counter.count()
        self.card("t1", "blocked", "blocked")
        self.assertEqual(0, self.counter.count())

    def test_a_counted_card_stays_counted_after_the_gc_deletes_it(self):
        self.counter.count()
        self.give_up("t1")
        self.assertEqual(1, self.counter.count())
        self.delete("t1")
        self.assertEqual(1, self.counter.count())

    def test_a_done_card_never_counts(self):
        self.counter.count()
        self.card("t2", "done", "created", "completed")
        self.assertEqual(0, self.counter.count())

    def test_an_unreadable_board_keeps_the_last_value(self):
        self.counter.count()
        self.give_up("t1")
        self.assertEqual(1, self.counter.count())
        self.db.unlink()
        self.assertEqual(1, self.counter.count())

    def test_a_board_unreadable_at_the_first_read_takes_its_baseline_later(self):
        board = [Path(self.temp.name) / "absent.db"]
        counter = metrics.KanbanGaveUpCounter(board=lambda: board[0])
        self.assertEqual(0, counter.count())
        # The baseline is not taken from a failed read: when the board appears
        # with a card already given up, that card is the baseline, not a count.
        board[0] = self.db
        self.give_up("old")
        self.assertEqual(0, counter.count())
        self.give_up("t1")
        self.assertEqual(1, counter.count())

    def test_a_board_under_a_home_with_uri_characters_is_still_read(self):
        # SQLite parses the path as a URI: an unencoded `?`, `#` or `%` would
        # open a different file read-write and the count would stay at 0.
        home = Path(self.temp.name) / "da?ta#1%2F"
        home.mkdir()
        db = home / "kanban.db"
        with sqlite3.connect(self.db) as src, sqlite3.connect(db) as dst:
            src.backup(dst)
        counter = metrics.KanbanGaveUpCounter(board=lambda: db)
        self.assertEqual(0, counter.count())
        with sqlite3.connect(db) as conn:
            conn.execute("INSERT INTO task_events (task_id, kind) VALUES (?, ?)", ("t1", metrics.KANBAN_GAVE_UP_EVENT_KIND))
        self.assertEqual(1, counter.count())
        self.assertFalse((Path(self.temp.name) / "da").exists(), "an unencoded ? opened a file named by the prefix")

    def test_the_exposition_carries_the_family_as_a_counter(self):
        registry = metrics.ChatMetrics(kanban=self.counter)
        self.give_up("old")
        exposition = registry.render()
        self.assertIn("# TYPE kubeagents_kanban_tasks_gave_up_total counter", exposition)
        self.assertEqual(["0"], _GAVE_UP_LINE.findall(exposition))
        self.give_up("t1")
        self.assertEqual(["1"], _GAVE_UP_LINE.findall(registry.render()))

    def test_the_listener_takes_the_baseline_when_it_starts(self):
        # A card that gives up between the gateway starting and the first
        # scrape is the baseline, not a count: start_listener reads once.
        registry = metrics.ChatMetrics(kanban=self.counter)
        self.give_up("old")
        with mock.patch.object(metrics, "ThreadingHTTPServer") as server:
            server.return_value.server_address = ("0.0.0.0", 0)
            metrics.start_listener(0, registry)
        self.give_up("t1")
        self.assertEqual(["1"], _GAVE_UP_LINE.findall(registry.render()))

    def test_the_board_path_is_the_agent_homes_board_outside_the_gateway_venv(self):
        with mock.patch.dict(os.environ, {"PLATFORM_AGENT_HOME": self.temp.name}, clear=False):
            self.assertEqual(self.db, metrics.board_path())


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

class _Platform:
    def __init__(self, value):
        self.value = value


class _Adapter:
    def __init__(self, bot, teams=None):
        self._bot_user_id = bot
        self._team_bot_user_ids = dict(teams or {})


class _Gateway:
    def __init__(self, adapters):
        self.adapters = adapters


class NegativeFeedbackTest(unittest.TestCase):
    """A thumbs-down a person put on the bot's message counts under its
    platform; the bot's own Slack identities come from the gateway the inbound
    hook noted, so the reaction hook can tell the bot's messages from a person's."""

    def tearDown(self):
        metrics.note_gateway(None)

    def test_a_negative_reaction_counts_under_its_platform(self):
        registry = metrics.ChatMetrics()
        registry.record_negative_feedback("slack")
        registry.record_negative_feedback("slack")
        registry.record_negative_feedback("google_chat")
        exposition = registry.render()
        self.assertIn("# TYPE kubeagents_chat_feedback_negative_total counter", exposition)
        self.assertEqual([("googlechat", "1"), ("slack", "2")], _NEGATIVE_LINE.findall(exposition))

    def test_an_idle_registry_renders_the_family_with_no_series(self):
        exposition = metrics.ChatMetrics().render()
        self.assertIn("# TYPE kubeagents_chat_feedback_negative_total counter", exposition)
        self.assertEqual([], _NEGATIVE_LINE.findall(exposition))

    def test_the_slack_bot_ids_come_from_the_noted_gateway(self):
        gateway = _Gateway({_Platform("slack"): _Adapter("U1", {"T1": "U2"}), _Platform("discord"): _Adapter("U9")})
        metrics.note_gateway(gateway)
        self.assertEqual(frozenset({"U1", "U2"}), metrics.slack_bot_user_ids())

    def test_without_a_gateway_the_bot_ids_are_empty(self):
        metrics.note_gateway(None)
        self.assertEqual(frozenset(), metrics.slack_bot_user_ids())
        metrics.note_gateway(object())
        self.assertEqual(frozenset(), metrics.slack_bot_user_ids())


if __name__ == "__main__":
    unittest.main()
