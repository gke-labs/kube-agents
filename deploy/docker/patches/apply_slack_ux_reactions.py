#!/usr/bin/env python3
"""Hand the Slack adapter's reaction hooks to kube-agents when KAGE_SLACK_UX is on.

Run by ``deploy/docker/Dockerfile`` against the Hermes tree at
``plugins/platforms/slack/adapter.py``, after ``slack_ux_reactions.py`` has
been copied to ``gateway/``.

Each of the two hooks gains a two-line prologue after its docstring::

    if _kage_slack_ux.enabled():
        return await _kage_slack_ux.on_processing_start(self, event)

and the module import is appended to the end of the file, where it resolves at
import time, before any hook can run. With the flag off the prologue falls
through to upstream's body, untouched, so the adapter's calls are exactly
upstream's. What the flag changes, and why, is in the module docstring of
``deploy/docker/patches/slack_ux_reactions.py``.

The anchors are the hooks' docstrings: one line each, unique in the file, and
the thing that says the hook still means what this patch was derived against.

Usage::

    python3 apply_slack_ux_reactions.py [HERMES_ROOT]  # /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

RELATIVE = "plugins/platforms/slack/adapter.py"
PREFIX = "slack_ux_reactions"

#: Asserted in the built bundle by the Dockerfile, and the second-run guard.
BUILD_MARKER = "_kage_slack_ux.enabled()"

START_ANCHOR = '        """Add an in-progress reaction when message processing begins."""\n'
START_PATCHED = START_ANCHOR + (
    "        # kube-agents patch: KAGE_SLACK_UX reacts by the ask's kind; see\n"
    "        # gateway/slack_ux_reactions.py. Off, upstream's body runs unchanged.\n"
    "        if _kage_slack_ux.enabled():\n"
    "            return await _kage_slack_ux.on_processing_start(self, event)\n"
)

COMPLETE_ANCHOR = (
    '        """Swap the in-progress reaction for a final success/failure reaction."""\n'
)
COMPLETE_PATCHED = COMPLETE_ANCHOR + (
    "        # kube-agents patch: KAGE_SLACK_UX never removes a reaction and\n"
    "        # defers the settle of delegated work to the kanban notifier; see\n"
    "        # gateway/slack_ux_reactions.py. Off, upstream's body runs unchanged.\n"
    "        if _kage_slack_ux.enabled():\n"
    "            return await _kage_slack_ux.on_processing_complete(self, event, outcome)\n"
)

IMPORT_LINE = (
    "\n\n# kube-agents patch: see gateway/slack_ux_reactions.py\n"
    "from gateway import slack_ux_reactions as _kage_slack_ux  # noqa: E402\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix=PREFIX)
    patch.refuse_if_patched(BUILD_MARKER)
    patch.substitute(START_ANCHOR, START_PATCHED, label="on_processing_start docstring")
    patch.substitute(COMPLETE_ANCHOR, COMPLETE_PATCHED, label="on_processing_complete docstring")
    patch.append(IMPORT_LINE)
    patch.commit("2 anchors, 1 import")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
