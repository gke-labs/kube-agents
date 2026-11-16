#!/usr/bin/env python3
"""Wire tools/kanban_link_running_guard.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. One anchored edit
in ``tools/kanban_tools.py``, plus its import: ``_handle_link`` checks the gate
inside its board block, just before upstream's ``kb.link_tasks`` call, so the
guard reads the same board the link would write.

Why the change is needed is documented in the module docstring of
``deploy/docker/patches/kanban_link_running_guard.py``. Usage::

    python3 apply_kanban_link_running_guard.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patchlib  # noqa: E402

RELATIVE = "tools/kanban_tools.py"

# upstream's link call in _handle_link, which only it has.
LINK_ANCHOR = "        kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)\n"
LINK_PATCHED = (
    "        # kube-agents patch: a link gates its child behind the parent, so\n"
    "        # linking a card that has already started strands its run (#3248).\n"
    "        # See tools/kanban_link_running_guard.py.\n"
    "        _link_err = _kanban_refuse_started_child_link(kb, conn, parent_id, child_id)\n"
    "        if _link_err:\n"
    "            return tool_error(_link_err)\n"
) + LINK_ANCHOR

TRAILER = (
    "\n\n# kube-agents patch: see tools/kanban_link_running_guard.py\n"
    "from tools.kanban_link_running_guard import (  # noqa: E402\n"
    "    refuse_started_child_link as _kanban_refuse_started_child_link,\n"
    ")\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    tools = patchlib.Patch(root, RELATIVE, prefix="kanban_link_running_guard")
    tools.refuse_if_patched("_kanban_refuse_started_child_link(")
    tools.substitute(LINK_ANCHOR, LINK_PATCHED, label="link gate")
    tools.append(TRAILER)
    tools.commit("1 anchor")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
