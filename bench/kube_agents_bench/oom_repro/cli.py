"""Drive a kube-agents install to the shell, credential-proxy and event-watcher OOMKills on a KWOK fleet.

    python -m kube_agents_bench.oom_repro fleet create --clusters 2
    python -m kube_agents_bench.oom_repro fleet create --clusters 2 --first 3 --events-only
    python -m kube_agents_bench.oom_repro --host-context <context> proxy
    python -m kube_agents_bench.oom_repro events --count 400000
    python -m kube_agents_bench.oom_repro --host-context <context> watcher --runc
    python -m kube_agents_bench.oom_repro --nodes 3000 --namespaces 300 fleet load --names oom-01,oom-02
    python -m kube_agents_bench.oom_repro --host-context <context> proxy --size L
    python -m kube_agents_bench.oom_repro fleet delete

``bench/oom-repro/README.md`` has the procedure. A scenario runs under the install's live-test lease.
It puts the container's resources (and the runtime class) back from a snapshot of the PlatformAgent
taken under that lease. It does not undo the work it started: the audit ``proxy`` marks due runs to
its end, with whatever writes that audit makes.
"""
import argparse
import base64
import concurrent.futures
import contextlib
import hashlib
import http.client
import json
import os
import re
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from kube_agents_bench.oom_repro import capture, fixtures

# The install under test. The PlatformAgent, labels and container names are the operator's.
DEFAULT_NAMESPACE = "kubeagents-system"
PLATFORM_AGENT = "platform-agent"
GATEWAY_SELECTOR = "app=platform-agent-gateway"
PROXY_SELECTOR = "app=platform-agent-credential-proxy"
PROXY_DEPLOYMENT = "deployment/platform-agent-credential-proxy"
AGENT_CONTAINER = "platform-agent"
PROXY_CONTAINER = "envoy-credential-proxy"
WATCHER_CONTAINER = "agent-api-auth"
SHELL_SELECTOR = "app=platform-agent-shell"
SHELL_CONTAINER = "shell"
CONTAINER_PYTHON = "/opt/hermes/.venv/bin/python3"
SHELL_PYTHON = "/usr/local/bin/python3"
# What `proxy` watches: where the audit's kubectl runs. Since the sandbox's kubectl reads through the
# Kubernetes API proxy (docs/designs/kube-proxy.md), it runs in the shell; on earlier builds it ran
# as a child of the credential proxy.
WATCH_TARGETS = {
    "shell": (SHELL_SELECTOR, SHELL_CONTAINER, "shellSandbox", SHELL_PYTHON),
    "proxy": (PROXY_SELECTOR, PROXY_CONTAINER, "credentialProxy", CONTAINER_PYTHON),
}
DEFAULT_WATCH = "shell"
# Where each container's resources live in the PlatformAgent spec.
RESOURCE_PATHS = {
    "credentialProxy": ("deployment", "credentialProxy"),
    "agentAPIAuth": ("deployment", "agentAPIAuth"),
    "shellSandbox": ("harness", "experimental", "shellSandbox"),
}
# The operator's default limits: credentialProxyCPULimit and credentialProxyMemoryLimit in
# credential_proxy_manifests.go, agentAPIAuthCPULimit and agentAPIAuthMemoryLimit in
# platformagent_manifests.go, shellSandboxCPULimit and shellSandboxMemoryLimit in
# shell_sandbox_manifests.go.
DEFAULT_LIMITS = {
    "credentialProxy": {"cpu": "2", "memory": "2Gi"},
    "agentAPIAuth": {"cpu": "1", "memory": "2Gi"},
    "shellSandbox": {"cpu": "2", "memory": "2Gi"},
}
# The L preset: a raised size for a large fleet, in decimal gigabytes. It is this harness's own and
# larger than the fleet tier below for the shell and the event watcher's CPU.
SIZES = {
    "default": DEFAULT_LIMITS,
    "L": {
        "credentialProxy": {"cpu": "8", "memory": "8G"},
        "agentAPIAuth": {"cpu": "2", "memory": "4G"},
        "shellSandbox": {"cpu": "8", "memory": "8G"},
    },
    # The fleet tier's limits (charts/kube-agents/examples/values-fleet.yaml, described in
    # docs/site/src/content/docs/deploy/fleet-sizing.md): the credential proxy at 8 vCPU and 8 GiB,
    # agent-api-auth at a 4 GiB memory limit with its CPU limit left at the default (the watcher's
    # GOMAXPROCS follows it), and the shell unchanged.
    "fleet": {
        "credentialProxy": {"cpu": "8", "memory": "8Gi"},
        "agentAPIAuth": {**DEFAULT_LIMITS["agentAPIAuth"], "memory": "4Gi"},
        "shellSandbox": DEFAULT_LIMITS["shellSandbox"],
    },
}
DEFAULT_SIZE = "default"
RUNTIME_CLASS_KEYS = ("deployment", "availability", "runtimeClassName")
SPEC_POINTER = "/spec"
RUNTIME_CLASS_PATH = SPEC_POINTER + "/" + "/".join(RUNTIME_CLASS_KEYS)
# The first-run audit whose collector lists every cluster's workloads, several clusters at once.
PROXY_TRIGGER_JOBS = ("compliance-audit",)
PLATFORM_HOME = "/opt/data/profiles/platform"
# Marks the jobs due the way the OOBE job does (agents/chat/scripts/oobe.py), and leaves a job an
# operator disabled or paused alone by OOBE's own rule (oobe.audit_holds): trigger_job would enable
# it again.
AGENT_DATA_DIR = "/opt/data"
AGENT_SCRIPTS_DIR = "/opt/data/scripts"
JOB_TRIGGER_SCRIPT = (
    "import sys\nfrom pathlib import Path\nsys.path.insert(0, sys.argv[1])\n"
    "import oobe\nfrom cron.jobs import trigger_job\n"
    "holds = oobe.audit_holds(Path(sys.argv[2]))\n"
    "if holds is None:\n    sys.exit('cannot read the Platform Agent roster; not starting anything')\n"
    "held = [f'{j} ({holds[j]})' for j in sys.argv[3:] if j in holds]\n"
    "if held:\n    sys.exit('held, not starting: ' + ', '.join(held))\n"
    "missed = [job for job in sys.argv[3:] if not trigger_job(job)]\n"
    "if missed:\n    sys.exit('not marked: ' + ' '.join(missed))\n"
)
# The test fleet.
DEFAULT_PROJECT_ENV = "OOM_REPRO_PROJECT"
DEFAULT_ZONE = "us-central1-f"
DEFAULT_PREFIX = "oom-"
NETWORK = "default"
FLEET_LABEL_KEY = "purpose"
FLEET_LABEL_VALUE = "oom-repro"
FLEET_KUBECONFIG = "fleet.kubeconfig"
CREATE_KUBECONFIG_FORMAT = "create-{cluster}.kubeconfig"
# Real nodes: GKE scales kube-dns and konnectivity with the node count, KWOK's own fake nodes
# included, and the KWOK controller needs a core and a half; 6 e2-standard-8 hold both.
FULL_MACHINE_TYPE = "e2-standard-8"
FULL_REAL_NODES = 6
# Larger fleets get one more real node per this many KWOK nodes.
KWOK_NODES_PER_REAL_NODE = 250
REAL_NODE_POOL = "default-pool"
NAMES_SEPARATOR = ","
MACHINE_TYPE_FORMAT = "value(config.machineType)"
NAMESPACE_OBJECT_PREFIX = "namespace/"
DEPLOYMENT_OBJECT_PREFIX = "deployment.apps/"
# GKE labels each real node with its pool; KWOK nodes carry fixtures.NODE_POOLS instead.
NODE_POOL_LABEL = "cloud.google.com/gke-nodepool"
EVENTS_MACHINE_TYPE = "e2-standard-4"
EVENTS_REAL_NODES = 2
NODE_DISK_GB = "50"
RELEASE_CHANNEL = "regular"
POD_RANGE_SUFFIX = ".64.0/18"
SERVICE_RANGE_SUFFIX = ".16.0/20"
PRIMARY_RANGE_SUFFIX = ".0.0/24"
DEFAULT_NODES = 1500
DEFAULT_NAMESPACES = 150
DEFAULT_DEPLOYMENTS = 10
DEFAULT_REPLICAS = 30
APPLY_PARALLELISM = 10
# A stream that fails (most often while the control plane resizes after the node count jumps) is
# tried up to this many times in all, this far apart.
APPLY_ATTEMPTS = 6
APPLY_RETRY_SECONDS = 60
READY_FRACTION = 0.99
FLEET_WAIT_SECONDS = 3600
POLL_SECONDS = 15
# KWOK, pinned by version and digest.
KWOK_VERSION = "v0.7.0"
KWOK_RELEASE = f"https://github.com/kubernetes-sigs/kwok/releases/download/{KWOK_VERSION}"
KWOK_MANIFESTS = {
    "kwok.yaml": "d656a6d748ba4ce2676209b52ad81af4924ff9c4566d7ccdc27923bc81499680",
    "stage-fast.yaml": "64cfeb13a54813a68f4af0de7e09844d2e7ff8c867fe3053ccb41f664edfa52c",
}
KWOK_NAMESPACE = "kube-system"
KWOK_DEPLOYMENT = "kwok-controller"
KWOK_CPU_REQUEST = "1500m"
KWOK_MEMORY_REQUEST = "1Gi"
ROLLOUT_TIMEOUT = "300s"
# Event injection.
EVENT_COUNT_PATH = "/api/v1/events?limit=1"
DEFAULT_EVENT_THREADS = 48
EVENT_RETRIES = 5
HTTP_CREATED = 201
HTTP_CONFLICT = 409
HTTP_TOO_MANY = 429
HTTP_TIMEOUT_SECONDS = 30
TOKEN_PLUGIN = ("gke-gcloud-auth-plugin", "--use_application_default_credentials")
# Scenarios.
DEFAULT_SCENARIO_SECONDS = 1200
DEFAULT_KILLS = 3
KILL_POLL_SECONDS = 10
SNAPSHOT_FILE = "platformagent-before.json"
REPORT_FILE = "report.md"
SAMPLES_FILE = "samples-{container}.jsonl"
PREVIOUS_LOG_FILE = "previous-{pod}-{container}-{restarts}.log"
RUN_DIR_FORMAT = "%Y%m%dT%H%M%SZ"
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
POD_RUNNING = "Running"
CLOCK_FORMAT = "%H:%M:%S"
APPLY_ERROR_TAIL_CHARS = 200
SUBNET_FORMAT = "{cluster}-subnet"
POD_RANGE_FORMAT = "{cluster}-pods"
SERVICE_RANGE_FORMAT = "{cluster}-svc"
WORKLOAD_POOL_FORMAT = "{project}.svc.id.goog"
NETWORK_PREFIX = "10."
ERROR_TAIL_CHARS = 600
# Quantities, for comparing a request with a limit.
# Kubernetes' quantity grammar: a decimal number with an optional exponent, then a suffix.
QUANTITY = re.compile(r"^([+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)([A-Za-z]*)$")
QUANTITY_SUFFIXES = {"": 1, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18,
                     "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60}
# The repository's live-test lease (scripts/live_test_lease.py, docs/designs/live-test-lease.md).
LEASE_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "live_test_lease.py"
LEASE_ENVS_VAR = "KUBE_AGENTS_LIVE_TEST_ENVS"
LEASE_SESSION_VAR = "KUBE_AGENTS_LEASE_SESSION"
LEASE_ENV_NAME = "oom-repro-host"
LEASE_ENVS_FILE = "envs.json"
LEASE_SESSION_PREFIX = "oom-repro-"
LEASE_TTL_MINUTES = 60
LEASE_RENEWALS_PER_TTL = 3
# After this many failed renewals in a row the scenario stops and restores, before the lease lapses.
LEASE_RENEWAL_FAILURES_TO_STOP = 2
SECONDS_PER_MINUTE = 60
DEFAULT_OUT = Path("oom-repro-out")


def run(cmd: list[str], *, stdin: str | None = None, check: bool = True, env: dict | None = None,
        timeout: float | None = None) -> str:
    r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, env=env, timeout=timeout)
    if check and r.returncode != 0:
        raise SystemExit(f"{' '.join(cmd[:6])} failed ({r.returncode}): {r.stderr.strip()[-ERROR_TAIL_CHARS:]}")
    return r.stdout


def region(zone: str) -> str:
    """The region of a zone: us-central1-f is in us-central1."""
    return zone.rsplit("-", 1)[0]


def quantity(value: str) -> float:
    """A Kubernetes quantity as a number, for comparison only: "500m" is 0.5, "2Gi" is 2**31."""
    m = QUANTITY.match(str(value))
    if not m or m.group(2) not in QUANTITY_SUFFIXES:
        raise ValueError(f"not a quantity this harness reads: {value!r}")
    return float(m.group(1)) * QUANTITY_SUFFIXES[m.group(2)]


def pool(count: int) -> concurrent.futures.ThreadPoolExecutor:
    if count < 1:
        raise SystemExit("no fleet clusters: create them with `fleet create`, or check --prefix and --project")
    return concurrent.futures.ThreadPoolExecutor(count)


class Fleet:
    """The test clusters, reached through a kubeconfig of their own under the output directory."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.project = args.project
        self.zone = args.zone
        self.region = region(args.zone)
        self.prefix = args.prefix
        self.kubeconfig = args.out / FLEET_KUBECONFIG
        self.reached: set[str] = set()

    def names(self) -> list[str]:
        out = run(["gcloud", "container", "clusters", "list", "--project", self.project,
                   "--filter", f"resourceLabels.{FLEET_LABEL_KEY}={FLEET_LABEL_VALUE} AND name~^{self.prefix}",
                   "--format", "value(name)"])
        return sorted(out.split())

    def context(self, cluster: str) -> str:
        return f"gke_{self.project}_{self.zone}_{cluster}"

    def kubectl(self, cluster: str, *args: str, stdin: str | None = None, check: bool = True) -> str:
        if cluster not in self.reached:
            # A fleet created in an earlier run, or by hand, has no context in this run's kubeconfig yet.
            self.credentials(cluster)
        return run(["kubectl", "--kubeconfig", str(self.kubeconfig), "--context", self.context(cluster), *args],
                   stdin=stdin, check=check)

    def credentials(self, cluster: str) -> None:
        run(["gcloud", "container", "clusters", "get-credentials", cluster, "--zone", self.zone,
             "--project", self.project], env={**os.environ, "KUBECONFIG": str(self.kubeconfig)})
        self.reached.add(cluster)


def real_nodes(kwok_nodes: int) -> int:
    """Real nodes a full cluster needs for its system Pods and the KWOK controller."""
    return max(FULL_REAL_NODES, -(-kwok_nodes // KWOK_NODES_PER_REAL_NODE))


def create_cluster(fleet: Fleet, name: str, block: int, events_only: bool, kwok_nodes: int) -> None:
    subnet = SUBNET_FORMAT.format(cluster=name)
    pods, services = POD_RANGE_FORMAT.format(cluster=name), SERVICE_RANGE_FORMAT.format(cluster=name)
    net = f"{NETWORK_PREFIX}{block}"
    run(["gcloud", "compute", "networks", "subnets", "create", subnet, "--project", fleet.project,
         "--network", NETWORK, "--region", fleet.region, "--range", f"{net}{PRIMARY_RANGE_SUFFIX}",
         "--secondary-range", f"{pods}={net}{POD_RANGE_SUFFIX},{services}={net}{SERVICE_RANGE_SUFFIX}",
         "--enable-private-ip-google-access", "-q"])
    # clusters create fetches credentials itself; give it a kubeconfig of its own, so it neither writes
    # the caller's kubeconfig nor races the other creates for one file.
    scratch = fleet.kubeconfig.parent / CREATE_KUBECONFIG_FORMAT.format(cluster=name)
    run(["gcloud", "container", "clusters", "create", name, "--project", fleet.project, "--zone", fleet.zone,
         "--release-channel", RELEASE_CHANNEL,
         "--machine-type", EVENTS_MACHINE_TYPE if events_only else FULL_MACHINE_TYPE,
         "--num-nodes", str(EVENTS_REAL_NODES if events_only else real_nodes(kwok_nodes)), "--disk-size", NODE_DISK_GB,
         "--enable-ip-alias", "--subnetwork", subnet, "--cluster-secondary-range-name", pods,
         "--services-secondary-range-name", services, "--labels", f"{FLEET_LABEL_KEY}={FLEET_LABEL_VALUE}",
         "--workload-pool", WORKLOAD_POOL_FORMAT.format(project=fleet.project), "-q"],
        env={**os.environ, "KUBECONFIG": str(scratch)})
    scratch.unlink(missing_ok=True)


def install_kwok(fleet: Fleet, cluster: str) -> None:
    for name, digest in KWOK_MANIFESTS.items():
        body = urllib.request.urlopen(f"{KWOK_RELEASE}/{name}").read()
        if hashlib.sha256(body).hexdigest() != digest:
            raise SystemExit(f"{name} from {KWOK_RELEASE} does not match its pinned digest")
        text = body.decode()
        # --force-conflicts: a cluster where KWOK was installed before, by another field manager, is re-applied.
        fleet.kubectl(cluster, "apply", "--server-side", "--force-conflicts", "-f", "-",
                      stdin=fixtures.kwok_manifest(text) if name == "kwok.yaml" else text)
    config_map = json.loads(fleet.kubectl(cluster, "-n", KWOK_NAMESPACE, "get", "configmap",
                                          fixtures.KWOK_CONFIG_MAP, "-o", "json"))
    fleet.kubectl(cluster, "apply", "-f", "-", stdin=json.dumps(fixtures.kwok_config(config_map)))
    fleet.kubectl(cluster, "-n", KWOK_NAMESPACE, "set", "resources", f"deployment/{KWOK_DEPLOYMENT}",
                  f"--requests=cpu={KWOK_CPU_REQUEST},memory={KWOK_MEMORY_REQUEST}")
    fleet.kubectl(cluster, "-n", KWOK_NAMESPACE, "rollout", "restart", f"deployment/{KWOK_DEPLOYMENT}")
    fleet.kubectl(cluster, "-n", KWOK_NAMESPACE, "rollout", "status", f"deployment/{KWOK_DEPLOYMENT}",
                  f"--timeout={ROLLOUT_TIMEOUT}")


def apply_parallel(fleet: Fleet, cluster: str, docs: list[dict]) -> None:
    """Server-side apply in APPLY_PARALLELISM streams, each applied again until it succeeds.

    Conflicts on objects that exist are forced. A stream still failing after APPLY_ATTEMPTS
    stops the run with its error, rather than leaving a fleet smaller than asked for.
    """
    def apply(text: str) -> None:
        for attempt in range(APPLY_ATTEMPTS):
            try:
                fleet.kubectl(cluster, "apply", "--server-side", "--force-conflicts", "-f", "-", stdin=text)
                return
            except SystemExit as exc:
                if attempt == APPLY_ATTEMPTS - 1:
                    raise
                print(f"{time.strftime(CLOCK_FORMAT)} {cluster}: apply failed, retrying: {str(exc)[-APPLY_ERROR_TAIL_CHARS:]}", flush=True)
                time.sleep(APPLY_RETRY_SECONDS)

    with concurrent.futures.ThreadPoolExecutor(APPLY_PARALLELISM) as workers:
        list(workers.map(apply, fixtures.chunks(docs, APPLY_PARALLELISM)))


def wait_until(what: str, measure, target: int) -> int:
    deadline = time.monotonic() + FLEET_WAIT_SECONDS
    value = measure()
    while value < target and time.monotonic() < deadline:
        print(f"{time.strftime(CLOCK_FORMAT)} {what}: {value} of {target}", flush=True)
        time.sleep(POLL_SECONDS)
        value = measure()
    return value


def load_cluster(fleet: Fleet, cluster: str, args: argparse.Namespace) -> None:
    install_kwok(fleet, cluster)
    nodes = [fixtures.node(i) for i in range(args.nodes)]
    target = int(args.nodes * READY_FRACTION)

    def ready() -> int:
        out = fleet.kubectl(cluster, "get", "nodes", "-l", kwok_selector(), "--no-headers", check=False)
        return sum(1 for line in out.splitlines() if " Ready " in line)

    apply_parallel(fleet, cluster, nodes)
    if wait_until(f"{cluster} nodes Ready", ready, target) < target:
        apply_parallel(fleet, cluster, nodes)  # nodes GKE deleted while the controller caught up
    docs = fixtures.workloads(args.namespaces, args.deployments, args.replicas)
    # Namespaces first, so no parallel stream applies a Deployment before its Namespace exists.
    apply_parallel(fleet, cluster, [d for d in docs if d["kind"] == "Namespace"])
    apply_parallel(fleet, cluster, [d for d in docs if d["kind"] != "Namespace"])

    def running() -> int:
        out = fleet.kubectl(cluster, "get", "deployments", "-A", "-o",
                            "jsonpath={range .items[*]}{.status.readyReplicas}{\"\\n\"}{end}", check=False)
        return sum(int(x) for x in out.split() if x.isdigit())

    pods = args.namespaces * args.deployments * args.replicas
    got = wait_until(f"{cluster} Pods ready", running, pods)
    print(f"{cluster}: {got} of {pods} Pods ready" + ("" if got >= pods else " (short: the wait timed out)"), flush=True)


def fleet_create(args: argparse.Namespace) -> None:
    fleet = Fleet(args)
    subnets = json.loads(run(["gcloud", "compute", "networks", "subnets", "list", "--project", fleet.project,
                              "--network", NETWORK, "--format", "json"]))
    names = [f"{fleet.prefix}{i:02d}" for i in range(args.first, args.first + args.clusters)]
    blocks = fixtures.free_blocks(subnets, len(names))
    with pool(len(names)) as workers:
        list(workers.map(lambda nb: create_cluster(fleet, nb[0], nb[1], args.events_only, args.nodes), zip(names, blocks)))
    for name in names:
        # Serially, before the parallel loads below: get-credentials writes one shared kubeconfig.
        fleet.credentials(name)
        if args.events_only:
            apply_parallel(fleet, name, [fixtures.namespace(n) for n in range(args.namespaces)])
    if not args.events_only:
        with pool(len(names)) as workers:
            list(workers.map(lambda n: load_cluster(fleet, n, args), names))


def kwok_selector() -> str:
    return f"{fixtures.KWOK_NODE_LABEL}={fixtures.KWOK_NODE_LABEL_VALUE}"


def kwok_node_count(fleet: Fleet, cluster: str) -> int:
    # check=True: a failed read must not count as zero, or the guard below would pass a shrink.
    return len(fleet.kubectl(cluster, "get", "nodes", "-l", kwok_selector(), "-o", "name").split())


def workload_namespace_count(fleet: Fleet, cluster: str) -> int:
    prefix = NAMESPACE_OBJECT_PREFIX + fixtures.NAMESPACE_PREFIX
    return sum(1 for n in fleet.kubectl(cluster, "get", "namespaces", "-o", "name").split() if n.startswith(prefix))


def workload_deployment_count(fleet: Fleet, cluster: str) -> int:
    # Every workload namespace holds the same Deployments, so the distinct names are the count per namespace.
    prefix = DEPLOYMENT_OBJECT_PREFIX + fixtures.DEPLOYMENT_PREFIX
    names = fleet.kubectl(cluster, "get", "deployments", "-A", "-o", "name").split()
    return len({n for n in names if n.startswith(prefix)})


def real_node_count(fleet: Fleet, cluster: str) -> int:
    selector = f"{NODE_POOL_LABEL}={REAL_NODE_POOL}"
    return len(fleet.kubectl(cluster, "get", "nodes", "-l", selector, "-o", "name").split())


def pool_machine_type(fleet: Fleet, cluster: str) -> str:
    return run(["gcloud", "container", "node-pools", "describe", REAL_NODE_POOL, "--cluster", cluster,
                "--zone", fleet.zone, "--project", fleet.project, "--format", MACHINE_TYPE_FORMAT]).strip()


def load_targets(requested: str, existing: list[str]) -> list[str]:
    """The clusters --names asks for, refusing a name that is not a fleet cluster."""
    names = list(dict.fromkeys(n.strip() for n in requested.split(NAMES_SEPARATOR) if n.strip()))
    unknown = sorted(set(names) - set(existing))
    if unknown or not names:
        raise SystemExit(f"not fleet clusters: {', '.join(unknown) or '(none named)'}")
    return names


def fleet_load(args: argparse.Namespace) -> None:
    """Bring the named fleet clusters up to --nodes KWOK nodes and the workload size given.

    It only grows a cluster: KWOK nodes, namespaces and Deployments above the size given would stay,
    sized for fewer real nodes, so a smaller --nodes, --namespaces or --deployments than the cluster
    has is refused, and a real node pool already at its size or above it is left as it is. A smaller
    --replicas scales the Deployments down. An Events-only cluster, with workload namespaces but no KWOK
    nodes or Deployments, is refused.
    """
    fleet = Fleet(args)
    names = load_targets(args.names, fleet.names())
    grow = []
    for name in names:
        machine = pool_machine_type(fleet, name)
        if machine != FULL_MACHINE_TYPE:
            raise SystemExit(f"{name} runs {machine}, not {FULL_MACHINE_TYPE}: fleet load grows full clusters only")
        fleet.credentials(name)  # serially: get-credentials writes one shared kubeconfig
        have = kwok_node_count(fleet, name)
        if have > args.nodes:
            raise SystemExit(f"{name} has {have} KWOK nodes, more than --nodes {args.nodes}; fleet load only grows")
        spaces = workload_namespace_count(fleet, name)
        if spaces > args.namespaces:
            raise SystemExit(f"{name} has {spaces} workload namespaces, more than --namespaces {args.namespaces}; "
                             "fleet load only grows")
        deployments = workload_deployment_count(fleet, name)
        if deployments > args.deployments:
            raise SystemExit(f"{name} has {deployments} Deployments per workload namespace, more than --deployments "
                             f"{args.deployments}; fleet load only grows")
        if spaces and not have and not deployments:
            raise SystemExit(f"{name} has workload namespaces but no KWOK nodes or Deployments: an Events-only "
                             "cluster is not grown")
        if real_node_count(fleet, name) < real_nodes(args.nodes):
            grow.append(name)
    with pool(len(names)) as workers:
        list(workers.map(lambda n: run(["gcloud", "container", "clusters", "resize", n, "--node-pool", REAL_NODE_POOL,
                                        "--num-nodes", str(real_nodes(args.nodes)), "--zone", fleet.zone,
                                        "--project", fleet.project, "-q"]), grow))
    with pool(len(names)) as workers:
        list(workers.map(lambda n: load_cluster(fleet, n, args), names))


def fleet_delete(args: argparse.Namespace) -> None:
    """Delete the fleet clusters, then every subnet of the fleet's prefix, including one a failed create left."""
    fleet = Fleet(args)
    for name in fleet.names():
        run(["gcloud", "container", "clusters", "delete", name, "--zone", fleet.zone, "--project", fleet.project, "-q"])
    # Only subnets this harness names: <prefix><two digits>-subnet, on its network, in its region.
    shape = re.compile(rf"^{re.escape(fleet.prefix)}[0-9]+{re.escape(SUBNET_FORMAT.format(cluster=''))}$")
    listed = run(["gcloud", "compute", "networks", "subnets", "list", "--project", fleet.project,
                  "--network", NETWORK, "--regions", fleet.region, "--format", "value(name)"]).split()
    subnets = [name for name in listed if shape.match(name)]
    for subnet in subnets:
        run(["gcloud", "compute", "networks", "subnets", "delete", subnet, "--region", fleet.region,
             "--project", fleet.project, "-q"], check=False)


def event_count(fleet: Fleet, cluster: str) -> int:
    page = json.loads(fleet.kubectl(cluster, "get", "--raw", EVENT_COUNT_PATH))
    return page["metadata"].get("remainingItemCount", 0) + len(page["items"])


def inject(fleet: Fleet, cluster: str, args: argparse.Namespace) -> None:
    info = json.loads(run(["gcloud", "container", "clusters", "describe", cluster, "--zone", fleet.zone,
                           "--project", fleet.project, "--format", "json(endpoint,masterAuth.clusterCaCertificate)"]))
    with tempfile.NamedTemporaryFile(suffix=".crt", delete=False) as ca:
        ca.write(base64.b64decode(info["masterAuth"]["clusterCaCertificate"]))
    ctx = ssl.create_default_context(cafile=ca.name)
    bearer = json.loads(run(list(TOKEN_PLUGIN)))["status"]["token"]
    run_id = f"{int(time.time()) % 100000:05d}"
    now = time.strftime(TIMESTAMP_FORMAT, time.gmtime())
    jobs = list(range(args.count))
    done = {"created": 0, "failed": 0}
    lock = threading.Lock()

    def worker() -> None:
        conn = http.client.HTTPSConnection(info["endpoint"], context=ctx, timeout=HTTP_TIMEOUT_SECONDS)
        while True:
            with lock:
                if not jobs:
                    return
                i = jobs.pop()
            ns, body = fixtures.event(i, args.namespaces, args.deployments, args.nodes, run_id, now, args.pad_to)
            outcome = "failed"
            for attempt in range(EVENT_RETRIES):
                try:
                    conn.request("POST", f"/api/v1/namespaces/{ns}/events", json.dumps(body),
                                 {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"})
                    r = conn.getresponse()
                    r.read()
                except (OSError, http.client.HTTPException):
                    conn = http.client.HTTPSConnection(info["endpoint"], context=ctx, timeout=HTTP_TIMEOUT_SECONDS)
                    continue
                if r.status == HTTP_TOO_MANY:
                    time.sleep(attempt + 1)
                    continue
                outcome = "created" if r.status in (HTTP_CREATED, HTTP_CONFLICT) else "failed"
                break
            with lock:
                done[outcome] += 1

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(args.threads)]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        time.sleep(POLL_SECONDS)
        print(f"{time.strftime(CLOCK_FORMAT)} {cluster}: {done['created']} created, {done['failed']} failed", flush=True)
    os.unlink(ca.name)


def events(args: argparse.Namespace) -> None:
    fleet = Fleet(args)
    names = fleet.names()
    for name in names:
        fleet.credentials(name)
    with pool(len(names)) as workers:
        list(workers.map(lambda n: inject(fleet, n, args), names))
    for name in names:
        print(f"{name}: {event_count(fleet, name)} Events (they expire after the API server's event TTL, 1 h by default)")


class Host:
    """The install under test, through the caller's kubeconfig context."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.context = args.host_context
        self.namespace = args.namespace
        # Set when the live-test lease is lost; every wait stops on it.
        self.stop = threading.Event()

    def check(self) -> None:
        if self.stop.is_set():
            raise SystemExit("stopped: the live-test lease could not be renewed")

    def kubectl(self, *args: str, stdin: str | None = None, check: bool = True, timeout: float | None = None) -> str:
        return run(["kubectl", "--context", self.context, "-n", self.namespace, *args], stdin=stdin, check=check,
                   timeout=timeout)

    def pods(self, selector: str | None = None) -> list[dict]:
        args = ["get", "pods", "-o", "json"] + (["-l", selector] if selector else [])
        return json.loads(self.kubectl(*args, check=False) or '{"items": []}')["items"]

    def running_pod(self, selector: str) -> str | None:
        names = [p["metadata"]["name"] for p in self.pods(selector) if p["status"].get("phase") == POD_RUNNING
                 and not p["metadata"].get("deletionTimestamp")]
        return names[0] if names else None

    def wait_running(self, selector: str) -> str:
        deadline = time.monotonic() + FLEET_WAIT_SECONDS
        while time.monotonic() < deadline:
            self.check()
            pod = self.running_pod(selector)
            if pod:
                return pod
            time.sleep(POLL_SECONDS)
        raise SystemExit(f"no running pod for {selector}")

    def platform_agent(self) -> dict:
        return json.loads(self.kubectl("get", "platformagent", PLATFORM_AGENT, "-o", "json"))

    def patch(self, patch: list[dict]) -> None:
        if patch:
            self.kubectl("patch", "platformagent", PLATFORM_AGENT, "--type", "json", "-p", json.dumps(patch))


def resources_patch(spec: dict, path: tuple[str, ...], limits: dict) -> list[dict]:
    """A JSON patch that sets the resources at ``spec.<path>.resources`` to ``limits``.

    Requests the spec already sets are kept, and lowered to the limit where they exceed it: the
    operator refuses a request above its limit. Missing parents are created.
    """
    node = spec
    for key in path:
        node = node.get(key, {}) if isinstance(node, dict) else {}
    resources = json.loads(json.dumps(node.get("resources") or {}))
    requests = resources.get("requests", {})
    for name, limit in limits.items():
        if name in requests and quantity(requests[name]) > quantity(limit):
            requests[name] = limit
    resources["limits"] = dict(limits)
    if requests:
        resources["requests"] = requests
    value, node, pointer = {"resources": resources}, spec, SPEC_POINTER
    for depth, key in enumerate(path):
        if not isinstance(node, dict) or key not in node:
            for inner in reversed(path[depth + 1:]):
                value = {inner: value}
            return [{"op": "add", "path": f"{pointer}/{key}", "value": value}]
        node, pointer = node[key], f"{pointer}/{key}"
    return [{"op": "add", "path": f"{pointer}/resources", "value": resources}]


def at(spec: dict, path: tuple[str, ...]):
    node = spec
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def restore_patch(before: dict, now: dict, path: tuple[str, ...], runtime_class: bool) -> list[dict]:
    """JSON patch ops that put the resources at ``path`` (and the runtime class) back as ``before`` had them.

    ``now`` is the spec as it stands, so a path the scenario never wrote is not removed.
    """
    ops = []
    pointer = SPEC_POINTER + "/" + "/".join(path) + "/resources"
    resources = (at(before, path) or {}).get("resources")
    if resources:
        ops.append({"op": "add", "path": pointer, "value": resources})
    elif (at(now, path) or {}).get("resources") is not None:
        # Remove the outermost object the scenario created, or only the resources it added.
        depth = next((d for d in range(1, len(path) + 1) if at(before, path[:d]) is None), None)
        target = SPEC_POINTER + "/" + "/".join(path[:depth]) if depth else pointer
        ops.append({"op": "remove", "path": target})
    if runtime_class:
        rc = at(before, RUNTIME_CLASS_KEYS)
        if rc:
            ops.append({"op": "add", "path": RUNTIME_CLASS_PATH, "value": rc})
    return ops


def has_limits(pod: dict, container: str, limits: dict) -> bool:
    """Whether a pod's container (or init sidecar) runs with exactly ``limits``."""
    spec = pod.get("spec", {})
    for c in spec.get("containers", []) + spec.get("initContainers", []):
        if c["name"] == container:
            have = c.get("resources", {}).get("limits", {})
            return all(name in have and quantity(have[name]) == quantity(v) for name, v in limits.items())
    return False


def wait_for_limits(host: Host, selector: str, container: str, limits: dict) -> str:
    """The running pod whose container carries ``limits``, once the operator has rolled it out."""
    deadline = time.monotonic() + FLEET_WAIT_SECONDS
    while time.monotonic() < deadline:
        host.check()
        for p in host.pods(selector):
            if (p["status"].get("phase") == POD_RUNNING and not p["metadata"].get("deletionTimestamp")
                    and has_limits(p, container, limits)):
                return p["metadata"]["name"]
        time.sleep(POLL_SECONDS)
    raise SystemExit(f"no running pod for {selector} carries {limits} on {container}")


@contextlib.contextmanager
def lease(host: Host, note: str):
    with tempfile.TemporaryDirectory() as directory:
        envs = Path(directory) / LEASE_ENVS_FILE
        envs.write_text(json.dumps({"installs": [{"name": LEASE_ENV_NAME, "context": host.context,
                                                  "namespace": host.namespace}]}))
        env = {**os.environ, LEASE_ENVS_VAR: str(envs), LEASE_SESSION_VAR: f"{LEASE_SESSION_PREFIX}{os.getpid()}"}

        def call(*a: str) -> subprocess.CompletedProcess:
            return subprocess.run([sys.executable, str(LEASE_SCRIPT), *a, "--env", LEASE_ENV_NAME], env=env,
                                  cwd=directory, capture_output=True, text=True, check=False)

        got = call("acquire", "--ttl", str(LEASE_TTL_MINUTES), "--note", note)
        if got.returncode != 0:
            raise SystemExit(f"could not take the install's live-test lease:\n{got.stdout}{got.stderr}")
        stop, lost = threading.Event(), threading.Event()

        def renew() -> None:
            failures = 0
            while not stop.wait(LEASE_TTL_MINUTES * SECONDS_PER_MINUTE / LEASE_RENEWALS_PER_TTL):
                renewed = call("renew", "--ttl", str(LEASE_TTL_MINUTES))
                failures = 0 if renewed.returncode == 0 else failures + 1
                if failures:
                    print(f"could not renew the live-test lease: {renewed.stdout}{renewed.stderr}", flush=True)
                if failures >= LEASE_RENEWAL_FAILURES_TO_STOP:
                    lost.set()
                    return

        threading.Thread(target=renew, daemon=True).start()
        try:
            yield lost
        finally:
            stop.set()
            released = call("release")
            if released.returncode:
                print(f"could not release the live-test lease; it lapses within {LEASE_TTL_MINUTES} minutes: "
                      f"{released.stdout}{released.stderr}", flush=True)


class Recorder:
    """Samples the watched container, polls it for OOMKills and keeps each killed container's last log."""

    def __init__(self, host: Host, out: Path, selector: str, container: str, python: str = CONTAINER_PYTHON) -> None:
        self.host, self.out, self.selector, self.container, self.python = host, out, selector, container, python
        self.stop = threading.Event()
        # Keyed by pod name and uid: a StatefulSet pod keeps its name across a rollout and restarts from 0.
        self.seen: dict[tuple[str, str | None], int] = {}
        self.events: list[dict] = []

    def sample(self) -> None:
        path = self.out / SAMPLES_FILE.format(container=self.container)
        while not self.stop.is_set():
            try:
                pod = self.host.running_pod(self.selector)
                if pod:
                    proc = subprocess.Popen(["kubectl", "--context", self.host.context, "-n", self.host.namespace,
                                             "exec", pod, "-c", self.container, "--", self.python, "-u", "-c",
                                             capture.SAMPLER_SCRIPT], stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, text=True)
                    with path.open("a") as f:
                        for line in proc.stdout:
                            try:
                                f.write(json.dumps({"pod": pod, **json.loads(line)}) + "\n")
                            except ValueError:
                                continue  # a line cut off when the container was killed mid-write
                            f.flush()
                            if self.stop.is_set():
                                proc.terminate()
                                break
                    if proc.wait() and not self.stop.is_set():
                        print(f"sampler: exec exited {proc.returncode}: "
                              f"{proc.stderr.read()[-ERROR_TAIL_CHARS:].strip()}; retrying", flush=True)
            except (SystemExit, OSError) as exc:
                print(f"sampler: {exc}; retrying", flush=True)
            self.stop.wait(capture.SAMPLE_SECONDS)

    def poll(self) -> None:
        while not self.stop.is_set():
            for pod in self.host.pods(self.selector):
                for kill in capture.kills(pod):
                    key = (kill["pod"], kill["uid"])
                    if kill["container"] != self.container or self.seen.get(key) == kill["restarts"]:
                        continue
                    self.seen[key] = kill["restarts"]
                    log = self.host.kubectl("logs", kill["pod"], "-c", kill["container"], "--previous",
                                            "--timestamps", check=False)
                    (self.out / PREVIOUS_LOG_FILE.format(**kill)).write_text(log)
                    self.events.append(kill)
                    print(f"OOMKilled: {kill['pod']}/{kill['container']} restarts={kill['restarts']} "
                          f"at {kill['finished']}", flush=True)
            self.stop.wait(KILL_POLL_SECONDS)

    def __enter__(self):
        for pod in self.host.pods(self.selector):
            for kill in capture.kills(pod):
                if kill["container"] == self.container:
                    self.seen[(kill["pod"], kill["uid"])] = kill["restarts"]
        for target in (self.sample, self.poll):
            threading.Thread(target=target, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop.set()


def read_samples(path: Path) -> list[dict]:
    """The samples a run wrote, skipping a line the sampler was still writing when the run ended."""
    samples = []
    if path.exists():
        for line in path.open():
            try:
                samples.append(json.loads(line))
            except ValueError:
                continue
    return samples


def render_report(container: str, kills: list[dict], samples: list[dict]) -> str:
    """The kills, and for each the share of every process class in the killed pod's last sample."""
    pods = sorted({x.get("pod") for x in samples if x.get("pod")})
    lines = [f"# OOMKills: {len(kills)}", "",
             f"Samples: {len(samples)} from {len(pods)} pod(s) of `{container}`: {', '.join(pods) or 'none'}.", ""]
    lines += [f"- {k['finished']} {k['pod']}/{k['container']} exit {k['exit_code']} (restart {k['restarts']})"
              for k in kills]
    for kill in kills:
        finished = datetime.fromisoformat(kill["finished"].replace("Z", "+00:00")).timestamp()
        sample = capture.sample_at_kill(samples, kill["pod"], finished)
        lines += ["", f"## {container}, kill at {kill['finished']}"]
        if sample is None:
            lines += ["", "No sample of this pod in the seconds before the kill."]
            continue
        when = time.strftime(CLOCK_FORMAT, time.gmtime(sample["ts"]))
        cgroup = f"{int(sample['cg_current']) // capture.MIB} MiB" if sample.get("cg_current") else "unknown (no cgroup v2)"
        lines += ["", f"Last sample {when} UTC: cgroup {cgroup}, of which the "
                  f"sampler {int(sample.get('sampler_rss_kib') or 0) // capture.KIB} MiB (not in the shares below).",
                  "", "| Share | MiB | Process |", "| --- | --- | --- |"]
        lines += [f"| {share:.1f}% | {mib} | `{name}` |" for name, share, mib in capture.breakdown(sample)]
    return "\n".join(lines) + "\n"


def report(out: Path, recorder: Recorder, strict: bool = True) -> None:
    samples = read_samples(out / SAMPLES_FILE.format(container=recorder.container))
    text = render_report(recorder.container, recorder.events, samples)
    (out / REPORT_FILE).write_text(text)
    print(text)
    if strict and not samples:
        # Zero kills with zero samples is not a result: nothing was watched.
        raise SystemExit(f"no samples of {recorder.container}: check --watch, --host-context and --namespace")


def limits_for(size: str, field: str) -> dict:
    """The limits a scenario sets for ``field`` at ``size``."""
    return SIZES[size][field]


def scenario(args: argparse.Namespace, field: str, container: str, selector: str, start,
             python: str = CONTAINER_PYTHON) -> None:
    """Run one scenario. ``field`` names the PlatformAgent resources (RESOURCE_PATHS) to set and restore."""
    host = Host(args)
    out = args.out / f"{args.command}-{time.strftime(RUN_DIR_FORMAT, time.gmtime())}"
    out.mkdir(parents=True)
    limits = limits_for(args.size, field)
    runtime_class = bool(getattr(args, "runc", False))
    with lease(host, f"oom-repro {field}") as lease_lost:
        host.stop = lease_lost
        before = host.platform_agent()["spec"]
        (out / SNAPSHOT_FILE).write_text(json.dumps(before, indent=1))
        stopped: BaseException | None = None
        with Recorder(host, out, selector, container, python) as recorder:
            try:
                host.patch(resources_patch(before, RESOURCE_PATHS[field], limits))
                start(host, before, limits)
                deadline = time.monotonic() + args.seconds
                while time.monotonic() < deadline and len(recorder.events) < args.kills and not lease_lost.is_set():
                    time.sleep(KILL_POLL_SECONDS)
                if lease_lost.is_set():
                    stopped = SystemExit("stopped: the live-test lease could not be renewed")
            except BaseException as exc:  # a failed trigger, Ctrl-C or a signal: restore and report first
                stopped = exc
            finally:
                try:
                    host.patch(restore_patch(before, host.platform_agent()["spec"], RESOURCE_PATHS[field], runtime_class))
                except SystemExit as exc:
                    print(f"RESTORE FAILED; put the spec back from {out / SNAPSHOT_FILE}: {exc}", flush=True)
                    raise
        # A run that stopped reports what it has; its own reason, not "no samples", is what it ends with.
        report(out, recorder, strict=stopped is None)
        if stopped is not None:
            raise stopped


def proxy(args: argparse.Namespace) -> None:
    selector, container, field, python = WATCH_TARGETS[args.watch]

    def start(host: Host, _before: dict, limits: dict) -> None:
        wait_for_limits(host, selector, container, limits)
        if field == "credentialProxy":
            host.kubectl("rollout", "status", PROXY_DEPLOYMENT, f"--timeout={ROLLOUT_TIMEOUT}")
        pod = host.wait_running(GATEWAY_SELECTOR)
        host.kubectl("exec", pod, "-c", AGENT_CONTAINER, "--", "env", f"HERMES_HOME={PLATFORM_HOME}", CONTAINER_PYTHON,
                     "-c", JOB_TRIGGER_SCRIPT, AGENT_SCRIPTS_DIR, AGENT_DATA_DIR, *PROXY_TRIGGER_JOBS)

    scenario(args, field, container, selector, start, python)


def watcher(args: argparse.Namespace) -> None:
    def start(host: Host, before: dict, limits: dict) -> None:
        if args.runc and at(before, RUNTIME_CLASS_KEYS):
            # gVisor applies one limit to the whole sandbox, so a container's own limit does not kill it.
            host.patch([{"op": "remove", "path": RUNTIME_CLASS_PATH}])
        # The new limits roll the pod, and its watcher starts with every fleet cluster's profile. A pod
        # that already had them is not rolled, so it is restarted for the same start-up lists.
        pod = wait_for_limits(host, GATEWAY_SELECTOR, WATCHER_CONTAINER, limits)
        if pod in existing:
            host.kubectl("delete", "pod", pod, "--wait=true", timeout=FLEET_WAIT_SECONDS)
            wait_for_limits(host, GATEWAY_SELECTOR, WATCHER_CONTAINER, limits)

    existing = {p["metadata"]["name"] for p in Host(args).pods(GATEWAY_SELECTOR)}

    scenario(args, "agentAPIAuth", WATCHER_CONTAINER, GATEWAY_SELECTOR, start)


def parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project", default=os.environ.get(DEFAULT_PROJECT_ENV))
    p.add_argument("--zone", default=DEFAULT_ZONE)
    p.add_argument("--prefix", default=DEFAULT_PREFIX)
    p.add_argument("--host-context", help="kubectl context of the install under test")
    p.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--nodes", type=int, default=DEFAULT_NODES)
    p.add_argument("--namespaces", type=int, default=DEFAULT_NAMESPACES)
    p.add_argument("--deployments", type=int, default=DEFAULT_DEPLOYMENTS)
    p.add_argument("--replicas", type=int, default=DEFAULT_REPLICAS)
    sub = p.add_subparsers(dest="command", required=True)
    fleet = sub.add_parser("fleet").add_subparsers(dest="action", required=True)
    create = fleet.add_parser("create")
    create.add_argument("--clusters", type=int, required=True)
    create.add_argument("--first", type=int, default=1)
    create.add_argument("--events-only", action="store_true", help="no KWOK nodes or Pods; Events only")
    load = fleet.add_parser("load", help="grow named fleet clusters to --nodes and the workload size given")
    load.add_argument("--names", required=True, help="comma-separated full fleet clusters to grow")
    fleet.add_parser("delete")
    ev = sub.add_parser("events")
    ev.add_argument("--count", type=int, required=True, help="Events to create in each fleet cluster")
    ev.add_argument("--pad-to", type=int, default=0, help="lengthen each message to this many characters")
    ev.add_argument("--threads", type=int, default=DEFAULT_EVENT_THREADS)
    for name in ("proxy", "watcher"):
        s = sub.add_parser(name)
        s.add_argument("--seconds", type=int, default=DEFAULT_SCENARIO_SECONDS)
        s.add_argument("--kills", type=int, default=DEFAULT_KILLS, help="stop after this many OOMKills")
        s.add_argument("--size", choices=sorted(SIZES), default=DEFAULT_SIZE, help="container limits to set")
        if name == "proxy":
            s.add_argument("--watch", choices=sorted(WATCH_TARGETS), default=DEFAULT_WATCH,
                           help="the container the audit's kubectl runs in: shell (default), or proxy on builds before the Kubernetes API proxy")
        if name == "watcher":
            s.add_argument("--runc", action="store_true", help="run the agent pod without its gVisor runtime class")
    args = p.parse_args(argv)
    if args.command in ("fleet", "events") and not args.project:
        p.error(f"--project or {DEFAULT_PROJECT_ENV} is required")
    if args.command in ("proxy", "watcher") and not args.host_context:
        p.error("--host-context is required")
    args.out.mkdir(parents=True, exist_ok=True)
    return args


FLEET_ACTIONS = {"create": fleet_create, "load": fleet_load, "delete": fleet_delete}


def stop_on_signal(signum, _frame) -> None:
    # SIGTERM or SIGHUP (a closed terminal) unwinds like Ctrl-C, so a scenario restores and releases.
    raise SystemExit(f"stopped by signal {signum}")


def main(argv: list[str] | None = None) -> int:
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop_on_signal)
    args = parse(argv)
    handlers = {"events": events, "proxy": proxy, "watcher": watcher}
    if args.command == "fleet":
        FLEET_ACTIONS[args.action](args)
    else:
        handlers[args.command](args)
    return 0
