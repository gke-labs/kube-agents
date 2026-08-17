#!/usr/bin/env python3
"""Wire gateway/kanban_notify_delivery.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. Three anchored
edits in ``gateway/kanban_watchers.py`` plus one import trailer, turning the
notifier's at-most-once claim into at-least-once delivery.

Why the change is needed — the 2026-08-09 loss of ``t_a18254ca``, the reason
the crash is not the defect, and the single-writer dependency the trade
introduces — is documented in the module docstring of
``deploy/docker/patches/kanban_notify_delivery.py``. This file documents only
where the edits land and why each anchor is shaped the way it is.

Anchor 1 (the claim) and anchor 2 (the advance) are the two halves of one
change and only make sense together: 1 stops the cursor moving before delivery,
2 makes the one remaining cursor write survivable and dedupe-aware. Anchor 3
neuters the now-vestigial rewind.

**Anchor 2 is why this is three anchors and not two.** The obvious anchor for
the success-path advance —::

    await asyncio.to_thread(
        self._kanban_advance, sub, d["cursor"], board_slug,
    )

— occurs **twice at identical 24-space indentation**: once on the unknown-
platform skip and once on the success path. ``Patch.substitute`` enforces
``expected=1``, so anchoring on it alone is a guaranteed ``SystemExit``, and
raising ``expected`` to 2 would silently patch the skip path as well. The
anchor therefore carries the last line of upstream's preceding comment, which
is unique to the success site. That comment line is load-bearing: if upstream
rewords it the build fails loudly, which is the intended outcome.

Ordering. Must run AFTER ``apply_kanban_wake_nudge.py`` — the anchors were
derived from the fully-patched tree, and although wake_nudge's own anchors (the
watcher-loop construction and the sleep site) are disjoint from all three of
these, deriving against a different tree than the one the build produces is how
an anchor rots undetected. Disjoint from ``apply_kanban_notifier.py`` as well:
that patch owns the handoff block and the wake set, neither of which any anchor
here touches. Both append trailers; appends compose.

Usage::

    python3 apply_kanban_notify_delivery.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

RELATIVE = "gateway/kanban_watchers.py"

#: Nesting depth of the per-subscription body inside ``_collect``.
CLAIM_INDENT = " " * 36
#: Nesting depth of the post-delivery block in the ``for``/``else`` clause.
ADVANCE_INDENT = " " * 24
#: Nesting depth of a method body on the watcher class.
METHOD_INDENT = " " * 8

# --- Anchor 1: the claim ------------------------------------------------------
#
# Upstream commits `last_event_id` here, ~130 lines of control flow before
# anything is sent. Replaced by a read that writes nothing.

CLAIM_ANCHOR = (
    f"{CLAIM_INDENT}old_cursor, cursor, events = _kb.claim_unseen_events_for_sub(\n"
    f"{CLAIM_INDENT}    conn,\n"
    f'{CLAIM_INDENT}    task_id=sub["task_id"],\n'
    f'{CLAIM_INDENT}    platform=sub["platform"],\n'
    f'{CLAIM_INDENT}    chat_id=sub["chat_id"],\n'
    f'{CLAIM_INDENT}    thread_id=sub.get("thread_id") or "",\n'
    f"{CLAIM_INDENT}    kinds=TERMINAL_KINDS,\n"
    f"{CLAIM_INDENT})\n"
)

# `watcher=self` carries the in-process high-water map, which is what bounds a
# permanently-failing cursor write to one duplicate per process lifetime. The
# three-tuple shape is preserved deliberately: `unseen_events_for_sub` returns
# two values, and unpacking two into three raises inside the per-subscription
# `except` below, which logs at WARNING and skips that subscription on every
# tick forever — a worse silent failure than the one being fixed.
CLAIM_PATCHED = (
    f"{CLAIM_INDENT}# kube-agents patch: read without claiming. See\n"
    f"{CLAIM_INDENT}# gateway/kanban_notify_delivery.py.\n"
    f"{CLAIM_INDENT}old_cursor, cursor, events = _kanban_read_unclaimed(\n"
    f"{CLAIM_INDENT}    _kb,\n"
    f"{CLAIM_INDENT}    conn,\n"
    f"{CLAIM_INDENT}    sub,\n"
    f"{CLAIM_INDENT}    kinds=TERMINAL_KINDS,\n"
    f"{CLAIM_INDENT}    watcher=self,\n"
    f"{CLAIM_INDENT})\n"
)

# --- Anchor 2: the success-path advance ---------------------------------------
#
# The comment line is part of the anchor because the three lines below it are
# not unique in this file. See the module docstring.

ADVANCE_ANCHOR = (
    f"{ADVANCE_INDENT}# of the same event on subsequent ticks.\n"
    f"{ADVANCE_INDENT}await asyncio.to_thread(\n"
    f'{ADVANCE_INDENT}    self._kanban_advance, sub, d["cursor"], board_slug,\n'
    f"{ADVANCE_INDENT})\n"
)

# The mark happens on the event loop, before the write is attempted, so it is
# recorded whether or not the write lands. The write itself moves behind a
# helper that logs instead of unwinding the tick — upstream's bare
# `self._kanban_advance` let a cursor-write failure abort every remaining
# delivery in the tick, which is the `kanban notifier tick failed: disk I/O
# error` line in the 2026-08-09 log.
ADVANCE_PATCHED = (
    f"{ADVANCE_INDENT}# of the same event on subsequent ticks.\n"
    f"{ADVANCE_INDENT}# kube-agents patch: this is now the ONLY cursor write, and\n"
    f"{ADVANCE_INDENT}# it happens after delivery. See\n"
    f"{ADVANCE_INDENT}# gateway/kanban_notify_delivery.py.\n"
    f'{ADVANCE_INDENT}_kanban_mark_delivered(self, sub, d["cursor"])\n'
    f"{ADVANCE_INDENT}await asyncio.to_thread(\n"
    f"{ADVANCE_INDENT}    _kanban_advance_delivered,\n"
    f'{ADVANCE_INDENT}    self, sub, d["cursor"], board_slug,\n'
    f"{ADVANCE_INDENT})\n"
)

# --- Anchor 3: the rewind -----------------------------------------------------
#
# Nothing is claimed any more, so nothing can be undone. Left as a method rather
# than removed: its three call sites each also perform the `continue`/`break`
# that ends the delivery attempt, and neutering here costs one anchor instead of
# three there.

REWIND_ANCHOR = (
    f'{METHOD_INDENT}"""Sync helper: undo a claimed notification cursor after send failure."""\n'
    f"{METHOD_INDENT}from hermes_cli import kanban_db as _kb\n"
    f"{METHOD_INDENT}conn = _kb.connect(board=board)\n"
    f"{METHOD_INDENT}try:\n"
    f"{METHOD_INDENT}    _kb.rewind_notify_cursor(\n"
    f"{METHOD_INDENT}        conn,\n"
    f'{METHOD_INDENT}        task_id=sub["task_id"],\n'
    f'{METHOD_INDENT}        platform=sub["platform"],\n'
    f'{METHOD_INDENT}        chat_id=sub["chat_id"],\n'
    f'{METHOD_INDENT}        thread_id=sub.get("thread_id") or "",\n'
    f"{METHOD_INDENT}        claimed_cursor=claimed_cursor,\n"
    f"{METHOD_INDENT}        old_cursor=old_cursor,\n"
    f"{METHOD_INDENT}    )\n"
    f"{METHOD_INDENT}finally:\n"
    f"{METHOD_INDENT}    conn.close()\n"
)

REWIND_PATCHED = (
    f'{METHOD_INDENT}"""Sync helper: no-op — nothing is claimed, so nothing can be undone.\n'
    f"\n"
    f"{METHOD_INDENT}kube-agents patch: see gateway/kanban_notify_delivery.py. The\n"
    f"{METHOD_INDENT}notifier no longer advances the cursor before delivering, so a\n"
    f"{METHOD_INDENT}failed send retries by virtue of the cursor never having moved.\n"
    f"{METHOD_INDENT}Undoing a claim that was never made is not merely redundant:\n"
    f"{METHOD_INDENT}``rewind_notify_cursor`` is a compare-and-swap on\n"
    f"{METHOD_INDENT}``last_event_id``, so a concurrent writer that had left the row\n"
    f"{METHOD_INDENT}at exactly the cursor this tick computed would see it dragged\n"
    f"{METHOD_INDENT}backwards, forcing a duplicate.\n"
    f'{METHOD_INDENT}"""\n'
    f"{METHOD_INDENT}logger.debug(\n"
    f'{METHOD_INDENT}    "kanban notifier: rewind for %s is a no-op under "\n'
    f'{METHOD_INDENT}    "at-least-once delivery (claimed=%s old=%s board=%s)",\n'
    f'{METHOD_INDENT}    sub.get("task_id"), claimed_cursor, old_cursor, board,\n'
    f"{METHOD_INDENT})\n"
)

EDITS = (
    ("notifier claim", CLAIM_ANCHOR, CLAIM_PATCHED),
    ("post-delivery advance", ADVANCE_ANCHOR, ADVANCE_PATCHED),
    ("vestigial rewind", REWIND_ANCHOR, REWIND_PATCHED),
)

# Appended rather than inserted: these names are resolved when the notifier loop
# runs, long after the module finishes importing.
TRAILER = (
    "\n\n# kube-agents patch: see gateway/kanban_notify_delivery.py\n"
    "from gateway.kanban_notify_delivery import (  # noqa: E402\n"
    "    advance_after_delivery as _kanban_advance_delivered,\n"
    "    mark_delivered as _kanban_mark_delivered,\n"
    "    read_unclaimed as _kanban_read_unclaimed,\n"
    ")\n"
)

#: Text that only exists after a successful run. All three anchors are consumed
#: by their own replacements, so a second pass would already fail on "found 0" —
#: but that message blames upstream drift for what is really a duplicated build
#: step, and it would fire only after the trailer had been appended twice.
SENTINELS = (
    "old_cursor, cursor, events = _kanban_read_unclaimed(",
    '_kanban_mark_delivered(self, sub, d["cursor"])',
    "from gateway.kanban_notify_delivery import",
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix="kanban_notify_delivery")
    patch.refuse_if_patched(*SENTINELS)
    for label, anchor, patched in EDITS:
        patch.substitute(anchor, patched, label=label)
    patch.append(TRAILER)
    patch.commit(f"{len(EDITS)} anchors")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
