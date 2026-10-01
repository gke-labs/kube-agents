"""React to each Slack ask by kind, and again when the work settles.

Installed into the image at ``/opt/hermes/gateway/slack_ux_reactions.py``.
``apply_slack_ux_reactions.py`` makes the Slack adapter's two reaction hooks
(``on_processing_start``, ``on_processing_complete``) hand over to this module
when ``KAGE_SLACK_UX`` is on, and ``kanban_progress_lines.py`` (installed by
``apply_kanban_progress_lines.py``) calls :func:`settle_delegated` after the
kanban notifier delivers a terminal event.
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
  arrived is not its to wait on, even one unblocked while the turn ran.
  Hermes records no actor on an unblock, so it may be the CLI's or another
  turn's; that card's own ask carries its outcome. Nor is a card created
  under one it did not open, a worker's follow-up or one filed beneath that:
  Hermes copies the creator's subscriptions onto it, so it is in the thread
  without being the turn's. A turn that failed after opening them still
  settles ❌ when they finish, whatever they did. The kanban notifier calls
  :func:`settle_delegated` on each terminal event: ⏸️ as soon as one of the
  ask's cards blocks on the user, and once every one of them has finished, ✅,
  or ❌ if any gave up. A completion extends its ask to the open cards created
  under it, directly or through follow-ups already completed (see
  :func:`settle_delegated`). A fan-out settles once, when all of it has. Only a
  finish reported while the turn runs, or after it, counts for its ask. Cards
  are read from every live board, as the notifier reads them, and known by
  board and id.

The deferred asks live in this process only (the notifier runs in the gateway
process too). A gateway restart between the turn and the settle loses the
settle, and the ask keeps its arrival reaction alone; nothing is ever put on a
message this process did not see arrive.

Fail-soft throughout: a kanban read that fails, on any board, settles the ask
at once as a direct answer, and a reaction that fails is logged at debug and
the turn carries on, as upstream's ``_react`` already does.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import OrderedDict
from pathlib import Path
from typing import Any, NamedTuple

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

#: Cards subscribed to one Slack thread that have not reached a final status,
#: with that status and the card whose worker created it, if one did: Hermes
#: copies the creator's subscriptions onto the new card, and whether it sits
#: parked by a give-up: still ``blocked``, with a ``gave_up`` as its latest stop.
#: An unblock, or any move off ``blocked``, revives it, so either clears that.
OPEN_CARDS_SQL = (
    "SELECT s.task_id, t.status, "
    "(SELECT json_extract(e.payload, '$.creator_task_id') FROM task_events e "
    "WHERE e.task_id = s.task_id AND e.kind = 'created'), "
    "t.status = 'blocked' AND (SELECT e.kind FROM task_events e WHERE e.task_id = s.task_id "
    "AND e.kind IN ('blocked', 'unblocked', 'gave_up') ORDER BY e.id DESC LIMIT 1) = 'gave_up' "
    "FROM kanban_notify_subs s JOIN tasks t ON t.id = s.task_id "
    "WHERE lower(s.platform) = ? AND s.chat_id = ? AND COALESCE(s.thread_id, '') = ? "
    "AND t.status NOT IN ('done', 'archived')"
)

#: Every card subscribed to one Slack thread, closed ones included, with the
#: card whose worker created it. Hermes keeps a subscription until the card is
#: archived, so a follow-up's creator that has since completed is still here.
THREAD_LINEAGE_SQL = (
    "SELECT s.task_id, "
    "(SELECT json_extract(e.payload, '$.creator_task_id') FROM task_events e "
    "WHERE e.task_id = s.task_id AND e.kind = 'created') "
    "FROM kanban_notify_subs s "
    "WHERE lower(s.platform) = ? AND s.chat_id = ? AND COALESCE(s.thread_id, '') = ?"
)

#: How many creator links a completion follows down to the open cards it holds
#: the ask for. A worker's follow-up is one link; one filed under that, two. A
#: card further down is not held, so the ask can settle while it runs; that
#: needs this many cards above it already completed.
LINEAGE_DEPTH = 8

#: ``ProcessingOutcome`` values, compared by value so this module needs no
#: gateway import. ``cancelled`` is absent: an interrupted turn settles nothing.
OUTCOME_SETTLES = {"success": "done", "failure": "failed"}

#: Bounds on the in-process maps, oldest evicted first. A turn in flight is
#: dropped at its completion; a deferred ask at its final settle. The caps only
#: matter for events that never complete.
STARTED_MAX = 512
DEFERRED_MAX = 512
#: Asks one thread can have waiting at once, oldest dropped first: a thread
#: whose cards never finish would otherwise grow its list on every ask.
DEFERRED_PER_THREAD = 32

#: The notifier's dedup key for a board whose database path does not resolve.
UNRESOLVED_PREFIX = "slug:"

#: Statuses a card runs from, or waits to be picked up in. A card that paused
#: during a turn but sits in one of these at its end was resumed within it.
RESUMED_STATUSES = frozenset({"todo", "ready", "scheduled", "running"})


class _Card(NamedTuple):
    """An open card as one board read saw it."""

    status: str
    #: The id of the card on its board whose worker created it, or None when
    #: a chat turn or the CLI did.
    creator: str | None = None
    #: Whether its latest stop was a give-up rather than a block on the user:
    #: Hermes parks a card that gave up at ``blocked`` too, waiting for a human.
    gave_up: bool = False


class _Turn:
    """A turn in flight: its thread, the thread's open cards when it started,
    the cards that finished while it ran, each with whether it failed, and the
    cards that paused while it ran.

    Only events seen during the turn count, so nothing a card did before this
    turn, or after an earlier one, decides this ask. A pause is kept because
    its ask is not deferred yet, so nothing else would put ⏸️ on it.
    """

    __slots__ = ("before", "finished", "key", "paused")

    def __init__(self, key: tuple, before: dict | None) -> None:
        self.key = key
        self.before = before
        self.finished: dict[tuple[str, str], bool] = {}
        self.paused: set[tuple[str, str]] = set()


class _Ask:
    """A Slack ask whose settle waits on the cards its turn opened, as ``(board, id)``."""

    __slots__ = ("cards", "failed", "team_id", "ts")

    def __init__(self, ts: str, team_id: Any, cards: set, failed: bool = False) -> None:
        self.ts = ts
        self.team_id = team_id
        self.cards = cards
        self.failed = failed


_started: OrderedDict[Any, _Turn] = OrderedDict()
_deferred: OrderedDict[tuple, list[_Ask]] = OrderedDict()
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


def _read_boards(sql: str, chat_id: str, thread_id: str) -> list:
    """``sql`` run against every live board for this thread, as ``[(board, row)]``.

    Boards are walked as the notifier walks them: one read per database, under
    the slug the notifier stamps on that database's deliveries. So a card read
    here and the card a terminal event names compare equal. A board with no
    database yet holds no cards and is skipped rather than created.

    Any other failure raises, where the notifier would skip the board: a turn
    compares two reads, and a board missing from one of them would make its
    cards look opened or finished by the turn. A failed read settles the ask
    at once instead.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect

    rows: list[tuple[str, Any]] = []
    seen: set[str] = set()
    for meta in kb.list_boards(include_archived=False):
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
        conn = kanban_db_connect.connect(board=slug)
        try:
            rows.extend((slug, row) for row in conn.execute(sql, (PLATFORM, chat_id, thread_id)).fetchall())
        finally:
            conn.close()
    return rows


def _query_open_cards(chat_id: str, thread_id: str) -> dict:
    """Every open card subscribed to this thread, on every live board, as ``{(board, id): _Card}``."""
    return {
        (slug, row[0]): _Card(row[1], row[2], bool(row[3]))
        for slug, row in _read_boards(OPEN_CARDS_SQL, chat_id, thread_id)
    }


def _query_thread_lineage(chat_id: str, thread_id: str) -> dict:
    """Every card subscribed to this thread, closed ones included, as ``{(board, id): creator id}``."""
    return {(slug, row[0]): row[1] for slug, row in _read_boards(THREAD_LINEAGE_SQL, chat_id, thread_id)}


async def open_cards(chat_id: str, thread_id: str) -> dict | None:
    """The thread's open cards as ``{(board, id): _Card}``, or None when the boards cannot be read."""
    try:
        return await asyncio.to_thread(_query_open_cards, chat_id, thread_id)
    except Exception as exc:  # noqa: BLE001 — a cosmetic read never fails a turn
        logger.debug("slack_ux_reactions: kanban read failed for %s/%s: %s", chat_id, thread_id, exc)
        return None


async def thread_lineage(chat_id: str, thread_id: str) -> dict:
    """The thread's cards as ``{(board, id): creator id}``, or ``{}`` when the boards cannot be read."""
    try:
        return await asyncio.to_thread(_query_thread_lineage, chat_id, thread_id)
    except Exception as exc:  # noqa: BLE001 — a cosmetic read never fails a turn
        logger.debug("slack_ux_reactions: lineage read failed for %s/%s: %s", chat_id, thread_id, exc)
        return {}


def _descendants(card: tuple, creators: dict, still_open: frozenset = frozenset()) -> set:
    """The cards created under ``card``, through workers' follow-ups, up to ``LINEAGE_DEPTH`` links down.

    The walk goes on only through cards that have completed. It stops at a card
    in ``still_open``: one running carries its own follow-ups when it completes,
    and one parked by a give-up has none that can start.
    """
    found: set = set()
    frontier = {card}
    for _ in range(LINEAGE_DEPTH):
        parents = frontier - still_open
        frontier = {
            (board, task) for (board, task), creator in creators.items()
            if (board, creator) in parents and (board, task) not in found
        }
        if not frontier:
            break
        found |= frontier
    return found


def _where(event: Any) -> tuple[str | None, str]:
    source = getattr(event, "source", None)
    return getattr(source, "chat_id", None), str(getattr(source, "thread_id", "") or "")


def _own_cards(before: dict, after: dict, finished: dict) -> set:
    """The cards this turn answers for: open at its end, or finished during it, and not open at its start.

    A card open at the start is never the turn's, even one unblocked while it
    ran: Hermes's ``unblocked`` event names no actor, so the turn cannot show
    the unblock was its own rather than the CLI's or another turn's. Nor is a
    new card whose creator was open at the start, such as a follow-up an
    earlier ask's worker files, nor one created under such a card. Only workers
    set a creator; the parents a turn names do not make a card anyone else's. A
    creator open at neither read opened within the turn, so it and the cards it
    created are the turn's, even when it closed before the notifier reported it.
    """
    opened = {card for card in (*after, *finished) if card not in before}
    foreign = set(before)
    while True:
        more = {
            (board, task) for board, task in opened - foreign
            if (board, task) in after and (board, after[(board, task)].creator) in foreign
        }
        if not more:
            return opened - foreign
        foreign |= more


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
    # Events are recorded from here on. One can lag the change it reports, so
    # _own_cards decides from the board what the turn did, not from arrival.
    _remember(_started, marker, _Turn((chat_id, thread_id), await open_cards(chat_id, thread_id)), STARTED_MAX)


async def on_processing_complete(adapter: Any, event: Any, outcome: Any) -> None:
    """Settle the ask now, or defer to the cards this turn put on the board. Never removes."""
    target = adapter._reacting_target(event)
    if target is None:
        return
    ts, team_id, marker = target
    adapter._reacting_message_ids.discard(marker)
    chat_id, thread_id = _where(event)
    settle = OUTCOME_SETTLES.get(str(getattr(outcome, "value", outcome)))
    if not chat_id or settle is None:
        _started.pop(marker, None)
        return
    after = await open_cards(chat_id, thread_id)
    # Still in flight across the read, so a card finishing during it is seen;
    # nothing below awaits before the ask is deferred.
    turn = _started.pop(marker, None)
    before = turn.before if turn is not None else None
    new = _own_cards(before, after, turn.finished) if before is not None and after is not None else set()
    if new:
        waiting = new - set(turn.finished)
        # The turn's own failure carries into the deferred settle: cards that
        # later complete do not undo an ask whose turn raised.
        failed = settle == _presenter.SETTLE_FAILED or any(turn.finished.get(card, False) for card in new)
        if waiting:
            asks = [*_deferred.get((chat_id, thread_id), []), _Ask(ts, team_id, waiting, failed)]
            if len(asks) > DEFERRED_PER_THREAD:
                logger.debug(
                    "slack_ux_reactions: %s/%s has %d asks waiting; dropping the oldest, which will not settle",
                    chat_id, thread_id, len(asks) - DEFERRED_PER_THREAD,
                )
            _remember(_deferred, (chat_id, thread_id), asks[-DEFERRED_PER_THREAD:], DEFERRED_MAX)
            # A pause the turn also saw resumed would put ⏸️ on nothing that waits.
            paused = {card for card in waiting & turn.paused if card in after}
            if any(after[card].status not in RESUMED_STATUSES for card in paused):
                blocked = _presenter.settle_reaction(_presenter.SETTLE_BLOCKED)
                await adapter._react(chat_id, ts, blocked, team_id, remove=False)
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
    with the card's id is how an ask knows it.

    Before a completion takes a card off, the thread is read once for open
    cards its worker created, and each ask waiting on the card waits on those
    too: a follow-up filed with ``parents=[own id]`` starts only once the card
    completes, so it was not on the board when the turn ended. Cards created
    under those count as well, through creators that have already completed,
    which a second read of the thread's closed cards supplies; if it fails,
    only the card's own open follow-ups are seen. A card created under a
    follow-up still open waits for that follow-up's completion, as it would
    have had the follow-up been on the board at the turn's end. One
    blocked on the user is kept and puts ⏸️ on the ask. One parked by a give-up
    is not, nor is anything created under it, and it leaves the ask ✅ for the
    work it did: being subscribed to this thread, the follow-up has posted its
    own "gave up" line in it, which is the failure. A
    give-up holds on nothing more: the card is parked, so a follow-up it gates
    cannot start, and the ask is ❌ whatever the rest do. A failed read settles
    as though there were none. A card filed under this one after its
    completion is delivered is not seen: the ask has settled by then.
    """
    if not enabled() or (sub.get("platform") or "").lower() != PLATFORM:
        return
    settle = _presenter.settle_for_kanban_kind(kind)
    if settle is None or not sub.get("task_id"):
        return
    card = (board or DEFAULT_BOARD, sub["task_id"])
    key = (sub.get("chat_id"), str(sub.get("thread_id") or ""))
    provisional = settle in _presenter.PROVISIONAL_SETTLES
    for turn in _started.values():
        if turn.key != key:
            continue
        if provisional:
            turn.paused.add(card)
        else:
            turn.finished[card] = settle == _presenter.SETTLE_FAILED
    asks = [ask for ask in _deferred.get(key, []) if card in ask.cards]
    if not asks or not hasattr(adapter, "_react"):
        return
    if provisional:
        for ask in asks:
            await adapter._react(key[0], ask.ts, _presenter.settle_reaction(settle), ask.team_id, remove=False)
        return
    follow_ups = {}
    if settle == _presenter.SETTLE_DONE:
        still_open = await open_cards(*key)
        if still_open:
            creators = {**await thread_lineage(*key), **{c: seen.creator for c, seen in still_open.items()}}
            under = _descendants(card, creators, frozenset(still_open) - {card})
            follow_ups = {c: seen for c, seen in still_open.items() if c in under and not seen.gave_up}
    waits_on_user = any(seen.status not in RESUMED_STATUSES for seen in follow_ups.values())
    # The read awaited, so another event may have settled an ask meanwhile.
    asks = [ask for ask in _deferred.get(key, []) if card in ask.cards]
    settled = []
    for ask in asks:
        ask.cards |= follow_ups.keys()
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
    if waits_on_user:
        blocked = _presenter.settle_reaction(_presenter.SETTLE_BLOCKED)
        for ask in asks:
            await adapter._react(key[0], ask.ts, blocked, ask.team_id, remove=False)
    for ask in settled:
        outcome = _presenter.SETTLE_FAILED if ask.failed else _presenter.SETTLE_DONE
        await adapter._react(key[0], ask.ts, _presenter.settle_reaction(outcome), ask.team_id, remove=False)
