"""Tests for hack/fleet-fixture-state.py, the designed-state half of the fleet check.

The runner (hack/fleet-kubeconfigs.sh) confirms a fixture's objects are PRESENT;
this script confirms they are in the state the cases depend on (#1544). What
these tests pin, with ``kubectl``, ``gcloud`` and ``gh`` stubbed:

* the real catalog loads, and every role declares at least one assertion;
* each operator and path form does what the catalog's ``state_syntax`` says;
* a healthy fleet converges on the first pass and leaves no ``.drift`` file;
* a fixture out of shape is DRIFTED: a ``.drift`` file naming the assertion and
  what was observed, a WARNING naming the role, and a summary count;
* ``--wait`` re-reads until the fixture converges (the crashloop's first
  restart) and only records drift at the deadline;
* a read that failed is "not checked", never drift -- no file, so a fixture
  nobody saw is never reported out of shape -- while one assertion read and
  failed beside an unread one is still drift;
* the ``cluster`` subject reads the slot's recorded name and location, and the
  minor-behind operator compares against the cluster's own channel default;
* the drift peers (slots a, b, d) keep the authorized-networks block and the
  outlier (slot c) is past the drift SOP's 24-hour age gate, which never
  holds a ``--wait``;
* a re-run that finds a role converged removes a stale ``.drift`` file;
* a malformed catalog or a directory the runner did not write is exit 1;
* a tool that is not installed comes back as exit status 127, a tool that
  cannot be run as 126 and a tool that runs past its timeout as 124, and a
  read that got one of them is "not checked".

The stubs answer from a JSON "world" file so each test names only its own
defect; a key whose value is a list (a kubectl subject or a describe) is
answered in order, once per call, which is how a fixture that converges between
passes is expressed.

Most tests run the script in the test process (``run_script``): its ``main()``
is called there, and each ``subprocess.run`` of a stubbed tool runs the stub's
code there. A few run it as a new process with the stubs as programs on its
``PATH`` (``run_script_process``); ``PassTest`` names them and what they hold.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import traceback
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

_REPO = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "hack" / "fleet-fixture-state.py"
_CATALOG = _REPO / "bench" / "tf" / "fleet" / "fixtures.json"
# The pool verifier: the one copy of what provisioning seeds into a pool
# project's GitOps repository, which the script's repository subject reads
# through (#5827).
_VERIFIER = _REPO / "scripts" / "verify_ci_pool_project.py"


def _load_module(path=_SCRIPT, name="fleet_fixture_state"):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ffs = _load_module()
verifier = _load_module(_VERIFIER, "verify_ci_pool_project")

# The declared-intent note as the fixture gate reads it: its path in the pool
# repository, the blob sha of the content provisioning seeds, and
# the blob the forty registered projects carried after the sixth declaration
# landed (#5827), which the gate must read as drift.
_NOTE_PATH = verifier.GITOPS_INTENT_NOTE_PATH
_NOTE_SUBJECT = f"repository/{_NOTE_PATH}"
_SEEDED_NOTE_SHA = verifier.gitops_seeded_blob_sha(_NOTE_PATH)
_FIVE_ITEM_NOTE_SHA = "3c5790d5a1b2c3d4e5f60718293a4b5c6d7e8f90"
_POOL_REPO = "gke-agentic/kube-agents-evals-infra"
# The catalog roles that carry the note assertion: every declared-* role.
_NOTE_ROLES = {"declared-no-pdb-workload", "declared-token-workload", "declared-overrequest-workload", "declared-single-zone-pool", "declared-mtu-peering", "declared-orphan-snapshot"}


class _SteppedClock:
    """A stand-in for the script's `time`: monotonic() moves only by sleep().

    `between_passes` runs at every sleep, so a test can assert what the script
    has not done yet. A sleep that does not move the clock, a hundredth sleep,
    or a fiftieth read of it fails rather than looping forever on a clock
    that never runs out: the read cap catches a loop that polls without
    sleeping at all.
    """

    def __init__(self, between_passes=lambda: None) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.reads = 0
        self.between_passes = between_passes

    def monotonic(self) -> float:
        self.reads += 1
        assert self.reads < 50, "fifty clock reads without reaching the deadline"
        return self.now

    def sleep(self, seconds: float) -> None:
        assert seconds > 0, f"sleep({seconds}) does not move the clock; the loop would never reach its deadline"
        assert len(self.sleeps) < 100, "a hundred sleeps without reaching the deadline"
        self.between_passes()
        self.sleeps.append(seconds)
        self.now += seconds


_STUB_KUBECTL = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write("kubectl " + " ".join(args) + "\n")
with open(os.environ["STUB_WORLD"]) as fh:
    world = json.load(fh)
i = args.index("get")
kind = args[i + 1]
# Real kubectl refuses a resource type it does not serve; a subject split on
# the wrong delimiter must fail here the way it would against a cluster,
# not be quietly rebuilt into the world's key.
if "?" in kind or "/" in kind:
    print(f'error: the server doesn\'t have a resource type "{kind}"', file=sys.stderr)
    sys.exit(1)
name = None
selector = None
rest = args[i + 2:]
j = 0
while j < len(rest):
    a = rest[j]
    if a == "-l":
        selector = rest[j + 1]; j += 2
    elif a in ("-n", "-o"):
        j += 2
    elif a.startswith("-"):
        j += 1
    else:
        name = a; j += 1
key = f"{kind}/{name}" if name else f"{kind}?{selector or ''}"
answers = world.get("kubectl", {})
if key not in answers:
    if name:
        print(f'Error from server (NotFound): {kind} "{name}" not found', file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"items": []}))
    sys.exit(0)
value = answers[key]
if isinstance(value, list):
    counter = os.environ["STUB_LOG"] + ".counts"
    counts = {}
    if os.path.exists(counter):
        with open(counter) as fh:
            counts = json.load(fh)
    n = counts.get(key, 0)
    counts[key] = n + 1
    with open(counter, "w") as fh:
        json.dump(counts, fh)
    value = value[min(n, len(value) - 1)]
if value == "UNREACHABLE":
    print("Unable to connect to the server: dial tcp 10.0.0.1:443: i/o timeout", file=sys.stderr)
    sys.exit(1)
print(json.dumps(value))
'''

# The startup-fail VM's console around a boot, and the same console after
# three weeks of guest-agent output pushed that boot out of the buffer; both
# shortened from real reads of the planted VM.
_FRESH_SERIAL = (
    "2026-10-07T02:18:03.055302+00:00 startup-fail-seeded google_metadata_script_runner[845]: "
    'Running metadata script runner (version 20260715.00) for "startup" event\n'
    "2026-10-07T02:18:03.065033+00:00 startup-fail-seeded google_metadata_script_runner[845]: "
    'Script "startup-script" failed with error: exit status 1\n'
)
_WRAPPED_SERIAL = (
    "-seeded mwlid_extension[723]: MWLID credential refresh is not enabled, skipping run\n"
    "2026-10-17T02:48:17.439738+00:00 startup-fail-seeded mwlid_extension[723]: "
    "MWLID credential refresh is not enabled, skipping run\n"
)
# The serial-flood VM's console after its flood pass: one pass writes about
# twice the 1 MiB buffer in counted filler lines, so only the filler remains
# and the boot's failure lines are out. The tail of a real pass.
_FLOODED_SERIAL = (
    "eval fixture: serial-flood filler 40958 of 40960\n"
    "eval fixture: serial-flood filler 40959 of 40960\n"
    "eval fixture: serial-flood filler 40960 of 40960\n"
)

_STUB_GCLOUD = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write("gcloud " + " ".join(args) + "\n")
with open(os.environ["STUB_WORLD"]) as fh:
    world = json.load(fh)
if args[:3] == ["container", "clusters", "describe"]:
    doc = world.get("describe", {}).get(args[3])
    if isinstance(doc, list):
        counter = os.environ["STUB_LOG"] + ".describes"
        counts = {}
        if os.path.exists(counter):
            with open(counter) as fh:
                counts = json.load(fh)
        n = counts.get(args[3], 0)
        counts[args[3]] = n + 1
        with open(counter, "w") as fh:
            json.dump(counts, fh)
        doc = doc[min(n, len(doc) - 1)]
    if doc is None:
        print(f"ERROR: (gcloud.container.clusters.describe) Not found: {args[3]}", file=sys.stderr)
        sys.exit(1)
    if doc == "UNREACHABLE":
        print("ERROR: (gcloud.container.clusters.describe) ResponseError: code=503", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(doc))
elif args[:3] == ["compute", "instances", "get-serial-port-output"]:
    # `compute instances get-serial-port-output NAME --zone=Z --port=1 --project=P`
    text = world.get("serial", {}).get(args[3])
    if text is None or text == "UNREACHABLE":
        print(f"ERROR: (gcloud.compute.instances.get-serial-port-output) Required 'compute.instances.getSerialPortOutput' permission for {args[3]}", file=sys.stderr)
        sys.exit(1)
    sys.stdout.write(text)
elif args[0] == "compute" and args[2] == "list":
    # `compute <collection> list --project P --filter=name ~ ^<prefix> --format json`
    answer = world.get("compute", {}).get(args[1], [])
    if answer == "UNREACHABLE":
        print(f"ERROR: (gcloud.compute.{args[1]}.list) Some requests did not succeed: Required 'compute.{args[1]}.list' permission", file=sys.stderr)
        sys.exit(1)
    if answer == "PARTIAL":
        # gcloud's default for a list that failed in part: a warning, what it
        # could read (here nothing), and exit 0. After an empty answer its last
        # line is the warning about the filter's key.
        print("WARNING: Some requests did not succeed.\n - Internal error in zone us-central1-a\n - Internal error in zone us-central1-b\n", file=sys.stderr)
        print("WARNING: The following filter keys were not present in any resource : name", file=sys.stderr)
        print("[]")
        sys.exit(0)
    # `name ~ ^<prefix>` is a prefix read; `name ~ ^<name>$` (the disk read
    # behind source_disk_gone) is an exact one.
    pattern = next(a for a in args if a.startswith("--filter=name ~ ^"))[len("--filter=name ~ ^"):]
    if pattern.endswith("$"):
        matched = [item for item in answer if item["name"] == pattern[:-1]]
    else:
        matched = [item for item in answer if item["name"].startswith(pattern)]
    print(json.dumps(matched))
elif args[:2] == ["container", "get-server-config"]:
    if world.get("server_config") == "UNREACHABLE":
        print("ERROR: (gcloud.container.get-server-config) ResponseError: code=503", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(world.get("server_config", {})))
else:
    print("unexpected gcloud call: " + " ".join(args), file=sys.stderr)
    sys.exit(64)
'''

# The gh stub: `gh api repos/<org>/<repo>[/contents/<path>]`, answered from
# world["github"] = {"<org>/<repo>": {"<path>": "<blob sha>"}}. No "github"
# key is a gh with no credential (exit 4, as gh prints it); a repository
# absent from the map is gh's 404, which it answers alike for a private
# repository the token cannot see; "UNREACHABLE" is a transport failure.
_STUB_GH = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write("gh " + " ".join(args) + "\n")
with open(os.environ["STUB_WORLD"]) as fh:
    world = json.load(fh)
github = world.get("github")
if github is None:
    print("To get started with GitHub CLI, please run:  gh auth login", file=sys.stderr)
    print("Alternatively, populate the GH_TOKEN environment variable with a GitHub API authentication token.", file=sys.stderr)
    sys.exit(4)
if github == "UNREACHABLE":
    print('Get "https://api.github.com/repos/x": dial tcp 140.82.112.5:443: i/o timeout', file=sys.stderr)
    sys.exit(1)
target = next(a for a in args[args.index("api") + 1:] if not a.startswith("-") and not a.startswith("Accept:"))
parts = target.split("/", 3)
slug = f"{parts[1]}/{parts[2]}"
repo = github.get(slug)
if repo is None:
    print("gh: Not Found (HTTP 404)", file=sys.stderr)
    sys.exit(1)
if len(parts) == 3:
    print(json.dumps({"full_name": slug, "default_branch": "main"}))
    sys.exit(0)
path = parts[3][len("contents/"):]
sha = repo.get(path)
if sha is None:
    print("gh: Not Found (HTTP 404)", file=sys.stderr)
    sys.exit(1)
print(json.dumps({"sha": sha, "path": path, "type": "file", "size": 1456, "encoding": "base64", "content": ""}))
'''


def _pod(
    *,
    restarts: int,
    last_reason: str | None,
    phase: str = "Running",
    waiting_reason: str | None = None,
    ready: bool | None = None,
) -> dict:
    # A container is ready when it runs; one waiting in a back-off is not,
    # though its pod's phase stays Running under restartPolicy Always.
    if ready is None:
        ready = phase == "Running" and waiting_reason is None
    status: dict = {"restartCount": restarts, "lastState": {}, "ready": ready}
    if last_reason:
        status["lastState"] = {"terminated": {"reason": last_reason, "exitCode": 137}}
    if waiting_reason:
        status["state"] = {"waiting": {"reason": waiting_reason}}
    return {"status": {"phase": phase, "containerStatuses": [status]}}


def _pods(*pods: dict) -> dict:
    return {"items": list(pods)}


def _node(*, ready: str = "True", taint: str | None = "idle-batch") -> dict:
    node = {"spec": {"taints": []}, "status": {"conditions": [{"type": "Ready", "status": ready}]}}
    if taint:
        node["spec"]["taints"] = [{"key": "seeded-role", "value": taint, "effect": "NoSchedule"}]
    return node


def _pool_node(name: str, *, allocatable: str = "1930m") -> dict:
    return {"metadata": {"name": name}, "status": {"allocatable": {"cpu": allocatable}, "conditions": [{"type": "Ready", "status": "True"}]}}


def _bound_pod(node: str | None, cpu: str, *, phase: str = "Running", init_cpu: str | None = None) -> dict:
    """A pod the scheduler has placed (or, with node None, has not), with one
    container requesting ``cpu`` and optionally one init container."""
    spec: dict = {"containers": [{"resources": {"requests": {"cpu": cpu}}}]}
    if node:
        spec["nodeName"] = node
    if init_cpu:
        spec["initContainers"] = [{"resources": {"requests": {"cpu": init_cpu}}}]
    return {"spec": spec, "status": {"phase": phase}}


def _writer_job(*, failed: bool = False, counter: int | None = None) -> dict:
    """A retained writer Job. ``failed`` sets the Failed condition; ``counter`` is the
    pod-failure counter, which a Job whose pod was never admitted leaves absent."""
    if not failed:
        return {"status": {"succeeded": 1, "conditions": [{"type": "Complete", "status": "True"}]}}
    status: dict = {"conditions": [{"type": "Failed", "status": "True"}]}
    if counter is not None:
        status["failed"] = counter
    return {"status": status}


def _writer_cronjob(*, suspend: bool = False, schedule: str = "*/10 * * * *") -> dict:
    return {
        "spec": {
            "schedule": schedule,
            "suspend": suspend,
            "jobTemplate": {"spec": {"template": {"spec": {"serviceAccountName": "legacy-endpoints-writer"}}}},
        }
    }


# The three finished Jobs of the generated-name-job-family role (defects-a.tf).
_FAMILY_JOBS = (
    "job/reindex-8daede4e-4620-426c-9e07-58f5f0505f39",
    "job/reindex-1657c63c-8745-4ba0-a6a9-1d4365c43041",
    "job/reindex-55fe5b6f-82b7-4fef-bfe3-877e9e5200b2",
)


def _family_job(*, finished_hours_ago: float = 24 * 30) -> dict:
    """One finished Job of the generated-name family: it succeeded, and it has no TTL and no owner."""
    return {
        "metadata": {"labels": {"app.kubernetes.io/part-of": "reindex"}},
        "spec": {},
        "status": {"succeeded": 1, "completionTime": _created(finished_hours_ago)},
    }


# The long-named Deployment of the long-name-replaced-pod role (defects-a.tf).
_LONG_NAME_DEPLOYMENT = "deployment/settlement-batch-reconciliation-exporter-for-partner-feeds"
_LONG_NAME_ROLE = "role/long-name-pod-cycle"
# The two Deployments of the cpu-burst-and-flat role, each with its mode and its CPU size.
_CPU_CYCLE = (("deployment/cpu-flat", "flat", "100m"), ("deployment/cpu-burst", "burst", "200m"))


def _long_name_deployment() -> dict:
    """The long-named Deployment between two replacements: one ready pod."""
    container = {"resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "200m", "memory": "256Mi"}}}
    return {
        "spec": {"replicas": 1, "template": {"spec": {"serviceAccountName": "long-name-pod-cycle", "containers": [container]}}},
        "status": {"replicas": 1, "readyReplicas": 1},
    }


def _cpu_cycle_deployment(mode: str, cpu: str) -> dict:
    container = {
        "resources": {"requests": {"cpu": cpu, "memory": "64Mi"}, "limits": {"cpu": cpu, "memory": "128Mi"}},
        "env": [{"name": "CPU_MODE", "value": mode}],
    }
    return {"spec": {"replicas": 1, "template": {"spec": {"containers": [container]}}}, "status": {"replicas": 1, "readyReplicas": 1}}


def _future(days: int = 30) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _created(hours: float = 24 * 30) -> str:
    """A `createTime` `hours` ago, in the offset form GKE prints."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


# A `compute snapshots list` item (main.tf, terraform_data.orphan_snapshots):
# the source disk's URL survives the disk's deletion, and a snapshot a schedule
# took would carry `sourceSnapshotSchedulePolicy`.
_SNAPSHOT_SOURCE_URL = "https://www.googleapis.com/compute/v1/projects/kube-agents-evals/zones/us-central1-a/disks/orphan-snap-source-seeded"
_SNAPSHOT_SOURCE_DISK = {"name": "orphan-snap-source-seeded", "zone": "https://www.googleapis.com/compute/v1/projects/kube-agents-evals/zones/us-central1-a", "sizeGb": "10"}


def _snapshot(name: str, *, created: str | None = None, status: str = "READY", schedule: str | None = None) -> dict:
    doc = {"name": name, "status": status, "sourceDisk": _SNAPSHOT_SOURCE_URL, "creationTimestamp": created or _created(24 * 100), "diskSizeGb": "10"}
    if schedule:
        doc["sourceSnapshotSchedulePolicy"] = schedule
    return doc


# The block slots a, b and d carry (bench/tf/fleet/main.tf), as `clusters describe` prints it.
_OPEN_BLOCK = {"enabled": True, "cidrBlocks": [{"displayName": "open-for-eval", "cidrBlock": "0.0.0.0/0"}]}


# seeded-a's declared-zonal-pool as `clusters describe` reports it (main.tf):
# autoscaling on, tainted, no node. The declared-single-zone-pool role reads
# it off the cluster document, the one place a pool with no node shows.
_DECLARED_ZONAL_POOL = {
    "name": "declared-zonal-pool",
    "status": "RUNNING",
    "locations": ["us-central1-a"],
    "autoscaling": {"enabled": True, "minNodeCount": 0, "maxNodeCount": 2},
    "config": {"machineType": "e2-small", "taints": [{"key": "seeded-role", "value": "declared-zonal", "effect": "NO_SCHEDULE"}]},
}

# seeded-c's ComputeClass reserved-ccc, the missing-reservation-compute-class
# role, and the nodes that carry its class label (none, by design).
_RESERVED_CCC = "computeclass/reserved-ccc"
_RESERVED_CCC_NODES = "node?cloud.google.com/compute-class=reserved-ccc"


def _reserved_ccc() -> dict:
    """The class as GKE holds it: the spec that the stack plants, the field
    that GKE adds, and the status that GKE writes for the missing reservation."""
    message = 'Reservation "seeded-absent-reservation" either does not exist or not accessible.'
    return {
        "spec": {
            "priorities": [
                {"machineType": "n2-standard-2", "reservations": {"affinity": "Specific", "specific": [{"name": "seeded-absent-reservation"}]}},
                {"machineType": "e2-standard-4"},
            ],
            "nodePoolAutoCreation": {"enabled": True, "shieldedInstanceConfig": {"enableIntegrityMonitoring": True, "enableSecureBoot": False}},
            "nodePoolConfig": {"taints": [{"key": "dedicated", "value": "reserved-ccc", "effect": "NoSchedule"}]},
            "whenUnsatisfiable": "DoNotScaleUp",
        },
        "status": {
            "conditions": [
                {"type": "CrdMisconfigured", "status": "True", "reason": "ReservationNotFound", "message": message},
                {"type": "Health", "status": "False", "reason": "Health", "message": "Crd is not healthy."},
            ]
        },
    }


# seeded-a's missing-reservation-pool as `clusters describe` reports it
# (main.tf): no autoscaling, tainted, no node, bound to one reservation name.
_MISSING_RESERVATION_POOL = {
    "name": "missing-reservation-pool",
    "status": "RUNNING",
    "locations": ["us-central1-a"],
    "config": {
        "machineType": "n2-standard-2",
        "taints": [{"key": "seeded-role", "value": "missing-reservation", "effect": "NO_SCHEDULE"}],
        "reservationAffinity": {"consumeReservationType": "SPECIFIC_RESERVATION", "key": "compute.googleapis.com/reservation-name", "values": ["seeded-absent-reservation"]},
    },
}


def _peer(*, block: dict | None = _OPEN_BLOCK, created: str | None = None) -> dict:
    """A drift-cohort peer's cluster document (slot a or d).

    Carries seeded-a's declared pool and its reservation pool so a peer
    document a test rewrites for its authorized-networks block still converges
    the two pools' roles; slot d's document carries them too, which no role on
    slot d reads.
    """
    doc = {"currentMasterVersion": "1.34.1-gke.1", "createTime": created or _created(), "nodePools": [_DECLARED_ZONAL_POOL, _MISSING_RESERVATION_POOL]}
    if block is not None:
        doc["masterAuthorizedNetworksConfig"] = block
    return doc


def _cluster_c(*, disk: int = 50, pids_limit: str | None = "4096", machine: str = "e2-standard-2", created: str | None = None) -> dict:
    """Slot c's cluster document: no authorized-networks block, and the
    default pool bench/tf/fleet/main.tf plants as the pool-level outlier, a
    50 GB disk with podPidsLimit 4096 (a string, as gcloud prints an int64).
    `pids_limit=None` leaves the pool with no kubeletConfig at all, as every
    peer pool reads."""
    # Since the ComputeClass fixtures (defects-c.tf): the pool carries the
    # class label and the NodeLocal DNSCache add-on is off (GKE omits the
    # false boolean, so the block is empty).
    config: dict = {"diskSizeGb": disk, "machineType": machine, "labels": {"cloud.google.com/compute-class": "general-ccc"}}
    if pids_limit is not None:
        config["kubeletConfig"] = {"podPidsLimit": pids_limit}
    return {
        "currentMasterVersion": "1.34.1-gke.1",
        "createTime": created or _created(),
        "nodePools": [{"name": "default-pool", "status": "RUNNING", "config": config}],
        "addonsConfig": {"dnsCacheConfig": {}},
    }


def _cluster_b(*, master: str = "1.33.4-gke.1134000", channel: str | None = "REGULAR", end=None, scope="NO_MINOR_UPGRADES", surge: int | None = None) -> dict:
    no_surge = {"maxUnavailable": 1}
    if surge is not None:
        no_surge["maxSurge"] = surge
    doc = {
        "currentMasterVersion": master,
        "createTime": _created(),
        "masterAuthorizedNetworksConfig": _OPEN_BLOCK,
        # The pools as `clusters describe` reports them. The
        # readiness-surge-blocked role asserts the no-surge pool's
        # maxUnavailable is 1 and maxSurge is absent, because the API omits
        # maxSurge when it is 0; `surge` gives the pool the surge capacity
        # that heals the fixture. The readiness-lb-drain-timeout role asserts
        # the default pool's status, the pool its backend service fronts.
        "nodePools": [
            {"name": "default-pool", "status": "RUNNING", "upgradeSettings": {"maxSurge": 2}},
            {"name": "no-surge-pool", "upgradeSettings": no_surge},
        ],
        "maintenancePolicy": {
            "window": {
                "maintenanceExclusions": {
                    "hold-the-minor-lag": {
                        "startTime": "2026-09-01T00:00:00Z",
                        "endTime": end or _future(),
                        "maintenanceExclusionOptions": {"scope": scope},
                    }
                }
            }
        },
    }
    if channel:
        doc["releaseChannel"] = {"channel": channel}
    return doc


def _healthy_world() -> dict:
    """Every role in its designed state, as the stubs answer it."""
    return {
        # The pool repository, with the declared-intent note as provisioning
        # seeds it: the blob the four declared-* roles assert on.
        "github": {_POOL_REPO: {_NOTE_PATH: _SEEDED_NOTE_SHA}},
        "kubectl": {
            "pod?app=payments-api": _pods(_pod(restarts=3, last_reason="OOMKilled")),
            "deployment/checkout-gateway": {"status": {"readyReplicas": 2, "replicas": 2}},
            "deployment/notification-relay": {"status": {"readyReplicas": 2, "replicas": 2}},
            "deployment/token-reader": {"status": {"readyReplicas": 1, "replicas": 1}},
            "deployment/token-sidecar": {"status": {"readyReplicas": 1, "replicas": 1}},
            "serviceaccount/default": {"automountServiceAccountToken": True},
            "deployment/burst-ingest": {"status": {"readyReplicas": 2, "replicas": 2}, "spec": {"template": {"spec": {"containers": [{"resources": {"requests": {"cpu": "10m", "memory": "128Mi"}}}]}}}},
            "deployment/calibration-eb84fcb6-cb5b-4a66-ae6e-76d9f868a0d6": {"metadata": {"labels": {"app.kubernetes.io/part-of": "calibration"}}, "status": {"readyReplicas": 2, "replicas": 2}, "spec": {"template": {"spec": {"containers": [{"resources": {"requests": {"cpu": "10m", "memory": "128Mi"}}}]}}}},
            "deployment/calibration-374472b8-d3be-4c23-879f-9a44a5775c2b": {"metadata": {"labels": {"app.kubernetes.io/part-of": "calibration"}}, "status": {"readyReplicas": 2, "replicas": 2}, "spec": {"template": {"spec": {"containers": [{"resources": {"requests": {"cpu": "10m", "memory": "128Mi"}}}]}}}},
            "deployment/calibration-20192efa-2b20-4f64-87c3-a3919cdcd132": {"metadata": {"labels": {"app.kubernetes.io/part-of": "calibration"}}, "status": {"readyReplicas": 2, "replicas": 2}, "spec": {"template": {"spec": {"containers": [{"resources": {"requests": {"cpu": "10m", "memory": "128Mi"}}}]}}}},
            **{subject: _family_job() for subject in _FAMILY_JOBS},
            _LONG_NAME_DEPLOYMENT: _long_name_deployment(),
            _LONG_NAME_ROLE: {"rules": [{"apiGroups": [""], "resources": ["pods"], "verbs": ["delete"]}]},
            **{subject: _cpu_cycle_deployment(mode, cpu) for subject, mode, cpu in _CPU_CYCLE},
            "deployment/ledger-writer": {"status": {"readyReplicas": 2, "replicas": 2}, "spec": {"template": {"spec": {"containers": [{"resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"cpu": "10m", "memory": "32Mi"}}}]}}}},
            "poddisruptionbudget?": {"items": []},
            "networkpolicy?": {"items": []},
            "deployment/inference-server": {"status": {"readyReplicas": 1, "replicas": 3}},
            "pod?app=inference-server": _pods(
                _pod(restarts=0, last_reason=None), _pod(restarts=0, last_reason=None, phase="Pending")
            ),
            "clusterrolebinding/debug-binding": {
                "roleRef": {"name": "cluster-admin"},
                "subjects": [{"kind": "ServiceAccount", "name": "default", "namespace": "seeded-security"}],
            },
            "node?cloud.google.com/gke-nodepool=idle-batch-pool": {"items": [_node()]},
            "node?cloud.google.com/gke-nodepool=default-pool": {"items": [_pool_node("gke-seeded-a-default-pool-0r0x"), _pool_node("gke-seeded-a-default-pool-z7br")]},
            # Every namespace's pods, as `kubectl get pod -A` answers: GKE's set
            # on both nodes, a fixture on each, a finished Job pod and an
            # unbound Pending pod that count for nothing.
            "pod?": _pods(
                _bound_pod("gke-seeded-a-default-pool-0r0x", "270m"), _bound_pod("gke-seeded-a-default-pool-0r0x", "100m"), _bound_pod("gke-seeded-a-default-pool-0r0x", "10m"),
                _bound_pod("gke-seeded-a-default-pool-z7br", "270m"), _bound_pod("gke-seeded-a-default-pool-z7br", "100m"), _bound_pod("gke-seeded-a-default-pool-z7br", "10m", init_cpu="300m"),
                _bound_pod("gke-seeded-a-default-pool-z7br", "500m", phase="Succeeded"), _bound_pod(None, "10m", phase="Pending"),
            ),
            "deployment/inventory-api": {
                "status": {
                    "conditions": [
                        {
                            "type": "Progressing",
                            "status": "False",
                            "reason": "ProgressDeadlineExceeded",
                        }
                    ]
                }
            },
            "pod?app=inventory-api": _pods(
                _pod(restarts=0, last_reason=None, phase="Pending", waiting_reason="CreateContainerConfigError")
            ),
            "cronjob/legacy-endpoints-writer": _writer_cronjob(),
            "endpoints/legacy-endpoints-lane": {"subsets": [{"addresses": [{"ip": "192.0.2.10"}], "ports": [{"port": 9}]}]},
            "job?app=legacy-endpoints-writer": {"items": [_writer_job()]},
            # seeded-b's readiness pair and the fail-closed webhook. The
            # no-surge pool's node must be Ready or the pinned workload is
            # Pending for a reason that is not the fixture, which is the
            # whole point of asserting on the pair rather than on the pool's
            # upgrade settings alone.
            "node?cloud.google.com/gke-nodepool=no-surge-pool": {"items": [_node(taint=None)]},
            "namespace/seeded-upgrade": {"metadata": {"name": "seeded-upgrade"}},
            "poddisruptionbudget/pinned-batch-runner": {
                "spec": {"maxUnavailable": 0},
                "status": {"disruptionsAllowed": 0, "currentHealthy": 1},
            },
            "deployment/pinned-batch-runner": {
                "spec": {"template": {"spec": {"nodeSelector": {"seeded-role": "no-surge"}}}},
                "status": {"readyReplicas": 1, "replicas": 1},
            },
            "pod?app=pinned-batch-runner": _pods(_pod(restarts=0, last_reason=None)),
            "validatingwebhookconfiguration/seeded-fail-closed-gate": {
                "webhooks": [{"name": "gate.seeded.invalid", "failurePolicy": "Fail", "timeoutSeconds": 30, "clientConfig": {"service": {"name": "nonexistent-admission-gate"}}}]
            },
            "validatingwebhookconfiguration/seeded-crd-gate": {
                "webhooks": [
                    {
                        "name": "crd-gate.seeded.invalid",
                        "failurePolicy": "Fail",
                        "timeoutSeconds": 2,
                        "clientConfig": {"service": {"name": "nonexistent-crd-gate"}},
                        "objectSelector": {"matchLabels": {"managed-by": "kube-agents-seeded-fleet"}},
                        "rules": [{"apiGroups": ["autoscaling.x-k8s.io"], "resources": ["capacitybuffers"]}],
                    }
                ]
            },
            # seeded-d's zonal-skew trio, each in the state its role's `state`
            # block asserts: a scheduled pod for the scheduling case, a Bound
            # claim for the volume case, a Pending pod for the capacity case.
            # Those three signals are what say the skew's cause is present,
            # not merely that the objects were created.
            "namespace/seeded-topology": {"metadata": {"name": "seeded-topology"}},
            # The ComputeClass fixtures on slot c (defects-c.tf): three running
            # workloads, the class, and the labelled, untainted node.
            "namespace/seeded-compute-class": {"metadata": {"name": "seeded-compute-class"}},
            "deployment/app-worker-a": {"status": {"readyReplicas": 3, "replicas": 3}, "spec": {"template": {"spec": {"containers": [{"resources": {"requests": {"cpu": "10m", "memory": "8Mi"}}}]}}}},
            "deployment/app-worker-b": {"status": {"readyReplicas": 1, "replicas": 1}, "spec": {"template": {"spec": {"containers": [{"resources": {"requests": {"cpu": "10m", "memory": "8Mi"}}}]}}}},
            "deployment/batch-worker-a": {"status": {"readyReplicas": 1, "replicas": 1}, "spec": {"template": {"spec": {"nodeSelector": {"cloud.google.com/compute-class": "general-ccc"}, "tolerations": [{"key": "cloud.google.com/compute-class", "value": "general-ccc", "effect": "NoSchedule"}], "containers": [{"name": "worker", "resources": {"requests": {"cpu": "10m", "memory": "8Mi"}, "limits": {"memory": "16Mi"}}}]}}}},
            "computeclass/general-ccc": {"spec": {"priorities": [{"nodepools": ["default-pool"]}, {"machineFamily": "n4"}, {"machineFamily": "c3"}], "nodePoolAutoCreation": {"enabled": False}}},
            _RESERVED_CCC: _reserved_ccc(),
            _RESERVED_CCC_NODES: {"items": []},
            "node?cloud.google.com/compute-class=general-ccc": {"items": [{"metadata": {"name": "gke-seeded-c-default-pool-4q2m", "labels": {"cloud.google.com/compute-class": "general-ccc"}}, "spec": {}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}]},
            "deployment/zone-pinned-api": {
                "spec": {"template": {"spec": {
                    "topologySpreadConstraints": [{"whenUnsatisfiable": "ScheduleAnyway"}],
                    "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [{"matchExpressions": [{"key": "topology.kubernetes.io/zone", "operator": "In", "values": ["z-a"]}]}]
                    }}},
                }}},
                "status": {"readyReplicas": 2, "replicas": 2},
            },
            "pod?app=zone-pinned-api": _pods(_pod(restarts=0, last_reason=None)),
            "statefulset/zone-bound-store": {
                "spec": {"volumeClaimTemplates": [{"spec": {"storageClassName": "seeded-zonal-pd"}}]},
                "status": {"readyReplicas": 1},
            },
            "persistentvolumeclaim?app=zone-bound-store": {"items": [{"status": {"phase": "Bound"}}]},
            "deployment/capacity-starved-worker": {"status": {"readyReplicas": 1, "replicas": 4}},
            "deployment/first-zone-sponge": {"spec": {"replicas": 6}},
            "pod?app=capacity-starved-worker": _pods(
                _pod(restarts=0, last_reason=None), _pod(restarts=0, last_reason=None, phase="Pending")
            ),
            # seeded-a's five upgrade-failure shapes, each in its designed state:
            # the pod runs (or the claim is bound) and the planted property is
            # still there. The property is the defect; the Running pod says the
            # before-state is live rather than already broken.
            "namespace/seeded-shapes": {"metadata": {"name": "seeded-shapes"}},
            "deployment/cache-on-emptydir": {
                "spec": {"template": {"spec": {"volumes": [{"name": "queue", "emptyDir": {}}]}}},
                "status": {"readyReplicas": 1, "replicas": 1},
            },
            "pod?app=cache-on-emptydir": _pods(_pod(restarts=0, last_reason=None)),
            "deployment/arch-pinned-worker": {
                "spec": {"template": {"spec": {"nodeSelector": {"beta.kubernetes.io/arch": "amd64"}}}},
                "status": {"readyReplicas": 1, "replicas": 1},
            },
            "pod?app=arch-pinned-worker": _pods(_pod(restarts=0, last_reason=None)),
            "daemonset/node-runtime-probe": {
                "spec": {"template": {"spec": {"volumes": [{"name": "sock", "hostPath": {"path": "/run/containerd/containerd.sock", "type": "Socket"}}]}}},
                "status": {"desiredNumberScheduled": 2, "numberReady": 2},
            },
            "persistentvolumeclaim/intree-pd": {"spec": {"volumeName": "intree-pd"}, "status": {"phase": "Bound"}},
            "persistentvolume/intree-pd": {
                "spec": {"gcePersistentDisk": {"pdName": "seeded-a-intree-pd", "fsType": "ext4"}, "claimRef": {"namespace": "seeded-shapes", "name": "intree-pd"}},
                "status": {"phase": "Bound"},
            },
            "deployment/intree-pd-reader": {"status": {"readyReplicas": 1, "replicas": 1}},
            "pod?app=intree-pd-reader": _pods(_pod(restarts=0, last_reason=None)),
            "deployment/legacy-registry-pull": {
                "spec": {"template": {"spec": {"containers": [{"name": "pause", "image": "k8s.gcr.io/pause:3.9", "imagePullPolicy": "Always"}]}}},
                "status": {"readyReplicas": 1, "replicas": 1},
            },
            "pod?app=legacy-registry-pull": _pods(_pod(restarts=0, last_reason=None)),
            # the six group-5 shapes: an admission pin to the moving rule set, a stale
            # client, a pre-cgroup-v2 JVM, a multi-process container, a
            # network-plugin-shaped agent and a suspended GPU job.
            "namespace/seeded-defaults": {"metadata": {"name": "seeded-defaults", "labels": {"kubernetes.io/metadata.name": "seeded-defaults", "managed-by": "kube-agents-seeded-fleet", "pod-security.kubernetes.io/enforce": "baseline", "pod-security.kubernetes.io/enforce-version": "latest", "pod-security.kubernetes.io/warn": "baseline", "pod-security.kubernetes.io/warn-version": "latest"}}},
            "deployment/baseline-edge-worker": {"status": {"readyReplicas": 1, "replicas": 1}},
            "pod?app=baseline-edge-worker": _pods(_pod(restarts=0, last_reason=None)),
            "cronjob/stale-kubectl-client": {"spec": {"schedule": "*/10 * * * *", "suspend": False, "jobTemplate": {"spec": {"template": {"spec": {"containers": [{"name": "kubectl", "image": "registry.k8s.io/kubectl:v1.29.0"}]}}}}}},
            "job?app=stale-kubectl-client": {"items": [{"status": {"succeeded": 1, "conditions": [{"type": "Complete", "status": "True"}]}}]},
            "deployment/cgroup-blind-jvm": {
                "spec": {"template": {"spec": {"containers": [{"name": "jvm", "image": "docker.io/library/eclipse-temurin:8u302-b08-jre", "resources": {"limits": {"memory": "192Mi"}}}]}}},
                "status": {"readyReplicas": 1, "replicas": 1},
            },
            "pod?app=cgroup-blind-jvm": _pods(_pod(restarts=0, last_reason=None)),
            "deployment/multi-process-worker": {
                "spec": {"template": {"spec": {"containers": [{"name": "supervisor", "command": ["sh", "-c", "sleep infinity & sleep infinity & wait"], "resources": {"limits": {"memory": "32Mi"}}}]}}},
                "status": {"readyReplicas": 1, "replicas": 1},
            },
            "pod?app=multi-process-worker": _pods(_pod(restarts=0, last_reason=None)),
            "daemonset/cni-shaped-agent": {
                "spec": {"template": {"spec": {"hostNetwork": True, "volumes": [{"name": "cni-conf", "hostPath": {"path": "/etc/cni/net.d", "type": "Directory"}}]}}},
                "status": {"desiredNumberScheduled": 2, "numberReady": 2},
            },
            # seeded-scaled-zero: a switched-off environment, nothing running,
            # and the Gateway status no controller would otherwise write.
            "deployment/storefront": {"spec": {"replicas": 0}},
            "deployment/envoy-seeded-scaled-zero-storefront-edge": {"spec": {"replicas": 0}},
            "gateway/storefront-edge": {"status": {"conditions": [
                {"type": "Accepted", "status": "True", "reason": "Accepted"},
                {"type": "Programmed", "status": "False", "reason": "NoResources"},
            ]}},
            "pod?app=storefront": {"items": []},
            "pod?app.kubernetes.io/name=envoy": {"items": []},
            "configconnector/configconnector.core.cnrm.cloud.google.com": {"spec": {"mode": "namespaced"}, "status": {"healthy": True}},
            "cronjob/cuda-pinned-trainer": {"spec": {"schedule": "0 3 * * *", "suspend": True, "jobTemplate": {"spec": {"template": {"spec": {"nodeSelector": {"cloud.google.com/gke-accelerator": "nvidia-l4"}, "containers": [{"name": "trainer", "image": "docker.io/nvidia/cuda:12.2.0-base-ubuntu22.04"}]}}}}}},
            "deployment/state-sync-controller": {
                "spec": {"template": {"spec": {"containers": [{"name": "controller", "args": ["--kube-api-qps=-1"]}]}}},
                "status": {"readyReplicas": 1, "replicas": 1},
            },
        },
        # The project-scoped roles: Compute Engine objects no cluster carries.
        "compute": {
            "instances": [
                {"name": "startup-fail-seeded", "status": "RUNNING", "zone": "https://www.googleapis.com/compute/v1/projects/kube-agents-evals/zones/us-central1-a", "metadata": {"items": [{"key": "startup-script", "value": "exit 1"}]}},
                {"name": "world-open-seeded", "status": "RUNNING", "networkInterfaces": [{"accessConfigs": [{"type": "ONE_TO_ONE_NAT", "natIP": "203.0.113.7"}]}]},
                {"name": "serial-flood-seeded", "status": "RUNNING", "zone": "https://www.googleapis.com/compute/v1/projects/kube-agents-evals/zones/us-central1-a", "metadata": {"items": [{"key": "startup-script", "value": "exit 1"}]}},
            ],
            "firewall-rules": [
                {"name": "world-open-2379-seeded", "disabled": False, "sourceRanges": ["0.0.0.0/0"], "allowed": [{"IPProtocol": "tcp", "ports": ["2379"]}]},
                {"name": "world-open-deny-remote-seeded", "disabled": False, "sourceRanges": ["0.0.0.0/0"], "denied": [{"IPProtocol": "tcp", "ports": ["22", "3389"]}]},
            ],
            # The peered pair as `networks list` prints it: `default` on GCP's
            # 1460 default, which the listing omits, and the jumbo-frame VPC.
            "networks": [
                {"name": "default", "peerings": [{"name": "default-to-mtu-peer-declared", "network": "https://www.googleapis.com/compute/v1/projects/kube-agents-evals/global/networks/mtu-peer-declared", "state": "ACTIVE"}]},
                {"name": "mtu-peer-declared", "mtu": 8896, "peerings": [{"name": "mtu-peer-declared-to-default", "network": "https://www.googleapis.com/compute/v1/projects/kube-agents-evals/global/networks/default", "state": "ACTIVE"}]},
            ],
            # The two orphaned snapshots (main.tf, terraform_data.orphan_snapshots):
            # READY, past the compute audit's 90-day gate, taken by no schedule,
            # of a throwaway disk the planter deleted, so the disk list is empty.
            "snapshots": [_snapshot("declared-orphan-snap-seeded"), _snapshot("orphan-snap-seeded")],
            "disks": [],
        },
        # Each instance's serial port 1 output, by name. The startup-fail VM's
        # holds the boot that ran its script; the serial-flood VM's holds only
        # its flood filler, the boot already rotated out.
        "serial": {"startup-fail-seeded": _FRESH_SERIAL, "serial-flood-seeded": _FLOODED_SERIAL},
        "describe": {
            "seeded-a": _peer(),
            "seeded-b": _cluster_b(),
            "seeded-c": _cluster_c(),
            "seeded-d": _peer(),
        },
        "server_config": {
            "channels": [
                {"channel": "RAPID", "defaultVersion": "1.35.0-gke.1", "validVersions": ["1.35.0-gke.1", "1.34.2-gke.5", "1.33.5-gke.9"]},
                {"channel": "REGULAR", "defaultVersion": "1.34.1-gke.1", "validVersions": ["1.34.1-gke.1", "1.33.4-gke.1134000", "1.33.4-gke.1000000"]},
            ]
        },
    }


def _exit_code(code, stderr) -> int:
    """What a process ends with when its main returns `code` or exits with it: the
    int, 0 for None, and 1 with the text on stderr for anything else."""
    if code is None or isinstance(code, int):
        return code or 0
    print(code, file=stderr)
    return 1


def _print_error(error: BaseException, stderr) -> None:
    """The traceback a process prints for an error it does not catch: the frames
    from the called code on, without the frame of the caller here."""
    traceback.print_exception(error.with_traceback(error.__traceback__.tb_next), file=stderr)


def _run_in_process(main, argv: list[str], env: dict) -> subprocess.CompletedProcess:
    """Call a script's `main(argv)` in this process and give back what a new process
    would: its exit code, its stdout and its stderr. The call gets `env` as its whole
    environment, the script's path and `argv` in sys.argv, and its own sys.stdout
    and sys.stderr; what it returns, a SystemExit, or an error it does not catch
    becomes the exit code. The environment, sys.argv, the two streams and the
    working directory are put back after the call."""
    out, err = io.StringIO(), io.StringIO()
    cwd = os.getcwd()
    try:
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(sys, "argv", [str(_SCRIPT), *argv]),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            try:
                code = _exit_code(main(argv), err)
            except SystemExit as exit:
                code = _exit_code(exit.code, err)
            except Exception as error:
                _print_error(error, err)
                code = 1
    finally:
        os.chdir(cwd)
    return subprocess.CompletedProcess([str(_SCRIPT), *argv], code, out.getvalue(), err.getvalue())


class _Harness(unittest.TestCase):
    """A fleet directory the runner would have written, and the three stubs."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        # Each stub's code for a run in the test process, compiled from the text of
        # its program on PATH, and as python3 compiles a program: without this
        # module's `from __future__` import.
        self.stub_code = {}
        for name, body in (("kubectl", _STUB_KUBECTL), ("gcloud", _STUB_GCLOUD), ("gh", _STUB_GH)):
            stub = self.bin / name
            stub.write_text(body, encoding="utf-8")
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
            self.stub_code[name] = compile(stub.read_text(encoding="utf-8"), str(stub), "exec", dont_inherit=True)
        self.log = self.tmp / "calls.log"
        self.log.write_text("")
        self.world = self.tmp / "world.json"
        self.fleet = self.tmp / "fleet"
        self.catalog = json.loads(_CATALOG.read_text(encoding="utf-8"))
        self.provision()

    def provision(self, roles=None):
        """Lay down what hack/fleet-kubeconfigs.sh leaves: the marker, the
        context (project plus each slot's cluster and location), one
        kubeconfig per published cluster role, and the `.confirmed` list
        alone for a published project-scoped role."""
        if self.fleet.exists():
            import shutil

            shutil.rmtree(self.fleet)
        self.fleet.mkdir()
        (self.fleet / ".kube-agents-fleet-kubeconfigs").write_text("")
        lines = ["project=kube-agents-evals"]
        for slot in ("a", "b", "c", "d"):
            lines += [f"cluster.{slot}=seeded-{slot}", f"location.{slot}=us-central1-a"]
        (self.fleet / ".fleet-context").write_text("\n".join(lines) + "\n")
        for role in roles if roles is not None else self.catalog["roles"]:
            if self.catalog["roles"][role]["cluster_slot"] is None:
                (self.fleet / f"{role}.confirmed").write_text("".join(f"{probe}\n" for probe in self.catalog["roles"][role]["probes"]))
            else:
                (self.fleet / f"{role}.kubeconfig").write_text("apiVersion: v1\nkind: Config\n")

    def script_env(self) -> dict:
        """The environment of one run of the script: the stubs first on PATH, their
        log and their world."""
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "STUB_LOG": str(self.log),
            "STUB_WORLD": str(self.world),
        }
        env.pop("BENCH_FLEET_KUBECONFIG_DIR", None)
        return env

    def run_stub(self, cmd: list[str], env: dict | None = None) -> tuple[int, str, str]:
        """Run the stub for `cmd[0]` in this process as its program on PATH runs: the
        command in sys.argv, `env` as its environment when one is given, its output
        taken, its exit code given back. An error the stub does not catch ends it
        with the traceback and 1, as a process ends."""
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with (
            mock.patch.dict(os.environ, dict(os.environ) if env is None else env, clear=True),
            mock.patch.object(sys, "argv", [str(self.bin / cmd[0]), *cmd[1:]]),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            try:
                exec(self.stub_code[cmd[0]], {"__name__": "__main__"})
            except SystemExit as exit:
                code = _exit_code(exit.code, err)
            except Exception as error:
                _print_error(error, err)
                code = 1
        return code, out.getvalue(), err.getvalue()

    def run_script(self, world: dict, *args: str, catalog: pathlib.Path = _CATALOG) -> subprocess.CompletedProcess:
        """One run of the script in the test process. A new process for each run,
        and one more for each command the script sends, cost this file most of its
        time; run_script_process is the same run across the real boundary.

        The script module is loaded anew for the run. Each `subprocess.run` of
        `kubectl`, `gcloud` or `gh`, by the script or by the pool verifier, runs
        that stub's code; the timeout is not used. Any other command is run."""
        self.world.write_text(json.dumps(world))
        real_run = subprocess.run

        def run(cmd, **kwargs):
            if cmd[0] not in self.stub_code:
                return real_run(cmd, **kwargs)
            return subprocess.CompletedProcess(cmd, *self.run_stub(cmd, kwargs.get("env")))

        with mock.patch.object(subprocess, "run", run):
            return _run_in_process(_load_module().main, ["--dir", str(self.fleet), "--catalog", str(catalog), *args], self.script_env())

    def run_script_process(self, world: dict, *args: str, catalog: pathlib.Path = _CATALOG) -> subprocess.CompletedProcess:
        """One run of the script as a new process, with the stubs as programs on its
        PATH: the boundary run_script leaves out."""
        self.world.write_text(json.dumps(world))
        return subprocess.run(
            [sys.executable, str(_SCRIPT), "--dir", str(self.fleet), "--catalog", str(catalog), *args],
            capture_output=True,
            text=True,
            env=self.script_env(),
            check=False,
        )

    def drift_files(self) -> dict[str, str]:
        return {p.stem: p.read_text() for p in sorted(self.fleet.glob("*.drift"))}


class RunInTheTestProcessTest(_Harness):
    """run_script calls the script in the test process. These hold that such a run
    is kept apart from the next one, that it ends with what a process would, and
    that each stub answers there as its program on PATH answers."""

    def test_a_run_leaves_nothing_for_the_next_run(self):
        seen = []

        def main(argv):
            seen.append({name: os.environ.get(name) for name in ("OUTSIDE", "MINE", "LEFT_BY_A_RUN")} | {"cwd": os.getcwd(), "argv": list(sys.argv)})
            os.environ["LEFT_BY_A_RUN"] = "1"
            os.chdir(self.tmp)
            sys.argv.append("left")
            print("on stdout")
            print("on stderr", file=sys.stderr)
            sys.exit(3)

        with mock.patch.dict(os.environ, {"OUTSIDE": "the test's own"}):
            before = (dict(os.environ), os.getcwd(), list(sys.argv), sys.stdout, sys.stderr)
            first = _run_in_process(main, ["a"], {"MINE": "first"})
            second = _run_in_process(main, ["b"], {})
            assert (dict(os.environ), os.getcwd(), list(sys.argv), sys.stdout, sys.stderr) == before
        assert (first.returncode, first.stdout, first.stderr) == (3, "on stdout\n", "on stderr\n")
        # Each run saw its own environment alone: not the test's, not the other run's.
        assert seen[0] == {"OUTSIDE": None, "MINE": "first", "LEFT_BY_A_RUN": None, "cwd": before[1], "argv": [str(_SCRIPT), "a"]}
        assert seen[1] == {"OUTSIDE": None, "MINE": None, "LEFT_BY_A_RUN": None, "cwd": before[1], "argv": [str(_SCRIPT), "b"]}

    def test_a_run_ends_with_what_a_process_would(self):
        # Each `main` here and as the body of a program: the same exit code, and
        # the same last line on stderr.
        bodies = {
            "pass": 0,
            "return 1": 1,
            "return 'a message'": 1,
            "sys.exit()": 0,
            "sys.exit(2)": 2,
            "sys.exit('a message')": 1,
            "raise ValueError('not caught')": 1,
        }
        for body, code in bodies.items():
            with self.subTest(body=body):
                scope = {"sys": sys}
                exec(f"def main(argv):\n    {body}\n", scope)
                here = _run_in_process(scope["main"], [], {})
                program = self.tmp / "program.py"
                program.write_text(f"import sys\ndef main(argv):\n    {body}\nsys.exit(main(sys.argv[1:]))\n", encoding="utf-8")
                there = subprocess.run([sys.executable, str(program)], capture_output=True, text=True, check=False)
                assert here.returncode == there.returncode == code
                assert here.stderr.splitlines()[-1:] == there.stderr.splitlines()[-1:]

    def test_a_stub_in_the_test_process_answers_as_its_program_on_path(self):
        for name, code in self.stub_code.items():
            program = self.bin / name
            assert code == compile(program.read_text(encoding="utf-8"), str(program), "exec", dont_inherit=True), name
        # Each command of one pass, with a subject that cannot be read and a
        # repository that is not there among them, sent again to the program.
        world = _healthy_world()
        world["kubectl"]["deployment/checkout-gateway"] = "UNREACHABLE"
        world["github"] = {}
        answers = []
        run_stub = self.run_stub

        def recorded(cmd, env=None):
            answers.append((list(cmd), run_stub(cmd, env)))
            return answers[-1][1]

        self.run_stub = recorded
        self.run_script(world)
        assert {cmd[0] for cmd, _ in answers} == set(self.stub_code)
        assert any(answer[0] != 0 for _, answer in answers), "no command of the pass failed"
        # And a command that the stub does not expect, which ends it with an error.
        with mock.patch.dict(os.environ, self.script_env(), clear=True):
            recorded(["kubectl", "version"])
        assert "ValueError" in answers[-1][1][2]
        for cmd, answer in answers:
            with self.subTest(cmd=cmd):
                program = subprocess.run([str(self.bin / cmd[0]), *cmd[1:]], capture_output=True, text=True, env=self.script_env(), check=False)
                assert answer == (program.returncode, program.stdout, program.stderr)


class CatalogTest(unittest.TestCase):
    def test_the_real_catalog_loads_and_every_role_asserts_something(self):
        roles = ffs.load_assertions(_CATALOG)
        assert set(roles) == set(json.loads(_CATALOG.read_text())["roles"])
        for role, spec in roles.items():
            assert spec["state"], f"role {role!r} declares no state assertions"
            for entry in spec["state"]:
                assert entry["why"], f"role {role!r}: every assertion says what the cases lose"

    def test_path_forms(self):
        assert ffs.parse_path("status.readyReplicas") == [("key", "status"), ("key", "readyReplicas")]
        assert ffs.parse_path("status.containerStatuses[*].restartCount") == [
            ("key", "status"),
            ("key", "containerStatuses"),
            ("all",),
            ("key", "restartCount"),
        ]
        assert ffs.parse_path("status.conditions[?(@.type=='Ready')].status") == [
            ("key", "status"),
            ("key", "conditions"),
            ("filter", "type", "Ready"),
            ("key", "status"),
        ]
        assert ffs.parse_path("subjects[0].namespace") == [("key", "subjects"), ("index", 0), ("key", "namespace")]
        assert ffs.parse_path("maintenanceExclusions.hold-the-minor-lag.endTime")[1] == ("key", "hold-the-minor-lag")
        assert ffs.parse_path('metadata.labels["pod-security.kubernetes.io/enforce-version"]') == [
            ("key", "metadata"),
            ("key", "labels"),
            ("key", "pod-security.kubernetes.io/enforce-version"),
        ]
        assert ffs.parse_path('spec.nodeSelector["cloud.google.com/gke-accelerator"].x')[-2:] == [
            ("key", "cloud.google.com/gke-accelerator"),
            ("key", "x"),
        ]

    def test_malformed_paths_are_repository_bugs(self):
        for bad in ("", "a..b", "a.", "a[?(@.x=y)]", "a b", "a[*", 'a["b', 'a["b"', 'a[""]', 'a["b]'):
            with self.subTest(path=bad), self.assertRaises(ffs.CatalogError):
                ffs.parse_path(bad)

    def _catalog_with(self, tmp: pathlib.Path, entry: dict) -> pathlib.Path:
        doc = json.loads(_CATALOG.read_text())
        doc["roles"]["crashloop-workload"]["state"] = [entry]
        path = tmp / "fixtures.json"
        path.write_text(json.dumps(doc))
        return path

    def test_malformed_state_entries_are_refused(self):
        import tempfile

        cases = [
            {"subject": "pod?app=x", "path": "a", "op": "bigger_than", "value": 1},
            {"subject": "pod?app=x", "path": "a", "op": "eq"},
            {"subject": "pod?app=x", "path": "a", "op": "absent", "value": 1},
            {"subject": "pod?app=x", "op": "eq", "value": 1},
            {"subject": "pod?app=x", "path": "a", "op": "minor_behind_channel_default", "value": 1},
            {"subject": "pod; rm -rf /", "path": "a", "op": "eq", "value": 1},
            {"subject": "cluster", "path": 7, "op": "eq", "value": 1},
            # node_cpu_spare_ge reads node objects and the pods bound to them: a node selector, no path.
            {"subject": "deployment/x", "op": "node_cpu_spare_ge", "value": 100},
            {"subject": "node?cloud.google.com/gke-nodepool=default-pool", "path": "status", "op": "node_cpu_spare_ge", "value": 100},
            {"subject": "node?cloud.google.com/gke-nodepool=default-pool", "op": "node_cpu_spare_ge"},
            # age_hours_ge takes a number of hours.
            {"subject": "cluster", "path": "createTime", "op": "age_hours_ge"},
            {"subject": "cluster", "path": "createTime", "op": "age_hours_ge", "value": "a day"},
            {"subject": "cluster", "path": "createTime", "op": "age_hours_ge", "value": "nan"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for entry in cases:
                with self.subTest(entry=entry), self.assertRaises(ffs.CatalogError):
                    ffs.load_assertions(self._catalog_with(pathlib.Path(tmp), entry))


class RunCommandTest(unittest.TestCase):
    """`_run` turns a tool that is not installed, a tool that cannot be run and
    a tool that runs too long into exit statuses.

    The statuses are the shell's: 127 for a command that is not found, 126 for
    one that is there and cannot be started, 124 for one that `timeout` stopped.
    Each real process here is this interpreter, a path that does not exist or
    a file that is not executable, so no test starts `kubectl`, `gcloud` or
    `gh`.
    """

    #: Short enough to keep the test fast, and a child that sleeps far longer.
    TIMEOUT_SECONDS = 0.2
    CHILD_SLEEP_SECONDS = 30

    def test_a_tool_that_is_not_installed_is_status_127(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(pathlib.Path(tmp) / "no-such-tool")
            rc, out, err = ffs._run([missing, "get", "pods"], self.TIMEOUT_SECONDS)
        self.assertEqual((rc, out), (127, ""))
        self.assertIn(missing, err)

    def test_a_tool_that_cannot_be_run_is_status_126(self):
        with tempfile.TemporaryDirectory() as tmp:
            not_executable = pathlib.Path(tmp) / "kubectl"
            not_executable.write_text("#!/bin/sh\n")
            not_executable.chmod(0o644)
            rc, out, err = ffs._run([str(not_executable), "get", "pods"], self.TIMEOUT_SECONDS)
        self.assertEqual((rc, out), (126, ""))
        self.assertIn(str(not_executable), err)

    def test_a_tool_that_runs_past_its_timeout_is_status_124(self):
        cmd = [sys.executable, "-c", f"import time; time.sleep({self.CHILD_SLEEP_SECONDS})"]
        rc, out, err = ffs._run(cmd, self.TIMEOUT_SECONDS)
        self.assertEqual((rc, out), (124, ""))
        self.assertEqual(err, f"timed out after {self.TIMEOUT_SECONDS:g}s: {' '.join(cmd)}")

    def test_a_tool_that_answers_keeps_its_status_and_both_streams(self):
        cmd = [sys.executable, "-c", "import sys; print('to stdout'); print('to stderr', file=sys.stderr); sys.exit(3)"]
        self.assertEqual(ffs._run(cmd, 30), (3, "to stdout\n", "to stderr\n"))

    def test_a_read_that_got_such_a_status_is_unreadable_and_names_it(self):
        """A caller reports the status, so "not installed", "cannot be run" and
        "too slow" read apart in the "not checked" line, and none is taken for
        drift."""
        failures = (
            (FileNotFoundError(2, "No such file or directory", "kubectl"), 127, "No such file or directory"),
            (PermissionError(13, "Permission denied", "kubectl"), 126, "Permission denied"),
            (OSError(8, "Exec format error", "kubectl"), 126, "Exec format error"),
            (subprocess.TimeoutExpired(["kubectl"], ffs.KUBECTL_TIMEOUT_SECONDS), 124, "timed out after"),
        )
        for failure, status, text in failures:
            with self.subTest(status=status), mock.patch.object(subprocess, "run", side_effect=failure):
                with self.assertRaises(ffs.Unreadable) as caught:
                    ffs.read_kubectl(pathlib.Path("kubeconfig"), None, "deployment/x")
                self.assertIn(f"({status})", str(caught.exception))
                self.assertIn(text, str(caught.exception))


class EvaluateTest(unittest.TestCase):
    def _entry(self, op, value=None, path="v", subject="deployment/x"):
        entry = {"subject": subject, "path": path, "op": op, "value": value, "why": ""}
        entry["steps"] = ffs.parse_path(path) if path else None
        return entry

    def test_eq_wants_exactly_one_equal_value(self):
        assert ffs.evaluate(self._entry("eq", 2), [{"v": 2}]) is None
        assert ffs.evaluate(self._entry("eq", "2"), [{"v": 2}]) is None
        assert ffs.evaluate(self._entry("eq", 2), [{"v": 1}]) is not None
        assert ffs.evaluate(self._entry("eq", 2), []) is not None
        assert ffs.evaluate(self._entry("eq", 2), [{"v": 2}, {"v": 2}]) is not None

    def test_ge_and_any_ge_are_numeric(self):
        assert ffs.evaluate(self._entry("ge", 1), [{"v": 1}]) is None
        assert ffs.evaluate(self._entry("ge", 1), [{"v": 0}]) is not None
        assert ffs.evaluate(self._entry("ge", 1), [{"v": "many"}]) is not None
        assert ffs.evaluate(self._entry("any_ge", 1), [{"v": 0}, {"v": 3}]) is None
        assert ffs.evaluate(self._entry("any_ge", 1), [{"v": 0}, {"v": 0}]) is not None

    def test_any_eq_and_none_eq_quantify(self):
        assert ffs.evaluate(self._entry("any_eq", "Pending"), [{"v": "Running"}, {"v": "Pending"}]) is None
        assert ffs.evaluate(self._entry("any_eq", "Pending"), [{"v": "Running"}]) is not None
        assert ffs.evaluate(self._entry("any_eq", "Pending"), []) is not None
        assert ffs.evaluate(self._entry("none_eq", True), [{"v": False}]) is None
        assert ffs.evaluate(self._entry("none_eq", True), []) is None
        assert ffs.evaluate(self._entry("none_eq", True), [{"v": True}]) is not None

    def test_absent_counts_values_or_objects(self):
        assert ffs.evaluate(self._entry("absent", path="v"), [{"w": 1}]) is None
        assert ffs.evaluate(self._entry("absent", path="v"), [{"v": 1}]) is not None
        assert ffs.evaluate(self._entry("absent", path=None), []) is None
        assert ffs.evaluate(self._entry("absent", path=None), [{"anything": 1}]) is not None

    def test_after_now_reads_rfc3339(self):
        assert ffs.evaluate(self._entry("after_now"), [{"v": _future()}]) is None
        assert ffs.evaluate(self._entry("after_now"), [{"v": "2020-01-01T00:00:00Z"}]) is not None
        assert ffs.evaluate(self._entry("after_now"), [{"v": "not a time"}]) is not None
        assert ffs.evaluate(self._entry("after_now"), []) is not None

    def test_age_hours_ge_wants_a_timestamp_old_enough(self):
        entry = self._entry("age_hours_ge", 24, path="createTime", subject="cluster")
        assert ffs.evaluate(entry, [{"createTime": _created(25)}]) is None
        assert ffs.evaluate(entry, [{"createTime": "2026-01-01T00:00:00Z"}]) is None
        # Defensive: a timestamp without an offset is read as UTC.
        naive = (datetime.now(timezone.utc) - timedelta(hours=30)).replace(tzinfo=None).isoformat()
        assert ffs.evaluate(entry, [{"createTime": naive}]) is None
        problem = ffs.evaluate(entry, [{"createTime": _created(3)}])
        assert problem is not None and "hours old" in problem, problem
        assert "not one RFC 3339 timestamp" in ffs.evaluate(entry, [{"createTime": "yesterday"}])
        assert "not one RFC 3339 timestamp" in ffs.evaluate(entry, [{}])
        # The offset counts: 20:00-04:00 is midnight UTC.
        then = datetime.now(timezone.utc) - timedelta(hours=25)
        eastern = then.astimezone(timezone(timedelta(hours=-4))).isoformat(timespec="seconds")
        assert ffs.evaluate(entry, [{"createTime": eastern}]) is None
        assert ffs.evaluate(self._entry("age_hours_ge", 26, path="createTime", subject="cluster"), [{"createTime": eastern}]) is not None
        # A creation time ahead of now is no age at all.
        assert ffs.evaluate(entry, [{"createTime": _future(1)}]) is not None

    def test_minor_behind_compares_against_the_clusters_own_channel(self):
        entry = self._entry("minor_behind_channel_default", 1, path="currentMasterVersion", subject="cluster")
        defaults = {"REGULAR": "1.34.1-gke.1", "RAPID": "1.35.0-gke.1"}
        lookup = defaults.get
        regular = {"currentMasterVersion": "1.33.4-gke.1", "releaseChannel": {"channel": "REGULAR"}}
        assert ffs.evaluate(entry, [regular], channel_default=lookup) is None
        rapid = dict(regular, releaseChannel={"channel": "RAPID"})
        problem = ffs.evaluate(entry, [rapid], channel_default=lookup)
        assert problem is not None and "1.35.0-gke.1" in problem
        healed = dict(regular, currentMasterVersion="1.34.1-gke.1")
        assert ffs.evaluate(entry, [healed], channel_default=lookup) is not None
        unchannelled = {"currentMasterVersion": "1.33.4-gke.1"}
        problem = ffs.evaluate(entry, [unchannelled], channel_default=lookup)
        assert problem is not None and "channel unset" in problem

    def test_not_ahead_of_channel_reads_the_clusters_own_channel(self):
        entry = self._entry("not_ahead_of_channel", path="currentMasterVersion", subject="cluster")
        valid = {"REGULAR": ["1.35.8-gke.1626001", "1.34.11-gke.1209000", "1.34.11-gke.1102000"], "RAPID": ["1.34.12-gke.1011000"]}
        lookup = valid.get
        on_channel = {"currentMasterVersion": "1.34.11-gke.1209000", "releaseChannel": {"channel": "REGULAR"}}
        assert ffs.evaluate(entry, [on_channel], channel_valid=lookup) is None
        # #3025/#3269: a RAPID patch newer than every 1.34 patch REGULAR
        # carries is one minor behind the default and still not the fixture.
        ahead = dict(on_channel, currentMasterVersion="1.34.12-gke.1011000")
        problem = ffs.evaluate(entry, [ahead], channel_valid=lookup)
        assert problem == (
            'cluster currentMasterVersion not_ahead_of_channel: observed "1.34.12-gke.1011000", '
            "newer than every 1.34 patch REGULAR offers (newest 1.34.11-gke.1209000)"
        ), problem
        # A patch REGULAR has moved past is what other upgrades cases read.
        moved_past = dict(on_channel, currentMasterVersion="1.34.10-gke.1")
        assert ffs.evaluate(entry, [moved_past], channel_valid=lookup) is None
        # Numeric, not string, order: 1.34.9 is behind 1.34.11, 1.34.12 is not.
        assert ffs.evaluate(entry, [dict(on_channel, currentMasterVersion="1.34.9-gke.9")], channel_valid=lookup) is None
        # A minor the channel no longer carries at all: behind if anything it
        # offers is newer, ahead if nothing is.
        assert ffs.evaluate(entry, [dict(on_channel, currentMasterVersion="1.33.4-gke.1")], channel_valid=lookup) is None
        problem = ffs.evaluate(entry, [dict(on_channel, currentMasterVersion="1.36.0-gke.1")], channel_valid=lookup)
        assert problem is not None and problem.endswith("newer than every version REGULAR offers"), problem
        # Read against the cluster's OWN channel: the RAPID patch is on RAPID.
        assert ffs.evaluate(entry, [dict(ahead, releaseChannel={"channel": "RAPID"})], channel_valid=lookup) is None
        unchannelled = {"currentMasterVersion": "1.34.11-gke.1209000"}
        assert "release channel unset" in ffs.evaluate(entry, [unchannelled], channel_valid=lookup)
        unlisted = dict(on_channel, releaseChannel={"channel": "EXTENDED"})
        assert "get-server-config lists no EXTENDED channel" in ffs.evaluate(entry, [unlisted], channel_valid=lookup)
        assert "not a GKE version" in ffs.evaluate(entry, [dict(on_channel, currentMasterVersion="1.34")], channel_valid=lookup)
        assert ffs.evaluate(entry, [{"releaseChannel": {"channel": "REGULAR"}}], channel_valid=lookup) == (
            "cluster currentMasterVersion not_ahead_of_channel: observed nothing"
        )
        # A roster with no version the operator can order is a failed read,
        # never "ahead of every version it offers".
        with self.assertRaises(ffs.Unreadable):
            ffs.evaluate(entry, [on_channel], channel_valid={"REGULAR": ["1.34", "latest"]}.get)
        # One stray entry is enough: ordering against the rest could drop the
        # newest patch of the minor and read the master as ahead.
        with self.assertRaises(ffs.Unreadable):
            ffs.evaluate(entry, [ahead], channel_valid={"REGULAR": ["1.34.11-gke.1209000", "1.34.13"]}.get)

    def test_not_ahead_of_channel_is_cluster_only_and_takes_no_value(self):
        def load(entry):
            path = pathlib.Path(tempfile.mkdtemp()) / "fixtures.json"
            self.addCleanup(shutil.rmtree, path.parent)
            path.write_text(json.dumps({"roles": {"r": {"cluster_slot": "b", "namespace": None, "state": [entry]}}}))
            return ffs.load_assertions(path)

        assert load({"subject": "cluster", "path": "currentMasterVersion", "op": "not_ahead_of_channel"})
        for bad in (
            {"subject": "pod?app=x", "path": "v", "op": "not_ahead_of_channel"},
            {"subject": "cluster", "path": "currentMasterVersion", "op": "not_ahead_of_channel", "value": "REGULAR"},
            {"subject": "cluster", "op": "not_ahead_of_channel"},
        ):
            with self.assertRaises(ffs.CatalogError):
                load(bad)

    def test_node_cpu_spare_ge_sums_what_is_bound_to_each_node(self):
        entry = self._entry("node_cpu_spare_ge", 100, path=None, subject="node?cloud.google.com/gke-nodepool=default-pool")
        nodes = [_pool_node("n1"), _pool_node("n2", allocatable="940m")]
        pods = {
            # n1: 1930 - (270 + max(10, 300)) = 1360; the Succeeded pod holds nothing.
            "n1": [_bound_pod("n1", "270m"), _bound_pod("n1", "500m", phase="Succeeded"), _bound_pod("n1", "10m", init_cpu="300m")],
            # n2: 940 - (800 + 100) = 40, below the margin.
            "n2": [_bound_pod("n2", "800m"), _bound_pod("n2", "0.1")],
        }
        problem = ffs.evaluate(entry, nodes, pods_by_node=lambda: pods)
        assert problem is not None and '"n2": 40' in problem and '"n1": 1360' in problem, problem
        assert ffs.evaluate(entry, nodes[:1], pods_by_node=lambda: pods) is None
        # No node matched is no headroom, not a vacuous pass.
        assert ffs.evaluate(entry, [], pods_by_node=lambda: pods) is not None

    def test_node_cpu_spare_ge_counts_a_pod_the_way_the_scheduler_does(self):
        entry = self._entry("node_cpu_spare_ge", 100, path=None, subject="node?cloud.google.com/gke-nodepool=default-pool")
        node = _pool_node("n1", allocatable="600m")
        sidecar = {"spec": {"nodeName": "n1", "containers": [{"resources": {"requests": {"cpu": "100m"}}}], "initContainers": [{"restartPolicy": "Always", "resources": {"requests": {"cpu": "50m"}}}, {"resources": {"requests": {"cpu": "120m"}}}]}, "status": {"phase": "Running"}}
        # containers 100 + sidecar 50 = 150; the ordinary init peaks at 120 + the sidecar started before it = 170; so 170.
        assert ffs._pod_cpu_request(sidecar) == 170
        overhead = {"spec": {"nodeName": "n1", "overhead": {"cpu": "250m"}, "containers": [{"resources": {"requests": {"cpu": "100m"}}}]}, "status": {"phase": "Running"}}
        assert ffs._pod_cpu_request(overhead) == 350
        # 600 - (170 + 350) = 80, under the margin only because sidecar and overhead are counted.
        problem = ffs.evaluate(entry, [node], pods_by_node=lambda: {"n1": [sidecar, overhead]})
        assert problem is not None and '"n1": 80' in problem, problem

    def test_a_cpu_quantity_that_is_not_finite_counts_as_nothing_rather_than_crashing(self):
        # float() admits "inf", "Infinity", "NaN" and "1e400"; the API server never serialises them,
        # but a value that is not a quantity must not take the whole pass down with an OverflowError.
        for bad in ("inf", "Infinity", "NaN", "1e400", "-inf"):
            assert ffs._millicores(bad) == 0.0, bad
        entry = self._entry("node_cpu_spare_ge", 100, path=None, subject="node?cloud.google.com/gke-nodepool=default-pool")
        node = {"metadata": {"name": "n1"}, "status": {"allocatable": {"cpu": "inf"}}}
        assert ffs.evaluate(entry, [node], pods_by_node=lambda: {"n1": [_bound_pod("n1", "1e400")]}) is not None

    def test_a_node_still_registering_leaves_the_role_not_checked(self):
        entry = self._entry("node_cpu_spare_ge", 100, path=None, subject="node?cloud.google.com/gke-nodepool=default-pool")
        fresh = {"metadata": {"name": "surge"}, "status": {}}
        with self.assertRaises(ffs.Unreadable):
            ffs.evaluate(entry, [_pool_node("n1"), fresh], pods_by_node=lambda: {})

    def test_a_failure_says_what_was_observed(self):
        problem = ffs.evaluate(self._entry("eq", 2, path="status.readyReplicas"), [{"status": {"readyReplicas": 0}}])
        assert problem == "deployment/x status.readyReplicas eq 2: observed 0"
        problem = ffs.evaluate(self._entry("any_eq", "OOMKilled", path="status.containerStatuses[*].lastState.terminated.reason"), [{"status": {"containerStatuses": [{"lastState": {}}]}}])
        assert problem.endswith("observed nothing")

    def test_an_object_value_compares_structurally_whatever_the_key_order(self):
        """kubectl returns map keys sorted and the catalog may write them in any
        order; equal objects are the designed state either way, and `{}` is
        the empty object the emptyDir assertion names."""
        want = {"a": 1, "b": {"c": [1, 2]}}
        assert ffs.evaluate(self._entry("eq", want), [{"v": {"b": {"c": [1, 2]}, "a": 1}}]) is None
        assert ffs.evaluate(self._entry("eq", want), [{"v": {"a": 1, "b": {"c": [2, 1]}}}]) is not None
        assert ffs.evaluate(self._entry("any_eq", {}), [{"v": {}}]) is None
        assert ffs.evaluate(self._entry("any_eq", {}), [{"v": {"medium": "Memory"}}]) is not None

    def test_a_quoted_step_reads_one_key_of_a_map(self):
        """A label or selector key with dots and a slash is one step in quotes,
        so the assertion names that key and the rest of the map is nobody's
        business."""
        entry = self._entry("eq", "latest", path='metadata.labels["pod-security.kubernetes.io/enforce-version"]', subject="namespace/x")
        labels = {"pod-security.kubernetes.io/enforce-version": "latest", "owner": "someone"}
        assert ffs.evaluate(entry, [{"metadata": {"labels": labels}}]) is None
        assert ffs.evaluate(entry, [{"metadata": {"labels": {"owner": "someone"}}}]) == (
            'namespace/x metadata.labels["pod-security.kubernetes.io/enforce-version"] eq "latest": observed nothing'
        )


class PassTest(_Harness):
    # These tests run the script as a new process with the stubs as programs on
    # its PATH: the healthy pass, the pending crashloop, the unreadable subject and
    # the directory that is not the runner's (run_script_process), and
    # test_the_environment_supplies_the_defaults. They hold what a run in the test
    # process leaves out: the script started as a program, its exit code through
    # `sys.exit(main())`, zero and not zero, and the real `subprocess.run` of the
    # tools on PATH.

    def test_a_healthy_fleet_converges_on_the_first_pass(self):
        done = self.run_script_process(_healthy_world())
        assert done.returncode == 0, done.stderr
        assert "Seeded-fleet fixture state: 61 role(s) in their designed state, 0 drifted, 0 not checked, 0 not yet ready (project kube-agents-evals)" in done.stderr
        assert self.drift_files() == {}
        assert "WARNING" not in done.stderr
        # One read per distinct subject, and the channel default once.
        assert self.log.read_text().count("get-server-config") == 1

    def test_a_pending_crashloop_is_drift_that_names_the_evidence(self):
        world = _healthy_world()
        world["kubectl"]["pod?app=payments-api"] = _pods(_pod(restarts=0, last_reason=None, phase="Pending"))
        done = self.run_script_process(world)
        assert done.returncode == 0, done.stderr
        assert "1 drifted" in done.stderr
        files = self.drift_files()
        assert set(files) == {"crashloop-workload"}
        body = files["crashloop-workload"]
        assert "status.containerStatuses[*].restartCount any_ge 1: observed 0" in body
        assert "lastState.terminated.reason any_eq \"OOMKilled\": observed nothing" in body
        assert "(why:" in body
        assert "WARNING: fixture role 'crashloop-workload' is present but not in its designed state in kube-agents-evals" in done.stderr
        assert "not in its designed state in kube-agents-evals" in done.stderr

    def test_the_wait_lets_the_first_restart_arrive(self):
        world = _healthy_world()
        world["kubectl"]["pod?app=payments-api"] = [
            _pods(_pod(restarts=0, last_reason=None, phase="Pending")),
            _pods(_pod(restarts=0, last_reason=None)),
            _pods(_pod(restarts=1, last_reason="OOMKilled")),
        ]
        done = self.run_script(world, "--wait", "20", "--interval", "0.1")
        assert done.returncode == 0, done.stderr
        assert "61 role(s) in their designed state, 0 drifted" in done.stderr
        assert self.drift_files() == {}
        # Only the pending role is re-read; converged roles are not asked again.
        assert self.log.read_text().count("get deployment checkout-gateway") == 1
        assert self.log.read_text().count("get pod") >= 3

    def test_drift_is_recorded_only_at_the_deadline(self):
        world = _healthy_world()
        world["kubectl"]["pod?app=payments-api"] = _pods(_pod(restarts=0, last_reason=None, phase="Pending"))
        # In-process on a clock that moves only when the script sleeps, so the
        # passes are counted rather than raced against the wall clock (#4415):
        # with --wait 1.125 --interval 0.25 the loop reads at 0, 0.25, 0.5,
        # 0.75, 1.0 and 1.125 (the last sleep cut to what was left), six passes
        # on any machine, with no drift file written until the last.
        def nothing_recorded_yet():
            assert self.drift_files() == {}, "drift recorded before the deadline"

        clock = _SteppedClock(nothing_recorded_yet)
        self.world.write_text(json.dumps(world))
        env = {"PATH": f"{self.bin}:{os.environ['PATH']}", "STUB_LOG": str(self.log), "STUB_WORLD": str(self.world)}
        stderr = io.StringIO()
        with mock.patch.object(ffs, "time", clock), mock.patch.dict(os.environ, env), contextlib.redirect_stderr(stderr):
            assert ffs.run(self.fleet, _CATALOG, None, wait=1.125, interval=0.25) == 0, stderr.getvalue()
        assert clock.sleeps == [0.25] * 4 + [0.125], stderr.getvalue()
        assert set(self.drift_files()) == {"crashloop-workload"}, stderr.getvalue()
        assert self.log.read_text().count("get pod -l app=payments-api") == 6

    def test_an_unreadable_subject_is_not_checked_and_leaves_no_drift_file(self):
        world = _healthy_world()
        world["kubectl"]["deployment/checkout-gateway"] = "UNREACHABLE"
        done = self.run_script_process(world)
        assert done.returncode == 0, done.stderr
        assert "60 role(s) in their designed state, 0 drifted, 1 not checked" in done.stderr
        assert self.drift_files() == {}
        assert "WARNING: fixture role 'no-pdb-workload' could not be checked" in done.stderr
        assert "Unable to connect" in done.stderr

    def test_an_unreadable_role_does_not_hold_the_wait(self):
        import time

        world = _healthy_world()
        world["kubectl"]["deployment/checkout-gateway"] = "UNREACHABLE"
        started = time.monotonic()
        done = self.run_script(world, "--wait", "30", "--interval", "0.1")
        assert done.returncode == 0, done.stderr
        assert time.monotonic() - started < 20, "nothing was worth waiting for"
        assert "1 not checked" in done.stderr
        assert self.log.read_text().count("get deployment checkout-gateway") == 1

    def test_an_unreadable_role_is_re_read_while_another_is_worth_waiting_for(self):
        world = _healthy_world()
        world["kubectl"]["deployment/checkout-gateway"] = ["UNREACHABLE", world["kubectl"]["deployment/checkout-gateway"]]
        world["kubectl"]["pod?app=payments-api"] = [
            _pods(_pod(restarts=0, last_reason=None, phase="Pending")),
            _pods(_pod(restarts=1, last_reason="OOMKilled")),
        ]
        done = self.run_script(world, "--wait", "20", "--interval", "0.1")
        assert done.returncode == 0, done.stderr
        assert "61 role(s) in their designed state, 0 drifted, 0 not checked" in done.stderr

    def test_a_read_failure_beside_a_failed_assertion_is_still_drift(self):
        world = _healthy_world()
        world["kubectl"]["deployment/checkout-gateway"] = "UNREACHABLE"
        world["kubectl"]["poddisruptionbudget?"] = {"items": [{"metadata": {"name": "checkout-gateway"}}]}
        done = self.run_script(world)
        # The stub answers the budget list for every namespace, so the planted
        # budget drifts declared-no-pdb-workload beside no-pdb-workload.
        assert "2 drifted, 0 not checked" in done.stderr
        body = self.drift_files()["no-pdb-workload"]
        assert "poddisruptionbudget? absent" in body
        assert "unread: deployment/checkout-gateway" in body

    def test_a_named_object_that_is_gone_is_drift_not_weather(self):
        world = _healthy_world()
        del world["kubectl"]["clusterrolebinding/debug-binding"]
        done = self.run_script(world)
        assert "1 drifted" in done.stderr
        assert "roleRef.name eq \"cluster-admin\": observed nothing" in self.drift_files()["rbac-overgrant"]

    def test_the_idle_pool_needs_a_ready_tainted_node(self):
        world = _healthy_world()
        done = self.run_script(world)
        assert "61 role(s) in their designed state" in done.stderr, done.stderr
        # The label key carries a slash: the subject is a selector, not kind/name.
        assert "get node -l cloud.google.com/gke-nodepool=idle-batch-pool" in self.log.read_text()
        world["kubectl"]["node?cloud.google.com/gke-nodepool=idle-batch-pool"] = {"items": [_node(ready="False")]}
        done = self.run_script(world)
        assert "idle-nodepool" in self.drift_files()
        world["kubectl"]["node?cloud.google.com/gke-nodepool=idle-batch-pool"] = {"items": [_node(taint=None)]}
        self.provision()
        done = self.run_script(world)
        assert "spec.taints[?(@.key=='seeded-role')].value any_eq \"idle-batch\": observed nothing" in self.drift_files()["idle-nodepool"], done.stderr

    def test_the_default_pool_needs_room_for_the_next_fixture(self):
        world = _healthy_world()
        done = self.run_script(world)
        assert "61 role(s) in their designed state" in done.stderr, done.stderr
        # The pods are read once, across every namespace, not once per node.
        assert self.log.read_text().count("get pod -A") == 1, self.log.read_text()
        # 2026-10-20's shape on kube-agents-evals-11: 940m nodes, the busier one 4m from full.
        world["kubectl"]["node?cloud.google.com/gke-nodepool=default-pool"] = {"items": [_pool_node("gke-seeded-a-default-pool-0r0x", allocatable="940m"), _pool_node("gke-seeded-a-default-pool-z7br", allocatable="940m")]}
        world["kubectl"]["pod?"] = _pods(_bound_pod("gke-seeded-a-default-pool-0r0x", "837m"), _bound_pod("gke-seeded-a-default-pool-z7br", "936m"))
        self.provision()
        done = self.run_script(world)
        drift = self.drift_files()["default-pool-headroom"]
        assert 'node_cpu_spare_ge 100: observed {"gke-seeded-a-default-pool-0r0x": 103, "gke-seeded-a-default-pool-z7br": 4}' in drift, drift
        assert "main.tf" in drift, drift

    def test_a_pod_list_that_cannot_be_read_leaves_the_headroom_role_not_checked(self):
        world = _healthy_world()
        world["kubectl"]["pod?"] = "UNREACHABLE"
        done = self.run_script(world)
        assert "60 role(s) in their designed state, 0 drifted, 1 not checked" in done.stderr, done.stderr
        assert "default-pool-headroom" in done.stderr and "kubectl get pod -A failed" in done.stderr, done.stderr
        assert "default-pool-headroom" not in self.drift_files()

    def test_the_capacity_fixture_needs_a_ready_pod_and_a_pending_surplus(self):
        world = _healthy_world()
        world["kubectl"]["pod?app=inference-server"] = _pods(_pod(restarts=0, last_reason=None))
        done = self.run_script(world)
        assert "status.phase any_eq \"Pending\": observed \"Running\"" in self.drift_files()["hpa-saturated"], done.stderr

    def test_a_retained_failed_writer_job_is_drift(self):
        world = _healthy_world()
        done = self.run_script(world)
        assert "61 role(s) in their designed state" in done.stderr, done.stderr
        # The Jobs are read by label, in the role's namespace; nothing named.
        assert "get job -l app=legacy-endpoints-writer -n seeded-deprecation" in self.log.read_text()
        # failedJobsHistoryLimit 1: one retained failure is what a broken caller leaves.
        world["kubectl"]["job?app=legacy-endpoints-writer"] = {"items": [_writer_job(failed=True, counter=1)]}
        done = self.run_script(world)
        assert "1 drifted" in done.stderr, done.stderr
        body = self.drift_files()["deprecated-api-caller"]
        assert "job?app=legacy-endpoints-writer status.conditions[?(@.type=='Failed')].status none_eq \"True\"" in body, body
        assert "spec.schedule" not in body
        # A Job whose pod was never admitted (ServiceAccount gone, quota, admission) fails
        # with the pod counter absent; a Job whose pod was replaced mid-run fails with it
        # at 2. The condition catches both shapes a counter assertion would miss.
        for shape in (_writer_job(failed=True), _writer_job(failed=True, counter=2)):
            world["kubectl"]["job?app=legacy-endpoints-writer"] = {"items": [shape]}
            self.provision()
            done = self.run_script(world)
            assert "1 drifted" in done.stderr, done.stderr
            assert "status.conditions[?(@.type=='Failed')].status none_eq \"True\"" in self.drift_files()["deprecated-api-caller"]
        # A suspended CronJob is the other silent shape.
        world["kubectl"]["job?app=legacy-endpoints-writer"] = {"items": [_writer_job()]}
        world["kubectl"]["cronjob/legacy-endpoints-writer"] = _writer_cronjob(suspend=True)
        self.provision()
        self.run_script(world)
        assert "spec.suspend none_eq true: observed true" in self.drift_files()["deprecated-api-caller"]

    def test_each_seeded_a_shape_is_drift_once_its_defect_is_healed(self):
        """The five shape roles assert on the planted property, so the heal that
        keeps the pod Running (a claim for the emptyDir, the GA label key, a
        registry that still exists, a CSI-provisioned volume) reads as drift."""
        world = _healthy_world()
        done = self.run_script(world)
        assert "61 role(s) in their designed state" in done.stderr, done.stderr
        heals = {
            "node-local-state": (
                "deployment/cache-on-emptydir",
                {"spec": {"template": {"spec": {"volumes": [{"name": "queue", "persistentVolumeClaim": {"claimName": "queue"}}]}}}, "status": {"readyReplicas": 1, "replicas": 1}},
                "spec.template.spec.volumes[?(@.name=='queue')].emptyDir any_eq {}: observed nothing",
            ),
            "deprecated-label-selector": (
                "deployment/arch-pinned-worker",
                {"spec": {"template": {"spec": {"nodeSelector": {"kubernetes.io/arch": "amd64"}}}}, "status": {"readyReplicas": 1, "replicas": 1}},
                'spec.template.spec.nodeSelector["beta.kubernetes.io/arch"] eq "amd64": observed nothing',
            ),
            "containerd-socket-agent": (
                "daemonset/node-runtime-probe",
                {"spec": {"template": {"spec": {"volumes": [{"name": "sock", "emptyDir": {}}]}}}, "status": {"desiredNumberScheduled": 2, "numberReady": 2}},
                "spec.template.spec.volumes[?(@.name=='sock')].hostPath.path any_eq \"/run/containerd/containerd.sock\": observed nothing",
            ),
            "intree-pd-volume": (
                "persistentvolume/intree-pd",
                {"spec": {"csi": {"driver": "pd.csi.storage.gke.io", "volumeHandle": "projects/p/zones/z/disks/seeded-a-intree-pd"}}, "status": {"phase": "Bound"}},
                "persistentvolume/intree-pd spec.csi absent: observed",
            ),
            "moving-admission-default": (
                "namespace/seeded-defaults",
                {"metadata": {"name": "seeded-defaults", "labels": {"kubernetes.io/metadata.name": "seeded-defaults", "managed-by": "kube-agents-seeded-fleet", "pod-security.kubernetes.io/enforce": "baseline", "pod-security.kubernetes.io/enforce-version": "v1.34", "pod-security.kubernetes.io/warn": "baseline", "pod-security.kubernetes.io/warn-version": "v1.34"}}},
                'metadata.labels["pod-security.kubernetes.io/enforce-version"] eq "latest": observed "v1.34"',
            ),
            "stale-client-skew": (
                "cronjob/stale-kubectl-client",
                {"spec": {"schedule": "*/10 * * * *", "suspend": False, "jobTemplate": {"spec": {"template": {"spec": {"containers": [{"name": "kubectl", "image": "registry.k8s.io/kubectl:v1.33.0"}]}}}}}},
                'spec.jobTemplate.spec.template.spec.containers[*].image any_eq "registry.k8s.io/kubectl:v1.29.0": observed "registry.k8s.io/kubectl:v1.33.0"',
            ),
            "cgroup-blind-runtime": (
                "deployment/cgroup-blind-jvm",
                {"spec": {"template": {"spec": {"containers": [{"name": "jvm", "image": "docker.io/library/eclipse-temurin:8u442-b06-jre", "resources": {"limits": {"memory": "192Mi"}}}]}}}, "status": {"readyReplicas": 1, "replicas": 1}},
                'image any_eq "docker.io/library/eclipse-temurin:8u302-b08-jre": observed "docker.io/library/eclipse-temurin:8u442-b06-jre"',
            ),
            "multi-process-container": (
                "deployment/multi-process-worker",
                {"spec": {"template": {"spec": {"containers": [{"name": "supervisor", "command": ["sh", "-c", "exec sleep infinity"], "resources": {"limits": {"memory": "32Mi"}}}]}}}, "status": {"readyReplicas": 1, "replicas": 1}},
                'spec.template.spec.containers[*].command[2] any_eq "sleep infinity & sleep infinity & wait": observed "exec sleep infinity"',
            ),
            "node-image-coupled-agent": (
                "daemonset/cni-shaped-agent",
                {"spec": {"template": {"spec": {"hostNetwork": False, "volumes": [{"name": "cni-conf", "hostPath": {"path": "/etc/cni/net.d", "type": "Directory"}}]}}}, "status": {"desiredNumberScheduled": 2, "numberReady": 2}},
                "spec.template.spec.hostNetwork eq true: observed false",
            ),
            "cuda-pinned-gpu-job": (
                "cronjob/cuda-pinned-trainer",
                {"spec": {"schedule": "0 3 * * *", "suspend": False, "jobTemplate": {"spec": {"template": {"spec": {"nodeSelector": {"cloud.google.com/gke-accelerator": "nvidia-l4"}, "containers": [{"name": "trainer", "image": "docker.io/nvidia/cuda:12.2.0-base-ubuntu22.04"}]}}}}}},
                "spec.suspend eq true: observed false",
            ),
            "retired-registry-image": (
                "deployment/legacy-registry-pull",
                {"spec": {"template": {"spec": {"containers": [{"name": "pause", "image": "registry.k8s.io/pause:3.9", "imagePullPolicy": "Always"}]}}}, "status": {"readyReplicas": 1, "replicas": 1}},
                'spec.template.spec.containers[*].image any_eq "k8s.gcr.io/pause:3.9": observed "registry.k8s.io/pause:3.9"',
            ),
            "state-sync-controller": (
                "deployment/state-sync-controller",
                {"spec": {"template": {"spec": {"containers": [{"name": "controller", "args": ["--kube-api-qps=0"]}]}}}, "status": {"readyReplicas": 1, "replicas": 1}},
                'spec.template.spec.containers[*].args[*] any_eq "--kube-api-qps=-1": observed "--kube-api-qps=0"',
            ),
        }
        for role, (subject, healed, line) in heals.items():
            world = _healthy_world()
            world["kubectl"][subject] = healed
            self.provision()
            done = self.run_script(world)
            assert "1 drifted" in done.stderr, (role, done.stderr)
            files = self.drift_files()
            assert set(files) == {role}, (role, set(files))
            assert line in files[role], (role, files[role])

    def test_an_ephemeral_storage_limit_or_request_on_the_planted_container_is_drift(self):
        """The no-ephemeral-limit role reads the planted container by name, so
        the write its case forbids, a limit or a request for ephemeral storage
        on `worker`, reads as drift. A filter that matched no container would
        pass on any world."""
        for field in ("limits", "requests"):
            world = _healthy_world()
            container = world["kubectl"]["deployment/batch-worker-a"]["spec"]["template"]["spec"]["containers"][0]
            container["resources"][field]["ephemeral-storage"] = "1Gi"
            self.provision()
            done = self.run_script(world)
            assert "1 drifted" in done.stderr, (field, done.stderr)
            files = self.drift_files()
            assert set(files) == {"no-ephemeral-limit-workload"}, (field, set(files))
            assert f"resources.{field}.ephemeral-storage absent: observed" in files["no-ephemeral-limit-workload"], (field, files)

    def test_a_pending_shape_pod_is_the_after_state_not_the_fixture(self):
        """A pod that cannot schedule is not ready, and the Deployment says so:
        readyReplicas is omitted at zero and the missing pod counts as
        unavailable."""
        world = _healthy_world()
        world["kubectl"]["pod?app=arch-pinned-worker"] = _pods(_pod(restarts=0, last_reason=None, phase="Pending"))
        world["kubectl"]["deployment/arch-pinned-worker"]["status"] = {"replicas": 1, "unavailableReplicas": 1}
        done = self.run_script(world)
        assert set(self.drift_files()) == {"deprecated-label-selector"}, done.stderr
        assert "deployment/arch-pinned-worker status.readyReplicas eq 1: observed nothing" in self.drift_files()["deprecated-label-selector"]
        assert "deployment/arch-pinned-worker status.unavailableReplicas absent: observed 1" in self.drift_files()["deprecated-label-selector"]

    def test_a_shape_pod_restarted_into_backoff_is_drift_though_its_phase_is_running(self):
        """Under restartPolicy Always a container that ran once and now waits in
        ImagePullBackOff or CrashLoopBackOff leaves the pod's phase Running;
        the Deployment's readiness count is what tells the before-state from
        that after-state."""
        world = _healthy_world()
        world["kubectl"]["pod?app=legacy-registry-pull"] = _pods(_pod(restarts=1, last_reason=None, waiting_reason="ImagePullBackOff"))
        world["kubectl"]["deployment/legacy-registry-pull"]["status"] = {"replicas": 1, "unavailableReplicas": 1}
        done = self.run_script(world)
        assert set(self.drift_files()) == {"retired-registry-image"}, done.stderr
        assert "deployment/legacy-registry-pull status.readyReplicas eq 1: observed nothing" in self.drift_files()["retired-registry-image"]

    def test_each_shape_deployments_readiness_is_asserted_under_its_own_role(self):
        """`load_assertions` checks a subject's shape, not which role's list it
        sits in, so a readiness assertion filed under another role loads and
        runs silently and the wrong role's `.drift` file names the workload.
        Each shape Deployment left with no ready pod must drift its own role
        and no other."""
        shapes = {
            "node-local-state": "cache-on-emptydir",
            "deprecated-label-selector": "arch-pinned-worker",
            "intree-pd-volume": "intree-pd-reader",
            "retired-registry-image": "legacy-registry-pull",
            "moving-admission-default": "baseline-edge-worker",
            "cgroup-blind-runtime": "cgroup-blind-jvm",
            "multi-process-container": "multi-process-worker",
            "state-sync-controller": "state-sync-controller",
        }
        for role, app in shapes.items():
            with self.subTest(role=role):
                world = _healthy_world()
                world["kubectl"][f"pod?app={app}"] = _pods(_pod(restarts=1, last_reason=None, waiting_reason="CrashLoopBackOff"))
                world["kubectl"][f"deployment/{app}"]["status"] = {"replicas": 1, "unavailableReplicas": 1}
                self.provision()
                done = self.run_script(world)
                assert set(self.drift_files()) == {role}, (role, done.stderr)
                assert f"deployment/{app} status.readyReplicas eq 1: observed nothing" in self.drift_files()[role], role

    def test_a_wedged_rolling_update_is_drift_though_its_old_pod_stays_ready(self):
        """A template edit on a one-replica RollingUpdate Deployment surges the
        new pod before the old one goes. When the new pod cannot start (the
        image no longer pulls, the volume cannot attach) the old pod stays
        Running and ready, so a pod list quantified with any_eq passes on it;
        the Deployment counts the waiting pod as unavailable, and that is what
        the roles read."""
        shapes = {
            "node-local-state": "cache-on-emptydir",
            "deprecated-label-selector": "arch-pinned-worker",
            "intree-pd-volume": "intree-pd-reader",
            "retired-registry-image": "legacy-registry-pull",
            "moving-admission-default": "baseline-edge-worker",
            "cgroup-blind-runtime": "cgroup-blind-jvm",
            "multi-process-container": "multi-process-worker",
            "state-sync-controller": "state-sync-controller",
        }
        for role, app in shapes.items():
            with self.subTest(role=role):
                world = _healthy_world()
                world["kubectl"][f"pod?app={app}"] = _pods(
                    _pod(restarts=0, last_reason=None),
                    _pod(restarts=0, last_reason=None, waiting_reason="ImagePullBackOff"),
                )
                world["kubectl"][f"deployment/{app}"]["status"] = {
                    "replicas": 2,
                    "updatedReplicas": 1,
                    "readyReplicas": 1,
                    "availableReplicas": 1,
                    "unavailableReplicas": 1,
                }
                self.provision()
                done = self.run_script(world)
                assert set(self.drift_files()) == {role}, (role, done.stderr)
                assert f"deployment/{app} status.unavailableReplicas absent: observed 1" in self.drift_files()[role], role
                assert "status.readyReplicas eq 1" not in self.drift_files()[role], role

    def test_a_halted_daemon_set_rollout_is_drift_though_one_node_stays_ready(self):
        """A DaemonSet rolling update replaces one node's pod at a time; when the
        new pod cannot start there, the other node's old pod keeps numberReady
        above zero and the halted node is counted unavailable."""
        for role, name in (("containerd-socket-agent", "node-runtime-probe"), ("node-image-coupled-agent", "cni-shaped-agent")):
            with self.subTest(role=role):
                world = _healthy_world()
                world["kubectl"][f"daemonset/{name}"]["status"] = {
                    "desiredNumberScheduled": 2,
                    "numberReady": 1,
                    "updatedNumberScheduled": 1,
                    "numberUnavailable": 1,
                }
                self.provision()
                done = self.run_script(world)
                assert set(self.drift_files()) == {role}, (role, done.stderr)
                assert f"daemonset/{name} status.numberUnavailable absent: observed 1" in self.drift_files()[role], role

    def test_an_oom_killed_cgroup_shape_is_drift_though_its_pod_runs_again(self):
        """An OOM-killed container restarts and is ready again between kills;
        the last termination is where the after-state shows."""
        for role, app in (("cgroup-blind-runtime", "cgroup-blind-jvm"), ("multi-process-container", "multi-process-worker")):
            world = _healthy_world()
            world["kubectl"][f"pod?app={app}"] = _pods(_pod(restarts=2, last_reason="OOMKilled"))
            self.provision()
            done = self.run_script(world)
            assert set(self.drift_files()) == {role}, (role, done.stderr)
            assert 'status.containerStatuses[*].lastState.terminated.reason none_eq "OOMKilled": observed "OOMKilled"' in self.drift_files()[role], role

    def test_an_extra_label_or_selector_key_is_not_drift(self):
        """The assertions name the Pod Security keys and the one selector key,
        not the whole map, so a label an operator adds, a later fleet_labels
        key, or a second selector key changes nothing."""
        world = _healthy_world()
        world["kubectl"]["namespace/seeded-defaults"]["metadata"]["labels"]["owner"] = "someone"
        world["kubectl"]["deployment/arch-pinned-worker"]["spec"]["template"]["spec"]["nodeSelector"]["cloud.google.com/gke-nodepool"] = "default-pool"
        done = self.run_script(world)
        assert done.returncode == 0, done.stderr
        assert "61 role(s) in their designed state, 0 drifted" in done.stderr
        assert self.drift_files() == {}

    def test_the_cluster_subject_reads_the_recorded_cluster(self):
        world = _healthy_world()
        world["describe"]["seeded-c"] = {"masterAuthorizedNetworksConfig": {"enabled": True, "cidrBlocks": []}}
        done = self.run_script(world)
        assert "drift-outlier" in self.drift_files(), done.stderr
        assert "masterAuthorizedNetworksConfig.enabled none_eq true: observed true" in self.drift_files()["drift-outlier"]
        assert "clusters describe seeded-c --location us-central1-a --project kube-agents-evals" in self.log.read_text()

    def test_the_drift_peers_assert_the_same_thing_on_each_slot(self):
        roles = ffs.load_assertions(_CATALOG)
        peers = {slot: roles[f"drift-peer-{slot}"] for slot in ("a", "b", "d")}
        assert {slot: spec["cluster_slot"] for slot, spec in peers.items()} == {"a": "a", "b": "b", "d": "d"}
        shapes = {slot: [(e["subject"], e["path"], e["op"], e["value"]) for e in spec["state"]] for slot, spec in peers.items()}
        assert shapes["a"] == shapes["b"] == shapes["d"], shapes

    def test_the_drift_peers_read_their_own_slots_cluster(self):
        done = self.run_script(_healthy_world())
        assert self.drift_files() == {}, done.stderr
        log = self.log.read_text()
        for slot in ("a", "d"):
            assert f"clusters describe seeded-{slot} --location us-central1-a --project kube-agents-evals" in log

    def test_a_peer_without_its_authorized_networks_block_is_drift(self):
        # The vote that makes seeded-c the outlier is 3/4; one peer OFF makes
        # it 2/4, under the SOP's 2/3, and the case has no finding to grade.
        for block, observed in (
            ({"enabled": False}, "masterAuthorizedNetworksConfig.enabled eq true: observed false"),
            (None, "masterAuthorizedNetworksConfig.enabled eq true: observed nothing"),
            ({"enabled": True, "cidrBlocks": []}, "cidrBlocks[*].displayName any_eq \"open-for-eval\": observed nothing"),
        ):
            with self.subTest(block=block):
                world = _healthy_world()
                world["describe"]["seeded-a"] = _peer(block=block)
                done = self.run_script(world)
                files = self.drift_files()
                assert set(files) == {"drift-peer-a"}, (files, done.stderr)
                assert observed in files["drift-peer-a"], files["drift-peer-a"]
                assert "1 drifted" in done.stderr

    def test_an_outlier_too_young_for_the_drift_cohort_is_drift(self):
        # A seeded-c under a day old sits out its own audit: no finding.
        # A new peer is not drift, since seeded-c stays outvoted 2/3.
        world = _healthy_world()
        world["describe"]["seeded-c"]["createTime"] = _created(2)
        world["describe"]["seeded-d"] = _peer(created=_created(5))
        done = self.run_script(world)
        files = self.drift_files()
        assert set(files) == {"drift-outlier"}, (files, done.stderr)
        assert "cluster createTime age_hours_ge 24: observed" in files["drift-outlier"], files["drift-outlier"]
        assert "2.0 hours old" in files["drift-outlier"]

    def test_a_default_pool_on_the_smaller_machine_is_drift(self):
        # An e2-medium node keeps about 60m of CPU unrequested, and a node
        # replacement can leave the slot's workloads Pending on it; no drift
        # facet reads machine type, so the pool's own role says it.
        world = _healthy_world()
        world["describe"]["seeded-c"] = _cluster_c(machine="e2-medium")
        done = self.run_script(world)
        files = self.drift_files()
        assert set(files) == {"untainted-compute-class-pool"}, (files, done.stderr)
        assert "nodePools[?(@.name=='default-pool')].config.machineType eq \"e2-standard-2\": observed \"e2-medium\"" in files["untainted-compute-class-pool"], files
        assert "1 drifted" in done.stderr, done.stderr

    def test_a_default_pool_matching_its_peers_is_drift(self):
        # The pool-level outlier is a separate role from drift-outlier: a
        # default pool back at the peers' 20 GB, or with the kubelet key
        # unset or at another value, drifts drift-pool-outlier alone, so the
        # case that lists drift-outlier is excused nothing by it.
        for doc, observed in (
            (_cluster_c(disk=20), "nodePools[?(@.name=='default-pool')].config.diskSizeGb eq 50: observed 20"),
            (_cluster_c(pids_limit=None), "nodePools[?(@.name=='default-pool')].config.kubeletConfig.podPidsLimit eq 4096: observed nothing"),
            (_cluster_c(pids_limit="1024"), "nodePools[?(@.name=='default-pool')].config.kubeletConfig.podPidsLimit eq 4096: observed \"1024\""),
        ):
            with self.subTest(observed=observed):
                world = _healthy_world()
                world["describe"]["seeded-c"] = doc
                done = self.run_script(world)
                files = self.drift_files()
                assert set(files) == {"drift-pool-outlier"}, (files, done.stderr)
                assert observed in files["drift-pool-outlier"], files["drift-pool-outlier"]
                assert "1 drifted" in done.stderr, done.stderr

    def test_an_age_failure_does_not_hold_the_wait(self):
        # Only the clock clears it, so a five-minute wait would be spent for
        # nothing: one pass, then drift.
        world = _healthy_world()
        world["describe"]["seeded-c"]["createTime"] = _created(2)
        started = datetime.now(timezone.utc)
        done = self.run_script(world, "--wait", "60", "--interval", "1")
        assert (datetime.now(timezone.utc) - started).total_seconds() < 30, done.stderr
        # One pass: every role on slot c reads its document once per pass
        # (the read cache is per role), so one describe per slot-c role.
        slot_c_roles = sum(
            1
            for spec in self.catalog["roles"].values()
            if spec["cluster_slot"] == "c" and any(entry["subject"] == "cluster" for entry in spec["state"])
        )
        assert slot_c_roles >= 2, slot_c_roles
        assert self.log.read_text().count("clusters describe seeded-c") == slot_c_roles
        assert set(self.drift_files()) == {"drift-outlier"}

    def test_a_block_lost_beside_a_young_outlier_still_holds_the_wait(self):
        world = _healthy_world()
        world["describe"]["seeded-c"]["createTime"] = _created(2)
        # Every slot-a role with a `cluster` subject reads the document once
        # on the first pass (the read cache is per role), so the lost block is
        # served to each of them; the second pass re-reads the one role that
        # drifted, drift-peer-a, and gets the restored block.
        slot_a_cluster_roles = sum(
            1
            for spec in self.catalog["roles"].values()
            if spec["cluster_slot"] == "a" and any(entry["subject"] == "cluster" for entry in spec["state"])
        )
        assert slot_a_cluster_roles >= 2, slot_a_cluster_roles
        world["describe"]["seeded-a"] = [_peer(block={"enabled": False})] * slot_a_cluster_roles + [_peer()]
        done = self.run_script(world, "--wait", "60", "--interval", "1")
        assert set(self.drift_files()) == {"drift-outlier"}, done.stderr
        assert self.log.read_text().count("clusters describe seeded-a") == slot_a_cluster_roles + 1

    def test_an_unreadable_peer_is_not_checked(self):
        world = _healthy_world()
        world["describe"]["seeded-d"] = "UNREACHABLE"
        done = self.run_script(world)
        assert "0 drifted, 1 not checked" in done.stderr, done.stderr
        assert "fixture role 'drift-peer-d' could not be checked" in done.stderr
        assert self.drift_files() == {}

    def test_the_laggard_is_drift_once_the_master_catches_up(self):
        world = _healthy_world()
        world["describe"]["seeded-b"] = _cluster_b(master="1.34.1-gke.1")
        done = self.run_script(world)
        body = self.drift_files()["version-laggard"]
        assert "currentMasterVersion minor_behind_channel_default 1: observed \"1.34.1-gke.1\" against channel default 1.34.1-gke.1" in body, done.stderr

    def test_the_laggard_on_a_patch_ahead_of_its_channel_is_drift(self):
        # #3025/#3269: one minor behind REGULAR's default, so the minor check
        # passes, on a patch newer than any REGULAR carries for that minor.
        world = _healthy_world()
        world["describe"]["seeded-b"] = _cluster_b(master="1.33.5-gke.9")
        done = self.run_script(world)
        assert "1 drifted" in done.stderr, done.stderr
        body = self.drift_files()["version-laggard"]
        assert (
            'cluster currentMasterVersion not_ahead_of_channel: observed "1.33.5-gke.9", '
            "newer than every 1.33 patch REGULAR offers (newest 1.33.4-gke.1134000)"
        ) in body, body
        assert "minor_behind_channel_default" not in body, body
        # One server-config read serves both channel assertions.
        assert self.log.read_text().count("get-server-config") == 1

    def test_the_laggard_on_a_patch_its_channel_moved_past_is_not_drift(self):
        world = _healthy_world()
        world["describe"]["seeded-b"] = _cluster_b(master="1.33.3-gke.1")
        done = self.run_script(world)
        assert "0 drifted" in done.stderr, done.stderr
        assert self.drift_files() == {}

    def test_a_failed_server_config_read_is_made_once_per_pass(self):
        world = _healthy_world()
        world["server_config"] = "UNREACHABLE"
        done = self.run_script(world)
        assert "version-laggard" not in self.drift_files(), done.stderr
        assert "1 not checked" in done.stderr, done.stderr
        assert self.log.read_text().count("get-server-config") == 1, self.log.read_text()

    def test_a_channel_entry_without_valid_versions_leaves_the_laggard_not_checked(self):
        for roster in (None, []):
            with self.subTest(roster=roster):
                world = _healthy_world()
                for channel in world["server_config"]["channels"]:
                    if roster is None:
                        channel.pop("validVersions")
                    else:
                        channel["validVersions"] = roster
                done = self.run_script(world)
                assert "0 drifted, 1 not checked" in done.stderr, done.stderr
                assert "carries no validVersions list" in done.stderr, done.stderr
                assert self.drift_files() == {}

    def test_a_lapsed_exclusion_is_drift_before_the_master_moves(self):
        world = _healthy_world()
        world["describe"]["seeded-b"] = _cluster_b(end="2026-01-01T00:00:00Z")
        self.run_script(world)
        body = self.drift_files()["version-laggard"]
        assert "endTime after_now: observed \"2026-01-01T00:00:00Z\"" in body
        assert "currentMasterVersion" not in body

    def test_a_no_surge_pool_given_surge_is_drift(self):
        # maxUnavailable still reads 1 and every probe still passes, so only
        # the maxSurge `absent` assertion can see that the fixture is healed.
        world = _healthy_world()
        world["describe"]["seeded-b"] = _cluster_b(surge=1)
        done = self.run_script(world)
        assert "1 drifted" in done.stderr, done.stderr
        files = self.drift_files()
        assert set(files) == {"readiness-surge-blocked"}, files
        body = files["readiness-surge-blocked"]
        assert "cluster nodePools[?(@.name=='no-surge-pool')].upgradeSettings.maxSurge absent: observed 1" in body, body
        assert "upgradeSettings.maxUnavailable any_eq" not in body, body

    def test_an_unreadable_cluster_is_not_checked(self):
        world = _healthy_world()
        world["describe"]["seeded-b"] = "UNREACHABLE"
        done = self.run_script(world)
        assert "4 not checked" in done.stderr
        # Four, not one: readiness-surge-blocked, readiness-lb-drain-timeout
        # and drift-peer-b read the same cluster document, so an unreadable
        # cluster takes them out of the run rather than drifting them.
        assert "version-laggard" not in self.drift_files()
        assert "readiness-surge-blocked" not in self.drift_files()

    def test_a_context_without_the_slots_cluster_is_not_checked(self):
        (self.fleet / ".fleet-context").write_text("project=kube-agents-evals\n")
        done = self.run_script(_healthy_world())
        assert "49 role(s) in their designed state, 0 drifted, 12 not checked" in done.stderr
        assert "records no cluster for slot" in done.stderr

    def test_only_published_roles_are_asserted(self):
        self.provision(roles=["crashloop-workload", "rbac-overgrant"])
        world = _healthy_world()
        world["describe"]["seeded-c"] = "UNREACHABLE"
        done = self.run_script(world)
        assert "2 role(s) in their designed state, 0 drifted, 0 not checked" in done.stderr
        assert "describe" not in self.log.read_text()

    def test_a_re_run_that_finds_the_role_converged_removes_the_stale_file(self):
        (self.fleet / "crashloop-workload.drift").write_text("stale\n")
        done = self.run_script(_healthy_world())
        assert done.returncode == 0
        assert self.drift_files() == {}

    def test_the_directory_must_be_the_runners(self):
        (self.fleet / ".kube-agents-fleet-kubeconfigs").unlink()
        done = self.run_script_process(_healthy_world())
        assert done.returncode == 1
        assert "not a directory hack/fleet-kubeconfigs.sh wrote" in done.stderr

    def test_a_malformed_catalog_is_exit_one(self):
        doc = json.loads(_CATALOG.read_text())
        doc["roles"]["crashloop-workload"]["state"][0]["op"] = "roughly"
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps(doc))
        done = self.run_script(_healthy_world(), catalog=bad)
        assert done.returncode == 1
        assert "unknown op 'roughly'" in done.stderr

    def test_the_environment_supplies_the_defaults(self):
        self.world.write_text(json.dumps(_healthy_world()))
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "STUB_LOG": str(self.log),
            "STUB_WORLD": str(self.world),
            "BENCH_FLEET_KUBECONFIG_DIR": str(self.fleet),
            "FLEET_CATALOG": str(_CATALOG),
            "FLEET_FIXTURE_STATE_WAIT_SECONDS": "0",
        }
        done = subprocess.run([sys.executable, str(_SCRIPT)], capture_output=True, text=True, env=env, check=False)
        assert done.returncode == 0, done.stderr
        assert "61 role(s) in their designed state" in done.stderr

    def test_stalled_controller_fixture_detects_drift_on_missing_deadline(self):
        world = _healthy_world()
        world["kubectl"]["deployment/inventory-api"] = {
            "status": {
                "conditions": [
                    {
                        "type": "Progressing",
                        "status": "True",
                        "reason": "NewReplicaSetAvailable",
                    }
                ]
            }
        }
        done = self.run_script(world)
        assert done.returncode == 0, done.stderr
        assert "1 drifted" in done.stderr
        files = self.drift_files()
        assert set(files) == {"stalled-controller"}
        body = files["stalled-controller"]
        assert "ProgressDeadlineExceeded" in body

    def test_stalled_controller_fixture_detects_drift_when_healed_by_configmap(self):
        world = _healthy_world()
        world["kubectl"]["configmap/inventory-flags"] = {"data": {"foo": "bar"}}
        done = self.run_script(world)
        assert done.returncode == 0, done.stderr
        assert "1 drifted" in done.stderr
        files = self.drift_files()
        assert set(files) == {"stalled-controller"}
        body = files["stalled-controller"]
        assert "configmap/inventory-flags absent: observed" in body

    def test_stalled_controller_fixture_detects_drift_when_available_replicas_set(self):
        world = _healthy_world()
        world["kubectl"]["deployment/inventory-api"] = {
            "status": {
                "availableReplicas": 1,
                "conditions": [
                    {
                        "type": "Progressing",
                        "status": "False",
                        "reason": "ProgressDeadlineExceeded",
                    }
                ],
            }
        }
        done = self.run_script(world)
        assert done.returncode == 0, done.stderr
        assert "1 drifted" in done.stderr
        files = self.drift_files()
        assert set(files) == {"stalled-controller"}
        body = files["stalled-controller"]
        assert "status.availableReplicas absent: observed 1" in body

    def test_stalled_controller_fixture_detects_drift_when_pod_waiting_reason_mismatches(self):
        world = _healthy_world()
        world["kubectl"]["pod?app=inventory-api"] = _pods(
            _pod(restarts=0, last_reason=None, phase="Pending", waiting_reason="ImagePullBackOff")
        )
        done = self.run_script(world)
        assert done.returncode == 0, done.stderr
        assert "1 drifted" in done.stderr
        files = self.drift_files()
        assert set(files) == {"stalled-controller"}
        body = files["stalled-controller"]
        assert "CreateContainerConfigError" in body


class ProjectScopedRoleTest(_Harness):
    """A project-scoped role (`cluster_slot: null`) is read with `gcloud compute
    ... list` in the project, and published by its `.confirmed` list alone."""

    def test_the_vm_and_the_rule_are_read_in_the_project(self):
        done = self.run_script(_healthy_world())
        assert done.returncode == 0, done.stderr
        assert self.drift_files() == {}, done.stderr
        log = self.log.read_text()
        assert "gcloud compute instances list --project kube-agents-evals --filter=name ~ ^startup-fail- --format json" in log
        assert "gcloud compute firewall-rules list --project kube-agents-evals --filter=name ~ ^world-open-2379- --format json" in log

    def test_a_stopped_startup_fail_vm_is_drift(self):
        world = _healthy_world()
        world["compute"]["instances"][0]["status"] = "TERMINATED"
        done = self.run_script(world)
        assert set(self.drift_files()) == {"startup-fail-vm"}, done.stderr
        assert 'instance/startup-fail-* status any_eq "RUNNING": observed "TERMINATED"' in self.drift_files()["startup-fail-vm"]

    def test_a_vm_whose_startup_script_was_removed_is_drift(self):
        world = _healthy_world()
        world["compute"]["instances"][0]["metadata"] = {"items": []}
        self.run_script(world)
        assert set(self.drift_files()) == {"startup-fail-vm"}

    def test_the_vms_serial_console_is_read_for_the_failure_line(self):
        done = self.run_script(_healthy_world())
        assert self.drift_files() == {}, done.stderr
        assert (
            "gcloud compute instances get-serial-port-output startup-fail-seeded --zone=us-central1-a --port=1 --project=kube-agents-evals"
            in self.log.read_text()
        )

    def test_a_console_that_lost_the_failure_line_is_drift(self):
        """The 2026-11-08 and -09 nightlies: the VM was RUNNING with its
        script set, the scan read it healthy, and the audit found nothing to
        file because the line had rotated out of the buffer."""
        world = _healthy_world()
        world["serial"]["startup-fail-seeded"] = _WRAPPED_SERIAL
        done = self.run_script(world)
        assert set(self.drift_files()) == {"startup-fail-vm"}, done.stderr
        body = self.drift_files()["startup-fail-vm"]
        assert 'instance/startup-fail-* serial_contains "Script \\"startup-script\\" failed with error"' in body, body
        assert "not in the serial port 1 output of startup-fail-seeded (us-central1-a," in body, body

    def test_an_instance_name_with_a_double_hyphen_is_read(self):
        """GCE allows `--` inside a name; a flag or a newline is still refused."""
        calls = []

        def run(cmd, timeout):
            calls.append(cmd)
            return 0, "boot output", ""

        with mock.patch.object(ffs, "_run", run):
            self.assertEqual(ffs.read_serial("p", "startup-fail--x", "us-central1-a"), "boot output")
            for bad in ("--zone=x", "startup-fail-x\n", "1vm"):
                with self.subTest(bad), self.assertRaises(ffs.Unreadable):
                    ffs.read_serial("p", bad, "us-central1-a")
        self.assertEqual(len(calls), 1)

    def test_a_rotated_console_does_not_hold_the_wait(self):
        """Only a reboot puts the line back, so a wait would be spent for
        nothing: one pass, then drift."""
        world = _healthy_world()
        world["serial"]["startup-fail-seeded"] = _WRAPPED_SERIAL
        started = datetime.now(timezone.utc)
        done = self.run_script(world, "--wait", "60", "--interval", "1")
        assert (datetime.now(timezone.utc) - started).total_seconds() < 30, done.stderr
        assert self.log.read_text().count("get-serial-port-output startup-fail-seeded") == 1, done.stderr
        assert set(self.drift_files()) == {"startup-fail-vm"}, done.stderr

    def test_a_refused_serial_read_is_not_checked_and_leaves_no_drift_file(self):
        world = _healthy_world()
        world["serial"]["startup-fail-seeded"] = "UNREACHABLE"
        done = self.run_script(world)
        assert self.drift_files() == {}, done.stderr
        assert "fixture role 'startup-fail-vm' could not be checked" in done.stderr
        assert "compute.instances.getSerialPortOutput" in done.stderr

    def test_the_serial_flood_vms_console_is_read_for_the_filler(self):
        done = self.run_script(_healthy_world())
        assert self.drift_files() == {}, done.stderr
        assert (
            "gcloud compute instances get-serial-port-output serial-flood-seeded --zone=us-central1-a --port=1 --project=kube-agents-evals"
            in self.log.read_text()
        )

    def test_a_serial_flood_console_still_showing_its_boot_is_drift(self):
        """The rotated console is this fixture's designed state: a buffer
        that still reads as a fresh boot means the flood never ran, and the
        audit would file a plain startup failure instead of the
        rotated-console note the case grades."""
        world = _healthy_world()
        world["serial"]["serial-flood-seeded"] = _FRESH_SERIAL.replace("startup-fail-seeded", "serial-flood-seeded")
        done = self.run_script(world)
        assert set(self.drift_files()) == {"rotated-console-vm"}, done.stderr
        body = self.drift_files()["rotated-console-vm"]
        assert 'instance/serial-flood-* serial_contains "eval fixture: serial-flood filler"' in body, body
        assert "not in the serial port 1 output of serial-flood-seeded (us-central1-a," in body, body

    def test_a_stopped_serial_flood_vm_is_drift(self):
        world = _healthy_world()
        world["compute"]["instances"][2]["status"] = "TERMINATED"
        done = self.run_script(world)
        assert set(self.drift_files()) == {"rotated-console-vm"}, done.stderr
        assert 'instance/serial-flood-* status any_eq "RUNNING": observed "TERMINATED"' in self.drift_files()["rotated-console-vm"]

    def test_serial_contains_is_for_a_project_instance_subject_only(self):
        for role, change in (
            ("world-open-ingress", {"subject": "firewall/world-open-2379-*"}),
            ("crashloop-workload", {"subject": "pod?app=payments-api"}),
            ("startup-fail-vm", {"path": "status"}),
            ("startup-fail-vm", {"value": ""}),
            ("startup-fail-vm", {"value": 1}),
        ):
            with self.subTest(role=role, change=change):
                catalog = json.loads(_CATALOG.read_text())
                entry = {"subject": "instance/startup-fail-*", "op": "serial_contains", "value": "failed with error"}
                entry.update(change)
                catalog["roles"][role]["state"].append(entry)
                path = self.tmp / "bad.json"
                path.write_text(json.dumps(catalog))
                with self.assertRaises(ffs.CatalogError):
                    ffs.load_assertions(path)

    def test_a_narrowed_or_disabled_rule_is_drift(self):
        for change in ({"sourceRanges": ["10.0.0.0/8"]}, {"disabled": True}, {"allowed": [{"IPProtocol": "tcp", "ports": ["2380"]}]}):
            with self.subTest(change=change):
                world = _healthy_world()
                world["compute"]["firewall-rules"][0].update(change)
                self.run_script(world)
                assert set(self.drift_files()) == {"world-open-ingress"}

    def test_a_vm_without_its_external_ip_is_drift(self):
        world = _healthy_world()
        world["compute"]["instances"][1]["networkInterfaces"] = [{"accessConfigs": []}]
        self.run_script(world)
        assert set(self.drift_files()) == {"world-open-ingress"}

    def test_a_deleted_object_is_drift_not_weather(self):
        world = _healthy_world()
        world["compute"]["instances"] = world["compute"]["instances"][1:]
        self.run_script(world)
        assert set(self.drift_files()) == {"startup-fail-vm"}

    def test_a_refused_list_is_not_checked_and_leaves_no_drift_file(self):
        world = _healthy_world()
        world["compute"]["instances"] = "UNREACHABLE"
        done = self.run_script(world)
        assert self.drift_files() == {}, done.stderr
        assert "58 role(s) in their designed state, 0 drifted, 3 not checked" in done.stderr
        assert "fixture role 'startup-fail-vm' could not be checked" in done.stderr

    def test_the_peered_networks_are_read_in_the_project(self):
        done = self.run_script(_healthy_world())
        assert done.returncode == 0, done.stderr
        assert self.drift_files() == {}, done.stderr
        log = self.log.read_text()
        # One list per network: the fixture's own and `default`, its peer.
        assert log.count("gcloud compute networks list --project kube-agents-evals --filter=name ~ ^mtu-peer-declared --format json") == 1
        assert log.count("gcloud compute networks list --project kube-agents-evals --filter=name ~ ^default --format json") == 1

    def test_a_missing_peering_is_drift(self):
        # A peering is two one-way halves, and a half alone reads INACTIVE;
        # the collector compares ACTIVE peerings only, so either half gone is
        # the posture gone.
        for side, peering in ((0, "default-to-mtu-peer-declared"), (1, "mtu-peer-declared-to-default")):
            with self.subTest(peering=peering):
                world = _healthy_world()
                world["compute"]["networks"][side]["peerings"] = []
                world["compute"]["networks"][1 - side]["peerings"][0]["state"] = "INACTIVE"
                done = self.run_script(world)
                assert set(self.drift_files()) == {"declared-mtu-peering"}, done.stderr
                assert f"peerings[?(@.name=='{peering}')].state any_eq \"ACTIVE\"" in self.drift_files()["declared-mtu-peering"]

    def test_a_changed_mtu_is_drift(self):
        # The posture is the mismatch: the jumbo network back at 1460 ends
        # it, and so does `default` raised to meet it.
        world = _healthy_world()
        world["compute"]["networks"][1]["mtu"] = 1460
        done = self.run_script(world)
        assert set(self.drift_files()) == {"declared-mtu-peering"}, done.stderr
        assert "network/mtu-peer-declared* mtu any_eq 8896: observed 1460" in self.drift_files()["declared-mtu-peering"]
        world = _healthy_world()
        world["compute"]["networks"][0]["mtu"] = 8896
        done = self.run_script(world)
        assert set(self.drift_files()) == {"declared-mtu-peering"}, done.stderr
        assert "network/default* mtu none_eq 8896" in self.drift_files()["declared-mtu-peering"]

    def test_a_project_role_without_its_confirmed_list_is_not_asserted(self):
        self.provision(roles=[role for role in self.catalog["roles"] if role != "startup-fail-vm"])
        (self.fleet / "startup-fail-vm.kubeconfig").write_text("apiVersion: v1\nkind: Config\n")
        self.run_script(_healthy_world())
        assert "^startup-fail-" not in self.log.read_text()

    def test_a_project_role_with_a_cluster_subject_is_a_catalog_bug(self):
        catalog = json.loads(_CATALOG.read_text())
        catalog["roles"]["startup-fail-vm"]["state"][0]["subject"] = "cluster"
        path = self.tmp / "bad.json"
        path.write_text(json.dumps(catalog))
        with self.assertRaises(ffs.CatalogError):
            ffs.load_assertions(path)
        catalog["roles"]["startup-fail-vm"]["state"][0]["subject"] = "deployment/startup-fail"
        path.write_text(json.dumps(catalog))
        with self.assertRaises(ffs.CatalogError):
            ffs.load_assertions(path)
        catalog["roles"]["startup-fail-vm"]["state"][0]["subject"] = "instance/startup-fail-*"
        catalog["roles"]["startup-fail-vm"]["namespace"] = "seeded-debug"
        path.write_text(json.dumps(catalog))
        with self.assertRaises(ffs.CatalogError):
            ffs.load_assertions(path)


class ReservationSubjectTest(_Harness):
    """`reservation/<name-prefix>*` is the project's reservations, read with
    `gcloud compute reservations list`. It is the one project subject that a
    cluster role carries: the pool that names a reservation is a cluster's, and
    the reservation is not (#6367)."""

    _LIST = "gcloud compute reservations list --project kube-agents-evals --filter=name ~ ^seeded-absent-reservation --format json"

    def test_the_reservations_are_listed_in_the_project_for_the_pool_role(self):
        done = self.run_script(_healthy_world())
        assert done.returncode == 0, done.stderr
        assert self.drift_files() == {}, done.stderr
        assert self._LIST in self.log.read_text()

    def test_a_reservation_with_the_fixtures_name_is_drift_that_names_it(self):
        """Someone created the reservation: the pool can grow now, and nothing
        on the cluster shows it until the first node."""
        world = _healthy_world()
        world["compute"]["reservations"] = [{"name": "seeded-absent-reservation", "zone": "https://www.googleapis.com/compute/v1/projects/kube-agents-evals/zones/us-central1-a"}]
        done = self.run_script(world)
        assert set(self.drift_files()) == {"missing-reservation-pool"}, done.stderr
        body = self.drift_files()["missing-reservation-pool"]
        assert 'reservation/seeded-absent-reservation* name none_eq "seeded-absent-reservation": observed "seeded-absent-reservation"' in body, body

    def test_a_reservation_of_another_name_is_not_drift(self):
        """The pool consumes one exact name. A reservation whose name only
        starts with it gives the pool nothing."""
        world = _healthy_world()
        world["compute"]["reservations"] = [{"name": "team-batch-reservation"}, {"name": "seeded-absent-reservation-2"}]
        done = self.run_script(world)
        assert self.drift_files() == {}, done.stderr

    def test_a_list_that_gcloud_read_in_part_is_not_checked(self):
        """gcloud exits 0 on a list that failed in part. An empty answer then
        is not "no reservation": the role is not checked, never healthy."""
        world = _healthy_world()
        world["compute"]["reservations"] = "PARTIAL"
        done = self.run_script(world)
        assert self.drift_files() == {}, done.stderr
        assert "60 role(s) in their designed state, 0 drifted, 1 not checked" in done.stderr
        assert "fixture role 'missing-reservation-pool' could not be checked" in done.stderr
        # The reason is each item under gcloud's first warning, not its last line.
        assert "compute reservations list was partial: Internal error in zone us-central1-a; Internal error in zone us-central1-b" in done.stderr
        assert "filter keys" not in done.stderr

    def test_a_refused_reservation_list_is_not_checked_and_leaves_no_drift_file(self):
        world = _healthy_world()
        world["compute"]["reservations"] = "UNREACHABLE"
        done = self.run_script(world)
        assert self.drift_files() == {}, done.stderr
        assert "60 role(s) in their designed state, 0 drifted, 1 not checked" in done.stderr
        assert "fixture role 'missing-reservation-pool' could not be checked" in done.stderr

    def test_no_other_project_subject_is_for_a_cluster_role(self):
        catalog = json.loads(_CATALOG.read_text())
        reservation = {"subject": "reservation/seeded-absent-reservation*", "path": "name", "op": "none_eq", "value": "seeded-absent-reservation", "why": "w"}
        path = self.tmp / "catalog.json"
        for subject, refused in (
            ("reservation/seeded-absent-reservation*", False),
            ("firewall/world-open-2379-*", True),
            ("reservation/Seeded*", True),
            ("reservation/seeded-absent-reservation", True),
            ("reservation?", True),
            ("reservation?name=seeded-absent-reservation", True),
        ):
            with self.subTest(subject=subject):
                catalog["roles"]["missing-reservation-pool"]["state"] = [{**reservation, "subject": subject}]
                path.write_text(json.dumps(catalog))
                if refused:
                    with self.assertRaises(ffs.CatalogError):
                        ffs.load_assertions(path)
                else:
                    assert ffs.load_assertions(path)["missing-reservation-pool"]["state"][0]["subject"] == subject


class RepositorySubjectTest(_Harness):
    """The declared-intent note in the pool project's GitOps repository is a
    fixture the declared-* roles depend on (#5827): forty registered projects
    carried the five-item note after the sixth declaration landed, the audit
    filed the pool as a finding, and the case graded `fail` because the scan
    read the node pools only. The `repository/<path>` subject reads the file
    out of the repository through the pool verifier, and the `as_seeded` op
    holds its blob to the one provisioning seeds."""

    def _note_lines(self, files: dict[str, str]) -> dict[str, str]:
        return {role: next(line for line in body.splitlines() if line.startswith(_NOTE_SUBJECT)) for role, body in files.items()}

    def _gh_calls(self) -> list[str]:
        return [line for line in self.log.read_text().splitlines() if line.startswith("gh ")]

    def test_the_subject_and_the_op_are_the_catalogs_vocabulary(self):
        # scripts/test_eval_rosters.py reads this constant to learn the scan
        # asserts on pool repositories; bench/kube_agents_bench/fixture_drift.py
        # matches drift lines on it.
        assert ffs.REPOSITORY_SUBJECT == "repository"
        assert ffs.OP_AS_SEEDED in ffs.OPS_WITHOUT_VALUE and ffs.OP_AS_SEEDED in ffs.OPS_WITHOUT_PATH
        roles = ffs.load_assertions(_CATALOG)
        on_note = {role for role, spec in roles.items() if any(e["subject"] == _NOTE_SUBJECT for e in spec["state"])}
        assert on_note == _NOTE_ROLES
        for role in on_note:
            entry = next(e for e in roles[role]["state"] if e["subject"] == _NOTE_SUBJECT)
            assert entry["op"] == ffs.OP_AS_SEEDED and entry["why"], (role, entry)

    def test_the_seeded_note_converges_and_is_read_once_for_every_role_on_it(self):
        done = self.run_script(_healthy_world())
        assert done.returncode == 0, done.stderr
        assert "0 drifted, 0 not checked" in done.stderr, done.stderr
        assert self._gh_calls() == [f"gh api repos/{_POOL_REPO}/contents/{_NOTE_PATH}"], self._gh_calls()

    def test_the_five_item_note_is_drift_on_every_declared_role(self):
        world = _healthy_world()
        world["github"][_POOL_REPO][_NOTE_PATH] = _FIVE_ITEM_NOTE_SHA
        report = self.tmp / "report.json"
        done = self.run_script(world, "--report", str(report))
        assert done.returncode == 0, done.stderr
        files = self.drift_files()
        assert set(files) == _NOTE_ROLES, (files, done.stderr)
        for role, line in self._note_lines(files).items():
            assert line.startswith(f"{_NOTE_SUBJECT} as_seeded: observed blob {_FIVE_ITEM_NOTE_SHA[:8]}, seeded blob {_SEEDED_NOTE_SHA[:8]}"), (role, line)
            # The repair is the verifier's own replace command, with the live
            # file's sha; the line names what prints it rather than carrying
            # the multi-line note inline.
            assert f'print(v.gitops_note_seed_command("{_POOL_REPO}", "{_FIVE_ITEM_NOTE_SHA}"))' in line, line
            assert "(why:" in line
        assert f"{len(_NOTE_ROLES)} drifted" in done.stderr
        # The in-cluster assertions of the same roles still held: the note is
        # the only line, so the reason the gate records is the note's.
        for role, body in files.items():
            assert body.count("\n") == 1, (role, body)
        doc = json.loads(report.read_text())
        for role in _NOTE_ROLES:
            assert doc["roles"][role]["state"] == "drifted"
            assert doc["roles"][role]["detail"][0].startswith(_NOTE_SUBJECT), doc["roles"][role]

    def test_a_missing_note_in_a_visible_repository_is_drift(self):
        world = _healthy_world()
        world["github"][_POOL_REPO] = {}
        done = self.run_script(world)
        files = self.drift_files()
        assert set(files) == _NOTE_ROLES, (files, done.stderr)
        for line in self._note_lines(files).values():
            assert f"{_NOTE_SUBJECT} as_seeded: observed nothing" in line, line
            # No live sha: the printed command seeds rather than replaces.
            assert f'print(v.gitops_note_seed_command("{_POOL_REPO}", ""))' in line, line
        # The 404 was told from an invisible repository by one more read.
        assert self._gh_calls() == [f"gh api repos/{_POOL_REPO}/contents/{_NOTE_PATH}", f"gh api repos/{_POOL_REPO}"], self._gh_calls()

    def test_a_repository_the_credential_cannot_see_is_not_checked(self):
        # gh answers 404 for a private repository the token cannot see as it
        # does for a missing file; only a repository that answers is evidence.
        world = _healthy_world()
        world["github"] = {}
        done = self.run_script(world)
        assert self.drift_files() == {}, done.stderr
        assert f"{len(_NOTE_ROLES)} not checked" in done.stderr, done.stderr
        for role in _NOTE_ROLES:
            assert f"fixture role '{role}' could not be checked" in done.stderr
        assert "not visible to this credential" in done.stderr, done.stderr

    def test_no_credential_leaves_the_note_unasserted_and_the_roles_on_their_cluster_state(self):
        # The hourly scan's case, and a laptop with no gh login: the pass has
        # nothing to read the repository with, so the assertion is not made,
        # the role rests on what the identity can read, and the report says
        # what was left out. Not "not checked": that verdict would hold a
        # fixture_drift incident on the role open forever on the hourly scan
        # and drop the digest's clean line for good.
        world = _healthy_world()
        del world["github"]
        report = self.tmp / "report.json"
        done = self.run_script(world, "--report", str(report))
        assert done.returncode == 0, done.stderr
        assert self.drift_files() == {}, done.stderr
        assert "0 drifted, 0 not checked" in done.stderr, done.stderr
        assert done.stderr.count("WARNING") == 1 and "not asserted" in done.stderr and "gh auth login" in done.stderr, done.stderr
        doc = json.loads(report.read_text())
        assert [u["subject"] for u in doc["unasserted"]] == [_NOTE_SUBJECT], doc["unasserted"]
        assert set(doc["unasserted"][0]["roles"]) == _NOTE_ROLES
        assert all(doc["roles"][role]["state"] == "converged" for role in _NOTE_ROLES)
        assert len(self._gh_calls()) == 1, self._gh_calls()
        # A cluster assertion out of shape on the same role is still drift
        # (the stub answers the PDB list for no-pdb-workload's namespace too).
        world["kubectl"]["poddisruptionbudget?"] = {"items": [{"metadata": {"name": "added"}}]}
        self.run_script(world)
        assert "declared-no-pdb-workload" in self.drift_files()
        assert "poddisruptionbudget?" in self.drift_files()["declared-no-pdb-workload"]

    def test_a_healthy_report_carries_no_unasserted_list(self):
        report = self.tmp / "report.json"
        self.run_script(_healthy_world(), "--report", str(report))
        assert json.loads(report.read_text())["unasserted"] == []

    def test_a_transport_failure_is_not_checked(self):
        world = _healthy_world()
        world["github"] = "UNREACHABLE"
        done = self.run_script(world)
        assert self.drift_files() == {}, done.stderr
        assert f"{len(_NOTE_ROLES)} not checked" in done.stderr, done.stderr

    def test_a_note_drift_does_not_hold_the_wait(self):
        # Only an operator's replace clears it; a wait sees nothing converge.
        # One pass: a held wait would re-read the note every second of the
        # sixty, so the one contents read is the evidence (not the clock,
        # which a loaded machine stretches).
        world = _healthy_world()
        world["github"][_POOL_REPO][_NOTE_PATH] = _FIVE_ITEM_NOTE_SHA
        done = self.run_script(world, "--wait", "60", "--interval", "1")
        assert set(self.drift_files()) == _NOTE_ROLES, done.stderr
        assert len(self._gh_calls()) == 1, self._gh_calls()

    def test_a_note_drift_beside_a_healed_cluster_fixture_is_still_drift(self):
        world = _healthy_world()
        world["github"][_POOL_REPO][_NOTE_PATH] = _FIVE_ITEM_NOTE_SHA
        world["kubectl"]["poddisruptionbudget?"] = {"items": [{"metadata": {"name": "added"}}]}
        self.run_script(world)
        body = self.drift_files()["declared-no-pdb-workload"]
        assert _NOTE_SUBJECT in body and "poddisruptionbudget?" in body, body

    def test_the_catalog_refuses_the_op_off_the_repository_and_a_path_nobody_seeds(self):
        for role, entry in (
            ("declared-no-pdb-workload", {"subject": "deployment/notification-relay", "op": "as_seeded"}),
            ("declared-no-pdb-workload", {"subject": "repository/knowledge/other.md", "op": "as_seeded"}),
            ("declared-no-pdb-workload", {"subject": _NOTE_SUBJECT, "op": "absent"}),
            ("declared-no-pdb-workload", {"subject": _NOTE_SUBJECT, "op": "as_seeded", "value": "x"}),
            ("declared-no-pdb-workload", {"subject": _NOTE_SUBJECT, "op": "as_seeded", "path": "sha"}),
            ("declared-no-pdb-workload", {"subject": "repository/../etc/passwd", "op": "as_seeded"}),
            ("startup-fail-vm", {"subject": _NOTE_SUBJECT, "op": "absent"}),
        ):
            with self.subTest(role=role, entry=entry):
                catalog = json.loads(_CATALOG.read_text())
                catalog["roles"][role]["state"].append(entry)
                path = self.tmp / "bad.json"
                path.write_text(json.dumps(catalog))
                with self.assertRaises(ffs.CatalogError):
                    ffs.load_assertions(path)

class OrphanSnapshotRoleTest(_Harness):
    """The two orphaned-snapshot roles (declared-orphan-snapshot and its
    undeclared control, orphan-snapshot): project-scoped `snapshot/<prefix>*`
    subjects, a `source_disk_gone` read of the project's disks, and a
    `ready_after_hours` gate that reports the role NOT READY, never drifted,
    for the 91 days the compute audit's orphaned-snapshots check ignores a
    snapshot."""

    ROLES = ("declared-orphan-snapshot", "orphan-snapshot")

    def test_an_aged_orphan_is_read_in_the_project_and_converges(self):
        done = self.run_script(_healthy_world())
        assert done.returncode == 0, done.stderr
        assert self.drift_files() == {}, done.stderr
        assert "0 drifted, 0 not checked, 0 not yet ready" in done.stderr
        log = self.log.read_text()
        assert "gcloud compute snapshots list --project kube-agents-evals --filter=name ~ ^declared-orphan-snap- --format json" in log
        assert "gcloud compute snapshots list --project kube-agents-evals --filter=name ~ ^orphan-snap- --format json" in log
        # The source disk is looked up by the name the snapshot's sourceDisk URL ends in, once per name.
        assert log.count("gcloud compute disks list --project kube-agents-evals --filter=name ~ ^orphan-snap-source-seeded$ --format json") == 1, log

    def test_a_young_orphan_is_not_ready_not_drifted(self):
        world = _healthy_world()
        created = (datetime.now(timezone.utc) - timedelta(days=10)).replace(microsecond=0)
        world["compute"]["snapshots"] = [_snapshot("declared-orphan-snap-seeded", created=created.isoformat()), _snapshot("orphan-snap-seeded")]
        report = self.tmp / "report.json"
        done = self.run_script(world, "--report", str(report))
        assert done.returncode == 0, done.stderr
        assert self.drift_files() == {}, done.stderr
        assert "60 role(s) in their designed state, 0 drifted, 0 not checked, 1 not yet ready" in done.stderr, done.stderr
        assert "NOTE: fixture role 'declared-orphan-snapshot' is planted in kube-agents-evals but not yet ready" in done.stderr, done.stderr
        assert "WARNING" not in done.stderr
        doc = json.loads(report.read_text())
        entry = doc["roles"]["declared-orphan-snapshot"]
        assert entry["state"] == "not_ready", entry
        ready_at = (created + timedelta(hours=2184)).isoformat()
        assert len(entry["detail"]) == 1 and entry["detail"][0].startswith("snapshot/declared-orphan-snap-* creationTimestamp ready_after_hours 2184: observed"), entry
        assert f"ready at {ready_at}" in entry["detail"][0], entry
        assert doc["roles"]["orphan-snapshot"]["state"] == "converged"
        assert doc["summary"] == {"converged": 60, "drifted": 0, "unchecked": 0, "not_ready": 1}

    def test_the_gate_does_not_hold_the_wait(self):
        # One read of the prefix under a 60-second wait at a 1-second interval
        # is the proof: a held wait would re-read the role every second. The
        # elapsed time is not asserted, because one pass over the catalog
        # takes longer than any threshold on a loaded host.
        world = _healthy_world()
        world["compute"]["snapshots"][0]["creationTimestamp"] = _created(2)
        done = self.run_script(world, "--wait", "60", "--interval", "1")
        assert self.log.read_text().count("^declared-orphan-snap-") == 1, done.stderr
        assert "1 not yet ready" in done.stderr

    def test_a_live_source_disk_is_drift(self):
        world = _healthy_world()
        world["compute"]["disks"] = [_SNAPSHOT_SOURCE_DISK]
        done = self.run_script(world)
        assert set(self.drift_files()) == set(self.ROLES), done.stderr
        body = self.drift_files()["declared-orphan-snapshot"]
        assert "snapshot/declared-orphan-snap-* source_disk_gone: sourceDisk orphan-snap-source-seeded is a live disk" in body, body

    def test_a_live_source_disk_beside_a_young_snapshot_is_still_drift(self):
        world = _healthy_world()
        world["compute"]["disks"] = [_SNAPSHOT_SOURCE_DISK]
        world["compute"]["snapshots"][0]["creationTimestamp"] = _created(2)
        done = self.run_script(world)
        assert set(self.drift_files()) == set(self.ROLES), done.stderr
        assert "2 drifted, 0 not checked, 0 not yet ready" in done.stderr
        body = self.drift_files()["declared-orphan-snapshot"]
        assert "is a live disk" in body and "not ready: snapshot/declared-orphan-snap-* creationTimestamp ready_after_hours" in body, body

    def test_a_scheduled_snapshot_is_drift(self):
        # The collector excludes a snapshot on either schedule field.
        for field, value in (
            ("sourceSnapshotSchedulePolicy", "https://www.googleapis.com/compute/v1/projects/kube-agents-evals/regions/us-central1/resourcePolicies/daily"),
            ("sourceSnapshotSchedulePolicyId", "1234567890"),
        ):
            with self.subTest(field=field):
                world = _healthy_world()
                world["compute"]["snapshots"][1][field] = value
                done = self.run_script(world)
                assert set(self.drift_files()) == {"orphan-snapshot"}, done.stderr
                assert f"snapshot/orphan-snap-* {field} absent: observed" in self.drift_files()["orphan-snapshot"]

    def test_a_live_source_disk_does_not_hold_the_wait(self):
        # Only the next reconcile's re-run of the planter deletes it; one
        # disk read under a 60-second wait is the proof, as above.
        world = _healthy_world()
        world["compute"]["disks"] = [_SNAPSHOT_SOURCE_DISK]
        done = self.run_script(world, "--wait", "60", "--interval", "1")
        assert self.log.read_text().count("^orphan-snap-source-seeded$") == 1, done.stderr
        assert set(self.drift_files()) == set(self.ROLES)

    def test_a_second_snapshot_under_the_prefix_is_drift_that_holds_no_wait(self):
        # Two timestamps are not one: the gate cannot be read, which is drift
        # (a renamed snapshot's predecessor, deleted by hand), and no wait
        # clears it.
        world = _healthy_world()
        world["compute"]["snapshots"].append(_snapshot("declared-orphan-snap-old"))
        done = self.run_script(world, "--wait", "60", "--interval", "1")
        assert self.log.read_text().count("^declared-orphan-snap-") == 1, done.stderr
        assert set(self.drift_files()) == {"declared-orphan-snapshot"}, done.stderr
        assert "ready_after_hours 2184: observed" in self.drift_files()["declared-orphan-snapshot"]
        assert "not one RFC 3339 timestamp" in self.drift_files()["declared-orphan-snapshot"]

    def test_the_gate_is_the_compute_audits_own_threshold(self):
        # The gate is the hour at which the collector starts to flag the
        # snapshot: an hour younger is no finding, that old is one. Asked of
        # the collector itself, so a moved threshold or a changed comparison
        # fails this test rather than leaving the roles ready on the wrong day.
        spec = importlib.util.spec_from_file_location(
            "compute_fleet_audit", _REPO / "agents" / "platform" / "skills" / "gce-compute-fleet-audit" / "scripts" / "compute_fleet_audit.py"
        )
        collector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(collector)
        now = datetime.now(timezone.utc)

        def flagged(hours: float) -> bool:
            snapshot = {"name": "orphan-snap-seeded", "sourceDisk": _SNAPSHOT_SOURCE_URL, "creationTimestamp": (now - timedelta(hours=hours)).isoformat()}
            return collector.check_orphaned_snapshot(snapshot, set(), now) is not None

        for role in self.ROLES:
            gates = [e["value"] for e in self.catalog["roles"][role]["state"] if e["op"] == "ready_after_hours"]
            assert len(gates) == 1, (role, gates)
            assert not flagged(gates[0] - 1) and flagged(gates[0]), (role, gates)

    def test_a_snapshot_that_is_not_ready_on_the_api_is_drift(self):
        world = _healthy_world()
        world["compute"]["snapshots"][0]["status"] = "UPLOADING"
        self.run_script(world)
        assert set(self.drift_files()) == {"declared-orphan-snapshot"}

    def test_a_snapshot_without_a_source_disk_field_is_drift(self):
        world = _healthy_world()
        del world["compute"]["snapshots"][0]["sourceDisk"]
        done = self.run_script(world)
        assert set(self.drift_files()) == {"declared-orphan-snapshot"}, done.stderr
        assert "source_disk_gone: declared-orphan-snap-seeded carries no sourceDisk" in self.drift_files()["declared-orphan-snapshot"]

    def test_a_deleted_snapshot_is_drift_not_weather(self):
        world = _healthy_world()
        world["compute"]["snapshots"] = world["compute"]["snapshots"][1:]
        done = self.run_script(world)
        assert set(self.drift_files()) == {"declared-orphan-snapshot"}, done.stderr
        assert "source_disk_gone: no snapshot matched" in self.drift_files()["declared-orphan-snapshot"]

    def test_a_refused_disk_list_is_not_checked_and_leaves_no_drift_file(self):
        world = _healthy_world()
        world["compute"]["disks"] = "UNREACHABLE"
        done = self.run_script(world)
        assert self.drift_files() == {}, done.stderr
        assert "59 role(s) in their designed state, 0 drifted, 2 not checked, 0 not yet ready" in done.stderr, done.stderr
        assert "fixture role 'declared-orphan-snapshot' could not be checked" in done.stderr
        assert "compute.disks.list" in done.stderr

    def test_the_two_ops_are_for_a_project_snapshot_subject_only(self):
        for role, entry in (
            ("world-open-ingress", {"subject": "firewall/world-open-2379-*", "op": "source_disk_gone"}),
            ("crashloop-workload", {"subject": "pod?app=payments-api", "op": "source_disk_gone"}),
            ("declared-orphan-snapshot", {"subject": "snapshot/declared-orphan-snap-*", "op": "source_disk_gone", "path": "sourceDisk"}),
            ("declared-orphan-snapshot", {"subject": "snapshot/declared-orphan-snap-*", "op": "source_disk_gone", "value": 1}),
            ("declared-orphan-snapshot", {"subject": "snapshot/declared-orphan-snap-*", "op": "ready_after_hours", "path": "creationTimestamp"}),
            ("declared-orphan-snapshot", {"subject": "snapshot/declared-orphan-snap-*", "op": "ready_after_hours", "path": "creationTimestamp", "value": "soon"}),
            ("declared-orphan-snapshot", {"subject": "snapshot/declared-orphan-snap-*", "op": "ready_after_hours", "value": 1}),
        ):
            with self.subTest(role=role, entry=entry):
                catalog = json.loads(_CATALOG.read_text())
                catalog["roles"][role]["state"].append(entry)
                path = self.tmp / "bad.json"
                path.write_text(json.dumps(catalog))
                with self.assertRaises(ffs.CatalogError):
                    ffs.load_assertions(path)

    def test_ready_after_hours_evaluates_like_the_age_gate_until_it_passes(self):
        entry = {"subject": "snapshot/x-*", "path": "creationTimestamp", "op": "ready_after_hours", "value": 2184, "why": "", "steps": ffs.parse_path("creationTimestamp")}
        assert ffs.evaluate(entry, [{"creationTimestamp": _created(24 * 92)}]) is None
        with self.assertRaises(ffs.NotReady) as caught:
            ffs.evaluate(entry, [{"creationTimestamp": _created(24 * 89)}])
        assert "ready at" in str(caught.exception) and "hours old" in str(caught.exception)
        # A timestamp that will not parse is a broken object, which is drift, not a clock.
        assert "not one RFC 3339 timestamp" in ffs.evaluate(entry, [{"creationTimestamp": "soon"}])
        assert "not one RFC 3339 timestamp" in ffs.evaluate(entry, [])



class FinishedJobFamilyRoleTest(_Harness):
    """generated-name-job-family: three finished Jobs named `reindex-<uuid>` on
    slot a, with no TTL and no owner. The cost audit's 3.9 files a finished Job
    7 days after it finished, so each Job has a `ready_after_hours` gate on its
    `status.completionTime`: a cluster role that is NOT READY, never drifted,
    for its first 168 hours."""

    ROLE = "generated-name-job-family"

    def test_three_jobs_that_finished_long_ago_converge(self):
        done = self.run_script(_healthy_world())
        assert done.returncode == 0, done.stderr
        assert self.drift_files() == {}, done.stderr
        assert "0 drifted, 0 not checked, 0 not yet ready" in done.stderr, done.stderr
        log = self.log.read_text()
        for subject in _FAMILY_JOBS:
            assert f"get {subject.replace('/', ' ')} -n seeded-family -o json" in log, log

    def test_a_job_that_finished_a_day_ago_is_not_ready_not_drifted(self):
        world = _healthy_world()
        finished = (datetime.now(timezone.utc) - timedelta(hours=24)).replace(microsecond=0)
        world["kubectl"][_FAMILY_JOBS[0]]["status"]["completionTime"] = finished.isoformat()
        report = self.tmp / "report.json"
        done = self.run_script(world, "--report", str(report))
        assert done.returncode == 0, done.stderr
        assert self.drift_files() == {}, done.stderr
        assert "0 drifted, 0 not checked, 1 not yet ready" in done.stderr, done.stderr
        assert f"NOTE: fixture role '{self.ROLE}' is planted in kube-agents-evals but not yet ready" in done.stderr, done.stderr
        entry = json.loads(report.read_text())["roles"][self.ROLE]
        assert entry["state"] == "not_ready", entry
        assert len(entry["detail"]) == 1 and entry["detail"][0].startswith(f"{_FAMILY_JOBS[0]} status.completionTime ready_after_hours 168: observed"), entry
        assert f"ready at {(finished + timedelta(hours=168)).isoformat()}" in entry["detail"][0], entry

    def test_the_gate_is_the_cost_audits_own_threshold(self):
        # The gate is the hour at which check 3.9 starts to file the Job: an
        # hour younger is no finding, that old is one. Asked of the collector
        # itself, so a moved threshold or a different timestamp fails this
        # test and does not leave the role ready on the wrong day.
        scripts = _REPO / "agents" / "platform" / "skills" / "fleet-audit" / "scripts"
        spec = importlib.util.spec_from_file_location("fleet_waste", scripts / "fleet_waste.py")
        collector = importlib.util.module_from_spec(spec)
        # The collector imports the modules beside it by name. The two patches
        # put its folder on the path for the import only, and take the modules
        # that it loaded out of sys.modules again.
        with mock.patch.object(sys, "path", [str(scripts), *sys.path]), mock.patch.dict(sys.modules):
            spec.loader.exec_module(collector)
        now = datetime.now(timezone.utc)
        name = _FAMILY_JOBS[0].split("/", 1)[1]

        def filed(hours: float) -> bool:
            job = {"metadata": {"name": name, "namespace": "seeded-family"}, "spec": {}, "status": {"succeeded": 1, "completionTime": (now - timedelta(hours=hours)).isoformat()}}
            hits = collector.check_terminal_pods({"pods": [], "jobs": [job], "cronjobs": []}, now=now)
            return any(hit["object"] == f"Job/{name}" for hit in hits)

        entries = [e for e in self.catalog["roles"][self.ROLE]["state"] if e["op"] == "ready_after_hours"]
        assert sorted(e["subject"] for e in entries) == sorted(_FAMILY_JOBS), entries
        for entry in entries:
            assert entry["path"] == "status.completionTime", entry
            assert not filed(entry["value"] - 1) and filed(entry["value"]), entry

    def test_the_script_holds_the_gate_within_an_hour(self):
        # An hour before the gate one Job keeps the role not ready; an hour
        # after it the role is in its designed state.
        for hours, summary in ((167, "0 drifted, 0 not checked, 1 not yet ready"), (169, "0 drifted, 0 not checked, 0 not yet ready")):
            with self.subTest(hours=hours):
                world = _healthy_world()
                world["kubectl"][_FAMILY_JOBS[2]]["status"]["completionTime"] = _created(hours)
                done = self.run_script(world)
                assert done.returncode == 0, done.stderr
                assert self.drift_files() == {}, done.stderr
                assert summary in done.stderr, done.stderr

    def test_each_job_is_drift_with_a_ttl_an_owner_or_no_family_label(self):
        # Check 3.9 files no Job that has a TTL (and the TTL controller deletes
        # it) and no Job that a CronJob owns; the family has no owner at all,
        # and the label is the mark of its parent. Each Job has each assertion.
        def with_ttl(job):
            job["spec"]["ttlSecondsAfterFinished"] = 3600

        def with_owner(job):
            job["metadata"]["ownerReferences"] = [{"kind": "CronJob", "name": "reindex"}]

        def without_label(job):
            job["metadata"]["labels"] = {}

        for defect, line in (
            (with_ttl, "spec.ttlSecondsAfterFinished absent: observed 3600"),
            (with_owner, "metadata.ownerReferences absent: observed"),
            (without_label, 'metadata.labels["app.kubernetes.io/part-of"] eq "reindex": observed nothing'),
        ):
            for subject in _FAMILY_JOBS:
                with self.subTest(defect=defect.__name__, subject=subject):
                    world = _healthy_world()
                    defect(world["kubectl"][subject])
                    done = self.run_script(world)
                    assert set(self.drift_files()) == {self.ROLE}, done.stderr
                    assert f"{subject} {line}" in self.drift_files()[self.ROLE], self.drift_files()[self.ROLE]

    def test_each_job_that_did_not_succeed_is_drift(self):
        for subject in _FAMILY_JOBS:
            with self.subTest(subject=subject):
                world = _healthy_world()
                world["kubectl"][subject]["status"] = {"failed": 1, "conditions": [{"type": "Failed", "status": "True"}]}
                done = self.run_script(world)
                assert set(self.drift_files()) == {self.ROLE}, done.stderr
                assert f"{subject} status.succeeded eq 1: observed nothing" in self.drift_files()[self.ROLE], self.drift_files()[self.ROLE]

    def test_a_job_that_did_not_succeed_is_drift_also_when_it_is_young(self):
        world = _healthy_world()
        world["kubectl"][_FAMILY_JOBS[0]]["status"] = {"failed": 1, "conditions": [{"type": "Failed", "status": "True"}]}
        world["kubectl"][_FAMILY_JOBS[1]]["status"]["completionTime"] = _created(2)
        done = self.run_script(world)
        assert set(self.drift_files()) == {self.ROLE}, done.stderr
        body = self.drift_files()[self.ROLE]
        assert f"{_FAMILY_JOBS[0]} status.succeeded eq 1: observed nothing" in body, body
        assert f"{_FAMILY_JOBS[0]} status.completionTime ready_after_hours 168: observed nothing, not one RFC 3339 timestamp" in body, body
        assert f"not ready: {_FAMILY_JOBS[1]} status.completionTime ready_after_hours 168" in body, body


class CostWorkloadRolesTest(_Harness):
    """long-name-replaced-pod and cpu-burst-and-flat: Deployments on slot a
    whose shape the waste audit's overrequest check reads. The long-named
    Deployment replaces its one pod each day. Its role still asserts a ready
    pod: a pod that never runs must not read as healthy, and a scan in the
    seconds of the replacement is one drifted scan that the next one clears."""

    LONG_NAME = "long-name-replaced-pod"
    CPU = "cpu-burst-and-flat"

    def test_a_pod_that_the_controller_holds_and_that_never_runs_is_drift(self):
        # A pull that fails or a script that does not start: the controller
        # counts the pod, and it is never ready. Without the readiness
        # assertion this reads as healthy, the audit has no pod to read, and
        # each case passes on each build.
        world = _healthy_world()
        world["kubectl"][_LONG_NAME_DEPLOYMENT]["status"] = {"replicas": 1, "unavailableReplicas": 1}
        done = self.run_script(world)
        assert set(self.drift_files()) == {self.LONG_NAME}, done.stderr
        assert f"{_LONG_NAME_DEPLOYMENT} status.readyReplicas eq 1: observed nothing" in self.drift_files()[self.LONG_NAME], self.drift_files()[self.LONG_NAME]

    def test_the_long_named_deployment_is_drift_out_of_its_shape(self):
        def no_pod(world):
            world[_LONG_NAME_DEPLOYMENT]["status"] = {}

        def two_replicas(world):
            world[_LONG_NAME_DEPLOYMENT]["spec"]["replicas"] = 2

        def other_account(world):
            world[_LONG_NAME_DEPLOYMENT]["spec"]["template"]["spec"]["serviceAccountName"] = "default"

        def smaller_cpu_request(world):
            world[_LONG_NAME_DEPLOYMENT]["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["cpu"] = "50m"

        def smaller_memory_request(world):
            world[_LONG_NAME_DEPLOYMENT]["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["memory"] = "64Mi"

        def lower_cpu_limit(world):
            world[_LONG_NAME_DEPLOYMENT]["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["cpu"] = "10m"

        def no_delete_verb(world):
            world[_LONG_NAME_ROLE]["rules"][0]["verbs"] = ["get"]

        def a_second_verb(world):
            world[_LONG_NAME_ROLE]["rules"][0]["verbs"] = ["delete", "list"]

        def another_resource(world):
            world[_LONG_NAME_ROLE]["rules"][0]["resources"] = ["configmaps"]

        def a_second_resource(world):
            world[_LONG_NAME_ROLE]["rules"][0]["resources"] = ["pods", "secrets"]

        for defect, line in (
            (no_pod, f"{_LONG_NAME_DEPLOYMENT} status.readyReplicas eq 1: observed nothing"),
            (two_replicas, f"{_LONG_NAME_DEPLOYMENT} spec.replicas eq 1: observed 2"),
            (other_account, f'{_LONG_NAME_DEPLOYMENT} spec.template.spec.serviceAccountName eq "long-name-pod-cycle": observed "default"'),
            (smaller_cpu_request, f'{_LONG_NAME_DEPLOYMENT} spec.template.spec.containers[0].resources.requests.cpu eq "100m": observed "50m"'),
            (smaller_memory_request, f'{_LONG_NAME_DEPLOYMENT} spec.template.spec.containers[0].resources.requests.memory eq "128Mi": observed "64Mi"'),
            (lower_cpu_limit, f'{_LONG_NAME_DEPLOYMENT} spec.template.spec.containers[0].resources.limits.cpu eq "200m": observed "10m"'),
            (no_delete_verb, f'{_LONG_NAME_ROLE} rules[0].verbs[*] eq "delete": observed "get"'),
            (a_second_verb, f'{_LONG_NAME_ROLE} rules[0].verbs[*] eq "delete": observed ["delete", "list"]'),
            (another_resource, f'{_LONG_NAME_ROLE} rules[0].resources[*] eq "pods": observed "configmaps"'),
            (a_second_resource, f'{_LONG_NAME_ROLE} rules[0].resources[*] eq "pods": observed ["pods", "secrets"]'),
        ):
            with self.subTest(defect=defect.__name__):
                world = _healthy_world()
                defect(world["kubectl"])
                done = self.run_script(world)
                assert set(self.drift_files()) == {self.LONG_NAME}, done.stderr
                assert line in self.drift_files()[self.LONG_NAME], self.drift_files()[self.LONG_NAME]

    def test_each_cpu_deployment_is_drift_out_of_its_shape(self):
        def container(world, subject):
            return world[subject]["spec"]["template"]["spec"]["containers"][0]

        def not_ready(world, subject):
            world[subject]["status"] = {"replicas": 1, "unavailableReplicas": 1}

        def no_cpu_limit(world, subject):
            del container(world, subject)["resources"]["limits"]["cpu"]

        def guaranteed(world, subject):
            container(world, subject)["resources"]["limits"]["memory"] = "64Mi"

        def larger_memory_request(world, subject):
            container(world, subject)["resources"]["requests"]["memory"] = "128Mi"

        def other_mode(world, subject):
            container(world, subject)["env"][0]["value"] = "sometimes"

        for subject, mode, cpu in _CPU_CYCLE:
            for defect, line in (
                (not_ready, f"{subject} status.readyReplicas eq 1: observed nothing"),
                (no_cpu_limit, f'{subject} spec.template.spec.containers[0].resources.limits.cpu eq "{cpu}": observed nothing'),
                (guaranteed, f'{subject} spec.template.spec.containers[0].resources.limits.memory eq "128Mi": observed "64Mi"'),
                (larger_memory_request, f'{subject} spec.template.spec.containers[0].resources.requests.memory eq "64Mi": observed "128Mi"'),
                (other_mode, f"{subject} spec.template.spec.containers[0].env[?(@.name=='CPU_MODE')].value eq \"{mode}\": observed \"sometimes\""),
            ):
                with self.subTest(subject=subject, defect=defect.__name__):
                    world = _healthy_world()
                    defect(world["kubectl"], subject)
                    done = self.run_script(world)
                    assert set(self.drift_files()) == {self.CPU}, done.stderr
                    assert line in self.drift_files()[self.CPU], self.drift_files()[self.CPU]

    def test_the_two_cpu_deployments_do_not_have_each_others_request(self):
        entries = self.catalog["roles"][self.CPU]["state"]
        requests = {e["subject"]: e["value"] for e in entries if e["path"].endswith("requests.cpu")}
        assert requests == {subject: cpu for subject, _, cpu in _CPU_CYCLE}, requests


class MissingReservationComputeClassRoleTest(_Harness):
    """missing-reservation-compute-class: ComputeClass reserved-ccc on slot c,
    whose first rule names a reservation that does not exist. GKE marks the
    class misconfigured, and the stockout audit files that status. Auto-creation
    is on, so the role also holds that no node carries the class label."""

    ROLE = "missing-reservation-compute-class"

    def test_the_class_with_its_status_and_no_node_converges(self):
        done = self.run_script(_healthy_world())
        assert done.returncode == 0, done.stderr
        assert self.drift_files() == {}, done.stderr
        log = self.log.read_text()
        assert "get computeclass reserved-ccc -o json" in log, log
        assert "get node -l cloud.google.com/compute-class=reserved-ccc -o json" in log, log

    def test_the_class_is_drift_out_of_its_shape(self):
        def rule(world, index):
            return world[_RESERVED_CCC]["spec"]["priorities"][index]

        def condition(world):
            return world[_RESERVED_CCC]["status"]["conditions"][0]

        def any_reservation(world):
            rule(world, 0)["reservations"]["affinity"] = "AnyBestEffort"

        def a_real_reservation(world):
            rule(world, 0)["reservations"]["specific"][0]["name"] = "team-batch-reservation"

        def a_second_reservation(world):
            rule(world, 0)["reservations"]["specific"].append({"name": "team-batch-reservation"})

        def a_first_rule_of_the_fallback_size(world):
            rule(world, 0)["machineType"] = "n2-standard-4"

        def no_fallback(world):
            del world[_RESERVED_CCC]["spec"]["priorities"][1]

        def a_family_fallback(world):
            world[_RESERVED_CCC]["spec"]["priorities"][1] = {"machineFamily": "n2"}

        def auto_creation_off(world):
            world[_RESERVED_CCC]["spec"]["nodePoolAutoCreation"]["enabled"] = False

        def scale_up_anyway(world):
            world[_RESERVED_CCC]["spec"]["whenUnsatisfiable"] = "ScaleUpAnyway"

        def taints(world):
            return world[_RESERVED_CCC]["spec"]["nodePoolConfig"]["taints"]

        def no_taint(world):
            del world[_RESERVED_CCC]["spec"]["nodePoolConfig"]

        def a_soft_taint(world):
            taints(world)[0]["effect"] = "PreferNoSchedule"

        def a_second_taint(world):
            taints(world).append({"key": "team", "value": "batch", "effect": "NoSchedule"})

        def a_controller_taint_key(world):
            taints(world)[0]["key"] = "ToBeDeletedByClusterAutoscaler"

        def healthy(world):
            world[_RESERVED_CCC]["status"]["conditions"] = [{"type": "Health", "status": "True", "reason": "Health"}]

        def not_misconfigured(world):
            condition(world)["status"] = "False"

        def another_reason(world):
            condition(world)["reason"] = "NapDisabledAndNoMatchingNodegroups"

        def no_status_yet(world):
            del world[_RESERVED_CCC]["status"]

        def a_node_of_the_class(world):
            world[_RESERVED_CCC_NODES] = {"items": [{"metadata": {"name": "gke-seeded-c-nap-e2-standard-4-x1"}}]}

        prefix = f"{_RESERVED_CCC} "
        for defect, line in (
            (any_reservation, prefix + 'spec.priorities[0].reservations.affinity eq "Specific": observed "AnyBestEffort"'),
            (a_real_reservation, prefix + 'spec.priorities[0].reservations.specific[*].name eq "seeded-absent-reservation": observed "team-batch-reservation"'),
            (a_second_reservation, prefix + 'spec.priorities[0].reservations.specific[*].name eq "seeded-absent-reservation": observed ["seeded-absent-reservation", "team-batch-reservation"]'),
            (a_first_rule_of_the_fallback_size, prefix + 'spec.priorities[0].machineType eq "n2-standard-2": observed "n2-standard-4"'),
            (no_fallback, prefix + 'spec.priorities[1].machineType eq "e2-standard-4": observed nothing'),
            (a_family_fallback, prefix + 'spec.priorities[1].machineType eq "e2-standard-4": observed nothing'),
            (auto_creation_off, prefix + "spec.nodePoolAutoCreation.enabled eq true: observed false"),
            (scale_up_anyway, prefix + 'spec.whenUnsatisfiable eq "DoNotScaleUp": observed "ScaleUpAnyway"'),
            (no_taint, prefix + 'spec.nodePoolConfig.taints[*].effect eq "NoSchedule": observed nothing'),
            (a_soft_taint, prefix + 'spec.nodePoolConfig.taints[*].effect eq "NoSchedule": observed "PreferNoSchedule"'),
            (a_second_taint, prefix + 'spec.nodePoolConfig.taints[*].key eq "dedicated": observed ["dedicated", "team"]'),
            (a_controller_taint_key, prefix + 'spec.nodePoolConfig.taints[*].key eq "dedicated": observed "ToBeDeletedByClusterAutoscaler"'),
            (healthy, prefix + "status.conditions[?(@.type=='CrdMisconfigured')].status eq \"True\": observed nothing"),
            (not_misconfigured, prefix + "status.conditions[?(@.type=='CrdMisconfigured')].status eq \"True\": observed \"False\""),
            (another_reason, prefix + "status.conditions[?(@.type=='CrdMisconfigured')].reason eq \"ReservationNotFound\": observed \"NapDisabledAndNoMatchingNodegroups\""),
            (no_status_yet, prefix + "status.conditions[?(@.type=='CrdMisconfigured')].status eq \"True\": observed nothing"),
            (a_node_of_the_class, f'{_RESERVED_CCC_NODES} metadata.name absent: observed "gke-seeded-c-nap-e2-standard-4-x1"'),
        ):
            with self.subTest(defect=defect.__name__):
                world = _healthy_world()
                defect(world["kubectl"])
                done = self.run_script(world)
                assert set(self.drift_files()) == {self.ROLE}, done.stderr
                assert line in self.drift_files()[self.ROLE], self.drift_files()[self.ROLE]

    def test_the_class_deleted_is_drift(self):
        world = _healthy_world()
        del world["kubectl"][_RESERVED_CCC]
        done = self.run_script(world)
        assert set(self.drift_files()) == {self.ROLE}, done.stderr
        assert f'{_RESERVED_CCC} spec.priorities[0].reservations.affinity eq "Specific": observed nothing' in self.drift_files()[self.ROLE]
        assert "60 role(s) in their designed state, 1 drifted" in done.stderr, done.stderr

    def test_the_stack_plants_the_class_that_the_role_asserts(self):
        stack = (_REPO / "bench" / "tf" / "fleet" / "defects-c.tf").read_text()
        block = re.search(r'^resource "kubernetes_manifest" "reserved_ccc" \{\n(.*?)^\}$', stack, re.S | re.M)
        assert block, "no kubernetes_manifest.reserved_ccc in the slot c stack"
        # One space for each run of white space, so a `tofu fmt` realign changes nothing here.
        text = " ".join(block.group(1).split())
        # The whole spec: exactly two rules in this order, no `project` under
        # `specific`, no machine family, the one taint and no other field.
        # Each of those gives another finding or another status.
        spec = (
            'spec = { priorities = [ { machineType = "n2-standard-2" reservations = { affinity = "Specific" '
            'specific = [{ name = "seeded-absent-reservation" }] } }, { machineType = "e2-standard-4" }, ] '
            'nodePoolAutoCreation = { enabled = true } '
            'nodePoolConfig = { taints = [{ key = "dedicated", value = "reserved-ccc", effect = "NoSchedule" }] } '
            'whenUnsatisfiable = "DoNotScaleUp" }'
        )
        for needle in (
            "provider = kubernetes.seeded_c",
            'apiVersion = "cloud.google.com/v1"',
            'kind = "ComputeClass"',
            'metadata = { name = "reserved-ccc" labels = local.fleet_labels }',
            spec,
            "depends_on = [google_container_node_pool.seeded_c_default]",
        ):
            assert needle in text, needle

    def test_the_role_probes_the_class(self):
        # The probe decides on which projects the role is published.
        role = json.loads(_CATALOG.read_text())["roles"][self.ROLE]
        assert role["probes"] == ["computeclass/reserved-ccc"], role["probes"]

    def test_nothing_in_the_stack_asks_for_the_class(self):
        # Node pool auto-creation is on in the class: a pod that asks for it
        # and tolerates its taint makes GKE create a paid node pool on each
        # pool project. A pod asks with the class name (selector or affinity)
        # or through a default class label on its namespace. The stack has
        # neither.
        fleet = _REPO / "bench" / "tf" / "fleet"
        for path in sorted(fleet.glob("*.tf")):
            code = "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("#"))
            code = re.sub(r'^resource "kubernetes_manifest" "reserved_ccc" \{\n.*?^\}$', "", code, flags=re.S | re.M)
            with self.subTest(file=path.name):
                assert "reserved-ccc" not in code and "reserved_ccc" not in code, path.name
                assert "default-compute-class" not in code, path.name


class ReportTest(_Harness):
    """`--report` writes every catalog role's verdict as JSON, for the health
    bot's scan (scripts/eval_dashboard/fixture_state.py), which runs this
    script per project and must not parse its warnings."""

    def test_the_report_carries_every_roles_verdict(self):
        world = _healthy_world()
        world["kubectl"]["deployment/checkout-gateway"] = {"status": {"readyReplicas": 0, "replicas": 2}}
        world["describe"].pop("seeded-c")
        self.provision(roles=[role for role in self.catalog["roles"] if role != "idle-nodepool"])
        report = self.tmp / "report.json"
        done = self.run_script(world, "--report", str(report))
        self.assertEqual(done.returncode, 0, done.stderr)
        doc = json.loads(report.read_text())
        self.assertEqual(doc["schema_version"], 1)
        self.assertEqual(doc["project"], "kube-agents-evals")
        states = {role: entry["state"] for role, entry in doc["roles"].items()}
        self.assertEqual(
            states,
            {
                "cgroup-blind-runtime": "converged",
                "compute-class-affinity-workload": "converged",
                "compute-class-toleration-workload": "converged",
                "declared-single-zone-pool": "converged",
                "manual-pools-compute-class": "converged",
                "missing-reservation-compute-class": "converged",
                "missing-reservation-pool": "converged",
                "no-ephemeral-limit-workload": "converged",
                "no-nodelocal-dnscache": "unchecked",
                "untainted-compute-class-pool": "unchecked",
                "untargeted-compute-class-workload": "converged",
                "config-connector-namespaced": "converged",
                "containerd-socket-agent": "converged",
                "crashloop-workload": "converged",
                "cuda-pinned-gpu-job": "converged",
                "declared-no-pdb-workload": "converged",
                "declared-overrequest-workload": "converged",
                "generated-name-family": "converged",
                "generated-name-job-family": "converged",
                "long-name-replaced-pod": "converged",
                "cpu-burst-and-flat": "converged",
                "guaranteed-qos-workload": "converged",
                "declared-token-workload": "converged",
                "default-pool-headroom": "converged",
                "deprecated-api-caller": "converged",
                "deprecated-label-selector": "converged",
                "drift-outlier": "unchecked",
                "drift-pool-outlier": "unchecked",
                "drift-peer-a": "converged",
                "drift-peer-b": "converged",
                "drift-peer-d": "converged",
                "hpa-saturated": "converged",
                "idle-nodepool": "unpublished",
                "intree-pd-volume": "converged",
                "moving-admission-default": "converged",
                "multi-process-container": "converged",
                "no-pdb-workload": "drifted",
                "node-image-coupled-agent": "converged",
                "node-local-state": "converged",
                "rbac-overgrant": "converged",
                "readiness-drain-blocked": "converged",
                "readiness-failclosed-webhook": "converged",
                "readiness-crd-webhook": "converged",
                "readiness-lb-drain-timeout": "converged",
                "readiness-pinned-workload": "converged",
                "readiness-surge-blocked": "converged",
                "retired-registry-image": "converged",
                "scaled-zero-gateway": "converged",
                "stale-client-skew": "converged",
                "stalled-controller": "converged",
                "state-sync-controller": "converged",
                "startup-fail-vm": "converged",
                "rotated-console-vm": "converged",
                "declared-orphan-snapshot": "converged",
                "orphan-snapshot": "converged",
                "version-laggard": "converged",
                "zonal-skew-capacity": "converged",
                "zonal-skew-scheduling": "converged",
                "zonal-skew-volume": "converged",
                "world-open-ingress": "converged",
                "declared-mtu-peering": "converged",
            },
        )
        self.assertEqual(doc["roles"]["no-pdb-workload"]["detail"], self.drift_files()["no-pdb-workload"].splitlines())
        self.assertTrue(doc["roles"]["drift-outlier"]["detail"][0].startswith("cluster: clusters describe seeded-c failed"), doc["roles"]["drift-outlier"])
        self.assertEqual(doc["roles"]["idle-nodepool"], {"cluster_slot": "a", "state": "unpublished", "detail": []})
        self.assertEqual(doc["roles"]["version-laggard"]["cluster_slot"], "b")
        self.assertEqual(doc["summary"], {"converged": 55, "drifted": 1, "unchecked": 4, "not_ready": 0})
        self.assertEqual(doc["roles"]["startup-fail-vm"]["cluster_slot"], None)
        self.assertIn("1 drifted, 4 not checked", done.stderr)

    def test_without_the_flag_no_report_is_written(self):
        done = self.run_script(_healthy_world())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(list(self.tmp.glob("*.json")), [self.world])


if __name__ == "__main__":
    unittest.main()
