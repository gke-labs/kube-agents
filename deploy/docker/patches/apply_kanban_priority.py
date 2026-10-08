#!/usr/bin/env python3
"""Wire hermes_cli/kanban_priority.py into the create tool, the dispatcher
watcher and the notifier.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``, in the latency
block after ``apply_kanban_children_settled.py``. The dispatcher half of the
feature (the reserved slot, the saturation record, the queued events) is
edits 7-10 of ``apply_kanban_scheduling.py``, because that applier already owns
``hermes_cli/kanban_db_dispatch.py`` and the file is the unit of risk there.
This applier carries the other three files, each held in memory and compiled
before any is written:

``tools/kanban_tools.py`` (two anchors and a trailer)

    1. The ``priority=`` keyword of the ``kb.create_task(...)`` call in
       ``_handle_create``: the requested priority is passed through
       ``stamp_priority`` with the creating worker's ``self_task``, a local the
       handler computed a few lines up. Not the handler's ``session_id`` local:
       that one prefers ``args["session_id"]``, which the model controls, so
       ``stamp_priority`` reads the turn's session from the runtime itself.
       Located inside ``_handle_create`` and refused anywhere else, the way
       ``apply_kanban_report_format.py`` pins its ``body=`` keyword to the same
       call. It shares no text with that edit.
    2. The handler's success return: ``queue_fields`` adds ``queued`` and
       ``queue_note`` when the new card will wait for a slot. Precedent:
       ``apply_kanban_comment_status.py`` grows ``kanban_comment``'s return
       the same way. ``apply_kanban_auto_subscribe.py`` and
       ``apply_kanban_children_settled.py`` anchor on the ``landed = ...`` line
       above it and keep that line, so the two do not touch.

``gateway/kanban_watchers.py`` (one anchor and a trailer)

    3. The dispatcher loop's stuck counter. A tick in which every board was
       capped or holding its reserved slot is saturation, logged as such by
       ``saturation_tick``, and does not count toward upstream's "stuck"
       warning. The anchor sits between ``apply_kanban_wake_nudge.py``'s two
       dispatcher anchors and overlaps neither; that applier runs first.

``gateway/kanban_watchers_notifier.py`` (one locator and a trailer)

    4. ``TERMINAL_KINDS`` gains ``"queued"``, the way
       ``apply_kanban_progress_lines.py`` adds ``"heartbeat"``: located by
       name, asserted to still be the terminal-kind filter, and widened in
       place. Not added to ``_WAKE_KINDS``, so a queued notice costs no LLM
       turn. The trailer registers ``format_queued`` in ``_EVENT_FORMATTERS``
       after the module has built the table; ``format_event`` reads the table
       at call time. ``kanban_progress_lines`` lists ``queued`` as a rolling
       kind, so the notice is the first line of the card's progress message.

Why the change is needed is documented in the module docstring of
``deploy/docker/patches/kanban_priority.py``. Usage::

    python3 apply_kanban_priority.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import ast
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patchlib  # noqa: E402

PREFIX = "kanban_priority"

TOOLS_RELATIVE = "tools/kanban_tools.py"
WATCHERS_RELATIVE = "gateway/kanban_watchers.py"
NOTIFIER_RELATIVE = "gateway/kanban_watchers_notifier.py"

# --- 1. stamp the priority at create time ------------------------------------
#
# Mid-call keyword, like apply_kanban_report_format's ``body=``: a trailing
# comment there would swallow the argument after it, so none rides along. The
# trailer names the patch. Consumes the anchor, so a second run also fails the
# count check.
HANDLER = "_handle_create"
PRIORITY_ANCHOR = 'priority=_opt_int(args.get("priority"), 0),'
PRIORITY_PATCHED = (
    'priority=_kanban_stamp_priority(_opt_int(args.get("priority"), 0), self_task),'
)

# --- 2. say when the new card is queued --------------------------------------
RETURN_ANCHOR = (
    "        return _ok(task_id=new_tid, **landed, "
    "subscribed=_maybe_auto_subscribe(conn, new_tid))\n"
)
RETURN_PATCHED = (
    "        # kube-agents patch: say when the new card waits for a worker slot,\n"
    "        # so the creating turn can tell the user it is queued.\n"
    "        # See hermes_cli/kanban_priority.py.\n"
    "        return _ok(\n"
    "            task_id=new_tid, **landed,\n"
    "            subscribed=_maybe_auto_subscribe(conn, new_tid),\n"
    '            **_kanban_queue_fields(conn, new_tid, args.get("board")),\n'
    "        )\n"
)

TOOLS_TRAILER = (
    "\n\n# kube-agents patch: see hermes_cli/kanban_priority.py\n"
    "from hermes_cli.kanban_priority import (  # noqa: E402\n"
    "    queue_fields as _kanban_queue_fields,\n"
    "    stamp_priority as _kanban_stamp_priority,\n"
    ")\n"
)

# --- 3. saturation is not "stuck" ---------------------------------------------
#
# ``results``, ``any_spawned`` and ``ready_pending`` are the three locals the
# loop computed on the lines above; ``logger`` and ``_HEALTH_WINDOW`` are the
# module's own. The upstream line is kept as the else branch, so the stuck
# warning below is untouched for every tick that is not saturation.
STUCK_ANCHOR = (
    "                    ready_pending = await _to_thread_process_service(dispatcher.ready_nonempty)\n"
    "                    bad_ticks = bad_ticks + 1 if ready_pending and not any_spawned else 0\n"
)
STUCK_PATCHED = (
    "                    ready_pending = await _to_thread_process_service(dispatcher.ready_nonempty)\n"
    "                    # kube-agents patch: every slot busy (or the one held for\n"
    "                    # user cards) is saturation, logged as such, and not a\n"
    "                    # broken profile. See hermes_cli/kanban_priority.py.\n"
    "                    if _kanban_saturation_tick(\n"
    "                        logger, results, ready_pending, any_spawned, _HEALTH_WINDOW\n"
    "                    ):\n"
    "                        bad_ticks = 0\n"
    "                    else:\n"
    "                        bad_ticks = bad_ticks + 1 if ready_pending and not any_spawned else 0\n"
)

WATCHERS_TRAILER = (
    "\n\n# kube-agents patch: see hermes_cli/kanban_priority.py\n"
    "from hermes_cli.kanban_priority import (  # noqa: E402\n"
    "    saturation_tick as _kanban_saturation_tick,\n"
    ")\n"
)

# --- 4. deliver the queued notice ---------------------------------------------
KINDS_COMMENT = (
    '# kube-agents patch: "queued" is NOT terminal either. The dispatcher\n'
    "# writes one per wait for a user card left without a worker slot; it\n"
    "# is claimed here so the notice reaches the card's thread, and it is\n"
    "# absent from _WAKE_KINDS so it costs no LLM turn.\n"
    "# See hermes_cli/kanban_priority.py.\n"
)

#: Kinds the filter must still carry for it to be the one this patch means.
KINDS_EXPECTED = ("completed", "blocked", "gave_up", "crashed", "timed_out")

QUEUED_WIDENING = ' + ("queued",)'

NOTIFIER_TRAILER = (
    "\n\n# kube-agents patch: see hermes_cli/kanban_priority.py\n"
    "from hermes_cli.kanban_priority import (  # noqa: E402\n"
    "    format_queued as _kanban_format_queued,\n"
    ")\n"
    '_EVENT_FORMATTERS["queued"] = _kanban_format_queued\n'
)

# One marker per file, text that exists only after a successful run. The
# notifier's edit widens a tuple in place and the trailers are appended, so
# counting alone cannot tell a fresh file from a patched one.
SENTINELS = {
    TOOLS_RELATIVE: "_kanban_stamp_priority(",
    WATCHERS_RELATIVE: "_kanban_saturation_tick(",
    NOTIFIER_RELATIVE: KINDS_COMMENT.splitlines()[0],
}


def _tuple_literals(node: ast.AST) -> list:
    """The tuple literals a ``(...) + (...)`` chain is built from, or [] if it is anything else."""
    if isinstance(node, ast.Tuple):
        return [node]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _tuple_literals(node.left), _tuple_literals(node.right)
        return left + right if left and right else []
    return []


def _widen_kinds(notifier: patchlib.Patch) -> None:
    """Add ``"queued"`` to ``TERMINAL_KINDS``.

    ``Assignment.expect_contains`` wants a bare tuple literal, and by the time
    this runs ``apply_kanban_progress_lines.py`` has already made the value
    ``(...) + ("heartbeat",)``. So the same assertion is made over the chain of
    tuple literals instead: the filter must still be built of literals only,
    and still hold the kinds this patch reasons about.
    """
    kinds = notifier.find_assign("TERMINAL_KINDS", label="terminal-kind filter")
    parts = _tuple_literals(kinds.node.value)
    if not parts:
        raise notifier._fail(
            "the terminal-kind filter is no longer built of tuple literals: "
            f"{kinds.value_text}. {notifier.note}"
        )
    present = {
        element.value
        for part in parts
        for element in part.elts
        if isinstance(element, ast.Constant)
    }
    missing = [kind for kind in KINDS_EXPECTED if kind not in present]
    if missing:
        raise notifier._fail(
            f"the terminal-kind filter no longer holds {missing}. {notifier.note}"
        )
    notifier.splice(kinds.value_start, kinds.value_end, kinds.value_text + QUEUED_WIDENING)
    notifier.insert(kinds.line_start, textwrap.indent(KINDS_COMMENT, kinds.indent))


def apply(root: Path) -> None:
    """Apply every edit under ``root``, or raise SystemExit with the reason.

    Nothing is written until every anchor in all three files has matched and
    all three results compile.
    """
    patches = {
        relative: patchlib.Patch(root, relative, prefix=PREFIX)
        for relative in (TOOLS_RELATIVE, WATCHERS_RELATIVE, NOTIFIER_RELATIVE)
    }
    for relative, sentinel in SENTINELS.items():
        patches[relative].refuse_if_patched(sentinel)

    tools = patches[TOOLS_RELATIVE]
    handler = tools.find_def(HANDLER, label="create handler")
    inside = tools.source[handler.start : handler.end].count(PRIORITY_ANCHOR)
    if inside != 1:
        raise SystemExit(
            f"{PREFIX} patch: {TOOLS_RELATIVE}: expected the priority anchor "
            f"{PRIORITY_ANCHOR!r} once inside {HANDLER}(), found {inside} "
            f"({tools.source.count(PRIORITY_ANCHOR)} in the whole file). {tools.note}"
        )
    tools.substitute(PRIORITY_ANCHOR, PRIORITY_PATCHED, label="create priority stamp")
    # The span ends where the def's last statement does, before its newline.
    handler = tools.find_def(HANDLER, label="create handler")
    inside = tools.source[handler.start : handler.end].count(RETURN_ANCHOR.rstrip("\n"))
    if inside != 1:
        raise SystemExit(
            f"{PREFIX} patch: {TOOLS_RELATIVE}: expected the create return "
            f"anchor once inside {HANDLER}(), found {inside}. {tools.note}"
        )
    tools.substitute(RETURN_ANCHOR, RETURN_PATCHED, label="create queue note")
    tools.append(TOOLS_TRAILER)

    watchers = patches[WATCHERS_RELATIVE]
    watchers.substitute(STUCK_ANCHOR, STUCK_PATCHED, label="dispatcher stuck counter")
    watchers.append(WATCHERS_TRAILER)

    notifier = patches[NOTIFIER_RELATIVE]
    _widen_kinds(notifier)
    notifier.append(NOTIFIER_TRAILER)

    for patch in patches.values():
        try:
            compile(patch.source, patch.relative, "exec")
        except SyntaxError as exc:
            raise SystemExit(
                f"{PREFIX} patch: {patch.relative} no longer parses after patching: {exc}"
            )
    tools.commit("2 anchors + trailer")
    watchers.commit("1 anchor + trailer")
    notifier.commit("1 locator + trailer")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
