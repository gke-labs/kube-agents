#!/usr/bin/env python3
"""Build gate for the event-triage chat-routing patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after the applier.
The applier only proves the anchor matched. This drives the *real patched*
tool handler — the same ``_handle_create`` the front door's ``kanban_create``
reaches — with the session context an event-triage turn actually carries, and
asserts the subscription row it writes is addressed somewhere a notifier can
deliver.

The failure being gated is #630: with ``platform='api_server'`` and the session
id in ``chat_id``, the row is well-formed, the notifier finds it every tick,
and no such chat platform exists — so the root-cause analysis is written to a
card and never reaches the human who raised the alert.

Every fall-through case is checked too, because ``kanban_tools`` runs in CLI
workers with no routing database and the patch must be invisible to them.

Usage::

    cd /opt/hermes && python3 verify_kanban_event_routing.py
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
KV = TMP / "session_kv.db"

os.environ["HERMES_KANBAN_DB"] = str(DB)
os.environ["SESSION_KV_DB_PATH"] = str(KV)
os.environ.pop("HERMES_KANBAN_TASK", None)
os.environ.pop("HERMES_SESSION_KEY", None)

SPACE = "spaces/0EXAMPLE"
THREAD = "spaces/0EXAMPLE/threads/ALERT1"

# The routing table exactly as session_kv_server.py declares it.
with sqlite3.connect(KV) as kv:
    kv.execute(
        """
        CREATE TABLE IF NOT EXISTS session_metadata (
            session_id TEXT PRIMARY KEY,
            metadata TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    kv.execute(
        "INSERT INTO session_metadata (session_id, metadata) VALUES (?, ?)",
        (
            "k8s-evt-routed",
            json.dumps(
                {
                    "platform": "google_chat",
                    "chat_id": SPACE,
                    "thread_id": THREAD,
                }
            ),
        ),
    )
    # The shape event sessions carried before the watcher recorded the real
    # platform. Substituting it would trade one undeliverable address for
    # another.
    kv.execute(
        "INSERT INTO session_metadata (session_id, metadata) VALUES (?, ?)",
        ("k8s-evt-prefix", json.dumps({"platform": "k8s-watcher"})),
    )
    # A row keyed by a real chat channel, to prove a chat session is never
    # re-addressed even when a lookup would succeed.
    kv.execute(
        "INSERT INTO session_metadata (session_id, metadata) VALUES (?, ?)",
        (SPACE, json.dumps({"platform": "slack", "chat_id": "C0WRONG"})),
    )

from hermes_cli import kanban_db as K  # noqa: E402
import tools.kanban_tools as kt  # noqa: E402

conn = K.connect(DB)


def subs(task_id):
    return conn.execute(
        "SELECT * FROM kanban_notify_subs WHERE task_id = ?", (task_id,)
    ).fetchall()


def session(platform, chat_id, thread_id=""):
    """Bind the session context an incoming turn would have."""
    os.environ["HERMES_SESSION_PLATFORM"] = platform
    os.environ["HERMES_SESSION_CHAT_ID"] = chat_id
    os.environ["HERMES_SESSION_THREAD_ID"] = thread_id


def tool_create(**args):
    out = json.loads(kt._handle_create({"assignee": "platform", **args}))
    if not out.get("ok"):
        raise AssertionError(f"kanban_create failed: {out}")
    return out["task_id"]


check(
    "the create handler resolved the routing import",
    hasattr(kt, "_kanban_event_route"),
    "the trailer import did not execute",
)

# --- #630, replayed ----------------------------------------------------------
print("event-triage card:")
session("api_server", "k8s-evt-routed")
routed = tool_create(title="Triage default/Pod/web-7 (Failed)")
rows = subs(routed)
check("the event-triage card is subscribed", len(rows) == 1)
if rows:
    row = rows[0]
    check(
        "the subscription is addressed to the alert's chat thread",
        row["platform"] == "google_chat"
        and row["chat_id"] == SPACE
        and row["thread_id"] == THREAD,
        f"platform={row['platform']} chat_id={row['chat_id']} "
        f"thread_id={row['thread_id']}",
    )
    check(
        "and not to the api_server origin no notifier can deliver to",
        row["platform"] != "api_server" and not row["chat_id"].startswith("k8s-evt-"),
    )

# --- Fall-through: everything the patch must leave alone ----------------------
print("sessions the patch must not touch:")

session("api_server", "k8s-evt-unknown")
unknown = tool_create(title="event with no recorded route")
rows = subs(unknown)
check(
    "an event session with no recorded route keeps today's behaviour",
    len(rows) == 1 and rows[0]["platform"] == "api_server",
    f"{[dict(r) for r in rows]}",
)

session("api_server", "k8s-evt-prefix")
prefixed = tool_create(title="event whose stored route is also non-chat")
rows = subs(prefixed)
check(
    "a stored route that is itself non-chat is not substituted",
    len(rows) == 1 and rows[0]["platform"] == "api_server",
    f"{[dict(r) for r in rows]}",
)

session("google_chat", SPACE, THREAD)
chat = tool_create(title="a card filed from chat")
rows = subs(chat)
check(
    "a genuine chat session is never re-addressed",
    len(rows) == 1
    and rows[0]["platform"] == "google_chat"
    and rows[0]["chat_id"] == SPACE
    and rows[0]["thread_id"] == THREAD,
    f"{[dict(r) for r in rows]}",
)

# The CLI-worker case: no routing database exists at all. The create must
# succeed and the row must be exactly what it is today.
os.environ["SESSION_KV_DB_PATH"] = str(TMP / "absent.db")
session("api_server", "k8s-evt-routed")
absent = tool_create(title="event with no routing database")
rows = subs(absent)
check(
    "a missing routing database falls through instead of failing the create",
    len(rows) == 1 and rows[0]["platform"] == "api_server",
    f"{[dict(r) for r in rows]}",
)

# An unreadable/corrupt database is the same contract: fail open, never raise.
corrupt = TMP / "corrupt.db"
corrupt.write_bytes(b"this is not a sqlite database")
os.environ["SESSION_KV_DB_PATH"] = str(corrupt)
session("api_server", "k8s-evt-routed")
broken = tool_create(title="event with a corrupt routing database")
rows = subs(broken)
check(
    "a corrupt routing database falls through instead of failing the create",
    len(rows) == 1 and rows[0]["platform"] == "api_server",
    f"{[dict(r) for r in rows]}",
)

print()
if FAILURES:
    print(f"verify_kanban_event_routing: {len(FAILURES)} FAILED")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("verify_kanban_event_routing: all checks passed")
