"""Tests for hack/ci-teardown.sh step accounting and lease heartbeat.

Runs the real script with stub gcloud/kubectl/helm binaries. The failure
scenario reproduces the 2026-09-01 incident on kube-agents-evals-4: the CRD
delete dies against an unreachable control plane and the
ValidatingAdmissionPolicy sweep is skipped by a failed API discovery. The old
script printed an unconditional checkmark over both; these tests pin the
truthful behaviour, the strict-exit opt-in, and that a Boskos heartbeat runs
for the teardown's whole duration.
"""

import subprocess
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
TEARDOWN = REPO_ROOT / "hack" / "ci-teardown.sh"

PROJECT = "kube-agents-evals-4"
EXPECTED_CONTEXT = f"gke_{PROJECT}_us-central1_platform-agent-host"

STUB_GCLOUD = "#!/bin/bash\nexit 0\n"
# Every helm and kubectl call lands in STUB_CALLS, so a test can assert what
# the teardown did not touch.
STUB_HELM = '#!/bin/bash\necho "helm $*" >> "${STUB_CALLS:-/dev/null}"\necho "release uninstalled"\nexit 0\n'

# KUBECTL_MODE=ok: everything succeeds. KUBECTL_MODE=incident: the CRD delete
# fails with connection refused and the VAP kind fails discovery — the
# 2026-09-01 log, verbatim failure modes.
STUB_KUBECTL = f"""#!/bin/bash
echo "kubectl $*" >> "${{STUB_CALLS:-/dev/null}}"
if [[ "$1 $2" == "config current-context" ]]; then
  echo "{EXPECTED_CONTEXT}"; exit 0
fi
if [[ "$1" == "get" ]]; then
  exit 0  # empty read: no Helm release records survive Step 1
fi
if [[ "$KUBECTL_MODE" == "incident" ]]; then
  if [[ "$1" == "delete" && "$2" == "-f" ]]; then
    echo 'dial tcp 34.123.149.88:443: connect: connection refused' >&2; exit 1
  fi
  if [[ "$*" == *validatingadmissionpolicies.admissionregistration.k8s.io* ]]; then
    echo 'error: the server doesn'"'"'t have a resource type "validatingadmissionpolicies"' >&2; exit 1
  fi
fi
echo "deleted"; exit 0
"""


class _FakeBoskos(BaseHTTPRequestHandler):
    updates = []  # (name, owner)
    # None accepts any owner; a name answers 401 (ranch's OwnerNotMatch) to
    # every other owner, which is what a project re-leased to another run
    # tells the first run's teardown (#3129).
    owner = None
    # Codes to answer before the owner check decides, one per beat: 503 is
    # what a restarting Boskos answers, and the daemon reads it like a
    # timed-out curl, an indeterminate beat.
    scripted = []

    def do_POST(self):
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        owner = q.get("owner", [""])[0]
        _FakeBoskos.updates.append((q.get("name", [""])[0], owner))
        if _FakeBoskos.scripted:
            self.send_response(_FakeBoskos.scripted.pop(0))
        else:
            ours = _FakeBoskos.owner is None or owner == _FakeBoskos.owner
            self.send_response(200 if ours else 401)
        self.end_headers()

    def log_message(self, *args):
        pass


class CiTeardownStatusTest(unittest.TestCase):
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
        _FakeBoskos.owner = None
        _FakeBoskos.scripted = []
        self.tmp = TemporaryDirectory()
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        for name, body in (
            ("gcloud", STUB_GCLOUD),
            ("helm", STUB_HELM),
            ("kubectl", STUB_KUBECTL),
        ):
            stub = bin_dir / name
            stub.write_text(body)
            stub.chmod(0o755)
        self.bin_dir = bin_dir

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, kubectl_mode, extra_env=None):
        import os

        env = {
            **os.environ,
            "PATH": f"{self.bin_dir}:{os.environ['PATH']}",
            "PROJECT_ID": PROJECT,
            "KUBECTL_MODE": kubectl_mode,
            "BOSKOS_HEARTBEAT_LOG": str(Path(self.tmp.name) / "beats.log"),
            "STUB_CALLS": str(Path(self.tmp.name) / "calls"),
        }
        # Scrub the Prow/Boskos identity the suite may inherit from its own CI
        # environment (JOB_NAME/BUILD_ID are always set in Prow): without
        # this, the script under test derives a real owner and heartbeats the
        # REAL in-cluster Boskos from inside the presubmit.
        for key in ("JOB_NAME", "BUILD_ID", "BOSKOS_HOST",
                    "BOSKOS_RESOURCE_NAME", "BOSKOS_OWNER_NAME"):
            env.pop(key, None)
        env.update(extra_env or {})
        return subprocess.run(
            ["bash", str(TEARDOWN)],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def test_clean_teardown_reports_all_checkmarks_and_exit_zero(self):
        result = self._run("ok")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("✓ Release uninstall finished", result.stdout)
        self.assertIn("✓ CRD step (deleted) finished", result.stdout)
        self.assertIn("Cleanup Complete", result.stdout)
        self.assertNotIn("✗", result.stdout)

    def test_incident_failures_are_reported_not_masked(self):
        result = self._run("incident")
        # Default stays exit 0: the wrapper must still reach its release.
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("✗ CRD step (delete timed out or failed", result.stdout)
        # The VAP kind erroring "doesn't have a resource type" counts absent,
        # not failed: real on 1.29 clusters, and false-✗ noise everywhere else.
        self.assertIn("(0/6 kinds failed, 1 absent)", result.stdout)
        self.assertIn("FINISHED WITH 1 FAILED STEP(S)", result.stdout)
        self.assertNotIn("Cleanup Complete", result.stdout)

    def test_strict_mode_exits_nonzero_on_failure(self):
        result = self._run("incident", {"CI_TEARDOWN_STRICT": "1"})
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FINISHED WITH 1 FAILED STEP(S)", result.stdout)

    def test_heartbeat_disabled_without_boskos_env_single_line(self):
        result = self._run("ok")
        disabled = [
            ln for ln in result.stdout.splitlines() if "boskos-heartbeat: disabled" in ln
        ]
        self.assertEqual(len(disabled), 1, result.stdout)

    def test_heartbeat_identity_derived_from_prow_env(self):
        # Inside Prow (JOB_NAME/BUILD_ID present) the teardown derives the
        # lease identity the wrapper used — owner "${JOB_NAME}-${BUILD_ID}",
        # resource PROJECT_ID — with no BOSKOS_* wiring from the job config.
        result = self._run(
            "ok",
            {
                "BOSKOS_HOST": self.boskos_host,  # env override of the in-cluster default
                "JOB_NAME": "pull-kube-agents-smoke-test",
                "BUILD_ID": "2094805894989615104",
                "BOSKOS_HEARTBEAT_INTERVAL_SECONDS": "0.1",
            },
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertGreaterEqual(len(_FakeBoskos.updates), 1, result.stdout)
        name, owner = _FakeBoskos.updates[0]
        self.assertEqual(name, PROJECT)
        self.assertEqual(owner, "pull-kube-agents-smoke-test-2094805894989615104")

    def test_heartbeat_beats_during_teardown(self):
        result = self._run(
            "ok",
            {
                "BOSKOS_HOST": self.boskos_host,
                "BOSKOS_RESOURCE_NAME": PROJECT,
                "BOSKOS_OWNER_NAME": "pull-kube-agents-smoke-test",
                "BOSKOS_HEARTBEAT_INTERVAL_SECONDS": "0.1",
            },
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("boskos-heartbeat: started", result.stdout)
        self.assertGreaterEqual(len(_FakeBoskos.updates), 1)
        self.assertTrue(all(n == PROJECT for n, _ in _FakeBoskos.updates))
        # The daemon must not outlive the script.
        time.sleep(0.5)
        beats_after_exit = len(_FakeBoskos.updates)
        time.sleep(0.5)
        self.assertEqual(len(_FakeBoskos.updates), beats_after_exit,
                         "heartbeat daemon leaked past teardown exit")

    def _calls(self):
        calls = Path(self.tmp.name) / "calls"
        return calls.read_text() if calls.exists() else ""

    def test_a_401_on_the_first_beat_skips_the_cluster_steps(self):
        # The project is another run's now (#3129): the wrapper's heartbeat
        # stopped at its default timeout, the reaper freed the project, a
        # second run leased it and installed, and at the ceiling the first
        # run's teardown uninstalled that install. Boskos's 401 on the first
        # beat is the signal, and the answer is to touch nothing.
        _FakeBoskos.owner = "pull-kube-agents-smoke-test-2110536740778283008"
        result = self._run(
            "ok",
            {
                "BOSKOS_HOST": self.boskos_host,
                "JOB_NAME": "pull-kube-agents-smoke-test",
                "BUILD_ID": "2110452492650156032",
                "BOSKOS_HEARTBEAT_INTERVAL_SECONDS": "0.1",
            },
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("401", result.stdout)
        self.assertIn("no longer this run's lease", result.stdout)
        self.assertNotIn("Step 1: Uninstalling", result.stdout)
        self.assertNotIn("Cleanup Complete", result.stdout)
        self.assertNotIn("helm uninstall", self._calls())
        self.assertNotIn("kubectl delete", self._calls())

    def _prow_env(self, **extra):
        return {
            "BOSKOS_HOST": self.boskos_host,
            "JOB_NAME": "pull-kube-agents-smoke-test",
            "BUILD_ID": "2110452492650156032",
            "BOSKOS_HEARTBEAT_INTERVAL_SECONDS": "0.2",
            **extra,
        }

    def test_an_indeterminate_first_beat_then_a_401_still_refuses(self):
        # The bot's walk of the incident with one blip: the first beat is not
        # an answer, the second is the 401 that must still refuse.
        _FakeBoskos.owner = "pull-kube-agents-smoke-test-2110536740778283008"
        _FakeBoskos.scripted = [503]
        result = self._run("ok", self._prow_env())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("beats: 503 401", result.stdout)
        self.assertNotIn("Step 1: Uninstalling", result.stdout)
        self.assertNotIn("helm uninstall", self._calls())
        self.assertNotIn("kubectl delete", self._calls())

    def test_an_indeterminate_first_beat_then_a_200_runs_the_cluster_steps(self):
        _FakeBoskos.owner = "pull-kube-agents-smoke-test-2110452492650156032"
        _FakeBoskos.scripted = [503]
        result = self._run("ok", self._prow_env())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Step 1: Uninstalling", result.stdout)
        self.assertIn("helm uninstall", self._calls())

    def test_no_definitive_answer_within_the_budget_refuses(self):
        _FakeBoskos.scripted = [503] * 50
        result = self._run("ok", self._prow_env(CI_TEARDOWN_LEASE_CHECK_WAIT_SECONDS="2"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("no definitive answer", result.stdout)
        self.assertNotIn("Step 1: Uninstalling", result.stdout)
        self.assertNotIn("helm uninstall", self._calls())

    def test_the_lease_check_outlasts_the_wrappers_heartbeat(self):
        # On the opening sweep the wrapper's `boskosctl heartbeat` is running
        # and stops the job after nine failed beats in a row; Step 0 must wait
        # longer than that, so a silent Boskos ends in an answer or a stopped
        # job, never in a skipped sweep the deploy then trips over.
        import re

        text = TEARDOWN.read_text(encoding="utf-8")
        default = int(re.search(r'LEASE_CHECK_WAIT_SECONDS:-(\d+)\}', text).group(1))
        wrapper_period_seconds, wrapper_retries = 30, 8
        self.assertGreater(default, (wrapper_retries + 1) * wrapper_period_seconds)

    def test_a_200_on_the_first_beat_runs_the_cluster_steps(self):
        _FakeBoskos.owner = "pull-kube-agents-smoke-test-2110452492650156032"
        result = self._run(
            "ok",
            {
                "BOSKOS_HOST": self.boskos_host,
                "JOB_NAME": "pull-kube-agents-smoke-test",
                "BUILD_ID": "2110452492650156032",
                "BOSKOS_HEARTBEAT_INTERVAL_SECONDS": "0.1",
            },
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Step 1: Uninstalling", result.stdout)
        self.assertIn("Cleanup Complete", result.stdout)
        self.assertIn("helm uninstall", self._calls())


if __name__ == "__main__":
    unittest.main(verbosity=2)
