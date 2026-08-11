"""At-least-once delivery for the kanban notifier's chat notifications.

Installed into the image as ``gateway/kanban_notify_delivery.py`` and wired
into ``gateway/kanban_watchers.py`` by ``apply_kanban_notify_delivery.py``.

The incident
------------
On 2026-08-09 task ``t_a18254ca`` finished, wrote a 7,124-byte report, and the
user never saw it — they had to ask for the answer by hand ten minutes later.
Nothing failed to *send*: nothing was ever *attempted*.

Upstream's notifier tick claims before it commits to delivering::

    old_cursor, cursor, events = _kb.claim_unseen_events_for_sub(...)  # COMMITS
    if not events:
        continue
    task = _kb.get_task(conn, sub["task_id"])                          # died here

``claim_unseen_events_for_sub`` advances ``kanban_notify_subs.last_event_id``
to the new cursor inside its own ``BEGIN IMMEDIATE``. The moment that commits,
the event is durably marked "seen" — while the only thing that knows it was
never delivered is a Python local (``d["old_cursor"]``) and the only thing that
can undo it is an in-process ``_kanban_rewind``. The gateway took a fatal
signal 162 ms after that commit, at ``get_task``. The undo died with it, the
cursor stayed at 971, and no later tick ever looked at the event again.

**The crash is not the defect.** The per-subscription handler that wraps the
claim catches bare ``Exception`` and does *not* rewind — it logs a warning and
moves to the next subscription. Any exception raised between the claim and a
successful send loses the message exactly as permanently as a fatal signal
does; the crash is merely the instance that was caught on camera. The defect is
that the cursor is written before anything is committed to delivering.

The fix
-------
Read without claiming; advance only after the send succeeds.

``unseen_events_for_sub`` is upstream's own read-only sibling of the claim —
same query, no write — and it exists precisely for this call shape. Its
docstring says so: "The subscription's cursor is NOT advanced here; call
``advance_notify_cursor`` after the gateway has successfully delivered the
notifications." The notifier already calls ``_kanban_advance`` on the success
path, so the second half is in place; this patch supplies the first.

That converts a **permanent silent loss** into a **recoverable duplicate**, and
it covers the ordinary-exception path as well as the crash path. Every window
between the read and the send — ``get_task``, adapter resolution, message
construction, artifact upload, the Slack round trip — becomes replayable: no
durable state moved, so the next tick (≤5 s later, or ≤0.25 s via the wake
nudge) re-reads the same events and tries again.

Three things at-least-once needs that the naive swap does not have
------------------------------------------------------------------
**1. A bound on the resend storm.** Once the cursor is the *only* record of
delivery, "send succeeded, cursor write failed" re-posts the same Slack message
every tick, for as long as the write keeps failing. Upstream's advance is
unwrapped, and an exception from it escapes the per-subscription scope all the
way to the tick handler — the literal ``kanban notifier tick failed: disk I/O
error`` line in this incident's log. So:

* :func:`advance_after_delivery` wraps the advance and logs instead of
  unwinding the tick. Under the old semantics that exception also aborted every
  remaining delivery in the tick, so this is a fix regardless of the rest.
* :func:`mark_delivered` records an in-process high-water mark *before* the
  durable advance is attempted. :func:`read_unclaimed` filters against it, so a
  permanently failing advance costs **one duplicate per process lifetime**
  rather than one every five seconds.
* When the high-water suppresses everything a subscription has to offer, the
  durable cursor is simply behind — :func:`read_unclaimed` retries the write
  directly (:func:`_repair_cursor`) rather than sending anything.

The high-water entry is dropped as soon as the durable advance succeeds, so in
normal operation the map is empty; :data:`MAX_TRACKED` is a backstop, not a
working limit.

**2. The right tuple.** ``unseen_events_for_sub`` returns ``(new_cursor,
events)``; the claim returns ``(old_cursor, new_cursor, events)``. Swapping one
for the other in place raises ``ValueError: not enough values to unpack``
*inside* the per-subscription ``except`` — which logs at WARNING and skips that
subscription on every tick, forever. A worse silent failure than the one being
fixed. :func:`read_unclaimed` returns the three-tuple the call site expects and
takes ``old_cursor`` from the subscription row (``list_notify_subs`` is
``SELECT *``, so ``last_event_id`` is already there — no extra query).

**3. No leftover rewinds.** With nothing claimed, there is nothing to undo, and
``rewind_notify_cursor`` is not inert: it is a compare-and-swap
(``SET last_event_id = old WHERE last_event_id = claimed``). Leaving the three
call sites live means that if any concurrent writer had advanced the row to
exactly the cursor this tick computed, the rewind drags it *backwards* and
forces a duplicate. The applier neuters ``_kanban_rewind`` itself rather than
deleting three call sites, which keeps the surrounding control flow — each site
also does the ``continue``/``break`` that makes the retry work — and costs one
anchor instead of three. Retry semantics are unchanged: a failed send now
retries because the cursor was never written, instead of because it was written
and then unwritten.

Single-writer dependency — read this before scaling out
-------------------------------------------------------
The claim's stated purpose was multi-process single-ownership: "concurrent
watchers serialize on SQLite's writer lock, and only the first process sees and
claims a given event range." This patch trades that for at-least-once, so
**duplicate delivery is prevented by there being exactly one notifier, not by
the database.**

That holds today: the operator renders ``replicas: 1`` with
``strategy: Recreate`` (it only emits RollingUpdate above one replica), one
``hermes gateway run`` per pod, one notifier task inside it, ticks strictly
sequential. The only other caller of ``claim_unseen_events_for_sub`` —
``tui_gateway/server.py`` — hard-filters ``platform == "tui"`` and touches
disjoint rows. (It has the same claim-before-deliver defect with *no* rewind
anywhere, which is why the claim helper stays in ``kanban_db.py``.)

Setting ``availability.replicas: 2`` would make duplicate Slack posts reachable
and would additionally require ``advance_notify_cursor`` to become monotonic
(``AND last_event_id < ?``). Treat that as a correctness change, not a scaling
knob.

What this patch deliberately does not do
----------------------------------------
* **It does not reorder ``get_task``.** Hoisting the task read above the event
  read looks like it shrinks the fatal window; it does the opposite.
  ``complete_task`` writes the task row and the ``completed`` event in one
  transaction, so reading events first *guarantees* the result is already
  populated. Reading the task first splits that — and the read blocks behind
  the completing worker under a 120-second busy timeout, so a stale task row is
  the deterministic outcome under contention, not a race. The report would be
  dropped and the subscription leaked. Under this patch the question is moot:
  there is no commit for ``get_task`` to sit after.
* **It does not touch ``mmap_size``.** The SIGBUS did not come from a mapped
  database file. ``mmap_size`` is already 0 on every kanban connection, and the
  gateway has no mapping of any ``.db`` file at all.
* **It does not persist the undo.** A durable claim lease (the rewind intent
  written into the same transaction as the cursor) would close the window by
  construction rather than by not-writing. It needs a schema column and four
  more anchors in ``kanban_db.py``; this patch is the smaller change that makes
  the loss recoverable, and the lease remains the upgrade path if the
  single-writer assumption ever has to go.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

#: Backstop on the high-water map. Entries are removed as soon as their durable
#: advance lands, so a healthy gateway holds zero of them and this ceiling is
#: never approached. It exists so that a board whose cursor writes fail
#: indefinitely cannot grow the map without bound; evicting the oldest entry
#: costs at most one duplicate for a subscription that has been idle across
#: this many other deliveries, which at-least-once tolerates by definition.
MAX_TRACKED = 2048

#: Attribute the high-water map hangs off on the watcher instance. Same
#: lazily-initialised pattern as upstream's ``_kanban_sub_fail_counts``.
_ATTR = "_kanban_delivered_high_water"


def sub_key(sub: dict) -> tuple:
    """The subscription's identity — the same four columns the cursor is keyed on.

    Matches upstream's ``sub_key`` in the delivery loop so the two maps agree on
    what "the same subscription" means.
    """
    return (
        sub["task_id"],
        sub["platform"],
        sub["chat_id"],
        sub.get("thread_id") or "",
    )


def high_water(watcher: Any) -> dict:
    """The watcher's in-process delivered-cursor map, created on first use."""
    tracked = getattr(watcher, _ATTR, None)
    if tracked is None:
        tracked = {}
        setattr(watcher, _ATTR, tracked)
    return tracked


def mark_delivered(watcher: Any, sub: dict, cursor: int) -> None:
    """Record that ``cursor`` has been delivered, before the durable write.

    Called on the success path only — control reaches it after every text ping
    in the batch has been sent (or, for non-push adapters, after the wake
    self-post that *is* the delivery has succeeded). Marking earlier would
    suppress the retry of a send that failed.
    """
    tracked = high_water(watcher)
    key = sub_key(sub)
    tracked[key] = max(int(tracked.get(key, 0)), int(cursor))
    while len(tracked) > MAX_TRACKED:
        # dicts preserve insertion order; re-assigning an existing key does not
        # move it, so this evicts the least recently *added* entry.
        tracked.pop(next(iter(tracked)), None)


def advance_after_delivery(
    watcher: Any, sub: dict, cursor: int, board: Optional[str] = None,
) -> None:
    """Advance the durable cursor after delivery, without unwinding the tick.

    Runs in ``asyncio.to_thread``, exactly where upstream called
    ``_kanban_advance``. On success the high-water entry is dropped, because the
    durable cursor now says everything the entry was standing in for.
    """
    try:
        watcher._kanban_advance(sub, int(cursor), board)
    except Exception as exc:
        # Deliberately swallowed. The message HAS been delivered; the only thing
        # lost is the record of it, and the high-water entry now covers that
        # until the next tick retries the write. Letting this propagate would
        # abort every remaining delivery in the tick — which is what upstream
        # did, and what turned a single failing cursor write into a tick-wide
        # outage in the 2026-08-09 log.
        logger.warning(
            "kanban notifier: delivered %s but could not advance its cursor "
            "to %s (%s); suppressing re-delivery in this process and retrying "
            "the write on the next tick",
            sub.get("task_id"), cursor, exc,
        )
        return
    high_water(watcher).pop(sub_key(sub), None)


def _repair_cursor(kb: Any, conn: Any, sub: dict, cursor: int) -> bool:
    """Retry a durable advance that failed after a successful delivery.

    Reached only when the high-water mark suppressed every event a subscription
    had to offer, which means this process already delivered them and only the
    cursor write is outstanding. Sends nothing. Returns whether the write
    landed, so the caller can drop the high-water entry it was standing in for.

    The call below is deliberately **not** wrapped in ``kb.write_txn``.
    ``advance_notify_cursor`` opens its own ``BEGIN IMMEDIATE`` internally, and
    ``kanban_db.connect`` uses ``isolation_level=None``, so the write commits on
    its own and wrapping it raises ``OperationalError: cannot start a
    transaction within a transaction`` — which this ``except`` would swallow
    into a warning, leaving the cursor unrepaired on every tick. The verify
    reads the repaired value back on a second connection precisely so this
    claim is tested rather than asserted.
    """
    try:
        kb.advance_notify_cursor(
            conn,
            task_id=sub["task_id"],
            platform=sub["platform"],
            chat_id=sub["chat_id"],
            thread_id=sub.get("thread_id") or "",
            new_cursor=int(cursor),
        )
    except Exception as exc:
        logger.warning(
            "kanban notifier: cursor repair for %s to %s failed: %s",
            sub.get("task_id"), cursor, exc,
        )
        return False
    logger.info(
        "kanban notifier: repaired the cursor for %s to %s after a delivered "
        "notification whose advance had failed",
        sub.get("task_id"), cursor,
    )
    return True


def read_unclaimed(
    kb: Any,
    conn: Any,
    sub: dict,
    *,
    kinds: Optional[Iterable[str]] = None,
    watcher: Any = None,
) -> tuple[int, int, list]:
    """Read a subscription's undelivered events **without** claiming them.

    Drop-in for ``claim_unseen_events_for_sub`` at the notifier's collect site:
    same ``(old_cursor, new_cursor, events)`` shape, same empty-list-means-skip
    contract, no write on the read path.

    ``old_cursor`` comes from the subscription row rather than a second query —
    ``list_notify_subs`` is ``SELECT *``. It is returned only because the call
    site stores it in the delivery dict; nothing consumes it any more, now that
    the rewind is a no-op.

    The returned cursor is recomputed from the events that survive the
    high-water filter, so the durable cursor can never be advanced past an event
    the in-process layer suppressed.
    """
    old_cursor = int(sub.get("last_event_id") or 0)
    new_cursor, events = kb.unseen_events_for_sub(
        conn,
        task_id=sub["task_id"],
        platform=sub["platform"],
        chat_id=sub["chat_id"],
        thread_id=sub.get("thread_id") or "",
        kinds=kinds,
    )
    if not events:
        return old_cursor, old_cursor, []
    tracked = high_water(watcher) if watcher is not None else {}
    seen = int(tracked.get(sub_key(sub), 0))
    fresh = [ev for ev in events if int(ev.id) > seen]
    if not fresh:
        # Everything visible was already sent by this process; the durable
        # cursor is simply behind. Fix the cursor, send nothing.
        repaired_to = min(int(new_cursor), seen)
        if _repair_cursor(kb, conn, sub, repaired_to) and repaired_to >= seen:
            # Same invariant as advance_after_delivery: once the durable cursor
            # says everything the entry was standing in for, the entry is
            # redundant and holding it only keeps the map from draining. Not
            # dropped when the write landed short of `seen` — the remainder is
            # still only in this process's memory.
            tracked.pop(sub_key(sub), None)
        return old_cursor, old_cursor, []
    return old_cursor, max(int(ev.id) for ev in fresh), fresh
