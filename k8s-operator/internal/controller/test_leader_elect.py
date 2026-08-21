import os
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


class TestGatewayProfile(unittest.TestCase):
    """The gateway argv this wrapper supervises, at spec.harness.experimental.platformFrontDoor.

    At one replica the operator puts `hermes --profile platform gateway run` straight into
    the container args and this file never runs. Above one it runs INSTEAD of those args, so
    the profile has to arrive as an environment variable — and if it does not arrive at all,
    an HA install silently keeps serving chat from the Chat Agent while the CR says otherwise.

    `--profile` is a global flag that hermes_cli/main.py pre-parses out of argv before any
    import, so its position ahead of `gateway` is part of the contract, not formatting.
    HERMES_COMMAND is built at import time, hence the reload.
    """

    def _command(self, value):
        import importlib
        with patch.dict(os.environ, {} if value is None else {"HERMES_GATEWAY_PROFILE": value},
                        clear=False):
            if value is None:
                os.environ.pop("HERMES_GATEWAY_PROFILE", None)
            return importlib.reload(leader_elect).HERMES_COMMAND

    def tearDown(self):
        # Leave the module as the other tests in this file expect to find it.
        self._command(None)

    def test_no_profile_runs_the_default_gateway(self):
        self.assertEqual(self._command(None), ["hermes", "gateway", "run"])

    def test_an_empty_profile_is_not_a_profile(self):
        """An unset variable and one set to "" must mean the same thing.

        Kubernetes renders an absent value as the empty string rather than omitting the
        variable, so `hermes --profile "" gateway run` is what a half-wired manifest would
        produce — and Hermes reads that as a profile named "", homing the gateway at a
        directory nothing scaffolded.
        """
        self.assertEqual(self._command(""), ["hermes", "gateway", "run"])
        self.assertEqual(self._command("  "), ["hermes", "gateway", "run"])

    def test_the_profile_precedes_the_subcommand(self):
        self.assertEqual(
            self._command("platform"),
            ["hermes", "--profile", "platform", "gateway", "run"],
            "`--profile` is global: after `gateway` it is not the same command",
        )

    @patch("leader_elect.time.sleep")
    def test_the_supervised_process_gets_the_profile(self, mock_sleep):
        """The argv is not merely built, it is what Popen is handed."""
        import importlib
        with patch.dict(os.environ, {"HERMES_GATEWAY_PROFILE": "platform"}):
            module = importlib.reload(leader_elect)
        module.lease_name, module.namespace, module.pod_name = "l", "ns", "pod-1"
        module.process, module.is_shutting_down = None, False

        mock_coordination = MagicMock()
        mock_kubernetes.client.CoordinationV1Api.return_value = mock_coordination
        mock_kubernetes.client.CoreV1Api.return_value = MagicMock()
        lease = MagicMock()
        lease.spec.holder_identity = "pod-1"
        mock_coordination.read_namespaced_lease.return_value = lease

        with patch.object(module.subprocess, "Popen") as popen:
            mock_sleep.side_effect = Exception("StopLoop")
            with self.assertRaises(Exception):
                module.main()

        popen.assert_called_once_with(["hermes", "--profile", "platform", "gateway", "run"])


if __name__ == '__main__':
    unittest.main()
