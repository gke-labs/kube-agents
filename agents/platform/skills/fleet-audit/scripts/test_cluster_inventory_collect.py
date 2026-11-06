#!/usr/bin/env python3
"""Tests for cluster_inventory_collect.py, the sweep's single-cluster inventory audit.

The `describe` fixture is a trimmed copy of a Standard cluster's
`gcloud container clusters describe --format json`; the dumps are the paged
reader's `List` pages. Every object is run through the hand-off's own
validators (`bootstrap_handoff._named`, `_malformed`, `finding_lines`), the
code that decides whether a `clusters` entry reaches the raw report."""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
os.environ.pop("KUBEAGENTS_SCOPE_DECLARED", None)  # module-level, on purpose
os.environ.pop("CREDENTIAL_PROXY_URL", None)  # module-level, on purpose
os.environ.pop("CREDENTIAL_PROXY_EXEC_URL", None)  # module-level, on purpose
import cluster_inventory_collect as cic  # noqa: E402
import collect  # noqa: E402
import collector_run  # noqa: E402

HANDOFF_DIR = Path(__file__).resolve().parents[4] / "chat" / "scripts"
sys.path.insert(0, str(HANDOFF_DIR))
import bootstrap_handoff  # noqa: E402

PROJECT, LOCATION, CLUSTER = "demo-proj", "us-central1-a", "seeded-c"

DESCRIBE = {
    "name": CLUSTER,
    "location": LOCATION,
    "currentMasterVersion": "1.34.1-gke.1829001",
    "workloadIdentityConfig": {"workloadPool": "demo-proj.svc.id.goog"},
    "networkConfig": {"datapathProvider": "LEGACY_DATAPATH"},
    "shieldedNodes": {"enabled": False},
    "monitoringConfig": {"managedPrometheusConfig": {"enabled": True}},
    "nodePools": [
        {
            "name": "default-pool",
            "config": {"machineType": "e2-standard-4"},
            "initialNodeCount": 3,
            "autoscaling": {"enabled": True, "minNodeCount": 1, "maxNodeCount": 5},
            "management": {"autoUpgrade": False},
        }
    ],
}


def _workload(kind, ns, name, *, startup=True, ro=True, add=(), env=(), labels=None, annotations=None):
    container = {
        "name": "app",
        "securityContext": {"readOnlyRootFilesystem": ro, "capabilities": {"add": list(add)}},
        "env": [{"name": n, "value": "x"} for n in env],
    }
    if startup:
        container["startupProbe"] = {"httpGet": {"path": "/", "port": 8080}}
    item = {
        "kind": kind,
        "apiVersion": "apps/v1" if kind != "Job" else "batch/v1",
        "metadata": {"namespace": ns, "name": name, "labels": labels or {}, "annotations": annotations or {}},
        "spec": {"replicas": 2, "template": {"metadata": {"annotations": {}}, "spec": {"containers": [container]}}},
        "status": {"readyReplicas": 2},
    }
    if kind == "Job":
        item["spec"] = {"completions": 1, "template": item["spec"]["template"]}
        item["status"] = {"succeeded": 1}
    if kind == "DaemonSet":
        item["status"] = {"numberReady": 3, "desiredNumberScheduled": 3}
    return item


WORKLOADS = {
    "deployments": [
        _workload("Deployment", "shop", "checkout", startup=False, ro=False),
        _workload("Deployment", "shop", "cart", add=["NET_ADMIN", "SYS_ADMIN"], env=["GOOGLE_APPLICATION_CREDENTIALS"]),
        _workload("Deployment", "kube-system", "kube-dns", startup=False, ro=False),
        _workload("Deployment", "otel", "otel-collector", labels={"app.kubernetes.io/managed-by": "Helm"}),
    ],
    "statefulsets": [_workload("StatefulSet", "shop", "db", env=["OTEL_EXPORTER_OTLP_ENDPOINT"])],
    "daemonsets": [_workload("DaemonSet", "gke-managed-system", "node-agent", startup=False)],
    "jobs": [_workload("Job", "shop", "migrate")],
}
GOVERNANCE = {
    "namespaces": [
        {"kind": "Namespace", "metadata": {"name": n}} for n in ("shop", "otel", "kube-system", "gke-managed-system")
    ],
    "resourcequotas": [{"kind": "ResourceQuota", "metadata": {"namespace": "shop", "name": "q"}}],
    "limitranges": [],
}


def _page(items):
    return json.dumps({"items": items, "metadata": {}})


class FakeRun:
    """Answers the collector's argv like the credential proxy would."""

    def __init__(self, *, credentials_rc=0, describe=DESCRIBE, fail_kinds=()):
        self.credentials_rc = credentials_rc
        self.describe = describe
        self.fail_kinds = set(fail_kinds)
        self.calls = []
        self.timeouts = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs.get("env", {}).get("KUBECONFIG")))
        self.timeouts.append(kwargs.get("timeout"))
        if argv[:4] == ["gcloud", "container", "clusters", "get-credentials"]:
            return collector_run.Run(argv, self.credentials_rc, "", "denied" if self.credentials_rc else "", 0.1)
        if argv[:4] == ["gcloud", "container", "clusters", "describe"]:
            if self.describe is None:
                return collector_run.Run(argv, 1, "", "PERMISSION_DENIED", 0.1)
            return collector_run.Run(argv, 0, json.dumps(self.describe), "", 0.1)
        if argv[:3] == ["kubectl", "get", "--raw"]:
            path = argv[3].split("?")[0]
            kind = path.rsplit("/", 1)[-1]
            if kind in self.fail_kinds:
                return collector_run.Run(argv, 1, "", "Forbidden", 0.1)
            items = WORKLOADS.get(kind, GOVERNANCE.get(kind, []))
            return collector_run.Run(argv, 0, _page(items), "", 0.1)
        raise AssertionError(f"unexpected command {argv}")


def _collect(**fake_kwargs):
    fake = FakeRun(**fake_kwargs)
    with TemporaryDirectory() as tmp, patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), redirect_stderr(io.StringIO()):
        meta = cic.collect_cluster_inventory(PROJECT, LOCATION, CLUSTER, run=fake)
    return meta, fake


def _checks(meta, slug):
    return [f for f in meta["findings"] if f["check"] == slug]


class MetadataShape(unittest.TestCase):
    def test_object_passes_the_handoff_validators(self):
        meta, fake = _collect()
        self.assertTrue(bootstrap_handoff._named(meta))
        self.assertEqual(bootstrap_handoff._malformed(meta), [])
        self.assertEqual(bootstrap_handoff._gap_list(meta), [])
        lines = bootstrap_handoff.finding_lines(meta)
        self.assertEqual(len(lines), len(meta["findings"]))
        for line in lines:
            self.assertEqual((line["project"], line["cluster"]), (PROJECT, CLUSTER))
        # Every kubectl page ran under the per-cluster kubeconfig, never the sandbox's own context.
        kubeconfigs = {kc for argv, kc in fake.calls if argv[0] == "kubectl"}
        self.assertEqual(len(kubeconfigs), 1)
        self.assertIn(f"kubeconfig_{PROJECT}_{CLUSTER}_{LOCATION}", next(iter(kubeconfigs)))
        json.dumps(meta)

    def test_every_workload_is_listed_with_the_sop_fields(self):
        meta, _ = _collect()
        names = sorted((w["kind"], w["namespace"], w["name"]) for w in meta["workloads"])
        self.assertEqual(len(names), 7)
        self.assertIn(("Job", "shop", "migrate"), names)
        self.assertIn(("Deployment", "kube-system", "kube-dns"), names)
        by_name = {w["name"]: w for w in meta["workloads"]}
        self.assertEqual(by_name["checkout"]["replicas"], "2/2")
        self.assertEqual(by_name["migrate"]["replicas"], "1/1")
        self.assertEqual(by_name["node-agent"]["replicas"], "3/3")
        self.assertFalse(by_name["checkout"]["probes"]["startup"])
        self.assertFalse(by_name["checkout"]["security_context"]["read_only_root_fs"])
        self.assertTrue(by_name["cart"]["probes"]["startup"])
        self.assertEqual(by_name["cart"]["security_context"]["added_capabilities"], ["NET_ADMIN", "SYS_ADMIN"])
        self.assertEqual(by_name["db"]["telemetry"], "otlp")
        self.assertEqual(by_name["checkout"]["telemetry"], "none")

    def test_findings_carry_the_sop_slugs_and_skip_provider_namespaces(self):
        meta, _ = _collect()
        self.assertEqual([f["workload"] for f in _checks(meta, "probes-startup")], ["checkout"])
        self.assertEqual([f["workload"] for f in _checks(meta, "readonly-root-fs")], ["checkout"])
        # SYS_ADMIN is the first-run audits' privileged check; NET_ADMIN alone is reported.
        added = _checks(meta, "added-capabilities")
        self.assertEqual([f["workload"] for f in added], ["cart"])
        self.assertIn("NET_ADMIN", added[0]["issue"])
        self.assertNotIn("SYS_ADMIN", added[0]["issue"])
        self.assertEqual(added[0]["severity"], "high")
        self.assertEqual([f["workload"] for f in _checks(meta, "static-sa-key")], ["cart"])
        workloads_with_findings = {f["workload"] for f in meta["findings"] if f["workload"]}
        self.assertNotIn("kube-dns", workloads_with_findings)
        self.assertNotIn("node-agent", workloads_with_findings)
        for finding in meta["findings"]:
            self.assertIn(finding["area"], ("security", "reliability", "observability"))
            self.assertIn(finding["severity"], ("high", "medium", "low"))

    def test_namespace_governance_rows_and_quota_finding(self):
        meta, _ = _collect()
        rows = {r["namespace"]: r for r in meta["namespace_governance"]}
        self.assertEqual(sorted(rows), ["otel", "shop"])
        self.assertEqual(rows["shop"], {"namespace": "shop", "resource_quota": True, "limit_range": False})
        quota = {f["namespace"]: f for f in _checks(meta, "no-resourcequota")}
        self.assertEqual(sorted(quota), ["otel", "shop"])
        self.assertEqual(quota["shop"]["workload"], "")
        self.assertIn("LimitRange", quota["shop"]["issue"])
        # A namespace-scoped finding is filed against its namespace by the hand-off.
        line = [ln for ln in bootstrap_handoff.finding_lines(meta) if ln["check"] == "no-resourcequota" and ln["namespace"] == "shop"][0]
        self.assertEqual(line["object"], "shop")

    def test_topology_and_cluster_findings_from_describe(self):
        meta, _ = _collect()
        topo = meta["topology"]
        self.assertEqual(topo["k8s_version"], "1.34.1-gke.1829001")
        self.assertEqual(topo["node_pools"], [{"name": "default-pool", "machine_type": "e2-standard-4", "nodes": 3, "autoscaling": "1-5"}])
        self.assertTrue(topo["workload_identity"])
        self.assertFalse(topo["dataplane_v2"])
        self.assertEqual(topo["observability"], "self-hosted")
        self.assertEqual(topo["deployment_toolchain"], "Helm releases")
        self.assertEqual(len(_checks(meta, "shielded-nodes")), 1)
        self.assertEqual(len(_checks(meta, "datapath-provider")), 1)
        self.assertEqual(len(_checks(meta, "no-autoupgrade")), 1)
        self.assertEqual(_checks(meta, "managed-prometheus"), [])
        self.assertEqual(_checks(meta, "no-otel-collector"), [])
        self.assertEqual(bootstrap_handoff._node_pools(topo), "default-pool (e2-standard-4)")

    def test_autopilot_skips_the_standard_only_checks(self):
        describe = dict(DESCRIBE, autopilot={"enabled": True}, shieldedNodes={"enabled": False})
        meta, _ = _collect(describe=describe)
        self.assertEqual(_checks(meta, "shielded-nodes"), [])
        self.assertEqual(_checks(meta, "no-autoupgrade"), [])
        self.assertEqual(len(_checks(meta, "datapath-provider")), 1)


class Gaps(unittest.TestCase):
    def test_unreachable_cluster_is_named_with_its_gap(self):
        meta, fake = _collect(credentials_rc=1)
        self.assertTrue(bootstrap_handoff._named(meta))
        self.assertEqual(meta["workloads"], [])
        self.assertEqual(meta["namespace_governance"], [])
        self.assertEqual(len(meta["gaps"]), 1)
        self.assertIn("get-credentials", meta["gaps"][0])
        self.assertIn("denied", meta["gaps"][0])
        self.assertFalse(any(argv[0] == "kubectl" for argv, _ in fake.calls))
        # The description still answered, so the topology is there for the fleet table.
        self.assertEqual(meta["topology"]["k8s_version"], "1.34.1-gke.1829001")
        # Nothing was read from the cluster itself: no workload verdict, no finding about one.
        self.assertIsNone(meta["topology"]["observability"])
        self.assertIsNone(meta["topology"]["deployment_toolchain"])
        self.assertEqual(_checks(meta, "no-otel-collector"), [])
        self.assertEqual({f["check"] for f in meta["findings"]}, {"shielded-nodes", "datapath-provider", "no-autoupgrade"})

    def test_a_failed_describe_leaves_the_settings_unknown_not_off(self):
        meta, _ = _collect(describe=None)
        topo = meta["topology"]
        self.assertIsNone(topo["k8s_version"])
        self.assertIsNone(topo["workload_identity"])
        self.assertIsNone(topo["dataplane_v2"])
        self.assertEqual(topo["node_pools"], [])
        self.assertEqual(_checks(meta, "datapath-provider"), [])
        self.assertEqual(_checks(meta, "shielded-nodes"), [])
        self.assertEqual(bootstrap_handoff._cell(topo["dataplane_v2"]), "n/a")

    def test_a_failed_workload_dump_raises_no_collector_finding(self):
        meta, _ = _collect(fail_kinds=("deployments",))
        self.assertIsNone(meta["topology"]["observability"])
        # The namespaces were read and name no toolchain; "none observed" would be a
        # verdict on the workload dump, which was not read.
        self.assertIsNone(meta["topology"]["deployment_toolchain"])
        self.assertEqual(_checks(meta, "no-otel-collector"), [])
        self.assertEqual(_checks(meta, "probes-startup"), [])
        self.assertEqual(len(meta["namespace_governance"]), 2)

    def test_the_read_budget_bounds_the_run(self):
        # A clock that is already past the budget: no read runs, every read is
        # the budget's gap, nothing hangs.
        fake = FakeRun()
        ticks = iter([0.0] + [1000.0] * 50)
        budget = cic._Budget(seconds=cic.READ_BUDGET_SECONDS, clock=lambda: next(ticks))
        with TemporaryDirectory() as tmp, patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), redirect_stderr(io.StringIO()):
            meta = cic.collect_cluster_inventory(PROJECT, LOCATION, CLUSTER, run=fake, budget=budget)
        self.assertEqual(meta["workloads"], [])
        self.assertEqual([g for g in meta["gaps"] if "read budget" in g].__len__(), 2)
        self.assertEqual(fake.calls, [])
        self.assertEqual(cic.READ_BUDGET_SECONDS < cic.TERMINAL_TIMEOUT_SECONDS, True)

    def test_each_read_gets_the_time_the_budget_has_left(self):
        # A clock at 0, then 450 s into a 480 s budget: the first reads get the
        # 60 s cap, the later ones the 30 s left, and after the deadline none runs.
        fake = FakeRun()
        ticks = iter([0.0, 0.0, 0.0, 450.0, 450.0, 450.0] + [1000.0] * 50)
        budget = cic._Budget(seconds=480, clock=lambda: next(ticks))
        with TemporaryDirectory() as tmp, patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), redirect_stderr(io.StringIO()):
            cic.collect_cluster_inventory(PROJECT, LOCATION, CLUSTER, run=fake, budget=budget)
        self.assertEqual(fake.timeouts[:2], [60, 60])
        self.assertIn(30, fake.timeouts)
        self.assertNotIn(1, fake.timeouts)

    def test_a_page_asked_for_after_the_budget_is_spent_is_the_budgets_gap(self):
        # The budget runs out inside the workload dump: the next page is not
        # asked for, and its gap names the budget, not a one-second timeout.
        fake = FakeRun()
        ticks = iter([0.0, 0.0, 0.0, 450.0, 450.0, 450.0] + [1000.0] * 50)
        budget = cic._Budget(seconds=480, clock=lambda: next(ticks))
        with TemporaryDirectory() as tmp, patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), redirect_stderr(io.StringIO()):
            meta = cic.collect_cluster_inventory(PROJECT, LOCATION, CLUSTER, run=fake, budget=budget)
        budget_gaps = [g for g in meta["gaps"] if "read budget" in g]
        self.assertTrue(budget_gaps, meta["gaps"])
        self.assertTrue(any(f"rc={cic.TIMEOUT_RC}" in g and "did not run" in g for g in budget_gaps), budget_gaps)
        self.assertEqual(len([argv for argv, _ in fake.calls if argv[0] == "kubectl"]), 1)

    def test_a_failed_page_gap_carries_no_continue_token(self):
        gap = cic._gap("Step 3", ["kubectl", "get", "--raw", "/apis/apps/v1/deployments?limit=500&continue=eyJ2IjoibWV0YSJ9"],
                       collector_run.Run(["kubectl"], 1, "", "boom", 0.1))
        self.assertNotIn("eyJ2", gap)
        self.assertIn("deployments", gap)

    def test_failed_dump_is_a_gap_not_a_clean_cluster(self):
        meta, _ = _collect(fail_kinds=("deployments",))
        self.assertEqual(meta["workloads"], [])
        self.assertEqual(len(meta["gaps"]), 1)
        self.assertIn("Step 3", meta["gaps"][0])
        self.assertIn("Forbidden", meta["gaps"][0])
        # The governance dump is independent and still read.
        self.assertEqual(len(meta["namespace_governance"]), 2)

    def test_failed_describe_is_a_gap_and_the_workloads_still_list(self):
        meta, _ = _collect(describe=None)
        self.assertEqual(len(meta["gaps"]), 1)
        self.assertIn("Step 2", meta["gaps"][0])
        self.assertIsNone(meta["topology"]["k8s_version"])
        self.assertEqual(len(meta["workloads"]), 7)
        self.assertEqual(_checks(meta, "shielded-nodes"), [])


    def test_failed_governance_dump_is_a_gap_and_the_workloads_still_list(self):
        meta, _ = _collect(fail_kinds=("namespaces",))
        self.assertEqual(meta["namespace_governance"], [])
        self.assertEqual(_checks(meta, "no-resourcequota"), [])
        self.assertEqual(len(meta["workloads"]), 7)
        self.assertEqual(len(meta["gaps"]), 1)
        self.assertIn("Step 3", meta["gaps"][0])
        self.assertIn("namespaces", meta["gaps"][0])

    def test_describe_that_is_not_an_object_is_a_gap(self):
        meta, _ = _collect(describe=["not", "an", "object"])
        self.assertEqual(len(meta["gaps"]), 1)
        self.assertIn("not a JSON object", meta["gaps"][0])
        self.assertIsNone(meta["topology"]["k8s_version"])
        self.assertEqual(len(meta["workloads"]), 7)


class ClusterLevelSlugs(unittest.TestCase):
    def test_managed_prometheus_off_and_no_collector_raise_their_slugs(self):
        describe = dict(DESCRIBE, monitoringConfig={"managedPrometheusConfig": {"enabled": False}})
        no_otel = {k: [w for w in v if "otel" not in w["metadata"]["name"]] for k, v in WORKLOADS.items()}
        fake = FakeRun(describe=describe)
        with TemporaryDirectory() as tmp, patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), patch.dict(WORKLOADS, no_otel), redirect_stderr(io.StringIO()):
            meta = cic.collect_cluster_inventory(PROJECT, LOCATION, CLUSTER, run=fake)
        self.assertEqual(meta["topology"]["observability"], "none")
        self.assertEqual(len(_checks(meta, "managed-prometheus")), 1)
        self.assertEqual(len(_checks(meta, "no-otel-collector")), 1)
        for f in _checks(meta, "managed-prometheus") + _checks(meta, "no-otel-collector"):
            self.assertEqual((f["area"], f["namespace"], f["workload"]), ("observability", "", ""))

    def test_object_names_its_project_location_and_cluster(self):
        meta, _ = _collect()
        self.assertEqual((meta["project"], meta["location"], meta["cluster"]), (PROJECT, LOCATION, CLUSTER))


class SweepFile(unittest.TestCase):
    def test_the_path_format_is_the_hand_offs(self):
        self.assertEqual(cic.SWEEP_DIR, bootstrap_handoff.SWEEP_DIR)
        self.assertEqual(cic.SWEEP_FILE_FORMAT, bootstrap_handoff.SWEEP_FILE_FORMAT)
        self.assertEqual(cic.SWEEP_FILE_STAMP_AT, bootstrap_handoff.SWEEP_FILE_STAMP_AT)
        self.assertEqual(cic.SWEEP_FILE_STAMP_SWEEP, bootstrap_handoff.SWEEP_FILE_STAMP_SWEEP)
        self.assertEqual(
            str(cic.sweep_file_path(PROJECT, LOCATION, CLUSTER)), bootstrap_handoff.sweep_file_path(PROJECT, LOCATION, CLUSTER)
        )

    def test_main_writes_the_stamped_object_for_the_hand_off(self):
        fake = FakeRun()
        with TemporaryDirectory() as tmp, patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), patch.object(cic, "default_run", fake), \
                patch.dict(os.environ, {cic.SWEEP_CARD_ENV: "t_sweep1"}), redirect_stderr(io.StringIO()):
            sweep_dir = Path(tmp) / "INVENTORY.sweep"
            rc = cic.main(["--project", PROJECT, "--location", LOCATION, "--cluster", CLUSTER, "--sweep-dir", str(sweep_dir)])
            written = json.loads((sweep_dir / f"{PROJECT}__{LOCATION}__{CLUSTER}.json").read_text(encoding="utf-8"))
            leftovers = [p.name for p in sweep_dir.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(rc, 0)
        self.assertEqual(leftovers, [])
        self.assertEqual(written["sweep_id"], "t_sweep1")
        self.assertIsInstance(written["collected_at"], int)
        self.assertEqual(len(written["workloads"]), 7)
        self.assertTrue(bootstrap_handoff._named(written))
        self.assertEqual(bootstrap_handoff._malformed(written), [])
        # The hand-off takes it as this sweep's audit.
        obj, why = bootstrap_handoff._collected_audit(
            json.dumps(written).encode(), {"project": PROJECT, "location": LOCATION, "cluster": CLUSTER}, "t_sweep1", written["collected_at"] - 5
        )
        self.assertEqual(why, "")
        self.assertEqual(obj["cluster"], CLUSTER)

    def test_sweep_id_comes_from_the_flag_or_the_card_environment(self):
        fake = FakeRun()
        with TemporaryDirectory() as tmp, patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), patch.object(cic, "default_run", fake), \
                patch.dict(os.environ, {cic.SWEEP_CARD_ENV: "t_env"}), redirect_stderr(io.StringIO()):
            sweep_dir = Path(tmp) / "sweep"
            cic.main(["--project", PROJECT, "--location", LOCATION, "--cluster", CLUSTER, "--sweep-dir", str(sweep_dir), "--sweep-id", "t_flag"])
            written = json.loads((sweep_dir / f"{PROJECT}__{LOCATION}__{CLUSTER}.json").read_text(encoding="utf-8"))
        self.assertEqual(written["sweep_id"], "t_flag")
        # Neither the flag nor the environment: argparse refuses before any read.
        fake2 = FakeRun()
        with TemporaryDirectory() as tmp, patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), patch.object(cic, "default_run", fake2), \
                patch.dict(os.environ, {cic.SWEEP_CARD_ENV: ""}), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cic.main(["--project", PROJECT, "--location", LOCATION, "--cluster", CLUSTER, "--sweep-dir", str(Path(tmp) / "sweep")])
        self.assertEqual(fake2.calls, [])

    def test_an_identity_that_is_not_a_bare_name_is_refused_before_any_read(self):
        fake = FakeRun()
        with patch.object(cic, "default_run", fake), redirect_stderr(io.StringIO()):
            # --sweep-id is given, so the only refusal left is the bare-name guard's.
            for argv in (["--project", "p/q", "--location", LOCATION, "--cluster", CLUSTER, "--sweep-id", "t_sweep1"],
                         ["--project", PROJECT, "--location", LOCATION, "--cluster", "../x", "--sweep-id", "t_sweep1"]):
                with self.subTest(argv=argv), self.assertRaises(SystemExit):
                    cic.main(argv)
        self.assertEqual(fake.calls, [])
        self.assertEqual(cic.IDENTITY_COMPONENT_RE.pattern, bootstrap_handoff.IDENTITY_COMPONENT_RE.pattern)

    def test_an_unwritable_sweep_dir_is_a_gap_and_the_object_still_prints(self):
        fake = FakeRun()
        with TemporaryDirectory() as tmp, patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), patch.object(cic, "default_run", fake), \
                redirect_stderr(io.StringIO()):
            blocked = Path(tmp) / "blocked"
            blocked.write_text("a file, not a directory")
            out = Path(tmp) / "meta.json"
            rc = cic.main(["--project", PROJECT, "--location", LOCATION, "--cluster", CLUSTER, "--sweep-dir", str(blocked), "--out", str(out), "--sweep-id", "t_sweep1"])
            meta = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(rc, 0)
        self.assertTrue(any("could not write" in g for g in meta["gaps"]))


class Cli(unittest.TestCase):
    def test_main_writes_the_object_to_out_and_exits_zero(self):
        fake = FakeRun()
        with TemporaryDirectory() as tmp, patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), patch.object(cic, "default_run", fake), redirect_stderr(io.StringIO()) as err:
            out = Path(tmp) / "meta.json"
            rc = cic.main(["--project", PROJECT, "--location", LOCATION, "--cluster", CLUSTER, "--out", str(out), "--sweep-dir", str(Path(tmp) / "sweep"), "--sweep-id", "t_sweep1"])
            self.assertEqual(rc, 0)
            meta = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(meta["cluster"], CLUSTER)
        self.assertIn("7 workload(s)", err.getvalue())


    def test_main_exits_zero_when_the_cluster_is_unreachable(self):
        fake = FakeRun(credentials_rc=1)
        with TemporaryDirectory() as tmp, patch.object(collect, "KUBECONFIG_DIR", Path(tmp)), patch.object(cic, "default_run", fake), \
                patch.dict(os.environ, {cic.SWEEP_CARD_ENV: "t_sweep1"}), redirect_stderr(io.StringIO()) as err:
            out = Path(tmp) / "meta.json"
            rc = cic.main(["--project", PROJECT, "--location", LOCATION, "--cluster", CLUSTER, "--out", str(out), "--sweep-dir", str(Path(tmp) / "sweep"), "--sweep-id", "t_sweep1"])
            meta = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(rc, 0)
        self.assertEqual(len(meta["gaps"]), 1)
        self.assertIn("1 gap(s)", err.getvalue())


if __name__ == "__main__":
    unittest.main()
