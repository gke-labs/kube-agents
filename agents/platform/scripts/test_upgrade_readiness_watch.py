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


def member(cluster: str, status: str, target: str = TARGET, readiness: str | None = None) -> dict:
    entry = {"project": "p1", "location": "us-central1-a", "cluster": cluster, "status": status, "target_version": target}
    if readiness is not None:
        entry["readiness"] = {"status": readiness}
    return entry


def envelope(members: list[dict], tables: str = "| table |", errors: list | None = None) -> dict:
    return {"exit": 0, "tables": tables, "report": {"members": members, "errors": errors or []}}


def loader_argv(stdin: str) -> list[str]:
    line = next(line for line in stdin.splitlines() if line.startswith(ARGV_LINE_PREFIX))
    return json.loads(line[len(ARGV_LINE_PREFIX):])


class FakeSandbox:
    """Stands in for sandbox_exec.run: answers the version table and the
    readiness run from canned envelopes and records what was asked."""

    def __init__(self, versions: dict, readiness: dict | None = None, readiness_error: Exception | None = None):
        self.versions = versions
        self.readiness = readiness or versions
        self.readiness_error = readiness_error
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
        body = self.readiness if wanted else self.versions
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
        self.assertIn("graded 1 blocked (p1/us-central1-a/a) and 1 ready", text)
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

    def test_failed_reads_are_listed_in_the_report(self) -> None:
        readiness = envelope(
            [member("a", "lagging", readiness="unknown")],
            errors=[{"project": "p1", "location": "us-central1-a", "cluster": "a", "message": "get-credentials failed: 403"}],
        )
        sandbox = FakeSandbox(envelope([member("a", "lagging")]), readiness)
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn("0 blocked, 0 ready", out)
        text = (self.home / "reports" / TARGET / "latest.md").read_text()
        self.assertIn("Reads that failed during this run", text)
        self.assertIn("- p1/us-central1-a/a: get-credentials failed: 403", text)


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


class DryRun(Base):
    def test_dry_run_runs_no_readiness_and_writes_nothing(self) -> None:
        sandbox = FakeSandbox(envelope([member("a", "lagging")]))
        code, out = self.run_tick(sandbox, ["--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn(f"dry run: would report {TARGET} (new target version) for a", out)
        self.assertEqual(sandbox.kinds(), ["versions"])
        self.assertFalse((self.home / watch.LEDGER_FILE_NAME).exists())

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

    def test_a_failed_readiness_run_leaves_the_ledger_unsaved_so_the_version_is_retried(self) -> None:
        sandbox = FakeSandbox(envelope([member("a", "lagging")]), readiness_error=RuntimeError("sandbox exited 255 without a report: lost connection"))
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn("the tick failed: RuntimeError: sandbox exited 255", out)
        self.assertFalse((self.home / watch.LEDGER_FILE_NAME).exists())
        self.assertFalse((self.home / "reports").exists())
        sandbox = FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="ready")]))
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn("new target version", out)

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
