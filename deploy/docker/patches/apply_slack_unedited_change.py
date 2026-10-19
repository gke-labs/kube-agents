#!/usr/bin/env python3
"""Drop a Slack ``message_changed`` that carries no edit.

One anchored insertion in ``plugins/platforms/slack/adapter.py``'s
``_normalize_changed_message``. Upstream turns every ``message_changed`` into a
plain message event unless the changed message's ts is in
``_processed_message_ts``, an in-memory map of messages already routed to the
agent. It exists so that a user who edits an @mention into a message gets one
turn for it, and nothing else stops a change from being read as a new message.

A restart empties that map. Slack sends ``message_changed`` for a thread's
parent whenever a reply in the thread changes (its reply metadata moves), and a
bot's own ``chat.update`` of a reply is such a change: a click rewriting the
clicked message, a needs-you message settling, an incident alert edited into
its report. After a restart the parent, which @-mentions the bot, is no longer
in the map, so the change is processed as that mention again and the thread's
opening ask runs a second time.

Slack marks a real edit with an ``edited`` object on the changed message; a
metadata change, a link unfurl and a bot-side rewrite of another message carry
none. The insertion returns None for a change without one, after upstream's
processed-ts check, so the edit-to-mention case keeps its single turn and
everything upstream already dropped stays dropped. A link unfurl was already
dropped by the processed-ts guard for any message the agent saw; this drops it
for one it did not see either, which was never a user's turn.

The Go gateway (next mode) admits no ``message_changed`` at all
(``slackTurnSubtypes`` in ``a2a/gateway/slack.go``), so this is today mode's half.
Unconditional: the replay needs only a restart and an edit in a thread, flag or
not.

Usage::

    python3 apply_slack_unedited_change.py [HERMES_ROOT]  # /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

RELATIVE = "plugins/platforms/slack/adapter.py"
PREFIX = "slack_unedited_change"

#: Asserted in the built bundle by the Dockerfile, and the second-run guard.
MARKER = "kube-agents patch: slack_unedited_change"

GUARD_ANCHOR = (
    "        if original_message_ts and original_message_ts in self._processed_message_ts:\n"
    "            return None\n"
)
GUARD_PATCHED = GUARD_ANCHOR + (
    f"        # {MARKER}: no `edited` means nobody edited it (reply metadata, an\n"
    "        # unfurl, a bot rewrite); after a restart it would replay a thread's opening ask.\n"
    '        if not isinstance(updated_message.get("edited"), dict):\n'
    "            return None\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix=PREFIX)
    patch.refuse_if_patched(MARKER)
    patch.substitute(GUARD_ANCHOR, GUARD_PATCHED, label="processed-ts guard in _normalize_changed_message")
    patch.commit("a message_changed with no edit is dropped")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
