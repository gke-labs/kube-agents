#!/usr/bin/env python3
"""Build gate for the kanban_link guard.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_kanban_link_running_guard.py``. The applier only proves the anchor
matched. This replays the #3248 shape through the *real patched* handlers on a
real board: a worker on the parent card fans out a child, the dispatcher
claims the child, and the worker then links the child to its own card. Before
the patch that link was accepted and the running child was gated behind its
parent. After it, the link is refused with the instruction to wait, no
``task_links`` row is written, and the child is still running.

It also checks what must keep working: a link to a card that has never
started, and ``kanban_create`` with ``parents=[<own card>]`` while the
creator is running.

Usage::

    cd /opt/hermes && python3 verify_kanban_link_running_guard.py
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
# Pin the board the tool layer resolves, the same pin the dispatcher injects
# into every worker it spawns.
os.environ["HERMES_KANBAN_DB"] = str(DB)
for name in ("HERMES_KANBAN_TASK", "HERMES_SESSION_KEY"):
    os.environ.pop(name, None)

from hermes_cli import kanban_db as K  # noqa: E402
from hermes_cli import kanban_db_connect as KC  # noqa: E402
import tools.kanban_tools as kt  # noqa: E402

conn = KC.connect(DB)
CHILD_ASSIGNEE = "cluster-ka-today-rehearsal"


def tool_create(**args):
    out = json.loads(kt._handle_create({"assignee": CHILD_ASSIGNEE, **args}))
    if not out.get("ok"):
        raise AssertionError(f"kanban_create failed: {out}")
    return out["task_id"]


def tool_link(parent_id, child_id):
    return json.loads(kt._handle_link({"parent_id": parent_id, "child_id": child_id}))


def status(task_id):
    return conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]


def linked(parent_id, child_id):
    return conn.execute(
        "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?", (parent_id, child_id)
    ).fetchone() is not None


print("wiring:")
check(
    "the link handler resolved the gate import",
    hasattr(kt, "_kanban_refuse_started_child_link"),
    "the trailer import did not execute",
)

# --- 1. The #3248 shape -------------------------------------------------------
print("a running child:")
parent = tool_create(title="How many pods in kubeagents-system")
check("the parent card claims", K.claim_task(conn, parent) is not None)
os.environ["HERMES_KANBAN_TASK"] = parent
child = tool_create(title="Count pods in kubeagents-system on ka-today-rehearsal")
check("the fan-out child claims", K.claim_task(conn, child) is not None)
out = tool_link(parent, child)
expected = (
    f"kanban_link refused: {child} is already running and reports back when it "
    f"finishes; a link now would make it wait for {parent} and strand its run. "
    f"No link is needed: wait for its result."
)
check("the running child is refused with the instruction to wait", out.get("error") == expected, f"got {out}")
check("no task_links row was written", not linked(parent, child))
check("the child is still running", status(child) == "running", status(child))

# --- 2. A child whose run already ended ---------------------------------------
print("a child that has had a run:")
check(
    "the child's run ends",
    K.complete_task(conn, child, result="16 pods", summary="counted"),
)
def started_text(card, card_status):
    return (
        f"kanban_link refused: {card} has already started (status: {card_status}); a "
        f"link now would gate it behind {parent}. Leave its dependencies as they "
        f"are; if it is blocked, it needs an unblock, not a link."
    )


out = tool_link(parent, child)
check("a done child is refused with the started text", out.get("error") == started_text(child, "done"), f"got {out}")
check("no task_links row was written", not linked(parent, child))

print("a blocked child:")
blocked = tool_create(title="Drain the node pool (needs approval)")
check("the second child claims", K.claim_task(conn, blocked) is not None)
check(
    "its run blocks on a question",
    K.block_task(conn, blocked, reason="approve the drain?", kind="needs_input"),
)
check("it is blocked", status(blocked) == "blocked", status(blocked))
out = tool_link(parent, blocked)
check(
    "it is told to unblock, not to wait",
    out.get("error") == started_text(blocked, "blocked"),
    f"got {out}",
)
check("no task_links row was written", not linked(parent, blocked))
check("the child is still blocked", status(blocked) == "blocked", status(blocked))

# --- 3. What keeps working ----------------------------------------------------
print("unaffected:")
queued = tool_create(title="Synthesize after the count")
check("a never-started card is ready", status(queued) == "ready", status(queued))
out = tool_link(parent, queued)
check("linking it is accepted", out.get("ok") is True, f"got {out}")
check("the link row is written", linked(parent, queued))
check("the card waits for the parent", status(queued) == "todo", status(queued))
gated = tool_create(title="Follow-up gated at creation", parents=[parent])
check("kanban_create with parents=[own card] links at creation", linked(parent, gated))
check("and the created card waits", status(gated) == "todo", status(gated))

if FAILURES:
    print(f"\nverify_kanban_link_running_guard: {len(FAILURES)} failure(s)")
    for failure in FAILURES:
        print(f"  - {failure}")
    sys.exit(1)
print("\nverify_kanban_link_running_guard: ok")
