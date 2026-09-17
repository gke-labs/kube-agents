"""The A2A bus transport behind ``AGENT_TRANSPORT=a2a``.

The harness hands a case's prompt to the agent the way the gateway hands a
chat message to it under ``spec.mode: next``: one ``message`` envelope on
``a2a.tasks.{addressee}.{taskId}.in``, then the task's events folded until a
terminal ``status-update`` lands. Envelope, subject and payload shapes are
``docs/designs/spec-a2a-payloads.md``; the fold mirrors ``a2a/lib/fold.go``;
the ids are minted the way ``a2a/gateway/gateway.go`` mints them. Nothing here
polls the model.

This module is the bus half: envelopes, the fold, and one submit-and-await
exchange over ``nats-py``. Environment, port-forwards, retry classes and the
mapping onto ``AgentResult`` stay in :mod:`kube_agents_bench.harness`, which is
the only importer. ``nats`` is imported lazily inside the exchange so the
default ``api`` transport never loads it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
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
    "OUTCOME_DEADLINE",
    "OUTCOME_NOT_ACCEPTED",
    "OUTCOME_TERMINAL",
    "STATE_COMPLETED",
    "TERMINAL_STATES",
    "BusTask",
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

# The JetStream stream holding ``a2a.tasks.>``, provisioned by the operator's
# provision Job (``lib.TasksStream``).
TASKS_STREAM = "TASKS"

# The executor a submission is addressed to when ``AGENT_A2A_ADDRESSEE`` is
# unset: the Hermes bridge's profile, the one executor for ``platform`` today.
DEFAULT_ADDRESSEE = "platform"

# The bus principal the harness connects as. The gateway's static user is the
# one whose grants fit a task requester -- publish on ``a2a.tasks.*.*.in``,
# subscribe on ``…events`` and ``…supervisor``, and the JetStream API -- and
# its inbox prefix is pinned by the same grant (``_INBOX.gateway.>``): the
# client default ``_INBOX.<nuid>`` would be refused and every API call would
# time out instead of failing.
NATS_USER = "gateway"
INBOX_PREFIX = "_INBOX.gateway"
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
# trajectory; ``scoring.py`` reads the terminal one as the record's liveness
# signal and duplicates the first literal for that (importing this module
# would drag ``nats`` into the scorer). Change it in both files or in neither.
EVENT_ENTRY_STATUS = "a2a.status-update"
EVENT_ENTRY_ARTIFACT = "a2a.artifact-update"

# Outcomes of one exchange.
OUTCOME_TERMINAL = "terminal"
OUTCOME_NOT_ACCEPTED = "not-accepted"
OUTCOME_DEADLINE = "deadline"

# Bounds on the bus client. The dial covers TCP plus auth on a local
# port-forward; the API timeout covers one JetStream request (stream info,
# consumer create, publish ack); the heartbeat is what lets the ordered
# consumer notice a gap and re-create itself; the queue poll is how often the
# wait loop re-checks its deadlines while nothing arrives; the disconnect grace
# is how long a dropped connection may stay dropped before the attempt is
# abandoned to the harness's retry, which respawns the tunnel underneath.
CONNECT_TIMEOUT_SECONDS = 10.0
API_TIMEOUT_SECONDS = 10.0
IDLE_HEARTBEAT_SECONDS = 5.0
QUEUE_POLL_SECONDS = 1.0
DISCONNECT_GRACE_SECONDS = 30.0
# How many times nats-py redials on its own, and how long it waits between
# dials, before it gives the connection up as closed. Bounded, because the
# library's default of unbounded redials turns a refused credential into a
# connect() that never returns (observed: one "Authorization Violation" every
# two seconds, forever); the harness's own retry -- through a fresh tunnel --
# takes over once the library gives up.
RECONNECT_ATTEMPTS = 5
RECONNECT_WAIT_SECONDS = 1.0

# Errors nats-py raises for a refused credential carry this text and nothing
# structured; retrying the same password cannot change the answer.
_AUTH_REFUSED_TEXT = "Authorization Violation"

# JetStream dedup header: the envelope id, as ``lib.Client.Publish`` sets it.
_MSG_ID_HEADER = "Nats-Msg-Id"


def task_in_subject(addressee: str, task_id: str) -> str:
    return f"a2a.tasks.{addressee}.{task_id}.in"


def task_events_subject(addressee: str, task_id: str) -> str:
    return f"a2a.tasks.{addressee}.{task_id}.events"


def task_supervisor_subject(addressee: str, task_id: str) -> str:
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


def build_envelope(
    kind: str,
    ids: TaskIds,
    payload: dict[str, Any],
    *,
    to: str | None = None,
    authority: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One envelope, in the field order the spec's example uses.

    ``identity`` is reserved and never populated by a client; ``authority`` is
    the gateway's advisory block and null from anyone else, so it is null here
    unless a caller has one to forward.
    """
    env: dict[str, Any] = {
        "protocol": PROTOCOL,
        "envelopeId": "env-" + secrets.token_hex(ENVELOPE_ID_BYTES),
        "correlationId": ids.correlation_id,
        "taskId": ids.task_id,
        "contextId": ids.context_id,
        "ts": _now_rfc3339(),
        "from": dict(FROM_PARTY),
        "identity": None,
        "authority": authority,
        "kind": kind,
        "payload": payload,
    }
    if to is not None:
        env["to"] = {"session": to}
    return env


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
                    "status": "completed",
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
                            "status": str(data.get("status") or "completed"),
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
                "status": "completed",
            }
        )


class BusUnavailable(RuntimeError):
    """The exchange never reached the bus, or lost it: NATS unreachable, the
    credential refused, no ``TASKS`` stream, a publish nobody acknowledged, a
    connection that stayed down. Never an executor's answer.

    ``retryable`` says whether the same exchange could plausibly succeed after
    the harness respawns the tunnel; a refused credential cannot.
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class Exchange:
    """What one exchange produced: the folded task and why the wait ended."""

    fold: Fold
    outcome: str
    events: list[dict[str, Any]]


class BusTask:
    """One task on the bus: submit once, await its terminal event.

    Holds the submission across attempts so a retry that respawned the tunnel
    re-subscribes and re-folds from the stream rather than publishing the
    prompt a second time. The ordered consumer replays from the first event
    on every subscribe, so each attempt's fold starts empty and is complete.
    """

    def __init__(
        self,
        *,
        url: str,
        password: str,
        addressee: str,
        ids: TaskIds,
        prompt: str,
        user: str = NATS_USER,
    ) -> None:
        self.url = url
        self.password = password
        self.user = user
        self.addressee = addressee
        self.ids = ids
        self.prompt = prompt
        self.submitted = False
        self.submission = build_envelope(
            KIND_MESSAGE, ids, message_payload(prompt, ids), to=addressee
        )

    def exchange(self, *, accept_timeout: float, deadline: float) -> Exchange:
        """Run one attempt to completion on a private event loop.

        Args:
            accept_timeout: Seconds an unaccepted task waits for a first event
                before the attempt ends ``OUTCOME_NOT_ACCEPTED``.
            deadline: ``time.monotonic()`` instant after which the attempt ends
                ``OUTCOME_DEADLINE``.

        Raises:
            BusUnavailable: The bus could not be reached or was lost.
        """
        return asyncio.run(self._run(accept_timeout=accept_timeout, deadline=deadline))

    async def _run(self, *, accept_timeout: float, deadline: float) -> Exchange:
        import nats
        import nats.errors
        import nats.js.errors
        from nats.js.api import ConsumerConfig, DeliverPolicy

        task_id = self.ids.task_id
        events_subject = task_events_subject(self.addressee, task_id)
        supervisor_subject = task_supervisor_subject(self.addressee, task_id)
        queue: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()
        disconnected_since: list[float] = []

        async def _on_message(msg: Any) -> None:
            await queue.put((msg.subject, msg.data))

        # The server's own error frames arrive through error_cb, not as the
        # exception connect() raises: a refused credential is reported there
        # on every attempt while connect() keeps dialling, and what finally
        # surfaces is a generic "no servers". The last frame is kept so the
        # failure can be named, and an authorization refusal can be told
        # apart from a bus that is merely down.
        server_errors: list[str] = []

        async def _on_disconnect() -> None:
            if not disconnected_since:
                disconnected_since.append(time.monotonic())
            _log.warning("a2a: bus connection dropped; nats-py is reconnecting")

        async def _on_reconnect() -> None:
            disconnected_since.clear()
            _log.info("a2a: bus connection restored")

        async def _on_error(exc: Exception) -> None:
            server_errors.append(str(exc))
            _log.warning("a2a: bus client error: %s", exc)

        try:
            nc = await nats.connect(
                self.url,
                user=self.user,
                password=self.password,
                name=CLIENT_NAME,
                inbox_prefix=INBOX_PREFIX,
                connect_timeout=CONNECT_TIMEOUT_SECONDS,
                allow_reconnect=True,
                max_reconnect_attempts=RECONNECT_ATTEMPTS,
                reconnect_time_wait=RECONNECT_WAIT_SECONDS,
                disconnected_cb=_on_disconnect,
                reconnected_cb=_on_reconnect,
                error_cb=_on_error,
            )
        except (TimeoutError, nats.errors.Error, OSError) as exc:
            refused = any(_AUTH_REFUSED_TEXT in e for e in [str(exc), *server_errors])
            detail = f"{exc}" + (f" (server said: {server_errors[-1]})" if server_errors else "")
            raise BusUnavailable(
                f"connect to {self.url} as {self.user!r} failed: {detail}",
                retryable=not refused,
            ) from exc

        try:
            js = nc.jetstream(timeout=API_TIMEOUT_SECONDS)
            try:
                await js.stream_info(TASKS_STREAM)
            except nats.js.errors.NotFoundError as exc:
                raise BusUnavailable(
                    f"stream {TASKS_STREAM} does not exist on {self.url}: the bus is not "
                    "provisioned (the provision Job has not completed)"
                ) from exc
            except (TimeoutError, nats.errors.Error) as exc:
                raise BusUnavailable(f"stream {TASKS_STREAM} lookup failed: {exc}") from exc

            # Subscribe before publishing: an executor's first event must not
            # be able to race the consumer. Deliver-all on an ordered consumer
            # also makes a re-subscribe on retry a replay of the whole task.
            try:
                sub = await js.subscribe(
                    events_subject,
                    stream=TASKS_STREAM,
                    cb=_on_message,
                    ordered_consumer=True,
                    idle_heartbeat=IDLE_HEARTBEAT_SECONDS,
                    config=ConsumerConfig(
                        filter_subjects=[events_subject, supervisor_subject],
                        deliver_policy=DeliverPolicy.ALL,
                    ),
                )
            except (TimeoutError, nats.errors.Error, nats.js.errors.Error) as exc:
                raise BusUnavailable(f"subscribe to {events_subject} failed: {exc}") from exc

            if not self.submitted:
                data = json.dumps(self.submission).encode("utf-8")
                try:
                    await js.publish(
                        task_in_subject(self.addressee, task_id),
                        data,
                        headers={_MSG_ID_HEADER: self.submission["envelopeId"]},
                        timeout=API_TIMEOUT_SECONDS,
                    )
                except (TimeoutError, nats.errors.Error, nats.js.errors.Error) as exc:
                    raise BusUnavailable(
                        f"publish of task {task_id} was not acknowledged: {exc}"
                    ) from exc
                self.submitted = True
                _log.info(
                    "a2a: submitted task %s to %s (correlation %s)",
                    task_id,
                    task_in_subject(self.addressee, task_id),
                    self.ids.correlation_id,
                )

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
                if nc.is_closed:
                    # nats-py spent its redials and gave the connection up;
                    # only a fresh tunnel and a fresh dial can continue.
                    raise BusUnavailable(
                        f"bus connection to {self.url} closed while awaiting task {task_id}"
                        + (f" (server said: {server_errors[-1]})" if server_errors else "")
                    )
                if disconnected_since and now - disconnected_since[0] > DISCONNECT_GRACE_SECONDS:
                    raise BusUnavailable(
                        f"bus connection to {self.url} stayed down for more than "
                        f"{DISCONNECT_GRACE_SECONDS:.0f}s while awaiting task {task_id}"
                    )
                try:
                    _, raw = await asyncio.wait_for(queue.get(), QUEUE_POLL_SECONDS)
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

            if outcome != OUTCOME_TERMINAL:
                # The task is abandoned from this side: say so on the bus
                # rather than leaving an executor working for nobody. Best
                # effort; the outcome is already decided.
                cancel = build_envelope(KIND_CANCEL, self.ids, cancel_payload(), to=self.addressee)
                try:
                    await js.publish(
                        task_in_subject(self.addressee, task_id),
                        json.dumps(cancel).encode("utf-8"),
                        headers={_MSG_ID_HEADER: cancel["envelopeId"]},
                        timeout=API_TIMEOUT_SECONDS,
                    )
                    _log.info("a2a: published cancel for task %s (%s)", task_id, outcome)
                except (TimeoutError, nats.errors.Error, nats.js.errors.Error) as exc:
                    _log.warning("a2a: cancel for task %s was not acknowledged: %s", task_id, exc)
            try:
                await sub.unsubscribe()
            except (TimeoutError, nats.errors.Error):
                pass
            return Exchange(fold=fold, outcome=outcome, events=events)
        finally:
            try:
                await nc.close()
            except (TimeoutError, nats.errors.Error, OSError):
                pass
