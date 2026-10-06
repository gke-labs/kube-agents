"""The audit file: where it is, one object per line, created on demand, locked, rotated at the cap,
written off an event loop, redacted by Hermes when present, and where a record goes when the file
cannot take it."""

import asyncio
import contextlib
import errno
import fcntl
import fnmatch
import io
import json
import logging
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import audit_sink  # noqa: E402

LOGGER = logging.getLogger("test.audit_sink")
# The two globs the sidecar tails, as basenames (buildFluentBitConfigMap).
SIDECAR_GLOBS = ("audit.jsonl", "*.log")
# A token shape Hermes' redactor knows and AuditRedactor does not.
GITLAB_TOKEN = "glpat-ABCDEFGHIJKLMNOPQRST"
GITLAB_TOKEN_MASKED = "glpat-...QRST"
# The modules the sink imports from a running Hermes.
HERMES_MODULES = ("hermes_constants", "agent", "agent.redact")


class SinkTestCase(unittest.TestCase):

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        env = mock.patch.dict(os.environ, {audit_sink.HERMES_HOME_ENV: str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        # These tests do not run inside Hermes; nothing may answer for it.
        previous = {name: sys.modules.pop(name, None) for name in HERMES_MODULES}
        self.addCleanup(self._restore_modules, previous)
        self.path = self.home / "logs" / "audit.jsonl"
        self.lock = self.home / "logs" / "audit.jsonl.lock"

    @staticmethod
    def _restore_modules(previous):
        for name, module in previous.items():
            sys.modules.pop(name, None)
            if module is not None:
                sys.modules[name] = module

    def fake_hermes(self, **attributes):
        sys.modules["hermes_constants"] = types.SimpleNamespace(**attributes)

    def fake_hermes_redactor(self):
        """Hermes' agent.redact, masking GITLAB_TOKEN; returns the `force` flags it was called with."""
        calls = []

        def redact_sensitive_text(text, *, force=False, **_):
            calls.append(force)
            return text.replace(GITLAB_TOKEN, GITLAB_TOKEN_MASKED)

        sys.modules["agent"] = types.ModuleType("agent")
        redact = types.ModuleType("agent.redact")
        redact.redact_sensitive_text = redact_sensitive_text
        sys.modules["agent.redact"] = redact
        return calls

    def lines(self):
        return self.path.read_text(encoding="utf-8").splitlines() if self.path.exists() else []

    def emit(self, record):
        """Emit inline and return what reached stdout."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertIsNone(audit_sink.emit(record, LOGGER))
        return out.getvalue()


class TestWhereTheFileIs(SinkTestCase):

    def test_under_the_profile_logs_directory(self):
        self.assertEqual(audit_sink.audit_file_path(), self.path)
        self.assertEqual(audit_sink.audit_file_path(Path("/opt/data/profiles/platform")),
                         Path("/opt/data/profiles/platform/logs/audit.jsonl"))
        self.assertEqual(audit_sink.lock_file_path(self.path), self.lock)

    def test_hermes_names_the_home_when_the_process_is_hermes(self):
        # Inside Hermes, get_hermes_home() carries the profile the gateway is
        # serving a turn for, which HERMES_HOME alone does not.
        served = self.home / "profiles" / "platform"
        self.fake_hermes(get_hermes_home=lambda: served)
        self.assertEqual(audit_sink.audit_file_path(), served / "logs" / "audit.jsonl")

    def test_a_failing_hermes_lookup_falls_back_to_the_environment(self):
        def broken():
            raise RuntimeError("no profile")

        self.fake_hermes(get_hermes_home=broken)
        self.assertEqual(audit_sink.audit_file_path(), self.path)

    def test_without_either_the_image_default_applies(self):
        with mock.patch.dict(os.environ, {audit_sink.HERMES_HOME_ENV: ""}):
            self.assertEqual(audit_sink.audit_file_path(), Path("/opt/data/logs/audit.jsonl"))


class TestAppending(SinkTestCase):

    def test_one_object_per_line_in_a_directory_created_on_demand(self):
        self.assertFalse(self.path.parent.exists())
        audit_sink.append_line(audit_sink.serialize({"b": 1, "a": "x\ny"}))
        audit_sink.append_line(audit_sink.serialize({"audit_event": "e"}))
        raw = self.path.read_text(encoding="utf-8")
        self.assertEqual(raw.count("\n"), 2)
        self.assertTrue(raw.endswith("\n"))
        first, second = raw.splitlines()
        self.assertEqual(first, '{"a": "x\\ny", "b": 1}')
        self.assertEqual(json.loads(first), {"a": "x\ny", "b": 1})
        self.assertEqual(json.loads(second), {"audit_event": "e"})

    def test_the_files_are_owner_writable_and_group_readable_only(self):
        previous = os.umask(0o002)
        self.addCleanup(os.umask, previous)
        audit_sink.append_line("{}")
        for path in (self.path, self.lock):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640, path)

    def test_the_lock_file_sits_beside_the_audit_file_outside_the_sidecar_globs(self):
        audit_sink.append_line("{}")
        self.assertTrue(self.lock.is_file())
        for pattern in SIDECAR_GLOBS:
            self.assertFalse(fnmatch.fnmatch(self.lock.name, pattern), pattern)
        self.assertTrue(fnmatch.fnmatch(self.path.name, SIDECAR_GLOBS[0]))

    def test_a_write_waits_for_a_lock_another_writer_releases_in_time(self):
        # Another process mid-rotation: it holds the lock, and this write waits
        # for it rather than racing it.
        self.path.parent.mkdir(parents=True)
        holder = os.open(self.lock, os.O_WRONLY | os.O_CREAT, 0o640)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX)
        writer = threading.Thread(target=audit_sink.append_line, args=("{}",))
        writer.start()
        time.sleep(0.2)
        self.assertEqual(self.lines(), [], "the write went ahead while another writer held the lock")
        fcntl.flock(holder, fcntl.LOCK_UN)
        writer.join(timeout=5)
        self.assertFalse(writer.is_alive())
        self.assertEqual(self.lines(), ["{}"])

    def test_a_lock_held_past_the_wait_is_given_up_on(self):
        self.path.parent.mkdir(parents=True)
        holder = os.open(self.lock, os.O_WRONLY | os.O_CREAT, 0o640)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX)
        with mock.patch.object(audit_sink, "LOCK_WAIT_SECONDS", 0.2):
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                audit_sink.append_line("{}")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(self.lines(), [])

    def test_a_write_that_fails_part_way_leaves_no_fragment(self):
        audit_sink.append_line('{"n": 1}')
        real_write = os.write
        calls = []

        def short_then_full(fd, data):
            calls.append(len(data))
            if len(calls) == 1:
                return real_write(fd, bytes(data[:3]))
            raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch.object(audit_sink.os, "write", side_effect=short_then_full):
            with self.assertRaises(OSError):
                audit_sink.append_line('{"n": 2}')
        self.assertEqual(self.path.read_text(encoding="utf-8"), '{"n": 1}\n', "a fragment survived")
        audit_sink.append_line('{"n": 3}')
        self.assertEqual(self.lines(), ['{"n": 1}', '{"n": 3}'])

    def test_hermes_makes_the_directory_when_the_process_is_hermes(self):
        made = []

        def mkdir_under_hermes_home(directory):
            made.append(Path(directory))
            Path(directory).mkdir(parents=True, exist_ok=True)
            return Path(directory)

        self.fake_hermes(get_hermes_home=lambda: self.home, mkdir_under_hermes_home=mkdir_under_hermes_home)
        audit_sink.append_line("{}")
        self.assertEqual(made, [self.path.parent])
        self.assertEqual(self.lines(), ["{}"])

    def test_an_unencodable_value_is_rendered_not_refused(self):
        line = audit_sink.serialize({"obj": object()})
        self.assertIn("<object object at", json.loads(line)["obj"])


class TestRedaction(SinkTestCase):

    def test_outside_hermes_the_line_is_written_as_it_is(self):
        # AuditRedactor is the emitters' layer over the fields; the sink adds
        # Hermes' layer only when Hermes is there to provide it.
        self.emit({"args": f"export GL={GITLAB_TOKEN}"})
        self.assertIn(GITLAB_TOKEN, self.lines()[0])

    def test_inside_hermes_the_line_passes_through_its_redactor_forced(self):
        calls = self.fake_hermes_redactor()
        self.emit({"args": f"export GL={GITLAB_TOKEN}"})
        self.assertEqual(json.loads(self.lines()[0]), {"agent_profile": "default", "args": f"export GL={GITLAB_TOKEN_MASKED}"})
        self.assertEqual(calls, [True], "security.redact_secrets: false must not reopen the trail")

    def test_the_stdout_fallback_is_redacted_too(self):
        self.fake_hermes_redactor()
        self.home.joinpath("logs").write_text("not a directory")
        with self.assertLogs(LOGGER, level="ERROR"):
            printed = self.emit({"args": f"export GL={GITLAB_TOKEN}"})
        self.assertEqual(json.loads(printed), {"agent_profile": "default", "args": f"export GL={GITLAB_TOKEN_MASKED}"})


class TestEmit(SinkTestCase):

    def test_the_record_goes_to_the_file_and_nowhere_else(self):
        with self.assertNoLogs(LOGGER, level="INFO"):
            printed = self.emit({"audit_event": "x", "n": 1})
        self.assertEqual(printed, "")
        self.assertEqual([json.loads(line) for line in self.lines()], [{"agent_profile": "default", "audit_event": "x", "n": 1}])

    def test_a_record_the_file_cannot_take_is_printed_to_stdout(self):
        self.home.joinpath("logs").write_text("not a directory")
        with self.assertLogs(LOGGER, level="ERROR") as captured:
            printed = self.emit({"audit_event": "x", "tool": "Bash"})
        # The record, whole, as the one line stdout receives.
        self.assertEqual(printed, '{"agent_profile": "default", "audit_event": "x", "tool": "Bash"}\n')
        # The notice names the file and the error and nothing of the record,
        # so the console's text-form query does not count it.
        notice = captured.output[0]
        self.assertIn(str(self.path), notice)
        self.assertIn("Not a directory", notice)
        self.assertNotIn("audit_event", notice)
        self.assertNotIn("Bash", notice)

    def test_a_lock_held_past_the_wait_sends_the_record_to_stdout(self):
        self.path.parent.mkdir(parents=True)
        holder = os.open(self.lock, os.O_WRONLY | os.O_CREAT, 0o640)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX)
        with mock.patch.object(audit_sink, "LOCK_WAIT_SECONDS", 0.2):
            with self.assertLogs(LOGGER, level="ERROR") as captured:
                printed = self.emit({"audit_event": "x"})
        self.assertEqual(printed, '{"agent_profile": "default", "audit_event": "x"}\n')
        self.assertIn("another writer has held", captured.output[0])
        self.assertEqual(self.lines(), [])

    def test_a_directory_hermes_refuses_sends_the_record_to_stdout(self):
        # What mkdir_under_hermes_home raises for a missing or tombstoned
        # named profile (hermes_constants.assert_named_profile_home_live).
        def refuse(directory):
            raise FileNotFoundError(f"Named profile home does not exist: {directory}")

        self.fake_hermes(get_hermes_home=lambda: self.home, mkdir_under_hermes_home=refuse)
        with self.assertLogs(LOGGER, level="ERROR") as captured:
            printed = self.emit({"audit_event": "x"})
        self.assertEqual(printed, '{"agent_profile": "default", "audit_event": "x"}\n')
        self.assertIn("Named profile home does not exist", captured.output[0])
        self.assertNotIn("audit_event", captured.output[0])
        self.assertFalse(self.path.parent.exists(), "the sink made the directory Hermes refused")

    def test_a_record_lost_to_both_is_said_so(self):
        self.home.joinpath("logs").write_text("not a directory")

        class Refusing(io.StringIO):
            def write(self, text):
                raise OSError("stdout is closed")

        with contextlib.redirect_stdout(Refusing()), self.assertLogs(LOGGER, level="ERROR") as captured:
            audit_sink.emit({"audit_event": "x"}, LOGGER)
        self.assertIn("the record is lost", captured.output[0])
        self.assertNotIn("audit_event", captured.output[0])


class TestProfileStamp(SinkTestCase):
    """Every emitted record names the profile whose file it lands in, so a tailed record is self-describing."""

    def test_a_named_profile_home_is_stamped_by_its_name(self):
        # Hermes serves a turn under a named profile; the record names it, so the
        # console reads the profile rather than the collector container it would
        # otherwise fall back to.
        served = self.home / "profiles" / "platform"
        self.fake_hermes(get_hermes_home=lambda: served)
        self.emit({"audit_event": "x"})
        record = json.loads((served / "logs" / "audit.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(record, {"agent_profile": "platform", "audit_event": "x"})

    def test_a_front_door_home_is_stamped_default(self):
        # The front door's home is the hermes home itself, not a profiles/<name>
        # child, so its records are the default profile's.
        self.emit({"audit_event": "x"})
        record = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(record, {"agent_profile": "default", "audit_event": "x"})


class TestWriterThread(SinkTestCase):
    """A caller on an event loop gets its write done on the writer thread, in order."""

    def spy_on_writes(self):
        threads = []
        real = audit_sink.append_line

        def spy(line, path=None):
            threads.append(threading.current_thread())
            return real(line, path)

        patch = mock.patch.object(audit_sink, "append_line", side_effect=spy)
        patch.start()
        self.addCleanup(patch.stop)
        return threads

    def test_a_call_on_an_event_loop_is_written_off_it_in_order(self):
        threads = self.spy_on_writes()

        async def on_loop():
            for n in range(3):
                self.assertIsNotNone(audit_sink.emit({"n": n}, LOGGER), "written inline on the loop")

        asyncio.run(on_loop())
        audit_sink.flush(timeout=5)
        self.assertEqual([json.loads(line)["n"] for line in self.lines()], [0, 1, 2])
        self.assertEqual(len(threads), 3)
        for thread in threads:
            self.assertIsNot(thread, threading.current_thread())
            self.assertTrue(thread.name.startswith(audit_sink.WRITER_THREAD_NAME), thread.name)

    def test_a_call_off_the_loop_is_written_inline(self):
        threads = self.spy_on_writes()
        self.assertIsNone(audit_sink.emit({"n": 1}, LOGGER))
        self.assertEqual(threads, [threading.current_thread()])
        self.assertEqual(len(self.lines()), 1)

    def test_the_profile_is_resolved_on_the_calling_thread(self):
        # The per-turn override is context-local to the loop thread; the writer
        # thread would resolve the default home instead.
        served = self.home / "profiles" / "platform"
        caller = threading.current_thread()
        self.fake_hermes(
            get_hermes_home=lambda: served if threading.current_thread() is caller else self.home
        )

        async def on_loop():
            audit_sink.emit({"n": 1}, LOGGER)

        asyncio.run(on_loop())
        audit_sink.flush(timeout=5)
        self.assertTrue((served / "logs" / "audit.jsonl").is_file())
        self.assertFalse(self.path.exists())

    def test_the_stdout_fallback_works_from_the_writer_thread(self):
        self.home.joinpath("logs").write_text("not a directory")
        out = io.StringIO()

        async def on_loop():
            audit_sink.emit({"audit_event": "x"}, LOGGER)

        with contextlib.redirect_stdout(out), self.assertLogs(LOGGER, level="ERROR") as captured:
            asyncio.run(on_loop())
            audit_sink.flush(timeout=5)
        self.assertEqual(out.getvalue(), '{"agent_profile": "default", "audit_event": "x"}\n')
        self.assertIn(str(self.path), captured.output[0])

    def _reset_writer(self):
        with audit_sink._writer_lock:
            writer, audit_sink._writer = audit_sink._writer, None
            audit_sink._pending_writes = 0
        if writer is not None:
            writer.shutdown(wait=False)

    def test_a_stalled_writer_spills_to_stdout_rather_than_queueing_without_bound(self):
        # A write that blocks rather than fails pins the single writer thread.
        # Without a cap every later event queues in memory unbounded and reaches
        # neither the file nor the stdout fallback _write takes only on an error.
        self._reset_writer()
        self.addCleanup(self._reset_writer)
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        real = audit_sink.append_line

        def stall(line, path=None):
            started.set()
            release.wait(timeout=10)
            return real(line, path)

        patch = mock.patch.object(audit_sink, "append_line", side_effect=stall)
        patch.start()
        self.addCleanup(patch.stop)

        cap = 3
        out = io.StringIO()
        with mock.patch.object(audit_sink, "MAX_PENDING_WRITES", cap):

            async def on_loop():
                # The first emit pins the writer thread; wait until it is in the
                # stalled write before filling the queue behind it.
                self.assertIsNotNone(audit_sink.emit({"n": 0}, LOGGER))
                self.assertTrue(started.wait(timeout=5))
                for n in range(1, 2 * cap):
                    audit_sink.emit({"n": n}, LOGGER)

            with contextlib.redirect_stdout(out), self.assertLogs(LOGGER, level="ERROR") as captured:
                asyncio.run(on_loop())
                self.assertLessEqual(audit_sink._pending_writes, cap, "the queue grew past the cap")
                release.set()
                audit_sink.flush(timeout=10)

        filed = sorted(json.loads(line)["n"] for line in self.lines())
        spilled = sorted(json.loads(line)["n"] for line in out.getvalue().splitlines())
        # None lost: the first `cap` on the file, the rest on stdout.
        self.assertEqual(sorted(filed + spilled), list(range(2 * cap)))
        self.assertEqual(len(filed), cap)
        self.assertEqual(len(spilled), cap, "records past the cap were queued instead of spilled to stdout")
        # Each spill logged an ERROR naming the backlog, never the record.
        self.assertTrue(any("backed up" in line for line in captured.output))
        for line in captured.output:
            self.assertNotIn('"n"', line)
        self.assertEqual(audit_sink._pending_writes, 0, "the backlog did not drain")


class TestRotation(SinkTestCase):

    LINE = "x" * 40  # 41 bytes with its newline

    def setUp(self):
        super().setUp()
        cap = mock.patch.object(audit_sink, "AUDIT_FILE_MAX_BYTES", 64)
        cap.start()
        self.addCleanup(cap.stop)

    def rotated(self, index):
        return self.path.with_name(f"{self.path.name}.{index}")

    def test_the_file_is_rotated_at_the_cap_and_the_backups_are_bounded(self):
        audit_sink.append_line(self.LINE)
        self.assertFalse(self.rotated(1).exists())
        # A second line would pass the cap, so the first is moved aside first.
        audit_sink.append_line(self.LINE + "2")
        self.assertEqual(self.lines(), [self.LINE + "2"])
        self.assertEqual(self.rotated(1).read_text(encoding="utf-8"), self.LINE + "\n")
        for suffix in ("3", "4", "5"):
            audit_sink.append_line(self.LINE + suffix)
        self.assertEqual(self.lines(), [self.LINE + "5"])
        self.assertEqual(self.rotated(1).read_text(encoding="utf-8"), self.LINE + "4\n")
        self.assertEqual(self.rotated(2).read_text(encoding="utf-8"), self.LINE + "3\n")
        self.assertEqual(self.rotated(3).read_text(encoding="utf-8"), self.LINE + "2\n")
        self.assertFalse(self.rotated(4).exists(), "more backups than AUDIT_FILE_BACKUP_COUNT")

    def test_a_single_line_over_the_cap_is_still_written(self):
        audit_sink.append_line("y" * 100)
        self.assertEqual(self.lines(), ["y" * 100])
        self.assertFalse(self.rotated(1).exists())

    def test_a_backup_that_cannot_be_moved_does_not_cost_the_record(self):
        audit_sink.append_line(self.LINE)
        with mock.patch.object(audit_sink.os, "replace", side_effect=PermissionError("read-only")):
            audit_sink.append_line(self.LINE + "2")
        self.assertEqual(self.lines(), [self.LINE, self.LINE + "2"])


if __name__ == "__main__":
    unittest.main()
