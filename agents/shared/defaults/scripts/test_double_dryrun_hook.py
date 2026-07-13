import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))
from double_dryrun_hook import DoubleDryRunHook


class TestDoubleDryRunHook(unittest.TestCase):
    def setUp(self):
        self.hook = DoubleDryRunHook()

    def test_non_kubectl_command_ignored(self):
        args = {"command": "ls -la"}
        context = {"user_email": "alice@example.com", "user_groups": ["dev-team"]}
        res = self.hook.process_tool_call("bash_command", args, context)
        self.assertEqual(res["command"], "ls -la")

    def test_readonly_command_skips_dryrun(self):
        args = {"command": "kubectl get pods -n prod"}
        context = {"user_email": "alice@example.com", "user_groups": ["dev-team"]}
        res = self.hook.process_tool_call("kubectl", args, context)
        self.assertEqual(res["command"], "kubectl get pods -n prod")

    def test_fail_closed_on_missing_user_email(self):
        args = {"command": "kubectl delete pod x -n prod"}
        context = {"user_email": "", "user_groups": ["sre-team@example.com"]}
        with self.assertRaises(PermissionError) as cm:
            self.hook.process_tool_call("kubectl", args, context)
        self.assertIn("SECURITY_DENIED", str(cm.exception))

    @patch.object(DoubleDryRunHook, "_exec")
    def test_check1_user_rbac_denied(self, mock_exec):
        mock_res1 = MagicMock()
        mock_res1.returncode = 1
        mock_res1.stderr = 'User "alice@example.com" cannot delete pods in prod'
        mock_exec.return_value = mock_res1

        args = {"command": "kubectl delete pod failing-pod -n prod"}
        context = {"user_email": "alice@example.com", "user_groups": ["dev-team@example.com"]}

        with self.assertRaises(PermissionError) as cm:
            self.hook.process_tool_call("kubectl", args, context)

        self.assertIn("USER_RBAC_DENIED", str(cm.exception))
        self.assertEqual(mock_exec.call_count, 1)

    @patch.object(DoubleDryRunHook, "_exec")
    def test_check2_agent_sa_denied(self, mock_exec):
        mock_res1 = MagicMock()
        mock_res1.returncode = 0
        mock_res1.stdout = "pod/failing-pod deleted (server dry run)"

        mock_res2 = MagicMock()
        mock_res2.returncode = 1
        mock_res2.stderr = 'User "system:serviceaccount:..." cannot delete pods in prod'

        mock_exec.side_effect = [mock_res1, mock_res2]

        args = {"command": "kubectl delete pod failing-pod -n prod"}
        context = {"user_email": "alice@example.com", "user_groups": ["dev-team@example.com"]}

        with self.assertRaises(PermissionError) as cm:
            self.hook.process_tool_call("kubectl", args, context)

        self.assertIn("AGENT_SA_DENIED", str(cm.exception))
        self.assertEqual(mock_exec.call_count, 2)

    @patch.object(DoubleDryRunHook, "_exec")
    def test_both_checks_pass(self, mock_exec):
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "pod/failing-pod deleted (server dry run)"
        mock_exec.return_value = mock_res

        args = {"command": "kubectl delete pod failing-pod -n prod"}
        context = {"user_email": "alice@example.com", "user_groups": ["sre-team@example.com"]}

    @patch("double_dryrun_hook.CloudIdentityResolver.get_user_groups")
    @patch.object(DoubleDryRunHook, "_exec")
    def test_expired_groups_re_queries_cloud_identity(self, mock_exec, mock_get_groups):
        mock_get_groups.return_value = ["new-sre-team@example.com"]
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_exec.return_value = mock_res

        args = {"command": "kubectl delete pod failing-pod -n prod"}
        context = {
            "user_email": "alice@example.com",
            "user_groups": ["old-dev-team@example.com"],
            "user_groups_expires_at": "2020-01-01T00:00:00+00:00",  # Expired
        }

        res = self.hook.process_tool_call("kubectl", args, context)
        self.assertEqual(res["command"], "kubectl delete pod failing-pod -n prod")
        self.assertTrue(mock_get_groups.called)
        self.assertTrue(mock_get_groups.call_args[1].get("force_refresh", False))


if __name__ == "__main__":
    unittest.main()
