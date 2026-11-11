#!/usr/bin/env python3
"""Draw the autonomy dial on the Slack Home tab when KUBEAGENTS_DIAL is on, and a "dial is off" line otherwise.

Run by ``deploy/docker/Dockerfile`` against the Hermes tree at
``plugins/platforms/slack/adapter.py``, after ``slack_dial_home.py`` has been
copied to ``gateway/``.

Upstream's ``_handle_app_home_opened`` returns at once for any tab but
Messages, so the Home tab is never drawn. It gains, before that return::

    if event.get("tab") == "home":
        await _kage_dial_home.opened(self, event, body)
        return

and ``_register_bolt_handlers`` gains, right before it wires the plugin
platform handlers::

    _kage_dial_home.register(self)

with the module import appended to the end of the file. The listener is for
the dial's own action id only, and is wired whatever the switch says so that a
click on a dial Slack still shows after the switch went off is acknowledged
rather than erroring. With the switch off, ``opened`` publishes the one line
"The autonomy dial is off on this install.", so a dial drawn before the switch
went off is cleared the next time the tab opens. Upstream returns on the Home
tab without doing anything, so every event upstream acts on is handled as
upstream handles it. What the tab
shows and who may change it is in the module docstring of
``deploy/docker/patches/slack_dial_home.py``.

Usage::

    python3 apply_slack_dial_home.py [HERMES_ROOT]  # /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

RELATIVE = "plugins/platforms/slack/adapter.py"
PREFIX = "slack_dial_home"

#: Asserted in the built bundle by the Dockerfile, and the second-run guard.
BUILD_MARKER = "_kage_dial_home.register(self)"

HOME_ANCHOR = '''\
        """Handle Slack Agent DM-open lifecycle events without producing replies."""
        if event.get("tab") != "messages":
            return
'''
HOME_PATCHED = '''\
        """Handle Slack Agent DM-open lifecycle events without producing replies."""
        # kube-agents patch: KUBEAGENTS_DIAL=on draws the autonomy dial on the
        # Home tab; off, the tab says the dial is off, so a stale dial does not
        # linger. See gateway/slack_dial_home.py.
        if event.get("tab") == "home":
            await _kage_dial_home.opened(self, event, body)
            return
        if event.get("tab") != "messages":
            return
'''

REGISTER_ANCHOR = "        self._wire_plugin_handlers(self._app)\n"
REGISTER_PATCHED = (
    "        # kube-agents patch: the Home tab dial's one action listener; see\n"
    "        # gateway/slack_dial_home.py. With KUBEAGENTS_DIAL off it only\n"
    "        # acknowledges a click on a dial drawn before the switch went off.\n"
    "        _kage_dial_home.register(self)\n"
) + REGISTER_ANCHOR

IMPORT_LINE = (
    "\n\n# kube-agents patch: see gateway/slack_dial_home.py\n"
    "from gateway import slack_dial_home as _kage_dial_home  # noqa: E402\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix=PREFIX)
    patch.refuse_if_patched(BUILD_MARKER)
    patch.substitute(HOME_ANCHOR, HOME_PATCHED, label="Home tab early return")
    patch.substitute(REGISTER_ANCHOR, REGISTER_PATCHED, label="plugin platform handler wiring")
    patch.append(IMPORT_LINE)
    patch.commit("2 anchors, 1 import")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
