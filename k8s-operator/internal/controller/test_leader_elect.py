import unittest
from datetime import datetime, timezone
import leader_elect

class TestLeaderElect(unittest.TestCase):
    def test_to_k8s_time(self):
        dt = datetime(2023, 10, 11, 12, 34, 56, 123456, tzinfo=timezone.utc)
        self.assertEqual(leader_elect.to_k8s_time(dt), "2023-10-11T12:34:56.123456Z")

    def test_from_k8s_time_microseconds(self):
        # 6 digits
        time_str = "2023-10-11T12:34:56.123456Z"
        dt = leader_elect.from_k8s_time(time_str)
        self.assertEqual(dt.year, 2023)
        self.assertEqual(dt.microsecond, 123456)
        
    def test_from_k8s_time_nanoseconds(self):
        # 9 digits
        time_str = "2023-10-11T12:34:56.123456789Z"
        dt = leader_elect.from_k8s_time(time_str)
        self.assertEqual(dt.year, 2023)
        self.assertEqual(dt.microsecond, 123456)

    def test_from_k8s_time_no_fractions(self):
        # 0 digits
        time_str = "2023-10-11T12:34:56Z"
        dt = leader_elect.from_k8s_time(time_str)
        self.assertEqual(dt.year, 2023)
        self.assertEqual(dt.microsecond, 0)
        
    def test_from_k8s_time_invalid(self):
        dt = leader_elect.from_k8s_time("invalid-time")
        self.assertIsInstance(dt, datetime)
        self.assertEqual(dt.tzinfo, timezone.utc)

import json
from unittest.mock import patch, MagicMock

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
    @patch("leader_elect.run_kubectl")
    @patch("leader_elect.time.sleep")
    def test_acquire_lease_when_no_lease_exists(self, mock_sleep, mock_kubectl, mock_popen):
        # kubectl get returns empty (no lease)
        # kubectl create succeeds
        mock_kubectl.side_effect = [
            None, # get lease
            "created", # create lease
            "labeled"  # label pod
        ]
        
        self.run_one_iteration(mock_sleep)
        
        # Verify it tried to create
        mock_kubectl.assert_any_call(["create", "-f", "-"], input_data=unittest.mock.ANY)
        # Verify it started process
        mock_popen.assert_called_once()

    @patch("leader_elect.subprocess.Popen")
    @patch("leader_elect.run_kubectl")
    @patch("leader_elect.time.sleep")
    def test_renew_lease_when_leader(self, mock_sleep, mock_kubectl, mock_popen):
        # Mock that we are already the leader
        mock_process = MagicMock()
        mock_process.poll.return_value = None
        leader_elect.process = mock_process
        
        lease_data = {
            "spec": {
                "holderIdentity": "pod-1",
                "renewTime": leader_elect.to_k8s_time(datetime.now(timezone.utc)),
                "leaseDurationSeconds": 15
            }
        }
        
        mock_kubectl.side_effect = [
            json.dumps(lease_data), # get lease
            "replaced" # replace lease
        ]
        
        self.run_one_iteration(mock_sleep)
        
        # Verify it renewed
        mock_kubectl.assert_any_call(["replace", "-f", "-"], input_data=unittest.mock.ANY)

    @patch("leader_elect.subprocess.Popen")
    @patch("leader_elect.run_kubectl")
    @patch("leader_elect.time.sleep")
    def test_clear_holder_on_renew_failure(self, mock_sleep, mock_kubectl, mock_popen):
        # Mock that we are the leader, but replace fails
        mock_process = MagicMock()
        mock_process.poll.return_value = None
        leader_elect.process = mock_process
        
        lease_data = {
            "spec": {
                "holderIdentity": "pod-1",
                "renewTime": leader_elect.to_k8s_time(datetime.now(timezone.utc)),
                "leaseDurationSeconds": 15
            }
        }
        
        mock_kubectl.side_effect = [
            json.dumps(lease_data), # get lease
            None, # replace lease FAILS (returns None)
            "unlabeled"   # remove label pod
        ]
        
        self.run_one_iteration(mock_sleep)
        
        # Verify process is terminated
        mock_process.terminate.assert_called_once()
        self.assertIsNone(leader_elect.process)

    @patch("leader_elect.subprocess.Popen")
    @patch("leader_elect.run_kubectl")
    @patch("leader_elect.time.sleep")
    def test_take_over_expired_lease(self, mock_sleep, mock_kubectl, mock_popen):
        # Mock that someone else was the leader but expired
        past_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
        lease_data = {
            "spec": {
                "holderIdentity": "pod-old",
                "renewTime": leader_elect.to_k8s_time(past_time),
                "leaseDurationSeconds": 15
            }
        }
        
        mock_kubectl.side_effect = [
            json.dumps(lease_data), # get lease
            "replaced", # replace lease (takeover)
            "labeled" # label pod
        ]
        
        self.run_one_iteration(mock_sleep)
        
        mock_kubectl.assert_any_call(["replace", "-f", "-"], input_data=unittest.mock.ANY)
        mock_popen.assert_called_once()

if __name__ == "__main__":
    unittest.main()
