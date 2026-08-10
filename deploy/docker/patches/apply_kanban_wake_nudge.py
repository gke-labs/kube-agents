#!/usr/bin/env python3
"""Wire hermes_cli/kanban_wake_nudge.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. Seven anchored
edits across two files plus an import trailer on each:

``hermes_cli/kanban_db.py`` — the producers. One nudge after each commit that
gives a watcher something to do:

1. ``create_task`` — after the insert txn exits (a claimable card exists).
2. ``complete_task`` — after ``recompute_ready``, so the nudge also covers
   children the completion just promoted.
3. ``unblock_task`` — the upstream ``return True`` sits *inside* the write
   txn (returning through the context manager commits first), so the edit
   dedents it out: txn exits → commit → nudge → return. The early
   ``return False`` path is untouched.

``gateway/kanban_watchers.py`` — the consumers. Per watcher loop, one
``WakeMonitor`` constructed where the loop starts (passed
``_kb.kanban_db_path`` uncalled, so resolution failures degrade the monitor
instead of killing the coroutine), and the fixed sleep swapped for
``wait_interval``:

4. notifier monitor construction        5. notifier sleep swap
6. dispatcher monitor construction      7. dispatcher sleep swap

Deliberately NOT hooked: ``claim_task``, ``heartbeat_claim``, and the rest of
what a dispatcher tick writes — nudging on those would let the dispatcher
wake itself into a busy loop.

Why the change is needed is documented in the module docstring of
``deploy/docker/patches/kanban_wake_nudge.py``. Usage::

    python3 apply_kanban_wake_nudge.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

DB_RELATIVE = "hermes_cli/kanban_db.py"
WATCHERS_RELATIVE = "gateway/kanban_watchers.py"

# --- hermes_cli/kanban_db.py -------------------------------------------------

CREATE_ANCHOR = (
    "                _inherit_notify_subs(conn, task_id, parents, created_at=now)\n"
    "            return task_id\n"
)

CREATE_PATCHED = (
    "                _inherit_notify_subs(conn, task_id, parents, created_at=now)\n"
    "            # kube-agents patch: the txn above just committed a claimable\n"
    "            # card; nudge the dispatcher so it reacts now, not next tick.\n"
    "            # See hermes_cli/kanban_wake_nudge.py.\n"
    "            _kanban_record_wake(conn)\n"
    "            return task_id\n"
)

COMPLETE_ANCHOR = (
    "    # Recompute ready status for dependents (separate txn so children see done).\n"
    "    recompute_ready(conn)\n"
)

COMPLETE_PATCHED = (
    "    # Recompute ready status for dependents (separate txn so children see done).\n"
    "    recompute_ready(conn)\n"
    "    # kube-agents patch: completion and any child promotion are committed;\n"
    "    # nudge the watchers so the notifier delivers and the dispatcher claims\n"
    "    # dependents now, not next tick. See hermes_cli/kanban_wake_nudge.py.\n"
    "    _kanban_record_wake(conn)\n"
)

UNBLOCK_ANCHOR = (
    "        _append_event(\n"
    '            conn, task_id, "unblocked",\n'
    '            {"status": new_status} if new_status != "ready" else None,\n'
    "        )\n"
    "        return True\n"
)

UNBLOCK_PATCHED = (
    "        _append_event(\n"
    '            conn, task_id, "unblocked",\n'
    '            {"status": new_status} if new_status != "ready" else None,\n'
    "        )\n"
    "    # kube-agents patch: the return is dedented out of the write txn so the\n"
    "    # nudge runs after the commit, not inside it — a watcher woken mid-txn\n"
    "    # would scan pre-commit state and burn the edge for nothing.\n"
    "    # See hermes_cli/kanban_wake_nudge.py.\n"
    "    _kanban_record_wake(conn)\n"
    "    return True\n"
)

DB_TRAILER = (
    "\n\n# kube-agents patch: see hermes_cli/kanban_wake_nudge.py\n"
    "from hermes_cli.kanban_wake_nudge import (  # noqa: E402\n"
    "    record_wake_for_conn as _kanban_record_wake,\n"
    ")\n"
)

# --- gateway/kanban_watchers.py ----------------------------------------------

NOTIFIER_MONITOR_ANCHOR = (
    "        # Initial delay so the gateway can finish wiring adapters.\n"
    "        await asyncio.sleep(5)\n"
    "\n"
    "        while self._running:\n"
)

NOTIFIER_MONITOR_PATCHED = (
    "        # Initial delay so the gateway can finish wiring adapters.\n"
    "        await asyncio.sleep(5)\n"
    "\n"
    "        # kube-agents patch: cross-process wake monitor, one per watcher\n"
    "        # loop. See hermes_cli/kanban_wake_nudge.py.\n"
    "        _wake_monitor = _KanbanWakeMonitor(_kb.kanban_db_path)\n"
    "        while self._running:\n"
)

NOTIFIER_SLEEP_ANCHOR = (
    "            # Sleep with cancellation checks.\n"
    "            for _ in range(int(max(1, interval))):\n"
    "                if not self._running:\n"
    "                    return\n"
    "                await asyncio.sleep(1)\n"
)

NOTIFIER_SLEEP_PATCHED = (
    "            # kube-agents patch: wake-aware wait — reacts to a board\n"
    "            # mutation in <=0.25s, degrades to the full-interval poll when\n"
    "            # the wake file is unavailable, and returns False on shutdown\n"
    "            # exactly where the old loop returned.\n"
    "            # See hermes_cli/kanban_wake_nudge.py.\n"
    "            if not await _kanban_wait_interval(\n"
    "                _wake_monitor, interval, lambda: self._running\n"
    "            ):\n"
    "                return\n"
)

DISPATCHER_MONITOR_ANCHOR = (
    "        logger.info(\n"
    '            "kanban dispatcher: embedded in gateway (interval=%.1fs)", interval\n'
    "        )\n"
    "        while self._running:\n"
)

DISPATCHER_MONITOR_PATCHED = (
    "        logger.info(\n"
    '            "kanban dispatcher: embedded in gateway (interval=%.1fs)", interval\n'
    "        )\n"
    "        # kube-agents patch: cross-process wake monitor, one per watcher\n"
    "        # loop. See hermes_cli/kanban_wake_nudge.py.\n"
    "        _wake_monitor = _KanbanWakeMonitor(_kb.kanban_db_path)\n"
    "        while self._running:\n"
)

DISPATCHER_SLEEP_ANCHOR = (
    "            # Sleep in 1s slices so shutdown is snappy — otherwise a stop()\n"
    "            # waits up to `interval` seconds for the current sleep to finish.\n"
    "            slept = 0.0\n"
    "            while slept < interval and self._running:\n"
    "                await asyncio.sleep(min(1.0, interval - slept))\n"
    "                slept += 1.0\n"
)

DISPATCHER_SLEEP_PATCHED = (
    "            # kube-agents patch: wake-aware wait; still checks self._running\n"
    "            # before every slice so shutdown stays snappy.\n"
    "            # See hermes_cli/kanban_wake_nudge.py.\n"
    "            await _kanban_wait_interval(\n"
    "                _wake_monitor, interval, lambda: self._running\n"
    "            )\n"
)

WATCHERS_TRAILER = (
    "\n\n# kube-agents patch: see hermes_cli/kanban_wake_nudge.py\n"
    "from hermes_cli.kanban_wake_nudge import (  # noqa: E402\n"
    "    WakeMonitor as _KanbanWakeMonitor,\n"
    "    wait_interval as _kanban_wait_interval,\n"
    ")\n"
)

FILES = (
    (
        DB_RELATIVE,
        (
            ("create_task nudge", CREATE_ANCHOR, CREATE_PATCHED),
            ("complete_task nudge", COMPLETE_ANCHOR, COMPLETE_PATCHED),
            ("unblock_task nudge", UNBLOCK_ANCHOR, UNBLOCK_PATCHED),
        ),
        DB_TRAILER,
    ),
    (
        WATCHERS_RELATIVE,
        (
            ("notifier monitor", NOTIFIER_MONITOR_ANCHOR, NOTIFIER_MONITOR_PATCHED),
            ("notifier sleep", NOTIFIER_SLEEP_ANCHOR, NOTIFIER_SLEEP_PATCHED),
            ("dispatcher monitor", DISPATCHER_MONITOR_ANCHOR, DISPATCHER_MONITOR_PATCHED),
            ("dispatcher sleep", DISPATCHER_SLEEP_ANCHOR, DISPATCHER_SLEEP_PATCHED),
        ),
        WATCHERS_TRAILER,
    ),
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    for relative, edits, trailer in FILES:
        patch = patchlib.Patch(root, relative, prefix="kanban_wake_nudge")
        for label, anchor, patched in edits:
            patch.substitute(anchor, patched, label=label)
        patch.append(trailer)
        patch.commit(f"{len(edits)} anchors")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
