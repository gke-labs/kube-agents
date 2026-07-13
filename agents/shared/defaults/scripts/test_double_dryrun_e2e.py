import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))
from double_dryrun_hook import DoubleDryRunHook
from session_manager import SessionManager


class TestDoubleDryRunE2E(unittest.TestCase):
    """End-to-End Test Suite for Permission Intersection & Double Dry-Run Pre-Flight."""

    def setUp(self):
        self.hook = DoubleDryRunHook()

    @patch.object(DoubleDryRunHook, "_exec")
    def test_e2e_positive_allow_sre_user(self, mock_exec):
        """
        POSITIVE TEST: User (Alice - SRE) has RBAC + Agent SA has RBAC.
        Result: Pre-flight passes both dry-runs and command is ALLOWED.
        """
        # Mock Check 1 (User dry-run) -> Success
        res1 = MagicMock()
        res1.returncode = 0
        res1.stdout = "deployment.apps/demo-web deleted (server dry run)"

        # Mock Check 2 (Agent SA dry-run) -> Success
        res2 = MagicMock()
        res2.returncode = 0
        res2.stdout = "deployment.apps/demo-web deleted (server dry run)"

        mock_exec.side_effect = [res1, res2]

        context = {
            "user_email": "alice@example.com",
            "user_groups": ["sre-team@example.com"],
            "session_id": "session-sre-101",
        }
        args = {"command": "kubectl delete deployment demo-web -n prod"}

        # Execute Hook
        result_args = self.hook.process_tool_call("kubectl", args, context)

        # Assertions
        self.assertEqual(result_args["command"], "kubectl delete deployment demo-web -n prod")
        self.assertEqual(mock_exec.call_count, 2)

        # Verify Check 1 was called with User Impersonation flags
        user_dryrun_cmd = mock_exec.call_args_list[0][0][0]
        self.assertIn("--as=alice@example.com", user_dryrun_cmd)
        self.assertIn("--as-group=sre-team@example.com", user_dryrun_cmd)
        self.assertIn("--dry-run=server", user_dryrun_cmd)

        # Verify Check 2 was called without User Impersonation flags (Agent SA identity)
        agent_dryrun_cmd = mock_exec.call_args_list[1][0][0]
        self.assertNotIn("--as=", agent_dryrun_cmd)
        self.assertIn("--dry-run=server", agent_dryrun_cmd)

    @patch.object(DoubleDryRunHook, "_exec")
    def test_e2e_negative_reject_dev_user(self, mock_exec):
        """
        NEGATIVE TEST: User (Bob - Dev) lacks RBAC (cannot delete deployments).
        Result: Pre-flight fails Check 1 and command is REJECTED with [USER_RBAC_DENIED].
        """
        # Mock Check 1 (User dry-run) -> Failure (403 Forbidden)
        res1 = MagicMock()
        res1.returncode = 1
        res1.stderr = 'Error from server (Forbidden): deployments.apps "demo-web" is forbidden: User "bob@example.com" cannot delete resource "deployments" in API group "apps" in namespace "prod"'

        mock_exec.return_value = res1

        context = {
            "user_email": "bob@example.com",
            "user_groups": ["dev-team@example.com"],
            "session_id": "session-dev-202",
        }
        args = {"command": "kubectl delete deployment demo-web -n prod"}

        # Execute Hook and assert PermissionError
        with self.assertRaises(PermissionError) as cm:
            self.hook.process_tool_call("kubectl", args, context)

        err_msg = str(cm.exception)
        self.assertIn("[USER_RBAC_DENIED]", err_msg)
        self.assertIn("bob@example.com", err_msg)

        # Verify execution aborted after Check 1 without running Check 2
        self.assertEqual(mock_exec.call_count, 1)

    @patch.object(DoubleDryRunHook, "_exec")
    def test_e2e_negative_reject_readonly_agent(self, mock_exec):
        """
        NEGATIVE TEST: User (Alice - SRE) has cluster-admin RBAC, but Agent SA is Read-Only.
        Result: Pre-flight passes Check 1, fails Check 2, and command is REJECTED with [AGENT_SA_DENIED].
        """
        # Mock Check 1 (User dry-run) -> Success (Alice is admin)
        res1 = MagicMock()
        res1.returncode = 0
        res1.stdout = "deployment.apps/demo-web deleted (server dry run)"

        # Mock Check 2 (Agent SA dry-run) -> Failure (Agent is Read-Only)
        res2 = MagicMock()
        res2.returncode = 1
        res2.stderr = 'Error from server (Forbidden): deployments.apps "demo-web" is forbidden: User "system:serviceaccount:kubeagents-system:platform-agent" cannot delete resource "deployments"'

        mock_exec.side_effect = [res1, res2]

        context = {
            "user_email": "alice@example.com",
            "user_groups": ["sre-team@example.com"],
            "session_id": "session-sre-303",
        }
        args = {"command": "kubectl delete deployment demo-web -n prod"}

        # Execute Hook and assert PermissionError
        with self.assertRaises(PermissionError) as cm:
            self.hook.process_tool_call("kubectl", args, context)

        err_msg = str(cm.exception)
        self.assertIn("[AGENT_SA_DENIED]", err_msg)

        # Verify Check 1 and Check 2 were both run
        self.assertEqual(mock_exec.call_count, 2)


if __name__ == "__main__":
    unittest.main()
