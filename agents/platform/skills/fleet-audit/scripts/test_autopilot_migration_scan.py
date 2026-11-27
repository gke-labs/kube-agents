#!/usr/bin/env python3
"""Unit tests for `autopilot_migration_scan.py`."""

from __future__ import annotations

import io
import json
import os
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

os.environ.pop("KUBEAGENTS_SCOPE_DECLARED", None)
scan.fleet_scope_args.SCOPE_DECLARED_FILE = "/nonexistent/kube-agents-sandbox/scope-declared"


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

    def test_cluster_supports_bursting_version_and_cgroup_mode(self) -> None:
        # 1. Autopilot and Standard clusters at 1.30.2-gke.1394000+ on cgroupv2 support bursting.
        self.assertTrue(
            scan.cluster_supports_bursting(
                _make_cluster(version="1.30.2-gke.1394000", autopilot=True)
            )
        )
        self.assertTrue(
            scan.cluster_supports_bursting(
                _make_cluster(version="1.33.1-gke.1107000", autopilot=False)
            )
        )
        # 2. Clusters below 1.30.2-gke.1394000 do not support Autopilot bursting.
        self.assertFalse(
            scan.cluster_supports_bursting(
                _make_cluster(version="1.30.2-gke.1393999", autopilot=True)
            )
        )
        self.assertFalse(
            scan.cluster_supports_bursting(
                _make_cluster(version="1.29.8-gke.1000000", autopilot=False)
            )
        )
        # 3. Clusters with CGROUP_MODE_V1 on nodeConfig or nodePools do not support bursting.
        cluster_v1 = _make_cluster(version="1.35.8-gke.1796000", autopilot=True)
        cluster_v1["nodeConfig"] = {"linuxNodeConfig": {"cgroupMode": "CGROUP_MODE_V1"}}
        self.assertFalse(scan.cluster_supports_bursting(cluster_v1))

        pool_v1 = _make_cluster(version="1.35.8-gke.1796000", autopilot=False)
        pool_v1["nodePools"] = [
            {"name": "default-pool", "config": {"linuxNodeConfig": {"cgroupMode": "CGROUP_MODE_V1"}}}
        ]
        self.assertFalse(scan.cluster_supports_bursting(pool_v1))


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

    def test_readonly_var_log_hostpath_allowed_and_writable_or_other_hostpath_blocked(
        self,
    ) -> None:
        # 1. Read-only hostPath on `/var/log` and `/var/log/pods` is admitted on Autopilot.
        ro_var_log = _make_deployment(
            "log-reader",
            containers=[
                {
                    "name": "reader",
                    "volumeMounts": [
                        {"name": "varlog", "mountPath": "/var/log", "readOnly": True},
                        {
                            "name": "podlogs",
                            "mountPath": "/var/log/pods",
                            "readOnly": True,
                        },
                    ],
                }
            ],
            pod_spec_extra={
                "volumes": [
                    {"name": "varlog", "hostPath": {"path": "/var/log"}},
                    {"name": "podlogs", "hostPath": {"path": "/var/log/pods"}},
                ]
            },
        )
        ro_eval = scan.evaluate_workload(ro_var_log)
        self.assertEqual(ro_eval["status"], scan.WORKLOAD_STATUS_READY)
        self.assertEqual(ro_eval["blockers"], [])

        # 2. Writable hostPath on `/var/log` (`readOnly` omitted or False) is blocked.
        rw_var_log = _make_deployment(
            "log-writer",
            containers=[
                {
                    "name": "writer",
                    "volumeMounts": [
                        {"name": "varlog", "mountPath": "/var/log", "readOnly": False}
                    ],
                }
            ],
            pod_spec_extra={
                "volumes": [
                    {"name": "varlog", "hostPath": {"path": "/var/log"}},
                ]
            },
        )
        rw_eval = scan.evaluate_workload(rw_var_log)
        self.assertEqual(rw_eval["status"], scan.WORKLOAD_STATUS_BLOCKED)
        self.assertIn(
            scan.RULE_HOST_PATH,
            [b["rule"] for b in rw_eval["blockers"]],
        )

        # 3. Non-`/var/log` path, `/var/logfoo` prefix without trailing slash, or `..` traversal mounted read-only is blocked.
        self.assertTrue(scan._is_var_log_path("/var/log"))
        self.assertTrue(scan._is_var_log_path("/var/log/pods"))
        self.assertFalse(scan._is_var_log_path("/var/logfoo"))
        self.assertFalse(scan._is_var_log_path("/var/logfoo/bar"))
        non_var_log_ro = _make_deployment(
            "etc-reader",
            containers=[
                {
                    "name": "reader",
                    "volumeMounts": [
                        {"name": "etcconf", "mountPath": "/etc", "readOnly": True},
                        {
                            "name": "logprefix",
                            "mountPath": "/var/logfoo",
                            "readOnly": True,
                        },
                        {
                            "name": "traversal",
                            "mountPath": "/var/log",
                            "readOnly": True,
                        },
                    ],
                }
            ],
            pod_spec_extra={
                "volumes": [
                    {"name": "etcconf", "hostPath": {"path": "/etc/cni/net.d"}},
                    {"name": "logprefix", "hostPath": {"path": "/var/logfoo"}},
                    {
                        "name": "traversal",
                        "hostPath": {"path": "/var/log/../etc/passwd"},
                    },
                ]
            },
        )
        non_var_log_eval = scan.evaluate_workload(non_var_log_ro)
        self.assertEqual(non_var_log_eval["status"], scan.WORKLOAD_STATUS_BLOCKED)
        host_path_detail = {
            b["rule"]: b["detail"] for b in non_var_log_eval["blockers"]
        }[scan.RULE_HOST_PATH]
        self.assertIn("etcconf (/etc/cni/net.d)", host_path_detail)
        self.assertIn("logprefix (/var/logfoo)", host_path_detail)
        self.assertIn("traversal (/var/log/../etc/passwd)", host_path_detail)

    def test_safe_sysctls_allowed_and_unsafe_sysctls_blocked(self) -> None:
        # 1. Pin the exact Kubernetes safe sysctl allowlist so adding an unsafe sysctl fails.
        expected_safe_sysctls = frozenset(
            {
                "kernel.shm_rmid_forced",
                "net.ipv4.ip_local_port_range",
                "net.ipv4.ip_local_reserved_ports",
                "net.ipv4.ip_unprivileged_port_start",
                "net.ipv4.ping_group_range",
                "net.ipv4.tcp_fin_timeout",
                "net.ipv4.tcp_keepalive_intvl",
                "net.ipv4.tcp_keepalive_probes",
                "net.ipv4.tcp_keepalive_time",
                "net.ipv4.tcp_rmem",
                "net.ipv4.tcp_syncookies",
                "net.ipv4.tcp_wmem",
            }
        )
        self.assertEqual(scan.SAFE_SYSCTLS, expected_safe_sysctls)

        # 2. Kubernetes safe sysctls schedule on Autopilot without blocking.
        safe_dep = _make_deployment(
            "safe-sysctl-app",
            pod_spec_extra={
                "securityContext": {
                    "sysctls": [
                        {
                            "name": "net.ipv4.ip_unprivileged_port_start",
                            "value": "1024",
                        },
                        {"name": "net.ipv4.ping_group_range", "value": "0 2147483647"},
                        {"name": "net.ipv4.tcp_keepalive_time", "value": "600"},
                        {"name": "kernel.shm_rmid_forced", "value": "1"},
                    ]
                }
            },
        )
        safe_eval = scan.evaluate_workload(safe_dep)
        self.assertEqual(safe_eval["status"], scan.WORKLOAD_STATUS_READY)
        self.assertEqual(safe_eval["blockers"], [])

        # 3. Mixed safe and unsafe sysctls block only on the unsafe sysctls.
        mixed_dep = _make_deployment(
            "unsafe-sysctl-app",
            pod_spec_extra={
                "securityContext": {
                    "sysctls": [
                        {
                            "name": "net.ipv4.ip_unprivileged_port_start",
                            "value": "1024",
                        },
                        {"name": "net.core.somaxconn", "value": "4096"},
                        {"name": "kernel.shmmax", "value": "65536"},
                        {"name": "kernel.msgmax", "value": "65536"},
                    ]
                }
            },
        )
        mixed_eval = scan.evaluate_workload(mixed_dep)
        self.assertEqual(mixed_eval["status"], scan.WORKLOAD_STATUS_BLOCKED)
        rules = {b["rule"]: b["detail"] for b in mixed_eval["blockers"]}
        self.assertIn(scan.RULE_SYSCTLS, rules)
        self.assertIn("net.core.somaxconn=4096", rules[scan.RULE_SYSCTLS])
        self.assertIn("kernel.shmmax=65536", rules[scan.RULE_SYSCTLS])
        self.assertIn("kernel.msgmax=65536", rules[scan.RULE_SYSCTLS])
        self.assertNotIn(
            "net.ipv4.ip_unprivileged_port_start", rules[scan.RULE_SYSCTLS]
        )

    def test_user_daemonset_and_standalone_pod_reported_as_advisories(self) -> None:
        # 1. Admissible DaemonSet (e.g. read-only /var/log reader) stays `ready` with a DaemonSet advisory.
        admissible_ds = {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {"name": "log-shipper", "namespace": "logging"},
            "spec": {
                "template": {
                    "spec": {
                        "volumes": [
                            {"name": "varlog", "hostPath": {"path": "/var/log"}}
                        ],
                        "containers": [
                            {
                                "name": "shipper",
                                "volumeMounts": [
                                    {
                                        "name": "varlog",
                                        "mountPath": "/var/log",
                                        "readOnly": True,
                                    }
                                ],
                            }
                        ],
                    }
                }
            },
        }
        admissible_ds_eval = scan.evaluate_workload(admissible_ds)
        self.assertEqual(admissible_ds_eval["status"], scan.WORKLOAD_STATUS_READY)
        self.assertEqual(admissible_ds_eval["blockers"], [])
        self.assertEqual(
            [a["rule"] for a in admissible_ds_eval["advisories"]],
            [scan.RULE_DAEMONSET],
        )

        # 2. DaemonSet with hostNetwork and non-/var/log hostPath is `blocked` by those rules and carries the DaemonSet advisory.
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
        ds_blocker_rules = [b["rule"] for b in ds_eval["blockers"]]
        self.assertNotIn(scan.RULE_DAEMONSET, ds_blocker_rules)
        self.assertIn(scan.RULE_HOST_NETWORK, ds_blocker_rules)
        self.assertIn(scan.RULE_HOST_PATH, ds_blocker_rules)
        self.assertEqual(
            [a["rule"] for a in ds_eval["advisories"]],
            [scan.RULE_DAEMONSET],
        )

        # 3. Admissible standalone Pod stays `ready` with a `standalone-pod` advisory.
        standalone_pod = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "ad-hoc-debug", "namespace": "default"},
            "spec": {"containers": [{"name": "shell"}]},
            "status": {"phase": "Running"},
        }
        pod_eval = scan.evaluate_workload(standalone_pod)
        self.assertEqual(pod_eval["status"], scan.WORKLOAD_STATUS_READY)
        self.assertEqual(pod_eval["blockers"], [])
        self.assertEqual(
            [a["rule"] for a in pod_eval["advisories"]],
            [scan.RULE_STANDALONE_POD],
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
        # Bursting target (default): 50m general-purpose minimum, no 250m step rounding.
        self.assertEqual(scan.round_autopilot_cpu_millis(0), 50)
        self.assertEqual(scan.round_autopilot_cpu_millis(10), 50)
        self.assertEqual(scan.round_autopilot_cpu_millis(50), 50)
        self.assertEqual(scan.round_autopilot_cpu_millis(251), 251)
        self.assertEqual(scan.round_autopilot_cpu_millis(300), 300)
        self.assertEqual(scan.round_autopilot_cpu_millis(1000), 1000)
        self.assertEqual(scan.round_autopilot_cpu_millis(1200), 1200)

        # Non-bursting target: 250m floor and 250m step rounding.
        self.assertEqual(scan.round_autopilot_cpu_millis(0, bursting=False), 250)
        self.assertEqual(scan.round_autopilot_cpu_millis(10, bursting=False), 250)
        self.assertEqual(scan.round_autopilot_cpu_millis(250, bursting=False), 250)
        self.assertEqual(scan.round_autopilot_cpu_millis(251, bursting=False), 500)
        self.assertEqual(scan.round_autopilot_cpu_millis(300, bursting=False), 500)
        self.assertEqual(scan.round_autopilot_cpu_millis(1000, bursting=False), 1000)
        self.assertEqual(scan.round_autopilot_cpu_millis(1200, bursting=False), 1250)

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
        # 1. Bursting target (default): 10m rounds to 50m (+40m).
        evaluated = scan.evaluate_workload(dep)
        self.assertEqual(evaluated["status"], scan.WORKLOAD_STATUS_READY)
        res = evaluated["resources"]
        self.assertTrue(res["bursting"])
        self.assertEqual(res["requested_cpu_millis"], 10)
        self.assertEqual(res["autopilot_cpu_millis"], 50)
        self.assertEqual(res["non_bursting_autopilot_cpu_millis"], 250)
        self.assertEqual(res["cpu_delta_millis"], 40)
        self.assertTrue(res["cpu_rounded"])
        self.assertEqual(res["qos_class"], scan.QOS_GUARANTEED)
        self.assertFalse(res["burst_impacted"])
        self.assertIn("10m -> 50m (+40m)", res["cpu_rounding_summary"])
        self.assertIn("Guaranteed", res["burst_summary"])

        # 2. Non-bursting target: 10m rounds to 250m (+240m).
        non_burst_res = scan.evaluate_workload(dep, bursting=False)["resources"]
        self.assertFalse(non_burst_res["bursting"])
        self.assertEqual(non_burst_res["requested_cpu_millis"], 10)
        self.assertEqual(non_burst_res["autopilot_cpu_millis"], 250)
        self.assertEqual(non_burst_res["cpu_delta_millis"], 240)
        self.assertTrue(non_burst_res["cpu_rounded"])
        self.assertIn("10m -> 250m (+240m)", non_burst_res["cpu_rounding_summary"])

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
        # 1. Bursting target (default): Burstable keeps burst (`burst_impacted == False`).
        b_res = scan.evaluate_workload(burstable)["resources"]
        self.assertEqual(b_res["qos_class"], scan.QOS_BURSTABLE)
        self.assertFalse(b_res["burst_impacted"])
        self.assertEqual(b_res["autopilot_cpu_millis"], 50)
        self.assertFalse(b_res["cpu_rounded"])
        self.assertIn("Burstable (target supports bursting", b_res["burst_summary"])
        self.assertIn("cpu limit 500m > request 50m", b_res["burst_summary"])
        self.assertIn("memory limit 128Mi > request 64Mi", b_res["burst_summary"])

        # 2. Non-bursting target: Burstable loses burst (`burst_impacted == True`).
        b_non_burst = scan.evaluate_workload(burstable, bursting=False)["resources"]
        self.assertEqual(b_non_burst["qos_class"], scan.QOS_BURSTABLE)
        self.assertTrue(b_non_burst["burst_impacted"])
        self.assertEqual(b_non_burst["autopilot_cpu_millis"], 250)
        self.assertIn(
            "non-bursting Autopilot enforces requests == limits, removing burst above request",
            b_non_burst["burst_summary"],
        )

        best_effort = _make_deployment(
            "unbounded",
            containers=[{"name": "app"}],
        )
        be_res = scan.evaluate_workload(best_effort)["resources"]
        self.assertEqual(be_res["qos_class"], scan.QOS_BEST_EFFORT)
        self.assertTrue(be_res["burst_impacted"])
        self.assertEqual(be_res["requested_cpu_millis"], 0)
        self.assertEqual(be_res["autopilot_cpu_millis"], 50)

        be_non_burst = scan.evaluate_workload(best_effort, bursting=False)["resources"]
        self.assertEqual(be_non_burst["qos_class"], scan.QOS_BEST_EFFORT)
        self.assertTrue(be_non_burst["burst_impacted"])
        self.assertEqual(be_non_burst["autopilot_cpu_millis"], 250)

    def test_quantity_scientific_notation_and_overflow_guards(self) -> None:
        self.assertEqual(scan.parse_cpu_millis("1e3m"), 1000)
        self.assertEqual(scan.parse_cpu_millis("1e-1"), 100)
        self.assertEqual(scan.parse_cpu_millis("1k"), 1_000_000)
        self.assertEqual(scan.parse_memory_bytes("5e8"), 500_000_000)
        self.assertIsNone(scan.parse_cpu_millis("1e300E"))
        self.assertIsNone(scan.parse_memory_bytes("1e300E"))
        self.assertIsNone(scan.parse_cpu_millis(10**400))
        self.assertIsNone(scan.parse_memory_bytes(10**400))
        self.assertIsNone(scan.parse_cpu_millis(float("inf")))
        self.assertIsNone(scan.parse_memory_bytes(float("nan")))

    def test_native_sidecar_and_pod_overhead_cpu_aggregation(self) -> None:
        dep = _make_deployment(
            "with-sidecar",
            containers=[
                {
                    "name": "app",
                    "resources": {
                        "requests": {"cpu": "200m", "memory": "128Mi"},
                        "limits": {"cpu": "200m", "memory": "128Mi"},
                    },
                }
            ],
            init_containers=[
                {
                    "name": "mesh-proxy",
                    "restartPolicy": "Always",
                    "resources": {
                        "requests": {"cpu": "200m", "memory": "128Mi"},
                        "limits": {"cpu": "200m", "memory": "128Mi"},
                    },
                },
                {
                    "name": "db-migrate",
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "64Mi"},
                        "limits": {"cpu": "100m", "memory": "64Mi"},
                    },
                },
            ],
            pod_spec_extra={"overhead": {"cpu": "50m"}},
        )
        # app (200m) + native sidecar (200m) = 400m > init peak (200m + 100m = 300m), plus overhead (50m) = 450m
        res = scan.evaluate_workload(dep)["resources"]
        self.assertEqual(res["requested_cpu_millis"], 450)
        self.assertEqual(res["autopilot_cpu_millis"], 450)
        self.assertEqual(res["non_bursting_autopilot_cpu_millis"], 500)
        self.assertEqual(res["cpu_delta_millis"], 0)
        self.assertEqual(res["qos_class"], scan.QOS_GUARANTEED)

        non_burst_res = scan.evaluate_workload(dep, bursting=False)["resources"]
        self.assertEqual(non_burst_res["requested_cpu_millis"], 450)
        self.assertEqual(non_burst_res["autopilot_cpu_millis"], 500)
        self.assertEqual(non_burst_res["cpu_delta_millis"], 50)

    def test_limits_only_containers_default_requests_from_limits(self) -> None:
        dep = _make_deployment(
            "limits-only",
            containers=[
                {
                    "name": "app",
                    "resources": {
                        "limits": {"cpu": "500m", "memory": "512Mi"},
                    },
                }
            ],
            init_containers=[
                {
                    "name": "sidecar",
                    "restartPolicy": "Always",
                    "resources": {
                        "limits": {"cpu": "250m", "memory": "128Mi"},
                    },
                },
                {
                    "name": "init",
                    "resources": {
                        "limits": {"cpu": "100m", "memory": "64Mi"},
                    },
                },
            ],
        )
        res = scan.evaluate_workload(dep)["resources"]
        self.assertEqual(res["requested_cpu_millis"], 750)
        self.assertEqual(res["autopilot_cpu_millis"], 750)
        self.assertEqual(res["cpu_delta_millis"], 0)
        self.assertFalse(res["cpu_rounded"])
        self.assertEqual(res["qos_class"], scan.QOS_GUARANTEED)
        self.assertFalse(res["burst_impacted"])
        self.assertEqual(res["burst_details"], [])

    def test_zero_quantity_containers_best_effort_and_burstable_details(self) -> None:
        all_zero = _make_deployment(
            "all-zero",
            containers=[
                {
                    "name": "app",
                    "resources": {
                        "requests": {"cpu": "0m", "memory": "0"},
                        "limits": {"cpu": "0m", "memory": "0"},
                    },
                }
            ],
        )
        zero_res = scan.evaluate_workload(all_zero)["resources"]
        self.assertEqual(zero_res["qos_class"], scan.QOS_BEST_EFFORT)
        self.assertTrue(zero_res["burst_impacted"])
        self.assertIn("BestEffort", zero_res["burst_summary"])

        mixed_zero = _make_deployment(
            "mixed-zero",
            containers=[
                {
                    "name": "app",
                    "resources": {
                        "requests": {"cpu": "500m", "memory": "0"},
                        "limits": {"cpu": "500m", "memory": "0"},
                    },
                }
            ],
        )
        mixed_res = scan.evaluate_workload(mixed_zero)["resources"]
        self.assertEqual(mixed_res["qos_class"], scan.QOS_BURSTABLE)
        self.assertFalse(mixed_res["burst_impacted"])
        self.assertIn("app: memory request and limit are 0", mixed_res["burst_details"])
        self.assertIn(
            "burst above request preserved: app: memory request and limit are 0)",
            mixed_res["burst_summary"],
        )

        mixed_non_burst = scan.evaluate_workload(mixed_zero, bursting=False)["resources"]
        self.assertTrue(mixed_non_burst["burst_impacted"])
        self.assertIn(
            "removing burst above request: app: memory request and limit are 0)",
            mixed_non_burst["burst_summary"],
        )


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
            self.assertTrue(by_name["seeded-a"]["bursting"])
            self.assertEqual(
                by_name["seeded-a"]["workload_summary"],
                {
                    "total": 2,
                    "ready": 1,
                    "blocked": 1,
                    "cpu_rounded": 2,
                    "burst_impacted": 0,
                },
            )

            # With `--non-bursting`, 250m rounding and Burstable burst removal apply.
            out_non_burst = Path(tmp) / "report-non-burst.json"
            buf_non_burst = io.StringIO()
            with mock.patch("sys.stdout", buf_non_burst):
                rc_nb = scan.main(
                    [
                        "--project",
                        "kube-agents-robot",
                        "--non-bursting",
                        "--kubeconfig-dir",
                        str(kc_dir),
                        "--output",
                        str(out_non_burst),
                    ],
                    run=runner,
                )
            self.assertEqual(rc_nb, scan.EXIT_OK)
            saved_nb = json.loads(out_non_burst.read_text(encoding="utf-8"))
            seeded_nb = {c["name"]: c for c in saved_nb["clusters"]}["seeded-a"]
            self.assertFalse(seeded_nb["bursting"])
            self.assertEqual(
                seeded_nb["workload_summary"],
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

    def test_unknown_master_version_sets_exit_error(self) -> None:
        clusters = [_make_cluster(name="seeded-a", location="us-central1-a", version="")]
        runner = self._fake_runner(clusters=clusters)
        with tempfile.TemporaryDirectory() as tmp:
            kc_dir = Path(tmp) / "kubeconfigs"
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                rc = scan.main(
                    [
                        "--project",
                        "kube-agents-robot",
                        "--kubeconfig-dir",
                        str(kc_dir),
                    ],
                    run=runner,
                )
            self.assertEqual(rc, scan.EXIT_ERROR)
            self.assertIn(scan.NOTE_VERSION_UNKNOWN, buf.getvalue())

    def test_list_project_clusters_quota_refusal_zone_timeout_and_empty_stdout(self) -> None:
        # 1. API disabled on the target project itself -> empty cluster list, no error.
        def run_own_disabled(argv: list[str], **_kwargs) -> scan.Run:
            return scan.Run(
                argv,
                1,
                "",
                "SERVICE_DISABLED: Kubernetes Engine API has not been used in project target-proj before",
                0.01,
            )

        clusters, err = scan.list_project_clusters("target-proj", run=run_own_disabled)
        self.assertEqual(clusters, [])
        self.assertIsNone(err)

        # 2. API disabled on a different quota project -> fails closed with error.
        def run_quota_disabled(argv: list[str], **_kwargs) -> scan.Run:
            return scan.Run(
                argv,
                1,
                "",
                "SERVICE_DISABLED: Kubernetes Engine API has not been used in project other-quota-proj before",
                0.01,
            )

        clusters, err = scan.list_project_clusters("target-proj", run=run_quota_disabled)
        self.assertIsNone(clusters)
        self.assertIsNotNone(err)
        self.assertIn("other-quota-proj", err or "")

        # 3. Empty stdout on rc == 0 -> fails closed with error.
        def run_empty_stdout(argv: list[str], **_kwargs) -> scan.Run:
            return scan.Run(argv, 0, "", "", 0.01)

        clusters, err = scan.list_project_clusters("target-proj", run=run_empty_stdout)
        self.assertIsNone(clusters)
        self.assertIn("empty stdout", err or "")

        # 4. Zone timeout warning on rc == 0 -> returns partial clusters and incomplete error.
        def run_zone_timeout(argv: list[str], **_kwargs) -> scan.Run:
            return scan.Run(
                argv,
                0,
                json.dumps([_make_cluster()]),
                "WARNING: Following zones did not respond: us-central1-f",
                0.01,
            )

        clusters, err = scan.list_project_clusters("target-proj", run=run_zone_timeout)
        self.assertEqual(len(clusters or []), 1)
        self.assertIn("incomplete", err or "")

    def test_discover_projects_filtered_listing_and_timeout(self) -> None:
        seen_timeouts: list[int | None] = []

        def run(argv: list[str], **kwargs) -> scan.Run:
            if argv == scan.CONFIG_PROJECT_ARGV:
                return scan.Run(argv, 0, "active-proj\n", "", 0.01)
            if argv == scan.PROJECTS_LIST_ARGV:
                seen_timeouts.append(kwargs.get("timeout"))
                return scan.Run(argv, 0, "other-proj\n", "", 0.01)
            if argv[:4] == ["gcloud", "container", "clusters", "list"]:
                return scan.Run(argv, 0, "[]", "", 0.01)
            return scan.Run(argv, 1, "", "unexpected", 0.01)

        discovery = scan.discover_projects(
            None,
            scope=scan.fleet_scope_args.DeclaredScope(),
            run=run,
        )
        self.assertEqual(discovery.projects, ["active-proj", "other-proj"])
        self.assertIsNone(discovery.error)
        self.assertIn("filtered rather than complete", discovery.partial or "")
        self.assertEqual(seen_timeouts, [scan.PROJECTS_LIST_TIMEOUT_SECONDS])

        # Filtered listing without `--project` fails closed (rc == EXIT_ERROR).
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            rc = scan.main([], run=run)
        self.assertEqual(rc, scan.EXIT_ERROR)
        self.assertIn("filtered rather than complete", buf.getvalue())

    def test_incomplete_project_discovery_fails_closed(self) -> None:
        # 1. `gcloud projects list` fails after `gcloud config get-value project` succeeds.
        def run_list_failed(argv: list[str], **_kwargs) -> scan.Run:
            if argv == scan.CONFIG_PROJECT_ARGV:
                return scan.Run(argv, 0, "active-proj\n", "", 0.01)
            if argv == scan.PROJECTS_LIST_ARGV:
                return scan.Run(argv, 1, "", "ERROR: deadline exceeded", 0.01)
            if argv[:4] == ["gcloud", "container", "clusters", "list"]:
                return scan.Run(argv, 0, "[]", "", 0.01)
            return scan.Run(argv, 1, "", "unexpected", 0.01)

        buf_failed = io.StringIO()
        with mock.patch("sys.stdout", buf_failed):
            rc_failed = scan.main([], run=run_list_failed)
        self.assertEqual(rc_failed, scan.EXIT_ERROR)
        self.assertIn("fell back to 'active-proj'", buf_failed.getvalue())

        # 2. Declared scope with `--scope-unread` entries fails closed when `--project` is not set.
        buf_unread = io.StringIO()
        with mock.patch("sys.stdout", buf_unread):
            rc_unread = scan.main(
                [
                    "--scope-projects",
                    "active-proj",
                    "--scope-unread",
                    "unread-proj: permission denied",
                ],
                run=run_list_failed,
            )
        self.assertEqual(rc_unread, scan.EXIT_ERROR)
        self.assertIn("unread-proj", buf_unread.getvalue())

    def test_fetch_cluster_credentials_guards_custom_and_symlinked_kubeconfig_dir(self) -> None:
        seen_kubeconfigs: list[str] = []

        def run_creds(argv: list[str], **kwargs) -> scan.Run:
            env = kwargs.get("env") or {}
            kc_val = env.get(scan.KUBECONFIG_ENV, "")
            seen_kubeconfigs.append(kc_val)
            if kc_val:
                Path(kc_val).write_text("apiVersion: v1\n", encoding="utf-8")
            return scan.Run(argv, 0, "", "", 0.01)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            custom_dir = tmp_path / "custom-kcs"
            custom_dir.mkdir(parents=True, exist_ok=True)
            planted_target = tmp_path / "planted-target.yaml"
            planted_target.write_text("planted\n", encoding="utf-8")

            kc_file = scan.kubeconfig_path(custom_dir, "proj-a", "seeded-a", "us-central1-a")
            kc_file.symlink_to(planted_target)

            # 1. Planted symlink at `kc` inside a custom `--kubeconfig-dir` is removed before get-credentials writes.
            kc_out, err = scan.fetch_cluster_credentials(
                "proj-a",
                "seeded-a",
                "us-central1-a",
                kubeconfig_dir=custom_dir,
                run=run_creds,
            )
            self.assertIsNone(err)
            self.assertEqual(kc_out, kc_file)
            self.assertFalse(kc_file.is_symlink())
            self.assertEqual(planted_target.read_text(encoding="utf-8"), "planted\n")

            # 2. A symlinked `--kubeconfig-dir` is refused by `prepare_kubeconfig`.
            symlink_dir = tmp_path / "symlink-kcs"
            symlink_dir.symlink_to(custom_dir)
            _, symlink_err = scan.fetch_cluster_credentials(
                "proj-a",
                "seeded-a",
                "us-central1-a",
                kubeconfig_dir=symlink_dir,
                run=run_creds,
            )
            self.assertIsNotNone(symlink_err)
            self.assertIn("symlink", (symlink_err or "").lower())


if __name__ == "__main__":
    unittest.main()
