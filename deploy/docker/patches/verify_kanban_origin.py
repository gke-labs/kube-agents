#!/usr/bin/env python3
"""Build gate for the card origin stamp (kanban_origin.py, apply_kanban_origin.py).

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after the applier. The
applier proves its locators matched; this drives the real patched board on a
throwaway ``HERMES_HOME``: ``create_task`` from a turn, a worker's child, an
auto-decomposed child, the ``kanban_create`` tool, the CLI create path with
and without a trusted writer's override, a dedupe hit, the dashboard's
``created_by``, and a ``--origin`` argument. The turn's session record is
stubbed (``_session_id``, ``_fetch_session_metadata``); nothing reaches the
network. On an unpatched tree every stamp check fails, because nothing writes
``kanban_task_origins``.

Usage::

    cd /opt/hermes && python3 verify_kanban_origin.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import closing
from pathlib import Path

FAILURES: list[str] = []

CHAT_SESSION = "20261103_101500_ab12cd34"
EVENT_SESSION = "k8s-evt-0a1b2c3d"
DEDUPE_KEY = "issue-resolve-example-fleet-42-20261103T10"


def check(label: str, condition: object, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


TMP = Path(tempfile.mkdtemp())
os.environ["HERMES_HOME"] = str(TMP)
os.environ["HERMES_KANBAN_DB"] = str(TMP / "kanban.db")
os.environ["HERMES_PROFILE"] = "default"
for name in ("HERMES_KANBAN_TASK", "HERMES_SESSION_ID", "KUBE_AGENTS_CARD_ORIGIN"):
    os.environ.pop(name, None)

from hermes_cli import kanban as KCLI  # noqa: E402
from hermes_cli import kanban_db as K  # noqa: E402
from hermes_cli import kanban_db_connect as KC  # noqa: E402
from hermes_cli import kanban_db_graph as KG  # noqa: E402
from hermes_cli import kanban_origin as KO  # noqa: E402

turn = {"session": ""}
records = {
    CHAT_SESSION: {"origin": "chat", "origin_platform": "slack"},
    EVENT_SESSION: {"origin": "event"},
}
KO._session_id = lambda: turn["session"]
KO._fetch_session_metadata = lambda session_id: records.get(session_id)


def origin(task_id: str) -> tuple:
    with closing(KC.connect()) as conn:
        return KO.origin_of(conn, task_id)


def create(**kwargs) -> str:
    kwargs.setdefault("title", "a card")
    kwargs.setdefault("assignee", "default")
    with closing(KC.connect()) as conn:
        return K.create_task(conn, **kwargs)


def slash(rest: str) -> str:
    out = str(KCLI.run_slash(rest))
    start, end = out.find("{"), out.rfind("}")
    if start == -1 or end <= start:
        return out
    return json.loads(out[start : end + 1]).get("id") or out


print("wiring")
check("create_task is wrapped", getattr(K.create_task, "__wrapped__", None) is not None)
check("inherit_creator_origin is wrapped", getattr(KG.inherit_creator_origin, "__wrapped__", None) is not None)
check("the CLI create command is wrapped", getattr(KCLI._HANDLERS["create"], "__wrapped__", None) is not None)

print("a turn's card")
turn["session"] = CHAT_SESSION
chat_card = create()
check("create_task stamps the turn's origin", origin(chat_card) == ("chat", "slack"), str(origin(chat_card)))
slash_card = slash("create --json --assignee default from-chat")
check("/kanban create stamps the turn's origin", origin(slash_card) == ("chat", "slack"), str(origin(slash_card)))

print("the kanban_create tool")
try:
    import tools.kanban_tools as KT

    answer = json.loads(KT._handle_create({"title": "tool card", "assignee": "default"}))
    tool_card = answer.get("task_id", "")
    check("kanban_create files", bool(answer.get("ok")) and bool(tool_card), str(answer))
    check("kanban_create stamps the turn's origin", origin(tool_card) == ("chat", "slack"), str(origin(tool_card)))
except Exception as exc:  # noqa: BLE001 — reported as a failure
    check("kanban_create stamps the turn's origin", False, repr(exc))

print("inheritance")
turn["session"] = EVENT_SESSION
worker_card = create(title="triage")
turn["session"] = CHAT_SESSION
child = create(title="child", creator_task_id=worker_card)
check("a worker's child inherits the worker's card", origin(child) == ("event", ""), str(origin(child)))
root = create(title="root", triage=True, creator_task_id=worker_card)
with closing(KC.connect()) as conn:
    kids = KG.decompose_triage_task(conn, root, root_assignee="default", children=[{"title": "k", "assignee": "default"}]) or []
check("decomposition made a child", len(kids) == 1, str(kids))
for kid in kids:
    check("a decomposed child inherits its root", origin(kid) == ("event", ""), str(origin(kid)))

print("first stamp wins")
squat = slash(f"create --json --assignee default --idempotency-key {DEDUPE_KEY} squat")
turn["session"] = ""
os.environ[KO.CARD_ORIGIN_ENV] = "issue"
again = slash(f"create --json --assignee default --idempotency-key {DEDUPE_KEY} gate")
check("the dedupe hit returns the first card", again == squat, f"{again} != {squat}")
check("the dedupe hit keeps the first stamp", origin(squat) == ("chat", "slack"), str(origin(squat)))

print("the trusted writers' override")
gate_card = slash("create --json --assignee default gate-card")
check("the CLI create path reads the override", origin(gate_card) == ("issue", ""), str(origin(gate_card)))
tool_path = create(title="not the CLI")
check("create_task outside the CLI ignores it", origin(tool_path) == ("other", ""), str(origin(tool_path)))
(TMP / ".env").write_text("KUBE_AGENTS_CARD_ORIGIN=issue\n")
refused = slash("create --json --assignee default from-dotenv")
check("an override the agent-writable .env names is refused", origin(refused) == ("other", ""), str(origin(refused)))
(TMP / ".env").unlink()
os.environ.pop(KO.CARD_ORIGIN_ENV)

print("no argument sets the origin")
with closing(KC.connect()) as conn:
    before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
answer = str(KCLI.run_slash("create --origin issue --assignee default x"))
with closing(KC.connect()) as conn:
    after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
check("the create parser rejects --origin", "--origin" in answer and before == after, answer[-200:])
dashboard_flag = slash("create --json --created-by dashboard --assignee default y")
check("--created-by dashboard is not console", origin(dashboard_flag) == ("other", ""), str(origin(dashboard_flag)))
dashboard = create(title="from the dashboard", created_by="dashboard")
check("the dashboard's create route is console", origin(dashboard) == ("console", ""), str(origin(dashboard)))

if FAILURES:
    print(f"\nkanban_origin verify: {len(FAILURES)} failure(s)")
    for failure in FAILURES:
        print(f"  - {failure}")
    sys.exit(1)
print("\nkanban_origin verify: ok")
