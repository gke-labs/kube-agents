#!/usr/bin/env python3
"""Wire tools/kanban_self_copy.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. One anchored
edit in ``tools/kanban_tools.py`` plus an import trailer: the
``kanban_create`` handler consults the refusal right after it reads the
caller's own card (``self_task``), before ``kb.create_task`` writes anything.
The anchor is upstream's own line, so the order against the other
``kanban_create`` patches (wake guard, priority, attribution) doesn't matter.

Why the change is needed is documented in the module docstring of
``deploy/docker/patches/kanban_self_copy.py``. Usage::

    python3 apply_kanban_self_copy.py [HERMES_ROOT]   # default /opt/hermes
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
    "        # kube-agents patch: a platform worker's card for its own profile\n"
    "        # that would run alongside it redoes the job (#3164). See\n"
    "        # tools/kanban_self_copy.py.\n"
    "        _self_copy_err = _kanban_refuse_self_copy(self_task, assignee, parents)\n"
    "        if _self_copy_err:\n"
    "            return tool_error(_self_copy_err)\n"
)

TRAILER = (
    "\n\n# kube-agents patch: see tools/kanban_self_copy.py\n"
    "from tools.kanban_self_copy import (  # noqa: E402\n"
    "    refuse_self_copy as _kanban_refuse_self_copy,\n"
    ")\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix="kanban_self_copy")
    patch.refuse_if_patched("_kanban_refuse_self_copy(")
    patch.substitute(ANCHOR, GATE, label="kanban_create self-copy gate")
    patch.append(TRAILER)
    patch.commit("1 anchor")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
