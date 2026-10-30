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
"""A reply under a pushed report: the report is stored for the conversation, then the reply is sent.

A prompt whose first line is :data:`DIRECTIVE` is three turns of work for
the harness and two for the agent. ``opening`` goes through the inject door
as the first turn, so the conversation has a gateway session and a Hermes
session behind it. Then ``report`` is stored against that conversation with
the row the kanban notifier writes for a card's report inviting a reply that it
posted into a Slack or Google Chat gateway conversation (``deploy/docker/patches/kanban_notifier.py``,
``store_incident_report``): one ``POST /v1/incidents`` on the agent pod's
Session KV server, keyed by the Hermes session the bridge runs the
conversation in and the conversation key (``kanban_event_routing`` addresses
such a subscription so). No writer posts into an inject conversation, so the
plant supplies the row; what it has is the shape. Then ``reply`` is sent on
the same conversation, and that turn's answer is the run's deliverable.

What it grades is the hermes-bridge's read of that row before the turn
(``a2a/hermes-bridge/incident.go``): under ``spec.mode: next`` a reply under
a report the relay or the notifier pushed reaches the bridge with no link to
the report, and the bridge attaches the stored report ahead of the user's
words, as the Hermes gateway's ``incident_context`` hook does under today.
A case asks about a detail only the report carries; an agent that has the
report answers it, and one that does not cannot.

It runs on the inject transport only: the API server has no conversation
the bridge keys the row to. A case that uses it lists itself in
``hack/eval/api-lane-exclusions.txt``.

The plant records :data:`PLANT_ENTRY` on the run's trajectory with status
``completed``, so a case's ``tool_called`` objective with
``require_success`` fails a run where the row was never stored rather than
passing its safeguards vacuously (``cases.HARNESS_WRITTEN_ENTRIES`` names
it, so a lossy door trace does not set that objective aside). A plant
script that never ran to completion is infrastructure
(:class:`PlantUnavailable`); one that ran in the image and failed there is
an errored run (:class:`PlantBroken`), because the fault is the image's.

Imported by ``scripts/test_eval_rosters.py`` in the repository's ``scripts``
test sweep, which installs no ``devops_bench``: nothing here imports it at
module scope.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from devops_bench.agents import AgentResult

__all__ = [
    "DIRECTIVE",
    "PLANT_ENTRY",
    "REPLY_SUFFIX",
    "PlantBroken",
    "PlantUnavailable",
    "Replay",
    "is_reply_under_pushed_report",
    "merge",
    "parse",
    "plant",
    "plant_command",
]

# First line of a replay prompt. The ``key: value`` lines under it are its
# fields, each on one line; blank lines are skipped, any other line is an
# authoring error.
DIRECTIVE = "[bench:reply-under-pushed-report]"
_FIELDS = ("opening", "report", "reply")
# The trajectory entry the plant writes, and the message-id suffix of the
# reply turn: the door dedupes on the id, so the reply cannot share the
# opening's.
PLANT_ENTRY = "harness_pushed_report_stored"
REPLY_SUFFIX = "/reply"
# The plant script prints this first, so a reply without it is a script
# that never ran (kubectl exec gave out) rather than one that failed.
SENTINEL = "__BENCH_PUSHED_REPORT__"
# The Session KV route the kanban notifier writes through, and its default
# address in the pod.
INCIDENTS_PATH = "/v1/incidents"
SESSION_KV_DEFAULT_URL = "http://127.0.0.1:8699"
POST_TIMEOUT_SECONDS = 10
# How much of a reply that is not JSON the error quotes.
PLANT_REPLY_TAIL_CHARS = 200

_PLANT_SCRIPT = r"""
import json, os, sys, urllib.request
from http import HTTPStatus
SENTINEL, URL, PATH, SESSION, CONVERSATION, REPORT, TIMEOUT = sys.argv[1:8]
print(SENTINEL)
out = {"stored": False, "status": None, "error": None}
headers = {"Content-Type": "application/json"}
token = (os.environ.get("SESSION_KV_API_KEY") or "").strip()
if token:
    headers["Authorization"] = "Bearer " + token
body = json.dumps({"chat_id": SESSION, "thread_id": CONVERSATION, "report": REPORT}).encode()
try:
    request = urllib.request.Request(URL + PATH, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=float(TIMEOUT)) as response:
        out["status"] = response.status
        out["stored"] = response.status == HTTPStatus.OK
except Exception as exc:
    out["error"] = f"{type(exc).__name__}: {exc}"
print(json.dumps(out))
"""


class PlantUnavailable(RuntimeError):
    """The plant script never ran to completion in the pod: infrastructure, not a run."""


class PlantBroken(RuntimeError):
    """The plant script ran in the image and the row was not stored: an errored run."""


@dataclass(frozen=True)
class Replay:
    """The three fields of a replay prompt."""

    opening: str
    report: str
    reply: str


def is_reply_under_pushed_report(prompt: str) -> bool:
    """Whether the prompt's first non-blank line opens with the directive: the
    one test :func:`parse` and every other reader of the grammar share. A
    near miss (the directive with its fields glued onto its line, as a folded
    or one-line prompt produces) is a replay here, so :func:`parse` refuses
    it rather than sending the directive and the report to the agent as an
    ordinary ask, as ``stop_hook.is_stopped_ask`` does for its directive."""
    return prompt.strip().partition("\n")[0].startswith(DIRECTIVE)


def parse(prompt: str) -> Replay | None:
    """The replay ``prompt`` asks for, or ``None`` when it is an ordinary ask.

    Raises :class:`ValueError` for a replay prompt missing a field or
    carrying a line that is not one: a case authoring error, not a run.
    """
    if not is_reply_under_pushed_report(prompt):
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
    return Replay(fields["opening"], fields["report"], fields["reply"])


def plant_command(session_id: str, conversation: str, report: str) -> str:
    """The in-pod command that stores ``report`` for ``conversation``'s session.

    ``conversation`` is the key as the door handed it back (``inject:…``),
    which is the key the gateway's session record and the bridge's authority
    carry; the harness adopts the door's spelling rather than prefixing it
    here (``inject_transport.InjectTask.submit``).
    """
    from kube_agents_bench.worker_trajectory import FALLBACK_PYTHON, HERMES_PYTHON

    args = [
        SENTINEL,
        SESSION_KV_DEFAULT_URL,
        INCIDENTS_PATH,
        session_id,
        conversation,
        report,
        str(POST_TIMEOUT_SECONDS),
    ]
    quoted = " ".join(shlex.quote(a) for a in args)
    return (
        f'PY={shlex.quote(HERMES_PYTHON)}; [ -x "$PY" ] || PY={shlex.quote(FALLBACK_PYTHON)}; '
        f'"$PY" -c {shlex.quote(_PLANT_SCRIPT)} {quoted}'
    )


def plant(shell: Callable[[str, float], str], session_id: str, conversation: str, report: str, timeout: float) -> dict:
    """Store ``report`` for the conversation in the pod; return the script's reply.

    ``shell`` is :func:`harness._agent_shell`. Raises
    :class:`PlantUnavailable` when the script never ran to completion and
    :class:`PlantBroken` when it ran and the row was not stored.
    """
    text = shell(plant_command(session_id, conversation, report), timeout)
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines or lines[0].strip() != SENTINEL:
        raise PlantUnavailable("pushed report: the plant script never ran in the agent pod (kubectl exec gave out)")
    try:
        payload = json.loads(lines[-1])
    except ValueError as exc:
        raise PlantBroken(
            f"pushed report: the plant script's reply is not JSON: {lines[-1][:PLANT_REPLY_TAIL_CHARS]!r}"
        ) from exc
    if not isinstance(payload, dict) or not payload.get("stored"):
        detail = payload.get("error") if isinstance(payload, dict) else payload
        raise PlantBroken(f"pushed report: the row was not stored: {detail!r}")
    return payload


def plant_entry(session_id: str, conversation: str, payload: dict) -> dict:
    """The trajectory entry that says the row was stored."""
    return {
        "name": PLANT_ENTRY,
        "args": {"session": session_id, "conversation": conversation},
        "result": payload,
        "status": "completed",
    }


def merge(opening: AgentResult, reply: AgentResult, entry: dict) -> AgentResult:
    """One result for the two turns, graded on the reply turn's answer.

    ``output`` and ``final_message`` are the reply turn's; the opening turn's
    answer is kept in metadata. The trajectory is the opening turn's, the
    plant's entry, then the reply turn's, so a ``tool_called`` on
    :data:`PLANT_ENTRY` reads it and the lane's safeguards read both turns.
    Errors are both turns', the opening's first. Tokens are both turns' too:
    the reply's buckets with the opening's added in, bucket by bucket and
    into the nested ones, as the card-wake merge sums two turns (an inject
    record's tokens are the chain's usage as the door reported it, null
    where it reported none, and a null bucket adds nothing).
    """
    from kube_agents_bench.card_wake import _add_tokens  # devops_bench-bound; imported where a result exists

    tokens = dict(reply.tokens)
    _add_tokens(tokens, opening.tokens)
    metadata = {**opening.metadata, **reply.metadata}
    metadata["pushed_report"] = {
        "opening_final_message": str(opening.metadata.get("final_message") or opening.output),
        "opening_task_id": opening.metadata.get("task_id"),
        "plant": entry["args"],
    }
    # The result's own class, so this module never imports devops_bench.
    return type(reply)(
        output=reply.output,
        trajectory=[*opening.trajectory, entry, *reply.trajectory],
        tokens=tokens,
        errors=[*opening.errors, *reply.errors],
        metadata=metadata,
    )
