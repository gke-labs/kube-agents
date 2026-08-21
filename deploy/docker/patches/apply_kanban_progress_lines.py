#!/usr/bin/env python3
"""Wire gateway/kanban_progress_lines.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. Five anchored
edits across two files plus an appended import, with the same guarantee as the
other patches in that file: every anchor must be found exactly once, every
edited file must still parse, and anything else fails the build loudly rather
than shipping an image whose delegated cards went quiet again.

Why the patch exists is documented in the module docstring of
``deploy/docker/patches/kanban_progress_lines.py``. Usage::

    python3 apply_kanban_progress_lines.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import patchlib

# --- claim heartbeat events ------------------------------------------------
#
# TERMINAL_KINDS is the kind filter the notifier claims events with. Heartbeat
# is not terminal and the name now undersells it, but widening this tuple is
# what puts the events in front of the render below; the alternative is a
# second claim path for one kind.
#
# Located rather than spelled out. Upstream owns this tuple's membership and
# adds to it — v2026.8.13 appended "review_requested" — so a literal anchor on
# the whole line failed the build every time upstream gained a kind, which is
# churn this patch has no opinion about. What it does have an opinion about is
# that the tuple is still the terminal-kind filter, which is what
# expect_contains asserts.

KINDS_COMMENT = (
    '# kube-agents patch: "heartbeat" is NOT terminal. It is claimed\n'
    "# here so mid-run progress notes reach the subscriber's thread;\n"
    "# the render below drops the noteless auto-heartbeats, and the\n"
    "# kind is deliberately absent from _WAKE_KINDS so a progress line\n"
    "# costs no LLM turn. See gateway/kanban_progress_lines.py.\n"
)

#: Kinds the filter must still carry for it to be the one this patch means.
#: Not the whole tuple: upstream may add to it, and this patch does not care.
KINDS_EXPECTED = ("completed", "blocked", "gave_up", "crashed", "timed_out")

# --- render a note as a chat line ------------------------------------------
#
# Inserted ahead of the "status" branch, inside the per-event if/elif chain.
# The 24-space indent is load-bearing: this sits inside
# ``for d in deliveries: ... for ev in d["events"]:``.

RENDER_ANCHOR = '                        elif kind == "status":\n'

RENDER_PATCHED = (
    '                        elif kind == "heartbeat":\n'
    "                            # kube-agents patch: mid-run progress. Only a\n"
    "                            # deliberate kanban_heartbeat(note=...) carries\n"
    "                            # a note; the per-tool-call auto-heartbeats have\n"
    "                            # payload=None and fall through to the same\n"
    "                            # silent `continue` archived/unblocked use, so\n"
    "                            # the cursor still advances past them.\n"
    "                            note = _progress_note(ev.payload)\n"
    "                            if not note:\n"
    "                                continue\n"
    '                            msg = f"⏳ {board_tag}{tag}{note}"\n'
) + RENDER_ANCHOR

# --- roll consecutive progress notes into one message -----------------------
#
# The notifier funnels every line it posts — progress and terminal alike —
# through this one send. Routing it through ``deliver`` is therefore the whole
# of the rolling-message behaviour: one anchor, and no branch of the render
# chain above needs to know about it. Every name passed is already in scope
# here (``board_tag`` is computed once per delivery, ``tag``/``kind``/``ev``
# per event).
#
# The 24-space indent on ``try:`` and 28 on its body place this inside
# ``for d in deliveries: ... for ev in d["events"]:``, the same block the
# render branch above is inserted into.

SEND_ANCHOR = (
    "                        try:\n"
    "                            _send_res = await adapter.send(\n"
    '                                sub["chat_id"], msg, metadata=metadata,\n'
    "                            )\n"
)

SEND_PATCHED = (
    "                        try:\n"
    "                            # kube-agents patch: one rolling message per\n"
    "                            # card. A progress note edits the message the\n"
    "                            # card already has instead of posting another,\n"
    "                            # so a five-milestone card pings the space once\n"
    "                            # rather than five times. A terminal event\n"
    "                            # settles that message and then posts its own,\n"
    "                            # which is the notification people want. Any\n"
    "                            # platform that cannot edit falls back to this\n"
    "                            # same send(). The return value keeps send()'s\n"
    "                            # shape, so the SendResult check below and the\n"
    "                            # failure accounting are both unchanged.\n"
    "                            # See gateway/kanban_progress_lines.py.\n"
    "                            _send_res = await _progress_deliver(\n"
    "                                self, adapter, sub, kind, ev, msg, metadata,\n"
    '                                header=f"{board_tag}{tag}",\n'
    "                            )\n"
)

IMPORT_LINE = (
    "\n\n# kube-agents patch: see gateway/kanban_progress_lines.py\n"
    "from gateway.kanban_progress_lines import progress_note as _progress_note\n"
    "from gateway.kanban_progress_lines import deliver as _progress_deliver\n"
)

# --- tell the model what a note actually does -------------------------------
#
# The tool schema is the description the model reads at the moment it decides
# whether to call this. Upstream's says the note is "Shown in the event log",
# which directly contradicts what the personas now promise and reads as "nobody
# will see this" — the surest way to get a feature nobody uses. The personas
# instruct the behaviour; this stops the tool from arguing with them.

SCHEMA_ANCHOR = (
    '    "description": (\n'
    '        "Signal that you\'re still alive during a long operation "\n'
    '        "(training, encoding, large crawls). Call every few minutes so "\n'
    '        "humans see liveness separately from PID checks. Pure side "\n'
    '        "effect — no work changes."\n'
    "    ),\n"
)

SCHEMA_PATCHED = (
    '    "description": (\n'
    '        "Report progress on a long-running task. A note is delivered "\n'
    '        "straight into the chat thread watching this card, within "\n'
    '        "seconds, without interrupting your run or costing a turn — so "\n'
    '        "call this at every milestone the user should see rather than "\n'
    '        "leaving them in silence until you complete. Notes after the "\n'
    '        "first are added to the same message, so calling this often "\n'
    '        "builds a running log and does not spam the thread. Pure side "\n'
    '        "effect — no work changes."\n'
    "    ),\n"
)

NOTE_ANCHOR = (
    '            "note": {\n'
    '                "type": "string",\n'
    '                "description": (\n'
    '                    "Optional short note describing current progress. "\n'
    '                    "Shown in the event log."\n'
    "                ),\n"
    "            },\n"
)

NOTE_PATCHED = (
    '            "note": {\n'
    '                "type": "string",\n'
    '                "description": (\n'
    '                    "A one-line progress update, written for the human "\n'
    '                    "waiting on this card and delivered to their chat "\n'
    '                    "thread. Keep it under 300 characters; longer notes "\n'
    '                    "are clipped. Omitting it makes the heartbeat a "\n'
    '                    "silent liveness ping that nobody sees."\n'
    "                ),\n"
    "            },\n"
)

PREFIX = "kanban_progress_lines"


def apply(root: Path) -> None:
    """Apply every patch under ``root``, or raise SystemExit with the reason."""
    watchers = patchlib.Patch(root, "gateway/kanban_watchers.py", prefix=PREFIX)
    # The kinds edit widens a tuple in place instead of consuming an anchor, so
    # the count check cannot tell a fresh file from one this has already run
    # against; without this a second pass would append a second "heartbeat".
    watchers.refuse_if_patched(KINDS_COMMENT.splitlines()[0])
    kinds = watchers.find_assign("TERMINAL_KINDS", label="terminal-kind filter")
    kinds.expect_contains(*KINDS_EXPECTED)
    widened = f'{kinds.value_text} + ("heartbeat",)'
    watchers.splice(kinds.value_start, kinds.value_end, widened)
    watchers.insert(
        kinds.line_start, textwrap.indent(KINDS_COMMENT, kinds.indent)
    )
    watchers.substitute(RENDER_ANCHOR, RENDER_PATCHED, label="heartbeat render")
    watchers.substitute(SEND_ANCHOR, SEND_PATCHED, label="notifier send")
    watchers.append(IMPORT_LINE)
    watchers.commit("1 locator, 2 anchors")

    tools = patchlib.Patch(root, "tools/kanban_tools.py", prefix=PREFIX)
    tools.substitute(SCHEMA_ANCHOR, SCHEMA_PATCHED, label="heartbeat description")
    tools.substitute(NOTE_ANCHOR, NOTE_PATCHED, label="heartbeat note description")
    tools.commit("2 anchors")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
