"""Tests for scripts/dev/repeat_under_load.sh, the pre-PR repeat-run step.

The script runs one test command several times while busy loops load the same
cores, and prints one line the PR body carries. Pinned here: the summary line's
count and exit code for a command that always passes and for one that fails on
a chosen run; the failing run is named; the busy loops are gone when the script
exits; Go's test result cache is off for the command; and a machine without
taskset runs unpinned and says so in the line.
"""

import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from unittest import mock

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
# A program that answers Ctrl-C with two lines of its own, a little apart, the
# way a test runner prints where it was interrupted. It sets no handler: Python
# raises KeyboardInterrupt only where it started with SIGINT at its default, as a
# shell can trap it only there. It writes `ready` inside the `try`, so the test
# signals no earlier, and it sleeps in short steps, so the signal is never held for
# a long sleep. A shell with a trap and a foreground `sleep` is not used: a signal
# that lands while the shell starts the sleep is lost or held until the sleep ends.
INTERRUPT_REPORT = (
    "import time\n"
    "try:\n"
    '    open("ready", "w").close()\n'
    "    while True:\n"
    "        time.sleep(0.05)\n"
    "except KeyboardInterrupt:\n"
    '    print("first", flush=True)\n'
    "    time.sleep(0.3)\n"
    '    print("second", flush=True)\n'
)
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
# its own handler is in place, so the test signals no earlier. The child is a Python
# program that sets its handler itself: it is an async command of the leader's shell,
# and an async subshell of bash 3.2 cannot trap SIGINT. It sleeps in short steps, so
# the signal is never held for a long sleep.
CHILD_OUTLIVES_LEADER = (
    'trap "exit 0" INT; '
    + shlex.quote(sys.executable)
    + " -c '"
    "import signal, sys, time\n"
    "def answer(*_):\n"
    '    print("child-int", flush=True); time.sleep(0.3); print("child-done", flush=True); sys.exit(0)\n'
    "signal.signal(signal.SIGINT, answer)\n"
    'open("ready", "w").close()\n'
    "while True:\n"
    "    time.sleep(0.05)' & "
    "wait"
)
# A run that records its pid, so a test that kills the script outright can end the
# run itself afterwards: the run is a foreign command in its own process group.
PID_THEN_EXEC_SLEEP = 'echo $$ > cmd.pid; exec sleep 30'
# The script's own ceiling on busy loops.
MAX_BUSY_LOOPS = 256
# More busy loops than any machine should be asked for.
TOO_MANY_LOOPS = "999"
# More digits than any run count a person would type; a 64-bit shell integer
# wraps on it.
OVERSIZED_COUNT = "9" * 20
# What a test needs from the machine. Each is found by a probe of the ability, never by
# the system's name, and each reason says what is absent and what the test would show.
PROC_STATUS = Path("/proc/self/status")
NO_PROC_STATUS = (
    "this machine has no /proc/self/status: the test reads the kernel's own cpu list or "
    "ignored-signal mask there, to show what the script pinned, clamped to or left ignored"
)
# A command started the way the script starts a run under setsid: an async subshell that
# restores SIGINT and execs. The command traps SIGINT, signals itself, and prints the mark
# from the trap; a command that started with SIGINT ignored cannot trap it and prints
# nothing. The script makes the same probe of its own bash before it uses setsid.
SIGINT_RESTORED_MARK = "restored"
SIGINT_RESTORE_PROBE = (
    f"( trap - INT QUIT; exec bash -c 'trap \"echo {SIGINT_RESTORED_MARK}\" INT; kill -INT $$; :' ) "
    "</dev/null & wait $!"
)
SIGINT_PROBE_DEADLINE_SECONDS = 20
# A stand-in for setsid that records its use in the file SETSID_MARK_ENV names, then runs
# the command in a new session: through the real setsid where REAL_SETSID_ENV names one,
# else with a session of its own making. Without the new session the run would stay in
# the script's process group, and a test's Ctrl-C to that group would reach it two times.
SETSID_MARK_ENV = "REPEAT_UNDER_LOAD_TEST_SETSID_MARK"
REAL_SETSID_ENV = "REPEAT_UNDER_LOAD_TEST_REAL_SETSID"
SETSID_STAND_IN = (
    f"#!{sys.executable}\n"
    "import os, sys\n"
    f'open(os.environ["{SETSID_MARK_ENV}"], "w").close()\n'
    f'real = os.environ.get("{REAL_SETSID_ENV}")\n'
    "if real:\n"
    "    os.execv(real, [real, *sys.argv[1:]])\n"
    "os.setsid()\n"
    "os.execvp(sys.argv[1], sys.argv[1:])\n"
)
# Inside the directory that `path_hiding` makes: the name of a folder of stand-ins, before
# the number of the PATH folder that it stands in for.
STAND_INS_FOLDER_PREFIX = "stand-ins-for-path-"
# Builtins that bash 4 added and macOS's /bin/bash 3.2 lacks.
BASH_4_BUILTINS = ("mapfile", "readarray")
# A cpu count below the cores the fallback test asks for, so the clamp shows.
STAND_IN_CPU_COUNT = 2
ASKED_CORES_ABOVE_STAND_IN = "8"


def sigint_at_its_default():
    """Run in a child before it starts: SIGINT at its default, whatever the test runner
    inherited. A runner started in the background of a non-interactive shell hands SIGINT
    on ignored, and a shell cannot trap a signal it started with ignored, so the script
    under test could not answer Ctrl-C at all."""
    signal.signal(signal.SIGINT, signal.SIG_DFL)


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
        preexec_fn=sigint_at_its_default,
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


def path_hiding(name):
    """A new directory and a PATH on which `name` is not found. The directory is first on
    that PATH: the caller may add a stand-in to it, and removes it afterwards.

    Each folder of the current PATH that holds `name` gives way to a folder, inside the new
    directory, of stand-ins for its other tools. A stand-in is a script of the test's own
    that runs the tool by its full path. It is not a link to the tool: on a managed machine
    a link to a protected tool is one that the test cannot remove again, and the folder
    then stays behind. A folder that does not hold `name` stays on the PATH as it is, so
    where the machine has no `name` the PATH is the current one behind the new directory.
    """
    bin_dir = Path(mkdtemp(prefix=f"repeat-under-load-path-{name}-"))
    folders = [str(bin_dir)]
    try:
        for number, folder in enumerate(os.environ["PATH"].split(os.pathsep)):
            # An empty entry of a PATH is the current folder, here that of the test.
            here = os.path.abspath(folder or os.curdir)
            if not os.path.lexists(os.path.join(here, name)):
                folders.append(folder)
                continue
            stand_ins = bin_dir / f"{STAND_INS_FOLDER_PREFIX}{number}"
            stand_ins.mkdir()
            for entry in os.listdir(here):
                tool = os.path.join(here, entry)
                if entry != name and os.path.isfile(tool) and os.access(tool, os.X_OK):
                    add_stand_in(stand_ins, entry, f'#!/bin/sh\nexec {shlex.quote(tool)} "$@"\n')
            folders.append(str(stand_ins))
    except BaseException:
        # The caller gets no directory to remove, so it must not stay.
        shutil.rmtree(bin_dir)
        raise
    return bin_dir, os.pathsep.join(folders)


def add_stand_in(bin_dir, name, script):
    """Put an executable `script` at `bin_dir/name`."""
    stand_in = bin_dir / name
    stand_in.write_text(script)
    stand_in.chmod(0o755)


def run_starts_with_sigint_ignored():
    """Whether a command started the way the script starts a run under setsid has SIGINT
    ignored, on the bash that the script runs on here."""
    probe = subprocess.run(
        ["bash", "-c", SIGINT_RESTORE_PROBE],
        capture_output=True,
        text=True,
        check=False,
        timeout=SIGINT_PROBE_DEADLINE_SECONDS,
        preexec_fn=sigint_at_its_default,
    )
    return SIGINT_RESTORED_MARK not in probe.stdout


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


class PathHidingTest(unittest.TestCase):
    """`path_hiding`, which the tests below use to take a tool off the PATH."""

    # A tool that prints the name it was started by, its pid and its arguments.
    NAME_PID_ARGUMENTS = '#!/bin/sh\necho "${0##*/} $$ got: $*"\n'

    def setUp(self):
        # Two folders of tools on the PATH, each with the tool to hide, as `/usr/bin` and
        # `/bin` both hold a tool on Linux. The first has a space in its name, one more
        # tool, a link to that one (as a package manager makes a link for a tool), a file
        # that is not executable and a sub-folder.
        tools = TemporaryDirectory(prefix="tools with a space ")
        more = TemporaryDirectory()
        for folder in (tools, more):
            self.addCleanup(folder.cleanup)
        self.tools, self.more = Path(tools.name), Path(more.name)
        for folder, name, script in (
            (self.tools, "hidden-tool", "#!/bin/sh\nexit 0\n"),
            (self.tools, "other-tool", self.NAME_PID_ARGUMENTS),
            (self.more, "hidden-tool", "#!/bin/sh\nexit 0\n"),
            (self.more, "second-tool", self.NAME_PID_ARGUMENTS),
        ):
            (folder / name).write_text(script)
            (folder / name).chmod(0o755)
        (self.tools / "linked-tool").symlink_to(self.tools / "other-tool")
        (self.tools / "not-a-tool.txt").write_text("a file that is not executable\n")
        (self.tools / "a-folder").mkdir()
        path = mock.patch.dict(os.environ, {"PATH": os.pathsep.join([str(self.tools), str(self.more), os.environ["PATH"]])})
        path.start()
        self.addCleanup(path.stop)

    def test_the_tool_is_not_found_and_the_other_tools_of_its_folders_still_run(self):
        bin_dir, path = path_hiding("hidden-tool")
        try:
            # Neither folder gives the tool.
            self.assertIsNone(shutil.which("hidden-tool", path=path))
            # Each other tool runs as itself: by its own name (a tool behind a link can read
            # that name), with the pid that the caller started, and with its arguments whole.
            for tool in ("other-tool", "linked-tool", "second-tool"):
                started = subprocess.Popen([tool, "a b", "c"], env=dict(os.environ, PATH=path), stdout=subprocess.PIPE, text=True)
                printed, _ = started.communicate()
                self.assertEqual((started.returncode, printed), (0, f"{tool} {started.pid} got: a b c\n"))
            for not_a_tool in ("not-a-tool.txt", "a-folder"):
                self.assertIsNone(shutil.which(not_a_tool, path=path), not_a_tool)
            # A tool of a folder that does not hold the hidden one is found where it is.
            self.assertEqual(shutil.which("sh", path=path), shutil.which("sh"))
            # The new directory is first: a stand-in that the caller adds wins over the tool.
            add_stand_in(bin_dir, "other-tool", "#!/bin/sh\necho stand-in\n")
            done = subprocess.run(["other-tool"], env=dict(os.environ, PATH=path), capture_output=True, text=True, check=False)
            self.assertEqual(done.stdout, "stand-in\n")
        finally:
            shutil.rmtree(bin_dir)

    def test_the_directory_holds_no_link_so_the_test_can_remove_all_of_it(self):
        """On a managed machine a link to a protected tool cannot be removed again. A file of
        the test's own can, whatever tool it runs."""
        bin_dir, _ = path_hiding("hidden-tool")
        try:
            made = sorted(bin_dir.rglob("*"))
            self.assertEqual([str(path) for path in made if path.is_symlink()], [])
            self.assertIn("linked-tool", [path.name for path in made])
        finally:
            shutil.rmtree(bin_dir)
        self.assertFalse(bin_dir.exists())

    def test_a_tool_that_the_machine_does_not_have_changes_nothing_on_the_path(self):
        bin_dir, path = path_hiding("a-tool-that-no-machine-has")
        try:
            self.assertEqual(path, f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
            self.assertEqual(list(bin_dir.iterdir()), [])
        finally:
            shutil.rmtree(bin_dir)

    def test_a_helper_that_fails_part_way_leaves_no_directory(self):
        """The caller gets no directory to remove when the helper raises."""
        made = []
        real_mkdtemp = mkdtemp

        def recording_mkdtemp(**options):
            made.append(Path(real_mkdtemp(**options)))
            # A helper that does leave its directory fails this test; the test removes it then.
            self.addCleanup(shutil.rmtree, made[-1], ignore_errors=True)
            return str(made[-1])

        fault = OSError("no space left on device")
        with mock.patch(f"{__name__}.mkdtemp", recording_mkdtemp), mock.patch(f"{__name__}.add_stand_in", side_effect=fault):
            with self.assertRaises(OSError) as caught:
                path_hiding("hidden-tool")
        self.assertIs(caught.exception, fault)
        self.assertEqual(len(made), 1)
        self.assertFalse(made[0].exists())


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
        bin_dir, path = path_hiding("taskset")
        try:
            self.assertIsNone(shutil.which("taskset", path=path))
            with TemporaryDirectory() as tmp:
                result = run_script(
                    ["-n", "1", *LIGHT_LOAD, "--", "true"],
                    cwd=tmp,
                    env={"PATH": path},
                )
        finally:
            shutil.rmtree(bin_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1 of 1 passed under load", result.stdout)
        self.assertIn("no pinning: taskset missing", result.stdout)

    def test_a_taskset_that_cannot_bind_the_cores_runs_unpinned_and_says_so(self):
        bin_dir, path = path_hiding("taskset")
        try:
            add_stand_in(
                bin_dir,
                "taskset",
                "#!/bin/sh\necho 'taskset: failed to set pid affinity: Invalid argument' >&2\nexit 1\n",
            )
            with TemporaryDirectory() as tmp:
                result = run_script(
                    ["-n", "1", *LIGHT_LOAD, "--", "true"],
                    cwd=tmp,
                    env={"PATH": path},
                )
        finally:
            shutil.rmtree(bin_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1 of 1 passed under load", result.stdout)
        self.assertRegex(result.stdout, r"no pinning: taskset cannot bind cpus \d")

    @unittest.skipUnless(PROC_STATUS.exists(), NO_PROC_STATUS)
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
                preexec_fn=sigint_at_its_default,
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

    def signal_to_the_pid_ends_the_run_in_flight(self, sig, expected_exit, env=None):
        with TemporaryDirectory() as tmp:
            proc = subprocess.Popen(
                [str(SCRIPT), "-n", "3", *LIGHT_LOAD, "--", "bash", "-c", PID_THEN_SLEEP],
                cwd=tmp,
                env=dict(os.environ, TMPDIR=tmp, **(env or {})),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
                preexec_fn=sigint_at_its_default,
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
                preexec_fn=sigint_at_its_default,
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
                preexec_fn=sigint_at_its_default,
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

    def test_without_setsid_a_command_that_ignores_ctrl_c_gets_a_term_it_can_answer(self):
        # Without setsid the run's process group comes from job control. The TERM must
        # reach the group: the command's shell answers only after its sleep has ended.
        bin_dir, path = path_hiding("setsid")
        try:
            self.assertIsNone(shutil.which("setsid", path=path))
            returncode, log, took, stderr = self.interrupt_after_ready(HANDLES_TERM_ONLY, env={"PATH": path})
        finally:
            shutil.rmtree(bin_dir)
        self.assertEqual(returncode, SIGINT_EXIT_CODE, stderr)
        self.assertGreaterEqual(took, MIN_GRACE_SECONDS, stderr)
        self.assertEqual(log.split()[-1:], ["termed"], f"the command was killed before it could answer TERM: {log!r}")

    def test_without_setsid_a_term_signal_to_the_script_ends_the_runs_child(self):
        bin_dir, path = path_hiding("setsid")
        try:
            self.assertIsNone(shutil.which("setsid", path=path))
            self.signal_to_the_pid_ends_the_run_in_flight(signal.SIGTERM, SIGTERM_EXIT_CODE, env={"PATH": path})
        finally:
            shutil.rmtree(bin_dir)

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
                preexec_fn=sigint_at_its_default,
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

    def interrupted_report(self, env=None):
        """Start the script over INTERRUPT_REPORT, Ctrl-C it once `ready` exists, and return
        (returncode, run-1 log text)."""
        with TemporaryDirectory() as tmp:
            proc = subprocess.Popen(
                [str(SCRIPT), "-n", "2", *LIGHT_LOAD, "--", sys.executable, "-c", INTERRUPT_REPORT],
                cwd=tmp,
                env=dict(os.environ, TMPDIR=tmp, **(env or {})),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
                preexec_fn=sigint_at_its_default,
            )
            try:
                self.assertTrue(
                    wait_until(lambda: (Path(tmp) / "ready").exists() and proc.poll() is None),
                    "the command never became ready",
                )
                os.killpg(proc.pid, signal.SIGINT)
                self.assertTrue(wait_until(lambda: proc.poll() is not None), "the script kept running after Ctrl-C")
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
            log = next(Path(tmp).glob("repeat-under-load.*/run-1.log")).read_text()
        return proc.returncode, log

    def test_an_interrupt_lets_the_command_finish_its_own_report(self):
        returncode, log = self.interrupted_report()
        self.assertEqual(returncode, SIGINT_EXIT_CODE)
        self.assertEqual(log.split(), ["first", "second"], "the command's interrupt report was cut short")

    def test_with_setsid_on_the_path_a_run_still_starts_with_ctrl_c_at_its_default(self):
        """A bash that does not restore SIGINT in an async subshell, as bash 3.2 does not, would
        start a run under setsid with Ctrl-C ignored. The script uses setsid only where this
        bash restores it, and job control elsewhere; the command's own report arrives either way."""
        real_setsid = shutil.which("setsid") or ""
        bin_dir, path = path_hiding("setsid")
        try:
            add_stand_in(bin_dir, "setsid", SETSID_STAND_IN)
            with TemporaryDirectory() as marks:
                mark = Path(marks) / "setsid.used"
                env = {"PATH": path, SETSID_MARK_ENV: str(mark), REAL_SETSID_ENV: real_setsid}
                returncode, log = self.interrupted_report(env=env)
                used = mark.exists()
        finally:
            shutil.rmtree(bin_dir)
        self.assertEqual(returncode, SIGINT_EXIT_CODE)
        self.assertEqual(log.split(), ["first", "second"], "the command's interrupt report did not arrive whole")
        self.assertEqual(used, not run_starts_with_sigint_ignored(), "setsid is for a bash that restores SIGINT only")

    def test_a_script_that_started_with_ctrl_c_ignored_keeps_setsid(self):
        """Job control repairs one state: a bash that keeps SIGINT ignored in an async subshell.
        A script that itself started with SIGINT ignored gains nothing from it on any bash, so
        it keeps setsid, with which a command that opens the terminal fails and does not stop."""
        real_setsid = shutil.which("setsid") or ""
        bin_dir, path = path_hiding("setsid")
        try:
            add_stand_in(bin_dir, "setsid", SETSID_STAND_IN)
            with TemporaryDirectory() as tmp:
                mark = Path(tmp) / "setsid.used"
                result = subprocess.run(
                    [str(SCRIPT), "-n", "1", *LIGHT_LOAD, "--", "true"],
                    cwd=tmp,
                    env=dict(os.environ, TMPDIR=tmp, PATH=path, **{SETSID_MARK_ENV: str(mark), REAL_SETSID_ENV: real_setsid}),
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=SCRIPT_DEADLINE_SECONDS,
                    preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_IGN),
                )
                used = mark.exists()
        finally:
            shutil.rmtree(bin_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(used, "the script left setsid for job control, which cannot restore SIGINT here")

    @unittest.skipUnless(PROC_STATUS.exists(), NO_PROC_STATUS)
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

    @unittest.skipUnless(PROC_STATUS.exists(), NO_PROC_STATUS)
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

    def test_with_no_cpu_list_and_no_nproc_the_core_count_comes_from_sysctl(self):
        # macOS has no /proc/self/status and no nproc. Stand-ins show the same on any
        # machine: a grep that finds no allowed list, no nproc on PATH, and a sysctl that
        # answers a count below the cores asked for.
        bin_dir, path = path_hiding("nproc")
        try:
            add_stand_in(
                bin_dir,
                "grep",
                f'#!/bin/sh\ncase "$1" in "^Cpus_allowed_list:") exit 1 ;; esac\nexec "{shutil.which("grep")}" "$@"\n',
            )
            add_stand_in(
                bin_dir,
                "sysctl",
                f'#!/bin/sh\n[ "$*" = "-n hw.ncpu" ] || exit 1\necho {STAND_IN_CPU_COUNT}\n',
            )
            with TemporaryDirectory() as tmp:
                result = run_script(
                    ["-n", "1", "-c", ASKED_CORES_ABOVE_STAND_IN, "-l", "1", "--", "true"],
                    cwd=tmp,
                    env={"PATH": path},
                )
        finally:
            shutil.rmtree(bin_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"({STAND_IN_CPU_COUNT} cores, {STAND_IN_CPU_COUNT} busy loops", result.stdout)

    def test_the_script_uses_no_builtin_that_bash_3_2_lacks(self):
        # macOS's /bin/bash is 3.2. The Linux job that runs this file has a bash with
        # these builtins, so there only the script's text can show one coming back.
        code = [line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#")]
        for builtin in BASH_4_BUILTINS:
            with self.subTest(builtin=builtin):
                used = [line for line in code if re.search(rf"(^|[\s;|&(]){builtin}\b", line)]
                self.assertEqual(used, [], f"{builtin} is a bash 4 builtin")

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
