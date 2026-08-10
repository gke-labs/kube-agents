"""Unit tests for the kanban wake nudge installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import ast
import asyncio
import logging
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from apply_kanban_wake_nudge import (
    CREATE_ANCHOR,
    DB_RELATIVE,
    DISPATCHER_SLEEP_ANCHOR,
    NOTIFIER_SLEEP_ANCHOR,
    UNBLOCK_ANCHOR,
    WATCHERS_RELATIVE,
    apply,
)
from kanban_wake_nudge import (
    WakeMonitor,
    record_wake,
    record_wake_for_conn,
    wait_interval,
    wake_path,
)
import kanban_wake_nudge

LOGGER_NAME = kanban_wake_nudge.logger.name


def tmp_db_path():
    return Path(tempfile.mkdtemp()) / "kanban.db"


class RecordWakeTest(unittest.TestCase):
    def test_the_wake_file_is_derived_from_the_db_path(self):
        db = tmp_db_path()
        self.assertEqual(wake_path(db), str(db) + ".wake")
        self.assertTrue(record_wake(db))
        self.assertTrue(Path(str(db) + ".wake").exists())

    def test_two_nudges_produce_distinct_mtimes(self):
        # The whole edge-detection scheme rests on this: mtime_ns is set
        # explicitly from time.time_ns(), not left to filesystem granularity.
        db = tmp_db_path()
        record_wake(db)
        first = os.stat(wake_path(db)).st_mtime_ns
        record_wake(db)
        second = os.stat(wake_path(db)).st_mtime_ns
        self.assertNotEqual(first, second)

    def test_an_unwritable_location_degrades_instead_of_raising(self):
        # The nudge rides on the tail of a committed board write; a read-only
        # filesystem must cost the nudge, never the write.
        missing = Path(tempfile.mkdtemp()) / "no" / "such" / "dir" / "kanban.db"
        self.assertFalse(record_wake(missing))

    def test_a_hostile_path_type_degrades_instead_of_raising(self):
        self.assertFalse(record_wake(None))


class RecordWakeForConnTest(unittest.TestCase):
    def test_the_nudge_lands_next_to_the_file_the_connection_writes(self):
        db = tmp_db_path()
        conn = sqlite3.connect(db)
        try:
            self.assertTrue(record_wake_for_conn(conn))
        finally:
            conn.close()
        self.assertTrue(Path(wake_path(db)).exists())

    def test_row_factory_does_not_break_the_pragma_read(self):
        db = tmp_db_path()
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row  # what kanban_db.connect() sets
        try:
            self.assertTrue(record_wake_for_conn(conn))
        finally:
            conn.close()
        self.assertTrue(Path(wake_path(db)).exists())

    def test_an_in_memory_db_has_no_file_and_degrades(self):
        conn = sqlite3.connect(":memory:")
        try:
            self.assertFalse(record_wake_for_conn(conn))
        finally:
            conn.close()

    def test_a_closed_connection_degrades_instead_of_raising(self):
        conn = sqlite3.connect(":memory:")
        conn.close()
        self.assertFalse(record_wake_for_conn(conn))

    def test_a_non_connection_degrades_instead_of_raising(self):
        self.assertFalse(record_wake_for_conn(object()))


class WakeMonitorTest(unittest.TestCase):
    def test_quiet_board_means_no_change(self):
        db = tmp_db_path()
        record_wake(db)
        monitor = WakeMonitor(db)
        self.assertFalse(monitor.changed())
        self.assertFalse(monitor.changed())

    def test_history_from_before_construction_is_not_replayed(self):
        db = tmp_db_path()
        record_wake(db)
        self.assertFalse(WakeMonitor(db).changed())

    def test_a_nudge_is_seen_exactly_once(self):
        db = tmp_db_path()
        monitor = WakeMonitor(db)
        record_wake(db)
        self.assertTrue(monitor.changed())
        self.assertFalse(monitor.changed(), "the edge must be consumed")

    def test_the_first_nudge_ever_creates_the_file_and_is_seen(self):
        db = tmp_db_path()
        monitor = WakeMonitor(db)  # wake file does not exist yet
        record_wake(db)
        self.assertTrue(monitor.changed())

    def test_a_burst_of_nudges_costs_one_early_scan(self):
        db = tmp_db_path()
        monitor = WakeMonitor(db)
        for _ in range(5):
            record_wake(db)
        self.assertTrue(monitor.changed())
        self.assertFalse(monitor.changed())

    def test_a_missing_wake_file_reads_as_quiet(self):
        monitor = WakeMonitor(tmp_db_path())
        self.assertFalse(monitor.changed())

    def test_a_callable_path_is_resolved_lazily_and_fail_soft(self):
        db = tmp_db_path()
        monitor = WakeMonitor(lambda: db)
        record_wake(db)
        self.assertTrue(monitor.changed())

    def test_a_raising_resolver_degrades_the_monitor_not_the_caller(self):
        def resolver():
            raise RuntimeError("board root unreadable")

        with self.assertLogs(LOGGER_NAME, level=logging.WARNING):
            monitor = WakeMonitor(resolver)
        self.assertFalse(monitor.changed())
        self.assertFalse(monitor.changed())

    def test_a_monitor_born_inert_says_why(self):
        # The one failure here worth a log line. A resolver that raises leaves
        # this monitor dead for the lifetime of the watcher loop that built it,
        # so wait_interval silently becomes the 5s sleep it replaced and the
        # only symptom is the per-hop latency the patch removed. Without this
        # line an operator sees the old numbers and no cause.
        def resolver():
            raise RuntimeError("HERMES_KANBAN_DB points at a missing volume")

        with self.assertLogs(LOGGER_NAME, level=logging.WARNING) as captured:
            WakeMonitor(resolver)
        joined = "\n".join(captured.output)
        self.assertIn("missing volume", joined, "the cause must survive")
        self.assertIn("polling", joined, "the consequence must be stated")

    def test_a_working_monitor_logs_nothing(self):
        # Both watcher loops build one at startup; a warning on the healthy
        # path would be noise on every gateway boot.
        logging.getLogger(LOGGER_NAME).warning("probe")
        with self.assertLogs(LOGGER_NAME, level=logging.WARNING) as captured:
            logging.getLogger(LOGGER_NAME).warning("probe")
            WakeMonitor(tmp_db_path())
        self.assertEqual(len(captured.output), 1)

    def test_an_unstattable_wake_file_stays_silent(self):
        # Deliberately unlike the constructor: _stat runs once per 0.25s slice
        # in both loops, so logging a missing wake file would cost eight lines
        # a second forever. An absent file simply reads as quiet.
        logging.getLogger(LOGGER_NAME).warning("probe")
        monitor = WakeMonitor(tmp_db_path())
        with self.assertLogs(LOGGER_NAME, level=logging.WARNING) as captured:
            logging.getLogger(LOGGER_NAME).warning("probe")
            for _ in range(10):
                monitor.changed()
        self.assertEqual(len(captured.output), 1)


def run(coro):
    return asyncio.run(coro)


class WaitIntervalTest(unittest.TestCase):
    def test_a_nudge_breaks_the_wait_early(self):
        db = tmp_db_path()
        monitor = WakeMonitor(db)
        record_wake(db)
        start = time.monotonic()
        self.assertTrue(run(wait_interval(monitor, 5.0)))
        # <=0.25s reaction promised; allow slack for a slow CI box.
        self.assertLess(time.monotonic() - start, 1.0)

    def test_a_quiet_board_waits_the_full_interval(self):
        db = tmp_db_path()
        monitor = WakeMonitor(db)
        start = time.monotonic()
        self.assertTrue(run(wait_interval(monitor, 0.4)))
        self.assertGreaterEqual(time.monotonic() - start, 0.35)

    def test_shutdown_returns_false_immediately(self):
        start = time.monotonic()
        self.assertFalse(run(wait_interval(WakeMonitor(tmp_db_path()), 30.0, lambda: False)))
        self.assertLess(time.monotonic() - start, 0.5)

    def test_a_nudge_mid_wait_is_noticed_within_a_slice(self):
        db = tmp_db_path()
        monitor = WakeMonitor(db)

        async def scenario():
            async def nudge_later():
                await asyncio.sleep(0.1)
                record_wake(db)

            task = asyncio.ensure_future(nudge_later())
            start = time.monotonic()
            woke = await wait_interval(monitor, 5.0)
            await task
            return woke, time.monotonic() - start

        woke, elapsed = run(scenario())
        self.assertTrue(woke)
        self.assertLess(elapsed, 1.5)

    def test_no_monitor_is_exactly_the_upstream_sleep(self):
        start = time.monotonic()
        self.assertTrue(run(wait_interval(None, 0.3)))
        self.assertGreaterEqual(time.monotonic() - start, 0.25)

    def test_a_raising_monitor_is_exactly_the_upstream_sleep(self):
        class Broken:
            def changed(self):
                raise OSError("stat storm")

        start = time.monotonic()
        self.assertTrue(run(wait_interval(Broken(), 0.3)))
        self.assertGreaterEqual(time.monotonic() - start, 0.25)

    def test_a_garbage_interval_falls_back_rather_than_raising(self):
        db = tmp_db_path()
        monitor = WakeMonitor(db)
        record_wake(db)
        self.assertTrue(run(wait_interval(monitor, "not a number")))

    def test_a_zero_interval_cannot_become_a_hot_loop(self):
        start = time.monotonic()
        self.assertTrue(run(wait_interval(WakeMonitor(tmp_db_path()), 0)))
        self.assertGreaterEqual(time.monotonic() - start, 0.2)


# The producers, reduced to the three regions the patch rewrites.
UPSTREAM_DB = '''\
def create_task(conn, **kwargs):
    for attempt in range(2):
        task_id = _new_task_id()
        try:
            with write_txn(conn):
                do_inserts()
                _inherit_notify_subs(conn, task_id, parents, created_at=now)
            return task_id
        except IntegrityError:
            continue
    raise RuntimeError("unreachable")


def complete_task(conn, task_id):
    _clear_failure_counter(conn, task_id)
    # Recompute ready status for dependents (separate txn so children see done).
    recompute_ready(conn)
    _cleanup_workspace(conn, task_id)
    return True


def unblock_task(conn, task_id):
    with write_txn(conn):
        cur = conn.execute("UPDATE tasks SET status = ?", ("ready",))
        if cur.rowcount != 1:
            return False
        _append_event(
            conn, task_id, "unblocked",
            {"status": new_status} if new_status != "ready" else None,
        )
        return True
'''

# The consumers, reduced to the two loops the patch rewrites.
UPSTREAM_WATCHERS = '''\
class GatewayKanbanWatchersMixin:
    async def _kanban_notifier_watcher(self, interval: float = 5.0) -> None:
        # Initial delay so the gateway can finish wiring adapters.
        await asyncio.sleep(5)

        while self._running:
            try:
                deliveries = await asyncio.to_thread(collect)
            except Exception as exc:
                logger.warning("kanban notifier tick failed: %s", exc)
            # Sleep with cancellation checks.
            for _ in range(int(max(1, interval))):
                if not self._running:
                    return
                await asyncio.sleep(1)

    async def _kanban_dispatcher_watcher(self) -> None:
        logger.info(
            "kanban dispatcher: embedded in gateway (interval=%.1fs)", interval
        )
        while self._running:
            try:
                results = await asyncio.to_thread(tick)
            except Exception:
                logger.exception("kanban dispatcher: unexpected watcher error")

            # Sleep in 1s slices so shutdown is snappy — otherwise a stop()
            # waits up to `interval` seconds for the current sleep to finish.
            slept = 0.0
            while slept < interval and self._running:
                await asyncio.sleep(min(1.0, interval - slept))
                slept += 1.0

        self._release_kanban_dispatcher_lock()
'''


def patch_tree(db_source=UPSTREAM_DB, watchers_source=UPSTREAM_WATCHERS):
    """Write miniature sources under a temp root, patch, and return the texts."""
    root = Path(tempfile.mkdtemp())
    db_target = root / DB_RELATIVE
    db_target.parent.mkdir(parents=True)
    db_target.write_text(db_source)
    watchers_target = root / WATCHERS_RELATIVE
    watchers_target.parent.mkdir(parents=True)
    watchers_target.write_text(watchers_source)
    apply(root)
    return db_target.read_text(), watchers_target.read_text()


class ApplyTest(unittest.TestCase):
    def test_every_anchor_matches_the_miniature_upstream_exactly_once(self):
        for anchor in (CREATE_ANCHOR, UNBLOCK_ANCHOR):
            self.assertEqual(UPSTREAM_DB.count(anchor), 1, anchor)
        for anchor in (NOTIFIER_SLEEP_ANCHOR, DISPATCHER_SLEEP_ANCHOR):
            self.assertEqual(UPSTREAM_WATCHERS.count(anchor), 1, anchor)

    def test_all_three_producers_gain_a_nudge(self):
        db, _ = patch_tree()
        self.assertEqual(db.count("_kanban_record_wake(conn)"), 3)

    def test_the_unblock_nudge_lands_after_the_commit(self):
        # The dedent is the point of that edit: a wake recorded inside the
        # txn fires before the commit is visible and burns the edge. At
        # function level the nudge runs after write_txn's __exit__ committed.
        db, _ = patch_tree()
        lines = db.splitlines()
        exits = [
            i
            for i, line in enumerate(lines[:-1])
            if line == "    _kanban_record_wake(conn)"
            and lines[i + 1] == "    return True"
        ]
        self.assertEqual(
            len(exits), 1, "the unblock nudge must sit at function level, pre-return"
        )
        self.assertNotIn(
            "        _kanban_record_wake(conn)\n        return True",
            db,
            "the nudge must not remain inside the unblock write txn",
        )

    def test_both_watcher_loops_get_a_monitor_and_a_wake_aware_wait(self):
        _, watchers = patch_tree()
        self.assertEqual(
            watchers.count("_wake_monitor = _KanbanWakeMonitor(_kb.kanban_db_path)"), 2
        )
        self.assertEqual(watchers.count("await _kanban_wait_interval("), 2)
        # The notifier's shutdown `return` must survive the swap.
        self.assertIn("            ):\n                return", watchers)

    def test_the_import_trailers_are_appended(self):
        db, watchers = patch_tree()
        self.assertIn(
            "from hermes_cli.kanban_wake_nudge import (  # noqa: E402", db
        )
        self.assertIn("record_wake_for_conn as _kanban_record_wake", db)
        self.assertIn("WakeMonitor as _KanbanWakeMonitor", watchers)
        self.assertIn("wait_interval as _kanban_wait_interval", watchers)

    def test_both_patched_files_still_parse(self):
        db, watchers = patch_tree()
        ast.parse(db)
        ast.parse(watchers)

    def test_a_drifted_anchor_fails_loudly_and_names_the_edit(self):
        drifted = UPSTREAM_WATCHERS.replace("await asyncio.sleep(1)", "await asyncio.sleep(2)")
        with self.assertRaises(SystemExit) as ctx:
            patch_tree(watchers_source=drifted)
        self.assertIn("notifier sleep", str(ctx.exception))
        self.assertIn("found 0", str(ctx.exception))

    def test_applying_twice_fails_rather_than_silently_no_opping(self):
        root = Path(tempfile.mkdtemp())
        (root / DB_RELATIVE).parent.mkdir(parents=True)
        (root / DB_RELATIVE).write_text(UPSTREAM_DB)
        (root / WATCHERS_RELATIVE).parent.mkdir(parents=True)
        (root / WATCHERS_RELATIVE).write_text(UPSTREAM_WATCHERS)
        apply(root)
        with self.assertRaises(SystemExit):
            apply(root)

    def test_a_missing_file_fails_loudly(self):
        with self.assertRaises(SystemExit) as ctx:
            apply(Path(tempfile.mkdtemp()))
        self.assertIn("does not exist", str(ctx.exception))


class PatchedProducerBehaviourTest(unittest.TestCase):
    """Execute the patched miniature producers against a real sqlite file.

    The build-time gate (verify_kanban_wake_nudge.py) drives the real engine;
    this asserts the same wiring holds host-side with no Hermes install: the
    nudge lands next to the DB the connection wrote, only after the commit.
    """

    def _load_patched_db_module(self):
        db_text, _ = patch_tree()
        # Strip the real trailer import and inject the local module instead —
        # hermes_cli does not exist host-side.
        db_text = db_text.split("\n\n# kube-agents patch:")[0]
        namespace = {
            "_kanban_record_wake": record_wake_for_conn,
            "_new_task_id": lambda: "t_test",
            "_inherit_notify_subs": lambda *a, **k: None,
            "_clear_failure_counter": lambda *a, **k: None,
            "_cleanup_workspace": lambda *a, **k: None,
            "recompute_ready": lambda conn: None,
            "_append_event": lambda *a, **k: None,
            "do_inserts": lambda: None,
            "IntegrityError": sqlite3.IntegrityError,
            "parents": (),
            "now": 0,
            "new_status": "ready",
        }
        import contextlib

        @contextlib.contextmanager
        def write_txn(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

        namespace["write_txn"] = write_txn
        exec(compile(db_text, "patched_kanban_db_miniature", "exec"), namespace)
        return namespace

    def _conn(self, db):
        conn = sqlite3.connect(db)
        conn.isolation_level = None  # autocommit + explicit BEGIN, as upstream
        conn.execute("CREATE TABLE tasks (status TEXT)")
        conn.execute("INSERT INTO tasks VALUES ('blocked')")
        return conn

    def test_create_complete_and_unblock_each_record_a_wake(self):
        ns = self._load_patched_db_module()
        db = tmp_db_path()
        conn = self._conn(db)
        try:
            monitor = WakeMonitor(db)
            self.assertEqual(ns["create_task"](conn), "t_test")
            self.assertTrue(monitor.changed(), "create_task must nudge")
            self.assertTrue(ns["complete_task"](conn, "t_test"))
            self.assertTrue(monitor.changed(), "complete_task must nudge")
            self.assertTrue(ns["unblock_task"](conn, "t_test"))
            self.assertTrue(monitor.changed(), "unblock_task must nudge")
        finally:
            conn.close()

    def test_a_failed_unblock_does_not_nudge(self):
        ns = self._load_patched_db_module()
        db = tmp_db_path()
        conn = self._conn(db)
        conn.execute("DELETE FROM tasks")  # rowcount will be 0
        try:
            monitor = WakeMonitor(db)
            self.assertFalse(ns["unblock_task"](conn, "t_test"))
            self.assertFalse(monitor.changed(), "a no-op unblock must stay quiet")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
