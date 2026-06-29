import unittest
from unittest.mock import patch, MagicMock
import json
import subprocess
import sys
from pathlib import Path

# Add the directory containing platform_mcp_server.py to sys.path so it can be imported
sys.path.insert(0, str(Path(__file__).parent.absolute()))

from platform_mcp_server import audit_log_searcher


class TestAuditLogSearcher(unittest.TestCase):

    @patch('platform_mcp_server.get_project_id')
    @patch('platform_mcp_server.subprocess.run')
    def test_audit_log_searcher_success(self, mock_run, mock_get_project_id):
        mock_get_project_id.return_value = "test-project"
        
        mock_response = MagicMock()
        mock_response.stdout = '[{"protoPayload": {"methodName": "google.containerfree.v1.ClusterManager.DeleteCluster"}}]'
        mock_run.return_value = mock_response

        result = audit_log_searcher("test-project")

        self.assertIn("DeleteCluster", result)
        mock_run.assert_called_once_with(
            [
                "gcloud", "logging", "read",
                'resource.type="gke_cluster" AND protoPayload.methodName:delete AND "deployments/bootstrap"',
                "--project=test-project",
                "--limit=5",
                "--format=json"
            ],
            capture_output=True, text=True, check=True
        )

    @patch('platform_mcp_server.get_project_id')
    @patch('platform_mcp_server.subprocess.run')
    def test_audit_log_searcher_failure(self, mock_run, mock_get_project_id):
        mock_get_project_id.return_value = "test-project"
        
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=1,
            cmd="gcloud logging read ...",
            stderr="Permission denied on logs"
        )

        result = audit_log_searcher("test-project")

        self.assertTrue(result.startswith("ERROR:"))
        self.assertIn("Permission denied on logs", result)

if __name__ == '__main__':
    unittest.main()
