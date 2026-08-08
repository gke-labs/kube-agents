"""Give a kanban worker's child cards the chat subscription of its own card.

Installed into the image at ``/opt/hermes/tools/kanban_auto_subscribe.py`` and
wired into ``tools/kanban_tools.py`` (the ``kanban_create`` handler) by
``deploy/docker/patches/apply_kanban_auto_subscribe.py``.

The gap, measured
-----------------
The gateway notifier only sees cards with a ``kanban_notify_subs`` row. Two
things write those rows at creation time, and both missed the worker case on
the 2026-08-07 live run (12:58):

* ``_maybe_auto_subscribe`` reads the originating chat identity
  (``HERMES_SESSION_CHAT_ID`` / ``_THREAD_ID`` / ``_PLATFORM``) from session
  context that exists only on inbound user messages. The coordinator card
  ``t_b9659077`` was created in-session by the chat agent and got its row; the
  four cards the coordinator created *as a dispatcher-spawned worker* (three
  sleep tasks and the synthesizer ``t_f54bd6b5``) had no session context and
  got nothing.
* Upstream ``create_task`` does inherit subscriptions — but only from the
  explicit ``parents=[...]`` graph edges (``_inherit_notify_subs``). A
  worker's own card is deliberately not a graph parent of the work it fans
  out (``parents=[<itself>]`` is the self-parenting deadlock the
  ``kanban_scheduling`` patch exists to untangle), so the chain from
  the user's thread to the fan-out is severed at the first worker.

Result: the synthesizer's answer sat undelivered for 91.3s until the user
asked after it — ``gateway.log`` shows zero Slack sends between 12:59:02 and
13:02:15. The manual remedy,
``agents/platform/scripts/kanban_notify_propagate.py``, relies on the worker
remembering to run it; it didn't.

The fix
-------
:func:`maybe_inherit_worker_subscriptions` runs right after
``_maybe_auto_subscribe`` in the ``kanban_create`` tool handler. When the
creating process is a kanban worker (``HERMES_KANBAN_TASK`` set, non-empty,
and not the new card itself — the dispatcher pins it into every worker), the
new card inherits every subscription row of the card the worker is running.
So the user's thread follows the work wherever a worker fans it out, one hop
at a time: coordinator → sleep tasks → synthesizer, each inheriting from the
card that created it.

Same contract as the manual script it automates: the copied column set is
``platform, chat_id, thread_id, user_id, notifier_profile`` (also exactly
what upstream ``_inherit_notify_subs`` copies), ``INSERT OR IGNORE`` on the
subscription primary key makes it idempotent — the script remains in place
as a manual/back-fill tool and double-writes are harmless — and
``created_at`` is re-stamped.

The cursor starts caught up
---------------------------
``last_event_id`` is seeded at the child's current ``MAX(task_events.id)``
rather than at 0, because a subscriber wants the events that happen after it
subscribes and nothing before. Both upstream seeding paths in
``hermes_cli/kanban_db.py`` already do this and each records the bug that
taught them to: ``add_notify_sub`` snaps to the head because a cursor of 0
on an already-active task made the notifier replay every historical terminal
event on its next tick, a boot-time burst of 100+ messages (issue #29905),
and ``_inherit_notify_subs`` does it so ``link_tasks(parent,
existing_child)`` does not replay the child's pre-link history.

This patch needs it for a third reason, and the reason is not theoretical.
``kanban_create`` accepts an ``idempotency_key``, and ``create_task`` answers
a repeat key by returning the id of the *existing* card — before it opens
``write_txn``, without creating anything. The ``new_tid`` this patch hooks is
therefore not always a new card: a worker that re-issues an idempotent create
gets back a card that may have run, blocked and completed days earlier.
Seeded at 0, the row written below made the notifier's very next tick post
that entire history into the user's chat thread — the status transition, the
block, and the completion line for work nobody had just asked about. Seeded
at the head, the same worker gets exactly what it wanted: this card's future
events, in the thread that asked for them.

Fail-soft throughout: any exception is logged and swallowed. A notification
bookkeeping failure must never fail the ``kanban_create`` the worker is
mid-flight on — the same posture ``_maybe_auto_subscribe`` itself takes.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Union

logger = logging.getLogger(__name__)

#: The subscription columns copied parent -> child. ``task_id`` is rewritten,
#: ``created_at`` re-stamped, ``last_event_id`` seeded at the child's own
#: head. Matches both the manual propagate script and upstream
#: ``_inherit_notify_subs`` so a schema drift shows up here as a clear error
#: instead of a silently wrong copy.
COPY_COLUMNS = ("platform", "chat_id", "thread_id", "user_id", "notifier_profile")

#: The env var the dispatcher pins into every worker it spawns: the id of the
#: card the worker is running. Presence (with a different id than the new
#: card) is the definition of "this create came from a worker".
WORKER_TASK_ENV = "HERMES_KANBAN_TASK"


def inherit_subscriptions(
    conn_or_db_path: Union[sqlite3.Connection, str, "os.PathLike[str]"],
    child_task_id: str,
    parent_task_id: str,
) -> int:
    """Copy ``kanban_notify_subs`` rows from parent to child. Fail-soft.

    Returns the number of rows written (0 on any failure, no-op, or when the
    child already has them — idempotent via ``INSERT OR IGNORE`` on the
    ``(task_id, platform, chat_id, thread_id)`` primary key). Never raises.

    Refuses to write for a child card not on the board, for the reason the
    manual script documents: the notifier unsubscribes only when a task turns
    terminal, so a row for a card that does not exist would be scanned on
    every notifier tick for the life of the board.

    The row is seeded at the child's current event head, so an already-active
    child — the card an idempotent ``kanban_create`` hands back — does not
    replay its history into the thread. See the module docstring.
    """
    try:
        if not child_task_id or not parent_task_id:
            return 0
        if child_task_id == parent_task_id:
            return 0

        own_conn = not isinstance(conn_or_db_path, sqlite3.Connection)
        if own_conn:
            # `timeout=` IS the busy timeout; the gateway and CLI write this
            # same board, and a nudge lost to a busy DB is a lost delivery.
            conn = sqlite3.connect(str(conn_or_db_path), timeout=10)
        else:
            conn = conn_or_db_path
        try:
            # One statement answers both questions this function has to ask
            # about the child: is it on the board at all, and where does its
            # event history already end. A row means it exists; the value is
            # the cursor to start the subscriber at.
            row = conn.execute(
                "SELECT COALESCE("
                "    (SELECT MAX(id) FROM task_events WHERE task_id = ?), 0"
                ") FROM tasks WHERE id = ?",
                (child_task_id, child_task_id),
            ).fetchone()
            if row is None:
                logger.warning(
                    "kanban_auto_subscribe: child card %r not on this board; "
                    "refusing to write an uncollectable subscription row",
                    child_task_id,
                )
                return 0
            head = int(row[0])
            cols = ", ".join(COPY_COLUMNS)
            cur = conn.execute(
                f"""
                INSERT OR IGNORE INTO kanban_notify_subs
                    (task_id, {cols}, created_at, last_event_id)
                SELECT ?, {cols}, ?, ?
                  FROM kanban_notify_subs
                 WHERE task_id = ?
                """,
                (child_task_id, int(time.time()), head, parent_task_id),
            )
            written = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            # kanban_db connections run autocommit with explicit BEGIN
            # IMMEDIATE for multi-statement writes; a plain connect() opens a
            # deferred txn on the INSERT instead. Commit whichever applies.
            if getattr(conn, "in_transaction", False):
                conn.commit()
            if written:
                logger.info(
                    "kanban_auto_subscribe: %s inherited %d subscription "
                    "row(s) from %s, watching from event %d",
                    child_task_id, written, parent_task_id, head,
                )
            return written
        finally:
            if own_conn:
                conn.close()
    except Exception as exc:  # noqa: BLE001 — never break the create
        logger.warning(
            "kanban_auto_subscribe: inherit %r -> %r failed (continuing): %r",
            parent_task_id, child_task_id, exc,
        )
        return 0


def maybe_inherit_worker_subscriptions(conn, child_task_id: str) -> int:
    """Inherit from the worker's own card when this process is a worker.

    The single call the patched ``kanban_create`` handler makes. A process
    with no ``HERMES_KANBAN_TASK`` (chat session, CLI, cron) is left exactly
    as upstream — ``_maybe_auto_subscribe`` and ``_inherit_notify_subs``
    already cover those paths.

    Never raises, but the guarantee lives one level down: reading an env var
    cannot fail, and every statement that can — the board reads and the
    insert — is inside :func:`inherit_subscriptions`'s own catch-all.
    """
    parent = (os.environ.get(WORKER_TASK_ENV) or "").strip()
    if not parent or parent == child_task_id:
        return 0
    return inherit_subscriptions(conn, child_task_id, parent)
