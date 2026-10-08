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
After that the report is refreshed every ``REFRESH_DAYS_DEFAULT`` days while a
cluster is still pending it, and a version no cluster is pending any more is
retired from the ledger. A tick with nothing due prints nothing.

Where it runs, and how. The agent container carries no ``gcloud`` or
``kubectl``; both live in the shell sandbox behind the credential proxy, and
the sandbox login never executes a file under the agent-owned ``/opt/data``
(``deploy/sandbox/Dockerfile``). So the skill's two scripts are read here, from
the agent image's copy under ``/opt/platform-template``, and handed to
``python3 -I -`` on the command's stdin wrapped in a small loader that
registers ``upgrade_readiness`` as a module before running
``fleet_upgrade_report.main``: the same route ``stall_watch.py`` takes with
``stall_report.py``. ``-I`` keeps the sandbox's working directory off the
module path. The sandbox's ``/opt/data`` is a different directory from this
pod's, so the loader prints the report back as JSON on stdout and this job
writes the files on its own side.

What it writes. Under ``<agent home>/upgrade-readiness/``: ``ledger.json``,
one entry per target version with when it was first seen, when it was last
reported and which clusters are pending it; and ``reports/<version>/
<timestamp>.md`` with the tables the skill prints and a header saying why the
report was produced, beside the same report as ``.json``. ``<agent home>`` is
``HERMES_HOME``, the Platform Agent profile when the roster runs this job,
which is on the data volume and survives a pod restart. The report script's
own rollout record (``--state-dir``) and kubeconfigs (``--kubeconfig-dir``) go
to directories of this job's own in the sandbox, so a scheduled run never
rewrites the record a user's own ``fleet_upgrade_report.py`` run compares
against, and the kubeconfigs stay where the model cannot read them.

Stdout is the chat message (``deliver: "chat"``): one line per report
produced, naming the target version, how many clusters are pending it, how
many the report graded blocked and ready, where the report is, and when the
next refresh is due; one line per version retired; and one line when a tick
fails. A quiet tick prints nothing, and nothing reaches chat. Exit code is 0
on every path except a ledger that cannot be saved, because a tick whose
ledger did not save would report the same version as new again tomorrow.

``UPGRADE_READINESS_REFRESH_DAYS`` overrides the refresh interval,
``UPGRADE_READINESS_WATCH_HOME`` the directory the ledger and reports live in,
and ``UPGRADE_READINESS_PROJECTS`` (comma-separated) the projects the report
enumerates; without it the report script's own project discovery applies,
``MONITORED_PROJECT_IDS`` first, then every project ``gcloud projects list``
returns. ``--dry-run`` runs the version table and the gate and prints what a
real tick would do without running the readiness report or touching the
ledger.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sandbox_exec  # noqa: E402

# Where the job keeps its ledger and reports, under the agent home.
HOME_ENV = "HERMES_HOME"
DEFAULT_HOME = "/opt/data"
WATCH_HOME_ENV = "UPGRADE_READINESS_WATCH_HOME"
WATCH_DIR_NAME = "upgrade-readiness"
LEDGER_FILE_NAME = "ledger.json"
LEDGER_TMP_SUFFIX = ".tmp"
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
PROJECTS_ENV = "UPGRADE_READINESS_PROJECTS"
PROJECTS_SEPARATOR = ","

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
SANDBOX_OUTPUT_PATH = "/tmp/upgrade-readiness-watch/report.json"
SANDBOX_STATE_DIR = "/tmp/upgrade-readiness-watch/state"
SANDBOX_KUBECONFIG_DIR = "/home/hermes/.kubeconfigs/upgrade-readiness-watch"
ENVELOPE_SENTINEL = "__UPGRADE_READINESS_WATCH_ENVELOPE__"
VERSION_TABLE_TIMEOUT_SECONDS = 600
READINESS_TIMEOUT_SECONDS = 1500
STDERR_EXCERPT_CHARS = 300

# The report's vocabulary this job reads (fleet_upgrade_report.py, upgrade_readiness.py).
MEMBERS_KEY = "members"
STATUS_KEY = "status"
TARGET_KEY = "target_version"
READINESS_KEY = "readiness"
ERRORS_KEY = "errors"
BEHIND_STATUSES = frozenset({"lagging", "patch-behind"})
READINESS_BLOCKED = "blocked"
READINESS_READY = "ready"
MEMBER_KEY_SEPARATOR = "/"

# Ledger vocabulary.
TARGETS_KEY = "targets"
FIRST_SEEN_KEY = "first_seen"
LAST_REPORT_KEY = "last_report_at"
PENDING_KEY = "pending"
LAST_TICK_KEY = "last_tick"
REASON_NEW = "new target version"
REASON_REFRESH = "weekly refresh"

# Output.
LINE_PREFIX = "upgrade readiness"
RETIRED_LINE = "{prefix}: {version} is no longer pending on any cluster; retired from the watch"
REPORT_LINE = (
    "{prefix}: {reason} {version}, {pending} cluster(s) pending ({names}): {blocked} blocked, {ready} ready; "
    "report at {path}; next refresh after {next_date}"
)
FAILED_LINE = "{prefix} watch: {what} failed: {detail}"
DRY_RUN_PREFIX = "dry run:"
MAX_NAMES_IN_LINE = 6
NAMES_OVERFLOW = ", +{more} more"
LEDGER_UNSAVED_EXIT = 2
DATE_FORMAT = "%Y-%m-%d"


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(moment: datetime) -> str:
    return moment.isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
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
    return value if value > 0 else REFRESH_DAYS_DEFAULT


def watch_home() -> Path:
    override = os.environ.get(WATCH_HOME_ENV, "").strip()
    if override:
        return Path(override)
    return Path(os.environ.get(HOME_ENV, DEFAULT_HOME)) / WATCH_DIR_NAME


def projects_argument() -> list[str]:
    raw = os.environ.get(PROJECTS_ENV, "")
    projects = [p.strip() for p in raw.split(PROJECTS_SEPARATOR) if p.strip()]
    argv: list[str] = []
    for project in projects:
        argv += ["--project", project]
    return argv


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
    ``import upgrade_readiness`` resolves, then ``main`` with ``argv``, its
    tables captured, and one JSON envelope printed after a sentinel line."""
    scripts = skill_scripts_dir()
    readiness_src = (scripts / (READINESS_MODULE + PYTHON_SOURCE_SUFFIX)).read_text(encoding="utf-8")
    report_src = (scripts / (REPORT_MODULE + PYTHON_SOURCE_SUFFIX)).read_text(encoding="utf-8")
    return "\n".join(
        [
            "import contextlib, io, json, os, sys, types",
            f"READINESS_SRC = {json.dumps(readiness_src)}",
            f"REPORT_SRC = {json.dumps(report_src)}",
            f"ARGV = {json.dumps(argv)}",
            f"OUTPUT = {json.dumps(SANDBOX_OUTPUT_PATH)}",
            f"SENTINEL = {json.dumps(ENVELOPE_SENTINEL)}",
            f"readiness = types.ModuleType({json.dumps(READINESS_MODULE)})",
            f"readiness.__file__ = {json.dumps(str(scripts / (READINESS_MODULE + PYTHON_SOURCE_SUFFIX)))}",
            f"exec(compile(READINESS_SRC, readiness.__file__, 'exec'), readiness.__dict__)",
            f"sys.modules[{json.dumps(READINESS_MODULE)}] = readiness",
            f"report = types.ModuleType({json.dumps(REPORT_MODULE)})",
            f"report.__file__ = {json.dumps(str(scripts / (REPORT_MODULE + PYTHON_SOURCE_SUFFIX)))}",
            "exec(compile(REPORT_SRC, report.__file__, 'exec'), report.__dict__)",
            "os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)",
            "tables = io.StringIO()",
            "with contextlib.redirect_stdout(tables):",
            "    code = report.main(ARGV)",
            "data = None",
            "if os.path.exists(OUTPUT):",
            "    with open(OUTPUT, encoding='utf-8') as handle:",
            "        data = json.load(handle)",
            "    os.remove(OUTPUT)",
            "print(SENTINEL)",
            "print(json.dumps({'exit': code, 'tables': tables.getvalue(), 'report': data}))",
            "",
        ]
    )


def report_argv(readiness: bool) -> list[str]:
    argv = ["--output", SANDBOX_OUTPUT_PATH, "--state-dir", SANDBOX_STATE_DIR] + projects_argument()
    if readiness:
        argv += ["--readiness", "--kubeconfig-dir", SANDBOX_KUBECONFIG_DIR]
    return argv


def run_report(readiness: bool) -> dict:
    """Run the skill's report in the sandbox and return the envelope:
    ``{"exit": int, "tables": str, "report": dict | None}``."""
    argv = report_argv(readiness)
    timeout = READINESS_TIMEOUT_SECONDS if readiness else VERSION_TABLE_TIMEOUT_SECONDS
    completed = sandbox_exec.run(
        [PYTHON_EXECUTABLE, PYTHON_ISOLATED_FLAG, STDIN_SCRIPT_ARG],
        timeout=timeout,
        check=False,
        stdin=loader_source(argv),
    )
    stdout = completed.stdout or ""
    if ENVELOPE_SENTINEL not in stdout:
        detail = " ".join((completed.stderr or stdout).split())[:STDERR_EXCERPT_CHARS]
        raise RuntimeError(f"sandbox exited {completed.returncode} without a report: {detail}")
    envelope = json.loads(stdout.rsplit(ENVELOPE_SENTINEL, 1)[1].strip())
    if not isinstance(envelope.get("report"), dict):
        raise RuntimeError(f"report script exited {envelope.get('exit')} and wrote no report")
    return envelope


# --- the gate --------------------------------------------------------------


def member_key(member: dict) -> str:
    return MEMBER_KEY_SEPARATOR.join(str(member.get(k, "")) for k in ("project", "location", "cluster"))


def pending_targets(report: dict) -> dict[str, list[str]]:
    """Target version -> the clusters below it, from the report's members."""
    pending: dict[str, list[str]] = {}
    for member in report.get(MEMBERS_KEY) or []:
        target = member.get(TARGET_KEY)
        if not target or member.get(STATUS_KEY) not in BEHIND_STATUSES:
            continue
        pending.setdefault(target, []).append(member_key(member))
    return {version: sorted(keys) for version, keys in pending.items()}


def empty_ledger() -> dict:
    return {"schema_version": LEDGER_SCHEMA_VERSION, TARGETS_KEY: {}, LAST_TICK_KEY: None}


def load_ledger(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty_ledger()
    if not isinstance(data, dict) or not isinstance(data.get(TARGETS_KEY), dict):
        raise RuntimeError(f"ledger at {path} is not this job's ledger; refusing to overwrite it")
    return data


def save_ledger(path: Path, ledger: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + LEDGER_TMP_SUFFIX)
    tmp.write_text(json.dumps(ledger, indent=JSON_INDENT, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def decide(ledger: dict, pending: dict[str, list[str]], now: datetime, days: int) -> tuple[dict[str, str], list[str]]:
    """Apply the gate. Returns (due: version -> reason, retired versions) and
    updates the ledger's targets in place: new versions get ``first_seen``,
    every pending version its current cluster list, retired versions go."""
    targets = ledger[TARGETS_KEY]
    due: dict[str, str] = {}
    for version, clusters in sorted(pending.items()):
        entry = targets.get(version)
        if entry is None:
            targets[version] = {FIRST_SEEN_KEY: iso(now), LAST_REPORT_KEY: None, PENDING_KEY: clusters}
            due[version] = REASON_NEW
            continue
        entry[PENDING_KEY] = clusters
        last = parse_iso(entry.get(LAST_REPORT_KEY))
        if last is None:
            due[version] = REASON_NEW
        elif now - last >= timedelta(days=days):
            due[version] = REASON_REFRESH
    retired = sorted(version for version in targets if version not in pending)
    for version in retired:
        del targets[version]
    return due, retired


# --- the report files ------------------------------------------------------


def readiness_counts(report: dict, clusters: list[str]) -> tuple[int, int]:
    blocked = ready = 0
    wanted = set(clusters)
    for member in report.get(MEMBERS_KEY) or []:
        if member_key(member) not in wanted:
            continue
        status = (member.get(READINESS_KEY) or {}).get(STATUS_KEY)
        if status == READINESS_BLOCKED:
            blocked += 1
        elif status == READINESS_READY:
            ready += 1
    return blocked, ready


def cluster_names(clusters: list[str]) -> str:
    names = [key.rsplit(MEMBER_KEY_SEPARATOR, 1)[-1] for key in clusters]
    shown = ", ".join(names[:MAX_NAMES_IN_LINE])
    if len(names) > MAX_NAMES_IN_LINE:
        shown += NAMES_OVERFLOW.format(more=len(names) - MAX_NAMES_IN_LINE)
    return shown


def render_markdown(version: str, reason: str, clusters: list[str], envelope: dict, now: datetime, days: int) -> str:
    report = envelope["report"]
    blocked, ready = readiness_counts(report, clusters)
    lines = [
        f"# Upgrade readiness for {version}",
        "",
        f"Produced {iso(now)} by the `upgrade-readiness-watch` job: {reason}. "
        f"{len(clusters)} cluster(s) are below this version: {', '.join(clusters)}. "
        f"Of those the readiness check graded {blocked} blocked and {ready} ready. "
        f"The next scheduled refresh is after {(now + timedelta(days=days)).strftime(DATE_FORMAT)} "
        "while any cluster is still pending; ask the Platform Agent for the report at any time to refresh it sooner.",
        "",
        "The tables below are what `fleet_upgrade_report.py --readiness` printed. A `blocked` member names what "
        "blocks it; fix that before scheduling the upgrade. A member graded on this version's channel default "
        "shows `channel default` in its target column.",
        "",
        "```",
        envelope.get("tables", "").rstrip(),
        "```",
        "",
    ]
    errors = report.get(ERRORS_KEY) or []
    if errors:
        lines.append("Reads that failed during this run, and so are not graded:")
        lines.append("")
        for error in errors:
            where = MEMBER_KEY_SEPARATOR.join(str(error.get(k)) for k in ("project", "location", "cluster") if error.get(k))
            lines.append(f"- {where}: {error.get('message', '')}")
        lines.append("")
    return "\n".join(lines)


def write_report(home: Path, version: str, reason: str, clusters: list[str], envelope: dict, now: datetime, days: int) -> Path:
    directory = home / REPORTS_DIR_NAME / version
    directory.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime(REPORT_TIMESTAMP_FORMAT)
    markdown = directory / (stamp + REPORT_MARKDOWN_SUFFIX)
    markdown.write_text(render_markdown(version, reason, clusters, envelope, now, days), encoding="utf-8")
    (directory / (stamp + REPORT_JSON_SUFFIX)).write_text(
        json.dumps(envelope["report"], indent=JSON_INDENT) + "\n", encoding="utf-8"
    )
    latest = directory / LATEST_LINK_NAME
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(markdown.name)
    return markdown


# --- the tick --------------------------------------------------------------


def tick(dry_run: bool = False) -> list[str]:
    now = now_utc()
    days = refresh_days()
    home = watch_home()
    ledger_path = home / LEDGER_FILE_NAME
    ledger = load_ledger(ledger_path)
    versions = run_report(readiness=False)
    pending = pending_targets(versions["report"])
    due, retired = decide(ledger, pending, now, days)
    lines = [RETIRED_LINE.format(prefix=LINE_PREFIX, version=v) for v in retired]
    if dry_run:
        for version, reason in due.items():
            lines.append(f"{DRY_RUN_PREFIX} would report {version} ({reason}) for {cluster_names(pending[version])}")
        if not due:
            lines.append(f"{DRY_RUN_PREFIX} nothing due; pending versions: {', '.join(sorted(pending)) or 'none'}")
        return lines
    if due:
        readiness = run_report(readiness=True)
        for version, reason in due.items():
            clusters = pending[version]
            path = write_report(home, version, reason, clusters, readiness, now, days)
            blocked, ready = readiness_counts(readiness["report"], clusters)
            ledger[TARGETS_KEY][version][LAST_REPORT_KEY] = iso(now)
            lines.append(
                REPORT_LINE.format(
                    prefix=LINE_PREFIX,
                    reason=reason,
                    version=version,
                    pending=len(clusters),
                    names=cluster_names(clusters),
                    blocked=blocked,
                    ready=ready,
                    path=path,
                    next_date=(now + timedelta(days=days)).strftime(DATE_FORMAT),
                )
            )
    ledger[LAST_TICK_KEY] = iso(now)
    save_ledger(ledger_path, ledger)
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Produce the upgrade readiness report when a new GKE version is pending.")
    parser.add_argument("--dry-run", action="store_true", help="Read the versions and the ledger, print what would be reported, change nothing.")
    args = parser.parse_args(argv)
    try:
        lines = tick(dry_run=args.dry_run)
    except OSError as exc:
        print(FAILED_LINE.format(prefix=LINE_PREFIX, what="saving the ledger or the report", detail=exc))
        return LEDGER_UNSAVED_EXIT
    except Exception as exc:  # noqa: BLE001 - one line to chat, never a traceback
        print(FAILED_LINE.format(prefix=LINE_PREFIX, what="the tick", detail=f"{type(exc).__name__}: {exc}"))
        return 0
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
