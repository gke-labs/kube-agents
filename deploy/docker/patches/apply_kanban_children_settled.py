#!/usr/bin/env python3
"""Wire tools/kanban_children_settled.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. Two anchored
edits in ``tools/kanban_tools.py`` plus an import trailer:

* the ``kanban_create`` handler gains ``_kanban_record_worker_child`` right
  after ``kanban_auto_subscribe``'s subscription-inheritance hook, so fan-out
  attribution and subscription inheritance describe the same set of cards;
* the ``kanban_complete`` handler gains the children gate immediately before
  ``kanban_result_required``'s result gate — refusing the completion outright
  is the more fundamental answer than critiquing the result's emptiness, and
  going first means the worker's one result-nudge is not spent on a
  completion that was never going to be accepted.

**Ordering: this applier must run AFTER ``apply_kanban_result_required.py``
and AFTER ``apply_kanban_auto_subscribe.py``.** Both anchors here are text
those patches introduced, on purpose: the sites this patch needs — "right
after subscription inheritance", "right before the result gate" — are only
addressable in the patched tree, and deriving anchors against a different
tree than the build produces is how an anchor rots undetected (the same
argument ``apply_kanban_notify_delivery.py`` makes for its ordering). Neither
anchor can drift silently: the completion anchor IS
``kanban_result_required.NEW_GATE``, imported, and the create anchor is
asserted at import time to be the last line of
``apply_kanban_auto_subscribe.PATCHED`` — so a change to either owning patch
breaks this one at import time, not at 3am in a build.

Why the change is needed is documented in the module docstring of
``deploy/docker/patches/kanban_children_settled.py``. Usage::

    python3 apply_kanban_children_settled.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patchlib  # noqa: E402
from apply_kanban_auto_subscribe import PATCHED as AUTO_SUBSCRIBE_PATCHED  # noqa: E402
from kanban_result_required import NEW_GATE as RESULT_GATE  # noqa: E402

RELATIVE = "tools/kanban_tools.py"

# The line apply_kanban_auto_subscribe.py inserts into the kanban_create
# handler. `conn` is open (the `kb.get_task(conn, new_tid)` two lines up) and
# `new_tid` is the created card. Asserted against the owning patch below so a
# rewrite there breaks this applier at import time rather than mid-build.
CREATE_ANCHOR = "            _kanban_inherit_worker_subs(conn, new_tid)\n"

if not AUTO_SUBSCRIBE_PATCHED.endswith(CREATE_ANCHOR):
    raise SystemExit(
        "apply_kanban_children_settled: apply_kanban_auto_subscribe.PATCHED no "
        "longer ends with the line this patch anchors on. Re-derive "
        "CREATE_ANCHOR against the auto-subscribe patch before building."
    )

CREATE_HOOK = CREATE_ANCHOR + (
    "            # kube-agents patch: remember which running card fanned this\n"
    "            # one out, so kanban_complete can refuse to hand back a\n"
    "            # dispatch receipt while it is unfinished (issue #1010).\n"
    "            # See tools/kanban_children_settled.py.\n"
    "            _kanban_record_worker_child(conn, new_tid)\n"
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
# kanban_auto_subscribe trailer uses.
TRAILER = (
    "\n\n# kube-agents patch: see tools/kanban_children_settled.py\n"
    "from hermes_cli.kanban_db import (  # noqa: E402\n"
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
