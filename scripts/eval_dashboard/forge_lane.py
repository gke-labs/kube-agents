#!/usr/bin/env python3
"""The ``lanes`` block of brief.json: each forge lane's last runs.

One document per forge lane (tiers.FORGE_TIERS): the GitLab lane, the GHES
lane and the GitLab CE lane (kube-agents#3682), each its pull-request job and
its scheduled jobs (nightly for GitLab, weekly for the two self-managed lanes,
which share a Brief section). What follows says it of the GitLab lane; the
other two are the same shape under their own tier and jobs.

The lane (``pull-kube-agents-smoke-test-gitlab``, kube-agents#2394) is the
forge-grading cases of the catalogue driven against a pool project's
GitLab repository. Its
runs carry ``tier: gitlab`` (tiers.py) and so reach no gate verdict, no case
history and no digest number (the digest's one forge line, digest_line, says
each scheduled job's last result); this block is where they are seen at all: the
newest ``RUNS_LISTED`` runs with Prow's result, the eval's own verdict, how
many cases passed, and a build link, plus the lane's builds still in
flight. Only stdlib, like the sibling nightly.py.
"""

from __future__ import annotations

import datetime

try:
    from . import nightly, tiers
except ImportError:  # run as a script
    import nightly  # type: ignore[no-redef]
    import tiers  # type: ignore[no-redef]

RUNS_LISTED = 20
# A lane build on the collector's pending_builds is in flight only while its
# first sighting is this recent: the lane runs under the presubmit's
# 360-minute ceiling, plus the time Prow takes to write
# finished.json. Past that it is a pod that died without uploading, which
# the collector keeps on pending_builds for two days (render.py's Grid
# columns draw the same line; nightly.py's running nights a longer one, for
# the nightly's 14-hour budget).
RUNNING_MAX_AGE = datetime.timedelta(hours=8)
DEFAULT_JOB = "pull-kube-agents-smoke-test-gitlab"
DEFAULT_NIGHTLY_JOB = "ci-kube-agents-eval-nightly-gitlab-next"
RESULT_SUCCESS = "SUCCESS"
CADENCE_NIGHTLY = "nightly"
CADENCE_WEEKLY = "weekly"
# How old a scheduled job's newest run may be and still be "last" night's or
# week's: a weekly job's week plus a day of slack for a late start.
WEEKLY_MAX_AGE = datetime.timedelta(days=8)
# The Brief's sections: the GitLab lane alone, and the self-managed forges
# together, one row per run with its forge named.
SECTION_GITLAB = "gitlab"
SECTION_SELF_MANAGED = "self-managed"
SECTION_TITLES = {SECTION_GITLAB: "GitLab lane", SECTION_SELF_MANAGED: "Self-managed lanes"}
# Each forge lane as the Brief names it, with its section, the cadence of its
# scheduled jobs, and its jobs: the pull-request job, and the scheduled jobs
# in the order the section lists them. A job the lane's runs name and this
# table does not is listed after these. A lane whose scheduled jobs are paused
# may carry ``off``, the reason, which the digest and the Brief say in place
# of their runs, so a paused job never reads as red or missed.
LANES = {
    tiers.TIER_GITLAB: {
        "title": "GitLab lane",
        "section": SECTION_GITLAB,
        "cadence": CADENCE_NIGHTLY,
        "job": DEFAULT_JOB,
        "nightly_jobs": (DEFAULT_NIGHTLY_JOB,),
    },
    tiers.TIER_GHES: {
        "title": "GHES lane",
        "section": SECTION_SELF_MANAGED,
        "cadence": CADENCE_WEEKLY,
        "job": "pull-kube-agents-smoke-test-ghes",
        "nightly_jobs": ("ci-kube-agents-eval-weekly-ghes-next",),
    },
    tiers.TIER_GITLAB_CE: {
        "title": "GitLab CE lane",
        "section": SECTION_SELF_MANAGED,
        "cadence": CADENCE_WEEKLY,
        "job": "pull-kube-agents-smoke-test-gitlab-ce",
        "nightly_jobs": ("ci-kube-agents-eval-weekly-gitlab-ce-next",),
    },
}
# The digest line on the forge lanes' scheduled jobs (digest_line).
DIGEST_GLYPH = "🦊"
NEXT_JOB_SUFFIX = "-next"


def is_night(run: dict) -> bool:
    """A forge lane run with no pull request is one of the lane's nightlies'."""
    return run.get("pr") is None


def lane_job(data: dict, nights: bool = False, tier: str = tiers.TIER_GITLAB) -> str:
    """The job name as the lane's newest pull-request run (or night) carries
    it, else the lane's default."""
    for run in reversed(tiers.forge_runs(data.get("runs"), tier)):
        if not isinstance(run, dict) or is_night(run) != nights:
            continue
        job = run.get("job")
        if isinstance(job, str) and job:
            return job
    lane = LANES[tier]
    return lane["nightly_jobs"][0] if nights else lane["job"]


def task_counts(run: dict) -> dict:
    counts = {"pass": 0, "fail": 0, "infra": 0}
    for task in run.get("tasks") or []:
        result = task.get("result") if isinstance(task, dict) else None
        if result in counts:
            counts[result] += 1
    return counts


def lane_run(run: dict) -> dict:
    """One run as the Brief's lane section lists it."""
    result = str(run.get("result") or "").upper() or None
    return {
        "build": str(run.get("build_id")),
        "job": run.get("job") if isinstance(run.get("job"), str) else None,
        "pr": run.get("pr"),
        "head_sha": run.get("head_sha") if isinstance(run.get("head_sha"), str) else None,
        "project": run.get("project") if isinstance(run.get("project"), str) else None,
        "log_url": run.get("log_url") if isinstance(run.get("log_url"), str) else None,
        # Also set for a night whose Spyglass log_url is into a private bucket
        # (a record from before gcs_base): the row links the Console instead.
        "gcs_base": nightly.console_base(run),
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


def running(data: dict, now: datetime.datetime | None = None, tier: str = tiers.TIER_GITLAB) -> list[dict]:
    """The lane's builds the collector listed but could not record yet,
    first seen inside RUNNING_MAX_AGE of ``now`` (without one the age is not
    judged); an entry with no readable first sighting is skipped."""
    out = []
    for entry in data.get("pending_builds") or []:
        if not (isinstance(entry, dict) and tiers.run_tier(entry) == tier and str(entry.get("build_id") or "").isdigit()):
            continue
        seen = parse_iso(entry.get("first_seen"))
        if seen is None or (now is not None and now - seen > RUNNING_MAX_AGE):
            continue
        out.append({"build": str(entry["build_id"]), "first_seen": entry.get("first_seen")})
    out.sort(key=lambda e: int(e["build"]))
    return out


def _counts(runs: list[dict]) -> dict:
    results = [str(r.get("result") or "").upper() for r in runs]
    return {
        "on_record": len(runs),
        "green": sum(1 for r in results if r == RESULT_SUCCESS),
        "red": sum(1 for r in results if r and r != RESULT_SUCCESS),
    }


def lane_document(data: dict, tier: str, now: datetime.datetime | None = None) -> dict:
    """``{tier, title, job, runs[], running[], counts, nightly_jobs[],
    nightly_job, nights[], night_counts}`` for one forge lane: its
    pull-request runs and its nights apart, each newest first and capped at
    RUNS_LISTED, with counts over every one on record; running judged
    against ``now``. ``nightly_jobs`` holds one ``{job, nights[],
    night_counts}`` per scheduled job, the table's jobs first (listed even with no
    night yet), then any other job the lane's nights name; ``nights`` and
    ``night_counts`` are over all of them, ``nightly_job`` the newest
    night's job."""
    every = [r for r in tiers.forge_runs(data.get("runs"), tier) if isinstance(r, dict) and str(r.get("build_id") or "").isdigit()]
    every.sort(key=lambda r: int(r["build_id"]), reverse=True)
    runs = [r for r in every if not is_night(r)]
    nights = [r for r in every if is_night(r)]
    jobs = list(LANES[tier]["nightly_jobs"])
    for night in nights:
        job = night.get("job")
        if isinstance(job, str) and job and job not in jobs:
            jobs.append(job)
    by_job = []
    for job in jobs:
        mine = [r for r in nights if r.get("job") == job]
        by_job.append({"job": job, "nights": [lane_run(r) for r in mine[:RUNS_LISTED]], "night_counts": _counts(mine)})
    return {
        "tier": tier,
        "title": LANES[tier]["title"],
        "section": LANES[tier]["section"],
        "section_title": SECTION_TITLES[LANES[tier]["section"]],
        "cadence": LANES[tier]["cadence"],
        "off": LANES[tier].get("off") or None,
        "job": lane_job(data, tier=tier),
        "runs": [lane_run(r) for r in runs[:RUNS_LISTED]],
        "running": running(data, now, tier),
        "counts": _counts(runs),
        "nightly_jobs": by_job,
        "nightly_job": lane_job(data, nights=True, tier=tier),
        "nights": [lane_run(r) for r in nights[:RUNS_LISTED]],
        "night_counts": _counts(nights),
    }


def lanes_document(data: dict, now: datetime.datetime | None = None) -> list[dict]:
    """brief.json's ``lanes``: one lane_document per forge lane, in
    tiers.FORGE_TIERS order."""
    return [lane_document(data, tier, now) for tier in tiers.FORGE_TIERS]


def gitlab_document(data: dict, now: datetime.datetime | None = None) -> dict:
    """The GitLab lane's lane_document."""
    return lane_document(data, tiers.TIER_GITLAB, now)


def _night_label(lane: dict, job: str) -> str:
    """A scheduled job's short name in the digest: the lane's noun and its
    mode (or ``weekly``) for a job in its table, the job's own name for any other (a rename or an
    extra prefix), so two jobs of one mode never read alike."""
    noun = lane["title"].removesuffix(" lane")
    if job not in lane["nightly_jobs"]:
        return f"{noun} {job}"
    if lane["cadence"] == CADENCE_WEEKLY:
        return f"{noun} weekly"
    return f"{noun} {'next' if job.endswith(NEXT_JOB_SUFFIX) else 'today'}"


def digest_line(data: dict | None, now: datetime.datetime) -> str | None:
    """One line for the daily digest on every forge lane's scheduled jobs:
    the last night's or week's result per job, by the lane's cadence (green,
    red, or none in that time); a lane with a reason (LANES' ``off``) as off,
    with it, its table's jobs only, whatever their runs; a table job with no
    run on record. A job outside the table (a rename, or a retired job with
    runs on record) is named only with a run inside the lane's
    window. None without data."""
    if not data:
        return None
    parts: list[str] = []
    waiting: dict[str, list[str]] = {}
    for lane_doc in lanes_document(data, now):
        lane = LANES[lane_doc["tier"]]
        weekly = lane["cadence"] == CADENCE_WEEKLY
        window = WEEKLY_MAX_AGE if weekly else nightly.LAST_NIGHT_MAX_AGE
        for entry in lane_doc["nightly_jobs"]:
            label = _night_label(lane, entry["job"])
            nights = entry["nights"]
            in_table = entry["job"] in lane["nightly_jobs"]
            started = parse_iso(nights[0].get("started")) if nights else None
            if not in_table and (started is None or now - started > window):
                continue
            if lane.get("off"):
                if in_table:
                    waiting.setdefault(lane["off"], []).append(label)
                continue
            if not nights:
                waiting.setdefault("", []).append(label)
                continue
            if started is None or now - started > window:
                parts.append(f"{label} {'no run last week' if weekly else 'no night last night'}")
            else:
                # A night with no recorded result is neither, as in _counts.
                newest = nights[0]
                parts.append(f"{label} {'green' if newest['green'] else 'red' if newest['result'] else 'no result'}")
    for reason, labels in waiting.items():
        # A job without a reason has run before, its nights just outside
        # what data.json holds.
        parts.append(f"{', '.join(labels)} off ({reason})" if reason else f"{', '.join(labels)} no run on record")
    return f"{DIGEST_GLYPH} Forge lanes: " + nightly.SEP.join(parts)
