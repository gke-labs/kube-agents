#!/usr/bin/env python3
"""Build, check and delete the sized test fleet: the only writer of bench/tf/sized-fleet.

docs/designs/sized-test-fleet.md is the design. Each size is its own OpenTofu
state, in the scale project's `<project>-tf-state` bucket under the prefix
`sized-fleet-<size>`, so a run from any checkout finds and deletes a fleet a
crashed run left.

  up         apply the stack, write each cluster's credentials to a private
             kubeconfig, apply the workloads, and wait for the servers to be ready
  workloads  apply the workloads again to a fleet that is up
  verify     check the fleet against its designed state and measure its churn;
             exit 1 when it is not sound
  run        on a fleet that is up: reset the install on the host cluster, run the
             burst window (the first-run flow and every audit) and the soak window,
             and write the verdict; exit 1 when it is red
  install    upgrade the host cluster's install to the newest `main` commit whose
             images are published, at the size's tier (the release's other values
             are kept, so no secret is needed)
  sweep      delete any sized fleet older than --max-age-hours, whatever its size:
             what an aborted run left
  down       destroy the stack
  plan       show what `up` would change, and change nothing

A run by hand goes through this script, from a `main` checkout, never through
`tofu apply` from a branch.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bench"))

from kube_agents_bench.scale import collect, kube  # noqa: E402
from kube_agents_bench.scale import sized_fleet as sf  # noqa: E402
from kube_agents_bench.scale import attribution, operations, runner, sized_records, sized_run  # noqa: E402

LOG = logging.getLogger("sized-fleet")
STACK_DIR = REPO / "bench" / "tf" / "sized-fleet"
TOFU = "tofu"
TOFU_ENV = {"TF_IN_AUTOMATION": "1", "TF_INPUT": "0"}
STATE_BUCKET_FORMAT = "{project}-tf-state"
STATE_PREFIX_FORMAT = "sized-fleet-{size}"
NODE_SA_FORMAT = "sized-fleet-nodes@{project}.iam.gserviceaccount.com"
DEFAULT_ZONE = "us-central1-f"
DEFAULT_HOST_CLUSTER = "scale-host"
DEFAULT_HOST_LOCATION = "us-central1-c"
DEFAULT_NAMESPACE = "kubeagents-system"
RELEASE = "kube-agents"
CHART_DIR = REPO / "charts" / "kube-agents"
CRD_DIR = CHART_DIR / "crds"
REGISTRY = "ghcr.io"
IMAGES_FILE = REPO / "images.json"
FIRST_PARTY = "first-party"
RELEASE_TAG_POLICY = "release"
MANIFEST_ACCEPT = ("application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, "
                   "application/vnd.docker.distribution.manifest.list.v2+json, "
                   "application/vnd.docker.distribution.manifest.v2+json")
REGISTRY_TIMEOUT_SECONDS = 30
HTTP_NOT_FOUND = 404
# The image tags the release sets, each to the commit under test.
IMAGE_TAG_KEYS = ("agentSandbox.image.tag", "operator.image.tag", "platformAgent.deployment.image.tag",
                  "plugins.pubsubPlatform.image.tag", "plugins.stockoutInvestigator.image.tag")
SIZING_KEY = "platformAgent.deployment.sizingProfile"
# The design's install under test ("The install under test"): no chat channel, no GitOps
# repository, and the audits' auto-promote off, so a nightly's reports reach no person.
AUTO_PROMOTE_ENV = "KUBEAGENTS_AUDIT_AUTO_PROMOTE"
AUTO_PROMOTE_OFF = "false"
CHAT_INTEGRATIONS = ("googleChat", "slack")
GITOPS_LISTS = ("forges", "repositories")
# The fleet-sizing page's tier for each size, by its fleet-controllers row.
TIERS = {"S": "medium", "M": "medium", "L": "fleet"}
# How far back in `main` to look for a commit whose images are published.
IMAGE_LOOKBACK = 30
HELM_TIMEOUT = "20m"
HELM_TIMEOUT_SECONDS = 25 * 60
DEFAULT_MAX_AGE_HOURS = 8
SIZE_LABEL = "kube-agents-scale-size"
# The soak window's chat questions: eval cases run once each through the eval harness.
EVAL_CASES_FILE = REPO / "hack" / "eval" / "sized-fleet-cases.txt"
BENCH_DIR = REPO / "bench"
EVALS_DIR = "evals"
EVAL_SUMMARY_FILE = "summary.json"
EVAL_TIMEOUT_SECONDS = 30 * 60
AGENT_SERVICE = "platform-agent"
JUDGE_PROVIDER = "google"
JUDGE_MODEL = "gemini-3.1-pro-preview"
RESULTS_LINE = re.compile(r"results: (\S+results\.json)")
KUBECONFIG_FORMAT = "sized-fleet-{size}.kubeconfig"
VERDICT_FILE = "scale-verdict.json"
USAGE_FILE = "capability-usage.json"
USAGE_REPORT = "capability-usage.md"
# The label a host cluster carries when its install exists to be measured: a run resets the
# install's state volumes, and refuses a host without it, as the scale runner does.
HOST_LABEL = "purpose=harness-scale"
LEASE_NOTE = "sized fleet run {run_id} ({size})"
MEASUREMENTS_FILE = "scale-measurements.json"
COST_FILE = "scale-cost.json"
PRIVATE_MODE = 0o600
# Clusters and their node pools are created in parallel; the google provider's own
# default of 10 would build an L fleet's 16 clusters in two waves.
TOFU_PARALLELISM = 32
TOFU_APPLY_TIMEOUT_SECONDS = 90 * 60
TOFU_INIT_TIMEOUT_SECONDS = 10 * 60
TOFU_PLAN_TIMEOUT_SECONDS = 15 * 60
TOFU_ARGS_LOGGED = 3
STDERR_TAIL_CHARS = 2000
SIGNAL_EXIT_BASE = 128
ZONE_SUFFIX_SEPARATOR = "-"
READY_TIMEOUT_SECONDS = 30 * 60
READY_POLL_SECONDS = 30
CHURN_POLL_SECONDS = 20
# One CronJob period (5 minutes) and a margin: by then every client has fired once.
CHURN_WARMUP_SECONDS = 10 * 60
DEFAULT_CHURN_MINUTES = 6
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600
CLUSTER_PARALLELISM = 8
EXIT_UNSOUND = 1
POD_FAILING_REASONS = ("ImagePullBackOff", "ErrImagePull", "CrashLoopBackOff", "Error")


class TofuFailed(Exception):
    """tofu exited non-zero; ``output`` is its stderr when it was captured."""

    def __init__(self, command: str, code: int, output: str = "") -> None:
        super().__init__(f"tofu {command} failed ({code}){': ' + output[-STDERR_TAIL_CHARS:] if output else ''}")
        self.output = output


def tofu(args: list[str], *, timeout: int, capture: bool = False) -> str:
    env = {**os.environ, **TOFU_ENV}
    LOG.info("tofu %s", " ".join(args[:TOFU_ARGS_LOGGED]))
    proc = subprocess.run([TOFU, *args], cwd=STACK_DIR, env=env, timeout=timeout, check=False, text=True,
                          capture_output=capture)
    if capture and proc.stdout:
        sys.stderr.write(proc.stdout)
    if proc.returncode != 0:
        raise TofuFailed(args[0], proc.returncode, (proc.stderr or "") if capture else "")
    return proc.stdout if capture else ""


def init(project: str, size: str) -> None:
    tofu(["init", "-reconfigure", "-lockfile=readonly", "-input=false", "-no-color",
          f"-backend-config=bucket={STATE_BUCKET_FORMAT.format(project=project)}",
          f"-backend-config=prefix={STATE_PREFIX_FORMAT.format(size=size.lower())}"],
         timeout=TOFU_INIT_TIMEOUT_SECONDS)


def region_of(zone: str) -> str:
    return zone.rsplit(ZONE_SUFFIX_SEPARATOR, 1)[0]


def tofu_vars(args: argparse.Namespace) -> list[str]:
    return [f"-var=project_id={args.project}", f"-var=size={args.size}", f"-var=zone={args.zone}",
            f"-var=region={region_of(args.zone)}",
            f"-var=node_service_account={args.node_sa or NODE_SA_FORMAT.format(project=args.project)}",
            f"-var=run_id={args.run_id}"]


def kubeconfig_path(args: argparse.Namespace) -> Path:
    return Path(args.kubeconfig or Path(tempfile.gettempdir()) / KUBECONFIG_FORMAT.format(size=args.size.lower()))


def use_kubeconfig(args: argparse.Namespace) -> None:
    path = kubeconfig_path(args)
    if not path.exists():
        path.touch(mode=PRIVATE_MODE)
    path.chmod(PRIVATE_MODE)
    os.environ[kube.KUBECONFIG_ENV] = str(path)


def contexts(args: argparse.Namespace, fleet: sf.Fleet) -> dict[str, str]:
    return {c.name: kube.context_name(args.project, args.zone, c.name) for c in fleet.clusters}


def credentials(args: argparse.Namespace, fleet: sf.Fleet) -> None:
    use_kubeconfig(args)
    for c in fleet.clusters:
        kube.gcloud("container", "clusters", "get-credentials", c.name, f"--zone={args.zone}", project=args.project)
    # The run resets and reads the host install through the same kubeconfig: without its
    # entry here, the host's context exists only in the caller's default kubeconfig.
    if getattr(args, "host_cluster", None):
        kube.gcloud("container", "clusters", "get-credentials", args.host_cluster,
                    f"--location={args.host_location}", project=args.project)


def apply_workloads(args: argparse.Namespace, fleet: sf.Fleet) -> None:
    ctx = contexts(args, fleet)

    def one(c: sf.Cluster) -> int:
        return kube.apply(ctx[c.name], sf.cluster_manifests(c, fleet.shape))

    with ThreadPoolExecutor(CLUSTER_PARALLELISM) as pool:
        applied = sum(pool.map(one, fleet.clusters))
    LOG.info("applied %d objects across %d clusters", applied, len(fleet.clusters))


def observe(context: str) -> sf.Observed:
    o = sf.Observed()
    nodes = kube.kubectl_json(context, "get", "nodes")["items"]
    o.ready_nodes = sum(
        1 for n in nodes
        if any(c["type"] == "Ready" and c["status"] == "True" for c in n["status"].get("conditions", []))
    )
    selector = f"{sf.PART_OF_LABEL}={sf.PART_OF}"
    deployments = kube.kubectl_json(context, "get", "deployments", "-A", "-l",
                                    f"{selector},{sf.COMPONENT_LABEL}={sf.WEB}")["items"]
    o.available_web = sum(
        1 for d in deployments if d.get("status", {}).get("availableReplicas", 0) >= d["spec"]["replicas"]
    )
    o.cronjobs = len(kube.kubectl_json(context, "get", "cronjobs", "-A", "-l", selector)["items"])
    pods = kube.kubectl_json(context, "get", "pods", "-A", "-l", f"{selector},{sf.COMPONENT_LABEL}={sf.BROKEN}")
    for pod in pods["items"]:
        for status in pod.get("status", {}).get("containerStatuses", []):
            waiting = status.get("state", {}).get("waiting", {}).get("reason", "")
            terminated = status.get("lastState", {}).get("terminated", {}).get("reason", "")
            if waiting in POD_FAILING_REASONS or terminated in POD_FAILING_REASONS:
                o.failing_pods += 1
                break
    return o


def observe_all(args: argparse.Namespace, fleet: sf.Fleet) -> dict[str, sf.Observed]:
    ctx = contexts(args, fleet)
    with ThreadPoolExecutor(CLUSTER_PARALLELISM) as pool:
        return dict(zip(ctx, pool.map(observe, ctx.values())))


def wait_ready(args: argparse.Namespace, fleet: sf.Fleet) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while True:
        observed = observe_all(args, fleet)
        ready = sum(o.available_web for o in observed.values())
        LOG.info("servers available: %d of %d", ready, fleet.deployments)
        if ready >= fleet.deployments:
            return
        if time.monotonic() > deadline:
            raise SystemExit(f"only {ready} of {fleet.deployments} servers available after {READY_TIMEOUT_SECONDS}s")
        time.sleep(READY_POLL_SECONDS)


def wait_clients_scheduled(ctx: dict[str, str], selector: str) -> None:
    """Wait until every client CronJob has fired once, at most CHURN_WARMUP_SECONDS.

    A new CronJob first fires at its next scheduled minute, so a churn window that starts
    as the fleet comes up counts too few starts: one run measured 22 Pods a minute of 50.
    """
    deadline = time.monotonic() + CHURN_WARMUP_SECONDS
    while True:
        total = scheduled = 0
        for context in ctx.values():
            jobs = kube.kubectl_json(context, "get", "cronjobs", "-A", "-l", selector)["items"]
            total += len(jobs)
            scheduled += sum(1 for j in jobs if j.get("status", {}).get("lastScheduleTime"))
        if scheduled >= total:
            return
        if time.monotonic() > deadline:
            LOG.warning("%d of %d client CronJobs have fired; measuring the churn anyway", scheduled, total)
            return
        LOG.info("waiting for the client CronJobs to fire once: %d of %d", scheduled, total)
        time.sleep(CHURN_POLL_SECONDS)


def measure_churn(args: argparse.Namespace, fleet: sf.Fleet, minutes: int) -> float:
    """Client Pods started a minute: the distinct client Pods seen created inside the window."""
    ctx = contexts(args, fleet)
    selector = f"{sf.PART_OF_LABEL}={sf.PART_OF},{sf.COMPONENT_LABEL}={sf.CLIENT}"
    wait_clients_scheduled(ctx, selector)
    # Whole seconds, as Kubernetes timestamps are: a fractional start drops the Pods created in its own second.
    start = float(int(time.time()))
    seen: set[str] = set()

    def names(context: str) -> set[str]:
        try:
            pods = kube.kubectl_json(context, "get", "pods", "-A", "-l", selector)["items"]
        except kube.CommandError as exc:
            # A control plane away past the retries (a new zonal cluster's resize) misses this poll
            # only: a client Pod lives longer than a poll, so the next one still sees most of them.
            LOG.warning("churn poll missed %s: %s", context, str(exc)[:200])
            return set()
        out = set()
        for p in pods:
            created = sf.epoch(p["metadata"]["creationTimestamp"])
            if created >= start:
                out.add(f"{context}/{p['metadata']['namespace']}/{p['metadata']['name']}")
        return out

    def poll() -> float:
        with ThreadPoolExecutor(CLUSTER_PARALLELISM) as pool:
            for found in pool.map(names, ctx.values()):
                seen.update(found)
        return time.time()

    polled = poll()
    while polled - start < minutes * SECONDS_PER_MINUTE:
        time.sleep(CHURN_POLL_SECONDS)
        polled = poll()
    # The window ends at the last poll, so no time after it counts without its Pods.
    return len(seen) / ((polled - start) / SECONDS_PER_MINUTE)


def cmd_plan(args: argparse.Namespace) -> int:
    init(args.project, args.size)
    tofu(["plan", "-input=false", "-no-color", f"-parallelism={TOFU_PARALLELISM}", *tofu_vars(args)],
         timeout=TOFU_PLAN_TIMEOUT_SECONDS)
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    fleet = sf.fleet(args.size)
    started = time.time()
    init(args.project, args.size)
    tofu(["apply", "-input=false", "-no-color", "-auto-approve", f"-parallelism={TOFU_PARALLELISM}",
          *tofu_vars(args)], timeout=TOFU_APPLY_TIMEOUT_SECONDS)
    LOG.info("clusters up in %.0fs", time.time() - started)
    credentials(args, fleet)
    apply_workloads(args, fleet)
    wait_ready(args, fleet)
    LOG.info("fleet %s up in %.0fs; kubeconfig %s", args.size, time.time() - started, kubeconfig_path(args))
    return 0


def cmd_workloads(args: argparse.Namespace) -> int:
    fleet = sf.fleet(args.size)
    credentials(args, fleet)
    apply_workloads(args, fleet)
    wait_ready(args, fleet)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    fleet = sf.fleet(args.size)
    credentials(args, fleet)
    observed = observe_all(args, fleet)
    churn = measure_churn(args, fleet, args.churn_minutes) if args.churn_minutes > 0 else None
    result = sf.check(fleet, observed, churn)
    text = json.dumps(result, indent=2)
    print(text)
    if args.report:
        Path(args.report).write_text(text + "\n")
    return 0 if result["sound"] else EXIT_UNSOUND


def leftover_clusters(args: argparse.Namespace) -> list[str]:
    """The run's clusters still in the project, by its run label."""
    out = kube.gcloud("container", "clusters", "list", f"--filter=resourceLabels.{sized_records.RUN_LABEL}={args.run_id}",
                      "--format=value(name)", project=args.project)
    return out.split()


def check_host(args: argparse.Namespace) -> None:
    """Refuse a host cluster without HOST_LABEL: the run resets its install's state volumes."""
    key, _, value = HOST_LABEL.partition("=")
    labels = kube.cluster_labels(args.project, args.host_location, args.host_cluster)
    if labels.get(key) != value:
        raise SystemExit(f"host cluster {args.host_cluster} does not carry {HOST_LABEL}; refusing to reset its install")


def host_lease(args: argparse.Namespace):
    """The host install's live-test lease, held and renewed for the run, as the scale runner holds it."""
    host_context = kube.context_name(args.project, args.host_location, args.host_cluster)
    plan = SimpleNamespace(target={"host_context": host_context, "namespace": args.namespace,
                                   "host_cluster": args.host_cluster}, settings={})
    return runner.hold_lease(plan, LEASE_NOTE.format(run_id=args.run_id, size=args.size))


def interruptible() -> None:
    """SIGTERM and SIGHUP (a closed terminal) end the run through the same paths as an error does.

    Installed only around the run itself: during a `tofu` call the handler would make Python
    SIGKILL tofu, which leaves its state lock held.
    """
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda signum, _frame: sys.exit(SIGNAL_EXIT_BASE + signum))


def shielded() -> None:
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, signal.SIG_IGN)


def cmd_run(args: argparse.Namespace) -> int:
    kube.gcloud("container", "clusters", "get-credentials", args.host_cluster, f"--location={args.host_location}",
                project=args.project)
    check_host(args)
    with host_lease(args):
        if not args.down:
            interruptible()
            try:
                return run_once(args)[0]
            finally:
                shielded()
        return run_and_delete(args)


def run_and_delete(args: argparse.Namespace) -> int:
    # A run that stops for any reason, an exception or a closed session's signal, still
    # deletes the fleet: a fleet left standing costs its whole size every hour.
    rc, out, down_error = EXIT_UNSOUND, None, ""
    interruptible()
    try:
        rc, out = run_once(args)
    finally:
        # A second signal must not cut the destroy short: that leaves tofu's state lock held
        # and the fleet standing.
        shielded()
        LOG.info("deleting the fleet (--down)")
        try:
            cmd_down(args)
        except (TofuFailed, subprocess.SubprocessError, kube.CommandError) as exc:
            down_error = str(exc)
            LOG.error("the fleet could not be deleted: %s", exc)
    left = leftover_clusters(args)
    if out is not None:
        verdict_path = out / VERDICT_FILE
        verdict = json.loads(verdict_path.read_text())
        failures = [f"{name} still exists" for name in left] + ([f"down failed: {down_error}"] if down_error else [])
        verdict["checks"]["teardown"] = {"passed": not failures, "failures": failures}
        verdict["green"] = verdict["green"] and not failures
        verdict_path.write_text(json.dumps(verdict, indent=2) + "\n")
        LOG.info("%s: %s", VERDICT_FILE, summary(VERDICT_FILE, verdict))
        if args.results_bucket:
            kube.run([kube.GCLOUD, "storage", "cp", str(verdict_path), results_prefix(args)])
    return EXIT_UNSOUND if left or down_error else rc


def results_prefix(args: argparse.Namespace) -> str:
    return f"{args.results_bucket.rstrip('/')}/{args.size.lower()}/{args.run_id}/"


def eval_cases(path: Path) -> list[str]:
    """The case paths a cases file lists, relative to bench/, comments and blank lines skipped."""
    lines = (line.strip() for line in path.read_text().splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def case_passed(result: dict) -> bool:
    """A case passes when every objective check passed, as the eval gate reads it."""
    objectives = [c for c in result.get("verification_report") or [] if c.get("role") == "objective"]
    return bool(objectives) and all(c.get("success") for c in objectives)


def run_case(case: str, name: str, env: dict[str, str], evals: Path) -> dict:
    """Run one case once; its log and results go under ``evals``."""
    started = time.time()
    # Its own session, so a timeout stops devops-bench along with uv.
    proc = subprocess.Popen(["uv", "run", "devops-bench", case, "--agent-type", "kubeagents"], cwd=BENCH_DIR,
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            start_new_session=True)
    try:
        output, _ = proc.communicate(timeout=EVAL_TIMEOUT_SECONDS)
    except BaseException:
        # A timeout, or the run's own end (a signal, a lost lease): devops-bench is in its own
        # session, so nothing else stops it.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        output, _ = proc.communicate()
        (evals / f"{name}.log").write_text(output or "")
        raise
    (evals / f"{name}.log").write_text(output)
    entry: dict = {"seconds": round(time.time() - started), "exit": proc.returncode, "passed": False}
    found = RESULTS_LINE.search(output)
    if found:
        result = json.loads(Path(found.group(1)).read_text())[0]
        (evals / f"{name}.results.json").write_text(json.dumps(result, indent=2) + "\n")
        # The answer's time and tokens are the capability usage design's C9 inputs.
        entry.update(passed=case_passed(result), latency=result.get("latency"), tokens=result.get("tokens"),
                     checks={c["name"]: c.get("status") for c in result.get("verification_report") or []})
    return entry


def run_evals(args: argparse.Namespace, host_context: str, out: Path) -> dict:
    """Run each listed case once against the host install; write each result and a summary under out/evals."""
    evals = out / EVALS_DIR
    evals.mkdir(parents=True, exist_ok=True)
    # The agent runs under GKE Sandbox, which a port-forward cannot reach, so the harness rides
    # the exec relay (bench/README.md, "Sandboxed installs").
    relay = operations.Relay(host_context, args.namespace)
    summary: dict[str, dict] = {}
    relay.start()
    try:
        env = {**os.environ, **operations.agent_env(host_context, args.namespace, relay.port),
               "PROJECT_ID": args.project, "GCP_PROJECT_ID": args.project, "CLUSTER_NAME": args.host_cluster,
               "AGENT_SERVICE_NAME": AGENT_SERVICE, "BENCH_TF_ROOT": "./tf",
               "JUDGE_PROVIDER": os.environ.get("JUDGE_PROVIDER", JUDGE_PROVIDER),
               "JUDGE_MODEL": os.environ.get("JUDGE_MODEL", JUDGE_MODEL)}
        for case in eval_cases(Path(args.eval_cases)):
            name = Path(case).parent.name
            LOG.info("eval case %s", name)
            try:
                entry = run_case(case, name, env, evals)
            except (OSError, ValueError, LookupError, subprocess.SubprocessError) as exc:
                # One case's failure does not cost the others their records.
                entry = {"passed": False, "error": str(exc)}
            LOG.info("eval case %s: %s", name, "passed" if entry["passed"] else "failed")
            summary[name] = entry
    finally:
        relay.stop()
    (evals / EVAL_SUMMARY_FILE).write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def capability_usage(host_context: str, namespace: str, windows: dict[str, dict]) -> dict[str, dict]:
    """Each window's tokens by capability (docs/designs/capability-usage-attribution.md), from one read."""
    starts = [datetime.fromisoformat(w["start"]).timestamp() for w in windows.values() if "start" in w]
    stores = attribution.read_stores(host_context, namespace, since=min(starts, default=0.0))
    usage = {}
    for name, window in windows.items():
        try:
            start = datetime.fromisoformat(window["start"]).timestamp()
            usage[name] = attribution.attribute(stores, start, start + window["seconds"], litellm=window.get("tokens"))
        except (KeyError, ValueError) as exc:
            # A window whose measurements were not read costs only its own record.
            usage[name] = {"error": f"window not attributed: {exc!r}"}
    return usage


def run_once(args: argparse.Namespace) -> tuple[int, Path]:
    fleet = sf.fleet(args.size)
    credentials(args, fleet)
    host_context = kube.context_name(args.project, args.host_location, args.host_cluster)

    def check_fleet() -> dict:
        churn = measure_churn(args, fleet, args.churn_minutes) if args.churn_minutes > 0 else None
        return sf.check(fleet, observe_all(args, fleet), churn)

    out = Path(args.out or tempfile.mkdtemp(prefix=f"sized-run-{args.run_id}-"))
    out.mkdir(parents=True, exist_ok=True)
    evals: dict = {}

    def chat_questions() -> None:
        evals.update(run_evals(args, host_context, out))

    install = collect.Install(args.project, host_context, args.namespace, args.host_cluster, args.host_location)
    record, result = sized_run.run(project=args.project, host_cluster=args.host_cluster, host_context=host_context,
                                   namespace=args.namespace, check_fleet=check_fleet, install=install,
                                   soak_seconds=args.soak_minutes * SECONDS_PER_MINUTE,
                                   during_soak=chat_questions if args.eval_cases else None)
    # Before `down`: the cost record reads the fleet's own instances and clusters.
    cost = sized_records.safe(
        lambda: sized_records.cost_record(args.project, args.run_id, record.windows, region=region_of(args.zone)),
        record.errors, "cost record")
    usage = sized_records.safe(lambda: capability_usage(host_context, args.namespace, record.windows),
                               record.errors, "capability usage") or {}
    result["errors"] = list(record.errors)
    records = {
        # The chat questions are eval records beside the verdict; they do not change it.
        VERDICT_FILE: {"size": args.size, "run_id": args.run_id, "fleet": record.fleet_check, **result,
                       "evals": evals},
        MEASUREMENTS_FILE: {"size": args.size, "run_id": args.run_id, "windows": record.windows},
        COST_FILE: cost,
        USAGE_FILE: usage,
    }
    for name, body in records.items():
        (out / name).write_text(json.dumps(body, indent=2) + "\n")
        LOG.info("%s: %s", name, summary(name, body))
    (out / USAGE_REPORT).write_text(attribution.markdown(usage) if usage else "No capability usage was read.\n")
    if args.results_bucket:
        kube.run([kube.GCLOUD, "storage", "cp", *[str(out / n) for n in (*records, USAGE_REPORT)],
                  results_prefix(args)])
        if (out / EVALS_DIR).is_dir():
            kube.run([kube.GCLOUD, "storage", "cp", "--recursive", str(out / EVALS_DIR), results_prefix(args)])
    print(json.dumps(records[VERDICT_FILE], indent=2))
    LOG.info("records in %s", out)
    return (0 if result["green"] else EXIT_UNSOUND), out


def summary(name: str, body: dict) -> str:
    """The one line a record prints to the log."""
    if name == VERDICT_FILE:
        failed = [c for c, v in body.get("checks", {}).items() if not v["passed"]]
        return f"{'green' if body.get('green') else 'red'}; failed: {', '.join(failed) or 'none'}"
    if name == USAGE_FILE:
        return "; ".join(f"{w}: {v['error']}" if "error" in v else
                         f"{w}: unattributed {v['checks']['unattributed']['share']:.1%}, "
                         f"sum {'sound' if v['checks']['sum']['passed'] else 'unsound'}" for w, v in body.items())
    if name == COST_FILE:
        return f"${body.get('usd', '?')} (infrastructure ${body.get('infrastructure', {}).get('usd', '?')})"
    windows = body.get("windows", {})
    return "; ".join(f"{w}: {len(v.get('containers', {}))} containers, tokens {v.get('tokens', {})}"
                     for w, v in windows.items())


def image_repositories() -> list[str]:
    """The registry path of every first-party image an install pulls at the release's tag.

    images.json is the inventory of what an install pulls. The chart's tag keys name only
    five; the operator renders the agent bus's images (a2a-*, hermes-bridge, web-console)
    at its own tag, and the release pushes them last.
    """
    images = json.loads(IMAGES_FILE.read_text())["images"]
    return sorted(i["repository"].removeprefix(f"{REGISTRY}/") for i in images
                  if i.get("origin") == FIRST_PARTY and i.get("tagPolicy") == RELEASE_TAG_POLICY)


def image_published(sha: str) -> bool:
    """Whether every image the release sets is in the registry at ``sha``.

    The release workflow publishes the operator and the agent images in separate jobs,
    the agent images one after another, so one image's tag does not mean the others'.
    """
    return all(manifest_exists(repo, sha) for repo in image_repositories())


def manifest_exists(repo: str, sha: str) -> bool:
    """Whether ``repo`` has a manifest tagged ``sha``, read anonymously."""
    token_url = f"https://{REGISTRY}/token?scope=repository:{repo}:pull&service={REGISTRY}"
    with urllib.request.urlopen(token_url, timeout=REGISTRY_TIMEOUT_SECONDS) as response:  # noqa: S310 -- fixed URL
        token = json.loads(response.read())["token"]
    request = urllib.request.Request(f"https://{REGISTRY}/v2/{repo}/manifests/{sha}",
                                     headers={"Authorization": f"Bearer {token}", "Accept": MANIFEST_ACCEPT})
    try:
        with urllib.request.urlopen(request, timeout=REGISTRY_TIMEOUT_SECONDS):  # noqa: S310
            return True
    except urllib.error.HTTPError as exc:
        if exc.code == HTTP_NOT_FOUND:
            return False
        raise


def newest_published(ref: str = "HEAD") -> str:
    """The newest commit at or before ``ref`` whose images are published: a merge's images take minutes."""
    shas = subprocess.run(["git", "-C", str(REPO), "log", "--format=%H", f"-{IMAGE_LOOKBACK}", ref],
                          capture_output=True, text=True, check=True).stdout.split()
    for sha in shas:
        if image_published(sha):
            return sha
    raise SystemExit(f"no commit in the last {IMAGE_LOOKBACK} of {ref} has published images")


def test_bed_values(values: dict) -> tuple[list[str], list[dict]]:
    """What in a release's values the design's install under test forbids, and its env with auto-promote off."""
    integration = (values.get("platformAgent") or {}).get("integration") or {}
    problems = [f"platformAgent.integration.{name}.enabled is true" for name in CHAT_INTEGRATIONS
                if (integration.get(name) or {}).get("enabled")]
    if (integration.get("github") or {}).get("gitRepo"):
        problems.append("platformAgent.integration.github.gitRepo is set")
    problems += [f"platformAgent.integration.{name} is not empty" for name in GITOPS_LISTS if integration.get(name)]
    env = [e for e in ((values.get("platformAgent") or {}).get("deployment") or {}).get("env") or []
           if e.get("name") != AUTO_PROMOTE_ENV]
    return problems, env + [{"name": AUTO_PROMOTE_ENV, "value": AUTO_PROMOTE_OFF}]


def cmd_install(args: argparse.Namespace) -> int:
    """Upgrade the host install to the commit under test, at the size's tier, under its lease."""
    sha = args.sha or newest_published()
    tier = TIERS[args.size]
    host_context = kube.context_name(args.project, args.host_location, args.host_cluster)
    kube.gcloud("container", "clusters", "get-credentials", args.host_cluster, f"--location={args.host_location}",
                project=args.project)
    check_host(args)
    values = json.loads(kube.run(["helm", "--kube-context", host_context, "-n", args.namespace, "get", "values",
                                  RELEASE, "-o", "json"]) or "{}")
    problems, env = test_bed_values(values)
    if problems:
        raise SystemExit("the host install is not the design's install under test: " + "; ".join(problems))
    with host_lease(args), tempfile.TemporaryDirectory(prefix="sized-chart-") as tree:
        # The chart and CRDs of the commit whose images run, not of this checkout: HEAD's images
        # may not be published yet, and a newer chart may read values older images lack.
        archive = subprocess.run(["git", "-C", str(REPO), "archive", sha, str(CHART_DIR.relative_to(REPO))],
                                 capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", tree], input=archive, check=True)
        chart = Path(tree) / CHART_DIR.relative_to(REPO)
        LOG.info("installing %s at the %s tier on %s", sha[:12], tier, args.host_cluster)
        kube.run(["kubectl", "--context", host_context, "apply", "--server-side", "--force-conflicts", "-f",
                  str(chart / CRD_DIR.name)], timeout=HELM_TIMEOUT_SECONDS)
        sets = [f"--set={key}={sha}" for key in IMAGE_TAG_KEYS] + [f"--set={SIZING_KEY}={tier}"]
        overrides = Path(tree) / "test-bed-values.json"
        overrides.write_text(json.dumps({"platformAgent": {"deployment": {"env": env}}}))
        sets += ["-f", str(overrides)]
        # --reset-then-reuse-values: the chart's own defaults, then the release's values on top,
        # so a key the newer chart adds gets its default rather than nothing.
        kube.run(["helm", "--kube-context", host_context, "-n", args.namespace, "upgrade", RELEASE, str(chart),
                  "--reset-then-reuse-values", *sets, "--wait", f"--timeout={HELM_TIMEOUT}"],
                 timeout=HELM_TIMEOUT_SECONDS)
    LOG.info("installed %s", sha)
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    """Delete every sized fleet with a cluster older than --max-age-hours, by its size's state."""
    out = kube.gcloud("container", "clusters", "list", "--filter=resourceLabels.managed-by=kube-agents-sized-fleet",
                      "--format=json", project=args.project)
    now, stale = time.time(), set()
    for cluster in json.loads(out or "[]"):
        created = datetime.fromisoformat(cluster["createTime"].replace("Z", "+00:00")).timestamp()
        if now - created > args.max_age_hours * SECONDS_PER_HOUR:
            stale.add(cluster["resourceLabels"].get(SIZE_LABEL, "").upper())
    unknown = sorted(stale - set(TIERS))
    if unknown:
        LOG.warning("sized-fleet clusters with no known size label, left alone: %s", ", ".join(unknown) or "(empty)")
    swept, failed = [], []
    # Every stale size is tried: one size whose destroy fails must not keep the others standing.
    for size in sorted(stale & set(TIERS)):
        LOG.warning("deleting the %s fleet an earlier run left", size)
        try:
            cmd_down(argparse.Namespace(**{**vars(args), "size": size}))
            swept.append(size)
        except (TofuFailed, subprocess.SubprocessError, kube.CommandError) as exc:
            LOG.error("the %s fleet could not be deleted: %s", size, exc)
            failed.append(size)
    LOG.info("sweep: deleted %s; failed %s", ", ".join(swept) or "nothing", ", ".join(failed) or "nothing")
    return EXIT_UNSOUND if failed else 0


def cmd_down(args: argparse.Namespace) -> int:
    started = time.time()
    init(args.project, args.size)
    tofu(["destroy", "-input=false", "-no-color", "-auto-approve", f"-parallelism={TOFU_PARALLELISM}",
          *tofu_vars(args)], timeout=TOFU_APPLY_TIMEOUT_SECONDS)
    LOG.info("fleet %s down in %.0fs", args.size, time.time() - started)
    return 0


COMMANDS = {"plan": cmd_plan, "up": cmd_up, "workloads": cmd_workloads, "verify": cmd_verify, "run": cmd_run,
            "down": cmd_down, "install": cmd_install, "sweep": cmd_sweep}


def use_ca_bundle() -> None:
    """Point urllib at certifi's CA bundle when nothing else names one.

    The python.org build for macOS ships without a CA bundle, so its urllib fails
    every HTTPS call (Cloud Monitoring, the Catalog API) with CERTIFICATE_VERIFY_FAILED.
    A Linux runner already has one, and an explicit SSL_CERT_FILE wins.
    """
    if os.environ.get("SSL_CERT_FILE"):
        return
    try:
        import certifi
    except ImportError:
        return
    os.environ["SSL_CERT_FILE"] = certifi.where()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--size", default=sf.load_sizes()["default_size"], choices=sorted(sf.load_sizes()["factors"]))
    parser.add_argument("--project", default=os.environ.get("SCALE_PROJECT_ID"), required="SCALE_PROJECT_ID" not in os.environ)
    parser.add_argument("--zone", default=DEFAULT_ZONE)
    parser.add_argument("--node-sa", help="the nodes' service account; default sized-fleet-nodes@<project>")
    parser.add_argument("--run-id", default="manual", help="the kube-agents-scale-run label on every resource")
    parser.add_argument("--kubeconfig", help="where to keep the fleet's credentials; default a file in the temp directory")
    parser.add_argument("--churn-minutes", type=int, default=DEFAULT_CHURN_MINUTES,
                        help="verify: how long to measure the churn; 0 skips it")
    parser.add_argument("--report", help="verify: also write the result to this file")
    parser.add_argument("--host-cluster", default=DEFAULT_HOST_CLUSTER, help="run: the install's cluster")
    parser.add_argument("--host-location", default=DEFAULT_HOST_LOCATION)
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE, help="run: the install's namespace")
    parser.add_argument("--soak-minutes", type=int, default=sized_run.SOAK_SECONDS // SECONDS_PER_MINUTE)
    parser.add_argument("--out", help="run: the folder for the records; default a new temp folder")
    parser.add_argument("--results-bucket", help="run: also copy the records to <bucket>/<size>/<run id>/")
    parser.add_argument("--eval-cases", nargs="?", const=str(EVAL_CASES_FILE), default=None,
                        help="run: run the cases this file lists in the soak window "
                             f"(default file {EVAL_CASES_FILE.relative_to(REPO)})")
    parser.add_argument("--sha", help="install: the commit to install; default the newest published one")
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS,
                        help="sweep: delete a fleet with a cluster older than this")
    parser.add_argument("--down", action="store_true",
                        help="run: delete the fleet when the run ends, whether it passed, failed or was interrupted")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    use_ca_bundle()
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
