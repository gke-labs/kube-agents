#!/usr/bin/env python3
"""The Prow periodics that keep the pool in shape, read for the CI health bot.

The pull sweep and the two seeded-fleet reconciles run on the build cluster and
report nowhere but TestGrid. This module reads each one's latest finished build
from the bucket they log to, gs://kube-agents-periodic-logs (`latest-build.txt`,
then `finished.json`, then the report the job wrote, on every finished build) and turns a failed or overdue run into
a note health.py carries and post_health.py posts once, with the job's history
link and the report's detail: for the reconcile the projects it refused or
could not finish, for the sweep the projects whose sweep failed and what the run
left for the next one. A passed build's report becomes the one-line summary the
recovery message carries (`periodics_runs`).

    fetch --out-dir DIR      one <job>.json per watched periodic that has a
                             finished build; a job that has never run writes
                             nothing, and nothing here fails the tick.

Which jobs are watched is WATCHED below; adding a periodic is one entry.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

try:
    from . import collect
except ImportError:  # run as a script: python3 scripts/eval_dashboard/periodics.py
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import collect

# Where Prow's pod utilities publish a periodic's builds, and Deck's history
# page for one (the link every message carries).
# The bucket these jobs log to, not the Prow archive: they run under their
# own identities, which cannot write gs://kube-agents-prow.
LOGS_ROOT = "gs://kube-agents-periodic-logs/logs"
JOB_HISTORY_ROOT = "https://oss.gprow.dev/job-history/gs/kube-agents-periodic-logs/logs"
POINTER = "latest-build.txt"
FINISHED = "finished.json"
ARTIFACTS_DIR = "artifacts"
# The pointer moves at job start, so the newest build is often still running;
# the one before it has finished. Three covers a run of aborted builds.
FALLBACK_BUILDS = 3
# finished.json's result for a build Prow stopped (a drained node, a plank
# abort): not a run of the job, so the reader walks past it as it does a
# build with no finished.json.
RESULT_ABORTED = "ABORTED"
VERDICT_FAILED = "FAILED"
VERDICT_STALE = "STALE"
# How many refused or failed projects a message names before "and N more".
DETAIL_LIMIT = 5
# The reconcile's artifact (hack/fleet_reconcile.py --report) and the outcomes
# in it worth naming.
RECONCILE_ARTIFACT = "fleet-reconcile.json"
# The sweep's report (hack/ci_sweep_agent_pulls.py write_report).
SWEEP_ARTIFACT = "pull-sweep.json"
SWEEP_KEY_CLOSED = "closed"
SWEEP_KEY_PROJECTS = "projects"
SWEEP_KEY_FAILED = "failed"
SWEEP_KEY_LEFT = "left_for_next_run"
SWEEP_KEY_ENDED_EARLY = "ended_early"
SWEEP_KEY_SKIPPED = "skipped"
REPORT_KEY_EXIT = "exit"
REPORT_EXIT_OK = "ok"
REPORT_EXIT_FAILED = "failed"
# Where the runbook sections live, as a link a reader can click.
RUNBOOK_ROOT = "https://github.com/gke-labs/kube-agents/blob/main/"
# The scope line every message carries: none of this reaches a user cluster.
SCOPE_LINE = "CI eval infrastructure only."
# When a failing job is news. The sweep runs every ten minutes and the bot
# reads its latest finished build every fifteen, so the unit is the check, not
# the build (one sweep build in three is never read), and one failed check
# followed by a clean one is a flap: the sweep is said once it has failed this
# many consecutive checks. The reconciles run hourly and weekly: their first
# failed build is the news. Any project's failure fails a run, so a per-project
# threshold could never fire before the run's; the per-project counts name the
# projects that have failed in every one of those checks.
SWEEP_RUN_ALERT_AFTER = 2
FIRST_FAILURE = 1
# A project named as persisting in the message: failed in this many consecutive
# checks, this check included.
PERSISTENT_AFTER = 2
# health.json's `periodics_streaks`, per job: the last build counted, each
# project's consecutive failed checks (reset by a clean build), and the run's.
KEY_STREAK_BUILD = "build"
KEY_STREAK_PROJECTS = "projects"
KEY_STREAK_RUNS = "runs"
RECONCILE_NAMED_OUTCOMES = ("refused", "failed", "interrupted")
# gsutil's absent-object wording, the set scripts/release/poll_rc_eval_verdict.py
# settled on for the Prow archive; the same wording here. Never a bare 404,
# because gsutil echoes the failing URL and a 19-digit build id can contain
# those digits.
NOT_FOUND_PATTERNS = ("matched no objects", "no urls matched", "notfoundexception: 404")
# A NotFound that names the bucket rather than an object: every job reads as
# "never ran" for as long as it lasts, so it is warned, unlike an absent pointer.
BUCKET_MISSING_MARKER = "bucket does not exist"
# GitHub Actions renders a line with this prefix as a workflow warning, as the
# sibling pool-pressure step's `echo "::warning::..."` does.
WARNING_PREFIX = "::warning::"
# argparse's exit for a bad command line.
EXIT_USAGE = 2
# One reading file per job, written by `fetch` and read by `load_readings`.
READING_SUFFIX = ".json"
# The detail line for a report that is present but not a JSON object: written
# last, a signal can cut it, and no later tick can read it either.
REPORT_UNREADABLE = "report unreadable: not a JSON object"
SECONDS_PER_HOUR = 3600
KEY_JOB = "job"
KEY_BUILD = "build"
KEY_FINISHED_AT = "finished_at"
KEY_PASSED = "passed"
KEY_RESULT = "result"
KEY_ARTIFACT = "artifact"
KEY_SINCE = "since"
KEY_VERDICT = "verdict"
KEY_TIMESTAMP = "timestamp"
KEY_LABEL = "label"
KEY_STALE_AFTER_H = "stale_after_h"
KEY_DRY_RUN = "dry_run"
KEY_DETAIL = "detail"
KEY_HISTORY_URL = "history_url"
KEY_PLACE = "place"
KEY_ABSENCE = "absence"
KEY_DOES = "does"
KEY_EFFECT = "effect"
KEY_RUNBOOK = "runbook"
KEY_SUMMARY = "summary"
# The reconcile's summary counts (hack/fleet_reconcile.py write_report).
RECONCILE_KEY_SUMMARY = "summary"
# The reconcile's report (hack/fleet_reconcile.py write_report).
REPORT_KEY_OUTCOMES = "outcomes"
REPORT_KEY_OUTCOME = "outcome"
REPORT_KEY_DETAIL = "detail"
REPORT_KEY_ERROR = "error"


@dataclasses.dataclass(frozen=True)
class Periodic:
    """One watched job, and the words its messages are built from: where it
    acts (`place`), what stops happening when it fails (`absence`) and starts
    again when it recovers (`presence`), what it does and how often (`does`),
    what a failure costs (`effect`), and where the runbook section is."""

    job: str
    label: str
    # A finished build older than this is a job that stopped running. It is a
    # window, not the cadence: the sweep runs every ten minutes and the hourly
    # reconcile hourly. The sweep's and the hourly's windows are what their
    # TestGrid stale-results settings were; the weekly's is a week and a day.
    stale_after: timedelta
    artifact: str | None
    place: str
    absence: str
    presence: str
    does: str
    effect: str
    runbook: str
    # A failed build is a note once the job has failed this many consecutive
    # checks; FIRST_FAILURE for a job whose every run is news.
    run_alert_after: int = FIRST_FAILURE


SWEEP_DOES = "closes the pull requests the agent opened during eval runs in the pool projects' `kube-agents-evals[-<n>]-infra` repos"
SWEEP_EFFECT = "pull requests pile up in those repos, and eval cases that open one can link an old one and fail."
RECONCILE_EFFECT = "drifted fixtures stay drifted, and the eval cases that assert on them fail."
WATCHED = (
    Periodic(
        "ci-kube-agents-pull-sweep", "GitOps pull sweep", timedelta(hours=1), SWEEP_ARTIFACT,
        "Eval GitOps repos", "leftover pull requests from eval runs are not being cleaned up",
        "leftover pull requests from eval runs are being cleaned up again",
        f"runs every ten minutes and {SWEEP_DOES}", SWEEP_EFFECT,
        f"{RUNBOOK_ROOT}docs/ci-pool-projects.md#55-the-pull-request-sweep",
        SWEEP_RUN_ALERT_AFTER,
    ),
    Periodic(
        "ci-kube-agents-fleet-reconcile", "seeded-fleet reconcile (hourly)", timedelta(hours=3), RECONCILE_ARTIFACT,
        "Eval seeded fleet", "planted defects are not being re-applied", "planted defects are being re-applied again",
        "runs hourly and re-applies the seeded-fleet stack in the pool projects the scan reports drifted", RECONCILE_EFFECT,
        f"{RUNBOOK_ROOT}docs/ci-pool-projects.md#62-the-scheduled-reconcile",
    ),
    Periodic(
        "ci-kube-agents-fleet-reconcile-all", "seeded-fleet reconcile (weekly)", timedelta(hours=192), RECONCILE_ARTIFACT,
        "Eval seeded fleet", "planted defects are not being re-applied", "planted defects are being re-applied again",
        "runs weekly and re-applies the seeded-fleet stack in every free pool project", RECONCILE_EFFECT,
        f"{RUNBOOK_ROOT}docs/ci-pool-projects.md#62-the-scheduled-reconcile",
    ),
)
WATCHED_BY_JOB = {p.job: p for p in WATCHED}


def history_url(job: str) -> str:
    return f"{JOB_HISTORY_ROOT}/{job}"


def iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds") if value else None


def parse_iso(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _not_found(err: str) -> bool:
    text = (err or "").lower()
    return any(pattern in text for pattern in NOT_FOUND_PATTERNS)


class Unreadable(Exception):
    """A read that failed for a reason other than NotFound: the tick is blind
    on this job rather than walking back to an older build and reporting it."""


def _finished(job: str, build: str, runner) -> dict | None:
    """The build's finished.json, None when it has none (still running or
    aborted); Unreadable when the read itself failed."""
    out, err = collect._gsutil_call(["-q", "cat", f"{LOGS_ROOT}/{job}/{build}/{FINISHED}"], runner=runner)
    if out is None:
        if _not_found(err):
            return None
        raise Unreadable(f"{build}/{FINISHED}: {err.strip()}")
    # Present but not a JSON object is unreadable, not "still running": walking
    # past it would report the build before as the latest finished run.
    try:
        doc = json.loads(out)
    except ValueError as exc:
        raise Unreadable(f"{build}/{FINISHED}: not JSON ({exc})")
    if not isinstance(doc, dict):
        raise Unreadable(f"{build}/{FINISHED}: a JSON {type(doc).__name__}, not an object")
    return doc


def _earlier_builds(job: str, newest: str, runner, log=print) -> list[str]:
    out, err = collect._gsutil_call(["-q", "ls", f"{LOGS_ROOT}/{job}/"], runner=runner)
    if out is None:
        log(f"{WARNING_PREFIX}could not list {job}'s builds behind a running {newest}: {err.strip()}", file=sys.stderr)
        return []
    ids = []
    for line in out.splitlines():
        name = line.strip().rstrip("/").rsplit("/", 1)[-1]
        if name.isdecimal() and int(name) < int(newest):
            # The listed name is what is read back: int() is the sort key only.
            ids.append((int(name), name))
    return [name for _, name in sorted(ids, reverse=True)[:FALLBACK_BUILDS]]


def _candidates(job: str, newest: str, runner, log=print):
    """The newest build, then -- only if it is needed -- the finished builds
    before it: the listing is one call over a prefix that only grows, and the
    newest build has finished most of the time."""
    yield newest
    yield from _earlier_builds(job, newest, runner, log)


def read_job(periodic: Periodic, runner=subprocess.run, log=print) -> dict | None:
    """The latest finished build of one periodic, or None when it has none.

    None is also what a pointer that cannot be read for a reason other than
    NotFound returns, after a warning: the bot has gone blind on this job, and
    an absent reading holds any open note rather than ending it."""
    out, err = collect._gsutil_call(["-q", "cat", f"{LOGS_ROOT}/{periodic.job}/{POINTER}"], runner=runner)
    if out is None:
        if not _not_found(err):
            log(f"{WARNING_PREFIX}could not read {periodic.job}'s build pointer: {err.strip()}", file=sys.stderr)
        elif BUCKET_MISSING_MARKER in err.lower():
            log(f"{WARNING_PREFIX}{periodic.job}'s log bucket does not exist: {err.strip()}", file=sys.stderr)
        return None
    newest = out.strip()
    # isdecimal, not isdigit: the latter admits characters int() rejects.
    if not newest.isdecimal():
        log(f"{WARNING_PREFIX}{periodic.job}'s build pointer is not a build id: {newest!r}", file=sys.stderr)
        return None
    for build in _candidates(periodic.job, newest, runner, log):
        try:
            finished = _finished(periodic.job, build, runner)
        except Unreadable as exc:
            log(f"{WARNING_PREFIX}could not read {periodic.job}'s {exc}", file=sys.stderr)
            return None
        if finished is None or finished.get(KEY_RESULT) == RESULT_ABORTED:
            # Still running, never finished, or stopped by Prow: not a run
            # of the job, so the one before it is what there is to read.
            continue
        timestamp = finished.get(KEY_TIMESTAMP)
        try:
            finished_at = datetime.fromtimestamp(timestamp, tz=timezone.utc) if isinstance(timestamp, (int, float)) else None
        except (OverflowError, OSError, ValueError):
            finished_at = None
        reading = {
            KEY_JOB: periodic.job,
            KEY_BUILD: build,
            KEY_FINISHED_AT: iso(finished_at),
            KEY_PASSED: bool(finished.get(KEY_PASSED)),
            KEY_RESULT: str(finished.get(KEY_RESULT) or ""),
        }
        reading[KEY_ARTIFACT] = None
        if periodic.artifact:
            # A failed build's report names the projects; a passed build's says
            # what the run did, which the recovery message carries. Absent is a
            # run that wrote none. Present but not a JSON object is a report cut
            # short, which no later tick can read either, so it is said rather
            # than never. Any other failure is unreadable and the tick is blind
            # on this job, as for finished.json: a note is posted once, and one
            # written without its projects would stay so.
            out, err = collect._gsutil_call(["-q", "cat", f"{LOGS_ROOT}/{periodic.job}/{build}/{ARTIFACTS_DIR}/{periodic.artifact}"], runner=runner)
            if out is not None:
                try:
                    loaded = json.loads(out)
                except ValueError:
                    loaded = None
                if not isinstance(loaded, dict):
                    log(f"{WARNING_PREFIX}{periodic.job}'s {build}/{periodic.artifact} is not a JSON object; the note says so", file=sys.stderr)
                    loaded = {REPORT_KEY_ERROR: REPORT_UNREADABLE}
                reading[KEY_ARTIFACT] = loaded
            elif not _not_found(err):
                # A failed build's note without its report would stay so, so the
                # tick is blind on it. A passed build's report is only the
                # recovery's summary: the reading stands without it.
                log(f"{WARNING_PREFIX}could not read {periodic.job}'s {build}/{periodic.artifact}: {err.strip()}", file=sys.stderr)
                if not reading[KEY_PASSED]:
                    return None
        return reading
    return None


def fetch(out_dir: pathlib.Path, watched=WATCHED, runner=subprocess.run, log=print) -> dict[str, dict]:
    """Write one <job>.json per periodic with a finished build; return them."""
    out_dir.mkdir(parents=True, exist_ok=True)
    readings = {}
    for periodic in watched:
        target = out_dir / f"{periodic.job}{READING_SUFFIX}"
        target.unlink(missing_ok=True)
        reading = read_job(periodic, runner, log)
        if reading is None:
            continue
        target.write_text(json.dumps(reading, indent=2) + "\n", encoding="utf-8")
        readings[periodic.job] = reading
    return readings


def load_readings(directory: pathlib.Path | None, watched=WATCHED) -> dict[str, dict]:
    """The readings `fetch` wrote, by job; an unreadable file is no reading."""
    readings = {}
    if directory is None or not directory.is_dir():
        return readings
    for periodic in watched:
        path = directory / f"{periodic.job}{READING_SUFFIX}"
        if not path.is_file():
            continue
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(loaded, dict) and loaded.get(KEY_JOB) == periodic.job:
            readings[periodic.job] = loaded
    return readings


def reconcile_detail(artifact: dict | None) -> list[str]:
    """What the reconcile's artifact says went wrong, one line per project."""
    if not isinstance(artifact, dict):
        return []
    lines = []
    outcomes = artifact.get(REPORT_KEY_OUTCOMES)
    if isinstance(outcomes, dict):
        for project in sorted(outcomes):
            entry = outcomes[project]
            if not isinstance(entry, dict) or entry.get(REPORT_KEY_OUTCOME) not in RECONCILE_NAMED_OUTCOMES:
                continue
            lines.append(f"{project}: {entry.get(REPORT_KEY_OUTCOME)} ({entry.get(REPORT_KEY_DETAIL) or 'no detail'})")
    # The cap counts projects; the run's own error line comes after it.
    if len(lines) > DETAIL_LIMIT:
        lines = lines[:DETAIL_LIMIT] + [f"and {len(lines) - DETAIL_LIMIT} more"]
    if artifact.get(REPORT_KEY_ERROR):
        lines.append(f"run: {artifact[REPORT_KEY_ERROR]}")
    return lines


def sweep_detail(artifact: dict | None) -> list[str]:
    """What the sweep's report says went wrong: one line per failed project,
    then what the run left for the next one, then the run's own error."""
    if not isinstance(artifact, dict):
        return []
    lines = []
    outcomes = artifact.get(REPORT_KEY_OUTCOMES)
    if isinstance(outcomes, dict):
        for project in sorted(outcomes):
            entry = outcomes[project]
            if isinstance(entry, dict) and entry.get(REPORT_KEY_ERROR):
                lines.append(f"{project}: {entry[REPORT_KEY_ERROR]}")
    if len(lines) > DETAIL_LIMIT:
        lines = lines[:DETAIL_LIMIT] + [f"and {len(lines) - DETAIL_LIMIT} more"]
    left = artifact.get(SWEEP_KEY_LEFT)
    if isinstance(left, int) and left > 0:
        lines.append(f"{left} write(s) left for the next run (the run's write budget)")
    skipped = artifact.get(SWEEP_KEY_SKIPPED)
    if isinstance(skipped, list) and skipped:
        lines.append(f"{len(skipped)} project(s) not swept after the run stopped: {', '.join(str(s) for s in skipped)}")
    if artifact.get(SWEEP_KEY_ENDED_EARLY):
        lines.append(f"run ended early: {artifact[SWEEP_KEY_ENDED_EARLY]}")
    elif artifact.get(REPORT_KEY_ERROR):
        lines.append(f"run: {artifact[REPORT_KEY_ERROR]}")
    return lines


def detail_lines(periodic: Periodic, artifact: dict | None) -> list[str]:
    if periodic.artifact == SWEEP_ARTIFACT:
        return sweep_detail(artifact)
    return reconcile_detail(artifact)


def run_summary(periodic: Periodic, artifact: dict | None, passed: bool) -> str | None:
    """One clause on what the run did, from its report; None without one."""
    if not isinstance(artifact, dict):
        return None
    if artifact.get(REPORT_KEY_ERROR) == REPORT_UNREADABLE:
        return REPORT_UNREADABLE
    if periodic.artifact == SWEEP_ARTIFACT:
        projects = artifact.get(SWEEP_KEY_PROJECTS)
        failed = artifact.get(SWEEP_KEY_FAILED)
        closed = artifact.get(SWEEP_KEY_CLOSED)
        if not isinstance(projects, int):
            return None
        exit_name = artifact.get(REPORT_KEY_EXIT)
        if exit_name not in (None, REPORT_EXIT_OK, REPORT_EXIT_FAILED):
            # Terminated or crashed: what it managed before that.
            text = f"{exit_name} after closing {closed or 0} pull request(s) across {projects} project(s)"
        elif passed:
            text = f"closed {closed or 0} pull request(s) across {projects} project(s)"
        elif not failed:
            # Failed above the project level (Boskos, the mapping, a service
            # unreachable): the run's own line in the detail says what.
            text = f"the run failed after closing {closed or 0} pull request(s) across {projects} project(s)"
        else:
            text = f"failed in {failed} of {projects} project(s)"
            if closed:
                text += f" after closing {closed} pull request(s)"
        skipped = artifact.get(SWEEP_KEY_SKIPPED)
        if isinstance(skipped, list) and skipped:
            text += f", {len(skipped)} not swept"
        left = artifact.get(SWEEP_KEY_LEFT)
        if isinstance(left, int) and left > 0:
            text += f", {left} write(s) left for the next run"
        return text
    summary = artifact.get(RECONCILE_KEY_SUMMARY)
    if not isinstance(summary, dict):
        return None
    parts = [f"{count} {outcome}" for outcome, count in summary.items() if isinstance(count, int) and count > 0]
    if passed:
        return ", ".join(parts) if parts else "nothing to do"
    # A failed build: the named failures first; none means the run failed
    # above the projects (Boskos, the mapping, a crash), which the detail's
    # run line names.
    named = [p for p in parts if p.split(" ", 1)[1] in RECONCILE_NAMED_OUTCOMES]
    if named:
        return ", ".join(named + [p for p in parts if p not in named])
    return f"the run failed after {', '.join(parts)}" if parts else "the run failed before reaching a project"


def runs(readings: dict[str, dict], watched=WATCHED) -> dict[str, dict]:
    """What each read job's latest finished build did, for the recovery message:
    `{job: {build, finished_at, passed, summary}}`."""
    out = {}
    for periodic in watched:
        reading = readings.get(periodic.job)
        if not isinstance(reading, dict):
            continue
        artifact = reading.get(KEY_ARTIFACT) if isinstance(reading.get(KEY_ARTIFACT), dict) else None
        out[periodic.job] = {
            KEY_BUILD: reading.get(KEY_BUILD),
            KEY_FINISHED_AT: reading.get(KEY_FINISHED_AT),
            KEY_PASSED: bool(reading.get(KEY_PASSED)),
            KEY_SUMMARY: run_summary(periodic, artifact, bool(reading.get(KEY_PASSED))),
        }
    return out


def _project_failed(periodic: Periodic, entry) -> bool:
    if not isinstance(entry, dict):
        return False
    if periodic.artifact == SWEEP_ARTIFACT:
        return bool(entry.get(REPORT_KEY_ERROR))
    return entry.get(REPORT_KEY_OUTCOME) in RECONCILE_NAMED_OUTCOMES


def streaks(readings: dict[str, dict], prev_streaks: dict | None, watched=WATCHED) -> dict[str, dict]:
    """Per job, how many checks in a row each project has failed in and how
    many the run has, carried from the previous health.json and advanced once
    per newly read build: a project that failed again counts up, one that
    succeeded drops out, one a failed build did not reach (busy, or the run
    stopped before it) keeps its count, and a clean build clears every count
    (a project it did not reach was busy, not failing); a job with no reading
    keeps everything."""
    out = {}
    for periodic in watched:
        before = (prev_streaks or {}).get(periodic.job) or {}
        projects = {p: n for p, n in (before.get(KEY_STREAK_PROJECTS) or {}).items() if isinstance(n, int) and n > 0}
        runs = before.get(KEY_STREAK_RUNS) if isinstance(before.get(KEY_STREAK_RUNS), int) else 0
        build = before.get(KEY_STREAK_BUILD)
        reading = readings.get(periodic.job)
        if isinstance(reading, dict) and reading.get(KEY_BUILD) != build:
            build = reading.get(KEY_BUILD)
            artifact = reading.get(KEY_ARTIFACT) if isinstance(reading.get(KEY_ARTIFACT), dict) else None
            outcomes = artifact.get(REPORT_KEY_OUTCOMES) if artifact else None
            if reading.get(KEY_PASSED):
                projects = {}
                runs = 0
            else:
                if isinstance(outcomes, dict):
                    for project, entry in outcomes.items():
                        if _project_failed(periodic, entry):
                            projects[project] = projects.get(project, 0) + 1
                        else:
                            projects.pop(project, None)
                runs += 1
        out[periodic.job] = {KEY_STREAK_BUILD: build, KEY_STREAK_PROJECTS: projects, KEY_STREAK_RUNS: runs}
    return out


def _persistent(periodic: Periodic, streak: dict, artifact: dict | None) -> dict[str, int]:
    """The projects that failed in this build and in the checks before it."""
    outcomes = (artifact or {}).get(REPORT_KEY_OUTCOMES)
    outcomes = outcomes if isinstance(outcomes, dict) else {}
    counts = streak.get(KEY_STREAK_PROJECTS) or {}
    return {p: n for p, n in counts.items() if n >= PERSISTENT_AFTER and _project_failed(periodic, outcomes.get(p))}


def _failure_text(periodic: Periodic, entry: dict) -> str:
    """What a failed project's report entry says: the sweep's error line, or
    the reconcile's outcome and detail."""
    if periodic.artifact == SWEEP_ARTIFACT:
        return str(entry.get(REPORT_KEY_ERROR))
    return f"{entry.get(REPORT_KEY_OUTCOME)} ({entry.get(REPORT_KEY_DETAIL) or 'no detail'})"


def _thresholded_detail(periodic: Periodic, persistent: dict, artifact: dict | None) -> list[str]:
    """The persisting projects first with their count, then this build's other
    failed projects, capped together; then the run's own lines."""
    outcomes = (artifact or {}).get(REPORT_KEY_OUTCOMES)
    outcomes = outcomes if isinstance(outcomes, dict) else {}
    lines = []
    for project in sorted(persistent, key=lambda p: (-persistent[p], p)):
        lines.append(f"{project}: failed in {persistent[project]} consecutive checks ({_failure_text(periodic, outcomes[project])})")
    for project in sorted(outcomes):
        if project not in persistent and _project_failed(periodic, outcomes[project]):
            lines.append(f"{project}: {_failure_text(periodic, outcomes[project])}")
    if len(lines) > DETAIL_LIMIT:
        lines = lines[:DETAIL_LIMIT] + [f"and {len(lines) - DETAIL_LIMIT} more"]
    project_prefixes = tuple(f"{p}:" for p in outcomes)
    lines.extend(line for line in detail_lines(periodic, artifact) if not line.startswith(project_prefixes) and not line.startswith("and "))
    return lines


def assess(readings: dict[str, dict], now: datetime, prev_notes: dict | None, watched=WATCHED, streaks: dict | None = None) -> dict[str, dict]:
    """The notes this tick: one per watched job whose latest finished build
    failed, or is older than the job's stale window. `prev_notes` carries each
    open note's `since`. With `streaks` (from streaks()), a failed build is a
    note only once the job's consecutive failed checks reach its threshold. A
    job with no reading writes no note and ends none."""
    notes = {}
    for periodic in watched:
        reading = readings.get(periodic.job)
        if not isinstance(reading, dict):
            continue
        finished_at = parse_iso(reading.get(KEY_FINISHED_AT))
        if finished_at is None or now - finished_at > periodic.stale_after:
            verdict = VERDICT_STALE
        elif not reading.get(KEY_PASSED):
            verdict = VERDICT_FAILED
        else:
            continue
        before = (prev_notes or {}).get(periodic.job) or {}
        artifact = reading.get(KEY_ARTIFACT) if isinstance(reading.get(KEY_ARTIFACT), dict) else None
        persistent = {}
        if verdict == VERDICT_FAILED and streaks is not None:
            streak = streaks.get(periodic.job) or {}
            if (streak.get(KEY_STREAK_RUNS) or 0) < periodic.run_alert_after:
                continue
            persistent = _persistent(periodic, streak, artifact)
        notes[periodic.job] = {
            KEY_JOB: periodic.job,
            KEY_LABEL: periodic.label,
            KEY_VERDICT: verdict,
            KEY_SINCE: before.get(KEY_SINCE) or iso(now),
            KEY_BUILD: reading.get(KEY_BUILD),
            KEY_FINISHED_AT: reading.get(KEY_FINISHED_AT),
            KEY_RESULT: reading.get(KEY_RESULT),
            KEY_STALE_AFTER_H: int(periodic.stale_after.total_seconds() // SECONDS_PER_HOUR),
            KEY_DRY_RUN: bool(artifact.get(KEY_DRY_RUN)) if artifact else None,
            # A job whose first failure is news keeps the report's own lines; a
            # thresholded job leads with the projects that keep failing.
            KEY_DETAIL: (_thresholded_detail(periodic, persistent, artifact) if periodic.run_alert_after > FIRST_FAILURE else detail_lines(periodic, artifact)) if verdict == VERDICT_FAILED else [],
            KEY_SUMMARY: run_summary(periodic, artifact, bool(reading.get(KEY_PASSED))) if verdict == VERDICT_FAILED else None,
            KEY_HISTORY_URL: history_url(periodic.job),
            KEY_PLACE: periodic.place,
            KEY_ABSENCE: periodic.absence,
            KEY_DOES: periodic.does,
            KEY_EFFECT: periodic.effect,
            KEY_RUNBOOK: periodic.runbook,
        }
    return notes


def evidence(note: dict) -> str:
    if note[KEY_VERDICT] == VERDICT_STALE:
        if not note[KEY_FINISHED_AT]:
            return f"{note[KEY_LABEL]}: build {note[KEY_BUILD]} finished at a time its finished.json does not give, so the {note[KEY_STALE_AFTER_H]}h window cannot be measured"
        return f"{note[KEY_LABEL]}: no finished run since {note[KEY_FINISHED_AT]} ({note[KEY_JOB]} has finished nothing in {note[KEY_STALE_AFTER_H]}h)"
    detail = f": {'; '.join(note[KEY_DETAIL])}" if note.get(KEY_DETAIL) else ""
    return f"{note[KEY_LABEL]}: build {note[KEY_BUILD]} failed at {note[KEY_FINISHED_AT]}{detail}"


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    fetch_cmd = sub.add_parser("fetch", help="write one <job>.json per watched periodic with a finished build")
    fetch_cmd.add_argument("--out-dir", type=pathlib.Path, required=True)
    fetch_cmd.add_argument("--job", action="append", help="watch only this job (repeatable; default: every watched job)")
    return parser.parse_args(argv)


def main(argv=None, runner=subprocess.run) -> int:
    args = parse_args(argv)
    watched = WATCHED
    if args.job:
        unknown = [job for job in args.job if job not in WATCHED_BY_JOB]
        if unknown:
            print(f"ERROR: not a watched periodic: {', '.join(unknown)}", file=sys.stderr)
            return EXIT_USAGE
        watched = tuple(WATCHED_BY_JOB[job] for job in args.job)
    readings = fetch(args.out_dir, watched, runner=runner)
    for job in sorted(readings):
        reading = readings[job]
        print(f"{job}: build {reading[KEY_BUILD]} {reading[KEY_RESULT] or ('passed' if reading[KEY_PASSED] else 'failed')} at {reading[KEY_FINISHED_AT]}")
    for periodic in watched:
        if periodic.job not in readings:
            print(f"{periodic.job}: no finished build")
    return 0


if __name__ == "__main__":
    sys.exit(main())
