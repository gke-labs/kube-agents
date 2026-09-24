"""The drift-detector's two lifecycle edges in deploy/shared/start-services.sh.

Both are invisible when they break. The container stays Ready either way, the
detector's own logs look the same, and what is lost -- a graceful shutdown, or
the truthfulness of the one ALERT this feature has -- shows up somewhere else
entirely, days later.

`terminate` has to signal each supervised process before it kills the subshell
supervising it. The other order reparents the process to PID 1 the moment the
subshell dies, after which `pkill -P` on the dead subshell's pid matches
nothing, the detector never sees SIGTERM, and the shutdown that NACKs its
unfinished Pub/Sub records -- so that they redeliver rather than being dropped
-- does not run.

`wait_for_drift_daemon` has to wait for the Session KV server before the first
launch, and has to do it inside the supervisor subshell. This container is a
native sidecar, so it starts before the platform-agent container the daemon
runs in; the detector's startup check against the daemon is fatal by design, so
without the wait it exits on connection-refused two or three times on every
cold start and the supervisor prints "NO out-of-band changes are being
detected" while nothing is wrong. Waiting in the foreground instead would trade
that for a startup path blocked for as long as the deadline.

These run bash against the functions as they ship rather than against a copy,
so a fix to the script cannot pass a test of last week's text.
"""

import pathlib
import re
import socket
import subprocess
import sys
import time
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from _lift_shell import lift_function  # noqa: E402

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

# The deadline handed to wait_for_drift_daemon when the test wants it to expire.
# Two polls of one second: enough to prove it loops rather than falling straight
# through, short enough to keep the suite fast.
_SHORT_WAIT_SECONDS = 2
_SHORT_POLL_SECONDS = 1

# What "returned promptly" means for the connect-succeeds case. One poll
# interval is 1s in that test, so anything under it proves the function did not
# sleep before noticing the listener.
_PROMPT_RETURN_SECONDS = 1.0

# A port nothing is listening on. Chosen by binding and closing, so it was free
# a moment ago; nothing here binds it again.
def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_for_file(path: pathlib.Path) -> bool:
    deadline = time.monotonic() + _MARKER_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(_MARKER_POLL_SECONDS)
    return False


def _run_bash(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=_MARKER_TIMEOUT_SECONDS * 3,
    )


class TerminateSignalsTheProcessNotJustItsSupervisor(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _SCRIPT.read_text()
        self.terminate = lift_function("terminate", self.text, _SCRIPT)

    def test_the_supervised_process_receives_sigterm(self) -> None:
        """The regression: kill the subshell first and this never arrives."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            ready = tmpdir / "ready"
            signalled = tmpdir / "signalled"

            # A supervisor of the shape start_drift_detector uses: a subshell
            # whose only job is to wait on the process it launched.
            harness = f"""
            set -u
            {self.terminate}
            runtime_pid=""
            envoy_pid=""
            watcher_pid=""
            (
              bash -c 'trap "touch {signalled}; exit 0" TERM
                       touch {ready}
                       for _ in $(seq 1 {_CHILD_LIFETIME_SECONDS * 10}); do sleep 0.1; done' &
              wait
            ) &
            drift_pid=$!
            """
            proc = subprocess.Popen(
                ["bash", "-c", harness + "\nwhile [ ! -e %s ]; do sleep 0.05; done\nterminate\nsleep 2\n" % ready],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                proc.wait(timeout=_MARKER_TIMEOUT_SECONDS * 3)
            except subprocess.TimeoutExpired:  # pragma: no cover - diagnostic
                proc.kill()
                self.fail("the harness did not finish")

            self.assertTrue(
                signalled.exists(),
                "the supervised process did not receive SIGTERM; terminate() most likely "
                "killed the supervisor subshell first, reparenting the process to PID 1 "
                "where `pkill -P` on the dead pid matches nothing",
            )

    def test_both_supervisors_signal_their_child_before_dying(self) -> None:
        """The watcher's branch and the detector's, which drifted apart once."""
        for pid_var in ("watcher_pid", "drift_pid"):
            with self.subTest(pid_var=pid_var):
                branch = re.search(
                    r'if \[\[ -n "\$\{%s\}" \]\]; then\n(.*?)\n  fi' % pid_var,
                    self.terminate,
                    re.S,
                )
                self.assertIsNotNone(branch, f"terminate() no longer has a {pid_var} branch")
                body = branch.group(1)
                pkill_at = body.find("pkill")
                kill_at = body.find("\n    kill ")
                self.assertNotEqual(pkill_at, -1, f"{pid_var} branch no longer signals children")
                self.assertNotEqual(kill_at, -1, f"{pid_var} branch no longer kills the supervisor")
                self.assertLess(
                    pkill_at,
                    kill_at,
                    f"{pid_var}: the supervisor is killed before its child is signalled, so the "
                    "child is reparented to PID 1 and the pkill matches nothing",
                )


class WaitForDriftDaemon(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _SCRIPT.read_text()
        self.func = lift_function("wait_for_drift_daemon", self.text, _SCRIPT)

    def _harness(self, port: int, wait: int, poll: int) -> str:
        return f"""
        set -u
        readonly DRIFT_DAEMON_HOST=127.0.0.1
        readonly DRIFT_DAEMON_PORT={port}
        readonly DRIFT_DAEMON_URL="http://127.0.0.1:{port}"
        DRIFT_DAEMON_WAIT_SECONDS={wait}
        DRIFT_DAEMON_POLL_SECONDS={poll}
        {self.func}
        wait_for_drift_daemon
        echo "returned=$?"
        """

    def test_it_returns_as_soon_as_the_daemon_is_listening(self) -> None:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]

            started = time.monotonic()
            result = _run_bash(self._harness(port, _SHORT_WAIT_SECONDS, _SHORT_POLL_SECONDS))
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
        result = _run_bash(self._harness(_free_port(), _SHORT_WAIT_SECONDS, _SHORT_POLL_SECONDS))

        self.assertIn("returned=0", result.stdout)
        self.assertIn("was not listening", result.stderr)

    def test_it_says_nothing_when_the_daemon_was_there(self) -> None:
        """The log line is the context for a later ALERT, not an event."""
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            result = _run_bash(
                self._harness(listener.getsockname()[1], _SHORT_WAIT_SECONDS, _SHORT_POLL_SECONDS)
            )

        self.assertEqual(result.stderr.strip(), "")

    def test_the_wait_happens_inside_the_supervisor_subshell(self) -> None:
        """In front of it, the container's startup path blocks for the deadline."""
        launcher = lift_function("start_drift_detector", self.text, _SCRIPT)
        call_at = launcher.find("wait_for_drift_daemon")
        subshell_at = launcher.find("\n  (\n")

        self.assertNotEqual(call_at, -1, "start_drift_detector no longer waits for the daemon")
        self.assertNotEqual(subshell_at, -1, "start_drift_detector no longer has a supervisor subshell")
        self.assertGreater(
            call_at,
            subshell_at,
            "the daemon wait runs before the supervisor subshell is backgrounded, so it holds "
            "up everything this script starts after the detector",
        )


if __name__ == "__main__":
    unittest.main()
