"""Replay the wake a blocked, failed or retried card sends the front door.

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

**A typed answer in a session the wake never reached.** A question prompt
with ``session: fresh`` sends the answer on a new conversation instead, as
Slack does when the thread has no live session for the answer to land in
(it expired, the gateway restarted, or sessions are per user): the new
session starts from the thread as Hermes' cold start reads it, the ask that
opened the thread, the question posted in it and the front door's reply to
the wake when it was not silent, then the typed answer with the sender
prefix a shared thread session carries. The in-pod context script
(:func:`context_command`) formats those messages with the image's own
``SlackAdapter._format_thread_context``, so the new session reads the
thread's text the way the gateway would hand it over, card id included or
not. The plant also files a decoy: a second card blocked on the same
question, as a question in another thread would be, made to list first and
read as the newer of the two so that neither "the first" nor "the latest"
is a guess that lands on the planted card. Only the thread's text tells the
two apart, and the read records the decoy's status beside the planted
card's, archived included, so ``replay_card`` can require that it is still
blocked.

**A worker that crashed or timed out.** The dispatcher retries a ``crashed``
or ``timed_out`` card until its failure breaker trips
(``hermes_cli/kanban_db_dispatch.py``, ``_record_task_failure``:
``DEFAULT_FAILURE_LIMIT`` consecutive failures, or the card's own
``max_retries``). Below the limit the card goes back to ``ready`` and the
event wakes the front door alone (``outcome: crashed`` or ``timed_out``). On
the attempt that trips it, the dispatcher appends ``gave_up`` straight after
the ``crashed`` or ``timed_out`` event and parks the card in ``blocked``, so
one wake carries both (``outcome: crashed_final`` or ``timed_out_final``) and
its status names both: gave up, and that the dispatcher will retry. The plant
records each attempt as the dispatcher does, the event and then
``_record_task_failure``, which counts it and trips on its own: one attempt
for a retry, and as many as the image's ``DEFAULT_FAILURE_LIMIT`` for a final
one. The card is assigned to
:data:`WORKER_ASSIGNEE` before it fails, a profile no install has, so a
retrying card left ``ready`` never starts a worker.

A final attempt's wake can arrive in two halves, which neither replay
reproduces: the dispatcher appends the ``crashed`` or ``timed_out`` event and
the breaker's ``gave_up`` in separate transactions, so the notifier can claim
the first alone and wake with "dispatcher will retry", then wake again with
``gave_up``. The plant always delivers both in one wake.

What neither replay reproduces: the turns arrive on the run's own
``/v1/responses`` conversation rather than the session that filed the card, so
the front door has not seen the ask that led to it; for a question, the flag is
set in the script's process whatever the install's setting and no message
reaches Slack. The wake under test is the notifier's, built by the image's own
code.

A question, blocked or gave_up card stays unassigned on the board, so
unblocking it hands no worker anything; the notifier is given a copy naming
:data:`WAKE_ASSIGNEE`, as a delegated card would carry, so the wake does not
read ``@None``. A crashed or timed-out card is assigned to
:data:`WORKER_ASSIGNEE`, and its wake names that.

Every replay's card carries a key minted for the run
(:data:`REPLAY_KEY_PREFIX`, the card's ``idempotency_key``). :func:`archive`
reads the card's status and comments, then, in a second exec, archives every
card carrying the key, so a card filed by a plant whose ``kubectl exec`` timed
out is swept as well, and an archive that fails keeps what was read. A card
an archive failed to sweep is archived by a later plant on the same install
once it is older than :data:`STALE_REPLAY_SECONDS`. What it read rides on the run's trajectory as a harness entry
(:data:`SETTLED_ENTRY`), because devops-bench persists the trajectory and not
the metadata; the ``replay_card`` verifier grades it.

Unlike :mod:`kube_agents_bench.board`, a failed plant is not best effort: a
run that never saw the wake grades nothing. :func:`plant` raises
:class:`ReplayUnavailable`, which the harness records as infrastructure, when
the script never ran to completion, and :class:`ReplayBroken`, which it
records as an error, when the script ran in the image and failed there. A
breaker that disagrees with the outcome (a retry that trips it, a final
attempt that does not) is an error too: the image's dispatcher no longer
retries the way the case asserts, so :func:`plant` raises
:class:`ReplayMismatch` and the harness records an errored run rather than an
infrastructure one, which the gate excludes. An errored run has no trajectory
and a null token count, so a single repetition of it stops at one of the
gate's absolute rungs (a check that did not run, or "not evidence of a real
agent run"): an absolute red, admitted case or not. The printed reason names
the rung, not the mismatch; the run's error names the mismatch.
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
    "ReplayMismatch",
    "ReplayUnavailable",
    "Settled",
    "archive",
    "fresh_answer",
    "merge",
    "parse",
    "plant",
    "tag",
    "thread_messages",
]

# First lines of the two replay prompts. The ``key: value`` lines under each
# are its fields (:data:`_QUESTION_FIELDS`, :data:`_FAILURE_FIELDS`); anything
# else is ignored.
QUESTION_DIRECTIVE = "[bench:slack-question-wake]"
FAILURE_DIRECTIVE = "[bench:card-failure-wake]"
_QUESTION_FIELDS = ("title", "question", "options", "answer")
# Optional for a question replay: ``session: fresh`` sends the answer on a new
# conversation that starts from the thread's context.
SESSION_FIELD = "session"
SESSION_FRESH = "fresh"
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
# A worker's crash or timeout the dispatcher will retry, and the same on the
# attempt that trips its failure breaker.
OUTCOME_CRASHED = "crashed"
OUTCOME_TIMED_OUT = "timed_out"
OUTCOME_CRASHED_FINAL = "crashed_final"
OUTCOME_TIMED_OUT_FINAL = "timed_out_final"
WORKER_OUTCOMES = (
    OUTCOME_CRASHED,
    OUTCOME_TIMED_OUT,
    OUTCOME_CRASHED_FINAL,
    OUTCOME_TIMED_OUT_FINAL,
)
FAILURE_OUTCOMES = (OUTCOME_BLOCKED, OUTCOME_GAVE_UP, *WORKER_OUTCOMES)

# Line the in-pod scripts print before their JSON. A reply without it means
# the script never ran to completion.
REPLAY_PRESENT = "__BENCH_CARD_WAKE__"

# A replay card's ``idempotency_key`` is this and a fresh hex suffix per run.
REPLAY_KEY_PREFIX = "devops-bench-card-wake-"
# Appended to the run's key for a fresh-session replay's decoy card.
DECOY_KEY_SUFFIX = "-decoy"
# How old another run's replay card must be before a plant archives it: past
# the longest a run can hold one (a 600 s turn, a 1800 s delegation), so a run
# going on beside this one on the same install keeps its card.
STALE_REPLAY_SECONDS = 2 * 60 * 60

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
# Who a crashed or timed-out card says was working it: shaped like a
# scaffolded cluster agent's profile (cluster_agent_profile.py, profile_name)
# but never scaffolded, so the dispatcher never takes a card assigned to it.
WORKER_ASSIGNEE = "cluster-bench-project-bench-sandbox-us-central1"
STUB_CHANNEL = "C0BENCHWAKE"
STUB_THREAD = "1700000000.000100"
# The fresh-session thread: the user who asked and answers, and the bot that
# posted the question and the wake reply. Ids, not secrets.
STUB_USER = "UBENCHASKER"
STUB_USER_NAME = "bench-user"
STUB_BOT = "UBENCHBOT"
# The question's ts is the plant script's stub post's; the wake reply follows it.
STUB_QUESTION_TS = "1700000000.000200"
STUB_REPLY_TS = "1700000000.000300"
ASK_MESSAGE_ID = "bench-ask"

# Runs inside the agent container. Positional arguments: the sentinel, the
# hermes root, the scripts directory, the flag, its on value, the creator,
# the stub channel, the stub thread, the card title, its body, the block
# reason or failure error, the outcome, the wake's assignee, the run's key,
# the decoy's key (empty for no decoy), the replay key prefix and the stale
# age. Before filing, it archives other runs' replay cards older than the
# stale age, which an archive whose exec failed left on the board; that sweep
# failing does not stop the plant. The cards are archived again if anything
# after filing them fails. The worker pid, elapsed time and runtime limit in
# a crash or timeout's payload are made up; only the front door reads them.
_PLANT_SCRIPT = r"""
import asyncio, dataclasses, json, os, sys, time

(SENTINEL, HERMES_ROOT, SCRIPTS, FLAG, ON, CREATOR,
 CHANNEL, THREAD, TITLE, BODY, REASON, OUTCOME, ASSIGNEE, KEY, DECOY_KEY, PREFIX, STALE) = sys.argv[1:18]
STUB_TS = "1700000000.000200"
STUB_PID = 4242
STUB_ELAPSED, STUB_LIMIT = 1830, 1800
# A final attempt's wake: its own crashed or timed_out event, then gave_up.
FINAL_BATCH = 2
# The decoy lists first (kanban_list sorts by priority, then created_at) and
# the planted card is backdated so the decoy is the newer: created_at is
# whole seconds, and two cards filed together would otherwise tie.
DECOY_PRIORITY = 1
CARD_BACKDATE_SECONDS = 60
out = {"card": None, "decoy": None, "wake": None, "posted": 0, "post": None,
       "error": None, "mismatch": None, "swept": []}


class BreakerMismatch(RuntimeError):
    pass


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
    try:
        stale = [row[0] for row in conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key LIKE ? AND idempotency_key != ? "
            "AND status != 'archived' AND created_at < ?",
            (PREFIX + "%", KEY, int(time.time()) - int(STALE)))]
        out["swept"] = [old for old in stale if kb.archive_task(conn, old)]
    except Exception as exc:
        out["sweep_error"] = "%s: %s" % (type(exc).__name__, exc)
    gave_up = OUTCOME == "gave_up"
    card = kb.create_task(conn, title=TITLE, body=BODY, created_by=CREATOR, idempotency_key=KEY)
    out["card"] = card
    batch = 1
    if OUTCOME in ("crashed", "timed_out", "crashed_final", "timed_out_final"):
        from hermes_cli import kanban_db_dispatch as dispatch
        trigger, final = OUTCOME.replace("_final", ""), OUTCOME.endswith("_final")
        # Assigned before it fails, as a real card is: the profile does not
        # exist, so no dispatcher ever takes it.
        if not kb.assign_task(conn, card, ASSIGNEE):
            raise RuntimeError("card %s would not take assignee %s" % (card, ASSIGNEE))
        # The payloads detect_crashed_workers and enforce_max_runtime write.
        if trigger == "crashed":
            payload = {"pid": STUB_PID, "claimer": None, "retry_status": "ready"}
            extra = {"pid": STUB_PID, "claimer": None}
        else:
            payload = {"pid": STUB_PID, "elapsed_seconds": STUB_ELAPSED, "limit_seconds": STUB_LIMIT,
                       "sigkill": False, "retry_status": "ready"}
            extra = {"pid": STUB_PID, "sigkill": False, "retry_status": "ready"}
        # Each attempt as the dispatcher records it: the event, then the
        # breaker's count, which appends gave_up when it trips. A final
        # attempt is the one that reaches the image's own limit.
        for _attempt in range(dispatch.DEFAULT_FAILURE_LIMIT if final else 1):
            with kb.write_txn(conn):
                kb._append_event(conn, card, trigger, payload)
            tripped = dispatch._record_task_failure(conn, card, REASON, outcome=trigger,
                                                    event_payload_extra=extra)
        if tripped != final:
            raise BreakerMismatch("card %s %s its failure breaker"
                                  % (card, "tripped" if tripped else "did not trip"))
        kind = "gave_up" if final else trigger
        # The notifier claims every event since its cursor, so a final
        # attempt's crash or timeout reaches the wake with the gave_up after it.
        batch = FINAL_BATCH if final else 1
    elif gave_up:
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
    events = [e for e in kb.list_events(conn, card) if e.kind != "assigned"][-batch:]
    if not events or events[-1].kind != kind:
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
    # A blocked, question or gave_up card stays unassigned on the board, so an
    # unblock hands no worker anything; a crashed or timed-out one carries a
    # profile no dispatcher takes. The notifier's copy names the assignee a
    # delegated card carries.
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
    if adapter.posts:
        out["post"] = {key: adapter.posts[0].get(key) or default for key, default in (("text", ""), ("blocks", []), ("attachments", []))}
    if DECOY_KEY:
        # Filed after the planted card, so it is the newer of two cards blocked on one question.
        decoy = kb.create_task(conn, title=TITLE, body=BODY, created_by=CREATOR,
                               priority=DECOY_PRIORITY, idempotency_key=DECOY_KEY)
        out["decoy"] = decoy
        if not kb.block_task(conn, decoy, reason=REASON, kind="needs_input"):
            raise RuntimeError("decoy card %s would not block" % decoy)
        conn.execute("UPDATE tasks SET created_at = created_at - ? WHERE id = ?",
                     (CARD_BACKDATE_SECONDS, card))
except Exception as exc:
    out["mismatch" if isinstance(exc, BreakerMismatch) else "error"] = "%s: %s" % (type(exc).__name__, exc)
    for filed in (out["card"], out["decoy"]):
        if conn is not None and filed:
            try:
                kb.archive_task(conn, filed)
            except Exception:
                pass
print(SENTINEL)
print(json.dumps(out))
"""

# Reads the run's newest card as the run left it, and its decoy's status.
# Positional arguments: the sentinel, the hermes root, the run's key and the
# decoy's key. ``card`` is ``None`` when no card that is not yet archived
# carries the key; ``decoy_status`` is ``None`` when no card, archived or
# not, carries the decoy's: an agent that archived the decoy reads as that.
_READ_SCRIPT = r"""
import json, sys

SENTINEL, HERMES_ROOT, KEY, DECOY_KEY = sys.argv[1:5]
sys.path.insert(0, HERMES_ROOT)
out = {"card": None, "status": None, "comments": [], "decoy_status": None, "error": None}
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
    decoys = [row[0] for row in conn.execute(
        "SELECT id FROM tasks WHERE idempotency_key = ? ORDER BY created_at DESC", (DECOY_KEY,))]
    if decoys:
        decoy = kb.get_task(conn, decoys[0])
        out["decoy_status"] = decoy.status if decoy else None
except Exception as exc:
    out["error"] = "%s: %s" % (type(exc).__name__, exc)
print(SENTINEL)
print(json.dumps(out))
"""

# Archives every card carrying the run's key or its decoy's. Positional
# arguments: the sentinel, the hermes root, the run's key and the decoy's key.
_ARCHIVE_SCRIPT = r"""
import json, sys

SENTINEL, HERMES_ROOT, KEY, DECOY_KEY = sys.argv[1:5]
sys.path.insert(0, HERMES_ROOT)
out = {"archived": False, "cards": [], "error": None}
try:
    from hermes_cli import kanban_db as kb
    try:
        from hermes_cli.kanban_db_connect import connect
    except ImportError:
        connect = kb.connect
    conn = connect()
    cards = [row[0] for key in (KEY, DECOY_KEY) for row in conn.execute(
        "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived'", (key,))]
    out["cards"] = cards
    out["archived"] = all([bool(kb.archive_task(conn, card)) for card in cards])
except Exception as exc:
    out["error"] = "%s: %s" % (type(exc).__name__, exc)
print(SENTINEL)
print(json.dumps(out))
"""


# Formats the fresh session's thread context with the image's Slack adapter.
# Positional arguments: the sentinel, the hermes root, the scripts directory,
# the channel, the thread, the asker's user id and name, the bot's user id,
# and a JSON list of the thread's messages before the answer. The adapter is
# never constructed or connected: only the formatter's own state is set, and
# name lookup and the allowlist answer from the arguments.
_CONTEXT_SCRIPT = r"""
import asyncio, json, sys

SENTINEL, HERMES_ROOT, SCRIPTS, CHANNEL, THREAD, USER, USER_NAME, BOT, MESSAGES = sys.argv[1:10]
out = {"context": None, "error": None}
try:
    sys.path[:0] = [HERMES_ROOT, SCRIPTS]
    from plugins.platforms.slack.adapter import SlackAdapter

    adapter = SlackAdapter.__new__(SlackAdapter)
    adapter._team_bot_user_ids = {}
    adapter._bot_user_id = BOT

    async def _resolve_user_name(user_id, chat_id=None, team_id=None):
        return USER_NAME if user_id == USER else user_id

    adapter._resolve_user_name = _resolve_user_name
    adapter._is_sender_authorized = lambda *args, **kwargs: True
    messages = json.loads(MESSAGES)
    content, _parent = asyncio.run(adapter._format_thread_context(
        messages, thread_ts=THREAD, current_ts="", team_id="", channel_id=CHANNEL))
    if not content:
        raise RuntimeError("the adapter formatted no thread context from %d message(s)" % len(messages))
    out["context"] = content
except Exception as exc:
    out["error"] = "%s: %s" % (type(exc).__name__, exc)
print(SENTINEL)
print(json.dumps(out))
"""


class ReplayUnavailable(RuntimeError):
    """The plant script never ran to completion in the pod: infrastructure."""


class ReplayBroken(RuntimeError):
    """The plant script ran in the image and failed there: the image's fault, not the cluster's."""


class ReplayMismatch(RuntimeError):
    """The image's failure breaker disagrees with the replay's outcome."""


@dataclass(frozen=True)
class Replay:
    """A replay prompt's fields: the card, its question and the user's answer."""

    title: str
    question: str
    options: tuple[str, ...]
    answer: str
    fresh: bool = False

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
    """What the plant left on the board: the card, its wake, how many posts the stub took, and the run's key.

    ``post`` is the first post's ``text``, ``blocks`` and ``attachments``, ``None`` when the
    stub took none; ``decoy`` is the fresh-session replay's decoy card.
    """

    card: str
    wake: str
    posted: int
    key: str = ""
    post: dict | None = None
    decoy: str | None = None


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
    decoy_status: str | None = None

    def as_metadata(self) -> dict:
        metadata = {"status": self.status, "comments": [dict(c) for c in self.comments]}
        if self.decoy_status is not None:
            metadata["decoy_status"] = self.decoy_status
        return metadata


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
    fields = _fields(lines[1:], (*_QUESTION_FIELDS, SESSION_FIELD))
    session = fields.get(SESSION_FIELD, "")
    if session and session != SESSION_FRESH:
        raise ValueError(f"{directive} session {session!r} is not {SESSION_FRESH!r}")
    options = tuple(
        o.strip() for o in fields.get("options", "").split(OPTION_SEPARATOR) if o.strip()
    )
    missing = [f for f in _QUESTION_FIELDS if not fields.get(f)] + ([] if options else ["options"])
    if missing:
        raise ValueError(f"{directive} prompt is missing {', '.join(dict.fromkeys(missing))}")
    return Replay(fields["title"], fields["question"], options, fields["answer"], session == SESSION_FRESH)


def _command(script: str, args: list[str]) -> str:
    quoted = " ".join(shlex.quote(a) for a in args)
    return (
        f'PY={shlex.quote(HERMES_PYTHON)}; [ -x "$PY" ] || PY={shlex.quote(FALLBACK_PYTHON)}; '
        f'"$PY" -c {shlex.quote(script)} {quoted}'
    )


def new_key() -> str:
    """A fresh run key for a replay card's ``idempotency_key``."""
    return REPLAY_KEY_PREFIX + uuid.uuid4().hex


def decoy_key(key: str) -> str:
    """The idempotency key of the decoy card a fresh-session replay files beside ``key``'s."""
    return key + DECOY_KEY_SUFFIX


def plant_command(replay: Replay | Failure, key: str) -> str:
    """The ``sh -c`` line that files, parks and wakes the replay's card in the pod."""
    if isinstance(replay, Failure):
        body, outcome = replay.body, replay.outcome
        assignee = WORKER_ASSIGNEE if outcome in WORKER_OUTCOMES else WAKE_ASSIGNEE
    else:
        body, outcome, assignee = replay.reason, OUTCOME_QUESTION, WAKE_ASSIGNEE
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
            assignee,
            key,
            decoy_key(key) if isinstance(replay, Replay) and replay.fresh else "",
            REPLAY_KEY_PREFIX,
            str(STALE_REPLAY_SECONDS),
        ],
    )


def read_command(key: str) -> str:
    """The ``sh -c`` line that reads the newest card carrying ``key``, and its decoy's status, in the pod."""
    return _command(_READ_SCRIPT, [REPLAY_PRESENT, HERMES_ROOT, key, decoy_key(key)])


def archive_command(key: str) -> str:
    """The ``sh -c`` line that archives the cards carrying ``key`` or its decoy's in the pod."""
    return _command(_ARCHIVE_SCRIPT, [REPLAY_PRESENT, HERMES_ROOT, key, decoy_key(key)])


def thread_messages(replay: Replay, planted: Planted, wake_reply: str) -> list[dict]:
    """The thread as Slack would hold it before the typed answer, oldest first.

    The ask that opened it, the question the stub took (when the image posts
    one) and the front door's reply to the wake, left out when it is ``""``:
    the gateway posts nothing for a silent reply.
    """
    bot = {"user": STUB_BOT, "bot_id": STUB_BOT}
    messages = [{"ts": STUB_THREAD, "user": STUB_USER, "client_msg_id": ASK_MESSAGE_ID, "text": replay.title}]
    if planted.post is not None:
        messages.append({"ts": STUB_QUESTION_TS, **bot, **planted.post})
    if wake_reply.strip():
        messages.append({"ts": STUB_REPLY_TS, **bot, "text": wake_reply})
    return messages


def context_command(messages: list[dict]) -> str:
    """The ``sh -c`` line that formats ``messages`` as the image's Slack adapter would."""
    return _command(
        _CONTEXT_SCRIPT,
        [
            REPLAY_PRESENT,
            HERMES_ROOT,
            SCRIPTS_DIR,
            STUB_CHANNEL,
            STUB_THREAD,
            STUB_USER,
            STUB_USER_NAME,
            STUB_BOT,
            json.dumps(messages),
        ],
    )


def fresh_answer(shell: Callable[[str, float], str], messages: list[dict], answer: str, timeout: float) -> str:
    """The first message a new thread session gets for ``answer``: the thread's context, then the answer.

    Joined as ``run_inbound._prefix_inbound_sender_context`` joins them, with
    the sender prefix a shared thread session carries. Raises
    :class:`ReplayUnavailable` or :class:`ReplayBroken` as :func:`plant` does.
    """
    reply = _reply(shell(context_command(messages), timeout), "thread context")
    content = reply.get("context")
    if not isinstance(content, str) or not content.strip():
        raise ReplayBroken(f"thread context: none in {reply!r}")
    return f"{content}\n\n[New message]\n[{STUB_USER_NAME} | Slack user <@{STUB_USER}>] {answer}"


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
    if payload.get("mismatch"):
        raise ReplayMismatch(f"{what}: {payload['mismatch']}")
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
    ``kubectl exec`` gave out, :class:`ReplayBroken` when its reply is not
    JSON, it reported an error, or it named no card or wake, and
    :class:`ReplayMismatch` when the image's failure breaker disagreed with
    the outcome. The script archives a card it filed before reporting an
    error. An image with the moments module that posts nothing is one such
    error: it would build the plain wake and pass for a red run.
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
    posted, post, decoy = payload.get("posted"), payload.get("post"), payload.get("decoy")
    if isinstance(replay, Replay) and replay.fresh and not (isinstance(decoy, str) and decoy):
        _sweep(shell, key, timeout)
        raise ReplayBroken(f"{what}: no decoy card in {payload!r}")
    return Planted(
        card,
        wake,
        posted if isinstance(posted, int) else 0,
        key,
        post if isinstance(post, dict) else None,
        decoy if isinstance(decoy, str) and decoy else None,
    )


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
    status, comments, decoy = reply.get("status"), reply.get("comments"), reply.get("decoy_status")
    return Settled(
        status if isinstance(status, str) else None,
        tuple(c for c in comments if isinstance(c, dict)) if isinstance(comments, list) else (),
        archived,
        decoy if isinstance(decoy, str) else None,
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
    metadata = {
        "card": planted.card,
        "wake": planted.wake,
        "posted": planted.posted,
        "settled": settled.as_metadata() if settled is not None else None,
    }
    if planted.decoy is not None:
        metadata["decoy"] = planted.decoy
    return metadata


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


def tag(
    planted: Planted, wake: AgentResult, settled: Settled | None = None, key: str = "failure_wake"
) -> AgentResult:
    """A replay's one turn, with the card, its wake and ``settled`` kept in metadata under ``key``.

    The failure replay's turn, or a question replay's whose wake errored
    (``key="question_wake"``). The reply to the wake is already the run's
    ``final_message``; the trajectory gains :data:`SETTLED_ENTRY` and nothing
    else changes.
    """
    metadata = {**wake.metadata, key: _card_metadata(planted, settled)}
    return AgentResult(
        output=wake.output,
        trajectory=[*wake.trajectory, _settled_entry(planted, settled)],
        tokens=wake.tokens,
        errors=wake.errors,
        metadata=metadata,
    )
