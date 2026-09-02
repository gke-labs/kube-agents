#!/usr/bin/env python3
"""Build gate for the fan-out completion guard.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_kanban_children_settled.py``. The applier only proves the anchors
matched. This replays the #1010 incident shape (build ``2093054394793725952``,
card ``t_470a97c5``) through the *real patched* handlers against a real board:
a claimed delegation card, a worker env (``HERMES_KANBAN_TASK``), fan-out
children created with no parents, and a completion whose ``result`` is a
dispatch receipt. Before the patch that completion was accepted and the
receipt was delivered as the final answer; after it, the completion is
refused once with the instruction to wait and synthesize — and the retry, the
settled board, the continuation idiom, and a board without the attribution
table all still complete, because a guard that can wedge a card shut is a
worse bug than the one it fixes.

Usage::

    cd /opt/hermes && python3 verify_kanban_children_settled.py
"""

from __future__ import annotations

import json
import os
import sqlite3
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
os.environ.pop("HERMES_SESSION_KEY", None)

from hermes_cli import kanban_db as K  # noqa: E402
import tools.kanban_tools as kt  # noqa: E402
import tools.kanban_children_settled as kcs  # noqa: E402

conn = K.connect(DB)

RECEIPT = (
    "Fanned out cluster investigation tasks for `inference-server` to each "
    "cluster agent. Awaiting synthesis."
)


def tool_create(**args):
    out = json.loads(kt._handle_create({"assignee": "platform", **args}))
    if not out.get("ok"):
        raise AssertionError(f"kanban_create failed: {out}")
    return out


def status(task_id):
    row = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return row[0] if row else None


def attributed_to(task_id):
    # The attribution table is created by the first worker write, so a fresh
    # board legitimately has no table yet — exactly the fail-open state the
    # gate must treat as "no children".
    try:
        return [
            r[0]
            for r in conn.execute(
                f"SELECT child_id FROM {kcs.CHILDREN_TABLE} WHERE creator_id = ? "
                "ORDER BY child_id",
                (task_id,),
            ).fetchall()
        ]
    except sqlite3.OperationalError:
        return []


def tool_complete(result=RECEIPT):
    return kt._handle_complete({"summary": "status line", "result": result})


# --- 0. The trailer imports resolved ------------------------------------------
print("wiring:")
check(
    "the create handler resolved the attribution import",
    hasattr(kt, "_kanban_record_worker_child"),
    "the trailer import did not execute",
)
check(
    "the complete handler resolved the gate import",
    hasattr(kt, "_require_children_settled")
    and hasattr(kt, "_kanban_children_connect"),
    "the trailer import did not execute",
)

# --- 1. The orchestrator files the delegation card ---------------------------
print("attribution:")
parent = tool_create(title="Investigate inference-server capacity")["task_id"]
check("an orchestrator create is attributed to nobody", attributed_to(parent) == [])
check("the delegation card claims normally", K.claim_task(conn, parent) is not None)

# --- 2. The worker fans out ---------------------------------------------------
os.environ["HERMES_KANBAN_TASK"] = parent
kid_a = tool_create(title="Investigate on seeded-a")["task_id"]
kid_b = tool_create(title="Investigate on seeded-b")["task_id"]
check(
    "a worker's fan-out children are attributed to its card",
    attributed_to(parent) == sorted([kid_a, kid_b]),
    f"got {attributed_to(parent)}",
)

# --- 3. The incident replay ---------------------------------------------------
print("the #1010 replay:")
refused = tool_complete()
check("the receipt completion is refused", "refused" in refused)
check("the refusal names the live children", kid_a in refused and kid_b in refused)
check("the refusal says how to self-correct", "kanban_show" in refused)
check("the card is still open", status(parent) != "done", f"status={status(parent)}")
stored = conn.execute(
    "SELECT COALESCE(result, '') FROM tasks WHERE id = ?", (parent,)
).fetchone()[0]
check(
    "the refusal preserved the submitted result on the still-open card",
    stored == RECEIPT,
    f"result={stored!r}",
)
check("the refusal says the submission was saved", "saved on this card" in refused)

# --- 4. Never a wedge ----------------------------------------------------------
retry = tool_complete()
retry_ok = "refused" not in retry
check("the immediate retry is accepted (never wedge)", retry_ok, retry)
check("the retry closed the card", status(parent) == "done")

# --- 5. The settled board completes first time --------------------------------
print("clean paths:")
os.environ.pop("HERMES_KANBAN_TASK", None)
parent2 = tool_create(title="Second delegation")["task_id"]
K.claim_task(conn, parent2)
os.environ["HERMES_KANBAN_TASK"] = parent2
kid_c = tool_create(title="child that finishes")["task_id"]
conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (kid_c,))
conn.commit()
done = tool_complete(result="The pool is `pinned-pool`; scaling is HPA-capped.")
check("a completion over settled children is accepted", "refused" not in done, done)
check("the settled-path card closed", status(parent2) == "done")

# --- 6. The continuation idiom is exempt ---------------------------------------
os.environ.pop("HERMES_KANBAN_TASK", None)
parent3 = tool_create(title="Third delegation")["task_id"]
K.claim_task(conn, parent3)
os.environ["HERMES_KANBAN_TASK"] = parent3
followup = tool_create(title="follow-up, runs after me", parents=[parent3])["task_id"]
check("the follow-up is gated on its creator", status(followup) != "running")
cont = tool_complete(result="Done; queued the follow-up.")
check(
    "completing over a child gated on this card is accepted",
    "refused" not in cont,
    cont,
)
check("the continuation card closed", status(parent3) == "done")

# --- 7. Fail-open on a board without the table ---------------------------------
os.environ.pop("HERMES_KANBAN_TASK", None)
parent4 = tool_create(title="Fourth delegation")["task_id"]
K.claim_task(conn, parent4)
conn.execute(f"DROP TABLE {kcs.CHILDREN_TABLE}")
conn.commit()
os.environ["HERMES_KANBAN_TASK"] = parent4
bare = tool_complete(result="An answer.")
check("a board without the attribution table completes", "refused" not in bare, bare)
check("the fail-open card closed", status(parent4) == "done")

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} check(s):")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("all checks passed")
