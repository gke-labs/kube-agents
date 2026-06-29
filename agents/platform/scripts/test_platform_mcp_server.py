import unittest
from unittest.mock import patch, MagicMock
import json
import subprocess
import sys
from pathlib import Path

# Add the directory containing platform_mcp_server.py to sys.path so it can be imported
sys.path.insert(0, str(Path(__file__).parent.absolute()))

from platform_mcp_server import get_cc_pod_diagnostics


class TestCcPodDiagnostics(unittest.TestCase):

    @patch('platform_mcp_server.subprocess.run')
    def test_get_cc_pod_diagnostics_success(self, mock_run):
        mock_response_get = MagicMock()
        mock_response_get.stdout = '{"status": {"phase": "Running"}}'
        
        mock_response_desc = MagicMock()
        mock_response_desc.stdout = 'Name: bootstrap-pod'
        
        mock_response_logs = MagicMock()
        mock_response_logs.stdout = 'Starting bootstrap...'

        # Mock the three subprocess.run calls sequentially
        mock_run.side_effect = [mock_response_get, mock_response_desc, mock_response_logs]

        result = get_cc_pod_diagnostics("bootstrap-pod-xyz")

        self.assertIn("=== POD STATUS (JSON) ===", result)
        self.assertIn('{"status": {"phase": "Running"}}', result)
        self.assertIn("=== POD DESCRIBE ===", result)
        self.assertIn("Name: bootstrap-pod", result)
        self.assertIn("=== POD LOGS (TAIL=100) ===", result)
        self.assertIn("Starting bootstrap...", result)

        self.assertEqual(mock_run.call_count, 3)

    @patch('platform_mcp_server.subprocess.run')
    def test_get_cc_pod_diagnostics_partial_failure(self, mock_run):
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

        result = get_cc_pod_diagnostics("bootstrap-pod-xyz")

        self.assertIn("=== POD STATUS (JSON) ===", result)
        self.assertIn("=== POD DESCRIBE ERROR ===", result)
        self.assertIn("Error describing pod", result)
        self.assertIn("=== POD LOGS (TAIL=100) ===", result)
        self.assertIn("OOMKilled logs", result)

    def test_get_cc_pod_diagnostics_invalid_format(self):
        result = get_cc_pod_diagnostics("bootstrap-pod_#")
        self.assertEqual(result, "ERROR: Invalid pod name format. Only lowercase alphanumeric and hyphens are allowed.")

    def test_get_cc_pod_diagnostics_unauthorized(self):
        result = get_cc_pod_diagnostics("kube-dns-xyz")
        self.assertEqual(result, "ERROR: Unauthorized pod name. Access is restricted to pods starting with 'bootstrap-' or 'cnrm-'.")

if __name__ == '__main__':
    unittest.main()
