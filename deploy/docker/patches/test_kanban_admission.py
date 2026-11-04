"""Unit tests for the kanban admission patch applied by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

``kanban_admission.py`` is tested flat, with a fake engine in place of
``hermes_cli`` (the tick's helpers, ``block_task`` over the minimal
tasks/task_runs/task_events schema the sibling tests use), a fake ``/proc``
and cgroup tree in a temporary directory, and a verbatim copy of upstream's
``classify_pressure`` so the budget arithmetic is proved against the real
thresholds. The applier is tested against the verifier's fixture tree, which
carries each anchor exactly once in upstream's v2026.9.14 shape; the verifier
itself drives the applied engine through ``dispatch_once``.
"""

import ast
import json
import signal
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

import apply_kanban_admission as applier
import kanban_admission as admission
import verify_kanban_admission as verifier

# gateway/memory_status.py at v2026.9.14: the tiers and the classifier, verbatim.
CRITICAL_AVAILABLE_KIB = 64 * 1024
CRITICAL_AVAILABLE_FRACTION = 0.05
ELEVATED_AVAILABLE_KIB = 128 * 1024
ELEVATED_AVAILABLE_FRACTION = 0.15
PRESSURE_TIERS = (
    ("critical", CRITICAL_AVAILABLE_KIB, CRITICAL_AVAILABLE_FRACTION),
    ("elevated", ELEVATED_AVAILABLE_KIB, ELEVATED_AVAILABLE_FRACTION),
)


def upstream_classify(available_kib, total_kib):
    def nonneg(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    available, total = nonneg(available_kib), nonneg(total_kib)
    if available is None:
        return "unknown"
    fraction = available / total if total else None
    for level, kib_floor, frac_floor in PRESSURE_TIERS:
        if available < kib_floor or (fraction is not None and fraction < frac_floor):
            return level
    return "ok"


SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    assignee TEXT,
    priority INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL,
    claim_lock TEXT,
    worker_pid INTEGER,
    current_run_id INTEGER,
    block_kind TEXT
);
CREATE TABLE task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    status TEXT,
    started_at INTEGER,
    ended_at INTEGER
);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    run_id INTEGER,
    kind TEXT NOT NULL,
    payload TEXT,
    created_at INTEGER
);
"""

NOW = 1_800_000_000
HOST = "platform-agent-gateway-75b5f6ddf6-7dkd7"
OWN_LOCK = f"{HOST}:4"
FOREIGN_LOCK = "platform-agent-gateway-595bbd777f-5vlzk:7"
MIB = 1024


def board():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def ready(conn, tid, *, age=0, assignee="platform"):
    conn.execute(
        "INSERT INTO tasks (id, status, assignee, created_at) VALUES (?, 'ready', ?, ?)",
        (tid, assignee, NOW - age),
    )
    return tid


def running(conn, tid, *, pid, lock=OWN_LOCK):
    cur = conn.execute(
        "INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'running', ?)", (tid, NOW),
    )
    conn.execute(
        "INSERT INTO tasks (id, status, assignee, created_at, claim_lock, worker_pid, current_run_id) "
        "VALUES (?, 'running', 'platform', ?, ?, ?, ?)",
        (tid, NOW, lock, pid, cur.lastrowid),
    )
    return tid


def event(conn, tid, kind, created_at):
    conn.execute(
        "INSERT INTO task_events (task_id, kind, created_at) VALUES (?, ?, ?)", (tid, kind, created_at),
    )


def ended_run(conn, tid, ended_at):
    conn.execute(
        "INSERT INTO task_runs (task_id, status, started_at, ended_at) VALUES (?, 'crashed', ?, ?)",
        (tid, ended_at - 1, ended_at),
    )


def status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0]


def blocks(conn, tid):
    return [
        json.loads(r["payload"])
        for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked' ORDER BY id", (tid,),
        )
    ]


def result(**fields):
    base = dict(
        spawned=[], skipped_unassigned=[], skipped_nonspawnable=[], skipped_per_profile_capped=[],
        respawn_guarded=[], auto_blocked=[],
    )
    base.update(fields)
    return SimpleNamespace(**base)


class FakeEngine:
    """The engine the decisions reach, over the minimal schema.

    ``block_task`` performs upstream's transition: the row leaves ``running``
    or ``ready`` for ``blocked`` with its claim and pid cleared, the open run is
    closed, and a ``blocked`` event carries ``reason`` and ``kind``. A kind
    outside the valid set raises, as upstream's does.
    """

    VALID_KINDS = {"dependency", "needs_input", "capability", "transient", "capacity"}

    def __init__(self, *, alive=(), upstream_level="ok", config=None, profiles=None, guarded=None):
        self.alive = set(alive)
        self.kills = []
        self.blocks = []
        self.upstream_level_value = upstream_level
        self.upstream_calls = 0
        self._config = config
        self._profiles = profiles
        self._guarded = guarded or {}
        #: Pids that exit on SIGTERM; the others need SIGKILL.
        self.dies_on_term = set()
        #: Pids that survive SIGKILL too (a worker in uninterruptible sleep).
        self.immortal = set()
        #: Pids the engine may not signal at all (``os.kill`` raises EPERM).
        self.untouchable = set()
        #: Pids that die between the liveness check and the SIGKILL.
        self.vanish_at_sigkill = set()
        #: Called as ``on_kill(pid, sig)`` after a signal lands, for a test
        #: that changes the world while the worker dies.
        self.on_kill = None

    def host_prefix(self):
        return f"{HOST}:"

    def pid_alive(self, pid):
        return pid in self.alive

    def kill_fn(self, signal_fn):
        return signal_fn or self._kill

    def _kill(self, pid, sig):
        if pid not in self.alive:
            raise ProcessLookupError(pid)
        if pid in self.untouchable:
            raise PermissionError(pid)
        if sig == signal.SIGKILL and pid in self.vanish_at_sigkill:
            self.alive.discard(pid)
            raise ProcessLookupError(pid)
        self.kills.append((pid, sig))
        if (sig == signal.SIGKILL and pid not in self.immortal) or pid in self.dies_on_term:
            self.alive.discard(pid)
        if self.on_kill is not None:
            self.on_kill(pid, sig)

    def poll_worker_exit(self, pid):
        return pid not in self.alive

    def sigkill(self, kill, pid):
        try:
            kill(pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            return False
        return True

    def terminate(self, pid, claim_lock, signal_fn):
        """Upstream's ``_terminate_reclaimed_worker`` over the fake kill."""
        info = {"prev_pid": pid, "host_local": False, "termination_attempted": False,
                "terminated": False, "sigkill": False}
        if not str(claim_lock).startswith(self.host_prefix()):
            return info
        info["host_local"] = True
        kill = self.kill_fn(signal_fn)
        info["termination_attempted"] = True
        try:
            kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            info["terminated"] = True
            return info
        except OSError:
            return info
        if self.poll_worker_exit(pid):
            info["terminated"] = True
            return info
        if self.pid_alive(pid):
            if not self.sigkill(kill, pid):
                return info
            info["sigkill"] = True
        info["terminated"] = not self.pid_alive(pid)
        return info

    @staticmethod
    def survived(termination):
        return bool(
            termination.get("termination_attempted")
            and termination.get("host_local")
            and not termination.get("terminated")
        )

    def block_task(self, conn, task_id, *, reason, kind, expected_run_id=None):
        if kind not in self.VALID_KINDS:
            raise ValueError(kind)
        sql = (
            "UPDATE tasks SET status = 'blocked', claim_lock = NULL, worker_pid = NULL, block_kind = ? "
            "WHERE id = ? AND status IN ('running', 'ready')"
        )
        params = (kind, task_id)
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params = (*params, expected_run_id)
        if conn.execute(sql, params).rowcount != 1:
            return False
        conn.execute(
            "UPDATE task_runs SET status = 'blocked', ended_at = ? WHERE task_id = ? AND ended_at IS NULL",
            (NOW, task_id),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'blocked', ?, ?)",
            (task_id, json.dumps({"reason": reason, "kind": kind}), NOW),
        )
        conn.commit()
        self.blocks.append((task_id, kind, reason))
        return True

    def upstream_pressure_level(self):
        self.upstream_calls += 1
        return self.upstream_level_value

    def classify(self, available_kib, total_kib):
        return upstream_classify(available_kib, total_kib)

    def profile_exists(self, assignee):
        return None if self._profiles is None else assignee in self._profiles

    def respawn_guard_reason(self, conn, task_id):
        return self._guarded.get(task_id)

    def config(self):
        if isinstance(self._config, Exception):
            raise self._config
        return self._config


def settings(**fields):
    return admission.AdmissionSettings(**fields)


def sample(budget_mib, used_mib, source="cgroup-v1"):
    budget_kib, used_kib = budget_mib * MIB, used_mib * MIB
    return {
        "mem_total_kib": budget_kib, "mem_available_kib": max(0, budget_kib - used_kib),
        "usage_kib": used_kib, "source": source,
    }


# ---------------------------------------------------------------------------
# A fake /proc and cgroup tree
# ---------------------------------------------------------------------------


class FakeTree:
    """``<root>/proc/<pid>/{stat,status}`` and the two cgroup files, on demand."""

    #: The fake kernel booted 100 s before NOW; a process's default start time
    #: (11,000 ticks at 100 Hz) is 10 s after NOW, inside every test run's row.
    BTIME = NOW - 100
    DEFAULT_STARTTIME = 11_000

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.boot(self.BTIME)
        self.v1 = str(self.root / "memory.usage_in_bytes")
        self.v2 = str(self.root / "memory.current")
        self.v1_stat = str(self.root / "memory.stat.v1")
        self.v2_stat = str(self.root / "memory.stat.v2")

    def close(self):
        self.tmp.cleanup()

    def boot(self, btime):
        (self.proc / "stat").write_text(f"cpu 1 2 3 4\nbtime {btime}\nprocesses 42\n")

    def no_boot_time(self):
        (self.proc / "stat").unlink()

    def process(self, pid, *, ppid, rss_kib=None, starttime=None, comm="python3", pgrp=0, cmdline=None):
        starttime = self.DEFAULT_STARTTIME if starttime is None else starttime
        here = self.proc / str(pid)
        here.mkdir()
        if cmdline is not None:
            (here / "cmdline").write_bytes(b"\0".join(part.encode() for part in cmdline) + b"\0")
        # pid (comm) state ppid pgrp session tty tpgid flags minflt cminflt majflt cmajflt
        # utime stime cutime cstime priority nice threads itrealvalue starttime ...
        tail = ["S", str(ppid), str(pgrp)] + ["0"] * 16 + [str(starttime), "0", "0"]
        (here / "stat").write_text(f"{pid} ({comm}) " + " ".join(tail) + "\n")
        lines = [f"Name:\t{comm}", "State:\tS (sleeping)", f"Pid:\t{pid}", f"PPid:\t{ppid}"]
        if rss_kib is not None:
            lines.insert(3, f"VmRSS:\t{rss_kib:8d} kB")
        (here / "status").write_text("\n".join(lines) + "\n")

    def restart(self, pid, starttime):
        """The same pid reused by a new process: only its start time changes."""
        here = self.proc / str(pid)
        text = (here / "stat").read_text().split()
        text[21] = str(starttime)
        (here / "stat").write_text(" ".join(text) + "\n")

    def cgroup_v1(self, text):
        Path(self.v1).write_text(text)

    def cgroup_v2(self, text):
        Path(self.v2).write_text(text)

    def cgroup_v1_stat(self, text):
        Path(self.v1_stat).write_text(text)

    def cgroup_v2_stat(self, text):
        Path(self.v2_stat).write_text(text)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class SettingsTest(unittest.TestCase):
    def test_defaults_when_the_section_is_absent(self):
        for config in ({}, None, {"kanban": {}}, {"kanban": {"admission": {}}}, {"kanban": "oops"}, []):
            with self.subTest(config=config):
                got = admission.settings_from(config)
                self.assertIsNone(got.memory_budget_mib)
                self.assertEqual(got.worker_rss_cap_mib, 1536)
                self.assertEqual(got.queue_bound_seconds, 120)
                self.assertTrue(got.fail_closed)

    def test_the_four_keys_are_read(self):
        got = admission.settings_from({"kanban": {"admission": {
            "memory_budget_mib": 4096, "worker_rss_cap_mib": 2048,
            "queue_bound_seconds": 30, "fail_closed": False,
        }}})
        self.assertEqual(
            (got.memory_budget_mib, got.worker_rss_cap_mib, got.queue_bound_seconds, got.fail_closed),
            (4096, 2048, 30, False),
        )

    def test_bad_values_fall_back_to_the_defaults(self):
        for bad in (0, -5, "many", None, True, 2.5, [1]):
            with self.subTest(bad=bad):
                got = admission.settings_from({"kanban": {"admission": {
                    "memory_budget_mib": bad, "worker_rss_cap_mib": bad, "queue_bound_seconds": bad,
                }}})
                self.assertIsNone(got.memory_budget_mib)
                self.assertEqual(got.worker_rss_cap_mib, 1536)
                self.assertEqual(got.queue_bound_seconds, 120)

    def test_numeric_strings_and_integral_floats_count(self):
        got = admission.settings_from({"kanban": {"admission": {
            "memory_budget_mib": "2048", "worker_rss_cap_mib": 1536.0,
        }}})
        self.assertEqual((got.memory_budget_mib, got.worker_rss_cap_mib), (2048, 1536))

    def test_fail_closed_spellings(self):
        for value, want in (
            (True, True), (False, False), ("false", False), ("no", False), ("0", False), (0, False),
            ("true", True), ("yes", True), ("on", True), (1, True), ("maybe", True), (None, True),
        ):
            with self.subTest(value=value):
                got = admission.settings_from({"kanban": {"admission": {"fail_closed": value}}})
                self.assertIs(got.fail_closed, want)

    def test_load_settings_reads_the_engine_config(self):
        engine = FakeEngine(config={"kanban": {"admission": {"memory_budget_mib": 1024}}})
        self.assertEqual(admission.load_settings(engine=engine).memory_budget_mib, 1024)

    def test_an_unreadable_config_yields_the_defaults_and_a_log_line(self):
        engine = FakeEngine(config=RuntimeError("no config"))
        with self.assertLogs(admission.logger, level="WARNING") as logs:
            got = admission.load_settings(engine=engine)
        self.assertEqual(got, admission.AdmissionSettings())
        self.assertTrue(any("kanban admission:" in line for line in logs.output))

    def test_an_explicit_config_is_not_loaded(self):
        engine = FakeEngine(config=RuntimeError("must not be called"))
        self.assertEqual(admission.load_settings({}, engine=engine).queue_bound_seconds, 120)


# ---------------------------------------------------------------------------
# The memory counters
# ---------------------------------------------------------------------------


class ProcessTreeTest(unittest.TestCase):
    def setUp(self):
        self.tree = FakeTree()
        self.addCleanup(self.tree.close)
        self.tree.process(1, ppid=0, rss_kib=1 * MIB)
        self.tree.process(100, ppid=1, rss_kib=10 * MIB)
        self.tree.process(200, ppid=100, rss_kib=20 * MIB)
        self.tree.process(300, ppid=200, rss_kib=5 * MIB)
        self.tree.process(400, ppid=1, rss_kib=40 * MIB)
        self.tree.process(500, ppid=100, rss_kib=None, comm="kworker")

    def test_the_tree_sums_the_worker_and_its_descendants_only(self):
        self.assertEqual(
            admission.process_tree_rss_kib(100, proc_root=str(self.tree.proc)), (10 + 20 + 5) * MIB,
        )

    def test_a_process_without_a_resident_set_counts_nothing(self):
        self.assertEqual(admission.process_tree_rss_kib(200, proc_root=str(self.tree.proc)), 25 * MIB)
        self.assertEqual(admission.process_tree_rss_kib(500, proc_root=str(self.tree.proc)), None)

    def test_a_gone_pid_is_none_not_zero(self):
        self.assertIsNone(admission.process_tree_rss_kib(999, proc_root=str(self.tree.proc)))

    def test_the_tree_lists_parents_before_children(self):
        members = admission.process_tree(100, proc_root=str(self.tree.proc))
        self.assertEqual(members[0], 100)
        self.assertEqual(set(members), {100, 200, 300, 500})
        self.assertLess(members.index(200), members.index(300))

    def test_a_cycle_in_a_corrupt_proc_terminates(self):
        self.tree.process(600, ppid=700, rss_kib=1)
        self.tree.process(700, ppid=600, rss_kib=1)
        self.assertEqual(admission.process_tree(600, proc_root=str(self.tree.proc)), [600, 700])

    def test_a_missing_proc_root_is_unknown(self):
        self.assertIsNone(admission.process_tree_rss_kib(100, proc_root=str(self.tree.root / "absent")))


class SampleTest(unittest.TestCase):
    def setUp(self):
        self.tree = FakeTree()
        self.addCleanup(self.tree.close)
        self.tree.process(1, ppid=0, rss_kib=1 * MIB)
        self.tree.process(100, ppid=1, rss_kib=300 * MIB)
        self.tree.process(200, ppid=100, rss_kib=200 * MIB)

    def take(self, budget_mib=2048):
        return admission.container_memory_sample(
            budget_mib, cgroup_v1_path=self.tree.v1, cgroup_v2_path=self.tree.v2,
            cgroup_v1_stat_path=self.tree.v1_stat, cgroup_v2_stat_path=self.tree.v2_stat,
            proc_root=str(self.tree.proc), own_pid=100,
        )

    def test_cgroup_v1_comes_first(self):
        self.tree.cgroup_v1(f"{836599808}\n")
        self.tree.cgroup_v2(f"{1024 * 1024 * 1024}\n")
        got = self.take()
        self.assertEqual(got["source"], "cgroup-v1")
        self.assertEqual(got["usage_kib"], 836599808 // 1024)
        self.assertEqual(got["mem_total_kib"], 2048 * MIB)
        self.assertEqual(got["mem_available_kib"], 2048 * MIB - 836599808 // 1024)
        # No memory.stat beside it: nothing is subtracted, the raw counter stands.
        self.assertEqual(got["inactive_file_kib"], 0)

    def test_the_inactive_file_cache_is_not_held_memory(self):
        """A warm file cache is reclaimed before anything is evicted; the kubelet's
        working set leaves it out, and so does the sampler."""
        self.tree.cgroup_v2(f"{1800 * MIB * 1024}\n")
        self.tree.cgroup_v2_stat(
            f"anon {850 * MIB * 1024}\nfile {950 * MIB * 1024}\n"
            f"active_file {50 * MIB * 1024}\ninactive_file {900 * MIB * 1024}\nslab_reclaimable 1024\n"
        )
        got = self.take()
        self.assertEqual(got["source"], "cgroup-v2")
        self.assertEqual(got["inactive_file_kib"], 900 * MIB)
        self.assertEqual(got["usage_kib"], 900 * MIB)
        self.assertEqual(got["mem_available_kib"], (2048 - 900) * MIB)

    def test_cgroup_v1_subtracts_the_hierarchy_total_only(self):
        self.tree.cgroup_v1(f"{1000 * MIB * 1024}\n")
        self.tree.cgroup_v1_stat(
            f"cache {400 * MIB * 1024}\ninactive_file {100 * MIB * 1024}\n"
            f"total_inactive_file {300 * MIB * 1024}\n"
        )
        got = self.take()
        self.assertEqual((got["source"], got["usage_kib"], got["inactive_file_kib"]),
                         ("cgroup-v1", 700 * MIB, 300 * MIB))

    def test_a_cache_larger_than_the_counter_leaves_zero_not_a_negative(self):
        self.tree.cgroup_v2(f"{100 * MIB * 1024}\n")
        self.tree.cgroup_v2_stat(f"inactive_file {200 * MIB * 1024}\n")
        self.assertEqual(self.take()["usage_kib"], 0)

    def test_a_malformed_stat_value_subtracts_nothing(self):
        self.tree.cgroup_v2(f"{1024 * MIB * 1024}\n")
        self.tree.cgroup_v2_stat("inactive_file lots\n")
        self.assertEqual(self.take()["usage_kib"], 1024 * MIB)

    def test_cgroup_v2_when_v1_is_absent(self):
        self.tree.cgroup_v2(f"{1024 * 1024 * 1024}\n")
        got = self.take()
        self.assertEqual((got["source"], got["usage_kib"]), ("cgroup-v2", 1024 * MIB))

    def test_the_process_tree_when_neither_cgroup_file_reads(self):
        got = self.take()
        self.assertEqual((got["source"], got["usage_kib"]), ("process-tree", 500 * MIB))
        self.assertEqual(got["mem_available_kib"], (2048 - 500) * MIB)

    def test_a_malformed_cgroup_file_is_skipped(self):
        self.tree.cgroup_v1("max\n")
        self.tree.cgroup_v2("not a number\n")
        self.assertEqual(self.take()["source"], "process-tree")

    def test_none_when_nothing_can_be_read(self):
        self.assertIsNone(admission.container_memory_sample(
            2048, cgroup_v1_path=self.tree.v1, cgroup_v2_path=self.tree.v2,
            proc_root=str(self.tree.root / "absent"), own_pid=100,
        ))

    def test_usage_over_the_budget_leaves_nothing_available(self):
        self.tree.cgroup_v1(f"{3 * 1024 * 1024 * 1024}\n")
        self.assertEqual(self.take()["mem_available_kib"], 0)


# ---------------------------------------------------------------------------
# The pressure decision
# ---------------------------------------------------------------------------


class PressureTest(unittest.TestCase):
    def level(self, used_mib, *, budget_mib=2048, engine=None, **fields):
        res = result()
        got = admission.pressure_level_for_tick(
            res, settings=settings(memory_budget_mib=budget_mib, **fields),
            sample_fn=lambda budget: sample(budget, used_mib), engine=engine or FakeEngine(),
        )
        return got, getattr(res, admission.RESULT_ATTR)

    def test_classifies_against_the_budget_not_meminfo(self):
        # 2,000 of 2,048 MiB used: 48 MiB available, under the 64 MiB floor.
        engine = FakeEngine(upstream_level="ok")
        got, record = self.level(2000, engine=engine)
        self.assertEqual(got, "critical")
        self.assertEqual(engine.upstream_calls, 0)
        self.assertEqual(
            (record["level"], record["limited"], record["budget_mib"], record["usage_mib"], record["source"]),
            ("critical", True, 2048, 2000, "cgroup-v1"),
        )

    def test_the_elevated_band_is_fifteen_percent_of_the_budget(self):
        # 148 MiB available of 2,048 is 7.2%: elevated, not critical.
        self.assertEqual(self.level(1900)[0], "elevated")
        # 400 MiB available is 19.5%: ok.
        self.assertEqual(self.level(1648)[0], "ok")
        self.assertFalse(self.level(1648)[1]["limited"])

    def test_the_development_install_reading_is_ok_against_its_request(self):
        # 836,599,808 bytes used (798 MiB) against the 2,048 MiB request.
        self.assertEqual(self.level(836599808 // (1024 * 1024))[0], "ok")

    def test_unreadable_counters_fail_closed_with_a_log_line(self):
        res = result()
        with self.assertLogs(admission.logger, level="WARNING") as logs:
            got = admission.pressure_level_for_tick(
                res, settings=settings(memory_budget_mib=2048), sample_fn=lambda budget: None,
                engine=FakeEngine(upstream_level="ok"),
            )
        self.assertEqual(got, "critical")
        record = getattr(res, admission.RESULT_ATTR)
        self.assertTrue(record["fail_closed"])
        self.assertIn("no memory counter is readable", record["cause"])
        self.assertTrue(any(line.startswith("WARNING:") and "kanban admission: memory pressure is unknown" in line
                            for line in logs.output), logs.output)

    def test_unreadable_counters_fall_back_to_upstream_when_not_failing_closed(self):
        engine = FakeEngine(upstream_level="elevated")
        res = result()
        got = admission.pressure_level_for_tick(
            res, settings=settings(memory_budget_mib=2048, fail_closed=False),
            sample_fn=lambda budget: None, engine=engine,
        )
        self.assertEqual((got, engine.upstream_calls), ("elevated", 1))
        self.assertEqual(getattr(res, admission.RESULT_ATTR)["source"], "meminfo")

    def test_without_a_budget_upstream_decides(self):
        engine = FakeEngine(upstream_level="ok")
        res = result()
        got = admission.pressure_level_for_tick(
            res, settings=settings(memory_budget_mib=None),
            sample_fn=lambda budget: self.fail("the sample must not be taken"), engine=engine,
        )
        self.assertEqual((got, engine.upstream_calls), ("ok", 1))

    def test_without_a_budget_upstreams_unknown_still_fails_closed(self):
        engine = FakeEngine(upstream_level="unknown")
        res = result()
        with self.assertLogs(admission.logger, level="WARNING"):
            got = admission.pressure_level_for_tick(res, settings=settings(memory_budget_mib=None), engine=engine)
        self.assertEqual(got, "critical")
        self.assertTrue(getattr(res, admission.RESULT_ATTR)["fail_closed"])
        got = admission.pressure_level_for_tick(
            result(), settings=settings(memory_budget_mib=None, fail_closed=False), engine=engine,
        )
        self.assertEqual(got, "unknown")

    def test_a_raising_sample_fails_closed_and_never_raises(self):
        def boom(budget):
            raise OSError("cgroup went away")

        res = result()
        with self.assertLogs(admission.logger, level="WARNING"):
            got = admission.pressure_level_for_tick(
                res, settings=settings(memory_budget_mib=2048), sample_fn=boom, engine=FakeEngine(),
            )
        self.assertEqual(got, "critical")
        self.assertIn("cgroup went away", getattr(res, admission.RESULT_ATTR)["cause"])
        engine = FakeEngine(upstream_level="elevated")
        res = result()
        got = admission.pressure_level_for_tick(
            res, settings=settings(memory_budget_mib=2048, fail_closed=False), sample_fn=boom, engine=engine,
        )
        self.assertEqual((got, engine.upstream_calls), ("elevated", 1))
        self.assertEqual(getattr(res, admission.RESULT_ATTR)["source"], "meminfo")
        got = admission.pressure_level_for_tick(
            result(), settings=settings(memory_budget_mib=2048, fail_closed=False), sample_fn=boom,
            engine=FakeEngine(upstream_level="unknown"),
        )
        self.assertEqual(got, "unknown")

    def test_the_settings_are_loaded_from_the_engine_when_not_given(self):
        engine = FakeEngine(config={"kanban": {"admission": {"memory_budget_mib": 1000}}})
        got = admission.pressure_level_for_tick(
            result(), sample_fn=lambda budget: sample(budget, 990), engine=engine,
        )
        self.assertEqual(got, "critical")

    def test_a_limited_tick_logs_the_figures(self):
        with self.assertLogs(admission.logger, level="WARNING") as logs:
            self.level(1900)
        self.assertTrue(any("1,900 MiB used of the 2,048 MiB budget (cgroup-v1)" in line for line in logs.output))


class TickWasLimitedTest(unittest.TestCase):
    def test_memory_limits(self):
        res = result()
        setattr(res, admission.RESULT_ATTR, {"limited": True})
        self.assertTrue(admission.tick_was_limited(res, spawn_budget=None, spawned=0))

    def test_an_exhausted_slot_budget_limits(self):
        self.assertTrue(admission.tick_was_limited(result(), spawn_budget=2, spawned=2))
        self.assertFalse(admission.tick_was_limited(result(), spawn_budget=2, spawned=1))

    def test_an_uncapped_idle_tick_is_not_limited(self):
        res = result()
        setattr(res, admission.RESULT_ATTR, {"limited": False})
        self.assertFalse(admission.tick_was_limited(res, spawn_budget=None, spawned=3))

    def test_the_siblings_reservation_limits(self):
        self.assertTrue(admission.tick_was_limited(result(skipped_reserved=["t_1"]), spawn_budget=None, spawned=0))
        self.assertFalse(admission.tick_was_limited(result(skipped_reserved=[]), spawn_budget=None, spawned=0))


# ---------------------------------------------------------------------------
# The worker RSS cap
# ---------------------------------------------------------------------------


class ReasonTest(unittest.TestCase):
    def test_the_contract_example(self):
        self.assertEqual(
            admission.capacity_reason("the worker used 1,612 MiB against the 1,536 MiB cap"),
            "capacity: the worker used 1,612 MiB against the 1,536 MiB cap; the install cannot take "
            "this now. Options: wait for the queue to drain, ask for a smaller scope (at most 10 "
            "clusters), or use the scheduled report.",
        )


class RssCapTest(unittest.TestCase):
    def enforce(self, conn, engine, sizes_mib, **kwargs):
        return admission.enforce_worker_rss_caps(
            conn, settings=settings(worker_rss_cap_mib=1536), engine=engine,
            rss_fn=lambda pid: None if sizes_mib.get(pid) is None else sizes_mib[pid] * MIB,
            identity_fn=lambda pid, task_id, run_started_at: True, **kwargs,
        )

    def test_a_stale_rows_pid_that_predates_the_run_is_never_measured_or_killed(self):
        """After a container restart the namespace hands a predecessor's pid to a
        new process while the stale running row is still host-local; a process
        that started before the row's run is not its worker, and the row is left
        to the crash sweep."""
        tree = FakeTree()
        self.addCleanup(tree.close)
        tree.process(1, ppid=0, rss_kib=1)
        # Started 95 s before NOW; the run row says NOW.
        tree.process(900, ppid=1, rss_kib=4000 * MIB, starttime=500)
        conn = board()
        running(conn, "t_stale", pid=900)
        engine = FakeEngine(alive={900})
        got = admission.enforce_worker_rss_caps(
            conn, settings=settings(worker_rss_cap_mib=1536), engine=engine, proc_root=str(tree.proc),
        )
        self.assertEqual((got, engine.kills, status(conn, "t_stale")), ([], [], "running"))

    def test_the_runs_own_worker_is_measured(self):
        tree = FakeTree()
        self.addCleanup(tree.close)
        tree.process(1, ppid=0, rss_kib=1)
        tree.process(901, ppid=1, rss_kib=4000 * MIB)  # default start: 10 s after the run
        conn = board()
        running(conn, "t_own", pid=901)
        engine = FakeEngine(alive={901})
        engine.dies_on_term = {901}
        got = admission.enforce_worker_rss_caps(
            conn, settings=settings(worker_rss_cap_mib=1536), engine=engine, proc_root=str(tree.proc),
        )
        self.assertEqual((got, status(conn, "t_own")), (["t_own"], "blocked"))

    def test_without_a_boot_time_the_command_line_names_the_worker(self):
        tree = FakeTree()
        self.addCleanup(tree.close)
        tree.no_boot_time()
        tree.process(1, ppid=0, rss_kib=1)
        tree.process(910, ppid=1, cmdline=["hermes", "-p", "platform", "chat", "-q", "work kanban task t_mine"])
        tree.process(911, ppid=1, cmdline=["hermes", "-p", "platform", "chat", "-q", "work kanban task t_other"])
        tree.process(912, ppid=1)
        root = str(tree.proc)
        self.assertTrue(admission.is_runs_worker(910, "t_mine", NOW, proc_root=root))
        self.assertFalse(admission.is_runs_worker(911, "t_mine", NOW, proc_root=root))
        self.assertFalse(admission.is_runs_worker(912, "t_mine", NOW, proc_root=root))

    def test_a_start_inside_the_skew_counts_as_the_worker(self):
        tree = FakeTree()
        self.addCleanup(tree.close)
        # 1 s before the run's second: the dispatcher records the run on a second clock.
        tree.process(920, ppid=1, starttime=(100 - 1) * 100)
        self.assertTrue(admission.is_runs_worker(920, "t_x", NOW, proc_root=str(tree.proc)))
        tree.process(921, ppid=1, starttime=(100 - 3) * 100)
        self.assertFalse(admission.is_runs_worker(921, "t_x", NOW, proc_root=str(tree.proc)))
        # No run start on the row and a boot time present: the command line is not consulted; no.
        self.assertFalse(admission.is_runs_worker(920, "t_x", None, proc_root=str(tree.proc)))

    def test_an_oversized_worker_is_terminated_and_its_card_blocked_with_capacity(self):
        conn = board()
        running(conn, "t_big", pid=4242)
        engine = FakeEngine(alive={4242})
        engine.dies_on_term = {4242}
        with self.assertLogs(admission.logger, level="WARNING") as logs:
            got = self.enforce(conn, engine, {4242: 1612})
        self.assertEqual(got, ["t_big"])
        self.assertEqual(engine.kills, [(4242, signal.SIGTERM)])
        self.assertEqual(status(conn, "t_big"), "blocked")
        payload = blocks(conn, "t_big")[0]
        self.assertEqual(payload["kind"], "capacity")
        self.assertEqual(
            payload["reason"],
            admission.capacity_reason("the worker used 1,612 MiB against the 1,536 MiB cap"),
        )
        row = conn.execute("SELECT claim_lock, worker_pid FROM tasks WHERE id = 't_big'").fetchone()
        self.assertEqual(tuple(row), (None, None))
        self.assertTrue(any("kanban admission: stopped worker 4242 of t_big" in line for line in logs.output))

    def test_a_worker_that_ignores_sigterm_gets_sigkill(self):
        conn = board()
        running(conn, "t_stuck", pid=77)
        engine = FakeEngine(alive={77})
        self.assertEqual(self.enforce(conn, engine, {77: 2000}), ["t_stuck"])
        self.assertEqual(engine.kills, [(77, signal.SIGTERM), (77, signal.SIGKILL)])
        self.assertNotIn(77, engine.alive)

    def test_a_worker_under_the_cap_is_left_alone(self):
        conn = board()
        running(conn, "t_ok", pid=11)
        engine = FakeEngine(alive={11})
        self.assertEqual(self.enforce(conn, engine, {11: 1536}), [])
        self.assertEqual((engine.kills, status(conn, "t_ok")), ([], "running"))

    def test_a_foreign_hosts_worker_is_never_signalled(self):
        conn = board()
        running(conn, "t_theirs", pid=12, lock=FOREIGN_LOCK)
        engine = FakeEngine(alive={12})
        self.assertEqual(self.enforce(conn, engine, {12: 9000}), [])
        self.assertEqual((engine.kills, status(conn, "t_theirs")), ([], "running"))

    def test_a_gone_worker_is_left_to_the_crash_sweep(self):
        conn = board()
        running(conn, "t_gone", pid=13)
        engine = FakeEngine()
        self.assertEqual(self.enforce(conn, engine, {}), [])
        self.assertEqual(status(conn, "t_gone"), "running")

    def test_the_test_hook_kill_is_used(self):
        conn = board()
        running(conn, "t_hook", pid=14)
        engine = FakeEngine(alive={14})
        seen = []

        def fake_kill(pid, sig):
            seen.append((pid, sig))
            engine.alive.discard(pid)

        self.assertEqual(self.enforce(conn, engine, {14: 5000}, signal_fn=fake_kill), ["t_hook"])
        self.assertEqual(seen, [(14, signal.SIGTERM)])
        self.assertEqual(engine.kills, [])

    def test_a_card_that_moved_on_is_not_blocked_but_the_worker_is_still_stopped(self):
        """The row was read, then the card got a new run before the block:
        ``expected_run_id`` keeps the block off the newer run."""
        conn = board()
        running(conn, "t_raced", pid=15)
        engine = FakeEngine(alive={15})
        engine.dies_on_term = {15}

        def measure_and_race(pid):
            conn.execute("UPDATE tasks SET current_run_id = 99 WHERE id = 't_raced'")
            return 5000 * MIB

        with self.assertLogs(admission.logger, level="WARNING") as logs:
            got = admission.enforce_worker_rss_caps(
                conn, settings=settings(worker_rss_cap_mib=1536), engine=engine, rss_fn=measure_and_race,
                identity_fn=lambda pid, task_id, run_started_at: True,
            )
        self.assertEqual(got, [])
        self.assertEqual(engine.kills, [(15, signal.SIGTERM)])
        self.assertEqual(status(conn, "t_raced"), "running")
        self.assertTrue(any("already left running" in line for line in logs.output))

    def test_descendants_that_outlive_the_worker_are_killed_by_identity(self):
        tree = FakeTree()
        self.addCleanup(tree.close)
        tree.process(1, ppid=0, rss_kib=1)
        tree.process(500, ppid=1, rss_kib=1000 * MIB)
        tree.process(501, ppid=500, rss_kib=600 * MIB, starttime=2000)
        tree.process(502, ppid=500, rss_kib=100 * MIB, starttime=3000)
        conn = board()
        running(conn, "t_tree", pid=500)
        engine = FakeEngine(alive={500, 501, 502})
        engine.dies_on_term = {500}

        def reuse_a_pid(pid, sig):
            # 502 dies with its parent and its pid is reused by something else
            # before the orphan sweep; 501 lives on and is the orphan.
            if pid == 500:
                tree.restart(502, starttime=9999)

        engine.on_kill = reuse_a_pid
        got = admission.enforce_worker_rss_caps(
            conn, settings=settings(worker_rss_cap_mib=1536), engine=engine, proc_root=str(tree.proc),
        )
        self.assertEqual(got, ["t_tree"])
        self.assertEqual(engine.kills, [(500, signal.SIGTERM), (501, signal.SIGKILL)])

    def test_the_tree_is_measured_from_proc_when_no_hook_is_given(self):
        tree = FakeTree()
        self.addCleanup(tree.close)
        tree.process(1, ppid=0, rss_kib=1)
        tree.process(600, ppid=1, rss_kib=1000 * MIB)
        tree.process(601, ppid=600, rss_kib=1000 * MIB)
        conn = board()
        running(conn, "t_sum", pid=600)
        engine = FakeEngine(alive={600, 601})
        engine.dies_on_term = {600, 601}
        got = admission.enforce_worker_rss_caps(
            conn, settings=settings(worker_rss_cap_mib=1536), engine=engine, proc_root=str(tree.proc),
        )
        self.assertEqual(got, ["t_sum"])
        self.assertIn("2,000 MiB against the 1,536 MiB cap", engine.blocks[0][2])

    def test_a_worker_the_ladder_cannot_signal_keeps_its_card_this_tick(self):
        """``os.kill`` is refused (EPERM): nothing was delivered, the worker is
        alive, so the card is not blocked under it; the next tick tries again."""
        conn = board()
        running(conn, "t_untouchable", pid=33)
        engine = FakeEngine(alive={33})
        engine.untouchable = {33}
        with self.assertLogs(admission.logger, level="WARNING") as logs:
            got = self.enforce(conn, engine, {33: 4000})
        self.assertEqual(got, [])
        self.assertEqual(engine.kills, [])
        self.assertEqual((status(conn, "t_untouchable"), engine.blocks), ("running", []))
        self.assertTrue(any("survived termination" in line for line in logs.output))

    def test_a_worker_in_uninterruptible_sleep_is_blocked_once_sigkill_is_delivered(self):
        """The probe right after the SIGKILL still sees the worker (a large
        address space being torn down, or a D state): a delivered SIGKILL is
        a death sentence, so the card is blocked rather than left to be
        reclaimed as a crash by a later tick that finds the pid gone."""
        conn = board()
        running(conn, "t_dstate", pid=34)
        engine = FakeEngine(alive={34})
        engine.immortal = {34}
        self.assertEqual(self.enforce(conn, engine, {34: 4000}), ["t_dstate"])
        self.assertEqual(engine.kills, [(34, signal.SIGTERM), (34, signal.SIGKILL)])
        self.assertEqual(status(conn, "t_dstate"), "blocked")

    def test_a_worker_that_dies_between_the_poll_and_the_sigkill_is_blocked(self):
        """Upstream reports nothing delivered when the SIGKILL finds no process;
        the pid is gone, so the card is blocked, not skipped."""
        conn = board()
        running(conn, "t_vanished", pid=35)
        engine = FakeEngine(alive={35})
        engine.vanish_at_sigkill = {35}
        self.assertEqual(self.enforce(conn, engine, {35: 4000}), ["t_vanished"])
        self.assertEqual(engine.kills, [(35, signal.SIGTERM)])
        self.assertEqual(status(conn, "t_vanished"), "blocked")

    def test_the_workers_process_group_is_swept_for_a_child_forked_in_the_grace(self):
        """A child forked into the worker's group after the descendants snapshot
        (the ssh client of a terminal command, say) is reparented when the
        worker dies, so the parent-field walk no longer finds it; its process
        group still names the worker, and that pass kills it."""
        tree = FakeTree()
        self.addCleanup(tree.close)
        tree.process(1, ppid=0, rss_kib=1)
        tree.process(700, ppid=1, rss_kib=2000 * MIB, pgrp=700)
        tree.process(701, ppid=1, rss_kib=100 * MIB, pgrp=700)
        tree.process(702, ppid=1, rss_kib=100 * MIB, pgrp=702)
        conn = board()
        running(conn, "t_grp", pid=700)
        engine = FakeEngine(alive={700, 701, 702})
        engine.dies_on_term = {700}
        got = admission.enforce_worker_rss_caps(
            conn, settings=settings(worker_rss_cap_mib=1536), engine=engine, proc_root=str(tree.proc),
        )
        self.assertEqual(got, ["t_grp"])
        self.assertEqual(engine.kills, [(700, signal.SIGTERM), (701, signal.SIGKILL)])

    def test_a_failing_block_never_raises_into_the_tick(self):
        conn = board()
        running(conn, "t_a", pid=21)
        running(conn, "t_b", pid=22)
        engine = FakeEngine(alive={21, 22})
        engine.dies_on_term = {21, 22}
        real_block = engine.block_task

        def flaky(conn_, task_id, **kwargs):
            if task_id == "t_a":
                raise sqlite3.OperationalError("database is locked")
            return real_block(conn_, task_id, **kwargs)

        engine.block_task = flaky
        with self.assertLogs(admission.logger, level="WARNING"):
            got = self.enforce(conn, engine, {21: 5000, 22: 5000})
        self.assertEqual(got, ["t_b"])


# ---------------------------------------------------------------------------
# The overdue refusal
# ---------------------------------------------------------------------------


class OverdueTest(unittest.TestCase):
    def refuse(self, conn, res=None, *, limited=True, engine=None, bound=120, **kwargs):
        return admission.refuse_overdue_ready(
            conn, res if res is not None else result(), limited_by_capacity=limited,
            settings=settings(queue_bound_seconds=bound), now=NOW, engine=engine or FakeEngine(), **kwargs,
        )

    def test_a_card_past_the_bound_is_refused_with_the_wait_in_the_reason(self):
        conn = board()
        ready(conn, "t_old", age=300)
        res = result()
        with self.assertLogs(admission.logger, level="WARNING") as logs:
            got = self.refuse(conn, res)
        self.assertEqual(got, ["t_old"])
        self.assertEqual(status(conn, "t_old"), "blocked")
        payload = blocks(conn, "t_old")[0]
        self.assertEqual(payload["kind"], "capacity")
        self.assertEqual(
            payload["reason"],
            admission.capacity_reason("the card waited 300 s for a worker slot against the 120 s bound"),
        )
        self.assertEqual(res.auto_blocked, ["t_old"])
        self.assertEqual(getattr(res, admission.RESULT_ATTR)["refused"], ["t_old"])
        self.assertTrue(any("kanban admission: refused t_old" in line for line in logs.output))

    def test_the_bound_is_inclusive_and_a_fresh_card_waits_on(self):
        conn = board()
        ready(conn, "t_at", age=120)
        ready(conn, "t_young", age=119)
        self.assertEqual(self.refuse(conn), ["t_at"])
        self.assertEqual(status(conn, "t_young"), "ready")

    def test_nothing_is_refused_when_the_tick_was_not_capacity_limited(self):
        conn = board()
        ready(conn, "t_old", age=3000)
        self.assertEqual(self.refuse(conn, limited=False), [])
        self.assertEqual(status(conn, "t_old"), "ready")

    def test_a_dry_run_refuses_nothing(self):
        conn = board()
        ready(conn, "t_old", age=3000)
        self.assertEqual(self.refuse(conn, dry_run=True), [])
        self.assertEqual(status(conn, "t_old"), "ready")

    def test_the_four_own_reason_exclusions_and_the_spawned(self):
        conn = board()
        for tid in ("t_unassigned", "t_nonspawnable", "t_guarded", "t_capped", "t_spawned", "t_waiting"):
            ready(conn, tid, age=600)
        res = result(
            skipped_unassigned=["t_unassigned"], skipped_nonspawnable=["t_nonspawnable"],
            respawn_guarded=[("t_guarded", "recent_success")],
            skipped_per_profile_capped=[("t_capped", "platform", 2)],
            spawned=[("t_spawned", "platform", "/ws")],
        )
        self.assertEqual(self.refuse(conn, res), ["t_waiting"])
        for tid in ("t_unassigned", "t_nonspawnable", "t_guarded", "t_capped", "t_spawned"):
            with self.subTest(tid=tid):
                self.assertEqual(status(conn, tid), "ready")

    def test_a_row_the_loop_never_reached_is_asked_the_same_questions(self):
        conn = board()
        ready(conn, "t_unrouted", age=600, assignee=None)
        ready(conn, "t_blank", age=600, assignee="")
        ready(conn, "t_no_profile", age=600, assignee="orion-cc")
        ready(conn, "t_guarded", age=600)
        ready(conn, "t_waiting", age=600)
        engine = FakeEngine(profiles={"platform"}, guarded={"t_guarded": "active_pr"})
        self.assertEqual(self.refuse(conn, engine=engine), ["t_waiting"])
        for tid in ("t_unrouted", "t_blank", "t_no_profile", "t_guarded"):
            with self.subTest(tid=tid):
                self.assertEqual(status(conn, tid), "ready")

    def test_a_card_at_its_profiles_cap_is_not_refused(self):
        """Upstream's per-profile cap is a quota, not capacity; a row the loop
        never reached is asked that question too, from the running counts."""
        conn = board()
        running(conn, "t_run_a", pid=51)
        running(conn, "t_run_b", pid=52)
        ready(conn, "t_capped", age=600, assignee="platform")
        ready(conn, "t_other", age=600, assignee="orion")
        self.assertEqual(self.refuse(conn, per_profile_cap=2), ["t_other"])
        self.assertEqual(status(conn, "t_capped"), "ready")
        self.assertEqual(self.refuse(conn, per_profile_cap=3), ["t_capped"])

    def test_a_per_profile_cap_that_is_not_a_positive_int_asks_nothing(self):
        conn = board()
        running(conn, "t_run", pid=53)
        ready(conn, "t_waiting", age=600, assignee="platform")
        for cap in (None, 0, -1, "two"):
            with self.subTest(cap=cap):
                conn.execute("UPDATE tasks SET status = 'ready' WHERE id = 't_waiting'")
                self.assertEqual(self.refuse(conn, per_profile_cap=cap), ["t_waiting"])

    def test_an_engine_that_cannot_name_profiles_trusts_the_assignee(self):
        conn = board()
        ready(conn, "t_any", age=600, assignee="whatever")
        self.assertEqual(self.refuse(conn, engine=FakeEngine(profiles=None)), ["t_any"])

    def test_the_wait_starts_when_the_card_last_entered_the_lane(self):
        conn = board()
        ready(conn, "t_retry", age=3000)
        ended_run(conn, "t_retry", NOW - 30)
        ready(conn, "t_unblocked", age=3000)
        event(conn, "t_unblocked", "unblocked", NOW - 10)
        ready(conn, "t_promoted", age=3000)
        event(conn, "t_promoted", "promoted", NOW - 200)
        ready(conn, "t_noise", age=3000)
        event(conn, "t_noise", "heartbeat", NOW - 5)
        event(conn, "t_noise", "queued", NOW - 5)
        got = self.refuse(conn)
        self.assertEqual(sorted(got), ["t_noise", "t_promoted"])
        self.assertIn("waited 200 s", blocks(conn, "t_promoted")[0]["reason"])
        self.assertEqual((status(conn, "t_retry"), status(conn, "t_unblocked")), ("ready", "ready"))

    def test_a_review_sent_back_or_reopened_waits_from_that_event(self):
        """Upstream's ``request_changes``, ``reopen_review_task``, a manual
        promotion, an assignment and a reclaim return a card to ready with an
        event and no new run, so the wait starts at the event, not at the old
        run's end. With ``promoted`` and ``unblocked`` above, that is every
        kind in ``READY_ENTRY_EVENT_KINDS``."""
        conn = board()
        covered = {"promoted", "unblocked"}
        for tid, kind in (
            ("t_changes", "changes_requested"), ("t_reopened", "review_reopened"),
            ("t_manual", "promoted_manual"), ("t_assigned", "assigned"), ("t_reclaimed", "reclaimed"),
        ):
            covered.add(kind)
            ready(conn, tid, age=90000)
            ended_run(conn, tid, NOW - 21600)
            event(conn, tid, kind, NOW - 30)
        ready(conn, "t_reopened_old", age=90000)
        ended_run(conn, "t_reopened_old", NOW - 21600)
        event(conn, "t_reopened_old", "review_reopened", NOW - 200)
        got = self.refuse(conn)
        self.assertEqual(got, ["t_reopened_old"])
        self.assertIn("waited 200 s", blocks(conn, "t_reopened_old")[0]["reason"])
        for tid in ("t_changes", "t_reopened", "t_manual", "t_assigned", "t_reclaimed"):
            with self.subTest(tid=tid):
                self.assertEqual(status(conn, tid), "ready")
        self.assertEqual(covered, set(admission.READY_ENTRY_EVENT_KINDS))

    def test_a_card_told_it_is_queued_this_tick_is_refused_no_earlier_than_the_next(self):
        """At the end of a tick the sibling patch has already posted the queued
        line; the refusal waits for the next limited tick so the thread reads
        them in that order."""
        conn = board()
        ready(conn, "t_noticed", age=600)
        self.assertEqual(self.refuse(conn, result(queued_noticed=["t_noticed"])), [])
        self.assertEqual(status(conn, "t_noticed"), "ready")
        self.assertEqual(self.refuse(conn, result(queued_noticed=[])), ["t_noticed"])

    def test_claimed_running_and_blocked_rows_are_never_candidates(self):
        conn = board()
        ready(conn, "t_claimed", age=600)
        conn.execute("UPDATE tasks SET claim_lock = ? WHERE id = 't_claimed'", (OWN_LOCK,))
        running(conn, "t_running", pid=5)
        conn.execute("INSERT INTO tasks (id, status, assignee, created_at) VALUES ('t_done', 'done', 'p', ?)", (0,))
        self.assertEqual(self.refuse(conn), [])

    def test_the_bound_setting_is_honoured(self):
        conn = board()
        ready(conn, "t_old", age=61)
        self.assertEqual(self.refuse(conn, bound=60), ["t_old"])
        self.assertIn("against the 60 s bound", blocks(conn, "t_old")[0]["reason"])

    def test_one_failing_block_does_not_stop_the_others(self):
        conn = board()
        ready(conn, "t_a", age=600)
        ready(conn, "t_b", age=600)
        engine = FakeEngine()
        real_block = engine.block_task

        def flaky(conn_, task_id, **kwargs):
            if task_id == "t_a":
                raise sqlite3.OperationalError("database is locked")
            return real_block(conn_, task_id, **kwargs)

        engine.block_task = flaky
        with self.assertLogs(admission.logger, level="WARNING"):
            got = self.refuse(conn, engine=engine)
        self.assertEqual(got, ["t_b"])

    def test_a_result_without_the_upstream_lists_is_tolerated(self):
        conn = board()
        ready(conn, "t_old", age=600)
        bare = SimpleNamespace()
        self.assertEqual(self.refuse(conn, bare), ["t_old"])
        self.assertEqual(getattr(bare, admission.RESULT_ATTR)["refused"], ["t_old"])


# ---------------------------------------------------------------------------
# The applier
# ---------------------------------------------------------------------------


class ApplierTest(unittest.TestCase):
    """The applier is the thing that fails the build, so exercise it directly."""

    def _tree(self, bodies=None):
        root = Path(tempfile.mkdtemp())
        sources = verifier.fixture_sources()
        sources.update(bodies or {})
        for relative, source in sources.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source)
        return root

    def _read(self, root):
        return {relative: (root / relative).read_text() for relative in (applier.DB_RELATIVE, applier.DISPATCH_RELATIVE)}

    def test_the_fixtures_are_faithful_stand_ins(self):
        """Every anchor exactly once in the file it names, nowhere in the other,
        and both fixtures parse, as does the pinned upstream text they copy."""
        sources = verifier.fixture_sources()
        for relative in (applier.DB_RELATIVE, applier.DISPATCH_RELATIVE):
            ast.parse(sources[relative])
        for relative, label, anchor, _patched in applier.EDITS:
            with self.subTest(anchor=label):
                self.assertEqual(sources[relative].count(anchor), 1)
                other = applier.DB_RELATIVE if relative == applier.DISPATCH_RELATIVE else applier.DISPATCH_RELATIVE
                self.assertEqual(sources[other].count(anchor), 0)

    def test_the_applied_tree_carries_every_marker(self):
        root = self._tree()
        self.assertTrue(applier.apply(root))
        got = self._read(root)
        for marker in applier.DISPATCH_MARKERS:
            self.assertIn(marker, got[applier.DISPATCH_RELATIVE])
        self.assertIn(applier.KINDS_BUILD_MARKER, got[applier.DB_RELATIVE])
        self.assertEqual(got[applier.DISPATCH_RELATIVE].count(applier.REFUSE_BUILD_MARKER), 2)
        self.assertTrue(got[applier.DISPATCH_RELATIVE].endswith(applier.TRAILER))
        for relative, source in got.items():
            compile(source, relative, "exec")

    def test_the_capacity_kind_is_the_modules(self):
        self.assertIn(f'"{admission.CAPACITY_KIND}"', applier.KINDS_PATCHED)
        self.assertEqual(admission.CAPACITY_KIND, "capacity")

    def test_a_second_run_is_a_no_op(self):
        root = self._tree()
        applier.apply(root)
        once = self._read(root)
        self.assertFalse(applier.apply(root))
        self.assertEqual(self._read(root), once)

    def test_a_half_patched_tree_is_refused_and_left_alone(self):
        root = self._tree()
        applier.apply(root)
        patched = self._read(root)
        (root / applier.DB_RELATIVE).write_text(verifier.fixture_sources()[applier.DB_RELATIVE])
        with self.assertRaises(SystemExit) as caught:
            applier.apply(root)
        self.assertIn("half-patched", str(caught.exception))
        self.assertEqual(self._read(root)[applier.DISPATCH_RELATIVE], patched[applier.DISPATCH_RELATIVE])
        self.assertEqual(self._read(root)[applier.DB_RELATIVE], verifier.fixture_sources()[applier.DB_RELATIVE])

    def test_every_anchor_is_load_bearing(self):
        for relative, label, anchor, _patched in applier.EDITS:
            with self.subTest(anchor=label):
                body = verifier.fixture_sources()[relative].replace(anchor, "", 1)
                root = self._tree({relative: body})
                with self.assertRaises(SystemExit):
                    applier.apply(root)
                self.assertEqual(self._read(root)[relative], body)

    def test_a_duplicate_anchor_fails_the_build(self):
        for relative, label, anchor, _patched in applier.EDITS:
            with self.subTest(anchor=label):
                root = self._tree({relative: verifier.fixture_sources()[relative] + anchor})
                with self.assertRaises(SystemExit):
                    applier.apply(root)

    def test_a_missing_anchor_in_the_second_file_writes_neither(self):
        pristine = verifier.fixture_sources()
        body = pristine[applier.DB_RELATIVE].replace(applier.KINDS_ANCHOR, "", 1)
        root = self._tree({applier.DB_RELATIVE: body})
        with self.assertRaises(SystemExit):
            applier.apply(root)
        self.assertEqual(self._read(root)[applier.DISPATCH_RELATIVE], pristine[applier.DISPATCH_RELATIVE])
        self.assertEqual(self._read(root)[applier.DB_RELATIVE], body)

    def test_a_result_that_does_not_compile_writes_neither_file(self):
        pristine = verifier.fixture_sources()
        body = pristine[applier.DISPATCH_RELATIVE].replace(
            "def _lane_rows(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:\n",
            "def _lane_rows(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:\n    continue\n",
            1,
        )
        self.assertNotEqual(body, pristine[applier.DISPATCH_RELATIVE])
        root = self._tree({applier.DISPATCH_RELATIVE: body})
        with self.assertRaises(SystemExit):
            applier.apply(root)
        self.assertEqual(self._read(root)[applier.DISPATCH_RELATIVE], body)
        self.assertEqual(self._read(root)[applier.DB_RELATIVE], pristine[applier.DB_RELATIVE])

    def test_a_missing_target_file_fails_the_build(self):
        with self.assertRaises(SystemExit):
            applier.apply(Path(tempfile.mkdtemp()))

    def test_the_fanout_reaper_anchor_survives(self):
        """apply_kanban_fanout_reaper runs later in the build on the same line."""
        import apply_kanban_fanout_reaper as reaper

        self.assertEqual(applier.RSS_CAP_ANCHOR, reaper.RECLAIM_ANCHOR)
        root = self._tree()
        applier.apply(root)
        dispatch = self._read(root)[applier.DISPATCH_RELATIVE]
        self.assertEqual(dispatch.count(reaper.RECLAIM_ANCHOR), 1)
        self.assertNotIn("_kanban_reap_overdue_fanout(conn)", dispatch)

    def test_the_scheduling_patch_leaves_every_anchor_in_place(self):
        """The Dockerfile applies this after apply_kanban_scheduling: none of
        its replacements may consume or duplicate an anchor here."""
        import apply_kanban_scheduling as scheduling

        for _relative, _label, anchor, _patched in applier.EDITS:
            for relative, label, their_anchor, their_patched in scheduling.EDITS:
                with self.subTest(ours=_label, theirs=label):
                    self.assertEqual(their_patched.count(anchor), their_anchor.count(anchor))

    def test_the_event_kind_check_reads_call_sites_not_words(self):
        """A kind counts when ``_append_event`` is called with it, as a literal
        or a module constant; a payload key, a comment or a status that spells
        the word does not."""
        with tempfile.TemporaryDirectory() as tmp:
            module = Path(tmp) / "writer.py"
            module.write_text(
                "RECLAIM_EVENT_KIND = \"reclaimed\"\n"
                "def f(conn, tid):\n"
                "    _append_event(conn, tid, \"promoted\", {\"unblocked\": 1})\n"
                "    _kb._append_event(conn, tid, RECLAIM_EVENT_KIND, None, run_id=3)\n"
                "    # the assigned event is written elsewhere\n"
                "    status = 'assigned'\n"
                "    log(\"review_reopened\")\n"
            )
            self.assertEqual(verifier.event_kinds_written([module]), {"promoted", "reclaimed"})
            self.assertEqual(verifier.event_kinds_written([Path(tmp) / "absent.py"]), set())

    def test_the_dockerfile_greps_for_the_markers_this_file_defines(self):
        dockerfile = Path(applier.__file__).parents[1] / "Dockerfile"
        if not dockerfile.exists():
            self.skipTest("Dockerfile not beside the patches")
        text = dockerfile.read_text()
        for marker in applier.BUILD_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(marker, text)
        self.assertIn("kanban_admission.py /opt/hermes/hermes_cli/kanban_admission.py", text)
        self.assertIn("verify_kanban_admission.py", text)

    def test_the_edit_count_matches_what_the_prose_claims(self):
        self.assertEqual(len(applier.EDITS), 6)
        self.assertIn("Five edits (six anchors)", applier.__doc__)


class CapSpawnBudgetTest(unittest.TestCase):
    """The per-tick headroom cap: one new worker per WORKER_RESERVE_MIB of headroom."""

    def _result(self, budget_mib, usage_mib):
        result = types.SimpleNamespace()
        setattr(result, admission.RESULT_ATTR, {
            admission.RECORD_BUDGET_MIB: budget_mib, admission.RECORD_USAGE_MIB: usage_mib,
            admission.RECORD_LIMITED: False,
        })
        return result

    def test_without_a_reading_the_slot_budget_is_unchanged(self):
        result = types.SimpleNamespace()
        self.assertEqual(admission.cap_spawn_budget(result, 6), 6)
        self.assertIsNone(admission.cap_spawn_budget(result, None))

    def test_enough_headroom_leaves_the_slot_budget_alone(self):
        result = self._result(4096, 800)  # 3,296 MiB free -> 6 workers at 512 each
        self.assertEqual(admission.cap_spawn_budget(result, 6), 6)
        self.assertFalse(getattr(result, admission.RESULT_ATTR)[admission.RECORD_LIMITED])

    def test_little_headroom_caps_the_tick_without_deciding_it(self):
        result = self._result(2048, 1300)  # 748 MiB free -> 1 worker
        self.assertEqual(admission.cap_spawn_budget(result, 6), 1)
        record = getattr(result, admission.RESULT_ATTR)
        self.assertFalse(record[admission.RECORD_LIMITED])  # the loop may still spend the one
        self.assertEqual(record[admission.RECORD_SPAWN_CAP], 1)
        # tick_was_limited sees the loop exhaust the cap.
        self.assertTrue(admission.tick_was_limited(result, spawn_budget=1, spawned=1))
        self.assertFalse(admission.tick_was_limited(result, spawn_budget=1, spawned=0))

    def test_an_uncapped_slot_budget_is_bounded_too(self):
        result = self._result(2048, 800)  # 1,248 MiB free -> 2 workers
        self.assertEqual(admission.cap_spawn_budget(result, None), 2)

    def test_usage_past_the_budget_spawns_nothing(self):
        result = self._result(2048, 2100)
        self.assertEqual(admission.cap_spawn_budget(result, 6), 0)
        self.assertTrue(getattr(result, admission.RESULT_ATTR)[admission.RECORD_LIMITED])

    def test_a_spent_cap_warns_every_tick_and_a_usable_cap_is_debug(self):
        """A default install's 2 GiB budget caps every tick under its slot budget,
        so that line is debug; a cap of zero is the tick's decision and warns."""
        with self.assertLogs(admission.logger, level="DEBUG") as logs:
            admission.cap_spawn_budget(self._result(2048, 800), 6)
        self.assertEqual([r.levelname for r in logs.records], ["DEBUG"])
        self.assertIn("bounds this tick to 2 new worker(s) (slot budget 6)", logs.output[0])
        with self.assertLogs(admission.logger, level="DEBUG") as logs:
            admission.cap_spawn_budget(self._result(640, 443), 5)
        self.assertEqual([r.levelname for r in logs.records], ["WARNING"])
        self.assertIn("headroom of 197 MiB bounds this tick to 0 new worker(s)", logs.output[0])


if __name__ == "__main__":
    unittest.main()
