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


def _pod(*, restarts: int, last_reason: str | None, phase: str = "Running") -> dict:
    status = {"restartCount": restarts, "lastState": {}}
    if last_reason:
        status["lastState"] = {"terminated": {"reason": last_reason, "exitCode": 137}}
    return {"status": {"phase": phase, "containerStatuses": [status]}}


def _pods(*pods: dict) -> dict:
    return {"items": list(pods)}


def _node(*, ready: str = "True", taint: str | None = "idle-batch") -> dict:
    node = {"spec": {"taints": []}, "status": {"conditions": [{"type": "Ready", "status": ready}]}}
    if taint:
        node["spec"]["taints"] = [{"key": "seeded-role", "value": taint, "effect": "NoSchedule"}]
    return node


def _future(days: int = 30) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cluster_b(*, master: str = "1.33.4-gke.1134000", channel: str | None = "REGULAR", end=None, scope="NO_MINOR_UPGRADES") -> dict:
    doc = {
        "currentMasterVersion": master,
        # The no-surge pool as `clusters describe` reports it; the
        # readiness-surge-blocked role asserts on maxUnavailable here.
        "nodePools": [
            {"name": "default-pool", "upgradeSettings": {"maxSurge": 1}},
            {"name": "no-surge-pool", "upgradeSettings": {"maxUnavailable": 1}},
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
            "poddisruptionbudget?": {"items": []},
            "deployment/inference-server": {"status": {"readyReplicas": 1, "replicas": 3}},
            "pod?app=inference-server": _pods(
                _pod(restarts=0, last_reason=None), _pod(restarts=0, last_reason=None, phase="Pending")
            ),
            "clusterrolebinding/debug-binding": {
                "roleRef": {"name": "cluster-admin"},
                "subjects": [{"kind": "ServiceAccount", "name": "default", "namespace": "seeded-security"}],
            },
            "node?cloud.google.com/gke-nodepool=idle-batch-pool": {"items": [_node()]},
            # seeded-b's readiness pair and the fail-closed webhook. The
            # no-surge pool's node must be Ready or the pinned workload is
            # Pending for a reason that is not the fixture, which is the
            # whole point of asserting on the pair rather than on the pool's
            # upgrade settings alone.
            "node?cloud.google.com/gke-nodepool=no-surge-pool": {"items": [_node(taint=None)]},
            "namespace/seeded-upgrade": {"metadata": {"name": "seeded-upgrade"}},
            "deployment/pinned-batch-runner": {
                "spec": {"template": {"spec": {"nodeSelector": {"seeded-role": "no-surge"}}}},
                "status": {"readyReplicas": 1, "replicas": 1},
            },
            "pod?app=pinned-batch-runner": _pods(_pod(restarts=0, last_reason=None)),
            "validatingwebhookconfiguration/seeded-fail-closed-gate": {
                "webhooks": [{"name": "gate.seeded.invalid", "failurePolicy": "Fail", "timeoutSeconds": 30}]
            },
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

    def test_malformed_paths_are_repository_bugs(self):
        for bad in ("", "a..b", "a.", "a[?(@.x=y)]", "a b", "a[*"):
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

    def test_a_failure_says_what_was_observed(self):
        problem = ffs.evaluate(self._entry("eq", 2, path="status.readyReplicas"), [{"status": {"readyReplicas": 0}}])
        assert problem == "deployment/x status.readyReplicas eq 2: observed 0"
        problem = ffs.evaluate(self._entry("any_eq", "OOMKilled", path="status.containerStatuses[*].lastState.terminated.reason"), [{"status": {"containerStatuses": [{"lastState": {}}]}}])
        assert problem.endswith("observed nothing")


class PassTest(_Harness):
    def test_a_healthy_fleet_converges_on_the_first_pass(self):
        done = self.run_script(_healthy_world())
        assert done.returncode == 0, done.stderr
        assert "Seeded-fleet fixture state: 10 role(s) in their designed state, 0 drifted, 0 not checked (project kube-agents-evals)" in done.stderr
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
        assert "10 role(s) in their designed state, 0 drifted" in done.stderr
        assert self.drift_files() == {}
        # Only the pending role is re-read; converged roles are not asked again.
        assert self.log.read_text().count("get deployment checkout-gateway") == 1
        assert self.log.read_text().count("get pod") >= 3

    def test_drift_is_recorded_only_at_the_deadline(self):
        world = _healthy_world()
        world["kubectl"]["pod?app=payments-api"] = _pods(_pod(restarts=0, last_reason=None, phase="Pending"))
        # Long enough for a second pass on a slow machine (a pass is seven
        # roles through two stub interpreters), short enough not to matter.
        done = self.run_script(world, "--wait", "4", "--interval", "0.1")
        assert done.returncode == 0
        assert set(self.drift_files()) == {"crashloop-workload"}
        assert self.log.read_text().count("get pod -l app=payments-api") >= 2

    def test_an_unreadable_subject_is_not_checked_and_leaves_no_drift_file(self):
        world = _healthy_world()
        world["kubectl"]["deployment/checkout-gateway"] = "UNREACHABLE"
        done = self.run_script(world)
        assert done.returncode == 0, done.stderr
        assert "9 role(s) in their designed state, 0 drifted, 1 not checked" in done.stderr
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
        assert "10 role(s) in their designed state, 0 drifted, 0 not checked" in done.stderr

    def test_a_read_failure_beside_a_failed_assertion_is_still_drift(self):
        world = _healthy_world()
        world["kubectl"]["deployment/checkout-gateway"] = "UNREACHABLE"
        world["kubectl"]["poddisruptionbudget?"] = {"items": [{"metadata": {"name": "checkout-gateway"}}]}
        done = self.run_script(world)
        assert "1 drifted, 0 not checked" in done.stderr
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
        assert "10 role(s) in their designed state" in done.stderr, done.stderr
        # The label key carries a slash: the subject is a selector, not kind/name.
        assert "get node -l cloud.google.com/gke-nodepool=idle-batch-pool" in self.log.read_text()
        world["kubectl"]["node?cloud.google.com/gke-nodepool=idle-batch-pool"] = {"items": [_node(ready="False")]}
        done = self.run_script(world)
        assert "idle-nodepool" in self.drift_files()
        world["kubectl"]["node?cloud.google.com/gke-nodepool=idle-batch-pool"] = {"items": [_node(taint=None)]}
        self.provision()
        done = self.run_script(world)
        assert "spec.taints[?(@.key=='seeded-role')].value any_eq \"idle-batch\": observed nothing" in self.drift_files()["idle-nodepool"], done.stderr

    def test_the_capacity_fixture_needs_a_ready_pod_and_a_pending_surplus(self):
        world = _healthy_world()
        world["kubectl"]["pod?app=inference-server"] = _pods(_pod(restarts=0, last_reason=None))
        done = self.run_script(world)
        assert "status.phase any_eq \"Pending\": observed \"Running\"" in self.drift_files()["hpa-saturated"], done.stderr

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

    def test_an_unreadable_cluster_is_not_checked(self):
        world = _healthy_world()
        world["describe"]["seeded-b"] = "UNREACHABLE"
        done = self.run_script(world)
        assert "2 not checked" in done.stderr
        # Two, not one: readiness-surge-blocked reads the same cluster
        # document for its pool's upgrade settings, so an unreadable cluster
        # takes it out of the run rather than drifting it.
        assert "version-laggard" not in self.drift_files()
        assert "readiness-surge-blocked" not in self.drift_files()

    def test_a_context_without_the_slots_cluster_is_not_checked(self):
        (self.fleet / ".fleet-context").write_text("project=kube-agents-evals\n")
        done = self.run_script(_healthy_world())
        assert "7 role(s) in their designed state, 0 drifted, 3 not checked" in done.stderr
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
        assert "10 role(s) in their designed state" in done.stderr



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
                "crashloop-workload": "converged",
                "drift-outlier": "unchecked",
                "hpa-saturated": "converged",
                "idle-nodepool": "unpublished",
                "no-pdb-workload": "drifted",
                "rbac-overgrant": "converged",
                "readiness-failclosed-webhook": "converged",
                "readiness-pinned-workload": "converged",
                "readiness-surge-blocked": "converged",
                "version-laggard": "converged",
            },
        )
        self.assertEqual(doc["roles"]["no-pdb-workload"]["detail"], self.drift_files()["no-pdb-workload"].splitlines())
        self.assertTrue(doc["roles"]["drift-outlier"]["detail"][0].startswith("cluster: clusters describe seeded-c failed"), doc["roles"]["drift-outlier"])
        self.assertEqual(doc["roles"]["idle-nodepool"], {"cluster_slot": "a", "state": "unpublished", "detail": []})
        self.assertEqual(doc["roles"]["version-laggard"]["cluster_slot"], "b")
        self.assertEqual(doc["summary"], {"converged": 7, "drifted": 1, "unchecked": 1})
        self.assertIn("1 drifted, 1 not checked", done.stderr)

    def test_without_the_flag_no_report_is_written(self):
        done = self.run_script(_healthy_world())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(list(self.tmp.glob("*.json")), [self.world])


if __name__ == "__main__":
    unittest.main()
