"""Tests for upgrade_readiness_watch.py: the gate, the files, the lines."""

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


def member(cluster: str, status: str, target: str = TARGET, readiness: str | None = None) -> dict:
    entry = {"project": "p1", "location": "us-central1-a", "cluster": cluster, "status": status, "target_version": target}
    if readiness is not None:
        entry["readiness"] = {"status": readiness}
    return entry


def envelope(members: list[dict], tables: str = "| table |", errors: list | None = None) -> dict:
    return {"exit": 0, "tables": tables, "report": {"members": members, "errors": errors or []}}


class FakeSandbox:
    """Stands in for sandbox_exec.run: answers the version table and the
    readiness run from canned envelopes and records what was asked."""

    def __init__(self, versions: dict, readiness: dict | None = None):
        self.versions = versions
        self.readiness = readiness or versions
        self.calls: list[list[str]] = []

    def run(self, argv, *, timeout, check, stdin):
        argv_line = next(line for line in stdin.splitlines() if line.startswith("ARGV = "))
        wanted = "--readiness" in json.loads(argv_line[len("ARGV = "):])
        self.calls.append(["readiness" if wanted else "versions", str(timeout)])
        body = self.readiness if wanted else self.versions
        return subprocess.CompletedProcess(argv, 0, stdout=f"noise\n{watch.ENVELOPE_SENTINEL}\n{json.dumps(body)}\n", stderr="")


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "watch"
        self.env = mock.patch.dict(os.environ, {watch.WATCH_HOME_ENV: str(self.home)}, clear=False)
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
        self.assertEqual([c[0] for c in sandbox.calls], ["versions", "readiness"])
        self.assertIn(f"new target version {TARGET}, 2 cluster(s) pending (a, c): 1 blocked, 1 ready", out)
        self.assertIn("next refresh after 2026-10-15", out)
        entry = self.ledger()["targets"][TARGET]
        self.assertEqual(entry["first_seen"], NOW.isoformat())
        self.assertEqual(entry["last_report_at"], NOW.isoformat())
        self.assertEqual(entry["pending"], ["p1/us-central1-a/a", "p1/us-central1-a/c"])
        reports = sorted((self.home / "reports" / TARGET).iterdir())
        names = [p.name for p in reports]
        self.assertIn("20261008T071000Z.md", names)
        self.assertIn("20261008T071000Z.json", names)
        self.assertEqual(os.readlink(self.home / "reports" / TARGET / "latest.md"), "20261008T071000Z.md")
        text = (self.home / "reports" / TARGET / "20261008T071000Z.md").read_text()
        self.assertIn("| readiness table |", text)
        self.assertIn("new target version", text)
        self.assertIn("graded 1 blocked and 1 ready", text)

    def test_a_cluster_at_or_above_its_target_is_not_pending(self) -> None:
        sandbox = FakeSandbox(envelope([member("a", "current"), member("b", "ahead"), member("c", "unknown")]))
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual([c[0] for c in sandbox.calls], ["versions"])
        self.assertEqual(self.ledger()["targets"], {})

    def test_two_pending_versions_each_get_a_report_from_one_readiness_run(self) -> None:
        versions = envelope([member("a", "lagging"), member("b", "lagging", target=OLDER_TARGET)])
        readiness = envelope([member("a", "lagging", readiness="ready"), member("b", "lagging", target=OLDER_TARGET, readiness="blocked")])
        sandbox = FakeSandbox(versions, readiness)
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(sandbox.calls.count(["readiness", str(watch.READINESS_TIMEOUT_SECONDS)]), 1)
        self.assertIn(f"{TARGET}, 1 cluster(s) pending (a): 0 blocked, 1 ready", out)
        self.assertIn(f"{OLDER_TARGET}, 1 cluster(s) pending (b): 1 blocked, 0 ready", out)
        self.assertTrue((self.home / "reports" / TARGET / "latest.md").exists())
        self.assertTrue((self.home / "reports" / OLDER_TARGET / "latest.md").exists())


class Refresh(Base):
    def seed(self, last_report: datetime | None) -> None:
        ledger = watch.empty_ledger()
        ledger["targets"][TARGET] = {
            "first_seen": (NOW - timedelta(days=10)).isoformat(),
            "last_report_at": last_report.isoformat() if last_report else None,
            "pending": ["p1/us-central1-a/a"],
        }
        watch.save_ledger(self.home / watch.LEDGER_FILE_NAME, ledger)

    def test_a_version_reported_this_week_is_quiet(self) -> None:
        self.seed(NOW - timedelta(days=3))
        sandbox = FakeSandbox(envelope([member("a", "lagging")]))
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual([c[0] for c in sandbox.calls], ["versions"])
        self.assertEqual(self.ledger()["targets"][TARGET]["last_report_at"], (NOW - timedelta(days=3)).isoformat())
        self.assertEqual(self.ledger()["last_tick"], NOW.isoformat())

    def test_a_version_reported_a_week_ago_is_refreshed(self) -> None:
        self.seed(NOW - timedelta(days=7))
        sandbox = FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="blocked")]))
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn(f"weekly refresh {TARGET}, 1 cluster(s) pending (a): 1 blocked, 0 ready", out)
        self.assertEqual(self.ledger()["targets"][TARGET]["last_report_at"], NOW.isoformat())

    def test_the_refresh_interval_comes_from_the_environment(self) -> None:
        self.seed(NOW - timedelta(days=3))
        with mock.patch.dict(os.environ, {watch.REFRESH_DAYS_ENV: "2"}):
            sandbox = FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="ready")]))
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn("weekly refresh", out)
        self.assertIn("next refresh after 2026-10-10", out)

    def test_a_seen_version_with_no_report_on_record_is_reported_as_new(self) -> None:
        self.seed(None)
        sandbox = FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="ready")]))
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn("new target version", out)


class Retired(Base):
    def test_a_version_no_cluster_is_pending_leaves_the_ledger_with_one_line(self) -> None:
        ledger = watch.empty_ledger()
        ledger["targets"][OLDER_TARGET] = {"first_seen": NOW.isoformat(), "last_report_at": NOW.isoformat(), "pending": ["p1/us-central1-a/b"]}
        watch.save_ledger(self.home / watch.LEDGER_FILE_NAME, ledger)
        sandbox = FakeSandbox(envelope([member("b", "current", target=OLDER_TARGET)]))
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), f"upgrade readiness: {OLDER_TARGET} is no longer pending on any cluster; retired from the watch")
        self.assertEqual(self.ledger()["targets"], {})


class DryRun(Base):
    def test_dry_run_runs_no_readiness_and_writes_nothing(self) -> None:
        sandbox = FakeSandbox(envelope([member("a", "lagging")]))
        code, out = self.run_tick(sandbox, ["--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn(f"dry run: would report {TARGET} (new target version) for a", out)
        self.assertEqual([c[0] for c in sandbox.calls], ["versions"])
        self.assertFalse((self.home / watch.LEDGER_FILE_NAME).exists())

    def test_dry_run_says_when_nothing_is_due(self) -> None:
        sandbox = FakeSandbox(envelope([member("a", "current")]))
        code, out = self.run_tick(sandbox, ["--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("dry run: nothing due; pending versions: none", out)


class Failures(Base):
    def test_a_sandbox_run_without_an_envelope_is_one_line_and_exit_zero(self) -> None:
        def broken(argv, *, timeout, check, stdin):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="gcloud: permission denied")

        out = io.StringIO()
        with mock.patch.object(watch.sandbox_exec, "run", side_effect=broken), redirect_stdout(out):
            code = watch.main([])
        self.assertEqual(code, 0)
        self.assertIn("upgrade readiness watch: the tick failed: RuntimeError: sandbox exited 1 without a report: gcloud: permission denied", out.getvalue())
        self.assertFalse((self.home / watch.LEDGER_FILE_NAME).exists())

    def test_a_foreign_file_at_the_ledger_path_is_refused_not_overwritten(self) -> None:
        self.home.mkdir(parents=True)
        (self.home / watch.LEDGER_FILE_NAME).write_text('{"something": "else"}')
        sandbox = FakeSandbox(envelope([member("a", "lagging")]))
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn("is not this job's ledger; refusing to overwrite it", out)
        self.assertEqual(sandbox.calls, [])
        self.assertEqual((self.home / watch.LEDGER_FILE_NAME).read_text(), '{"something": "else"}')

    def test_an_unsaveable_ledger_exits_non_zero(self) -> None:
        sandbox = FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="ready")]))
        with mock.patch.object(watch, "save_ledger", side_effect=OSError("read-only file system")):
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, watch.LEDGER_UNSAVED_EXIT)
        self.assertIn("saving the ledger or the report failed: read-only file system", out)


class Loader(unittest.TestCase):
    def test_the_loader_registers_the_readiness_module_and_calls_main(self) -> None:
        source = watch.loader_source(["--output", watch.SANDBOX_OUTPUT_PATH])
        compile(source, "loader", "exec")
        self.assertIn("sys.modules['upgrade_readiness']".replace("'", '"'), source)
        self.assertIn("report.main(ARGV)", source)
        self.assertIn(watch.ENVELOPE_SENTINEL, source)

    def test_the_report_argv_names_the_sandbox_side_paths(self) -> None:
        self.assertEqual(
            watch.report_argv(readiness=True),
            ["--output", watch.SANDBOX_OUTPUT_PATH, "--state-dir", watch.SANDBOX_STATE_DIR, "--readiness", "--kubeconfig-dir", watch.SANDBOX_KUBECONFIG_DIR],
        )
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1, p2"}):
            self.assertEqual(watch.report_argv(readiness=False)[-4:], ["--project", "p1", "--project", "p2"])

    def test_the_loader_runs_the_checkout_copy_of_the_report_script(self) -> None:
        """End to end against the real scripts, with gcloud answering no
        projects: main returns its usage exit and the envelope still arrives."""
        source = watch.loader_source(["--output", watch.SANDBOX_OUTPUT_PATH, "--project", "p1", "--state-dir", watch.SANDBOX_STATE_DIR])
        env = {"PATH": str(Path(tempfile.mkdtemp())), "HOME": tempfile.mkdtemp()}
        fake_gcloud = Path(env["PATH"]) / "gcloud"
        fake_gcloud.write_text("#!/bin/sh\necho '[]'\n")
        fake_gcloud.chmod(0o755)
        completed = subprocess.run([sys.executable, "-I", "-"], input=source, capture_output=True, text=True, env=env, timeout=120)
        self.assertIn(watch.ENVELOPE_SENTINEL, completed.stdout, completed.stderr)
        body = json.loads(completed.stdout.rsplit(watch.ENVELOPE_SENTINEL, 1)[1])
        self.assertEqual(body["exit"], 0)
        self.assertEqual(body["report"]["members"], [])
        self.assertIn("project", body["tables"])


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
        self.assertEqual(entry["risk"], "low")


if __name__ == "__main__":
    unittest.main()
