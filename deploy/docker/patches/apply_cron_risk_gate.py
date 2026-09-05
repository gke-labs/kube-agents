#!/usr/bin/env python3
"""Wire tools/cron_risk_gate.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``.
Must run AFTER ``apply_cron_tirith_scan.py``: anchors into ``tools/approval.py``
where ``apply_cron_tirith_scan.py`` introduced ``_cron_mode = _get_cron_approval_mode()``.

Two edits to ``tools/approval.py``:
1. In ``check_all_command_guards``: escalates mode for high-risk cron jobs and runs
   content checks (terminal escapes and lookalike TLDs).
2. In ``check_execute_code_guard``: unconditionally refuses execute_code on cron runs.

Usage::

    python3 apply_cron_risk_gate.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

# --- tools/approval.py: check_all_command_guards ----------------------------
COMMAND_CRON_ARM = (
    "        if _is_cron_approval_context():\n"
    "            _cron_mode = _get_cron_approval_mode()\n"
)

COMMAND_CRON_ARM_PATCHED = (
    "        if _is_cron_approval_context():\n"
    "            _cron_mode = _get_cron_approval_mode()\n"
    "            # kube-agents patch: see tools/cron_risk_gate.py\n"
    "            from tools.cron_risk_gate import (\n"
    "                cron_content_block,\n"
    "                cron_effective_mode,\n"
    "            )\n"
    "            from tools.cron_run_scope import current_cron_risk\n"
    "            _cron_mode = cron_effective_mode(_cron_mode, current_cron_risk())\n"
    "            _risk_block = cron_content_block(command)\n"
    "            if _risk_block is not None:\n"
    "                return _risk_block\n"
)

# --- tools/approval.py: check_execute_code_guard ----------------------------
EXECUTE_CODE_CRON_ARM = (
    "    # Cron: no user is present to approve arbitrary code.\n"
    "    if _is_cron_approval_context():\n"
    '        if _get_cron_approval_mode() == "deny":\n'
)

EXECUTE_CODE_CRON_ARM_PATCHED = (
    "    # Cron: no user is present to approve arbitrary code.\n"
    "    if _is_cron_approval_context():\n"
    "        # kube-agents patch: block execute_code unconditionally on cron runs (THREAT-002).\n"
    "        from tools.cron_risk_gate import cron_execute_code_block\n"
    "        _exec_block = cron_execute_code_block()\n"
    "        if _exec_block is not None:\n"
    "            return _exec_block\n"
    '        if _get_cron_approval_mode() == "deny":\n'
)

PATCHES = (
    (
        "tools/approval.py",
        (
            (COMMAND_CRON_ARM, COMMAND_CRON_ARM_PATCHED, 1),
            (EXECUTE_CODE_CRON_ARM, EXECUTE_CODE_CRON_ARM_PATCHED, 1),
        ),
    ),
)


def apply(root: Path) -> None:
    """Apply every patch under ``root``, or raise SystemExit with the reason."""
    for relative, edits in PATCHES:
        patch = patchlib.Patch(root, relative, prefix="cron_risk_gate")
        patch.refuse_if_patched("from tools.cron_risk_gate import")
        for anchor, replacement, expected in edits:
            patch.substitute(anchor, replacement, expected=expected)
        patch.commit(f"{len(edits)} anchors")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
