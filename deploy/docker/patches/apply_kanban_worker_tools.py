#!/usr/bin/env python3
"""Wire tools/kanban_worker_tools.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. Eight edits in one
file — an import plus one ``check_fn`` per worker-only tool — with the same
guarantee as the other patches here: every edit site must be found exactly once,
the file must still parse, and anything else fails the build loudly rather than
shipping a half-patched image.

**Located by AST, not by literal anchors.** Every site here is a node with a
name: the ``registry.register`` call that registers ``kanban_complete``, the
``def`` the import has to land after. This applier used to spell each of those
out as an exact slice of upstream source — five lines per tool, seven tools —
which meant the registration block was the single largest concentration of
literal anchors in the tree, and reformatting it (or adding one keyword
argument, or reordering two) would have broken the build on eight of them at
once. Eight anchors became one locator apiece and none of them care about
layout.

What the anchors *were* doing besides finding the call is preserved:
``expect()`` re-asserts the toolset, schema and handler that the five-line slice
used to pin, so a tool upstream has rewired underneath us still fails the build
instead of being silently re-gated. See ``patchlib.CallSite.expect``.

Why the change is needed is documented in the module docstring of
``deploy/docker/patches/kanban_worker_tools.py``. Usage::

    python3 apply_kanban_worker_tools.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib
from kanban_worker_tools import WORKER_ONLY_TOOLS

RELATIVE = "tools/kanban_tools.py"

# The import lands after this function rather than at the end of the file the
# way the kanban_notifier patch does. `check_fn=` is evaluated at import time,
# several hundred lines above the end of the module, so a trailing import would
# raise NameError before it ever ran. `_check_kanban_orchestrator_mode` is the
# last definition above the registration block that this patch has a reason to
# name — it is the gate `check_kanban_worker_mode` mirrors.
IMPORT_AFTER = "_check_kanban_orchestrator_mode"

IMPORT_BLOCK = (
    "\n\n"
    "# kube-agents patch: see tools/kanban_worker_tools.py\n"
    "from tools.kanban_worker_tools import (\n"
    "    check_kanban_worker_mode as _check_kanban_worker_mode,\n"
    ")\n"
)

#: The gate upstream ships on every one of these tools, and the one this patch
#: swaps it for. Asserting the old value is what makes a second run fail.
UPSTREAM_CHECK_FN = "_check_kanban_mode"
WORKER_CHECK_FN = "_check_kanban_worker_mode"

# Handler name per tool. Once part of the anchor text; now an expectation, so
# that a renamed handler fails with "sets handler=X where Y was expected"
# rather than disappearing into a "found 0".
HANDLERS = {
    "kanban_complete": "_handle_complete",
    "kanban_block": "_handle_block",
    "kanban_heartbeat": "_handle_heartbeat",
    "kanban_link": "_handle_link",
    "kanban_attach": "_handle_attach",
    "kanban_attach_url": "_handle_attach_url",
    "kanban_attachments": "_handle_attachments",
}


def registration_expectations(tool: str) -> dict:
    """The arguments a worker-only ``registry.register`` call must still pass."""
    return {
        "toolset": "kanban",
        "schema": patchlib.Ident(f"{tool.upper()}_SCHEMA"),
        "handler": patchlib.Ident(HANDLERS[tool]),
        "check_fn": patchlib.Ident(UPSTREAM_CHECK_FN),
    }


def check_handler_mapping() -> None:
    """Refuse to run if this applier and kanban_worker_tools.py have drifted."""
    missing = set(WORKER_ONLY_TOOLS) - set(HANDLERS)
    if missing:
        raise SystemExit(
            "kanban_worker_tools patch: no handler mapping for "
            f"{', '.join(sorted(missing))} — kanban_worker_tools.py and this "
            "applier have drifted apart."
        )


def apply(root: Path) -> None:
    """Apply every edit under ``root``, or raise SystemExit with the reason."""
    check_handler_mapping()
    patch = patchlib.Patch(root, RELATIVE, prefix="kanban_worker_tools")

    gate = patch.find_def(IMPORT_AFTER, label="worker-gate import site")
    patch.insert(gate.after, IMPORT_BLOCK)

    for tool in WORKER_ONLY_TOOLS:
        site = patch.find_call(
            "registry.register", label=f"{tool} registration", name=tool
        )
        site.expect(**registration_expectations(tool))
        start, end = site.keyword_span("check_fn")
        patch.splice(start, end, WORKER_CHECK_FN)

    patch.commit(f"1 import + {len(WORKER_ONLY_TOOLS)} registrations")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
