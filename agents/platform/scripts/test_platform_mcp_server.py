import unittest
from unittest.mock import patch, MagicMock
import json
import subprocess
import sys
from pathlib import Path

# Add the directory containing platform_mcp_server.py to sys.path so it can be imported
sys.path.insert(0, str(Path(__file__).parent.absolute()))

from platform_mcp_server import verify_gke_cluster, list_cc_healthchecks, get_cc_operator_status, list_cc_pods, switch_kube_context, get_cc_pod_diagnostics

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


class TestCcDiagnosticTools(unittest.TestCase):

    @patch('platform_mcp_server.switch_kube_context')
    @patch('platform_mcp_server.subprocess.run')
    def test_list_cc_healthchecks_success(self, mock_run, mock_switch):
        mock_response = MagicMock()
        mock_response.stdout = '{"items": []}'
        mock_run.return_value = mock_response
        mock_switch.return_value = ("", {"KUBECONFIG": "/tmp/test.yaml"})

        result = list_cc_healthchecks("proj", "clust", "loc")

        self.assertEqual(result, '{"items": []}')
        mock_switch.assert_called_once_with("proj", "clust", "loc")
        mock_run.assert_called_once_with(
            [
                "kubectl", "get", "healthchecks.healthcheck.config.gke.io",
                "-n", "krmapihosting-system",
                "-o", "json"
            ],
            capture_output=True, text=True, check=True, timeout=30, env={"KUBECONFIG": "/tmp/test.yaml"}
        )

    @patch('platform_mcp_server.switch_kube_context')
    @patch('platform_mcp_server.subprocess.run')
    def test_get_cc_operator_status_success(self, mock_run, mock_switch):
        mock_response = MagicMock()
        mock_response.stdout = '{"status": {"healthy": True}}'
        mock_run.return_value = mock_response
        mock_switch.return_value = ("", {"KUBECONFIG": "/tmp/test.yaml"})

        result = get_cc_operator_status("proj", "clust", "loc")

        self.assertEqual(result, '{"status": {"healthy": True}}')
        mock_switch.assert_called_once_with("proj", "clust", "loc")
        mock_run.assert_called_once_with(
            [
                "kubectl", "get", "configconnectors.core.cnrm.cloud.google.com",
                "configconnector",
                "-o", "json"
            ],
            capture_output=True, text=True, check=True, timeout=30, env={"KUBECONFIG": "/tmp/test.yaml"}
        )

    @patch('platform_mcp_server.switch_kube_context')
    @patch('platform_mcp_server.subprocess.run')
    def test_list_cc_pods_success(self, mock_run, mock_switch):
        mock_response = MagicMock()
        mock_response.stdout = json.dumps({
            "items": [
                {
                    "metadata": {"name": "bootstrap-pod"},
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [
                            {"restartCount": 1, "state": {"running": {}}}
                        ]
                    }
                },
                {
                    "metadata": {"name": "git-sync-pod"},
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [
                            {"restartCount": 0, "state": {"running": {}}}
                        ]
                    }
                }
            ]
        })
        mock_run.return_value = mock_response
        mock_switch.return_value = ("", {"KUBECONFIG": "/tmp/test.yaml"})

        result_str = list_cc_pods("proj", "clust", "loc")
        result = json.loads(result_str)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["name"], "bootstrap-pod")
        self.assertEqual(result[0]["status"], "Running")
        self.assertEqual(result[0]["restarts"], 1)
        self.assertEqual(result[1]["name"], "git-sync-pod")
        mock_switch.assert_called_once_with("proj", "clust", "loc")

    @patch('platform_mcp_server.switch_kube_context')
    @patch('platform_mcp_server.subprocess.run')
    def test_list_cc_pods_null_status_fields(self, mock_run, mock_switch):
        mock_response = MagicMock()
        mock_response.stdout = json.dumps({
            "items": [
                {
                    "metadata": {"name": "pending-pod"},
                    "status": {
                        "phase": "Pending",
                        "containerStatuses": None
                    }
                }
            ]
        })
        mock_run.return_value = mock_response
        mock_switch.return_value = ("", {"KUBECONFIG": "/tmp/test.yaml"})

        result_str = list_cc_pods("proj", "clust", "loc")
        result = json.loads(result_str)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "pending-pod")
        self.assertEqual(result[0]["status"], "Pending")
        self.assertEqual(result[0]["restarts"], 0)

    @patch('platform_mcp_server.switch_kube_context')
    @patch('platform_mcp_server.subprocess.run')
    def test_list_cc_pods_init_and_terminated(self, mock_run, mock_switch):
        mock_response = MagicMock()
        mock_response.stdout = json.dumps({
            "items": [
                {
                    "metadata": {"name": "init-pod"},
                    "status": {
                        "phase": "Pending",
                        "initContainerStatuses": [
                            {"restartCount": 2, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}
                        ]
                    }
                },
                {
                    "metadata": {"name": "oom-pod"},
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [
                            {"restartCount": 1, "state": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}
                        ]
                    }
                }
            ]
        })
        mock_run.return_value = mock_response
        mock_switch.return_value = ("", {"KUBECONFIG": "/tmp/test.yaml"})

        result_str = list_cc_pods("proj", "clust", "loc")
        result = json.loads(result_str)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["name"], "init-pod")
        self.assertEqual(result[0]["status"], "CrashLoopBackOff")
        self.assertEqual(result[0]["restarts"], 2)
        self.assertEqual(result[1]["name"], "oom-pod")
        self.assertEqual(result[1]["status"], "OOMKilled")
        self.assertEqual(result[1]["restarts"], 1)

    @patch('platform_mcp_server.switch_kube_context')
    @patch('platform_mcp_server.subprocess.run')
    def test_list_cc_healthchecks_timeout(self, mock_run, mock_switch):
        mock_switch.return_value = ("", {"KUBECONFIG": "/tmp/test.yaml"})
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="kubectl ...", timeout=30)
        result = list_cc_healthchecks("proj", "clust", "loc")
        self.assertIn("Timed out querying Config Controller health checks", result)

    @patch('platform_mcp_server.switch_kube_context')
    @patch('platform_mcp_server.subprocess.run')
    def test_get_cc_operator_status_timeout(self, mock_run, mock_switch):
        mock_switch.return_value = ("", {"KUBECONFIG": "/tmp/test.yaml"})
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="kubectl ...", timeout=30)
        result = get_cc_operator_status("proj", "clust", "loc")
        self.assertIn("Timed out retrieving Config Controller operator status", result)

    @patch('platform_mcp_server.switch_kube_context')
    @patch('platform_mcp_server.subprocess.run')
    def test_list_cc_pods_timeout(self, mock_run, mock_switch):
        mock_switch.return_value = ("", {"KUBECONFIG": "/tmp/test.yaml"})
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="kubectl ...", timeout=30)
        result = list_cc_pods("proj", "clust", "loc")
        self.assertIn("Timed out listing Config Controller pods", result)

    @patch('platform_mcp_server.switch_kube_context')
    @patch('platform_mcp_server.subprocess.run')
    def test_list_cc_pods_error(self, mock_run, mock_switch):
        mock_run.side_effect = subprocess.CalledProcessError(1, "kubectl", stderr="Error listing pods")
        mock_switch.return_value = ("", {"KUBECONFIG": "/tmp/test.yaml"})

        result = list_cc_pods("proj", "clust", "loc")

        self.assertTrue(result.startswith("ERROR:"))
        self.assertIn("Error listing pods", result)
        mock_switch.assert_called_once_with("proj", "clust", "loc")


class TestSwitchKubeContext(unittest.TestCase):

    @patch('platform_mcp_server.subprocess.run')
    def test_switch_kube_context_all_empty_noop(self, mock_run):
        err, env = switch_kube_context("", "", "")
        self.assertEqual(err, "")
        self.assertIsNone(env)
        mock_run.assert_not_called()

    @patch('platform_mcp_server.subprocess.run')
    def test_switch_kube_context_partial_arguments_error(self, mock_run):
        err1, env1 = switch_kube_context("", "my-cluster", "us-central1")
        self.assertTrue(err1.startswith("ERROR:"))
        self.assertIn("partially specified", err1)
        self.assertIsNone(env1)
        mock_run.assert_not_called()

        err2, env2 = switch_kube_context("my-project", "", "us-central1")
        self.assertTrue(err2.startswith("ERROR:"))
        self.assertIn("partially specified", err2)
        self.assertIsNone(env2)
        mock_run.assert_not_called()

        err3, env3 = switch_kube_context("my-project", "my-cluster", "")
        self.assertTrue(err3.startswith("ERROR:"))
        self.assertIn("partially specified", err3)
        self.assertIsNone(env3)
        mock_run.assert_not_called()

    @patch('platform_mcp_server.subprocess.run')
    def test_switch_kube_context_success(self, mock_run):
        err, env = switch_kube_context("my-project", "my-cluster", "us-central1")

        self.assertEqual(err, "")
        self.assertIsNotNone(env)
        self.assertEqual(env["KUBECONFIG"], "/tmp/kubeconfig_my-project_my-cluster_us-central1.yaml")
        mock_run.assert_called_once_with(
            [
                "gcloud", "container", "clusters", "get-credentials", "my-cluster",
                "--location=us-central1",
                "--project=my-project",
                "--kubeconfig=/tmp/kubeconfig_my-project_my-cluster_us-central1.yaml"
            ],
            capture_output=True, text=True, check=True, timeout=30, env=env
        )

    @patch('platform_mcp_server.subprocess.run')
    def test_switch_kube_context_error(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(1, "gcloud", stderr="Not authorized")

        err, env = switch_kube_context("my-project", "my-cluster", "us-central1")

        self.assertTrue(err.startswith("ERROR:"))
        self.assertIn("Not authorized", err)
        self.assertIsNone(env)

    @patch('platform_mcp_server.subprocess.run')
    def test_switch_kube_context_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gcloud ...", timeout=30)

        err, env = switch_kube_context("my-project", "my-cluster", "us-central1")

        self.assertTrue(err.startswith("ERROR:"))
        self.assertIn("Timed out switching kube context", err)
        self.assertIsNone(env)


class TestContextSwitchFailurePropagation(unittest.TestCase):

    @patch('platform_mcp_server.switch_kube_context')
    @patch('platform_mcp_server.subprocess.run')
    def test_context_switch_error_returned_by_tool(self, mock_run, mock_switch):
        mock_switch.return_value = (
            "ERROR: Failed to switch kube context to cluster 'bad-cluster'.\nExit Code: 1\nStderr: Not authorized",
            None
        )

        result = list_cc_healthchecks("proj", "bad-cluster", "loc")

        self.assertIn("Failed to switch kube context", result)
        mock_run.assert_not_called()


class TestCcPodDiagnostics(unittest.TestCase):

    @patch('platform_mcp_server.switch_kube_context')
    @patch('platform_mcp_server.subprocess.run')
    def test_get_cc_pod_diagnostics_success(self, mock_run, mock_switch):
        mock_switch.return_value = ("", {"KUBECONFIG": "/tmp/test.yaml"})
        mock_response_get = MagicMock()
        mock_response_get.stdout = '{"status": {"phase": "Running"}}'
        mock_response_desc = MagicMock()
        mock_response_desc.stdout = 'Name: bootstrap-pod'
        mock_response_logs = MagicMock()
        mock_response_logs.stdout = 'Starting bootstrap...'
        mock_response_prev_logs = MagicMock()
        mock_response_prev_logs.stdout = 'Previous crash trace...'

        mock_run.side_effect = [mock_response_get, mock_response_desc, mock_response_logs, mock_response_prev_logs]

        result = get_cc_pod_diagnostics("bootstrap-pod-xyz", "proj", "clust", "loc")

        self.assertIn("=== POD STATUS (JSON) ===", result)
        self.assertIn("=== POD DESCRIBE ===", result)
        self.assertIn("=== POD LOGS (CURRENT TAIL=100) ===", result)
        self.assertIn("=== POD LOGS (PREVIOUS TAIL=100) ===", result)
        mock_switch.assert_called_once_with("proj", "clust", "loc")
        self.assertEqual(mock_run.call_count, 4)

    @patch('platform_mcp_server.switch_kube_context')
    @patch('platform_mcp_server.subprocess.run')
    def test_get_cc_pod_diagnostics_broadened_pod(self, mock_run, mock_switch):
        mock_switch.return_value = ("", {"KUBECONFIG": "/tmp/test.yaml"})
        mock_response_get = MagicMock()
        mock_response_get.stdout = '{"status": {"phase": "Running"}}'
        mock_response_desc = MagicMock()
        mock_response_desc.stdout = 'Name: git-sync-pod'
        mock_response_logs = MagicMock()
        mock_response_logs.stdout = 'Syncing git repo...'
        mock_response_prev_logs = MagicMock()
        mock_response_prev_logs.stdout = 'Previous git crash...'

        mock_run.side_effect = [mock_response_get, mock_response_desc, mock_response_logs, mock_response_prev_logs]

        result = get_cc_pod_diagnostics("git-sync-pod-123", "proj", "clust", "loc")

        self.assertIn("=== POD STATUS (JSON) ===", result)
        self.assertIn("=== POD DESCRIBE ===", result)
        self.assertIn("=== POD LOGS (CURRENT TAIL=100) ===", result)
        self.assertIn("=== POD LOGS (PREVIOUS TAIL=100) ===", result)
        mock_switch.assert_called_once_with("proj", "clust", "loc")
        self.assertEqual(mock_run.call_count, 4)

    def test_get_cc_pod_diagnostics_invalid_format(self):
        result = get_cc_pod_diagnostics("invalid_pod$name")
        self.assertIn("Invalid pod name format", result)

    @patch('platform_mcp_server.switch_kube_context')
    @patch('platform_mcp_server.subprocess.run')
    def test_get_cc_pod_diagnostics_timeout(self, mock_run, mock_switch):
        mock_switch.return_value = ("", {"KUBECONFIG": "/tmp/test.yaml"})
        mock_response_get = MagicMock()
        mock_response_get.stdout = '{"status": {"phase": "Running"}}'
        mock_run.side_effect = [
            mock_response_get,
            subprocess.TimeoutExpired(cmd="kubectl describe ...", timeout=30),
            subprocess.TimeoutExpired(cmd="kubectl logs ...", timeout=30),
            subprocess.TimeoutExpired(cmd="kubectl logs --previous ...", timeout=30)
        ]

        result = get_cc_pod_diagnostics("bootstrap-pod-xyz", "proj", "clust", "loc")

        self.assertIn("=== POD STATUS (JSON) ===", result)
        self.assertIn("=== POD DESCRIBE TIMEOUT ===", result)
        self.assertIn("=== POD LOGS (CURRENT TAIL=100) TIMEOUT ===", result)
        self.assertIn("=== POD LOGS (PREVIOUS TAIL=100) TIMEOUT ===", result)
        self.assertEqual(mock_run.call_count, 4)


if __name__ == '__main__':
    unittest.main()
