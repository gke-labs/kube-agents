"""``kubeagents`` agent harness: HTTP transport to the in-cluster platform agent.

The agent runs inside the cluster, so this harness only ensures the service is
reachable on a local port (lazily spawning ``kubectl port-forward``) and POSTs
the prompt to its Responses-style endpoint. No model SDK is imported; all
inference happens in the cluster, and reading the reply lives in
:mod:`kube_agents_bench.parsing`.

That port-forward cannot reach a pod running under GKE Sandbox (gVisor), which
a stock install turns on. Nothing here notices: the forward binds its local
port before anything dials the pod, so ``_ensure_port_forward`` sees an open
port and returns, and the run dies later as a transport failure. Noticing at
all takes a request rather than a port check, as ``tests/e2e/conftest.py``
does -- though a request only separates a working transport from a broken one,
never a sandboxed agent from one that is down. ``bench/README.md`` has the
symptoms and the remedies.

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

    AGENT_TRANSPORT: ``api`` (default; everything above) or ``a2a``, the
        diagnostic transport: the prompt goes onto the A2A bus directly as
        one ``message`` envelope on ``a2a.tasks.{addressee}.{taskId}.in`` and
        the task's events are folded until the terminal one
        (:mod:`kube_agents_bench.a2a_transport`). It proves the bus and the
        executor and skips the gateway, which is why it is a diagnostic: the
        next-mode transport the evals will run on is the gateway's inject
        adapter, planned and not yet built, which keeps the only credential
        that may publish on every addressee's ``in`` subject. This one
        connects as the operator's ``eval`` principal -- publish on
        ``platform``'s ``in``, subscribe on ``platform``'s ``events`` and
        ``supervisor``, nothing else -- and never the gateway's,
        and every envelope it publishes carries ``authority: null``: that
        block is the gateway's to populate and its shape is advisory, so the
        harness asserts nothing there rather than invent one. Same harness,
        same ``AgentResult``, same transcript stash for the verifiers. The
        a2a path reads ``AGENT_NAMESPACE``, ``AGENT_CLUSTER_CONTEXT``,
        ``AGENT_HTTP_TIMEOUT`` (as the whole task's deadline), the delegation
        variables, and:
    AGENT_A2A_ADDRESSEE: The executor the task is addressed to (default
        ``platform``, the only addressee the ``eval`` grants reach).
    AGENT_A2A_LOCAL_PORT: Local side of the port-forward to the NATS Service.
        Unset, the harness picks a free port once per process and owns the one
        forward on it, which every run in the process rides, so parallel units
        never share a tunnel and a run leaves no idle forward; set it to reuse
        a forward you run yourself. The remote side is always the client port
        4222.
    AGENT_A2A_NATS_SERVICE: The NATS Service to port-forward to (default
        ``<AGENT_SERVICE_NAME>-a2a-nats``); the credentials Secret is
        ``<service>-creds`` in ``AGENT_NAMESPACE``, key ``eval-password``,
        which exists only where the operator runs with
        ``A2A_EVAL_PRINCIPAL=true``.
    AGENT_A2A_NATS_URL: A bus URL to use instead of spawning a port-forward.
    AGENT_A2A_NATS_PASSWORD: The ``eval`` principal's password, instead of
        reading the Secret.
    AGENT_A2A_ACCEPT_TIMEOUT: Seconds a submitted task may wait for its first
        event before the run is classified as infrastructure -- no executor
        consumed it (default ``120``).
"""

from __future__ import annotations

import atexit
import base64
import http.client
import json
import logging
import os
import re
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
from collections.abc import Callable
from pathlib import Path
from typing import Any

from devops_bench.agents import AgentHarness, AgentResult
from devops_bench.agents.result import empty_tokens

from kube_agents_bench import a2a_transport as a2a
from kube_agents_bench import transcript
from kube_agents_bench.parsing import (
    STATUS_TOOL,
    delegated_task_ids,
    delivered_results,
    merge_new,
    new_calls,
    parse_response,
    reported_statuses,
)

__all__ = ["INFRA_FAILURE_MARKER", "KubeAgentsHarness"]

_log = logging.getLogger("kube_agents_bench.harness")

SERVICE_API_PORT = 8642

# Prefix on ``AgentResult.errors[0]`` that marks a run whose transport died on
# every attempt: the agent tunnel never established, the opening turn never
# reached the agent, or the delegation wait lost the endpoint on every
# status-turn retry with cards still outstanding. ``scoring.py`` matches this
# string on a record's error and classifies the repetition as infrastructure
# rather than grading it. The literal is duplicated there (importing the
# harness would drag ``devops_bench`` into the scorer), and ``test_scoring.py``
# asserts the two strings agree: change it in both files or in neither.
INFRA_FAILURE_MARKER = "KUBE_AGENTS_INFRA_FAILURE"

# The two transports ``AGENT_TRANSPORT`` selects between.
TRANSPORT_API = "api"
TRANSPORT_A2A = "a2a"
_TRANSPORTS = frozenset({TRANSPORT_API, TRANSPORT_A2A})

# Defaults for the bounds both transports read from the environment, as the
# strings ``_numeric_env`` parses: one request (on a2a, the whole task), the
# delegation wait's total, and the interval between status turns, in seconds.
_DEFAULT_HTTP_TIMEOUT = "600"
_DEFAULT_DELEGATION_TIMEOUT = "1800"
_DEFAULT_POLL_INTERVAL = "30"

# The a2a transport's port-forward target: the operator's NATS Service, named
# ``<cr>-a2a-nats`` and listening for clients on 4222; its credentials Secret
# is ``<service>-creds``, and the one key read from it is the ``eval``
# principal's -- the harness's own identity on the bus, never the gateway's.
# The local side defaults away from 4222 so a bus a developer already runs on
# the loopback is not mistaken for the tunnel.
_A2A_NATS_SERVICE_SUFFIX = "-a2a-nats"
_A2A_CREDS_SECRET_SUFFIX = "-creds"
_A2A_CREDS_KEY = "eval-password"
_A2A_NATS_CLIENT_PORT = 4222
_LOOPBACK_HOST = "127.0.0.1"
# The tunnel's near end: where a port-forward the harness spawned listens.
_A2A_LOOPBACK_URL = "nats://127.0.0.1"
# What a bounded a2a wait reports about the cancel it published for its task.
_CANCEL_TAKEN_NOTE = "cancel published"
_CANCEL_UNCONFIRMED_NOTE = "cancel not confirmed"
# How long a submitted task may sit with no event at all before the run is
# classified as infrastructure: nothing consumed it. The bridge publishes
# ``submitted`` on accept before queueing, so a busy executor still answers
# inside this window; only an absent one does not.
_A2A_DEFAULT_ACCEPT_TIMEOUT = "120"
# Ceiling on one ``kubectl get secret`` for the bus credential.
_A2A_SECRET_READ_TIMEOUT = 30.0
# Terminal reasons that say the executor lost the task rather than the persona
# failing it (docs/designs/eval-next-transport.md, stage 1): the bridge's, from
# a2a/hermes-bridge/bridge.go and sweep.go, and the worker adapter's, from
# a2a/worker-adapter/adapter.go. A run ending on one is infrastructure, the
# class the api transport gives an exhausted retry. The persona's own reasons
# (``hermes-exited-nonzero``, ``deadline-exceeded``, ``canceled-by-request``)
# and any the harness does not know stay graded.
_A2A_INFRA_REASONS = frozenset(
    {
        "bridge-shutdown",
        "bridge-queue-overflow",
        "bus-publish-failed",
        "spawn-failed",
        "bridge-died-without-terminal-event",
        "worker-evicted",
        "bus-subscribe-failed",
        "canceled-before-start",
    }
)
# What ``tokens`` say on an a2a record: the bus carries no usage, and a null
# bucket is the truthful value rather than a zero (``scoring.py`` reads the
# terminal event in the trajectory as the liveness signal instead).
_A2A_TOKENS_NOTE = "the a2a transport carries no token usage; every bucket is null"

# Where hermes keeps per-card state in the agent's data volume. A card's
# attachments hold the files its worker produced -- the deliverable itself on a
# task that asks for a written report -- and its log holds the worker's whole
# transcript. Both outlive the card: deleting it from the board drops the row
# and leaves these, so the next run of the same task can find the previous run's
# finished answer by searching the filesystem.
_ATTACHMENTS_DIR = "/opt/data/kanban/attachments"
_LOGS_DIR = "/opt/data/kanban/logs"
# One terminal command per line in a card's worker log, as hermes renders it:
# ``  ┊ 💻 $         <command>  0.6s [exit 1]``. The timing and exit suffixes
# are stripped; the command is kept verbatim otherwise.
_WORKER_COMMAND_RE = re.compile(
    r"💻 \$\s+(?P<command>.+?)(?:\s+\d+(?:\.\d+)?s(?: \[exit \d+\])?)?\s*$"
)
_MAX_WORKER_LOG_BYTES = 512_000

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

_PF_LOCK = threading.Lock()  # guards the four registries below
_PF_PROCESSES: dict[int, subprocess.Popen[bytes]] = {}
_PF_PORT_LOCKS: dict[int, threading.Lock] = {}
_PF_LOG_DIR: Path | None = None
_A2A_PICKED_PORT: int | None = None  # the a2a door's port, picked free once per process


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


def _port_open(port: int, host: str = _LOOPBACK_HOST) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            sock.connect((host, port))
            return True
    except OSError:
        return False


def _free_local_port() -> int:
    """A loopback port nobody listens on, for a forward this process will own.

    Bound to ``127.0.0.1:0`` and released, so the kernel picks it from the
    ephemeral range. The window between the release and kubectl's bind is the
    one every ephemeral-port user accepts; a sibling picking the same port in
    that instant would be ridden rather than refused, which is the shared-port
    failure this exists to avoid, at the odds the range gives it.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((_LOOPBACK_HOST, 0))
        return int(sock.getsockname()[1])


def _a2a_local_port() -> int:
    """The process's own a2a port: picked free on the first call, then reused.

    One port per process, not per run: the forward on it is established once
    and ridden by every later run in the process (``_ensure_port_forward`` is
    a no-op while the port is open), and it is torn down by atexit or by a
    retry's reset, never left idle behind a run that moved to a fresh port.
    """
    global _A2A_PICKED_PORT
    with _PF_LOCK:
        if _A2A_PICKED_PORT is None:
            _A2A_PICKED_PORT = _free_local_port()
        return _A2A_PICKED_PORT


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


def _kubectl_target(service: str | None = None) -> list[str]:
    """The service, namespace and context flags shared by every kubectl call.

    ``service`` defaults to the agent's own Service; the a2a transport passes
    the NATS Service, which lives in the same namespace and context.
    """
    cmd = [
        f"svc/{service or os.environ.get('AGENT_SERVICE_NAME', 'platform-agent')}",
        "-n",
        os.environ.get("AGENT_NAMESPACE", "kubeagents-system"),
    ]
    context = os.environ.get("AGENT_CLUSTER_CONTEXT")
    if context:
        cmd.extend(["--context", context])
    return cmd


def _port_forward_command(
    local_port: int, service: str | None = None, remote_port: int = SERVICE_API_PORT
) -> list[str]:
    target, *rest = _kubectl_target(service)
    return ["kubectl", "port-forward", target, f"{local_port}:{remote_port}", *rest]


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


def _ensure_port_forward(
    local_port: int, *, service: str | None = None, remote_port: int = SERVICE_API_PORT
) -> None:
    """Start a background ``kubectl port-forward`` if the port is closed.

    An already-open port is a no-op: the harness never assumes it owns the
    transport. Serialised per port, so different ports establish in parallel.
    ``service`` and ``remote_port`` default to the agent's HTTP endpoint; the
    a2a transport forwards the NATS Service's client port instead.

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

        cmd = _port_forward_command(local_port, service, remote_port)
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


def _reset_port_forward(
    local_port: int, *, service: str | None = None, remote_port: int = SERVICE_API_PORT
) -> None:
    """Tear this process's forward down and stand a fresh one up.

    ``_ensure_port_forward`` returns immediately when ``_port_open`` is true,
    and an open local listener whose upstream is gone is exactly the state a
    transport retry has to escape -- ``kubectl port-forward`` keeps accepting
    on 127.0.0.1 after the pod behind it has been replaced. Probing the port
    therefore proves nothing; the process has to go first.

    A forward this process did not spawn is left alone (nothing is registered
    to kill), and the re-establish is then a no-op -- someone else owns the
    tunnel and terminating it is not ours to do.

    Raises:
        RuntimeError: The replacement forward could not be established.
    """
    with _port_establishment_lock(local_port):
        with _PF_LOCK:
            proc = _PF_PROCESSES.pop(local_port, None)
        if proc is None:
            _log.info("no port-forward owned on port %d; nothing to tear down", local_port)
        else:
            _log.info("tearing down the port-forward on port %d before retrying", local_port)
            _stop_process(proc)
    # Outside the lock: _ensure_port_forward takes the same non-reentrant one.
    # The agent's endpoint is the positional-only call the api path has
    # always made; only another target spells its service and port out.
    if service is None and remote_port == SERVICE_API_PORT:
        _ensure_port_forward(local_port)
    else:
        _ensure_port_forward(local_port, service=service, remote_port=remote_port)


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


def _append_final(result: AgentResult, sections: list[str]) -> None:
    """Fold delegated deliverables into the run-level final message.

    ``metadata["final_message"]`` is what the user ultimately receives: the
    delegating turn's own closing message plus, when work was delegated, the
    delivered card results and artifacts. Poll-turn recitals stay out (see
    ``_fold_status_turn``). The transcript verifiers' default scope reads
    this, so a worker's actual RCA satisfies a phrase check while a router's
    progress paraphrase cannot.
    """
    if not sections:
        return
    current = str(result.metadata.get("final_message") or "")
    add = [s for s in sections if s.split("\n", 1)[-1].strip() not in current]
    if add:
        result.metadata["final_message"] = "\n\n".join(filter(None, [current, *add]))


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
    all_sections = [
        f"Result of delegated task {tid}:\n{text}"
        for tid, text in delivered_results(observed, task_ids).items()
    ]
    sections = [s for s in all_sections if s.split("\n", 1)[1].strip() not in result.output]
    if sections:
        result.output = "\n\n".join(filter(None, [result.output, *sections]))
    _append_final(result, all_sections)


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
    _append_final(result, sections)


_LOG_PRESENT = "__WORKER_LOG__"
_LOG_ABSENT = "__NO_WORKER_LOG__"


def _worker_commands(task_ids: list[str], timeout: float) -> list[dict[str, str]] | None:
    """Every terminal command the delegated workers ran, from their card logs.

    The worker is a separate hermes session and its tool calls never reach
    ``result.trajectory`` (see ``ToolCalledVerifier``), but its log records
    each terminal command it executed. Read here, before ``_purge_card_state``
    deletes the log, and stashed for the ``worker_commands`` verifier -- the
    one check that can say which route a worker took, not only what it
    answered. Only terminal commands are visible; MCP tool calls are not.

    ``None`` when any card's log could not be read at all. ``_agent_shell``
    returns ``""`` for a kubectl that failed as readily as for an empty file,
    and the first time this ran, a credential hiccup on the runner turned a
    worker that had run dozens of commands into "0 command(s)" -- which
    failed the required pattern for the wrong reason and passed the forbidden
    one for no reason. The script therefore prints a sentinel before the log
    (or a different one when the file is absent), and a reply carrying
    neither is a capture failure, which the verifier reports as
    ``status="error"`` rather than grading.
    """
    # No card, no capture: a router that answered from memory leaves nothing
    # to read, and grading an empty list would let a forbidden-pattern check
    # pass on a run where no worker ran -- silence as a pass. Review caught
    # this returning [] here.
    if not task_ids:
        return None
    commands: list[dict[str, str]] = []
    for tid in task_ids:
        path = _shell_quote(f"{_LOGS_DIR}/{tid}.log")
        script = (
            f'if [ -f {path} ]; then echo {_LOG_PRESENT}; head -c {_MAX_WORKER_LOG_BYTES} {path}; '
            f"else echo {_LOG_ABSENT}; fi"
        )
        text = _agent_shell(script, timeout)
        first, _, body = text.partition("\n")
        if first.strip() == _LOG_ABSENT:
            continue
        if first.strip() != _LOG_PRESENT:
            _log.warning("worker log for %s could not be read; route checks will error", tid)
            return None
        for line in body.splitlines():
            match = _WORKER_COMMAND_RE.search(line)
            if match:
                commands.append({"task": tid, "command": match.group("command").strip()})
    return commands


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
        # Deliberately NOT folded into metadata["final_message"]: a poll
        # turn's closer is the router reciting progress, not the answer the
        # user receives. Run-level final_message is composed of the
        # delegating turn's own closer plus the delivered card results and
        # artifacts (_append_final via _settle) -- letting a later recital
        # overwrite it would replace "created, the id is 7" with "the card
        # settled", which is exactly the sentence the exact checks must not
        # grade.
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
    ``AgentResult.errors``.

    ``retryable`` says whether issuing the same request again could plausibly
    succeed. It is False by default so a new raise site has to opt in.

    ``fatal`` says the transport refused the harness outright -- on the a2a
    path, a credential or a subject the bus will not take -- so neither a
    retry nor grading is right, and the delegation wait ends the run as
    infrastructure at once. On the api path a non-retryable failure is a
    handler's own answer and stays graded; only the a2a status turn sets it.
    """

    def __init__(self, message: str, *, retryable: bool = False, fatal: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.fatal = fatal


# Gateway statuses a proxy in front of the agent emits when the upstream is
# gone or saturated -- the pod restarted, the tunnel died -- and which clear
# once it is back. 429 is the endpoint's own admission control ("Too many
# concurrent runs"): the rejected request itself never reached an agent --
# true of an opening turn and of a status poll alike, where the delegating
# turn already ran but this poll was refused at the door -- and the condition
# clears when a slot frees, the same run class as a saturated gateway. On
# exhaustion both turn paths deliberately end in _infra_failure rather than
# grading a partial record: see _DelegationTransportExhausted for why settling
# the cards into a record that is about to be replaced wholesale is not a
# rescue. Every other status is an answer about the request itself and
# repeating the request cannot change it, 500 included: a handler that raised
# will raise again.
_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})


def _connection_dropped(exc: BaseException) -> bool:
    """Whether ``exc`` is a connection lost in flight rather than a timeout.

    A reset or a half-closed keepalive says the socket went away and a fresh
    one may not; a timeout says the request may still be running on the other
    end, and re-issuing it would spend the whole HTTP budget a second time for
    a turn that could yet return. ``URLError`` wraps the real ``OSError`` in
    ``reason``, so unwrap before testing.
    """
    reason = getattr(exc, "reason", None)
    if isinstance(reason, BaseException):
        exc = reason
    if isinstance(exc, TimeoutError):
        return False
    return isinstance(exc, ConnectionError | http.client.IncompleteRead)


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
            f"HTTP {exc.code} from agent endpoint: {_http_error_detail(exc)}",
            retryable=exc.code in _RETRYABLE_STATUSES,
        ) from exc
    except (OSError, http.client.HTTPException, ValueError) as exc:
        # Timeouts, resets, a mid-read protocol failure, and a body that is
        # neither UTF-8 nor JSON: transport, not agent, bugs.
        raise _TransportError(
            f"{type(exc).__name__}: {exc}", retryable=_connection_dropped(exc)
        ) from exc

    if not isinstance(payload, dict):
        raise _TransportError(f"agent endpoint returned non-object JSON: {type(payload).__name__}")
    return parse_response(payload), session_id


def _cancel_note(exchange: a2a.Exchange) -> str:
    """Whether the server took the cancel the bounded wait published."""
    return _CANCEL_TAKEN_NOTE if exchange.cancel_taken else _CANCEL_UNCONFIRMED_NOTE


def _nothing_ran(exchange: a2a.Exchange) -> bool:
    """True when no executor ran the submission before the wait ended.

    The accept bound says so outright. A deadline that fell first says the
    same thing when the fold is still empty, and when the fold never left
    ``submitted``: the bridge publishes that on accept and queues the task
    behind its workers, so a task still there at the deadline waited out the
    budget with no model running (docs/designs/eval-next-transport.md, stage
    1, the same rule on both transports). Either way the record is the run
    class, not an answer.
    """
    return exchange.outcome == a2a.OUTCOME_NOT_ACCEPTED or (
        exchange.outcome == a2a.OUTCOME_DEADLINE and not exchange.fold.started
    )


def _executor_lost(exchange: a2a.Exchange) -> str:
    """Why the executor lost the task, or ``""`` when the terminal is the persona's.

    A ``rejected`` terminal is the harness's own defect (a submission with no
    text parts) and is never graded. A ``failed`` or ``canceled`` one is
    infrastructure when its reason token is in ``_A2A_INFRA_REASONS``. A
    terminal with no reason, or one the harness does not know, is the
    persona's outcome and stays graded.
    """
    fold = exchange.fold
    if not fold.final:
        return ""
    if fold.state == a2a.STATE_REJECTED:
        return f"rejected: {fold.status_message or 'no reason given'}"
    if fold.state in (a2a.STATE_FAILED, a2a.STATE_CANCELED) and fold.reason in _A2A_INFRA_REASONS:
        return fold.status_message
    return ""


def _infra_failure(detail: str) -> AgentResult:
    """A run whose transport died under it, recorded as infrastructure.

    ``output`` is deliberately left empty. ``AgentResult.errored`` copies its
    message into ``output``, which the eval harness writes to results.json as
    the "Actual Output" the LLM judge grades -- and on build
    2092339233527173120 that is how a proxy's HTTP 502 error page came to be
    graded as the agent's answer to ``gpu-stress-test-diagnosis``
    ("The Actual Output consists entirely of an HTTP 502 Bad Gateway",
    OutcomeValidity 0.0). A transport failure has to set a run class, not an
    output: the marker on ``errors[0]`` is what ``scoring.py`` reads.
    """
    return AgentResult(
        output="",
        trajectory=[],
        errors=[f"{INFRA_FAILURE_MARKER}: {detail}"],
        metadata={"infra_failure": detail},
    )


def _a2a_password(nats_service: str) -> str:
    """The ``eval`` principal's bus password, from the env or the Secret.

    Only that key is ever read. The same Secret holds the gateway's password,
    and the gateway's grants are the ones that reach every addressee; a
    harness holding them would be a second requester the bus could not tell
    from the first.

    Raises:
        RuntimeError: The Secret could not be read or carries no such key --
            the install has no bus (``mode: today``), or kubectl cannot reach
            it. The message names the Secret and quotes kubectl.
    """
    given = os.environ.get("AGENT_A2A_NATS_PASSWORD")
    if given:
        return given
    secret = nats_service + _A2A_CREDS_SECRET_SUFFIX
    _target, *rest = _kubectl_target()
    cmd = [
        "kubectl",
        "get",
        "secret",
        secret,
        *rest,
        "-o",
        f"jsonpath={{.data.{_A2A_CREDS_KEY}}}",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_A2A_SECRET_READ_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"no bus credential: reading secret/{secret} failed: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"no bus credential: kubectl get secret {secret} exited {proc.returncode}: "
            f"{proc.stderr.strip()}{_cluster_hint()}"
        )
    try:
        password = base64.b64decode(proc.stdout.strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(
            f"no bus credential: secret/{secret} key {_A2A_CREDS_KEY} is unreadable"
        ) from exc
    if not password:
        raise RuntimeError(f"no bus credential: secret/{secret} has no {_A2A_CREDS_KEY} key")
    return password


def _a2a_result(exchange: a2a.Exchange, ids: a2a.TaskIds, addressee: str) -> AgentResult:
    """Map a folded task onto the canonical result.

    ``output`` and ``final_message`` are the ``result`` artifact's text -- the
    deliverable, and the text the transcript verifiers grade by default. The
    trajectory is the fold's: the task's own lifecycle events, plus any tool
    calls an ``activity`` artifact carried. Tokens stay null; the bus reports
    no usage, and ``metadata`` says so rather than inventing a number.
    """
    fold = exchange.fold
    output = fold.artifact_text(a2a.ARTIFACT_RESULT)
    return AgentResult(
        output=output,
        trajectory=list(fold.trajectory),
        tokens=empty_tokens(),
        errors=[],
        metadata={
            "transport": TRANSPORT_A2A,
            "final_message": output,
            "task_id": ids.task_id,
            "context_id": ids.context_id,
            "correlation_id": ids.correlation_id,
            "addressee": addressee,
            "terminal_state": fold.state if fold.final else None,
            "status_history": list(fold.history),
            "artifacts": fold.artifact_names(),
            "events": len(exchange.events),
            "post_final_dropped": fold.post_final_dropped,
            "malformed_events": fold.malformed,
            "tokens_note": _A2A_TOKENS_NOTE,
        },
    )


class _DelegationTransportExhausted(Exception):
    """The delegation wait lost its transport on every status-turn retry.

    Raised out of ``_await_delegated_work`` instead of appending to
    ``result.errors``: an appended error still reaches the judge with the
    delegation receipt graded as the answer (build 2093030474753511424:
    ``rca-remediation-pr`` scored 0.0 for a pod restart while its worker filed
    the real remediation PR). ``_execute`` catches this and replaces the graded
    result wholesale with :func:`_infra_failure`, the same run class the
    opening turn returns on exhaustion. Carries the marker detail as ``str``.
    """


class KubeAgentsHarness(AgentHarness):
    """Drives the in-cluster platform agent over its HTTP endpoint, or, under
    ``AGENT_TRANSPORT=a2a``, over the A2A bus.

    Known failure modes (HTTP errors, unreachable endpoint, malformed JSON)
    return an ``AgentResult`` with ``errors`` populated; the base class's safety
    net covers anything unexpected.
    """

    def run(self, prompt: str, workspace_path: Path | None = None) -> AgentResult:
        """Run the agent, then stash the transcript for the text/trace verifiers.

        Wraps :meth:`AgentHarness.run` rather than ``_execute``: ``_execute``
        has five early error-returns and the base's safety net converts
        unexpected exceptions to ``AgentResult.errored(...)``, and every one of
        those paths must still reach the stash — an errored run's (possibly
        empty) transcript is the truthful input for the verifiers, not the
        previous task's. The clear() up front is the other half of that: see
        the staleness caveat in :mod:`kube_agents_bench.transcript`.

        ``started_at`` is wall clock, taken before the agent is invoked and
        carried through to the stash: ``ledger_issue_contains`` compares it to
        the timestamp the audit script rendered into the GitHub ledger issue,
        which is the only way to tell the artifact THIS run published from the
        one the previous run left at the same issue number. It must be read
        here and not at stash time, when the run is already over.
        """
        transcript.clear()
        started_at = time.time()
        result = super().run(prompt, workspace_path)
        transcript.set(
            result.output,
            result.trajectory,
            prompt=prompt,
            final_message=str(result.metadata.get("final_message") or ""),
            started_at=started_at,
            worker_commands=result.metadata.get("worker_commands"),
        )
        return result

    def _execute(self, prompt: str, workspace_path: Path | None = None) -> AgentResult:
        transport = os.environ.get("AGENT_TRANSPORT", TRANSPORT_API)
        if transport not in _TRANSPORTS:
            return AgentResult.errored(
                f"AGENT_TRANSPORT must be one of {sorted(_TRANSPORTS)}, got {transport!r}"
            )
        if transport == TRANSPORT_A2A:
            return self._execute_a2a(prompt)

        api_path = os.environ.get("AGENT_API_PATH", "/v1/responses")
        try:
            local_port = _numeric_env("AGENT_LOCAL_PORT", str(SERVICE_API_PORT), int)
            timeout = _numeric_env("AGENT_HTTP_TIMEOUT", _DEFAULT_HTTP_TIMEOUT, float)
            delegation_timeout = _numeric_env(
                "AGENT_DELEGATION_TIMEOUT", _DEFAULT_DELEGATION_TIMEOUT, float
            )
            poll_interval = _numeric_env(
                "AGENT_DELEGATION_POLL_INTERVAL", _DEFAULT_POLL_INTERVAL, float
            )
        except ValueError as exc:
            return AgentResult.errored(str(exc))

        # "@evil.example/..." would make 127.0.0.1:<port> the userinfo of
        # another host and send the bearer token there.
        if not api_path.startswith("/"):
            return AgentResult.errored(f"AGENT_API_PATH must start with '/': {api_path!r}")

        # A tunnel that cannot be established is the same outage as one that
        # dies mid-run -- the gateway pod replaced, its node draining, its
        # cluster unreachable -- so it gets the same bounded retry the two
        # turn loops use and the same run class on exhaustion. Returning the
        # RuntimeError text as an errored result put "kubectl port-forward
        # exited with 1" in front of the judge as the agent's answer: 11 of
        # the 17 no-agent-ran repetitions in #1116's 46-PR sweep are this
        # shape, and three builds lost every repetition of a case to it,
        # which repetition voting cannot absorb. INFRA instead drops the
        # repetition from the denominator, the class terminal 429s join
        # via #1095's _RETRYABLE_STATUSES entry.
        transport_failures = 0
        while True:
            try:
                _ensure_port_forward(local_port)
                break
            except RuntimeError as exc:
                transport_failures += 1
                _log.warning(
                    "port-forward failed to establish (%d/%d): %s",
                    transport_failures,
                    _MAX_TRANSPORT_FAILURES,
                    exc,
                )
                if transport_failures >= _MAX_TRANSPORT_FAILURES:
                    # Not AgentResult.errored: see _infra_failure. No agent
                    # ever saw the request, so this is the run class, not an
                    # answer.
                    return _infra_failure(
                        f"the agent tunnel failed to establish {transport_failures} "
                        f"times running; last failure: {exc}"
                    )

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

        # Same shape as the status-turn retry in _await_delegated_work: count
        # the transport failures, log each one against the ceiling, respawn the
        # tunnel between attempts, and give up at _MAX_TRANSPORT_FAILURES. What
        # differs is only the pacing -- there is no poll interval to back off
        # over here. The 502 both loops exist for is a live proxy over a dead
        # upstream, so the tunnel is torn down and respawned, never merely
        # probed.
        transport_failures = 0
        while True:
            try:
                result, session_id = _post_turn(url, body, headers, timeout)
                break
            except _TransportError as exc:
                # A 500, a 4xx other than 429, or a body that is not JSON
                # says a handler answered; that is the agent's own failure and
                # still belongs in front of the judge. Only a gateway status,
                # an admission-control 429, or a dropped connection is worth a
                # second attempt: see _RETRYABLE_STATUSES.
                if not exc.retryable:
                    return AgentResult.errored(str(exc))
                transport_failures += 1
                _log.warning(
                    "opening turn failed in transport (%d/%d): %s",
                    transport_failures,
                    _MAX_TRANSPORT_FAILURES,
                    exc,
                )
                if transport_failures >= _MAX_TRANSPORT_FAILURES:
                    # Not AgentResult.errored: see _infra_failure. This is the
                    # run class, not an answer.
                    return _infra_failure(
                        f"the opening turn failed in transport {transport_failures} times "
                        f"running; last failure: {exc}"
                    )
                try:
                    _reset_port_forward(local_port)
                except RuntimeError as pf_exc:
                    # Counted, not returned: a forward that will not come back
                    # is the same outage, and the loop's own ceiling ends it.
                    _log.warning("port-forward respawn failed before retry: %s", pf_exc)

        if delegation_timeout > 0:

            def _status_turn(poll: str, turn_timeout: float) -> tuple[AgentResult, str]:
                return _post_turn(url, {**body, "input": poll}, headers, turn_timeout)

            def _respawn_tunnel() -> None:
                _reset_port_forward(local_port)

            try:
                session_id = (
                    self._await_delegated_work(
                        result,
                        turn=_status_turn,
                        reset=_respawn_tunnel,
                        timeout=timeout,
                        delegation_timeout=delegation_timeout,
                        poll_interval=poll_interval,
                    )
                    or session_id
                )
            except _DelegationTransportExhausted as exc:
                # Not AgentResult.errored, and not the delegating turn's
                # partial result either: see _infra_failure. The wait died in
                # transport, so this is the run class, not an answer.
                return _infra_failure(str(exc))

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

    def _execute_a2a(self, prompt: str) -> AgentResult:
        """The a2a transport: submit the prompt on the bus and fold the reply.

        A diagnostic (see :mod:`kube_agents_bench.a2a_transport`), with
        ``_execute``'s retry classes. The tunnel to the NATS Service gets the
        same bounded establishment retry. An attempt that never reaches the
        bus or loses it (connect refused, the connection dropped before the
        terminal) is retried through a fresh tunnel as a NEW task, up to
        :data:`_MAX_TRANSPORT_FAILURES` attempts in all; the ``eval``
        principal has no replay, so the same task cannot be picked back up.
        A task an attempt had submitted before it dropped gets a cancel from
        the next attempt, or on the way out when there is none, and its id
        lands in ``metadata["abandoned_tasks"]`` on every record this method
        returns, the infrastructure one included. Exhaustion, a refused
        credential or a refused subject, a missing creds Secret, a task no
        executor accepted inside ``AGENT_A2A_ACCEPT_TIMEOUT``, and one the
        bridge accepted and left queued at ``submitted`` until the deadline
        (a cancel is published either way) are infrastructure.
        A task an executor took and ended ``failed`` or ``canceled`` is the
        agent's own outcome and stays in front of the judge with the terminal
        on ``errors``, unless the terminal's reason token is one the bridge or
        the worker adapter writes for its own fault (``_A2A_INFRA_REASONS``);
        those, and a ``rejected`` terminal, which is the harness's own defect,
        are infrastructure too. A task still running at ``AGENT_HTTP_TIMEOUT``
        is graded, after a cancel.

        This method submits and awaits and nothing more. The kanban poll for
        delegated cases behind the bridge is :meth:`_a2a_delegation_wait`,
        called after it, where it can be deleted.
        """
        try:
            timeout = _numeric_env("AGENT_HTTP_TIMEOUT", _DEFAULT_HTTP_TIMEOUT, float)
            accept_timeout = _numeric_env(
                "AGENT_A2A_ACCEPT_TIMEOUT", _A2A_DEFAULT_ACCEPT_TIMEOUT, float
            )
            pinned_port: int | None = None
            if os.environ.get("AGENT_A2A_LOCAL_PORT"):
                pinned_port = _numeric_env("AGENT_A2A_LOCAL_PORT", "", int)
            delegation_timeout = _numeric_env(
                "AGENT_DELEGATION_TIMEOUT", _DEFAULT_DELEGATION_TIMEOUT, float
            )
            poll_interval = _numeric_env(
                "AGENT_DELEGATION_POLL_INTERVAL", _DEFAULT_POLL_INTERVAL, float
            )
        except ValueError as exc:
            return AgentResult.errored(str(exc))
        addressee = os.environ.get("AGENT_A2A_ADDRESSEE", a2a.DEFAULT_ADDRESSEE)
        nats_service = os.environ.get("AGENT_A2A_NATS_SERVICE") or (
            os.environ.get("AGENT_SERVICE_NAME", "platform-agent") + _A2A_NATS_SERVICE_SUFFIX
        )
        url = os.environ.get("AGENT_A2A_NATS_URL")
        # A URL given outright is somebody else's tunnel (or a bus on the
        # network): nothing to establish and nothing to respawn between retries.
        own_tunnel = not url
        # No port pinned: a free one, picked once per process and owned by it,
        # so every run in the process rides the one forward and a new run
        # leaves no idle kubectl behind on a port nothing dials again. A
        # shared default would put every parallel unit through whichever
        # process forwarded first, and that owner's atexit teardown, or its
        # retry's respawn, drops the listener under its siblings mid-task;
        # hack/ci-eval-pr.sh gives each api-path unit its own AGENT_LOCAL_PORT
        # for the same reason. A pinned port keeps the reuse: a forward the
        # operator runs is left alone, and _ensure_port_forward rides it.
        local_port = pinned_port
        if local_port is None and own_tunnel:
            local_port = _a2a_local_port()

        def _tunnel(reset: bool) -> None:
            if not own_tunnel:
                return
            forward = _reset_port_forward if reset else _ensure_port_forward
            forward(local_port, service=nats_service, remote_port=_A2A_NATS_CLIENT_PORT)

        # Same establishment retry as the api path's, for the same reason: a
        # tunnel that cannot come up is the run class, not an answer.
        transport_failures = 0
        while True:
            try:
                _tunnel(reset=False)
                break
            except RuntimeError as exc:
                transport_failures += 1
                _log.warning(
                    "a2a: port-forward to %s failed to establish (%d/%d): %s",
                    nats_service,
                    transport_failures,
                    _MAX_TRANSPORT_FAILURES,
                    exc,
                )
                if transport_failures >= _MAX_TRANSPORT_FAILURES:
                    return _infra_failure(
                        f"the bus tunnel to svc/{nats_service} failed to establish "
                        f"{transport_failures} times running; last failure: {exc}"
                    )
        if not url:
            url = f"{_A2A_LOOPBACK_URL}:{local_port}"

        try:
            password = _a2a_password(nats_service)
        except RuntimeError as exc:
            # No credential means no bus on this install (mode today, or the
            # stack never rendered): infrastructure, and not worth a retry.
            return _infra_failure(str(exc))

        client = a2a.BusClient(url=url, password=password, addressee=addressee)
        # Every task an attempt submitted and then lost, the status turns'
        # included. One list for the whole run, so it reaches the record
        # whether the run ends in an answer or in infrastructure.
        abandoned: list[str] = []

        def _submit(
            text: str,
            *,
            budget: float,
            context_id: str | None = None,
            correlation_id: str | None = None,
            max_attempts: int = _MAX_TRANSPORT_FAILURES,
        ) -> tuple[a2a.TaskIds, a2a.Exchange]:
            """One prompt to a terminal, through the transport retry.

            Every attempt is a new task id: the principal has no replay, so
            an attempt whose connection dropped cannot resume the task it
            submitted. Such a task is cancelled by the next attempt, or on
            the way out when there is none, and its id is added to
            ``abandoned`` either way. Every attempt also gets the full
            ``budget``, as each re-POST on the api path does: the fault the
            retry absorbs must not turn the time it consumed into a graded
            timeout on the new task.
            """
            attempts = 0
            uncancelled: list[a2a.TaskIds] = []
            while True:
                ids = a2a.mint_ids(context_id=context_id, correlation_id=correlation_id)
                attempts += 1
                until = time.monotonic() + budget
                try:
                    exchange = client.submit_and_await(
                        ids,
                        text,
                        accept_timeout=accept_timeout,
                        deadline=until,
                        cancel_first=tuple(uncancelled),
                    )
                except a2a.BusUnavailable as exc:
                    uncancelled = [t for t in uncancelled if t.task_id not in exc.cancelled]
                    if exc.submitted:
                        abandoned.append(ids.task_id)
                        uncancelled.append(ids)
                    _log.warning(
                        "a2a: attempt %d/%d for task %s failed in transport: %s",
                        attempts,
                        max_attempts,
                        ids.task_id,
                        exc,
                    )
                    if not exc.retryable or attempts >= max_attempts:
                        # No next attempt will carry these cancels: publish
                        # them now, so an executor is not left working on a
                        # task nobody awaits after the run has moved on.
                        _cancel_outstanding(uncancelled, respawn=True)
                        raise
                    try:
                        _tunnel(reset=True)
                    except RuntimeError as pf_exc:
                        # Counted, not raised: the ceiling above ends it.
                        _log.warning("a2a: port-forward respawn failed before retry: %s", pf_exc)
                else:
                    _cancel_outstanding(
                        [t for t in uncancelled if t.task_id not in exchange.cancelled],
                        respawn=False,
                    )
                    return ids, exchange

        def _cancel_outstanding(tasks: list[a2a.TaskIds], *, respawn: bool) -> None:
            if not tasks:
                return
            if respawn:
                # The attempt that abandoned these is the one whose tunnel
                # just dropped, and the cancel dials the same URL: stand the
                # forward up again first, or the cancel goes down the dead
                # listener and the task runs on for nobody.
                try:
                    _tunnel(reset=True)
                except RuntimeError as pf_exc:
                    _log.warning("a2a: port-forward respawn failed before the cancel: %s", pf_exc)
            taken = client.cancel(tasks, "abandoned by an exhausted retry")
            left = [t.task_id for t in tasks if t.task_id not in taken]
            if left:
                _log.warning(
                    "a2a: no cancel reached task(s) %s; an executor may still be working on them",
                    ", ".join(left),
                )

        try:
            ids, exchange = _submit(prompt, budget=timeout)
        except a2a.BusUnavailable as exc:
            failure = _infra_failure(f"the bus exchange failed: {exc}")
            failure.metadata["abandoned_tasks"] = abandoned
            return failure

        if _nothing_ran(exchange):
            # Nothing ran the submission: no executor for the addressee is
            # running, or the bridge accepted the task and left it queued
            # behind its workers for the whole budget. Neither is an answer,
            # and no judge should see the record as one. The run's deadline
            # can fall before the accept bound does -- a short
            # AGENT_HTTP_TIMEOUT, or a retry late in the budget -- and an
            # empty fold at the deadline is the same fact.
            if exchange.outcome == a2a.OUTCOME_NOT_ACCEPTED:
                what = (
                    f"no executor accepted task {ids.task_id} on "
                    f"{a2a.task_in_subject(addressee, ids.task_id)} within {accept_timeout:.0f}s"
                )
            elif not exchange.fold.accepted:
                what = (
                    f"no executor accepted task {ids.task_id} on "
                    f"{a2a.task_in_subject(addressee, ids.task_id)} "
                    f"before the run's {timeout:.0f}s deadline"
                )
            else:
                what = (
                    f"an executor accepted task {ids.task_id} and left it queued at "
                    f"{a2a.STATE_SUBMITTED!r} for the run's whole {timeout:.0f}s budget"
                )
            failure = _infra_failure(f"{what}; {_cancel_note(exchange)}")
            failure.metadata["abandoned_tasks"] = abandoned
            return failure

        lost = _executor_lost(exchange)
        if lost:
            # The executor lost the task rather than the persona failing it:
            # the bridge's or the worker adapter's own reason on the
            # terminal, or a rejected submission, which is the harness's
            # defect. The design of record classifies these as infrastructure
            # on both transports; a record here would grade a broken executor
            # as a 0.0 for the agent.
            failure = _infra_failure(f"executor lost task {ids.task_id}: {lost}")
            failure.metadata["abandoned_tasks"] = abandoned
            return failure

        result = _a2a_result(exchange, ids, addressee)
        # The same list the status turns append to: the record sees theirs too.
        result.metadata["abandoned_tasks"] = abandoned
        if exchange.outcome == a2a.OUTCOME_DEADLINE:
            result.errors.append(
                f"task {ids.task_id} did not reach a terminal state within {timeout:.0f}s "
                f"(last state {exchange.fold.state or 'none'!r}); {_cancel_note(exchange)}"
            )
        elif exchange.fold.state != a2a.STATE_COMPLETED:
            detail = f": {exchange.fold.status_message}" if exchange.fold.status_message else ""
            result.errors.append(f"task {ids.task_id} ended {exchange.fold.state}{detail}")

        # A task cancelled at the run's deadline has spent the budget the
        # wait would draw on, and its record already carries the error; the
        # api path never reaches its wait after a timeout either.
        if delegation_timeout > 0 and exchange.outcome != a2a.OUTCOME_DEADLINE:

            def _follow_up(poll: str, turn_timeout: float) -> tuple[AgentResult, str]:
                """A status turn: a follow-up task on the same context and
                correlation, the way a second message in the same chat
                thread would be. One attempt: the wait retries a failed turn
                itself, through the same reset, so the ceiling is the wait's
                rather than the wait's times this one's."""
                try:
                    turn_ids, turn_exchange = _submit(
                        poll,
                        budget=turn_timeout,
                        context_id=ids.context_id,
                        correlation_id=ids.correlation_id,
                        max_attempts=1,
                    )
                except a2a.BusUnavailable as exc:
                    # A refused credential or subject is never an executor's
                    # answer, and no fresh tunnel mends it; left as a plain
                    # non-retryable error the wait would read it as the api
                    # path's "a handler answered" and grade the receipt.
                    raise _TransportError(
                        str(exc), retryable=exc.retryable, fatal=not exc.retryable
                    ) from exc
                if _nothing_ran(turn_exchange):
                    raise _TransportError(
                        f"no executor ran status turn {turn_ids.task_id}",
                        retryable=True,
                    )
                lost = _executor_lost(turn_exchange)
                if lost:
                    # The wait's retry asks again; the ceiling ends it as
                    # infrastructure, the same as a turn nobody ran.
                    raise _TransportError(
                        f"executor lost status turn {turn_ids.task_id}: {lost}",
                        retryable=True,
                    )
                return _a2a_result(turn_exchange, turn_ids, addressee), ""

            def _respawn_tunnel() -> None:
                _tunnel(reset=True)

            try:
                self._a2a_delegation_wait(
                    result,
                    follow_up=_follow_up,
                    reset=_respawn_tunnel,
                    timeout=timeout,
                    delegation_timeout=delegation_timeout,
                    poll_interval=poll_interval,
                )
            except _DelegationTransportExhausted as exc:
                failure = _infra_failure(str(exc))
                failure.metadata["abandoned_tasks"] = abandoned
                return failure
        return result

    def _a2a_delegation_wait(
        self,
        result: AgentResult,
        *,
        follow_up: Callable[[str, float], tuple[AgentResult, str]],
        reset: Callable[[], None],
        timeout: float,
        delegation_timeout: float,
        poll_interval: float,
    ) -> None:
        """The case runner's kanban poll for delegated cases behind the bridge.

        Under ``spec.mode: next`` the platform persona still delegates by
        filing a kanban card, and the bridge's terminal means the turn ended,
        not the work. So the same wait the api path runs
        (:meth:`_await_delegated_work`) runs here, asking over the bus. It is
        the harness's, not the transport's: the transport submits and awaits
        a task id and never polls the model. When agent-initiated delegation
        becomes a child task on the bus, the parent's events will name the
        child's task id, ``BusClient.await_terminal`` awaits it, and this
        method is deleted.

        Card ids are read from the trajectory, which on this path carries
        tool calls only once the executor publishes ``activity`` artifacts;
        until then the wait finds nothing outstanding and settles at once.
        """
        self._await_delegated_work(
            result,
            turn=follow_up,
            reset=reset,
            timeout=timeout,
            delegation_timeout=delegation_timeout,
            poll_interval=poll_interval,
        )

    def _await_delegated_work(
        self,
        result: AgentResult,
        *,
        turn: Callable[[str, float], tuple[AgentResult, str]],
        reset: Callable[[], None],
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
        to. ``turn`` issues one status turn -- on the api transport a re-POST
        of the same stateful ``conversation`` so the agent keeps its context,
        on the a2a transport a follow-up task on the same context -- and
        returns the parsed reply with its session id; ``reset`` respawns the
        transport's tunnel between failed turns. Cards filed *during* a
        status turn join the wait.

        A turn that fails in transport is retried up to
        :data:`_MAX_TRANSPORT_FAILURES` times running -- through a fresh
        tunnel each time, like the opening turn -- and one reporting no
        outstanding card is tolerated up to :data:`_MAX_SILENT_TURNS`.

        Returns:
            The session id from the last status turn, or ``""`` when no status
            turn ran or the header was absent.

        Raises:
            _DelegationTransportExhausted: Every retry died without reaching
                an agent -- no HTTP answer at all, or a 429 refused at the
                admission door -- or the transport refused the harness
                outright (a2a: the bus rejected the credential or the
                subject); the run is infrastructure, not a gradable result.
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
            _log.info(
                "waiting %.0fs on delegated tasks: %s",
                poll_interval,
                ", ".join(outstanding),
            )
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
                status_turn, turn_session = turn(poll, min(timeout, remaining))
            except _TransportError as exc:
                transport_failures += 1
                _log.warning(
                    "status turn failed (%d/%d): %s",
                    transport_failures,
                    _MAX_TRANSPORT_FAILURES,
                    exc,
                )
                if exc.fatal:
                    # The transport refused the harness outright (a2a: the
                    # bus rejected the credential or the subject, as it does
                    # once an operator reconciles without the eval flag and
                    # rolls the bus under a run). Not the agent's answer and
                    # not mended by a retry: the exhausted retry's road below,
                    # purge included, taken at once.
                    _purge_card_state(awaited, _EXEC_TIMEOUT)
                    raise _DelegationTransportExhausted(
                        f"status turn refused in transport: {exc}; "
                        "still waiting on: " + ", ".join(outstanding)
                    ) from exc
                if transport_failures < _MAX_TRANSPORT_FAILURES:
                    # Back off one poll interval and ask again: the loop top
                    # re-checks the deadline, so retries cannot outlive it.
                    # A retryable failure usually means the endpoint never
                    # answered, and the tunnel is the prime suspect (build
                    # 2092638061140643840: three 502s over a live listener
                    # whose upstream pod had been replaced), so it is torn
                    # down and respawned first, exactly like the opening
                    # turn. The exception is 429, the one retryable status
                    # the endpoint itself sends: the tunnel it arrived
                    # through is healthy, and the respawn's few seconds are
                    # merely pacing before the slot is asked for again.
                    if exc.retryable:
                        try:
                            reset()
                        except RuntimeError as pf_exc:
                            # Counted, not raised: a forward that will not
                            # come back is the same outage, and the ceiling
                            # ends it.
                            _log.warning(
                                "port-forward respawn failed before retry: %s", pf_exc
                            )
                    continue
                if exc.retryable:
                    # Classified, not graded: appending here used to leave the
                    # run validating with the delegation receipt graded as the
                    # answer -- the exact failure this wait exists to prevent.
                    # The cards' on-disk state still has to go (nothing is
                    # settled into a record that is about to be replaced, but
                    # a rerun must not find this attempt's leavings), then the
                    # run becomes infrastructure, mirroring the opening turn.
                    _purge_card_state(awaited, _EXEC_TIMEOUT)
                    raise _DelegationTransportExhausted(
                        f"status turns failed in transport {transport_failures} times "
                        "running; still waiting on: " + ", ".join(outstanding)
                    ) from exc
                # A handler answered every time (a non-429 4xx, a 500,
                # non-JSON): that is the agent's own failure, so it stays in
                # front of the judge as before -- recorded, not just logged,
                # which is what stops devops-bench promoting the partial
                # record.
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
            fresh_reported = reported_statuses(new_calls(status_turn, seen_calls))
            _fold_status_turn(
                result,
                status_turn,
                settled=any(s in _TERMINAL_STATUSES for s in fresh_reported.values()),
            )
            # ``observed`` needs each distinct result once, so it stays on the
            # content test -- a replayed reading adds nothing to the answer.
            observed.extend(merge_new(observed, status_turn.trajectory))
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
            statuses.update(reported_statuses(status_turn.trajectory))
            # dict.fromkeys: order-preserving dedupe, so a card the agent
            # re-filed under the same id is awaited once. The overflow is
            # reported only the first time, since the replayed trajectory
            # re-offers the dropped ids on every poll.
            merged = list(dict.fromkeys(awaited + delegated_task_ids(status_turn.trajectory)))
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
        result.metadata["worker_commands"] = _worker_commands(awaited, _EXEC_TIMEOUT)
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
