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
it, and a version no cluster is pending any more is retired from the ledger,
but only on a tick whose version table read every project: a partial table
retires nothing, so a failed listing cannot erase a version and have it come
back as new. A report whose readiness reads graded none of a version's
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
# whole tick stays under this, so the readiness runs get what the version
# table left, each capped at READINESS_TIMEOUT_SECONDS, and a project the
# budget cannot reach is left unread and named rather than started.
TICK_BUDGET_SECONDS = 2700
MIN_PROJECT_RUN_SECONDS = 120
BUDGET_EXHAUSTED_DETAIL = "tick budget exhausted before project {project} ran; retried tomorrow"
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
PARTIAL_LINE = "{prefix} watch: the version table was partial ({errors} read error(s), exit {code}); nothing retired while it stays so"
PARTIAL_CLEARED_LINE = "{prefix} watch: the version table reads every project again"
DRY_RUN_WOULD_RETIRE = "dry run: would retire {version} (no cluster is pending it)"
UNGRADED_LINE = (
    "{prefix}: {reason} {version}, {pending} cluster(s) pending ({names}): none graded ({detail}); "
    "report on the gateway pod at {path}; retrying tomorrow"
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
DRY_RUN_WOULD_REPORT = "dry run: would report {version} ({reason}) for {names}"
DRY_RUN_NOTHING_DUE = "dry run: nothing due; pending versions: {versions}"
NONE_WORD = "none"
MAX_NAMES_IN_LINE = 6
NAMES_OVERFLOW = ", +{more} more"
WRITE_FAILED_EXIT = 2
DATE_FORMAT = "%Y-%m-%d"


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
        raise RuntimeError(TIMED_OUT_DETAIL.format(seconds=timeout)) from None
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


def pending_targets(report: dict) -> dict[str, list[str]]:
    """Target version -> the clusters below it, from the report's members."""
    pending: dict[str, list[str]] = {}
    for member in report.get(MEMBERS_KEY) or []:
        target = member.get(TARGET_KEY)
        if not target or member.get(STATUS_KEY) not in BEHIND_STATUSES:
            continue
        pending.setdefault(target, []).append(member_key(member))
    return {version: sorted(keys) for version, keys in pending.items()}


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
    return {LEDGER_SCHEMA_KEY: LEDGER_SCHEMA_VERSION, TARGETS_KEY: {}, LAST_TICK_KEY: None, ANNOUNCED_KEY: {}}


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
            and VERSION_KEY_RE.match(version) is not None
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
    ledger: dict, pending: dict[str, list[str]], now: datetime, days: int, retire: bool = True
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
    retired = sorted(version for version in targets if version not in pending) if retire else []
    for version in retired:
        del targets[version]
    return due, retired


# --- the report files ------------------------------------------------------


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


def readiness_by_project(
    pending: dict[str, list[str]], due: dict[str, str], deadline: float, rotation: int = 0
) -> tuple[dict, dict[str, str]]:
    """One readiness run per project that holds a due version's pending
    clusters, each capped by its own timeout and by what is left of the tick's
    budget before ``deadline`` (a ``time.monotonic`` instant), merged into one
    envelope; a project whose run failed, or that the budget could not reach,
    leaves its clusters ungraded and is named in the second value, so the
    other projects' versions still get their report. ``rotation`` (the day
    number) turns the sorted order so a budget that never reaches the last
    project does not leave the same project unread every day."""
    needed = sorted({project_of(key) for version in due for key in pending[version]})
    if needed:
        start = rotation % len(needed)
        needed = needed[start:] + needed[:start]
    merged = {ENVELOPE_EXIT_KEY: EXIT_OK, ENVELOPE_TABLES_KEY: "", ENVELOPE_REPORT_KEY: {MEMBERS_KEY: [], ERRORS_KEY: []}}
    failures: dict[str, str] = {}
    for project in needed:
        remaining = deadline - time.monotonic()
        if remaining < MIN_PROJECT_RUN_SECONDS:
            failures[project] = BUDGET_EXHAUSTED_DETAIL.format(project=project)
            continue
        try:
            envelope = run_report([project], readiness=True, timeout=min(READINESS_TIMEOUT_SECONDS, remaining))
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            failures[project] = f"{type(exc).__name__}: {exc}" if not isinstance(exc, RuntimeError) else str(exc)
            continue
        merged[ENVELOPE_TABLES_KEY] += envelope.get(ENVELOPE_TABLES_KEY, "")
        merged[ENVELOPE_REPORT_KEY][MEMBERS_KEY] += envelope[ENVELOPE_REPORT_KEY].get(MEMBERS_KEY) or []
        merged[ENVELOPE_REPORT_KEY][ERRORS_KEY] += envelope[ENVELOPE_REPORT_KEY].get(ERRORS_KEY) or []
    for project, error in failures.items():
        merged[ENVELOPE_REPORT_KEY][ERRORS_KEY].append({MEMBER_ID_KEYS[0]: project, MESSAGE_KEY: PROJECT_RUN_FAILED_DETAIL.format(project=project, error=error)})
    return merged, failures


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


def render_markdown(version: str, reason: str, clusters: list[str], envelope: dict, now: datetime, days: int) -> str:
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
        f"The next scheduled refresh is after {(now + timedelta(days=days)).strftime(DATE_FORMAT)} "
        "while any cluster is still pending; ask the Platform Agent for the report at any time to refresh it sooner.",
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
    errors = report.get(ERRORS_KEY) or []
    if errors:
        lines.append("Reads that failed during this run, and so are not graded:")
        lines.append("")
        for error in errors:
            where = MEMBER_KEY_SEPARATOR.join(str(error.get(k)) for k in MEMBER_ID_KEYS if error.get(k))
            lines.append(f"- {where}: {error.get(MESSAGE_KEY, '')}")
        lines.append("")
    return "\n".join(lines)


def write_report(home: Path, version: str, reason: str, clusters: list[str], envelope: dict, now: datetime, days: int) -> Path:
    directory = home / REPORTS_DIR_NAME / version
    directory.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime(REPORT_TIMESTAMP_FORMAT)
    markdown = directory / (stamp + REPORT_MARKDOWN_SUFFIX)
    markdown.write_text(render_markdown(version, reason, clusters, envelope, now, days), encoding="utf-8")
    (directory / (stamp + REPORT_JSON_SUFFIX)).write_text(
        json.dumps(envelope[ENVELOPE_REPORT_KEY], indent=JSON_INDENT) + "\n", encoding="utf-8"
    )
    latest = directory / LATEST_LINK_NAME
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(markdown.name)
    return markdown


# --- the tick --------------------------------------------------------------


def tick(dry_run: bool = False) -> list[str]:
    started = time.monotonic()
    now = now_utc()
    days = refresh_days()
    home = watch_home()
    ledger_path = home / LEDGER_FILE_NAME
    ledger = load_ledger(ledger_path)
    names = projects()
    versions = run_report(names, readiness=False)
    read_errors = versions[ENVELOPE_REPORT_KEY].get(ERRORS_KEY) or []
    complete = versions.get(ENVELOPE_EXIT_KEY) == EXIT_OK and not read_errors
    pending = pending_targets(versions[ENVELOPE_REPORT_KEY])
    carry_forward_unlisted(pending, ledger, read_errors)
    if dry_run:
        due, _ = decide(ledger, pending, now, days, retire=False)
        lines = [DRY_RUN_WOULD_RETIRE.format(version=v) for v in sorted(ledger[TARGETS_KEY]) if v not in pending and complete]
        for version, reason in due.items():
            lines.append(DRY_RUN_WOULD_REPORT.format(version=version, reason=reason, names=cluster_names(pending[version])))
        if not due:
            lines.append(DRY_RUN_NOTHING_DUE.format(versions=", ".join(sorted(pending)) or NONE_WORD))
        return lines
    due, retired = decide(ledger, pending, now, days, retire=complete)
    lines = [RETIRED_LINE.format(prefix=LINE_PREFIX, version=v) for v in retired]
    partial_signature = json.dumps(sorted(json.dumps(e, sort_keys=True) for e in read_errors)) if not complete else None
    already = announced(ledger).get(ANNOUNCED_PARTIAL_KEY)
    if partial_signature and partial_signature != already:
        lines.append(PARTIAL_LINE.format(prefix=LINE_PREFIX, errors=len(read_errors), code=versions.get(ENVELOPE_EXIT_KEY)))
    elif complete and already:
        lines.append(PARTIAL_CLEARED_LINE.format(prefix=LINE_PREFIX))
    announced(ledger)[ANNOUNCED_PARTIAL_KEY] = partial_signature
    if due:
        readiness, failures = readiness_by_project(pending, due, started + TICK_BUDGET_SECONDS, rotation=now.toordinal())
        for version, reason in due.items():
            clusters = pending[version]
            try:
                path = write_report(home, version, reason, clusters, readiness, now, days)
            except OSError as exc:
                raise WriteFailed(str(exc)) from exc
            blocked, ready, unknown, unread = readiness_verdicts(readiness[ENVELOPE_REPORT_KEY], clusters)
            failed_projects = sorted({project_of(k) for k in clusters} & set(failures))
            failure_text = "; ".join(PROJECT_RUN_FAILED_DETAIL.format(project=p, error=failures[p]) for p in failed_projects)
            if not blocked and not ready and not unknown:
                detail = failure_text or READS_FAILED_DETAIL
                ungraded_announced = announced(ledger).setdefault(ANNOUNCED_UNGRADED_KEY, {})
                previous = ungraded_announced.get(version) or {}
                attempts = (previous.get(ATTEMPTS_KEY) or 0) + 1
                # Once parked, a version stays on the weekly cadence until graded:
                # the ladder of daily retries runs once, not once a week.
                parked = previous.get(PARKED_KEY, False) or attempts >= UNGRADED_ATTEMPTS_BEFORE_WEEKLY
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
    try:
        save_ledger(ledger_path, ledger)
    except OSError as exc:
        raise WriteFailed(str(exc)) from exc
    marker = home / FAILURE_MARKER_FILE_NAME
    if marker.exists():
        lines.insert(0, RECOVERED_LINE.format(prefix=LINE_PREFIX))
        marker.unlink()
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
        if announce_failure_once(detail):
            print(FAILED_LINE.format(prefix=LINE_PREFIX, what=FAILED_WHAT_TICK, detail=detail))
        return 0
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
