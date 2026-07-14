import json
import queue
import tempfile
import unittest
from pathlib import Path

from credential_proxy import CommandExecutor, Policy, SlackRelay


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.policy_path = Path(self.temp_dir.name) / "policy.json"
        self.policy_path.write_text(
            json.dumps(
                {
                    "blockedMessage": "Command blocked for security reasons.",
                    "rules": [
                        {
                            "id": "gcp.access-token-disclosure",
                            "pattern": r"\bgcloud\s+auth\s+print-access-token\b",
                        },
                        {
                            "id": "github.token-disclosure",
                            "pattern": r"\bgh\s+auth\s+token\b",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.policy = Policy.load(str(self.policy_path))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_blocks_configured_command(self):
        rule = self.policy.blocked_by("gcloud auth print-access-token")
        self.assertIsNotNone(rule)
        self.assertEqual("gcp.access-token-disclosure", rule.rule_id)

    def test_allows_generic_pipeline(self):
        self.assertIsNone(self.policy.blocked_by("printf test | tr a-z A-Z"))


class CommandExecutorTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def executor(self, timeout_seconds=5):
        return CommandExecutor(
            timeout_seconds=timeout_seconds,
            max_output_bytes=1024,
            state_dir=self.temp_dir.name,
        )

    def test_executes_exact_shell_pipeline(self):
        result = self.executor().execute("printf hello | tr a-z A-Z")
        self.assertEqual(0, result.exit_code)
        self.assertEqual("HELLO", result.stdout)

    def test_preserves_workspace_between_commands(self):
        self.executor().execute("mkdir repo && printf main > repo/branch")
        result = self.executor().execute("cat repo/branch")
        self.assertEqual("main", result.stdout)

    def test_forwards_stdin_to_command(self):
        result = self.executor().execute("tr a-z A-Z", stdin="hello")
        self.assertEqual("HELLO", result.stdout)

    def test_timeout_kills_command(self):
        result = self.executor(timeout_seconds=1).execute("sleep 10")
        self.assertTrue(result.timed_out)
        self.assertEqual(124, result.exit_code)

    def test_treats_cli_names_as_generic_shell_commands(self):
        result = self.executor().execute(
            "printf '#!/bin/sh\\nprintf generic-kubectl' > kubectl && "
            "chmod +x kubectl && ./kubectl get pods"
        )
        self.assertEqual(0, result.exit_code)
        self.assertEqual("generic-kubectl", result.stdout)

    def test_provides_private_trusted_shell_profile(self):
        result = self.executor().execute(
            "printf '%s\\n' \"$HOME\" \"$KUBECONFIG\" "
            "\"$CLOUDSDK_CONFIG\" \"$GH_CONFIG_DIR\""
        )
        paths = result.stdout.splitlines()
        self.assertEqual(str(Path(self.temp_dir.name) / "home"), paths[0])
        self.assertTrue(paths[1].endswith("/home/.kube/config"))
        self.assertTrue(paths[2].endswith("/home/.config/gcloud"))
        self.assertTrue(paths[3].endswith("/home/.config/gh"))

    def test_bootstrap_prepares_profile_for_later_commands(self):
        executor = self.executor()
        executor.bootstrap("printf ready > \"$HOME/bootstrap-state\"")
        result = executor.execute("cat \"$HOME/bootstrap-state\"")
        self.assertEqual(0, result.exit_code)
        self.assertEqual("ready", result.stdout)

    def test_bootstrap_failure_does_not_return_command_output(self):
        with self.assertRaisesRegex(RuntimeError, "exit code 9") as raised:
            self.executor().bootstrap("printf secret >&2; exit 9")
        self.assertNotIn("secret", str(raised.exception))


class SlackRelayTest(unittest.TestCase):
    class FakeClient:
        token = "xoxb-not-returned"

        def chat_postMessage(self, **kwargs):
            return {"ok": True, "message": kwargs}

        def auth_revoke(self, **kwargs):
            return {"ok": True, "arguments": kwargs}

    def relay(self):
        relay = SlackRelay.__new__(SlackRelay)
        relay.primary_client = self.FakeClient()
        relay.clients = {"T123": relay.primary_client}
        relay.workspaces = [{"teamId": "T123", "botUserId": "U123", "botName": "agent"}]
        relay._events = queue.Queue()
        relay._receipts = {}
        import threading

        relay._lock = threading.Lock()
        return relay

    def test_forwards_supported_web_api_method_without_token(self):
        result = self.relay().api_call(
            "T123", "chat_postMessage", {"channel": "C123", "text": "hello"}
        )
        self.assertTrue(result["ok"])
        self.assertNotIn("token", json.dumps(result))

    def test_blocks_credential_management_api_method(self):
        with self.assertRaisesRegex(ValueError, "not available"):
            self.relay().api_call("T123", "auth_revoke", {})

    def test_nack_requeues_event(self):
        relay = self.relay()
        relay._events.put({"type": "events_api", "payload": {"event": {}}})
        event = relay.pull(timeout_seconds=1)
        self.assertTrue(relay.settle(event["receipt"], acknowledge=False))
        retried = relay.pull(timeout_seconds=1)
        self.assertEqual("events_api", retried["type"])


if __name__ == "__main__":
    unittest.main()
