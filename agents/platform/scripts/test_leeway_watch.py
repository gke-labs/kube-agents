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
NO_REPORT_PREFIX = "leeway watch: the tick failed: "
NO_REPORT_TIMED_OUT = "no cluster could be read this run: the reads timed out; it will try again next run"
NO_REPORT_FAILED = "no cluster could be read this run: the reads failed; it will try again next run"
NO_REPORT_MIXED = (
    "no cluster could be read this run: some reads timed out and others failed; it will try again next run"
)
FAILED_TIMED_OUT_LINE = (
    "**The topology skew check didn't run today.** Reading the clusters took too long. "
    "It will try again at its next scheduled run."
)
FAILED_ERROR_LINE = (
    "**The topology skew check didn't run today.** Reading the clusters failed. "
    "It will try again at its next scheduled run."
)
FAILED_MIXED_LINE = (
    "**The topology skew check didn't run today.** Some cluster reads timed out and others failed. "
    "It will try again at its next scheduled run."
)
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
                watch.HOME_ENV: self.tmp.name,
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

    def log_text(self) -> str:
        path = self.home / "logs" / watch.WATCH_LOG_FILE
        return path.read_text(encoding="utf-8") if path.is_file() else ""

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
            "**2 pod placement problems on cluster seeded-d (all new)**\n"
            "- checkout-api (shop) on seeded-d: 100% of its pods in one zone; its zone spread rule is not being kept\n"
            "- compute class balanced-spot on seeded-d: running on its last machine choice too long "
            "(last choice 75% of the time)\n",
            out,
        )
        self.assertIn("If they're still open, I'll post them again on or after 2026-10-26.", out)
        self.assertNotIn(str(self.home), out)

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
        self.assertIn("**1 pod placement problem on cluster seeded-d (changed)**", out)

        # Day 2, severity escalates from warning to critical -> immediate report.
        t2 = T0 + timedelta(days=2)
        c3 = {
            "cluster": "p1/us-central1/seeded-d",
            "status": "ok",
            "topology_findings": [make_topology_finding(cause="volume_pinning", severity="critical")],
        }
        code3, out3 = self.run_tick(lambda *a, **kw: completed_json(make_report([c3], t2)), now=t2)
        self.assertEqual(code3, 0)
        self.assertIn("**1 pod placement problem on cluster seeded-d (changed)**", out3)

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
        self.assertIn("**1 pod placement problem on cluster seeded-d (still open)**", out)

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
        self.assertIn(
            "**The topology skew check couldn't read p1.** Existing findings there are kept until the next clean read.",
            out_err,
        )
        self.assertIn("kubectl list nodes timed out", self.log_text())
        self.assertNotIn("kubectl list nodes timed out", out_err)
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
        self.assertIn("**The topology skew check read every project cleanly again.**", out_ok)
        self.assertIn("**1 pod placement problem has cleared**\n- checkout-api (shop) on seeded-d\n", out_ok)
        ledger_after = watch.load_ledger(self.home)
        self.assertEqual(ledger_after[watch.FINDINGS_KEY], {})

    def test_tick_failure_marker_suppresses_duplicate_alerts_and_announces_recovery(self) -> None:
        def failing_run(*a, **kw):
            return subprocess.CompletedProcess(args=["leeway"], returncode=1, stdout="", stderr="broker unreachable")

        code1, out1 = self.run_tick(failing_run, now=T0)
        self.assertEqual(code1, 0)
        self.assertEqual(out1.strip(), FAILED_ERROR_LINE)
        self.assertIn("broker unreachable", self.log_text())
        self.assertNotIn("broker unreachable", out1)

        # Second failure with identical reason prints nothing.
        code2, out2 = self.run_tick(failing_run, now=T0 + timedelta(days=1))
        self.assertEqual(code2, 0)
        self.assertEqual(out2, "")

        # Recovery prints the plain recovery line.
        c_clean = {"cluster": "p1/us-central1/seeded-d", "status": "ok", "topology_findings": []}
        code3, out3 = self.run_tick(
            lambda *a, **kw: completed_json(make_report([c_clean], T0 + timedelta(days=2))),
            now=T0 + timedelta(days=2),
        )
        self.assertEqual(code3, 0)
        self.assertEqual(out3.strip(), "**The topology skew check is running again.**")

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
        self.assertEqual(
            out,
            "dry run (nothing posted):\n"
            "**1 pod placement problem on cluster seeded-d (new)**\n"
            "- checkout-api (shop) on seeded-d: 100% of its pods in one zone; its zone spread rule is not being kept\n"
            "Ask me how to fix any of these. If they're still open, I'll post them again on or after 2026-10-26.\n",
        )
        self.assertFalse((self.home / watch.LEDGER_FILE_NAME).exists())
        self.assertFalse((self.home / watch.REPORTS_DIR_NAME).exists())
        self.assertFalse(hasattr(watch, "due_finding_summary"))

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
            self.assertIn("**1 pod placement problem has cleared**", out2)
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
        self.assertEqual(
            out1.strip(),
            "**The topology skew check didn't run today.** Its record of earlier runs can't be read. "
            "An admin needs to fix this before it can run; the details are in the agent's log.",
        )
        self.assertIn("refusing to overwrite it", self.log_text())
        self.assertNotIn("refusing to overwrite it", out1)
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
            self.assertIn(
                "**The topology skew check couldn't read p1.** Existing findings there are kept until the next clean read.",
                out,
            )
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

        def partial(project: str) -> str:
            return f"**The topology skew check couldn't read {project}.** Existing findings there are kept until the next clean read."

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
                [partial("p2")],
                [],  # the same target failing again is not re-posted
                [partial("p1")],  # a new target posts; p2 read cleanly
                [],
                ["**The topology skew check read every project cleanly again.**"],
            ],
        )
        self.assertIn("leeway exited 1 without a report: err", self.log_text())

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
            self.assertIn("**1 pod placement problem has cleared**", out)

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
        self.assertIn("**2 pod placement problems on cluster seeded-d (all new)**", out)
        self.assertIn("ledger-sync (finance) on seeded-d: 100% of its pods in one zone; its node selector allows only one zone", out)
        self.assertIn("frontend-web (shop) on seeded-d: 100% of its pods in one zone; no clear cause found", out)
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
        self.assertIn(
            "**4 pod placement problems on cluster seeded-d (all new)**\n"
            "- compute class gpu-spot on seeded-d: pods are stuck with no allowed machines to run on\n"
            "- zone us-central1-c on seeded-d: it takes no new pods because every node is cordoned or tainted\n"
            "- confined-app (shop) on seeded-d: 100% of its pods in one zone; its node selector allows only one zone\n"
            "- and 1 more\n",
            out,
        )
        self.assertEqual(len(watch.load_ledger(self.home)[watch.FINDINGS_KEY]), 4)
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
        self.assertIn("**8 pod placement problems on 2 clusters (all new)**", out)
        self.assertIn("- svc-0 (shop) on alpha: 100% of its pods in one zone", out)
        self.assertIn("- and 5 more\n", out)
        self.assertNotIn("p1/us-central1/alpha", out)

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
            self.assertEqual(len(lines0), 4)
            self.assertIn("**1 pod placement problem on cluster seeded-d (new)**", lines0[0])
            self.assertEqual(lines0[3], watch.TOPIC_PUBLISH_FAILED_LINE)
            for plumbing in ("Error: nats:", "TopicsStateSubjectMissing", "nats stream edit TOPICS-STATE", "kubectl"):
                self.assertNotIn(plumbing, lines0[3])
                self.assertIn(plumbing, self.log_text())
            self.assertEqual(len(a2a_calls), 1)

            # Day 1: quiet tick retries latest.json and fails again -> no duplicate warning spam.
            t1 = T0 + timedelta(days=1)
            code1, out1 = self.run_tick(lambda *a, **kw: completed_json(rep), now=t1)
            self.assertEqual(code1, 0)
            self.assertEqual(out1, "")
            self.assertEqual(len(a2a_calls), 2)

            # Day 2: operator added the subject to TOPICS-STATE -> quiet tick retry succeeds and clears marker silently.
            t2 = T0 + timedelta(days=2)
            code2, out2 = self.run_tick(lambda *a, **kw: completed_json(rep), now=t2)
            self.assertEqual(code2, 0)
            self.assertEqual(out2, "")
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
            self.assertIn("**1 pod placement problem has cleared**", out1)
            self.assertIn(
                "**The topology skew check couldn't read the configured clusters.** "
                "Existing findings there are kept until the next clean read.",
                out1,
            )
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
            self.assertEqual(out2.strip(), "**The topology skew check read every project cleanly again.**")
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
        self.assertIn("**2 pod placement problems on cluster seeded-d (all new)**", out0)

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
        self.assertIn("**2 pod placement problems have cleared**", out4)
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
            self.assertIn(watch.TOPIC_PUBLISH_FAILED_LINE, out0)
            self.assertIn(
                "publishing to blackboard topic topology-skew failed "
                "(TimeoutExpired: a2a topics write topology-skew timed out after 30s)",
                self.log_text(),
            )
            warn_line = [ln for ln in out0.splitlines() if "agent bus" in ln][0]
            self.assertNotIn("--text", warn_line)
            self.assertNotIn("--data", warn_line)
            self.assertNotIn("pod placement problem", warn_line)
            self.assertNotIn("TimeoutExpired", warn_line)

            t1 = T0 + timedelta(days=1)
            code1, out1 = self.run_tick(lambda *a, **kw: completed_json(rep), now=t1)
            self.assertEqual(code1, 0)
            self.assertEqual(out1, "")

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
        self.assertIn("**2 pod placement problems on cluster seeded-d (all new)**", out)
        self.assertIn("legacy-api (shop) on seeded-d: 100% of its pods in one zone; no clear cause found", out)
        self.assertIn("compute class novel-class on seeded-d: running on its last machine choice too long", out)

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
        self.assertIn("kube-dns (kube-system) on ap-mcp: 100% of its pods in one zone; no clear cause found", out0)

        t1 = T0 + timedelta(days=1)
        rep1 = make_report([{"cluster": "p1/us-central1/ap-mcp", "status": "ok", "topology_findings": [rebalanced]}], t1)
        code1, out1 = self.run_tick(lambda *a, **kw: completed_json(rep1), now=t1)
        self.assertEqual(code1, 0)
        self.assertIn("**1 pod placement problem has cleared**\n- kube-dns (kube-system) on ap-mcp", out1)
        self.assertEqual(watch.load_ledger(self.home)[watch.FINDINGS_KEY], {})
        latest1 = json.loads((self.home / watch.REPORTS_DIR_NAME / watch.LATEST_JSON_LINK_NAME).read_text())
        self.assertEqual(latest1["summary"]["topology_findings"], 0)

        t2 = T0 + timedelta(days=2)
        rep2 = make_report([{"cluster": "p1/us-central1/ap-mcp", "status": "ok", "topology_findings": [stacked]}], t2)
        code2, out2 = self.run_tick(lambda *a, **kw: completed_json(rep2), now=t2)
        self.assertEqual(code2, 0)
        self.assertIn("**1 pod placement problem on cluster ap-mcp (new)**", out2)
        self.assertIn("kube-dns (kube-system) on ap-mcp: 100% of its pods in one zone; no clear cause found", out2)

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
        self.assertIn("**1 pod placement problem has cleared**\n- kube-dns (kube-system) on ap-mcp", out3)
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
        self.assertIn("kube-dns (kube-system) on ap-mcp: 50% of its pods in one zone; its zone spread rule is not being kept", out4)

        t5 = T0 + timedelta(days=5)
        rep5 = make_report([{"cluster": "p1/us-central1/ap-mcp", "status": "ok", "topology_findings": [rebalanced]}], t5)
        code5, out5 = self.run_tick(lambda *a, **kw: completed_json(rep5), now=t5)
        self.assertEqual(code5, 0)
        self.assertIn("**1 pod placement problem has cleared**\n- kube-dns (kube-system) on ap-mcp", out5)
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

    def test_chat_names_no_pod_path_no_triple_and_no_cause_code(self) -> None:
        """The due message carries no report path on the pod, no project/location
        triple and no leeway cause code; the report is still written there."""
        rep = make_report(
            [{"cluster": "p1/us-central1/seeded-d", "status": "ok", "topology_findings": [make_topology_finding()]}],
            T0,
        )
        code, out = self.run_tick(lambda *a, **kw: completed_json(rep), now=T0)
        self.assertEqual(code, 0)
        self.assertTrue((self.home / watch.REPORTS_DIR_NAME / watch.LATEST_MD_LINK_NAME).is_file())
        for plumbing in (str(self.home), watch.REPORTS_DIR_NAME + "/", watch.REPORT_MARKDOWN_SUFFIX,
                         "p1/us-central1", "constraint_ignored", "seeded-d:shop", "shop/checkout-api", "leeway", "finding"):
            self.assertNotIn(plumbing, out)


def chat_item(
    name: str = "checkout-api",
    namespace: str = "shop",
    cluster: str = "p1/us-central1/prod-east",
    kind: str = "leeway.placement_drift",
    subject_kind: str = "Deployment",
    cause: str = "constraint_ignored",
    share: float = 1.0,
    severity: str = "warning",
) -> dict:
    return {
        "key": "/".join([cluster, namespace, subject_kind, name, kind]),
        "cluster": cluster,
        "namespace": namespace,
        "kind": kind,
        "subject_kind": subject_kind,
        "subject_name": name,
        "suspected_cause": cause,
        "max_domain_share": share,
        "severity": severity,
    }


class ChatMessageTests(unittest.TestCase):
    """The due and resolved chat messages (#6255): headline first, plain items, bounded."""

    NEXT = "2026-11-25"

    def test_headline_is_the_first_line_bold_and_answers(self) -> None:
        message = watch.due_message([chat_item()], [watch.REASON_NEW], self.NEXT)
        lines = message.splitlines()
        self.assertEqual(lines[0], "**1 pod placement problem on cluster prod-east (new)**")
        self.assertEqual(
            lines[1],
            "- checkout-api (shop) on prod-east: 100% of its pods in one zone; its zone spread rule is not being kept",
        )
        self.assertEqual(lines[2], "Ask me how to fix any of these. If they're still open, I'll post them again on or after 2026-11-25.")
        self.assertEqual(len(lines), 3)

    def test_plural_wording_counts_clusters_and_breaks_down_reasons(self) -> None:
        items = [
            chat_item(name="a"),
            chat_item(name="b", cluster="p2/us-east1/prod-west"),
            chat_item(name="c", cluster="p2/us-east1/prod-west"),
        ]
        reasons = [watch.REASON_REFRESH, watch.REASON_NEW, watch.REASON_NEW]
        self.assertEqual(
            watch.due_message(items, reasons, self.NEXT).splitlines()[0],
            "**3 pod placement problems on 2 clusters (2 new, 1 still open)**",
        )
        self.assertEqual(
            watch.due_message(items, [watch.REASON_CHANGED] * 3, self.NEXT).splitlines()[0],
            "**3 pod placement problems on 2 clusters (all changed)**",
        )
        self.assertEqual(watch.resolved_message(["x on c"]).splitlines()[0], "**1 pod placement problem has cleared**")
        self.assertEqual(
            watch.resolved_message(["x on c", "y on c"]).splitlines()[0], "**2 pod placement problems have cleared**"
        )

    def test_items_stop_at_the_cap_and_say_how_many_more(self) -> None:
        cap = watch.MAX_CHAT_ITEMS
        items = [chat_item(name=f"svc-{i}") for i in range(cap + 2)]
        lines = watch.due_message(items, [watch.REASON_NEW] * len(items), self.NEXT).splitlines()
        self.assertEqual(sum(1 for ln in lines if ln.startswith("- svc-")), cap)
        self.assertIn("- and 2 more", lines)
        exact = watch.due_message(items[:cap], [watch.REASON_NEW] * cap, self.NEXT)
        self.assertNotIn("more", exact)
        resolved = watch.resolved_message([f"svc-{i} (shop) on prod-east" for i in range(cap + 4)]).splitlines()
        self.assertEqual(len(resolved), 1 + cap + 1)
        self.assertEqual(resolved[-1], "- and 4 more")

    def test_many_long_items_stay_bounded(self) -> None:
        long = "x" * 250
        items = [chat_item(name=f"{long}-{i}", namespace=long, cluster=f"p/l/{long}-{i % 7}") for i in range(200)]
        single = [chat_item(name=long, namespace=long, cluster=f"p/l/{long}")]
        bound = 2 * watch.CHAT_ITEM_MAX_CHARS + (watch.MAX_CHAT_ITEMS + 1) * (watch.CHAT_ITEM_MAX_CHARS + 2)
        for due in (items, single):
            message = watch.due_message(due, [watch.REASON_NEW] * len(due), self.NEXT)
            self.assertLessEqual(len(message), bound)
            for line in message.splitlines():
                self.assertLessEqual(len(line), watch.CHAT_ITEM_MAX_CHARS + len("- "))
        resolved = watch.resolved_message([watch.short_finding_label(i) for i in items])
        self.assertLessEqual(len(resolved), bound)

    def test_cause_codes_become_plain_phrases_and_an_unknown_code_falls_back(self) -> None:
        for code, phrase in watch.CAUSE_PHRASES.items():
            text = watch.due_chat_item(chat_item(cause=code))
            self.assertIn(phrase, text)
            self.assertNotIn(code, text.replace(phrase, ""))
        novel = watch.due_chat_item(chat_item(cause="novel_cause_v2"))
        self.assertIn(watch.CAUSE_PHRASE_FALLBACK, novel)
        self.assertNotIn("novel_cause_v2", novel)
        self.assertIn(watch.CAUSE_PHRASES[watch.DEFAULT_UNKNOWN], watch.due_chat_item(chat_item(cause="")))

    def test_an_over_long_item_keeps_its_cause(self) -> None:
        item = chat_item(name="n" * 60, namespace="s" * 50, cluster="p1/us-central1/" + "c" * 40)
        text = watch.due_chat_item(item)
        self.assertLessEqual(len(text), watch.CHAT_ITEM_MAX_CHARS)
        self.assertTrue(
            text.endswith(" on " + "c" * 40 + ": 100% of its pods in one zone; its zone spread rule is not being kept"), text
        )
        self.assertIn(watch.CHAT_ELLIPSIS, text)
        # A cluster name past GKE's 40-character cap, with the longest causes:
        # the cluster is cut too, never the share or the cause.
        long_cluster = "p1/us-central1/" + "k" * 60
        for item, ending in (
            (chat_item(name="n" * 60, namespace="s" * 60, cluster=long_cluster, cause="wedged"),
             ": 100% of its pods in one zone; pods are stuck with no allowed machines to run on"),
            (chat_item(name="g" * 60, namespace="_cluster", kind="leeway.rank_degraded", subject_kind="ComputeClass",
                       cluster=long_cluster, cause="no-migration", share=1.0),
             ": not moving back to its preferred machines (last choice 100% of the time)"),
        ):
            text = watch.due_chat_item(item)
            self.assertLessEqual(len(text), watch.CHAT_ITEM_MAX_CHARS)
            self.assertTrue(text.endswith(ending), text)
            self.assertIn(" on kkkk", text)
        # The issue's own workload, with the longest workload cause, fits whole.
        gateway = chat_item(name="platform-agent-a2a-gateway", namespace="kubeagents-system",
                            cluster="p1/us-central1/platform-agent-host", cause="taint_exclusion")
        self.assertNotIn(watch.CHAT_ELLIPSIS, watch.due_chat_item(gateway))

    def test_a_zone_cause_on_a_workload_names_the_other_zone(self) -> None:
        # On a workload, taint_exclusion and domain_outage are about the zone
        # its pods could not reach, so the item must not read as its own zone.
        for code, workload_phrase in (
            ("taint_exclusion", "a node in another zone was cordoned or tainted"),
            ("domain_outage", "another zone has nodes that are not ready"),
        ):
            workload = watch.due_chat_item(chat_item(cause=code, share=0.8))
            self.assertEqual(
                workload, f"checkout-api (shop) on prod-east: 80% of its pods in one zone; {workload_phrase}"
            )
            zone = watch.due_chat_item(chat_item(
                name="us-central1-c", namespace="_cluster", kind=watch.FINDING_KIND_DOMAIN_UNAVAILABLE,
                subject_kind="Domain", cause=code, share=0.0,
            ))
            self.assertIn("zone us-central1-c on prod-east: it takes no new pods because ", zone)
        empty = watch.due_chat_item(chat_item(
            name="us-central1-c", namespace="_cluster", kind=watch.FINDING_KIND_DOMAIN_UNAVAILABLE,
            subject_kind="Domain", cause="consolidation", share=0.0,
        ))
        self.assertEqual(empty, "zone us-central1-c on prod-east: it has no nodes left")

    def test_zone_compute_class_and_cluster_wide_items_read_plainly(self) -> None:
        zone = chat_item(
            name="us-central1-c", namespace="_cluster", kind=watch.FINDING_KIND_DOMAIN_UNAVAILABLE,
            subject_kind="Domain", cause="taint_exclusion", share=0.0,
        )
        self.assertEqual(
            watch.due_chat_item(zone),
            "zone us-central1-c on prod-east: it takes no new pods because every node is cordoned or tainted",
        )
        rank = chat_item(name="gold", namespace="_cluster", kind="leeway.rank_degraded",
                         subject_kind="ComputeClass", cause="last-rank", share=0.75)
        self.assertEqual(
            watch.due_chat_item(rank),
            "compute class gold on prod-east: running on its last machine choice too long (last choice 75% of the time)",
        )
        wedged = dict(rank, kind="leeway.rank_wedged", suspected_cause="wedged", max_domain_share=0.0)
        self.assertEqual(
            watch.due_chat_item(wedged), "compute class gold on prod-east: pods are stuck with no allowed machines to run on"
        )
        pool = chat_item(name="pool-a", namespace="_cluster", subject_kind="NodeGroup", cause="unknown", share=0.5)
        self.assertEqual(
            watch.due_chat_item(pool), "node pool pool-a on prod-east: 50% of its nodes in one zone; no clear cause found"
        )
        self.assertEqual(watch.short_finding_label(pool), "node pool pool-a on prod-east")
        custom = chat_item(name="tier-1", namespace="_cluster", subject_kind="Custom", cause="unknown", share=0.5)
        self.assertEqual(watch.due_chat_item(custom), "Custom tier-1 on prod-east: 50% in one zone; no clear cause found")
        self.assertEqual(watch.short_finding_label(zone), "zone us-central1-c on prod-east")

    def test_most_severe_then_unusable_zone_then_most_concentrated_first(self) -> None:
        items = [
            chat_item(name="half", share=0.5),
            chat_item(name="full", share=1.0),
            chat_item(name="us-a", namespace="_cluster", kind=watch.FINDING_KIND_DOMAIN_UNAVAILABLE,
                      subject_kind="Domain", cause="domain_outage", share=0.0),
            chat_item(name="hard", share=0.6, severity="critical"),
        ]
        lines = watch.due_message(items, [watch.REASON_NEW] * len(items), self.NEXT).splitlines()
        self.assertTrue(lines[1].startswith("- hard (shop)"), lines)
        self.assertTrue(lines[2].startswith("- zone us-a"), lines)
        self.assertTrue(lines[3].startswith("- full (shop)"), lines)
        self.assertEqual(lines[4], "- and 1 more")


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
                watch.HOME_ENV: tmp,
                "PLATFORM_AGENT_HOME": tmp,
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
            log = (Path(tmp) / "logs" / watch.WATCH_LOG_FILE).read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertEqual(
            buf.getvalue().strip(),
            "**The topology skew check didn't run today.** It has no GCP project to read. "
            "An admin needs to fix this before it can run; the details are in the agent's log.",
        )
        self.assertIn("no GCP project", log)
        self.assertNotIn("LEEWAY_WATCH_CLUSTERS", buf.getvalue())

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
                watch.HOME_ENV: tmp,
                "PLATFORM_AGENT_HOME": tmp,
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
            log = (Path(tmp) / "logs" / watch.WATCH_LOG_FILE).read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        output = buf.getvalue()
        self.assertEqual(
            output.strip(),
            "**The topology skew check didn't run today.** It couldn't read the install's scope. "
            "An admin needs to fix this before it can run; the details are in the agent's log.",
        )
        self.assertNotIn("ScopeRenderUnreadable", output)
        self.assertIn("ScopeRenderUnreadable: unreadable json", log)


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
            {watch.WATCH_HOME_ENV: str(self.home), watch.HOME_ENV: self.tmp.name,
             "PLATFORM_AGENT_HOME": str(self.home),
             watch.CLUSTERS_ENV: "", watch.PROJECTS_ENV: "p1",
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

    def log_text(self) -> str:
        path = self.home / "logs" / watch.WATCH_LOG_FILE
        return path.read_text(encoding="utf-8") if path.is_file() else ""

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
        targets = [e.split(": ", 1)[0] for e in entries]
        return watch.PARTIAL_LINE.format(targets=watch.format_name_list(targets))

    def partial_log(self, count: int, *entries: str) -> str:
        return watch.PARTIAL_LOG.format(
            prefix=watch.LINE_PREFIX,
            errors=count,
            reasons=watch.format_name_list(list(entries)),
        )

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
        self.assertEqual(scan_errors, {"p1": "12 of 20 clusters read — 8 not reached (out of time this run)"})
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
        prefix = "12 of 24 clusters read — 12 failed ("
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
        detail = f"{20 - len(failed)} of 20 clusters read — {len(failed)} failed (leeway crashed)"
        self.assertEqual(lines[-1], self.partial(1, f"the configured clusters: {detail}"))
        self.assertEqual(self.finding_clusters(), sorted(failed), "findings retire only on clusters read")
        self.assertEqual(watch.load_ledger(self.home)[watch.SCAN_ERRORS_KEY], {watch.EXPLICIT_TARGET_LABEL: detail})

    def test_explicit_clusters_with_every_batch_failed_fail_the_tick_with_the_fixed_detail(self) -> None:
        explicit = triples("p1", 20)
        with mock.patch.dict(os.environ, {watch.CLUSTERS_ENV: ",".join(explicit), watch.PROJECTS_ENV: ""}), self.sandbox(
            lambda clusters, _: subprocess.CompletedProcess([], 255, "", f"crash {clusters[0]}")
        ):
            with self.assertRaises(watch.NoReportReturned) as raised:
                self.sweep({}, {}, 3600.0)
        # Which batch went first, and so its cause, changes from run to run:
        # the detail names neither.
        self.assertEqual(str(raised.exception), NO_REPORT_FAILED)
        self.assertEqual(len(self.calls), 2)
        # One call that held every cluster keeps its own cause, as before batching.
        self.calls.clear()
        with mock.patch.dict(os.environ, {watch.CLUSTERS_ENV: "p1/l/c00", watch.PROJECTS_ENV: ""}), self.sandbox(
            lambda clusters, _: subprocess.CompletedProcess([], 255, "", "crash")
        ):
            with self.assertRaises(RuntimeError) as raised:
                self.sweep({}, {}, 3600.0)
        self.assertEqual(str(raised.exception), "leeway exited 255 without a report: crash")

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
            parts.append(f"{left} not tried this run (reading stops at the first timeout)")
        self.assertEqual(lines[-1], self.partial(1, f"p1: {read} of 30 clusters read — {' — '.join(parts)}"))
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
        detail = "0 of 42 clusters read — 12 failed (the sandbox run timed out after 900s) — " \
                 "30 not tried this run (reading stops at the first timeout)"
        self.assertEqual(outputs, [[self.partial(1, f"p-hang: {detail}")], [], [], []])

    def test_a_tick_where_no_call_returns_still_rotates_the_projects(self) -> None:
        names = ["p1", "p2", "p3", "p4"]
        self.roster = {p: triples(p, 2) for p in names}
        every = [c for p in names for c in self.roster[p]]
        firsts = []
        outputs = []
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: ",".join(names)}):
            for day in range(3):
                outputs.append(self.tick(day, hangs(*every)))
                firsts.append(clusters_arg(self.calls[0][0])[0].split("/")[0])
                self.assertEqual(len(self.calls), 3, "three hung calls spend the tick")
        self.assertEqual(firsts, ["p1", "p4", "p3"], "the projects that had a turn rotate behind")
        # The line names no target and no count, so the rotation does not
        # change it and it posts once.
        self.assertEqual(outputs, [[FAILED_TIMED_OUT_LINE], [], []])
        self.assertIn(NO_REPORT_PREFIX + NO_REPORT_TIMED_OUT, self.log_text())
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
            [self.partial(1, "p1: 38 of 100 clusters read — 12 failed (boom) — 50 not reached (out of time this run)")],
        )
        # The next day the counts differ but the target is the same: no repost.
        self.assertEqual(self.tick(1, second_call_fails), [])
        self.assertEqual(
            watch.load_ledger(self.home)[watch.SCAN_ERRORS_KEY], {"p1": "88 of 100 clusters read — 12 failed (boom)"}
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
            "p1: 12 of 30 clusters read — 18 failed (boom + 1 other cause(s))",
            "p2: leeway scan for p2 failed: RuntimeError: leeway exited 1 without a report: nope",
        )])

    def test_error_rows_are_folded_into_their_projects_one_entry(self) -> None:
        self.roster = {"p1": triples("p1", 2)}

        def one_error_row(clusters, _):
            rows = [{"cluster": c, "status": "error" if c == "p1/l/c01" else "ok", "error": "forbidden"}
                    for c in clusters]
            return completed_json(make_report(rows))

        lines = self.tick(0, one_error_row)
        self.assertEqual(lines, [self.partial(1, "p1: 1 of 2 clusters read — c01 returned an error (forbidden)")])

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
            [watch.PARTIAL_CLEARED_LINE],
        ])

    def test_a_target_that_leaves_the_sweep_drops_its_episode(self) -> None:
        self.roster = {"p1": triples("p1", 1), "p2": triples("p2", 1)}
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            self.assertEqual(len(self.tick(0, fails("p2"))), 1)
        # p2 is no longer swept: its episode cannot close by a read, so it is
        # dropped, and p1's own failure and recovery are said as usual.
        def error_row(clusters, _):
            return completed_json(make_report([{"cluster": c, "status": "error", "error": "denied"} for c in clusters]))

        self.assertEqual(self.tick(1, error_row), [self.partial(1, "p1: 0 of 1 clusters read — c00 returned an error (denied)")])
        self.assertEqual(self.tick(2), [watch.PARTIAL_CLEARED_LINE])

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
        self.assertEqual(third, [watch.PARTIAL_CLEARED_LINE])
        self.assertNotIn(watch.ANNOUNCED_FAILED_TARGETS_KEY, watch.load_ledger(self.home).get(watch.ANNOUNCED_KEY, {}))

    def test_the_post_leads_with_what_is_due_then_what_cleared_then_the_scan(self) -> None:
        self.roster = {"p1": triples("p1", 1), "p2": triples("p2", 1)}
        (c1,) = self.roster["p1"]
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            self.tick(0, findings={c1: make_topology_finding(name="web")})
            # web clears and api is new on p1 while p2 fails: three kinds of line in one post.
            lines = self.tick(1, fails("p2"), findings={c1: make_topology_finding(name="api")})
        headlines = [i for i, line in enumerate(lines) if line.startswith("**")]
        self.assertEqual(len(headlines), 3)
        self.assertEqual(headlines[0], 0)
        self.assertTrue(lines[0].startswith("**1 pod placement problem on cluster "), lines)
        self.assertEqual(lines[headlines[1]], "**1 pod placement problem has cleared**")
        self.assertEqual(
            lines[-1],
            "**The topology skew check couldn't read p2.** Existing findings there are kept until the next clean read.",
        )

    def test_the_topic_summary_is_the_headline_as_plain_text(self) -> None:
        self.roster = {"p1": triples("p1", 1)}
        (c1,) = self.roster["p1"]
        published: list[str] = []

        def publish(summary, path):
            published.append(summary)
            return True, None

        with mock.patch.object(watch, "publish_blackboard_topic", side_effect=publish):
            lines = self.tick(0, findings={c1: make_topology_finding(name="web")})
        self.assertEqual(published, [lines[0].strip("*")])
        self.assertNotIn("\n", published[0])
        self.assertNotIn("Ask me", published[0])

    def test_clusters_not_reached_alone_post_nothing_and_keep_their_findings(self) -> None:
        self.roster = {"p1": triples("p1", 30)}
        self.tick(0, findings={c: make_topology_finding(name="web") for c in self.roster["p1"]})
        half = watch.TICK_BUDGET_SECONDS / 2
        # A new finding on every cluster read makes the tick write a report.
        lines = self.tick(1, seconds=half, findings={c: make_topology_finding(name="api") for c in self.roster["p1"]})
        self.assertEqual(len(self.ok), 24)
        # What is due leads the post; what cleared follows.
        self.assertEqual(lines[0], "**24 pod placement problems on 24 clusters (all new)**")
        self.assertIn("**24 pod placement problems have cleared**", lines)
        self.assertEqual(sum(line.startswith("**") for line in lines), 2)
        self.assertFalse([line for line in lines if line.startswith("leeway watch:")], "no partial-scan line")
        web = {f["cluster"] for f in watch.load_ledger(self.home)[watch.FINDINGS_KEY].values() if f["subject_name"] == "web"}
        self.assertEqual(web, set(self.roster["p1"]) - self.ok)
        markdown = (self.home / watch.REPORTS_DIR_NAME / watch.LATEST_MD_LINK_NAME).read_text(encoding="utf-8")
        self.assertIn(watch.REPORT_NOT_REACHED_HEADING, markdown)
        self.assertIn("- `p1`: 24 of 30 clusters read — 6 not reached (out of time this run)", markdown)
        self.assertNotIn("## Scan Errors", markdown)

    def test_the_json_report_says_what_the_run_did_not_read(self) -> None:
        # The JSON summary counts only clusters read this run; the coverage
        # block lets a reader tell an unread cluster from a clean one.
        self.roster = {"p1": triples("p1", 30)}
        self.tick(0, findings={c: make_topology_finding(name="web") for c in self.roster["p1"]})
        half = watch.TICK_BUDGET_SECONDS / 2
        self.tick(1, seconds=half, findings={c: make_topology_finding(name="api") for c in self.roster["p1"]})
        latest = self.home / watch.REPORTS_DIR_NAME / watch.LATEST_JSON_LINK_NAME
        coverage = json.loads(latest.read_text(encoding="utf-8"))[watch.COVERAGE_FIELD]
        self.assertEqual(coverage[watch.COVERAGE_CLUSTERS_SWEPT_FIELD], 30)
        self.assertEqual(coverage[watch.COVERAGE_CLUSTERS_READ_FIELD], 24)
        self.assertEqual(
            coverage[watch.COVERAGE_NOT_REACHED_FIELD],
            {"p1": "24 of 30 clusters read — 6 not reached (out of time this run)"},
        )
        self.assertEqual(coverage[watch.COVERAGE_SCAN_ERRORS_FIELD], {})

    def test_the_json_report_names_the_runs_scan_errors(self) -> None:
        self.roster = {"p1": triples("p1", 3)}

        def error_row(clusters, _):
            rows = []
            for c in clusters:
                if c == "p1/l/c02":
                    rows.append({"cluster": c, "status": "error", "error": "denied"})
                else:
                    rows.append({"cluster": c, "status": "ok", "topology_findings": [make_topology_finding(name="web")]})
            return completed_json(make_report(rows))

        self.tick(0, error_row)
        latest = self.home / watch.REPORTS_DIR_NAME / watch.LATEST_JSON_LINK_NAME
        coverage = json.loads(latest.read_text(encoding="utf-8"))[watch.COVERAGE_FIELD]
        self.assertEqual(coverage[watch.COVERAGE_CLUSTERS_SWEPT_FIELD], 3)
        self.assertEqual(coverage[watch.COVERAGE_CLUSTERS_READ_FIELD], 2)
        self.assertEqual(coverage[watch.COVERAGE_NOT_REACHED_FIELD], {})
        self.assertEqual(list(coverage[watch.COVERAGE_SCAN_ERRORS_FIELD]), ["p1"])

    def test_clusters_not_reached_hold_back_the_report_refresh_and_the_topic_retry(self) -> None:
        self.roster = {"p1": triples("p1", 30)}
        half = watch.TICK_BUDGET_SECONDS / 2
        latest = self.home / watch.REPORTS_DIR_NAME / watch.LATEST_JSON_LINK_NAME
        published: list[str] = []

        def publish(summary, path):
            published.append(summary)
            return True, None

        with mock.patch.object(watch, "publish_blackboard_topic", side_effect=publish):
            self.tick(0, findings={c: make_topology_finding(name="web") for c in self.roster["p1"]})
            first_report = latest.resolve()
            published.clear()
            # Findings retire on the 24 clusters read; 6 are not reached, so
            # `latest.json` and the topic wait for a sweep of the whole fleet.
            lines = self.tick(1, seconds=half)
            self.assertEqual(lines[0], "**24 pod placement problems have cleared**")
            self.assertEqual(sum(line.startswith("**") for line in lines), 1, "only the cleared message")
            self.assertFalse([line for line in lines if line.startswith("leeway watch:")])
            self.assertEqual(latest.resolve(), first_report)
            self.assertEqual(published, [])
            self.assertIn(watch.ANNOUNCED_DEFERRED_REFRESH_KEY, watch.load_ledger(self.home)[watch.ANNOUNCED_KEY])
            # The next tick reads every cluster: the refresh happens then.
            self.tick(2)
            self.assertNotEqual(latest.resolve(), first_report)
            self.assertEqual(len(published), 1)
            # A failed publish waits the same way.
            ledger = watch.load_ledger(self.home)
            ledger[watch.ANNOUNCED_KEY] = {watch.ANNOUNCED_TOPIC_PUBLISH_KEY: "a2a topics write failed"}
            watch.save_ledger(self.home, ledger)
            published.clear()
            self.tick(3, seconds=half)
            self.assertEqual(published, [], "no retry while clusters were not reached")
            self.tick(4)
            self.assertEqual(published, [watch.A2A_RETRY_SUMMARY.format(prefix=watch.LINE_PREFIX)])

    def test_an_older_ledgers_episodes_recover_once_or_announce_anew(self) -> None:
        self.roster = {"p1": triples("p1", 1), "p2": triples("p2", 1)}
        entry = "p2: leeway scan for p2 failed: RuntimeError: leeway exited 1 without a report: boom"
        cleared = watch.PARTIAL_CLEARED_LINE
        open_p2 = {"p2": ["p2/l/c00"]}
        signature = watch.ANNOUNCED_PARTIAL_KEY
        targets = watch.ANNOUNCED_FAILED_TARGETS_KEY
        scan_failed = [watch.ERROR_KIND_SCAN_FAILED]
        held = [watch.ERROR_KIND_CLUSTER_ERROR, watch.ERROR_KIND_TIMEOUT]
        cases = (
            # A failure was announced by kind: one of the same kind in the
            # first run is taken as announced, as main would; one of another
            # kind posts. The signature is removed in that run, without a line.
            ({signature: scan_failed}, fails("p2"), [], open_p2, None),
            ({signature: held}, fails("p2"), [self.partial(1, entry)], open_p2, None),
            ({signature: scan_failed}, None, [], None, None),
            # A budget gap alone announced no failure under today's rules.
            ({signature: [watch.ERROR_KIND_BUDGET_EXHAUSTED, "cut_short"]}, fails("p2"), [self.partial(1, entry)],
             open_p2, None),
            ({signature: [watch.ERROR_KIND_BUDGET_EXHAUSTED]}, None, [], None, None),
            # A list of targets: each is open as a whole until read without
            # failing, and a failed call adds its clusters.
            ({targets: ["p2"]}, fails("p2"), [], {"p2": [watch.WHOLE_TARGET_MARKER, "p2/l/c00"]}, None),
            ({targets: ["p2"]}, None, [cleared], None, None),
            # The clusters each target failed on.
            ({targets: open_p2}, fails("p2"), [], open_p2, None),
            ({targets: open_p2}, None, [cleared], None, None),
        )
        for stored, outcome, expected, open_targets, kept in cases:
            with self.subTest(stored=stored, failing=outcome is not None):
                self.write_ledger(**{watch.ANNOUNCED_KEY: stored})
                with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
                    self.assertEqual(self.tick(0, outcome), expected)
                announced = watch.load_ledger(self.home).get(watch.ANNOUNCED_KEY, {})
                self.assertEqual(announced.get(signature), kept)
                self.assertEqual(announced.get(targets), open_targets)

    def test_an_older_ledgers_signature_is_converted_in_the_first_run_and_removed(self) -> None:
        # p3 is too big for what the run leaves it, so a target is not reached
        # every run: the signature must not wait for a run where nothing goes unread.
        self.roster = {"p1": triples("p1", 1), "p2": triples("p2", 1), "p3": triples("p3", 30)}
        signature = watch.ANNOUNCED_PARTIAL_KEY
        self.write_ledger(**{watch.ANNOUNCED_KEY: {signature: [watch.ERROR_KIND_BUDGET_EXHAUSTED, watch.ERROR_KIND_TIMEOUT]}})
        call_seconds = float(watch.PROJECT_TIMEOUT_SECONDS)
        outputs = []
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2,p3"}):
            # Day 0, the first run: p1 times out, a kind the signature holds, so it is silent.
            outputs.append(self.tick(0, hangs(*self.roster["p1"]), seconds=call_seconds))
            self.assertNotEqual(self.ok & set(self.roster["p3"]), set(self.roster["p3"]), "p3 is not read in full")
            announced = watch.load_ledger(self.home)[watch.ANNOUNCED_KEY]
            self.assertNotIn(signature, announced)
            self.assertEqual(announced[watch.ANNOUNCED_FAILED_TARGETS_KEY], {"p1": ["p1/l/c00"]})
            # Day 1: p1 reads cleanly, closing the episode the signature opened.
            outputs.append(self.tick(1, seconds=call_seconds))
            # Day 2: p2, which main never announced, times out and posts.
            outputs.append(self.tick(2, hangs(*self.roster["p2"]), seconds=call_seconds))
        self.assertEqual(outputs, [
            [],
            [watch.PARTIAL_CLEARED_LINE],
            [self.partial(1, "p2: the sandbox run timed out after 900s")],
        ])

    def test_a_big_project_with_an_always_erroring_cluster_posts_once_and_does_not_flap(self) -> None:
        # 100 clusters at 60 s a read: about 84 are read a tick, so the
        # erroring cluster is missed on some ticks. Its episode stays open
        # until that cluster itself reads cleanly.
        self.roster = {"p1": triples("p1", 100)}
        stuck = "p1/l/c42"
        state = {"erroring": True}

        def outcome(clusters, _):
            rows = [
                {"cluster": c, "status": "error", "error": "forbidden"}
                if c == stuck and state["erroring"] else {"cluster": c, "status": "ok"}
                for c in clusters
            ]
            return completed_json(make_report(rows))

        call_seconds = 60.0 * watch.CLUSTERS_PER_BATCH / watch.PARALLELISM
        days, stuck_called = [], []
        for day in range(30):
            state["erroring"] = day < 20
            days.append(self.tick(day, outcome, seconds=call_seconds))
            stuck_called.append(any(stuck in clusters_arg(a) for a, _ in self.calls))
        self.assertIn(False, stuck_called[:20], "the erroring cluster is missed on some ticks")
        first = stuck_called.index(True)
        expected = [[] for _ in days]
        expected[first] = [self.partial(
            1, "p1: 77 of 100 clusters read — c42 returned an error (forbidden) — 22 not reached (out of time this run)"
        )]
        recovered = stuck_called.index(True, 20)
        expected[recovered] = [watch.PARTIAL_CLEARED_LINE]
        self.assertEqual(days, expected)

    def test_a_cluster_the_project_no_longer_lists_closes_its_episode(self) -> None:
        self.roster = {"p1": triples("p1", 3)}

        def error_row(clusters, _):
            return completed_json(make_report([
                {"cluster": c, "status": "error" if c == "p1/l/c02" else "ok", "error": "denied"} for c in clusters
            ]))

        self.assertEqual(len(self.tick(0, error_row)), 1)
        self.roster = {"p1": triples("p1", 2)}
        self.assertEqual(self.tick(1), [watch.PARTIAL_CLEARED_LINE])

    def test_a_bare_configured_cluster_that_leaves_the_list_closes_its_episode(self) -> None:
        # Bare names (no project/location) are a supported LEEWAY_WATCH_CLUSTERS
        # form; a failed one that is removed from the variable must not keep
        # the episode open and silence later failures.
        def web_errors(clusters, _):
            return completed_json(make_report([
                {"cluster": c, "status": "error" if c == "web" else "ok", "error": "denied"} for c in clusters
            ]))

        def db_errors(clusters, _):
            return completed_json(make_report([
                {"cluster": c, "status": "error" if c == "db" else "ok", "error": "forbidden"} for c in clusters
            ]))

        with mock.patch.dict(os.environ, {watch.CLUSTERS_ENV: "web,api", watch.PROJECTS_ENV: ""}):
            self.assertEqual(len(self.tick(0, web_errors)), 1)
        with mock.patch.dict(os.environ, {watch.CLUSTERS_ENV: "api,db", watch.PROJECTS_ENV: ""}):
            self.assertEqual(self.tick(1), [watch.PARTIAL_CLEARED_LINE])
            lines = self.tick(2, db_errors)
        self.assertEqual(len(lines), 1)
        self.assertIn("db returned an error (forbidden)", self.log_text())

    def test_error_rows_name_up_to_three_clusters_then_count_them(self) -> None:
        def errors(names, causes=None):
            def outcome(clusters, _):
                rows = [{"cluster": c, "status": "error", "error": (causes or {}).get(name, "forbidden")}
                        if (name := c.rsplit("/", 1)[-1]) in names else {"cluster": c, "status": "ok"}
                        for c in clusters]
                return completed_json(make_report(rows))

            return outcome

        self.roster = {"p1": triples("p1", 5)}
        cases = (
            (errors({"c01", "c03"}, {"c03": "timeout"}),
             "p1: 3 of 5 clusters read — c01 and c03 returned an error (forbidden + 1 other cause(s))"),
            (errors({"c00", "c01", "c02"}), "p1: 2 of 5 clusters read — c00, c01 and c02 returned an error (forbidden)"),
            (errors({"c00", "c01", "c02", "c03"}), "p1: 1 of 5 clusters read — 4 clusters returned an error (forbidden)"),
        )
        for outcome, entry in cases:
            with self.subTest(entry=entry):
                self.write_ledger()
                self.assertEqual(self.tick(0, outcome), [self.partial(1, entry)])
                self.assertIn(self.partial_log(1, entry), self.log_text())

    def test_only_ok_rows_count_as_read_and_a_report_never_counts_past_its_call(self) -> None:
        self.roster = {"p1": triples("p1", 3)}

        def omits_one(clusters, _):
            # leeway leaves c02 out: neither read nor an error.
            return completed_json(make_report([
                {"cluster": "p1/l/c00", "status": "ok"},
                {"cluster": "p1/l/c01", "status": "error", "error": "forbidden"},
            ]))

        self.assertEqual(
            self.tick(0, omits_one), [self.partial(1, "p1: 1 of 3 clusters read — c01 returned an error (forbidden)")]
        )
        self.assertIn("p1: 1 of 3 clusters read — c01 returned an error (forbidden)", self.log_text())
        self.roster = {"p1": triples("p1", 1)}

        def extra_rows(clusters, _):
            return completed_json(make_report([
                {"cluster": "p1/l/c00", "status": "error", "error": "forbidden"},
                {"cluster": "p9/l/stray", "status": "error", "error": "forbidden"},
            ]))

        self.write_ledger()
        self.assertEqual(
            self.tick(0, extra_rows), [self.partial(1, "p1: 0 of 1 clusters read — c00 returned an error (forbidden)")]
        )
        self.assertIn("p1: 0 of 1 clusters read — c00 returned an error (forbidden)", self.log_text())

    def test_a_project_reached_late_is_stamped_only_when_every_cluster_got_a_call(self) -> None:
        # p1's call leaves 600 s: p2's turn starts with less than a full
        # call's time, and a 600 s call holds 8 clusters.
        late = float(watch.TICK_BUDGET_SECONDS - 600)
        for size, stamped in ((8, True), (9, False)):
            with self.subTest(size=size):
                self.roster = {"p1": triples("p1", 1), "p2": triples("p2", size)}
                self.calls.clear()
                with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}), self.sandbox(seconds=late):
                    self.clock = 0.0
                    _, project_runs, _, _, _, _ = self.sweep({}, {}, float(watch.TICK_BUDGET_SECONDS))
                self.assertEqual([len(clusters_arg(a)) for a, _ in self.calls], [1, 8])
                self.assertEqual("p2" in project_runs, stamped)

    def test_a_project_no_call_started_for_does_not_count_as_read(self) -> None:
        # p1's cluster list could not be read: its episode is open as a
        # whole. The next tick its listing spends the budget and no call
        # starts, so its episode stays open; a tick that reads it closes it.
        self.roster = {"p2": triples("p2", 1)}
        listing = {"code": 1, "seconds": 0.0}

        def run(argv, **kw):
            if argv[0] == watch.LIST_CLUSTERS_ARGV[0]:
                self.clock += listing["seconds"]
                return subprocess.CompletedProcess(argv, listing["code"], "c00\tl\n", "denied")
            return completed_json(make_report([{"cluster": c, "status": "ok"} for c in clusters_arg(argv)]))

        def tick(day):
            self.clock = 0.0
            buf = io.StringIO()
            with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}), mock.patch.object(
                watch.sandbox_exec, "run", side_effect=run
            ), redirect_stdout(buf):
                watch.tick(now=T0 + timedelta(days=day))
            return buf.getvalue().splitlines()

        self.assertEqual(len(tick(0)), 1)
        listing.update(code=0, seconds=watch.TICK_BUDGET_SECONDS - watch.MIN_PROJECT_RUN_SECONDS + 1.0)
        self.assertEqual(tick(1), [])
        self.assertEqual(
            watch.load_ledger(self.home)[watch.ANNOUNCED_KEY][watch.ANNOUNCED_FAILED_TARGETS_KEY], {"p1": None}
        )
        listing["seconds"] = 0.0
        self.assertEqual(tick(2), [watch.PARTIAL_CLEARED_LINE])

    # --- row matching -----------------------------------------------------

    def test_rows_match_by_source_then_context_then_a_short_name_the_call_does_not_share(self) -> None:
        batch = ["p1/us-a/web", "p1/us-b/web", "p1/us-a/api"]
        cases = (
            # leeway's own row: a short name two clusters share, and a unique source.
            ({"cluster": "web", "source": "gke:p1/us-b/web", "status": "error", "error": "denied"},
             watch.CallRows(errors={"p1/us-b/web": "denied"})),
            ({"cluster": "gke_p1_us-a_web", "status": "ok"}, watch.CallRows(ok={"p1/us-a/web"})),
            ({"cluster": "p1/us-a/web", "status": "ok"}, watch.CallRows(ok={"p1/us-a/web"})),
            ({"cluster": "api", "status": "ok"}, watch.CallRows(ok={"p1/us-a/api"})),
            # A shared short name, a stray source and no name match nothing.
            ({"cluster": "web", "status": "error", "error": "denied"}, watch.CallRows(unmatched_errors=["denied"])),
            ({"cluster": "web", "source": "gke:p9/l/web", "status": "ok"}, watch.CallRows(unmatched_ok=1)),
            # A source triple the call did not hold matches nothing, whatever its name.
            ({"cluster": "api", "source": "gke:p9/l/api", "status": "error", "error": "denied"},
             watch.CallRows(unmatched_errors=["denied"])),
            ({"cluster": "p1/us-a/api", "source": "gke:p1/us-c/api", "status": "ok"}, watch.CallRows(unmatched_ok=1)),
            # A source that is not a triple falls through to the name.
            ({"cluster": "api", "source": "gke:api", "status": "ok"}, watch.CallRows(ok={"p1/us-a/api"})),
            ({"status": "error"}, watch.CallRows(unmatched_errors=[watch.DEFAULT_CLUSTER_SCAN_FAILED])),
            ({"cluster": "", "source": "", "status": "ok"}, watch.CallRows(unmatched_ok=1)),
            ({"cluster": "api", "status": "pending"}, watch.CallRows()),
        )
        for row, expected in cases:
            with self.subTest(row=row):
                self.assertEqual(watch.batch_rows({"clusters": [row]}, batch), expected)

    def test_clusters_sharing_a_short_name_are_told_apart_by_source_across_batches(self) -> None:
        # leeway names a row by the cluster's short name and its source by the
        # full triple; two locations of one project share every short name.
        self.roster = {"p1": [f"p1/{loc}/c{i:02d}" for loc in ("us-a", "us-b") for i in range(8)]}
        first_batch = watch.order_clusters(self.roster["p1"], {}, T0)[: watch.CLUSTERS_PER_BATCH]
        bad = next(t for t in first_batch if t.startswith("p1/us-b/") and t.replace("us-b", "us-a") in first_batch)
        state = {"bad": bad}

        def leeway_rows(clusters, _):
            return completed_json({"clusters": [
                {"cluster": c.rsplit("/", 1)[-1], "source": f"gke:{c}",
                 "status": "error" if c == state["bad"] else "ok", "error": "forbidden"}
                for c in clusters
            ]})

        short = bad.rsplit("/", 1)[-1]
        self.assertEqual(self.tick(0, leeway_rows),
                         [self.partial(1, f"p1: 15 of 16 clusters read — {short} returned an error (forbidden)")])
        announced = watch.load_ledger(self.home)[watch.ANNOUNCED_KEY]
        self.assertEqual(announced[watch.ANNOUNCED_FAILED_TARGETS_KEY], {"p1": [bad]})
        # Its twin in the other location reads cleanly; the episode stays open
        # until the failed cluster itself does.
        state["bad"] = bad.replace("us-b", "us-a")
        self.assertEqual(self.tick(1, leeway_rows), [])
        state["bad"] = None
        self.assertEqual(self.tick(2, leeway_rows), [watch.PARTIAL_CLEARED_LINE])

    def test_an_unnamed_error_row_is_counted_unnamed_and_its_episode_closes_on_the_next_clean_call(self) -> None:
        self.roster = {"p1": triples("p1", 4)}

        def unnamed_errors(count):
            def outcome(clusters, _):
                rows = [{"cluster": c.rsplit("/", 1)[-1], "source": f"gke:{c}", "status": "ok"}
                        for c in clusters[: len(clusters) - count]]
                return completed_json({"clusters": rows + [{"status": "error", "error": "forbidden"}] * count})

            return outcome

        for count, part in ((1, "1 cluster returned an error"), (2, "2 clusters returned an error")):
            with self.subTest(count=count):
                self.write_ledger()
                lines = self.tick(0, unnamed_errors(count))
                # read + errored = clusters: each unnamed row counts once.
                self.assertEqual(lines, [self.partial(1, f"p1: {4 - count} of 4 clusters read — {part} (forbidden)")])
                self.assertIn(f"p1: {4 - count} of 4 clusters read — {part} (forbidden)", self.log_text())
                self.assertNotIn(watch.DEFAULT_UNKNOWN, lines[0])
                announced = watch.load_ledger(self.home)[watch.ANNOUNCED_KEY]
                self.assertEqual(announced[watch.ANNOUNCED_FAILED_TARGETS_KEY], {"p1": None})
                self.assertEqual(self.tick(1), [watch.PARTIAL_CLEARED_LINE])

        # Beside a named error row, an unnamed one still makes the episode whole.
        def named_and_unnamed(clusters, _):
            rows = [{"cluster": c, "status": "error" if c == "p1/l/c00" else "ok", "error": "denied"}
                    for c in clusters if c != "p1/l/c03"]
            return completed_json({"clusters": rows + [{"status": "error", "error": "forbidden"}]})

        self.write_ledger()
        self.assertEqual(self.tick(0, named_and_unnamed), [self.partial(
            1, "p1: 2 of 4 clusters read — 2 clusters returned an error (denied + 1 other cause(s))"
        )])
        self.assertIn(
            "p1: 2 of 4 clusters read — 2 clusters returned an error (denied + 1 other cause(s))",
            self.log_text(),
        )
        announced = watch.load_ledger(self.home)[watch.ANNOUNCED_KEY]
        self.assertEqual(announced[watch.ANNOUNCED_FAILED_TARGETS_KEY], {"p1": [watch.WHOLE_TARGET_MARKER, "p1/l/c00"]})

    def test_an_unmatched_error_row_keeps_the_remembered_clusters_until_they_read_cleanly(self) -> None:
        # p0 reads first every run, so a run always returns a report.
        self.roster = {"p0": triples("p0", 1), "p1": triples("p1", 14)}

        def leeway_rows(clusters):
            return [{"cluster": c.rsplit("/", 1)[-1], "source": f"gke:{c}", "status": "ok"} for c in clusters]

        def ok(clusters, _):
            return completed_json({"clusters": leeway_rows(clusters)})

        def unmatched_error(clusters, _):
            # p1's calls return one error row, for a cluster the call did not hold.
            if clusters[0].startswith("p0/"):
                return ok(clusters, _)
            stray = {"cluster": "c99", "source": "gke:p1/l/c99", "status": "error", "error": "forbidden"}
            return completed_json({"clusters": [stray]})

        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p0,p1"}):
            # Day 0: p1's first call times out; its twelve clusters are remembered.
            first = watch.order_clusters(self.roster["p1"], {}, T0)[0]
            self.assertEqual(len(self.tick(0, hangs(first))), 1)
            failed = set(clusters_arg(self.calls[1][0]))
            self.assertEqual(len(failed), watch.CLUSTERS_PER_BATCH)
            stored = watch.load_ledger(self.home)[watch.ANNOUNCED_KEY][watch.ANNOUNCED_FAILED_TARGETS_KEY]
            self.assertEqual(stored, {"p1": sorted(failed)})
            # Day 1: an error row matches no cluster. The target is open as a
            # whole as well, and the clusters it remembered stay remembered.
            self.assertEqual(self.tick(1, unmatched_error), [])
            stored = watch.load_ledger(self.home)[watch.ANNOUNCED_KEY][watch.ANNOUNCED_FAILED_TARGETS_KEY]
            self.assertEqual(stored, {"p1": [watch.WHOLE_TARGET_MARKER, *sorted(failed)]})
            # Day 2: p0 and one call of p1 read cleanly and spend the run. The
            # target is whole again, but remembered clusters were not reached.
            self.assertEqual(self.tick(2, ok, seconds=1300.0), [])
            read = {c for argv, _ in self.calls for c in clusters_arg(argv)}
            self.assertEqual(len(read), 1 + watch.CLUSTERS_PER_BATCH)
            unread = failed - read
            self.assertTrue(unread, "a remembered cluster was not reached")
            stored = watch.load_ledger(self.home)[watch.ANNOUNCED_KEY][watch.ANNOUNCED_FAILED_TARGETS_KEY]
            self.assertEqual(stored, {"p1": sorted(unread)})
            # Day 3: every cluster reads.
            self.assertEqual(self.tick(3, ok), [watch.PARTIAL_CLEARED_LINE])

    def test_a_project_whose_list_is_now_empty_closes_its_episode(self) -> None:
        self.roster = {"p1": []}
        self.write_ledger(**{watch.ANNOUNCED_KEY: {watch.ANNOUNCED_FAILED_TARGETS_KEY: {"p1": ["p1/l/c00"]}}})

        def run(argv, **kw):
            self.assertEqual(argv[: len(watch.LIST_CLUSTERS_ARGV)], list(watch.LIST_CLUSTERS_ARGV))
            return subprocess.CompletedProcess(argv, 0, "", "")

        buf = io.StringIO()
        with mock.patch.object(watch.sandbox_exec, "run", side_effect=run), redirect_stdout(buf):
            watch.tick(now=T0)
        self.assertEqual(buf.getvalue().splitlines(), [watch.PARTIAL_CLEARED_LINE])
        self.assertNotIn(watch.ANNOUNCED_KEY, watch.load_ledger(self.home))

    # --- tick failure -----------------------------------------------------

    def test_a_tick_where_no_call_returns_posts_one_fixed_line_over_days(self) -> None:
        # Projects of mixed sizes: four hang (one only on some calls, after
        # others exit 1), two exit 1 with a cause that names the batch. The
        # hangs spend the tick, so the rotation changes which projects fail,
        # how far each gets and why from day to day; the line does not.
        self.roster = {"pa": triples("pa", 30), "pb": triples("pb", 5), "pc": triples("pc", 1),
                       "pd": triples("pd", 13), "pe": triples("pe", 3), "pf": triples("pf", 2)}

        def outcome(clusters, timeout):
            project = clusters[0].split("/")[0]
            if project in ("pa", "pe", "pf") or (project == "pd" and len(self.calls) % 2):
                raise subprocess.TimeoutExpired(cmd="leeway", timeout=timeout)
            return subprocess.CompletedProcess([], 1, "", f"error reading {clusters[0]}")

        outputs = []
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: ",".join(self.roster)}):
            for day in range(6):
                outputs.append(self.tick(day, outcome, seconds=30.0))
        self.assertEqual(outputs, [[FAILED_MIXED_LINE]] + [[]] * 5)
        self.assertEqual(
            (self.home / watch.FAILURE_MARKER_FILE_NAME).read_text(encoding="utf-8").strip(),
            watch.FAILED_REASON_MIXED,
        )
        self.assertIn(NO_REPORT_PREFIX + NO_REPORT_MIXED, self.log_text())

    def test_the_no_report_line_names_the_kind_of_failure_and_posts_again_when_it_changes(self) -> None:
        self.roster = {p: triples(p, 1) for p in ("p1", "p2", "p3")}
        every = ("p1", "p2", "p3")

        def outcome(timing_out=(), exiting=()):
            def run(clusters, timeout):
                project = clusters[0].split("/")[0]
                if project in timing_out:
                    raise subprocess.TimeoutExpired(cmd="leeway", timeout=timeout)
                if project in exiting:
                    return subprocess.CompletedProcess([], 1, "", f"error reading {clusters[0]}")
                return None

            return run

        timeouts, failures, mixed, clean = outcome(every), outcome(exiting=every), outcome(("p2", "p3"), ("p1",)), outcome()
        recovered = [watch.RECOVERED_LINE]
        days = (
            (timeouts, [FAILED_TIMED_OUT_LINE]),
            (timeouts, []),
            (timeouts, []),
            # Another kind posts again; while the tick keeps failing the line
            # keeps both kinds, so a run that meets only one says nothing.
            (failures, [FAILED_MIXED_LINE]),
            (timeouts, []),
            (clean, recovered),
            (failures, [FAILED_ERROR_LINE]),
            (failures, []),
            (failures, []),
            (clean, recovered),
            (mixed, [FAILED_MIXED_LINE]),
            (mixed, []),
            (mixed, []),
        )
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: ",".join(every)}):
            outputs = [self.tick(day, condition) for day, (condition, _) in enumerate(days)]
        self.assertEqual(outputs, [expected for _, expected in days])
        for detail in (NO_REPORT_TIMED_OUT, NO_REPORT_FAILED, NO_REPORT_MIXED):
            self.assertNotIn("p1", detail)
            self.assertNotIn("Error:", detail)
            self.assertIn(NO_REPORT_PREFIX + detail, self.log_text())

    # --- refresh gate -----------------------------------------------------

    def test_a_deferred_refresh_waits_while_clusters_are_not_reached(self) -> None:
        self.roster = {"p1": triples("p1", 30)}
        published: list[str] = []
        reports = self.home / watch.REPORTS_DIR_NAME
        self.write_ledger(**{watch.ANNOUNCED_KEY: {watch.ANNOUNCED_DEFERRED_REFRESH_KEY: watch.REASON_ALL_RESOLVED}})
        with mock.patch.object(watch, "publish_blackboard_topic",
                               side_effect=lambda summary, path: (published.append(summary), (True, None))[1]):
            # Nothing fails, but six clusters are not reached: no refresh.
            self.assertEqual(self.tick(1, seconds=watch.TICK_BUDGET_SECONDS / 2), [])
            self.assertEqual(len(self.ok), 24)
            self.assertFalse(reports.exists())
            self.assertEqual(published, [])
            self.assertIn(watch.ANNOUNCED_DEFERRED_REFRESH_KEY, watch.load_ledger(self.home)[watch.ANNOUNCED_KEY])
            self.tick(2)
        self.assertTrue((reports / watch.LATEST_JSON_LINK_NAME).is_file())
        self.assertEqual(published, [watch.A2A_RETRY_SUMMARY.format(prefix=watch.LINE_PREFIX)])
        self.assertNotIn(watch.ANNOUNCED_KEY, watch.load_ledger(self.home))

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



# The operator's nats.conf render, byte for byte (the operator's
# TestRenderedNATSConfMatchesTheCalloutFixture holds it to the render).
NATS_CONF_FIXTURE = Path(__file__).resolve().parents[3] / "a2a" / "authcallout" / "testdata" / "rendered-nats.conf"
# The NATS server's max_payload when the config sets none.
NATS_SERVER_DEFAULT_MAX_PAYLOAD = 1024 * 1024
# How many bytes the sizing pass's widest counts may exceed the final ones by.
COUNT_DIGITS_SLACK = 32


def padded_finding(name: str, severity: str = "warning", tier: str = "B") -> dict:
    """A topology finding about the size of a real one (2.0-2.4 KB compact)."""
    finding = make_topology_finding(name=name, severity=severity, tier=tier)
    finding["contributingFactors"].append("x" * 900)
    return finding


def report_with(findings_per_cluster: list[list[dict]]) -> dict:
    return make_report(
        [
            {"cluster": f"p1/us-central1/c{i:03d}", "status": "ok", "topology_findings": findings}
            for i, findings in enumerate(findings_per_cluster)
        ]
    )


class TopicPublishCapTests(unittest.TestCase):
    """The topic write fits one bus message: a report too large is published
    capped to its most severe findings, never refused by the bus."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def publish(
        self, report: dict, text: str = "leeway: report", path: Path | None = None
    ) -> tuple[list[str], str | None]:
        if path is None:
            path = self.dir / "report.json"
            path.write_text(json.dumps(report, indent=watch.JSON_INDENT, sort_keys=True) + "\n", encoding="utf-8")
        calls: list[tuple[list[str], str | None]] = []

        def fake_run(argv, **kw):
            calls.append((list(argv), kw.get("input")))
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        with mock.patch.dict(os.environ, {watch.NATS_URL_ENV: "nats://nats:4222"}), mock.patch.object(
            watch.shutil, "which", return_value="/usr/local/bin/a2a"
        ), mock.patch.object(watch.subprocess, "run", side_effect=fake_run):
            published, err = watch.publish_blackboard_topic(text, path)
        self.assertTrue(published)
        self.assertIsNone(err)
        self.assertEqual(len(calls), 1)
        return calls[0]

    def capped(self, argv: list[str], stdin: str | None) -> tuple[str, dict]:
        self.assertEqual(argv[argv.index(watch.A2A_DATA_FLAG) + 1], watch.A2A_DATA_STDIN)
        self.assertIsNotNone(stdin)
        return argv[argv.index(watch.A2A_TEXT_FLAG) + 1], json.loads(stdin)

    def assert_fits_one_message(self, text: str, doc: dict) -> None:
        wire = watch.topic_artifact_size(text, doc) + watch.A2A_ENVELOPE_RESERVE_BYTES
        self.assertLess(wire, watch.NATS_MAX_PAYLOAD_BYTES)
        self.assertLessEqual(watch.topic_artifact_size(text, doc), watch.TOPIC_ARTIFACT_BUDGET_BYTES)

    def test_the_limit_is_the_rendered_buses_max_payload(self) -> None:
        conf = NATS_CONF_FIXTURE.read_text(encoding="utf-8")
        values = []
        for line in conf.splitlines():
            stripped = line.split("#", 1)[0].strip()
            key, sep, value = stripped.partition(":")
            if not sep:
                key, sep, value = stripped.partition("=")
            if sep and key.strip() == "max_payload":
                values.append(value.strip())
        self.assertLessEqual(len(values), 1)
        limit = int(values[0]) if values else NATS_SERVER_DEFAULT_MAX_PAYLOAD
        self.assertEqual(watch.NATS_MAX_PAYLOAD_BYTES, limit)

    def test_a_report_under_the_limit_is_published_from_its_file_unchanged(self) -> None:
        report = report_with([[padded_finding(f"w{j}") for j in range(20)] for _ in range(3)])
        argv, stdin = self.publish(report)
        path = self.dir / "report.json"
        self.assertEqual(argv[argv.index(watch.A2A_DATA_FLAG) + 1], f"@{path}")
        self.assertIsNone(stdin)
        self.assertEqual(argv[argv.index(watch.A2A_TEXT_FLAG) + 1], "leeway: report")

    def test_a_report_over_the_limit_is_published_capped_and_fits_one_message(self) -> None:
        # 16 clusters x 30 findings: about 1.1 MB compact, over the limit.
        report = report_with([[padded_finding(f"c{i}-w{j}") for j in range(30)] for i in range(16)])
        self.assertGreater(watch.topic_artifact_size("leeway: report", report), watch.NATS_MAX_PAYLOAD_BYTES)
        text, doc = self.capped(*self.publish(report))
        self.assert_fits_one_message(text, doc)
        # Still a leeway.report/v1alpha1 document a reader of the full one reads.
        self.assertEqual(doc["schema_version"], watch.SCHEMA_VERSION_VALUE)
        self.assertEqual(doc["generated_at"], report["generated_at"])
        self.assertEqual(doc["summary"], report["summary"], "the summary stays the full report's")
        self.assertEqual([c["cluster"] for c in doc["clusters"]], [c["cluster"] for c in report["clusters"]])
        for capped_cluster, full_cluster in zip(doc["clusters"], report["clusters"]):
            kept = capped_cluster["topology_findings"]
            full = full_cluster["topology_findings"]
            self.assertEqual(kept, [f for f in full if f in kept], "kept findings stay in report order")
        findings, ok_clusters, _ = watch.extract_findings(doc)
        self.assertEqual(len(ok_clusters), 16)
        truncated = doc[watch.TRUNCATED_FIELD]
        self.assertEqual(truncated[watch.TRUNCATED_FINDINGS_TOTAL_FIELD], 480)
        self.assertEqual(truncated[watch.TRUNCATED_FINDINGS_PUBLISHED_FIELD], len(findings))
        self.assertGreater(len(findings), 0)
        self.assertEqual(
            truncated[watch.TRUNCATED_FINDINGS_PUBLISHED_FIELD] + truncated[watch.TRUNCATED_FINDINGS_OMITTED_FIELD], 480
        )
        self.assertEqual(
            truncated[watch.TRUNCATED_OMITTED_BY_SEVERITY_FIELD],
            {"warning": truncated[watch.TRUNCATED_FINDINGS_OMITTED_FIELD]},
        )
        self.assertEqual(truncated[watch.TRUNCATED_FULL_REPORT_FIELD], str((self.dir / "report.json").resolve()))
        self.assertNotIn(watch.TRUNCATED_CLUSTERS_OMITTED_FIELD, truncated)
        self.assertTrue(text.startswith("leeway: report (topic copy capped at "))
        self.assertIn("of 480 findings", text)
        # The cap fills the budget: the next finding would not have fitted.
        # (The sizing pass counts every number at its widest, a few bytes over.)
        kept = report_findings(doc)
        next_finding = next(f for f in report_findings(report) if f not in kept)
        self.assertGreater(
            watch.topic_artifact_size(text, doc) + watch.a2a_encoded_size(next_finding) + COUNT_DIGITS_SLACK,
            watch.TOPIC_ARTIFACT_BUDGET_BYTES,
        )

    def test_the_boundary_is_the_artifact_budget(self) -> None:
        text = "leeway: report"
        report = report_with([[padded_finding(f"w{j}") for j in range(30)] for _ in range(12)])
        report["clusters"][0]["topology_findings"][0]["contributingFactors"].append("")
        pad = report["clusters"][0]["topology_findings"][0]["contributingFactors"]
        pad[-1] = "y" * (watch.TOPIC_ARTIFACT_BUDGET_BYTES - watch.topic_artifact_size(text, report))
        self.assertEqual(watch.topic_artifact_size(text, report), watch.TOPIC_ARTIFACT_BUDGET_BYTES)
        argv, stdin = self.publish(report, text)
        self.assertIsNone(stdin, "a report exactly at the budget is published unchanged")
        pad[-1] += "y"
        argv, stdin = self.publish(report, text)
        capped_text, doc = self.capped(argv, stdin)
        self.assert_fits_one_message(capped_text, doc)

    def test_the_cap_keeps_the_most_severe_findings(self) -> None:
        # Criticals sit last in report order, infos first; tiers vary within each severity.
        clusters = []
        for i in range(16):
            row = [padded_finding(f"c{i}-info{j}", "info", "A") for j in range(10)]
            row += [padded_finding(f"c{i}-warn{j}", "warning", "BC"[j % 2]) for j in range(10)]
            row += [padded_finding(f"c{i}-crit{j}", "critical", "CA"[j % 2]) for j in range(10)]
            clusters.append(row)
        report = report_with(clusters)
        text, doc = self.capped(*self.publish(report))
        self.assert_fits_one_message(text, doc)
        kept = [f for c in doc["clusters"] for f in c["topology_findings"]]
        kept_names = {f["subject"]["name"] for f in kept}
        omitted = [f for c in report["clusters"] for f in c["topology_findings"] if f["subject"]["name"] not in kept_names]
        self.assertTrue(kept and omitted)
        self.assertEqual(sum(1 for f in kept if f["severity"] == "critical"), 160, "every critical fits and is kept")
        rank = watch.finding_sort_key
        self.assertLessEqual(max(rank(f) for f in kept), min(rank(f) for f in omitted))
        omitted_by_severity = doc[watch.TRUNCATED_FIELD][watch.TRUNCATED_OMITTED_BY_SEVERITY_FIELD]
        self.assertNotIn("critical", omitted_by_severity)
        self.assertEqual(sum(omitted_by_severity.values()), len(omitted))

    def test_clusters_too_many_to_fit_are_left_out_and_counted(self) -> None:
        report = report_with([[padded_finding(f"c{i}")] for i in range(40)])
        # Room for a quarter of the clusters with their findings stripped, so
        # even the bare cluster entries do not fit and are left out.
        bare = [{**c, "topology_findings": []} for c in report["clusters"]]
        budget = watch.topic_artifact_size("t", {**report, "clusters": bare[:10]})
        with mock.patch.object(watch, "TOPIC_ARTIFACT_BUDGET_BYTES", budget):
            text, doc = self.capped(*self.publish(report, "t"))
            self.assertLessEqual(watch.topic_artifact_size(text, doc), budget)
        self.assertEqual(doc["clusters"], [])
        self.assertEqual(doc["summary"], report["summary"])
        truncated = doc[watch.TRUNCATED_FIELD]
        self.assertEqual(truncated[watch.TRUNCATED_CLUSTERS_OMITTED_FIELD], 40)
        self.assertEqual(truncated[watch.TRUNCATED_FINDINGS_PUBLISHED_FIELD], 0)
        self.assertEqual(truncated[watch.TRUNCATED_FINDINGS_OMITTED_FIELD], 40)

    def test_a_smaller_less_severe_finding_never_takes_the_place_of_a_more_severe_one(self) -> None:
        small_critical = padded_finding("small-crit", "critical", "A")
        large_critical = padded_finding("large-crit", "critical", "B")
        large_critical["contributingFactors"].append("z" * 50000)
        small_warning = padded_finding("small-warn", "warning", "A")
        report = report_with([[small_critical, large_critical, small_warning]])
        # Room for both small findings and the truncated block, not the large one.
        both_small = report_with([[small_critical, small_warning]])
        budget = watch.topic_artifact_size("t", both_small) + 5000
        self.assertGreater(watch.topic_artifact_size("t", report), budget)
        with mock.patch.object(watch, "TOPIC_ARTIFACT_BUDGET_BYTES", budget):
            text, doc = self.capped(*self.publish(report, "t"))
            self.assertLessEqual(watch.topic_artifact_size(text, doc), budget)
        self.assertEqual([f["subject"]["name"] for f in report_findings(doc)], ["small-crit"])
        self.assertEqual(
            doc[watch.TRUNCATED_FIELD][watch.TRUNCATED_OMITTED_BY_SEVERITY_FIELD], {"critical": 1, "warning": 1}
        )

    def test_one_severity_is_kept_tier_a_then_b_then_c(self) -> None:
        report = report_with([[padded_finding(f"tier-{t}", "warning", t)] for t in "CBA"])
        bare = report_with([[] for _ in "CBA"])
        full = watch.topic_artifact_size("t", report) + 2000
        admitted: list[str] = []
        for budget in range(watch.topic_artifact_size("t", bare), full, 100):
            with mock.patch.object(watch, "TOPIC_ARTIFACT_BUDGET_BYTES", budget):
                _, doc = watch.cap_topic_report(report, "t", self.dir / "report.json")
            for f in report_findings(doc):
                if f["tier"] not in admitted:
                    admitted.append(f["tier"])
        self.assertEqual(admitted, ["A", "B", "C"])

    def test_the_text_part_counts_toward_the_size(self) -> None:
        doc = report_with([[padded_finding("w0")]])
        base = watch.topic_artifact_size("", doc)
        short = watch.topic_artifact_size("x" * 100, doc) - base
        long = watch.topic_artifact_size("x" * 200, doc) - base
        self.assertGreater(short, 100)
        self.assertEqual(long - short, 100)

    def test_the_cap_leaves_room_for_a_large_summary_text(self) -> None:
        # A summary tens of KB long must come out of the findings' share: the
        # capped artifact, text included, still fits the budget.
        report = report_with([[padded_finding(f"c{i}-w{j}") for j in range(30)] for i in range(16)])
        text, doc = self.capped(*self.publish(report, text="x" * 40000))
        self.assertLessEqual(watch.topic_artifact_size(text, doc), watch.TOPIC_ARTIFACT_BUDGET_BYTES)

    def test_full_report_names_the_timestamped_file_behind_latest_json(self) -> None:
        report = report_with([[padded_finding(f"c{i}-w{j}") for j in range(30)] for i in range(16)])
        target = self.dir / "20261118T000000Z.json"
        target.write_text(json.dumps(report, indent=watch.JSON_INDENT, sort_keys=True) + "\n", encoding="utf-8")
        link = self.dir / watch.LATEST_JSON_LINK_NAME
        link.symlink_to(target.name)
        text, doc = self.capped(*self.publish(report, path=link))
        full_report = doc[watch.TRUNCATED_FIELD][watch.TRUNCATED_FULL_REPORT_FIELD]
        self.assertEqual(full_report, str(target.resolve()))
        self.assertNotEqual(full_report, str(link))
        self.assertIn(str(target.resolve()), text)

    def test_the_size_counts_what_gos_encoder_escapes(self) -> None:
        self.assertEqual(watch.a2a_encoded_size({"a": "x"}), len('{"a":"x"}'))
        self.assertEqual(watch.a2a_encoded_size("<>&"), len('"\\u003c\\u003e\\u0026"'))
        line_separators = chr(0x2028) + chr(0x2029)
        self.assertEqual(watch.a2a_encoded_size(line_separators), len('"\\u2028\\u2029"'))
        self.assertEqual(watch.a2a_encoded_size("é"), len('"é"'.encode("utf-8")))


def report_findings(report: dict) -> list[dict]:
    return [f for c in report["clusters"] for f in c.get("topology_findings") or []]


class PlainStatusAndDryRunTests(unittest.TestCase):
    """Status lines carry no exception names, exit codes, or commands, and
    `--dry-run` previews the exact due and resolved messages."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "leeway"
        self.env = mock.patch.dict(
            os.environ,
            {
                watch.WATCH_HOME_ENV: str(self.home),
                watch.HOME_ENV: str(self.root),
                "PLATFORM_AGENT_HOME": str(self.root),
                watch.PROJECTS_ENV: "p1",
                watch.CLUSTERS_ENV: "",
                watch.NATS_URL_ENV: "",
                watch.MANAGEMENT_PROJECT_ENV: "",
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def write_roster(self, expr: str) -> None:
        cron = self.root / "cron"
        cron.mkdir(parents=True, exist_ok=True)
        (cron / "jobs.json").write_text(
            json.dumps({"jobs": [{"id": watch.WATCH_JOB_ID, "schedule": {"kind": "cron", "expr": expr}}]}),
            encoding="utf-8",
        )

    def log_text(self) -> str:
        path = self.root / "logs" / watch.WATCH_LOG_FILE
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def clear_marker_and_log(self) -> None:
        (self.home / watch.FAILURE_MARKER_FILE_NAME).unlink(missing_ok=True)
        (self.root / "logs" / watch.WATCH_LOG_FILE).unlink(missing_ok=True)

    def test_each_failure_class_posts_plain_reason_and_next_step_and_logs_raw_detail(self) -> None:
        self.write_roster("30 8 * * *")
        cases = (
            (
                watch.NoProject(watch.NO_PROJECT_DETAIL),
                watch.FAILED_REASON_NO_PROJECT,
                watch.FAILED_NEEDS_ADMIN,
                watch.FAILED_TICK_LINE,
                "no GCP project: set LEEWAY_WATCH_CLUSTERS",
            ),
            (
                watch.fleet_scope_targets.ScopeRenderUnreadable("bad scope.json"),
                watch.FAILED_REASON_SCOPE_UNREADABLE,
                watch.FAILED_NEEDS_ADMIN,
                watch.FAILED_TICK_LINE,
                "ScopeRenderUnreadable: bad scope.json",
            ),
            (
                watch.LedgerUnusable("ledger at /opt/data/leeway/ledger.json is not valid JSON"),
                watch.FAILED_REASON_LEDGER,
                watch.FAILED_NEEDS_ADMIN,
                watch.FAILED_TICK_LINE,
                "LedgerUnusable: ledger at /opt/data/leeway/ledger.json is not valid JSON",
            ),
            (
                watch.SandboxTimedOut("the sandbox run timed out after 900s"),
                watch.FAILED_REASON_TIMED_OUT,
                watch.FAILED_NEXT_DAILY,
                watch.FAILED_TICK_LINE,
                "SandboxTimedOut: the sandbox run timed out after 900s",
            ),
            (
                watch.NoReportReturned({watch.ERROR_KIND_TIMEOUT}),
                watch.FAILED_REASON_TIMED_OUT,
                watch.FAILED_NEXT_DAILY,
                watch.FAILED_TICK_LINE,
                NO_REPORT_TIMED_OUT,
            ),
            (
                watch.NoReportReturned({watch.ERROR_KIND_SCAN_FAILED}),
                watch.FAILED_REASON_ERROR,
                watch.FAILED_NEXT_DAILY,
                watch.FAILED_TICK_LINE,
                NO_REPORT_FAILED,
            ),
            (
                watch.NoReportReturned({watch.ERROR_KIND_TIMEOUT, watch.ERROR_KIND_SCAN_FAILED}),
                watch.FAILED_REASON_MIXED,
                watch.FAILED_NEXT_DAILY,
                watch.FAILED_TICK_LINE,
                NO_REPORT_MIXED,
            ),
            (
                RuntimeError("leeway exited 1 without a report: connection refused"),
                watch.FAILED_REASON_ERROR,
                watch.FAILED_NEXT_DAILY,
                watch.FAILED_TICK_LINE,
                "RuntimeError: leeway exited 1 without a report: connection refused",
            ),
            (
                watch.WriteFailed("cannot write ledger at /opt/data/leeway/ledger.json: No space left on device"),
                watch.FAILED_REASON_WRITE,
                watch.FAILED_NEEDS_ADMIN,
                watch.FAILED_WRITE_LINE,
                "cannot write ledger at /opt/data/leeway/ledger.json: No space left on device",
            ),
        )
        for exc, reason, next_step, template, raw_snippet in cases:
            with self.subTest(exc=type(exc).__name__, reason=reason):
                self.clear_marker_and_log()
                lines = watch.record_tick_failure(self.home, exc, dry_run=False)
                expected = template.format(reason=reason, next=next_step)
                self.assertEqual(lines, [expected])
                for forbidden in ("leeway watch:", "RuntimeError", "exited 1", "LEEWAY_WATCH_", "No space left"):
                    self.assertNotIn(forbidden, lines[0])
                self.assertIn(raw_snippet, self.log_text())
                self.assertEqual(
                    (self.home / watch.FAILURE_MARKER_FILE_NAME).read_text(encoding="utf-8").strip(),
                    reason,
                )
                # Repeating a failure with the same reason is suppressed even when raw exception text changes.
                self.assertEqual(watch.record_tick_failure(self.home, exc, dry_run=False), [])

    def test_write_failure_during_tick_exits_zero_and_suppresses_duplicates(self) -> None:
        self.write_roster("30 8 * * *")
        rep = make_report(
            [{"cluster": "p1/us-central1/seeded-d", "status": "ok", "topology_findings": [make_topology_finding()]}],
            T0,
        )
        with mock.patch.object(watch, "roster_clusters_by_project", return_value={"p1": ["p1/us-central1/seeded-d"]}), mock.patch.object(
            watch.sandbox_exec, "run", return_value=completed_json(rep)
        ), mock.patch.object(
            watch, "save_ledger", side_effect=watch.WriteFailed("cannot write ledger: disk full")
        ):
            buf1 = io.StringIO()
            with redirect_stdout(buf1):
                code1 = watch.main([])
            buf2 = io.StringIO()
            with redirect_stdout(buf2):
                code2 = watch.main([])
        self.assertEqual(code1, 0)
        self.assertEqual(
            buf1.getvalue().strip(),
            "**The topology skew check didn't finish today.** It couldn't save its report. "
            "An admin needs to fix this before it can run; the details are in the agent's log.",
        )
        self.assertEqual(code2, 0)
        self.assertEqual(buf2.getvalue(), "")
        self.assertIn("leeway watch: writing the ledger or the report failed: cannot write ledger: disk full", self.log_text())

    def test_non_daily_schedule_uses_next_scheduled_run_sentence(self) -> None:
        self.write_roster("30 8 * * 1")
        lines = watch.record_tick_failure(self.home, RuntimeError("boom"), dry_run=False)
        self.assertEqual(
            lines,
            [
                "**The topology skew check didn't run today.** Reading the clusters failed. "
                "It will try again at its next scheduled run."
            ],
        )

    def test_dry_run_previews_due_and_resolved_messages_and_nothing_due(self) -> None:
        c_skewed = {
            "cluster": "p1/us-central1/seeded-d",
            "status": "ok",
            "topology_findings": [make_topology_finding()],
        }
        c_clean = {
            "cluster": "p1/us-central1/seeded-d",
            "status": "ok",
            "topology_findings": [],
        }
        with mock.patch.object(watch, "roster_clusters_by_project", return_value={"p1": ["p1/us-central1/seeded-d"]}):
            # 1. Seed the ledger with one finding via a real tick, plus a second finding on another cluster that will resolve.
            rep_seed = make_report(
                [
                    c_skewed,
                    {
                        "cluster": "p1/us-central1/other",
                        "status": "ok",
                        "topology_findings": [make_topology_finding(name="old-svc")],
                    },
                ],
                T0,
            )
            with mock.patch.object(
                watch,
                "roster_clusters_by_project",
                return_value={"p1": ["p1/us-central1/seeded-d", "p1/us-central1/other"]},
            ), mock.patch.object(watch.sandbox_exec, "run", return_value=completed_json(rep_seed)), redirect_stdout(
                io.StringIO()
            ):
                watch.tick(now=T0)

            # 2. Dry-run when nothing is due and nothing retired.
            t1 = T0 + timedelta(days=1)
            buf_quiet = io.StringIO()
            with mock.patch.object(
                watch,
                "roster_clusters_by_project",
                return_value={"p1": ["p1/us-central1/seeded-d", "p1/us-central1/other"]},
            ), mock.patch.object(watch.sandbox_exec, "run", return_value=completed_json(rep_seed)), redirect_stdout(
                buf_quiet
            ):
                watch.tick(dry_run=True, now=t1)
            self.assertEqual(buf_quiet.getvalue().strip(), "dry run: nothing due; active findings: 2")

            # 3. Dry-run with both a new due finding and a retired finding.
            rep_both = make_report(
                [
                    {
                        "cluster": "p1/us-central1/seeded-d",
                        "status": "ok",
                        "topology_findings": [make_topology_finding(name="new-svc")],
                    },
                    {"cluster": "p1/us-central1/other", "status": "ok", "topology_findings": []},
                ],
                t1,
            )
            buf_both = io.StringIO()
            with mock.patch.object(
                watch,
                "roster_clusters_by_project",
                return_value={"p1": ["p1/us-central1/seeded-d", "p1/us-central1/other"]},
            ), mock.patch.object(watch.sandbox_exec, "run", return_value=completed_json(rep_both)), redirect_stdout(
                buf_both
            ):
                watch.tick(dry_run=True, now=t1)
            self.assertEqual(
                buf_both.getvalue(),
                "dry run (nothing posted):\n"
                "**1 pod placement problem on cluster seeded-d (new)**\n"
                "- new-svc (shop) on seeded-d: 100% of its pods in one zone; its zone spread rule is not being kept\n"
                "Ask me how to fix any of these. If they're still open, I'll post them again on or after 2026-10-27.\n"
                "dry run (nothing posted):\n"
                "**2 pod placement problems have cleared**\n"
                "- old-svc (shop) on other\n"
                "- checkout-api (shop) on seeded-d\n",
            )
            for removed in (
                "DRY_RUN_WOULD_REPORT",
                "DRY_RUN_WOULD_RETIRE",
                "due_finding_summary",
                "DUE_SUBJECT_NAMESPACED",
                "DUE_SUBJECT_CLUSTER",
                "DUE_SUMMARY_WITH_SHARE",
                "DUE_SUMMARY_NO_SHARE",
                "DUE_CLUSTER_PREFIX",
                "TOPIC_PUBLISH_CLEARED_LINE",
            ):
                self.assertFalse(hasattr(watch, removed), removed)
            _ = c_clean


if __name__ == "__main__":
    unittest.main()
