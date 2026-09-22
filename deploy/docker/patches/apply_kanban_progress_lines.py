#!/usr/bin/env python3
"""Wire gateway/kanban_progress_lines.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. One locator and
three anchored edits in ``gateway/kanban_watchers_notifier.py``, one locator
and one anchor for the heartbeat formatter, two anchors in
``tools/kanban_tools_schemas.py``, plus an appended import — with the same
guarantee as the other patches in that file: every anchor must be found
exactly once, every edited file must still parse, and anything else fails the
build loudly rather than shipping an image whose delegated cards went quiet
again.

Where the sites live, as of v2026.9.14. Upstream's September decomposition
(``fd2bfa1893``) moved the notifier's per-tick claim and per-subscription
delivery out of ``gateway/kanban_watchers.py`` into
``gateway/kanban_watchers_notifier.py``, and turned the per-event
``if kind == …`` chain into an ``_EVENT_FORMATTERS`` table read by
``_KanbanNotification.format_event``. The three edits this patch used to make
inside one loop body are now:

* the ``TERMINAL_KINDS`` filter, module-level in the notifier module (still
  located by name, still asserted to be the terminal-kind filter);
* a ``heartbeat`` entry in the formatter table, backed by a ``_fmt_heartbeat``
  inserted after the last upstream formatter. ``format_event`` treats a
  ``None`` message as "silent kind", which is exactly the behaviour the old
  ``continue`` gave a noteless auto-heartbeat, so no control-flow edit is
  needed any more;
* the one ``adapter.send`` in ``_KanbanNotification._send_event``, routed
  through ``deliver``. The message map is hung off ``self.runner`` — the
  ``GatewayRunner`` — and not off the ``_KanbanNotification``, which upstream
  now constructs afresh for every delivery and which would therefore forget
  the message id between one tick and the next.

The tool schema moved from ``tools/kanban_tools.py`` to
``tools/kanban_tools_schemas.py`` in the same decomposition and is now built
by ``_schema(...)`` / ``_prop(...)`` helpers; the two description anchors
follow it there.

Why the patch exists is documented in the module docstring of
``deploy/docker/patches/kanban_progress_lines.py``. Usage::

    python3 apply_kanban_progress_lines.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import patchlib

NOTIFIER_RELATIVE = "gateway/kanban_watchers_notifier.py"
SCHEMAS_RELATIVE = "tools/kanban_tools_schemas.py"

# --- claim heartbeat events ------------------------------------------------
#
# TERMINAL_KINDS is the kind filter the notifier claims events with. Heartbeat
# is not terminal and the name now undersells it, but widening this tuple is
# what puts the events in front of the formatter below; the alternative is a
# second claim path for one kind.
#
# Located rather than spelled out. Upstream owns this tuple's membership and
# adds to it — v2026.8.13 appended "review_requested", v2026.9.14
# "changes_requested" — so a literal anchor on the whole line failed the build
# every time upstream gained a kind, which is churn this patch has no opinion
# about. What it does have an opinion about is that the tuple is still the
# terminal-kind filter, which is what expect_contains asserts.

KINDS_COMMENT = (
    '# kube-agents patch: "heartbeat" is NOT terminal. It is claimed\n'
    "# here so mid-run progress notes reach the subscriber's thread;\n"
    "# _fmt_heartbeat below drops the noteless auto-heartbeats, and the\n"
    "# kind is deliberately absent from _WAKE_KINDS so a progress line\n"
    "# costs no LLM turn. See gateway/kanban_progress_lines.py.\n"
)

#: Kinds the filter must still carry for it to be the one this patch means.
#: Not the whole tuple: upstream may add to it, and this patch does not care.
KINDS_EXPECTED = ("completed", "blocked", "gave_up", "crashed", "timed_out")

# --- the header a progress line carries ------------------------------------
#
# Upstream folds the board tag and the @-mention into ``self.head`` together
# with "Kanban <id>". A progress line has always been the bare
# ``⏳ [board] @assignee note`` — the card id is on the message it rolls up to,
# not on every milestone — so the two halves are kept on the notification as
# well, one line after upstream builds ``head`` from them. ``tag`` is the local
# upstream computes on the line above the anchor.

HEADER_ANCHOR = (
    '        self.head = f"{self.board_tag}{tag}Kanban {self.task_id}"\n'
)

HEADER_PATCHED = HEADER_ANCHOR + (
    "        # kube-agents patch: the header a progress line and the rolling\n"
    "        # message it belongs to share. See gateway/kanban_progress_lines.py.\n"
    '        self.progress_header = f"{self.board_tag}{tag}"\n'
)

# --- render a note as a chat line ------------------------------------------
#
# A formatter in upstream's table returns ``(msg, wake_handoff, review_detail)``
# and ``format_event`` hands ``msg`` straight back to ``_send_pings``, which
# skips a ``None``. That is the same silent path a kind with no formatter
# takes, so a noteless auto-heartbeat is dropped and the cursor still advances
# past it — what the old ``continue`` did, without editing control flow.
#
# Inserted after the last upstream ``_fmt_*`` def (located by name, so a
# signature change there does not move the insertion) and registered ahead of
# every upstream entry, which is the only position one anchor on the table's
# opening line can give it. Membership is unordered; the position is not
# load-bearing.

FORMATTER_AFTER = "_fmt_changes_requested"

FORMATTER_DEF = (
    "\n\n"
    "def _fmt_heartbeat(ev, n) -> tuple:\n"
    "    # kube-agents patch: mid-run progress. Only a deliberate\n"
    "    # kanban_heartbeat(note=...) carries a note; the per-tool-call\n"
    "    # auto-heartbeats have payload=None and render as None, which\n"
    "    # format_event() passes through and _send_pings() skips, so they\n"
    "    # stay silent while the cursor still advances past them.\n"
    "    # See gateway/kanban_progress_lines.py.\n"
    "    note = _progress_note(ev.payload)\n"
    '    return (f"⏳ {n.progress_header}{note}" if note else None), None, None\n'
)

FORMATTERS_ANCHOR = (
    '_EVENT_FORMATTERS: dict[str, Callable[[Any, "_KanbanNotification"], tuple]] = {\n'
)

FORMATTERS_PATCHED = FORMATTERS_ANCHOR + (
    '    "heartbeat": _fmt_heartbeat,  # kube-agents patch\n'
)

# --- roll consecutive progress notes into one message -----------------------
#
# The notifier funnels every line it posts — progress and terminal alike —
# through this one send in ``_KanbanNotification._send_event``. Routing it
# through ``deliver`` is therefore the whole of the rolling-message behaviour:
# one anchor, and no formatter above needs to know about it. ``sub``,
# ``adapter``, ``msg`` and ``metadata`` are the method's own locals; ``ev`` is
# its parameter.
#
# The 8-space indent places this inside a method body of the class.

SEND_ANCHOR = (
    '        _send_res = await adapter.send(sub["chat_id"], msg, metadata=metadata)\n'
)

SEND_PATCHED = (
    "        # kube-agents patch: one rolling message per card. A progress note\n"
    "        # edits the message the card already has instead of posting\n"
    "        # another, so a five-milestone card pings the space once rather\n"
    "        # than five times. A terminal event settles that message and then\n"
    "        # posts its own, which is the notification people want. Any\n"
    "        # platform that cannot edit falls back to this same send(). The\n"
    "        # return value keeps send()'s shape, so the SendResult check below\n"
    "        # and the failure accounting are both unchanged. The map is hung\n"
    "        # off the runner: this object is rebuilt for every delivery.\n"
    "        # See gateway/kanban_progress_lines.py.\n"
    "        _send_res = await _progress_deliver(\n"
    "            self.runner, adapter, sub, ev.kind, ev, msg, metadata,\n"
    "            header=self.progress_header,\n"
    "        )\n"
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
#
# The anchors are the argument text of upstream's ``_schema(...)`` /
# ``_prop(...)`` calls, not a dict literal: the schema module builds every
# tool's schema through those helpers.

SCHEMA_ANCHOR = (
    "    (\n"
    '        "Signal that you\'re still alive during a long operation "\n'
    '        "(training, encoding, large crawls). Call every few minutes so "\n'
    '        "humans see liveness separately from PID checks. Pure side "\n'
    '        "effect — no work changes."\n'
    "    ),\n"
)

SCHEMA_PATCHED = (
    "    (\n"
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
    '        "note": _prop("string", (\n'
    '                "Optional short note describing current progress. "\n'
    '                "Shown in the event log."\n'
    "        )),\n"
)

NOTE_PATCHED = (
    '        "note": _prop("string", (\n'
    '                "A one-line progress update, written for the human "\n'
    '                "waiting on this card and delivered to their chat "\n'
    '                "thread. Keep it under 300 characters; longer notes "\n'
    '                "are clipped. Omitting it makes the heartbeat a "\n'
    '                "silent liveness ping that nobody sees."\n'
    "        )),\n"
)

PREFIX = "kanban_progress_lines"


def apply(root: Path) -> None:
    """Apply every patch under ``root``, or raise SystemExit with the reason."""
    notifier = patchlib.Patch(root, NOTIFIER_RELATIVE, prefix=PREFIX)
    # The kinds edit widens a tuple in place instead of consuming an anchor, so
    # the count check cannot tell a fresh file from one this has already run
    # against; without this a second pass would append a second "heartbeat".
    notifier.refuse_if_patched(KINDS_COMMENT.splitlines()[0])
    kinds = notifier.find_assign("TERMINAL_KINDS", label="terminal-kind filter")
    kinds.expect_contains(*KINDS_EXPECTED)
    widened = f'{kinds.value_text} + ("heartbeat",)'
    notifier.splice(kinds.value_start, kinds.value_end, widened)
    notifier.insert(
        kinds.line_start, textwrap.indent(KINDS_COMMENT, kinds.indent)
    )
    notifier.substitute(HEADER_ANCHOR, HEADER_PATCHED, label="progress header")
    # Located after the tuple edit above, which changed offsets before it.
    last_formatter = notifier.find_def(FORMATTER_AFTER, label="last formatter")
    notifier.insert(last_formatter.after, FORMATTER_DEF)
    notifier.substitute(
        FORMATTERS_ANCHOR, FORMATTERS_PATCHED, label="heartbeat formatter entry"
    )
    notifier.substitute(SEND_ANCHOR, SEND_PATCHED, label="notifier send")
    notifier.append(IMPORT_LINE)
    notifier.commit("2 locators, 3 anchors")

    schemas = patchlib.Patch(root, SCHEMAS_RELATIVE, prefix=PREFIX)
    schemas.substitute(SCHEMA_ANCHOR, SCHEMA_PATCHED, label="heartbeat description")
    schemas.substitute(NOTE_ANCHOR, NOTE_PATCHED, label="heartbeat note description")
    schemas.commit("2 anchors")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
