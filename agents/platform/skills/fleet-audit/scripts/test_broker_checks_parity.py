"""The credential broker re-checks a no-pdb fix with `automerge_checks` (stdlib
only, no fleet-audit import); its verdicts and its copies of the audit's scope
rules must stay the sandbox's own (`pdb_safety.judge`, `collect`)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit_report  # puts the shared scripts directory on the path
import automerge_checks
import pdb_safety
from test_pdb_safety import BUDGET_TEXT, finding, ready
from test_recheck import CTX, LABELS, NS, FakeRun, deployment, hpa, pdb


class BrokerChecksParityTest(unittest.TestCase):
    ac = automerge_checks

    def test_the_brokers_copies_of_the_audits_rules_are_the_audits(self):
        import collect

        self.assertEqual(self.ac.SYSTEM_NAMESPACES, collect.SYSTEM_NAMESPACES)
        self.assertEqual(self.ac.OPT_OUT_KEY, collect.OPT_OUT_KEY)
        self.assertEqual(self.ac.BUILTIN_OWNER_GROUPS, collect.BUILTIN_OWNER_GROUPS)
        self.assertEqual(self.ac.BUILTIN_OWNER_KINDS, collect.BUILTIN_OWNER_KINDS)
        self.assertEqual(self.ac.DEFAULT_HPA_MIN_REPLICAS, collect.DEFAULT_HPA_MIN_REPLICAS)
        self.assertEqual(set(self.ac.READ_KINDS.split(",")), set(pdb_safety.READ.kinds.split(",")))
        for ns in ("gke-x", "config-management-y", "shop", "asm-x", "istio-x", "anthos-x", "gmp-x", "cnrm-x"):
            self.assertEqual(ns.startswith(self.ac.SYSTEM_NAMESPACE_PREFIXES), collect._is_system_namespace(ns) and ns not in collect.SYSTEM_NAMESPACES)

    def test_the_broker_reaches_the_sandboxs_verdicts(self):
        job = {"kind": "Job", "metadata": {"name": "migrate", "namespace": NS},
               "spec": {"template": {"metadata": {"labels": LABELS}}}}
        cron = {"kind": "CronJob", "metadata": {"name": "nightly", "namespace": NS},
                "spec": {"jobTemplate": {"spec": {"template": {"metadata": {"labels": LABELS}}}}}}
        owned_job = {**job, "metadata": {**job["metadata"], "ownerReferences": [{"apiVersion": "batch/v1", "kind": "CronJob", "name": "nightly"}]}}
        daemon = {"kind": "DaemonSet", "metadata": {"name": "agent", "namespace": NS},
                  "spec": {"selector": {"matchLabels": LABELS}, "template": {"metadata": {"labels": LABELS}}}}
        no_floor = hpa(min_replicas=2)
        del no_floor["spec"]["minReplicas"]
        other_group = hpa(min_replicas=1)
        other_group["spec"]["scaleTargetRef"]["apiVersion"] = "example.com/v1"

        def meta(**extra):
            item = ready(deployment())
            item["metadata"].update(extra)
            return item

        def expr_pdb(key, operator, values=None):
            item = pdb(f"expr-{key}-{operator.lower()}", labels={})
            expression = {"key": key, "operator": operator}
            if values is not None:
                expression["values"] = values
            item["spec"]["selector"] = {"matchExpressions": [expression]}
            return item

        null_pdb, all_pdb = pdb("null"), pdb("all")
        null_pdb["spec"]["selector"], all_pdb["spec"]["selector"] = None, {}
        stale = ready(deployment())
        stale["metadata"]["generation"] = 4
        cases = {
            "ready": [ready(deployment())],
            "one replica": [ready(deployment(replicas=1))],
            "not ready": [ready(deployment(), count=1)],
            "stale status": [stale],
            "autoscaler floor 1": [ready(deployment()), hpa(min_replicas=1)],
            "autoscaler floor 2": [ready(deployment()), hpa(min_replicas=2)],
            "overlapping budget": [ready(deployment()), pdb("legacy")],
            "same-named budget": [ready(deployment()), pdb("checkout-pdb", labels={"app": "cart"})],
            "another workload": [ready(deployment()), ready(deployment("checkout-canary"))],
            "a job": [ready(deployment()), job],
            "a cronjob": [ready(deployment()), cron],
            "a cronjob's own job": [ready(deployment()), cron, owned_job],
            "a daemonset": [ready(deployment()), daemon],
            "autoscaler with no floor": [ready(deployment()), no_floor],
            "autoscaler floor 0": [ready(deployment()), hpa(min_replicas=0)],
            "autoscaler of another group": [ready(deployment()), other_group],
            "null-selector budget": [ready(deployment()), null_pdb],
            "empty-selector budget": [ready(deployment()), all_pdb],
            "opted out by label": [meta(labels={"kubeagents.x-k8s.io/reliability-audit": "exempt"})],
            "opted out by annotation": [meta(annotations={"kubeagents.x-k8s.io/reliability-audit": "exempt"})],
            "addon-managed": [meta(labels={"addonmanager.kubernetes.io/mode": "Reconcile"})],
            "owned by a replicaset": [meta(ownerReferences=[{"apiVersion": "apps/v1", "kind": "ReplicaSet", "name": "x"}])],
            "owned by a custom resource": [meta(ownerReferences=[{"apiVersion": "example.com/v1", "kind": "Rollout", "name": "x"}])],
            "scaled to zero": [ready(deployment(replicas=0))],
            "missing workload": [],
            # Live budgets selecting by expression: each operator, and NotIn
            # and DoesNotExist where the pods carry no such key.
            "budget In, matching": [ready(deployment()), expr_pdb("app", "In", ["checkout", "x"])],
            "budget In, not matching": [ready(deployment()), expr_pdb("app", "In", ["cart"])],
            "budget NotIn, key present": [ready(deployment()), expr_pdb("app", "NotIn", ["checkout"])],
            "budget NotIn, key absent": [ready(deployment()), expr_pdb("tier", "NotIn", ["batch"])],
            "budget Exists, present": [ready(deployment()), expr_pdb("app", "Exists")],
            "budget Exists, absent": [ready(deployment()), expr_pdb("tier", "Exists")],
            "budget DoesNotExist, absent": [ready(deployment()), expr_pdb("tier", "DoesNotExist")],
            "budget DoesNotExist, present": [ready(deployment()), expr_pdb("app", "DoesNotExist")],
        }
        for case, items in cases.items():
            with self.subTest(case=case):
                sandbox = pdb_safety.check_fix(finding(), BUDGET_TEXT, in_manifest=True, run=FakeRun(items), kube_context=CTX)
                broker = self.ac.judge({"items": items}, "Deployment/checkout", NS, "checkout-pdb", {"matchLabels": LABELS})
                self.assertEqual(
                    {c["name"]: c["passed"] for c in broker},
                    {c["name"]: c["passed"] for c in sandbox["checks"]},
                    (broker, sandbox["checks"]),
                )

    def test_an_unreadable_live_selector_is_stricter_here_on_purpose(self):
        odd = pdb("odd", labels={"app": "other"})
        odd["spec"]["selector"]["matchExpressions"] = [{"key": "a", "operator": "Bogus"}]
        items = [ready(deployment()), odd]
        sandbox = pdb_safety.check_fix(finding(), BUDGET_TEXT, in_manifest=True, run=FakeRun(items), kube_context=CTX)
        broker = self.ac.judge({"items": items}, "Deployment/checkout", NS, "checkout-pdb", {"matchLabels": LABELS})
        self.assertTrue({c["name"]: c["passed"] for c in sandbox["checks"]}[self.ac.NO_OVERLAPPING_BUDGET])
        self.assertFalse({c["name"]: c["passed"] for c in broker}[self.ac.NO_OVERLAPPING_BUDGET])


if __name__ == "__main__":
    unittest.main()
