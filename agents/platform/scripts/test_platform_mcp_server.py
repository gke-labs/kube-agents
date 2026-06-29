import unittest
from unittest.mock import patch, MagicMock
import json
import subprocess
import sys
from pathlib import Path

# Add the directory containing platform_mcp_server.py to sys.path so it can be imported
sys.path.insert(0, str(Path(__file__).parent.absolute()))

from platform_mcp_server import list_cc_healthchecks, get_cc_operator_status, list_cc_pods, switch_kube_context


class TestCcDiagnosticTools(unittest.TestCase):

    @patch('platform_mcp_server.switch_kube_context')
    @patch('platform_mcp_server.subprocess.run')
    def test_list_cc_healthchecks_success(self, mock_run, mock_switch):
        mock_response = MagicMock()
        mock_response.stdout = '{"items": []}'
        mock_run.return_value = mock_response
        mock_switch.return_value = ""

        result = list_cc_healthchecks("proj", "clust", "loc")

        self.assertEqual(result, '{"items": []}')
        mock_switch.assert_called_once_with("proj", "clust", "loc")
        mock_run.assert_called_once_with(
            [
                "kubectl", "get", "healthchecks.healthcheck.config.gke.io",
                "-n", "krmapihosting-system",
                "-o", "json"
            ],
            capture_output=True, text=True, check=True, timeout=30
        )

    @patch('platform_mcp_server.switch_kube_context')
    @patch('platform_mcp_server.subprocess.run')
    def test_list_cc_healthchecks_failure(self, mock_run, mock_switch):
        mock_switch.return_value = ""
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=1,
            cmd="kubectl ...",
            stderr="Error from server (NotFound)"
        )

        result = list_cc_healthchecks("proj", "clust", "loc")

        self.assertTrue(result.startswith("ERROR:"))
        self.assertIn("Error from server (NotFound)", result)
        mock_switch.assert_called_once_with("proj", "clust", "loc")

    @patch('platform_mcp_server.switch_kube_context')
    @patch('platform_mcp_server.subprocess.run')
    def test_get_cc_operator_status_success(self, mock_run, mock_switch):
        mock_response = MagicMock()
        mock_response.stdout = '{"status": "Active"}'
        mock_run.return_value = mock_response
        mock_switch.return_value = ""

        result = get_cc_operator_status("proj", "clust", "loc")

        self.assertEqual(result, '{"status": "Active"}')
        mock_switch.assert_called_once_with("proj", "clust", "loc")
        mock_run.assert_called_once_with(
            [
                "kubectl", "get", "configconnectors.core.cnrm.cloud.google.com",
                "configconnector",
                "-o", "json"
            ],
            capture_output=True, text=True, check=True, timeout=30
        )

    @patch('platform_mcp_server.switch_kube_context')
    @patch('platform_mcp_server.subprocess.run')
    def test_get_cc_operator_status_failure(self, mock_run, mock_switch):
        mock_switch.return_value = ""
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=1,
            cmd="kubectl ...",
            stderr="Error from server (NotFound)"
        )

        result = get_cc_operator_status("proj", "clust", "loc")

        self.assertTrue(result.startswith("ERROR:"))
        self.assertIn("Error from server (NotFound)", result)
        mock_switch.assert_called_once_with("proj", "clust", "loc")

    @patch('platform_mcp_server.switch_kube_context')
    @patch('platform_mcp_server.subprocess.run')
    def test_list_cc_pods_success(self, mock_run, mock_switch):
        mock_response = MagicMock()
        mock_response.stdout = json.dumps({
            "items": [
                {
                    "metadata": {"name": "bootstrap-pod-xyz"},
                    "status": {"phase": "Running"}
                },
                {
                    "metadata": {"name": "cnrm-controller-manager-123"},
                    "status": {"phase": "Pending"}
                },
                {
                    "metadata": {"name": "kube-dns-5c68f"},
                    "status": {"phase": "Running"}
                }
            ]
        })
        mock_run.return_value = mock_response
        mock_switch.return_value = ""

        result_str = list_cc_pods("proj", "clust", "loc")
        result = json.loads(result_str)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["name"], "bootstrap-pod-xyz")
        self.assertEqual(result[0]["status"], "Running")
        self.assertEqual(result[1]["name"], "cnrm-controller-manager-123")
        self.assertEqual(result[1]["status"], "Pending")
        mock_switch.assert_called_once_with("proj", "clust", "loc")

        mock_run.assert_called_once_with(
            [
                "kubectl", "get", "pods",
                "-n", "krmapihosting-system",
                "-o", "json"
            ],
            capture_output=True, text=True, check=True, timeout=30
        )

    @patch('platform_mcp_server.switch_kube_context')
    @patch('platform_mcp_server.subprocess.run')
    def test_list_cc_pods_failure(self, mock_run, mock_switch):
        mock_switch.return_value = ""
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=1,
            cmd="kubectl ...",
            stderr="Error listing pods"
        )

        result = list_cc_pods("proj", "clust", "loc")

        self.assertTrue(result.startswith("ERROR:"))
        self.assertIn("Error listing pods", result)
        mock_switch.assert_called_once_with("proj", "clust", "loc")


class TestSwitchKubeContext(unittest.TestCase):

    @patch('platform_mcp_server.subprocess.run')
    def test_switch_kube_context_noop(self, mock_run):
        self.assertEqual(switch_kube_context("", "my-cluster", "us-central1"), "")
        mock_run.assert_not_called()

        self.assertEqual(switch_kube_context("my-project", "", "us-central1"), "")
        mock_run.assert_not_called()

        self.assertEqual(switch_kube_context("my-project", "my-cluster", ""), "")
        mock_run.assert_not_called()

    @patch('platform_mcp_server.subprocess.run')
    def test_switch_kube_context_success(self, mock_run):
        result = switch_kube_context("my-project", "my-cluster", "us-central1")

        self.assertEqual(result, "")
        mock_run.assert_called_once_with(
            [
                "gcloud", "container", "clusters", "get-credentials", "my-cluster",
                "--location=us-central1",
                "--project=my-project"
            ],
            capture_output=True, text=True, check=True, timeout=30
        )

    @patch('platform_mcp_server.subprocess.run')
    def test_switch_kube_context_failure(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=1,
            cmd="gcloud ...",
            stderr="ERROR: (gcloud) Not authorized"
        )

        result = switch_kube_context("my-project", "my-cluster", "us-central1")

        self.assertIn("Failed to switch kube context", result)
        self.assertIn("Not authorized", result)


class TestContextSwitchFailurePropagation(unittest.TestCase):

    @patch('platform_mcp_server.switch_kube_context')
    @patch('platform_mcp_server.subprocess.run')
    def test_context_switch_error_returned_by_tool(self, mock_run, mock_switch):
        mock_switch.return_value = "ERROR: Failed to switch kube context to cluster 'bad-cluster'.\nExit Code: 1\nStderr: Not authorized"

        result = list_cc_healthchecks("proj", "bad-cluster", "loc")

        self.assertIn("Failed to switch kube context", result)
        mock_run.assert_not_called()

if __name__ == '__main__':
    unittest.main()
