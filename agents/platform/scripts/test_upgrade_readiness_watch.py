"""Tests for upgrade_readiness_watch.py: the gate, the files, the lines, the projects, the scope."""

from __future__ import annotations

import dataclasses
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet_scope_args  # noqa: E402
import fleet_scope_targets  # noqa: E402
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


def scope_targets(
    *projects: str, unread: tuple[tuple[str, str], ...] = (), excluded: tuple[str, ...] = (), retiring: tuple[str, ...] = ()
) -> fleet_scope_targets.ScopeTargets:
    return fleet_scope_targets.ScopeTargets(projects=projects, unread=unread, resolved_at=None, path="fleet_scope.json", excluded=excluded, retiring=retiring)


def declared_snapshot(
    projects: list[str], unread: list[str] | None = None, exclude: list[str] | None = None, containers: list[dict] | None = None, retiring: list[str] | None = None
) -> dict:
    """The reconcile's snapshot for an install whose ``spec.scope`` resolved to
    ``projects`` (the first the management project), with ``unread`` declared
    projects the install could not read (``name`` for a denial, else
    ``name=outcome``; ``unresolved`` is a declared project with no row, as the
    reconcile leaves one it has not resolved), ``exclude`` the declaration's
    ``exclude.projects`` entries, ``containers`` the reconcile's record of
    each folder lookup this run, which puts the folder in the declaration, and
    ``retiring`` the projects the declaration dropped, whose rows the reconcile
    marks so until its next clean run prunes their profiles."""
    outcomes = {name: outcome or "denied" for name, _, outcome in (entry.partition("=") for entry in unread or [])}
    rows = [{"id": p, "outcome": "ok", "state": "in-scope", "via": ["management"] if i == 0 else ["projects"]} for i, p in enumerate(projects)]
    rows += [{"id": p, "outcome": outcome, "state": "in-scope", "via": ["projects"]} for p, outcome in outcomes.items() if outcome != "unresolved"]
    rows += [{"id": p, "outcome": "ok", "state": "retiring", "via": []} for p in retiring or []]
    declared = {
        "projects": projects[1:] + list(outcomes), "folders": [c["id"].split("/")[-1] for c in containers or []],
        "organizations": [], "sharedVpcHosts": [], "metricsScopes": [], "exclude": {"projects": list(exclude or [])},
    }
    snapshot = {"resolvedAt": "2026-10-09T12:00:00Z", "present": True, "boundary": True, "declared": declared, "projects": rows}
    if containers is not None:
        snapshot["containers"] = containers
    return snapshot


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
        table_failing_projects: set | None = None,
        timeout_projects: set | None = None,
        table_timeout_projects: set | None = None,
        aliases: dict | None = None,
        describe_down: set | None = None,
        scope_declared: bool = False,
    ):
        self.versions = versions
        self.readiness = readiness or versions
        self.readiness_error = readiness_error
        self.failing_projects = failing_projects or set()
        self.garbled_projects = garbled_projects or set()
        self.table_failing_projects = table_failing_projects or set()
        self.timeout_projects = timeout_projects or set()
        self.table_timeout_projects = table_timeout_projects or set()
        # A project asked for by number answers with the id the report resolves, as the script does.
        self.aliases = aliases or {}
        # Spellings whose `projects describe` is down: the report keys the run by the spelling itself.
        self.describe_down = describe_down or set()
        # The operator's marker on the sandbox (`sandbox_declares_scope`), set at its start.
        self.scope_declared = scope_declared
        self.calls: list[tuple[str, list[str], float]] = []

    def run(self, argv, *, timeout, check, stdin=None):
        if stdin is None:
            self.calls.append(("gcloud", list(argv), timeout))
            return subprocess.CompletedProcess(argv, 0, stdout="configured-project\n", stderr="")
        report_argv = loader_argv(stdin)
        wanted = watch.READINESS_FLAG in report_argv
        self.calls.append(("readiness" if wanted else "versions", report_argv, timeout))
        # The projects asked for are the --project values alone: the scope flags carry other ids.
        asked = [report_argv[i + 1] for i, flag in enumerate(report_argv) if flag == watch.PROJECT_FLAG]
        if wanted and self.readiness_error is not None:
            raise self.readiness_error
        if wanted and any(p in self.failing_projects for p in asked):
            return subprocess.CompletedProcess(argv, 255, stdout="", stderr="ssh: lost connection")
        if not wanted and any(p in self.table_failing_projects for p in asked):
            return subprocess.CompletedProcess(argv, 255, stdout="", stderr="ssh: lost connection")
        if any(p in (self.timeout_projects if wanted else self.table_timeout_projects) for p in asked):
            raise subprocess.TimeoutExpired(argv, timeout)
        flags = {report_argv[i]: report_argv[i + 1] for i, flag in enumerate(report_argv[:-1]) if flag in (fleet_scope_targets.SCOPE_PROJECTS_FLAG, fleet_scope_targets.SCOPE_UNREAD_FLAG)}
        if flags or self.scope_declared:
            # The reporter's own rule decides, from the handed argv and the sandbox's marker
            # (`DeclaredScope.set`, `override_error`): the fake refuses what the script refuses and
            # reads what it reads, so a scoped tick test cannot stage a read or a refusal the
            # reporter would not make, a marked sandbox's refusal of the bare flag included.
            scope = fleet_scope_args.DeclaredScope()
            with mock.patch.object(fleet_scope_args, "sandbox_declares_scope", return_value=self.scope_declared):
                scope.set(flags.get(fleet_scope_targets.SCOPE_PROJECTS_FLAG), flags.get(fleet_scope_targets.SCOPE_UNREAD_FLAG))
            # On the id the report resolves, as `get_target_projects` checks it: a number
            # resolves through `projects describe` (the aliases) unless describe is down, when
            # the digits reach the rule, which refuses them; an id is checked as it is.
            # Over every --project, as `get_target_projects` decides: one refusal refuses the run.
            resolved_ids = sorted({a if a in self.describe_down else self.resolves_to(a) for a in asked})
            reason = next((r for r in (scope.override_error(p) for p in resolved_ids) if r is not None), None)
            if reason is not None:
                # The script's usage exit, no report, the reason on stderr.
                body = {"exit": 2, "tables": "", "report": None}
                return subprocess.CompletedProcess(argv, 0, stdout=f"{watch.ENVELOPE_SENTINEL}\n{json.dumps(body)}\n", stderr=f"(declared scope: fleet_scope): {reason}\n")
        # A garbled envelope means the reporter ran and printed: staged after its refusal decision.
        if wanted and any(p in self.garbled_projects for p in asked):
            return subprocess.CompletedProcess(argv, 0, stdout=f"{watch.ENVELOPE_SENTINEL}\n{{\"exit\": 0, \"tables\": \"| cut", stderr="")
        body = self.readiness if wanted else self.versions
        # Both hops run once per project: answer with that project's slice.
        down = {a for a in asked if a in self.describe_down}
        resolved = {a: (a if a in down else self.resolves_to(a)) for a in asked}
        projects = {self.resolves_to(a) for a in asked}

        def spelled(project: str) -> str:
            # On a describe outage the script keys members and errors alike by the spelling it was asked for.
            return next((a for a in down if self.aliases.get(a) == project), project)

        members = [dict(m, project=spelled(m["project"])) for m in body["report"]["members"] if m["project"] in projects]
        errors = [dict(e, project=spelled(e["project"])) for e in body["report"].get("errors") or [] if e.get("project") in projects]
        exit_code = body.get("exit", 0) if errors else (1 if down else 0)
        body = dict(body, exit=exit_code, report=dict(body["report"], members=members, errors=errors, projects=sorted(resolved.values())))
        return subprocess.CompletedProcess(argv, 0, stdout=f"noise\n{watch.ENVELOPE_SENTINEL}\n{json.dumps(body)}\n", stderr="")

    def resolves_to(self, spelling: str) -> str:
        """The id the report resolves a spelling to (`_normalise_project_id`): a number
        through `projects describe`, the aliases; anything else as it is."""
        return self.aliases.get(spelling, spelling) if spelling.isdigit() else spelling

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "watch"
        # The scope reader looks for the reconcile's snapshot under the agent
        # home and the operator's render under its variable: an install that
        # declares no scope until a test writes one.
        self.env = mock.patch.dict(
            os.environ,
            {
                watch.WATCH_HOME_ENV: str(self.home),
                # The profile home holds no roster, so the failure line's ending
                # does not depend on the machine's own HERMES_HOME.
                watch.HOME_ENV: self.tmp.name,
                watch.PROJECTS_ENV: "p1",
                watch.REFRESH_DAYS_ENV: "",
                watch.SKILL_SCRIPTS_DIR_ENV: "",
                fleet_scope_targets.AGENT_HOME_ENV: self.tmp.name,
                fleet_scope_targets.SCOPE_FILE_ENV: "",
            },
            clear=False,
        )
        self.env.start()
        self.clock = mock.patch.object(watch, "now_utc", return_value=NOW)
        self.clock.start()

    def declare_scope(
        self, projects: list[str], unread: list[str] | None = None, exclude: list[str] | None = None, containers: list[dict] | None = None, retiring: list[str] | None = None
    ) -> None:
        (Path(self.tmp.name) / fleet_scope_targets.SNAPSHOT_FILE).write_text(
            json.dumps(declared_snapshot(projects, unread, exclude, containers, retiring)), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.clock.stop()
        self.env.stop()
        self.tmp.cleanup()

    @staticmethod
    def failed(detail: str, reason: str = watch.FAILED_REASON_ERROR, admin: bool | None = None) -> str:
        """What a failed tick prints, stdout and stderr in turn: the plain line, then the detail.

        No roster under this profile home, so a failure a retry may clear tries again at
        the next scheduled run; any other says a person has to act."""
        if admin is None:
            admin = reason in watch.FAILED_REASONS_NEED_ADMIN
        then = watch.FAILED_NEEDS_ADMIN if admin else watch.FAILED_NEXT_UNKNOWN
        line = watch.FAILED_TICK_LINE.format(reason=reason, next=then)
        return f"{line}\nupgrade readiness watch: the tick failed: {detail}\n"

    def run_tick(self, sandbox: FakeSandbox, argv: list[str] | None = None) -> tuple[int, str]:
        """``main``'s exit code and what it printed, stdout and stderr in one buffer."""
        out = io.StringIO()
        with mock.patch.object(watch.sandbox_exec, "run", side_effect=sandbox.run), redirect_stdout(out), redirect_stderr(out):
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
        self.assertEqual(
            out.strip(),
            "upgrade readiness watch: the version table was partial (1 read error(s), exit 1): p1: clusters list failed; a version is retired only once "
            "every project that pends it has been read, or a readiness run has listed every cluster pending it as no longer below it",
        )
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


    def test_the_ungraded_line_is_keyed_on_the_projects_and_their_kinds_not_the_error_text(self) -> None:
        outs = []
        for text in ("kex_exchange_identification: read: Connection reset by peer", "Connection timed out during banner exchange"):
            sandbox = FakeSandbox(envelope([member("a", "lagging")]), readiness_error=RuntimeError(f"sandbox exited 255 without a report: {text}"))
            code, out = self.run_tick(sandbox)
            self.assertEqual(code, 0)
            outs.append(out)
        self.assertIn("none graded (readiness run for p1 failed: sandbox exited 255 without a report: kex_exchange_identification: read: Connection reset by peer)", outs[0])
        self.assertEqual(outs[1], "", "the same project failing the same way, reworded, is the condition already announced")
        record = self.ledger()["announced"]["ungraded"][TARGET]
        self.assertEqual(record["signature"], [["p1", "readiness run failed"]])
        self.assertEqual(record["attempts"], 2)
        self.assertIn("Connection timed out during banner exchange", record["detail"], "the detail rides along for the ledger's reader")

    def test_an_ungraded_version_whose_project_fails_another_way_is_announced_again(self) -> None:
        sandbox = FakeSandbox(envelope([member("a", "lagging")]), readiness_error=RuntimeError("sandbox exited 255 without a report: lost connection"))
        code, out1 = self.run_tick(sandbox)
        unread = envelope([], errors=[{"project": "p1", "location": "us-central1-a", "cluster": "a", "message": "403"}])
        code, out2 = self.run_tick(FakeSandbox(envelope([member("a", "lagging")]), unread))
        self.assertIn("none graded (readiness run for p1 failed", out1)
        self.assertIn("none graded (their kubectl read failed or the run returned nothing for them)", out2, "the run is back and the reads fail: a different kind")
        self.assertEqual(self.ledger()["announced"]["ungraded"][TARGET]["signature"], [["p1", "reads failed"]])

    def test_an_ungraded_record_an_earlier_release_wrote_without_a_signature_is_compared_on_its_detail(self) -> None:
        detail = "readiness run for p1 failed: sandbox exited 255 without a report: lost connection"
        ledger = watch.empty_ledger()
        ledger["targets"][TARGET] = {"first_seen": (NOW - timedelta(days=1)).isoformat(), "last_report_at": None, "pending": ["p1/us-central1-a/a"]}
        ledger["announced"] = {"ungraded": {TARGET: {"detail": detail, "attempts": 1, "parked": False}}}
        watch.save_ledger(self.home / watch.LEDGER_FILE_NAME, ledger)
        sandbox = FakeSandbox(envelope([member("a", "lagging")]), readiness_error=RuntimeError("sandbox exited 255 without a report: lost connection"))
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(out, "", "the same detail an earlier release recorded is the condition already announced")
        self.assertEqual(self.ledger()["announced"]["ungraded"][TARGET]["signature"], [["p1", "readiness run failed"]])

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

    def test_a_failed_audit_log_read_leaves_the_member_graded(self) -> None:
        """The report lists a failed audit-log read under ``readiness.audit_log_failures``,
        apart from ``errors``, so the watch's reading of a cluster-keyed error as a kubectl
        read that failed never sees it: the member keeps its verdict."""
        graded = member("a", "lagging", readiness=watch.READINESS_READY)
        report = {
            watch.MEMBERS_KEY: [graded],
            watch.ERRORS_KEY: [],
            watch.READINESS_KEY: {"audit_log_failures": [{"project": "p1", "location": "us-central1-a", "cluster": "a", "read": "removed", "message": "timed out"}]},
        }
        blocked, ready, unknown, unread = watch.readiness_verdicts(report, [watch.member_key(graded)])
        self.assertEqual((blocked, ready, unknown, unread), ([], [watch.member_key(graded)], [], []))

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
        for block in (None, [], {"partial": 5}, {"ungraded": "x"}, {"ungraded": {TARGET: "old-string-shape"}}, {"ungraded": {TARGET: {"detail": "x", "attempts": 1, "signature": "p1"}}}):
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
        self.assertGreater(watch.TICK_BUDGET_SECONDS - watch.TABLE_BUDGET_SECONDS, watch.READINESS_TIMEOUT_SECONDS, "the readiness runs keep at least one full run after the table's share")

    def test_a_table_that_outgrows_its_share_leaves_the_readiness_runs_theirs(self) -> None:
        self.seed(TARGET, NOW - timedelta(days=8), ["p1/us-central1-a/a", "p2/us-central1-a/b"])
        versions, readiness = self.two_projects()
        # started, table p1, table p2 (past the table's share), readiness p1, readiness p2 (both inside the tick's)
        late = watch.TABLE_BUDGET_SECONDS - 10.0
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}), self.monotonic_readings(0.0, 0.0, late, late, late):
            sandbox = FakeSandbox(versions, readiness)
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual([c[1][1] for c in sandbox.calls if c[0] == "versions"], ["p1"])
        self.assertEqual([c[1][1] for c in sandbox.calls if c[0] == "readiness"], ["p1", "p2"])
        self.assertIn("the version table was partial (1 read error(s), exit 0): p2: tick budget exhausted before the version table read project p2", out, "the line names the project the table left unread, and why")
        self.assertIn(f"scheduled refresh {TARGET}, 2 cluster(s) pending (a, b): 0 blocked, 2 ready;", out)
        self.assertEqual(self.ledger()["targets"][TARGET]["last_report_at"], NOW.isoformat())
        self.assertEqual(sorted(self.ledger()["table_runs"]), ["p1"], "the unread project is not stamped, so it goes first tomorrow")

    def test_a_project_the_budget_cannot_reach_is_left_unread_and_named(self) -> None:
        versions, readiness = self.two_projects()
        # started, table p1, table p2, readiness p1, readiness p2 (too late)
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}), self.monotonic_readings(0.0, 0.0, 0.0, 0.0, watch.TICK_BUDGET_SECONDS - 10.0):
            sandbox = FakeSandbox(versions, readiness)
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual([c[1][1] for c in sandbox.calls if c[0] == "readiness"], ["p1"])
        self.assertIn("2 cluster(s) pending (a, b): 0 blocked, 1 ready, 1 not read;", out)
        self.assertIn("next refresh after 2026-10-15; readiness run for p2 not started: tick budget exhausted before project p2 could run", out)
        self.assertNotIn("retried tomorrow", out)
        self.assertEqual(self.ledger()["targets"][TARGET]["last_report_at"], NOW.isoformat())
        self.assertEqual(sorted(self.ledger()["readiness_runs"]), ["p1"], "only the project that ran is stamped")
        text = (self.home / "reports" / TARGET / "latest.md").read_text()
        self.assertIn("- p2: readiness run for p2 not started", text)

    def test_the_next_sweep_starts_at_the_project_the_budget_left_unread(self) -> None:
        versions, readiness = self.two_projects()
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}), self.monotonic_readings(0.0, 0.0, 0.0, 0.0, watch.TICK_BUDGET_SECONDS - 10.0):
            self.run_tick(FakeSandbox(versions, readiness))
        ledger = self.ledger()
        ledger["targets"][TARGET]["last_report_at"] = (NOW - timedelta(days=8)).isoformat()
        watch.save_ledger(self.home / watch.LEDGER_FILE_NAME, ledger)
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            sandbox = FakeSandbox(versions, readiness)
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual([c[1][1] for c in sandbox.calls if c[0] == "readiness"], ["p2", "p1"])
        self.assertIn("2 cluster(s) pending (a, b): 0 blocked, 2 ready;", out)
        self.assertEqual(sorted(self.ledger()["readiness_runs"]), ["p1", "p2"])

    def test_a_version_whose_only_project_was_not_started_is_not_recorded_or_parked(self) -> None:
        versions = envelope([member("a", "lagging"), member("b", "lagging", project="p2", target=OLDER_TARGET)])
        readiness = envelope([member("a", "lagging", readiness="ready"), member("b", "lagging", project="p2", target=OLDER_TARGET, readiness="ready")])
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}), self.monotonic_readings(0.0, 0.0, 0.0, 0.0, watch.TICK_BUDGET_SECONDS - 10.0):
            sandbox = FakeSandbox(versions, readiness)
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn(f"new target version {TARGET}, 1 cluster(s) pending (a): 0 blocked, 1 ready;", out)
        self.assertIn(f"new target version {OLDER_TARGET}, 1 cluster(s) pending (b): not run to completion; the tick budget was spent before p2 finished; retried tomorrow, starting there", out)
        self.assertNotIn("none graded", out)
        text = (self.home / "reports" / TARGET / "latest.md").read_text()
        self.assertNotIn("p2", text, "the version's report lists only its own projects' read errors")
        self.assertIsNone(self.ledger()["targets"][OLDER_TARGET]["last_report_at"])
        self.assertEqual(self.ledger()["announced"].get("ungraded", {}), {})
        self.assertFalse((self.home / "reports" / OLDER_TARGET).exists())
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            sandbox = FakeSandbox(versions, readiness)
            code, out = self.run_tick(sandbox)
        self.assertEqual([c[1][1] for c in sandbox.calls if c[0] == "readiness"], ["p2"])
        self.assertIn(f"new target version {OLDER_TARGET}, 1 cluster(s) pending (b): 0 blocked, 1 ready;", out)
        self.assertEqual(self.ledger()["targets"][OLDER_TARGET]["last_report_at"], NOW.isoformat())

    def test_the_version_table_stops_at_the_budget_and_resumes_there(self) -> None:
        self.seed(TARGET, NOW - timedelta(days=8), ["p1/us-central1-a/a", "p2/us-central1-a/b"])
        versions, readiness = self.two_projects()
        # started, table p1, table p2 (past the table's share), then readiness runs with room
        late = watch.TABLE_BUDGET_SECONDS - 10.0
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}), self.monotonic_readings(0.0, 0.0, late, late):
            sandbox = FakeSandbox(versions, readiness)
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual([c[1][1] for c in sandbox.calls if c[0] == "versions"], ["p1"])
        self.assertIn("the version table was partial (1 read error(s), exit 0): p2: tick budget exhausted before the version table read project p2; a version is retired only once every project that pends it has been read, or a readiness run", out)
        self.assertEqual(sorted(self.ledger()["table_runs"]), ["p1"])
        self.assertEqual([(e["project"], e["kind"]) for e in self.ledger()["table_errors"]], [("p2", "table not run to completion by the budget")], "the ledger keeps the last tick's table errors")
        self.assertEqual(self.ledger()["targets"][TARGET]["pending"], ["p1/us-central1-a/a", "p2/us-central1-a/b"])
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            sandbox = FakeSandbox(versions, readiness)
            code, out = self.run_tick(sandbox)
        self.assertEqual([c[1][1] for c in sandbox.calls if c[0] == "versions"], ["p2", "p1"])
        self.assertIn("the version table reads every project again", out)
        self.assertEqual(sorted(self.ledger()["table_runs"]), ["p1", "p2"])
        self.assertEqual(self.ledger()["table_errors"], [], "a complete table leaves none")

    def test_a_failed_table_for_one_project_is_that_project_s_gap(self) -> None:
        self.seed(TARGET, NOW - timedelta(days=8), ["p1/us-central1-a/a", "p2/us-central1-a/b"])
        versions, readiness = self.two_projects()
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            sandbox = FakeSandbox(versions, readiness, table_failing_projects={"p2"}, failing_projects={"p2"})
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn("the version table was partial (1 read error(s), exit 0): p2: version table for p2 failed: sandbox exited 255 without a report: ssh: lost connection;", out, "the table's reason reaches the line")
        self.assertIn("2 cluster(s) pending (a, b): 0 blocked, 1 ready, 1 not read;", out)
        self.assertIn("readiness run for p2 failed: sandbox exited 255 without a report: ssh: lost connection", out)
        self.assertEqual(self.ledger()["targets"][TARGET]["pending"], ["p1/us-central1-a/a", "p2/us-central1-a/b"])
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            sandbox = FakeSandbox(versions, readiness, table_failing_projects={"p2"})
            code, out = self.run_tick(sandbox)
        self.assertEqual(out, "", "the partial table is announced once; a readable p2 readiness run grades b under the carried-forward pending set on a day it is due")

    def test_a_run_the_budget_cut_short_is_not_an_attempt_and_the_next_sweep_starts_there(self) -> None:
        versions, readiness = self.two_projects()
        # started, table p1, table p2, readiness p1 (full cap), readiness p2 (a shrunk timeout that then fires)
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}), self.monotonic_readings(0.0, 0.0, 0.0, 0.0, watch.TICK_BUDGET_SECONDS - 400.0):
            sandbox = FakeSandbox(versions, readiness, timeout_projects={"p2"})
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual([c[2] for c in sandbox.calls if c[0] == "readiness"], [watch.READINESS_TIMEOUT_SECONDS, 400.0])
        self.assertIn("1 ready, 1 not read;", out)
        self.assertIn("readiness run for p2 cut short by the tick budget; it goes first next time", out)
        self.assertNotIn("not started", out)
        self.assertNotIn("timed out", out)
        self.assertEqual(sorted(self.ledger()["readiness_runs"]), ["p1"], "a cut-short run is not stamped")
        self.assertEqual(self.ledger()["announced"].get("ungraded", {}), {})

    def test_a_run_that_times_out_at_its_own_cap_is_a_failed_attempt_with_a_whole_number(self) -> None:
        sandbox = FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="ready")]), timeout_projects={"p1"})
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn(f"none graded (readiness run for p1 failed: the sandbox run timed out after {watch.READINESS_TIMEOUT_SECONDS}s)", out)
        self.assertIn("p1", self.ledger()["readiness_runs"], "a run that hit its own cap is an attempt and is stamped")
        self.assertEqual(self.ledger()["announced"]["ungraded"][TARGET]["attempts"], 1)

    def test_the_partial_line_is_keyed_on_the_kind_of_error_so_a_turning_window_posts_once(self) -> None:
        versions = envelope([member("a", "lagging"), member("b", "lagging", project="p2"), member("c", "lagging", project="p3")])
        readiness = envelope([member("a", "lagging", readiness="ready"), member("b", "lagging", project="p2", readiness="ready"), member("c", "lagging", project="p3", readiness="ready")])
        outs = []
        for day in range(3):
            # started, table first, table second, table third (past the share), then readiness runs with room
            late = watch.TABLE_BUDGET_SECONDS - 10.0
            with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2,p3"}), mock.patch.object(watch, "now_utc", return_value=NOW + timedelta(days=day)), self.monotonic_readings(0.0, 0.0, 0.0, late, late, late, late):
                sandbox = FakeSandbox(versions, readiness)
                code, out = self.run_tick(sandbox)
            outs.append((out, [c[1][1] for c in sandbox.calls if c[0] == "versions"]))
        self.assertIn("the version table was partial (1 read error(s), exit 0): p3: tick budget exhausted before the version table read project p3", outs[0][0])
        self.assertEqual([o[1] for o in outs], [["p1", "p2"], ["p3", "p1"], ["p2", "p1"]], "the least recently read projects go first, then by name")
        self.assertNotIn("partial", outs[1][0], "a turning unread window is the same kind of partial table, announced once")
        self.assertNotIn("partial", outs[2][0])

    def test_a_sweep_over_another_version_does_not_reset_which_project_goes_first(self) -> None:
        versions = envelope([member("a", "lagging"), member("b", "lagging", project="p2")])
        readiness = envelope([member("a", "lagging", readiness="ready"), member("b", "lagging", project="p2", readiness="ready")])
        env = {watch.PROJECTS_ENV: "p1,p2"}
        # day 0: p2's readiness run is cut short
        with mock.patch.dict(os.environ, env), self.monotonic_readings(0.0, 0.0, 0.0, 0.0, watch.TICK_BUDGET_SECONDS - 400.0):
            self.run_tick(FakeSandbox(versions, readiness, timeout_projects={"p2"}))
        # day 1: a new version in p1 alone sweeps p1 and completes
        versions2 = envelope([member("a", "lagging"), member("b", "lagging", project="p2"), member("d", "lagging", target=OLDER_TARGET)])
        readiness2 = envelope([member("a", "lagging", readiness="ready"), member("b", "lagging", project="p2", readiness="ready"), member("d", "lagging", target=OLDER_TARGET, readiness="ready")])
        with mock.patch.dict(os.environ, env), mock.patch.object(watch, "now_utc", return_value=NOW + timedelta(days=1)):
            sandbox = FakeSandbox(versions2, readiness2)
            self.run_tick(sandbox)
        self.assertEqual([c[1][1] for c in sandbox.calls if c[0] == "readiness"], ["p1"])
        # day 8: the first version's refresh starts at p2, the project still owed a run
        ledger = self.ledger()
        ledger["targets"][TARGET]["last_report_at"] = NOW.isoformat()
        watch.save_ledger(self.home / watch.LEDGER_FILE_NAME, ledger)
        with mock.patch.dict(os.environ, env), mock.patch.object(watch, "now_utc", return_value=NOW + timedelta(days=8)):
            sandbox = FakeSandbox(versions2, readiness2)
            code, out = self.run_tick(sandbox)
        self.assertEqual([c[1][1] for c in sandbox.calls if c[0] == "readiness"], ["p2", "p1"])
        self.assertIn(f"scheduled refresh {TARGET}, 2 cluster(s) pending (a, b): 0 blocked, 2 ready;", out)

    def test_a_readiness_run_settles_the_clusters_the_table_did_not_reach(self) -> None:
        # The ledger says b (p2) pends TARGET; the table's share reaches p1 only; p2's readiness run lists b as current.
        self.seed(TARGET, NOW - timedelta(days=8), ["p1/us-central1-a/a", "p2/us-central1-a/b"])
        versions = envelope([member("a", "lagging"), member("b", "lagging", project="p2")])
        readiness = envelope([member("a", "lagging", readiness="ready"), member("b", "current", project="p2", readiness="ready")])
        late = watch.TABLE_BUDGET_SECONDS - 10.0
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}), self.monotonic_readings(0.0, 0.0, late, late, late):
            code, out = self.run_tick(FakeSandbox(versions, readiness))
        self.assertEqual(code, 0)
        self.assertIn(f"scheduled refresh {TARGET}, 1 cluster(s) pending (a): 0 blocked, 1 ready;", out)
        self.assertEqual(self.ledger()["targets"][TARGET]["pending"], ["p1/us-central1-a/a"])
        text = (self.home / "reports" / TARGET / "latest.md").read_text()
        self.assertIn("1 cluster(s) are below this version: p1/us-central1-a/a.", text)

    def test_a_readiness_run_that_shows_every_pending_cluster_current_retires_the_version(self) -> None:
        # A report for the version exists from an earlier tick, so retirement has a directory to remove.
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p2"}):
            self.run_tick(FakeSandbox(envelope([member("b", "lagging", project="p2", target=OLDER_TARGET)]), envelope([member("b", "lagging", project="p2", target=OLDER_TARGET, readiness="ready")])))
        self.assertTrue((self.home / "reports" / OLDER_TARGET / "latest.md").exists())
        ledger = self.ledger()
        ledger["targets"][OLDER_TARGET]["last_report_at"] = (NOW - timedelta(days=8)).isoformat()
        watch.save_ledger(self.home / watch.LEDGER_FILE_NAME, ledger)
        versions = envelope([member("a", "lagging")])
        readiness = envelope([member("a", "lagging", readiness="ready"), member("b", "current", project="p2", readiness="ready")])
        late = watch.TABLE_BUDGET_SECONDS - 10.0
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}), self.monotonic_readings(0.0, 0.0, late, late, late):
            code, out = self.run_tick(FakeSandbox(versions, readiness))
        self.assertEqual(code, 0)
        self.assertIn(f"{OLDER_TARGET} is no longer pending", out)
        self.assertIn("the version table was partial (1 read error(s), exit 0): p2: tick budget exhausted before the version table read project p2", out)
        self.assertIn("or a readiness run has listed every cluster pending it as no longer below it", out, "the partial line states the rule this tick retires by")
        self.assertNotIn(OLDER_TARGET, self.ledger()["targets"])
        self.assertFalse((self.home / "reports" / OLDER_TARGET).exists(), "the retired version's directory goes with it")

    def test_a_readiness_run_that_cannot_resolve_a_cluster_s_target_settles_nothing(self) -> None:
        self.seed(TARGET, NOW - timedelta(days=8), ["p2/us-central1-a/b"])
        versions = envelope([member("a", "current")])
        unknown = dict(member("b", "unknown", project="p2", target=None, readiness="unknown"))
        readiness = envelope([unknown], errors=[{"project": "p2", "location": "us-central1-a", "message": "get-server-config timed out"}])
        late = watch.TABLE_BUDGET_SECONDS - 10.0
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}), self.monotonic_readings(0.0, 0.0, late, late, late):
            code, out = self.run_tick(FakeSandbox(versions, readiness))
        self.assertEqual(code, 0)
        self.assertNotIn("no longer pending", out)
        self.assertIn(f"scheduled refresh {TARGET}, 1 cluster(s) pending (b): none graded", out)
        self.assertEqual(self.ledger()["targets"][TARGET]["pending"], ["p2/us-central1-a/b"])
        self.assertEqual(self.ledger()["targets"][TARGET]["last_report_at"], (NOW - timedelta(days=8)).isoformat(), "still due tomorrow")

    def test_a_cut_short_table_run_is_the_same_kind_of_partial_table_as_an_unreached_one(self) -> None:
        self.seed(TARGET, NOW, ["p1/us-central1-a/a", "p2/us-central1-a/b", "p3/us-central1-a/c"])
        versions = envelope([member("a", "lagging"), member("b", "lagging", project="p2"), member("c", "lagging", project="p3")])
        outs = []
        for day, cut in enumerate(({"p2"}, set(), {"p3"})):
            late = watch.TABLE_BUDGET_SECONDS - 10.0
            # started, first table run, second table run under a shrunk timeout, third past the share
            with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2,p3"}), mock.patch.object(watch, "now_utc", return_value=NOW + timedelta(days=day)), self.monotonic_readings(0.0, 0.0, watch.TABLE_BUDGET_SECONDS - 550.0, late, late):
                code, out = self.run_tick(FakeSandbox(versions, table_timeout_projects=cut))
            outs.append(out)
        self.assertIn("the version table was partial", outs[0])
        self.assertEqual(outs[1], "", "a day the budget merely stopped short posts nothing new")
        self.assertEqual(outs[2], "", "a day a run was cut short posts nothing new either")

    def test_a_project_given_by_number_is_keyed_by_the_id_the_report_resolves(self) -> None:
        versions = envelope([member("a", "lagging", project="my-proj"), member("b", "lagging", project="other")])
        readiness = envelope([member("a", "lagging", project="my-proj", readiness="ready"), member("b", "lagging", project="other", readiness="ready")])
        env = {watch.PROJECTS_ENV: "123456,other"}
        with mock.patch.dict(os.environ, env):
            sandbox = FakeSandbox(versions, readiness, aliases={"123456": "my-proj"})
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(self.ledger()["project_ids"], {"123456": "my-proj", "other": "other"})
        self.assertEqual(self.ledger()["targets"][TARGET]["pending"], ["my-proj/us-central1-a/a", "other/us-central1-a/b"])
        # The number's table fails next week: its clusters are carried forward under the id, not dropped.
        ledger = self.ledger()
        ledger["targets"][TARGET]["last_report_at"] = (NOW - timedelta(days=8)).isoformat()
        watch.save_ledger(self.home / watch.LEDGER_FILE_NAME, ledger)
        with mock.patch.dict(os.environ, env):
            sandbox = FakeSandbox(versions, readiness, aliases={"123456": "my-proj"}, table_failing_projects={"123456"})
            code, out = self.run_tick(sandbox)
        self.assertIn("2 cluster(s) pending (a, b): 0 blocked, 2 ready;", out)
        self.assertEqual(self.ledger()["targets"][TARGET]["pending"], ["my-proj/us-central1-a/a", "other/us-central1-a/b"])
        self.assertEqual([c[1][1] for c in sandbox.calls if c[0] == "readiness"], ["123456", "other"], "the readiness run is asked for by the roster's spelling")

    def test_two_spellings_of_one_project_are_tabled_once_and_counted_once(self) -> None:
        versions = envelope([member("a", "lagging", project="my-proj")])
        readiness = envelope([member("a", "lagging", project="my-proj", readiness="ready")])
        env = {watch.PROJECTS_ENV: "123456,my-proj"}
        with mock.patch.dict(os.environ, env):
            sandbox = FakeSandbox(versions, readiness, aliases={"123456": "my-proj"})
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(len([c for c in sandbox.calls if c[0] == "versions"]), 1, "the id the number's run learned is tabled, so the id spelling is skipped the same day")
        self.assertEqual(self.ledger()["project_ids"], {"123456": "my-proj"})
        ledger = self.ledger()
        ledger["targets"][TARGET]["last_report_at"] = (NOW - timedelta(days=8)).isoformat()
        watch.save_ledger(self.home / watch.LEDGER_FILE_NAME, ledger)
        with mock.patch.dict(os.environ, env):
            sandbox = FakeSandbox(versions, readiness, aliases={"123456": "my-proj"})
            code, out = self.run_tick(sandbox)
        self.assertEqual(len([c for c in sandbox.calls if c[0] == "versions"]), 1, "one project, one table run")
        self.assertEqual(self.ledger()["project_ids"], {"123456": "my-proj", "my-proj": "my-proj"}, "the id spelling, never run before, goes first and learns itself")
        self.assertIn("1 cluster(s) pending (a): 0 blocked, 1 ready;", out)
        self.assertEqual(self.ledger()["targets"][TARGET]["pending"], ["my-proj/us-central1-a/a"])

    def test_a_describe_outage_does_not_replace_a_learned_id(self) -> None:
        versions = envelope([member("a", "lagging", project="my-proj")])
        readiness = envelope([member("a", "lagging", project="my-proj", readiness="ready")])
        env = {watch.PROJECTS_ENV: "123456"}
        with mock.patch.dict(os.environ, env):
            self.run_tick(FakeSandbox(versions, readiness, aliases={"123456": "my-proj"}))
        self.assertEqual(self.ledger()["project_ids"], {"123456": "my-proj"})
        ledger = self.ledger()
        ledger["targets"][TARGET]["last_report_at"] = (NOW - timedelta(days=8)).isoformat()
        watch.save_ledger(self.home / watch.LEDGER_FILE_NAME, ledger)
        with mock.patch.dict(os.environ, env):
            sandbox = FakeSandbox(versions, readiness, aliases={"123456": "my-proj"}, describe_down={"123456"})
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(self.ledger()["project_ids"], {"123456": "my-proj"}, "the learned id stands through the outage")
        self.assertEqual(self.ledger()["targets"][TARGET]["pending"], ["my-proj/us-central1-a/a"], "members keyed by the number are read under the id")
        self.assertIn("1 cluster(s) pending (a): 0 blocked, 1 ready;", out)

    def test_a_describe_outage_beside_failed_reads_keeps_the_clusters_pending_and_the_errors_under_the_id(self) -> None:
        clean = envelope([member("a", "lagging", project="my-proj")])
        graded = envelope([member("a", "lagging", project="my-proj", readiness="ready")])
        env = {watch.PROJECTS_ENV: "123456"}
        with mock.patch.dict(os.environ, env):
            self.run_tick(FakeSandbox(clean, graded, aliases={"123456": "my-proj"}))
        self.assertEqual(self.ledger()["project_ids"], {"123456": "my-proj"})
        ledger = self.ledger()
        ledger["targets"][TARGET]["last_report_at"] = (NOW - timedelta(days=8)).isoformat()
        watch.save_ledger(self.home / watch.LEDGER_FILE_NAME, ledger)
        # The outage day: the script keys the failed listing, the member and its
        # failed kubectl read by the number; the fake rewrites the id-keyed fixtures to it.
        unlisted = envelope([], errors=[{"project": "my-proj", "location": None, "message": "clusters list failed"}], exit_code=1)
        unread = envelope(
            [member("a", "lagging", project="my-proj", readiness="unknown")],
            errors=[{"project": "my-proj", "location": "us-central1-a", "cluster": "a", "message": "kubectl get failed"}],
            exit_code=1,
        )
        with mock.patch.dict(os.environ, env):
            sandbox = FakeSandbox(unlisted, unread, aliases={"123456": "my-proj"}, describe_down={"123456"})
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(sandbox.kinds(), ["versions", "readiness"])
        self.assertNotIn("is no longer pending", out, "a project whose listing failed is not a read project")
        self.assertIn("the version table was partial (1 read error(s), exit 1): my-proj: clusters list failed", out, "the unread project is named by its id, with the script's reason")
        self.assertEqual(self.ledger()["targets"][TARGET]["pending"], ["my-proj/us-central1-a/a"], "carried forward from the ledger under the id")
        self.assertIn("1 cluster(s) pending (a): none graded (their kubectl read failed or the run returned nothing for them)", out)
        self.assertEqual(self.ledger()["targets"][TARGET]["last_report_at"], (NOW - timedelta(days=8)).isoformat(), "an ungraded version is not recorded")
        text = (self.home / "reports" / TARGET / "latest.md").read_text()
        self.assertIn("1 not read (my-proj/us-central1-a/a;", text)
        self.assertIn("- my-proj/us-central1-a/a: kubectl get failed", text, "the read error is kept, under the id")
        data = json.loads((self.home / "reports" / TARGET / "latest.md").resolve().with_suffix(".json").read_text())
        self.assertEqual([e["project"] for e in data["errors"]], ["my-proj"])
        # Describe is back: the version is a refresh, not new, and the week is recorded.
        with mock.patch.dict(os.environ, env):
            code, out = self.run_tick(FakeSandbox(clean, graded, aliases={"123456": "my-proj"}))
        self.assertEqual(code, 0)
        self.assertIn(f"scheduled refresh {TARGET}, 1 cluster(s) pending (a): 0 blocked, 1 ready;", out)
        self.assertNotIn("new target version", out)

    def test_two_spellings_whose_one_table_run_fails_are_one_failure_line_not_a_partial_table(self) -> None:
        versions = envelope([member("a", "lagging", project="my-proj")])
        readiness = envelope([member("a", "lagging", project="my-proj", readiness="ready")])
        env = {watch.PROJECTS_ENV: "123456,my-proj"}
        with mock.patch.dict(os.environ, env):
            self.run_tick(FakeSandbox(versions, readiness, aliases={"123456": "my-proj"}))
        before = self.ledger()
        with mock.patch.dict(os.environ, env):
            sandbox = FakeSandbox(versions, readiness, aliases={"123456": "my-proj"}, table_failing_projects={"123456", "my-proj"})
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(out, self.failed("RuntimeError: sandbox exited 255 without a report: ssh: lost connection"))
        self.assertEqual(sandbox.kinds(), ["versions"], "one project, one run, and no readiness run on a failed tick")
        self.assertEqual(self.ledger(), before, "a failed tick writes nothing to the ledger")
        self.assertTrue(self.marker().exists())

    def test_a_number_the_report_cannot_resolve_keys_nothing_until_its_id_is_learned(self) -> None:
        # Day one: the number's first table run falls on a `projects describe` outage, so the script keys its cluster by the number.
        versions = envelope([member("a", "lagging", project="my-proj"), member("b", "lagging", project="p2", target=OLDER_TARGET)])
        readiness = envelope([member("a", "lagging", project="my-proj", readiness="ready"), member("b", "lagging", project="p2", target=OLDER_TARGET, readiness="ready")])
        env = {watch.PROJECTS_ENV: "123456,p2"}
        with mock.patch.dict(os.environ, env):
            code, out = self.run_tick(FakeSandbox(versions, readiness, aliases={"123456": "my-proj"}, describe_down={"123456"}))
        self.assertEqual(code, 0)
        self.assertIn("the version table was partial (1 read error(s), exit 1): 123456: the project number did not resolve to its id (`gcloud projects describe` failed)", out, "the unresolved number is the table's gap, named by the spelling, with the reason")
        self.assertIn("grant the credential `resourcemanager.projects.get` or name the project by its id", out, "and the remedy")
        self.assertIn(f"new target version {OLDER_TARGET}, 1 cluster(s) pending (b): 0 blocked, 1 ready;", out)
        self.assertNotIn(TARGET, out, "a run the report keyed by an unresolved number keys no member")
        self.assertNotIn(TARGET, self.ledger()["targets"])
        self.assertNotIn("123456", json.dumps(self.ledger()["targets"]) + json.dumps(self.ledger()["readiness_runs"]), "the ledger never holds a key under a number")
        self.assertEqual(self.ledger()["project_ids"], {"p2": "p2"}, "a number the script handed back unresolved is not learned")
        self.assertEqual(sorted(self.ledger()["table_runs"]), ["123456", "p2"], "the run completed, so the stamp rotates the order")
        # Day two: describe is back, so the number resolves and the version it hid is new, keyed by the id.
        with mock.patch.dict(os.environ, env), mock.patch.object(watch, "now_utc", return_value=NOW + timedelta(days=1)):
            sandbox = FakeSandbox(versions, readiness, aliases={"123456": "my-proj"})
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn("the version table reads every project again", out)
        self.assertIn(f"new target version {TARGET}, 1 cluster(s) pending (a): 0 blocked, 1 ready;", out)
        self.assertEqual(self.ledger()["project_ids"], {"123456": "my-proj", "p2": "p2"})
        self.assertEqual(self.ledger()["targets"][TARGET]["pending"], ["my-proj/us-central1-a/a"])
        self.assertEqual([c[1][1] for c in sandbox.calls if c[0] == "readiness"], ["123456"], "the readiness run is asked for by the roster's spelling")

    def test_two_spellings_on_the_number_s_first_describe_outage_day_table_one_project_once(self) -> None:
        versions = envelope([member("a", "lagging", project="my-proj")])
        readiness = envelope([member("a", "lagging", project="my-proj", readiness="ready")])
        env = {watch.PROJECTS_ENV: "123456,my-proj"}
        with mock.patch.dict(os.environ, env):
            sandbox = FakeSandbox(versions, readiness, aliases={"123456": "my-proj"}, describe_down={"123456"})
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn(f"new target version {TARGET}, 1 cluster(s) pending (a): 0 blocked, 1 ready;", out, "the id's run lists the project; the number's keys nothing")
        self.assertNotIn("2 cluster(s)", out)
        self.assertIn("the version table was partial (1 read error(s), exit 1): 123456: the project number did not resolve to its id", out)
        self.assertEqual([c[1][1] for c in sandbox.calls if c[0] == "readiness"], ["my-proj"], "one readiness run, by the id")
        self.assertEqual(self.ledger()["targets"][TARGET]["pending"], ["my-proj/us-central1-a/a"])
        self.assertEqual(sorted(self.ledger()["readiness_runs"]), ["my-proj"])
        self.assertEqual(self.ledger()["project_ids"], {"my-proj": "my-proj"})
        text = (self.home / "reports" / TARGET / "latest.md").read_text()
        self.assertIn("1 cluster(s) are below this version: my-proj/us-central1-a/a.", text)
        # Describe is back: the number resolves to the id already tabled, so the id spelling is skipped and the table is whole.
        with mock.patch.dict(os.environ, env), mock.patch.object(watch, "now_utc", return_value=NOW + timedelta(days=1)):
            sandbox = FakeSandbox(versions, readiness, aliases={"123456": "my-proj"})
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual([c[1][1] for c in sandbox.calls if c[0] == "versions"], ["123456"], "one project, one table run")
        self.assertEqual(out, "upgrade readiness watch: the version table reads every project again\n")
        self.assertEqual(self.ledger()["project_ids"], {"123456": "my-proj", "my-proj": "my-proj"})

    def test_a_number_the_credential_can_never_resolve_fails_the_tick_once_with_the_remedy_then_recovers(self) -> None:
        versions = envelope([member("a", "lagging", project="my-proj")])
        readiness = envelope([member("a", "lagging", project="my-proj", readiness="ready")])
        env = {watch.PROJECTS_ENV: "123456"}
        outs = []
        for day in range(3):
            with mock.patch.dict(os.environ, env), mock.patch.object(watch, "now_utc", return_value=NOW + timedelta(days=day)):
                sandbox = FakeSandbox(versions, readiness, aliases={"123456": "my-proj"}, describe_down={"123456"})
                code, out = self.run_tick(sandbox)
            self.assertEqual(code, 0)
            self.assertEqual(sandbox.kinds(), ["versions"], "nothing is keyed, so no readiness run")
            outs.append(out)
        self.assertEqual(
            outs[0],
            self.failed(
                "ProjectUnresolved: no project was read under a key; 123456: the project number did not "
                "resolve to its id (`gcloud projects describe` failed), so its clusters are not keyed and earn no report until it does; grant the "
                "credential `resourcemanager.projects.get` or name the project by its id",
                watch.FAILED_REASON_UNRESOLVED,
            ),
        )
        self.assertEqual(outs[1], "", "a standing failure is announced once")
        self.assertEqual(outs[2], "")
        self.assertFalse((self.home / watch.LEDGER_FILE_NAME).exists(), "a failed tick writes nothing")
        self.assertFalse((self.home / "reports").exists())
        self.assertEqual(self.marker().read_text(), watch.FAILED_REASON_UNRESOLVED, "the standing failure is kept beside the ledger, by its kind")
        # The credential can describe at last: the recovery line, the id learned, the version reported under it.
        with mock.patch.dict(os.environ, env), mock.patch.object(watch, "now_utc", return_value=NOW + timedelta(days=3)):
            code, out = self.run_tick(FakeSandbox(versions, readiness, aliases={"123456": "my-proj"}))
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], watch.RECOVERED_LINE)
        self.assertIn(f"new target version {TARGET}, 1 cluster(s) pending (a): 0 blocked, 1 ready;", out)
        self.assertEqual(self.ledger()["project_ids"], {"123456": "my-proj"})
        self.assertEqual(self.ledger()["targets"][TARGET]["pending"], ["my-proj/us-central1-a/a"])
        self.assertFalse(self.marker().exists())

    def test_a_tick_whose_every_number_is_unresolved_names_six_and_counts_the_rest(self) -> None:
        numbers = [f"12345{i}" for i in range(8)]
        versions = envelope([member(f"c{i}", "lagging", project=f"proj-{i}") for i in range(8)])
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: ",".join(numbers)}):
            sandbox = FakeSandbox(versions, aliases={n: f"proj-{i}" for i, n in enumerate(numbers)}, describe_down=set(numbers))
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn("ProjectUnresolved: no project was read under a key", out)
        self.assertEqual(out.count("the project number did not resolve"), watch.MAX_NAMES_IN_LINE)
        self.assertIn(watch.NAMES_OVERFLOW.format(more=2), out)

    def test_a_table_that_fails_for_every_project_is_one_failure_line(self) -> None:
        versions, readiness = self.two_projects()
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            sandbox = FakeSandbox(versions, readiness, table_failing_projects={"p1", "p2"})
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(out, self.failed("RuntimeError: sandbox exited 255 without a report: ssh: lost connection"))
        self.assertFalse((self.home / watch.LEDGER_FILE_NAME).exists())

    def test_a_project_s_timeout_shrinks_to_what_is_left_of_the_budget(self) -> None:
        # started, table p1 (inside the table's share), readiness p1 (700 s left of the tick's budget)
        with self.monotonic_readings(0.0, 0.0, 2000.0):
            sandbox = FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="ready")]))
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual([c[2] for c in sandbox.calls if c[0] == "readiness"], [watch.TICK_BUDGET_SECONDS - 2000.0])
        self.assertEqual([c[2] for c in sandbox.calls if c[0] == "versions"], [watch.VERSION_TABLE_TIMEOUT_SECONDS])

    def test_a_project_with_the_budget_to_spare_keeps_its_full_timeout(self) -> None:
        with self.monotonic_readings(0.0, 0.0, 100.0):
            sandbox = FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="ready")]))
            self.run_tick(sandbox)
        self.assertEqual([c[2] for c in sandbox.calls if c[0] == "readiness"], [watch.READINESS_TIMEOUT_SECONDS])


class WebhookReadFailure(unittest.TestCase):
    """A member whose first read answered and whose other rules were graded is graded, not
    unread, when only the second, webhook read failed: its `unknown` is a verdict."""

    def _report(self, read_tag):
        member = {"project": "p", "location": "us-central1", "cluster": "big", "readiness": {"status": "unknown"}}
        error = {"project": "p", "location": "us-central1", "cluster": "big", "message": "kubectl output cut at the credential proxy's cap"}
        if read_tag:
            error[watch.ERROR_READ_KEY] = read_tag
        return {watch.MEMBERS_KEY: [member], watch.ERRORS_KEY: [error]}

    def test_a_webhook_read_failure_leaves_the_member_graded_unknown(self) -> None:
        key = watch.member_key({"project": "p", "location": "us-central1", "cluster": "big"})
        blocked, ready, unknown, unread = watch.readiness_verdicts(self._report(watch.WEBHOOK_READ_ERROR), [key])
        self.assertEqual((blocked, ready, unknown, unread), ([], [], [key], []))

    def test_a_first_read_failure_still_leaves_the_member_unread(self) -> None:
        key = watch.member_key({"project": "p", "location": "us-central1", "cluster": "big"})
        blocked, ready, unknown, unread = watch.readiness_verdicts(self._report(None), [key])
        self.assertEqual((blocked, ready, unknown, unread), ([], [], [], [key]))

    def test_the_markdown_lists_a_webhook_read_row_apart_from_the_unread_ones(self) -> None:
        # The summary counts the member as graded (unknown); the row is listed under its own
        # heading, not under the one that says its member is not graded, which the first-read
        # row of another member still sits under.
        big = {"project": "p", "location": "us-central1", "cluster": "big", "readiness": {"status": "unknown"}}
        small = {"project": "p", "location": "us-central1", "cluster": "small", "readiness": {"status": "unknown"}}
        rows = [
            {"project": "p", "location": "us-central1", "cluster": "big", "message": "kubectl output cut at the credential proxy's cap", watch.ERROR_READ_KEY: watch.WEBHOOK_READ_ERROR},
            {"project": "p", "location": "us-central1", "cluster": "small", "message": "get-credentials failed: 403"},
        ]
        keys = [watch.member_key(big), watch.member_key(small)]
        text = watch.render_markdown(TARGET, "a new version", keys, envelope([big, small], errors=rows), datetime(2026, 10, 14, tzinfo=timezone.utc), "Next report in 7 days")
        self.assertIn("1 unknown (p/us-central1/big; the table says what it could not decide) and 1 not read (p/us-central1/small;", text)
        unread_at = text.index(watch.FAILED_READS_HEADING)
        webhook_at = text.index(watch.WEBHOOK_READ_ROWS_HEADING)
        self.assertLess(unread_at, webhook_at)
        self.assertIn("- p/us-central1/small: get-credentials failed: 403", text[unread_at:webhook_at])
        self.assertNotIn("big", text[unread_at:webhook_at])
        self.assertIn("- p/us-central1/big: kubectl output cut at the credential proxy's cap", text[webhook_at:])
        self.assertTrue(watch.webhook_read_row(rows[0]) and not watch.unread_row(rows[0]))
        self.assertTrue(watch.unread_row(rows[1]) and not watch.webhook_read_row(rows[1]))
        self.assertFalse(watch.unread_row({"project": "p", "location": "us-central1", "message": "server config failed"}))

    def test_a_member_with_a_first_read_row_and_a_webhook_row_is_listed_as_unread_with_both_rows(self) -> None:
        # The first read timed out at the deadline (no skip marker), the second read then failed
        # too: two rows for one member, which the summary counts as not read, so neither row
        # sits under the heading that says the member was graded.
        big = {"project": "p", "location": "us-central1", "cluster": "big", "readiness": {"status": "unknown"}}
        rows = [
            {"project": "p", "location": "us-central1", "cluster": "big", "message": "kubectl get pdb,deploy,statefulset -A -o json timed out after 60 seconds"},
            {"project": "p", "location": "us-central1", "cluster": "big", "message": "kubectl output cut at the credential proxy's cap", watch.ERROR_READ_KEY: watch.WEBHOOK_READ_ERROR},
        ]
        key = watch.member_key(big)
        self.assertEqual(watch.readiness_verdicts({watch.MEMBERS_KEY: [big], watch.ERRORS_KEY: rows}, [key])[3], [key])
        text = watch.render_markdown(TARGET, "a new version", [key], envelope([big], errors=rows), datetime(2026, 10, 14, tzinfo=timezone.utc), "Next report in 7 days")
        self.assertIn("1 not read (p/us-central1/big;", text)
        self.assertNotIn(watch.WEBHOOK_READ_ROWS_HEADING, text)
        tail = text[text.index(watch.FAILED_READS_HEADING):]
        self.assertIn("- p/us-central1/big: kubectl get pdb,deploy,statefulset -A -o json timed out after 60 seconds", tail)
        self.assertIn("- p/us-central1/big: kubectl output cut at the credential proxy's cap", tail)

    def test_a_member_in_a_location_whose_server_config_failed_keeps_its_webhook_row_off_the_graded_heading(self) -> None:
        # The summary counts the member as not read (its location's get-server-config failed and
        # it is graded unknown, with no target), so its one webhook row is listed with the failed
        # reads, not under the heading that says the member is graded.
        big = {"project": "p", "location": "us-central1", "cluster": "big", "readiness": {"status": "unknown"}}
        rows = [
            {"project": "p", "location": "us-central1", "message": "get-server-config timed out"},
            {"project": "p", "location": "us-central1", "cluster": "big", "message": "kubectl output cut at the credential proxy's cap", watch.ERROR_READ_KEY: watch.WEBHOOK_READ_ERROR},
        ]
        key = watch.member_key(big)
        self.assertEqual(watch.readiness_verdicts({watch.MEMBERS_KEY: [big], watch.ERRORS_KEY: rows}, [key])[3], [key])
        text = watch.render_markdown(TARGET, "a new version", [key], envelope([big], errors=rows), datetime(2026, 10, 14, tzinfo=timezone.utc), "Next report in 7 days")
        self.assertIn("1 not read (p/us-central1/big;", text)
        self.assertNotIn(watch.WEBHOOK_READ_ROWS_HEADING, text)
        tail = text[text.index(watch.FAILED_READS_HEADING):]
        self.assertIn("- p/us-central1: get-server-config timed out", tail)
        self.assertIn("- p/us-central1/big: kubectl output cut at the credential proxy's cap", tail)


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
        with mock.patch.object(watch.sandbox_exec, "run", side_effect=broken), redirect_stdout(out), redirect_stderr(out):
            code = watch.main([])
        self.assertEqual(code, 0)
        self.assertIn("upgrade readiness watch: the tick failed: RuntimeError: sandbox exited 1 without a report: gcloud: permission denied", out.getvalue())
        self.assertFalse((self.home / watch.LEDGER_FILE_NAME).exists())

    def test_a_usage_failure_carries_the_script_s_own_stderr(self) -> None:
        def usage(argv, *, timeout, check, stdin=None):
            body = {"exit": 2, "tables": "", "report": None}
            return subprocess.CompletedProcess(argv, 0, stdout=f"{watch.ENVELOPE_SENTINEL}\n{json.dumps(body)}\n", stderr="no project: pass --project\n")

        out = io.StringIO()
        with mock.patch.object(watch.sandbox_exec, "run", side_effect=usage), redirect_stdout(out), redirect_stderr(out):
            code = watch.main([])
        self.assertEqual(code, 0)
        self.assertIn("report script exited 2 and wrote no report: no project: pass --project", out.getvalue())

    def test_a_timeout_is_one_short_line_without_the_ssh_command(self) -> None:
        def slow(argv, *, timeout, check, stdin=None):
            raise subprocess.TimeoutExpired(["ssh", "-F", "/dev/null", "hermes@sandbox"], timeout)

        out = io.StringIO()
        with mock.patch.object(watch.sandbox_exec, "run", side_effect=slow), redirect_stdout(out), redirect_stderr(out):
            code = watch.main([])
        self.assertEqual(code, 0)
        self.assertEqual(
            out.getvalue(),
            self.failed(f"SandboxTimedOut: the sandbox run timed out after {watch.VERSION_TABLE_TIMEOUT_SECONDS}s", watch.FAILED_REASON_TIMED_OUT),
        )
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
        with mock.patch.object(watch.sandbox_exec, "run", side_effect=run), redirect_stdout(out), redirect_stderr(out):
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
        ), mock.patch.object(watch.sandbox_exec, "run", side_effect=slow), redirect_stdout(out), redirect_stderr(out):
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
        ), mock.patch.object(watch.sandbox_exec, "run", side_effect=unset), redirect_stdout(out), redirect_stderr(out):
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
            with mock.patch.object(watch.sandbox_exec, "run", side_effect=side_effect), redirect_stdout(out), redirect_stderr(out):
                self.assertEqual(watch.main([]), 0)
            outs.append(out.getvalue())
        self.assertIn("the tick failed: RuntimeError: sandbox exited 1 without a report: gcloud: permission denied", outs[0])
        self.assertTrue(outs[0].startswith(watch.FAILED_TICK_LINE.format(reason=watch.FAILED_REASON_ERROR, next=watch.FAILED_NEXT_UNKNOWN)))
        log = Path(self.tmp.name) / "logs" / watch.WATCH_LOG_FILE
        self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 4, "every failed run is logged, posted or not")
        self.assertEqual(outs[1], "")
        # A changed error of the same kind is the same failure to the channel: the
        # plain line would read the same, so it is not posted again.
        self.assertEqual(outs[2], "")
        self.assertEqual(outs[3], "")
        self.assertFalse((self.home / watch.LEDGER_FILE_NAME).exists())
        self.assertTrue(self.marker().exists())
        dry = FakeSandbox(envelope([member("a", "lagging")]))
        code, out = self.run_tick(dry, ["--dry-run"])
        self.assertNotIn("runs again", out)
        self.assertTrue(self.marker().exists())
        code, out = self.run_tick(FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="ready")])))
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], watch.RECOVERED_LINE)
        self.assertIn("new target version", out)
        self.assertFalse(self.marker().exists())
        code, out = self.run_tick(FakeSandbox(envelope([member("a", "lagging")])))
        self.assertEqual(out, "")

    def test_a_ledger_key_that_is_not_a_version_is_refused_not_overwritten(self) -> None:
        for key in ("../../scratch/x", "latest", "1.35", "1.35.8-gke.1225000/..", "1.35.8-gke.1225000\n"):
            with self.subTest(key=key):
                self.home.mkdir(parents=True, exist_ok=True)
                self.marker().unlink(missing_ok=True)
                (self.home / watch.LEDGER_FILE_NAME).write_text(json.dumps({"targets": {key: {"pending": ["p1/l/a"], "last_report_at": None}}}))
                sandbox = FakeSandbox(envelope([member("a", "lagging")]))
                code, out = self.run_tick(sandbox)
                self.assertEqual(code, 0)
                self.assertIn(f"has a malformed entry for {key.strip()}", out)
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

    def test_a_marker_that_cannot_be_removed_does_not_lose_the_report_lines(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.marker().mkdir()
        sandbox = FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="ready")]))
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn("new target version", out)
        self.assertNotIn("runs again", out)
        self.assertNotIn("the tick failed", out)
        self.assertEqual(self.ledger()["targets"][TARGET]["last_report_at"], NOW.isoformat())
        self.assertTrue(self.marker().is_dir())

    def test_a_failing_dry_run_prints_every_time_and_leaves_no_marker(self) -> None:
        def broken(argv, *, timeout, check, stdin=None):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="gcloud: permission denied")

        outs = []
        for argv in (["--dry-run"], ["--dry-run"], []):
            out = io.StringIO()
            with mock.patch.object(watch.sandbox_exec, "run", side_effect=broken), redirect_stdout(out), redirect_stderr(out):
                self.assertEqual(watch.main(argv), 0)
            outs.append(out.getvalue())
        self.assertIn("the tick failed", outs[0])
        self.assertIn("the tick failed", outs[1])
        self.assertIn("the tick failed", outs[2], "the scheduled tick's own line is not silenced by a dry run")
        self.assertTrue(self.marker().exists())
        out = io.StringIO()
        with mock.patch.object(watch.sandbox_exec, "run", side_effect=broken), redirect_stdout(out), redirect_stderr(out):
            self.assertEqual(watch.main(["--dry-run"]), 0)
        self.assertIn("the tick failed", out.getvalue(), "a dry run on the failure being investigated says so")

    def test_reports_are_pruned_to_the_newest_ten_and_a_retired_version_s_directory_goes(self) -> None:
        sandbox = FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="ready")]))
        for day in range(12):
            if day:
                ledger = self.ledger()
                ledger["targets"][TARGET]["last_report_at"] = (NOW + timedelta(days=day - 8)).isoformat()
                watch.save_ledger(self.home / watch.LEDGER_FILE_NAME, ledger)
            with mock.patch.object(watch, "now_utc", return_value=NOW + timedelta(days=day)):
                code, out = self.run_tick(sandbox)
            self.assertEqual(code, 0)
        directory = self.home / "reports" / TARGET
        stamps = sorted(p.name for p in directory.iterdir() if p.name != "latest.md")
        self.assertEqual(len(stamps), 2 * watch.REPORTS_KEPT_PER_VERSION)
        self.assertEqual(stamps[0], (NOW + timedelta(days=2)).strftime(watch.REPORT_TIMESTAMP_FORMAT) + ".json")
        self.assertEqual((directory / "latest.md").resolve().name, (NOW + timedelta(days=11)).strftime(watch.REPORT_TIMESTAMP_FORMAT) + ".md")
        code, out = self.run_tick(FakeSandbox(envelope([member("a", "current")])))
        self.assertIn(f"{TARGET} is no longer pending", out)
        self.assertFalse(directory.exists())

    def test_the_json_holds_only_the_version_s_clusters(self) -> None:
        versions = envelope([member("a", "lagging"), member("b", "lagging", project="p2", target=OLDER_TARGET)])
        readiness = envelope([member("a", "lagging", readiness="ready"), member("b", "lagging", project="p2", target=OLDER_TARGET, readiness="ready")])
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            code, out = self.run_tick(FakeSandbox(versions, readiness, failing_projects=set()))
        self.assertEqual(code, 0)
        for version, cluster in ((TARGET, "a"), (OLDER_TARGET, "b")):
            latest = (self.home / "reports" / version / "latest.md").resolve()
            data = json.loads(latest.with_suffix(".json").read_text())
            self.assertEqual([m["cluster"] for m in data["members"]], [cluster])
            self.assertEqual(data["errors"], [])

    def test_a_target_the_report_left_unstripped_is_keyed_stripped(self) -> None:
        versions = envelope([member("a", "lagging", target=TARGET + " \n")])
        readiness = envelope([member("a", "lagging", target=TARGET + " \n", readiness="ready")])
        code, out = self.run_tick(FakeSandbox(versions, readiness))
        self.assertEqual(code, 0)
        self.assertIn(f"new target version {TARGET}, 1 cluster(s)", out)
        self.assertEqual(sorted(self.ledger()["targets"]), [TARGET])
        self.assertTrue((self.home / "reports" / TARGET / "latest.md").exists())
        code, out = self.run_tick(FakeSandbox(versions, readiness))
        self.assertEqual(out, "", "the ledger the job wrote loads again")

    def test_a_version_whose_clusters_upgraded_is_retired_even_while_another_project_s_listing_fails(self) -> None:
        self.seed(OLDER_TARGET, NOW, ["p2/us-central1-a/b"])
        partial = envelope(
            [member("a", "lagging"), member("b", "current", project="p2")],
            errors=[{"project": "p1", "message": "clusters list failed"}],
            exit_code=1,
        )
        readiness = envelope([member("a", "lagging", readiness="ready")])
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            code, out = self.run_tick(FakeSandbox(partial, readiness))
        self.assertEqual(code, 0)
        self.assertIn(f"{OLDER_TARGET} is no longer pending", out)
        self.assertNotIn(OLDER_TARGET, self.ledger()["targets"])
        self.assertIn("the version table was partial", out)

    def test_the_saved_report_s_header_says_what_happens_next(self) -> None:
        readiness = envelope([], errors=[{"project": "p1", "location": "us-central1-a", "cluster": "a", "message": "kubectl failed"}])
        code, out = self.run_tick(FakeSandbox(envelope([member("a", "lagging")]), readiness))
        self.assertIn("retrying tomorrow", out)
        text = (self.home / "reports" / TARGET / "latest.md").read_text()
        self.assertIn("so the job retries tomorrow; ask the Platform Agent", text)
        self.assertNotIn("next scheduled refresh", text)
        code, out = self.run_tick(FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="ready")])))
        text = (self.home / "reports" / TARGET / "latest.md").read_text()
        self.assertIn("The next scheduled refresh is after 2026-10-15 while any cluster is still pending; ask the Platform Agent", text)

    def test_an_unsaveable_ledger_exits_zero_with_its_own_line(self) -> None:
        sandbox = FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="ready")]))
        with mock.patch.object(watch, "save_ledger", side_effect=OSError("read-only file system")):
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0, "the plain line, not the scheduler's own failure line")
        self.assertIn("writing the ledger or the report failed: read-only file system", out)
        self.assertIn(watch.FAILED_NEEDS_ADMIN, out)

    def test_an_unwritable_report_exits_zero_and_claims_nothing(self) -> None:
        sandbox = FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="ready")]))
        with mock.patch.object(watch, "write_report", side_effect=OSError("disk full")):
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertIn("writing the ledger or the report failed: disk full", out)
        self.assertFalse((self.home / watch.LEDGER_FILE_NAME).exists())


class DeclaredScope(Base):
    """On an install with a ``spec.scope`` block the sandbox refuses the report
    script a bare ``--project`` (``fleet_scope_args``), so the tick hands it
    the resolved scope beside the project, as the fleet_scope tool spells it
    less the cluster exclusion; an install that declares none keeps the bare
    flag."""

    def test_a_declared_scope_hands_the_report_the_resolved_scope_beside_the_project(self) -> None:
        self.declare_scope(["p1", "p2"], unread=["p3"])
        sandbox = FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="blocked")]))
        code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(sandbox.kinds(), ["versions", "readiness"])
        scope = ["--scope-projects", "p1,p2", "--scope-unread", "p3=denied"]
        self.assertEqual(sandbox.calls[0][1], ["--project", "p1", *scope])
        self.assertEqual(sandbox.calls[1][1], ["--project", "p1", *scope, "--readiness", "--kubeconfig-dir", watch.SANDBOX_KUBECONFIG_DIR])
        self.assertIn(f"new target version {TARGET}, 1 cluster(s) pending (a): 1 blocked (a), 0 ready", out)

    def test_under_a_declared_scope_a_number_resolves_before_the_scope_check(self) -> None:
        """The roster spells the project by number and the scope lists its id: the report
        resolves the number and reads it, as `get_target_projects` checks the resolved id."""
        self.declare_scope(["my-proj"])
        versions = envelope([member("a", "lagging", project="my-proj")])
        readiness = envelope([member("a", "lagging", project="my-proj", readiness="ready")])
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "123456"}):
            code, out = self.run_tick(FakeSandbox(versions, readiness, aliases={"123456": "my-proj"}))
        self.assertEqual(code, 0)
        self.assertNotIn("refused", out)
        self.assertEqual(self.ledger()["targets"][TARGET]["pending"], ["my-proj/us-central1-a/a"])

    def test_under_a_declared_scope_a_learned_number_is_asked_by_its_id_through_a_describe_outage(self) -> None:
        """Once a tick has learned that the number is my-proj, both hops ask the report for
        my-proj, which the scope lists, so a morning whose describe fails reads the project
        instead of refusing it as a number the operator must respell."""
        self.declare_scope(["my-proj"])
        versions = envelope([member("a", "lagging", project="my-proj")])
        readiness = envelope([member("a", "lagging", project="my-proj", readiness="ready")])
        env = {watch.PROJECTS_ENV: "123456"}
        with mock.patch.dict(os.environ, env):
            self.run_tick(FakeSandbox(versions, readiness, aliases={"123456": "my-proj"}))
        self.assertEqual(self.ledger()["project_ids"], {"123456": "my-proj"})
        ledger = self.ledger()
        ledger["targets"][TARGET]["last_report_at"] = (NOW - timedelta(days=8)).isoformat()
        watch.save_ledger(self.home / watch.LEDGER_FILE_NAME, ledger)
        with mock.patch.dict(os.environ, env):
            sandbox = FakeSandbox(versions, readiness, aliases={"123456": "my-proj"}, describe_down={"123456"})
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertNotIn("refused", out)
        self.assertEqual([argv[1] for _, argv, _ in sandbox.calls if argv and argv[0] == "--project"], ["my-proj", "my-proj"])
        self.assertIn("1 cluster(s) pending (a): 0 blocked, 1 ready;", out)

    def test_an_install_that_declares_no_scope_keeps_the_bare_project_flag(self) -> None:
        sandbox = FakeSandbox(envelope([member("a", "lagging")]))
        code, _ = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(sandbox.kinds(), ["versions", "readiness"])
        for _, argv, _ in sandbox.calls:
            self.assertNotIn(fleet_scope_targets.SCOPE_PROJECTS_FLAG, argv)
            self.assertNotIn(fleet_scope_targets.SCOPE_UNREAD_FLAG, argv)
        self.assertEqual(sandbox.calls[0][1], ["--project", "p1"])

    def test_the_render_alone_declares_a_scope_before_the_first_reconcile_tick(self) -> None:
        render = Path(self.tmp.name) / "scope.json"
        render.write_text(json.dumps({"present": True, "projects": [], "exclude": {}}), encoding="utf-8")
        sandbox = FakeSandbox(envelope([member("a", "lagging")]))
        with mock.patch.dict(os.environ, {fleet_scope_targets.SCOPE_FILE_ENV: str(render), watch.MANAGEMENT_PROJECT_ENV: "p1"}):
            self.run_tick(sandbox)
        self.assertEqual(sandbox.calls[0][1], ["--project", "p1", "--scope-projects", "p1", "--scope-unread", "declared-scope=unresolved"])

    def test_the_whole_resolved_scope_goes_beside_every_project_so_the_report_can_refuse_one_outside_it(self) -> None:
        """The roster is not bounded by the declaration (a profile onboarded by
        hand before the scope, a hand run's list), so each run is handed the
        set the reconcile resolved, not the project itself: p2 is read only if
        the report finds it inside."""
        self.declare_scope(["p1"])
        sandbox = FakeSandbox(envelope([member("a", "lagging"), member("b", "lagging", project="p2")]))
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertCountEqual(
            [argv for kind, argv, _ in sandbox.calls if kind == "versions"],
            [["--project", "p1", "--scope-projects", "p1"], ["--project", "p2", "--scope-projects", "p1"]],
        )
        # The report refuses p2 from that argv, so only p1's cluster is pending and the line names the refusal.
        self.assertIn("does not list p2; it lists p1", out)
        self.assertNotIn("p2/us-central1-a/b", json.dumps(self.ledger()))

    def test_a_sandbox_still_marked_after_the_scope_is_gone_refuses_the_bare_flag_through_the_tick(self) -> None:
        """The fourth refusal, driven through a tick: this pod reads no declaration and hands the
        bare flag, and the sandbox, marked scoped at its start, refuses it. With a render named and
        declaring nothing, the remedy is the restart; with no render to read, the run's environment."""
        render = Path(self.tmp.name) / "scope.json"
        render.write_text(json.dumps({fleet_scope_targets.RENDER_PRESENT_KEY: False}), encoding="utf-8")
        for scope_file, remedy in ((str(render), watch.REMEDY_SANDBOX_RESTART), ("", watch.REMEDY_SCOPE_UNSEEN)):
            with self.subTest(scope_file=scope_file):
                self.marker().unlink(missing_ok=True)
                sandbox = FakeSandbox(envelope([]), scope_declared=True)
                with mock.patch.dict(os.environ, {fleet_scope_targets.SCOPE_FILE_ENV: scope_file}):
                    code, out = self.run_tick(sandbox)
                self.assertEqual(code, 0)
                self.assertEqual([argv for kind, argv, _ in sandbox.calls if kind == "versions"], [[watch.PROJECT_FLAG, "p1"]])
                self.assertIn("the sandbox says the install declares a scope, but this pod reads no declaration", out)
                self.assertIn(remedy, out)
                self.assertEqual(json.loads(self.marker().read_text()), [watch.FAILURE_KEY_REFUSED, [["p1", watch.REFUSAL_SANDBOX]]])

    def test_an_all_refused_tick_names_every_refusal_it_is_keyed_on(self) -> None:
        """Every roster project refused, for two causes: the failure line names each
        project with its reason and remedy, the key the marker holds, so a change of
        either cause re-posts a line that says what changed."""
        self.declare_scope(["mgmt"], unread=["p2"])
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            code, out = self.run_tick(FakeSandbox(envelope([])))
        self.assertEqual(code, 0)
        self.assertIn("the tick failed: ReportRefused: p1: refused by the report script: the install's declared scope does not list p1", out)
        self.assertIn("; p2: refused by the report script: the install's declared scope lists p2 but the install could not read it", out)
        self.assertEqual(json.loads(self.marker().read_text()), [watch.FAILURE_KEY_REFUSED, sorted([["p1", watch.REFUSAL_OUTSIDE], ["p2", watch.REFUSAL_UNREAD.format(outcome="denied")]])])

    def test_an_all_refused_tick_names_six_refusals_and_counts_the_rest(self) -> None:
        """Bounded as the partial line is: a roster of outside projects posts one line
        of six refusals and a count, and the marker still keys on every one."""
        self.declare_scope(["mgmt"])
        roster = [f"p{i}" for i in range(1, 9)]
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: ",".join(roster)}):
            code, out = self.run_tick(FakeSandbox(envelope([])))
        self.assertEqual(code, 0)
        self.assertEqual(out.count("refused by the report script"), watch.MAX_NAMES_IN_LINE)
        self.assertIn(watch.NAMES_OVERFLOW.format(more=2), out)
        self.assertEqual([project for project, _ in json.loads(self.marker().read_text())[1]], roster)

    def test_a_refused_project_is_its_own_kind_of_partial_so_a_later_outage_is_still_announced(self) -> None:
        """A roster project the declared scope does not list is refused every
        tick: the partial line names it once, with the remedy on the install,
        and stays quiet while that is all; a sandbox outage on another project
        is a new signature and a new line, not the same one swallowed."""
        self.declare_scope(["p1", "p3"])
        versions = envelope([member("a", "lagging"), member("b", "lagging", project="p2"), member("c", "current", project="p3")])
        readiness = envelope([member("a", "lagging", readiness="ready")])
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2,p3"}):
            code, out = self.run_tick(FakeSandbox(versions, readiness))
            self.assertEqual(code, 0)
            self.assertIn("the version table was partial (1 read error(s)", out)
            self.assertIn(
                "p2: version table for p2 refused by the report script: the install's declared scope does not list p2; it lists p1, p3; "
                "the remedy is on the install, not this job: name the project in spec.scope, "
                "or take it out of the roster (its Cluster Agent profile, or UPGRADE_READINESS_PROJECTS)",
                out,
            )
            # The script's own remedy is for an agent; nothing of it reaches chat.
            self.assertNotIn("collector_args", out)
            self.assertNotIn("fleet_scope", out)
            self.assertIn(f"new target version {TARGET}, 1 cluster(s) pending (a)", out)
            self.assertEqual([e["kind"] for e in self.ledger()["table_errors"]], ["refused by the report script: outside the resolved scope"])
            code, out = self.run_tick(FakeSandbox(versions, readiness))
            self.assertEqual((code, out.strip()), (0, ""))
            code, out = self.run_tick(FakeSandbox(versions, readiness, table_failing_projects={"p1"}))
            self.assertEqual(code, 0)
            self.assertIn("the version table was partial (2 read error(s)", out)
            self.assertIn("p1: version table for p1 failed: sandbox exited 255 without a report: ssh: lost connection", out)
            self.assertCountEqual([e["kind"] for e in self.ledger()["table_errors"]], ["refused by the report script: outside the resolved scope", "table failed"])

    def test_pending_clusters_of_a_refused_project_are_an_ungraded_line_in_the_install_s_terms_posted_once_then_weekly(self) -> None:
        """A project the scope does not list but whose clusters the ledger
        pended before (declared later, or dropped from the declaration) keeps
        them pending; its readiness run is refused every due day, and the
        ungraded line says why in the install's terms, once, and then, as for
        an unreachable cluster, at the weekly refresh: the condition needs the
        operator, and the weekly line carries the remedy."""
        self.declare_scope(["p1"])
        self.seed(OLDER_TARGET, None, ["p2/us-central1-a/c"])
        versions = envelope([member("a", "current")])
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            code, out = self.run_tick(FakeSandbox(versions))
            self.assertEqual(code, 0)
            self.assertIn(
                f"new target version {OLDER_TARGET}, 1 cluster(s) pending (c): none graded (readiness run for p2 refused by the report script: "
                "the install's declared scope does not list p2; it lists p1; the remedy is on the install, not this job: name the project in spec.scope",
                out,
            )
            self.assertNotIn("collector_args", out)
            self.assertEqual(self.ledger()["announced"]["ungraded"][OLDER_TARGET]["signature"], [["p2", "readiness run refused: outside the resolved scope"]])
            self.assertEqual(self.ledger()["targets"][OLDER_TARGET]["pending"], ["p2/us-central1-a/c"])
            code, out = self.run_tick(FakeSandbox(versions))
            self.assertEqual((code, out.strip()), (0, ""))
            code, out = self.run_tick(FakeSandbox(versions))
            self.assertIn("none graded (readiness run for p2 refused by the report script: the install's declared scope does not list p2; it lists p1;", out)
            self.assertIn("not graded on 3 consecutive attempt(s)", out)
            self.assertIn("next attempt at the weekly refresh", out)
            self.assertTrue(self.ledger()["announced"]["ungraded"][OLDER_TARGET]["parked"])

    def test_a_refused_readiness_run_that_turns_into_a_failed_one_is_announced_again(self) -> None:
        self.declare_scope(["p1"])
        self.seed(OLDER_TARGET, None, ["p2/us-central1-a/c"])
        versions = envelope([member("a", "current")])
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            self.run_tick(FakeSandbox(versions))
            code, out = self.run_tick(FakeSandbox(versions, failing_projects={"p2"}))
            self.assertEqual(code, 0)
            self.assertIn("none graded (readiness run for p2 failed: sandbox exited 255 without a report: ssh: lost connection)", out)
            self.assertEqual(self.ledger()["announced"]["ungraded"][OLDER_TARGET]["signature"], [["p2", "readiness run failed"]])

    def test_an_all_failed_tick_names_a_failure_before_a_refusal(self) -> None:
        """A refused outside project that sorts first must not mask the real
        failure on the one project the install can read."""
        self.declare_scope(["mgmt-prod"])
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "alpha-legacy,mgmt-prod"}):
            code, out = self.run_tick(FakeSandbox(envelope([]), table_failing_projects={"mgmt-prod"}))
        self.assertEqual(code, 0)
        self.assertIn("the tick failed: RuntimeError: sandbox exited 255 without a report: ssh: lost connection", out)
        self.assertNotIn("refused", out)

    def test_a_tick_whose_every_project_is_refused_fails_in_the_install_s_terms(self) -> None:
        self.declare_scope(["p1"])
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p2"}):
            code, out = self.run_tick(FakeSandbox(envelope([])), ["--dry-run"])
            self.assertEqual(code, 0)
            self.assertIn("the tick failed: ReportRefused:", out)
            self.assertFalse((self.home / watch.LEDGER_FILE_NAME).exists(), "a dry run changes nothing")
            code, out = self.run_tick(FakeSandbox(envelope([])))
        self.assertEqual(code, 0)
        self.assertEqual(
            out,
            self.failed(
                "ReportRefused: refused by the report script: the install's declared scope does not list p2; "
                "it lists p1; the remedy is on the install, not this job: name the project in spec.scope, "
                "or take it out of the roster (its Cluster Agent profile, or UPGRADE_READINESS_PROJECTS)",
                watch.FAILED_REASON_REFUSED,
                admin=True,
            ),
        )
        # The failed tick writes nothing of its own to the ledger but the standing-outside record (and any id it learned, none
        # here), which the next tick's hold reads.
        self.assertEqual(self.ledger()["standing_outside"], {"p2": []})
        self.assertEqual((self.ledger()["last_tick"], self.ledger()["targets"], self.ledger()["table_runs"]), (None, {}, {}))

    def test_a_refused_number_learns_its_id_so_a_describe_outage_keeps_its_cause(self) -> None:
        """Roster mgmt and 123456, the number legacy-proj, outside the scope: the first tick's
        refusal names legacy-proj, which the watch learns, so the next tick asks for legacy-proj
        with no describe; a describe outage that morning leaves the outside cause, the partial
        line does not post again, and the standing record stays."""
        self.declare_scope(["mgmt"])
        versions = envelope([member("a", "current", project="mgmt")])
        env = {watch.PROJECTS_ENV: "mgmt,123456"}
        with mock.patch.dict(os.environ, env):
            code, first = self.run_tick(FakeSandbox(versions, aliases={"123456": "legacy-proj"}))
            self.assertEqual(code, 0)
            self.assertIn("does not list legacy-proj", first)
            self.assertEqual(self.ledger()["project_ids"].get("123456"), "legacy-proj")
            sandbox = FakeSandbox(versions, aliases={"123456": "legacy-proj"}, describe_down={"123456"})
            code, second = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(second, "", "the same refusal under the same cause posts nothing")
        self.assertIn(["--project", "legacy-proj", "--scope-projects", "mgmt"], [argv for kind, argv, _ in sandbox.calls if kind == "versions"])
        self.assertIn("legacy-proj", self.ledger()["standing_outside"])

    def test_both_spellings_of_a_refused_project_run_once_on_the_tick_its_id_is_learned(self) -> None:
        """Roster 123456 and legacy-proj, one project outside the scope: the number runs first,
        its refusal teaches the id, and the id spelling is the same project, already run."""
        self.declare_scope(["mgmt"])
        versions = envelope([member("a", "current", project="mgmt")])
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "mgmt,123456,legacy-proj"}):
            sandbox = FakeSandbox(versions, aliases={"123456": "legacy-proj"})
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        asked = [argv[1] for kind, argv, _ in sandbox.calls if kind == "versions"]
        self.assertEqual(sorted(asked), ["123456", "mgmt"], "legacy-proj is the number's project, run once")
        self.assertEqual(out.count("does not list legacy-proj"), 1)

    def test_both_spellings_refused_on_the_tick_the_id_is_learned_are_one_refusal_so_the_next_tick_is_quiet(self) -> None:
        """The number was stamped by an earlier tick that did not learn its id, so the id spelling,
        never run, goes first; the number runs after it, unlearned, and its refusal teaches the id.
        The partial line keys the project once, so the next tick, which runs it once, posts nothing."""
        self.declare_scope(["mgmt"])
        ledger = watch.empty_ledger()
        ledger[watch.TABLE_RUNS_KEY] = {"123456": (NOW - timedelta(days=1)).isoformat()}
        watch.save_ledger(self.home / watch.LEDGER_FILE_NAME, ledger)
        versions = envelope([member("a", "current", project="mgmt")])
        outs = []
        for day in range(2):
            with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "mgmt,123456,legacy-proj"}), mock.patch.object(watch, "now_utc", return_value=NOW + timedelta(days=day)):
                code, out = self.run_tick(FakeSandbox(versions, aliases={"123456": "legacy-proj"}))
            self.assertEqual(code, 0)
            outs.append(out)
        self.assertIn("does not list legacy-proj", outs[0])
        self.assertIn("(1 read error(s),", outs[0], "one refused project is one read error, whichever spellings ran")
        self.assertEqual(outs[1], "", "the same refusal, keyed once, posts nothing the next day")

    def test_an_all_refused_tick_names_a_project_once_when_both_its_spellings_ran(self) -> None:
        """The id spelling, never run, goes first; the number, stamped by an earlier tick that did
        not learn it, runs after it and teaches the id: the line, like its key, names one refusal."""
        self.declare_scope(["mgmt"])
        ledger = watch.empty_ledger()
        ledger[watch.TABLE_RUNS_KEY] = {"123456": (NOW - timedelta(days=1)).isoformat()}
        watch.save_ledger(self.home / watch.LEDGER_FILE_NAME, ledger)
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "123456,legacy-proj"}):
            sandbox = FakeSandbox(envelope([]), aliases={"123456": "legacy-proj"})
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(argv[1] for kind, argv, _ in sandbox.calls if kind == "versions"), ["123456", "legacy-proj"])
        self.assertEqual(out.count("does not list legacy-proj"), 1)
        self.assertNotIn("123456:", out)

    def test_an_all_refused_ticks_marker_keys_a_number_by_the_id_it_learned(self) -> None:
        """Whichever spelling runs, the key is the project's: the number, refused naming
        legacy-proj, is keyed as legacy-proj."""
        self.declare_scope(["mgmt"])
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "123456,legacy-proj"}):
            code, out = self.run_tick(FakeSandbox(envelope([]), aliases={"123456": "legacy-proj"}))
        self.assertEqual(code, 0)
        self.assertIn("the tick failed", out)
        self.assertEqual(json.loads(self.marker().read_text()), [watch.FAILURE_KEY_REFUSED, [["legacy-proj", watch.REFUSAL_OUTSIDE]]])

    def test_an_all_refused_ticks_failure_line_is_keyed_on_the_refusal_not_its_text(self) -> None:
        """The failure line names the resolved set, which moves with the scope's
        membership; the marker is keyed on what the partial line keys a refusal on,
        the refused projects and their causes, so a project joining the scope beside a
        standing refusal re-posts nothing, and a changed cause is a new line."""
        self.declare_scope(["p1"])
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p2"}):
            code, first = self.run_tick(FakeSandbox(envelope([])))
            self.assertEqual(code, 0)
            self.assertIn("does not list p2; it lists p1;", first)
            self.assertEqual(json.loads(self.marker().read_text()), [watch.FAILURE_KEY_REFUSED, [["p2", watch.REFUSAL_OUTSIDE]]])
            self.declare_scope(["p1", "p3"])
            code, second = self.run_tick(FakeSandbox(envelope([])))
            self.assertEqual((code, second), (0, ""), "the same refusal with a longer resolved set is not a new line")
            self.declare_scope(["p1"], unread=["p2"])
            code, third = self.run_tick(FakeSandbox(envelope([])))
            self.assertEqual(code, 0)
            self.assertIn("lists p2 but the install could not read it", third)
            self.assertEqual(json.loads(self.marker().read_text()), [watch.FAILURE_KEY_REFUSED, [["p2", watch.REFUSAL_UNREAD.format(outcome="denied")]]])

    def test_an_all_refused_tick_keeps_the_standing_record_so_its_failure_line_holds_across_a_failed_lookup(self) -> None:
        """A folder-scoped install whose roster holds nothing but p2, in no
        part of the declaration: every run is refused, the tick fails in the
        install's terms, and the failure line is keyed on the refusal's project
        and cause. The record the refusal filed is saved though the tick wrote
        nothing else, so the folder lookup failing on the next tick keeps the
        cause, and the lookup resolving after that posts nothing more:
        one failure line, not three. The record is written once, not on each
        failed tick; declaring p2 reads it, and the recovery line follows."""
        resolved = {"id": "folders/123456789012", "outcome": "ok", "projects": 0}
        failed = dict(resolved, outcome="unreachable")
        outs = []
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p2"}), mock.patch.object(watch, "save_ledger", wraps=watch.save_ledger) as saves:
            for containers in ([resolved], [failed], [resolved]):
                self.declare_scope(["p1"], containers=containers)
                code, out = self.run_tick(FakeSandbox(envelope([])))
                self.assertEqual(code, 0)
                outs.append(out)
                self.assertEqual(self.ledger()["standing_outside"], {"p2": ["folders/123456789012"]})
                self.assertIsNone(self.ledger()["last_tick"], "nothing else of the failed tick is written")
            self.assertEqual(saves.call_count, 1)
            self.assertIn(
                "the tick failed: ReportRefused: refused by the report script: the install's declared scope does not list p2; it lists p1; "
                "the remedy is on the install, not this job: name the project in spec.scope",
                outs[0],
            )
            self.assertEqual(outs[1:], ["", ""], "one failure line, not three")
            self.assertEqual(json.loads(self.marker().read_text()), [watch.FAILURE_KEY_REFUSED, [["p2", watch.REFUSAL_OUTSIDE]]])
            self.declare_scope(["p1", "p2"], containers=[resolved])
            code, out = self.run_tick(FakeSandbox(envelope([])))
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [watch.RECOVERED_LINE])
        self.assertEqual(self.ledger()["standing_outside"], {})
        self.assertFalse(self.marker().exists())

    def test_an_all_refused_number_keeps_its_learned_id_so_the_hold_survives_a_failed_lookup(self) -> None:
        """Roster 123456 alone, the number legacy-proj, outside a folder-scoped declaration:
        the failed tick saves the id it learned beside the record filed under it, so the next
        tick, the folder lookup failing, still finds the record and posts nothing."""
        resolved = {"id": "folders/123456789012", "outcome": "ok", "projects": 0}
        failed = dict(resolved, outcome="unreachable")
        outs = []
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "123456"}):
            for containers in ([resolved], [failed]):
                self.declare_scope(["p1"], containers=containers)
                code, out = self.run_tick(FakeSandbox(envelope([]), aliases={"123456": "legacy-proj"}))
                self.assertEqual(code, 0)
                outs.append(out)
                self.assertEqual(self.ledger()["project_ids"], {"123456": "legacy-proj"})
                self.assertEqual(self.ledger()["standing_outside"], {"legacy-proj": ["folders/123456789012"]})
        self.assertIn("does not list legacy-proj", outs[0])
        self.assertEqual(outs[1], "", "the hold holds through the failed lookup: no second line")

    def test_an_all_failed_tick_keeps_the_record_of_the_project_it_refused_beside_the_failure_it_names(self) -> None:
        """p1's sandbox run fails beside p2's refusal: the tick fails with the
        sandbox's failure, and p2's record is saved all the same, so the
        sandbox recovering on a day the folder lookup fails posts p2 under
        the declaration remedy, not the lookup's."""
        versions = envelope([member("a", "current")])
        resolved = {"id": "folders/123456789012", "outcome": "ok", "projects": 0}
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            self.declare_scope(["p1"], containers=[resolved])
            code, out = self.run_tick(FakeSandbox(versions, table_failing_projects={"p1"}))
            self.assertIn("the tick failed: RuntimeError: sandbox exited 255 without a report", out)
            self.assertEqual(self.ledger()["standing_outside"], {"p2": ["folders/123456789012"]})
            self.declare_scope(["p1"], containers=[dict(resolved, outcome="unreachable")])
            code, out = self.run_tick(FakeSandbox(versions))
            self.assertEqual(out.splitlines()[0], watch.RECOVERED_LINE)
            self.assertIn("p2: version table for p2 refused by the report script: the install's declared scope does not list p2; it lists p1; the remedy is on the install, not this job: name the project in spec.scope", out)
            self.assertNotIn("the declaration is not fully resolved", out)

    def test_a_tick_that_fails_after_the_table_keeps_the_standing_outside_record(self) -> None:
        """The table files p2 as outside and reads p1, whose pending cluster
        is due, so the readiness hop runs; then the tick fails: the hop's
        sandbox call raises, or the report cannot be written. Nothing of the
        tick's own reaches the ledger, and the record its table filed is kept
        all the same (the round-9 wrapper covered the table's failure alone),
        so the folder lookup failing on the next tick posts the partial line
        once, with the declaration remedy, the announcement having been lost
        with the ledger, and the lookup resolving after that posts nothing;
        without the record the line would carry the lookup's remedy and then
        the declaration's. A dry run that fails after the table writes
        nothing."""
        versions = envelope([member("a", "lagging")])
        readiness = envelope([member("a", "lagging", readiness="ready")])
        resolved = {"id": "folders/123456789012", "outcome": "ok", "projects": 0}
        failed = dict(resolved, outcome="unreachable")
        held = (
            "p2: version table for p2 refused by the report script: the install's declared scope does not list p2; it lists p1; "
            "the remedy is on the install, not this job: name the project in spec.scope"
        )
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            self.declare_scope(["p1"], containers=[resolved])
            with mock.patch.object(watch, "decide", side_effect=RuntimeError("boom")):
                code, out = self.run_tick(FakeSandbox(versions, readiness), ["--dry-run"])
            self.assertEqual((code, out), (0, self.failed("RuntimeError: boom")))
            self.assertFalse((self.home / watch.LEDGER_FILE_NAME).exists(), "a dry run changes nothing")
            for failure in ("the readiness hop raised", "the report could not be written"):
                with self.subTest(failure):
                    (self.home / watch.LEDGER_FILE_NAME).unlink(missing_ok=True)
                    self.marker().unlink(missing_ok=True)
                    self.declare_scope(["p1"], containers=[resolved])
                    if failure == "the readiness hop raised":
                        code, out = self.run_tick(FakeSandbox(versions, readiness, readiness_error=OSError("sandbox: cannot fork")))
                        self.assertEqual((code, out), (0, self.failed("OSError: sandbox: cannot fork")))
                    else:
                        with mock.patch.object(watch, "write_report", side_effect=OSError("disk full")):
                            code, out = self.run_tick(FakeSandbox(versions, readiness))
                        self.assertEqual(
            (code, out),
            (
                0,
                f"{watch.FAILED_WRITE_LINE.format(reason=watch.FAILED_REASON_WRITE, next=watch.FAILED_NEEDS_ADMIN)}\n"
                "upgrade readiness watch: writing the ledger or the report failed: disk full\n",
            ),
        )
                    self.assertEqual(self.ledger()["standing_outside"], {"p2": ["folders/123456789012"]})
                    self.assertEqual((self.ledger()["last_tick"], self.ledger()["targets"], self.ledger()["announced"]), (None, {}, {}), "nothing else of the failed tick is written")
                    self.declare_scope(["p1"], containers=[failed])
                    code, out = self.run_tick(FakeSandbox(versions, readiness))
                    self.assertEqual(code, 0)
                    self.assertIn(held, out)
                    self.assertNotIn("the declaration is not fully resolved", out)
                    self.assertEqual([e["kind"] for e in self.ledger()["table_errors"]], ["refused by the report script: outside the resolved scope"])
                    self.declare_scope(["p1"], containers=[resolved])
                    code, out = self.run_tick(FakeSandbox(versions, readiness))
                    self.assertEqual((code, out.strip()), (0, ""))

    def test_a_ledger_the_failed_tick_cannot_reread_does_not_replace_its_failure(self) -> None:
        """The report write fails after the table filed p2 as outside; the ledger the
        failure handler re-reads is one this process rejects. The write failure, with
        its own exit, is what the tick reports."""
        versions = envelope([member("a", "lagging")])
        readiness = envelope([member("a", "lagging", readiness="ready")])
        resolved = {"id": "folders/123456789012", "outcome": "ok", "projects": 0}
        real_load = watch.load_ledger
        reads = []

        def load(path):
            reads.append(path)
            if len(reads) > 1:
                raise RuntimeError("the ledger is not valid JSON; refusing to overwrite it")
            return real_load(path)

        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            self.declare_scope(["p1"], containers=[resolved])
            with mock.patch.object(watch, "write_report", side_effect=OSError("disk full")), mock.patch.object(watch, "load_ledger", side_effect=load):
                code, out = self.run_tick(FakeSandbox(versions, readiness))
        self.assertEqual(len(reads), 2, "the tick's read, then the handler's")
        self.assertEqual(
            (code, out),
            (
                0,
                f"{watch.FAILED_WRITE_LINE.format(reason=watch.FAILED_REASON_WRITE, next=watch.FAILED_NEEDS_ADMIN)}\n"
                "upgrade readiness watch: writing the ledger or the report failed: disk full\n",
            ),
        )

    def test_a_refusal_after_another_refusal_on_stderr_names_its_own_project(self) -> None:
        """One line per refused id, a number's ahead of an id's: the project is the
        id the second line names, never a span running across the first."""
        label = "(declared scope: fleet_scope): "
        stderr = (
            label + fleet_scope_args.PROJECT_NUMBER_UNRESOLVED_ERROR.format(number="123456789012") + "\n"
            + label + fleet_scope_args.DECLARED_SCOPE_OVERRIDE_OUTSIDE_ERROR.format(project="acme-prod", projects="mgmt") + "\n"
        )
        self.assertEqual(watch.refused_project(stderr), "acme-prod")

    def test_each_refusal_is_re_said_from_the_script_s_own_message_with_the_remedy_and_cause_it_has(self) -> None:
        label = "(declared scope: fleet_scope): "
        roster = "or take it out of the roster (its Cluster Agent profile, or UPGRADE_READINESS_PROJECTS)"
        outside, unread = fleet_scope_args.DECLARED_SCOPE_OVERRIDE_OUTSIDE_ERROR, fleet_scope_args.DECLARED_SCOPE_OVERRIDE_UNREAD_ERROR
        resolved = scope_targets("p1")
        wide = ", ".join(f"tenant-{i:03d}" for i in range(150))
        self.assertEqual(
            watch.refusal_detail(label + outside.format(project="p2", projects=wide) + "\n", resolved),
            (
                "refused by the report script: the install's declared scope does not list p2; it lists tenant-000, tenant-001, tenant-002, tenant-003, "
                f"tenant-004, tenant-005, +144 more; the remedy is on the install, not this job: name the project in spec.scope, {roster}",
                "outside the resolved scope",
            ),
        )
        self.assertEqual(
            watch.refusal_detail(label + unread.format(project="p2", outcome="denied"), resolved),
            (
                "refused by the report script: the install's declared scope lists p2 but the install could not read it (denied); "
                f"the remedy is on the install, not this job: give the install read access to the project, {roster}",
                "listed but unread (denied)",
            ),
        )
        self.assertEqual(
            watch.refusal_detail(label + fleet_scope_args.PROJECT_NUMBER_UNRESOLVED_ERROR.format(number="123456789012"), resolved),
            (
                "refused by the report script: the report script could not resolve project number 123456789012 to an id; "
                "the remedy is on the install, not this job: spell the project by its id (spec.harness.projectId, or UPGRADE_READINESS_PROJECTS for a hand run), or grant the agent's identity resourcemanager.projects.get on it so the reporter can resolve the number",
                "project number unresolved",
            ),
        )
        # The sandbox froze the marker at its start; this pod reads the declaration live and finds
        # none, so it handed no flags (the tick that meets this refusal has no scope in hand).
        with mock.patch.dict(os.environ, {fleet_scope_targets.SCOPE_FILE_ENV: "/etc/kube-agents/scope.json"}):
            self.assertEqual(
                watch.refusal_detail(label + fleet_scope_args.DECLARED_SCOPE_ARGS_MISSING_ERROR, None),
                (
                    "refused by the report script: the sandbox says the install declares a scope, but this pod reads no declaration; "
                    "the sandbox started under a spec.scope block this pod no longer reads; nothing to do: the sandbox restarts under the new declaration and the next tick runs again",
                    "sandbox marker stale",
                ),
            )
        # A run that cannot see a declaration at all hands no flags for another reason: the fix is its environment.
        with mock.patch.dict(os.environ, {fleet_scope_targets.SCOPE_FILE_ENV: ""}):
            detail, _cause = watch.refusal_detail(label + fleet_scope_args.DECLARED_SCOPE_ARGS_MISSING_ERROR, None)
        self.assertIn("this run reads no scope declaration (KUBEAGENTS_SCOPE_FILE is unset)", detail)
        self.assertNotIn("nothing to do", detail)
        # The remedy and the cause follow what the message captured, not the message: one unread message covers a
        # project over the cap, one an hour from being resolved, and one the install cannot reach.
        detail, cause = watch.refusal_detail(label + unread.format(project="p2", outcome="over-cap"), resolved)
        self.assertTrue(detail.endswith(f"not this job: raise spec.scope.maxProjects, {roster}"), detail)
        self.assertEqual(cause, "listed but unread (over-cap)")
        detail, cause = watch.refusal_detail(label + unread.format(project="p2", outcome="unresolved"), resolved)
        self.assertTrue(detail.endswith("; " + watch.REMEDY_UNRESOLVED), detail)
        self.assertEqual(cause, "listed but unread (unresolved)")
        # The reconcile's listing did not finish (a timeout, a carried tick): not an access failure, so no grant is asked for.
        for outcome in ("unreachable", "unknown"):
            detail, cause = watch.refusal_detail(label + unread.format(project="p2", outcome=outcome), resolved)
            self.assertTrue(detail.endswith("; " + watch.REMEDY_LISTING_UNFINISHED), detail)
            self.assertNotIn("read access", detail)
            self.assertEqual(cause, f"listed but unread ({outcome})")
        # Outside a scope the reconcile has not resolved (the render window with a management project known by
        # number; a carried tick): the line names both ways out, and the cause changes when the scope resolves.
        numeric = fleet_scope_targets.ScopeTargets(projects=(), unread=(("declared-scope", "unresolved"),), resolved_at=None, path="render", note=fleet_scope_targets.RENDER_NUMERIC_HOST_NOTE)
        self.assertEqual(
            watch.refusal_detail(label + outside.format(project="my-mgmt", projects="none readable"), numeric),
            (
                "refused by the report script: the declared scope resolved to no readable project this tick, so it does not list my-mgmt; "
                + watch.REMEDY_OUTSIDE_UNRESOLVED.format(note=fleet_scope_targets.RENDER_NUMERIC_HOST_NOTE),
                "outside a scope not yet resolved",
            ),
        )
        carried = scope_targets("p1", unread=(("declared-scope", "unresolved"),))
        self.assertEqual(
            watch.refusal_detail(label + outside.format(project="p2", projects="p1"), carried),
            (
                "refused by the report script: the install's declared scope does not list p2; it lists p1; "
                + watch.REMEDY_OUTSIDE_UNRESOLVED.format(note=watch.PARTLY_UNRESOLVED_SCOPE_NOTE),
                "outside a scope not yet resolved",
            ),
        )
        self.assertIn("a lookup that keeps failing needs the install's read access", watch.REMEDY_OUTSIDE_UNRESOLVED)
        # The last tick filed the project as outside the resolved scope: a lookup that fails this tick carries the
        # members it had, so the cause (and the signature) holds; the scope resolving is the news, not this.
        self.assertEqual(
            watch.refusal_detail(label + outside.format(project="p2", projects="p1"), carried, outside_stands=True),
            (
                "refused by the report script: the install's declared scope does not list p2; it lists p1; "
                f"the remedy is on the install, not this job: name the project in spec.scope, {roster}",
                "outside the resolved scope",
            ),
        )
        # A project an exclude entry matches has no row, so the script's message is the outside one; the scope the
        # pod holds says which, whatever the scope resolved. Whether the entry is why is the reconcile's knowledge
        # (a project under no part of the declaration matches a glob the same way), so the remedy names the entry
        # first and the declaration after it, and the roster.
        for scope in (scope_targets("p1", excluded=("legacy-*",)), scope_targets("p1", unread=(("declared-scope", "unresolved"),), excluded=("legacy-01",))):
            self.assertEqual(
                watch.refusal_detail(label + outside.format(project="legacy-01", projects="p1"), scope),
                (
                    "refused by the report script: the install's declared scope does not list legacy-01, and a spec.scope.exclude.projects entry matches it; "
                    "the remedy is on the install, not this job: drop the entry if spec.scope names the project or a folder, organisation or selector that holds it, "
                    f"else name the project in spec.scope, {roster}",
                    "excluded by spec.scope.exclude",
                ),
            )
        # An entry that is a project number matches no id: the reconcile drops a member a Metrics Scope named by that
        # number on every route and records nothing the pod can match, so the declaration remedy names the entry too;
        # with no number entry it stands as it was.
        detail, cause = watch.refusal_detail(label + outside.format(project="legacy", projects="p1"), scope_targets("p1", excluded=("123456789012",)))
        self.assertEqual(cause, "outside the resolved scope")
        self.assertTrue(
            detail.endswith(f"name the project in spec.scope, or, if the project number a spec.scope.exclude.projects entry names is this project's, drop that entry, {roster}"),
            detail,
        )
        detail, _ = watch.refusal_detail(label + outside.format(project="p2", projects="p1"), scope_targets("p1", excluded=("legacy-*",)))
        self.assertTrue(detail.endswith(f"not this job: name the project in spec.scope, {roster}"), detail)
        # A project the declaration dropped has a retiring row until the prune: the reconcile has judged it, so the
        # cause is its own whatever the scope's state, and the remedy is nothing to do, never the declaration or the roster.
        for scope in (scope_targets("p1", retiring=("p2",)), scope_targets("p1", unread=(("declared-scope", "unresolved"),), retiring=("p2",))):
            self.assertEqual(
                watch.refusal_detail(label + outside.format(project="p2", projects="p1"), scope, outside_stands=True),
                (
                    "refused by the report script: the install's declared scope no longer lists p2; the reconcile has it retiring; "
                    "nothing to do: the project left the declaration, and the reconcile prunes its Cluster Agent profile on its next clean run; "
                    "if the removal was a mistake, put the project back under spec.scope, and the next reconcile run that resolves it puts it back in the sweep",
                    "retiring from the declaration",
                ),
            )
        # An exclude entry that matches a retiring project is the detail the undo names: the reconcile drops an explicit
        # project the entry matches and marks its row retiring as it does a removal, so the project may still be under
        # spec.scope and "put it back" would undo nothing. The cause stays the reconcile's verdict, under a resolved and
        # a carried scope alike, and the roster is still not named.
        for scope in (scope_targets("p1", excluded=("p2",), retiring=("p2",)), scope_targets("p1", unread=(("declared-scope", "unresolved"),), excluded=("p*",), retiring=("p2",))):
            self.assertEqual(
                watch.refusal_detail(label + outside.format(project="p2", projects="p1"), scope),
                (
                    "refused by the report script: the install's declared scope no longer lists p2, and a spec.scope.exclude.projects entry matches it; "
                    "the reconcile has it retiring; nothing to do: the project left the declaration, and the reconcile prunes its Cluster Agent profile "
                    "on its next clean run; if the entry was a mistake, drop it, name the project in spec.scope if nothing there holds it, and the next "
                    "reconcile run that resolves the project puts it back in the sweep",
                    "retiring from the declaration",
                ),
            )
        for remedy in (watch.REMEDY_RETIRING, watch.REMEDY_RETIRING_EXCLUDED):
            self.assertNotIn(roster, remedy)
        self.assertNotIn("put the project back under spec.scope", watch.REMEDY_RETIRING_EXCLUDED)
        # A remedy states no cadence: the same detail reaches a line posted once per cause and the ungraded line,
        # which repeats weekly once parked, so neither unread remedy says when the line returns.
        for remedy in (watch.REMEDY_UNRESOLVED, watch.REMEDY_LISTING_UNFINISHED):
            self.assertIn("the project stays unread until a reconcile tick lists it", remedy)
            self.assertNotIn("posts once", remedy)
            self.assertNotIn("if it does not", remedy)
        for detail, _ in (
            watch.refusal_detail(label + m, resolved) for m in (
                outside.format(project="p2", projects="p1"),
                unread.format(project="p2", outcome="denied"),
                fleet_scope_args.PROJECT_NUMBER_UNRESOLVED_ERROR.format(number="1"),
                fleet_scope_args.DECLARED_SCOPE_ARGS_MISSING_ERROR,
            )
        ):
            self.assertNotIn("collector_args", detail)
            self.assertNotIn("fleet_scope tool", detail)
        # A usage exit this job does not know is not a refusal: it stays the plain failure, as the script wrote it.
        self.assertIsNone(watch.refusal_detail("no project: pass --project\n", resolved))

    def test_a_refusal_whose_cause_changes_is_announced_again_with_the_new_remedy(self) -> None:
        """The operator names p2 in spec.scope and the reconcile records it
        denied: the remedy moves from the declaration to read access, so the
        line posts again; the same refusal after that is quiet."""
        versions = envelope([member("a", "current")])
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            self.declare_scope(["p1"])
            code, out = self.run_tick(FakeSandbox(versions))
            self.assertIn("p2: version table for p2 refused by the report script: the install's declared scope does not list p2; it lists p1; the remedy is on the install, not this job: name the project in spec.scope", out)
            self.declare_scope(["p1"], unread=["p2"])
            code, out = self.run_tick(FakeSandbox(versions))
            self.assertIn(
                "p2: version table for p2 refused by the report script: the install's declared scope lists p2 but the install could not read it (denied); "
                "the remedy is on the install, not this job: give the install read access to the project",
                out,
            )
            self.assertNotIn("name the project in spec.scope", out)
            code, out = self.run_tick(FakeSandbox(versions))
            self.assertEqual((code, out.strip()), (0, ""))

    def test_a_declared_project_the_reconcile_cannot_list_is_announced_once_per_outcome(self) -> None:
        """The reconcile's listing of p2 times out every run, so the snapshot
        records it unreachable on every tick. The partial line posts once,
        with the listing remedy, and does not return while that outcome
        stands, since its key holds the project and the cause (the remedy
        itself states no cadence); the reconcile recording
        another outcome (unresolved, then a denial) is a new cause, named with
        its remedy, and the project being read clears the table."""
        versions = envelope([member("a", "current")])
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            self.declare_scope(["p1"], unread=["p2=unreachable"])
            code, out = self.run_tick(FakeSandbox(versions))
            self.assertEqual(code, 0)
            self.assertIn(
                "p2: version table for p2 refused by the report script: the install's declared scope lists p2 but the install could not read it (unreachable); "
                + watch.REMEDY_LISTING_UNFINISHED,
                out,
            )
            self.assertIn("the project stays unread until a reconcile tick lists it", out)
            self.assertNotIn("if it does not", out)
            self.assertEqual([e["kind"] for e in self.ledger()["table_errors"]], ["refused by the report script: listed but unread (unreachable)"])
            for _ in range(3):
                code, out = self.run_tick(FakeSandbox(versions))
                self.assertEqual((code, out.strip()), (0, ""))
            self.declare_scope(["p1"], unread=["p2=unresolved"])
            code, out = self.run_tick(FakeSandbox(versions))
            self.assertIn("could not read it (unresolved); " + watch.REMEDY_UNRESOLVED, out)
            code, out = self.run_tick(FakeSandbox(versions))
            self.assertEqual((code, out.strip()), (0, ""))
            self.declare_scope(["p1"], unread=["p2"])
            code, out = self.run_tick(FakeSandbox(versions))
            self.assertIn("could not read it (denied); the remedy is on the install, not this job: give the install read access to the project", out)
            self.declare_scope(["p1", "p2"])
            code, out = self.run_tick(FakeSandbox(versions))
            self.assertIn("the version table reads every project again", out)

    def test_a_project_the_declaration_dropped_is_retiring_so_the_line_says_nothing_to_do(self) -> None:
        """The operator takes p2 out of spec.scope. The reconcile's first clean
        run marks its row retiring and keeps its profile until the next, so
        the roster still names p2 for one reconcile interval (longer while a
        lookup is unclean or the prune's delete fails) and the report refuses
        it as outside. The line says the reconcile has it retiring and that
        there is nothing to do, on both hops, never the declaration remedy,
        which would undo the edit or delete a profile about to go; an unclean
        run in between changes nothing; the prune clears the table; and a
        profile onboarded by hand after it has no row, so the line returns
        with the declaration remedy."""
        self.seed(OLDER_TARGET, None, ["p2/us-central1-a/c"])
        versions = envelope([member("a", "current")])
        resolved = {"id": "folders/123456789012", "outcome": "ok", "projects": 0}
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            self.declare_scope(["p1"], containers=[resolved], retiring=["p2"])
            code, out = self.run_tick(FakeSandbox(versions))
            self.assertEqual(code, 0)
            self.assertIn(
                "p2: version table for p2 refused by the report script: the install's declared scope no longer lists p2; the reconcile has it retiring; "
                "nothing to do: the project left the declaration, and the reconcile prunes its Cluster Agent profile on its next clean run; "
                "if the removal was a mistake, put the project back under spec.scope, and the next reconcile run that resolves it puts it back in the sweep",
                out,
            )
            self.assertIn(
                "none graded (readiness run for p2 refused by the report script: the install's declared scope no longer lists p2; the reconcile has it retiring; nothing to do",
                out,
            )
            self.assertNotIn("the remedy is on the install", out)
            self.assertNotIn("take it out of the roster", out)
            self.assertEqual([e["kind"] for e in self.ledger()["table_errors"]], ["refused by the report script: retiring from the declaration"])
            self.assertEqual(self.ledger()["announced"]["ungraded"][OLDER_TARGET]["signature"], [["p2", "readiness run refused: retiring from the declaration"]])
            self.assertEqual(self.ledger()["standing_outside"], {})
            # An unclean run holds the row at retiring and leaves the scope unresolved: the reconcile's verdict stands.
            self.declare_scope(["p1"], containers=[dict(resolved, outcome="unreachable")], retiring=["p2"])
            code, out = self.run_tick(FakeSandbox(versions))
            self.assertEqual((code, out.strip()), (0, ""))
            self.assertEqual([e["kind"] for e in self.ledger()["table_errors"]], ["refused by the report script: retiring from the declaration"])
        # The prune lands: the profile is gone, the roster is p1 again, and the table is complete.
        self.declare_scope(["p1"], containers=[resolved])
        code, out = self.run_tick(FakeSandbox(versions))
        self.assertEqual(code, 0)
        self.assertIn("the version table reads every project again", out)
        # Onboarded by hand after the prune: no row, so outside the resolved scope, and the line returns with the declaration remedy.
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            code, out = self.run_tick(FakeSandbox(versions))
            self.assertIn(
                "p2: version table for p2 refused by the report script: the install's declared scope does not list p2; it lists p1; "
                "the remedy is on the install, not this job: name the project in spec.scope",
                out,
            )
            self.assertEqual(self.ledger()["standing_outside"], {"p2": ["folders/123456789012"]})

    def test_a_retiring_project_an_exclude_entry_matches_is_told_to_drop_the_entry_not_to_declare_it_again(self) -> None:
        """The operator adds p2 to spec.scope.exclude.projects and leaves it
        under spec.scope.projects: the reconcile drops it on the explicit
        route and marks its row retiring on its first clean run, as it does a
        removal. The project is still under spec.scope, so "put it back under
        spec.scope" undoes nothing; on both hops the line names the entry as
        the undo and the declaration after it, under the retiring cause, and
        the entry dropped reads the project again."""
        self.seed(OLDER_TARGET, None, ["p2/us-central1-a/c"])
        versions = envelope([member("a", "current")])
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            self.declare_scope(["p1"], exclude=["p2"], retiring=["p2"])
            code, out = self.run_tick(FakeSandbox(versions))
            self.assertEqual(code, 0)
            self.assertIn(
                "p2: version table for p2 refused by the report script: the install's declared scope no longer lists p2, and a spec.scope.exclude.projects "
                "entry matches it; the reconcile has it retiring; nothing to do: the project left the declaration, and the reconcile prunes its Cluster Agent "
                "profile on its next clean run; if the entry was a mistake, drop it, name the project in spec.scope if nothing there holds it, and the next "
                "reconcile run that resolves the project puts it back in the sweep",
                out,
            )
            self.assertIn(
                "none graded (readiness run for p2 refused by the report script: the install's declared scope no longer lists p2, and a "
                "spec.scope.exclude.projects entry matches it; the reconcile has it retiring; nothing to do",
                out,
            )
            self.assertNotIn("put the project back under spec.scope", out)
            self.assertNotIn("the remedy is on the install", out)
            self.assertEqual([e["kind"] for e in self.ledger()["table_errors"]], ["refused by the report script: retiring from the declaration"])
            self.assertEqual(self.ledger()["announced"]["ungraded"][OLDER_TARGET]["signature"], [["p2", "readiness run refused: retiring from the declaration"]])
            self.assertEqual(self.ledger()["standing_outside"], {})
            # The entry dropped: the reconcile resolves p2 again and the table reads it.
            self.declare_scope(["p1", "p2"])
            code, out = self.run_tick(FakeSandbox(versions))
            self.assertEqual(code, 0)
            self.assertIn("the version table reads every project again", out)

    def test_a_second_refused_project_is_announced_while_the_first_stands(self) -> None:
        versions = envelope([member("a", "current")])
        self.declare_scope(["p1"])
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            code, out = self.run_tick(FakeSandbox(versions))
            self.assertIn("p2: version table for p2 refused", out)
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2,p4"}):
            code, out = self.run_tick(FakeSandbox(versions))
            self.assertIn("the version table was partial (2 read error(s)", out)
            self.assertIn("p4: version table for p4 refused", out)
            code, out = self.run_tick(FakeSandbox(versions))
            self.assertEqual((code, out.strip()), (0, ""))

    def test_a_standing_outside_refusal_is_not_re_posted_when_a_container_lookup_fails_for_a_tick(self) -> None:
        """A folder-scoped install whose roster holds p2, in no part of the
        declaration. The first tick's folder lookup failed, so the line names
        both ways out; the scope resolving is news, and the line returns with
        the declaration remedy; a lookup that fails again carries the members
        it had, so p2 is no more inside than it was, and the line stays quiet
        on the way out and on the way back."""
        versions = envelope([member("a", "current")])
        resolved = {"id": "folders/123456789012", "outcome": "ok", "projects": 0}
        failed = dict(resolved, outcome="unreachable")
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            self.declare_scope(["p1"], containers=[failed])
            code, out = self.run_tick(FakeSandbox(versions))
            self.assertIn("p2: version table for p2 refused by the report script: the install's declared scope does not list p2; it lists p1; the declaration is not fully resolved", out)
            self.assertEqual([e["kind"] for e in self.ledger()["table_errors"]], ["refused by the report script: outside a scope not yet resolved"])
            self.declare_scope(["p1"], containers=[resolved])
            code, out = self.run_tick(FakeSandbox(versions))
            self.assertIn("p2: version table for p2 refused by the report script: the install's declared scope does not list p2; it lists p1; the remedy is on the install, not this job: name the project in spec.scope", out)
            self.assertEqual([e["kind"] for e in self.ledger()["table_errors"]], ["refused by the report script: outside the resolved scope"])
            for containers in ([failed], [resolved]):
                self.declare_scope(["p1"], containers=containers)
                code, out = self.run_tick(FakeSandbox(versions))
                self.assertEqual((code, out.strip()), (0, ""))
                self.assertEqual([e["kind"] for e in self.ledger()["table_errors"]], ["refused by the report script: outside the resolved scope"])

    def test_the_readiness_hop_keeps_a_standing_outside_refusal_s_signature_across_a_failed_lookup(self) -> None:
        self.seed(OLDER_TARGET, None, ["p2/us-central1-a/c"])
        versions = envelope([member("a", "current")])
        resolved = {"id": "folders/123456789012", "outcome": "ok", "projects": 0}
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            self.declare_scope(["p1"], containers=[resolved])
            code, out = self.run_tick(FakeSandbox(versions))
            self.assertIn("none graded (readiness run for p2 refused by the report script: the install's declared scope does not list p2; it lists p1; the remedy is on the install, not this job: name the project in spec.scope", out)
            self.assertEqual(self.ledger()["announced"]["ungraded"][OLDER_TARGET]["signature"], [["p2", "readiness run refused: outside the resolved scope"]])
            self.declare_scope(["p1"], containers=[dict(resolved, outcome="unreachable")])
            code, out = self.run_tick(FakeSandbox(versions))
            self.assertEqual((code, out.strip()), (0, ""))
            self.assertEqual(self.ledger()["announced"]["ungraded"][OLDER_TARGET]["signature"], [["p2", "readiness run refused: outside the resolved scope"]])

    def test_a_container_the_declaration_gains_releases_the_hold_so_its_failed_lookup_is_named(self) -> None:
        """The operator answers the declaration remedy by adding a folder that
        holds p2, and the install cannot list it. That folder's failed lookup
        carries no members (the reconcile never had any), so p2 is refused as
        outside still; the hold, keyed on the containers the declaration named
        when p2 was filed, releases, and the line names the lookup and its read
        access rather than staying quiet behind the remedy it posted. The
        lookup failing again is quiet; the folder resolving without p2 is news,
        with the declaration remedy; the first folder failing after that is
        quiet again, under the record's new container set."""
        versions = envelope([member("a", "current")])
        first = {"id": "folders/111111111111", "outcome": "ok", "projects": 0}
        gained = {"id": "folders/222222222222", "outcome": "denied", "projects": 0}
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
            self.declare_scope(["p1"], containers=[first])
            code, out = self.run_tick(FakeSandbox(versions))
            self.assertIn("the remedy is on the install, not this job: name the project in spec.scope", out)
            self.assertEqual(self.ledger()["standing_outside"], {"p2": ["folders/111111111111"]})
            self.declare_scope(["p1"], containers=[first, gained])
            code, out = self.run_tick(FakeSandbox(versions))
            self.assertEqual(code, 0)
            self.assertIn("p2: version table for p2 refused by the report script: the install's declared scope does not list p2; it lists p1; the declaration is not fully resolved", out)
            self.assertIn("a lookup that keeps failing needs the install's read access on that folder, organisation or selector", out)
            self.assertEqual([e["kind"] for e in self.ledger()["table_errors"]], ["refused by the report script: outside a scope not yet resolved"])
            self.assertEqual(self.ledger()["standing_outside"], {})
            code, out = self.run_tick(FakeSandbox(versions))
            self.assertEqual((code, out.strip()), (0, ""))
            self.declare_scope(["p1"], containers=[first, dict(gained, outcome="ok")])
            code, out = self.run_tick(FakeSandbox(versions))
            self.assertIn("the remedy is on the install, not this job: name the project in spec.scope", out)
            self.assertEqual(self.ledger()["standing_outside"], {"p2": ["folders/111111111111", "folders/222222222222"]})
            self.declare_scope(["p1"], containers=[dict(first, outcome="unreachable"), dict(gained, outcome="ok")])
            code, out = self.run_tick(FakeSandbox(versions))
            self.assertEqual((code, out.strip()), (0, ""))
            self.assertEqual([e["kind"] for e in self.ledger()["table_errors"]], ["refused by the report script: outside the resolved scope"])

    def test_a_day_the_project_s_run_failed_or_was_left_unrun_does_not_forget_the_standing_cause(self) -> None:
        """The record outlives a day on which p2's table run was no verdict on
        the scope: the sandbox failed it beside a readable p1, or the budget
        left it unrun, as the rotation of a table that does not fit does every
        few days. A lookup that fails on the day after keeps the declaration
        cause (the line that posts, since the signature moved off the failure,
        carries the declaration remedy and not the lookup's), and the lookup
        resolving after that posts nothing more."""
        versions = envelope([member("a", "current")])
        resolved = {"id": "folders/123456789012", "outcome": "ok", "projects": 0}
        failed = dict(resolved, outcome="unreachable")
        late = watch.TABLE_BUDGET_SECONDS - 10.0
        for interruption in ("table failed", "unrun"):
            with self.subTest(interruption), mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2"}):
                (self.home / watch.LEDGER_FILE_NAME).unlink(missing_ok=True)
                self.declare_scope(["p1"], containers=[resolved])
                code, out = self.run_tick(FakeSandbox(versions))
                self.assertIn("name the project in spec.scope", out)
                if interruption == "table failed":
                    code, out = self.run_tick(FakeSandbox(versions, table_failing_projects={"p2"}))
                    self.assertIn("p2: version table for p2 failed: sandbox exited 255 without a report", out)
                else:
                    # started, p1's run, then p2 past the table's share of the budget.
                    with self.monotonic_readings(0.0, 0.0, late, late, late):
                        code, out = self.run_tick(FakeSandbox(versions))
                    self.assertIn("tick budget exhausted before the version table read project p2", out)
                self.assertNotIn("refused", out)
                self.assertEqual(self.ledger()["standing_outside"], {"p2": ["folders/123456789012"]}, "the record outlives a day with no verdict on the scope")
                self.declare_scope(["p1"], containers=[failed])
                code, out = self.run_tick(FakeSandbox(versions))
                self.assertEqual(code, 0)
                self.assertIn("p2: version table for p2 refused by the report script: the install's declared scope does not list p2; it lists p1; the remedy is on the install, not this job: name the project in spec.scope", out)
                self.assertNotIn("the declaration is not fully resolved", out)
                self.assertEqual([e["kind"] for e in self.ledger()["table_errors"]], ["refused by the report script: outside the resolved scope"])
                self.declare_scope(["p1"], containers=[resolved])
                code, out = self.run_tick(FakeSandbox(versions))
                self.assertEqual((code, out.strip()), (0, ""))

    def test_the_standing_outside_record_is_kept_by_both_hops(self) -> None:
        """The table hop files p2 and the readiness hop p5, each with the
        containers the declaration names; a project read again, or refused
        under another cause, leaves the record; a ledger from before the key,
        or one whose record is malformed, reads as none."""
        self.seed(OLDER_TARGET, None, ["p5/us-central1-a/c"])
        versions = envelope([member("a", "current")])
        resolved = {"id": "folders/123456789012", "outcome": "ok", "projects": 0}
        # p5's table run fails in the sandbox, so its clusters are carried forward and its record is the readiness hop's alone.
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,p2,p5"}):
            self.declare_scope(["p1"], containers=[resolved])
            self.run_tick(FakeSandbox(versions, table_failing_projects={"p5"}))
            self.assertEqual([e["kind"] for e in self.ledger()["table_errors"] if e["project"] == "p5"], ["table failed"])
            self.assertEqual(self.ledger()["standing_outside"], {"p2": ["folders/123456789012"], "p5": ["folders/123456789012"]})
            self.assertEqual(self.ledger()["announced"]["ungraded"][OLDER_TARGET]["signature"], [["p5", "readiness run refused: outside the resolved scope"]])
            # p2 is declared and read; p5 is refused as excluded: neither stands outside the resolved scope now.
            self.declare_scope(["p1", "p2"], exclude=["p5"], containers=[resolved])
            self.run_tick(FakeSandbox(versions, table_failing_projects={"p5"}))
            self.assertEqual(self.ledger()["standing_outside"], {})
            self.assertEqual(self.ledger()["announced"]["ungraded"][OLDER_TARGET]["signature"], [["p5", "readiness run refused: excluded by spec.scope.exclude"]])
        self.assertEqual(watch.empty_ledger()["standing_outside"], {})
        path = self.home / "older.json"
        ledger = watch.empty_ledger()
        ledger.pop("standing_outside")
        watch.save_ledger(path, ledger)
        self.assertEqual(watch.load_ledger(path)["standing_outside"], {})
        ledger["standing_outside"] = {"p2": ["folders/1", 7], "p3": "folders/1"}
        watch.save_ledger(path, ledger)
        self.assertEqual(watch.load_ledger(path)["standing_outside"], {"p2": ["folders/1"]})

    def test_a_roster_project_an_exclude_entry_matches_is_told_to_drop_the_entry_before_declaring_it(self) -> None:
        """An excluded project has no row, so the script's message is the
        outside one; the scope the pod holds says an entry matches it. Whether
        the entry is why is the reconcile's knowledge: the project is in
        spec.scope under exclude (its profile retiring, or onboarded by hand
        and never produced), or under no part of the declaration and matched
        by a glob all the same, where dropping the entry brings in nothing. So
        the remedy names the entry first and the declaration after it, never
        the declaration alone, which round 6 posted for a project already in
        spec.scope."""
        versions = envelope([member("a", "current")])
        self.declare_scope(["p1"], exclude=["legacy-*"])
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1,legacy-01"}):
            code, out = self.run_tick(FakeSandbox(versions))
        self.assertEqual(code, 0)
        self.assertIn(
            "legacy-01: version table for legacy-01 refused by the report script: the install's declared scope does not list legacy-01, "
            "and a spec.scope.exclude.projects entry matches it; the remedy is on the install, not this job: drop the entry if spec.scope names "
            "the project or a folder, organisation or selector that holds it, else name the project in spec.scope, "
            "or take it out of the roster (its Cluster Agent profile, or UPGRADE_READINESS_PROJECTS)",
            out,
        )
        self.assertNotIn("not this job: name the project in spec.scope", out)
        self.assertEqual([e["kind"] for e in self.ledger()["table_errors"]], ["refused by the report script: excluded by spec.scope.exclude"])

    def test_an_unreadable_render_fails_the_tick_before_any_sandbox_call(self) -> None:
        """With no roster and no project variable the projects come from a
        sandbox call; the scope is read first, so an unreadable render costs none."""
        sandbox = FakeSandbox(envelope([]))
        with mock.patch.dict(
            os.environ,
            {watch.PROJECTS_ENV: "", watch.MANAGEMENT_PROJECT_ENV: "", fleet_scope_targets.SCOPE_FILE_ENV: str(Path(self.tmp.name) / "absent.json")},
        ), mock.patch.object(watch, "roster_projects", return_value=set()):
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(sandbox.calls, [])
        self.assertIn("the operator's scope render could not be read", out)

    def test_a_roster_spelling_with_a_space_fails_the_tick_before_any_sandbox_call(self) -> None:
        """A project id or number is one token, as the refusal parser reads it: a hand-typed
        list separated by spaces is refused with its remedy, not run as one project."""
        sandbox = FakeSandbox(envelope([]))
        with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "p1 p2"}):
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(sandbox.calls, [])
        self.assertIn("the roster names 'p1 p2', which is not a project id or number; spell each project as its id, in UPGRADE_READINESS_PROJECTS separated by commas", out)

    def test_a_management_project_spelling_with_a_space_fails_the_tick_before_any_sandbox_call(self) -> None:
        """Every source of the roster is held to the same grammar: spec.harness.projectId
        reaches the pod as GCP_PROJECT_ID, and a space there is refused as one in the list is.
        A comma is refused too: that source holds one project, and the parser's token stops at it."""
        for spelling in ("foo bar", "mgmt,x"):
            with self.subTest(spelling=spelling):
                # Both are the same kind of failure, which the marker posts once.
                self.marker().unlink(missing_ok=True)
                sandbox = FakeSandbox(envelope([]))
                with mock.patch.dict(os.environ, {watch.PROJECTS_ENV: "", watch.MANAGEMENT_PROJECT_ENV: spelling}), mock.patch.object(watch, "roster_projects", return_value=set()):
                    code, out = self.run_tick(sandbox)
                self.assertEqual(code, 0)
                self.assertEqual(sandbox.calls, [])
                self.assertIn(f"the roster names {spelling!r}, which is not a project id or number", out)

    def test_the_argv_is_the_tool_s_collector_args_whatever_the_spelling(self) -> None:
        scope = scope_targets("p1", "p2", unread=(("p3", "denied"),))
        handed = ["--scope-projects", "p1,p2", "--scope-unread", "p3=denied"]
        self.assertEqual(watch.report_argv(["123456789012"], readiness=False, scope=scope), ["--project", "123456789012", *handed])
        self.assertEqual(watch.report_argv(["p1"], readiness=False, scope=scope), ["--project", "p1", *handed])
        self.assertEqual(watch.report_argv(["p1"], readiness=False, scope=None), ["--project", "p1"])
        # Nothing readable this tick: the unread flag goes alone and the report refuses, fail closed.
        self.assertEqual(watch.report_argv(["p1"], readiness=False, scope=scope_targets()), ["--project", "p1", "--scope-unread", "declared-scope=unresolved"])

    def test_an_unreadable_scope_render_fails_the_tick_closed_with_the_reason(self) -> None:
        sandbox = FakeSandbox(envelope([member("a", "lagging")]))
        with mock.patch.dict(os.environ, {fleet_scope_targets.SCOPE_FILE_ENV: str(Path(self.tmp.name) / "absent.json")}):
            code, out = self.run_tick(sandbox)
        self.assertEqual(code, 0)
        self.assertEqual(sandbox.calls, [])
        self.assertIn("the tick failed: ScopeUnreadable: the operator's scope render could not be read", out)
        self.assertIn(watch.FAILED_TICK_LINE.format(reason=watch.FAILED_REASON_SCOPE_UNREADABLE, next=watch.FAILED_NEEDS_ADMIN), out)
        self.assertIn("absent.json", out)
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
            for name in ("default", "platform", "cluster-x", "cluster-y", "cluster-z", "cluster-blank", "cluster-broken"):
                (Path(home) / "profiles" / name).mkdir(parents=True)

            def identity(path: Path):
                if path.name == "cluster-x":
                    return {"project": "tenant-a", "cluster": "x", "location": "l"}
                if path.name == "cluster-y":
                    return {"project": "tenant-b", "cluster": "y", "location": "l"}
                if path.name == "cluster-z":
                    # A hand-edited quoted scalar: stripped as every other roster source is.
                    return {"project": " tenant-c\n", "cluster": "z", "location": "l"}
                if path.name == "cluster-blank":
                    return {"project": "  ", "cluster": "b", "location": "l"}
                raise OSError("unreadable")

            with mock.patch.object(watch.gitops_workspace, "agent_home", return_value=home), mock.patch(
                "cluster_agent_profile.read_cluster_identity", side_effect=identity
            ):
                self.assertEqual(watch.roster_projects(), {"tenant-a", "tenant-b", "tenant-c"})


class Loader(unittest.TestCase):
    def setUp(self) -> None:
        self.env = mock.patch.dict(os.environ, {watch.SKILL_SCRIPTS_DIR_ENV: ""}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_the_loader_registers_the_readiness_module_and_calls_main_in_a_private_directory(self) -> None:
        source = watch.loader_source(["--project", "p1"])
        compile(source, "loader", "exec")
        self.assertIn("sys.meta_path.insert(0, Shipped())", source)
        self.assertIn('importlib.import_module("fleet_upgrade_report")', source)
        self.assertIn("report.main(ARGV + ", source)
        self.assertIn("tempfile.mkdtemp(", source)
        self.assertIn("shutil.rmtree(private", source)
        self.assertIn(watch.ENVELOPE_SENTINEL, source)
        self.assertNotIn("/tmp/upgrade-readiness", source)

    def test_the_loader_hands_the_sandbox_this_image_s_fleet_scope_args(self) -> None:
        """The report script formats a refusal with the constants this job parses it by,
        whatever copy the sandbox image carries: this image's ``fleet_scope_args`` is in
        the shipped set, known by its path here (or under a caller's root), and the finder
        serves it ahead of any copy on the sandbox's path."""
        self.assertEqual(watch.scope_args_path(), Path(watch.__file__).resolve().parent / "fleet_scope_args.py")
        source, file, is_package = watch.shipped_modules()[watch.SCOPE_ARGS_MODULE]
        self.assertEqual((source, file, is_package), (watch.scope_args_path().read_text(encoding="utf-8"), str(watch.scope_args_path()), False))
        self.assertEqual(watch.shipped_modules(root="/absent")[watch.SCOPE_ARGS_MODULE][1], "/absent/fleet_scope_args.py")
        self.assertIn(repr(watch.SCOPE_ARGS_MODULE) + ":", watch.loader_source(["--project", "p1"]))
        program = watch.loader_preamble() + "\n".join(
            [
                f"scope_args = importlib.import_module({json.dumps(watch.SCOPE_ARGS_MODULE)})",
                "print(json.dumps([scope_args.__file__, scope_args.DECLARED_SCOPE_ARGS_MISSING_ERROR]))",
            ]
        )
        with tempfile.TemporaryDirectory() as scratch:
            decoy = Path(scratch) / "fleet_scope_args.py"
            decoy.write_text("DECLARED_SCOPE_ARGS_MISSING_ERROR = 'the sandbox image copy'\n", encoding="utf-8")
            env = {"PATH": os.environ.get("PATH", ""), "HOME": scratch, "TMPDIR": scratch, "PYTHONPATH": scratch}
            completed = subprocess.run([sys.executable, "-"], input=program, capture_output=True, text=True, cwd=scratch, env=env, timeout=120)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout.strip().splitlines()[-1]), [str(watch.scope_args_path()), fleet_scope_args.DECLARED_SCOPE_ARGS_MISSING_ERROR])

    def test_the_report_argv_carries_the_projects_and_the_sandbox_side_kubeconfig_dir(self) -> None:
        self.assertEqual(
            watch.report_argv(["p1", "p2"], readiness=True, scope=None),
            ["--project", "p1", "--project", "p2", "--readiness", "--kubeconfig-dir", watch.SANDBOX_KUBECONFIG_DIR],
        )
        self.assertEqual(watch.report_argv(["p1"], readiness=False, scope=None), ["--project", "p1"])
        self.assertEqual(
            watch.report_argv(["p1", "p2"], readiness=True, scope=scope_targets("p1", "p2", "p3")),
            ["--project", "p1", "--project", "p2", "--scope-projects", "p1,p2,p3", "--readiness", "--kubeconfig-dir", watch.SANDBOX_KUBECONFIG_DIR],
        )

    @staticmethod
    def run_loader(source: str, scratch: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        """``python3 -I -`` on the loader with a gcloud that answers no clusters."""
        bin_dir = Path(scratch) / "bin"
        bin_dir.mkdir(exist_ok=True)
        fake_gcloud = bin_dir / "gcloud"
        fake_gcloud.write_text("#!/bin/sh\necho '[]'\n")
        fake_gcloud.chmod(0o755)
        env = {"PATH": str(bin_dir), "HOME": scratch, "TMPDIR": scratch, **(extra_env or {})}
        # The reporter reads the sandbox's root-owned marker before the variable; a host that
        # carries the file (an install's sandbox) would decide the scope instead of the test. Point
        # the shipped module at a path in the scratch directory, as the sibling suites do at import.
        imported = f"report = importlib.import_module({json.dumps(watch.REPORT_MODULE)})\n"
        assert source.count(imported) == 1, "the loader no longer imports the report on one line"
        absent = json.dumps(str(Path(scratch) / "scope-declared"))
        source = source.replace(imported, imported + f"importlib.import_module('fleet_scope_args').SCOPE_DECLARED_FILE = {absent}\n")
        return subprocess.run([sys.executable, "-I", "-"], input=source, capture_output=True, text=True, env=env, timeout=120)

    @staticmethod
    def envelope_of(completed: subprocess.CompletedProcess) -> dict:
        return json.loads(completed.stdout.rsplit(watch.ENVELOPE_SENTINEL, 1)[1])

    def test_the_report_argv_never_carries_the_cluster_exclusion(self) -> None:
        # #3081: the watch's report still covers the clusters
        # spec.scope.exclude.clusters excludes (#3026), as the CRD field documents.
        excluding = dataclasses.replace(scope_targets("ops-mgmt", "payments-prod"), excluded_clusters=(("ops-mgmt", "us-east1-b", "kyber-test"),))
        self.assertIn(fleet_scope_targets.SCOPE_EXCLUDE_CLUSTERS_FLAG, excluding.collector_argv())
        for readiness in (False, True):
            self.assertNotIn(fleet_scope_targets.SCOPE_EXCLUDE_CLUSTERS_FLAG, watch.report_argv(["ops-mgmt", "payments-prod"], readiness, scope=None))
            argv = watch.report_argv(["ops-mgmt", "payments-prod"], readiness, scope=excluding)
            self.assertNotIn(fleet_scope_targets.SCOPE_EXCLUDE_CLUSTERS_FLAG, argv)
            self.assertEqual(argv[4:6], ["--scope-projects", "ops-mgmt,payments-prod"])

    def test_the_shipped_set_is_every_skill_module_but_the_tests(self) -> None:
        shipped = watch.shipped_modules()
        for name in (watch.REPORT_MODULE, watch.READINESS_MODULE, "readiness_rules", "readiness_rules.audit_log", "readiness_rules.zonal_control_plane", "readiness_rules.finding", "upgrade_shape_tables"):
            self.assertIn(name, shipped)
        self.assertEqual([name for name in shipped if name.rsplit(".", 1)[-1].startswith(watch.SKILL_TEST_PREFIX)], [])
        self.assertTrue(shipped["readiness_rules"][2])
        self.assertFalse(shipped[watch.REPORT_MODULE][2])
        self.assertTrue(all(Path(entry[1]).is_file() for entry in shipped.values()))
        # The data files beside the modules ship too (the removal table), the test fixtures do not.
        data = watch.shipped_data()
        self.assertEqual(sorted(Path(path).name for path in data), ["removed_apis.json"])
        self.assertTrue(all(Path(path).is_file() for path in data))
        self.assertEqual(sorted(Path(path).name for path in watch.shipped_data(root="/elsewhere")), ["removed_apis.json"])
        self.assertTrue(all(path.startswith("/elsewhere/") for path in watch.shipped_data(root="/elsewhere")))

    def test_a_replacement_is_read_from_the_shipped_set_alone(self) -> None:
        """The shape tables read the removal table through their module's loader. With the
        set re-rooted to a directory that does not exist, as the sandbox's view of the
        agent-side paths is, the replacement still comes from the shipped data; without
        the data, the read fails loudly rather than as an empty table."""
        with tempfile.TemporaryDirectory() as scratch:
            absent = os.path.join(scratch, "absent")
            program = watch.loader_preamble(root=absent) + "\n".join(
                [
                    "tables = importlib.import_module('upgrade_shape_tables')",
                    "print(tables.replacement_for('extensions/v1beta1 networkpolicies'))",
                ]
            )
            env = {"PATH": os.environ.get("PATH", ""), "HOME": scratch, "TMPDIR": scratch}
            completed = subprocess.run([sys.executable, "-I", "-"], input=program, capture_output=True, text=True, cwd=scratch, env=env, timeout=120)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip().splitlines()[-1], "networking.k8s.io/v1")
            without_data = program.replace("DATA = {", "DATA = {}  # ", 1)
            self.assertNotEqual(without_data, program)
            completed = subprocess.run([sys.executable, "-I", "-"], input=without_data, capture_output=True, text=True, cwd=scratch, env=env, timeout=120)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("removed_apis.json could not be read beside upgrade_shape_tables", completed.stderr)

    def test_the_report_imports_from_the_shipped_set_alone(self) -> None:
        """The finder alone, in an empty directory under ``-I``: every import the report
        makes resolves from the shipped modules (or the shared scripts directory the report
        appends itself), so a module a later change imports without shipping fails here,
        not in the daily job."""
        program = watch.loader_preamble() + "\n".join(
            [
                f"report = importlib.import_module({json.dumps(watch.REPORT_MODULE)})",
                "print(json.dumps(sorted(name for name in sys.modules if name in SOURCES)))",
            ]
        )
        with tempfile.TemporaryDirectory() as scratch:
            env = {"PATH": os.environ.get("PATH", ""), "HOME": scratch, "TMPDIR": scratch}
            completed = subprocess.run([sys.executable, "-I", "-"], input=program, capture_output=True, text=True, cwd=scratch, env=env, timeout=120)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        loaded = json.loads(completed.stdout.strip().splitlines()[-1])
        for name in (watch.REPORT_MODULE, watch.READINESS_MODULE, "readiness_rules", "readiness_rules.audit_log", "readiness_rules.zonal_control_plane", "upgrade_shape_tables"):
            self.assertIn(name, loaded)

    def test_the_loader_runs_the_checkout_copy_of_the_report_script(self) -> None:
        """End to end against the real scripts, with gcloud answering no
        clusters: the envelope arrives and the private directory is gone."""
        source = watch.loader_source(["--project", "p1"])
        with tempfile.TemporaryDirectory() as scratch:
            completed = self.run_loader(source, scratch)
            self.assertIn(watch.ENVELOPE_SENTINEL, completed.stdout, completed.stderr)
            body = self.envelope_of(completed)
            self.assertEqual(body["exit"], 0)
            self.assertEqual(body["report"]["members"], [])
            self.assertIn("project", body["tables"])
            leftovers = [p.name for p in Path(scratch).iterdir() if p.name.startswith(watch.SANDBOX_TMP_PREFIX)]
            self.assertEqual(leftovers, [])

    def test_the_shipped_scope_module_is_the_one_this_job_imported(self) -> None:
        """Wherever the import resolved, not beside the script: a watch staged without the module
        beside it still ships the copy it parses refusals by."""
        with tempfile.TemporaryDirectory() as scratch:
            elsewhere = Path(scratch) / "fleet_scope_args.py"
            elsewhere.write_text(Path(fleet_scope_args.__file__).read_text(encoding="utf-8"), encoding="utf-8")
            with mock.patch.object(watch.fleet_scope_args, "__file__", str(elsewhere)):
                self.assertEqual(watch.scope_args_path(), elsewhere.resolve())
                self.assertEqual(watch.shipped_modules()[watch.SCOPE_ARGS_MODULE][1], str(elsewhere.resolve()))

    def test_the_real_report_script_on_a_scoped_sandbox_refuses_the_bare_flag_and_reads_under_the_scoped_argv(self) -> None:
        """End to end with the operator's marker set, as in the sandbox of an
        install with a ``spec.scope`` block: the bare ``--project`` is the exit
        2 the watch posted before the scope was handed on, and the argv a
        declared scope builds reads the project."""
        scoped_sandbox = {fleet_scope_args.SCOPE_DECLARED_ENV: fleet_scope_args.SCOPE_DECLARED_TRUE}
        with tempfile.TemporaryDirectory() as scratch:
            bare = self.run_loader(watch.loader_source(watch.report_argv(["p1"], readiness=False, scope=None)), scratch, scoped_sandbox)
            body = self.envelope_of(bare)
            self.assertEqual(body["exit"], 2)
            self.assertIsNone(body["report"])
            self.assertIn(fleet_scope_args.DECLARED_SCOPE_ARGS_MISSING_ERROR, bare.stderr)
            scoped = self.run_loader(watch.loader_source(watch.report_argv(["p1"], readiness=False, scope=scope_targets("p1"))), scratch, scoped_sandbox)
            body = self.envelope_of(scoped)
            self.assertEqual(body["exit"], 0, scoped.stderr)
            self.assertEqual(body["report"]["members"], [])
            self.assertNotIn(fleet_scope_args.DECLARED_SCOPE_NOTE_PREFIX, scoped.stderr)

    def test_the_real_report_script_refuses_a_project_the_handed_scope_does_not_list_with_the_reason(self) -> None:
        """A roster project outside the declared scope, or one the install could
        not read, is the report's refusal, not a read: the unread flag is what
        gives the second its reason."""
        scoped_sandbox = {fleet_scope_args.SCOPE_DECLARED_ENV: fleet_scope_args.SCOPE_DECLARED_TRUE}
        with tempfile.TemporaryDirectory() as scratch:
            outside = self.run_loader(watch.loader_source(watch.report_argv(["p2"], readiness=False, scope=scope_targets("p1"))), scratch, scoped_sandbox)
            self.assertEqual(self.envelope_of(outside)["exit"], 2)
            self.assertIn(fleet_scope_args.DECLARED_SCOPE_OVERRIDE_OUTSIDE_ERROR.format(project="p2", projects="p1"), outside.stderr)
            unread = self.run_loader(watch.loader_source(watch.report_argv(["p2"], readiness=False, scope=scope_targets("p1", unread=(("p2", "denied"),)))), scratch, scoped_sandbox)
            self.assertEqual(self.envelope_of(unread)["exit"], 2)
            self.assertIn(fleet_scope_args.DECLARED_SCOPE_OVERRIDE_UNREAD_ERROR.format(project="p2", outcome="denied"), unread.stderr)


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



class PlainFailureLine(unittest.TestCase):
    """Only the plain line reaches stdout, which the relay posts; the detail goes to stderr."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name in ("watch_home", "profile_home"):
            home = mock.patch.object(watch, name, return_value=Path(self.tmp.name))
            home.start()
            self.addCleanup(home.stop)
        env = mock.patch.dict(os.environ, {fleet_scope_targets.AGENT_HOME_ENV: self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def roster(self, expr: str) -> None:
        cron = Path(self.tmp.name) / "cron"
        cron.mkdir(exist_ok=True)
        (cron / "jobs.json").write_text(
            json.dumps({"jobs": [{"id": watch.WATCH_JOB_ID, "schedule": {"kind": "cron", "expr": expr}}]}), encoding="utf-8"
        )

    def run_main(self, error: Exception) -> tuple[str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(watch, "tick", side_effect=error), redirect_stdout(out), redirect_stderr(err):
            watch.main([])
        return out.getvalue(), err.getvalue()

    def test_stdout_is_the_plain_line_and_stderr_the_detail(self) -> None:
        self.roster("10 10 * * *")
        out, err = self.run_main(RuntimeError("report script exited 2 and wrote no report: ControlSocket /tmp/.sandbox-ssh/cm-1 already exists"))
        self.assertEqual(out, "**The upgrade readiness check didn't run today.** It hit an error. It will try again tomorrow.\n")
        self.assertIn("ControlSocket", err)
        self.assertIn("ControlSocket", (Path(self.tmp.name) / "logs" / watch.WATCH_LOG_FILE).read_text(encoding="utf-8"))

    def test_a_failure_a_retry_does_not_clear_says_an_admin_must_act(self) -> None:
        self.roster("10 10 * * *")
        for error in (watch.ProjectUnresolved("number"), watch.ReportRefused("refused", watch.REFUSAL_OUTSIDE), watch.ScopeUnreadable("render")):
            with self.subTest(error=error):
                (Path(self.tmp.name) / watch.FAILURE_MARKER_FILE_NAME).unlink(missing_ok=True)
                out, _ = self.run_main(error)
                self.assertTrue(out.endswith(" An admin needs to fix this before it can run; the details are in the agent's log.\n"))
        (Path(self.tmp.name) / watch.FAILURE_MARKER_FILE_NAME).unlink(missing_ok=True)
        out, _ = self.run_main(watch.WriteFailed("disk full"))
        self.assertEqual(
            out,
            "**The upgrade readiness check didn't finish today.** It couldn't save its report. "
            "An admin needs to fix this before it can run; the details are in the agent's log.\n",
        )

    def test_tomorrow_only_while_the_roster_runs_it_daily(self) -> None:
        self.roster("10 10 * * 1")
        out, _ = self.run_main(RuntimeError("boom"))
        self.assertEqual(out, "**The upgrade readiness check didn't run today.** It hit an error. It will try again at its next scheduled run.\n")

    def test_a_failure_whose_text_changes_every_run_is_posted_once(self) -> None:
        first, _ = self.run_main(RuntimeError("ControlSocket /tmp/.sandbox-ssh/cm-1 already exists"))
        second, _ = self.run_main(RuntimeError("ControlSocket /tmp/.sandbox-ssh/cm-2 already exists"))
        self.assertTrue(first)
        self.assertEqual(second, "")

    def test_each_kind_has_its_reason(self) -> None:
        cases = (
            (watch.ReportRefused("refused", watch.REFUSAL_OUTSIDE), watch.FAILED_REASON_REFUSED),
            (watch.ScopeUnreadable("render"), watch.FAILED_REASON_SCOPE_UNREADABLE),
            (watch.ProjectUnresolved("number"), watch.FAILED_REASON_UNRESOLVED),
            (watch.SandboxTimedOut("slow"), watch.FAILED_REASON_TIMED_OUT),
            (watch.WriteFailed("disk full"), watch.FAILED_REASON_WRITE),
            (OSError("cannot fork"), watch.FAILED_REASON_ERROR),
        )
        for error, reason in cases:
            with self.subTest(error=error):
                self.assertEqual(watch.failure_reason(error), reason)

    def test_a_refusal_that_clears_on_its_own_promises_a_retry(self) -> None:
        self.roster("10 10 * * *")
        for cause in sorted(watch.REFUSAL_CAUSES_THAT_CLEAR):
            with self.subTest(cause=cause):
                (Path(self.tmp.name) / watch.FAILURE_MARKER_FILE_NAME).unlink(missing_ok=True)
                out, _ = self.run_main(watch.ReportRefused("refused", cause))
                self.assertTrue(out.endswith(" It will try again tomorrow.\n"), out)

    def test_a_tick_with_any_refusal_that_needs_a_person_says_so(self) -> None:
        self.roster("10 10 * * *")
        refused = watch.ReportRefused("refused", watch.REFUSAL_SANDBOX)
        refused.causes = [watch.REFUSAL_SANDBOX, watch.REFUSAL_OUTSIDE]
        out, _ = self.run_main(refused)
        self.assertTrue(out.endswith(watch.FAILED_NEEDS_ADMIN + "\n"))


class SaveFailure(unittest.TestCase):
    """A ledger save that keeps failing is announced once, as any standing failure is."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        env = mock.patch.dict(
            os.environ,
            {
                watch.WATCH_HOME_ENV: str(self.home),
                watch.HOME_ENV: self.tmp.name,
                watch.PROJECTS_ENV: "p1",
                fleet_scope_targets.AGENT_HOME_ENV: self.tmp.name,
                fleet_scope_targets.SCOPE_FILE_ENV: "",
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def run_tick(self) -> str:
        out = io.StringIO()
        sandbox = FakeSandbox(envelope([member("a", "lagging")]), envelope([member("a", "lagging", readiness="ready")]))
        with mock.patch.object(watch.sandbox_exec, "run", side_effect=sandbox.run), mock.patch.object(
            watch, "save_ledger", side_effect=OSError("ledger.json.tmp is a directory")
        ), redirect_stdout(out), redirect_stderr(io.StringIO()):
            watch.main([])
        return out.getvalue()

    def test_a_save_that_keeps_failing_posts_once(self) -> None:
        first = self.run_tick()
        second = self.run_tick()
        self.assertIn("didn't finish today", first)
        self.assertEqual(second, "")


if __name__ == "__main__":
    unittest.main()
