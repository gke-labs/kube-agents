#!/usr/bin/env python3
"""Wire gateway/slack_ux_failure.py into the wake, the final send and Slack's blocks.

Run by ``deploy/docker/Dockerfile`` against the Hermes tree, after
``slack_ux_failure.py`` has been copied to ``gateway/`` and after
``apply_slack_ux_moments.py``, whose wake-text line the first anchor follows.
Four files:

``gateway/kanban_watchers_notifier.py``: ``build_wake_text`` ends with
``note_wake(self.sub, self.wake_kinds, self.synth)``, so a Slack failure wake
marks its thread before the wake is delivered.

``gateway/platforms/base.py``: ``_process_message_background`` calls
``start(event)`` after its processing-start hook, so a wake turn claims its
thread's mark; ``send_final_ledgered`` brackets its ``_send_with_retry`` with
``begin(event)`` and ``end(token)``, so the send of that turn's reply runs marked.

``plugins/platforms/slack/adapter.py``: upstream's ``_maybe_blocks`` is renamed
``_kage_upstream_maybe_blocks`` and a ``_maybe_blocks`` that hands it to
``maybe_blocks`` takes its place, so the normal post and the stream finalize
both draw a marked reply.

``gateway/run_turn.py``: ``_run_agent_queued_followup`` calls
``drop(turn_ctx.source, pending_event)`` just before it runs the follow-up: the
same claim as ``start``, for a turn that does not pass through
``_process_message_background``.

With the flag off nothing is marked and every call returns upstream's result.
What the flag changes, and why, is in the module docstring of
``deploy/docker/patches/slack_ux_failure.py``. Anchors are derived against
v2026.9.14.

Usage::

    python3 apply_slack_ux_failure.py [HERMES_ROOT]  # /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

PREFIX = "slack_ux_failure"

#: Asserted in the built bundle by the Dockerfile, and the second-run guard.
BUILD_MARKER = "_kage_slack_failure."

GATEWAY_IMPORT = (
    "\n\n# kube-agents patch: see gateway/slack_ux_failure.py\n"
    "from gateway import slack_ux_failure as _kage_slack_failure  # noqa: E402\n"
)

NOTIFIER = "gateway/kanban_watchers_notifier.py"

WAKE_ANCHOR = '        self.synth = _kage_moments_wake_text(self.sub, self.d["events"], self.wake_kinds, self.synth)\n'
WAKE_PATCHED = WAKE_ANCHOR + (
    "        # kube-agents patch: KAGE_SLACK_UX marks a failure wake's thread so its\n"
    "        # reply is drawn as one; see gateway/slack_ux_failure.py.\n"
    "        _kage_slack_failure.note_wake(self.sub, self.wake_kinds, self.synth)\n"
)

BASE = "gateway/platforms/base.py"

FINAL_ANCHOR = (
    "        result = await delivery_adapter._send_with_retry(\n"
    "            chat_id=event.source.chat_id, content=text_content, reply_to=reply_to, metadata=metadata)\n"
)
FINAL_PATCHED = (
    "        # kube-agents patch: a marked failure wake's reply is drawn as one; see\n"
    "        # gateway/slack_ux_failure.py. Unmarked, begin returns None.\n"
    "        _kage_failure_token = _kage_slack_failure.begin(event)\n"
    "        try:\n"
    "            result = await delivery_adapter._send_with_retry(\n"
    "                chat_id=event.source.chat_id, content=text_content, reply_to=reply_to, metadata=metadata)\n"
    "        finally:\n"
    "            _kage_slack_failure.end(_kage_failure_token)\n"
)

START_ANCHOR = '            await self._run_processing_hook("on_processing_start", event)\n'
START_PATCHED = START_ANCHOR + (
    "            # kube-agents patch: a failure wake's turn claims its thread's mark;\n"
    "            # see gateway/slack_ux_failure.py.\n"
    "            _kage_slack_failure.start(event)\n"
)

SLACK_ADAPTER = "plugins/platforms/slack/adapter.py"

BLOCKS_ANCHOR = "    def _maybe_blocks(self, content: str) -> Optional[list]:\n"
BLOCKS_PATCHED = (
    "    def _maybe_blocks(self, content: str) -> Optional[list]:\n"
    "        # kube-agents patch: a marked failure reply gets its bold lead and offer;\n"
    "        # see gateway/slack_ux_failure.py. Unmarked, this is upstream's.\n"
    "        return _kage_slack_failure.maybe_blocks(content, self._kage_upstream_maybe_blocks)\n"
    "\n"
    "    def _kage_upstream_maybe_blocks(self, content: str) -> Optional[list]:\n"
)

RUN_TURN = "gateway/run_turn.py"

FOLLOWUP_ANCHOR = '        await _run_followup_processing_hook(_hook_adapter, pending_event, "on_processing_start")\n'
FOLLOWUP_PATCHED = FOLLOWUP_ANCHOR + (
    "        # kube-agents patch: a queued failure wake's turn claims its thread's\n"
    "        # mark, and a user's message clears an older one; see\n"
    "        # gateway/slack_ux_failure.py.\n"
    "        _kage_slack_failure.drop(turn_ctx.source, pending_event)\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    notifier = patchlib.Patch(root, NOTIFIER, prefix=PREFIX)
    notifier.refuse_if_patched(BUILD_MARKER)
    notifier.substitute(WAKE_ANCHOR, WAKE_PATCHED, label="moments wake-text line")
    notifier.append(GATEWAY_IMPORT)

    base = patchlib.Patch(root, BASE, prefix=PREFIX)
    base.refuse_if_patched(BUILD_MARKER)
    base.substitute(FINAL_ANCHOR, FINAL_PATCHED, label="send_final_ledgered send")
    base.substitute(START_ANCHOR, START_PATCHED, label="_process_message_background processing hook")
    base.append(GATEWAY_IMPORT)

    slack_adapter = patchlib.Patch(root, SLACK_ADAPTER, prefix=PREFIX)
    slack_adapter.refuse_if_patched(BUILD_MARKER)
    slack_adapter.substitute(BLOCKS_ANCHOR, BLOCKS_PATCHED, label="SlackAdapter._maybe_blocks")
    slack_adapter.append(GATEWAY_IMPORT)

    run_turn = patchlib.Patch(root, RUN_TURN, prefix=PREFIX)
    run_turn.refuse_if_patched(BUILD_MARKER)
    run_turn.substitute(FOLLOWUP_ANCHOR, FOLLOWUP_PATCHED, label="_run_agent_queued_followup processing hook")
    run_turn.append(GATEWAY_IMPORT)

    notifier.commit("1 anchor, 1 import")
    base.commit("2 anchors, 1 import")
    slack_adapter.commit("1 anchor, 1 import")
    run_turn.commit("1 anchor, 1 import")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
