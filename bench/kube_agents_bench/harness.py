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

Registration is entirely declarative: the ``devops_bench.agents`` entry point
in ``pyproject.toml`` is the only path, so importing this module has no side
effects and the stock ``devops-bench`` CLI resolves ``--agent-type kubeagents``
without importing anything from this package.

Environment:
    AGENT_LOCAL_PORT: Local port for the agent endpoint (default ``8642``).
    AGENT_API_PATH: Request path (default ``/v1/responses``).
    AGENT_SERVICE_NAME: Service to port-forward to (default ``platform-agent``).
    AGENT_NAMESPACE: Namespace of the service (default ``kubeagents-system``,
        where deploy/kustomize/platform installs it; CI overrides this with the
        per-PR target namespace).
    AGENT_PORT: Remote service port (defaults to ``AGENT_LOCAL_PORT``).
    AGENT_CLUSTER_CONTEXT: Optional kubectl context for the port-forward.
    AGENT_MODEL_NAME: ``model`` field sent to the endpoint (default
        ``hermes-agent``).
    AGENT_CONVERSATION_ID: Pins the ``conversation`` field. Unset (the
        default) generates a fresh id per invocation so each task's
        trajectory is isolated on this stateful endpoint.
    AGENT_HTTP_TIMEOUT: Request timeout in seconds (default ``600``).
    PLATFORM_AGENT_TOKEN: Bearer token for the endpoint.
"""

from __future__ import annotations

import atexit
import http.client
import json
import logging
import os
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from devops_bench.agents import AgentHarness, AgentResult, ToolCall
from devops_bench.agents.result import empty_tokens

__all__ = ["KubeAgentsHarness"]

_log = logging.getLogger("kube_agents_bench.harness")

# Background kubectl port-forwards owned by this process, keyed by local port.
# Guarded by _PF_LOCK: parallel evaluations may drive agents concurrently.
_PF_PROCESSES: dict[int, subprocess.Popen[bytes]] = {}
_PF_LOCK = threading.Lock()

# One private mode-0700 log directory per process (created under _PF_LOCK): a
# predictable path in shared /tmp would be open to symlink redirection by
# other local users, and a directory per spawn would leak one tempdir per
# reconnect.
_PF_LOG_DIR: Path | None = None


def _pf_log_dir() -> Path:
    global _PF_LOG_DIR
    if _PF_LOG_DIR is None:
        _PF_LOG_DIR = Path(tempfile.mkdtemp(prefix="kubeagents-pf-"))
    return _PF_LOG_DIR


def _stop_process(proc: subprocess.Popen[bytes]) -> None:
    """Terminate ``proc``, escalating to SIGKILL if it ignores SIGTERM."""
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    """Return True when ``host:port`` accepts a TCP connection."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            sock.connect((host, port))
            return True
    except OSError:
        return False


def _cleanup_port_forwards() -> None:
    """Terminate every port-forward this process spawned (atexit hook)."""
    with _PF_LOCK:
        for port, proc in list(_PF_PROCESSES.items()):
            if proc.poll() is None:
                _log.info("terminating agent port-forward on port %d", port)
            _stop_process(proc)
            del _PF_PROCESSES[port]


# Registered once at import time; per-spawn registration would stack a
# duplicate handler on every reconnect.
atexit.register(_cleanup_port_forwards)


def _ensure_port_forward(local_port: int) -> None:
    """Lazily start a background ``kubectl port-forward`` if the port is closed.

    When the port is already open (an externally-established forward, an
    in-cluster run, or a test stub server) this is a no-op -- the harness never
    assumes it owns the transport. A previously spawned forward for the same
    port that has died is reaped before a replacement is spawned.

    Raises:
        RuntimeError: If the spawned port-forward exits immediately or the
            port does not open in time.
    """
    with _PF_LOCK:
        if _port_open(local_port):
            return

        # Reap a dead forward for this port before replacing it.
        stale = _PF_PROCESSES.pop(local_port, None)
        if stale is not None:
            _stop_process(stale)

        service = os.environ.get("AGENT_SERVICE_NAME", "platform-agent")
        namespace = os.environ.get("AGENT_NAMESPACE", "kubeagents-system")
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
        stderr_log = _pf_log_dir() / f"pf-{local_port}.log"
        with open(stderr_log, "wb") as log_file:
            try:
                proc = subprocess.Popen(cmd, stdout=log_file, stderr=log_file)
            except OSError as exc:
                # A missing/broken kubectl is a known failure mode, not a crash:
                # surface it through the same RuntimeError path as a dead
                # forward so _execute converts it into AgentResult.errored.
                raise RuntimeError(f"failed to spawn kubectl port-forward: {exc}") from exc
        _PF_PROCESSES[local_port] = proc

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"kubectl port-forward exited with {proc.returncode}; see {stderr_log}"
                )
            if _port_open(local_port):
                _log.info("port-forward established on port %d", local_port)
                return
            time.sleep(0.5)
        raise RuntimeError(
            f"port-forward did not open port {local_port} in time; see {stderr_log}"
        )


def _parse_response(payload: dict[str, Any]) -> AgentResult:
    """Map a Responses-style payload onto the canonical ``AgentResult``.

    ``output`` items of type ``message`` contribute assistant text, and each
    ``function_call`` becomes one canonical :class:`ToolCall`. A
    ``function_call_output`` is *folded into* its originating call -- filling
    ``result`` and flipping ``status`` to ``completed`` -- rather than appended
    as a second entry, matching how every builtin harness emits trajectories
    (see ``agents/api/agent.py`` and the CLI parsers). Emitting the output as
    its own entry makes trajectory metrics read a redundant argument-less call
    and penalise the agent for a tool invocation it never made.

    Outputs carry no ``name`` of their own -- the platform agent correlates
    them to their call via ``call_id`` -- so the fold is keyed on ``call_id``,
    falling back to the most recent unresolved call when the id is absent.

    The stateful response ``id`` (and ``status``/``model``) are preserved in
    ``metadata`` so a benchmark artifact can be joined back to the agent's
    full execution trajectory via ``GET /v1/responses/<id>``.
    """
    output_text = ""
    trajectory: list[ToolCall] = []
    tools_used: dict[str, int] = {}
    calls_by_id: dict[str, ToolCall] = {}
    unkeyed_calls: deque[ToolCall] = deque()
    orphan_errors: list[str] = []

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
            if not isinstance(args, dict):
                # ToolCall.args is a mapping; wrap scalar/list arguments.
                args = {} if args is None else {"raw": args}
            name = part.get("name", "")
            entry = ToolCall(name=name, args=args)
            trajectory.append(entry)
            tools_used[name] = tools_used.get(name, 0) + 1
            call_id = part.get("call_id")
            if call_id:
                calls_by_id[str(call_id)] = entry
            else:
                unkeyed_calls.append(entry)
        elif part_type == "function_call_output":
            call_id = part.get("call_id")
            target = calls_by_id.pop(str(call_id), None) if call_id else None
            if target is None and unkeyed_calls:
                target = unkeyed_calls.popleft()
            if target is None:
                # No matching call: keep the payload rather than drop it, and
                # record the anomaly so it is visible in the result.
                orphan_errors.append(f"tool output without a matching call (call_id={call_id!r})")
                target = ToolCall(name=part.get("name", ""), args={})
                trajectory.append(target)
            target.result = part.get("output")
            target.status = "completed"

    # Canonical bucket shape from the library (all-None = "unavailable"), so
    # an endpoint that omits usage is not conflated with a measured zero.
    usage = payload.get("usage", {})
    tokens = empty_tokens()
    for bucket, key in (
        ("input", "input_tokens"),
        ("output", "output_tokens"),
        ("total", "total_tokens"),
    ):
        if usage.get(key) is not None:
            tokens[bucket] = usage[key]
    metadata: dict[str, Any] = {"tools": tools_used}
    for key in ("id", "status", "model"):
        if payload.get(key) is not None:
            metadata[f"response_{key}"] = payload[key]
    return AgentResult(
        output=output_text,
        trajectory=[entry.to_dict() for entry in trajectory],
        tokens=tokens,
        errors=orphan_errors,
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

        # 127.0.0.1, matching _port_open's probe host: with "localhost" a
        # forward bound only to ::1 would probe closed yet serve the request,
        # or probe open (v4) while the request resolves to v6 and fails.
        url = f"http://127.0.0.1:{local_port}{api_path}"
        # A fresh conversation per invocation. The endpoint is stateful and
        # replays the whole conversation's tool calls in every response, so a
        # shared id makes each task inherit the previous task's trajectory
        # (measured: 1 -> 2 -> 4 calls over three turns) and silently corrupts
        # trajectory-based scoring. AGENT_CONVERSATION_ID still pins the id
        # when continuity is wanted.
        conversation = os.environ.get("AGENT_CONVERSATION_ID") or (
            f"devops-bench-{uuid.uuid4().hex[:12]}"
        )
        body = {
            "model": os.environ.get("AGENT_MODEL_NAME", "hermes-agent"),
            "conversation": conversation,
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
        except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
            # OSError covers URLError, timeouts, and connection resets;
            # HTTPException covers a mid-read protocol failure (IncompleteRead
            # and friends). All are known transport failures, not agent bugs.
            return AgentResult.errored(f"{type(exc).__name__}: {exc}")

        if not isinstance(payload, dict):
            return AgentResult.errored(
                f"agent endpoint returned non-object JSON: {type(payload).__name__}"
            )
        return _parse_response(payload)
