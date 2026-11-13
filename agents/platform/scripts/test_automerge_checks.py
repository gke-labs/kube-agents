"""Tests for automerge_checks.py: the broker's safety checks on a generated budget.

`fleet-audit/scripts/test_broker_checks_parity.py` holds the verdicts to the
sandbox's own `pdb_safety.judge`.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import automerge_checks as ac

NS = "shop"
LABELS = {"app": "checkout"}
OBJ = "Deployment/checkout"
NAME = "checkout-pdb"
SELECTOR = {"matchLabels": LABELS}


def deployment(name="checkout", replicas=3, ready=None, labels=None, meta_labels=None, **meta):
    labels = labels or LABELS
    if meta_labels:
        meta["labels"] = meta_labels
    return {
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": NS, **meta},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": labels},
            "template": {"metadata": {"labels": labels}},
        },
        "status": {"readyReplicas": replicas if ready is None else ready},
    }


def pdb(name="legacy", labels=None, **spec):
    return {
        "kind": "PodDisruptionBudget",
        "metadata": {"name": name, "namespace": NS},
        "spec": {"selector": {"matchLabels": labels or LABELS}, **spec},
    }


def by_name(results):
    return {r["name"]: r for r in results}

def hpa(target="checkout", min_replicas=2, api_version="apps/v1"):
    spec = {"scaleTargetRef": {"apiVersion": api_version, "kind": "Deployment", "name": target}}
    if min_replicas is not None:
        spec["minReplicas"] = min_replicas
    return {"kind": "HorizontalPodAutoscaler", "metadata": {"name": f"{target}-hpa", "namespace": NS}, "spec": spec}


class JudgeTest(unittest.TestCase):

    def judge(self, *items):
        return by_name(ac.judge({"items": list(items)}, OBJ, NS, NAME, SELECTOR))

    def test_a_ready_workload_with_no_budget_passes(self):
        self.assertTrue(all(r["passed"] for r in self.judge(deployment()).values()))

    def test_each_check_fails_on_its_own_condition(self):
        for items, failed in (
            ((deployment(replicas=1),), ac.REPLICAS_READY),
            ((deployment(ready=2),), ac.REPLICAS_READY),
            ((deployment(generation=4),), ac.REPLICAS_READY),
            ((deployment(), pdb()), ac.NO_OVERLAPPING_BUDGET),
            ((deployment(), pdb(NAME, labels={"app": "cart"})), ac.NO_OVERLAPPING_BUDGET),
            ((deployment(labels={"app": "checkout", "tier": "web"}),), ac.SELECTOR_MATCHES),
            ((deployment(), deployment("checkout-canary")), ac.SELECTOR_MATCHES),
        ):
            with self.subTest(failed=failed, items=items):
                results = self.judge(*items)
                self.assertFalse(results[failed]["passed"], results)

    def test_a_missing_workload_fails_everything(self):
        self.assertFalse(any(r["passed"] for r in self.judge().values()))

    def test_an_unknown_selector_operator_matches_nothing(self):
        self.assertFalse(ac.selector_matches({"matchExpressions": [{"key": "app", "operator": "Like"}]}, LABELS))

    def test_a_workload_outside_the_audits_scope_fails_everything(self):
        for item in (
            deployment(meta_labels={ac.OPT_OUT_KEY: "exempt"}),
            deployment(annotations={ac.OPT_OUT_KEY: "exempt"}),
            deployment(meta_labels={ac.ADDON_MANAGER_LABEL: "Reconcile"}),
            deployment(ownerReferences=[{"apiVersion": "apps/v1", "kind": "ReplicaSet", "name": "x"}]),
            deployment(replicas=0),
        ):
            with self.subTest(item=item["metadata"]):
                self.assertFalse(any(r["passed"] for r in self.judge(item).values()))
        # A CRD owner is not a built-in controller: the audit still reads it.
        crd = deployment(ownerReferences=[{"apiVersion": "example.com/v1", "kind": "Deployment", "name": "x"}])
        self.assertTrue(all(r["passed"] for r in self.judge(crd).values()))

    def test_a_system_namespace_is_outside_the_scope(self):
        for ns in ("kube-system", "gke-managed-x", "config-management-system"):
            with self.subTest(ns=ns):
                item = deployment()
                item["metadata"]["namespace"] = ns
                results = ac.judge({"items": [item]}, OBJ, ns, NAME, SELECTOR)
                self.assertFalse(any(r["passed"] for r in results))

    def test_an_unreadable_budget_selector_counts_as_overlapping(self):
        odd = pdb("odd")
        odd["spec"]["selector"] = {"matchExpressions": [{"key": "app", "operator": "Like", "values": ["x"]}]}
        self.assertFalse(self.judge(deployment(), odd)[ac.NO_OVERLAPPING_BUDGET]["passed"])
        null = pdb("null")
        null["spec"]["selector"] = None
        self.assertTrue(self.judge(deployment(), null)[ac.NO_OVERLAPPING_BUDGET]["passed"])
        everything = pdb("all")
        everything["spec"]["selector"] = {}
        self.assertFalse(self.judge(deployment(), everything)[ac.NO_OVERLAPPING_BUDGET]["passed"])

    def test_an_item_with_no_namespace_is_not_this_namespaces(self):
        loose = pdb()
        del loose["metadata"]["namespace"]
        self.assertTrue(self.judge(deployment(), loose)[ac.NO_OVERLAPPING_BUDGET]["passed"])

    def test_an_autoscaler_floor_below_two_fails_replicas_ready(self):
        for floor, passed in ((None, False), (0, False), (1, False), (2, True)):
            with self.subTest(floor=floor):
                results = self.judge(deployment(), hpa(min_replicas=floor))
                self.assertEqual(results[ac.REPLICAS_READY]["passed"], passed, results)
                self.assertIn(f"autoscaler floor={1 if floor is None else floor}", results[ac.REPLICAS_READY]["detail"])

    def test_an_autoscaler_for_another_workload_or_group_is_not_its_floor(self):
        for scaler in (hpa(target="cart", min_replicas=1), hpa(min_replicas=1, api_version="example.com/v1")):
            with self.subTest(scaler=scaler):
                self.assertTrue(self.judge(deployment(), scaler)[ac.REPLICAS_READY]["passed"])

    def test_a_job_or_cronjob_the_selector_reaches_fails_selector_matches(self):
        job = {"kind": "Job", "metadata": {"name": "migrate", "namespace": NS},
               "spec": {"template": {"metadata": {"labels": LABELS}}}}
        cron = {"kind": "CronJob", "metadata": {"name": "nightly", "namespace": NS},
                "spec": {"jobTemplate": {"spec": {"template": {"metadata": {"labels": LABELS}}}}}}
        owned = {**job, "metadata": {**job["metadata"], "ownerReferences": [{"kind": "CronJob", "name": "nightly"}]}}
        self.assertIn("Job/migrate", self.judge(deployment(), job)[ac.SELECTOR_MATCHES]["detail"])
        self.assertIn("CronJob/nightly", self.judge(deployment(), cron)[ac.SELECTOR_MATCHES]["detail"])
        # A Job its CronJob owns is counted once, as the CronJob.
        detail = self.judge(deployment(), cron, owned)[ac.SELECTOR_MATCHES]["detail"]
        self.assertNotIn("Job/migrate", detail)

    def test_the_read_covers_every_kind_the_checks_look_at(self):
        for kind in ("cronjobs", "jobs", "horizontalpodautoscalers", "poddisruptionbudgets", "daemonsets"):
            self.assertIn(kind, ac.READ_KINDS.split(","))


if __name__ == "__main__":
    unittest.main()
