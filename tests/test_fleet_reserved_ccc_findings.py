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
writes, under the reservation check (`reservation-mismatch-risk`). These tests
ask the collectors themselves, with the class as GKE holds it, so a change of
a check that adds a second finding on the fixture, or that drops the one,
fails here and not on a pool project.
"""

from __future__ import annotations

import copy
import json
import re
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "agents" / "platform" / "skills" / "fleet-audit" / "scripts"
# Slot c's other class as GKE reports it, captured live: `general-ccc`.
GENERAL_CAPTURE = SCRIPTS / "testdata" / "computeclass_taint_missing.json"
# The case that grades the stockout finding on the class, and its check.
CASE_FILE = REPO_ROOT / "bench" / "tasks" / "stockout-pinned-pool" / "task.yaml"
CASE_CHECK = "a-reservation-finding-was-filed-against-the-class-with-no-reservation"
# The case's check that the class is not filed under the class-status check.
CASE_MOVE_CHECK = "the-class-with-no-reservation-is-not-filed-as-a-broken-class"
# A project ID of the longest length, which makes the finding id longer than
# the ledger's limit, so the id is shortened.
LONGEST_PROJECT = "p" * 30
ROLE = "missing-reservation-compute-class"
# The stockout check that files the class, and the check that files a class
# which GKE reports for another cause (slot c's `general-ccc`).
RESERVATION_CHECK = "reservation-mismatch-risk"
CLASS_STATUS_CHECK = "dangling-compute-class"
CLASS_OBJECT = "ComputeClass/reserved-ccc"

sys.path.insert(0, str(SCRIPTS))

import audit_report  # noqa: E402
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
# Slot c as the stockout collector gets it from the cluster list: one zone.
PROJECT = "acme"
SLOT_C = {"name": "seeded-c", "project": PROJECT, "location": "us-central1-a", "autopilot": False, "locations": ["us-central1-a"]}
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


def _collected(*classes: dict) -> dict:
    """The stockout collector's entry for slot c with these classes and the
    slot's one node pool. The reads are answered here; no command runs."""

    def run(argv, **_):
        if argv[:3] == ["kubectl", "get", fleet_stockout.COMPUTECLASS_KIND]:
            return fleet_stockout.Run(argv, 0, json.dumps({"items": list(classes)}), "", 0.0)
        if argv[:2] == ["kubectl", "get"]:
            return fleet_stockout.Run(argv, 0, json.dumps({"items": []}), "", 0.0)
        if argv[:3] == ["gcloud", "container", "node-pools"]:
            return fleet_stockout.Run(argv, 0, json.dumps([DEFAULT_POOL]), "", 0.0)
        return fleet_stockout.Run(argv, 0, "", "", 0.0)

    with TemporaryDirectory() as tmp, patch.object(fleet_stockout, "KUBECONFIG_DIR", Path(tmp)):
        return fleet_stockout.collect_cluster(SLOT_C, run=run)


def _filed_on_the_class(entry: dict) -> list[dict]:
    return [candidate for candidate in entry["candidates"] if candidate["object"] == CLASS_OBJECT]


class StockoutFindingsTest(unittest.TestCase):
    def test_the_collector_files_one_finding_on_the_class_under_the_reservation_check(self):
        general = json.loads(GENERAL_CAPTURE.read_text(encoding="utf-8"))
        entry = _collected(general, RESERVED)
        [hit] = _filed_on_the_class(entry)
        self.assertEqual(hit["check"], RESERVATION_CHECK)
        self.assertIn("CrdMisconfigured", hit["excerpt"])
        self.assertIn("ReservationNotFound", hit["excerpt"])
        self.assertIn("seeded-absent-reservation", hit["excerpt"])
        # GKE named the reservation, so the fleet step gets no rule to compare.
        self.assertNotIn(fleet_stockout.RESERVED_CLASSES_KEY, entry)

    def test_the_two_classes_of_slot_c_each_get_their_own_finding(self):
        general = json.loads(GENERAL_CAPTURE.read_text(encoding="utf-8"))
        entry = _collected(general, RESERVED)
        filed = sorted((c["check"], c["object"]) for c in entry["candidates"] if c["check"] in (CLASS_STATUS_CHECK, RESERVATION_CHECK))
        self.assertEqual(filed, [(CLASS_STATUS_CHECK, "ComputeClass/general-ccc"), (RESERVATION_CHECK, CLASS_OBJECT)])

    def test_a_class_with_no_status_yet_is_filed_by_the_comparison(self):
        # The minutes after the plant: GKE did not write the status yet. The
        # fleet step then compares the rule with the project's reservations.
        young = copy.deepcopy(RESERVED)
        del young["status"]
        entry = _collected(young)
        self.assertEqual(_filed_on_the_class(entry), [])
        fleet_stockout.file_reserved_classes(entry, {PROJECT: {}}, frozenset({PROJECT}))
        [hit] = _filed_on_the_class(entry)
        self.assertEqual(hit["check"], RESERVATION_CHECK)
        # The rule names no zone, so the comparison is in the slot's one zone.
        self.assertEqual(
            hit["excerpt"],
            "priority 1: reservations.affinity=Specific; reservation seeded-absent-reservation, "
            "compared in cluster zones us-central1-a: found in no zone of project acme",
        )

    def test_the_case_check_names_the_collectors_check_and_object(self):
        # The case `stockout-pinned-pool` grades the finding. The phrase is in
        # the case file and the object is in the collector; this joins the two.
        case = yaml.safe_load(CASE_FILE.read_text(encoding="utf-8"))
        [check] = [c["check"] for c in case["verification_spec"] if c.get("name") == CASE_CHECK]
        [hit] = _filed_on_the_class(_collected(RESERVED))
        self.assertIn(ROLE, case["fixtures"])
        self.assertEqual(check["scope"], "finding")
        self.assertEqual(check["finding_check"], hit["check"])
        self.assertEqual(check["required_phrases"], [hit["object"]])

    def test_the_case_s_second_check_fails_on_the_class_status_id_only(self):
        # The pattern is in the case file, and the id is derived in
        # `audit_report.py` from the collector's check and object. This joins
        # them, as the bench check reads them: lowercase ids, one on each line.
        case = yaml.safe_load(CASE_FILE.read_text(encoding="utf-8"))
        [check] = [c["check"] for c in case["verification_spec"] if c.get("name") == CASE_MOVE_CHECK]
        self.assertEqual(check["scope"], "finding_ids")
        [pattern] = check["forbidden_patterns"]
        general = json.loads(GENERAL_CAPTURE.read_text(encoding="utf-8"))

        def ids(entry: dict, candidates: list[dict]) -> str:
            return "\n".join(audit_report.published_id({**c, "cluster": entry["name"]}) for c in candidates).lower()

        for project in (PROJECT, LONGEST_PROJECT):
            with self.subTest(project=project), patch.dict(SLOT_C, {"project": project}):
                entry = _collected(general, RESERVED)
                # What the collector files on slot c: the reservation finding
                # on the class, and the class-status finding on `general-ccc`.
                self.assertNotRegex(ids(entry, entry["candidates"]), re.compile(pattern, re.IGNORECASE))
                # The class-status hit on the class, which the collector files
                # when it does not file the status under the reservation check.
                status_hit = {**fleet_stockout.check_compute_class_status(RESERVED), "check": CLASS_STATUS_CHECK}
                self.assertEqual(status_hit["object"], CLASS_OBJECT)
                self.assertRegex(ids(entry, [status_hit]), re.compile(pattern, re.IGNORECASE))
                shortened = len(audit_report.derive_finding_id({**status_hit, "cluster": entry["name"]})) > audit_report.MAX_FINDING_ID
                self.assertEqual(shortened, project == LONGEST_PROJECT)

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

    def test_with_auto_creation_off_the_class_gets_the_manual_fallback_finding_too(self):
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
        # 3.10(a) files only an affinity that goes round the rules of the
        # class. `Specific` is not one.
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
