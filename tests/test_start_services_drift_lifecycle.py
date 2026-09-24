"""The drift-detector's two lifecycle edges in deploy/shared/start-services.sh.

Both are invisible when they break. The container stays Ready either way, the
detector's own logs look the same, and what is lost -- a graceful shutdown, or
the truthfulness of the one ALERT this feature has -- shows up somewhere else
entirely, days later.

Shutdown has three parts and each has its own failure. `terminate` has to signal
each supervised process before it kills the subshell supervising it, because the
other order reparents the process to PID 1 the moment the subshell dies, after
which `pkill -P` on the dead subshell's pid matches nothing. It then has to
outlive the signal it sent: this script is the container's PID 1, so returning
from `terminate` is the script exiting, and the kernel takes the PID namespace
with it -- a SIGTERM delivered and not waited for buys nothing over a SIGKILL.
And it must not orphan a process launched during the wait, which is what a
supervisor free to go round its loop will do: the process it was supervising
exits, the loop reaches its backoff `sleep`, and once that elapses it starts a
replacement inside the drain that the drain then kills unsignalled.

That last one is why every fake supervisor below is the shape the script ships --
a `while true` loop that runs its process in the foreground and sleeps between
runs -- and not the `( child & wait )` shape that is easier to write. Against the
easier shape the supervisor exits as soon as its child does, so the drain's poll
goes empty on its own and a drain that would loop forever against the real thing
looks correct.

What all three protect is the detector's shutdown, which NACKs the records it
has not finished with so that they redeliver rather than each costing a
duplicate inject, and the watcher's, which writes its dedup snapshot so that a
restart does not replay every event still inside the API server's TTL.

`wait_for_drift_daemon` has to wait for the Session KV server before the first
launch, and has to do it inside the supervisor subshell. This container is a
native sidecar, so it starts before the platform-agent container the daemon runs
in; the detector's startup check against the daemon is fatal by design, so
without the wait it exits on connection-refused two or three times on every cold
start and the supervisor prints "NO out-of-band changes are being detected"
while nothing is wrong. Waiting in the foreground instead would trade that for a
script that does not reach its `wait -n` on the credential path for as long as
the deadline runs, during which a dead credential runtime or Envoy would not end
the container.

The intervals both of those sleep on arrive from the environment unfiltered, so
the last class here holds the clamp that keeps a hand-typed value from turning a
supervisor into a hot loop or the wait into a permanent one.

These run bash against the functions and constants as they ship rather than
against a copy, so a fix to the script cannot pass a test of last week's text.
"""

import os
import pathlib
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from _lift_shell import lift_constant, lift_function  # noqa: E402

_REPO_ROOT = _HERE.parent
_SCRIPT = _REPO_ROOT / "deploy" / "shared" / "start-services.sh"

# Long enough that the child is still running when terminate() is called, short
# enough that a hung test is a slow test rather than a stuck suite.
_CHILD_LIFETIME_SECONDS = 30

# How long to wait for the fake child to install its SIGTERM trap, and for it
# to react once signalled. Both are a loop iteration of a bash script on an
# unloaded machine; the ceiling is for a loaded CI runner.
_MARKER_TIMEOUT_SECONDS = 10.0
_MARKER_POLL_SECONDS = 0.05

# How long the fake child spends shutting down, and the floor terminate() must
# therefore not return before. Two seconds is comfortably longer than the
# milliseconds a signal-and-return takes, and comfortably shorter than the drain
# budget the script ships.
_CHILD_SHUTDOWN_SECONDS = 2
_SHUTDOWN_FLOOR_SECONDS = 1.0

# The drain budget handed to terminate() when the test wants it to expire: a
# child that ignores SIGTERM must not hold the container open indefinitely.
_SHORT_DRAIN_SECONDS = 2
_DRAIN_CEILING_SECONDS = 8.0

# How long to give bash to reap a supervisor the drain has just killed. The wait
# is what makes the check deterministic: SIGKILL is delivered synchronously but
# the reap that makes `kill -0` fail is a SIGCHLD the shell handles when it next
# runs a command, so testing once immediately after terminate() races it.
_REAP_OBSERVATION_SECONDS = 2

# The fake supervisor's backoff between runs of its process. Short enough that a
# supervisor still free to go round its loop gets there well inside the drain
# budget below, which is the whole point of the relaunch test: the defect has to
# have room to happen, or the test passes on a broken drain by running out of
# scenario rather than by being satisfied.
_SUPERVISOR_BACKOFF_SECONDS = 2

# The drain budget for the relaunch test. Comfortably longer than a backoff plus
# the relaunch after it, so a drain that leaves the supervisor looping fails on
# the replacement it orphaned rather than on the clock.
_RELAUNCH_DRAIN_SECONDS = 8

# How long that test's child takes to shut down, so that the drain's first poll
# lands while it is still there. A child that dies the instant it is signalled
# lets the drain finish before the supervisor has left its loop, which is a pass
# for a reason the test is not making an assertion about.
_RELAUNCH_SHUTDOWN_SECONDS = 1

# What the drain should cost once the supervisor leaves its loop: its child exits
# on the signal, so anything near the budget means the poll never went empty.
_EARLY_BREAK_CEILING_SECONDS = 4.0

# Every setting the clamp block covers, with a value the clamp leaves alone. The
# harness sets all of them because `clamp_at_least` reads each by name under
# `set -u`, and a test interested in one still has to supply the rest.
_GOOD_INTERVAL_SECONDS = 10
_CLAMPED_SETTINGS = (
    "WATCHER_RETRY_MIN_SECONDS",
    "WATCHER_RETRY_MAX_SECONDS",
    "DRIFT_RETRY_MIN_SECONDS",
    "DRIFT_RETRY_MAX_SECONDS",
    "DRIFT_DAEMON_POLL_SECONDS",
    "DRIFT_DAEMON_WAIT_SECONDS",
    "WATCHER_HEALTHY_RUN_SECONDS",
    "DRIFT_HEALTHY_RUN_SECONDS",
    "DRIFT_SHORT_EXIT_ALERT_COUNT",
)

# A value that is all digits, in range, and still fatal: bash reads a leading
# zero as octal, and 8 is not an octal digit, so `$(( 08 ))` is an error rather
# than eight. `012` would be the quieter half of the same bug -- valid octal,
# silently ten -- but an error is the half a test can assert on cheaply.
_LEADING_ZERO_VALUE = "08"

# The two ceilings and the settings that floor them. A test that hands a ceiling
# a small value has to lower its floor too, or the clamp it is measuring is the
# floor's rather than the one it meant to exercise.
_CEILING_FLOORS = {
    "WATCHER_RETRY_MAX_SECONDS": "WATCHER_RETRY_MIN_SECONDS",
    "DRIFT_RETRY_MAX_SECONDS": "DRIFT_RETRY_MIN_SECONDS",
}

# The deadline handed to wait_for_drift_daemon when the test wants it to expire.
# Two polls of one second: enough to prove it loops rather than falling straight
# through, short enough to keep the suite fast.
_SHORT_WAIT_SECONDS = 2
_SHORT_POLL_SECONDS = 1

# What "returned promptly" means for the connect-succeeds case. One poll
# interval is 1s in that test, so anything under it proves the function did not
# sleep before noticing the listener.
_PROMPT_RETURN_SECONDS = 1.0

# Long enough that a wait loop which never advances its counter is still going
# when we look, short enough not to dominate the suite.
_HANG_OBSERVATION_SECONDS = 6

# A port nothing listens on, for the probes that have to fail. Port 1 rather
# than an ephemeral one the kernel just handed back: binding and closing a
# socket returns a number anything on the machine may claim before bash probes
# it, which is a rare green where a red belongs, and holding the socket bound
# instead makes macOS hang the connect for eight seconds rather than refusing
# it. Port 1 needs root to bind and is refused immediately on both platforms.
_UNLISTENING_PORT = 1


def _run_bash(
    script: str, timeout: float = _MARKER_TIMEOUT_SECONDS * 3
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _interval_settings(**overrides: object) -> str:
    """Bash assignments for every setting the clamp block reads.

    All of them every time: `clamp_at_least` dereferences each by name under
    `set -u`, so a test interested in one still has to supply the rest, and the
    ones it is not interested in get a value the clamp leaves alone.
    """
    values: dict[str, object] = dict.fromkeys(_CLAMPED_SETTINGS, _GOOD_INTERVAL_SECONDS)
    values.update(overrides)
    return "\n".join(f'{name}="{value}"' for name, value in values.items())


def _kill_recorded(pidfile: pathlib.Path) -> None:
    """SIGKILL the process whose pid is in `pidfile`, if it is still there.

    A test that proves the drain gives up leaves behind the process it gave up
    on. Left alone it holds the harness's stdout pipe open for its whole
    lifetime, which turns every later assertion in the module into a wait.
    """
    try:
        os.kill(int(pidfile.read_text().strip()), signal.SIGKILL)
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        pass


class _ScriptCase(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _SCRIPT.read_text()

    def lift(self, name: str) -> str:
        return lift_function(name, self.text, _SCRIPT)

    def clamp_block(self) -> str:
        """The shipped interval clamp: the floor, the helper, and the calls.

        The three together, because each is inert without the others -- a helper
        nobody calls clamps nothing, and the calls are what decide which settings
        are covered and which floor each gets.
        """
        calls = re.findall(r"^clamp_at_least .*$", self.text, re.M)
        assert calls, f"{_SCRIPT} no longer clamps anything"
        return "\n".join(
            [
                lift_constant("MIN_SETTING_VALUE", self.text, _SCRIPT),
                lift_function("clamp_at_least", self.text, _SCRIPT),
                *calls,
            ]
        )


class TerminateShutsTheProcessDownRatherThanJustSignallingIt(_ScriptCase):
    """The three shutdown edges, exercised as bash rather than read as text."""

    def _harness(self, body: str, drain: int = _SHORT_DRAIN_SECONDS) -> str:
        """The shipped drain functions, wired to a fake supervisor.

        `body` sets up `drift_pid` and then calls terminate; the caller asserts
        on what the child left behind.
        """
        return f"""
        set -euo pipefail
        readonly SHUTDOWN_DRAIN_SECONDS={drain}
        readonly SHUTDOWN_DRAIN_POLL_SECONDS=1
        {self.lift("drain_supervised")}
        {self.lift("terminate")}
        runtime_pid=""
        envoy_pid=""
        watcher_pid=""
        drift_pid=""
        {body}
        """

    def _supervisor(
        self,
        child: str,
        pidfile: pathlib.Path,
        backoff: int = _SUPERVISOR_BACKOFF_SECONDS,
    ) -> str:
        """A fake supervisor of the shape both launchers ship, assigned to drift_pid.

        A `while true` loop that runs its process in the *foreground* and sleeps
        between runs, carrying the `trap 'exit 0' TERM` the real ones carry. Both
        halves matter. The process is a real child, so `pkill -P` reaches it; the
        loop means a drain that signals only the child does not end the
        supervisor, which is the difference between this and a
        `( child & wait )` stand-in that exits when its child does and makes a
        broken drain look correct.

        Its output goes to /dev/null and its pid to `pidfile`, both because a
        drain under test may fail to end it: on the captured pipe it would block
        `subprocess.run` until the timeout instead of failing an assertion, and
        unrecorded it would loop for the rest of the session.
        """
        return f"""
        (
          trap 'exit 0' TERM
          while true; do
            bash -c {shlex.quote(child)} || true
            sleep {backoff}
          done
        ) >/dev/null 2>&1 &
        drift_pid=$!
        echo "${{drift_pid}}" > {pidfile}
        """

    def test_the_supervised_process_receives_sigterm(self) -> None:
        """The first regression: kill the subshell first and this never arrives."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            ready = tmpdir / "ready"
            signalled = tmpdir / "signalled"
            supervisor = tmpdir / "supervisor"

            child = f"""trap 'touch {signalled}; exit 0' TERM
                        touch {ready}
                        for _ in $(seq 1 {_CHILD_LIFETIME_SECONDS * 10}); do sleep 0.1; done"""
            body = f"""
            {self._supervisor(child, supervisor)}
            while [ ! -e {ready} ]; do sleep {_MARKER_POLL_SECONDS}; done
            terminate
            """
            try:
                _run_bash(self._harness(body))
            finally:
                _kill_recorded(supervisor)

            self.assertTrue(
                signalled.exists(),
                "the supervised process did not receive SIGTERM; terminate() most likely "
                "killed the supervisor subshell first, reparenting the process to PID 1 "
                "where `pkill -P` on the dead pid matches nothing",
            )

    def test_it_waits_for_the_signalled_process_to_finish(self) -> None:
        """The second: a SIGTERM this script does not outlive buys nothing.

        `terminate` returning is the script exiting, and the script is the
        container's PID 1, so the kernel SIGKILLs whatever is still shutting
        down. The detector budgets ten seconds to settle its in-flight records.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            ready = tmpdir / "ready"
            done = tmpdir / "done"
            returned = tmpdir / "returned"
            supervisor = tmpdir / "supervisor"

            child = f"""trap 'sleep {_CHILD_SHUTDOWN_SECONDS}; touch {done}; exit 0' TERM
                        touch {ready}
                        for _ in $(seq 1 {_CHILD_LIFETIME_SECONDS * 10}); do sleep 0.1; done"""
            body = f"""
            {self._supervisor(child, supervisor)}
            while [ ! -e {ready} ]; do sleep {_MARKER_POLL_SECONDS}; done
            terminate
            touch {returned}
            echo "terminate-returned"
            """
            try:
                started = time.monotonic()
                result = _run_bash(self._harness(body, drain=_CHILD_SHUTDOWN_SECONDS * 5))
                elapsed = time.monotonic() - started
            finally:
                _kill_recorded(supervisor)

            self.assertIn("terminate-returned", result.stdout)
            self.assertTrue(
                done.exists(),
                "terminate() returned before the process it signalled had finished "
                "shutting down; as PID 1 that is the script exiting, and the kernel "
                "SIGKILLs the shutdown it just started",
            )
            self.assertLess(
                done.stat().st_mtime,
                returned.stat().st_mtime,
                "the signalled process finished after terminate() returned, so on a "
                "real pod it would have been SIGKILLed mid-shutdown",
            )
            self.assertGreater(
                elapsed,
                _SHUTDOWN_FLOOR_SECONDS,
                "terminate() returned too quickly to have waited for anything",
            )

    def test_it_gives_up_on_a_process_that_ignores_sigterm(self) -> None:
        """The drain is bounded: the pod's grace period is not ours to spend."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            pidfile = tmpdir / "pid"
            supervisor = tmpdir / "supervisor"

            # This child outlives the harness by design, so it records its pid
            # for the cleanup below rather than being left to the reaper. Its
            # supervisor outlives it too: bash runs the trap between commands, so
            # a supervisor whose foreground child never returns never reaches it.
            child = f"""trap '' TERM
                        echo $$ > {pidfile}
                        for _ in $(seq 1 {_CHILD_LIFETIME_SECONDS * 10}); do sleep 0.1; done"""
            # Whether the supervisor is gone afterwards is the other half of the
            # claim, and the poll is why it is not a race: SIGKILL lands at once
            # but the reap that makes `kill -0` fail is a SIGCHLD this shell
            # handles between commands.
            reap_polls = int(_REAP_OBSERVATION_SECONDS / _MARKER_POLL_SECONDS)
            body = f"""
            {self._supervisor(child, supervisor)}
            while [ ! -s {pidfile} ]; do sleep {_MARKER_POLL_SECONDS}; done
            terminate
            echo "terminate-returned"
            reaped=no
            for _ in $(seq 1 {reap_polls}); do
              kill -0 "$(cat {supervisor})" 2>/dev/null || {{ reaped=yes; break; }}
              sleep {_MARKER_POLL_SECONDS}
            done
            echo "supervisor-reaped=${{reaped}}"
            """
            try:
                started = time.monotonic()
                result = _run_bash(self._harness(body))
                elapsed = time.monotonic() - started
            finally:
                _kill_recorded(pidfile)
                _kill_recorded(supervisor)

            self.assertIn("terminate-returned", result.stdout)
            self.assertLess(
                elapsed,
                _DRAIN_CEILING_SECONDS,
                "terminate() outlasted its drain budget waiting on a process that "
                "will never exit",
            )
            self.assertIn(
                "supervisor-reaped=yes",
                result.stdout,
                "the drain gave up without ending the supervisor. Its last resort "
                "has to be SIGKILL: the trap defers a second SIGTERM exactly as it "
                "deferred the first, and a supervisor still running at the budget "
                "is by definition one whose process did not return",
            )

    def test_the_supervisor_does_not_relaunch_inside_the_drain(self) -> None:
        """The third: the drain must end the loop, not just the process in it.

        Signalling only the child leaves the supervisor going round: the child
        exits, the loop reaches its backoff `sleep`, and when that elapses it
        starts a replacement *inside* the drain -- which the drain then kills
        without signalling, since its one round of `pkill` is long past. The
        symptom on a pod is a detector that is SIGKILLed mid-flight on every
        shutdown, redelivering whatever it was holding, and a drain that always
        costs its full budget: the drain that had this bug polled the child set
        with `pgrep -P`, and the backoff `sleep` is itself a child, so the poll
        never went empty. The shipped drain polls the supervisors themselves.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            launches = tmpdir / "launches"
            supervisor = tmpdir / "supervisor"

            child = f"""trap 'sleep {_RELAUNCH_SHUTDOWN_SECONDS}; exit 0' TERM
                        echo started >> {launches}
                        for _ in $(seq 1 {_CHILD_LIFETIME_SECONDS * 10}); do sleep 0.1; done"""
            body = f"""
            {self._supervisor(child, supervisor)}
            while [ ! -s {launches} ]; do sleep {_MARKER_POLL_SECONDS}; done
            terminate
            echo "terminate-returned"
            """
            try:
                started = time.monotonic()
                result = _run_bash(self._harness(body, drain=_RELAUNCH_DRAIN_SECONDS))
                elapsed = time.monotonic() - started
                # The replacement, if there is one, may still be starting as
                # terminate() returns.
                time.sleep(_SUPERVISOR_BACKOFF_SECONDS)
            finally:
                _kill_recorded(supervisor)

            self.assertIn("terminate-returned", result.stdout)
            self.assertEqual(
                launches.read_text().count("started"),
                1,
                "the supervisor went round its loop during the drain and started a "
                "replacement, which terminate() then killed without signalling",
            )
            self.assertLess(
                elapsed,
                _EARLY_BREAK_CEILING_SECONDS,
                "the drain ran for most of its budget although the supervised process "
                "exited at once, so the poll kept finding the supervisor alive -- still "
                "in its loop, waiting out the backoff `sleep` rather than leaving on the "
                "trap",
            )

    def test_both_launchers_leave_their_loop_on_sigterm(self) -> None:
        """The mechanism above, asserted against the two real supervisors.

        bash defers a trap until the foreground command returns, which is what
        makes this safe: the supervised process still gets its full shutdown, and
        the supervisor then exits instead of reaching its backoff `sleep`. Drop
        the trap from either launcher and the orphan comes back with every test
        above still green, because they run a fake supervisor rather than these.
        """
        for name in ("start_event_watcher", "start_drift_detector"):
            with self.subTest(launcher=name):
                self.assertIn(
                    "trap 'exit 0' TERM",
                    self.lift(name),
                    f"{name}'s supervisor no longer leaves its loop on SIGTERM, so "
                    "terminate() can orphan the replacement it starts mid-drain",
                )

    def test_both_supervisors_are_drained_and_the_signal_precedes_the_kill(self) -> None:
        drain = self.lift("drain_supervised")
        pkill_at = drain.find("pkill -TERM -P")
        term_at = drain.find('kill -TERM "${pid}"')
        wait_at = drain.find('kill -0 "${pid}"')
        kill_at = drain.find('kill -KILL "${pid}"')

        self.assertNotEqual(pkill_at, -1, "drain_supervised no longer signals children")
        self.assertNotEqual(term_at, -1, "drain_supervised no longer signals the supervisors")
        self.assertNotEqual(wait_at, -1, "drain_supervised no longer waits for them to exit")
        self.assertNotEqual(
            kill_at,
            -1,
            "drain_supervised's last resort is no longer SIGKILL; a second SIGTERM is "
            "deferred by the supervisor's trap exactly as the first was, so it cannot "
            "end a supervisor whose process is ignoring the signal",
        )
        self.assertLess(
            max(pkill_at, term_at),
            wait_at,
            "drain_supervised waits before it signals, so it waits for nothing",
        )
        self.assertLess(
            wait_at,
            kill_at,
            "drain_supervised kills the supervisors before its children have exited, "
            "reparenting them to PID 1 mid-shutdown",
        )

        terminate = self.lift("terminate")
        for pid_var in ("watcher_pid", "drift_pid"):
            with self.subTest(pid_var=pid_var):
                self.assertRegex(
                    terminate,
                    r"supervisors\+=\(\"\$\{%s\}\"\)" % pid_var,
                    f"terminate() no longer drains {pid_var}",
                )


class WaitForDriftDaemon(_ScriptCase):
    def _harness(self, port: int, wait: int, poll: str) -> str:
        return f"""
        set -u
        readonly KV_DAEMON_HOST=127.0.0.1
        readonly KV_DAEMON_PORT={port}
        readonly KV_DAEMON_URL="http://127.0.0.1:{port}"
        {_interval_settings(DRIFT_DAEMON_WAIT_SECONDS=wait, DRIFT_DAEMON_POLL_SECONDS=poll)}
        {self.clamp_block()}
        {self.lift("wait_for_drift_daemon")}
        wait_for_drift_daemon
        echo "returned=$?"
        """

    def test_it_returns_as_soon_as_the_daemon_is_listening(self) -> None:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]

            started = time.monotonic()
            result = _run_bash(self._harness(port, _SHORT_WAIT_SECONDS, str(_SHORT_POLL_SECONDS)))
            elapsed = time.monotonic() - started

        self.assertIn("returned=0", result.stdout)
        self.assertLess(
            elapsed,
            _PROMPT_RETURN_SECONDS + _SHORT_POLL_SECONDS,
            "the wait did not notice a listening daemon on its first probe",
        )
        self.assertNotIn("was not listening", result.stderr)

    def test_it_gives_up_after_the_deadline_rather_than_blocking_forever(self) -> None:
        """A daemon that never arrives must not be why drift goes undetected."""
        result = _run_bash(
            self._harness(_UNLISTENING_PORT, _SHORT_WAIT_SECONDS, str(_SHORT_POLL_SECONDS))
        )

        self.assertIn("returned=0", result.stdout)
        self.assertIn("was not listening", result.stderr)

    def test_a_zero_poll_interval_does_not_hang_the_wait_forever(self) -> None:
        """The counter would never advance, and the silence would be total.

        A detector that is never launched has no short exits, so the supervisor's
        ALERT never fires either: the container stays Ready with drift detection
        off and nothing in the log.
        """
        try:
            result = _run_bash(
                self._harness(_UNLISTENING_PORT, _SHORT_WAIT_SECONDS, "0"),
                timeout=_HANG_OBSERVATION_SECONDS,
            )
        except subprocess.TimeoutExpired:
            self.fail(
                "DRIFT_DAEMON_POLL_SECONDS=0 left the wait loop spinning; the counter "
                "never advances, so the detector is never launched"
            )

        self.assertIn("returned=0", result.stdout)
        self.assertIn("was not listening", result.stderr)

    def test_it_says_nothing_when_the_daemon_was_there(self) -> None:
        """The log line is the context for a later ALERT, not an event."""
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            result = _run_bash(
                self._harness(
                    listener.getsockname()[1], _SHORT_WAIT_SECONDS, str(_SHORT_POLL_SECONDS)
                )
            )

        self.assertEqual(result.stderr.strip(), "")

    def test_the_wait_happens_inside_the_supervisor_subshell(self) -> None:
        """In front of it, the script does not reach its `wait -n` for the deadline."""
        launcher = self.lift("start_drift_detector")
        call_at = launcher.find("wait_for_drift_daemon")
        subshell_at = launcher.find("\n  (\n")

        self.assertNotEqual(call_at, -1, "start_drift_detector no longer waits for the daemon")
        self.assertNotEqual(
            subshell_at, -1, "start_drift_detector no longer has a supervisor subshell"
        )
        self.assertGreater(
            call_at,
            subshell_at,
            "the daemon wait runs before the supervisor subshell is backgrounded, so the script "
            "does not reach its `wait -n` on the credential path until the deadline expires",
        )


class EveryTunableIsCorrectedBeforeAnythingReadsIt(_ScriptCase):
    """The settings all arrive from the environment, unfiltered.

    `spec.deployment.env` reaches this container as written, so a hand-typed
    value is the realistic source of a bad one, and every way of getting one
    wrong ends the same way: a container that stays Ready with nothing in the
    log. Four shapes, each with its own route there.

    Zero survives every check the retry loops already make: `sleep 0` returns at
    once, `0 * 2` stays zero, and the cap comparison never lifts it, so a
    detector that exits quickly is re-exec'd as fast as the kernel allows -- in
    the same container as the API authenticator, for the life of the pod. A
    ceiling below its floor is the same loop by a longer route, the cap line
    dragging every backoff back down on the second failure. A non-numeric value
    makes `sleep` fail and, worse, is read as a variable name by the bare-word
    comparisons these feed, so under `set -u` it is a fatal "unbound variable" on
    the loop's first pass. And a leading zero passes for a number right up until
    the first `$(( ))` touches it, where bash reads it as octal.
    """

    def _clamped(self, **overrides: object) -> tuple[dict[str, str], str]:
        script = "\n".join(
            [
                "set -u",
                _interval_settings(**overrides),
                self.clamp_block(),
                *(f'echo "{name}=${{{name}}}"' for name in _CLAMPED_SETTINGS),
            ]
        )
        result = _run_bash(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        return dict(line.split("=", 1) for line in result.stdout.split()), result.stderr

    def test_a_zero_retry_floor_cannot_become_a_hot_loop(self) -> None:
        values, stderr = self._clamped(
            WATCHER_RETRY_MIN_SECONDS=0, DRIFT_RETRY_MIN_SECONDS=0
        )

        for name in ("WATCHER_RETRY_MIN_SECONDS", "DRIFT_RETRY_MIN_SECONDS"):
            with self.subTest(setting=name):
                self.assertNotEqual(
                    values[name],
                    "0",
                    f"{name}=0 reaches the supervisor's backoff, which then re-execs a "
                    "fast-failing process as fast as the kernel allows",
                )
                self.assertIn(name, stderr, f"{name} was clamped without saying so")

    def test_a_ceiling_below_its_floor_is_raised_to_it(self) -> None:
        """Otherwise the cap line drags every backoff back down to the ceiling."""
        floor = _GOOD_INTERVAL_SECONDS
        overrides: dict[str, object] = {}
        for ceiling, floor_name in _CEILING_FLOORS.items():
            overrides[floor_name] = floor
            overrides[ceiling] = 1
        values, stderr = self._clamped(**overrides)

        for ceiling, floor_name in _CEILING_FLOORS.items():
            with self.subTest(setting=ceiling):
                self.assertGreaterEqual(
                    int(values[ceiling]),
                    floor,
                    f"{ceiling} stayed below its minimum, so the cap pulls the backoff "
                    "down to it on the second failure and the retry never slows",
                )
                # The setting that was wrong is the ceiling; the one that set the
                # floor is a different variable, and an operator debugging the
                # message needs to be sent to both rather than to the default.
                self.assertRegex(stderr, rf"{ceiling}=.*minimum {floor_name}={floor}")

    def test_a_non_numeric_interval_never_reaches_sleep(self) -> None:
        """`sleep abc` fails, errexit ends the supervisor, and nothing says so."""
        for name in _CLAMPED_SETTINGS:
            with self.subTest(setting=name):
                values, stderr = self._clamped(**{name: "abc"})

                self.assertRegex(
                    values[name],
                    r"^[0-9]+$",
                    f"{name} would be passed to `sleep` as written",
                )
                self.assertIn(name, stderr, f"{name} was clamped without saying so")

    def test_a_leading_zero_is_normalised_rather_than_passed_through(self) -> None:
        """`08` is all digits and in range, and still kills the supervisor.

        Every arithmetic expansion downstream reads it as octal -- `08: value too
        great for base` -- which under errexit ends the subshell. A check that
        tested only the shape would let it through, so the clamp rewrites the
        value rather than merely accepting it.
        """
        for name in _CLAMPED_SETTINGS:
            with self.subTest(setting=name):
                overrides: dict[str, object] = {name: _LEADING_ZERO_VALUE}
                if name in _CEILING_FLOORS:
                    overrides[_CEILING_FLOORS[name]] = 1
                values, _ = self._clamped(**overrides)
                usable = _run_bash(f"set -e; echo $(( {values[name]} + 1 ))")

                self.assertEqual(
                    usable.returncode,
                    0,
                    f"{name}={values[name]} still fails bash arithmetic: "
                    f"{usable.stderr.strip()}",
                )
                self.assertEqual(int(values[name]), int(_LEADING_ZERO_VALUE))

    def test_usable_values_are_left_alone_and_say_nothing(self) -> None:
        """The clamp is a floor, not a policy: it must not rewrite a good value."""
        values, stderr = self._clamped()

        self.assertEqual(
            values,
            {name: str(_GOOD_INTERVAL_SECONDS) for name in _CLAMPED_SETTINGS},
        )
        self.assertEqual(stderr.strip(), "")


class TheDaemonAddressIsDeclaredOnceAndUsedByBoth(_ScriptCase):
    """Both launchers post to the same loopback daemon.

    The watcher's `--daemon-url` carried the literal for as long as the watcher
    was the only one there. It stopped being safe when the detector's wait began
    probing a named host and port: changing the port would then take two edits,
    and the second one is silent.
    """

    def test_the_url_is_composed_from_the_host_and_port(self) -> None:
        composed = _run_bash(
            "set -u\n"
            + lift_constant("KV_DAEMON_HOST", self.text, _SCRIPT)
            + lift_constant("KV_DAEMON_PORT", self.text, _SCRIPT)
            + lift_constant("KV_DAEMON_URL", self.text, _SCRIPT)
            + 'printf "%s|%s|%s" "${KV_DAEMON_HOST}" "${KV_DAEMON_PORT}" "${KV_DAEMON_URL}"'
        )
        host, port, url = composed.stdout.split("|")

        self.assertEqual(url, f"http://{host}:{port}")

    def test_neither_launcher_writes_the_address_out(self) -> None:
        for name in ("start_event_watcher", "start_drift_detector"):
            with self.subTest(launcher=name):
                launcher = self.lift(name)
                self.assertIn('--daemon-url="${KV_DAEMON_URL}"', launcher)
                self.assertNotRegex(
                    launcher,
                    r"--daemon-url=http",
                    f"{name} writes the daemon address out instead of using KV_DAEMON_URL, "
                    "so changing the port takes two edits and the second is silent",
                )


if __name__ == "__main__":
    unittest.main()
