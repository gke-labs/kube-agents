"""User cards ahead of background triage on the kanban dispatcher.

Installed into the image at ``/opt/hermes/hermes_cli/kanban_priority.py``.
Wired into the dispatcher (``hermes_cli/kanban_db_dispatch.py``) by edits 7-10
of ``deploy/docker/patches/apply_kanban_scheduling.py``, and into the create
tool, the dispatcher watcher and the notifier (``tools/kanban_tools.py``,
``gateway/kanban_watchers.py``, ``gateway/kanban_watchers_notifier.py``) by
``deploy/docker/patches/apply_kanban_priority.py``.

Why
---
The gateway runs every kanban card through one host-wide cap,
``kanban.max_in_progress``. Event triage files a card per alert, so a burst of
alerts filled every slot, and a user's question filed a moment later waited
behind triage it had nothing to do with: p50 4.4 minutes, p90 18, and up to the
30-minute stale timeout. Upstream already sorts ready cards
``priority DESC, created_at ASC``, but nothing in this install ever set a
priority, so dispatch was plain FIFO. The gateway's health check made it worse:
a full cap returns from the tick before any ready row is read and records
nothing, so a saturated dispatcher logged "kanban dispatcher stuck ... check
profile health", which sends the reader to the wrong place.

What
----
1. **Classification at create time** (:func:`stamp_priority`). A card filed
   from a background session (``k8s-evt-`` event triage, ``cron-`` report
   relays; :data:`BACKGROUND_SESSION_PREFIXES`) is background. Every other
   creator is a user: Slack, Google Chat, the inject and A2A doors, a card with
   no session at all. User cards get at least :data:`USER_PRIORITY`; background
   cards are clamped below it, so a model cannot promote triage by passing
   ``priority``. A dispatcher worker's child inherits its parent's priority, so
   a user card's fan-out stays user-class and a triage card's stays
   background. The class is read only from trusted context (the turn's own
   session as the runtime bound it, or the priority stamped on the worker's
   own card), never from a ``session_id``: not the one a model may pass to
   the tool, and not the one stored on the worker's card, which upstream
   copies from that same argument. A worker whose own card cannot be read
   files background. On the ``kanban_create`` path the classification fails
   toward the user by design: a background producer with a prefix this list
   does not know about is stamped user-class. That card then counts against
   the user class and can take a user's slot, and a person's card filed after
   it sorts behind it at equal priority, so the cost is a user slot, not only
   triage speed. A new background producer adds its prefix here.
   Cards that never pass through
   ``kanban_create`` are not stamped and keep upstream's priority 0, which is
   background: ``hermes kanban create`` without ``--priority 100``, the
   dashboard, and the children auto-decompose inserts for a ``triage: true``
   card (upstream's ``_insert_decomposed_child`` writes no priority, so a
   user card filed with ``triage: true`` runs its decomposed graph as
   background).
2. **One slot guaranteed to each class** (:class:`ReservedSlot`). At a cap of
   2 or more, each class may hold at most ``max_in_progress - 1`` slots
   host-wide: background cannot take the user's last slot, and user cards
   (including a door's fan-out) cannot take triage's last slot, so alerts are
   never silenced. The slots between go to whoever is first, and user cards
   sort first. The ready loop skips a held row into
   ``DispatchResult.skipped_reserved``. Waiting coordinators are discounted
   the same way ``count_running_tasks`` discounts them
   (``kanban_scheduling`` part 4). At a cap of 1 nothing is held; user cards
   still sort first.
3. **An honest warning** (:func:`saturation_tick`). The dispatcher records
   what filled the slots on ``DispatchResult.saturation`` and the watcher logs
   ``kanban dispatcher saturated: ...`` instead of "stuck" when every board is
   capped or holding its reserved slot. "stuck" stays for budget available and
   nothing spawned.
4. **A queued notice** (:func:`note_waiting`, :func:`format_queued`). A user
   card left waiting for a slot gets one ``queued`` task event per wait. The
   notifier delivers it into the card's thread as the first line of the card's
   rolling progress message, with no LLM turn. ``kanban_create`` also says the
   card is queued when no slot is free (:func:`queue_fields`).

Nothing here may break a dispatch tick or a ``kanban_create``: every entry
point fails open to upstream's behaviour and logs why.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

#: The priority a user-initiated card is stamped with, at least. Upstream's
#: column defaults to 0 and nothing else in this install writes it, so any
#: card at or above this is user-class and anything below is background.
USER_PRIORITY = 100

#: Session-id prefixes of the background producers, as
#: ``agents/platform/scripts/session_kv_server.py`` mints them:
#: ``create_session`` (``k8s-evt-<hex>``, event triage, drift and stall) and
#: ``_cron_report_session_id`` (``cron-<profile>-<job>-<day>``, report relays).
#: ``test_kanban_priority`` pins both against that file, so a renamed prefix
#: fails CI rather than silently turning triage into user work.
BACKGROUND_SESSION_PREFIXES = ("k8s-evt-", "cron-")


def is_background_session(session_id: object) -> bool:
    """Whether a card's session id names a background producer."""
    return isinstance(session_id, str) and session_id.startswith(
        BACKGROUND_SESSION_PREFIXES
    )


def is_user_priority(priority: object) -> bool:
    """Whether a card's priority puts it in the user class."""
    try:
        return int(priority or 0) >= USER_PRIORITY
    except (TypeError, ValueError):
        return False


_UNREAD = object()


def trusted_origin_session() -> str:
    """The creating turn's own session as the runtime bound it, or ``""``.

    Upstream's ``tools.async_delegation._current_origin_session_id``: the
    request-scoped ``HERMES_SESSION_CHAT_ID`` of an ``api_server`` turn, which
    is where event triage (``k8s-evt-...``) and cron relays (``cron-...``) run.
    It reads the gateway's ContextVars, not anything the model passed.
    """
    try:
        from tools.async_delegation import _current_origin_session_id

        return _current_origin_session_id() or ""
    except Exception:  # noqa: BLE001 — no session context is not an error
        return ""


def stamp_priority(
    requested: object, parent: Any = None, worker_task_id: object = None,
    origin_session: object = _UNREAD,
) -> int:
    """The priority ``kanban_create`` writes for a new card.

    ``requested`` is what the caller asked for (upstream's
    ``_opt_int(args.get("priority"), 0)``) and ``parent`` the dispatcher-owned
    card creating it, if any (the handler's ``self_task``), and
    ``worker_task_id`` the card id a dispatcher worker runs (the handler's
    ``self_tid``). ``origin_session`` defaults to :func:`trusted_origin_session`;
    tests pass it.

    The class comes only from trusted context: the creating turn's own session
    as the runtime bound it, and for a dispatcher worker the priority stamped
    on its own card. The ``session_id`` argument a model may pass to
    ``kanban_create`` is ignored here, because a triage worker could otherwise
    name a user-looking session and take the user slot. So is the parent's
    stored ``session_id``: upstream writes ``args["session_id"]`` there, so a
    person's card filed with a triage-looking argument would otherwise fan out
    background children. The parent's priority is the trusted record of its
    class, written by this function from the parent's own origin (or by an
    operator on the dashboard). The card is background when the origin session
    has a background prefix or the parent is below :data:`USER_PRIORITY`. A dispatcher
    worker whose own card cannot be read (it named another ``board``, or the
    card is gone) fails closed to background: a worker never files a user card
    on the strength of a card it cannot show.

    * A child starts from ``max(requested, parent.priority)``. Upstream does not
      inherit priority at all, so without this a user card's fan-out would
      drop back to background.
    * A background card is clamped below :data:`USER_PRIORITY`, so a model
      passing ``priority`` cannot promote triage. An operator can still promote
      a card from the dashboard, which edits the row directly.
    * Every other card is raised to at least :data:`USER_PRIORITY`.

    Never raises: a value it cannot read keeps upstream's.
    """
    try:
        base = int(requested or 0)
    except (TypeError, ValueError):
        return requested if isinstance(requested, int) else 0
    try:
        if origin_session is _UNREAD:
            origin_session = trusted_origin_session()
        background = is_background_session(origin_session)
        if worker_task_id and parent is None:
            background = True
        if parent is not None:
            parent_priority = getattr(parent, "priority", None)
            if parent_priority is not None:
                base = max(base, int(parent_priority))
                if not is_user_priority(parent_priority):
                    background = True
        if background:
            return min(base, USER_PRIORITY - 1)
        return max(base, USER_PRIORITY)
    except Exception as exc:  # noqa: BLE001 — never fail kanban_create
        logger.warning("kanban priority: stamping fell back to %r: %r", base, exc)
        # A worker's card is never promoted on a failed read.
        if worker_task_id:
            return min(base, USER_PRIORITY - 1)
        return base


# ---------------------------------------------------------------------------
# Slot accounting (dispatcher side)
# ---------------------------------------------------------------------------

#: Event kind written for a user card left waiting for a worker slot.
QUEUED_KIND = "queued"

#: The line the user sees in the card's thread, carried on the dispatcher's
#: ``queued`` event. ``kanban_create`` returns counts only, no text for the
#: model to relay. Worded by bnaylor: no counts in the user-facing line; the
#: counts go to the gateway log's saturation warning.
QUEUED_TEXT = "Queued: the system is busy. Your request will start when a worker frees up."
QUEUED_MARKER = "⏳"

#: How many running cards the saturation warning names.
CARDS_SHOWN = 5

#: How often the saturation warning may repeat, matching upstream's stuck
#: warning, which it replaces for this case.
WARN_INTERVAL_SECONDS = 300

#: The units the saturation warning prints a running card's age in.
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600


def _kb():
    from hermes_cli import kanban_db

    return kanban_db


def _kbd():
    from hermes_cli import kanban_db_dispatch

    return kanban_db_dispatch


def _kbc():
    from hermes_cli import kanban_db_connect

    return kanban_db_connect


def _count_waiting(conn, below_priority: int) -> int:
    try:
        from hermes_cli.kanban_scheduling import count_waiting_on_children
    except ImportError:  # unit tests import the patch modules flat
        from kanban_scheduling import count_waiting_on_children
    return count_waiting_on_children(conn, below_priority=below_priority)


def count_running_background(conn) -> int:
    """Running background cards on one board, less those only waiting on children."""
    raw = int(
        conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'running' AND priority < ?",
            (USER_PRIORITY,),
        ).fetchone()[0]
    )
    return max(0, raw - _count_waiting(conn, USER_PRIORITY))


def _other_board_conns(board: Optional[str]) -> Iterable[Any]:
    """Connections to every other board, matched the way upstream's
    ``count_running_tasks_other_boards`` matches them (resolved DB path), so the
    background share is host-wide like the cap it is a share of."""
    kb = _kb()
    try:
        current = str(kb.kanban_db_path(board=board).expanduser().resolve())
    except Exception:
        current = None
    try:
        boards = kb.list_boards(include_archived=False)
    except Exception:
        return
    for meta in boards:
        slug = meta.get("slug") or kb.DEFAULT_BOARD
        try:
            path = kb.kanban_db_path(board=slug).expanduser()
            if current is not None and str(path.resolve()) == current:
                continue
            if not path.exists():
                continue
            other = _kbc().connect(board=slug)
        except Exception:
            continue
        try:
            yield other
        finally:
            try:
                other.close()
            except Exception:
                pass


def count_running_background_host(conn, board: Optional[str]) -> int:
    """Running background cards on every board. Fails open per board."""
    total = count_running_background(conn)
    for other in _other_board_conns(board):
        try:
            total += count_running_background(other)
        except Exception:
            continue
    return total


def count_running_host(conn, board: Optional[str]) -> int:
    """What ``_tick_spawn_budget`` compares with the cap, recounted now."""
    kbd = _kbd()
    return int(kbd.count_running_tasks(conn)) + int(
        kbd.count_running_tasks_other_boards(board)
    )


def class_cap(max_in_progress: Optional[int]) -> Optional[int]:
    """How many slots either class may hold, or None for no limit.

    The same number for both: at a cap of 2 or more, background cards may hold
    every slot but the one guaranteed to user cards, and user cards every slot
    but the one guaranteed to background triage. The slots between go to
    whoever is first, and user cards sort first. A cap of 1 cannot guarantee
    either class a slot without stopping the other, so nothing is held there
    and user cards only sort first. No cap at all needs no floor.
    """
    if max_in_progress is None:
        return None
    try:
        cap = int(max_in_progress)
    except (TypeError, ValueError):
        return None
    return cap - 1 if cap >= 2 else None


#: The background share is the original name for the class cap.
background_cap = class_cap


def _ready_rows(conn) -> list:
    return conn.execute(
        "SELECT id, priority, created_at FROM tasks "
        "WHERE status = 'ready' AND claim_lock IS NULL "
        "ORDER BY priority DESC, created_at ASC"
    ).fetchall()


def _cell(row: Any, key: str, index: int) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return row[index]


class ReservedSlot:
    """The ready loop's view of each class's share for one tick.

    Built after ``spawned = 0``; :meth:`holds_back` is asked for every row the
    loop is about to dispatch, :meth:`took` is told about every spawn, and
    :meth:`finish` runs after the loop. Any failure turns the reservation off
    for the tick rather than stopping dispatch.
    """

    def __init__(self, conn, max_in_progress: Optional[int], board: Optional[str]) -> None:
        self.conn = conn
        self.board = board
        self.max_in_progress = max_in_progress
        self.cap = class_cap(max_in_progress)
        self.background_running = 0
        self.user_running = 0
        self.priorities: dict = {}
        if self.cap is None:
            return
        try:
            self.background_running = count_running_background_host(conn, board)
            self.user_running = max(
                0, count_running_host(conn, board) - self.background_running
            )
            self.priorities = {
                _cell(row, "id", 0): _cell(row, "priority", 1)
                for row in _ready_rows(conn)
            }
        except Exception as exc:  # noqa: BLE001 — never break the dispatch tick
            logger.warning(
                "kanban priority: reading the class shares failed, no slot "
                "reserved this tick: %r", exc,
            )
            self.cap = None

    def _priority(self, task_id: str) -> int:
        if task_id not in self.priorities:
            row = self.conn.execute(
                "SELECT priority FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            self.priorities[task_id] = int(row[0] or 0) if row else 0
        return int(self.priorities[task_id] or 0)

    def holds_back(self, row: Any, result: Any) -> bool:
        """True when ``row``'s class already holds every slot but the other's one."""
        if self.cap is None:
            return False
        try:
            task_id = _cell(row, "id", 0)
            user = is_user_priority(self._priority(task_id))
            running = self.user_running if user else self.background_running
            if running >= self.cap:
                result.skipped_reserved.append(task_id)
                return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("kanban priority: reservation check failed: %r", exc)
        return False

    def took(self, row: Any) -> None:
        """Count a spawn against its class's share."""
        if self.cap is None:
            return
        try:
            if is_user_priority(self._priority(_cell(row, "id", 0))):
                self.user_running += 1
            else:
                self.background_running += 1
        except Exception:  # noqa: BLE001
            pass

    def finish(self, result: Any, *, dry_run: bool = False) -> None:
        note_waiting(
            self.conn, result, max_in_progress=self.max_in_progress,
            board=self.board, dry_run=dry_run,
        )


def reserved_slot(conn, max_in_progress: Optional[int], board: Optional[str]) -> ReservedSlot:
    return ReservedSlot(conn, max_in_progress, board)


def record_saturation(
    conn, result: Any, running: int, limit: int, board: Optional[str] = None,
) -> None:
    """Record what is holding the slots on ``result.saturation``.

    Called from ``_tick_spawn_budget`` when the cap is hit (before its early
    return, which otherwise records nothing) and by :func:`note_waiting` after
    a ready loop that left cards waiting. Never raises.
    """
    try:
        cards = [
            {
                "id": _cell(row, "id", 0),
                "assignee": _cell(row, "assignee", 1),
                "priority": int(_cell(row, "priority", 2) or 0),
                "session_id": _cell(row, "session_id", 3),
                "started_at": _cell(row, "started_at", 4),
            }
            for row in conn.execute(
                "SELECT id, assignee, priority, session_id, started_at FROM tasks "
                "WHERE status = 'running' ORDER BY started_at ASC, id ASC LIMIT ?",
                (CARDS_SHOWN,),
            ).fetchall()
        ]
        background = count_running_background_host(conn, board)
        user_waiting = background_waiting = 0
        for row in _ready_rows(conn):
            if is_user_priority(_cell(row, "priority", 1)):
                user_waiting += 1
            else:
                background_waiting += 1
        result.saturation = {
            "running": int(running),
            "limit": int(limit),
            "background_running": background,
            "user_running": max(0, int(running) - background),
            "cards": cards,
            "user_waiting": user_waiting,
            "background_waiting": background_waiting,
            "reserved": 0,
            "reserved_user": 0,
            "reserved_background": 0,
        }
    except Exception as exc:  # noqa: BLE001 — never break the dispatch tick
        logger.warning("kanban priority: recording saturation failed: %r", exc)


def _skipped_ids(result: Any) -> set:
    """Ready rows the tick skipped for a reason other than a full slot."""
    skipped = set(getattr(result, "skipped_unassigned", ()) or ())
    skipped.update(getattr(result, "skipped_nonspawnable", ()) or ())
    skipped.update(entry[0] for entry in getattr(result, "skipped_per_profile_capped", ()) or ())
    skipped.update(entry[0] for entry in getattr(result, "respawn_guarded", ()) or ())
    return skipped


def _queued_owed(conn, task_id: str) -> bool:
    """One ``queued`` event per wait: none yet, or none since the last claim."""
    queued = conn.execute(
        "SELECT MAX(id) FROM task_events WHERE task_id = ? AND kind = ?",
        (task_id, QUEUED_KIND),
    ).fetchone()[0]
    if queued is None:
        return True
    claimed = conn.execute(
        "SELECT MAX(id) FROM task_events WHERE task_id = ? AND kind = 'claimed'",
        (task_id,),
    ).fetchone()[0]
    return claimed is not None and claimed > queued


def _filed_by_a_person(conn, task_id: str) -> bool:
    """Whether a card is a top-level card a person's turn filed, not a child.

    A child is a card with a dependency parent (``task_links``) or a creator
    card (the ``creator_task_id`` upstream records on the ``created`` event
    when a dispatcher worker files it). A child of a user card waits as part
    of a request that is already running, so its wait is not news for the
    person's thread. Fails toward silence.
    """
    try:
        if conn.execute(
            "SELECT 1 FROM task_links WHERE child_id = ? LIMIT 1", (task_id,)
        ).fetchone():
            return False
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'created' "
            "ORDER BY id LIMIT 1",
            (task_id,),
        ).fetchone()
        if row and row[0]:
            payload = json.loads(row[0])
            if isinstance(payload, dict) and payload.get("creator_task_id"):
                return False
        return True
    except Exception:  # noqa: BLE001
        return False


def note_waiting(
    conn,
    result: Any,
    *,
    max_in_progress: Optional[int],
    board: Optional[str] = None,
    dry_run: bool = False,
) -> None:
    """After a tick: record saturation and tell waiting user cards they are queued.

    A user card counts as waiting when it is still ``ready`` and unclaimed, the
    tick did not skip it for a reason of its own (no assignee, not a profile,
    the per-profile cap, the respawn guard), and either every slot is busy or
    the only free one is held for background triage. Memory pressure can also
    leave cards behind with slots free; those are not told they are queued,
    because that is not what is holding them.

    ``result.ready_left`` counts only the rows left waiting for a slot, so a
    board whose remaining rows the tick skipped for their own reasons reads as
    having nothing left, not as stuck.
    """
    try:
        rows = _ready_rows(conn)
        skipped = _skipped_ids(result)
        waiting = [
            (_cell(row, "id", 0), _cell(row, "priority", 1))
            for row in rows
            if _cell(row, "id", 0) not in skipped
        ]
        result.ready_left = len(waiting)
        if max_in_progress is None:
            return
        user_waiting = [tid for tid, priority in waiting if is_user_priority(priority)]
        held = set(getattr(result, "skipped_reserved", ()) or ())
        held_user = [tid for tid in user_waiting if tid in held]
        reserved = len(held)
        if not user_waiting and not reserved and result.saturation is None:
            return
        if result.saturation is None:
            running = count_running_host(conn, board)
            if running < int(max_in_progress) and not reserved:
                return
            record_saturation(conn, result, running, int(max_in_progress), board)
        if result.saturation is None:
            return
        result.saturation["reserved"] = reserved
        result.saturation["reserved_user"] = len(held_user)
        result.saturation["reserved_background"] = reserved - len(held_user)
        if dry_run:
            return
        full = int(result.saturation["running"]) >= int(max_in_progress)
        candidates = user_waiting if full else held_user
        owed = [
            tid for tid in candidates
            if _filed_by_a_person(conn, tid) and _queued_owed(conn, tid)
        ]
        if not owed:
            return
        payload_base = {
            "note": QUEUED_TEXT,
            "running": result.saturation["running"],
            "limit": result.saturation["limit"],
            "background_running": result.saturation["background_running"],
        }
        kb = _kb()
        with kb.write_txn(conn):
            for tid in owed:
                ahead = user_waiting.index(tid)
                kb._append_event(conn, tid, QUEUED_KIND, {**payload_base, "ahead": ahead})
        result.queued_noticed.extend(owed)
    except Exception as exc:  # noqa: BLE001 — never break the dispatch tick
        logger.warning("kanban priority: noting waiting cards failed: %r", exc)


# ---------------------------------------------------------------------------
# kanban_create's return value
# ---------------------------------------------------------------------------

def _configured_cap() -> Optional[int]:
    kbd = _kbd()
    return kbd.resolve_max_in_progress(kbd.configured_max_in_progress())


def queue_fields(
    conn, task_id: str, board: Optional[str] = None,
    cap_reader: Optional[Callable[[], Optional[int]]] = None,
) -> dict:
    """``{"queued": True, "queue": {...}}`` when the new card's class has no
    free slot now.

    The test is the slot count alone: the cap less every running card, and,
    at a cap of 2 or more, the card's class share (:func:`class_cap`) less the
    cards of its class already running. Ready rows ahead of the card are not
    counted, because the dispatcher may hold them for the other class's slot or
    skip them for reasons of their own (no assignee, the per-profile cap), and
    a row it will not spawn is no wait. So ``queued`` never reports a wait that
    will not happen; a card filed behind other spawnable rows can still wait a
    tick unflagged, and the dispatcher's ``queued`` event covers that.

    Machine-readable only. The thread hears about the wait once, from the
    dispatcher's ``queued`` event, so this carries no text for the model to
    relay. Empty when the card has started, has no slot to wait for, or
    anything cannot be read, and in a dispatcher worker: the cap read here is
    the calling profile's, which is the dispatcher's only in the gateway's own
    turn (a worker's profile carries no ``kanban`` key).
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return {}
    try:
        row = conn.execute(
            "SELECT status, priority, created_at, claim_lock FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None or _cell(row, "status", 0) != "ready" or _cell(row, "claim_lock", 3):
            return {}
        limit = (cap_reader or _configured_cap)()
        if limit is None:
            return {}
        limit = int(limit)
        running = count_running_host(conn, board)
        free = limit - running
        user = is_user_priority(_cell(row, "priority", 1))
        cap = class_cap(limit)
        if cap is not None:
            background = count_running_background_host(conn, board)
            held_by_class = background if not user else max(0, running - background)
            free = min(free, cap - held_by_class)
        if free > 0:
            return {}
        return {"queued": True, "queue": {"running": running, "limit": limit}}
    except Exception as exc:  # noqa: BLE001 — never fail kanban_create
        logger.debug("kanban priority: queue snapshot failed: %r", exc)
        return {}


# ---------------------------------------------------------------------------
# The notifier's formatter
# ---------------------------------------------------------------------------


def queued_note(payload: object) -> str:
    """The line a ``queued`` event carries, or ``""``."""
    if not isinstance(payload, dict):
        return ""
    note = payload.get("note")
    return str(note).strip() if note else ""


def format_queued(ev: Any, n: Any) -> tuple:
    """``_EVENT_FORMATTERS["queued"]``: the queued line, waking nobody.

    Returns upstream's ``(msg, wake_handoff, review_detail)``. The message is
    the user-facing sentence and nothing else; ``kanban_progress_lines`` rolls
    it into the card's progress message, where the worker's first note joins
    it under the card's header.
    """
    note = queued_note(getattr(ev, "payload", None))
    if not note:
        return None, None, None
    return f"{QUEUED_MARKER} {note}", None, None


# ---------------------------------------------------------------------------
# The watcher's warning
# ---------------------------------------------------------------------------

_WATCH = {"ticks": 0, "last_warn": 0.0}


def _explained(res: Any) -> bool:
    """A board whose tick spawned nothing for a reason this module knows."""
    if getattr(res, "saturation", None) is not None:
        return True
    if getattr(res, "skipped_reserved", None):
        return True
    return getattr(res, "ready_left", None) == 0


def saturation_of(results: Optional[list]) -> Optional[dict]:
    """The merged saturation of a tick in which every board is accounted for,
    or None when the tick is not saturation (one board idle for no known
    reason is enough: that is upstream's stuck case)."""
    boards = [res for _slug, res in (results or []) if res is not None]
    if not boards or not all(_explained(res) for res in boards):
        return None
    sats = [res.saturation for res in boards if getattr(res, "saturation", None)]
    if not sats:
        return None
    merged = dict(sats[0])
    merged["cards"] = [card for sat in sats for card in sat.get("cards", ())][:CARDS_SHOWN]
    for key in ("user_waiting", "background_waiting", "reserved", "reserved_user", "reserved_background"):
        merged[key] = sum(int(sat.get(key, 0) or 0) for sat in sats)
    return merged


def _age(started_at: object, now: float) -> str:
    try:
        seconds = max(0, int(now) - int(started_at))
    except (TypeError, ValueError):
        return "?"
    if seconds >= SECONDS_PER_HOUR:
        return f"{seconds // SECONDS_PER_HOUR}h"
    if seconds >= SECONDS_PER_MINUTE:
        return f"{seconds // SECONDS_PER_MINUTE}m"
    return f"{seconds}s"


def _origin(card: dict) -> str:
    """A running card's label: ``user``, or the background producer's prefix.

    The priority decides the class. The stored session only names which
    background producer filed a background card, because a user card can carry
    whatever session a model passed to ``kanban_create``.
    """
    if is_user_priority(card.get("priority")):
        return "user"
    session = card.get("session_id")
    for prefix in BACKGROUND_SESSION_PREFIXES:
        if isinstance(session, str) and session.startswith(prefix):
            return prefix
    return "background"


def saturation_message(sat: dict, now: Optional[float] = None) -> str:
    """The warning text. Counts belong here, not in the user's thread."""
    now = time.time() if now is None else now
    cards = ", ".join(
        f"{card.get('id')} @{card.get('assignee') or '?'} "
        f"{_age(card.get('started_at'), now)} [{_origin(card)}]"
        for card in sat.get("cards", ())
    )
    # A free slot held for the other class is not saturation; say which class
    # is being held back.
    running, limit = sat.get("running"), sat.get("limit")
    full = isinstance(running, int) and isinstance(limit, int) and running >= limit
    held_user = int(sat.get("reserved_user", 0) or 0)
    held_background = sat.get("reserved_background")
    if held_background is None:
        held_background = int(sat.get("reserved", 0) or 0) - held_user
    held_background = int(held_background or 0)
    if full:
        state = "saturated"
    elif held_user and not held_background:
        state = "holding user cards"
    else:
        state = "holding background cards"
    text = (
        f"kanban dispatcher {state}: {running}/{limit} "
        f"worker slots busy ({sat.get('background_running', 0)} background, "
        f"{sat.get('user_running', 0)} user: {cards or 'none listed'}); "
        f"{sat.get('user_waiting', 0)} user card(s) and "
        f"{sat.get('background_waiting', 0)} background card(s) waiting"
    )
    if held_background:
        text += (
            f"; {held_background} background card(s) held back because one slot "
            "is reserved for user cards"
        )
    if held_user:
        text += (
            f"; {held_user} user card(s) held back because one slot is reserved "
            "for background triage"
        )
    return text


def saturation_tick(
    log: Any, results: Optional[list], ready_pending: bool, any_spawned: bool,
    window: int, now: Optional[float] = None,
) -> bool:
    """Classify one dispatcher tick; True when it was saturation, not a bad tick.

    The caller resets its stuck counter on True. The warning is logged once
    ``window`` saturated ticks have run back to back (upstream's own
    ``_HEALTH_WINDOW``, so a cap touched for a moment says nothing) and at most
    every :data:`WARN_INTERVAL_SECONDS`. Never raises; on any error the tick is
    left to upstream's stuck check.
    """
    try:
        sat = saturation_of(results) if ready_pending and not any_spawned else None
        if sat is None:
            _WATCH["ticks"] = 0
            return False
        _WATCH["ticks"] += 1
        now = time.time() if now is None else now
        if _WATCH["ticks"] >= window and now - _WATCH["last_warn"] >= WARN_INTERVAL_SECONDS:
            log.warning("%s", saturation_message(sat, now))
            _WATCH["last_warn"] = now
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("kanban priority: saturation check failed: %r", exc)
        return False
