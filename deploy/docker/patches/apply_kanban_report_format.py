#!/usr/bin/env python3
"""Wire tools/kanban_report_format.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. One edit: the card
body a worker is handed at ``kanban_create`` gains the report-format stanza when
it carries no format instructions of its own.

Why the body and not the persona: the persona is read once at the top of a run
and then competes with several thousand tokens of immediate task text, which is
why the fan-out of 2026-08-08 produced four different formats from one persona.
The card body is the task text. Every rule the stanza states is one the detector
in ``kanban_report_format`` can measure and the notifier may log about, so a
report is never complained about for a rule it was not given. Nothing refuses a
completion over shape — the stanza is an instruction, not a gate.

The anchor is the ``body=`` keyword of the ``kb.create_task(...)`` call inside
``_handle_create``. Since v2026.9.14 upstream no longer binds ``body`` to a
local first — the handler passes ``args.get("body")`` straight into the call —
so wrapping the argument is the whole edit, and ``body=args.get("body"),`` is
the only such spelling in the file (the old ``_handle_update`` that also read
it is gone). The import is appended as a trailer the way the other kanban
patches append theirs: the name is resolved when the handler runs, and an
ImportError still surfaces when the module loads, where the build's verify can
see it.

Usage::

    python3 apply_kanban_report_format.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patchlib  # noqa: E402

RELATIVE = "tools/kanban_tools.py"

# The handler the anchor must sit inside. ``body=args.get("body"),`` is unique
# in the file today, so the count check alone would still pass if upstream
# moved that keyword to another handler and dropped it from this one -- and
# the stanza would then be added to the wrong call. The span check below makes
# that move fail the build instead.
HANDLER = "_handle_create"

OLD_BODY_ARG = 'body=args.get("body"),'

# Consumes the anchor, so a second run fails on the count check. No comment
# rides along: the keyword sits mid-line inside the call, where a trailing
# comment would swallow the argument after it. The trailer names the patch.
NEW_BODY_ARG = 'body=_with_report_format(args.get("body")),'

# Appended rather than inserted: the name is resolved when the tool handler
# runs, long after the module finishes importing, and the import itself still
# fails at module load if the companion module is missing.
TRAILER = (
    "\n\n# kube-agents patch: see tools/kanban_report_format.py\n"
    "from tools.kanban_report_format import (  # noqa: E402\n"
    "    with_report_format as _with_report_format,\n"
    ")\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix="kanban_report_format")
    handler = patch.find_def(HANDLER, label="create handler")
    inside = patch.source[handler.start : handler.end].count(OLD_BODY_ARG)
    if inside != 1:
        raise SystemExit(
            f"kanban_report_format patch: {RELATIVE}: expected the create body "
            f"anchor {OLD_BODY_ARG!r} once inside {HANDLER}(), found {inside} "
            f"({patch.source.count(OLD_BODY_ARG)} in the whole file). {patch.note}"
        )
    patch.substitute(OLD_BODY_ARG, NEW_BODY_ARG, label="create body")
    patch.append(TRAILER)
    patch.commit("1 anchor + trailer")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
