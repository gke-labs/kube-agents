#!/usr/bin/env python3
"""Wire tools/kanban_ended_run.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. Two files:

* ``hermes_cli/kanban_db.py`` gains a trailer that rebinds ``block_task`` to
  :func:`stop_worker_on_external_block`'s wrapper. Every caller looks the
  name up on the module (``kb.block_task``, ``kanban_db.block_task``) or
  calls it from inside the module, so all of them get the wrapper.
* ``tools/kanban_tools.py`` gains four anchored edits plus an import trailer:
  the ended-run check right after ``_worker_guard`` in the ``kanban_complete``
  and ``kanban_block`` handlers, ahead of every gate, and the ended-run text
  in place of the reason each handler reports when the board refuses it.

**Ordering: run AFTER ``apply_kanban_children_settled.py`` and
``apply_kanban_result_required.py``.** Their gates sit below ``_worker_guard``
and this check must come before them, which inserting right after the guard
line gives in either order. The refusal anchors are upstream's own lines.

Why the change is needed is documented in the module docstring of
``deploy/docker/patches/kanban_ended_run.py``. Usage::

    python3 apply_kanban_ended_run.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patchlib  # noqa: E402

DB_RELATIVE = "hermes_cli/kanban_db.py"
TOOLS_RELATIVE = "tools/kanban_tools.py"

DB_TRAILER = (
    "\n\n# kube-agents patch: a block from outside the card's run stops the\n"
    "# run's worker (#2947). See tools/kanban_ended_run.py.\n"
    "import sys as _kanban_sys  # noqa: E402\n"
    "from tools.kanban_ended_run import (  # noqa: E402\n"
    "    stop_worker_on_external_block as _kanban_stop_worker_on_external_block,\n"
    ")\n"
    "block_task = _kanban_stop_worker_on_external_block(\n"
    "    _kanban_sys.modules[__name__], block_task\n"
    ")\n"
    "from tools.kanban_ended_run import (  # noqa: E402\n"
    "    saved_result_banner as _kanban_saved_result_banner,\n"
    ")\n"
)

# The top of the worker context, before the header: a re-claimed card whose
# previous run saved its result and blocked on a dependency reads that first
# (#3252).
CONTEXT_ANCHOR = "    lines: list[str] = []\n    _ctx_header(lines, task)\n"
CONTEXT_PATCHED = (
    "    lines: list[str] = []\n"
    "    # kube-agents patch: a saved result from a re-queued run leads (#3252).\n"
    "    lines.extend(_kanban_saved_result_banner(conn, task_id))\n"
    "    _ctx_header(lines, task)\n"
)


def _gate(tool: str) -> tuple[str, str]:
    anchor = f'    tid = _worker_guard("{tool}", args)\n'
    return anchor, anchor + (
        "    # kube-agents patch: a worker whose run has ended gets one terminal\n"
        "    # answer, before any gate runs (#2947). See tools/kanban_ended_run.py.\n"
        "    _ended_err = _kanban_refuse_ended_run(tid, _kanban_ended_run_connect)\n"
        "    if _ended_err:\n"
        "        return tool_error(_ended_err)\n"
    )


COMPLETE_GATE = _gate("kanban_complete")
BLOCK_GATE = _gate("kanban_block")

# Upstream reports the card's last_failure_error, which belongs to an earlier
# run whenever this worker's own run is still going, except for this run's
# PR-acceptance refusal, which current_run_failure passes through. `_check`
# is a plain call that evaluates its message either way, so the ended-run
# lookup sits behind `ok`: on a successful handoff the card has just left
# `running`, and the lookup would latch "your run is over" for a run that
# ended by finishing.
COMPLETE_REFUSAL = (
    "        _check(ok, (task.last_failure_error if task else None) or\n"
    '               f"could not complete {tid} (unknown id, stale run, or already terminal)")\n',
    "        # kube-agents patch: never another run's failure (#2947).\n"
    '        _check(ok, "" if ok else (\n'
    "            _kanban_ended_run_refusal(tid, conn) or\n"
    "            _kanban_waiting_parent_refusal(tid, conn) or\n"
    "            _kanban_current_run_failure(tid, conn) or\n"
    '            f"could not complete {tid} (unknown id, stale run, or already terminal)"))\n',
)

BLOCK_REFUSAL = (
    '        _check(ok, f"could not block {tid} (unknown id or not in running/ready)")\n',
    "        # kube-agents patch: say plainly when the run is over (#2947).\n"
    '        _check(ok, "" if ok else (\n'
    "            _kanban_ended_run_refusal(tid, conn) or\n"
    '            f"could not block {tid} (unknown id or not in running/ready)"))\n',
)

TOOLS_TRAILER = (
    "\n\n# kube-agents patch: see tools/kanban_ended_run.py\n"
    "from hermes_cli.kanban_db_connect import (  # noqa: E402\n"
    "    connect as _kanban_ended_run_connect,\n"
    ")\n"
    "from tools.kanban_ended_run import (  # noqa: E402\n"
    "    current_run_failure as _kanban_current_run_failure,\n"
    "    ended_run_refusal as _kanban_ended_run_refusal,\n"
    "    refuse_ended_run as _kanban_refuse_ended_run,\n"
    "    waiting_parent_refusal as _kanban_waiting_parent_refusal,\n"
    ")\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    db = patchlib.Patch(root, DB_RELATIVE, prefix="kanban_ended_run")
    db.refuse_if_patched("_kanban_stop_worker_on_external_block")
    db.substitute(CONTEXT_ANCHOR, CONTEXT_PATCHED, label="worker-context saved-result banner")
    db.append(DB_TRAILER)

    tools = patchlib.Patch(root, TOOLS_RELATIVE, prefix="kanban_ended_run")
    tools.refuse_if_patched("_kanban_refuse_ended_run(")
    tools.substitute(*COMPLETE_GATE, label="kanban_complete ended-run gate")
    tools.substitute(*BLOCK_GATE, label="kanban_block ended-run gate")
    tools.substitute(*COMPLETE_REFUSAL, label="kanban_complete refusal text")
    tools.substitute(*BLOCK_REFUSAL, label="kanban_block refusal text")
    tools.append(TOOLS_TRAILER)

    db.commit("block_task wrapper, 1 anchor")
    tools.commit("4 anchors")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
