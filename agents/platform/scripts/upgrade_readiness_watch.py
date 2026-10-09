#!/usr/bin/env python3
"""upgrade_readiness_watch.py - produce the upgrade readiness report when GKE
publishes a version the fleet would move to, without anyone asking.

The ``fleet-upgrade-verification`` skill's readiness report grades every
cluster on what would stop its next upgrade: a drain-blocking
PodDisruptionBudget, a maintenance exclusion covering the upgrade, node pools
too far below the target. Until this job existed it ran only when a user asked
for it in chat, so the fleet learned what an upgrade would break only when
someone thought to ask. This job runs once a day from the Platform Agent's
roster and asks the cheap half of the question first: which version is each
cluster's release channel offering now, and which clusters are below it. A
target version a cluster is below is *pending*. A pending version the ledger
has never seen is *new*, and a new version earns a readiness report at once.
After that the report is refreshed every ``REFRESH_DAYS_DEFAULT`` days (with
ten minutes of slack for the tick's own drift) while a cluster is still pending
it, and a version no cluster is pending any more is retired from the ledger
once every project that pended it has been read: a project the table did not
read keeps its clusters pending from the ledger, so a failed listing cannot
erase a version and have it come back as new. A report whose readiness reads graded none of a version's
pending clusters is written but not recorded, and the version is tried again
the next day, up to ``UNGRADED_ATTEMPTS_BEFORE_WEEKLY`` days in a row, after
which it is recorded as reported and stays on the weekly cadence until a
cluster is graded, so an unreachable cluster costs its project one sweep a
week rather than one a day and the daily ladder runs once, not every week; a cluster the script graded
``unknown`` counts as graded, since that is a verdict with its reason in the
table; "not read" is a cluster whose kubectl read failed (the script lists it
under ``errors`` and grades it ``unknown``) or that the run returned nothing
for, and a ``blocked`` verdict stands whatever the kubectl read did. A tick with nothing due prints nothing.

Which clusters. The projects come from this pod, not from the sandbox:
``UPGRADE_READINESS_PROJECTS`` when set, otherwise the management project
(``GCP_PROJECT_ID``) together with every project a Cluster Agent profile's
``cluster_identity`` names, the roster the cluster reconciler keeps; with
neither, the sandbox's ``gcloud config get-value project``; and with nothing
at all the tick fails closed rather than let the report script enumerate every
project the credential can list. Every cluster in those projects is read,
including ones ``spec.scope.exclude.clusters`` keeps a Cluster Agent from,
because the report script enumerates by project. The readiness read runs once
per project that holds a pending cluster, each run with its own timeout, so a
project the sandbox cannot finish leaves only its own clusters ungraded.

Where it runs, and how. The agent container carries no ``gcloud`` or
``kubectl``; both live in the shell sandbox behind the credential proxy, and
the sandbox login never executes a file under the agent-owned ``/opt/data``
(``deploy/sandbox/Dockerfile``). So the skill's two scripts are read here, from
the agent image's copy under ``/opt/platform-template``, and handed to
``python3 -I -`` on the command's stdin wrapped in a small loader that
registers ``upgrade_readiness`` as a module before running
``fleet_upgrade_report.main``: the same route ``stall_watch.py`` takes with
``stall_report.py``. ``-I`` keeps the sandbox's working directory off the
module path. The loader gives the report script a private temporary directory
of its own (``tempfile.mkdtemp``, owned by the sandbox login and readable by
nobody else) for its JSON output and its rollout record, reads the output
back, removes the directory, and prints the report as one JSON envelope after
a sentinel line; nothing the model can write to is on that path. The sandbox's
``/opt/data`` is a different directory from this pod's, so the files below
are written on this side.

What it writes. Under ``<agent home>/upgrade-readiness/``: ``ledger.json``,
one entry per target version with when it was first seen, when it was last
reported and which clusters are pending it; and ``reports/<version>/
<timestamp>.md`` with the tables the skill prints and a header saying why the
report was produced, beside the same report as ``.json``. ``<agent home>`` is
``HERMES_HOME``, the Platform Agent profile when the roster runs this job,
which is on the data volume and survives a pod restart. The kubeconfigs the
readiness read needs go to a directory of this job's own under the sandbox
login's home, where the model cannot read them. Each run starts from an empty
rollout record, so the progress section of a saved report is a first-run
baseline, not a comparison with last week.

Stdout is the chat message (``deliver: "chat"``): one line per report
produced, naming the target version, the clusters pending it, how many the
report graded blocked, ready and not graded and which are blocked, where the
report is on the gateway pod, and when the next refresh is due; one line per
version retired; one line when the version table was partial; and one line
when a tick fails. A quiet tick prints nothing, and
nothing reaches chat. Exit code is 0 on every path except a ledger or report
that cannot be written, because a tick whose ledger did not save would report
the same version as new again tomorrow.

``UPGRADE_READINESS_REFRESH_DAYS`` overrides the refresh interval,
``UPGRADE_READINESS_WATCH_HOME`` the directory the ledger and reports live in,
and ``UPGRADE_READINESS_PROJECTS`` (comma-separated) the projects. These are
for a run started by hand in the pod; the operator's ``spec.deployment.env``
allowlist does not carry them. ``--dry-run`` runs the version table and the
gate and prints what a real tick would do without running the readiness report
or touching the ledger.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gitops_workspace  # noqa: E402
import sandbox_exec  # noqa: E402

# Where the job keeps its ledger and reports, under the agent home.
HOME_ENV = "HERMES_HOME"
DEFAULT_HOME = "/opt/data"
# Under the ticker HERMES_HOME is the platform profile home; in a shell in the
# container it is the gateway's home, and the profile is a directory under it
# (feedback_prompt.py resolves the same two cases).
PROFILE_HOME = Path("profiles") / "platform"
WATCH_HOME_ENV = "UPGRADE_READINESS_WATCH_HOME"
WATCH_DIR_NAME = "upgrade-readiness"
LEDGER_FILE_NAME = "ledger.json"
LEDGER_TMP_SUFFIX = ".tmp"
LEDGER_SCHEMA_KEY = "schema_version"
LEDGER_SCHEMA_VERSION = 1
REPORTS_DIR_NAME = "reports"
REPORT_MARKDOWN_SUFFIX = ".md"
REPORT_JSON_SUFFIX = ".json"
REPORT_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
LATEST_LINK_NAME = "latest.md"
# Dated reports kept per version; older pairs go when a new one is written, and
# a retired version takes its directory with it.
REPORTS_KEPT_PER_VERSION = 10
JSON_INDENT = 2

# The refresh interval: a report per new version, then one a week while pending.
REFRESH_DAYS_ENV = "UPGRADE_READINESS_REFRESH_DAYS"
REFRESH_DAYS_DEFAULT = 7
# A ceiling well inside what timedelta and a date header can carry; past it the default applies.
REFRESH_DAYS_MAX = 365
# Ten minutes of slack on the weekly comparison: the tick's own start second drifts
# from one day to the next, and a strict week would land on day eight (as
# feedback_prompt.py found).
REFRESH_SLACK_SECONDS = 10 * 60

# Which projects the report enumerates, resolved on this side.
PROJECTS_ENV = "UPGRADE_READINESS_PROJECTS"
PROJECTS_SEPARATOR = ","
MANAGEMENT_PROJECT_ENV = "GCP_PROJECT_ID"
PROFILES_DIR = "profiles"
IDENTITY_PROJECT_KEY = "project"
CONFIG_PROJECT_ARGV = ("gcloud", "config", "get-value", "project")
PROJECT_LOOKUP_TIMEOUT_SECONDS = 30
PROJECT_FLAG = "--project"
NO_PROJECT_DETAIL = (
    f"no GCP project: set {PROJECTS_ENV} or {MANAGEMENT_PROJECT_ENV}, onboard a cluster, or configure gcloud in the sandbox; "
    "refusing to let the report enumerate every project the credential can list"
)

# The skill's scripts: the agent image's copy first, the checkout's beside this
# file for a run from the repository.
IMAGE_SKILL_SCRIPTS_DIR = "/opt/platform-template/skills/fleet-upgrade-verification/scripts"
LOCAL_SKILL_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "fleet-upgrade-verification" / "scripts"
SKILL_SCRIPTS_DIR_ENV = "UPGRADE_READINESS_SKILL_SCRIPTS"
REPORT_MODULE = "fleet_upgrade_report"
READINESS_MODULE = "upgrade_readiness"
PYTHON_SOURCE_SUFFIX = ".py"

# How the report runs in the sandbox.
PYTHON_EXECUTABLE = "python3"
PYTHON_ISOLATED_FLAG = "-I"
STDIN_SCRIPT_ARG = "-"
SANDBOX_TMP_PREFIX = "upgrade-readiness-watch-"
SANDBOX_OUTPUT_NAME = "report.json"
SANDBOX_STATE_DIR_NAME = "state"
SANDBOX_KUBECONFIG_DIR = "/home/hermes/.kubeconfigs/upgrade-readiness-watch"
OUTPUT_FLAG = "--output"
STATE_DIR_FLAG = "--state-dir"
READINESS_FLAG = "--readiness"
KUBECONFIG_DIR_FLAG = "--kubeconfig-dir"
ENVELOPE_SENTINEL = "__UPGRADE_READINESS_WATCH_ENVELOPE__"
ENVELOPE_EXIT_KEY = "exit"
ENVELOPE_TABLES_KEY = "tables"
ENVELOPE_REPORT_KEY = "report"
WROTE_LINE_PREFIX = "Wrote "
VERSION_TABLE_TIMEOUT_SECONDS = 600
READINESS_TIMEOUT_SECONDS = 1500
# Hermes kills a no_agent script at an hour (stall_watch.py records it); the
# whole tick stays under this. The version table and the readiness runs go
# project by project, each capped at its own timeout and at what the budget
# has left; a project the budget cannot reach is left unread and named rather
# than started, and the next sweep starts at it.
TICK_BUDGET_SECONDS = 2700
# The version table's share of the budget: what it does not use by then is the
# readiness runs', so a table that does not fit never starves the report, which
# is the tick's deliverable; the projects it did not reach are read first tomorrow.
TABLE_BUDGET_SECONDS = 900
MIN_PROJECT_RUN_SECONDS = 120
BUDGET_EXHAUSTED_DETAIL = "tick budget exhausted before project {project} could run"
TABLE_UNRUN_DETAIL = "tick budget exhausted before the version table read project {project}"
TABLE_CUT_SHORT_DETAIL = "the version table's run for project {project} was cut short by its share of the budget"
TABLE_FAILED_DETAIL = "version table for {project} failed: {error}"
PROJECT_NOT_RUN_DETAIL = "readiness run for {project} not started: {error}"
PROJECT_CUT_SHORT_DETAIL = "readiness run for {project} cut short by the tick budget; it goes first next time"
STDERR_EXCERPT_CHARS = 300

# The report's vocabulary this job reads (fleet_upgrade_report.py, upgrade_readiness.py).
MEMBERS_KEY = "members"
EXIT_OK = 0
STATUS_KEY = "status"
TARGET_KEY = "target_version"
READINESS_KEY = "readiness"
ERRORS_KEY = "errors"
MESSAGE_KEY = "message"
MEMBER_ID_KEYS = ("project", "location", "cluster")
BEHIND_STATUSES = frozenset({"lagging", "patch-behind"})
# The statuses that settle a cluster as no longer below a version: a graded
# position above or at it. "unknown" (a target the script could not resolve)
# settles nothing; the cluster stays pending until a run grades it.
SETTLED_STATUSES = frozenset({"current", "ahead"})
READINESS_BLOCKED = "blocked"
READINESS_READY = "ready"
READINESS_UNKNOWN = "unknown"
MEMBER_KEY_SEPARATOR = "/"
# fleet_upgrade_report.VERSION_RE, which every target_version the report prints
# has matched; a ledger key that does not is not a version and names no directory.
VERSION_KEY_RE = re.compile(r"^\d+\.\d+\.\d+(?:-gke\.\d+)?$")

# Ledger vocabulary.
TARGETS_KEY = "targets"
FIRST_SEEN_KEY = "first_seen"
LAST_REPORT_KEY = "last_report_at"
PENDING_KEY = "pending"
LAST_TICK_KEY = "last_tick"
# When each project's version table and readiness run last ran to completion or
# to its own cap: each sweep runs the least recently run projects first, so a
# project the budget left unread or cut short goes first next time, whichever
# versions are due then.
TABLE_RUNS_KEY = "table_runs"
READINESS_RUNS_KEY = "readiness_runs"
# The report script keys members by the project id it resolved, so a project the
# roster spells as a number must be keyed the same way in the watch's own read
# errors and stamps; the id is learned from the first successful table run.
PROJECT_IDS_KEY = "project_ids"
REPORT_PROJECTS_KEY = "projects"
ANNOUNCED_KEY = "announced"
ANNOUNCED_PARTIAL_KEY = "partial"
ANNOUNCED_UNGRADED_KEY = "ungraded"
# A failed tick writes nothing to the ledger; the failure it announced is kept
# in this file beside it, so the same failure is posted once and its recovery once.
FAILURE_MARKER_FILE_NAME = "last-failure.txt"
RECOVERED_LINE = "{prefix} watch: the tick runs again"
DETAIL_KEY = "detail"
ATTEMPTS_KEY = "attempts"
REASON_NEW = "new target version"
REASON_REFRESH = "scheduled refresh"

# Output.
LINE_PREFIX = "upgrade readiness"
RETIRED_LINE = "{prefix}: {version} is no longer pending on any cluster; retired from the watch"
REPORT_LINE = (
    "{prefix}: {reason} {version}, {pending} cluster(s) pending ({names}): {blocked} blocked{blocked_names}, "
    "{ready} ready{ungraded}; report on the gateway pod at {path}; next refresh after {next_date}"
)
BLOCKED_NAMES = " ({names})"
FAILED_LINE = "{prefix} watch: {what} failed: {detail}"
FAILED_WHAT_TICK = "the tick"
FAILED_WHAT_WRITE = "writing the ledger or the report"
PARTIAL_LINE = "{prefix} watch: the version table was partial ({errors} read error(s), exit {code}); a version is retired only once every project that pends it has been read"
PARTIAL_CLEARED_LINE = "{prefix} watch: the version table reads every project again"
DRY_RUN_WOULD_RETIRE = "dry run: would retire {version} (no cluster is pending it)"
UNGRADED_LINE = (
    "{prefix}: {reason} {version}, {pending} cluster(s) pending ({names}): none graded ({detail}); "
    "report on the gateway pod at {path}; retrying tomorrow"
)
NOT_RUN_LINE = (
    "{prefix}: {reason} {version}, {pending} cluster(s) pending ({names}): not run to completion; the tick budget "
    "was spent before {projects} finished; retried tomorrow, starting there"
)
UNKNOWN_COUNT = ", {count} unknown"
UNREAD_COUNT = ", {count} not read"
READS_FAILED_DETAIL = "their kubectl read failed or the run returned nothing for them"
UNGRADED_ATTEMPTS_BEFORE_WEEKLY = 3
UNGRADED_PARKED_LINE = (
    "{prefix}: {reason} {version}, {pending} cluster(s) pending ({names}): none graded ({detail}); "
    "not graded on {attempts} consecutive attempt(s); report on the gateway pod at {path}; next attempt at the weekly refresh"
)
PARKED_KEY = "parked"
PROJECT_FAILURES_SUFFIX = "; {failures}"
PROJECT_RUN_FAILED_DETAIL = "readiness run for {project} failed: {error}"
TIMED_OUT_DETAIL = "the sandbox run timed out after {seconds}s"
# Phrases the saved report ends its header with; the one that is true for the
# branch the tick takes, so the file and the chat line agree on what happens next.
NEXT_REFRESH_PHRASE = "The next scheduled refresh is after {date} while any cluster is still pending"
NEXT_TOMORROW_PHRASE = "None of the pending clusters was graded, so the job retries tomorrow"
NEXT_WEEKLY_PHRASE = "None of the pending clusters was graded on {attempts} consecutive attempt(s), so the next attempt is at the weekly refresh"
# What the partial-table announcement is keyed on: the kinds of read error and
# how many of each, not their text, so a window of unread projects that turns
# daily, or a timeout's figure, does not re-post the line.
ERROR_KIND_UNRUN = "table not run to completion by the budget"
ERROR_KIND_TABLE_FAILED = "table failed"
# The watch writes its own read errors with the kind on them; the report
# script's carry no kind and are classified by their shape.
ERROR_KIND_KEY = "kind"
ERROR_KIND_LISTING = "listing failed"
ERROR_KIND_SERVER_CONFIG = "server config failed"
ERROR_KIND_READ = "read failed"
DRY_RUN_WOULD_REPORT = "dry run: would report {version} ({reason}) for {names}"
DRY_RUN_NOTHING_DUE = "dry run: nothing due; pending versions: {versions}"
NONE_WORD = "none"
MAX_NAMES_IN_LINE = 6
NAMES_OVERFLOW = ", +{more} more"
WRITE_FAILED_EXIT = 2
DATE_FORMAT = "%Y-%m-%d"


class SandboxTimedOut(RuntimeError):
    """The sandbox run hit the client-side timeout."""


class WriteFailed(Exception):
    """The ledger or a report file could not be written; the tick's result must not be claimed."""


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(moment: datetime) -> str:
    return moment.isoformat()


def parse_iso(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def refresh_days() -> int:
    raw = os.environ.get(REFRESH_DAYS_ENV, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return REFRESH_DAYS_DEFAULT
    return value if 0 < value <= REFRESH_DAYS_MAX else REFRESH_DAYS_DEFAULT


def profile_home() -> Path:
    """The platform profile's home: HERMES_HOME itself under the ticker, or the
    profile directory beneath it from a shell in the container, so a hand run
    and the scheduled tick share one ledger."""
    home = Path(os.environ.get(HOME_ENV, DEFAULT_HOME))
    beneath = home / PROFILE_HOME
    return beneath if beneath.is_dir() else home


def watch_home() -> Path:
    override = os.environ.get(WATCH_HOME_ENV, "").strip()
    if override:
        return Path(override)
    return profile_home() / WATCH_DIR_NAME


# --- which projects ----------------------------------------------------------


def roster_projects() -> set[str]:
    """The projects the Cluster Agent profiles' identities name, as
    stall_watch.py reads them; a profile whose identity cannot be read names none."""
    from cluster_agent_profile import RESERVED_PROFILES, read_cluster_identity  # lazy, as in stall_watch

    base = Path(gitops_workspace.agent_home()) / PROFILES_DIR
    if not base.is_dir():
        return set()
    projects: set[str] = set()
    for home in base.iterdir():
        if home.name in RESERVED_PROFILES or not home.is_dir():
            continue
        try:
            identity = read_cluster_identity(home)
        except Exception:  # noqa: BLE001 - any unreadable file is an absent identity
            identity = None
        if identity and identity.get(IDENTITY_PROJECT_KEY):
            projects.add(identity[IDENTITY_PROJECT_KEY])
    return projects


def projects() -> list[str]:
    explicit = [p.strip() for p in os.environ.get(PROJECTS_ENV, "").split(PROJECTS_SEPARATOR) if p.strip()]
    if explicit:
        return sorted(set(explicit))
    found = roster_projects()
    management = os.environ.get(MANAGEMENT_PROJECT_ENV, "").strip()
    if management:
        found.add(management)
    if found:
        return sorted(found)
    try:
        completed = sandbox_exec.run(list(CONFIG_PROJECT_ARGV), timeout=PROJECT_LOOKUP_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        raise RuntimeError(TIMED_OUT_DETAIL.format(seconds=PROJECT_LOOKUP_TIMEOUT_SECONDS)) from None
    configured = (completed.stdout or "").strip()
    if not configured:
        raise RuntimeError(NO_PROJECT_DETAIL)
    return [configured]


def project_flags(names: list[str]) -> list[str]:
    flags: list[str] = []
    for name in names:
        flags += [PROJECT_FLAG, name]
    return flags


# --- the sandbox hop -------------------------------------------------------


def skill_scripts_dir() -> Path:
    override = os.environ.get(SKILL_SCRIPTS_DIR_ENV, "").strip()
    candidates = [Path(override)] if override else [Path(IMAGE_SKILL_SCRIPTS_DIR), LOCAL_SKILL_SCRIPTS_DIR]
    for candidate in candidates:
        if (candidate / (REPORT_MODULE + PYTHON_SOURCE_SUFFIX)).is_file():
            return candidate
    raise RuntimeError(f"{REPORT_MODULE}.py not found under {', '.join(str(c) for c in candidates)}")


def loader_source(argv: list[str]) -> str:
    """The program ``python3 -I -`` runs in the sandbox: both skill scripts as
    string literals, the readiness module registered first so the report's
    ``import upgrade_readiness`` resolves, then ``main`` with ``argv`` plus an
    output path and a rollout record inside a private temporary directory, its
    tables captured, the directory removed, and one JSON envelope printed after
    a sentinel line."""
    scripts = skill_scripts_dir()
    readiness_path = scripts / (READINESS_MODULE + PYTHON_SOURCE_SUFFIX)
    report_path = scripts / (REPORT_MODULE + PYTHON_SOURCE_SUFFIX)
    return "\n".join(
        [
            "import contextlib, io, json, os, shutil, sys, tempfile, types",
            f"READINESS_SRC = {json.dumps(readiness_path.read_text(encoding='utf-8'))}",
            f"REPORT_SRC = {json.dumps(report_path.read_text(encoding='utf-8'))}",
            f"ARGV = {json.dumps(argv)}",
            f"SENTINEL = {json.dumps(ENVELOPE_SENTINEL)}",
            f"readiness = types.ModuleType({json.dumps(READINESS_MODULE)})",
            f"readiness.__file__ = {json.dumps(str(readiness_path))}",
            "exec(compile(READINESS_SRC, readiness.__file__, 'exec'), readiness.__dict__)",
            f"sys.modules[{json.dumps(READINESS_MODULE)}] = readiness",
            f"report = types.ModuleType({json.dumps(REPORT_MODULE)})",
            f"report.__file__ = {json.dumps(str(report_path))}",
            "exec(compile(REPORT_SRC, report.__file__, 'exec'), report.__dict__)",
            f"private = tempfile.mkdtemp(prefix={json.dumps(SANDBOX_TMP_PREFIX)})",
            f"output = os.path.join(private, {json.dumps(SANDBOX_OUTPUT_NAME)})",
            f"state = os.path.join(private, {json.dumps(SANDBOX_STATE_DIR_NAME)})",
            "tables = io.StringIO()",
            "data = None",
            "try:",
            "    with contextlib.redirect_stdout(tables):",
            f"        code = report.main(ARGV + [{json.dumps(OUTPUT_FLAG)}, output, {json.dumps(STATE_DIR_FLAG)}, state])",
            "    if os.path.exists(output):",
            "        with open(output, encoding='utf-8') as handle:",
            "            data = json.load(handle)",
            "finally:",
            "    shutil.rmtree(private, ignore_errors=True)",
            "print(SENTINEL)",
            f"print(json.dumps({{{json.dumps(ENVELOPE_EXIT_KEY)}: code, {json.dumps(ENVELOPE_TABLES_KEY)}: tables.getvalue(), {json.dumps(ENVELOPE_REPORT_KEY)}: data}}))",
            "",
        ]
    )


def envelope_line(stdout: str) -> str | None:
    """The JSON line after the last line that is exactly the sentinel. The
    sentinel inside a tenant-written string sits on the JSON line itself, never
    alone on a line, so it cannot shift the split."""
    lines = stdout.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() == ENVELOPE_SENTINEL:
            return lines[index + 1].strip() if index + 1 < len(lines) else None
    return None


def report_argv(names: list[str], readiness: bool) -> list[str]:
    argv = project_flags(names)
    if readiness:
        argv += [READINESS_FLAG, KUBECONFIG_DIR_FLAG, SANDBOX_KUBECONFIG_DIR]
    return argv


def run_report(names: list[str], readiness: bool, timeout: float | None = None) -> dict:
    """Run the skill's report in the sandbox and return the envelope:
    ``{"exit": int, "tables": str, "report": dict | None}``."""
    if timeout is None:
        timeout = READINESS_TIMEOUT_SECONDS if readiness else VERSION_TABLE_TIMEOUT_SECONDS
    try:
        completed = sandbox_exec.run(
            [PYTHON_EXECUTABLE, PYTHON_ISOLATED_FLAG, STDIN_SCRIPT_ARG],
            timeout=timeout,
            check=False,
            stdin=loader_source(report_argv(names, readiness)),
        )
    except subprocess.TimeoutExpired:
        raise SandboxTimedOut(TIMED_OUT_DETAIL.format(seconds=int(timeout))) from None
    stdout = completed.stdout or ""
    excerpt = " ".join((completed.stderr or "").split())[:STDERR_EXCERPT_CHARS]
    envelope_text = envelope_line(stdout)
    if envelope_text is None:
        detail = excerpt or " ".join(stdout.split())[:STDERR_EXCERPT_CHARS]
        raise RuntimeError(f"sandbox exited {completed.returncode} without a report: {detail}")
    envelope = json.loads(envelope_text)
    if not isinstance(envelope.get(ENVELOPE_REPORT_KEY), dict):
        raise RuntimeError(f"report script exited {envelope.get(ENVELOPE_EXIT_KEY)} and wrote no report: {excerpt}")
    return envelope


# --- the gate --------------------------------------------------------------


def member_key(member: dict) -> str:
    return MEMBER_KEY_SEPARATOR.join(str(member.get(k, "")) for k in MEMBER_ID_KEYS)


def canonical_member_key(member: dict, ids: dict[str, str]) -> str:
    """The member key with the project as its learned id: a report that fell
    back to a numbered spelling keys the same cluster the way the ledger does."""
    canonical = dict(member)
    canonical[MEMBER_ID_KEYS[0]] = ids.get(str(member.get(MEMBER_ID_KEYS[0], "")), member.get(MEMBER_ID_KEYS[0], ""))
    return member_key(canonical)


def canonicalise_members(report: dict, ids: dict[str, str]) -> None:
    """Rewrite each member's project to its learned id in place, so every
    reader of the envelope keys the cluster the way the ledger does."""
    for member in report.get(MEMBERS_KEY) or []:
        project = str(member.get(MEMBER_ID_KEYS[0], ""))
        member[MEMBER_ID_KEYS[0]] = ids.get(project, project)


def pending_targets(report: dict, ids: dict[str, str] | None = None) -> dict[str, list[str]]:
    """Target version -> the clusters below it, from the report's members."""
    pending: dict[str, list[str]] = {}
    for member in report.get(MEMBERS_KEY) or []:
        # The report grades a stripped copy of the version and stores the raw
        # one; the key the ledger and the report directory get is the shape the
        # ledger's reader accepts, and anything else is not a version.
        target = str(member.get(TARGET_KEY) or "").strip()
        if not VERSION_KEY_RE.fullmatch(target) or member.get(STATUS_KEY) not in BEHIND_STATUSES:
            continue
        key = canonical_member_key(member, ids or {})
        if key not in pending.setdefault(target, []):
            pending[target].append(key)
    return {version: sorted(keys) for version, keys in pending.items()}


def error_kind(error: dict) -> str:
    if error.get(ERROR_KIND_KEY):
        return str(error[ERROR_KIND_KEY])
    if not error.get(MEMBER_ID_KEYS[1]):
        return ERROR_KIND_LISTING
    if not error.get(MEMBER_ID_KEYS[-1]):
        return ERROR_KIND_SERVER_CONFIG
    return ERROR_KIND_READ


def partial_signature(read_errors: list) -> str:
    """The announcement key for a partial table: the kinds of read error, not
    their count, since how many projects a morning's latency leaves unread
    moves day to day while the condition is the same."""
    return json.dumps(sorted({error_kind(error) for error in read_errors}))


def unlisted_projects(read_errors: list) -> set[str]:
    """Projects whose clusters this tick's table did not enumerate: a failed or
    unrun listing is an error with a project and no location."""
    return {error.get(MEMBER_ID_KEYS[0]) for error in read_errors if error.get(MEMBER_ID_KEYS[0]) and not error.get(MEMBER_ID_KEYS[1])}


def prune_read_projects(ledger: dict, pending: dict[str, list[str]], names: list[str], read_errors: list, ids: dict[str, str]) -> None:
    """A project the table listed this tick is the truth for its clusters: a
    ledger version keeps, from that project, only the clusters the table still
    shows below it. Without this a version whose clusters upgraded kept its
    stale list while the table stayed partial, and a later carry-forward
    reported those clusters as pending again."""
    read = {ids.get(name, name) for name in names} - unlisted_projects(read_errors)
    unconfigured = {
        (error.get(MEMBER_ID_KEYS[0]), error.get(MEMBER_ID_KEYS[1]))
        for error in read_errors
        if error.get(MEMBER_ID_KEYS[1]) and not error.get(MEMBER_ID_KEYS[-1])
    }

    def settled(key: str) -> bool:
        parts = key.split(MEMBER_KEY_SEPARATOR)
        return parts[0] in read and tuple(parts[:2]) not in unconfigured

    for version, entry in ledger[TARGETS_KEY].items():
        still = set(pending.get(version, []))
        entry[PENDING_KEY] = [key for key in entry.get(PENDING_KEY) or [] if not settled(key) or key in still]


def carry_forward_unlisted(pending: dict[str, list[str]], ledger: dict, read_errors: list) -> None:
    """A project whose ``clusters list`` failed (an error with no location),
    or a location whose ``get-server-config`` failed (an error with a location
    and no cluster, which leaves its clusters with no target), contributed no
    pending members, so those clusters would drop out of every version's
    pending set and rejoin a week later under a version already recorded.
    Keep them pending from the ledger instead; the readiness run then counts
    them as not read, and a version with nothing graded is not recorded."""
    unlisted = {error.get(MEMBER_ID_KEYS[0]) for error in read_errors if error.get(MEMBER_ID_KEYS[0]) and not error.get(MEMBER_ID_KEYS[1])}
    unconfigured = {
        (error.get(MEMBER_ID_KEYS[0]), error.get(MEMBER_ID_KEYS[1]))
        for error in read_errors
        if error.get(MEMBER_ID_KEYS[1]) and not error.get(MEMBER_ID_KEYS[-1])
    }
    if not unlisted and not unconfigured:
        return
    for version, entry in ledger[TARGETS_KEY].items():
        kept = [
            key for key in entry.get(PENDING_KEY) or []
            if project_of(key) in unlisted or tuple(key.split(MEMBER_KEY_SEPARATOR)[:2]) in unconfigured
        ]
        if kept:
            merged = sorted(set(pending.get(version, [])) | set(kept))
            pending[version] = merged


def empty_ledger() -> dict:
    return {LEDGER_SCHEMA_KEY: LEDGER_SCHEMA_VERSION, TARGETS_KEY: {}, LAST_TICK_KEY: None, ANNOUNCED_KEY: {}, TABLE_RUNS_KEY: {}, READINESS_RUNS_KEY: {}, PROJECT_IDS_KEY: {}}


def announced(ledger: dict) -> dict:
    return ledger.setdefault(ANNOUNCED_KEY, {})


def load_ledger(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty_ledger()
    except ValueError as exc:
        raise RuntimeError(f"ledger at {path} is not valid JSON ({exc}); refusing to overwrite it") from None
    if not isinstance(data, dict) or not isinstance(data.get(TARGETS_KEY), dict):
        raise RuntimeError(f"ledger at {path} is not this job's ledger; refusing to overwrite it")
    for version, entry in data[TARGETS_KEY].items():
        shape_ok = (
            isinstance(version, str)
            and VERSION_KEY_RE.fullmatch(version) is not None
            and isinstance(entry, dict)
            and isinstance(entry.get(PENDING_KEY), list)
            and all(isinstance(c, str) for c in entry[PENDING_KEY])
            and (entry.get(LAST_REPORT_KEY) is None or parse_iso(entry.get(LAST_REPORT_KEY)) is not None)
        )
        if not shape_ok:
            raise RuntimeError(f"ledger at {path} has a malformed entry for {version}; refusing to overwrite it")
    block = data.get(ANNOUNCED_KEY)
    if block is None:
        data[ANNOUNCED_KEY] = {}
    elif not announced_shape_ok(block):
        raise RuntimeError(f"ledger at {path} has a malformed {ANNOUNCED_KEY} block; refusing to overwrite it")
    # A null map is the absent one: ``setdefault`` and ``get`` would otherwise
    # hand the None back on the next due or retired version.
    if data[ANNOUNCED_KEY].get(ANNOUNCED_UNGRADED_KEY) is None:
        data[ANNOUNCED_KEY][ANNOUNCED_UNGRADED_KEY] = {}
    for key in (TABLE_RUNS_KEY, READINESS_RUNS_KEY, PROJECT_IDS_KEY):
        runs = data.get(key)
        data[key] = {k: v for k, v in runs.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(runs, dict) else {}
    return data


def announced_shape_ok(block: object) -> bool:
    if not isinstance(block, dict):
        return False
    partial = block.get(ANNOUNCED_PARTIAL_KEY)
    if partial is not None and not isinstance(partial, str):
        return False
    ungraded = block.get(ANNOUNCED_UNGRADED_KEY)
    if ungraded is None:
        return True
    if not isinstance(ungraded, dict):
        return False
    return all(
        isinstance(entry, dict)
        and isinstance(entry.get(DETAIL_KEY), str)
        and isinstance(entry.get(ATTEMPTS_KEY), int)
        and isinstance(entry.get(PARKED_KEY, False), bool)
        for entry in ungraded.values()
    )


def save_ledger(path: Path, ledger: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + LEDGER_TMP_SUFFIX)
    tmp.write_text(json.dumps(ledger, indent=JSON_INDENT, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def decide(
    ledger: dict, pending: dict[str, list[str]], now: datetime, days: int, retire: bool = True, dry_run: bool = False
) -> tuple[dict[str, str], list[str]]:
    """Apply the gate. Returns (due: version -> reason, retired versions) and
    updates the ledger's targets in place: new versions get ``first_seen``,
    every pending version its current cluster list, and, when ``retire`` is
    set (a complete version table), versions no cluster is pending go. A
    ``last_report_at`` in the future counts as never reported."""
    targets = ledger[TARGETS_KEY]
    due: dict[str, str] = {}
    interval = timedelta(days=days) - timedelta(seconds=REFRESH_SLACK_SECONDS)
    for version, clusters in sorted(pending.items()):
        entry = targets.get(version)
        if entry is None:
            targets[version] = {FIRST_SEEN_KEY: iso(now), LAST_REPORT_KEY: None, PENDING_KEY: clusters}
            due[version] = REASON_NEW
            continue
        entry[PENDING_KEY] = clusters
        last = parse_iso(entry.get(LAST_REPORT_KEY))
        if last is None or last > now:
            due[version] = REASON_NEW
        elif now - last >= interval:
            due[version] = REASON_REFRESH
    # A version no cluster is pending goes when the table was complete, or when
    # every project that pended it was read this tick and none still does.
    retired = sorted(version for version in targets if version not in pending and (retire or not targets[version].get(PENDING_KEY)))
    if dry_run:
        return due, retired
    for version in retired:
        del targets[version]
    return due, retired


# --- the report files ------------------------------------------------------


def settled_against(listing: tuple[str, str], version: str) -> bool:
    """True when a listed member is definitely no longer below ``version``: a
    graded position at or above it, or a graded lag toward a different target.
    An unresolved target (status unknown, empty target) settles nothing."""
    status, target = listing
    if status in SETTLED_STATUSES:
        return True
    return status in BEHIND_STATUSES and bool(target) and target != version


def settle_from_readiness(report: dict, pending: dict[str, list[str]], ledger: dict) -> list[str]:
    """A readiness run lists every cluster of its project, including one the
    version table did not reach this tick. A listed cluster that is no longer
    below a version leaves that version's pending set, here and in the ledger,
    so it is neither reported pending nor graded against a version it has
    passed. Returns the versions left with no pending cluster, for retirement."""
    listed: dict[str, tuple[str, str]] = {}
    for member in report.get(MEMBERS_KEY) or []:
        listed[canonical_member_key(member, ledger.get(PROJECT_IDS_KEY) or {})] = (str(member.get(STATUS_KEY) or ""), str(member.get(TARGET_KEY) or "").strip())
    emptied: list[str] = []
    for version in list(pending):
        kept = [
            key for key in pending[version]
            if key not in listed or not settled_against(listed[key], version)
        ]
        pending[version] = kept
        if version in ledger[TARGETS_KEY]:
            ledger[TARGETS_KEY][version][PENDING_KEY] = kept
        if not kept:
            emptied.append(version)
    return emptied


def readiness_verdicts(report: dict, clusters: list[str]) -> tuple[list[str], list[str], list[str], list[str]]:
    """The blocked, ready, unknown and unread clusters among ``clusters``, by
    member key. ``unknown`` is a verdict the script gave (an exclusion or a pool
    it could not decide) and counts as graded; ``unread`` is a cluster the run
    returned no member for, one whose kubectl read the report lists under
    ``errors`` and that the script graded ``unknown`` for want of that read, or
    one graded ``unknown`` in a location whose ``get-server-config`` failed (an
    ``errors`` entry with a location and no cluster), which left it no target. A
    ``blocked`` verdict stands whatever the kubectl read did: the script grades
    a covering exclusion or blocking skew from cluster metadata and says a
    definite blocker beats an unknown."""
    wanted = set(clusters)
    errors = report.get(ERRORS_KEY) or []
    failed_reads = {member_key(error) for error in errors if error.get(MEMBER_ID_KEYS[-1])}
    failed_locations = {
        (error.get(MEMBER_ID_KEYS[0]), error.get(MEMBER_ID_KEYS[1]))
        for error in errors
        if error.get(MEMBER_ID_KEYS[1]) and not error.get(MEMBER_ID_KEYS[-1])
    }
    buckets: dict[str, list[str]] = {READINESS_BLOCKED: [], READINESS_READY: [], READINESS_UNKNOWN: []}
    for member in report.get(MEMBERS_KEY) or []:
        key = member_key(member)
        if key not in wanted:
            continue
        status = (member.get(READINESS_KEY) or {}).get(STATUS_KEY)
        location = (member.get(MEMBER_ID_KEYS[0]), member.get(MEMBER_ID_KEYS[1]))
        read_failed = key in failed_reads or (location in failed_locations and status == READINESS_UNKNOWN)
        if read_failed and status != READINESS_BLOCKED:
            continue
        if status in buckets:
            buckets[status].append(key)
    seen = {k for keys in buckets.values() for k in keys}
    unread = sorted(wanted - seen)
    return sorted(buckets[READINESS_BLOCKED]), sorted(buckets[READINESS_READY]), sorted(buckets[READINESS_UNKNOWN]), unread


def project_of(member_key_text: str) -> str:
    return member_key_text.split(MEMBER_KEY_SEPARATOR, 1)[0]


def ordered_projects(names: set[str] | list[str], last_runs: dict[str, str]) -> list[str]:
    """The projects least recently run first (never run before any), then by
    name: a project the last sweep left unread or cut short goes first next
    time, whichever versions are due then."""
    return sorted(names, key=lambda project: (last_runs.get(project, ""), project))


def empty_envelope() -> dict:
    return {ENVELOPE_EXIT_KEY: EXIT_OK, ENVELOPE_TABLES_KEY: "", ENVELOPE_REPORT_KEY: {MEMBERS_KEY: [], ERRORS_KEY: []}}


def merge_envelope(merged: dict, envelope: dict) -> None:
    merged[ENVELOPE_TABLES_KEY] += envelope.get(ENVELOPE_TABLES_KEY, "")
    merged[ENVELOPE_REPORT_KEY][MEMBERS_KEY] += envelope[ENVELOPE_REPORT_KEY].get(MEMBERS_KEY) or []
    merged[ENVELOPE_REPORT_KEY][ERRORS_KEY] += envelope[ENVELOPE_REPORT_KEY].get(ERRORS_KEY) or []
    if envelope.get(ENVELOPE_EXIT_KEY) != EXIT_OK and merged[ENVELOPE_EXIT_KEY] == EXIT_OK:
        merged[ENVELOPE_EXIT_KEY] = envelope.get(ENVELOPE_EXIT_KEY)


def versions_by_project(names: list[str], deadline: float, last_runs: dict[str, str], now: datetime, ids: dict[str, str]) -> dict:
    """The version table, one sandbox run per project under its own timeout and
    what is left of the table's share of the budget before ``deadline``, merged
    into one envelope. A project whose run failed, or that the budget did not reach, is a
    read error with no location, the shape a failed ``clusters list`` has, so
    the table counts as partial: nothing is retired, and the ledger's pending
    clusters of that project are carried forward. ``last_runs`` is stamped for
    every project that ran to completion or to its own cap, so the next table
    starts with the ones it did not."""
    merged = empty_envelope()
    unrun: list[str] = []
    cut_short: list[str] = []
    failed: dict[str, Exception] = {}

    def keyed(project: str) -> str:
        return ids.get(project, project)

    # Two spellings of one project (a number and its id) run once: the first
    # spelling the order reaches runs, the other is the same project.
    tabled: set[str] = set()
    for project in ordered_projects(names, last_runs):
        if keyed(project) in tabled:
            continue
        tabled.add(keyed(project))
        remaining = deadline - time.monotonic()
        if remaining < MIN_PROJECT_RUN_SECONDS:
            unrun.append(project)
            continue
        timeout = min(VERSION_TABLE_TIMEOUT_SECONDS, remaining)
        try:
            envelope = run_report([project], readiness=False, timeout=timeout)
        except SandboxTimedOut as exc:
            if timeout < VERSION_TABLE_TIMEOUT_SECONDS:
                # Killed by the budget, not by its own cap: not stamped, so it goes first next time.
                cut_short.append(project)
                continue
            last_runs[project] = iso(now)
            failed[project] = exc
            merged[ENVELOPE_REPORT_KEY][ERRORS_KEY].append(
                {MEMBER_ID_KEYS[0]: keyed(project), MEMBER_ID_KEYS[1]: None, ERROR_KIND_KEY: ERROR_KIND_TABLE_FAILED, MESSAGE_KEY: TABLE_FAILED_DETAIL.format(project=project, error=str(exc))}
            )
            continue
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            last_runs[project] = iso(now)
            failed[project] = exc
            error = f"{type(exc).__name__}: {exc}" if not isinstance(exc, RuntimeError) else str(exc)
            merged[ENVELOPE_REPORT_KEY][ERRORS_KEY].append(
                {MEMBER_ID_KEYS[0]: keyed(project), MEMBER_ID_KEYS[1]: None, ERROR_KIND_KEY: ERROR_KIND_TABLE_FAILED, MESSAGE_KEY: TABLE_FAILED_DETAIL.format(project=project, error=error)}
            )
            continue
        last_runs[project] = iso(now)
        resolved = envelope[ENVELOPE_REPORT_KEY].get(REPORT_PROJECTS_KEY) or []
        if len(resolved) == 1 and isinstance(resolved[0], str) and resolved[0]:
            # A learned id stands: on a day `projects describe` fails the script
            # falls back to the spelling itself, which must not replace the id.
            if resolved[0] != project:
                ids[project] = resolved[0]
            elif project not in ids:
                ids[project] = project
        merge_envelope(merged, envelope)
    if failed and len(failed) == len(names):
        # No project read at all is the sandbox or the credential, not a
        # project: one failure line for the tick, announced once, rather than a
        # partial table nobody can act on.
        raise next(iter(failed.values()))
    # Both budget shapes are one kind: the announcement must not re-post when a
    # cut-short run comes and goes beside the projects never reached.
    for project in unrun:
        merged[ENVELOPE_REPORT_KEY][ERRORS_KEY].append(
            {MEMBER_ID_KEYS[0]: keyed(project), MEMBER_ID_KEYS[1]: None, ERROR_KIND_KEY: ERROR_KIND_UNRUN, MESSAGE_KEY: TABLE_UNRUN_DETAIL.format(project=project)}
        )
    for project in cut_short:
        merged[ENVELOPE_REPORT_KEY][ERRORS_KEY].append(
            {MEMBER_ID_KEYS[0]: keyed(project), MEMBER_ID_KEYS[1]: None, ERROR_KIND_KEY: ERROR_KIND_UNRUN, MESSAGE_KEY: TABLE_CUT_SHORT_DETAIL.format(project=project)}
        )
    return merged


def readiness_by_project(
    pending: dict[str, list[str]], due: dict[str, str], deadline: float, last_runs: dict[str, str], now: datetime, ids: dict[str, str]
) -> tuple[dict, dict[str, str], dict[str, str]]:
    """One readiness run per project that holds a due version's pending
    clusters, each capped by its own timeout and by what is left of the tick's
    budget before ``deadline`` (a ``time.monotonic`` instant), merged into one
    envelope. A project whose run failed leaves its clusters ungraded and is
    named in the second value; a project the budget did not reach, or cut
    short under a shrunk timeout, is named in the third with the phrase that
    says which, so the caller can tell a failed read from a read that never
    finished. All are read errors in the envelope, so the report names them.
    ``last_runs`` is stamped for a run that completed or hit its own cap, so
    the least recently run projects go first next time."""
    # Member keys carry the id the report resolved; the run is asked for by the
    # roster's spelling where one is known for that id.
    spelled = {resolved: given for given, resolved in ids.items()}
    needed = {project_of(key) for version in due for key in pending[version]}
    merged = empty_envelope()
    failures: dict[str, str] = {}
    unfinished: dict[str, str] = {}
    for project in ordered_projects(needed, last_runs):
        remaining = deadline - time.monotonic()
        if remaining < MIN_PROJECT_RUN_SECONDS:
            unfinished[project] = PROJECT_NOT_RUN_DETAIL.format(project=project, error=BUDGET_EXHAUSTED_DETAIL.format(project=project))
            continue
        timeout = min(READINESS_TIMEOUT_SECONDS, remaining)
        try:
            envelope = run_report([spelled.get(project, project)], readiness=True, timeout=timeout)
        except SandboxTimedOut as exc:
            if timeout < READINESS_TIMEOUT_SECONDS:
                # Killed by the budget, not by its own cap: not an attempt, not
                # stamped, so it goes first next time.
                unfinished[project] = PROJECT_CUT_SHORT_DETAIL.format(project=project)
                continue
            last_runs[project] = iso(now)
            failures[project] = str(exc)
            continue
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            last_runs[project] = iso(now)
            failures[project] = f"{type(exc).__name__}: {exc}" if not isinstance(exc, RuntimeError) else str(exc)
            continue
        last_runs[project] = iso(now)
        merge_envelope(merged, envelope)
    for project, error in failures.items():
        merged[ENVELOPE_REPORT_KEY][ERRORS_KEY].append({MEMBER_ID_KEYS[0]: project, MESSAGE_KEY: PROJECT_RUN_FAILED_DETAIL.format(project=project, error=error)})
    for project, detail in unfinished.items():
        merged[ENVELOPE_REPORT_KEY][ERRORS_KEY].append({MEMBER_ID_KEYS[0]: project, MESSAGE_KEY: detail})
    return merged, failures, unfinished


def cluster_names(clusters: list[str]) -> str:
    names = [key.rsplit(MEMBER_KEY_SEPARATOR, 1)[-1] for key in clusters]
    shown = ", ".join(names[:MAX_NAMES_IN_LINE])
    if len(names) > MAX_NAMES_IN_LINE:
        shown += NAMES_OVERFLOW.format(more=len(names) - MAX_NAMES_IN_LINE)
    return shown


def tables_without_the_output_line(tables: str) -> str:
    """The skill's tables as printed, minus the line naming the loader's
    temporary output path, which is gone by the time anyone reads this."""
    return "\n".join(line for line in tables.rstrip().splitlines() if not line.startswith(WROTE_LINE_PREFIX))


def render_markdown(version: str, reason: str, clusters: list[str], envelope: dict, now: datetime, next_phrase: str) -> str:
    report = envelope[ENVELOPE_REPORT_KEY]
    blocked, ready, unknown, unread = readiness_verdicts(report, clusters)
    lines = [
        f"# Upgrade readiness for {version}",
        "",
        f"Produced {iso(now)} by the `upgrade-readiness-watch` job: {reason}. "
        f"{len(clusters)} cluster(s) are below this version: {', '.join(clusters)}. "
        f"Of those the readiness check graded {len(blocked)} blocked"
        + (f" ({', '.join(blocked)})" if blocked else "")
        + f", {len(ready)} ready"
        + (f", {len(unknown)} unknown ({', '.join(unknown)}; the table says what it could not decide)" if unknown else "")
        + (f" and {len(unread)} not read ({', '.join(unread)}; their kubectl read failed or the run returned nothing for them)" if unread else "")
        + ". "
        f"{next_phrase}; ask the Platform Agent for the report at any time to refresh it sooner.",
        "",
        "The tables below are what `fleet_upgrade_report.py --readiness` printed. A `blocked` member names what "
        "blocks it; fix that before scheduling the upgrade. A member graded on this version's channel default "
        "shows `channel default` in its target column. The progress table is a first-run baseline: each "
        "scheduled run starts from an empty rollout record, so it does not compare with the previous report.",
        "",
        "```",
        tables_without_the_output_line(envelope.get(ENVELOPE_TABLES_KEY, "")),
        "```",
        "",
    ]
    errors = version_slice(report, clusters)[ERRORS_KEY]
    if errors:
        lines.append("Reads that failed during this run, and so are not graded:")
        lines.append("")
        for error in errors:
            where = MEMBER_KEY_SEPARATOR.join(str(error.get(k)) for k in MEMBER_ID_KEYS if error.get(k))
            lines.append(f"- {where}: {error.get(MESSAGE_KEY, '')}")
        lines.append("")
    return "\n".join(lines)


def version_slice(report: dict, clusters: list[str]) -> dict:
    """The merged report cut down to one version: its pending clusters' members
    and the read errors of their projects. The tables stay whole in the
    Markdown, since the skill prints them per project."""
    wanted = set(clusters)
    projects = {project_of(key) for key in clusters}
    return {
        MEMBERS_KEY: [m for m in report.get(MEMBERS_KEY) or [] if member_key(m) in wanted],
        ERRORS_KEY: [e for e in report.get(ERRORS_KEY) or [] if e.get(MEMBER_ID_KEYS[0]) in projects],
    }


def prune_reports(directory: Path) -> None:
    """Keep the newest REPORTS_KEPT_PER_VERSION dated pairs; the stamps sort by time."""
    stamps = sorted({path.name.rsplit(".", 1)[0] for path in directory.iterdir() if path.name != LATEST_LINK_NAME and not path.is_symlink()})
    for stamp in stamps[:-REPORTS_KEPT_PER_VERSION]:
        for suffix in (REPORT_MARKDOWN_SUFFIX, REPORT_JSON_SUFFIX):
            (directory / (stamp + suffix)).unlink(missing_ok=True)


def write_report(home: Path, version: str, reason: str, clusters: list[str], envelope: dict, now: datetime, next_phrase: str) -> Path:
    directory = home / REPORTS_DIR_NAME / version
    directory.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime(REPORT_TIMESTAMP_FORMAT)
    markdown = directory / (stamp + REPORT_MARKDOWN_SUFFIX)
    markdown.write_text(render_markdown(version, reason, clusters, envelope, now, next_phrase), encoding="utf-8")
    (directory / (stamp + REPORT_JSON_SUFFIX)).write_text(
        json.dumps(version_slice(envelope[ENVELOPE_REPORT_KEY], clusters), indent=JSON_INDENT) + "\n", encoding="utf-8"
    )
    latest = directory / LATEST_LINK_NAME
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(markdown.name)
    prune_reports(directory)
    return markdown


def remove_reports(home: Path, version: str) -> None:
    """A retired version's directory goes with it; nothing reads it afterwards."""
    shutil.rmtree(home / REPORTS_DIR_NAME / version, ignore_errors=True)


def clear_failure_marker(home: Path) -> bool:
    """Remove the last failure's marker if there is one; True when one was
    removed, so the caller announces the recovery. A marker that cannot be
    removed stays, and the recovery is announced once it can be."""
    marker = home / FAILURE_MARKER_FILE_NAME
    try:
        if not marker.exists():
            return False
        marker.unlink()
    except OSError:
        return False
    return True


# --- the tick --------------------------------------------------------------


def tick(dry_run: bool = False) -> list[str]:
    started = time.monotonic()
    now = now_utc()
    days = refresh_days()
    home = watch_home()
    ledger_path = home / LEDGER_FILE_NAME
    ledger = load_ledger(ledger_path)
    names = projects()
    deadline = started + TICK_BUDGET_SECONDS
    versions = versions_by_project(names, started + TABLE_BUDGET_SECONDS, ledger[TABLE_RUNS_KEY], now, ledger[PROJECT_IDS_KEY])
    canonicalise_members(versions[ENVELOPE_REPORT_KEY], ledger[PROJECT_IDS_KEY])
    read_errors = versions[ENVELOPE_REPORT_KEY].get(ERRORS_KEY) or []
    # A table is partial when a read failed; the script's exit code alone (it
    # exits 1 on a project it had to spell by number) does not make it one.
    complete = not read_errors
    pending = pending_targets(versions[ENVELOPE_REPORT_KEY], ledger[PROJECT_IDS_KEY])
    prune_read_projects(ledger, pending, names, read_errors, ledger[PROJECT_IDS_KEY])
    carry_forward_unlisted(pending, ledger, read_errors)
    if dry_run:
        due, would_retire = decide(ledger, pending, now, days, retire=complete, dry_run=True)
        lines = [DRY_RUN_WOULD_RETIRE.format(version=v) for v in would_retire]
        for version, reason in due.items():
            lines.append(DRY_RUN_WOULD_REPORT.format(version=version, reason=reason, names=cluster_names(pending[version])))
        if not due:
            lines.append(DRY_RUN_NOTHING_DUE.format(versions=", ".join(sorted(pending)) or NONE_WORD))
        return lines
    due, retired = decide(ledger, pending, now, days, retire=complete)
    lines = [RETIRED_LINE.format(prefix=LINE_PREFIX, version=v) for v in retired]
    for version in retired:
        remove_reports(home, version)
    signature = partial_signature(read_errors) if not complete else None
    already = announced(ledger).get(ANNOUNCED_PARTIAL_KEY)
    if signature and signature != already:
        lines.append(PARTIAL_LINE.format(prefix=LINE_PREFIX, errors=len(read_errors), code=versions.get(ENVELOPE_EXIT_KEY)))
    elif complete and already:
        lines.append(PARTIAL_CLEARED_LINE.format(prefix=LINE_PREFIX))
    announced(ledger)[ANNOUNCED_PARTIAL_KEY] = signature
    if due:
        readiness, failures, unfinished = readiness_by_project(pending, due, deadline, ledger[READINESS_RUNS_KEY], now, ledger[PROJECT_IDS_KEY])
        canonicalise_members(readiness[ENVELOPE_REPORT_KEY], ledger[PROJECT_IDS_KEY])
        for version in settle_from_readiness(readiness[ENVELOPE_REPORT_KEY], pending, ledger):
            if version in due and version in ledger[TARGETS_KEY]:
                del ledger[TARGETS_KEY][version]
                due.pop(version)
                retired.append(version)
                remove_reports(home, version)
                lines.append(RETIRED_LINE.format(prefix=LINE_PREFIX, version=version))
        for version, reason in due.items():
            clusters = pending[version]
            blocked, ready, unknown, unread = readiness_verdicts(readiness[ENVELOPE_REPORT_KEY], clusters)
            version_projects = {project_of(k) for k in clusters}
            failed_projects = sorted(version_projects & set(failures))
            unrun_projects = sorted(version_projects & set(unfinished))
            if not blocked and not ready and not unknown and unrun_projects and not failed_projects and len(unrun_projects) == len(version_projects):
                # Nothing was attempted for this version: not a report, not an
                # ungraded attempt, and not recorded, so it is due again tomorrow,
                # when the sweep starts at the project the budget left.
                lines.append(
                    NOT_RUN_LINE.format(
                        prefix=LINE_PREFIX, reason=reason, version=version, pending=len(clusters),
                        names=cluster_names(clusters), projects=", ".join(unrun_projects),
                    )
                )
                continue
            failure_text = "; ".join(
                [PROJECT_RUN_FAILED_DETAIL.format(project=p, error=failures[p]) for p in failed_projects]
                + [unfinished[p] for p in unrun_projects]
            )
            graded = bool(blocked or ready or unknown)
            ungraded_announced = announced(ledger).setdefault(ANNOUNCED_UNGRADED_KEY, {})
            previous = ungraded_announced.get(version) or {}
            if graded:
                next_phrase = NEXT_REFRESH_PHRASE.format(date=(now + timedelta(days=days)).strftime(DATE_FORMAT))
            else:
                detail = failure_text or READS_FAILED_DETAIL
                attempts = (previous.get(ATTEMPTS_KEY) or 0) + 1
                # Once parked, a version stays on the weekly cadence until graded:
                # the ladder of daily retries runs once, not once a week.
                parked = previous.get(PARKED_KEY, False) or attempts >= UNGRADED_ATTEMPTS_BEFORE_WEEKLY
                next_phrase = NEXT_WEEKLY_PHRASE.format(attempts=attempts) if parked else NEXT_TOMORROW_PHRASE
            try:
                path = write_report(home, version, reason, clusters, readiness, now, next_phrase)
            except OSError as exc:
                raise WriteFailed(str(exc)) from exc
            if not graded:
                if parked:
                    lines.append(
                        UNGRADED_PARKED_LINE.format(
                            prefix=LINE_PREFIX, reason=reason, version=version, pending=len(clusters),
                            names=cluster_names(clusters), detail=detail, attempts=attempts, path=path,
                        )
                    )
                    ledger[TARGETS_KEY][version][LAST_REPORT_KEY] = iso(now)
                elif previous.get(DETAIL_KEY) != detail:
                    lines.append(
                        UNGRADED_LINE.format(
                            prefix=LINE_PREFIX, reason=reason, version=version, pending=len(clusters),
                            names=cluster_names(clusters), detail=detail, path=path,
                        )
                    )
                ungraded_announced[version] = {DETAIL_KEY: detail, ATTEMPTS_KEY: attempts, PARKED_KEY: parked}
                continue
            announced(ledger).get(ANNOUNCED_UNGRADED_KEY, {}).pop(version, None)
            ledger[TARGETS_KEY][version][LAST_REPORT_KEY] = iso(now)
            lines.append(
                REPORT_LINE.format(
                    prefix=LINE_PREFIX,
                    reason=reason,
                    version=version,
                    pending=len(clusters),
                    names=cluster_names(clusters),
                    blocked=len(blocked),
                    blocked_names=BLOCKED_NAMES.format(names=cluster_names(blocked)) if blocked else "",
                    ready=len(ready),
                    ungraded=(UNKNOWN_COUNT.format(count=len(unknown)) if unknown else "")
                    + (UNREAD_COUNT.format(count=len(unread)) if unread else ""),
                    path=path,
                    next_date=(now + timedelta(days=days)).strftime(DATE_FORMAT),
                )
                + (PROJECT_FAILURES_SUFFIX.format(failures=failure_text) if failure_text else "")
            )
    for version in retired:
        announced(ledger).get(ANNOUNCED_UNGRADED_KEY, {}).pop(version, None)
    ledger[LAST_TICK_KEY] = iso(now)
    # The marker goes before the ledger is saved: a marker that cannot be
    # removed must not cost the lines a saved ledger has already recorded.
    recovered = clear_failure_marker(home)
    try:
        save_ledger(ledger_path, ledger)
    except OSError as exc:
        raise WriteFailed(str(exc)) from exc
    if recovered:
        lines.insert(0, RECOVERED_LINE.format(prefix=LINE_PREFIX))
    return lines


def announce_failure_once(detail: str) -> bool:
    """A failure that persists is posted once per distinct detail and the next
    clean tick posts a recovery line. The marker lives beside the ledger, which
    a failed tick never writes; a marker that cannot be read or written leaves
    the line posted every time, since nothing can remember it."""
    try:
        marker = watch_home() / FAILURE_MARKER_FILE_NAME
        if marker.exists() and marker.read_text(encoding="utf-8") == detail:
            return False
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(detail, encoding="utf-8")
    except (OSError, RuntimeError):
        pass
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Produce the upgrade readiness report when a new GKE version is pending.")
    parser.add_argument("--dry-run", action="store_true", help="Read the versions and the ledger, print what would be reported, change nothing.")
    args = parser.parse_args(argv)
    try:
        lines = tick(dry_run=args.dry_run)
    except WriteFailed as exc:
        print(FAILED_LINE.format(prefix=LINE_PREFIX, what=FAILED_WHAT_WRITE, detail=exc))
        return WRITE_FAILED_EXIT
    except Exception as exc:  # noqa: BLE001 - one line to chat, never a traceback
        detail = f"{type(exc).__name__}: {exc}"
        # A dry run changes nothing, the marker included, and is never silenced
        # by it: the operator running one is looking at the failure.
        if args.dry_run or announce_failure_once(detail):
            print(FAILED_LINE.format(prefix=LINE_PREFIX, what=FAILED_WHAT_TICK, detail=detail))
        return 0
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
