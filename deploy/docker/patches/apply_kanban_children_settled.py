#!/usr/bin/env python3
"""Wire tools/kanban_children_settled.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. Two anchored
edits in ``tools/kanban_tools.py`` plus an import trailer:

* the ``kanban_create`` handler gains ``_kanban_record_worker_child`` right
  after the created card is read back — the same upstream line
  ``kanban_auto_subscribe`` hooks its subscription inheritance on, so fan-out
  attribution and subscription inheritance describe the same set of cards
  whichever of the two runs first;
* the ``kanban_complete`` handler gains the children gate immediately before
  ``kanban_result_required``'s result gate — refusing the completion outright
  is the more fundamental answer than critiquing the result's emptiness, and
  going first means the worker's one result-nudge is not spent on a
  completion that was never going to be accepted.

**Ordering: this applier must run AFTER ``apply_kanban_result_required.py``.**
The completion anchor IS ``kanban_result_required.NEW_GATE``, imported, so a
change to that patch breaks this one at import time rather than mid-build.
It no longer needs ``apply_kanban_auto_subscribe.py`` to have run: since
v2026.9.14 upstream's ``create_task`` inherits the creator card's
subscriptions itself, that patch is a candidate for retirement, and anchoring
on its inserted line would have made retiring it a build break here. Both
hooks anchor on upstream's own line instead and stack in either order.

Why the change is needed is documented in the module docstring of
``deploy/docker/patches/kanban_children_settled.py``. Usage::

    python3 apply_kanban_children_settled.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patchlib  # noqa: E402
from kanban_result_required import NEW_GATE as RESULT_GATE  # noqa: E402

RELATIVE = "tools/kanban_tools.py"

# The line in the kanban_create handler that reads the created card back.
# `conn` is open (inside `with _board(...) as (kb, conn)`) and `new_tid` is the
# created card; `kb.create_task` has returned, so the child exists. The same
# line apply_kanban_auto_subscribe.py anchors on, by design (see above).
CREATE_ANCHOR = "        landed = _fields(kb.get_task(conn, new_tid), _CREATED_FIELDS)\n"

CREATE_HOOK = CREATE_ANCHOR + (
    "        # kube-agents patch: remember which running card fanned this\n"
    "        # one out, so kanban_complete can refuse to hand back a\n"
    "        # dispatch receipt while it is unfinished (issue #1010).\n"
    "        # See tools/kanban_children_settled.py.\n"
    "        _kanban_record_worker_child(conn, new_tid)\n"
)

COMPLETE_GATE = (
    "    # kube-agents patch: completing IS the delivery, and a card whose\n"
    "    # fanned-out children are still running has no answer to deliver yet.\n"
    "    # The submitted result rides along so a refusal preserves it on the\n"
    "    # card first. See tools/kanban_children_settled.py (issue #1010).\n"
    "    _children_err = _require_children_settled(\n"
    "        tid, _kanban_children_connect, result\n"
    "    )\n"
    "    if _children_err:\n"
    "        return tool_error(_children_err)\n"
)

# Appended rather than inserted: every name is resolved when a tool handler
# runs, long after the module finishes importing. Same placement the
# kanban_auto_subscribe trailer uses. `connect` comes from
# hermes_cli.kanban_db_connect, where upstream moved it in the Sep 2026
# decomposition; the old hermes_cli.kanban_db path is a plugin-compat shim
# that warns and is scheduled for removal.
TRAILER = (
    "\n\n# kube-agents patch: see tools/kanban_children_settled.py\n"
    "from hermes_cli.kanban_db_connect import (  # noqa: E402\n"
    "    connect as _kanban_children_connect,\n"
    ")\n"
    "from tools.kanban_children_settled import (  # noqa: E402\n"
    "    maybe_record_worker_child as _kanban_record_worker_child,\n"
    "    require_children_settled as _require_children_settled,\n"
    ")\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix="kanban_children_settled")
    # Both edits keep their anchor text (the hooks are added beside it), so
    # anchor-counting alone cannot catch a re-run. Refuse explicitly rather
    # than stack second hooks and a second trailer import.
    patch.refuse_if_patched(
        "_kanban_record_worker_child(conn, new_tid)",
        "_require_children_settled(",
    )
    patch.substitute(CREATE_ANCHOR, CREATE_HOOK, label="create attribution hook")
    patch.substitute(
        RESULT_GATE, COMPLETE_GATE + RESULT_GATE, label="completion gate"
    )
    patch.append(TRAILER)
    patch.commit("2 anchors")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
