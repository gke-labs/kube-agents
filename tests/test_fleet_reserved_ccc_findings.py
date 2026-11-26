# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""What the audits file on the seeded fleet's ComputeClass `reserved-ccc`
(bench/tf/fleet/defects-c.tf, role missing-reservation-compute-class).

The class names a reservation that does not exist. Its shape was chosen so
that the stockout audit files exactly one finding on it, the status that GKE
writes. These tests ask the collectors themselves, with the class as GKE holds
it, so a change of a check that adds a second finding on the fixture, or that
drops the one, fails here and not on a pool project.
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "agents" / "platform" / "skills" / "fleet-audit" / "scripts"
# Slot c's other class as GKE reports it, captured live: `general-ccc`.
GENERAL_CAPTURE = SCRIPTS / "testdata" / "computeclass_taint_missing.json"

sys.path.insert(0, str(SCRIPTS))

import collect  # noqa: E402
import fleet_stockout  # noqa: E402

# The class as GKE holds it (read on GKE 1.36): the fields that the stack
# plants, the field that GKE adds, and the status for the missing reservation.
RESERVED = {
    "apiVersion": "cloud.google.com/v1",
    "kind": "ComputeClass",
    "metadata": {"name": "reserved-ccc", "labels": {"managed-by": "kube-agents-seeded-fleet"}},
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
            {
                "type": "CrdMisconfigured",
                "status": "True",
                "reason": "ReservationNotFound",
                "message": 'Reservation "seeded-absent-reservation" either does not exist or not accessible.',
            },
            {"type": "Health", "status": "False", "reason": "Health", "message": "Crd is not healthy."},
        ]
    },
}
# Slot c's one node pool: the class label of `general-ccc`, and no taint.
DEFAULT_POOL = {"name": "default-pool", "config": {"machineType": "e2-standard-2", "labels": {"cloud.google.com/compute-class": "general-ccc"}}}
# The same pool as the obtainability audit reads it: running, autoscaling on.
SLOT_C_POOL = {**DEFAULT_POOL, "status": "RUNNING", "autoscaling": {"enabled": True, "minNodeCount": 1, "maxNodeCount": 1}}
# Slot c's Deployment with no class selector (role untargeted-compute-class-workload).
APP_WORKER_A = {
    "kind": "Deployment",
    "name": "app-worker-a",
    "ns": "seeded-compute-class",
    "template": {"containers": [{"name": "worker", "resources": {"requests": {"cpu": "10m", "memory": "8Mi"}}}]},
}


def _reserved(**spec) -> dict:
    """The class with some fields of its spec replaced."""
    changed = copy.deepcopy(RESERVED)
    changed["spec"].update(spec)
    return changed


class StockoutFindingsTest(unittest.TestCase):
    def test_the_status_of_the_class_is_one_finding_with_the_reservation_in_its_text(self):
        hit = fleet_stockout.check_compute_class_status(RESERVED)
        self.assertEqual(hit["object"], "ComputeClass/reserved-ccc")
        self.assertIn("CrdMisconfigured", hit["excerpt"])
        self.assertIn("ReservationNotFound", hit["excerpt"])
        self.assertIn("seeded-absent-reservation", hit["excerpt"])

    def test_the_two_classes_of_slot_c_each_get_their_own_finding(self):
        general = json.loads(GENERAL_CAPTURE.read_text(encoding="utf-8"))
        hits = [fleet_stockout.check_compute_class_status(cc) for cc in (general, RESERVED)]
        self.assertEqual([hit["object"] for hit in hits], ["ComputeClass/general-ccc", "ComputeClass/reserved-ccc"])
        self.assertNotIn("ReservationNotFound", hits[0]["excerpt"])
        self.assertNotEqual(hits[0]["excerpt"], hits[1]["excerpt"])

    def test_a_class_with_no_status_yet_is_not_judged(self):
        # The minutes after the plant: GKE did not write the status yet.
        young = copy.deepcopy(RESERVED)
        del young["status"]
        self.assertIsNone(fleet_stockout.check_compute_class_status(young))

    def test_the_second_rule_is_a_fallback_for_each_zone_count(self):
        # A machine type of another family and another size: the two rules
        # differ in two dimensions, so the class has a fallback.
        for zones in (1, 3, None):
            with self.subTest(zones=zones):
                self.assertIsNone(fleet_stockout.check_ccc_missing_fallbacks(RESERVED, zones))

    def test_a_second_rule_of_the_same_family_has_no_fallback(self):
        # Why the stack does not use `machineFamily: n2` for the second rule.
        same_family = _reserved(priorities=[RESERVED["spec"]["priorities"][0], {"machineFamily": "n2"}])
        self.assertIsNotNone(fleet_stockout.check_ccc_missing_fallbacks(same_family, 1))

    def test_a_first_rule_near_the_second_has_no_fallback(self):
        # Why the first rule is `n2-standard-2`: with the size or the family
        # of the second rule, the two rules differ in one dimension only.
        for machine in ("n2-standard-4", "e2-standard-2"):
            with self.subTest(machine=machine):
                first = {**RESERVED["spec"]["priorities"][0], "machineType": machine}
                near = _reserved(priorities=[first, RESERVED["spec"]["priorities"][1]])
                self.assertIsNotNone(fleet_stockout.check_ccc_missing_fallbacks(near, 1))

    def test_with_auto_creation_on_the_class_is_not_an_unbacked_manual_class(self):
        self.assertIsNone(fleet_stockout.check_ccc_unbacked_manual_fallback(RESERVED, [DEFAULT_POOL]))

    def test_with_auto_creation_off_the_class_gets_a_second_finding(self):
        # Why the stack has auto-creation on in this class.
        off = _reserved(nodePoolAutoCreation={"enabled": False})
        hit = fleet_stockout.check_ccc_unbacked_manual_fallback(off, [DEFAULT_POOL])
        self.assertEqual(hit["object"], "ComputeClass/reserved-ccc")

    def test_no_other_class_check_files_the_class(self):
        self.assertIsNone(fleet_stockout.check_ccc_no_ondemand_floor(RESERVED, referenced_by_inference=False))
        self.assertEqual(fleet_stockout.check_ccc_large_vm_scarcity(RESERVED), [])
        self.assertIsNone(fleet_stockout.check_ccc_priority_starvation(RESERVED))
        self.assertIsNone(fleet_stockout.check_ccc_mixed_disk_generations(RESERVED, stateful_referencing=False))
        self.assertIsNone(fleet_stockout.check_ccc_hyperdisk_incompatible(RESERVED, uses_hyperdisk=False))
        # The reservation check of the class loop files only an affinity that
        # goes round the rules of the class. `Specific` is not one.
        self.assertIsNone(fleet_stockout.check_reservation_affinity(RESERVED))


class ObtainabilityEffectTest(unittest.TestCase):
    """The obtainability audit's 3.24 on slot c's Deployment `app-worker-a`.
    It names one target class only when the cluster has exactly one untainted
    general-purpose class. The taint of this class keeps it out of that
    count, so the finding names `general-ccc` with or without this class."""

    def finding(self, classes: list[dict]) -> dict:
        context = {
            "compute_classes": classes,
            "node_pools": [SLOT_C_POOL],
            "nodes": [{"metadata": {"labels": {"cloud.google.com/gke-nodepool": "default-pool"}}}],
            "namespaces": [{"metadata": {"name": "seeded-compute-class", "labels": {"managed-by": "kube-agents-seeded-fleet"}}}],
        }
        return collect.check_untargeted_compute_class_workload(APP_WORKER_A, context)

    def test_with_general_ccc_alone_the_finding_names_it(self):
        general = json.loads(GENERAL_CAPTURE.read_text(encoding="utf-8"))
        hit = self.finding([general])
        self.assertEqual(hit["object"], "Deployment/app-worker-a")
        self.assertEqual(hit["single_compute_class"], "general-ccc")

    def test_with_the_class_beside_general_ccc_the_finding_is_the_same(self):
        general = json.loads(GENERAL_CAPTURE.read_text(encoding="utf-8"))
        self.assertEqual(self.finding([general, RESERVED]), self.finding([general]))

    def test_the_class_with_no_status_yet_gives_the_same_finding(self):
        general = json.loads(GENERAL_CAPTURE.read_text(encoding="utf-8"))
        young = copy.deepcopy(RESERVED)
        del young["status"]
        self.assertEqual(self.finding([general, young]), self.finding([general]))

    def test_the_class_is_not_counted_as_a_general_purpose_class(self):
        self.assertFalse(collect._is_untainted_gp_compute_class(RESERVED, node_pools=[SLOT_C_POOL]))

    def test_without_its_taint_the_finding_names_no_class(self):
        # Why the class has the taint: a second untainted general-purpose
        # class leaves the finding with no target class, and the fix that the
        # SOP gives then is a choice of a class by the owner.
        general = json.loads(GENERAL_CAPTURE.read_text(encoding="utf-8"))
        # A soft taint, and a taint key that a node controller sets, do not
        # take the class out of the count either.
        soft = {"key": "dedicated", "value": "reserved-ccc", "effect": "PreferNoSchedule"}
        controller = {"key": "ToBeDeletedByClusterAutoscaler", "value": "reserved-ccc", "effect": "NoSchedule"}
        for without in (_reserved(nodePoolConfig={}), _reserved(nodePoolConfig={"taints": [soft]}), _reserved(nodePoolConfig={"taints": [controller]})):
            with self.subTest(config=without["spec"]["nodePoolConfig"]):
                hit = self.finding([general, without])
                self.assertEqual(hit["single_compute_class"], "")
                self.assertNotIn("target class", hit["excerpt"])


if __name__ == "__main__":
    unittest.main()
