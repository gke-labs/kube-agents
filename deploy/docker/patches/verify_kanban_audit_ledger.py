#!/usr/bin/env python3
"""Build gate for the audit-ledger completion guard.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_kanban_audit_ledger.py``. The applier only proves the anchor matched.
This replays the #2856 shape (periodic build ``2109267671341076480``, card
``t_a65e3664``) through the *real patched* handler, a real board and a real
Hermes session store: a worker ran ``audit_report.py start``, its ``finish``
was rejected, and it closed the card claiming the ledger was published.
Before the patch that completion was accepted. After it, the completion is
refused once with the saved result on the card, and the retry closes the card
marked not published. A run whose ``finish`` published closes with the ledger
URL in its result, and a card that never ran ``start`` is untouched.

Usage::

    cd /opt/hermes && python3 verify_kanban_audit_ledger.py
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
HOME = TMP / "home"
HOME.mkdir()
DB = TMP / "kanban.db"
os.environ["HERMES_HOME"] = str(HOME)
os.environ["HERMES_KANBAN_DB"] = str(DB)
for name in ("HERMES_KANBAN_TASK", "HERMES_SESSION_ID", "HERMES_SESSION_KEY"):
    os.environ.pop(name, None)

from hermes_cli import kanban_db as K  # noqa: E402
from hermes_cli import kanban_db_connect as KC  # noqa: E402
from hermes_state import SessionDB  # noqa: E402
import tools.kanban_tools as kt  # noqa: E402
import tools.kanban_audit_ledger as kal  # noqa: E402

conn = KC.connect(DB)
sessions = SessionDB(db_path=HOME / kal.STATE_DB_NAME)

START = "python3 /opt/data/profiles/platform/skills/fleet-audit/scripts/audit_report.py start --audit compliance-audit"
FINISH = (
    "python3 /opt/data/profiles/platform/skills/fleet-audit/scripts/audit_report.py finish "
    "--audit compliance-audit --findings-file /opt/data/scratch/findings_compliance-audit.json"
)
LEDGER = "https://github.com/acme/infra/issues/248"
STARTED = json.dumps({
    "issue": None, "repo": "acme/infra", "mode": "content",
    "workspace": "/opt/data/gitops/compliance-audit/acme__infra",
    "findings_path": "/opt/data/scratch/findings_compliance-audit.json",
})
CLAIM = "The detailed ledger and automated fix PRs have been published."
_calls = [0]


def tool_round(session_id: str, command: str, output: str, exit_code: int) -> None:
    _calls[0] += 1
    call_id = f"call_{_calls[0]}"
    sessions.append_message(
        session_id, "assistant", content="",
        tool_calls=[{"id": call_id, "type": "function",
                     "function": {"name": "terminal", "arguments": json.dumps({"command": command})}}],
    )
    sessions.append_message(
        session_id, "tool", content=json.dumps({"output": output, "exit_code": exit_code, "error": None}),
        tool_call_id=call_id, tool_name="terminal",
    )


def worker_card(title: str, session_id: str) -> str:
    os.environ.pop("HERMES_KANBAN_TASK", None)
    out = json.loads(kt._handle_create({"assignee": "platform", "title": title}))
    if not out.get("ok"):
        raise AssertionError(f"kanban_create failed: {out}")
    tid = out["task_id"]
    K.claim_task(conn, tid)
    sessions.create_session(session_id, "kanban")
    os.environ["HERMES_KANBAN_TASK"] = tid
    os.environ["HERMES_SESSION_ID"] = session_id
    return tid


def card(tid: str):
    return conn.execute("SELECT status, result FROM tasks WHERE id = ?", (tid,)).fetchone()


def complete(result: str) -> dict:
    return json.loads(kt._handle_complete({"summary": "status line", "result": result}))


print("wiring:")
check(
    "the complete handler resolved the gate import",
    hasattr(kt, "_require_audit_ledger") and hasattr(kt, "_kanban_audit_connect"),
    "the trailer import did not execute",
)

print("a rejected finish, then done:")
tid = worker_card("Run the compliance audit", "sess-rejected")
tool_round("sess-rejected", START, STARTED, 0)
tool_round("sess-rejected", FINISH, "FINDINGS REJECTED: scope: required object", 2)
first = complete(CLAIM)
check("the completion is refused", not first.get("ok"), str(first))
check("the refusal names the next step", "finish" in json.dumps(first) and "kanban_block" in json.dumps(first), str(first))
row = card(tid)
check(
    "the card stays open with the result saved, marked not published",
    row is not None and row[0] == "running" and str(row[1]).startswith("⚠️ **Not published:**") and str(row[1]).endswith(CLAIM),
    repr(row),
)
second = complete(CLAIM)
check("the retry closes the card", second.get("ok"), str(second))
row = card(tid)
check(
    "the delivered result says not published",
    row is not None and row[0] == "done" and str(row[1]).startswith("⚠️ **Not published:**") and str(row[1]).endswith(CLAIM),
    repr(row),
)

print("a published run:")
tid = worker_card("Run the compliance audit again", "sess-published")
tool_round("sess-published", START, STARTED, 0)
tool_round("sess-published", FINISH, json.dumps({"status": "OPENED", "issue_url": LEDGER, "new": 11}), 0)
out = complete("Audit complete, 11 findings.")
row = card(tid)
check("the completion is accepted", out.get("ok"), str(out))
check("the ledger URL is appended", row is not None and str(row[1]).endswith("Ledger: " + LEDGER), repr(row))

print("a card that is not an audit run:")
tid = worker_card("List the platform cron jobs", "sess-other")
tool_round("sess-other", "hermes cron list", "fleet-audit  enabled", 0)
out = complete("Five jobs are enabled.")
row = card(tid)
check("the completion is accepted unchanged", out.get("ok") and row is not None and row[1] == "Five jobs are enabled.", repr(row))

if FAILURES:
    print(f"verify_kanban_audit_ledger: {len(FAILURES)} failure(s)")
    sys.exit(1)
print("verify_kanban_audit_ledger: ok")
