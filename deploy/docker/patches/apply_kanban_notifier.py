#!/usr/bin/env python3
"""Wire gateway/kanban_notifier.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. Five anchored
edits in ``gateway/kanban_watchers_notifier.py`` plus one import trailer. Two
of the anchors are the same two sites this applier has always owned — the
completion handoff and the wake set — the third carries the incident-store
call, which used to share the wake anchor and no longer can (below), the
fourth routes the completion message through ``completion_text`` so
``KAGE_SLACK_UX`` can drop its head line on Slack, and the fifth settles the
failure lines that flag held for the wake. One more anchor is substituted
unchanged: the ``platform_str`` binding the completion message reads.

Where the sites live, as of v2026.9.14. Upstream's September decomposition
(``fd2bfa1893``) moved the notifier's per-subscription delivery out of the
``_kanban_notifier_watcher`` loop body in ``gateway/kanban_watchers.py`` and
into ``gateway/kanban_watchers_notifier.py``, as a ``_KanbanNotification``
object and an ``_EVENT_FORMATTERS`` table:

* **The handoff** is built by the module-level ``_fmt_completed(ev, n)``.
  Upstream still hard-slices it — now through ``_first_line(text, 200)`` and
  ``_first_line(text, 160)`` rather than ``lines[0][:200]`` — and still
  carries only that slice into the message, so both the clip and the
  ``result`` delivery are as needed as they were. The two slices become
  ``_clip_handoff`` calls and the hook is appended after the ``handoff =``
  line, exactly as before; only the spelling around them moved.
* **The wake set** is computed by ``_KanbanNotification.build_wake_text``,
  which ``deliver()`` calls right after ``_send_pings()`` has returned True —
  so, as before, control reaches it only once every text ping for the
  delivery has been sent. Upstream's per-subscription gate is now
  ``if self.wake_agent``; it is kept verbatim around the helper for the same
  reason as always (see the comment on :data:`WAKE_PATCHED`). The marker call
  follows the assignment there.
* **The incident call** used to sit beside the marker call, because in the
  old ``for … else`` loop that one spot was both "after every ping" and
  "inside the per-event loop with ``ev`` bound". Those are two places now:
  ``build_wake_text`` has no event in scope, and the per-event loop is
  ``_send_pings``. The call goes where its premise actually holds — right
  after ``_send_event`` returned for *this* event and the send was accounted
  for — which is a stronger guarantee than the old site gave (the old site
  also ran on the non-push path, where nothing had been sent).
* **The completion message** is ``_fmt_completed``'s ``return``: its
  ``✔ … done`` f-string becomes a ``completion_text`` call, which returns the
  same text unless ``KAGE_SLACK_UX`` is on and ``n.platform_str`` is Slack.
* **The wake step's outcome** is the ``if wake_kinds: try: await self.wake()``
  block in ``_KanbanNotification.deliver()``, above the ``advance()`` tail.
  Each way out of it (no wake, wake admitted, wake raised) settles the failure
  lines anchor 5 describes; ``WakeNotAccepted`` leaves them held.

The old ``LegacyEquivalenceTest`` claim — that this applier's output is
byte-identical to three superseded appliers' — cannot be made against a
notifier those appliers never saw. ``test_kanban_notifier.py`` now pins the
next best thing: the patched module differs from upstream by exactly the
lines named here, so each behaviour stays visible as one block.

Not merged in, deliberately:

* ``gateway/kanban_handoff_clip.py`` stays a module of its own — it is a
  dependency-free text utility that ``tools/cron_run_scope.py`` also imports,
  and it needs to exist earlier in the build than this applier runs. It carries
  no anchor into upstream source, so it is not part of this patch's coupling to
  the notifier; only its *wiring* is, and that is anchor 1 here.
* ``hermes_cli/kanban_wake_nudge.py`` edits ``gateway/kanban_watchers.py``, at
  the watcher loops' construction and sleep sites, which stayed in that file.
  Disjoint from every anchor here; it appends its own trailer to its own file.
* ``gateway/kanban_notify_delivery.py`` edits the claim in
  ``_Collector._claim_for_sub`` and the ``await self.advance()`` at the
  success-path tail of ``_KanbanNotification.deliver()`` — both in this file,
  neither touched here. Anchor 5 ends inside the wake step's ``try``, above
  that tail.

Why the changes are needed is documented in the module docstrings of
``deploy/docker/patches/kanban_notifier.py`` and
``deploy/docker/patches/kanban_handoff_clip.py``. Usage::

    python3 apply_kanban_notifier.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import patchlib

RELATIVE = "gateway/kanban_watchers_notifier.py"

#: Nesting depth of ``_fmt_completed``'s body: a module-level function.
HANDOFF_INDENT = " " * 4
#: Nesting depth of ``build_wake_text``'s body: a method.
WAKE_INDENT = " " * 8
#: Nesting depth of the post-send lines in ``_send_pings``: method, ``for``,
#: ``try``.
PING_INDENT = " " * 16
#: One block outward from those lines: the ``except`` that closes their
#: ``try``, which the incident anchor ends on.
PING_EXCEPT_INDENT = " " * 12
#: The wake step in ``deliver()``: method body, then ``if``, ``try``, and the
#: ``except`` bodies.
DELIVER_INDENT = " " * 8
WAKE_TRY_INDENT = " " * 16

# --- Anchor 1: the completion handoff ----------------------------------------
#
# Covers both of upstream's hard slices (the 200-character one that severed a
# published ledger URL down to ".../is" on 2026-08-03, and the 160-character one
# in the no-summary branch) and ends where the hook has to go.
#
# ``wake_handoff`` is upstream's name for the clipped string it keeps for the
# synthetic wake turn (#70752); v2026.9.14 derives ``handoff`` from it rather
# than the other way round. It is carried through unchanged, which means the
# wake turn inherits the un-severed clip for free — a strict improvement on the
# hard slice, and the most this patch should do to it. The full report is
# deliberately NOT handed to the wake turn: it goes on the delivered message,
# below, where a human reads it. See section 1 of gateway/kanban_notifier.py.

HANDOFF_ANCHOR = (
    f"{HANDOFF_INDENT}if payload_summary:\n"
    f"{HANDOFF_INDENT}    wake_handoff = _first_line(str(payload_summary), 200)\n"
    f"{HANDOFF_INDENT}elif n.task and n.task.result:\n"
    f"{HANDOFF_INDENT}    wake_handoff = _first_line(n.task.result, 160)\n"
    f'{HANDOFF_INDENT}handoff = f"\\n{{wake_handoff}}" if wake_handoff is not None else ""\n'
)

# Assignment, not ``+=``. In the branch that has no event summary the notifier
# has just put a 1200-character clip of ``task.result`` into ``handoff``, and
# the report is about to be printed in full underneath it; only something that
# owns the whole tail can drop that clip. Appending sent the opening of a report
# and then the report — on a 60-line cron catalogue, jobs 1 to 19 arrived twice.
# See the comment block in gateway/kanban_notifier.py.
HANDOFF_PATCHED = (
    f"{HANDOFF_INDENT}# kube-agents patch: see gateway/kanban_notifier.py\n"
    f"{HANDOFF_INDENT}if payload_summary:\n"
    f"{HANDOFF_INDENT}    wake_handoff = _clip_handoff(payload_summary)\n"
    f"{HANDOFF_INDENT}elif n.task and n.task.result:\n"
    f"{HANDOFF_INDENT}    wake_handoff = _clip_handoff(n.task.result)\n"
    f'{HANDOFF_INDENT}handoff = f"\\n{{wake_handoff}}" if wake_handoff is not None else ""\n'
    f"{HANDOFF_INDENT}handoff = _kanban_handoff_with_result(handoff, n.task)\n"
)

# --- Anchor 2: the wake set ---------------------------------------------------
#
# The ``task, sub = …`` line is part of the anchor because the marker call
# below reads both locals; asserting they are still bound there is what keeps
# a NameError out of the delivery path.

WAKE_ANCHOR = (
    f"{WAKE_INDENT}task, sub = self.task, self.sub\n"
    f'{WAKE_INDENT}self.wake_kinds = {{ev.kind for ev in self.d["events"] if ev.kind in _WAKE_KINDS}} if self.wake_agent else set()\n'
)

# `adapter=` and `passive_delivered=` are both load-bearing, not decoration.
# Each names one way the notifier reaches this point having sent no text ping,
# and where nothing was sent the wake IS the delivery: narrowing it away drops
# the completion on the floor. `adapter=` covers the non-push platforms;
# `passive_delivered=self.send_passive` covers v2026.8.13's `delivery_mode="wake"`
# subscriptions, which suppress the ping on a push adapter too. See
# wake_kinds_for's docstring.
#
# ``if self.wake_agent else set()`` is upstream's, added in v2026.8.13 along
# with the subscription's ``delivery_mode`` (notify / notify+wake / wake), and
# it is kept verbatim rather than folded into the helper. The two gates answer
# different questions and both have to hold: upstream's is "did this subscriber
# ask to be woken at all", ours is "is this event kind worth a model turn".
# Collapsing them would put a per-subscription setting inside a module-level
# config reader.
#
# Note that ``wake_agent`` and ``send_passive`` partition the three modes
# differently and neither implies the other, which is the trap this arrangement
# exists to avoid: ``notify`` has a ping and no wake, ``notify+wake`` has both,
# and ``wake`` has a wake and no ping. Only the middle one has an already-
# delivered answer for the narrowing to be redundant with.
#
# The marker call has to sit at this exact point and no earlier: ``deliver()``
# calls ``build_wake_text()`` only after ``_send_pings()`` returned True, i.e.
# when every text ping for the delivery has been sent, which is what makes "its
# result was already delivered to this conversation" — the claim the note makes
# to the creator — true. It reads ``self.wake_kinds`` to work out what the
# narrowing suppressed, so it must also come after the assignment above.
#
# It takes `wake_configured=self.wake_agent` for the same reason the gate is
# kept separate: with mode="notify" the wake set is empty because the
# *subscriber* turned waking off, and a note claiming this patch suppressed the
# wake would be a lie told on every completion of every notify-only card. See
# section 4 of gateway/kanban_notifier.py.
MARKER_CALL = (
    f"{WAKE_INDENT}_kanban_note_suppressed(\n"
    f'{WAKE_INDENT}    self.runner, self.d["events"], self.wake_kinds, task, sub, self.board_slug,\n'
    f"{WAKE_INDENT}    wake_configured=self.wake_agent,\n"
    f"{WAKE_INDENT})\n"
)

WAKE_PATCHED = (
    f"{WAKE_INDENT}task, sub = self.task, self.sub\n"
    f"{WAKE_INDENT}# kube-agents patch: see gateway/kanban_notifier.py\n"
    f"{WAKE_INDENT}self.wake_kinds = (\n"
    f"{WAKE_INDENT}    _wake_kinds_for(\n"
    f'{WAKE_INDENT}        self.d["events"], adapter=self.adapter, passive_delivered=self.send_passive\n'
    f"{WAKE_INDENT}    )\n"
    f"{WAKE_INDENT}    if self.wake_agent\n"
    f"{WAKE_INDENT}    else set()\n"
    f"{WAKE_INDENT})\n"
) + MARKER_CALL

# --- Anchor 3: the incident row -----------------------------------------------
#
# The post-send tail of ``_send_pings``: ``_send_event(ev, msg)`` has returned,
# the ping is checkpointed, the failure counter cleared. Every one of those is
# the premise of storing the report for the reader to reply to — "the reader
# has this report" — so the call goes after all three and inside the same
# ``try``, where a send that raised never reaches it.
#
# ``ev``, not ``self.d["events"]``: this loop runs once per event, and the
# helper asks whether *this* send was the report. Handed the list it would fire
# on a ``commented`` event too, storing the row before the ``completed``
# iteration sends the report it claims the reader has. ``INSERT OR IGNORE``
# would absorb the duplicate row; the premise and the log line are what it
# would not absorb.
#
# ``posted=self.send_passive`` for the same reason the marker call takes it:
# with delivery_mode="wake" the agent is woken and the thread gets no message,
# so there is no delivered report to key to it. On this path it is always True
# — ``_send_pings`` skips the send for wake-only subscriptions above — and it
# is passed anyway so the helper's contract is spelled at its call site.
#
# See section 5 of gateway/kanban_notifier.py.

INCIDENT_ANCHOR = (
    f"{PING_INDENT}self.clear_failures()\n"
    f"{PING_EXCEPT_INDENT}except Exception as exc:\n"
)

INCIDENT_CALL = (
    f"{PING_INDENT}_kanban_store_incident(ev, self.task, self.sub, posted=self.send_passive)\n"
)

INCIDENT_PATCHED = (
    f"{PING_INDENT}self.clear_failures()\n"
    f"{PING_INDENT}# kube-agents patch: see gateway/kanban_notifier.py\n"
) + INCIDENT_CALL + (
    f"{PING_EXCEPT_INDENT}except Exception as exc:\n"
)

# --- Anchor 4: the completion message ------------------------------------------
#
# ``_fmt_completed``'s return, which prefixes the handoff with
# ``✔ <head> done — <title>``. ``completion_text`` returns that same string
# unless ``KAGE_SLACK_UX`` is on and the subscription is Slack, where it
# returns the handoff alone. ``n.platform_str`` is upstream's lower-cased
# platform, set in ``_KanbanNotification.__init__``. See section 6 of
# gateway/kanban_notifier.py.

COMPLETION_ANCHOR = (
    f'{HANDOFF_INDENT}return f"✔ {{n.head}} done — {{n.title}}{{handoff}}", wake_handoff, None\n'
)

COMPLETION_CALL = (
    "_kanban_completion_text(n.head, n.title, handoff, n.platform_str)"
)

#: The completion call reads ``n.platform_str`` ahead of the flag, and no
#: other anchor holds it. Pinned unchanged and inside
#: ``_KanbanNotification.__init__``, so an upstream that renames, rewrites,
#: duplicates or moves the binding fails the build instead of raising
#: AttributeError on every completion.
PLATFORM_BINDING = '        self.platform_str = (sub["platform"] or "").lower()\n'
PLATFORM_CLASS = "_KanbanNotification"
PLATFORM_METHOD = "__init__"

COMPLETION_PATCHED = (
    f"{HANDOFF_INDENT}# kube-agents patch: see gateway/kanban_notifier.py\n"
    f"{HANDOFF_INDENT}return {COMPLETION_CALL}, wake_handoff, None\n"
)

# --- Anchor 5: settling the held failure lines --------------------------------
#
# With ``KAGE_SLACK_UX`` on, ``kanban_progress_lines`` holds a Slack failure
# line the creator's wake is expected to explain, and ``_send_pings`` has
# recorded it as sent. Only the wake step knows whether that expectation held,
# so the held lines are settled here, once per outcome:
#
# * no wake at all: post them. The mirror in ``explained_by_wake`` predicted a
#   wake that ``build_wake_text`` did not ask for, and the line is then the only
#   word the thread gets.
# * wake admitted: drop the kinds it was admitted for, post any others.
# * wake raised: post them, before ``_wake_failed`` rewinds or drops the
#   subscription, so even the attempt that unsubscribes tells the thread.
# * ``WakeNotAccepted``: nothing. The claim is rewound, the held line waits,
#   and the retry settles it.
#
# ``_kanban_tell_unexplained`` never raises and returns at once when nothing is
# held, which is every delivery with the flag off. See section 6 of
# gateway/kanban_notifier.py.

# ``self._owner_scope`` rides along so the held line is posted in the
# subscriber profile's scope, as ``_send_pings`` posts the pings it replaces.
TELL_WOKEN = (
    "await _kanban_tell_unexplained("
    "self.runner, self.adapter, self.sub, wake_kinds, self._owner_scope)\n"
)
TELL_NONE = (
    "await _kanban_tell_unexplained("
    "self.runner, self.adapter, self.sub, set(), self._owner_scope)\n"
)

TELL_ANCHOR = (
    f"{DELIVER_INDENT}if wake_kinds:\n"
    f"{DELIVER_INDENT}    try:\n"
    f"{DELIVER_INDENT}        await self.wake()\n"
    f"{DELIVER_INDENT}        self.clear_failures()\n"
    f"{DELIVER_INDENT}    except WakeNotAccepted:\n"
    f"{WAKE_TRY_INDENT}# Startup / full queue is not a dead destination. Keep the durable\n"
    f"{WAKE_TRY_INDENT}# subscription alive regardless of how long admission takes.\n"
    f"{WAKE_TRY_INDENT}await self.rewind()\n"
    f"{WAKE_TRY_INDENT}return\n"
    f"{DELIVER_INDENT}    except Exception as _wk_err:\n"
)

TELL_PATCHED = (
    f"{DELIVER_INDENT}# kube-agents patch: see gateway/kanban_notifier.py\n"
    f"{DELIVER_INDENT}if not wake_kinds:\n"
    f"{DELIVER_INDENT}    {TELL_NONE}"
    f"{DELIVER_INDENT}if wake_kinds:\n"
    f"{DELIVER_INDENT}    try:\n"
    f"{DELIVER_INDENT}        await self.wake()\n"
    f"{DELIVER_INDENT}        self.clear_failures()\n"
    f"{WAKE_TRY_INDENT}{TELL_WOKEN}"
    f"{DELIVER_INDENT}    except WakeNotAccepted:\n"
    f"{WAKE_TRY_INDENT}# Startup / full queue is not a dead destination. Keep the durable\n"
    f"{WAKE_TRY_INDENT}# subscription alive regardless of how long admission takes.\n"
    f"{WAKE_TRY_INDENT}await self.rewind()\n"
    f"{WAKE_TRY_INDENT}return\n"
    f"{DELIVER_INDENT}    except Exception as _wk_err:\n"
    f"{WAKE_TRY_INDENT}{TELL_NONE}"
)

EDITS = (
    ("completion handoff", HANDOFF_ANCHOR, HANDOFF_PATCHED),
    ("wake set", WAKE_ANCHOR, WAKE_PATCHED),
    ("incident row", INCIDENT_ANCHOR, INCIDENT_PATCHED),
    ("completion message", COMPLETION_ANCHOR, COMPLETION_PATCHED),
    ("held failure lines", TELL_ANCHOR, TELL_PATCHED),
)

# Appended rather than inserted: unlike a `check_fn=`, these names are resolved
# when a delivery runs, long after the module finishes importing. One trailer
# for all of them — the notifier names a single kube-agents module.
TRAILER = (
    "\n\n# kube-agents patch: see gateway/kanban_notifier.py\n"
    "from gateway.kanban_notifier import (  # noqa: E402\n"
    "    clip_handoff as _clip_handoff,\n"
    "    completion_text as _kanban_completion_text,\n"
    "    handoff_with_result as _kanban_handoff_with_result,\n"
    "    note_suppressed_completion as _kanban_note_suppressed,\n"
    "    store_incident_report as _kanban_store_incident,\n"
    "    tell_unexplained as _kanban_tell_unexplained,\n"
    "    wake_kinds_for as _wake_kinds_for,\n"
    ")\n"
)

#: Text that only exists after a successful run. All five edited anchors
#: are destroyed by their own replacement (the platform_str pin is not, and
#: passes a re-run), so a re-run would already fail on
#: "found 0" — but that message blames upstream drift for what is actually a
#: duplicated build step, and before the old delivery applier grew this guard a
#: second pass exited 0 and left a second hook call and a second trailer import
#: behind.
#:
#: The wake sentinel is the helper's argument line: the trailer's
#: ``wake_kinds_for as _wake_kinds_for`` import does not carry the arguments,
#: and ``_wake_kinds_for(`` alone would also match that import's alias.
SENTINELS = (
    "handoff = _kanban_handoff_with_result(handoff, n.task)",
    'self.d["events"], adapter=self.adapter, passive_delivered=self.send_passive',
    "_kanban_note_suppressed(",
    "_kanban_store_incident(",
    COMPLETION_CALL,
    "_kanban_tell_unexplained(self.runner",
)


def _bindings(body: list[ast.stmt], name: str) -> list[ast.AST]:
    """Every statement or target in ``body`` that may bind ``name`` in its scope.

    Branches count, since any of them may run, and so do a tuple target, an
    import, a loop, ``with`` or ``except`` name and a walrus; a nested scope's
    body does not, and neither does a bare annotation, which binds nothing.
    """
    found, pending = [], list(body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                found.append(node)
            continue
        if isinstance(node, ast.Lambda) or (isinstance(node, ast.AnnAssign) and node.value is None):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id == name:
            found.append(node)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            found.extend(a for a in node.names if (a.asname or a.name).split(".")[0] == name)
        elif isinstance(node, ast.ExceptHandler) and node.name == name:
            found.append(node)
        pending.extend(ast.iter_child_nodes(node))
    return found


def _exits(fn: ast.FunctionDef) -> list[ast.stmt]:
    """The ``return`` and ``raise`` statements of ``fn`` itself, not of a def nested in it."""
    found, pending = [], list(fn.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.Return, ast.Raise)):
            found.append(node)
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            pending.extend(ast.iter_child_nodes(node))
    return found


def expect_platform_binding(patch: patchlib.Patch) -> None:
    """Assert the one ``PLATFORM_BINDING`` line is a statement of ``_KanbanNotification.__init__``.

    ``substitute`` counts the line anywhere, and as a substring, so a binding
    moved verbatim into another scope, or nested deeper inside ``__init__``
    under an ``if``, a ``try`` or a closure, would pass it and leave some
    instances without ``platform_str``. Only a statement directly in the
    constructor's body, at the pinned indentation, runs on every construction.
    The statement found must also be the binding itself, so the text sitting in
    a comment or a string on another assignment's line does not pass. Python
    keeps the last binding, so the class and the constructor must each be the
    only binding of their name in their scope, in any branch or form
    (``_bindings``), and no ``return`` or ``raise`` may come before the binding.
    """
    offset = patch.source.index(PLATFORM_BINDING)
    lineno = patch.source.count("\n", 0, offset) + 1
    col = len(PLATFORM_BINDING) - len(PLATFORM_BINDING.lstrip(" "))
    classes = _bindings(patch._tree().body, PLATFORM_CLASS)
    methods = _bindings(classes[0].body, PLATFORM_METHOD) if len(classes) == 1 and isinstance(classes[0], ast.ClassDef) else []
    if len(methods) == 1 and isinstance(methods[0], ast.FunctionDef) and any(
        isinstance(stmt, ast.Assign) and stmt.lineno == lineno and stmt.col_offset == col
        and ast.get_source_segment(patch.source, stmt) == PLATFORM_BINDING.strip()
        for stmt in methods[0].body
    ) and not any(node.lineno < lineno for node in _exits(methods[0])):
        return
    raise patch._fail(
        f"the platform_str binding at line {lineno} is no longer a statement of "
        f"{PLATFORM_CLASS}.{PLATFORM_METHOD}'s own body, so the completion call's "
        f"n.platform_str is not set on every construction. {patch.note}"
    )


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix="kanban_notifier")
    patch.refuse_if_patched(*SENTINELS)
    patch.substitute(PLATFORM_BINDING, PLATFORM_BINDING, label="platform_str binding")
    expect_platform_binding(patch)
    for label, anchor, patched in EDITS:
        patch.substitute(anchor, patched, label=label)
    patch.append(TRAILER)
    patch.commit(f"{len(EDITS)} anchors")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
