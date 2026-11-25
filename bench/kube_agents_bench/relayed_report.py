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
"""Two replies under a report the gateway relayed into the conversation (#6870).

A prompt whose first line is :data:`DIRECTIVE` is three turns on one
conversation. ``opening`` is the first: it leads to a report that the A2A
gateway's notify route posts into that conversation (``a2a/gateway/notify.go``,
``Notifier.reply``), which is the one path on which the gateway keeps the
report (``KeepReport``) and attaches it to the next human turn as a
``pushedReport`` part. Once the post has landed, ``reply1`` asks about a fact
only the report holds (``fact1``), and ``reply2``, a second human turn on the
same conversation and the same session pod, asks about another
(``fact2``). The worker frames a pushed report into its harness session once,
so ``reply2`` answering shows the report survived in that session's history
rather than riding along on the turn that carried it.

This is a different row from :mod:`pushed_report`, which plants the
hermes-bridge's fallback row in the session store. Here the gateway's own
relay has to run. The harness drives the three turns through
:class:`RelayTransport`, a seam any way of reaching a conversation a notify
route serves can implement, so nothing here assumes which. The one today is
the inject door (``harness._InjectRelayTransport``): on an eval install the
gateway's inject notify route (``chat.notify.inject``) relays a card's report
into the ``inject:`` conversation the card was filed from as a transcript post
the door marks ``report`` with ``replyTo`` naming the task whose turn filed
it, keeps it by that root, and attaches it to the next human turn there as a
``pushedReport`` part. On a lane no transport serves the harness reports the
case as infrastructure (``harness._relay_transport``).

The run writes two trajectory entries (``cases.HARNESS_WRITTEN_ENTRIES``
names both):

- :data:`POSTED_ENTRY`, ``completed``, once the report post landed in the
  conversation. A run where it never lands within
  :data:`REPORT_POST_WAIT_SECONDS` is infrastructure, not a model failure:
  the path under test never ran.
- :data:`REPLIES_ENTRY`, ``completed`` when both replies were answered and
  each answer carries its fact (matched as ``report_contains`` matches a
  phrase: case, emphasis and whitespace aside), ``error`` otherwise. A reply
  that lacks its fact is graded: the report path ran and the answer did not
  use it. A case's ``report_contains`` checks the facts over the merged
  answer too.

A transport failure (:class:`RelayUnavailable`), a reply that ran on a
different session than the other, or the report post never landing, is
:class:`RelayInfrastructure`, which the harness records as infrastructure.

Nothing here imports ``devops_bench`` at module scope, like
:mod:`pushed_report`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from devops_bench.agents import AgentResult

__all__ = [
    "DIRECTIVE",
    "POSTED_ENTRY",
    "REPLIES_ENTRY",
    "REPLY_SUFFIX",
    "REPORT_POST_WAIT_SECONDS",
    "RelayInfrastructure",
    "RelayTransport",
    "RelayTurn",
    "RelayUnavailable",
    "Relayed",
    "ReportPost",
    "carries",
    "is_reply_under_relayed_report",
    "merge",
    "parse",
    "run",
]

#: The first line of a relayed-report prompt. The ``key: value`` lines under
#: it are its fields, each on one line; blank lines are skipped, any other
#: line is an authoring error.
DIRECTIVE = "[bench:reply-under-relayed-report]"
_FIELDS = ("opening", "reply1", "fact1", "reply2", "fact2")
#: The trajectory entries the run writes.
POSTED_ENTRY = "harness_relay_report_posted"
REPLIES_ENTRY = "harness_relay_replies"
#: The suffix a reply's message id takes after the opening's, numbered per
#: reply (``/relay-reply1``, ``/relay-reply2``), on a transport whose door
#: dedupes on the message id.
REPLY_SUFFIX = "/relay-reply"
#: How long the run waits, after the opening turn's answer, for the report
#: post to land in the conversation, in seconds. The opening's card has to
#: run and report and the notify route has to post it; past this the path
#: under test is taken not to have run.
REPORT_POST_WAIT_SECONDS = 900.0
#: Between the two answers in the merged result the grader sees.
ANSWER_SEPARATOR = "\n\n"


class RelayUnavailable(RuntimeError):
    """A :class:`RelayTransport` could not do what it was asked for a reason
    that is not the agent's (the door or the chat surface could not be
    reached, a turn started nothing). Raised by implementations."""


class RelayInfrastructure(RuntimeError):
    """The run cannot be graded: the harness records it as infrastructure."""


@dataclass(frozen=True)
class Relayed:
    """The five fields of a relayed-report prompt."""

    opening: str
    reply1: str
    fact1: str
    reply2: str
    fact2: str


@dataclass
class RelayTurn:
    """One human turn followed to its answer.

    ``result`` is the turn as the grader would see it alone (output,
    ``final_message``, trajectory, tokens, errors). ``conversation`` is the
    transport's own spelling of the conversation the turn went to;
    ``session`` names the session pod (or the transport's nearest stand-in)
    that answered it, ``""`` when the transport cannot tell; ``task_id`` the
    task the turn started.
    """

    result: AgentResult
    conversation: str
    task_id: str = ""
    session: str = ""

    @property
    def answer(self) -> str:
        return str(self.result.metadata.get("final_message") or self.result.output or "")


@dataclass(frozen=True)
class ReportPost:
    """The report post, as the transport saw it land in the conversation.

    ``position`` is where in the conversation it landed (the transport's
    sequence number or timestamp, as text); ``evidence`` says how the
    transport knew this post was the relayed report (a flag on the entry,
    the notify route's marker, a match on the report's text), for the record.
    """

    conversation: str
    text: str
    position: str = ""
    evidence: str = ""


@runtime_checkable
class RelayTransport(Protocol):
    """What the run needs from a way to reach a conversation a notify route serves.

    ``conversation`` is the harness's key for the run (one per case
    repetition); an implementation maps it onto its own conversation (an
    inject key, a console thread, a Slack thread) and keeps using the same one
    for every call with that key. Each method raises
    :class:`RelayUnavailable` for a failure that is not the agent's.
    """

    #: Named in the run's records and in its infrastructure messages.
    name: str

    def open_turn(self, conversation: str, text: str) -> RelayTurn:
        """Send ``text`` as the conversation's first human turn and follow it
        to its answer."""
        ...

    def wait_for_report_post(self, conversation: str, timeout: float) -> ReportPost | None:
        """Wait up to ``timeout`` seconds for the notify route's report post
        to land in the conversation; ``None`` when none did."""
        ...

    def reply(self, conversation: str, text: str) -> RelayTurn:
        """Send ``text`` as a further human turn on the conversation (the
        gateway attaches a kept report to it) and follow it to its answer."""
        ...


def is_reply_under_relayed_report(prompt: str) -> bool:
    """Whether the prompt's first non-blank line opens with the directive.

    A near miss (fields glued onto the directive's line) is one here too, so
    :func:`parse` refuses it rather than the ask reaching the agent as text.
    """
    return prompt.strip().partition("\n")[0].startswith(DIRECTIVE)


def parse(prompt: str) -> Relayed | None:
    """The relayed-report prompt ``prompt`` holds, or ``None`` for an ordinary ask.

    Raises :class:`ValueError` for a prompt missing a field, repeating one,
    or carrying a line that is not one: a case authoring error, not a run.
    """
    if not is_reply_under_relayed_report(prompt):
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
    return Relayed(**fields)


_EMPHASIS = re.compile(r"[*_`]+")
_SPACE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _SPACE.sub(" ", _EMPHASIS.sub("", text)).strip().casefold()


def carries(answer: str, fact: str) -> bool:
    """Whether ``answer`` states ``fact``: a substring match with case,
    Markdown emphasis and whitespace runs aside, as ``report_contains``
    matches a required phrase."""
    want = _normalize(fact)
    return bool(want) and want in _normalize(answer)


def _turn(transport: RelayTransport, label: str, call: Any, *args: Any) -> Any:
    try:
        return call(*args)
    except RelayUnavailable as exc:
        raise RelayInfrastructure(f"{DIRECTIVE}: {transport.name}: the {label} failed: {exc}") from exc


def run(transport: RelayTransport, relayed: Relayed, conversation: str) -> AgentResult:
    """Open, wait for the report post, reply twice; one result for the grader.

    Raises :class:`RelayInfrastructure` when the run cannot be graded: a
    transport failure, no report post within :data:`REPORT_POST_WAIT_SECONDS`,
    or the two replies answered on different sessions (``reply2`` then shows
    nothing about the session's history).
    """
    opening = _turn(transport, "opening turn", transport.open_turn, conversation, relayed.opening)
    post = _turn(
        transport, "wait for the report post", transport.wait_for_report_post, conversation, REPORT_POST_WAIT_SECONDS
    )
    if post is None:
        raise RelayInfrastructure(
            f"{DIRECTIVE}: {transport.name}: no report was posted into {opening.conversation} within "
            f"{REPORT_POST_WAIT_SECONDS:.0f}s of the opening turn's answer, so the relay under test never ran"
        )
    posted = posted_entry(transport.name, post, relayed)
    first = _turn(transport, "first reply", transport.reply, conversation, relayed.reply1)
    second = _turn(transport, "second reply", transport.reply, conversation, relayed.reply2)
    if first.session and second.session and first.session != second.session:
        raise RelayInfrastructure(
            f"{DIRECTIVE}: {transport.name}: the second reply ran on session {second.session}, the first on "
            f"{first.session}; the case needs both on one reused session to show the report survived in its history"
        )
    return merge(opening, first, second, posted, replies_entry(relayed, first, second))


def posted_entry(transport: str, post: ReportPost, relayed: Relayed) -> dict[str, Any]:
    """:data:`POSTED_ENTRY`: the report post landed. Which facts the post
    itself carries is recorded, not graded: the report's text is the
    notifier's."""
    return {
        "name": POSTED_ENTRY,
        "args": {"transport": transport, "conversation": post.conversation},
        "result": {
            "position": post.position,
            "evidence": post.evidence,
            "facts_in_post": [carries(post.text, relayed.fact1), carries(post.text, relayed.fact2)],
        },
        "status": "completed",
    }


def replies_entry(relayed: Relayed, first: RelayTurn, second: RelayTurn) -> dict[str, Any]:
    """:data:`REPLIES_ENTRY`: ``completed`` when each reply's answer carries
    its fact, ``error`` naming each that does not."""
    replies = []
    problems = []
    for label, turn, fact in (("reply1", first, relayed.fact1), ("reply2", second, relayed.fact2)):
        answer = turn.answer
        found = carries(answer, fact)
        replies.append({"turn": label, "task_id": turn.task_id or None, "session": turn.session or None, "answered": bool(answer.strip()), "fact_found": found})
        if not answer.strip():
            problems.append(f"{label} was not answered")
        elif not found:
            problems.append(f"{label}'s answer does not carry {fact!r}")
    entry: dict[str, Any] = {"name": REPLIES_ENTRY, "args": {"conversation": first.conversation, "replies": replies}}
    if problems:
        return {**entry, "status": "error", "result": "; ".join(problems)}
    return {**entry, "status": "completed", "result": "both replies answered from the report"}


def merge(opening: RelayTurn, first: RelayTurn, second: RelayTurn, posted: dict, replies: dict) -> AgentResult:
    """One result for the three turns, graded on the two replies' answers.

    ``output`` and ``final_message`` are both replies' answers, the first's
    then the second's; the opening's answer is kept in metadata. The
    trajectory is the opening turn's, :data:`POSTED_ENTRY`, both replies',
    then :data:`REPLIES_ENTRY`. Errors and tokens are all three turns', as
    :func:`pushed_report.merge` sums two.
    """
    # devops_bench-bound, so imported here, where a result exists.
    from kube_agents_bench import worker_trajectory
    from kube_agents_bench.card_wake import _add_tokens

    turns = (opening.result, first.result, second.result)

    def _no_worker_calls(trajectory: list) -> bool:
        return any(isinstance(e, dict) and e.get("name") == worker_trajectory.NO_WORKER_CALLS_ENTRY for e in trajectory)

    tokens = dict(second.result.tokens)
    _add_tokens(tokens, first.result.tokens)
    _add_tokens(tokens, opening.result.tokens)
    metadata = {**opening.result.metadata, **first.result.metadata, **second.result.metadata}
    answer = ANSWER_SEPARATOR.join(a for a in (first.answer, second.answer) if a)
    output = ANSWER_SEPARATOR.join(o for o in (first.result.output, second.result.output) if o)
    metadata["final_message"] = answer
    metadata["relayed_report"] = {
        "opening_final_message": opening.answer,
        "opening_task_id": opening.task_id or None,
        "reply_task_ids": [first.task_id or None, second.task_id or None],
        "conversation": first.conversation,
    }
    trajectory = [*opening.result.trajectory, posted, *first.result.trajectory, *second.result.trajectory, replies]
    # A no-worker-calls entry speaks for the cards of the turn that wrote it.
    if not all(_no_worker_calls(t.trajectory) for t in turns):
        trajectory = [e for e in trajectory if not (isinstance(e, dict) and e.get("name") == worker_trajectory.NO_WORKER_CALLS_ENTRY)]
    return type(second.result)(
        output=output,
        trajectory=trajectory,
        tokens=tokens,
        errors=[*opening.result.errors, *first.result.errors, *second.result.errors],
        metadata=metadata,
    )
