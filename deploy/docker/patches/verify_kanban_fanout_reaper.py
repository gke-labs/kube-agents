#!/usr/bin/env python3
"""Build gate for the fan-out child reaper.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_kanban_fanout_reaper.py``. The applier only proves the anchor matched.
This replays the #2978 shape through the real patched dispatcher reclaim phase
on a real board, with real worker processes:

- a fan-out child (a worker's ``kanban_create``) whose active run started past
  the bound, with a fresh heartbeat, is stopped and settled with a result that
  says it didn't finish in time on its cluster;
- a card a person filed and a single delegation to one Cluster Agent, each
  running just as long, are left alone and their workers keep running (a
  follow-up queued with ``parents`` can't run while its creator holds its card,
  so the host tests cover that exclusion);
- a fan-out child still inside the bound is left alone.

Before the patch all three stay running: the child's heartbeat keeps the stale
reclaim off it, and nothing else bounds a card without ``max_runtime_seconds``.

Usage::

    cd /opt/hermes && python3 verify_kanban_fanout_reaper.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
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
os.environ.pop("HERMES_KANBAN_TASK", None)
os.environ.pop("HERMES_SESSION_KEY", None)

from hermes_cli import kanban_db as K  # noqa: E402
from hermes_cli import kanban_db_connect as KC  # noqa: E402
from hermes_cli import kanban_db_dispatch as KD  # noqa: E402
import tools.kanban_tools as kt  # noqa: E402
import tools.kanban_fanout_reaper as reaper  # noqa: E402

conn = KC.connect(DB)
WORKERS: list[subprocess.Popen] = []


def cluster(name):
    return f"cluster-kube-agents-evals-13-{name}-us-central1-a"


def tool_create(**args):
    out = json.loads(kt._handle_create({"assignee": "platform", **args}))
    if not out.get("ok"):
        raise AssertionError(f"kanban_create failed: {out}")
    return out["task_id"]


def run_as_worker(task_id: str, started_ago: int) -> subprocess.Popen:
    """Claim ``task_id`` with a live worker process whose run started
    ``started_ago`` seconds back and which heartbeat just now."""
    K.claim_task(conn, task_id)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    WORKERS.append(proc)
    now = int(time.time())
    conn.execute(
        "UPDATE tasks SET worker_pid = ?, started_at = ?, last_heartbeat_at = ? WHERE id = ?",
        (proc.pid, now - started_ago, now, task_id),
    )
    conn.execute(
        "UPDATE task_runs SET started_at = ? WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
        (now - started_ago, task_id),
    )
    conn.commit()
    return proc


def card(task_id):
    row = conn.execute("SELECT status, COALESCE(result, '') FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return (row[0], row[1]) if row else (None, "")


def gone(proc: subprocess.Popen) -> bool:
    try:
        proc.wait(timeout=10)
        return True
    except subprocess.TimeoutExpired:
        return False


print("wiring:")
check(
    "the dispatcher resolved the reaper import",
    hasattr(KD, "_kanban_reap_overdue_fanout"),
    "the trailer import did not execute",
)

over = reaper.FANOUT_CHILD_MAX_SECONDS + 600

# The coordinator: a card the front door filed, claimed by a platform worker.
parent = tool_create(title="Find checkout-gateway and propose a PDB")
K.claim_task(conn, parent)
os.environ["HERMES_KANBAN_TASK"] = parent
stuck_child = tool_create(title="seeded-b", assignee=cluster("seeded-b"))
young_child = tool_create(title="seeded-c", assignee=cluster("seeded-c"))
os.environ.pop("HERMES_KANBAN_TASK", None)
# A card a person filed (no worker created it).
person_card = tool_create(title="A long investigation a person asked for")
# Another worker's single delegation, and a follow-up queued after its creator.
solo_parent = tool_create(title="Deep RCA on seeded-d")
K.claim_task(conn, solo_parent)
os.environ["HERMES_KANBAN_TASK"] = solo_parent
solo_child = tool_create(title="seeded-d", assignee=cluster("seeded-d"))
os.environ.pop("HERMES_KANBAN_TASK", None)

stuck_proc = run_as_worker(stuck_child, over)
young_proc = run_as_worker(young_child, 300)
person_proc = run_as_worker(person_card, over)
solo_proc = run_as_worker(solo_child, over)

print("the #2978 replay:")
try:
    KD._run_reclaim_phase(
        conn, KD.DispatchResult(),
        stale_timeout_seconds=1800, failure_limit=3, reconcile_orphans=False,
    )
    status, result = card(stuck_child)
    check("an overdue fan-out child with a fresh heartbeat is settled", status == "done", f"status={status}")
    check("its result says it didn't finish in time on its cluster",
          "Did not finish within 30 minutes on seeded-b" in result, result)
    check("its worker was stopped", gone(stuck_proc))
    reaped = conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'fanout_reaped'", (stuck_child,)
    ).fetchone()[0]
    check("a fanout_reaped event tells it apart from a real finish", reaped == 1, f"events={reaped}")
    status, _ = card(solo_child)
    check("a single delegation running as long is left alone", status == "running", f"status={status}")
    check("and its worker keeps running", solo_proc.poll() is None)
    status, _ = card(person_card)
    check("a person's card running as long is left alone", status == "running", f"status={status}")
    check("and its worker keeps running", person_proc.poll() is None)
    status, _ = card(young_child)
    check("a fan-out child inside the bound is left alone", status == "running", f"status={status}")
    check("and its worker keeps running", young_proc.poll() is None)
finally:
    for proc in WORKERS:
        if proc.poll() is None:
            proc.kill()

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} check(s):")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("all checks passed")
