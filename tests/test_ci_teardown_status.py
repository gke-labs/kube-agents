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
STUB_HELM = '#!/bin/bash\necho "release uninstalled"\nexit 0\n'

# KUBECTL_MODE=ok: everything succeeds. KUBECTL_MODE=incident: the CRD delete
# fails with connection refused and the VAP kind fails discovery — the
# 2026-09-01 log, verbatim failure modes.
STUB_KUBECTL = f"""#!/bin/bash
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

    def do_POST(self):
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        _FakeBoskos.updates.append(
            (q.get("name", [""])[0], q.get("owner", [""])[0])
        )
        self.send_response(200)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
