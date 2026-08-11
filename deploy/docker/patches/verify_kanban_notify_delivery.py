#!/usr/bin/env python3
"""Build gate for the at-least-once kanban notifier patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after the applier.
The applier only proves three anchors matched, and for this patch a matched
anchor is a long way short of the property that matters.

Every failure mode here is silent. The patch does not add a feature whose
absence anyone would notice; it removes a window in which a notification can be
destroyed. An image where it silently did nothing behaves *exactly* like an
image where it worked, right up until the gateway dies in that window — which,
on 2026-08-09, cost task ``t_a18254ca`` a 7,124-byte report that was sitting
complete in the database while the user asked for it by hand. Nothing raised.
The card was marked done, the subscription's cursor said the event had been
handled, and the only surviving evidence was a ``kanban.db.wake`` mtime 162 ms
before the fatal signal.

So this drives the *patched* runtime against a real board rather than reading
it, and it checks two classes of thing:

* **That the patch is wired.** The three names the notifier loop resolves at
  call time, and the absence of upstream's claim. A trailer import that did not
  execute is a ``NameError`` on the first delivery — loud, but only in
  production.
* **That the assumption the patch rests on still holds.** This is the one worth
  the file. The fix is "read without writing, write after delivering", and it is
  correct only while ``kanban_db.unseen_events_for_sub`` remains a pure read.
  Upstream owns that function. If a base-image bump ever gave it a cursor write
  — the way its sibling ``claim_unseen_events_for_sub`` has one — the notifier
  would go straight back to at-most-once delivery with no diagnostic anywhere,
  and every check above would still pass. Section 3 opens a real database and
  looks.

Section 5 replays the incident itself: an event read, a process that dies before
sending, and a second process that must still find the event waiting for it.

Usage::

    cd /opt/hermes && python3 verify_kanban_notify_delivery.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

FAILURES: list[str] = []


def check(label: str, condition: object, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


TMP = Path(tempfile.mkdtemp())
DB = TMP / "kanban.db"
# `_kanban_advance` opens its own connection with `board=None`, which resolves
# through HERMES_KANBAN_DB. Pinning it here is what lets the real watcher method
# be driven rather than imitated.
os.environ["HERMES_KANBAN_DB"] = str(DB)

from hermes_cli import kanban_db as K  # noqa: E402
import gateway.kanban_watchers as watchers  # noqa: E402
from gateway.kanban_notify_delivery import (  # noqa: E402
    MAX_TRACKED,
    advance_after_delivery,
    high_water,
    mark_delivered,
    read_unclaimed,
    sub_key,
)

NOTIFIER_SOURCE = open("gateway/kanban_watchers.py").read()

# The kinds the notifier claims for a subscriber. A completion left behind the
# cursor is a report that never reaches the user's thread.
TERMINAL_KINDS = (
    "completed", "blocked", "gave_up", "crashed", "timed_out",
    "status", "archived", "unblocked", "block_loop_detected",
)

PLATFORM = "slack"
CHAT = "D0BKGRBM6RH"
THREAD = "1786279791.090359"


# --- 1. The wiring resolved ---------------------------------------------------
# All three names arrive in one import trailer, so one missing means none
# resolved — but they are checked separately because each one fails somewhere
# different: the read at the top of the collect loop, the mark and the advance
# at the bottom of the delivery loop.
print("import wiring:")
check(
    "the notifier resolved the read import",
    hasattr(watchers, "_kanban_read_unclaimed"),
    "the trailer import did not execute",
)
check(
    "the notifier resolved the mark import",
    hasattr(watchers, "_kanban_mark_delivered"),
    "the trailer import did not execute",
)
check(
    "the notifier resolved the advance import",
    hasattr(watchers, "_kanban_advance_delivered"),
    "the trailer import did not execute",
)
check(
    "the delivery module is named exactly once",
    NOTIFIER_SOURCE.count("from gateway.kanban_notify_delivery import") == 1,
    f"found {NOTIFIER_SOURCE.count('from gateway.kanban_notify_delivery import')} "
    "trailer imports; 0 means the applier never ran, 2 means it ran twice",
)


# --- 2. Nothing is claimed any more -------------------------------------------
print("the claim is gone:")
check(
    "upstream's claim call does not survive anywhere in the notifier",
    "_kb.claim_unseen_events_for_sub(" not in NOTIFIER_SOURCE,
    "a surviving claim still commits the cursor before anything is delivered",
)
check(
    "the collect site reads through the patch",
    "old_cursor, cursor, events = _kanban_read_unclaimed(" in NOTIFIER_SOURCE,
)
check(
    "the read is handed the watcher",
    "watcher=self," in NOTIFIER_SOURCE,
    "without it there is no high-water map and a failing cursor write "
    "re-sends the same notification every tick forever",
)
check(
    "the task row is still read after the events, not before",
    0
    < NOTIFIER_SOURCE.find("_kanban_read_unclaimed(")
    < NOTIFIER_SOURCE.find('task = _kb.get_task(conn, sub["task_id"])'),
    "hoisting get_task above the read splits complete_task's single "
    "transaction and makes a stale, result-less task row the likely read",
)
check(
    "the success path marks before it writes",
    0
    < NOTIFIER_SOURCE.find('_kanban_mark_delivered(self, sub, d["cursor"])')
    < NOTIFIER_SOURCE.find("_kanban_advance_delivered,"),
)
check(
    "the vestigial rewind no longer writes",
    "_kb.rewind_notify_cursor(" not in NOTIFIER_SOURCE,
    "rewinding a claim that was never made drags a concurrent writer's "
    "cursor backwards and forces a duplicate",
)


# --- 3. The assumption the whole patch rests on -------------------------------
# `unseen_events_for_sub` is upstream's, not ours, and the fix is only a fix
# while it stays a pure read. Its sibling `claim_unseen_events_for_sub` differs
# from it by exactly one `advance_notify_cursor` call inside a `write_txn`; if
# that call ever migrates, delivery silently reverts to at-most-once and every
# check above still passes. This is the reason this file opens a database.
print("upstream's read contract:")
conn = K.connect(DB)

CARD = K.create_task(conn, title="workload reliability audit", assignee="platform")
K.add_notify_sub(
    conn,
    task_id=CARD,
    platform=PLATFORM,
    chat_id=CHAT,
    thread_id=THREAD,
    user_id="U0ADAM",
    notifier_profile="default",
)
# The subscription snaps to the board's current head, so the completion has to
# be appended after it — which is the real ordering: the card is subscribed when
# it is created and completed minutes later.
with K.write_txn(conn):
    K._append_event(conn, CARD, "completed", None)


def sub_row():
    """The subscription exactly as the notifier's collect loop receives it."""
    rows = [
        s
        for s in K.list_notify_subs(conn, CARD)
        if s["platform"] == PLATFORM and s["chat_id"] == CHAT
    ]
    return rows[0] if rows else None


def cursor_now():
    row = conn.execute(
        "SELECT last_event_id FROM kanban_notify_subs "
        "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?",
        (CARD, PLATFORM, CHAT, THREAD),
    ).fetchone()
    return None if row is None else int(row["last_event_id"])


SUB = sub_row()
check("the fixture subscription exists", SUB is not None)
check(
    "the subscription's cursor is on the row the read uses",
    SUB is not None and "last_event_id" in SUB,
    "read_unclaimed takes old_cursor from here instead of a second query",
)

START = cursor_now()
_before_read = START
_read = K.unseen_events_for_sub(
    conn,
    task_id=CARD,
    platform=PLATFORM,
    chat_id=CHAT,
    thread_id=THREAD,
    kinds=TERMINAL_KINDS,
)
# The arity is itself the contract: `read_unclaimed` unpacks two values here and
# returns three, and the claiming sibling it replaced returned three. A base
# image that unified the two signatures would land inside the per-subscription
# `except`, where it logs at WARNING and skips the subscription on every tick.
check(
    "unseen_events_for_sub still returns (cursor, events)",
    isinstance(_read, tuple) and len(_read) == 2,
    f"got {len(_read) if isinstance(_read, tuple) else type(_read).__name__}",
)
_events = _read[1] if isinstance(_read, tuple) and len(_read) == 2 else []
check(
    "the completion is visible to the read",
    [e.kind for e in _events] == ["completed"],
    f"got {[e.kind for e in _events]}",
)
check(
    "unseen_events_for_sub is still a pure read",
    cursor_now() == _before_read,
    f"the cursor moved {_before_read} -> {cursor_now()} on a READ. Upstream "
    "gave this function a write; the patch's at-least-once guarantee is void "
    "and delivery has silently reverted to at-most-once",
)
check(
    "the claiming sibling still exists to be distinguished from it",
    callable(getattr(K, "claim_unseen_events_for_sub", None)),
    "if it is gone, re-derive this gate against whatever replaced it",
)
check(
    "advance_notify_cursor is still the durable write the patch calls",
    callable(getattr(K, "advance_notify_cursor", None)),
)


# --- 4. The method advance_after_delivery calls positionally -------------------
# `advance_after_delivery` calls `watcher._kanban_advance(sub, cursor, board)`.
# Upstream owns that method; a signature change would raise inside the helper's
# own `except`, which by design swallows it — so the write would stop happening
# and the only symptom would be a WARNING nobody reads.
print("the watcher method:")
import inspect  # noqa: E402

_advance = getattr(watchers.GatewayKanbanWatchersMixin, "_kanban_advance", None)
check("the watcher still has _kanban_advance", callable(_advance))
if callable(_advance):
    _params = list(inspect.signature(_advance).parameters)
    check(
        "it still takes (self, sub, cursor, board)",
        _params[:4] == ["self", "sub", "cursor", "board"],
        f"got {_params}; advance_after_delivery passes these positionally and "
        "swallows the TypeError, so the cursor would silently stop advancing",
    )


class _Watcher(watchers.GatewayKanbanWatchersMixin):
    """A real watcher, minus the gateway it normally hangs off.

    ``_kanban_advance`` opens its own connection and touches no instance state,
    so the real method runs here against the real DB.
    """

    def __init__(self):
        pass


# --- 5. The incident, replayed -------------------------------------------------
# 2026-08-09 13:00:29. The notifier claimed event 971, committing the cursor,
# and the process died 162 ms later at the `get_task` on the next line — before
# a single Slack call. The card's own row still held the finished report; the
# cursor said it had been handled; every later tick skipped it. The user asked
# for the answer by hand.
print("the incident, replayed:")
proc1 = _Watcher()
old_cursor, cursor, events = read_unclaimed(
    K, conn, sub_row(), kinds=TERMINAL_KINDS, watcher=proc1,
)
check(
    "the first tick sees the completion",
    [e.kind for e in events] == ["completed"],
    f"got {[e.kind for e in events]}",
)
check(
    "the read returns the three-tuple the collect site unpacks",
    isinstance(old_cursor, int) and isinstance(cursor, int),
    "unpacking two values into three raises inside the per-subscription "
    "except, which logs at WARNING and skips that subscription forever",
)
check(
    "reading did not move the cursor",
    cursor_now() == START,
    f"cursor is {cursor_now()}, was {START}",
)

# ...the process dies here. Nothing was sent. Nothing was written. A new
# process has no high-water map, which is the case that must still work.
del proc1
proc2 = _Watcher()
_, cursor2, events2 = read_unclaimed(
    K, conn, sub_row(), kinds=TERMINAL_KINDS, watcher=proc2,
)
check(
    "a fresh process still finds the notification waiting",
    [e.kind for e in events2] == ["completed"],
    "THE INCIDENT IS BACK: the event was consumed by a tick that delivered "
    "nothing, and no later tick will ever see it",
)
check("and it is the same event", cursor2 == cursor)


# --- 6. Delivery, then the one durable write -----------------------------------
print("delivery advances the cursor:")
mark_delivered(proc2, sub_row(), cursor2)
advance_after_delivery(proc2, sub_row(), cursor2, None)
check(
    "the real _kanban_advance wrote through to the board",
    cursor_now() == cursor2,
    f"cursor is {cursor_now()}, expected {cursor2}",
)
check(
    "a successful write drops the in-process entry",
    high_water(proc2) == {},
    "otherwise the map grows once per delivery for the life of the process",
)
_, _, events3 = read_unclaimed(
    K, conn, sub_row(), kinds=TERMINAL_KINDS, watcher=proc2,
)
check(
    "the delivered event is not offered again",
    events3 == [],
    "at-least-once must not mean every-tick-forever",
)


# --- 7. A cursor write that fails ----------------------------------------------
# The other half of the trade. Upstream's bare `self._kanban_advance` let a
# failing write abort the whole tick — the `kanban notifier tick failed: disk
# I/O error` line in the incident log. It must now cost one duplicate, once,
# and then repair itself.
print("a failing cursor write:")
with K.write_txn(conn):
    K._append_event(conn, CARD, "status", None)
BEFORE_FAILURE = cursor_now()


class _BrokenWatcher(_Watcher):
    def _kanban_advance(self, sub, cursor, board=None):
        raise RuntimeError("disk I/O error")


broken = _BrokenWatcher()
sends = 0
# Sampled inside the loop, not after it: the entry is only *meant* to survive
# for as long as the write is outstanding, and the very next tick repairs the
# cursor and drops it. Reading it after the loop would test the wrong instant.
outstanding = None
for _ in range(5):
    _, c, evs = read_unclaimed(K, conn, sub_row(), kinds=TERMINAL_KINDS, watcher=broken)
    if not evs:
        continue
    sends += 1
    mark_delivered(broken, sub_row(), c)
    advance_after_delivery(broken, sub_row(), c, None)
    outstanding = high_water(broken).get(sub_key(sub_row()))

check(
    "five ticks against a broken write send the message once, not five times",
    sends == 1,
    f"sent {sends} times",
)
check(
    "the failed write did not raise out of the delivery",
    True,
    "reaching this line at all is the check",
)
check(
    "the high-water entry is kept while the write is outstanding",
    outstanding == BEFORE_FAILURE + 1,
    f"got {outstanding!r}",
)
check(
    "and dropped once the repair has made it redundant",
    sub_key(sub_row()) not in high_water(broken),
    f"still holds {high_water(broken)!r}; an entry the durable cursor already "
    "covers only keeps the map from draining",
)
check(
    "the durable cursor was repaired without re-sending",
    cursor_now() == BEFORE_FAILURE + 1,
    f"cursor is {cursor_now()}, expected {BEFORE_FAILURE + 1}; the read path "
    "should retry the write it knows is outstanding",
)


def cursor_on_a_fresh_connection():
    """The repaired cursor as a *different* connection sees it.

    ``cursor_now`` reads back through the same handle that issued the write, so
    an uncommitted transaction would satisfy it — the assertion above cannot
    tell "committed" from "pending on this connection". A gateway restart is a
    new connection, and re-delivery is exactly what an unread repair costs, so
    the durability claim has to be made from one.
    """
    other = K.connect(DB)
    try:
        row = other.execute(
            "SELECT last_event_id FROM kanban_notify_subs "
            "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?",
            (CARD, PLATFORM, CHAT, THREAD),
        ).fetchone()
        return None if row is None else int(row["last_event_id"])
    finally:
        other.close()


check(
    "the repair is durable — a second connection sees it",
    cursor_on_a_fresh_connection() == BEFORE_FAILURE + 1,
    f"another connection reads {cursor_on_a_fresh_connection()}, expected "
    f"{BEFORE_FAILURE + 1}; the repair never left this handle, so a restart "
    "would re-deliver",
)


# --- 8. The bound on the map ---------------------------------------------------
# The map is the one thing the patch adds that grows. It is emptied by every
# successful write, so it only accumulates while writes are failing — but a
# gateway whose disk is gone must not also run out of memory.
print("the map is bounded:")
bounded = _Watcher()
for i in range(MAX_TRACKED + 64):
    mark_delivered(bounded, {**sub_row(), "task_id": f"t_{i:06d}"}, i + 1)
check(
    "the high-water map cannot grow without bound",
    len(high_water(bounded)) <= MAX_TRACKED,
    f"holds {len(high_water(bounded))} entries",
)


# --- 9. The rewind is inert ----------------------------------------------------
# It still has three call sites, each of which also performs the control flow
# that ends the delivery attempt, so the method stays and the body goes.
print("the rewind:")
_rewind = getattr(watchers.GatewayKanbanWatchersMixin, "_kanban_rewind", None)
check("the method is still there for its three call sites", callable(_rewind))
if callable(_rewind):
    AT = cursor_now()
    _rewind(_Watcher(), sub_row(), AT, AT - 1, None)
    check(
        "calling it moves nothing",
        cursor_now() == AT,
        f"cursor moved {AT} -> {cursor_now()}; the rewind still writes",
    )

print()
if FAILURES:
    print(f"verify_kanban_notify_delivery: {len(FAILURES)} check(s) FAILED")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("verify_kanban_notify_delivery: all checks passed")
