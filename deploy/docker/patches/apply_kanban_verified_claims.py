#!/usr/bin/env python3
"""Wire tools/kanban_verified_claims.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. Three edits in two
files:

``tools/terminal_tool_result.py``
    ``finalize_foreground_result`` records each call just before it returns the
    tool's JSON, after the output was cut, stripped and redacted: the record
    holds what the model saw, which is what it copies an excerpt from. The
    record is inside the file's own ``_quiet`` block, so a failure there never
    fails a terminal call.

``tools/kanban_tools.py``
    ``_handle_complete`` runs the completion rules (``verified_claims`` is the
    first) right before upstream's
    ``_require_dict_metadata``, after the result gate, so a refusal leaves the
    card in flight with nothing written. The schema fixup is inserted before the
    ``_TOOLS`` table, as ``apply_kanban_result_required.py`` does, so the
    registry never hands a model the schema without the new parameter.

The record is a patch and not a profile plugin because plugins are enabled per
profile, and the cluster-agent profiles enable only ``hermes_otel``: a plugin
would leave their cards with an empty record and every claim refused.

Why the change is needed is documented in the module docstring of
``deploy/docker/patches/kanban_verified_claims.py``. Usage::

    python3 apply_kanban_verified_claims.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patchlib  # noqa: E402

TERMINAL_RELATIVE = "tools/terminal_tool_result.py"
KANBAN_RELATIVE = "tools/kanban_tools.py"

TERMINAL_HANDLER = "finalize_foreground_result"
COMPLETE_HANDLER = "_handle_complete"
TOOL_TABLE = "_TOOLS"

#: Text only a patched file holds. Every anchor survives its own edit, so this
#: is what refuses a second run.
MARKER = "# kube-agents patch: see tools/kanban_verified_claims.py"

OLD_TERMINAL_RETURN = "    return json.dumps(result_dict, ensure_ascii=False)\n"

NEW_TERMINAL_RETURN = (
    f"    {MARKER}\n"
    '    with _quiet("verified-claims record"):\n'
    "        from tools.kanban_verified_claims import record_terminal_call\n"
    "        record_terminal_call(command, returncode, output)\n"
    "    return json.dumps(result_dict, ensure_ascii=False)\n"
)

OLD_COMPLETE_METADATA = (
    "    _require_dict_metadata(metadata)\n"
    "    metadata = _stamp_worker_session_metadata(tid, metadata)\n"
)

NEW_COMPLETE_METADATA = (
    f"    {MARKER}\n"
    "    _rules_err, result, metadata = _apply_completion_rules(\n"
    "        tid, args, result, metadata, redact=_redact,\n"
    '        connect=lambda: _kanban_verified_claims_connect(board=args.get("board")))\n'
    "    if _rules_err:\n"
    "        return tool_error(_rules_err)\n"
    "    _require_dict_metadata(metadata)\n"
    "    metadata = _stamp_worker_session_metadata(tid, metadata)\n"
)

SCHEMA_PREAMBLE = (
    f"{MARKER}\n"
    "from hermes_cli.kanban_db_connect import (\n"
    "    connect as _kanban_verified_claims_connect,\n"
    ")\n"
    "from tools.kanban_verified_claims import (\n"
    "    apply_schema as _apply_verified_claims_schema,\n"
    "    apply_completion_rules as _apply_completion_rules,\n"
    ")\n"
    "\n"
    "_apply_verified_claims_schema(KANBAN_COMPLETE_SCHEMA)\n"
    "\n"
)


def _expect_inside(patch: patchlib.Patch, handler: str, anchor: str, label: str) -> None:
    """Fail unless ``anchor`` sits once inside ``handler``, not elsewhere in the file."""
    span = patch.find_def(handler, label=label)
    # The def's span ends at its last character, before the newline after it.
    inside = patch.source[span.start : span.end].count(anchor.rstrip("\n"))
    if inside != 1:
        raise SystemExit(
            f"kanban_verified_claims patch: {patch.relative}: expected the {label} "
            f"anchor once inside {handler}(), found {inside}. {patch.note}"
        )


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    terminal = patchlib.Patch(root, TERMINAL_RELATIVE, prefix="kanban_verified_claims")
    terminal.refuse_if_patched(MARKER)
    _expect_inside(terminal, TERMINAL_HANDLER, OLD_TERMINAL_RETURN, "terminal return")
    terminal.substitute(OLD_TERMINAL_RETURN, NEW_TERMINAL_RETURN, label="terminal return")

    kanban = patchlib.Patch(root, KANBAN_RELATIVE, prefix="kanban_verified_claims")
    kanban.refuse_if_patched(MARKER)
    _expect_inside(kanban, COMPLETE_HANDLER, OLD_COMPLETE_METADATA, "complete metadata")
    kanban.substitute(OLD_COMPLETE_METADATA, NEW_COMPLETE_METADATA, label="complete metadata")
    table = kanban.find_assign(TOOL_TABLE, label="kanban_complete registration")
    kanban.insert(table.line_start, SCHEMA_PREAMBLE)

    terminal.commit("1 anchor")
    kanban.commit("1 anchor + 1 registration")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
