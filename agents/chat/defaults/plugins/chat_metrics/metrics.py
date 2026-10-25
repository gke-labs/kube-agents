"""Inbound chat messages, new sessions and failed turns by platform, and the board's
cards that gave up, in the Prometheus text exposition.

Hand-rolled, like the credential broker's registry: prometheus_client is not in
the agent image, and four small counters do not justify a dependency
in the container that holds the chat credentials. One lock, because the
gateway's event loop writes while a scrape thread reads. Series appear on first
increment and render in a fixed order, so two scrapes of an idle registry are
byte-identical. REGISTRY is the one instance: plugin.py serves it, and the
session_store plugin counts sessions on it from its own hook.

The listener serves `/metrics` and nothing else, unauthenticated: its readers,
the managed-Prometheus collector and the operator's usage poller, hold no
caller token, and the operator's NetworkPolicy on the gateway pod is what bounds
who reaches the port. The port comes from `CHAT_METRICS_PORT`, which the
operator sets beside the container port it declares; unset or unreadable means
no listener, and a line in the log says so.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import quote

logger = logging.getLogger("hermes.plugin.chat_metrics")

# The series the operator reads status.usage.chatMessagesInboundTotal from
# (usage_counters_scrape.go's chatInboundSeries) and its one label. The label's
# vocabulary is HERMES_PLATFORM_LABELS' values plus LABEL_OTHER: Hermes'
# spellings of the platforms, as the gateway event's source names them, mapped
# to the spellings status.usage.activeInterfaces uses, with api for the
# gateway's own API port (Hermes' api_server). The dashboard is not a platform
# Hermes names on a gateway event. A name outside the map counts as other.
CHAT_INBOUND_METRIC = "kubeagents_chat_messages_inbound_total"
# The series the operator reads the chat half of status.usage.sessionsTotal
# from (usage_counters_scrape.go's chatSessionsSeries): one per session the
# session_store plugin first saw, under the same platform label.
CHAT_SESSIONS_METRIC = "kubeagents_chat_sessions_total"
# The series the operator reads status.usage.chatTurnsErroredTotal from
# (usage_counters_scrape.go's chatTurnsErroredSeries): synchronous turns the
# gateway failed to complete, by platform, counted by the gateway patch
# chat_turn_metrics at the turn's exception path, its failed-result path (a
# provider 429 or 5xx, a connection error, the gateway timeout) and the
# provider-authentication failure.
CHAT_TURNS_ERRORED_METRIC = "kubeagents_chat_turns_errored_total"
# The series the operator reads status.usage.backgroundTasksGaveUpTotal from
# (usage_counters_scrape.go's kanbanGaveUpSeries): background cards whose
# breaker tripped since the listener started, read off Hermes' board at scrape
# time. Hermes parks such a card in status blocked and appends a gave_up
# event to task_events (gave_up is an event kind and a run outcome, never a
# task status), so the read is the distinct task_ids with a gave_up event. No
# labels. The operator treats a new process as starting from zero and adds its
# whole sample, so the cards already given up when the listener starts are a
# baseline, not a count; a card counts once however many times it gives up,
# and stays counted after the workspace GC deletes it.
KANBAN_GAVE_UP_METRIC = "kubeagents_kanban_tasks_gave_up_total"
KANBAN_GAVE_UP_EVENT_KIND = "gave_up"
# Hermes' board. gitops_workspace.board_path() is the resolution the stall
# watch and the GitOps skills use (Hermes' own kanban_db_path() in the gateway
# venv, else the agent home's kanban.db); it is importable from the gateway
# process because the operator puts the scripts directory on PYTHONPATH. The
# fallbacks below are for a process without it: the agent home the operator
# names, then Hermes' home, then the data volume.
BOARD_DB_NAME = "kanban.db"
AGENT_HOME_ENV = "PLATFORM_AGENT_HOME"
HERMES_HOME_ENV = "HERMES_HOME"
DEFAULT_HERMES_HOME = "/opt/data"
# A scrape waits this long on a locked board before keeping the last value.
BOARD_BUSY_TIMEOUT_SECONDS = 1.0
# The series the operator reads status.usage.negativeFeedbackTotal from
# (usage_counters_scrape.go's chatFeedbackNegativeSeries): thumbs-down
# reactions a person put on one of the bot's messages, by platform, counted by
# the chat_feedback_reactions hook once per person and message. The hook tells
# the bot's messages from a person's by the Slack identities this module reads
# off the gateway the inbound hook noted (note_gateway, slack_bot_user_ids).
CHAT_FEEDBACK_NEGATIVE_METRIC = "kubeagents_chat_feedback_negative_total"
SLACK_PLATFORM_VALUE = "slack"
PLATFORM_LABEL = "platform"
LABEL_OTHER = "other"
HERMES_PLATFORM_LABELS = {
    "google_chat": "googlechat",
    "googlechat": "googlechat",
    "slack": "slack",
    "teams": "teams",
    "api_server": "api",
}
PROCESS_START_TIME_METRIC = "process_start_time_seconds"
PROCESS_START_TIME_SECONDS = time.time()
METRICS_PATH = "/metrics"
METRICS_PORT_ENV = "CHAT_METRICS_PORT"
METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
METRICS_SERVER_HEADER = "kube-agents-chat-metrics"
LISTENER_THREAD_NAME = "chat-metrics-listener"
LISTENER_BIND_ADDRESS = "0.0.0.0"  # nosec B104 -- a metrics-only listener the pod's NetworkPolicy bounds
# Per-connection read deadline: a scrape is one GET, and a peer that holds the
# socket open must not hold a handler thread with it.
CONNECTION_TIMEOUT_SECONDS = 5
MAX_PORT = 65535


def board_path() -> Path:
    """The board the gateway's agent files on."""
    try:
        import gitops_workspace  # lazy: on PYTHONPATH in the agent container, not in a test's

        return Path(str(gitops_workspace.board_path()))
    except Exception:  # noqa: BLE001 - outside the agent container the fallbacks below are the resolution
        pass
    try:
        from hermes_cli.kanban_db import kanban_db_path  # lazy: the gateway venv's

        return Path(str(kanban_db_path()))
    except Exception:  # noqa: BLE001 - outside the gateway venv the default board is the only one
        pass
    home = os.environ.get(AGENT_HOME_ENV, "").strip() or os.environ.get(HERMES_HOME_ENV, "").strip() or DEFAULT_HERMES_HOME
    return Path(home) / BOARD_DB_NAME


class KanbanGaveUpCounter:
    """Cards with a gave_up event since the first successful read of the board.

    The first read is the baseline and counts nothing (start_listener makes
    it, so the baseline is the listener's start when the board is readable
    then); every later read adds the task_ids it has not seen to a set whose
    size is the count, so a deleted card stays counted and a card that gives
    up twice counts once. A read that fails keeps the last value and logs
    once per failure streak; a baseline is never taken from a failed read.
    """

    def __init__(self, board: Callable[[], Path] = board_path) -> None:
        self._board = board
        self._lock = threading.Lock()
        self._baseline: Optional[set] = None
        self._seen: set = set()
        self._failing = False

    def _read_gave_up(self) -> set:
        # SQLite reads the path as a URI, so `?`, `#` and `%` in it are encoded;
        # `/` is left alone and SQLite decodes the rest back.
        path = quote(str(self._board()))
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=BOARD_BUSY_TIMEOUT_SECONDS)
        try:
            rows = conn.execute("SELECT DISTINCT task_id FROM task_events WHERE kind = ?", (KANBAN_GAVE_UP_EVENT_KIND,)).fetchall()
        finally:
            conn.close()
        return {str(row[0]) for row in rows}

    def count(self) -> int:
        with self._lock:
            try:
                gave_up = self._read_gave_up()
            except (sqlite3.Error, OSError) as exc:
                if not self._failing:
                    logger.warning("chat_metrics: the kanban board could not be read, keeping the last count: %s", type(exc).__name__)
                    self._failing = True
                return len(self._seen)
            self._failing = False
            if self._baseline is None:
                self._baseline = gave_up
                return 0
            self._seen |= gave_up - self._baseline
            return len(self._seen)


def platform_label(hermes_platform: str) -> str:
    """The label value for a Hermes platform name: one of the values of HERMES_PLATFORM_LABELS, or LABEL_OTHER."""
    return HERMES_PLATFORM_LABELS.get(str(hermes_platform or "").strip().lower(), LABEL_OTHER)


class ChatMetrics:
    """Four counters, by platform: inbound messages, new sessions, errored turns
    and thumbs-downs; and the board's gave_up cards, read at scrape time."""

    def __init__(self, kanban: Optional[KanbanGaveUpCounter] = None) -> None:
        self._kanban = kanban or KanbanGaveUpCounter()
        self._lock = threading.Lock()
        self._inbound: dict[str, int] = {}
        self._sessions: dict[str, int] = {}
        self._errored: dict[str, int] = {}
        self._negative: dict[str, int] = {}

    def prime(self) -> None:
        """Take the board's baseline now rather than at the first scrape."""
        self._kanban.count()

    def record_inbound(self, hermes_platform: str) -> None:
        label = platform_label(hermes_platform)
        with self._lock:
            self._inbound[label] = self._inbound.get(label, 0) + 1

    def record_session(self, hermes_platform: str) -> None:
        """Count one session the gateway opened, under its platform."""
        label = platform_label(hermes_platform)
        with self._lock:
            self._sessions[label] = self._sessions.get(label, 0) + 1

    def record_turn_error(self, hermes_platform: str) -> None:
        """Count one synchronous turn the gateway failed to complete, under its platform."""
        label = platform_label(hermes_platform)
        with self._lock:
            self._errored[label] = self._errored.get(label, 0) + 1

    def record_negative_feedback(self, hermes_platform: str) -> None:
        label = platform_label(hermes_platform)
        with self._lock:
            self._negative[label] = self._negative.get(label, 0) + 1

    def render(self) -> str:
        with self._lock:
            inbound = sorted(self._inbound.items())
            sessions = sorted(self._sessions.items())
            errored = sorted(self._errored.items())
            negative = sorted(self._negative.items())
        lines = [
            f"# HELP {CHAT_INBOUND_METRIC} Chat messages that reached the gateway's dispatch hook, by platform.",
            f"# TYPE {CHAT_INBOUND_METRIC} counter",
        ]
        for label, count in inbound:
            lines.append(f'{CHAT_INBOUND_METRIC}{{{PLATFORM_LABEL}="{label}"}} {count}')
        lines += [
            f"# HELP {CHAT_SESSIONS_METRIC} Sessions the gateway opened for a chat conversation, by platform, counted on the first message of each.",
            f"# TYPE {CHAT_SESSIONS_METRIC} counter",
        ]
        for label, count in sessions:
            lines.append(f'{CHAT_SESSIONS_METRIC}{{{PLATFORM_LABEL}="{label}"}} {count}')
        lines += [
            f"# HELP {CHAT_TURNS_ERRORED_METRIC} Synchronous chat turns the gateway failed to complete, by platform: an exception out of the turn, a result the agent marked failed (a provider 429 or 5xx, a connection error, the gateway timeout), or a provider authentication failure.",
            f"# TYPE {CHAT_TURNS_ERRORED_METRIC} counter",
        ]
        for label, count in errored:
            lines.append(f'{CHAT_TURNS_ERRORED_METRIC}{{{PLATFORM_LABEL}="{label}"}} {count}')
        lines += [
            f"# HELP {KANBAN_GAVE_UP_METRIC} Background cards on the agent's board that reached gave_up since this process started, read off the board at scrape time.",
            f"# TYPE {KANBAN_GAVE_UP_METRIC} counter",
            f"{KANBAN_GAVE_UP_METRIC} {self._kanban.count()}",
        ]
        lines += [
            f"# HELP {CHAT_FEEDBACK_NEGATIVE_METRIC} Thumbs-down reactions a person put on one of the bot's messages, by platform, once per person and message.",
            f"# TYPE {CHAT_FEEDBACK_NEGATIVE_METRIC} counter",
        ]
        for label, count in negative:
            lines.append(f'{CHAT_FEEDBACK_NEGATIVE_METRIC}{{{PLATFORM_LABEL}="{label}"}} {count}')
        lines += [
            f"# HELP {PROCESS_START_TIME_METRIC} Start time of the process since unix epoch in seconds, captured once at start.",
            f"# TYPE {PROCESS_START_TIME_METRIC} gauge",
            f"{PROCESS_START_TIME_METRIC} {PROCESS_START_TIME_SECONDS!r}",
        ]
        return "\n".join(lines) + "\n"


# The one registry of the gateway process. The listener serves it, the hook
# in plugin.py counts messages on it, and session_store counts sessions on it.
REGISTRY = ChatMetrics()


class _MetricsHandler(BaseHTTPRequestHandler):
    server_version = METRICS_SERVER_HEADER
    sys_version = ""
    timeout = CONNECTION_TIMEOUT_SECONDS
    registry: ChatMetrics  # set per server by start_listener

    def do_GET(self) -> None:  # noqa: N802 -- the http.server contract
        if self.path.split("?", 1)[0] != METRICS_PATH:
            self.send_response(HTTPStatus.NOT_FOUND)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = self.registry.render().encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", METRICS_CONTENT_TYPE)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 -- the http.server contract
        # A scrape every 30 seconds is not an access-log line; the gateway's
        # log is the agent's.
        return


def listener_port_from_env() -> Optional[int]:
    """The port the operator set, or None for no listener."""
    raw = os.environ.get(METRICS_PORT_ENV, "").strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError:
        logger.warning("%s=%r is not a port; the chat metrics listener is off", METRICS_PORT_ENV, raw)
        return None
    if not 0 < port <= MAX_PORT:
        logger.warning("%s=%r is outside 1-%d; the chat metrics listener is off", METRICS_PORT_ENV, raw, MAX_PORT)
        return None
    return port


def start_listener(port: int, registry: ChatMetrics) -> Optional[ThreadingHTTPServer]:
    """Serve registry on port from a daemon thread; None, logged, when the port cannot be bound."""
    handler = type("ChatMetricsHandler", (_MetricsHandler,), {"registry": registry})
    try:
        server = ThreadingHTTPServer((LISTENER_BIND_ADDRESS, port), handler)
        registry.prime()
    except OSError as exc:
        logger.warning("chat metrics listener could not bind port %d: %s", port, exc)
        return None
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name=f"{LISTENER_THREAD_NAME}-{server.server_port}", daemon=True)
    thread.start()
    logger.info("chat metrics listener serving %s on port %d", METRICS_PATH, server.server_port)
    return server


# The gateway the inbound hook last saw, kept so the reaction hook, which
# Hermes hands only an event, can ask which Slack identities are the bot's.
_gateway: Any = None


def note_gateway(gateway: Any) -> None:
    """Remember the gateway object the pre_gateway_dispatch hook was handed."""
    global _gateway
    _gateway = gateway


def slack_bot_user_ids() -> frozenset:
    """The bot's Slack user ids on every team, from the noted gateway's Slack
    adapter; empty when no gateway has been noted or it has no Slack adapter."""
    adapters = getattr(_gateway, "adapters", None)
    if not isinstance(adapters, dict):
        return frozenset()
    ids: set = set()
    for platform, adapter in adapters.items():
        if str(getattr(platform, "value", platform)).lower() != SLACK_PLATFORM_VALUE:
            continue
        bot = getattr(adapter, "_bot_user_id", None)
        if bot:
            ids.add(str(bot))
        teams = getattr(adapter, "_team_bot_user_ids", None)
        if isinstance(teams, dict):
            ids.update(str(v) for v in teams.values() if v)
    return frozenset(ids)
