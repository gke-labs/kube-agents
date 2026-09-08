"""Unit tests for agentplugins_e2e_test helper functions.

Verifies operator deployment name resolution across Helm and Kustomize installations,
pod selector polling, and deployment rollout existence and retry mechanisms.
"""

import os
import subprocess
import unittest
from unittest.mock import MagicMock, call, patch

import tests.e2e.operator.agentplugins_e2e_test as e2e


class AgentPluginsE2EHelpersTest(unittest.TestCase):
    """Unit tests for operator deployment resolution and rollout polling helpers."""

    def test_get_operator_deployment_honors_env_override(self):
        """When OPERATOR_DEPLOYMENT env var is set, return it directly without kubectl."""
        with patch.dict(os.environ, {"OPERATOR_DEPLOYMENT": "custom-operator-manager"}):
            with patch.object(e2e, "run_kubectl") as mock_kubectl:
                name = e2e.get_operator_deployment(timeout_sec=5)
                self.assertEqual(name, "custom-operator-manager")
                mock_kubectl.assert_not_called()

    def test_get_operator_deployment_resolves_helm_first(self):
        """When Helm deployment exists, resolve to Helm deployment name."""
        with patch.dict(os.environ, {}, clear=True):
            if "OPERATOR_DEPLOYMENT" in os.environ:
                del os.environ["OPERATOR_DEPLOYMENT"]

            def mock_run_kubectl(cmd, check=True, capture_output=False):
                mock_res = MagicMock()
                if cmd[2] == e2e.OPERATOR_DEPLOYMENT_HELM:
                    mock_res.returncode = 0
                else:
                    mock_res.returncode = 1
                return mock_res

            with patch.object(e2e, "run_kubectl", side_effect=mock_run_kubectl):
                name = e2e.get_operator_deployment(timeout_sec=5)
                self.assertEqual(name, e2e.OPERATOR_DEPLOYMENT_HELM)

    def test_get_operator_deployment_resolves_kustomize_fallback(self):
        """When Helm deployment does not exist but Kustomize does, resolve to Kustomize."""
        with patch.dict(os.environ, {}, clear=True):
            if "OPERATOR_DEPLOYMENT" in os.environ:
                del os.environ["OPERATOR_DEPLOYMENT"]

            def mock_run_kubectl(cmd, check=True, capture_output=False):
                mock_res = MagicMock()
                if cmd[2] == e2e.OPERATOR_DEPLOYMENT_KUSTOMIZE:
                    mock_res.returncode = 0
                else:
                    mock_res.returncode = 1
                return mock_res

            with patch.object(e2e, "run_kubectl", side_effect=mock_run_kubectl):
                name = e2e.get_operator_deployment(timeout_sec=5)
                self.assertEqual(name, e2e.OPERATOR_DEPLOYMENT_KUSTOMIZE)

    def test_get_operator_deployment_timeout_defaults_to_helm(self):
        """When neither deployment is found before timeout, default to Helm name."""
        with patch.dict(os.environ, {}, clear=True):
            if "OPERATOR_DEPLOYMENT" in os.environ:
                del os.environ["OPERATOR_DEPLOYMENT"]

            mock_res = MagicMock(returncode=1)
            with patch.object(e2e, "run_kubectl", return_value=mock_res), \
                 patch("time.sleep", return_value=None):
                name = e2e.get_operator_deployment(timeout_sec=0)
                self.assertEqual(name, e2e.OPERATOR_DEPLOYMENT_HELM)

    def test_poll_operator_pod_matches_helm_selector(self):
        """When Helm pod selector matches, return the pod name."""
        def mock_poll_pod(selector, container, expected_image="", timeout_sec=2):
            if selector == e2e.OPERATOR_POD_SELECTOR_HELM:
                return "kube-agents-controller-manager-abc-123"
            return ""

        with patch.object(e2e, "poll_pod_with_image", side_effect=mock_poll_pod):
            pod = e2e.poll_operator_pod(timeout_sec=5)
            self.assertEqual(pod, "kube-agents-controller-manager-abc-123")

    def test_poll_operator_pod_matches_kustomize_selector_fallback(self):
        """When Helm selector does not match but Kustomize selector does, return the pod name."""
        def mock_poll_pod(selector, container, expected_image="", timeout_sec=2):
            if selector == e2e.OPERATOR_POD_SELECTOR_KUSTOMIZE:
                return "kubeagents-controller-manager-xyz-456"
            return ""

        with patch.object(e2e, "poll_pod_with_image", side_effect=mock_poll_pod):
            pod = e2e.poll_operator_pod(timeout_sec=5)
            self.assertEqual(pod, "kubeagents-controller-manager-xyz-456")

    def test_poll_operator_pod_timeout_returns_empty(self):
        """When no operator pod matches any selector, return empty string on timeout."""
        with patch.object(e2e, "poll_pod_with_image", return_value=""), \
             patch("time.sleep", return_value=None):
            pod = e2e.poll_operator_pod(timeout_sec=0)
            self.assertEqual(pod, "")

    def test_wait_deployment_rollout_waits_for_existence_and_succeeds(self):
        """wait_deployment_rollout checks existence first, then succeeds on rollout."""
        calls = []

        def mock_run_kubectl(cmd, check=True, capture_output=False):
            calls.append(cmd)
            mock_res = MagicMock(returncode=0)
            return mock_res

        with patch.object(e2e, "run_kubectl", side_effect=mock_run_kubectl):
            e2e.wait_deployment_rollout("kube-agents-controller-manager", timeout="10s")

        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(calls[0][:3], ["get", "deployment", "kube-agents-controller-manager"])
        self.assertEqual(calls[1][:3], ["rollout", "status", "deployment/kube-agents-controller-manager"])

    def test_wait_deployment_rollout_retries_transient_error(self):
        """Transient error in rollout status retries and succeeds if resolved before deadline."""
        call_count = {"count": 0}

        def mock_run_kubectl(cmd, check=True, capture_output=False):
            if cmd[0] == "get":
                return MagicMock(returncode=0)
            if cmd[0] == "rollout":
                call_count["count"] += 1
                if call_count["count"] == 1:
                    raise subprocess.CalledProcessError(1, cmd)
                return MagicMock(returncode=0)
            return MagicMock(returncode=0)

        with patch.object(e2e, "run_kubectl", side_effect=mock_run_kubectl), \
             patch("time.sleep", return_value=None):
            e2e.wait_deployment_rollout("kube-agents-controller-manager", timeout="20s")

        self.assertEqual(call_count["count"], 2)


if __name__ == "__main__":
    unittest.main()
