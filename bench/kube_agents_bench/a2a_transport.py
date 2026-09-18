"""The A2A bus transport behind ``AGENT_TRANSPORT=a2a``: a diagnostic.

The harness hands a case's prompt to the executor the way a task reaches it on
the bus under ``spec.mode: next``: one ``message`` envelope on
``a2a.tasks.{addressee}.{taskId}.in``, then the task's ``events`` folded until
a terminal ``status-update`` lands. It proves the bus, the stream and the
executor, not the auth callout: the ``eval`` principal and the bridge's own
``bridge`` principal are both static users listed in ``auth_users``, so a green
run says nothing about the callout. It skips the gateway -- its routing, its session
registry, the relay back -- which is why it is a diagnostic rather than the
next-mode transport the evals will run on. That one is the gateway's inject
adapter, planned and not yet built, which goes through the gateway's inbound
path and carries the only credential that may publish on every addressee's
``in`` subject; this transport authenticates as its own ``eval`` principal,
whose grants are publish on ``platform``'s ``in`` and subscribe on
``platform``'s ``events`` and ``supervisor`` and nothing else. Those wildcards
reach every ``platform`` task, the gateway's included: the events show each
task's id, the ``in`` publish takes a cancel or a message for any of them, and
the bridge cannot tell requesters apart. A static NATS grant cannot name one
task, so the key is held like the gateway's.

Envelope, subject and payload shapes are ``docs/designs/spec-a2a-payloads.md``;
the fold mirrors ``a2a/lib/fold.go``; the ids are minted the way
``a2a/gateway/gateway.go`` mints them. ``authority`` is null on every envelope
this transport publishes: the block is the gateway's to populate, its shape is
advisory, and nothing here invents one.

Two operations, and the second is a function of a task id rather than of "the
task I submitted": :meth:`BusClient.submit_and_await` publishes a submission
and awaits its terminal, :meth:`BusClient.await_terminal` awaits the terminal
of any task id on the addressee -- the same code will await a child task once
a parent's events name one. Nothing here polls the model; the kanban poll for
delegated cases is the harness's, in :mod:`kube_agents_bench.harness`, where
it can be deleted.

The ``eval`` principal holds no JetStream API grant, so the subscriptions are
core NATS subscriptions taken before the publish, and they have no replay.
That is the trade a diagnostic makes: a connection that drops mid-task cannot
recover what it missed, so it ends the attempt as :class:`BusUnavailable` and
the harness resubmits as a new task. The publish asks for no acknowledgement
-- one carrying a reply subject would get the stream's PubAck on the
principal's inbox, which its grants allow -- and learns that the server took
the frame from the flush that follows it; a stream that is missing surfaces as
a task nobody accepted.

``nats`` is imported lazily inside the client so the default ``api`` transport
never loads it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "ARTIFACT_ACTIVITY",
    "ARTIFACT_PROGRESS",
    "ARTIFACT_RESULT",
    "ARTIFACT_THINKING",
    "DEFAULT_ADDRESSEE",
    "EVENT_ENTRY_ARTIFACT",
    "EVENT_ENTRY_STATUS",
    "NATS_USER",
    "OUTCOME_DEADLINE",
    "OUTCOME_NOT_ACCEPTED",
    "OUTCOME_TERMINAL",
    "STATE_COMPLETED",
    "TERMINAL_STATES",
    "BusClient",
    "BusUnavailable",
    "Exchange",
    "Fold",
    "TaskIds",
    "build_envelope",
    "cancel_payload",
    "message_payload",
    "mint_ids",
    "task_events_subject",
    "task_in_subject",
    "task_supervisor_subject",
]

_log = logging.getLogger("kube_agents_bench.a2a_transport")

# The wire protocol this transport speaks; ``a2a/lib/envelope.go`` emits exactly
# this string and consumers refuse any other major.
PROTOCOL = "a2a-jetstream/0.4"

# The executor a submission is addressed to when ``AGENT_A2A_ADDRESSEE`` is
# unset: the Hermes bridge's profile, the one executor for ``platform`` today.
# It is also the only addressee the ``eval`` principal's grants reach.
DEFAULT_ADDRESSEE = "platform"

# The bus principal the harness connects as: the operator's ``eval`` identity
# (``platformagent_a2a_identities.go``), never the gateway's. Its inbox prefix
# is pinned by its own grant; the client default ``_INBOX.<nuid>`` would be
# refused if anything ever made a request.
NATS_USER = "eval"
INBOX_PREFIX = "_INBOX.eval"
CLIENT_NAME = "kube-agents-bench"

# ``from`` on every envelope this transport publishes. Display only -- nothing
# on the bus decides on it -- but it must not name the addressee, or the
# envelope-subject agreement check refuses the submission as an executor
# writing its own ``in`` subject.
FROM_PARTY: dict[str, str] = {
    "session": "devops-bench",
    "agentType": "kube-agents-bench",
}

# Random-id widths in bytes, matching the gateway's ``randHex`` widths so a task
# this harness started reads like one the gateway started.
TASK_ID_BYTES = 8
CONTEXT_ID_BYTES = 12
CORRELATION_ID_BYTES = 12
MESSAGE_ID_BYTES = 8
ENVELOPE_ID_BYTES = 11

KIND_MESSAGE = "message"
KIND_CANCEL = "cancel"
KIND_STATUS_UPDATE = "status-update"
KIND_ARTIFACT_UPDATE = "artifact-update"

STATE_COMPLETED = "completed"
TERMINAL_STATES = frozenset({STATE_COMPLETED, "failed", "canceled", "rejected"})

# The reserved artifact names (``a2a/lib/payload.go``).
ARTIFACT_RESULT = "result"
ARTIFACT_THINKING = "thinking"
ARTIFACT_ACTIVITY = "activity"
ARTIFACT_PROGRESS = "progress"

# Trajectory entry names for the task's own lifecycle events. The bus carries
# no tool calls until an executor publishes ``activity`` artifacts, so the
# events are what this transport can truthfully record as the run's
# trajectory; ``scoring.py`` imports the first as the record's liveness
# signal (``nats`` is imported inside the session, so the scorer does not
# load it).
EVENT_ENTRY_STATUS = "a2a.status-update"
EVENT_ENTRY_ARTIFACT = "a2a.artifact-update"
# The ``status`` a trajectory entry carries once its call is over; every entry
# this transport records is written after the fact, so it is the only value.
ENTRY_STATUS_DONE = "completed"

# Outcomes of one await.
OUTCOME_TERMINAL = "terminal"
OUTCOME_NOT_ACCEPTED = "not-accepted"
OUTCOME_DEADLINE = "deadline"

# Bounds on the bus client. The dial covers TCP plus auth on a local
# port-forward; the flush is one PING/PONG round trip, which is how a core
# publish or subscribe learns the server took it (or refused it: a permissions
# violation is an asynchronous error frame that arrives before the PONG); the
# queue poll is how often the wait loop re-checks its deadlines while nothing
# arrives.
CONNECT_TIMEOUT_SECONDS = 10.0
FLUSH_TIMEOUT_SECONDS = 10.0
QUEUE_POLL_SECONDS = 1.0

# The server's error frames carry these texts and nothing structured, and
# their case differs between the -ERR frame and the text nats-py wraps it in
# (measured: "Authorization Violation" but "permissions violation for
# publish to ..."), so both are matched case-insensitively. A refused
# credential cannot be fixed by dialling again; a permissions violation says
# the principal's grants do not cover the subject, which no retry changes
# either.
_AUTH_REFUSED_TEXT = "authorization violation"
_PERMISSIONS_TEXT = "permissions violation"

# JetStream dedup header: the envelope id, as ``lib.Client.Publish`` sets it.
# The stream honours it on a core publish too.
_MSG_ID_HEADER = "Nats-Msg-Id"


def task_in_subject(addressee: str, task_id: str) -> str:
    return f"a2a.tasks.{addressee}.{task_id}.in"


def task_events_subject(addressee: str, task_id: str) -> str:
    return f"a2a.tasks.{addressee}.{task_id}.events"


def task_supervisor_subject(addressee: str, task_id: str) -> str:
    """Where the task's supervisor writes the terminal an executor died without."""
    return f"a2a.tasks.{addressee}.{task_id}.supervisor"


@dataclass(frozen=True)
class TaskIds:
    """The three identifiers a submission carries."""

    task_id: str
    context_id: str
    correlation_id: str


def mint_ids(*, context_id: str | None = None, correlation_id: str | None = None) -> TaskIds:
    """Mint a task id, and a context and correlation id unless given.

    A follow-up on the same conversation keeps the context id; a follow-up in
    service of the same user interaction keeps the correlation id too (the
    payload spec's field rule: minted once, copied on every hop).
    """
    return TaskIds(
        task_id="task-" + secrets.token_hex(TASK_ID_BYTES),
        context_id=context_id or "ctx-" + secrets.token_hex(CONTEXT_ID_BYTES),
        correlation_id=correlation_id or "corr-" + secrets.token_hex(CORRELATION_ID_BYTES),
    )


def _now_rfc3339() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def build_envelope(kind: str, ids: TaskIds, payload: dict[str, Any], *, to: str) -> dict[str, Any]:
    """One envelope, in the field order the spec's example uses.

    ``identity`` is reserved and never populated by a client. ``authority`` is
    null: it is the gateway's advisory block, populated where the gateway
    admits a principal, and this transport has nothing to assert there and
    invents no shape. ``to`` names the addressee, which the subject-agreement
    check requires of a submission.
    """
    return {
        "protocol": PROTOCOL,
        "envelopeId": "env-" + secrets.token_hex(ENVELOPE_ID_BYTES),
        "correlationId": ids.correlation_id,
        "taskId": ids.task_id,
        "contextId": ids.context_id,
        "ts": _now_rfc3339(),
        "from": dict(FROM_PARTY),
        "to": {"session": to},
        "identity": None,
        "authority": None,
        "kind": kind,
        "payload": payload,
    }


def message_payload(text: str, ids: TaskIds) -> dict[str, Any]:
    """The A2A Message a submission carries: one user text part."""
    return {
        "role": "user",
        "parts": [{"kind": "text", "text": text}],
        "messageId": "msg-" + secrets.token_hex(MESSAGE_ID_BYTES),
        "taskId": ids.task_id,
        "contextId": ids.context_id,
    }


def cancel_payload() -> dict[str, Any]:
    """A cancel carries the empty object; the envelope's taskId names the target."""
    return {}


def _text_of(parts: Any) -> str:
    """Concatenate the text parts of an A2A part list."""
    if not isinstance(parts, list):
        return ""
    return "".join(
        str(p.get("text") or "") for p in parts if isinstance(p, dict) and p.get("kind") == "text"
    )


@dataclass
class Fold:
    """A task materialised from its events, as ``lib.FoldTask`` does it.

    Events after the terminal one are dropped and counted; events that do not
    parse or name another task are counted as malformed and skipped, the way
    the library's replay skips a poison write rather than failing the fold.
    """

    task_id: str
    state: str = ""
    final: bool = False
    history: list[str] = field(default_factory=list)
    status_message: str = ""
    artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    trajectory: list[dict[str, Any]] = field(default_factory=list)
    post_final_dropped: int = 0
    malformed: int = 0

    @property
    def accepted(self) -> bool:
        """Whether any executor has published an event for the task."""
        return bool(self.history) or bool(self.artifacts)

    def artifact_text(self, name: str) -> str:
        """The concatenated text parts of the artifact called ``name``."""
        for artifact in self.artifacts.values():
            if artifact.get("name") == name:
                return _text_of(artifact.get("parts"))
        return ""

    def artifact_names(self) -> list[str]:
        return [str(a.get("name") or a.get("artifactId") or "") for a in self.artifacts.values()]

    def apply(self, env: Any) -> None:
        """Fold one envelope in stream order."""
        if not isinstance(env, dict) or env.get("taskId") != self.task_id:
            self.malformed += 1
            return
        if self.final:
            self.post_final_dropped += 1
            return
        kind = env.get("kind")
        payload = env.get("payload")
        if not isinstance(payload, dict):
            self.malformed += 1
            return
        if kind == KIND_STATUS_UPDATE:
            status = payload.get("status")
            state = status.get("state") if isinstance(status, dict) else None
            if not isinstance(state, str) or not state:
                self.malformed += 1
                return
            message = status.get("message") if isinstance(status, dict) else None
            text = _text_of(message.get("parts")) if isinstance(message, dict) else ""
            self.state = state
            self.final = bool(payload.get("final"))
            self.history.append(state)
            if text:
                self.status_message = text
            self.trajectory.append(
                {
                    "name": EVENT_ENTRY_STATUS,
                    "args": {"state": state, "final": self.final},
                    "result": text or None,
                    "status": ENTRY_STATUS_DONE,
                }
            )
        elif kind == KIND_ARTIFACT_UPDATE:
            artifact = payload.get("artifact")
            if not isinstance(artifact, dict) or not isinstance(artifact.get("parts"), list):
                self.malformed += 1
                return
            self._merge_artifact(artifact, append=bool(payload.get("append")))
        else:
            self.malformed += 1

    def _merge_artifact(self, artifact: dict[str, Any], *, append: bool) -> None:
        key = str(artifact.get("artifactId") or artifact.get("name") or "")
        parts = list(artifact.get("parts") or [])
        name = str(artifact.get("name") or "")
        current = self.artifacts.get(key)
        if current is not None and append:
            current["parts"] = list(current.get("parts") or []) + parts
        else:
            self.artifacts[key] = {
                "artifactId": artifact.get("artifactId"),
                "name": name,
                "parts": parts,
            }
        if name == ARTIFACT_ACTIVITY:
            # The structured tool-call trace: each data part that names a tool
            # is recorded as a canonical trajectory entry, so the tool-call
            # verifiers and the delegation seam read it as they read a call
            # the HTTP endpoint replayed.
            for part in parts:
                data = part.get("data") if isinstance(part, dict) else None
                if isinstance(data, dict) and isinstance(data.get("name"), str):
                    args = data.get("args")
                    if not isinstance(args, dict):
                        raw_args = data.get("arguments")
                        args = raw_args if isinstance(raw_args, dict) else {}
                    self.trajectory.append(
                        {
                            "name": data["name"],
                            "args": args,
                            "result": data.get("result"),
                            "status": str(data.get("status") or ENTRY_STATUS_DONE),
                        }
                    )
            return
        # The deliverable lives in ``output``; the trajectory entry only records
        # that it arrived. Progress and thinking are text the reader may want
        # beside the tool calls, so their text rides on the entry.
        self.trajectory.append(
            {
                "name": EVENT_ENTRY_ARTIFACT,
                "args": {"artifact": name, "append": append, "parts": len(parts)},
                "result": (
                    _text_of(parts) if name in (ARTIFACT_PROGRESS, ARTIFACT_THINKING) else None
                ),
                "status": ENTRY_STATUS_DONE,
            }
        )


class BusUnavailable(RuntimeError):
    """The attempt never reached the bus, or lost it: NATS unreachable, the
    credential refused, a subject the principal's grants do not cover, a
    connection that dropped before the terminal. Never an executor's answer.

    ``retryable`` says whether a fresh attempt through a fresh tunnel could
    plausibly succeed; a refused credential or a refused subject cannot.
    ``submitted`` says whether the submission's frame had left for the server
    when the attempt failed -- taken, or possibly taken, since a flush that
    fails after the frame was written cannot tell -- in which case an executor
    may be working on a task nobody is awaiting and someone owes it a cancel:
    the next attempt, or the caller on its way out when there is none.
    ``cancelled``
    names the tasks this attempt did publish a cancel for before it failed,
    so the caller does not cancel them again.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        submitted: bool = False,
        cancelled: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.submitted = submitted
        self.cancelled: tuple[str, ...] = tuple(cancelled)


@dataclass
class Exchange:
    """What one await produced: the folded task and why the wait ended."""

    fold: Fold
    outcome: str
    events: list[dict[str, Any]]
    #: The ``cancel_first`` task ids whose cancel the server took on the way in.
    cancelled: tuple[str, ...] = ()


class _Session:
    """One connection to the bus, as the ``eval`` principal, on one event loop.

    The public :class:`BusClient` opens one per operation; the pieces are
    separate so that "subscribe, then publish, then await" is the order the
    code reads in, and so awaiting a task id needs nothing but a subscription.
    """

    def __init__(self, *, url: str, password: str, addressee: str, user: str) -> None:
        self.url = url
        self.password = password
        self.addressee = addressee
        self.user = user
        self.nc: Any = None
        # The server's own error frames arrive through error_cb, never as the
        # return of the call that provoked them: a refused credential during
        # the dial, a permissions violation after a SUB or PUB. Kept so the
        # failure can be named and told apart from a bus that is merely down.
        self.server_errors: list[str] = []
        self._queues: dict[str, asyncio.Queue[bytes]] = {}
        self._subs: list[Any] = []

    async def open(self) -> None:
        import nats
        import nats.errors

        async def _on_error(exc: Exception) -> None:
            self.server_errors.append(str(exc))
            _log.warning("a2a: bus client error: %s", exc)

        try:
            # No reconnect, deliberately: a core subscription cannot replay
            # what a reconnect gap dropped, so a connection that goes away is
            # this attempt over, and the harness decides whether to resubmit.
            # It also keeps a refused credential from turning into a dial
            # loop that never returns (observed with the library default).
            self.nc = await nats.connect(
                self.url,
                user=self.user,
                password=self.password,
                name=CLIENT_NAME,
                inbox_prefix=INBOX_PREFIX,
                connect_timeout=CONNECT_TIMEOUT_SECONDS,
                allow_reconnect=False,
                error_cb=_on_error,
            )
        except (TimeoutError, nats.errors.Error, OSError) as exc:
            refused = any(_AUTH_REFUSED_TEXT in e.lower() for e in [str(exc), *self.server_errors])
            detail = f"{exc}" + (
                f" (server said: {self.server_errors[-1]})" if self.server_errors else ""
            )
            raise BusUnavailable(
                f"connect to {self.url} as {self.user!r} failed: {detail}",
                retryable=not refused,
            ) from exc

    async def _flush(self, what: str) -> None:
        """One round trip, so the server has taken ``what`` -- or refused it."""
        import nats.errors

        seen = len(self.server_errors)
        try:
            await self.nc.flush(timeout=FLUSH_TIMEOUT_SECONDS)
        except (TimeoutError, nats.errors.Error) as exc:
            raise BusUnavailable(f"{what} was not taken by {self.url}: {exc}") from exc
        refused = [e for e in self.server_errors[seen:] if _PERMISSIONS_TEXT in e.lower()]
        if refused:
            raise BusUnavailable(
                f"{what} refused for {self.user!r}: {refused[-1]}", retryable=False
            )

    async def watch(self, task_id: str) -> None:
        """Subscribe to ``task_id``'s events and supervisor subjects, before
        anything is published.

        The pair is what ``lib.TaskReplaySubjects`` folds: the executor's own
        events, and the terminal its supervisor writes if the executor dies
        without one. One connection delivers both in publish order into one
        queue, and the fold drops whatever follows the first terminal.
        """
        import nats.errors

        subjects = (
            task_events_subject(self.addressee, task_id),
            task_supervisor_subject(self.addressee, task_id),
        )
        queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._queues[task_id] = queue

        async def _on_message(msg: Any) -> None:
            await queue.put(msg.data)

        for subject in subjects:
            try:
                self._subs.append(await self.nc.subscribe(subject, cb=_on_message))
            except (TimeoutError, nats.errors.Error) as exc:
                raise BusUnavailable(f"subscribe to {subject} failed: {exc}") from exc
        await self._flush(f"subscriptions to {' and '.join(subjects)}")

    async def publish(self, envelope: dict[str, Any]) -> None:
        """Publish one envelope on the addressee's ``in`` subject and flush."""
        import nats.errors

        subject = task_in_subject(self.addressee, str(envelope["taskId"]))
        try:
            await self.nc.publish(
                subject,
                json.dumps(envelope).encode("utf-8"),
                headers={_MSG_ID_HEADER: str(envelope["envelopeId"])},
            )
        except (TimeoutError, nats.errors.Error) as exc:
            raise BusUnavailable(f"publish on {subject} failed: {exc}") from exc
        try:
            await self._flush(f"{envelope['kind']} on {subject}")
        except BusUnavailable as exc:
            # The frame left this side before the round trip failed, so the
            # server may have taken it. A submission it took is the caller's
            # to cancel and record; a cancel it took twice costs nothing.
            exc.submitted = True
            raise

    async def submit(self, ids: TaskIds, prompt: str) -> None:
        await self.publish(
            build_envelope(KIND_MESSAGE, ids, message_payload(prompt, ids), to=self.addressee)
        )
        _log.info(
            "a2a: submitted task %s to %s (correlation %s)",
            ids.task_id,
            task_in_subject(self.addressee, ids.task_id),
            ids.correlation_id,
        )

    async def cancel(self, ids: TaskIds, why: str) -> bool:
        """Tell the executor the task is abandoned from this side. Best effort:
        True when the server took the cancel."""
        try:
            await self.publish(
                build_envelope(KIND_CANCEL, ids, cancel_payload(), to=self.addressee)
            )
        except BusUnavailable as exc:
            _log.warning("a2a: cancel for task %s was not taken: %s", ids.task_id, exc)
            return False
        _log.info("a2a: published cancel for task %s (%s)", ids.task_id, why)
        return True

    async def await_terminal(
        self, task_id: str, *, accept_timeout: float, deadline: float
    ) -> Exchange:
        """Fold ``task_id``'s events until its terminal, or a bound.

        Args:
            accept_timeout: Seconds an unaccepted task waits for a first event
                before the wait ends ``OUTCOME_NOT_ACCEPTED``.
            deadline: ``time.monotonic()`` instant after which the wait ends
                ``OUTCOME_DEADLINE``.

        Raises:
            BusUnavailable: The connection closed before a terminal arrived.
        """
        queue = self._queues.get(task_id)
        if queue is None:
            await self.watch(task_id)
            queue = self._queues[task_id]
        fold = Fold(task_id)
        events: list[dict[str, Any]] = []
        started = time.monotonic()
        outcome = OUTCOME_TERMINAL
        while True:
            now = time.monotonic()
            if fold.final:
                break
            if not fold.accepted and now - started > accept_timeout:
                outcome = OUTCOME_NOT_ACCEPTED
                break
            if now >= deadline:
                outcome = OUTCOME_DEADLINE
                break
            if self.nc.is_closed:
                raise BusUnavailable(
                    f"bus connection to {self.url} closed while awaiting task {task_id}"
                    + (f" (server said: {self.server_errors[-1]})" if self.server_errors else "")
                )
            try:
                raw = await asyncio.wait_for(queue.get(), QUEUE_POLL_SECONDS)
            except TimeoutError:
                continue
            try:
                env = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                fold.malformed += 1
                continue
            if isinstance(env, dict):
                events.append(env)
            fold.apply(env)
        return Exchange(fold=fold, outcome=outcome, events=events)

    async def close(self) -> None:
        import nats.errors

        if self.nc is None:
            return
        for sub in self._subs:
            try:
                await sub.unsubscribe()
            except (TimeoutError, nats.errors.Error):
                pass
        try:
            await self.nc.close()
        except (TimeoutError, nats.errors.Error, OSError):
            pass


class BusClient:
    """The ``eval`` principal's two operations against one bus URL.

    Each call opens its own connection on a private event loop and closes it
    before returning, so a caller that retries through a fresh tunnel needs
    nothing reset here.
    """

    def __init__(self, *, url: str, password: str, addressee: str, user: str = NATS_USER) -> None:
        self.url = url
        self.password = password
        self.addressee = addressee
        self.user = user

    def _session(self) -> _Session:
        return _Session(
            url=self.url, password=self.password, addressee=self.addressee, user=self.user
        )

    def submit_and_await(
        self,
        ids: TaskIds,
        prompt: str,
        *,
        accept_timeout: float,
        deadline: float,
        cancel_first: Sequence[TaskIds] = (),
    ) -> Exchange:
        """Publish ``prompt`` as task ``ids`` and await that task's terminal.

        Subscribes before it publishes, so an executor's first event cannot
        race the subscription. ``cancel_first`` names tasks an earlier attempt
        abandoned when its connection dropped; each gets a cancel before the
        new submission, so an executor is not left working for nobody, and the
        ones the server took are named on the exchange, or on the exception
        when this attempt fails too. A wait that ends on a bound rather than a
        terminal publishes a cancel for ``ids`` too.

        Raises:
            BusUnavailable: The bus could not be reached, refused the
                principal or a subject, or dropped the connection.
        """

        async def _run() -> Exchange:
            session = self._session()
            await session.open()
            submitted = False
            cancelled: list[str] = []
            try:
                for stale in cancel_first:
                    if await session.cancel(stale, "abandoned by an earlier attempt"):
                        cancelled.append(stale.task_id)
                await session.watch(ids.task_id)
                await session.submit(ids, prompt)
                submitted = True
                exchange = await session.await_terminal(
                    ids.task_id, accept_timeout=accept_timeout, deadline=deadline
                )
                if exchange.outcome != OUTCOME_TERMINAL:
                    await session.cancel(ids, exchange.outcome)
                exchange.cancelled = tuple(cancelled)
                return exchange
            except BusUnavailable as exc:
                exc.submitted = submitted or exc.submitted
                exc.cancelled = tuple(cancelled)
                raise
            finally:
                await session.close()

        return asyncio.run(_run())

    def cancel(self, tasks: Sequence[TaskIds], why: str) -> tuple[str, ...]:
        """Publish a cancel for each of ``tasks`` on a connection of its own.

        For the tasks a retry abandoned when no further attempt will carry
        their cancel: the last attempt was the one that dropped, or the
        failure is not worth retrying. Best effort: returns the ids the server
        took a cancel for, and an unreachable bus returns none.
        """

        async def _run() -> tuple[str, ...]:
            session = self._session()
            try:
                await session.open()
            except BusUnavailable as exc:
                _log.warning(
                    "a2a: no connection to cancel %d abandoned task(s): %s", len(tasks), exc
                )
                return ()
            taken: list[str] = []
            try:
                for ids in tasks:
                    if await session.cancel(ids, why):
                        taken.append(ids.task_id)
            finally:
                await session.close()
            return tuple(taken)

        return asyncio.run(_run())

    def await_terminal(self, task_id: str, *, accept_timeout: float, deadline: float) -> Exchange:
        """Await the terminal of ``task_id``, whoever submitted it.

        Sees the events published after it subscribes -- the ``eval`` principal
        has no replay -- so it is for a task that is still running: one the
        gateway started, or a child a parent's events named. It owns the task
        no more than it submitted it, so it never cancels.
        """

        async def _run() -> Exchange:
            session = self._session()
            await session.open()
            try:
                return await session.await_terminal(
                    task_id, accept_timeout=accept_timeout, deadline=deadline
                )
            finally:
                await session.close()

        return asyncio.run(_run())
