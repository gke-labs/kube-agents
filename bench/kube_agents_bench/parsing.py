"""Reading a Responses-style payload, and reading facts back out of a turn.

The endpoint is stateful per ``conversation`` and replays earlier turns' output
items, sometimes duplicated and sometimes reordered after a compaction. Most of
the care here is about not counting a replay as new work: the judge grades the
trajectory, and a duplicated call reads as a tool loop the agent never ran.
"""

from __future__ import annotations

import json
import logging
import re
from collections import deque
from typing import Any

from devops_bench.agents import AgentResult, ToolCall
from devops_bench.agents.result import empty_tokens

_log = logging.getLogger("kube_agents_bench.parsing")

DELEGATION_TOOL = "kanban_create"
STATUS_TOOL = "kanban_show"

_TOOL_ERROR_PREFIX = "Error executing tool"

# hermes replaces a re-emitted output with this notice when an identical one
# appears later in the payload. Keeping the entry loses the real output.
_ELIDED_OUTPUT = re.compile(r"^\[Duplicate tool output\b.*\]$", re.DOTALL)

# Card ids come from the graded agent's own tool output and are interpolated
# into the next prompt, the log, and the run record. A charset rather than
# hermes' exact "t_" + 8 hex shape: wide enough to survive an id-format change,
# narrow enough that no whitespace or prose can ride in.
_TASK_ID_RE = re.compile(r"\A[A-Za-z0-9_.:-]{1,64}\Z")

_ENVELOPE_TOKEN_KEYS = (
    ("input", "input_tokens"),
    ("output", "output_tokens"),
    ("total", "total_tokens"),
)


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


def _output_failed(text: str) -> bool:
    """Whether the text is a *structured* hermes tool failure.

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
    # An error beside a real payload is a diagnostic, not a failure, so every
    # key that can carry one counts.
    return bool(data.get("error")) and not (
        data.get("content") or data.get("result") or data.get("structuredContent")
    )


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
    """Coerce a reported tool name to a string, since it becomes a dict key."""
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


def _envelope_tokens(payload: dict[str, Any]) -> dict[str, Any]:
    """Token buckets from the response envelope; an omitted one stays ``None``."""
    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    tokens = empty_tokens()
    for bucket, key in _ENVELOPE_TOKEN_KEYS:
        if usage.get(key) is not None:
            tokens[bucket] = usage[key]
    return tokens


def parse_response(payload: dict[str, Any]) -> AgentResult:
    """Map a Responses-style payload onto the canonical ``AgentResult``.

    A ``function_call`` repeating a ``call_id`` seen in this payload is a replay
    and is dropped. A ``function_call_output`` folds *into* its originating
    call, keyed on ``call_id`` and falling back to the oldest unresolved call
    only when the id is absent -- appending it instead would show the judge a
    redundant argument-less entry.

    ``metadata`` carries two things the poll loop needs and ``AgentResult`` has
    no field for: ``final_message``, the turn's closing text, and ``call_ids``,
    each surviving entry's originating id positionally.

    Shape anomalies degrade rather than raise: whatever was readable is kept,
    with the anomaly on ``errors``.
    """
    output_text = ""
    final_message = ""
    trajectory: list[ToolCall] = []
    tools_used: dict[str, int] = {}
    calls_by_id: dict[str, ToolCall] = {}
    unkeyed_calls: deque[ToolCall] = deque()
    elided: set[int] = set()
    # Entries synthesised from an output with no matching call. Never counted in
    # ``tools_used``, so an elided one must not be decremented out of it.
    orphans: set[int] = set()
    call_ids: dict[int, str] = {}
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
            message = _message_text(part)
            output_text += message
            if message.strip():
                final_message = message

        elif part_type == "function_call":
            call_id = part.get("call_id")
            if call_id and str(call_id) in calls_by_id:
                continue
            name = _tool_name(part.get("name"))
            entry = ToolCall(name=name, args=_call_args(part.get("arguments")))
            trajectory.append(entry)
            call_ids[id(entry)] = str(call_id) if call_id else ""
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
                # An id matching nothing is an orphan, not a licence to consume
                # an unrelated call.
                parse_errors.append(f"tool output without a matching call (call_id={call_id!r})")
                target = ToolCall(name=_tool_name(part.get("name")), args={})
                trajectory.append(target)
                orphans.add(id(target))
            text = _output_text(part.get("output"))
            if _ELIDED_OUTPUT.match(text.strip()):
                elided.add(id(target))
                continue
            elided.discard(id(target))
            target.result = text
            # Outputs carry no status, so failure is read out of the payload.
            target.status = "error" if _output_failed(text) else "completed"

    for entry in trajectory:
        if id(entry) in elided and id(entry) not in orphans and tools_used.get(entry.name):
            tools_used[entry.name] -= 1
            if not tools_used[entry.name]:
                del tools_used[entry.name]
    trajectory = [entry for entry in trajectory if id(entry) not in elided]

    metadata: dict[str, Any] = {
        "tools": tools_used,
        "final_message": final_message,
        "call_ids": [call_ids.get(id(entry), "") for entry in trajectory],
    }
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


def _json_object(raw: Any) -> dict[str, Any]:
    """Parse a recorded tool result into a mapping; anything else yields ``{}``.

    A failed hermes tool answers with a differently-shaped payload, so every
    read of a result is defensive.
    """
    if not isinstance(raw, str):
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def delegated_task_ids(trajectory: list[dict[str, Any]]) -> list[str]:
    """Card ids filed by ``kanban_create`` during a turn, in call order.

    Read out of the tool result rather than the prose. A failed create carries
    no ``task_id``, and anything that does not look like an id is dropped.
    """
    ids: list[str] = []
    for entry in trajectory:
        if entry.get("name") != DELEGATION_TOOL:
            continue
        task_id = _json_object(entry.get("result")).get("task_id")
        if isinstance(task_id, str) and not _TASK_ID_RE.match(task_id):
            _log.warning("ignoring malformed task id from %s", DELEGATION_TOOL)
            continue
        if isinstance(task_id, str) and task_id and task_id not in ids:
            ids.append(task_id)
    return ids


def reported_statuses(trajectory: list[dict[str, Any]]) -> dict[str, str]:
    """Card statuses read out of any board-reading tool result in a turn.

    Keyed on payload shape, not tool name: an agent asked about several cards
    may batch-read with ``kanban_list`` (``{"tasks": [...]}``) instead of
    ``kanban_show`` (``{"task": {...}}``). A later reading of a card wins.
    """
    statuses: dict[str, str] = {}
    for entry in trajectory:
        payload = _json_object(entry.get("result"))
        listed = payload.get("tasks")
        for task in [payload.get("task"), *(listed if isinstance(listed, list) else [])]:
            if not isinstance(task, dict):
                continue
            task_id, status = task.get("id"), task.get("status")
            if isinstance(task_id, str) and isinstance(status, str):
                statuses[task_id] = status
    return statuses


def delivered_results(trajectory: list[dict[str, Any]], task_ids: list[str]) -> dict[str, str]:
    """Completion text each awaited card carries, keyed by card id.

    The delegated worker's answer, not the router's paraphrase of it. Two places
    carry it, because hermes' ``complete_task`` writes its two arguments to two
    tables: ``result`` sets ``tasks.result`` and ``summary`` sets
    ``task_runs.summary``. The platform profile finishes with a summary alone,
    which leaves ``tasks.result`` null.
    """
    delivered: dict[str, str] = {}
    for entry in trajectory:
        payload = _json_object(entry.get("result"))
        listed = payload.get("tasks")
        shown = payload.get("task")
        for task in [shown, *(listed if isinstance(listed, list) else [])]:
            if not isinstance(task, dict):
                continue
            task_id, text = task.get("id"), task.get("result")
            if task_id in task_ids and isinstance(text, str) and text.strip():
                delivered[task_id] = text
        # ``runs`` sits beside ``task``, not inside it, so it can only be
        # attributed to the card that payload showed, and only when the card
        # carries no explicit deliverable of its own.
        if not isinstance(shown, dict) or shown.get("id") not in task_ids:
            continue
        task_id = str(shown["id"])
        if delivered.get(task_id, "").strip():
            continue
        runs = payload.get("runs")
        for run in runs if isinstance(runs, list) else []:
            summary = run.get("summary") if isinstance(run, dict) else None
            if isinstance(summary, str) and summary.strip():
                delivered[task_id] = summary
    return delivered


def merge_new(base: list[dict[str, Any]], turn: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The entries of ``turn`` that ``base`` does not already hold, in order.

    Matched on content, not position: a compaction re-emits the earlier calls in
    a different order, which a positional test misses. Replays are
    byte-identical, so the whole entry is a sound key.
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


def new_calls(turn: AgentResult, seen: set[str]) -> list[dict[str, Any]]:
    """The turn's entries whose ``call_id`` is not already in ``seen``.

    ``seen`` is updated in place. Keyed on the id rather than on content,
    because an unchanged card answers identically on consecutive polls and a
    content test would read that as silence. An entry with no id counts as new:
    over-reporting freshness costs one extra poll, under-reporting abandons a
    wait that was working.
    """
    ids = turn.metadata.get("call_ids")
    ids = ids if isinstance(ids, list) else []
    fresh = []
    for index, entry in enumerate(turn.trajectory):
        call_id = str(ids[index]) if index < len(ids) else ""
        if call_id and call_id in seen:
            continue
        if call_id:
            seen.add(call_id)
        fresh.append(entry)
    return fresh
