"""Tests for shell-memory-guard.py: the counters it reads, the process it picks, and when it acts."""

import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

GUARD_PATH = Path(__file__).with_name("shell-memory-guard.py")
_spec = importlib.util.spec_from_file_location("shell_memory_guard", GUARD_PATH)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

MIB = 1024 * 1024
GIB = 1024 * MIB


def cgroup_v2(root: Path, current: int, limit: str, file: int = 0, shmem: int = 0, slab_reclaimable: int = 0) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "memory.current").write_text(f"{current}\n")
    (root / "memory.stat").write_text(
        f"anon 1\nfile {file}\nshmem {shmem}\nslab_reclaimable {slab_reclaimable}\ninactive_file 0\n")
    (root / "memory.max").write_text(f"{limit}\n")
    return root


def cgroup_v1(root: Path, usage: int, limit: int, cache: int = 0, shmem: int = 0, stat: bool = True) -> Path:
    (root / "memory").mkdir(parents=True, exist_ok=True)
    (root / "memory" / "memory.usage_in_bytes").write_text(f"{usage}\n")
    if stat:
        (root / "memory" / "memory.stat").write_text(f"total_cache {cache}\ntotal_shmem {shmem}\n")
    (root / "memory" / "memory.limit_in_bytes").write_text(f"{limit}\n")
    return root


def proc_entry(root: Path, pid: int, uid, rss_kib, cmd: bytes, rss_anon_kib=None, rss_shmem_kib=None) -> None:
    d = root / str(pid)
    d.mkdir(parents=True)
    uid_line = f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n" if uid is not None else ""
    rss = f"VmRSS:\t{rss_kib} kB\n" if rss_kib is not None else ""
    anon = f"RssAnon:\t{rss_anon_kib} kB\n" if rss_anon_kib is not None else ""
    anon += f"RssShmem:\t{rss_shmem_kib} kB\n" if rss_shmem_kib is not None else ""
    (d / "status").write_text(f"Name:\tx\n{uid_line}{rss}{anon}")
    (d / "cmdline").write_bytes(cmd)


class TempDirTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)


class CountersTest(TempDirTest):
    def test_v2_subtracts_page_cache_and_reclaimable_slab_but_not_shmem(self):
        root = cgroup_v2(self.tmp, current=1900 * MIB, limit=str(2 * GIB), file=300 * MIB, shmem=50 * MIB,
                         slab_reclaimable=20 * MIB)
        self.assertEqual(guard.memory_in_use(str(root)), (1900 - 300 + 50 - 20) * MIB)

    def test_falls_back_to_cgroup_v1(self):
        root = cgroup_v1(self.tmp, usage=800 * MIB, limit=10 * GIB, cache=100 * MIB, shmem=10 * MIB)
        self.assertEqual(guard.memory_in_use(str(root)), 710 * MIB)

    def test_subtracts_nothing_without_memory_stat(self):
        root = cgroup_v1(self.tmp, usage=800 * MIB, limit=10 * GIB, stat=False)
        self.assertEqual(guard.memory_in_use(str(root)), 800 * MIB)

    def test_is_none_without_counters(self):
        self.assertIsNone(guard.memory_in_use(str(self.tmp)))

    def test_limit_prefers_the_operators_variable(self):
        root = cgroup_v2(self.tmp, current=0, limit=str(8 * GIB))
        self.assertEqual(guard.memory_limit({guard.LIMIT_ENV: str(2 * GIB)}, str(root)), 2 * GIB)

    def test_limit_reads_cgroup_v2_max(self):
        root = cgroup_v2(self.tmp, current=0, limit=str(2 * GIB))
        self.assertEqual(guard.memory_limit({}, str(root)), 2 * GIB)

    def test_limit_ignores_an_unusable_variable(self):
        root = cgroup_v2(self.tmp, current=0, limit=str(2 * GIB))
        # "²" and "①" are digits to str.isdigit() that int() refuses.
        for value in ["", "0", "-5", "2Gi", "abc", "²", "①"]:
            with self.subTest(value=value):
                self.assertEqual(guard.memory_limit({guard.LIMIT_ENV: value}, str(root)), 2 * GIB)

    def test_unreadable_counter_content_reads_as_absent(self):
        (self.tmp / "memory.current").write_text("²\n")
        (self.tmp / "memory.max").write_text("①\n")
        self.assertIsNone(guard.memory_in_use(str(self.tmp)))
        self.assertIsNone(guard.memory_limit({}, str(self.tmp)))

    def test_limit_falls_back_to_cgroup_v1_only_below_the_machines_memory(self):
        proc = self.tmp / "proc"
        proc.mkdir()
        (proc / "meminfo").write_text(f"MemTotal:       {16 * GIB // 1024} kB\n")
        shell = cgroup_v1(self.tmp / "shell", 0, 2 * GIB)
        self.assertEqual(guard.memory_limit({}, str(shell), str(proc)), 2 * GIB)
        # On the gateway under gVisor the v1 file is the sandbox's whole virtual memory: not a limit.
        gateway = cgroup_v1(self.tmp / "gateway", 0, 16 * GIB)
        self.assertIsNone(guard.memory_limit({}, str(gateway), str(proc)))

    def test_limit_is_none_when_unlimited_or_unreadable(self):
        self.assertIsNone(guard.memory_limit({}, str(cgroup_v2(self.tmp / "a", 0, "max"))))
        # With no MemTotal to compare against, a v1 limit is not trusted.
        self.assertIsNone(guard.memory_limit({}, str(cgroup_v1(self.tmp / "b", 0, 10 * GIB)), str(self.tmp / "noproc")))

    def test_high_watermark_leaves_the_larger_of_256_mib_and_an_eighth_at_most_a_quarter(self):
        cases = [(2 * GIB, 1792 * MIB), (8 * GIB, 7 * GIB), (GIB, 768 * MIB),
                 (512 * MIB, 384 * MIB), (256 * MIB, 192 * MIB)]
        for limit, watermark in cases:
            with self.subTest(limit=limit):
                self.assertEqual(guard.high_watermark(limit), watermark)


class ProcessesTest(TempDirTest):
    def test_sizes_by_anonymous_plus_shared_memory_before_resident_size(self):
        proc_entry(self.tmp, 77, 1000, 300000, b"kubectl\0", rss_anon_kib=200000)
        proc_entry(self.tmp, 78, 1000, 900000, b"shm-writer\0", rss_anon_kib=1000, rss_shmem_kib=800000)
        self.assertEqual(sorted(guard.processes(str(self.tmp))), [
            (77, 1000, 200000 * 1024, "kubectl"), (78, 1000, 801000 * 1024, "shm-writer")])

    def test_reads_uid_rss_and_command_line(self):
        proc_entry(self.tmp, 1, 0, 7000, b"sshd: /usr/sbin/sshd -D -e\0")
        proc_entry(self.tmp, 42, 1000, 900 * 1024, b"/usr/local/libexec/kubectl\0get\0pods\0-A\0")
        (self.tmp / "self").mkdir()
        self.assertEqual(sorted(guard.processes(str(self.tmp))), [
            (1, 0, 7000 * 1024, "sshd: /usr/sbin/sshd -D -e"),
            (42, 1000, 900 * MIB, "/usr/local/libexec/kubectl get pods -A")])

    def test_survives_undecodable_vanished_and_malformed_entries(self):
        proc_entry(self.tmp, 10, 1000, 100 * 1024, b"grep\0\xff\xfe\0")   # not UTF-8
        proc_entry(self.tmp, 11, None, 100 * 1024, b"x\0")                 # no Uid line
        (self.tmp / "12").mkdir()                                          # exited mid-read: no files
        proc_entry(self.tmp, 13, 1000, None, b"")                          # kernel thread or zombie
        procs = {p[0]: p for p in guard.processes(str(self.tmp))}
        self.assertEqual(set(procs), {10, 13})
        self.assertTrue(procs[10][3].startswith("grep"))
        self.assertEqual(procs[13][2], 0)


PROCS = [
    (1, 0, 8 * MIB, "sshd: /usr/sbin/sshd -D -e"),
    (7, 0, 12 * MIB, "/usr/local/bin/python3 -I /usr/local/libexec/shell-memory-guard"),
    (50, 1000, 9 * MIB, "sshd-session: agent@notty"),
    (51, 1000, 5 * MIB, "bash"),
    (60, 1000, 900 * MIB, "/usr/local/libexec/kubectl get pods -A"),
    (61, 1001, 1500 * MIB, "/usr/local/libexec/kubectl get deploy,sts,ds,cronjob,pod -A -o json"),
    (62, 1000, 60 * MIB, "python3 collect.py compliance-audit"),
]


class ChoiceTest(unittest.TestCase):
    def test_is_candidate(self):
        cases = [(1000, 900 * MIB, True), (1001, 64 * MIB, True),
                 (1000, 63 * MIB, False),   # a shell, sshd's session process, a small tool
                 (0, 3 * GIB, False)]       # sshd, the entrypoint, this guard
        for uid, rss, expected in cases:
            with self.subTest(uid=uid, rss=rss):
                self.assertIs(guard.is_candidate(uid, rss), expected)

    def test_picks_the_largest_candidate(self):
        self.assertEqual(guard.choose_victim(PROCS), (
            61, 1500 * MIB, "/usr/local/libexec/kubectl get deploy,sts,ds,cronjob,pod -A -o json"))

    def test_never_picks_root_or_small_processes(self):
        self.assertIsNone(guard.choose_victim([p for p in PROCS if p[0] not in (60, 61)]))

    def test_over_watermark_edges(self):
        self.assertFalse(guard.over_watermark(1792 * MIB - 1, 2 * GIB))
        self.assertTrue(guard.over_watermark(1792 * MIB, 2 * GIB))
        self.assertFalse(guard.over_watermark(None, 2 * GIB))
        self.assertFalse(guard.over_watermark(2 * GIB, None))

    def test_gvisor_reserves_its_per_process_overhead(self):
        self.assertEqual(guard.effective_watermark(2 * GIB, False, 180), 1792 * MIB)
        self.assertEqual(guard.effective_watermark(2 * GIB, True, 180), 1792 * MIB - 180 * guard.GVISOR_PER_PROCESS_BYTES)

    def test_gvisor_is_read_from_the_kernel_release(self):
        with mock.patch.object(guard.os, "uname", return_value=mock.Mock(release="4.19.0-gvisor")):
            self.assertTrue(guard.on_gvisor())
        with mock.patch.object(guard.os, "uname", return_value=mock.Mock(release="6.1.0-gke")):
            self.assertFalse(guard.on_gvisor())

    def test_no_kill_can_help_only_within_the_floor_of_the_limit(self):
        self.assertFalse(guard.no_kill_can_help(2 * GIB - guard.MIN_VICTIM_BYTES - 1, 2 * GIB))
        self.assertTrue(guard.no_kill_can_help(2 * GIB - guard.MIN_VICTIM_BYTES, 2 * GIB))


class Fake:
    """A scripted container: usage per tick, the process table (fixed or per tick), and the live pids."""

    def __init__(self, test, usages, procs, alive=()):
        self.usages = list(usages)
        self.tables = procs if procs and isinstance(procs[0], list) else None
        self.procs, self.alive = procs, set(alive)
        self.killed, self.logs, self.scans = [], [], 0
        for target, attr, value in [
            (guard, "memory_in_use", lambda root=None: self.usages[0]),
            (guard, "memory_limit", lambda env, root=None, proc=None: 2 * GIB),
            (guard, "processes", self.processes),
            (guard.os, "kill", lambda pid, sig: self.killed.append((pid, sig))),
            (guard.os.path, "exists", lambda p: int(os.path.basename(p)) in self.alive),
            (guard, "log", self.logs.append),
        ]:
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            test.addCleanup(patcher.stop)
        self.dir = str(test.tmp)

    def processes(self, root=None):
        self.scans += 1
        table = self.tables[len(self.tables) - len(self.usages)] if self.tables else self.procs
        gone = {k for k, _ in self.killed} - self.alive
        return [p for p in table if p[0] not in gone]

    def run(self, g=None):
        g = g or guard.Guard({}, self.dir, self.dir)
        t = 0.0
        while self.usages:
            g.tick(t)
            self.usages.pop(0)
            t += guard.INTERVAL_SECONDS
        return [r["event"] for r in self.logs]


class LoopTest(TempDirTest):
    def test_on_gvisor_a_crowded_sandbox_acts_earlier(self):
        crowd = [(500 + i, 1000, 4 * MIB, "bash") for i in range(150)] + [(700, 1000, 300 * MIB, "kubectl get pods -A")]
        fake = Fake(self, [1500 * MIB], crowd)
        with mock.patch.object(guard, "process_count", return_value=len(crowd)):
            g = guard.Guard({}, fake.dir, fake.dir)
            g.gvisor = True  # 1792 MiB less 151 x 3 MiB is under 1,500 MiB
            self.assertEqual(fake.run(g), ["started", "killed"])
        self.assertEqual(fake.killed, [(700, signal.SIGKILL)])

    def test_below_the_watermark_it_neither_kills_nor_scans_proc(self):
        fake = Fake(self, [500 * MIB, 1200 * MIB, 1791 * MIB], PROCS)
        self.assertEqual(fake.run(), ["started"])
        self.assertEqual((fake.killed, fake.scans), ([], 0))

    def test_at_the_watermark_it_kills_the_largest_and_logs_it(self):
        fake = Fake(self, [1900 * MIB, 400 * MIB], PROCS)
        self.assertEqual(fake.run(), ["started", "killed"])
        self.assertEqual(fake.killed, [(61, signal.SIGKILL)])
        record = fake.logs[-1]
        self.assertEqual((record["pid"], record["rss"], record["high_watermark"]), (61, 1500 * MIB, 1792 * MIB))
        self.assertEqual(record["command"], "kubectl get deploy,sts,ds,cronjob,pod")
        json.dumps(record)

    def test_it_waits_for_the_victim_to_exit_before_a_second_kill(self):
        fake = Fake(self, [1900 * MIB] * 3, PROCS, alive={61})
        fake.run()
        self.assertEqual(fake.killed, [(61, signal.SIGKILL)])

    def test_two_kills_that_free_memory_in_a_row_are_both_made(self):
        fake = Fake(self, [1900 * MIB, 400 * MIB, 1850 * MIB, 950 * MIB], PROCS)
        self.assertEqual(fake.run(), ["started", "killed", "killed"])
        self.assertEqual([pid for pid, _ in fake.killed], [61, 60])

    def test_kills_continue_while_other_commands_grow(self):
        # A fan-out: 15 lists, each 5 MiB larger every tick; every kill is still made.
        def table(tick):
            return [(200 + i, 1000, (120 + i + 5 * tick) * MIB, f"kubectl get pods -A --context c{i}")
                    for i in range(15)]
        usages = [1900 * MIB, 1890 * MIB, 1880 * MIB, 1870 * MIB]
        fake = Fake(self, usages, [table(t) for t in range(len(usages))])
        events = fake.run()
        self.assertEqual(events[0], "started")
        self.assertEqual(len(fake.killed), len(events) - 1)
        self.assertGreaterEqual(len(fake.killed), len(usages))  # every tick still stops something

    def test_a_kill_that_can_still_save_the_container_is_made_beside_tmpfs(self):
        # 1,700 MiB of tmpfs, above the watermark, plus a 150 MiB list: stopping the list keeps the container up.
        procs = [(300, 1000, 150 * MIB, "kubectl get pods -A"), (301, 1000, 10 * MIB, "bash")]
        fake = Fake(self, [1860 * MIB], procs)
        self.assertEqual(fake.run(), ["started", "killed"])
        self.assertEqual(fake.killed, [(300, signal.SIGKILL)])

    def test_memory_no_kill_can_free_holds_off_and_releases_below_the_watermark(self):
        # 1,990 MiB outside processes: even stopping a 64 MiB command would leave the container at its limit.
        fake = Fake(self, [2000 * MIB, 2000 * MIB, 1000 * MIB], [(300, 1000, 10 * MIB, "bash")])
        self.assertEqual(fake.run(), ["started", "holding", "released"])
        self.assertEqual(fake.killed, [])

    def test_a_hold_releases_as_soon_as_a_kill_can_help_and_kills(self):
        bash = (300, 1000, 10 * MIB, "bash")
        lst = (301, 1000, 70 * MIB, "kubectl get pods -A")
        # outside 1,990 MiB: hold. Then 1,968 MiB of it plus a 70 MiB list: stopping the list keeps the container up.
        fake = Fake(self, [2000 * MIB, 2048 * MIB], [[bash], [bash, lst]])
        self.assertEqual(fake.run(), ["started", "holding", "released", "killed"])
        self.assertEqual(fake.killed, [(301, signal.SIGKILL)])

    def test_a_pid_still_signalled_is_skipped_for_the_next_largest(self):
        fake = Fake(self, [1900 * MIB], PROCS)
        g = guard.Guard({}, fake.dir, fake.dir)
        g.signalled = {61: -10.0}  # signalled long ago and still there: stuck in the kernel
        fake.run(g)
        self.assertEqual(fake.killed, [(60, signal.SIGKILL)])

    def test_signalled_pids_are_forgotten_below_the_watermark(self):
        # A later command that reuses a pid killed in an earlier episode is still stopped.
        fake = Fake(self, [500 * MIB, 1900 * MIB], PROCS)
        g = guard.Guard({}, fake.dir, fake.dir)
        g.signalled = {61: -10.0}
        fake.run(g)
        self.assertEqual(fake.killed, [(61, signal.SIGKILL)])

    def test_a_victim_that_does_not_exit_is_followed_by_another_after_the_settle_time(self):
        ticks = int(guard.SETTLE_SECONDS / guard.INTERVAL_SECONDS) + 2
        fake = Fake(self, [1900 * MIB] * ticks, PROCS, alive={61})
        fake.run()
        self.assertEqual([pid for pid, _ in fake.killed], [61, 60])

    def test_a_burst_is_stopped_in_one_batch(self):
        # 20 reads of 100 MiB, 2,040 MiB in use: the excess below the watermark less the batch margin
        # (2040 - (1792 - 64) = 312 MiB) is covered by four kills made together, not one per second.
        burst = [(800 + i, 1000, 100 * MIB, f"kubectl get pods -n team-{i:03d} -o json") for i in range(20)]
        fake = Fake(self, [2040 * MIB], burst)
        self.assertEqual(fake.run(), ["started"] + ["killed"] * 4)
        self.assertEqual({r["batch"] for r in fake.logs[1:]}, {4})

    def test_memory_already_on_its_way_out_is_not_freed_twice(self):
        burst = [(800 + i, 1000, 100 * MIB, "kubectl get pods -n x -o json") for i in range(20)]
        fake = Fake(self, [2040 * MIB, 2040 * MIB], burst, alive={800, 801, 802, 803, 804, 805, 806, 807, 808, 809,
                                                                 810, 811, 812, 813, 814, 815, 816, 817, 818, 819})
        fake.run()
        self.assertEqual(len(fake.killed), 4)  # the second tick counts the four still exiting

    def test_many_small_commands_are_not_stopped_and_it_says_so_once(self):
        small = [(400 + i, 1000, 50 * MIB, f"kubectl get ns --context c{i}") for i in range(38)]
        fake = Fake(self, [1900 * MIB, 1900 * MIB, 1000 * MIB, 1900 * MIB], small)
        self.assertEqual(fake.run(), ["started", "no-candidate", "no-candidate"])  # once per episode
        self.assertEqual(fake.killed, [])
        self.assertEqual((fake.logs[1]["largest"], fake.logs[1]["floor"]), (50 * MIB, guard.MIN_VICTIM_BYTES))

    def test_it_says_when_it_is_disabled_and_when_it_starts(self):
        fake = Fake(self, [None, None, 500 * MIB], PROCS)
        self.assertEqual(fake.run(), ["disabled", "started"])
        self.assertEqual(fake.killed, [])


class LoggableCommandTest(unittest.TestCase):
    def test_keeps_the_program_and_plain_words_before_the_first_flag(self):
        cases = [
            ("/usr/local/libexec/kubectl get deploy,sts,ds,cronjob,pod -A -o json",
             "kubectl get deploy,sts,ds,cronjob,pod"),
            ("python3 /opt/data/scripts/collect.py compliance-audit --workspace /x", "python3 /opt/data/scripts/collect.py compliance-audit"),
            ("kubectl --token=eyJhbGciOiJSUzI1NiIsImtpZCI6 get pods -A", "kubectl"),
            ("kubectl get pods --token eyJhbGciOiJSUzI1NiIsImtpZCI6", "kubectl get pods"),
            ("curl https://x/?access_token=abc", "curl"),                 # '?' and '=' are not plain
            ("tool " + "A" * 60, "tool"),                                   # a long word may be a secret
            ("a b c d e f g", "a b c d e"),
            ("python3 push.py ghp_" + "a1" * 18, "python3 push.py"),       # a positional token holds digits
            ("python3 call.py 0123456789abcdef0123456789abcdef", "python3 call.py"),
            ("tool AKIAIOSFODNN7EXAMPLE", "tool"),
            ("", ""),
        ]
        for cmd, expected in cases:
            with self.subTest(cmd=cmd):
                self.assertEqual(guard.loggable_command(cmd), expected)


class RunTest(unittest.TestCase):
    def test_an_error_in_a_tick_is_logged_and_the_loop_goes_on(self):
        calls, logs = [], []

        def tick(_self, now):
            calls.append(now)
            if len(calls) == 1:
                raise ValueError("boom")
            if len(calls) == 3:
                raise KeyboardInterrupt

        with mock.patch.object(guard.Guard, "tick", tick), mock.patch.object(guard.time, "sleep"), \
                mock.patch.object(guard, "log", logs.append):
            with self.assertRaises(KeyboardInterrupt):
                guard.run({})
        self.assertEqual(len(calls), 3)
        self.assertEqual(logs[0]["event"], "error")
        self.assertIn("boom", logs[0]["error"])


@unittest.skipUnless(sys.platform.startswith("linux"), "reads a real /proc")
class RealProcessTest(unittest.TestCase):
    def test_a_real_child_is_read_and_stopped(self):
        child = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time; b = bytearray(96 << 20); print('ready', flush=True); time.sleep(60)"],
            stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")  # allocated before /proc is read
            mine = [p for p in guard.processes() if p[0] == child.pid]
            self.assertTrue(mine and mine[0][2] >= guard.MIN_VICTIM_BYTES)
            pid, _rss, _cmd = guard.choose_victim([(mine[0][0], 1000, mine[0][2], mine[0][3])])
            os.kill(pid, signal.SIGKILL)
            self.assertEqual(child.wait(timeout=10), -signal.SIGKILL)
        finally:
            if child.poll() is None:
                child.kill()


if __name__ == "__main__":
    unittest.main()
