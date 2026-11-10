"""Tests for recheck.py: one fleet-audit finding re-derived against its target."""

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import collect
import recheck

CLUSTER = "proj/us-central1/fleet-1"
NS = "shop"
LABELS = {"app": "checkout"}


def deployment(name="checkout", replicas=3, labels=None, ns=NS):
    labels = labels or LABELS
    return {
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": labels},
            "template": {"metadata": {"labels": labels}, "spec": {"containers": [{"name": "c"}]}},
        },
    }


def pdb(name="checkout-pdb", labels=None, ns=NS, **spec):
    return {
        "kind": "PodDisruptionBudget",
        "metadata": {"name": name, "namespace": ns},
        "spec": {"selector": {"matchLabels": labels or LABELS}, **spec},
    }


def hpa(target="checkout", min_replicas=2, ns=NS):
    return {
        "kind": "HorizontalPodAutoscaler",
        "metadata": {"name": f"{target}-hpa", "namespace": ns},
        "spec": {
            "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": target},
            "minReplicas": min_replicas,
        },
    }


class FakeRun:
    def __init__(self, items=None, rc=0, stdout=None, stderr=""):
        self.stdout = json.dumps({"items": items or []}) if stdout is None else stdout
        self.rc, self.stderr = rc, stderr
        self.calls = []

    def __call__(self, argv, *, env=None, timeout=None):
        self.calls.append((argv, env, timeout))
        return collect.Run(argv, self.rc, self.stdout, self.stderr, 0.0)


def target(budget="", obj="Deployment/checkout", check="no-pdb"):
    return recheck.Target(check, CLUSTER, NS, obj, budget)


class NoPdbTest(unittest.TestCase):
    def test_missing_budget_reproduces_with_the_audits_excerpt(self):
        answer = recheck.recheck(target(), run=FakeRun([deployment()]), pr={"state": "MERGED"})
        self.assertEqual(answer["outcome"], recheck.OUTCOME_REPRODUCES)
        self.assertEqual(answer["state"], "pr-merged-persists")
        [evidence] = answer["evidence"]
        self.assertEqual(evidence["check"], "no-pdb")
        self.assertEqual(evidence["excerpt"], "replicas=3, no PodDisruptionBudget matches this workload's pod labels")
        self.assertEqual(answer["finding_id"], "no-pdb.proj-us-central1-fleet-1.shop.deployment-checkout")

    def test_the_fixs_budget_reads_clean(self):
        items = [deployment(), pdb(maxUnavailable=1)]
        answer = recheck.recheck(target("checkout-pdb"), run=FakeRun(items), pr={"state": "MERGED"})
        self.assertEqual(answer["outcome"], recheck.OUTCOME_CLEAN)
        self.assertEqual(answer["state"], "resolved-merged")
        self.assertEqual(answer["budgets"], ["checkout-pdb"])
        self.assertEqual(answer["evidence"], [])

    def test_a_budget_that_permits_no_eviction_reproduces_as_blocking_pdb(self):
        items = [deployment(replicas=3), pdb(minAvailable=3)]
        answer = recheck.recheck(target(), run=FakeRun(items))
        self.assertEqual(answer["outcome"], recheck.OUTCOME_REPRODUCES)
        [evidence] = answer["evidence"]
        self.assertEqual(evidence["check"], "blocking-pdb")
        self.assertEqual(evidence["object"], "PodDisruptionBudget/checkout-pdb")
        self.assertIn("against Deployment/checkout", evidence["excerpt"])
        self.assertNotIn("state", answer)  # no pull request record was given

    def test_an_unrelated_blocking_budget_does_not_fail_a_correct_fix(self):
        other = {"app": "cart"}
        items = [
            deployment(),
            pdb(maxUnavailable=1),
            deployment("cart", replicas=2, labels=other),
            pdb("cart-pdb", labels=other, maxUnavailable=0),
        ]
        answer = recheck.recheck(target("checkout-pdb"), run=FakeRun(items))
        self.assertEqual(answer["outcome"], recheck.OUTCOME_CLEAN)
        # The whole-cluster check does fire on the other budget: narrowing is
        # what keeps it out of this answer.
        dump = {"items": items}
        workloads = collect.normalize_workloads(dump)
        blocking = collect.check_blocking_pdb(collect.build_context(dump, workloads))
        self.assertEqual([h["object"] for h in blocking], ["PodDisruptionBudget/cart-pdb"])

    def test_a_second_budget_over_the_same_pods_reproduces_as_pdb_overlapping(self):
        # Both budgets permit evictions, but the eviction API refuses a pod two
        # budgets select. Named or not, the fix is not clean.
        items = [deployment(), pdb(maxUnavailable=1), pdb("legacy", maxUnavailable=1)]
        for budget in ("checkout-pdb", ""):
            with self.subTest(budget=budget):
                answer = recheck.recheck(target(budget), run=FakeRun(items), pr={"state": "MERGED"})
                self.assertEqual(answer["outcome"], recheck.OUTCOME_REPRODUCES)
                self.assertEqual(answer["state"], "pr-merged-persists")
                [evidence] = answer["evidence"]
                self.assertEqual((evidence["check"], evidence["object"]), ("pdb-overlapping", "Deployment/checkout"))

    def test_an_overlap_on_another_workload_does_not_fail_a_correct_fix(self):
        other = {"app": "cart"}
        items = [
            deployment(),
            pdb(maxUnavailable=1),
            deployment("cart", replicas=2, labels=other),
            pdb("cart-a", labels=other, maxUnavailable=1),
            pdb("cart-b", labels=other, maxUnavailable=1),
        ]
        self.assertEqual(recheck.recheck(target("checkout-pdb"), run=FakeRun(items))["outcome"], recheck.OUTCOME_CLEAN)

    def test_budget_named_by_the_caller_narrows_blocking_pdb_to_it(self):
        # The fix's budget is fine; a blocking budget elsewhere in the namespace
        # is not over these pods, so it is not this finding's business.
        other = {"app": "cart"}
        items = [
            deployment(),
            pdb(maxUnavailable=1),
            deployment("cart", labels=other),
            pdb("cart-pdb", labels=other, minAvailable=3),
        ]
        answer = recheck.recheck(target("checkout-pdb"), run=FakeRun(items))
        self.assertEqual((answer["outcome"], answer["budgets"]), (recheck.OUTCOME_CLEAN, ["checkout-pdb"]))

    def test_autoscaler_floor_is_what_the_budget_is_measured_against(self):
        items = [deployment(replicas=4), pdb(minAvailable=2), hpa(min_replicas=2)]
        answer = recheck.recheck(target(), run=FakeRun(items))
        self.assertEqual(answer["outcome"], recheck.OUTCOME_REPRODUCES)
        self.assertIn("minReplicas 2", answer["evidence"][0]["excerpt"])

    def test_named_budget_absent_but_another_covers_is_unchecked(self):
        items = [deployment(), pdb("someone-elses", maxUnavailable=1)]
        answer = recheck.recheck(target("checkout-pdb"), run=FakeRun(items), pr={"state": "MERGED"})
        self.assertEqual(answer["outcome"], recheck.OUTCOME_UNCHECKED)
        self.assertIn("does not exist", answer["reason"])
        self.assertIn("covered by someone-elses", answer["reason"])
        self.assertNotIn("state", answer)

    def test_named_budget_that_misses_the_pods_is_unchecked_when_another_covers(self):
        items = [deployment(), pdb(labels={"app": "typo"}, maxUnavailable=1), pdb("other", maxUnavailable=1)]
        answer = recheck.recheck(target("checkout-pdb"), run=FakeRun(items))
        self.assertEqual(answer["outcome"], recheck.OUTCOME_UNCHECKED)
        self.assertIn("does not select its pods", answer["reason"])

    def test_named_budget_that_misses_the_pods_reproduces_no_pdb(self):
        items = [deployment(), pdb(labels={"app": "typo"}, maxUnavailable=1)]
        answer = recheck.recheck(target("checkout-pdb"), run=FakeRun(items))
        self.assertEqual(answer["outcome"], recheck.OUTCOME_REPRODUCES)
        self.assertEqual(answer["evidence"][0]["check"], "no-pdb")

    def test_scaled_below_two_with_named_budget_gone_is_unchecked(self):
        items = [deployment(replicas=1)]
        answer = recheck.recheck(target("checkout-pdb"), run=FakeRun(items))
        self.assertEqual(answer["outcome"], recheck.OUTCOME_UNCHECKED)
        self.assertIn("not required to have a budget now", answer["reason"])


class ScopeAndReadTest(unittest.TestCase):
    def test_absent_workload_is_unchecked(self):
        answer = recheck.recheck(target(), run=FakeRun([]), pr={"state": "MERGED"})
        self.assertEqual(answer["outcome"], recheck.OUTCOME_UNCHECKED)
        self.assertIn("was not found", answer["reason"])
        self.assertNotIn("state", answer)

    def test_scaled_to_zero_workload_is_unchecked(self):
        answer = recheck.recheck(target(), run=FakeRun([deployment(replicas=0)]))
        self.assertEqual(answer["outcome"], recheck.OUTCOME_UNCHECKED)
        self.assertIn("outside the audit's scope", answer["reason"])

    def test_failed_read_is_unchecked_never_clean(self):
        for run in (
            FakeRun(rc=1, stdout="", stderr="Forbidden"),
            FakeRun(stdout=""),
            FakeRun(stdout="{not json"),
            FakeRun(stdout='{"kind": "List"}'),
        ):
            with self.subTest(stdout=run.stdout, rc=run.rc):
                answer = recheck.recheck(target(), run=run)
                self.assertEqual(answer["outcome"], recheck.OUTCOME_UNCHECKED)
                self.assertIn("failed its gate", answer["reason"])

    def test_unregistered_check_reads_nothing(self):
        run = FakeRun([deployment()])
        answer = recheck.recheck(target(check="no-hpa"), run=run)
        self.assertEqual(answer["outcome"], recheck.OUTCOME_UNCHECKED)
        self.assertIn("no on-demand re-check is registered", answer["reason"])
        self.assertEqual(run.calls, [])

    def test_reads_one_namespace_with_the_callers_kubeconfig_and_context(self):
        run = FakeRun([deployment()])
        answer = recheck.recheck(target(), run=run, kubeconfig="/tmp/kc", kube_context="ctx")
        [(argv, env, timeout)] = run.calls
        self.assertEqual(
            argv,
            ["kubectl", "get", recheck.RECHECKS["no-pdb"].kinds, "-n", NS, "-o", "json", "--context", "ctx"],
        )
        self.assertNotIn("-A", argv)
        self.assertEqual(env["KUBECONFIG"], "/tmp/kc")
        self.assertEqual(timeout, recheck.READ_TIMEOUT_S)
        self.assertTrue(answer["command"].startswith("KUBECONFIG=/tmp/kc kubectl get "))


class RegistrationTest(unittest.TestCase):
    def test_steps_resolve_checks_by_the_collectors_id_and_kind(self):
        with self.assertRaises(KeyError):
            recheck.workload_check("no-such-check")
        with self.assertRaises(ValueError):
            recheck.workload_check("blocking-pdb")
        with self.assertRaises(ValueError):
            recheck.narrowed_cluster_check("no-pdb", "pdbs")

    def test_a_workload_step_runs_the_collectors_function_for_that_id(self):
        # The step is the collector's CheckSpec, not a copy of it.
        spec = next(s for s in collect.OBTAINABILITY_CHECKS if s.slug == "no-pdb")
        self.assertIs(recheck._spec("no-pdb"), spec)


class AuditParityTest(unittest.TestCase):
    """The re-check says what the audit's own collector says about the target."""

    def test_evidence_matches_the_collectors_candidates_for_the_target(self):
        other = {"app": "cart"}
        items = [
            deployment(replicas=3),
            pdb(minAvailable=3),
            deployment("cart", replicas=2, labels=other),
            pdb("cart-pdb", labels=other, maxUnavailable=0),
            deployment("web", replicas=2, labels={"app": "web"}),
        ]
        dump = {"items": items}
        workloads = collect.normalize_workloads(dump)
        specs = tuple(
            s for s in collect.OBTAINABILITY_CHECKS if s.slug in ("no-pdb", "blocking-pdb", "pdb-overlapping")
        )
        collected = collect.CollectedContext(
            collect.build_context(dump, workloads), workloads, {s.slug: {} for s in specs}
        )
        cluster = {"name": "fleet-1", "project": "proj", "location": "us-central1", "target": CLUSTER}
        entry = collect.collect_cluster(cluster, checks=specs, collected=collected)
        audit = {(c["check"], c["object"]): c["excerpt"] for c in entry["candidates"]}

        answer = recheck.recheck(target(), run=FakeRun(items))
        mine = {(e["check"], e["object"]): e["excerpt"] for e in answer["evidence"]}
        self.assertEqual(mine, {k: v for k, v in audit.items() if k == ("blocking-pdb", "PodDisruptionBudget/checkout-pdb")})
        self.assertIn(("blocking-pdb", "PodDisruptionBudget/cart-pdb"), audit)

        web = recheck.recheck(target(obj="Deployment/web"), run=FakeRun(items))
        self.assertEqual(
            {(e["check"], e["object"]): e["excerpt"] for e in web["evidence"]},
            {("no-pdb", "Deployment/web"): audit[("no-pdb", "Deployment/web")]},
        )


class MainTest(unittest.TestCase):
    def run_main(self, items, *extra):
        buf = io.StringIO()
        with mock.patch.object(collect, "default_run", FakeRun(items)), redirect_stdout(buf):
            rc = recheck.main(
                ["--check", "no-pdb", "--cluster", CLUSTER, "--namespace", NS, "--object", "Deployment/checkout", *extra]
            )
        return rc, json.loads(buf.getvalue())

    def test_exit_codes_follow_the_outcome(self):
        rc, answer = self.run_main([deployment()], "--merged")
        self.assertEqual((rc, answer["outcome"], answer["state"]), (1, "reproduces", "pr-merged-persists"))
        rc, answer = self.run_main([deployment(), pdb(maxUnavailable=1)], "--budget", "checkout-pdb", "--merged")
        self.assertEqual((rc, answer["outcome"], answer["state"]), (0, "clean", "resolved-merged"))
        rc, answer = self.run_main([])
        self.assertEqual((rc, answer["outcome"]), (2, "unchecked"))

    def test_a_crash_inside_the_recheck_is_unchecked_not_reproduces(self):
        def broken(subject):
            raise TypeError("boom")

        original = recheck.RECHECKS["no-pdb"]
        buf = io.StringIO()
        try:
            recheck.RECHECKS["no-pdb"] = original._replace(steps=(broken,))
            with mock.patch.object(collect, "default_run", FakeRun([deployment()])), redirect_stdout(buf):
                rc = recheck.main(
                    ["--check", "no-pdb", "--cluster", CLUSTER, "--namespace", NS, "--object", "Deployment/checkout"]
                )
        finally:
            recheck.RECHECKS["no-pdb"] = original
        answer = json.loads(buf.getvalue())
        self.assertEqual((rc, answer["outcome"]), (2, "unchecked"))
        self.assertIn("TypeError: boom", answer["reason"])
        self.assertEqual(answer["finding_id"], "no-pdb.proj-us-central1-fleet-1.shop.deployment-checkout")
        self.assertNotIn("budget", answer)


if __name__ == "__main__":
    unittest.main()
