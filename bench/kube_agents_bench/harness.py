"""``kubeagents`` agent harness: HTTP transport to the in-cluster platform agent.

A port of the legacy ``pkg/agents/runner/kubeagents.py`` evaluator runner onto
devops-bench's :class:`~devops_bench.agents.AgentHarness` contract. The agent
itself runs inside the cluster; this harness only:

1. ensures the agent service is reachable on a local port (lazily spawning a
   background ``kubectl port-forward`` when it is not),
2. POSTs the task prompt to the agent's Responses-style HTTP endpoint, and
3. parses the response into the canonical :class:`AgentResult` shape
   (final text, tool-call trajectory, token usage), replacing the envelope's
   token counts with the session row's canonical split when it is reachable
   (:func:`_canonical_session_tokens`).

No model SDK is imported here -- all inference happens in the cluster, which
is why this package needs only the ``devops_bench.agents`` extension axis and
not the ``MODELS`` registry.

Registration is entirely declarative: the ``devops_bench.agents`` entry point
in ``pyproject.toml`` is the only path, so importing this module has no side
effects and the stock ``devops-bench`` CLI resolves ``--agent-type kubeagents``
without importing anything from this package.

Environment:
    AGENT_LOCAL_PORT: Local side of the port-forward -- the port the harness
        connects to on 127.0.0.1 (default ``8642``). Set it when the agent is
        already reachable locally, or when 8642 is taken. The remote side is
        always the Service's 8642 and is not configurable.
    AGENT_API_PATH: Request path (default ``/v1/responses``).
    AGENT_SERVICE_NAME: Service to port-forward to (default ``platform-agent``).
    AGENT_NAMESPACE: Namespace of the service (default ``kubeagents-system``,
        where deploy/kustomize/platform installs it; CI overrides this with the
        per-PR target namespace).
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

# Background kubectl port-forwards owned by this process, keyed by local port.
_PF_PROCESSES: dict[int, subprocess.Popen[bytes]] = {}

# Guards the registries below
_PF_LOCK = threading.Lock()

# One establishment lock per local port
_PF_PORT_LOCKS: dict[int, threading.Lock] = {}

# One private mode-0700 log directory per process
_PF_LOG_DIR: Path | None = None


def _port_establishment_lock(port: int) -> threading.Lock:
    """Return ``port``'s establishment lock, creating it on first use."""
    with _PF_LOCK:
        return _PF_PORT_LOCKS.setdefault(port, threading.Lock())


def _pf_log_dir() -> Path:
    """Return the process-wide port-forward log directory, creating it once."""
    global _PF_LOG_DIR
    with _PF_LOCK:
        if _PF_LOG_DIR is None:
            _PF_LOG_DIR = Path(tempfile.mkdtemp(prefix="kubeagents-pf-"))
        return _PF_LOG_DIR


def _tail(path: Path, max_bytes: int = 2048) -> str:
    """Last ``max_bytes`` of ``path``, for embedding kubectl stderr in errors.

    Error messages must be self-contained: the log directory is deleted at
    process exit, so a "see <path>" pointer would dangle in results.json.
    """
    try:
        data = path.read_bytes()[-max_bytes:]
        return data.decode("utf-8", errors="replace").strip() or "(no output)"
    except OSError:
        return "(log unavailable)"


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
    global _PF_LOG_DIR
    with _PF_LOCK:
        for port, proc in list(_PF_PROCESSES.items()):
            if proc.poll() is None:
                _log.info("terminating agent port-forward on port %d", port)
            _stop_process(proc)
            del _PF_PROCESSES[port]
        if _PF_LOG_DIR is not None:
            # Safe to delete: error paths embed the relevant log tail in the
            # message rather than pointing at these files.
            shutil.rmtree(_PF_LOG_DIR, ignore_errors=True)
            _PF_LOG_DIR = None


# Registered once at import time; per-spawn registration would stack a
# duplicate handler on every reconnect.
atexit.register(_cleanup_port_forwards)


def _ensure_port_forward(local_port: int) -> None:
    """Lazily start a background ``kubectl port-forward`` if the port is closed.

    When the port is already open (an externally-established forward, an
    in-cluster run, or a test stub server) this is a no-op -- the harness never
    assumes it owns the transport. A previously spawned forward for the same
    port that has died is reaped before a replacement is spawned.

    Serialised on ``local_port`` only, so different ports establish in
    parallel. A forward that fails to come up is torn down before raising.

    Raises:
        RuntimeError: If the spawned port-forward exits immediately or the
            port does not open in time.
    """
    with _port_establishment_lock(local_port):
        if _port_open(local_port):
            return

        # Reap a dead forward for this port before replacing it
        with _PF_LOCK:
            stale = _PF_PROCESSES.pop(local_port, None)
        if stale is not None:
            _stop_process(stale)

        service = os.environ.get("AGENT_SERVICE_NAME", "platform-agent")
        namespace = os.environ.get("AGENT_NAMESPACE", "kubeagents-system")
        remote_port = SERVICE_API_PORT
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


# Failure is read out of the payload, porting hermes' own _tool_result_failed
_TOOL_ERROR_PREFIX = "Error executing tool"


def _output_text(output: Any) -> str:
    """Flatten a ``function_call_output.output`` into text.

    The streaming builder wraps the same payload in ``input_text`` blocks;
    accept both so ``ToolCall.result`` does not depend on which one served
    the request.
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
    """Return True when ``text`` is a *structured* hermes tool failure.

    Only structured failure counts: tool output legitimately contains the word
    "error" (a test log, a grep hit), so free text is never sniffed.
    """
    if text.startswith(_TOOL_ERROR_PREFIX):
        return True
    try:
        data = json.loads(text.strip())
    except ValueError:  # non-JSON output is not a failure
        return False
    if not isinstance(data, dict):
        return False
    if data.get("success") is False or data.get("ok") is False:
        return True
    exit_code = data.get("exit_code", data.get("returncode"))
    if isinstance(exit_code, int) and exit_code != 0:
        return True
    # tool_error() / MCP isError: a bare error string with no result payload.
    return bool(data.get("error")) and not data.get("content")


_SESSION_ID_HEADER = "X-Hermes-Session-Id"

# Every canonical bucket: only the row carries the split (turn_finalizer.py:531)
_SESSION_TOKEN_KEYS = (
    ("input", "input_tokens"),
    ("cached", "cache_read_tokens"),
    ("cache_write", "cache_write_tokens"),
    ("reasoning", "reasoning_tokens"),
    ("output", "output_tokens"),
)


def _canonical_session_tokens(
    tokens: dict[str, Any],
    session_id: str,
    local_port: int,
    headers: dict[str, str],
    timeout: float,
) -> None:
    """Replace the envelope's counts with the session row's canonical ones.

    ``TOKEN_BUCKETS`` defines ``input`` as the *non-cached* prompt and ``total``
    as the sum of every bucket. The envelope reports hermes' ``prompt_tokens``,
    which is ``input + cache_read + cache_write`` (usage_pricing.py:44), so
    adding cache counts beside it would count them twice. Only the row splits
    them out (conversation_loop.py:2278), so it replaces the envelope's counts
    wholesale -- and a partial row is discarded, since half of each accounting
    is worse than either.

    Best effort: the only call outside the Responses contract, into a route that
    may be unrouted or 503 when the session DB is down, so any failure just
    leaves the envelope's counts in place.
    """
    quoted = urllib.parse.quote(session_id, safe="")
    url = f"http://127.0.0.1:{local_port}/api/sessions/{quoted}"
    probe = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(probe, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, http.client.HTTPException, ValueError) as exc:
        # ValueError covers a bad JSON body *and* a non-UTF-8 one: supplementary
        # accounting must never cost a successful run its result.
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


def _parse_response(payload: dict[str, Any]) -> AgentResult:
    """Map a Responses-style payload onto the canonical ``AgentResult``.

    ``output`` items of type ``message`` contribute assistant text, and each
    ``function_call`` becomes one canonical :class:`ToolCall`. A
    ``function_call_output`` is *folded into* its originating call -- filling
    in its ``result`` and ``status`` -- rather than appended as a second entry,
    matching how every builtin harness emits trajectories (see
    ``agents/api/agent.py`` and the CLI parsers). Appending it would make
    trajectory metrics read a redundant argument-less call and penalise the
    agent for an invocation it never made.

    Outputs carry no ``name`` of their own, so the fold is keyed on
    ``call_id``, falling back to the oldest unresolved call only when the id is
    absent -- an id that matches nothing is an orphan, not a licence to consume
    an unrelated call. They carry no status either, so failure is read out of
    the payload (:func:`_output_failed`).

    Shape anomalies degrade rather than raise: a payload that is valid JSON but
    the wrong shape still yields whatever text and calls were readable, with the
    anomaly on ``errors``.

    The stateful response ``id`` (and ``status``/``model``) are preserved in
    ``metadata`` so a benchmark artifact can be joined back to the agent's
    full execution trajectory via ``GET /v1/responses/<id>``.
    """
    output_text = ""
    trajectory: list[ToolCall] = []
    tools_used: dict[str, int] = {}
    calls_by_id: dict[str, ToolCall] = {}
    unkeyed_calls: deque[ToolCall] = deque()
    # Parse anomalies surfaced on the result rather than dropped
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
            content = part.get("content")
            for chunk in content if isinstance(content, list) else []:
                if not isinstance(chunk, dict) or chunk.get("type") != "output_text":
                    continue
                text = chunk.get("text")
                if isinstance(text, str):
                    output_text += text
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
            if call_id:
                # An unmatched id falls through to the orphan branch rather
                # than the deque: its call is either closed or never existed.
                target = calls_by_id.pop(str(call_id), None)
            else:
                target = unkeyed_calls.popleft() if unkeyed_calls else None
            if target is None:
                # Keep the payload rather than drop it, and flag the anomaly
                parse_errors.append(f"tool output without a matching call (call_id={call_id!r})")
                target = ToolCall(name=part.get("name", ""), args={})
                trajectory.append(target)
            text = _output_text(part.get("output"))
            target.result = text
            # An output that arrived means the tool ran; only "badly?" is left
            target.status = "error" if _output_failed(text) else "completed"

    # All-None = "unavailable", so an omitted bucket is not a measured zero
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        usage = {}
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
        errors=parse_errors,
        metadata=metadata,
    )


class KubeAgentsHarness(AgentHarness):
    """Drives the in-cluster platform agent over its HTTP endpoint.

    ``_execute`` handles its *known* failure modes (HTTP errors, unreachable
    endpoint, malformed JSON) by returning an ``AgentResult`` with ``errors``
    populated; the base class's safety net covers anything unexpected.
    """

    def _execute(self, prompt: str, workspace_path: Path | None = None) -> AgentResult:
        local_port = int(os.environ.get("AGENT_LOCAL_PORT", str(SERVICE_API_PORT)))
        api_path = os.environ.get("AGENT_API_PATH", "/v1/responses")
        timeout = float(os.environ.get("AGENT_HTTP_TIMEOUT", "600"))

        # Without this, "@evil.example/v1/responses" makes 127.0.0.1:<port> the
        # userinfo of another host and sends the bearer token there.
        if not api_path.startswith("/"):
            return AgentResult.errored(f"AGENT_API_PATH must start with '/': {api_path!r}")

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
                session_id = response.headers.get(_SESSION_ID_HEADER, "")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(detail).get("error", {}).get("message", detail)
            except (json.JSONDecodeError, AttributeError):
                pass
            return AgentResult.errored(f"HTTP {exc.code} from agent endpoint: {detail}")
        except (OSError, http.client.HTTPException, ValueError) as exc:
            # OSError covers URLError, timeouts, and connection resets;
            # HTTPException covers a mid-read protocol failure (IncompleteRead
            # and friends); ValueError covers both a bad JSON body and a
            # non-UTF-8 one. All are known transport failures, not agent bugs.
            return AgentResult.errored(f"{type(exc).__name__}: {exc}")

        if not isinstance(payload, dict):
            return AgentResult.errored(
                f"agent endpoint returned non-object JSON: {type(payload).__name__}"
            )
        result = _parse_response(payload)
        if session_id:
            result.metadata["session_id"] = session_id
            _canonical_session_tokens(result.tokens, session_id, local_port, headers, timeout)
        return result
