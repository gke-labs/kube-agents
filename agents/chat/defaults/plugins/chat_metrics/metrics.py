"""Inbound chat messages, new sessions and failed turns by platform, the board's
cards that gave up, and the proposals ledger by origin, read from Session KV,
in the Prometheus text exposition.

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

import http.client
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.request
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
# Pull requests the agent opened, by the origin of the work behind each and,
# for chat, its platform: Session KV's proposals_opened ledger, which the
# pr_opened hook writes one row per verified and claimed proposal, counted at
# scrape time. The ledger is on the pod's volume, so a restart of this process
# serves the same counts; the family is a gauge because a leadership change
# swaps one pod's ledger for another's. The read is Session KV's loopback
# counts endpoint, not the SQLite file, whose volume this process may not see.
# ORIGINS is session_kv_server.ORIGINS in its order (a test pins the two).
PROPOSALS_OPENED_METRIC = "kubeagents_proposals_opened"
ORIGIN_LABEL = "origin"
ORIGINS = ("chat", "console", "api", "event", "drift", "audit", "scheduled", "issue", "other")
PROPOSALS_COUNTS_URL = "http://127.0.0.1:8699/v1/proposals/opened/counts"
SESSION_KV_KEY_ENV = "SESSION_KV_API_KEY"
PROPOSALS_READ_TIMEOUT_SECONDS = 2
# Beside the counts, rendered only with them: seconds since the last
# successful ledger read, on a monotonic clock. 0 right after a good read; it
# grows while failed reads keep serving the last counts, so a scraper can tell
# stale counts from fresh (as kubeagents_vcs_readings_age_seconds does for the
# broker's readings). The family is unlabelled; the consumer sets the limit.
PROPOSALS_READ_AGE_METRIC = "kubeagents_proposals_opened_read_age_seconds"
PROPOSALS_READ_AGE_HELP = "Seconds since the proposals ledger was last read successfully; present with kubeagents_proposals_opened, and growing while a failed read keeps its last counts."
PROPOSALS_READ_AGE_DECIMALS = 3
# The platform label of an origin the ledger has no row for, and of a row with
# no platform.
NO_PLATFORM = ""
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
# The series the operator reads the KanbanDispatcherFailing condition from
# (usage_counters_scrape.go's kanbanDispatcherFailedTicksSeries): the kanban
# dispatcher's consecutive failed ticks on each board, set by the gateway patch
# kanban_dispatch_failures at the dispatcher's "tick failed on board" path, with
# the class of the last failure. A good tick drops the board's series, so only
# failing boards have one.
KANBAN_DISPATCHER_FAILED_TICKS_METRIC = "kubeagents_kanban_dispatcher_failed_ticks"
BOARD_LABEL = "board"
ERROR_LABEL = "error"
# Beside it, rendered for the same boards: how long each board's streak has
# lasted, in whole seconds on the registry's monotonic clock, from its first
# failure to the scrape. The operator sets KanbanDispatcherFailing by the
# streak's age as well as its ticks (usage_counters_scrape.go's
# kanbanDispatcherFailingSecondsSeries). On a fully locked board failures come
# 20 to 36 minutes apart, so this takes about an hour there; the stuck family
# below shows such a board sooner.
KANBAN_DISPATCHER_FAILING_SECONDS_METRIC = "kubeagents_kanban_dispatcher_failing_seconds"
# Both windows below count from the last time the dispatcher tried the board: now
# while it is trying it, else the later of its last failure and the end of its
# last try. The patch records the start and end of every per-board connect in the
# dispatcher's loop (the tick, the ready probe, auto-decompose). On a locked board
# each of those can take many minutes, because connect() waits the SQLite busy
# timeout (120 s, kanban_db_connect's DEFAULT_BUSY_TIMEOUT_MS) once per statement
# it runs; a board being tried neither goes stale nor stops ageing.
#
# A board with no tick in progress whose last failure is older than this is
# dropped at render: the dispatcher stopped ticking it (archived, quarantined as
# corrupt, or the dispatcher paused), and no good tick will come to end its
# streak. The ready probe and auto-decompose still reach a quarantined board;
# they do not count as tries while it is quarantined (record_dispatch_tick_end).
DISPATCH_FAILURE_STALE_SECONDS = 600.0
# The streak's age stops growing this long after the board was last tried, so a
# board the dispatcher stopped trying cannot age into the condition on a few
# early failures before the stale window drops it.
DISPATCH_FAILURE_AGE_GRACE_SECONDS = 250.0
# A third family for the same condition: a board whose tick has run longer than
# STUCK_TICK_SECONDS, which says the board is locked well before its failures,
# 20 to 36 minutes apart on a fully locked board, build a streak. Only the tick
# counts: auto-decompose can run a model call per board, so a long one can be
# healthy. A board stays stuck through its later tries and ticks until a good
# tick, until a tick fails for another reason without such a wait, or until the
# dispatcher stops trying it (DISPATCH_FAILURE_STALE_SECONDS), which a corrupt
# quarantine counts as; the value is the seconds since the stuck tick began. The operator sets
# KanbanDispatcherFailing for a stuck board (usage_counters_scrape.go's
# kanbanDispatcherStuckSecondsSeries).
KANBAN_DISPATCHER_STUCK_SECONDS_METRIC = "kubeagents_kanban_dispatcher_stuck_seconds"
# Two and a half SQLite busy timeouts (120 s each). The timeout applies per
# statement, so a working tick on a busy board can wait just under it more than
# once and still succeed; two such waits stay under 240 s, so a good tick needs
# three long waits to pass this. A locked tick took about 12 minutes on a board
# in WAL mode in the live runs, and this rule catches it while it runs; in
# rollback-journal mode it failed after about 242 s (two waits), which the lock
# rule below catches. A good tick that does run past the limit leaves the board
# stuck only until it ends, so the condition can read True for one poll.
STUCK_TICK_SECONDS = 300.0
# A tick that fails on a lock error after waiting at least one SQLite busy
# timeout (120 s) marks the board stuck at once: a locked board's tick fails
# only after waiting a full timeout (about 242 s per tick, two timeouts, on a
# board in rollback-journal mode in the live runs), a good tick never fails, and
# a fast failure, or a slow one of another kind, is left to the streak rules.
# The floor is Hermes' busy timeout for the board: DEFAULT_BUSY_TIMEOUT_MS
# (hermes_cli/kanban_db_connect.py), 120 s, unless HERMES_KANBAN_BUSY_TIMEOUT_MS
# changes it. The dispatcher runs in this process, so the registry reads the
# same variable when it is made (stuck_lock_wait_seconds).
STUCK_LOCK_WAIT_SECONDS = 120.0
KANBAN_BUSY_TIMEOUT_ENV = "HERMES_KANBAN_BUSY_TIMEOUT_MS"
KANBAN_BUSY_TIMEOUT_MINIMUM_MS = 1
MILLISECONDS_PER_SECOND = 1000.0
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


class ProposalsReadError(ValueError):
    """The counts endpoint could not be asked, or its answer is not `{origin: {platform: n}}`."""


def _zero_filled(counts: dict[tuple[str, str], int]) -> dict[tuple[str, str], int]:
    """counts with a (origin, NO_PLATFORM) 0 for every origin that has no series."""
    filled = dict(counts)
    present = {origin for origin, _ in counts}
    for origin in ORIGINS:
        if origin not in present:
            filled[(origin, NO_PLATFORM)] = 0
    return filled


class ProposalsOpenedGauge:
    """The proposals ledger by (origin, platform), read from Session KV at scrape time.

    Each successful read replaces the previous counts, zero-filled so every
    origin has a series. A read that fails (no API key, a connection error, a
    non-200, a truncated or malformed HTTP answer, an answer of the wrong
    shape) keeps the last counts and logs the
    exception type once per failure streak. Before the first successful read
    there are no counts: :meth:`counts` returns None and the family is not
    rendered, so a scraper sees a miss rather than nine zero readings. The
    gauge holds nothing the ledger does not: a new process reads the same.
    :meth:`reading` also returns the seconds since the last successful read,
    on ``clock`` (monotonic by default), so kept counts are marked stale.
    """

    def __init__(self, url: str = PROPOSALS_COUNTS_URL, clock: Callable[[], float] = time.monotonic) -> None:
        self._url = url
        self._clock = clock
        self._lock = threading.Lock()
        self._counts: Optional[dict[tuple[str, str], int]] = None
        self._read_at = 0.0
        self._failing = False

    def _read(self) -> dict[tuple[str, str], int]:
        key = os.environ.get(SESSION_KV_KEY_ENV, "").strip()
        if not key:
            raise ProposalsReadError(f"{SESSION_KV_KEY_ENV} is not set")
        request = urllib.request.Request(self._url, headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(request, timeout=PROPOSALS_READ_TIMEOUT_SECONDS) as response:  # nosec B310 -- a fixed loopback http URL
            if response.status != HTTPStatus.OK:
                raise ProposalsReadError(f"status {response.status}")
            answer = json.loads(response.read())
        if not isinstance(answer, dict):
            raise ProposalsReadError("the answer is not an object")
        counts: dict[tuple[str, str], int] = {}
        for origin, platforms in answer.items():
            if not isinstance(platforms, dict):
                raise ProposalsReadError("an origin's counts are not an object")
            for platform, n in platforms.items():
                if not isinstance(n, int) or isinstance(n, bool) or n < 0:
                    raise ProposalsReadError("a count is not a non-negative integer")
                counts[(origin, platform)] = n
        return _zero_filled(counts)

    def counts(self) -> Optional[dict[tuple[str, str], int]]:
        """The ledger's counts, the last ones on a failed read, or None before any read succeeded."""
        reading = self.reading()
        return None if reading is None else reading[0]

    def reading(self) -> Optional[tuple[dict[tuple[str, str], int], float]]:
        """The counts as :meth:`counts` returns them and the seconds since the last successful read, or None before one."""
        with self._lock:
            try:
                counts = self._read()
            # URLError and timeouts are OSError; JSON errors and ProposalsReadError are ValueError;
            # IncompleteRead, BadStatusLine and LineTooLong are HTTPException.
            except (OSError, ValueError, http.client.HTTPException) as exc:
                if not self._failing:
                    logger.warning("chat_metrics: the proposals ledger could not be read, keeping the last counts: %s", type(exc).__name__)
                    self._failing = True
                if self._counts is None:
                    return None
                return dict(self._counts), max(self._clock() - self._read_at, 0.0)
            self._failing = False
            self._counts = counts
            self._read_at = self._clock()
            return dict(counts), 0.0


def _label_value(value: str) -> str:
    """value escaped for a Prometheus label: backslash, double quote and newline."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def platform_label(hermes_platform: str) -> str:
    """The label value for a Hermes platform name: one of the values of HERMES_PLATFORM_LABELS, or LABEL_OTHER."""
    return HERMES_PLATFORM_LABELS.get(str(hermes_platform or "").strip().lower(), LABEL_OTHER)


def stuck_lock_wait_seconds() -> float:
    """The lock rule's floor: Hermes' busy timeout for the board, in seconds.

    Read as Hermes reads it (kanban_db._env_int): an integer number of
    milliseconds; absent, empty, not an integer or below the minimum is the
    default."""
    raw = os.environ.get(KANBAN_BUSY_TIMEOUT_ENV, "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            return STUCK_LOCK_WAIT_SECONDS
        if parsed >= KANBAN_BUSY_TIMEOUT_MINIMUM_MS:
            return parsed / MILLISECONDS_PER_SECOND
    return STUCK_LOCK_WAIT_SECONDS


class ChatMetrics:
    """Four counters, by platform: inbound messages, new sessions, errored turns
    and thumbs-downs; the board's gave_up cards and the proposals ledger, read
    at scrape time; and the dispatcher's failed-tick streak, by board."""

    def __init__(
        self,
        kanban: Optional[KanbanGaveUpCounter] = None,
        proposals: Optional[ProposalsOpenedGauge] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._kanban = kanban or KanbanGaveUpCounter()
        self._proposals = proposals or ProposalsOpenedGauge()
        self._clock = clock
        self._lock_wait_seconds = stuck_lock_wait_seconds()
        self._lock = threading.Lock()
        self._inbound: dict[str, int] = {}
        self._sessions: dict[str, int] = {}
        self._errored: dict[str, int] = {}
        self._negative: dict[str, int] = {}
        # board -> (failed ticks in a row, last error class, last failure, first failure)
        self._dispatch_failures: dict[str, tuple[int, str, float, float]] = {}
        # board -> when its in-progress try began
        self._dispatch_ticking: dict[str, float] = {}
        # failing board -> when its last try ended
        self._dispatch_tried: dict[str, float] = {}
        # board -> when its in-progress tick began (ticks only, not the other tries)
        self._dispatch_tick_started: dict[str, float] = {}
        # stuck board -> [when the stuck tick began, when its last try ended]
        self._dispatch_stuck: dict[str, list[float]] = {}
        # boards in the corrupt-board quarantine that still have a streak or a
        # stuck state: their ready-probe and auto-decompose tries do not count
        self._dispatch_in_quarantine: set[str] = set()

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

    def record_dispatch_tick(self, board: str, error_class: str, locked: bool = False) -> None:
        """One kanban dispatcher tick on ``board``: a failure (``error_class``
        set) adds one to the board's streak; a good tick (``""``) ends it.
        ``locked`` says the failure was a database lock or busy error."""
        with self._lock:
            # A tick with a result, good or failed, ends a quarantine.
            self._dispatch_in_quarantine.discard(board)
            if not error_class:
                self._dispatch_failures.pop(board, None)
                self._dispatch_tried.pop(board, None)
                # A good tick ends a stuck board too, however long it ran.
                self._dispatch_stuck.pop(board, None)
                self._dispatch_tick_started.pop(board, None)
                return
            now = self._clock()
            # A failed tick that ran past the limit leaves the board stuck,
            # also when no scrape saw it running, and so does a lock failure
            # after a full busy timeout. A failure of another kind that did
            # neither ends the stuck state: the board no longer waits on its
            # database, and its streak carries the new cause.
            tick_started = self._dispatch_tick_started.pop(board, None)
            if tick_started is not None:
                ran = now - tick_started
                if ran > STUCK_TICK_SECONDS or (locked and ran >= self._lock_wait_seconds):
                    self._dispatch_stuck.setdefault(board, [tick_started, now])
                elif not locked:
                    self._dispatch_stuck.pop(board, None)
            streak, _, at, began = self._dispatch_failures.get(board, (0, "", 0.0, now))
            # The gap that counts is from the last try before this one to this
            # try's start; a locked board's tries follow each other closely even
            # when its failures are far apart.
            started = self._dispatch_ticking.get(board, now)
            if started - max(at, self._dispatch_tried.get(board, at)) > DISPATCH_FAILURE_STALE_SECONDS:
                # A streak past the window ended unseen: render had not run to drop it.
                streak, began = 0, now
            self._dispatch_failures[board] = (streak + 1, error_class, now, began)

    def record_dispatch_tick_start(self, board: str, tick: bool = False) -> None:
        """A try on ``board`` began: a tick (``tick``), a ready probe or an
        auto-decompose connect."""
        with self._lock:
            now = self._clock()
            stuck = self._dispatch_stuck.get(board)
            if stuck is not None and board not in self._dispatch_ticking and now - stuck[1] > DISPATCH_FAILURE_STALE_SECONDS:
                del self._dispatch_stuck[board]
            if board in self._dispatch_failures and board not in self._dispatch_ticking:
                # Drop a streak past the window first, as render() would have:
                # this try must not carry it on.
                _, _, at, _ = self._dispatch_failures[board]
                if now - max(at, self._dispatch_tried.get(board, at)) > DISPATCH_FAILURE_STALE_SECONDS:
                    del self._dispatch_failures[board]
                    self._dispatch_tried.pop(board, None)
            if board not in self._dispatch_failures and board not in self._dispatch_stuck:
                self._dispatch_in_quarantine.discard(board)
            if board in self._dispatch_in_quarantine and not tick:
                # A quarantined board's ready probe or auto-decompose: not a
                # try, so it does not keep the streak or the stuck state.
                return
            if tick:
                self._dispatch_tick_started[board] = now
            self._dispatch_ticking[board] = now

    def record_dispatch_tick_end(self, board: str) -> None:
        """The try on ``board`` ended, whichever way."""
        with self._lock:
            self._dispatch_ticking.pop(board, None)
            # A tick still marked here recorded no result (record_dispatch_tick
            # takes the mark): the corrupt-board branch, which quarantines the
            # board for five minutes at a time. Hermes takes that branch also
            # when a race makes a board look corrupt for one tick, so the
            # streak and the stuck state stay. But while the board is
            # quarantined, this tick and the other tries, which still reach the
            # board, do not refresh them: a quarantine that lasts drops the
            # board after the stale window, and a short one keeps it whole. A
            # stuck state that this very tick set (a scrape saw it past the
            # limit) goes at once: the tick was not a wait on a lock.
            tick_started = self._dispatch_tick_started.pop(board, None)
            if tick_started is not None:
                stuck = self._dispatch_stuck.get(board)
                if stuck is not None and stuck[0] == tick_started:
                    del self._dispatch_stuck[board]
                if board in self._dispatch_failures or board in self._dispatch_stuck:
                    self._dispatch_in_quarantine.add(board)
                return
            if board in self._dispatch_in_quarantine:
                return
            if board in self._dispatch_failures:
                self._dispatch_tried[board] = self._clock()
            if board in self._dispatch_stuck:
                self._dispatch_stuck[board][1] = self._clock()

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
            now = self._clock()
            # When each failing board was last tried: now while a try is in progress.
            live = {b: now if b in self._dispatch_ticking else max(at, self._dispatch_tried.get(b, at))
                    for b, (_, _, at, _) in self._dispatch_failures.items()}
            for board in [b for b, t in live.items() if now - t > DISPATCH_FAILURE_STALE_SECONDS]:
                del self._dispatch_failures[board]
                self._dispatch_tried.pop(board, None)
            dispatch_failures = sorted(self._dispatch_failures.items())
            for board, started in self._dispatch_tick_started.items():
                if now - started > STUCK_TICK_SECONDS:
                    self._dispatch_stuck.setdefault(board, [started, now])
            for board in [b for b, (_, tried) in self._dispatch_stuck.items()
                          if b not in self._dispatch_ticking and now - tried > DISPATCH_FAILURE_STALE_SECONDS]:
                del self._dispatch_stuck[board]
            dispatch_stuck = sorted((b, since) for b, (since, _) in self._dispatch_stuck.items())
            self._dispatch_in_quarantine.intersection_update(set(self._dispatch_failures) | set(self._dispatch_stuck))
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
        reading = self._proposals.reading()
        if reading is not None:
            proposals, read_age = reading
            lines += [
                f"# HELP {PROPOSALS_OPENED_METRIC} Pull requests the agent opened, by the origin of the work and, for chat, its platform, read from the pod's proposals ledger at scrape time.",
                f"# TYPE {PROPOSALS_OPENED_METRIC} gauge",
            ]
            for (origin, platform), count in sorted(proposals.items()):
                lines.append(f'{PROPOSALS_OPENED_METRIC}{{{ORIGIN_LABEL}="{_label_value(origin)}",{PLATFORM_LABEL}="{_label_value(platform)}"}} {count}')
            lines += [
                f"# HELP {PROPOSALS_READ_AGE_METRIC} {PROPOSALS_READ_AGE_HELP}",
                f"# TYPE {PROPOSALS_READ_AGE_METRIC} gauge",
                f"{PROPOSALS_READ_AGE_METRIC} {read_age:.{PROPOSALS_READ_AGE_DECIMALS}f}",
            ]
        lines += [
            f"# HELP {CHAT_FEEDBACK_NEGATIVE_METRIC} Thumbs-down reactions a person put on one of the bot's messages, by platform, once per person and message.",
            f"# TYPE {CHAT_FEEDBACK_NEGATIVE_METRIC} counter",
        ]
        for label, count in negative:
            lines.append(f'{CHAT_FEEDBACK_NEGATIVE_METRIC}{{{PLATFORM_LABEL}="{label}"}} {count}')
        lines += [
            f"# HELP {KANBAN_DISPATCHER_FAILED_TICKS_METRIC} Consecutive kanban dispatcher ticks that failed on a board, with the class of the last failure; a good tick removes the board's series.",
            f"# TYPE {KANBAN_DISPATCHER_FAILED_TICKS_METRIC} gauge",
        ]
        for board, (streak, error_class, _, _) in dispatch_failures:
            lines.append(f'{KANBAN_DISPATCHER_FAILED_TICKS_METRIC}{{{BOARD_LABEL}="{_label_value(board)}",{ERROR_LABEL}="{_label_value(error_class)}"}} {streak}')
        lines += [
            f"# HELP {KANBAN_DISPATCHER_FAILING_SECONDS_METRIC} Seconds since the first failure of each board's current kanban dispatcher streak, for the boards in {KANBAN_DISPATCHER_FAILED_TICKS_METRIC}.",
            f"# TYPE {KANBAN_DISPATCHER_FAILING_SECONDS_METRIC} gauge",
        ]
        for board, (_, error_class, at, began) in dispatch_failures:
            age = min(now, live[board] + DISPATCH_FAILURE_AGE_GRACE_SECONDS) - began
            lines.append(f'{KANBAN_DISPATCHER_FAILING_SECONDS_METRIC}{{{BOARD_LABEL}="{_label_value(board)}",{ERROR_LABEL}="{_label_value(error_class)}"}} {int(max(0.0, age))}')
        lines += [
            f"# HELP {KANBAN_DISPATCHER_STUCK_SECONDS_METRIC} Seconds since a kanban dispatcher tick began that ran longer than {int(STUCK_TICK_SECONDS)} s, or failed on a database lock after at least {self._lock_wait_seconds:g} s, on a board, until a good tick, a quick failure of another kind, or ten minutes with no try that counts.",
            f"# TYPE {KANBAN_DISPATCHER_STUCK_SECONDS_METRIC} gauge",
        ]
        for board, since in dispatch_stuck:
            lines.append(f'{KANBAN_DISPATCHER_STUCK_SECONDS_METRIC}{{{BOARD_LABEL}="{_label_value(board)}"}} {int(max(0.0, now - since))}')
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
