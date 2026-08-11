#!/usr/bin/env python3
"""Build gate for the kanban wake-nudge patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after the applier.
The applier only proves the anchors matched. This drives the *patched* engine
against a real board — real ``create_task`` / ``claim_task`` /
``complete_task`` / ``unblock_task`` — because the property that matters is
behavioural: the exact mutations that give a watcher work must nudge, the
mutations a dispatcher tick performs must NOT (that would be a self-waking
busy loop), and a board whose wake file cannot be written must degrade to
plain polling rather than fail the write.

Usage::

    cd /opt/hermes && python3 verify_kanban_wake_nudge.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

FAILURES: list[str] = []


def check(label: str, condition: object, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


from hermes_cli import kanban_db as K  # noqa: E402
from hermes_cli.kanban_wake_nudge import (  # noqa: E402
    WakeMonitor,
    record_wake,
    wait_interval,
    wake_path,
)

TMP = Path(tempfile.mkdtemp())


def fresh():
    db = TMP / f"kanban{len(list(TMP.iterdir()))}.db"
    return db, K.connect(db)


def status(conn, tid):
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
    return row["status"] if row else None


# --- 1. The wiring resolved --------------------------------------------------
print("import wiring:")
import gateway.kanban_watchers as watchers  # noqa: E402

check(
    "the watchers resolved the monitor import",
    hasattr(watchers, "_KanbanWakeMonitor") and hasattr(watchers, "_kanban_wait_interval"),
    "the trailer import did not execute",
)
check(
    "kanban_db resolved the producer import",
    hasattr(K, "_kanban_record_wake"),
)

# --- 2. Producers nudge, dispatcher writes stay quiet ------------------------
print("producer nudges:")
db, conn = fresh()
monitor = WakeMonitor(db)

parent = K.create_task(conn, title="coordinator", assignee="platform")
check("create_task writes the wake file next to the board", Path(wake_path(db)).exists())
check("create_task nudges", monitor.changed())

K.recompute_ready(conn)
claimed = K.claim_task(conn, parent)
check("the card claims normally", claimed is not None)
check(
    "a dispatcher's own writes do not nudge",
    not monitor.changed(),
    "claim/recompute nudging would wake the dispatcher into a busy loop",
)

child = K.create_task(conn, title="synthesizer", assignee="platform", parents=(parent,))
monitor.changed()  # consume the create nudge; the completion is what's under test
check("the dependent child waits on its parent", status(conn, child) == "todo")
done = K.complete_task(conn, parent, result="the answer", summary="done")
check("complete_task succeeds", done is True)
check("complete_task nudges", monitor.changed())
check(
    "the completion nudge covers the promoted child",
    status(conn, child) == "ready",
    f"child left in {status(conn, child)!r}",
)

blocked = K.create_task(
    conn, title="parked for ops review", assignee="platform", initial_status="blocked"
)
monitor.changed()  # consume the create nudge
check("unblock_task returns the card", K.unblock_task(conn, blocked) is True)
check("unblock_task nudges", monitor.changed())
check(
    "a no-op unblock stays quiet",
    K.unblock_task(conn, blocked) is False and not monitor.changed(),
)

# --- 3. Reaction time through the real wait ----------------------------------
print("reaction time:")


async def race():
    db2, conn2 = fresh()
    conn2.close()
    m = WakeMonitor(db2)

    def create_on_thread():
        # A worker is another process with its own connection; the nearest
        # in-process equivalent is another thread with its own connection
        # (kanban connections are thread-bound).
        c = K.connect(db2)
        try:
            K.create_task(c, title="mid-wait card", assignee="platform")
        finally:
            c.close()

    async def create_later():
        await asyncio.sleep(0.1)
        await asyncio.to_thread(create_on_thread)

    task = asyncio.ensure_future(create_later())
    start = time.monotonic()
    woke = await wait_interval(m, 5.0)
    await task
    return woke, time.monotonic() - start


woke, elapsed = asyncio.run(race())
check("a mid-wait create breaks the wait early", woke and elapsed < 1.5, f"{elapsed:.2f}s")
check(
    "the wait would have cost the full 5s tick upstream",
    elapsed < 2.5,  # avg upstream penalty per hop measured 2026-08-07
    f"{elapsed:.2f}s",
)

# --- 4. Degradation is exactly upstream polling -------------------------------
print("fail-soft degradation:")
db3, conn3 = fresh()
with mock.patch("hermes_cli.kanban_wake_nudge.os.utime", side_effect=OSError("read-only fs")):
    check("record_wake fails soft", record_wake(db3) is False)
    tid = K.create_task(conn3, title="card on a read-only wake path", assignee="platform")
    check("the board write itself still succeeds", status(conn3, tid) == "ready")

quiet = WakeMonitor(TMP / "never-created.db")
start = time.monotonic()
full = asyncio.run(wait_interval(quiet, 0.4))
check(
    "an absent wake file degrades to the full-interval poll",
    full is True and time.monotonic() - start >= 0.3,
)
check("shutdown still interrupts the wait", asyncio.run(wait_interval(quiet, 30.0, lambda: False)) is False)

print()
if FAILURES:
    print(f"verify_kanban_wake_nudge: {len(FAILURES)} FAILED")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("verify_kanban_wake_nudge: all checks passed")
