#!/usr/bin/env python3
"""Build gate for the platform self-copy refusal.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_kanban_self_copy.py``. The applier only proves the anchor matched.
This replays the #3164 shape through the real patched ``kanban_create`` on a
real board:

- a platform worker's ``kanban_create(assignee="platform")`` with no
  ``parents`` is refused, says to do the work on its own card without
  offering ``parents`` as a way round, and writes no card;
- the same worker's follow-up queued with ``parents=[<its card>]`` (platform
  SOUL section 0) is created;
- the same worker's card for a Cluster Agent is created;
- a card for ``platform`` filed outside any worker (a person, the chat
  session) is created;
- a title-cased ``Platform`` is refused like ``platform``;
- a dependency block on the creator doesn't release the platform follow-up
  to run alongside it (the scheduling repair skips that edge), and the
  creator can still complete with it queued.

Before the patch the first call creates a second platform card that runs
alongside the first.

Usage::

    cd /opt/hermes && python3 verify_kanban_self_copy.py
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
os.environ["HERMES_KANBAN_DB"] = str(DB)
for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_KEY"):
    os.environ.pop(var, None)

from hermes_cli import kanban_db as K  # noqa: E402
from hermes_cli import kanban_db_connect as KC  # noqa: E402
import tools.kanban_tools as kt  # noqa: E402

conn = KC.connect(DB)
CLUSTER = "cluster-kube-agents-evals-4-seeded-a-us-central1-a"


def create(**args):
    return json.loads(kt._handle_create(args))


def cards():
    return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


print("wiring:")
check("the create handler resolved the import", hasattr(kt, "_kanban_refuse_self_copy"))

print("the #3164 replay:")
parent = create(assignee="platform", title="Check checkout-gateway disruption budget and submit PR")
check("a card for platform filed outside any worker is created", parent.get("ok"), str(parent))
pid = parent["task_id"]
K.claim_task(conn, pid)
os.environ["HERMES_KANBAN_TASK"] = pid

before = cards()
copy = create(assignee="platform", title="Propose PDB manifest for checkout-gateway")
err = copy.get("error", "")
check("the platform worker's concurrent platform card is refused", not copy.get("ok") and err, str(copy))
check("the refusal says to do the work on its own card, with no parents hint",
      "Do this work on your own card" in err and "parents" not in err, err)
check("and no card was written", cards() == before, f"{before} -> {cards()}")

follow = create(assignee="platform", title="Open the follow-up issue", parents=[pid])
check("a follow-up queued with parents=[its card] is created", follow.get("ok"), str(follow))

cluster = create(assignee=CLUSTER, title="seeded-a")
check("a card for a Cluster Agent is created", cluster.get("ok"), str(cluster))

cased = create(assignee="Platform", title="Propose it again")
check("a title-cased `Platform` is refused too (create_task lower-cases it)",
      not cased.get("ok"), str(cased))

print("the follow-up stays a follow-up:")
run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (pid,)).fetchone()[0]
os.environ["HERMES_KANBAN_RUN_ID"] = str(run_id)
blocked = json.loads(kt._handle_block({"reason": "waiting on the follow-up", "kind": "dependency"}))
status = conn.execute("SELECT status FROM tasks WHERE id = ?", (follow["task_id"],)).fetchone()[0]
edge = conn.execute("SELECT COUNT(*) FROM task_links WHERE parent_id = ? AND child_id = ?",
                    (pid, follow["task_id"])).fetchone()[0]
check("a dependency block doesn't release the platform follow-up to run alongside",
      status == "todo" and edge == 1, f"follow-up status={status}, edge={edge}, block={blocked}")
conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (cluster["task_id"],))
conn.commit()
done = json.loads(kt._handle_complete({"summary": "proposed", "result": "PR #276 adds the PDB."}))
check("and the creator can still complete with its follow-up queued", done.get("ok"), str(done))
K.recompute_ready(conn)
status = conn.execute("SELECT status FROM tasks WHERE id = ?", (follow["task_id"],)).fetchone()[0]
check("after which the follow-up is released", status == "ready", f"follow-up status={status}")

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} check(s):")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("all checks passed")
