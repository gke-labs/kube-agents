"""``kubeagents`` agent harness: HTTP transport to the in-cluster platform agent.

A port of the legacy ``pkg/agents/runner/kubeagents.py`` evaluator runner onto
devops-bench's :class:`~devops_bench.agents.AgentHarness` contract. The agent
itself runs inside the cluster; this harness only:

1. ensures the agent service is reachable on a local port (lazily spawning a
   background ``kubectl port-forward`` when it is not),
2. POSTs the task prompt to the agent's Responses-style HTTP endpoint, and
3. parses the response into the canonical :class:`AgentResult` shape
   (final text, tool-call trajectory, token usage).

No model SDK is imported here -- all inference happens in the cluster, which
is why this package needs only the ``devops_bench.agents`` extension axis and
not the ``MODELS`` registry.

Environment:
    AGENT_LOCAL_PORT: Local port for the agent endpoint (default ``8642``).
    AGENT_API_PATH: Request path (default ``/v1/responses``).
    AGENT_SERVICE_NAME: Service to port-forward to (default ``platform-agent``).
    AGENT_NAMESPACE: Namespace of the service (default ``default``).
    AGENT_PORT: Remote service port (defaults to ``AGENT_LOCAL_PORT``).
    AGENT_CLUSTER_CONTEXT: Optional kubectl context for the port-forward.
    AGENT_MODEL_NAME: ``model`` field sent to the endpoint (default
        ``hermes-agent``).
    AGENT_CONVERSATION_ID: ``conversation`` field sent to the endpoint.
    AGENT_HTTP_TIMEOUT: Request timeout in seconds (default ``600``).
    PLATFORM_AGENT_TOKEN: Bearer token for the endpoint.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from devops_bench.agents import AGENTS, AgentHarness, AgentResult, ToolCall
from devops_bench.core.errors import AlreadyRegisteredError

__all__ = ["KubeAgentsHarness"]

_log = logging.getLogger("kube_agents_bench.harness")

# Background kubectl port-forward shared by every run in this process.
_PF_PROCESS: subprocess.Popen[bytes] | None = None


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    """Return True when ``host:port`` accepts a TCP connection."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            sock.connect((host, port))
            return True
    except OSError:
        return False


def _cleanup_port_forward() -> None:
    global _PF_PROCESS
    if _PF_PROCESS is not None:
        _log.info("terminating agent port-forward")
        _PF_PROCESS.terminate()
        _PF_PROCESS.wait()
        _PF_PROCESS = None


def _ensure_port_forward(local_port: int) -> None:
    """Lazily start a background ``kubectl port-forward`` if the port is closed.

    When the port is already open (an externally-established forward, an
    in-cluster run, or a test stub server) this is a no-op -- the harness never
    assumes it owns the transport.

    Raises:
        RuntimeError: If the spawned port-forward exits immediately.
    """
    global _PF_PROCESS
    if _port_open(local_port):
        return

    service = os.environ.get("AGENT_SERVICE_NAME", "platform-agent")
    namespace = os.environ.get("AGENT_NAMESPACE", "default")
    remote_port = os.environ.get("AGENT_PORT", str(local_port))
    context = os.environ.get("AGENT_CLUSTER_CONTEXT")

    cmd = [
        "kubectl",
        "port-forward",
        f"svc/{service}",
        f"{local_port}:{remote_port}",
        "-n",
        namespace,
    ]
    if context:
        cmd.extend(["--context", context])

    _log.info("port %d closed; establishing port-forward to svc/%s", local_port, service)
    stderr_log = Path(tempfile.gettempdir()) / f"kubeagents-pf-{local_port}.log"
    with open(stderr_log, "wb") as log_file:
        _PF_PROCESS = subprocess.Popen(cmd, stdout=log_file, stderr=log_file)
    atexit.register(_cleanup_port_forward)

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if _PF_PROCESS.poll() is not None:
            raise RuntimeError(
                f"kubectl port-forward exited with {_PF_PROCESS.returncode}; "
                f"see {stderr_log}"
            )
        if _port_open(local_port):
            _log.info("port-forward established on port %d", local_port)
            return
        time.sleep(0.5)
    raise RuntimeError(f"port-forward did not open port {local_port} in time; see {stderr_log}")


def _parse_response(payload: dict[str, Any]) -> AgentResult:
    """Map a Responses-style payload onto the canonical ``AgentResult``.

    ``output`` items of type ``message`` contribute assistant text;
    ``function_call`` / ``function_call_output`` items become canonical
    :class:`ToolCall` trajectory entries so metrics consume one schema.
    A ``function_call_output`` carries no ``name`` of its own -- the platform
    agent correlates it to its call via ``call_id`` -- so the tool name is
    resolved from the matching ``function_call`` entry.

    The stateful response ``id`` (and ``status``/``model``) are preserved in
    ``metadata`` so a benchmark artifact can be joined back to the agent's
    full execution trajectory via ``GET /v1/responses/<id>``.
    """
    output_text = ""
    trajectory: list[dict[str, Any]] = []
    tools_used: dict[str, int] = {}
    call_names: dict[str, str] = {}

    for part in payload.get("output", []):
        part_type = part.get("type")
        if part_type == "message" and part.get("role") == "assistant":
            for chunk in part.get("content", []):
                if chunk.get("type") == "output_text":
                    output_text += chunk.get("text", "")
        elif part_type == "function_call":
            args = part.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"raw": args}
            name = part.get("name", "")
            call_id = part.get("call_id")
            if call_id:
                call_names[call_id] = name
            trajectory.append(ToolCall(name=name, args=args or {}).to_dict())
            tools_used[name] = tools_used.get(name, 0) + 1
        elif part_type == "function_call_output":
            name = part.get("name") or call_names.get(part.get("call_id", ""), "")
            trajectory.append(
                ToolCall(
                    name=name,
                    args={},
                    result=part.get("output"),
                    status="completed",
                ).to_dict()
            )

    usage = payload.get("usage", {})
    tokens = {
        "input": usage.get("input_tokens", 0),
        "output": usage.get("output_tokens", 0),
        "total": usage.get("total_tokens", 0),
    }
    metadata: dict[str, Any] = {"tools": tools_used}
    for key in ("id", "status", "model"):
        if payload.get(key) is not None:
            metadata[f"response_{key}"] = payload[key]
    return AgentResult(
        output=output_text,
        trajectory=trajectory,
        tokens=tokens,
        metadata=metadata,
    )


class KubeAgentsHarness(AgentHarness):
    """Drives the in-cluster platform agent over its HTTP endpoint.

    ``_execute`` handles its *known* failure modes (HTTP errors, unreachable
    endpoint, malformed JSON) by returning an ``AgentResult`` with ``errors``
    populated; the base class's safety net covers anything unexpected.
    """

    def _execute(self, prompt: str, workspace_path: Path | None = None) -> AgentResult:
        local_port = int(os.environ.get("AGENT_LOCAL_PORT", "8642"))
        api_path = os.environ.get("AGENT_API_PATH", "/v1/responses")
        timeout = float(os.environ.get("AGENT_HTTP_TIMEOUT", "600"))

        try:
            _ensure_port_forward(local_port)
        except RuntimeError as exc:
            return AgentResult.errored(str(exc))

        url = f"http://localhost:{local_port}{api_path}"
        body = {
            "model": os.environ.get("AGENT_MODEL_NAME", "hermes-agent"),
            "conversation": os.environ.get("AGENT_CONVERSATION_ID", "devops-bench-session"),
            "input": prompt,
        }
        headers = {"Content-Type": "application/json"}
        token = os.environ.get("PLATFORM_AGENT_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        request = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(detail).get("error", {}).get("message", detail)
            except (json.JSONDecodeError, AttributeError):
                pass
            return AgentResult.errored(f"HTTP {exc.code} from agent endpoint: {detail}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            return AgentResult.errored(f"{type(exc).__name__}: {exc}")

        return _parse_response(payload)


# Register on import so the interim driver path (`import kube_agents_bench`)
# works against library versions without agent entry-point discovery. Guarded
# with try/except rather than a membership test: `in AGENTS` would trigger the
# registry's one-time entry-point scan, and when THIS import is itself that
# scan loading the `kubeagents` entry point, re-entering the scan would
# deadlock on the registry lock. `register` never scans, and the scan skips
# names that are already registered, so this is safe in every ordering.
try:
    AGENTS.register("kubeagents")(KubeAgentsHarness)
except AlreadyRegisteredError:  # pragma: no cover - depends on import order
    pass
