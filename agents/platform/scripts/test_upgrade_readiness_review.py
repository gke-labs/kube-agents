"""Tests for upgrade_readiness_review.py: the mapping, the rubric, the registration, the record, the lines."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import findings_queue as fq  # noqa: E402
import upgrade_readiness_review as review  # noqa: E402
import upgrade_readiness_watch as watch  # noqa: E402

ROSTER = Path(__file__).resolve().parents[1] / "cron" / "jobs.json"
SOP = Path(__file__).resolve().parents[1] / "governance" / "upgrade_readiness_brief_sop.md"
NOW = datetime(2026, 10, 18, 6, 0, tzinfo=timezone.utc)
PROJECT = "acme-prod"
OTHER_PROJECT = "acme-dev"
SCOPE = watch.fleet_scope_targets.ScopeTargets(projects=(PROJECT,), unread=(), resolved_at=None, path="fleet_scope.json", excluded=(), retiring=())


def member(cluster: str, status: str = "ready", project: str = PROJECT, **readiness) -> dict:
    entry = {
        "project": project,
        "location": "us-central1-a",
        "cluster": cluster,
        "status": "lagging",
        "target_version": "1.36.4-gke.1391002",
        "readiness": {"status": status, "read_error": None, "webhook_read_error": None, **readiness},
    }
    return entry


def budget(name="pinned-batch-runner", namespace="seeded-upgrade", replicas=1) -> dict:
    return {
        "pdb": f"{namespace}/{name}",
        "namespace": namespace,
        "name": name,
        "field": "maxUnavailable: 0",
        "workloads": [{"kind": "Deployment", "namespace": namespace, "name": name, "replicas": replicas}],
        "expected_pods": replicas,
        "disruptions_allowed": 0,
    }


def seeded_b() -> dict:
    return member(
        "seeded-b",
        "blocked",
        pdbs={"blocking": [budget()], "evaluated": 1},
        maintenance={
            "exclusions": [
                {
                    "name": "hold-the-minor-lag",
                    "scope": "NO_MINOR_UPGRADES",
                    "end_time": "2027-01-05T18:27:10Z",
                    "in_effect": True,
                    "blocks": True,
                    "detail": "blocks auto-upgrade to 1.36.4-gke.1391002 until 2027-01-05T18:27Z",
                }
            ],
            "blocking_exclusions": ["hold-the-minor-lag"],
        },
        skew={
            "applicable": True,
            "pools": [
                {"name": "default-pool", "version": "1.34.12-gke.1011000", "minors_behind_target": 2, "verdict": "at ceiling", "detail": "2 minors behind the target control plane, at the skew ceiling"},
                {"name": "no-surge-pool", "version": "1.34.11-gke.1209000", "minors_behind_target": 2, "verdict": "at ceiling", "detail": "2 minors behind"},
            ],
        },
        webhooks={
            "blocking": [],
            "outage": [
                {
                    "webhook": "seeded-fail-closed-gate/gate.seeded.invalid",
                    "config_kind": "ValidatingWebhookConfiguration",
                    "config": "seeded-fail-closed-gate",
                    "name": "gate.seeded.invalid",
                    "service": "seeded-upgrade/nonexistent-admission-gate",
                    "reason": "Service seeded-upgrade/nonexistent-admission-gate does not exist",
                    "upgrade_path": [],
                    "rules": ["CREATE configmaps at v1"],
                    "live_rules": ["CREATE configmaps at v1"],
                }
            ],
        },
    )


def gemma() -> dict:
    return member(
        "gemma-gpu",
        "blocked",
        pdbs={"blocking": [budget("gemma-server", "kubeagents-system", 1)]},
        skew={"applicable": True, "pools": [{"name": "gpu-pool", "version": "1.31.14-gke.2704000", "minors_behind_target": 5, "verdict": "blocks", "detail": "5 minors behind"}]},
        webhooks={"blocking": [{"config_kind": "ValidatingWebhookConfiguration", "config": "gatekeeper", "name": "validation.gatekeeper.sh", "service": "gatekeeper-system/gatekeeper-webhook-service", "reason": "no ready endpoint", "upgrade_path": ["CREATE pods at v1"], "rules": ["CREATE pods at v1"]}], "outage": []},
    )


def with_rules() -> dict:
    return member(
        "seeded-a",
        "ready",
        rules={"surge-capacity": {"entry": 2, "can_block": True, "blocking": 1, "risks": 0}, "removed-node-label": {"entry": 12, "can_block": False, "blocking": 0, "risks": 1}},
        rule_blocking=[{"rule": "surge-capacity", "entry": 2, "pool": "no-surge-pool", "strategy": "SURGE", "max_surge": 0, "text": "pool no-surge-pool (maxSurge 0, maxUnavailable 1; 1 node) has no room for its displaced pods"}],
        risks=[
            {"rule": "removed-node-label", "entry": 12, "grade": "risk", "kind": "Deployment", "namespace": "seeded-shapes", "name": "arch-pinned-worker", "object": "seeded-shapes/arch-pinned-worker", "text": "selects on beta.kubernetes.io/arch=amd64"},
            {"rule": "zonal-control-plane", "entry": 11, "text": "control plane is zonal (us-central1-a)"},
        ],
    )


def severity(item: dict) -> str:
    return review.severity_of(item)


class TheMapping(unittest.TestCase):
    def test_a_singleton_budget_is_critical_and_a_spread_one_is_major(self):
        single = review.budget_findings(seeded_b())[0]
        spread = review.budget_findings(member("c", "blocked", pdbs={"blocking": [budget(replicas=3)]}))[0]
        self.assertEqual(severity(single), "critical")
        self.assertEqual(severity(spread), "major")
        self.assertEqual(single["check"], "upgrade-drain-blocking-budget")
        self.assertEqual((single["namespace"], single["object"]), ("seeded-upgrade", "pinned-batch-runner"))
        self.assertIn("maxUnavailable to 1", single["recommendation"]["action"])

    def test_skew_blocks_is_critical_and_the_ceiling_is_major(self):
        ceiling = review.skew_findings(seeded_b())
        blocks = review.skew_findings(gemma())
        self.assertEqual([severity(f) for f in ceiling], ["major", "major"])
        self.assertEqual(severity(blocks[0]), "critical")
        self.assertEqual({f["object"] for f in ceiling}, {"default-pool", "no-surge-pool"})

    def test_a_webhook_in_the_upgrade_path_is_critical_and_an_outage_is_major(self):
        outage = review.webhook_findings(seeded_b())[0]
        blocking = review.webhook_findings(gemma())[0]
        self.assertEqual(severity(outage), "major")
        self.assertEqual(severity(blocking), "critical")
        self.assertEqual(outage["object"], "validatingwebhookconfiguration/seeded-fail-closed-gate/gate.seeded.invalid")
        self.assertIn("in the upgrade's path", blocking["detail"])

    def test_an_exclusion_is_a_minor_posture(self):
        (exclusion,) = review.exclusion_findings(seeded_b())
        self.assertEqual(severity(exclusion), "minor")
        self.assertIn("2027-01-05", exclusion["title"])

    def test_rule_rows_map_by_entry_with_blockers_critical(self):
        found = review.rule_findings(with_rules())
        by_check = {f["check"]: f for f in found}
        self.assertEqual(set(by_check), {"upgrade-surge-capacity", "upgrade-removed-node-label", "upgrade-zonal-control-plane"})
        self.assertEqual(severity(by_check["upgrade-surge-capacity"]), "critical")
        self.assertEqual(by_check["upgrade-surge-capacity"]["object"], "no-surge-pool")
        self.assertEqual(severity(by_check["upgrade-removed-node-label"]), "major")
        self.assertEqual((by_check["upgrade-removed-node-label"]["namespace"], by_check["upgrade-removed-node-label"]["object"]), ("seeded-shapes", "Deployment/arch-pinned-worker"))
        self.assertEqual(severity(by_check["upgrade-zonal-control-plane"]), "minor")
        self.assertEqual(by_check["upgrade-zonal-control-plane"]["object"], review.CLUSTER_OBJECT)
        self.assertIn("Replace deprecated labels", by_check["upgrade-removed-node-label"]["recommendation"]["action"])

    def test_an_entry_2_rule_row_carries_the_rules_own_remedy(self):
        # The surge rule's Pod-range and accelerator shapes end with their remedy after the last
        # `; ` outside parentheses; the surge-settings shape carries none and keeps the entry's
        # mitigation, which names the default surge settings it lacks.
        pod_range = "pod range 10.4.0.0/23 has 0 free /24 block(s) (2 of 2 hold a node) and the 2 surge node(s) of pool(s) default-pool (maxSurge 1), gpu-pool (maxSurge 1) each need one: a surge node with no block never registers and the pool upgrade stalls between versions; lower max-surge or add a secondary Pod range"
        accelerator = "pool gpu-pool (1 x nvidia-l4) surges (maxSurge 1) with no reservation affinity: each surge node needs accelerator capacity the zone may not have, and a pool upgrade that cannot create one stalls between versions; hold a reservation for the surge node, or, on a pool with a spare accelerator node for the drained pods, set the in-place strategy (max-surge 0, max-unavailable 1)"
        self.assertEqual(review.rule_remedy(pod_range), "Lower max-surge or add a secondary Pod range")
        self.assertTrue(review.rule_remedy(accelerator).startswith("Hold a reservation for the surge node, or, on a pool with a spare accelerator node"))
        self.assertEqual(review.rule_remedy("pool no-surge-pool (maxSurge 0, maxUnavailable 1; 1 node) has no room for its displaced pods"), "")
        m = member(
            "seeded-b",
            "blocked",
            rules={"surge-capacity": {"entry": 2, "can_block": True, "blocking": 1, "risks": 1}},
            rule_blocking=[{"rule": "surge-capacity", "entry": 2, "pool": "default-pool", "text": pod_range}],
            risks=[{"rule": "surge-capacity", "entry": 2, "grade": "risk", "pool": "gpu-pool", "text": accelerator}],
        )
        actions = [f["recommendation"]["action"] for f in review.rule_findings(m)]
        self.assertEqual(actions[0], "Lower max-surge or add a secondary Pod range")
        self.assertTrue(actions[1].startswith("Hold a reservation for the surge node"), actions[1])
        (settings,) = review.rule_findings(with_rules())[:1]
        self.assertEqual(settings["recommendation"]["action"], review.ENTRY_MITIGATIONS[2])

    def test_every_payload_is_one_the_queue_accepts(self):
        for m in (seeded_b(), gemma(), with_rules()):
            for item in review.member_findings(m):
                fq.validate_finding(review.payload(item))
                self.assertNotIn("_entry", review.payload(item))

    def test_ids_are_stable_and_short_enough_to_type(self):
        first = review.item_id(review.budget_findings(seeded_b())[0])
        second = review.item_id(review.budget_findings(seeded_b())[0])
        self.assertEqual(first, second)
        self.assertEqual(first, "upgrade-drain-blocking-budget.acme-prod.seeded-b.seeded-upgrade.pinned-batch-runner")

    def test_a_member_is_complete_only_when_every_read_landed(self):
        self.assertTrue(review.member_complete(seeded_b(), []))
        failed = seeded_b()
        failed["readiness"]["read_error"] = "connection refused"
        self.assertFalse(review.member_complete(failed, []))
        self.assertFalse(review.member_complete(seeded_b(), [{"project": PROJECT, "location": "us-central1-a", "cluster": "seeded-b", "error": "x"}]))
        # A location whose get-server-config failed left its members no target:
        # no pool verdicts and no exclusion decisions, so the cluster was not
        # read in full even though no row names it, whatever verdict the watch keeps for it.
        location_failed = [{"project": PROJECT, "location": "us-central1-a", "message": "get-server-config: 503"}]
        self.assertFalse(review.member_complete(seeded_b(), location_failed))
        self.assertTrue(review.member_complete(seeded_b(), [{"project": PROJECT, "location": "europe-west1", "message": "get-server-config: 503"}]))
        self.assertTrue(review.member_complete(seeded_b(), [{"project": PROJECT, "message": "project unreadable"}]))
        # What the grader could not decide is not a read in full either: the
        # rows it would have written are missing, not cleared.
        self.assertFalse(review.member_complete(member("u", "unknown"), []))
        pool_unparsed = seeded_b()
        pool_unparsed["readiness"]["skew"]["unknown"] = ["odd-pool"]
        self.assertFalse(review.member_complete(pool_unparsed, []))
        exclusion_undecided = seeded_b()
        exclusion_undecided["readiness"]["maintenance"]["undecided_exclusions"] = ["freeze"]
        self.assertFalse(review.member_complete(exclusion_undecided, []))
        rule_failed = seeded_b()
        rule_failed["readiness"]["unknown"] = [{"rule": "surge-capacity", "entry": 2, "text": "raised"}]
        self.assertFalse(review.member_complete(rule_failed, []))

    def test_the_entry_titles_are_the_catalogue_headings(self):
        import re

        catalogue = Path(__file__).resolve().parents[3] / "docs" / "designs" / "upgrade-failure-catalogue.md"
        headings = {int(n): title.strip() for n, title in re.findall(r"^### (\d+)\. (.+)$", catalogue.read_text(encoding="utf-8"), re.M)}
        self.assertEqual(headings, review.ENTRY_TITLES)
        self.assertEqual(set(review.ENTRY_MITIGATIONS), set(headings))

    def test_the_prose_rubric_in_the_sop_and_the_readme_matches_the_script(self):
        # The SOP's live brief and the README grade by the same sentence; the
        # script's table is what the Saturday review applies.
        by_kind = {kind: review.severity_of({"rubric": review.rubric(v)}) for kind, v in review.RUBRIC.items()}
        self.assertEqual(
            by_kind,
            {"budget-singleton": "critical", "skew-blocks": "critical", "webhook-blocking": "critical", "rule-blocking": "critical", "budget": "major", "skew-ceiling": "major", "webhook-outage": "major", "exclusion": "minor"},
        )
        readme = Path(__file__).resolve().parents[1] / "cron" / "README.md"
        skill = Path(__file__).resolve().parents[1] / "skills" / "upgrade-readiness-brief" / "SKILL.md"
        by_entry = {entry: review.severity_of({"rubric": review.rubric(v)}) for entry, v in review.ENTRY_RISK_RUBRIC.items()}
        self.assertEqual(review.severity_of({"rubric": review.rubric(review.DEFAULT_RISK_RUBRIC)}), "major")
        self.assertEqual(set(by_entry.values()), {"minor", "major"})
        minors = [str(entry) for entry in sorted(by_entry) if by_entry[entry] == "minor"]
        split = f"its risk is minor for catalogue entries {', '.join(minors[:-1])} and {minors[-1]} and major for every other entry"
        for document in (SOP, readme, skill):
            text = " ".join(document.read_text(encoding="utf-8").split())
            critical, rest = text.split("are critical", 1)
            major, rest = rest.split("are major", 1)
            minor = rest.split("minor", 1)[0]
            self.assertIn("single replica", critical[-240:], document.name)
            self.assertIn("past the skew ceiling", critical[-240:], document.name)
            self.assertIn("upgrade's path", critical[-240:], document.name)
            self.assertIn("several replicas", major[-240:], document.name)
            self.assertIn("at the ceiling", major[-240:], document.name)
            self.assertIn("matches today", major[-240:], document.name)
            self.assertIn("covering exclusion", minor[-80:], document.name)
            # The eighth kind, a readiness rule's blocker, and the split of its risks by entry:
            # the worker on the live route cannot read the script's table, so the prose carries it.
            self.assertIn("blocker is critical", text, document.name)
            self.assertIn(split, text, document.name)

    def test_the_rubric_table_covers_the_queue_anchors(self):
        for values in list(review.RUBRIC.values()) + list(review.ENTRY_RISK_RUBRIC.values()) + [review.DEFAULT_RISK_RUBRIC]:
            fq.validate_rubric(review.rubric(values))


class Registration(unittest.TestCase):
    def test_batches_per_cluster_with_scope_only_when_complete(self):
        calls = []

        def fake_request(path, body, timeout=None):
            calls.append((path, body))
            return {"results": [{"id": fq.derive_finding_id(f["check"], f["project"], f["cluster"], f["namespace"], f["object"]), "outcome": "created"} for f in body["findings"]], "downgraded": 0}

        partial = gemma()
        partial["readiness"]["webhook_read_error"] = "timed out"
        members = [seeded_b(), partial]
        findings = [item for m in members for item in review.member_findings(m)]
        with mock.patch.object(review, "kv_request", fake_request):
            outcomes, problems, claimed = review.register(members, [], findings)
        self.assertEqual(problems, [])
        self.assertEqual(claimed, {f"{PROJECT}/us-central1-a/seeded-b"})
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1]["scope"], {"project": PROJECT, "cluster": "seeded-b", "complete": True, "check_prefix": review.CHECK_PREFIX, "source": review.SOURCE})
        self.assertNotIn("scope", calls[1][1])
        self.assertEqual(set(outcomes.values()), {"created"})
        self.assertEqual(len(outcomes), len(findings))

    def test_the_sends_wait_only_what_is_left_and_a_batch_past_the_stop_is_booked_not_sent(self):
        timeouts = []

        def fake_request(path, body, timeout=None):
            timeouts.append(timeout)
            return {"results": [{"id": fq.derive_finding_id(f["check"], f["project"], f["cluster"], f["namespace"], f["object"]), "outcome": "created"} for f in body["findings"]], "downgraded": 0}

        findings = review.member_findings(seeded_b())
        with mock.patch.object(review, "kv_request", fake_request), mock.patch.object(review.time, "monotonic", return_value=1000.0):
            # Ten seconds left: the send waits ten, not the full timeout.
            review.register([seeded_b()], [], findings, stop=1010.0)
            self.assertEqual(timeouts, [10.0])
            # Nothing left: no send, every row booked as not sent, and the claim counted as lost.
            outcomes, problems, claimed = review.register([seeded_b()], [], findings, stop=1000.0)
        self.assertEqual(timeouts, [10.0])
        self.assertEqual(set(outcomes.values()), {review.REGISTRATION_FAILED})
        self.assertEqual(claimed, set())
        self.assertEqual(problems, [f"{PROJECT}/seeded-b: {review.CLAIM_FAILED_MARK}: {review.REGISTRATION_OUT_OF_TIME_DETAIL}"])

    def test_a_busy_queue_gets_the_same_batch_once_more_after_a_pause(self):
        calls = []

        def busy_once(path, body, timeout=None):
            calls.append(body)
            if len(calls) == 1:
                raise review.QueueBusy("HTTP 503 Service Unavailable: the findings queue is busy")
            return {"results": [{"id": fq.derive_finding_id(f["check"], f["project"], f["cluster"], f["namespace"], f["object"]), "outcome": "created"} for f in body["findings"]], "downgraded": 0}

        findings = review.member_findings(seeded_b())
        with mock.patch.object(review, "kv_request", busy_once), mock.patch.object(review.time, "sleep") as slept:
            outcomes, problems, claimed = review.register([seeded_b()], [], findings)
        self.assertEqual((problems, claimed), ([], {f"{PROJECT}/us-central1-a/seeded-b"}))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        slept.assert_called_once_with(review.BUSY_RETRY_SECONDS)
        # Busy again: one retry only, then the batch and its claim are booked as not sent.
        calls.clear()

        def always_busy(path, body, timeout=None):
            calls.append(body)
            raise review.QueueBusy("HTTP 503 Service Unavailable: the findings queue is busy")

        with mock.patch.object(review, "kv_request", always_busy), mock.patch.object(review.time, "sleep"):
            outcomes, problems, claimed = review.register([seeded_b()], [], findings)
        self.assertEqual(len(calls), 2)
        self.assertEqual((set(outcomes.values()), claimed), ({review.REGISTRATION_FAILED}, set()))
        self.assertIn(review.CLAIM_FAILED_MARK, problems[0])

    def test_a_complete_cluster_with_nothing_to_report_sends_an_empty_scoped_batch(self):
        calls = []

        def fake_request(path, body, timeout=None):
            calls.append(body)
            return {"results": [], "downgraded": 0}

        clean = member("seeded-c")
        with mock.patch.object(review, "kv_request", fake_request):
            outcomes, problems, claimed = review.register([clean], [], [])
        self.assertEqual((outcomes, problems, claimed), ({}, [], {f"{PROJECT}/us-central1-a/seeded-c"}))
        self.assertEqual(calls, [{"findings": [], "scope": {"project": PROJECT, "cluster": "seeded-c", "complete": True, "check_prefix": review.CHECK_PREFIX, "source": review.SOURCE}}])
        partial = member("seeded-c")
        partial["readiness"]["read_error"] = "timed out"
        calls.clear()
        with mock.patch.object(review, "kv_request", fake_request):
            review.register([partial], [], [])
        self.assertEqual(calls, [])

    def test_an_answer_that_does_not_acknowledge_the_batch_registers_nothing_and_lands_no_claim(self):
        # A redirected POST that urllib turned into a GET answers the findings list: a dict,
        # status 200, no results. Neither its rows nor its claim count as registered.
        def listing_answer(path, body, timeout=None):
            return {"findings": []}

        members = [seeded_b(), member("seeded-c")]
        findings = [item for m in members for item in review.member_findings(m)]
        with mock.patch.object(review, "kv_request", listing_answer):
            outcomes, problems, claimed = review.register(members, [], findings)
        self.assertEqual(claimed, set())
        self.assertEqual(set(outcomes.values()), {review.REGISTRATION_FAILED})
        self.assertEqual(len(problems), 2)
        self.assertTrue(all(review.CLAIM_FAILED_MARK in p and "no list of results" in p and "no count of the rows" in p for p in problems), problems)

        # Results for some rows only, or a claim answered without the count it lowered, are refused too.
        def partial_answer(path, body, timeout=None):
            return {"results": [{"id": review.item_id(item), "outcome": "created"} for item in findings[:1]], "downgraded": 0}

        def no_count(path, body, timeout=None):
            return {"results": [{"id": fq.validate_finding(f)["id"], "outcome": "created"} for f in body["findings"]]}

        with mock.patch.object(review, "kv_request", partial_answer):
            _outcomes, problems, claimed = review.register(members, [], findings)
        # seeded-b's batch came back short, and seeded-c's empty batch came back with an id it never
        # sent: neither answer is that batch's, so neither claim lands.
        self.assertEqual(claimed, set())
        self.assertEqual([p.split(":")[0] for p in problems], [f"{PROJECT}/seeded-b", f"{PROJECT}/seeded-c"])
        # Each line names its own cause: rows short of a result, or a result for a row never sent.
        self.assertIn(f"{len(findings) - 1} of {len(findings)} row(s) without a result", problems[0])
        self.assertNotIn("no count of the rows", problems[0])
        self.assertIn("1 result(s) for row(s) the batch never sent", problems[1])
        self.assertNotIn("without a result", problems[1])
        with mock.patch.object(review, "kv_request", no_count):
            _outcomes, problems, claimed = review.register(members, [], findings)
        self.assertEqual(claimed, set())
        self.assertEqual(len(problems), 2)
        self.assertTrue(all("no count of the rows the claim lowered" in p and "without a result" not in p for p in problems), problems)

        # A result for an id the batch never sent is not this batch's answer, whatever else it holds.
        def extra_id(path, body, timeout=None):
            results = [{"id": fq.validate_finding(f)["id"], "outcome": "created"} for f in body["findings"]]
            return {"results": results + [{"id": "upgrade-pool-skew.elsewhere.other._.pool", "outcome": "created"}], "downgraded": 0}

        with mock.patch.object(review, "kv_request", extra_id):
            _outcomes, problems, claimed = review.register(members, [], findings)
        self.assertEqual(claimed, set())
        self.assertEqual(len(problems), 2)
        self.assertTrue(all("1 result(s) for row(s) the batch never sent" in p and "without a result" not in p and "no count" not in p for p in problems), problems)

    def test_a_cluster_past_the_batch_limit_is_sent_in_pieces_without_a_claim(self):
        calls = []

        def fake_request(path, body, timeout=None):
            calls.append(body)
            return {"results": [{"id": fq.derive_finding_id(f["check"], f["project"], f["cluster"], f["namespace"], f["object"]), "outcome": "created"} for f in body["findings"]], "downgraded": 0}

        big = member("seeded-z", "blocked", pdbs={"blocking": [budget(name=f"b{i}") for i in range(review.KV_BATCH_LIMIT + 1)]})
        findings = review.member_findings(big)
        with mock.patch.object(review, "kv_request", fake_request):
            outcomes, problems, claimed = review.register([big], [], findings)
        self.assertEqual(problems, [])
        self.assertEqual(claimed, set())
        self.assertEqual([len(c["findings"]) for c in calls], [review.KV_BATCH_LIMIT, 1])
        self.assertTrue(all("scope" not in c for c in calls))
        self.assertEqual(len(outcomes), review.KV_BATCH_LIMIT + 1)

    def test_a_cluster_sent_in_pieces_anyway_counts_no_withheld_claim_for_a_refused_row(self):
        """A refused row withholds a claim only where one would have been sent: a cluster
        past the batch limit claims nothing either way, so no lost claim is counted."""
        def fake_request(path, body, timeout=None):
            return {"results": [{"id": fq.derive_finding_id(f["check"], f["project"], f["cluster"], f["namespace"], f["object"]), "outcome": "created"} for f in body["findings"]], "downgraded": 0}

        big = member("seeded-z", "blocked", pdbs={"blocking": [budget(name=f"b{i}") for i in range(3)]})
        findings = review.member_findings(big)
        real = fq.validate_finding

        def strict(raw):
            if raw.get("object") == "b0":
                raise fq.FindingError("refused for the test")
            return real(raw)

        with mock.patch.object(review, "KV_BATCH_LIMIT", 2), mock.patch.object(review, "kv_request", fake_request), mock.patch.object(review.findings_queue, "validate_finding", strict):
            _outcomes, problems, claimed = review.register([big], [], findings)
        self.assertEqual(claimed, set())
        self.assertEqual(len(problems), 1, problems)
        self.assertNotIn(review.CLAIM_FAILED_MARK, problems[0])

    def test_same_named_clusters_in_two_locations_are_two_batches_and_no_claim(self):
        # Through the real queue: the queue keys rows and claims by project and
        # cluster, so a claim from either twin would lower the other's rows the
        # moment after they were written. Two batches go; no claim does.
        import sqlite3

        conn = sqlite3.connect(":memory:")
        fq.init_findings_schema(conn)
        self.addCleanup(conn.close)
        calls = []

        def through_the_queue(path, body, timeout=None):
            calls.append(body)
            return fq.register_findings(conn, body["findings"], body.get("scope"))

        west = member("prod", "blocked", pdbs={"blocking": [budget(name="west-budget")]})
        east = member("prod", "blocked", pdbs={"blocking": [budget(name="east-budget")]})
        east["location"] = "europe-west1"
        findings = review.member_findings(west) + review.member_findings(east)
        with mock.patch.object(review, "kv_request", through_the_queue):
            outcomes, problems, claimed = review.register([west, east], [], findings)
        self.assertEqual(problems, [])
        self.assertEqual(claimed, set())
        self.assertEqual([[f["object"] for f in c["findings"]] for c in calls], [["west-budget"], ["east-budget"]])
        self.assertTrue(all("scope" not in c for c in calls))
        self.assertEqual(set(outcomes.values()), {"created"})
        for row in fq.ranked_findings(conn):
            self.assertIsNone(row["absent_since"], row["id"])
            self.assertEqual(row["rubric"]["C"], 1.0, row["id"])
        # A lone cluster of that name still claims.
        with mock.patch.object(review, "kv_request", through_the_queue):
            _outcomes, _problems, claimed = review.register([west], [], review.member_findings(west))
        self.assertEqual(claimed, {f"{PROJECT}/us-central1-a/prod"})

    def test_twins_whose_objects_coincide_share_one_finding(self):
        calls = []

        def fake_request(path, body, timeout=None):
            calls.append(body)
            return {"results": [{"id": fq.derive_finding_id(f["check"], f["project"], f["cluster"], f["namespace"], f["object"]), "outcome": "created"} for f in body["findings"]], "downgraded": 0}

        west = member("prod", "blocked", pdbs={"blocking": [budget(name="pinned")]})
        east = member("prod", "blocked", pdbs={"blocking": [budget(name="pinned")]})
        east["location"] = "europe-west1"
        findings = review.fold_shared_ids(review.member_findings(west) + review.member_findings(east))
        self.assertEqual(len(findings), 1)
        self.assertIn("also on acme-prod/europe-west1/prod", findings[0]["detail"])
        with mock.patch.object(review, "kv_request", fake_request):
            outcomes, problems, claimed = review.register([west, east], [], review.member_findings(west) + review.member_findings(east))
        self.assertEqual(problems, [])
        self.assertEqual(claimed, set())
        self.assertEqual(len(calls), 1)  # one POST carries the one finding; the other twin has nothing left to send and no claim
        self.assertEqual(set(outcomes.values()), {"created"})
        self.assertEqual(len(outcomes), 1)

    def test_an_exclusion_named_without_a_letter_or_digit_registers_under_a_digest(self):
        odd = seeded_b()
        odd["readiness"]["maintenance"]["exclusions"].append({"name": "冻结", "scope": "NO_MINOR_UPGRADES", "end_time": "2027-01-05T00:00:00Z", "blocks": True, "detail": "a freeze named in CJK"})
        found = [f for f in review.exclusion_findings(odd) if "冻结" in f["title"]]
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["object"].startswith("exclusion-"))
        fq.validate_finding(review.payload(found[0]))  # the queue accepts it
        again = [f for f in review.exclusion_findings(odd) if "冻结" in f["title"]][0]
        self.assertEqual(found[0]["object"], again["object"])  # stable across runs

    def test_an_object_past_the_queues_cap_keys_the_same_id_on_both_sides(self):
        long_name = "a" * (fq.MAX_LABEL_CHARS + 40)
        found = review.budget_findings(member("c", "blocked", pdbs={"blocking": [budget(name=long_name)]}))[0]
        self.assertEqual(len(found["object"]), fq.MAX_LABEL_CHARS)
        capped = fq.derive_finding_id(found["check"], found["project"], found["cluster"], found["namespace"], long_name[: fq.MAX_LABEL_CHARS])
        uncapped = fq.derive_finding_id(found["check"], found["project"], found["cluster"], found["namespace"], long_name)
        self.assertNotEqual(capped, uncapped)
        self.assertEqual(review.item_id(found), capped)

    def test_an_error_body_cut_short_is_still_this_batchs_failure(self):
        import http.client
        import urllib.error

        class CutShort:
            def read(self):
                raise http.client.IncompleteRead(b"{")

        def refuse(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, CutShort())

        with mock.patch.object(review.urllib.request, "urlopen", refuse):
            with self.assertRaises(OSError) as caught:
                review.kv_request(review.KV_FINDINGS_PATH, {"findings": []})
        self.assertEqual(str(caught.exception), "HTTP 503 Service Unavailable")
        # The one answer that wrote nothing is told apart, so it may be sent again.
        self.assertIsInstance(caught.exception, review.QueueBusy)

        def bad_request(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, CutShort())

        with mock.patch.object(review.urllib.request, "urlopen", bad_request):
            with self.assertRaises(OSError) as caught:
                review.kv_request(review.KV_FINDINGS_PATH, {"findings": []})
        self.assertNotIsInstance(caught.exception, review.QueueBusy)

    def test_an_answer_that_is_not_an_object_is_this_batchs_failure(self):
        import io

        class Answer(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch.object(review.urllib.request, "urlopen", lambda req, timeout: Answer(b"[]")):
            with self.assertRaises(OSError) as caught:
                review.kv_request(review.KV_FINDINGS_PATH, {"findings": []})
        self.assertIn("not an object", str(caught.exception))

    def test_twins_rows_count_under_both_members_in_the_record(self):
        west = member("prod", "blocked", pdbs={"blocking": [budget(name="pinned")]})
        east = member("prod", "blocked", pdbs={"blocking": [budget(name="pinned")]})
        east["location"] = "europe-west1"
        findings = review.fold_shared_ids(review.member_findings(west) + review.member_findings(east))
        doc = review.summary(stamp="20261018T060000Z", now=NOW, names=[PROJECT], members=[west, east], errors=[], failures={}, findings=findings, outcomes={review.item_id(f): "created" for f in findings}, problems=[], previous=None, report_path=Path("x"))
        self.assertEqual(doc["findings"][0]["members"], [f"{PROJECT}/us-central1-a/prod", f"{PROJECT}/europe-west1/prod"])
        report = review.render_report(doc, findings, {}, [])
        self.assertIn(f"| {PROJECT}/us-central1-a/prod | blocked | 1 | 0 | 0 |", report)
        self.assertIn(f"| {PROJECT}/europe-west1/prod | blocked | 1 | 0 | 0 |", report)
        self.assertIn(f"- Where: {PROJECT}/us-central1-a/prod, {PROJECT}/europe-west1/prod", report)

    def test_a_row_the_queue_would_refuse_fails_alone(self):
        calls = []

        def fake_request(path, body, timeout=None):
            calls.append(body)
            return {"results": [{"id": fq.derive_finding_id(f["check"], f["project"], f["cluster"], f["namespace"], f["object"]), "outcome": "created"} for f in body["findings"]], "downgraded": 0}

        odd = seeded_b()
        odd["readiness"]["maintenance"]["exclusions"].append({"name": "freeze", "scope": "NO_MINOR_UPGRADES", "end_time": "2027-01-05T00:00:00Z", "blocks": True, "detail": "a second exclusion"})
        findings = review.member_findings(odd)
        real = fq.validate_finding

        def strict(raw):
            if raw.get("object") == "freeze":
                raise fq.FindingError("refused for the test")
            return real(raw)

        with mock.patch.object(review, "kv_request", fake_request), mock.patch.object(review.findings_queue, "validate_finding", strict):
            outcomes, problems, claimed = review.register([odd], [], findings)
        self.assertEqual(len(problems), 2)
        self.assertIn("freeze", problems[0])
        self.assertEqual(len(calls), 1)
        self.assertNotIn("freeze", [f["object"] for f in calls[0]["findings"]])
        # The refused row is a finding read and not sent: a claim would book its row from last week absent.
        # The claim is withheld, and counted as one that did not reach the queue: last week's rows keep their confidence.
        self.assertNotIn("scope", calls[0])
        self.assertEqual(claimed, set())
        self.assertIn(review.CLAIM_FAILED_MARK, problems[1])
        self.assertEqual(sum(1 for v in outcomes.values() if v == review.REGISTRATION_FAILED), 1)
        self.assertEqual(sum(1 for v in outcomes.values() if v == "created"), len(findings) - 1)

    def test_an_answer_cut_short_is_this_batchs_failure(self):
        import http.client

        def cut_short(req, timeout):
            raise http.client.IncompleteRead(b"{")

        with mock.patch.object(review.urllib.request, "urlopen", cut_short):
            with self.assertRaises(OSError) as caught:
                review.kv_request(review.KV_FINDINGS_PATH, {"findings": []})
        self.assertIn("IncompleteRead", str(caught.exception))

    def test_a_budget_over_a_scaled_to_zero_and_a_running_workload_is_not_a_singleton(self):
        pair = budget(replicas=3)
        pair["workloads"].append({"kind": "Deployment", "namespace": "seeded-upgrade", "name": "idle", "replicas": 0})
        found = review.budget_findings(member("c", "blocked", pdbs={"blocking": [pair]}))[0]
        self.assertEqual(severity(found), "major")
        none_running = budget(replicas=0)
        self.assertEqual(severity(review.budget_findings(member("c", "blocked", pdbs={"blocking": [none_running]}))[0]), "critical")

    def test_a_boolean_entry_is_no_entry(self):
        self.assertEqual(review.rule_entry({"rule": "odd", "entry": True}, {}), ("odd", None))
        self.assertEqual(review.rule_entry({"rule": "odd", "entry": 12}, {}), ("odd", 12))

    def test_a_refused_batch_keeps_the_queues_reason(self):
        import io
        import urllib.error

        def refuse(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"error": "findings[0]: object is required"}'))

        with mock.patch.object(review.urllib.request, "urlopen", refuse):
            with self.assertRaises(OSError) as caught:
                review.kv_request(review.KV_FINDINGS_PATH, {"findings": []})
        self.assertIn("400", str(caught.exception))
        self.assertIn("object is required", str(caught.exception))

    def test_a_failed_batch_is_reported_and_marks_its_rows(self):
        def failing(path, body, timeout=None):
            raise OSError("connection refused")

        members = [seeded_b()]
        findings = review.member_findings(members[0])
        with mock.patch.object(review, "kv_request", failing):
            outcomes, problems, _claimed = review.register(members, [], findings)
        self.assertEqual(len(problems), 1)
        self.assertIn("seeded-b", problems[0])
        self.assertEqual(set(outcomes.values()), {review.REGISTRATION_FAILED})


class TheRecord(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "upgrade-readiness"
        self.env = mock.patch.dict(os.environ, {watch.WATCH_HOME_ENV: str(self.home), watch.PROJECTS_ENV: f"{PROJECT},{OTHER_PROJECT}"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def summary_for(self, members, outcomes=None, previous=None, claimed=None):
        findings = [item for m in members for item in review.member_findings(m)]
        outcomes = outcomes if outcomes is not None else {review.item_id(f): "created" for f in findings}
        doc = review.summary(stamp="20261018T060000Z", now=NOW, names=[PROJECT], members=members, errors=[], failures={}, findings=findings, outcomes=outcomes, problems=[], previous=previous, report_path=self.home / "x", claimed=claimed)
        return doc, findings

    def test_the_summary_counts_verdicts_severities_and_the_delta(self):
        doc, findings = self.summary_for([seeded_b(), gemma(), member("seeded-c")])
        self.assertEqual(doc["clusters"]["blocked"], [f"{PROJECT}/us-central1-a/gemma-gpu", f"{PROJECT}/us-central1-a/seeded-b"])
        self.assertEqual(doc["clusters"]["ready"], [f"{PROJECT}/us-central1-a/seeded-c"])
        self.assertEqual(doc["counts"]["critical"], 4)  # seeded-b's budget; gemma's budget, skew and webhook
        self.assertEqual(len(doc["new_ids"]), len(findings))
        # Last week's rows count as no longer reported only on a cluster this
        # run read; a cluster not read this week keeps its rows unjudged.
        gone = {"id": "upgrade-pool-skew.acme-prod.seeded-b._.old-pool", "project": PROJECT, "cluster": "seeded-b"}
        unread = {"id": "upgrade-pool-skew.acme-prod.old-cluster._.default-pool", "project": PROJECT, "cluster": "old-cluster"}
        half_read = {"id": "upgrade-pool-skew.acme-prod.gemma-gpu._.old-pool", "project": PROJECT, "cluster": "gemma-gpu"}
        kept = dict(doc["findings"][0])
        partial = gemma()
        partial["readiness"]["read_error"] = "timed out"
        doc2, _ = self.summary_for([seeded_b(), partial], previous={"findings": [gone, unread, half_read, kept]})
        self.assertEqual(doc2["no_longer_reported_ids"], [gone["id"]])
        # A cluster read in full whose claim never reached the queue (a batch
        # refused, or sent in pieces) keeps last week's rows unjudged too.
        doc3, _ = self.summary_for([seeded_b(), partial], previous={"findings": [gone, unread, half_read, kept]}, claimed=set())
        self.assertEqual(doc3["no_longer_reported_ids"], [])
        doc4, _ = self.summary_for([seeded_b(), partial], previous={"findings": [gone, unread, half_read, kept]}, claimed={f"{PROJECT}/us-central1-a/seeded-b"})
        self.assertEqual(doc4["no_longer_reported_ids"], [gone["id"]])
        # A row is judged by its project and cluster, as the queue lowers it: the us-east1 twin is gone,
        # and the cluster of that name read in full this week no longer reports the row the two shared,
        # nor the one only the gone twin wrote.
        folded = {"id": "upgrade-drain-blocking-budget.acme-prod.seeded-b.ns.web", "project": PROJECT, "cluster": "seeded-b",
                  "member": f"{PROJECT}/us-east1/seeded-b", "members": [f"{PROJECT}/us-east1/seeded-b", f"{PROJECT}/us-central1-a/seeded-b"]}
        lone = {"id": "upgrade-drain-blocking-budget.acme-prod.seeded-b.ns.east-only", "project": PROJECT, "cluster": "seeded-b",
                "member": f"{PROJECT}/us-east1/seeded-b", "members": [f"{PROJECT}/us-east1/seeded-b"]}
        doc5, _ = self.summary_for([seeded_b()], previous={"findings": [folded, lone]}, claimed={f"{PROJECT}/us-central1-a/seeded-b"})
        self.assertEqual(doc5["no_longer_reported_ids"], [folded["id"], lone["id"]])

    def test_the_report_names_every_finding_with_its_id_and_action(self):
        doc, findings = self.summary_for([seeded_b()])
        # The reporter's last line names the loader's output file, gone before anyone reads the record.
        text = review.render_report(doc, findings, {PROJECT: "| table |\n\nWrote 1 member(s) to /tmp/upgrade-readiness-watch-x/report.json\n"}, [])
        for row in doc["findings"]:
            self.assertIn(f"`{row['id']}`", text)
            self.assertIn(row["action"], text)
        self.assertIn("| acme-prod/us-central1-a/seeded-b | blocked | 1 | 3 | 1 |", text)
        self.assertIn("## Report script output", text)
        self.assertIn("| table |", text)
        self.assertNotIn("Wrote 1 member(s)", text)
        self.assertIn("dismiss", text)

    def test_the_record_is_written_pruned_and_the_latest_files_refreshed(self):
        doc, findings = self.summary_for([seeded_b()])
        home = review.weekly_home()
        for stamp in [f"2026100{i}T060000Z" for i in range(1, 10)]:
            review.write_record(home, stamp, f"# r {stamp}\n", {"members": []}, {"findings": []}, {**doc, "stamp": stamp})
        runs = sorted(p.name for p in home.iterdir() if p.is_dir())
        self.assertEqual(len(runs), review.KEEP_RUNS)
        self.assertEqual(runs[-1], "20261009T060000Z")
        # The latest files are the newest run's, with no temporary left.
        self.assertEqual(json.loads((home / review.LATEST_JSON_NAME).read_text())["stamp"], "20261009T060000Z")
        self.assertEqual((home / review.LATEST_MD_NAME).read_text(), "# r 20261009T060000Z\n")
        self.assertEqual(review.load_previous(home)["stamp"], "20261009T060000Z")
        self.assertEqual([p.name for p in home.iterdir() if p.name.endswith(review.TMP_SUFFIX)], [])

    def test_the_sandbox_copy_program_writes_both_files_readable(self):
        program = review.sandbox_copy_program({"latest.md": "# r 🎄 exclusion\n", "latest.json": "{}"})
        self.assertIn(review.SANDBOX_COPY_DIR, program)
        self.assertIn("os.chmod", program)
        fake_dir = Path(self.tmp.name) / "sandbox"
        exec_globals = {}
        exec(program.replace(json.dumps(review.SANDBOX_COPY_DIR), json.dumps(str(fake_dir))), exec_globals)  # noqa: S102 - the program under test
        self.assertEqual(sorted(p.name for p in fake_dir.iterdir()), ["latest.json", "latest.md"])
        self.assertEqual((fake_dir / "latest.md").read_text(encoding="utf-8"), "# r 🎄 exclusion\n")
        self.assertEqual(oct(os.stat(fake_dir / "latest.md").st_mode & 0o777), oct(review.SANDBOX_COPY_MODE))
        # Read-only files in a read-only directory: a model turn's `cp` over the
        # copy fails instead of replacing the review's record.
        self.assertEqual(review.SANDBOX_COPY_MODE & 0o222, 0)
        self.assertEqual(oct(os.stat(fake_dir).st_mode & 0o777), oct(review.SANDBOX_COPY_DIR_CLOSED_MODE))
        if os.geteuid() != 0:  # root ignores the mode bits these two assertions rely on
            with self.assertRaises(PermissionError):
                (fake_dir / "latest.md").write_text("clobbered")
            with self.assertRaises(PermissionError):
                (fake_dir / "fleet_versions.json").write_text("{}")
        # The next review replaces both files through the same program.
        second = review.sandbox_copy_program({"latest.md": "# second\n", "latest.json": "{\"n\": 2}"})
        exec(second.replace(json.dumps(review.SANDBOX_COPY_DIR), json.dumps(str(fake_dir))), {})  # noqa: S102 - the program under test
        self.assertEqual((fake_dir / "latest.md").read_text(encoding="utf-8"), "# second\n")
        self.assertEqual(oct(os.stat(fake_dir).st_mode & 0o777), oct(review.SANDBOX_COPY_DIR_CLOSED_MODE))
        self.assertEqual(sorted(p.name for p in fake_dir.iterdir()), ["latest.json", "latest.md"])  # no temporary file left behind
        # A write that fails leaves the previous pair whole and the directory closed.
        broken = review.sandbox_copy_program({"latest.md": "# third\n", "latest.json": "{}"}).replace(json.dumps(review.SANDBOX_COPY_DIR), json.dumps(str(fake_dir)))
        # The failure lands after the first temporary is made read-only and
        # before its rename: the stray the next run has to get past.
        broken = broken.replace("        os.chmod(os.path.join(DIR, name + TMP), MODE)\n", "        os.chmod(os.path.join(DIR, name + TMP), MODE)\n        raise OSError('ssh dropped')\n", 1)
        self.assertIn("ssh dropped", broken)
        with self.assertRaises(OSError):
            exec(broken, {})  # noqa: S102 - the program under test
        self.assertEqual((fake_dir / "latest.md").read_text(encoding="utf-8"), "# second\n")
        self.assertEqual(oct(os.stat(fake_dir).st_mode & 0o777), oct(review.SANDBOX_COPY_DIR_CLOSED_MODE))
        strays = [p for p in fake_dir.iterdir() if p.name.endswith(review.TMP_SUFFIX)]
        self.assertEqual(len(strays), 2)  # both temporaries written, one already read-only
        self.assertIn(oct(review.SANDBOX_COPY_MODE), {oct(os.stat(p).st_mode & 0o777) for p in strays})
        # The next run replaces the strays itself: nobody has to clean up by hand.
        fourth = review.sandbox_copy_program({"latest.md": "# fourth\n", "latest.json": "{}"}).replace(json.dumps(review.SANDBOX_COPY_DIR), json.dumps(str(fake_dir)))
        exec(fourth, {})  # noqa: S102 - the program under test
        self.assertEqual((fake_dir / "latest.md").read_text(encoding="utf-8"), "# fourth\n")
        self.assertEqual(sorted(p.name for p in fake_dir.iterdir()), ["latest.json", "latest.md"])
        os.chmod(fake_dir, review.SANDBOX_COPY_DIR_OPEN_MODE)  # so the fixture's cleanup can remove it

    def test_without_an_ssh_sandbox_the_copy_is_a_plain_local_write(self):
        target = Path(self.tmp.name) / "local-copy"

        def never(*args, **kwargs):
            raise AssertionError("no remote program without an ssh sandbox")

        with mock.patch.object(review.sandbox_exec, "sandbox_enabled", lambda path=None: False), mock.patch.object(review.sandbox_exec, "run", never), mock.patch.object(review, "SANDBOX_COPY_DIR", str(target)):
            self.assertIsNone(review.copy_to_sandbox("# r\n", {"n": 1}))
        self.assertEqual((target / review.LATEST_MD_NAME).read_text(encoding="utf-8"), "# r\n")
        self.assertTrue(os.access(target, os.W_OK))
        self.assertEqual(sorted(p.name for p in target.iterdir()), ["latest.json", "latest.md"])
        # A write that fails before both temporaries exist leaves the previous pair whole.
        (target / (review.LATEST_JSON_NAME + review.TMP_SUFFIX)).mkdir()
        with mock.patch.object(review.sandbox_exec, "sandbox_enabled", lambda path=None: False), mock.patch.object(review.sandbox_exec, "run", never), mock.patch.object(review, "SANDBOX_COPY_DIR", str(target)):
            self.assertIsNotNone(review.copy_to_sandbox("# newer\n", {"n": 2}))
        self.assertEqual((target / review.LATEST_MD_NAME).read_text(encoding="utf-8"), "# r\n")
        (target / (review.LATEST_JSON_NAME + review.TMP_SUFFIX)).rmdir()
        # The record's own directory is never the copy's target.
        with mock.patch.object(review.sandbox_exec, "sandbox_enabled", lambda path=None: False), mock.patch.object(review.sandbox_exec, "run", never), mock.patch.object(review, "SANDBOX_COPY_DIR", str(review.weekly_home())):
            self.assertIsNone(review.copy_to_sandbox("# r\n", {"n": 1}))
        self.assertFalse((review.weekly_home() / review.LATEST_MD_NAME).exists())

    def test_the_sop_reads_the_fields_the_summary_writes(self):
        # Step 1 of the brief SOP lists the summary's keys in backticks; a
        # renamed key here breaks every install's Saturday brief with no signal,
        # so the list is held to the dict `summary()` writes.
        import re
        doc, _ = self.summary_for([seeded_b()])
        text = SOP.read_text()
        step_one = text.split("### 1. Read the review", 1)[1].split("### 2.", 1)[0]
        named = set(re.findall(r"`([a-z_]+)`", step_one))
        row_keys = set(doc["findings"][0]) if doc["findings"] else set()
        self.assertTrue(named, "the SOP names no fields")
        for key in named - {"generated_at"}:
            self.assertTrue(key in doc or key in row_keys, f"the SOP names `{key}`, which the summary does not write")
        for key in ("generated_at", "projects", "clusters", "counts", "findings", "new_ids", "suppressed_ids", "no_longer_reported_ids", "project_failures", "registration_errors", "report_path"):
            self.assertIn(key, named, f"the SOP does not tell the brief about `{key}`")

    def test_projects_last_week_could_not_read_go_first(self):
        # Only a failure the order can repair moves a project up: the budget
        # ran out before it, or cut its run short. A run that timed out on its
        # own, or was refused, keeps its place.
        cut = review.CUT_SHORT_DETAIL.format(seconds=300)
        self.assertEqual(review.ordered_names(["a", "b", "c"], {"project_failures": {"c": cut}}), ["c", "a", "b"])
        self.assertEqual(review.ordered_names(["a", "b", "c"], {"project_failures": {"c": review.BUDGET_EXHAUSTED_DETAIL}}), ["c", "a", "b"])
        self.assertEqual(review.ordered_names(["a", "b", "c"], {"project_failures": {"c": "the sandbox run timed out after 1500s"}}), ["a", "b", "c"])
        self.assertEqual(review.ordered_names(["a", "b"], None), ["a", "b"])
        # A spelling by number runs after the ids in its group.
        self.assertEqual(review.ordered_names(["123456789012", "acme-prod"], None), ["acme-prod", "123456789012"])
        # A run whose every project failed writes no record: the marker it left carries its failures.
        review.announce_failure_once("x", {"a": "sandbox exited 255 without a report", "c": cut})
        self.assertEqual(review.ordered_names(["a", "b", "c"], {"project_failures": {}}), ["c", "a", "b"])
        review.clear_failure_marker()
        # Last week's failure is keyed by the id the watch learned for the number the roster spells.
        with mock.patch.object(review, "learned_project_ids", return_value={"123456789012": "acme-prod"}):
            self.assertEqual(review.ordered_names(["acme-dev", "123456789012"], {"project_failures": {"acme-prod": cut}}), ["123456789012", "acme-dev"])


class TheTick(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "upgrade-readiness"
        self.env = mock.patch.dict(os.environ, {watch.WATCH_HOME_ENV: str(self.home), watch.PROJECTS_ENV: PROJECT, "SESSION_KV_API_KEY": "k"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.registered = []
        self.copies = []
        self.patches = [
            mock.patch.object(watch, "declared_scope", return_value=None),
            mock.patch.object(watch, "run_report", side_effect=self.fake_run_report),
            mock.patch.object(review, "kv_request", side_effect=self.fake_kv),
            mock.patch.object(review, "copy_to_sandbox", side_effect=self.fake_copy),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.envelope = {"exit": 0, "tables": "| t |", "report": {"members": [seeded_b(), member("seeded-c")], "errors": []}}

    def fake_run_report(self, names, readiness, *, scope, outside_stands, timeout=None):
        self.asked = (names, readiness, scope, timeout)
        return self.envelope

    def fake_kv(self, path, body, timeout=None):
        self.registered.append(body)
        return {"results": [{"id": fq.derive_finding_id(f["check"], f["project"], f["cluster"], f["namespace"], f["object"]), "outcome": "created"} for f in body["findings"]], "downgraded": 0}

    def fake_copy(self, report_md, summary_doc):
        self.copies.append((report_md, summary_doc))
        return None

    def run_main(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = review.main(list(argv))
        return code, out.getvalue()

    def test_a_clean_run_registers_writes_copies_and_says_nothing(self):
        code, out = self.run_main()
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(self.asked[0], [PROJECT])
        self.assertTrue(self.asked[1])
        self.assertEqual(len(self.registered), 2)  # seeded-b's findings, and seeded-c's complete empty batch
        self.assertEqual(self.registered[0]["scope"]["cluster"], "seeded-b")
        self.assertEqual((self.registered[1]["findings"], self.registered[1]["scope"]["cluster"]), ([], "seeded-c"))
        home = review.weekly_home()
        self.assertTrue((home / review.LATEST_MD_NAME).exists())
        latest = json.loads((home / review.LATEST_JSON_NAME).read_text())
        self.assertEqual(latest["clusters"]["blocked"], [f"{PROJECT}/us-central1-a/seeded-b"])
        self.assertEqual(len(self.copies), 1)
        self.assertEqual(self.copies[0][1]["stamp"], latest["stamp"])

    def test_a_dry_run_registers_and_writes_nothing(self):
        code, out = self.run_main("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn(review.DRY_RUN_HEADER, out)
        self.assertIn("upgrade-drain-blocking-budget", out)
        self.assertEqual(self.registered, [])
        self.assertEqual(self.copies, [])
        self.assertFalse(review.weekly_home().exists())

    def test_a_failure_is_said_once_then_the_recovery(self):
        with mock.patch.object(watch, "run_report", side_effect=RuntimeError("sandbox exited 255 without a report")):
            code, out = self.run_main()
            self.assertEqual(code, 0)
            self.assertIn("failed: acme-prod: sandbox exited 255", out)
            code, out = self.run_main()
            self.assertEqual(out, "")
        code, out = self.run_main()
        self.assertEqual(out.strip(), review.RECOVERED_LINE.format(prefix=review.LOG_PREFIX))

    def test_a_failure_that_differs_only_in_its_numbers_is_the_same_failure(self):
        with mock.patch.object(watch, "run_report", side_effect=RuntimeError("the sandbox run timed out after 1187s")):
            code, out = self.run_main()
            self.assertIn("failed:", out)
        with mock.patch.object(watch, "run_report", side_effect=RuntimeError("the sandbox run timed out after 1203s")):
            code, out = self.run_main()
            self.assertEqual(out, "")
        self.assertEqual(review.failure_signature("a: after 1187s; b: after 12s"), "a: after #s; b: after #s")

    def test_findings_the_queue_refused_are_said_in_chat_not_on_stderr(self):
        with mock.patch.object(review, "kv_request", side_effect=OSError("HTTP 503 Service Unavailable: the findings queue is busy")):
            code, out = self.run_main()
        self.assertEqual(code, 0)
        # seeded-b's five rows and its claim failed; seeded-c's clean read sent a claim that failed.
        self.assertIn(f"{review.LOG_PREFIX}: 5 finding(s) and 2 completeness claim(s) did not reach the findings queue:", out)
        self.assertIn("503", out)
        latest = json.loads((review.weekly_home() / review.LATEST_JSON_NAME).read_text())
        self.assertEqual(len(latest["registration_errors"]), 2)
        self.assertTrue(all(r["outcome"] == review.REGISTRATION_FAILED for r in latest["findings"]))

    def test_a_record_that_cannot_be_written_still_says_what_the_queue_refused(self):
        with mock.patch.object(review, "kv_request", side_effect=OSError("HTTP 503 Service Unavailable: the findings queue is busy")), \
                mock.patch.object(review, "write_record", side_effect=OSError("No space left on device")):
            code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("could not write the record under", out)
        self.assertIn(f"{review.LOG_PREFIX}: 5 finding(s) and 2 completeness claim(s) did not reach the findings queue:", out)
        self.assertIn(review.REGISTRATION_NO_RECORD, out)
        self.assertNotIn(review.REGISTRATION_IN_REPORT, out)

    def test_the_registration_line_names_three_refusals_and_counts_the_rest(self):
        blocked = [member(f"c{i}", "blocked", pdbs={"blocking": [budget(name=f"b{i}")]}) for i in range(4)]
        self.envelope = {"exit": 0, "tables": "", "report": {"members": blocked, "errors": []}}
        with mock.patch.object(review, "kv_request", side_effect=OSError("HTTP 503 Service Unavailable: the findings queue is busy")):
            code, out = self.run_main()
        self.assertEqual(code, 0)
        # Each complete cluster's one batch carried its claim, so each failure lost a claim too.
        self.assertIn(f"{review.LOG_PREFIX}: 4 finding(s) and 4 completeness claim(s) did not reach the findings queue:", out)
        self.assertIn(review.OVERFLOW_NOTE.format(count=1), out)
        self.assertEqual(out.count("HTTP 503"), review.MAX_PROBLEMS_IN_LINE)

    def test_an_all_failed_line_names_six_projects_and_counts_the_rest(self):
        names = [f"proj-{i}" for i in range(8)]
        with mock.patch.object(watch, "projects", return_value=names), mock.patch.object(watch, "run_report", side_effect=RuntimeError("sandbox exited 255 without a report")):
            code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("failed:", out)
        self.assertEqual(out.count("sandbox exited 255"), review.MAX_NAMES_IN_LINE)
        self.assertIn(review.OVERFLOW_NOTE_PLAIN.format(count=2), out)
        self.assertNotIn("in the record", out)

    def test_a_project_not_read_is_one_line_and_the_rest_is_registered(self):
        calls = []

        def run_report(names, readiness, *, scope, outside_stands, timeout=None):
            calls.append(names[0])
            if names[0] == OTHER_PROJECT:
                raise watch.SandboxTimedOut(f"the sandbox run timed out after {int(timeout)}s")
            return self.envelope

        # `projects()` sorts, which would put acme-dev first on its own; the
        # names are handed unsorted so the second run's order is `ordered_names`'s.
        # The budget is short of two full runs, so the second project's run is
        # one the review's own budget cut short: the failure the order repairs.
        with mock.patch.object(watch, "projects", return_value=[PROJECT, OTHER_PROJECT]), mock.patch.object(review, "TOTAL_BUDGET_SECONDS", 900), mock.patch.object(watch, "run_report", side_effect=run_report):
            code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(calls, [PROJECT, OTHER_PROJECT])
        self.assertIn(f"1 project(s) not read this week: acme-dev: {review.CUT_SHORT_PREFIX}", out)
        self.assertEqual(len(self.registered), 2)
        latest = json.loads((review.weekly_home() / review.LATEST_JSON_NAME).read_text())
        self.assertTrue(latest["project_failures"][OTHER_PROJECT].startswith(review.CUT_SHORT_PREFIX))
        # Next run, the project the budget cut short goes first.
        with mock.patch.object(watch, "projects", return_value=[PROJECT, OTHER_PROJECT]), mock.patch.object(review, "TOTAL_BUDGET_SECONDS", 900), mock.patch.object(watch, "run_report", side_effect=run_report):
            calls.clear()
            self.run_main()
        self.assertEqual(calls, [OTHER_PROJECT, PROJECT])

    def test_a_project_whose_listing_failed_is_not_read(self):
        def run_report(names, readiness, *, scope, outside_stands, timeout=None):
            if names[0] == OTHER_PROJECT:
                return {"exit": 0, "tables": "", "report": {"members": [], "errors": [{"project": OTHER_PROJECT, "location": None, "message": "clusters list: PERMISSION_DENIED"}]}}
            return self.envelope

        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: f"{PROJECT},{OTHER_PROJECT}"}), mock.patch.object(watch, "run_report", side_effect=run_report):
            code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("1 project(s) not read this week: acme-dev: clusters list: PERMISSION_DENIED", out)
        latest = json.loads((review.weekly_home() / review.LATEST_JSON_NAME).read_text())
        self.assertEqual(latest["project_failures"], {OTHER_PROJECT: "clusters list: PERMISSION_DENIED"})
        self.assertEqual(len(self.registered), 2)  # the read project's two members, nothing for the unlisted one

    def test_a_project_spelled_by_number_the_report_could_not_resolve_is_not_read(self):
        # The report records a failed describe as a listing row with no location,
        # whatever else the envelope carries; that row is what makes the project
        # not read, so nothing is registered under the number.
        numbered = "123456789012"

        def run_report(names, readiness, *, scope, outside_stands, timeout=None):
            if names[0] == numbered:
                return {"exit": 0, "tables": "", "report": {"members": [member("ghost", "blocked", project=numbered, pdbs={"blocking": [budget()]})], "errors": [{"project": "declared-scope", "location": None, "message": "describe 123456789012: PERMISSION_DENIED"}]}}
            return self.envelope

        with mock.patch.object(watch, "projects", return_value=[PROJECT, numbered]), mock.patch.object(watch, "run_report", side_effect=run_report):
            code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn(f"1 project(s) not read this week: {numbered}: describe {numbered}: PERMISSION_DENIED", out)
        self.assertFalse(any(f["project"] == numbered for body in self.registered for f in body["findings"]))
        latest = json.loads((review.weekly_home() / review.LATEST_JSON_NAME).read_text())
        self.assertIn(numbered, latest["project_failures"])

    def test_a_project_spelled_by_number_and_by_id_is_registered_once(self):
        """No ledger maps the number yet, so its run happens; the report resolves it to the id
        already read, and that run is dropped: one set of rows, one table."""
        numbered = "123456789012"

        def run_report(names, readiness, *, scope, outside_stands, timeout=None):
            # The report resolves the number and enumerates under the id, as the id run does.
            return {"exit": 0, "tables": "| t |", "report": {"projects": [PROJECT], "members": [seeded_b(), member("seeded-c")], "errors": []}}

        with mock.patch.object(watch, "projects", return_value=[numbered, PROJECT]), mock.patch.object(watch, "run_report", side_effect=run_report):
            code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(len(self.registered), 2)  # seeded-b's batch and seeded-c's claim, once
        latest = json.loads((review.weekly_home() / review.LATEST_JSON_NAME).read_text())
        self.assertEqual(len(latest["clusters"]["reviewed"]), 2)
        self.assertEqual(len(latest["findings"]), len(review.member_findings(seeded_b())))
        self.assertEqual(latest["projects"], [PROJECT])  # the id the rows carry, not the number
        report = (review.weekly_home() / review.LATEST_MD_NAME).read_text()
        self.assertEqual(report.count("### acme-prod"), 1)
        self.assertNotIn(f"### {numbered}", report)

    def test_a_number_the_watchs_ledger_maps_to_an_id_already_read_is_not_run(self):
        """The daily watch learned that the number is the id's project: the review reads the id
        and runs nothing for the number, rather than a second full readiness run it drops."""
        numbered = "123456789012"
        self.learn_ids({numbered: PROJECT})
        calls = []

        def run_report(names, readiness, *, scope, outside_stands, timeout=None):
            calls.append(names[0])
            return {"exit": 0, "tables": "| t |", "report": {"projects": [PROJECT], "members": [seeded_b(), member("seeded-c")], "errors": []}}

        with mock.patch.object(watch, "projects", return_value=[numbered, PROJECT]), mock.patch.object(watch, "run_report", side_effect=run_report):
            code, out = self.run_main()
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(calls, [PROJECT])
        latest = json.loads((review.weekly_home() / review.LATEST_JSON_NAME).read_text())
        self.assertEqual(latest["projects"], [PROJECT])
        self.assertNotIn(numbered, latest.get("project_failures") or {})

    def test_a_number_that_runs_first_reads_the_project_and_the_id_is_not_run_again(self):
        """Last week's budget put the number ahead of the id: the number runs first, asked for
        by the id the watch learned, and the id spelling is the same project, already read."""
        numbered = "123456789012"
        self.learn_ids({numbered: PROJECT})
        calls = []

        def run_report(names, readiness, *, scope, outside_stands, timeout=None):
            calls.append(names[0])
            return {"exit": 0, "tables": "| t |", "report": {"projects": [PROJECT], "members": [seeded_b(), member("seeded-c")], "errors": []}}

        with mock.patch.object(review, "ordered_names", side_effect=lambda names, previous: [numbered, PROJECT]), mock.patch.object(watch, "projects", return_value=[numbered, PROJECT]), mock.patch.object(watch, "run_report", side_effect=run_report):
            code, out = self.run_main()
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(calls, [PROJECT])  # the number's run, by its learned id; the id spelling is not run again
        report = (review.weekly_home() / review.LATEST_MD_NAME).read_text()
        self.assertIn("### acme-prod", report)  # the id the rows carry heads the table
        self.assertNotIn(f"### {numbered}", report)

    def test_an_id_that_failed_beside_a_number_that_read_is_not_booked_not_read(self):
        numbered = "123456789012"
        calls = []

        def run_report(names, readiness, *, scope, outside_stands, timeout=None):
            calls.append(names[0])
            if names[0] == PROJECT:
                raise watch.SandboxTimedOut(f"the sandbox run timed out after {int(timeout)}s")
            return {"exit": 0, "tables": "| t |", "report": {"projects": [PROJECT], "members": [seeded_b(), member("seeded-c")], "errors": []}}

        with mock.patch.object(watch, "projects", return_value=[PROJECT, numbered]), mock.patch.object(watch, "run_report", side_effect=run_report):
            code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(calls, [PROJECT, numbered])
        latest = json.loads((review.weekly_home() / review.LATEST_JSON_NAME).read_text())
        self.assertNotIn(PROJECT, latest.get("project_failures") or {})
        self.assertNotIn("not read", out)
        report = (review.weekly_home() / review.LATEST_MD_NAME).read_text()
        self.assertIn("### acme-prod", report)
        self.assertNotIn(f"### {numbered}", report)

    def learn_ids(self, ids: dict[str, str]) -> None:
        """The ids the daily watch has learned, in its ledger, where the review reads them."""
        ledger = watch.empty_ledger()
        ledger[watch.PROJECT_IDS_KEY] = ids
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / watch.LEDGER_FILE_NAME).write_text(json.dumps(ledger), encoding="utf-8")

    def test_under_a_declared_scope_a_number_the_watch_learned_is_asked_for_by_its_id(self):
        """The scope lists ids, so the report reads the id with no describe; asked for by the
        number, a describe that fails on Saturday refuses the run and the project is not read."""
        numbered = "123456789012"
        self.learn_ids({numbered: PROJECT})
        calls = []

        def run_report(names, readiness, *, scope, outside_stands, timeout=None):
            calls.append(names[0])
            if names[0] == numbered:
                raise RuntimeError(f"refused by the report script: project number {numbered} did not resolve")
            return {"exit": 0, "tables": "| t |", "report": {"projects": [PROJECT], "members": [seeded_b(), member("seeded-c")], "errors": []}}

        with mock.patch.object(watch, "declared_scope", return_value=SCOPE), mock.patch.object(watch, "projects", return_value=[numbered]), mock.patch.object(watch, "run_report", side_effect=run_report):
            code, out = self.run_main()
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(calls, [PROJECT])
        latest = json.loads((review.weekly_home() / review.LATEST_JSON_NAME).read_text())
        self.assertEqual(latest["projects"], [PROJECT])
        self.assertEqual(len(latest["clusters"]["reviewed"]), 2)

    def test_on_an_undeclared_install_a_number_the_watch_learned_is_asked_for_by_its_id(self):
        """An id needs no describe: asked for by the number, a describe that fails on Saturday
        returns the number's clusters beside a listing row, and the project goes unread."""
        numbered = "123456789012"
        self.learn_ids({numbered: PROJECT})
        calls = []

        def run_report(names, readiness, *, scope, outside_stands, timeout=None):
            calls.append(names[0])
            if names[0] == numbered:
                return {"exit": 0, "tables": "", "report": {"members": [member("ghost", "blocked", project=numbered, pdbs={"blocking": [budget()]})], "errors": [{"project": numbered, "location": None, "message": f"describe {numbered}: PERMISSION_DENIED"}]}}
            return {"exit": 0, "tables": "| t |", "report": {"projects": [PROJECT], "members": [seeded_b(), member("seeded-c")], "errors": []}}

        with mock.patch.object(watch, "projects", return_value=[numbered]), mock.patch.object(watch, "run_report", side_effect=run_report):
            code, out = self.run_main()
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(calls, [PROJECT])
        latest = json.loads((review.weekly_home() / review.LATEST_JSON_NAME).read_text())
        self.assertEqual(latest["project_failures"], {})
        self.assertEqual(len(self.registered), 2)

    def test_a_project_that_failed_is_not_run_again_by_its_other_spelling(self):
        """Where the watch's ledger maps the number, declared scope or not: a second run would
        replace the first failure's cause, and a budget that cut the second short would move
        the project ahead of every other next week."""
        numbered = "123456789012"
        self.learn_ids({numbered: PROJECT})
        calls = []

        def run_report(names, readiness, *, scope, outside_stands, timeout=None):
            calls.append(names[0])
            raise RuntimeError("sandbox exited 137: OOMKilled" if names[0] == PROJECT else f"describe {names[0]}: PERMISSION_DENIED")

        with mock.patch.object(watch, "projects", return_value=[PROJECT, numbered]), mock.patch.object(watch, "run_report", side_effect=run_report):
            code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(calls, [PROJECT])
        self.assertIn("failed: acme-prod: sandbox exited 137: OOMKilled", out)
        self.assertNotIn(numbered, out)

    def test_a_project_that_failed_is_counted_once_beside_a_project_read(self):
        """The ledger maps the number to the id that failed, so the number is not run, and the
        partial line, the record's failures and its projects name the project once."""
        numbered = "123456789012"
        self.learn_ids({numbered: PROJECT})

        def run_report(names, readiness, *, scope, outside_stands, timeout=None):
            if names[0] in (PROJECT, numbered):
                raise RuntimeError("sandbox exited 255 without a report")
            return {"exit": 0, "tables": "| t |", "report": {"projects": [OTHER_PROJECT], "members": [member("seeded-d", project=OTHER_PROJECT)], "errors": []}}

        with mock.patch.object(watch, "projects", return_value=[PROJECT, numbered, OTHER_PROJECT]), mock.patch.object(watch, "run_report", side_effect=run_report):
            code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("1 project(s) not read this week: acme-prod: sandbox exited 255", out)
        latest = json.loads((review.weekly_home() / review.LATEST_JSON_NAME).read_text())
        self.assertEqual(latest["project_failures"], {PROJECT: "sandbox exited 255 without a report"})
        self.assertEqual(latest["projects"], [OTHER_PROJECT, PROJECT])

    def test_a_different_project_failing_the_same_way_is_announced_again(self):
        def refused(names, readiness, *, scope, outside_stands, timeout=None):
            raise RuntimeError(f"refused by the report script: project number {names[0]} did not resolve")

        outs = []
        for roster in (["123456789012"], ["123456789012"], ["987654321098"]):
            with mock.patch.object(watch, "projects", return_value=roster), mock.patch.object(watch, "run_report", side_effect=refused):
                outs.append(self.run_main()[1])
        self.assertIn("123456789012", outs[0])
        self.assertEqual(outs[1], "", "the same project failing the same way posts once")
        self.assertIn("987654321098", outs[2], "another project failing the same way is a new line")

    def test_a_numbered_project_the_report_resolved_whose_listing_failed_is_not_read(self):
        numbered = "123456789012"

        def run_report(names, readiness, *, scope, outside_stands, timeout=None):
            if names[0] == numbered:
                # The report resolved the number to its id and keyed the listing error by the id.
                return {"exit": 0, "tables": "", "report": {"projects": ["acme-dev"], "members": [], "errors": [{"project": "acme-dev", "location": None, "message": "clusters list: PERMISSION_DENIED"}]}}
            return self.envelope

        with mock.patch.object(watch, "projects", return_value=[PROJECT, numbered]), mock.patch.object(watch, "run_report", side_effect=run_report):
            code, out = self.run_main()
        self.assertEqual(code, 0)
        # Booked under the id the report resolved, as its table and its read errors are.
        self.assertIn("1 project(s) not read this week: acme-dev: clusters list: PERMISSION_DENIED", out)
        latest = json.loads((review.weekly_home() / review.LATEST_JSON_NAME).read_text())
        self.assertEqual(latest["project_failures"], {"acme-dev": "clusters list: PERMISSION_DENIED"})

    def test_a_clean_project_with_no_clusters_beside_a_failed_one_is_a_partial_run(self):
        def run_report(names, readiness, *, scope, outside_stands, timeout=None):
            if names[0] == OTHER_PROJECT:
                raise RuntimeError("sandbox exited 255 without a report")
            return {"exit": 0, "tables": "", "report": {"members": [], "errors": []}}

        with mock.patch.object(watch, "projects", return_value=[PROJECT, OTHER_PROJECT]), mock.patch.object(watch, "run_report", side_effect=run_report):
            code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("1 project(s) not read this week", out)
        self.assertNotIn("failed:", out)
        latest = json.loads((review.weekly_home() / review.LATEST_JSON_NAME).read_text())
        self.assertEqual(latest["project_failures"], {OTHER_PROJECT: "sandbox exited 255 without a report"})
        self.assertEqual(latest["clusters"]["reviewed"], [])

    def test_the_chat_lines_name_a_few_and_count_the_rest(self):
        self.assertEqual(review.shown(["a", "b"], 3), "a; b")
        self.assertEqual(review.shown(["a", "b", "c", "d", "e"], 3), "a; b; c; and 2 more in the record")

    def test_a_run_the_budget_cut_short_is_said_as_such(self):
        def run_report(names, readiness, *, scope, outside_stands, timeout=None):
            if names[0] == OTHER_PROJECT:
                raise watch.SandboxTimedOut(f"the sandbox run timed out after {int(timeout)}s")
            return self.envelope

        with mock.patch.object(watch, "projects", return_value=[PROJECT, OTHER_PROJECT]), mock.patch.object(review, "PROJECT_TIMEOUT_SECONDS", 1500), mock.patch.object(review, "TOTAL_BUDGET_SECONDS", 900), mock.patch.object(watch, "run_report", side_effect=run_report):
            code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("cut short by the review's time budget", out)
        self.assertNotIn("the sandbox run timed out", out)

    def test_two_rows_that_derive_one_id_are_one_finding(self):
        twice = with_rules()
        twice["readiness"]["risks"].append({"rule": "zonal-control-plane", "entry": 11, "text": "control plane is zonal (us-central1-a), said again"})
        found = review.rule_findings(twice)
        self.assertEqual(len(found), 4)  # the mapper itself keeps both zonal rows
        merged = [f for f in review.member_findings(twice) if f["check"] == "upgrade-zonal-control-plane"]
        self.assertEqual(len(merged), 1)
        self.assertIn("said again", merged[0]["detail"])
        self.assertIn(review.DUPLICATE_DETAIL_SEPARATOR.strip(), merged[0]["detail"])
        ids = [review.item_id(f) for f in review.member_findings(twice)]
        self.assertEqual(len(ids), len(set(ids)))

    def test_each_project_is_registered_before_the_next_one_runs(self):
        events = []

        def run_report(names, readiness, *, scope, outside_stands, timeout=None):
            events.append(("run", names[0]))
            return self.envelope

        def kv(path, body, timeout=None):
            events.append(("kv", body["scope"]["cluster"] if "scope" in body else body["findings"][0]["cluster"]))
            return {"results": [{"id": fq.derive_finding_id(f["check"], f["project"], f["cluster"], f["namespace"], f["object"]), "outcome": "created"} for f in body["findings"]], "downgraded": 0}

        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: f"{PROJECT},{OTHER_PROJECT}"}), mock.patch.object(watch, "run_report", side_effect=run_report), mock.patch.object(review, "kv_request", side_effect=kv):
            self.run_main()
        runs = [i for i, e in enumerate(events) if e[0] == "run"]
        self.assertEqual(len(runs), 2)
        self.assertTrue(any(e[0] == "kv" for e in events[runs[0] : runs[1]]), events)

    def test_no_project_is_a_failure_line(self):
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: ""}), mock.patch.object(watch, "projects", return_value=[]):
            code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn(review.NO_PROJECTS_DETAIL, out)


class TheRoster(unittest.TestCase):
    def test_the_two_saturday_jobs_are_on_the_roster_in_order(self):
        jobs = {job["id"]: job for job in json.loads(ROSTER.read_text())["jobs"]}
        review_job = jobs[review.JOB_ID]
        brief = jobs[review.BRIEF_JOB_ID]
        self.assertEqual(review_job["schedule"]["expr"], "0 6 * * 6")
        self.assertEqual(brief["schedule"]["expr"], "0 7 * * 6")
        self.assertTrue(review_job["no_agent"])
        self.assertEqual(review_job["script"], Path(review.__file__).name)
        self.assertEqual((review_job["deliver"], brief["deliver"]), ("chat", "chat"))
        self.assertIn("governance/upgrade_readiness_brief_sop.md", brief["prompt"])
        self.assertFalse(brief.get("no_agent"))
        # Both read tenant-written names: the review as the input-threat label a
        # script carries, the brief as the read-only command policy on the one
        # model turn over those names.
        self.assertEqual((review_job["risk"], brief["risk"]), ("high", "high"))

    def test_the_skill_carries_the_sop_shape_verbatim(self):
        # The on-request route lives in a skill the worker loads; its template
        # must be the SOP's, or a chat brief and a Saturday brief drift apart.
        skill = (Path(__file__).resolve().parents[1] / "skills" / "upgrade-readiness-brief" / "SKILL.md").read_text()
        def template(text):
            blocks, current = [], None
            for line in text.splitlines():
                if line.strip().startswith("```"):
                    if current is None:
                        current = []
                    else:
                        blocks.append(current)
                        current = None
                elif current is not None:
                    current.append(line.strip())
            shapes = [b for b in blocks if any("Decide now" in line for line in b)]
            self.assertEqual(len(shapes), 1, "one fenced template with Decide now")
            return "\n".join(line for line in shapes[0] if line)
        self.assertEqual(template(SOP.read_text()), template(skill))
        self.assertIn("upgrade-readiness-brief", SOP.read_text())

    def test_the_brief_sop_names_the_copy_the_review_places(self):
        text = SOP.read_text()
        self.assertIn(f"{review.SANDBOX_COPY_DIR}/{review.LATEST_JSON_NAME}", text)
        self.assertIn(f"{review.SANDBOX_COPY_DIR}/{review.LATEST_MD_NAME}", text)
        self.assertIn(f"`{review.CHECK_PREFIX}`", text)
        self.assertIn("get_ranked_findings", text)


if __name__ == "__main__":
    unittest.main()
