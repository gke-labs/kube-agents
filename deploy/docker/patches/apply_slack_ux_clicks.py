#!/usr/bin/env python3
"""Register kube-agents' Slack button handlers when KAGE_SLACK_UX is on.

Run by ``deploy/docker/Dockerfile`` against the Hermes tree at
``plugins/platforms/slack/adapter.py``, after ``slack_ux_clicks.py`` has been
copied to ``gateway/``.

``_register_bolt_handlers`` gains, right after it wires the plugin action
handlers::

    if _kage_slack_clicks.enabled():
        _kage_slack_clicks.register(self)

and the module import is appended to the end of the file, where it resolves at
import time, before the adapter connects. With the flag off nothing is
registered, so the adapter's listeners are exactly upstream's. After the
plugin handlers, because Bolt dispatches to the first matching listener: a
plugin that claims one of these action ids keeps it. What the handlers do,
and why, is in the module docstring of
``deploy/docker/patches/slack_ux_clicks.py``.

The anchor is the call that wires the plugin action handlers: one line,
unique in the file, and inside the method that must run before Socket Mode
starts.

Usage::

    python3 apply_slack_ux_clicks.py [HERMES_ROOT]  # /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

RELATIVE = "plugins/platforms/slack/adapter.py"
PREFIX = "slack_ux_clicks"

#: Asserted in the built bundle by the Dockerfile, and the second-run guard.
BUILD_MARKER = "_kage_slack_clicks.enabled()"

REGISTER_ANCHOR = "        self._register_plugin_action_handlers()\n"
REGISTER_PATCHED = REGISTER_ANCHOR + (
    "        # kube-agents patch: KAGE_SLACK_UX answers clicks on our own buttons;\n"
    "        # see gateway/slack_ux_clicks.py. Off, nothing is registered.\n"
    "        if _kage_slack_clicks.enabled():\n"
    "            _kage_slack_clicks.register(self)\n"
)

IMPORT_LINE = (
    "\n\n# kube-agents patch: see gateway/slack_ux_clicks.py\n"
    "from gateway import slack_ux_clicks as _kage_slack_clicks  # noqa: E402\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix=PREFIX)
    patch.refuse_if_patched(BUILD_MARKER)
    patch.substitute(REGISTER_ANCHOR, REGISTER_PATCHED, label="plugin action handler wiring")
    patch.append(IMPORT_LINE)
    patch.commit("1 anchor, 1 import")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
