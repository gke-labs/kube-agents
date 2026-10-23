#!/usr/bin/env python3
"""Build gate for the verified-claims patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_kanban_verified_claims.py``. The applier only proves its anchors
matched. This drives the patched code the way a worker reaches it: a real
terminal result through ``finalize_foreground_result``, then a real
``kanban_complete`` on a real board, for the pilot's shape (a "verified" claim
with no command behind it), for a claim the card's own call backs, and for a
parent card that relays a claim the gate accepted on a card it created.

Usage::

    cd /opt/hermes && python3 verify_kanban_verified_claims.py
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
import tools.kanban_verified_claims as kvc  # noqa: E402
from tools.terminal_tool_result import finalize_foreground_result  # noqa: E402

COMMAND = "kubectl get nodes --context prod"
OUTPUT = "NAME     STATUS     ROLES\nnode-a   Ready      <none>\nnode-b   NotReady   <none>"
BACKED = {"claim": "node-b is NotReady.", "command": COMMAND, "excerpt": "node-b   NotReady"}
# The pilot's report: a verified label and a figure, with no command behind it.
PILOT = {"claim": "No mismatch with the metrics exceeds 30%.", "command": "", "excerpt": ""}

conn = KC.connect(DB)


def new_card(title):
    out = json.loads(kt._handle_create({"assignee": "platform", "title": title}))
    if not out.get("ok"):
        raise AssertionError(f"kanban_create failed: {out}")
    task_id = out["task_id"]
    K.claim_task(conn, task_id)
    run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]
    os.environ["HERMES_KANBAN_TASK"] = task_id
    os.environ["HERMES_KANBAN_RUN_ID"] = str(run_id)
    return task_id


def run_terminal(command, output, returncode=0):
    return finalize_foreground_result(
        command=command, result={"output": output, "returncode": returncode}, env=None,
        env_type="local", effective_task_id="default", task_id=None, session_id=None,
        session_key="default", workdir=None, command_cwd=None, approval_note=None,
    )


def card(task_id):
    return K.get_task(conn, task_id)


print("wiring:")
props = kt.KANBAN_COMPLETE_SCHEMA["parameters"]["properties"]
check("kanban_complete takes verified_claims", kvc.PARAM in props)
check("verified_claims is optional", kvc.PARAM not in kt.KANBAN_COMPLETE_SCHEMA["parameters"].get("required", []))
check("the handler resolved the gate", kt._apply_completion_rules is kvc.apply_completion_rules)

print("a backed claim:")
backed = new_card("Which nodes are not ready?")
run_terminal(COMMAND, OUTPUT)
check("the terminal call was recorded for the card", len(kvc._calls.get(backed, ())) == 1, str(kvc._calls))
out = json.loads(kt._handle_complete({
    "summary": "node-b is NotReady", "result": "**node-b is NotReady.**", kvc.PARAM: [BACKED],
}))
check("kanban_complete succeeds", out.get("ok"), str(out))
task = card(backed)
check("the card is done", task.status == "done", task.status)
check("the evidence is appended to the result", kvc.EVIDENCE_HEADING in (task.result or "") and COMMAND in task.result, task.result)
run = K.latest_run(conn, backed)
stored = (run.metadata or {}).get(kvc.METADATA_KEY) if run else None
check("the claim is stored with its exit code", bool(stored) and stored[0]["exit_code"] == 0, str(stored))

print("the pilot's shape:")
pilot = new_card("Review the fleet's cost and verify each claim")
first = json.loads(kt._handle_complete({
    "summary": "cost review", "result": "Verified: no mismatch exceeds 30%.", kvc.PARAM: [PILOT],
}))
check("a claim with no command is refused", not first.get("ok") and "has no command" in json.dumps(first), str(first))
check("the card is still running", card(pilot).status == "running", card(pilot).status)
second = json.loads(kt._handle_complete({
    "summary": "cost review", "result": "No mismatch exceeds 30%.", kvc.PARAM: [PILOT],
}))
check("the retry closes the card", second.get("ok"), str(second))
check("the dropped claim is not shown as verified", kvc.EVIDENCE_HEADING not in (card(pilot).result or ""), card(pilot).result)

print("a claim relayed from a card this card created:")
parent = new_card("Why does payments-api restart? Verify each claim.")
child = new_card("Read payments-api's last termination on seeded-a")
conn.execute(
    "CREATE TABLE IF NOT EXISTS kanban_worker_children ("
    " child_id TEXT PRIMARY KEY, creator_id TEXT NOT NULL, created_at INTEGER NOT NULL)"
)
conn.execute("INSERT OR IGNORE INTO kanban_worker_children VALUES (?, ?, 0)", (child, parent))
conn.commit()
run_terminal(COMMAND, OUTPUT)
out = json.loads(kt._handle_complete({"summary": "node-b", "result": "node-b is NotReady.", kvc.PARAM: [BACKED]}))
check("the child card completes with its claim", out.get("ok"), str(out))
os.environ["HERMES_KANBAN_TASK"] = parent
os.environ["HERMES_KANBAN_RUN_ID"] = str(K.latest_run(conn, parent).id)
check("the parent card ran no command itself", not kvc.card_terminal_calls(parent))
out = json.loads(kt._handle_complete({"summary": "node-b", "result": "**node-b is NotReady.**", kvc.PARAM: [BACKED]}))
check("the parent's relayed claim is accepted", out.get("ok"), str(out))
check("the evidence names the child card", f"on card {child}" in (card(parent).result or ""), card(parent).result)

if FAILURES:
    print(f"\nverify_kanban_verified_claims: {len(FAILURES)} check(s) FAILED")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("\nverify_kanban_verified_claims: all checks passed")
