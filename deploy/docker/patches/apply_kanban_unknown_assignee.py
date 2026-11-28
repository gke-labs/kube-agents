#!/usr/bin/env python3
"""Wire tools/kanban_unknown_assignee.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. One anchored
edit in ``tools/kanban_tools.py`` plus an import trailer: the
``kanban_create`` handler consults the refusal right after it reads the
caller's own card (``self_task``), before ``kb.create_task`` writes anything.
The anchor is upstream's own line, the one ``apply_kanban_self_copy.py`` uses,
and each applier puts its gate directly after it, so the two apply in either
order.

Why the change is needed is documented in the module docstring of
``deploy/docker/patches/kanban_unknown_assignee.py``. Usage::

    python3 apply_kanban_unknown_assignee.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patchlib  # noqa: E402

RELATIVE = "tools/kanban_tools.py"

# Inside `with _board(...) as (kb, conn)` in the kanban_create handler, once
# the caller's own card is known and before the write.
ANCHOR = "        self_task = kb.get_task(conn, self_tid) if self_tid else None\n"
GATE = ANCHOR + (
    "        # kube-agents patch: no worker picks up a card whose assignee is\n"
    "        # not a profile, so it would wait in `ready` (#3786). See\n"
    "        # tools/kanban_unknown_assignee.py.\n"
    "        _unknown_assignee_err = _kanban_refuse_unknown_assignee(assignee)\n"
    "        if _unknown_assignee_err:\n"
    "            return tool_error(_unknown_assignee_err)\n"
)

TRAILER = (
    "\n\n# kube-agents patch: see tools/kanban_unknown_assignee.py\n"
    "from tools.kanban_unknown_assignee import (  # noqa: E402\n"
    "    refuse_unknown_assignee as _kanban_refuse_unknown_assignee,\n"
    ")\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix="kanban_unknown_assignee")
    patch.refuse_if_patched("_kanban_refuse_unknown_assignee(")
    patch.substitute(ANCHOR, GATE, label="kanban_create unknown-assignee gate")
    patch.append(TRAILER)
    patch.commit("1 anchor")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
