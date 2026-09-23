#!/usr/bin/env python3
"""Wire tools/cron_risk_gate.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``.
Must run AFTER ``apply_cron_tirith_scan.py``: anchors into ``tools/approval.py``
where ``apply_cron_tirith_scan.py`` introduced
``_cron_mode = approval_context._get_cron_approval_mode()``.

Two edits to ``tools/approval.py``, derived against v2026.9.14:
1. In ``check_all_command_guards``: enforces read-only command policy for high-risk
   cron jobs and runs content checks (terminal escapes and lookalike TLDs), ahead
   of the Tirith scan and of upstream's ``_unattended_deny`` loop, in every mode.
2. In ``check_execute_code_guard``: unconditionally refuses execute_code on cron
   runs, ahead of upstream's ``_unattended_contexts()`` loop that would approve it
   under ``cron_mode: approve``.

Usage::

    python3 apply_cron_risk_gate.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

# --- tools/approval.py: check_all_command_guards ----------------------------
# Both lines are text apply_cron_tirith_scan.py inserted; upstream has no
# ``_cron_mode`` local of its own at v2026.9.14.
COMMAND_CRON_ARM = (
    "        if _is_cron_approval_context():\n"
    "            _cron_mode = approval_context._get_cron_approval_mode()\n"
)

COMMAND_CRON_ARM_PATCHED = (
    "        if _is_cron_approval_context():\n"
    "            _cron_mode = approval_context._get_cron_approval_mode()\n"
    "            # kube-agents patch: see tools/cron_risk_gate.py\n"
    "            from tools.cron_risk_gate import (\n"
    "                cron_command_policy_block,\n"
    "                cron_content_block,\n"
    "            )\n"
    "            from tools.cron_run_scope import current_cron_risk\n"
    "            _cron_risk = current_cron_risk()\n"
    "            _risk_block = cron_content_block(command)\n"
    "            if _risk_block is not None:\n"
    "                return _risk_block\n"
    "            _policy_block = cron_command_policy_block(command, _cron_risk)\n"
    "            if _policy_block is not None:\n"
    "                return _policy_block\n"
)

# --- tools/approval.py: check_execute_code_guard ----------------------------
# Upstream's per-context loop (55248a133f): the first active unattended context
# either denies or approves the whole script from its mode, so under
# ``cron_mode: approve`` execute_code runs on cron. ``_run_approval_gate`` has a
# loop over ``_unattended_contexts()`` too, but four spaces deeper and assigning
# a message rather than returning ``_denied(``, so this spelling occurs once.
EXECUTE_CODE_CRON_ARM = (
    "    for ctx in _unattended_contexts():\n"
    '        if ctx.mode() == "deny":\n'
    "            return _denied(\n"
)

EXECUTE_CODE_CRON_ARM_PATCHED = (
    "    # kube-agents patch: block execute_code unconditionally on cron runs (THREAT-002).\n"
    "    if _is_cron_approval_context():\n"
    "        from tools.cron_risk_gate import cron_execute_code_block\n"
    "        _exec_block = cron_execute_code_block()\n"
    "        if _exec_block is not None:\n"
    "            return _exec_block\n"
    "    for ctx in _unattended_contexts():\n"
    '        if ctx.mode() == "deny":\n'
    "            return _denied(\n"
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
