import unittest
from unittest.mock import patch, MagicMock
import json
import subprocess
import sys
from pathlib import Path

# Add the directory containing platform_mcp_server.py to sys.path so it can be imported
sys.path.insert(0, str(Path(__file__).parent.absolute()))

from platform_mcp_server import verify_gke_cluster

class TestVerifyGkeCluster(unittest.TestCase):

    @patch('platform_mcp_server.get_project_id')
    @patch('platform_mcp_server.validate_location')
    @patch('platform_mcp_server.subprocess.run')
    def test_verify_gke_cluster_success(self, mock_run, mock_validate_location, mock_get_project_id):
        mock_get_project_id.return_value = "test-project"
        mock_validate_location.return_value = ""
        
        mock_response = MagicMock()
        mock_response.stdout = json.dumps({"status": "RUNNING", "id": "1234567890"})
        mock_run.return_value = mock_response

        result_str = verify_gke_cluster("my-cluster", "us-central1", "test-project")
        result = json.loads(result_str)

        self.assertTrue(result["exists"])
        self.assertEqual(result["status"], "RUNNING")
        self.assertEqual(result["id"], "1234567890")
        
        mock_run.assert_called_once_with(
            [
                "gcloud", "container", "clusters", "describe", "my-cluster",
                "--location=us-central1",
                "--project=test-project",
                "--format=json(status, id)"
            ],
            capture_output=True, text=True, check=True
        )

    @patch('platform_mcp_server.get_project_id')
    @patch('platform_mcp_server.validate_location')
    @patch('platform_mcp_server.subprocess.run')
    def test_verify_gke_cluster_not_found(self, mock_run, mock_validate_location, mock_get_project_id):
        mock_get_project_id.return_value = "test-project"
        mock_validate_location.return_value = ""
        
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=1,
            cmd="gcloud ...",
            stderr="ERROR: (gcloud.container.clusters.describe) NotFound: Resource not found."
        )

        result_str = verify_gke_cluster("non-existent-cluster", "us-central1", "test-project")
        result = json.loads(result_str)

        self.assertFalse(result["exists"])

    @patch('platform_mcp_server.get_project_id')
    @patch('platform_mcp_server.validate_location')
    @patch('platform_mcp_server.subprocess.run')
    def test_verify_gke_cluster_general_failure(self, mock_run, mock_validate_location, mock_get_project_id):
        mock_get_project_id.return_value = "test-project"
        mock_validate_location.return_value = ""
        
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=1,
            cmd="gcloud ...",
            stderr="ERROR: (gcloud.container.clusters.describe) Required permission container.clusters.get is missing."
        )

        result = verify_gke_cluster("my-cluster", "us-central1", "test-project")

        self.assertTrue(result.startswith("ERROR:"))
        self.assertIn("Required permission container.clusters.get is missing.", result)

    @patch('platform_mcp_server.get_project_id')
    @patch('platform_mcp_server.validate_location')
    def test_verify_gke_cluster_invalid_location(self, mock_validate_location, mock_get_project_id):
        mock_get_project_id.return_value = "test-project"
        mock_validate_location.return_value = "ERROR: Invalid GKE location 'invalid-region' specified."

        result = verify_gke_cluster("my-cluster", "invalid-region", "test-project")

        self.assertEqual(result, "ERROR: Invalid GKE location 'invalid-region' specified.")

if __name__ == '__main__':
    unittest.main()
