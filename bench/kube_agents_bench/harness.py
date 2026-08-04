"""``kubeagents`` agent harness: HTTP transport to the in-cluster platform agent.

The agent runs inside the cluster, so this harness only ensures the service is
reachable on a local port (lazily spawning ``kubectl port-forward``), POSTs the
prompt to its Responses-style endpoint, and parses the reply into an
``AgentResult``. No model SDK is imported; all inference happens in the cluster.

Registration is the ``devops_bench.agents`` entry point in ``pyproject.toml``,
so importing this module has no side effects.

Environment:
    AGENT_LOCAL_PORT: Local side of the port-forward (default ``8642``). The
        remote side is always the Service's 8642 and is not configurable.
    AGENT_API_PATH: Request path (default ``/v1/responses``).
    AGENT_SERVICE_NAME: Service to port-forward to (default ``platform-agent``).
    AGENT_NAMESPACE: Namespace of the service (default ``kubeagents-system``).
    AGENT_CLUSTER_CONTEXT: Optional kubectl context for the port-forward.
    AGENT_MODEL_NAME: ``model`` field sent to the endpoint (default ``hermes-agent``).
    AGENT_CONVERSATION_ID: Pins the ``conversation`` field. Unset (the default)
        generates a fresh id per invocation so each task's trajectory is
        isolated on this stateful endpoint.
    AGENT_HTTP_TIMEOUT: Request timeout in seconds (default ``600``).
    PLATFORM_AGENT_TOKEN: Bearer token for the endpoint.
"""

from __future__ import annotations

import atexit
import http.client
import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from devops_bench.agents import AgentHarness, AgentResult, ToolCall
from devops_bench.agents.result import empty_tokens

__all__ = ["KubeAgentsHarness"]

_log = logging.getLogger("kube_agents_bench.harness")

SERVICE_API_PORT = 8642

_PF_LOCK = threading.Lock()  # guards the three registries below
_PF_PROCESSES: dict[int, subprocess.Popen[bytes]] = {}
_PF_PORT_LOCKS: dict[int, threading.Lock] = {}
_PF_LOG_DIR: Path | None = None


def _port_establishment_lock(port: int) -> threading.Lock:
    with _PF_LOCK:
        return _PF_PORT_LOCKS.setdefault(port, threading.Lock())


def _pf_log_dir() -> Path:
    global _PF_LOG_DIR
    with _PF_LOCK:
        if _PF_LOG_DIR is None:
            _PF_LOG_DIR = Path(tempfile.mkdtemp(prefix="kubeagents-pf-"))
        return _PF_LOG_DIR


def _tail(path: Path, max_bytes: int = 2048) -> str:
    """Last ``max_bytes`` of ``path``. Errors embed this rather than pointing at
    the file, which is deleted at process exit."""
    try:
        data = path.read_bytes()[-max_bytes:]
        return data.decode("utf-8", errors="replace").strip() or "(no output)"
    except OSError:
        return "(log unavailable)"


def _stop_process(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            sock.connect((host, port))
            return True
    except OSError:
        return False


@atexit.register
def _cleanup_port_forwards() -> None:
    """Terminate every port-forward this process spawned."""
    global _PF_LOG_DIR
    with _PF_LOCK:
        while _PF_PROCESSES:
            port, proc = _PF_PROCESSES.popitem()
            if proc.poll() is None:
                _log.info("terminating agent port-forward on port %d", port)
            _stop_process(proc)
        if _PF_LOG_DIR is not None:
            shutil.rmtree(_PF_LOG_DIR, ignore_errors=True)
            _PF_LOG_DIR = None


def _port_forward_command(local_port: int) -> list[str]:
    service = os.environ.get("AGENT_SERVICE_NAME", "platform-agent")
    cmd = [
        "kubectl",
        "port-forward",
        f"svc/{service}",
        f"{local_port}:{SERVICE_API_PORT}",
        "-n",
        os.environ.get("AGENT_NAMESPACE", "kubeagents-system"),
    ]
    context = os.environ.get("AGENT_CLUSTER_CONTEXT")
    if context:
        cmd.extend(["--context", context])
    return cmd


def _ensure_port_forward(local_port: int) -> None:
    """Start a background ``kubectl port-forward`` if the port is closed.

    An already-open port is a no-op: the harness never assumes it owns the
    transport. Serialised per port, so different ports establish in parallel.

    Raises:
        RuntimeError: The forward exited immediately, could not be spawned, or
            the port did not open in time.
    """
    with _port_establishment_lock(local_port):
        if _port_open(local_port):
            return

        with _PF_LOCK:
            stale = _PF_PROCESSES.pop(local_port, None)
        if stale is not None:
            _stop_process(stale)

        cmd = _port_forward_command(local_port)
        _log.info("port %d closed; establishing port-forward: %s", local_port, " ".join(cmd))
        stderr_log = _pf_log_dir() / f"pf-{local_port}.log"
        try:
            with open(stderr_log, "wb") as log_file:
                proc = subprocess.Popen(cmd, stdout=log_file, stderr=log_file)
        except OSError as exc:
            # A missing kubectl reaches _execute as a known error, not a crash.
            raise RuntimeError(f"failed to spawn kubectl port-forward: {exc}") from exc
        with _PF_LOCK:
            _PF_PROCESSES[local_port] = proc

        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError(
                        f"kubectl port-forward exited with {proc.returncode}: {_tail(stderr_log)}"
                    )
                if _port_open(local_port):
                    _log.info("port-forward established on port %d", local_port)
                    return
                time.sleep(0.5)
            raise RuntimeError(
                f"port-forward did not open port {local_port} in time: {_tail(stderr_log)}"
            )
        except BaseException:
            with _PF_LOCK:
                _PF_PROCESSES.pop(local_port, None)
            _stop_process(proc)
            raise


_TOOL_ERROR_PREFIX = "Error executing tool"


def _output_text(output: Any) -> str:
    """Flatten a ``function_call_output.output`` into text.

    The streaming builder wraps the same payload in text blocks; accept both so
    ``ToolCall.result`` does not depend on which builder served the request.
    """
    if isinstance(output, str):
        return output
    if output is None:
        return ""
    if isinstance(output, list):
        parts: list[str] = []
        for block in output:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return json.dumps(output, default=str)


def _output_failed(text: str) -> bool:
    """Return True for a *structured* hermes tool failure.

    Free text is never sniffed: tool output legitimately contains the word
    "error" (a test log, a grep hit).
    """
    if text.startswith(_TOOL_ERROR_PREFIX):
        return True
    try:
        data = json.loads(text.strip())
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    if data.get("success") is False or data.get("ok") is False:
        return True
    exit_code = data.get("exit_code", data.get("returncode"))
    if isinstance(exit_code, int) and exit_code != 0:
        return True
    # tool_error() / MCP isError: a bare error string with no result payload.
    # An error beside a real payload is a diagnostic, so every key the endpoint
    # uses to carry one counts -- MCP's `content`, and hermes' own `result` /
    # `structuredContent`.
    return bool(data.get("error")) and not (
        data.get("content") or data.get("result") or data.get("structuredContent")
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses every redirect, turning it into an ``HTTPError`` instead.

    urllib follows redirects by default and, unlike requests, does not strip
    ``Authorization`` on a cross-host hop -- so one ``302`` from whatever
    answers on the local port would hand the bearer token to another origin,
    defeating the ``AGENT_API_PATH`` check. A port-forward to a fixed local
    port has no legitimate reason to redirect.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)

_SESSION_ID_HEADER = "X-Hermes-Session-Id"

# The session lookup only refines accounting, so it never inherits the agent's
# (minutes-long) budget: a hung route would be billed as the agent's latency.
_SESSION_LOOKUP_TIMEOUT = 15.0

_SESSION_TOKEN_KEYS = (
    ("input", "input_tokens"),
    ("cached", "cache_read_tokens"),
    ("cache_write", "cache_write_tokens"),
    ("reasoning", "reasoning_tokens"),
    ("output", "output_tokens"),
)

_ENVELOPE_TOKEN_KEYS = (
    ("input", "input_tokens"),
    ("output", "output_tokens"),
    ("total", "total_tokens"),
)


def _canonical_session_tokens(
    tokens: dict[str, Any],
    session_id: str,
    local_port: int,
    headers: dict[str, str],
    timeout: float,
) -> None:
    """Replace the envelope's counts with the session row's canonical split.

    The envelope reports hermes' ``prompt_tokens`` (input + cache_read +
    cache_write), while ``TOKEN_BUCKETS`` defines ``input`` as the non-cached
    prompt alone -- so the row replaces the envelope wholesale rather than
    merging, and a partial row is discarded. Best effort: any failure leaves
    the envelope's counts in place.
    """
    quoted = urllib.parse.quote(session_id, safe="")
    probe = urllib.request.Request(
        f"http://127.0.0.1:{local_port}/api/sessions/{quoted}", headers=headers, method="GET"
    )
    try:
        with _OPENER.open(probe, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, http.client.HTTPException, ValueError) as exc:
        _log.debug("session token lookup failed for %s: %s", session_id, exc)
        return

    session = body.get("session") if isinstance(body, dict) else None
    if not isinstance(session, dict):
        return
    counts: dict[str, int] = {}
    for bucket, key in _SESSION_TOKEN_KEYS:
        value = session.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            return
        counts[bucket] = value
    tokens.update(counts)
    tokens["total"] = sum(counts.values())


def _envelope_tokens(payload: dict[str, Any]) -> dict[str, Any]:
    """Token buckets from the response envelope; an omitted one stays ``None``."""
    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    tokens = empty_tokens()
    for bucket, key in _ENVELOPE_TOKEN_KEYS:
        if usage.get(key) is not None:
            tokens[bucket] = usage[key]
    return tokens


def _call_args(raw: Any) -> dict[str, Any]:
    """Coerce ``function_call.arguments`` into the mapping ``ToolCall`` wants."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}
    if isinstance(raw, dict):
        return raw
    return {} if raw is None else {"raw": raw}


def _tool_name(raw: Any) -> str:
    """Coerce a reported tool name to a string.

    It becomes ``ToolCall.name`` and a key of ``metadata['tools']``, so an
    unhashable one would otherwise raise out of a parser that promises to
    degrade.
    """
    if isinstance(raw, str):
        return raw
    return "" if raw is None else str(raw)


def _message_text(part: dict[str, Any]) -> str:
    content = part.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(
        chunk["text"]
        for chunk in content
        if isinstance(chunk, dict)
        and chunk.get("type") == "output_text"
        and isinstance(chunk.get("text"), str)
    )


def _parse_response(payload: dict[str, Any]) -> AgentResult:
    """Map a Responses-style payload onto the canonical ``AgentResult``.

    A ``function_call_output`` is folded *into* its originating call -- filling
    in ``result`` and ``status`` -- rather than appended as a second entry,
    which trajectory metrics would score as a redundant argument-less call. The
    fold is keyed on ``call_id``, falling back to the oldest unresolved call
    only when the id is absent: an id matching nothing is an orphan, not a
    licence to consume an unrelated call. Outputs carry no status, so failure
    is read out of the payload (:func:`_output_failed`).

    Shape anomalies degrade rather than raise -- whatever was readable is kept,
    with the anomaly on ``errors``.
    """
    output_text = ""
    trajectory: list[ToolCall] = []
    tools_used: dict[str, int] = {}
    calls_by_id: dict[str, ToolCall] = {}
    unkeyed_calls: deque[ToolCall] = deque()
    parse_errors: list[str] = []

    items = payload.get("output")
    if not isinstance(items, list):
        if items is not None:
            parse_errors.append(f"'output' is not a list (got {type(items).__name__})")
        items = []

    for part in items:
        if not isinstance(part, dict):
            parse_errors.append(f"skipped a non-object output item ({type(part).__name__})")
            continue
        part_type = part.get("type")

        if part_type == "message" and part.get("role") == "assistant":
            output_text += _message_text(part)

        elif part_type == "function_call":
            name = _tool_name(part.get("name"))
            entry = ToolCall(name=name, args=_call_args(part.get("arguments")))
            trajectory.append(entry)
            tools_used[name] = tools_used.get(name, 0) + 1
            call_id = part.get("call_id")
            if call_id:
                calls_by_id[str(call_id)] = entry
            else:
                unkeyed_calls.append(entry)

        elif part_type == "function_call_output":
            call_id = part.get("call_id")
            if call_id:
                target = calls_by_id.pop(str(call_id), None)
            else:
                target = unkeyed_calls.popleft() if unkeyed_calls else None
            if target is None:
                parse_errors.append(f"tool output without a matching call (call_id={call_id!r})")
                target = ToolCall(name=_tool_name(part.get("name")), args={})
                trajectory.append(target)
            text = _output_text(part.get("output"))
            target.result = text
            target.status = "error" if _output_failed(text) else "completed"

    metadata: dict[str, Any] = {"tools": tools_used}
    for key in ("id", "status", "model"):
        if payload.get(key) is not None:
            metadata[f"response_{key}"] = payload[key]

    return AgentResult(
        output=output_text,
        trajectory=[entry.to_dict() for entry in trajectory],
        tokens=_envelope_tokens(payload),
        errors=parse_errors,
        metadata=metadata,
    )


def _numeric_env(name: str, default: str, cast: Any) -> Any:
    """Read a numeric env var, naming it in the error rather than the value."""
    raw = os.environ.get(name, default)
    try:
        return cast(raw)
    except ValueError:
        raise ValueError(f"{name} must be numeric, got {raw!r}") from None


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    detail = exc.read().decode("utf-8", errors="replace")
    try:
        return json.loads(detail).get("error", {}).get("message", detail)
    except (json.JSONDecodeError, AttributeError):
        return detail


class KubeAgentsHarness(AgentHarness):
    """Drives the in-cluster platform agent over its HTTP endpoint.

    Known failure modes (HTTP errors, unreachable endpoint, malformed JSON)
    return an ``AgentResult`` with ``errors`` populated; the base class's safety
    net covers anything unexpected.
    """

    def _execute(self, prompt: str, workspace_path: Path | None = None) -> AgentResult:
        api_path = os.environ.get("AGENT_API_PATH", "/v1/responses")
        try:
            local_port = _numeric_env("AGENT_LOCAL_PORT", str(SERVICE_API_PORT), int)
            timeout = _numeric_env("AGENT_HTTP_TIMEOUT", "600", float)
        except ValueError as exc:
            return AgentResult.errored(str(exc))

        # "@evil.example/..." would make 127.0.0.1:<port> the userinfo of
        # another host and send the bearer token there.
        if not api_path.startswith("/"):
            return AgentResult.errored(f"AGENT_API_PATH must start with '/': {api_path!r}")

        try:
            _ensure_port_forward(local_port)
        except RuntimeError as exc:
            return AgentResult.errored(str(exc))

        # 127.0.0.1 rather than localhost, matching _port_open's probe host: a
        # v4/v6 mismatch would make the probe and the request disagree.
        url = f"http://127.0.0.1:{local_port}{api_path}"
        headers = {"Content-Type": "application/json"}
        token = os.environ.get("PLATFORM_AGENT_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = {
            "model": os.environ.get("AGENT_MODEL_NAME", "hermes-agent"),
            # The endpoint is stateful and replays the whole conversation's tool
            # calls, so a shared id would make each task inherit the previous
            # task's trajectory and corrupt trajectory scoring.
            "conversation": os.environ.get("AGENT_CONVERSATION_ID")
            or f"devops-bench-{uuid.uuid4().hex[:12]}",
            "input": prompt,
        }

        request = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with _OPENER.open(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
                session_id = response.headers.get(_SESSION_ID_HEADER, "")
        except urllib.error.HTTPError as exc:
            return AgentResult.errored(
                f"HTTP {exc.code} from agent endpoint: {_http_error_detail(exc)}"
            )
        except (OSError, http.client.HTTPException, ValueError) as exc:
            # URLError, timeouts, resets, a mid-read protocol failure, and a
            # body that is neither UTF-8 nor JSON: transport, not agent, bugs.
            return AgentResult.errored(f"{type(exc).__name__}: {exc}")

        if not isinstance(payload, dict):
            return AgentResult.errored(
                f"agent endpoint returned non-object JSON: {type(payload).__name__}"
            )
        result = _parse_response(payload)
        if session_id:
            result.metadata["session_id"] = session_id
            _canonical_session_tokens(
                result.tokens,
                session_id,
                local_port,
                headers,
                min(timeout, _SESSION_LOOKUP_TIMEOUT),
            )
        return result
