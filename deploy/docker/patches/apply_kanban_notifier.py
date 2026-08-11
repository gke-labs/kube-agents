#!/usr/bin/env python3
"""Wire gateway/kanban_notifier.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. Two anchored edits
in ``gateway/kanban_watchers.py`` plus one import trailer, replacing what used
to be three appliers (a Dockerfile one-liner for the clip, one for the wake
set, one for the result delivery) with four anchors between them.

The merge is possible because the clip and the delivery are adjacent. Upstream
builds the status line in an ``if``/``elif`` and then immediately builds the
message from it::

    if payload_summary:
        lines = payload_summary.strip().splitlines()
        h = lines[0][:200] if lines else payload_summary[:200]
        handoff = f"\\n{h}"
    elif task and task.result:
        ...
    msg = (
        f"✔ {board_tag}{tag}Kanban {sub['task_id']} done"
        f" — {title}{handoff}"
    )

The old delivery applier anchored on that ``msg = (`` block purely to have
somewhere to insert the hook *after* the clip lines, which a different patch
owned. Owning both, this applier can end its anchor at the last ``handoff =``
of the ``elif`` and append the hook there — so the ``msg`` block stops being an
anchor at all. Four anchors become two, and the ordering hazard the old
appliers documented ("it sits after the handoff lines... so this patch composes
with those instead of fighting them for the same lines, whatever order the
build applies them in") stops existing: one edit produces both.

The output is what the three old patches produced plus one added call —
:data:`MARKER_CALL`, which records a terminal event whose wake the narrowing
suppressed (section 4 of ``deploy/docker/patches/kanban_notifier.py``).
Everything else is byte-identical, comment text aside, and
``test_kanban_notifier.py``'s ``LegacyEquivalenceTest`` still asserts that
against a replay of the old pipeline: it subtracts :data:`MARKER_CALL` and
requires the remainder to match exactly, so the merge stays a refactor and the
one new behaviour stays visible as one block. Upstream's now-unused
``lines = …`` assignments are kept rather than tidied away: they are upstream's
code, not ours, and a patch that also edits the thing being patched cannot
claim to be behaviour-preserving.

Not merged in, deliberately:

* ``gateway/kanban_handoff_clip.py`` stays a module of its own — it is a
  dependency-free text utility that ``tools/cron_run_scope.py`` also imports,
  and it needs to exist earlier in the build than this applier runs. It carries
  no anchor into upstream source, so it is not part of this patch's coupling to
  ``kanban_watchers.py``; only its *wiring* is, and that is anchor 1 here.
* ``hermes_cli/kanban_wake_nudge.py`` also edits this file, at the watcher
  loops' construction and sleep sites (roughly lines 191 and 1150). Those
  anchors are disjoint from both of ours and neither applier disturbs the
  other's text; wake_nudge runs later in the Dockerfile and appends its own
  trailer after ours.

Why the changes are needed is documented in the module docstrings of
``deploy/docker/patches/kanban_notifier.py`` and
``deploy/docker/patches/kanban_handoff_clip.py``. Usage::

    python3 apply_kanban_notifier.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

RELATIVE = "gateway/kanban_watchers.py"

#: Nesting depth of the ``completed`` branch's body.
HANDOFF_INDENT = " " * 28
#: Nesting depth of the post-delivery wake block, two levels shallower.
WAKE_INDENT = " " * 24

# --- Anchor 1: the completion handoff ----------------------------------------
#
# Covers both of upstream's hard slices (the 200-character one that severed a
# published ledger URL down to ".../is" on 2026-08-03, and the 160-character one
# in the no-summary branch) and ends where the hook has to go.

HANDOFF_ANCHOR = (
    f"{HANDOFF_INDENT}if payload_summary:\n"
    f"{HANDOFF_INDENT}    lines = payload_summary.strip().splitlines()\n"
    f"{HANDOFF_INDENT}    h = lines[0][:200] if lines else payload_summary[:200]\n"
    f'{HANDOFF_INDENT}    handoff = f"\\n{{h}}"\n'
    f"{HANDOFF_INDENT}elif task and task.result:\n"
    f"{HANDOFF_INDENT}    lines = task.result.strip().splitlines()\n"
    f"{HANDOFF_INDENT}    r = lines[0][:160] if lines else task.result[:160]\n"
    f'{HANDOFF_INDENT}    handoff = f"\\n{{r}}"\n'
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
    f"{HANDOFF_INDENT}    lines = payload_summary.strip().splitlines()\n"
    f"{HANDOFF_INDENT}    h = _clip_handoff(payload_summary)\n"
    f'{HANDOFF_INDENT}    handoff = f"\\n{{h}}"\n'
    f"{HANDOFF_INDENT}elif task and task.result:\n"
    f"{HANDOFF_INDENT}    lines = task.result.strip().splitlines()\n"
    f"{HANDOFF_INDENT}    r = _clip_handoff(task.result)\n"
    f'{HANDOFF_INDENT}    handoff = f"\\n{{r}}"\n'
    f"{HANDOFF_INDENT}handoff = _kanban_handoff_with_result(handoff, task)\n"
)

# --- Anchor 2: the wake set ---------------------------------------------------

WAKE_ANCHOR = (
    f'{WAKE_INDENT}_WAKE_KINDS = ("completed", "gave_up", "crashed", "timed_out", "blocked")\n'
    f'{WAKE_INDENT}_wake_kinds = {{ev.kind for ev in d["events"] if ev.kind in _WAKE_KINDS}}\n'
)

# `adapter=` is load-bearing, not decoration: without it the narrowing also
# applies to adapters whose send() the notifier deliberately skips, where the
# wake IS the delivery. See wake_kinds_for's docstring.
#
# The marker call is the one line here that is not upstream-equivalent. It has
# to sit at this exact point and no earlier: control only reaches the `else`
# clause this block lives in when every text ping for the delivery has been
# sent, which is what makes "its result was already delivered to this
# conversation" — the claim the note makes to the creator — true. It reads
# `_wake_kinds` to work out what the narrowing suppressed, so it must also come
# after the line above. `self`, `task`, `sub` and `board_slug` are all already
# in scope (the line above this anchor computes `task_terminal` from `task`,
# and `self._kanban_unsub(sub, board_slug)` is called further down the same
# block). See section 4 of gateway/kanban_notifier.py.
WAKE_PATCHED = (
    f"{WAKE_INDENT}# kube-agents patch: see gateway/kanban_notifier.py\n"
    f'{WAKE_INDENT}_wake_kinds = _wake_kinds_for(d["events"], adapter=adapter)\n'
    f"{WAKE_INDENT}_kanban_note_suppressed(\n"
    f'{WAKE_INDENT}    self, d["events"], _wake_kinds, task, sub, board_slug,\n'
    f"{WAKE_INDENT})\n"
)

#: The marker call, as emitted — the one block of :data:`WAKE_PATCHED` that the
#: three superseded appliers had no counterpart for. Named here so
#: ``test_kanban_notifier.py`` can subtract it and keep checking the rest of
#: this applier's output against the legacy pipeline byte for byte.
MARKER_CALL = "".join(WAKE_PATCHED.splitlines(keepends=True)[2:])

EDITS = (
    ("completion handoff", HANDOFF_ANCHOR, HANDOFF_PATCHED),
    ("wake set", WAKE_ANCHOR, WAKE_PATCHED),
)

# Appended rather than inserted: unlike a `check_fn=`, these names are resolved
# when the notifier loop runs, long after the module finishes importing. One
# trailer for all three, which is the other half of the merge — the notifier now
# names a single kube-agents module instead of three.
TRAILER = (
    "\n\n# kube-agents patch: see gateway/kanban_notifier.py\n"
    "from gateway.kanban_notifier import (  # noqa: E402\n"
    "    clip_handoff as _clip_handoff,\n"
    "    handoff_with_result as _kanban_handoff_with_result,\n"
    "    note_suppressed_completion as _kanban_note_suppressed,\n"
    "    wake_kinds_for as _wake_kinds_for,\n"
    ")\n"
)

#: Text that only exists after a successful run. Both anchors are destroyed by
#: their own replacement, so a re-run would already fail on "found 0" — but that
#: message blames upstream drift for what is actually a duplicated build step,
#: and before the old delivery applier grew this guard a second pass exited 0
#: and left a second hook call and a second trailer import behind.
SENTINELS = (
    "handoff = _kanban_handoff_with_result(handoff, task)",
    '_wake_kinds = _wake_kinds_for(d["events"], adapter=adapter)',
    "_kanban_note_suppressed(",
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix="kanban_notifier")
    patch.refuse_if_patched(*SENTINELS)
    for label, anchor, patched in EDITS:
        patch.substitute(anchor, patched, label=label)
    patch.append(TRAILER)
    patch.commit(f"{len(EDITS)} anchors")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
