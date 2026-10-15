#!/usr/bin/env python3
"""Build gate for the ended-run patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_kanban_ended_run.py``. The applier only proves the anchors matched.
This replays the #2947 rehearsal shape on a real board, through the real
patched ``block_task`` and tool handlers, with real worker processes:

- an operator's block (no ``expected_run_id``, outside any worker) on a
  running card stops its live worker, records a ``block_worker_termination``
  event and leaves a comment saying the run was cut short;
- a worker's own successful ``kanban_block`` or ``kanban_complete`` leaves
  nothing latched (the refusal lookup runs only on a refusal);
- the card's own worker blocking through ``kanban_block``, or with no run id
  as the rate-limit exit does, ends its run and stops nothing;
- a run that ends between the handler's first check and its write is still
  told the run is over by the board's refusal, not shown the earlier failure;
- the stopped run's ``kanban_complete``, on a card whose
  ``last_failure_error`` holds an earlier run's "protocol violation" text,
  gets the terminal answer and none of that text, and a second call (and its
  ``kanban_block``) gets the same answer without opening the board, even
  with the card put back as if the run were current.

Before the patch the blocked card's worker keeps running, and its
``kanban_complete`` is told "If the prior run already did the work, verify it
and report the result via kanban_complete".

Usage::

    cd /opt/hermes && python3 verify_kanban_ended_run.py
"""

from __future__ import annotations

import json
import os
import subprocess
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
import tools.kanban_ended_run as ker  # noqa: E402

conn = KC.connect(DB)
WORKERS: list[subprocess.Popen] = []

# Run 1's failure on the rehearsal card t_43bd650c, as run 2 was shown it.
PRIOR_FAILURE = (
    "worker exited cleanly (rc=0) without calling kanban_complete or "
    "kanban_block — protocol violation. If the prior run already did the "
    "work, verify it and report the result via kanban_complete; otherwise "
    "do the work and then call kanban_complete."
)


def tool_create(title):
    out = json.loads(kt._handle_create({"assignee": "platform", "title": title}))
    if not out.get("ok"):
        raise AssertionError(f"kanban_create failed: {out}")
    return out["task_id"]


def start_run(task_id):
    """Claim ``task_id`` with a live stand-in worker process; return
    ``(run_id, proc)``."""
    K.claim_task(conn, task_id)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    WORKERS.append(proc)
    conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (proc.pid, task_id))
    conn.commit()
    run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]
    return run_id, proc


def as_worker(task_id, run_id):
    os.environ["HERMES_KANBAN_TASK"] = task_id
    os.environ["HERMES_KANBAN_RUN_ID"] = str(run_id)


def as_operator():
    os.environ.pop("HERMES_KANBAN_TASK", None)
    os.environ.pop("HERMES_KANBAN_RUN_ID", None)


def status(task_id):
    return conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]


def gone(proc):
    try:
        proc.wait(timeout=15)
        return True
    except subprocess.TimeoutExpired:
        return False


def events(task_id, kind):
    return conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = ?", (task_id, kind)
    ).fetchone()[0]


def comments(task_id):
    return [r[0] for r in conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ?", (task_id,)).fetchall()]


print("wiring:")
check("block_task is the wrapper", getattr(K.block_task, "__kanban_ended_run__", False))
check("the tool handlers resolved the imports",
      all(hasattr(kt, n) for n in ("_kanban_refuse_ended_run", "_kanban_ended_run_refusal",
                                   "_kanban_ended_run_connect", "_kanban_current_run_failure")))

try:
    print("an operator's block on a running card:")
    rehearsal = tool_create("Find what is draining the checkout budget")
    run_id, proc = start_run(rehearsal)
    conn.execute("UPDATE tasks SET last_failure_error = ? WHERE id = ?", (PRIOR_FAILURE, rehearsal))
    conn.commit()
    as_operator()
    check("the block lands", K.block_task(conn, rehearsal, reason="operator: pausing this"))
    check("the card is blocked", status(rehearsal) == "blocked", status(rehearsal))
    check("its worker was stopped", gone(proc))
    check("a block_worker_termination event records it",
          events(rehearsal, "block_worker_termination") == 1)
    said = comments(rehearsal)
    check("a comment says the run was cut short",
          any("was stopped" in c and "cut short" in c for c in said), str(said))

    print("the card's own worker blocking:")
    own = tool_create("Ask which namespace to look in")
    own_run, own_proc = start_run(own)
    as_worker(own, own_run)
    out = json.loads(kt._handle_block({"reason": "Which namespace should I check?"}))
    check("kanban_block succeeds", out.get("ok"), str(out))
    check("the card is blocked", status(own) == "blocked", status(own))
    check("its worker was not stopped", own_proc.poll() is None)
    check("no termination is recorded", events(own, "block_worker_termination") == 0)
    check("and the run that blocked itself is not latched as over", ker._LATCHED is None, str(ker._LATCHED))

    print("a worker completing its card:")
    done = tool_create("Report the node count")
    done_run, _ = start_run(done)
    as_worker(done, done_run)
    out = json.loads(kt._handle_complete({"summary": "3 nodes", "result": "The cluster has 3 nodes."}))
    check("kanban_complete succeeds", out.get("ok"), str(out))
    check("and the run that finished is not latched as over", ker._LATCHED is None, str(ker._LATCHED))

    print("the card's own worker blocking without a run id (the rate-limit exit):")
    own2 = tool_create("Hit the provider's rate limit")
    own2_run, own2_proc = start_run(own2)
    os.environ["HERMES_KANBAN_TASK"] = own2
    os.environ.pop("HERMES_KANBAN_RUN_ID", None)
    check("the block lands", K.block_task(conn, own2, reason="429", kind="transient"))
    check("its worker was not stopped", own2_proc.poll() is None)

    print("a run that ends between the first check and the write:")
    raced = tool_create("Summarize the incident")
    raced_run, _ = start_run(raced)
    conn.execute("UPDATE tasks SET last_failure_error = ? WHERE id = ?", (PRIOR_FAILURE, raced))
    conn.commit()
    gate = kt._kanban_refuse_ended_run
    kt._kanban_refuse_ended_run = lambda *a: None
    as_operator()
    K.block_task(conn, raced, reason="operator: pausing this")
    as_worker(raced, raced_run)
    out = json.loads(kt._handle_complete({"summary": "done", "result": "Summary."}))
    err = out.get("error", "")
    check("the board's refusal says the run is over", "no longer yours to finish" in err, err)
    check("and none of the earlier run's failure", "protocol violation" not in err, err)
    kt._kanban_refuse_ended_run = gate
    ker._LATCHED = None

    print("the stopped run's handoffs:")
    as_worker(rehearsal, run_id)
    out = json.loads(kt._handle_complete({"summary": "done", "result": "The budget is fine."}))
    err = out.get("error", "")
    check("kanban_complete is refused", not out.get("ok") and err, str(out))
    check("with the terminal answer", "no longer yours to finish" in err and "Stop now" in err, err)
    check("and none of the earlier run's failure", "protocol violation" not in err
          and "verify it and report" not in err, err)
    check("the card is still blocked", status(rehearsal) == "blocked", status(rehearsal))

    def no_board():
        raise AssertionError("opened the board after the run was known to be over")

    kt._kanban_ended_run_connect = no_board
    # Put the card back as if the run were current: only the latch can still
    # refuse now, and completing would be the wrong answer.
    conn.execute("UPDATE tasks SET status = 'running', current_run_id = ? WHERE id = ?",
                 (run_id, rehearsal))
    conn.commit()
    again = json.loads(kt._handle_complete({"summary": "done", "result": "The budget is fine."}))
    check("a second kanban_complete gets the same answer without the board",
          again.get("error") == err, str(again))
    block = json.loads(kt._handle_block({"reason": "Can't finish"}))
    check("so does kanban_block", block.get("error") == err, str(block))
    check("and the card was left alone", status(rehearsal) == "running", status(rehearsal))
finally:
    for p in WORKERS:
        if p.poll() is None:
            p.kill()

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} check(s):")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("all checks passed")
