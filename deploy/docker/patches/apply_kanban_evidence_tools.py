#!/usr/bin/env python3
"""Wire tools/kanban_evidence_tools.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``, after
``apply_kanban_worker_tools.py``. One edit: a registration block inserted
on the line before upstream's kanban tool table, where ``registry``,
``tool_error`` and the worker-only gate are all already bound. Recording
evidence is specialist work in exactly the way closing a card is, so the
tools gate with ``_check_kanban_worker_mode`` — the gate the worker patch
imports into this file — and this applier refuses to run before that gate
exists rather than silently registering a front-door-wide tool surface.

**Located by AST, not by literal anchors.** Upstream registers its kanban
tools table-driven since v2026.9.14: a module-level ``_TOOLS`` tuple of
``(name, schema, handler, emoji)`` rows and one ``for`` loop that calls
``registry.register`` per row. There is no per-tool
``registry.register(name="kanban_show", ...)`` statement to sit above any
more, so the block lands on the line before ``_TOOLS`` instead: by then the
module has bound everything the block names, and the loop below registers
upstream's tools after ours. What the old anchor was also asserting — that
this is upstream's kanban table and not some other ``_TOOLS`` — is kept as
an expectation: the table must be a module-level tuple with a
``kanban_show`` row.

Why the tools exist is documented in the module docstring of
``deploy/docker/patches/kanban_evidence_tools.py``. Usage::

    python3 apply_kanban_evidence_tools.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patchlib  # noqa: E402

RELATIVE = "tools/kanban_tools.py"

#: The worker-only gate the kanban_worker_tools patch imports into the file.
WORKER_CHECK_FN = "_check_kanban_worker_mode"

#: Upstream's registration table; the block lands on the line before it.
TOOL_TABLE = "_TOOLS"

#: A row the table must carry for this to be upstream's kanban table.
UPSTREAM_TOOL = "kanban_show"

#: Position of the tool name inside a ``_TOOLS`` row.
ROW_NAME = 0

#: Text that exists only after a successful run; a second pass is refused.
APPLIED_MARKER = "_register_evidence_tools("

REGISTER_BLOCK = (
    "# kube-agents patch: see tools/kanban_evidence_tools.py\n"
    "from tools.kanban_evidence_tools import register as _register_evidence_tools\n"
    "\n"
    f"_register_evidence_tools(registry, {WORKER_CHECK_FN}, tool_error)\n"
    "\n"
)


def table_tool_names(patch: patchlib.Patch, table: patchlib.Assignment) -> set[str]:
    """The tool names upstream's table registers, or a loud refusal."""
    if table.node.col_offset != 0:
        raise patch._fail(
            f"{TOOL_TABLE} is not a module-level assignment; the registration "
            f"block needs registry, tool_error and {WORKER_CHECK_FN} bound at "
            f"import time. {patch.note}"
        )
    if not isinstance(table.node.value, ast.Tuple):
        raise patch._fail(
            f"{TOOL_TABLE} is {patchlib._render(table.node.value)}, not a tuple "
            f"of rows. {patch.note}"
        )
    return {
        row.elts[ROW_NAME].value
        for row in table.node.value.elts
        if isinstance(row, ast.Tuple)
        and row.elts
        and isinstance(row.elts[ROW_NAME], ast.Constant)
    }


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    if WORKER_CHECK_FN not in (root / RELATIVE).read_text(encoding="utf-8"):
        raise SystemExit(
            "kanban_evidence_tools patch: tools/kanban_tools.py does not bind "
            f"{WORKER_CHECK_FN}; run apply_kanban_worker_tools.py first."
        )
    patch = patchlib.Patch(root, RELATIVE, prefix="kanban_evidence_tools")
    patch.refuse_if_patched(APPLIED_MARKER)

    table = patch.find_assign(TOOL_TABLE, label="kanban tool table")
    names = table_tool_names(patch, table)
    if UPSTREAM_TOOL not in names:
        raise patch._fail(
            f"{TOOL_TABLE} carries no {UPSTREAM_TOOL} row, so it is not the "
            f"kanban tool table this patch was derived against. {patch.note}"
        )
    patch.insert(table.line_start, REGISTER_BLOCK)

    patch.commit("1 registration block")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
