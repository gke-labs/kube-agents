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
  down       destroy the stack
  plan       show what `up` would change, and change nothing

A run by hand goes through this script, from a `main` checkout, never through
`tofu apply` from a branch.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bench"))

from kube_agents_bench.scale import collect, kube  # noqa: E402
from kube_agents_bench.scale import sized_fleet as sf  # noqa: E402
from kube_agents_bench.scale import sized_records, sized_run  # noqa: E402

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
KUBECONFIG_FORMAT = "sized-fleet-{size}.kubeconfig"
NETWORK_FORMAT = "sized-fleet-{size}"
VERDICT_FILE = "scale-verdict.json"
MEASUREMENTS_FILE = "scale-measurements.json"
COST_FILE = "scale-cost.json"
PRIVATE_MODE = 0o600
# Clusters and their node pools are created in parallel; the google provider's own
# default of 10 would build an L fleet's 16 clusters in two waves.
TOFU_PARALLELISM = 32
TOFU_APPLY_TIMEOUT_SECONDS = 90 * 60
TOFU_INIT_TIMEOUT_SECONDS = 10 * 60
TOFU_PLAN_TIMEOUT_SECONDS = 15 * 60
FIREWALL_DELETE_TIMEOUT_SECONDS = 10 * 60
TOFU_ARGS_LOGGED = 3
STDERR_TAIL_CHARS = 2000
SIGNAL_EXIT_BASE = 128
# The error a destroy gives when rules outside the stack still use the network.
NETWORK_IN_USE = "is already being used by"
FIREWALL_RESOURCE = "/firewalls/"
ZONE_SUFFIX_SEPARATOR = "-"
READY_TIMEOUT_SECONDS = 30 * 60
READY_POLL_SECONDS = 30
CHURN_POLL_SECONDS = 20
DEFAULT_CHURN_MINUTES = 6
SECONDS_PER_MINUTE = 60
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


def measure_churn(args: argparse.Namespace, fleet: sf.Fleet, minutes: int) -> float:
    """Client Pods started a minute: the distinct client Pods seen created inside the window."""
    ctx = contexts(args, fleet)
    selector = f"{sf.PART_OF_LABEL}={sf.PART_OF},{sf.COMPONENT_LABEL}={sf.CLIENT}"
    start = time.time()
    seen: set[str] = set()

    def names(context: str) -> set[str]:
        pods = kube.kubectl_json(context, "get", "pods", "-A", "-l", selector)["items"]
        out = set()
        for p in pods:
            created = sf.epoch(p["metadata"]["creationTimestamp"])
            if created >= start:
                out.add(f"{context}/{p['metadata']['namespace']}/{p['metadata']['name']}")
        return out

    while time.time() - start < minutes * SECONDS_PER_MINUTE:
        with ThreadPoolExecutor(CLUSTER_PARALLELISM) as pool:
            for found in pool.map(names, ctx.values()):
                seen |= found
        time.sleep(CHURN_POLL_SECONDS)
    elapsed = (time.time() - start) / SECONDS_PER_MINUTE
    return len(seen) / elapsed


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


def cmd_run(args: argparse.Namespace) -> int:
    if not args.down:
        return run_once(args)[0]
    # A run that stops for any reason, an exception or a closed session's signal, still
    # deletes the fleet: a fleet left standing costs its whole size every hour.
    rc, out = EXIT_UNSOUND, None
    try:
        rc, out = run_once(args)
    finally:
        # A second signal must not cut the destroy short: that leaves tofu's state lock held
        # and the fleet standing.
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sig, signal.SIG_IGN)
        LOG.info("deleting the fleet (--down)")
        cmd_down(args)
    left = leftover_clusters(args)
    if out is not None:
        verdict_path = out / VERDICT_FILE
        verdict = json.loads(verdict_path.read_text())
        verdict["checks"]["teardown"] = {"passed": not left, "failures": [f"{name} still exists" for name in left]}
        verdict["green"] = verdict["green"] and not left
        verdict_path.write_text(json.dumps(verdict, indent=2) + "\n")
        LOG.info("%s: %s", VERDICT_FILE, summary(VERDICT_FILE, verdict))
        if args.results_bucket:
            kube.run([kube.GCLOUD, "storage", "cp", str(verdict_path), results_prefix(args)])
    return EXIT_UNSOUND if left else rc


def results_prefix(args: argparse.Namespace) -> str:
    return f"{args.results_bucket.rstrip('/')}/{args.size.lower()}/{args.run_id}/"


def run_once(args: argparse.Namespace) -> tuple[int, Path]:
    fleet = sf.fleet(args.size)
    credentials(args, fleet)
    kube.gcloud("container", "clusters", "get-credentials", args.host_cluster, f"--location={args.host_location}",
                project=args.project)
    host_context = kube.context_name(args.project, args.host_location, args.host_cluster)

    def check_fleet() -> dict:
        churn = measure_churn(args, fleet, args.churn_minutes) if args.churn_minutes > 0 else None
        return sf.check(fleet, observe_all(args, fleet), churn)

    install = collect.Install(args.project, host_context, args.namespace, args.host_cluster, args.host_location)
    record, result = sized_run.run(project=args.project, host_cluster=args.host_cluster, host_context=host_context,
                                   namespace=args.namespace, check_fleet=check_fleet, install=install,
                                   soak_seconds=args.soak_minutes * SECONDS_PER_MINUTE)
    # Before `down`: the cost record reads the fleet's own instances and clusters.
    cost = sized_records.safe(lambda: sized_records.cost_record(args.project, args.run_id, record.windows),
                              record.errors, "cost record")
    records = {
        VERDICT_FILE: {"size": args.size, "run_id": args.run_id, "fleet": record.fleet_check, **result,
                       "notes": list(record.errors)},
        MEASUREMENTS_FILE: {"size": args.size, "run_id": args.run_id, "windows": record.windows},
        COST_FILE: cost,
    }
    out = Path(args.out or tempfile.mkdtemp(prefix=f"sized-run-{args.run_id}-"))
    out.mkdir(parents=True, exist_ok=True)
    for name, body in records.items():
        (out / name).write_text(json.dumps(body, indent=2) + "\n")
        LOG.info("%s: %s", name, summary(name, body))
    if args.results_bucket:
        kube.run([kube.GCLOUD, "storage", "cp", *[str(out / n) for n in records], results_prefix(args)])
    print(json.dumps(records[VERDICT_FILE], indent=2))
    LOG.info("records in %s", out)
    return (0 if result["green"] else EXIT_UNSOUND), out


def summary(name: str, body: dict) -> str:
    """The one line a record prints to the log."""
    if name == VERDICT_FILE:
        failed = [c for c, v in body.get("checks", {}).items() if not v["passed"]]
        return f"{'green' if body.get('green') else 'red'}; failed: {', '.join(failed) or 'none'}"
    if name == COST_FILE:
        return f"${body.get('usd', '?')} (infrastructure ${body.get('infrastructure', {}).get('usd', '?')})"
    windows = body.get("windows", {})
    return "; ".join(f"{w}: {len(v.get('containers', {}))} containers, tokens {v.get('tokens', {})}"
                     for w, v in windows.items())


def delete_network_firewalls(args: argparse.Namespace) -> list[str]:
    """Delete every firewall rule on the fleet's own network.

    An organization policy agent can create firewall rules on any new VPC, outside
    the stack, and a network with rules on it cannot be deleted. The network is the
    fleet's alone (`sized-fleet-<size>`), so every rule on it goes with it.
    """
    network = NETWORK_FORMAT.format(size=args.size.lower())
    names = kube.gcloud("compute", "firewall-rules", "list", f"--filter=network~/networks/{network}$",
                        "--format=value(name)", project=args.project).split()
    if names:
        kube.gcloud("compute", "firewall-rules", "delete", *names, "--quiet", project=args.project,
                    timeout=FIREWALL_DELETE_TIMEOUT_SECONDS)
    return names


def cmd_down(args: argparse.Namespace) -> int:
    started = time.time()
    init(args.project, args.size)
    destroy = ["destroy", "-input=false", "-no-color", "-auto-approve", f"-parallelism={TOFU_PARALLELISM}",
               *tofu_vars(args)]
    try:
        tofu(destroy, timeout=TOFU_APPLY_TIMEOUT_SECONDS, capture=True)
    except TofuFailed as exc:
        # Only the failure firewall rules outside the stack cause: anything else (a held state
        # lock, an API error) must not touch the rules GKE manages on a fleet that may be live.
        if NETWORK_IN_USE not in exc.output or FIREWALL_RESOURCE not in exc.output:
            raise
        removed = delete_network_firewalls(args)
        LOG.warning("a firewall rule outside the stack held the network; deleted %d, destroying again", len(removed))
        tofu(destroy, timeout=TOFU_APPLY_TIMEOUT_SECONDS, capture=True)
    LOG.info("fleet %s down in %.0fs", args.size, time.time() - started)
    return 0


COMMANDS = {"plan": cmd_plan, "up": cmd_up, "workloads": cmd_workloads, "verify": cmd_verify, "run": cmd_run,
            "down": cmd_down}


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
    parser.add_argument("--out", help="run: the folder for the three records; default a new temp folder")
    parser.add_argument("--results-bucket", help="run: also copy the records to <bucket>/<size>/<run id>/")
    parser.add_argument("--down", action="store_true",
                        help="run: delete the fleet when the run ends, whether it passed, failed or was interrupted")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    use_ca_bundle()
    # SIGTERM and SIGHUP (a closed terminal) end the run through the same finally as an error does.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda signum, _frame: sys.exit(SIGNAL_EXIT_BASE + signum))
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
