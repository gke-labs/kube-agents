#!/usr/bin/env python3
"""Wire tools/kanban_audit_ledger.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. One anchored edit
in ``tools/kanban_tools.py`` plus an import trailer: the ``kanban_complete``
handler gains the audit-ledger gate immediately after
``kanban_result_required``'s result gate. After it, because that gate makes
``result`` carry the report, and this one appends to or prefixes that report.

**Ordering: this applier must run AFTER ``apply_kanban_result_required.py``.**
The anchor IS ``kanban_result_required.NEW_GATE``, imported, so a change to
that patch breaks this one at import time rather than mid-build.
``apply_kanban_children_settled.py`` inserts its gates before the same anchor
and leaves it in place, so the two stack in either order.

Why the change is needed is documented in the module docstring of
``deploy/docker/patches/kanban_audit_ledger.py``. Usage::

    python3 apply_kanban_audit_ledger.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patchlib  # noqa: E402
from kanban_result_required import NEW_GATE as RESULT_GATE  # noqa: E402

RELATIVE = "tools/kanban_tools.py"

AUDIT_GATE = (
    "    # kube-agents patch: a fleet-audit card whose run published no ledger\n"
    "    # is not done (#2856). See tools/kanban_audit_ledger.py.\n"
    "    _audit_err, result = _require_audit_ledger(\n"
    "        tid, _kanban_audit_connect, result\n"
    "    )\n"
    "    if _audit_err:\n"
    "        return tool_error(_audit_err)\n"
)

TRAILER = (
    "\n\n# kube-agents patch: see tools/kanban_audit_ledger.py\n"
    "from hermes_cli.kanban_db_connect import (  # noqa: E402\n"
    "    connect as _kanban_audit_connect,\n"
    ")\n"
    "from tools.kanban_audit_ledger import (  # noqa: E402\n"
    "    require_audit_ledger as _require_audit_ledger,\n"
    ")\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix="kanban_audit_ledger")
    # The edit keeps its anchor text, so anchor counting alone cannot catch a
    # re-run.
    patch.refuse_if_patched("_require_audit_ledger(")
    patch.substitute(RESULT_GATE, RESULT_GATE + AUDIT_GATE, label="audit ledger gate")
    patch.append(TRAILER)
    patch.commit("1 anchor")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
