"""Tests for the deploy-lifetime Boskos lease heartbeat in hack/ci-deploy.sh (#3129).

Section 2 starts hack/boskos_heartbeat.sh for the deploy's own lifetime, with
the lease identity derived from the Prow environment as ci-teardown.sh and
ci-eval-pr.sh derive it, and stops it from the EXIT trap after the failure
dump. Each test lifts that block as written, with the file-head constants, and
runs it against a fake Boskos /update endpoint. Pinned: beats arrive with the
wrapper's owner convention and the leased project's name; the daemon does not
outlive the script on a clean exit or on a failure; outside Prow nothing is
derived and the daemon disables itself with one line.
"""

import os
import pathlib
import subprocess
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlparse

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"

_BLOCK_START = 'DEPLOY_HEARTBEAT_PID=""'
_BLOCK_END = "\nensure_helm\n"

PROJECT = "kube-agents-evals-24"
JOB_NAME = "pull-kube-agents-smoke-test"
BUILD_ID = "2110452492650156032"


def _text():
    return _CI_DEPLOY.read_text(encoding="utf-8")


def _constants_block():
    return "\n".join(line for line in _text().splitlines() if line.startswith("readonly "))


def _heartbeat_block():
    text = _text()
    start = text.find(_BLOCK_START)
    assert start != -1, f"{_BLOCK_START!r} not found in hack/ci-deploy.sh"
    end = text.find(_BLOCK_END, start)
    assert end != -1, f"{_BLOCK_END!r} not found after {_BLOCK_START!r}"
    return text[start:end]


class _FakeBoskos(BaseHTTPRequestHandler):
    updates = []  # (name, owner, state)

    def do_POST(self):
        q = parse_qs(urlparse(self.path).query)
        _FakeBoskos.updates.append(
            (q.get("name", [""])[0], q.get("owner", [""])[0], q.get("state", [""])[0])
        )
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


class CiDeployLeaseHeartbeatTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeBoskos)
        cls.boskos_host = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        _FakeBoskos.updates.clear()
        self.tmp = TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, exit_status, extra_env=None, in_prow=True):
        script = "\n".join(
            [
                "set -euo pipefail",
                f'SCRIPT_DIR="{_REPO_ROOT / "hack"}"',
                # The failure dump the trap calls first; it is ci-env.sh's and
                # not under test here, so it only records the status it saw.
                'dump_prow_artifacts_on_failure() { echo "DUMP SAW $?"; }',
                _constants_block(),
                _heartbeat_block(),
                # One beat's worth of the deploy, then the exit under test.
                "sleep 0.5",
                f"exit {exit_status}",
            ]
        )
        env = {
            **os.environ,
            "PROJECT_ID": PROJECT,
            "BOSKOS_HEARTBEAT_INTERVAL_SECONDS": "0.1",
            "BOSKOS_HEARTBEAT_LOG": str(pathlib.Path(self.tmp.name) / "beats.log"),
        }
        for key in ("JOB_NAME", "BUILD_ID", "BOSKOS_HOST", "BOSKOS_RESOURCE_NAME", "BOSKOS_OWNER_NAME"):
            env.pop(key, None)
        if in_prow:
            env.update({"JOB_NAME": JOB_NAME, "BUILD_ID": BUILD_ID, "BOSKOS_HOST": self.boskos_host})
        env.update(extra_env or {})
        return subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60,
        )

    def _assert_daemon_stopped(self):
        time.sleep(0.5)
        after = len(_FakeBoskos.updates)
        time.sleep(0.5)
        self.assertEqual(len(_FakeBoskos.updates), after, "heartbeat daemon outlived the deploy")

    def test_beats_carry_the_wrappers_owner_and_the_leased_project(self):
        result = self._run(0)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("boskos-heartbeat: started", result.stdout)
        self.assertGreaterEqual(len(_FakeBoskos.updates), 1, result.stdout)
        for name, owner, state in _FakeBoskos.updates:
            self.assertEqual((name, owner, state), (PROJECT, f"{JOB_NAME}-{BUILD_ID}", "busy"))
        self._assert_daemon_stopped()

    def test_the_daemon_stops_on_a_failure_and_the_dump_still_sees_the_status(self):
        result = self._run(7)
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertIn("DUMP SAW 7", result.stdout)
        self.assertGreaterEqual(len(_FakeBoskos.updates), 1, result.stdout)
        self._assert_daemon_stopped()

    def test_outside_prow_nothing_is_derived_and_the_daemon_disables_itself(self):
        result = self._run(0, in_prow=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        disabled = [ln for ln in result.stdout.splitlines() if "boskos-heartbeat: disabled" in ln]
        self.assertEqual(len(disabled), 1, result.stdout)
        self.assertEqual(_FakeBoskos.updates, [])

    def test_every_exit_trap_stops_the_daemon_after_the_dump(self):
        """The three `trap ... EXIT` sites: the one armed before the daemon
        starts, section 7's replacement (which also kills the port-forward)
        and the restore after it. Each runs the failure dump first, because
        it reads the exit status, and stops the daemon after it; none re-arms
        the bare dump, which would orphan the daemon."""
        text = _text()
        traps = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("trap ") and ln.strip().endswith(" EXIT")]
        self.assertEqual(traps, ["trap deploy_exit EXIT", "trap cleanup_pf_and_dump EXIT", "trap deploy_exit EXIT"], traps)
        self.assertIn("deploy_exit() {\n  dump_prow_artifacts_on_failure\n  stop_deploy_heartbeat\n}", text)
        self.assertIn(
            "cleanup_pf_and_dump() {\n  kill \"${PF_PID:-}\" 2>/dev/null || true\n  dump_prow_artifacts_on_failure\n  stop_deploy_heartbeat\n}",
            text,
        )
        self.assertLess(text.index("trap deploy_exit EXIT"), text.index('"${SCRIPT_DIR}/boskos_heartbeat.sh" &'))


if __name__ == "__main__":
    unittest.main(verbosity=2)
