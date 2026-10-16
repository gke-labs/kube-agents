#!/usr/bin/env python3
"""Build gate for the card stop route (`POST /v1/sessions/{id}/cards/stop`, #3263).

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` with the shared
scripts on ``PYTHONPATH``, after both are in place. The unit tests in
``agents/platform/scripts/test_session_kv_server.py`` run the route against a
stub ``archive_task`` written by the same hand as the route. This runs it
against the real one, on a board built with Hermes' own ``create_task``,
``claim_task`` and ``_set_worker_pid``, so what it checks is what the
dispatcher's board does:

0. the route's own board connection carries Hermes' PRAGMAs, its 3 s busy
   timeout and Hermes' probe registration;
1. the open statuses the route archives are Hermes' statuses less the two
   settled ones, so a status added upstream is a decision, not a silent change;
2. a running parent with a gated todo child, a ready fan-out child under
   another session id and its gated fan-out grandchild, plus a card filed
   after a compression, are all archived, and ``recompute_ready`` promotes
   none of them on the way (the fan-out is found through
   ``kanban_worker_children`` when the children patch recorded it, else the
   ``created`` event's ``creator_task_id``);
3. the running card's worker, a real process started as the dispatcher starts
   one, is terminated through the route's ``signal_fn``
   (``archive_worker_termination``), and the ``sleep`` it started is gone too;
   a running card whose pid belongs to a process without the card's marker is
   archived and that process is not signalled;
4. ``task_links`` children that wait on our card under another conversation's
   session, a cron job's and an alert's are not archived, though Hermes'
   ``create_task(parents=...)`` and ``link_tasks`` copied our thread's
   subscription onto them and the route is called with that thread; nor are
   the cards filed in a branch or by a delegate of the compressed session, nor
   another conversation's card; a done card is untouched and reported settled;
5. a second call stops nothing;
6. a worker pid written onto a card after it was archived (the claim->spawn
   window in ``_set_worker_pid``) is killed and cleared by the late-pid sweep.
   If upstream has added a status guard there, the pid is never written and
   the check says so instead.

Usage::

    cd /opt/hermes && PYTHONPATH=/opt/defaults/scripts python3 verify_card_stop.py
"""

from __future__ import annotations

import inspect
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.getcwd())

FAILURES: list[str] = []


def check(label: str, condition: object, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


TMP = Path(tempfile.mkdtemp())
DB = TMP / "kanban.db"
HOME = TMP / "hermes-home"
HOME.mkdir()
os.environ["HERMES_HOME"] = str(HOME)
os.environ["HERMES_KANBAN_DB"] = str(DB)
os.environ["SESSION_KV_DB_PATH"] = str(TMP / "session_kv.db")
os.environ.pop("HERMES_KANBAN_TASK", None)

CONTEXT = "ctx-verify"
SESSION = "a2a-ctx-verify"
CONVERSATION = "slack:C0VERIFY/1700000000.000100"
MARKER = "HERMES_KANBAN_TASK"

# The conversation's session, compressed once: a card filed after that carries
# the continuation's id.
from hermes_state import SessionDB  # noqa: E402

_sessions = SessionDB(HOME / "state.db")
_sessions.create_session(SESSION, "api_server")
_sessions.end_session(SESSION, "compression")
_sessions.create_session("verify-cont-1", "api_server", parent_session_id=SESSION)
# Children of the compressed session that are not its continuation, by
# Hermes' own rule: a branch and a delegate.
_sessions.create_session("verify-branch", "api_server", parent_session_id=SESSION, model_config={"_branched_from": SESSION})
_sessions.create_session("verify-delegate", "api_server", parent_session_id=SESSION, model_config={"_delegate_from": SESSION})
_sessions.close()

from hermes_cli import kanban_db as kb  # noqa: E402
from hermes_cli import kanban_db_dispatch as dispatch  # noqa: E402
from hermes_cli.kanban_db_connect import connect  # noqa: E402

import session_kv_server as skv  # noqa: E402

skv.KANBAN_DB_PATH = str(DB)
skv.CARD_STOP_RESCAN_INTERVAL_SECONDS = 0.0

PROCS: list[subprocess.Popen] = []


def fake_worker(task_id: str) -> tuple[subprocess.Popen, int]:
    """A worker started the way `_default_spawn` starts one, with a child of its own."""
    pid_file = TMP / f"{task_id}.child"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, sys, time\n"
            "child = subprocess.Popen(['sleep', '300'])\n"
            "open(sys.argv[1] + '.tmp', 'w').write(str(child.pid))\n"
            "__import__('os').rename(sys.argv[1] + '.tmp', sys.argv[1])\n"
            "time.sleep(300)\n",
            str(pid_file),
        ],
        env={**os.environ, MARKER: task_id},
        start_new_session=True,
    )
    PROCS.append(proc)
    deadline = time.monotonic() + 15
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    return proc, int(pid_file.read_text())


def alive(pid: int) -> bool:
    return dispatch._pid_alive(pid)


def status(task_id: str) -> str:
    return conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]


try:
    print("card stop: Hermes' statuses")
    open_statuses = set(kb.VALID_STATUSES) - skv.CARD_STOP_SETTLED_STATUSES
    check(
        "the route's open statuses are Hermes' less done and archived",
        open_statuses == {"triage", "todo", "scheduled", "ready", "running", "blocked", "review"},
        f"Hermes now declares {sorted(kb.VALID_STATUSES)}; decide whether a new one is stopped",
    )
    check(
        "archive_task still takes (conn, task_id, *, signal_fn)",
        list(inspect.signature(kb.archive_task).parameters) == ["conn", "task_id", "signal_fn"],
        str(inspect.signature(kb.archive_task)),
    )

    conn = connect(DB)
    from hermes_cli.sqlite_safe_read import connect_tracked  # noqa: E402
    from hermes_cli.kanban_db_notify import add_notify_sub  # noqa: E402

    check("the route opens its board connections through connect_tracked", skv._hermes_kanban() == (kb, connect_tracked))
    route_conn = skv._card_stop_connect(connect_tracked, str(DB))
    pragma_names = [p.split("=")[0] for p in skv.CARD_STOP_BOARD_PRAGMAS]
    ours = {name: route_conn.execute(f"PRAGMA {name}").fetchone()[0] for name in pragma_names}
    hermes = {name: conn.execute(f"PRAGMA {name}").fetchone()[0] for name in pragma_names}
    check("the route's connection carries the PRAGMAs Hermes' connect sets", ours == hermes, f"{ours} != {hermes}")
    check("with the route's busy timeout, not Hermes' 120 s",
          route_conn.execute("PRAGMA busy_timeout").fetchone()[0] == int(skv.CARD_STOP_BUSY_TIMEOUT_S * 1000))
    check("and registered against Hermes' byte-level probes", getattr(route_conn, "_hermes_tracked_path", None) is not None)
    route_conn.close()

    parent = kb.create_task(conn, title="check node pools across the fleet", session_id=SESSION)
    # The subscription kanban_event_routing.py writes for a card the turn filed.
    add_notify_sub(conn, task_id=parent, platform="slack", chat_id=SESSION, thread_id=CONVERSATION)
    claimed = kb.claim_task(conn, parent)
    check("the parent card is claimed (running)", claimed is not None and status(parent) == "running")
    worker, worker_child = fake_worker(parent)
    dispatch._set_worker_pid(conn, parent, worker.pid)
    # The descendants carry explicit session ids of their own (inherit_creator_origin
    # keeps an explicit one), so it is the edges that find them, not the seed.
    linked = kb.create_task(conn, title="summarise", parents=[parent], session_id=SESSION)
    # A fan-out child under an explicit session of its own (inherit_creator_origin
    # keeps it), so the fan-out edge is what finds it, and its own gated fan-out.
    fanned = kb.create_task(conn, title="cluster a", session_id="verify-explicit-2", creator_task_id=parent)
    grand = kb.create_task(conn, title="cluster a, step 2", parents=[fanned], creator_task_id=fanned)
    # Cards that wait on ours but are not ours: another conversation's, a cron job's.
    foreign_conv = kb.create_task(conn, title="waits on it, elsewhere", parents=[parent], session_id="a2a-ctx-other2")
    foreign_cron = kb.create_task(conn, title="nightly, waits on it", parents=[parent], session_id="cron_nightly")
    foreign_linked = kb.create_task(conn, title="an alert's card, linked later", session_id="k8s-evt-verify")
    kb.link_tasks(conn, parent, foreign_linked)
    inherited = {
        r[0]
        for r in conn.execute(
            "SELECT task_id FROM kanban_notify_subs WHERE chat_id = ? AND thread_id = ?", (SESSION, CONVERSATION)
        )
    }
    check("Hermes copied our subscription onto the waiting cards (create_task and link_tasks)",
          {foreign_conv, foreign_cron, foreign_linked} <= inherited, str(inherited))
    on_branch = kb.create_task(conn, title="filed in a branch", session_id="verify-branch")
    on_delegate = kb.create_task(conn, title="filed by a delegate", session_id="verify-delegate")
    # Running, with a pid that a process without the card's marker holds.
    impostor_card = kb.create_task(conn, title="pid reused", session_id=SESSION)
    kb.claim_task(conn, impostor_card)
    impostor = subprocess.Popen(["sleep", "300"], start_new_session=True)
    PROCS.append(impostor)
    dispatch._set_worker_pid(conn, impostor_card, impostor.pid)
    after_compression = kb.create_task(conn, title="filed after a compression", session_id="verify-cont-1")
    done = kb.create_task(conn, title="an earlier answer", session_id=SESSION)
    kb.complete_task(conn, done, result="done earlier", summary="done earlier")
    other = kb.create_task(conn, title="another conversation", session_id="a2a-ctx-other")
    other_child = kb.create_task(conn, title="another conversation, gated", parents=[other], session_id="a2a-ctx-other")
    tree = [parent, linked, fanned, grand, after_compression, impostor_card]
    check(
        "the tree starts open: running, todo, ready, todo, ready, running",
        [status(t) for t in tree] == ["running", "todo", "ready", "todo", "ready", "running"],
        str([status(t) for t in tree]),
    )
    foreign = [foreign_conv, foreign_cron, foreign_linked]
    check("the cards waiting on it start todo", [status(t) for t in foreign] == ["todo"] * 3, str([status(t) for t in foreign]))
    # Hermes stamps created_at in whole seconds, so every card here ties and the
    # route's tie-break is the random id. Spread them in filing order, parents
    # first, which is the order that promotes a child if the route archived by age.
    for n, task_id in enumerate([parent, linked, fanned, grand, *foreign, impostor_card, after_compression]):
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (1_700_000_000 + n, task_id))
    check("the done card is done", status(done) == "done", status(done))
    last_event = conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0]

    print("card stop: the first stop")
    body = skv.stop_session_cards(SESSION, {"context_id": CONTEXT, "conversation": CONVERSATION})
    stopped = {entry["id"]: entry for entry in body["stopped"]}
    check("every card of the tree is reported stopped", set(stopped) == set(tree), json.dumps(body["stopped"]))
    check("every card of the tree is archived", all(status(t) == "archived" for t in tree), str([status(t) for t in tree]))
    promoted = [
        r[0]
        for r in conn.execute("SELECT task_id FROM task_events WHERE kind = 'promoted' AND id > ?", (last_event,))
    ]
    check("recompute_ready promoted none of them", not set(promoted) & set(tree), str(promoted))
    check("the cards waiting on it, carrying our subscription, are not archived (and are promoted, as finishing it would have)",
          [status(t) for t in foreign] == ["ready"] * 3, str([status(t) for t in foreign]))
    check("a branch's and a delegate's cards are not the conversation's",
          (status(on_branch), status(on_delegate)) == ("ready", "ready"), str((status(on_branch), status(on_delegate))))

    def termination(task_id):
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'archive_worker_termination'", (task_id,)
        ).fetchone()
        return json.loads(row[0]) if row else {}

    if os.path.isdir("/proc/self"):
        check("Hermes terminated the running card's worker through the route's signal_fn",
              termination(parent).get("terminated") is True, json.dumps(termination(parent)))
        worker.wait(10)
        check("the worker process is gone", worker.poll() is not None)
        deadline = time.monotonic() + 5
        while alive(worker_child) and time.monotonic() < deadline:
            time.sleep(0.05)
        check("the worker's own child is gone too (its group)", not alive(worker_child), f"pid {worker_child}")
        check("both were reported signalled", (stopped.get(parent, {}).get("worker") or {}).get("signalled") == 2,
              json.dumps(stopped.get(parent)))
        check("a pid held by a process without the marker is not signalled", impostor.poll() is None and alive(impostor.pid))
        check("and Hermes records that termination as not done",
              termination(impostor_card).get("termination_attempted") is True
              and termination(impostor_card).get("terminated") is False,
              json.dumps(termination(impostor_card)))
    else:
        print("  skip the worker kill checks: no /proc on this host")
    check("another conversation's cards are untouched", (status(other), status(other_child)) == ("ready", "todo"),
          str((status(other), status(other_child))))
    check("the done card is reported settled, not stopped",
          [entry["id"] for entry in body["settled"]] == [done] and status(done) == "done", json.dumps(body["settled"]))
    check("nothing failed, pending or unresolved, and no board error",
          (body["failed"], body["pending"], body["unresolved"], body["errors"]) == ([], [], [], []),
          json.dumps({k: body[k] for k in ("failed", "pending", "unresolved", "errors")}))

    print("card stop: the second stop")
    again = skv.stop_session_cards(SESSION, {"context_id": CONTEXT, "conversation": CONVERSATION})
    check("a second call stops nothing", again["stopped"] == [] and again["rounds"] == 0, json.dumps(again))

    print("card stop: a worker spawned onto an archived card")
    late, late_child = fake_worker(fanned)
    dispatch._set_worker_pid(conn, fanned, late.pid)
    written = conn.execute("SELECT worker_pid FROM tasks WHERE id = ?", (fanned,)).fetchone()[0]
    if written is None:
        print("  note _set_worker_pid no longer writes onto an archived card (upstream guard); nothing to sweep")
        late.kill()
    elif not os.path.isdir("/proc/self"):
        print("  skip the late-pid sweep checks: no /proc on this host")
        late.kill()
    else:
        swept = skv._sweep_late_worker_pids(conn, [fanned], skv._Budget())
        late.wait(10)
        check("the late worker is killed", late.poll() is not None and not alive(late_child), json.dumps(swept))
        cleared = conn.execute("SELECT worker_pid FROM tasks WHERE id = ?", (fanned,)).fetchone()[0]
        check("its pid is cleared from the archived row", cleared is None, str(cleared))
finally:
    for proc in PROCS:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait(5)

print()
if FAILURES:
    print(f"verify_card_stop: {len(FAILURES)} FAILED")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("verify_card_stop: all checks passed")
