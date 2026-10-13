#!/usr/bin/env python3
"""Build gate for the wake-turn guard on kanban_create.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_kanban_wake_guard.py``. The applier only proves the anchors matched.
This drives the patched code itself:

1. ``gateway.wake._self_post_chat_completion`` sends the wake header (the HTTP
   call is captured, not made).
2. The installed chat-completions wrapper marks the turn for a marked request
   in a front-door session, including one continued through compression, and
   not for an unmarked request or an event-watcher session.
3. The mark reaches the executor thread ``_run_agent`` runs the turn on only
   through ``carry_wake_mark``, and the patched ``_run_agent`` uses it.
4. The real ``kanban_create`` handler refuses on a marked turn and writes no
   card. A turn in another context, such as a person's message landing in the
   same session meanwhile, still creates.
5. The real ``kanban_unblock`` handler refuses on a marked turn and leaves the
   card blocked; the person's answer, a turn of its own, still unblocks it.

The #2610 shape: periodic run 2109358269314109440, where a front-door
session's wake turn filed the card whose worker opened #210.

Usage::

    cd /opt/hermes && python3 verify_kanban_wake_guard.py
"""

from __future__ import annotations

import asyncio
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
for name in ("HERMES_KANBAN_TASK", "HERMES_SESSION_KEY", "HERMES_SESSION_PLATFORM",
             "HERMES_SESSION_CHAT_ID"):
    os.environ.pop(name, None)

import gateway.wake as wake  # noqa: E402
import gateway.platforms.api_server as api  # noqa: E402
import tools.kanban_tools as kt  # noqa: E402
import tools.kanban_wake_guard as guard  # noqa: E402
from hermes_cli import kanban_db_connect as KC  # noqa: E402

conn = KC.connect(DB)
FRONT_DOOR = "a2a-ctx-ba0e5ccf90bd164ea10f7d31"
EVENT_WATCHER = "k8s-evt-13f95359"


def cards():
    return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


def as_session(session_id):
    os.environ["HERMES_SESSION_PLATFORM"] = "api_server"
    os.environ["HERMES_SESSION_CHAT_ID"] = session_id


def tool_create(title):
    return json.loads(kt._handle_create({"assignee": "platform", "title": title}))


# --- 1. The self-post marks its request ----------------------------------------
print("the self-post:")
sent: dict = {}


class _Resp:
    status = 200

    async def read(self):
        return b"{}"

    async def text(self):
        return "{}"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, *a, **k):
        pass

    def post(self, url, json=None, headers=None):
        sent.update(headers or {})
        return _Resp()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Adapter:
    _host, _port, _api_key, _model_name = "127.0.0.1", 8642, "k", "hermes-agent"


import aiohttp  # noqa: E402

_real_session = aiohttp.ClientSession
aiohttp.ClientSession = _Session
try:
    asyncio.run(wake._self_post_chat_completion(_Adapter(), text="[kanban] Task t_x completed.", session_id=FRONT_DOOR))
finally:
    aiohttp.ClientSession = _real_session
check("the wake self-post sends the wake header", sent.get(guard.WAKE_HEADER) == "1", f"headers={sorted(sent)}")
check("and still names the session", sent.get("X-Hermes-Session-Id") == FRONT_DOOR)

# --- 2. The installed wrapper marks the turn --------------------------------------
print("the chat-completions wrapper:")
layer = api.APIServerAdapter._handle_chat_completions
found = None
while layer is not None:
    code = getattr(layer, "__code__", None)
    if code is not None and code.co_filename.endswith("kanban_wake_guard.py"):
        found = layer
        break
    layer = getattr(layer, "__wrapped__", None)
check("the handler is wrapped by the wake guard", found is not None)

COMPRESSED_TIP = "20261013_160000_ab12cd"

# The real session store, with a compressed continuation of a front-door
# session, read the way the wrapper reads it (through asyncio.to_thread).
from hermes_state import SessionDB  # noqa: E402

store = SessionDB(TMP / "state.db")
store.create_session(FRONT_DOOR, "api_server")
store.create_session(COMPRESSED_TIP, "api_server", parent_session_id=FRONT_DOOR)
check(
    "the API server has the session-store accessor the walk uses",
    asyncio.iscoroutinefunction(getattr(api.APIServerAdapter, "_ensure_session_db_async", None)),
)


class _Adapter2:
    async def _ensure_session_db_async(self):
        return store


class _Request:
    def __init__(self, headers):
        self.headers = dict(headers)


def run_wrapper(headers):
    """Run the installed wrapper over a probe in place of the real handler
    (its closure cell is swapped and restored); report the mark the probe saw."""
    seen = {}

    async def probe(self, request, *a, **k):
        seen["wake"] = guard.is_wake_turn()
        return "probe"

    cells = [c for c in (found.__closure__ or ()) if callable(c.cell_contents)]
    if len(cells) != 1:
        return None
    real = cells[0].cell_contents
    cells[0].cell_contents = probe
    try:
        asyncio.run(found(_Adapter2(), _Request(headers)))
    finally:
        cells[0].cell_contents = real
    return seen.get("wake")


if found is not None:
    marked = {guard.WAKE_HEADER: "1"}
    check("a marked front-door request runs as a wake turn",
          run_wrapper({**marked, "X-Hermes-Session-Id": FRONT_DOOR}) is True)
    check("so does one whose session continues a front-door one through compression",
          run_wrapper({**marked, "X-Hermes-Session-Id": COMPRESSED_TIP}) is True)
    check("an unmarked request does not",
          run_wrapper({"X-Hermes-Session-Id": FRONT_DOOR}) is False)
    check("an event-watcher wake is out of scope",
          run_wrapper({**marked, "X-Hermes-Session-Id": EVENT_WATCHER}) is False)
    inner = next((c.cell_contents for c in (found.__closure__ or ()) if callable(c.cell_contents)), None)
    check("the wrapper's inner handler is the real chat-completions handler",
          getattr(inner, "__name__", "") == "_handle_chat_completions", repr(inner))

# --- 3. The mark reaches the executor thread -------------------------------------
print("the executor thread:")
import inspect  # noqa: E402

check("the patched _run_agent carries the mark to its executor",
      "run_in_executor(None, _kanban_wake_carry(_run))" in inspect.getsource(api.APIServerAdapter._run_agent))


async def on_executor(carry):
    token = guard._WAKE_TURN.set(True)
    try:
        fn = guard.is_wake_turn
        return await asyncio.get_running_loop().run_in_executor(None, guard.carry_wake_mark(fn) if carry else fn)
    finally:
        guard._WAKE_TURN.reset(token)


check("with the carry, the turn's thread sees the mark", asyncio.run(on_executor(True)) is True)
check("without it, the thread doesn't (why the carry is needed)", asyncio.run(on_executor(False)) is False)

# --- 4. kanban_create --------------------------------------------------------------
print("kanban_create:")
import contextvars  # noqa: E402

as_session(FRONT_DOOR)
before = cards()
token = guard._WAKE_TURN.set(True)
try:
    refused = tool_create("Fix payments-api command")
    person = contextvars.Context().run(tool_create, "Investigate payments-api")
finally:
    guard._WAKE_TURN.reset(token)
check("a wake turn's create is refused", not refused.get("ok") and "refused" in json.dumps(refused), json.dumps(refused))
check("the refusal keeps the suggestion for the person's next message", "when the person next writes" in json.dumps(refused))
check("a turn in another context, meanwhile, still creates", person.get("ok") is True, json.dumps(person))
check("only that card was written", cards() == before + 1, f"cards {before} -> {cards()}")
after = tool_create("Next question")
check("once the wake turn ends, the session creates again", after.get("ok") is True, json.dumps(after))

# --- 5. kanban_unblock -------------------------------------------------------------
print("kanban_unblock:")


def status(task_id):
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row[0] if row else None


asked = tool_create("Fix ready: shall I open a PR?")["task_id"]
conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (asked,))
conn.commit()
token = guard._WAKE_TURN.set(True)
try:
    wake_unblock = json.loads(kt._handle_unblock({"task_id": asked}))
finally:
    guard._WAKE_TURN.reset(token)
check("a wake turn's unblock is refused", not wake_unblock.get("ok") and "refused" in json.dumps(wake_unblock), json.dumps(wake_unblock))
check("the refusal says the question is for the person", "for the person" in json.dumps(wake_unblock))
check("the card stays blocked", status(asked) == "blocked", f"status={status(asked)}")
answer = json.loads(contextvars.Context().run(kt._handle_unblock, {"task_id": asked}))
check("the person's answer, a turn of its own, still unblocks it", answer.get("ok") is True, json.dumps(answer))

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} check(s):")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("all checks passed")
