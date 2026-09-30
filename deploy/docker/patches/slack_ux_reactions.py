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
  its settle to those cards, and only those: a card already open when the ask
  arrived is not its to wait on. A turn that failed after opening them still
  settles ❌ when they finish, whatever they did. The kanban notifier calls
  :func:`settle_delegated` on each terminal event: ⏸️ as soon as one of the
  ask's cards blocks on the user, and once every one of them has finished, ✅,
  or ❌ if any gave up. A fan-out settles once, when all of it has. Cards are
  read from every live board, as the notifier reads them, and known by board
  and id.

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
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:
    import slack_presenter as _presenter
except ImportError:  # the scripts directory is not on PYTHONPATH
    _presenter = None

#: The flag, read here only to word the warning when the presenter is missing.
FLAG_ENV = "KAGE_SLACK_UX"

#: ``slack_presenter.FLAG_ON_VALUES``, copied because the warning below fires
#: exactly when that module cannot be imported.
FLAG_ON_VALUES = frozenset({"1", "true", "yes", "on"})

#: The platform name kanban subscriptions carry for Slack.
PLATFORM = "slack"

#: Hermes's ``kanban_db.DEFAULT_BOARD``: the board a terminal event is on when
#: the notifier names none.
DEFAULT_BOARD = "default"

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
#: only matter for events that never complete. ``FINISHED_MAX`` bounds the
#: cards remembered as finished, which only needs to outlast one turn's
#: completion.
STARTED_MAX = 512
DEFERRED_MAX = 512
FINISHED_MAX = 512
#: Asks one thread can have waiting at once, oldest dropped first: a thread
#: whose cards never finish would otherwise grow its list on every ask.
DEFERRED_PER_THREAD = 32

#: The notifier's dedup key for a board whose database path does not resolve.
UNRESOLVED_PREFIX = "slug:"


class _Ask:
    """A Slack ask whose settle waits on the cards its turn opened, as ``(board, id)``.

    A plain class, not a dataclass: the build-time verify loads this module by
    path without registering it in ``sys.modules``, which ``@dataclass`` needs.
    """

    __slots__ = ("cards", "failed", "team_id", "ts")

    def __init__(self, ts: str, team_id: Any, cards: set, failed: bool = False) -> None:
        self.ts = ts
        self.team_id = team_id
        self.cards = cards
        self.failed = failed


_started: OrderedDict[Any, frozenset | None] = OrderedDict()
_deferred: OrderedDict[tuple, list[_Ask]] = OrderedDict()
#: Cards the notifier has reported finished, as ``(board, id)``, and whether each
#: failed. A card can finish between the turn's board read and the moment its
#: ask is deferred; the turn checks here so it never waits on a card that has
#: already settled.
_finished: OrderedDict[tuple[str, str], bool] = OrderedDict()
_warned_missing = False


def enabled() -> bool:
    """Whether ``KAGE_SLACK_UX`` is on and the presenter is importable."""
    global _warned_missing
    if _presenter is not None:
        return _presenter.enabled()
    if os.environ.get(FLAG_ENV, "").strip().lower() in FLAG_ON_VALUES and not _warned_missing:
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


def _live_boards(kb: Any) -> list[dict]:
    """Every live board, listed as the kanban notifier lists them."""
    try:
        return kb.list_boards(include_archived=False)
    except Exception:  # noqa: BLE001 — the notifier falls back to the default board too
        return [kb.read_board_metadata(kb.DEFAULT_BOARD)]


def _query_open_cards(chat_id: str, thread_id: str) -> frozenset:
    """``(board, id)`` of every open card subscribed to this thread, on every live board.

    Boards are walked as the notifier walks them: one read per database, under
    the slug the notifier stamps on that database's deliveries. So a card read
    here and the card a terminal event names compare equal. A board with no
    database yet is skipped rather than created, and a board that cannot be
    read is skipped with the rest still read, as the notifier skips it.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect

    cards: set[tuple[str, str]] = set()
    seen: set[str] = set()
    for meta in _live_boards(kb):
        slug = meta.get("slug") or kb.DEFAULT_BOARD
        path = meta.get("db_path")
        try:
            resolved = str(Path(path).expanduser().resolve()) if path else str(kb.kanban_db_path(slug).resolve())
        except Exception:  # noqa: BLE001 — as the notifier: key an unresolvable board by slug
            resolved = f"{UNRESOLVED_PREFIX}{slug}"
        if resolved in seen:
            continue
        seen.add(resolved)
        if not resolved.startswith(UNRESOLVED_PREFIX) and not Path(resolved).exists():
            continue
        try:
            conn = kanban_db_connect.connect(board=slug)
            try:
                rows = conn.execute(OPEN_CARDS_SQL, (PLATFORM, chat_id, thread_id)).fetchall()
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 — one bad board does not blank the others
            logger.debug("slack_ux_reactions: kanban board %s unreadable: %s", slug, exc)
            continue
        cards.update((slug, row[0]) for row in rows)
    return frozenset(cards)


async def open_cards(chat_id: str, thread_id: str) -> frozenset | None:
    """``(board, id)`` of the open cards subscribed to this thread, or None when a board cannot be read."""
    try:
        return await asyncio.to_thread(_query_open_cards, chat_id, thread_id)
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
        new = after - before
        waiting = set(new) - set(_finished)
        # The turn's own failure carries into the deferred settle: cards that
        # later complete do not undo an ask whose turn raised.
        failed = settle == _presenter.SETTLE_FAILED or any(_finished.get(card, False) for card in new)
        if waiting:
            asks = [*_deferred.get((chat_id, thread_id), []), _Ask(ts, team_id, waiting, failed)]
            if len(asks) > DEFERRED_PER_THREAD:
                logger.debug(
                    "slack_ux_reactions: %s/%s has %d asks waiting; dropping the oldest, which will not settle",
                    chat_id, thread_id, len(asks) - DEFERRED_PER_THREAD,
                )
            _remember(_deferred, (chat_id, thread_id), asks[-DEFERRED_PER_THREAD:], DEFERRED_MAX)
            return
        if failed:
            settle = _presenter.SETTLE_FAILED
    await adapter._react(chat_id, ts, _presenter.settle_reaction(settle), team_id, remove=False)


async def settle_delegated(adapter: Any, sub: dict, kind: str, board: str | None = None) -> None:
    """Settle the asks waiting on this card, after a notifier terminal event.

    ⏸️ goes on each ask waiting on the card as soon as it blocks. A final event
    takes the card off each ask's set; an ask whose set empties gets ✅, or ❌
    if any of its cards gave up, and is forgotten. A card no ask is waiting on
    is left alone. ``board`` is the notifier's slug for the card's board, which
    with the card's id is how an ask knows it; no board is read here.
    """
    if not enabled() or (sub.get("platform") or "").lower() != PLATFORM:
        return
    settle = _presenter.settle_for_kanban_kind(kind)
    if settle is None or not sub.get("task_id"):
        return
    card = (board or DEFAULT_BOARD, sub["task_id"])
    key = (sub.get("chat_id"), str(sub.get("thread_id") or ""))
    provisional = settle in _presenter.PROVISIONAL_SETTLES
    if not provisional:
        _remember(_finished, card, settle == _presenter.SETTLE_FAILED, FINISHED_MAX)
    asks = [ask for ask in _deferred.get(key, []) if card in ask.cards]
    if not asks or not hasattr(adapter, "_react"):
        return
    if provisional:
        for ask in asks:
            await adapter._react(key[0], ask.ts, _presenter.settle_reaction(settle), ask.team_id, remove=False)
        return
    settled = []
    for ask in asks:
        ask.cards.discard(card)
        ask.failed = ask.failed or settle == _presenter.SETTLE_FAILED
        if not ask.cards:
            settled.append(ask)
    if settled:
        remaining = [ask for ask in _deferred.get(key, []) if ask not in settled]
        if remaining:
            _deferred[key] = remaining
        else:
            _deferred.pop(key, None)
    for ask in settled:
        outcome = _presenter.SETTLE_FAILED if ask.failed else _presenter.SETTLE_DONE
        await adapter._react(key[0], ask.ts, _presenter.settle_reaction(outcome), ask.team_id, remove=False)
