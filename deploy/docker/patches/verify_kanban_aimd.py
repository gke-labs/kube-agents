#!/usr/bin/env python3
"""Build gate for the adaptive kanban cap.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_kanban_scheduling.py`` (edit 11) and ``apply_kanban_aimd.py``. The
appliers only prove their anchors matched. This drives the patched engine the
way the install reaches it: a real board, a planted ``pressure`` event, the real
``dispatch_once`` under a cap of 4 with four ready cards, and the patched
``finalize_foreground_result`` as a worker.

  A. A board with no pressure dispatches under the configured cap and writes
     the limiter's row.
  B. A planted pressure event leaves one slot: one spawn, the row at 1, the
     saturation record against the effective cap on the next tick.
  C. The clock moved past the cooldown: the next tick dispatches one more.
  D. As a worker, an ssh exit 255 without the cwd marker and the credential
     proxy's busy line each write a pressure event on the worker's card, a
     clean result writes none, and the dispatcher reads what was written.

Usage::

    cd /opt/hermes && python3 verify_kanban_aimd.py
"""

from __future__ import annotations

import itertools
import json
import os
import sys
import tempfile
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
if str(HERMES) not in sys.path:
    sys.path.insert(0, str(HERMES))

FAILURES: list[str] = []


def check(label: str, condition: object, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


TMP = Path(tempfile.mkdtemp())
for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_DB"):
    os.environ.pop(var, None)

from hermes_cli import kanban_aimd as KA  # noqa: E402
from hermes_cli import kanban_db as K  # noqa: E402
from hermes_cli import kanban_db_connect as KC  # noqa: E402
from hermes_cli import kanban_db_dispatch as KD  # noqa: E402
from hermes_cli import kanban_priority as KP  # noqa: E402
from hermes_cli import profiles as _profiles  # noqa: E402
from tools.terminal_tool_result import finalize_foreground_result  # noqa: E402

CAP = 4
COOLDOWN = 60
ASSIGNEE = "platform"
USER = KP.USER_PRIORITY

# The fixture's assignee is not a profile in the build; without this every
# ready row is dropped as non-spawnable before the cap is consulted.
_profiles.profile_exists = lambda name: name == ASSIGNEE
_BOARDS = itertools.count()
_now = [1_000_000.0]
KA.CLOCK = lambda: _now[0]
os.environ[KA.COOLDOWN_ENV] = str(COOLDOWN)
# The sandbox probe would need an ssh master; none exists in the build.
check("the shell probe is a no-op without a master socket", KA.probe_shell_memory() is None)


def fresh():
    path = TMP / f"kanban{next(_BOARDS)}.db"
    conn = KC.connect(path)
    if conn.execute("SELECT count(*) FROM tasks").fetchone()[0]:
        raise SystemExit("verify_kanban_aimd: fixture defect — a fresh board is not empty")
    return conn, path


def new_card(conn, title, priority=0):
    task = K.create_task(conn, title=title, assignee=ASSIGNEE, priority=priority)
    return task.id if hasattr(task, "id") else str(task)


def tick(conn):
    asked = []

    def spawn_fn(*args, **kwargs):
        task = kwargs.get("task") or kwargs.get("task_id") or (args[0] if args else None)
        asked.append(getattr(task, "id", task))
        return os.getpid()

    res = KD.dispatch_once(conn, spawn_fn=spawn_fn, max_in_progress=CAP)
    return asked, res


def plant_pressure(conn, task_id):
    with KC.write_txn(conn):
        K._append_event(conn, task_id, KA.PRESSURE_KIND, {"reason": "planted", "source": "verify"})


def state(conn):
    return KA.load_state(conn)


print("wiring:")
check("the dispatcher imports the limiter", getattr(KD, "_kanban_effective_max_in_progress", None) is KA.effective_max_in_progress)

print("A. a clean board:")
# Two user cards and two background ones: at a cap of 4 each class may hold
# three slots (kanban_priority), so all four spawn under the configured cap.
PRIORITIES = (USER, USER, 0, 0)


def ready_board():
    conn, path = fresh()
    cards = [new_card(conn, f"card {n}", priority=priority) for n, priority in enumerate(PRIORITIES)]
    K.recompute_ready(conn)
    return conn, path, cards


conn, _, cards = ready_board()
asked, res = tick(conn)
check("A1. four ready cards at cap 4 all spawn", len(asked) == CAP, f"spawned {asked}, result {res}")
row = state(conn)
check("A2. the limiter's row is written at the configured cap", row is not None and (row["configured"], row["effective"]) == (CAP, CAP), str(row))

print("B. a planted pressure event:")
conn, _, cards = ready_board()
# The first tick on a board starts the event cursor, so the plant comes after it.
KA.effective_max_in_progress(conn, CAP, probe=lambda: None)
plant_pressure(conn, cards[0])
asked, res = tick(conn)
check("B1. one spawn under a planted pressure event", len(asked) == 1, f"spawned {asked}")
row = state(conn)
check("B2. the row is at the floor with a cooldown armed", row is not None and row["effective"] == KA.FLOOR and row["cooldown_until"] == _now[0] + COOLDOWN, str(row))
asked, res = tick(conn)
check("B3. the next tick spawns nothing", asked == [], f"spawned {asked}")
sat = res.saturation or {}
check("B4. saturation is recorded against the effective cap", sat.get("limit") == KA.FLOOR and sat.get("running") == 1, f"saturation {sat}")

print("C. recovery:")
_now[0] += COOLDOWN
asked, res = tick(conn)
check("C1. one more spawn once the cooldown lapsed", len(asked) == 1, f"spawned {asked}")
check("C2. the row stepped to 2", state(conn)["effective"] == 2, str(state(conn)))
_now[0] += COOLDOWN
asked, res = tick(conn)
check("C3. and to 3 a cooldown later", len(asked) == 1 and state(conn)["effective"] == 3, f"spawned {asked}, {state(conn)}")

print("D. the worker side:")
conn, path = fresh()
worker = new_card(conn, "Why is web-7 failing?")
K.recompute_ready(conn)
K.claim_task(conn, worker)
# The dispatcher's first tick on a board starts its event cursor at the newest
# row, so it runs before the worker writes anything.
KA.effective_max_in_progress(conn, CAP, probe=lambda: None)
os.environ["HERMES_KANBAN_DB"] = str(path)
os.environ["HERMES_KANBAN_TASK"] = worker


def run_terminal(returncode, output, env_type="ssh", cwd_observed=False):
    result = {"output": output, "returncode": returncode}
    if cwd_observed:
        result["cwd_observed"] = True
        result["cwd"] = "/home/agent"
    return finalize_foreground_result(
        command="kubectl get pods", result=result, env=None, env_type=env_type,
        effective_task_id=worker, task_id=None, session_id=None, session_key="default",
        workdir=None, command_cwd=None, approval_note=None,
    )


def pressure_events(conn, task_id):
    return [
        json.loads(r["payload"] or "{}")
        for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id", (task_id, KA.PRESSURE_KIND),
        )
    ]


out = json.loads(run_terminal(0, "NAME READY\nweb-7 0/1", cwd_observed=True))
check("D1. a clean result writes nothing", pressure_events(conn, worker) == [] and out["exit_code"] == 0)
out = json.loads(run_terminal(255, "", cwd_observed=False))
events = pressure_events(conn, worker)
check("D2. an ssh exit 255 without the marker writes a pressure event", len(events) == 1 and events[0]["reason"] == KA.REASON_SSH_EXIT, str(events))
check("D2. the tool result still carries the exit code", out["exit_code"] == 255, str(out))
out = json.loads(run_terminal(1, f"{KA.BUSY_LINE_PREFIX}503)\ncredential proxy error (HTTP 503): non-JSON response", cwd_observed=True))
events = pressure_events(conn, worker)
check("D3. the broker's busy line writes a pressure event", len(events) == 2 and events[1]["reason"] == KA.REASON_BROKER_BUSY, str(events))
out = json.loads(run_terminal(255, "", cwd_observed=True))
check("D4. a command's own 255 with the marker writes nothing", len(pressure_events(conn, worker)) == 2)
os.environ.pop("HERMES_KANBAN_TASK")
out = json.loads(run_terminal(255, "", cwd_observed=False))
check("D5. outside a worker nothing is written", len(pressure_events(conn, worker)) == 2)
KA.effective_max_in_progress(conn, CAP, probe=lambda: None)
check("D6. the dispatcher reads the worker's events", state(conn)["effective"] == KA.FLOOR, str(state(conn)))

print()
if FAILURES:
    print(f"verify_kanban_aimd: {len(FAILURES)} FAILED")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("verify_kanban_aimd: all checks passed")
