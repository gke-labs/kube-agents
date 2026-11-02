#!/usr/bin/env python3
"""Have a kanban worker's terminal result report pressure to the dispatcher.

One anchored edit in ``tools/terminal_tool_result.py``: ``finalize_foreground_result``
calls ``hermes_cli.kanban_aimd.record_worker_pressure`` just before it builds
the result dict, inside upstream's own fail-open ``_quiet`` block. In a worker
(``HERMES_KANBAN_TASK`` set) an ssh result with exit 255 and no cwd marker, or
the credential proxy's busy line in the output, becomes a ``pressure`` event on
the worker's card; the dispatcher reads those each tick and lowers its
effective cap (edit 11 of ``apply_kanban_scheduling.py``). Everywhere else the
call is a no-op.

The anchor is the line that builds ``result_dict``, after every pass that
rewrites ``output`` and ``returncode`` has run, so the recorder reads what the
model will read. Two other patches edit the same function and neither shares
this line: ``apply_kanban_verified_claims`` (earlier in the build) replaces the
function's ``return``, and ``apply_ssh_shared_master`` (later) inserts after the
``failure_hint`` assignment. ``test_apply_kanban_aimd`` asserts the three stay
disjoint, in both directions.

Usage::

    python3 apply_kanban_aimd.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patchlib  # noqa: E402

PREFIX = "kanban-aimd"
DEFAULT_HERMES_ROOT = "/opt/hermes"

RESULT_RELATIVE = "tools/terminal_tool_result.py"
RESULT_HANDLER = "finalize_foreground_result"

#: Text only a patched file holds. The anchor survives its own edit, so this
#: is what refuses a second run; the Dockerfile greps for the call line.
MARKER = "# kube-agents patch: see hermes_cli/kanban_aimd.py"
BUILD_MARKER = "record_worker_pressure(env_type, returncode, output, bool((result or {}).get(\"cwd_observed\")))"

RESULT_ANCHOR = '    result_dict = {"output": output, "exit_code": returncode, "error": None}\n'

RESULT_PATCHED = (
    f"    {MARKER}\n"
    '    with _quiet("kanban pressure record"):\n'
    "        from hermes_cli.kanban_aimd import record_worker_pressure\n"
    f"        {BUILD_MARKER}\n"
) + RESULT_ANCHOR


def _expect_inside(patch: patchlib.Patch, handler: str, anchor: str, label: str) -> None:
    """Fail unless ``anchor`` sits once inside ``handler``, not elsewhere in the file."""
    span = patch.find_def(handler, label=label)
    inside = patch.source[span.start : span.end].count(anchor.rstrip("\n"))
    if inside != 1:
        raise SystemExit(
            f"{PREFIX} patch: {patch.relative}: expected the {label} anchor once "
            f"inside {handler}(), found {inside}. {patch.note}"
        )


def apply(root: Path) -> None:
    """Apply the edit under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RESULT_RELATIVE, prefix=PREFIX)
    patch.refuse_if_patched(MARKER)
    _expect_inside(patch, RESULT_HANDLER, RESULT_ANCHOR, "result dict")
    patch.substitute(RESULT_ANCHOR, RESULT_PATCHED, label="result dict")
    patch.commit("a worker's ssh exit 255 or credential-proxy busy line records pressure on its card")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_HERMES_ROOT))
