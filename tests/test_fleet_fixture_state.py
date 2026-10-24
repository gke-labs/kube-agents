"""Tests for hack/fleet-fixture-state.py, the designed-state half of the fleet check.

The runner (hack/fleet-kubeconfigs.sh) confirms a fixture's objects are PRESENT;
this script confirms they are in the state the cases depend on (#1544). What
these tests pin, with ``kubectl`` and ``gcloud`` stubbed on ``PATH``:

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
* a re-run that finds a role converged removes a stale ``.drift`` file;
* a malformed catalog or a directory the runner did not write is exit 1.

The stubs answer from a JSON "world" file so each test names only its own
defect; a key whose value is a list is answered in order, once per call, which
is how a fixture that converges between passes is expressed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import stat
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone

_REPO = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "hack" / "fleet-fixture-state.py"
_CATALOG = _REPO / "bench" / "tf" / "fleet" / "fixtures.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("fleet_fixture_state", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ffs = _load_module()


_STUB_KUBECTL = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write("kubectl " + " ".join(args) + "\n")
world = json.load(open(os.environ["STUB_WORLD"]))
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
    counts = json.load(open(counter)) if os.path.exists(counter) else {}
    n = counts.get(key, 0)
    counts[key] = n + 1
    json.dump(counts, open(counter, "w"))
    value = value[min(n, len(value) - 1)]
if value == "UNREACHABLE":
    print("Unable to connect to the server: dial tcp 10.0.0.1:443: i/o timeout", file=sys.stderr)
    sys.exit(1)
print(json.dumps(value))
'''

_STUB_GCLOUD = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write("gcloud " + " ".join(args) + "\n")
world = json.load(open(os.environ["STUB_WORLD"]))
if args[:3] == ["container", "clusters", "describe"]:
    doc = world.get("describe", {}).get(args[3])
    if doc is None:
        print(f"ERROR: (gcloud.container.clusters.describe) Not found: {args[3]}", file=sys.stderr)
        sys.exit(1)
    if doc == "UNREACHABLE":
        print("ERROR: (gcloud.container.clusters.describe) ResponseError: code=503", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(doc))
elif args[:2] == ["container", "get-server-config"]:
    print(json.dumps(world.get("server_config", {})))
else:
    print("unexpected gcloud call: " + " ".join(args), file=sys.stderr)
    sys.exit(64)
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


def _future(days: int = 30) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cluster_b(*, master: str = "1.33.4-gke.1134000", channel: str | None = "REGULAR", end=None, scope="NO_MINOR_UPGRADES", surge: int | None = None) -> dict:
    no_surge = {"maxUnavailable": 1}
    if surge is not None:
        no_surge["maxSurge"] = surge
    doc = {
        "currentMasterVersion": master,
        # The pools as `clusters describe` reports them. The
        # readiness-surge-blocked role asserts the no-surge pool's
        # maxUnavailable is 1 and maxSurge is absent, because the API omits
        # maxSurge when it is 0; `surge` gives the pool the surge capacity
        # that heals the fixture. The readiness-lb-drain-timeout role asserts
        # the default pool's status, the pool its backend service fronts.
        "nodePools": [
            {"name": "default-pool", "status": "RUNNING", "upgradeSettings": {"maxSurge": 1}},
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
        "kubectl": {
            "pod?app=payments-api": _pods(_pod(restarts=3, last_reason="OOMKilled")),
            "deployment/checkout-gateway": {"status": {"readyReplicas": 2, "replicas": 2}},
            "deployment/notification-relay": {"status": {"readyReplicas": 2, "replicas": 2}},
            "deployment/token-reader": {"status": {"readyReplicas": 1, "replicas": 1}},
            "deployment/token-sidecar": {"status": {"readyReplicas": 1, "replicas": 1}},
            "serviceaccount/default": {"automountServiceAccountToken": True},
            "deployment/burst-ingest": {"status": {"readyReplicas": 2, "replicas": 2}, "spec": {"template": {"spec": {"containers": [{"resources": {"requests": {"cpu": "10m", "memory": "128Mi"}}}]}}}},
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
            # seeded-d's zonal-skew trio, each in the state its role's `state`
            # block asserts: a scheduled pod for the scheduling case, a Bound
            # claim for the volume case, a Pending pod for the capacity case.
            # Those three signals are what say the skew's cause is present,
            # not merely that the objects were created.
            "namespace/seeded-topology": {"metadata": {"name": "seeded-topology"}},
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
            "cronjob/cuda-pinned-trainer": {"spec": {"schedule": "0 3 * * *", "suspend": True, "jobTemplate": {"spec": {"template": {"spec": {"nodeSelector": {"cloud.google.com/gke-accelerator": "nvidia-l4"}, "containers": [{"name": "trainer", "image": "docker.io/nvidia/cuda:12.2.0-base-ubuntu22.04"}]}}}}}},
        },
        "describe": {
            "seeded-b": _cluster_b(),
            "seeded-c": {"currentMasterVersion": "1.34.1-gke.1"},
        },
        "server_config": {
            "channels": [
                {"channel": "RAPID", "defaultVersion": "1.35.0-gke.1"},
                {"channel": "REGULAR", "defaultVersion": "1.34.1-gke.1"},
            ]
        },
    }


class _Harness(unittest.TestCase):
    """A fleet directory the runner would have written, and the two stubs."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, body in (("kubectl", _STUB_KUBECTL), ("gcloud", _STUB_GCLOUD)):
            stub = self.bin / name
            stub.write_text(body, encoding="utf-8")
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        self.log = self.tmp / "calls.log"
        self.log.write_text("")
        self.world = self.tmp / "world.json"
        self.fleet = self.tmp / "fleet"
        self.catalog = json.loads(_CATALOG.read_text(encoding="utf-8"))
        self.provision()

    def provision(self, roles=None):
        """Lay down what hack/fleet-kubeconfigs.sh leaves: the marker, the
        context (project plus each slot's cluster and location), and one
        kubeconfig per published role."""
        if self.fleet.exists():
            import shutil

            shutil.rmtree(self.fleet)
        self.fleet.mkdir()
        (self.fleet / ".kube-agents-fleet-kubeconfigs").write_text("")
        lines = ["project=kube-agents-evals"]
        for slot in ("a", "b", "c"):
            lines += [f"cluster.{slot}=seeded-{slot}", f"location.{slot}=us-central1-a"]
        (self.fleet / ".fleet-context").write_text("\n".join(lines) + "\n")
        for role in roles if roles is not None else self.catalog["roles"]:
            (self.fleet / f"{role}.kubeconfig").write_text("apiVersion: v1\nkind: Config\n")

    def run_script(self, world: dict, *args: str, catalog: pathlib.Path = _CATALOG) -> subprocess.CompletedProcess:
        self.world.write_text(json.dumps(world))
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "STUB_LOG": str(self.log),
            "STUB_WORLD": str(self.world),
        }
        env.pop("BENCH_FLEET_KUBECONFIG_DIR", None)
        return subprocess.run(
            [sys.executable, str(_SCRIPT), "--dir", str(self.fleet), "--catalog", str(catalog), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def drift_files(self) -> dict[str, str]:
        return {p.stem: p.read_text() for p in sorted(self.fleet.glob("*.drift"))}


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
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for entry in cases:
                with self.subTest(entry=entry), self.assertRaises(ffs.CatalogError):
                    ffs.load_assertions(self._catalog_with(pathlib.Path(tmp), entry))


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
    def test_a_healthy_fleet_converges_on_the_first_pass(self):
        done = self.run_script(_healthy_world())
        assert done.returncode == 0, done.stderr
        assert "Seeded-fleet fixture state: 34 role(s) in their designed state, 0 drifted, 0 not checked (project kube-agents-evals)" in done.stderr
        assert self.drift_files() == {}
        assert "WARNING" not in done.stderr
        # One read per distinct subject, and the channel default once.
        assert self.log.read_text().count("get-server-config") == 1

    def test_a_pending_crashloop_is_drift_that_names_the_evidence(self):
        world = _healthy_world()
        world["kubectl"]["pod?app=payments-api"] = _pods(_pod(restarts=0, last_reason=None, phase="Pending"))
        done = self.run_script(world)
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
        assert "34 role(s) in their designed state, 0 drifted" in done.stderr
        assert self.drift_files() == {}
        # Only the pending role is re-read; converged roles are not asked again.
        assert self.log.read_text().count("get deployment checkout-gateway") == 1
        assert self.log.read_text().count("get pod") >= 3

    def test_drift_is_recorded_only_at_the_deadline(self):
        world = _healthy_world()
        world["kubectl"]["pod?app=payments-api"] = _pods(_pod(restarts=0, last_reason=None, phase="Pending"))
        # Long enough for a second pass on a slow machine (a pass is every
        # role through two stub interpreters), short enough not to matter.
        done = self.run_script(world, "--wait", "4", "--interval", "0.1")
        assert done.returncode == 0
        assert set(self.drift_files()) == {"crashloop-workload"}
        assert self.log.read_text().count("get pod -l app=payments-api") >= 2

    def test_an_unreadable_subject_is_not_checked_and_leaves_no_drift_file(self):
        world = _healthy_world()
        world["kubectl"]["deployment/checkout-gateway"] = "UNREACHABLE"
        done = self.run_script(world)
        assert done.returncode == 0, done.stderr
        assert "33 role(s) in their designed state, 0 drifted, 1 not checked" in done.stderr
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
        assert "34 role(s) in their designed state, 0 drifted, 0 not checked" in done.stderr

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
        assert "34 role(s) in their designed state" in done.stderr, done.stderr
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
        assert "34 role(s) in their designed state" in done.stderr, done.stderr
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
        assert "33 role(s) in their designed state, 0 drifted, 1 not checked" in done.stderr, done.stderr
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
        assert "34 role(s) in their designed state" in done.stderr, done.stderr
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
        assert "34 role(s) in their designed state" in done.stderr, done.stderr
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
        assert "34 role(s) in their designed state, 0 drifted" in done.stderr
        assert self.drift_files() == {}

    def test_the_cluster_subject_reads_the_recorded_cluster(self):
        world = _healthy_world()
        world["describe"]["seeded-c"] = {"masterAuthorizedNetworksConfig": {"enabled": True, "cidrBlocks": []}}
        done = self.run_script(world)
        assert "drift-outlier" in self.drift_files(), done.stderr
        assert "masterAuthorizedNetworksConfig.enabled none_eq true: observed true" in self.drift_files()["drift-outlier"]
        assert "clusters describe seeded-c --location us-central1-a --project kube-agents-evals" in self.log.read_text()

    def test_the_laggard_is_drift_once_the_master_catches_up(self):
        world = _healthy_world()
        world["describe"]["seeded-b"] = _cluster_b(master="1.34.1-gke.1")
        done = self.run_script(world)
        body = self.drift_files()["version-laggard"]
        assert "currentMasterVersion minor_behind_channel_default 1: observed \"1.34.1-gke.1\" against channel default 1.34.1-gke.1" in body, done.stderr

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
        assert "3 not checked" in done.stderr
        # Three, not one: readiness-surge-blocked and readiness-lb-drain-timeout
        # read the same cluster document for their pools, so an unreadable
        # cluster takes them out of the run rather than drifting them.
        assert "version-laggard" not in self.drift_files()
        assert "readiness-surge-blocked" not in self.drift_files()

    def test_a_context_without_the_slots_cluster_is_not_checked(self):
        (self.fleet / ".fleet-context").write_text("project=kube-agents-evals\n")
        done = self.run_script(_healthy_world())
        assert "30 role(s) in their designed state, 0 drifted, 4 not checked" in done.stderr
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
        done = self.run_script(_healthy_world())
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
        assert "34 role(s) in their designed state" in done.stderr

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
                "containerd-socket-agent": "converged",
                "crashloop-workload": "converged",
                "cuda-pinned-gpu-job": "converged",
                "declared-no-pdb-workload": "converged",
                "declared-overrequest-workload": "converged",
                "guaranteed-qos-workload": "converged",
                "declared-token-workload": "converged",
                "default-pool-headroom": "converged",
                "deprecated-api-caller": "converged",
                "deprecated-label-selector": "converged",
                "drift-outlier": "unchecked",
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
                "readiness-lb-drain-timeout": "converged",
                "readiness-pinned-workload": "converged",
                "readiness-surge-blocked": "converged",
                "retired-registry-image": "converged",
                "scaled-zero-gateway": "converged",
                "stale-client-skew": "converged",
                "stalled-controller": "converged",
                "version-laggard": "converged",
                "zonal-skew-capacity": "converged",
                "zonal-skew-scheduling": "converged",
                "zonal-skew-volume": "converged",
            },
        )
        self.assertEqual(doc["roles"]["no-pdb-workload"]["detail"], self.drift_files()["no-pdb-workload"].splitlines())
        self.assertTrue(doc["roles"]["drift-outlier"]["detail"][0].startswith("cluster: clusters describe seeded-c failed"), doc["roles"]["drift-outlier"])
        self.assertEqual(doc["roles"]["idle-nodepool"], {"cluster_slot": "a", "state": "unpublished", "detail": []})
        self.assertEqual(doc["roles"]["version-laggard"]["cluster_slot"], "b")
        self.assertEqual(doc["summary"], {"converged": 31, "drifted": 1, "unchecked": 1})
        self.assertIn("1 drifted, 1 not checked", done.stderr)

    def test_without_the_flag_no_report_is_written(self):
        done = self.run_script(_healthy_world())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(list(self.tmp.glob("*.json")), [self.world])


if __name__ == "__main__":
    unittest.main()
