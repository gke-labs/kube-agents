#!/usr/bin/env python3
"""Hand the Slack adapter's session status and channel-ask titles to kube-agents when KAGE_SLACK_UX is on.

Run by ``deploy/docker/Dockerfile`` against the Hermes tree at
``plugins/platforms/slack/adapter.py``, after ``slack_ux_status.py`` has been
copied to ``gateway/``.

``_set_thread_status`` gains a prologue after its docstring that hands the call
to ``slack_ux_status.set_thread_status`` when the flag is on and the SDK has
Agent Sessions, passing the module's own method resolvers. The message-event
builder gains a guarded line after upstream's DM title that keeps a channel
ask's words for its session title. The module import is appended to the end of
the file. With the flag off both fall through to upstream's code, untouched.
What the flag changes, and why, is in the module docstring of
``deploy/docker/patches/slack_ux_status.py``.

Usage::

    python3 apply_slack_ux_status.py [HERMES_ROOT]  # /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

RELATIVE = "plugins/platforms/slack/adapter.py"
PREFIX = "slack_ux_status"

#: Asserted in the built bundle by the Dockerfile, and the second-run guard.
BUILD_MARKER = "_kage_slack_status.enabled()"

STATUS_ANCHOR = (
    '        """``assistant.threads.setStatus`` (empty ``status`` clears); failures are debug-logged."""\n'
)
STATUS_PATCHED = STATUS_ANCHOR + (
    "        # kube-agents patch: KAGE_SLACK_UX sends agents.sessions.setStatus\n"
    "        # an enum value it accepts, and only on a change; see\n"
    "        # gateway/slack_ux_status.py. Off, upstream's body runs unchanged.\n"
    "        if _kage_slack_status.enabled() and _sdk_supports_agent_sessions():\n"
    "            return await _kage_slack_status.set_thread_status(\n"
    "                self, chat_id, team_id, thread_ts, status, fail_label,\n"
    "                _session_status_method, _session_title_method)\n"
)

TITLE_ANCHOR = (
    "        if is_dm and thread_ts and msg_type != MessageType.COMMAND:\n"
    "            await self._set_assistant_thread_title(\n"
    "                channel_id, thread_ts, original_text or text, team_id=team_id)\n"
)
TITLE_PATCHED = TITLE_ANCHOR + (
    "        # kube-agents patch: KAGE_SLACK_UX titles a channel ask's session\n"
    "        # too, once the session opens; see gateway/slack_ux_status.py.\n"
    "        if not is_dm and msg_type != MessageType.COMMAND:\n"
    "            _kage_slack_status.note_ask(channel_id, thread_ts or ts, original_text or text)\n"
)

IMPORT_LINE = (
    "\n\n# kube-agents patch: see gateway/slack_ux_status.py\n"
    "from gateway import slack_ux_status as _kage_slack_status  # noqa: E402\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix=PREFIX)
    patch.refuse_if_patched(BUILD_MARKER)
    patch.substitute(STATUS_ANCHOR, STATUS_PATCHED, label="_set_thread_status docstring")
    patch.substitute(TITLE_ANCHOR, TITLE_PATCHED, label="DM thread title in _build_message_event")
    patch.append(IMPORT_LINE)
    patch.commit("2 anchors, 1 import")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
