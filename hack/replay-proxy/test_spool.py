"""The proxy's segment log and data budget: rotation, recovery, failed writes, the budget stop."""

import errno
import os
import shutil
import tempfile
import unittest
from unittest import mock

import spool

PREFIX = "pod-a-1700000000000"
# Long enough that only the fake clock, never real time, closes a segment.
AGE_SECONDS = 60.0
# A budget re-measured only when a test calls _measure() itself.
NEVER_SECONDS = 3600.0


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class TempDirTestCase(unittest.TestCase):
    def tmpdir(self):
        path = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path

    @staticmethod
    def names(directory):
        return sorted(os.listdir(directory))

    @staticmethod
    def read(directory, name):
        with open(os.path.join(directory, name), "rb") as handle:
            return handle.read()


class SegmentRotationTest(TempDirTestCase):
    def setUp(self):
        self.dir = self.tmpdir()
        self.clock = FakeClock()

    def log(self, **kwargs):
        log = spool.SegmentLog(self.dir, PREFIX, AGE_SECONDS, clock=self.clock, **kwargs)
        self.addCleanup(log.close)
        return log

    def test_a_line_is_on_disk_in_the_open_segment_at_once(self):
        log = self.log()
        self.assertTrue(log.write('{"n": 1}'))
        self.assertEqual(self.names(self.dir), [f"{PREFIX}-000001.jsonl.open"])
        self.assertEqual(self.read(self.dir, f"{PREFIX}-000001.jsonl.open"), b'{"n": 1}\n')

    def test_closed_by_age(self):
        log = self.log()
        log.write("a")
        self.clock.now += AGE_SECONDS - 1
        log.roll_if_due()
        self.assertEqual(self.names(self.dir), [f"{PREFIX}-000001.jsonl.open"])
        self.clock.now += 1
        log.roll_if_due()
        self.assertEqual(self.names(self.dir), [f"{PREFIX}-000001.jsonl"])
        log.write("b")
        self.assertEqual(self.names(self.dir), [f"{PREFIX}-000001.jsonl", f"{PREFIX}-000002.jsonl.open"])
        self.assertEqual(self.read(self.dir, f"{PREFIX}-000001.jsonl"), b"a\n")

    def test_age_is_counted_from_the_first_line_not_from_start(self):
        log = self.log()
        self.clock.now += AGE_SECONDS * 2
        log.roll_if_due()  # nothing open: nothing to close, nothing created
        self.assertEqual(self.names(self.dir), [])
        log.write("a")
        log.roll_if_due()
        self.assertEqual(self.names(self.dir), [f"{PREFIX}-000001.jsonl.open"])

    def test_closed_by_size(self):
        log = self.log(max_bytes=10)
        log.write("12345")  # 6 bytes
        self.assertEqual(self.names(self.dir), [f"{PREFIX}-000001.jsonl.open"])
        log.write("1234")  # 11 bytes: at or past the cap, closed after the line
        self.assertEqual(self.names(self.dir), [f"{PREFIX}-000001.jsonl"])
        self.assertEqual(self.read(self.dir, f"{PREFIX}-000001.jsonl"), b"12345\n1234\n")
        log.write("x")
        self.assertIn(f"{PREFIX}-000002.jsonl.open", self.names(self.dir))

    def test_close_closes_the_open_segment(self):
        log = self.log()
        log.write("a")
        log.close()
        self.assertEqual(self.names(self.dir), [f"{PREFIX}-000001.jsonl"])

    def test_segment_is_owner_only(self):
        log = self.log()
        log.write("a")
        mode = os.stat(os.path.join(self.dir, f"{PREFIX}-000001.jsonl.open")).st_mode & 0o777
        self.assertEqual(mode, spool.SEGMENT_FILE_MODE)

    def test_prefix_is_pod_and_start_ms_with_unsafe_characters_replaced(self):
        self.assertEqual(spool.segment_prefix("pod/a b", 1.5), "pod_a_b-1500")


class RecoveryTest(TempDirTestCase):
    def setUp(self):
        self.dir = self.tmpdir()

    def put(self, name, data):
        with open(os.path.join(self.dir, name), "wb") as handle:
            handle.write(data)

    def test_open_segment_is_closed_cut_to_its_last_whole_line(self):
        self.put("old-1-000003.jsonl.open", b'{"a": 1}\n{"b": 2}\n{"c": ')
        log = spool.SegmentLog(self.dir, PREFIX, AGE_SECONDS)
        self.addCleanup(log.close)
        self.assertEqual(log.recovered, 1)
        self.assertEqual(self.names(self.dir), ["old-1-000003.jsonl"])
        self.assertEqual(self.read(self.dir, "old-1-000003.jsonl"), b'{"a": 1}\n{"b": 2}\n')

    def test_open_segment_with_no_whole_line_is_removed(self):
        self.put("old-1-000001.jsonl.open", b'{"partial')
        self.put("old-1-000002.jsonl.open", b"")
        log = spool.SegmentLog(self.dir, PREFIX, AGE_SECONDS)
        self.addCleanup(log.close)
        self.assertEqual(log.recovered, 0)
        self.assertEqual(self.names(self.dir), [])

    def test_closed_segments_and_other_files_are_left_alone(self):
        self.put("old-1-000001.jsonl", b"closed\n")
        self.put("notes.txt.open", b"not ours")
        log = spool.SegmentLog(self.dir, PREFIX, AGE_SECONDS)
        self.addCleanup(log.close)
        self.assertEqual(self.names(self.dir), ["notes.txt.open", "old-1-000001.jsonl"])
        self.assertEqual(self.read(self.dir, "old-1-000001.jsonl"), b"closed\n")

    def test_a_segment_that_cannot_be_renamed_is_left_and_the_rest_recovered(self):
        self.put("old-1-000001.jsonl.open", b"a\n")
        self.put("old-1-000002.jsonl.open", b"b\n")
        real_replace = os.replace

        def flaky(src, dst):
            if src.endswith("000001.jsonl.open"):
                raise OSError(errno.EACCES, "denied")
            return real_replace(src, dst)

        with mock.patch.object(spool.os, "replace", side_effect=flaky):
            log = spool.SegmentLog(self.dir, PREFIX, AGE_SECONDS)
        self.addCleanup(log.close)
        self.assertEqual(log.recovered, 1)
        self.assertEqual(self.names(self.dir), ["old-1-000001.jsonl.open", "old-1-000002.jsonl"])


class WriteFailureTest(TempDirTestCase):
    def setUp(self):
        self.dir = self.tmpdir()
        self.log = spool.SegmentLog(self.dir, PREFIX, AGE_SECONDS)
        self.addCleanup(self.log.close)

    def test_failed_write_is_cut_back_and_the_segment_closed(self):
        self.log.write("good")
        real_write = os.write

        def half_then_fail(fd, data):
            # Part of the line reaches the file, then the disk fills.
            real_write(fd, bytes(data[:3]))
            raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch.object(spool.os, "write", side_effect=half_then_fail):
            with self.assertRaises(OSError):
                self.log.write("this line is lost")
        self.assertEqual(self.names(self.dir), [f"{PREFIX}-000001.jsonl"])
        self.assertEqual(self.read(self.dir, f"{PREFIX}-000001.jsonl"), b"good\n")
        # The next line starts a fresh segment.
        self.assertTrue(self.log.write("next"))
        self.assertEqual(self.read(self.dir, f"{PREFIX}-000002.jsonl.open"), b"next\n")

    def test_failed_first_write_leaves_no_file(self):
        with mock.patch.object(spool.os, "write", side_effect=OSError(errno.ENOSPC, "full")):
            with self.assertRaises(OSError):
                self.log.write("lost")
        self.assertEqual(self.names(self.dir), [])

    def test_failed_truncate_leaves_the_open_segment_for_the_next_start(self):
        self.log.write("good")
        real_write = os.write

        def half_then_fail(fd, data):
            real_write(fd, bytes(data[:3]))
            raise OSError(errno.EIO, "io")

        with mock.patch.object(spool.os, "write", side_effect=half_then_fail), \
                mock.patch.object(spool.os, "ftruncate", side_effect=OSError(errno.EIO, "io")):
            with self.assertRaises(OSError):
                self.log.write("partial")
        name = f"{PREFIX}-000001.jsonl.open"
        self.assertEqual(self.names(self.dir), [name])
        # A restart closes it at its last whole line.
        again = spool.SegmentLog(self.dir, "pod-a-2", AGE_SECONDS)
        self.addCleanup(again.close)
        self.assertEqual(self.read(self.dir, f"{PREFIX}-000001.jsonl"), b"good\n")

    def test_short_writes_are_completed(self):
        real_write = os.write

        def one_byte(fd, data):
            return real_write(fd, bytes(data[:1]))

        with mock.patch.object(spool.os, "write", side_effect=one_byte):
            self.log.write("abc")
        self.log.close()
        self.assertEqual(self.read(self.dir, f"{PREFIX}-000001.jsonl"), b"abc\n")


class DataBudgetTest(TempDirTestCase):
    def budget(self, roots, max_bytes):
        budget = spool.DataBudget(roots, max_bytes, NEVER_SECONDS)
        self.addCleanup(budget.close)
        return budget

    def test_measures_the_files_under_every_root(self):
        a, b = self.tmpdir(), self.tmpdir()
        os.makedirs(os.path.join(a, "deep"))
        with open(os.path.join(a, "deep", "x"), "wb") as handle:
            handle.write(b"x" * 10)
        with open(os.path.join(b, "y"), "wb") as handle:
            handle.write(b"y" * 5)
        self.assertEqual(spool.tree_bytes([a, b, "", os.path.join(a, "missing")]), 15)
        self.assertEqual(self.budget([a, b], 100).used(), 15)

    def test_writes_count_between_measurements_and_the_budget_stops_them(self):
        budget = self.budget([self.tmpdir()], 10)
        self.assertTrue(budget.allows())
        budget.note(9)
        self.assertTrue(budget.allows())
        budget.note(1)
        self.assertFalse(budget.allows())

    def test_segment_log_stops_once_spent_and_resumes_when_room_returns(self):
        logs = self.tmpdir()
        budget = self.budget([logs], 8)
        log = spool.SegmentLog(logs, PREFIX, AGE_SECONDS, budget)
        self.addCleanup(log.close)
        self.assertTrue(log.write("1234567"))  # 8 bytes: spent
        self.assertFalse(log.write("dropped"))
        self.assertEqual(self.read(logs, f"{PREFIX}-000001.jsonl.open"), b"1234567\n")
        # The collector empties what it has uploaded; the next measurement sees it.
        log.close()
        os.truncate(os.path.join(logs, f"{PREFIX}-000001.jsonl"), 0)
        with budget._lock:
            budget._used = spool.tree_bytes(budget.roots)
        self.assertTrue(log.write("again"))

    def test_close_stops_the_measuring_thread(self):
        budget = spool.DataBudget([self.tmpdir()], 1, NEVER_SECONDS)
        budget.close()
        self.assertFalse(budget._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
