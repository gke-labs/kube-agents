#!/usr/bin/env python3
"""Build gate for the user-card priority patch (kanban_priority.py).

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_kanban_priority.py``. The applier only proves its anchors matched. This
drives the real patched code:

  A. ``_handle_create``, the handler the front door's ``kanban_create``
     reaches, under the session contexts real turns carry: a chat user's card
     is stamped user-class, an event-triage card (``api_server``,
     ``k8s-evt-...``) and a cron relay card stay background, a model-supplied
     priority or ``session_id`` cannot promote triage, and a dispatcher
     worker's child inherits its parent's class.
  B. ``kanban_create``'s return flags a queued card with counts only, and
     says nothing when a slot is free.
  C. The notifier claims ``queued`` without waking anyone, formats it as the
     agreed sentence, and ``kanban_progress_lines`` posts it bare and then
     rolls the worker's first note into the same message.
  D. The dispatcher watcher resolved its import and reads a saturated tick as
     saturation, with the counts in the log line, and leaves upstream's stuck
     warning in place for everything else.

The dispatcher half (the reserved slot, the saturation record, the queued
event) is gated by section G of ``verify_kanban_scheduling.py``.

Usage::

    cd /opt/hermes && python3 verify_kanban_priority.py
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

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
os.environ["SESSION_KV_DB_PATH"] = str(TMP / "absent-session-kv.db")
for name in ("HERMES_KANBAN_TASK", "HERMES_SESSION_KEY", "HERMES_SESSION_ID", "KAGE_SLACK_UX"):
    os.environ.pop(name, None)

from hermes_cli import kanban_db as K  # noqa: E402
from hermes_cli import kanban_db_connect as KC  # noqa: E402
from hermes_cli import kanban_priority as KP  # noqa: E402
import tools.kanban_tools as kt  # noqa: E402

conn = KC.connect(DB)


def session(platform: str, chat_id: str, thread_id: str = "") -> None:
    """Bind the session context an incoming turn would have."""
    os.environ["HERMES_SESSION_PLATFORM"] = platform
    os.environ["HERMES_SESSION_CHAT_ID"] = chat_id
    os.environ["HERMES_SESSION_THREAD_ID"] = thread_id


def no_session() -> None:
    for name in ("HERMES_SESSION_PLATFORM", "HERMES_SESSION_CHAT_ID", "HERMES_SESSION_THREAD_ID"):
        os.environ.pop(name, None)


def tool_create(**args) -> dict:
    out = json.loads(kt._handle_create({"assignee": "platform", **args}))
    if not out.get("ok"):
        raise AssertionError(f"kanban_create failed: {out}")
    return out


def priority(task_id: str) -> int:
    return int(K.get_task(conn, task_id).priority or 0)


print("wiring:")
check(
    "the create handler resolved the priority import",
    hasattr(kt, "_kanban_stamp_priority") and hasattr(kt, "_kanban_queue_fields"),
    "the trailer import did not execute",
)

# --- A. Classification at create time -----------------------------------------
print("classification:")
# No cap for section A, so no create returns a queue note here.
KP._configured_cap = lambda: None

session("slack", "C0EXAMPLE", "1700000000.000100")
chat = tool_create(title="Why is web-7 failing?")["task_id"]
check("a Slack user's card is user-class", priority(chat) == KP.USER_PRIORITY, f"priority={priority(chat)}")

session("google_chat", "spaces/0EXAMPLE", "spaces/0EXAMPLE/threads/T1")
gchat = tool_create(title="Scale the web pool", priority=150)["task_id"]
check(
    "a user's higher request is kept, not lowered to the floor",
    priority(gchat) == 150,
    f"priority={priority(gchat)}",
)

session("api_server", "k8s-evt-0a1b2c3d")
triage = tool_create(title="Triage default/Pod/web-7 (CrashLoopBackOff) on dev")["task_id"]
check("an event-triage card is background", priority(triage) == 0, f"priority={priority(triage)}")
check(
    "the triage card carries the event session it was classified by",
    K.get_task(conn, triage).session_id == "k8s-evt-0a1b2c3d",
    f"session_id={K.get_task(conn, triage).session_id}",
)

promoted = tool_create(title="Triage with a promoted priority", priority=500)["task_id"]
check(
    "a model cannot promote triage past the user floor",
    priority(promoted) == KP.USER_PRIORITY - 1,
    f"priority={priority(promoted)}",
)

# The session_id a model passes is not trusted context: a triage turn naming a
# user-looking session still files a background card.
spoofed = tool_create(
    title="Triage claiming a chat session", priority=500,
    session_id="20261008_101500_ab12cd34",
)["task_id"]
check(
    "a triage turn cannot pass itself off as a user with args.session_id",
    priority(spoofed) == KP.USER_PRIORITY - 1,
    f"priority={priority(spoofed)}",
)

session("api_server", "cron-platform-stall-watch-20261008")
relay = tool_create(title="Report relay")["task_id"]
check("a cron relay card is background", priority(relay) == 0, f"priority={priority(relay)}")

session("api_server", "20261008_101500_ab12cd34")
door = tool_create(title="A question through the inject door")["task_id"]
check("an API door card is user-class", priority(door) == KP.USER_PRIORITY, f"priority={priority(door)}")

no_session()
cli = tool_create(title="A card with no session at all")["task_id"]
check("a card with no session is user-class", priority(cli) == KP.USER_PRIORITY, f"priority={priority(cli)}")

# A dispatcher worker's child inherits its parent's class through the parent
# card, not the worker's own session.
K.claim_task(conn, chat)
os.environ["HERMES_KANBAN_TASK"] = chat
child = tool_create(title="Check web-7 logs on cluster A")["task_id"]
check(
    "a user card's fan-out stays user-class",
    priority(child) >= KP.USER_PRIORITY,
    f"priority={priority(child)}",
)
os.environ.pop("HERMES_KANBAN_TASK", None)
K.claim_task(conn, triage)
os.environ["HERMES_KANBAN_TASK"] = triage
tchild = tool_create(
    title="Triage sub-step", priority=300, session_id="20261008_101500_ab12cd34",
)["task_id"]
spoof = tool_create(title="Triage on another board", priority=300, board="x")["task_id"]
spoof_priority = None
for row in conn.execute("SELECT priority FROM tasks WHERE id = ?", (spoof,)):
    spoof_priority = int(row[0] or 0)
check(
    "a triage worker naming another board still files background",
    spoof_priority is not None and spoof_priority < KP.USER_PRIORITY,
    f"priority={spoof_priority}",
)
check(
    "a triage card's fan-out stays background whatever priority or session it names",
    priority(tchild) < KP.USER_PRIORITY,
    f"priority={priority(tchild)}",
)
os.environ.pop("HERMES_KANBAN_TASK", None)

# --- B. kanban_create says when the card waits ---------------------------------
print("queue note:")
# Two cards are running (chat and triage, claimed above). Their children are
# settled first: a coordinator with an unsettled child is discounted from the
# running count (kanban_scheduling part 4), and this section is about a full
# cap, not about that discount. A cap of 2 is then full.
conn.execute("UPDATE tasks SET status = 'done' WHERE id IN (?, ?)", (child, tchild))
conn.commit()
KP._configured_cap = lambda: 2
session("slack", "C0EXAMPLE", "1700000000.000200")
queued = tool_create(title="One more question")
check(
    "a user card filed into a full cap is reported queued, machine-readably",
    queued.get("queued") is True and queued.get("queue", {}).get("limit") == 2,
    f"{queued}",
)
check(
    "the return carries no text for the model to relay (the thread hears it once)",
    "queue_note" not in queued,
    f"{queued}",
)
KP._configured_cap = lambda: 8
free = tool_create(title="A question with slots to spare")
check(
    "a card with a free slot gets upstream's return unchanged",
    "queued" not in free and "queue_note" not in free,
    f"{free}",
)

# --- C. The notifier delivers the queued notice --------------------------------
print("notifier:")
import gateway.kanban_watchers_notifier as N  # noqa: E402

check("queued is claimed", "queued" in N.TERMINAL_KINDS, f"{N.TERMINAL_KINDS}")
check("heartbeat is still claimed", "heartbeat" in N.TERMINAL_KINDS)
check("queued never wakes the agent", "queued" not in N._WAKE_KINDS, f"{N._WAKE_KINDS}")
formatter = N._EVENT_FORMATTERS.get("queued")
ev = SimpleNamespace(id=7, kind="queued", payload={"note": KP.QUEUED_TEXT, "running": 2, "limit": 2})
n = SimpleNamespace(progress_header="@cluster-dev ", head="@cluster-dev Kanban t_1")
message = formatter(ev, n)[0] if formatter else None
check(
    "the formatter renders the agreed sentence exactly",
    message == "⏳ Queued: the system is busy. Your request will start when a worker frees up.",
    f"{message!r}",
)

from gateway import kanban_progress_lines as PL  # noqa: E402


class Adapter:
    def __init__(self):
        self.sent, self.edits = [], []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append(text)
        return SimpleNamespace(success=True, message_id=f"m{len(self.sent)}")

    async def edit_message(self, chat_id, message_id, text):
        self.edits.append((message_id, text))
        return SimpleNamespace(success=True, message_id=message_id)


watcher = SimpleNamespace()
adapter = Adapter()
sub = {"task_id": "t_1", "platform": "google_chat", "chat_id": "spaces/0EXAMPLE", "thread_id": "T1"}


async def drive():
    await PL.deliver(watcher, adapter, sub, "queued", ev, message, {}, header="@cluster-dev ")
    note = SimpleNamespace(id=8, kind="heartbeat", payload={"note": "Reading the pod's events"})
    await PL.deliver(
        watcher, adapter, sub, "heartbeat", note, "⏳ @cluster-dev Reading the pod's events",
        {}, header="@cluster-dev ",
    )


asyncio.run(drive())
check(
    "the queued line is posted bare, as the user-facing sentence",
    adapter.sent == [message],
    f"{adapter.sent}",
)
check(
    "the worker's first note edits that message rather than posting another",
    len(adapter.sent) == 1 and len(adapter.edits) == 1
    and KP.QUEUED_TEXT in adapter.edits[0][1]
    and "Reading the pod's events" in adapter.edits[0][1]
    and "@cluster-dev" in adapter.edits[0][1],
    f"sent={adapter.sent} edits={adapter.edits}",
)

# --- D. The watcher's warning ---------------------------------------------------
print("watcher:")
import gateway.kanban_watchers as W  # noqa: E402

check(
    "the watcher resolved the saturation import",
    hasattr(W, "_kanban_saturation_tick"),
    "the trailer import did not execute",
)
loop = inspect.getsource(W.GatewayKanbanWatchersMixin._kanban_dispatcher_watcher)
check(
    "the stuck counter consults the saturation check first",
    loop.index("_kanban_saturation_tick(") < loop.index("bad_ticks = bad_ticks + 1"),
)
check("upstream's stuck warning is still there", "kanban dispatcher stuck" in loop)


class Log:
    def __init__(self):
        self.lines = []

    def warning(self, fmt, *args):
        self.lines.append(fmt % args)


sat = {
    "running": 2, "limit": 2, "background_running": 2, "user_running": 0,
    "cards": [
        {"id": "t_a", "assignee": "cluster-dev", "priority": 0, "session_id": "k8s-evt-1", "started_at": 0},
    ],
    "user_waiting": 1, "background_waiting": 0, "reserved": 0,
}
results = [("default", SimpleNamespace(saturation=sat, skipped_reserved=[], ready_left=1))]
log = Log()
saturated = [
    W._kanban_saturation_tick(log, results, True, False, 6, now=10_000.0 + i)
    for i in range(6)
]
check("a capped tick is saturation, not a bad tick", all(saturated))
check(
    "the saturation warning names the slots and who holds them",
    len(log.lines) == 1
    and log.lines[0].startswith("kanban dispatcher saturated: 2/2 worker slots busy (2 background, 0 user: t_a @cluster-dev")
    and "[k8s-evt-]" in log.lines[0]
    and "1 user card(s) and 0 background card(s) waiting" in log.lines[0],
    f"{log.lines}",
)
idle = [("default", SimpleNamespace(saturation=None, skipped_reserved=[], ready_left=3))]
check(
    "a tick with free slots and nothing spawned stays upstream's stuck case",
    W._kanban_saturation_tick(log, idle, True, False, 6) is False,
)

print()
if FAILURES:
    print(f"verify_kanban_priority: {len(FAILURES)} FAILED")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("verify_kanban_priority: all checks passed")
