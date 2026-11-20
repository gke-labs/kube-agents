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
"""A reply after an idle wait: the opening turn, a wait, then a reply on the same conversation.

A prompt whose first line is :data:`DIRECTIVE` is two turns for the agent.
``opening`` goes through the inject door as the first turn. The harness then
waits ``idle_seconds`` with the conversation idle, and sends ``reply`` on the
same conversation; that turn's answer is the run's deliverable.

What it grades is the gateway's session lifecycle across an idle gap. A
single-turn DM's session pod is reaped after the DM idle TTL (5 minutes by
default once #2371's DM TTL ships; 30 minutes, the idle TTL for every
session, before it), and the next message on the conversation spawns a fresh pod
rehydrated from the conversation's history (``a2a/gateway/reap.go``). A case
gives a fact in the opening turn and asks for it in the reply: an agent whose
rehydrated session carries the history answers it, and one that lost it
cannot. A wait past the TTL plus a sweep is what makes the reply land on a
rehydrated pod; a shorter one tests the warm pod instead.

It runs on the inject transport only, which every CI lane uses.

The harness reads the conversation's session pod (its ``session-key``
annotation names the conversation) after the opening and again after the
reply, and records both on :data:`WAIT_ENTRY`. The entry is ``completed``
only when both were found and differ: the opening's pod was reaped and the
reply ran on a fresh one. Otherwise it is ``error`` and says which, so a
case's ``tool_called`` objective with ``require_success`` fails a run whose
reply reached the warm pod, or found no session pod at all, rather than
passing it (``cases.HARNESS_WRITTEN_ENTRIES`` names the entry, so a lossy
door trace does not set that objective aside).

Nothing here imports devops_bench at module scope, as for
:mod:`pushed_report`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from devops_bench.agents import AgentResult

__all__ = [
    "DIRECTIVE",
    "MAX_IDLE_SECONDS",
    "MIN_IDLE_SECONDS",
    "REPLY_SUFFIX",
    "WAIT_ENTRY",
    "Replay",
    "is_reply_after_idle",
    "merge",
    "parse",
    "session_pod",
    "wait_entry",
]

# First line of the prompt; the ``key: value`` lines under it are its fields,
# each on one line. Blank lines are skipped, and any other line is an
# authoring error.
DIRECTIVE = "[bench:reply-after-idle]"
_FIELDS = ("opening", "idle_seconds", "reply")
# The bounds on the wait: at least a minute, which is the gateway's floor for
# the DM idle TTL, and at most half an hour, the default idle TTL for every
# other session, past which the case would test nothing the default doesn't.
MIN_IDLE_SECONDS = 60
MAX_IDLE_SECONDS = 1800
# The trajectory entry the wait writes, and the reply turn's message-id
# suffix: the door dedupes on the id, so the reply can't share the opening's.
WAIT_ENTRY = "harness_idle_wait"
REPLY_SUFFIX = "/after-idle"
# The result's metadata key for the opening turn's answer and the wait.
METADATA_KEY = "reply_after_idle"
# How the gateway marks a session pod and names its conversation
# (a2a/gateway/spawn.go: labelRole/sessionRole, annoConvo).
SESSION_POD_SELECTOR = "app.kubernetes.io/component=a2a-session"
SESSION_KEY_ANNOTATION = "a2a.kubeagents.dev/session-key"


@dataclass(frozen=True)
class Replay:
    """The three fields of the prompt."""

    opening: str
    idle_seconds: int
    reply: str


def is_reply_after_idle(prompt: str) -> bool:
    """Whether the prompt's first non-blank line opens with the directive. A
    near miss (the fields glued onto the directive's line) counts, so
    :func:`parse` refuses it rather than sending the directive to the agent."""
    return prompt.strip().partition("\n")[0].startswith(DIRECTIVE)


def parse(prompt: str) -> Replay | None:
    """The replay ``prompt`` asks for, or ``None`` when it is an ordinary ask.

    Raises :class:`ValueError` for a prompt missing a field, carrying a line
    that is not one, or a wait that isn't a whole number of seconds within
    the bounds: a case authoring error, not a run.
    """
    if not is_reply_after_idle(prompt):
        return None
    lines = prompt.strip().splitlines()
    if lines[0].strip() != DIRECTIVE:
        raise ValueError(f"{DIRECTIVE} must stand alone on the first line; got {lines[0].strip()!r}")
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        key = key.strip()
        if not sep or key not in _FIELDS:
            raise ValueError(f"{DIRECTIVE} prompt line is not a field: {line.strip()!r}")
        if key in fields:
            raise ValueError(f"{DIRECTIVE} prompt repeats {key}")
        fields[key] = value.strip()
    missing = [name for name in _FIELDS if not fields.get(name)]
    if missing:
        raise ValueError(f"{DIRECTIVE} prompt is missing {', '.join(missing)}")
    raw = fields["idle_seconds"]
    if not raw.isdigit() or not MIN_IDLE_SECONDS <= int(raw) <= MAX_IDLE_SECONDS:
        raise ValueError(
            f"{DIRECTIVE} idle_seconds must be a whole number from {MIN_IDLE_SECONDS} to {MAX_IDLE_SECONDS}; got {raw!r}"
        )
    return Replay(fields["opening"], int(raw), fields["reply"])


def wait_entry(
    conversation: str, idle_seconds: int, waited_seconds: float, opening_pod: str | None, reply_pod: str | None
) -> dict:
    """The trajectory entry for the wait: how long, and the session pod each turn ran on.

    ``completed`` only when both pods were found and differ (the reply was
    rehydrated); ``error`` otherwise, with the reason. A pod that could not be
    read is ``None``; one the read found no match for is ``""``.
    """
    result: dict = {"waited_seconds": round(waited_seconds, 1), "opening_pod": opening_pod, "reply_pod": reply_pod}
    if not opening_pod or not reply_pod:
        result["reason"] = "no session pod found for the conversation after the " + (
            "opening" if not opening_pod else "reply"
        )
    elif opening_pod == reply_pod:
        result["reason"] = "the reply ran on the opening's pod: it was not reaped during the wait"
    return {
        "name": WAIT_ENTRY,
        "args": {"conversation": conversation, "idle_seconds": idle_seconds},
        "result": result,
        "status": "error" if "reason" in result else "completed",
    }


def session_pod(pods_json: str | None, conversation: str) -> str | None:
    """The name of the live session pod whose session-key annotation is ``conversation``.

    ``pods_json`` is ``kubectl get pods -o json`` over the session pods.
    ``None`` when the read failed (``pods_json`` is ``None`` or not JSON);
    ``""`` when no live pod matches. A pod being deleted doesn't count.
    """
    import json

    if pods_json is None:
        return None
    try:
        items = json.loads(pods_json).get("items") or []
    except (ValueError, AttributeError):
        return None
    for pod in items:
        meta = pod.get("metadata") or {}
        if meta.get("deletionTimestamp"):
            continue
        if (meta.get("annotations") or {}).get(SESSION_KEY_ANNOTATION) == conversation:
            return str(meta.get("name") or "")
    return ""


def merge(opening: AgentResult, reply: AgentResult, entry: dict) -> AgentResult:
    """One result for the two turns, graded on the reply turn's answer.

    The same merge as a pushed-report replay's (:func:`pushed_report.merge`):
    the reply turn's output, both trajectories with the wait's entry between
    them, both turns' errors and tokens. The opening turn's answer and the
    wait go under :data:`METADATA_KEY`.
    """
    from kube_agents_bench import pushed_report

    merged = pushed_report.merge(opening, reply, entry)
    shared = merged.metadata.pop("pushed_report")
    merged.metadata[METADATA_KEY] = {
        "opening_final_message": shared["opening_final_message"],
        "opening_task_id": shared["opening_task_id"],
        "wait": {**entry["args"], **entry["result"]},
    }
    return merged
