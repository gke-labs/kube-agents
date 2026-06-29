import unittest
from unittest.mock import patch, MagicMock
import json
import subprocess
import sys
from pathlib import Path

# Add the directory containing platform_mcp_server.py to sys.path so it can be imported
sys.path.insert(0, str(Path(__file__).parent.absolute()))

from platform_mcp_server import get_cc_pod_diagnostics, switch_kube_context


class TestCcPodDiagnostics(unittest.TestCase):

    @patch('platform_mcp_server.switch_kube_context')
    @patch('platform_mcp_server.subprocess.run')
    def test_get_cc_pod_diagnostics_success(self, mock_run, mock_switch):
        mock_response_get = MagicMock()
        mock_response_get.stdout = '{"status": {"phase": "Running"}}'
        
        mock_response_desc = MagicMock()
        mock_response_desc.stdout = 'Name: bootstrap-pod'
        
        mock_response_logs = MagicMock()
        mock_response_logs.stdout = 'Starting bootstrap...'

        # Mock the three subprocess.run calls sequentially
        mock_run.side_effect = [mock_response_get, mock_response_desc, mock_response_logs]

        result = get_cc_pod_diagnostics("bootstrap-pod-xyz", "proj", "clust", "loc")

        self.assertIn("=== POD STATUS (JSON) ===", result)
        self.assertIn('{"status": {"phase": "Running"}}', result)
        self.assertIn("=== POD DESCRIBE ===", result)
        self.assertIn("Name: bootstrap-pod", result)
        self.assertIn("=== POD LOGS (TAIL=100) ===", result)
        self.assertIn("Starting bootstrap...", result)

        self.assertEqual(mock_run.call_count, 3)
        mock_switch.assert_called_once_with("proj", "clust", "loc")

    @patch('platform_mcp_server.switch_kube_context')
    @patch('platform_mcp_server.subprocess.run')
    def test_get_cc_pod_diagnostics_partial_failure(self, mock_run, mock_switch):
        mock_response_get = MagicMock()
        mock_response_get.stdout = '{"status": {"phase": "Failed"}}'
        
        # Simulate describe failure
        mock_response_desc_error = subprocess.CalledProcessError(
            returncode=1,
            cmd="kubectl describe ...",
            stderr="Error describing pod"
        )
        
        mock_response_logs = MagicMock()
        mock_response_logs.stdout = 'OOMKilled logs'

        mock_run.side_effect = [mock_response_get, mock_response_desc_error, mock_response_logs]

        result = get_cc_pod_diagnostics("bootstrap-pod-xyz", "proj", "clust", "loc")

        self.assertIn("=== POD STATUS (JSON) ===", result)
        self.assertIn("=== POD DESCRIBE ERROR ===", result)
        self.assertIn("Error describing pod", result)
        self.assertIn("=== POD LOGS (TAIL=100) ===", result)
        self.assertIn("OOMKilled logs", result)
        mock_switch.assert_called_once_with("proj", "clust", "loc")

    def test_get_cc_pod_diagnostics_invalid_format(self):
        result = get_cc_pod_diagnostics("bootstrap-pod_#")
        self.assertEqual(result, "ERROR: Invalid pod name format. Only lowercase alphanumeric and hyphens are allowed.")

    def test_get_cc_pod_diagnostics_unauthorized(self):
        result = get_cc_pod_diagnostics("kube-dns-xyz")
        self.assertEqual(result, "ERROR: Unauthorized pod name. Access is restricted to pods starting with 'bootstrap-' or 'cnrm-'.")


class TestSwitchKubeContext(unittest.TestCase):

    @patch('platform_mcp_server.subprocess.run')
    def test_switch_kube_context_noop(self, mock_run):
        switch_kube_context("", "my-cluster", "us-central1")
        mock_run.assert_not_called()

        switch_kube_context("my-project", "", "us-central1")
        mock_run.assert_not_called()

        switch_kube_context("my-project", "my-cluster", "")
        mock_run.assert_not_called()

    @patch('platform_mcp_server.subprocess.run')
    def test_switch_kube_context_success(self, mock_run):
        switch_kube_context("my-project", "my-cluster", "us-central1")
        
        mock_run.assert_called_once_with(
            [
                "gcloud", "container", "clusters", "get-credentials", "my-cluster",
                "--location=us-central1",
                "--project=my-project"
            ],
            capture_output=True, text=True, check=True
        )

if __name__ == '__main__':
    unittest.main()
