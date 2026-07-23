import unittest
import sys
from unittest.mock import MagicMock, patch

# Mock the kubernetes module BEFORE importing leader_elect
mock_kubernetes = MagicMock()
sys.modules['kubernetes'] = mock_kubernetes
sys.modules['kubernetes.client'] = mock_kubernetes.client
sys.modules['kubernetes.client.rest'] = mock_kubernetes.client.rest
sys.modules['kubernetes.config'] = mock_kubernetes.config

# Now we can import leader_elect safely
import leader_elect
from datetime import datetime, timezone, timedelta

class TestLeaderElectLogic(unittest.TestCase):
    def setUp(self):
        leader_elect.lease_name = "test-lease"
        leader_elect.namespace = "test-ns"
        leader_elect.pod_name = "pod-1"
        leader_elect.process = None
        leader_elect.is_shutting_down = False
        
    def tearDown(self):
        if leader_elect.process:
            leader_elect.process = None

    def run_one_iteration(self, mock_sleep):
        # mock sleep to stop the loop
        mock_sleep.side_effect = Exception("StopLoop")
        try:
            leader_elect.main()
        except Exception as e:
            if str(e) != "StopLoop":
                raise e

    @patch("leader_elect.subprocess.Popen")
    @patch("leader_elect.time.sleep")
    def test_acquire_lease_when_no_lease_exists(self, mock_sleep, mock_popen):
        # Set up mocks
        mock_coordination = MagicMock()
        mock_v1 = MagicMock()
        mock_kubernetes.client.CoordinationV1Api.return_value = mock_coordination
        mock_kubernetes.client.CoreV1Api.return_value = mock_v1
        
        # Make read_namespaced_lease raise a 404 ApiException
        mock_api_exception = Exception("Not Found")
        mock_api_exception.status = 404
        mock_kubernetes.client.rest.ApiException = type('ApiException', (Exception,), {})
        
        # Override the mock exception to actually behave like ApiException
        class MockApiException(Exception):
            def __init__(self, status):
                self.status = status
        
        leader_elect.ApiException = MockApiException
        
        mock_coordination.read_namespaced_lease.side_effect = MockApiException(404)
        
        self.run_one_iteration(mock_sleep)
        
        # Verify it tried to create the lease
        mock_coordination.create_namespaced_lease.assert_called_once()
        # Verify it started the process
        mock_popen.assert_called_once()
        # Verify it labelled the pod
        mock_v1.patch_namespaced_pod.assert_called_once()

    @patch("leader_elect.subprocess.Popen")
    @patch("leader_elect.time.sleep")
    def test_renew_lease_when_leader(self, mock_sleep, mock_popen):
        # Mock that we are already the leader
        mock_process = MagicMock()
        mock_process.poll.return_value = None
        leader_elect.process = mock_process
        
        mock_coordination = MagicMock()
        mock_kubernetes.client.CoordinationV1Api.return_value = mock_coordination
        
        mock_lease = MagicMock()
        mock_lease.spec.holder_identity = "pod-1"
        mock_coordination.read_namespaced_lease.return_value = mock_lease
        
        self.run_one_iteration(mock_sleep)
        
        # Verify it updated the lease
        mock_coordination.replace_namespaced_lease.assert_called_once()
        # Ensure it didn't try to start a new process
        mock_popen.assert_not_called()

    @patch("leader_elect.time.sleep")
    def test_do_nothing_when_someone_else_is_leader(self, mock_sleep):
        mock_coordination = MagicMock()
        mock_kubernetes.client.CoordinationV1Api.return_value = mock_coordination
        
        mock_lease = MagicMock()
        mock_lease.spec.holder_identity = "pod-2"
        # Not expired
        mock_lease.spec.renew_time = datetime.now(timezone.utc)
        mock_lease.spec.lease_duration_seconds = 15
        
        mock_coordination.read_namespaced_lease.return_value = mock_lease
        
        self.run_one_iteration(mock_sleep)
        
        # Verify it didn't try to acquire or replace the lease
        mock_coordination.replace_namespaced_lease.assert_not_called()

    @patch("leader_elect.subprocess.Popen")
    @patch("leader_elect.time.sleep")
    def test_take_over_expired_lease(self, mock_sleep, mock_popen):
        mock_coordination = MagicMock()
        mock_v1 = MagicMock()
        mock_kubernetes.client.CoordinationV1Api.return_value = mock_coordination
        mock_kubernetes.client.CoreV1Api.return_value = mock_v1
        
        mock_lease = MagicMock()
        mock_lease.spec.holder_identity = "pod-2"
        # Expired: renewed 20 seconds ago, duration is 15
        mock_lease.spec.renew_time = datetime.now(timezone.utc) - timedelta(seconds=20)
        mock_lease.spec.lease_duration_seconds = 15
        
        mock_coordination.read_namespaced_lease.return_value = mock_lease
        
        self.run_one_iteration(mock_sleep)
        
        # Verify it tried to acquire the lease
        mock_coordination.replace_namespaced_lease.assert_called_once()
        args, kwargs = mock_coordination.replace_namespaced_lease.call_args
        self.assertEqual(kwargs['body'].spec.holder_identity, "pod-1")
        
        # Verify it started the process
        mock_popen.assert_called_once()

if __name__ == '__main__':
    unittest.main()
