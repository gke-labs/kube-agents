#!/usr/bin/env python3
"""The ``gitlab`` block of brief.json: the GitLab lane's last runs.

The lane (``pull-kube-agents-smoke-test-gitlab``, kube-agents#2394) is the
forge-grading cases of the catalogue driven against a pool project's
GitLab repository. Its
runs carry ``tier: gitlab`` (tiers.py) and so reach no gate verdict, no case
history and no digest number; this block is where they are seen at all: the
newest ``RUNS_LISTED`` runs with Prow's result, the eval's own verdict, how
many cases passed, and a Spyglass link, plus the lane's builds still in
flight. Only stdlib, like the sibling nightly.py.
"""

from __future__ import annotations

import datetime

try:
    from . import tiers
except ImportError:  # run as a script
    import tiers  # type: ignore[no-redef]

RUNS_LISTED = 20
# A lane build on the collector's pending_builds is in flight only while its
# first sighting is this recent: the lane runs under the presubmit's
# 360-minute ceiling, plus the time Prow takes to write
# finished.json. Past that it is a pod that died without uploading, which
# the collector keeps on pending_builds for two days (render.py's Grid
# columns and nightly.py's running nights draw the same line).
RUNNING_MAX_AGE = datetime.timedelta(hours=8)
DEFAULT_JOB = "pull-kube-agents-smoke-test-gitlab"
RESULT_SUCCESS = "SUCCESS"


def lane_job(data: dict) -> str:
    """The job name as the newest GitLab run carries it, else the default."""
    for run in reversed(tiers.gitlab_runs(data.get("runs"))):
        job = run.get("job")
        if isinstance(job, str) and job:
            return job
    return DEFAULT_JOB


def task_counts(run: dict) -> dict:
    counts = {"pass": 0, "fail": 0, "infra": 0}
    for task in run.get("tasks") or []:
        result = task.get("result") if isinstance(task, dict) else None
        if result in counts:
            counts[result] += 1
    return counts


def lane_run(run: dict) -> dict:
    """One run as the Brief's GitLab section lists it."""
    result = str(run.get("result") or "").upper() or None
    return {
        "build": str(run.get("build_id")),
        "job": run.get("job") if isinstance(run.get("job"), str) else None,
        "pr": run.get("pr"),
        "head_sha": run.get("head_sha") if isinstance(run.get("head_sha"), str) else None,
        "project": run.get("project") if isinstance(run.get("project"), str) else None,
        "started": run.get("started"),
        "finished": run.get("finished"),
        "duration_s": run.get("duration_s") if isinstance(run.get("duration_s"), (int, float)) else None,
        "result": result,
        "eval_verdict": run.get("eval_verdict"),
        "green": result == RESULT_SUCCESS,
        "tasks": task_counts(run),
    }


def parse_iso(value) -> datetime.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)


def running(data: dict, now: datetime.datetime | None = None) -> list[dict]:
    """The lane's builds the collector listed but could not record yet,
    first seen inside RUNNING_MAX_AGE of ``now`` (without one the age is not
    judged); an entry with no readable first sighting is skipped."""
    out = []
    for entry in data.get("pending_builds") or []:
        if not (isinstance(entry, dict) and tiers.is_gitlab(entry) and str(entry.get("build_id") or "").isdigit()):
            continue
        seen = parse_iso(entry.get("first_seen"))
        if seen is None or (now is not None and now - seen > RUNNING_MAX_AGE):
            continue
        out.append({"build": str(entry["build_id"]), "first_seen": entry.get("first_seen")})
    out.sort(key=lambda e: int(e["build"]))
    return out


def gitlab_document(data: dict, now: datetime.datetime | None = None) -> dict:
    """``{job, runs[], running[], counts}``: runs newest first, capped at
    RUNS_LISTED; running judged against ``now``; counts over every GitLab
    run on record."""
    runs = [r for r in tiers.gitlab_runs(data.get("runs")) if isinstance(r, dict) and str(r.get("build_id") or "").isdigit()]
    runs.sort(key=lambda r: int(r["build_id"]), reverse=True)
    listed = [lane_run(r) for r in runs[:RUNS_LISTED]]
    results = [str(r.get("result") or "").upper() for r in runs]
    return {
        "job": lane_job(data),
        "runs": listed,
        "running": running(data, now),
        "counts": {
            "on_record": len(runs),
            "green": sum(1 for r in results if r == RESULT_SUCCESS),
            "red": sum(1 for r in results if r and r != RESULT_SUCCESS),
        },
    }
