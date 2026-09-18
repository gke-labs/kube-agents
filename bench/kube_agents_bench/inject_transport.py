# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The inject transport behind ``AGENT_TRANSPORT=inject``.

The harness hands a case's prompt to the agent the way a customer hands it a
chat message under ``spec.mode: next``: one ``POST /inject`` on the A2A
gateway's inject side door, which enters ``handleInbound`` like a message from
any other backend -- routing, the session record, ``startTask``, the bus, and
the relay posting the reply back into the conversation. What the harness
grades is what the conversation received, which is the point: if a customer
would not see it, a verifier should not depend on it.

This module is the door half: the three HTTP calls (submit; the read that
polls the transcript and, with ``probe=1``, the gateway's own record; and the
cancel), the fold of a conversation's transcript, the classification of how a
task ended, and one submit-and-await exchange. Environment, port-forwards,
retry classes and the mapping onto ``AgentResult`` stay in
:mod:`kube_agents_bench.harness`, which is the only importer.

The wait is written as "await the terminal of task id X" rather than "await
the task I submitted" (the A2A owner's instruction on #1661): when
agent-initiated delegation becomes a child task on the bus, the parent's
events will name the child's id and the same call awaits that instead. The
kanban poll that stands in for child tasks today is NOT here -- it lives in
the case runner, so it can be deleted without touching this module.

The gateway's inject door is dev and eval only. Every request carries the
bearer token the operator mints into a Secret beside the door, which the
harness reads the way the presubmit reads the agent's own API key; see
``a2a/gateway/inject.go`` and the test-backend section of
``docs/designs/spec-chatops-gateway.md``.
"""

from __future__ import annotations

import http.client
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "CANCEL_SUFFIX",
    "DEFAULT_AUTHOR",
    "ENTRY_EDIT",
    "ENTRY_POST",
    "ENTRY_TASK",
    "ENTRY_TERMINAL",
    "EVENT_ENTRY_EDIT",
    "EVENT_ENTRY_POST",
    "EVENT_ENTRY_STATUS",
    "EVENT_ENTRY_TASK",
    "GRACE_MARGIN_SECONDS",
    "INFRASTRUCTURE_REASONS",
    "OUTCOME_DEADLINE",
    "OUTCOME_NEVER_STARTED",
    "OUTCOME_NOT_ACCEPTED",
    "OUTCOME_QUEUED",
    "OUTCOME_STREAM_TERMINAL",
    "OUTCOME_TERMINAL",
    "OUTCOME_UNCLASSIFIED",
    "PERSONA_REASONS",
    "PROBE_PARAM",
    "REASON_CANCELED_BEFORE_START",
    "REASON_PREFIX",
    "REFUSAL_NO_ANSWER",
    "REFUSAL_NO_TASK",
    "REFUSAL_PUBLISH_FAILED",
    "REFUSAL_UNVERIFIED_AUTHOR",
    "RETRYABLE_STATUSES",
    "STATE_COMPLETED",
    "STATE_SUBMITTED",
    "STATE_WORKING",
    "TERMINAL_SOURCE_EXECUTOR",
    "TERMINAL_SOURCE_GATEWAY",
    "TERMINAL_SOURCE_NEVER_STARTED",
    "TERMINAL_SOURCE_SUPERVISOR",
    "TERMINAL_STATES",
    "BudgetBelowFloor",
    "Exchange",
    "Fold",
    "InjectTask",
    "InjectUnavailable",
    "Probe",
    "budget_floor",
    "check_budget",
    "conversation_key",
    "message_id",
    "parse_reason",
    "refusal_detail",
]

_log = logging.getLogger("kube_agents_bench.inject_transport")

# The gateway's three endpoints (``a2a/gateway/inject.go``).
INJECT_PATH = "/inject"
CONVERSATIONS_PATH = "/conversations/"
CANCEL_SUFFIX = "/cancel"

# The bearer credential the door requires on every request. There is no
# unauthenticated mode: the gateway refuses to arm the door without a token,
# because the NetworkPolicy in front of it does not govern the port-forward
# this transport arrives through.
AUTHORIZATION_HEADER = "Authorization"
BEARER_PREFIX = "Bearer "

# Where the token comes from, named in the refusal a wrong one produces. The
# operator renders it into a Secret beside the door, and the harness reads it
# the way the presubmit reads the agent's own API key out of
# platform-agent-secrets.
DEFAULT_TOKEN_SECRET_HINT = "<agent>-a2a-inject Secret's `token` key"

# The author the operator's rendered principal map admits (``a2aInjectAuthor``
# in the operator's A2A manifests). An author with no entry in the map is
# dropped at verification, so this is not cosmetic.
DEFAULT_AUTHOR = "devops-bench"

# How a run, a case and a repetition are joined into the conversation key and
# the backend message id: ``<run>/<case>/<rep>``. The run id is minted fresh
# per invocation and never pinned from the environment (the api path's
# ``AGENT_CONVERSATION_ID`` is not honoured here), so the key is fresh per
# case and repetition -- the gateway keeps a session record per conversation,
# and a shared key would have each task inherit the previous one's context or
# land as a steer on it -- and the message id is unique per invocation, which
# is what makes it a safe dedupe key at the door: a retry of the same POST
# carries the same id and is answered with the same task, while a pinned id
# would have the door answer a rerun with a previous invocation's task and
# its terminal. The gateway prefixes the key with ``inject:``.
KEY_SEPARATOR = "/"

# Appended to the backend message id of a cancel, so the ingress log shows
# which submission the cancel belongs to and still tells the two apart.
CANCEL_MESSAGE_ID_SUFFIX = "-cancel"


# Transcript entry kinds, as the gateway spells them.
ENTRY_POST = "post"
ENTRY_EDIT = "edit"
ENTRY_TASK = "task"
ENTRY_TERMINAL = "terminal"

# Trajectory entry names this transport records. The inject door carries no
# tool calls, and structurally cannot: the relay never posts `activity`
# artifacts to a conversation (they are debug and audit views), so no
# transport that reads a conversation sees them however the executor behaves.
# What this one can truthfully record is the conversation itself -- what the
# agent said -- and the task's lifecycle: every executor state the read route
# showed (submitted, working) and the terminal, each as an entry named
# ``a2a.status-update`` with ``args.state`` and ``args.final``, the same name
# and shape the bus-reading a2a transport gives the task's status-update
# events. ``scoring.py`` reads a final one as the record's liveness signal in
# place of a token count and duplicates the literal (importing this module
# would drag the transport into the scorer); ``test_scoring.py`` asserts the
# two agree, so change it in both files or in neither.
EVENT_ENTRY_POST = "inject.post"
EVENT_ENTRY_EDIT = "inject.edit"
EVENT_ENTRY_TASK = "inject.task"
EVENT_ENTRY_STATUS = "a2a.status-update"
# The ``status`` field of every trajectory entry this transport writes.
ENTRY_STATUS_DONE = "completed"

STATE_SUBMITTED = "submitted"
STATE_WORKING = "working"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_CANCELED = "canceled"
STATE_REJECTED = "rejected"
TERMINAL_STATES = frozenset({STATE_COMPLETED, STATE_FAILED, STATE_CANCELED, STATE_REJECTED})

# Who declared a terminal (``TerminalSource`` in a2a/gateway/adapter.go).
# Only the first is the agent's own outcome. A gateway-declared one is a task
# the gateway could not put on the bus; a supervisor one is the gateway's word
# about an executor that died or never ran, which the read route's fold reads
# off the subject the terminal arrived on; a never-started one is the
# gateway's never-started heal, a task that reached the bus and that no
# executor ever touched. None of the three is an answer, and grading any of
# them scores an install fault against the agent.
TERMINAL_SOURCE_EXECUTOR = "executor"
TERMINAL_SOURCE_GATEWAY = "gateway"
TERMINAL_SOURCE_SUPERVISOR = "supervisor"
TERMINAL_SOURCE_NEVER_STARTED = "gateway-never-started"

# How an executor says why a task ended: the terminal status event's message,
# written ``reason: <token>[ - detail]`` by the bridge (a2a/hermes-bridge) and
# the worker adapter (a2a/worker-adapter). The gateway passes it through
# verbatim on the terminal entry (``reason``); this side strips the prefix
# and takes the token up to the next space. No prefix means no token, and an
# unknown token is graded like any other failure -- the persona's.
REASON_PREFIX = "reason: "

# The executors' own reasons, named against their definitions. A failed
# terminal carrying one of these is not the persona's failure: the bridge was
# shut down or overflowed, the bus refused a publish, the subprocess could not
# be spawned, the worker was evicted. They classify as infrastructure, the
# same class as an exhausted transport retry, and are never graded.
REASON_BRIDGE_SHUTDOWN = "bridge-shutdown"
REASON_BRIDGE_QUEUE_OVERFLOW = "bridge-queue-overflow"
REASON_BUS_PUBLISH_FAILED = "bus-publish-failed"
REASON_SPAWN_FAILED = "spawn-failed"
REASON_BRIDGE_DIED = "bridge-died-without-terminal-event"
REASON_WORKER_EVICTED = "worker-evicted"
REASON_BUS_SUBSCRIBE_FAILED = "bus-subscribe-failed"
INFRASTRUCTURE_REASONS = frozenset(
    {
        REASON_BRIDGE_SHUTDOWN,
        REASON_BRIDGE_QUEUE_OVERFLOW,
        REASON_BUS_PUBLISH_FAILED,
        REASON_SPAWN_FAILED,
        REASON_BRIDGE_DIED,
        REASON_WORKER_EVICTED,
        REASON_BUS_SUBSCRIBE_FAILED,
    }
)
# The persona's reasons, graded: the hermes turn exited non-zero, or ran past
# the bridge's own deadline. Listed for the record and the tests; an unknown
# token lands in the same class, so nothing here is consulted to grade.
REASON_HERMES_EXITED_NONZERO = "hermes-exited-nonzero"
REASON_DEADLINE_EXCEEDED = "deadline-exceeded"
PERSONA_REASONS = frozenset({REASON_HERMES_EXITED_NONZERO, REASON_DEADLINE_EXCEEDED})
# The two canceled terminals. After this transport's own cancel the executor
# answers canceled-by-request, which is the graded timeout; a task the bridge
# cancelled out of its queue before ever spawning answers
# canceled-before-start, which is infrastructure -- nothing ran.
REASON_CANCELED_BEFORE_START = "canceled-before-start"
REASON_CANCELED_BY_REQUEST = "canceled-by-request"

# Why the door answered a ``POST`` with no task id (``refusal`` on the reply,
# ``a2a/gateway/inject.go``). Branched on rather than matched in the note,
# which is prose the gateway is free to rewrite. All four are infrastructure:
# in none of them did an agent see the prompt.
REFUSAL_UNVERIFIED_AUTHOR = "unverified-author"
REFUSAL_PUBLISH_FAILED = "publish-failed"
REFUSAL_NO_TASK = "no-task"
REFUSAL_NO_ANSWER = "no-answer"
# What each one means, in the words the run's record carries. The publish
# failure is its own class rather than part of the refusal above because it
# says something different about the install: the door and the principal map
# are fine and the bus is not.
_REFUSAL_DETAIL = {
    REFUSAL_UNVERIFIED_AUTHOR: (
        "the gateway refused the injection: its principal map does not carry the author"
    ),
    REFUSAL_PUBLISH_FAILED: (
        "the gateway failed to publish the submission: it minted the task and could not put it "
        "on the bus, so no executor can ever see the prompt"
    ),
    REFUSAL_NO_TASK: (
        "the gateway answered the turn without starting a task (a steer, a status answer, or a stop)"
    ),
    REFUSAL_NO_ANSWER: "the gateway did not say what it did with the prompt inside its own bound",
}
_REFUSAL_UNKNOWN = "the gateway started no task for the prompt"


def refusal_detail(refusal: str) -> str:
    """The infrastructure reason behind a refusal code, or the generic one.

    An unknown code is a gateway newer than this harness; it reads as the
    generic reason rather than as an error of its own, because the fact that
    matters -- nothing ran -- is the same either way.
    """
    return _REFUSAL_DETAIL.get(refusal, _REFUSAL_UNKNOWN)


# Outcomes of one exchange.
#
# ``OUTCOME_TERMINAL``: the task ended and the fold says how; the harness
# then classifies the terminal (source, state, reason). ``OUTCOME_NOT_ACCEPTED``:
# the gateway answered the turn without starting a task. ``OUTCOME_NEVER_STARTED``:
# the read route showed the task active with nothing on its stream past the
# gateway's first-event grace -- no executor ever touched it; infrastructure.
# ``OUTCOME_QUEUED``: at the deadline the stream had never shown more than
# ``submitted`` -- the bridge queued the task behind its concurrency cap for
# the whole budget and never ran it; infrastructure, and the task is
# cancelled. ``OUTCOME_DEADLINE``: at the deadline the executor was working;
# a graded timeout, and the task is cancelled. ``OUTCOME_UNCLASSIFIED``: the
# read at the deadline could not say -- the gateway could not look, or the
# record no longer held the task and no terminal was ever seen; infrastructure,
# nothing graded. ``OUTCOME_STREAM_TERMINAL``: the record still held the task
# but its stream had already folded to a terminal and the relay never posted
# it -- the relay acks a terminal before it clears the record, so a restart
# or a failed record write leaves exactly this, and on a key never reused no
# heal arrives. The terminal, its source, its reason and the result text are
# taken from the read's fold; graded like a finished run when the source is
# the executor's, infrastructure when it is the gateway's (the supervisor
# ended an executor that died or never ran); never cancelled.
#
# Every outcome that leaves an active task -- never-started, queued, deadline,
# and an unclassifiable read -- is followed by a cancel naming the task the
# POST was answered with. The classification never comes from the cancel's
# answer; the cancel bounds a stray run (see :meth:`InjectTask.cancel`).
OUTCOME_TERMINAL = "terminal"
OUTCOME_NOT_ACCEPTED = "not-accepted"
OUTCOME_DEADLINE = "deadline"
OUTCOME_QUEUED = "queued"
OUTCOME_NEVER_STARTED = "never-started"
OUTCOME_UNCLASSIFIED = "unclassified"
OUTCOME_STREAM_TERMINAL = "stream-terminal"

# The read route: ``GET /conversations/<key>?probe=1`` makes the gateway
# report its session record for the conversation and the state of the active
# task's stream -- a PURE READ, with nothing healed, routed or written
# (``ConversationProbe`` in a2a/gateway/adapter.go). Every poll this transport
# makes asks for it, because it is where the task's lifecycle is read from,
# and the classification is made here rather than there: the read reports
# active, the executor's state, the task's age and the gateway's grace, and
# this side decides. It has to be a read rather than a message, because every
# message a program could send to the gateway is itself a turn.
PROBE_PARAM = "probe"

# How far above the gateway's own first-event grace this transport's budget
# has to sit, in seconds. "Nobody took this task" is a window the gateway
# owns (``A2A_FIRST_EVENT_GRACE``): inside it an active task with nothing on
# its stream is legitimately pre-first-event, and a budget that expired there
# would cancel a task no executor has seen and have nothing to classify by.
# The floor is grace plus this margin, and a budget below it is refused
# before anything is started (:func:`check_budget`). Two minutes is well
# above any submit latency and small beside the grace's ten-minute default.
GRACE_MARGIN_SECONDS = 120.0

# How long to wait, after cancelling a task, for the executor's ``canceled``
# terminal. A bound on the executor confirming, not on any work; a task that
# does not confirm is graded on what it produced.
CANCEL_SETTLE_SECONDS = 60.0

# How long to wait, after a read has shown the task terminal on the stream,
# for the relay to post the terminal it is about to deliver. The relay's
# batch is normally a moment behind the stream; a relay that missed the event
# never delivers, and after this the stream's word is adopted as the
# executor's.
FINISHED_SETTLE_SECONDS = 30.0

# How long one ``GET /conversations`` asks the gateway to block for something
# new. Long enough that a quiet task costs one request a minute rather than
# one a second, and well under the gateway's own ceiling on the parameter.
POLL_WAIT_SECONDS = 30

# Ceilings on one HTTP round trip. The POST waits for the gateway to say what
# it did with the message, bounded by its turn timeout; the GET waits
# POLL_WAIT_SECONDS. Both get a margin over their server-side bound, so a
# client timeout means the connection died rather than that the server is
# still thinking.
SUBMIT_TIMEOUT_SECONDS = 120.0
# The margin a poll's client timeout gets over the wait it asked the server
# for. Without it, a GET near a deadline asks for `budget` seconds and times
# out after `budget` seconds, so the fractional second decides -- and a lost
# race reads as a dead tunnel, tearing down a healthy port-forward.
POLL_TIMEOUT_MARGIN_SECONDS = 30.0
POLL_TIMEOUT_SECONDS = POLL_WAIT_SECONDS + POLL_TIMEOUT_MARGIN_SECONDS

# How much of the conversation's last post a log line or an infrastructure
# message quotes, so a stalled task's last words are on the record without
# a whole report riding in an error string.
LAST_POST_EXCERPT_CHARS = 160

# Pause before re-issuing a poll the gateway answered with nothing. The GET
# blocks server-side already, so this only paces the pathological case of an
# endpoint answering empty at once.
POLL_PAUSE_SECONDS = 1.0

# HTTP statuses a request is re-sent on, with the same body. These clear on
# their own: the door is overloaded, or the tunnel's far end is between
# pods. Every other 4xx is this request being wrong, and re-sending it cannot
# change the answer; a 5xx outside this set is a bug at the door, which a
# retry only repeats. A dropped connection is retried like these. The same
# body matters on the opening POST: the retry carries the same backend
# message id, and the door answers it with the task the first attempt
# started rather than routing a second message (``injectSubmission`` in
# a2a/gateway/inject.go).
RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})


def conversation_key(run_id: str, case_id: str, repetition: str) -> str:
    """The conversation key for one case's repetition in one run, unprefixed."""
    return KEY_SEPARATOR.join((run_id, case_id, repetition))


def message_id(run_id: str, case_id: str, repetition: str) -> str:
    """The backend message id for the opening POST: the same triple."""
    return KEY_SEPARATOR.join((run_id, case_id, repetition))


def parse_reason(text: str) -> str:
    """The reason token in an executor's terminal message, or ``""``.

    ``reason: hermes-exited-nonzero - exit status 1`` yields
    ``hermes-exited-nonzero``; a message without the prefix yields nothing,
    and is graded as an unknown reason.
    """
    stripped = text.strip()
    if not stripped.startswith(REASON_PREFIX):
        return ""
    rest = stripped[len(REASON_PREFIX) :].strip()
    return rest.split(" ", 1)[0] if rest else ""


def budget_floor(grace_seconds: float) -> float:
    """The smallest budget that can be classified: the grace plus the margin."""
    return grace_seconds + GRACE_MARGIN_SECONDS


class BudgetBelowFloor(ValueError):
    """The configured budget sits inside the gateway's first-event grace.

    Raised before anything is started. A budget below the floor would expire
    while an active task with nothing on its stream is still legitimately
    pre-first-event, and the read at the deadline could not tell a slow
    install from one with no executor.
    """


def check_budget(configured: float, grace_seconds: float) -> float:
    """Return ``configured`` if it clears the floor; refuse otherwise."""
    floor = budget_floor(grace_seconds)
    if configured < floor:
        raise BudgetBelowFloor(
            f"the inject budget {configured:.0f}s is below the floor of {floor:.0f}s "
            f"(the gateway's first-event grace {grace_seconds:.0f}s plus a "
            f"{GRACE_MARGIN_SECONDS:.0f}s margin): a task no executor has taken cannot be "
            "classified inside the grace, so the run refuses to start rather than grade "
            "a slow install as a hung one"
        )
    return configured


class InjectUnavailable(RuntimeError):
    """The exchange never reached the gateway, or lost it.

    A refused connection, a tunnel that died, a retryable status from the
    endpoint, a body that is not JSON. Never the agent's answer: a task an
    executor took and ended ``failed`` is an outcome and is graded.

    ``retryable`` says whether the same exchange could plausibly succeed once
    the harness respawns the tunnel. A 4xx cannot -- the request is wrong, and
    sending it again produces the same refusal.
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class Fold:
    """A conversation materialised from the entries the gateway relayed.

    The transcript is what a chat user would have seen: posts the gateway
    made, edits rewriting the rolling progress line in place, and the two
    lifecycle entries bracketing each task.

    ``deliverable`` is the answer: every post this task produced that nothing
    rewrote, in order. Two rules make that the right set.

    The rolling status line is a post the relay rewrites as progress arrives
    (``⏳ submitted…`` becoming ``⚙️ working`` and so on), so an edited post is
    presentation and is excluded. Discriminating on "was this ever edited"
    rather than on the wording of the placeholder keeps the harness out of the
    business of recognising the relay's prose, which is free to change.

    And the answer can be more than one post. ``Gateway.post`` splits anything
    over its backend chunk cap into separate posts, so an agent report of any
    length arrives as several -- taking only the last would grade a long RCA
    on its closing fragment, which is how a report naming the right cause in
    its first paragraph fails a phrase check. They are joined with no
    separator, because that is what reassembles a chunked message exactly: the
    gateway cuts on a line break where it can and mid-text where it cannot, so
    anything inserted between chunks could fall inside the phrase a verifier
    is looking for.

    The fold also keeps the task's lifecycle as the read route showed it
    (``executor_states``), recording each new state as an ``a2a.status-update``
    trajectory entry, with the terminal as the final one.
    """

    task_id: str
    entries: list[dict[str, Any]] = field(default_factory=list)
    posts: list[dict[str, Any]] = field(default_factory=list)
    edited: set[str] = field(default_factory=set)
    trajectory: list[dict[str, Any]] = field(default_factory=list)
    terminal: str = ""
    terminal_source: str = ""
    terminal_reason: str = ""
    # The result artifact's text as the read route's fold reported it, when
    # the terminal was adopted from the stream rather than posted by the
    # relay (:data:`OUTCOME_STREAM_TERMINAL`). The deliverable falls back to
    # it when the conversation received no answer post.
    stream_result: str = ""
    # Every executor state the read route showed for this task, in the order
    # seen: the lifecycle. Terminal states land here too, through
    # :meth:`mark_terminal`.
    executor_states: list[str] = field(default_factory=list)
    malformed: int = 0
    # Whether the entries being folded belong to this task yet. A conversation
    # carries every task's entries, and the task entry the gateway writes when
    # it mints one is the boundary.
    in_window: bool = False
    saw_task_marker: bool = False

    @property
    def final(self) -> bool:
        return self.terminal != ""

    @property
    def deliverable(self) -> str:
        """Everything this task posted that nothing rewrote, reassembled.

        The window is decided here rather than while folding: whether a post
        belongs to this task is only answerable once it is known whether this
        task's marker appeared at all, and a fold reading a bounded transcript
        may never see one. With a marker, the posts between it and the next
        task's; without, every post, which is over-wide but beats reporting no
        answer on a conversation that has one.
        """
        windowed = [post for post in self.posts if post["inWindow"]]
        if not windowed:
            windowed = self.posts
        posted = "".join(post["text"] for post in windowed if post["messageId"] not in self.edited)
        return posted or self.stream_result

    @property
    def gateway_declared(self) -> bool:
        """Whether the terminal is the gateway's word rather than an executor's.

        "The task failed" and "the gateway could not start the task" are the
        same state and mean opposite things: the first is what the executor
        did with the ask, the second is that no executor ever saw it. Grading
        the second as an answer scores a bus outage against the agent. Three
        sources say the second in different ways -- the gateway could not
        publish it, the supervisor ended an executor that died or never ran,
        and the never-started heal released one nobody took.
        """
        return self.terminal_source in (
            TERMINAL_SOURCE_GATEWAY,
            TERMINAL_SOURCE_SUPERVISOR,
            TERMINAL_SOURCE_NEVER_STARTED,
        )

    @property
    def never_started(self) -> bool:
        """Whether no executor ever took the task.

        Set from the read route -- active, nothing on the stream, age past
        the gateway's grace -- or from the gateway's own heal if a later turn
        released the task first. An install with no executor on the addressee
        (a `next` install whose bridge is not declared) lands here, and it is
        infrastructure rather than an agent that answered badly.
        """
        return self.terminal_source == TERMINAL_SOURCE_NEVER_STARTED

    @property
    def reason_token(self) -> str:
        """The executor's reason token for the terminal, or ``""``."""
        return parse_reason(self.terminal_reason)

    @property
    def infrastructure_terminal(self) -> str:
        """Why an executor-declared terminal is infrastructure, or ``""``.

        A failed terminal is not always the persona's failure. The bridge's
        and the worker adapter's own reasons (:data:`INFRASTRUCTURE_REASONS`)
        say the executor broke around the task; a rejected terminal says the
        submission carried nothing the executor could run; a
        canceled-before-start says the bridge dropped a queued task that
        never ran. None is an answer. Every other failed or canceled
        terminal, including one with a reason this side does not know, is
        graded.
        """
        if not self.final or self.gateway_declared:
            return ""
        if self.terminal == STATE_REJECTED:
            return "the executor rejected the task before running it"
        token = self.reason_token
        if token in INFRASTRUCTURE_REASONS:
            return f"the executor's own failure ({token})"
        if token == REASON_CANCELED_BEFORE_START:
            return "the bridge cancelled the task out of its queue before it ran"
        return ""

    @property
    def queued_only(self) -> bool:
        """Whether the stream never showed more than ``submitted``.

        The bridge publishes ``submitted`` when it queues a task behind its
        concurrency cap and ``working`` only when it spawns, so a task whose
        lifecycle is submitted alone sat in the queue for as long as it was
        watched.
        """
        return bool(self.executor_states) and set(self.executor_states) <= {STATE_SUBMITTED}

    def apply(self, entry: Any) -> None:
        """Fold one transcript entry, in sequence order."""
        if not isinstance(entry, dict):
            self.malformed += 1
            return
        kind = entry.get("kind")
        if not isinstance(kind, str) or not isinstance(entry.get("seq"), int):
            self.malformed += 1
            return
        self.entries.append(entry)
        text = str(entry.get("text") or "")
        entry_message_id = str(entry.get("messageId") or "")

        if kind == ENTRY_POST:
            self.posts.append(
                {"messageId": entry_message_id, "text": text, "inWindow": self.in_window}
            )
            self._record(EVENT_ENTRY_POST, {"messageId": entry_message_id}, text)
        elif kind == ENTRY_EDIT:
            if entry_message_id:
                self.edited.add(entry_message_id)
            self._record(EVENT_ENTRY_EDIT, {"messageId": entry_message_id}, text)
        elif kind == ENTRY_TASK:
            # A conversation carries every task's entries. This task's own
            # marker opens its window and the next task's marker closes it.
            self.in_window = entry.get("taskId") == self.task_id
            if self.in_window:
                self.saw_task_marker = True
            self._record(EVENT_ENTRY_TASK, {"taskId": entry.get("taskId")}, None)
        elif kind == ENTRY_TERMINAL:
            # Only THIS task's terminal ends the wait. A conversation can
            # carry more than one task -- the case runner's follow-up turns
            # are further tasks on the same key -- and an earlier one's
            # terminal must not be read as this one's answer.
            if entry.get("taskId") == self.task_id:
                self.mark_terminal(
                    str(entry.get("state") or ""),
                    str(entry.get("source") or ""),
                    str(entry.get("reason") or ""),
                )
        else:
            self.malformed += 1

    def note_executor_state(self, state: str) -> None:
        """Record a non-final state the read route showed, once per state."""
        if not state or state in self.executor_states or state in TERMINAL_STATES:
            return
        self.executor_states.append(state)
        self._record_status(state, final=False, text="")

    def mark_terminal(self, state: str, source: str, reason: str) -> None:
        """Record how the task ended, once; later calls do not overwrite."""
        if self.final or not state:
            return
        self.terminal = state
        self.terminal_source = source
        self.terminal_reason = reason
        self.executor_states.append(state)
        self._record_status(state, final=True, text=reason)

    def _record_status(self, state: str, *, final: bool, text: str) -> None:
        self.trajectory.append(
            {
                "name": EVENT_ENTRY_STATUS,
                "args": {"state": state, "final": final},
                "result": text or None,
                "status": ENTRY_STATUS_DONE,
            }
        )

    def _record(self, name: str, args: dict[str, Any], result: str | None) -> None:
        self.trajectory.append(
            {"name": name, "args": args, "result": result, "status": ENTRY_STATUS_DONE}
        )


@dataclass
class Probe:
    """One answer from the read route: the gateway's record, as it stands.

    Nothing here is a verdict; :meth:`InjectTask.await_terminal` classifies
    from it. ``executor_state`` is ``""`` when the task's stream holds no
    event at all.
    """

    backend: str = ""
    inject_only: bool = False
    grace_seconds: float = 0.0
    active: bool = False
    task_id: str = ""
    submitted_at: str = ""
    age_seconds: float = 0.0
    detached: bool = False
    executor_state: str = ""
    final: bool = False
    # The fold's terminal, when ``final``: whose word it is (the gateway's
    # ``executor`` or ``gateway``), the result artifact's text and the
    # terminal's status message, as the read route reports them.
    terminal_source: str = ""
    result: str = ""
    reason: str = ""
    last_post: str = ""
    error: str = ""

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> Probe | None:
        """The probe a ``GET`` reply carries, or ``None`` when it carries none."""
        raw = body.get("probe")
        if not isinstance(raw, dict):
            return None

        def _num(name: str) -> float:
            value = raw.get(name)
            return float(value) if isinstance(value, (int, float)) else 0.0

        last_post = raw.get("lastPost")
        return cls(
            backend=str(raw.get("backend") or ""),
            inject_only=bool(raw.get("injectOnly")),
            grace_seconds=_num("graceSeconds"),
            active=bool(raw.get("active")),
            task_id=str(raw.get("taskId") or ""),
            submitted_at=str(raw.get("submittedAt") or ""),
            age_seconds=_num("ageSeconds"),
            detached=bool(raw.get("detached")),
            executor_state=str(raw.get("executorState") or ""),
            final=bool(raw.get("final")),
            terminal_source=str(raw.get("terminalSource") or ""),
            result=str(raw.get("result") or ""),
            reason=str(raw.get("reason") or ""),
            last_post=str(last_post.get("text") or "") if isinstance(last_post, dict) else "",
            error=str(raw.get("error") or ""),
        )

    @property
    def could_look(self) -> bool:
        """Whether the gateway read its record and the stream without error."""
        return self.error == ""

    def concerns(self, task_id: str) -> bool:
        """Whether the record's active task is the one being awaited."""
        return self.active and self.task_id == task_id

    @property
    def past_grace(self) -> bool:
        """Whether the active task is older than the gateway's grace."""
        return self.grace_seconds > 0 and self.age_seconds > self.grace_seconds

    def describe(self) -> str:
        """One line for a log or an error, naming what the gateway said."""
        if self.error:
            return f"could not look: {self.error}"
        if not self.active:
            return "no active task on the record"
        text = f"active task {self.task_id}, executor state {self.executor_state or 'none'}"
        if self.final:
            text += f" (final, {self.terminal_source or 'source unknown'})"
        if self.detached:
            text += ", detached"
        text += f", {self.age_seconds:.0f}s old against a {self.grace_seconds:.0f}s grace"
        if self.submitted_at:
            text += f", submitted at {self.submitted_at}"
        if self.last_post:
            excerpt = self.last_post[:LAST_POST_EXCERPT_CHARS]
            if len(self.last_post) > LAST_POST_EXCERPT_CHARS:
                excerpt += "…"
            text += f"; last post: {excerpt!r}"
        return text


@dataclass
class Exchange:
    """What one exchange produced: the folded conversation and why it ended."""

    fold: Fold
    outcome: str
    conversation: str
    task_id: str
    # The read route's last answer, when any poll carried one.
    probe: Probe | None = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses every redirect, turning it into an ``HTTPError`` instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


# ProxyHandler({}): the destination is always a loopback port-forward, and
# urllib would otherwise honour http_proxy with no bypass for it. Same
# reasoning as the harness's own opener.
_OPENER = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))


def _request(
    url: str, timeout: float, token: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """One JSON round trip to the gateway, carrying the door's bearer token.

    Raises:
        InjectUnavailable: The gateway could not be reached, refused the
            token, or answered with something that is not a JSON object.
    """
    data = None
    headers = {AUTHORIZATION_HEADER: BEARER_PREFIX + token}
    method = "GET"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        if exc.code == 401:
            # Named, because the cause is one thing and the fix is one thing:
            # the token the harness holds is not the token the operator
            # rendered into the Secret beside the door.
            raise InjectUnavailable(
                f"the inject door refused the bearer token ({url}): read it from the "
                f"{DEFAULT_TOKEN_SECRET_HINT}",
                retryable=False,
            ) from exc
        raise InjectUnavailable(
            f"HTTP {exc.code} from {url}: {detail}", retryable=exc.code in RETRYABLE_STATUSES
        ) from exc
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise InjectUnavailable(f"{type(exc).__name__} from {url}: {exc}") from exc
    if not isinstance(body, dict):
        raise InjectUnavailable(
            f"{url} returned non-object JSON: {type(body).__name__}", retryable=False
        )
    return body


class InjectTask:
    """One task through the gateway's front door: submit once, await its end.

    The submission is held across attempts so a retry that respawned the
    tunnel re-polls the conversation rather than sending the prompt a second
    time; a retry of the POST itself carries the same body and message id,
    which the door dedupes. The conversation is replayed from the start on
    each attempt, so a fold that began again is still complete.
    """

    def __init__(
        self,
        *,
        base_url: str,
        conversation: str,
        prompt: str,
        token: str,
        author: str = DEFAULT_AUTHOR,
        message_id: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.conversation = conversation
        self.prompt = prompt
        self.token = token
        self.author = author
        # The backend message id, which the gateway's ingress log joins to the
        # correlationId and the door dedupes on. The run, case and repetition
        # go here, so the audit chain reaches the eval record with nothing
        # added (the A2A owner's ask on the design doc); empty lets the
        # gateway mint one, and then nothing is deduped.
        self.message_id = message_id
        self.task_id = ""
        self.note = ""
        # Why the door started nothing, when it started nothing: one of the
        # REFUSAL_* codes. The note beside it is the gateway's prose.
        self.refusal = ""
        # The gateway's own first-event grace, learned from the preflight
        # read or the submission, and whether the gateway runs on the inject
        # door alone. Neither is a deadline: the grace is the floor under
        # the budget and the yardstick a read's age is measured against.
        self.first_event_grace = 0.0
        self.inject_only = False
        self.backend = ""
        self.submitted_at = 0.0
        # Whether a cancel for this task reached the gateway (the route
        # answered), so a caller's record can say what was sent rather than
        # what was attempted.
        self.cancel_sent = False

    def preflight(self) -> Probe:
        """Read the conversation before anything is started.

        The key is fresh, so the record is empty; what the read returns is
        the gateway's grace and whether it runs on the door alone, which is
        what the budget check and the run's record need before the POST.
        Nothing is minted by asking.

        Raises:
            InjectUnavailable: The gateway could not be reached.
        """
        body = self._poll("", 0, 0)
        probe = Probe.from_body(body) or Probe(error="no probe in the reply")
        if probe.grace_seconds > 0:
            self.first_event_grace = probe.grace_seconds
        self.inject_only = probe.inject_only
        self.backend = probe.backend
        if probe.inject_only:
            _log.info(
                "inject: the gateway runs on the inject door alone (inject-only); "
                "no Discord token or Chat relay is armed"
            )
        return probe

    def submit(self) -> str:
        """POST the prompt and return the task id the gateway started.

        Returns ``""`` when the door started nothing: an author the principal
        map does not know, a submission the gateway could not publish, or a
        message that landed as a steer, a status query or a stop.
        :attr:`refusal` then carries which of those it was and ``note`` the
        gateway's own prose, and the caller decides what it means -- for a
        case's opening turn all of them are infrastructure, because no agent
        ever saw the prompt.

        A task id means the submission is on the bus, not merely that the
        gateway minted one: the door waits for the publish before it answers
        (``TaskObserver.TaskAccepted``), because a task that never reached a
        subject has no terminal to await and would burn the whole budget.

        Raises:
            InjectUnavailable: The gateway could not be reached.
        """
        if self.task_id:
            return self.task_id
        payload: dict[str, Any] = {
            "conversation": self.conversation,
            "author": self.author,
            "text": self.prompt,
        }
        if self.message_id:
            payload["messageId"] = self.message_id
        body = _request(
            self.base_url + INJECT_PATH, SUBMIT_TIMEOUT_SECONDS, self.token, payload
        )
        grace = body.get("firstEventGraceSeconds")
        if isinstance(grace, (int, float)) and grace > 0:
            self.first_event_grace = float(grace)
        # The gateway prefixes the key it was given, and every later GET has
        # to use the prefixed form; adopt what it handed back rather than
        # spelling the prefix here too.
        self.conversation = str(body.get("conversation") or self.conversation)
        self.note = str(body.get("note") or "")
        self.refusal = str(body.get("refusal") or "")
        self.task_id = str(body.get("taskId") or "")
        if self.task_id:
            self.submitted_at = time.monotonic()
            if body.get("deduplicated"):
                _log.info(
                    "inject: the door answered a retried POST with the task it had already "
                    "started (%s on %s)",
                    self.task_id,
                    self.conversation,
                )
            else:
                _log.info(
                    "inject: submitted task %s on conversation %s", self.task_id, self.conversation
                )
        else:
            _log.warning(
                "inject: the gateway started no task on %s (%s): %s",
                self.conversation,
                self.refusal or "no refusal code",
                self.note,
            )
        return self.task_id

    def await_terminal(self, task_id: str, *, deadline: float) -> Exchange:
        """Await the terminal of ``task_id``, folding what the relay posts.

        A function of a task id rather than of "the task I submitted", so the
        day a parent task's events name a child's id, awaiting the child is
        this same call.

        Every poll carries ``probe=1``, and the read route's answer is where
        the task's lifecycle comes from and where two classifications are
        made before the deadline: an active task with nothing on its stream
        past the gateway's grace is one no executor took
        (:data:`OUTCOME_NEVER_STARTED`), and a task the stream shows terminal
        before the relay has posted it is finished (:meth:`_finish`, which
        adopts the fold if the relay never posts). At the deadline one more
        read classifies the rest (:meth:`_classify`).
        Nothing is ever sent to find out, because anything sent would be a
        turn.

        Args:
            task_id: The task whose terminal ends the wait.
            deadline: ``time.monotonic()`` instant after which the wait ends
                on the read route's answer. The budget behind it clears the
                floor (:func:`check_budget`).

        Raises:
            InjectUnavailable: The gateway could not be reached or was lost.
        """
        fold = Fold(task_id)
        after = 0
        probe: Probe | None = None
        while True:
            if fold.final:
                return self._ended(fold, task_id, probe)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._classify(fold, task_id, after)
            body = self._poll(task_id, after, remaining)
            after, fresh = self._absorb(fold, body, after)
            answered = Probe.from_body(body)
            if answered is not None:
                probe = answered
                self._note(fold, task_id, answered)
                if answered.could_look and answered.concerns(task_id) and not fold.final:
                    if answered.final:
                        return self._finish(fold, task_id, after, answered)
                    if answered.executor_state == "" and answered.past_grace:
                        return self._never_started(fold, task_id, answered)
            if not fresh and not fold.final:
                time.sleep(POLL_PAUSE_SECONDS)

    def _ended(self, fold: Fold, task_id: str, probe: Probe | None) -> Exchange:
        outcome = OUTCOME_NEVER_STARTED if fold.never_started else OUTCOME_TERMINAL
        return Exchange(fold, outcome, self.conversation, task_id, probe)

    def _never_started(self, fold: Fold, task_id: str, probe: Probe) -> Exchange:
        """The read showed nothing on the stream past the grace: nobody took it."""
        _log.warning(
            "inject: no executor has touched task %s on %s: %s",
            task_id,
            self.conversation,
            probe.describe(),
        )
        fold.mark_terminal(STATE_FAILED, TERMINAL_SOURCE_NEVER_STARTED, "")
        return Exchange(fold, OUTCOME_NEVER_STARTED, self.conversation, task_id, probe)

    def _note(self, fold: Fold, task_id: str, probe: Probe) -> None:
        """Record what a read showed of this task's lifecycle."""
        if probe.grace_seconds > 0:
            self.first_event_grace = probe.grace_seconds
        if probe.could_look and probe.concerns(task_id) and not probe.final and not fold.final:
            fold.note_executor_state(probe.executor_state)

    def _absorb(self, fold: Fold, body: dict[str, Any], after: int) -> tuple[int, bool]:
        """Fold one ``GET`` reply; return the next ``after`` and whether it had entries."""
        entries = body.get("entries")
        fresh = isinstance(entries, list) and bool(entries)
        if isinstance(entries, list):
            for entry in entries:
                fold.apply(entry)
        # The gateway's own sequence rather than the fold's high-water mark:
        # it keeps counting across the evictions that bound its transcript,
        # and following its number is what stops a poll re-reading entries
        # or stalling on a gap it can never fill.
        last_seq = body.get("lastSeq")
        if isinstance(last_seq, int) and last_seq > after:
            after = last_seq
        # The terminal is answered out of band as well as in the entries, so
        # a reader that arrived after the event still learns it rather than
        # waiting for one that has been and gone.
        terminal = body.get("terminal")
        if isinstance(terminal, str) and terminal and not fold.final:
            fold.mark_terminal(terminal, "", "")
        return after, fresh

    def _classify(self, fold: Fold, task_id: str, after: int) -> Exchange:
        """The deadline read: ask the gateway what its record holds, send nothing.

        One read, and one of five answers. A terminal the fold now has, or
        the stream shows: finished. Nothing on the stream past the grace: no
        executor took it. Only ``submitted`` ever: queued behind the bridge's
        cap for the whole budget. ``working`` (or detached): a graded timeout.
        Anything else -- the gateway could not look, or the record no longer
        holds the task and this side never saw a terminal -- cannot be
        classified.
        """
        body = self._poll(task_id, after, 0)
        after, _ = self._absorb(fold, body, after)
        probe = Probe.from_body(body) or Probe(error="no probe in the reply")
        self._note(fold, task_id, probe)
        _log.info(
            "inject: deadline read on %s for %s: %s", self.conversation, task_id, probe.describe()
        )
        if fold.final:
            return self._ended(fold, task_id, probe)
        if not probe.could_look or not probe.concerns(task_id):
            return Exchange(fold, OUTCOME_UNCLASSIFIED, self.conversation, task_id, probe)
        if probe.final:
            return self._finish(fold, task_id, after, probe)
        if probe.executor_state == "":
            if probe.past_grace:
                return self._never_started(fold, task_id, probe)
            # Inside the grace at the deadline: the budget sat below the
            # floor, which check_budget refuses, so this is a gateway whose
            # reported grace changed under the run. Not classifiable.
            return Exchange(fold, OUTCOME_UNCLASSIFIED, self.conversation, task_id, probe)
        if fold.queued_only and not probe.detached:
            return Exchange(fold, OUTCOME_QUEUED, self.conversation, task_id, probe)
        return Exchange(fold, OUTCOME_DEADLINE, self.conversation, task_id, probe)

    def _finish(self, fold: Fold, task_id: str, after: int, probe: Probe) -> Exchange:
        """A read showed the task terminal on the stream: wait for the relay.

        Unprobed polls for :data:`FINISHED_SETTLE_SECONDS`, so the relay's
        deliverable and terminal land in the fold in the order a chat user
        would have read them (:data:`OUTCOME_TERMINAL`). If the relay never
        delivers -- it acked the terminal and lost the record write, or the
        pod restarted between the two -- the read's fold is adopted whole:
        the terminal with the source the gateway attributed it to, its
        reason, and the result artifact's text as the deliverable
        (:data:`OUTCOME_STREAM_TERMINAL`). The caller grades an executor's
        terminal and classifies the gateway's as infrastructure; neither is
        cancelled, because nothing is running.
        """
        settle = time.monotonic() + FINISHED_SETTLE_SECONDS
        while not fold.final:
            remaining = settle - time.monotonic()
            if remaining <= 0:
                break
            body = self._poll(task_id, after, remaining, probe=False)
            after, fresh = self._absorb(fold, body, after)
            if not fresh and not fold.final:
                time.sleep(POLL_PAUSE_SECONDS)
        if fold.final:
            return Exchange(fold, OUTCOME_TERMINAL, self.conversation, task_id, probe)
        _log.warning(
            "inject: the relay posted no terminal for %s within %.0fs of the stream's; "
            "adopting the stream's fold: %s",
            task_id,
            FINISHED_SETTLE_SECONDS,
            probe.describe(),
        )
        fold.stream_result = probe.result
        fold.mark_terminal(
            probe.executor_state or STATE_FAILED,
            probe.terminal_source or TERMINAL_SOURCE_EXECUTOR,
            probe.reason,
        )
        return Exchange(fold, OUTCOME_STREAM_TERMINAL, self.conversation, task_id, probe)

    def _poll(self, task_id: str, after: int, budget: float, *, probe: bool = True) -> dict[str, Any]:
        """One blocking GET for whatever is new on the conversation."""
        wait = max(0, min(POLL_WAIT_SECONDS, int(budget)))
        params: dict[str, Any] = {"after": after, "wait": wait}
        if task_id:
            params["task"] = task_id
        if probe:
            params[PROBE_PARAM] = 1
        query = urllib.parse.urlencode(params)
        url = (
            self.base_url
            + CONVERSATIONS_PATH
            + urllib.parse.quote(self.conversation, safe="")
            + "?"
            + query
        )
        # The margin is over what the SERVER was asked to wait, not over the
        # caller's remaining budget: a client timeout should mean the
        # connection died, never that the gateway answered a moment late.
        return _request(url, wait + POLL_TIMEOUT_MARGIN_SECONDS, self.token)

    def cancel(self, task_id: str = "", *, settle: float | None = None) -> Exchange | None:
        """Stop the conversation's task, and read what the stop found.

        The explicit cancel route, not the stop text. A control action must
        not depend on a phrase list the gateway is free to change, and an ask
        that happened to be the word "stop" would be indistinguishable from
        one. What lands on the bus is the same ``kind: cancel`` envelope the
        text path publishes.

        Sent only after a read has classified the task, never before, and in
        every outcome that leaves an active task: working at the budget (a
        graded timeout), queued for the whole budget, never taken by any
        executor, and a read that could not classify. The classification is
        the read's; nothing here changes it. The cancel names ``task_id`` --
        the id the opening POST was answered with -- so the gateway publishes
        it whether or not its record still holds the task: for a task nobody
        took, the gateway's own never-started heal releases the record on
        this very turn, while the submission is still on the bus, and the
        bridge's durable consumer delivers from the start of the stream, so
        a bridge that binds later within retention would otherwise run the
        stale prompt. Honestly: on today's bridge the cancel does not prevent
        that spawn -- the consumer delivers serially, so an idle worker
        spawns the stale prompt before the cancel is dispatched, and the
        cancel then kills it inside the bridge's kill grace with a
        ``canceled-by-request`` terminal; ``canceled-before-start`` is what a
        task still queued behind the cap gets. The cancel bounds the stray
        run and leaves the record. The terminal that follows, if an executor
        confirms inside ``settle``, is ``canceled`` with the executor's
        reason.

        Returns:
            The folded outcome when a terminal arrived inside ``settle``,
            otherwise ``None``. Best effort throughout: a cancel that cannot
            be delivered is logged, because the caller is already abandoning
            the task.
        """
        task_id = task_id or self.task_id
        # Resolved here rather than in the signature, so the module constant
        # is read at call time (a default argument is bound once, at import).
        if settle is None:
            settle = CANCEL_SETTLE_SECONDS
        url = (
            self.base_url
            + CONVERSATIONS_PATH
            + urllib.parse.quote(self.conversation, safe="")
            + CANCEL_SUFFIX
        )
        payload: dict[str, Any] = {"author": self.author}
        if task_id:
            payload["taskId"] = task_id
        if self.message_id:
            payload["messageId"] = self.message_id + CANCEL_MESSAGE_ID_SUFFIX
        try:
            _request(url, SUBMIT_TIMEOUT_SECONDS, self.token, payload)
            self.cancel_sent = True
            _log.info("inject: cancelled task %s on %s", task_id, self.conversation)
        except InjectUnavailable as exc:
            _log.warning("inject: the cancel for task %s did not land: %s", task_id, exc)
            return None
        if not task_id or settle <= 0:
            return None
        try:
            return self.await_terminal(task_id, deadline=time.monotonic() + settle)
        except InjectUnavailable as exc:
            _log.warning("inject: could not read what the cancel of %s found: %s", task_id, exc)
            return None
