"""Replay the wake a blocked or failed card sends the front door.

The front door's reply to a card's wake is a turn the harness's own asks never
reach. Two replays produce one. A prompt whose first line is
:data:`QUESTION_DIRECTIVE` or :data:`FAILURE_DIRECTIVE` is a replay instead of
an ask; :func:`parse` reads it.

**A specialist's Slack question.** A card that blocks on ``needs_input`` wakes
the conversation that filed it, and the front door's reply follows
``agents/chat/SOUL.md`` §2, step 5: exactly ``[SILENT]`` when the wake says the
question is already posted, unless something else in the notification needs
saying, otherwise the question in its own words. The user's answer then goes
to the card
with ``kanban_comment`` and ``kanban_unblock`` (§1.5, **Unblock**). On an image
whose gateway carries a Slack moments module (``gateway/slack_ux_moments.py``)
with ``KAGE_SLACK_UX`` on, that module's ``needs_you`` posts the question in
the Slack thread itself before the wake is built. None of that is reachable
from the harness. The API server never subscribes a card the way a Slack
thread does, and on an install with no Slack adapter the notifier skips a
Slack subscription before it builds any text.

For :data:`QUESTION_DIRECTIVE`, the in-pod script files a card on the agent's
board and blocks it on ``needs_input`` with the case's question, posts the
question through ``needs_you`` with a stub Slack client where the image has
that module, and builds the wake with the image's own notifier. An image that
has the module but posts nothing is broken, not red. The harness then sends
that wake as the first turn and the case's typed answer as the second, on one
conversation (:meth:`KubeAgentsHarness._execute_card_wake`).

**A card that blocked or gave up.** On the API server a ``blocked`` or
``gave_up`` card wakes the conversation that filed it through the notifier's
self-post (``deploy/docker/patches/kanban_notifier.py``, ``wake_kinds_for``: a
non-push adapter always wakes for the failure kinds), and the reply to that
wake is the user's only announcement of the failure (``agents/chat/SOUL.md``
§2, step 5). The harness never reads it: its own poll turns ask for a status
recital, and no prompt makes a specialist fail every time. For
:data:`FAILURE_DIRECTIVE` the in-pod script files the card, blocks it with the case's reason (``outcome:
blocked``) or trips its failure breaker with it as the error (``outcome:
gave_up``, the event the dispatcher records when its retries run out), and
builds the wake with the image's notifier through a non-push stub adapter, as
the API server's is. The harness sends that wake as the run's only turn. The
wake names the card but not the reason, so the front door reads the card
(``kanban_show``) as it would in a real thread.

What neither replay reproduces: the turns arrive on the run's own
``/v1/responses`` conversation rather than the session that filed the card, so
the front door has not seen the ask that led to it; for a question, the flag is
set in the script's process whatever the install's setting and no message
reaches Slack. The wake under test is the notifier's, built by the image's own
code.

Either replay's card stays unassigned on the board, so unblocking it hands
no worker anything; the notifier is given a copy naming
:data:`WAKE_ASSIGNEE`, as a delegated card would carry, so the wake does not
read ``@None``.

Every replay's card carries a key minted for the run
(:data:`REPLAY_KEY_PREFIX`, the card's ``idempotency_key``). :func:`archive`
reads the card's status and comments, then, in a second exec, archives every
card carrying the key, so a card filed by a plant whose ``kubectl exec`` timed
out is swept as well, and an archive that fails keeps what was read. What it read rides on the run's trajectory as a harness entry
(:data:`SETTLED_ENTRY`), because devops-bench persists the trajectory and not
the metadata; the ``replay_card`` verifier grades it.

Unlike :mod:`kube_agents_bench.board`, a failed plant is not best effort: a
run that never saw the wake grades nothing. :func:`plant` raises
:class:`ReplayUnavailable`, which the harness records as infrastructure, when
the script never ran to completion, and :class:`ReplayBroken`, which it
records as an error, when the script ran in the image and failed there.
"""

from __future__ import annotations

import json
import shlex
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from devops_bench.agents import AgentResult

from kube_agents_bench.worker_trajectory import FALLBACK_PYTHON, HERMES_PYTHON

__all__ = [
    "FAILURE_DIRECTIVE",
    "QUESTION_DIRECTIVE",
    "SETTLED_ENTRY",
    "Failure",
    "Planted",
    "Replay",
    "ReplayBroken",
    "ReplayUnavailable",
    "Settled",
    "archive",
    "merge",
    "parse",
    "plant",
    "tag",
]

# First lines of the two replay prompts. The ``key: value`` lines under each
# are its fields (:data:`_QUESTION_FIELDS`, :data:`_FAILURE_FIELDS`); anything
# else is ignored.
QUESTION_DIRECTIVE = "[bench:slack-question-wake]"
FAILURE_DIRECTIVE = "[bench:card-failure-wake]"
_QUESTION_FIELDS = ("title", "question", "options", "answer")
_FAILURE_FIELDS = ("title", "body", "outcome", "reason")
OPTION_SEPARATOR = "|"

# The name of the harness trajectory entry carrying the card as the run left
# it: ``args.card`` is the card, ``result`` is ``Settled.as_metadata()`` or
# ``None`` when the card could not be read.
SETTLED_ENTRY = "card_wake_settled"

# How the plant script ends the card. ``question`` is a ``needs_input`` block;
# the failure outcomes are a failure prompt's ``outcome:`` values.
OUTCOME_QUESTION = "question"
OUTCOME_BLOCKED = "blocked"
OUTCOME_GAVE_UP = "gave_up"
FAILURE_OUTCOMES = (OUTCOME_BLOCKED, OUTCOME_GAVE_UP)

# Line the in-pod scripts print before their JSON. A reply without it means
# the script never ran to completion.
REPLAY_PRESENT = "__BENCH_CARD_WAKE__"

# A replay card's ``idempotency_key`` is this and a fresh hex suffix per run.
REPLAY_KEY_PREFIX = "devops-bench-card-wake-"

# Where the image installs hermes, and the scripts directory slack_ux_moments
# imports its layout modules from.
HERMES_ROOT = "/opt/hermes"
SCRIPTS_DIR = "/opt/defaults/scripts"

# slack_presenter.FLAG_ENV and a value it reads as on.
SLACK_UX_FLAG = "KAGE_SLACK_UX"
FLAG_ON = "1"

# Who the card says filed it, who its wake says was working it, and the stub
# thread a question is posted in. Slack never sees the thread.
CARD_CREATOR = "devops-bench"
WAKE_ASSIGNEE = "platform"
STUB_CHANNEL = "C0BENCHWAKE"
STUB_THREAD = "1700000000.000100"

# Runs inside the agent container. Positional arguments: the sentinel, the
# hermes root, the scripts directory, the flag, its on value, the creator,
# the stub channel, the stub thread, the card title, its body, the block
# reason or failure error, the outcome, the wake's assignee and the run's key.
# The card is archived again if anything after filing it fails.
_PLANT_SCRIPT = r"""
import asyncio, dataclasses, json, os, sys

(SENTINEL, HERMES_ROOT, SCRIPTS, FLAG, ON, CREATOR,
 CHANNEL, THREAD, TITLE, BODY, REASON, OUTCOME, ASSIGNEE, KEY) = sys.argv[1:15]
STUB_TS = "1700000000.000200"
out = {"card": None, "wake": None, "posted": 0, "error": None}


class _Client:
    def __init__(self, posts):
        self.posts = posts

    async def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ok": True, "ts": STUB_TS}

    async def chat_update(self, **kwargs):
        return {"ok": True}


class _Adapter:
    def __init__(self):
        self.posts = []

    def _get_client(self, chat_id, team_id=None):
        return _Client(self.posts)


class _ApiServerAdapter:
    # The API server's adapter has no push channel: its wake is a self-post.
    supports_async_delivery = False

    def __init__(self):
        self.posts = []


conn = None
try:
    sys.path[:0] = [HERMES_ROOT, SCRIPTS]
    os.environ[FLAG] = ON
    from hermes_cli import kanban_db as kb
    from gateway import kanban_watchers_notifier as notifier
    try:
        from hermes_cli.kanban_db_connect import connect
    except ImportError:
        connect = kb.connect
    try:
        from gateway import slack_ux_moments as moments
    except ImportError:
        moments = None
    conn = connect()
    card = kb.create_task(conn, title=TITLE, body=BODY, created_by=CREATOR, idempotency_key=KEY)
    out["card"] = card
    if OUTCOME == "gave_up":
        from hermes_cli import kanban_db_dispatch as dispatch
        # force_trip records gave_up and parks the card on its first failure,
        # where the dispatcher would after exhausting its retries.
        if not dispatch._record_task_failure(conn, card, REASON, outcome="crashed", force_trip=True):
            raise RuntimeError("card %s would not give up" % card)
        kind = "gave_up"
    else:
        block_kind = "needs_input" if OUTCOME == "question" else None
        if not kb.block_task(conn, card, reason=REASON, kind=block_kind):
            raise RuntimeError("card %s would not block" % card)
        kind = "blocked"
    events = [e for e in kb.list_events(conn, card) if e.kind == kind][-1:]
    if not events:
        raise RuntimeError("card %s has no %s event" % (card, kind))
    if OUTCOME == "question":
        sub = {"task_id": card, "platform": "slack", "chat_id": CHANNEL, "thread_id": THREAD,
               "delivery_mode": "notify+wake"}
        adapter = _Adapter()
        if moments is not None:
            asyncio.run(moments.needs_you(adapter, sub, events[0].payload or {}, events[0].id))
            if not adapter.posts:
                raise RuntimeError("the image has slack_ux_moments but it posted nothing for card %s" % card)
    else:
        sub = {"task_id": card, "platform": "api_server", "chat_id": CHANNEL, "thread_id": "",
               "delivery_mode": "notify+wake"}
        adapter = _ApiServerAdapter()
    # The board's card stays unassigned, so an unblock hands no worker
    # anything; the notifier's copy names the assignee a delegated card carries.
    task = dataclasses.replace(kb.get_task(conn, card), assignee=ASSIGNEE)
    wake = notifier._KanbanNotification(
        None, {"sub": sub, "task": task, "board": kb.DEFAULT_BOARD, "events": events},
        platform_cls=None, sub_fail_counts={})
    wake.adapter = adapter
    wake.is_push_adapter = OUTCOME == "question"
    for ev in events:
        wake.format_event(ev)
    wake.build_wake_text()
    if not wake.synth:
        raise RuntimeError("the notifier built no wake for card %s" % card)
    out.update(wake=wake.synth, posted=len(adapter.posts))
except Exception as exc:
    out["error"] = "%s: %s" % (type(exc).__name__, exc)
    if conn is not None and out["card"]:
        try:
            kb.archive_task(conn, out["card"])
        except Exception:
            pass
print(SENTINEL)
print(json.dumps(out))
"""

# Reads the run's newest card as the run left it. Positional arguments: the
# sentinel, the hermes root and the run's key. ``card`` is ``None`` when no
# card that is not yet archived carries the key.
_READ_SCRIPT = r"""
import json, sys

SENTINEL, HERMES_ROOT, KEY = sys.argv[1:4]
sys.path.insert(0, HERMES_ROOT)
out = {"card": None, "status": None, "comments": [], "error": None}
try:
    from hermes_cli import kanban_db as kb
    try:
        from hermes_cli.kanban_db_connect import connect
    except ImportError:
        connect = kb.connect
    conn = connect()
    cards = [row[0] for row in conn.execute(
        "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
        "ORDER BY created_at DESC", (KEY,))]
    if cards:
        task = kb.get_task(conn, cards[0])
        out["card"] = cards[0]
        out["status"] = task.status if task else None
        out["comments"] = [{"author": c.author, "body": c.body} for c in kb.list_comments(conn, cards[0])]
except Exception as exc:
    out["error"] = "%s: %s" % (type(exc).__name__, exc)
print(SENTINEL)
print(json.dumps(out))
"""

# Archives every card carrying the run's key. Positional arguments: the
# sentinel, the hermes root and the run's key.
_ARCHIVE_SCRIPT = r"""
import json, sys

SENTINEL, HERMES_ROOT, KEY = sys.argv[1:4]
sys.path.insert(0, HERMES_ROOT)
out = {"archived": False, "cards": [], "error": None}
try:
    from hermes_cli import kanban_db as kb
    try:
        from hermes_cli.kanban_db_connect import connect
    except ImportError:
        connect = kb.connect
    conn = connect()
    cards = [row[0] for row in conn.execute(
        "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived'", (KEY,))]
    out["cards"] = cards
    out["archived"] = all([bool(kb.archive_task(conn, card)) for card in cards])
except Exception as exc:
    out["error"] = "%s: %s" % (type(exc).__name__, exc)
print(SENTINEL)
print(json.dumps(out))
"""


class ReplayUnavailable(RuntimeError):
    """The plant script never ran to completion in the pod: infrastructure."""


class ReplayBroken(RuntimeError):
    """The plant script ran in the image and failed there: the image's fault, not the cluster's."""


@dataclass(frozen=True)
class Replay:
    """A replay prompt's fields: the card, its question and the user's answer."""

    title: str
    question: str
    options: tuple[str, ...]
    answer: str

    @property
    def reason(self) -> str:
        """The block reason as a specialist writes it: the question, then a ``- `` line per choice."""
        return "\n".join([self.question, *(f"- {option}" for option in self.options)])


@dataclass(frozen=True)
class Failure:
    """A failure prompt's fields: the card, its body, how it ended and why."""

    title: str
    body: str
    outcome: str
    reason: str


@dataclass(frozen=True)
class Planted:
    """What the plant left on the board: the card, its wake, how many posts the stub took, and the run's key."""

    card: str
    wake: str
    posted: int
    key: str = ""


@dataclass(frozen=True)
class Settled:
    """The replay's card as the run left it, read by :func:`archive` before it archives it.

    ``archived`` says whether the archive that followed was confirmed; it is
    not part of what the card says, so it is left out of comparisons and of
    :meth:`as_metadata`.
    """

    status: str | None
    comments: tuple[dict, ...] = ()
    archived: bool = field(default=False, compare=False)

    def as_metadata(self) -> dict:
        return {"status": self.status, "comments": [dict(c) for c in self.comments]}


def _fields(lines: list[str], names: tuple[str, ...]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.partition(":")
        if sep and key.strip() in names:
            fields[key.strip()] = value.strip()
    return fields


def parse(prompt: str) -> Replay | Failure | None:
    """The replay ``prompt`` asks for, or ``None`` when it is an ordinary ask.

    Raises :class:`ValueError` for a replay prompt missing a field or naming
    an unknown outcome: that is a case authoring error, not a run.
    """
    lines = prompt.strip().splitlines()
    directive = lines[0].strip() if lines else ""
    if directive == FAILURE_DIRECTIVE:
        fields = _fields(lines[1:], _FAILURE_FIELDS)
        missing = [f for f in _FAILURE_FIELDS if not fields.get(f)]
        if missing:
            raise ValueError(f"{directive} prompt is missing {', '.join(missing)}")
        if fields["outcome"] not in FAILURE_OUTCOMES:
            raise ValueError(
                f"{directive} outcome {fields['outcome']!r} is not one of {', '.join(FAILURE_OUTCOMES)}"
            )
        return Failure(fields["title"], fields["body"], fields["outcome"], fields["reason"])
    if directive != QUESTION_DIRECTIVE:
        return None
    fields = _fields(lines[1:], _QUESTION_FIELDS)
    options = tuple(
        o.strip() for o in fields.get("options", "").split(OPTION_SEPARATOR) if o.strip()
    )
    missing = [f for f in _QUESTION_FIELDS if not fields.get(f)] + ([] if options else ["options"])
    if missing:
        raise ValueError(f"{directive} prompt is missing {', '.join(dict.fromkeys(missing))}")
    return Replay(fields["title"], fields["question"], options, fields["answer"])


def _command(script: str, args: list[str]) -> str:
    quoted = " ".join(shlex.quote(a) for a in args)
    return (
        f'PY={shlex.quote(HERMES_PYTHON)}; [ -x "$PY" ] || PY={shlex.quote(FALLBACK_PYTHON)}; '
        f'"$PY" -c {shlex.quote(script)} {quoted}'
    )


def new_key() -> str:
    """A fresh run key for a replay card's ``idempotency_key``."""
    return REPLAY_KEY_PREFIX + uuid.uuid4().hex


def plant_command(replay: Replay | Failure, key: str) -> str:
    """The ``sh -c`` line that files, parks and wakes the replay's card in the pod."""
    if isinstance(replay, Failure):
        body, outcome = replay.body, replay.outcome
    else:
        body, outcome = replay.reason, OUTCOME_QUESTION
    return _command(
        _PLANT_SCRIPT,
        [
            REPLAY_PRESENT,
            HERMES_ROOT,
            SCRIPTS_DIR,
            SLACK_UX_FLAG,
            FLAG_ON,
            CARD_CREATOR,
            STUB_CHANNEL,
            STUB_THREAD,
            replay.title,
            body,
            replay.reason,
            outcome,
            WAKE_ASSIGNEE,
            key,
        ],
    )


def read_command(key: str) -> str:
    """The ``sh -c`` line that reads the newest card carrying ``key`` in the pod."""
    return _command(_READ_SCRIPT, [REPLAY_PRESENT, HERMES_ROOT, key])


def archive_command(key: str) -> str:
    """The ``sh -c`` line that archives the cards carrying ``key`` in the pod."""
    return _command(_ARCHIVE_SCRIPT, [REPLAY_PRESENT, HERMES_ROOT, key])


def _reply(text: str, what: str) -> dict:
    marker = text.find(REPLAY_PRESENT)
    if marker < 0:
        raise ReplayUnavailable(f"{what}: the in-pod script did not run")
    try:
        payload = json.loads(text[marker + len(REPLAY_PRESENT) :].strip())
    except json.JSONDecodeError as exc:
        raise ReplayBroken(f"{what}: reply is not JSON ({exc})") from exc
    if not isinstance(payload, dict):
        raise ReplayBroken(f"{what}: reply is not an object")
    if payload.get("error"):
        raise ReplayBroken(f"{what}: {payload['error']}")
    return payload


def plant(
    shell: Callable[[str, float], str], replay: Replay | Failure, timeout: float, key: str | None = None
) -> Planted:
    """File the replay's card, park it, and return the wake the image builds for it.

    ``shell`` is :func:`harness._agent_shell`; ``key`` defaults to
    :func:`new_key`. Raises :class:`ReplayUnavailable` when the script did not
    run to completion, after sweeping any card it filed before the
    ``kubectl exec`` gave out, and :class:`ReplayBroken` when its reply is not
    JSON, it reported an error, or it named no card or wake. The script
    archives a card it filed before reporting an error. An image with the
    moments module that posts nothing is one such error: it would build the
    plain wake and pass for a red run.
    """
    key = key or new_key()
    what = "failure wake" if isinstance(replay, Failure) else "question wake"
    try:
        payload = _reply(shell(plant_command(replay, key), timeout), what)
    except ReplayUnavailable:
        _sweep(shell, key, timeout)
        raise
    card, wake = payload.get("card"), payload.get("wake")
    if not isinstance(card, str) or not card or not isinstance(wake, str) or not wake.strip():
        _sweep(shell, key, timeout)
        raise ReplayBroken(f"{what}: no card or no wake in {payload!r}")
    posted = payload.get("posted")
    return Planted(card, wake, posted if isinstance(posted, int) else 0, key)


def _sweep(shell: Callable[[str, float], str], key: str, timeout: float) -> bool:
    """Archive every card carrying ``key``; True when the archive was confirmed."""
    try:
        reply = _reply(shell(archive_command(key), timeout), "archiving the replay's cards")
    except (ReplayUnavailable, ReplayBroken):
        return False
    return bool(reply.get("archived"))


def archive(shell: Callable[[str, float], str], key: str, timeout: float) -> Settled | None:
    """Read the run's newest card, then archive every card carrying ``key``.

    The read and the archive are separate execs, so an archive that times out
    or is refused keeps the card already read, with ``archived`` False.
    ``None`` when the read failed or found no card carrying ``key``: the
    card's status and comments are then unknown, and ``replay_card`` reports
    an error rather than grading an absent card as unblocked.
    """
    try:
        reply = _reply(shell(read_command(key), timeout), "reading the replay's card")
    except (ReplayUnavailable, ReplayBroken):
        reply = None
    archived = _sweep(shell, key, timeout)
    if reply is None or not isinstance(reply.get("card"), str):
        return None
    status, comments = reply.get("status"), reply.get("comments")
    return Settled(
        status if isinstance(status, str) else None,
        tuple(c for c in comments if isinstance(c, dict)) if isinstance(comments, list) else (),
        archived,
    )


# Metadata ``KubeAgentsHarness._settle`` writes on every turn, ``None`` on a
# turn that delegated nothing; the merged run keeps both turns' captures.
_WORKER_KEYS = ("worker_commands", "worker_trajectory")


def _add_tokens(base: dict, extra: dict) -> None:
    # ``harness._sum_tokens`` with nested buckets (``workers``, ``front_door``)
    # summed key by key rather than added as dicts.
    for bucket, value in extra.items():
        current = base.get(bucket)
        if value is None:
            continue
        if isinstance(value, dict):
            nested = dict(current) if isinstance(current, dict) else {}
            _add_tokens(nested, value)
            base[bucket] = nested
        elif current is None or isinstance(current, dict):
            base[bucket] = value
        else:
            base[bucket] = current + value


def _combine(first, second):
    if first is None:
        return second
    if second is None:
        return first
    if isinstance(first, list) and isinstance(second, list):
        return [*first, *second]
    if isinstance(first, dict) and isinstance(second, dict):
        return {**first, **{k: _combine(first.get(k), v) for k, v in second.items()}}
    return second


def _card_metadata(planted: Planted, settled: Settled | None) -> dict:
    # ``settled`` is ``None`` when the card could not be read; its status and
    # comments are then unknown rather than empty.
    return {
        "card": planted.card,
        "wake": planted.wake,
        "posted": planted.posted,
        "settled": settled.as_metadata() if settled is not None else None,
    }


def _settled_entry(planted: Planted, settled: Settled | None) -> dict:
    return {
        "name": SETTLED_ENTRY,
        "args": {"card": planted.card},
        "result": settled.as_metadata() if settled is not None else None,
        "status": "harness",
    }


def merge(
    planted: Planted, wake: AgentResult, answer: AgentResult, settled: Settled | None = None
) -> AgentResult:
    """One result for the two turns, graded on the wake turn's reply.

    ``output`` and ``final_message`` are the reply to the wake; the answer
    turn's text, and the card as the run left it (``settled``), are kept in
    metadata and as the trajectory's :data:`SETTLED_ENTRY`. The trajectory,
    errors and worker captures are both turns'. The
    answer turn's tokens supersede the wake turn's when both read the same
    session, whose row is cumulative over the conversation, except for the
    wake turn's ``workers``, which are that turn's alone and are added back;
    otherwise they are summed.
    """
    session = wake.metadata.get("session_id")
    same_session = bool(session) and session == answer.metadata.get("session_id")
    tokens = dict(answer.tokens if same_session else wake.tokens)
    if not same_session:
        _add_tokens(tokens, answer.tokens)
    elif isinstance(wake.tokens.get("workers"), dict):
        # As ``harness._fold_worker_tokens`` folded them into that turn's totals.
        workers = wake.tokens["workers"]
        _add_tokens(tokens, {"workers": workers})
        _add_tokens(tokens, {k: v for k, v in workers.items() if isinstance(v, int) and not isinstance(v, bool)})
    metadata = {**answer.metadata, **wake.metadata}
    for key in _WORKER_KEYS:
        if key in wake.metadata or key in answer.metadata:
            metadata[key] = _combine(wake.metadata.get(key), answer.metadata.get(key))
    metadata["final_message"] = str(wake.metadata.get("final_message") or wake.output)
    metadata["question_wake"] = {
        **_card_metadata(planted, settled),
        "answer_output": answer.output,
        "answer_final_message": str(answer.metadata.get("final_message") or answer.output),
    }
    return AgentResult(
        output=wake.output,
        trajectory=[*wake.trajectory, *answer.trajectory, _settled_entry(planted, settled)],
        tokens=tokens,
        errors=[*wake.errors, *answer.errors],
        metadata=metadata,
    )


def tag(planted: Planted, wake: AgentResult, settled: Settled | None = None) -> AgentResult:
    """The failure replay's one turn, with the card, its wake and ``settled`` kept in metadata.

    The reply to the wake is already the run's ``final_message``; the
    trajectory gains :data:`SETTLED_ENTRY` and nothing else changes.
    """
    metadata = {**wake.metadata, "failure_wake": _card_metadata(planted, settled)}
    return AgentResult(
        output=wake.output,
        trajectory=[*wake.trajectory, _settled_entry(planted, settled)],
        tokens=wake.tokens,
        errors=wake.errors,
        metadata=metadata,
    )
