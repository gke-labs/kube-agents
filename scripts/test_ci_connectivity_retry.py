"""The deploy job's connectivity check must survive a dropped port-forward.

`hack/ci-deploy.sh` verifies the Platform Agent by curling one real inference
through a `kubectl port-forward` tunnel. On cold autoscaling pools that tunnel
drops mid-request ("error: lost connection to pod") while the gateway pod is
healthy, and a dead port-forward never comes back on its own — so the check
retries with a FRESH tunnel per attempt, bounds the curl with --max-time, and
only hard-fails after every attempt has spoken.

Like scripts/test_ci_eval_trap.py, this lifts the real section out of the real
file and runs it under bash with stubbed kubectl/curl/nc, so it fails if the
retry loop is removed, if the tunnel stops being respawned per attempt, or if
the curl loses its deadline.
"""

import os
import pathlib
import re
import stat
import subprocess
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEPLOY_SCRIPT = REPO_ROOT / "hack" / "ci-deploy.sh"
ENV_SCRIPT = REPO_ROOT / "hack" / "ci-env.sh"

SECTION_START = "# ─── 7. Agent API Connectivity Verification"
SECTION_END = "TOTAL_DURATION="

KUBECTL_STUB = """#!/usr/bin/env bash
echo "$@" >> "${STUB_LOG}/kubectl.args"
case "$1" in
  port-forward)
    echo "Forwarding from 127.0.0.1:8642 -> 8642"
    exec sleep 60
    ;;
  get)
    # `get secret ... -o jsonpath=...` -> base64("test-key")
    echo "dGVzdC1rZXk="
    ;;
esac
exit "${KUBECTL_EXIT:-0}"
"""

CURL_STUB = """#!/usr/bin/env bash
echo "$@" >> "${STUB_LOG}/curl.args"
n=$(cat "${STUB_LOG}/curl.count" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "${STUB_LOG}/curl.count"
if [ "$n" -ge "${CURL_SUCCEED_ON:-1}" ]; then
  echo '{"output": [{"role": "assistant", "content": "pong"}]}'
fi
exit 0
"""

NC_STUB = """#!/usr/bin/env bash
exit 0
"""


def connectivity_section() -> str:
    """The connectivity-check section as written, lifted from the script."""
    src = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    match = re.search(
        rf"^{re.escape(SECTION_START)}.*?(?=^{re.escape(SECTION_END)})",
        src,
        re.S | re.M,
    )
    if match is None:  # pragma: no cover - a rename should say so loudly
        raise AssertionError(f"connectivity section not found in {DEPLOY_SCRIPT}")
    return match.group(0)


def dump_function() -> str:
    """dump_prow_artifacts_on_failure() as written, lifted from ci-env.sh."""
    src = ENV_SCRIPT.read_text(encoding="utf-8")
    match = re.search(
        r"^dump_prow_artifacts_on_failure\(\) \{\n.*?^\}$", src, re.S | re.M
    )
    if match is None:  # pragma: no cover
        raise AssertionError(f"dump_prow_artifacts_on_failure() not found in {ENV_SCRIPT}")
    return match.group(0)


def write_stub(directory: pathlib.Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def run_check(succeed_on: int) -> tuple[subprocess.CompletedProcess, pathlib.Path]:
    """Run the lifted section with the curl stub succeeding on attempt N."""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="conncheck-"))
    stubs = tmp / "bin"
    stubs.mkdir()
    write_stub(stubs, "kubectl", KUBECTL_STUB)
    write_stub(stubs, "curl", CURL_STUB)
    write_stub(stubs, "nc", NC_STUB)
    script = "\n".join(
        [
            "set -euo pipefail",
            "NAMESPACE=test-ns",
            "STEP_START=0",
            "dump_prow_artifacts_on_failure() { :; }",
            connectivity_section(),
            'echo "SECTION COMPLETED"',
        ]
    )
    env = dict(
        os.environ,
        PATH=f"{stubs}:{os.environ['PATH']}",
        STUB_LOG=str(tmp),
        CURL_SUCCEED_ON=str(succeed_on),
    )
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False, env=env
    )
    return result, tmp


class ConnectivityRetryTest(unittest.TestCase):
    def test_a_dropped_first_attempt_is_retried_on_a_fresh_tunnel(self):
        """One empty response (the 08-31 signature) must not fail the deploy."""
        result, tmp = run_check(succeed_on=2)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SECTION COMPLETED", result.stdout)
        self.assertIn("✓ Agent API Server responded successfully", result.stdout)
        # Exactly one failed attempt, logged as such, then a respawn.
        self.assertEqual(result.stdout.count("respawning tunnel"), 1)
        self.assertIn(
            "connectivity attempt 1/5 failed: empty response after port-forward drop;"
            " respawning tunnel",
            result.stdout,
        )
        # The respawn is real: one port-forward per attempt, so two total.
        kubectl_args = (tmp / "kubectl.args").read_text(encoding="utf-8")
        self.assertEqual(
            len([l for l in kubectl_args.splitlines() if l.startswith("port-forward")]),
            2,
        )

    def test_exhausted_attempts_hard_fail_with_the_tunnel_log_echoed(self):
        result, tmp = run_check(succeed_on=999)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("SECTION COMPLETED", result.stdout)
        for attempt in range(1, 6):
            self.assertIn(f"connectivity attempt {attempt}/5 failed", result.stdout)
        self.assertIn(
            "ERROR: Platform Agent API server connectivity check failed after 5 attempts!",
            result.stdout,
        )
        # The port-forward log tail is echoed for diagnosis, one entry per attempt.
        self.assertIn("=== Debug: Port Forward Log (tail) ===", result.stdout)
        self.assertIn("--- port-forward attempt 5/5 ---", result.stdout)
        # Five fresh tunnels were attempted.
        kubectl_args = (tmp / "kubectl.args").read_text(encoding="utf-8")
        self.assertEqual(
            len([l for l in kubectl_args.splitlines() if l.startswith("port-forward")]),
            5,
        )

    def test_the_inference_curl_carries_a_deadline(self):
        """Without --max-time a half-dead tunnel hangs the job, not fails it."""
        _, tmp = run_check(succeed_on=1)
        curl_args = (tmp / "curl.args").read_text(encoding="utf-8")
        self.assertIn("--max-time", curl_args)


class FailureDumpArtifactsTest(unittest.TestCase):
    """The evidence the 08-31 investigation lacked must be in the failure dump."""

    def run_dump(self, kubectl_exit: int) -> tuple[subprocess.CompletedProcess, pathlib.Path]:
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="conndump-"))
        stubs = tmp / "bin"
        stubs.mkdir()
        write_stub(stubs, "kubectl", KUBECTL_STUB)
        write_stub(stubs, "gcloud", "#!/usr/bin/env bash\nexit ${KUBECTL_EXIT:-0}\n")
        script = "\n".join(
            [
                "set -euo pipefail",
                "collect_bench_results() { :; }",
                dump_function(),
                "false || dump_prow_artifacts_on_failure",
                'echo "SURVIVED"',
            ]
        )
        env = dict(
            os.environ,
            PATH=f"{stubs}:{os.environ['PATH']}",
            STUB_LOG=str(tmp),
            KUBECTL_EXIT=str(kubectl_exit),
            ARTIFACTS=str(tmp / "artifacts"),
            PROJECT_ID="test-project",
            TARGET_NAMESPACE="test-ns",
        )
        result = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, check=False, env=env
        )
        return result, tmp

    def test_the_envoy_sidecar_and_kube_system_events_are_captured(self):
        result, tmp = self.run_dump(kubectl_exit=0)
        self.assertIn("SURVIVED", result.stdout)
        kubectl_args = (tmp / "kubectl.args").read_text(encoding="utf-8")
        self.assertIn("-c envoy-credential-proxy", kubectl_args)
        self.assertRegex(
            kubectl_args, r"get events -n kube-system --sort-by=\.lastTimestamp"
        )

    def test_the_new_captures_cannot_fail_the_dump(self):
        """Every command in the dumper is || true guarded; so are the new two."""
        result, _ = self.run_dump(kubectl_exit=1)
        self.assertIn("SURVIVED", result.stdout)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
