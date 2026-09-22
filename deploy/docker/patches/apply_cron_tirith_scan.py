#!/usr/bin/env python3
"""Wire tools/cron_tirith_scan.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. One anchored
insert, in ``tools/approval.py``: the unattended branch of
``check_all_command_guards`` learns to run the Tirith content scan on cron
sessions under the ``approve`` mode, where upstream runs it only under
``deny``. Why that matters is in the module docstring of
``deploy/docker/patches/cron_tirith_scan.py``.

The guarantee is the same as every other patch the Dockerfile applies: the
anchor must be found the exact number of times expected, the edited file must
still parse, and anything else fails the build loudly rather than shipping an
image whose security posture is not the one the config describes. That last
clause is the point here — a silently-missing edit leaves cron running
unscanned, which looks identical to it working.

Usage::

    python3 apply_cron_tirith_scan.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

# --- tools/approval.py: scan the command even when the prompt is waived -----
#
# Derived against v2026.9.14. Upstream's "unify the three human-approval gates"
# refactor (55248a133f) folded the per-context ``if _is_cron_approval_context():
# if _get_cron_approval_mode() == "deny": ...`` arm this patch used to anchor on
# into a table: ``_unattended_contexts()`` yields the active contexts (single
# query, then cron, else an unattended platform) and ``_unattended_deny`` runs
# pattern detection and then Tirith for one of them — and returns ``None``
# before either the moment ``ctx.mode() != "deny"``. So the behaviour the patch
# corrects is unchanged: under ``cron_mode: approve`` nothing scans the command.
#
# The anchor is the loop that calls ``_unattended_deny``. ``_run_approval_gate``
# iterates ``_unattended_contexts()`` too, but at a deeper indent and without
# the ``result = _unattended_deny(command, ctx)`` line, which is what makes the
# three lines below occur exactly once. The comment upstream keeps above the
# ``if`` is deliberately not part of the anchor: comments get reflowed
# (8878592515 did exactly that to a neighbour), code lines less so.
APPROVAL_CRON_ARM = (
    "    if not is_cli and not is_gateway and not is_ask:\n"
    "        for ctx in _unattended_contexts():\n"
    "            result = _unattended_deny(command, ctx)\n"
)

# The insert sits ahead of upstream's loop rather than inside ``_unattended_deny``
# so the order of the v2026.8.19 arm is kept: the cron-specific gates first,
# then upstream's own deny handling untouched. Under ``deny`` this block does
# nothing and ``_unattended_deny`` scans once, as upstream always did; under
# ``approve`` this block scans once and ``_unattended_deny`` returns ``None``
# without scanning. No command is ever scanned twice — each scan is a
# subprocess spawn.
#
# The mode is read once into a local and then branched on, and
# ``apply_cron_risk_gate.py`` anchors on that line to branch on it again.
# Reading it twice would be two config loads per command and, worse, would let
# a config reload land between them. It is read through the ``approval_context``
# module rather than a bare name because v2026.9.14 no longer imports
# ``_get_cron_approval_mode`` into ``tools.approval`` — upstream's own
# ``_Unattended.mode()`` resolves the getter on ``approval_context`` at call
# time for the same reason, and the verifier pins it there.
#
# ``_cron_mode != "deny"`` rather than ``== "approve"``: ``_binary_approval_mode``
# normalises anything unrecognised to "deny", so those two are exhaustive today,
# but if upstream ever adds a third value the scan should cover it by default
# rather than be silently skipped on it.
#
# ``_format_tirith_description`` is passed in rather than reimplemented so the
# model reads the same rendering of a finding that an interactive user would.
# It is a module-level function defined above this one in the same file.
APPROVAL_CRON_ARM_PATCHED = (
    "    if not is_cli and not is_gateway and not is_ask:\n"
    "        # kube-agents patch: approvals.cron_mode answers the approval\n"
    "        # PROMPT, which an unattended run cannot answer. It does not\n"
    "        # answer the content scanner, and _unattended_deny below returns\n"
    "        # before Tirith unless the mode is deny — so cron_mode: approve\n"
    "        # shipped every command in every cron run unscanned. Scan here\n"
    "        # too; the mode still decides what happens to a pattern-flagged\n"
    "        # command. See tools/cron_tirith_scan.py.\n"
    "        if _is_cron_approval_context():\n"
    "            _cron_mode = approval_context._get_cron_approval_mode()\n"
    '            if _cron_mode != "deny":\n'
    "                from tools.cron_tirith_scan import cron_tirith_block\n"
    "                _scan_block = cron_tirith_block(\n"
    "                    command, describe=_format_tirith_description\n"
    "                )\n"
    "                if _scan_block is not None:\n"
    "                    return _scan_block\n"
    "        for ctx in _unattended_contexts():\n"
    "            result = _unattended_deny(command, ctx)\n"
)

# The replacement consumes its anchor, so a second run would already fail on
# the count; the marker turns that into a named "already patched" refusal.
# This import line exists only after a run.
PATCHED_MARKER = "from tools.cron_tirith_scan import cron_tirith_block"

# (relative path, [(anchor, replacement, expected occurrences)])
PATCHES = (
    (
        "tools/approval.py",
        ((APPROVAL_CRON_ARM, APPROVAL_CRON_ARM_PATCHED, 1),),
    ),
)


def apply(root: Path) -> None:
    """Apply every patch under ``root``, or raise SystemExit with the reason."""
    for relative, edits in PATCHES:
        patch = patchlib.Patch(root, relative, prefix="cron_tirith_scan")
        patch.refuse_if_patched(PATCHED_MARKER)
        for anchor, replacement, expected in edits:
            patch.substitute(anchor, replacement, expected=expected)
        patch.commit(f"{len(edits)} anchors")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
