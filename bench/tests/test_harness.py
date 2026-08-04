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
from kube_agents_bench import harness
from kube_agents_bench.harness import KubeAgentsHarness, _parse_response

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

# A turn with parallel tool calls: hermes emits both function_calls before
# either tool returns, so the outputs arrive in completion order rather than
# call order, and one sibling can fail while the other succeeds.
_LIST_ID = "call_list_9f2a"
_US_ID = "call_prov_us_central1"
_EU_ID = "call_prov_europe_west4"
_MULTI_RESPONSE: dict[str, Any] = {
    "id": "resp_5c1d0be2aa1f43829f6d",
    "object": "response",
    "status": "completed",
    "model": "hermes-agent",
    "output": [
        {
            "type": "function_call",
            "name": "mcp_platform_control_list_clusters",
            "arguments": json.dumps({"project": "agentic-harness-demo"}),
            "call_id": _LIST_ID,
        },
        {
            "type": "function_call_output",
            "call_id": _LIST_ID,
            "output": json.dumps({"result": "mercury-09, venus-02"}),
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Two clusters found. "}],
        },
        {
            "type": "function_call",
            "name": "mcp_platform_control_provision_operator",
            "arguments": json.dumps({"location": "us-central1", "cluster_name": "mercury-09"}),
            "call_id": _US_ID,
        },
        {
            "type": "function_call",
            "name": "mcp_platform_control_provision_operator",
            "arguments": json.dumps({"location": "europe-west4", "cluster_name": "venus-02"}),
            "call_id": _EU_ID,
        },
        # europe-west4 returned first, and failed
        {
            "type": "function_call_output",
            "call_id": _EU_ID,
            "output": json.dumps({"error": "quota exceeded in europe-west4"}),
        },
        {
            "type": "function_call_output",
            "call_id": _US_ID,
            "output": json.dumps({"result": "SUCCESS: operator-mercury-09-us-central1"}),
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "mercury-09 is up; venus-02 hit quota."}],
        },
    ],
    "usage": {"input_tokens": 91204, "output_tokens": 218, "total_tokens": 91422},
}


_SESSION_ID = "sess_2f1c94ab77d4"

# GET /api/sessions/<id>, trimmed to the keys the harness reads (the real
# _session_response allowlist at api_server.py:2179 is much wider).
#
# These reconcile with _RESPONSE's envelope, as a real row must: hermes'
# prompt_tokens is input + cache_read + cache_write (usage_pricing.py:44), so
# 1076 + 51200 + 8192 = the envelope's 60468, and 60468 + 79 = its 60547.
_SESSION_ROW: dict[str, Any] = {
    "object": "hermes.session",
    "session": {
        "id": _SESSION_ID,
        "input_tokens": 1076,
        "output_tokens": 79,
        "cache_read_tokens": 51200,
        "cache_write_tokens": 8192,
        "reasoning_tokens": 1024,
    },
}


class _StubAgentHandler(BaseHTTPRequestHandler):
    """Responses-style endpoint that records the request it served."""

    server: "_StubAgentServer"

    def _respond(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", 0))
        self.server.last_request = json.loads(self.rfile.read(length))
        self.server.last_auth = self.headers.get("Authorization")
        if self.path != "/v1/responses":
            self.send_error(404)
            return
        if self.server.redirect_to is not None:
            self.send_response(302)
            self.send_header("Location", self.server.redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        headers = {}
        if self.server.session_id:
            headers["X-Hermes-Session-Id"] = self.server.session_id
        if self.server.fail_with is not None:
            body = json.dumps({"error": {"message": "agent exploded"}}).encode()
            self._respond(self.server.fail_with, body, headers)
        elif self.server.raw_body is not None:
            self._respond(200, self.server.raw_body, headers)
        else:
            self._respond(200, json.dumps(_RESPONSE).encode(), headers)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self.server.session_lookups.append(self.path)
        # urllib rewrites a 302'd POST into a GET, so this is where a leaked
        # bearer token would surface on a redirect target.
        self.server.get_auths.append(self.headers.get("Authorization"))
        if not self.path.startswith("/api/sessions/"):
            self.send_error(404)
            return
        if self.server.session_fail_with is not None:
            self.send_error(self.server.session_fail_with)
            return
        if self.server.session_raw_body is not None:
            self._respond(200, self.server.session_raw_body)
            return
        self._respond(200, json.dumps(self.server.session_row).encode())

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # keep pytest output clean


class _StubAgentServer(ThreadingHTTPServer):
    last_request: dict[str, Any] | None = None
    last_auth: str | None = None
    fail_with: int | None = None
    raw_body: bytes | None = None
    session_id: str | None = _SESSION_ID
    session_row: dict[str, Any] = _SESSION_ROW
    session_fail_with: int | None = None
    session_raw_body: bytes | None = None
    session_lookups: list[str]
    get_auths: list[str | None]
    redirect_to: str | None = None


@pytest.fixture
def stub_agent(monkeypatch: pytest.MonkeyPatch) -> Generator[_StubAgentServer, None, None]:
    server = _StubAgentServer(("127.0.0.1", 0), _StubAgentHandler)
    server.session_lookups = []
    server.get_auths = []
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
    if getattr(AGENTS, "_entry_point_group", None) != "devops_bench.agents":
        pytest.skip(
            "installed devops-bench predates agent entry-point discovery "
            "(devops-bench#48); resolution activates when the pin is bumped"
        )
    assert AGENTS.get("kubeagents") is KubeAgentsHarness


def test_run_parses_agent_response(stub_agent: _StubAgentServer) -> None:
    result = KubeAgentsHarness().run("Provision operator agent in cluster mercury-09.")

    assert isinstance(result, AgentResult)
    assert not result.has_errors()
    assert result.output == _FINAL_TEXT
    assert result.latency > 0.0
    # The session row replaces the envelope's counts wholesale. input is the
    # *non-cached* prompt per TOKEN_BUCKETS, so it is the row's 1076 and not
    # the envelope's cache-inclusive 60468; total is the sum of the buckets.
    assert result.tokens == {
        "input": 1076,
        "cached": 51200,
        "cache_write": 8192,
        "reasoning": 1024,
        "output": 79,
        "total": 61571,
    }
    assert result.metadata["session_id"] == _SESSION_ID
    assert stub_agent.session_lookups == [f"/api/sessions/{_SESSION_ID}"]
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


def test_run_parses_a_multi_call_response(stub_agent: _StubAgentServer) -> None:
    """Three calls, folded independently and left in call order.

    The interesting parts of a multi-call turn are all things a one-call
    payload cannot show: outputs that arrive out of order, one tool failing
    beside a successful sibling, and the same tool invoked twice.
    """
    stub_agent.raw_body = json.dumps(_MULTI_RESPONSE).encode()
    # No session row for this payload, so the envelope's own counts stand
    stub_agent.session_id = None

    result = KubeAgentsHarness().run("Provision operators for every cluster.")

    assert not result.has_errors()
    # Text is accumulated across both message items, in order
    assert result.output == "Two clusters found. mercury-09 is up; venus-02 hit quota."
    assert result.tokens["input"] == 91204
    assert result.tokens["output"] == 218
    assert result.tokens["total"] == 91422

    # Three calls in, three entries out -- one per function_call, ordered as
    # called, not as returned. The eu output arrived before the us one; keying
    # the fold on call_id is what keeps each result on its own call.
    assert result.trajectory == [
        {
            "name": "mcp_platform_control_list_clusters",
            "args": {"project": "agentic-harness-demo"},
            "result": json.dumps({"result": "mercury-09, venus-02"}),
            "status": "completed",
        },
        {
            "name": "mcp_platform_control_provision_operator",
            "args": {"location": "us-central1", "cluster_name": "mercury-09"},
            "result": json.dumps({"result": "SUCCESS: operator-mercury-09-us-central1"}),
            "status": "completed",
        },
        {
            "name": "mcp_platform_control_provision_operator",
            "args": {"location": "europe-west4", "cluster_name": "venus-02"},
            "result": json.dumps({"error": "quota exceeded in europe-west4"}),
            # Per-call, not per-response: a failed sibling does not taint the
            # calls that worked, and a partly-failed turn is not a failed run.
            "status": "error",
        },
    ]
    assert result.metadata["tools"] == {
        "mcp_platform_control_list_clusters": 1,
        "mcp_platform_control_provision_operator": 2,
    }


def test_unkeyed_outputs_fold_onto_calls_in_order() -> None:
    """Without call_ids the fold is FIFO: first output to the first open call."""
    payload = {
        "output": [
            {"type": "function_call", "name": "first", "arguments": "{}"},
            {"type": "function_call", "name": "second", "arguments": "{}"},
            {"type": "function_call_output", "output": "1"},
            {"type": "function_call_output", "output": "2"},
        ]
    }

    result = _parse_response(payload)

    assert [(e["name"], e["result"]) for e in result.trajectory] == [
        ("first", "1"),
        ("second", "2"),
    ]


def test_a_call_with_no_output_stays_called(stub_agent: _StubAgentServer) -> None:
    """An unresolved call keeps the default status -- it is not a failure.

    A turn can end mid-flight (iteration cap, truncation) with the last call's
    output never emitted. Scoring it "error" would invent a failure the agent
    never had; "completed" would credit a result that never arrived.
    """
    truncated = {**_MULTI_RESPONSE, "output": _MULTI_RESPONSE["output"][:5]}
    stub_agent.raw_body = json.dumps(truncated).encode()

    result = KubeAgentsHarness().run("prompt")

    assert not result.has_errors()
    assert [e["status"] for e in result.trajectory] == ["completed", "called", "called"]
    assert result.trajectory[2]["result"] is None


_A_CALL = {"type": "function_call", "name": "t", "arguments": "{}", "call_id": _US_ID}
_ITS_OUTPUT = {"type": "function_call_output", "call_id": _US_ID, "output": "ok"}
_UNKNOWN_ID = {"type": "function_call_output", "call_id": "call_never_seen", "output": "orphan"}


# Every way an output can arrive with no open call to fold into. The fold pops
# its target on match, so a replayed output is orphaned too -- its call is
# already closed, and folding it again would overwrite a real result.
@pytest.mark.parametrize(
    "output_items",
    [
        pytest.param([_UNKNOWN_ID], id="unknown-call-id-with-no-calls-at-all"),
        pytest.param([_A_CALL, _ITS_OUTPUT, _UNKNOWN_ID], id="unknown-call-id-after-the-fold"),
        pytest.param(
            [{"type": "function_call_output", "output": "orphan"}],
            id="no-call-id-and-no-open-call",
        ),
        pytest.param(
            [_A_CALL, _ITS_OUTPUT, {**_ITS_OUTPUT, "output": "orphan"}],
            id="a-second-output-for-an-already-folded-call",
        ),
    ],
)
def test_an_orphaned_output_is_kept_and_flagged(output_items: list[dict[str, Any]]) -> None:
    result = _parse_response({"output": output_items})

    # The payload survives as its own entry rather than being dropped ...
    assert result.trajectory[-1] == {
        "name": "",
        "args": {},
        "result": "orphan",
        "status": "completed",
    }
    # ... and the anomaly is reported, because a trajectory that silently
    # swallows tool output is one a judge cannot be trusted to score.
    assert len(result.errors) == 1
    assert "tool output without a matching call" in result.errors[0]


def test_orphaned_outputs_do_not_disturb_the_calls_around_them() -> None:
    """Orphans are appended in place, leaving the real calls correctly folded."""
    payload = {
        "output": [
            {"type": "function_call", "name": "first", "arguments": "{}", "call_id": "c1"},
            {"type": "function_call_output", "call_id": "c1", "output": "1"},
            {"type": "function_call_output", "call_id": "ghost_a", "output": "stray"},
            {"type": "function_call", "name": "second", "arguments": "{}", "call_id": "c2"},
            {"type": "function_call_output", "call_id": "c2", "output": "2"},
            # An orphan is classified like any other output, not defaulted
            {"type": "function_call_output", "call_id": "ghost_b", "output": '{"error": "boom"}'},
        ]
    }

    result = _parse_response(payload)

    assert [(e["name"], e["result"], e["status"]) for e in result.trajectory] == [
        ("first", "1", "completed"),
        ("", "stray", "completed"),
        ("second", "2", "completed"),
        ("", '{"error": "boom"}', "error"),
    ]
    assert len(result.errors) == 2
    assert "ghost_a" in result.errors[0]
    assert "ghost_b" in result.errors[1]


def test_an_orphaned_output_flags_the_run_without_failing_it(
    stub_agent: _StubAgentServer,
) -> None:
    """The flag reaches ``AgentResult.errors``, and the rest still scores.

    ``errors`` is a diagnostic surface, not a kill switch: nothing in the
    library gates on it. A malformed tail must not cost the run its final text
    or the calls the agent genuinely made.
    """
    orphaned = {**_MULTI_RESPONSE, "output": [*_MULTI_RESPONSE["output"], _UNKNOWN_ID]}
    stub_agent.raw_body = json.dumps(orphaned).encode()

    result = KubeAgentsHarness().run("prompt")

    assert result.has_errors()
    assert "call_never_seen" in result.errors[0]
    assert result.output == "Two clusters found. mercury-09 is up; venus-02 hit quota."
    assert [e["status"] for e in result.trajectory] == [
        "completed",
        "completed",
        "error",
        "completed",  # the orphan
    ]
    # The orphan has no name, so it never inflates the per-tool counts
    assert result.metadata["tools"] == {
        "mcp_platform_control_list_clusters": 1,
        "mcp_platform_control_provision_operator": 2,
    }


# Every way the lookup can fail must leave the envelope's counts in place -- a
# run scores the agent, and must not fail over supplementary accounting.
@pytest.mark.parametrize(
    ("attr", "value"),
    [
        ("session_fail_with", 503),  # hermes' "session database unavailable"
        ("session_fail_with", 404),  # a build that does not route /api/sessions
        # A row without the token keys, and a body that is not a session at all
        ("session_row", {"object": "hermes.session", "session": {"id": _SESSION_ID}}),
        ("session_row", {"unexpected": True}),
        # A row missing one bucket is discarded whole, not merged: taking the
        # row's cache counts beside the envelope's cache-inclusive input would
        # count the cache twice.
        (
            "session_row",
            {
                "object": "hermes.session",
                "session": {
                    k: v for k, v in _SESSION_ROW["session"].items() if k != "output_tokens"
                },
            },
        ),
        # Not UTF-8, so .decode() raises -- a ValueError, not a JSONDecodeError
        ("session_raw_body", b"\xff\xfe not utf-8"),
    ],
)
def test_a_failed_session_lookup_leaves_the_envelope_counts(
    stub_agent: _StubAgentServer, attr: str, value: Any
) -> None:
    setattr(stub_agent, attr, value)

    result = KubeAgentsHarness().run("prompt")

    assert not result.has_errors()
    assert result.output == _FINAL_TEXT
    # The envelope's own accounting, untouched and self-consistent
    assert result.tokens["input"] == 60468
    assert result.tokens["output"] == 79
    assert result.tokens["total"] == 60547
    assert result.tokens["cached"] is None
    assert result.tokens["cache_write"] is None
    assert result.tokens["reasoning"] is None


def test_no_session_header_skips_the_lookup(stub_agent: _StubAgentServer) -> None:
    """No id, no second request -- there is nothing to correlate a row to."""
    stub_agent.session_id = None

    result = KubeAgentsHarness().run("prompt")

    assert stub_agent.session_lookups == []
    assert "session_id" not in result.metadata
    assert result.tokens["cached"] is None
    assert result.tokens["input"] == 60468


def test_session_id_is_percent_encoded_into_the_lookup_path(
    stub_agent: _StubAgentServer,
) -> None:
    """The id is quoted, not interpolated.

    Hermes treats this header as untrusted (run_agent.py:2719); an id holding
    ``/`` or ``?`` would otherwise retarget the GET at another route.
    """
    stub_agent.session_id = "../v1/responses?x=1"

    KubeAgentsHarness().run("prompt")

    assert stub_agent.session_lookups == ["/api/sessions/..%2Fv1%2Fresponses%3Fx%3D1"]


def _one_call_response(output: Any, status: str | None = None) -> dict[str, Any]:
    """A one-call/one-output payload carrying ``output`` (and optionally ``status``)."""
    item: dict[str, Any] = {
        "type": "function_call_output",
        "call_id": _CALL_ID,
        "output": output,
    }
    if status is not None:
        item["status"] = status
    return {
        "output": [
            {"type": "function_call", "name": "t", "arguments": "{}", "call_id": _CALL_ID},
            item,
        ]
    }


# Every shape hermes uses to signal a failed tool, traced to what emits it
@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("Error executing tool 'terminal': boom", "error"),  # tool_executor.py
        ("Error executing tool: boom", "error"),  # conversation_loop.py
        # mcp_tool.py on CallToolResult.isError, and registry.py tool_error()
        (json.dumps({"error": "MCP tool returned an error"}), "error"),
        (json.dumps({"error": "bad input", "success": False}), "error"),
        (json.dumps({"success": False, "transcript": ""}), "error"),
        (json.dumps({"ok": False}), "error"),
        (json.dumps({"exit_code": 1, "stdout": ""}), "error"),
        (json.dumps({"returncode": 2}), "error"),
        (_TOOL_OUTPUT, "completed"),
        (json.dumps({"success": True, "count": 42}), "completed"),
        (json.dumps({"exit_code": 0, "stdout": "ok"}), "completed"),
        # A payload key beside the error is a payload whichever key carries it:
        # MCP uses `content`, hermes' own tools use `result`/`structuredContent`
        (json.dumps({"error": "deprecated flag", "result": "the answer"}), "completed"),
        (json.dumps({"error": "partial", "structuredContent": {"n": 1}}), "completed"),
        # Free text is never sniffed -- only structured failure counts
        ("2 tests failed with error: assertion", "completed"),
        ("ERROR: 0 occurrences found", "completed"),
        # An error beside a real payload is a diagnostic, not a failure
        (json.dumps({"error": "partial", "content": "the answer"}), "completed"),
    ],
)
def test_tool_failure_is_read_out_of_the_output_payload(output: str, expected: str) -> None:
    result = _parse_response(_one_call_response(output))

    assert result.trajectory[0]["status"] == expected


# execute_code's own status (code_execution_tool.py) is derived, never
# independent -- timeout/interrupt come back off the exit code and the rest
# set "error" -- so the generic rules already cover it. Uses the exit codes
# hermes really sets, to catch a path ever stopping being covered.
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"status": "success", "output": "42", "exit_code": 0}, "completed"),
        ({"status": "timeout", "exit_code": -9, "error": "Script timed out"}, "error"),
        ({"status": "interrupted", "output": "x", "exit_code": -1}, "error"),
        ({"status": "timeout", "exit_code": 124, "error": "Script timed out"}, "error"),
        ({"status": "interrupted", "output": "x", "exit_code": 130}, "error"),
        ({"status": "error", "exit_code": 1, "error": "Traceback..."}, "error"),
        # No exit_code at all: the guard / python-missing early returns
        ({"status": "error", "error": "blocked by approval guard."}, "error"),
    ],
)
def test_execute_code_failures_are_covered_by_the_generic_rules(
    payload: dict[str, Any], expected: str
) -> None:
    full = {"tool_calls_made": 3, "duration_seconds": 1.2, **payload}

    result = _parse_response(_one_call_response(json.dumps(full)))

    assert result.trajectory[0]["status"] == expected


def test_a_bare_status_field_is_not_read_as_a_tool_outcome() -> None:
    """``status`` is deliberately not a failure signal on its own.

    Tools here report *resource* status: a pod phase of "Error" is a successful
    query about a broken pod, not a broken tool call.
    """
    payload = json.dumps({"status": "Error", "pod": "operator-mercury-09", "restarts": 4})

    result = _parse_response(_one_call_response(payload))

    assert result.trajectory[0]["status"] == "completed"


def test_streaming_shaped_output_is_flattened_to_text() -> None:
    """The streaming builder wraps the payload in input_text blocks.

    ``ToolCall.result`` is typed ``str | None``, and the failure check reads
    text, so both builders' shapes must land on the same string.
    """
    blocks = [{"type": "input_text", "text": json.dumps({"error": "nope"})}]

    result = _parse_response(_one_call_response(blocks))

    assert result.trajectory[0]["result"] == '{"error": "nope"}'
    assert result.trajectory[0]["status"] == "error"


# Hermes never sends an item status on a tool result in the final output
# array, and the one place it writes the field -- the streaming SSE event --
# hardcodes "completed" on failures too, so reading it could only mask them.
@pytest.mark.parametrize("reported", ["completed", "in_progress", "failed", "quantum"])
def test_a_reported_item_status_is_ignored_in_favour_of_the_payload(reported: str) -> None:
    ok = _parse_response(_one_call_response(_TOOL_OUTPUT, status=reported))
    bad = _parse_response(_one_call_response(json.dumps({"error": "boom"}), status=reported))

    assert ok.trajectory[0]["status"] == "completed"
    assert bad.trajectory[0]["status"] == "error"
    assert not ok.has_errors()


def test_a_keyed_output_never_consumes_an_unkeyed_call() -> None:
    """An unmatched ``call_id`` is an orphan, not a claim on someone else's call.

    Hermes emits ``tc.get("id", "")`` (api_server.py:4530), so an unkeyed call
    can sit open beside keyed ones. A replayed output for an already-folded
    call must not overwrite that open call with the wrong tool's payload.
    """
    payload = {
        "output": [
            {"type": "function_call", "name": "unkeyed", "arguments": "{}"},
            {"type": "function_call", "name": "keyed", "arguments": "{}", "call_id": "x"},
            {"type": "function_call_output", "call_id": "x", "output": "keyed result"},
            {"type": "function_call_output", "call_id": "x", "output": "replayed"},
        ]
    }

    result = _parse_response(payload)

    # The unkeyed call is left open rather than fed the replayed payload
    assert result.trajectory[0] == {
        "name": "unkeyed",
        "args": {},
        "result": None,
        "status": "called",
    }
    assert result.trajectory[1]["result"] == "keyed result"
    assert result.trajectory[2]["result"] == "replayed"  # the orphan
    assert "tool output without a matching call" in result.errors[0]


# Valid JSON in the wrong shape. Each of these used to raise, and the base
# class converted the whole run into an errored result -- discarding text and
# calls that had already parsed cleanly.
@pytest.mark.parametrize(
    ("payload", "note"),
    [
        ({"output": [], "usage": None}, "usage is null, not absent"),
        ({"output": "not a list"}, "output is a string"),
        ({"output": ["not an object", 42]}, "output items are scalars"),
        ({"output": [{"type": "message", "role": "assistant", "content": "text"}]}, "content str"),
        ({"output": [{"type": "message", "role": "assistant", "content": [None, "x"]}]}, "chunks"),
    ],
)
def test_a_misshapen_payload_degrades_instead_of_raising(
    payload: dict[str, Any], note: str
) -> None:
    result = _parse_response(payload)

    assert isinstance(result, AgentResult)
    assert result.tokens["input"] is None


def test_a_misshapen_payload_keeps_what_parsed(stub_agent: _StubAgentServer) -> None:
    """The readable half of a bad payload still reaches the judge."""
    payload = {
        "output": [
            {"type": "function_call", "name": "t", "arguments": "{}", "call_id": "x"},
            "a stray string where an item should be",
            {"type": "function_call_output", "call_id": "x", "output": "ok"},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text",
                                                                  "text": "done"}]},
        ],
        "usage": None,
    }
    stub_agent.raw_body = json.dumps(payload).encode()

    result = KubeAgentsHarness().run("prompt")

    assert result.output == "done"
    assert result.trajectory[0]["result"] == "ok"
    assert result.trajectory[0]["status"] == "completed"
    assert "non-object output item" in result.errors[0]


@pytest.mark.parametrize("name", [{"a": 1}, ["t"], 7, None])
def test_a_non_string_tool_name_does_not_raise(name: Any) -> None:
    """``metadata['tools']`` is keyed by name, so an unhashable one must not
    escape the parser that promises to degrade."""
    payload = {
        "output": [
            {"type": "function_call", "name": name, "arguments": "{}", "call_id": "x"},
            {"type": "function_call_output", "call_id": "x", "output": "ok"},
        ]
    }

    result = _parse_response(payload)

    assert isinstance(result.trajectory[0]["name"], str)
    assert result.trajectory[0]["result"] == "ok"
    assert all(isinstance(key, str) for key in result.metadata["tools"])


def test_a_redirect_is_refused_and_never_forwards_the_token(
    stub_agent: _StubAgentServer,
) -> None:
    """urllib follows redirects and does NOT strip Authorization on a cross-host
    hop, so a 302 would hand the bearer token to another origin -- the very
    thing the AGENT_API_PATH check exists to prevent."""
    sink = _StubAgentServer(("127.0.0.1", 0), _StubAgentHandler)
    sink.session_lookups = []
    sink.get_auths = []
    threading.Thread(target=sink.serve_forever, daemon=True).start()
    stub_agent.redirect_to = f"http://127.0.0.1:{sink.server_address[1]}/stolen"

    try:
        result = KubeAgentsHarness().run("prompt")
    finally:
        sink.shutdown()
        sink.server_close()

    # The redirect target saw no request at all. Without the no-redirect opener
    # urllib rewrites the POST into a GET and carries the bearer token along,
    # so get_auths -- not last_auth -- is what proves nothing leaked.
    assert sink.get_auths == []
    assert sink.last_request is None
    assert result.has_errors()
    assert "HTTP 302" in result.errors[0]


def test_the_session_lookup_does_not_inherit_the_agent_timeout(
    stub_agent: _StubAgentServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lookup only refines accounting; a hung route must not be billed as
    the agent's latency."""
    monkeypatch.setenv("AGENT_HTTP_TIMEOUT", "600")
    seen: list[float] = []
    monkeypatch.setattr(
        harness,
        "_canonical_session_tokens",
        lambda tokens, sid, port, headers, timeout: seen.append(timeout),
    )

    KubeAgentsHarness().run("prompt")

    assert seen == [harness._SESSION_LOOKUP_TIMEOUT]
    assert harness._SESSION_LOOKUP_TIMEOUT < 600


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("AGENT_LOCAL_PORT", ""),  # an empty k8s env value, not an absent one
        ("AGENT_LOCAL_PORT", "not-a-port"),
        ("AGENT_HTTP_TIMEOUT", ""),
        ("AGENT_HTTP_TIMEOUT", "10s"),
    ],
)
def test_a_malformed_numeric_env_var_names_itself(
    monkeypatch: pytest.MonkeyPatch, var: str, value: str
) -> None:
    """Known misconfiguration comes back as a named error, like AGENT_API_PATH --
    not as an opaque ValueError from the base class's safety net."""
    monkeypatch.setenv(var, value)

    result = KubeAgentsHarness().run("prompt")

    assert result.has_errors()
    assert var in result.errors[0]
    assert "must be numeric" in result.errors[0]


def test_a_relative_api_path_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """``AGENT_API_PATH`` is concatenated onto the base URL, so it must be a path.

    ``@host/...`` would turn ``127.0.0.1:<port>`` into userinfo and send the
    request -- with the bearer token -- to ``host`` instead.
    """
    monkeypatch.setenv("AGENT_API_PATH", "@evil.example/v1/responses")
    monkeypatch.setenv("AGENT_LOCAL_PORT", "1")

    result = KubeAgentsHarness().run("prompt")

    assert result.has_errors()
    assert "AGENT_API_PATH must start with '/'" in result.errors[0]


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
    # The spawn failure must travel the harness's own known-error path, not
    # the base class's unexpected-exception safety net: the error names the
    # port-forward rather than a bare FileNotFoundError traceback.
    assert "port-forward" in result.errors[0]
