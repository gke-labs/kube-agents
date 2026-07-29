#!/usr/bin/env python3
# kanban_notify_propagate.py - Copy a parent kanban card's chat subscription onto
# a child card, so the child's completion pings the user's chat thread too.
#
# Why this exists
# ---------------
# The gateway kanban notifier delivers thread updates by iterating rows in the
# `kanban_notify_subs` table: a card with no subscription row is invisible to it
# (no line posted, no agent woken). Subscriptions are written by
# `_maybe_auto_subscribe` at `kanban_create` time, which reads the originating
# chat identity (`HERMES_SESSION_CHAT_ID` / `_THREAD_ID` / `_PLATFORM`) from the
# session context. Those context vars are set ONLY on inbound user messages.
#
# A specialist agent that is running as a dispatcher-spawned kanban worker has no
# such session context, so the child cards it creates to stage its work are NOT
# auto-subscribed — and their completions never reach the user. This helper closes
# that gap: it copies the parent card's subscription row(s) onto a freshly created
# child, so each staged sub-step's completion posts its own line into the same
# chat thread the original request came from.
#
# Usage (run by the specialist right after creating a child card):
#   python3 /opt/data/scripts/kanban_notify_propagate.py --to <child_id> [--from <parent_id>]
#
# `--from` defaults to $HERMES_KANBAN_TASK (the worker's current card). The board
# DB is read from $HERMES_KANBAN_DB (pinned into every worker by the dispatcher).
#
# Idempotent (INSERT OR IGNORE on the subscription primary key) and fail-soft: any
# operational problem is logged to stderr and exits 0 so it can never break the
# specialist's flow. It couples only to the stable `kanban_notify_subs` columns,
# plus a soft read of `tasks` to confirm the child card is real before writing a
# subscription row that nothing would ever clean up (see `propagate`).

import argparse
import os
import sqlite3
import sys
import time

# The subscription columns we copy from parent -> child. `task_id` is rewritten
# to the child id; `created_at` is re-stamped; `last_event_id` resets to 0 so the
# child's own events are delivered from the start. Pinned here so a schema drift
# in Hermes surfaces as a clear error (and a failing unit test) rather than
# silently copying the wrong shape.
_COPY_COLUMNS = ("platform", "chat_id", "thread_id", "user_id", "notifier_profile")


def log(msg: str) -> None:
    print(f"[KANBAN-PROPAGATE] {msg}", file=sys.stderr)


def _connect(db_path: str) -> sqlite3.Connection:
    # `timeout=` IS the busy timeout (Python calls sqlite3_busy_timeout with it), so
    # it is the only knob set here — a `PRAGMA busy_timeout` would silently override
    # it and leave two different values in the source. Waiting matters: the gateway
    # and CLI write this same board, and a propagation lost to a busy DB costs the
    # user progress updates for that sub-step, which is the whole point of the
    # script. Deliberately no `PRAGMA journal_mode` — cooperate with the WAL store
    # the gateway/CLI already set up; never force it.
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def propagate(db_path: str, parent_id: str, child_id: str) -> int:
    """Copy every kanban_notify_subs row from parent_id to child_id.

    Returns the number of subscription rows written to the child. Idempotent:
    re-running is a no-op once the child rows exist. Raises on a genuinely broken
    DB / missing table / bad args so callers (and tests) can see real failures;
    the CLI wrapper turns those into a fail-soft exit.

    Refuses to write for a child card that is not on the board, because such a
    row can never be cleaned up: the notifier unsubscribes only when a task turns
    terminal, and `delete_task` opens with `DELETE FROM tasks WHERE id = ?` and
    returns early when that matches nothing, so its cascade never reaches
    `kanban_notify_subs`. A typo'd `--to` would therefore leave a row scanned on
    every notifier tick for the life of the board.
    """
    if not child_id:
        raise ValueError("child id (--to) is required")
    if not parent_id:
        raise ValueError("parent id (--from / $HERMES_KANBAN_TASK) is required")
    if parent_id == child_id:
        log(f"parent and child are the same card ({child_id}); nothing to do")
        return 0
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"kanban DB not found at {db_path!r}")

    conn = _connect(db_path)
    try:
        # Soft coupling on purpose. This module's contract is that it depends only
        # on `kanban_notify_subs`; a board that somehow lacks `tasks` gets the old
        # unguarded behaviour rather than losing propagation entirely, because
        # failing every propagation would be a worse bug than the orphan row this
        # prevents. On a real board `tasks` is always there and the guard is live.
        if _table_exists(conn, "tasks"):
            if not conn.execute(
                "SELECT 1 FROM tasks WHERE id = ?", (child_id,)
            ).fetchone():
                raise ValueError(f"child card {child_id!r} not found on this board")
        else:
            log("board has no `tasks` table; skipping the child-card existence check")

        cols = ", ".join(_COPY_COLUMNS)
        rows = conn.execute(
            f"SELECT {cols} FROM kanban_notify_subs WHERE task_id = ?",
            (parent_id,),
        ).fetchall()
        if not rows:
            log(
                f"parent card {parent_id} has no chat subscription; nothing to "
                f"propagate to {child_id} (the request may not have come from chat)"
            )
            return 0

        now = int(time.time())
        placeholders = ", ".join(["?"] * (len(_COPY_COLUMNS) + 3))
        insert_cols = "task_id, " + cols + ", created_at, last_event_id"
        with conn:  # single transaction; commits on success, rolls back on error
            conn.executemany(
                f"INSERT OR IGNORE INTO kanban_notify_subs ({insert_cols}) "
                f"VALUES ({placeholders})",
                [(child_id, *[r[c] for c in _COPY_COLUMNS], now, 0) for r in rows],
            )
        # Report the child's resulting subscription count (idempotent: a re-run
        # leaves this unchanged rather than double-counting).
        written = conn.execute(
            "SELECT COUNT(*) FROM kanban_notify_subs WHERE task_id = ?",
            (child_id,),
        ).fetchone()[0]
        log(f"propagated subscription(s) from {parent_id} -> {child_id} "
            f"(child now has {written} row(s))")
        return written
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Copy a parent kanban card's chat subscription onto a child card."
    )
    parser.add_argument("--to", dest="child", required=True, help="child task id")
    parser.add_argument(
        "--from", dest="parent", default=os.environ.get("HERMES_KANBAN_TASK", ""),
        help="parent task id (defaults to $HERMES_KANBAN_TASK)",
    )
    parser.add_argument(
        "--db", dest="db", default=os.environ.get("HERMES_KANBAN_DB", ""),
        help="board DB path (defaults to $HERMES_KANBAN_DB)",
    )
    args = parser.parse_args(argv)

    if not args.db:
        log("no board DB configured ($HERMES_KANBAN_DB unset); skipping (fail-soft)")
        return 0
    try:
        propagate(args.db, args.parent, args.child)
    except Exception as e:  # noqa: BLE001 - fail-soft: never break the worker's flow
        log(f"propagation failed (continuing): {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
