#!/usr/bin/env python3
"""Wire tools/cron_tirith_scan.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. One anchored
replacement, in ``tools/approval.py``: the cron arm of
``check_all_command_guards`` learns to run the Tirith content scan on the
``approve`` path, where upstream ran it only on the ``deny`` path. Why that
matters is in the module docstring of
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
# The three inner lines are NOT unique on their own: `_run_approval_gate` opens
# with the same comment, the same `_is_cron_approval_context()` test and the
# same `== "deny"` compare at the same indentation. The `is_ask` line above them
# is what distinguishes `check_all_command_guards`, so the anchor starts there.
APPROVAL_CRON_ARM = (
    "    if not is_cli and not is_gateway and not is_ask:\n"
    "        # Cron sessions: respect cron_mode config\n"
    "        if _is_cron_approval_context():\n"
    '            if _get_cron_approval_mode() == "deny":\n'
)

# The mode is read once into a local and then branched on twice. Reading it
# twice would be two config loads per command and, worse, would let a config
# reload land between them — the scan arm and the deny arm could then disagree
# about which mode this command is running under.
#
# `_cron_mode != "deny"` rather than `== "approve"`: `_get_cron_approval_mode`
# normalises anything unrecognised to "deny", so those two are exhaustive today,
# but if upstream ever adds a third value the scan should cover it by default
# rather than be silently skipped on it.
#
# The scan runs BEFORE the auto-approve return and does not touch the deny arm,
# so the deny arm keeps upstream's own Tirith call verbatim and no command is
# ever scanned twice — each scan is a subprocess spawn.
#
# `_format_tirith_description` is passed in rather than reimplemented so the
# model reads the same rendering of a finding that an interactive user would.
# It is a module-level function defined above this one in the same file.
APPROVAL_CRON_ARM_PATCHED = (
    "    if not is_cli and not is_gateway and not is_ask:\n"
    "        # Cron sessions: respect cron_mode config\n"
    "        if _is_cron_approval_context():\n"
    "            _cron_mode = _get_cron_approval_mode()\n"
    "            # kube-agents patch: approvals.cron_mode answers the approval\n"
    "            # PROMPT, which an unattended run cannot answer. It does not\n"
    "            # answer the content scanner, and upstream ran Tirith only on\n"
    "            # the deny arm below — so cron_mode: approve shipped every\n"
    "            # command in every cron run unscanned. Scan here too; the mode\n"
    "            # still decides what happens to a pattern-flagged command.\n"
    "            # See tools/cron_tirith_scan.py.\n"
    '            if _cron_mode != "deny":\n'
    "                from tools.cron_tirith_scan import cron_tirith_block\n"
    "                _scan_block = cron_tirith_block(\n"
    "                    command, describe=_format_tirith_description\n"
    "                )\n"
    "                if _scan_block is not None:\n"
    "                    return _scan_block\n"
    '            if _cron_mode == "deny":\n'
)

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
        for anchor, replacement, expected in edits:
            patch.substitute(anchor, replacement, expected=expected)
        patch.commit(f"{len(edits)} anchors")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
