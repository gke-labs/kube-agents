from __future__ import annotations

import json
import unittest
from collections.abc import Callable

from admin_console.agent_chat import (
    ChatCommandResult,
    AgentChatError,
    AgentChatProvider,
)
from admin_console.project_config import DeploymentTarget


class ChatRunner:
    def __init__(self, response: dict | None = None) -> None:
        self.response = response or {
            "run_id": "run_0123456789abcdef0123456789abcdef",
            "session_id": "portal_0123456789abcdef0123456789abcdef",
            "status": "completed",
            "output": "Game deployed.",
            "events": [{"event": "run.completed"}],
        }
        self.calls: list[tuple[list[str], str]] = []

    def run(
        self,
        arguments: list[str],
        *,
        input_text: str,
        timeout: int = 620,
        line_callback: Callable[[str], None] | None = None,
    ) -> ChatCommandResult:
        self.calls.append((arguments, input_text))
        if "get" in arguments and "pods" in arguments:
            return ChatCommandResult(
                0,
                json.dumps(
                    {"items": [{"metadata": {"name": "test-agent-01-gateway-1"}}]}
                ),
            )
        output = json.dumps(self.response)
        if line_callback is not None:
            line_callback(output)
        return ChatCommandResult(0, output)


class AgentChatProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = ChatRunner()
        self.provider = AgentChatProvider(
            DeploymentTarget("test-project-01", "test-cluster-01", "us-east4"),
            runner=self.runner,
        )

    def test_run_uses_fixed_in_pod_client_and_stdin_payload(self):
        result = self.provider.run(
            "test-agent-01",
            prompt="Deploy the game",
            session_id="portal_0123456789abcdef0123456789abcdef",
            history=({"role": "assistant", "content": "Ready"},),
            user_email="admin@example.com",
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.output, "Game deployed.")
        arguments, input_text = self.runner.calls[-1]
        self.assertIn("exec", arguments)
        self.assertIn("-i", arguments)
        self.assertNotIn("sh", arguments)
        self.assertNotIn("Deploy the game", arguments)
        embedded_client = arguments[-1]
        self.assertIn('os.environ["API_SERVER_KEY"]', embedded_client)
        self.assertNotIn("cluster-internal-trusted", embedded_client)
        payload = json.loads(input_text)
        self.assertEqual(payload["prompt"], "Deploy the game")
        self.assertEqual(payload["profile"], "default")
        self.assertEqual(payload["user_email"], "admin@example.com")

    def test_rejects_invalid_portal_identity_before_kubectl(self):
        with self.assertRaises(ValueError):
            self.provider.run(
                "test-agent-01",
                prompt="hello",
                session_id="portal_0123456789abcdef0123456789abcdef",
                user_email="invalid identity",
            )
        self.assertEqual(self.runner.calls, [])

    def test_approval_is_limited_to_once_or_deny(self):
        with self.assertRaises(ValueError):
            self.provider.resolve_approval(
                "test-agent-01",
                run_id="run_0123456789abcdef0123456789abcdef",
                choice="always",
            )

        self.provider.resolve_approval(
            "test-agent-01",
            run_id="run_0123456789abcdef0123456789abcdef",
            choice="once",
        )
        payload = json.loads(self.runner.calls[-1][1])
        self.assertEqual(payload["choice"], "once")

    def test_run_stream_reports_root_id_before_terminal_result(self):
        updates = []
        self.runner.response = {
            "checkpoint": True,
            "run_id": "run_0123456789abcdef0123456789abcdef",
            "session_id": "portal_0123456789abcdef0123456789abcdef",
            "status": "running",
        }

        result = self.provider.run(
            "test-agent-01",
            prompt="Deploy the game",
            session_id="portal_0123456789abcdef0123456789abcdef",
            on_update=updates.append,
        )

        self.assertEqual(updates[0].run_id, result.run_id)
        self.assertEqual(updates[0].status, "running")

    def test_embedded_client_keeps_event_stream_open_across_approval(self):
        self.provider.run(
            "test-agent-01",
            prompt="Deploy the game",
            session_id="portal_0123456789abcdef0123456789abcdef",
        )
        embedded_client = self.runner.calls[-1][0][-1]

        approval_branch = embedded_client.split(
            'if event.get("event") == "approval.request":', 1
        )[1].split(
            'if event.get("event") in {"run.completed", "run.failed", "run.cancelled"}:',
            1,
        )[0]
        self.assertIn("continue", approval_branch)
        self.assertNotIn("SystemExit", approval_branch)
        approve_action = embedded_client.split('elif action == "approve":', 1)[1].split(
            'elif action == "stop":', 1
        )[0]
        self.assertNotIn("poll(", approve_action)

    def test_transport_errors_are_safe_and_redacted(self):
        runner = ChatRunner(
            {
                "transport_error": True,
                "detail": {"error": {"message": "api_key=secret-value"}},
            }
        )
        provider = AgentChatProvider(self.provider.target, runner=runner)

        with self.assertRaises(AgentChatError) as caught:
            provider.run(
                "test-agent-01",
                prompt="hello",
                session_id="portal_0123456789abcdef0123456789abcdef",
            )

        self.assertIn("[REDACTED]", str(caught.exception))
        self.assertNotIn("secret-value", str(caught.exception))

    def test_rejects_oversized_prompt_before_kubectl(self):
        with self.assertRaises(ValueError):
            self.provider.run(
                "test-agent-01",
                prompt="x" * 32_001,
                session_id="portal_0123456789abcdef0123456789abcdef",
            )
        self.assertEqual(self.runner.calls, [])


if __name__ == "__main__":
    unittest.main()
