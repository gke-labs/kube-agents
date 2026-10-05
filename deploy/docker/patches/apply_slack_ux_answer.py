#!/usr/bin/env python3
"""Let a finished card's answer post folded when KAGE_SLACK_UX is on.

Run by ``deploy/docker/Dockerfile`` against the Hermes tree at
``gateway/kanban_watchers_notifier.py``, after ``apply_slack_ux_incident.py``
has wrapped the adapter of the notifier's ``_progress_deliver`` call and after
``slack_ux_answer.py`` has been copied to ``gateway/``.

The incident factory's adapter argument becomes::

    _kage_slack_answer.adapter_for(adapter, self.platform_str, ev, self.task, sub)

which is ``adapter`` itself unless the flag is on and the event is a
``completed`` card on Slack. It sits inside the incident wrapper, so an alert
edit is tried first and a failed one falls back to it. What the returned
adapter does, and why, is in the module docstring of
``deploy/docker/patches/slack_ux_answer.py``.

Usage::

    python3 apply_slack_ux_answer.py [HERMES_ROOT]  # /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

RELATIVE = "gateway/kanban_watchers_notifier.py"
PREFIX = "slack_ux_answer"

#: Asserted in the built bundle by the Dockerfile, and the second-run guard.
BUILD_MARKER = "_kage_slack_answer.adapter_for("

#: ``apply_slack_ux_incident``'s adapter line in ``_send_event``.
INCIDENT_ANCHOR = (
    "            _kage_slack_incident.adapter_for(adapter, self.platform_str, ev, self.task, sub),\n"
)
#: One line with no comment above it, directly after ``self.runner,``, as
#: ``verify_kanban_progress_lines``' ``RUNNER_ARGS`` reads it; the import says why.
INCIDENT_PATCHED = (
    "            _kage_slack_incident.adapter_for(_kage_slack_answer.adapter_for("
    "adapter, self.platform_str, ev, self.task, sub), self.platform_str, ev, self.task, sub),\n"
)

IMPORT_LINE = (
    "\n\n# kube-agents patch: KAGE_SLACK_UX folds a finished card's answer, through\n"
    "# the adapter inside _kage_slack_incident's; see gateway/slack_ux_answer.py\n"
    "from gateway import slack_ux_answer as _kage_slack_answer  # noqa: E402\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix=PREFIX)
    patch.refuse_if_patched(BUILD_MARKER)
    patch.substitute(INCIDENT_ANCHOR, INCIDENT_PATCHED, label="incident adapter argument")
    patch.append(IMPORT_LINE)
    patch.commit("1 anchor, 1 import")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
