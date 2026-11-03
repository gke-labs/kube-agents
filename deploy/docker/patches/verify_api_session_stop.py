"""Build gate for the API-server session stop.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_api_session_stop.py``. The applier only proves the anchors matched.
This drives the patched code itself:

1. The installed chat-completions handler is wrapped by ``track``, inside the
   wake guard's wrapper, and ``_run_agent`` carries the turn's record to its
   thread and binds the agent to it.
2. The route table names the stop route.
3. The stop reaches a stand-in agent through upstream's own
   ``request_hard_interrupt`` and ``_reap_disconnected_agent_processes``, both
   imported where the patch imports them.
4. The real ``kanban_create`` handler refuses on a stopped turn and writes no
   card; a turn in another context still creates.

The #3263 shape: a Stop pressed half a second into a turn, whose card the turn
filed 18 s later.

Usage::

    cd /opt/hermes && python3 verify_api_session_stop.py
"""

from __future__ import annotations

import contextvars
import inspect
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

import gateway.platforms.api_server as api  # noqa: E402
import tools.api_session_stop as stop  # noqa: E402
import tools.kanban_tools as kt  # noqa: E402
from hermes_cli import kanban_db_connect as KC  # noqa: E402

conn = KC.connect(DB)
SESSION = "a2a-ctx-0b1e5ccf90bd164ea10f7d31"

# --- 1. The handler and _run_agent ---------------------------------------------
print("the chat-completions handler and _run_agent:")
layers = []
layer = api.APIServerAdapter._handle_chat_completions
while layer is not None:
    code = getattr(layer, "__code__", None)
    layers.append(Path(code.co_filename).name if code is not None else "?")
    layer = getattr(layer, "__wrapped__", None)
check("the handler is wrapped by the session stop", "api_session_stop.py" in layers, f"layers={layers}")
check("inside the wake guard's wrapper",
      "kanban_wake_guard.py" in layers and layers.index("kanban_wake_guard.py") < layers.index("api_session_stop.py"),
      f"layers={layers}")
run_agent = inspect.getsource(api.APIServerAdapter._run_agent)
check("_run_agent carries the turn's record to its thread",
      "_kanban_wake_carry(_kube_session_stop.carry(self, _run))" in run_agent)
check("and binds the agent after registering it for shutdown interrupts",
      run_agent.find("_kube_session_stop.bind(agent)") > run_agent.find("self._shutdown_interruptible_agents[id(agent)] = agent") > 0)

# --- 2. The route ---------------------------------------------------------------
print("the route:")
check("the route table appends the stop route",
      "routes.append(_kube_session_stop.route(self))" in inspect.getsource(api.APIServerAdapter._http_route_table))

# --- 3. The interrupt -------------------------------------------------------------
print("the interrupt:")


class _Agent:
    def __init__(self):
        self.stops = []

    def hard_interrupt(self, message=None, *, tool_reason=None):
        self.stops.append(message)


agent = _Agent()
turn = stop._Turn(SESSION)
turn.agent = agent


class _Adapter:
    pass


adapter = _Adapter()
stop._turns(adapter)[SESSION] = {turn}
try:
    turns, interrupted = stop.stop(adapter, SESSION)
except Exception as exc:  # noqa: BLE001 — the build's to see
    turns, interrupted = [], 0
    check("the stop runs against upstream's interrupt and reap", False, repr(exc))
check("the session's running turn is stopped", turns == [turn] and turn.stopped)
check("its agent gets upstream's hard interrupt", interrupted == 1 and agent.stops == [stop.INTERRUPT_MESSAGE], repr(agent.stops))

# --- 4. kanban_create ---------------------------------------------------------------
print("kanban_create:")


def cards():
    return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


def tool_create(title):
    return json.loads(kt._handle_create({"assignee": "platform", "title": title}))


os.environ["HERMES_SESSION_PLATFORM"] = "api_server"
os.environ["HERMES_SESSION_CHAT_ID"] = SESSION
before = cards()
token = stop._CURRENT.set(turn)
try:
    refused = tool_create("Check seeded-a for Deployments missing memory limits")
    other = contextvars.Context().run(tool_create, "The next question")
finally:
    stop._CURRENT.reset(token)
check("a stopped turn's create is refused", not refused.get("ok") and "refused" in json.dumps(refused), json.dumps(refused))
check("a turn in another context, meanwhile, still creates", other.get("ok") is True, json.dumps(other))
check("only that card was written", cards() == before + 1, f"cards {before} -> {cards()}")

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} check(s):")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("all checks passed")
