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
**The session status (#576).** With slack-sdk 3.44 or later, Hermes sends its
thread status to ``agents.sessions.setStatus``: a phrase ("is thinking...")
every 2 seconds while a turn runs, and ``""`` to clear. That method takes
``processing``, ``suspended`` or ``closed`` and nothing else, so every call
fails ``invalid_arguments``, logged at debug: Working… never shows, the clear
never lands, and the refresh loop spends 30 Slack calls a minute on errors.
With the flag on, the phrase becomes ``processing`` and the clear
``closed``, and a status is sent only when it changes or
:data:`SESSION_REFRESH_SECONDS` have passed, so a turn costs a call or two
rather than 30 a minute. Each open and close is logged at info. The legacy
``assistant.threads.setStatus``, used when the SDK has no Agent Sessions, takes
free text and is left to upstream.

**The session title.** Upstream titles only DM threads. With the flag on, a
channel thread's first ask becomes its session title, set right after a
``processing`` lands: ``agents.sessions.rename`` refuses a thread with no
session yet. A refused rename keeps the ask for the next ``processing`` sent;
once the title is set, a follow-up ask in the thread keeps it.

**One plan per thread.** ``kanban_progress_lines`` rolls each card's progress
notes into one message per card. With the flag on, a Slack thread instead gets
one message holding a plan with a row per card, edited in place with
``chat.update`` as notes arrive. A heartbeat creates a card's row; a terminal
event settles an existing row (``complete``, ``error``, or ``pending`` for a
card waiting on the user) and never creates one, so a card that finished
without a note adds no plan above its report. Two kinds upstream never posts
reach the plan through ``kanban_progress_lines.silent_event``:
``unblocked`` sets a waiting row running again, and ``archived`` settles a
running or waiting row as stopped, with an ``Archived`` note. The row stays
rather than the message going: the credential proxy refuses ``chat.delete``,
as it refuses every destructive Slack verb. ``crashed`` and ``timed_out``
leave the row running, since the dispatcher retries the card, and
``block_loop_detected`` sets it waiting, as ``slack_presenter`` reads them.
The plan holds the thread's session status: ``processing`` while a row runs,
``suspended`` while rows wait on the user and none runs, and a Planning Agent
turn ending in the thread does not close it under either. Once no row is
running or waiting, the session is closed and the plan is forgotten here, and
the thread's next card starts a new plan. A plan with no note or settled row
for :data:`PLAN_HOLD_SECONDS` closes the session itself; its next note opens
it again. A plan evicted at :data:`PLANS_MAX` closes its session on the way
out, since nothing else would. The plan has no Stop button yet: ``/stop`` interrupts only the
Planning Agent's turn, and the cards would run on.

Fallback: when posting or editing the plan fails (Slack refuses the blocks, the
message was deleted), the thread drops to the rolling line until every card
that rolled a note since has settled or been archived, which is what the
thread showed before this module. A fallen-back plan with no news for
:data:`PLAN_HOLD_SECONDS` is forgotten, so a card whose terminal event was
lost does not keep the thread off the plan for good. Everything here is in process,
like the progress-line map: a gateway restart forgets the plan, and the next
note starts a new one.
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
#: exactly when that module cannot be imported.
FLAG_ON_VALUES = frozenset({"1", "true", "yes", "on"})

#: The platform name kanban subscriptions carry for Slack.
PLATFORM = "slack"

#: How long an unchanged ``processing`` stands before it is sent again. Slack
#: documents no expiry for an agent session's status; this bounds how stale a
#: status can go if one ever does, at one call a minute instead of thirty.
SESSION_REFRESH_SECONDS = 60.0

#: How long a plan with no new note or settled row holds the session open
#: before closing it. A card whose terminal event never reaches the thread (a
#: dropped subscription, a lost event) leaves its row running for good; this
#: stops that row holding Working… forever, while covering a card's silent
#: stretches, since noteless heartbeats reach no one.
PLAN_HOLD_SECONDS = 1800.0

#: ``fail_label`` for the adapter's status setter when the plan sets it.
PLAN_STATUS_LABEL = "plan"

#: The notifier kind for a card archived by hand: its row settles as stopped,
#: with :data:`ARCHIVED_NOTE` as its last step.
ARCHIVED_KIND = "archived"
ARCHIVED_NOTE = "Archived"

#: The notifier kind for a card the user unblocked: its waiting row runs again.
UNBLOCKED_KIND = "unblocked"

#: Bounds on the in-process maps, oldest evicted first.
SESSIONS_MAX = 512
ASKS_MAX = 512
PLANS_MAX = 256

_warned_missing = False


class _Row:
    """One card's row. A plain class, for the reason ``slack_ux_reactions._Ask`` is."""

    __slots__ = ("last_event_id", "lines", "status", "task_id", "title")

    def __init__(self, task_id: str, title: str) -> None:
        self.task_id = task_id
        self.title = title
        self.lines: list[str] = []
        self.status = ""
        self.last_event_id = 0


class _Plan:
    __slots__ = ("fallback", "lapse", "rolling", "rows", "team_id", "touched", "ts")

    def __init__(self, team_id: str) -> None:
        self.ts = ""
        self.team_id = team_id
        self.rows: OrderedDict[str, _Row] = OrderedDict()
        self.fallback = False
        #: Cards whose notes went to a rolling line after the plan fell back.
        #: The plan is kept until each has settled, so none of them opens a
        #: second plan beside its rolling message.
        self.rolling: set[str] = set()
        #: ``time.monotonic()`` at the last note or settled row.
        self.touched = time.monotonic()
        #: The timer that closes the session after :data:`PLAN_HOLD_SECONDS`.
        self.lapse: asyncio.TimerHandle | None = None


#: ``(channel, thread) -> (status sent, when)``. No team: a kanban
#: subscription carries none, so the plan's sends and the Planning Agent's
#: turn must share one entry or each skips a change the other made.
_sessions: OrderedDict[tuple, tuple] = OrderedDict()
#: ``(channel, thread) -> the ask's words``, waiting for the session to open.
_asks: OrderedDict[tuple, str] = OrderedDict()
#: ``(channel, thread) -> the title set on the thread``, which the plan shows too.
_titles: OrderedDict[tuple, str] = OrderedDict()
#: ``(channel, thread) -> _Plan``.
_plans: OrderedDict[tuple, _Plan] = OrderedDict()
#: Lapse tasks in flight, held so the loop does not drop them mid-run.
_lapsing: set = set()


def enabled() -> bool:
    """Whether ``KAGE_SLACK_UX`` is on and the presenter and renderer are importable."""
    global _warned_missing
    if _presenter is not None and _status is not None:
        return _presenter.enabled()
    if os.environ.get(FLAG_ENV, "").strip().lower() in FLAG_ON_VALUES and not _warned_missing:
        _warned_missing = True
        logger.warning(
            "slack_ux_status: %s is set but slack_presenter or slack_status is not "
            "importable; treating the flag as off", FLAG_ENV,
        )
    return False


def _remember(store: OrderedDict, key: Any, value: Any, cap: int) -> None:
    store[key] = value
    store.move_to_end(key)
    while len(store) > cap:
        store.popitem(last=False)


# --- the session -----------------------------------------------------------


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
    if wanted == _status.SESSION_CLOSED:
        # A Planning Agent turn ends with a clear; the thread's plan outlives it.
        wanted = _plan_session(str(chat_id), str(thread_ts)) or wanted
    key = (str(chat_id), str(thread_ts))
    sent = _sessions.get(key)
    now = time.monotonic()
    if sent and sent[0] == wanted and (
        wanted != _status.SESSION_PROCESSING or now - sent[1] < SESSION_REFRESH_SECONDS
    ):
        return
    client = adapter._get_client(chat_id, team_id=team_id)
    try:
        await status_method(client)(channel_id=chat_id, thread_ts=thread_ts, status=wanted)
    except Exception as exc:  # noqa: BLE001 — upstream debug-logs its own failures too
        logger.debug("[Slack] agents.sessions.setStatus %s: %s", fail_label, exc)
        return
    if not sent or sent[0] != wanted:
        logger.info("slack_ux_status: session %s in %s/%s", wanted, chat_id, thread_ts)
    _remember(_sessions, key, (wanted, now), SESSIONS_MAX)
    if wanted != _status.SESSION_PROCESSING or key not in _asks:
        return
    title = _status.session_title(_asks[key])
    if title:
        try:
            await title_method(client)(channel_id=chat_id, thread_ts=thread_ts, title=title)
        except Exception as exc:  # noqa: BLE001 — a title is cosmetic
            # The ask stays, so the next processing sent retries it and a
            # follow-up ask does not take its place.
            logger.debug("[Slack] agents.sessions.rename failed: %s", exc)
            return
        _remember(_titles, key, title, ASKS_MAX)
    _asks.pop(key, None)


# --- the plan --------------------------------------------------------------


def _thread(sub: dict) -> tuple:
    return str(sub.get("chat_id") or ""), str(sub.get("thread_id") or "")


def _plan_session(chat_id: str, thread_ts: str) -> str:
    """The session status the thread's plan holds, or ``""`` when no row runs or waits.

    A plan untouched for :data:`PLAN_HOLD_SECONDS` holds nothing.
    """
    plan = _plans.get((chat_id, thread_ts))
    if plan is None or time.monotonic() - plan.touched >= PLAN_HOLD_SECONDS:
        return ""
    if _status.running(plan.rows.values()):
        return _status.SESSION_PROCESSING
    if any(row.status == _status.TASK_PENDING for row in plan.rows.values()):
        return _status.SESSION_SUSPENDED
    return ""


async def _render(adapter: Any, key: tuple, plan: _Plan) -> bool:
    """Post the plan, or edit it in place; False when Slack refused either."""
    chat_id, thread_ts = key
    title = _titles.get(key)
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
    """Send the session status the plan now holds, through the adapter's setter.

    A row running opens the session; nothing running clears it, which
    :func:`set_thread_status` turns into ``suspended`` while a row still waits
    and the legacy thread status shows as cleared. Sent on every note and
    settle: :func:`set_thread_status` skips an unchanged status against what
    Slack last accepted, so a refused one is retried and one a Planning Agent
    turn changed is restored. The legacy setter has no such check and costs a
    call per note, beside the note's own edit.
    """
    chat_id, thread_ts = key
    wanted = _plan_session(chat_id, thread_ts) if _plans.get(key) is plan else ""
    setter = getattr(adapter, "_set_thread_status", None)
    if setter is None:
        return
    phrase = ""
    if wanted == _status.SESSION_PROCESSING:
        # Hermes's own phrase: the Agent Sessions path maps it to ``processing``,
        # and the legacy thread status, which takes free text, shows it as is.
        default_text = getattr(adapter, "_default_status_text", None)
        phrase = default_text(None) if callable(default_text) else _status.SESSION_PROCESSING
    try:
        await setter(chat_id, plan.team_id, thread_ts, phrase, PLAN_STATUS_LABEL)
    except Exception as exc:  # noqa: BLE001 — cosmetic
        logger.debug("slack_ux_status: setting the plan's session status failed: %s", exc)


def _arm(adapter: Any, key: tuple, plan: _Plan) -> None:
    """(Re)start the plan's lapse timer; with no running loop there is nothing to close later."""
    _disarm(plan)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    plan.lapse = loop.call_later(PLAN_HOLD_SECONDS, _start_lapse, adapter, key, plan)


def _disarm(plan: _Plan) -> None:
    if plan.lapse is not None:
        plan.lapse.cancel()
        plan.lapse = None


def _start_lapse(adapter: Any, key: tuple, plan: _Plan) -> None:
    plan.lapse = None
    task = asyncio.ensure_future(_lapse(adapter, key, plan))
    _lapsing.add(task)
    task.add_done_callback(_lapsing.discard)


async def _lapse(adapter: Any, key: tuple, plan: _Plan) -> None:
    """Close the session of a plan nothing has touched for :data:`PLAN_HOLD_SECONDS`.

    The plan stays: a late note puts its row back and opens the session again.
    """
    if _plans.get(key) is not plan or time.monotonic() - plan.touched < PLAN_HOLD_SECONDS:
        return
    if plan.fallback:
        # Its rolling cards went quiet; the next note tries a plan again.
        logger.info(
            "slack_ux_status: the fallen-back plan in %s/%s had no news for %ss; forgetting it",
            key[0], key[1], int(PLAN_HOLD_SECONDS),
        )
        _plans.pop(key, None)
        if plan.ts:
            await _session(adapter, key, plan)
        return
    logger.info(
        "slack_ux_status: the plan in %s/%s had no news for %ss; closing its session",
        key[0], key[1], int(PLAN_HOLD_SECONDS),
    )
    await _session(adapter, key, plan)


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
        if old.ts:
            logger.info("slack_ux_status: evicting the plan in %s/%s; closing its session", *old_key)
            await _session(adapter, old_key, old)


def _roll(adapter: Any, key: tuple, plan: _Plan, card: str) -> None:
    """Note that ``card`` is on a rolling line while the plan stands fallen back."""
    plan.rolling.add(card)
    plan.touched = time.monotonic()
    _arm(adapter, key, plan)


def _settled(plan: _Plan) -> bool:
    return not plan.rolling and not any(
        row.status in (_status.TASK_RUNNING, _status.TASK_PENDING) for row in plan.rows.values()
    )


async def deliver_row(
    adapter: Any, sub: dict, event_id: int, title: str, line: str,
) -> bool:
    """Put a progress note on the card's row in the thread's plan.

    True when the plan took it, which the caller reports as delivered; False
    when the caller should roll it into a progress line instead: no thread, no
    Slack client, or a plan that fell back, on this note or an earlier one.
    """
    key = _thread(sub)
    card = str(sub.get("task_id") or "")
    if not (key[0] and key[1] and card and hasattr(adapter, "_get_client")):
        return False
    plan = _plans.get(key)
    if plan is None:
        plan = _Plan(str(sub.get("team_id") or ""))
        await _keep(adapter, key, plan)
    if plan.fallback:
        _roll(adapter, key, plan, card)
        return False
    row = plan.rows.get(card)
    created = row is None
    if created:
        row = plan.rows[card] = _Row(card, title)
    if event_id and event_id <= row.last_event_id:
        return True  # an at-least-once replay already on the row
    previous = (list(row.lines), row.status, row.last_event_id)
    row.lines = [*row.lines, line][-_status.STEPS_MAX:]
    row.status = _status.TASK_RUNNING
    row.last_event_id = max(row.last_event_id, event_id)
    if not await _render(adapter, key, plan):
        if created:
            # Never shown, so no event could settle it: the card is rolling now.
            del plan.rows[card]
        else:
            row.lines, row.status, row.last_event_id = previous
        _roll(adapter, key, plan, card)
        return False
    plan.touched = time.monotonic()
    _arm(adapter, key, plan)
    await _session(adapter, key, plan)
    return True


async def settle_row(adapter: Any, sub: dict, kind: str) -> None:
    """Settle the card's row after a terminal event, if the thread's plan has one."""
    key = _thread(sub)
    plan = _plans.get(key)
    card = str(sub.get("task_id") or "")
    if plan is None:
        return
    status = _status.task_status(kind)
    done = kind == ARCHIVED_KIND or status in (_status.TASK_COMPLETE, _status.TASK_ERROR)
    unrolled = done and card in plan.rolling
    if done:
        plan.rolling.discard(card)
    row = plan.rows.get(card)
    moved = row is not None and (status is not None or kind == ARCHIVED_KIND)
    live = (_status.TASK_RUNNING, _status.TASK_PENDING)
    if moved and (
        (kind == UNBLOCKED_KIND and row.status != _status.TASK_PENDING)
        or (kind == ARCHIVED_KIND and row.status not in live)
    ):
        # A replay, or a card archived after it finished: its row stands.
        moved = False
    if not (moved or unrolled):
        return
    if moved and kind == ARCHIVED_KIND:
        # Archived by hand: nothing else will settle the row.
        row.lines = [*row.lines, ARCHIVED_NOTE][-_status.STEPS_MAX:]
        row.status = _status.TASK_ERROR
    elif moved:
        row.status = status
    plan.touched = time.monotonic()
    if moved and plan.ts and not plan.fallback:
        await _render(adapter, key, plan)
    # A plan that fell back is still forgotten once its cards settle, so the
    # thread's next card tries a plan again.
    if _settled(plan):
        _disarm(plan)
        if _plans.get(key) is plan:
            _plans.pop(key, None)
    else:
        _arm(adapter, key, plan)
    if plan.ts:
        await _session(adapter, key, plan)
