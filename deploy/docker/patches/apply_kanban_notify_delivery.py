#!/usr/bin/env python3
"""Wire gateway/kanban_notify_delivery.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. Three anchored
edits across two files plus one import trailer, turning the notifier's
at-most-once claim into at-least-once delivery. Anchors derived against
v2026.9.14 as patched by the steps the Dockerfile runs before this one.

Why the change is needed — the 2026-08-09 loss of ``t_a18254ca``, the reason
the crash is not the defect, what upstream's later per-ping checkpoint does and
does not cover, and the single-writer dependency the trade introduces — is
documented in the module docstring of
``deploy/docker/patches/kanban_notify_delivery.py``. This file documents only
where the edits land and why each anchor is shaped the way it is.

v2026.9.14 split the notifier: the per-tick claim (``_Collector``) and the
per-subscription delivery (``_KanbanNotification``) live in
``gateway/kanban_watchers_notifier.py``; the loop and the cursor helpers
(``_kanban_sub_op`` / ``_kanban_advance`` / ``_kanban_rewind``) stayed on the
mixin in ``gateway/kanban_watchers.py``. Anchors 1 and 2 are therefore in the
notifier module and anchor 3 in the mixin. Both files are checked before either
is written, so a drifted rewind cannot leave the claim swapped on its own.

Anchor 1 (the claim, ``_Collector._claim_for_sub``) and anchor 2 (the advance
at the tail of ``_KanbanNotification.deliver``) are the two halves of one
change and only make sense together: 1 stops the cursor moving before
delivery, 2 makes the one remaining post-delivery cursor write survivable and
dedupe-aware. Anchor 3 neuters the now-vestigial rewind.

**Anchor 2 carries upstream's comment line for the same reason it always
did.** ``await self.advance()`` occurs twice in ``deliver()``: once on the
unknown-platform skip, one indent deeper inside its ``except ValueError:``,
and once at method depth on the success path. The deeper line still contains
the method-depth anchor as a substring, so ``Patch.substitute`` (which
enforces ``expected=1``) would count two on the call alone -- a guaranteed
``SystemExit`` -- and raising ``expected`` to 2 would silently patch the skip
path as well. The comment line above the success-path call is
unique to that site and is load-bearing: if upstream rewords it the build
fails loudly, which is the intended outcome.

The high-water map hangs off ``self.runner`` at both sites — the
``GatewayRunner`` the mixin is part of — because ``_Collector`` is built per
tick and ``_KanbanNotification`` per delivery; only the runner outlives a tick.

Ordering. Must run AFTER ``apply_kanban_wake_nudge.py`` — the anchors were
derived from the fully-patched tree, and although wake_nudge's own anchors
(the loop construction and the sleep sites, all on the mixin) are disjoint
from all three of these, deriving against a different tree than the one the
build produces is how an anchor rots undetected. Disjoint from
``apply_kanban_notifier.py`` and ``apply_kanban_progress_lines.py`` as well:
those own the formatters, the wake set and ``_send_event``, none of which any
anchor here touches. All three append trailers to the notifier module;
appends compose.

Usage::

    python3 apply_kanban_notify_delivery.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

NOTIFIER_RELATIVE = "gateway/kanban_watchers_notifier.py"
MIXIN_RELATIVE = "gateway/kanban_watchers.py"

#: Nesting depth of a method body on ``_Collector`` / ``_KanbanNotification``
#: and on the mixin — every site this patch touches is one level inside a class.
METHOD_INDENT = " " * 8

# --- Anchor 1: the claim (notifier module) ------------------------------------
#
# Upstream commits `last_event_id` here, before ``get_task`` on the next line
# and long before anything is sent. Replaced by a read that writes nothing.

CLAIM_ANCHOR = (
    f"{METHOD_INDENT}old_cursor, cursor, events = _kbn().claim_unseen_events_for_sub(\n"
    f'{METHOD_INDENT}    conn, task_id=sub["task_id"], platform=sub["platform"], chat_id=sub["chat_id"],\n'
    f'{METHOD_INDENT}    thread_id=sub.get("thread_id") or "", kinds=TERMINAL_KINDS,\n'
    f"{METHOD_INDENT})\n"
)

# `watcher=self.runner` carries the in-process high-water map, which is what
# bounds a permanently-failing cursor write to one duplicate per process
# lifetime. `_kbn()` is the module that owns both the read
# (`unseen_events_for_sub`) and the repair write (`advance_notify_cursor`). The
# three-tuple shape is preserved deliberately: `unseen_events_for_sub` returns
# two values, and unpacking two into three raises inside the per-subscription
# `except` in `collect_board`, which logs at WARNING and skips that
# subscription on every tick forever — a worse silent failure than the one
# being fixed.
CLAIM_PATCHED = (
    f"{METHOD_INDENT}# kube-agents patch: read without claiming. See\n"
    f"{METHOD_INDENT}# gateway/kanban_notify_delivery.py.\n"
    f"{METHOD_INDENT}old_cursor, cursor, events = _kanban_read_unclaimed(\n"
    f"{METHOD_INDENT}    _kbn(), conn, sub, kinds=TERMINAL_KINDS, watcher=self.runner,\n"
    f"{METHOD_INDENT})\n"
)

# --- Anchor 2: the success-path advance (notifier module) ---------------------
#
# The comment line is part of the anchor because the call below it is not
# unique in this file. See the module docstring.

ADVANCE_ANCHOR = (
    f"{METHOD_INDENT}# Delivery complete: advance the cursor (the dedup mechanism).\n"
    f"{METHOD_INDENT}await self.advance()\n"
)

# The mark happens on the event loop, before the write is attempted, so it is
# recorded whether or not the write lands. The write itself moves behind a
# helper that logs instead of unwinding the tick — upstream's bare
# `self.advance()` lets a cursor-write failure propagate out of `deliver()`
# into the loop's tick handler and abort every remaining delivery in the tick,
# which is the `kanban notifier tick failed: disk I/O error` line in the
# 2026-08-09 log. `_to_thread_process_service` rather than `asyncio.to_thread`
# for the same reason upstream's `advance()` uses it: a fresh Context so a
# lingering delegate_task marker cannot false-trip `write_txn`'s guard.
ADVANCE_PATCHED = (
    f"{METHOD_INDENT}# Delivery complete: advance the cursor (the dedup mechanism).\n"
    f"{METHOD_INDENT}# kube-agents patch: this is now the only post-delivery cursor\n"
    f"{METHOD_INDENT}# write, and it happens after every leg has been sent. See\n"
    f"{METHOD_INDENT}# gateway/kanban_notify_delivery.py.\n"
    f'{METHOD_INDENT}_kanban_mark_delivered(self.runner, self.sub, self.d["cursor"])\n'
    f"{METHOD_INDENT}await _to_thread_process_service(\n"
    f'{METHOD_INDENT}    _kanban_advance_delivered, self.runner, self.sub, self.d["cursor"], self.board_slug,\n'
    f"{METHOD_INDENT})\n"
)

# --- Anchor 3: the rewind (mixin) ---------------------------------------------
#
# Nothing is claimed any more, so nothing can be undone. Left as a method rather
# than removed: `_KanbanNotification.rewind()` still calls it from every
# handled failure path, each of which also performs the `return` that ends the
# delivery attempt, and neutering here costs one anchor instead of several.

REWIND_ANCHOR = (
    f'{METHOD_INDENT}"""Undo a claimed notification cursor after send failure."""\n'
    f'{METHOD_INDENT}self._kanban_sub_op(board, "rewind_notify_cursor", sub, claimed_cursor=claimed_cursor, old_cursor=old_cursor)\n'
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

NOTIFIER_EDITS = (
    ("notifier claim", CLAIM_ANCHOR, CLAIM_PATCHED),
    ("post-delivery advance", ADVANCE_ANCHOR, ADVANCE_PATCHED),
)

MIXIN_EDITS = (
    ("vestigial rewind", REWIND_ANCHOR, REWIND_PATCHED),
)

# Appended rather than inserted: these names are resolved when the notifier
# runs, long after the module finishes importing. The mixin needs no trailer —
# its replacement uses only ``logger``, which it already imports.
TRAILER = (
    "\n\n# kube-agents patch: see gateway/kanban_notify_delivery.py\n"
    "from gateway.kanban_notify_delivery import (  # noqa: E402\n"
    "    advance_after_delivery as _kanban_advance_delivered,\n"
    "    mark_delivered as _kanban_mark_delivered,\n"
    "    read_unclaimed as _kanban_read_unclaimed,\n"
    ")\n"
)

#: Text that only exists after a successful run. Every anchor is consumed by its
#: own replacement, so a second pass would already fail on "found 0" — but that
#: message blames upstream drift for what is really a duplicated build step,
#: and it would fire only after the trailer had been appended twice.
NOTIFIER_SENTINELS = (
    "old_cursor, cursor, events = _kanban_read_unclaimed(",
    '_kanban_mark_delivered(self.runner, self.sub, self.d["cursor"])',
    "from gateway.kanban_notify_delivery import",
)
MIXIN_SENTINELS = (
    "is a no-op under",
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason.

    Every anchor in both files is checked before either file is written, so a
    drifted mixin anchor cannot leave the notifier module reading without
    claiming while the rewind still drags cursors backwards.
    """
    notifier = patchlib.Patch(root, NOTIFIER_RELATIVE, prefix="kanban_notify_delivery")
    notifier.refuse_if_patched(*NOTIFIER_SENTINELS)
    for label, anchor, patched in NOTIFIER_EDITS:
        notifier.substitute(anchor, patched, label=label)
    notifier.append(TRAILER)

    mixin = patchlib.Patch(root, MIXIN_RELATIVE, prefix="kanban_notify_delivery")
    mixin.refuse_if_patched(*MIXIN_SENTINELS)
    for label, anchor, patched in MIXIN_EDITS:
        mixin.substitute(anchor, patched, label=label)

    notifier.commit(f"{len(NOTIFIER_EDITS)} anchors")
    mixin.commit(f"{len(MIXIN_EDITS)} anchor")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
