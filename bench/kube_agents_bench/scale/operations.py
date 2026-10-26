"""The agent operations a run times: prompts through the agent API, soaks, a restart, a reset.

Prompts go through :class:`kube_agents_bench.harness.KubeAgentsHarness`, the
transport the evals use, so a prompt here is sent, delegated and awaited the
same way an eval's is. Concurrent prompts run in separate processes, because
the harness keeps its port-forward state per process.
"""

from __future__ import annotations

import base64
import json
import logging
import multiprocessing
import os
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kube_agents_bench.scale import kube
from kube_agents_bench.scale.collect import GATEWAY_SELECTOR
from kube_agents_bench.scale.config import Operation

LOG = logging.getLogger(__name__)

# Named for what they hold: the name of the Kubernetes object and of the field that carry the
# agent API's bearer value, not the value itself.
AGENT_TOKEN_SOURCE_NAME = "platform-agent-secrets"
AGENT_TOKEN_FIELD = "API_SERVER_KEY"
RESET_TIMEOUT_SECONDS = 1800
READY_POLL_SECONDS = 10
READY_WAIT_ARG = "--timeout=60s"
# devops-bench's canonical token buckets, as the harness reports them.
TOKEN_BUCKETS = {"tokens_input": "input", "tokens_output": "output", "tokens_cached": "cached"}
RUN_SLOT_REFUSAL = "Too many concurrent runs"
RATE_LIMIT_MARKERS = ("RESOURCE_EXHAUSTED", "rate limit", "rate_limit", "Too Many Requests", "RateLimitError")
# The harness marks a run its transport could not complete; such a run has no response to time.
INFRA_FAILURE_MARKER = "KUBE_AGENTS_INFRA_FAILURE"
# The eval lane's inject-door switches; cleared so every prompt takes the agent API.
INJECT_ENV = ("AGENT_INJECT_TOKEN", "AGENT_INJECT_URL", "AGENT_INJECT_SERVICE")
TRANSPORT_ENV = "AGENT_TRANSPORT"
API_TRANSPORT = "api"
PROMPT_FAILED = "prompt failed"
PROMPT_UNFINISHED = "prompt unfinished"
# The harness's mark for delegated work that outlived its delegation wait.
DELEGATION_CEILING_MARKER = "KUBE_AGENTS_DELEGATION_CEILING"
# Phrases an answer uses when the agent declined or cancelled the operation it was asked for.
REFUSAL_MARKERS = (
    "NO_REGISTERED_REPO",
    "no registered GitOps repository",
    "explicitly excluded from the audit scope",
    "audit cancelled",
    "audit was cancelled",
    "I couldn't run",
)
# How much of each answer a record keeps, enough to see what the agent did without storing reports.
ANSWER_PREVIEW_CHARS = 240
SPAWN = "spawn"
NO_DEADLINE_SECONDS = 86400
# The agent pod runs under gVisor, which kubectl port-forward cannot reach, so the harness rides
# the repository's kubectl-exec relay into the sidecar that fronts the agent API (bench/README.md,
# "Sandboxed installs").
RELAY_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "hermes-dashboard-tunnel.py"
RELAY_CONTAINER = "agent-api-auth"
RELAY_REMOTE_PORT = 8643
LOCALHOST = "127.0.0.1"
RELAY_START_SECONDS = 15
KUBECONFIG_MODE = 0o600
RELAY_KUBECONFIG_PREFIX = "harness-scale-"
SHELL_POD = "platform-agent-shell-0"
SHELL_CONTAINER = "shell"
# The sandbox's client for brokered commands: the path every agent kubectl and gcloud takes.
PROXY_CLIENT = "credential-proxy-exec"
PROXY_LOAD_GRACE_SECONDS = 600
MIN_WALL_SECONDS = 1e-3
# Runs inside the shell container: concurrency, seconds, argv (JSON, "{kubeconfig}" filled in), kubeconfig glob.
PROXY_LOAD_SCRIPT = """
import concurrent.futures, glob, json, subprocess, sys, time
concurrency, seconds = int(sys.argv[1]), float(sys.argv[2])
kubeconfig = sorted(glob.glob(sys.argv[4]))[0]
argv = [a.replace("{kubeconfig}", kubeconfig) for a in json.loads(sys.argv[3])]
deadline = time.time() + seconds
def worker(_):
    done, errors, first = [], 0, ""
    while time.time() < deadline:
        start = time.time()
        p = subprocess.run(argv, capture_output=True, text=True)
        if p.returncode == 0:
            done.append(time.time() - start)
        else:
            errors += 1
            first = first or (p.stderr or p.stdout).strip()[:200]
    return done, errors, first
began = time.time()
with concurrent.futures.ThreadPoolExecutor(concurrency) as pool:
    results = list(pool.map(worker, range(concurrency)))
wall = time.time() - began
latency = sorted(d for done, _, _ in results for d in done)
pick = lambda q: latency[min(len(latency) - 1, int(q * len(latency)))] if latency else None
print(json.dumps({"completed": len(latency), "errors": sum(e for _, e, _ in results), "wall": wall,
                  "p50": pick(0.5), "p95": pick(0.95), "max": latency[-1] if latency else None,
                  "first_error": next((f for _, _, f in results if f), "")}))
"""
KUBECONFIG_SUFFIX = ".kubeconfig"
# The hourly Cluster Agent reconcile (agents/chat/defaults/cron/jobs.json), which the gateway's
# agent container ticks; run at once, it scaffolds new clusters' profiles and prunes deleted ones'.
RECONCILE_CONTAINER = "platform-agent"
RECONCILE_COMMAND = ("python3", "/opt/data/scripts/cluster_agent_reconcile.py")
RECONCILE_TIMEOUT_SECONDS = 1800
# What a new install starts without: the agent's data and session metadata, the shell's data, the bus's data.
RESET_CLAIMS = ("platform-agent-data", "system-metadata", "data-platform-agent-shell-0", "data-platform-agent-a2a-nats-0")
RESET_STATEFUL_PODS = ("platform-agent-shell-0", "platform-agent-a2a-nats-0")
NATS_POD = "platform-agent-a2a-nats-0"
PROVISION_JOB_FRAGMENT = "a2a-provision"


@dataclass
class OperationResult:
    response_seconds: list[float] = field(default_factory=list)
    previews: list[str] = field(default_factory=list)
    tokens: dict[str, float] = field(default_factory=dict)
    failed_prompts: int = 0
    # Answered, but without performing the operation (declined, cancelled, cut off by the delegation wait).
    unfinished_prompts: int = 0
    provider_429s: int = 0
    run_slot_refusals: int = 0
    errors: list[str] = field(default_factory=list)
    # Operation-specific measurements (proxy_load's throughput and latency).
    metrics: dict[str, float] = field(default_factory=dict)


def agent_env(context: str, namespace: str, local_port: int) -> dict[str, str]:
    """The environment the harness reads, for the experiment install's agent API on ``local_port``."""
    encoded = kube.kubectl(context, "get", "secret", AGENT_TOKEN_SOURCE_NAME, "-n", namespace,
                           "-o", f"jsonpath={{.data.{AGENT_TOKEN_FIELD}}}")
    # The agent API path, whatever the caller exported: the transport named, the inject door's settings blanked.
    env = {name: "" for name in INJECT_ENV}
    env.update({
        TRANSPORT_ENV: API_TRANSPORT,
        "AGENT_CLUSTER_CONTEXT": context,
        "AGENT_NAMESPACE": namespace,
        "AGENT_LOCAL_PORT": str(local_port),
        "PLATFORM_AGENT_TOKEN": base64.b64decode(encoded).decode(),
    })
    return env


class Relay:
    """The kubectl-exec relay to the agent API, run with a kubeconfig of one context.

    The relay resolves its pod through the current kubectl context; giving it a
    kubeconfig holding only the install's context keeps it off whatever context
    the caller has selected, and it re-resolves the pod after a gateway restart.
    """

    def __init__(self, context: str, namespace: str) -> None:
        self.context = context
        self.namespace = namespace
        self.port = 0
        self.proc: subprocess.Popen[bytes] | None = None
        handle, name = tempfile.mkstemp(prefix=RELAY_KUBECONFIG_PREFIX, suffix=KUBECONFIG_SUFFIX)
        os.close(handle)
        self.kubeconfig = Path(name)

    def start(self) -> None:
        self.kubeconfig.write_text(kube.kubectl(self.context, "config", "view", "--minify", "--flatten",
                                                f"--context={self.context}"))
        self.kubeconfig.chmod(KUBECONFIG_MODE)
        # A free port each time, so a relay another run left behind cannot block this one.
        with socket.socket() as probe:
            probe.bind((LOCALHOST, 0))
            self.port = int(probe.getsockname()[1])
        self.proc = subprocess.Popen(
            [sys.executable, str(RELAY_SCRIPT), "--namespace", self.namespace, "--container", RELAY_CONTAINER,
             "--remote-port", str(RELAY_REMOTE_PORT), "--local-port", str(self.port)],
            env={**os.environ, "KUBECONFIG": str(self.kubeconfig)},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(RELAY_START_SECONDS)
        if self.proc.poll() is not None:
            self.stop()  # removes the kubeconfig copy, which holds the host cluster's credentials
            raise kube.CommandError("the agent API relay exited at start")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait()
        self.kubeconfig.unlink(missing_ok=True)


def _prompt_worker(prompt: str, env: dict[str, str]) -> dict[str, Any]:
    """Run one prompt in this process and return the parts a record needs."""
    os.environ.update(env)
    from kube_agents_bench.harness import KubeAgentsHarness  # imported after the environment is set

    started = time.monotonic()
    result = KubeAgentsHarness().run(prompt)
    return {
        "seconds": time.monotonic() - started,
        "tokens": dict(result.tokens or {}),
        "errors": list(result.errors or []),
        "output_chars": len(result.output or ""),
        "preview": (result.output or "")[:ANSWER_PREVIEW_CHARS],
    }


def unfinished_reason(preview: str, errors: list[str]) -> str:
    """Why an answer that came back did not perform its operation, or '' when it did.

    The agent can decline (an operation that needs a GitOps repository the install
    does not have), cancel (it judged every cluster out of scope), or leave its
    delegated work running past the harness's delegation wait, in which case the
    answer is only the acknowledgement.
    """
    for error in errors:
        if DELEGATION_CEILING_MARKER in error:
            return "the delegation wait ran out; the answer is the acknowledgement only"
    lowered = preview.lower()
    for marker in REFUSAL_MARKERS:
        if marker.lower() in lowered:
            return f"the agent did not perform the operation ({marker})"
    return ""


def fold_answer(outcome: OperationResult, answer: dict[str, Any]) -> None:
    """Add one prompt's answer to the run's outcome.

    A prompt the transport could not complete, or one that came back empty, is
    a failed prompt, not a fast one: it is counted and its time is not recorded.
    """
    errors = list(answer.get("errors") or [])
    outcome.previews.append(str(answer.get("preview", "")))
    reported_tokens = any(isinstance(v, (int, float)) and v for v in (answer.get("tokens") or {}).values())
    # Failed: the transport says so, the answer is empty, or no model call stands behind it (the harness
    # erred first, or the agent answered from memory of an earlier run), so it did not do the operation.
    failed = (any(INFRA_FAILURE_MARKER in error for error in errors) or not answer.get("output_chars")
              or not reported_tokens)
    rate_limited = any(marker.lower() in error.lower() for error in errors for marker in RATE_LIMIT_MARKERS)
    if failed and rate_limited:
        # The provider refused, not the harness: a 429, which excludes the run from the response fit.
        outcome.provider_429s += 1
        outcome.errors.append("provider rate limit: " + "; ".join(errors))
    elif failed:
        outcome.failed_prompts += 1
        reason = "; ".join(errors) or ("empty answer" if not answer.get("output_chars") else "no model call behind the answer")
        outcome.errors.append(f"{PROMPT_FAILED}: {reason}")
    elif unfinished := unfinished_reason(str(answer.get("preview", "")), errors):
        outcome.unfinished_prompts += 1
        outcome.errors.append(f"{PROMPT_UNFINISHED}: {unfinished}")
    else:
        outcome.response_seconds.append(float(answer["seconds"]))
    for metric, bucket in TOKEN_BUCKETS.items():
        value = (answer.get("tokens") or {}).get(bucket)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            outcome.tokens[metric] = outcome.tokens.get(metric, 0.0) + value
    for error in errors:
        if RUN_SLOT_REFUSAL in error:
            outcome.run_slot_refusals += 1
    if not failed:
        if rate_limited:
            outcome.provider_429s += 1
        outcome.errors.extend(errors)


def run_prompts(operation: Operation, env: dict[str, str], concurrent: int, first_cluster: str) -> OperationResult:
    """The operation's prompt, plus ``concurrent - 1`` follow-up questions sent at the same moment.

    The whole batch shares one deadline, the operation's timeout. A prompt still
    running at the deadline is counted as failed, and its local worker is
    terminated; the agent may still finish the turn and its delegated cards on
    the server, so the next run can start with that work in flight.
    """
    prompt = operation.prompt.format(first_cluster=first_cluster)
    follow_up = (operation.follow_up or operation.prompt).format(first_cluster=first_cluster)
    prompts = [prompt] + [follow_up] * max(0, concurrent - 1)
    outcome = OperationResult()
    deadline = time.monotonic() + (operation.timeout_seconds or NO_DEADLINE_SECONDS)
    pool = multiprocessing.get_context(SPAWN).Pool(processes=len(prompts))
    try:
        pending = [pool.apply_async(_prompt_worker, (text, env)) for text in prompts]
        for job in pending:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                fold_answer(outcome, job.get(timeout=remaining))
            except multiprocessing.TimeoutError:
                outcome.failed_prompts += 1
                outcome.errors.append(f"{PROMPT_FAILED}: no answer within {operation.timeout_seconds}s")
            except Exception as exc:  # a prompt whose worker died is recorded, and the run goes on
                outcome.failed_prompts += 1
                outcome.errors.append(f"{PROMPT_FAILED}: {type(exc).__name__}: {exc}")
    finally:
        pool.terminate()
        pool.join()
    return outcome


def proxy_load(context: str, namespace: str, operation: Operation, concurrency: int,
               kubeconfig_glob: str) -> OperationResult:
    """Drive the credential proxy with brokered commands, as the agent's sandbox does, and time them.

    ``concurrency`` workers in the sandbox shell each repeat ``operation.command``
    through ``credential-proxy-exec`` for ``operation.window_seconds``; the run
    records the commands completed per second and their latency. No model call
    is involved, so the result is the capacity of the path every agent command
    takes: the client in the sandbox shell, whose CPU the shell's other work
    shares, and the broker. Which of them binds shows in their CPU peaks.
    """
    outcome = OperationResult()
    argv = [PROXY_CLIENT, *operation.command]
    try:
        out = kube.kubectl(context, "exec", "-i", "-n", namespace, SHELL_POD, "-c", SHELL_CONTAINER, "--",
                           "python3", "-", str(concurrency), str(operation.window_seconds), json.dumps(argv),
                           kubeconfig_glob, stdin=PROXY_LOAD_SCRIPT,
                           timeout=operation.window_seconds + PROXY_LOAD_GRACE_SECONDS)
        result = json.loads(out.strip().splitlines()[-1])
    except (kube.CommandError, ValueError, IndexError) as exc:
        outcome.errors.append(f"proxy load failed: {exc}")
        return outcome
    wall = max(float(result["wall"]), MIN_WALL_SECONDS)
    outcome.metrics = {
        "proxy_load_completed": float(result["completed"]),
        "proxy_load_errors": float(result["errors"]),
        "proxy_load_per_second": float(result["completed"]) / wall,
    }
    for name in ("p50", "p95", "max"):
        if result.get(name) is not None:
            outcome.metrics[f"proxy_load_{name}_seconds"] = float(result[name])
    if result.get("first_error"):
        outcome.errors.append(f"proxy load command error: {result['first_error']}")
    return outcome


def soak(window_seconds: int) -> OperationResult:
    time.sleep(window_seconds)
    return OperationResult()


def _gateway_pod(context: str, namespace: str) -> str | None:
    pods = kube.kubectl_json(context, "get", "pods", "-n", namespace, "-l", GATEWAY_SELECTOR)["items"]
    live = [p for p in pods if not p["metadata"].get("deletionTimestamp")]
    return f"pod/{live[0]['metadata']['name']}" if live else None


def reconcile_profiles(context: str, namespace: str) -> None:
    """Run the Cluster Agent reconcile now rather than at its next hourly tick.

    It does what that tick would: a profile for each cluster in scope that has
    none, and no profile for a cluster that is gone. A reconcile already running
    holds the lock, and this one returns at once.
    """
    pod = _gateway_pod(context, namespace)
    if not pod:
        raise kube.CommandError("no gateway pod to run the reconcile in")
    kube.kubectl(context, "exec", "-n", namespace, pod, "-c", RECONCILE_CONTAINER, "--", *RECONCILE_COMMAND,
                 timeout=RECONCILE_TIMEOUT_SECONDS)


def wait_gateway_ready(context: str, namespace: str, timeout: float = RESET_TIMEOUT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pod = _gateway_pod(context, namespace)
        if pod:
            try:
                kube.kubectl(context, "wait", "-n", namespace, "--for=condition=Ready", pod, READY_WAIT_ARG)
                return
            except kube.CommandError:
                pass
        time.sleep(READY_POLL_SECONDS)
    raise kube.CommandError("gateway pod did not become Ready")


def restart(context: str, namespace: str) -> None:
    """Delete the gateway pod; the new one replays the fleet's standing Warning events."""
    pod = _gateway_pod(context, namespace)
    if pod:
        kube.kubectl(context, "delete", "-n", namespace, pod, "--wait=true", timeout=RESET_TIMEOUT_SECONDS)
    wait_gateway_ready(context, namespace)


def reset(context: str, namespace: str) -> None:
    """Give the agent the empty state a new install has, then wait for it.

    Each claim is deleted while mounted, so it waits in Terminating; deleting
    the pods that mount it releases it, and the operator renders new claims and
    pods. The onboarding jobs then run as on a first install, without re-running
    Terraform. The sandbox's SSH key volume is kept: a new install would mint a
    new key, which changes nothing the measurement reads.
    """
    for claim in RESET_CLAIMS:
        kube.kubectl(context, "delete", "pvc", claim, "-n", namespace, "--wait=false", "--ignore-not-found")
    has_bus = bool(kube.kubectl(context, "get", "pod", NATS_POD, "-n", namespace, "--ignore-not-found", "-o", "name").strip())
    for pod in RESET_STATEFUL_PODS:
        kube.kubectl(context, "delete", "pod", pod, "-n", namespace, "--wait=true", "--ignore-not-found",
                     timeout=RESET_TIMEOUT_SECONDS)
    if not has_bus:  # a mode-today install runs no bus
        restart(context, namespace)
        return
    # An empty bus has none of its JetStream buckets; the install creates them with a provision Job,
    # which the operator renders again once the completed one is deleted.
    _wait_pod_ready(context, namespace, NATS_POD)
    jobs = kube.kubectl(context, "get", "jobs", "-n", namespace, "--no-headers", "-o", "name")
    for job in (line.strip() for line in jobs.splitlines()):
        if PROVISION_JOB_FRAGMENT in job:
            kube.kubectl(context, "delete", "-n", namespace, job, "--wait=true", timeout=RESET_TIMEOUT_SECONDS)
    _wait_provisioned(context, namespace)
    restart(context, namespace)


def _wait_pod_ready(context: str, namespace: str, pod: str) -> None:
    """Wait for a pod a StatefulSet re-creates: until it exists, then until it is Ready.

    ``kubectl wait`` on a name fails at once with NotFound in the moment between
    the old pod's deletion and the controller creating the new one.
    """
    deadline = time.monotonic() + RESET_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            kube.kubectl(context, "wait", "-n", namespace, "--for=condition=Ready", f"pod/{pod}", READY_WAIT_ARG)
            return
        except kube.CommandError:
            time.sleep(READY_POLL_SECONDS)
    raise kube.CommandError(f"{pod} did not become Ready after the reset")


def _wait_provisioned(context: str, namespace: str) -> None:
    deadline = time.monotonic() + RESET_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        jobs = kube.kubectl_json(context, "get", "jobs", "-n", namespace)["items"]
        provision = [j for j in jobs if PROVISION_JOB_FRAGMENT in j["metadata"]["name"]]
        if provision and all(j.get("status", {}).get("succeeded") for j in provision):
            return
        time.sleep(READY_POLL_SECONDS)
    raise kube.CommandError("the bus provision Job did not complete after the reset")
