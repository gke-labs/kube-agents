"""Tests for upgrade_readiness_watch.py: the gate, the files, the lines, the projects."""

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
import upgrade_readiness_watch as watch  # noqa: E402

ROSTER = Path(__file__).resolve().parents[1] / "cron" / "jobs.json"
NOW = datetime(2026, 10, 8, 7, 10, tzinfo=timezone.utc)
TARGET = "1.35.8-gke.1225000"
OLDER_TARGET = "1.34.9-gke.1000000"
ARGV_LINE_PREFIX = "ARGV = "
HERMES_NO_AGENT_KILL_SECONDS = 3600


def member(cluster: str, status: str, target: str = TARGET, readiness: str | None = None, project: str = "p1") -> dict:
    entry = {"project": project, "location": "us-central1-a", "cluster": cluster, "status": status, "target_version": target}
    if readiness is not None:
        entry["readiness"] = {"status": readiness}
    return entry


def envelope(members: list[dict], tables: str = "| table |", errors: list | None = None, exit_code: int = 0) -> dict:
    return {"exit": exit_code, "tables": tables, "report": {"members": members, "errors": errors or []}}


def loader_argv(stdin: str) -> list[str]:
    line = next(line for line in stdin.splitlines() if line.startswith(ARGV_LINE_PREFIX))
    return json.loads(line[len(ARGV_LINE_PREFIX):])


class FakeSandbox:
    """Stands in for sandbox_exec.run: answers the version table and the
    readiness run from canned envelopes and records what was asked."""

    def __init__(
        self,
        versions: dict,
        readiness: dict | None = None,
        readiness_error: Exception | None = None,
        failing_projects: set | None = None,
        garbled_projects: set | None = None,
    ):
        self.versions = versions
        self.readiness = readiness or versions
        self.readiness_error = readiness_error
        self.failing_projects = failing_projects or set()
        self.garbled_projects = garbled_projects or set()
        self.calls: list[tuple[str, list[str], float]] = []

    def run(self, argv, *, timeout, check, stdin=None):
        if stdin is None:
            self.calls.append(("gcloud", list(argv), timeout))
            return subprocess.CompletedProcess(argv, 0, stdout="configured-project\n", stderr="")
        report_argv = loader_argv(stdin)
        wanted = watch.READINESS_FLAG in report_argv
        self.calls.append(("readiness" if wanted else "versions", report_argv, timeout))
        if wanted and self.readiness_error is not None:
            raise self.readiness_error
        if wanted and any(p in self.failing_projects for p in report_argv[1::2]):
            return subprocess.CompletedProcess(argv, 255, stdout="", stderr="ssh: lost connection")
        if wanted and any(p in self.garbled_projects for p in report_argv[1::2]):
            return subprocess.CompletedProcess(argv, 0, stdout=f"{watch.ENVELOPE_SENTINEL}\n{{\"exit\": 0, \"tables\": \"| cut", stderr="")
        body = self.readiness if wanted else self.versions
        if wanted:
            projects = set(report_argv[1::2])
            body = dict(body, report=dict(body["report"], members=[m for m in body["report"]["members"] if m["project"] in projects]))
        return subprocess.CompletedProcess(argv, 0, stdout=f"noise\n{watch.ENVELOPE_SENTINEL}\n{json.dumps(body)}\n", stderr="")

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "watch"
        self.env = mock.patch.dict(os.environ, {watch.WATCH_HOME_ENV: str(self.home), watch.PROJECTS_ENV: "p1"}, clear=False)
        self.env.start()
        self.clock = mock.patch.object(watch, "now_utc", return_value=NOW)
        self.clock.start()

    def tearDown(self) -> None:
        self.clock.stop()
        self.env.stop()
        self.tmp.cleanup()

    def run_tick(self, sandbox: FakeSandbox, argv: list[str] | None = None) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.object(watch.sandbox_exec, "run", side_effect=sandbox.run), redirect_stdout(out):
            code = watch.main(argv or [])
        return code, out.getvalue()

    def ledger(self) -> dict:
        return json.loads((self.home / watch.LEDGER_FILE_NAME).read_text())

    def marker(self) -> Path:
        return self.home / watch.FAILURE_MARKER_FILE_NAME

    @staticmethod
    def monotonic_readings(*readings: float):
        """``watch.time`` with ``monotonic`` answering the readings in turn and
        the last one afterwards; only the module under test sees it."""
        values = iter(readings)
        last = readings[-1]
        return mock.patch.object(watch, "time", mock.Mock(monotonic=lambda: next(values, last)))

    def seed(self, version: str, last_report: datetime | None, pending: list[str] | None = None) -> None:
        ledger = watch.empty_ledger()
        ledger["targets"][version] = {
            "first_seen": (NOW - timedelta(days=10)).isoformat(),
            "last_report_at": last_report.isoformat() if last_report else None,
            "pending": pending or ["p1/us-central1-a/a"],
        }
        watch.save_ledger(self.home / watch.LEDGER_FILE_NAME, ledger)


class NewVersion(Base):
    def test_a_new_pending_version_earns_a_report_and_one_line(self) -> None:
        versions = envelope([member("a", "lagging"), member("b", "current"), member("c", "patch-behind")])
        readiness = envelope(
            [member("a", "lagging", readiness="blocked"), member("b", "current", readiness="ready"), member("c", "patch-behind", readiness="ready")],
            tables="| readiness table |",
        )
        sandbox = FakeSandbox(versions, readiness)
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(sandbox.kinds(), ["versions", "readiness"])
        self.assertEqual(sandbox.calls[0][1], ["--project", "p1"])
        self.assertEqual(sandbox.calls[1][1], ["--project", "p1", "--readiness", "--kubeconfig-dir", watch.SANDBOX_KUBECONFIG_DIR])
        self.assertIn(f"new target version {TARGET}, 2 cluster(s) pending (a, c): 1 blocked (a), 1 ready", out)
        self.assertIn("report on the gateway pod at", out)
        self.assertIn("next refresh after 2026-10-15", out)
        entry = self.ledger()["targets"][TARGET]
        self.assertEqual(entry["first_seen"], NOW.isoformat())
        self.assertEqual(entry["last_report_at"], NOW.isoformat())
        self.assertEqual(entry["pending"], ["p1/us-central1-a/a", "p1/us-central1-a/c"])
        names = [p.name for p in sorted((self.home / "reports" / TARGET).iterdir())]
        self.assertIn("20261008T071000Z.md", names)
        self.assertIn("20261008T071000Z.json", names)
        self.assertEqual(os.readlink(self.home / "reports" / TARGET / "latest.md"), "20261008T071000Z.md")
        text = (self.home / "reports" / TARGET / "20261008T071000Z.md").read_text()
        self.assertIn("| readiness table |", text)
        self.assertIn("new target version", text)
        self.assertIn("graded 1 blocked (p1/us-central1-a/a), 1 ready.", text)
        self.assertIn("first-run baseline", text)

    def test_the_report_script_s_output_path_line_is_not_saved(self) -> None:
        readiness = envelope([member("a", "lagging", readiness="ready")], tables="| t |\nWrote 1 member(s) to /tmp/upgrade-readiness-watch-x/report.json\n")
        sandbox = FakeSandbox(envelope([member("a", "lagging")]), readiness)
        self.run_tick(sandbox)
        text = (self.home / "reports" / TARGET / "latest.md").read_text()
        self.assertIn("| t |", text)
        self.assertNotIn("Wrote 1 member(s)", text)

    def test_a_cluster_at_or_above_its_target_is_not_pending(self) -> None:
        sandbox = FakeSandbox(envelope([member("a", "current"), member("b", "ahead"), member("c", "unknown"), member("d", "lagging", target=None)]))
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(sandbox.kinds(), ["versions"])
        self.assertEqual(self.ledger()["targets"], {})

    def test_two_pending_versions_each_get_a_report_from_one_readiness_run(self) -> None:
        versions = envelope([member("a", "lagging"), member("b", "lagging", target=OLDER_TARGET)])
        readiness = envelope([member("a", "lagging", readiness="ready"), member("b", "lagging", target=OLDER_TARGET, readiness="blocked")])
        sandbox = FakeSandbox(versions, readiness)
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(sandbox.kinds().count("readiness"), 1)
        self.assertEqual([c[2] for c in sandbox.calls], [watch.VERSION_TABLE_TIMEOUT_SECONDS, watch.READINESS_TIMEOUT_SECONDS])
        self.assertIn(f"{TARGET}, 1 cluster(s) pending (a): 0 blocked, 1 ready", out)
        self.assertIn(f"{OLDER_TARGET}, 1 cluster(s) pending (b): 1 blocked (b), 0 ready", out)
        self.assertTrue((self.home / "reports" / TARGET / "latest.md").exists())
        self.assertTrue((self.home / "reports" / OLDER_TARGET / "latest.md").exists())

    def test_a_version_none_of_whose_clusters_were_graded_is_written_but_not_recorded(self) -> None:
        readiness = envelope(
            [member("a", "lagging", readiness="unknown")],
            errors=[{"project": "p1", "location": "us-central1-a", "cluster": "a", "message": "get-credentials failed: 403"}],
        )
        sandbox = FakeSandbox(envelope([member("a", "lagging")]), readiness)
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn(f"new target version {TARGET}, 1 cluster(s) pending (a): none graded (their kubectl read failed or the run returned nothing for them)", out)
        self.assertIn("retrying tomorrow", out)
        self.assertNotIn("0 blocked", out)
        self.assertIsNone(self.ledger()["targets"][TARGET]["last_report_at"])
        text = (self.home / "reports" / TARGET / "latest.md").read_text()
        self.assertIn("1 not read (p1/us-central1-a/a; their kubectl read failed or the run returned nothing for them)", text)
        self.assertIn("Reads that failed during this run", text)
        self.assertIn("- p1/us-central1-a/a: get-credentials failed: 403", text)

    def test_an_unknown_verdict_is_graded_and_recorded_not_retried(self) -> None:
        readiness = envelope([member("a", "lagging", readiness="unknown")])
        sandbox = FakeSandbox(envelope([member("a", "lagging")]), readiness)
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn("0 blocked, 0 ready, 1 unknown;", out)
        self.assertNotIn("retrying tomorrow", out)
        self.assertEqual(self.ledger()["targets"][TARGET]["last_report_at"], NOW.isoformat())
        text = (self.home / "reports" / TARGET / "latest.md").read_text()
        self.assertIn("1 unknown (p1/us-central1-a/a; the table says what it could not decide)", text)

    def test_a_partly_graded_version_counts_the_ungraded_clusters_in_the_line(self) -> None:
        readiness = envelope([member("a", "lagging", readiness="ready")])
        sandbox = FakeSandbox(envelope([member("a", "lagging"), member("b", "lagging")]), readiness)
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn("0 blocked, 1 ready, 1 not read;", out)
        self.assertEqual(self.ledger()["targets"][TARGET]["last_report_at"], NOW.isoformat())

    def test_readiness_runs_once_per_project_and_a_failed_project_leaves_only_its_clusters_ungraded(self) -> None:
        versions = envelope([member("a", "lagging"), member("b", "lagging", project="p2", target=OLDER_TARGET)])
        readiness = envelope([member("a", "lagging", readiness="ready"), member("b", "lagging", project="p2", target=OLDER_TARGET, readiness="ready")])
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            sandbox = FakeSandbox(versions, readiness, failing_projects={"p2"})
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertCountEqual([c[1] for c in sandbox.calls if c[0] == "readiness"], [["--project", "p1", "--readiness", "--kubeconfig-dir", watch.SANDBOX_KUBECONFIG_DIR], ["--project", "p2", "--readiness", "--kubeconfig-dir", watch.SANDBOX_KUBECONFIG_DIR]])
        self.assertIn(f"{TARGET}, 1 cluster(s) pending (a): 0 blocked, 1 ready;", out)
        self.assertIn(f"{OLDER_TARGET}, 1 cluster(s) pending (b): none graded (readiness run for p2 failed: sandbox exited 255 without a report: ssh: lost connection)", out)
        self.assertEqual(self.ledger()["targets"][TARGET]["last_report_at"], NOW.isoformat())
        self.assertIsNone(self.ledger()["targets"][OLDER_TARGET]["last_report_at"])


class Refresh(Base):
    def test_a_version_reported_this_week_is_quiet(self) -> None:
        self.seed(TARGET, NOW - timedelta(days=3))
        sandbox = FakeSandbox(envelope([member("a", "lagging")]))
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(sandbox.kinds(), ["versions"])
        self.assertEqual(self.ledger()["targets"][TARGET]["last_report_at"], (NOW - timedelta(days=3)).isoformat())
        self.assertEqual(self.ledger()["last_tick"], NOW.isoformat())

    def test_a_version_reported_a_week_ago_is_refreshed(self) -> None:
        self.seed(TARGET, NOW - timedelta(days=7))
        sandbox = FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="blocked")]))
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn(f"scheduled refresh {TARGET}, 1 cluster(s) pending (a): 1 blocked (a), 0 ready", out)
        self.assertEqual(self.ledger()["targets"][TARGET]["last_report_at"], NOW.isoformat())

    def test_a_tick_a_few_seconds_short_of_a_week_still_refreshes(self) -> None:
        self.seed(TARGET, NOW - timedelta(days=7) + timedelta(seconds=45))
        sandbox = FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="ready")]))
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn("scheduled refresh", out)

    def test_a_last_report_in_the_future_counts_as_never_reported(self) -> None:
        self.seed(TARGET, NOW + timedelta(days=3))
        sandbox = FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="ready")]))
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn("new target version", out)
        self.assertEqual(self.ledger()["targets"][TARGET]["last_report_at"], NOW.isoformat())

    def test_the_refresh_interval_comes_from_the_environment(self) -> None:
        self.seed(TARGET, NOW - timedelta(days=3))
        with mock.patch.dict(os.environ, {watch.REFRESH_DAYS_ENV: "2"}):
            sandbox = FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="ready")]))
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn("scheduled refresh", out)
        self.assertIn("next refresh after 2026-10-10", out)

    def test_a_seen_version_with_no_report_on_record_is_reported_as_new(self) -> None:
        self.seed(TARGET, None)
        sandbox = FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="ready")]))
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn("new target version", out)


class Retired(Base):
    def test_a_version_no_cluster_is_pending_leaves_the_ledger_with_one_line(self) -> None:
        self.seed(OLDER_TARGET, NOW, ["p1/us-central1-a/b"])
        sandbox = FakeSandbox(envelope([member("b", "current", target=OLDER_TARGET)]))
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), f"upgrade readiness: {OLDER_TARGET} is no longer pending on any cluster; retired from the watch")
        self.assertEqual(self.ledger()["targets"], {})


    def test_a_partial_version_table_retires_nothing_and_says_so(self) -> None:
        self.seed(OLDER_TARGET, NOW, ["p1/us-central1-a/b"])
        partial = envelope([member("a", "current")], errors=[{"project": "p1", "message": "clusters list failed"}], exit_code=1)
        sandbox = FakeSandbox(partial)
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "upgrade readiness watch: the version table was partial (1 read error(s), exit 1); nothing retired while it stays so")
        self.assertIn(OLDER_TARGET, self.ledger()["targets"])


    def test_a_persisting_partial_table_is_announced_once_and_its_recovery_once(self) -> None:
        self.seed(OLDER_TARGET, NOW, ["p1/us-central1-a/b"])
        partial = envelope([member("a", "current")], errors=[{"project": "p1", "message": "clusters list failed"}], exit_code=1)
        code, out1 = self.run_tick(FakeSandbox(partial))
        code, out2 = self.run_tick(FakeSandbox(partial))
        self.assertIn("the version table was partial", out1)
        self.assertEqual(out2, "")
        code, out3 = self.run_tick(FakeSandbox(envelope([member("b", "current", target=OLDER_TARGET)])))
        self.assertIn("the version table reads every project again", out3)
        self.assertIn(f"{OLDER_TARGET} is no longer pending", out3)

    def test_a_persisting_ungraded_version_is_announced_once(self) -> None:
        readiness = envelope([], errors=[{"project": "p1", "location": "us-central1-a", "cluster": "a", "message": "403"}])
        code, out1 = self.run_tick(FakeSandbox(envelope([member("a", "lagging")]), readiness))
        code, out2 = self.run_tick(FakeSandbox(envelope([member("a", "lagging")]), readiness))
        self.assertIn("none graded", out1)
        self.assertEqual(out2, "")
        code, out3 = self.run_tick(FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="ready")])))
        self.assertIn("1 ready;", out3)


    def test_a_blocked_verdict_stands_when_the_kubectl_read_failed(self) -> None:
        readiness = envelope(
            [member("a", "lagging", readiness="blocked")],
            errors=[{"project": "p1", "location": "us-central1-a", "cluster": "a", "message": "kubectl get timed out"}],
        )
        sandbox = FakeSandbox(envelope([member("a", "lagging")]), readiness)
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn("1 blocked (a), 0 ready;", out)
        self.assertEqual(self.ledger()["targets"][TARGET]["last_report_at"], NOW.isoformat())

    def test_an_unreadable_cluster_is_retried_three_days_then_parked_until_the_weekly_refresh(self) -> None:
        readiness = envelope([member("a", "lagging", readiness="unknown")], errors=[{"project": "p1", "location": "us-central1-a", "cluster": "a", "message": "403"}])
        outs = []
        for _ in range(4):
            code, out = self.run_tick(FakeSandbox(envelope([member("a", "lagging")]), readiness))
            outs.append(out)
        self.assertIn("none graded", outs[0])
        self.assertEqual(outs[1], "")
        self.assertIn("not graded on 3 consecutive attempt(s)", outs[2])
        self.assertIn("next attempt at the weekly refresh", outs[2])
        self.assertNotIn("retrying tomorrow", outs[2])
        self.assertEqual(self.ledger()["targets"][TARGET]["last_report_at"], NOW.isoformat())
        self.assertEqual(outs[3], "")
        # A week later the version is still unreadable: parked again at once, no new daily ladder.
        with mock.patch.object(watch, "now_utc", return_value=NOW + timedelta(days=7)):
            code, out = self.run_tick(FakeSandbox(envelope([member("a", "lagging")]), readiness))
        self.assertIn("not graded on 4 consecutive attempt(s)", out)
        self.assertEqual(self.ledger()["targets"][TARGET]["last_report_at"], (NOW + timedelta(days=7)).isoformat())
        # Graded at last: the attempt record is gone.
        with mock.patch.object(watch, "now_utc", return_value=NOW + timedelta(days=14)):
            code, out = self.run_tick(FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="ready")])))
        self.assertIn("1 ready;", out)
        self.assertNotIn(TARGET, self.ledger()["announced"].get("ungraded", {}))

    def test_a_server_config_failure_leaves_the_location_s_unknown_members_unread(self) -> None:
        readiness = envelope(
            [member("a", "lagging", readiness="unknown")],
            errors=[{"project": "p1", "location": "us-central1-a", "message": "get-server-config timed out"}],
        )
        sandbox = FakeSandbox(envelope([member("a", "lagging")]), readiness)
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn("none graded", out)
        self.assertIsNone(self.ledger()["targets"][TARGET]["last_report_at"])

    def test_a_failed_project_run_is_named_even_when_another_project_graded(self) -> None:
        versions = envelope([member("a", "lagging"), member("b", "lagging", project="p2")])
        readiness = envelope([member("a", "lagging", readiness="ready"), member("b", "lagging", project="p2", readiness="ready")])
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            sandbox = FakeSandbox(versions, readiness, failing_projects={"p2"})
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn("1 ready, 1 not read;", out)
        self.assertIn("readiness run for p2 failed: sandbox exited 255 without a report: ssh: lost connection", out)
        text = (self.home / "reports" / TARGET / "latest.md").read_text()
        self.assertIn("- p2: readiness run for p2 failed", text)

    def test_a_malformed_announced_block_is_refused(self) -> None:
        for block in (None, [], {"partial": 5}, {"ungraded": "x"}, {"ungraded": {TARGET: "old-string-shape"}}):
            with self.subTest(block=block):
                self.home.mkdir(parents=True, exist_ok=True)
                self.marker().unlink(missing_ok=True)
                (self.home / watch.LEDGER_FILE_NAME).write_text(json.dumps({"targets": {}, "announced": block}))
                sandbox = FakeSandbox(envelope([member("a", "lagging")]))
                code, out = self.run_tick(sandbox)
                self.assertEqual(code, 0)
                if block is None:
                    self.assertNotIn("malformed", out)
                    self.assertNotIn("the tick failed", out)
                    self.assertEqual(sandbox.kinds(), ["versions", "readiness"])
                    self.assertIsInstance(self.ledger()["announced"], dict)
                else:
                    self.assertIn("has a malformed announced block; refusing to overwrite it", out)
                    self.assertEqual(sandbox.calls, [])


    def test_a_null_ungraded_map_in_the_announced_block_is_the_absent_one(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        ledger = watch.empty_ledger()
        ledger["targets"][OLDER_TARGET] = {"first_seen": NOW.isoformat(), "last_report_at": NOW.isoformat(), "pending": ["p1/us-central1-a/b"]}
        ledger["announced"] = {"ungraded": None}
        (self.home / watch.LEDGER_FILE_NAME).write_text(json.dumps(ledger))
        sandbox = FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="ready")]))
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertNotIn("the tick failed", out)
        self.assertIn("new target version", out)
        self.assertIn(f"{OLDER_TARGET} is no longer pending", out)
        self.assertEqual(self.ledger()["announced"]["ungraded"], {})

    def test_a_failed_server_config_read_keeps_its_location_s_clusters_pending_as_not_read(self) -> None:
        self.seed(TARGET, NOW - timedelta(days=8), ["p1/us-central1-a/a", "p1/us-west1-a/b"])
        table = envelope(
            [member("a", "lagging"), dict(member("b", "unknown", target=None), location="us-west1-a")],
            errors=[{"project": "p1", "location": "us-west1-a", "message": "get-server-config timed out"}],
            exit_code=1,
        )
        readiness = envelope([member("a", "lagging", readiness="ready")])
        code, out = self.run_tick(FakeSandbox(table, readiness))
        self.assertEqual(code, 0)
        self.assertIn("2 cluster(s) pending (a, b): 0 blocked, 1 ready, 1 not read;", out)
        self.assertEqual(self.ledger()["targets"][TARGET]["pending"], ["p1/us-central1-a/a", "p1/us-west1-a/b"])

    def test_an_oversized_refresh_interval_falls_back_to_the_default(self) -> None:
        self.seed(TARGET, NOW - timedelta(days=3))
        with mock.patch.dict(os.environ, {watch.REFRESH_DAYS_ENV: "99999999999"}):
            sandbox = FakeSandbox(envelope([member("a", "lagging")]))
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(self.ledger()["last_tick"], NOW.isoformat())

    def test_a_failed_project_listing_keeps_its_clusters_pending_as_not_read(self) -> None:
        self.seed(TARGET, NOW - timedelta(days=8), ["p1/us-central1-a/a", "p2/us-central1-a/b"])
        partial = envelope([member("a", "lagging")], errors=[{"project": "p2", "message": "clusters list failed"}], exit_code=1)
        readiness = envelope([member("a", "lagging", readiness="ready")])
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            sandbox = FakeSandbox(partial, readiness)
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn("2 cluster(s) pending (a, b): 0 blocked, 1 ready, 1 not read;", out)
        self.assertEqual(self.ledger()["targets"][TARGET]["pending"], ["p1/us-central1-a/a", "p2/us-central1-a/b"])


class Budget(Base):
    """The whole tick stays under the hour Hermes gives a no_agent script."""

    def two_projects(self) -> tuple[dict, dict]:
        versions = envelope([member("a", "lagging"), member("b", "lagging", project="p2")])
        readiness = envelope([member("a", "lagging", readiness="ready"), member("b", "lagging", project="p2", readiness="ready")])
        return versions, readiness

    def test_the_budget_leaves_room_under_the_kill(self) -> None:
        self.assertLess(watch.TICK_BUDGET_SECONDS + watch.MIN_PROJECT_RUN_SECONDS, HERMES_NO_AGENT_KILL_SECONDS)
        self.assertGreater(watch.TICK_BUDGET_SECONDS, watch.VERSION_TABLE_TIMEOUT_SECONDS + watch.READINESS_TIMEOUT_SECONDS)

    def test_a_project_the_budget_cannot_reach_is_left_unread_and_named(self) -> None:
        versions, readiness = self.two_projects()
        first, second = (("p1", "p2") if NOW.toordinal() % 2 == 0 else ("p2", "p1"))
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}), self.monotonic_readings(0.0, 0.0, watch.TICK_BUDGET_SECONDS - 10.0):
            sandbox = FakeSandbox(versions, readiness)
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual([c[1][1] for c in sandbox.calls if c[0] == "readiness"], [first])
        self.assertIn("2 cluster(s) pending (a, b): 0 blocked, 1 ready, 1 not read;", out)
        self.assertIn(f"readiness run for {second} failed: tick budget exhausted before project {second} ran; retried tomorrow", out)
        self.assertEqual(self.ledger()["targets"][TARGET]["last_report_at"], NOW.isoformat())

    def test_a_project_s_timeout_shrinks_to_what_is_left_of_the_budget(self) -> None:
        with self.monotonic_readings(0.0, 2000.0):
            sandbox = FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="ready")]))
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual([c[2] for c in sandbox.calls if c[0] == "readiness"], [watch.TICK_BUDGET_SECONDS - 2000.0])
        self.assertEqual([c[2] for c in sandbox.calls if c[0] == "versions"], [watch.VERSION_TABLE_TIMEOUT_SECONDS])

    def test_a_project_with_the_budget_to_spare_keeps_its_full_timeout(self) -> None:
        with self.monotonic_readings(0.0, 100.0):
            sandbox = FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="ready")]))
            self.run_tick(sandbox)
        self.assertEqual([c[2] for c in sandbox.calls if c[0] == "readiness"], [watch.READINESS_TIMEOUT_SECONDS])

    def test_the_project_order_turns_daily_so_a_skipped_project_is_not_the_same_one(self) -> None:
        versions, readiness = self.two_projects()
        orders = []
        for day in (NOW, NOW + timedelta(days=1)):
            (self.home / watch.LEDGER_FILE_NAME).unlink(missing_ok=True)
            with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}), mock.patch.object(watch, "now_utc", return_value=day):
                sandbox = FakeSandbox(versions, readiness)
                self.run_tick(sandbox)
            orders.append([c[1][1] for c in sandbox.calls if c[0] == "readiness"])
        self.assertCountEqual(orders, [["p1", "p2"], ["p2", "p1"]])


class ProfileHome(unittest.TestCase):
    def test_a_hand_run_from_the_gateway_home_finds_the_profile_s_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "profiles" / "platform").mkdir(parents=True)
            with mock.patch.dict(os.environ, {watch.HOME_ENV: root, watch.WATCH_HOME_ENV: ""}):
                self.assertEqual(watch.watch_home(), Path(root) / "profiles" / "platform" / watch.WATCH_DIR_NAME)
            with mock.patch.dict(os.environ, {watch.HOME_ENV: str(Path(root) / "profiles" / "platform"), watch.WATCH_HOME_ENV: ""}):
                self.assertEqual(watch.watch_home(), Path(root) / "profiles" / "platform" / watch.WATCH_DIR_NAME)


class DryRun(Base):
    def test_dry_run_runs_no_readiness_and_writes_nothing(self) -> None:
        sandbox = FakeSandbox(envelope([member("a", "lagging")]))
        code, out = self.run_tick(sandbox, ["--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn(f"dry run: would report {TARGET} (new target version) for a", out)
        self.assertEqual(sandbox.kinds(), ["versions"])
        self.assertFalse((self.home / watch.LEDGER_FILE_NAME).exists())

    def test_dry_run_says_would_retire_rather_than_retired(self) -> None:
        self.seed(OLDER_TARGET, NOW, ["p1/us-central1-a/b"])
        sandbox = FakeSandbox(envelope([member("b", "current", target=OLDER_TARGET)]))
        code, out = self.run_tick(sandbox, ["--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn(f"dry run: would retire {OLDER_TARGET}", out)
        self.assertNotIn("retired from the watch", out)
        self.assertIn(OLDER_TARGET, self.ledger()["targets"])

    def test_dry_run_says_when_nothing_is_due(self) -> None:
        sandbox = FakeSandbox(envelope([member("a", "current")]))
        code, out = self.run_tick(sandbox, ["--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("dry run: nothing due; pending versions: none", out)


class Failures(Base):
    def test_a_sandbox_run_without_an_envelope_is_one_line_and_exit_zero(self) -> None:
        def broken(argv, *, timeout, check, stdin=None):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="gcloud: permission denied")

        out = io.StringIO()
        with mock.patch.object(watch.sandbox_exec, "run", side_effect=broken), redirect_stdout(out):
            code = watch.main([])
        self.assertEqual(code, 0)
        self.assertIn("upgrade readiness watch: the tick failed: RuntimeError: sandbox exited 1 without a report: gcloud: permission denied", out.getvalue())
        self.assertFalse((self.home / watch.LEDGER_FILE_NAME).exists())

    def test_a_usage_failure_carries_the_script_s_own_stderr(self) -> None:
        def usage(argv, *, timeout, check, stdin=None):
            body = {"exit": 2, "tables": "", "report": None}
            return subprocess.CompletedProcess(argv, 0, stdout=f"{watch.ENVELOPE_SENTINEL}\n{json.dumps(body)}\n", stderr="no project: pass --project\n")

        out = io.StringIO()
        with mock.patch.object(watch.sandbox_exec, "run", side_effect=usage), redirect_stdout(out):
            code = watch.main([])
        self.assertEqual(code, 0)
        self.assertIn("report script exited 2 and wrote no report: no project: pass --project", out.getvalue())

    def test_a_timeout_is_one_short_line_without_the_ssh_command(self) -> None:
        def slow(argv, *, timeout, check, stdin=None):
            raise subprocess.TimeoutExpired(["ssh", "-F", "/dev/null", "hermes@sandbox"], timeout)

        out = io.StringIO()
        with mock.patch.object(watch.sandbox_exec, "run", side_effect=slow), redirect_stdout(out):
            code = watch.main([])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(), f"upgrade readiness watch: the tick failed: RuntimeError: the sandbox run timed out after {watch.VERSION_TABLE_TIMEOUT_SECONDS}s")
        self.assertNotIn("ssh", out.getvalue())

    def test_a_failed_readiness_run_records_no_report_so_the_version_is_retried(self) -> None:
        sandbox = FakeSandbox(envelope([member("a", "lagging")]), readiness_error=RuntimeError("sandbox exited 255 without a report: lost connection"))
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn("none graded (readiness run for p1 failed: sandbox exited 255 without a report: lost connection)", out)
        self.assertIn("retrying tomorrow", out)
        self.assertIsNone(self.ledger()["targets"][TARGET]["last_report_at"])
        sandbox = FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="ready")]))
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn("new target version", out)
        self.assertEqual(self.ledger()["targets"][TARGET]["last_report_at"], NOW.isoformat())

    def test_a_foreign_file_at_the_ledger_path_is_refused_not_overwritten(self) -> None:
        self.home.mkdir(parents=True)
        (self.home / watch.LEDGER_FILE_NAME).write_text('{"something": "else"}')
        sandbox = FakeSandbox(envelope([member("a", "lagging")]))
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn("is not this job's ledger; refusing to overwrite it", out)
        self.assertEqual(sandbox.calls, [])
        self.assertEqual((self.home / watch.LEDGER_FILE_NAME).read_text(), '{"something": "else"}')

    def test_a_malformed_entry_in_the_ledger_is_refused_not_overwritten(self) -> None:
        for bad in ("x", {"pending": ["p1/l/a"], "last_report_at": 5}, {"pending": ["p1/l/a"], "last_report_at": ["2026-10-01"]}, {"pending": "p1/l/a"}):
            with self.subTest(entry=bad):
                self.home.mkdir(parents=True, exist_ok=True)
                self.marker().unlink(missing_ok=True)
                (self.home / watch.LEDGER_FILE_NAME).write_text(json.dumps({"targets": {TARGET: bad}}))
                sandbox = FakeSandbox(envelope([member("a", "lagging")]))
                code, out = self.run_tick(sandbox)
                self.assertEqual(code, 0)
                self.assertIn(f"has a malformed entry for {TARGET}; refusing to overwrite it", out)
                self.assertEqual(sandbox.calls, [])

    def test_a_sentinel_inside_tenant_text_does_not_shift_the_envelope(self) -> None:
        body = envelope([member("a", "lagging")], tables=f"| exclusion {watch.ENVELOPE_SENTINEL} (NO_UPGRADES) |")

        def run(argv, *, timeout, check, stdin=None):
            return subprocess.CompletedProcess(argv, 0, stdout=f"{watch.ENVELOPE_SENTINEL}\n{json.dumps(body)}\n", stderr="")

        out = io.StringIO()
        with mock.patch.object(watch.sandbox_exec, "run", side_effect=run), redirect_stdout(out):
            code = watch.main(["--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("dry run: would report", out.getvalue())

    def test_an_unparsable_ledger_is_refused_not_overwritten(self) -> None:
        self.home.mkdir(parents=True)
        (self.home / watch.LEDGER_FILE_NAME).write_text('{"targets": {')
        sandbox = FakeSandbox(envelope([member("a", "lagging")]))
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn("is not valid JSON", out)
        self.assertEqual(sandbox.calls, [])
        self.assertEqual((self.home / watch.LEDGER_FILE_NAME).read_text(), '{"targets": {')

    def test_a_project_lookup_timeout_is_one_short_line(self) -> None:
        def slow(argv, *, timeout, check, stdin=None):
            raise subprocess.TimeoutExpired(["ssh", "-F", "/dev/null"], timeout)

        out = io.StringIO()
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "", watch.MANAGEMENT_PROJECT_ENV: ""}), mock.patch.object(
            watch, "roster_projects", return_value=set()
        ), mock.patch.object(watch.sandbox_exec, "run", side_effect=slow), redirect_stdout(out):
            code = watch.main([])
        self.assertEqual(code, 0)
        self.assertIn(f"timed out after {watch.PROJECT_LOOKUP_TIMEOUT_SECONDS}s", out.getvalue())
        self.assertNotIn("ssh", out.getvalue())

    def test_no_resolvable_project_fails_closed(self) -> None:
        def unset(argv, *, timeout, check, stdin=None):
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="(unset)\n")

        out = io.StringIO()
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "", watch.MANAGEMENT_PROJECT_ENV: ""}), mock.patch.object(
            watch, "roster_projects", return_value=set()
        ), mock.patch.object(watch.sandbox_exec, "run", side_effect=unset), redirect_stdout(out):
            code = watch.main([])
        self.assertEqual(code, 0)
        self.assertIn("the tick failed: RuntimeError: no GCP project: set UPGRADE_READINESS_PROJECTS or GCP_PROJECT_ID", out.getvalue())
        self.assertFalse((self.home / watch.LEDGER_FILE_NAME).exists())

    def test_a_persisting_failure_is_announced_once_and_its_recovery_once(self) -> None:
        def broken(argv, *, timeout, check, stdin=None):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="gcloud: permission denied")

        def broken_otherwise(argv, *, timeout, check, stdin=None):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="gcloud: quota exceeded")

        outs = []
        for side_effect in (broken, broken, broken_otherwise, broken_otherwise):
            out = io.StringIO()
            with mock.patch.object(watch.sandbox_exec, "run", side_effect=side_effect), redirect_stdout(out):
                self.assertEqual(watch.main([]), 0)
            outs.append(out.getvalue())
        self.assertIn("the tick failed: RuntimeError: sandbox exited 1 without a report: gcloud: permission denied", outs[0])
        self.assertEqual(outs[1], "")
        self.assertIn("the tick failed: RuntimeError: sandbox exited 1 without a report: gcloud: quota exceeded", outs[2])
        self.assertEqual(outs[3], "")
        self.assertFalse((self.home / watch.LEDGER_FILE_NAME).exists())
        self.assertTrue(self.marker().exists())
        dry = FakeSandbox(envelope([member("a", "lagging")]))
        code, out = self.run_tick(dry, ["--dry-run"])
        self.assertNotIn("runs again", out)
        self.assertTrue(self.marker().exists())
        code, out = self.run_tick(FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="ready")])))
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "upgrade readiness watch: the tick runs again")
        self.assertIn("new target version", out)
        self.assertFalse(self.marker().exists())
        code, out = self.run_tick(FakeSandbox(envelope([member("a", "lagging")])))
        self.assertEqual(out, "")

    def test_a_ledger_key_that_is_not_a_version_is_refused_not_overwritten(self) -> None:
        for key in ("../../scratch/x", "latest", "1.35", "1.35.8-gke.1225000/.."):
            with self.subTest(key=key):
                self.home.mkdir(parents=True, exist_ok=True)
                self.marker().unlink(missing_ok=True)
                (self.home / watch.LEDGER_FILE_NAME).write_text(json.dumps({"targets": {key: {"pending": ["p1/l/a"], "last_report_at": None}}}))
                sandbox = FakeSandbox(envelope([member("a", "lagging")]))
                code, out = self.run_tick(sandbox)
                self.assertEqual(code, 0)
                self.assertIn(f"has a malformed entry for {key}; refusing to overwrite it", out)
                self.assertEqual(sandbox.calls, [])
                self.assertFalse((self.home / "reports").exists())

    def test_a_garbled_envelope_from_one_project_fails_only_that_project(self) -> None:
        versions = envelope([member("a", "lagging"), member("b", "lagging", project="p2")])
        readiness = envelope([member("a", "lagging", readiness="ready"), member("b", "lagging", project="p2", readiness="ready")])
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            sandbox = FakeSandbox(versions, readiness, garbled_projects={"p2"})
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertNotIn("the tick failed", out)
        self.assertIn("2 cluster(s) pending (a, b): 0 blocked, 1 ready, 1 not read;", out)
        self.assertIn("readiness run for p2 failed: JSONDecodeError:", out)
        self.assertEqual(len([c for c in sandbox.calls if c[0] == "readiness"]), 2)
        self.assertEqual(self.ledger()["targets"][TARGET]["last_report_at"], NOW.isoformat())

    def test_an_unsaveable_ledger_exits_non_zero(self) -> None:
        sandbox = FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="ready")]))
        with mock.patch.object(watch, "save_ledger", side_effect=OSError("read-only file system")):
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, watch.WRITE_FAILED_EXIT)
        self.assertIn("writing the ledger or the report failed: read-only file system", out)

    def test_an_unwritable_report_exits_non_zero_and_claims_nothing(self) -> None:
        sandbox = FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="ready")]))
        with mock.patch.object(watch, "write_report", side_effect=OSError("disk full")):
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, watch.WRITE_FAILED_EXIT)
        self.assertIn("writing the ledger or the report failed: disk full", out)
        self.assertFalse((self.home / watch.LEDGER_FILE_NAME).exists())


class Projects(unittest.TestCase):
    def test_the_explicit_list_wins(self) -> None:
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p2, p1,p2", watch.MANAGEMENT_PROJECT_ENV: "mgmt"}):
            self.assertEqual(watch.projects(), ["p1", "p2"])

    def test_the_roster_and_the_management_project_otherwise(self) -> None:
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "", watch.MANAGEMENT_PROJECT_ENV: "mgmt"}), mock.patch.object(
            watch, "roster_projects", return_value={"tenant-a", "mgmt"}
        ):
            self.assertEqual(watch.projects(), ["mgmt", "tenant-a"])

    def test_nothing_resolvable_raises_rather_than_widening_the_scope(self) -> None:
        def unset(argv, *, timeout, check, stdin=None):
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="(unset)\n")

        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "", watch.MANAGEMENT_PROJECT_ENV: ""}), mock.patch.object(
            watch, "roster_projects", return_value=set()
        ), mock.patch.object(watch.sandbox_exec, "run", side_effect=unset):
            with self.assertRaises(RuntimeError):
                watch.projects()

    def test_the_sandbox_s_configured_project_as_the_last_resort(self) -> None:
        sandbox = FakeSandbox(envelope([]))
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "", watch.MANAGEMENT_PROJECT_ENV: ""}), mock.patch.object(
            watch, "roster_projects", return_value=set()
        ), mock.patch.object(watch.sandbox_exec, "run", side_effect=sandbox.run):
            self.assertEqual(watch.projects(), ["configured-project"])
        self.assertEqual(sandbox.calls[0][1], list(watch.CONFIG_PROJECT_ARGV))

    def test_the_roster_reads_each_profile_s_identity_and_skips_the_reserved_ones(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            for name in ("default", "platform", "cluster-x", "cluster-y", "cluster-broken"):
                (Path(home) / "profiles" / name).mkdir(parents=True)

            def identity(path: Path):
                if path.name == "cluster-x":
                    return {"project": "tenant-a", "cluster": "x", "location": "l"}
                if path.name == "cluster-y":
                    return {"project": "tenant-b", "cluster": "y", "location": "l"}
                raise OSError("unreadable")

            with mock.patch.object(watch.gitops_workspace, "agent_home", return_value=home), mock.patch(
                "cluster_agent_profile.read_cluster_identity", side_effect=identity
            ):
                self.assertEqual(watch.roster_projects(), {"tenant-a", "tenant-b"})


class Loader(unittest.TestCase):
    def test_the_loader_registers_the_readiness_module_and_calls_main_in_a_private_directory(self) -> None:
        source = watch.loader_source(["--project", "p1"])
        compile(source, "loader", "exec")
        self.assertIn('sys.modules["upgrade_readiness"]', source)
        self.assertIn("report.main(ARGV + ", source)
        self.assertIn("tempfile.mkdtemp(", source)
        self.assertIn("shutil.rmtree(private", source)
        self.assertIn(watch.ENVELOPE_SENTINEL, source)
        self.assertNotIn("/tmp/upgrade-readiness", source)

    def test_the_report_argv_carries_the_projects_and_the_sandbox_side_kubeconfig_dir(self) -> None:
        self.assertEqual(
            watch.report_argv(["p1", "p2"], readiness=True),
            ["--project", "p1", "--project", "p2", "--readiness", "--kubeconfig-dir", watch.SANDBOX_KUBECONFIG_DIR],
        )
        self.assertEqual(watch.report_argv(["p1"], readiness=False), ["--project", "p1"])

    def test_the_loader_runs_the_checkout_copy_of_the_report_script(self) -> None:
        """End to end against the real scripts, with gcloud answering no
        clusters: the envelope arrives and the private directory is gone."""
        source = watch.loader_source(["--project", "p1"])
        with tempfile.TemporaryDirectory() as scratch:
            bin_dir = Path(scratch) / "bin"
            bin_dir.mkdir()
            fake_gcloud = bin_dir / "gcloud"
            fake_gcloud.write_text("#!/bin/sh\necho '[]'\n")
            fake_gcloud.chmod(0o755)
            env = {"PATH": str(bin_dir), "HOME": scratch, "TMPDIR": scratch}
            completed = subprocess.run([sys.executable, "-I", "-"], input=source, capture_output=True, text=True, env=env, timeout=120)
            self.assertIn(watch.ENVELOPE_SENTINEL, completed.stdout, completed.stderr)
            body = json.loads(completed.stdout.rsplit(watch.ENVELOPE_SENTINEL, 1)[1])
            self.assertEqual(body["exit"], 0)
            self.assertEqual(body["report"]["members"], [])
            self.assertIn("project", body["tables"])
            leftovers = [p.name for p in Path(scratch).iterdir() if p.name.startswith(watch.SANDBOX_TMP_PREFIX)]
            self.assertEqual(leftovers, [])


class Roster(unittest.TestCase):
    def test_the_platform_roster_carries_the_job_as_a_daily_no_agent_entry(self) -> None:
        jobs = {j["id"]: j for j in json.loads(ROSTER.read_text(encoding="utf-8"))["jobs"]}
        entry = jobs["upgrade-readiness-watch"]
        self.assertTrue(entry["no_agent"])
        self.assertEqual(entry["script"], "upgrade_readiness_watch.py")
        self.assertEqual(entry["deliver"], "chat")
        self.assertEqual(entry["schedule"]["expr"], entry["schedule"]["display"])
        self.assertEqual(entry["schedule"]["expr"].split()[2:], ["*", "*", "*"], "a daily job")
        self.assertTrue(entry["enabled"])
        self.assertEqual(entry["risk"], "high")


if __name__ == "__main__":
    unittest.main()
