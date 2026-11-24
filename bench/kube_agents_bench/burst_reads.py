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
"""Concurrent session conversations that each fan out cluster reads: a measurement.

A prompt whose first line is :data:`DIRECTIVE` is an ask the harness sends on
several ``/session`` conversations at once, in steps of rising concurrency
(``levels=1,3,6`` by default). The rest of the prompt is that ask, and it has
to open a session (``/session ...``); a case picks one that makes the session
delegate several read-only cluster reads to the ``cluster`` AgentProfile, so
each conversation's turn waits on child tasks the dispatcher hands to that
profile's warm pool (#7014).

For each level L in turn the harness opens L conversations through the
inject door, each on its own key (:func:`conversation_key`) with its own
message id, submits them together, and follows every one to its terminal
inside ``AGENT_INJECT_TIMEOUT``. A conversation still running at that
deadline is cancelled and fails the run, and the next level starts only
once every conversation of this one has ended and each cancelled one's
session has been released (waited for up to
:data:`STRAGGLER_RELEASE_SECONDS`, else infrastructure), so levels never
overlap. Whatever happens, anything still running when the run ends is
cancelled.

What it records, per conversation: its key, its task id, its terminal state,
and the UTC times it was submitted and seen to end; per level: when it
started and ended. The record is :data:`ENTRY`'s ``args`` in the trajectory,
which is what reaches ``results_<case>_rep<N>.json`` (devops-bench writes no
``metadata`` block), and ``metadata["burst_reads"]`` for the harness log.
``bench/tools/child_queue_time.py`` reads it beside the gateway and
dispatcher logs to break the child tasks' queue time down by level.

What it grades: completion only. :data:`ENTRY` is ``completed`` when every
conversation of every level ended ``completed``, ``error`` otherwise; the
answers' content is not graded, because the case is a measurement.

WHAT IS INFRASTRUCTURE. A session cap below the highest level (or one that
cannot be read), checked before anything is sent: the gateway would queue or
refuse the extra conversations and the level would measure the cap, not the
reads. A conversation the gateway queued or refused at submit, one that never
started (no executor took it), a terminal the gateway or supervisor declared,
a door the harness could not reach, and a straggler whose cancel was not
published (the next level would overlap it). None of these say anything about
how long the reads took.

It runs on the inject transport only.
"""

from __future__ import annotations

import itertools
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from kube_agents_bench import session_cap

__all__ = [
    "DEFAULT_LEVELS",
    "DIRECTIVE",
    "ENTRY",
    "MAX_LEVEL",
    "BurstAsk",
    "ConversationRecord",
    "LevelRecord",
    "assess",
    "conversation_key",
    "infrastructure",
    "is_burst_reads_ask",
    "message_id",
    "parse",
    "precondition",
    "records_from",
    "summary",
    "utc_stamp",
]

#: The first line that makes a prompt a burst-reads ask.
DIRECTIVE = "[bench:burst-reads]"
#: The one optional field after it: the concurrency levels, comma-separated.
LEVELS_FIELD = "levels="
LEVELS_SEPARATOR = ","
#: The levels run when the directive names none.
DEFAULT_LEVELS = (1, 3, 6)
#: The highest level a directive may name: each conversation holds a session
#: pod for its turn, and the shipped session cap is ten.
MAX_LEVEL = 8
#: The trajectory entry the harness records the measurement under.
ENTRY = "harness_burst_reads"
#: How a conversation's key and message id name its level and place.
LEVEL_KEY_FORMAT = "{base}-L{level}-{index}"
#: The UTC timestamp format of every recorded time (ISO 8601, Z suffix).
UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
#: The terminal state every conversation must reach for the run to pass.
STATE_COMPLETED = "completed"


@dataclass(frozen=True)
class BurstAsk:
    """The ask each conversation sends, and the levels to run it at."""

    prompt: str
    levels: tuple[int, ...]


def is_burst_reads_ask(prompt: str) -> bool:
    """Whether ``prompt``'s first non-blank line opens with the directive."""
    return prompt.strip().partition("\n")[0].startswith(DIRECTIVE)


def parse(prompt: str) -> BurstAsk | None:
    """The burst-reads ask ``prompt`` holds, or ``None`` for an ordinary ask.

    Raises:
        ValueError: The directive is glued to a field, carries a field other
            than one ``levels=``, the levels are not ascending whole numbers
            from 1 to :data:`MAX_LEVEL`, or no ``/session`` ask follows it.
    """
    if not is_burst_reads_ask(prompt):
        return None
    first, _, rest = prompt.strip().partition("\n")
    words = first.split()
    if words[0] != DIRECTIVE:
        raise ValueError(f"{DIRECTIVE} must stand alone, then a space before its fields; got {words[0]}")
    raw_levels: str | None = None
    for word in words[1:]:
        if not word.startswith(LEVELS_FIELD) or raw_levels is not None:
            raise ValueError(f"{DIRECTIVE} takes at most one {LEVELS_FIELD}<n>,<n>,... field, got {words[1:]}")
        raw_levels = word[len(LEVELS_FIELD) :]
    levels = DEFAULT_LEVELS if raw_levels is None else _levels(raw_levels)
    ask = rest.strip()
    if not ask:
        raise ValueError(f"{DIRECTIVE} needs the ask on the lines after it")
    # The gateway's own test for a /session that starts a task, shared with
    # the session-cap directive so the two cannot read one ask two ways.
    if not session_cap._opens_a_session(ask):
        raise ValueError(
            f"{DIRECTIVE}: the ask must open a session with a request (/session <ask>), or no session "
            f"pod runs it and nothing delegates; got {ask.splitlines()[0]!r}"
        )
    return BurstAsk(prompt=ask, levels=levels)


def _levels(raw: str) -> tuple[int, ...]:
    parts = raw.split(LEVELS_SEPARATOR)
    if not all(p.isdigit() and 1 <= int(p) <= MAX_LEVEL for p in parts):
        raise ValueError(
            f"{DIRECTIVE}: {LEVELS_FIELD} must list whole numbers from 1 to {MAX_LEVEL}, "
            f"separated by commas, got {raw!r}"
        )
    levels = tuple(int(p) for p in parts)
    if any(b <= a for a, b in itertools.pairwise(levels)):
        raise ValueError(f"{DIRECTIVE}: {LEVELS_FIELD} must be strictly ascending, got {raw!r}")
    return levels


def precondition(cap: int | None, levels: Iterable[int]) -> str:
    """Why this install cannot run the levels, or ``""`` when it can."""
    highest = max(levels)
    if cap is None:
        return "the install's session cap (A2A_MAX_SESSIONS on the gateway Deployment) could not be read"
    if highest > cap:
        return (
            f"level {highest} needs {highest} session workers at once and this install's session cap is "
            f"{cap}, so the gateway would queue or refuse the extra conversations; the levels were not run"
        )
    return ""


def conversation_key(base: str, level: int, index: int) -> str:
    """The conversation key of the ``index``-th conversation of ``level``."""
    return LEVEL_KEY_FORMAT.format(base=base, level=level, index=index)


def message_id(base: str, level: int, index: int) -> str:
    """The backend message id of that conversation's opening POST."""
    return LEVEL_KEY_FORMAT.format(base=base, level=level, index=index)


def utc_stamp(when: datetime | None = None) -> str:
    """``when`` (now, by default) as an ISO 8601 UTC timestamp with a Z."""
    return (when or datetime.now(UTC)).astimezone(UTC).strftime(UTC_FORMAT)


@dataclass
class ConversationRecord:
    """What one conversation of a level did."""

    level: int
    index: int
    conversation: str
    task_id: str = ""
    submitted_at: str = ""
    ended_at: str = ""
    terminal_state: str = ""
    outcome: str = ""
    # The door's refusal code when it started nothing; queued is the session
    # cap's line, any other code a refusal.
    refusal: str = ""
    queued: bool = False
    refused: bool = False
    # Whether the harness cancelled it (a straggler at the deadline, or
    # anything still running when the run ended), and whether the door
    # published that cancel.
    cancelled: bool = False
    cancel_published: bool = False
    # Whether it was still running at AGENT_INJECT_TIMEOUT. Such a
    # conversation fails the run even if it ended completed during the
    # cancel's settle: it waited out the budget.
    timed_out: bool = False
    # Why this conversation's run cannot be graded, or "".
    infrastructure: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "index": self.index,
            "conversation": self.conversation,
            "task_id": self.task_id or None,
            "submitted_at": self.submitted_at or None,
            "ended_at": self.ended_at or None,
            "terminal_state": self.terminal_state or None,
            "outcome": self.outcome or None,
            "refusal": self.refusal or None,
            "queued": self.queued,
            "refused": self.refused,
            "cancelled": self.cancelled,
            "cancel_published": self.cancel_published,
            "timed_out": self.timed_out,
            "infrastructure": self.infrastructure or None,
        }


#: How long the harness waits, after a level, for its cancelled stragglers'
#: sessions to be released before the next level starts.
STRAGGLER_RELEASE_SECONDS = 120.0


@dataclass
class LevelRecord:
    """One level: its concurrency, when it ran, and its conversations."""

    level: int
    started_at: str = ""
    ended_at: str = ""
    wall_seconds: float | None = None
    conversations: list[ConversationRecord] = field(default_factory=list)

    def states(self) -> Counter[str]:
        return Counter(c.terminal_state or "none" for c in self.conversations)

    def completed(self) -> int:
        return sum(c.terminal_state == STATE_COMPLETED for c in self.conversations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "started_at": self.started_at or None,
            "ended_at": self.ended_at or None,
            "wall_seconds": None if self.wall_seconds is None else round(self.wall_seconds, 3),
            "completed": self.completed(),
            "states": dict(sorted(self.states().items())),
            "conversations": [c.to_dict() for c in self.conversations],
        }


def infrastructure(levels: Iterable[LevelRecord]) -> str:
    """The first conversation's reason its run cannot be graded, named, or ``""``."""
    for level in levels:
        for conv in level.conversations:
            if conv.infrastructure:
                return f"level {level.level}, conversation {conv.conversation}: {conv.infrastructure}"
    return ""


def summary(levels: Iterable[LevelRecord]) -> str:
    """One plain line per level: how many completed, the terminal states, the wall time."""
    lines = []
    for level in levels:
        states = ", ".join(f"{state} {n}" for state, n in sorted(level.states().items()))
        wall = "?" if level.wall_seconds is None else f"{level.wall_seconds:.0f}s"
        lines.append(
            f"level {level.level}: {level.completed()} of {len(level.conversations)} conversations completed "
            f"({states}) in {wall}"
        )
    return "\n".join(lines)


def assess(levels: list[LevelRecord], planned: Iterable[int]) -> dict[str, Any]:
    """:data:`ENTRY`: ``completed`` when every planned level ran and every
    conversation in it ended completed, ``error`` otherwise with which did
    not. ``args`` carries the whole record."""
    planned = tuple(planned)
    problems = []
    ran = tuple(level.level for level in levels)
    if ran != planned:
        problems.append(f"levels {list(planned)} were planned and {list(ran)} ran")
    for level in levels:
        for conv in level.conversations:
            if conv.timed_out:
                problems.append(
                    f"level {level.level} conversation {conv.index} ({conv.task_id or 'no task'}) was still "
                    "running at the deadline"
                )
            elif conv.terminal_state != STATE_COMPLETED:
                problems.append(
                    f"level {level.level} conversation {conv.index} ({conv.task_id or 'no task'}) ended "
                    f"{conv.terminal_state or conv.outcome or 'without a terminal'}"
                )
    args = {"planned_levels": list(planned), "levels": [level.to_dict() for level in levels]}
    if problems:
        return {"name": ENTRY, "args": args, "status": "error", "result": "; ".join(problems)}
    return {"name": ENTRY, "args": args, "status": "completed", "result": summary(levels)}


def records_from(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The level dicts of a recorded :data:`ENTRY`'s ``args`` (or of
    ``metadata["burst_reads"]``, which holds the same), ``[]`` when absent."""
    levels = data.get("levels")
    return [lv for lv in levels if isinstance(lv, Mapping)] if isinstance(levels, list) else []
