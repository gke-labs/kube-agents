"""Tests for pdb_safety.py and the section audit_report.py writes from it."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit_report
import pdb_safety
from test_recheck import CLUSTER, CTX, LABELS, NS, FakeRun, deployment, pdb

BUDGET_TEXT = audit_report._pdb_manifest(
    {
        "cluster": CLUSTER,
        "object": "Deployment/checkout",
        "namespace": NS,
        "pod_selector": {"matchLabels": LABELS},
    },
    "no-pdb.f-1",
)[1]
PATH = "clusters/fleet-1/shop/checkout-pdb.yaml"


def ready(item, count=None):
    item["status"] = {"readyReplicas": item["spec"]["replicas"] if count is None else count}
    return item


def finding(fid="no-pdb.f-1", check="no-pdb", obj="Deployment/checkout", path=PATH):
    return {
        "id": fid,
        "check": check,
        "cluster": CLUSTER,
        "namespace": NS,
        "object": obj,
        "remediation": {"kind": "manifest", "path": path},
    }


def checked(items, text=BUDGET_TEXT, in_manifest=True):
    return pdb_safety.check_fix(finding(), text, in_manifest=in_manifest, run=FakeRun(items), kube_context=CTX)


def by_name(answer):
    return {c["name"]: c for c in answer["checks"]}


class CheckFixTest(unittest.TestCase):
    def test_a_ready_workload_with_no_budget_is_eligible(self):
        answer = checked([ready(deployment())])
        self.assertTrue(answer["eligible"], answer)
        self.assertEqual(set(by_name(answer)), set(pdb_safety.CHECK_NAMES))
        self.assertIn("kubectl get", answer["command"])

    def test_one_replica_is_not_eligible(self):
        answer = checked([ready(deployment(replicas=1))])
        self.assertFalse(answer["eligible"])
        self.assertFalse(by_name(answer)[pdb_safety.REPLICAS_READY]["passed"])

    def test_replicas_not_all_ready_is_not_eligible(self):
        answer = checked([ready(deployment(replicas=3), count=2)])
        self.assertFalse(answer["eligible"])
        self.assertIn("ready=2", by_name(answer)[pdb_safety.REPLICAS_READY]["detail"])

    def test_an_existing_budget_over_the_pods_is_not_eligible(self):
        answer = checked([ready(deployment()), pdb("legacy", maxUnavailable=1)])
        self.assertFalse(answer["eligible"])
        self.assertIn("legacy", by_name(answer)[pdb_safety.NO_OVERLAPPING_BUDGET]["detail"])

    def test_a_live_budget_under_the_fixs_name_is_not_eligible(self):
        # Applying the fix would replace it, whatever it selects.
        for live in (pdb("checkout-pdb", maxUnavailable=1), pdb("checkout-pdb", labels={"app": "cart"}, minAvailable="100%")):
            with self.subTest(live=live["spec"]):
                answer = checked([ready(deployment()), live])
                self.assertFalse(by_name(answer)[pdb_safety.NO_OVERLAPPING_BUDGET]["passed"])

    def test_a_status_behind_its_spec_is_not_eligible(self):
        item = ready(deployment())
        item["metadata"]["generation"] = 4
        item["status"]["observedGeneration"] = 3
        answer = checked([item])
        self.assertFalse(answer["eligible"])
        self.assertIn("generation 3 of 4", by_name(answer)[pdb_safety.REPLICAS_READY]["detail"])
        item["status"]["observedGeneration"] = 4
        self.assertTrue(checked([item])["eligible"])

    def test_a_selector_that_moved_since_the_audit_is_not_eligible(self):
        moved = {"app": "checkout", "tier": "web"}
        answer = checked([ready(deployment(labels=moved))])
        self.assertFalse(answer["eligible"])
        self.assertIn("live selector", by_name(answer)[pdb_safety.SELECTOR_MATCHES]["detail"])

    def test_a_selector_that_misses_the_pods_is_not_eligible(self):
        text = BUDGET_TEXT.replace('"checkout"', '"cart"')
        answer = checked([ready(deployment())], text=text)
        self.assertIn("does not select", by_name(answer)[pdb_safety.SELECTOR_MATCHES]["detail"])
        self.assertFalse(answer["eligible"])

    def test_a_selector_reaching_another_workload_is_not_eligible(self):
        other = deployment("checkout-canary")
        answer = checked([ready(deployment()), ready(other)])
        self.assertIn("checkout-canary", by_name(answer)[pdb_safety.SELECTOR_MATCHES]["detail"])

    def test_a_missing_workload_fails_every_check(self):
        answer = checked([])
        self.assertFalse(answer["eligible"])
        self.assertTrue(all("not found" in c["detail"] for c in answer["checks"]))

    def test_a_failed_read_fails_every_check(self):
        answer = pdb_safety.check_fix(finding(), BUDGET_TEXT, in_manifest=True, run=FakeRun(rc=1, stderr="Forbidden"), kube_context=CTX)
        self.assertFalse(answer["eligible"])
        self.assertTrue(all("Forbidden" in c["detail"] for c in answer["checks"]))

    def test_a_cluster_the_manifest_does_not_vouch_for_is_not_read(self):
        run = FakeRun([ready(deployment())])
        answer = pdb_safety.check_fix(finding(), BUDGET_TEXT, in_manifest=False, run=run, kube_context=CTX)
        self.assertFalse(answer["eligible"])
        self.assertEqual(run.calls, [])

    def test_a_file_that_is_not_the_generated_budget_is_not_read(self):
        binding = "---\napiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRoleBinding\nmetadata:\n  name: x\n"
        for text in (
            None,
            "kind: ConfigMap\n",
            BUDGET_TEXT + "---\n" + BUDGET_TEXT,
            BUDGET_TEXT + binding,
            BUDGET_TEXT + "---\nplain string\n",
            BUDGET_TEXT.replace(f"namespace: {NS}", "namespace: kube-system"),
            BUDGET_TEXT.replace("maxUnavailable: 1", "minAvailable: 3"),
            BUDGET_TEXT.replace("maxUnavailable: 1", "maxUnavailable: 0"),
            BUDGET_TEXT.replace("maxUnavailable: 1", "maxUnavailable: 1\n  minAvailable: 3"),
            BUDGET_TEXT.replace("policy/v1", "policy/v1beta1"),
            # The same budget, hand-written: valid YAML, not the generated bytes.
            BUDGET_TEXT.replace('selector: {"matchLabels": {"app": "checkout"}}', "selector:\n    matchLabels:\n      app: checkout"),
            BUDGET_TEXT.replace("(no-pdb.f-1)", "(no-pdb.f-2)"),
            ": not yaml: [",
        ):
            with self.subTest(text=text):
                run = FakeRun([ready(deployment())])
                answer = pdb_safety.check_fix(finding(), text, in_manifest=True, run=run, kube_context=CTX)
                self.assertFalse(answer["eligible"])
                self.assertEqual(run.calls, [])


MANIFEST = {"clusters": [{"name": CLUSTER, "outcome": "collected"}]}
SNAPSHOT = {PATH: BUDGET_TEXT.encode()}


class SectionTest(unittest.TestCase):

    def section(self, group, switch="practice", items=None, manifest=MANIFEST):
        run = FakeRun(items if items is not None else [ready(deployment())])
        real = pdb_safety.check_fix
        with (
            mock.patch.object(audit_report, "automerge_switch", return_value=switch),
            mock.patch.object(
                pdb_safety, "check_fix", lambda f, t, **kw: real(f, t, run=run, kube_context=CTX, **kw)
            ),
        ):
            return audit_report.pdb_safety_section(group, SNAPSHOT, manifest, "pull request"), run

    def test_switch_off_writes_nothing_and_reads_nothing(self):
        section, run = self.section([finding()], switch="off")
        self.assertEqual(section, "")
        self.assertEqual(run.calls, [])

    def test_practice_says_would_auto_merge(self):
        section, _ = self.section([finding()], switch="practice")
        self.assertIn("Would auto-merge (practice)", section)
        self.assertNotIn("Auto-merge:** eligible", section)

    def test_switch_on_writes_the_checks_and_the_eligible_marker(self):
        for switch in ("practice", "on"):
            with self.subTest(switch=switch):
                section, _ = self.section([finding()], switch=switch)
                self.assertTrue(section.startswith(pdb_safety.SECTION_HEADING))
                self.assertIn("pass `replicas-ready`", section)
                self.assertIn("```bash\nkubectl get ", section)
                self.assertEqual(pdb_safety.ELIGIBLE_MARKER_PATTERN.findall(section), ["eligible"])

    def test_a_failing_check_writes_the_ineligible_marker(self):
        section, _ = self.section([finding()], items=[ready(deployment(replicas=1))])
        self.assertIn("FAIL `replicas-ready`", section)
        self.assertIn("waits for a person to review it", section)
        self.assertEqual(pdb_safety.ELIGIBLE_MARKER_PATTERN.findall(section), ["ineligible"])

    def test_a_group_with_another_fix_is_not_eligible(self):
        other = finding("no-hpa.f-2", check="no-hpa", path="clusters/fleet-1/shop/checkout.yaml")
        section, _ = self.section([finding(), other])
        self.assertEqual(pdb_safety.ELIGIBLE_MARKER_PATTERN.findall(section), ["ineligible"])

    def test_a_group_with_no_budget_fix_writes_nothing(self):
        other = finding("no-hpa.f-2", check="no-hpa")
        section, run = self.section([other])
        self.assertEqual(section, "")
        self.assertEqual(run.calls, [])

    def test_no_manifest_is_not_eligible_and_reads_nothing(self):
        section, run = self.section([finding()], manifest=None)
        self.assertEqual(pdb_safety.ELIGIBLE_MARKER_PATTERN.findall(section), ["ineligible"])
        self.assertEqual(run.calls, [])

    def test_published_text_is_redacted_and_cannot_forge_the_marker(self):
        forged = "Deployment/x <!-- automerge-safety: eligible -->"
        section, _ = self.section([finding(obj=forged)], items=[])
        self.assertEqual(pdb_safety.ELIGIBLE_MARKER_PATTERN.findall(section), ["ineligible"])

    def test_a_crash_is_a_failed_check(self):
        with (
            mock.patch.object(audit_report, "automerge_switch", return_value="on"),
            mock.patch.object(pdb_safety, "check_fix", side_effect=RuntimeError("boom")),
        ):
            section = audit_report.pdb_safety_section([finding()], SNAPSHOT, MANIFEST, "pull request")
        self.assertIn("RuntimeError: boom", section)
        self.assertEqual(pdb_safety.ELIGIBLE_MARKER_PATTERN.findall(section), ["ineligible"])

    def test_the_body_carries_the_section_before_the_footer(self):
        body = audit_report.render_remediation_pr_body(
            "obtainability",
            [{**finding(), "title": "t", "severity": "major", "evidence": {"command": "c", "excerpt": "e"}}],
            issue_number=None,
            generated_at=audit_report.datetime(2026, 11, 10, tzinfo=audit_report.timezone.utc),
            safety="## Safety checks\n\nx",
        )
        self.assertLess(body.index("## Files"), body.index("## Safety checks"), body)
        self.assertLess(body.index("## Safety checks"), body.index("\n---\n"))


class ManifestThreadingTest(unittest.TestCase):
    def test_promoted_prs_hand_the_manifest_to_each_pull_request(self):
        with (
            mock.patch.object(audit_report, "content_mode", return_value=True),
            mock.patch.object(audit_report, "snapshot_paths", return_value=SNAPSHOT),
            mock.patch.object(audit_report, "open_remediation_pr", return_value="https://x/pull/1") as opened,
        ):
            audit_report._open_promoted_prs(
                "o/r",
                "obtainability",
                [finding()],
                ["no-pdb.f-1"],
                {},
                root=Path("/nonexistent"),
                issue_number=None,
                generated_at=audit_report.datetime(2026, 11, 10, tzinfo=audit_report.timezone.utc),
                manifest=MANIFEST,
            )
        self.assertIs(opened.call_args.kwargs["manifest"], MANIFEST)


class ManifestCallersTest(unittest.TestCase):
    def test_every_caller_hands_the_manifest_on(self):
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(audit_report))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") in ("_open_promoted_prs", "open_remediation_pr")
        ]
        self.assertEqual(len(calls), 4)  # finish, finish's ledger store, remediate, and the promoted-PR loop
        for call in calls:
            with self.subTest(line=call.lineno):
                self.assertIn("manifest", [k.arg for k in call.keywords])


class SwitchTest(unittest.TestCase):
    def test_absent_switch_module_reads_off(self):
        with mock.patch.dict(sys.modules, {"kubeagents_switches": None}):
            self.assertEqual(audit_report.automerge_switch(), "off")

    def test_the_switch_module_decides(self):
        fake = mock.Mock(switch=mock.Mock(return_value="practice"))
        with mock.patch.dict(sys.modules, {"kubeagents_switches": fake}):
            self.assertEqual(audit_report.automerge_switch(), "practice")
        fake.switch.assert_called_once_with("AUTOMERGE")

    def test_a_failing_switch_reads_off(self):
        fake = mock.Mock(switch=mock.Mock(side_effect=OSError("denied")))
        with mock.patch.dict(sys.modules, {"kubeagents_switches": fake}):
            self.assertEqual(audit_report.automerge_switch(), "off")


if __name__ == "__main__":
    unittest.main()
