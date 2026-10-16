#!/usr/bin/env python3
"""Note a posted question in the Planning Agent's wake when KAGE_SLACK_UX is on.

Run by ``deploy/docker/Dockerfile`` against the Hermes tree at
``gateway/kanban_watchers_notifier.py``, after ``slack_ux_moments.py`` has been
copied to ``gateway/``.

``_KanbanNotification.build_wake_text`` gains, after it sets ``self.synth``::

    self.synth = _kage_moments_wake_text(self.sub, self.d["events"], self.wake_kinds, self.synth)

and the module import is appended to the end of the file. ``wake_text``
returns the text unchanged unless ``slack_ux_moments.needs_you`` posted this
wake's ``blocked`` question in the thread, which it does only with the flag
on, so with the flag off the wake is exactly upstream's. When it did, the
wake says the question is already posted and asks for nothing but carrying
the answer to the card. Why, is in the module docstring of
``deploy/docker/patches/slack_ux_moments.py``.

The anchor is the line that finishes the wake text: one line, unique in the
file, and the last thing ``build_wake_text`` does.

Usage::

    python3 apply_slack_ux_moments.py [HERMES_ROOT]  # /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

RELATIVE = "gateway/kanban_watchers_notifier.py"
PREFIX = "slack_ux_moments"

#: Asserted in the built bundle by the Dockerfile, and the second-run guard.
BUILD_MARKER = "_kage_moments_wake_text("

WAKE_ANCHOR = '        self.synth = synth + "\\n\\n" + t("gateway.kanban.wake.guidance")\n'
WAKE_PATCHED = WAKE_ANCHOR + (
    "        # kube-agents patch: KAGE_SLACK_UX notes a question already posted in\n"
    "        # the thread; see gateway/slack_ux_moments.py. Off, the text is unchanged.\n"
    '        self.synth = _kage_moments_wake_text(self.sub, self.d["events"], self.wake_kinds, self.synth)\n'
)

IMPORT_LINE = (
    "\n\n# kube-agents patch: see gateway/slack_ux_moments.py\n"
    "from gateway.slack_ux_moments import wake_text as _kage_moments_wake_text  # noqa: E402\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix=PREFIX)
    patch.refuse_if_patched(BUILD_MARKER)
    patch.substitute(WAKE_ANCHOR, WAKE_PATCHED, label="wake text guidance line")
    patch.append(IMPORT_LINE)
    patch.commit("1 anchor, 1 import")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
