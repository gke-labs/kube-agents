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

"""Read a session pod's tool-call capture back into the inject trajectory.

On the session route the persona runs in a session pod the gateway spawns per
conversation, and its tool calls reach the record as the task's ``activity``
artifact parts (:mod:`kube_agents_bench.inject_transport`). Those parts carry
the tool's name but, on a gateway that strips them, not its ``input``: every
call would read ``args: {}`` and a ``tool_called`` check with an
``arguments:`` pattern could not pass.

The arguments are kept elsewhere. The session pod's worker adapter records
every tool call it publishes, scrubbed and capped, in its own memory, and
serves it read-only on a loopback port inside the pod; the adapter binary's
``trajectory <task id>`` subcommand prints it. This module runs that
subcommand with one ``kubectl exec`` into the pod's ``worker`` container, with
the runner's own kubeconfig, namespace and context (the ones
``harness._kubectl_target`` gives every other kubectl the bench runs), right
after the turn settles. The bridge route (the platform agent) has no session
pod and is not read here; its workers come back through
:mod:`kube_agents_bench.worker_trajectory`.

The reply is a sentinel line (:data:`CAPTURE_PRESENT`) and one JSON document:
``{"task", "known", "calls": [{"seq", "tool", "input", "at"}], "dropped"?}``.
A reply without the sentinel is "could not read" -- a pod whose adapter
predates the capture, a pod already gone, an exec that failed -- and never "no
calls". ``known: false`` says the adapter holds nothing for the task (evicted,
or never seen). An input over the adapter's cap arrives as the same
``{"truncated", "bytes", "head"}`` stand-in the activity parts use, and is
counted as truncated the same way.

The activity parts stay the source of order, tool names and markers;
:func:`merge` only fills an ``input`` the part does not carry, matched per
task by position (the capture's ``seq`` order) and checked by tool name. Any
disagreement in count or name fills nothing and is logged, so the record is
what it would have been without the capture. Nothing here logs an input: the
inputs reach only the trajectory entries, where the part's own input would
have been.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from typing import Any

from kube_agents_bench.inject_transport import ACTIVITY_INPUT_KEY, ACTIVITY_TOOL_KEY

__all__ = ["CAPTURE_PRESENT", "SessionCapture", "capture", "command", "merge", "parse"]

_log = logging.getLogger("kube_agents_bench.session_trajectory")

# The line the adapter's client prints before the capture's JSON
# (``TrajectorySentinel`` in ``a2a/worker-adapter/trajectory.go``).
CAPTURE_PRESENT = "__SESSION_TRAJECTORY__"
# The session pod's one container and the adapter binary in it
# (``workerContainer`` and ``workerAdapterPath`` in ``a2a/gateway/spawn.go``),
# and the subcommand that prints a task's capture (``TrajectoryCommand``).
WORKER_CONTAINER = "worker"
WORKER_ADAPTER_PATH = "/usr/local/bin/worker-adapter"
TRAJECTORY_SUBCOMMAND = "trajectory"
# The capture's keys.
CAPTURE_TASK_KEY = "task"
CAPTURE_KNOWN_KEY = "known"
CAPTURE_CALLS_KEY = "calls"
CAPTURE_DROPPED_KEY = "dropped"
CALL_SEQ_KEY = "seq"
CALL_TOOL_KEY = "tool"
CALL_INPUT_KEY = "input"
# How much of kubectl's stderr a failed exec logs.
STDERR_LOG_CHARS = 200
# How many session pods without a conversation's annotation are tried when
# no pod carries it (:func:`unbound_pods`).
UNBOUND_PODS_TRIED = 8


@dataclass
class SessionCapture:
    """One task's capture as the adapter served it.

    ``calls`` are in ``seq`` order, each ``{"seq", "tool", "input"}`` with
    ``input`` ``None`` when the adapter recorded none. ``known`` is false for
    a task the adapter holds nothing for; ``dropped`` counts calls past its
    caps, which it did not keep.
    """

    task: str
    known: bool
    calls: list[dict[str, Any]] = field(default_factory=list)
    dropped: int = 0


def command(target: list[str], task_id: str) -> list[str]:
    """The ``kubectl exec`` argv that prints ``task_id``'s capture.

    ``target`` is ``harness._kubectl_target("pod/<name>")``: the pod, then
    the namespace and context flags every other kubectl the bench runs uses.
    """
    return [
        "kubectl",
        "exec",
        *target,
        "-c",
        WORKER_CONTAINER,
        "--",
        WORKER_ADAPTER_PATH,
        TRAJECTORY_SUBCOMMAND,
        task_id,
    ]


def parse(reply: str, task_id: str) -> SessionCapture | None:
    """The capture in ``reply``, or ``None`` when it could not be read.

    ``None`` for a reply without the sentinel, a body that is not one JSON
    object, or a capture of another task. A capture of a task the adapter
    does not know is returned with ``known`` false and no calls.
    """
    marker = reply.find(CAPTURE_PRESENT)
    if marker < 0:
        _log.info("session trajectory for %s could not be read (no capture in the reply)", task_id)
        return None
    try:
        payload = json.loads(reply[marker + len(CAPTURE_PRESENT) :].strip())
    except json.JSONDecodeError as exc:
        _log.warning("session trajectory for %s is not JSON: %s", task_id, exc)
        return None
    if not isinstance(payload, dict):
        _log.warning("session trajectory for %s is not a JSON object", task_id)
        return None
    if payload.get(CAPTURE_TASK_KEY) != task_id:
        _log.warning("session trajectory asked for %s answered for another task", task_id)
        return None
    if payload.get(CAPTURE_KNOWN_KEY) is not True:
        _log.info("session trajectory: the pod holds no capture for %s (evicted or never seen)", task_id)
        return SessionCapture(task=task_id, known=False)
    raw_calls = payload.get(CAPTURE_CALLS_KEY)
    if not isinstance(raw_calls, list):
        _log.warning("session trajectory for %s has no call list", task_id)
        return None
    calls: list[dict[str, Any]] = []
    for c in raw_calls:
        seq = c.get(CALL_SEQ_KEY) if isinstance(c, dict) else None
        if not isinstance(seq, int) or isinstance(seq, bool) or not isinstance(c.get(CALL_TOOL_KEY), str):
            _log.warning("session trajectory for %s has a malformed call", task_id)
            return None
        calls.append({CALL_SEQ_KEY: seq, CALL_TOOL_KEY: c[CALL_TOOL_KEY], CALL_INPUT_KEY: c.get(CALL_INPUT_KEY)})
    calls.sort(key=lambda c: c[CALL_SEQ_KEY])
    dropped = payload.get(CAPTURE_DROPPED_KEY)
    if not isinstance(dropped, int) or isinstance(dropped, bool) or dropped < 0:
        dropped = 0
    return SessionCapture(task=task_id, known=True, calls=calls, dropped=dropped)


def unbound_pods(pods_json: str | None, session_key_annotation: str) -> list[str]:
    """The live session pods that carry no conversation's annotation, oldest first.

    A pod the gateway claimed from its warm pool runs a conversation without
    its session-key annotation: the warm pod is created without one and the
    gateway can't patch pods. So when no pod carries the conversation's
    annotation, the capture is looked for on these, at most
    :data:`UNBOUND_PODS_TRIED`; only the pod that ran the task answers
    ``known: true`` for it, and task ids are unique, so the one that does is
    the one. Live as ``idle_reply.session_pod`` counts it: not being deleted,
    not exited.
    """
    if pods_json is None:
        return []
    try:
        items = json.loads(pods_json).get("items") or []
    except (ValueError, AttributeError):
        return []
    live = []
    for pod in items:
        meta = pod.get("metadata") or {}
        if meta.get("deletionTimestamp") or (pod.get("status") or {}).get("phase") in ("Succeeded", "Failed"):
            continue
        if (meta.get("annotations") or {}).get(session_key_annotation):
            continue
        live.append((str(meta.get("creationTimestamp") or ""), str(meta.get("name") or "")))
    return [name for _, name in sorted(live) if name][:UNBOUND_PODS_TRIED]


def capture(target: list[str], task_id: str, timeout: float) -> SessionCapture | None:
    """Read ``task_id``'s capture from the session pod ``target`` names.

    Best effort: a missing kubectl, an exec that fails or times out, or a pod
    whose adapter has no ``trajectory`` subcommand all return ``None``.
    """
    try:
        proc = subprocess.run(
            command(target, task_id),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log.info("session trajectory for %s could not be read: %s", task_id, exc)
        return None
    if proc.returncode != 0:
        _log.info(
            "session trajectory for %s could not be read (kubectl exited %d): %s",
            task_id,
            proc.returncode,
            proc.stderr.strip()[:STDERR_LOG_CHARS],
        )
        return None
    return parse(proc.stdout, task_id)


def merge(parts: list[tuple[int, dict[str, Any]]], captured: SessionCapture) -> dict[int, Any]:
    """The inputs to fill in, by the index of the activity part they belong to.

    ``parts`` are the task's call parts in stream order, each with its index
    in the fold's activity (``Fold.call_parts``). Matched to the capture's
    calls by position and checked by tool name; a count or name that differs
    fills nothing and is logged (tool names only, never an input).

    A part that carries its own ``input`` keeps it: a gateway that still
    publishes inputs is the source it always was, and the capture fills only
    the parts that arrive without one.
    """
    if not captured.known:
        return {}
    if captured.dropped or len(parts) != len(captured.calls):
        _log.warning(
            "session trajectory for %s: %d activity calls, %d captured (%d dropped); args left as the parts gave them",
            captured.task,
            len(parts),
            len(captured.calls),
            captured.dropped,
        )
        return {}
    for position, ((_, part), call) in enumerate(zip(parts, captured.calls)):
        if part.get(ACTIVITY_TOOL_KEY) != call[CALL_TOOL_KEY]:
            _log.warning(
                "session trajectory for %s: call %d is %r in the activity and %r in the capture; "
                "args left as the parts gave them",
                captured.task,
                position + 1,
                part.get(ACTIVITY_TOOL_KEY),
                call[CALL_TOOL_KEY],
            )
            return {}
    return {
        index: call[CALL_INPUT_KEY]
        for (index, part), call in zip(parts, captured.calls)
        if part.get(ACTIVITY_INPUT_KEY) is None and call[CALL_INPUT_KEY] is not None
    }
