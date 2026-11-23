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
"""A new conversation at the session cap: the queued notice, then the answer.

A prompt whose first line is :data:`DIRECTIVE` is an ask the harness sends
only once every session worker the install allows is busy. The rest of the
prompt is that ask, and it has to open a session (``/session ...``). The
harness reads the install's session cap off the gateway Deployment
(``A2A_MAX_SESSIONS``), then opens that many conversations through the inject
door, each a ``/session`` ask that keeps its worker busy (:data:`FILLER_PROMPT`,
a ``sleep`` of ``hold`` seconds), then sends the ask on one conversation more.
At the cap with nothing idle to evict, the gateway queues that turn (#6882):
the door answers the POST with the ``queued`` refusal and the queued notice,
and the harness cancels the fillers so a worker frees, then reads the task
the queued turn starts and its answer off the same conversation, as a chat
user would read them in the thread. Whatever happens, the fillers it opened
are cancelled before the run ends.

The harness records :data:`QUEUED_ENTRY` (:func:`assess`): ``completed`` when
the conversation shows the queued notice, then the task the queued turn
started, then a post of that task's; ``error`` otherwise. A gateway that
refuses the ask's POST at the cap instead (the cap's own ``🚦 not started:
N session workers are already running (cap N)``, no task, no answer) is the
red this case exists for: the entry is ``error`` with the refusal, graded
rather than set aside. Nothing else is graded red: any other refusal, and
anything the gateway says after it has queued the turn (it gave up on the
wait, it restarted), is infrastructure, because by then the queue did its
part and whether a freed worker went to another conversation can't be told.
The answer itself is graded by the case.

WHAT IT NEEDS. A session cap of at most ``max-cap`` (default
:data:`DEFAULT_MAX_CAP`), so filling it costs a few session pods rather than
the shipped ten, and the session route, which the ``/session`` asks reach on
either front door. The case cannot set the cap itself: an install whose cap
is above ``max-cap`` (or whose cap cannot be read) is reported as
infrastructure, naming the cap it found, before anything is sent. So is a run
where the cap was not held by working fillers: a filler queued (something
else holds the cap), one not working inside the fillers' wait, one no longer
running when the ask goes, or an ask that started at once. That is the
fillers' failure to hold the cap, not the queue's. The bench has no "not applicable" class for a
case-level precondition, which is why infrastructure is the honest answer.

It runs on the inject transport only.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DEFAULT_HOLD_SECONDS",
    "DEFAULT_MAX_CAP",
    "DIRECTIVE",
    "FILLER_PROMPT",
    "FILLER_WAIT_SECONDS",
    "GATEWAY_DEFAULT_MAX_SESSIONS",
    "HOLD_MARGIN_SECONDS",
    "QUEUED_ENTRY",
    "CapAsk",
    "assess",
    "filler_prompt",
    "filler_wait",
    "is_session_cap_ask",
    "parse",
    "parse_cap",
    "precondition",
    "refused_at_cap",
]

#: The first line that makes a prompt a session-cap ask.
DIRECTIVE = "[bench:session-cap-queue]"
#: The optional fields after it.
MAX_CAP_FIELD = "max-cap="
HOLD_FIELD = "hold="
#: The highest session cap the case fills when the directive names none, and
#: the highest it may name: each slot is a session pod held for the run.
DEFAULT_MAX_CAP = 2
MAX_MAX_CAP = 4
#: How long each filler keeps its worker busy, in seconds, and the bounds a
#: directive may set. The fillers start one after another, each waited on
#: until it is working, inside one shared wait (:func:`filler_wait`): the hold
#: is that wait plus :data:`HOLD_MARGIN_SECONDS`, so the first filler is still
#: busy when the last one starts and the ask goes. The harness cancels the
#: fillers once the ask is queued, so a run does not wait the hold out.
HOLD_MARGIN_SECONDS = 120
FILLER_WAIT_SECONDS = 600
DEFAULT_HOLD_SECONDS = FILLER_WAIT_SECONDS + HOLD_MARGIN_SECONDS
MIN_HOLD_SECONDS = HOLD_MARGIN_SECONDS + 60
MAX_HOLD_SECONDS = 1800
#: What each filler conversation asks. ``/session`` opens a session pod on
#: either front door; the ``sleep`` keeps its task, and so the pod, busy, so
#: the cap cannot evict it to make room.
FILLER_PROMPT = (
    "/session Run the shell command `sleep {hold}` in your terminal and wait for it to finish, "
    "then reply with the single word FILLED."
)
#: What ``A2A_MAX_SESSIONS`` means when the gateway Deployment does not set
#: it (``defaultMaxSessions`` in a2a/gateway/config.go).
GATEWAY_DEFAULT_MAX_SESSIONS = 10
#: The trajectory entry the harness records for the queue's part.
QUEUED_ENTRY = "harness_session_cap_queued"

# The gateway's queued notice and its cap refusal, as a reader of the
# conversation tells them apart (a2a/gateway/session_cap_queue.go, spawn.go).
# Matched loosely, emphasis aside: the wording is the gateway's to change.
_QUEUED = re.compile(r"\bqueued\b", re.IGNORECASE)
# The cap's refusal alone (refuseAtSessionCap in a2a/gateway/spawn.go:
# "🚦 not started: N session workers are already running (cap N). ..."), not
# the gateway's other "not started" lines (a count that failed, a wait for the
# cap check, a capability or a previous task it could not handle, a restart).
_CAP_REFUSAL = re.compile(r"not started: \d+ session workers? (?:is|are) already running \(cap \d+\)", re.IGNORECASE)


@dataclass(frozen=True)
class CapAsk:
    """The ask to send at the cap, the highest cap the case fills, and how
    long each filler holds its worker."""

    prompt: str
    max_cap: int
    hold_seconds: int


def is_session_cap_ask(prompt: str) -> bool:
    """Whether ``prompt``'s first non-blank line opens with the directive."""
    return prompt.strip().partition("\n")[0].startswith(DIRECTIVE)


def parse(prompt: str) -> CapAsk | None:
    """The session-cap ask ``prompt`` holds, or ``None`` for an ordinary ask.

    Raises:
        ValueError: The directive is glued to a field, carries a field other
            than one each of ``max-cap=`` and ``hold=`` within bounds, or no
            ``/session`` ask follows it.
    """
    if not is_session_cap_ask(prompt):
        return None
    first, _, rest = prompt.strip().partition("\n")
    words = first.split()
    if words[0] != DIRECTIVE:
        raise ValueError(f"{DIRECTIVE} must stand alone, then a space before its fields; got {words[0]}")
    fields: dict[str, str] = {}
    for word in words[1:]:
        name = next((f for f in (MAX_CAP_FIELD, HOLD_FIELD) if word.startswith(f)), None)
        if name is None or name in fields:
            raise ValueError(f"{DIRECTIVE} takes at most one each of {MAX_CAP_FIELD}<n> and {HOLD_FIELD}<seconds>, got {words[1:]}")
        fields[name] = word[len(name) :]
    max_cap = _whole(MAX_CAP_FIELD, fields.get(MAX_CAP_FIELD), DEFAULT_MAX_CAP, 1, MAX_MAX_CAP)
    hold = _whole(HOLD_FIELD, fields.get(HOLD_FIELD), DEFAULT_HOLD_SECONDS, MIN_HOLD_SECONDS, MAX_HOLD_SECONDS)
    ask = rest.strip()
    if not ask:
        raise ValueError(f"{DIRECTIVE} needs the ask on the lines after it")
    if not _opens_a_session(ask):
        raise ValueError(
            f"{DIRECTIVE}: the ask must open a session with a request (/session <ask>), or the gateway starts "
            f"no task and the cap never applies to it; got {ask.splitlines()[0]!r}"
        )
    return CapAsk(prompt=ask, max_cap=max_cap, hold_seconds=hold)


#: The gateway's /session arguments that start no task (a2a/lib/chattext/commands.go:
#: IsSessionOff and stopWords), compared after its Normalize.
_SESSION_NO_TASK = frozenset({"off", "stop", "cancel", "abort"})


def _normalize(text: str) -> str:
    """chattext.Normalize: lower case, letters and digits kept, apostrophes and other
    punctuation dropped, runs of whitespace one space."""
    out: list[str] = []
    last_space = True
    for ch in text.strip().lower():
        if "a" <= ch <= "z" or "0" <= ch <= "9":
            out.append(ch)
            last_space = False
        elif ch == "'":
            last_space = False
        elif ch in " \t\n":
            if not last_space:
                out.append(" ")
            last_space = True
    return "".join(out).strip()


def _opens_a_session(ask: str) -> bool:
    """Whether the gateway's sessionCommand would start a task for ask: the word /session
    (case-insensitive, then whitespace or the end; chattext.slashWord), followed by a request
    that is neither empty, "off", nor a stop word (a2a/gateway/gateway.go sessionCommand)."""
    text = ask.strip()
    if not text.startswith("/"):
        return False
    word, _, rest = text[1:].partition(next((c for c in text[1:] if c.isspace()), "\0"))
    if word.lower() != "session":
        return False
    rest = rest.strip()
    return bool(rest) and _normalize(rest) not in _SESSION_NO_TASK


def _whole(field: str, value: str | None, default: int, floor: int, ceiling: int) -> int:
    if value is None:
        return default
    if not value.isdigit() or not floor <= int(value) <= ceiling:
        raise ValueError(f"{DIRECTIVE}: {field} must be a whole number from {floor} to {ceiling}, got {value!r}")
    return int(value)


def filler_wait(hold_seconds: int) -> float:
    """How long the harness waits, in all, for the fillers to be working:
    the hold less its margin, so the first is still busy when the last
    starts."""
    return float(hold_seconds - HOLD_MARGIN_SECONDS)


def filler_prompt(hold_seconds: int) -> str:
    """The ask each filler conversation sends."""
    return FILLER_PROMPT.format(hold=hold_seconds)


def parse_cap(raw: str | None) -> int | None:
    """The session cap from the gateway container's ``A2A_MAX_SESSIONS``
    value as read: ``None`` for a read that failed or a value that is not a
    whole number, the gateway's default for an unset one."""
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return GATEWAY_DEFAULT_MAX_SESSIONS
    if not value.isdigit() or int(value) < 1:
        return None
    return int(value)


def precondition(cap: int | None, max_cap: int) -> str:
    """Why this install cannot evaluate the case, or ``""`` when it can."""
    if cap is None:
        return "the install's session cap (A2A_MAX_SESSIONS on the gateway Deployment) could not be read"
    if cap > max_cap:
        return (
            f"this case needs a session cap of at most {max_cap} to fill it (the {DIRECTIVE} directive's "
            f"{MAX_CAP_FIELD}); this install's is {cap}, so it is not evaluable here. Its evidence runs "
            "set spec.harness.tuning.maxSessions low on the install."
        )
    return ""


def refused_at_cap(entries: Iterable[Mapping[str, Any]]) -> str:
    """The gateway's cap refusal among a conversation's posts, or ``""``.
    Only the cap's own refusal counts; any other "not started" is not the
    queue's red."""
    for entry in entries:
        text = str(entry.get("text") or "")
        if entry.get("kind") == "post" and _CAP_REFUSAL.search(text):
            return text
    return ""


def assess(entries: Iterable[Mapping[str, Any]], task_id: str, detail: str = "") -> dict[str, Any]:
    """:data:`QUEUED_ENTRY` for the ask's conversation.

    ``completed`` when its entries show, in order, a post carrying the queued
    notice, the ``task`` entry of ``task_id`` (the task the queued turn
    started), and a post after it (the task's own: its placeholder or its
    answer). ``error`` otherwise, with why; ``detail`` is added to the
    reason, for what the harness saw that the entries do not say.
    """
    entries = [e for e in entries if isinstance(e, Mapping) and isinstance(e.get("seq"), int)]
    entries.sort(key=lambda e: e["seq"])
    notice = next((e["seq"] for e in entries if e.get("kind") == "post" and _QUEUED.search(str(e.get("text") or ""))), None)
    task = next((e["seq"] for e in entries if task_id and e.get("kind") == "task" and e.get("taskId") == task_id), None)
    after = task is not None and any(e.get("kind") == "post" and e["seq"] > task for e in entries)
    args = {"task_id": task_id or None, "notice_seq": notice, "task_seq": task}
    problems = []
    if notice is None:
        refusal = refused_at_cap(entries)
        problems.append(f"the gateway refused the turn at the cap: {refusal!r}" if refusal else "no queued notice was posted")
    if task is None:
        problems.append("the queued turn started no task")
    elif notice is not None and task < notice:
        problems.append("the task started before the queued notice")
    elif not after:
        problems.append("nothing was posted for the task after it started")
    if detail:
        problems.append(detail)
    if problems:
        return {"name": QUEUED_ENTRY, "args": args, "status": "error", "result": "; ".join(problems)}
    return {"name": QUEUED_ENTRY, "args": args, "status": "completed", "result": "queued, then started, then answered"}
