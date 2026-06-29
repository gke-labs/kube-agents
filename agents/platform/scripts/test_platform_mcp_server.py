import unittest
from unittest.mock import patch, MagicMock
import json
import subprocess
import sys
from pathlib import Path

# Add the directory containing platform_mcp_server.py to sys.path so it can be imported
sys.path.insert(0, str(Path(__file__).parent.absolute()))

from platform_mcp_server import list_cc_healthchecks, get_cc_operator_status


class TestCcDiagnosticTools(unittest.TestCase):

    @patch('platform_mcp_server.subprocess.run')
    def test_list_cc_healthchecks_success(self, mock_run):
        mock_response = MagicMock()
        mock_response.stdout = '{"items": []}'
        mock_run.return_value = mock_response

        result = list_cc_healthchecks()

        self.assertEqual(result, '{"items": []}')
        mock_run.assert_called_once_with(
            [
                "kubectl", "get", "healthchecks.healthcheck.config.gke.io",
                "-n", "krmapihosting-system",
                "-o", "json"
            ],
            capture_output=True, text=True, check=True
        )

    @patch('platform_mcp_server.subprocess.run')
    def test_list_cc_healthchecks_failure(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=1,
            cmd="kubectl ...",
            stderr="Error from server (NotFound)"
        )

        result = list_cc_healthchecks()

        self.assertTrue(result.startswith("ERROR:"))
        self.assertIn("Error from server (NotFound)", result)

    @patch('platform_mcp_server.subprocess.run')
    def test_get_cc_operator_status_success(self, mock_run):
        mock_response = MagicMock()
        mock_response.stdout = '{"status": "Active"}'
        mock_run.return_value = mock_response

        result = get_cc_operator_status()

        self.assertEqual(result, '{"status": "Active"}')
        mock_run.assert_called_once_with(
            [
                "kubectl", "get", "configconnectors.core.cnrm.cloud.google.com",
                "configconnector",
                "-o", "json"
            ],
            capture_output=True, text=True, check=True
        )

    @patch('platform_mcp_server.subprocess.run')
    def test_get_cc_operator_status_failure(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=1,
            cmd="kubectl ...",
            stderr="Error from server (NotFound)"
        )

        result = get_cc_operator_status()

        self.assertTrue(result.startswith("ERROR:"))
        self.assertIn("Error from server (NotFound)", result)

if __name__ == '__main__':
    unittest.main()
