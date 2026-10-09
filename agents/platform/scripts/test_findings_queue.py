import importlib.util
import io
import json
import sqlite3
import sys
import unittest
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import findings_queue as fq
import inventory_findings as inv


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
        "project": "acme-prod",
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
    def test_id_is_the_five_field_tuple(self):
        self.assertEqual(
            fq.derive_finding_id("probes-readiness", "acme-prod", "prod-eu", "payments", "Deployment/checkout"),
            "probes-readiness.acme-prod.prod-eu.payments.deployment-checkout",
        )

    def test_cluster_scoped_uses_the_empty_sentinel(self):
        self.assertEqual(
            fq.derive_finding_id("public-control-plane", "acme-prod", "prod-eu", "", "cluster"),
            "public-control-plane.acme-prod.prod-eu._.cluster",
        )

    def test_a_value_cannot_manufacture_a_segment_boundary(self):
        self.assertEqual(
            fq.derive_finding_id("x", "p", "c", "n", "widgets.example.com").count("."),
            4,
        )

    def test_two_sources_naming_one_problem_derive_one_id(self):
        self.assertEqual(
            fq.derive_finding_id("workload-crashloop", "acme-prod", "prod-eu", "payments", "Deployment/checkout"),
            fq.derive_finding_id("workload-crashloop", "acme-prod", "prod-eu", "payments", "deployment/checkout"),
        )

    def test_the_same_cluster_name_in_two_projects_is_two_identities(self):
        # The collision the project segment exists to prevent: without it the
        # second `prod` overwrites the first's row while keeping its state.
        self.assertNotEqual(
            fq.derive_finding_id("workload-identity-off", "acme-prod", "prod", "", "prod"),
            fq.derive_finding_id("workload-identity-off", "acme-staging", "prod", "", "prod"),
        )

    def test_long_ids_are_shortened_injectively(self):
        long_ns = "a" * 80
        first = fq.derive_finding_id("probes-readiness", "acme-prod", "prod-eu", long_ns, "Deployment/frontend-api")
        second = fq.derive_finding_id("probes-readiness", "acme-prod", "prod-eu", long_ns, "Deployment/frontend-web")
        self.assertLessEqual(len(first), fq.MAX_FINDING_ID)
        self.assertNotEqual(first, second)

    @unittest.skipIf(AUDIT_REPORT is None, "audit_report.py not importable here")
    def test_id_matches_audit_report_with_the_project_segment_spliced_in(self):
        # The queue's derivation is the audit's plus a project segment after
        # the check, so segment-level parity is the contract: for ids short
        # enough that neither side shortens, splicing `_id_segment(project)`
        # into the audit's id reproduces the queue's exactly. The shortening
        # budgets differ once a fifth segment exists, so the long-id cases are
        # covered by the queue-local tests above instead.
        cases = [
            ("probes-readiness", "acme-prod", "prod-eu", "payments", "Deployment/checkout"),
            ("public-control-plane", "acme-prod", "prod-eu", "", "cluster"),
            ("x", "p", "c", "n", "widgets.example.com"),
            ("workload-crashloop", "Acme_Prod", "Prod_EU", "kube-system", "DaemonSet//fluentbit"),
        ]
        for check, project, cluster, namespace, obj in cases:
            audit = AUDIT_REPORT._shorten_id(
                AUDIT_REPORT.derive_finding_id(
                    {"check": check, "cluster": cluster, "namespace": namespace, "object": obj}
                )
            )
            head, tail = audit.split(".", 1)
            expected = f"{head}.{fq._id_segment(project)}.{tail}"
            self.assertLessEqual(len(expected), fq.MAX_FINDING_ID, "case long enough to shorten; move it")
            self.assertEqual(fq.derive_finding_id(check, project, cluster, namespace, obj), expected)


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
    def test_a_pre_release_table_without_project_is_rebuilt(self):
        # A dev-install DB created before the key change would otherwise fail
        # every INSERT with `no such column: project`, surfaced as a 503 that
        # reads as retryable. No released writer ever filled the table, so the
        # old shape carries nothing worth migrating.
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE findings (id TEXT PRIMARY KEY, cluster TEXT NOT NULL)")
        conn.execute("INSERT INTO findings VALUES ('stale-row', 'prod')")

        fq.init_findings_schema(conn)
        result = fq.register_findings(conn, [sample()])

        self.assertEqual(result["results"][0]["outcome"], "created")
        ids = [row[0] for row in conn.execute("SELECT id FROM findings")]
        self.assertNotIn("stale-row", ids)

    def test_a_current_table_survives_reinit_with_its_rows(self):
        self.register(sample())
        fid = self.ids()[0]
        fq.init_findings_schema(self.conn)
        self.assertEqual(self.ids(), [fid])


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

    def test_the_same_cluster_name_in_two_projects_is_two_rows(self):
        # The key collision this column exists to prevent: without project in
        # the identity, the staging registration lands as an *update* of the
        # prod row — its title and detail overwrite prod's, and because the
        # sticky-state handling keeps the existing state, an acknowledged prod
        # finding silently re-points at staging's object while still reading
        # as acknowledged.
        prod = sample(project="acme-prod", cluster="prod", object="prod", namespace="", title="WI off in acme-prod")
        staging = sample(project="acme-staging", cluster="prod", object="prod", namespace="", title="WI off in acme-staging")
        self.register(prod)
        fq.patch_finding(self.conn, fq.validate_finding(prod)["id"], {"state": "accepted"})

        result = self.register(staging)

        self.assertEqual(result["results"][0]["outcome"], "created")
        rows = fq.list_findings(self.conn, cluster="prod")
        self.assertEqual(len(rows), 2)
        by_project = {row["project"]: row for row in rows}
        self.assertEqual(by_project["acme-prod"]["title"], "WI off in acme-prod")
        self.assertEqual(by_project["acme-prod"]["state"], "accepted")
        self.assertEqual(by_project["acme-staging"]["state"], "queued")
        self.assertEqual(fq.list_findings(self.conn, cluster="prod", project="acme-staging"), [by_project["acme-staging"]])

    def test_a_finding_without_a_project_is_refused(self):
        payload = sample()
        del payload["project"]
        with self.assertRaisesRegex(fq.FindingError, "project is required"):
            self.register(payload)

    def test_a_complete_scope_without_a_project_is_refused(self):
        # The absence rule has the same ambiguity as the key: "complete for
        # cluster prod" must say which prod, or it downgrades another
        # project's rows.
        with self.assertRaisesRegex(fq.FindingError, "scope.project is required"):
            self.register(sample(), scope={"cluster": "prod-eu", "complete": True})

    def test_a_complete_run_does_not_reach_another_projects_same_named_cluster(self):
        other_project = sample(project="acme-staging", title="the staging copy")
        self.register(sample(), other_project)

        result = self.register(
            sample(), scope={"project": "acme-prod", "cluster": "prod-eu", "complete": True}
        )

        self.assertEqual(result["downgraded"], 0)
        row = fq.get_finding(self.conn, fq.validate_finding(other_project)["id"])
        self.assertEqual(row["rubric"]["C"], 1.0)

    def test_a_complete_run_lowers_confidence_on_what_it_did_not_report(self):
        other = sample(object="Deployment/ledger", detail="also missing")
        self.register(sample(), other)
        absent_id = fq.validate_finding(other)["id"]

        result = self.register(sample(), scope={"project": "acme-prod", "cluster": "prod-eu", "complete": True})

        self.assertEqual(result["downgraded"], 1)
        row = fq.get_finding(self.conn, absent_id)
        self.assertEqual(row["rubric"]["C"], 0.6)
        self.assertEqual(row["rank_score"], 54)

    def test_a_surfaced_finding_is_downgraded_by_absence_like_any_other(self):
        # The nudge marks what it names surfaced. Exempting that state would
        # make the rows the nudge repeats daily the only ones absence never
        # reaches, so a fixed one is nagged about forever.
        other = sample(object="Deployment/ledger", detail="also missing")
        self.register(sample(), other)
        absent_id = fq.validate_finding(other)["id"]
        fq.mark_surfaced(self.conn, absent_id)

        result = self.register(sample(), scope={"project": "acme-prod", "cluster": "prod-eu", "complete": True})

        self.assertEqual(result["downgraded"], 1)
        self.assertEqual(fq.get_finding(self.conn, absent_id)["rubric"]["C"], 0.6)

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

        self.register(sample(), scope={"project": "acme-prod", "cluster": "prod-eu", "complete": True})

        self.assertEqual(fq.get_finding(self.conn, elsewhere_id)["rubric"]["C"], 1.0)

    def test_absence_does_not_reach_a_state_the_user_set(self):
        accepted = sample(object="Deployment/ledger")
        self.register(sample(), accepted)
        accepted_id = fq.validate_finding(accepted)["id"]
        fq.patch_finding(self.conn, accepted_id, {"state": "accepted"})

        self.register(sample(), scope={"project": "acme-prod", "cluster": "prod-eu", "complete": True})

        self.assertEqual(fq.get_finding(self.conn, accepted_id)["rubric"]["C"], 1.0)

    def test_the_scope_cluster_matches_whatever_case_it_arrives_in(self):
        # A miss here is silent: the sweep reports zero downgrades, which is
        # indistinguishable from a run that found everything still failing.
        other = sample(object="Deployment/ledger")
        self.register(sample(), other)

        result = self.register(sample(), scope={"project": "Acme-Prod", "cluster": "Prod-EU", "complete": True})

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

    def test_a_person_can_reverse_their_own_dismissal(self):
        # Deliberate, not a hole in the sticky rule. Registration and
        # verification are both blocked from reviving a dismissed row, so
        # without this door a mis-click is unrecoverable by any route.
        fq.patch_finding(self.conn, self.fid, {"state": "dismissed"})
        self.assertEqual(self.ids(), [])

        row = fq.patch_finding(self.conn, self.fid, {"state": "accepted"})

        self.assertEqual(row["state"], "accepted")
        self.assertEqual(self.ids(), [self.fid])

    def test_leaving_a_snooze_by_any_door_clears_its_deadline(self):
        for state in ("accepted", "dismissed", "surfaced"):
            fq.patch_finding(self.conn, self.fid, {"state": "snoozed", "snoozed_until": "2026-09-01"})
            self.assertIsNotNone(fq.get_finding(self.conn, self.fid)["snoozed_until"])
            row = fq.patch_finding(self.conn, self.fid, {"state": state})
            self.assertIsNone(row["snoozed_until"], f"{state} left a wake-up time behind")

    def test_expiry_returns_a_lapsed_snooze_to_the_list(self):
        fq.patch_finding(self.conn, self.fid, {"state": "snoozed", "snoozed_until": "2000-01-01"})
        self.assertEqual(self.ids(), [])

        self.assertEqual(fq.expire_snoozes(self.conn), 1)

        row = fq.get_finding(self.conn, self.fid)
        self.assertEqual(row["state"], "surfaced")
        self.assertIsNone(row["snoozed_until"])
        self.assertEqual(self.ids(), [self.fid])

    def test_expiry_leaves_an_unlapsed_snooze_alone(self):
        fq.patch_finding(self.conn, self.fid, {"state": "snoozed", "snoozed_until": "2999-01-01"})

        self.assertEqual(fq.expire_snoozes(self.conn), 0)

        row = fq.get_finding(self.conn, self.fid)
        self.assertEqual(row["state"], "snoozed")
        self.assertIsNotNone(row["snoozed_until"])
        self.assertEqual(self.ids(), [])

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


PRIORITIZE_SOP = (
    Path(__file__).resolve().parents[1] / "governance" / "inventory_prioritize_sop.md"
)


def _table_after(text: str, marker: str) -> list[list[str]]:
    """Cells of the first Markdown table following `marker`, separator dropped."""
    body = text.split(marker, 1)[1]
    rows = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            if rows:
                break
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(set(cell) <= set("-: ") for cell in cells):
            continue
        rows.append(cells)
    return rows


UTC = timezone.utc
CRITICAL_RUBRIC = {"B": 8, "L": 6, "detect": 3, "recover": 2, "C": 1.0}  # 240


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """A moment in October 2026, UTC."""
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def stamp(moment: datetime) -> str:
    """As SQLite's datetime('now') writes it: naive UTC."""
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def row(rid, severity="critical", check=None, state="queued", shown=None, C=1.0, score=None, absent=None, **extra) -> dict:
    """A ranked row, as `/ranked` returns it, without a database."""
    out = {
        "id": rid,
        "check_slug": check or rid,
        "project": "acme",
        "cluster": "prod",
        "namespace": "payments",
        "object": rid,
        "title": f"title {rid}",
        "severity": severity,
        "rank_score": score if score is not None else {"critical": 240, "major": 90, "minor": 2}[severity],
        "rubric": {"C": C},
        "state": state,
        "first_shown_at": stamp(shown) if shown else None,
        "absent_since": stamp(absent) if absent else None,
        "provider_managed": False,
        "actionable": True,
    }
    out.update(extra)
    return out


NONE_ADDED = {"critical": 0, "noncritical": 0}


def ids(items) -> list[list[str]]:
    return [[member["id"] for member in item.members] for item in items]


class TestPacingSchema(unittest.TestCase):
    def _released_table(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        released = (
            fq.FINDINGS_SCHEMA.replace("    first_shown_at    TIMESTAMP,\n", "")
            .replace("    added_class       TEXT,\n", "")
            .replace("    absent_since      TIMESTAMP,\n", "")
        )
        self.assertNotIn("first_shown_at", released)
        self.assertNotIn("absent_since", released)
        conn.execute(released)
        columns = "id, source, check_slug, project, cluster, object, title, severity, rank_score, rubric, recommendation, remediation, verification, state, surfaced_at, surface_count"
        # The old nudge named the top two criticals each morning. A model
        # answering a pull marked "pulled-critical" through the same route.
        for rid, severity, score, state, surfaced_at, count in (
            ("named-critical", "critical", 250, "surfaced", "2026-10-05 12:00:01", 3),
            ("named-critical-2", "critical", 240, "surfaced", "2026-10-05 12:00:01", 3),
            ("pulled-critical", "critical", 200, "surfaced", "2026-10-05 09:00:00", 1),
            ("pulled-major", "major", 100, "surfaced", "2026-10-05 09:00:00", 1),
            ("never-named", "critical", 100, "queued", None, 0),
        ):
            conn.execute(
                f"INSERT INTO findings ({columns}) VALUES (?, 'inventory', ?, 'acme', 'prod', 'o', 't', ?, ?, "
                "'{\"B\": 8, \"L\": 6, \"detect\": 3, \"recover\": 2, \"C\": 100}', '{}', '{}', '{}', ?, ?, ?)",
                (rid, rid, severity, score, state, surfaced_at, count),
            )
        return conn

    def test_a_released_table_gains_the_columns_and_keeps_its_rows(self):
        conn = self._released_table()

        fq.init_findings_schema(conn)

        rows = {f["id"]: f for f in fq.ranked_findings(conn)}
        self.assertEqual(
            set(rows), {"named-critical", "named-critical-2", "pulled-critical", "pulled-major", "never-named"}
        )
        # The old nudge named these criticals, so they are shown and keep being
        # reminded, but they are no addition: no day's budget is spent on them.
        for rid in ("named-critical", "named-critical-2"):
            self.assertEqual(rows[rid]["first_shown_at"], "2026-10-05 12:00:01")
            self.assertIsNone(rows[rid]["added_class"])
        # Below the old nudge's top two, so a pull marked it: shown here, it
        # would be pending without ever being added.
        self.assertIsNone(rows["pulled-critical"]["first_shown_at"])
        # The old nudge never marked a non-critical, so whoever did was not a
        # paced publisher: shown here, it would stop every addition.
        self.assertIsNone(rows["pulled-major"]["first_shown_at"])
        self.assertIsNone(rows["never-named"]["first_shown_at"])
        decision = fq.pace(fq.ranked_findings(conn), at(6, 12), fq.PacingLimits(), NONE_ADDED)
        self.assertEqual(ids(decision.add), [["pulled-critical"], ["never-named"]])
        self.assertEqual(ids(decision.remind), [["named-critical"], ["named-critical-2"]])
        self.assertEqual(fq.additions_on(conn, "2026-10-05"), {"day": "2026-10-05", "critical": 0, "noncritical": 0})
        self.assertEqual(fq.register_findings(conn, [sample()])["results"][0]["outcome"], "created")

    def test_a_table_with_the_pacing_columns_gains_absent_since_and_reads_as_still_reported(self):
        # A table an earlier build of the pacing change wrote: `first_shown_at`
        # and `added_class` exist, `absent_since` does not. A shown critical at
        # C = 0.6 may have been downgraded for absence or registered as
        # inferred; nothing tells them apart, so it stays pending and reminded.
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute(fq.FINDINGS_SCHEMA.replace("    absent_since      TIMESTAMP,\n", ""))
        conn.execute(
            "INSERT INTO findings (id, source, check_slug, project, cluster, object, title, severity, rank_score, "
            "rubric, recommendation, remediation, verification, state, first_shown_at) VALUES ('inferred', "
            "'inventory', 'wi-off', 'acme', 'prod', 'prod', 't', 'critical', 173, "
            "'{\"B\": 8, \"L\": 6, \"detect\": 3, \"recover\": 3, \"C\": 60}', '{}', '{}', '{}', 'surfaced', "
            "'2026-10-01 12:00:00')"
        )

        fq.init_findings_schema(conn)

        rows = fq.ranked_findings(conn)
        self.assertIsNone(rows[0]["absent_since"])
        self.assertEqual(rows[0]["first_shown_at"], "2026-10-01 12:00:00")
        self.assertEqual(ids(fq.pace(rows, at(6, 12), fq.PacingLimits(), NONE_ADDED).remind), [["inferred"]])

    def test_the_backfill_runs_once(self):
        conn = self._released_table()
        fq.init_findings_schema(conn)
        conn.execute("UPDATE findings SET first_shown_at = NULL WHERE id = 'named-critical'")

        fq.init_findings_schema(conn)

        self.assertIsNone(fq.get_finding(conn, "named-critical")["first_shown_at"])


class TestShownMarker(QueueTestCase):
    def setUp(self):
        super().setUp()
        self.register(sample())
        self.fid = self.ids()[0]

    def test_a_pull_names_without_showing(self):
        # The MCP tool sends no publisher: a model answering "show me the list"
        # must not spend a day's budget or create a pending item.
        row = fq.mark_surfaced(self.conn, self.fid, "spaces/AAA")
        self.assertEqual(row["surface_count"], 1)
        self.assertEqual(row["state"], "surfaced")
        self.assertIsNone(row["first_shown_at"])
        self.assertIsNone(row["added_class"])

    def test_a_paced_publisher_shows_once_and_records_the_class_once(self):
        first = fq.mark_surfaced(self.conn, self.fid, publisher="nudge", added_class="noncritical")
        self.assertIsNotNone(first["first_shown_at"])
        self.assertEqual(first["added_class"], "noncritical")
        self.conn.execute("UPDATE findings SET first_shown_at = '2026-10-01 12:00:00' WHERE id = ?", (self.fid,))

        again = fq.mark_surfaced(self.conn, self.fid, publisher="nudge", added_class="critical")

        self.assertEqual(again["first_shown_at"], "2026-10-01 12:00:00")
        self.assertEqual(again["added_class"], "noncritical")
        self.assertEqual(again["surface_count"], 2)

    def test_a_row_joining_a_shown_item_is_shown_without_a_class(self):
        row = fq.mark_surfaced(self.conn, self.fid, publisher="nudge")
        self.assertIsNotNone(row["first_shown_at"])
        self.assertIsNone(row["added_class"])

    def test_only_a_paced_publisher_may_show(self):
        with self.assertRaises(fq.FindingError):
            fq.mark_surfaced(self.conn, self.fid, publisher="backlog")
        with self.assertRaises(fq.FindingError):
            fq.mark_surfaced(self.conn, self.fid, added_class="critical")
        with self.assertRaises(fq.FindingError):
            fq.mark_surfaced(self.conn, self.fid, publisher="nudge", added_class="major")
        self.assertIsNone(fq.get_finding(self.conn, self.fid)["first_shown_at"])

    def test_a_paced_publisher_may_not_show_a_decided_row(self):
        # A re-armed first report can name a finding the user dismissed earlier.
        for state, patch in (
            ("dismissed", {"state": "dismissed"}),
            ("accepted", {"state": "accepted"}),
            ("snoozed", {"state": "snoozed", "snoozed_until": "2099-01-01T00:00:00Z"}),
        ):
            with self.subTest(state=state):
                fq.patch_finding(self.conn, self.fid, patch)
                for publisher in fq.PACED_PUBLISHERS:
                    with self.assertRaises(fq.FindingError):
                        fq.mark_surfaced(self.conn, self.fid, publisher=publisher, added_class="critical", run="r1")
                row = fq.get_finding(self.conn, self.fid)
                self.assertEqual((row["state"], row["surface_count"]), (state, 0))
                self.assertIsNone(row["first_shown_at"])
                self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM findings_additions").fetchone()[0], 0)
        # A pull still names it.
        self.assertEqual(fq.mark_surfaced(self.conn, self.fid)["surface_count"], 1)

    def test_a_recurrence_is_new_again(self):
        fq.mark_surfaced(self.conn, self.fid, publisher="nudge", added_class="noncritical")
        fq.record_verification(self.conn, self.fid, "resolved")

        self.register(sample())

        row = fq.get_finding(self.conn, self.fid)
        self.assertIsNone(row["first_shown_at"])
        self.assertIsNone(row["added_class"])


class TestAdditions(QueueTestCase):
    # mark_surfaced stamps each addition with SQLite's date('now'), the real
    # UTC day, so the tests compare against today's UTC date.
    def setUp(self):
        super().setUp()
        self.DAY = datetime.now(UTC).date().isoformat()

    def add(self, finding, added_class="critical", when=at(6, 12)):
        # `when` only names the run: two calls with the same `when` are one run.
        fid = fq.validate_finding(finding)["id"]
        fq.mark_surfaced(self.conn, fid, publisher="nudge", added_class=added_class, run=when.isoformat())
        return fid

    def test_dismissing_or_snoozing_refunds_nothing(self):
        first = sample(check="a", rubric=CRITICAL_RUBRIC)
        second = sample(check="b", rubric=CRITICAL_RUBRIC)
        self.register(first, second)
        dismissed, snoozed = self.add(first), self.add(second)
        fq.patch_finding(self.conn, dismissed, {"state": "dismissed"})
        fq.patch_finding(self.conn, snoozed, {"state": "snoozed", "snoozed_until": "2026-12-01"})

        self.assertEqual(fq.ranked_findings(self.conn), [])
        self.assertEqual(fq.additions_on(self.conn, self.DAY), {"day": self.DAY, "critical": 2, "noncritical": 0})

    def test_a_gathered_line_is_one_addition(self):
        members = [sample(object=f"Deployment/d{i}") for i in range(3)]
        self.register(*members)
        for member in members:
            self.add(member, "noncritical")
        self.assertEqual(fq.additions_on(self.conn, self.DAY)["noncritical"], 1)

    def test_a_line_added_again_later_the_same_day_counts_again(self):
        # x0 is added and dismissed; a new object on the same line is a new
        # item, and adding it is a second message, so a second addition.
        first, second = sample(object="Deployment/x0"), sample(object="Deployment/a0")
        self.register(first)
        fq.patch_finding(self.conn, self.add(first, when=at(6, 12)), {"state": "dismissed"})
        self.register(second)
        self.add(second, when=at(6, 13))
        self.assertEqual(fq.additions_on(self.conn, self.DAY)["critical"], 2)

    def test_a_recurrence_refunds_nothing(self):
        first, second = sample(check="a", rubric=CRITICAL_RUBRIC), sample(check="b", rubric=CRITICAL_RUBRIC)
        self.register(first, second)
        added = [self.add(first), self.add(second)]
        for fid in added:
            fq.record_verification(self.conn, fid, "resolved")
        self.register(first, second)

        self.assertIsNone(fq.get_finding(self.conn, added[0])["first_shown_at"])
        self.assertEqual(fq.additions_on(self.conn, self.DAY)["critical"], 2)

    def test_the_day_is_the_utc_date(self):
        self.register(sample())
        self.add(sample())
        self.assertEqual(fq.additions_on(self.conn, self.DAY)["critical"], 1)

    def test_pulls_and_joins_are_not_additions(self):
        pulled, joined = sample(check="a"), sample(check="b")
        self.register(pulled, joined)
        fq.mark_surfaced(self.conn, fq.validate_finding(pulled)["id"])
        fq.mark_surfaced(self.conn, fq.validate_finding(joined)["id"], publisher="nudge")
        self.assertEqual(fq.additions_on(self.conn, self.DAY), {"day": self.DAY, "critical": 0, "noncritical": 0})

    def test_the_first_report_is_a_paced_publisher(self):
        # bootstrap_delivery.py marks each row of the report's items with one run.
        members = [sample(object=f"Deployment/d{i}", rubric=CRITICAL_RUBRIC) for i in range(2)]
        self.register(*members)
        for member in members:
            fid = fq.validate_finding(member)["id"]
            fq.mark_surfaced(self.conn, fid, publisher="first_report", added_class="critical", run="2026-10-07T09:00:00Z")
            self.assertIsNotNone(fq.get_finding(self.conn, fid)["first_shown_at"])
        self.assertEqual(fq.additions_on(self.conn, self.DAY), {"day": self.DAY, "critical": 1, "noncritical": 0})

    def test_a_day_must_be_a_date(self):
        for day in ("", "yesterday", "2026-10-06T00:00:00", None, "2026-99-99", "2026-02-30", "20261006", "٢٠٢٦-١٠-٠٦"):
            with self.assertRaises(fq.FindingError):
                fq.additions_on(self.conn, day)


class TestDecisionCoversItem(QueueTestCase):
    def setUp(self):
        super().setUp()
        self.members = [sample(object=f"Deployment/d{i}") for i in range(3)]
        self.other = sample(check="limits-missing")
        self.register(*self.members, self.other)
        self.member_ids = [fq.validate_finding(m)["id"] for m in self.members]
        for fid in self.member_ids[:2]:
            fq.mark_surfaced(self.conn, fid, publisher="nudge")

    def test_a_decision_on_one_id_decides_the_line(self):
        result = fq.patch_finding(self.conn, self.member_ids[0], {"state": "snoozed", "snoozed_until": "2026-12-01"})
        self.assertEqual(sorted(result["item_rows_decided"]), sorted(self.member_ids[1:]))
        for fid in self.member_ids:
            row = fq.get_finding(self.conn, fid)
            self.assertEqual(row["state"], "snoozed")
            self.assertIsNotNone(row["snoozed_until"])
        self.assertEqual(fq.get_finding(self.conn, fq.validate_finding(self.other)["id"])["state"], "queued")

    def test_deciding_the_named_id_clears_stop_add(self):
        rows = fq.ranked_findings(self.conn)
        for r in rows:
            r["first_shown_at"] = stamp(at(5, 16)) if r["first_shown_at"] else None
        self.assertEqual(len(fq.pace(rows, at(6, 12), fq.PacingLimits(), NONE_ADDED).blocking), 1)

        fq.patch_finding(self.conn, self.member_ids[0], {"state": "dismissed"})

        rows = fq.ranked_findings(self.conn)
        self.assertEqual(fq.pace(rows, at(6, 12), fq.PacingLimits(), NONE_ADDED).blocking, [])

    def test_decided_rows_and_other_transitions_stay_per_row(self):
        # A never-shown member of the line is in the item too.
        accepted = fq.patch_finding(self.conn, self.member_ids[2], {"state": "accepted"})
        self.assertEqual(sorted(accepted["item_rows_decided"]), sorted(self.member_ids[:2]))
        # Rows already decided keep their decision.
        result = fq.patch_finding(self.conn, self.member_ids[0], {"state": "dismissed"})
        self.assertEqual(result["item_rows_decided"], [])
        self.assertEqual(fq.get_finding(self.conn, self.member_ids[1])["state"], "accepted")

        result = fq.patch_finding(self.conn, self.member_ids[1], {"state": "surfaced"})
        self.assertNotIn("item_rows_decided", result)
        self.assertEqual(fq.get_finding(self.conn, self.member_ids[2])["state"], "accepted")


class TestAbsenceMarker(QueueTestCase):
    """`absent_since` is what takes a shown row out of pending, not C = 0.6 (§5.2, §7.2)."""

    SCOPE = {"project": "acme-prod", "cluster": "prod-eu", "complete": True}
    # 8 * 6 * (3 + 3) * 0.6 = 173: critical at the "inferred" confidence.
    INFERRED_CRITICAL = {"B": 8, "L": 6, "detect": 3, "recover": 3, "C": 0.6}

    def setUp(self):
        super().setUp()
        self.inferred = sample(check="workload-identity-off", namespace="", object="prod-eu", rubric=self.INFERRED_CRITICAL)
        self.major = sample(check="limits-missing", rubric={"B": 3, "L": 6, "detect": 3, "recover": 2, "C": 1.0})
        self.other = sample(check="probes-liveness", rubric=CRITICAL_RUBRIC)
        self.register(self.inferred, self.major, self.other)
        self.inferred_id = fq.validate_finding(self.inferred)["id"]
        self.major_id = fq.validate_finding(self.major)["id"]
        for fid in (self.inferred_id, self.major_id):
            fq.mark_surfaced(self.conn, fid, publisher="nudge")
            self.conn.execute("UPDATE findings SET first_shown_at = ? WHERE id = ?", (stamp(at(4, 12)), fid))

    def plan(self):
        return fq.pace(fq.ranked_findings(self.conn), at(6, 12), fq.PacingLimits(), NONE_ADDED)

    def test_a_critical_registered_as_inferred_stays_pending_and_is_reminded(self):
        self.register(self.inferred, self.major, self.other, scope=self.SCOPE)
        row = fq.get_finding(self.conn, self.inferred_id)
        self.assertEqual((row["severity"], row["rubric"]["C"], row["absent_since"]), ("critical", 0.6, None))
        plan = self.plan()
        self.assertEqual(ids(plan.remind), [[self.inferred_id]])
        self.assertEqual(ids(plan.blocking), [[self.major_id]])

    def test_rows_a_complete_sweep_missed_are_neither_pending_nor_reminded(self):
        result = self.register(self.other, scope=self.SCOPE)
        # The major was re-ranked; the inferred critical was already at 0.6,
        # so it is marked without a re-rank and not counted.
        self.assertEqual(result["downgraded"], 1)
        for fid in (self.inferred_id, self.major_id):
            self.assertIsNotNone(fq.get_finding(self.conn, fid)["absent_since"])
        self.assertEqual(fq.get_finding(self.conn, self.inferred_id)["rank_score"], 173)
        plan = self.plan()
        self.assertEqual((plan.remind, plan.blocking), ([], []))
        # Nothing pending holds back the new critical.
        self.assertEqual(ids(plan.add), [[fq.validate_finding(self.other)["id"]]])

    def test_a_later_miss_keeps_the_first(self):
        self.register(self.other, scope=self.SCOPE)
        self.conn.execute("UPDATE findings SET absent_since = '2026-10-01 00:00:00' WHERE id = ?", (self.major_id,))
        self.register(self.other, scope=self.SCOPE)
        self.assertEqual(fq.get_finding(self.conn, self.major_id)["absent_since"], "2026-10-01 00:00:00")

    def test_a_missed_row_reported_again_is_pending_again(self):
        self.register(self.other, scope=self.SCOPE)
        self.register(self.inferred, self.major)
        for fid in (self.inferred_id, self.major_id):
            self.assertIsNone(fq.get_finding(self.conn, fid)["absent_since"])
        self.assertEqual(fq.get_finding(self.conn, self.major_id)["rubric"]["C"], 1.0)
        plan = self.plan()
        self.assertEqual(ids(plan.remind), [[self.inferred_id]])
        self.assertEqual(ids(plan.blocking), [[self.major_id]])

    def test_verification_that_it_still_fails_clears_the_marker(self):
        self.register(self.other, scope=self.SCOPE)
        fq.record_verification(self.conn, self.major_id, "unverifiable", "Forbidden")
        self.assertIsNotNone(fq.get_finding(self.conn, self.major_id)["absent_since"])
        fq.record_verification(self.conn, self.major_id, "still_failing", "no limits")
        self.assertIsNone(fq.get_finding(self.conn, self.major_id)["absent_since"])

    def test_a_recurrence_and_a_suppressed_report_clear_the_marker(self):
        self.register(self.other, scope=self.SCOPE)
        fq.record_verification(self.conn, self.major_id, "resolved")
        fq.patch_finding(self.conn, self.inferred_id, {"state": "dismissed"})
        self.register(self.inferred, self.major)
        self.assertIsNone(fq.get_finding(self.conn, self.major_id)["absent_since"])
        self.assertIsNone(fq.get_finding(self.conn, self.inferred_id)["absent_since"])


class TestPacingLimits(unittest.TestCase):
    def limits(self, env):
        err = io.StringIO()
        with unittest.mock.patch.object(sys, "stderr", err):
            return fq.pacing_limits(env), err.getvalue()

    def test_the_defaults(self):
        limits, err = self.limits({})
        self.assertEqual(limits, fq.PacingLimits(2, 2, 3, 16))
        self.assertEqual(err, "")

    def test_overrides_including_zero(self):
        limits, err = self.limits(
            {
                "FINDINGS_FIRST_REPORT_CRITICALS": "0",
                "FINDINGS_DAILY_CRITICALS": " 5 ",
                "FINDINGS_NONCRITICAL_MAX": "0",
                "FINDINGS_NONCRITICAL_AFTER_HOUR": "0",
            }
        )
        self.assertEqual(limits, fq.PacingLimits(0, 5, 0, 0))
        self.assertEqual(err, "")

    def test_a_bad_value_falls_back_to_its_default_and_says_so(self):
        for variable, raw in (
            ("FINDINGS_DAILY_CRITICALS", "two"),
            ("FINDINGS_DAILY_CRITICALS", "2.5"),
            ("FINDINGS_NONCRITICAL_MAX", "-1"),
            ("FINDINGS_NONCRITICAL_AFTER_HOUR", "24"),
            ("FINDINGS_NONCRITICAL_AFTER_HOUR", "-3"),
        ):
            with self.subTest(variable=variable, raw=raw):
                limits, err = self.limits({variable: raw})
                self.assertEqual(limits, fq.PacingLimits())
                self.assertIn(variable, err)
                self.assertIn("using the default", err)

    def test_the_last_hour_of_the_day_is_an_hour(self):
        limits, _ = self.limits({"FINDINGS_NONCRITICAL_AFTER_HOUR": "23"})
        self.assertEqual(limits.noncritical_after_hour, 23)


class TestPace(QueueTestCase):
    LIMITS = fq.PacingLimits()

    def pace(self, rows, now, added=NONE_ADDED, limits=None, may_add=True):
        return fq.pace(rows, now, limits or self.LIMITS, added, may_add)

    def test_six_criticals_add_the_top_two_at_noon(self):
        rows = [row(f"c{i}", score=300 - i) for i in range(6)]
        plan = self.pace(rows, at(6, 12))
        self.assertEqual(ids(plan.add), [["c0"], ["c1"]])
        self.assertEqual(len(plan.waiting), 4)
        self.assertEqual(plan.remind, [])
        self.assertEqual(plan.blocking, [])

    def test_two_added_today_means_none_more_today(self):
        rows = [row("c0", state="surfaced", shown=at(6, 12)), row("c1", state="surfaced", shown=at(6, 12))]
        rows += [row(f"c{i}") for i in range(2, 6)]
        plan = self.pace(rows, at(6, 20), {"critical": 2, "noncritical": 0})
        self.assertEqual(plan.add, [])
        # Shown today, so not reminded today either.
        self.assertEqual(plan.remind, [])

    def test_the_next_day_adds_two_more_and_reminds_the_two_pending(self):
        rows = [row("c0", state="surfaced", shown=at(6, 12)), row("c1", state="surfaced", shown=at(6, 12))]
        rows += [row(f"c{i}") for i in range(2, 6)]
        plan = self.pace(rows, at(7, 12))
        self.assertEqual(ids(plan.add), [["c2"], ["c3"]])
        self.assertEqual(ids(plan.remind), [["c0"], ["c1"]])

    def test_the_day_rolls_over_at_midnight_but_criticals_wait_for_noon(self):
        rows = [row("c0", state="surfaced", shown=at(6, 23, 30)), row("c1")]
        self.assertEqual(self.pace(rows, at(7, 0)).add, [])
        self.assertEqual(self.pace(rows, at(7, 11, 59)).add, [])
        self.assertEqual(ids(self.pace(rows, at(7, 12)).add), [["c1"]])

    def test_zero_criticals_wait_for_the_noncritical_hour_then_add_three(self):
        rows = [row(f"m{i}", "major", score=100 - i) for i in range(5)]
        self.assertEqual(self.pace(rows, at(6, 12)).add, [])
        self.assertEqual(self.pace(rows, at(6, 15, 59)).add, [])
        plan = self.pace(rows, at(6, 16))
        self.assertEqual(ids(plan.add), [["m0"], ["m1"], ["m2"]])
        self.assertEqual(len(plan.waiting), 2)

    def test_the_noncritical_cap_is_per_day(self):
        rows = [row(f"m{i}", "major") for i in range(5)]
        self.assertEqual(self.pace(rows, at(6, 18), {"critical": 0, "noncritical": 3}).add, [])
        self.assertEqual(ids(self.pace(rows, at(6, 18), {"critical": 0, "noncritical": 1}).add), [["m0"], ["m1"]])

    def test_an_announced_but_unrecorded_item_counts_against_its_class_budget(self):
        rows = [row("c-new", score=400), row("c0", score=300), row("c1", score=290), row("m0", "major")]
        announced = [fq.item_key(r) for r in rows[1:3]]
        plan = fq.pace(rows, at(6, 13), self.LIMITS, NONE_ADDED, announced=announced)
        self.assertEqual(plan.add, [])
        self.assertEqual(ids(plan.waiting), [["c-new"], ["m0"]])
        # With one of the two announced, one slot is left, and an unrecorded
        # critical still holds the major back.
        plan = fq.pace(rows, at(6, 16), self.LIMITS, NONE_ADDED, announced=announced[:1])
        self.assertEqual(ids(plan.add), [["c-new"]])
        plan = fq.pace(rows[1:2] + rows[3:], at(6, 16), self.LIMITS, NONE_ADDED, announced=announced[:1])
        self.assertEqual(plan.add, [])

    def test_an_announced_but_unrecorded_noncritical_stops_every_addition(self):
        rows = [row("c-new", score=400), row("m0", "major")]
        plan = fq.pace(rows, at(6, 17), self.LIMITS, NONE_ADDED, announced=[fq.item_key(rows[1])])
        self.assertEqual(plan.add, [])
        self.assertEqual(plan.blocking, [])
        self.assertEqual(ids(plan.waiting), [["c-new"]])
        # As when its mark succeeded and it is pending.
        rows[1] = row("m0", "major", state="surfaced", shown=at(6, 16))
        self.assertEqual(fq.pace(rows, at(6, 17), self.LIMITS, NONE_ADDED).add, [])

    def test_a_pending_critical_holds_back_noncriticals(self):
        rows = [row("c0", state="surfaced", shown=at(5, 12)), row("m0", "major")]
        plan = self.pace(rows, at(6, 16))
        self.assertEqual(plan.add, [])
        self.assertEqual(ids(plan.remind), [["c0"]])

    def test_a_critical_waiting_for_budget_holds_back_noncriticals(self):
        # Today's two criticals were dismissed (so not in /ranked), a third is
        # waiting for tomorrow: adding majors now would block it behind stop-add.
        rows = [row("c2"), row("m0", "major")]
        plan = self.pace(rows, at(6, 16), {"critical": 2, "noncritical": 0})
        self.assertEqual(plan.add, [])
        self.assertEqual(ids(plan.waiting), [["c2"], ["m0"]])

    def test_a_critical_waiting_for_noon_holds_back_noncriticals_on_an_early_hour(self):
        limits = fq.PacingLimits(noncritical_after_hour=8)
        rows = [row("c0"), row("m0", "major")]
        self.assertEqual(self.pace(rows, at(6, 9), limits=limits).add, [])

    def test_a_pending_noncritical_stops_every_addition(self):
        rows = [
            row("c-old", state="surfaced", shown=at(4, 12)),
            row("m0", "major", state="surfaced", shown=at(5, 16)),
            row("c-new"),
        ]
        plan = self.pace(rows, at(6, 12))
        self.assertEqual(plan.add, [])
        self.assertEqual(ids(plan.blocking), [["m0"]])
        self.assertEqual(ids(plan.waiting), [["c-new"]])
        # Reminders are not additions, so stop-add does not silence them.
        self.assertEqual(ids(plan.remind), [["c-old"]])

    def test_dismissed_snoozed_and_accepted_rows_are_not_pending(self):
        # Dismissed and snoozed rows are not in /ranked at all; accepted ones are.
        rows = [row("m0", "major", state="accepted", shown=at(5, 16)), row("c0")]
        plan = self.pace(rows, at(6, 12))
        self.assertEqual(plan.blocking, [])
        self.assertEqual(ids(plan.add), [["c0"]])

    def test_a_lapsed_snooze_of_a_shown_row_is_pending_again(self):
        major, critical = sample(check="m0"), sample(check="c0", rubric=CRITICAL_RUBRIC)
        self.register(major, critical)
        major_id, critical_id = (fq.validate_finding(f)["id"] for f in (major, critical))
        fq.mark_surfaced(self.conn, major_id, publisher="nudge", added_class="noncritical")
        fq.patch_finding(self.conn, major_id, {"state": "snoozed", "snoozed_until": "2000-01-01"})
        # While snoozed it is not in /ranked, so nothing stops the critical.
        self.assertEqual(ids(self.pace(fq.ranked_findings(self.conn), at(6, 12)).add), [[critical_id]])

        self.assertEqual(fq.expire_snoozes(self.conn), 1)

        plan = self.pace(fq.ranked_findings(self.conn), at(6, 12))
        self.assertEqual(ids(plan.blocking), [[major_id]])
        self.assertEqual(plan.add, [])

    def test_a_pull_marked_row_is_new_not_pending(self):
        rows = [row("m0", "major", state="surfaced"), row("c0")]
        plan = self.pace(rows, at(6, 12))
        self.assertEqual(plan.blocking, [])
        self.assertEqual(ids(plan.add), [["c0"]])

    def test_a_row_the_sweep_stopped_reporting_is_not_pending(self):
        # The fix the user made drops C to 0.6, which can re-score a critical
        # as major. That must not turn on stop-add, nor be reminded forever.
        rows = [
            row("fixed-major", "major", state="surfaced", shown=at(4, 12), C=0.6, absent=at(5, 3)),
            row("fixed-critical", state="surfaced", shown=at(4, 12), C=0.6, absent=at(5, 3)),
            row("c0"),
        ]
        plan = self.pace(rows, at(6, 12))
        self.assertEqual(plan.blocking, [])
        self.assertEqual(plan.remind, [])
        self.assertEqual(ids(plan.add), [["c0"]])

    def test_a_row_registered_as_inferred_is_pending_like_any_other(self):
        # C = 0.6 is also what a source registers for "inferred". Only the
        # absence rule's marker takes a shown row out of pending.
        rows = [
            row("wi-off", state="surfaced", shown=at(4, 12), C=0.6, score=173),
            row("m0", "major", state="surfaced", shown=at(4, 16), C=0.6, score=54),
            row("c0"),
        ]
        plan = self.pace(rows, at(6, 12))
        self.assertEqual(ids(plan.remind), [["wi-off"]])
        self.assertEqual(ids(plan.blocking), [["m0"]])
        self.assertEqual(plan.add, [])

    def test_a_never_shown_row_the_sweep_stopped_reporting_is_still_added(self):
        # Absence lowers confidence and does not resolve (§5.2), and the old
        # nudge named such rows. Once shown, it is neither pending nor reminded.
        rows = [row("gone", C=0.6, score=173, absent=at(5, 3))]
        self.assertEqual(ids(self.pace(rows, at(6, 12)).add), [["gone"]])
        rows = [row("gone", state="surfaced", shown=at(6, 12), C=0.6, score=173, absent=at(5, 3))]
        plan = self.pace(rows, at(7, 12))
        self.assertEqual((plan.add, plan.remind, plan.blocking), ([], [], []))

    def test_an_item_covers_every_undecided_row_of_its_line(self):
        # Pending p0, never-shown p1, two shown rows the sweep stopped
        # reporting (in no item, still undecided), an accepted row (decided).
        rows = [
            row("p0", check="probes", state="surfaced", shown=at(5, 12)),
            row("p1", check="probes"),
            row("p2", check="probes", state="surfaced", shown=at(5, 12), C=0.6, absent=at(5, 20)),
            row("p3", check="probes", state="surfaced", shown=at(5, 12), C=0.6, absent=at(5, 20)),
            row("p4", check="probes", state="accepted", shown=at(5, 12)),
            row("obs", check="probes", provider_managed=True, actionable=False),
        ]
        plan = self.pace(rows, at(6, 12))
        self.assertEqual(ids(plan.remind), [["p0", "p1"]])
        self.assertEqual(plan.remind[0].covers, 4)

    def test_a_gathered_line_is_one_item_named_whole(self):
        rows = [
            row("probe-a", check="probes"),
            row("other", score=200),
            row("probe-b", "major", check="probes"),
            row("probe-c", "minor", check="probes"),
        ]
        plan = self.pace(rows, at(6, 12))
        self.assertEqual(ids(plan.add), [["probe-a", "probe-b", "probe-c"], ["other"]])
        self.assertEqual(plan.add[0].item_class, "critical")

    def test_a_gathered_line_whose_critical_resolved_is_a_pending_noncritical(self):
        # A sweep resolved the critical member, so /ranked no longer has it.
        # Verification changes only its own row, so the major shown with it
        # still waits for a decision.
        rows = [row("probe-b", "major", check="probes", state="surfaced", shown=at(5, 12)), row("c0")]
        plan = self.pace(rows, at(6, 12))
        self.assertEqual(ids(plan.blocking), [["probe-b"]])
        self.assertEqual(plan.blocking[0].severity, "major")
        self.assertEqual(plan.add, [])

    def test_a_member_that_joins_a_shown_line_rides_along(self):
        rows = [
            row("probe-a", check="probes", state="surfaced", shown=at(5, 12)),
            row("probe-b", check="probes"),
        ]
        plan = self.pace(rows, at(6, 12))
        self.assertEqual(plan.add, [])
        self.assertEqual(ids(plan.remind), [["probe-a", "probe-b"]])

    def test_a_member_left_behind_by_an_accepted_line_is_new(self):
        rows = [
            row("probe-a", check="probes", state="accepted", shown=at(5, 12)),
            row("probe-b", check="probes"),
        ]
        self.assertEqual(ids(self.pace(rows, at(6, 12)).add), [["probe-b"]])

    def test_the_line_is_check_project_and_cluster(self):
        rows = [row("a", check="probes"), row("b", check="probes", cluster="staging"), row("c", check="Probes")]
        self.assertEqual(ids(self.pace(rows, at(6, 12), limits=fq.PacingLimits(daily_criticals=5)).add), [["a", "c"], ["b"]])

    def test_provider_managed_observations_are_never_items(self):
        rows = [row("obs", provider_managed=True, actionable=False), row("fault", provider_managed=True)]
        plan = self.pace(rows, at(6, 12))
        self.assertEqual(ids(plan.add), [["fault"]])
        self.assertEqual(plan.rolled_up, 1)

    def test_provider_managed_observations_are_counted_by_line(self):
        observation = {"provider_managed": True, "actionable": False}
        rows = [row(f"dns{i}", check="dns", **observation) for i in range(3)]
        rows += [row("dns-staging", check="dns", cluster="staging", **observation), row("c0")]
        self.assertEqual(self.pace(rows, at(6, 12)).rolled_up, 2)

    def test_a_daily_limit_of_zero_adds_and_reminds_no_criticals(self):
        limits = fq.PacingLimits(daily_criticals=0)
        rows = [row("c-old", state="surfaced", shown=at(4, 12)), row("c0"), row("m0", "major")]
        plan = self.pace(rows, at(6, 16), limits=limits)
        self.assertEqual(plan.remind, [])
        # Nothing is added, and with a pending critical no non-critical either.
        self.assertEqual(plan.add, [])
        plan = self.pace(rows[1:], at(6, 16), limits=limits)
        self.assertEqual(ids(plan.add), [["m0"]])

    def test_a_noncritical_limit_of_zero_adds_none(self):
        rows = [row("m0", "major")]
        self.assertEqual(self.pace(rows, at(6, 20), limits=fq.PacingLimits(noncritical_max=0)).add, [])

    def test_reminders_are_the_top_daily_criticals(self):
        rows = [row(f"c{i}", state="surfaced", shown=at(1, 12), score=300 - i) for i in range(4)]
        self.assertEqual(ids(self.pace(rows, at(6, 12)).remind), [["c0"], ["c1"]])

    def test_nothing_is_added_before_the_first_report(self):
        rows = [row("c0"), row("m0", "major")]
        self.assertEqual(self.pace(rows, at(6, 20), may_add=False).add, [])

    def test_a_naive_now_is_utc(self):
        rows = [row("c0")]
        self.assertEqual(ids(self.pace(rows, datetime(2026, 10, 6, 12)).add), [["c0"]])

    def test_from_the_database_the_covered_count_is_what_a_decision_decides(self):
        conn = self.conn
        members = [sample(object=f"Deployment/d{i}", rubric=CRITICAL_RUBRIC) for i in range(5)]
        self.register(*members)
        member_ids = [fq.validate_finding(m)["id"] for m in members]
        for fid in member_ids:
            fq.mark_surfaced(conn, fid, publisher="nudge", added_class="critical")
        # A complete sweep reports two of the five; the other three are no
        # longer pending, but a decision on the line still reaches them.
        self.register(*members[:2], scope={"project": "acme-prod", "cluster": "prod-eu", "complete": True})
        # Set directly: a decision through the route would decide the line.
        conn.execute("UPDATE findings SET state = 'accepted' WHERE id = ?", (member_ids[4],))
        rows = fq.ranked_findings(conn)
        for r in rows:
            r["first_shown_at"] = stamp(at(5, 12))

        item = fq.pace(rows, at(6, 12), self.LIMITS, NONE_ADDED).remind[0]
        self.assertEqual(len(item.members), 2)

        decided = fq.patch_finding(conn, item.members[0]["id"], {"state": "dismissed"})["item_rows_decided"]
        self.assertEqual(item.covers, 1 + len(decided))
        self.assertEqual(item.covers, 4)

    def test_from_the_database_dismissing_refunds_nothing(self):
        conn = self.conn
        findings = [sample(check=f"c{i}", rubric=CRITICAL_RUBRIC) for i in range(3)]
        self.register(*findings)
        plan = fq.pace(fq.ranked_findings(conn), at(6, 12), self.LIMITS, fq.additions_on(conn, "2026-10-06"))
        self.assertEqual(len(plan.add), 2)
        for item in plan.add:
            for member in item.members:
                fq.mark_surfaced(conn, member["id"], publisher="nudge", added_class=item.item_class)
                conn.execute("UPDATE findings SET first_shown_at = ? WHERE id = ?", (stamp(at(6, 12)), member["id"]))
                fq.patch_finding(conn, member["id"], {"state": "dismissed"})
        conn.execute("UPDATE findings_additions SET day = '2026-10-06'")

        plan = fq.pace(fq.ranked_findings(conn), at(6, 13), self.LIMITS, fq.additions_on(conn, "2026-10-06"))

        self.assertEqual(plan.add, [])
        self.assertEqual(len(plan.waiting), 1)


class SopRubricParityTests(unittest.TestCase):
    """The SOP is where a model reads the rubric from; this is where it stops drifting.

    Nothing at runtime compares the two: the worker classifies from the SOP's
    tables and `findings_queue` scores from its own constants, so a scale edited
    in one place and not the other produces findings that validate and rank
    wrong.
    """

    @classmethod
    def setUpClass(cls):
        cls.text = PRIORITIZE_SOP.read_text(encoding="utf-8")

    def _anchors(self, marker):
        rows = _table_after(self.text, marker)
        return tuple(sorted(int(row[0]) for row in rows if row[0].isdigit()))

    def test_anchor_scales_match_the_module(self):
        self.assertEqual(self._anchors("**B — blast radius"), tuple(sorted(fq.B_ANCHORS)))
        self.assertEqual(self._anchors("**L — likelihood"), tuple(sorted(fq.L_ANCHORS)))
        self.assertEqual(self._anchors("**detect and recover"), tuple(sorted(fq.E_ANCHORS)))
        for confidence in fq.C_PERCENTS:
            self.assertIn(f"`{confidence / 100}`", self.text)

    def test_thresholds_and_floor_match_the_module(self):
        self.assertIn(f"`critical` at {fq.SEVERITY_CRITICAL_AT} and above", self.text)
        self.assertIn(f"`major` from {fq.SEVERITY_MAJOR_AT} to {fq.SEVERITY_CRITICAL_AT - 1}", self.text)
        self.assertIn(f"below {fq.SEVERITY_MAJOR_AT}", self.text)
        self.assertIn(
            f"**`L = {fq.FLOOR_LIKELIHOOD}` together with `B ≥ {fq.FLOOR_BLAST_RADIUS}`", self.text
        )

    def test_worked_examples_score_as_the_sop_prints_them(self):
        rows = _table_after(self.text, "### Worked examples")
        self.assertEqual(rows[0][:6], ["finding", "B", "L", "detect", "recover", "C"])
        self.assertGreater(len(rows) - 1, 5)
        for finding, b, likelihood, detect, recover, confidence, score, severity in rows[1:]:
            with self.subTest(finding=finding):
                rubric = fq.validate_rubric(
                    {
                        "B": int(b),
                        "L": int(likelihood),
                        "detect": int(detect),
                        "recover": int(recover),
                        "C": float(confidence),
                    }
                )
                computed = fq.rank_score(rubric)
                self.assertEqual(computed, int(score))
                self.assertEqual(fq.severity_for(computed, rubric), severity.split(" — ")[0])

    def test_the_floored_example_is_still_a_floored_example(self):
        # The row exists to show the floor overriding the bands. If a threshold
        # moves so that it clears `critical` on score alone, it stops
        # demonstrating anything and the SOP needs a new row.
        rows = _table_after(self.text, "### Worked examples")
        floored = [row for row in rows[1:] if row[7].endswith("floored")]
        self.assertEqual(len(floored), 1)
        self.assertLess(int(floored[0][6]), fq.SEVERITY_CRITICAL_AT)

    def test_provider_namespaces_match_the_module(self):
        for namespace in fq.PROVIDER_NAMESPACES:
            self.assertIn(f"`{namespace}`", self.text)
        for prefix in ("gke-*", "gmp-*"):
            self.assertIn(f"`{prefix}`", self.text)

    def test_the_sop_names_the_commands_and_enum_values_it_tells_the_worker_to_send(self):
        self.assertIn("inventory_findings.py extract", self.text)
        self.assertIn("inventory_findings.py register", self.text)
        self.assertIn("inventory_findings.py select", self.text)
        for kind in fq.REMEDIATION_KINDS:
            self.assertIn(f"`{kind}`", self.text)
        for kind in fq.VERIFICATION_KINDS:
            self.assertIn(f"`{kind}`", self.text)

    def test_the_sop_does_not_send_the_worker_at_the_queue_directly(self):
        # `source` and the identity fields moved out of the worker's hands when
        # `inventory_findings.py` took over the call; an SOP that still names
        # the MCP tools is telling it to bypass the completeness gate — and a
        # live run blocked outright looking for `get_ranked_findings` as a third
        # script once the steps either side of it became shell commands.
        self.assertNotIn("register_findings", self.text)
        self.assertNotIn("get_ranked_findings", self.text)

    def test_the_extractor_registers_under_a_source_the_queue_accepts(self):
        self.assertIn(inv.SOURCE, fq.SOURCES)

    def test_the_extractor_scores_only_fields_the_queue_takes_from_a_source(self):
        # A field the script forwards but `validate_finding` ignores is a score
        # the worker writes and nothing reads.
        forwarded = set(inv.SCORE_REQUIRED) | set(inv.SCORE_OPTIONAL)
        accepted = set(
            fq.validate_finding(
                {
                    "source": inv.SOURCE,
                    "check": "probes-readiness",
                    "project": "acme-prod",
                    "cluster": "prod",
                    "object": "api",
                    "title": "t",
                    "rubric": {"B": 3, "L": 6, "detect": 3, "recover": 2, "C": 1.0},
                    "recommendation": {"action": "a", "rationale": "r", "risk": "k"},
                    "remediation": {"kind": "manual", "note": "n"},
                    "verification": {"kind": "manual", "still_failing_when": "w"},
                }
            )
        )
        self.assertEqual(forwarded - accepted, set())


if __name__ == "__main__":
    unittest.main()
