#!/usr/bin/env python3
"""Scan every CI pool project's seeded fleet for fixture drift, on the health bot's clock.

Presence probes passed on 2026-09-07 while `payments-api` sat Pending on all
30 pool projects (#1278). hack/fleet-fixture-state.py (#1544) is the check
that would have failed, and this is where it runs on a schedule rather than
per lease: once an hour the CI health workflow (.github/workflows/ci-health.yml,
job `fixture-state-scan`) runs it against every pool project and publishes
`fixture-state.json` beside health.json. health.py reads that file for its
`fixture_drift` condition and post_health.py puts one line of it in the 9 AM
digest (docs/ci-health.md, "The seeded-fleet scan").

Per project, in a temporary directory of its own:

    hack/fleet-kubeconfigs.sh       discover the trio, publish one kubeconfig per role
    hack/fleet-fixture-state.py     assert each published role's designed state,
                                    --wait 0, --report so the verdicts arrive as JSON

Every read runs as that project's seeded-fleet reader
(`seeded-fleet-reader@<project>`, bench/tf/fleet). CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT
makes gcloud impersonate it for the cluster listing, the credentials and the
control-plane describes; FLEET_READONLY_SA makes the runner rewrite each
kubeconfig so kubectl's token is minted as it too. The bot therefore needs
exactly one grant per pool project -- roles/iam.serviceAccountTokenCreator on
that account, the grant #1238 gave the presubmit's identity -- and nothing on
the project itself. The impersonation is pre-flighted with one token mint, so
a project missing the grant is "not checked" with gcloud's own words rather
than a runner that could not list clusters.

The project list is `gitops_repo_for_project()` in hack/ci-deploy.sh, the one
list of pool projects this repository holds. The leasable roster is Boskos's
(gke-internal/test-infra), and every leasable project is mapped here first:
scripts/verify_ci_pool_project.py refuses to pass one that is not. A mapped
project that is not provisioned, not visible to the bot, or missing the grant
scans as "not checked", which is the point -- the scan says what it could not
see instead of guessing.

Nothing here fails the bot's run. A missing kubectl or gcloud, an unreachable
project, a runner that timed out, a project without the grant: each is
"not checked" with its reason, per role, and the exit is 0. Only a repository
bug (no mapping in ci-deploy.sh, no catalog) exits 1.

Run:  python3 scripts/eval_dashboard/fixture_state.py --out fixture-state.json [--prior fixture-state.json]
Test: cd scripts && python3 -m unittest test_eval_dashboard_fixture_state
"""

from __future__ import annotations

import argparse
import collections
import json
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

SCHEMA_VERSION = 1

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
CI_DEPLOY_SCRIPT = REPO_ROOT / "hack" / "ci-deploy.sh"
FLEET_KUBECONFIGS = REPO_ROOT / "hack" / "fleet-kubeconfigs.sh"
FLEET_FIXTURE_STATE = REPO_ROOT / "hack" / "fleet-fixture-state.py"
FLEET_CATALOG = REPO_ROOT / "bench" / "tf" / "fleet" / "fixtures.json"

# The pool-project list: the case arms of gitops_repo_for_project() in
# hack/ci-deploy.sh, read the way scripts/verify_ci_pool_project.py reads
# them (a row must start its own line, so a commented-out arm is not one).
MAPPING_RE = re.compile(r"gitops_repo_for_project\(\)\s*\{(.*?)\n\}", re.DOTALL)
MAPPING_ROW_RE = re.compile(r"^[ \t]*([a-z][a-z0-9-]*)\)\s*echo\s", re.MULTILINE)

# The identity every read runs as, per project (bench/tf/fleet/main.tf), and
# the gcloud property that makes every gcloud call impersonate it.
READER_SA_TEMPLATE = "seeded-fleet-reader@{project}.iam.gserviceaccount.com"
IMPERSONATE_ENV = "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT"
# The runner's inputs (hack/fleet-kubeconfigs.sh header).
RUNNER_PROJECT_ENV = "FLEET_PROJECT_ID"
RUNNER_DIR_ENV = "BENCH_FLEET_KUBECONFIG_DIR"
RUNNER_READER_ENV = "FLEET_READONLY_SA"
RUNNER_CATALOG_ENV = "FLEET_CATALOG"
# Both scripts shell out to these; without either nothing can be checked.
REQUIRED_BINARIES = ("gcloud", "kubectl")

# Per-role states in fixture-state.json, and how the state script's report
# verdicts (hack/fleet-fixture-state.py --report) map onto them.
ROLE_HEALTHY = "healthy"
ROLE_DRIFTED = "drifted"
ROLE_NOT_CHECKED = "not_checked"
REPORT_VERDICT_TO_STATE = {
    "converged": ROLE_HEALTHY,
    "drifted": ROLE_DRIFTED,
    "unchecked": ROLE_NOT_CHECKED,
    "unpublished": ROLE_NOT_CHECKED,
    "no_state": ROLE_NOT_CHECKED,
}
REPORT_VERDICT_UNPUBLISHED = "unpublished"
REPORT_VERDICT_NO_STATE = "no_state"
REPORT_FILE = "fixture-state-report.json"
FLEET_SUBDIR = "fleet"

# fixture-state.json keys (docs/ci-health.md states the document).
KEY_SCANNED_AT = "scanned_at"
KEY_DURATION = "duration_s"
KEY_PROJECTS = "projects"
KEY_ROLES = "roles"
KEY_STATE = "state"
KEY_DETAIL = "detail"
KEY_SUMMARY = "summary"
KEY_PREVIOUS = "previous"
KEY_DRIFTED = "drifted"
KEY_READER = "reader"
KEY_ERROR = "error"

# Reasons written when a whole project could not be checked.
REASON_NO_BINARY = "{binary} is not on PATH, so nothing was checked"
REASON_CANNOT_IMPERSONATE = "cannot mint a token as {reader} (is roles/iam.serviceAccountTokenCreator granted to the bot?): {error}"
REASON_RUNNER_FAILED = "hack/fleet-kubeconfigs.sh failed ({rc}): {error}"
REASON_RUNNER_TIMEOUT = "hack/fleet-kubeconfigs.sh did not finish within {seconds}s"
REASON_STATE_FAILED = "hack/fleet-fixture-state.py failed ({rc}): {error}"
REASON_STATE_TIMEOUT = "hack/fleet-fixture-state.py did not finish within {seconds}s"
REASON_NO_REPORT = "hack/fleet-fixture-state.py wrote no report"
REASON_UNPUBLISHED = "hack/fleet-kubeconfigs.sh published no kubeconfig for this role"
REASON_NO_STATE = "the catalog declares no designed-state assertions for this role"
NO_OUTPUT = "no output"
# gcloud's failures arrive as one `ERROR:` line followed by a YAML dump of
# the RPC status; the line is the reason and the dump is noise. Capped so a
# reason fits on one line of a Chat message or an issue.
ERROR_LINE_PREFIX = "ERROR"
REASON_MAX_CHARS = 300

# The runner's warnings that explain an unpublished role, most specific
# first: a role named, its slot named, then the project-wide ones.
RUNNER_WARNING_PREFIX = "WARNING: "
RUNNER_MINT_WARNING = "could not mint a read-only token"
RUNNER_LIST_WARNING = "could not list clusters"
RUNNER_ROLE_WARNING = "fixture role '{role}'"
RUNNER_SLOT_WARNING = "slot '{slot}'"

DEFAULT_WORKERS = 6
DEFAULT_PROJECT_TIMEOUT_S = 600
# The pre-flight token mint is one IAM call; longer than this is the API
# being down, which the runner would then also fail on.
IMPERSONATE_TIMEOUT_S = 60

EXIT_OK = 0
EXIT_REPOSITORY_BUG = 1

UTC = timezone.utc


def log(message: str) -> None:
    print(message, file=sys.stderr)


def iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat(timespec="seconds") if value else None


def parse_iso(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #


def pool_projects(ci_deploy_text: str) -> list[str]:
    """The projects gitops_repo_for_project() maps, in file order.

    Raises ValueError when the function is not there: a checkout without the
    mapping cannot name the pool, and guessing at project names would scan
    the wrong fleet.
    """
    match = MAPPING_RE.search(ci_deploy_text)
    if not match:
        raise ValueError("no gitops_repo_for_project() in hack/ci-deploy.sh")
    seen: list[str] = []
    for row in MAPPING_ROW_RE.finditer(match.group(1)):
        if row.group(1) not in seen:
            seen.append(row.group(1))
    if not seen:
        raise ValueError("gitops_repo_for_project() maps no project")
    return seen


def catalog_roles(catalog: pathlib.Path) -> dict[str, str]:
    """{role: cluster_slot} from fixtures.json; ValueError when unreadable."""
    try:
        document = json.loads(catalog.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"could not read {catalog}: {exc}") from exc
    roles = document.get("roles") if isinstance(document, dict) else None
    if not isinstance(roles, dict) or not roles:
        raise ValueError(f"{catalog} declares no fixture roles")
    return {role: str((spec or {}).get("cluster_slot") or "") for role, spec in sorted(roles.items())}


def missing_binaries(which=shutil.which) -> list[str]:
    return [name for name in REQUIRED_BINARIES if which(name) is None]


# --------------------------------------------------------------------------- #
# One project
# --------------------------------------------------------------------------- #


def _error_summary(text: str) -> str:
    """The first `ERROR` line of a command's stderr, else its last non-empty
    line, capped at REASON_MAX_CHARS."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return NO_OUTPUT
    chosen = next((line for line in lines if line.startswith(ERROR_LINE_PREFIX)), lines[-1])
    return chosen if len(chosen) <= REASON_MAX_CHARS else chosen[: REASON_MAX_CHARS - 1] + "…"


def _run(cmd: list[str], env: dict, timeout: float, runner=subprocess.run) -> tuple[int, str, str]:
    """(rc, stdout, stderr); rc 124 on a timeout, 127 when the binary is missing."""
    try:
        proc = runner(cmd, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired as exc:
        err = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return 124, "", err
    return proc.returncode, proc.stdout, proc.stderr


def runner_warnings(stderr: str) -> list[str]:
    return [line[len(RUNNER_WARNING_PREFIX):].strip() for line in (stderr or "").splitlines() if line.startswith(RUNNER_WARNING_PREFIX)]


def unpublished_reason(role: str, slot: str, warnings: list[str]) -> str:
    """Why the runner wrote no kubeconfig for `role`, from its own warnings."""
    for needle in (RUNNER_LIST_WARNING, RUNNER_MINT_WARNING, RUNNER_ROLE_WARNING.format(role=role), RUNNER_SLOT_WARNING.format(slot=slot)):
        for warning in warnings:
            if needle in warning:
                return warning
    return warnings[0] if warnings else REASON_UNPUBLISHED


def _all_roles(roles: dict[str, str], state: str, detail: list[str]) -> dict[str, dict]:
    return {role: {KEY_STATE: state, KEY_DETAIL: list(detail)} for role in roles}


def _project_entry(roles_out: dict[str, dict], reader: str | None, started: float, error: str | None = None) -> dict:
    counts = collections.Counter(entry[KEY_STATE] for entry in roles_out.values())
    entry = {
        KEY_ROLES: roles_out,
        KEY_SUMMARY: {state: counts.get(state, 0) for state in (ROLE_HEALTHY, ROLE_DRIFTED, ROLE_NOT_CHECKED)},
        KEY_DURATION: int(time.monotonic() - started),
        KEY_READER: reader,
    }
    if error:
        entry[KEY_ERROR] = error
    return entry


def scan_project(
    project: str,
    roles: dict[str, str],
    workdir: pathlib.Path,
    timeout: float,
    *,
    impersonate: bool = True,
    catalog: pathlib.Path = FLEET_CATALOG,
    runner_script: pathlib.Path = FLEET_KUBECONFIGS,
    state_script: pathlib.Path = FLEET_FIXTURE_STATE,
    environ=os.environ,
    runner=subprocess.run,
) -> dict:
    """One project's entry for fixture-state.json.

    Every failure is a "not checked" verdict with its reason, never an
    exception: the scan over the other projects goes on.
    """
    started = time.monotonic()
    reader = READER_SA_TEMPLATE.format(project=project) if impersonate else None
    fleet_dir = workdir / project / FLEET_SUBDIR
    fleet_dir.parent.mkdir(parents=True, exist_ok=True)
    env = dict(environ)
    env.update({RUNNER_PROJECT_ENV: project, RUNNER_DIR_ENV: str(fleet_dir), RUNNER_CATALOG_ENV: str(catalog)})
    if reader:
        env[IMPERSONATE_ENV] = reader
        env[RUNNER_READER_ENV] = reader
        rc, _, err = _run(["gcloud", "auth", "print-access-token", f"--impersonate-service-account={reader}"], env, IMPERSONATE_TIMEOUT_S, runner)
        if rc != 0:
            reason = REASON_CANNOT_IMPERSONATE.format(reader=reader, error=_error_summary(err))
            return _project_entry(_all_roles(roles, ROLE_NOT_CHECKED, [reason]), reader, started, reason)

    deadline = time.monotonic() + timeout
    rc, _, runner_err = _run(["bash", str(runner_script)], env, timeout, runner)
    if rc == 124:
        reason = REASON_RUNNER_TIMEOUT.format(seconds=int(timeout))
        return _project_entry(_all_roles(roles, ROLE_NOT_CHECKED, [reason]), reader, started, reason)
    if rc != 0:
        reason = REASON_RUNNER_FAILED.format(rc=rc, error=_error_summary(runner_err))
        return _project_entry(_all_roles(roles, ROLE_NOT_CHECKED, [reason]), reader, started, reason)
    warnings = runner_warnings(runner_err)

    report = workdir / project / REPORT_FILE
    remaining = max(deadline - time.monotonic(), 1.0)
    rc, _, state_err = _run(
        ["python3", str(state_script), "--dir", str(fleet_dir), "--catalog", str(catalog), "--project", project, "--wait", "0", "--report", str(report)],
        env,
        remaining,
        runner,
    )
    if rc == 124:
        reason = REASON_STATE_TIMEOUT.format(seconds=int(remaining))
        return _project_entry(_all_roles(roles, ROLE_NOT_CHECKED, [reason]), reader, started, reason)
    if rc != 0:
        reason = REASON_STATE_FAILED.format(rc=rc, error=_error_summary(state_err))
        return _project_entry(_all_roles(roles, ROLE_NOT_CHECKED, [reason]), reader, started, reason)
    try:
        verdicts = json.loads(report.read_text(encoding="utf-8")).get(KEY_ROLES) or {}
    except (OSError, ValueError, AttributeError):
        reason = REASON_NO_REPORT
        return _project_entry(_all_roles(roles, ROLE_NOT_CHECKED, [reason]), reader, started, reason)

    roles_out = {}
    for role, slot in roles.items():
        verdict = verdicts.get(role) if isinstance(verdicts.get(role), dict) else {}
        kind = str(verdict.get(KEY_STATE) or REPORT_VERDICT_UNPUBLISHED)
        detail = [str(line) for line in verdict.get(KEY_DETAIL) or []]
        if kind == REPORT_VERDICT_UNPUBLISHED:
            detail = [unpublished_reason(role, slot, warnings)]
        elif kind == REPORT_VERDICT_NO_STATE:
            detail = [REASON_NO_STATE]
        roles_out[role] = {KEY_STATE: REPORT_VERDICT_TO_STATE.get(kind, ROLE_NOT_CHECKED), KEY_DETAIL: detail}
    return _project_entry(roles_out, reader, started)


# --------------------------------------------------------------------------- #
# The document
# --------------------------------------------------------------------------- #


def drift_map(document: dict | None) -> dict[str, list[str]]:
    """{project: [drifted roles]} for the projects with any, sorted."""
    out: dict[str, list[str]] = {}
    projects = (document or {}).get(KEY_PROJECTS) if isinstance(document, dict) else None
    for project, entry in sorted((projects or {}).items()) if isinstance(projects, dict) else []:
        roles = (entry or {}).get(KEY_ROLES) if isinstance(entry, dict) else None
        drifted = sorted(role for role, verdict in (roles or {}).items() if isinstance(verdict, dict) and verdict.get(KEY_STATE) == ROLE_DRIFTED)
        if drifted:
            out[project] = drifted
    return out


def previous_drift_map(document: dict | None) -> dict[str, list[str]]:
    """The drift map the scan before this one carried (`previous.drifted`)."""
    previous = (document or {}).get(KEY_PREVIOUS) if isinstance(document, dict) else None
    drifted = (previous or {}).get(KEY_DRIFTED) if isinstance(previous, dict) else None
    if not isinstance(drifted, dict):
        return {}
    return {str(project): sorted(str(role) for role in roles) for project, roles in drifted.items() if isinstance(roles, list)}


def drift_detail(document: dict | None, project: str, role: str) -> list[str]:
    entry = ((document or {}).get(KEY_PROJECTS) or {}).get(project) or {}
    verdict = (entry.get(KEY_ROLES) or {}).get(role) or {}
    return [str(line) for line in verdict.get(KEY_DETAIL) or []]


def checked_projects(document: dict | None) -> int:
    """Projects on which at least one role was read (healthy or drifted)."""
    projects = (document or {}).get(KEY_PROJECTS) if isinstance(document, dict) else None
    count = 0
    for entry in (projects or {}).values() if isinstance(projects, dict) else []:
        roles = (entry or {}).get(KEY_ROLES) if isinstance(entry, dict) else None
        if any(isinstance(v, dict) and v.get(KEY_STATE) in (ROLE_HEALTHY, ROLE_DRIFTED) for v in (roles or {}).values()):
            count += 1
    return count


def not_checked_reason(document: dict | None) -> str | None:
    """The commonest reason a role went unchecked, for a scan that saw nothing."""
    reasons: collections.Counter = collections.Counter()
    projects = (document or {}).get(KEY_PROJECTS) if isinstance(document, dict) else None
    for entry in (projects or {}).values() if isinstance(projects, dict) else []:
        if isinstance(entry, dict) and entry.get(KEY_ERROR):
            reasons[str(entry[KEY_ERROR])] += 1
            continue
        roles = (entry or {}).get(KEY_ROLES) if isinstance(entry, dict) else None
        for verdict in (roles or {}).values():
            if isinstance(verdict, dict) and verdict.get(KEY_STATE) == ROLE_NOT_CHECKED and verdict.get(KEY_DETAIL):
                reasons[str(verdict[KEY_DETAIL][0])] += 1
    return reasons.most_common(1)[0][0] if reasons else None


def summarize(projects: dict[str, dict]) -> dict:
    counts = collections.Counter()
    for entry in projects.values():
        for verdict in entry[KEY_ROLES].values():
            counts[verdict[KEY_STATE]] += 1
    return {
        KEY_PROJECTS: len(projects),
        "checked": checked_projects({KEY_PROJECTS: projects}),
        "drifted_projects": len(drift_map({KEY_PROJECTS: projects})),
        ROLE_HEALTHY: counts.get(ROLE_HEALTHY, 0),
        ROLE_DRIFTED: counts.get(ROLE_DRIFTED, 0),
        ROLE_NOT_CHECKED: counts.get(ROLE_NOT_CHECKED, 0),
    }


def scan(
    projects: list[str],
    roles: dict[str, str],
    workdir: pathlib.Path,
    *,
    prior: dict | None = None,
    now: datetime | None = None,
    workers: int = DEFAULT_WORKERS,
    project_timeout: float = DEFAULT_PROJECT_TIMEOUT_S,
    impersonate: bool = True,
    which=shutil.which,
    **project_kwargs,
) -> dict:
    """fixture-state.json as a dict. `prior` is the previously published
    document, whose drift map rides along as `previous` so the adjudicator
    can ask "drifted last scan too?" from one file."""
    started = time.monotonic()
    now = now or datetime.now(UTC)
    missing = missing_binaries(which)
    if missing:
        reason = REASON_NO_BINARY.format(binary=" and ".join(missing))
        log(f"warning: {reason}")
        entries = {project: _project_entry(_all_roles(roles, ROLE_NOT_CHECKED, [reason]), None, started, reason) for project in projects}
    else:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(projects) or 1))) as pool:
            futures = {project: pool.submit(scan_project, project, roles, workdir, project_timeout, impersonate=impersonate, **project_kwargs) for project in projects}
            entries = {project: future.result() for project, future in futures.items()}
    for project, entry in entries.items():
        drifted = [role for role, verdict in entry[KEY_ROLES].items() if verdict[KEY_STATE] == ROLE_DRIFTED]
        if entry.get(KEY_ERROR):
            log(f"{project}: not checked: {entry[KEY_ERROR]}")
        elif drifted:
            log(f"{project}: drifted: {', '.join(drifted)}")
    document = {
        "schema_version": SCHEMA_VERSION,
        KEY_SCANNED_AT: iso(now),
        KEY_DURATION: int(time.monotonic() - started),
        KEY_PROJECTS: entries,
        KEY_SUMMARY: summarize(entries),
        KEY_PREVIOUS: {KEY_SCANNED_AT: (prior or {}).get(KEY_SCANNED_AT) if isinstance(prior, dict) else None, KEY_DRIFTED: drift_map(prior)},
    }
    return document


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def load_json(path: pathlib.Path | None) -> dict | None:
    if path is None or not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log(f"warning: {path}: {exc}; ignoring")
        return None
    return loaded if isinstance(loaded, dict) else None


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=pathlib.Path, required=True, help="where to write fixture-state.json")
    parser.add_argument("--prior", type=pathlib.Path, help="the previously published fixture-state.json (missing is fine)")
    parser.add_argument("--projects", help="comma-separated project ids (default: gitops_repo_for_project() in hack/ci-deploy.sh)")
    parser.add_argument("--ci-deploy-script", type=pathlib.Path, default=CI_DEPLOY_SCRIPT, help=argparse.SUPPRESS)
    parser.add_argument("--catalog", type=pathlib.Path, default=FLEET_CATALOG, help="fixtures.json (default: the repository's)")
    parser.add_argument("--workdir", type=pathlib.Path, help="where the per-project directories go (default: a temporary directory, removed afterwards)")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help=f"projects scanned at once (default {DEFAULT_WORKERS})")
    parser.add_argument("--project-timeout", type=float, default=DEFAULT_PROJECT_TIMEOUT_S, help=f"seconds per project for the runner and the state check together (default {DEFAULT_PROJECT_TIMEOUT_S})")
    parser.add_argument("--no-impersonate", action="store_true", help="read as the caller instead of each project's seeded-fleet reader (a laptop with direct access)")
    parser.add_argument("--now", help="the scan time to record, ISO 8601 (default: now)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        projects = [p for p in args.projects.split(",") if p] if args.projects else pool_projects(args.ci_deploy_script.read_text(encoding="utf-8"))
        roles = catalog_roles(args.catalog)
    except (OSError, ValueError) as exc:
        log(f"ERROR: {exc}")
        return EXIT_REPOSITORY_BUG
    if not projects:
        log("ERROR: no projects to scan")
        return EXIT_REPOSITORY_BUG
    workdir = args.workdir
    cleanup = workdir is None
    if workdir is None:
        workdir = pathlib.Path(tempfile.mkdtemp(prefix="fixture-state-"))
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        document = scan(
            projects,
            roles,
            workdir,
            prior=load_json(args.prior),
            now=parse_iso(args.now),
            workers=args.workers,
            project_timeout=args.project_timeout,
            impersonate=not args.no_impersonate,
            catalog=args.catalog,
        )
    finally:
        if cleanup:
            shutil.rmtree(workdir, ignore_errors=True)
    args.out.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    summary = document[KEY_SUMMARY]
    log(
        f"fixture-state: {summary['checked']} of {summary[KEY_PROJECTS]} pool projects checked, "
        f"{summary['drifted_projects']} with drift ({summary[ROLE_HEALTHY]} roles healthy, "
        f"{summary[ROLE_DRIFTED]} drifted, {summary[ROLE_NOT_CHECKED]} not checked) in {document[KEY_DURATION]}s; wrote {args.out}"
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
