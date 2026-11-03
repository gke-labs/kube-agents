"""Unit tests for the adaptive kanban cap (kanban_aimd.py) and edit 11 of apply_kanban_scheduling.py.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The limiter is driven with a fake clock and a fake probe; the dispatcher side
against a real sqlite ``task_events`` table and, for the tick itself, through
edits 8 and 11 spliced into an upstream-shaped ``_tick_spawn_budget``, as
``test_kanban_priority`` does for edits 7-10. The two names this module
shares with files in other images, the broker's busy line and the state
table the board snapshot reads, are pinned against those files' source.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import apply_kanban_scheduling as scheduling
import kanban_aimd as ka

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
CREDENTIAL_PROXY_CLIENT = REPO / "agents" / "platform" / "scripts" / "credential_proxy_client.py"
BOARD_SNAPSHOT = REPO / "agents" / "platform" / "scripts" / "board_snapshot.py"

SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, status TEXT NOT NULL, priority INTEGER DEFAULT 0,
    assignee TEXT, session_id TEXT, started_at INTEGER, claim_lock TEXT, created_at INTEGER
);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, run_id INTEGER,
    kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL
);
"""


class Clock:
    def __init__(self, now=1_000.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


def board():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def pressure(conn, task_id="t1", when=1):
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
        (task_id, ka.PRESSURE_KIND, json.dumps({"reason": "test"}), when),
    )


# ---------------------------------------------------------------------------
# The limiter
# ---------------------------------------------------------------------------


class LimiterTest(unittest.TestCase):
    def test_the_acceptance_case(self):
        """C_max 4: 4 -> 1 on a signal, held for 60s, then 1 -> 2 -> 3 -> 4 over three clean minutes."""
        clock = Clock(0)
        limiter = ka.AimdConcurrencyLimiter(4, clock=clock, cooldown=60)
        self.assertEqual(limiter.effective, 4)
        self.assertEqual(limiter.tick(pressure=True), 1)
        for t in (1, 30, 59, 59.9):
            clock.now = t
            self.assertEqual(limiter.tick(pressure=False), 1, t)
        clock.now = 60
        self.assertEqual(limiter.tick(pressure=False), 2)
        clock.now = 119
        self.assertEqual(limiter.tick(pressure=False), 2)
        clock.now = 120
        self.assertEqual(limiter.tick(pressure=False), 3)
        clock.now = 180
        self.assertEqual(limiter.tick(pressure=False), 4)
        self.assertIsNone(limiter.cooldown_until)
        clock.now = 10_000
        self.assertEqual(limiter.tick(pressure=False), 4, "the ceiling holds")

    def test_a_signal_during_the_cooldown_restarts_it(self):
        clock = Clock(0)
        limiter = ka.AimdConcurrencyLimiter(4, clock=clock, cooldown=60)
        limiter.tick(pressure=True)
        clock.now = 45
        self.assertEqual(limiter.tick(pressure=True), 1)
        clock.now = 60
        self.assertEqual(limiter.tick(pressure=False), 1, "the first deadline no longer counts")
        clock.now = 105
        self.assertEqual(limiter.tick(pressure=False), 2)

    def test_a_signal_while_recovering_drops_straight_back_to_the_floor(self):
        clock = Clock(0)
        limiter = ka.AimdConcurrencyLimiter(4, clock=clock, cooldown=60)
        limiter.tick(pressure=True)
        clock.now = 130
        limiter.tick(pressure=False)
        clock.now = 130
        self.assertEqual(limiter.effective, 2)
        self.assertEqual(limiter.tick(pressure=True), 1)

    def test_one_step_per_tick_however_long_the_pause(self):
        """A dispatcher that slept through three cooldowns comes back one slot at a time."""
        clock = Clock(0)
        limiter = ka.AimdConcurrencyLimiter(4, clock=clock, cooldown=60)
        limiter.tick(pressure=True)
        clock.now = 1_000
        self.assertEqual(limiter.tick(pressure=False), 2)
        self.assertEqual(limiter.tick(pressure=False), 2)
        clock.now = 1_060
        self.assertEqual(limiter.tick(pressure=False), 3)

    def test_the_probe_is_asked_when_no_verdict_is_passed(self):
        verdicts = iter([True, False])
        limiter = ka.AimdConcurrencyLimiter(3, clock=Clock(0), cooldown=60, probe=lambda: next(verdicts))
        self.assertEqual(limiter.tick(), 1)
        self.assertEqual(limiter.tick(), 1)

    def test_a_lowered_ceiling_clamps_the_effective_cap(self):
        restored = ka.AimdConcurrencyLimiter(2, clock=Clock(0), cooldown=60, effective=6, cooldown_until=None)
        self.assertEqual(restored.effective, 2)

    def test_a_raised_ceiling_is_climbed_to_not_jumped_to(self):
        clock = Clock(0)
        limiter = ka.AimdConcurrencyLimiter(2, clock=clock, cooldown=60)
        limiter.tick(pressure=True)
        clock.now = 60
        self.assertEqual(limiter.tick(pressure=False), 2)
        self.assertIsNone(limiter.cooldown_until)
        restored = ka.AimdConcurrencyLimiter(4, clock=clock, cooldown=60, effective=2, cooldown_until=None)
        self.assertEqual(restored.effective, 2)
        clock.now = 10_000
        # No cooldown was armed at the old ceiling, so the first clean tick
        # arms one rather than stepping; the steps follow a cooldown apart.
        self.assertEqual(restored.tick(pressure=False), 2)
        clock.now = 10_059
        self.assertEqual(restored.tick(pressure=False), 2)
        clock.now = 10_060
        self.assertEqual(restored.tick(pressure=False), 3)
        clock.now = 10_120
        self.assertEqual(restored.tick(pressure=False), 4)

    def test_the_floor_is_one_whatever_the_ceiling(self):
        self.assertEqual(ka.AimdConcurrencyLimiter(0, clock=Clock(0), cooldown=60).c_max, 1)
        self.assertEqual(ka.AimdConcurrencyLimiter(1, clock=Clock(0), cooldown=60).tick(pressure=True), 1)

    def test_the_cooldown_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {ka.COOLDOWN_ENV: "15"}):
            self.assertEqual(ka.cooldown_seconds(), 15.0)
            self.assertEqual(ka.AimdConcurrencyLimiter(4, clock=Clock(0)).cooldown, 15.0)
        # "inf" would arm a deadline that never lapses, "nan" one that SQLite
        # stores as NULL and the next tick arms again; both pin the cap at the floor.
        for bad in ("", "  ", "soon", "0", "-5", "inf", "-inf", "Infinity", "1e999", "nan"):
            with self.subTest(value=bad), mock.patch.dict(os.environ, {ka.COOLDOWN_ENV: bad}):
                self.assertEqual(ka.cooldown_seconds(), float(ka.DEFAULT_COOLDOWN_SECONDS))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ka.cooldown_seconds(), 60.0)


# ---------------------------------------------------------------------------
# The shell probe
# ---------------------------------------------------------------------------


class ClassifyMemoryTest(unittest.TestCase):
    def test_no_limit_is_a_no_op(self):
        self.assertIsNone(ka.classify_memory("123456\nmax\n"))

    def test_a_missing_limit_is_a_no_op(self):
        self.assertIsNone(ka.classify_memory("123456\n"))
        self.assertIsNone(ka.classify_memory(""))

    def test_garbage_is_a_no_op(self):
        self.assertIsNone(ka.classify_memory("lots\nsome\n"))
        self.assertIsNone(ka.classify_memory("5\n0\n"))

    def test_at_or_under_the_ratio_is_fine(self):
        self.assertIsNone(ka.classify_memory("750\n1000\n"))
        self.assertIsNone(ka.classify_memory("10\n1000\n"))

    def test_above_the_ratio_is_pressure_with_the_percentage(self):
        self.assertEqual(ka.classify_memory("751\n1000\n"), f"{ka.REASON_SHELL_MEMORY} 75%")
        self.assertEqual(ka.classify_memory("990\n1000\n"), f"{ka.REASON_SHELL_MEMORY} 99%")

    def test_page_cache_and_reclaimable_slab_are_not_pressure(self):
        """A shell that has cloned a few repositories sits near its limit in cache while healthy."""
        stat = "anon 200\nfile 700\nactive_file 300\ninactive_file 400\nslab_reclaimable 50\nslab_unreclaimable 20\n"
        self.assertIsNone(ka.classify_memory(f"990\n1000\n{stat}"))

    def test_anonymous_memory_above_the_ratio_is_pressure_after_the_cache_is_taken_off(self):
        stat = "anon 800\nactive_file 100\ninactive_file 50\nslab_reclaimable 0\n"
        self.assertEqual(ka.classify_memory(f"960\n1000\n{stat}"), f"{ka.REASON_SHELL_MEMORY} 81%")

    def test_a_malformed_stat_line_counts_as_nothing_reclaimable(self):
        self.assertEqual(ka.classify_memory("800\n1000\nactive_file lots\nfile\n"), f"{ka.REASON_SHELL_MEMORY} 80%")


class ProbeTest(unittest.TestCase):
    TARGET = ("agent", "platform-agent-shell-0.platform-agent-shell.ns.svc", 2222, "/etc/sandbox-ssh/id")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        patcher = mock.patch.object(ka.tempfile, "gettempdir", return_value=str(self.tmp))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []

    def runner(self, returncode=0, stdout="", raise_timeout=False):
        def run(cmd, **kwargs):
            self.calls.append((cmd, kwargs))
            if raise_timeout:
                raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
            return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

        return run

    def socket(self):
        path = ka.control_socket(*self.TARGET[:3])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def test_the_socket_path_is_the_one_hermes_derives(self):
        import hashlib

        expected = hashlib.sha256(b"agent@platform-agent-shell-0.platform-agent-shell.ns.svc:2222").hexdigest()[:16]
        self.assertEqual(ka.control_socket(*self.TARGET[:3]), self.tmp / "hermes-ssh" / f"{expected}.sock")

    def test_no_terminal_target_means_no_probe(self):
        with mock.patch.object(ka, "ssh_target", return_value=None):
            self.assertIsNone(ka.probe_shell_memory(runner=self.runner()))
        self.assertEqual(self.calls, [])

    def test_no_master_socket_means_no_connection(self):
        self.assertIsNone(ka.probe_shell_memory(self.TARGET, runner=self.runner(0, "9\n10\n")))
        self.assertEqual(self.calls, [])

    def test_the_probe_rides_the_master_and_opens_none(self):
        socket = self.socket()
        self.assertIsNone(ka.probe_shell_memory(self.TARGET, runner=self.runner(0, "100\n1000\n")))
        (cmd, kwargs), = self.calls
        self.assertEqual(cmd[0], "ssh")
        self.assertIn(f"ControlPath={socket}", cmd)
        self.assertIn("ControlMaster=no", cmd)
        self.assertIn("BatchMode=yes", cmd)
        self.assertEqual(cmd[cmd.index("-p") + 1], "2222")
        self.assertEqual(cmd[cmd.index("-i") + 1], "/etc/sandbox-ssh/id")
        self.assertEqual(cmd[-2:], ["agent@platform-agent-shell-0.platform-agent-shell.ns.svc", ka.PROBE_COMMAND])
        self.assertEqual(kwargs["timeout"], ka.PROBE_TIMEOUT_SECONDS)
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)

    def test_the_default_port_and_no_key_add_no_flags(self):
        target = ("agent", "shell", 22, "")
        path = ka.control_socket(*target[:3])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        ka.probe_shell_memory(target, runner=self.runner(0, "1\nmax\n"))
        (cmd, _), = self.calls
        self.assertNotIn("-p", cmd)
        self.assertNotIn("-i", cmd)

    def test_memory_above_the_ratio_is_pressure(self):
        self.socket()
        self.assertEqual(
            ka.probe_shell_memory(self.TARGET, runner=self.runner(0, "800\n1000\n")),
            f"{ka.REASON_SHELL_MEMORY} 80%",
        )

    def test_an_ssh_failure_is_pressure(self):
        self.socket()
        self.assertEqual(ka.probe_shell_memory(self.TARGET, runner=self.runner(255)), ka.REASON_PROBE_EXIT)

    def test_a_remote_cat_failure_is_a_no_op(self):
        self.socket()
        self.assertIsNone(ka.probe_shell_memory(self.TARGET, runner=self.runner(1)))

    def test_a_timeout_is_logged_and_counts_for_nothing(self):
        self.socket()
        with self.assertLogs(ka.logger, level=logging.WARNING) as logs:
            self.assertIsNone(ka.probe_shell_memory(self.TARGET, runner=self.runner(raise_timeout=True)))
        self.assertIn("timed out", logs.output[0])

    def test_the_target_is_read_from_the_terminal_config(self):
        config = {"terminal": {"backend": "ssh", "ssh_host": "h", "ssh_user": "u", "ssh_port": "2222", "ssh_key": "k"}}
        fake = SimpleNamespace(load_config_readonly=lambda: config)
        with mock.patch.dict("sys.modules", {"hermes_cli": SimpleNamespace(config=fake), "hermes_cli.config": fake}):
            self.assertEqual(ka.ssh_target(), ("u", "h", 2222, "k"))
            config["terminal"]["backend"] = "local"
            self.assertIsNone(ka.ssh_target())
            config["terminal"] = {"backend": "ssh"}
            self.assertIsNone(ka.ssh_target())


# ---------------------------------------------------------------------------
# The dispatcher side
# ---------------------------------------------------------------------------


class EffectiveCapTest(unittest.TestCase):
    def setUp(self):
        self.conn = board()
        self.addCleanup(self.conn.close)
        self.clock = Clock(1_000)
        self.verdict = None

    def cap(self, configured=4):
        return ka.effective_max_in_progress(self.conn, configured, "default", now=self.clock(), probe=lambda: self.verdict)

    def state(self):
        return ka.load_state(self.conn)

    def running(self, task_id="r1"):
        self.conn.execute("INSERT INTO tasks (id, status) VALUES (?, 'running')", (task_id,))

    def test_an_uncapped_dispatcher_stays_uncapped(self):
        self.assertIsNone(self.cap(None))
        self.assertIsNone(self.state())

    def test_the_first_tick_writes_the_state_and_ignores_old_rows(self):
        pressure(self.conn, when=1)
        pressure(self.conn, when=2)
        self.assertEqual(self.cap(4), 4)
        state = self.state()
        self.assertEqual((state["configured"], state["effective"], state["last_event_id"]), (4, 4, 2))
        self.assertIsNone(state["cooldown_until"])

    def test_a_worker_signal_drops_the_cap_and_is_read_once(self):
        self.cap(4)
        pressure(self.conn, when=5)
        with self.assertLogs(ka.logger, level=logging.WARNING) as logs:
            self.assertEqual(self.cap(4), 1)
        self.assertIn("1 worker signal(s)", logs.output[0])
        self.assertIn("4 -> 1 of 4", logs.output[0])
        self.assertEqual(self.state()["cooldown_until"], 1_060.0)
        self.clock.advance(5)
        self.assertEqual(self.cap(4), 1, "the same row is not a second signal")
        self.assertEqual(self.state()["cooldown_until"], 1_060.0)

    def test_recovery_climbs_one_slot_per_clean_cooldown(self):
        self.cap(4)
        pressure(self.conn, when=5)
        self.assertEqual(self.cap(4), 1)
        steps = []
        for seconds in (59, 1, 60, 60):
            self.clock.advance(seconds)
            steps.append(self.cap(4))
        self.assertEqual(steps, [1, 2, 3, 4])

    def test_the_state_survives_a_new_instance(self):
        """A restart reads the row back: the cap stays down and the cooldown deadline holds."""
        self.cap(4)
        pressure(self.conn, when=5)
        self.cap(4)
        self.clock.advance(30)
        self.assertEqual(ka.effective_max_in_progress(self.conn, 4, now=self.clock(), probe=lambda: None), 1)
        self.clock.advance(30)
        self.assertEqual(ka.effective_max_in_progress(self.conn, 4, now=self.clock(), probe=lambda: None), 2)

    def test_the_probe_verdict_is_a_signal_too(self):
        self.cap(4)
        self.running()
        self.verdict = ka.REASON_PROBE_EXIT
        with self.assertLogs(ka.logger, level=logging.WARNING) as logs:
            self.assertEqual(self.cap(4), 1)
        self.assertIn(ka.REASON_PROBE_EXIT, logs.output[0])
        self.verdict = None
        self.clock.advance(60)
        self.assertEqual(self.cap(4), 2)

    def test_a_lowered_configured_cap_clamps_at_once(self):
        self.cap(6)
        self.assertEqual(self.cap(2), 2)
        self.assertEqual(self.state()["configured"], 2)

    def test_a_cursor_past_the_newest_row_rereads_the_table(self):
        """``_rebuild_drifted_tables`` can hand ids out again; a cursor past the newest row restarts at zero."""
        self.cap(4)
        ka.save_state(self.conn, configured=4, effective=4, cooldown_until=None, last_event_id=50, now=self.clock())
        pressure(self.conn, when=5)
        self.assertEqual(self.cap(4), 1)
        self.assertEqual(self.state()["last_event_id"], 1)

    def test_a_broken_board_falls_back_to_the_configured_cap(self):
        broken = sqlite3.connect(":memory:")
        self.addCleanup(broken.close)
        with self.assertLogs(ka.logger, level=logging.WARNING) as logs:
            self.assertEqual(ka.effective_max_in_progress(broken, 4, now=1.0, probe=lambda: None), 4)
        self.assertIn("falling back", logs.output[0])

    def test_a_probe_that_raises_falls_back_to_the_configured_cap(self):
        def boom():
            raise RuntimeError("no ssh")

        self.running()
        with self.assertLogs(ka.logger, level=logging.WARNING):
            self.assertEqual(ka.effective_max_in_progress(self.conn, 4, now=1.0, probe=boom), 4)

    def test_the_probe_is_not_asked_on_an_idle_board(self):
        """No worker is running: nothing to hold back, and the probe would keep the ssh master alive alone."""
        asked = []
        ka.effective_max_in_progress(self.conn, 4, now=1.0, probe=lambda: asked.append(1) or ka.REASON_PROBE_EXIT)
        self.assertEqual(asked, [])
        self.assertEqual(self.state()["effective"], 4)
        self.running()
        ka.effective_max_in_progress(self.conn, 4, now=2.0, probe=lambda: asked.append(1) or None)
        self.assertEqual(asked, [1])

    def test_one_card_steps_the_cap_down_once_per_cooldown(self):
        """A card that keeps hitting a busy broker cannot hold the cap at the floor on its own."""
        self.cap(4)
        pressure(self.conn, task_id="loud", when=5)
        self.assertEqual(self.cap(4), 1)
        for seconds in (10, 10, 10):
            self.clock.advance(seconds)
            pressure(self.conn, task_id="loud", when=6)
            self.assertEqual(self.cap(4), 1)
        self.assertEqual(self.state()["cooldown_until"], 1_060.0, "its later signals did not re-arm")
        self.assertEqual(self.state()["signalled"], ["loud"])
        self.clock.advance(30)
        self.assertEqual(self.cap(4), 2)
        self.assertEqual(self.state()["signalled"], [], "a clean step forgets the card")
        pressure(self.conn, task_id="loud", when=7)
        self.assertEqual(self.cap(4), 1, "a new cooldown: the same card counts again")

    def test_a_repeat_signal_read_on_the_lapse_tick_does_not_re_arm(self):
        """The card's row lands in the last tick window before the deadline, so the
        tick that reads it is the one that should step up; it must not count the
        row as fresh because the deadline has passed by the time it looks."""
        self.cap(4)
        pressure(self.conn, task_id="loud", when=5)
        self.assertEqual(self.cap(4), 1)
        self.assertEqual(self.state()["cooldown_until"], 1_060.0)
        self.clock.advance(60)
        pressure(self.conn, task_id="loud", when=1_057)
        self.assertEqual(self.cap(4), 2, "a clean step, not a new cooldown")
        self.assertEqual(self.state()["cooldown_until"], 1_120.0)
        self.assertEqual(self.state()["signalled"], [])
        self.clock.advance(5)
        pressure(self.conn, task_id="loud", when=1_062)
        self.assertEqual(self.cap(4), 1, "after the clean step the same card counts again")

    def test_a_card_stays_muted_across_a_re_armed_cooldown_until_a_clean_step(self):
        self.cap(4)
        pressure(self.conn, task_id="one", when=5)
        self.assertEqual(self.cap(4), 1)
        self.clock.advance(30)
        pressure(self.conn, task_id="two", when=6)
        self.assertEqual(self.cap(4), 1)
        self.assertEqual(self.state()["cooldown_until"], 1_090.0)
        self.clock.advance(30)
        pressure(self.conn, task_id="one", when=7)
        self.assertEqual(self.cap(4), 1)
        self.assertEqual(self.state()["cooldown_until"], 1_090.0, "the first card's repeat did not re-arm")
        self.assertEqual(self.state()["signalled"], ["one", "two"])
        self.clock.advance(30)
        pressure(self.conn, task_id="one", when=8)
        self.assertEqual(self.cap(4), 2, "nor at the lapse tick")
        self.assertEqual(self.state()["signalled"], [])

    def test_a_second_card_does_re_arm_the_cooldown(self):
        self.cap(4)
        pressure(self.conn, task_id="one", when=5)
        self.cap(4)
        self.clock.advance(30)
        pressure(self.conn, task_id="two", when=6)
        self.assertEqual(self.cap(4), 1)
        self.assertEqual(self.state()["cooldown_until"], 1_090.0)
        self.assertEqual(self.state()["signalled"], ["one", "two"])

    def test_the_row_is_written_only_when_something_changed(self):
        self.cap(4)
        written = self.state()["updated_at"]
        self.clock.advance(5)
        self.cap(4)
        self.assertEqual(self.state()["updated_at"], written, "an idle tick writes nothing")
        self.conn.execute("INSERT INTO task_events (task_id, kind, created_at) VALUES ('t', 'comment', 1)")
        self.clock.advance(5)
        self.cap(4)
        self.assertEqual(self.state()["updated_at"], 1_010.0, "a moved cursor is a change")

    def test_an_event_written_between_the_two_reads_is_counted_once(self):
        """``pending_pressure`` is bounded by the newest id the tick read first."""
        self.cap(4)
        pressure(self.conn, when=5)
        self.assertEqual(ka.pending_pressure(self.conn, 0, 0), [])
        self.assertEqual(ka.pending_pressure(self.conn, 0, 1), ["t1"])

    def test_a_nonsense_cap_is_returned_as_given(self):
        self.assertEqual(ka.effective_max_in_progress(self.conn, "six", now=1.0, probe=lambda: None), "six")
        self.assertEqual(ka.effective_max_in_progress(self.conn, 0, now=1.0, probe=lambda: None), 0)


# ``_tick_spawn_budget`` at v2026.9.14 reduced to what edits 8 and 11 splice
# into, assembled from the applier's own anchors so a drift fails the splice.
TICK_SOURCE = (
    "from typing import Optional\n\n\n"
    "class DispatchResult:\n"
    "    saturation = None\n"
    "    memory_pressure = None\n"
    "\n\n"
    "def _tick_spawn_budget(conn, result, *, max_spawn, max_in_progress, board):\n"
    + scheduling.AIMD_ANCHOR
    + "    if max_spawn is not None:\n"
    "        if running_count >= max_spawn:\n"
    "            return False, None\n"
    "        spawn_budget = max_spawn - running_count\n"
    "    if max_in_progress is not None:\n"
    + scheduling.SATURATION_ANCHOR
    + "        remaining = max_in_progress - total_running\n"
    "        if spawn_budget is None or spawn_budget > remaining:\n"
    "            spawn_budget = remaining\n"
    "    return True, spawn_budget\n"
)

TICK_EDITS = {
    label: (anchor, patched)
    for _file, label, anchor, patched in scheduling.EDITS
    if label in ("_tick_spawn_budget saturation record", "_tick_spawn_budget adaptive cap")
}


class PatchedTickTest(unittest.TestCase):
    """Edit 11 through the tick itself: the budget and the saturation record read the effective cap."""

    def setUp(self):
        self.conn = board()
        self.addCleanup(self.conn.close)
        self.clock = Clock(1_000)
        self.running = 0
        self.recorded = []
        source = TICK_SOURCE
        self.assertEqual(len(TICK_EDITS), 2)
        for label, (anchor, patched) in TICK_EDITS.items():
            self.assertEqual(source.count(anchor), 1, label)
            source = source.replace(anchor, patched)
        compile(source, "<tick>", "exec")
        self.ns = {
            "count_running_tasks": lambda conn: self.running,
            "count_running_tasks_other_boards": lambda board=None: 0,
            "_kanban_record_saturation": lambda conn, result, running, limit, board: self.recorded.append((running, limit)),
            "_kanban_effective_max_in_progress": lambda conn, cap, board: ka.effective_max_in_progress(
                conn, cap, board, now=self.clock(), probe=lambda: None
            ),
        }
        exec(source, self.ns)

    def tick(self, cap=6):
        result = self.ns["DispatchResult"]()
        return self.ns["_tick_spawn_budget"](self.conn, result, max_spawn=None, max_in_progress=cap, board="default")

    def test_a_clean_board_dispatches_under_the_configured_cap(self):
        self.running = 2
        self.assertEqual(self.tick(6), (True, 4))

    def test_a_pressure_event_leaves_one_slot_and_the_record_says_so(self):
        self.tick(6)
        pressure(self.conn, when=5)
        self.assertEqual(self.tick(6), (True, 1))
        self.running = 1
        self.assertEqual(self.tick(6), (False, None))
        self.assertEqual(self.recorded, [(1, 1)], "saturation is recorded against the effective cap")
        self.clock.advance(60)
        self.assertEqual(self.tick(6), (True, 1))
        self.clock.advance(60)
        self.assertEqual(self.tick(6), (True, 2))

    def test_an_uncapped_tick_is_untouched(self):
        self.assertEqual(self.tick(None), (True, None))


# ---------------------------------------------------------------------------
# The worker side
# ---------------------------------------------------------------------------


class WorkerSignalTest(unittest.TestCase):
    def setUp(self):
        self.conn = board()
        self.addCleanup(self.conn.close)

    def events(self):
        return [
            (row["task_id"], row["run_id"], json.loads(row["payload"]))
            for row in self.conn.execute("SELECT task_id, run_id, payload FROM task_events WHERE kind = ?", (ka.PRESSURE_KIND,))
        ]

    def record(self, env_type, returncode, output, cwd_observed=False, **env):
        with mock.patch.dict(os.environ, env, clear=False):
            return ka.record_worker_pressure(env_type, returncode, output, cwd_observed, connect=lambda: self.conn)

    def test_an_ssh_exit_255_without_the_marker_is_a_signal(self):
        self.assertEqual(ka.worker_pressure_reason("ssh", 255, "", False), ka.REASON_SSH_EXIT)

    def test_a_commands_own_255_with_the_marker_is_not(self):
        self.assertIsNone(ka.worker_pressure_reason("ssh", 255, "", True))

    def test_a_local_255_is_not(self):
        self.assertIsNone(ka.worker_pressure_reason("local", 255, "", False))

    def test_the_brokers_busy_exit_with_its_line_is_a_signal(self):
        output = f"some output\n{ka.BUSY_LINE_PREFIX}503)\nsome more"
        self.assertEqual(ka.worker_pressure_reason("ssh", 75, output, True), ka.REASON_BROKER_BUSY)
        self.assertEqual(ka.worker_pressure_reason("local", 75, output, False), ka.REASON_BROKER_BUSY)

    def test_the_busy_line_in_a_commands_output_is_not_a_signal_without_the_exit(self):
        """A log, a file or a README a worker reads can carry the line; only the shim exits 75 with it."""
        output = f"{ka.BUSY_LINE_PREFIX}503)\n"
        for code in (0, 1, 2):
            with self.subTest(exit=code):
                self.assertIsNone(ka.worker_pressure_reason("ssh", code, output, True))

    def test_the_busy_exit_without_the_line_is_not_a_signal(self):
        self.assertIsNone(ka.worker_pressure_reason("ssh", 75, "temporary failure of my own", True))

    def test_the_busy_line_mid_line_is_not_a_signal(self):
        self.assertIsNone(ka.worker_pressure_reason("ssh", 75, f"saw: {ka.BUSY_LINE_PREFIX}503)", True))

    def test_a_clean_result_is_not(self):
        self.assertIsNone(ka.worker_pressure_reason("ssh", 0, "fine", True))
        self.assertIsNone(ka.worker_pressure_reason("ssh", 1, "credential proxy error (HTTP 500): x", True))

    def test_outside_a_worker_nothing_is_recorded(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(ka.record_worker_pressure("ssh", 255, "", False, connect=lambda: self.conn))
        self.assertEqual(self.events(), [])

    def test_a_worker_records_on_its_own_card_with_its_run(self):
        reason = self.record("ssh", 255, "", HERMES_KANBAN_TASK="t_abc", HERMES_KANBAN_RUN_ID="7")
        self.assertEqual(reason, ka.REASON_SSH_EXIT)
        (task, run, payload), = self.events()
        self.assertEqual((task, run), ("t_abc", 7))
        self.assertEqual(payload, {"reason": ka.REASON_SSH_EXIT, "source": ka.SOURCE_WORKER, "exit_code": 255})

    def test_a_worker_without_a_run_id_records_a_task_scoped_event(self):
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_abc"}, clear=True):
            self.assertEqual(
                ka.record_worker_pressure("local", 75, f"{ka.BUSY_LINE_PREFIX}429)", connect=lambda: self.conn),
                ka.REASON_BROKER_BUSY,
            )
        (task, run, payload), = self.events()
        self.assertEqual((task, run, payload["reason"]), ("t_abc", None, ka.REASON_BROKER_BUSY))

    def test_a_non_signal_in_a_worker_opens_no_board(self):
        opened = []
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_abc"}):
            self.assertIsNone(ka.record_worker_pressure("ssh", 0, "ok", True, connect=lambda: opened.append(1) or self.conn))
        self.assertEqual(opened, [])

    def test_a_signal_a_worker_wrote_reaches_the_dispatcher(self):
        clock = Clock(1_000)
        self.assertEqual(ka.effective_max_in_progress(self.conn, 4, now=clock(), probe=lambda: None), 4)
        self.record("ssh", 255, "", HERMES_KANBAN_TASK="t_abc")
        self.assertEqual(ka.effective_max_in_progress(self.conn, 4, now=clock(), probe=lambda: None), 1)


# ---------------------------------------------------------------------------
# Names shared with other images
# ---------------------------------------------------------------------------


def _module_constants(path: Path) -> dict:
    tree = ast.parse(path.read_text())
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                out[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                continue
    return out


class SharedNamesTest(unittest.TestCase):
    def test_the_busy_line_is_the_one_the_shim_prints(self):
        """The shim runs in the sandbox image and spells the line itself."""
        if not CREDENTIAL_PROXY_CLIENT.exists():
            self.skipTest("credential_proxy_client.py not in this checkout")
        constants = _module_constants(CREDENTIAL_PROXY_CLIENT)
        self.assertEqual(constants["BROKER_BUSY_LINE"].format(code=503), f"{ka.BUSY_LINE_PREFIX}503)")
        self.assertNotIn("BROKER_BUSY_STATUSES", constants)  # the body's code decides, not the status
        self.assertEqual(constants["BUSY_CODE"], "CREDENTIAL_PROXY_BUSY")
        self.assertEqual(constants["BROKER_BUSY_EXIT_FALLBACK"], ka.BROKER_BUSY_EXIT)
        self.assertEqual(getattr(os, "EX_TEMPFAIL", ka.BROKER_BUSY_EXIT), ka.BROKER_BUSY_EXIT)

    def test_the_state_table_is_the_one_the_board_snapshot_reads(self):
        if not BOARD_SNAPSHOT.exists():
            self.skipTest("board_snapshot.py not in this checkout")
        constants = _module_constants(BOARD_SNAPSHOT)
        self.assertEqual(constants["AIMD_STATE_TABLE"], ka.STATE_TABLE)
        self.assertEqual(constants["AIMD_STATE_ROW_ID"], ka.STATE_ROW_ID)


if __name__ == "__main__":
    unittest.main()
