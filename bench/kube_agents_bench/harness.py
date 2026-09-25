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
        artifacts and its workers' session stores, and when clearing its state
        (default ``platform-agent``).
    AGENT_MODEL_NAME: ``model`` field sent to the endpoint (default
        ``model-default``, the name the operator pins on ``/v1/models`` via
        ``API_SERVER_MODEL_NAME`` and the one LiteLLM actually serves).
    AGENT_CONVERSATION_ID: Pins the ``conversation`` field. Unset (the default)
        generates a fresh id per invocation so each task's trajectory is
        isolated on this stateful endpoint.
    AGENT_HTTP_TIMEOUT: Per-request timeout in seconds (default ``600``).
    AGENT_DELEGATION_TIMEOUT: Total seconds to wait for delegated work across
        all status turns (default ``1800``). ``0`` disables waiting, restoring
        the single-turn behaviour. A card still running when it elapses is
        archived on the agent's board, which stops its worker.
    AGENT_DELEGATION_POLL_INTERVAL: Seconds between board reads while delegated
        work is awaited (default ``30``). Each read is a ``kubectl exec``
        against the agent's kanban store; a status turn through the model is
        made only when a read shows a card settled, or when the board cannot
        be read at all.
    ARTIFACTS: Set by Prow to the directory it uploads. When set, each unit's
        port-forward stderr goes to ``<ARTIFACTS>/port-forward/`` and a card
        that ran to the delegation ceiling leaves its worker transcript under
        ``<ARTIFACTS>/worker-logs/``; unset, the stderr goes to a temp directory
        and no transcript is kept.
    PLATFORM_AGENT_TOKEN: Bearer token for the endpoint.

    AGENT_TRANSPORT: ``api`` (default; everything above) or ``inject``, which
        hands the prompt to the agent through the A2A gateway's inject backend
        instead -- ``POST /inject`` with the prompt, then the conversation's
        transcript folded until the task's terminal, the way the gateway
        drives a customer conversation under ``spec.mode: next``
        (:mod:`kube_agents_bench.inject_transport`). Same harness, same
        ``AgentResult``, same transcript stash for the verifiers. The inject
        path reads ``AGENT_NAMESPACE``, ``AGENT_CLUSTER_CONTEXT``, the
        delegation variables, and:
    AGENT_INJECT_SERVICE: The Service to port-forward to (default
        ``<AGENT_SERVICE_NAME>-a2a-inject``, which is what the operator
        renders under its eval flag).
    AGENT_INJECT_LOCAL_PORT: Local side of that port-forward (default
        ``28099``). The remote side is always the Service's 8099.
    AGENT_INJECT_URL: A gateway base URL to use instead of spawning a
        port-forward.
    AGENT_INJECT_TOKEN: The door's bearer token, required. The operator
        renders it into the ``<agent>-a2a-inject`` Secret under its eval
        flag, and this is read the way the presubmit reads the agent's own
        key out of ``platform-agent-secrets``::

            AGENT_INJECT_TOKEN=$(kubectl get secret platform-agent-a2a-inject \
              -n kubeagents-system -o jsonpath='{.data.token}' | base64 --decode)

    AGENT_INJECT_AUTHOR: The author id the message is sent as, resolved
        through the door's own principal map (default ``devops-bench``, the
        entry the operator renders).
    AGENT_INJECT_TIMEOUT: Seconds one task may run before the harness reads
        the conversation's record and classifies it (default ``1800``). This
        is the whole task's budget, not a request's: on the api transport
        ``AGENT_HTTP_TIMEOUT`` bounds one POST and the work's real budget is
        ``AGENT_DELEGATION_TIMEOUT``, whereas here a single await covers the
        work, so it takes the same scale as the latter. Its floor is the
        gateway's own first-event grace plus a margin, learned from a read
        of the fresh conversation before the POST, and a budget below the
        floor is refused before anything is started
        (:func:`~kube_agents_bench.inject_transport.check_budget`). There is
        no variable for "how long before I decide nobody took this": that
        window is the gateway's, and the harness reads it off the read
        route. At the deadline nothing is sent -- one more read classifies
        the task, and every outcome that leaves an active task is then
        followed by a cancel naming it, which bounds a stray run and never
        changes the classification (see
        :meth:`KubeAgentsHarness._execute_inject`).

    The conversation key and the backend message id are not configurable.
    Both are ``<run>/<case>/<rep>``: the run id is a ``devops-bench-<hex>``
    id minted fresh per invocation (``AGENT_CONVERSATION_ID`` is the api
    path's and is not honoured here: a pinned id would have the door's
    dedupe answer a rerun with a previous invocation's task and terminal),
    and the case and repetition are ``EVAL_CASE_ID`` and ``EVAL_REPETITION``
    as the presubmit exports them (``adhoc`` and ``1`` outside it). The
    gateway prefixes the key with ``inject:``. The triple is what makes the
    key fresh per case and repetition, the message id unique per invocation
    -- the door dedupes a retried POST on it -- and the ingress log joinable
    to the eval record, which stores the three beside the task id. Each
    status turn of the delegation wait carries its own id,
    ``<run>/<case>/<rep>/status-<n>``, so the dedupe does not fold it into
    the opening task.
"""

from __future__ import annotations

import atexit
import fcntl
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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from devops_bench.agents import AgentHarness, AgentResult
from devops_bench.agents.result import empty_tokens

from kube_agents_bench import inject_transport as inject
from kube_agents_bench import board, transcript, worker_trajectory
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

# The two doors ``AGENT_TRANSPORT`` selects between. ``api`` is the agent's own
# HTTP endpoint, which is identical under both modes and therefore says nothing
# about the next stack; ``inject`` goes through the A2A gateway, which is the
# stack the mode renders.
TRANSPORT_API = "api"
TRANSPORT_INJECT = "inject"
_TRANSPORTS = frozenset({TRANSPORT_API, TRANSPORT_INJECT})

# The inject transport's port-forward target: the Service the operator renders
# for the gateway's inject backend under its eval flag, listening on 8099. The
# local side defaults well away from it so a gateway a developer is already
# forwarding cannot be mistaken for this tunnel.
_INJECT_SERVICE_SUFFIX = "-a2a-inject"
_INJECT_REMOTE_PORT = 8099
_INJECT_DEFAULT_LOCAL_PORT = 28099
# The Secret the operator renders the door's bearer token into, named in the
# message a run without the token fails with. Suffix and key, so the message
# can spell the whole kubectl read.
_INJECT_TOKEN_SECRET_SUFFIX = "-a2a-inject"
_INJECT_TOKEN_SECRET_KEY = "token"  # noqa: S105 -- a Secret key name, not a credential
# Appended to the run's message id on a delegation status turn, with the
# turn's ordinal after it, so the ingress log tells the opening ask and each
# follow-up apart -- and so the door's dedupe on the message id cannot fold a
# second status turn into the first one's task.
_INJECT_STATUS_TURN_SUFFIX = "/status-"
# The three budgets both transports share, named here rather than spelled at
# each _numeric_env call: one default per knob, or the two doors disagree
# about how long a case may take.
_DEFAULT_HTTP_TIMEOUT = "600"
_DEFAULT_DELEGATION_TIMEOUT = "1800"
_DEFAULT_DELEGATION_POLL_INTERVAL = "30"
# What ``tokens`` say on an inject record: the gateway reports no usage, and a
# null bucket is the truthful value rather than a zero (``scoring.py`` reads
# the task's final ``a2a.status-update`` entry in the trajectory as the
# liveness signal instead).
_INJECT_TOKENS_NOTE = "the inject transport carries no token usage; every bucket is null"
# The case and repetition the presubmit is running (hack/ci-eval-pr.sh exports
# both), which with the run id become the conversation key and the backend
# message id so the gateway's ingress log joins to the eval record. Outside
# the presubmit neither is set; the fallbacks keep the key well-formed, and
# the run id alone keeps two concurrent runs apart.
_EVAL_CASE_ID_ENV = "EVAL_CASE_ID"
_EVAL_REPETITION_ENV = "EVAL_REPETITION"
_EVAL_CASE_FALLBACK = "adhoc"
_EVAL_REPETITION_FALLBACK = "1"
# The agent Service and namespace the inject path assumes when the presubmit
# has not said otherwise: the operator's defaults, which name the inject
# Service (<service>-a2a-inject) and the token Secret beside it.
_DEFAULT_AGENT_SERVICE_NAME = "platform-agent"
_DEFAULT_AGENT_NAMESPACE = "kubeagents-system"
# The per-invocation run id: the api path's stateful ``conversation`` field
# when AGENT_CONVERSATION_ID is unset, and always the inject path's key and
# message id.
_RUN_ID_PREFIX = "devops-bench-"
_RUN_ID_HEX_WIDTH = 12
# How long one injected task may run before the harness reads the record and
# classifies it. The api path's AGENT_HTTP_TIMEOUT is a PER-REQUEST bound
# there, with AGENT_DELEGATION_TIMEOUT covering the work across status turns;
# on this path one await covers the work, so the budget has to be of the
# second kind. Borrowing the first cut every case to 600s, which a ten-minute
# case reached with no terminal and an empty answer. The floor under it is
# the gateway's grace plus a margin, and the harness refuses to start below
# it. Its own budget, not the grace: the bridge queues a task (``submitted``)
# behind its concurrency cap and spawns it (``working``) when a slot frees,
# so a deadline of grace plus margin would grade every queued unit of a
# parallel run as a timeout.
_INJECT_DEFAULT_TIMEOUT = "1800"
# Leads the deadline error when the delegation wait ran out and no awaited
# card had delivered anything: the graded output is then the front door's
# acknowledgement alone, which is no answer to grade for or against the agent
# under test. ``scoring.py`` matches this string and classifies the repetition
# as its own infrastructure class, apart from the transport marker above (the
# agent was reached and its worker was still running). A ceiling hit after a
# partial delivery -- a fan-out with some cards finished -- carries no marker
# and grades as before. Duplicated in ``scoring.py`` for the same reason as
# the marker above; ``test_scoring.py`` asserts the two strings agree.
DELEGATION_CEILING_MARKER = "KUBE_AGENTS_DELEGATION_CEILING"

# Where hermes keeps per-card state in the agent's data volume. A card's
# attachments hold the files its worker produced -- the deliverable itself on a
# task that asks for a written report -- and its log holds the worker's whole
# transcript. Both outlive the card: deleting it from the board drops the row
# and leaves these, so the next run of the same task can find the previous run's
# finished answer by searching the filesystem.
_ATTACHMENTS_DIR = "/opt/data/kanban/attachments"
_LOGS_DIR = "/opt/data/kanban/logs"
# The hermes CLI in the agent pod, for when it is not on the exec shell's PATH.
_HERMES_BIN_FALLBACK = "/opt/hermes/.venv/bin/hermes"
# What ``hermes kanban archive <id>`` prints on success. A refusal goes to
# stderr with exit 1, so the archive script folds stderr in and exits 0:
# ``_agent_shell`` returns nothing for a non-zero exit, and the reason is
# what the warning is for.
_ARCHIVED_PREFIX = "Archived "
# How much of a refused archive's reply the warning quotes.
_LOG_EXCERPT_CHARS = 200
# One terminal command per line in a card's worker log, as hermes renders it:
# ``  ┊ 💻 $         <command>  0.6s [exit 1]``. The timing and exit suffixes
# are stripped; the command is kept verbatim otherwise.
_WORKER_COMMAND_RE = re.compile(
    r"💻 \$\s+(?P<command>.+?)(?:\s+\d+(?:\.\d+)?s(?: \[exit \d+\])?)?\s*$"
)
_MAX_WORKER_LOG_BYTES = 512_000
# Where a stalled card's transcript is kept. Prow sets ARTIFACTS to the
# directory it uploads; a local run leaves it unset and nothing is written.
_ARTIFACTS_ENV = "ARTIFACTS"
_WORKER_LOG_SUBDIR = "worker-logs"
_PF_LOG_SUBDIR = "port-forward"
# Written at the top of each spawn's stderr in the (appended) port log.
_PF_SPAWN_MARKER = "--- port-forward spawned"
_PF_SPAWN_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Bound on artifact text folded into one answer. The judge grades the output as
# prose, so a worker that writes a large file would otherwise bury the reply.
_MAX_ARTIFACT_BYTES = 20000

# Ceiling on files read back from one run's cards. A card is expected to produce
# a report, not a directory tree, and each file costs a round trip.
_MAX_ARTIFACTS = 8

# Ceiling on one kubectl exec. Reading a capped file, querying a session store
# for one run's cards or deleting a handful of directories is near-instant;
# anything slower is a cluster problem, and every caller would rather give up
# than hold the run open.
_EXEC_TIMEOUT = 60.0

_PF_LOCK = threading.Lock()  # guards the three registries below
_PF_PROCESSES: dict[int, subprocess.Popen[bytes]] = {}
_PF_PORT_LOCKS: dict[int, threading.Lock] = {}
_PF_LOG_DIR: Path | None = None
# True while _PF_LOG_DIR is this process's own temp directory, and therefore
# ours to delete. Under ARTIFACTS it is Prow's, and deleting it at exit would
# take the logs with it -- the whole point of putting them there.
_PF_LOG_DIR_IS_TEMP = True


def _port_establishment_lock(port: int) -> threading.Lock:
    with _PF_LOCK:
        return _PF_PORT_LOCKS.setdefault(port, threading.Lock())


def _pf_log_dir() -> Path:
    """Where each port's kubectl stderr is written.

    Under Prow this is a subdirectory of ARTIFACTS, so a tunnel that dies
    mid-run leaves a record: every unit gets its own port and so its own log,
    and none of them survived the temp directory before (#1764). A local run,
    or an ARTIFACTS that will not take a directory, falls back to the temp
    directory this has always used.
    """
    global _PF_LOG_DIR, _PF_LOG_DIR_IS_TEMP
    with _PF_LOCK:
        if _PF_LOG_DIR is None:
            artifacts = os.environ.get(_ARTIFACTS_ENV)
            if artifacts:
                target = Path(artifacts) / _PF_LOG_SUBDIR
                try:
                    target.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    _log.warning("could not use %s for port-forward logs: %s", target, exc)
                else:
                    _PF_LOG_DIR = target
                    _PF_LOG_DIR_IS_TEMP = False
            if _PF_LOG_DIR is None:
                _PF_LOG_DIR = Path(tempfile.mkdtemp(prefix="kubeagents-pf-"))
                _PF_LOG_DIR_IS_TEMP = True
        return _PF_LOG_DIR


def _pf_log_path(port: int) -> Path:
    """One log per port. Each eval unit owns its own tunnel on its own port
    (``AGENT_LOCAL_PORT``), so this keeps the units' stderr apart."""
    return _pf_log_dir() / f"pf-{port}.log"


def _tail(path: Path, max_bytes: int = 2048) -> str:
    """Last ``max_bytes`` of ``path``, embedded in the error rather than linked.

    The file outlives the run under ARTIFACTS but not in a temp directory, and
    an error message a reader can act on without a second lookup is worth the
    duplication either way.
    """
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
        # Only ours to delete. Under ARTIFACTS the directory is Prow's upload
        # staging area, and removing it here would drop the logs seconds before
        # they are collected.
        if _PF_LOG_DIR is not None and _PF_LOG_DIR_IS_TEMP:
            shutil.rmtree(_PF_LOG_DIR, ignore_errors=True)
        _PF_LOG_DIR = None


def _kubectl_target(service: str | None = None) -> list[str]:
    """The service, namespace and context flags shared by every kubectl call.

    ``service`` defaults to the agent's own Service; the inject transport
    passes the gateway's inject Service, which lives in the same namespace and
    the same context.
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
        # errors="replace": a transcript cut mid-glyph by ``head -c`` must not
        # raise out of the read and skip the purge that follows it.
        proc = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace", timeout=timeout, check=False
        )
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
    inject transport forwards the gateway's inject Service instead.

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
        stderr_log = _pf_log_path(local_port)
        try:
            # Append, with a marker per spawn: a respawn after a dead tunnel
            # must not erase the stderr that says why the last one died.
            with open(stderr_log, "ab") as log_file:
                log_file.write(
                    f"{_PF_SPAWN_MARKER} {time.strftime(_PF_SPAWN_TIME_FORMAT, time.gmtime())}\n".encode()
                )
                log_file.flush()
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
    # The agent's endpoint stays the positional-only call the api path has
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
# finished, blocked needs a human, and failed and cancelled are hermes' own
# terminal failures (agents/platform/scripts/kanban_workspace_gc.py names the
# same set). The other hermes statuses (triage, todo, ready, running) still
# have a worker or the dispatcher behind them. A failed card left out of this
# set would run the wait to its ceiling and grade as the harness's, not the
# worker's.
_TERMINAL_STATUSES = frozenset({"done", "archived", "blocked", "failed", "cancelled"})

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

    The worker runs as a separate hermes session, so its card result is the
    part of its work that crosses back through the conversation (its tool
    calls are read from its session store separately, see
    :mod:`kube_agents_bench.worker_trajectory`); without this the graded
    answer is only the router's closing message. ``observed`` is the status turns' trajectory
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
# Index state for a card whose transcript could not be read at all (the exec
# failed): the file may exist, and its worker may have run.
_LOG_UNREAD = "__WORKER_LOG_UNREAD__"
# Index state for a transcript that was read but whose copy under ARTIFACTS
# could not be written: the stat is real, the file beside the index is not.
_LOG_UNWRITTEN = "__WORKER_LOG_UNWRITTEN__"
# ``stat -c`` is GNU and busybox; both ship it, and a pod whose image has
# neither still yields the sentinel, so the body is read either way.
_STAT_FORMAT = "%Y %s"
# Printed for mtime and size when ``stat`` itself failed. Distinct from the
# absent sentinel: the file is there, its metadata is not.
_STAT_UNKNOWN = "-"
# One row per stalled card, appended: every repetition in a build shares one
# ARTIFACTS directory, so a file written whole would keep only the last stall.
# The header is written once, by whichever repetition finds the file empty.
_WORKER_LOG_INDEX = "index.txt"
_WORKER_LOG_INDEX_HEADER = "card\tmtime_epoch\tsize_bytes\tstate"


@dataclass(frozen=True)
class _WorkerLog:
    """One card's worker transcript and the metadata of the file it came from.

    ``mtime`` and ``size`` are read in the same ``kubectl exec`` as the body,
    before :func:`_purge_card_state` deletes the file, because they answer a
    question the body cannot: an mtime frozen near claim time says the worker
    died or wedged as it started, and one still advancing at the ceiling says
    it was alive and the wait is elsewhere. Both are the raw strings ``stat``
    printed -- epoch seconds and bytes -- or ``_STAT_UNKNOWN``.
    """

    body: str
    mtime: str
    size: str


def _worker_logs(task_ids: list[str], timeout: float) -> dict[str, _WorkerLog | None] | None:
    """Each delegated card's worker transcript, keyed by card id.

    The worker is a separate hermes session; its tool calls reach
    ``result.trajectory`` only through the session-store read in
    :mod:`kube_agents_bench.worker_trajectory`, tagged so that
    ``ToolCalledVerifier`` skips them, and its log records each terminal
    command it executed. Only terminal commands are visible; MCP tool calls
    are not.

    Read here, before ``_purge_card_state`` deletes the logs, and consumed
    twice: by :func:`_worker_commands` for the ``worker_commands`` verifier --
    the one check that can say which route a worker took, not only what it
    answered -- and by :func:`_dump_worker_logs` for the run artifacts. One
    read serves both; each is a ``kubectl exec`` into the agent pod.

    A card whose log could not be read at all maps to ``None``. ``_agent_shell``
    returns ``""`` for a kubectl that failed as readily as for an empty file,
    and the first time this ran, a credential hiccup on the runner turned a
    worker that had run dozens of commands into "0 command(s)" -- which
    failed the required pattern for the wrong reason and passed the forbidden
    one for no reason. The script therefore prints a sentinel before the log
    (or a different one when the file is absent), and a reply carrying
    neither is a capture failure: :func:`_worker_commands` reports it as
    ``status="error"`` rather than grading, while the cards read before and
    after it stay in the map for the dump. A card that simply has no log is
    absent from the map, which is not a failure. ``None`` for the whole map
    means nothing was read because no card was delegated.

    The sentinel line carries the file's mtime and size, so one exec returns
    both the transcript and the metadata :class:`_WorkerLog` documents. A
    ``stat`` that fails degrades to ``_STAT_UNKNOWN`` rather than losing the
    body with it.
    """
    # No card, no capture: a router that answered from memory leaves nothing
    # to read, and grading an empty list would let a forbidden-pattern check
    # pass on a run where no worker ran -- silence as a pass. Review caught
    # this returning [] here.
    if not task_ids:
        return None
    logs: dict[str, _WorkerLog | None] = {}
    for tid in task_ids:
        path = _shell_quote(f"{_LOGS_DIR}/{tid}.log")
        unknown = f"{_STAT_UNKNOWN} {_STAT_UNKNOWN}"
        script = (
            f"if [ -f {path} ]; then "
            f"echo {_LOG_PRESENT} \"$(stat -c '{_STAT_FORMAT}' {path} 2>/dev/null "
            f'|| echo "{unknown}")"; head -c {_MAX_WORKER_LOG_BYTES} {path}; '
            f"else echo {_LOG_ABSENT}; fi"
        )
        text = _agent_shell(script, timeout)
        first, _, body = text.partition("\n")
        # The sentinel is the first field; stat's two follow it when the file
        # is present, so split on whitespace rather than comparing the line.
        fields = first.split()
        marker = fields[0] if fields else ""
        if marker == _LOG_ABSENT:
            continue
        if marker != _LOG_PRESENT:
            # Keep going: one unread card errors the verifier, but the dump
            # wants every transcript that was in hand.
            _log.warning("worker log for %s could not be read; route checks will error", tid)
            logs[tid] = None
            continue
        mtime, size = (fields + [_STAT_UNKNOWN, _STAT_UNKNOWN])[1:3]
        logs[tid] = _WorkerLog(body=body, mtime=mtime, size=size)
    return logs


def _worker_commands(logs: dict[str, _WorkerLog | None] | None) -> list[dict[str, str]] | None:
    """Every terminal command the delegated workers ran, from their card logs.

    The worker is a separate hermes session and its tool calls never reach
    ``result.trajectory`` (see ``ToolCalledVerifier``), but its log records
    each terminal command it executed -- the one check that can say which
    route a worker took, not only what it answered. Only terminal commands
    are visible; MCP tool calls are not.

    ``None`` when :func:`_worker_logs` read nothing or failed on any card: a
    capture that failed, or a run that delegated nothing, is not a run whose
    workers issued no commands, and a partial capture must not be graded.
    """
    if logs is None:
        return None
    commands: list[dict[str, str]] = []
    for tid, log in logs.items():
        if log is None:
            return None
        for line in log.body.splitlines():
            match = _WORKER_COMMAND_RE.search(line)
            if match:
                commands.append({"task": tid, "command": match.group("command").strip()})
    return commands


def _dump_worker_logs(logs: dict[str, _WorkerLog | None] | None, stalled: Sequence[str]) -> None:
    """Write the transcript of every card that ran to the ceiling into ARTIFACTS.

    Without this a stalled card would leave nothing behind: :func:`_worker_commands`
    keeps its shell lines, the rest is dropped, and :func:`_purge_card_state`
    deletes the file -- so by the time anyone read the run, the only record of
    where the worker stopped was gone and the stall could not be root-caused.

    Written for the stalled cards alone, so a run that never hit the ceiling
    adds no files, and only when ``ARTIFACTS`` names a directory. Card ids come
    from ``delegated_task_ids``, which rejects anything carrying a path
    separator, so they are safe as file names.

    ``_WORKER_LOG_INDEX`` gets a row for every stalled card, including one
    with no transcript at all. That row is not an empty result: a stalled card
    with no file never had a worker, which is a different fault from a worker
    that started and went quiet, and a missing file cannot tell the two apart.
    A card whose read failed (``None`` in the map, or no map at all) is a
    third state, ``_LOG_UNREAD``, because it says nothing about whether the
    file was there; the cards read alongside it keep their transcripts. The
    transcripts are written before the index, so a ``_LOG_PRESENT`` row means
    the file is beside it; one that would not write keeps its stat under
    ``_LOG_UNWRITTEN``.
    """
    directory = os.environ.get(_ARTIFACTS_ENV)
    if not directory or not stalled:
        return
    target = Path(directory) / _WORKER_LOG_SUBDIR
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Best effort, like every other read _settle makes: an artifact
        # directory that will not take a file is not worth a failed run.
        _log.warning("could not write the worker logs: %s", exc)
        return
    found = logs or {}
    rows = []
    for tid in stalled:
        if logs is not None and tid not in found:
            rows.append(f"{tid}\t{_STAT_UNKNOWN}\t{_STAT_UNKNOWN}\t{_LOG_ABSENT}")
            continue
        log = found.get(tid)
        if log is None:
            rows.append(f"{tid}\t{_STAT_UNKNOWN}\t{_STAT_UNKNOWN}\t{_LOG_UNREAD}")
            continue
        state = _LOG_PRESENT
        try:
            (target / f"{tid}.log").write_text(log.body, encoding="utf-8")
        except OSError as exc:
            _log.warning("could not write the transcript of %s: %s", tid, exc)
            state = _LOG_UNWRITTEN
        rows.append(f"{tid}\t{log.mtime}\t{log.size}\t{state}")
    try:
        index = target / _WORKER_LOG_INDEX
        # One append-mode open under an exclusive flock. Repetitions run in
        # parallel, and a header written through a separate exclusive-create
        # handle sat at offset 0 until close, where it could overwrite the
        # first row another repetition had appended meanwhile. O_APPEND puts
        # every write at the end, and the lock makes header-then-rows one step.
        with index.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                if os.fstat(fh.fileno()).st_size == 0:
                    fh.write(_WORKER_LOG_INDEX_HEADER + "\n")
                fh.write("\n".join(rows) + "\n")
                fh.flush()
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        _log.warning("could not write the worker log index: %s", exc)


def _archive_stalled_cards(stalled: Sequence[str], timeout: float) -> None:
    """Archive the cards that ran to the ceiling, which stops their workers.

    The unit has given up on them: its record is written and their files are
    about to be deleted. Left alone, a worker that still heartbeats keeps its
    dispatcher slot until it finishes on its own (the dispatcher's stale timer
    reclaims only a silent one), and every later unit's card queues behind
    it. ``hermes kanban archive`` moves a running card to the terminal
    ``archived`` state and terminates its worker. Children the worker filed
    are not known here and keep running.

    One exec per card, so a card the dispatcher already archived does not
    fail the batch for the rest, and each kill has the whole exec timeout.
    Best effort, like the rest of :meth:`KubeAgentsHarness._settle`, but a
    card that did not archive is warned about by name, with hermes's reason:
    its worker may still hold a slot, and the run log should not say
    otherwise.
    """
    for tid in stalled:
        script = (
            f'H=$(command -v hermes || echo {_HERMES_BIN_FALLBACK}); '
            f'"$H" kanban archive {_shell_quote(tid)} 2>&1 || true'
        )
        out = _agent_shell(script, timeout).strip()
        if out.startswith(_ARCHIVED_PREFIX):
            _log.info("archived stalled card %s", tid)
        else:
            _log.warning(
                "could not archive stalled card %s; its worker may still hold a dispatcher slot (%s)",
                tid,
                out[:_LOG_EXCERPT_CHARS] or "no output",
            )


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
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


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


def _mint_run_id() -> str:
    """A fresh per-invocation run id."""
    return _RUN_ID_PREFIX + uuid.uuid4().hex[:_RUN_ID_HEX_WIDTH]


def _run_id() -> str:
    """The api path's run id: pinned by ``AGENT_CONVERSATION_ID`` or minted.

    It is the stateful ``conversation`` field, fresh per invocation so no task
    inherits the previous task's trajectory. The inject path does not use it:
    see :func:`_inject_identity`.
    """
    return os.environ.get("AGENT_CONVERSATION_ID") or _mint_run_id()


def _inject_identity() -> tuple[str, str, str]:
    """The run id, case id and repetition the inject path keys everything on.

    The run id is always minted fresh, never read from ``AGENT_CONVERSATION_ID``:
    it is the first segment of the backend message id, which the door dedupes
    on, and a pinned id would make the dedupe answer this invocation's POST
    with a previous invocation's task and replay its terminal as this run's.
    The presubmit exports the case and repetition; on a developer's machine
    neither is set and the fallbacks keep the triple well-formed. The gateway's
    ingress log joins the backend message id to the correlationId, so the
    triple there reaches the eval record with nothing else added (the A2A
    owner's ask on the design doc).
    """
    case = os.environ.get(_EVAL_CASE_ID_ENV, "").strip() or _EVAL_CASE_FALLBACK
    repetition = os.environ.get(_EVAL_REPETITION_ENV, "").strip() or _EVAL_REPETITION_FALLBACK
    return _mint_run_id(), case, repetition


def _inject_result(exchange: inject.Exchange, identity: dict[str, Any]) -> AgentResult:
    """Map a folded conversation onto the canonical result.

    ``output`` and ``final_message`` are the deliverable -- the posts the
    conversation received for this task that the relay never rewrote, which is
    what a customer would read as the answer. The trajectory is the
    conversation itself plus the task's lifecycle: the relay does not post
    ``activity`` artifacts, so no transport reading a conversation carries
    tool calls, and what it carries instead is every executor state the read
    route showed and the terminal, as ``a2a.status-update`` entries. Tokens
    stay null -- the gateway reports no usage -- and ``metadata`` says so
    rather than inventing a number. ``identity`` is the run, case and
    repetition the key and message id were built from, stored beside the
    task id so the record joins to the gateway's ingress log.
    """
    fold = exchange.fold
    return AgentResult(
        output=fold.deliverable,
        trajectory=list(fold.trajectory),
        tokens=empty_tokens(),
        errors=[],
        metadata={
            "transport": TRANSPORT_INJECT,
            "final_message": fold.deliverable,
            "task_id": exchange.task_id,
            "conversation": exchange.conversation,
            "outcome": exchange.outcome,
            **identity,
            "terminal_state": fold.terminal or None,
            "terminal_source": fold.terminal_source or None,
            "terminal_reason": fold.terminal_reason or None,
            "reason_token": fold.reason_token or None,
            "executor_states": list(fold.executor_states),
            "posts": len(fold.posts),
            "entries": len(fold.entries),
            "malformed_entries": fold.malformed,
            "tokens_note": _INJECT_TOKENS_NOTE,
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
    """Drives the in-cluster platform agent over its HTTP endpoint.

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
        if transport == TRANSPORT_INJECT:
            return self._execute_inject(prompt)

        api_path = os.environ.get("AGENT_API_PATH", "/v1/responses")
        try:
            local_port = _numeric_env("AGENT_LOCAL_PORT", str(SERVICE_API_PORT), int)
            timeout = _numeric_env("AGENT_HTTP_TIMEOUT", _DEFAULT_HTTP_TIMEOUT, float)
            delegation_timeout = _numeric_env(
                "AGENT_DELEGATION_TIMEOUT", _DEFAULT_DELEGATION_TIMEOUT, float
            )
            poll_interval = _numeric_env(
                "AGENT_DELEGATION_POLL_INTERVAL", _DEFAULT_DELEGATION_POLL_INTERVAL, float
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
            "conversation": _run_id(),
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
                        f"running; last failure: {exc}; "
                        f"tunnel log: {_tail(_pf_log_path(local_port))}"
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
                        local_port=local_port,
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

    def _execute_inject(self, prompt: str) -> AgentResult:
        """The inject transport: send the prompt through the gateway's front door.

        Mirrors ``_execute``'s retry classes. The tunnel to the inject Service
        gets the same bounded establishment retry; an exchange that never
        reaches the gateway is attempted up to :data:`_MAX_TRANSPORT_FAILURES`
        times in all through a fresh tunnel each time -- the opening POST
        with the same body and message id, which the door dedupes -- and then
        classified as infrastructure. The task's deadline is set once, when
        the POST is first accepted, and a retry rejoins the same wait rather
        than restarting the budget.

        How a task ended is classified from what the gateway's read route
        reports, never from a clock of this harness's beside the gateway's
        grace and never from a message sent to provoke it. Infrastructure,
        never graded: a task the read shows active with nothing on its stream
        past the grace (no executor took it); a task that only ever reached
        ``submitted`` by ``AGENT_INJECT_TIMEOUT`` (queued behind the bridge's
        cap for the whole budget); a terminal the gateway declared -- about
        a task it could not put on the bus, or, on the read's fold, the
        supervisor's word about an executor that died or never ran; a
        submission the door refused, because the principal map does not carry
        the author or because the gateway could not publish it; a failed
        terminal carrying one of the executors' own reasons (bridge-shutdown,
        spawn-failed and the rest of :data:`inject.INFRASTRUCTURE_REASONS`),
        a rejected one, or a canceled-before-start; and a deadline the read
        cannot classify. Graded, in front of the judge with the terminal on
        ``errors``: a task an executor took and ended ``failed`` or
        ``canceled`` for any other reason, a task still ``working`` at the
        budget, graded on what it produced, and a task whose record the
        relay left active on a finished stream, graded from the fold's
        result text.

        Every outcome that leaves an active task -- working or queued at the
        budget, never taken, unclassifiable -- is followed by a cancel that
        names the task id the POST answered with, after the read and never
        before it. It bounds a stray run (the submission is on the bus for
        any bridge that binds later; see :meth:`inject.InjectTask.cancel`
        for what today's bridge does with it) and leaves the record; the
        classification is the read's, and only the graded timeout adopts the
        terminal the cancel brings.
        """
        try:
            timeout = _numeric_env("AGENT_INJECT_TIMEOUT", _INJECT_DEFAULT_TIMEOUT, float)
            local_port = _numeric_env(
                "AGENT_INJECT_LOCAL_PORT", str(_INJECT_DEFAULT_LOCAL_PORT), int
            )
            delegation_timeout = _numeric_env(
                "AGENT_DELEGATION_TIMEOUT", _DEFAULT_DELEGATION_TIMEOUT, float
            )
            poll_interval = _numeric_env(
                "AGENT_DELEGATION_POLL_INTERVAL", _DEFAULT_DELEGATION_POLL_INTERVAL, float
            )
        except ValueError as exc:
            return AgentResult.errored(str(exc))

        agent_service = os.environ.get("AGENT_SERVICE_NAME", _DEFAULT_AGENT_SERVICE_NAME)
        service = os.environ.get("AGENT_INJECT_SERVICE") or (agent_service + _INJECT_SERVICE_SUFFIX)
        author = os.environ.get("AGENT_INJECT_AUTHOR", inject.DEFAULT_AUTHOR)
        token = os.environ.get("AGENT_INJECT_TOKEN", "").strip()
        if not token:
            # Infrastructure rather than an error: a run with no token never
            # reaches the agent, so it is the same class as a tunnel that
            # will not come up, and grading it would score the install's
            # configuration as the agent's answer.
            secret = agent_service + _INJECT_TOKEN_SECRET_SUFFIX
            return _infra_failure(
                "AGENT_INJECT_TOKEN is unset and the inject door authenticates every request. "
                f"Read it from the {secret} Secret's `{_INJECT_TOKEN_SECRET_KEY}` key in "
                f"{os.environ.get('AGENT_NAMESPACE', _DEFAULT_AGENT_NAMESPACE)}"
            )
        run_id, case_id, repetition = _inject_identity()
        # A fresh conversation per case and repetition: the gateway keeps a
        # session record per conversation, so a shared key would have each
        # task inherit the previous task's context and, worse, arrive while
        # its task is still running and be absorbed as a steer. The message
        # id is the same triple, unique per invocation, which is what lets
        # the door dedupe a retried POST on it.
        conversation = inject.conversation_key(run_id, case_id, repetition)
        message_id = inject.message_id(run_id, case_id, repetition)
        identity: dict[str, Any] = {
            "run_id": run_id,
            "case_id": case_id,
            "repetition": repetition,
            "message_id": message_id,
        }
        base_url = os.environ.get("AGENT_INJECT_URL")
        # A URL given outright is somebody else's tunnel (or a gateway on the
        # network): nothing to establish and nothing to respawn between
        # retries.
        own_tunnel = not base_url

        def _tunnel(reset: bool) -> None:
            if not own_tunnel:
                return
            forward = _reset_port_forward if reset else _ensure_port_forward
            forward(local_port, service=service, remote_port=_INJECT_REMOTE_PORT)

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
                    "inject: port-forward to svc/%s failed to establish (%d/%d): %s",
                    service,
                    transport_failures,
                    _MAX_TRANSPORT_FAILURES,
                    exc,
                )
                if transport_failures >= _MAX_TRANSPORT_FAILURES:
                    return _infra_failure(
                        f"the inject tunnel to svc/{service} failed to establish "
                        f"{transport_failures} times running; last failure: {exc}"
                    )
        if not base_url:
            # 127.0.0.1 rather than localhost, matching _port_open's probe
            # host: a v4/v6 mismatch would make the probe and the request
            # disagree about whether the tunnel is up.
            base_url = f"http://127.0.0.1:{local_port}"

        def _exchange(task: inject.InjectTask, budget: float, *, opening: bool) -> inject.Exchange:
            """One task to its terminal, through the transport retry.

            ``budget`` is this exchange's own: the task timeout for the
            case's prompt, the turn timeout for a status turn. The opening
            exchange reads the fresh conversation first and refuses a budget
            below the gateway's floor before anything is started; a status
            turn's budget is raised to the floor instead, because the case
            is already running and the floor is not the operator's to set
            there.
            """
            failures = 0
            # Set once the POST is first accepted and kept across transport
            # retries: a retry that respawned the tunnel rejoins the same
            # wait, so two dropped polls late in a task cannot triple its
            # budget.
            deadline: float | None = None
            while True:
                try:
                    if not task.task_id:
                        task.preflight()
                        floor = inject.budget_floor(task.first_event_grace)
                        if opening:
                            inject.check_budget(budget, task.first_event_grace)
                        elif budget < floor:
                            budget = floor
                    task_id = task.submit()
                    if deadline is None:
                        deadline = time.monotonic() + budget
                    if not task_id:
                        # The door started nothing and said why. No agent saw
                        # the prompt, so this is the run class rather than an
                        # answer: an author the principal map does not carry
                        # or a submission the gateway could not publish, both
                        # of them a broken install.
                        if not opening:
                            # A status turn hands the refusal back as an
                            # outcome rather than raising, because the
                            # delegation wait's own retry is what has to see
                            # it: each of its turns carries a fresh message
                            # id, so the door's dedupe is not answering, and
                            # an exhausted wait must end as infrastructure
                            # rather than as an appended error the judge
                            # grades (_DelegationTransportExhausted).
                            return inject.Exchange(
                                inject.Fold(""),
                                inject.OUTCOME_NOT_ACCEPTED,
                                task.conversation,
                                "",
                            )
                        # The opening turn is not retried at all: the door
                        # answers a repeat of the same message id with the
                        # same refusal, so a retry cannot change it.
                        raise _TransportError(
                            f"{inject.refusal_detail(task.refusal)}, on "
                            f"{task.conversation}: {task.note}",
                            retryable=False,
                        )
                    return task.await_terminal(task_id, deadline=deadline)
                except inject.InjectUnavailable as exc:
                    failures += 1
                    _log.warning(
                        "inject: exchange on %s failed in transport (%d/%d): %s",
                        task.conversation,
                        failures,
                        _MAX_TRANSPORT_FAILURES,
                        exc,
                    )
                    if not exc.retryable or failures >= _MAX_TRANSPORT_FAILURES:
                        raise
                    try:
                        _tunnel(reset=True)
                    except RuntimeError as pf_exc:
                        # Counted, not raised: the ceiling above ends it.
                        _log.warning(
                            "inject: port-forward respawn failed before retry: %s", pf_exc
                        )

        def _abandon(task: inject.InjectTask) -> str:
            """Cancel a task the transport gave up on, and say what happened.

            The exchange raises when the gateway cannot be reached at all --
            the retries spent, or a status the retry set does not cover --
            and by then the POST may long since have been accepted, so a task
            is running on the bridge with nobody watching it. It holds a
            concurrency slot until the bridge's own deadline, and the units
            behind it in the same run queue. Best effort, on the transport
            that has just failed: it may fail too, and the run is
            infrastructure either way.

            The id may be unknown: a POST the door accepted whose reply
            never arrived leaves the task running with this side holding no
            id for it (the door finishes a claimed turn whether or not its
            client is still there), and the retries that would have been
            answered with the id by the dedupe have failed too. One read of
            the conversation recovers it when the transport allows.
            """
            task_id = task.task_id or task.recover_task_id()
            if not task_id:
                if not task.unanswered_post:
                    # Refused, or never sent: every POST was answered without
                    # a task, or none went out. Nothing is running.
                    return "; no task was started, so nothing is left running"
                return (
                    "; no task id is known for it: the POST's reply never arrived and a read "
                    "of the conversation recovered none (see the log), so a task the POST "
                    "started, if any, was left running"
                )
            task.cancel(task_id, settle=0)
            if task.cancel_sent:
                return (
                    f"; a cancel naming task {task_id} was published, so it does not hold a "
                    "bridge slot for the rest of its budget"
                )
            return (
                f"; task {task_id} was left running: the cancel could not be sent over the "
                "same failed transport (see the log)"
            )

        def _bound_stray(task: inject.InjectTask, exchange: inject.Exchange) -> tuple[bool, bool]:
            """Cancel a task an exchange left active, after the read.

            Every exchange -- the opening turn and each status turn -- goes
            through this, because each can end with its task still on the
            bus: an executor has it (working or queued), nobody took it yet
            (the submission waits for a bridge that binds later), or the
            gateway could not say. The cancel comes only now, after the read
            and naming the task the POST answered with, so it reaches the bus
            even where the gateway's own heal has released the record on
            this very turn. It never changes the classification. The settle
            read after it contributes one thing, and only to the graded
            timeout: how the task ended after the cancel -- the terminal,
            whose word it is and its reason -- for the record, never for the
            verdict (the supervisor completing a requester's stop for a
            worker that exited without its own terminal is still the
            timeout). The
            deliverable stays what the task produced inside its budget --
            the cancel acknowledgement the gateway posts after it is not the
            agent's answer -- and anything else the settle says (a transient
            at that one read, a task that has not confirmed) must not relabel
            what the first, authoritative read classified.

            Returns ``(timed_out, stop_pending)``: whether the exchange ended
            at its budget, and whether a stop was already pending on the
            record so none was sent.
            """
            timed_out = exchange.outcome in (
                inject.OUTCOME_DEADLINE,
                inject.OUTCOME_QUEUED,
                inject.OUTCOME_PARKED,
            )
            leaves_active = timed_out or exchange.outcome in (
                inject.OUTCOME_NEVER_STARTED,
                inject.OUTCOME_UNCLASSIFIED,
            )
            stop_pending = leaves_active and exchange.probe is not None and exchange.probe.detached
            if leaves_active and not stop_pending:
                settled = task.cancel(exchange.task_id, settle=None if timed_out else 0)
                if timed_out and settled is not None and settled.outcome == inject.OUTCOME_TERMINAL:
                    exchange.fold.mark_terminal(
                        settled.fold.terminal,
                        settled.fold.terminal_source,
                        settled.fold.terminal_reason,
                    )
            return timed_out, stop_pending

        def _stop_word(task: inject.InjectTask, stop_pending: bool) -> str:
            """How the stray was bounded, for the record.

            Read from what the door said, never assumed from the cancel having
            been sent: a cancel the gateway refused or could not publish
            leaves the task holding its bridge slot, and a record that said
            "cancelled" would tell a reader the stray was bounded when it was
            not. The never-started branch phrases the same three cases in its
            own words.
            """
            if stop_pending:
                return "a stop was already pending"
            if task.cancel_sent:
                return "cancelled"
            return "no cancel was published (see the log), so it may still be running"

        task = inject.InjectTask(
            base_url=base_url,
            conversation=conversation,
            prompt=prompt,
            token=token,
            author=author,
            message_id=message_id,
        )
        try:
            exchange = _exchange(task, timeout, opening=True)
        except inject.BudgetBelowFloor as exc:
            # Configuration, refused before the POST: the same class as a
            # non-numeric budget, and nothing ran.
            return AgentResult.errored(f"AGENT_INJECT_TIMEOUT: {exc}")
        except inject.InjectUnavailable as exc:
            return _infra_failure(
                f"the inject exchange on {conversation} failed: {exc}{_abandon(task)}"
            )
        except _TransportError as exc:
            return _infra_failure(f"{exc}{_abandon(task)}")
        identity["inject_only"] = task.inject_only
        identity["backend"] = task.backend

        timed_out, stop_pending = _bound_stray(task, exchange)

        if exchange.outcome == inject.OUTCOME_UNCLASSIFIED:
            # The read at the deadline could not say -- the gateway could not
            # look at the stream, or the record no longer held the task and
            # no terminal was ever seen. Nothing says whether an executor saw
            # the prompt, so nothing is graded.
            said = exchange.probe.describe() if exchange.probe else "no answer"
            return _infra_failure(
                f"task {exchange.task_id} on {exchange.conversation} could not be classified at "
                f"the deadline: the gateway's read route said {said}"
            )

        if exchange.outcome == inject.OUTCOME_NEVER_STARTED or exchange.fold.never_started:
            # No executor ever touched the task: the read showed it active
            # with nothing on its stream past the gateway's first-event
            # grace. An install with no executor on the addressee -- a
            # `next` install whose bridge sidecar is not declared -- not an
            # agent that answered badly. The cancel above is on the bus for
            # whichever bridge binds next.
            if task.cancel_sent:
                bounded = (
                    "; a cancel naming the task was published so a bridge that binds later "
                    "kills the stale prompt rather than running it to completion"
                )
            elif stop_pending:
                bounded = "; a stop was already pending on the record"
            else:
                bounded = "; the cancel naming the task could not be sent (see the log)"
            return _infra_failure(
                f"no executor took task {exchange.task_id} on {exchange.conversation}: nothing "
                f"reached its event stream inside the gateway's {task.first_event_grace:.0f}s "
                f"first-event grace, so nothing is running on the addressee{bounded}"
            )

        if exchange.outcome == inject.OUTCOME_QUEUED:
            # The bridge queued the task (submitted) behind its concurrency
            # cap and never spawned it inside the budget. Nothing ran, so
            # there is nothing to grade; the cancel above asked the bridge
            # to drop it from the queue (it answers canceled-before-start).
            ended = exchange.fold.terminal or "no terminal inside the settle"
            bounded = _stop_word(task, stop_pending)
            return _infra_failure(
                f"task {exchange.task_id} on {exchange.conversation} sat queued (submitted, "
                f"never working) for the whole {timeout:.0f}s budget; {bounded}, {ended}. The "
                "bridge's BRIDGE_CONCURRENCY is below the run's parallelism, or its slots are "
                "held by earlier tasks"
            )

        if exchange.outcome == inject.OUTCOME_PARKED:
            # An executor took the task past submitted and never brought it
            # to working: parked at input-required or auth-required for the
            # rest of the budget. No model ran, so there is nothing to grade,
            # and a record with no working or final entry is one rung 3 would
            # refuse as not a run (Fold.started is that rule). The cancel
            # above bounds it.
            parked_at = ", ".join(exchange.fold.executor_states) or "no state"
            ended = exchange.fold.terminal or "no terminal inside the settle"
            bounded = _stop_word(task, stop_pending)
            return _infra_failure(
                f"task {exchange.task_id} on {exchange.conversation} was parked ({parked_at}, "
                f"never working) for the whole {timeout:.0f}s budget; {bounded}, {ended}. An "
                "executor took the task and did not run it"
            )

        if exchange.fold.gateway_declared and not timed_out:
            # The gateway declared this terminal rather than an executor:
            # about a task it could not put on the bus, or -- on the read's
            # fold -- the supervisor's word about an executor that died or
            # never ran. Same state on the wire as an executor's failure and
            # the opposite meaning: grading it would score an outage as the
            # agent answering badly. Which of the two it was is worth saying,
            # because they name different broken things. Not on the graded
            # timeout: there the terminal came from the settle after this
            # harness's own cancel, and a supervisor's `canceled` completing
            # that stop (the worker exited without its own terminal) is how
            # the task ended, not a relabelling of what the deadline read
            # classified -- the same exclusion the reason-based check below
            # makes.
            reason = exchange.fold.terminal_reason or "no reason given"
            if exchange.fold.terminal_source == inject.TERMINAL_SOURCE_SUPERVISOR:
                whose = "the supervisor ended it, so its executor died or never ran"
            else:
                whose = "the gateway failed to publish the submission, so it never reached the bus"
            return _infra_failure(
                f"task {exchange.task_id} was ended by the gateway rather than by an executor "
                f"(state {exchange.fold.terminal}, source {exchange.fold.terminal_source}, "
                f"reason: {reason}): {whose}, so nothing the agent said is on the record"
            )

        infrastructure = exchange.fold.infrastructure_terminal
        if infrastructure and not timed_out:
            # The executor's own failure around the task, not the persona's:
            # the terminal's reason names the bridge or the worker adapter
            # breaking, or the executor refusing the submission. A canceled
            # terminal after this harness's own cancel is excluded -- that is
            # the graded timeout, whatever the bridge's reason says.
            return _infra_failure(
                f"task {exchange.task_id} ended {exchange.fold.terminal}: {infrastructure} "
                f"(reason: {exchange.fold.terminal_reason or 'none given'})"
            )

        result = _inject_result(exchange, identity)
        if timed_out:
            # On the record whether or not the cancel was confirmed: a
            # terminal of `canceled` below says how it ended, this says why --
            # and whether the stop went, read from the door.
            how = _stop_word(task, stop_pending)
            result.errors.append(
                f"task {exchange.task_id} did not reach a terminal state within "
                f"{timeout:.0f}s; {how}"
            )
        if exchange.fold.terminal and exchange.fold.terminal != inject.STATE_COMPLETED:
            reason = exchange.fold.reason_token or "no reason given"
            result.errors.append(
                f"task {exchange.task_id} ended {exchange.fold.terminal} (reason: {reason})"
            )

        if delegation_timeout > 0:
            # The seam for delegated work, and it is the case runner's rather
            # than the transport's on purpose (the A2A owner's instruction on
            # #1661): when agent-initiated delegation becomes a child task on
            # the bus, this whole block goes and the transport is untouched.
            # Card ids are read from the trajectory, which on this path
            # carries no tool calls, so the wait finds nothing outstanding and
            # settles at once. A status turn is a further message on the same
            # conversation, the way a second message in a chat thread is --
            # and it is a new task rather than a steer, because the first
            # task's terminal has already released the conversation. Each
            # turn gets its own message id, or the door's dedupe would answer
            # the second turn with the first turn's task.
            turns = 0

            def _status_turn(poll: str, turn_timeout: float) -> tuple[AgentResult, str]:
                nonlocal turns
                turns += 1
                follow = inject.InjectTask(
                    base_url=base_url,
                    conversation=exchange.conversation,
                    prompt=poll,
                    token=token,
                    author=author,
                    message_id=f"{message_id}{_INJECT_STATUS_TURN_SUFFIX}{turns}",
                )
                try:
                    turn_exchange = _exchange(follow, turn_timeout, opening=False)
                except inject.InjectUnavailable as exc:
                    raise _TransportError(
                        f"{exc}{_abandon(follow)}", retryable=exc.retryable
                    ) from exc
                if turn_exchange.outcome == inject.OUTCOME_NOT_ACCEPTED:
                    # Retryable, and therefore infrastructure once the wait's
                    # retries are spent: nothing executed this turn, so there
                    # is no answer to grade. The next attempt carries the
                    # next status turn's own message id.
                    raise _TransportError(
                        f"{inject.refusal_detail(follow.refusal)}, on status turn {turns} of "
                        f"{follow.conversation}: {follow.note}",
                        retryable=True,
                    )
                # A status turn's task can be left active at its budget the
                # same as the opening turn's, and the next turn on a key
                # whose record still holds it would be absorbed as a steer.
                _bound_stray(follow, turn_exchange)
                return _inject_result(turn_exchange, identity), ""

            def _respawn_tunnel() -> None:
                _tunnel(reset=True)

            try:
                self._await_delegated_work(
                    result,
                    turn=_status_turn,
                    reset=_respawn_tunnel,
                    local_port=local_port,
                    timeout=timeout,
                    delegation_timeout=delegation_timeout,
                    poll_interval=poll_interval,
                )
            except _DelegationTransportExhausted as exc:
                return _infra_failure(str(exc))
        return result

    def _await_delegated_work(
        self,
        result: AgentResult,
        *,
        turn: Callable[[str, float], tuple[AgentResult, str]],
        reset: Callable[[], None],
        local_port: int,
        timeout: float,
        delegation_timeout: float,
        poll_interval: float,
    ) -> str:
        """Poll the agent until every card it filed settles.

        Only two things reach ``result`` from the polling: the delivered card
        results, appended to the agent's own answer, and the turns' token
        spend. Everything else belongs to the harness -- see
        :func:`_fold_status_turn`. (The workers' own tool calls join the
        trajectory afterwards, in :meth:`_settle`, read from their session
        stores rather than from any turn.)

        Each poll first reads the cards' statuses off the board itself
        (:mod:`kube_agents_bench.board`, one ``kubectl exec`` and no model
        turn). Only when a card has stopped moving -- or the board cannot be
        read -- does the harness ask the agent. ``turn`` issues that status
        turn -- on the api transport a re-POST of the same stateful
        ``conversation`` so the agent keeps its context and can carry the
        card's result back, on the inject transport a further message on the
        same conversation -- and returns the parsed reply with its session
        id; ``reset`` respawns the transport's tunnel between failed turns,
        and ``local_port`` is the port that tunnel serves, whose kubectl
        stderr (:func:`_pf_log_path`) the transport-failure messages quote.
        Cards filed *during* a status turn join the wait.

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
                admission door; the run is infrastructure, not a gradable
                result.
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
        # The freshest status seen for each card, from whichever source read
        # it last -- the board or a status turn -- for the deadline report.
        # ``statuses`` stays the agent's own readings, which are what settle a
        # card: a board reading never retires a card from ``outstanding``,
        # because only a status turn can carry the card's result back.
        latest: dict[str, str] = dict(statuses)
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

            # Ask the board before asking the agent. A status turn replays the
            # whole conversation through the model, so a card that runs for
            # 45 minutes used to cost ~90 turns and millions of input tokens
            # against the quota the worker under test shares. The board read
            # is one kubectl exec and no tokens; the turn is spent only once
            # the board says a card has stopped moving, since that is the one
            # turn that can bring its result into the conversation. A read
            # that fails, or does not know every outstanding card, decides
            # nothing: the turn is made exactly as before, so the silent-turn
            # ceiling still ends a wait on a card the agent cannot see.
            on_board = board.read_statuses(_agent_shell, outstanding, _EXEC_TIMEOUT)
            if on_board is not None:
                latest.update(on_board)
                if all(t in on_board for t in outstanding) and not any(
                    on_board[t] in _TERMINAL_STATUSES for t in outstanding
                ):
                    continue

            # The board read took its own time off the budget (up to
            # _EXEC_TIMEOUT); clamp again so the turn cannot overrun the
            # deadline by that much.
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
                        "running; still waiting on: " + ", ".join(outstanding) + "; "
                        f"tunnel log: {_tail(_pf_log_path(local_port))}"
                    ) from exc
                # A handler answered every time (a non-429 4xx, a 500,
                # non-JSON): that is the agent's own failure, so it stays in
                # front of the judge as before -- recorded, not just logged,
                # which is what stops devops-bench promoting the partial
                # record.
                result.errors.append(
                    f"status turns failed in transport {transport_failures} times running; "
                    "still waiting on: " + ", ".join(outstanding) + "; "
                    f"tunnel log: {_tail(_pf_log_path(local_port))}"
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
            latest.update(fresh_reported)
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
            report = (
                "delegated tasks did not finish within "
                f"{delegation_timeout:.0f}s: "
                + ", ".join(f"{t} ({latest.get(t, 'unknown')})" for t in outstanding)
            )
            # With nothing delivered the record holds the acknowledgement
            # alone; the marker routes it to its own class in the scorer
            # rather than a graded failure of the agent under test. A partial
            # delivery keeps the plain report and grades on what arrived.
            if not delivered_results(observed, awaited):
                report = f"{DELEGATION_CEILING_MARKER}: {report}"
            result.errors.append(report)
        # Only the deadline path leaves transcripts behind: a card still
        # outstanding after a transport failure or a mute agent did not stall,
        # and the run stopped for a reason the record already names.
        self._settle(result, observed, awaited, stalled=outstanding if timed_out else [])
        return session_id

    @staticmethod
    def _settle(
        result: AgentResult,
        observed: list[dict[str, Any]],
        awaited: list[str],
        *,
        stalled: Sequence[str] = (),
    ) -> None:
        """Collect everything the delegated cards produced, then clear them out.

        Reading precedes purging: the artifacts are only worth deleting once
        they are part of the answer. ``stalled`` is the subset of ``awaited``
        that ran to the delegation ceiling: their transcripts are copied out
        under ``ARTIFACTS`` before the purge deletes them with the rest, and the
        cards are archived so their workers stop holding dispatcher slots.
        Keyword-only with a default
        so a caller that has no stalled cards, in-tree or in a sibling branch,
        need not name it.

        The workers' own tool calls join the trajectory here, after the front
        agent's, each tagged with the profile that made them (see
        :mod:`kube_agents_bench.worker_trajectory`). ``metadata
        ["worker_trajectory"]`` carries the card-to-session map the read used,
        or ``None`` when the read could not run -- the same distinction
        ``worker_commands`` draws between an empty capture and no capture. It
        stays on the in-process result: devops-bench writes ``trajectory`` to
        the record and drops ``metadata``.
        """
        _append_delivered(result, observed, awaited)
        _append_artifacts(result, awaited, _EXEC_TIMEOUT)
        logs = _worker_logs(awaited, _EXEC_TIMEOUT)
        result.metadata["worker_commands"] = _worker_commands(logs)
        _dump_worker_logs(logs, stalled)
        _archive_stalled_cards(stalled, _EXEC_TIMEOUT)
        captured = worker_trajectory.capture(_agent_shell, awaited, _EXEC_TIMEOUT)
        if captured is None:
            result.metadata["worker_trajectory"] = None
        else:
            result.trajectory.extend(captured.entries)
            result.metadata["worker_trajectory"] = captured.summary
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
