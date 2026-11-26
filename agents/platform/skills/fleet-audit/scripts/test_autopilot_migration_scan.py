#!/usr/bin/env python3
"""Unit tests for `autopilot_migration_scan.py`."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import autopilot_migration_scan as scan  # noqa: E402
import collect  # noqa: E402


def _make_cluster(
    *,
    name: str = "seeded-a",
    location: str = "us-central1-a",
    status: str = "RUNNING",
    version: str = "1.34.1-gke.1200000",
    channel: str = "REGULAR",
    use_ip_aliases: bool | None = True,
    autopilot: bool = False,
) -> dict:
    record: dict = {
        "name": name,
        "location": location,
        "status": status,
        "currentMasterVersion": version,
        "releaseChannel": {"channel": channel},
        "autopilot": {"enabled": autopilot},
    }
    if use_ip_aliases is not None:
        record["ipAllocationPolicy"] = {"useIpAliases": use_ip_aliases}
    return record


def _make_deployment(
    name: str,
    namespace: str = "default",
    *,
    containers: list[dict] | None = None,
    init_containers: list[dict] | None = None,
    pod_spec_extra: dict | None = None,
) -> dict:
    pod_spec: dict = {
        "containers": containers
        if containers is not None
        else [
            {
                "name": "app",
                "resources": {
                    "requests": {"cpu": "250m", "memory": "256Mi"},
                    "limits": {"cpu": "250m", "memory": "256Mi"},
                },
            }
        ],
    }
    if init_containers is not None:
        pod_spec["initContainers"] = init_containers
    if pod_spec_extra:
        pod_spec.update(pod_spec_extra)
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"template": {"spec": pod_spec}},
    }


class ClusterRuleTests(unittest.TestCase):
    def test_system_namespaces_match_collect(self) -> None:
        self.assertEqual(scan.SYSTEM_NAMESPACES, collect.SYSTEM_NAMESPACES)
        for ns in scan.SYSTEM_NAMESPACES:
            self.assertTrue(scan.is_system_namespace(ns), ns)
        self.assertTrue(scan.is_system_namespace("gke-managed-cim"))
        self.assertTrue(scan.is_system_namespace("config-management-system"))
        self.assertFalse(scan.is_system_namespace("seeded-shapes"))
        self.assertFalse(scan.is_system_namespace("seeded-qos"))

    def test_already_autopilot_cluster(self) -> None:
        result = scan.evaluate_cluster(
            _make_cluster(name="robot-host", location="us-central1", autopilot=True)
        )
        self.assertTrue(result["autopilot"])
        self.assertEqual(result["status"], scan.CLUSTER_STATUS_AUTOPILOT)
        self.assertEqual(result["blockers"], [])
        self.assertIn(scan.NOTE_ALREADY_AUTOPILOT, result["notes"])

    def test_standard_cluster_ready(self) -> None:
        result = scan.evaluate_cluster(_make_cluster())
        self.assertFalse(result["autopilot"])
        self.assertEqual(result["status"], scan.CLUSTER_STATUS_READY)
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["notes"], [])

    def test_routes_based_networking_blocked(self) -> None:
        for ip_aliases in (False, None):
            with self.subTest(use_ip_aliases=ip_aliases):
                result = scan.evaluate_cluster(_make_cluster(use_ip_aliases=ip_aliases))
                self.assertEqual(result["status"], scan.CLUSTER_STATUS_BLOCKED)
                rules = [b["rule"] for b in result["blockers"]]
                self.assertIn(scan.RULE_ROUTES_BASED_NETWORKING, rules)

    def test_extended_release_channel_blocked(self) -> None:
        result = scan.evaluate_cluster(_make_cluster(channel="EXTENDED"))
        self.assertEqual(result["status"], scan.CLUSTER_STATUS_BLOCKED)
        rules = [b["rule"] for b in result["blockers"]]
        self.assertIn(scan.RULE_EXTENDED_RELEASE_CHANNEL, rules)

    def test_control_plane_version_floor(self) -> None:
        below = scan.evaluate_cluster(_make_cluster(version="1.33.1-gke.1106999"))
        self.assertEqual(below["status"], scan.CLUSTER_STATUS_BLOCKED)
        self.assertIn(
            scan.RULE_CONTROL_PLANE_VERSION_FLOOR,
            [b["rule"] for b in below["blockers"]],
        )

        at_floor = scan.evaluate_cluster(_make_cluster(version="1.33.1-gke.1107000"))
        self.assertEqual(at_floor["status"], scan.CLUSTER_STATUS_READY)
        self.assertEqual(at_floor["blockers"], [])

        unparsable = scan.evaluate_cluster(_make_cluster(version=""))
        self.assertEqual(unparsable["status"], scan.CLUSTER_STATUS_UNKNOWN)
        self.assertIn(scan.NOTE_VERSION_UNKNOWN, unparsable["notes"])


class WorkloadBlockerRuleTests(unittest.TestCase):
    def test_privileged_containers_and_init_containers(self) -> None:
        dep = _make_deployment(
            "priv-app",
            containers=[
                {"name": "main", "securityContext": {"privileged": True}},
            ],
            init_containers=[
                {"name": "init- root", "securityContext": {"privileged": True}},
            ],
        )
        res = scan.evaluate_workload(dep)
        self.assertEqual(res["status"], scan.WORKLOAD_STATUS_BLOCKED)
        rules = {b["rule"]: b["detail"] for b in res["blockers"]}
        self.assertIn(scan.RULE_PRIVILEGED_CONTAINER, rules)
        self.assertIn("main", rules[scan.RULE_PRIVILEGED_CONTAINER])
        self.assertIn("init- root", rules[scan.RULE_PRIVILEGED_CONTAINER])

    def test_host_namespaces_and_host_path_and_sysctls(self) -> None:
        dep = _make_deployment(
            "node-coupled",
            pod_spec_extra={
                "hostNetwork": True,
                "hostPID": True,
                "hostIPC": True,
                "volumes": [
                    {"name": "cni-conf", "hostPath": {"path": "/etc/cni/net.d"}},
                ],
                "securityContext": {
                    "sysctls": [
                        {"name": "net.core.somaxconn", "value": "4096"},
                    ]
                },
            },
        )
        res = scan.evaluate_workload(dep)
        self.assertEqual(res["status"], scan.WORKLOAD_STATUS_BLOCKED)
        rules = {b["rule"]: b["detail"] for b in res["blockers"]}
        self.assertIn(scan.RULE_HOST_NETWORK, rules)
        self.assertIn(scan.RULE_HOST_PID, rules)
        self.assertIn(scan.RULE_HOST_IPC, rules)
        self.assertIn(scan.RULE_HOST_PATH, rules)
        self.assertIn("/etc/cni/net.d", rules[scan.RULE_HOST_PATH])
        self.assertIn(scan.RULE_SYSCTLS, rules)
        self.assertIn("net.core.somaxconn=4096", rules[scan.RULE_SYSCTLS])

    def test_user_daemonset_and_standalone_pod_flagged(self) -> None:
        ds = {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {"name": "cni-shaped-agent", "namespace": "seeded-shapes"},
            "spec": {
                "template": {
                    "spec": {
                        "hostNetwork": True,
                        "volumes": [
                            {"name": "cni-conf", "hostPath": {"path": "/etc/cni/net.d"}}
                        ],
                        "containers": [{"name": "agent"}],
                    }
                }
            },
        }
        ds_eval = scan.evaluate_workload(ds)
        self.assertEqual(ds_eval["status"], scan.WORKLOAD_STATUS_BLOCKED)
        ds_rules = [b["rule"] for b in ds_eval["blockers"]]
        self.assertIn(scan.RULE_DAEMONSET, ds_rules)
        self.assertIn(scan.RULE_HOST_NETWORK, ds_rules)
        self.assertIn(scan.RULE_HOST_PATH, ds_rules)

        standalone_pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "ad-hoc-debug", "namespace": "default"},
            "spec": {"containers": [{"name": "shell"}]},
            "status": {"phase": "Running"},
        }
        pod_eval = scan.evaluate_workload(standalone_pod)
        self.assertEqual(pod_eval["status"], scan.WORKLOAD_STATUS_BLOCKED)
        self.assertIn(
            scan.RULE_STANDALONE_POD,
            [b["rule"] for b in pod_eval["blockers"]],
        )

    def test_extract_workloads_filters_system_owned_mirror_and_terminal_pods(self) -> None:
        items = [
            _make_deployment("sys-dep", namespace="kube-system"),
            _make_deployment("cnrm-dep", namespace="cnrm-system"),
            _make_deployment("gke-dep", namespace="gke-managed-system"),
            _make_deployment("user-dep", namespace="app"),
            {
                "apiVersion": "batch/v1",
                "kind": "CronJob",
                "metadata": {"name": "nightly", "namespace": "app"},
                "spec": {
                    "jobTemplate": {
                        "spec": {
                            "template": {
                                "spec": {"containers": [{"name": "job"}]}
                            }
                        }
                    }
                },
            },
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {
                    "name": "nightly-123",
                    "namespace": "app",
                    "ownerReferences": [{"kind": "CronJob", "name": "nightly"}],
                },
                "spec": {"template": {"spec": {"containers": [{"name": "job"}]}}},
            },
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": "one-off-migration", "namespace": "app"},
                "spec": {"template": {"spec": {"containers": [{"name": "job"}]}}},
            },
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": "user-dep-abc",
                    "namespace": "app",
                    "ownerReferences": [{"kind": "ReplicaSet", "name": "user-dep-rs"}],
                },
                "spec": {"containers": [{"name": "app"}]},
                "status": {"phase": "Running"},
            },
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": "mirror-pod",
                    "namespace": "app",
                    "annotations": {"kubernetes.io/config.mirror": "hash"},
                },
                "spec": {"containers": [{"name": "mirror"}]},
                "status": {"phase": "Running"},
            },
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": "completed-pod", "namespace": "app"},
                "spec": {"containers": [{"name": "done"}]},
                "status": {"phase": "Succeeded"},
            },
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": "orphan-pod", "namespace": "app"},
                "spec": {"containers": [{"name": "orphan"}]},
                "status": {"phase": "Running"},
            },
        ]
        extracted = scan.extract_workloads(items)
        keys = [(w["kind"], w["metadata"]["name"]) for w in extracted]
        self.assertEqual(
            keys,
            [
                ("CronJob", "nightly"),
                ("Deployment", "user-dep"),
                ("Job", "one-off-migration"),
                ("Pod", "orphan-pod"),
            ],
        )


class ResourceImpactTests(unittest.TestCase):
    def test_cpu_rounding_formula(self) -> None:
        self.assertEqual(scan.round_autopilot_cpu_millis(0), 250)
        self.assertEqual(scan.round_autopilot_cpu_millis(10), 250)
        self.assertEqual(scan.round_autopilot_cpu_millis(250), 250)
        self.assertEqual(scan.round_autopilot_cpu_millis(251), 500)
        self.assertEqual(scan.round_autopilot_cpu_millis(300), 500)
        self.assertEqual(scan.round_autopilot_cpu_millis(1000), 1000)
        self.assertEqual(scan.round_autopilot_cpu_millis(1200), 1250)

    def test_guaranteed_workload_with_cpu_rounding(self) -> None:
        # Matches `seeded-qos/ledger-writer`: 10m CPU, 32Mi memory, requests == limits.
        dep = _make_deployment(
            "ledger-writer",
            namespace="seeded-qos",
            containers=[
                {
                    "name": "writer",
                    "resources": {
                        "requests": {"cpu": "10m", "memory": "32Mi"},
                        "limits": {"cpu": "10m", "memory": "32Mi"},
                    },
                }
            ],
        )
        evaluated = scan.evaluate_workload(dep)
        self.assertEqual(evaluated["status"], scan.WORKLOAD_STATUS_READY)
        res = evaluated["resources"]
        self.assertEqual(res["requested_cpu_millis"], 10)
        self.assertEqual(res["autopilot_cpu_millis"], 250)
        self.assertEqual(res["cpu_delta_millis"], 240)
        self.assertTrue(res["cpu_rounded"])
        self.assertEqual(res["qos_class"], scan.QOS_GUARANTEED)
        self.assertFalse(res["burst_impacted"])
        self.assertIn("10m -> 250m (+240m)", res["cpu_rounding_summary"])
        self.assertIn("Guaranteed", res["burst_summary"])

    def test_burstable_and_best_effort_workloads(self) -> None:
        burstable = _make_deployment(
            "cpu-burst",
            namespace="seeded-waste",
            containers=[
                {
                    "name": "worker",
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "64Mi"},
                        "limits": {"cpu": "500m", "memory": "128Mi"},
                    },
                }
            ],
        )
        b_res = scan.evaluate_workload(burstable)["resources"]
        self.assertEqual(b_res["qos_class"], scan.QOS_BURSTABLE)
        self.assertTrue(b_res["burst_impacted"])
        self.assertIn("Burstable", b_res["burst_summary"])
        self.assertIn("cpu limit 500m > request 50m", b_res["burst_summary"])
        self.assertIn("memory limit 128Mi > request 64Mi", b_res["burst_summary"])

        best_effort = _make_deployment(
            "unbounded",
            containers=[{"name": "app"}],
        )
        be_res = scan.evaluate_workload(best_effort)["resources"]
        self.assertEqual(be_res["qos_class"], scan.QOS_BEST_EFFORT)
        self.assertTrue(be_res["burst_impacted"])
        self.assertEqual(be_res["requested_cpu_millis"], 0)
        self.assertEqual(be_res["autopilot_cpu_millis"], 250)


class EndToEndCliTests(unittest.TestCase):
    def _fake_runner(
        self,
        *,
        clusters: list[dict],
        raw_pages_by_path: dict[str, dict] | None = None,
        fail_raw_path: str | None = None,
        fail_get_credentials: bool = False,
    ):
        pages = raw_pages_by_path or {}

        def run(argv: list[str], **_kwargs) -> scan.Run:
            if argv[:4] == ["gcloud", "container", "clusters", "list"]:
                return scan.Run(argv, 0, json.dumps(clusters), "", 0.01)
            if argv[:4] == ["gcloud", "container", "clusters", "get-credentials"]:
                if fail_get_credentials:
                    return scan.Run(argv, 1, "", "ERROR: permission denied", 0.01)
                return scan.Run(argv, 0, "", "", 0.01)
            if argv[:3] == ["kubectl", "get", "--raw"]:
                raw_url = argv[3]
                path = raw_url.split("?", 1)[0]
                if fail_raw_path and path == fail_raw_path:
                    return scan.Run(argv, 1, "", "Error from server: forbidden", 0.01)
                payload = pages.get(path, {"apiVersion": "v1", "kind": "List", "items": []})
                return scan.Run(argv, 0, json.dumps(payload), "", 0.01)
            return scan.Run(argv, 1, "", f"unexpected command: {argv}", 0.01)

        return run

    def test_cli_scan_markdown_and_json_output(self) -> None:
        clusters = [
            _make_cluster(name="robot-host", location="us-central1", autopilot=True),
            _make_cluster(name="seeded-a", location="us-central1-a", autopilot=False),
        ]
        pages = {
            "/apis/apps/v1/daemonsets": {
                "apiVersion": "v1",
                "kind": "List",
                "items": [
                    {
                        "metadata": {"name": "cni-shaped-agent", "namespace": "seeded-shapes"},
                        "spec": {
                            "template": {
                                "spec": {
                                    "hostNetwork": True,
                                    "volumes": [
                                        {
                                            "name": "cni-conf",
                                            "hostPath": {"path": "/etc/cni/net.d"},
                                        }
                                    ],
                                    "containers": [
                                        {
                                            "name": "agent",
                                            "resources": {
                                                "requests": {"cpu": "10m", "memory": "16Mi"},
                                                "limits": {"memory": "32Mi"},
                                            },
                                        }
                                    ],
                                }
                            }
                        },
                    }
                ],
            },
            "/apis/apps/v1/deployments": {
                "apiVersion": "v1",
                "kind": "List",
                "items": [
                    _make_deployment(
                        "ledger-writer",
                        namespace="seeded-qos",
                        containers=[
                            {
                                "name": "writer",
                                "resources": {
                                    "requests": {"cpu": "10m", "memory": "32Mi"},
                                    "limits": {"cpu": "10m", "memory": "32Mi"},
                                },
                            }
                        ],
                    ),
                    _make_deployment("cnrm-controller", namespace="cnrm-system"),
                ],
            },
        }

        runner = self._fake_runner(clusters=clusters, raw_pages_by_path=pages)
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "report.json"
            kc_dir = Path(tmp) / "kubeconfigs"
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                rc = scan.main(
                    [
                        "--project",
                        "kube-agents-robot",
                        "--kubeconfig-dir",
                        str(kc_dir),
                        "--output",
                        str(out_path),
                    ],
                    run=runner,
                )
            self.assertEqual(rc, scan.EXIT_OK)
            markdown = buf.getvalue()
            self.assertIn("robot-host", markdown)
            self.assertIn("seeded-a", markdown)
            self.assertIn("DaemonSet/cni-shaped-agent", markdown)
            self.assertIn("Deployment/ledger-writer", markdown)
            self.assertNotIn("cnrm-controller", markdown)

            saved = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["errors"], [])
            by_name = {c["name"]: c for c in saved["clusters"]}
            self.assertEqual(by_name["robot-host"]["status"], scan.CLUSTER_STATUS_AUTOPILOT)
            self.assertEqual(by_name["seeded-a"]["status"], scan.CLUSTER_STATUS_READY)
            self.assertEqual(
                by_name["seeded-a"]["workload_summary"],
                {
                    "total": 2,
                    "ready": 1,
                    "blocked": 1,
                    "cpu_rounded": 2,
                    "burst_impacted": 1,
                },
            )

    def test_paged_dump_failure_fails_closed(self) -> None:
        clusters = [_make_cluster(name="seeded-a", location="us-central1-a")]
        runner = self._fake_runner(
            clusters=clusters,
            fail_raw_path="/apis/apps/v1/pods"
            if False
            else "/api/v1/pods",
        )
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "report.json"
            kc_dir = Path(tmp) / "kubeconfigs"
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                rc = scan.main(
                    [
                        "--project",
                        "kube-agents-robot",
                        "--kubeconfig-dir",
                        str(kc_dir),
                        "--output",
                        str(out_path),
                    ],
                    run=runner,
                )
            self.assertEqual(rc, scan.EXIT_ERROR)
            saved = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(len(saved["errors"]), 1)
            self.assertEqual(saved["clusters"][0]["status"], scan.CLUSTER_STATUS_UNKNOWN)
            self.assertIn("forbidden", saved["clusters"][0]["read_error"])

    def test_single_kubeconfig_with_multiple_clusters_fails_closed(self) -> None:
        clusters = [
            _make_cluster(name="seeded-a", location="us-central1-a"),
            _make_cluster(name="seeded-b", location="us-central1-b"),
        ]
        runner = self._fake_runner(clusters=clusters)
        with tempfile.TemporaryDirectory() as tmp:
            kc_file = Path(tmp) / "single.kubeconfig"
            kc_file.write_text("apiVersion: v1\n", encoding="utf-8")
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                rc = scan.main(
                    [
                        "--project",
                        "kube-agents-robot",
                        "--kubeconfig",
                        str(kc_file),
                    ],
                    run=runner,
                )
            self.assertEqual(rc, scan.EXIT_ERROR)
            self.assertIn(scan.SINGLE_KUBECONFIG_MULTI_CLUSTER_ERROR, buf.getvalue())

    def test_scope_flags_and_cluster_filter_and_non_running_status(self) -> None:
        clusters = [
            _make_cluster(name="seeded-a", location="us-central1-a", status="PROVISIONING"),
            _make_cluster(name="seeded-b", location="us-central1-b", status="RUNNING"),
        ]
        runner = self._fake_runner(clusters=clusters)
        with tempfile.TemporaryDirectory() as tmp:
            kc_dir = Path(tmp) / "kubeconfigs"
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                rc = scan.main(
                    [
                        "--scope-projects",
                        "kube-agents-robot",
                        "--scope-exclude-clusters",
                        "kube-agents-robot/us-central1-b/seeded-b",
                        "--kubeconfig-dir",
                        str(kc_dir),
                    ],
                    run=runner,
                )
            self.assertEqual(rc, scan.EXIT_ERROR)
            output = buf.getvalue()
            self.assertIn("seeded-a", output)
            self.assertNotIn("seeded-b", output)
            self.assertIn("PROVISIONING", output)

    def test_get_credentials_failure_and_missing_cluster_filter(self) -> None:
        clusters = [_make_cluster(name="seeded-a", location="us-central1-a")]
        runner = self._fake_runner(clusters=clusters, fail_get_credentials=True)
        with tempfile.TemporaryDirectory() as tmp:
            kc_dir = Path(tmp) / "kubeconfigs"
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                rc = scan.main(
                    [
                        "--project",
                        "kube-agents-robot",
                        "--cluster",
                        "us-central1-a/seeded-a",
                        "--kubeconfig-dir",
                        str(kc_dir),
                    ],
                    run=runner,
                )
            self.assertEqual(rc, scan.EXIT_ERROR)
            self.assertIn("permission denied", buf.getvalue())

            buf_missing = io.StringIO()
            with mock.patch("sys.stdout", buf_missing):
                rc_missing = scan.main(
                    [
                        "--project",
                        "kube-agents-robot",
                        "--cluster",
                        "us-central1-a/nonexistent",
                        "--kubeconfig-dir",
                        str(kc_dir),
                    ],
                    run=runner,
                )
            self.assertEqual(rc_missing, scan.EXIT_ERROR)
            self.assertIn("no cluster matching", buf_missing.getvalue())


if __name__ == "__main__":
    unittest.main()
