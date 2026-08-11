from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from kube_agents_bench.cuj import (
    Agent,
    AssertionOutcome,
    CUJEvaluator,
    JudgeDecision,
    MessageGoal,
    Persona,
    PortalTransport,
    PortalTransportError,
    Scenario,
    SoftGoal,
    ToolGoal,
)


class PortalHandler(BaseHTTPRequestHandler):
    get_count = 0
    output = "Capacity checked in project demo-project, cluster host, us-east4."
    tool_calls = [
        {"name": "compute.capacity.get", "status": "completed"},
    ]
    final_status = "completed"
    diagnostics: list[str] = []
    redirect_target_count = 0

    def log_message(self, format: str, *args) -> None:
        return None

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        if length:
            json.loads(self.rfile.read(length))
        if self.path == "/api/v1/interactions":
            self._json(
                202,
                {
                    "interactionId": "ix_test",
                    "sessionId": "portal_eval_test",
                    "status": "queued",
                    "terminal": False,
                },
            )
            return
        self._json(404, {})

    def do_GET(self) -> None:
        if self.path == "/api/v1/redirect":
            self.send_response(302)
            self.send_header("location", "/api/v1/redirect-target")
            self.end_headers()
            return
        if self.path == "/api/v1/redirect-target":
            type(self).redirect_target_count += 1
            self._json(200, {"followed": True})
            return
        if self.path != "/api/v1/interactions/ix_test":
            self._json(404, {})
            return
        type(self).get_count += 1
        if type(self).get_count == 1:
            self._json(
                200,
                {
                    "interactionId": "ix_test",
                    "sessionId": "portal_eval_test",
                    "status": "waiting_for_tasks",
                    "terminal": False,
                    "output": "I delegated the check.",
                },
            )
            return
        status = type(self).final_status
        self._json(
            200,
            {
                "interactionId": "ix_test",
                "sessionId": "portal_eval_test",
                "status": status,
                "terminal": True,
                "output": type(self).output,
                "error": "delegated capacity check failed" if status == "failed" else "",
                "diagnostics": type(self).diagnostics,
                "toolCalls": type(self).tool_calls,
                "tasks": [{"taskId": "t_capacity", "status": "done"}],
            },
        )

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class AlwaysPassJudge:
    def evaluate(self, *, rubric: str, response: str, context: dict) -> JudgeDecision:
        return JudgeDecision(True, "Response is concise and professional.", (rubric,))


def run_scenario(goals, *, judge=None):
    PortalHandler.get_count = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), PortalHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        agent = Agent(
            "test-agent",
            "platform-agent",
            f"http://127.0.0.1:{server.server_port}/api/v1",
        )
        persona = Persona(
            "platform-admin",
            "Platform admin",
            "Platform Administrator",
        )
        scenario = Scenario(
            "capacity-cuj",
            "Capacity CUJ",
            "Check capacity",
            tuple(goals),
            timeout_seconds=2,
            poll_interval_seconds=0.001,
        )
        return CUJEvaluator(judge=judge).run(agent, persona, scenario)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class CUJEvaluatorTest(unittest.TestCase):
    def test_credentialed_transport_requires_https(self):
        with self.assertRaisesRegex(ValueError, "must use HTTPS"):
            PortalTransport("http://portal.example.test/api/v1", token="secret")

    def test_transport_does_not_follow_redirects(self):
        PortalHandler.redirect_target_count = 0
        server = ThreadingHTTPServer(("127.0.0.1", 0), PortalHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            transport = PortalTransport(
                f"http://127.0.0.1:{server.server_port}/api/v1"
            )
            with self.assertRaisesRegex(PortalTransportError, "HTTP 302"):
                transport.get("redirect")
            self.assertEqual(PortalHandler.redirect_target_count, 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_async_run_waits_for_terminal_work_before_asserting(self):
        PortalHandler.output = (
            "Capacity checked in project demo-project, cluster host, us-east4."
        )
        PortalHandler.tool_calls = [
            {"name": "compute.capacity.get", "status": "completed"},
        ]
        PortalHandler.final_status = "completed"
        PortalHandler.diagnostics = []

        run = run_scenario(
            (
                ToolGoal(
                    "capacity-tool",
                    "Capacity API is actually called.",
                    ("compute.capacity.get",),
                ),
                MessageGoal(
                    "environment-message",
                    "Names the environment.",
                    ("demo-project", "host", "us-east4"),
                ),
                SoftGoal(
                    "response-quality",
                    "Responds clearly.",
                    "Professional, direct, and concise.",
                    max_words=30,
                ),
            ),
            judge=AlwaysPassJudge(),
        )

        self.assertEqual(PortalHandler.get_count, 2)
        self.assertTrue(run.passed)
        self.assertEqual(
            [item.outcome for item in run.assertions],
            [
                AssertionOutcome.PASSED,
                AssertionOutcome.PASSED,
                AssertionOutcome.PASSED,
                AssertionOutcome.PASSED,
            ],
        )
        self.assertEqual(run.conversation[1]["content"], PortalHandler.output)

    def test_agent_promise_does_not_satisfy_missing_tool_evidence(self):
        PortalHandler.output = (
            "I will call the capacity API later and then report the stockout analysis."
        )
        PortalHandler.tool_calls = [
            {"name": "compute.capacity.get", "status": "started"},
        ]
        PortalHandler.final_status = "completed"
        PortalHandler.diagnostics = []

        run = run_scenario(
            (
                ToolGoal(
                    "capacity-tool",
                    "Capacity API is actually called.",
                    ("compute.capacity.get",),
                ),
                MessageGoal(
                    "completed-analysis",
                    "Returns the checked environment.",
                    ("demo-project", "us-east4"),
                ),
            )
        )

        assertions = {item.goal_id: item for item in run.assertions}
        self.assertFalse(run.passed)
        self.assertEqual(
            assertions["capacity-tool"].outcome,
            AssertionOutcome.FAILED,
        )
        self.assertIn("promises", assertions["capacity-tool"].diagnostics[0])
        self.assertEqual(
            assertions["completed-analysis"].outcome,
            AssertionOutcome.FAILED,
        )
        self.assertEqual(run.conversation[1]["content"], PortalHandler.output)

    def test_failed_interaction_keeps_target_diagnostics(self):
        PortalHandler.output = ""
        PortalHandler.tool_calls = []
        PortalHandler.final_status = "failed"
        PortalHandler.diagnostics = ["Inspect Task Kanban task t_capacity."]

        run = run_scenario(())

        self.assertFalse(run.passed)
        self.assertEqual(
            run.assertions[0].outcome,
            AssertionOutcome.FAILED,
        )
        self.assertEqual(
            run.assertions[0].diagnostics,
            ("Inspect Task Kanban task t_capacity.",),
        )

    def test_observed_interaction_can_be_rescored_without_rerunning_agent(self):
        interaction = {
            "interactionId": "ix_recorded",
            "status": "completed",
            "terminal": True,
            "output": "Connected to project demo-project in us-east4.",
            "toolCalls": [
                {"name": "delegate_task", "status": "completed"},
            ],
            "tasks": [],
        }
        agent = Agent("test-agent", "platform-agent", "http://127.0.0.1:1/api/v1")
        persona = Persona(
            "platform-admin",
            "Platform admin",
            "Platform Administrator",
        )
        scenario = Scenario(
            "recorded",
            "Recorded run",
            "Identify the environment",
            (
                ToolGoal("delegated", "Delegated.", ("delegate_task",)),
                MessageGoal("project", "Names project.", ("demo-project",)),
            ),
        )

        run = CUJEvaluator().evaluate_observed(
            agent,
            persona,
            scenario,
            interaction,
        )

        self.assertTrue(run.passed)
        self.assertEqual(run.interaction_id, "ix_recorded")


if __name__ == "__main__":
    unittest.main()
