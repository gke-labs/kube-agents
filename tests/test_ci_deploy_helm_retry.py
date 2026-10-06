"""Tests for the Helm chart install retry on transient API-server 5xx errors (#2382).

The deploy script retries the `helm upgrade --install` call a bounded number
of times when the Kubernetes API server responds with a transient 5xx
(Internal error occurred:, the server is currently unable to handle the request,
the server was unable to return a response in the time allotted, etc.)
during control-plane scaling or cluster startup.

These tests pin:
* A successful Helm install completes on attempt 1 with no retry.
* Transient API-server 5xx errors (webhook InternalError, client-go 503/504 sentences,
  etcdserver errors, timeout phrases, and bounded 5xx codes) retry and proceed to evaluation.
* The retry log explicitly names the attempt and transient 5xx.
* Persistent 5xx errors exhaust the bounded retries and fail with the helm exit code.
* Non-5xx errors fail immediately on the first attempt without retrying.
"""

import json
import pathlib
import stat
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"

_DEPLOY_START = "# ─── 5c. Deploy the chart"
_DEPLOY_END = "# ─── 6. Readiness Verification"

_NAMESPACE = "kubeagents-system"


def _deploy_text():
    return _CI_DEPLOY.read_text(encoding="utf-8")


def _head_constants(text):
    """The file-head `readonly` declarations the deploy block reads."""
    return "\n".join(
        line for line in text.splitlines() if line.startswith("readonly ")
    )


def _heal_function(text):
    """The heal_poisoned_release_record helper defined in hack/ci-deploy.sh."""
    start = text.find("heal_poisoned_release_record() {")
    assert start != -1, "heal_poisoned_release_record() not found in hack/ci-deploy.sh"
    end = text.find("\n}\n", start)
    assert end != -1, "closing brace of heal_poisoned_release_record() not found"
    return text[start : end + 3]


def _deploy_block(text):
    start = text.find(_DEPLOY_START)
    assert start != -1, f"{_DEPLOY_START!r} not found in hack/ci-deploy.sh"
    end = text.find(_DEPLOY_END, start)
    assert end != -1, f"{_DEPLOY_END!r} not found after {_DEPLOY_START!r}"
    return text[start:end]


class CiDeployHelmRetryTest(unittest.TestCase):
    maxDiff = None

    def _run_deploy_block(self, helm_responses, history_json="", history_exit=None):
        """Run the lifted deploy block with recording stubs.

        helm_responses: list of (exit_code, stdout, stderr) tuples returned
                        sequentially on each `helm upgrade --install` call.
        history_exit: exit code for `helm history`. Defaults to 0 when history_json
                      is provided, or 1 (absent release) when empty.
        """
        if history_exit is None:
            history_exit = 0 if history_json else 1

        text = _deploy_text()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            log = tmp_path / "calls.log"
            log.touch()

            # Store responses in a json file for the stub to consume
            responses_file = tmp_path / "responses.json"
            responses_file.write_text(json.dumps(helm_responses), encoding="utf-8")
            index_file = tmp_path / "call_index.txt"
            index_file.write_text("0", encoding="utf-8")

            history_file = tmp_path / "history.json"
            history_file.write_text(history_json, encoding="utf-8")

            helm_stub = bin_dir / "helm"
            helm_stub.write_text(
                f"""#!/usr/bin/env bash
echo "helm $*" >> "{log}"
case "$1" in
  history)
    cat "{history_file}"
    exit {history_exit}
    ;;
  uninstall)
    exit 0
    ;;
  upgrade)
    idx=$(cat "{index_file}")
    resp=$(python3 -c '
import json, sys
data = json.load(open("{responses_file}"))
idx = int(sys.argv[1])
if idx < len(data):
    code, out, err = data[idx]
    if out: sys.stdout.write(out + "\\n")
    if err: sys.stderr.write(err + "\\n")
    sys.exit(code)
sys.exit(0)
' "$idx")
    rc=$?
    echo $((idx + 1)) > "{index_file}"
    echo "$resp"
    exit $rc
    ;;
  *)
    exit 0
    ;;
esac
""",
                encoding="utf-8",
            )

            kubectl_stub = bin_dir / "kubectl"
            kubectl_stub.write_text(
                f"""#!/usr/bin/env bash
echo "kubectl $*" >> "{log}"
exit 0
""",
                encoding="utf-8",
            )

            sleep_stub = bin_dir / "sleep"
            sleep_stub.write_text(
                f"""#!/usr/bin/env bash
echo "sleep $*" >> "{log}"
exit 0
""",
                encoding="utf-8",
            )

            for stub in (helm_stub, kubectl_stub, sleep_stub):
                stub.chmod(stub.stat().st_mode | stat.S_IXUSR)

            sandbox_dir = tmp_path / "sandbox_keys"
            sandbox_dir.mkdir()
            (sandbox_dir / "id_sandbox").touch()
            (sandbox_dir / "id_sandbox.pub").touch()

            env_vars = {
                "NAMESPACE": _NAMESPACE,
                "CLUSTER_NAME": "test-cluster",
                "REGION": "us-central1",
                "PROJECT_ID": "test-project",
                "GSA_NAME": "test-gsa",
                "GITOPS_REPO": "test-repo",
                "API_SERVER_KEY": "test-key",
                "GEMINI_API_KEY": "test-gemini",
                "SANDBOX_KEY_DIR": str(sandbox_dir),
                "MODEL_PROVIDER": "vertex_ai",
                "MODEL_DEFAULT_NAME": "gemini-2.5-flash",
                "LITELLM_GSA_NAME": "litellm-gsa",
            }

            preamble = """
STEP_START=$SECONDS
IMAGE_ARGS=()
GITHUB_MINTER_ARGS=()
A2A_OPERATOR_ENV_ARGS=()
"""
            proc = subprocess.run(
                [
                    "bash",
                    "-c",
                    "set -euo pipefail\n"
                    + _head_constants(text)
                    + "\n"
                    + _heal_function(text)
                    + "\n"
                    + preamble
                    + "\n"
                    + _deploy_block(text),
                ],
                capture_output=True,
                text=True,
                cwd=_REPO_ROOT,
                env=get_isolated_test_env(bin_dir=bin_dir, overrides=env_vars),
            )
            calls = log.read_text(encoding="utf-8").splitlines()
        return proc.returncode, calls, proc.stdout, proc.stderr

    def test_a_successful_helm_install_pays_one_call_and_no_retry(self):
        rc, calls, out, err = self._run_deploy_block([(0, "Release kube-agents installed", "")])
        self.assertEqual(rc, 0, err)
        helm_upgrades = [c for c in calls if c.startswith("helm upgrade")]
        self.assertEqual(len(helm_upgrades), 1, f"expected exactly 1 helm call: {calls}")
        self.assertNotIn("retrying", out)
        self.assertNotIn("retrying", err)

    def test_a_transient_api_server_internal_error_webhook_retries_and_succeeds(self):
        # Real webhook admission failure during fresh cluster setup (500 InternalError)
        # from bench/upgrade-scenarios/evidence/07/webhook.txt.
        err_msg = (
            'Error from server (InternalError): Internal error occurred: failed calling webhook '
            '"gate.scen.example.com": failed to call webhook: '
            'Post "https://absent-hook.scen.svc:443/validate?timeout=5s": no endpoints available for service "absent-hook"'
        )
        responses = [
            (1, "", err_msg),
            (0, "Release kube-agents installed", ""),
        ]
        rc, calls, out, err = self._run_deploy_block(responses)
        self.assertEqual(rc, 0, err)
        helm_upgrades = [c for c in calls if c.startswith("helm upgrade")]
        self.assertEqual(len(helm_upgrades), 2, f"expected 2 helm calls: {calls}")
        self.assertIn("hit a transient API-server 5xx, retrying", out)
        self.assertIn("attempt 1 of 3", out)

    def test_a_transient_unable_to_handle_request_retries_and_succeeds(self):
        # Exercises client-go 503 'the server is currently unable to handle the request'.
        err_msg = "Error from server (ServiceUnavailable): the server is currently unable to handle the request (post configmaps)"
        responses = [
            (1, "", err_msg),
            (0, "Release kube-agents installed", ""),
        ]
        rc, calls, out, err = self._run_deploy_block(responses)
        self.assertEqual(rc, 0, err)
        helm_upgrades = [c for c in calls if c.startswith("helm upgrade")]
        self.assertEqual(len(helm_upgrades), 2, f"expected 2 helm calls: {calls}")
        self.assertIn("hit a transient API-server 5xx, retrying", out)

    def test_a_transient_server_unable_to_return_response_in_time_retries_and_succeeds(self):
        # Exercises client-go 504 'the server was unable to return a response in the time allotted, but may still be processing the request'.
        err_msg = (
            "Error from server (Timeout): the server was unable to return a response "
            "in the time allotted, but may still be processing the request (get deployments)"
        )
        responses = [
            (1, "", err_msg),
            (0, "Release kube-agents installed", ""),
        ]
        rc, calls, out, err = self._run_deploy_block(responses)
        self.assertEqual(rc, 0, err)
        helm_upgrades = [c for c in calls if c.startswith("helm upgrade")]
        self.assertEqual(len(helm_upgrades), 2, f"expected 2 helm calls: {calls}")
        self.assertIn("hit a transient API-server 5xx, retrying", out)

    def test_a_transient_an_error_on_the_server_retries_and_succeeds(self):
        # Exercises client-go 500/502/other when no Status body is returned.
        err_msg = 'an error on the server ("connection reset by peer") has prevented the request from succeeding (post configmaps)'
        responses = [
            (1, "", err_msg),
            (0, "Release kube-agents installed", ""),
        ]
        rc, calls, out, err = self._run_deploy_block(responses)
        self.assertEqual(rc, 0, err)
        helm_upgrades = [c for c in calls if c.startswith("helm upgrade")]
        self.assertEqual(len(helm_upgrades), 2, f"expected 2 helm calls: {calls}")
        self.assertIn("hit a transient API-server 5xx, retrying", out)

    def test_a_transient_etcdserver_timeout_retries_and_succeeds(self):
        # Exercises transient etcd control plane failure.
        err_msg = "etcdserver: leader changed while evaluating transaction"
        responses = [
            (1, "", err_msg),
            (0, "Release kube-agents installed", ""),
        ]
        rc, calls, out, err = self._run_deploy_block(responses)
        self.assertEqual(rc, 0, err)
        helm_upgrades = [c for c in calls if c.startswith("helm upgrade")]
        self.assertEqual(len(helm_upgrades), 2, f"expected 2 helm calls: {calls}")
        self.assertIn("hit a transient API-server 5xx, retrying", out)

    def test_a_transient_server_responded_with_status_code_retries_and_succeeds(self):
        # Exercises client-go 'the server responded with the status code 50[0234]'.
        err_msg = "the server responded with the status code 502"
        responses = [
            (1, "", err_msg),
            (0, "Release kube-agents installed", ""),
        ]
        rc, calls, out, err = self._run_deploy_block(responses)
        self.assertEqual(rc, 0, err)
        helm_upgrades = [c for c in calls if c.startswith("helm upgrade")]
        self.assertEqual(len(helm_upgrades), 2, f"expected 2 helm calls: {calls}")
        self.assertIn("hit a transient API-server 5xx, retrying", out)

    def test_a_transient_bounded_5xx_http_code_retries_and_succeeds(self):
        # Exercises bounded HTTP status code from upstream ingress or proxy.
        err_msg = "HTTP response status 502 from control plane load balancer"
        responses = [
            (1, "", err_msg),
            (0, "Release kube-agents installed", ""),
        ]
        rc, calls, out, err = self._run_deploy_block(responses)
        self.assertEqual(rc, 0, err)
        helm_upgrades = [c for c in calls if c.startswith("helm upgrade")]
        self.assertEqual(len(helm_upgrades), 2, f"expected 2 helm calls: {calls}")
        self.assertIn("hit a transient API-server 5xx, retrying", out)

    def test_helm_api_server_5xx_regex_matches_all_alternations(self):
        # Pin that HELM_API_SERVER_5XX_RE matches every alternation and rejects non-5xx errors.
        import re

        text = _deploy_text()
        m = re.search(r'^readonly HELM_API_SERVER_5XX_RE=["\']([^"\']+)["\']', text, re.MULTILINE)
        self.assertIsNotNone(m, "HELM_API_SERVER_5XX_RE not found in hack/ci-deploy.sh")
        assert m is not None
        pattern = re.compile(m.group(1))

        # Each distinct alternation required by the regex:
        # 1. an error on the server (client-go 500/502/other when no Status body)
        # 2. the server is currently unable to handle the request (client-go 503)
        # 3. the server was unable to return a response in the time allotted (client-go 504)
        # 4. the server responded with the status code 50[0234] (client-go status sentence)
        # 5. Internal error occurred: (API server 500 / admission webhook failure)
        # 6. etcdserver: (etcd transient timeout / leader election)
        # 7. request did not complete within (API server request timeout)
        # 8. (HTTP|status( code)?)[: ]+50[0234]([^0-9]|$) (bounded 5xx HTTP/status codes)
        signatures = [
            'an error on the server ("connection reset") has prevented the request from succeeding',
            "the server is currently unable to handle the request (post configmaps)",
            "the server was unable to return a response in the time allotted, but may still be processing the request",
            "the server responded with the status code 502",
            "the server responded with the status code 503",
            "the server responded with the status code 504",
            "the server responded with the status code 500",
            'Error from server (InternalError): Internal error occurred: failed calling webhook "gate.example.com"',
            "etcdserver: request timed out",
            "Timeout: request did not complete within the allotted timeout",
            "HTTP response status 502 from control plane load balancer",
            "HTTP 500 Internal Server Error",
            "status code 504",
            "status: 503",
        ]
        for sig in signatures:
            with self.subTest(signature=sig):
                self.assertTrue(pattern.search(sig), f"pattern must match signature: {sig!r}")

        # Non-matching client errors, operational failures, line numbers, and resource quantities:
        non_5xx = [
            "release: already exists",
            "cannot re-use a name that is still in use",
            "timed out waiting for the condition",
            "404 Not Found",
            "401 Unauthorized",
            "403 Forbidden",
            "invalid chart values",
            "failed to connect to host:8502 connection refused",
            "exit status 5000",
            "replicaCount=2500",
            "execution error at (kube-agents/templates/_helpers.tpl:502:14)",
            "yaml: line 503:",
            "exceeded quota: requested: cpu=500m",
            "Invalid value: \"500m\"",
            "cpu: 500m",
            "requested: 500Mi",
            ":502:",
            "-c504f-",
            "504s",
            "pid 503",
        ]
        for non in non_5xx:
            with self.subTest(non_5xx=non):
                self.assertFalse(pattern.search(non), f"pattern must NOT match non-5xx: {non!r}")

    def test_persistent_5xx_exhausts_retries_and_fails(self):
        err_msg = "Timeout: request did not complete within the allotted timeout"
        responses = [
            (42, "", err_msg),
            (42, "", err_msg),
            (42, "", err_msg),
        ]
        rc, calls, out, err = self._run_deploy_block(responses)
        self.assertEqual(rc, 42, f"persistent 5xx must fail with helm exit code: {rc}")
        helm_upgrades = [c for c in calls if c.startswith("helm upgrade")]
        self.assertEqual(len(helm_upgrades), 3, f"expected 3 helm calls: {calls}")
        self.assertIn("attempt 1 of 3 hit a transient API-server 5xx, retrying", out)
        self.assertIn("attempt 2 of 3 hit a transient API-server 5xx, retrying", out)

    def test_transient_5xx_heals_poisoned_release_record_before_retry(self):
        err_msg = "the server is currently unable to handle the request"
        responses = [
            (1, "", err_msg),
            (0, "Release kube-agents installed", ""),
        ]
        history_poisoned = json.dumps([{"revision": 1, "status": "failed"}])
        rc, calls, out, err = self._run_deploy_block(responses, history_json=history_poisoned, history_exit=0)
        self.assertEqual(rc, 0, err)
        cr_deletes = [c for c in calls if c.startswith("kubectl delete platformagent")]
        self.assertEqual(len(cr_deletes), 1, f"expected CR delete before uninstall: {calls}")
        self.assertIn("--wait", cr_deletes[0])
        self.assertIn("--ignore-not-found", cr_deletes[0])
        uninstalls = [c for c in calls if c.startswith("helm uninstall")]
        self.assertEqual(len(uninstalls), 1, f"expected poisoned record to be healed before retry: {calls}")
        # Verify CR delete precedes helm uninstall
        cr_idx = calls.index(cr_deletes[0])
        uninstall_idx = calls.index(uninstalls[0])
        self.assertLess(cr_idx, uninstall_idx, "CR delete must precede helm uninstall")
        self.assertIn("record before retrying", out.lower())
        self.assertIn("cleared the poisoned", out.lower())

    def test_transient_5xx_with_deployed_revision_does_not_heal_release_record(self):
        # A release with a deployed revision is healthy and must not be uninstalled (#2382 bot review).
        err_msg = "the server is currently unable to handle the request"
        responses = [
            (1, "", err_msg),
            (0, "Release kube-agents installed", ""),
        ]
        history_healthy = json.dumps([{"revision": 1, "status": "deployed"}, {"revision": 2, "status": "failed"}])
        rc, calls, out, err = self._run_deploy_block(responses, history_json=history_healthy, history_exit=0)
        self.assertEqual(rc, 0, err)
        cr_deletes = [c for c in calls if c.startswith("kubectl delete platformagent")]
        self.assertEqual(len(cr_deletes), 0, f"healthy release must not delete CR: {calls}")
        uninstalls = [c for c in calls if c.startswith("helm uninstall")]
        self.assertEqual(len(uninstalls), 0, f"release with deployed revision must not be uninstalled: {calls}")
        self.assertNotIn("cleared the poisoned", out.lower())
        self.assertNotIn("exists with no deployed revision", out.lower())

    def test_transient_5xx_absent_release_does_not_heal_release_record(self):
        # When helm history exits 1 (fresh project or no release yet), heal must not fire.
        err_msg = "the server is currently unable to handle the request"
        responses = [
            (1, "", err_msg),
            (0, "Release kube-agents installed", ""),
        ]
        rc, calls, out, err = self._run_deploy_block(responses, history_json="", history_exit=1)
        self.assertEqual(rc, 0, err)
        cr_deletes = [c for c in calls if c.startswith("kubectl delete platformagent")]
        self.assertEqual(len(cr_deletes), 0, f"absent release must not delete CR: {calls}")
        uninstalls = [c for c in calls if c.startswith("helm uninstall")]
        self.assertEqual(len(uninstalls), 0, f"absent release must not issue uninstall: {calls}")
        self.assertNotIn("cleared the poisoned", out.lower())
        self.assertNotIn("exists with no deployed revision", out.lower())

    def test_non_5xx_error_fails_immediately_without_retry(self):
        err_msg = "Error: execution error at (kube-agents/templates/deployment.yaml:10:14): invalid value"
        responses = [
            (17, "", err_msg),
            (0, "should not be reached", ""),
        ]
        rc, calls, out, err = self._run_deploy_block(responses)
        self.assertEqual(rc, 17, f"non-5xx must fail with helm exit code: {rc}")
        helm_upgrades = [c for c in calls if c.startswith("helm upgrade")]
        self.assertEqual(len(helm_upgrades), 1, f"non-5xx must not retry: {calls}")
        self.assertNotIn("retrying", out)


if __name__ == "__main__":
    unittest.main()
