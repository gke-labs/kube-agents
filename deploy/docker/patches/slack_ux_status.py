"""Show a Slack thread's delegated work as one plan, and keep its session status valid.

Installed into the image at ``/opt/hermes/gateway/slack_ux_status.py``.
``apply_slack_ux_status.py`` makes the Slack adapter's thread-status setter
and its message-event builder hand over to this module when ``KAGE_SLACK_UX``
is on, and ``gateway/kanban_progress_lines.py`` calls :func:`deliver_row` and
:func:`settle_row` for a Slack card. With the flag off none of those callers
reaches anything here. What the blocks look like is
``agents/platform/scripts/slack_status.py``; this module decides when to post
them.

Upstream, and why it changes
----------------------------
**The session status.** With slack-sdk 3.44 or later, Hermes sends its
thread status to ``agents.sessions.setStatus``: a phrase ("is thinking...")
every 2 seconds while a turn runs, and ``""`` to clear. That method takes
``processing``, ``suspended`` or ``closed`` and nothing else, so every call
fails ``invalid_arguments``, logged at debug: Working… never shows, the clear
never lands, and the refresh loop spends 30 Slack calls a minute on errors.
With the flag on, the phrase becomes ``processing`` and the clear
``closed``, and a status is sent only when it changes, or when
:data:`SESSION_REFRESH_SECONDS` have passed on ``processing``, so a turn
costs a call or two rather than 30 a minute. Each open and close is logged at info. The legacy
``assistant.threads.setStatus``, used when the SDK has no Agent Sessions, takes
free text and is left to upstream.

**The session title.** Upstream titles only DM threads. With the flag on, a
channel thread's first ask (or the question a clicked choice answers, offered by
``slack_ux_clicks`` before its turn runs) becomes its session title, set right after a
``processing`` lands: ``agents.sessions.rename`` refuses a thread with no
session yet. A failed rename keeps the ask for the next ``processing`` sent;
Slack's ``invalid_name`` refusal is logged at warning, once per thread and
title, and any other failure at debug; once the title is set, a follow-up ask in the thread
keeps it. An event alert's thread takes the title the event watcher recorded for it instead
(``slack_ux_incident.alert_title``), ahead of any ask.

**One plan per thread.** ``kanban_progress_lines`` rolls each card's progress
notes into one message per card. With the flag on, a Slack thread instead gets
one message holding a plan with a row per card, edited in place with
``chat.update`` as notes arrive. A progress note creates a card's row; a terminal
event settles it (``complete`` with the card's one-line result, ``error``, or
``pending`` for a card waiting on the user). A card that reaches one of those
without a note gets its row then, already settled, so every card in the thread
shows in the plan; a card this process already put on a plan opens no second
row, so a replayed event or one arriving after its plan was dropped adds
nothing far down the thread. :func:`settle_row` says whether the plan now
shows the card complete, which lets ``kanban_progress_lines`` fold the report
of a card beneath a fan-out still open on the thread into its row.
Two kinds upstream never posts
reach the plan through ``kanban_progress_lines.silent_event``:
``unblocked`` sets a waiting row, or one that gave up, running again, and
``archived`` settles a running or waiting row as failed, with an ``Archived``
note. The row stays
rather than the message going: the credential proxy refuses ``chat.delete``,
as it refuses every destructive Slack verb. ``crashed`` and ``timed_out``
leave the row running, since the dispatcher retries the card, and
``block_loop_detected`` sets it waiting, as ``slack_presenter`` reads them.
The plan holds the thread's session status: ``processing`` while a row runs,
``suspended`` while rows wait on the user and none runs, and a Planning Agent
turn ending in the thread does not close it under either. Once no row is
running or waiting, the session is closed and the plan is forgotten here, and
the thread's next card starts a new plan. A plan with a card that gave up,
which the breaker parks until it is unblocked, is set aside instead, as a
lapsed one is below, so the unblock still finds its row. A plan with no note or settled row
for :data:`PLAN_HOLD_SECONDS` is set aside, so a card whose terminal event
was lost does not hold the next card's Working… or put its row on a message
far up the thread; the next note starts a new plan. A set-aside plan holds
nothing running, so its session closes unless one of its cards waits on the
user, which holds ``suspended`` until that card moves: waiting is not a lost
event. Its cards' later events still update their rows on it, and a card
answered there runs on it again: it becomes the thread's plan once more, or,
beside a newer plan, holds ``processing`` for another hold. Past
:data:`LAPSED_PER_THREAD` a thread drops a set-aside plan that is quiet and
has no card waiting first. A set-aside plan with no card waiting on the user
or given up is dropped :data:`SET_ASIDE_MAX_SECONDS` after it was set aside,
or after a card on it resumed, its session sent, so a rolling card or running row whose
terminal event was lost does not keep it for good. A plan evicted at
:data:`PLANS_MAX`, current or set
aside, has its session sent again on the way out, since nothing else would;
the current plan evicted is the thread with the oldest note.
Past ``slack_status.ROWS_MAX`` rows the oldest settled rows leave the plan
first, so the cap hides a live card only when more than that many are live.
The plan has no Stop button yet: ``/stop`` interrupts only the
Planning Agent's turn, and the cards would run on.

Fallback: when posting or editing the plan fails (Slack refuses the blocks, the
message was deleted), the thread drops to the rolling line until every card
that rolled a note since has settled or been archived, which is what the
thread showed before this module. A plan holds ``processing`` while
those cards roll and ``suspended`` while they wait on the user, clearing the
session when its cards settle, even if its initial post was refused by Slack.
A settle still edits a posted plan, best effort, so an edit refused once,
for a rate limit say, does not leave its rows showing as running. Everything
here is in process, like the progress-line map: a gateway restart forgets
the plan, and the next note starts a new one. A
card that settles after a restart opens its row on a new plan, as a card with
no note does, and one whose row this process dropped settles with no plan;
either closes the thread's session, or suspends
it while the card waits on the user, so the Working… the old process set
does not stick; it can also clear Working… for another card from before the
restart that is still running, until that card's next note.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

try:
    import slack_presenter as _presenter
    import slack_status as _status
except ImportError:  # the scripts directory is not on PYTHONPATH
    _presenter = None
    _status = None

#: The flag, read here only to word the warning when the presenter is missing.
FLAG_ENV = "KAGE_SLACK_UX"

#: ``slack_presenter.FLAG_ON_VALUES``, copied because the warning below fires
#: exactly when that module cannot be imported. Unset is on there too.
FLAG_ON_VALUES = frozenset({"1", "true", "yes", "on"})

#: How long an unchanged ``processing`` stands before it is sent again. Slack
#: documents no expiry for an agent session's status; this bounds how stale a
#: status can go if one ever does, at one call a minute instead of thirty.
SESSION_REFRESH_SECONDS = 60.0

#: How long a plan with no new note or settled row stands before it is set
#: aside, closing the session unless a card waits on the user. A card whose
#: terminal event never reaches the thread (a dropped subscription, a lost
#: event) leaves its row running for good; this stops that row holding Working…
#: forever, while covering a card's silent stretches, since noteless heartbeats
#: reach no one.
PLAN_HOLD_SECONDS = 1800.0

#: How long a set-aside plan is kept after it was set aside, or after a card
#: on it resumed, when nothing on it waits on a person:
#: no card waiting on the user and none that gave up. Its running rows and rolling cards lost their terminal events, or
#: their cards have been quiet this long; well past a card's silent stretches,
#: so a late event almost always still finds its row.
SET_ASIDE_MAX_SECONDS = 4 * 3600.0

#: ``fail_label`` for the adapter's status setter when the plan sets it.
PLAN_STATUS_LABEL = "plan"

#: The notifier kind for a card archived by hand: its row settles as failed,
#: with :data:`ARCHIVED_NOTE` as its last step.
ARCHIVED_KIND = "archived"
ARCHIVED_NOTE = "Archived"

#: The notifier kind for a card the user unblocked: its waiting row runs
#: again, as does one that gave up, which the breaker parks until unblocked.
UNBLOCKED_KIND = "unblocked"

#: A dashboard move (a ``status`` event) to one of these columns settles the
#: card's row as the event kind it maps to would. A move to any other column
#: leaves the row's status alone: the dashboard cannot set ``running``, so a
#: move never means the card runs. Upstream writes the event only for a drag
#: to ready, todo or triage, which a card resuming into review lands as
#: ``review``; a drag to done or blocked posts its own ``completed`` or
#: ``blocked`` event instead.
MOVE_KINDS = {"review": "review_requested"}

#: The error ``agents.sessions.rename`` answers for a title holding a character
#: it refuses, as opposed to a thread with no session yet or a network fault.
RENAME_REFUSED_ERROR = "invalid_name"

#: Bounds on the in-process maps, oldest evicted first.
SESSIONS_MAX = 512
ASKS_MAX = 512
PLANS_MAX = 256
#: Cards remembered as having reached a thread's plan. Only a card not among
#: them has a row opened by a terminal event: the notifier delivers one again
#: when its post failed, and a card whose plan was forgotten or dropped must
#: not open a second row far down the thread.
SEEN_MAX = 1024
#: Set-aside plans kept per thread. :func:`_set_aside` drops the oldest quiet
#: one with no card waiting first; one dropped stops settling its rows and
#: holding ``suspended``. ``_lapsed`` itself is capped
#: at :data:`PLANS_MAX` threads.
LAPSED_PER_THREAD = 4

_warned_missing = False


class _Row:
    """One card's row. A plain class, for the reason ``slack_ux_reactions._Ask`` is."""

    __slots__ = (
        "archived", "last_event_id", "lines", "note", "result", "status", "steps", "task_id", "title",
    )

    def __init__(self, task_id: str, title: str) -> None:
        self.task_id = task_id
        self.title = title
        self.lines: list[str] = []
        #: Every note the row took, past the last :data:`slack_status.STEPS_MAX`
        #: kept; a dashboard move is not one. A new plan starts it again.
        self.steps = 0
        #: The latest of those notes, or :data:`ARCHIVED_NOTE` once archived by
        #: hand: what a running or failed row's title shows. A move lands in
        #: ``lines`` without touching it.
        self.note = ""
        #: The completed event's summary line, which the settled row shows; the
        #: card's title when it is empty.
        self.result = ""
        self.status = ""
        self.last_event_id = 0
        self.archived = False


class _Plan:
    __slots__ = ("expiry", "fallback", "lapse", "rolling", "rows", "team_id", "touched", "ts", "waiting")

    def __init__(self, team_id: str) -> None:
        self.ts = ""
        self.team_id = team_id
        self.rows: OrderedDict[str, _Row] = OrderedDict()
        self.fallback = False
        #: Cards whose notes went to a rolling line after the plan fell back.
        #: The plan is kept until each has settled, so none of them opens a
        #: second plan beside its rolling message.
        self.rolling: set[str] = set()
        #: The rolling cards now waiting on the user.
        self.waiting: set[str] = set()
        #: ``time.monotonic()`` at the last note or settled row while current, or
        #: when a card resumed once set aside; a settled row then does not move it.
        self.touched = time.monotonic()
        #: The timer that sets the plan aside after :data:`PLAN_HOLD_SECONDS`.
        self.lapse: asyncio.TimerHandle | None = None
        #: The timer that drops the plan once set aside, after
        #: :data:`SET_ASIDE_MAX_SECONDS`.
        self.expiry: asyncio.TimerHandle | None = None


#: ``(channel, thread) -> (status sent, when)``. No team: a kanban
#: subscription carries none, so the plan's sends and the Planning Agent's
#: turn must share one entry or each skips a change the other made.
_sessions: OrderedDict[tuple, tuple] = OrderedDict()
#: ``(channel, thread) -> the ask's words``, waiting for the session to open.
_asks: OrderedDict[tuple, str] = OrderedDict()
#: ``(channel, thread) -> the title set on the thread``, which the plan shows too.
_titles: OrderedDict[tuple, str] = OrderedDict()
#: ``(channel, thread) -> the title Slack refused``, so a retry every refresh warns once.
_refused_titles: OrderedDict[tuple, str] = OrderedDict()
#: ``(channel, thread) -> the alert title recorded for it``, "" for none, so a
#: thread's routing row is read once. The watcher records it before the alert's
#: first turn, so a miss stays a miss.
_alert_titles: OrderedDict[tuple, str] = OrderedDict()
#: ``(channel, thread) -> _Plan``.
_plans: OrderedDict[tuple, _Plan] = OrderedDict()
#: ``(channel, thread) -> [_Plan]`` the lapse set aside with a row still
#: running or waiting, or a card still rolling, newest last, kept so the
#: card's later events still settle it.
_lapsed: OrderedDict[tuple, list] = OrderedDict()
#: ``(channel, thread, card) -> True`` for every card a note, move or settle
#: brought to the thread's plan in this process.
_seen: OrderedDict[tuple, bool] = OrderedDict()
#: Lapse tasks in flight, held so the loop does not drop them mid-run.
_lapsing: set = set()
#: ``(channel, thread) -> suspended`` for a card waiting after a restart, with
#: no plan to hold it: :func:`_settle_orphan` sends the legacy setter a clear,
#: and :func:`set_thread_status` reads this once to send the wait instead.
_orphan_waits: OrderedDict[tuple, str] = OrderedDict()


def enabled() -> bool:
    """Whether ``KAGE_SLACK_UX`` is on and the presenter and renderer are importable."""
    global _warned_missing
    if _presenter is not None and _status is not None:
        return _presenter.enabled()
    value = os.environ.get(FLAG_ENV)
    if (value is None or value.strip().lower() in FLAG_ON_VALUES) and not _warned_missing:
        _warned_missing = True
        logger.warning(
            "slack_ux_status: %s is on but slack_presenter or slack_status is not "
            "importable; treating the flag as off", FLAG_ENV,
        )
    return False


def _remember(store: OrderedDict, key: Any, value: Any, cap: int) -> None:
    store[key] = value
    store.move_to_end(key)
    while len(store) > cap:
        store.popitem(last=False)


# --- the session -----------------------------------------------------------


def _alert_title(key: tuple) -> str:
    """The title the event watcher recorded for the alert posted as this thread, else ""."""
    if key not in _alert_titles:
        try:
            from gateway import slack_ux_incident

            title = slack_ux_incident.alert_title(*key)
        except Exception as exc:  # noqa: BLE001 — a title is cosmetic
            logger.debug("slack_ux_status: no alert title for %s/%s: %s", *key, exc)
            title = ""
        _remember(_alert_titles, key, title, ASKS_MAX)
    return _alert_titles[key]


def note_ask(chat_id: str, thread_ts: str | None, text: Any) -> None:
    """Keep a channel thread's first ask to title its session once it opens."""
    if not (chat_id and thread_ts and enabled()):
        return
    key = (str(chat_id), str(thread_ts))
    if key in _titles or key in _asks:
        return
    if str(text or "").strip():
        _remember(_asks, key, str(text), ASKS_MAX)


async def set_thread_status(
    adapter: Any,
    chat_id: str,
    team_id: str,
    thread_ts: str,
    status: Any,
    fail_label: str,
    status_method: Callable[[Any], Any],
    title_method: Callable[[Any], Any],
) -> None:
    """The adapter's thread-status setter, for ``agents.sessions``: an enum value, sent on change.

    ``status_method`` and ``title_method`` are the adapter module's own
    resolvers, passed in so this module needs no import of the adapter. The
    caller only hands over when the SDK has Agent Sessions.
    """
    wanted = _status.session_status(status)
    key = (str(chat_id), str(thread_ts))
    orphan = False
    if wanted == _status.SESSION_CLOSED:
        # A Planning Agent turn ends with a clear; the thread's plan outlives it.
        planned = _plan_session(*key)
        orphan = not planned and key in _orphan_waits
        wanted = planned or _orphan_waits.get(key, "") or wanted
    sent = _sessions.get(key)
    now = time.monotonic()
    if sent and sent[0] == wanted and (
        wanted != _status.SESSION_PROCESSING or now - sent[1] < SESSION_REFRESH_SECONDS
    ):
        if orphan:
            _orphan_waits.pop(key, None)
        return
    try:
        client = adapter._get_client(chat_id, team_id=team_id)
        await status_method(client)(channel_id=chat_id, thread_ts=thread_ts, status=wanted)
    except Exception as exc:  # noqa: BLE001 — upstream debug-logs its own failures too
        logger.debug("[Slack] agents.sessions.setStatus %s: %s", fail_label, exc)
        return
    if orphan:
        # Read once, and only once sent, so a refused send leaves it for the retry.
        _orphan_waits.pop(key, None)
    if not sent or sent[0] != wanted:
        logger.info("slack_ux_status: session %s in %s/%s", wanted, chat_id, thread_ts)
    _remember(_sessions, key, (wanted, now), SESSIONS_MAX)
    if wanted != _status.SESSION_PROCESSING or key in _titles:
        return
    ask = _alert_title(key) or _asks.get(key)
    if not ask:
        return
    title = _status.session_title(ask)
    if title:
        try:
            await title_method(client)(channel_id=chat_id, thread_ts=thread_ts, title=title)
        except Exception as exc:  # noqa: BLE001 — a title is cosmetic
            # The ask stays, so the next processing sent retries it and a
            # follow-up ask does not take its place. A refusal is a warning,
            # since it means session_title let through a character Slack
            # rejects; the retry every refresh would otherwise repeat it.
            if RENAME_REFUSED_ERROR in str(exc) and _refused_titles.get(key) != title:
                _remember(_refused_titles, key, title, ASKS_MAX)
                logger.warning(
                    "slack_ux_status: agents.sessions.rename refused the title in %s/%s: %s",
                    chat_id, thread_ts, exc,
                )
            else:
                logger.debug("slack_ux_status: agents.sessions.rename failed in %s/%s: %s", chat_id, thread_ts, exc)
            return
        _remember(_titles, key, title, ASKS_MAX)
    _asks.pop(key, None)


# --- the plan --------------------------------------------------------------


def _thread(sub: dict) -> tuple:
    return str(sub.get("chat_id") or ""), str(sub.get("thread_id") or "")


def _plan_session(chat_id: str, thread_ts: str) -> str:
    """The session status the thread's plans hold, or ``""`` when no card runs or waits.

    A card runs on a plan touched within :data:`PLAN_HOLD_SECONDS`: a row
    running, or a card rolling after its plan fell back (including when its
    initial post was refused). A set-aside plan is untouched that long
    unless a card on it was answered since. A card waiting on the user, on
    any of the thread's plans, holds ``suspended``.
    """
    key = (chat_id, thread_ts)
    plan = _plans.get(key)
    plans = [*_lapsed.get(key, ()), *([plan] if plan is not None else [])]
    now = time.monotonic()
    if any(now - p.touched < PLAN_HOLD_SECONDS and _running(p) for p in plans):
        return _status.SESSION_PROCESSING
    if any(_waiting(p) for p in plans):
        return _status.SESSION_SUSPENDED
    return ""


def _running(plan: _Plan) -> bool:
    return bool(plan.rolling - plan.waiting) or _status.running(plan.rows.values())


def _waiting(plan: _Plan) -> bool:
    return bool(plan.waiting) or any(
        row.status == _status.TASK_PENDING for row in plan.rows.values()
    )


def _held(plan: _Plan) -> bool:
    """Whether a set-aside plan still has a card whose events can move it."""
    return bool(plan.rolling) or any(_open(row) for row in plan.rows.values())


def _live(row: _Row) -> bool:
    return row.status in (_status.TASK_RUNNING, _status.TASK_PENDING)


def _open(row: _Row) -> bool:
    """Whether the row can still move: live, or a card that gave up, which ``unblocked`` resumes."""
    return _live(row) or (row.status == _status.TASK_ERROR and not row.archived)


def _prune(plan: _Plan) -> None:
    """Drop the oldest settled rows past ``ROWS_MAX``, so the renderer's cap cuts no live card."""
    extra = len(plan.rows) - _status.ROWS_MAX
    if extra > 0:
        for card in [card for card, row in plan.rows.items() if not _open(row)][:extra]:
            del plan.rows[card]


async def _render(adapter: Any, key: tuple, plan: _Plan) -> bool:
    """Post the plan, or edit it in place; False when Slack refused either."""
    chat_id, thread_ts = key
    title = _titles.get(key)
    _prune(plan)
    rows = list(plan.rows.values())
    blocks = _status.plan_blocks(title, rows)
    text = _status.plan_text(title, rows)
    try:
        client = adapter._get_client(chat_id, team_id=plan.team_id or None)
        if plan.ts:
            await client.chat_update(channel=chat_id, ts=plan.ts, text=text, blocks=blocks)
        else:
            result = await client.chat_postMessage(
                channel=chat_id, thread_ts=thread_ts, text=text, blocks=blocks,
            )
            plan.ts = str(result.get("ts") or "") if hasattr(result, "get") else ""
            if not plan.ts:
                raise RuntimeError("chat.postMessage returned no ts")
    except Exception as exc:  # noqa: BLE001 — the rolling line takes over
        logger.warning(
            "slack_ux_status: the plan in %s/%s failed (%s); using progress lines",
            chat_id, thread_ts, exc,
        )
        plan.fallback = True
        return False
    return True


async def _session(adapter: Any, key: tuple, plan: _Plan) -> None:
    """Send the session status the thread's plans now hold, with this plan's team.

    :func:`_plan_session` reads the current plan and any set aside: a card
    running opens the session, one waiting on the user suspends it, nothing
    clears it. Sent on every note and settle that moves a plan:
    :func:`set_thread_status` skips an unchanged status against what
    Slack last accepted, so a refused one is retried and one a Planning Agent
    turn changed is restored. The legacy setter has no such check and costs a
    call per note, beside the note's own edit. An unposted plan sends status
    during note delivery and card settlement, keeping the thread's session in
    sync with its rolling and waiting cards.
    """
    chat_id, thread_ts = key
    wanted = _plan_session(chat_id, thread_ts)
    setter = getattr(adapter, "_set_thread_status", None)
    if setter is None:
        return
    try:
        phrase = ""
        if wanted == _status.SESSION_PROCESSING:
            # Hermes's own phrase: the Agent Sessions path maps it to ``processing``,
            # and the legacy thread status, which takes free text, shows it as is.
            default_text = getattr(adapter, "_default_status_text", None)
            phrase = default_text(None) if callable(default_text) else _status.SESSION_PROCESSING
        await setter(chat_id, plan.team_id, thread_ts, phrase, PLAN_STATUS_LABEL)
    except Exception as exc:  # noqa: BLE001 — cosmetic
        logger.debug("slack_ux_status: setting the plan's session status failed: %s", exc)


def _arm(adapter: Any, key: tuple, plan: _Plan) -> None:
    """(Re)start the plan's lapse timer; with no running loop there is nothing to close later."""
    if plan.lapse is not None:
        plan.lapse.cancel()
        plan.lapse = None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    plan.lapse = loop.call_later(PLAN_HOLD_SECONDS, _start_lapse, adapter, key, plan)


def _disarm(plan: _Plan) -> None:
    for timer in (plan.lapse, plan.expiry):
        if timer is not None:
            timer.cancel()
    plan.lapse = plan.expiry = None


def _arm_expiry(adapter: Any, key: tuple, plan: _Plan, delay: float) -> None:
    """(Re)start the timer that drops a set-aside plan; with no running loop nothing drops it."""
    if plan.expiry is not None:
        plan.expiry.cancel()
        plan.expiry = None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    plan.expiry = loop.call_later(delay, _start_expiry, adapter, key, plan)


def _start_expiry(adapter: Any, key: tuple, plan: _Plan) -> None:
    plan.expiry = None
    task = asyncio.ensure_future(_expire(adapter, key, plan))
    _lapsing.add(task)
    task.add_done_callback(_lapsing.discard)


def _kept(plan: _Plan) -> bool:
    """Whether a set-aside plan waits on a person: a card waiting on the user, or one that gave up."""
    return _waiting(plan) or any(
        row.status == _status.TASK_ERROR and not row.archived for row in plan.rows.values()
    )


async def _expire(adapter: Any, key: tuple, plan: _Plan) -> None:
    """Drop a set-aside plan untouched for :data:`SET_ASIDE_MAX_SECONDS`, and send its session.

    A plan that waits on a person (:func:`_kept`) stays, bounded by the caps,
    and is looked at again a full period later, as is one a resumed card touched since.
    """
    plans = _lapsed.get(key)
    if not plans or not any(old is plan for old in plans):
        return  # dropped, evicted, or running as the thread's plan again
    remaining = SET_ASIDE_MAX_SECONDS - (time.monotonic() - plan.touched)
    if _kept(plan) or remaining > 0:
        _arm_expiry(adapter, key, plan, remaining if remaining > 0 else SET_ASIDE_MAX_SECONDS)
        return
    logger.info(
        "slack_ux_status: dropping the set-aside plan in %s/%s, quiet for %ss; resending its session",
        key[0], key[1], int(SET_ASIDE_MAX_SECONDS),
    )
    _disarm(plan)
    left = [old for old in plans if old is not plan]
    if left:
        _lapsed[key] = left
    else:
        _lapsed.pop(key, None)
    await _session(adapter, key, plan)


def _start_lapse(adapter: Any, key: tuple, plan: _Plan) -> None:
    plan.lapse = None
    task = asyncio.ensure_future(_lapse(adapter, key, plan))
    _lapsing.add(task)
    task.add_done_callback(_lapsing.discard)


async def _lapse(adapter: Any, key: tuple, plan: _Plan) -> None:
    """Set aside a plan nothing has touched for :data:`PLAN_HOLD_SECONDS`.

    Its session closes unless a card waits on the user. A row still running
    there lost its terminal event, or its card is quiet; the thread's next
    note starts a new plan rather than reopening this one. A posted plan with
    a row still running or waiting, or any plan with a card still rolling, joins
    :data:`_lapsed`, where that card's events still settle its row
    (:func:`settle_row`), a card waiting on the user keeps the session
    ``suspended``, and a rolling card's dashboard move still reaches its
    rolling message (:func:`_deliver_move`), the plan never posted included.
    """
    if time.monotonic() - plan.touched < PLAN_HOLD_SECONDS:
        return
    if _plans.get(key) is not plan:
        # Set aside already, and answered beside a newer plan: that hold ran out.
        if any(old is plan for old in _lapsed.get(key, ())):
            await _session(adapter, key, plan)
        return
    logger.info(
        "slack_ux_status: the plan in %s/%s had no news for %ss; setting it aside",
        key[0], key[1], int(PLAN_HOLD_SECONDS),
    )
    _plans.pop(key, None)
    if (plan.ts or plan.rolling) and _held(plan):
        await _set_aside(adapter, key, plan)
    await _session(adapter, key, plan)


async def _set_aside(adapter: Any, key: tuple, plan: _Plan) -> None:
    """Add a lapsed plan to :data:`_lapsed`, within both bounds.

    Past :data:`LAPSED_PER_THREAD` the oldest plan with no card waiting goes
    first, quiet ones before one answered within the hold, so a wait keeps its
    ``suspended`` and an answered card its ``processing`` while quiet cards
    come and go. A
    thread evicted at :data:`PLANS_MAX` has its session sent again, as
    :func:`_keep` does, so a wait that can no longer settle does not stay
    ``suspended``.
    """
    plans = [*_lapsed.get(key, ()), plan]
    while len(plans) > LAPSED_PER_THREAD:
        now = time.monotonic()
        idle = [old for old in plans if not _waiting(old)]
        quiet = [old for old in idle if now - old.touched >= PLAN_HOLD_SECONDS]
        dropped = (quiet or idle or plans)[0]
        _disarm(dropped)
        plans.remove(dropped)
    _lapsed[key] = plans
    _lapsed.move_to_end(key)
    if plan in plans:
        _arm_expiry(adapter, key, plan, SET_ASIDE_MAX_SECONDS)
    while len(_lapsed) > PLANS_MAX:
        old_key, evicted = _lapsed.popitem(last=False)
        for old in evicted:
            _disarm(old)
        logger.info(
            "slack_ux_status: evicting the set-aside plans in %s/%s; resending its session", *old_key,
        )
        posted = next((old for old in reversed(evicted) if old.ts), None)
        sender = posted or (evicted[-1] if evicted else None)
        if sender is not None:
            await _session(adapter, old_key, sender)


async def _keep(adapter: Any, key: tuple, plan: _Plan) -> None:
    """Remember a new plan, closing the session of any plan evicted at :data:`PLANS_MAX`.

    An evicted plan can still hold ``processing``, and once it is gone neither
    its timer nor a settle can reach it.
    """
    _plans[key] = plan
    _plans.move_to_end(key)
    while len(_plans) > PLANS_MAX:
        old_key, old = _plans.popitem(last=False)
        _disarm(old)
        logger.info("slack_ux_status: evicting the plan in %s/%s; closing its session", *old_key)
        await _session(adapter, old_key, old)


def _roll(adapter: Any, key: tuple, plan: _Plan, card: str) -> None:
    """Note that ``card`` is on a rolling line while the plan stands fallen back."""
    plan.rolling.add(card)
    plan.waiting.discard(card)  # a note means it runs
    plan.touched = time.monotonic()
    _arm(adapter, key, plan)


def _settled(plan: _Plan) -> bool:
    return not plan.rolling and not any(_live(row) for row in plan.rows.values())


def _move(row: _Row, kind: str, result: str = "") -> bool:
    """Apply a terminal or silent event to the card's row; False when it moves nothing."""
    if row.archived:
        return False  # upstream never unarchives, so a later event is a redelivery
    if kind == ARCHIVED_KIND:
        if not _open(row):
            return False  # archived after it finished: its row stands
        # Archived by hand: nothing else will settle the row.
        row.lines = [*row.lines, ARCHIVED_NOTE][-_status.STEPS_MAX:]
        row.note = ARCHIVED_NOTE
        row.status = _status.TASK_ERROR
        row.archived = True
        return True
    status = _status.task_status(kind)
    resumable = (_status.TASK_PENDING, _status.TASK_ERROR)
    if status is None or (kind == UNBLOCKED_KIND and row.status not in resumable):
        return False  # nothing to move, or an unblocked replay
    row.status = status
    if status == _status.TASK_COMPLETE:
        row.result = result
    return True


def _park(plan: _Plan, card: str, status: str | None, done: bool) -> bool:
    """Track whether a card rolling on the plan waits on the user; True when that changed."""
    if card not in plan.rolling or (status is None and not done):
        return False
    was = card in plan.waiting
    if status == _status.TASK_PENDING:
        plan.waiting.add(card)
    else:
        plan.waiting.discard(card)
    return was != (card in plan.waiting)


async def _settle_lapsed(
    adapter: Any, key: tuple, card: str, kind: str, status: str | None, done: bool, result: str = "",
) -> _Plan | None:
    """Settle the card on the plans the lapse set aside.

    A card answered there runs on that plan again: with no current plan it
    becomes the thread's plan once more, so its next note lands on its row;
    beside a newer one it is touched and timed like a current plan. Returns a
    plan that moved, for the caller to send the session status from.
    """
    plans = _lapsed.get(key)
    if not plans:
        return None
    changed = resumed = None
    for old in plans:
        unrolled = done and card in old.rolling
        parked = _park(old, card, status, done)
        if done:
            old.rolling.discard(card)
        row = old.rows.get(card)
        if row is not None and _move(row, kind, result):
            if row.status == _status.TASK_RUNNING:
                # Before the render's await, as in deliver_row: an expiry due
                # during it must not drop the plan the card resumes on.
                old.touched = time.monotonic()
                resumed = old
            await _render(adapter, key, old)
        elif not (parked or unrolled):
            continue
        elif card in old.rolling and card not in old.waiting:
            resumed = old
        changed = old
    if resumed is not None:
        resumed.touched = time.monotonic()
        _arm(adapter, key, resumed)
        if key not in _plans and _lapsed.get(key) is plans:
            # Replaced, not edited: another settle may be iterating the old list.
            plans = _lapsed[key] = [old for old in plans if old is not resumed]
            await _keep(adapter, key, resumed)
    if changed is not None and _lapsed.get(key) is plans:
        left = [old for old in plans if _held(old)]
        for old in plans:
            if old not in left:
                _disarm(old)
        if left:
            _lapsed[key] = left
        else:
            _lapsed.pop(key, None)
    return changed


async def _settle_current(
    adapter: Any, key: tuple, plan: _Plan, card: str, kind: str, status: str | None, done: bool,
    result: str = "",
) -> bool:
    """Settle the card on the thread's current plan; True when anything moved."""
    unrolled = done and card in plan.rolling
    parked = _park(plan, card, status, done)
    if done:
        plan.rolling.discard(card)
    row = plan.rows.get(card)
    moved = row is not None and _move(row, kind, result)
    if not (moved or unrolled or parked):
        return False
    plan.touched = time.monotonic()
    if moved and plan.ts:
        # Best effort on a fallen-back plan too, so its rows do not stay running.
        await _render(adapter, key, plan)
    # A plan that fell back is still forgotten once its cards settle, so the
    # thread's next card tries a plan again.
    if _settled(plan):
        _disarm(plan)
        if _plans.get(key) is plan:
            _plans.pop(key, None)
            if plan.ts and _held(plan):
                await _set_aside(adapter, key, plan)  # a card that gave up waits there for its unblock
    else:
        _arm(adapter, key, plan)
    return True


async def _deliver_move(
    adapter: Any, sub: dict, key: tuple, card: str, event_id: int, line: str, moved: str,
) -> bool:
    """Put a dashboard move on the card's row, settling it per :data:`MOVE_KINDS`.

    A move opens no row: one for a card with no row on the current plan is
    taken and dropped, unless the plan fell back or the card rolls on a plan
    set aside, where the card's rolling message takes it.
    """
    plan = _plans.get(key)
    row = plan.rows.get(card) if plan is not None and not plan.fallback else None
    if row is not None:
        if event_id and event_id <= row.last_event_id:
            return True  # an at-least-once replay already on the row
        previous = (list(row.lines), row.last_event_id)
        row.lines = [*row.lines, line][-_status.STEPS_MAX:]
        row.last_event_id = max(row.last_event_id, event_id)
    kind = MOVE_KINDS.get(moved)
    if kind is not None:
        await _settle(adapter, sub, kind, "", "", opens=False)
        refused = row is not None and plan.fallback  # the settle's edit was refused
    else:
        refused = row is not None and not await _render(adapter, key, plan)
    if refused:
        row.lines, row.last_event_id = previous
        return False
    rolls = any(card in old.rolling for old in _lapsed.get(key, ()))
    return row is not None or not (rolls or (plan is not None and plan.fallback))


async def deliver_row(
    adapter: Any, sub: dict, event_id: int, title: str, line: str, moved: str | None = None,
) -> bool:
    """Put a progress note on the card's row in the thread's plan.

    True when the plan took it, which the caller reports as delivered; False
    when the caller should roll it into a progress line instead: no thread, no
    Slack client, or a plan that fell back, on this note or an earlier one.
    ``moved`` is the column a ``status`` event moved the card to, which
    :func:`_deliver_move` handles rather than marking the row running.
    """
    key = _thread(sub)
    card = str(sub.get("task_id") or "")
    if not (key[0] and key[1] and card and hasattr(adapter, "_get_client")):
        return False
    if moved is not None:
        # Not seen: a move opens no row, so the card's terminal event still must.
        return await _deliver_move(adapter, sub, key, card, event_id, line, moved)
    _remember(_seen, (*key, card), True, SEEN_MAX)
    plan = _plans.get(key)
    if plan is None:
        plan = _Plan(str(sub.get("team_id") or ""))
        await _keep(adapter, key, plan)
    else:
        _plans.move_to_end(key)  # eviction at PLANS_MAX takes the least active thread
    if plan.fallback:
        _roll(adapter, key, plan, card)
        await _session(adapter, key, plan)
        return False
    row = plan.rows.get(card)
    created = row is None
    if created:
        row = plan.rows[card] = _Row(card, title)
    if event_id and event_id <= row.last_event_id:
        return True  # an at-least-once replay already on the row
    previous = (list(row.lines), row.steps, row.note, row.status, row.last_event_id)
    row.lines = [*row.lines, line][-_status.STEPS_MAX:]
    row.steps += 1
    row.note = line
    row.status = _status.TASK_RUNNING
    row.last_event_id = max(row.last_event_id, event_id)
    # Before the render's await, so a lapse due during it sees the note and
    # does not set the plan aside under it.
    plan.touched = time.monotonic()
    if not await _render(adapter, key, plan):
        if created:
            # Never shown, so no event could settle it: the card is rolling now.
            del plan.rows[card]
        else:
            row.lines, row.steps, row.note, row.status, row.last_event_id = previous
        _roll(adapter, key, plan, card)
        await _session(adapter, key, plan)
        return False
    _arm(adapter, key, plan)
    await _session(adapter, key, plan)
    return True


async def settle_row(adapter: Any, sub: dict, kind: str, result: str = "", title: str = "") -> bool:
    """Settle the card's row after a terminal event, on the thread's plan and on any set aside.

    ``result`` is a completed card's one line, which its row shows once settled.
    A card that completes, waits on the user or gives up with no row on any of
    the thread's plans gets one, led by ``title``, the card's. True when the
    plan now shows the card complete, which lets a child card's report fold
    into it (``kanban_progress_lines``).
    """
    return await _settle(adapter, sub, kind, result, title, opens=True)


async def _settle(adapter: Any, sub: dict, kind: str, result: str, title: str, opens: bool) -> bool:
    key = _thread(sub)
    card = str(sub.get("task_id") or "")
    status = _status.task_status(kind)
    done = kind == ARCHIVED_KIND or status in (_status.TASK_COMPLETE, _status.TASK_ERROR)
    opens = opens and (*key, card) not in _seen and status not in (None, _status.TASK_RUNNING)
    if opens:
        _remember(_seen, (*key, card), True, SEEN_MAX)
    # Read before the settle, which may forget or set aside the plan it settles.
    plans = [*_lapsed.get(key, ())]
    sender = await _settle_lapsed(adapter, key, card, kind, status, done, result)
    plan = _plans.get(key)
    plans.append(plan)
    if plan is not None and await _settle_current(adapter, key, plan, card, kind, status, done, result):
        sender = plan
    opened = None
    if sender is not None:
        await _session(adapter, key, sender)
    elif opens:
        opened = await _open_settled(adapter, sub, key, card, status, title, result)
        plans.append(opened)
    if sender is None and opened is None and plan is None and not _lapsed.get(key):
        # An archive with no row in this process is cleanup of a card that
        # finished long ago, not a settle: the thread may hold another card.
        await _settle_orphan(adapter, sub, key, status, done and kind != ARCHIVED_KIND)
    return any(_shows_complete(p, card) for p in plans if p is not None)


def _shows_complete(plan: _Plan, card: str) -> bool:
    """Whether the plan's last post or edit landed with the card's row complete."""
    row = plan.rows.get(card)
    return bool(plan.ts) and not plan.fallback and row is not None and row.status == _status.TASK_COMPLETE


async def _open_settled(
    adapter: Any, sub: dict, key: tuple, card: str, status: str, title: str, result: str,
) -> _Plan | None:
    """Add a row, already settled, for a card that sent no note; the plan, or None when there is nowhere to put it.

    The row joins the thread's plan, or starts one, and the plan is then kept,
    set aside or forgotten exactly as a settle on an existing row leaves it. A
    refused post or edit drops the row and leaves the plan fallen back, as a
    refused note does, with a card waiting on the user rolling on it.
    """
    if not (key[0] and key[1] and card and hasattr(adapter, "_get_client")):
        return None
    plan = _plans.get(key)
    if plan is None:
        plan = _Plan(str(sub.get("team_id") or ""))
        await _keep(adapter, key, plan)
    elif plan.fallback:
        # The thread is on rolling lines until its cards settle. A card waiting
        # on the user rolls there, as on a refused render, so the plan keeps its wait.
        if status != _status.TASK_PENDING:
            return None
        plan.rolling.add(card)
        plan.waiting.add(card)
        plan.touched = time.monotonic()
        _arm(adapter, key, plan)
        await _session(adapter, key, plan)
        return plan
    else:
        _plans.move_to_end(key)
    row = plan.rows[card] = _Row(card, title)
    row.status = status
    if status == _status.TASK_COMPLETE:
        row.result = result
    plan.touched = time.monotonic()
    if not await _render(adapter, key, plan):
        del plan.rows[card]  # never shown
        if status == _status.TASK_PENDING:
            # Rolling, as a refused note leaves it, so the plan keeps its wait.
            plan.rolling.add(card)
            plan.waiting.add(card)
    if _settled(plan):
        _disarm(plan)
        if _plans.get(key) is plan:
            _plans.pop(key, None)
            if plan.ts and _held(plan):
                await _set_aside(adapter, key, plan)  # a card that gave up waits there for its unblock
    else:
        _arm(adapter, key, plan)
    sent = _sessions.get(key)
    if _plan_session(*key) or not (sent and sent[0] == _status.SESSION_PROCESSING):
        # With nothing left to hold, a Planning Agent turn working in the
        # thread is left to clear the session itself, as _settle_orphan leaves it.
        await _session(adapter, key, plan)
    return plan


async def _settle_orphan(adapter: Any, sub: dict, key: tuple, status: str | None, done: bool) -> None:
    """Clear or suspend the session for a card no plan in this process holds.

    After a gateway restart Slack still shows the Working… the old process
    set, and no plan is left to clear it. A settled card closes the session
    and one waiting on the user suspends it; a running or retried card leaves
    it. :func:`set_thread_status` skips a status it already sent, so this goes
    once, and a Planning Agent turn holding the session in this process is
    left to clear it itself.
    """
    if done:
        wanted = _status.SESSION_CLOSED
    elif status == _status.TASK_PENDING:
        wanted = _status.SESSION_SUSPENDED
    else:
        # The card runs again, so a wait whose send failed is over too.
        _orphan_waits.pop(key, None)
        return
    sent = _sessions.get(key)
    setter = getattr(adapter, "_set_thread_status", None)
    if not (key[0] and key[1]) or setter is None or (sent and sent[0] == _status.SESSION_PROCESSING):
        return
    chat_id, thread_ts = key
    # The legacy setter takes free text, so it gets only a clear, as from
    # :func:`_session`; the Agent Sessions path turns it back into the wait.
    if wanted == _status.SESSION_SUSPENDED:
        _remember(_orphan_waits, key, wanted, SESSIONS_MAX)
    else:
        _orphan_waits.pop(key, None)
    try:
        await setter(chat_id, str(sub.get("team_id") or ""), thread_ts, "", PLAN_STATUS_LABEL)
    except Exception as exc:  # noqa: BLE001 — cosmetic
        logger.debug("slack_ux_status: setting the session status after a restart failed: %s", exc)
