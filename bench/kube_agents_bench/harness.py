"""``kubeagents`` agent harness: HTTP transport to the in-cluster platform agent.

The agent runs inside the cluster, so this harness only ensures the service is
reachable on a local port (lazily spawning ``kubectl port-forward``) and POSTs
the prompt to its Responses-style endpoint. No model SDK is imported; all
inference happens in the cluster, and reading the reply lives in
:mod:`kube_agents_bench.parsing`.

The platform agent delegates substantive work to subagents by filing a kanban
card and ending its turn -- there is no synchronous await tool, by design. Its
first reply is therefore an acknowledgement carrying a task id, not the answer.
Returning that would have the eval harness grade the acknowledgement and delete
the workspace while the subagent is still running, so a turn that files a card
is followed by status turns on the same conversation until every card settles
(see :meth:`KubeAgentsHarness._await_delegated_work`).

Registration is the ``devops_bench.agents`` entry point in ``pyproject.toml``,
so importing this module has no side effects.

Environment:
    AGENT_LOCAL_PORT: Local side of the port-forward (default ``8642``). The
        remote side is always the Service's 8642 and is not configurable.
    AGENT_API_PATH: Request path (default ``/v1/responses``).
    AGENT_SERVICE_NAME: Service to port-forward to (default ``platform-agent``).
    AGENT_NAMESPACE: Namespace of the service (default ``kubeagents-system``).
    AGENT_CLUSTER_CONTEXT: Optional kubectl context for the port-forward.
    AGENT_CONTAINER: Container to exec into when reading back a delegated card's
        artifacts and clearing its state (default ``platform-agent``).
    AGENT_MODEL_NAME: ``model`` field sent to the endpoint (default
        ``model-default``, the name the operator pins on ``/v1/models`` via
        ``API_SERVER_MODEL_NAME`` and the one LiteLLM actually serves).
    AGENT_CONVERSATION_ID: Pins the ``conversation`` field. Unset (the default)
        generates a fresh id per invocation so each task's trajectory is
        isolated on this stateful endpoint.
    AGENT_HTTP_TIMEOUT: Per-request timeout in seconds (default ``600``).
    AGENT_DELEGATION_TIMEOUT: Total seconds to wait for delegated work across
        all status turns (default ``1800``). ``0`` disables waiting, restoring
        the single-turn behaviour.
    AGENT_DELEGATION_POLL_INTERVAL: Seconds between status turns (default ``30``).
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
from pathlib import Path
from typing import Any

from devops_bench.agents import AgentHarness, AgentResult

from kube_agents_bench.parsing import (
    STATUS_TOOL,
    delegated_task_ids,
    delivered_results,
    merge_new,
    new_calls,
    parse_response,
    reported_statuses,
)

__all__ = ["KubeAgentsHarness"]

_log = logging.getLogger("kube_agents_bench.harness")

SERVICE_API_PORT = 8642

# Where hermes keeps per-card state in the agent's data volume. A card's
# attachments hold the files its worker produced -- the deliverable itself on a
# task that asks for a written report -- and its log holds the worker's whole
# transcript. Both outlive the card: deleting it from the board drops the row
# and leaves these, so the next run of the same task can find the previous run's
# finished answer by searching the filesystem.
_ATTACHMENTS_DIR = "/opt/data/kanban/attachments"
_LOGS_DIR = "/opt/data/kanban/logs"

# Bound on artifact text folded into one answer. The judge grades the output as
# prose, so a worker that writes a large file would otherwise bury the reply.
_MAX_ARTIFACT_BYTES = 20000

# Ceiling on files read back from one run's cards. A card is expected to produce
# a report, not a directory tree, and each file costs a round trip.
_MAX_ARTIFACTS = 8

# Ceiling on one kubectl exec. Reading a capped file or deleting a handful of
# directories is near-instant; anything slower is a cluster problem, and both
# callers would rather give up than hold the run open.
_EXEC_TIMEOUT = 60.0

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
    """Last ``max_bytes`` of ``path``, embedded in errors rather than linked --
    the log directory is deleted at process exit."""
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


def _kubectl_target() -> list[str]:
    """The service, namespace and context flags shared by every kubectl call."""
    cmd = [
        f"svc/{os.environ.get('AGENT_SERVICE_NAME', 'platform-agent')}",
        "-n",
        os.environ.get("AGENT_NAMESPACE", "kubeagents-system"),
    ]
    context = os.environ.get("AGENT_CLUSTER_CONTEXT")
    if context:
        cmd.extend(["--context", context])
    return cmd


def _port_forward_command(local_port: int) -> list[str]:
    service, *rest = _kubectl_target()
    return ["kubectl", "port-forward", service, f"{local_port}:{SERVICE_API_PORT}", *rest]


def _cluster_hint() -> str:
    """Name the cluster a failed port-forward was aimed at, if it is not pinned.

    Provisioning a task cluster runs ``gcloud container clusters
    get-credentials``, which repoints kubectl's current context; an unpinned
    port-forward then targets the task cluster, where nothing answers. The
    failure that follows reads as an agent fault, so it says which context it
    used and that nothing pinned it.
    """
    if os.environ.get("AGENT_CLUSTER_CONTEXT"):
        return ""
    try:
        proc = subprocess.run(
            ["kubectl", "config", "current-context"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    context = proc.stdout.strip() if proc.returncode == 0 else ""
    return (
        f"\nAGENT_CLUSTER_CONTEXT is unset, so this used the current context "
        f"{context or '<none>'!r}; provisioning a task cluster repoints it."
    )


def _agent_shell(script: str, timeout: float) -> str:
    """Run ``script`` in the agent container and return its stdout.

    The router has no filesystem tools -- asked to read a file it searches for a
    way and then says it cannot -- so anything on the agent's disk is reachable
    only from outside the conversation. This is the same kubectl the port-forward
    already relies on, pointed at the same Service.

    Best effort: a missing binary, an unreachable cluster or a non-zero exit all
    return ``""``, because neither caller is worth failing a run over.
    """
    cmd = [
        "kubectl",
        "exec",
        *_kubectl_target(),
        "-c",
        os.environ.get("AGENT_CONTAINER", "platform-agent"),
        "--",
        "sh",
        "-c",
        script,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        _log.debug("kubectl exec failed: %s", exc)
        return ""
    if proc.returncode != 0:
        _log.debug("kubectl exec exited %d: %s", proc.returncode, proc.stderr.strip()[:200])
        return ""
    return proc.stdout


def _ensure_port_forward(local_port: int) -> None:
    """Start a background ``kubectl port-forward`` if the port is closed.

    An already-open port is a no-op: the harness never assumes it owns the
    transport. Serialised per port, so different ports establish in parallel.

    Raises:
        RuntimeError: The forward could not be spawned, exited, or did not open
            the port in time.
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
                        f"kubectl port-forward exited with {proc.returncode}: "
                        f"{_tail(stderr_log)}{_cluster_hint()}"
                    )
                if _port_open(local_port):
                    _log.info("port-forward established on port %d", local_port)
                    return
                time.sleep(0.5)
            raise RuntimeError(
                f"port-forward did not open port {local_port} in time: "
                f"{_tail(stderr_log)}{_cluster_hint()}"
            )
        except BaseException:
            with _PF_LOCK:
                _PF_PROCESSES.pop(local_port, None)
            _stop_process(proc)
            raise


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses every redirect, turning it into an ``HTTPError`` instead.

    urllib follows redirects by default and, unlike requests, does not strip
    ``Authorization`` on a cross-host hop, so one ``302`` from whatever answers
    on the local port would hand the bearer token to another origin. A
    port-forward has no legitimate reason to redirect.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


# ProxyHandler({}): urllib's default handler honours ``http_proxy`` and has no
# implicit loopback bypass, so a proxy set in the environment would receive the
# bearer token in cleartext. The destination is always 127.0.0.1.
_OPENER = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))

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

# hermes counts reasoning inside output, not beside it, so a total that sums
# every bucket bills the thinking twice. Reported for its own sake, summed as
# part of ``output``.
_TOTAL_BUCKETS = ("input", "cached", "cache_write", "output")


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
    prompt alone, so the row replaces the envelope wholesale and a partial row
    is discarded. Best effort: any failure leaves the envelope in place.
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
    tokens["total"] = sum(counts[bucket] for bucket in _TOTAL_BUCKETS)


# A card in one of these has stopped moving on its own: done and archived are
# finished, and blocked needs a human. The other hermes statuses (triage, todo,
# ready, running) still have a worker or the dispatcher behind them.
_TERMINAL_STATUSES = frozenset({"done", "archived", "blocked"})

# ``kanban_show`` shares kanban_create's toolset in hermes, so a profile that
# can file a card can always read one back.
_POLL_PROMPT = (
    "Do not start any new work. Call {tool} on each of these task ids and "
    "report their current status: {ids}. For every task that has finished, "
    "include its complete result in your reply."
)

# Ceiling on cards awaited at once. The card list grows the poll prompt and the
# run record on every turn, so an agent looping on kanban_create would inflate
# both without bound. Far above any real fan-out.
_MAX_AWAITED_TASKS = 32

# Consecutive status turns that may report nothing before the wait is
# abandoned. One off-turn is cheap to absorb; a run of them means the agent will
# not read the board.
_MAX_SILENT_TURNS = 3

# Consecutive status turns that may fail in transport before the wait is
# abandoned. An idle keepalive dropping between turns is not a broken agent, and
# retrying costs one poll interval against the whole delegated result.
_MAX_TRANSPORT_FAILURES = 3


def _append_delivered(
    result: AgentResult, observed: list[dict[str, Any]], task_ids: list[str]
) -> None:
    """Append each finished card's own result to the text the judge grades.

    The worker runs as a separate hermes session, so its card result is the one
    part of its work that crosses back; without this the graded answer is only
    the router's closing message. ``observed`` is the status turns' trajectory
    rather than ``result.trajectory``, so the polls inform the answer without
    being graded as the agent's tool use.
    """
    sections = [
        f"Result of delegated task {tid}:\n{text}"
        for tid, text in delivered_results(observed, task_ids).items()
        if text.strip() not in result.output
    ]
    if sections:
        result.output = "\n\n".join(filter(None, [result.output, *sections]))


def _shell_quote(value: str) -> str:
    """Single-quote a value for the ``sh -c`` scripts below."""
    return "'" + value.replace("'", "'\\''") + "'"


def _artifact_paths(task_ids: list[str], timeout: float) -> list[str]:
    """List the files the delegated cards produced, one path per line.

    Listing is a separate call from reading so that no file's contents are ever
    framed by a delimiter: a report is free to contain whatever text it likes
    without being able to pass part of itself off as another artifact.
    """
    listing = " ".join(_shell_quote(t) for t in task_ids)
    script = (
        f"for t in {listing}; do "
        f'  d={_ATTACHMENTS_DIR}/$t; [ -d "$d" ] || continue; '
        '  for f in "$d"/*; do [ -f "$f" ] && echo "$f"; done; '
        "done"
    )
    prefixes = tuple(f"{_ATTACHMENTS_DIR}/{t}/" for t in task_ids)
    paths = [
        line for line in _agent_shell(script, timeout).splitlines() if line.startswith(prefixes)
    ]
    if len(paths) > _MAX_ARTIFACTS:
        _log.warning("reading %d of %d artifacts", _MAX_ARTIFACTS, len(paths))
    return paths[:_MAX_ARTIFACTS]


def _append_artifacts(result: AgentResult, task_ids: list[str], timeout: float) -> None:
    """Append the files the delegated cards produced to the graded answer.

    On a task whose deliverable is a written report, the worker's card result is
    a summary *of* the report and the report is a file the judge never sees --
    which scores the checks that ask for its contents at zero even though the
    work is done and correct. Reading it back makes the deliverable part of the
    answer, the same way :func:`_append_delivered` does for the card result.
    """
    if not task_ids:
        return
    sections = []
    for path in _artifact_paths(task_ids, timeout):
        text = _agent_shell(f"head -c {_MAX_ARTIFACT_BYTES} {_shell_quote(path)}", timeout)
        if text.strip():
            tid, _, name = path[len(_ATTACHMENTS_DIR) + 1 :].partition("/")
            sections.append(f"Artifact {name} produced by delegated task {tid}:\n{text.rstrip()}")
    if sections:
        result.output = "\n\n".join(filter(None, [result.output, *sections]))


def _purge_card_state(task_ids: list[str], timeout: float) -> None:
    """Delete the attachments and worker log of every card this run filed.

    The agent pod outlives the run, so without this each finished task leaves
    its report and its worker transcript on disk for the next run to find --
    which is how a repeat of a task reads back the previous attempt's answer
    instead of doing the work. Scoped to cards this harness delegated, so it
    cannot touch anything the run did not create.
    """
    if not task_ids:
        return
    listing = " ".join(_shell_quote(t) for t in task_ids)
    script = (
        f"for t in {listing}; do "
        f'  rm -rf {_ATTACHMENTS_DIR}/"$t" {_LOGS_DIR}/"$t".log; '
        "done"
    )
    _agent_shell(script, timeout)


def _sum_tokens(base: dict[str, Any], extra: dict[str, Any]) -> None:
    """Add ``extra``'s token buckets into ``base`` in place.

    ``None`` means "the endpoint did not report this bucket", which is not the
    same as zero: it only becomes a number once some turn reports one.
    """
    for bucket, value in extra.items():
        if value is None:
            continue
        current = base.get(bucket)
        base[bucket] = value if current is None else current + value


def _pending_first(task_ids: list[str], statuses: dict[str, str]) -> list[str]:
    """Order cards still moving ahead of settled ones, keeping filing order.

    Only matters once :data:`_MAX_AWAITED_TASKS` bites: the cap keeps a prefix,
    so a fan-out whose first cards are already finished would otherwise be
    trimmed to nothing but those and the wait skipped.
    """
    pending = [t for t in task_ids if statuses.get(t) not in _TERMINAL_STATUSES]
    return pending + [t for t in task_ids if t not in set(pending)]


def _fold_status_turn(base: AgentResult, turn: AgentResult, *, settled: bool) -> None:
    """Fold a status turn's *accounting* into the result, and nothing else.

    Waiting is the harness's own bookkeeping and may not be charged to the agent
    under test, so the poll turns' tool calls stay out of the trajectory and the
    tool counts.

    Text accumulates rather than superseding, but only from a turn that
    ``settled`` a card. Which turn holds the answer is not knowable up front: on
    a simple task it is the delegating turn ("created, the id is ...") and on a
    delegated investigation it is the last poll ("root cause: ..."). A turn
    reporting a card still running has nothing to add, and repeated "still
    running" restatements sink a task that asked for one sentence.

    What accumulates is the turn's *closing* message, not its whole text. This
    endpoint replays tool calls but not messages; reading the closing message
    means a change to that costs an omission rather than a garbled answer.

    Tokens accumulate, because usage is per turn rather than cumulative. The
    session row supersedes the sum when it is reachable.
    """
    answer = str(turn.metadata.get("final_message") or turn.output)
    if settled and answer.strip() and answer.strip() not in base.output:
        base.output = "\n\n".join(filter(None, [base.output, answer]))
    _sum_tokens(base.tokens, turn.tokens)
    for key in ("response_id", "response_status"):
        if turn.metadata.get(key) is not None:
            base.metadata[key] = turn.metadata[key]


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


class _TransportError(RuntimeError):
    """A turn that never reached the agent, or came back unreadable.

    Distinct from an agent that answered badly: the message is ready for
    ``AgentResult.errors`` and the caller stops rather than retrying.
    """


def _post_turn(
    url: str, body: dict[str, Any], headers: dict[str, str], timeout: float
) -> tuple[AgentResult, str]:
    """POST one turn and parse the reply, for the opening prompt and every poll.

    Returns:
        The parsed result and the session id header (``""`` when absent).

    Raises:
        _TransportError: The request failed or the reply was not a JSON object.
    """
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            session_id = response.headers.get(_SESSION_ID_HEADER, "")
    except urllib.error.HTTPError as exc:
        raise _TransportError(
            f"HTTP {exc.code} from agent endpoint: {_http_error_detail(exc)}"
        ) from exc
    except (OSError, http.client.HTTPException, ValueError) as exc:
        # Timeouts, resets, a mid-read protocol failure, and a body that is
        # neither UTF-8 nor JSON: transport, not agent, bugs.
        raise _TransportError(f"{type(exc).__name__}: {exc}") from exc

    if not isinstance(payload, dict):
        raise _TransportError(f"agent endpoint returned non-object JSON: {type(payload).__name__}")
    return parse_response(payload), session_id


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
            delegation_timeout = _numeric_env("AGENT_DELEGATION_TIMEOUT", "1800", float)
            poll_interval = _numeric_env("AGENT_DELEGATION_POLL_INTERVAL", "30", float)
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
            "model": os.environ.get("AGENT_MODEL_NAME", "model-default"),
            # The endpoint is stateful and replays the whole conversation's tool
            # calls, so a shared id would make each task inherit the previous
            # task's trajectory and corrupt trajectory scoring.
            "conversation": os.environ.get("AGENT_CONVERSATION_ID")
            or f"devops-bench-{uuid.uuid4().hex[:12]}",
            "input": prompt,
        }

        try:
            result, session_id = _post_turn(url, body, headers, timeout)
        except _TransportError as exc:
            return AgentResult.errored(str(exc))

        if delegation_timeout > 0:
            session_id = (
                self._await_delegated_work(
                    result,
                    url=url,
                    body=body,
                    headers=headers,
                    timeout=timeout,
                    delegation_timeout=delegation_timeout,
                    poll_interval=poll_interval,
                )
                or session_id
            )

        # One lookup, after the last turn: the session row is cumulative over
        # the conversation, so it supersedes the summed envelopes outright.
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

    def _await_delegated_work(
        self,
        result: AgentResult,
        *,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
        delegation_timeout: float,
        poll_interval: float,
    ) -> str:
        """Poll the agent until every card it filed settles.

        Only two things reach ``result``: the delivered card results, appended
        to the agent's own answer, and the turns' token spend. Everything else
        belongs to the harness -- see :func:`_fold_status_turn`.

        The harness cannot read the board itself (in-cluster SQLite, with only
        ``/v1/responses`` and ``/api/sessions`` exposed), so it asks the agent
        to, re-POSTing the same stateful ``conversation`` so the agent keeps its
        context. Cards filed *during* a status turn join the wait.

        A turn that fails in transport is retried up to
        :data:`_MAX_TRANSPORT_FAILURES` times running, and one reporting no
        outstanding card is tolerated up to :data:`_MAX_SILENT_TURNS`.

        Returns:
            The session id from the last status turn, or ``""`` when no status
            turn ran or the header was absent.
        """
        # The delegating turn may already have shown a card done, in which case
        # there is nothing to wait on and no reason to sleep a poll interval.
        statuses: dict[str, str] = reported_statuses(result.trajectory)
        filed = delegated_task_ids(result.trajectory)
        # One cap over the filed set, with both lists derived from it. Capping
        # them separately let them disagree, so cards dropped from one were
        # polled to completion via the other and had their results discarded.
        capped = len(filed) > _MAX_AWAITED_TASKS
        # Every card this episode waits on, including the ones that settle
        # mid-loop and leave ``outstanding``; their results are the answer.
        awaited: list[str] = self._capped(_pending_first(filed, statuses), result)
        outstanding = [t for t in awaited if statuses.get(t) not in _TERMINAL_STATUSES]
        # Call ids the delegating turn already spent, so its own reads are not
        # mistaken for the first poll's.
        seen_calls: set[str] = set()
        new_calls(result, seen_calls)
        # The status turns' trajectories, which carry the settled cards'
        # results. Kept beside the graded trajectory rather than in it.
        observed: list[dict[str, Any]] = list(result.trajectory)
        if not outstanding:
            self._settle(result, observed, awaited)
            return ""

        deadline = time.monotonic() + delegation_timeout
        session_id = ""
        silent = 0
        transport_failures = 0
        timed_out = True
        while outstanding:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _log.info("waiting %.0fs on delegated tasks: %s", poll_interval, ", ".join(outstanding))
            # max(0.0, ...): a negative AGENT_DELEGATION_POLL_INTERVAL would
            # otherwise raise straight out of sleep().
            time.sleep(max(0.0, min(poll_interval, remaining)))

            # Clamp the request to what is left, or a turn issued just before
            # the deadline could block for a further AGENT_HTTP_TIMEOUT and
            # overrun the total budget by that much.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            poll = _POLL_PROMPT.format(tool=STATUS_TOOL, ids=", ".join(outstanding))
            try:
                turn, turn_session = _post_turn(
                    url, {**body, "input": poll}, headers, min(timeout, remaining)
                )
            except _TransportError as exc:
                transport_failures += 1
                _log.warning(
                    "status turn failed (%d/%d): %s",
                    transport_failures,
                    _MAX_TRANSPORT_FAILURES,
                    exc,
                )
                if transport_failures < _MAX_TRANSPORT_FAILURES:
                    # Back off one poll interval and ask again: the loop top
                    # re-checks the deadline, so retries cannot outlive it.
                    continue
                # Recorded, not just logged: abandoning the wait silently
                # leaves the run validating with the delegation receipt graded
                # as the answer, the exact failure this wait exists to prevent.
                result.errors.append(
                    f"status turns failed in transport {transport_failures} times running; "
                    "still waiting on: " + ", ".join(outstanding)
                )
                timed_out = False
                break
            transport_failures = 0
            # Freshness comes off the turn's *new* calls, not the whole
            # replayed episode. Every earlier board reading comes back on every
            # poll, so the cumulative view would let an agent that has stopped
            # reading the board pass as one still answering, and would mark
            # every turn after the first terminal card as settled.
            fresh_reported = reported_statuses(new_calls(turn, seen_calls))
            _fold_status_turn(
                result,
                turn,
                settled=any(s in _TERMINAL_STATUSES for s in fresh_reported.values()),
            )
            # ``observed`` needs each distinct result once, so it stays on the
            # content test -- a replayed reading adds nothing to the answer.
            observed.extend(merge_new(observed, turn.trajectory))
            session_id = turn_session or session_id

            if any(task_id in fresh_reported for task_id in outstanding):
                silent = 0
            else:
                silent += 1
                if silent >= _MAX_SILENT_TURNS:
                    result.errors.append(
                        f"agent reported no status for {silent} turns running; "
                        "still waiting on: " + ", ".join(outstanding)
                    )
                    timed_out = False
                    break
            statuses.update(reported_statuses(turn.trajectory))
            # dict.fromkeys: order-preserving dedupe, so a card the agent
            # re-filed under the same id is awaited once. The overflow is
            # reported only the first time, since the replayed trajectory
            # re-offers the dropped ids on every poll.
            merged = list(dict.fromkeys(awaited + delegated_task_ids(turn.trajectory)))
            awaited = self._capped(_pending_first(merged, statuses), None if capped else result)
            capped = capped or len(merged) > _MAX_AWAITED_TASKS
            outstanding = [t for t in awaited if statuses.get(t) not in _TERMINAL_STATUSES]

        # Only on the deadline path: after a transport failure or a mute agent
        # the budget is untouched, and claiming it ran out would misreport why
        # the run stopped.
        if outstanding and timed_out:
            result.errors.append(
                "delegated tasks did not finish within "
                f"{delegation_timeout:.0f}s: "
                + ", ".join(f"{t} ({statuses.get(t, 'unknown')})" for t in outstanding)
            )
        self._settle(result, observed, awaited)
        return session_id

    @staticmethod
    def _settle(result: AgentResult, observed: list[dict[str, Any]], awaited: list[str]) -> None:
        """Collect everything the delegated cards produced, then clear them out.

        Reading precedes purging: the artifacts are only worth deleting once
        they are part of the answer.
        """
        _append_delivered(result, observed, awaited)
        _append_artifacts(result, awaited, _EXEC_TIMEOUT)
        _purge_card_state(awaited, _EXEC_TIMEOUT)

    @staticmethod
    def _capped(task_ids: list[str], result: AgentResult | None) -> list[str]:
        """Trim the awaited set to :data:`_MAX_AWAITED_TASKS`, recording the drop.

        Silent truncation would read as a full wait, so the overflow lands in
        ``errors``, which also stops the record promoting. A ``None`` result
        means the drop is already recorded and only the trim is wanted.
        """
        if len(task_ids) <= _MAX_AWAITED_TASKS:
            return task_ids
        dropped = len(task_ids) - _MAX_AWAITED_TASKS
        _log.warning("awaiting only %d of %d cards", _MAX_AWAITED_TASKS, len(task_ids))
        if result is not None:
            result.errors.append(
                f"too many delegated tasks: awaiting {_MAX_AWAITED_TASKS}, ignoring {dropped}"
            )
        return task_ids[:_MAX_AWAITED_TASKS]
