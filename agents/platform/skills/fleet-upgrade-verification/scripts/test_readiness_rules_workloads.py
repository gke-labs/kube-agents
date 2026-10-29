#!/usr/bin/env python3
"""Unit tests for the workload rules of readiness_rules/ (catalogue entries 4, 12, 14, 15, 17,
18 and 20) on fixture-shaped objects.

The objects mirror the seeded fleet's shapes on bench/tf/fleet (cache-on-emptydir,
arch-pinned-worker, cgroup-blind-jvm, multi-process-worker, cni-shaped-agent,
cuda-pinned-trainer, legacy-registry-pull) plus the variants each rule grades differently:
the blocking, risk, unknown, note and clean paths a rule has, and the inputs the adversarial
review of the first cut found graded wrong (a `DoesNotExist` term, a preferred term, a pool
scaled to zero, a quoted `&`, a compute-capability env, a scratch emptyDir).
"""

import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import readiness_rules as rules  # noqa: E402
import upgrade_readiness as r  # noqa: E402
import upgrade_shape_tables as tables  # noqa: E402
from fleet_upgrade_report import parse_version  # noqa: E402
from test_readiness_rules import affinity, pool, read, template  # noqa: E402
from readiness_rules import (  # noqa: E402
    cgroup_v2_runtime,
    gpu_driver_mismatch,
    group_oom_kill,
    node_image_coupled_agent,
    node_local_state,
    removed_node_label,
    retired_registry,
)

AT = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
TARGET_TEXT = "1.34.1-gke.1000"
TARGET = parse_version(TARGET_TEXT)
NS = "seeded-shapes"
WORKLOAD_RULES = {node_local_state: 4, removed_node_label: 12, cgroup_v2_runtime: 14, group_oom_kill: 15, node_image_coupled_agent: 17, gpu_driver_mismatch: 18, retired_registry: 20}
CAN_BLOCK = {removed_node_label, gpu_driver_mismatch}


def node(name, pool="default-pool", labels=None):
    base = {tables.NODEPOOL_LABEL: pool, "kubernetes.io/arch": "amd64", "beta.kubernetes.io/arch": "amd64", "kubernetes.io/os": "linux", "topology.kubernetes.io/zone": "us-central1-a"}
    return {"kind": "Node", "metadata": {"name": name, "labels": {**base, **(labels or {})}}}


def read_of(items, read_errors=None):
    """The sibling's `read` over a list of items."""
    return read(items=list(items), read_errors=read_errors)


def context(pools, target_text=TARGET_TEXT, autopilot=False):
    return {rules.CONTEXT_AT: AT, rules.CONTEXT_TARGET_TEXT: target_text, rules.CONTEXT_MASTER: parse_version("1.33.4-gke.1000"), rules.CONTEXT_POOLS: pools, rules.CONTEXT_AUTOPILOT: autopilot, rules.CONTEXT_CACHE: {}}


def run(rule, items, pools=None, target=TARGET, autopilot=False, read_errors=None):
    pools = [pool()] if pools is None else pools
    return rule.evaluate({}, {}, read_of(items, read_errors), target, context(pools, autopilot=autopilot))


def busybox(command=None, name="worker", mounts=None):
    container = {"name": name, "image": "busybox:1.36"}
    if command:
        container["command"] = command
    if mounts:
        container["volumeMounts"] = mounts
    return container


WORKLOADS_UNREAD = {"daemonset": "daemonsets is forbidden", "cronjob": "daemonsets is forbidden"}


class RegistrationTest(unittest.TestCase):
    """The seven modules are registered in entry order with the blocking contract each declares."""

    def test_registered_in_entry_order_with_can_block(self):
        for module, entry in WORKLOAD_RULES.items():
            self.assertIn(module, r.EXTRA_RULES, module.RULE_ID)
            self.assertEqual(module.ENTRY, entry)
            self.assertEqual(module.CAN_BLOCK, module in CAN_BLOCK, module.RULE_ID)
        entries = [m.ENTRY for m in r.EXTRA_RULES if m.ENTRY is not None]  # the plan-level rules, with no entry, come after the numbered ones
        self.assertEqual(entries, sorted(entries))

    def test_the_fold_names_the_object_in_a_per_object_unknown_and_prefixes_notes(self):
        pinned = template("Deployment", "pinned", {"containers": [busybox()], "nodeSelector": {"team/gpu": "yes"}})
        folded = r.evaluate_extra_rules({}, {}, read_of([pinned], {"node": "nodes is forbidden"}), TARGET, context([pool()]), rules=[removed_node_label])
        self.assertEqual(folded["unknown"][0]["text"], "removed-node-label: Deployment seeded-shapes/pinned: selects on team/gpu=yes (nodeSelector); the Node read failed (nodes is forbidden), so whether a node carries it is unknown")
        self.assertTrue(folded["unknown"][0]["can_block"])
        scratch = template("Deployment", "cache-on-emptydir", {"containers": [busybox(name="queue")], "volumes": [{"name": "queue", "emptyDir": {}}]})
        folded = r.evaluate_extra_rules({}, {}, read_of([scratch]), TARGET, context([pool()]), rules=[node_local_state])
        self.assertEqual(folded["notes"], ["data-on-the-node: Deployment seeded-shapes/cache-on-emptydir keeps scratch on the node in queue (emptyDir; the name suggests state); not a risk: a Deployment's emptyDir is emptied by every reschedule and a read-only or memory-backed mount holds nothing of the workload's"])
        self.assertEqual(folded["rules"]["data-on-the-node"], {"entry": 4, "can_block": False, "blocking": 0, "risks": 0, "unknown": 0, "notes": 1})

    def test_the_contract_call_with_empty_reads_and_context_is_clean(self):
        for module in WORKLOAD_RULES:
            result = module.evaluate({"location": "us-central1"}, {}, read_of([]), TARGET, {})
            self.assertEqual(result, rules.new_result(), module.RULE_ID)


class NodeLocalStateTest(unittest.TestCase):
    """Entry 4: state is a risk, scratch a note; nothing it reads can block."""

    def test_deployment_emptydir_queue_is_a_note_not_a_risk(self):
        out = run(node_local_state, [template("Deployment", "cache-on-emptydir", {"containers": [busybox(name="queue")], "volumes": [{"name": "queue", "emptyDir": {}}]})])
        self.assertEqual((out["blocking"], out["risks"], out["unknown"]), ([], [], []))
        self.assertEqual(out["notes"], ["Deployment seeded-shapes/cache-on-emptydir keeps scratch on the node in queue (emptyDir; the name suggests state); not a risk: a Deployment's emptyDir is emptied by every reschedule and a read-only or memory-backed mount holds nothing of the workload's"])

    def test_three_tmp_emptydirs_are_notes_and_fill_no_risk(self):
        items = [template("Deployment", f"svc-{i}", {"containers": [busybox(mounts=[{"name": "tmp", "mountPath": "/tmp"}])], "volumes": [{"name": "tmp", "emptyDir": {}}]}) for i in range(3)]
        out = run(node_local_state, items)
        self.assertEqual(out["risks"], [])
        self.assertEqual(len(out["notes"]), 3)

    def test_statefulset_emptydir_is_the_strongest_risk(self):
        out = run(node_local_state, [template("StatefulSet", "queue", {"containers": [busybox()], "volumes": [{"name": "data", "emptyDir": {}}]})])
        finding = out["risks"][0]
        self.assertEqual((finding["kind"], finding["object"], finding["grade"], finding["volumes"]), ("StatefulSet", "seeded-shapes/queue", "risk", ["data (emptyDir; the name suggests state)"]))
        self.assertEqual(node_local_state.describe(finding), "StatefulSet seeded-shapes/queue: keeps data on the node in data (emptyDir; the name suggests state); a node rebuild loses it, and a StatefulSet's identity survives the rebuild while its node-local data does not")

    def test_local_ssd_and_writable_hostpaths_are_risks_memory_and_read_only_are_notes(self):
        spec = {
            "containers": [busybox(mounts=[{"name": "scratch", "mountPath": "/scratch"}, {"name": "ssd", "mountPath": "/ssd", "readOnly": True}, {"name": "logs", "mountPath": "/host/log", "readOnly": True}, {"name": "work", "mountPath": "/work"}])],
            "volumes": [{"name": "scratch", "emptyDir": {"medium": "Memory"}}, {"name": "ssd", "hostPath": {"path": "/mnt/disks/ssd0"}}, {"name": "logs", "hostPath": {"path": "/var/log"}}, {"name": "work", "hostPath": {"path": "/opt/work"}}, {"name": "spare", "hostPath": {"path": "/opt/spare"}}, {"name": "cfg", "configMap": {"name": "c"}}],
        }
        out = run(node_local_state, [template("Deployment", "d", spec)])
        self.assertEqual(out["risks"][0]["volumes"], ["ssd (hostPath /mnt/disks/ssd0, a Local SSD mount)", "work (hostPath /opt/work)"])
        # A hostPath no container mounts is declared, not mounted read-only.
        self.assertIn("scratch (emptyDir, medium Memory), logs (hostPath /var/log, mounted read-only), spare (hostPath /opt/spare, declared, mounted by no container)", out["notes"][0])

    def test_statefulset_on_a_local_storage_class_and_claims(self):
        items = [
            {"kind": "StorageClass", "metadata": {"name": "local-scsi"}, "provisioner": "kubernetes.io/no-provisioner"},
            {"kind": "StorageClass", "metadata": {"name": "standard-rwo"}, "provisioner": "pd.csi.storage.gke.io"},
            {"kind": "PersistentVolumeClaim", "metadata": {"namespace": NS, "name": "fast"}, "spec": {"storageClassName": "local-scsi"}},
            {"kind": "PersistentVolumeClaim", "metadata": {"namespace": NS, "name": "disk"}, "spec": {"storageClassName": "standard-rwo"}},
            template("StatefulSet", "db", {"containers": [busybox()]}, workload_spec={"volumeClaimTemplates": [{"metadata": {"name": "data"}, "spec": {"storageClassName": "local-scsi"}}]}),
            template("Deployment", "on-ssd", {"containers": [busybox()], "volumes": [{"name": "v", "persistentVolumeClaim": {"claimName": "fast"}}]}),
            template("Deployment", "on-disk", {"containers": [busybox()], "volumes": [{"name": "v", "persistentVolumeClaim": {"claimName": "disk"}}]}),
        ]
        out = run(node_local_state, items)
        self.assertEqual([(f["name"], f["volumes"]) for f in out["risks"]], [("db", ["data (volumeClaimTemplate on local StorageClass local-scsi)"]), ("on-ssd", ["v (claim fast on local StorageClass local-scsi)"])])
        self.assertEqual(out["unknown"], [])

    def test_storage_read_failure_makes_claims_unknown_and_nothing_else(self):
        items = [
            template("Deployment", "on-disk", {"containers": [busybox()], "volumes": [{"name": "v", "persistentVolumeClaim": {"claimName": "disk"}}]}),
            template("Deployment", "stateless", {"containers": [busybox()]}),
        ]
        out = run(node_local_state, items, read_errors={"storageclass": "storageclasses is forbidden", "pv": "storageclasses is forbidden"})
        self.assertEqual([u["name"] for u in out["unknown"]], ["on-disk"])
        self.assertEqual(out["unknown"][0]["reason"], "Deployment seeded-shapes/on-disk: mounts claim(s) disk; the storageclass read failed (storageclasses is forbidden), so whether a claim is on a local class is unknown")
        # A refused claim read costs the same question; a refused DaemonSet read costs this rule nothing.
        out = run(node_local_state, items, read_errors={"pvc": "pvcs is forbidden"})
        self.assertIn("the pvc read failed", out["unknown"][0]["detail"])
        # A claim template names its class: with the StorageClass read in, a refused PVC read
        # leaves it graded (a risk on a local class) and files no unknown; a refused StorageClass
        # read is the unknown, with no risk beside it.
        local = {"kind": "StorageClass", "metadata": {"name": "local-scsi"}, "provisioner": "kubernetes.io/no-provisioner"}
        db = template("StatefulSet", "db", {"containers": [busybox()]}, workload_spec={"volumeClaimTemplates": [{"metadata": {"name": "data"}, "spec": {"storageClassName": "local-scsi"}}]})
        out = run(node_local_state, [local, db], read_errors={"pvc": "pvcs is forbidden"})
        self.assertEqual(([f["name"] for f in out["risks"]], out["unknown"]), (["db"], []))
        out = run(node_local_state, [db], read_errors={"storageclass": "storageclasses is forbidden", "pv": "storageclasses is forbidden"})
        self.assertEqual(out["risks"], [])
        self.assertEqual([u["detail"] for u in out["unknown"]], ["mounts claim(s) data; the storageclass read failed (storageclasses is forbidden), so whether a claim is on a local class is unknown"])
        self.assertEqual(run(node_local_state, items, read_errors=WORKLOADS_UNREAD)["unknown"], [])

    def test_no_workload_read_is_one_unknown_not_a_clean_record(self):
        # The PDB read carries the Deployments and StatefulSets this rule grades: when it did not
        # run, the rule says so instead of reading as a cluster with no node-local state.
        failed = {"kubeconfig": "k", "items": None, "error": "get-credentials failed", "read_errors": {kind: rules.NO_READ_RAN_REASON for kind in rules.READ_KINDS}}
        out = node_local_state.evaluate({}, {}, failed, TARGET, context([pool()]))
        self.assertEqual((out["risks"], out["notes"]), ([], []))
        self.assertEqual([(u["object"], u["detail"]) for u in out["unknown"]], [(None, "Deployments and StatefulSets not read (get-credentials failed); nothing graded")])

    def test_clean_daemonsets_cronjobs_and_system_namespaces_are_not_read(self):
        items = [
            template("Deployment", "stateless", {"containers": [busybox()], "volumes": [{"name": "cfg", "configMap": {"name": "c"}}]}),
            template("DaemonSet", "agent", {"containers": [busybox()], "volumes": [{"name": "logs", "hostPath": {"path": "/var/log"}}]}),
            template("CronJob", "job", {"containers": [busybox()], "volumes": [{"name": "tmp", "emptyDir": {}}]}),
            template("StatefulSet", "kube-dns", {"containers": [busybox()], "volumes": [{"name": "tmp", "emptyDir": {}}]}, namespace="kube-system"),
            template("StatefulSet", "collector", {"containers": [busybox()], "volumes": [{"name": "tmp", "emptyDir": {}}]}, namespace="gke-managed-cim"),
            template("StatefulSet", "cnrm", {"containers": [busybox()], "volumes": [{"name": "tmp", "emptyDir": {}}]}, namespace="cnrm-system"),
        ]
        self.assertEqual(run(node_local_state, items), rules.new_result())


class RemovedNodeLabelTest(unittest.TestCase):
    NODES = [node("n1"), node("n2")]

    def test_deprecated_beta_arch_label_the_nodes_carry_is_a_risk_with_the_replacement(self):
        items = self.NODES + [template("Deployment", "arch-pinned-worker", {"containers": [busybox()], "nodeSelector": {"beta.kubernetes.io/arch": "amd64"}})]
        out = run(removed_node_label, items)
        self.assertEqual((out["blocking"], out["unknown"]), ([], []))
        finding = out["risks"][0]
        self.assertEqual((finding["object"], finding["label"], finding["replacement"]), ("seeded-shapes/arch-pinned-worker", "beta.kubernetes.io/arch", "kubernetes.io/arch"))
        self.assertEqual(removed_node_label.describe(finding), "Deployment seeded-shapes/arch-pinned-worker: selects on beta.kubernetes.io/arch=amd64 (nodeSelector), deprecated since 1.14 in favour of kubernetes.io/arch; the kubelet still sets it today, and a node image that stops setting it strands the pods")

    def test_a_drop_blocks_only_when_a_selected_pool_crosses_it(self):
        pinned = template("Deployment", "docker-pinned", {"containers": [busybox()], "nodeSelector": {"cloud.google.com/gke-container-runtime": "docker"}})
        out = run(removed_node_label, self.NODES + [pinned], pools=[pool(version="1.23.17-gke.1")], target=parse_version("1.24.0-gke.1"))
        finding = out["blocking"][0]
        self.assertEqual((finding["grade"], finding["dropped_at"], finding["pools_crossing"]), ("blocking", "1.24", ["default-pool"]))
        self.assertIn("dropped at 1.24: GKE 1.24 removes Docker node images; pool(s) default-pool cross the drop in this upgrade to 1.24", finding["detail"])
        out = run(removed_node_label, self.NODES + [pinned])
        self.assertEqual((out["blocking"], out["risks"], out["unknown"]), ([], [], []))
        self.assertEqual(out["notes"], ["Deployment seeded-shapes/docker-pinned selects on cloud.google.com/gke-container-runtime=docker (nodeSelector), dropped at 1.24, which every pool passed already; the pods either run or are already unschedulable, and the upgrade changes neither"])
        out = run(removed_node_label, self.NODES + [pinned], target=None)
        self.assertIn("whether a pool crosses the drop needs a target", out["unknown"][0]["detail"])
        self.assertTrue(out["unknown"][0]["reason"].startswith("Deployment seeded-shapes/docker-pinned: "))
        # A target below the drop: nothing has passed it; a later upgrade crosses it.
        out = run(removed_node_label, self.NODES + [pinned], pools=[pool(version="1.22.17-gke.1")], target=parse_version("1.23.0-gke.1"))
        self.assertEqual((out["blocking"], out["unknown"]), ([], []))
        self.assertEqual(out["notes"], ["Deployment seeded-shapes/docker-pinned selects on cloud.google.com/gke-container-runtime=docker (nodeSelector), dropped at 1.24, after this upgrade's target 1.23; a later upgrade crosses the drop"])
        # A pool whose version did not parse: whether it crosses the drop is unknown, not passed.
        out = run(removed_node_label, self.NODES + [pinned], pools=[pool(version="latest")], target=parse_version("1.24.0-gke.1"))
        self.assertEqual(out["notes"], [])
        self.assertIn("dropped at 1.24; the version of pool(s) default-pool did not parse, so whether they cross the drop in this upgrade is unknown", out["unknown"][0]["detail"])
        # A selector naming a pool the record lacks: no pool crosses the drop, and the note says which is missing.
        ghost = template("Deployment", "ghost-pinned", {"containers": [busybox()], "nodeSelector": {tables.NODEPOOL_LABEL: "ghost", "cloud.google.com/gke-container-runtime": "docker"}})
        out = run(removed_node_label, self.NODES + [ghost], target=parse_version("1.24.0-gke.1"))
        self.assertEqual((out["blocking"], out["unknown"]), ([], []))
        self.assertIn("dropped at 1.24; pool(s) ghost are not in the cluster record, so no pool crosses the drop in this upgrade", out["notes"][0])
        # The carried question on the same missing pool names it too, rather than printing a blank list.
        stale = template("Deployment", "stale", {"containers": [busybox()], "nodeSelector": {tables.NODEPOOL_LABEL: "ghost", "team": "ml"}})
        out = run(removed_node_label, self.NODES + [stale])
        self.assertEqual((out["blocking"], out["risks"], out["unknown"]), ([], [], []))
        self.assertEqual(out["notes"], ["Deployment seeded-shapes/stale selects on team=ml (nodeSelector) and pool(s) ghost (not in the cluster record) has no node today; not graded as stranded"])
        # The value that was not dropped, on a key GKE sets from the pool's configuration: no node of
        # the canned list carries it, and that is a pool shape, not this rule's risk.
        containerd = template("Deployment", "containerd-pinned", {"containers": [busybox()], "nodeSelector": {"cloud.google.com/gke-container-runtime": "containerd"}})
        self.assertEqual(run(removed_node_label, self.NODES + [containerd]), rules.new_result())

    def test_kubeadm_master_label_on_gke_is_a_note_for_exists_and_nothing_for_does_not_exist(self):
        exists = template("Deployment", "reflector", {"containers": [busybox()], **affinity("node-role.kubernetes.io/master", "Exists")})
        out = run(removed_node_label, self.NODES + [exists])
        self.assertEqual((out["blocking"], out["risks"]), ([], []))
        self.assertIn("node-role.kubernetes.io/master (nodeAffinity), dropped at 1.24, which every pool passed already", out["notes"][0])
        avoid = template("Deployment", "operator", {"containers": [busybox()], **affinity("node-role.kubernetes.io/master", "DoesNotExist")})
        self.assertEqual(run(removed_node_label, self.NODES + [avoid]), rules.new_result())

    def test_negative_operators_and_preferred_terms_are_never_graded(self):
        items = self.NODES + [
            template("Deployment", "web", {"containers": [busybox()], **affinity("cloud.google.com/gke-spot", "DoesNotExist")}),
            template("Deployment", "batch", {"containers": [busybox()], **affinity("cloud.google.com/gke-spot", "In", ["true"], required=False)}),
            template("Deployment", "x86", {"containers": [busybox()], **affinity("kubernetes.io/arch", "NotIn", ["arm64"])}),
            template("Deployment", "gt", {"containers": [busybox()], **affinity("example.com/tier", "Gt", ["2"])}),
            template("Deployment", "no-arm", {"containers": [busybox()], **affinity("beta.kubernetes.io/arch", "NotIn", ["arm64"])}),
        ]
        self.assertEqual(run(removed_node_label, items), rules.new_result())

    def test_label_no_node_carries_is_a_risk_scoped_to_the_selected_pool(self):
        nodes = [node("a1", pool="pool-a", labels={"team/gpu": "yes"}), node("b1", pool="pool-b")]
        pinned = template("Deployment", "pinned", {"containers": [busybox()], "nodeSelector": {tables.NODEPOOL_LABEL: "pool-b", "team/gpu": "yes"}})
        out = run(removed_node_label, nodes + [pinned], pools=[pool("pool-a"), pool("pool-b")])
        self.assertEqual([f["label"] for f in out["risks"]], ["team/gpu"])
        self.assertIn("selects on team/gpu=yes (nodeSelector): no pool's labels define it and no node in pool(s) pool-b carries it today; the rebuilt nodes carry it no more", out["risks"][0]["detail"])
        self.assertNotIn("do not schedule", out["risks"][0]["detail"])
        free = template("Deployment", "free", {"containers": [busybox()], "nodeSelector": {"team/gpu": "yes"}})
        self.assertEqual(run(removed_node_label, nodes + [free], pools=[pool("pool-a"), pool("pool-b")]), rules.new_result())
        # A pool whose configuration defines the label provides it, with no node today or ever: not this rule's risk.
        defined = pool("pool-b", labels={"team/gpu": "yes"})
        self.assertEqual(run(removed_node_label, nodes + [pinned], pools=[pool("pool-a"), defined]), rules.new_result())
        other_value = pool("pool-b", labels={"team/gpu": "no"})
        self.assertEqual([f["label"] for f in run(removed_node_label, nodes + [pinned], pools=[pool("pool-a"), other_value])["risks"]], ["team/gpu"])

    def test_a_pool_at_zero_nodes_is_a_note_and_autopilot_skips_the_carried_question(self):
        # The pool and accelerator labels are GKE's, set from the pool's configuration: a pool at zero
        # nodes files nothing for them. A custom label on the same empty pool is the note.
        gpu_pool = pool("gpu-pool", autoscaling={"enabled": True, "minNodeCount": 0, "maxNodeCount": 4})
        trainer = template("CronJob", "trainer", {"containers": [busybox()], "nodeSelector": {tables.NODEPOOL_LABEL: "gpu-pool", "cloud.google.com/gke-accelerator": "nvidia-l4"}})
        self.assertEqual(run(removed_node_label, self.NODES + [trainer], pools=[pool(), gpu_pool]), rules.new_result())
        tagged = template("CronJob", "trainer", {"containers": [busybox()], "nodeSelector": {tables.NODEPOOL_LABEL: "gpu-pool", "team/gpu": "yes"}})
        out = run(removed_node_label, self.NODES + [tagged], pools=[pool(), gpu_pool])
        self.assertEqual((out["blocking"], out["risks"], out["unknown"]), ([], [], []))
        self.assertEqual(len(out["notes"]), 1)
        self.assertIn("selects on team/gpu=yes (nodeSelector) and pool(s) gpu-pool has no node today (the pool scales from zero); not graded as stranded", out["notes"][0])
        arch = template("Deployment", "arch-pinned-worker", {"containers": [busybox()], "nodeSelector": {"beta.kubernetes.io/arch": "amd64", "team/gpu": "yes"}})
        out = run(removed_node_label, [arch], pools=[], autopilot=True)
        self.assertEqual([f["label"] for f in out["risks"]], ["beta.kubernetes.io/arch"])
        self.assertEqual(out["notes"], [])

    def test_the_seeded_gpu_selector_with_no_gpu_pool_is_not_this_rules_risk(self):
        # The seeded cuda-pinned-trainer: a selector on cloud.google.com/gke-accelerator=nvidia-l4 on a
        # cluster with no accelerator pool and no node carrying the label. The missing pool is the
        # gpu-driver rule's finding (entry 18); this rule files nothing, so the object has one cell.
        trainer = template("CronJob", "cuda-pinned-trainer", {"containers": [busybox()], "nodeSelector": {"cloud.google.com/gke-accelerator": "nvidia-l4"}})
        self.assertEqual(run(removed_node_label, self.NODES + [trainer], pools=[pool()]), rules.new_result())
        self.assertEqual(run(removed_node_label, self.NODES + [trainer], pools=[pool()], read_errors={"node": "nodes is forbidden"}), rules.new_result())

    def test_read_failures_are_unknown_for_what_they_hide(self):
        pinned = template("Deployment", "pinned", {"containers": [busybox()], "nodeSelector": {"team/gpu": "yes"}})
        out = run(removed_node_label, [pinned], read_errors={"node": "nodes is forbidden"})
        self.assertEqual(out["risks"], [])
        self.assertIn("the Node read failed (nodes is forbidden), so whether a node carries it is unknown", out["unknown"][0]["detail"])
        out = run(removed_node_label, self.NODES + [template("Deployment", "plain", {"containers": [busybox()]})], read_errors=WORKLOADS_UNREAD)
        self.assertEqual([(u["object"], u["detail"]) for u in out["unknown"]], [(None, "DaemonSets and CronJobs not read (daemonsets is forbidden); Deployments and StatefulSets graded")])

    def test_ga_labels_the_nodes_carry_and_system_namespaces_are_clean(self):
        items = self.NODES + [
            template("Deployment", "zonal", {"containers": [busybox()], **affinity("topology.kubernetes.io/zone", "In", ["us-central1-a"])}),
            template("Deployment", "arch", {"containers": [busybox()], "nodeSelector": {"kubernetes.io/arch": "amd64"}}),
            template("DaemonSet", "fluentbit", {"containers": [busybox()], "nodeSelector": {"beta.kubernetes.io/os": "linux"}}, namespace="kube-system"),
            template("Deployment", "gatekeeper", {"containers": [busybox()], "nodeSelector": {"beta.kubernetes.io/os": "linux"}}, namespace="gatekeeper-system"),
        ]
        self.assertEqual(run(removed_node_label, items), rules.new_result())


class CgroupV2RuntimeTest(unittest.TestCase):
    JVM = template("Deployment", "cgroup-blind-jvm", {"containers": [{"name": "jvm", "image": "docker.io/library/eclipse-temurin:8u302-b08-jre"}]})

    def test_old_jdk_on_a_pool_gke_migrates_at_the_target_is_a_risk(self):
        out = run(cgroup_v2_runtime, [self.JVM], pools=[pool(effectiveCgroupMode="EFFECTIVE_CGROUP_MODE_V1")])
        self.assertEqual((out["blocking"], out["unknown"]), ([], []))
        finding = out["risks"][0]
        self.assertEqual((finding["object"], finding["image"], finding["runtime"]), ("seeded-shapes/cgroup-blind-jvm", "docker.io/library/eclipse-temurin:8u302-b08-jre", "JDK 8u302, below 8u372"))
        self.assertEqual(cgroup_v2_runtime.describe(finding), "Deployment seeded-shapes/cgroup-blind-jvm: container jvm runs docker.io/library/eclipse-temurin:8u302-b08-jre (JDK 8u302, below 8u372) on pool default-pool, which GKE migrates cgroup v1 pools to v2 from 1.33 at the target; the heap is then sized from the node and the container is OOM-killed")

    def test_pool_already_on_v2_or_staying_on_v1_is_a_note_not_a_risk(self):
        out = run(cgroup_v2_runtime, [self.JVM], pools=[pool(effectiveCgroupMode="EFFECTIVE_CGROUP_MODE_V2")])
        self.assertEqual(out["risks"], [])
        self.assertEqual(out["notes"], ["Deployment seeded-shapes/cgroup-blind-jvm container jvm runs docker.io/library/eclipse-temurin:8u302-b08-jre (JDK 8u302, below 8u372) on pool default-pool, already on cgroup v2; not an upgrade risk"])
        out = run(cgroup_v2_runtime, [self.JVM], pools=[pool(version="1.31.0-gke.1", effectiveCgroupMode="EFFECTIVE_CGROUP_MODE_V1")], target=parse_version("1.32.0-gke.1"))
        self.assertEqual(out["risks"], [])
        self.assertIn("the pool stays on cgroup v1 below 1.33; not a risk at this target", out["notes"][0])

    def test_a_v1_pin_holds_until_1_35(self):
        pinned = pool(effectiveCgroupMode="EFFECTIVE_CGROUP_MODE_V1", linuxNodeConfig={"cgroupMode": "CGROUP_MODE_V1"})
        self.assertEqual(run(cgroup_v2_runtime, [self.JVM], pools=[pinned])["risks"], [])
        out = run(cgroup_v2_runtime, [self.JVM], pools=[pinned], target=parse_version("1.35.0-gke.1"))
        self.assertIn("the pool is pinned to cgroup v1 and GKE removes cgroup v1 at 1.35", out["risks"][0]["detail"])

    def test_unread_cgroup_mode_unversioned_tags_and_a_failed_workload_read_are_unknown(self):
        out = run(cgroup_v2_runtime, [self.JVM], pools=[pool()])
        self.assertEqual(out["risks"], [])
        self.assertIn("the pool record carries no effectiveCgroupMode", out["unknown"][0]["detail"])
        self.assertTrue(out["unknown"][0]["reason"].startswith("Deployment seeded-shapes/cgroup-blind-jvm: "))
        floating = template("Deployment", "floating", {"containers": [{"name": "jvm", "image": "eclipse-temurin:8-jre"}]})
        out = run(cgroup_v2_runtime, [floating], pools=[pool(effectiveCgroupMode="EFFECTIVE_CGROUP_MODE_V1")])
        self.assertEqual(out["risks"], [])
        self.assertIn("(JDK 8 with no update in the tag) on pool default-pool, which GKE migrates cgroup v1 pools to v2 from 1.33 at the target; the tag does not say whether it reads cgroup v2", out["unknown"][0]["detail"])
        out = run(cgroup_v2_runtime, [self.JVM], pools=[pool(effectiveCgroupMode="EFFECTIVE_CGROUP_MODE_V1")], target=None)
        self.assertIn("no target to decide whether the pool moves to cgroup v2", out["unknown"][0]["detail"])
        out = run(cgroup_v2_runtime, [], read_errors=WORKLOADS_UNREAD)
        self.assertEqual([u["object"] for u in out["unknown"]], [None])

    def test_runtime_verdicts(self):
        below, ok, unversioned = cgroup_v2_runtime.RUNTIME_BELOW, cgroup_v2_runtime.RUNTIME_OK, cgroup_v2_runtime.RUNTIME_UNVERSIONED
        self.assertEqual(cgroup_v2_runtime.runtime_verdict("openjdk:11.0.12-jre")[0], below)
        self.assertEqual(cgroup_v2_runtime.runtime_verdict("eclipse-temurin:11.0.20_8-jre")[0], ok)
        self.assertEqual(cgroup_v2_runtime.runtime_verdict("eclipse-temurin:8u372-b07-jre")[0], ok)
        self.assertEqual(cgroup_v2_runtime.runtime_verdict("openjdk:17")[0], ok)
        self.assertEqual(cgroup_v2_runtime.runtime_verdict("adoptopenjdk:14-jre-hotspot"), (below, "JDK 14, which predates cgroup v2 support (from JDK 15)"))
        self.assertEqual(cgroup_v2_runtime.runtime_verdict("openjdk:11")[0], unversioned)
        self.assertEqual(cgroup_v2_runtime.runtime_verdict("openjdk:latest")[0], unversioned)
        self.assertEqual(cgroup_v2_runtime.runtime_verdict("mcr.microsoft.com/dotnet/aspnet:3.1"), (below, ".NET 3.1, below 5.0"))
        self.assertEqual(cgroup_v2_runtime.runtime_verdict("mcr.microsoft.com/dotnet/aspnet:8.0")[0], ok)
        self.assertEqual(cgroup_v2_runtime.runtime_verdict("mcr.microsoft.com/dotnet/runtime:latest")[0], unversioned)
        # .NET Framework runs in Windows containers only, where there is no cgroup to read.
        self.assertIsNone(cgroup_v2_runtime.runtime_verdict("mcr.microsoft.com/dotnet/framework/aspnet:4.8-windowsservercore-ltsc2022"))
        self.assertIsNone(cgroup_v2_runtime.runtime_verdict("microsoft/dotnet-framework:4.8"))
        self.assertIsNone(cgroup_v2_runtime.runtime_verdict("busybox:1.36"))
        self.assertIsNone(cgroup_v2_runtime.runtime_verdict("registry.k8s.io/pause:3.10"))

    def test_one_note_per_pool_and_clean_runtimes(self):
        out = run(cgroup_v2_runtime, [self.JVM], pools=[pool("a", effectiveCgroupMode="EFFECTIVE_CGROUP_MODE_V2"), pool("b", effectiveCgroupMode="EFFECTIVE_CGROUP_MODE_V2"), pool("c", effectiveCgroupMode="EFFECTIVE_CGROUP_MODE_V1")])
        self.assertEqual(([f["pool"] for f in out["risks"]], len(out["notes"])), (["c"], 2))
        items = [
            template("Deployment", "modern", {"containers": [{"name": "jvm", "image": "eclipse-temurin:21-jre"}]}),
            template("Deployment", "old-but-system", {"containers": [{"name": "jvm", "image": "openjdk:8u212-jre"}]}, namespace="kube-system"),
        ]
        self.assertEqual(run(cgroup_v2_runtime, items, pools=[pool(effectiveCgroupMode="EFFECTIVE_CGROUP_MODE_V1")]), rules.new_result())


class GroupOomKillTest(unittest.TestCase):
    WORKER = template("Deployment", "multi-process-worker", {"containers": [busybox(["sh", "-c", "sleep infinity & sleep infinity & wait"], name="supervisor")]})

    def test_forking_shell_on_a_pool_moving_to_v2_is_a_risk_marked_heuristic(self):
        out = run(group_oom_kill, [self.WORKER], pools=[pool(effectiveCgroupMode="EFFECTIVE_CGROUP_MODE_V1")])
        self.assertEqual(out["blocking"], [])
        finding = out["risks"][0]
        self.assertTrue(finding["heuristic"])
        self.assertEqual(finding["shape"], "runs a shell script that forks a background job")
        self.assertEqual(group_oom_kill.describe(finding), "Deployment seeded-shapes/multi-process-worker: container supervisor runs a shell script that forks a background job (heuristic: the process count is not readable from the API) on pool default-pool, which GKE migrates cgroup v1 pools to v2 from 1.33 at the target; a kubelet at 1.34 then sets memory.oom.group and an OOM kill takes every process in the container")

    def test_kubelet_crossing_1_28_on_a_v2_pool_is_a_risk_and_past_it_a_note(self):
        out = run(group_oom_kill, [self.WORKER], pools=[pool(version="1.27.9-gke.1", effectiveCgroupMode="EFFECTIVE_CGROUP_MODE_V2")], target=parse_version("1.29.0-gke.1"))
        self.assertIn("on cgroup v2 with a kubelet at 1.27.9-gke.1; from 1.28 the kubelet sets memory.oom.group", out["risks"][0]["detail"])
        out = run(group_oom_kill, [self.WORKER], pools=[pool(effectiveCgroupMode="EFFECTIVE_CGROUP_MODE_V2")])
        self.assertEqual(out["risks"], [])
        self.assertEqual(out["notes"], ["Deployment seeded-shapes/multi-process-worker container supervisor runs a shell script that forks a background job on pool default-pool, on cgroup v2 with a kubelet at 1.33.4-gke.1000, which already group-kills; not an upgrade risk"])

    def test_single_process_oom_kill_an_unread_mode_and_a_failed_workload_read(self):
        out = run(group_oom_kill, [self.WORKER], pools=[pool(effectiveCgroupMode="EFFECTIVE_CGROUP_MODE_V1", linuxNodeConfig={"singleProcessOomKill": True})])
        self.assertEqual(out["risks"], [])
        self.assertIn("whose node config sets singleProcessOOMKill; not an upgrade risk", out["notes"][0])
        out = run(group_oom_kill, [self.WORKER], pools=[pool()])
        self.assertEqual(out["risks"], [])
        self.assertIn("the pool record carries no effectiveCgroupMode", out["unknown"][0]["detail"])
        out = run(group_oom_kill, [], read_errors=WORKLOADS_UNREAD)
        self.assertEqual([u["object"] for u in out["unknown"]], [None])

    def test_shapes_quoting_and_flag_groups(self):
        shape = group_oom_kill.multi_process_shape
        self.assertEqual(shape({"command": ["/usr/bin/supervisord", "-n"]}), "runs supervisord")
        self.assertEqual(shape({"command": ["/init"], "args": ["s6-svscan", "/etc/s6"]}), "runs s6-svscan")
        self.assertEqual(shape({"command": ["tini", "--", "bash", "-c", "nginx & php-fpm & wait"]}), "runs a shell script that forks a background job")
        self.assertEqual(shape({"command": ["sh", "-ec", "nginx & php-fpm & wait"]}), "runs a shell script that forks a background job")
        self.assertEqual(shape({"command": ["bash", "-lc", "a & b & wait"]}), "runs a shell script that forks a background job")
        # Options between the shell and `-c`: a flag with an argument, a lone flag, a long flag.
        self.assertEqual(shape({"command": ["bash", "-o", "pipefail", "-c", "a & b & wait"]}), "runs a shell script that forks a background job")
        self.assertEqual(shape({"command": ["sh", "-e", "-c", "a & b & wait"]}), "runs a shell script that forks a background job")
        self.assertEqual(shape({"command": ["bash", "--norc", "-c", "a & b & wait"]}), "runs a shell script that forks a background job")
        self.assertEqual(shape({"command": ["bash", "-eo", "pipefail", "-c", "a & b & wait"]}), "runs a shell script that forks a background job")
        self.assertEqual(shape({"command": ["bash", "-c", "-o", "pipefail", "a & b & wait"]}), "runs a shell script that forks a background job")
        self.assertEqual(shape({"image": "docker.io/phusion/baseimage:jammy-1.0.1"}), "runs phusion/baseimage, whose entrypoint is a supervisor")
        self.assertIsNone(shape({"command": ["sh", "-c", "curl -fsS 'http://svc/health?x=y&z=1'"]}))
        self.assertIsNone(shape({"command": ["sh", "-c", 'echo "Tom & Jerry"']}))
        self.assertIsNone(shape({"command": ["sh", "-c", "date -u > /tmp/stamp && exec tail -f /dev/null 2>&1"]}))
        self.assertIsNone(shape({"command": ["sh", "-c", "java -version && exec sleep infinity"]}))
        self.assertIsNone(shape({"image": "nginx:1.27", "args": ["nginx", "-g", "daemon off;"]}))
        # shell_forks reads the script as bash does: an expansion is a word, a substitution a command list.
        self.assertIsNone(shape({"command": ["bash", "-c", "((x & 1)) && exec agent"]}))
        self.assertEqual(shape({"command": ["sh", "-c", "echo \"$(start-agent &)\"; wait"]}), "runs a shell script that forks a background job")
        # A script the scanner cannot read is its own shape, which evaluate files as unknown, never clean.
        self.assertEqual(shape({"command": ["sh", "-c", "echo 'oops"]}), group_oom_kill.SHAPE_UNREADABLE)
        self.assertEqual(shape({"image": "docker.io/phusion/baseimage:jammy-1.0.1", "command": ["sh", "-c", "echo 'oops"]}), "runs phusion/baseimage, whose entrypoint is a supervisor")

    def test_an_unreadable_script_is_unknown_where_a_fork_would_have_been_a_risk(self):
        torn = template("Deployment", "torn", {"containers": [busybox(["sh", "-c", "echo 'oops"], name="main")]})
        out = run(group_oom_kill, [torn], pools=[pool(effectiveCgroupMode="EFFECTIVE_CGROUP_MODE_V1")])
        self.assertEqual((out["blocking"], out["risks"], out["notes"]), ([], [], []))
        self.assertEqual([u["reason"] for u in out["unknown"]], ["Deployment seeded-shapes/torn: container main runs a shell script the rule cannot read (an unterminated quote, heredoc or expansion) on pool default-pool; whether it forks a background job, and so whether a group OOM kill at the target takes more than one process, is unknown"])
        self.assertEqual([u["reason"] for u in run(group_oom_kill, [torn], pools=[pool()])["unknown"]], [u["reason"] for u in out["unknown"]])
        # On a pool that already group-kills a fork would change nothing, so nothing is filed.
        self.assertEqual(run(group_oom_kill, [torn], pools=[pool(effectiveCgroupMode="EFFECTIVE_CGROUP_MODE_V2")]), rules.new_result())

    def test_clean_when_nothing_looks_multi_process(self):
        items = [template("Deployment", "single", {"containers": [busybox(["sh", "-c", "tail -f /dev/null"])]})]
        self.assertEqual(run(group_oom_kill, items, pools=[pool(effectiveCgroupMode="EFFECTIVE_CGROUP_MODE_V1")]), rules.new_result())


class NodeImageCoupledAgentTest(unittest.TestCase):
    AGENT = template("DaemonSet", "cni-shaped-agent", {"hostNetwork": True, "containers": [{"name": "agent", "image": "registry.k8s.io/pause:3.10"}], "volumes": [{"name": "cni-conf", "hostPath": {"path": "/etc/cni/net.d", "type": "Directory"}}]})

    def test_host_network_daemonset_mounting_the_cni_directory_is_a_risk(self):
        out = run(node_image_coupled_agent, [self.AGENT])
        self.assertEqual((out["blocking"], out["unknown"]), ([], []))
        finding = out["risks"][0]
        self.assertEqual((finding["kind"], finding["object"], finding["couplings"], finding["pools_below_target"]), ("DaemonSet", "seeded-shapes/cni-shaped-agent", ["mounts /etc/cni/net.d from the node"], ["default-pool"]))
        self.assertEqual(node_image_coupled_agent.describe(finding), "DaemonSet seeded-shapes/cni-shaped-agent: on the node's network and coupled to the node image: mounts /etc/cni/net.d from the node; pool(s) default-pool are below the target and get a new node image in this upgrade")

    def test_pools_at_the_target_no_target_and_a_failed_workload_read(self):
        out = run(node_image_coupled_agent, [self.AGENT], pools=[pool(version=TARGET_TEXT)])
        self.assertIn("no pool is below the target; the next node upgrade changes the image under it", out["risks"][0]["detail"])
        out = run(node_image_coupled_agent, [self.AGENT], target=None)
        self.assertIn("which pools change image needs a target", out["risks"][0]["detail"])
        out = run(node_image_coupled_agent, [self.AGENT], read_errors=WORKLOADS_UNREAD)
        self.assertEqual((out["risks"], [u["detail"] for u in out["unknown"]]), ([], ["DaemonSets not read (daemonsets is forbidden); nothing graded"]))

    def test_label_module_and_modprobe_couplings(self):
        spec = {
            "hostNetwork": True,
            "nodeSelector": {"cloud.google.com/gke-os-distribution": "cos"},
            "containers": [{"name": "a", "image": "x", "command": ["/bin/sh", "-c", "modprobe ip_vs && exec agent"]}],
            "volumes": [{"name": "mods", "hostPath": {"path": "/lib/modules"}}, {"name": "bins", "hostPath": {"path": "/home/kubernetes/bin"}}],
        }
        self.assertEqual(node_image_coupled_agent.couplings(spec), ["selects on node-image label cloud.google.com/gke-os-distribution", "mounts /lib/modules from the node", "mounts /home/kubernetes/bin from the node", "runs modprobe"])
        spec["containers"][0]["command"] = ["modprobe", "ip_vs"]
        self.assertIn("runs modprobe", node_image_coupled_agent.couplings(spec))

    def test_a_module_load_inside_a_shell_script_is_a_risk(self):
        # The program is a word of the `-c` script, not of argv: the shared script walk finds it
        # under a path, after `&&`, and inside args as well as command; a program the script only
        # names in passing (a file called modprobe.conf) is not a load.
        agent = template("DaemonSet", "module-loader", {"hostNetwork": True, "containers": [{"name": "agent", "image": "x", "command": ["/bin/sh", "-c", "modprobe br_netfilter && exec /agent"]}]})
        out = run(node_image_coupled_agent, [agent])
        self.assertEqual((out["blocking"], out["unknown"]), ([], []))
        self.assertEqual((out["risks"][0]["object"], out["risks"][0]["couplings"]), ("seeded-shapes/module-loader", ["runs modprobe"]))
        self.assertIn("coupled to the node image: runs modprobe; pool(s) default-pool are below the target", out["risks"][0]["detail"])
        for command in (["bash", "-ec", "/sbin/insmod /lib/modules/x.ko; /agent"], ["tini", "--", "sh", "-lc", "exec modprobe -a ip_vs ip_vs_rr"]):
            spec = {"hostNetwork": True, "containers": [{"name": "a", "image": "x", "args": command}]}
            self.assertEqual(node_image_coupled_agent.couplings(spec), ["runs " + ("insmod" if "insmod" in command[-1] else "modprobe")], command)
        spec = {"hostNetwork": True, "containers": [{"name": "a", "image": "x", "command": ["/bin/sh", "-c", "cat /etc/modprobe.conf && exec /agent"]}]}
        self.assertEqual(node_image_coupled_agent.couplings(spec), [])
        self.assertEqual(rules.shell_scripts(["sh", "-c"]), [])
        self.assertEqual(rules.shell_words(None), [])
        # A quoted program path, a quoted variable path, a nested shell and a backtick
        # substitution (the older spelling of `$(…)`) name the program too.
        for script in ("'/sbin/modprobe' br_netfilter", 'exec "$BIN/modprobe" x', "bash -c 'modprobe x'", "sh -c \"modprobe x\"", "MOD=`which modprobe`; $MOD br_netfilter", "`/sbin/modprobe` br_netfilter && exec /agent"):
            spec = {"hostNetwork": True, "containers": [{"name": "a", "image": "x", "command": ["/bin/sh", "-c", script]}]}
            self.assertEqual(node_image_coupled_agent.couplings(spec), ["runs modprobe"], script)
        # The script is read behind the shell's other options, and a shell with no `c` flag hands back none.
        self.assertEqual(rules.shell_scripts(["bash", "-o", "pipefail", "-c", "a"]), ["a"])
        self.assertEqual(rules.shell_scripts(["sh", "-e", "-c", "a"]), ["a"])
        self.assertEqual(rules.shell_scripts(["bash", "--norc", "-c", "a"]), ["a"])
        self.assertEqual(rules.shell_scripts(["bash", "--rcfile", "/etc/rc", "-c", "a", "arg0"]), ["a"])
        self.assertEqual(rules.shell_scripts(["bash", "-c", "-e", "a"]), ["a"])
        # An argument-taking option after `-c` is stepped over with its argument, as before it.
        self.assertEqual(rules.shell_scripts(["bash", "-c", "-o", "pipefail", "a"]), ["a"])
        self.assertEqual(rules.shell_scripts(["bash", "-co", "pipefail", "a"]), ["a"])
        self.assertEqual(rules.shell_scripts(["bash", "-c", "--norc", "a"]), ["a"])
        self.assertEqual(rules.shell_scripts(["sh", "-c", "-O", "extglob", "a", "arg0"]), ["a"])
        self.assertEqual(rules.shell_scripts(["bash", "-e", "script.sh"]), [])
        self.assertEqual(rules.shell_scripts(["bash", "-o", "pipefail", "script.sh"]), [])

    def test_clean_without_host_network_without_coupling_or_in_managed_namespaces(self):
        items = [
            template("DaemonSet", "pod-network", {"containers": [busybox()], "volumes": [{"name": "cni-conf", "hostPath": {"path": "/etc/cni/net.d"}}]}),
            template("DaemonSet", "host-network-only", {"hostNetwork": True, "containers": [busybox()], "volumes": [{"name": "logs", "hostPath": {"path": "/var/log"}}]}),
            template("Deployment", "not-an-agent", {"hostNetwork": True, "containers": [busybox()], "volumes": [{"name": "cni-conf", "hostPath": {"path": "/etc/cni/net.d"}}]}),
            template("DaemonSet", "netd", {"hostNetwork": True, "containers": [busybox()], "volumes": [{"name": "cni-conf", "hostPath": {"path": "/etc/cni/net.d"}}]}, namespace="kube-system"),
            template("DaemonSet", "otel", {"hostNetwork": True, "containers": [busybox()], "volumes": [{"name": "mods", "hostPath": {"path": "/lib/modules"}}]}, namespace="config-management-monitoring"),
            template("DaemonSet", "istio-cni", {"hostNetwork": True, "containers": [busybox()], "volumes": [{"name": "cni-conf", "hostPath": {"path": "/etc/cni/net.d"}}]}, namespace="istio-system"),
        ]
        self.assertEqual(run(node_image_coupled_agent, items), rules.new_result())


class GpuDriverMismatchTest(unittest.TestCase):
    def trainer(self, image="docker.io/nvidia/cuda:12.2.0-base-ubuntu22.04", env=None, select=True, extra_selector=None):
        container = {"name": "trainer", "image": image, "resources": {"limits": {"nvidia.com/gpu": "1"}, "requests": {"nvidia.com/gpu": "1"}}}
        if env:
            container["env"] = env
        spec = {"containers": [container]}
        if select:
            spec["nodeSelector"] = {"cloud.google.com/gke-accelerator": "nvidia-l4", **(extra_selector or {})}
        return template("CronJob", "cuda-pinned-trainer", spec)

    def gpu_pool(self, version="1.32.4-gke.1", mode="DEFAULT", image_type="COS_CONTAINERD", accelerator="nvidia-l4"):
        return pool("gpu-pool", version, imageType=image_type, accelerators=[{"acceleratorType": accelerator, "acceleratorCount": "1", "gpuDriverInstallationConfig": {"gpuDriverVersion": mode}}])

    def test_no_pool_carries_the_accelerator_is_a_risk_naming_the_pin(self):
        out = run(gpu_driver_mismatch, [self.trainer()])
        self.assertEqual((out["blocking"], out["unknown"]), ([], []))
        finding = out["risks"][0]
        self.assertEqual((finding["kind"], finding["object"], finding["cuda"]), ("CronJob", "seeded-shapes/cuda-pinned-trainer", ["12.2"]))
        self.assertEqual(gpu_driver_mismatch.describe(finding), "CronJob seeded-shapes/cuda-pinned-trainer: requests nvidia.com/gpu on accelerator nvidia-l4 and no pool in the cluster carries that accelerator; the target driver cannot be read, and the pin image docker.io/nvidia/cuda:12.2.0-base-ubuntu22.04 stays unchecked")

    def test_a_floor_the_pool_already_fails_today_is_a_risk_not_this_upgrades_block(self):
        # DEFAULT is R535 at 1.32 and at 1.33: a CUDA 13.x pin fails on the running node image
        # too, and the upgrade keeps the driver, so the miss is a risk naming both, not a blocker.
        out = run(gpu_driver_mismatch, [self.trainer("nvidia/cuda:13.0.0-runtime-ubuntu24.04")], pools=[self.gpu_pool()], target=parse_version("1.33.0-gke.1"))
        self.assertEqual((out["blocking"], out["unknown"], out["notes"]), ([], [], []))
        finding = out["risks"][0]
        self.assertEqual((finding["cuda"], finding["driver"], finding["running_driver"]), ("13.0", 535, 535))
        self.assertIn("on pool gpu-pool; the running 1.32 node image ships driver R535 (gpuDriverVersion DEFAULT), below the R580 floor for CUDA 13.x, and the target 1.33 node image keeps it: the device does not open today, and the upgrade does not change that", finding["detail"])
        # A driver that moves and stays below the floor is the same risk, naming both branches.
        out = run(gpu_driver_mismatch, [self.trainer("nvidia/cuda:13.0.0-runtime-ubuntu24.04")], pools=[self.gpu_pool(version="1.27.3-gke.1")], target=parse_version("1.28.0-gke.1"))
        self.assertEqual(out["blocking"], [])
        self.assertIn("the running 1.27 node image ships driver R470 (gpuDriverVersion DEFAULT), below the R580 floor for CUDA 13.x, and the target 1.28 node image ships driver R535, below it too: the device does not open today", out["risks"][0]["detail"])

    def test_a_floor_the_target_newly_falls_below_blocks_and_an_unread_running_branch_grades_the_target_alone(self):
        # No row of the table as entered ships an older branch at a later minor, so the shape
        # that newly breaks a served pin is set up on a copy of the COS table.
        with patch.dict(tables.GKE_COS_GPU_DRIVERS, {(1, 33): (470, 580)}):
            out = run(gpu_driver_mismatch, [self.trainer()], pools=[self.gpu_pool()], target=parse_version("1.33.0-gke.1"))
        self.assertEqual((out["risks"], out["unknown"]), ([], []))
        self.assertEqual((out["blocking"][0]["cuda"], out["blocking"][0]["driver"]), ("12.2", 470))
        self.assertNotIn("running_driver", out["blocking"][0])
        self.assertIn("on pool gpu-pool; the target 1.33 node image ships driver R470 (gpuDriverVersion DEFAULT), below the R525 floor for CUDA 12.x; the device does not open", out["blocking"][0]["detail"])
        # A pool whose running minor the table lacks is graded against the target alone, as the
        # catalogue's "Before" line defines the check; so is Autopilot, with no pool to read.
        out = run(gpu_driver_mismatch, [self.trainer("nvidia/cuda:13.0.0-runtime-ubuntu24.04")], pools=[self.gpu_pool(version="1.25.8-gke.1")], target=parse_version("1.33.0-gke.1"))
        self.assertEqual(([f["cuda"] for f in out["blocking"]], out["risks"]), (["13.0"], []))
        self.assertIsNone(gpu_driver_mismatch.driver_today(self.gpu_pool(version="1.25.8-gke.1")))
        self.assertIsNone(gpu_driver_mismatch.driver_today(self.gpu_pool(mode="INSTALLATION_DISABLED")))
        self.assertEqual(gpu_driver_mismatch.driver_today(self.gpu_pool(mode="LATEST")), 580)

    def test_below_the_toolkit_minimum_is_a_risk_and_served_is_a_note(self):
        target = parse_version("1.33.0-gke.1")
        out = run(gpu_driver_mismatch, [self.trainer("pytorch/pytorch:2.6.0-cuda12.8-cudnn9-runtime")], pools=[self.gpu_pool()], target=target)
        self.assertEqual(out["blocking"], [])
        self.assertIn("below the R570 CUDA 12.8 names; the build runs under minor version compatibility", out["risks"][0]["detail"])
        out = run(gpu_driver_mismatch, [self.trainer()], pools=[self.gpu_pool()], target=target)
        self.assertEqual((out["blocking"], out["risks"], out["unknown"]), ([], [], []))
        self.assertEqual(out["notes"], ["CronJob seeded-shapes/cuda-pinned-trainer requests nvidia.com/gpu with image docker.io/nvidia/cuda:12.2.0-base-ubuntu22.04 on pool gpu-pool; the target 1.33 node image ships driver R535 (gpuDriverVersion DEFAULT), which serves CUDA 12.2; forward-compatibility packages inside the image are not readable from the tag"])
        out = run(gpu_driver_mismatch, [self.trainer("nvidia/cuda:13.0.0-runtime-ubuntu24.04")], pools=[self.gpu_pool(mode="LATEST")], target=target)
        self.assertEqual(out["blocking"], [])
        self.assertIn("driver R580 (gpuDriverVersion LATEST), which serves CUDA 13.0", out["notes"][0])

    def test_a_toolkit_the_minimum_table_has_no_row_for_is_a_note_naming_the_row_never_served(self):
        # 13.9 clears CUDA 13's R580 floor on a LATEST pool at 1.33, but the toolkit table ends
        # before it: what the toolkit itself needs is unread, so the note says so.
        self.assertNotIn((13, 9), tables.CUDA_TOOLKIT_MIN_DRIVER)
        out = run(gpu_driver_mismatch, [self.trainer("nvidia/cuda:13.9.0-base-ubuntu24.04")], pools=[self.gpu_pool(mode="LATEST")], target=parse_version("1.33.0-gke.1"))
        self.assertEqual((out["blocking"], out["risks"], out["unknown"]), ([], [], []))
        self.assertEqual(len(out["notes"]), 1)
        self.assertIn("ships driver R580 (gpuDriverVersion LATEST), above the R580 floor for CUDA 13.x, but the toolkit table has no row for CUDA 13.9", out["notes"][0])
        self.assertNotIn("serves", out["notes"][0])

    def test_autopilot_reads_the_driver_from_the_cos_table_and_the_selector(self):
        target = parse_version("1.33.0-gke.1")
        out = run(gpu_driver_mismatch, [self.trainer("nvidia/cuda:13.0.0-runtime-ubuntu24.04")], pools=[], target=target, autopilot=True)
        self.assertIn("on the node Autopilot provisions; the target 1.33 node image ships driver R535 (cloud.google.com/gke-gpu-driver-version default), below the R580 floor for CUDA 13.x", out["blocking"][0]["detail"])
        latest = self.trainer("nvidia/cuda:13.0.0-runtime-ubuntu24.04", extra_selector={"cloud.google.com/gke-gpu-driver-version": "latest"})
        out = run(gpu_driver_mismatch, [latest], pools=[], target=target, autopilot=True)
        self.assertEqual(out["blocking"], [])
        self.assertIn("driver R580 (cloud.google.com/gke-gpu-driver-version latest), which serves CUDA 13.0", out["notes"][0])
        # Past the table on Autopilot: the same risk as on a Standard pool, naming the page.
        out = run(gpu_driver_mismatch, [self.trainer()], pools=[], autopilot=True)
        self.assertEqual((out["blocking"], out["unknown"]), ([], []))
        self.assertIn("on the node Autopilot provisions; the COS_CONTAINERD driver table has no row for 1.34 (its rows end at 1.33); verify the driver floor the pin needs against GKE's GPU page (https://docs.cloud.google.com/kubernetes-engine/docs/how-to/gpus)", out["risks"][0]["detail"])

    def test_a_target_past_the_driver_table_is_a_risk_naming_the_page_not_an_unknown(self):
        # The page has no row for 1.34 or later (read 2026-10-20), so every GPU member graded
        # against a current channel default would otherwise be unknown on every run.
        out = run(gpu_driver_mismatch, [self.trainer()], pools=[self.gpu_pool()])
        self.assertEqual((out["blocking"], out["unknown"], out["notes"]), ([], [], []))
        finding = out["risks"][0]
        self.assertEqual((finding["object"], finding["pool"], finding["table_ends_at"]), ("seeded-shapes/cuda-pinned-trainer", "gpu-pool", "1.33"))
        self.assertEqual(gpu_driver_mismatch.describe(finding), "CronJob seeded-shapes/cuda-pinned-trainer: requests nvidia.com/gpu with image docker.io/nvidia/cuda:12.2.0-base-ubuntu22.04 on pool gpu-pool; the COS_CONTAINERD driver table has no row for 1.34 (its rows end at 1.33); verify the driver floor the pin needs against GKE's GPU page (https://docs.cloud.google.com/kubernetes-engine/docs/how-to/gpus)")
        out = run(gpu_driver_mismatch, [self.trainer()], pools=[self.gpu_pool(image_type="UBUNTU_CONTAINERD")], target=parse_version("1.36.4-gke.1"))
        self.assertIn("the UBUNTU_CONTAINERD driver table has no row for 1.36 (its rows end at 1.33)", out["risks"][0]["detail"])
        # The last row itself is graded from the table, and a target below the first row stays unknown.
        out = run(gpu_driver_mismatch, [self.trainer()], pools=[self.gpu_pool()], target=parse_version("1.33.9-gke.1"))
        self.assertEqual((out["risks"], out["unknown"], len(out["notes"])), ([], [], 1))
        out = run(gpu_driver_mismatch, [self.trainer()], pools=[self.gpu_pool()], target=parse_version("1.25.0-gke.1"))
        self.assertIn("the COS_CONTAINERD driver table covers 1.26 to 1.33, not the target 1.25", out["unknown"][0]["detail"])

    def test_unknown_when_the_driver_is_not_in_the_record_or_the_table(self):
        out = run(gpu_driver_mismatch, [self.trainer()], pools=[self.gpu_pool()], target=parse_version("1.25.0-gke.1"))
        self.assertIn("the COS_CONTAINERD driver table covers 1.26 to 1.33, not the target 1.25", out["unknown"][0]["detail"])
        self.assertTrue(out["unknown"][0]["reason"].startswith("CronJob seeded-shapes/cuda-pinned-trainer: "))
        out = run(gpu_driver_mismatch, [self.trainer()], pools=[self.gpu_pool(mode="INSTALLATION_DISABLED")], target=parse_version("1.33.0-gke.1"))
        self.assertIn("the operator-installed driver is not in the record", out["unknown"][0]["detail"])
        out = run(gpu_driver_mismatch, [self.trainer()], pools=[self.gpu_pool(mode=None)], target=parse_version("1.33.0-gke.1"))
        self.assertIn("the pool record carries no gpuDriverVersion", out["unknown"][0]["detail"])
        out = run(gpu_driver_mismatch, [self.trainer()], pools=[self.gpu_pool(image_type="UBUNTU_CONTAINERD")], target=parse_version("1.30.0-gke.1"))
        self.assertIn("the UBUNTU_CONTAINERD driver table covers 1.31 to 1.33, not the target 1.30", out["unknown"][0]["detail"])
        out = run(gpu_driver_mismatch, [self.trainer("nvidia/cuda:10.2-base")], pools=[self.gpu_pool()], target=parse_version("1.33.0-gke.1"))
        self.assertIn("CUDA 10.2 is outside the table (majors 11, 12, 13)", out["unknown"][0]["detail"])
        out = run(gpu_driver_mismatch, [self.trainer()], pools=[self.gpu_pool()], target=None)
        self.assertIn("no target to read the driver at", out["unknown"][0]["detail"])
        out = run(gpu_driver_mismatch, [], read_errors=WORKLOADS_UNREAD)
        self.assertEqual([u["object"] for u in out["unknown"]], [None])

    def test_env_pins_are_the_two_named_variables_only(self):
        target = parse_version("1.33.0-gke.1")
        arch_list = self.trainer("nvcr.io/nvidia/pytorch:24.01-py3", env=[{"name": "TORCH_CUDA_ARCH_LIST", "value": "8.0;8.6;9.0"}, {"name": "TF_CUDA_COMPUTE_CAPABILITIES", "value": "7.0,8.0"}])
        out = run(gpu_driver_mismatch, [arch_list], pools=[self.gpu_pool()], target=target)
        self.assertEqual(out["unknown"], [])
        self.assertIn("names no CUDA version", out["risks"][0]["detail"])
        out = run(gpu_driver_mismatch, [self.trainer("my/trainer:1", env=[{"name": "CUDA_VERSION", "value": "12.4.1"}])], pools=[self.gpu_pool()], target=target)
        self.assertIn("with env CUDA_VERSION=12.4.1 on pool gpu-pool", out["risks"][0]["detail"])
        out = run(gpu_driver_mismatch, [self.trainer("my/trainer:1", env=[{"name": "NVIDIA_REQUIRE_CUDA", "value": "cuda>=12.2 brand=tesla,driver>=470,driver<471"}])], pools=[self.gpu_pool()], target=target)
        self.assertIn("which serves CUDA 12.2", out["notes"][0])
        out = run(gpu_driver_mismatch, [self.trainer("tensorflow/tensorflow:2.15.0-gpu")], pools=[self.gpu_pool()], target=target)
        self.assertEqual(gpu_driver_mismatch.describe(out["risks"][0]), "CronJob seeded-shapes/cuda-pinned-trainer: requests nvidia.com/gpu and its image tensorflow/tensorflow:2.15.0-gpu names no CUDA version; the driver it needs cannot be read from the tag")

    def test_clean_without_a_gpu_request_and_pool_selection(self):
        plain = template("Deployment", "cpu-only", {"containers": [{"name": "c", "image": "nvidia/cuda:13.0.0-base"}]})
        self.assertEqual(run(gpu_driver_mismatch, [plain], pools=[self.gpu_pool()], target=parse_version("1.33.0-gke.1")), rules.new_result())
        other = self.trainer("nvidia/cuda:13.0.0-base")
        other["spec"]["jobTemplate"]["spec"]["template"]["spec"]["nodeSelector"] = {"cloud.google.com/gke-accelerator": "nvidia-tesla-t4"}
        out = run(gpu_driver_mismatch, [other], pools=[self.gpu_pool()], target=parse_version("1.33.0-gke.1"))
        self.assertEqual(out["blocking"], [])
        self.assertIn("no pool in the cluster carries that accelerator", out["risks"][0]["detail"])


class RetiredRegistryTest(unittest.TestCase):
    LEGACY = template("Deployment", "legacy-registry-pull", {"containers": [{"name": "pause", "image": "k8s.gcr.io/pause:3.9", "imagePullPolicy": "Always"}]})

    def test_retired_host_is_a_risk_naming_the_image_the_host_and_the_rebuilt_pools(self):
        out = run(retired_registry, [self.LEGACY])
        self.assertEqual((out["blocking"], out["unknown"]), ([], []))
        finding = out["risks"][0]
        self.assertEqual((finding["grade"], finding["object"], finding["hosts"], finding["pools_below_target"]), ("risk", "seeded-shapes/legacy-registry-pull", ["k8s.gcr.io/"], ["default-pool"]))
        self.assertEqual(retired_registry.describe(finding), "Deployment seeded-shapes/legacy-registry-pull: container pause pulls k8s.gcr.io/pause:3.9 from k8s.gcr.io (frozen 2023-04-03, a redirect to registry.k8s.io since 2023-03-20); a rebuilt node pulls it again from a host that stopped publishing; pool(s) default-pool are below the target and are rebuilt in this upgrade")

    def test_pools_at_the_target_and_no_target_keep_the_grade(self):
        out = run(retired_registry, [self.LEGACY], pools=[pool(version=TARGET_TEXT)])
        self.assertEqual(out["blocking"], [])
        self.assertIn("no pool is below the target; the next node upgrade pulls it again", out["risks"][0]["detail"])
        out = run(retired_registry, [self.LEGACY], target=None)
        self.assertIn("which pools are rebuilt needs a target", out["risks"][0]["detail"])

    def test_every_retired_host_including_init_containers_and_a_failed_workload_read(self):
        items = [
            template("StatefulSet", "tiller", {"initContainers": [{"name": "init", "image": "gcr.io/kubernetes-helm/tiller:v2.17.0"}], "containers": [busybox()]}),
            template("DaemonSet", "old-agent", {"containers": [{"name": "a", "image": "gcr.io/google_containers/pause-amd64:3.1"}]}),
            template("CronJob", "old-job", {"containers": [{"name": "j", "image": "gcr.io/google-containers/busybox:1.27"}]}),
        ]
        out = run(retired_registry, items)
        self.assertEqual([(f["kind"], f["hosts"]) for f in out["risks"]], [("StatefulSet", ["gcr.io/kubernetes-helm/"]), ("DaemonSet", ["gcr.io/google_containers/"]), ("CronJob", ["gcr.io/google-containers/"])])
        self.assertIn("Helm 2's registry, unsupported since 2020-11-13", out["risks"][0]["detail"])
        out = run(retired_registry, items[:1], read_errors=WORKLOADS_UNREAD)
        self.assertEqual(([u["object"] for u in out["unknown"]], len(out["risks"])), ([None], 1))

    def test_clean_hosts_and_system_namespaces(self):
        items = [
            template("Deployment", "current", {"containers": [{"name": "c", "image": "registry.k8s.io/pause:3.10"}, {"name": "d", "image": "busybox:1.36"}, {"name": "e", "image": "us-docker.pkg.dev/p/r/i:1"}]}),
            template("Deployment", "kube-proxy", {"containers": [{"name": "c", "image": "k8s.gcr.io/kube-proxy:v1.20.0"}]}, namespace="kube-system"),
        ]
        self.assertEqual(run(retired_registry, items), rules.new_result())


if __name__ == "__main__":
    unittest.main()
