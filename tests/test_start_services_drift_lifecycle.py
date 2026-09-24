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
And it must not orphan a process launched during the wait, which is what would
happen if a supervisor in its backoff `sleep` were free to loop.

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

These run bash against the functions and constants as they ship rather than
against a copy, so a fix to the script cannot pass a test of last week's text.
"""

import os
import pathlib
import re
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

    def test_the_supervised_process_receives_sigterm(self) -> None:
        """The first regression: kill the subshell first and this never arrives."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            ready = tmpdir / "ready"
            signalled = tmpdir / "signalled"

            # A supervisor of the shape start_drift_detector uses: a subshell
            # whose only job is to run the process it launched. The process has
            # to be a separate one -- `pkill -P` is how the supervisor's child is
            # reached, and a subshell that traps for itself has no child to find.
            body = f"""
            (
              bash -c 'trap "touch {signalled}; exit 0" TERM
                       touch {ready}
                       for _ in $(seq 1 {_CHILD_LIFETIME_SECONDS * 10}); do sleep 0.1; done' &
              wait
            ) &
            drift_pid=$!
            while [ ! -e {ready} ]; do sleep {_MARKER_POLL_SECONDS}; done
            terminate
            """
            _run_bash(self._harness(body))

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

            # The child's output goes to /dev/null rather than the captured
            # pipe. A child that outlives the harness holds that pipe open, and
            # `subprocess.run` then blocks until it closes -- which would make
            # this test pass on a terminate() that waited for nothing, the
            # waiting having been done by the test.
            body = f"""
            (
              bash -c 'trap "sleep {_CHILD_SHUTDOWN_SECONDS}; touch {done}; exit 0" TERM
                       touch {ready}
                       for _ in $(seq 1 {_CHILD_LIFETIME_SECONDS * 10}); do sleep 0.1; done' \
                >/dev/null 2>&1 &
              wait
            ) &
            drift_pid=$!
            while [ ! -e {ready} ]; do sleep {_MARKER_POLL_SECONDS}; done
            terminate
            touch {returned}
            echo "terminate-returned"
            """
            started = time.monotonic()
            result = _run_bash(self._harness(body, drain=_CHILD_SHUTDOWN_SECONDS * 5))
            elapsed = time.monotonic() - started

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
            pidfile = pathlib.Path(tmp) / "pid"

            # This child outlives the harness by design, so it records its pid
            # for the cleanup below rather than being left to the reaper.
            body = f"""
            (
              bash -c 'trap "" TERM
                       echo $$ > {pidfile}
                       for _ in $(seq 1 {_CHILD_LIFETIME_SECONDS * 10}); do sleep 0.1; done' \
                >/dev/null 2>&1 &
              wait
            ) &
            drift_pid=$!
            while [ ! -s {pidfile} ]; do sleep {_MARKER_POLL_SECONDS}; done
            terminate
            echo "terminate-returned"
            """
            try:
                started = time.monotonic()
                result = _run_bash(self._harness(body))
                elapsed = time.monotonic() - started
            finally:
                _kill_recorded(pidfile)

            self.assertIn("terminate-returned", result.stdout)
            self.assertLess(
                elapsed,
                _DRAIN_CEILING_SECONDS,
                "terminate() outlasted its drain budget waiting on a process that "
                "will never exit",
            )

    def test_a_supervisor_in_its_backoff_sleep_does_not_relaunch(self) -> None:
        """The third: killing the backoff `sleep` must end the supervisor.

        A crash-looping detector spends most of its time here, so this is the
        ordinary state at shutdown rather than an edge. `set -e` is what stops
        the loop from starting a replacement the drain would then orphan, which
        makes the shell option load-bearing -- hence a test rather than a
        comment.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            ready = tmpdir / "ready"
            relaunched = tmpdir / "relaunched"

            body = f"""
            (
              while true; do
                touch {ready}
                sleep {_CHILD_LIFETIME_SECONDS}
                touch {relaunched}
              done
            ) &
            drift_pid=$!
            while [ ! -e {ready} ]; do sleep {_MARKER_POLL_SECONDS}; done
            sleep {_MARKER_POLL_SECONDS}
            terminate
            """
            _run_bash(self._harness(body))
            time.sleep(_MARKER_POLL_SECONDS * 4)

            self.assertFalse(
                relaunched.exists(),
                "the supervisor looped after its backoff sleep was killed, so the "
                "process it started next was never signalled",
            )

        # The above proves the mechanism against a supervisor of the same shape.
        # This proves the two real ones still have that shape: a `|| true` on
        # either backoff sleep would leave errexit with nothing to act on, and
        # the orphan would come back with every test above still green.
        for name in ("start_event_watcher", "start_drift_detector"):
            with self.subTest(launcher=name):
                self.assertRegex(
                    self.lift(name),
                    re.compile(r'^ *sleep "\$\{delay\}"$', re.M),
                    f"{name}'s backoff sleep no longer fails the supervisor, so "
                    "terminate() can orphan the process it relaunches",
                )

    def test_both_supervisors_are_drained_and_the_signal_precedes_the_kill(self) -> None:
        drain = self.lift("drain_supervised")
        pkill_at = drain.find("pkill -TERM -P")
        wait_at = drain.find("pgrep -P")
        kill_at = drain.find("\n    kill ")

        self.assertNotEqual(pkill_at, -1, "drain_supervised no longer signals children")
        self.assertNotEqual(wait_at, -1, "drain_supervised no longer waits for them to exit")
        self.assertNotEqual(kill_at, -1, "drain_supervised no longer kills the supervisors")
        self.assertLess(
            pkill_at,
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
        DRIFT_DAEMON_WAIT_SECONDS={wait}
        DRIFT_DAEMON_POLL_SECONDS={poll}
        {self._clamp()}
        {self.lift("wait_for_drift_daemon")}
        wait_for_drift_daemon
        echo "returned=$?"
        """

    def _clamp(self) -> str:
        """The shipped guard on DRIFT_DAEMON_POLL_SECONDS, lifted as text.

        It is an `if` at file scope rather than a function, so there is nothing
        for `lift_function` to take.
        """
        match = re.search(
            r"^if \[\[ ! \"\$\{DRIFT_DAEMON_POLL_SECONDS\}\".*?^fi$",
            self.text,
            re.S | re.M,
        )
        assert match is not None, f"{_SCRIPT} no longer clamps DRIFT_DAEMON_POLL_SECONDS"
        return match.group(0)

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
