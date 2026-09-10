#!/usr/bin/env python3
"""Track a check that failed and then passed on a re-run of the same commit.

A re-run that turns green without a new commit is a flake by construction: the
tree did not change between the two attempts, so whatever failed the first one
was not the code. Nothing records that today. The person who clicked re-run
moves on once the check is green, the failed attempt drops off the pull request
page behind the successful one, and the next person to hit the same failure
starts from nothing. `Python Unit Tests` failed three attempts across two runs
in two days in September 2026 (34233173585 once, 34297433610 twice) on pull
requests that did not touch Python, each attempt naming a different test of
the same class, and each re-run was an isolated event until someone read the
logs together and found one cause behind them.

So this writes it down. `.github/workflows/flaky-check-notify.yml` hands it a
run id and an attempt number whenever a watched workflow completes an attempt
after its first, and it decides whether the pair of attempts is a flake:

    attempt N passed, an earlier attempt failed, same commit -> record it
    anything else                                            -> nothing to say

Every earlier failed attempt, not only the one just before: a check that took
two re-runs to pass failed twice, each failure is a row, and the first one's
log is the half of the evidence a per-class fingerprint exists to collect. A
cancelled attempt in the chain is skipped rather than ending it.

One issue per flaky *container*, not per occurrence and not per run. The failed
attempts' job logs are read for the test ids that failed (unittest, go test and
pytest output are recognised), each id is reduced to its container -- the
unittest class, the top-level Go test, the pytest file -- and every container
gets an issue of its own keyed on the workflow plus that container. Keyed on
the id, a flaky shared fixture that surfaces through whichever test happens to
run first would file each surfacing separately, which is what the two-run
episode above would have got; keyed on the whole set of containers a run
failed, the same flake would file a second issue whenever another failure
happened alongside it. Each occurrence keeps the ids of its own container and
the issue lists their union. When no test id can be found the failing job and
step names stand in, one issue per job-and-step. A flake that recurs adds a
row to the issue it already has, so the issue accumulates the evidence --
dates, runs, pull requests, who re-ran -- that finding the cause needs, and a
reader sees one issue with three occurrences rather than three re-runs nobody
connected.

It never closes an issue. A flake is fixed by a change, not by a green run, and
every green run of a flaky check is exactly what a flake looks like from the
outside. Close it by hand when the cause is fixed; a recurrence after that opens
a fresh issue that links back to the closed one.

It reconciles rather than appends. Each occurrence is stored in the issue body
as a hidden line, the visible table is rebuilt from those lines on every update,
and an occurrence is identified by its run id and failed attempt -- so handling
the same event twice cannot add a second row, and a hand-edited body is restored
by the next occurrence. The title is written once and left alone after that,
so a rename that says what the cause turned out to be survives.

What this trusts: that a re-run is a human's decision. The workflow acts on
`run_attempt > 1`, which only a re-run produces, and this repository re-runs
nothing automatically. If something ever does, every retry it makes on a real
break becomes a row here.

Setup: none. It writes to this repository with the workflow's own `GITHUB_TOKEN`
and creates the `ci:flaky` label the first time it needs it.

Run:  python3 scripts/notify_flaky_check.py --run-id 123456789 --attempt 2 --dry-run
Test: cd scripts && python3 -m unittest test_notify_flaky_check
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from github_api import (
    API_ROOT,
    REQUEST_ATTEMPTS,
    REQUEST_RETRY_SECONDS,
    GitHubAPI as BaseGitHubAPI,
    _rate_limited,
    _retry_delay,
    log,
)

# What a first attempt has to have concluded for its re-run's green to mean
# anything. `cancelled` is a person or a concurrency group stopping the run, not
# the run failing, and `startup_failure` is a workflow file that will not parse,
# which a re-run cannot fix -- neither is a flake.
FAILING_CONCLUSIONS = frozenset({"failure", "timed_out"})
SUCCESS = "success"

# A job that hits its `timeout-minutes` is reported as `cancelled` by the
# jobs API while the run around it concludes `failure`, and a matrix leg
# stopped because a sibling failed reads `cancelled` too. So when no job of a
# failed attempt carries a failing conclusion, every job that did not pass
# or skip stands in, and the step that was running when it stopped names it.
JOB_FALLBACK_EXCLUDED = frozenset({SUCCESS, "skipped", None})
STEP_FAILING_CONCLUSIONS = FAILING_CONCLUSIONS | frozenset({"cancelled"})

ISSUE_OPEN = "open"
ISSUE_CLOSED = "closed"
UNKNOWN_LOGIN = "unknown"

# Lines in a job log that name what failed, each with one capture group holding
# the identifier. Read in order; the first family that matches anything wins,
# so a suite that prints both a pytest and a unittest style line does not get
# counted twice.
TEST_ID_PATTERNS = (
    # unittest: `FAIL: test_x (module.Class.test_x)` / `ERROR: test_x (...)`,
    # with a subTest's label and parameters after it: `... [alpha]`,
    # `... (conclusion='cancelled')`, `... [lbl] (k=1)`. Neither is part of
    # the id; one test with forty subTests is one test.
    re.compile(r"^(?:FAIL|ERROR): (\S+ \([\w.]+\))(?: \[.*\])?(?: \(.*\))?$"),
    # go test: `--- FAIL: TestX (0.01s)`, indented for subtests
    re.compile(r"^\s*--- FAIL: (\S+)"),
    # pytest: `FAILED tests/test_x.py::test_y - AssertionError`, and
    # `ERROR tests/test_x.py::test_y - ...` for a fixture that failed to set
    # up, which is the shape a flaky fixture takes.
    re.compile(r"^(?:FAILED|ERROR) (\S+::\S+)"),
)

# Every line of a downloaded job log starts with an ISO timestamp, and the
# first line of the file also carries a UTF-8 byte order mark.
LOG_LINE_PREFIX = re.compile(r"^﻿?\d{4}-\d{2}-\d{2}T[\d:.]+Z ")

# How a test id maps to the container the issue is keyed on. Each pattern's
# first group is the container; an id no pattern matches is its own container.
CONTAINER_PATTERNS = (
    # unittest class and module fixtures name the container itself:
    # `setUpClass (pkg.module.Class)` -> `pkg.module.Class`. Before the
    # generic pattern, which would strip `Class` as though it were a method.
    re.compile(r"^(?:setUpClass|tearDownClass|setUpModule|tearDownModule) \(([\w.]+)\)$"),
    # unittest `test_x (pkg.module.Class.test_x)` -> `pkg.module.Class`
    re.compile(r"^\S+ \(([\w.]+)\.\w+\)$"),
    # pytest `path/test_x.py::Class::test_y` -> `path/test_x.py`. Before the
    # Go pattern, whose `/` would otherwise cut the path at its first segment.
    re.compile(r"^([^:\s]+)::"),
    # go `TestX/sub/case` -> `TestX`
    re.compile(r"^([^/\s]+)/"),
)

# How many identifiers one fingerprint keeps, how many ids one occurrence
# keeps, how many occurrences one issue keeps, and how long an id may be. A
# run whose whole suite failed names hundreds of ids, and past a handful the
# extra ones add nothing to the key. Together the four caps keep the body
# under GitHub's issue-body limit -- `test_a_body_at_every_cap_fits_github`
# renders the worst case and checks -- because an update over the limit is
# refused and every later occurrence of the fingerprint lost with it. Ids
# come out of a log the pull request's own code wrote, so besides being
# bounded they have the markdown and comment delimiters that could break out
# of a code span or the hidden lines neutralised (`_plain`).
GITHUB_ISSUE_BODY_LIMIT = 65536
MAX_OCCURRENCE_IDS = 8
MAX_OCCURRENCES = 20
MAX_ID_LENGTH = 160

# How many containers one failed attempt may key on before the failure is
# treated as the job's rather than the tests': an attempt that failed sixty
# classes is a broken environment, and sixty issues for it would be noise
# that hides the flakes. Past this the attempt keys on job and step names.
MAX_CONTAINERS_PER_ATTEMPT = 5

# Control characters in log text: stripped, because JSON escapes each one to
# six characters whatever `ensure_ascii` says, and none belongs in a test id.
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

DEFAULT_REPO = "gke-labs/kube-agents"
USER_AGENT = "kube-agents-notify-flaky-check"

# The cap applies to what is stored and shown, not to what the container is
# derived from: a fifth of this repository's unittest ids are longer than
# MAX_ID_LENGTH, and a class name read off a truncated id is the truncated
# id. In memory an id is bounded only against a pathological log line.
MAX_RAW_ID_LENGTH = 4096

# How undecodable bytes in a job log are handled.
DECODE_ERRORS = "replace"
COMMENT_CLOSE = "-->"
COMMENT_CLOSE_NEUTRAL = "-- >"
# HTML5 parsers also close a comment at `--!>`.
COMMENT_CLOSE_BANG = "--!>"
COMMENT_CLOSE_BANG_NEUTRAL = "--! >"

# A matrix leg's job name carries its parameters: `validate (macos-14)`.
# The fallback fingerprint drops them, so the same flake on two legs of one
# matrix is one issue, the way two legs failing the same test already are.
MATRIX_SUFFIX = re.compile(r"\s*\(.*\)$")

# The attempt number a re-run produces; anything below it has no earlier
# attempt to compare against.
FIRST_RERUN_ATTEMPT = 2
PULL_STATE_ALL = "all"
# What the commit-to-pulls lookup answers for a commit GitHub cannot place.
NOT_FOUND_STATUSES = (404, 422)

# Hex characters of the fingerprint digest kept in the issue marker.
FINGERPRINT_DIGEST_LENGTH = 12

# Page size for the list endpoints. Job listings and the pull-request lookup
# read one page: a run with more than a hundred jobs, or a branch with a
# hundred pull requests, is not a state worth writing pagination for. Issue
# listings read every page, because the closed list only grows and the link
# back to an old recurrence depends on it.
PAGE_SIZE = 100

# Status codes the log fetch retries, besides what `_rate_limited` names.
SERVER_ERROR_MIN = 500
TOO_MANY_REQUESTS = 429

# A job log is read through a redirect to blob storage, and the redirect target
# is pre-signed: following it with the GitHub token attached is refused with
# 401, so the two steps are separate requests and the redirect is recognised
# by its status.
REDIRECT_STATUS_MIN = 300
REDIRECT_STATUS_MAX = 399
LOG_ENCODING = "utf-8"

# Rendering: the abbreviated commit in a table cell, the date part of an ISO
# timestamp, and the occurrence count a --dry-run shows for the comment it
# would post if an issue were already open (the smallest count that comment
# can carry).
SHORT_SHA_LENGTH = 7
DATE_LENGTH = len("2026-09-09")
DRY_RUN_COMMENT_COUNT = 2

GITHUB_WEB_ROOT = "https://github.com"
ACCEPT_JSON = "application/vnd.github+json"

LABEL = "ci:flaky"
LABEL_COLOR = "fbca04"
LABEL_DESCRIPTION = "A check failed and passed when re-run on the same commit"

MARKER_PREFIX = "<!-- flaky-check "
OCCURRENCE_PREFIX = "<!-- flaky-occurrence "
OCCURRENCE_SUFFIX = " -->"

WORKFLOW_FILE = ".github/workflows/flaky-check-notify.yml"


# --------------------------------------------------------------------------- #
# Deciding whether the pair of attempts is a flake
# --------------------------------------------------------------------------- #


def failed_before(current, earlier):
    """The attempts in `earlier` that failed on `current`'s commit, oldest first.

    A cancelled attempt is skipped, not a stop: attempt 1 red, 2 cancelled, 3
    green is still a same-commit flake. The commit check cannot fail for a
    re-run, which is by definition the same commit, but the whole argument
    rests on it, so it is checked rather than assumed.
    """
    return sorted(
        (
            attempt
            for attempt in earlier
            if attempt is not None
            and attempt.get("conclusion") in FAILING_CONCLUSIONS
            and attempt.get("head_sha") == current.get("head_sha")
        ),
        key=lambda attempt: attempt["run_attempt"],
    )


def decide(current, failed_attempts):
    """Why `current` is not a flake, or None when it is one.

    `failed_attempts` is what `failed_before` returned for the attempts ahead
    of it. The reason string is for the log; the workflow's `if:` has already
    filtered most of these, and reading them back off the API is what makes a
    hand-run on the wrong run id harmless.
    """
    if current.get("run_attempt", 1) < FIRST_RERUN_ATTEMPT:
        return "first attempt; nothing to compare against"
    if current.get("conclusion") != SUCCESS:
        return f"attempt {current['run_attempt']} concluded {current.get('conclusion')}, not success"
    if not failed_attempts:
        return "no earlier attempt failed on this commit"
    return None


# --------------------------------------------------------------------------- #
# Fingerprinting what failed
# --------------------------------------------------------------------------- #


def strip_log_prefix(line):
    return LOG_LINE_PREFIX.sub("", line, count=1)


def _neutral(text):
    """Log-derived text with the delimiters that would close a code span or a
    hidden comment replaced. The log was written by the pull request's own
    code, so nothing in it is trusted to render as markdown."""
    # The double quote and the backslash are replaced as well: the hidden
    # occurrence lines are JSON, where each would escape to two characters
    # and the body-size arithmetic would be off by up to a factor of two.
    return (
        CONTROL_CHARS.sub(" ", text[:MAX_RAW_ID_LENGTH])
        .replace("`", "'")
        .replace('"', "'")
        .replace("\\", "/")
        .replace(COMMENT_CLOSE_BANG, COMMENT_CLOSE_BANG_NEUTRAL)
        .replace(COMMENT_CLOSE, COMMENT_CLOSE_NEUTRAL)
    )


def _plain(text):
    """`_neutral`, bounded: the form that is stored in and shown on an issue."""
    return _neutral(text)[:MAX_ID_LENGTH]


def extract_test_ids(log_text):
    """The identifiers of the tests a job log says failed, sorted, or []."""
    lines = [strip_log_prefix(line).rstrip() for line in log_text.splitlines()]
    for pattern in TEST_ID_PATTERNS:
        found = {_neutral(match.group(1)) for line in lines for match in [pattern.match(line)] if match}
        if found:
            return sorted(found)
    return []


def container(test_id):
    """The class, top-level test or file a test id belongs to, or the id."""
    for pattern in CONTAINER_PATTERNS:
        match = pattern.match(test_id)
        if match:
            return match.group(1)
    return test_id


def failed_steps(job):
    return [step["name"] for step in job.get("steps") or [] if step.get("conclusion") in STEP_FAILING_CONCLUSIONS]


def all_test_ids(failed_jobs):
    """Every test id the failing jobs' logs name, pooled and sorted."""
    return sorted({test_id for job in failed_jobs for test_id in job["test_ids"]})


def job_keys(job):
    """The job/step keys one failing job files under when its log names no
    test: one per failed step, or the job itself when no step is marked."""
    name = MATRIX_SUFFIX.sub("", job["name"])
    steps = failed_steps(job)
    return [f"{name} / {step}" for step in steps] or [name]


def keys(failed_jobs):
    """What one failed attempt files under, as key -> the test ids behind it.
    Each key is one issue.

    `failed_jobs` is one attempt's failing jobs, dicts with `name`, `steps`
    and `test_ids`. A job whose log names tests files under their containers
    -- pooled across jobs, so two legs of a matrix failing the same test are
    one flake, and two attempts failing different tests of one class are one
    flake too. A job whose log names none files under its job and step, in
    the same attempt as the others: a leg that died downloading is not the
    same flake as the leg that failed a test. When one attempt names more
    containers than MAX_CONTAINERS_PER_ATTEMPT the failure is the jobs', not
    the tests', and every job files under its name with the ids it named.
    """
    with_ids = [job for job in failed_jobs if job["test_ids"]]
    without = [job for job in failed_jobs if not job["test_ids"]]
    ids = all_test_ids(with_ids)
    containers = sorted({container(test_id) for test_id in ids})
    found = {}
    if len(containers) > MAX_CONTAINERS_PER_ATTEMPT:
        without = failed_jobs
    else:
        for key in containers:
            found[key] = [test_id for test_id in ids if container(test_id) == key]
    for job in without:
        for key in job_keys(job):
            found[key] = sorted(set(found.get(key, [])) | set(job["test_ids"]))
    return found


def signature(failed_jobs):
    """The keys of `keys`, sorted."""
    return sorted(keys(failed_jobs))


def digest(workflow_id, lines):
    material = f"{workflow_id}\n" + "\n".join(lines)
    return hashlib.sha256(material.encode()).hexdigest()[:FINGERPRINT_DIGEST_LENGTH]


def issue_marker(workflow_id, fingerprint):
    """The hidden line that ties an issue to one distinct failure of one
    workflow. Matching on this rather than on the title means a renamed issue
    is still found."""
    return f"{MARKER_PREFIX}workflow={workflow_id} fingerprint={fingerprint}{OCCURRENCE_SUFFIX}"


def workflow_marker(workflow_id):
    return f"{MARKER_PREFIX}workflow={workflow_id} "


# --------------------------------------------------------------------------- #
# Occurrences
# --------------------------------------------------------------------------- #


def occurrence(current, previous, pull_request, test_ids=()):
    """One flake, as the row the issue keeps for it.

    `pull_request` is the number the commit belongs to, or None on a push.
    `test_ids` are what this attempt's logs named, kept per occurrence so the
    issue can list every test the flake has surfaced through.
    `triggering_actor` on the green attempt is whoever clicked re-run, which
    on a re-run is a person -- unlike `actor`, which is who opened the pull
    request or, on `main`, Tide.
    """
    return {
        "failed": [_plain(test_id) for test_id in list(test_ids)[:MAX_OCCURRENCE_IDS]],
        "run_id": current["id"],
        "run_number": current["run_number"],
        "failed_attempt": previous["run_attempt"],
        "passed_attempt": current["run_attempt"],
        "sha": current["head_sha"],
        "date": (previous.get("run_started_at") or previous.get("created_at") or "")[:DATE_LENGTH],
        "pull_request": pull_request,
        # A fork names its own branch, so it is log-grade text and is bounded
        # here, before it reaches the hidden occurrence line as well as the
        # table.
        "branch": _plain(current.get("head_branch") or ""),
        "rerun_by": (current.get("triggering_actor") or {}).get("login") or UNKNOWN_LOGIN,
    }


def occurrence_key(record):
    return (record["run_id"], record["failed_attempt"])


def occurrences_in(body):
    """The occurrences an issue body already records, oldest first."""
    found = []
    for line in (body or "").splitlines():
        line = line.strip()
        if line.startswith(OCCURRENCE_PREFIX) and line.endswith(OCCURRENCE_SUFFIX):
            raw = line[len(OCCURRENCE_PREFIX) : -len(OCCURRENCE_SUFFIX)]
            try:
                found.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return found


def merge_occurrences(existing, new_records):
    """`existing` plus whichever of `new_records` are not already there.

    Returns the merged list, oldest first and trimmed to the most recent
    MAX_OCCURRENCES, and the records that were in fact new. Identity is the
    run and its failed attempt, so the same event handled twice is one row.
    """
    keys = {occurrence_key(record) for record in existing}
    added = []
    for record in new_records:
        if occurrence_key(record) not in keys:
            keys.add(occurrence_key(record))
            added.append(record)
    return (list(existing) + added)[-MAX_OCCURRENCES:], added


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _attempt_url(repo, record, attempt):
    return f"{GITHUB_WEB_ROOT}/{repo}/actions/runs/{record['run_id']}/attempts/{attempt}"


def _commit_link(repo, sha):
    return f"[`{sha[:SHORT_SHA_LENGTH]}`]({GITHUB_WEB_ROOT}/{repo}/commit/{sha})"


def failed_across(records):
    """Every test id any occurrence named, sorted; the issue's "what failed"."""
    return sorted({test_id for record in records for test_id in record.get("failed") or []})


def _where(record):
    """The pull request, bare so GitHub autolinks it, or the branch."""
    if record.get("pull_request"):
        return f"#{record['pull_request']}"
    return f"`{_cell(record['branch'])}`" if record.get("branch") else ""


def _cell(text):
    return str(text).replace("|", "\\|")


def render_title(workflow_name, lines):
    first = _plain(lines[0]) if lines else "unknown failure"
    return f"🎲 flaky check: {workflow_name}: {first}"


def render_body(repo, workflow_name, lines, records, marker, previously=None):
    """The issue body, rebuilt from scratch on every update."""
    count = len(records)
    plural = "" if count == 1 else "s"
    out = [
        f"🎲 **`{workflow_name}`** failed and then passed on a re-run of the same commit, {count} time{plural}.",
        "",
        "The tree did not change between the two attempts, so the code was not the cause. "
        "Each row is one failed-then-passed pair; the run link opens the failed attempt.",
        "",
        "**What failed**",
        "",
    ]
    # The key lines (classes, files, or job/step names) and, when the logs
    # named tests, every test any occurrence surfaced through.
    tests = failed_across(records)
    out += [f"- `{_cell(_plain(line))}`" for line in lines] or ["- (no test id or step name could be read from the logs)"]
    if tests and tests != lines:
        out += ["", "Tests named across the occurrences:", ""]
        out += [f"- `{_cell(_plain(test_id))}`" for test_id in tests[:MAX_OCCURRENCE_IDS]]
        if len(tests) > MAX_OCCURRENCE_IDS:
            out.append(f"- (+{len(tests) - MAX_OCCURRENCE_IDS} more)")
    out += [
        "",
        "| date | run | commit | where | re-run by |",
        "| --- | --- | --- | --- | --- |",
    ]
    for record in records:
        out.append(
            f"| {record['date']} "
            f"| [{record['run_number']} attempt {record['failed_attempt']} → {record['passed_attempt']}]"
            f"({_attempt_url(repo, record, record['failed_attempt'])}) "
            f"| {_commit_link(repo, record['sha'])} "
            f"| {_where(record)} "
            f"| {_cell(record['rerun_by'])} |"
        )
    out.append("")
    if count >= MAX_OCCURRENCES:
        out += [f"The table keeps at most the {MAX_OCCURRENCES} most recent occurrences; any earlier ones are not shown.", ""]
    if previously:
        out += [f"Previously tracked in #{previously['number']}, which was closed; this is a recurrence.", ""]
    out += [
        f"Opened and updated automatically by "
        f"[`flaky-check-notify.yml`]({GITHUB_WEB_ROOT}/{repo}/blob/main/{WORKFLOW_FILE}) "
        f"whenever a re-run of `{workflow_name}` passes where an earlier attempt on the same commit failed. "
        "It never closes an issue: a flake is fixed by a change, not by a green run. "
        "Close it when the cause is fixed; a recurrence after that opens a new issue that links back here. "
        f"Keep the `{LABEL}` label on it: the label is how the next occurrence finds this issue.",
        "",
        marker,
    ]
    # ensure_ascii=False: GitHub's limit counts characters, and an escaped
    # non-ASCII character is six of them.
    out += [
        f"{OCCURRENCE_PREFIX}{json.dumps(record, sort_keys=True, ensure_ascii=False)}{OCCURRENCE_SUFFIX}"
        for record in records
    ]
    return "\n".join(out)


def render_comment(repo, added, count):
    """What to add when an existing issue gains occurrences. An issue body
    edit notifies nobody, so without this a subscriber never learns the flake
    is still live. `added` is one run's worth of new rows -- one per failed
    attempt -- so it shares a pull request and a re-runner."""
    first = added[0]
    where = _where(first)
    where = f" on {where}" if where else ""
    attempts = " and ".join(
        f"[run {record['run_number']} attempt {record['failed_attempt']}]({_attempt_url(repo, record, record['failed_attempt'])})"
        for record in added
    )
    return f"Flaked again: {attempts}{where}, re-run by {_cell(first['rerun_by'])}.\n\n{count} occurrences."


# --------------------------------------------------------------------------- #
# GitHub API
# --------------------------------------------------------------------------- #


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface a 3xx as the HTTPError it is rather than following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHubAPI(BaseGitHubAPI):
    def __init__(
        self,
        repo,
        token,
        root=API_ROOT,
        user_agent=USER_AGENT,
        opener=urllib.request.urlopen,
        sleep=time.sleep,
        redirect_opener=None,
    ):
        super().__init__(repo=repo, token=token, root=root, user_agent=user_agent, opener=opener, sleep=sleep)
        self.redirect_opener = redirect_opener or urllib.request.build_opener(_NoRedirect).open

    def run(self, run_id):
        return self.get(f"/repos/{self.repo}/actions/runs/{run_id}")

    def attempt(self, run_id, number):
        """The run as of one attempt. 404 for an attempt that does not exist."""
        return self.get(f"/repos/{self.repo}/actions/runs/{run_id}/attempts/{number}", tolerate=(404,))

    def attempt_jobs(self, run_id, number):
        query = urllib.parse.urlencode({"per_page": PAGE_SIZE})
        return self.get(f"/repos/{self.repo}/actions/runs/{run_id}/attempts/{number}/jobs?{query}")["jobs"]

    def job_log(self, job_id):
        """The plain-text log of one job, or "" when it cannot be read.

        GitHub answers with a 302 to a pre-signed blob URL. The token must not
        travel with the second request -- the blob store refuses it -- so the
        redirect is taken by hand: one authenticated request that is not
        followed, then one anonymous request to wherever it pointed. A log
        that is gone (expired, or the job never produced one) is not a reason
        to fail; the fingerprint falls back to the job and step names.
        """
        request = urllib.request.Request(
            f"{self.root}/repos/{self.repo}/actions/jobs/{job_id}/logs",
            headers={
                "Accept": ACCEPT_JSON,
                "Authorization": f"Bearer {self.token}",
                "User-Agent": self.user_agent,
            },
        )
        # The first hop cannot go through the base client's `request`, whose
        # opener follows the redirect with the token attached, so it carries
        # its own copy of the base client's retry: a transient 5xx here would
        # otherwise swap a class-keyed fingerprint for a job/step one and open
        # a second issue for a known flake.
        location = None
        for attempt in range(1, REQUEST_ATTEMPTS + 1):
            try:
                with self.redirect_opener(request) as response:
                    # No redirect: the body is the log.
                    return response.read().decode(LOG_ENCODING, DECODE_ERRORS)
            except urllib.error.HTTPError as error:
                if REDIRECT_STATUS_MIN <= error.code <= REDIRECT_STATUS_MAX:
                    location = (getattr(error, "headers", None) or {}).get("Location")
                    break
                retryable = error.code >= SERVER_ERROR_MIN or error.code == TOO_MANY_REQUESTS or _rate_limited(error)
                if not retryable or attempt == REQUEST_ATTEMPTS:
                    log(f"job {job_id} log: {error.code}; fingerprint falls back to job and step names")
                    return ""
                delay = _retry_delay(error)
            except urllib.error.URLError as error:
                if attempt == REQUEST_ATTEMPTS:
                    log(f"job {job_id} log: {error.reason}; fingerprint falls back to job and step names")
                    return ""
                delay = REQUEST_RETRY_SECONDS
            self.sleep(delay)
        if not location:
            log(f"job {job_id} log: redirect without a location; fingerprint falls back to job and step names")
            return ""
        # The second hop gets the same retry as the first, for the same
        # reason: one 503 from the blob store would otherwise swap the
        # fingerprint. Pre-signed URLs outlive three short retries.
        for attempt in range(1, REQUEST_ATTEMPTS + 1):
            try:
                with self.opener(urllib.request.Request(location, headers={"User-Agent": self.user_agent})) as response:
                    return response.read().decode(LOG_ENCODING, DECODE_ERRORS)
            except urllib.error.HTTPError as error:
                if error.code < SERVER_ERROR_MIN or attempt == REQUEST_ATTEMPTS:
                    log(f"job {job_id} log redirect: {error.code}; fingerprint falls back to job and step names")
                    return ""
            except urllib.error.URLError as error:
                if attempt == REQUEST_ATTEMPTS:
                    log(f"job {job_id} log redirect: {error.reason}; fingerprint falls back to job and step names")
                    return ""
            self.sleep(REQUEST_RETRY_SECONDS)
        return ""

    def pull_requests_for(self, run):
        """Numbers of the pull requests whose head is this run's commit.

        Neither of the obvious sources works for a fork pull request, which is
        every pull request here: the run's own `pull_requests` list is empty,
        and `GET /commits/{sha}/pulls` returns nothing for the head of an
        *open* fork pull request (it lists #1289 once merged, and nothing for
        #1331 while open). Listing pulls by `head=<fork owner>:<branch>` does
        resolve them, and both halves are on the run. The commit lookup stays
        as the fallback for a run with no head repository recorded.

        The pull request whose head is this commit comes first. When none is
        -- the author pushed again between the failed attempt and now -- the
        open pull request on that branch is still the one the commit was
        tested for, so it is returned rather than nothing.
        """
        sha = run["head_sha"]
        owner = ((run.get("head_repository") or {}).get("owner") or {}).get("login")
        branch = run.get("head_branch")
        # The number is a decoration on the row; the run's log evidence is
        # what cannot be recovered later. A lookup that fails past the base
        # client's retries costs the number, not the record.
        try:
            if owner and branch:
                query = urllib.parse.urlencode({"head": f"{owner}:{branch}", "state": PULL_STATE_ALL, "per_page": PAGE_SIZE})
                pulls = self.get(f"/repos/{self.repo}/pulls?{query}") or []
            else:
                pulls = self.get(f"/repos/{self.repo}/commits/{sha}/pulls", tolerate=NOT_FOUND_STATUSES) or []
        except (urllib.error.HTTPError, urllib.error.URLError) as error:
            log(f"pull request lookup for {sha[:SHORT_SHA_LENGTH]}: {error}; the row shows the branch instead")
            return []
        at_head = [pull["number"] for pull in pulls if (pull.get("head") or {}).get("sha") == sha]
        moved_on = [pull["number"] for pull in pulls if pull.get("state") == ISSUE_OPEN and (pull.get("head") or {}).get("sha") != sha]
        return at_head + moved_on

    def issues_for_workflow(self, workflow_id, state):
        """`ci:flaky` issues carrying this workflow's marker, newest first.

        The list endpoint returns pull requests too, so anything carrying a
        `pull_request` key is dropped.
        """
        # Every page: the closed list is the one the link back to an old
        # recurrence depends on, and it only grows.
        query = urllib.parse.urlencode({"labels": LABEL, "state": state})
        issues = self.get_all(f"/repos/{self.repo}/issues?{query}", per_page=PAGE_SIZE)
        prefix = workflow_marker(workflow_id)
        return [
            issue for issue in issues if "pull_request" not in issue and prefix in (issue.get("body") or "")
        ]

    def ensure_label(self):
        """422 is what GitHub returns for a label that already exists."""
        self.request(
            "POST",
            f"/repos/{self.repo}/labels",
            {"name": LABEL, "color": LABEL_COLOR, "description": LABEL_DESCRIPTION},
            tolerate=(422,),
        )

    def create_issue(self, title, body):
        return self.request("POST", f"/repos/{self.repo}/issues", {"title": title, "body": body, "labels": [LABEL]})

    def update_issue(self, number, **fields):
        return self.request("PATCH", f"/repos/{self.repo}/issues/{number}", fields)

    def comment(self, number, body):
        return self.request("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body})


# --------------------------------------------------------------------------- #
# Reconciling the issue with the new occurrence
# --------------------------------------------------------------------------- #


def failed_jobs_with_ids(api, run_id, attempt_number):
    """The failing jobs of one attempt, each with the test ids its log names.

    The attempt is known to have failed, so when no job says so in as many
    words -- a job stopped by its timeout reads `cancelled` -- whichever jobs
    did not pass or skip are the failing ones.
    """
    listed = api.attempt_jobs(run_id, attempt_number)
    failing = [job for job in listed if job.get("conclusion") in FAILING_CONCLUSIONS]
    if not failing:
        failing = [job for job in listed if job.get("conclusion") not in JOB_FALLBACK_EXCLUDED]
    jobs = []
    for job in failing:
        jobs.append(
            {
                "name": job["name"],
                "steps": job.get("steps") or [],
                "test_ids": extract_test_ids(api.job_log(job["id"])),
            }
        )
    return jobs


def workflow_listings(api, workflow_id):
    """The open and closed `ci:flaky` issues carrying this workflow's marker."""
    return api.issues_for_workflow(workflow_id, ISSUE_OPEN), api.issues_for_workflow(workflow_id, ISSUE_CLOSED)


def reconcile(api, repo, workflow_id, workflow_name, lines, records, listings=None):
    """Bring the issue for this fingerprint into line with the new occurrences.

    `records` is one run's worth: a row per failed attempt behind one green.
    `listings` is the (open, closed) issue lists for this workflow when the
    caller has them already -- a run with several keys lists once and passes
    them in -- else they are fetched here.

    Returns a human-readable account of what it did, for the log.
    """
    fingerprint = digest(workflow_id, lines)
    marker = issue_marker(workflow_id, fingerprint)
    open_issues, closed = listings or workflow_listings(api, workflow_id)
    current = next((issue for issue in open_issues if marker in (issue.get("body") or "")), None)
    # The closed list is read on every path, not only when opening: the body
    # is rebuilt from scratch on each update and nothing else remembers the
    # link back.
    previously = next((issue for issue in closed if marker in (issue.get("body") or "")), None)

    if current is None:
        merged, _ = merge_occurrences([], records)
        body = render_body(repo, workflow_name, lines, merged, marker, previously)
        api.ensure_label()
        created = api.create_issue(render_title(workflow_name, lines), body)
        return f"opened #{created['number']}" + (f" (recurrence of #{previously['number']})" if previously else "")

    merged, added = merge_occurrences(occurrences_in(current.get("body")), records)
    if not added:
        return f"#{current['number']} already records run {records[0]['run_id']}"
    # Body only. The title was written when the issue opened, and whoever
    # renamed it since knows more about the cause than this script does.
    body = render_body(repo, workflow_name, lines, merged, marker, previously)
    api.update_issue(current["number"], body=body)
    api.comment(current["number"], render_comment(repo, added, len(merged)))
    return f"updated #{current['number']} ({len(merged)} occurrences)"


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", type=int, required=True, help="the workflow run whose latest attempt completed")
    parser.add_argument(
        "--attempt",
        type=int,
        default=None,
        help="the attempt that just completed (default: the run's latest)",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPO),
        help="owner/name (default: $GITHUB_REPOSITORY)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the issue instead of writing it")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        log("GITHUB_TOKEN (or GH_TOKEN) is not set")
        return 1

    api = GitHubAPI(args.repo, token)
    current = api.attempt(args.run_id, args.attempt) if args.attempt else api.run(args.run_id)
    if current is None:
        log(f"run {args.run_id} attempt {args.attempt} does not exist")
        return 0
    earlier = [api.attempt(args.run_id, number) for number in range(1, current.get("run_attempt", 1))]
    failed_attempts = failed_before(current, earlier)

    reason = decide(current, failed_attempts)
    if reason:
        log(f"run {args.run_id}: {reason}; nothing to record")
        return 0

    workflow_id = current["workflow_id"]
    workflow_name = current["name"]
    pulls = api.pull_requests_for(current)
    pull_request = pulls[0] if pulls else None

    # One issue per key, and each failed attempt keyed on its own evidence: an
    # attempt whose log names tests joins those classes' issues, one whose log
    # names none joins its job-and-step issue, and no attempt is attached to
    # an issue it did not fail in. Keys are gathered in attempt order so the
    # rows come out oldest first.
    by_key = {}
    for failed in failed_attempts:
        jobs = failed_jobs_with_ids(api, args.run_id, failed["run_attempt"])
        for key, ids in (keys(jobs) or {None: []}).items():
            by_key.setdefault(key, []).append(occurrence(current, failed, pull_request, ids))

    # Each key's issue is written on its own: one refused update must not
    # leave the other keys of this run unrecorded, since nothing comes back
    # for them. The run still fails at the end so the refusal is visible.
    failures = 0
    listings = None if args.dry_run else workflow_listings(api, workflow_id)
    for key, records in by_key.items():
        lines = [key] if key is not None else []
        if args.dry_run:
            marker = issue_marker(workflow_id, digest(workflow_id, lines))
            log(f"--dry-run: flake in {workflow_name}, fingerprint {marker}")
            log(f"\n# {render_title(workflow_name, lines)}\n\n{render_body(args.repo, workflow_name, lines, records, marker)}")
            log(f"\n--- comment, if an issue is open ---\n{render_comment(args.repo, records, DRY_RUN_COMMENT_COUNT)}")
            continue
        try:
            log(reconcile(api, args.repo, workflow_id, workflow_name, lines, records, listings))
        except (urllib.error.HTTPError, urllib.error.URLError) as error:
            failures += 1
            log(f"key {key!r}: {error}; not recorded")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
