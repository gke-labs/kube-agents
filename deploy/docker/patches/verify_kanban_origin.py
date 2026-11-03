#!/usr/bin/env python3
"""Build gate for the card origin stamp (kanban_origin.py, apply_kanban_origin.py).

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after the applier. The
applier proves its locators matched; this drives the real patched board on a
throwaway ``HERMES_HOME``: ``create_task`` from a turn, a worker's child, an
auto-decomposed child, the ``kanban_create`` tool, a trusted writer's stamp
ticket (once, then spent) and a planted one, a dedupe hit, the dashboard's
``created_by``, and a ``--origin`` argument. The turn's session record and the
Session KV server's ticket routes are stubbed (``_session_id``,
``_fetch_session_metadata``, ``_session_kv``); nothing reaches the network. On an unpatched tree every stamp check fails, because nothing writes
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
for name in ("HERMES_KANBAN_TASK", "HERMES_SESSION_ID", "KUBE_AGENTS_CARD_STAMP_TICKET"):
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
tickets: dict = {}


def ticket_server(path: str, payload: dict | None = None) -> dict:
    """The Session KV server's mint and redeem routes, in memory: a ticket redeems once."""
    if path == KO.STAMP_TICKETS_PATH:
        ticket = f"{len(tickets) + 1:032x}"
        tickets[ticket] = dict(payload or {})
        return {"ticket": ticket}
    for ticket, body in list(tickets.items()):
        if path == KO.STAMP_TICKET_REDEEM_PATH.format(ticket=ticket) and body is not None:
            tickets[ticket] = None
            return body
    raise LookupError("HTTP 404")


KO._session_kv = ticket_server


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
with KO.writer_ticket("issue"):
    again = slash(f"create --json --assignee default --idempotency-key {DEDUPE_KEY} gate")
check("the dedupe hit returns the first card", again == squat, f"{again} != {squat}")
check("the dedupe hit keeps the first stamp", origin(squat) == ("chat", "slack"), str(origin(squat)))

print("the trusted writers' stamp ticket")
with KO.writer_ticket("issue"):
    gate_card = slash("create --json --assignee default gate-card")
    check("the ticket is in the create's environment", bool(os.environ.get(KO.STAMP_TICKET_ENV)))
check("a minted ticket stamps its origin", origin(gate_card) == ("issue", ""), str(origin(gate_card)))
check("the ticket does not outlive the call", KO.STAMP_TICKET_ENV not in os.environ)
os.environ[KO.STAMP_TICKET_ENV] = KO.mint_ticket("drift")
first = slash("create --json --assignee default first")
turn["session"] = CHAT_SESSION
second = create(title="second")
check("the ticket stamps the first insert", origin(first) == ("drift", ""), str(origin(first)))
check("a spent ticket is the turn's origin", origin(second) == ("chat", "slack"), str(origin(second)))
os.environ[KO.STAMP_TICKET_ENV] = "issue"
planted = slash("create --json --assignee default planted")
check("a planted ticket value is the turn's origin", origin(planted) == ("chat", "slack"), str(origin(planted)))
os.environ.pop(KO.STAMP_TICKET_ENV)
turn["session"] = ""

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
