"""The line deploy/shared/start-services.sh writes when the credential path ends.

The script is the proxy container's PID 1 and waits on two processes, the
credential runtime and Envoy. Until this line existed, whichever of them died
ended the container without a word from the script: the broker's own log stops
mid-request, Envoy's says nothing, and the only record is the kubelet's
lastState. The line names the process and its status, and turns a signal exit
into the signal's number, so that a restart on a large fleet (#4315) can be
read from the previous container's log without a second pass over the pod.

Run against the function as it ships, lifted from the script, with the pids
and the status handed to it as the foot of the script hands them.
"""

from __future__ import annotations

import pathlib
import signal
import subprocess
import sys
import time
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from _lift_shell import lift_constant, lift_function  # noqa: E402

_REPO_ROOT = _HERE.parent
_SCRIPT = _REPO_ROOT / "deploy" / "shared" / "start-services.sh"
_RUNTIME_PID = "41"
_ENVOY_PID = "42"
_SIGKILL_STATUS = 137
_SIGTERM_STATUS = 143
_CLEAN_STATUS = 0
_FAILED_STATUS = 3
_CHILD_LIFETIME_SECONDS = 30
_SIGNAL_TIMEOUT_SECONDS = 10
_WAIT_POLL_SECONDS = 0.01
_WCHAN_DEADLINE_SECONDS = 2


def _wait_until_blocked_in_wait(pid: int) -> bool:
    """Poll /proc/<pid>/wchan until bash sits in the kernel's wait; True when
    it was seen there. A kernel that hides wchan reads `0`, and the poll gives
    up after _WCHAN_DEADLINE_SECONDS: bash reaches `wait -n` within
    microseconds of printing READY, so the window left is one assignment."""
    deadline = time.monotonic() + _WCHAN_DEADLINE_SECONDS
    wchan = pathlib.Path("/proc") / str(pid) / "wchan"
    while time.monotonic() < deadline:
        try:
            if "wait" in wchan.read_text():
                return True
        except OSError:
            return False
        time.sleep(_WAIT_POLL_SECONDS)
    return False


def _report(ended_pid: str, status: int, envoy_pid: str = _ENVOY_PID) -> subprocess.CompletedProcess[str]:
    text = _SCRIPT.read_text()
    program = "\n".join(
        [
            lift_constant("SIGNAL_EXIT_BASE", text, _SCRIPT),
            lift_function("report_credential_path_exit", text, _SCRIPT),
            f'runtime_pid="{_RUNTIME_PID}"',
            f'envoy_pid="{envoy_pid}"',
            f'report_credential_path_exit "{ended_pid}" "{status}"',
        ]
    )
    return subprocess.run(["bash", "-c", program], capture_output=True, text=True, check=False)


class ReportCredentialPathExitTest(unittest.TestCase):
    def test_a_killed_runtime_is_named_with_its_signal(self) -> None:
        result = _report(_RUNTIME_PID, _SIGKILL_STATUS)
        self.assertIn("the credential runtime (pid 41) was killed by signal 9 (exit status 137)", result.stderr)
        self.assertIn("the container ends", result.stderr)

    def test_a_terminated_envoy_is_named_with_its_signal(self) -> None:
        result = _report(_ENVOY_PID, _SIGTERM_STATUS)
        self.assertIn("envoy (pid 42) was killed by signal 15 (exit status 143)", result.stderr)

    def test_a_plain_exit_is_reported_as_a_status(self) -> None:
        result = _report(_RUNTIME_PID, _FAILED_STATUS)
        self.assertIn("the credential runtime (pid 41) exited with status 3", result.stderr)
        self.assertNotIn("signal", result.stderr)

    def test_the_status_it_was_given_is_the_status_it_returns(self) -> None:
        # The report is the script's last command, so this is the container's
        # exit status: the kubelet keeps it as the previous container's.
        self.assertEqual(_SIGKILL_STATUS, _report(_RUNTIME_PID, _SIGKILL_STATUS).returncode)
        self.assertEqual(_CLEAN_STATUS, _report(_RUNTIME_PID, _CLEAN_STATUS).returncode)

    def test_a_signal_with_nothing_waited_for_is_the_scripts_own(self) -> None:
        # The kubelet's SIGTERM lands on this script, not on a child: `wait -n`
        # returns 143 having waited for nobody, and the line says so rather
        # than blaming a process that was still running.
        result = _report("", _SIGTERM_STATUS)
        self.assertIn("stopped by signal 15 (exit status 143) before a credential-path process exited", result.stderr)
        self.assertEqual(_SIGTERM_STATUS, result.returncode)

    def test_the_real_foot_survives_the_kubelets_sigterm_under_set_u(self) -> None:
        # The foot as it ships, with the script's own `set` line, two
        # long-lived children standing in for the runtime and Envoy, and a
        # SIGTERM to the script: the report is the last line and 143 is the
        # status. Before `${ended_pid-}` this died on `ended_pid: unbound
        # variable` with status 1. The trap is the test's own two-line
        # `terminate`, not the script's: the subject here is the foot, and the
        # script's trap has callees and constants of its own that other
        # changes move; what the foot needs from it is that it returns.
        text = _SCRIPT.read_text()
        foot = text[text.rindex("exit_status=0\n") :]
        program = "\n".join(
            [
                "set -euo pipefail",
                lift_constant("SIGNAL_EXIT_BASE", text, _SCRIPT),
                lift_function("report_credential_path_exit", text, _SCRIPT),
                'terminate() { trap - EXIT INT TERM; kill "${runtime_pid}" "${envoy_pid}" 2>/dev/null || true; }',
                f"sleep {_CHILD_LIFETIME_SECONDS} & runtime_pid=$!",
                f"sleep {_CHILD_LIFETIME_SECONDS} & envoy_pid=$!",
                "trap terminate EXIT INT TERM",
                "echo READY >&2",
                foot,
            ]
        )
        process = subprocess.Popen(["bash", "-c", program], stderr=subprocess.PIPE, text=True)
        try:
            ready = process.stderr.readline()
            self.assertEqual("READY\n", ready)
            # READY is printed before `wait -n` is entered; a signal that lands
            # on the assignment in between is taken at the next command
            # boundary, after which `wait -n` reaps a child `terminate` killed
            # and the other branch reports. Wait for bash to be in its wait.
            in_wait = _wait_until_blocked_in_wait(process.pid)
            process.send_signal(signal.SIGTERM)
            _, stderr = process.communicate(timeout=_SIGNAL_TIMEOUT_SECONDS)
        finally:
            if process.poll() is None:
                process.kill()
        self.assertEqual(_SIGTERM_STATUS, process.returncode, stderr)
        self.assertNotIn("unbound variable", stderr)
        if in_wait:
            # Signalled inside `wait -n`: nothing was waited for, and the line
            # is the kubelet-stop shape the reference page names.
            self.assertIn("start-services: stopped by signal 15 (exit status 143) before a credential-path process exited; the container ends", stderr)
        else:
            # wchan hidden: the signal may have landed on the assignment before
            # the wait, after which the other branch reports a killed child.
            self.assertIn("start-services: ", stderr)
            self.assertIn("(exit status 143); the container ends", stderr.replace(") before a credential-path process exited;", ");"))

    def test_an_unknown_pid_still_reports_without_a_name(self) -> None:
        # The api-proxy role runs no Envoy, and `wait -n` can in principle
        # return before `-p` is set; neither leaves the status unsaid.
        result = _report("", _FAILED_STATUS, envoy_pid="")
        self.assertIn("a credential-path process (pid ) exited with status 3", result.stderr)

    def test_the_foot_of_the_script_reports_what_it_waited_on(self) -> None:
        text = _SCRIPT.read_text()
        foot = text[text.rindex("wait -n") :]
        self.assertIn('wait -n -p ended_pid "${runtime_pid}" ${envoy_pid} || exit_status=$?', foot)
        # `-p` unsets its variable when nothing was waited for; under the
        # script's `set -u` a bare expansion would end the script on this line.
        self.assertIn('report_credential_path_exit "${ended_pid-}" "${exit_status}"', foot)
        self.assertTrue(
            foot.rstrip().endswith('report_credential_path_exit "${ended_pid-}" "${exit_status}"'),
            "the report must be the script's last command so that its return is the exit status",
        )


if __name__ == "__main__":
    unittest.main()
