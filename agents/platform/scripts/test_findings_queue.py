import importlib.util
import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import findings_queue as fq


def _load_audit_report():
    """The fleet-audit CLI, imported by path so identity parity can be asserted.

    It lives in the skills tree rather than on this server's PYTHONPATH, which
    is why `findings_queue` transcribes the derivation instead of importing it.
    Returning None keeps the rest of the suite runnable where the module or its
    dependencies are absent; `test_id_matches_audit_report` fails loudly rather
    than skipping when the file is present but the two disagree.
    """
    path = (
        Path(__file__).resolve().parents[1]
        / "skills"
        / "fleet-audit"
        / "scripts"
        / "audit_report.py"
    )
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("_audit_report_for_parity", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    return module


AUDIT_REPORT = _load_audit_report()


def sample(**overrides) -> dict:
    finding = {
        "source": "inventory",
        "check": "probes-readiness",
        "cluster": "prod-eu",
        "namespace": "payments",
        "object": "Deployment/checkout",
        "title": "No readinessProbe on a 3-replica serving Deployment",
        "detail": "spec.template.spec.containers[0] has no readinessProbe",
        "rubric": {"B": 3, "L": 6, "detect": 3, "recover": 2, "C": 1.0},
        "recommendation": {
            "action": "Add a readinessProbe",
            "rationale": "Rollouts shift traffic to pods that are not serving",
            "risk": "A probe tuned too tight restarts healthy pods",
        },
        "remediation": {"kind": "manifest", "path": "apps/checkout/deployment.yaml", "note": "Add the probe"},
        "verification": {
            "kind": "kubectl",
            "command": "kubectl -n payments get deploy checkout -o json",
            "still_failing_when": "readinessProbe is absent from every container",
        },
    }
    finding.update(overrides)
    return finding


class TestIdentity(unittest.TestCase):
    def test_id_is_the_four_field_tuple(self):
        self.assertEqual(
            fq.derive_finding_id("probes-readiness", "prod-eu", "payments", "Deployment/checkout"),
            "probes-readiness.prod-eu.payments.deployment-checkout",
        )

    def test_cluster_scoped_uses_the_empty_sentinel(self):
        self.assertEqual(
            fq.derive_finding_id("public-control-plane", "prod-eu", "", "cluster"),
            "public-control-plane.prod-eu._.cluster",
        )

    def test_a_value_cannot_manufacture_a_segment_boundary(self):
        self.assertEqual(
            fq.derive_finding_id("x", "c", "n", "widgets.example.com").count("."),
            3,
        )

    def test_two_sources_naming_one_problem_derive_one_id(self):
        self.assertEqual(
            fq.derive_finding_id("workload-crashloop", "prod-eu", "payments", "Deployment/checkout"),
            fq.derive_finding_id("workload-crashloop", "prod-eu", "payments", "deployment/checkout"),
        )

    def test_long_ids_are_shortened_injectively(self):
        long_ns = "a" * 80
        first = fq.derive_finding_id("probes-readiness", "prod-eu", long_ns, "Deployment/frontend-api")
        second = fq.derive_finding_id("probes-readiness", "prod-eu", long_ns, "Deployment/frontend-web")
        self.assertLessEqual(len(first), fq.MAX_FINDING_ID)
        self.assertNotEqual(first, second)

    @unittest.skipIf(AUDIT_REPORT is None, "audit_report.py not importable here")
    def test_id_matches_audit_report(self):
        cases = [
            ("probes-readiness", "prod-eu", "payments", "Deployment/checkout"),
            ("public-control-plane", "prod-eu", "", "cluster"),
            ("x", "c", "n", "widgets.example.com"),
            ("workload-crashloop", "Prod_EU", "kube-system", "DaemonSet//fluentbit"),
            ("podsecurity-gaps", "c" * 60, "n" * 60, "o" * 60),
            ("probes-readiness", "prod-eu", "a" * 80, "Deployment/frontend-api"),
            ("probes-readiness", "prod-eu", "a" * 80, "Deployment/frontend-web"),
        ]
        for check, cluster, namespace, obj in cases:
            expected = AUDIT_REPORT._shorten_id(
                AUDIT_REPORT.derive_finding_id(
                    {"check": check, "cluster": cluster, "namespace": namespace, "object": obj}
                )
            )
            self.assertEqual(fq.derive_finding_id(check, cluster, namespace, obj), expected)


class TestRubric(unittest.TestCase):
    WORKED_EXAMPLES = [
        # docs/designs/inventory-findings-queue.md §4.3
        ({"B": 8, "L": 6, "detect": 3, "recover": 3, "C": 1.0}, 288, "critical"),
        ({"B": 5, "L": 10, "detect": 1, "recover": 2, "C": 1.0}, 150, "critical"),
        ({"B": 3, "L": 10, "detect": 1, "recover": 2, "C": 1.0}, 90, "critical"),
        ({"B": 3, "L": 6, "detect": 3, "recover": 2, "C": 1.0}, 90, "major"),
        ({"B": 8, "L": 2, "detect": 3, "recover": 3, "C": 0.9}, 86, "major"),
        ({"B": 3, "L": 6, "detect": 2, "recover": 2, "C": 1.0}, 72, "major"),
        ({"B": 2, "L": 10, "detect": 1, "recover": 2, "C": 1.0}, 60, "major"),
        ({"B": 1, "L": 1, "detect": 2, "recover": 1, "C": 1.0}, 3, "minor"),
    ]

    def test_worked_examples(self):
        for raw, expected_score, expected_severity in self.WORKED_EXAMPLES:
            rubric = fq.validate_rubric(raw)
            score = fq.rank_score(rubric)
            self.assertEqual(score, expected_score, raw)
            self.assertEqual(fq.severity_for(score, rubric), expected_severity, raw)

    def test_score_range(self):
        lowest = fq.validate_rubric({"B": 1, "L": 1, "detect": 1, "recover": 1, "C": 0.6})
        highest = fq.validate_rubric({"B": 8, "L": 10, "detect": 3, "recover": 3, "C": 1.0})
        self.assertEqual(fq.rank_score(lowest), 1)
        self.assertEqual(fq.rank_score(highest), 480)

    def test_floor_needs_both_measures(self):
        floored = fq.validate_rubric({"B": 3, "L": 10, "detect": 1, "recover": 1, "C": 1.0})
        thin_blast = fq.validate_rubric({"B": 1, "L": 10, "detect": 1, "recover": 1, "C": 1.0})
        rare = fq.validate_rubric({"B": 8, "L": 1, "detect": 1, "recover": 1, "C": 1.0})
        self.assertEqual(fq.severity_for(fq.rank_score(floored), floored), "critical")
        self.assertEqual(fq.severity_for(fq.rank_score(thin_blast), thin_blast), "minor")
        self.assertEqual(fq.severity_for(fq.rank_score(rare), rare), "minor")

    def test_off_anchor_values_are_refused(self):
        for bad in ({"B": 4}, {"L": 5}, {"detect": 0}, {"recover": 4}, {"C": 0.8}, {"B": True}):
            raw = {"B": 3, "L": 6, "detect": 2, "recover": 2, "C": 1.0, **bad}
            with self.assertRaises(fq.FindingError, msg=bad):
                fq.validate_rubric(raw)

    def test_rounding_is_half_up_and_not_a_float(self):
        # 5 x 0.9 = 4.5. Python's round() gives 4 here; the rubric gives 5.
        rubric = fq.validate_rubric({"B": 1, "L": 1, "detect": 2, "recover": 3, "C": 0.9})
        self.assertEqual(fq.rank_score(rubric), 5)


class TestValidation(unittest.TestCase):
    def test_a_complete_finding_normalises(self):
        finding = fq.validate_finding(sample())
        self.assertEqual(finding["rank_score"], 90)
        self.assertEqual(finding["severity"], "major")
        self.assertFalse(finding["provider_managed"])
        self.assertTrue(finding["actionable"])

    def test_derived_fields_may_not_be_supplied(self):
        for field in ("severity", "rank_score"):
            with self.assertRaises(fq.FindingError):
                fq.validate_finding(sample(**{field: "critical"}))

    def test_provider_managed_namespaces_are_flagged_whatever_the_source_says(self):
        for namespace in ("kube-system", "kube-public", "kube-node-lease", "gke-managed-cim", "gmp-system"):
            finding = fq.validate_finding(sample(namespace=namespace, provider_managed=False))
            self.assertTrue(finding["provider_managed"], namespace)

    def test_remediation_path_needs_manifest_kind(self):
        with self.assertRaises(fq.FindingError):
            fq.validate_finding(sample(remediation={"kind": "gcloud", "path": "a.yaml", "note": "n"}))

    def test_remediation_note_is_required(self):
        with self.assertRaises(fq.FindingError):
            fq.validate_finding(sample(remediation={"kind": "manual", "note": ""}))

    def test_manual_verification_needs_no_command(self):
        finding = fq.validate_finding(
            sample(verification={"kind": "manual", "command": "", "still_failing_when": "WI not adopted"})
        )
        self.assertEqual(finding["verification"]["command"], "")

    def test_commanded_verification_needs_a_command(self):
        with self.assertRaises(fq.FindingError):
            fq.validate_finding(
                sample(verification={"kind": "kubectl", "command": "", "still_failing_when": "x"})
            )

    def test_unusable_identity_is_refused(self):
        with self.assertRaises(fq.FindingError):
            fq.validate_finding(sample(check="!!!"))


class QueueTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        fq.init_findings_schema(self.conn)
        self.addCleanup(self.conn.close)

    def register(self, *findings, scope=None):
        return fq.register_findings(self.conn, [dict(f) for f in findings], scope)

    def ids(self):
        return [f["id"] for f in fq.ranked_findings(self.conn)]


class TestSchema(QueueTestCase):
    def test_generated_columns_track_the_rubric(self):
        self.register(sample())
        fid = fq.validate_finding(sample())["id"]
        row = self.conn.execute(
            "SELECT likelihood, blast_radius FROM findings WHERE id = ?", (fid,)
        ).fetchone()
        self.assertEqual(row, (6, 3))
        fq.record_verification(
            self.conn, fid, "still_failing", rubric={"B": 3, "L": 10, "detect": 3, "recover": 2, "C": 1.0}
        )
        row = self.conn.execute(
            "SELECT likelihood, blast_radius FROM findings WHERE id = ?", (fid,)
        ).fetchone()
        self.assertEqual(row, (10, 3))

    def test_the_designed_indexes_exist(self):
        names = {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'findings'"
            )
        }
        self.assertLessEqual(
            {"findings_ranked", "findings_urgent", "findings_object", "findings_pr"}, names
        )


class TestRegistration(QueueTestCase):
    def test_first_registration_creates(self):
        result = self.register(sample())
        self.assertEqual([r["outcome"] for r in result["results"]], ["created"])

    def test_re_registration_updates_one_row(self):
        self.register(sample())
        result = self.register(sample(detail="now two containers"))
        self.assertEqual([r["outcome"] for r in result["results"]], ["updated"])
        self.assertEqual(len(fq.ranked_findings(self.conn)), 1)

    def test_re_registration_leaves_the_users_decisions_alone(self):
        self.register(sample())
        fid = self.ids()[0]
        fq.mark_surfaced(self.conn, fid)
        fq.patch_finding(self.conn, fid, {"state": "snoozed", "snoozed_until": "2026-09-01"})
        before = fq.get_finding(self.conn, fid)

        self.register(sample(detail="seen again"))

        after = fq.get_finding(self.conn, fid)
        self.assertEqual(after["state"], "snoozed")
        self.assertEqual(after["snoozed_until"], before["snoozed_until"])
        self.assertEqual(after["surface_count"], before["surface_count"])
        self.assertEqual(after["detail"], "seen again")

    def test_dismissed_is_sticky(self):
        self.register(sample())
        fid = self.ids()[0]
        fq.patch_finding(self.conn, fid, {"state": "dismissed"})

        result = self.register(sample(detail="the sweep found it again"))

        self.assertEqual([r["outcome"] for r in result["results"]], ["suppressed"])
        row = fq.get_finding(self.conn, fid)
        self.assertEqual(row["state"], "dismissed")
        self.assertNotEqual(row["detail"], "the sweep found it again")
        self.assertEqual(self.ids(), [])

    def test_recurrence_requeues_and_rearms_the_alarm(self):
        self.register(sample())
        fid = self.ids()[0]
        fq.mark_surfaced(self.conn, fid)
        self.conn.execute("UPDATE findings SET alarmed_at = datetime('now') WHERE id = ?", (fid,))
        fq.record_verification(self.conn, fid, "resolved")
        first_seen = fq.get_finding(self.conn, fid)["first_seen"]

        self.register(sample())

        row = fq.get_finding(self.conn, fid)
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["first_seen"], first_seen)
        self.assertEqual(row["surface_count"], 0)
        self.assertIsNone(row["alarmed_at"])

    def test_a_complete_run_lowers_confidence_on_what_it_did_not_report(self):
        other = sample(object="Deployment/ledger", detail="also missing")
        self.register(sample(), other)
        absent_id = fq.validate_finding(other)["id"]

        result = self.register(sample(), scope={"cluster": "prod-eu", "complete": True})

        self.assertEqual(result["downgraded"], 1)
        row = fq.get_finding(self.conn, absent_id)
        self.assertEqual(row["rubric"]["C"], 0.6)
        self.assertEqual(row["rank_score"], 54)

    def test_a_partial_run_touches_nothing(self):
        other = sample(object="Deployment/ledger")
        self.register(sample(), other)
        absent_id = fq.validate_finding(other)["id"]

        result = self.register(sample())

        self.assertEqual(result["downgraded"], 0)
        self.assertEqual(fq.get_finding(self.conn, absent_id)["rubric"]["C"], 1.0)

    def test_a_complete_run_does_not_reach_another_cluster(self):
        elsewhere = sample(cluster="prod-us")
        self.register(sample(), elsewhere)
        elsewhere_id = fq.validate_finding(elsewhere)["id"]

        self.register(sample(), scope={"cluster": "prod-eu", "complete": True})

        self.assertEqual(fq.get_finding(self.conn, elsewhere_id)["rubric"]["C"], 1.0)

    def test_absence_does_not_reach_a_state_the_user_set(self):
        accepted = sample(object="Deployment/ledger")
        self.register(sample(), accepted)
        accepted_id = fq.validate_finding(accepted)["id"]
        fq.patch_finding(self.conn, accepted_id, {"state": "accepted"})

        self.register(sample(), scope={"cluster": "prod-eu", "complete": True})

        self.assertEqual(fq.get_finding(self.conn, accepted_id)["rubric"]["C"], 1.0)

    def test_the_scope_cluster_matches_whatever_case_it_arrives_in(self):
        # A miss here is silent: the sweep reports zero downgrades, which is
        # indistinguishable from a run that found everything still failing.
        other = sample(object="Deployment/ledger")
        self.register(sample(), other)

        result = self.register(sample(), scope={"cluster": "Prod-EU", "complete": True})

        self.assertEqual(result["downgraded"], 1)
        self.assertEqual(fq.get_finding(self.conn, fq.validate_finding(other)["id"])["rubric"]["C"], 0.6)

    def test_a_batch_is_bounded(self):
        with self.assertRaises(fq.FindingError):
            self.register(*[sample(object=f"Deployment/d{i}") for i in range(fq.MAX_BATCH + 1)])


class TestOrdering(QueueTestCase):
    def test_actionable_beats_score(self):
        low = sample(object="Deployment/low", rubric={"B": 1, "L": 1, "detect": 1, "recover": 1, "C": 1.0})
        high = sample(
            object="Deployment/high",
            actionable=False,
            rubric={"B": 8, "L": 10, "detect": 3, "recover": 3, "C": 1.0},
        )
        self.register(low, high)
        self.assertEqual(self.ids()[0], fq.validate_finding(low)["id"])

    def test_score_orders_the_actionable_rows(self):
        findings = [
            sample(object=f"Deployment/{name}", rubric=rubric)
            for name, rubric in (
                ("key", {"B": 8, "L": 6, "detect": 3, "recover": 3, "C": 1.0}),
                ("crash", {"B": 5, "L": 10, "detect": 1, "recover": 2, "C": 1.0}),
                ("prom", {"B": 1, "L": 1, "detect": 2, "recover": 1, "C": 1.0}),
            )
        ]
        self.register(*findings)
        self.assertEqual(
            [f["rank_score"] for f in fq.ranked_findings(self.conn)], [288, 150, 3]
        )

    def test_ties_break_deterministically_and_do_not_group(self):
        tied = [
            sample(object=obj, rubric={"B": 3, "L": 6, "detect": 3, "recover": 2, "C": 1.0})
            for obj in ("Deployment/b", "Deployment/a", "Deployment/c")
        ]
        self.register(*tied)
        first = self.ids()
        self.register(*reversed(tied))
        self.assertEqual(first, self.ids())
        self.assertEqual(
            [f["object"] for f in fq.ranked_findings(self.conn)],
            ["Deployment/a", "Deployment/b", "Deployment/c"],
        )

    def test_ranked_returns_the_open_states_only(self):
        # §3.2's table, transcribed rather than read back from fq.OPEN_STATES:
        # an expectation derived from the constant under test passes for any
        # value of that constant.
        states = {
            "queued": (None, True),
            "surfaced": ({"state": "surfaced"}, True),
            "accepted": ({"state": "accepted"}, True),
            "snoozed": ({"state": "snoozed", "snoozed_until": "2026-09-01"}, False),
            "dismissed": ({"state": "dismissed"}, False),
        }
        expected = []
        for name, (patch, is_open) in states.items():
            finding = sample(object=f"Deployment/{name}")
            self.register(finding)
            fid = fq.validate_finding(finding)["id"]
            if patch:
                fq.patch_finding(self.conn, fid, patch)
            if is_open:
                expected.append(fid)
        self.assertEqual(sorted(self.ids()), sorted(expected))

    def test_a_manifest_fix_breaks_a_tie_at_an_equal_score(self):
        # §4.5: fix cost enters the order exactly once, as a tie-break.
        # Named so the object tie-break that follows would order them the other
        # way round; without §4.5 applied first, 'aaa-manual' comes out on top.
        manual = sample(
            object="Deployment/aaa-manual",
            remediation={"kind": "manual", "note": "Ask the platform team"},
        )
        manifest = sample(
            object="Deployment/zzz-manifest",
            remediation={"kind": "manifest", "path": "apps/a.yaml", "note": "Add the probe"},
        )
        self.register(manual, manifest)
        ranked = fq.ranked_findings(self.conn)
        self.assertEqual([f["rank_score"] for f in ranked], [90, 90])
        self.assertEqual(ranked[0]["object"], "Deployment/zzz-manifest")

    def test_the_manifest_tie_break_never_outranks_the_score(self):
        manual = sample(
            object="Deployment/manual",
            rubric={"B": 8, "L": 10, "detect": 3, "recover": 3, "C": 1.0},
            remediation={"kind": "manual", "note": "Ask the platform team"},
        )
        manifest = sample(
            object="Deployment/manifest",
            rubric={"B": 1, "L": 1, "detect": 1, "recover": 1, "C": 1.0},
        )
        self.register(manual, manifest)
        self.assertEqual(fq.ranked_findings(self.conn)[0]["object"], "Deployment/manual")

    def test_an_expired_snooze_rejoins_the_list(self):
        self.register(sample())
        fid = self.ids()[0]
        fq.patch_finding(self.conn, fid, {"state": "snoozed", "snoozed_until": "2026-09-01"})
        self.assertEqual(self.ids(), [])

        row = fq.patch_finding(self.conn, fid, {"state": "surfaced"})

        self.assertEqual(self.ids(), [fid])
        self.assertIsNone(row["snoozed_until"])


class TestTransitions(QueueTestCase):
    def setUp(self):
        super().setUp()
        self.register(sample())
        self.fid = self.ids()[0]

    def test_surfacing_counts_and_routes(self):
        row = fq.mark_surfaced(self.conn, self.fid, "spaces/AAA", "spaces/AAA/threads/BBB")
        self.assertEqual(row["state"], "surfaced")
        self.assertEqual(row["surface_count"], 1)
        self.assertEqual(row["chat_id"], "spaces/AAA")
        row = fq.mark_surfaced(self.conn, self.fid)
        self.assertEqual(row["surface_count"], 2)
        self.assertEqual(row["chat_id"], "spaces/AAA")

    def test_surfacing_does_not_override_a_users_state(self):
        fq.patch_finding(self.conn, self.fid, {"state": "accepted"})
        self.assertEqual(fq.mark_surfaced(self.conn, self.fid)["state"], "accepted")

    def test_snooze_needs_a_date(self):
        with self.assertRaises(fq.FindingError):
            fq.patch_finding(self.conn, self.fid, {"state": "snoozed"})

    def test_patch_refuses_the_verification_outcomes(self):
        for state in ("resolved", "stale", "queued"):
            with self.assertRaises(fq.FindingError):
                fq.patch_finding(self.conn, self.fid, {"state": state})

    def test_pr_fields_are_stored_opaquely(self):
        row = fq.patch_finding(
            self.conn,
            self.fid,
            {"pr_url": "https://example.invalid/x/y/pull/7", "pr_state": "open"},
        )
        self.assertEqual(row["pr_url"], "https://example.invalid/x/y/pull/7")
        self.assertEqual(row["pr_state"], "open")
        with self.assertRaises(fq.FindingError):
            fq.patch_finding(self.conn, self.fid, {"pr_state": "draft"})

    def test_patch_on_an_unknown_finding_raises(self):
        with self.assertRaises(fq.FindingNotFound):
            fq.patch_finding(self.conn, "no-such-finding", {"state": "accepted"})

    def test_leaving_a_snooze_by_any_door_clears_its_deadline(self):
        for state in ("accepted", "dismissed", "surfaced"):
            fq.patch_finding(self.conn, self.fid, {"state": "snoozed", "snoozed_until": "2026-09-01"})
            self.assertIsNotNone(fq.get_finding(self.conn, self.fid)["snoozed_until"])
            row = fq.patch_finding(self.conn, self.fid, {"state": state})
            self.assertIsNone(row["snoozed_until"], f"{state} left a wake-up time behind")

    def test_a_snooze_deadline_must_be_a_real_timestamp(self):
        for bad in ("when hell freezes over", "2026-13-01", "next tuesday"):
            with self.assertRaises(fq.FindingError):
                fq.patch_finding(self.conn, self.fid, {"state": "snoozed", "snoozed_until": bad})

    def test_a_snooze_deadline_is_stored_as_sqlite_compares_it(self):
        # `snoozed_until <= datetime('now')` is a string comparison, so the
        # stored form has to be UTC 'YYYY-MM-DD HH:MM:SS' whatever was sent.
        for sent, stored in (
            ("2026-09-01", "2026-09-01 00:00:00"),
            ("2026-09-01T12:30:00Z", "2026-09-01 12:30:00"),
            ("2026-09-01T12:30:00+02:00", "2026-09-01 10:30:00"),
        ):
            row = fq.patch_finding(self.conn, self.fid, {"state": "snoozed", "snoozed_until": sent})
            self.assertEqual(row["snoozed_until"], stored)

    def test_a_provider_managed_finding_takes_no_pull_request(self):
        # §4.4: nothing in kube-system has a manifest the operator owns.
        self.register(sample(namespace="kube-system", object="DaemonSet/fluentbit"))
        fid = fq.validate_finding(sample(namespace="kube-system", object="DaemonSet/fluentbit"))["id"]
        self.assertTrue(fq.get_finding(self.conn, fid)["provider_managed"])
        for patch in ({"pr_url": "https://example.invalid/pull/7"}, {"pr_state": "open"}):
            with self.assertRaises(fq.FindingError):
                fq.patch_finding(self.conn, fid, patch)

    def test_a_pull_request_link_can_be_cleared(self):
        fq.patch_finding(self.conn, self.fid, {"pr_url": "https://example.invalid/pull/7", "pr_state": "open"})
        row = fq.patch_finding(self.conn, self.fid, {"pr_url": "", "pr_state": ""})
        self.assertIsNone(row["pr_url"])
        self.assertIsNone(row["pr_state"])

    def test_a_rejected_value_is_not_echoed_back_whole(self):
        payload = "A" * 20000
        with self.assertRaises(fq.FindingError) as caught:
            fq.patch_finding(self.conn, self.fid, {"pr_state": payload})
        self.assertLess(len(str(caught.exception)), 200)
        self.assertNotIn(payload, str(caught.exception))


class TestVerification(QueueTestCase):
    def setUp(self):
        super().setUp()
        self.register(sample())
        self.fid = self.ids()[0]
        self.conn.execute("UPDATE findings SET last_verified = '2020-01-01 00:00:00' WHERE id = ?", (self.fid,))

    def test_still_failing_advances_freshness(self):
        row = fq.record_verification(self.conn, self.fid, "still_failing", "0/3 ready")
        self.assertNotEqual(row["last_verified"], "2020-01-01 00:00:00")
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["last_verification"]["observed"], "0/3 ready")

    def test_resolved_leaves_the_queue(self):
        row = fq.record_verification(self.conn, self.fid, "resolved", "probe present")
        self.assertEqual(row["state"], "resolved")
        self.assertEqual(self.ids(), [])

    def test_unverifiable_is_not_resolved_and_does_not_advance_freshness(self):
        row = fq.record_verification(self.conn, self.fid, "unverifiable", "Unauthorized")
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["last_verified"], "2020-01-01 00:00:00")
        self.assertEqual(self.ids(), [self.fid])

    def test_a_missing_object_is_stale_not_resolved(self):
        row = fq.record_verification(self.conn, self.fid, "unverifiable", "NotFound", object_missing=True)
        self.assertEqual(row["state"], "stale")
        self.assertEqual(row["last_verified"], "2020-01-01 00:00:00")

    def test_an_unknown_outcome_is_refused(self):
        with self.assertRaises(fq.FindingError):
            fq.record_verification(self.conn, self.fid, "probably_fine")

    def test_a_gap_that_starts_firing_rescores(self):
        row = fq.record_verification(
            self.conn,
            self.fid,
            "still_failing",
            rubric={"B": 3, "L": 10, "detect": 3, "recover": 2, "C": 1.0},
        )
        self.assertEqual(row["rank_score"], 150)
        self.assertEqual(row["severity"], "critical")

    def test_verification_cannot_resurrect_a_dismissed_finding(self):
        # The direct revival is blocked by §5.2's sticky rule, but 'resolved'
        # and 'stale' are both recurrence states: writing either here would
        # arm the *next* sweep to re-queue the row the user took off the list.
        fq.patch_finding(self.conn, self.fid, {"state": "dismissed"})

        for outcome, kwargs in (
            ("resolved", {}),
            ("unverifiable", {"object_missing": True}),
            ("still_failing", {}),
        ):
            row = fq.record_verification(self.conn, self.fid, outcome, "observed", **kwargs)
            self.assertEqual(row["state"], "dismissed", f"{outcome} moved a dismissed row")

        # Re-registering after the round trip still finds it dismissed.
        self.assertEqual(self.register(sample())["results"][0]["outcome"], "suppressed")
        self.assertEqual(fq.get_finding(self.conn, self.fid)["state"], "dismissed")
        self.assertEqual(self.ids(), [])

    def test_a_dismissed_finding_still_records_that_it_was_checked(self):
        fq.patch_finding(self.conn, self.fid, {"state": "dismissed"})
        row = fq.record_verification(self.conn, self.fid, "still_failing", "0/3 ready")
        self.assertNotEqual(row["last_verified"], "2020-01-01 00:00:00")
        self.assertEqual(row["last_verification"]["observed"], "0/3 ready")

    def test_a_fault_that_stops_firing_clears_the_alarm(self):
        fq.record_verification(
            self.conn, self.fid, "still_failing", rubric={"B": 3, "L": 10, "detect": 3, "recover": 2, "C": 1.0}
        )
        self.conn.execute("UPDATE findings SET alarmed_at = datetime('now') WHERE id = ?", (self.fid,))

        row = fq.record_verification(
            self.conn, self.fid, "still_failing", rubric={"B": 3, "L": 6, "detect": 3, "recover": 2, "C": 1.0}
        )

        self.assertIsNone(row["alarmed_at"])
        self.assertEqual(row["state"], "queued")

    def test_a_rescore_that_does_not_lower_l_leaves_the_alarm_alone(self):
        fq.record_verification(
            self.conn, self.fid, "still_failing", rubric={"B": 3, "L": 10, "detect": 3, "recover": 2, "C": 1.0}
        )
        self.conn.execute("UPDATE findings SET alarmed_at = '2026-08-01 09:00:00' WHERE id = ?", (self.fid,))

        row = fq.record_verification(
            self.conn, self.fid, "still_failing", rubric={"B": 5, "L": 10, "detect": 3, "recover": 2, "C": 1.0}
        )

        self.assertEqual(row["alarmed_at"], "2026-08-01 09:00:00")


class TestListing(QueueTestCase):
    def setUp(self):
        super().setUp()
        self.register(
            sample(),
            sample(cluster="prod-us", object="Deployment/ledger"),
            sample(object="Deployment/db", rubric={"B": 8, "L": 6, "detect": 3, "recover": 3, "C": 1.0}),
        )

    def test_filters(self):
        self.assertEqual(len(fq.list_findings(self.conn, cluster="prod-eu")), 2)
        self.assertEqual(len(fq.list_findings(self.conn, severity="critical")), 1)
        self.assertEqual(len(fq.list_findings(self.conn, state="queued")), 3)
        self.assertEqual(len(fq.list_findings(self.conn, cluster="prod-eu", severity="major")), 1)

    def test_bad_filters_are_refused(self):
        with self.assertRaises(fq.FindingError):
            fq.list_findings(self.conn, state="pending")
        with self.assertRaises(fq.FindingError):
            fq.list_findings(self.conn, severity="Warning")

    def test_the_limit_takes_the_worst(self):
        self.assertEqual(fq.list_findings(self.conn, limit=1)[0]["rank_score"], 288)


class TestPublications(QueueTestCase):
    def test_round_trip(self):
        self.assertIsNone(fq.get_publication(self.conn, "backlog"))
        fq.put_publication(
            self.conn,
            "backlog",
            {"target_kind": "github-issue", "target_ref": "https://example.invalid/i/1", "content_hash": "abc"},
        )
        row = fq.get_publication(self.conn, "backlog")
        self.assertEqual(row["target_ref"], "https://example.invalid/i/1")
        self.assertIsNotNone(row["last_published"])

        fq.put_publication(
            self.conn,
            "backlog",
            {"target_kind": "github-issue", "target_ref": "https://example.invalid/i/1", "content_hash": "def"},
        )
        self.assertEqual(fq.get_publication(self.conn, "backlog")["content_hash"], "def")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM queue_publications").fetchone()[0], 1
        )

    def test_an_omitted_field_is_left_alone_not_nulled(self):
        # The backlog publisher's whole job is remembering the document it
        # rewrites; a hash-only update must not lose it.
        fq.put_publication(
            self.conn,
            "backlog",
            {"target_kind": "github-issue", "target_ref": "https://example.invalid/i/1", "content_hash": "abc"},
        )
        row = fq.put_publication(self.conn, "backlog", {"target_kind": "github-issue", "content_hash": "def"})
        self.assertEqual(row["target_ref"], "https://example.invalid/i/1")
        self.assertEqual(row["content_hash"], "def")

        self.assertIsNone(
            fq.put_publication(
                self.conn, "backlog", {"target_kind": "github-issue", "target_ref": ""}
            )["target_ref"]
        )

    def test_unknown_publishers_and_targets_are_refused(self):
        with self.assertRaises(fq.FindingError):
            fq.put_publication(self.conn, "email", {"target_kind": "chat"})
        with self.assertRaises(fq.FindingError):
            fq.put_publication(self.conn, "nudge", {"target_kind": "pigeon"})


if __name__ == "__main__":
    unittest.main()
