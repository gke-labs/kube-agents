#!/usr/bin/env python3
"""Wire tools/kanban_result_required.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. Two edits: the
completion gate, and an import/schema-fixup block placed immediately before
``kanban_complete`` is registered.

The schema wording is NOT edited textually — ``apply_schema`` rewrites the live
``KANBAN_COMPLETE_SCHEMA`` dict at import time. Placing the call before
``registry.register`` rather than at end-of-file means the registry cannot
capture the pre-patch wording even if it copies the dict it is handed.

The gate is a literal anchor because the text being replaced *is* the edit. The
registration is not: it is only an insertion point, and the four-line slice that
used to name it (``registry.register(``, then ``name=``, ``toolset=``,
``schema=``, because ``registry.register(\\n    name=`` recurs for every kanban
tool) was pure layout. ``find_call`` asks for the statement that registers
``kanban_complete`` instead, which is the same requirement stated in a way an
upstream reformat cannot break, and ``expect`` keeps the ``schema=`` assertion
the old anchor's fourth line was really there for.

Why the change is needed is documented in the module docstring of
``deploy/docker/patches/kanban_result_required.py``. Usage::

    python3 apply_kanban_result_required.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patchlib  # noqa: E402
from kanban_result_required import NEW_GATE, OLD_GATE  # noqa: E402

RELATIVE = "tools/kanban_tools.py"

#: The tool whose registration the fixup has to precede.
REGISTERED_TOOL = "kanban_complete"

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
    "_apply_result_schema(KANBAN_COMPLETE_SCHEMA)\n"
    "\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix="kanban_result_required")

    patch.substitute(OLD_GATE, NEW_GATE, label="gate")

    site = patch.find_call(
        "registry.register",
        label=f"{REGISTERED_TOOL} registration",
        name=REGISTERED_TOOL,
    )
    # The schema constant is not decoration here: `_apply_result_schema` is
    # inserted to rewrite this exact dict, and a registration that had been
    # rewired to a different one would leave the fixup editing nothing.
    site.expect(toolset="kanban", schema=patchlib.Ident("KANBAN_COMPLETE_SCHEMA"))
    patch.insert(site.start, REGISTER_PREAMBLE)

    patch.commit("1 anchor + 1 registration")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
