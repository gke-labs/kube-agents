"""Unit tests for the at-least-once notifier patch installed by the Dockerfile.

Two halves, matching the two files under test:

* :class:`ReadUnclaimedTest` and friends drive ``kanban_notify_delivery`` itself
  against a fake ``kanban_db`` — the delivery semantics, the high-water bound,
  and the cursor repair.
* :class:`ApplyTest` drives ``apply_kanban_notify_delivery`` against a reduced
  copy of upstream's notifier loop. The fixture deliberately contains **both**
  occurrences of the advance block, at their real 24-space indentation, because
  the ambiguity between them is the reason anchor 2 is shaped the way it is; a
  fixture with only one would let a regression through.

The behavioural gate that proves the patched notifier actually replays a lost
notification is ``verify_kanban_notify_delivery.py``, which runs inside the
image against the real board. These tests run on the repo, where Hermes is not
importable.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import ast
import logging
import tempfile
import unittest
from pathlib import Path

from apply_kanban_notify_delivery import (
    ADVANCE_ANCHOR,
    CLAIM_ANCHOR,
    RELATIVE,
    REWIND_ANCHOR,
    apply,
)
from kanban_notify_delivery import (
    MAX_TRACKED,
    advance_after_delivery,
    high_water,
    mark_delivered,
    read_unclaimed,
    sub_key,
)

# The subscription from the 2026-08-09 incident, in the shape `list_notify_subs`
# returns it (SELECT *, so `last_event_id` is present — which is where
# `read_unclaimed` gets `old_cursor` without a second query).
SUB = {
    "task_id": "t_a18254ca",
    "platform": "slack",
    "chat_id": "D0BKGRBM6RH",
    "thread_id": "1786279791.090359",
    "last_event_id": 958,
}

TERMINAL_KINDS = ("completed", "blocked", "gave_up", "crashed", "timed_out")


class _Event:
    """The two attributes the notifier reads off a ``kanban_db.Event``."""

    def __init__(self, id, kind="completed"):
        self.id = id
        self.kind = kind


class _FakeKB:
    """Enough of ``hermes_cli.kanban_db`` for the collect site.

    Records every call so a test can assert that the read path performs no
    write, which is the entire point of the patch.
    """

    def __init__(self, events=(), advance_error=None):
        self.events = list(events)
        self.advance_error = advance_error
        self.advances = []
        self.reads = 0

    def unseen_events_for_sub(self, conn, *, task_id, platform, chat_id,
                              thread_id=None, kinds=None):
        self.reads += 1
        self.last_kinds = kinds
        cursor = conn["last_event_id"]
        fresh = [e for e in self.events if e.id > cursor]
        if kinds is not None:
            fresh = [e for e in fresh if e.kind in kinds]
        return (max([cursor] + [e.id for e in fresh]), fresh)

    def advance_notify_cursor(self, conn, *, task_id, platform, chat_id,
                              thread_id=None, new_cursor):
        if self.advance_error is not None:
            raise self.advance_error
        self.advances.append(new_cursor)
        conn["last_event_id"] = new_cursor


class _Watcher:
    """Stand-in for the gateway watcher: owns the map and the advance helper."""

    def __init__(self, kb=None, conn=None, error=None):
        self.kb = kb
        self.conn = conn
        self.error = error
        self.advanced = []

    def _kanban_advance(self, sub, cursor, board=None):
        if self.error is not None:
            raise self.error
        self.advanced.append((sub["task_id"], cursor, board))
        if self.conn is not None:
            self.conn["last_event_id"] = cursor


def conn_at(cursor):
    """The one column the fake DB layer needs — the subscription's cursor."""
    return {"last_event_id": cursor}


# =============================================================================
# The read path
# =============================================================================


class ReadUnclaimedTest(unittest.TestCase):
    def test_the_read_writes_nothing(self):
        # The defect in one assertion: upstream committed the cursor here, and
        # the process died before anything was sent.
        kb = _FakeKB([_Event(971)])
        conn = conn_at(958)
        read_unclaimed(kb, conn, SUB, kinds=TERMINAL_KINDS, watcher=_Watcher())
        self.assertEqual(kb.advances, [])
        self.assertEqual(conn["last_event_id"], 958)

    def test_it_returns_the_three_tuple_the_call_site_unpacks(self):
        # `unseen_events_for_sub` returns two values. Unpacking two into three
        # raises inside the per-subscription `except`, which logs at WARNING and
        # skips that subscription on every tick forever.
        kb = _FakeKB([_Event(971)])
        result = read_unclaimed(kb, conn_at(958), SUB, kinds=TERMINAL_KINDS,
                                watcher=_Watcher())
        self.assertEqual(len(result), 3)
        old_cursor, cursor, events = result
        self.assertEqual(old_cursor, 958)
        self.assertEqual(cursor, 971)
        self.assertEqual([e.id for e in events], [971])

    def test_old_cursor_comes_off_the_subscription_row(self):
        # No second query: `list_notify_subs` already selected it.
        old_cursor, _, _ = read_unclaimed(
            _FakeKB([_Event(971)]), conn_at(958), SUB,
            kinds=TERMINAL_KINDS, watcher=_Watcher(),
        )
        self.assertEqual(old_cursor, SUB["last_event_id"])

    def test_a_row_with_no_cursor_reads_as_zero(self):
        sub = {k: v for k, v in SUB.items() if k != "last_event_id"}
        old_cursor, _, _ = read_unclaimed(
            _FakeKB([_Event(3)]), conn_at(0), sub,
            kinds=TERMINAL_KINDS, watcher=_Watcher(),
        )
        self.assertEqual(old_cursor, 0)

    def test_no_events_yields_the_skip_shape(self):
        # `if not events: continue` upstream — the empty list must survive.
        old_cursor, cursor, events = read_unclaimed(
            _FakeKB([]), conn_at(958), SUB, kinds=TERMINAL_KINDS,
            watcher=_Watcher(),
        )
        self.assertEqual((old_cursor, cursor, events), (958, 958, []))

    def test_the_kind_filter_is_passed_through(self):
        kb = _FakeKB([_Event(960, kind="heartbeat"), _Event(971)])
        _, cursor, events = read_unclaimed(
            kb, conn_at(958), SUB, kinds=TERMINAL_KINDS, watcher=_Watcher(),
        )
        self.assertEqual(kb.last_kinds, TERMINAL_KINDS)
        self.assertEqual([e.id for e in events], [971])
        self.assertEqual(cursor, 971)

    def test_it_works_without_a_watcher(self):
        # Defensive: the collect site always passes one, but a helper that
        # explodes on None would turn a wiring slip into a silent skip.
        _, cursor, events = read_unclaimed(
            _FakeKB([_Event(971)]), conn_at(958), SUB, kinds=TERMINAL_KINDS,
        )
        self.assertEqual((cursor, len(events)), (971, 1))


# =============================================================================
# The high-water bound
# =============================================================================


class HighWaterTest(unittest.TestCase):
    def test_a_delivered_event_is_not_read_again_by_this_process(self):
        # The resend storm this exists to bound: send succeeded, cursor write
        # failed, so the durable cursor still says 958 and the read would
        # otherwise return event 971 again on every tick.
        watcher = _Watcher()
        mark_delivered(watcher, SUB, 971)
        _, _, events = read_unclaimed(
            _FakeKB([_Event(971)]), conn_at(958), SUB,
            kinds=TERMINAL_KINDS, watcher=watcher,
        )
        self.assertEqual(events, [])

    def test_the_suppressed_read_repairs_the_durable_cursor(self):
        # Nothing is sent, but the write that failed last tick is retried.
        watcher = _Watcher()
        mark_delivered(watcher, SUB, 971)
        kb = _FakeKB([_Event(971)])
        conn = conn_at(958)
        read_unclaimed(kb, conn, SUB, kinds=TERMINAL_KINDS, watcher=watcher)
        self.assertEqual(kb.advances, [971])
        self.assertEqual(conn["last_event_id"], 971)

    def test_a_failing_repair_does_not_raise(self):
        # It runs inside the per-subscription try, but raising here would skip
        # every remaining subscription's read for no benefit.
        watcher = _Watcher()
        mark_delivered(watcher, SUB, 971)
        kb = _FakeKB([_Event(971)], advance_error=RuntimeError("disk I/O error"))
        with self.assertLogs("kanban_notify_delivery", level=logging.WARNING):
            _, _, events = read_unclaimed(
                kb, conn_at(958), SUB, kinds=TERMINAL_KINDS, watcher=watcher,
            )
        self.assertEqual(events, [])

    def test_an_event_newer_than_the_high_water_still_delivers(self):
        # The mark suppresses re-delivery, not delivery.
        watcher = _Watcher()
        mark_delivered(watcher, SUB, 971)
        _, cursor, events = read_unclaimed(
            _FakeKB([_Event(971), _Event(980)]), conn_at(958), SUB,
            kinds=TERMINAL_KINDS, watcher=watcher,
        )
        self.assertEqual([e.id for e in events], [980])
        # Recomputed from the surviving events, so the durable cursor can never
        # be advanced past something the in-memory layer suppressed.
        self.assertEqual(cursor, 980)

    def test_the_mark_only_moves_forward(self):
        watcher = _Watcher()
        mark_delivered(watcher, SUB, 971)
        mark_delivered(watcher, SUB, 960)
        self.assertEqual(high_water(watcher)[sub_key(SUB)], 971)

    def test_subscriptions_are_keyed_on_all_four_columns(self):
        # Same task, different thread, is a different delivery.
        watcher = _Watcher()
        mark_delivered(watcher, SUB, 971)
        other = dict(SUB, thread_id="1786279791.999999")
        _, _, events = read_unclaimed(
            _FakeKB([_Event(971)]), conn_at(958), other,
            kinds=TERMINAL_KINDS, watcher=watcher,
        )
        self.assertEqual([e.id for e in events], [971])

    def test_the_map_is_bounded(self):
        watcher = _Watcher()
        for i in range(MAX_TRACKED + 50):
            mark_delivered(watcher, dict(SUB, task_id=f"t_{i:06d}"), i + 1)
        self.assertLessEqual(len(high_water(watcher)), MAX_TRACKED)
        # Oldest evicted, newest retained.
        self.assertIn(sub_key(dict(SUB, task_id=f"t_{MAX_TRACKED + 49:06d}")),
                      high_water(watcher))
        self.assertNotIn(sub_key(dict(SUB, task_id="t_000000")),
                         high_water(watcher))


# =============================================================================
# The write path
# =============================================================================


class AdvanceAfterDeliveryTest(unittest.TestCase):
    def test_a_successful_advance_clears_the_high_water_entry(self):
        # The durable cursor now says everything the entry stood in for, so in
        # normal operation the map is empty rather than growing per delivery.
        watcher = _Watcher()
        mark_delivered(watcher, SUB, 971)
        advance_after_delivery(watcher, SUB, 971, "default")
        self.assertEqual(watcher.advanced, [("t_a18254ca", 971, "default")])
        self.assertEqual(high_water(watcher), {})

    def test_a_failing_advance_does_not_unwind_the_tick(self):
        # Upstream's bare `self._kanban_advance` let this abort every remaining
        # delivery in the tick — the `kanban notifier tick failed: disk I/O
        # error` line in the incident log.
        watcher = _Watcher(error=RuntimeError("disk I/O error"))
        mark_delivered(watcher, SUB, 971)
        with self.assertLogs("kanban_notify_delivery", level=logging.WARNING) as log:
            advance_after_delivery(watcher, SUB, 971, "default")
        self.assertIn("could not advance", "\n".join(log.output))

    def test_a_failing_advance_keeps_the_high_water_entry(self):
        # Keeping it is what bounds the storm to one duplicate per process.
        watcher = _Watcher(error=RuntimeError("disk I/O error"))
        mark_delivered(watcher, SUB, 971)
        with self.assertLogs("kanban_notify_delivery", level=logging.WARNING):
            advance_after_delivery(watcher, SUB, 971, "default")
        self.assertEqual(high_water(watcher)[sub_key(SUB)], 971)


class EndToEndSemanticsTest(unittest.TestCase):
    """The two sequences the patch exists to change, end to end."""

    def read(self, kb, conn, watcher):
        return read_unclaimed(kb, conn, dict(SUB, last_event_id=conn["last_event_id"]),
                              kinds=TERMINAL_KINDS, watcher=watcher)

    def test_a_crash_between_read_and_send_loses_nothing(self):
        # The incident. Tick 1 reads and then the process dies; tick 2 — in a
        # brand new process, so no high-water map survives — must still see it.
        kb, conn = _FakeKB([_Event(971)]), conn_at(958)
        _, _, events = self.read(kb, conn, _Watcher())
        self.assertEqual([e.id for e in events], [971])
        # …process dies here. Nothing was written.
        self.assertEqual(conn["last_event_id"], 958)

        fresh_process = _Watcher(conn=conn)
        _, cursor, events = self.read(kb, conn, fresh_process)
        self.assertEqual([e.id for e in events], [971])
        advance_after_delivery(fresh_process, SUB, cursor)
        self.assertEqual(conn["last_event_id"], 971)

    def test_a_permanently_failing_cursor_write_costs_one_duplicate(self):
        # Not zero — at-least-once buys recoverability with duplicates — but
        # one per process, not one every five seconds.
        kb, conn = _FakeKB([_Event(971)]), conn_at(958)
        watcher = _Watcher(error=RuntimeError("disk I/O error"))
        sends = 0
        for _ in range(10):
            _, cursor, events = self.read(kb, conn, watcher)
            if not events:
                continue
            sends += 1
            mark_delivered(watcher, SUB, cursor)
            with self.assertLogs("kanban_notify_delivery", level=logging.WARNING):
                advance_after_delivery(watcher, SUB, cursor)
        self.assertEqual(sends, 1)


# =============================================================================
# The applier
# =============================================================================

# Upstream's notifier reduced to the three sites the patch rewrites, at their
# real nesting depth because every anchor is indentation-sensitive. Both
# advance blocks are present: they are byte-identical at identical indentation,
# and that ambiguity is why anchor 2 carries a comment line.
UPSTREAM_WATCHERS = '''\
class GatewayKanbanWatchers:
    async def _kanban_notifier_watcher(self, interval: float = 5.0) -> None:
        while self._running:
            try:
                def _collect():
                    for board_meta in boards:
                        try:
                            for sub in subs:
                                try:
                                    old_cursor, cursor, events = _kb.claim_unseen_events_for_sub(
                                        conn,
                                        task_id=sub["task_id"],
                                        platform=sub["platform"],
                                        chat_id=sub["chat_id"],
                                        thread_id=sub.get("thread_id") or "",
                                        kinds=TERMINAL_KINDS,
                                    )
                                    if not events:
                                        continue
                                    task = _kb.get_task(conn, sub["task_id"])
                                except Exception as sub_exc:
                                    logger.warning("failed: %s", sub_exc)
                        finally:
                            conn.close()

                deliveries = await asyncio.to_thread(_collect)
                for d in deliveries:
                    try:
                        plat = _Platform(platform_str)
                    except ValueError:
                        # Unknown platform string; skip and advance cursor so
                        # we don't replay forever.
                        await asyncio.to_thread(
                            self._kanban_advance, sub, d["cursor"], board_slug,
                        )
                        continue
                    for ev in d["events"]:
                        await adapter.send(sub["chat_id"], msg)
                    else:
                        # Delivery complete (text ping for push adapters, wake
                        # self-post for non-push): advance cursor. The cursor
                        # is the dedup mechanism — it prevents re-delivery
                        # of the same event on subsequent ticks.
                        await asyncio.to_thread(
                            self._kanban_advance, sub, d["cursor"], board_slug,
                        )
                        if task_terminal:
                            await asyncio.to_thread(
                                self._kanban_unsub, sub, board_slug,
                            )
            except Exception as exc:
                logger.warning("kanban notifier tick failed: %s", exc)

    def _kanban_rewind(
        self,
        sub: dict,
        claimed_cursor: int,
        old_cursor: int,
        board: Optional[str] = None,
    ) -> None:
        """Sync helper: undo a claimed notification cursor after send failure."""
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.rewind_notify_cursor(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
                claimed_cursor=claimed_cursor,
                old_cursor=old_cursor,
            )
        finally:
            conn.close()
'''


def patch_tree(source):
    """Write ``source`` as gateway/kanban_watchers.py under a temp root and patch it."""
    root = Path(tempfile.mkdtemp())
    target = root / RELATIVE
    target.parent.mkdir(parents=True)
    target.write_text(source)
    apply(root)
    return target.read_text()


class ApplyTest(unittest.TestCase):
    def test_every_anchor_matches_upstream_exactly_once(self):
        for label, anchor in (
            ("claim", CLAIM_ANCHOR),
            ("advance", ADVANCE_ANCHOR),
            ("rewind", REWIND_ANCHOR),
        ):
            with self.subTest(label):
                self.assertEqual(UPSTREAM_WATCHERS.count(anchor), 1)

    def test_the_advance_block_alone_is_ambiguous(self):
        # The reason anchor 2 carries a comment line. Without it `substitute`
        # would SystemExit on expected=1, and raising the count to 2 would
        # silently patch the unknown-platform skip as well.
        bare = "\n".join(ADVANCE_ANCHOR.splitlines()[1:]) + "\n"
        self.assertEqual(UPSTREAM_WATCHERS.count(bare), 2)
        self.assertEqual(UPSTREAM_WATCHERS.count(ADVANCE_ANCHOR), 1)

    def test_the_claim_is_gone(self):
        patched = patch_tree(UPSTREAM_WATCHERS)
        self.assertNotIn("_kb.claim_unseen_events_for_sub(", patched)
        self.assertIn("old_cursor, cursor, events = _kanban_read_unclaimed(", patched)

    def test_the_read_is_handed_the_watcher(self):
        # Without it there is no high-water map and no bound on the resend
        # storm, and the patch would still look applied.
        self.assertIn("watcher=self,", patch_tree(UPSTREAM_WATCHERS))

    def test_get_task_stays_below_the_read(self):
        # Hoisting it above would split `complete_task`'s single transaction
        # and make a stale task row the deterministic outcome under contention.
        patched = patch_tree(UPSTREAM_WATCHERS)
        self.assertLess(
            patched.index("_kanban_read_unclaimed("),
            patched.index('task = _kb.get_task(conn, sub["task_id"])'),
        )

    def test_only_the_success_path_advance_is_rewritten(self):
        patched = patch_tree(UPSTREAM_WATCHERS)
        self.assertEqual(patched.count("_kanban_advance_delivered"), 2)  # call + import
        # The unknown-platform skip keeps upstream's direct advance: nothing was
        # delivered there, so there is nothing to mark.
        self.assertEqual(
            patched.count('self._kanban_advance, sub, d["cursor"], board_slug,'), 1
        )

    def test_the_mark_precedes_the_durable_write(self):
        patched = patch_tree(UPSTREAM_WATCHERS)
        self.assertLess(
            patched.index('_kanban_mark_delivered(self, sub, d["cursor"])'),
            patched.index("_kanban_advance_delivered,"),
        )

    def test_the_mark_follows_the_send(self):
        # Marking before the send would suppress the retry of a failed one.
        patched = patch_tree(UPSTREAM_WATCHERS)
        self.assertLess(
            patched.index("await adapter.send("),
            patched.index('_kanban_mark_delivered(self, sub, d["cursor"])'),
        )

    def test_the_rewind_no_longer_writes(self):
        patched = patch_tree(UPSTREAM_WATCHERS)
        self.assertNotIn("_kb.rewind_notify_cursor(", patched)
        self.assertIn("no-op", patched)

    def test_one_import_trailer_carries_all_three_names(self):
        patched = patch_tree(UPSTREAM_WATCHERS)
        for name in (
            "advance_after_delivery as _kanban_advance_delivered",
            "mark_delivered as _kanban_mark_delivered",
            "read_unclaimed as _kanban_read_unclaimed",
        ):
            self.assertIn(name, patched)
        self.assertEqual(
            patched.count("from gateway.kanban_notify_delivery import"), 1
        )

    def test_the_patched_module_still_parses(self):
        ast.parse(patch_tree(UPSTREAM_WATCHERS))

    def test_a_drifted_claim_anchor_fails_loudly(self):
        drifted = UPSTREAM_WATCHERS.replace("kinds=TERMINAL_KINDS,", "kinds=KINDS,", 1)
        with self.assertRaises(SystemExit) as ctx:
            patch_tree(drifted)
        self.assertIn("found 0", str(ctx.exception))
        self.assertIn("notifier claim", str(ctx.exception))

    def test_a_reworded_advance_comment_fails_loudly(self):
        # The comment line is load-bearing. If upstream rewords it the build
        # must stop, because the remaining lines match two different sites.
        drifted = UPSTREAM_WATCHERS.replace(
            "# of the same event on subsequent ticks.",
            "# of the same event on later ticks.",
        )
        with self.assertRaises(SystemExit) as ctx:
            patch_tree(drifted)
        self.assertIn("found 0", str(ctx.exception))
        self.assertIn("post-delivery advance", str(ctx.exception))

    def test_a_drifted_rewind_anchor_fails_loudly(self):
        drifted = UPSTREAM_WATCHERS.replace("claimed_cursor=claimed_cursor,", "")
        with self.assertRaises(SystemExit) as ctx:
            patch_tree(drifted)
        self.assertIn("found 0", str(ctx.exception))
        self.assertIn("vestigial rewind", str(ctx.exception))

    def test_a_later_anchor_failing_leaves_the_file_untouched(self):
        drifted = UPSTREAM_WATCHERS.replace("claimed_cursor=claimed_cursor,", "")
        root = Path(tempfile.mkdtemp())
        target = root / RELATIVE
        target.parent.mkdir(parents=True)
        target.write_text(drifted)
        with self.assertRaises(SystemExit):
            apply(root)
        self.assertEqual(target.read_text(), drifted)

    def test_applying_twice_fails_rather_than_stacking_a_trailer(self):
        root = Path(tempfile.mkdtemp())
        target = root / RELATIVE
        target.parent.mkdir(parents=True)
        target.write_text(UPSTREAM_WATCHERS)
        apply(root)
        with self.assertRaises(SystemExit) as ctx:
            apply(root)
        self.assertIn("already patched", str(ctx.exception))
        self.assertEqual(
            target.read_text().count("from gateway.kanban_notify_delivery import"), 1
        )

    def test_a_missing_file_fails_loudly(self):
        with self.assertRaises(SystemExit) as ctx:
            apply(Path(tempfile.mkdtemp()))
        self.assertIn("does not exist", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
