"""Tests for leeway_watch.py: discovery, 7-day gate, cause/severity escalation, reports, and roster."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import leeway_watch as watch  # noqa: E402

ROSTER = Path(__file__).resolve().parent.parent / "cron" / "jobs.json"
T0 = datetime(2026, 10, 19, 8, 30, 0, tzinfo=timezone.utc)


def make_topology_finding(
    name: str = "zone-pinned-api",
    namespace: str = "seeded-topology",
    kind: str = "Deployment",
    replicas: int = 2,
    topology_key: str = "topology.kubernetes.io/zone",
    tier: str = "B",
    severity: str = "warning",
    cause: str = "affinity_conflict",
    actual_a: int = 2,
    actual_b: int = 0,
) -> dict:
    return {
        "kind": "leeway.placement_drift",
        "subject": {"kind": kind, "namespace": namespace, "name": name, "replicas": replicas},
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
        "domains": [
            {"domain": "us-central1-a", "actual": actual_a, "expected": 1, "delta": actual_a - 1, "readyNodes": 1},
            {"domain": "us-central1-b", "actual": actual_b, "expected": 1, "delta": actual_b - 1, "readyNodes": 1},
        ],
        "suspectedCause": cause,
        "contributingFactors": ["pod template pins topology.kubernetes.io/zone=us-central1-a"],
        "remediation": {
            "kind": "manifest_patch",
            "summary": "Remove hard zone nodeSelector and add topologySpreadConstraints.",
            "yaml": "spec:\n  template:\n    spec:\n      topologySpreadConstraints:\n      - maxSkew: 1\n        topologyKey: topology.kubernetes.io/zone\n        whenUnsatisfiable: ScheduleAnyway",
        },
    }


def make_rank_finding(
    compute_class: str = " balanced-spot",
    severity: str = "warning",
    cause: str = "spot_preemption",
) -> dict:
    return {
        "kind": "leeway.rank_cascade",
        "computeClass": compute_class.strip(),
        "tier": "A",
        "severity": severity,
        "activeRank": 1,
        "floorShare": 0.75,
        "unreadyPods": 0,
        "suspectedCause": cause,
        "contributingFactors": ["rank 0 spot pool exhausted in us-central1-a"],
        "remediation": {
            "kind": "node_pool_scale",
            "summary": "Check spot stockout in us-central1-a or widen machine family list.",
        },
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
            },
            clear=False,
        )
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.tmp.cleanup()

    def run_tick(self, fake_run, *, now: datetime = T0, dry_run: bool = False) -> tuple[int, str]:
        buf = io.StringIO()
        with mock.patch.object(watch.sandbox_exec, "run", side_effect=fake_run), redirect_stdout(buf):
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
        self.assertIn("leeway: 2 finding(s) due (2 new finding) across 1 cluster(s) (p1/us-central1/seeded-d)", out)
        self.assertIn("next refresh after 2026-10-26", out)

        # Verify report files and symlinks exist and contain required fields.
        latest_md = self.home / "reports" / "latest.md"
        latest_json = self.home / "reports" / "latest.json"
        self.assertTrue(latest_md.is_symlink())
        self.assertTrue(latest_json.is_symlink())
        md_text = latest_md.read_text(encoding="utf-8")
        self.assertIn("`seeded-topology/Deployment/zone-pinned-api`", md_text)
        self.assertIn("2/2 (100%)", md_text)
        self.assertIn("us-central1-a: 2, us-central1-b: 0", md_text)
        self.assertIn("`affinity_conflict`", md_text)
        self.assertIn("topologySpreadConstraints:", md_text)

        # Next day (T0 + 1 day): unchanged findings are not due -> quiet tick.
        t1 = T0 + timedelta(days=1)
        code2, out2 = self.run_tick(lambda *a, **kw: completed_json(rep), now=t1)
        self.assertEqual(code2, 0)
        self.assertEqual(out2, "")

    def test_cause_change_or_severity_escalation_triggers_immediate_report(self) -> None:
        c1 = {
            "cluster": "p1/us-central1/seeded-d",
            "status": "ok",
            "topology_findings": [make_topology_finding(cause="missing_constraint", severity="warning")],
        }
        self.run_tick(lambda *a, **kw: completed_json(make_report([c1], T0)), now=T0)

        # Next day, suspectedCause shifts to pvc_locality -> immediate report.
        t1 = T0 + timedelta(days=1)
        c2 = {
            "cluster": "p1/us-central1/seeded-d",
            "status": "ok",
            "topology_findings": [make_topology_finding(cause="pvc_locality", severity="warning")],
        }
        code, out = self.run_tick(lambda *a, **kw: completed_json(make_report([c2], t1)), now=t1)
        self.assertEqual(code, 0)
        self.assertIn("1 cause/severity change", out)

        # Day 2, severity escalates from warning to critical -> immediate report.
        t2 = T0 + timedelta(days=2)
        c3 = {
            "cluster": "p1/us-central1/seeded-d",
            "status": "ok",
            "topology_findings": [make_topology_finding(cause="pvc_locality", severity="critical")],
        }
        code3, out3 = self.run_tick(lambda *a, **kw: completed_json(make_report([c3], t2)), now=t2)
        self.assertEqual(code3, 0)
        self.assertIn("1 cause/severity change", out3)

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
        self.assertIn("leeway watch: the fleet scan reads every target again", out_ok)
        self.assertIn("leeway: 1 finding(s) resolved (seeded-d:seeded-topology/zone-pinned-api); retired from the watch", out_ok)
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
            "cluster": "p1/us-central1/seeded-d",
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


class DiscoveryTests(unittest.TestCase):
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
            [["leeway", "report", "--topology-tier-c-signals", "--clusters", "p1/us-central1/c1,p2/us-east1/c2"]],
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


if __name__ == "__main__":
    unittest.main()
