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
channel ask's words become its thread's session title, set once, right after
the first ``processing``: ``agents.sessions.rename`` refuses a thread with no
session yet.

**One plan per thread.** ``kanban_progress_lines`` rolls each card's progress
notes into one message per card. With the flag on, a Slack thread instead gets
one message holding a plan with a row per card, edited in place with
``chat.update`` as notes arrive. A heartbeat creates a card's row; a terminal
event settles an existing row (``complete``, ``error``, or ``pending`` for a
card waiting on the user) and never creates one, so a card that finished
without a note adds no plan above its report. While a row runs, the plan
carries the ``kage_stop`` button and the thread's session is set to
``processing``. Once no row is running or waiting, the session is closed
and the plan is forgotten here, and the thread's next card starts a new plan.

**Stop.** ``gateway/slack_ux_clicks.py`` answers a Stop click as the clicker
typing ``/stop`` and removes the button from the message; a plan edit after
that must not put it back, so a plan whose Stop was answered is rendered
without it. Stop acts on the thread, not on the viewer, so once clicked it is
gone for everyone.

Fallback: when posting or editing the plan fails (Slack refuses the blocks, the
message was deleted), the thread drops to the rolling line until the plan's
cards settle, which is what the thread showed before this module. Everything here is in process,
like the progress-line map: a gateway restart forgets the plan, and the next
note starts a new one.
"""

from __future__ import annotations

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

#: ``fail_label`` for the adapter's status setter when the plan sets it.
PLAN_STATUS_LABEL = "plan"

#: ``slack_ux_clicks``'s record of an answered Stop, keyed
#: ``(channel, message ts, STOP_KIND)``.
CLICKS_STOP_KIND = "kage stop"

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
    __slots__ = ("fallback", "rows", "team_id", "ts")

    def __init__(self, team_id: str) -> None:
        self.ts = ""
        self.team_id = team_id
        self.rows: OrderedDict[str, _Row] = OrderedDict()
        self.fallback = False


#: ``(team, channel, thread) -> (status sent, when)``.
_sessions: OrderedDict[tuple, tuple] = OrderedDict()
#: ``(channel, thread) -> the ask's words``, waiting for the session to open.
_asks: OrderedDict[tuple, str] = OrderedDict()
#: ``(channel, thread) -> the title set on the thread``, which the plan shows too.
_titles: OrderedDict[tuple, str] = OrderedDict()
#: ``(channel, thread) -> _Plan``.
_plans: OrderedDict[tuple, _Plan] = OrderedDict()


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
    """Keep a channel ask's words to title its thread's session once it opens."""
    if not (chat_id and thread_ts and enabled()):
        return
    if str(text or "").strip():
        _remember(_asks, (str(chat_id), str(thread_ts)), str(text), ASKS_MAX)


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
    key = (str(team_id or ""), str(chat_id), str(thread_ts))
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
    if wanted != _status.SESSION_PROCESSING:
        return
    ask = _asks.pop((str(chat_id), str(thread_ts)), None)
    title = _status.session_title(ask)
    if not title:
        return
    try:
        await title_method(client)(channel_id=chat_id, thread_ts=thread_ts, title=title)
    except Exception as exc:  # noqa: BLE001 — a title is cosmetic
        logger.debug("[Slack] agents.sessions.rename failed: %s", exc)
        return
    _remember(_titles, (str(chat_id), str(thread_ts)), title, ASKS_MAX)


# --- the plan --------------------------------------------------------------


def _thread(sub: dict) -> tuple:
    return str(sub.get("chat_id") or ""), str(sub.get("thread_id") or "")


def _stop_answered(chat_id: str, ts: str) -> bool:
    """Whether ``slack_ux_clicks`` has answered a Stop click on this message."""
    try:
        from gateway import slack_ux_clicks
    except ImportError:
        return False
    answered = getattr(slack_ux_clicks, "_answered", None) or {}
    return (chat_id, ts, CLICKS_STOP_KIND) in answered


async def _render(adapter: Any, key: tuple, plan: _Plan) -> bool:
    """Post the plan, or edit it in place; False when Slack refused either."""
    chat_id, thread_ts = key
    title = _titles.get(key)
    rows = list(plan.rows.values())
    stop = not (plan.ts and _stop_answered(chat_id, plan.ts))
    blocks = _status.plan_blocks(title, rows, stop=stop)
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


async def _session(adapter: Any, chat_id: str, team_id: str, thread_ts: str, opening: bool) -> None:
    """Open the thread's session when its plan is posted, or clear it once the plan settles."""
    setter = getattr(adapter, "_set_thread_status", None)
    if setter is None:
        return
    phrase = ""
    if opening:
        # Hermes's own phrase: the Agent Sessions path maps it to ``processing``,
        # and the legacy thread status, which takes free text, shows it as is.
        default_text = getattr(adapter, "_default_status_text", None)
        phrase = default_text(None) if callable(default_text) else _status.SESSION_PROCESSING
    try:
        await setter(chat_id, team_id, thread_ts, phrase, PLAN_STATUS_LABEL)
    except Exception as exc:  # noqa: BLE001 — cosmetic
        logger.debug("slack_ux_status: setting the plan's session status failed: %s", exc)


def _settled(plan: _Plan) -> bool:
    return not any(
        row.status in (_status.TASK_RUNNING, _status.TASK_PENDING) for row in plan.rows.values()
    )


async def deliver_row(
    adapter: Any, sub: dict, event_id: int, title: str, line: str,
) -> bool:
    """Put a progress note on the card's row in the thread's plan.

    True when the plan took it, which the caller reports as delivered; False
    when the caller should roll it into a progress line instead: no thread, no
    Slack client, or a plan that already fell back.
    """
    key = _thread(sub)
    card = str(sub.get("task_id") or "")
    if not (key[0] and key[1] and card and hasattr(adapter, "_get_client")):
        return False
    plan = _plans.get(key)
    if plan is None:
        plan = _Plan(str(sub.get("team_id") or ""))
        _remember(_plans, key, plan, PLANS_MAX)
    if plan.fallback:
        return False
    row = plan.rows.get(card)
    if row is None:
        row = plan.rows[card] = _Row(card, title)
    if event_id and event_id <= row.last_event_id:
        return True  # an at-least-once replay already on the row
    opening = not plan.ts
    previous = (list(row.lines), row.status, row.last_event_id)
    row.lines = [*row.lines, line][-_status.STEPS_MAX:]
    row.status = _status.TASK_RUNNING
    row.last_event_id = max(row.last_event_id, event_id)
    if not await _render(adapter, key, plan):
        row.lines, row.status, row.last_event_id = previous
        return False
    if opening:
        await _session(adapter, key[0], plan.team_id, key[1], opening=True)
    return True


async def settle_row(adapter: Any, sub: dict, kind: str) -> None:
    """Settle the card's row after a terminal event, if the thread's plan has one."""
    key = _thread(sub)
    plan = _plans.get(key)
    row = plan.rows.get(str(sub.get("task_id") or "")) if plan else None
    status = _status.task_status(kind)
    if row is None or status is None:
        return
    row.status = status
    if plan.ts and not plan.fallback:
        await _render(adapter, key, plan)
    # A plan that fell back is still forgotten once its rows settle, so the
    # thread's next card tries a plan again.
    if _settled(plan) and _plans.get(key) is plan:
        _plans.pop(key, None)
        if plan.ts:
            await _session(adapter, key[0], plan.team_id, key[1], opening=False)
