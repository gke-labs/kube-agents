#!/usr/bin/env python3
"""Wire tools/kanban_fanout_reaper.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. One anchored edit
in ``hermes_cli/kanban_db_dispatch.py`` plus an import trailer: the
dispatcher's reclaim phase (``_run_reclaim_phase``) calls the reaper right
after upstream's ``enforce_max_runtime`` and before ``recompute_ready``, so a
settled child counts as settled on the same tick.

Why the change is needed is documented in the module docstring of
``deploy/docker/patches/kanban_fanout_reaper.py``. Usage::

    python3 apply_kanban_fanout_reaper.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patchlib  # noqa: E402

RELATIVE = "hermes_cli/kanban_db_dispatch.py"

# The reclaim phase's runtime enforcement, which only it has.
RECLAIM_ANCHOR = "    result.timed_out = enforce_max_runtime(conn)\n"
RECLAIM_PATCHED = RECLAIM_ANCHOR + (
    "    # kube-agents patch: settle a fan-out child that has run past its\n"
    "    # bound, heartbeat or not (#2978). See tools/kanban_fanout_reaper.py.\n"
    "    _kanban_reap_overdue_fanout(conn)\n"
)

# Appended rather than inserted: the name is resolved when the reclaim phase
# runs, long after the module finishes importing.
TRAILER = (
    "\n\n# kube-agents patch: see tools/kanban_fanout_reaper.py\n"
    "from tools.kanban_fanout_reaper import (  # noqa: E402\n"
    "    reap_overdue_fanout_children as _kanban_reap_overdue_fanout,\n"
    ")\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix="kanban_fanout_reaper")
    patch.refuse_if_patched("_kanban_reap_overdue_fanout(conn)")
    patch.substitute(RECLAIM_ANCHOR, RECLAIM_PATCHED, label="reclaim-phase hook")
    patch.append(TRAILER)
    patch.commit("1 anchor")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
