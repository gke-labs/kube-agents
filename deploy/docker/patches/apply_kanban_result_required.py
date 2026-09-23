#!/usr/bin/env python3
"""Wire tools/kanban_result_required.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. Two edits: the
completion gate, and an import/schema-fixup block placed immediately before the
``_TOOLS`` table that upstream registers ``kanban_complete`` from.

The schema wording is NOT edited textually — ``apply_schema`` rewrites the live
``KANBAN_COMPLETE_SCHEMA`` dict at import time. Placing the call before the
registration loop rather than at end-of-file means the registry cannot capture
the pre-patch wording even if it copies the dict it is handed. Since
v2026.9.14 the dict itself lives in ``tools/kanban_tools_schemas.py`` and is
imported into ``kanban_tools``; the fixup rewrites that one object in place,
so both modules see the new wording.

The gate is a literal anchor because the text being replaced *is* the edit. The
registration is not: it is only an insertion point. Upstream no longer has a
``registry.register(name="kanban_complete", ...)`` statement to locate — it
registers table-driven, one ``registry.register`` in a ``for`` over a
``_TOOLS`` tuple of ``(name, schema, handler, emoji)`` rows — so the insertion
point is the ``_TOOLS`` assignment, and the ``schema=`` assertion the old
``find_call(...).expect`` made is now made on the ``kanban_complete`` row of
that tuple: the fixup is inserted to rewrite this exact dict, and a row wired
to a different one would leave it editing nothing.

Why the change is needed is documented in the module docstring of
``deploy/docker/patches/kanban_result_required.py``. Usage::

    python3 apply_kanban_result_required.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patchlib  # noqa: E402
from kanban_result_required import NEW_GATE, OLD_GATE  # noqa: E402

RELATIVE = "tools/kanban_tools.py"

#: The tool whose registration the fixup has to precede, and the schema
#: constant its ``_TOOLS`` row must still name.
REGISTERED_TOOL = "kanban_complete"
SCHEMA_CONSTANT = "KANBAN_COMPLETE_SCHEMA"

#: Upstream's registration table, and the positions inside one of its rows.
TOOL_TABLE = "_TOOLS"
ROW_NAME, ROW_SCHEMA = 0, 1
ROW_MIN_LEN = 2

# Imported at module scope so ``apply_schema`` runs while the module is still
# importing — before any tool call and before the registry hands the schema to a
# model. ``_require_result`` and ``_blank_to_none`` are resolved later, when a
# worker actually completes.
REGISTER_PREAMBLE = (
    "# kube-agents patch: see tools/kanban_result_required.py\n"
    "from tools.kanban_result_required import (\n"
    "    apply_schema as _apply_result_schema,\n"
    "    blank_to_none as _blank_to_none,\n"
    "    require_result as _require_result,\n"
    ")\n"
    "\n"
    f"_apply_result_schema({SCHEMA_CONSTANT})\n"
    "\n"
)


def expect_schema_row(patch: patchlib.Patch, table: patchlib.Assignment) -> None:
    """Assert the ``kanban_complete`` row of ``_TOOLS`` still names the schema.

    The half of the old ``find_call(...).expect(schema=...)`` worth keeping:
    the fixup below rewrites ``KANBAN_COMPLETE_SCHEMA`` in place, and a
    registration that had been rewired to a different dict would leave it
    editing nothing while the build reported success.
    """
    if not isinstance(table.node.value, ast.Tuple):
        raise patch._fail(
            f"{TOOL_TABLE} is {patchlib._render(table.node.value)}, not a "
            f"tuple of rows. {patch.note}"
        )
    rows = [
        row
        for row in table.node.value.elts
        if isinstance(row, ast.Tuple)
        and len(row.elts) >= ROW_MIN_LEN
        and isinstance(row.elts[ROW_NAME], ast.Constant)
        and row.elts[ROW_NAME].value == REGISTERED_TOOL
    ]
    if len(rows) != 1:
        raise patch._fail(
            f"expected 1 {TOOL_TABLE} row for {REGISTERED_TOOL}, found "
            f"{len(rows)}. {patch.note}"
        )
    schema = rows[0].elts[ROW_SCHEMA]
    if not (isinstance(schema, ast.Name) and schema.id == SCHEMA_CONSTANT):
        raise patch._fail(
            f"the {REGISTERED_TOOL} row names schema {patchlib._render(schema)} "
            f"where {SCHEMA_CONSTANT} was expected. {patch.note}"
        )


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix="kanban_result_required")

    patch.substitute(OLD_GATE, NEW_GATE, label="gate")

    table = patch.find_assign(TOOL_TABLE, label=f"{REGISTERED_TOOL} registration")
    expect_schema_row(patch, table)
    patch.insert(table.line_start, REGISTER_PREAMBLE)

    patch.commit("1 anchor + 1 registration")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
