#!/usr/bin/env python3
"""Build gate for the kanban worker subscription-inheritance patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after the applier.
The applier only proves the anchor matched. This replays the 2026-08-07
incident shape through the *real patched* tool handler — the same
``_handle_create`` a worker's ``kanban_create`` call reaches — against a real
board: a subscribed coordinator card, a worker env
(``HERMES_KANBAN_TASK``), and children created without the session context
that ``_maybe_auto_subscribe`` needs. Before the patch those children were
invisible to the notifier and the synthesizer's answer sat undelivered for
91.3 seconds; after it, every hop inherits the chat thread.

Usage::

    cd /opt/hermes && python3 verify_kanban_auto_subscribe.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

FAILURES: list[str] = []


def check(label: str, condition: object, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


TMP = Path(tempfile.mkdtemp())
DB = TMP / "kanban.db"
# Pin the board the tool layer resolves — the same pin the dispatcher injects
# into every worker it spawns.
os.environ["HERMES_KANBAN_DB"] = str(DB)
os.environ.pop("HERMES_KANBAN_TASK", None)
# The TUI fallback in _maybe_auto_subscribe keys on HERMES_SESSION_KEY; the
# incident's defining feature is that a worker has no session identity at all.
os.environ.pop("HERMES_SESSION_KEY", None)

from hermes_cli import kanban_db as K  # noqa: E402
import tools.kanban_tools as kt  # noqa: E402

conn = K.connect(DB)


def subs(task_id):
    return conn.execute(
        "SELECT * FROM kanban_notify_subs WHERE task_id = ? "
        "ORDER BY platform, chat_id",
        (task_id,),
    ).fetchall()


def head(task_id):
    return conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM task_events WHERE task_id = ?",
        (task_id,),
    ).fetchone()[0]


# The kinds gateway/kanban_watchers.py claims for a subscriber; anything the
# cursor is left behind is a message that lands in the user's chat thread.
# Mirrors the tuple as apply_kanban_progress_lines.py leaves it -- "heartbeat"
# included. Narrower than the patched claim filter and this check goes green on
# exactly the replay it exists to catch: a cursor left behind progress notes
# would repost them into the thread.
TERMINAL_KINDS = (
    "completed", "blocked", "gave_up", "crashed", "timed_out",
    "status", "archived", "unblocked", "block_loop_detected", "heartbeat",
)


def would_replay(task_id):
    """The events the notifier's next tick would post for ``task_id``."""
    _, events = K.unseen_events_for_sub(
        conn,
        task_id=task_id,
        platform="slack",
        chat_id="C0PLATFORM",
        thread_id="1723033132.001",
        kinds=TERMINAL_KINDS,
    )
    return [e.kind for e in events]


def tool_create(**args):
    out = json.loads(kt._handle_create({"assignee": "platform", **args}))
    if not out.get("ok"):
        raise AssertionError(f"kanban_create failed: {out}")
    return out


check(
    "the create handler resolved the inheritance import",
    hasattr(kt, "_kanban_inherit_worker_subs"),
    "the trailer import did not execute",
)

# --- The incident, replayed --------------------------------------------------
print("worker-created cards:")
# The coordinator card, subscribed the way the chat agent's session was.
coordinator = K.create_task(conn, title="coordinator", assignee="platform")
K.add_notify_sub(
    conn,
    task_id=coordinator,
    platform="slack",
    chat_id="C0PLATFORM",
    thread_id="1723033132.001",
    user_id="U0ADAM",
    notifier_profile="default",
)
check("the coordinator card is subscribed", len(subs(coordinator)) == 1)

# Now the coordinator runs as a dispatcher-spawned worker: HERMES_KANBAN_TASK
# is pinned, session context vars are absent.
os.environ["HERMES_KANBAN_TASK"] = coordinator

sleeper = tool_create(title="sleep task 1")
check(
    "the worker's create has no session context to auto-subscribe with",
    sleeper.get("subscribed") is False,
    "session context leaked into the verify environment",
)
rows = subs(sleeper["task_id"])
check("the worker's child card inherits the chat subscription", len(rows) == 1)
if rows:
    row = rows[0]
    check(
        "the inherited row targets the originating thread",
        row["platform"] == "slack"
        and row["chat_id"] == "C0PLATFORM"
        and row["thread_id"] == "1723033132.001"
        and row["user_id"] == "U0ADAM"
        and row["notifier_profile"] == "default",
    )
    check(
        "the child's cursor starts caught up with its own history",
        row["last_event_id"] == head(sleeper["task_id"]),
        f"last_event_id={row['last_event_id']}, head={head(sleeper['task_id'])}",
    )
    check(
        "so the notifier's next tick has nothing to replay",
        would_replay(sleeper["task_id"]) == [],
        f"would post {would_replay(sleeper['task_id'])}",
    )

# A child that ALSO names the subscribed card as a graph parent hits both
# upstream's _inherit_notify_subs and this patch — the primary key must
# deduplicate them.
synthesizer = tool_create(
    title="synthesizer", parents=[sleeper["task_id"]]
)
check(
    "overlap with upstream parent-edge inheritance stays single",
    len(subs(synthesizer["task_id"])) == 1,
    f"{len(subs(synthesizer['task_id']))} rows",
)

# The chain must survive a second hop: a worker running a child card fans out
# again, and the grandchild still reaches the user's thread.
os.environ["HERMES_KANBAN_TASK"] = synthesizer["task_id"]
grandchild = tool_create(title="grandchild")
check("a second fan-out hop still inherits", len(subs(grandchild["task_id"])) == 1)

# A worker whose own card was never subscribed (no chat origin) propagates
# nothing — and the create must still succeed.
unsubscribed = K.create_task(conn, title="cron-born card", assignee="platform")
os.environ["HERMES_KANBAN_TASK"] = unsubscribed
orphan = tool_create(title="child of an unsubscribed card")
check("an unsubscribed parent propagates nothing", len(subs(orphan["task_id"])) == 0)

# --- The card an idempotent create hands back --------------------------------
# create_task answers a repeat idempotency_key by returning the id of the
# EXISTING card, before it opens write_txn. The tool handler cannot tell the
# difference, so this patch's hook can land on a card with a full history —
# and a cursor of 0 would post all of it into the user's thread.
print("idempotent re-create of a finished card:")
finished = K.create_task(
    conn, title="nightly cost report", assignee="platform",
    idempotency_key="nightly-cost-report",
)
# _append_event is the helper every kernel status/blocked/completed path calls;
# the gate only needs the history to be there, not the runs that produced it.
with K.write_txn(conn):
    for kind in ("status", "blocked", "unblocked", "completed"):
        K._append_event(conn, finished, kind, None)

os.environ["HERMES_KANBAN_TASK"] = coordinator
again = tool_create(
    title="nightly cost report", idempotency_key="nightly-cost-report"
)
check(
    "the repeat create returns the existing card",
    again["task_id"] == finished,
    f"got {again['task_id']}, expected {finished}",
)
check("that card is subscribed to the worker's thread", len(subs(finished)) == 1)
check(
    "and its closed history is NOT replayed into the thread",
    would_replay(finished) == [],
    f"would post {would_replay(finished)}",
)

# --- Non-worker creates are untouched -----------------------------------------
print("non-worker creates:")
os.environ.pop("HERMES_KANBAN_TASK", None)
plain = tool_create(title="created outside any worker")
check(
    "a non-worker create writes no subscription",
    len(subs(plain["task_id"])) == 0,
    "the patch must not widen beyond dispatcher-spawned workers",
)

print()
if FAILURES:
    print(f"verify_kanban_auto_subscribe: {len(FAILURES)} FAILED")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("verify_kanban_auto_subscribe: all checks passed")
