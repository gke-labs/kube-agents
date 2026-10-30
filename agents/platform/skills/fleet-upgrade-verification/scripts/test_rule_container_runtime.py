#!/usr/bin/env python3
"""Unit tests for readiness_rules/container_runtime.py (catalogue entry 13)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import readiness_rules as shared  # noqa: E402
from fleet_upgrade_report import parse_version  # noqa: E402
from readiness_rules import container_runtime  # noqa: E402
from test_readiness_rules import TARGET, cluster, deployment_pod, node, pod, read, socket_volume, template  # noqa: E402
from test_rule_capacity import context_for, pool_record  # noqa: E402

TARGET_1_33 = parse_version("1.33.1-gke.1000")
PROBE_SPEC = {"containers": [{"name": "probe", "volumeMounts": [{"name": "sock", "mountPath": "/run/containerd/containerd.sock"}]}], "volumes": [socket_volume()]}


def items_for(runtime="containerd://1.7.34", with_client=True, with_nodes=True):
    items = []
    if with_nodes:
        items.append(node("gke-a-default-1", "default-pool", runtime=runtime))
    if with_client:
        items.append(template("DaemonSet", "node-runtime-probe", PROBE_SPEC))
        items.append(pod("seeded-shapes", "node-runtime-probe-x", "gke-a-default-1", owner=("DaemonSet", "node-runtime-probe"), volumes=[socket_volume()]))
    return items


def evaluate(record, items, target=TARGET_1_33, **read_kw):
    return container_runtime.evaluate(record, {}, read(items=items, **read_kw), target, context_for(record))


class ContainerRuntimeRuleTest(unittest.TestCase):
    def test_contract_attributes(self):
        self.assertEqual((container_runtime.RULE_ID, container_runtime.ENTRY, container_runtime.CAN_BLOCK), ("container-runtime", 13, False))

    def test_major_change_under_a_socket_client_is_a_risk(self):
        record = cluster(name="seeded-a", pools=[pool_record("default-pool", "1.32.4-gke.1000")])
        out = evaluate(record, items_for())
        self.assertEqual((out["blocking"], out["unknown"]), ([], []))
        finding = out["risks"][0]
        self.assertEqual((finding["pool"], finding["image_type"], finding["current_major"], finding["target_major"]), ("default-pool", "COS_CONTAINERD", 1, 2))
        self.assertEqual(finding["clients"], [{"owner": "DaemonSet seeded-shapes/node-runtime-probe", "paths": ["/run/containerd/containerd.sock"]}])
        text = container_runtime.describe(finding)
        self.assertIn("runs containerd 1 on its nodes (containerd://1.7.34) and the target's node image ships containerd 2", text)
        self.assertIn("DaemonSet seeded-shapes/node-runtime-probe (/run/containerd/containerd.sock)", text)

    def test_nodes_already_on_containerd_2_are_no_change(self):
        record = cluster(name="seeded-a", pools=[pool_record("default-pool", "1.32.4-gke.1000")])
        out = evaluate(record, items_for(runtime="containerd://2.0.10"))
        self.assertEqual(out["risks"], [])
        self.assertIn("no pool's containerd major changes at the target", out["notes"][0])
        self.assertIn("DaemonSet seeded-shapes/node-runtime-probe", out["notes"][0])

    def test_nodes_unread_fall_back_to_the_table_by_pool_minor(self):
        record = cluster(name="seeded-a", pools=[pool_record("default-pool", "1.32.4-gke.1000")])
        out = evaluate(record, items_for(), read_errors={"node": "nodes is forbidden"})
        self.assertIn("by its version 1.32, nodes unread", out["risks"][0]["current"])
        record = cluster(name="seeded-a", pools=[pool_record("default-pool", "1.33.0-gke.1000")])
        self.assertEqual(evaluate(record, items_for(with_nodes=False), target=parse_version("1.34.1-gke.1000"), read_errors={"node": "x"})["risks"], [])

    def test_a_pool_with_no_node_reporting_a_runtime_says_so_not_nodes_unread(self):
        # The Node read answered and the pool is in it with no node (empty, or not yet registered):
        # the table answers by the pool's minor, and the text says why, not that the nodes were unread.
        record = cluster(name="seeded-a", pools=[pool_record("default-pool", "1.32.4-gke.1000")])
        out = evaluate(record, items_for(with_nodes=False))
        self.assertEqual(out["risks"][0]["current"], "containerd 1 by its version 1.32; no node of the pool reports a runtime")
        self.assertNotIn("unread", container_runtime.describe(out["risks"][0]))
        # A node of the pool with no parsable runtime version is the same answer.
        blank = node("gke-a-default-1", "default-pool", runtime="")
        out = evaluate(record, [blank] + items_for(with_nodes=False))
        self.assertIn("; no node of the pool reports a runtime", out["risks"][0]["current"])

    def test_windows_pools_move_at_1_35(self):
        record = cluster(name="win", pools=[pool_record("win-pool", "1.34.2-gke.1000", image="WINDOWS_LTSC_CONTAINERD")])
        items = [template("DaemonSet", "win-agent", PROBE_SPEC, namespace="monitoring")]
        self.assertEqual(evaluate(record, items, target=parse_version("1.34.5-gke.1000"))["risks"], [])
        self.assertEqual(evaluate(record, items, target=parse_version("1.35.0-gke.1000"))["risks"][0]["target_major"], 2)

    def test_change_without_clients_is_a_note_and_unread_clients_are_unknown(self):
        record = cluster(name="seeded-a", pools=[pool_record("default-pool", "1.32.4-gke.1000")])
        out = evaluate(record, items_for(with_client=False))
        self.assertEqual(out["risks"], [])
        self.assertIn("no pod mounts the runtime socket", out["notes"][0])
        unread = evaluate(record, items_for(), read_errors={"pods": "pods is forbidden"})
        self.assertEqual(unread["risks"], [])
        self.assertIn("pods not read (pods is forbidden), so whether a pod mounts the runtime socket is unknown", unread["unknown"][0])
        nothing = container_runtime.evaluate(record, {}, read(error="get-credentials failed"), TARGET_1_33, context_for(record))
        self.assertIn("so whether a pod mounts the runtime socket is unknown", nothing["unknown"][0])
        # A read failure on a cluster whose pools do not change major costs nothing.
        settled = cluster(pools=[pool_record("p", "1.33.0-gke.1")])
        self.assertEqual(container_runtime.evaluate(settled, {}, read(error="x"), parse_version("1.34.0-gke.1"), context_for(settled)), shared.new_result())

    def test_no_target_and_unparsable_versions_are_unknown_only_with_clients(self):
        record = cluster(name="seeded-a", pools=[pool_record("default-pool", "1.32.4-gke.1000")])
        self.assertIn("no target", evaluate(record, items_for(), target=None)["unknown"][0])
        self.assertEqual(evaluate(record, items_for(with_client=False), target=None), shared.new_result())
        record["nodePools"][0]["version"] = "latest"
        self.assertIn("version unparsable", evaluate(record, items_for(), target=TARGET)["unknown"][0])

    def test_a_planted_daemonset_counts_from_its_template_whatever_its_pods_do(self):
        # The fleet's node-runtime-probe shape: a DaemonSet outside the system namespaces whose
        # template mounts the socket file. It is a client when its pods run, when none runs on the
        # cluster, and when only its pods were read; the system DaemonSets beside it count too.
        probe = template("DaemonSet", "node-runtime-probe", PROBE_SPEC)
        system = template("DaemonSet", "gke-metadata-server", {"containers": [{"name": "c"}], "volumes": [{"name": "run", "hostPath": {"path": "/var/run/"}}]}, namespace="kube-system")
        probe_pod = pod("seeded-shapes", "node-runtime-probe-4xvj9", "n1", owner=("DaemonSet", "node-runtime-probe"), volumes=[socket_volume()])
        expected = [{"owner": "DaemonSet kube-system/gke-metadata-server", "paths": ["/var/run/"]}, {"owner": "DaemonSet seeded-shapes/node-runtime-probe", "paths": ["/run/containerd/containerd.sock"]}]
        self.assertEqual(container_runtime.socket_clients([probe, system, probe_pod]), expected)
        self.assertEqual(container_runtime.socket_clients([probe, system]), expected)
        self.assertEqual(container_runtime.socket_clients([system, probe_pod]), expected)
        # Through the rule, on a pool whose containerd major does not change at the target.
        record = cluster(name="seeded-a", pools=[pool_record("default-pool", "1.33.4-gke.1000")])
        out = evaluate(record, [probe, system, node("n1", "default-pool", runtime="containerd://2.0.4")], target=TARGET)
        self.assertEqual((out["risks"], out["unknown"]), ([], []))
        self.assertEqual(out["notes"], ["2 workload(s) mount the container runtime socket (DaemonSet kube-system/gke-metadata-server, DaemonSet seeded-shapes/node-runtime-probe); no pool's containerd major changes at the target"])

    def test_socket_clients_dedupe_across_pods_and_templates_and_accept_both_spellings(self):
        items = [
            template("Deployment", "agent", {"containers": [{"name": "c"}], "volumes": [socket_volume("/var/run/docker.sock"), socket_volume("/var/run/containerd/containerd.sock")]}, namespace="ops"),
            deployment_pod("ops", "agent", "n1", volumes=[socket_volume("/var/run/docker.sock")]),
            template("StatefulSet", "db", {"containers": [{"name": "c"}], "volumes": [{"name": "data", "hostPath": {"path": "/mnt/data"}}]}, namespace="ops"),
            template("CronJob", "nightly", {"containers": [{"name": "c"}], "volumes": [socket_volume()]}, namespace="kube-system"),
            # The socket's directory, mounted whole, reaches it too; a wider mount does not count.
            template("DaemonSet", "falco", {"containers": [{"name": "c"}], "volumes": [{"name": "containerd-socket", "hostPath": {"path": "/run/containerd"}}, {"name": "var", "hostPath": {"path": "/var"}}]}, namespace="falco"),
            template("DaemonSet", "cadvisor", {"containers": [{"name": "c"}], "volumes": [{"name": "run", "hostPath": {"path": "/var/run/"}}]}, namespace="monitoring"),
        ]
        self.assertEqual(
            container_runtime.socket_clients(items),
            [
                {"owner": "CronJob kube-system/nightly", "paths": ["/run/containerd/containerd.sock"]},
                {"owner": "DaemonSet falco/falco", "paths": ["/run/containerd"]},
                {"owner": "DaemonSet monitoring/cadvisor", "paths": ["/var/run/"]},
                {"owner": "Deployment ops/agent", "paths": ["/var/run/containerd/containerd.sock", "/var/run/docker.sock"]},
            ],
        )
        self.assertTrue(all(container_runtime.socket_mount(p) for p in ("/run", "/var/run", "/run/containerd/", "/var/run/containerd", "/var/run/docker.sock")))
        self.assertFalse(any(container_runtime.socket_mount(p) for p in ("/", "/var", "/mnt/data", "/run/containerd-shim", "")))


if __name__ == "__main__":
    unittest.main()
