#!/usr/bin/env python3
"""Unit tests for readiness_rules/intree_volumes.py (catalogue entry 19)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import readiness_rules as shared  # noqa: E402
from readiness_rules import intree_volumes  # noqa: E402
from test_readiness_rules import TARGET, claim_volume, cluster, deployment_pod, read, template  # noqa: E402
from test_rule_capacity import context_for  # noqa: E402


def pv(name, pd_name="disk-1", claim=("seeded-shapes", "intree-pd"), phase="Bound", csi=False):
    spec = {"capacity": {"storage": "10Gi"}}
    if csi:
        spec["csi"] = {"driver": "pd.csi.storage.gke.io", "volumeHandle": pd_name}
    else:
        spec["gcePersistentDisk"] = {"pdName": pd_name, "fsType": "ext4"}
    if claim:
        spec["claimRef"] = {"namespace": claim[0], "name": claim[1]}
    return {"kind": "PersistentVolume", "metadata": {"name": name}, "spec": spec, "status": {"phase": phase}}


def pvc(name, volume, namespace="seeded-shapes"):
    return {"kind": "PersistentVolumeClaim", "metadata": {"namespace": namespace, "name": name}, "spec": {"volumeName": volume}, "status": {"phase": "Bound"}}


def items_for(with_pod=True, with_template=True, with_claim=True):
    items = [pv("intree-pd", pd_name="seeded-a-intree-pd"), pv("csi-pd", csi=True, claim=("other", "csi"))]
    if with_claim:
        items.append(pvc("intree-pd", "intree-pd"))
    if with_template:
        items.append(template("Deployment", "intree-pd-reader", {"containers": [{"name": "c"}], "volumes": [claim_volume("intree-pd")]}))
    if with_pod:
        items.append(deployment_pod("seeded-shapes", "intree-pd-reader", "n1", volumes=[claim_volume("intree-pd")]))
    return items


ADDON_OFF = {"gcePersistentDiskCsiDriverConfig": {}}
ADDON_ON = {"gcePersistentDiskCsiDriverConfig": {"enabled": True}}


def evaluate(record, items, **read_kw):
    return intree_volumes.evaluate(record, {}, read(items=items, **read_kw), TARGET, context_for(record))


class InTreeVolumesRuleTest(unittest.TestCase):
    def test_contract_attributes(self):
        self.assertEqual((intree_volumes.RULE_ID, intree_volumes.ENTRY, intree_volumes.CAN_BLOCK), ("in-tree-volumes", 19, True))

    def test_addon_off_with_a_consumer_blocks(self):
        record = cluster(name="seeded-a", addonsConfig=ADDON_OFF)
        out = evaluate(record, items_for())
        self.assertEqual((out["risks"], out["unknown"]), ([], []))
        finding = out["blocking"][0]
        self.assertEqual((finding["pv"], finding["disk"], finding["claim"], finding["phase"], finding["addon_enabled"]), ("intree-pd", "seeded-a-intree-pd", "seeded-shapes/intree-pd", "Bound", False))
        self.assertEqual(finding["workloads"], ["Deployment seeded-shapes/intree-pd-reader"])
        self.assertNotIn("migrated_to", finding)
        text = intree_volumes.describe(finding)
        self.assertIn("PersistentVolume intree-pd (in-tree gcePersistentDisk seeded-a-intree-pd, claim seeded-shapes/intree-pd, Bound)", text)
        self.assertIn("the attach fails when a drain moves Deployment seeded-shapes/intree-pd-reader; enable the add-on", text)

    def test_addon_on_is_a_risk_naming_the_migration(self):
        record = cluster(name="seeded-a", addonsConfig=ADDON_ON)
        out = evaluate(record, items_for())
        self.assertEqual((out["blocking"], len(out["risks"])), ([], 1))
        self.assertIn("attaches through CSI migration for Deployment seeded-shapes/intree-pd-reader; move it to pd.csi.storage.gke.io", intree_volumes.describe(out["risks"][0]))

    def test_addon_off_without_a_consumer_is_a_risk_and_a_template_consumer_counts(self):
        record = cluster(name="seeded-a")
        out = evaluate(record, items_for(with_pod=False, with_template=False))
        self.assertEqual(out["blocking"], [])
        self.assertIn("nothing mounts it now", intree_volumes.describe(out["risks"][0]))
        out = evaluate(record, items_for(with_pod=False))
        self.assertEqual(out["blocking"][0]["workloads"], ["Deployment seeded-shapes/intree-pd-reader"])

    def test_unbound_volume(self):
        record = cluster(name="seeded-a", addonsConfig=ADDON_ON)
        out = evaluate(record, [pv("loose", claim=None, phase="Available")])
        self.assertEqual((out["risks"][0]["claim"], out["risks"][0]["claim_live"], out["notes"]), (None, False, []))
        self.assertIn("claim unbound, Available", intree_volumes.describe(out["risks"][0]))

    def test_a_released_volume_whose_claim_rebound_elsewhere_counts_no_consumer(self):
        # A Retain-policy volume keeps the claimRef of a claim since deleted; the claim recreated
        # under that name is bound to a CSI volume, and the Deployment mounting it mounts that
        # one. The stale volume is graded as unmounted, a risk, and the claimRef is a note.
        record = cluster(name="seeded-a", addonsConfig=ADDON_OFF)
        items = [pv("intree-pd", pd_name="seeded-a-intree-pd", phase="Released"), pv("csi-pd", csi=True, claim=("seeded-shapes", "intree-pd")), pvc("intree-pd", "csi-pd")]
        items += items_for(with_claim=False)[2:]
        out = evaluate(record, items)
        self.assertEqual((out["blocking"], out["unknown"]), ([], []))
        finding = out["risks"][0]
        self.assertEqual((finding["pv"], finding["claim"], finding["claim_live"], finding["phase"], finding["workloads"]), ("intree-pd", "seeded-shapes/intree-pd", False, "Released", []))
        text = intree_volumes.describe(finding)
        self.assertIn("(in-tree gcePersistentDisk seeded-a-intree-pd, claim seeded-shapes/intree-pd (stale), Released) with the PD CSI driver add-on off: nothing mounts it now", text)
        self.assertEqual(out["notes"], ["PersistentVolume intree-pd (Released) still names claim seeded-shapes/intree-pd, which is bound to PersistentVolume csi-pd; the workloads mounting that claim do not mount this volume"])
        # The same volume with the add-on on is the migration risk, naming no workload.
        out = evaluate(cluster(name="seeded-a", addonsConfig=ADDON_ON), items)
        self.assertIn("(stale), Released) attaches through CSI migration; move it to pd.csi.storage.gke.io", intree_volumes.describe(out["risks"][0]))

    def test_a_stale_claim_that_no_longer_exists_or_is_not_bound_to_the_volume(self):
        record = cluster(name="seeded-a", addonsConfig=ADDON_OFF)
        # The claim is gone: the template mounting a claim of that name waits on a claim, not on this volume.
        out = evaluate(record, [pv("intree-pd", phase="Released")] + items_for(with_claim=False)[2:])
        self.assertEqual((out["blocking"], out["risks"][0]["workloads"]), ([], []))
        self.assertEqual(out["notes"], ["PersistentVolume intree-pd (Released) still names claim seeded-shapes/intree-pd, which no longer exists; the workloads mounting that claim do not mount this volume"])
        # The claim names this volume but the volume is not Bound to it (a uid mismatch leaves it Released).
        out = evaluate(record, [pv("intree-pd", phase="Released"), pvc("intree-pd", "intree-pd")] + items_for(with_claim=False)[2:])
        self.assertEqual(out["blocking"], [])
        self.assertIn("which is not bound to it;", out["notes"][0])
        # A Bound volume whose claim the read did not return is live on the phase alone.
        out = evaluate(record, items_for(with_claim=False), read_errors={"pvc": "persistentvolumeclaims is forbidden"})
        self.assertEqual((out["blocking"][0]["claim_live"], out["blocking"][0]["workloads"], out["notes"]), (True, ["Deployment seeded-shapes/intree-pd-reader"], []))
        # A Bound volume whose claim is absent from a read that returned the claims is stale (a transient).
        out = evaluate(record, items_for(with_claim=False))
        self.assertEqual((out["blocking"], out["risks"][0]["claim_live"]), ([], False))

    def test_clean_and_unread(self):
        record = cluster(name="seeded-a")
        self.assertEqual(evaluate(record, [pv("csi-pd", csi=True)]), shared.new_result())
        refused = evaluate(record, items_for(), read_errors={"pv": "persistentvolumes is forbidden"})
        self.assertEqual((refused["blocking"], refused["risks"]), ([], []))
        self.assertEqual(refused["unknown"], ["PersistentVolumes not read (persistentvolumes is forbidden), so in-tree volumes are unknown"])
        nothing = intree_volumes.evaluate(record, {}, read(error="get-credentials failed"), TARGET, context_for(record))
        self.assertIn("PersistentVolumes not read (get-credentials failed)", nothing["unknown"][0])
        # Pods unread: the templates still ground the blocker, and the gap is named.
        partial = evaluate(record, items_for(with_pod=False), read_errors={"pods": "pods is forbidden"})
        self.assertEqual(partial["blocking"][0]["workloads"], ["Deployment seeded-shapes/intree-pd-reader"])
        self.assertEqual(partial["unknown"], ["Pods not read (pods is forbidden), so a pod mounting an in-tree volume outside a template is unseen"])
        self.assertEqual(evaluate(record, [pv("csi-pd", csi=True)], read_errors={"pods": "x"}), shared.new_result())


if __name__ == "__main__":
    unittest.main()
