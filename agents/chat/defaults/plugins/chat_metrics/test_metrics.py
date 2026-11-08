"""The chat_metrics plugin: inbound chat messages by platform, served on a metrics-only listener.

Run: cd agents/chat/defaults/plugins && python3 -m pytest chat_metrics/test_metrics.py -q
"""

import http.client
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chat_metrics import metrics  # noqa: E402
from chat_metrics import plugin  # noqa: E402

_SERIES_LINE = re.compile(r'^kubeagents_chat_messages_inbound_total\{platform="([a-z]+)"\} (\d+)$', re.M)
_SESSIONS_LINE = re.compile(r'^kubeagents_chat_sessions_total\{platform="([a-z]+)"\} (\d+)$', re.M)


_LEDGER_KEY_PATCH = mock.patch.dict(os.environ)


def setUpModule():
    # A registry built with no gauge injected reads the ledger over loopback when
    # the Session KV key is in the environment; the suite never wants that read.
    _LEDGER_KEY_PATCH.start()
    os.environ.pop(metrics.SESSION_KV_KEY_ENV, None)


def tearDownModule():
    _LEDGER_KEY_PATCH.stop()


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
        # No Session KV key in this suite: the ledger was never read, so the
        # family is absent rather than nine zeros.
        self.assertNotIn("kubeagents_proposals_opened", text)
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


_PROPOSALS_LINE = re.compile(r'^kubeagents_proposals_opened\{origin="([a-z]*)",platform="([a-z]*)"\} (\d+)$', re.M)
_SESSION_KV_KEY = "test-session-kv-key"


class _Ledger:
    """A fake Session KV server: answers GET /v1/proposals/opened/counts with
    `body` (a dict serialised as JSON, or bytes as is) and `status`, and
    records the Authorization header of each request."""

    def __init__(self):
        self.body = {}
        self.status = 200
        self.auth = []
        ledger = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                ledger.auth.append(self.headers.get("Authorization"))
                body = ledger.body if isinstance(ledger.body, bytes) else json.dumps(ledger.body).encode()
                self.send_response(ledger.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):  # noqa: A002
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/proposals/opened/counts"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _served(origins=None, **rows):
    """The server's answer: every origin, those not in `rows` as {"": 0}."""
    answer = {origin: {"": 0} for origin in (origins or metrics.ORIGINS)}
    answer.update(rows)
    return answer


class ProposalsOpenedTest(unittest.TestCase):
    """The proposals ledger, read over loopback from Session KV at scrape time
    and served as kubeagents_proposals_opened{origin,platform}: one series per
    origin at least, the last counts kept on a read error, no state of its own."""

    def setUp(self):
        self.ledger = _Ledger()
        self.addCleanup(self.ledger.close)
        env = mock.patch.dict(os.environ, {"SESSION_KV_API_KEY": _SESSION_KV_KEY})
        env.start()
        self.addCleanup(env.stop)
        self.gauge = metrics.ProposalsOpenedGauge(url=self.ledger.url)

    def series(self, gauge=None):
        registry = metrics.ChatMetrics(proposals=gauge or self.gauge)
        return {(o, p): int(n) for o, p, n in _PROPOSALS_LINE.findall(registry.render())}

    def test_every_origin_is_served_zero_filled(self):
        # A successful read of an empty ledger serves every origin at 0 under
        # platform="".
        self.ledger.body = {}
        self.assertEqual({(o, ""): 0 for o in metrics.ORIGINS}, self.series())
        self.assertEqual([f"Bearer {_SESSION_KV_KEY}"], self.ledger.auth)
        self.ledger.body = _served()
        self.assertEqual({(o, ""): 0 for o in metrics.ORIGINS}, self.series())
        # An answer that omits an origin still serves it, zero-filled.
        self.ledger.body = {"audit": {"": 2}}
        got = self.series()
        self.assertEqual(2, got[("audit", "")])
        self.assertEqual({o for o in metrics.ORIGINS}, {o for o, _ in got})
        self.assertEqual(0, got[("drift", "")])

    def test_no_family_is_served_before_the_first_successful_read(self):
        # The operator reads nine resolved origins as a reading; before a
        # successful read there is no count to serve, so the family is absent.
        self.ledger.body = _served(chat={"slack": 3})
        with self.assertLogs("hermes.plugin.chat_metrics", level="WARNING"):
            with mock.patch.dict(os.environ, {"SESSION_KV_API_KEY": ""}):
                self.assertIsNone(self.gauge.counts())
                self.assertNotIn("kubeagents_proposals_opened", metrics.ChatMetrics(proposals=self.gauge).render())
            self.assertEqual([], self.ledger.auth, "no request without the key")
            self.ledger.status = 503
            self.assertNotIn("kubeagents_proposals_opened", metrics.ChatMetrics(proposals=self.gauge).render())
            closed = metrics.ProposalsOpenedGauge(url="http://127.0.0.1:1/v1/proposals/opened/counts")
            self.assertNotIn("kubeagents_proposals_opened", metrics.ChatMetrics(proposals=closed).render())
        # The first good read serves the family, and a later failure keeps it.
        self.ledger.status = 200
        self.assertEqual(3, self.series()[("chat", "slack")])
        self.ledger.status = 503
        with self.assertLogs("hermes.plugin.chat_metrics", level="WARNING"):
            self.assertEqual(3, self.series()[("chat", "slack")])

    def test_chat_rows_carry_the_platform(self):
        self.ledger.body = _served(chat={"slack": 3, "googlechat": 1}, audit={"": 2})
        exposition = metrics.ChatMetrics(proposals=self.gauge).render()
        self.assertIn("# TYPE kubeagents_proposals_opened gauge", exposition)
        self.assertIn('kubeagents_proposals_opened{origin="chat",platform="slack"} 3\n', exposition)
        self.assertIn('kubeagents_proposals_opened{origin="chat",platform="googlechat"} 1\n', exposition)
        self.assertIn('kubeagents_proposals_opened{origin="audit",platform=""} 2\n', exposition)
        self.assertNotIn(("chat", ""), self.gauge.counts(), "a chat origin with rows has no empty-platform series")
        lines = [l for l in exposition.splitlines() if l.startswith("kubeagents_proposals_opened{")]
        self.assertEqual(sorted(lines), lines, "series render in sorted (origin, platform) order")
        self.assertEqual(len(metrics.ORIGINS) + 1, len(lines))
        # A later answer replaces the earlier one; nothing accumulates.
        self.ledger.body = _served(chat={"slack": 4})
        got = self.series()
        self.assertEqual(4, got[("chat", "slack")])
        self.assertNotIn(("chat", "googlechat"), got)
        self.assertEqual(0, got[("audit", "")])

    def test_a_read_error_keeps_the_last_count(self):
        self.ledger.body = _served(chat={"slack": 3}, issue={"": 1})
        last = self.gauge.counts()
        self.assertEqual(3, last[("chat", "slack")])
        with self.assertLogs("hermes.plugin.chat_metrics", level="WARNING") as logs:
            self.ledger.status = 500
            self.assertEqual(last, self.gauge.counts())
            self.ledger.status = 200
            for body in (b"not json", b"[]", json.dumps({"chat": {"slack": "3"}}).encode(), json.dumps({"chat": [1]}).encode()):
                self.ledger.body = body
                self.assertEqual(last, self.gauge.counts())
            with mock.patch.dict(os.environ, {"SESSION_KV_API_KEY": ""}):
                self.assertEqual(last, self.gauge.counts())
            closed = metrics.ProposalsOpenedGauge(url="http://127.0.0.1:1/v1/proposals/opened/counts")
            self.assertIsNone(closed.counts(), "a gauge that never read has no last count")
        # One line per failure streak per gauge, naming the exception type and
        # never the key.
        self.assertEqual(2, len(logs.output), logs.output)
        self.assertNotIn(_SESSION_KV_KEY, "\n".join(logs.output))
        # A good read ends the streak; the next failure logs again.
        self.ledger.body = _served(chat={"slack": 5})
        self.assertEqual(5, self.gauge.counts()[("chat", "slack")])
        with self.assertLogs("hermes.plugin.chat_metrics", level="WARNING") as again:
            self.ledger.status = 503
            self.assertEqual(5, self.gauge.counts()[("chat", "slack")])
        self.assertEqual(1, len(again.output))

    def test_a_truncated_or_malformed_http_answer_keeps_the_last_count(self):
        # Session KV restarting mid-answer, or something non-HTTP on its port:
        # the scrape keeps every family, and the streak logs once.
        self.ledger.body = _served(chat={"slack": 3})
        last = self.gauge.counts()
        failures = (http.client.IncompleteRead(b"{", 10), http.client.BadStatusLine("garbage"))
        with self.assertLogs("hermes.plugin.chat_metrics", level="WARNING") as logs:
            for failure in failures:
                with mock.patch.object(metrics.urllib.request, "urlopen", side_effect=failure):
                    self.assertEqual(last, self.gauge.counts())
                    exposition = metrics.ChatMetrics(proposals=self.gauge).render()
                self.assertIn('kubeagents_proposals_opened{origin="chat",platform="slack"} 3\n', exposition)
        ledger_lines = [line for line in logs.output if "proposals ledger" in line]
        self.assertEqual(1, len(ledger_lines), logs.output)
        self.assertIn("IncompleteRead", ledger_lines[0])

    def test_a_restart_of_the_process_serves_the_ledger_unchanged(self):
        # The ledger is durable on the pod's volume; the gauge holds nothing of
        # its own, so a fresh gauge against the same server answers the same.
        self.ledger.body = _served(chat={"slack": 3}, drift={"": 7})
        before = self.series()
        restarted = metrics.ProposalsOpenedGauge(url=self.ledger.url)
        self.assertEqual(before, self.series(restarted))
        # A restart while the ledger is unreachable serves no family, not zeros.
        self.ledger.status = 503
        with self.assertLogs("hermes.plugin.chat_metrics", level="WARNING"):
            self.assertEqual({}, self.series(metrics.ProposalsOpenedGauge(url=self.ledger.url)))
        self.assertEqual(7, before[("drift", "")])

    def test_the_origins_are_the_session_kv_servers(self):
        # session_kv_server imports FastAPI, which this plugin's tests do not
        # carry, so the pin reads its source text.
        server = Path(__file__).resolve().parents[4] / "platform" / "scripts" / "session_kv_server.py"
        match = re.search(r"^ORIGINS = \(([^)]*)\)$", server.read_text(), re.M)
        self.assertIsNotNone(match, "session_kv_server's ORIGINS moved; update this reader")
        self.assertEqual(tuple(re.findall(r'"([a-z]+)"', match.group(1))), metrics.ORIGINS)


class _FakeClock:
    """A monotonic clock a test advances by hand."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


_READ_AGE_LINE = re.compile(r"^kubeagents_proposals_opened_read_age_seconds ([0-9.]+)$", re.M)


class ProposalsReadAgeTest(unittest.TestCase):
    """kubeagents_proposals_opened_read_age_seconds: the seconds since the
    last successful ledger read, served beside the counts and only with them,
    so a scraper can tell the last counts kept on a failed read from fresh."""

    def setUp(self):
        self.ledger = _Ledger()
        self.addCleanup(self.ledger.close)
        env = mock.patch.dict(os.environ, {"SESSION_KV_API_KEY": _SESSION_KV_KEY})
        env.start()
        self.addCleanup(env.stop)
        self.clock = _FakeClock()
        self.gauge = metrics.ProposalsOpenedGauge(url=self.ledger.url, clock=self.clock)
        self.ledger.body = _served(chat={"slack": 3})

    def render(self):
        return metrics.ChatMetrics(proposals=self.gauge).render()

    def age(self, exposition):
        found = _READ_AGE_LINE.findall(exposition)
        return None if not found else [float(n) for n in found]

    def test_the_family_name_and_its_help_and_type_lines(self):
        self.assertEqual("kubeagents_proposals_opened_read_age_seconds", metrics.PROPOSALS_READ_AGE_METRIC)
        exposition = self.render()
        self.assertIn(f"# HELP kubeagents_proposals_opened_read_age_seconds {metrics.PROPOSALS_READ_AGE_HELP}\n", exposition)
        self.assertIn("# TYPE kubeagents_proposals_opened_read_age_seconds gauge\n", exposition)
        # One unlabelled sample, rendered right after the counts family.
        self.assertEqual([0.0], self.age(exposition))
        lines = exposition.splitlines()
        last_count = max(i for i, l in enumerate(lines) if l.startswith("kubeagents_proposals_opened{"))
        self.assertTrue(lines[last_count + 1].startswith("# HELP kubeagents_proposals_opened_read_age_seconds "))

    def test_no_age_is_served_before_the_first_successful_read(self):
        self.ledger.status = 503
        with self.assertLogs("hermes.plugin.chat_metrics", level="WARNING"):
            exposition = self.render()
            self.clock.now += 30
            exposition += self.render()
        self.assertNotIn("kubeagents_proposals_opened_read_age_seconds", exposition)
        self.assertNotIn("kubeagents_proposals_opened{", exposition, "the counts are absent with the age")

    def test_the_age_renders_exactly_when_the_counts_do(self):
        # Before the first read neither renders; after it both do, through failed reads too.
        for step in ("before", "read", "failed", "failed again", "read again"):
            self.ledger.status = 200 if step.startswith("read") else 503
            with self.subTest(step=step):
                exposition = self.render()
                has_counts = "kubeagents_proposals_opened{" in exposition
                self.assertEqual(bool(self.age(exposition)), has_counts)
                self.assertEqual(has_counts, step != "before")

    def test_the_default_clock_is_the_monotonic_one(self):
        gauge = metrics.ProposalsOpenedGauge(url=self.gauge._url)
        self.assertIs(gauge._clock, metrics.time.monotonic, "the default clock is monotonic, so a wall-clock step cannot move the age")
        exposition = metrics.ChatMetrics(proposals=gauge).render()
        self.assertEqual([0.0], self.age(exposition), "a fresh successful read on the real clock reads zero")

    def test_the_age_is_zero_right_after_a_successful_read(self):
        self.assertEqual([0.0], self.age(self.render()))
        self.clock.now += 45
        self.assertEqual([0.0], self.age(self.render()), "each successful read restarts the age")

    def test_the_age_grows_across_failed_reads_while_the_last_counts_render(self):
        self.render()
        self.ledger.status = 503
        with self.assertLogs("hermes.plugin.chat_metrics", level="WARNING") as logs:
            self.clock.now += 15
            exposition = self.render()
            self.assertEqual([15.0], self.age(exposition))
            self.assertIn('kubeagents_proposals_opened{origin="chat",platform="slack"} 3\n', exposition)
            for elapsed in (75, 600, 3600.5):
                self.clock.now = 1000.0 + elapsed
                exposition = self.render()
                self.assertEqual([elapsed], self.age(exposition))
                self.assertIn('kubeagents_proposals_opened{origin="chat",platform="slack"} 3\n', exposition)
        ledger_lines = [line for line in logs.output if "proposals ledger" in line]
        self.assertEqual(1, len(ledger_lines), "the streak still logs once")

    def test_the_age_resets_on_the_next_successful_read(self):
        self.render()
        self.ledger.status = 503
        with self.assertLogs("hermes.plugin.chat_metrics", level="WARNING"):
            self.clock.now += 120
            self.assertEqual([120.0], self.age(self.render()))
        self.ledger.status = 200
        self.ledger.body = _served(chat={"slack": 4})
        self.clock.now += 5
        exposition = self.render()
        self.assertEqual([0.0], self.age(exposition))
        self.assertIn('kubeagents_proposals_opened{origin="chat",platform="slack"} 4\n', exposition)


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
