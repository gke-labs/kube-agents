#!/usr/bin/env python3
"""Track a failing release pipeline stage as an issue, and say so in Chat.

A release pipeline run that fails is a red row in the Actions tab and nothing
else. On 2026-10-20 and 2026-10-21 `Staging: Promotion Pipeline` failed two
nights in a row on the same test, the GA scheduler skipped both mornings with
"No commits between 0.16.0 and the gate-passing commit", and the first anyone
knew was a person asking why there had been no release. #1978 describes the
gap for the promotion pipeline; the RC and GA pipelines have the same one.

So this writes it down, the way `scripts/notify_broken_main.py` does for a
required check on `main`. For each watched workflow it reads the newest
completed run that said anything and brings the tracking issue into line:

    failing, no open issue      -> open "release pipeline stage broken: <workflow>"
    failing, issue open         -> rewrite the body for the newest run, one comment
    green, issue open           -> comment and close, when the failed stage ran
    anything already recorded   -> nothing

One open issue per workflow at a time, found by a hidden marker in its body
rather than by title. The body names the stage (the failing job that
completed last, leaving aside the compensation and non-gating jobs named in
`COMPENSATION_JOB_MARKERS` unless nothing else failed), the step inside it,
the candidate the run tested, the first failure line the job's log shows,
and the runs of the current streak; hidden stamps list the
run ids the body already reflects and the stage that failed, so handling a
run twice writes nothing. An issue a person closed while the pipeline was
still red stays closed for the runs it names; the next failing run opens a
fresh one.

The promotion pipeline has a second issue of its own. A night red on the
eval's RED verdict alone is no stage failure, but `STALL_NIGHTS` of them in
a row (a night is a UTC date; several RED runs on one date are one night) cut no staging tag and stall the release chain; that opens one
`ci:release-stalled` issue with a row per RED run (the night, the run, the
candidate commit, the eval build the step-5 log links), adds a row for each
later RED run, and closes on the next run whose eval passed. A night that fails on a
stage before the eval passes leaves it alone. A night that skips the eval
step, a `skip_promotion` night or a hand-dispatched `skip_pipeline` one,
neither counts nor ends the streak; past `STALL_SKIP_RUNS_MAX` such runs the
walk stops looking further back.

Two bounds on what a green run may close. A run being re-run leaves
`status=completed`, so the newest run by number is read whatever its status,
and while it is queued or in progress nothing is decided: the earlier green
behind it is not the pipeline's current state, and closing on it would cite
a run that finished before the failure. And a green closes only when the
stage the issue records ran and passed in it: `release-publish.yml` under
`schedule_gate=dry-run`, a promotion run with `skip_pipeline`, an RC run
that skips, all conclude `success` with the stage skipped, which says
nothing about the failure. A green that is itself a re-run of the failing
run counts: it is that run passing on its candidate.

On open and on close it also posts a few lines to the CI-health Chat space,
through the same sender, space and token `scripts/eval_dashboard/post_health.py`
uses: the workflow mints the token as the dashboard publisher and passes it
in `CI_HEALTH_CHAT_TOKEN`; unconfigured, the post is skipped and said so,
and the issue is still filed. The GitHub write comes first, so the message
can cite the issue number.

The candidate is not in the run's API record: the watched workflows take it
as a `workflow_dispatch` input, which the runs endpoint does not return. It
is read from the job logs instead, where the runner echoes a reusable
workflow's inputs (`commit_sha:`, `rc_tag:`), a step's `env:` (`RC_TAG:`,
`TARGET_COMMIT:`) and the resolver banners (`Candidate RC Tag:`,
`Gate-passing commit:`). A run whose logs have none of those is reported
with the candidate unknown rather than left unreported. The first failure
line is read from the failing step's slice of the log, by the timestamps
every log line carries, before the whole log: a pytest summary line first,
then the runner's `##[error]` annotation, then a script's own error line.

A sweep rather than a `workflow_run` trigger. Posting to Chat needs
`id-token: write` to mint the token, and `tests/conformance/test_B_write_path.py`
(B4) requires a `workflow_run` job holding that permission to gate on
`workflow_run.conclusion == 'success'`, which a failure notifier cannot. The
sweep runs every fifteen minutes, reconciles every watched workflow, and
writes nothing when the issues already say what the runs say, so it is safe
to run at any time; a dispatch with `run_id` reconciles that run's workflow
alone. The release pipelines run a handful of times a day and take between
forty minutes and six hours, so a bound of fifteen minutes on the delay
costs nothing.

Setup: `GITHUB_TOKEN` (or `GH_TOKEN`; the workflow's own token, with
`issues: write` and `actions: read`). Each of the two `ci:` labels
(`ci:release-stage-broken`, `ci:release-stalled`) is created the first time
it is needed. Chat needs `CI_HEALTH_CHAT_SPACE` and `CI_HEALTH_CHAT_TOKEN`, the
poster's own variables, or nothing is posted and the log says so. The client
is `scripts/github_api.py`'s, through the flaky-check reporter's subclass
for the job-log redirect, so reads retry on 5xx and rate limits.

Run:  GH_TOKEN="$(gh auth token)" python3 scripts/release/notify_release_stage_broken.py --sweep --dry-run
      GH_TOKEN="$(gh auth token)" python3 scripts/release/notify_release_stage_broken.py --run-id 39533999681 --dry-run
Test: cd scripts && python3 -m unittest test_notify_release_stage_broken
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The shared helpers live one directory up: the broken-main notifier's
# event-agnostic decision over a run history, the flaky-check reporter's API
# client (the retrying base client plus the two-hop job-log read), and the
# CI-health poster's Chat sender, so this file spells none of them a second
# time.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval_dashboard.post_health import SPACE_ENV, TOKEN_ENV, Sender  # noqa: E402
from github_api import is_refusal, log  # noqa: E402
from notify_broken_main import FAILING_CONCLUSIONS, decide, newest_reporting_run, reporting_history  # noqa: E402
from notify_flaky_check import GitHubAPI as FlakyGitHubAPI  # noqa: E402
from poll_rc_eval_verdict import SETTLED_TRUE, VERDICT_LINE_FORMAT, VERDICT_RED  # noqa: E402

# The release workflows, by the `name:` each declares, which is how the
# Actions API lists them. `test_notify_release_stage_broken.py` holds this
# tuple to the workflow files, and `test_integration_contracts.py` holds it to
# `RELEASE_STAGE_WATCHED_WORKFLOWS`, the roster of release stages that have to
# be watched.
EVAL_PIPELINE = "Staging: Promotion Pipeline"
WATCHED_WORKFLOWS = (
    "RC: Release Pipeline",
    EVAL_PIPELINE,
    "Release & Publish (GA)",
)

LABEL = "ci:release-stage-broken"
LABEL_COLOR = "d73a4a"
LABEL_DESCRIPTION = "A release pipeline stage is failing"

DEFAULT_REPO = "gke-labs/kube-agents"
REPO_ENV = "GITHUB_REPOSITORY"
# Where the token is read from, in order: the name the workflow sets, then
# the name `gh` uses, for a hand run with `GH_TOKEN="$(gh auth token)"`.
TOKEN_ENVS = ("GITHUB_TOKEN", "GH_TOKEN")
USER_AGENT = "kube-agents-notify-release-stage"

# Runs to read back per workflow, every status. The streak the issue lists
# is cut off here; thirty runs of a pipeline that runs at most eight times a
# day is several days of history, past which a streak is not news.
HISTORY_DEPTH = 30
# Issues to read back per state. One open issue per workflow is the
# invariant, so a page covers the open ones; every closed one on the page is
# checked for the run, newest-updated first.
ISSUE_PAGE = 50
# The run's first jobs whose logs are read for the candidate. Each pipeline
# names it in its first job (the tag step, the resolver, the gate decision);
# the second is for a run whose first job is a checkout-only prelude.
CANDIDATE_LOG_JOBS = 2
# A commit sha is shown short in prose and in Chat; the body carries it whole.
SHORT_SHA = 7
# A run's time is shown to the minute: `2026-10-21 02:36 UTC`.
WHEN_LENGTH = len("2026-10-21 02:36")
WHEN_SUFFIX = " UTC"
WHEN_UNKNOWN = "time unknown"
# What an issue opened under `--dry-run` is numbered in the printed messages.
DRY_RUN_ISSUE_NUMBER = 0
# The first failure line is cut here; a pytest summary line can run to a
# screenful of assertion text, and the log link has the rest.
FAILURE_LINE_LIMIT = 200
# The label POST answers this for a label that already exists, which is the
# ordinary case from the second breakage on.
LABEL_EXISTS = 422

# A run's status while it is queued, running, or being re-run; a run that
# has said what it has to say is `completed`.
COMPLETED = "completed"
# The two issue states the reconciliation reads.
ISSUE_OPEN = "open"
ISSUE_CLOSED = "closed"
# What GitHub records on an issue a person reopened. A green leaves such an
# issue alone: reopening is a decision, and the sweep would otherwise close it
# again every fifteen minutes with a fresh "passed" comment and Chat post.
REOPENED = "reopened"
# Job and step conclusions. A job cancelled by a timeout or by a sibling's
# failure is where the reader looks when no job failed outright. In a green
# run, a stage that concluded skipped or cancelled did not run, which is no
# recovery; one that concluded anything else ran, failure included, since a
# job that fails inside a green run is `continue-on-error` and not the gate
# (the promotion pipeline's rollback leg is one).
JOB_STOPPED_CONCLUSIONS = ("cancelled",)
JOB_NOT_RUN_CONCLUSIONS = ("skipped", "cancelled")
STEP_FAILED = "failure"
# A job without a `completed_at` sorts before every job that has one.
NO_TIME = ""
# Substrings of the job names that are compensation or non-gating legs in
# the watched workflows: the promotion pipeline's rollback leg (`Roll
# Back`, `continue-on-error`), its withdrawal of an unsettled nomination
# (`Withdraw`, `always()`) and its teardown (`Tear Down`, `!cancelled()`),
# the RC pipeline's teardown and its `(non-gating)` legs. The
# jobs API carries neither `if:` nor `continue-on-error`, so the names are
# the only handle. Such a job fails after the gating failure (a withdrawal
# runs because step 5 did not settle) or inside green runs, and is skipped
# on an ordinary green night, so naming it as the stage would hold the
# issue open for good. `test_integration_contracts.py` pins this tuple to
# the workflow files, so a rename of those jobs reds the test.
ROLLBACK_LEG_MARKER = "Roll Back"
WITHDRAWAL_JOB_MARKER = "Withdraw"
COMPENSATION_JOB_MARKERS = ("(non-gating)", ROLLBACK_LEG_MARKER, WITHDRAWAL_JOB_MARKER, "Tear Down")
# The promotion pipeline's (`EVAL_PIPELINE`) eval wait (step 5), by name
# substring. It fails on a RED verdict and on no verdict alike,
# and the jobs API carries no job outputs, so the withdrawal is what tells
# the two apart: its `if:` runs it only when step 5 did not settle, and a
# settled RED leaves it skipped. A RED is the eval rejecting the candidate,
# which is the gate working and a row in the eval dashboard's Brief, not a
# stage failure; a night red on it alone is read as a green night
# (gke-labs/kube-agents#1978). The rollback leg is the one job that fails
# inside green runs (`continue-on-error`), so it is the one failure that
# counts for nothing beside the verdict; a teardown that failed reds a
# green night and reds a RED one.
EVAL_WAIT_JOB_MARKER = "Await Release-Candidate Eval"
JOB_SKIPPED = "skipped"

# Two switches the workflow sets, each turning one log line into a warning
# annotation on the job (the unit tests run without them, and their output
# is echoed into a CI step where an unconditional annotation would land on
# every pull request). `RELEASE_STAGE_ANNOTATE` is set on every workflow
# run, for a newest run stuck past `STUCK_RUN_AFTER`; `RELEASE_STAGE_CHAT_EXPECTED`
# only when the Chat token was meant to be minted (a write on `main`, not
# muted), for an unconfigured or failed post, since this workflow is the
# only place a persistent mint failure would show.
ANNOTATE_ENV = "RELEASE_STAGE_ANNOTATE"
CHAT_EXPECTED_ENV = "RELEASE_STAGE_CHAT_EXPECTED"
SWITCH_ON = "true"
WARNING_PREFIX = "::warning::"
# A newest run that is still queued or in progress silences the sweep for
# its workflow, by design; past this age it is stuck (an environment
# approval nobody gave, a runner that never picked it up) and the sweep
# says so. The longest watched pipeline is the promotion: a deploy and a
# matrix of about two hours, then an eval wait of up to 330 minutes.
STUCK_RUN_AFTER = timedelta(hours=8)
RUN_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# A job of a reusable workflow is listed as `<caller job> / <called job>`,
# and the same stage is listed under the short name when the call itself
# never expanded, so a stage is matched on either side of the separator.
REUSABLE_JOB_SEPARATOR = " / "

# Hidden lines in the issue body. The marker finds the issue again whatever
# its title; the stamps name the runs the body already reflects and the
# stage that failed, which a green run has to have run to count.
MARKER = "<!-- release-stage-broken workflow={workflow_id} -->"
STAMP = "<!-- release-stage-broken runs={run_ids} -->"
STAGE_STAMP = "<!-- release-stage-broken stage={stage} -->"
# A stamped run is `<id>:<attempt>`, so a re-run that fails on another stage
# is new evidence and rewrites the body; a bare `<id>` (the stamp's first
# shape) reads as the first attempt.
# A stamp is `<run id>[:<attempt>]` entries joined by commas and nothing else;
# a hand-edited stamp that does not fit reads as no stamp rather than as a
# ValueError out of the whole sweep.
_STAMP_RE = re.compile(r"<!-- release-stage-broken runs=((?:\d+(?::\d+)?(?:,\d+(?::\d+)?)*)?) -->")
_STAGE_STAMP_RE = re.compile(r"<!-- release-stage-broken stage=(.+?) -->")
STAMP_ATTEMPT_SEPARATOR = ":"
FIRST_ATTEMPT = 1

# The branch whose runs are the pipeline's state. The three workflows are
# dispatched on `main` by their schedulers; a dispatch from a feature branch
# is someone trying a change, and reading it as the pipeline would open an
# issue for a branch failure, close a real one on a branch pass, or silence
# the sweep while a branch run is in progress.
MAIN_BRANCH = "main"

TITLE = "🔴 release pipeline stage broken: {workflow}"
RUNBOOK = "`scripts/release/README.md`, \"Pipeline Cadence & Execution Flow\" (the \"When a stage fails\" bullet)"
CANDIDATE_UNKNOWN = "not found in the run's logs"
STAGE_UNKNOWN = "no failing job in the run's job list"

# What the runner writes at the start of every log line (the first line of
# the file also carries a byte order mark), captured to the second so a line
# can be placed inside a step's `started_at`..`completed_at` window, which
# the jobs endpoint gives to the second; and the colour codes a step's own
# output carries.
_LOG_TIMESTAMP_RE = re.compile(r"^﻿?(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?Z ")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
TIMESTAMP_SUFFIX = "Z"
# The candidate, as the watched pipelines' logs spell it: a reusable
# workflow's echoed inputs, a step's echoed `env:`, and the resolvers'
# banners.
_CANDIDATE_TAG_RE = re.compile(r"^\s*(?:rc_tag|RC_TAG|Candidate RC Tag|From gate tag):\s*(\S+)\s*$")
_CANDIDATE_SHA_RE = re.compile(
    r"^\s*(?:commit_sha|COMMIT_SHA|Commit SHA|TARGET_COMMIT|Gate-passing commit):\s*([0-9a-f]{40})\s*$"
)
# The first failure, in order of how much it says: a pytest summary line
# names the test; the runner's `##[error]` annotation names what a step
# reported, except the one every failed `run:` step emits, which says only
# that it exited non-zero; a script's own error line names its check; and
# that generic annotation is the last resort.
_FAILURE_LINE_RES = (
    re.compile(r"^(?:FAILED|ERROR) \S+::.*$"),
    re.compile(r"^##\[error\](?!Process completed with exit code).*$"),
    re.compile(r"^.*(?:❌|ERROR:).*$"),
    re.compile(r"^##\[error\].*$"),
)

CHAT_BROKEN = "🔴 *Release pipeline: {workflow} failed* — {stage}: {failure}"
# Chat parses the text for paired `*`, `_`, `~` and backticks; a failure line
# is a log line, so each is replaced by a look-alike that Chat does not parse.
CHAT_FORMATTING = {"*": "∗", "~": "∼", "`": "'"}
CHAT_UNDERSCORE_PAIR_RE = re.compile(r"(?<!\w)_|_(?!\w)")
CHAT_UNDERSCORE_LOOKALIKE = "‗"
CHAT_BROKEN_DETAIL = "Candidate {candidate}. Tracking #{number}."
CHAT_RECOVERED = "🟢 *Release pipeline: {workflow} passes again* — run #{run_number} on candidate {candidate}. Closed #{number}."
CHAT_RECOVERED_RED_VERDICT = "🟢 *Release pipeline: {workflow} stages pass again* — run #{run_number} was rejected by the eval's RED verdict on candidate {candidate}, not by a stage. Closed #{number}."
# How the closing comment describes the run that closed the issue: an
# ordinary green run, or a night red on the eval's RED verdict alone, whose
# stages all ran and passed up to a verdict on the candidate.
RECOVERED_PASSED = "passed on candidate {candidate}"
RECOVERED_RED_VERDICT = "ran every stage before the release-candidate eval and was rejected by its RED verdict on candidate {candidate}, a judgement on the candidate and not a stage failure"
NOT_CONFIGURED = "chat not configured: set CI_HEALTH_CHAT_SPACE and CI_HEALTH_CHAT_TOKEN; nothing posted"

# The stalled release chain (gke-labs/kube-agents#4761). A RED night is no
# stage failure, so the stage issue stays shut, and night after night of them
# cuts no staging tag and leaves the GA scheduler nothing to publish. This
# many settled-RED nights of `EVAL_PIPELINE` in a row, with no green run and
# no stage failure between, open one issue. A night is the UTC date a run was
# created on, so several RED runs on one date count once; each run still gets
# its row and its stamp. Each later RED run adds its row; the next run that passes the eval closes it. A night that fails on a
# stage before the eval passes leaves it alone: the stage issue covers that
# night.
STALL_NIGHTS = 2
# The most skip runs the walk looks past, over the whole walk: a week of them
# at one a night. Each one passed over costs a jobs read per sweep, so the cap
# counts runs, not dates. Past it the walk stops, so with no issue open the
# streak counts only the RED nights it reached.
STALL_SKIP_RUNS_MAX = 7
STALL_LABEL = "ci:release-stalled"
STALL_LABEL_COLOR = "b60205"
STALL_LABEL_DESCRIPTION = "The release-candidate eval has returned RED several nights in a row"
TRIAGE_LABEL = "needs-triage"
# Its own marker kind, so it never collides with the stage issue on the same
# workflow; the stamp lists the RED runs the body has rows for, newest first.
STALL_MARKER = "<!-- release-stalled workflow={workflow_id} -->"
STALL_STAMP = "<!-- release-stalled runs={run_ids} -->"
_STALL_STAMP_RE = re.compile(r"<!-- release-stalled runs=((?:\d+(?::\d+)?(?:,\d+(?::\d+)?)*)?) -->")
STALL_TITLE = "🔴 release chain stalled: the RC eval has returned RED {n} nights in a row"
STALL_LEAD = "The promotion pipeline has cut no staging tag because the release-candidate eval verdict was RED on each of these nights."
STALL_TABLE_HEADER = "| Night (UTC) | Promotion run | Candidate | Eval build |"
STALL_TABLE_RULE = "| --- | --- | --- | --- |"
STALL_ROW = "| {night} | {run} | {candidate} | {build} |"
STALL_CELL_SEPARATOR = "|"
STALL_FOOTER = "The failing cases are in each eval build's verdict; this issue names the nights, not the cases."
STALL_COMMENT = "RED again: {runs}. The RC eval has now returned RED {n} nights in a row."
STALL_CLOSED_COMMENT = "{run} ({when}) passed the release-candidate eval; the release chain is moving again. Closing."
CHAT_STALLED = STALL_TITLE + " — {url}"
CHAT_MOVING = "✅ release chain moving again: run #{run_number} passed the gate — closed {url}"
# The close for a run whose eval passed and a later stage then failed: the
# stall is over, but the chain is not moving, and the same sweep has filed
# the stage issue.
STALL_CLOSED_STAGE_FAILED_COMMENT = "{run} ({when}) passed the release-candidate eval, so the stall is over; a later stage failed in that run and the stage issue holds it. Closing."
CHAT_PAST_GATE = "✅ release chain past the gate: run #{run_number} passed the eval; a later stage failed, see the stage issue — closed {url}"
EVAL_LINK_UNAVAILABLE = "(eval link unavailable)"
# A night is the UTC date the run was created: `2026-11-04`.
NIGHT_LENGTH = len("2026-11-04")
COMMIT_URL = "https://github.com/{repo}/commit/{sha}"
ISSUE_URL = "https://github.com/{repo}/issues/{number}"
# What `poll_rc_eval_verdict.py` prints into the step-5 log once it has a
# settled RED: the Prow build id and its Spyglass link. An empty build id
# prints no link, and reads as unavailable.
# The pattern is built from the poller's own format, so a change to the line
# there changes what is read here.
_LINE_FIELD = "FIELDPLACEHOLDER"
_LINE_FIELD_RE = r"(\S+)"
_RED_VERDICT_LINE_RE = re.compile(
    "^"
    + re.escape(VERDICT_LINE_FORMAT.format(verdict=VERDICT_RED, settled=SETTLED_TRUE, build_id=_LINE_FIELD, url=_LINE_FIELD)).replace(_LINE_FIELD, _LINE_FIELD_RE)
    + "$"
)
JOB_PASSED = "success"
# The outcome word of the stalled-chain half of a reconcile, and how it is
# joined to the stage half's when both wrote.
STALL_OUTCOME = "stall-{outcome}"
OUTCOME_SEPARATOR = ", "
NOTHING = "nothing"


# --------------------------------------------------------------------------- #
# Reading a run
# --------------------------------------------------------------------------- #


def split_log_line(raw: str) -> tuple:
    """(timestamp to the second with its `Z`, or None; the line without the
    runner's prefix and colour codes)."""
    match = _LOG_TIMESTAMP_RE.match(raw)
    stamp = match.group(1) + TIMESTAMP_SUFFIX if match else None
    body = raw[match.end() :] if match else raw
    return stamp, _ANSI_RE.sub("", body.rstrip("\r"))


def clean_log_line(raw: str) -> str:
    return split_log_line(raw)[1]


def extract_candidate(log_text: str, found: dict | None = None) -> dict:
    """The candidate tag and commit the log names, first mention of each.

    `found` carries what an earlier job's log already gave, so a second log
    fills only the holes.
    """
    found = dict(found or {})
    for raw in log_text.splitlines():
        line = clean_log_line(raw)
        if "tag" not in found:
            match = _CANDIDATE_TAG_RE.match(line)
            if match:
                found["tag"] = match.group(1)
        if "sha" not in found:
            match = _CANDIDATE_SHA_RE.match(line)
            if match:
                found["sha"] = match.group(1)
        if "tag" in found and "sha" in found:
            break
    return found


def _first_match(lines: list) -> str | None:
    for pattern in _FAILURE_LINE_RES:
        for line in lines:
            if pattern.match(line):
                return line[:FAILURE_LINE_LIMIT]
    return None


def extract_first_failure(log_text: str, window: tuple | None = None) -> str | None:
    """The most informative failure line the log holds, or None.

    `window` is the failing step's `(started_at, completed_at)`; lines
    stamped inside it are searched first, so an `ERROR:` an earlier step
    printed and survived cannot stand in for the one that failed the job.
    The whole log is the fallback, for a step whose times the API did not
    give.
    """
    stamped = [split_log_line(raw) for raw in log_text.splitlines()]
    if window and all(window):
        start, end = window
        inside = [line for stamp, line in stamped if stamp and start <= stamp <= end]
        found = _first_match(inside)
        if found:
            return found
    return _first_match([line for _, line in stamped])


def is_compensation(job: dict) -> bool:
    name = job.get("name") or ""
    return any(marker in name for marker in COMPENSATION_JOB_MARKERS)


def is_eval_wait(job: dict) -> bool:
    return EVAL_WAIT_JOB_MARKER in (job.get("name") or "")


def settled_red_verdict(jobs: list) -> bool:
    """True when the eval wait failed and its withdrawal was skipped: the
    verdict settled, and a failed step 5 with a settled verdict is a RED."""
    eval_failed = any(is_eval_wait(job) and job.get("conclusion") in FAILING_CONCLUSIONS for job in jobs)
    withdrawal_skipped = any(WITHDRAWAL_JOB_MARKER in (job.get("name") or "") and job.get("conclusion") == JOB_SKIPPED for job in jobs)
    return eval_failed and withdrawal_skipped


def red_verdict_night(jobs: list) -> bool:
    """True when the run is red on a settled RED verdict and nothing else:
    no job other than the eval wait failed or stopped, the rollback leg
    excepted, since it fails inside green runs too. A teardown that failed
    beside the verdict makes the night a stage failure, as it would a green
    one, and `failing_job` names it."""
    if not settled_red_verdict(jobs):
        return False
    return not any(
        (job.get("conclusion") in FAILING_CONCLUSIONS or job.get("conclusion") in JOB_STOPPED_CONCLUSIONS)
        and not is_eval_wait(job)
        and ROLLBACK_LEG_MARKER not in (job.get("name") or "")
        for job in jobs
    )


def eval_passed(jobs: list) -> bool:
    """True when the eval wait ran and passed: the verdict was GREEN."""
    return any(is_eval_wait(job) and job.get("conclusion") == JOB_PASSED for job in jobs)


def eval_skipped(jobs: list) -> bool:
    """True when the run has an eval wait and it was skipped."""
    return any(is_eval_wait(job) and job.get("conclusion") == JOB_SKIPPED for job in jobs)


def failing_job(jobs: list) -> dict | None:
    """The job the run stopped at, in four ranks: a gating job that failed,
    a gating job that was cancelled, a compensation job that failed, any
    job that was cancelled; within a rank, the one that completed last.

    Last to complete, not first in the list: a `continue-on-error` job that
    failed sits in the list beside the gating stages and fails inside green
    runs too, while the failure that ends a run is the one that finishes
    it. A gating job killed at its `timeout-minutes` can read `cancelled`
    rather than `timed_out`, and still outranks a compensation job that
    failed after it: a withdrawal or a teardown runs because a stage
    stopped, and is skipped on a green night, so it is the stage only when
    nothing else stopped. Ties and missing times fall back to list order.
    An eval wait that failed on a settled RED verdict is not a failure at
    all, and is passed over for whatever else stopped the run.
    """
    if settled_red_verdict(jobs):
        jobs = [job for job in jobs if not is_eval_wait(job)]

    def ranked(conclusions, compensation):
        found = [
            (job.get("completed_at") or NO_TIME, -index, job)
            for index, job in enumerate(jobs)
            if job.get("conclusion") in conclusions and (compensation is None or is_compensation(job) is compensation)
        ]
        return max(found, key=lambda item: item[:2])[2] if found else None

    for conclusions, compensation in (
        (FAILING_CONCLUSIONS, False),
        (JOB_STOPPED_CONCLUSIONS, False),
        (FAILING_CONCLUSIONS, True),
        (JOB_STOPPED_CONCLUSIONS, None),
    ):
        job = ranked(conclusions, compensation)
        if job is not None:
            return job
    return None


def failing_step(job: dict | None) -> dict | None:
    for step in (job or {}).get("steps") or []:
        if step.get("conclusion") == STEP_FAILED:
            return step
    return None


def job_named(jobs: list, stage: str) -> dict | None:
    """The job that is `stage` in another run: the same name, or the same
    name on one side of the reusable-workflow separator."""
    for job in jobs:
        name = job.get("name") or ""
        if name == stage or name.startswith(stage + REUSABLE_JOB_SEPARATOR) or stage.startswith(name + REUSABLE_JOB_SEPARATOR):
            return job
    return None


def candidate_text(candidate: dict) -> str:
    tag, sha = candidate.get("tag"), candidate.get("sha")
    if tag and sha:
        return f"`{tag}` (`{sha}`)"
    if tag:
        return f"`{tag}`"
    if sha:
        return f"`{sha}`"
    return CANDIDATE_UNKNOWN


def candidate_short(candidate: dict) -> str:
    tag, sha = candidate.get("tag"), candidate.get("sha")
    if tag and sha:
        return f"{tag} ({sha[:SHORT_SHA]})"
    if tag:
        return tag
    if sha:
        return sha[:SHORT_SHA]
    return CANDIDATE_UNKNOWN


def run_details(api: GitHubAPI, run: dict, with_failure: bool = True, jobs: list | None = None) -> dict:
    """What the issue says about one run: stage, step, first failure, candidate.

    Reads the run's jobs once (or takes them), the first jobs' logs for the
    candidate, and the failing job's log for the failure line, each log at
    most once. A log that cannot be read leaves its field unknown rather
    than failing the reconciliation: the issue with a hole in it is still
    the signal.
    """
    jobs = api.jobs(run["id"]) if jobs is None else jobs
    logs: dict = {}

    def read(job_id):
        if job_id not in logs:
            logs[job_id] = api.job_log(job_id) or None
        return logs[job_id]

    candidate: dict = {}
    for job in jobs[:CANDIDATE_LOG_JOBS]:
        if "tag" in candidate and "sha" in candidate:
            break
        text = read(job["id"])
        if text is not None:
            candidate = extract_candidate(text, candidate)
    stage = failing_job(jobs) if with_failure else None
    step = failing_step(stage)
    failure = None
    if stage is not None:
        text = read(stage["id"])
        window = (step.get("started_at"), step.get("completed_at")) if step else None
        failure = extract_first_failure(text, window) if text is not None else None
    return {
        "run": run,
        "stage": stage.get("name") if stage else None,
        "stage_url": stage.get("html_url") if stage else None,
        "step": step.get("name") if step else None,
        "failure": failure,
        "candidate": candidate,
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def marker(workflow_id: int) -> str:
    return MARKER.format(workflow_id=workflow_id)


def stamped_attempts(body: str | None, pattern: re.Pattern = _STAMP_RE) -> set:
    """The `(run id, attempt)` pairs the body records; a bare id is its
    first attempt. `pattern` picks the stage stamp or the stall stamp."""
    match = pattern.search(body or "")
    if not match or not match.group(1):
        return set()
    found = set()
    for item in match.group(1).split(","):
        if not item:
            continue
        run_id, _, attempt = item.partition(STAMP_ATTEMPT_SEPARATOR)
        found.add((int(run_id), int(attempt) if attempt else FIRST_ATTEMPT))
    return found


def stamped_runs(body: str | None) -> set:
    return {run_id for run_id, _ in stamped_attempts(body)}


def attempt_of(run: dict) -> tuple:
    return (run["id"], run.get("run_attempt") or FIRST_ATTEMPT)


def stamped_stage(body: str | None) -> str | None:
    match = _STAGE_STAMP_RE.search(body or "")
    return match.group(1) if match else None


def run_link(run: dict) -> str:
    return f"[#{run['run_number']}]({run['html_url']})"


def run_when(run: dict) -> str:
    """`2026-10-21 02:36 UTC` from the run's ISO creation time."""
    created = run.get("created_at") or ""
    if not created:
        return WHEN_UNKNOWN
    return created.replace("T", " ")[:WHEN_LENGTH] + WHEN_SUFFIX


def _inline(text: str) -> str:
    """Log-derived text inside a code span in a table cell. A backtick would
    close the span and a `|` would split the cell, so neither survives."""
    return text.replace("`", "'").replace("|", "\\|")


def failure_text(details: dict) -> str:
    if details.get("failure"):
        return f"`{_inline(details['failure'])}`"
    if details.get("step"):
        return f"step `{_inline(details['step'])}` failed; the log has no failure line this reads"
    return "no failure line found; read the run"


def stage_text(details: dict) -> str:
    if not details.get("stage"):
        return STAGE_UNKNOWN
    if details.get("stage_url"):
        return f"[`{details['stage']}`]({details['stage_url']})"
    return f"`{details['stage']}`"


def render_title(workflow_name: str) -> str:
    return TITLE.format(workflow=workflow_name)


def render_body(workflow_name: str, workflow_id: int, details: dict, streak: list) -> str:
    """The issue as it stands now, rebuilt from the newest failing run and
    the streak behind it; edited in place rather than appended to."""
    run = details["run"]
    lines = [
        marker(workflow_id),
        STAMP.format(run_ids=",".join(stamp_entry(each) for each in streak)),
    ]
    if details.get("stage"):
        lines.append(STAGE_STAMP.format(stage=details["stage"]))
    lines += [
        "",
        f"`{workflow_name}` is failing. Newest failing run: {run_link(run)} ({run_when(run)}).",
        "",
        "| | |",
        "| --- | --- |",
        f"| Stage | {stage_text(details)} |",
        f"| Step | {'`' + details['step'] + '`' if details.get('step') else 'none recorded'} |",
        f"| Candidate | {candidate_text(details['candidate'])} |",
        f"| First failure | {failure_text(details)} |",
        f"| Run | {run['html_url']} |",
        "",
    ]
    if len(streak) > 1:
        lines.append("Failing runs in this streak, oldest first:")
        lines.append("")
        lines.extend(f"- {run_link(each)} ({run_when(each)})" for each in streak)
        lines.append("")
    lines += [
        "This issue closes itself when a later run of the workflow passes with this stage run. "
        f"What each stage does and how to re-run it: {RUNBOOK}.",
    ]
    return "\n".join(lines)


def stall_marker(workflow_id: int) -> str:
    return STALL_MARKER.format(workflow_id=workflow_id)


def stamp_entry(run: dict) -> str:
    run_id, attempt = attempt_of(run)
    return f"{run_id}{STAMP_ATTEMPT_SEPARATOR}{attempt}"


def stall_stamp_entries(body: str | None) -> list:
    """The stall stamp's `<id>:<attempt>` entries in their order, newest
    first; a stamp that does not fit the shape reads as none."""
    match = _STALL_STAMP_RE.search(body or "")
    return [entry for entry in match.group(1).split(",") if entry] if match else []


def extract_red_verdict_build(log_text: str) -> tuple | None:
    """`(build id, Spyglass link)` from the poller's settled-RED line, or None."""
    for raw in log_text.splitlines():
        match = _RED_VERDICT_LINE_RE.match(clean_log_line(raw).strip())
        if match:
            return match.group(1), match.group(2)
    return None


def night_of(run: dict) -> str:
    """The run's night: the UTC date it was created on. Several runs on one
    date are one night."""
    created = run.get("created_at") or ""
    return created[:NIGHT_LENGTH] if created else WHEN_UNKNOWN


def row_night(row: str) -> str:
    """The night a rendered stall row names, its first cell."""
    return row.split(STALL_CELL_SEPARATOR)[1].strip()


def stall_row(repo: str, row: dict) -> str:
    """One RED run of the stall table: the night, the run, the candidate
    commit and the eval build, each a link where the logs gave one."""
    run, sha, build = row["run"], row["candidate"].get("sha"), row["build"]
    return STALL_ROW.format(
        night=night_of(run),
        run=run_link(run),
        candidate=f"[`{sha[:SHORT_SHA]}`]({COMMIT_URL.format(repo=repo, sha=sha)})" if sha else CANDIDATE_UNKNOWN,
        build=f"[{build[0]}]({build[1]})" if build else EVAL_LINK_UNAVAILABLE,
    )


def stall_rows_in(body: str | None) -> list:
    """The table rows an earlier render wrote, newest first, so a rewrite
    keeps the nights it no longer reads the logs of."""
    lines = (body or "").splitlines()
    if STALL_TABLE_RULE not in lines:
        return []
    rows = []
    for line in lines[lines.index(STALL_TABLE_RULE) + 1 :]:
        if not line.startswith("|"):
            break
        rows.append(line)
    return rows


def render_stall_title(nights: int) -> str:
    return STALL_TITLE.format(n=nights)


def render_stall_body(workflow_id: int, stamped: list, rows: list) -> str:
    """The stalled-chain issue: `stamped` and `rows` newest first."""
    return "\n".join(
        [
            stall_marker(workflow_id),
            STALL_STAMP.format(run_ids=",".join(stamped)),
            "",
            STALL_LEAD,
            "",
            STALL_TABLE_HEADER,
            STALL_TABLE_RULE,
            *rows,
            "",
            STALL_FOOTER,
        ]
    )


def render_still_failing(details: dict) -> str:
    run = details["run"]
    return (
        f"Still failing: {run_link(run)} ({run_when(run)}), stage {stage_text(details)}, "
        f"candidate {candidate_text(details['candidate'])}. First failure: {failure_text(details)}"
    )


def render_recovered(details: dict, note: str | None = None, outcome: str = RECOVERED_PASSED) -> str:
    run = details["run"]
    text = f"{run_link(run)} ({run_when(run)}) {outcome.format(candidate=candidate_text(details['candidate']))}."
    if note:
        text += f" {note}"
    return text + " Closing."


def chat_plain(text: str) -> str:
    """Log-derived text in a Chat message: nothing in it toggles formatting."""
    for char, lookalike in CHAT_FORMATTING.items():
        text = text.replace(char, lookalike)
    return CHAT_UNDERSCORE_PAIR_RE.sub(CHAT_UNDERSCORE_LOOKALIKE, text)


def render_chat_broken(workflow_name: str, details: dict, number: int) -> str:
    run = details["run"]
    failure = details.get("failure") or (f"step `{details['step']}` failed" if details.get("step") else "see the run")
    lines = [
        CHAT_BROKEN.format(workflow=workflow_name, stage=details.get("stage") or STAGE_UNKNOWN, failure=chat_plain(failure)),
        CHAT_BROKEN_DETAIL.format(candidate=candidate_short(details["candidate"]), number=number),
        run["html_url"],
    ]
    return "\n".join(lines)


def render_chat_recovered(workflow_name: str, details: dict, number: int, template: str = CHAT_RECOVERED) -> str:
    run = details["run"]
    lines = [
        template.format(
            workflow=workflow_name,
            run_number=run["run_number"],
            candidate=candidate_short(details["candidate"]),
            number=number,
        ),
        run["html_url"],
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# GitHub
# --------------------------------------------------------------------------- #


class GitHubAPI(FlakyGitHubAPI):
    """The flaky-check reporter's client (the retrying base client plus the
    two-hop job-log read and `run`), with the reads and writes this script
    makes. Writes are printed and skipped under `dry_run`.

    The five issue methods below are the shape of the flaky-check and
    broken-main reporters' own, which read their module's `LABEL`, label
    colour and description and marker function directly; making those read
    an attribute would rewrite all five in each sibling, so this client
    carries its own copies with this label and marker.
    """

    def __init__(self, repo: str, token: str, dry_run: bool = False, **kwargs):
        super().__init__(repo, token, user_agent=USER_AGENT, **kwargs)
        self.dry_run = dry_run

    def runs(self, workflow_id: int) -> list:
        """The workflow's newest runs on `main`, every status: a run being
        re-run is not completed, and leaving it out would make the run
        before it the newest. The branch is asked of the endpoint and checked
        again on the answer, so a page that ignores the filter still cannot
        put a branch dispatch in the history."""
        query = urllib.parse.urlencode({"branch": MAIN_BRANCH, "exclude_pull_requests": "true", "per_page": HISTORY_DEPTH})
        batch = self.get(f"/repos/{self.repo}/actions/workflows/{workflow_id}/runs?{query}")
        return [run for run in (batch or {}).get("workflow_runs") or [] if run.get("head_branch") == MAIN_BRANCH]

    def jobs(self, run_id: int) -> list:
        return self.get_object_pages(f"/repos/{self.repo}/actions/runs/{run_id}/jobs", "jobs")

    def issues_for_workflow(self, workflow_id: int, state: str, label: str = LABEL, marker_for=marker) -> list:
        """The issues labelled `label` in `state` that carry this workflow's
        marker (the stage issue's, or the one `marker_for` renders),
        newest-updated first; pull requests, which the endpoint lists too,
        dropped."""
        query = urllib.parse.urlencode(
            {"labels": label, "state": state, "sort": "updated", "direction": "desc", "per_page": ISSUE_PAGE}
        )
        issues = self.get(f"/repos/{self.repo}/issues?{query}") or []
        return [
            issue
            for issue in issues
            if "pull_request" not in issue and marker_for(workflow_id) in (issue.get("body") or "")
        ]

    def ensure_label(self, name: str = LABEL, color: str = LABEL_COLOR, description: str = LABEL_DESCRIPTION) -> None:
        if self.dry_run:
            log(f"dry-run: would ensure label {name}")
            return
        self.request(
            "POST",
            f"/repos/{self.repo}/labels",
            {"name": name, "color": color, "description": description},
            tolerate=(LABEL_EXISTS,),
        )

    def create_issue(self, title: str, body: str, labels: tuple = (LABEL,)) -> dict:
        if self.dry_run:
            log(f"dry-run: would open an issue titled {title!r} labelled {', '.join(labels)}:\n{body}")
            return {"number": DRY_RUN_ISSUE_NUMBER}
        return self.request("POST", f"/repos/{self.repo}/issues", {"title": title, "body": body, "labels": list(labels)})

    def update_issue(self, number: int, **fields) -> None:
        if self.dry_run:
            log(f"dry-run: would update #{number} with {sorted(fields)}" + (f":\n{fields['body']}" if "body" in fields else ""))
            return
        self.request("PATCH", f"/repos/{self.repo}/issues/{number}", fields)

    def comment(self, number: int, body: str) -> None:
        if self.dry_run:
            log(f"dry-run: would comment on #{number}:\n{body}")
            return
        self.request("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body})


# --------------------------------------------------------------------------- #
# Reconciling one workflow
# --------------------------------------------------------------------------- #


def warn(message: str, environ=os.environ, switch: str = ANNOTATE_ENV) -> None:
    """A log line that, when the named switch is on, is also a warning
    annotation on the job. Stdout, where the runner reads workflow commands.

    The same four lines as `warn` in `notify_broken_main.py` and
    `notify_flaky_check.py`, each gated on its own variable; extracting one
    means editing both siblings, so this is a third copy, as the issue
    methods on `GitHubAPI` are.
    """
    if environ.get(switch) == SWITCH_ON:
        print(f"{WARNING_PREFIX}{message}", flush=True)
    log(message)


def run_age(run: dict, now: datetime) -> timedelta | None:
    """How long the run's latest attempt has been going. `run_started_at`
    moves on a re-run and `created_at` does not, so a morning re-run of last
    night's failure is young, not stuck."""
    started = run.get("run_started_at") or run.get("created_at")
    if not started:
        return None
    return now - datetime.strptime(started, RUN_TIME_FORMAT).replace(tzinfo=timezone.utc)


def post(sender: Sender, text: str, dry_run: bool, environ=os.environ) -> None:
    if dry_run:
        log(f"dry-run: would post to chat ({sender.describe()}):\n{text}")
        return
    if not sender.configured:
        warn(f"{NOT_CONFIGURED}; the issue is filed, the chat message is not", environ, CHAT_EXPECTED_ENV)
        return
    if sender.send(text):
        log("posted to chat")
    else:
        warn(f"chat post failed ({sender.describe()}); the issue is filed, the chat message is not", environ, CHAT_EXPECTED_ENV)


def recovery_jobs(name: str, green: dict, issue: dict, jobs_for) -> tuple:
    """`(jobs, note)` when the green run is the recovery the issue is waiting
    for, else `(None, None)` with the reason logged.

    It must not be older than the newest failing run the issue records, and
    the stage that failed must have run in it: a stage that concluded
    skipped or cancelled holds the close, a stage that concluded anything
    else ran (a failure inside a green run is a non-gating job), and a stage
    no job of the green run answers to was renamed between the failure and
    the recovery, which is a close with the note saying so rather than an
    issue nothing can ever close.
    """
    stamped = stamped_runs(issue.get("body"))
    if stamped and green["id"] < max(stamped):
        log(f"{name}: green run #{green['run_number']} is older than the failing run #{issue['number']} records; nothing")
        return None, None
    jobs = jobs_for(green)
    stage = stamped_stage(issue.get("body"))
    if not stage:
        return jobs, None
    job = job_named(jobs, stage)
    if job is None:
        return jobs, f"No job of this run is named `{stage}`, the stage that failed; the workflow has renamed it since."
    conclusion = job.get("conclusion")
    if conclusion in JOB_NOT_RUN_CONCLUSIONS or conclusion is None:
        log(f"{name}: run #{green['run_number']} passed but stage {stage!r} concluded {conclusion}; #{issue['number']} stays open")
        return None, None
    return jobs, None


def reconcile(api: GitHubAPI, workflow: dict, sender: Sender, dry_run: bool, environ=os.environ, now: datetime | None = None) -> str:
    """Bring the workflow's issue into line with its newest reporting run.
    Returns a one-word outcome for the log and the tests."""
    name, workflow_id = workflow["name"], workflow["id"]
    runs = api.runs(workflow_id)
    newest = max(runs, key=lambda run: run["run_number"], default=None)
    if newest is not None and newest.get("status") != COMPLETED:
        age = run_age(newest, now or datetime.now(timezone.utc))
        message = f"{name}: run #{newest['run_number']} is {newest.get('status')}; nothing this sweep"
        if age is not None and age > STUCK_RUN_AFTER:
            warn(f"{message} ({newest['html_url']} has been {newest.get('status')} for {age}, past {STUCK_RUN_AFTER}; the sweep is silent for this workflow until it completes)", environ)
        else:
            log(message)
        return "nothing"
    current = newest_reporting_run(runs)
    if current is None:
        log(f"{name}: no completed run has said anything yet")
        return "nothing"
    decision = decide(current, reporting_history(runs, current))
    # Each run's jobs are read at most once per reconcile, whichever of the
    # stage issue and the stalled-chain issue asks first.
    cache: dict = {}

    def jobs_for(run: dict) -> list:
        if run["id"] not in cache:
            cache[run["id"]] = api.jobs(run["id"])
        return cache[run["id"]]

    outcome = reconcile_stage(api, workflow, current, decision, jobs_for, sender, dry_run, environ)
    if name != EVAL_PIPELINE:
        return outcome
    stall = reconcile_stall(api, workflow, runs, current, decision, jobs_for, sender, dry_run, environ)
    if stall == NOTHING:
        return outcome
    stall = STALL_OUTCOME.format(outcome=stall)
    return stall if outcome == NOTHING else f"{outcome}{OUTCOME_SEPARATOR}{stall}"


def reconcile_stage(api: GitHubAPI, workflow: dict, current: dict, decision: dict, jobs_for, sender: Sender, dry_run: bool, environ=os.environ) -> str:
    """Bring the workflow's stage issue into line with its newest reporting
    run. `jobs_for` reads a run's jobs once per reconcile."""
    name, workflow_id = workflow["name"], workflow["id"]
    open_issues = api.issues_for_workflow(workflow_id, ISSUE_OPEN)
    issue = open_issues[0] if open_issues else None

    # The promotion pipeline's red run has its jobs read once here, for the
    # RED-verdict question and then for the issue. Every other red run's are
    # read when the issue needs them; a green run's only when one is open.
    jobs = jobs_for(current) if decision["kind"] != "green" and name == EVAL_PIPELINE else None
    red_verdict = jobs is not None and red_verdict_night(jobs)
    if red_verdict:
        log(f"{name}: run #{current['run_number']} is red on the eval's RED verdict alone; the gate rejected the candidate, which is no stage failure")

    if decision["kind"] == "green" or red_verdict:
        if issue is None:
            log(f"{name}: green (run #{current['run_number']}), no issue open")
            return "nothing"
        if issue.get("state_reason") == REOPENED:
            log(f"{name}: green (run #{current['run_number']}), but a person reopened #{issue['number']}; leaving it open")
            return "nothing"
        jobs, note = recovery_jobs(name, current, issue, jobs_for)
        if jobs is None:
            return "nothing"
        details = run_details(api, current, with_failure=False, jobs=jobs)
        api.comment(issue["number"], render_recovered(details, note, RECOVERED_RED_VERDICT if red_verdict else RECOVERED_PASSED))
        api.update_issue(issue["number"], state=ISSUE_CLOSED)
        log(f"{name}: run #{current['run_number']} passed; closed #{issue['number']}")
        post(sender, render_chat_recovered(name, details, issue["number"], CHAT_RECOVERED_RED_VERDICT if red_verdict else CHAT_RECOVERED), dry_run, environ)
        return "closed"

    streak = decision["streak"]
    if issue is not None:
        if attempt_of(current) in stamped_attempts(issue.get("body")):
            log(f"{name}: #{issue['number']} already records run #{current['run_number']} attempt {attempt_of(current)[1]}")
            return "nothing"
        details = run_details(api, current, jobs=jobs)
        # Comment first: the body rewrite stamps the attempt, and a comment
        # that failed after it would never be retried, since the next sweep
        # finds the attempt already recorded and writes nothing.
        api.comment(issue["number"], render_still_failing(details))
        api.update_issue(issue["number"], body=render_body(name, workflow_id, details, streak))
        log(f"{name}: run #{current['run_number']} failed again; updated #{issue['number']}")
        return "updated"

    for closed in api.issues_for_workflow(workflow_id, ISSUE_CLOSED):
        if attempt_of(current) in stamped_attempts(closed.get("body")):
            log(f"{name}: run #{current['run_number']} attempt {attempt_of(current)[1]} is recorded on closed #{closed['number']}; someone dealt with it")
            return "nothing"
    details = run_details(api, current, jobs=jobs)
    api.ensure_label()
    created = api.create_issue(render_title(name), render_body(name, workflow_id, details, streak))
    log(f"{name}: run #{current['run_number']} failed; opened #{created['number']}")
    post(sender, render_chat_broken(name, details, created["number"]), dry_run, environ)
    return "opened"


def stall_details(api: GitHubAPI, run: dict, jobs: list) -> dict:
    """One RED night's row: the candidate from the first jobs' logs, and
    the eval build from the step-5 log's verdict line. A log that cannot be
    read leaves its cell saying so; the issue still files."""
    candidate = run_details(api, run, with_failure=False, jobs=jobs)["candidate"]
    wait = next((job for job in jobs if is_eval_wait(job)), None)
    text = api.job_log(wait["id"]) if wait is not None else None
    return {"run": run, "candidate": candidate, "build": extract_red_verdict_build(text) if text else None}


def reconcile_stall(api: GitHubAPI, workflow: dict, runs: list, current: dict, decision: dict, jobs_for, sender: Sender, dry_run: bool, environ=os.environ) -> str:
    """Bring the promotion pipeline's stalled-chain issue into line with
    its newest reporting run. Returns `nothing`, `opened`, `updated` or
    `closed`.

    A settled-RED night walks the RED runs in a row behind it, newest
    first, passing over up to `STALL_SKIP_RUNS_MAX` runs that skip the eval
    step (green, step 5 skipped: a `skip_promotion` run or a hand-dispatched
    `skip_pipeline` one), and stops at the first run that
    is not one (green, or red on a stage)
    or that the open issue already stamps; jobs are read only that far.
    The count is the number of distinct UTC dates those runs were created
    on, so a hand dispatch beside the scheduled run is no second night. A
    run whose eval passed closes the issue, whatever happened to the run
    after the gate. A night that fails on a stage before the eval passes
    leaves it alone.
    """
    name, workflow_id = workflow["name"], workflow["id"]
    if decision["kind"] == "green":
        red_night, passed = False, None
    else:
        jobs = jobs_for(current)
        red_night, passed = red_verdict_night(jobs), eval_passed(jobs)
        if not red_night and not passed:
            log(f"{name}: run #{current['run_number']} failed on a stage, not on the verdict; the stalled-chain issue is left as it is")
            return NOTHING
    open_issues = api.issues_for_workflow(workflow_id, ISSUE_OPEN, STALL_LABEL, stall_marker)
    issue = open_issues[0] if open_issues else None
    stamped = stamped_attempts(issue.get("body"), _STALL_STAMP_RE) if issue else set()

    if not red_night:
        if issue is None:
            return NOTHING
        if issue.get("state_reason") == REOPENED:
            log(f"{name}: run #{current['run_number']} is no RED night, but a person reopened #{issue['number']}; leaving it open")
            return NOTHING
        if stamped and current["id"] < max(run_id for run_id, _ in stamped):
            log(f"{name}: run #{current['run_number']} is older than the RED nights #{issue['number']} records; nothing")
            return NOTHING
        if passed is None:
            passed = eval_passed(jobs_for(current))
        if not passed:
            log(f"{name}: run #{current['run_number']} did not pass the release-candidate eval (it did not run); #{issue['number']} stays open")
            return NOTHING
        stage_failed = decision["kind"] != "green"
        comment = STALL_CLOSED_STAGE_FAILED_COMMENT if stage_failed else STALL_CLOSED_COMMENT
        api.comment(issue["number"], comment.format(run=run_link(current), when=run_when(current)))
        api.update_issue(issue["number"], state=ISSUE_CLOSED)
        log(f"{name}: run #{current['run_number']} passed the eval; closed stalled-chain #{issue['number']}")
        post(sender, (CHAT_PAST_GATE if stage_failed else CHAT_MOVING).format(run_number=current["run_number"], url=ISSUE_URL.format(repo=api.repo, number=issue["number"])), dry_run, environ)
        return "closed"

    if issue is None:
        # Before the walk, so a stall a person closed costs one listing a
        # sweep rather than a job list per night.
        for closed in api.issues_for_workflow(workflow_id, ISSUE_CLOSED, STALL_LABEL, stall_marker):
            if current["id"] in {run_id for run_id, _ in stamped_attempts(closed.get("body"), _STALL_STAMP_RE)}:
                log(f"{name}: run #{current['run_number']} is recorded on closed #{closed['number']}; someone dealt with it")
                return NOTHING

    stamped_ids = {run_id for run_id, _ in stamped}
    new = []
    skipped = 0
    for run in [current, *reporting_history(runs, current)]:
        if run["id"] in stamped_ids:
            break
        jobs = jobs_for(run)
        # A night that skips the eval step, a `skip_promotion` night or a
        # hand-dispatched `skip_pipeline` one, says nothing about the gate, so
        # it neither counts nor ends the streak, up to `STALL_SKIP_RUNS_MAX`
        # such runs; at one more the walk stops.
        if run["conclusion"] not in FAILING_CONCLUSIONS:
            if eval_skipped(jobs) and skipped < STALL_SKIP_RUNS_MAX:
                skipped += 1
                continue
            break
        if not red_verdict_night(jobs):
            break
        new.append((run, jobs))

    new_nights = {night_of(run) for run, _ in new}
    if issue is not None:
        if not new:
            log(f"{name}: stalled-chain #{issue['number']} already records run #{current['run_number']}")
            return NOTHING
        old_rows = stall_rows_in(issue.get("body"))
        old_nights = {row_night(row) for row in old_rows}
        nights = len(old_nights | new_nights)
        rows = [stall_row(api.repo, stall_details(api, run, jobs)) for run, jobs in new]
        entries = [stamp_entry(run) for run, _ in new]
        # Comment first, as on the stage issue: the rewrite stamps the run. A
        # RED run on a night already counted adds its row and says nothing.
        if nights > len(old_nights):
            api.comment(issue["number"], STALL_COMMENT.format(runs=", ".join(run_link(run) for run, _ in new), n=nights))
        api.update_issue(
            issue["number"],
            title=render_stall_title(nights),
            body=render_stall_body(workflow_id, entries + stall_stamp_entries(issue.get("body")), rows + old_rows),
        )
        log(f"{name}: run #{current['run_number']} rejected again; stalled-chain #{issue['number']} now counts {nights} nights")
        return "updated"

    nights = len(new_nights)
    if nights < STALL_NIGHTS:
        log(f"{name}: {nights} settled-RED night{'' if nights == 1 else 's'} in a row; {STALL_NIGHTS} open the stalled-chain issue")
        return NOTHING
    rows = [stall_row(api.repo, stall_details(api, run, jobs)) for run, jobs in new]
    api.ensure_label(STALL_LABEL, STALL_LABEL_COLOR, STALL_LABEL_DESCRIPTION)
    created = api.create_issue(
        render_stall_title(nights),
        render_stall_body(workflow_id, [stamp_entry(run) for run, _ in new], rows),
        (STALL_LABEL, TRIAGE_LABEL),
    )
    log(f"{name}: {nights} settled-RED nights in a row; opened stalled-chain #{created['number']}")
    post(sender, CHAT_STALLED.format(n=nights, url=ISSUE_URL.format(repo=api.repo, number=created["number"])), dry_run, environ)
    return "opened"


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def watched_workflows(api: GitHubAPI) -> list:
    by_name = {workflow["name"]: workflow for workflow in api.workflows()}
    missing = [name for name in WATCHED_WORKFLOWS if name not in by_name]
    if missing:
        raise RuntimeError(f"watched workflows not found in the repository (renamed?): {', '.join(missing)}")
    return [by_name[name] for name in WATCHED_WORKFLOWS]


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--run-id", type=int, help="reconcile the workflow this completed run belongs to")
    which.add_argument("--sweep", action="store_true", help="reconcile every watched workflow")
    parser.add_argument("--dry-run", action="store_true", help="print what would be written and posted; write nothing")
    parser.add_argument("--repo", default=os.environ.get(REPO_ENV, DEFAULT_REPO))
    return parser.parse_args(argv)


def main(argv=None, environ=os.environ, api: GitHubAPI | None = None, opener=None) -> int:
    args = parse_args(argv)
    if api is None:
        token = next((environ[name] for name in TOKEN_ENVS if environ.get(name)), None)
        if not token:
            log(f"{' or '.join(TOKEN_ENVS)} is not set; for a hand run: GH_TOKEN=\"$(gh auth token)\"")
            return 1
        api = GitHubAPI(args.repo, token, dry_run=args.dry_run)
    sender = Sender.from_env(environ, opener) if opener else Sender.from_env(environ)
    if not sender.configured:
        warn(f"{NOT_CONFIGURED} ({SPACE_ENV}={'set' if environ.get(SPACE_ENV) else 'unset'}, {TOKEN_ENV}={'set' if environ.get(TOKEN_ENV) else 'unset'})", environ, CHAT_EXPECTED_ENV)
    try:
        if args.sweep:
            targets = watched_workflows(api)
        else:
            run = api.run(args.run_id)
            if run.get("name") not in WATCHED_WORKFLOWS:
                log(f"run {args.run_id} belongs to {run.get('name')!r}, which is not watched; nothing to do")
                return 0
            if run.get("head_branch") != MAIN_BRANCH:
                log(f"run {args.run_id} is on {run.get('head_branch')!r}, not a {MAIN_BRANCH} run; nothing to do")
                return 0
            targets = [{"id": run["workflow_id"], "name": run["name"]}]
    except (RuntimeError, OSError) as exc:
        # Nothing can be reconciled without the targets. A missing name or a
        # request GitHub refused is ours to fix and reds the job; a read that
        # failed past the client's retries is an incident, logged and
        # annotated, and the next sweep is fifteen minutes away.
        log(f"error: {exc}")
        if isinstance(exc, RuntimeError) or is_refusal(exc):
            return 1
        warn(f"{exc}; nothing reconciled this sweep, the next one retries", environ)
        return 0
    failed, broken = [], []
    for workflow in targets:
        # One workflow's failure, read or write, must not stop the rest:
        # `scripts/notify_broken_main.py` sweeps the same way, for the same
        # reasons. `urllib.error.HTTPError` and `URLError` are `OSError`s.
        try:
            outcome = reconcile(api, workflow, sender, args.dry_run, environ)
            log(f"{workflow['name']}: {outcome}")
        except Exception as exc:  # noqa: BLE001 - every failure is logged and classified below
            log(f"{workflow['name']}: {type(exc).__name__}: {exc}")
            (broken if is_refusal(exc) else failed).append(workflow["name"])
    if failed:
        # Annotated, not a red: an API incident that outlasts the client's
        # retries would otherwise red the schedule every fifteen minutes and
        # mail the cron line's last editor each time, for a fault nobody here
        # can fix. The next sweep retries.
        warn(f"Reconciling {', '.join(failed)} failed; the rest were reconciled, and the next sweep retries", environ)
    if broken:
        log(f"GitHub refused a request while reconciling {', '.join(broken)}; that is a fault in this repository")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
