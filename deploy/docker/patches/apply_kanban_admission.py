#!/usr/bin/env python3
"""Wire ``hermes_cli/kanban_admission.py`` into the kanban dispatcher.

Four edits (five anchors) across two files plus one import trailer, applied
after ``apply_kanban_scheduling.py`` and before ``apply_kanban_fanout_reaper.py``
in the Dockerfile; every anchor is upstream v2026.9.14 text that both of those
leave in place. Why the change is needed is in the module docstring of
``kanban_admission.py``.

``hermes_cli/kanban_db_dispatch.py``

    1. ``_tick_spawn_budget`` — the one line that reads the memory pressure,
       ``pressure = _memory_pressure_level()``, becomes a call into the
       module, which classifies against the pod's budget, fails closed, and
       records on the result whether the tick is capacity-limited. Upstream's
       lines after it (critical spawns nothing, elevated at most one) are
       unchanged and keep acting on the level.
    2. ``_run_reclaim_phase`` — after ``enforce_max_runtime``, the worker RSS
       cap: a running worker over it is stopped and its card blocked with kind
       ``capacity``; the ids join ``result.auto_blocked``. The anchor line is
       kept, because ``apply_kanban_fanout_reaper`` anchors on the same line
       later in the build and inserts after it too.
    3. ``_dispatch_once_locked``, two anchors — the ``if not may_spawn:``
       early return (a full slot cap or critical memory, so the tick is
       capacity-limited by construction) and the final ``return result``
       (limited when memory, the slot budget or the sibling reservation held
       a ready row back): the ready cards past the queue bound are refused.
       The early-return insert lands ahead of the kanban-scheduling patch's
       ``queued`` notice so a refused card is not told it is queued first.

``hermes_cli/kanban_db.py``

    4. ``VALID_BLOCK_KINDS`` — gains ``"capacity"``, the kind ``block_task``
       accepts for the three refusals above.

Idempotent: a tree carrying every marker is left alone with a note; a tree
carrying some of them is refused, because that is what a failed layer or a
hand edit leaves. Both files are compiled before either is written.

Usage::

    python3 apply_kanban_admission.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kanban_admission as _admission  # noqa: E402
import patchlib  # noqa: E402

PREFIX = "kanban-admission"

# Where the image installs Hermes, when no root is given.
DEFAULT_HERMES_ROOT = "/opt/hermes"

# The origin module, which holds the block kinds.
DB_RELATIVE = "hermes_cli/kanban_db.py"

# The dispatcher module upstream split out at v2026.9: the tick.
DISPATCH_RELATIVE = "hermes_cli/kanban_db_dispatch.py"

# The name the trailer binds the module to inside the dispatcher.
MODULE_ALIAS = "_kanban_admission"

# --- 1. _tick_spawn_budget: classify against the budget, fail closed --------
PRESSURE_ANCHOR = "    pressure = _memory_pressure_level()\n"

PRESSURE_PATCHED = (
    "    # kube-agents patch: classify pressure against the pod's memory budget\n"
    "    # from the container's own counters, fail closed when none can be read,\n"
    "    # and record on the result whether this tick is capacity-limited.\n"
    "    # See hermes_cli/kanban_admission.py.\n"
    f"    pressure = {MODULE_ALIAS}.pressure_level_for_tick(result)\n"
)

# --- 2. _run_reclaim_phase: the worker RSS cap -------------------------------
#
# The reclaim phase's runtime enforcement, which only it has. The line stays:
# apply_kanban_fanout_reaper anchors on it later in the build.
RSS_CAP_ANCHOR = "    result.timed_out = enforce_max_runtime(conn)\n"

RSS_CAP_PATCHED = RSS_CAP_ANCHOR + (
    "    # kube-agents patch: stop a worker whose process tree is over its\n"
    "    # resident-size cap and block its card with a capacity reason.\n"
    "    # See hermes_cli/kanban_admission.py.\n"
    f"    result.auto_blocked.extend({MODULE_ALIAS}.enforce_worker_rss_caps(conn))\n"
)

# --- 3a. _dispatch_once_locked: the early return --------------------------
#
# The closing parenthesis of the ``_tick_spawn_budget`` call and the test of
# its verdict. The insert goes first inside the branch, ahead of whatever the
# kanban-scheduling patch put there.
EARLY_RETURN_ANCHOR = "    )\n    if not may_spawn:\n"

EARLY_RETURN_PATCHED = EARLY_RETURN_ANCHOR + (
    "        # kube-agents patch: the tick spawns nothing (slot cap or memory),\n"
    "        # so a ready card past the queue bound is refused with capacity.\n"
    "        # See hermes_cli/kanban_admission.py.\n"
    f"        {MODULE_ALIAS}.refuse_overdue_ready(\n"
    "            conn, result, limited_by_capacity=True, dry_run=dry_run,\n"
    "        )\n"
)

# --- 3b. _dispatch_once_locked: the final return --------------------------
#
# ``return result`` is everywhere in this file; the two blank lines and the
# next def pin this one to the end of the tick.
FINAL_RETURN_ANCHOR = "    return result\n\n\ndef _positive_int("

FINAL_RETURN_PATCHED = (
    "    # kube-agents patch: a ready card past the queue bound that this tick\n"
    "    # left waiting for memory or a slot is refused with capacity.\n"
    "    # See hermes_cli/kanban_admission.py.\n"
    f"    {MODULE_ALIAS}.refuse_overdue_ready(\n"
    "        conn, result,\n"
    f"        limited_by_capacity={MODULE_ALIAS}.tick_was_limited(\n"
    "            result, spawn_budget=spawn_budget, spawned=spawned,\n"
    "        ),\n"
    "        dry_run=dry_run,\n"
    "    )\n"
) + FINAL_RETURN_ANCHOR

# --- 4. VALID_BLOCK_KINDS: the capacity kind --------------------------------
KINDS_ANCHOR = 'VALID_BLOCK_KINDS = {"dependency", "needs_input", "capability", "transient"}\n'

KINDS_PATCHED = (
    f'# kube-agents patch: "{_admission.CAPACITY_KIND}" is the dispatcher\'s refusal: memory\n'
    "# pressure, a worker over its resident-size cap, or a wait past the queue\n"
    "# bound. See hermes_cli/kanban_admission.py.\n"
    'VALID_BLOCK_KINDS = {"dependency", "needs_input", "capability", "transient", '
    f'"{_admission.CAPACITY_KIND}"}}\n'
)

# Appended rather than inserted: the name is resolved when the tick runs,
# long after the module finishes importing, as the sibling trailers do.
TRAILER = (
    "\n\n# kube-agents patch: see hermes_cli/kanban_admission.py\n"
    f"from hermes_cli import kanban_admission as {MODULE_ALIAS}  # noqa: E402\n"
)

# ``(file, label, anchor, replacement)``; the tests iterate this to prove each
# anchor is load-bearing in the file it names.
EDITS = (
    (DISPATCH_RELATIVE, "_tick_spawn_budget pressure", PRESSURE_ANCHOR, PRESSURE_PATCHED),
    (DISPATCH_RELATIVE, "_run_reclaim_phase RSS cap", RSS_CAP_ANCHOR, RSS_CAP_PATCHED),
    (DISPATCH_RELATIVE, "_dispatch_once_locked early return", EARLY_RETURN_ANCHOR, EARLY_RETURN_PATCHED),
    (DISPATCH_RELATIVE, "_dispatch_once_locked final return", FINAL_RETURN_ANCHOR, FINAL_RETURN_PATCHED),
    (DB_RELATIVE, "VALID_BLOCK_KINDS capacity", KINDS_ANCHOR, KINDS_PATCHED),
)

TRAILERS = ((DISPATCH_RELATIVE, TRAILER),)

# The text the Dockerfile greps for after this applier runs. Named here so the
# grep strings have one home; test_kanban_admission asserts the Dockerfile
# still greps for each.
PRESSURE_BUILD_MARKER = f"pressure = {MODULE_ALIAS}.pressure_level_for_tick(result)"
RSS_CAP_BUILD_MARKER = f"result.auto_blocked.extend({MODULE_ALIAS}.enforce_worker_rss_caps(conn))"
REFUSE_BUILD_MARKER = f"{MODULE_ALIAS}.refuse_overdue_ready("
TRAILER_BUILD_MARKER = f"from hermes_cli import kanban_admission as {MODULE_ALIAS}"
KINDS_BUILD_MARKER = f'"transient", "{_admission.CAPACITY_KIND}"}}'
BUILD_MARKERS = (
    PRESSURE_BUILD_MARKER,
    RSS_CAP_BUILD_MARKER,
    REFUSE_BUILD_MARKER,
    TRAILER_BUILD_MARKER,
    KINDS_BUILD_MARKER,
)

# Written by a successful apply and by nothing else, one set per file. Three
# of the five anchors survive their own replacement (edits 2 and 3 insert
# around theirs), so counting alone cannot tell a fresh tree from a patched
# one; these can.
DISPATCH_MARKERS = (
    PRESSURE_BUILD_MARKER,
    RSS_CAP_BUILD_MARKER,
    REFUSE_BUILD_MARKER,
    TRAILER_BUILD_MARKER,
)
DB_MARKERS = (KINDS_BUILD_MARKER,)
MARKERS = ((DISPATCH_RELATIVE, DISPATCH_MARKERS), (DB_RELATIVE, DB_MARKERS))

# What a second run prints instead of editing.
ALREADY_APPLIED_NOTE = f"{PREFIX} patch: already applied, nothing to do"


def _present(patch: patchlib.Patch, markers: tuple) -> list:
    """Which of ``markers`` the file already carries."""
    return [marker for marker in markers if marker in patch.source]


def apply(root: Path) -> bool:
    """Apply under ``root``; True when it edited, False when it found the patch in place.

    Raises ``SystemExit`` on a half-patched tree or a moved anchor. Nothing is
    written until every anchor in both files has matched exactly once and
    both results compile.
    """
    patches = {
        relative: patchlib.Patch(root, relative, prefix=PREFIX)
        for relative in (DISPATCH_RELATIVE, DB_RELATIVE)
    }
    present = {relative: _present(patches[relative], markers) for relative, markers in MARKERS}
    if all(len(present[relative]) == len(markers) for relative, markers in MARKERS):
        print(ALREADY_APPLIED_NOTE)
        return False
    if any(present.values()):
        found = "; ".join(f"{relative}: {found!r}" for relative, found in present.items() if found)
        raise SystemExit(
            f"{PREFIX} patch: the tree is half-patched ({found}). Restore both files "
            "from upstream and apply again."
        )
    for relative, label, anchor, patched in EDITS:
        patches[relative].substitute(anchor, patched, label=label)
    for relative, trailer in TRAILERS:
        patches[relative].append(trailer)
    # ``Patch.commit`` compiles and writes in one step, per file. Compile both
    # here first so the second file failing cannot leave the first on disk.
    for patch in patches.values():
        try:
            compile(patch.source, patch.relative, "exec")
        except SyntaxError as exc:
            raise SystemExit(
                f"{PREFIX} patch: {patch.relative} no longer parses after patching: {exc}"
            )
    edits_per_file = {relative: 0 for relative in patches}
    for relative, _label, _anchor, _patched in EDITS:
        edits_per_file[relative] += 1
    for relative, patch in patches.items():
        patch.commit(f"{edits_per_file[relative]} anchors")
    return True


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_HERMES_ROOT))
