"""Functional tests for the ``kubeagents`` harness.

A local HTTP stub stands in for the in-cluster platform agent: the stub's port
is passed via ``AGENT_LOCAL_PORT``, so the harness sees an open port and never
spawns ``kubectl``. This exercises the full request -> parse -> AgentResult
path the eval harness consumes.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import entry_points
from typing import Any

import pytest

from devops_bench.agents import AGENTS, AgentResult
from kube_agents_bench.harness import KubeAgentsHarness

# Verbatim response from the platform-agent Observability & Benchmarking docs
# (stateful Responses API). Notably: function_call_output carries NO name --
# it correlates to its function_call via call_id only.
_CALL_ID = (
    "call_ff2395db2edb49e0b4c6740a5ca9__thought__"
    "EjQKMgEMOdbH2WnHSbVPCVbDZJwR92wgyy0Mb0mH0Jlk9v/23DldpofaTLEYye0chBckkfaz"
)
_TOOL_OUTPUT = json.dumps(
    {
        "result": "SUCCESS: operator-mercury-09-us-central1 | PROJECT: agentic-harness-demo",
        "structuredContent": {
            "result": "SUCCESS: operator-mercury-09-us-central1 | PROJECT: agentic-harness-demo"
        },
    }
)
_FINAL_TEXT = (
    "I have successfully initiated the provisioning of the operator agent for "
    "cluster mercury-09 in us-central1. The GKE rollout is in progress and "
    "should take approximately 5-8 minutes to complete."
)
_RESPONSE: dict[str, Any] = {
    "id": "resp_391e38a6c764442b80155a9a10f0",
    "object": "response",
    "status": "completed",
    "created_at": 1780001240,
    "model": "hermes-agent",
    "output": [
        {
            "type": "function_call",
            "name": "mcp_platform_control_provision_operator",
            "arguments": json.dumps({"location": "us-central1", "cluster_name": "mercury-09"}),
            "call_id": _CALL_ID,
        },
        {
            "type": "function_call_output",
            "call_id": _CALL_ID,
            "output": _TOOL_OUTPUT,
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": _FINAL_TEXT}],
        },
    ],
    "usage": {"input_tokens": 60468, "output_tokens": 79, "total_tokens": 60547},
}


class _StubAgentHandler(BaseHTTPRequestHandler):
    """Responses-style endpoint that records the request it served."""

    server: "_StubAgentServer"

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", 0))
        self.server.last_request = json.loads(self.rfile.read(length))
        self.server.last_auth = self.headers.get("Authorization")
        if self.path != "/v1/responses":
            self.send_error(404)
            return
        if self.server.fail_with is not None:
            body = json.dumps({"error": {"message": "agent exploded"}}).encode()
            self.send_response(self.server.fail_with)
        elif self.server.raw_body is not None:
            body = self.server.raw_body
            self.send_response(200)
        else:
            body = json.dumps(_RESPONSE).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # keep pytest output clean


class _StubAgentServer(ThreadingHTTPServer):
    last_request: dict[str, Any] | None = None
    last_auth: str | None = None
    fail_with: int | None = None
    raw_body: bytes | None = None


@pytest.fixture
def stub_agent(monkeypatch: pytest.MonkeyPatch) -> Generator[_StubAgentServer, None, None]:
    server = _StubAgentServer(("127.0.0.1", 0), _StubAgentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("AGENT_LOCAL_PORT", str(server.server_address[1]))
    monkeypatch.setenv("PLATFORM_AGENT_TOKEN", "test-token")
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def test_harness_resolves_through_the_entry_point() -> None:
    """The declared entry point is the only registration path.

    Nothing here registers the harness; importing the module has no side
    effects. Resolution therefore proves the packaging contract that lets the
    stock ``devops-bench`` CLI serve ``--agent-type kubeagents``. Requires a
    devops-bench that scans the ``devops_bench.agents`` group (#48).
    """
    declared = {ep.name: ep.value for ep in entry_points(group="devops_bench.agents")}
    assert declared["kubeagents"] == "kube_agents_bench.harness:KubeAgentsHarness"
    assert AGENTS.get("kubeagents") is KubeAgentsHarness


def test_run_parses_agent_response(stub_agent: _StubAgentServer) -> None:
    result = KubeAgentsHarness().run("Provision operator agent in cluster mercury-09.")

    assert isinstance(result, AgentResult)
    assert not result.has_errors()
    assert result.output == _FINAL_TEXT
    assert result.latency > 0.0
    assert result.tokens == {"input": 60468, "output": 79, "total": 60547}
    # One call in, one trajectory entry out: the function_call_output is folded
    # into its originating call (via call_id) rather than appended as a second
    # entry, which trajectory metrics would score as a redundant empty call.
    assert len(result.trajectory) == 1
    assert result.trajectory[0] == {
        "name": "mcp_platform_control_provision_operator",
        "args": {"location": "us-central1", "cluster_name": "mercury-09"},
        "result": _TOOL_OUTPUT,
        "status": "completed",
    }
    assert result.metadata["tools"] == {"mcp_platform_control_provision_operator": 1}
    # The stateful response id is preserved so the run can be joined back to
    # GET /v1/responses/<id> for the full execution trajectory.
    assert result.metadata["response_id"] == "resp_391e38a6c764442b80155a9a10f0"
    assert result.metadata["response_status"] == "completed"

    # The transport carried the prompt, model, and bearer token.
    assert stub_agent.last_request is not None
    assert stub_agent.last_request["input"] == "Provision operator agent in cluster mercury-09."
    assert stub_agent.last_request["model"] == "hermes-agent"
    assert stub_agent.last_auth == "Bearer test-token"


def test_http_error_becomes_errored_result(stub_agent: _StubAgentServer) -> None:
    stub_agent.fail_with = 500

    result = KubeAgentsHarness().run("prompt")

    assert result.has_errors()
    assert "HTTP 500" in result.errors[0]
    assert "agent exploded" in result.errors[0]


def test_non_object_json_becomes_errored_result(stub_agent: _StubAgentServer) -> None:
    stub_agent.raw_body = json.dumps(["not", "an", "object"]).encode()

    result = KubeAgentsHarness().run("prompt")

    assert result.has_errors()
    assert "non-object JSON" in result.errors[0]


def test_unreachable_endpoint_becomes_errored_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A closed port with kubectl missing from PATH: the port-forward attempt
    # fails fast and surfaces as a known error, not an exception.
    monkeypatch.setenv("AGENT_LOCAL_PORT", "1")  # privileged port, never open
    monkeypatch.setenv("PATH", "/nonexistent")

    result = KubeAgentsHarness().run("prompt")

    assert result.has_errors()
