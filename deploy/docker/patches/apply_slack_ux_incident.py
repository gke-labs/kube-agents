#!/usr/bin/env python3
"""Let an incident's triage report edit its alert when KAGE_SLACK_UX is on.

Run by ``deploy/docker/Dockerfile`` against the Hermes tree at
``gateway/kanban_watchers_notifier.py``, after ``apply_kanban_progress_lines.py``
has routed the notifier's send through ``_progress_deliver`` and after
``slack_ux_incident.py`` has been copied to ``gateway/``.

The adapter argument of that call becomes::

    _kage_slack_incident.adapter_for(adapter, self.platform_str, ev, self.task, sub)

which is ``adapter`` itself unless the flag is on and the event is a triage
report for its own alert's thread on Slack. The rolling-message settle, the
settle reaction, the ``SendResult`` check, artifact upload and the incident
row that follow all run as before. What the returned adapter does, and why,
is in the module docstring of ``deploy/docker/patches/slack_ux_incident.py``.

Usage::

    python3 apply_slack_ux_incident.py [HERMES_ROOT]  # /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

RELATIVE = "gateway/kanban_watchers_notifier.py"
PREFIX = "slack_ux_incident"

#: Asserted in the built bundle by the Dockerfile, and the second-run guard.
BUILD_MARKER = "_kage_slack_incident.adapter_for("

#: The argument line of ``apply_kanban_progress_lines``'s ``_progress_deliver``
#: call in ``_send_event``.
DELIVER_ANCHOR = "            self.runner, adapter, sub, ev.kind, ev, msg, metadata,\n"
DELIVER_PATCHED = (
    "            # kube-agents patch: KAGE_SLACK_UX edits an alert into its triage;\n"
    "            # see gateway/slack_ux_incident.py. Off, this is `adapter`.\n"
    "            self.runner,\n"
    "            _kage_slack_incident.adapter_for(adapter, self.platform_str, ev, self.task, sub),\n"
    "            sub, ev.kind, ev, msg, metadata,\n"
)

IMPORT_LINE = (
    "\n\n# kube-agents patch: see gateway/slack_ux_incident.py\n"
    "from gateway import slack_ux_incident as _kage_slack_incident  # noqa: E402\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix=PREFIX)
    patch.refuse_if_patched(BUILD_MARKER)
    patch.substitute(DELIVER_ANCHOR, DELIVER_PATCHED, label="notifier deliver adapter")
    patch.append(IMPORT_LINE)
    patch.commit("1 anchor, 1 import")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
