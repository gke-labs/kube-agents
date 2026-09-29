"""React to each Slack ask by kind, and again when the work settles.

Installed into the image at ``/opt/hermes/gateway/slack_ux_reactions.py``.
``apply_slack_ux_reactions.py`` makes the Slack adapter's two reaction hooks
(``on_processing_start``, ``on_processing_complete``) hand over to this module
when ``KAGE_SLACK_UX`` is on, and ``apply_kanban_progress_lines.py`` calls
:func:`settle_delegated` after the kanban notifier delivers a terminal event.
With the flag off neither caller reaches anything here, and the adapter keeps
upstream's 👀 then ✅/❌.

Upstream, and why it changes
----------------------------
Upstream adds 👀 when a turn starts, then removes it and adds ✅ or ❌ when the
turn ends. Two things go wrong in a kube-agents install:

* The credential proxy refuses every Slack method ending in ``remove``, so the
  👀 never goes and the ✅ lands beside it anyway.
* The turn that delegates ends in seconds, with an acknowledgement; the work
  runs on the kanban board for minutes after. Upstream's ✅ says "done" at the
  moment the work starts.

With the flag on:

* The arrival reaction says what kind of ask this is, chosen from its words
  before any model call (``slack_presenter.arrival_reaction``): 👀 a question
  or check, 🛠️ a change, 📋 the board, 🚨 an incident.
* Nothing is ever removed.
* A turn that answered directly settles at once: ✅, or ❌ on failure. A
  cancelled turn adds nothing.
* A turn that put new cards on the board, subscribed to this thread, defers
  its settle to the cards. The kanban notifier calls :func:`settle_delegated`
  on each terminal event: ⏸️ as soon as a card blocks on the user, and ✅/❌
  once the last open card subscribed to the thread settles, so a fan-out
  settles once, when all of it has.

The deferred asks live in this process only (the notifier runs in the gateway
process too). A gateway restart between the turn and the settle loses the
settle, and the ask keeps its arrival reaction alone; nothing is ever put on a
message this process did not see arrive.

Fail-soft throughout: a kanban read or a reaction that fails is logged at
debug and the turn carries on, as upstream's ``_react`` already does.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

try:
    import slack_presenter as _presenter
except ImportError:  # the scripts directory is not on PYTHONPATH
    _presenter = None

#: The flag, read here only to word the warning when the presenter is missing.
FLAG_ENV = "KAGE_SLACK_UX"

#: The platform name kanban subscriptions carry for Slack.
PLATFORM = "slack"

#: Cards subscribed to one Slack thread that have not reached a final status.
#: ``blocked`` counts as open: it waits on the user and will run on.
OPEN_CARDS_SQL = (
    "SELECT s.task_id FROM kanban_notify_subs s JOIN tasks t ON t.id = s.task_id "
    "WHERE lower(s.platform) = ? AND s.chat_id = ? AND COALESCE(s.thread_id, '') = ? "
    "AND t.status NOT IN ('done', 'archived')"
)

#: ``ProcessingOutcome`` values, compared by value so this module needs no
#: gateway import. ``cancelled`` is absent: an interrupted turn settles nothing.
OUTCOME_SETTLES = {"success": "done", "failure": "failed"}

#: Bounds on the in-process maps, oldest evicted first. A turn's start snapshot
#: is dropped at its completion; a deferred ask at its final settle. The caps
#: only matter for events that never complete.
STARTED_MAX = 512
DEFERRED_MAX = 512

_started: OrderedDict[Any, frozenset | None] = OrderedDict()
_deferred: OrderedDict[tuple, list] = OrderedDict()
_warned_missing = False


def enabled() -> bool:
    """Whether ``KAGE_SLACK_UX`` is on and the presenter is importable."""
    global _warned_missing
    if _presenter is not None:
        return _presenter.enabled()
    if os.environ.get(FLAG_ENV, "").strip() and not _warned_missing:
        _warned_missing = True
        logger.warning(
            "slack_ux_reactions: %s is set but slack_presenter is not importable; "
            "treating the flag as off", FLAG_ENV,
        )
    return False


def _remember(store: OrderedDict, key: Any, value: Any, cap: int) -> None:
    store[key] = value
    store.move_to_end(key)
    while len(store) > cap:
        store.popitem(last=False)


def _query_open_cards(chat_id: str, thread_id: str, board: str | None) -> frozenset:
    from hermes_cli import kanban_db_connect

    conn = kanban_db_connect.connect(board=board)
    try:
        rows = conn.execute(OPEN_CARDS_SQL, (PLATFORM, chat_id, thread_id)).fetchall()
    finally:
        conn.close()
    return frozenset(row[0] for row in rows)


async def open_cards(chat_id: str, thread_id: str, board: str | None = None) -> frozenset | None:
    """Ids of the open cards subscribed to this thread, or None when the board cannot be read."""
    try:
        return await asyncio.to_thread(_query_open_cards, chat_id, thread_id, board)
    except Exception as exc:  # noqa: BLE001 — a cosmetic read never fails a turn
        logger.debug("slack_ux_reactions: kanban read failed for %s/%s: %s", chat_id, thread_id, exc)
        return None


def _where(event: Any) -> tuple[str | None, str]:
    source = getattr(event, "source", None)
    return getattr(source, "chat_id", None), str(getattr(source, "thread_id", "") or "")


async def on_processing_start(adapter: Any, event: Any) -> None:
    """Add the arrival reaction for this ask's kind, and note the thread's open cards."""
    target = adapter._reacting_target(event)
    if target is None:
        return
    ts, team_id, marker = target
    chat_id, thread_id = _where(event)
    if not chat_id:
        return
    emoji = _presenter.arrival_reaction(getattr(event, "text", ""))
    await adapter._react(chat_id, ts, emoji, team_id, remove=False)
    # After the reaction, so the read never delays it; the model has not
    # created a card yet, since the turn has not reached its first tool call.
    _remember(_started, marker, await open_cards(chat_id, thread_id), STARTED_MAX)


async def on_processing_complete(adapter: Any, event: Any, outcome: Any) -> None:
    """Settle the ask now, or defer to the cards this turn put on the board. Never removes."""
    target = adapter._reacting_target(event)
    if target is None:
        return
    ts, team_id, marker = target
    adapter._reacting_message_ids.discard(marker)
    before = _started.pop(marker, None)
    chat_id, thread_id = _where(event)
    settle = OUTCOME_SETTLES.get(str(getattr(outcome, "value", outcome)))
    if not chat_id or settle is None:
        return
    after = await open_cards(chat_id, thread_id)
    if before is not None and after is not None and after - before:
        asks = _deferred.get((chat_id, thread_id), [])
        _remember(_deferred, (chat_id, thread_id), [*asks, (ts, team_id)], DEFERRED_MAX)
        return
    await adapter._react(chat_id, ts, _presenter.settle_reaction(settle), team_id, remove=False)


async def settle_delegated(adapter: Any, sub: dict, kind: str, board: str | None = None) -> None:
    """Settle the asks waiting on this thread's cards, after a notifier terminal event.

    ⏸️ goes on the newest waiting ask as soon as any card blocks. ✅ or ❌ goes
    on every waiting ask once no other open card is subscribed to the thread,
    and the asks are forgotten. A thread with no waiting ask is left alone.
    """
    if not enabled() or (sub.get("platform") or "").lower() != PLATFORM:
        return
    settle = _presenter.settle_for_kanban_kind(kind)
    if settle is None:
        return
    key = (sub.get("chat_id"), str(sub.get("thread_id") or ""))
    asks = _deferred.get(key)
    if not asks or not hasattr(adapter, "_react"):
        return
    emoji = _presenter.settle_reaction(settle)
    if settle in _presenter.PROVISIONAL_SETTLES:
        ts, team_id = asks[-1]
        await adapter._react(key[0], ts, emoji, team_id, remove=False)
        return
    others = await open_cards(key[0], key[1], board)
    if others is None or others - {sub.get("task_id")}:
        return
    _deferred.pop(key, None)
    for ts, team_id in asks:
        await adapter._react(key[0], ts, emoji, team_id, remove=False)
