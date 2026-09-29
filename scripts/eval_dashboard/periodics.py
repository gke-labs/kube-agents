#!/usr/bin/env python3
"""The Prow periodics that keep the pool in shape, read for the CI health bot.

The pull sweep and the two seeded-fleet reconciles run on the build cluster and
report nowhere but TestGrid. This module reads each one's latest finished build
from the Prow archive (`latest-build.txt`, then `finished.json`, then the
artifact the job wrote, if it writes one) and turns a failed or overdue run into
a note health.py carries and post_health.py posts once, with the job's history
link and, for the reconcile, the projects it refused or could not finish.

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
RECONCILE_NAMED_OUTCOMES = ("refused", "failed", "interrupted")
# gsutil's absent-object wording, the set scripts/release/poll_rc_eval_verdict.py
# settled on for this archive: never a bare 404, because gsutil echoes the
# failing URL and a 19-digit build id can contain those digits.
NOT_FOUND_PATTERNS = ("matched no objects", "no urls matched", "notfoundexception: 404")
# GitHub Actions renders a line with this prefix as a workflow warning, as the
# sibling pool-pressure step's `echo "::warning::..."` does.
WARNING_PREFIX = "::warning::"
# run()'s conventions for a binary that is missing and a call that timed out,
# and argparse's exit for a bad command line.
EXIT_USAGE = 2
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
KEY_DOC = "doc"
# The reconcile's report (hack/fleet_reconcile.py write_report).
REPORT_KEY_OUTCOMES = "outcomes"
REPORT_KEY_OUTCOME = "outcome"
REPORT_KEY_DETAIL = "detail"
REPORT_KEY_ERROR = "error"


@dataclasses.dataclass(frozen=True)
class Periodic:
    job: str
    label: str
    # A finished build older than this is a job that stopped running. It is a
    # window, not the cadence: the sweep runs every ten minutes and the hourly
    # reconcile hourly, and each window is what the TestGrid stale-results
    # setting was for the job.
    stale_after: timedelta
    artifact: str | None
    # Where the recovery is written up.
    doc: str


WATCHED = (
    Periodic("ci-kube-agents-pull-sweep", "GitOps pull sweep", timedelta(hours=1), None, "docs/ci-pool-projects.md, section 5.5"),
    Periodic("ci-kube-agents-fleet-reconcile", "seeded-fleet reconcile (hourly)", timedelta(hours=3), RECONCILE_ARTIFACT, "docs/ci-pool-projects.md, section 6.2"),
    Periodic("ci-kube-agents-fleet-reconcile-all", "seeded-fleet reconcile (weekly)", timedelta(hours=192), RECONCILE_ARTIFACT, "docs/ci-pool-projects.md, section 6.2"),
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
            ids.append(int(name))
    return [str(i) for i in sorted(ids, reverse=True)[:FALLBACK_BUILDS]]


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
        if periodic.artifact:
            # Absent is a run that wrote none; any other failure is unreadable
            # and the tick is blind on this job, as for finished.json: a note
            # is posted once, so one written without its projects stays so.
            out, err = collect._gsutil_call(["-q", "cat", f"{LOGS_ROOT}/{periodic.job}/{build}/{ARTIFACTS_DIR}/{periodic.artifact}"], runner=runner)
            artifact = None
            if out is not None:
                try:
                    loaded = json.loads(out)
                except ValueError:
                    loaded = None
                if not isinstance(loaded, dict):
                    # Present but cut short (the report is written last, and
                    # a signal can cut the write): unreadable, as above.
                    log(f"{WARNING_PREFIX}could not read {periodic.job}'s {build}/{periodic.artifact}: not a JSON object", file=sys.stderr)
                    return None
                artifact = loaded
            elif not _not_found(err):
                log(f"{WARNING_PREFIX}could not read {periodic.job}'s {build}/{periodic.artifact}: {err.strip()}", file=sys.stderr)
                return None
            reading[KEY_ARTIFACT] = artifact
        return reading
    return None


def fetch(out_dir: pathlib.Path, watched=WATCHED, runner=subprocess.run, log=print) -> dict[str, dict]:
    """Write one <job>.json per periodic with a finished build; return them."""
    out_dir.mkdir(parents=True, exist_ok=True)
    readings = {}
    for periodic in watched:
        target = out_dir / f"{periodic.job}.json"
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
        path = directory / f"{periodic.job}.json"
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


def assess(readings: dict[str, dict], now: datetime, prev_notes: dict | None, watched=WATCHED) -> dict[str, dict]:
    """The notes this tick: one per watched job whose latest finished build
    failed, or is older than the job's stale window. `prev_notes` carries each
    open note's `since`. A job with no reading writes no note and ends none."""
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
            KEY_DETAIL: reconcile_detail(artifact) if verdict == VERDICT_FAILED else [],
            KEY_HISTORY_URL: history_url(periodic.job),
            KEY_DOC: periodic.doc,
        }
    return notes


def evidence(note: dict) -> str:
    if note[KEY_VERDICT] == VERDICT_STALE:
        if not note[KEY_FINISHED_AT]:
            return f"{note[KEY_LABEL]}: build {note[KEY_BUILD]} finished at a time the archive does not give, so the {note[KEY_STALE_AFTER_H]}h window cannot be measured"
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
