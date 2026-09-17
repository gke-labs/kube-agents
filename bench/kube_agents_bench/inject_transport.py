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
gateway's inject backend, which enters ``handleInbound`` like a message from
any other backend -- routing, the session record, ``startTask``, the bus, and
the relay posting the reply back into the conversation. What the harness
grades is what the conversation received, which is the point: if a customer
would not see it, a verifier should not depend on it.

This module is the door half: the two HTTP calls, the fold of a
conversation's transcript, and one submit-and-await exchange. Environment,
port-forwards, retry classes and the mapping onto ``AgentResult`` stay in
:mod:`kube_agents_bench.harness`, which is the only importer.

The wait is written as "await the terminal of task id X" rather than "await
the task I submitted" (the A2A owner's instruction on #1661): when
agent-initiated delegation becomes a child task on the bus, the parent's
events will name the child's id and the same call awaits that instead. The
kanban poll that stands in for child tasks today is NOT here -- it lives in
the case runner, so it can be deleted without touching this module.

The gateway's inject backend is dev and eval only and carries no
authentication; see ``a2a/gateway/inject.go`` and the test-backend section of
``docs/designs/spec-chatops-gateway.md``.
"""

from __future__ import annotations

import http.client
import json
import logging
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "DEFAULT_AUTHOR",
    "ENTRY_EDIT",
    "ENTRY_POST",
    "ENTRY_TASK",
    "ENTRY_TERMINAL",
    "EVENT_ENTRY_EDIT",
    "EVENT_ENTRY_POST",
    "EVENT_ENTRY_TASK",
    "EVENT_ENTRY_TERMINAL",
    "OUTCOME_DEADLINE",
    "OUTCOME_NOT_ACCEPTED",
    "OUTCOME_TERMINAL",
    "STATE_COMPLETED",
    "STOP_TEXT",
    "TERMINAL_SOURCE_EXECUTOR",
    "TERMINAL_SOURCE_GATEWAY",
    "TERMINAL_STATES",
    "Exchange",
    "Fold",
    "InjectTask",
    "InjectUnavailable",
    "mint_conversation",
]

_log = logging.getLogger("kube_agents_bench.inject_transport")

# The gateway's two endpoints (``a2a/gateway/inject.go``).
INJECT_PATH = "/inject"
CONVERSATIONS_PATH = "/conversations/"

# The author the operator's rendered principal map admits (``a2aInjectAuthor``
# in the operator's A2A manifests). An author with no entry in the map is
# dropped at verification, so this is not cosmetic.
DEFAULT_AUTHOR = "devops-bench"

# Conversation keys the harness mints. The gateway prefixes what it is given
# with ``inject:``; this sits inside that, so a run's conversation is
# identifiable in the gateway's ingress log and in the session registry.
CONVERSATION_PREFIX = "devops-bench-"
CONVERSATION_BYTES = 6

# The gateway's cancel affordance, which is a normal message rather than an
# API: a turn whose whole text is "stop" publishes ``kind: cancel`` for the
# running task (``a2a/gateway/text.go``). Abandoning a task without it leaves
# an executor working for nobody.
STOP_TEXT = "stop"

# Transcript entry kinds, as the gateway spells them.
ENTRY_POST = "post"
ENTRY_EDIT = "edit"
ENTRY_TASK = "task"
ENTRY_TERMINAL = "terminal"

# Trajectory entry names this transport records. The inject door carries no
# tool calls, and structurally cannot: the relay never posts `activity`
# artifacts to a conversation (they are debug and audit views), so no
# transport that reads a conversation sees them however the executor behaves.
# What this one can truthfully record is the conversation itself: what the
# agent said, and how the task ended. ``scoring.py`` reads the terminal one as the record's
# liveness signal and duplicates that literal (importing this module would
# drag the transport into the scorer); ``test_scoring.py`` asserts the two
# agree, so change it in both files or in neither.
EVENT_ENTRY_POST = "inject.post"
EVENT_ENTRY_EDIT = "inject.edit"
EVENT_ENTRY_TASK = "inject.task"
EVENT_ENTRY_TERMINAL = "inject.terminal"

STATE_COMPLETED = "completed"
TERMINAL_STATES = frozenset({STATE_COMPLETED, "failed", "canceled", "rejected"})

# Who declared a terminal (``TerminalSource`` in a2a/gateway/adapter.go). A
# gateway-declared one is a task that never reached the bus, so no executor
# saw the prompt -- infrastructure, not the agent's answer.
TERMINAL_SOURCE_EXECUTOR = "executor"
TERMINAL_SOURCE_GATEWAY = "gateway"

# Outcomes of one exchange.
OUTCOME_TERMINAL = "terminal"
OUTCOME_NOT_ACCEPTED = "not-accepted"
OUTCOME_DEADLINE = "deadline"

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

# Pause before re-issuing a poll the gateway answered with nothing. The GET
# blocks server-side already, so this only paces the pathological case of an
# endpoint answering empty at once.
POLL_PAUSE_SECONDS = 1.0


def mint_conversation() -> str:
    """A fresh conversation key, so two runs never share a session record."""
    return CONVERSATION_PREFIX + secrets.token_hex(CONVERSATION_BYTES)


class InjectUnavailable(RuntimeError):
    """The exchange never reached the gateway, or lost it.

    A refused connection, a tunnel that died, a 5xx from the endpoint, a body
    that is not JSON. Never the agent's answer: a task an executor took and
    ended ``failed`` is an outcome and is graded.

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
    """

    task_id: str
    entries: list[dict[str, Any]] = field(default_factory=list)
    posts: list[dict[str, Any]] = field(default_factory=list)
    edited: set[str] = field(default_factory=set)
    trajectory: list[dict[str, Any]] = field(default_factory=list)
    terminal: str = ""
    terminal_source: str = ""
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
        return "".join(post["text"] for post in windowed if post["messageId"] not in self.edited)

    @property
    def gateway_declared(self) -> bool:
        """Whether the terminal is the gateway's word rather than an executor's.

        "The task failed" and "the gateway could not start the task" are the
        same state and mean opposite things: the first is what the executor
        did with the ask, the second is that no executor ever saw it. Grading
        the second as an answer scores a bus outage against the agent.
        """
        return self.terminal_source == TERMINAL_SOURCE_GATEWAY

    @property
    def observed(self) -> bool:
        """Whether anything past the gateway's own placeholder has arrived.

        The gateway posts the placeholder itself before the submission
        reaches the bus, so its presence says nothing about whether an
        executor is listening. An edit to it, or a second post, came from a
        real event on the task's stream and is the first evidence that
        something took the task.
        """
        return bool(self.edited) or len(self.posts) > 1 or self.final

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
        message_id = str(entry.get("messageId") or "")

        if kind == ENTRY_POST:
            self.posts.append(
                {"messageId": message_id, "text": text, "inWindow": self.in_window}
            )
            self._record(EVENT_ENTRY_POST, {"messageId": message_id}, text)
        elif kind == ENTRY_EDIT:
            if message_id:
                self.edited.add(message_id)
            self._record(EVENT_ENTRY_EDIT, {"messageId": message_id}, text)
        elif kind == ENTRY_TASK:
            # A conversation carries every task's entries. This task's own
            # marker opens its window and the next task's marker closes it.
            self.in_window = entry.get("taskId") == self.task_id
            if self.in_window:
                self.saw_task_marker = True
            self._record(EVENT_ENTRY_TASK, {"taskId": entry.get("taskId")}, None)
        elif kind == ENTRY_TERMINAL:
            state = str(entry.get("state") or "")
            # Only THIS task's terminal ends the wait. A conversation can
            # carry more than one task -- the case runner's follow-up turns
            # are further tasks on the same key -- and an earlier one's
            # terminal must not be read as this one's answer.
            source = str(entry.get("source") or "")
            if entry.get("taskId") == self.task_id:
                self.terminal = state
                self.terminal_source = source
            self._record(
                EVENT_ENTRY_TERMINAL,
                {"taskId": entry.get("taskId"), "state": state, "source": source},
                None,
            )
        else:
            self.malformed += 1

    def _record(self, name: str, args: dict[str, Any], result: str | None) -> None:
        self.trajectory.append(
            {"name": name, "args": args, "result": result, "status": "completed"}
        )


@dataclass
class Exchange:
    """What one exchange produced: the folded conversation and why it ended."""

    fold: Fold
    outcome: str
    conversation: str
    task_id: str


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses every redirect, turning it into an ``HTTPError`` instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


# ProxyHandler({}): the destination is always a loopback port-forward, and
# urllib would otherwise honour http_proxy with no bypass for it. Same
# reasoning as the harness's own opener.
_OPENER = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))


def _request(url: str, timeout: float, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """One JSON round trip to the gateway.

    Raises:
        InjectUnavailable: The gateway could not be reached, or answered with
            something that is not a JSON object.
    """
    data = None
    headers: dict[str, str] = {}
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
        # A 4xx is this request being wrong -- a malformed body, a
        # conversation key the gateway refuses -- and re-sending it cannot
        # change the answer. A 5xx, and the 503 the backend sends before its
        # handler is installed, clear on their own.
        raise InjectUnavailable(
            f"HTTP {exc.code} from {url}: {detail}", retryable=exc.code >= 500
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
    time. The conversation is replayed from the start on each attempt, so a
    fold that began again is still complete.
    """

    def __init__(
        self,
        *,
        base_url: str,
        conversation: str,
        prompt: str,
        author: str = DEFAULT_AUTHOR,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.conversation = conversation
        self.prompt = prompt
        self.author = author
        self.task_id = ""
        self.note = ""
        # When the acceptance window opened; see await_terminal.
        self._accept_started: float | None = None

    def submit(self) -> str:
        """POST the prompt and return the task id the gateway started.

        Returns ``""`` when the gateway answered the turn without starting a
        task: an author the principal map does not know, or a message that
        landed as a steer, a status query or a stop. ``note`` then carries the
        gateway's own explanation, and the caller decides what it means -- for
        a case's opening turn it is infrastructure, because no agent ever saw
        the prompt.

        Raises:
            InjectUnavailable: The gateway could not be reached.
        """
        if self.task_id:
            return self.task_id
        body = _request(
            self.base_url + INJECT_PATH,
            SUBMIT_TIMEOUT_SECONDS,
            {"conversation": self.conversation, "author": self.author, "text": self.prompt},
        )
        # The gateway prefixes the key it was given, and every later GET has
        # to use the prefixed form; adopt what it handed back rather than
        # spelling the prefix here too.
        self.conversation = str(body.get("conversation") or self.conversation)
        self.note = str(body.get("note") or "")
        self.task_id = str(body.get("taskId") or "")
        if self.task_id:
            _log.info(
                "inject: submitted task %s on conversation %s", self.task_id, self.conversation
            )
        else:
            _log.warning(
                "inject: the gateway started no task on %s: %s", self.conversation, self.note
            )
        return self.task_id

    def await_terminal(self, task_id: str, *, accept_timeout: float, deadline: float) -> Exchange:
        """Await the terminal of ``task_id``, folding what the relay posts.

        A function of a task id rather than of "the task I submitted", so the
        day a parent task's events name a child's id, awaiting the child is
        this same call.

        Args:
            task_id: The task whose terminal ends the wait.
            accept_timeout: Seconds the conversation may show nothing past the
                gateway's own placeholder before the wait ends
                ``OUTCOME_NOT_ACCEPTED`` -- an install with no executor on the
                addressee.
            deadline: ``time.monotonic()`` instant after which the wait ends
                ``OUTCOME_DEADLINE``.

        Raises:
            InjectUnavailable: The gateway could not be reached or was lost.
        """
        fold = Fold(task_id)
        after = 0
        # Set once per task, not per attempt: a transport retry re-enters this
        # method, and restarting the clock would let the acceptance window
        # run for accept_timeout all over again on each one.
        if self._accept_started is None:
            self._accept_started = time.monotonic()
        started = self._accept_started
        while True:
            if fold.final:
                return Exchange(fold, OUTCOME_TERMINAL, self.conversation, task_id)
            now = time.monotonic()
            if not fold.observed and now - started > accept_timeout:
                return Exchange(fold, OUTCOME_NOT_ACCEPTED, self.conversation, task_id)
            remaining = deadline - now
            if remaining <= 0:
                return Exchange(fold, OUTCOME_DEADLINE, self.conversation, task_id)
            # Do not block past whichever bound comes first, or the wait
            # overshoots the one that was going to end it.
            budget = remaining
            if not fold.observed:
                budget = min(budget, accept_timeout - (now - started))
            body = self._poll(task_id, after, budget)

            entries = body.get("entries")
            if isinstance(entries, list):
                for entry in entries:
                    fold.apply(entry)
            # The gateway's own sequence rather than the fold's high-water
            # mark: it keeps counting across the evictions that bound its
            # transcript, and following its number is what stops a poll
            # re-reading entries or stalling on a gap it can never fill.
            last_seq = body.get("lastSeq")
            if isinstance(last_seq, int) and last_seq > after:
                after = last_seq
            # The terminal is answered out of band as well as in the entries,
            # so a reader that arrived after the event still learns it rather
            # than waiting for one that has been and gone.
            terminal = body.get("terminal")
            if isinstance(terminal, str) and terminal and not fold.final:
                fold.terminal = terminal
            if not entries and not fold.final:
                time.sleep(POLL_PAUSE_SECONDS)

    def _poll(self, task_id: str, after: int, budget: float) -> dict[str, Any]:
        """One blocking GET for whatever is new on the conversation."""
        wait = max(0, min(POLL_WAIT_SECONDS, int(budget)))
        query = urllib.parse.urlencode({"after": after, "wait": wait, "task": task_id})
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
        return _request(url, wait + POLL_TIMEOUT_MARGIN_SECONDS)

    def cancel(self) -> None:
        """Ask the gateway to stop the running task. Best effort.

        Sent when the harness abandons a task, so an executor is not left
        working for a requester that has gone. It is an ordinary message --
        the gateway's stop affordance is a phrase, not an API -- so a
        conversation with nothing running answers it harmlessly.
        """
        try:
            _request(
                self.base_url + INJECT_PATH,
                SUBMIT_TIMEOUT_SECONDS,
                {"conversation": self.conversation, "author": self.author, "text": STOP_TEXT},
            )
            _log.info("inject: sent stop for task %s", self.task_id)
        except InjectUnavailable as exc:
            _log.warning("inject: the stop for task %s did not land: %s", self.task_id, exc)
