#!/usr/bin/env python3
# kanban_notify_propagate.py - Copy a parent kanban card's chat subscription onto
# a child card, so the child's completion pings the user's chat thread too.
#
# Why this exists
# ---------------
# The gateway kanban notifier delivers thread updates by iterating a card's
# subscriptions: a card with none is invisible to it (no line posted, no agent
# woken). Subscriptions are written by `_maybe_auto_subscribe` at `kanban_create`
# time, which reads the originating chat identity (`HERMES_SESSION_CHAT_ID` /
# `_THREAD_ID` / `_PLATFORM`) from the session context. Those context vars are set
# ONLY on inbound user messages.
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
# is read from $HERMES_KANBAN_DB (pinned into every worker by the dispatcher).
#
# Idempotent and fail-soft: any operational problem is logged to stderr and exits
# 0 so it can never break the specialist's flow.
#
# What this file is NOT
# ---------------------
# It is not where the board's storage lives. Every table name, column list, and
# connection setting belongs to `kanban_store.KanbanStore`; this file is the
# policy on top -- which cards, in which direction, and what to do when it fails.
# Adding SQL here would put the coupling back where it was.

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.absolute()))

from kanban_store import BoardUnavailable, KanbanStore  # noqa: E402


def log(msg: str) -> None:
    print(f"[KANBAN-PROPAGATE] {msg}", file=sys.stderr)


def propagate(db_path: str, parent_id: str, child_id: str) -> int:
    """Copy every subscription from parent_id to child_id.

    Returns the number of subscriptions the child ends up with. Idempotent:
    re-running is a no-op once they exist. Raises on a genuinely broken board /
    missing card / bad args so callers (and tests) can see real failures; the CLI
    wrapper below turns those into a fail-soft exit.

    Refuses to write for a child card that is not on the board, because such a
    row can never be cleaned up: the notifier unsubscribes only when a task turns
    terminal, and `delete_task` opens with `DELETE FROM tasks WHERE id = ?` and
    returns early when that matches nothing, so its cascade never reaches the
    subscription table. A typo'd `--to` would therefore leave a row scanned on
    every notifier tick for the life of the board.
    """
    if not child_id:
        raise ValueError("child id (--to) is required")
    if not parent_id:
        raise ValueError("parent id (--from / $HERMES_KANBAN_TASK) is required")
    if parent_id == child_id:
        log(f"parent and child are the same card ({child_id}); nothing to do")
        return 0

    store = KanbanStore(db_path)

    # Tri-state on purpose: `None` means the board has no card table to check
    # against. Treating that as "card missing" would refuse every propagation on a
    # board this helper does not recognise, which is a worse failure than the
    # orphan row the check prevents. On a real board the check is live.
    exists = store.card_exists(child_id)
    if exists is False:
        raise ValueError(f"child card {child_id!r} not found on this board")
    if exists is None:
        log("board has no card table; skipping the child-card existence check")

    identities = store.subscriptions_for(parent_id)
    if not identities:
        log(
            f"parent card {parent_id} has no chat subscription; nothing to "
            f"propagate to {child_id} (the request may not have come from chat)"
        )
        return 0

    written = store.add_subscriptions(child_id, identities)
    log(f"propagated subscription(s) from {parent_id} -> {child_id} "
        f"(child now has {written} row(s))")
    return written


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
        "--db", dest="db", default="",
        help="board path (defaults to the store's configured board)",
    )
    args = parser.parse_args(argv)

    try:
        db_path = KanbanStore.from_env(args.db).db_path
    except BoardUnavailable as e:
        log(f"{e}; skipping (fail-soft)")
        return 0
    try:
        propagate(db_path, args.parent, args.child)
    except Exception as e:  # noqa: BLE001 - fail-soft: never break the worker's flow
        log(f"propagation failed (continuing): {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
