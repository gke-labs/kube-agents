"""Tests for leeway_watch.py: discovery, 7-day gate, cause/severity escalation, reports, and roster."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import leeway_watch as watch  # noqa: E402

ROSTER = Path(__file__).resolve().parent.parent / "cron" / "jobs.json"
CRON_README = ROSTER.parent / "README.md"
WATCHDOGS_PAGE = (
    Path(__file__).resolve().parents[3] / "docs" / "site" / "src" / "content" / "docs" / "concepts" / "autonomous-watchdogs.md"
)
T0 = datetime(2026, 10, 19, 8, 30, 0, tzinfo=timezone.utc)
# The simulated leeway's start-up and report assembly per call.
SIM_STARTUP_SECONDS = 10.0


def make_topology_finding(
    name: str = "checkout-api",
    namespace: str = "shop",
    kind: str = "Deployment",
    replicas: int = 2,
    topology_key: str = "topology.kubernetes.io/zone",
    tier: str = "B",
    severity: str = "warning",
    cause: str = "constraint_ignored",
    actual_a: int = 2,
    actual_b: int = 0,
) -> dict:
    # The shape of a leeway.Finding as `leeway report` (v0.1.3) prints it:
    # pkg/leeway/finding.go; `subject` carries kind/namespace/name only.
    return {
        "kind": "leeway.placement_drift",
        "subject": {"kind": kind, "namespace": namespace, "name": name},
        "topologyKey": topology_key,
        "tier": tier,
        "severity": severity,
        "score": {
            "drift": 0.5,
            "observedSkew": abs(actual_a - actual_b),
            "minAchievableSkew": 0,
            "excessSkew": abs(actual_a - actual_b),
            "relocationDistance": 1,
            "maxDomainShare": actual_a / replicas if replicas else 1.0,
        },
        "intent": {
            "source": "topology-spread-constraint",
            "confidence": "declared",
            "weighting": "Equal",
            "maxSkew": 1,
            "evidence": [
                f"topologySpreadConstraints[0]: topologyKey={topology_key} maxSkew=1 whenUnsatisfiable=ScheduleAnyway labelSelector=app={name}",
                f"conflicting nodeAffinity/nodeSelector on {topology_key} restricts placement to 1 domain(s), contradicting the spread constraint on {topology_key}",
            ],
        },
        "domains": [
            {"domain": "us-central1-a", "actual": actual_a, "expected": 1, "delta": actual_a - 1, "readyNodes": 1},
            {"domain": "us-central1-b", "actual": actual_b, "expected": 1, "delta": actual_b - 1, "readyNodes": 1},
        ],
        "suspectedCause": cause,
        "contributingFactors": [
            "0 pods pinned by zonal volumes",
            f"whenUnsatisfiable: ScheduleAnyway with maxSkew 1 and observed skew {abs(actual_a - actual_b)}",
        ],
        "untestedCauses": ["rollout_bias"],
        "firstSeenAt": "2026-10-19T12:00:00Z",
        "remediation": {
            "kind": "manifest_patch",
            "summary": f"Remove the conflicting nodeAffinity/nodeSelector on {topology_key} from {kind}/{namespace}/{name} so its topologySpreadConstraint can spread replicas across eligible domains.",
            "command": f"kubectl -n {namespace} rollout restart deployment/{name}",
            "yaml": "spec:\n  template:\n    spec:\n      topologySpreadConstraints:\n      - maxSkew: 1\n        topologyKey: topology.kubernetes.io/zone\n        whenUnsatisfiable: ScheduleAnyway",
        },
    }


def make_rank_finding(
    compute_class: str = " balanced-spot",
    severity: str = "warning",
    rule: str = "last-rank",
    last_rank_share: float = 0.75,
) -> dict:
    # The shape of a leeway.RankFinding as `leeway report` (v0.1.3) prints it:
    # pkg/leeway/rankfinding.go. No suspectedCause, contributingFactors or
    # remediation; the judging rule and the observed shares carry the finding.
    return {
        "kind": "leeway.rank_degraded",
        "subject": {"kind": "PreferenceAxis", "name": compute_class.strip()},
        "provider": "gke-computeclass",
        "objectKind": "ComputeClass",
        "specHash": "842f2f5b7a0deb21",
        "tier": "B",
        "severity": severity,
        "rule": rule,
        "observed": {
            "windowSeconds": 3600,
            "tierSeconds": 21600,
            "rank0Share": round(1 - last_rank_share, 4),
            "lastRank": 1,
            "lastRankShare": last_rank_share,
            "meanRank": last_rank_share,
            "pendingPods": 1,
            "pendingProvisioning": 0,
            "pendingDeclined": 0,
            "degradedPods": 6,
            "improving": 0,
            "worsening": 0,
            "lateral": 0,
        },
        "ranks": [
            {"rank": 0, "rules": "machineFamily=n2", "podSeconds": 0, "share": round(1 - last_rank_share, 4), "pods": 0},
            {"rank": 1, "rules": "machineFamily=e2", "podSeconds": 21600, "share": last_rank_share, "pods": 6},
        ],
        "ordering": "list-position",
        "tiers": 2,
        "scaleUp": "unset",
        "optimizeRulePriority": False,
        "reason": f"rank 1 share {last_rank_share:.2f} is above the ceiling 0.90: running on last-resort capacity, which on most classes is the spot or the cheapest rule",
        "firstSeenAt": "2026-10-16T12:00:00Z",
    }


def make_report(clusters: list[dict], generated_at: datetime = T0) -> dict:
    return watch.merge_reports([{"clusters": clusters}], generated_at)


def completed_json(payload: dict, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["leeway", "report"],
        returncode=returncode,
        stdout=json.dumps(payload),
        stderr="",
    )


class GateAndReportingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "leeway"
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                watch.WATCH_HOME_ENV: str(self.home),
                watch.PROJECTS_ENV: "p1",
                watch.CLUSTERS_ENV: "",
                watch.REFRESH_DAYS_ENV: "",
                watch.MIN_SEVERITY_ENV: "",
                watch.MIN_DRIFT_ENV: "",
                watch.MIN_MAX_DOMAIN_SHARE_ENV: "",
                watch.MIN_EXCESS_OVER_FLOOR_ENV: "",
                watch.NATS_URL_ENV: "",
                watch.MANAGEMENT_PROJECT_ENV: "",
                "PLATFORM_AGENT_HOME": str(self.home),
            },
            clear=False,
        )
        self.env_patch.start()
        self.roster_patch = mock.patch.object(watch, "roster_clusters_by_project", return_value={})
        self.roster_patch.start()

    def tearDown(self) -> None:
        self.roster_patch.stop()
        self.env_patch.stop()
        self.tmp.cleanup()

    def run_tick(self, fake_run, *, now: datetime = T0, dry_run: bool = False) -> tuple[int, str]:
        # The project under test has no Cluster Agent profiles (roster_patch), so
        # the sweep enumerates it with gcloud first; this harness answers that
        # listing with one cluster and hands every other sandbox command,
        # i.e. the `leeway report --clusters ...` run, to the test's fake.
        def sandbox(argv, **kw):
            if argv[0] == "gcloud":
                return subprocess.CompletedProcess(args=argv, returncode=0, stdout="seeded-d\tus-central1\n", stderr="")
            return fake_run(argv, **kw)

        buf = io.StringIO()
        with mock.patch.object(watch.sandbox_exec, "run", side_effect=sandbox), redirect_stdout(buf):
            code = watch.tick(dry_run=dry_run, now=now)
        return code, buf.getvalue()

    def test_new_finding_writes_report_and_quiet_tick_within_week_prints_nothing(self) -> None:
        cluster_ok = {
            "cluster": "p1/us-central1/seeded-d",
            "status": "ok",
            "topology_findings": [make_topology_finding()],
            "rank_findings": [make_rank_finding()],
        }
        rep = make_report([cluster_ok], T0)

        code, out = self.run_tick(lambda *a, **kw: completed_json(rep), now=T0)
        self.assertEqual(code, 0)
        self.assertIn(
            "leeway: 2 finding(s) due (2 new finding) across 1 cluster(s) (p1/us-central1/seeded-d): "
            "ComputeClass/balanced-spot [last-rank, 75%], Deployment/shop/checkout-api [constraint_ignored, 100%]",
            out,
        )
        self.assertIn("next refresh after 2026-10-26", out)

        # Verify report files and symlinks exist and contain required fields.
        latest_md = self.home / "reports" / "latest.md"
        latest_json = self.home / "reports" / "latest.json"
        self.assertTrue(latest_md.is_symlink())
        self.assertTrue(latest_json.is_symlink())
        md_text = latest_md.read_text(encoding="utf-8")
        self.assertIn("`shop/Deployment/checkout-api`", md_text)
        self.assertIn("2/2 (100%)", md_text)
        self.assertIn("us-central1-a: 2, us-central1-b: 0", md_text)
        self.assertIn("`constraint_ignored`", md_text)
        self.assertIn("topologySpreadConstraints:", md_text)
        # The rank finding renders from the RankFinding shape: the rule stands
        # in for the cause, the last-rank share for the blast radius.
        self.assertIn("`ComputeClass/balanced-spot`", md_text)
        self.assertIn("`last-rank`", md_text)
        self.assertIn("lastRankShare 75%", md_text)

        # Next day (T0 + 1 day): unchanged findings are not due -> quiet tick.
        t1 = T0 + timedelta(days=1)
        code2, out2 = self.run_tick(lambda *a, **kw: completed_json(rep), now=t1)
        self.assertEqual(code2, 0)
        self.assertEqual(out2, "")

    def test_cause_change_or_severity_escalation_triggers_immediate_report(self) -> None:
        c1 = {
            "cluster": "p1/us-central1/seeded-d",
            "status": "ok",
            "topology_findings": [make_topology_finding(cause="unknown", severity="warning")],
        }
        self.run_tick(lambda *a, **kw: completed_json(make_report([c1], T0)), now=T0)

        # Next day, suspectedCause shifts to volume_pinning -> immediate report.
        t1 = T0 + timedelta(days=1)
        c2 = {
            "cluster": "p1/us-central1/seeded-d",
            "status": "ok",
            "topology_findings": [make_topology_finding(cause="volume_pinning", severity="warning")],
        }
        code, out = self.run_tick(lambda *a, **kw: completed_json(make_report([c2], t1)), now=t1)
        self.assertEqual(code, 0)
        self.assertIn("1 cause/severity change", out)

        # Day 2, severity escalates from warning to critical -> immediate report.
        t2 = T0 + timedelta(days=2)
        c3 = {
            "cluster": "p1/us-central1/seeded-d",
            "status": "ok",
            "topology_findings": [make_topology_finding(cause="volume_pinning", severity="critical")],
        }
        code3, out3 = self.run_tick(lambda *a, **kw: completed_json(make_report([c3], t2)), now=t2)
        self.assertEqual(code3, 0)
        self.assertIn("1 cause/severity change", out3)

        # Day 3, severity drops back to warning -> quiet tick, and baseline severity in ledger stays critical
        # so bouncing back to critical on Day 4 does NOT re-page inside the 7-day suppression window.
        t3 = T0 + timedelta(days=3)
        self.run_tick(lambda *a, **kw: completed_json(make_report([c2], t3)), now=t3)
        t4 = T0 + timedelta(days=4)
        code4, out4 = self.run_tick(lambda *a, **kw: completed_json(make_report([c3], t4)), now=t4)
        self.assertEqual(code4, 0)
        self.assertEqual(out4, "")

    def test_scheduled_refresh_fires_after_seven_days_with_slack(self) -> None:
        c1 = {
            "cluster": "p1/us-central1/seeded-d",
            "status": "ok",
            "topology_findings": [make_topology_finding()],
        }
        self.run_tick(lambda *a, **kw: completed_json(make_report([c1], T0)), now=T0)

        # 7 days minus 5 minutes (inside the 10-minute slack window) -> refreshes!
        t_refresh = T0 + timedelta(days=7) - timedelta(minutes=5)
        code, out = self.run_tick(lambda *a, **kw: completed_json(make_report([c1], t_refresh)), now=t_refresh)
        self.assertEqual(code, 0)
        self.assertIn("1 scheduled refresh", out)

    def test_resolved_finding_is_retired_on_ok_scan_and_retained_on_error_scan(self) -> None:
        c_skewed = {
            "cluster": "p1/us-central1/seeded-d",
            "status": "ok",
            "topology_findings": [make_topology_finding()],
        }
        self.run_tick(lambda *a, **kw: completed_json(make_report([c_skewed], T0)), now=T0)

        # Day 1: cluster scan fails (`status: "error"`). Finding MUST be retained in ledger, not retired!
        t1 = T0 + timedelta(days=1)
        c_error = {
            "cluster": "p1/us-central1/seeded-d",
            "status": "error",
            "error": "kubectl list nodes timed out",
        }
        code_err, out_err = self.run_tick(lambda *a, **kw: completed_json(make_report([c_error], t1)), now=t1)
        self.assertEqual(code_err, 0)
        self.assertIn("leeway watch: the fleet scan was partial (1 error(s))", out_err)
        self.assertNotIn("retired", out_err)
        ledger = watch.load_ledger(self.home)
        self.assertEqual(len(ledger[watch.FINDINGS_KEY]), 1)

        # Day 2: same error -> partial line is not repeated.
        t2 = T0 + timedelta(days=2)
        _, out_err2 = self.run_tick(lambda *a, **kw: completed_json(make_report([c_error], t2)), now=t2)
        self.assertEqual(out_err2, "")

        # Day 3: cluster scan succeeds with zero findings -> partial cleared AND finding retired!
        t3 = T0 + timedelta(days=3)
        c_clean = {
            "cluster": "p1/us-central1/seeded-d",
            "status": "ok",
            "topology_findings": [],
        }
        code_ok, out_ok = self.run_tick(lambda *a, **kw: completed_json(make_report([c_clean], t3)), now=t3)
        self.assertEqual(code_ok, 0)
        self.assertIn("leeway watch: every target that failed reads cleanly again", out_ok)
        self.assertIn("leeway: 1 finding(s) resolved (seeded-d:shop/checkout-api); retired from the watch", out_ok)
        ledger_after = watch.load_ledger(self.home)
        self.assertEqual(ledger_after[watch.FINDINGS_KEY], {})

    def test_tick_failure_marker_suppresses_duplicate_alerts_and_announces_recovery(self) -> None:
        def failing_run(*a, **kw):
            return subprocess.CompletedProcess(args=["leeway"], returncode=1, stdout="", stderr="broker unreachable")

        code1, out1 = self.run_tick(failing_run, now=T0)
        self.assertEqual(code1, 0)
        self.assertIn("leeway watch: the tick failed:", out1)

        # Second failure with identical detail prints nothing.
        code2, out2 = self.run_tick(failing_run, now=T0 + timedelta(days=1))
        self.assertEqual(code2, 0)
        self.assertEqual(out2, "")

        # Recovery prints "the tick runs again".
        c_clean = {"cluster": "p1/us-central1/seeded-d", "status": "ok", "topology_findings": []}
        code3, out3 = self.run_tick(
            lambda *a, **kw: completed_json(make_report([c_clean], T0 + timedelta(days=2))),
            now=T0 + timedelta(days=2),
        )
        self.assertEqual(code3, 0)
        self.assertEqual(out3.strip(), "leeway watch: the tick runs again")

    def test_dry_run_leaves_ledger_and_reports_untouched(self) -> None:
        c_skewed = {
            "cluster": "p1/us-central1/seeded-d",
            "status": "ok",
            "topology_findings": [make_topology_finding()],
        }
        code, out = self.run_tick(
            lambda *a, **kw: completed_json(make_report([c_skewed], T0)),
            now=T0,
            dry_run=True,
        )
        self.assertEqual(code, 0)
        self.assertIn("dry run: would report 1 finding(s) (1 new finding)", out)
        self.assertIn("Deployment/shop/checkout-api [constraint_ignored, 100%]", out)
        self.assertFalse((self.home / watch.LEDGER_FILE_NAME).exists())
        self.assertFalse((self.home / watch.REPORTS_DIR_NAME).exists())

    def test_prune_old_reports_keeps_newest_ten(self) -> None:
        c_skewed = {
            "cluster": "p1/us-central1/seeded-d",
            "status": "ok",
            "topology_findings": [make_topology_finding()],
        }
        rep = make_report([c_skewed], T0)
        for i in range(12):
            moment = T0 + timedelta(days=i * 8)
            watch.write_report_files(self.home, rep, f"# Report {i}\n", moment)
        reports_dir = self.home / watch.REPORTS_DIR_NAME
        md_files = [p for p in reports_dir.glob("*.md") if not p.is_symlink()]
        json_files = [p for p in reports_dir.glob("*.json") if not p.is_symlink()]
        self.assertEqual(len(md_files), watch.REPORTS_KEPT)
        self.assertEqual(len(json_files), watch.REPORTS_KEPT)

    def test_publishes_to_blackboard_topic_when_nats_url_and_a2a_are_present(self) -> None:
        c_skewed = {
            "cluster": "seeded-d",
            "source": "gke:p1/us-central1/seeded-d",
            "status": "ok",
            "topology_findings": [make_topology_finding()],
        }
        rep = make_report([c_skewed], T0)
        a2a_calls: list[list[str]] = []

        def fake_subprocess_run(argv, **kw):
            a2a_calls.append(list(argv))
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        with mock.patch.dict(os.environ, {watch.NATS_URL_ENV: "nats://nats:4222"}, clear=False), mock.patch.object(
            watch.shutil, "which", return_value="/usr/local/bin/a2a"
        ), mock.patch.object(watch.subprocess, "run", side_effect=fake_subprocess_run):
            code, _ = self.run_tick(lambda *a, **kw: completed_json(rep), now=T0)
            self.assertEqual(code, 0)
            self.assertEqual(len(a2a_calls), 1)
            self.assertEqual(a2a_calls[0][:4], ["a2a", "topics", "write", "topology-skew"])
            self.assertIn("--text", a2a_calls[0])
            self.assertIn("--data", a2a_calls[0])

            # When all findings resolve on a clean scan, latest.{md,json} and the blackboard topic
            # are refreshed so readers do not see stale active findings.
            t1 = T0 + timedelta(days=1)
            c_clean = {
                "cluster": "seeded-d",
                "source": "gke:p1/us-central1/seeded-d",
                "status": "ok",
                "topology_findings": [],
            }
            rep_clean = make_report([c_clean], t1)
            code2, out2 = self.run_tick(lambda *a, **kw: completed_json(rep_clean), now=t1)
            self.assertEqual(code2, 0)
            self.assertIn("retired from the watch", out2)
            self.assertEqual(len(a2a_calls), 2)
            latest_json = json.loads((self.home / "reports" / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual(latest_json["summary"]["topology_findings"], 0)
            self.assertEqual(latest_json["clusters"][0]["cluster"], "p1/us-central1/seeded-d")

    def test_corrupt_ledger_refuses_to_overwrite_and_announces_failure_once(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        ledger_path = self.home / watch.LEDGER_FILE_NAME
        ledger_path.write_text("{corrupt json", encoding="utf-8")

        c_clean = {"cluster": "p1/us-central1/seeded-d", "status": "ok", "topology_findings": []}
        code1, out1 = self.run_tick(lambda *a, **kw: completed_json(make_report([c_clean], T0)), now=T0)
        self.assertEqual(code1, 0)
        self.assertIn("refusing to overwrite it", out1)
        self.assertEqual(ledger_path.read_text(encoding="utf-8"), "{corrupt json")

        # Second tick with the same corrupt ledger is suppressed by the failure marker.
        code2, out2 = self.run_tick(
            lambda *a, **kw: completed_json(make_report([c_clean], T0 + timedelta(days=1))),
            now=T0 + timedelta(days=1),
        )
        self.assertEqual(code2, 0)
        self.assertEqual(out2, "")

    def test_normalizers_read_the_fields_leeway_prints(self) -> None:
        # Verbatim `leeway report` (v0.1.3) output for one topology finding and
        # one rank finding, from leeway's own report collector on its test
        # fixtures (pkg/report/report_test.go). The field names here are the
        # contract; a normalizer that reads a field this payload does not
        # carry falls back to its defaults and this test catches it.
        topology = {
            "kind": "leeway.contract_violated",
            "subject": {"kind": "Deployment", "namespace": "shop", "name": "batch-worker"},
            "topologyKey": "topology.kubernetes.io/zone",
            "tier": "A",
            "severity": "critical",
            "score": {
                "drift": 0.5,
                "observedSkew": 1,
                "minAchievableSkew": 0,
                "excessSkew": 3,
                "relocationDistance": 1,
                "maxDomainShare": 1,
            },
            "intent": {
                "source": "topology-spread-constraint",
                "confidence": "declared",
                "weighting": "Equal",
                "maxSkew": 1,
                "evidence": [
                    "topologySpreadConstraints[0]: topologyKey=topology.kubernetes.io/zone maxSkew=1 whenUnsatisfiable=DoNotSchedule labelSelector=app=batch-worker"
                ],
            },
            "domains": [
                {"domain": "us-central1-a", "actual": 0, "expected": 1, "delta": -1, "readyNodes": 1},
                {"domain": "us-central1-b", "actual": 1, "expected": 1, "delta": 0, "readyNodes": 1},
            ],
            "suspectedCause": "domain_capacity_shortfall",
            "contributingFactors": [
                "3 pods Pending with FailedScheduling: 0/2 nodes are available: 1 Insufficient cpu, 1 node(s) didn't match pod topology spread constraints.",
                "0 pods pinned by zonal volumes",
            ],
            "firstSeenAt": "2026-10-19T12:00:00Z",
            "remediation": {
                "kind": "cluster_capacity",
                "summary": "Deficit domain(s) (us-central1-a) lack allocatable capacity to schedule replicas of Deployment/shop/batch-worker; scale node pools in us-central1-a or right-size pod resource requests rather than editing workload spread constraints.",
            },
        }
        norm_t = watch.normalize_topology_finding("p1/us-central1/c1", topology)
        self.assertEqual(
            norm_t["key"],
            "p1/us-central1/c1/shop/Deployment/batch-worker/topology.kubernetes.io/zone/leeway.contract_violated",
        )
        self.assertEqual(norm_t["suspected_cause"], "domain_capacity_shortfall")
        self.assertEqual(norm_t["observed_skew"], 1)
        self.assertEqual(norm_t["max_domain_share"], 1.0)
        self.assertEqual(norm_t["blast_radius_display"], "1/1 (100%)")
        self.assertEqual(norm_t["distribution_display"], "us-central1-a: 0, us-central1-b: 1")
        self.assertEqual(norm_t["remediation"]["kind"], "cluster_capacity")
        self.assertEqual(norm_t["remediation"]["yaml"], "")

        rank = {
            "kind": "leeway.rank_degraded",
            "subject": {"kind": "PreferenceAxis", "name": "gold"},
            "provider": "gke-computeclass",
            "objectKind": "ComputeClass",
            "specHash": "842f2f5b7a0deb21",
            "tier": "B",
            "severity": "warning",
            "rule": "last-rank",
            "observed": {
                "windowSeconds": 3600,
                "tierSeconds": 21600,
                "rank0Share": 0,
                "lastRank": 1,
                "lastRankShare": 1,
                "meanRank": 1,
                "pendingPods": 1,
                "pendingProvisioning": 0,
                "pendingDeclined": 0,
                "degradedPods": 6,
                "improving": 0,
                "worsening": 0,
                "lateral": 0,
            },
            "ranks": [
                {"rank": 0, "rules": "machineFamily=n2", "podSeconds": 0, "share": 0, "pods": 0},
                {"rank": 1, "rules": "machineFamily=e2", "podSeconds": 21600, "share": 1, "pods": 6},
            ],
            "ordering": "list-position",
            "tiers": 2,
            "scaleUp": "unset",
            "optimizeRulePriority": False,
            "reason": "rank 1 share 1.00 is above the ceiling 0.90: running on last-resort capacity, which on most classes is the spot or the cheapest rule",
            "firstSeenAt": "2026-10-16T12:00:00Z",
        }
        norm_r = watch.normalize_rank_finding("p1/us-central1/c1", rank)
        self.assertEqual(norm_r["key"], "p1/us-central1/c1/_cluster/ComputeClass/gold/rule:last-rank/leeway.rank_degraded")
        self.assertEqual(norm_r["subject_name"], "gold")
        self.assertEqual(norm_r["suspected_cause"], "last-rank")
        self.assertEqual(norm_r["observed_skew"], 1)
        self.assertEqual(norm_r["max_domain_share"], 1.0)
        self.assertEqual(norm_r["blast_radius_display"], "lastRankShare 100%, 1 pending")
        self.assertEqual(
            norm_r["distribution_display"],
            "rank 0 (machineFamily=n2): 0%, rank 1 (machineFamily=e2): 100%",
        )
        self.assertEqual(norm_r["contributing_factors"], [rank["reason"]])

    def test_defensive_normalizers_handle_non_numeric_and_null_fields(self) -> None:
        weird_topo = {
            "kind": None,
            "subject": {"kind": None, "namespace": None, "name": None, "replicas": "not-an-int"},
            "topologyKey": None,
            "score": {"maxDomainShare": "not-a-float", "observedSkew": "bad"},
            "domains": [{"domain": None, "actual": "bad"}],
            "contributingFactors": "not-a-list",
        }
        norm_t = watch.normalize_topology_finding("p1/us-central1/c1", weird_topo)
        self.assertEqual(norm_t["observed_skew"], 0)
        self.assertEqual(norm_t["max_domain_share"], 0.0)
        self.assertEqual(norm_t["contributing_factors"], [])

        weird_rank = {
            "kind": None,
            "subject": None,
            "objectKind": None,
            "rule": None,
            "observed": {"lastRank": "bad", "lastRankShare": "NaN", "rank0Share": None},
            "ranks": "not-a-list",
            "reason": "  ",
        }
        norm_r = watch.normalize_rank_finding("p1/us-central1/c1", weird_rank)
        self.assertEqual(norm_r["key"], "p1/us-central1/c1/_cluster/ComputeClass/unknown/compute-class/unknown")
        self.assertEqual(norm_r["observed_skew"], 0)
        self.assertEqual(norm_r["max_domain_share"], 0.0)
        self.assertEqual(norm_r["contributing_factors"], [])
        self.assertEqual(norm_r["suspected_cause"], "unknown")
        self.assertEqual(watch._float("Infinity", 0.0), 0.0)
        self.assertEqual(watch._float(1e308, 0.0), 0.0)
        self.assertEqual(watch._int(float("inf"), 0), 0)

    def test_failing_project_at_full_timeout_is_stamped_so_tail_projects_rotate(self) -> None:
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}, clear=False):
            def fake_run(argv, **kw):
                # argv[-1] is the `--clusters` value: this project's triples.
                if argv[-1].startswith("p1/"):
                    raise subprocess.TimeoutExpired(cmd=argv, timeout=watch.PROJECT_TIMEOUT_SECONDS)
                return completed_json(make_report([{"cluster": "p2/us-central1/seeded-d", "status": "ok"}], T0))

            code, out = self.run_tick(fake_run, now=T0)
            self.assertEqual(code, 0)
            self.assertIn("leeway watch: the fleet scan was partial (1 error(s))", out)
            ledger = watch.load_ledger(self.home)
            self.assertEqual(ledger[watch.PROJECT_RUNS_KEY].get("p1"), watch.iso(T0))
            self.assertEqual(ledger[watch.PROJECT_RUNS_KEY].get("p2"), watch.iso(T0))

    def test_partial_scan_announces_each_failing_target_once(self) -> None:
        def failing(*projects: str):
            def run(argv, **kw):
                if argv[-1].split("/", 1)[0] in projects:
                    return subprocess.CompletedProcess(args=argv, returncode=1, stdout="", stderr="err")
                cluster = argv[-1]
                return completed_json(make_report([{"cluster": cluster, "status": "ok"}], T0))

            return run

        def entry(project: str) -> str:
            return f"{project}: leeway scan for {project} failed: RuntimeError: leeway exited 1 without a report: err"

        partial = "leeway watch: the fleet scan was partial ({count} error(s)): {reasons}; " \
            "findings on unreadable clusters are retained until read again"
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}, clear=False):
            outputs = [
                self.run_tick(outcome, now=T0 + timedelta(days=day))[1].splitlines()
                for day, outcome in enumerate(
                    (failing("p2"), failing("p2"), failing("p1"), failing("p1"), failing())
                )
            ]
        self.assertEqual(
            outputs,
            [
                [partial.format(count=1, reasons=entry("p2"))],
                [],  # the same target failing again is not re-posted
                [partial.format(count=1, reasons=entry("p1"))],  # a new target posts; p2 read cleanly
                [],
                ["leeway watch: every target that failed reads cleanly again"],
            ],
        )

    def test_retires_resolved_finding_in_clusters_mode_and_with_bare_cluster_names(self) -> None:
        with mock.patch.dict(
            os.environ,
            {watch.CLUSTERS_ENV: "p1/us-central1/seeded-d,p1/us-east1/other", watch.PROJECTS_ENV: ""},
            clear=False,
        ):
            # Day 0: report uses bare cluster name "seeded-d" with a finding.
            c_skewed = {
                "cluster": "seeded-d",
                "status": "ok",
                "topology_findings": [make_topology_finding()],
            }
            c_other_ok = {"cluster": "p1/us-east1/other", "status": "ok", "topology_findings": []}
            self.run_tick(lambda *a, **kw: completed_json(make_report([c_skewed, c_other_ok], T0)), now=T0)

            # Day 1: clean scan of `p1/us-central1/seeded-d` while `p1/us-east1/other` errors
            # retires the bare "seeded-d" ledger entry specifically via `bare_cluster in ok_bare`.
            t1 = T0 + timedelta(days=1)
            c_clean = {"cluster": "p1/us-central1/seeded-d", "status": "ok", "topology_findings": []}
            c_other_err = {"cluster": "p1/us-east1/other", "status": "error", "error": "timeout"}
            code, out = self.run_tick(
                lambda *a, **kw: completed_json(make_report([c_clean, c_other_err], t1)), now=t1
            )
            self.assertEqual(code, 0)
            self.assertIn("retired from the watch", out)

        # Verify an explicitly requested target omitted from `report.clusters` is retained (not retired).
        self.assertFalse(
            watch.can_retire_finding(
                "p1/us-central1/seeded-d/ns/Deployment/app/topology.kubernetes.io/zone/leeway.placement_drift",
                {"cluster": "p1/us-central1/seeded-d"},
                ok_clusters=set(),
                cluster_errors={},
                scanned_projects={"p1"},
                scan_errors={},
                explicit_targets={"p1/us-central1/seeded-d"},
            )
        )
        # Also verify cross-project same-bare-name safety: p1/us-central1/prod ok does NOT retire
        # p2/us-east1/prod when p2 failed to scan.
        self.assertFalse(
            watch.can_retire_finding(
                "p2/us-east1/prod/ns/Deployment/app/topology.kubernetes.io/zone/leeway.placement_drift",
                {"cluster": "p2/us-east1/prod"},
                ok_clusters={"p1/us-central1/prod"},
                cluster_errors={},
                scanned_projects={"p1"},
                scan_errors={"p2": "scan failed"},
            )
        )
        # And verify a de-scoped cluster in explicit_targets mode is retired when the sweep is clean.
        self.assertTrue(
            watch.can_retire_finding(
                "p2/us-east1/old/ns/Deployment/app/topology.kubernetes.io/zone/leeway.placement_drift",
                {"cluster": "p2/us-east1/old"},
                ok_clusters={"p1/us-central1/seeded-d"},
                cluster_errors={},
                scanned_projects=set(),
                scan_errors={},
                explicit_targets={"p1/us-central1/seeded-d"},
            )
        )

    def test_floor_equal_unknown_findings_are_suppressed_while_real_skew_and_selector_confined_remain(self) -> None:
        # [0, 1, 1] and [1, 1, 2] have observedSkew == minAchievableSkew == 1 with suspectedCause == "unknown"
        # and are already at the pigeonhole floor; [4, 0] (`observedSkew: 4, minAchievableSkew: 0, excessSkew: 0`)
        # and `selector_confined` (`drift: 0, maxDomainShare: 1.0`) must still be surfaced.
        even_two_of_three = make_topology_finding(
            name="metrics-server",
            namespace="kube-system",
            replicas=2,
            tier="C",
            severity="info",
            cause="unknown",
        )
        even_two_of_three["score"] = {
            "drift": 0.1667,
            "observedSkew": 1,
            "minAchievableSkew": 1,
            "excessSkew": 0,
            "relocationDistance": 0,
            "maxDomainShare": 0.5,
        }
        even_four_of_three = make_topology_finding(
            name="redis",
            namespace="cache",
            replicas=4,
            tier="C",
            severity="info",
            cause="unknown",
        )
        even_four_of_three["score"] = {
            "drift": 0.125,
            "observedSkew": 1,
            "minAchievableSkew": 1,
            "excessSkew": 0,
            "relocationDistance": 0,
            "maxDomainShare": 0.5,
        }
        stacked_default = make_topology_finding(
            name="frontend-web",
            namespace="shop",
            replicas=4,
            tier="B",
            severity="warning",
            cause="unknown",
            actual_a=4,
            actual_b=0,
        )
        stacked_default["score"] = {
            "drift": 0.5,
            "observedSkew": 4,
            "minAchievableSkew": 0,
            "excessSkew": 0,
            "relocationDistance": 2,
            "maxDomainShare": 1.0,
        }
        selector_confined = make_topology_finding(
            name="ledger-sync",
            namespace="finance",
            replicas=3,
            tier="C",
            severity="info",
            cause="selector_confined",
            actual_a=3,
            actual_b=0,
        )
        selector_confined["score"] = {
            "drift": 0.0,
            "observedSkew": 0,
            "minAchievableSkew": 0,
            "excessSkew": 0,
            "relocationDistance": 0,
            "maxDomainShare": 1.0,
        }
        rep = make_report(
            [
                {
                    "cluster": "p1/us-central1/seeded-d",
                    "status": "ok",
                    "topology_findings": [
                        even_two_of_three,
                        even_four_of_three,
                        stacked_default,
                        selector_confined,
                    ],
                }
            ],
            T0,
        )
        code, out = self.run_tick(lambda *a, **kw: completed_json(rep), now=T0)
        self.assertEqual(code, 0)
        self.assertIn("2 finding(s) due", out)
        self.assertIn("Deployment/finance/ledger-sync [selector_confined, 100%]", out)
        self.assertIn("Deployment/shop/frontend-web [unknown, 100%]", out)
        self.assertNotIn("metrics-server", out)
        self.assertNotIn("redis", out)

        latest_json = json.loads((self.home / "reports" / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(latest_json["summary"]["topology_findings"], 2)
        md_text = (self.home / "reports" / "latest.md").read_text(encoding="utf-8")
        self.assertNotIn("metrics-server", md_text)
        self.assertIn("`selector_confined`", md_text)

    def test_threshold_env_overrides_filter_severity_drift_and_share_without_dropping_domain_unavailable(self) -> None:
        domain_down = {
            "kind": "leeway.domain_unavailable",
            "subject": {"kind": "Domain", "name": "us-central1-c"},
            "topologyKey": "topology.kubernetes.io/zone",
            "tier": "A",
            "severity": "warning",
            "score": {
                "drift": 0.0,
                "observedSkew": 0,
                "minAchievableSkew": 0,
                "excessSkew": 0,
                "relocationDistance": 0,
                "maxDomainShare": 0.0,
            },
            "domains": [{"domain": "us-central1-c", "actual": 0, "expected": 0, "delta": 0, "readyNodes": 1}],
            "suspectedCause": "taint_exclusion",
            "remediation": {"kind": "cluster_nodes", "summary": "Uncordon nodes in us-central1-c."},
        }
        info_finding = make_topology_finding(name="info-app", tier="C", severity="info", cause="constraint_ignored")
        info_finding["score"]["drift"] = 0.67
        selector_confined_warn = make_topology_finding(
            name="confined-app",
            replicas=3,
            tier="C",
            severity="warning",
            cause="selector_confined",
            actual_a=3,
            actual_b=0,
        )
        selector_confined_warn["score"]["drift"] = 0.0
        low_drift = make_topology_finding(
            name="low-drift",
            replicas=3,
            tier="B",
            severity="warning",
            cause="constraint_ignored",
            actual_a=3,
            actual_b=0,
        )
        low_drift["score"]["drift"] = 0.2
        low_share = make_topology_finding(
            name="mild-skew",
            replicas=3,
            tier="B",
            severity="warning",
            cause="constraint_ignored",
            actual_a=2,
            actual_b=1,
        )
        low_share["score"]["drift"] = 0.5
        high_share = make_topology_finding(
            name="full-skew",
            replicas=3,
            tier="B",
            severity="warning",
            cause="constraint_ignored",
            actual_a=3,
            actual_b=0,
        )
        high_share["score"]["drift"] = 0.67
        wedged_rank = make_rank_finding(
            compute_class="gpu-spot",
            severity="critical",
            rule="wedged",
            last_rank_share=0.0,
        )
        wedged_rank["kind"] = "leeway.rank_wedged"
        info_rank = make_rank_finding(
            compute_class="unused-tier",
            severity="info",
            rule="tier-unused",
            last_rank_share=0.0,
        )
        info_rank["kind"] = "leeway.rank_tier_unused"
        rep = make_report(
            [
                {
                    "cluster": "p1/us-central1/seeded-d",
                    "status": "ok",
                    "topology_findings": [
                        domain_down,
                        info_finding,
                        selector_confined_warn,
                        low_drift,
                        low_share,
                        high_share,
                    ],
                    "rank_findings": [wedged_rank, info_rank],
                }
            ],
            T0,
        )
        with mock.patch.dict(
            os.environ,
            {
                watch.MIN_SEVERITY_ENV: "warning",
                watch.MIN_DRIFT_ENV: "0.4",
                watch.MIN_MAX_DOMAIN_SHARE_ENV: "0.8",
            },
            clear=False,
        ):
            code, out = self.run_tick(lambda *a, **kw: completed_json(rep), now=T0)
        self.assertEqual(code, 0)
        self.assertIn("4 finding(s) due", out)
        self.assertIn("Domain/us-central1-c [taint_exclusion]", out)
        self.assertIn("Deployment/shop/confined-app [selector_confined, 100%]", out)
        self.assertIn("Deployment/shop/full-skew [constraint_ignored, 100%]", out)
        self.assertIn("ComputeClass/gpu-spot [wedged, 0%]", out)
        self.assertNotIn("info-app", out)
        self.assertNotIn("low-drift", out)
        self.assertNotIn("mild-skew", out)
        self.assertNotIn("unused-tier", out)

    def test_report_line_prefixes_cluster_short_names_across_multiple_clusters_and_truncates_overflow(self) -> None:
        c1_findings = [make_topology_finding(name=f"svc-{i}") for i in range(5)]
        c2_findings = [make_topology_finding(name=f"api-{i}") for i in range(3)]
        rep = make_report(
            [
                {"cluster": "p1/us-central1/alpha", "status": "ok", "topology_findings": c1_findings},
                {"cluster": "p1/us-east1/beta", "status": "ok", "topology_findings": c2_findings},
            ],
            T0,
        )
        with mock.patch.dict(
            os.environ,
            {watch.CLUSTERS_ENV: "p1/us-central1/alpha,p1/us-east1/beta", watch.PROJECTS_ENV: ""},
            clear=False,
        ):
            code, out = self.run_tick(lambda *a, **kw: completed_json(rep), now=T0)
        self.assertEqual(code, 0)
        self.assertIn("8 finding(s) due (8 new finding) across 2 cluster(s)", out)
        self.assertIn("alpha:Deployment/shop/svc-0 [constraint_ignored, 100%]", out)
        self.assertIn("beta:Deployment/shop/api-0 [constraint_ignored, 100%]", out)
        self.assertIn("+2 more", out)

    def test_blackboard_topic_publish_failure_warns_once_and_retries_on_quiet_tick(self) -> None:
        c_skewed = {
            "cluster": "p1/us-central1/seeded-d",
            "status": "ok",
            "topology_findings": [make_topology_finding()],
        }
        rep = make_report([c_skewed], T0)
        outcomes = [
            subprocess.CompletedProcess(
                args=["a2a"],
                returncode=1,
                stdout="",
                stderr="Error:\n  nats: no `TOPICS-STATE` response",
            ),
            subprocess.CompletedProcess(
                args=["a2a"], returncode=1, stdout="", stderr="nats: no response from stream"
            ),
            subprocess.CompletedProcess(args=["a2a"], returncode=0, stdout="", stderr=""),
        ]
        a2a_calls: list[list[str]] = []

        def fake_a2a(argv, **kw):
            a2a_calls.append(list(argv))
            return outcomes.pop(0)

        with mock.patch.dict(os.environ, {watch.NATS_URL_ENV: "nats://nats:4222"}, clear=False), mock.patch.object(
            watch.shutil, "which", return_value="/usr/local/bin/a2a"
        ), mock.patch.object(watch.subprocess, "run", side_effect=fake_a2a):
            # Day 0: finding is due, report writes, topic publish fails -> report line AND single-line warning printed once.
            code0, out0 = self.run_tick(lambda *a, **kw: completed_json(rep), now=T0)
            self.assertEqual(code0, 0)
            lines0 = out0.strip().splitlines()
            self.assertEqual(len(lines0), 2)
            self.assertIn("leeway: 1 finding(s) due", lines0[0])
            self.assertIn("publishing to blackboard topic topology-skew failed", lines0[1])
            self.assertIn("Error: nats: no 'TOPICS-STATE' response", lines0[1])
            self.assertIn("TopicsStateSubjectMissing", lines0[1])
            self.assertIn("nats stream edit TOPICS-STATE", lines0[1])
            self.assertEqual(len(a2a_calls), 1)

            # Day 1: quiet tick retries latest.json and fails again -> no duplicate warning spam.
            t1 = T0 + timedelta(days=1)
            code1, out1 = self.run_tick(lambda *a, **kw: completed_json(rep), now=t1)
            self.assertEqual(code1, 0)
            self.assertEqual(out1, "")
            self.assertEqual(len(a2a_calls), 2)

            # Day 2: operator added the subject to TOPICS-STATE -> quiet tick retry succeeds and announces recovery.
            t2 = T0 + timedelta(days=2)
            code2, out2 = self.run_tick(lambda *a, **kw: completed_json(rep), now=t2)
            self.assertEqual(code2, 0)
            self.assertEqual(out2.strip(), "leeway watch: publishing to blackboard topic topology-skew succeeded again")
            self.assertEqual(len(a2a_calls), 3)
            ledger = watch.load_ledger(self.home)
            self.assertNotIn(watch.ANNOUNCED_KEY, ledger)

        # If a publish failure marker is in ledger.json and NATS_URL is later unconfigured,
        # the next clean quiet tick drops the stale marker without printing "succeeded again".
        ledger_with_marker = watch.load_ledger(self.home)
        ledger_with_marker[watch.ANNOUNCED_KEY] = {watch.ANNOUNCED_TOPIC_PUBLISH_KEY: watch.ERROR_KIND_TOPIC_PUBLISH}
        watch.save_ledger(self.home, ledger_with_marker)
        t3 = T0 + timedelta(days=3)
        code3, out3 = self.run_tick(lambda *a, **kw: completed_json(rep), now=t3)
        self.assertEqual(code3, 0)
        self.assertEqual(out3, "")
        self.assertNotIn(watch.ANNOUNCED_KEY, watch.load_ledger(self.home))

    def test_partial_scan_retirement_defers_report_refresh_and_topic_retry_until_clean_scan(self) -> None:
        rep_day0 = make_report(
            [
                {
                    "cluster": "p1/us-central1/alpha",
                    "status": "ok",
                    "topology_findings": [make_topology_finding()],
                },
                {"cluster": "p1/us-east1/beta", "status": "ok", "topology_findings": []},
            ],
            T0,
        )
        t1 = T0 + timedelta(days=1)
        rep_day1_partial = make_report(
            [
                {"cluster": "p1/us-central1/alpha", "status": "ok", "topology_findings": []},
                {"cluster": "p1/us-east1/beta", "status": "error", "error": "dial tcp timeout"},
            ],
            t1,
        )
        t2 = T0 + timedelta(days=2)
        rep_day2_clean = make_report(
            [
                {"cluster": "p1/us-central1/alpha", "status": "ok", "topology_findings": []},
                {"cluster": "p1/us-east1/beta", "status": "ok", "topology_findings": []},
            ],
            t2,
        )
        outcomes = [
            subprocess.CompletedProcess(args=["a2a"], returncode=1, stdout="", stderr="stream missing"),
            subprocess.CompletedProcess(args=["a2a"], returncode=0, stdout="", stderr=""),
        ]
        published_payloads: list[dict] = []

        def fake_a2a(argv, **kw):
            data_flag = argv.index(watch.A2A_DATA_FLAG)
            json_arg = argv[data_flag + 1].removeprefix(watch.A2A_DATA_FILE_PREFIX)
            published_payloads.append(json.loads(Path(json_arg).read_text(encoding="utf-8")))
            return outcomes.pop(0)

        with mock.patch.dict(
            os.environ,
            {
                watch.CLUSTERS_ENV: "p1/us-central1/alpha,p1/us-east1/beta",
                watch.PROJECTS_ENV: "",
                watch.NATS_URL_ENV: "nats://nats:4222",
            },
            clear=False,
        ), mock.patch.object(watch.shutil, "which", return_value="/usr/local/bin/a2a"), mock.patch.object(
            watch.subprocess, "run", side_effect=fake_a2a
        ):
            code0, _ = self.run_tick(lambda *a, **kw: completed_json(rep_day0), now=T0)
            self.assertEqual(code0, 0)
            self.assertEqual(len(published_payloads), 1)

            # Day 1: alpha's finding retires while beta fails to scan -> do NOT retry stale Day-0 latest.json.
            code1, out1 = self.run_tick(lambda *a, **kw: completed_json(rep_day1_partial), now=t1)
            self.assertEqual(code1, 0)
            self.assertIn("1 finding(s) resolved", out1)
            self.assertIn("the fleet scan was partial (1 error(s))", out1)
            self.assertEqual(len(published_payloads), 1)
            ledger1 = watch.load_ledger(self.home)
            self.assertEqual(
                ledger1[watch.ANNOUNCED_KEY].get(watch.ANNOUNCED_DEFERRED_REFRESH_KEY),
                "all findings resolved",
            )

            # Day 2: clean scan refreshes latest.json with zero findings and publishes the clean snapshot.
            code2, out2 = self.run_tick(lambda *a, **kw: completed_json(rep_day2_clean), now=t2)
            self.assertEqual(code2, 0)
            self.assertEqual(len(published_payloads), 2)
            self.assertEqual(published_payloads[1]["clusters"][0]["topology_findings"], [])
            self.assertIn("every target that failed reads cleanly again", out2)
            self.assertIn("publishing to blackboard topic topology-skew succeeded again", out2)
            ledger2 = watch.load_ledger(self.home)
            self.assertNotIn(watch.ANNOUNCED_KEY, ledger2)

    def test_threshold_filtered_findings_stay_in_ledger_without_false_retirement_or_repage(self) -> None:
        floor_equal = make_topology_finding(
            name="metrics-server",
            namespace="kube-system",
            replicas=2,
            tier="C",
            severity="info",
            cause="unknown",
        )
        floor_equal["score"] = {
            "drift": 0.1667,
            "observedSkew": 1,
            "minAchievableSkew": 1,
            "excessSkew": 0,
            "relocationDistance": 0,
            "maxDomainShare": 0.5,
        }
        warn_skew = make_topology_finding(name="checkout-api", severity="warning", cause="constraint_ignored")
        rep_both = make_report(
            [{"cluster": "p1/us-central1/seeded-d", "status": "ok", "topology_findings": [floor_equal, warn_skew]}],
            T0,
        )

        # Day 0 (pre-upgrade / explicit excess floor 0): both findings enter ledger.json.
        with mock.patch.dict(os.environ, {watch.MIN_EXCESS_OVER_FLOOR_ENV: "0"}, clear=False):
            code0, out0 = self.run_tick(lambda *a, **kw: completed_json(rep_both), now=T0)
        self.assertEqual(code0, 0)
        self.assertIn("2 finding(s) due", out0)

        # Day 1 (upgrade to default floor filter): floor_equal is suppressed by thresholds while still
        # emitted by leeway -> must NOT print a false "resolved" line and stays silently in ledger.json.
        t1 = T0 + timedelta(days=1)
        code1, out1 = self.run_tick(lambda *a, **kw: completed_json(rep_both), now=t1)
        self.assertEqual(code1, 0)
        self.assertEqual(out1, "")
        ledger1 = watch.load_ledger(self.home)
        self.assertEqual(len(ledger1[watch.FINDINGS_KEY]), 2)

        # Day 2 (operator runs manual tick with LEEWAY_WATCH_MIN_SEVERITY=critical without --dry-run):
        # warn_skew is threshold-filtered, so it must NOT retire or lose first_seen/last_report_at.
        t2 = T0 + timedelta(days=2)
        with mock.patch.dict(os.environ, {watch.MIN_SEVERITY_ENV: "critical"}, clear=False):
            code2, out2 = self.run_tick(lambda *a, **kw: completed_json(rep_both), now=t2)
        self.assertEqual(code2, 0)
        self.assertEqual(out2, "")

        # Day 3 (normal cron tick at default thresholds): warn_skew is still within its 7-day window
        # and was preserved in ledger.json, so it does NOT re-page as a new finding.
        t3 = T0 + timedelta(days=3)
        code3, out3 = self.run_tick(lambda *a, **kw: completed_json(rep_both), now=t3)
        self.assertEqual(code3, 0)
        self.assertEqual(out3, "")
        warn_key = next(k for k in watch.load_ledger(self.home)[watch.FINDINGS_KEY] if "checkout-api" in k)
        self.assertEqual(watch.load_ledger(self.home)[watch.FINDINGS_KEY][warn_key][watch.FIRST_SEEN_KEY], watch.iso(T0))

        # Day 4: leeway stops emitting both findings on a clean scan -> now both genuinely retire.
        t4 = T0 + timedelta(days=4)
        rep_empty = make_report([{"cluster": "p1/us-central1/seeded-d", "status": "ok", "topology_findings": []}], t4)
        code4, out4 = self.run_tick(lambda *a, **kw: completed_json(rep_empty), now=t4)
        self.assertEqual(code4, 0)
        self.assertIn("2 finding(s) resolved", out4)
        self.assertEqual(watch.load_ledger(self.home)[watch.FINDINGS_KEY], {})

    def test_due_finding_clears_deferred_report_refresh_marker(self) -> None:
        rep_day0 = make_report(
            [
                {
                    "cluster": "p1/us-central1/alpha",
                    "status": "ok",
                    "topology_findings": [make_topology_finding(name="svc-old")],
                },
                {"cluster": "p1/us-east1/beta", "status": "ok", "topology_findings": []},
            ],
            T0,
        )
        t1 = T0 + timedelta(days=1)
        rep_day1_partial = make_report(
            [
                {"cluster": "p1/us-central1/alpha", "status": "ok", "topology_findings": []},
                {"cluster": "p1/us-east1/beta", "status": "error", "error": "dial tcp timeout"},
            ],
            t1,
        )
        t2 = T0 + timedelta(days=2)
        rep_day2_due = make_report(
            [
                {"cluster": "p1/us-central1/alpha", "status": "ok", "topology_findings": []},
                {
                    "cluster": "p1/us-east1/beta",
                    "status": "ok",
                    "topology_findings": [make_topology_finding(name="svc-new")],
                },
            ],
            t2,
        )
        t3 = T0 + timedelta(days=3)
        rep_day3_quiet = make_report(rep_day2_due["clusters"], t3)
        a2a_calls: list[list[str]] = []

        def fake_a2a(argv, **kw):
            a2a_calls.append(list(argv))
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        with mock.patch.dict(
            os.environ,
            {
                watch.CLUSTERS_ENV: "p1/us-central1/alpha,p1/us-east1/beta",
                watch.PROJECTS_ENV: "",
                watch.NATS_URL_ENV: "nats://nats:4222",
            },
            clear=False,
        ), mock.patch.object(watch.shutil, "which", return_value="/usr/local/bin/a2a"), mock.patch.object(
            watch.subprocess, "run", side_effect=fake_a2a
        ):
            self.run_tick(lambda *a, **kw: completed_json(rep_day0), now=T0)
            self.assertEqual(len(a2a_calls), 1)

            # Day 1: partial-scan retirement sets announced.deferred_report_refresh.
            self.run_tick(lambda *a, **kw: completed_json(rep_day1_partial), now=t1)
            self.assertEqual(
                watch.load_ledger(self.home)[watch.ANNOUNCED_KEY].get(watch.ANNOUNCED_DEFERRED_REFRESH_KEY),
                watch.REASON_ALL_RESOLVED,
            )
            self.assertEqual(len(a2a_calls), 1)

            # Day 2: new due finding writes fresh report, publishes topic, and clears deferred_report_refresh.
            code2, out2 = self.run_tick(lambda *a, **kw: completed_json(rep_day2_due), now=t2)
            self.assertEqual(code2, 0)
            self.assertIn("svc-new", out2)
            self.assertEqual(len(a2a_calls), 2)
            self.assertNotIn(watch.ANNOUNCED_KEY, watch.load_ledger(self.home))

            # Day 3: quiet clean scan makes zero redundant report refreshes or a2a calls.
            code3, out3 = self.run_tick(lambda *a, **kw: completed_json(rep_day3_quiet), now=t3)
            self.assertEqual(code3, 0)
            self.assertEqual(out3, "")
            self.assertEqual(len(a2a_calls), 2)

    def test_blackboard_topic_publish_timeout_surfaces_exception_and_recovers(self) -> None:
        rep = make_report(
            [{"cluster": "p1/us-central1/seeded-d", "status": "ok", "topology_findings": [make_topology_finding()]}],
            T0,
        )
        attempts = 0

        def fake_a2a(argv, **kw):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise subprocess.TimeoutExpired(cmd=argv, timeout=watch.A2A_PUBLISH_TIMEOUT_SECONDS)
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        with mock.patch.dict(os.environ, {watch.NATS_URL_ENV: "nats://nats:4222"}, clear=False), mock.patch.object(
            watch.shutil, "which", return_value="/usr/local/bin/a2a"
        ), mock.patch.object(watch.subprocess, "run", side_effect=fake_a2a):
            code0, out0 = self.run_tick(lambda *a, **kw: completed_json(rep), now=T0)
            self.assertEqual(code0, 0)
            self.assertIn(
                "publishing to blackboard topic topology-skew failed "
                "(TimeoutExpired: a2a topics write topology-skew timed out after 30s)",
                out0,
            )
            warn_line = [ln for ln in out0.splitlines() if "publishing to blackboard topic" in ln][0]
            self.assertNotIn("--text", warn_line)
            self.assertNotIn("--data", warn_line)
            self.assertNotIn("finding(s) due", warn_line)

            t1 = T0 + timedelta(days=1)
            code1, out1 = self.run_tick(lambda *a, **kw: completed_json(rep), now=t1)
            self.assertEqual(code1, 0)
            self.assertEqual(out1.strip(), "leeway watch: publishing to blackboard topic topology-skew succeeded again")

        with mock.patch.dict(os.environ, {watch.NATS_URL_ENV: "nats://nats:4222"}, clear=False), mock.patch.object(
            watch.shutil, "which", return_value="/usr/local/bin/a2a"
        ), mock.patch.object(
            watch.subprocess,
            "run",
            side_effect=OSError("bad `socket`\n" + ("x" * 400)),
        ):
            ok, detail = watch.publish_blackboard_topic("summary", self.home / "r.json")
            self.assertFalse(ok)
            assert detail is not None
            self.assertTrue(detail.startswith("OSError: bad 'socket' "))
            self.assertNotIn("\n", detail)
            self.assertNotIn("`", detail)
            self.assertLessEqual(len(detail), len("OSError: ") + watch.STDERR_EXCERPT_CHARS)

    def test_unknown_cause_without_score_and_unrecognized_severity_pass_default_thresholds(self) -> None:
        no_score_concentrated = {
            "kind": "leeway.placement_drift",
            "subject": {"kind": "Deployment", "namespace": "shop", "name": "legacy-api", "replicas": 4},
            "topologyKey": "topology.kubernetes.io/zone",
            "tier": "C",
            "severity": "urgent",
            "suspectedCause": "unknown",
            "domains": [
                {"domain": "us-central1-a", "actual": 4},
                {"domain": "us-central1-b", "actual": 0},
            ],
        }
        novel_rank = make_rank_finding(compute_class="novel-class", severity="high", rule="last-rank")
        rep = make_report(
            [
                {
                    "cluster": "p1/us-central1/seeded-d",
                    "status": "ok",
                    "topology_findings": [no_score_concentrated],
                    "rank_findings": [novel_rank],
                }
            ],
            T0,
        )
        code, out = self.run_tick(lambda *a, **kw: completed_json(rep), now=T0)
        self.assertEqual(code, 0)
        self.assertIn("2 finding(s) due", out)
        self.assertIn("Deployment/shop/legacy-api [unknown, 100%]", out)
        self.assertIn("ComputeClass/novel-class [last-rank, 75%]", out)

        # Even when non-zero drift/share/excess floors are active, a finding that omits
        # score and replica/domain counts (`has_drift=False, has_max_domain_share=False, excess_over_floor=None`)
        # is not dropped by metric gates that have no metric to compare against.
        bare_norm = watch.normalize_topology_finding(
            "p1/us-central1/seeded-d",
            {
                "kind": "leeway.placement_drift",
                "subject": {"kind": "Deployment", "namespace": "shop", "name": "bare", "replicas": 0},
                "topologyKey": "topology.kubernetes.io/zone",
                "tier": "C",
                "severity": "urgent",
                "suspectedCause": "unknown",
                "domains": [],
            },
        )
        self.assertFalse(bare_norm[watch.HAS_DRIFT_KEY])
        self.assertFalse(bare_norm[watch.HAS_MAX_DOMAIN_SHARE_KEY])
        self.assertIsNone(bare_norm[watch.EXCESS_OVER_FLOOR_KEY])
        self.assertTrue(
            watch.passes_topology_thresholds(
                bare_norm,
                min_sev_rank=watch.SEVERITY_RANKS["warning"],
                min_d=0.4,
                min_share=0.8,
                min_excess=1,
            )
        )

    def test_rebalancing_skew_down_to_pigeonhole_floor_retires_and_realerts_on_restack(self) -> None:
        stacked = make_topology_finding(name="kube-dns", namespace="kube-system", cause="unknown", severity="warning")
        stacked["subject"]["replicas"] = 2
        stacked["domains"] = [
            {"domain": "us-central1-a", "actual": 2, "expected": 1},
            {"domain": "us-central1-b", "actual": 0, "expected": 1},
            {"domain": "us-central1-f", "actual": 0, "expected": 0},
        ]
        stacked["score"] = {
            "drift": 0.5,
            "observedSkew": 2,
            "minAchievableSkew": 1,
            "excessSkew": 1,
            "relocationDistance": 1,
            "maxDomainShare": 1.0,
        }
        rebalanced = json.loads(json.dumps(stacked))
        rebalanced["domains"] = [
            {"domain": "us-central1-a", "actual": 1, "expected": 1},
            {"domain": "us-central1-b", "actual": 1, "expected": 1},
            {"domain": "us-central1-f", "actual": 0, "expected": 0},
        ]
        rebalanced["score"] = {
            "drift": 0.0,
            "observedSkew": 1,
            "minAchievableSkew": 1,
            "excessSkew": 0,
            "relocationDistance": 0,
            "maxDomainShare": 0.5,
        }

        rep0 = make_report([{"cluster": "p1/us-central1/ap-mcp", "status": "ok", "topology_findings": [stacked]}], T0)
        code0, out0 = self.run_tick(lambda *a, **kw: completed_json(rep0), now=T0)
        self.assertEqual(code0, 0)
        self.assertIn("Deployment/kube-system/kube-dns [unknown, 100%]", out0)

        t1 = T0 + timedelta(days=1)
        rep1 = make_report([{"cluster": "p1/us-central1/ap-mcp", "status": "ok", "topology_findings": [rebalanced]}], t1)
        code1, out1 = self.run_tick(lambda *a, **kw: completed_json(rep1), now=t1)
        self.assertEqual(code1, 0)
        self.assertIn("1 finding(s) resolved (ap-mcp:kube-system/kube-dns)", out1)
        self.assertEqual(watch.load_ledger(self.home)[watch.FINDINGS_KEY], {})
        latest1 = json.loads((self.home / watch.REPORTS_DIR_NAME / watch.LATEST_JSON_LINK_NAME).read_text())
        self.assertEqual(latest1["summary"]["topology_findings"], 0)

        t2 = T0 + timedelta(days=2)
        rep2 = make_report([{"cluster": "p1/us-central1/ap-mcp", "status": "ok", "topology_findings": [stacked]}], t2)
        code2, out2 = self.run_tick(lambda *a, **kw: completed_json(rep2), now=t2)
        self.assertEqual(code2, 0)
        self.assertIn("1 finding(s) due (1 new finding)", out2)
        self.assertIn("Deployment/kube-system/kube-dns [unknown, 100%]", out2)

        # Pre-existing ledger fallback: strip `excess_over_floor` from the ledger entry (matching
        # the pre-upgrade ledger schema that stored only `observed_skew: 2`) and confirm rebalancing
        # to `observedSkew: 1, minAchievableSkew: 1` still retires the finding via `observed_skew` comparison.
        legacy_ledger = watch.load_ledger(self.home)
        for entry in legacy_ledger[watch.FINDINGS_KEY].values():
            entry.pop(watch.EXCESS_OVER_FLOOR_KEY, None)
        watch.save_ledger(self.home, legacy_ledger)

        t3 = T0 + timedelta(days=3)
        rep3 = make_report([{"cluster": "p1/us-central1/ap-mcp", "status": "ok", "topology_findings": [rebalanced]}], t3)
        code3, out3 = self.run_tick(lambda *a, **kw: completed_json(rep3), now=t3)
        self.assertEqual(code3, 0)
        self.assertIn("1 finding(s) resolved (ap-mcp:kube-system/kube-dns)", out3)
        self.assertEqual(watch.load_ledger(self.home)[watch.FINDINGS_KEY], {})

        # Non-`unknown` cause (`prior_cause != "unknown"`) at `observedSkew == minAchievableSkew == 1`
        # that resolves into a balanced `unknown` floor-equal finding also retires cleanly.
        constrained_odd = json.loads(json.dumps(rebalanced))
        constrained_odd["suspectedCause"] = "constraint_ignored"
        constrained_odd["score"]["drift"] = 0.25
        t4 = T0 + timedelta(days=4)
        rep4 = make_report(
            [{"cluster": "p1/us-central1/ap-mcp", "status": "ok", "topology_findings": [constrained_odd]}], t4
        )
        code4, out4 = self.run_tick(lambda *a, **kw: completed_json(rep4), now=t4)
        self.assertEqual(code4, 0)
        self.assertIn("Deployment/kube-system/kube-dns [constraint_ignored, 50%]", out4)

        t5 = T0 + timedelta(days=5)
        rep5 = make_report([{"cluster": "p1/us-central1/ap-mcp", "status": "ok", "topology_findings": [rebalanced]}], t5)
        code5, out5 = self.run_tick(lambda *a, **kw: completed_json(rep5), now=t5)
        self.assertEqual(code5, 0)
        self.assertIn("1 finding(s) resolved (ap-mcp:kube-system/kube-dns)", out5)
        self.assertEqual(watch.load_ledger(self.home)[watch.FINDINGS_KEY], {})

    def test_non_dry_run_threshold_override_report_refreshes_on_next_default_tick(self) -> None:
        warn_skew = make_topology_finding(name="checkout-api", severity="warning", cause="constraint_ignored")
        crit_skew = make_topology_finding(name="payments-db", severity="critical", cause="volume_pinned")
        rep_day0 = make_report(
            [{"cluster": "p1/us-central1/seeded-d", "status": "ok", "topology_findings": [warn_skew]}],
            T0,
        )
        t1 = T0 + timedelta(days=1)
        rep_day1 = make_report(
            [{"cluster": "p1/us-central1/seeded-d", "status": "ok", "topology_findings": [warn_skew, crit_skew]}],
            t1,
        )
        t2 = T0 + timedelta(days=2)
        rep_day2 = make_report(rep_day1["clusters"], t2)
        a2a_calls: list[list[str]] = []

        def fake_a2a(argv, **kw):
            a2a_calls.append(list(argv))
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        with mock.patch.dict(os.environ, {watch.NATS_URL_ENV: "nats://nats:4222"}, clear=False), mock.patch.object(
            watch.shutil, "which", return_value="/usr/local/bin/a2a"
        ), mock.patch.object(watch.subprocess, "run", side_effect=fake_a2a):
            self.run_tick(lambda *a, **kw: completed_json(rep_day0), now=T0)
            self.assertEqual(len(a2a_calls), 1)

            # Day 1: operator omits --dry-run with MIN_SEVERITY=critical when crit_skew is due.
            with mock.patch.dict(os.environ, {watch.MIN_SEVERITY_ENV: "critical"}, clear=False):
                code1, out1 = self.run_tick(lambda *a, **kw: completed_json(rep_day1), now=t1)
            self.assertEqual(code1, 0)
            self.assertIn("payments-db", out1)
            latest_override = json.loads((self.home / watch.REPORTS_DIR_NAME / watch.LATEST_JSON_LINK_NAME).read_text())
            self.assertEqual(latest_override["summary"]["topology_findings"], 1)
            self.assertEqual(len(a2a_calls), 2)

            # Day 2: default cron tick has no due/retired findings, yet refreshes latest.json and topic to full set.
            code2, out2 = self.run_tick(lambda *a, **kw: completed_json(rep_day2), now=t2)
            self.assertEqual(code2, 0)
            self.assertEqual(out2, "")
            latest_restored = json.loads((self.home / watch.REPORTS_DIR_NAME / watch.LATEST_JSON_LINK_NAME).read_text())
            self.assertEqual(latest_restored["summary"]["topology_findings"], 2)
            self.assertEqual(len(a2a_calls), 3)
            self.assertNotIn(watch.ANNOUNCED_KEY, watch.load_ledger(self.home))


class DiscoveryTests(unittest.TestCase):
    def test_project_with_roster_profile_triples_uses_clusters_flag(self) -> None:
        seen_argv: list[list[str]] = []

        def fake_run(argv, **kw):
            seen_argv.append(list(argv))
            return completed_json(make_report([{"cluster": "p1/us-central1/c1", "status": "ok"}]))

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                watch.WATCH_HOME_ENV: tmp,
                watch.CLUSTERS_ENV: "",
                watch.PROJECTS_ENV: "p1",
            },
            clear=False,
        ), mock.patch.object(
            watch,
            "roster_clusters_by_project",
            return_value={"p1": ["p1/us-central1/c1", "p1/us-east1/c2"]},
        ), mock.patch.object(watch.sandbox_exec, "run", side_effect=fake_run):
            watch.tick(now=T0)

        self.assertEqual(
            seen_argv,
            [
                [
                    "leeway",
                    "report",
                    "--topology-tier-c-signals",
                    "--parallelism",
                    "2",
                    "--timeout",
                    "300s",
                    "--clusters",
                    "p1/us-central1/c1,p1/us-east1/c2",
                ]
            ],
        )
    def test_project_without_profiles_is_enumerated_with_gcloud_and_swept_by_triples(self) -> None:
        # `--clusters-from <project>` is never used: in the sandbox it would go
        # to the GKE API over a proxy route the api_policy does not relay. The
        # project's clusters come from `gcloud container clusters list`, and
        # the sweep is a `--clusters` run over the triples that returns.
        seen_argv: list[list[str]] = []

        def fake_run(argv, **kw):
            seen_argv.append(list(argv))
            if argv[0] == "gcloud":
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout="west\tus-west1\neast\tus-east1-b\n\n", stderr=""
                )
            return completed_json(make_report([{"cluster": "p1/us-west1/west", "status": "ok"}]))

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {watch.WATCH_HOME_ENV: tmp, watch.CLUSTERS_ENV: "", watch.PROJECTS_ENV: "p1"},
            clear=False,
        ), mock.patch.object(watch, "roster_clusters_by_project", return_value={}), mock.patch.object(
            watch.sandbox_exec, "run", side_effect=fake_run
        ):
            watch.tick(now=T0)

        self.assertEqual(
            seen_argv,
            [
                ["gcloud", "container", "clusters", "list", "--format=value(name,location)", "--project", "p1"],
                [
                    "leeway",
                    "report",
                    "--topology-tier-c-signals",
                    "--parallelism",
                    "2",
                    "--timeout",
                    "300s",
                    "--clusters",
                    "p1/us-east1-b/east,p1/us-west1/west",
                ],
            ],
        )
        self.assertNotIn("--clusters-from", " ".join(" ".join(a) for a in seen_argv))

    def test_project_with_no_clusters_is_a_clean_read_not_an_error(self) -> None:
        seen_argv: list[list[str]] = []

        def fake_run(argv, **kw):
            seen_argv.append(list(argv))
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {watch.WATCH_HOME_ENV: tmp, watch.CLUSTERS_ENV: "", watch.PROJECTS_ENV: "p1"},
            clear=False,
        ), mock.patch.object(watch, "roster_clusters_by_project", return_value={}), mock.patch.object(
            watch.sandbox_exec, "run", side_effect=fake_run
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = watch.tick(now=T0)
            ledger = watch.load_ledger(Path(tmp))
        self.assertEqual(code, 0)
        self.assertEqual(buf.getvalue(), "")
        self.assertEqual([a[0] for a in seen_argv], ["gcloud"])
        # The project is stamped as swept even though leeway had nothing to read.
        self.assertEqual(ledger[watch.PROJECT_RUNS_KEY].get("p1"), watch.iso(T0))

    def test_explicit_clusters_env_passes_clusters_flag(self) -> None:
        seen_argv: list[list[str]] = []

        def fake_run(argv, **kw):
            seen_argv.append(list(argv))
            return completed_json(make_report([{"cluster": "p1/us-central1/c1", "status": "ok"}]))

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                watch.WATCH_HOME_ENV: tmp,
                watch.CLUSTERS_ENV: "p1/us-central1/c1, p2/us-east1/c2",
            },
            clear=False,
        ), mock.patch.object(watch.sandbox_exec, "run", side_effect=fake_run):
            watch.tick(now=T0)

        self.assertEqual(
            seen_argv,
            [
                [
                    "leeway",
                    "report",
                    "--topology-tier-c-signals",
                    "--parallelism",
                    "2",
                    "--timeout",
                    "300s",
                    "--clusters",
                    "p1/us-central1/c1,p2/us-east1/c2",
                ]
            ],
        )

    def test_fails_closed_when_no_project_or_cluster_configured(self) -> None:
        def fake_gcloud(argv, **kw):
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                watch.WATCH_HOME_ENV: tmp,
                watch.CLUSTERS_ENV: "",
                watch.PROJECTS_ENV: "",
                watch.MANAGEMENT_PROJECT_ENV: "",
            },
            clear=False,
        ), mock.patch.object(watch, "roster_projects", return_value=set()), mock.patch.object(
            watch.sandbox_exec, "run", side_effect=fake_gcloud
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = watch.tick(now=T0)
        self.assertEqual(code, 0)
        self.assertIn("no GCP project", buf.getvalue())

    def test_declared_scope_resolves_projects_and_drops_out_of_scope_roster(self) -> None:
        fake_scope = mock.Mock(
            projects=("p-scoped",),
            excluded_clusters=(),
        )
        with mock.patch.object(watch, "declared_scope", return_value=fake_scope), mock.patch.object(
            watch, "roster_projects", return_value={"p-out-of-scope"}
        ):
            self.assertEqual(watch.projects(), ["p-scoped"])

    def test_declared_scope_drops_excluded_clusters_from_sweep(self) -> None:
        fake_scope = mock.Mock(
            projects=("p-scoped",),
            excluded_clusters=(("p-scoped", "us-central1", "c-excluded"),),
        )
        seen_argv: list[list[str]] = []

        def fake_run(argv, **kw):
            seen_argv.append(list(argv))
            return completed_json(make_report([{"cluster": "p-scoped/us-central1/c-included", "status": "ok"}]))

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                watch.WATCH_HOME_ENV: tmp,
                watch.CLUSTERS_ENV: "",
                watch.PROJECTS_ENV: "",
            },
            clear=False,
        ), mock.patch.object(watch, "declared_scope", return_value=fake_scope), mock.patch.object(
            watch, "roster_clusters_by_project", return_value={
                "p-scoped": ["p-scoped/us-central1/c-included", "p-scoped/us-central1/c-excluded"]
            }
        ), mock.patch.object(
            watch.sandbox_exec, "run", side_effect=fake_run
        ):
            code = watch.tick(now=T0)
        self.assertEqual(code, 0)
        self.assertEqual(
            seen_argv,
            [
                [
                    "leeway",
                    "report",
                    "--topology-tier-c-signals",
                    "--parallelism",
                    "2",
                    "--timeout",
                    "300s",
                    "--clusters",
                    "p-scoped/us-central1/c-included",
                ]
            ],
        )

    def test_declared_scope_drops_excluded_clusters_from_explicit_clusters_flag(self) -> None:
        fake_scope = mock.Mock(
            projects=("p1",),
            excluded_clusters=(("p1", "us-central1", "c-excluded"),),
        )
        seen_argv: list[list[str]] = []

        def fake_run(argv, **kw):
            seen_argv.append(list(argv))
            return completed_json(make_report([{"cluster": "p1/us-central1/c-ok", "status": "ok"}]))

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                watch.WATCH_HOME_ENV: tmp,
                watch.CLUSTERS_ENV: "p1/us-central1/c-ok, p1/us-central1/c-excluded",
            },
            clear=False,
        ), mock.patch.object(watch, "declared_scope", return_value=fake_scope), mock.patch.object(
            watch.sandbox_exec, "run", side_effect=fake_run
        ):
            code = watch.tick(now=T0)
        self.assertEqual(code, 0)
        self.assertEqual(
            seen_argv,
            [
                [
                    "leeway",
                    "report",
                    "--topology-tier-c-signals",
                    "--parallelism",
                    "2",
                    "--timeout",
                    "300s",
                    "--clusters",
                    "p1/us-central1/c-ok",
                ]
            ],
        )

    def test_unreadable_declared_scope_fails_tick_via_record_tick_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                watch.WATCH_HOME_ENV: tmp,
                watch.CLUSTERS_ENV: "",
                watch.PROJECTS_ENV: "",
            },
            clear=False,
        ), mock.patch.object(
            watch.fleet_scope_targets,
            "declared_scope_targets",
            side_effect=watch.fleet_scope_targets.ScopeRenderUnreadable("unreadable json"),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = watch.tick(now=T0)
        self.assertEqual(code, 0)
        output = buf.getvalue()
        self.assertIn("failed", output)
        self.assertIn("ScopeRenderUnreadable: unreadable json", output)


class ClusterTimeoutTests(unittest.TestCase):
    def test_one_wave_gets_the_whole_call_less_overhead(self) -> None:
        # Two clusters read in parallel finish in one wave.
        # Capped: one or two clusters would otherwise get 870 s each.
        self.assertEqual(watch.cluster_timeout_seconds(900, 1), watch.MAX_CLUSTER_TIMEOUT_SECONDS)
        self.assertEqual(watch.cluster_timeout_seconds(900, 2), watch.MAX_CLUSTER_TIMEOUT_SECONDS)
        self.assertEqual(watch.cluster_timeout_seconds(390, 1), 300)
        self.assertEqual(watch.cluster_timeout_seconds(200, 1), 110)

    def test_waves_split_the_call(self) -> None:
        self.assertEqual(watch.cluster_timeout_seconds(900, 3), watch.MAX_CLUSTER_TIMEOUT_SECONDS)
        self.assertEqual(watch.cluster_timeout_seconds(900, 10), 162)
        self.assertEqual(watch.cluster_timeout_seconds(600, 5), 170)

    def test_never_below_leeways_own_default(self) -> None:
        self.assertEqual(watch.cluster_timeout_seconds(900, 100), watch.MIN_CLUSTER_TIMEOUT_SECONDS)
        self.assertEqual(watch.cluster_timeout_seconds(60, 1), watch.MIN_CLUSTER_TIMEOUT_SECONDS)

    def test_the_smallest_started_call_of_one_wave_lets_the_cluster_deadline_fire_first(self) -> None:
        # The smallest call sweep_fleet starts leaves room for one cluster's
        # floor and leeway's overhead inside the sandbox timeout.
        smallest = watch.MIN_PROJECT_RUN_SECONDS
        self.assertGreaterEqual(smallest - watch.LEEWAY_OVERHEAD_SECONDS, watch.cluster_timeout_seconds(smallest, 1))

    def test_run_passes_the_timeout_sized_to_the_call(self) -> None:
        seen_argv: list[list[str]] = []

        def fake_run(argv, **kw):
            seen_argv.append(list(argv))
            return completed_json(make_report([{"cluster": "p/l/a", "status": "ok"}]))

        with mock.patch.object(watch.sandbox_exec, "run", side_effect=fake_run):
            watch.run_leeway_report(watch.CLUSTERS_FLAG, "p/l/a,p/l/b,p/l/c", timeout=300)

        argv = seen_argv[0]
        self.assertEqual(argv[argv.index(watch.CLUSTER_TIMEOUT_FLAG) + 1], "105s")
        self.assertEqual(argv[-2:], [watch.CLUSTERS_FLAG, "p/l/a,p/l/b,p/l/c"])

    def test_budget_cut_call_shrinks_the_cluster_timeout(self) -> None:
        # Late in the tick the call's timeout is the remaining budget, and the
        # per-cluster deadline follows it down.
        seen_argv: list[list[str]] = []

        def fake_run(argv, **kw):
            seen_argv.append(list(argv))
            return completed_json(make_report([{"cluster": "p1/us-central1/c1", "status": "ok"}]))

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {watch.WATCH_HOME_ENV: tmp, watch.CLUSTERS_ENV: "p1/us-central1/c1", watch.PROJECTS_ENV: ""},
            clear=False,
        ), mock.patch.object(watch.sandbox_exec, "run", side_effect=fake_run), mock.patch.object(
            watch.time, "monotonic", return_value=0.0
        ):
            watch.sweep_fleet({}, T0, deadline=250.0)

        argv = seen_argv[0]
        self.assertEqual(argv[argv.index(watch.CLUSTER_TIMEOUT_FLAG) + 1], "160s")

    def test_a_call_starts_only_with_the_floor_plus_overhead_left(self) -> None:
        # One second short of MIN_PROJECT_RUN_SECONDS the tick budget is spent
        # and leeway is not run; at it, the call runs with the conservative
        # read time.
        for left, runs in ((watch.MIN_PROJECT_RUN_SECONDS - 1, False), (watch.MIN_PROJECT_RUN_SECONDS, True)):
            with self.subTest(left=left):
                seen_argv: list[list[str]] = []

                def fake_run(argv, **kw):
                    seen_argv.append(list(argv))
                    return completed_json(make_report([{"cluster": "p1/us-central1/c1", "status": "ok"}]))

                with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
                    os.environ,
                    {watch.WATCH_HOME_ENV: tmp, watch.CLUSTERS_ENV: "p1/us-central1/c1", watch.PROJECTS_ENV: ""},
                    clear=False,
                ), mock.patch.object(watch.sandbox_exec, "run", side_effect=fake_run), mock.patch.object(
                    watch.time, "monotonic", return_value=0.0
                ):
                    if runs:
                        watch.sweep_fleet({}, T0, deadline=float(left))
                    else:
                        with self.assertRaises(RuntimeError):
                            watch.sweep_fleet({}, T0, deadline=float(left))

                if runs:
                    argv = seen_argv[0]
                    self.assertEqual(
                        argv[argv.index(watch.CLUSTER_TIMEOUT_FLAG) + 1], f"{watch.CONSERVATIVE_CLUSTER_READ_SECONDS}s"
                    )
                else:
                    self.assertEqual(seen_argv, [])


def triples(project: str, count: int) -> list[str]:
    return [f"{project}/l/c{i:02d}" for i in range(count)]


def clusters_arg(argv: list[str]) -> list[str]:
    return argv[argv.index(watch.CLUSTERS_FLAG) + 1].split(",")


def hangs(*clusters: str):
    """An outcome whose call times out when it holds any of `clusters`."""

    def outcome(batch, timeout):
        if set(batch) & set(clusters):
            raise subprocess.TimeoutExpired(cmd="leeway", timeout=timeout)
        return None

    return outcome


def fails(project: str, stderr: str = "boom"):
    """An outcome whose call exits 1 without a report when it holds a cluster of `project`."""

    def outcome(batch, _):
        if batch[0].startswith(project + "/"):
            return subprocess.CompletedProcess([], 1, "", stderr)
        return None

    return outcome


class BatchTests(unittest.TestCase):
    """#6370: a project's clusters (and LEEWAY_WATCH_CLUSTERS) are read in
    calls of at most CLUSTERS_PER_BATCH, so a slow call costs only its batch;
    a project's turn ends at its first sandbox timeout; and failures are
    announced per target."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "leeway"
        self.env = mock.patch.dict(
            os.environ,
            {watch.WATCH_HOME_ENV: str(self.home), watch.CLUSTERS_ENV: "", watch.PROJECTS_ENV: "p1",
             watch.NATS_URL_ENV: "", watch.MANAGEMENT_PROJECT_ENV: ""},
            clear=False,
        )
        self.env.start()
        self.roster = {"p1": triples("p1", 20)}
        self.roster_patch = mock.patch.object(watch, "roster_clusters_by_project", side_effect=lambda: self.roster)
        self.roster_patch.start()
        self.clock = 0.0
        self.clock_patch = mock.patch.object(watch.time, "monotonic", side_effect=lambda: self.clock)
        self.clock_patch.start()
        self.calls: list[tuple[list[str], int]] = []
        self.ok: set[str] = set()

    def tearDown(self) -> None:
        self.clock_patch.stop()
        self.roster_patch.stop()
        self.env.stop()
        self.tmp.cleanup()

    def sandbox(self, outcome=None, *, seconds: float = 0.0, findings: dict | None = None):
        """A sandbox whose `leeway report` returns an ok row per requested cluster
        (with `findings[cluster]` when given), unless `outcome(clusters, timeout)`
        raises or returns a CompletedProcess. A call advances the clock by
        `seconds`, or by its timeout when it times out; `self.ok` collects the
        clusters returned as ok rows."""

        def run(argv, **kw):
            clusters = clusters_arg(argv)
            self.calls.append((list(argv), kw["timeout"]))
            try:
                result = outcome(clusters, kw["timeout"]) if outcome is not None else None
            except subprocess.TimeoutExpired:
                self.clock += kw["timeout"]
                raise
            self.clock += seconds
            if result is not None:
                rows = json.loads(result.stdout).get("clusters", []) if result.stdout else []
                self.ok.update(r["cluster"] for r in rows if r.get("status") == "ok")
                return result
            self.ok.update(clusters)
            rows = [
                {"cluster": c, "status": "ok", "topology_findings": [f] if (f := (findings or {}).get(c)) else []}
                for c in clusters
            ]
            return completed_json(make_report(rows))

        return mock.patch.object(watch.sandbox_exec, "run", side_effect=run)

    def sweep(self, ledger: dict, cluster_runs: dict, deadline: float):
        return watch.sweep_fleet(ledger, T0, deadline, cluster_runs)

    def tick(self, day: int, outcome=None, *, seconds: float = 0.0, findings: dict | None = None) -> list[str]:
        self.clock = 0.0
        self.calls.clear()
        self.ok.clear()
        buf = io.StringIO()
        with self.sandbox(outcome, seconds=seconds, findings=findings), redirect_stdout(buf):
            watch.tick(now=T0 + timedelta(days=day))
        return buf.getvalue().splitlines()

    def write_ledger(self, finding_clusters: list[str] = (), **extra) -> None:
        findings = {}
        for cluster in finding_clusters:
            item = watch.normalize_topology_finding(cluster, make_topology_finding(name="web"))
            findings[item["key"]] = {
                k: item[k] for k in ("cluster", "kind", "namespace", "subject_kind", "subject_name",
                                     "topology_key", "tier", "severity", "suspected_cause")
            } | {watch.FIRST_SEEN_KEY: watch.iso(T0), watch.LAST_REPORT_KEY: watch.iso(T0)}
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / watch.LEDGER_FILE_NAME).write_text(
            json.dumps({watch.FINDINGS_KEY: findings, **extra}), encoding="utf-8"
        )

    def finding_clusters(self) -> list[str]:
        return sorted(f["cluster"] for f in watch.load_ledger(self.home)[watch.FINDINGS_KEY].values())

    def partial(self, count: int, *entries: str) -> str:
        return (f"leeway watch: the fleet scan was partial ({count} error(s)): {', '.join(entries)}; "
                "findings on unreadable clusters are retained until read again")

    # --- batching ---------------------------------------------------------

    def test_the_batch_size_fits_one_call_at_the_conservative_read_time(self) -> None:
        size = watch.CLUSTERS_PER_BATCH
        self.assertEqual(size, 12)
        self.assertEqual(size % watch.PARALLELISM, 0)
        per_cluster = watch.cluster_timeout_seconds(watch.PROJECT_TIMEOUT_SECONDS, size)
        self.assertGreaterEqual(per_cluster, watch.CONSERVATIVE_CLUSTER_READ_SECONDS)
        waves = size // watch.PARALLELISM
        self.assertLessEqual(waves * per_cluster + watch.LEEWAY_OVERHEAD_SECONDS, watch.PROJECT_TIMEOUT_SECONDS)
        # The last deadline of a full batch ends well before the sandbox kills
        # the call: more than twice the 30 s start-up alone can take.
        self.assertGreaterEqual(watch.PROJECT_TIMEOUT_SECONDS - waves * per_cluster, 60)
        # The largest such batch: one more wave would fall below the read time.
        self.assertLess(
            watch.cluster_timeout_seconds(watch.PROJECT_TIMEOUT_SECONDS, size + watch.PARALLELISM),
            watch.CONSERVATIVE_CLUSTER_READ_SECONDS,
        )

    def test_every_started_call_fits_its_deadlines_at_every_budget(self) -> None:
        for remaining in range(watch.MIN_PROJECT_RUN_SECONDS, watch.PROJECT_TIMEOUT_SECONDS + 1):
            size = watch.batch_size(remaining)
            for count in range(1, size + 1):
                per_cluster = watch.cluster_timeout_seconds(remaining, count)
                waves = -(-count // watch.PARALLELISM)
                self.assertGreaterEqual(per_cluster, watch.CONSERVATIVE_CLUSTER_READ_SECONDS, (remaining, count))
                self.assertLessEqual(waves * per_cluster + watch.LEEWAY_OVERHEAD_SECONDS, remaining, (remaining, count))

    def test_a_project_of_one_batch_is_one_call_in_name_order(self) -> None:
        self.roster = {"p1": triples("p1", watch.CLUSTERS_PER_BATCH)}
        runs = {t: watch.iso(T0 - timedelta(days=i)) for i, t in enumerate(self.roster["p1"])}
        with self.sandbox():
            self.sweep({}, runs, 3600.0)
        self.assertEqual([clusters_arg(a) for a, _ in self.calls], [self.roster["p1"]])

    def test_a_larger_project_is_read_in_batches_each_with_its_own_timeouts(self) -> None:
        self.roster = {"p1": triples("p1", watch.CLUSTERS_PER_BATCH + 1)}
        with self.sandbox():
            _, project_runs, scan_errors, _, scanned, requested = self.sweep({}, {}, 3600.0)
        self.assertEqual([len(clusters_arg(a)) for a, _ in self.calls], [watch.CLUSTERS_PER_BATCH, 1])
        self.assertEqual({c for a, _ in self.calls for c in clusters_arg(a)}, set(self.roster["p1"]))
        self.assertEqual([t for _, t in self.calls], [watch.PROJECT_TIMEOUT_SECONDS] * 2)
        per_cluster = [a[a.index(watch.CLUSTER_TIMEOUT_FLAG) + 1] for a, _ in self.calls]
        self.assertEqual(per_cluster, ["135s", "300s"])
        self.assertEqual(scan_errors, {})
        self.assertEqual(scanned, {"p1"})
        self.assertEqual(requested, set(self.roster["p1"]))
        self.assertEqual(project_runs, {"p1": watch.iso(T0)})

    def test_a_call_on_a_short_budget_holds_only_the_waves_that_fit(self) -> None:
        self.assertEqual(watch.batch_size(watch.PROJECT_TIMEOUT_SECONDS), watch.CLUSTERS_PER_BATCH)
        self.assertEqual(watch.batch_size(330), 4)
        self.assertEqual(watch.batch_size(watch.MIN_PROJECT_RUN_SECONDS), watch.PARALLELISM)
        self.assertEqual(watch.batch_size(1), watch.PARALLELISM)
        with self.sandbox():
            self.sweep({}, {}, 330.0)
        self.assertEqual([len(clusters_arg(a)) for a, _ in self.calls], [4] * 5)
        for argv, timeout in self.calls:
            per_cluster = int(argv[argv.index(watch.CLUSTER_TIMEOUT_FLAG) + 1].rstrip("s"))
            self.assertGreaterEqual(per_cluster, watch.CONSERVATIVE_CLUSTER_READ_SECONDS)
            self.assertLessEqual(2 * per_cluster + watch.LEEWAY_OVERHEAD_SECONDS, timeout)

    def test_the_budget_running_out_mid_project_leaves_the_rest_for_its_next_turn(self) -> None:
        deadline = watch.PROJECT_TIMEOUT_SECONDS + watch.MIN_PROJECT_RUN_SECONDS - 10.0
        runs: dict = {}
        with self.sandbox(seconds=watch.PROJECT_TIMEOUT_SECONDS):
            _, project_runs, scan_errors, error_kinds, scanned, requested = self.sweep({}, runs, deadline)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(scan_errors, {"p1": "12 of 20 clusters read; 8 not reached (out of time this run)"})
        self.assertEqual(error_kinds, {"p1": watch.ERROR_KIND_BUDGET_EXHAUSTED})
        self.assertEqual(scanned, {"p1"})
        self.assertEqual(requested, set(self.roster["p1"]))
        self.assertEqual(project_runs, {"p1": watch.iso(T0)}, "its turn started with a full call's time")
        self.assertEqual(runs, dict.fromkeys(clusters_arg(self.calls[0][0]), watch.iso(T0)))

    def test_a_single_batch_failure_keeps_the_one_call_message(self) -> None:
        self.roster = {"p1": triples("p1", 2), "p2": triples("p2", 1)}
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}), self.sandbox(fails("p1")):
            _, _, scan_errors, error_kinds, _, _ = self.sweep({}, {}, 3600.0)
        self.assertEqual(
            scan_errors,
            {"p1": watch.SCAN_FAILED_DETAIL.format(
                target="p1", error="RuntimeError: leeway exited 1 without a report: boom"
            )},
        )
        self.assertEqual(error_kinds, {"p1": watch.ERROR_KIND_SCAN_FAILED})

    def test_a_long_cause_is_cut_so_the_entry_stays_short(self) -> None:
        self.roster = {"p1": triples("p1", 2 * watch.CLUSTERS_PER_BATCH)}
        stderr = "x" * watch.STDERR_EXCERPT_CHARS
        with self.sandbox(lambda clusters, _: subprocess.CompletedProcess([], 1, "", stderr)
                          if "p1/l/c00" in clusters else None):
            _, _, scan_errors, _, _, _ = self.sweep({}, {}, 3600.0)
        prefix = "12 of 24 clusters read; 12 failed ("
        self.assertTrue(scan_errors["p1"].startswith(prefix + "xxx"))
        self.assertTrue(scan_errors["p1"].endswith(watch.CAUSE_ELLIPSIS + ")"))
        self.assertEqual(len(scan_errors["p1"]), len(prefix) + watch.CAUSE_MAX_CHARS + len(")"))

    def test_a_listing_timeout_reports_the_listings_own_timeout(self) -> None:
        self.roster = {"p2": triples("p2", 1)}

        def run(argv, **kw):
            if argv[0] == watch.LIST_CLUSTERS_ARGV[0]:
                raise subprocess.TimeoutExpired(cmd=argv, timeout=kw["timeout"])
            return completed_json(make_report([{"cluster": c, "status": "ok"} for c in clusters_arg(argv)]))

        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}), mock.patch.object(
            watch.sandbox_exec, "run", side_effect=run
        ):
            _, _, scan_errors, error_kinds, _, _ = self.sweep({}, {}, 500.0)
        self.assertEqual(
            scan_errors,
            {"p1": f"gcloud container clusters list --project p1 timed out after {watch.PROJECT_LOOKUP_TIMEOUT_SECONDS}s"},
        )
        self.assertEqual(error_kinds, {"p1": watch.ERROR_KIND_TIMEOUT})

    def test_explicit_clusters_are_batched_and_a_failed_batch_does_not_fail_the_tick(self) -> None:
        explicit = triples("p1", 20)
        with mock.patch.dict(os.environ, {watch.CLUSTERS_ENV: ",".join(explicit), watch.PROJECTS_ENV: ""}):
            self.tick(0, findings={c: make_topology_finding(name="web") for c in explicit})

            def first_fails(clusters, _):
                if "p1/l/c00" in clusters:
                    return subprocess.CompletedProcess([], 255, "", "leeway crashed")
                return None

            lines = self.tick(1, first_fails)
        failed = next(clusters_arg(a) for a, _ in self.calls if "p1/l/c00" in clusters_arg(a))
        detail = f"{20 - len(failed)} of 20 clusters read; {len(failed)} failed (leeway crashed)"
        self.assertEqual(lines[0], self.partial(1, f"the configured clusters: {detail}"))
        self.assertEqual(self.finding_clusters(), sorted(failed), "findings retire only on clusters read")
        self.assertEqual(watch.load_ledger(self.home)[watch.SCAN_ERRORS_KEY], {watch.EXPLICIT_TARGET_LABEL: detail})

    def test_explicit_clusters_with_every_batch_failed_fail_the_tick_with_the_first_cause(self) -> None:
        explicit = triples("p1", 20)
        with mock.patch.dict(os.environ, {watch.CLUSTERS_ENV: ",".join(explicit), watch.PROJECTS_ENV: ""}), self.sandbox(
            lambda clusters, _: subprocess.CompletedProcess([], 255, "", f"crash {clusters[0]}")
        ):
            with self.assertRaises(RuntimeError) as raised:
                self.sweep({}, {}, 3600.0)
        first = clusters_arg(self.calls[0][0])[0]
        self.assertEqual(str(raised.exception), f"leeway exited 255 without a report: crash {first}")
        self.assertEqual(len(self.calls), 2)

    def test_cluster_stamps_are_saved_pruned_by_the_project_list_and_optional_in_the_ledger(self) -> None:
        self.assertEqual(watch.ledger_cluster_runs({}), {})
        self.assertEqual(watch.ledger_cluster_runs({watch.CLUSTER_RUNS_KEY: ["x"]}), {})
        self.roster = {"p1": triples("p1", 2)}
        old = watch.iso(T0 - timedelta(days=1))
        self.write_ledger(**{watch.CLUSTER_RUNS_KEY: {"p1/l/dropped": old, "p9/l/other": old}})
        self.tick(0)
        # p1's list no longer names p1/l/dropped; p9's list was not read, so its stamp stays.
        self.assertEqual(
            watch.load_ledger(self.home)[watch.CLUSTER_RUNS_KEY],
            {"p1/l/c00": watch.iso(T0), "p1/l/c01": watch.iso(T0), "p9/l/other": old},
        )

    # --- one turn per project ---------------------------------------------

    def test_a_timed_out_call_ends_the_projects_turn_and_costs_only_its_own_clusters(self) -> None:
        gone = "p1/l/zz-gone"
        self.roster = {"p1": [*triples("p1", 30), gone]}
        self.tick(0, findings={c: make_topology_finding(name="web") for c in self.roster["p1"]})
        self.assertEqual(len(self.finding_clusters()), 31)

        # Tick 1: the call holding the stuck cluster times out after two calls
        # returned; the project's turn ends there.
        self.roster = {"p1": triples("p1", 30)}
        runs = watch.load_ledger(self.home)[watch.CLUSTER_RUNS_KEY]
        stuck = watch.order_clusters(self.roster["p1"], runs, T0 + timedelta(days=1))[watch.CLUSTERS_PER_BATCH * 2]
        lines = self.tick(1, hangs(stuck))
        sizes = [len(clusters_arg(a)) for a, _ in self.calls]
        killed = set(clusters_arg(self.calls[-1][0]))
        self.assertEqual(len(sizes), 3)
        self.assertIn(stuck, killed, "no call starts after the timeout")
        read = len(self.ok)
        left = 30 - sum(sizes)
        parts = [f"{len(killed)} failed (the sandbox run timed out after 900s)"]
        if left:
            parts.append(f"{left} left for its next turn after the timeout")
        self.assertEqual(lines[0], self.partial(1, f"p1: {read} of 30 clusters read; {', '.join(parts)}"))
        # Findings retire on the clusters read and on the cluster p1 no longer lists.
        self.assertEqual(self.finding_clusters(), sorted(set(self.roster["p1"]) - self.ok))
        ledger = watch.load_ledger(self.home)
        self.assertEqual(ledger[watch.PROJECT_RUNS_KEY], {"p1": watch.iso(T0 + timedelta(days=1))})
        stamped = {c for c, s in ledger[watch.CLUSTER_RUNS_KEY].items() if s == watch.iso(T0 + timedelta(days=1))}
        self.assertEqual(stamped, self.ok | killed, "every cluster in a call is stamped, however it ended")

    def test_a_hanging_project_costs_one_call_a_tick_and_the_others_are_read(self) -> None:
        self.roster = {"p-hang": triples("p-hang", 42), "p2": triples("p2", 5), "p3": triples("p3", 5)}
        outputs = []
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p-hang,p2,p3"}):
            for day in range(4):
                outputs.append(self.tick(day, hangs(*self.roster["p-hang"]), seconds=60.0))
                hang_calls = [a for a, _ in self.calls if clusters_arg(a)[0].startswith("p-hang/")]
                self.assertEqual(len(hang_calls), 1, f"day {day}")
                self.assertEqual(self.ok, set(self.roster["p2"]) | set(self.roster["p3"]), f"day {day}")
        detail = "0 of 42 clusters read; 12 failed (the sandbox run timed out after 900s), " \
                 "30 left for its next turn after the timeout"
        self.assertEqual(outputs, [[self.partial(1, f"p-hang: {detail}")], [], [], []])

    def test_a_tick_where_no_call_returns_still_rotates_the_projects(self) -> None:
        names = ["p1", "p2", "p3", "p4"]
        self.roster = {p: triples(p, 2) for p in names}
        every = [c for p in names for c in self.roster[p]]
        firsts = []
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: ",".join(names)}):
            for day in range(3):
                lines = self.tick(day, hangs(*every))
                self.assertTrue(lines[0].startswith("leeway watch: the tick failed: RuntimeError: "), lines)
                firsts.append(clusters_arg(self.calls[0][0])[0].split("/")[0])
                self.assertEqual(len(self.calls), 3, "three hung calls spend the tick")
        self.assertEqual(firsts, ["p1", "p4", "p3"], "the projects that had a turn rotate behind")
        ledger = watch.load_ledger(self.home)
        self.assertEqual(set(ledger[watch.CLUSTER_RUNS_KEY]), set(every))

    def test_a_dry_run_writes_no_stamps_even_when_the_tick_fails(self) -> None:
        self.roster = {"p1": triples("p1", 2)}
        self.clock = 0.0
        with self.sandbox(hangs(*self.roster["p1"])), redirect_stdout(io.StringIO()):
            watch.tick(now=T0, dry_run=True)
        self.assertFalse((self.home / watch.LEDGER_FILE_NAME).exists())

    def test_a_project_bigger_than_a_tick_does_not_starve_the_projects_after_it(self) -> None:
        self.roster = {"p-big": triples("p-big", 60), "p-small": triples("p-small", 1)}
        read: list[set[str]] = []
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p-big,p-small"}):
            for day in range(6):
                self.tick(day, seconds=850.0)
                read.append(set(self.ok))
        small = [bool(r & set(self.roster["p-small"])) for r in read]
        self.assertEqual(small, [False, True, False, True, False, True])
        self.assertTrue(all(r & set(self.roster["p-big"]) for r in read), "p-big is read every tick")
        self.assertEqual((read[0] | read[1]) & set(self.roster["p-big"]), set(self.roster["p-big"]))

    def test_a_project_bigger_than_a_tick_is_covered_over_successive_ticks(self) -> None:
        self.roster = {"p1": triples("p1", 36)}
        read: list[set[str]] = []
        for day in range(3):
            self.tick(day, seconds=watch.TICK_BUDGET_SECONDS - watch.MIN_PROJECT_RUN_SECONDS + 1.0)
            read.append(set(self.ok))
        self.assertEqual([len(r) for r in read], [12, 12, 12])
        self.assertEqual(read[0] | read[1] | read[2], set(self.roster["p1"]), "each tick starts where the last stopped")

    def test_tied_stamps_rotate_batches_so_a_hanging_clusters_batch_mates_are_read(self) -> None:
        # Every cluster read in one tick carries the same stamp. Broken by
        # name, the hanging cluster's batch-mates would share its call, and be
        # lost with it, on every tick.
        self.roster = {"p1": triples("p1", 42)}
        stuck = "p1/l/c00"
        ever_read: set[str] = set()
        first_mates = None
        for day in range(6):
            self.tick(day, hangs(stuck))
            if first_mates is None:
                first_mates = set(clusters_arg(self.calls[-1][0])) - {stuck}
            ever_read |= self.ok
        self.assertEqual(len(first_mates), watch.CLUSTERS_PER_BATCH - 1)
        self.assertEqual(ever_read, set(self.roster["p1"]) - {stuck})

    # --- announcements ----------------------------------------------------

    def test_a_big_project_with_failures_is_one_counted_entry_that_does_not_repost(self) -> None:
        self.roster = {"p1": triples("p1", 100)}

        def second_call_fails(clusters, _):
            return subprocess.CompletedProcess([], 1, "", "boom") if len(self.calls) == 2 else None

        self.assertEqual(
            self.tick(0, second_call_fails, seconds=600.0),
            [self.partial(1, "p1: 38 of 100 clusters read; 12 failed (boom), 50 not reached (out of time this run)")],
        )
        # The next day the counts differ but the target is the same: no repost.
        self.assertEqual(self.tick(1, second_call_fails), [])
        self.assertEqual(
            watch.load_ledger(self.home)[watch.SCAN_ERRORS_KEY], {"p1": "88 of 100 clusters read; 12 failed (boom)"}
        )

    def test_two_failing_projects_are_one_entry_each_with_every_cause_counted(self) -> None:
        self.roster = {"p1": triples("p1", 30), "p2": triples("p2", 1), "p3": triples("p3", 1)}

        def outcome(clusters, timeout):
            if clusters[0].startswith("p2/"):
                return subprocess.CompletedProcess([], 1, "", "nope")
            if len(self.calls) == 2:
                return subprocess.CompletedProcess([], 1, "", "boom")
            if len(self.calls) == 3:
                raise subprocess.TimeoutExpired(cmd="leeway", timeout=timeout)
            return None

        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2,p3"}):
            lines = self.tick(0, outcome)
        self.assertEqual(lines, [self.partial(
            2,
            "p1: 12 of 30 clusters read; 18 failed (boom; 1 other cause(s))",
            "p2: leeway scan for p2 failed: RuntimeError: leeway exited 1 without a report: nope",
        )])

    def test_error_rows_are_folded_into_their_projects_one_entry(self) -> None:
        self.roster = {"p1": triples("p1", 2)}

        def one_error_row(clusters, _):
            rows = [{"cluster": c, "status": "error" if c == "p1/l/c01" else "ok", "error": "forbidden"}
                    for c in clusters]
            return completed_json(make_report(rows))

        lines = self.tick(0, one_error_row)
        self.assertEqual(lines, [self.partial(1, "p1: 1 of 2 clusters read; 1 returned an error (forbidden)")])

    def test_a_new_failing_target_posts_while_another_targets_episode_is_open(self) -> None:
        self.roster = {"p1": triples("p1", 1), "p2": triples("p2", 1), "p3": triples("p3", 1)}

        def failing(*projects):
            def outcome(clusters, _):
                if clusters[0].split("/")[0] in projects:
                    return subprocess.CompletedProcess([], 1, "", "boom")
                return None

            return outcome

        def entry(p):
            return f"{p}: leeway scan for {p} failed: RuntimeError: leeway exited 1 without a report: boom"

        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2,p3"}):
            outputs = [self.tick(day, outcome) for day, outcome in enumerate(
                (failing("p1"), failing("p1", "p3"), failing("p1", "p3"), failing("p3"), failing())
            )]
        self.assertEqual(outputs, [
            [self.partial(1, entry("p1"))],
            [self.partial(2, entry("p3"), entry("p1"))],  # the new target first
            [],
            [],  # p1 reads cleanly; p3 is still failing
            ["leeway watch: every target that failed reads cleanly again"],
        ])

    def test_a_target_that_leaves_the_sweep_drops_its_episode(self) -> None:
        self.roster = {"p1": triples("p1", 1), "p2": triples("p2", 1)}
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            self.assertEqual(len(self.tick(0, fails("p2"))), 1)
        # p2 is no longer swept: its episode cannot close by a read, so it is
        # dropped, and p1's own failure and recovery are said as usual.
        def error_row(clusters, _):
            return completed_json(make_report([{"cluster": c, "status": "error", "error": "denied"} for c in clusters]))

        self.assertEqual(self.tick(1, error_row), [self.partial(1, "p1: 0 of 1 clusters read; 1 returned an error (denied)")])
        self.assertEqual(self.tick(2), ["leeway watch: every target that failed reads cleanly again"])

    def test_recovery_waits_until_every_announced_target_reads_cleanly(self) -> None:
        self.roster = {"p1": triples("p1", 1), "p2": triples("p2", 1), "p3": triples("p3", 1)}
        spend_tick = watch.TICK_BUDGET_SECONDS - watch.MIN_PROJECT_RUN_SECONDS + 1.0

        def two_fail(clusters, _):
            return subprocess.CompletedProcess([], 1, "", "boom") if clusters[0][:2] in ("p1", "p2") else None

        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2,p3"}):
            first = self.tick(0, two_fail)
            # p1 reads cleanly and spends the tick: p2 is not reached, so its episode stays open.
            second = self.tick(1, seconds=spend_tick)
            self.assertEqual(self.ok, set(self.roster["p1"]))
            third = self.tick(2)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(third, ["leeway watch: every target that failed reads cleanly again"])
        self.assertNotIn(watch.ANNOUNCED_FAILED_TARGETS_KEY, watch.load_ledger(self.home).get(watch.ANNOUNCED_KEY, {}))

    def test_clusters_not_reached_alone_post_nothing_and_keep_their_findings(self) -> None:
        self.roster = {"p1": triples("p1", 30)}
        self.tick(0, findings={c: make_topology_finding(name="web") for c in self.roster["p1"]})
        half = watch.TICK_BUDGET_SECONDS / 2
        lines = self.tick(1, seconds=half)
        self.assertEqual(len(self.ok), 24)
        self.assertTrue(lines[0].startswith("leeway: 24 finding(s) resolved"))
        self.assertEqual(len(lines), 1, "no partial-scan line")
        self.assertEqual(self.finding_clusters(), sorted(set(self.roster["p1"]) - self.ok))
        markdown = (self.home / watch.REPORTS_DIR_NAME / watch.LATEST_MD_LINK_NAME).read_text(encoding="utf-8")
        self.assertIn(watch.REPORT_NOT_REACHED_HEADING, markdown)
        self.assertIn("- `p1`: 24 of 30 clusters read; 6 not reached (out of time this run)", markdown)
        self.assertNotIn("## Scan Errors", markdown)

    def test_an_older_ledgers_signature_neither_posts_nor_sticks(self) -> None:
        self.roster = {"p1": triples("p1", 1), "p2": triples("p2", 1)}
        entry = "p2: leeway scan for p2 failed: RuntimeError: leeway exited 1 without a report: boom"
        cases = (
            # A failure was announced by kind: today's failures count as announced.
            ([watch.ERROR_KIND_SCAN_FAILED], fails("p2"), [], ["p2"]),
            ([watch.ERROR_KIND_CLUSTER_ERROR, watch.ERROR_KIND_TIMEOUT], fails("p2"), [], ["p2"]),
            # Nothing fails now: the old episode is dropped without a line.
            ([watch.ERROR_KIND_SCAN_FAILED], None, [], None),
            # A budget gap alone announced no failure under today's rules.
            ([watch.ERROR_KIND_BUDGET_EXHAUSTED, "cut_short"], fails("p2"), [self.partial(1, entry)], ["p2"]),
            ([watch.ERROR_KIND_BUDGET_EXHAUSTED], None, [], None),
        )
        for stored, outcome, expected, open_targets in cases:
            with self.subTest(stored=stored, failing=outcome is not None):
                self.write_ledger(**{watch.ANNOUNCED_KEY: {watch.ANNOUNCED_PARTIAL_KEY: stored}})
                with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
                    self.assertEqual(self.tick(0, outcome), expected)
                announced = watch.load_ledger(self.home).get(watch.ANNOUNCED_KEY, {})
                self.assertNotIn(watch.ANNOUNCED_PARTIAL_KEY, announced)
                self.assertEqual(announced.get(watch.ANNOUNCED_FAILED_TARGETS_KEY), open_targets)

    # --- retirement -------------------------------------------------------

    def test_a_narrowed_or_empty_scope_retires_nothing_beyond_mains_rule(self) -> None:
        old = watch.iso(T0 - timedelta(days=1))
        self.roster = {"p1": triples("p1", 2)}
        # p1 returns an error row: the tick is not clean, so p2's finding
        # (outside the scope) stays.
        self.write_ledger(["p2/l/c00"], **{watch.PROJECT_RUNS_KEY: {"p2": old}})

        def error_row(clusters, _):
            return completed_json(make_report([{"cluster": c, "status": "error", "error": "denied"} for c in clusters]))

        self.tick(1, error_row)
        self.assertEqual(self.finding_clusters(), ["p2/l/c00"])
        self.assertEqual(watch.load_ledger(self.home)[watch.PROJECT_RUNS_KEY]["p2"], old)
        # An empty scope reads nothing, so nothing retires.
        empty = mock.Mock(projects=(), excluded_clusters=())
        with mock.patch.object(watch, "declared_scope", return_value=empty):
            self.tick(2)
        self.assertEqual(self.finding_clusters(), ["p2/l/c00"])
        # A clean tick retires it, as on main (the whole sweep read without an error).
        self.tick(3)
        self.assertEqual(self.finding_clusters(), [])

    # --- simulated fleets -------------------------------------------------

    def simulated_leeway(self, durations: dict[str, float]):
        """A sandbox that runs `leeway report` as leeway does: the clusters in
        waves of PARALLELISM, each wave as long as its slowest read capped at
        the per-cluster `--timeout`; a read longer than that is an error row,
        and a call longer than its sandbox timeout is killed."""

        def run(argv, **kw):
            clusters = clusters_arg(argv)
            per_cluster = int(argv[argv.index(watch.CLUSTER_TIMEOUT_FLAG) + 1].rstrip("s"))
            wall = SIM_STARTUP_SECONDS
            rows = []
            for start in range(0, len(clusters), watch.PARALLELISM):
                wave = clusters[start : start + watch.PARALLELISM]
                wall += max(min(durations[c], per_cluster) for c in wave)
                rows += [
                    {"cluster": c, "status": "ok"}
                    if durations[c] <= per_cluster
                    else {"cluster": c, "status": "error", "error": "context deadline exceeded"}
                    for c in wave
                ]
            if wall > kw["timeout"]:
                self.clock += kw["timeout"]
                raise subprocess.TimeoutExpired(cmd="leeway", timeout=kw["timeout"])
            self.clock += wall
            self.ok.update(r["cluster"] for r in rows if r["status"] == "ok")
            self.errored.update(r["cluster"] for r in rows if r["status"] != "ok")
            return completed_json(make_report(rows))

        return mock.patch.object(watch.sandbox_exec, "run", side_effect=run)

    def simulate(self, roster: dict[str, list[str]], days: int) -> list[tuple[list[str], set[str], set[str]]]:
        """Daily ticks over `roster` with read times between 30 and 114 s; per
        tick, the chat lines and the clusters read ok and in error."""
        self.roster = roster
        fleet = [c for project in sorted(roster) for c in roster[project]]
        durations = {c: 30.0 + (k * 37) % 85 for k, c in enumerate(fleet)}
        ticks = []
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: ",".join(sorted(roster))}):
            for day in range(days):
                self.clock = 0.0
                self.ok, self.errored = set(), set()
                buf = io.StringIO()
                with self.simulated_leeway(durations), redirect_stdout(buf):
                    watch.tick(now=T0 + timedelta(days=day))
                ticks.append((buf.getvalue().splitlines(), set(self.ok), set(self.errored)))
        return ticks

    def test_a_simulated_fleet_reads_every_cluster_within_three_ticks_and_posts_nothing(self) -> None:
        # 74 clusters at up to 114 s each: about 50 fit a tick. No read errors
        # (every call gives each cluster at least 120 s), and no chat line.
        roster = {"pa": triples("pa", 40), "pb": triples("pb", 25), "pc": triples("pc", 7), "pd": triples("pd", 2)}
        ticks = self.simulate(roster, 10)
        self.assertEqual([sorted(errors) for _, _, errors in ticks], [[]] * 10)
        self.assertEqual([line for lines, _, _ in ticks for line in lines], [])
        fleet = {c for clusters in roster.values() for c in clusters}
        for day in range(len(ticks) - 2):
            self.assertEqual(ticks[day][1] | ticks[day + 1][1] | ticks[day + 2][1], fleet, f"ticks {day} to {day + 2}")


class BatchDocsTest(unittest.TestCase):
    def test_the_docs_state_the_batch_size_the_code_uses(self) -> None:
        phrase = f"calls of at most {watch.CLUSTERS_PER_BATCH} clusters"
        for doc in (CRON_README, WATCHDOGS_PAGE):
            with self.subTest(doc=doc.name):
                self.assertIn(phrase, " ".join(doc.read_text(encoding="utf-8").split()))


class RosterTest(unittest.TestCase):
    def test_the_platform_roster_carries_leeway_watch_as_a_daily_no_agent_entry(self) -> None:
        jobs = {j["id"]: j for j in json.loads(ROSTER.read_text(encoding="utf-8"))["jobs"]}
        self.assertIn("leeway-watch", jobs)
        entry = jobs["leeway-watch"]
        self.assertTrue(entry["no_agent"])
        self.assertEqual(entry["script"], "leeway_watch.py")
        self.assertEqual(entry["deliver"], "chat")
        self.assertEqual(entry["schedule"]["expr"], entry["schedule"]["display"])
        self.assertEqual(entry["schedule"]["expr"].split()[2:], ["*", "*", "*"], "a daily job")
        self.assertTrue(entry["enabled"])
        self.assertEqual(entry["risk"], "high")


class ResolvedScopeTests(unittest.TestCase):
    """#5621: under spec.mode: next the resolved scope names the projects and
    each member project's clusters; the management and explicit projects are
    listed live, a member nothing listed is a scan error, and the profiles
    directory is not walked."""

    def test_next_mode_sweeps_from_the_resolved_scope(self) -> None:
        argvs: list[list[str]] = []

        def sandbox(argv, **kw):
            argvs.append(list(argv))
            if argv[0] == "gcloud":
                return subprocess.CompletedProcess(args=argv, returncode=0, stdout="m1\tus-central1\nm-old\tus-east1\n", stderr="")
            target = argv[argv.index(watch.CLUSTERS_FLAG) + 1]
            return completed_json(make_report([{"cluster": c, "status": "ok"} for c in target.split(",")]))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "profiles" / "cluster-ghost").mkdir(parents=True)
            (root / "profiles" / "cluster-ghost" / "config.yaml").write_text(
                "cluster_identity:\n  project: member-a\n  cluster: ghost\n  location: l\n", encoding="utf-8")
            (root / "scope.json").write_text(json.dumps({
                "present": True, "projects": [], "folders": ["1"],
                "exclude": {"clusters": [{"projectId": "mgmt", "location": "us-east1", "clusterName": "m-old"}]},
            }), encoding="utf-8")
            (root / "resolved-scope.json").write_text(json.dumps({
                "v": 1, "resolvedAt": "2026-11-10T12:00:00Z", "state": "ok", "memberHash": __import__("scope_publisher").member_hash(json.loads((root / "scope.json").read_text())), "excludedProjects": [],
                "projects": [{"id": p, "via": ["folders/1"], "outcome": o} for p, o in (("member-a", "ok"), ("member-b", "denied"), ("member-empty", "ok"))],
                "clusters": {"member-a": {"at": "2026-11-10T12:00:00Z", "clusters": [["us-central1", "a1"]]},
                             "member-empty": {"at": "2026-11-10T12:00:00Z", "clusters": []}},
            }), encoding="utf-8")
            env = {
                watch.WATCH_HOME_ENV: str(root / "leeway"), watch.CLUSTERS_ENV: "", watch.PROJECTS_ENV: "",
                watch.MANAGEMENT_PROJECT_ENV: "mgmt", "PLATFORM_AGENT_HOME": tmp,
                watch.fleet_scope_targets.SCOPE_FILE_ENV: str(root / "scope.json"),
                watch.fleet_scope_targets.RESOLVED_SCOPE_FILE_ENV: str(root / "resolved-scope.json"),
            }
            with mock.patch.dict(os.environ, env), mock.patch.object(
                watch, "roster_clusters_by_project", side_effect=AssertionError("walked the profiles")
            ), mock.patch.object(
                watch.fleet_scope_targets, "profile_identities", side_effect=AssertionError("walked the profiles")
            ), mock.patch.object(watch.sandbox_exec, "run", side_effect=sandbox):
                _, _, scan_errors, error_kinds, scanned, requested = watch.sweep_fleet({}, T0, time.monotonic() + 3600)
        self.assertEqual([a for a in argvs if a[0] == "gcloud"], [[*watch.LIST_CLUSTERS_ARGV, "mgmt"]], "only the management project is listed live")
        self.assertEqual(requested, {"mgmt/us-central1/m1", "member-a/us-central1/a1"})
        self.assertEqual(scanned, {"mgmt", "member-a", "member-empty"})
        self.assertEqual(set(scan_errors), {"member-b"})
        self.assertIn("denied", scan_errors["member-b"])
        self.assertEqual(error_kinds["member-b"], watch.ERROR_KIND_SCAN_FAILED)

    def test_an_explicit_roster_with_an_unusable_resolved_scope_retires_nothing_on_its_members(self) -> None:
        """LEEWAY_WATCH_PROJECTS names a member, the resolved scope is missing:
        the member is never scanned, so a finding it holds is not retired."""

        def sandbox(argv, **kw):
            if argv[0] == "gcloud":
                return subprocess.CompletedProcess(args=argv, returncode=0, stdout="m1\tus-central1\n", stderr="")
            target = argv[argv.index(watch.CLUSTERS_FLAG) + 1]
            return completed_json(make_report([{"cluster": c, "status": "ok"} for c in target.split(",")]))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scope.json").write_text(json.dumps({"present": True, "projects": [], "folders": ["1"]}), encoding="utf-8")
            env = {
                watch.WATCH_HOME_ENV: str(root / "leeway"), watch.CLUSTERS_ENV: "", watch.PROJECTS_ENV: "mgmt,member-a",
                watch.MANAGEMENT_PROJECT_ENV: "mgmt", "PLATFORM_AGENT_HOME": tmp,
                watch.fleet_scope_targets.SCOPE_FILE_ENV: str(root / "scope.json"),
                watch.fleet_scope_targets.RESOLVED_SCOPE_FILE_ENV: str(root / "resolved-scope.json"),
            }
            with mock.patch.dict(os.environ, env), mock.patch.object(watch.sandbox_exec, "run", side_effect=sandbox):
                _, _, scan_errors, _, scanned, _ = watch.sweep_fleet({}, T0, time.monotonic() + 3600)
        self.assertNotIn("member-a", scanned)
        self.assertTrue(scan_errors, "the members the unusable scope could not name are a scan error")
        self.assertFalse(watch.can_retire_finding(
            "member-a/us-central1/a1", {"cluster": "member-a/us-central1/a1"},
            {"mgmt/us-central1/m1"}, {}, scanned, scan_errors,
        ))


if __name__ == "__main__":
    unittest.main()
