#!/usr/bin/env python3
"""Scan every CI pool project's shape for drift, on the health bot's clock.

A pool project is verified once, by hand, at onboarding
(scripts/verify_ci_pool_project.py, docs/ci-pool-projects.md section 7) and
never again, so when the verifier's bundle changes or a project drifts the
first signal is a 403 in an agent transcript on whichever pull request leased
it. roles/serviceusage.serviceUsageConsumer joined the platform GSA's bundle
on 2026-09-08 and was missing on all 30 projects until 2026-09-23 (#1927).
This is the detector #1967 asks for, built the way the seeded-fleet scan is
(fixture_state.py): the check lives in one place, the hourly job runs it, the
document is published beside fixture-state.json, and health.py turns it into
a DEGRADED condition with a tracking issue that carries the repair command.
It asserts; it never grants, revokes or deletes.

Per project, in a temporary directory of its own:

    scripts/verify_ci_pool_project.py --project-id <p> --checks <read-only set> --report <file>

The checks are the verifier's POOL_STATE_CHECKS: the project and its APIs,
IAM, Artifact Registry, the clusters and state bucket, and the KMS half of
the token minter. Every read runs as the bot itself
(eval-dashboard-publisher@kube-agents-prow), which needs the project-level
read roles bench/tf/fleet grants it (`pool_state_readers`); a project without
them scans as "not checked" with gcloud's own words. Not run here: the fleet
fixtures (fixture_state.py already does), the GitHub-reading checks
(github_repo_and_app, gitops_declaration, ledger_read_credential; each needs a
credential the bot must not hold), the mapping (about the checkout, not the
project).

The project list is `gitops_repo_for_project()` in hack/ci-deploy.sh, read
the way fixture_state.py reads it. Nothing here fails the bot's run: a
missing gcloud, a verifier that timed out or could not be started, a project
the bot cannot read are each "not checked" with a reason, exit 0. Only a repository bug (no mapping,
no verifier) exits 1.

Run:  python3 scripts/eval_dashboard/pool_state.py --out pool-state.json [--prior pool-state.json]
Test: cd scripts && python3 -m unittest test_eval_dashboard_pool_state
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import verify_ci_pool_project as verifier  # noqa: E402  (the one implementation of the checks)

try:
    from . import fixture_state
except ImportError:  # run as a script
    import fixture_state  # type: ignore[no-redef]

SCHEMA_VERSION = 1

CI_DEPLOY_SCRIPT = REPO_ROOT / "hack" / "ci-deploy.sh"
VERIFIER_SCRIPT = SCRIPTS_DIR / "verify_ci_pool_project.py"
# The verifier's own vocabulary, imported rather than copied.
DEFAULT_CHECKS = tuple(verifier.POOL_STATE_CHECKS)
DEFAULT_LOCATION = "us-central1"
REPORT_FILE = "pool-state-report.json"
# The verifier shells out to gcloud for every read; without it nothing can be checked.
REQUIRED_BINARIES = ("gcloud",)

# Per-check states in pool-state.json (the same three words as the fleet
# scan's roles), and how the verifier's --report statuses map onto them.
CHECK_HEALTHY = fixture_state.ROLE_HEALTHY
CHECK_DRIFTED = fixture_state.ROLE_DRIFTED
CHECK_NOT_CHECKED = fixture_state.ROLE_NOT_CHECKED
REPORT_STATUS_TO_STATE = {
    verifier.REPORT_STATUS_PASS: CHECK_HEALTHY,
    verifier.REPORT_STATUS_FAIL: CHECK_DRIFTED,
    verifier.REPORT_STATUS_UNCHECKED: CHECK_NOT_CHECKED,
}

# pool-state.json keys. The document-level ones are the fleet scan's, so
# health.py reads both through one rule; per project, `checks` is what was
# read and `findings` what was found.
KEY_SCANNED_AT = fixture_state.KEY_SCANNED_AT
KEY_DURATION = fixture_state.KEY_DURATION
KEY_PROJECTS = fixture_state.KEY_PROJECTS
KEY_STATE = fixture_state.KEY_STATE
KEY_DETAIL = fixture_state.KEY_DETAIL
KEY_SUMMARY = fixture_state.KEY_SUMMARY
KEY_PREVIOUS = fixture_state.KEY_PREVIOUS
KEY_DRIFTED = fixture_state.KEY_DRIFTED
KEY_ERROR = fixture_state.KEY_ERROR
KEY_SCOPE = fixture_state.KEY_SCOPE
SCOPE_POOL = fixture_state.SCOPE_POOL
SCOPE_SELECTED = fixture_state.SCOPE_SELECTED
KEY_CHECKS = "checks"
KEY_FINDINGS = "findings"
KEY_CHECK = "check"
KEY_REPAIR = "repair"
# A healthy or drifted check's refused reads, from the verifier's `unread`:
# advice on a read that happened stays in `detail` and does not stop the
# check counting as read in full; a refused read does, whichever way the
# check's other reads came out.
KEY_UNREAD = "unread"
# The verifier's deadline sits this far inside the scan's per-project
# ceiling: past it the verifier starts no check and cuts every command short,
# so the report is written with every verdict the run reached, with this much
# left for the process to start and the report to be written.
DEADLINE_MARGIN_S = 30

# Reasons written when a whole project could not be checked.
REASON_NO_BINARY = "{binary} is not on PATH, so nothing was checked"
REASON_VERIFIER_FAILED = "scripts/verify_ci_pool_project.py exited {rc} without a report: {error}"
REASON_VERIFIER_TIMEOUT = "scripts/verify_ci_pool_project.py did not finish within {seconds}s"
REASON_NO_REPORT = "scripts/verify_ci_pool_project.py wrote no report for this check"
REASON_CHECK_UNCHECKED = "the verifier could read nothing about this item"
REASON_WORKDIR = "the scan could not prepare a work directory for this project: {error}"
REASON_REPORT_UNREADABLE = "scripts/verify_ci_pool_project.py exited {rc} and its report could not be read: {error}"
REASON_PROJECT_PLACEHOLDER = "<project>"
REASON_VERIFIER_UNRUNNABLE = "scripts/verify_ci_pool_project.py could not be run: {error}"
# What `--projects` admits: the mapping row's shape in hack/ci-deploy.sh, at
# GCP's 30-character cap. An id is also a directory name under the work
# directory, so nothing with a path in it, or too long for one, gets that far.
PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,29}$")
# GNU timeout's code, as fixture_state._run reports a ceiling.
EXIT_TIMED_OUT = 124
# Summary keys the entry point and the workflow's upload step read back.
KEY_CHECKED = "checked"
KEY_DRIFTED_PROJECTS = "drifted_projects"

DEFAULT_WORKERS = 6
# The five checks make about twenty gcloud calls; a healthy project takes
# under a minute. The ceiling is for a hung API.
DEFAULT_PROJECT_TIMEOUT_S = 600

EXIT_OK = 0
EXIT_REPOSITORY_BUG = 1

UTC = timezone.utc

log = fixture_state.log
iso = fixture_state.iso
parse_iso = fixture_state.parse_iso
pool_projects = fixture_state.pool_projects
load_json = fixture_state.load_json
_error_summary = fixture_state._error_summary
_run = fixture_state._run


def missing_binaries(which=shutil.which) -> list[str]:
    return [name for name in REQUIRED_BINARIES if which(name) is None]


# --------------------------------------------------------------------------- #
# One project
# --------------------------------------------------------------------------- #


def _all_checks(checks: tuple[str, ...] | list[str], state: str, detail: list[str]) -> dict[str, dict]:
    return {check: {KEY_STATE: state, KEY_DETAIL: list(detail)} for check in checks}


def _project_entry(checks_out: dict[str, dict], findings: dict[str, dict], started: float, error: str | None = None) -> dict:
    counts = collections.Counter(entry[KEY_STATE] for entry in checks_out.values())
    entry = {
        KEY_CHECKS: checks_out,
        KEY_FINDINGS: findings,
        KEY_SUMMARY: {state: counts.get(state, 0) for state in (CHECK_HEALTHY, CHECK_DRIFTED, CHECK_NOT_CHECKED)},
        KEY_DURATION: int(time.monotonic() - started),
    }
    if error:
        entry[KEY_ERROR] = error
    return entry


def _lines(record: dict, key: str) -> list[str]:
    """A record's list field as strings; a field of any other shape is empty,
    so a malformed report is a thin verdict rather than a dead scan."""
    value = record.get(key)
    return [str(line) for line in value] if isinstance(value, list) else []


def from_report(report: dict, checks: tuple[str, ...] | list[str]) -> tuple[dict[str, dict], dict[str, dict]]:
    """(checks, findings) for one project from the verifier's --report document."""
    recorded = report.get(KEY_CHECKS) if isinstance(report, dict) else None
    recorded = recorded if isinstance(recorded, dict) else {}
    checks_out: dict[str, dict] = {}
    findings: dict[str, dict] = {}
    for check in checks:
        record = recorded.get(check) if isinstance(recorded.get(check), dict) else None
        if record is None:
            checks_out[check] = {KEY_STATE: CHECK_NOT_CHECKED, KEY_DETAIL: [REASON_NO_REPORT]}
            continue
        state = REPORT_STATUS_TO_STATE.get(str(record.get(verifier.REPORT_KEY_STATUS)), CHECK_NOT_CHECKED)
        if state == CHECK_DRIFTED:
            detail = _lines(record, verifier.REPORT_KEY_DETAILS) or [str(record.get(verifier.REPORT_KEY_MESSAGE) or "")]
        elif state == CHECK_NOT_CHECKED:
            detail = _lines(record, verifier.REPORT_KEY_WARNINGS) or [REASON_CHECK_UNCHECKED]
        else:
            detail = _lines(record, verifier.REPORT_KEY_WARNINGS)
        checks_out[check] = {KEY_STATE: state, KEY_DETAIL: detail}
        if state in (CHECK_HEALTHY, CHECK_DRIFTED):
            checks_out[check][KEY_UNREAD] = _lines(record, verifier.REPORT_KEY_UNREAD)
        if state == CHECK_DRIFTED:
            listed = record.get(verifier.REPORT_KEY_FINDINGS)
            for finding in listed if isinstance(listed, list) else []:
                if not isinstance(finding, dict) or not finding.get(verifier.FINDING_KEY_ID):
                    continue
                findings[str(finding[verifier.FINDING_KEY_ID])] = {
                    KEY_CHECK: check,
                    KEY_DETAIL: [str(finding.get(verifier.FINDING_KEY_OBSERVED) or "")],
                    KEY_REPAIR: str(finding.get(verifier.FINDING_KEY_REPAIR) or ""),
                }
    return checks_out, findings


def verifier_deadline(timeout: float) -> int:
    """Seconds into the run after which the verifier starts no more checks."""
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError(f"project timeout must be a finite, non-negative number of seconds, not {timeout!r}")
    return max(1, int(timeout) - DEADLINE_MARGIN_S)


def scan_project(
    project: str,
    checks: tuple[str, ...] | list[str],
    workdir: pathlib.Path,
    timeout: float,
    *,
    location: str = DEFAULT_LOCATION,
    verifier_script: pathlib.Path = VERIFIER_SCRIPT,
    environ=os.environ,
    runner=subprocess.run,
) -> dict:
    """One project's entry for pool-state.json.

    Every failure is a "not checked" verdict with its reason, never an
    exception: the scan over the other projects goes on. The verifier's exit
    code is not the verdict -- it exits 1 on a finding and 2 on an unread
    item, both of which the report records per check -- so only a run that
    wrote no report is a failure of the scan itself.
    """
    started = time.monotonic()
    project_dir = workdir / project
    report = project_dir / REPORT_FILE
    try:
        project_dir.mkdir(parents=True, exist_ok=True)
        # A --workdir reused across runs keeps the last report; a verifier that
        # dies before writing must read as unread, not as last run's verdict.
        report.unlink(missing_ok=True)
    except OSError as exc:
        reason = REASON_WORKDIR.format(error=exc)
        return _project_entry(_all_checks(checks, CHECK_NOT_CHECKED, [reason]), {}, started, reason)
    try:
        rc, _, err = _run(
            [
                sys.executable, str(verifier_script),
                "--project-id", project,
                "--checks", ",".join(checks),
                "--location", location,
                "--report", str(report),
                "--deadline-seconds", str(verifier_deadline(timeout)),
            ],
            dict(environ),
            timeout,
            runner,
        )
    except Exception as exc:  # noqa: BLE001 -- the contract above: a reason, never a dead scan
        reason = REASON_VERIFIER_UNRUNNABLE.format(error=f"{type(exc).__name__}: {exc}")
        return _project_entry(_all_checks(checks, CHECK_NOT_CHECKED, [reason]), {}, started, reason)
    if rc == EXIT_TIMED_OUT:
        reason = REASON_VERIFIER_TIMEOUT.format(seconds=int(timeout))
        return _project_entry(_all_checks(checks, CHECK_NOT_CHECKED, [reason]), {}, started, reason)
    if not report.is_file():
        # Any code: an uncaught exception exits 1, and stderr says why.
        reason = REASON_VERIFIER_FAILED.format(rc=rc, error=_error_summary(err))
        return _project_entry(_all_checks(checks, CHECK_NOT_CHECKED, [reason]), {}, started, reason)
    try:
        document = json.loads(report.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError(f"the report is a JSON {type(document).__name__}, not an object")
    except (OSError, ValueError) as exc:
        # A report that is there but cannot be read -- a write cut short, a
        # full disk -- is its own reason; "without a report" would name a
        # crash that did not happen.
        reason = REASON_REPORT_UNREADABLE.format(rc=rc, error=exc)
        return _project_entry(_all_checks(checks, CHECK_NOT_CHECKED, [reason]), {}, started, reason)
    checks_out, findings = from_report(document, checks)
    return _project_entry(checks_out, findings, started)


# --------------------------------------------------------------------------- #
# The document
# --------------------------------------------------------------------------- #


def _entries(document: dict | None) -> list[tuple[str, dict]]:
    projects = (document or {}).get(KEY_PROJECTS) if isinstance(document, dict) else None
    return sorted((project, entry) for project, entry in (projects or {}).items() if isinstance(entry, dict)) if isinstance(projects, dict) else []


def drift_map(document: dict | None) -> dict[str, list[str]]:
    """{project: [finding ids]} for the projects with any, sorted."""
    out: dict[str, list[str]] = {}
    for project, entry in _entries(document):
        findings = entry.get(KEY_FINDINGS)
        drifted = sorted(str(finding) for finding in findings) if isinstance(findings, dict) and findings else []
        if drifted:
            out[project] = drifted
    return out


def _read_checks(entry: dict, whole: bool) -> list[str]:
    """The checks the scan read on one project. `whole`: read in full only.
    A check is a bundle of reads and lists the refused ones under `unread`,
    whether it passed or failed on the rest; a finding in a refused read was
    not seen. Advice on a read that happened is `detail` and does not count
    against it."""
    checks = entry.get(KEY_CHECKS)
    if not isinstance(checks, dict):
        return []
    out = []
    for check, verdict in checks.items():
        if not isinstance(verdict, dict):
            continue
        state = verdict.get(KEY_STATE)
        if state in (CHECK_DRIFTED, CHECK_HEALTHY) and (not whole or not verdict.get(KEY_UNREAD)):
            out.append(check)
    return sorted(out)


def unread_units(document: dict | None) -> int:
    """How many checks were not read in full on projects the scan did check:
    not checked at all, or read in part with the rest refused (healthy or
    drifted with `unread`). The digest's "every one shaped as the verifier
    requires" is only as true as this is zero."""
    count = 0
    for project, entry in _entries(document):
        checks = entry.get(KEY_CHECKS) if isinstance(entry, dict) else None
        if not isinstance(checks, dict) or not _read_checks(entry, whole=False):
            continue
        for verdict in checks.values():
            if not isinstance(verdict, dict):
                continue
            if verdict.get(KEY_STATE) == CHECK_NOT_CHECKED or verdict.get(KEY_UNREAD):
                count += 1
    return count


def failing_map(document: dict | None) -> dict[str, list[str]]:
    """{project: [check ids in the drifted state]}, sorted: what a
    `<check>/failed` unit stands for, whatever the check's findings are named."""
    out: dict[str, list[str]] = {}
    for project, entry in _entries(document):
        checks = entry.get(KEY_CHECKS)
        if not isinstance(checks, dict):
            continue
        failing = sorted(check for check, verdict in checks.items() if isinstance(verdict, dict) and verdict.get(KEY_STATE) == CHECK_DRIFTED)
        if failing:
            out[project] = failing
    return out


def read_map(document: dict | None) -> dict[str, list[str]]:
    """{project: [check ids read in full]}, sorted: what rule 6's exit may
    take as proof a finding is gone."""
    out: dict[str, list[str]] = {}
    for project, entry in _entries(document):
        read = _read_checks(entry, whole=True)
        if read:
            out[project] = read
    return out


previous_drift_map = fixture_state.previous_drift_map


def finding(document: dict | None, project: str, finding_id: str) -> dict:
    entry = ((document or {}).get(KEY_PROJECTS) or {}).get(project) or {}
    record = (entry.get(KEY_FINDINGS) or {}).get(finding_id) or {}
    return record if isinstance(record, dict) else {}


def drift_detail(document: dict | None, project: str, finding_id: str) -> list[str]:
    return [str(line) for line in finding(document, project, finding_id).get(KEY_DETAIL) or []]


def repair_for(document: dict | None, project: str, finding_id: str) -> str:
    return str(finding(document, project, finding_id).get(KEY_REPAIR) or "")


def check_of(document: dict | None, project: str, finding_id: str) -> str:
    """The check a finding belongs to, which is what the scan reads per project."""
    return str(finding(document, project, finding_id).get(KEY_CHECK) or "")


def checked_projects(document: dict | None) -> int:
    """Projects where at least one check read anything; a partial read is
    not a blind scan."""
    return sum(1 for _, entry in _entries(document) if _read_checks(entry, whole=False))


def _project_reason(entry: dict) -> str | None:
    """One project's reason for being unchecked: the whole-project error, else
    the project check's own line (the checks that need its number restate
    it), else its commonest not-checked line."""
    if entry.get(KEY_ERROR):
        return str(entry[KEY_ERROR])
    lines: collections.Counter = collections.Counter()
    for check, verdict in (entry.get(KEY_CHECKS) or {}).items():
        if isinstance(verdict, dict) and verdict.get(KEY_STATE) == CHECK_NOT_CHECKED and verdict.get(KEY_DETAIL):
            if check == verifier.CHECK_PROJECT_AND_APIS:
                return str(verdict[KEY_DETAIL][0])
            lines[str(verdict[KEY_DETAIL][0])] += 1
    return lines.most_common(1)[0][0] if lines else None


def not_checked_reason(document: dict | None) -> str | None:
    """The reason a scan saw nothing: one reason per project, then the one
    most projects share, so one project's refusal does not name a stall on
    the other thirty-four. A reason that names its project (the verifier's
    "Could not describe <project>...") is the same reason on every project,
    so the vote is on the wording with the project's id taken out, and the
    first project's own line is what comes back."""
    votes: collections.Counter = collections.Counter()
    first: dict[str, str] = {}
    for project, entry in _entries(document):
        reason = _project_reason(entry)
        if reason:
            wording = reason.replace(project, REASON_PROJECT_PLACEHOLDER)
            votes[wording] += 1
            first.setdefault(wording, reason)
    return first[votes.most_common(1)[0][0]] if votes else None


def summarize(projects: dict[str, dict]) -> dict:
    counts = collections.Counter()
    for entry in projects.values():
        for verdict in entry[KEY_CHECKS].values():
            counts[verdict[KEY_STATE]] += 1
    return {
        KEY_PROJECTS: len(projects),
        KEY_CHECKED: checked_projects({KEY_PROJECTS: projects}),
        KEY_DRIFTED_PROJECTS: len(drift_map({KEY_PROJECTS: projects})),
        KEY_FINDINGS: sum(len(entry.get(KEY_FINDINGS) or {}) for entry in projects.values()),
        CHECK_HEALTHY: counts.get(CHECK_HEALTHY, 0),
        CHECK_DRIFTED: counts.get(CHECK_DRIFTED, 0),
        CHECK_NOT_CHECKED: counts.get(CHECK_NOT_CHECKED, 0),
    }


def scan(
    projects: list[str],
    checks: tuple[str, ...] | list[str],
    workdir: pathlib.Path,
    *,
    prior: dict | None = None,
    now: datetime | None = None,
    workers: int = DEFAULT_WORKERS,
    project_timeout: float = DEFAULT_PROJECT_TIMEOUT_S,
    which=shutil.which,
    scope: str = SCOPE_POOL,
    **project_kwargs,
) -> dict:
    """pool-state.json as a dict. `prior` is the previously published
    document, whose drift map rides along as `previous` so the adjudicator
    can ask "the same finding last scan too?" from one file."""
    started = time.monotonic()
    now = now or datetime.now(UTC)
    missing = missing_binaries(which)
    if missing:
        reason = REASON_NO_BINARY.format(binary=" and ".join(missing))
        log(f"warning: {reason}")
        entries = {project: _project_entry(_all_checks(checks, CHECK_NOT_CHECKED, [reason]), {}, started, reason) for project in projects}
    else:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(projects) or 1))) as pool:
            futures = {project: pool.submit(scan_project, project, checks, workdir, project_timeout, **project_kwargs) for project in projects}
            entries = {project: future.result() for project, future in futures.items()}
    for project, entry in entries.items():
        if entry.get(KEY_ERROR):
            log(f"{project}: not checked: {entry[KEY_ERROR]}")
        elif entry[KEY_FINDINGS]:
            log(f"{project}: drifted: {', '.join(sorted(entry[KEY_FINDINGS]))}")
    return {
        "schema_version": SCHEMA_VERSION,
        KEY_SCANNED_AT: iso(now),
        KEY_SCOPE: scope,
        KEY_DURATION: int(time.monotonic() - started),
        KEY_CHECKS: list(checks),
        KEY_PROJECTS: entries,
        KEY_SUMMARY: summarize(entries),
        KEY_PREVIOUS: {KEY_SCANNED_AT: (prior or {}).get(KEY_SCANNED_AT) if isinstance(prior, dict) else None, KEY_DRIFTED: drift_map(prior)},
    }


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _finite_seconds(text: str) -> float:
    """argparse type: a finite, non-negative number of seconds, refused at
    the door rather than inside a worker thread."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of seconds")
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError(f"{text!r} is not a finite, non-negative number of seconds")
    return value


def _project_list(text: str) -> list[str]:
    """argparse type: comma-separated project ids of the mapping's shape."""
    # Deduplicated: a repeated id would run two verifiers on one report file.
    projects = list(dict.fromkeys(p.strip() for p in text.split(",") if p.strip()))
    bad = [p for p in projects if not PROJECT_ID_RE.match(p)]
    if bad:
        raise argparse.ArgumentTypeError(f"not a project id: {', '.join(repr(p) for p in bad)}")
    if not projects:
        # An empty value (an unset shell variable) must not widen to the pool.
        raise argparse.ArgumentTypeError(f"{text!r} names no project")
    return projects


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=pathlib.Path, required=True, help="where to write pool-state.json")
    parser.add_argument("--prior", type=pathlib.Path, help="the previously published pool-state.json (missing is fine)")
    parser.add_argument("--projects", type=_project_list, help="comma-separated project ids (default: gitops_repo_for_project() in hack/ci-deploy.sh)")
    parser.add_argument("--checks", default=",".join(DEFAULT_CHECKS), help=f"comma-separated verifier check ids (default {','.join(DEFAULT_CHECKS)})")
    parser.add_argument("--location", default=DEFAULT_LOCATION, help=f"the verifier's --location (default {DEFAULT_LOCATION})")
    parser.add_argument("--ci-deploy-script", type=pathlib.Path, default=CI_DEPLOY_SCRIPT, help=argparse.SUPPRESS)
    parser.add_argument("--verifier", type=pathlib.Path, default=VERIFIER_SCRIPT, help=argparse.SUPPRESS)
    parser.add_argument("--workdir", type=pathlib.Path, help="where the per-project reports go (default: a temporary directory, removed afterwards)")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help=f"projects scanned at once (default {DEFAULT_WORKERS})")
    parser.add_argument("--project-timeout", type=_finite_seconds, default=DEFAULT_PROJECT_TIMEOUT_S, help=f"seconds per project for the verifier (default {DEFAULT_PROJECT_TIMEOUT_S})")
    parser.add_argument("--now", help="the scan time to record, ISO 8601 (default: now)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        projects = args.projects or pool_projects(args.ci_deploy_script.read_text(encoding="utf-8"))
        checks = verifier.parse_checks(args.checks) or list(DEFAULT_CHECKS)
    except (OSError, ValueError) as exc:
        log(f"ERROR: {exc}")
        return EXIT_REPOSITORY_BUG
    if not projects:
        log("ERROR: no projects to scan")
        return EXIT_REPOSITORY_BUG
    if not args.verifier.is_file():
        log(f"ERROR: {args.verifier} is not there")
        return EXIT_REPOSITORY_BUG
    workdir = args.workdir
    cleanup = workdir is None
    if workdir is None:
        workdir = pathlib.Path(tempfile.mkdtemp(prefix="pool-state-"))
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        document = scan(
            projects,
            checks,
            workdir,
            prior=load_json(args.prior),
            now=parse_iso(args.now),
            workers=args.workers,
            project_timeout=args.project_timeout,
            scope=SCOPE_SELECTED if args.projects else SCOPE_POOL,
            location=args.location,
            verifier_script=args.verifier,
        )
    finally:
        if cleanup:
            shutil.rmtree(workdir, ignore_errors=True)
    args.out.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    summary = document[KEY_SUMMARY]
    log(
        f"pool-state: {summary[KEY_CHECKED]} of {summary[KEY_PROJECTS]} pool projects checked, "
        f"{summary[KEY_DRIFTED_PROJECTS]} with drift ({summary[KEY_FINDINGS]} findings; checks: {summary[CHECK_HEALTHY]} healthy, "
        f"{summary[CHECK_DRIFTED]} drifted, {summary[CHECK_NOT_CHECKED]} not checked) in {document[KEY_DURATION]}s; wrote {args.out}"
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
