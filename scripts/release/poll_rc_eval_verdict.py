#!/usr/bin/env python3
"""Waits for the release-candidate eval's verdict on one nominated commit.

NOT WIRED YET, in the same sense as the evalcand_ helpers in common.sh: no
workflow in this repository runs this script, and the paragraph below describes
where it is going rather than what runs tonight. Tonight the nightly still
pushes staging_ straight off a green matrix, and the eval still fires on that
tag. The pull request that changes nightly-pipeline.yml is what joins the pieces
up. Everything after this paragraph is true of the script itself and can be
relied on now.

The nightly pipeline will push an `evalcand_<ts>_<sha>` tag, which fires
`post-kube-agents-eval-rc` in GoogleCloudPlatform/oss-test-infra. That job runs
the full eval catalog against the candidate's published images and takes hours.
This script stands between the nomination and the promotion: it polls the job's
GCS artifact archive until a build at that commit has finished, and reports
whether it passed. The pipeline pushes the `staging_` tag -- the one
staging-deploy.yml triggers on -- only when this says green.

WHY POLL GCS AND NOT ASK PROW. The verdict is also a commit status on the tagged
commit, which would be cheaper to read. It is not enough: a commit that carries
an earlier build's status would answer instantly with a verdict about a
different run, and a re-run from Deck overwrites the status without telling us
which build produced it. The archive is per-build, so "the newest build at this
commit" is a question it can answer and the status API cannot.

WHAT IS READ, AND WHY NOT THE EXIT STATUS. Three artifacts per build:

  started.json   names the commit Prow checked out. This is what identifies a
                 build as ours. It is written by Prow's decoration before
                 anything else runs, so it appears within minutes of the push.
  finished.json  says the build is over. Its absence means still in flight.
  artifacts/rc-eval-summary.md
                 carries the verdict word, and it is the one that decides.

The build's own pass/fail is deliberately NOT the verdict. It collapses two
outcomes an operator must not have collapsed: a candidate whose eval catalog
went red, and a lane that broke before measuring anything -- a Boskos project
that never leased, a deploy that failed, a 429. hack/ci-eval-rc.sh writes
GREEN, RED or NOT RUN into the summary artifact, so reading that word keeps a
distinction the build status throws away entirely.

HOW FAR THAT DISTINCTION GOES TODAY, stated because "RED is a judgement on the
candidate" is the load-bearing half of it and the driver's split is narrower
than the sentence above sounds. ci-eval-rc.sh writes NOT RUN when the deploy
fails or when it never reaches its reporting step; every other non-zero exit of
hack/ci-eval-pr.sh becomes RED, and some of those measured nothing either -- a
ledger token that would not mint, a runner image short of `uv`, a night on which
every case died on infrastructure. Those land here as a settled RED, and a
settled RED keeps its evalcand_ tag, so that candidate is never measured again.
It does not stall the lane: the next nightly resolves a newer commit and staging
advances. It does cost one candidate and leave a rejection in the record that
was not one. Narrowing it is the driver's job, not this script's: ci-eval-rc.sh
is the only place that can see which of its own steps failed, and this side of
the read cannot recover a distinction the word it is handed does not carry.

A missing summary artifact is itself informative rather than a gap. The driver
writes it on every path that reaches the reporting step, so its absence means
the run stopped before then -- a dormancy gate, a killed pod, a crash. That is
the case a green build status is most misleading about, since a run that
measured nothing exits 0.

EXIT CODES. 0 only on a green verdict. Everything else is a refusal to promote,
split so the summary and the surrounding workflow can tell which without
parsing prose. None of them is 1 or 2: those belong to an uncaught exception and
to argparse, and a poller that crashed must not be readable as a release that
failed. The `settled` output is the other half of the same split -- true only
when the eval actually answered (green or red), so the workflow can tell a
candidate that was judged from one whose lane broke and which may be retried.

Usage:
  poll_rc_eval_verdict.py --commit <sha> [--evalcand-tag <tag>]

Requires `gsutil`, authenticated against the archive bucket.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time

# The archive `post-kube-agents-eval-rc` uploads to, and the Prow UI that
# renders one build directory from it. Both are the deployment's, not this
# repository's: scripts/eval_dashboard/collect.py reads the same bucket for the
# dashboard, and .github/workflows/ci-health.yml holds the credentials that
# reach it.
DEFAULT_LOGS_PREFIX = "gs://kube-agents-prow/logs/post-kube-agents-eval-rc/"
SPYGLASS_VIEW = "https://oss.gprow.dev/view/gs/"
GS_SCHEME = "gs://"

# How long to wait for a verdict, in minutes. The eval's own ceiling is the
# binding constraint from the other side: the job is configured `timeout: 360m`
# with `grace_period: 5m`, and it can sit before that waiting up to 10m for a
# Boskos project, so a build is entitled to run longer than a GitHub Actions job
# is allowed to exist (360 minutes, hard). There is no deadline that waits out
# every legitimate build, so this one is chosen to leave the surrounding job
# time to mint a token and push the tag: 330 + the job's other steps stays under
# the ceiling, where 360 would be killed by the runner with no summary written.
DEFAULT_DEADLINE_MINUTES = 330
# How long to wait for a build to APPEAR at the commit -- a much shorter clock,
# running only until the first build identifies itself as ours. Prow's
# decoration uploads started.json before clonerefs, so the delay here is
# webhook, prowjob, pod schedule: minutes. Nothing appearing does not mean a
# slow eval, it means no eval. Reporting that at 45 minutes rather than at 330
# is the difference between a fixable morning and a lost day.
DEFAULT_APPEAR_DEADLINE_MINUTES = 45
# Between polls. The eval runs for hours, so a tighter interval buys nothing and
# a `gsutil ls` every two minutes over five hours is ~165 calls.
DEFAULT_INTERVAL_SECONDS = 120
# How many build directories to read per poll, newest first. The job fires at
# most once a day, so the build we want is almost always the newest; this bounds
# the per-poll cost when the listing has a year of history in it. Hitting the
# cap is reported rather than silently truncating the scan.
DEFAULT_SCAN_LIMIT = 20

# Per-call ceiling on gsutil, matching scripts/eval_dashboard/collect.py.
GSUTIL_TIMEOUT_S = 300
DEFAULT_GSUTIL = "gsutil"

SECONDS_PER_MINUTE = 60

STARTED_FILE = "started.json"
FINISHED_FILE = "finished.json"
# Where Prow's sidecar uploads $ARTIFACTS, and the file hack/ci-eval-rc.sh
# writes there. Renaming either end breaks the verdict read, which surfaces as
# every candidate reporting `not_run`; RC_SUMMARY_FILE in that script is the
# other half of this pair.
ARTIFACTS_DIR = "artifacts/"
RC_SUMMARY_FILE = "rc-eval-summary.md"

# started.json's scalar commit fields. `repos` is handled separately: its values
# are `<ref>:<sha>` for a tag-push postsubmit and a bare sha otherwise.
STARTED_COMMIT_FIELDS = ("repo-commit", "repo-version")
STARTED_REPOS_FIELD = "repos"
# started.json's wall-clock start, in unix seconds. Prow's decoration writes it
# before anything else runs, which is what makes it usable as "when did this
# attempt begin" -- see `--not-before` and `started_after`.
STARTED_TIMESTAMP_FIELD = "timestamp"
# Allowance for the two clocks involved in that comparison disagreeing: the
# GitHub runner that stamps the nomination and the Prow node that writes
# started.json. Generous on purpose. The two events are seconds apart by
# construction -- the runner stamps, pushes the tag, and the webhook fires --
# while the build this has to exclude is a whole nightly period old, so there is
# no tension between the two and no reason to cut it fine.
NOMINATION_SKEW_SECONDS = 15 * SECONDS_PER_MINUTE
# finished.json's fields. These no longer carry the verdict -- see the module
# docstring -- but they still answer "is this build over", and a build whose
# status disagrees with the summary artifact is reported as a broken lane rather
# than resolved in either direction.
FINISHED_PASSED_FIELD = "passed"
FINISHED_RESULT_FIELD = "result"
FINISHED_SUCCESS = "SUCCESS"

# The row hack/ci-eval-rc.sh writes into the summary artifact, and the three
# words it can carry. Matched case-insensitively on the row rather than searched
# for anywhere in the document: the prose around the table names the other
# verdicts while explaining them, and a substring search would match those.
SUMMARY_VERDICT_ROW = re.compile(r"^\|\s*Verdict\s*\|\s*(.+?)\s*\|\s*$", re.IGNORECASE)
SUMMARY_GREEN = "GREEN"
SUMMARY_RED = "RED"
SUMMARY_NOT_RUN = "NOT RUN"

# A commit has to be given at least this specifically to be matched against an
# artifact. Seven is what the short SHA in the tag carries; anything shorter
# would start matching builds at other commits.
MIN_SHA_CHARS = 7

# What this script decided, in the `verdict` output and the summary.
VERDICT_GREEN = "green"
VERDICT_RED = "red"
VERDICT_NOT_RUN = "not_run"
VERDICT_TIMEOUT = "timeout"
VERDICT_NEVER_RAN = "never_ran"

# Exit codes, one per verdict. Distinct so a workflow can tell "the release is
# bad" from "the eval lane is broken" without parsing the summary, and none of
# them 1 or 2: an uncaught exception exits 1 and argparse exits 2, so reusing
# either would make a broken poller indistinguishable from a rejected release.
EXIT_GREEN = 0
EXIT_RED = 10
EXIT_NOT_RUN = 11
EXIT_TIMEOUT = 12
EXIT_NEVER_RAN = 13
# The poller itself failed. Distinct from every verdict for the same reason.
EXIT_INTERNAL_ERROR = 14


class _Unreadable:
    """A read that failed, as distinct from an object that is not there.

    The difference decides correctness rather than wording. `scan_once` walks
    builds newest first and skips any whose started.json does not name the
    commit; if a transient error were indistinguishable from "this build is not
    ours", one failed read of the newest build would silently promote an older
    build's verdict -- which is the exact substitution polling GCS was chosen to
    prevent.
    """

    def __repr__(self):
        return "UNREADABLE"


UNREADABLE = _Unreadable()

# gsutil's stderr when the object genuinely is not there, as opposed to a 403,
# a 429, a timeout or a broken credential. Anything not matching is treated as
# UNREADABLE, which is the safe direction: the cost of calling a real absence
# unreadable is one more poll, while the reverse mistakes an outage for an
# answer.
#
# Anchored on gsutil's phrasings rather than on the bare status number, because
# gsutil echoes the failing URL in its error text and a Prow build id is 19
# digits: a 503 on `.../2097891568546404123/started.json` contains "404", and
# treating that as an absence would let the build be skipped as "not ours" and
# an older build answer in its place. scripts/eval_dashboard/collect.py sets the
# precedent with a compiled `matched no objects`.
_NOT_FOUND_PATTERNS = (
    "matched no objects",
    "no urls matched",
    "notfoundexception: 404",
)


def _gsutil(args, gsutil=DEFAULT_GSUTIL):
    """stdout of one gsutil call; None if the object is absent, UNREADABLE if
    the call failed.

    A failed call is a warning rather than a fatal error: the poll has hours of
    retries ahead of it, and a transient GCS error must not be the thing that
    holds a good candidate back.
    """
    try:
        proc = subprocess.run(
            [gsutil, *args],
            capture_output=True,
            text=True,
            timeout=GSUTIL_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"warning: {gsutil} {' '.join(args)}: {exc}", file=sys.stderr)
        return UNREADABLE
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        print(f"warning: {gsutil} {' '.join(args)}: {stderr}", file=sys.stderr)
        lowered = stderr.lower()
        if any(pattern in lowered for pattern in _NOT_FOUND_PATTERNS):
            return None
        return UNREADABLE
    return proc.stdout


def build_dirs(listing):
    """(build_id, directory URL) pairs from one `gsutil ls` listing, newest first.

    `gsutil ls a/` prints plain `gs://.../<id>/` lines and `gsutil ls a/*`
    prints each matched directory as a `gs://.../<id>/:` header over its
    contents; accept both. Everything that is not a numerically-named directory
    is dropped -- latest-build.txt, and the per-object lines the second form
    brings with it.

    Newest first because Prow build ids increase with time, which is also what
    makes a re-run from Deck supersede the build it re-runs.
    """
    dirs = []
    seen = set()
    for line in listing.splitlines():
        line = line.strip()
        if line.endswith("/:"):
            line = line[:-1]
        if not line.endswith("/") or line in seen:
            continue
        seen.add(line)
        build_id = line.rstrip("/").rsplit("/", 1)[-1]
        # `str.isdigit` accepts characters `int` refuses -- superscripts, other
        # scripts' digits -- so the parse is what decides, not the test. A
        # directory neither can agree on is not a Prow build id.
        try:
            ordinal = int(build_id)
        except ValueError:
            continue
        dirs.append((ordinal, build_id, line))
    return [(build_id, url) for _, build_id, url in sorted(dirs, reverse=True)]


def started_names_commit(started_text, commit):
    """Whether this build's started.json says Prow checked out `commit`.

    Compared prefix-wise in whichever direction is longer, so a full SHA on the
    command line matches a short one in the artifact and the reverse. The named
    fields are checked rather than the whole document as text: `repos` keys are
    repository names and a substring search over the raw JSON would let one
    match.
    """
    if not started_text or not isinstance(started_text, str):
        return False
    try:
        data = json.loads(started_text)
    except (ValueError, TypeError):
        return False
    if not isinstance(data, dict):
        return False

    candidates = [data.get(field) for field in STARTED_COMMIT_FIELDS]
    repos = data.get(STARTED_REPOS_FIELD)
    if isinstance(repos, dict):
        for value in repos.values():
            if isinstance(value, str):
                # `<ref>:<sha>` for a tag-push postsubmit; a bare sha otherwise.
                candidates.append(value.rsplit(":", 1)[-1])

    for value in candidates:
        if not isinstance(value, str):
            continue
        # Stripped before the length test, not after: padding would otherwise
        # satisfy the floor and leave the comparison running on something
        # shorter than it, which is what the floor exists to forbid.
        value = value.strip().lower()
        if len(value) < MIN_SHA_CHARS:
            continue
        shorter, longer = sorted((value, commit), key=len)
        if longer.startswith(shorter):
            return True
    return False


def started_after(started_text, not_before):
    """Whether this build began at or after `not_before` (unix seconds).

    THIS IS WHAT MAKES THE RETRY A RETRY. A withdrawn nomination is re-pushed
    under the SAME tag at the SAME commit -- the name is derived from the RC tag
    and is stable across attempts -- so the archive accumulates one build per
    attempt, all matching `started_names_commit`. Prow takes a minute or two to
    upload started.json for the new one, and the poll's first sweep happens
    immediately after the push. Without a floor the newest build naming the
    commit during that window is the PREVIOUS attempt's, already finished, and
    the poll answers with last night's verdict in seconds: the nomination is
    withdrawn again and tonight's build runs for hours with nobody reading it.
    The candidate is never re-measured, however many nights it is nominated.

    True when no floor was given, so a hand-run poll against a commit still
    behaves as it reads. The caller that gates a promotion always passes one.

    A build whose started.json carries no usable timestamp is NOT ours. That is
    the fail-safe direction: the cost is one nomination withdrawn and re-made,
    where believing it risks answering from a superseded attempt.
    """
    if not_before is None:
        return True
    if not started_text or not isinstance(started_text, str):
        return False
    try:
        data = json.loads(started_text)
    except (ValueError, TypeError):
        return False
    if not isinstance(data, dict):
        return False
    stamp = data.get(STARTED_TIMESTAMP_FIELD)
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return False
    return stamp >= not_before - NOMINATION_SKEW_SECONDS


def summary_verdict(summary_text):
    """GREEN, RED or NOT RUN from the eval's summary artifact, else None.

    None covers both an artifact that is not there and one whose Verdict row
    does not parse. Both mean the same thing to the caller -- the driver did not
    get far enough to record a judgement -- and neither may be resolved in the
    candidate's favour.
    """
    if not summary_text or not isinstance(summary_text, str):
        return None
    for line in summary_text.splitlines():
        match = SUMMARY_VERDICT_ROW.match(line.strip())
        if not match:
            continue
        word = " ".join(match.group(1).split()).upper()
        if word in (SUMMARY_GREEN, SUMMARY_RED, SUMMARY_NOT_RUN):
            return word
        return None
    return None


def finished_passed(finished_text):
    """True/False from a build's finished.json, or None if it has not finished.

    This is no longer the verdict -- see the module docstring -- but it is still
    how the poll knows a build is over, and it cross-checks the summary
    artifact: a build that says it passed while the artifact says RED, or the
    reverse, is reported as a broken lane rather than believed.
    """
    if not finished_text or not isinstance(finished_text, str):
        return None
    try:
        data = json.loads(finished_text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    passed = data.get(FINISHED_PASSED_FIELD)
    if isinstance(passed, bool):
        return passed
    result = data.get(FINISHED_RESULT_FIELD)
    if isinstance(result, str):
        return result.strip().upper() == FINISHED_SUCCESS
    return None


def spyglass_url(base):
    """Prow's page for a build directory, for the summary to link."""
    if not isinstance(base, str) or not base.startswith(GS_SCHEME):
        return None
    return SPYGLASS_VIEW + base[len(GS_SCHEME):].rstrip("/")


def read_verdict(base, read_file, finished):
    """The verdict for one finished build directory.

    `finished` is the build's finished.json text, passed in rather than read
    here. The caller has already read it -- that read is how it knows the build
    is over -- and it is the caller's read that is allowed to fail: an
    UNREADABLE finished.json there means "say nothing and poll again". Reading
    it a second time here would give a transient 429 a second chance to come
    back UNREADABLE, and finished_passed(UNREADABLE) is None rather than False,
    which is not the refusal below but the absence of it. A GREEN summary on a
    build Prow failed would then promote.

    Ordering matters. The summary artifact decides, because it is the only
    source that separates a red catalog from a lane that measured nothing. The
    build's own status is consulted afterwards, and only in the direction that
    refuses: it can downgrade a GREEN summary, never a RED one.

    WHY THE DISAGREEMENT IS NOT SYMMETRIC, since treating both the same way is
    the obvious design and it is wrong. A GREEN summary under a failed build is
    a promotion this cannot justify, so it becomes NOT RUN -- the candidate is
    neither promoted nor rejected, the nomination is withdrawn, and a later
    night measures it again. A RED summary under a passing build is already the
    refusing outcome, and re-reading it as NOT RUN would withdraw the nomination
    of a candidate the eval REJECTED, putting it back in the pool to be
    re-measured for hours every night to reach the same answer. There is nothing
    to gain by disbelieving a red and a standing cost to doing it, so a red
    summary is a red verdict whatever the status says.

    The asymmetry also removes a cross-repository prerequisite this had no way
    to state. The eval job wrapped its driver in `|| true` for as long as the
    lane was advisory, and under that configuration `passed` is true on every
    build including a red one -- so the symmetric rule turned EVERY red into the
    re-measure loop above, exactly when the lane stopped being advisory. That
    `|| true` is being removed in the same change, but a verdict reader should
    not silently depend on a line in another repository's job config.
    """
    summary = read_file(base + ARTIFACTS_DIR + RC_SUMMARY_FILE)
    if summary is UNREADABLE:
        return None
    word = summary_verdict(summary)
    if word is None:
        # The driver never reached its reporting step. A green build status is
        # at its most misleading here: every dormancy gate in ci-eval-rc.sh
        # exits 0 without measuring anything.
        return VERDICT_NOT_RUN
    if word == SUMMARY_NOT_RUN:
        return VERDICT_NOT_RUN

    passed = finished_passed(finished)
    if word == SUMMARY_GREEN:
        if passed is False:
            print(
                f"warning: {base} reports a GREEN verdict on a build that failed;"
                " refusing to promote on it and reporting the lane as broken",
                file=sys.stderr,
                flush=True,
            )
            return VERDICT_NOT_RUN
        return VERDICT_GREEN
    if passed is True:
        print(
            f"warning: {base} reports a RED verdict on a build that passed;"
            " believing the summary, since a red verdict refuses either way",
            file=sys.stderr,
            flush=True,
        )
    return VERDICT_RED


def scan_once(
    commit,
    read_listing,
    read_file,
    scan_limit=DEFAULT_SCAN_LIMIT,
    note=None,
    not_before=None,
):
    """One sweep of the archive for a build at `commit`.

    Returns (state, build_id, base), where state is a verdict when a finished
    build was found, None when a build at the commit is still running or the
    sweep could not be completed, and VERDICT_NEVER_RAN when the sweep saw no
    build at the commit at all.

    Newest first, and the first build at the commit decides -- finished or not.
    An unfinished build that is NEWER than a finished one is a re-run from Deck
    superseding it, so reporting the older build's verdict would answer with the
    result the operator re-ran to replace.

    `not_before` narrows "at the commit" to "at the commit, for THIS nomination"
    -- see `started_after`, which is what stops a previous attempt's build
    answering for the one just pushed.

    `note` collects one-shot messages so a 165-poll run does not repeat them.
    """
    listing = read_listing()
    if listing is UNREADABLE or listing is None:
        # A listing we could not read is not an empty archive. Say nothing and
        # let the caller try again.
        return None, None, None

    dirs = build_dirs(listing)
    if len(dirs) > scan_limit:
        if note is not None:
            note(
                f"note: {len(dirs)} builds listed, reading the newest {scan_limit};"
                f" a build at {commit[:MIN_SHA_CHARS]} older than those is not seen"
            )
        dirs = dirs[:scan_limit]

    for index, (build_id, base) in enumerate(dirs):
        started = read_file(base + STARTED_FILE)
        if started is UNREADABLE:
            # Not "this build is not ours". Walking past it would let the next
            # build down answer for a candidate this one may own.
            return None, None, None
        if started is None and index == 0:
            # The newest directory exists but has no started.json yet, which is
            # the minute or two between Prow creating the build and decoration
            # uploading the artifact. Absent is not "not ours" HERE: this is the
            # build most likely to be the one we are waiting for, and walking
            # past it lets an older build at the same commit answer -- for a
            # re-run from Deck, with the very verdict the operator re-ran to
            # replace. Deeper in the listing the same absence is an old build
            # whose artifact was reaped, and skipping it is right.
            return None, None, None
        if not started_names_commit(started, commit):
            continue
        if not started_after(started, not_before):
            if note is not None:
                note(
                    f"note: build {build_id} is at {commit[:MIN_SHA_CHARS]} but began"
                    " before this nomination, so it belongs to an earlier attempt"
                    " and is not read"
                )
            continue
        finished = read_file(base + FINISHED_FILE)
        if finished is UNREADABLE or finished_passed(finished) is None:
            return None, build_id, base
        return read_verdict(base, read_file, finished), build_id, base

    return VERDICT_NEVER_RAN, None, None


def poll(
    commit,
    read_listing,
    read_file,
    now=time.monotonic,
    sleep=time.sleep,
    deadline_minutes=DEFAULT_DEADLINE_MINUTES,
    appear_deadline_minutes=DEFAULT_APPEAR_DEADLINE_MINUTES,
    interval_seconds=DEFAULT_INTERVAL_SECONDS,
    scan_limit=DEFAULT_SCAN_LIMIT,
    not_before=None,
):
    """Sweeps until a verdict, a deadline, or the appearance clock running out.

    `now` and `sleep` are injected so the deadlines can be exercised without
    one, and `time.monotonic` rather than the wall clock so an NTP correction
    mid-poll cannot move a deadline.
    """
    started_at = now()
    deadline = started_at + deadline_minutes * SECONDS_PER_MINUTE
    appear_deadline = started_at + appear_deadline_minutes * SECONDS_PER_MINUTE
    seen_build = None
    said = set()

    def note(message):
        if message not in said:
            said.add(message)
            print(message, file=sys.stderr, flush=True)

    while True:
        state, build_id, base = scan_once(
            commit, read_listing, read_file, scan_limit, note, not_before
        )

        if state in (VERDICT_GREEN, VERDICT_RED, VERDICT_NOT_RUN):
            return state, build_id, base

        if build_id is not None and build_id != seen_build:
            # Announce it once. The interesting transition for someone reading
            # the log is "the eval picked our tag up", not each of the 160
            # polls that follow it.
            seen_build = build_id
            print(f"build {build_id} is running at {commit}: {spyglass_url(base)}", flush=True)

        elapsed = now() - started_at
        if seen_build is None and now() >= appear_deadline:
            return VERDICT_NEVER_RAN, None, None
        if now() >= deadline:
            return VERDICT_TIMEOUT, seen_build, base

        print(
            f"waiting for the eval verdict on {commit}"
            f" ({int(elapsed // SECONDS_PER_MINUTE)}m elapsed,"
            f" {int((deadline - now()) // SECONDS_PER_MINUTE)}m left)",
            flush=True,
        )
        sleep(interval_seconds)


# What the workflow prints for each outcome, and what an operator should do
# about it. Kept beside the verdicts rather than inlined at the exit, so the
# four refusals stay visibly parallel.
#
# None of them tells the reader to re-run from Deck or to re-dispatch the
# pipeline on the same candidate. Neither works: the poll has already returned
# by the time a Deck re-run finishes, and a commit that carries an evalcand_ tag
# is skipped by resolve_promotion_candidate.sh on every later run. Recovery from
# an unsettled verdict is the pipeline's own job, which drops the nomination so
# the next nightly can make it again.
_SUMMARY = {
    VERDICT_GREEN: (
        "The release-candidate eval passed on this candidate. Promoting it to staging."
    ),
    VERDICT_RED: (
        "The release-candidate eval FAILED on this candidate, so it was not promoted and"
        " staging stays on the previous build. The linked build says which cases failed."
        " RED is treated as a judgement on the candidate, so the nomination stands and the"
        " next nightly measures the next candidate. Read the linked build before accepting"
        " that: the driver reports RED for anything that made the eval step exit non-zero,"
        " which includes a few ways it can fail without grading a case. If that is what"
        " happened, delete the evalcand_ tag by hand and the candidate is nominated again."
    ),
    VERDICT_NOT_RUN: (
        "The release-candidate eval did not measure this candidate, so it was not promoted"
        " and staging stays on the previous build. This is a broken lane rather than a bad"
        " release -- a deploy that failed, a project that never leased, or a run that"
        " stopped before its reporting step -- and the linked build says where it stopped."
        " The candidate is not rejected: the nomination is dropped so a later nightly can"
        " nominate the same commit again."
    ),
    VERDICT_TIMEOUT: (
        "The release-candidate eval had not finished when this job's deadline passed, so"
        " the candidate was not promoted and staging stays on the previous build. This is"
        " not a verdict on the release. The eval is allowed to run for longer than a"
        " GitHub Actions job may exist, so a slow-but-green build reaches this outcome."
        " The candidate is not rejected: the nomination is dropped so a later nightly can"
        " nominate the same commit again."
    ),
    VERDICT_NEVER_RAN: (
        "No release-candidate eval build ever appeared for this candidate, so it was not"
        " promoted. This is a broken lane rather than a bad release. Two causes look"
        " identical from here and the build log above separates them: the archive being"
        " unreadable from this job (an expired credential, a revoked grant, the wrong"
        " bucket), which logs a warning on every sweep; or no job having fired at all,"
        " which logs none and points at the evalcand_ tag shape and the `branches` regex"
        " on post-kube-agents-eval-rc in GoogleCloudPlatform/oss-test-infra having drifted"
        " apart. The candidate is not rejected: the nomination is dropped so a later"
        " nightly can nominate the same commit again."
    ),
}

_EXIT = {
    VERDICT_GREEN: EXIT_GREEN,
    VERDICT_RED: EXIT_RED,
    VERDICT_NOT_RUN: EXIT_NOT_RUN,
    VERDICT_TIMEOUT: EXIT_TIMEOUT,
    VERDICT_NEVER_RAN: EXIT_NEVER_RAN,
}

# Whether the eval actually answered about this candidate. False is what lets
# the pipeline drop the nomination and try again, so the distinction is the
# retry policy: a red candidate is settled and must never be re-measured, while
# a broken lane has said nothing about the candidate at all.
_SETTLED = {
    VERDICT_GREEN: True,
    VERDICT_RED: True,
    VERDICT_NOT_RUN: False,
    VERDICT_TIMEOUT: False,
    VERDICT_NEVER_RAN: False,
}


def _write_kv(path, pairs):
    """Append `k=v` lines to a GitHub Actions file, when the runner set one."""
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in pairs:
            handle.write(f"{key}={value}\n")


def _write_summary(path, lines):
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main(argv=None):
    # Refusing defaults, written before anything else can fail -- before the
    # arguments are even parsed, since argparse exits 2 on a bad one. Every exit
    # below appends over these, and GitHub Actions takes the LAST line for a
    # repeated key, so the seed costs nothing on the normal path. What it covers
    # is the paths that never reach one: a mistyped flag, an exception inside
    # poll(), a bug here. A caller running this under `continue-on-error` and
    # branching on the output would otherwise read the empty string, and an
    # empty string is not "red" -- it would sail through a `!= 'red'` guard and
    # promote. The exit-code taxonomy exists so the caller CAN keep going, so
    # the outputs have to be safe for one that does.
    outputs_path = os.environ.get("GITHUB_OUTPUT")
    _write_kv(
        outputs_path,
        [
            ("verdict", VERDICT_NOT_RUN),
            ("settled", "false"),
            ("build_id", ""),
            ("log_url", ""),
        ],
    )

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--commit", required=True, help="Full SHA of the nominated candidate")
    parser.add_argument("--evalcand-tag", default="", help="Tag that nominated it, for the summary")
    parser.add_argument("--logs-prefix", default=DEFAULT_LOGS_PREFIX)
    parser.add_argument("--deadline-minutes", type=int, default=DEFAULT_DEADLINE_MINUTES)
    parser.add_argument(
        "--appear-deadline-minutes", type=int, default=DEFAULT_APPEAR_DEADLINE_MINUTES
    )
    parser.add_argument("--interval-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--scan-limit", type=int, default=DEFAULT_SCAN_LIMIT)
    parser.add_argument(
        "--not-before",
        type=int,
        default=None,
        help=(
            "Unix seconds the nomination was pushed. Builds that began earlier"
            " belong to a previous attempt at the same commit and are ignored."
        ),
    )
    parser.add_argument("--gsutil", default=DEFAULT_GSUTIL)
    args = parser.parse_args(argv)

    commit = args.commit.strip().lower()
    if len(commit) < MIN_SHA_CHARS:
        parser.error(
            f"--commit must be at least {MIN_SHA_CHARS} characters;"
            f" a shorter one would match builds at other commits"
        )
    # An appearance clock that outlasts the overall deadline never fires, and
    # the run then reports TIMEOUT -- whose advice is "a slow-but-green build
    # reaches this outcome" -- for a job that never started, whose advice names
    # the `branches` regex having drifted. Wrong diagnosis, so refuse the
    # combination rather than emit it. The defaults are consistent; this catches
    # the first caller who tunes one of them.
    if args.appear_deadline_minutes > args.deadline_minutes:
        parser.error(
            f"--appear-deadline-minutes ({args.appear_deadline_minutes}) exceeds"
            f" --deadline-minutes ({args.deadline_minutes}), so it could never fire"
        )

    prefix = args.logs_prefix if args.logs_prefix.endswith("/") else args.logs_prefix + "/"
    if args.not_before is None:
        print(
            "warning: no --not-before given, so a build from an earlier attempt at"
            " this commit can answer for the current nomination",
            file=sys.stderr,
            flush=True,
        )
    verdict, build_id, base = poll(
        commit,
        read_listing=lambda: _gsutil(["ls", prefix], args.gsutil),
        read_file=lambda url: _gsutil(["cat", url], args.gsutil),
        deadline_minutes=args.deadline_minutes,
        appear_deadline_minutes=args.appear_deadline_minutes,
        interval_seconds=args.interval_seconds,
        scan_limit=args.scan_limit,
        not_before=args.not_before,
    )

    url = spyglass_url(base) or ""
    settled = "true" if _SETTLED[verdict] else "false"
    print(f"verdict={verdict} settled={settled} build_id={build_id or ''} {url}".rstrip(), flush=True)

    _write_kv(
        outputs_path,
        [
            ("verdict", verdict),
            ("settled", settled),
            ("build_id", build_id or ""),
            ("log_url", url),
        ],
    )

    lines = [f"### Release-candidate eval: `{verdict.upper()}`", ""]
    if args.evalcand_tag:
        lines.append(f"Candidate: `{args.evalcand_tag}` (`{commit}`)")
    lines += ["", _SUMMARY[verdict]]
    if url:
        lines += ["", f"[Eval build {build_id}]({url})"]
    _write_summary(os.environ.get("GITHUB_STEP_SUMMARY"), lines)

    return _EXIT[verdict]


if __name__ == "__main__":
    # An uncaught exception would exit 1, which the workflow reads as a rejected
    # release. Every failure of the poller itself is worth a code of its own.
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - the exit code is the contract
        print(f"error: the eval-verdict poller failed: {exc!r}", file=sys.stderr)
        sys.exit(EXIT_INTERNAL_ERROR)
