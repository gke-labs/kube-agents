"""``kubeagents`` agent harness: HTTP transport to the in-cluster platform agent.

The agent runs inside the cluster, so this harness only ensures the service is
reachable on a local port (lazily spawning ``kubectl port-forward``), POSTs the
prompt to its Responses-style endpoint, and parses the reply into an
``AgentResult``. No model SDK is imported; all inference happens in the cluster.

The platform agent delegates substantive work to subagents by filing a kanban
card and ending its turn -- there is no synchronous await tool, by design. Its
first reply is therefore an acknowledgement carrying a task id, not the answer.
Returning that would have the eval harness grade the acknowledgement and delete
the workspace while the subagent is still running, so a turn that files a card
is followed by status turns on the same conversation until every card reaches a
terminal state (see :meth:`KubeAgentsHarness._await_delegated_work`).

Registration is the ``devops_bench.agents`` entry point in ``pyproject.toml``,
so importing this module has no side effects.

Environment:
    AGENT_LOCAL_PORT: Local side of the port-forward (default ``8642``). The
        remote side is always the Service's 8642 and is not configurable.
    AGENT_API_PATH: Request path (default ``/v1/responses``).
    AGENT_SERVICE_NAME: Service to port-forward to (default ``platform-agent``).
    AGENT_NAMESPACE: Namespace of the service (default ``kubeagents-system``).
    AGENT_CLUSTER_CONTEXT: Optional kubectl context for the port-forward.
    AGENT_MODEL_NAME: ``model`` field sent to the endpoint (default ``hermes-agent``).
    AGENT_CONVERSATION_ID: Pins the ``conversation`` field. Unset (the default)
        generates a fresh id per invocation so each task's trajectory is
        isolated on this stateful endpoint.
    AGENT_HTTP_TIMEOUT: Per-request timeout in seconds (default ``600``).
    AGENT_DELEGATION_TIMEOUT: Total seconds to wait for delegated work across
        all status turns (default ``1800``). ``0`` disables waiting, restoring
        the single-turn behaviour.
    AGENT_DELEGATION_POLL_INTERVAL: Seconds between status turns (default ``30``).
    PLATFORM_AGENT_TOKEN: Bearer token for the endpoint.
"""

from __future__ import annotations

import atexit
import http.client
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from devops_bench.agents import AgentHarness, AgentResult, ToolCall
from devops_bench.agents.result import empty_tokens

__all__ = ["KubeAgentsHarness"]

_log = logging.getLogger("kube_agents_bench.harness")

SERVICE_API_PORT = 8642

_PF_LOCK = threading.Lock()  # guards the three registries below
_PF_PROCESSES: dict[int, subprocess.Popen[bytes]] = {}
_PF_PORT_LOCKS: dict[int, threading.Lock] = {}
_PF_LOG_DIR: Path | None = None


def _port_establishment_lock(port: int) -> threading.Lock:
    with _PF_LOCK:
        return _PF_PORT_LOCKS.setdefault(port, threading.Lock())


def _pf_log_dir() -> Path:
    global _PF_LOG_DIR
    with _PF_LOCK:
        if _PF_LOG_DIR is None:
            _PF_LOG_DIR = Path(tempfile.mkdtemp(prefix="kubeagents-pf-"))
        return _PF_LOG_DIR


def _tail(path: Path, max_bytes: int = 2048) -> str:
    """Last ``max_bytes`` of ``path``. Errors embed this rather than pointing at
    the file, which is deleted at process exit."""
    try:
        data = path.read_bytes()[-max_bytes:]
        return data.decode("utf-8", errors="replace").strip() or "(no output)"
    except OSError:
        return "(log unavailable)"


def _stop_process(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            sock.connect((host, port))
            return True
    except OSError:
        return False


@atexit.register
def _cleanup_port_forwards() -> None:
    """Terminate every port-forward this process spawned."""
    global _PF_LOG_DIR
    with _PF_LOCK:
        while _PF_PROCESSES:
            port, proc = _PF_PROCESSES.popitem()
            if proc.poll() is None:
                _log.info("terminating agent port-forward on port %d", port)
            _stop_process(proc)
        if _PF_LOG_DIR is not None:
            shutil.rmtree(_PF_LOG_DIR, ignore_errors=True)
            _PF_LOG_DIR = None


def _port_forward_command(local_port: int) -> list[str]:
    service = os.environ.get("AGENT_SERVICE_NAME", "platform-agent")
    cmd = [
        "kubectl",
        "port-forward",
        f"svc/{service}",
        f"{local_port}:{SERVICE_API_PORT}",
        "-n",
        os.environ.get("AGENT_NAMESPACE", "kubeagents-system"),
    ]
    context = os.environ.get("AGENT_CLUSTER_CONTEXT")
    if context:
        cmd.extend(["--context", context])
    return cmd


def _ensure_port_forward(local_port: int) -> None:
    """Start a background ``kubectl port-forward`` if the port is closed.

    An already-open port is a no-op: the harness never assumes it owns the
    transport. Serialised per port, so different ports establish in parallel.

    Raises:
        RuntimeError: The forward exited immediately, could not be spawned, or
            the port did not open in time.
    """
    with _port_establishment_lock(local_port):
        if _port_open(local_port):
            return

        with _PF_LOCK:
            stale = _PF_PROCESSES.pop(local_port, None)
        if stale is not None:
            _stop_process(stale)

        cmd = _port_forward_command(local_port)
        _log.info("port %d closed; establishing port-forward: %s", local_port, " ".join(cmd))
        stderr_log = _pf_log_dir() / f"pf-{local_port}.log"
        try:
            with open(stderr_log, "wb") as log_file:
                proc = subprocess.Popen(cmd, stdout=log_file, stderr=log_file)
        except OSError as exc:
            # A missing kubectl reaches _execute as a known error, not a crash.
            raise RuntimeError(f"failed to spawn kubectl port-forward: {exc}") from exc
        with _PF_LOCK:
            _PF_PROCESSES[local_port] = proc

        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError(
                        f"kubectl port-forward exited with {proc.returncode}: {_tail(stderr_log)}"
                    )
                if _port_open(local_port):
                    _log.info("port-forward established on port %d", local_port)
                    return
                time.sleep(0.5)
            raise RuntimeError(
                f"port-forward did not open port {local_port} in time: {_tail(stderr_log)}"
            )
        except BaseException:
            with _PF_LOCK:
                _PF_PROCESSES.pop(local_port, None)
            _stop_process(proc)
            raise


_TOOL_ERROR_PREFIX = "Error executing tool"


def _output_text(output: Any) -> str:
    """Flatten a ``function_call_output.output`` into text.

    The streaming builder wraps the same payload in text blocks; accept both so
    ``ToolCall.result`` does not depend on which builder served the request.
    """
    if isinstance(output, str):
        return output
    if output is None:
        return ""
    if isinstance(output, list):
        parts: list[str] = []
        for block in output:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return json.dumps(output, default=str)


# When a payload re-emits a call whose output matches a later call's byte for
# byte, hermes replaces the older copy's text with this notice instead of
# repeating it. Keeping such an entry costs twice: the real output is gone, and
# the notice is not what the previous turn recorded for that call, so the
# cumulative payload stops looking like a replay and the whole episode is
# appended to itself. Measured on run_20260806_161755_684221, where nine tool
# calls were graded as nineteen. The entry is redundant by the notice's own
# definition -- the same content appears further down the payload.
_ELIDED_OUTPUT = re.compile(r"^\[Duplicate tool output\b.*\]$", re.DOTALL)


def _output_failed(text: str) -> bool:
    """Return True for a *structured* hermes tool failure.

    Free text is never sniffed: tool output legitimately contains the word
    "error" (a test log, a grep hit).
    """
    if text.startswith(_TOOL_ERROR_PREFIX):
        return True
    try:
        data = json.loads(text.strip())
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    if data.get("success") is False or data.get("ok") is False:
        return True
    exit_code = data.get("exit_code", data.get("returncode"))
    if isinstance(exit_code, int) and exit_code != 0:
        return True
    # tool_error() / MCP isError: a bare error string with no result payload.
    # An error beside a real payload is a diagnostic, so every key the endpoint
    # uses to carry one counts -- MCP's `content`, and hermes' own `result` /
    # `structuredContent`.
    return bool(data.get("error")) and not (
        data.get("content") or data.get("result") or data.get("structuredContent")
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses every redirect, turning it into an ``HTTPError`` instead.

    urllib follows redirects by default and, unlike requests, does not strip
    ``Authorization`` on a cross-host hop -- so one ``302`` from whatever
    answers on the local port would hand the bearer token to another origin,
    defeating the ``AGENT_API_PATH`` check. A port-forward to a fixed local
    port has no legitimate reason to redirect.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)

_SESSION_ID_HEADER = "X-Hermes-Session-Id"

# The session lookup only refines accounting, so it never inherits the agent's
# (minutes-long) budget: a hung route would be billed as the agent's latency.
_SESSION_LOOKUP_TIMEOUT = 15.0

_SESSION_TOKEN_KEYS = (
    ("input", "input_tokens"),
    ("cached", "cache_read_tokens"),
    ("cache_write", "cache_write_tokens"),
    ("reasoning", "reasoning_tokens"),
    ("output", "output_tokens"),
)

_ENVELOPE_TOKEN_KEYS = (
    ("input", "input_tokens"),
    ("output", "output_tokens"),
    ("total", "total_tokens"),
)


def _canonical_session_tokens(
    tokens: dict[str, Any],
    session_id: str,
    local_port: int,
    headers: dict[str, str],
    timeout: float,
) -> None:
    """Replace the envelope's counts with the session row's canonical split.

    The envelope reports hermes' ``prompt_tokens`` (input + cache_read +
    cache_write), while ``TOKEN_BUCKETS`` defines ``input`` as the non-cached
    prompt alone -- so the row replaces the envelope wholesale rather than
    merging, and a partial row is discarded. Best effort: any failure leaves
    the envelope's counts in place.
    """
    quoted = urllib.parse.quote(session_id, safe="")
    probe = urllib.request.Request(
        f"http://127.0.0.1:{local_port}/api/sessions/{quoted}", headers=headers, method="GET"
    )
    try:
        with _OPENER.open(probe, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, http.client.HTTPException, ValueError) as exc:
        _log.debug("session token lookup failed for %s: %s", session_id, exc)
        return

    session = body.get("session") if isinstance(body, dict) else None
    if not isinstance(session, dict):
        return
    counts: dict[str, int] = {}
    for bucket, key in _SESSION_TOKEN_KEYS:
        value = session.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            return
        counts[bucket] = value
    tokens.update(counts)
    tokens["total"] = sum(counts.values())


def _envelope_tokens(payload: dict[str, Any]) -> dict[str, Any]:
    """Token buckets from the response envelope; an omitted one stays ``None``."""
    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    tokens = empty_tokens()
    for bucket, key in _ENVELOPE_TOKEN_KEYS:
        if usage.get(key) is not None:
            tokens[bucket] = usage[key]
    return tokens


def _call_args(raw: Any) -> dict[str, Any]:
    """Coerce ``function_call.arguments`` into the mapping ``ToolCall`` wants."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}
    if isinstance(raw, dict):
        return raw
    return {} if raw is None else {"raw": raw}


def _tool_name(raw: Any) -> str:
    """Coerce a reported tool name to a string.

    It becomes ``ToolCall.name`` and a key of ``metadata['tools']``, so an
    unhashable one would otherwise raise out of a parser that promises to
    degrade.
    """
    if isinstance(raw, str):
        return raw
    return "" if raw is None else str(raw)


def _message_text(part: dict[str, Any]) -> str:
    content = part.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(
        chunk["text"]
        for chunk in content
        if isinstance(chunk, dict)
        and chunk.get("type") == "output_text"
        and isinstance(chunk.get("text"), str)
    )


def _parse_response(payload: dict[str, Any]) -> AgentResult:
    """Map a Responses-style payload onto the canonical ``AgentResult``.

    A ``function_call`` whose ``call_id`` was already recorded in this payload
    is a replay of one invocation, not a second one, and is dropped. The
    endpoint re-emits earlier turns' items on a reused ``conversation`` and
    duplicates them as the turn count grows -- measured against a live agent,
    where a single ``kanban_create`` came back twice under one call_id on the
    third turn. Keeping both would show the judge a tool loop the agent never
    ran (``ToolInvocation`` scored a clean single delegation 0.40/FAIL on it).

    A ``function_call_output`` is folded *into* its originating call -- filling
    in ``result`` and ``status`` -- rather than appended as a second entry,
    which trajectory metrics would score as a redundant argument-less call. The
    fold is keyed on ``call_id``, falling back to the oldest unresolved call
    only when the id is absent: an id matching nothing is an orphan, not a
    licence to consume an unrelated call. A repeat output for an id already
    resolved rewrites its entry rather than orphaning, since the replayed text
    is byte-identical. An output the endpoint elided (:data:`_ELIDED_OUTPUT`)
    takes its whole entry with it. Outputs carry no status, so failure is read
    out of the payload (:func:`_output_failed`).

    Shape anomalies degrade rather than raise -- whatever was readable is kept,
    with the anomaly on ``errors``.
    """
    output_text = ""
    trajectory: list[ToolCall] = []
    tools_used: dict[str, int] = {}
    calls_by_id: dict[str, ToolCall] = {}
    unkeyed_calls: deque[ToolCall] = deque()
    elided: set[int] = set()
    parse_errors: list[str] = []

    items = payload.get("output")
    if not isinstance(items, list):
        if items is not None:
            parse_errors.append(f"'output' is not a list (got {type(items).__name__})")
        items = []

    for part in items:
        if not isinstance(part, dict):
            parse_errors.append(f"skipped a non-object output item ({type(part).__name__})")
            continue
        part_type = part.get("type")

        if part_type == "message" and part.get("role") == "assistant":
            output_text += _message_text(part)

        elif part_type == "function_call":
            call_id = part.get("call_id")
            if call_id and str(call_id) in calls_by_id:
                continue
            name = _tool_name(part.get("name"))
            entry = ToolCall(name=name, args=_call_args(part.get("arguments")))
            trajectory.append(entry)
            tools_used[name] = tools_used.get(name, 0) + 1
            if call_id:
                calls_by_id[str(call_id)] = entry
            else:
                unkeyed_calls.append(entry)

        elif part_type == "function_call_output":
            call_id = part.get("call_id")
            if call_id:
                target = calls_by_id.get(str(call_id))
            else:
                target = unkeyed_calls.popleft() if unkeyed_calls else None
            if target is None:
                parse_errors.append(f"tool output without a matching call (call_id={call_id!r})")
                target = ToolCall(name=_tool_name(part.get("name")), args={})
                trajectory.append(target)
            text = _output_text(part.get("output"))
            if _ELIDED_OUTPUT.match(text.strip()):
                elided.add(id(target))
                continue
            elided.discard(id(target))
            target.result = text
            target.status = "error" if _output_failed(text) else "completed"

    for entry in trajectory:
        if id(entry) in elided and tools_used.get(entry.name):
            tools_used[entry.name] -= 1
            if not tools_used[entry.name]:
                del tools_used[entry.name]
    trajectory = [entry for entry in trajectory if id(entry) not in elided]

    metadata: dict[str, Any] = {"tools": tools_used}
    for key in ("id", "status", "model"):
        if payload.get(key) is not None:
            metadata[f"response_{key}"] = payload[key]

    return AgentResult(
        output=output_text,
        trajectory=[entry.to_dict() for entry in trajectory],
        tokens=_envelope_tokens(payload),
        errors=parse_errors,
        metadata=metadata,
    )


_DELEGATION_TOOL = "kanban_create"
_STATUS_TOOL = "kanban_show"

# hermes' kanban_db.VALID_STATUSES is {triage, todo, ready, running, blocked,
# done, archived}. A card in one of these has stopped moving on its own: done /
# archived are finished, and blocked needs a human, so waiting it out would only
# burn the budget. The rest still have a worker or the dispatcher behind them.
_TERMINAL_STATUSES = frozenset({"done", "archived", "blocked"})

# ``kanban_show`` shares kanban_create's toolset and check_fn in hermes
# (tools/kanban_tools.py), so any profile that can file a card can also read one
# -- the status turn never needs a capability the delegating turn lacked.
_POLL_PROMPT = (
    "Do not start any new work. Call {tool} on each of these task ids and "
    "report their current status: {ids}. For every task that has finished, "
    "include its complete result in your reply."
)

# hermes mints ids as "t_" + 8 hex (kanban_db._new_task_id), but the id reaches
# us through the graded agent's own tool output, and it is then interpolated
# into the next prompt, the log, and the result record. Match a charset rather
# than that exact shape -- wide enough to survive an id-format change, narrow
# enough that no whitespace, newline, or instruction-shaped prose can ride in.
_TASK_ID_RE = re.compile(r"\A[A-Za-z0-9_.:-]{1,64}\Z")

# Ceiling on cards awaited at once. A card list grows the poll prompt, the log
# line and the stored trajectory on every one of the (timeout / interval) turns,
# so an agent looping on kanban_create would otherwise inflate the run record
# without bound. Far above any real fan-out.
_MAX_AWAITED_TASKS = 32

# Consecutive status turns that may report nothing before the wait is abandoned.
# One off-turn is cheap to absorb; a run of them means the agent will not read
# the board, and further turns would only burn the budget.
_MAX_SILENT_TURNS = 3

# Consecutive status turns that may fail in transport before the wait is
# abandoned. A delegation runs for minutes, and a live run against a healthy
# endpoint saw ``RemoteDisconnected`` on the first status turn: an idle
# keepalive dropped between turns, not a broken agent. Retrying costs one poll
# interval; abandoning costs the whole delegated result.
_MAX_TRANSPORT_FAILURES = 3


def _json_object(raw: Any) -> dict[str, Any]:
    """Parse a recorded tool result into a mapping; anything else yields ``{}``.

    Tool results reach us as text, and a failed hermes tool answers with a
    differently-shaped ``{"error": ...}`` payload, so every read here is
    defensive: an unparseable or unexpected result simply carries no signal.
    """
    if not isinstance(raw, str):
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _delegated_task_ids(trajectory: list[dict[str, Any]]) -> list[str]:
    """Card ids filed by ``kanban_create`` during a turn, in call order.

    Structural, not prose: the ids come out of the tool result
    (``{"ok": true, "task_id": ...}``) that ``_parse_response`` already folded
    onto the call. A failed create answers ``{"error": ...}`` with no
    ``task_id`` and is skipped, as is anything that does not look like an id
    (:data:`_TASK_ID_RE`) -- this text is echoed back to the agent and into the
    run record, so it is filtered at the point it enters the harness.
    """
    ids: list[str] = []
    for entry in trajectory:
        if entry.get("name") != _DELEGATION_TOOL:
            continue
        task_id = _json_object(entry.get("result")).get("task_id")
        if isinstance(task_id, str) and not _TASK_ID_RE.match(task_id):
            _log.warning("ignoring malformed task id from %s", _DELEGATION_TOOL)
            continue
        if isinstance(task_id, str) and task_id and task_id not in ids:
            ids.append(task_id)
    return ids


def _reported_statuses(trajectory: list[dict[str, Any]]) -> dict[str, str]:
    """Card statuses read out of any board-reading tool result in a turn.

    Keyed on payload shape, not tool name: ``kanban_show`` answers
    ``{"task": {...}}`` and ``kanban_list`` answers ``{"tasks": [...]}``
    (hermes ``_task_summary_dict``), both carrying ``id`` and ``status``. An
    agent asked for several cards may reasonably batch-read with the latter, so
    insisting on ``kanban_show`` would discard a perfectly good answer.

    A later reading of the same card wins, so a turn that shows one card twice
    reports the fresher state.
    """
    statuses: dict[str, str] = {}
    for entry in trajectory:
        payload = _json_object(entry.get("result"))
        listed = payload.get("tasks")
        found = [payload.get("task"), *(listed if isinstance(listed, list) else [])]
        for task in found:
            if not isinstance(task, dict):
                continue
            task_id, status = task.get("id"), task.get("status")
            if isinstance(task_id, str) and isinstance(status, str):
                statuses[task_id] = status
    return statuses


def _delivered_results(trajectory: list[dict[str, Any]], task_ids: list[str]) -> dict[str, str]:
    """Completion text each awaited card carries, keyed by card id.

    ``kanban_complete(summary=...)`` lands on the card's ``result`` field, and
    that is the delegated worker's actual answer -- the router's own closing
    message is a paraphrase of it. Reads the same ``kanban_show`` payloads
    :func:`_reported_statuses` does, so no extra turn is needed.
    """
    delivered: dict[str, str] = {}
    for entry in trajectory:
        payload = _json_object(entry.get("result"))
        listed = payload.get("tasks")
        for task in [payload.get("task"), *(listed if isinstance(listed, list) else [])]:
            if not isinstance(task, dict):
                continue
            task_id, text = task.get("id"), task.get("result")
            if task_id in task_ids and isinstance(text, str) and text.strip():
                delivered[task_id] = text
    return delivered


def _append_delivered(
    result: AgentResult, observed: list[dict[str, Any]], task_ids: list[str]
) -> None:
    """Append each finished card's own result to the text the judge grades.

    Without this the graded answer is only the router's closing chat message.
    The worker runs as a separate hermes session, so its reasoning and tool
    calls are invisible here; its card ``result`` is the one part of its work
    that crosses back, and dropping it costs marks for content the run did
    produce (measured: OutcomeValidity 0.60 on a report whose findings were
    all present on the card).

    ``observed`` is the status turns' trajectory, which is deliberately not
    ``result.trajectory``: the polls are the harness's, so they inform the
    answer without being graded as the agent's tool use.
    """
    # A router that already quoted the card verbatim needs no appendix; adding
    # one would show the judge the same finding twice.
    sections = [
        f"Result of delegated task {tid}:\n{text}"
        for tid, text in _delivered_results(observed, task_ids).items()
        if text.strip() not in result.output
    ]
    if sections:
        result.output = "\n\n".join(filter(None, [result.output, *sections]))


def _sum_tokens(base: dict[str, Any], extra: dict[str, Any]) -> None:
    """Add ``extra``'s token buckets into ``base`` in place.

    ``None`` means "the endpoint did not report this bucket", which is not the
    same as zero: it only becomes a number once some turn reports one.
    """
    for bucket, value in extra.items():
        if value is None:
            continue
        current = base.get(bucket)
        base[bucket] = value if current is None else current + value


def _replays(base: list[dict[str, Any]], turn: list[dict[str, Any]]) -> bool:
    """Whether ``turn`` re-sends what we already have rather than only new work.

    This endpoint is stateful and, on a reused ``conversation`` id, returns the
    whole conversation's output items in every response -- measured on a live
    Platform Agent in 373b453, where the fixed default id made each task
    inherit its predecessors' trajectories. So a status turn's payload normally
    *contains* the delegating turn's ``kanban_create``, and concatenating would
    re-create the duplicated, argument-less trajectory entries that commit
    removed (the ToolInvocation judge scored a correct single invocation
    0.60/FAIL on them).

    Detected rather than assumed: a cumulative payload starts with everything
    already recorded, so a prefix match means "replace" and anything else means
    "append". A build that returns only the newest items still folds correctly.
    """
    return len(turn) >= len(base) and turn[: len(base)] == base


def _merge_new(base: list[dict[str, Any]], turn: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The entries of ``turn`` that ``base`` does not already hold, in order.

    :func:`_replays` only recognises a cumulative payload that lines up as an
    exact prefix. A long wait breaks that: once the conversation is compacted
    the endpoint re-emits the earlier calls interleaved differently, so the
    prefix test fails and every already-recorded call is appended a second
    time -- which is what made the ToolInvocation judge read a clean run as a
    repetitive tool loop. Replays are byte-identical, so identity on the whole
    entry is a sound key, and it survives the call ids being reassigned.

    A genuinely repeated call whose arguments *and* result are identical
    collapses into one entry. That only happens for the harness's own status
    polls, which are not agent decisions and should not be graded as such.
    """
    seen = {json.dumps(entry, sort_keys=True, default=str) for entry in base}
    fresh = []
    for entry in turn:
        key = json.dumps(entry, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        fresh.append(entry)
    return fresh


def _fold_status_turn(base: AgentResult, turn: AgentResult, *, settled: bool) -> None:
    """Fold a status turn's *accounting* into the result, and nothing else.

    Waiting on a card is the harness's own bookkeeping, so none of it may be
    charged to the agent under test. Every part of the grade the wait touched
    on run_20260806_164343_515494 it touched wrongly:

    * ``ToolInvocation`` 0.40 -- "entering a redundant loop of calling
      kanban_show six times". The poll prompt issued those six calls, not the
      agent, so they stay out of the trajectory and the tool counts.
    * "the execution ended in a protocol violation crash" -- a harness-side
      read timeout on ``errors``. The agent did not crash.
    * ``OutcomeValidity`` 0.20 -- "fails to confirm the successful creation".
      The delegating turn had already answered with the id the task asked for;
      the last poll turn's "the task is still running" *replaced* it.

    So the turn's text accumulates instead of superseding -- but only when the
    turn ``settled`` a card, meaning it reported one terminal. Which turn holds
    the answer is not knowable up front: on the smoke task it is the delegating
    turn ("created, the id is t_...") and on a delegated RCA it is the last
    poll ("root cause: ..."), where the delegating turn only says work has
    started. A turn that reports a card still running has nothing to add, and
    keeping it is not free -- five "the task is still running" restatements
    took OutcomeValidity to 0.00 on a task that asked for one sentence. Text
    the turn already replayed is not added twice.

    Tokens are real spend and still accumulate, on the same rule as before:
    the endpoint reports them cumulatively for a replayed conversation, so
    that view supersedes ours rather than adding to it.
    """
    if settled and turn.output:
        if base.output.strip() and base.output.strip() in turn.output:
            # A cumulative payload opens with every earlier message, so it
            # already is the whole answer -- appending would say it twice.
            base.output = turn.output
        elif turn.output.strip() not in base.output:
            base.output = "\n\n".join(filter(None, [base.output, turn.output]))
    if _replays(base.trajectory, turn.trajectory):
        base.tokens = dict(turn.tokens)
    else:
        _sum_tokens(base.tokens, turn.tokens)
    for key in ("response_id", "response_status"):
        if turn.metadata.get(key) is not None:
            base.metadata[key] = turn.metadata[key]


def _numeric_env(name: str, default: str, cast: Any) -> Any:
    """Read a numeric env var, naming it in the error rather than the value."""
    raw = os.environ.get(name, default)
    try:
        return cast(raw)
    except ValueError:
        raise ValueError(f"{name} must be numeric, got {raw!r}") from None


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    detail = exc.read().decode("utf-8", errors="replace")
    try:
        return json.loads(detail).get("error", {}).get("message", detail)
    except (json.JSONDecodeError, AttributeError):
        return detail


class _TransportError(RuntimeError):
    """A turn that never reached the agent, or came back unreadable.

    Distinct from an agent that answered badly: the message is ready for
    ``AgentResult.errors`` and the caller stops rather than retrying.
    """


def _post_turn(
    url: str, body: dict[str, Any], headers: dict[str, str], timeout: float
) -> tuple[AgentResult, str]:
    """POST one turn and parse the reply.

    Shared by the opening prompt and every status turn so they cannot drift.

    Returns:
        The parsed result and the session id header (``""`` when absent).

    Raises:
        _TransportError: The request failed or the reply was not a JSON object.
    """
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            session_id = response.headers.get(_SESSION_ID_HEADER, "")
    except urllib.error.HTTPError as exc:
        raise _TransportError(
            f"HTTP {exc.code} from agent endpoint: {_http_error_detail(exc)}"
        ) from exc
    except (OSError, http.client.HTTPException, ValueError) as exc:
        # URLError, timeouts, resets, a mid-read protocol failure, and a
        # body that is neither UTF-8 nor JSON: transport, not agent, bugs.
        raise _TransportError(f"{type(exc).__name__}: {exc}") from exc

    if not isinstance(payload, dict):
        raise _TransportError(f"agent endpoint returned non-object JSON: {type(payload).__name__}")
    return _parse_response(payload), session_id


class KubeAgentsHarness(AgentHarness):
    """Drives the in-cluster platform agent over its HTTP endpoint.

    Known failure modes (HTTP errors, unreachable endpoint, malformed JSON)
    return an ``AgentResult`` with ``errors`` populated; the base class's safety
    net covers anything unexpected.
    """

    def _execute(self, prompt: str, workspace_path: Path | None = None) -> AgentResult:
        api_path = os.environ.get("AGENT_API_PATH", "/v1/responses")
        try:
            local_port = _numeric_env("AGENT_LOCAL_PORT", str(SERVICE_API_PORT), int)
            timeout = _numeric_env("AGENT_HTTP_TIMEOUT", "600", float)
            delegation_timeout = _numeric_env("AGENT_DELEGATION_TIMEOUT", "1800", float)
            poll_interval = _numeric_env("AGENT_DELEGATION_POLL_INTERVAL", "30", float)
        except ValueError as exc:
            return AgentResult.errored(str(exc))

        # "@evil.example/..." would make 127.0.0.1:<port> the userinfo of
        # another host and send the bearer token there.
        if not api_path.startswith("/"):
            return AgentResult.errored(f"AGENT_API_PATH must start with '/': {api_path!r}")

        try:
            _ensure_port_forward(local_port)
        except RuntimeError as exc:
            return AgentResult.errored(str(exc))

        # 127.0.0.1 rather than localhost, matching _port_open's probe host: a
        # v4/v6 mismatch would make the probe and the request disagree.
        url = f"http://127.0.0.1:{local_port}{api_path}"
        headers = {"Content-Type": "application/json"}
        token = os.environ.get("PLATFORM_AGENT_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = {
            "model": os.environ.get("AGENT_MODEL_NAME", "hermes-agent"),
            # The endpoint is stateful and replays the whole conversation's tool
            # calls, so a shared id would make each task inherit the previous
            # task's trajectory and corrupt trajectory scoring.
            "conversation": os.environ.get("AGENT_CONVERSATION_ID")
            or f"devops-bench-{uuid.uuid4().hex[:12]}",
            "input": prompt,
        }

        try:
            result, session_id = _post_turn(url, body, headers, timeout)
        except _TransportError as exc:
            return AgentResult.errored(str(exc))

        if delegation_timeout > 0:
            session_id = (
                self._await_delegated_work(
                    result,
                    url=url,
                    body=body,
                    headers=headers,
                    timeout=timeout,
                    delegation_timeout=delegation_timeout,
                    poll_interval=poll_interval,
                )
                or session_id
            )

        # One lookup, after the last turn: the session row is cumulative over
        # the conversation, so it supersedes the summed envelopes outright.
        if session_id:
            result.metadata["session_id"] = session_id
            _canonical_session_tokens(
                result.tokens,
                session_id,
                local_port,
                headers,
                min(timeout, _SESSION_LOOKUP_TIMEOUT),
            )
        return result

    def _await_delegated_work(
        self,
        result: AgentResult,
        *,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
        delegation_timeout: float,
        poll_interval: float,
    ) -> str:
        """Poll the agent until every card it filed settles.

        Only two things reach ``result``: the delivered card results, appended
        to the agent's own answer, and the turns' token spend. Everything else
        the wait does is the harness's, not the agent's, and grading it as the
        agent's is what made this feature score a passing task 0.00 --
        see :func:`_fold_status_turn`.

        The endpoint is stateful per ``conversation``, so re-POSTing the same id
        continues this episode with the agent's context intact -- the harness
        cannot read the board itself (it is in-cluster SQLite, and only
        ``/v1/responses`` and ``/api/sessions`` are exposed), so it asks the
        agent to read it. Cards filed *during* a status turn join the wait, so a
        fan-out that grows mid-flight is still awaited.

        A status turn that fails in transport is retried up to
        :data:`_MAX_TRANSPORT_FAILURES` times running. A turn that reports no
        outstanding card is tolerated up to :data:`_MAX_SILENT_TURNS` in a row:
        one off-turn -- the agent answering from context, or a read that
        errored -- should not cost the whole task, but an agent that will not
        read the board is a dead end rather than a reason to spin.

        Returns:
            The session id from the last status turn, or ``""`` when no status
            turn ran or the header was absent.
        """
        # The delegating turn may already have shown a card done, in which case
        # there is nothing to wait on and no reason to sleep a poll interval.
        statuses: dict[str, str] = _reported_statuses(result.trajectory)
        filed = _delegated_task_ids(result.trajectory)
        outstanding = self._capped(
            [task_id for task_id in filed if statuses.get(task_id) not in _TERMINAL_STATUSES],
            result,
        )
        # Every card this episode waited on, including the ones that settle
        # mid-loop and leave ``outstanding``; their results are the answer.
        awaited: list[str] = list(filed)
        # The status turns' trajectories, which carry the settled cards'
        # results. Kept beside the graded trajectory rather than in it.
        observed: list[dict[str, Any]] = list(result.trajectory)
        if not outstanding:
            _append_delivered(result, observed, awaited)
            return ""

        deadline = time.monotonic() + delegation_timeout
        session_id = ""
        silent = 0
        transport_failures = 0
        timed_out = True
        while outstanding:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _log.info("waiting %.0fs on delegated tasks: %s", poll_interval, ", ".join(outstanding))
            # max(0.0, ...): a negative AGENT_DELEGATION_POLL_INTERVAL would
            # otherwise raise straight out of sleep().
            time.sleep(max(0.0, min(poll_interval, remaining)))

            # Clamp the request to what is left, or a turn issued just before
            # the deadline could block for a further AGENT_HTTP_TIMEOUT and
            # overrun the total budget by that much.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            poll = _POLL_PROMPT.format(tool=_STATUS_TOOL, ids=", ".join(outstanding))
            try:
                turn, turn_session = _post_turn(
                    url, {**body, "input": poll}, headers, min(timeout, remaining)
                )
            except _TransportError as exc:
                transport_failures += 1
                _log.warning(
                    "status turn failed (%d/%d): %s",
                    transport_failures,
                    _MAX_TRANSPORT_FAILURES,
                    exc,
                )
                if transport_failures < _MAX_TRANSPORT_FAILURES:
                    # Back off one poll interval and ask again: the loop top
                    # re-checks the deadline, so retries cannot outlive it.
                    continue
                # Recorded, not just logged. Abandoning the wait silently left
                # ``errors`` empty, so the run still validated and the judge
                # graded the delegation receipt as the answer -- the exact
                # false low score this wait exists to prevent.
                result.errors.append(
                    f"status turns failed in transport {transport_failures} times running; "
                    "still waiting on: " + ", ".join(outstanding)
                )
                timed_out = False
                break
            transport_failures = 0
            reported = _reported_statuses(turn.trajectory)
            _fold_status_turn(
                result,
                turn,
                settled=any(status in _TERMINAL_STATUSES for status in reported.values()),
            )
            observed.extend(_merge_new(observed, turn.trajectory))
            session_id = turn_session or session_id

            if any(task_id in reported for task_id in outstanding):
                silent = 0
            else:
                silent += 1
                if silent >= _MAX_SILENT_TURNS:
                    result.errors.append(
                        f"agent reported no status for {silent} turns running; "
                        "still waiting on: " + ", ".join(outstanding)
                    )
                    timed_out = False
                    break
            statuses.update(reported)
            outstanding = [
                task_id
                # dict.fromkeys: order-preserving dedupe, so a card the agent
                # re-filed under the same id is awaited once.
                for task_id in dict.fromkeys(outstanding + _delegated_task_ids(turn.trajectory))
                if statuses.get(task_id) not in _TERMINAL_STATUSES
            ]
            outstanding = self._capped(outstanding, result)
            awaited = list(dict.fromkeys(awaited + _delegated_task_ids(turn.trajectory)))

        # Only on the deadline path: after a transport failure or a mute agent
        # the budget is untouched, and claiming it ran out would misreport why
        # the run stopped.
        if outstanding and timed_out:
            result.errors.append(
                "delegated tasks did not finish within "
                f"{delegation_timeout:.0f}s: "
                + ", ".join(f"{t} ({statuses.get(t, 'unknown')})" for t in outstanding)
            )
        _append_delivered(result, observed, awaited)
        return session_id

    @staticmethod
    def _capped(task_ids: list[str], result: AgentResult) -> list[str]:
        """Trim the awaited set to :data:`_MAX_AWAITED_TASKS`, recording the drop.

        Silent truncation would read as "we waited for everything" on a run
        that did not, so the overflow lands in ``errors`` -- which also stops
        the record promoting on a partial wait.
        """
        if len(task_ids) <= _MAX_AWAITED_TASKS:
            return task_ids
        dropped = len(task_ids) - _MAX_AWAITED_TASKS
        _log.warning("awaiting only %d of %d cards", _MAX_AWAITED_TASKS, len(task_ids))
        result.errors.append(
            f"too many delegated tasks: awaiting {_MAX_AWAITED_TASKS}, ignoring {dropped}"
        )
        return task_ids[:_MAX_AWAITED_TASKS]
