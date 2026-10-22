"""Inbound chat messages, new sessions and failed turns by platform, in the Prometheus text exposition.

Hand-rolled, like the credential broker's registry: prometheus_client is not in
the agent image, and three counters with one label do not justify a dependency
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
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

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


def platform_label(hermes_platform: str) -> str:
    """The label value for a Hermes platform name: one of the values of HERMES_PLATFORM_LABELS, or LABEL_OTHER."""
    return HERMES_PLATFORM_LABELS.get(str(hermes_platform or "").strip().lower(), LABEL_OTHER)


class ChatMetrics:
    """Three counters, by platform: inbound messages, new sessions and errored turns."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inbound: dict[str, int] = {}
        self._sessions: dict[str, int] = {}
        self._errored: dict[str, int] = {}

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

    def render(self) -> str:
        with self._lock:
            inbound = sorted(self._inbound.items())
            sessions = sorted(self._sessions.items())
            errored = sorted(self._errored.items())
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
    except OSError as exc:
        logger.warning("chat metrics listener could not bind port %d: %s", port, exc)
        return None
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name=f"{LISTENER_THREAD_NAME}-{server.server_port}", daemon=True)
    thread.start()
    logger.info("chat metrics listener serving %s on port %d", METRICS_PATH, server.server_port)
    return server
