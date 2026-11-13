"""Tests for scripts/dev/repeat_under_load.sh, the pre-PR repeat-run step.

The script runs one test command several times while busy loops load the same
cores, and prints one line the PR body carries. Pinned here: the summary line's
count and exit code for a command that always passes and for one that fails on
a chosen run; the failing run is named; the busy loops are gone when the script
exits; Go's test result cache is off for the command; and a machine without
taskset runs unpinned and says so in the line.
"""

import os
import shutil
import signal
import subprocess
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dev" / "repeat_under_load.sh"

RUNS = 4
FAILING_RUN = 2
# One core, one busy loop: enough to prove the load starts and stops without
# slowing the suite.
LIGHT_LOAD = ["-c", "1", "-l", "1"]
# Counts its own invocations in a file in the cwd and fails on FAILING_RUN.
FAIL_ON_ONE_RUN = (
    "n=$(cat ctr 2>/dev/null || echo 0); n=$((n+1)); echo $n > ctr; "
    f"[ $n -ne {FAILING_RUN} ]"
)
LOGS_PREFIX = "logs: "
LOAD_PIDS_FILE = "load.pids"
# A command that survives Ctrl-C the way go test and pytest do: it handles
# SIGINT and exits normally, so only the script's own trap can stop the loop.
SIGINT_HANDLED = 'trap "exit 0" INT; sleep 30'
# A command whose arguments only re-run if the summary line quotes them.
SPACED_ARGUMENT = "a b"
# Far above the time the script needs to notice the signal; never slept in
# full on a healthy run.
INTERRUPT_DEADLINE_SECONDS = 20.0
INTERRUPT_POLL_SECONDS = 0.1
SIGHUP_EXIT_CODE = 129
SIGINT_EXIT_CODE = 130
SIGTERM_EXIT_CODE = 143
USAGE_EXIT_CODE = 2
# A script that hangs must fail the test, not the suite.
SCRIPT_DEADLINE_SECONDS = 60
# A command that records its own pid and its child's, then waits, so a test can
# tell whether the script took the whole tree down.
PID_THEN_SLEEP = 'echo $$ > cmd.pid; sleep 30 & echo $! > child.pid; wait'
# A command that answers Ctrl-C with two lines of its own, a little apart, the
# way a test runner prints where it was interrupted. It touches `ready` once its
# trap is in place, so the test signals no earlier.
INTERRUPT_REPORT = 'trap "echo first; sleep 0.3; echo second; exit 0" INT; : > ready; sleep 30'
# A command that ignores the signals the script forwards, so only the final KILL
# ends it; it records its pid first.
IGNORES_SIGNALS = 'trap "" INT TERM HUP; echo $$ > cmd.pid; sleep 30'
# A grace period the script must have waited before it escalated: below its own
# first grace, so a slow machine cannot fail the lower bound.
MIN_GRACE_SECONDS = 1.0
# A command that ignores Ctrl-C but answers the TERM the script sends next with a
# line of its own: the middle rung of the escalation.
HANDLES_TERM_ONLY = 'trap "" INT; trap "echo termed; exit 0" TERM; : > ready; sleep 30'
# A command whose leader leaves on Ctrl-C while a child of its own is still writing
# its interrupt report; the report must arrive whole. The child writes `ready` once
# its own trap is in place, so the test signals no earlier.
CHILD_OUTLIVES_LEADER = (
    'trap "exit 0" INT; '
    '( trap "echo child-int; sleep 0.3; echo child-done; exit 0" INT; : > ready; sleep 30 ) & '
    'wait'
)
# A run that records its pid, so a test that kills the script outright can end the
# run itself afterwards: the run is a foreign command in its own session.
PID_THEN_EXEC_SLEEP = 'echo $$ > cmd.pid; exec sleep 30'
# The script's own ceiling on busy loops.
MAX_BUSY_LOOPS = 256
# More busy loops than any machine should be asked for.
TOO_MANY_LOOPS = "999"
# More digits than any run count a person would type; a 64-bit shell integer
# wraps on it.
OVERSIZED_COUNT = "9" * 20


def run_script(args, cwd, env=None):
    full_env = dict(os.environ, TMPDIR=str(cwd))
    if env:
        full_env.update(env)
    return subprocess.run(
        [str(SCRIPT), *args],
        cwd=cwd,
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=SCRIPT_DEADLINE_SECONDS,
    )


def logs_dir(result):
    for line in result.stderr.splitlines():
        if line.startswith(LOGS_PREFIX):
            return Path(line[len(LOGS_PREFIX):].strip())
    raise AssertionError(f"no '{LOGS_PREFIX}' line in stderr:\n{result.stderr}")


def wait_until(condition, deadline=INTERRUPT_DEADLINE_SECONDS):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(INTERRUPT_POLL_SECONDS)
    return condition()


def path_without(name):
    """A PATH holding every tool on the current PATH except `name`."""
    bin_dir = Path(os.environ.get("TMPDIR", "/tmp")) / f"repeat-under-load-path-{os.getpid()}-{name}"
    if bin_dir.exists():
        shutil.rmtree(bin_dir)
    bin_dir.mkdir()
    for d in os.environ["PATH"].split(os.pathsep):
        for entry in os.listdir(d) if os.path.isdir(d) else []:
            link = bin_dir / entry
            if entry == name or link.is_symlink() or link.exists():
                continue
            link.symlink_to(Path(d) / entry)
    return bin_dir


def allowed_cpus(status_text):
    """The CPUs in a /proc/<pid>/status Cpus_allowed_list line, in order."""
    for line in status_text.splitlines():
        if line.startswith("Cpus_allowed_list:"):
            cpus = []
            for part in line.split(":", 1)[1].strip().split(","):
                lo, _, hi = part.partition("-")
                cpus.extend(range(int(lo), int(hi or lo) + 1))
            return cpus
    raise AssertionError("no Cpus_allowed_list line")


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RepeatUnderLoadTest(unittest.TestCase):
    def test_a_command_that_passes_every_run_reports_n_of_n_and_exits_zero(self):
        with TemporaryDirectory() as tmp:
            result = run_script(["-n", str(RUNS), *LIGHT_LOAD, "--", "true"], cwd=tmp)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"{RUNS} of {RUNS} passed under load", result.stdout)
        self.assertIn(": true", result.stdout.strip().splitlines()[-1])

    def test_a_command_that_fails_on_one_run_names_the_run_and_exits_nonzero(self):
        with TemporaryDirectory() as tmp:
            result = run_script(
                ["-n", str(RUNS), *LIGHT_LOAD, "--", "bash", "-c", FAIL_ON_ONE_RUN],
                cwd=tmp,
            )
            logs = logs_dir(result)
            failing_log = logs / f"run-{FAILING_RUN}.log"
            self.assertTrue(failing_log.exists(), sorted(os.listdir(logs)))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"{RUNS - 1} of {RUNS} passed under load", result.stdout)
        self.assertIn(f"failed runs: {FAILING_RUN}", result.stderr)

    def test_the_busy_loops_are_gone_when_the_script_exits(self):
        with TemporaryDirectory() as tmp:
            result = run_script(["-n", "1", *LIGHT_LOAD, "--", "true"], cwd=tmp)
            pids_file = logs_dir(result) / LOAD_PIDS_FILE
            self.assertTrue(pids_file.exists(), result.stderr)
            pids = [int(p) for p in pids_file.read_text().split()]
        self.assertTrue(pids, "the script recorded no busy-loop pids")
        for pid in pids:
            self.assertTrue(wait_until(lambda: not pid_alive(pid)), f"busy loop {pid} outlived the script")

    def test_the_go_test_result_cache_is_off_for_the_command(self):
        with TemporaryDirectory() as tmp:
            result = run_script(
                ["-n", "1", *LIGHT_LOAD, "--", "sh", "-c", 'echo "GOFLAGS=$GOFLAGS"'],
                cwd=tmp,
                env={"GOFLAGS": "-mod=mod"},
            )
            log = (logs_dir(result) / "run-1.log").read_text()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-count=1", log)
        self.assertIn("-mod=mod", log, "the caller's GOFLAGS were dropped")

    def test_without_taskset_it_runs_unpinned_and_says_so(self):
        bin_dir = path_without("taskset")
        try:
            self.assertIsNone(shutil.which("taskset", path=str(bin_dir)))
            with TemporaryDirectory() as tmp:
                result = run_script(
                    ["-n", "1", *LIGHT_LOAD, "--", "true"],
                    cwd=tmp,
                    env={"PATH": str(bin_dir)},
                )
        finally:
            shutil.rmtree(bin_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1 of 1 passed under load", result.stdout)
        self.assertIn("no pinning: taskset missing", result.stdout)

    def test_a_taskset_that_cannot_bind_the_cores_runs_unpinned_and_says_so(self):
        bin_dir = path_without("taskset")
        try:
            fake = bin_dir / "taskset"
            fake.write_text("#!/bin/sh\necho 'taskset: failed to set pid affinity: Invalid argument' >&2\nexit 1\n")
            fake.chmod(0o755)
            with TemporaryDirectory() as tmp:
                result = run_script(
                    ["-n", "1", *LIGHT_LOAD, "--", "true"],
                    cwd=tmp,
                    env={"PATH": str(bin_dir)},
                )
        finally:
            shutil.rmtree(bin_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1 of 1 passed under load", result.stdout)
        self.assertRegex(result.stdout, r"no pinning: taskset cannot bind cpus \d")

    def test_the_command_runs_pinned_to_the_first_cpu_this_process_may_use(self):
        first_cpu = allowed_cpus(Path("/proc/self/status").read_text())[0]
        if shutil.which("taskset") is None or subprocess.run(
            ["taskset", "-c", str(first_cpu), "true"], capture_output=True, check=False
        ).returncode != 0:
            self.skipTest("this machine cannot pin a process to a cpu")
        with TemporaryDirectory() as tmp:
            result = run_script(
                ["-n", "1", "-c", "1", "-l", "1", "--", "grep", "Cpus_allowed_list", "/proc/self/status"],
                cwd=tmp,
            )
            log = (logs_dir(result) / "run-1.log").read_text()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(log, rf"Cpus_allowed_list:\s+{first_cpu}\s*$")

    def test_an_interrupt_the_command_handles_itself_still_stops_the_script(self):
        with TemporaryDirectory() as tmp:
            proc = subprocess.Popen(
                [str(SCRIPT), "-n", "3", *LIGHT_LOAD, "--", "bash", "-c", SIGINT_HANDLED],
                cwd=tmp,
                env=dict(os.environ, TMPDIR=tmp),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                # The first run has started once its log file exists in this run's own
                # TMPDIR; by then the script's traps are armed.
                self.assertTrue(
                    wait_until(lambda: any(Path(tmp).glob("repeat-under-load.*/run-1.log"))
                               and proc.poll() is None),
                    "the script never started its first run",
                )
                os.killpg(proc.pid, signal.SIGINT)
                self.assertTrue(wait_until(lambda: proc.poll() is not None),
                                "the script kept running after Ctrl-C")
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                stdout, stderr = proc.communicate()
            logs = None
            for line in stderr.splitlines():
                if line.startswith(LOGS_PREFIX):
                    logs = Path(line[len(LOGS_PREFIX):].strip())
            self.assertIsNotNone(logs, stderr)
            self.assertFalse((logs / "run-2.log").exists(), "the script went on to run 2 after Ctrl-C")
            pids = [int(p) for p in (logs / LOAD_PIDS_FILE).read_text().split()]
        self.assertEqual(proc.returncode, SIGINT_EXIT_CODE, (stdout, stderr))
        for pid in pids:
            self.assertTrue(wait_until(lambda: not pid_alive(pid)), f"busy loop {pid} outlived the script")

    def test_a_term_signal_to_the_script_ends_the_run_in_flight(self):
        self.signal_to_the_pid_ends_the_run_in_flight(signal.SIGTERM, SIGTERM_EXIT_CODE)

    def test_a_hangup_signal_to_the_script_ends_the_run_in_flight(self):
        self.signal_to_the_pid_ends_the_run_in_flight(signal.SIGHUP, SIGHUP_EXIT_CODE)

    def signal_to_the_pid_ends_the_run_in_flight(self, sig, expected_exit):
        with TemporaryDirectory() as tmp:
            proc = subprocess.Popen(
                [str(SCRIPT), "-n", "3", *LIGHT_LOAD, "--", "bash", "-c", PID_THEN_SLEEP],
                cwd=tmp,
                env=dict(os.environ, TMPDIR=tmp),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                pid_file = Path(tmp) / "cmd.pid"
                child_file = Path(tmp) / "child.pid"
                self.assertTrue(wait_until(lambda: child_file.exists() and child_file.read_text().strip()),
                                "the command never started")
                cmd_pid = int(pid_file.read_text())
                child_pid = int(child_file.read_text())
                proc.send_signal(sig)
                self.assertTrue(wait_until(lambda: proc.poll() is not None),
                                "the script waited for the run to finish")
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                stdout, stderr = proc.communicate()
        self.assertEqual(proc.returncode, expected_exit, (stdout, stderr))
        self.assertTrue(wait_until(lambda: not pid_alive(cmd_pid)), "the command outlived the script")
        self.assertTrue(wait_until(lambda: not pid_alive(child_pid)), "the command's child outlived the script")

    def test_a_command_that_ignores_the_signal_is_killed_after_the_grace_period(self):
        with TemporaryDirectory() as tmp:
            proc = subprocess.Popen(
                [str(SCRIPT), "-n", "2", *LIGHT_LOAD, "--", "bash", "-c", IGNORES_SIGNALS],
                cwd=tmp,
                env=dict(os.environ, TMPDIR=tmp),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                pid_file = Path(tmp) / "cmd.pid"
                self.assertTrue(wait_until(lambda: pid_file.exists() and pid_file.read_text().strip()),
                                "the command never started")
                cmd_pid = int(pid_file.read_text())
                sent_at = time.monotonic()
                os.killpg(proc.pid, signal.SIGINT)
                self.assertTrue(wait_until(lambda: proc.poll() is not None), "the script kept running after Ctrl-C")
                took = time.monotonic() - sent_at
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                stdout, stderr = proc.communicate()
        self.assertEqual(proc.returncode, SIGINT_EXIT_CODE, (stdout, stderr))
        self.assertGreaterEqual(took, MIN_GRACE_SECONDS, "the script did not wait for the command's report")
        self.assertFalse(pid_alive(cmd_pid), "the command outlived the script")

    def interrupt_after_ready(self, command, env=None):
        """Start the script over `command`, Ctrl-C it once `ready` exists, and return
        (returncode, run-1 log text, seconds from signal to exit, the script's stderr)."""
        with TemporaryDirectory() as tmp:
            proc = subprocess.Popen(
                [str(SCRIPT), "-n", "2", *LIGHT_LOAD, "--", "bash", "-c", command],
                cwd=tmp,
                env=dict(os.environ, TMPDIR=tmp, **(env or {})),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                self.assertTrue(
                    wait_until(lambda: (Path(tmp) / "ready").exists() and proc.poll() is None),
                    "the command never became ready",
                )
                sent_at = time.monotonic()
                os.killpg(proc.pid, signal.SIGINT)
                self.assertTrue(wait_until(lambda: proc.poll() is not None), "the script kept running after Ctrl-C")
                took = time.monotonic() - sent_at
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                _, stderr = proc.communicate()
            log = next(Path(tmp).glob("repeat-under-load.*/run-1.log")).read_text()
        return proc.returncode, log, took, stderr

    def test_a_command_that_ignores_ctrl_c_gets_a_term_it_can_answer(self):
        returncode, log, took, stderr = self.interrupt_after_ready(HANDLES_TERM_ONLY)
        self.assertEqual(returncode, SIGINT_EXIT_CODE, stderr)
        self.assertGreaterEqual(took, MIN_GRACE_SECONDS, stderr)
        # bash's own "Terminated" line for the sleep may precede the command's answer.
        self.assertEqual(log.split()[-1:], ["termed"], f"the command was killed before it could answer TERM: {log!r}")

    def test_a_child_that_outlives_the_leader_finishes_its_own_report(self):
        returncode, log, _, stderr = self.interrupt_after_ready(CHILD_OUTLIVES_LEADER)
        self.assertEqual(returncode, SIGINT_EXIT_CODE, stderr)
        self.assertEqual(log.split(), ["child-int", "child-done"], "the child's report was cut short")

    def test_without_setsid_the_script_still_stops_the_run_and_exits(self):
        # The fallback signals the pid alone: the script still waits its grace for a
        # run that ignores Ctrl-C, ends it, and exits with the signal's code.
        bin_dir = path_without("setsid")
        try:
            self.assertIsNone(shutil.which("setsid", path=str(bin_dir)))
            returncode, log, took, stderr = self.interrupt_after_ready(
                'trap "" INT; trap "echo termed; exit 0" TERM; : > ready; exec sleep 30',
                env={"PATH": str(bin_dir)},
            )
        finally:
            shutil.rmtree(bin_dir)
        self.assertEqual(returncode, SIGINT_EXIT_CODE, stderr)
        self.assertGreaterEqual(took, MIN_GRACE_SECONDS, stderr)

    def test_the_busy_loops_die_when_the_script_is_killed_outright(self):
        with TemporaryDirectory() as tmp:
            proc = subprocess.Popen(
                [str(SCRIPT), "-n", "1", *LIGHT_LOAD, "--", "bash", "-c", PID_THEN_EXEC_SLEEP],
                cwd=tmp,
                env=dict(os.environ, TMPDIR=tmp),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            run_pid = None
            try:
                pids_files = lambda: list(Path(tmp).glob(f"repeat-under-load.*/{LOAD_PIDS_FILE}"))
                pid_file = Path(tmp) / "cmd.pid"
                self.assertTrue(wait_until(lambda: pids_files() and pids_files()[0].read_text().strip()
                                           and pid_file.exists() and pid_file.read_text().strip()),
                                "the script never recorded its busy loops and run")
                pids = [int(p) for p in pids_files()[0].read_text().split()]
                run_pid = int(pid_file.read_text())
                proc.kill()
            finally:
                proc.communicate()
                # Only the loops are the script's to end; the test ends the run itself.
                if run_pid is not None:
                    try:
                        os.kill(run_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        for pid in pids:
            self.assertTrue(wait_until(lambda: not pid_alive(pid)), f"busy loop {pid} outlived a killed script")

    def test_an_interrupt_lets_the_command_finish_its_own_report(self):
        with TemporaryDirectory() as tmp:
            proc = subprocess.Popen(
                [str(SCRIPT), "-n", "2", *LIGHT_LOAD, "--", "bash", "-c", INTERRUPT_REPORT],
                cwd=tmp,
                env=dict(os.environ, TMPDIR=tmp),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                self.assertTrue(
                    wait_until(lambda: (Path(tmp) / "ready").exists() and proc.poll() is None),
                    "the command never armed its trap",
                )
                os.killpg(proc.pid, signal.SIGINT)
                self.assertTrue(wait_until(lambda: proc.poll() is not None), "the script kept running after Ctrl-C")
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
            log = next(Path(tmp).glob("repeat-under-load.*/run-1.log")).read_text()
        self.assertEqual(proc.returncode, SIGINT_EXIT_CODE)
        self.assertEqual(log.split(), ["first", "second"], "the command's interrupt report was cut short")

    def test_the_command_can_still_be_interrupted_by_sigint(self):
        # A background job of a non-interactive shell starts with SIGINT ignored, and a
        # test that handles Ctrl-C itself would never see it.
        with TemporaryDirectory() as tmp:
            result = run_script(["-n", "1", *LIGHT_LOAD, "--", "grep", "SigIgn", "/proc/self/status"], cwd=tmp)
            log = (logs_dir(result) / "run-1.log").read_text()
        self.assertEqual(result.returncode, 0, result.stderr)
        ignored = int(log.split(":", 1)[1].strip(), 16)
        self.assertFalse(ignored & (1 << (signal.SIGINT - 1)), f"SIGINT is ignored under the script: {log.strip()}")

    def test_a_flag_as_a_flag_value_prints_usage(self):
        with TemporaryDirectory() as tmp:
            result = run_script(["-n", "-c", "1", "-l", "1", "--", "true"], cwd=tmp)
        self.assertEqual(result.returncode, USAGE_EXIT_CODE, result.stdout + result.stderr)
        self.assertNotIn("passed under load", result.stdout)

    def test_too_many_busy_loops_prints_usage(self):
        with TemporaryDirectory() as tmp:
            result = run_script(["-n", "1", "-c", "1", "-l", TOO_MANY_LOOPS, "--", "true"], cwd=tmp)
        self.assertEqual(result.returncode, USAGE_EXIT_CODE, result.stdout + result.stderr)
        self.assertNotIn("passed under load", result.stdout)

    def test_a_count_with_a_leading_zero_is_read_in_base_ten(self):
        with TemporaryDirectory() as tmp:
            result = run_script(["-n", "010", *LIGHT_LOAD, "--", "true"], cwd=tmp)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("10 of 10 passed under load", result.stdout)

    def test_more_cores_than_this_process_may_use_clamps_to_what_it_may(self):
        count = len(allowed_cpus(Path("/proc/self/status").read_text()))
        if count >= MAX_BUSY_LOOPS:
            self.skipTest("this machine has more cpus than the script's busy-loop ceiling")
        with TemporaryDirectory() as tmp:
            # OMP_NUM_THREADS makes nproc under-report; the clamp must not follow it.
            result = run_script(
                ["-n", "1", "-c", str(count + 1), "-l", "1", "--", "true"],
                cwd=tmp,
                env={"OMP_NUM_THREADS": "1"},
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"({count} cores, {count} busy loops", result.stdout)

    def test_a_count_of_zero_however_spelt_prints_usage(self):
        for spelling in ("0", "00", "000"):
            with self.subTest(spelling=spelling), TemporaryDirectory() as tmp:
                result = run_script(["-n", spelling, *LIGHT_LOAD, "--", "true"], cwd=tmp)
                self.assertEqual(result.returncode, USAGE_EXIT_CODE, result.stdout + result.stderr)
                self.assertNotIn("passed under load", result.stdout)

    def test_an_oversized_count_prints_usage(self):
        with TemporaryDirectory() as tmp:
            result = run_script(["-n", OVERSIZED_COUNT, *LIGHT_LOAD, "--", "true"], cwd=tmp)
        self.assertEqual(result.returncode, USAGE_EXIT_CODE, result.stdout + result.stderr)
        self.assertNotIn("passed under load", result.stdout)

    def test_each_run_is_announced_on_stderr(self):
        with TemporaryDirectory() as tmp:
            result = run_script(["-n", "2", *LIGHT_LOAD, "--", "true"], cwd=tmp)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("run 1 of 2", result.stderr)
        self.assertIn("run 2 of 2", result.stderr)

    def test_a_count_that_is_not_a_number_prints_usage(self):
        with TemporaryDirectory() as tmp:
            result = run_script(["-n", "four", *LIGHT_LOAD, "--", "true"], cwd=tmp)
        self.assertEqual(result.returncode, USAGE_EXIT_CODE, result.stdout + result.stderr)
        self.assertIn("Usage:", result.stderr)

    def test_the_summary_line_quotes_an_argument_with_a_space(self):
        with TemporaryDirectory() as tmp:
            result = run_script(["-n", "1", *LIGHT_LOAD, "--", "bash", "-c", f"true {SPACED_ARGUMENT}"], cwd=tmp)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = result.stdout.strip().splitlines()[-1]
        command_part = summary.split("): ", 1)[1]
        self.assertNotIn(f"-c true {SPACED_ARGUMENT}", command_part, "the argument lost its quoting")
        self.assertIn("bash -c", command_part)

    def test_a_flag_without_a_value_prints_usage(self):
        with TemporaryDirectory() as tmp:
            result = run_script(["-n"], cwd=tmp)
        self.assertEqual(result.returncode, USAGE_EXIT_CODE, result.stderr)
        self.assertIn("Usage:", result.stderr)
        self.assertNotIn("unbound variable", result.stderr)

    def test_help_goes_to_stdout_with_the_header_and_no_code_and_exits_zero(self):
        with TemporaryDirectory() as tmp:
            result = run_script(["-h"], cwd=tmp)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usage:", result.stdout)
        self.assertNotIn("set -euo pipefail", result.stdout)
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
