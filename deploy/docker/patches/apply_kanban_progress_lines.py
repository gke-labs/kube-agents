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
from pathlib import Path

# --- claim heartbeat events ------------------------------------------------
#
# TERMINAL_KINDS is the kind filter the notifier claims events with. Heartbeat
# is not terminal and the name now undersells it, but widening this tuple is
# what puts the events in front of the render below; the alternative is a
# second claim path for one kind.

KINDS_ANCHOR = (
    '        TERMINAL_KINDS = ("completed", "blocked", "gave_up", "crashed", '
    '"timed_out", "status", "archived", "unblocked", "block_loop_detected")\n'
)

KINDS_PATCHED = (
    "        # kube-agents patch: \"heartbeat\" is NOT terminal. It is claimed\n"
    "        # here so mid-run progress notes reach the subscriber's thread;\n"
    "        # the render below drops the noteless auto-heartbeats, and the\n"
    "        # kind is deliberately absent from _WAKE_KINDS so a progress line\n"
    "        # costs no LLM turn. See gateway/kanban_progress_lines.py.\n"
    '        TERMINAL_KINDS = ("completed", "blocked", "gave_up", "crashed", '
    '"timed_out", "status", "archived", "unblocked", "block_loop_detected", '
    '"heartbeat")\n'
)

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

# (relative path, [(anchor, replacement, expected occurrences)], appended text)
PATCHES = (
    (
        "gateway/kanban_watchers.py",
        (
            (KINDS_ANCHOR, KINDS_PATCHED, 1),
            (RENDER_ANCHOR, RENDER_PATCHED, 1),
            (SEND_ANCHOR, SEND_PATCHED, 1),
        ),
        IMPORT_LINE,
    ),
    (
        "tools/kanban_tools.py",
        (
            (SCHEMA_ANCHOR, SCHEMA_PATCHED, 1),
            (NOTE_ANCHOR, NOTE_PATCHED, 1),
        ),
        "",
    ),
)


def apply(root: Path) -> None:
    """Apply every patch under ``root``, or raise SystemExit with the reason."""
    for relative, edits, append in PATCHES:
        path = root / relative
        if not path.is_file():
            raise SystemExit(f"kanban_progress_lines patch: {path} does not exist")
        source = path.read_text()
        for anchor, replacement, expected in edits:
            found = source.count(anchor)
            if found != expected:
                raise SystemExit(
                    f"kanban_progress_lines patch: {relative}: expected "
                    f"{expected} occurrence(s) of anchor, found {found}. "
                    f"Upstream Hermes changed — re-derive the anchor before "
                    f"bumping the base image.\n--- anchor ---\n{anchor}"
                )
            source = source.replace(anchor, replacement)
        source += append
        try:
            # compile(), not ast.parse(): the inserted branch ends in a `continue`,
            # and ast.parse accepts a `continue` outside a loop — only the compile
            # step rejects it. A misplaced insertion would otherwise reach the
            # image and fail at import, in the gateway, at runtime.
            compile(source, str(relative), "exec")
        except SyntaxError as e:
            raise SystemExit(
                f"kanban_progress_lines patch: {relative} no longer parses "
                f"after patching: {e}"
            )
        path.write_text(source)
        print(f"kanban_progress_lines patch: {relative} ({len(edits)} anchors)")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
