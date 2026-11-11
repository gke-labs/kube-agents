#!/usr/bin/env python3
"""File a bug for a case the nightly fails on every run, three nights in a
row, and tell the bug when the case passes again.

The nightly runs every case against ``main`` once a night (``nightly.py``),
and nothing acted on its reds: on 2026-10-19, 17 cases had scored 0 passes
on at least two of the last seven nights and none had a tracking issue. This
is the nightly's counterpart of ``gate_issue.py``::

    a case failed every graded repetition on its last STREAK_NIGHTS graded nights
    and its task.yaml does not carry `expected_fail: true`
    and no OPEN issue labelled `nightly-red` already names it,
    and none was closed after the streak's oldest night started
        (``closed_after_start``), or closed as not planned with no pass
        since (``still_tracks``)
    -> file one, labelled `nightly-red`, `domain:<slug>` and `needs-triage`,
       assigned per the rule below

    a case with an open `nightly-red` issue passed every graded repetition
    on a graded night after its last failing one
    -> comment once on the issue with that night's run link; never close it

**One cause, one issue.** Before filing, the cases that would each file
are grouped by ``(first red night, failing check)``: the date their run of
failing nights began, and a check every graded failing repetition of that
run names (``rep_checks``). GROUP_MIN_CASES or more cases on one key file
one issue instead of one each (``Filer.file_group``)::

    GROUP_MIN_CASES cases would file, turned red on the same night and share
    a failing check on every failing repetition since
    -> one issue, labelled `nightly-red`, each case's `domain:<slug>` and
       `needs-triage`, listing each case's nights; assigned to the domain
       owner, or to ``infra_owner`` when the cases span domains, with every
       case's effective owner @-mentioned

    every case an open group issue names has recovered (``recovery_night``)
    -> one comment listing each case's clear night; no per-case recovery
       comment on a group issue, so ten cases do not make eleven comments

The group issue carries each case's marker, so the dedupe reads it as it
reads a per-case issue, and its own marker (GROUP_MARKER) for the all-clear.
A case that reaches the streak a tick after its group was filed, with the
same first night and check, files on its own: GROUP_MARKER carries the night
only, by design, so no key matches it to the group (a known follow-up). It names no check: the repository is
public, and the check is on the dashboard behind the run links. On
2026-11-09 the filer opened 12 issues, 10 of them for one grader change
(#4973, fixed by #5179), sent to five owners.

**A graded night** for a case is one where the part of the night that ran
it (``nightly.night_part``) was not cut short and at least one of its
repetitions was graded. Three kinds of night say nothing about one case and
are skipped -- they neither break a streak nor extend one:

- a part cut off at the job deadline (``nightly.run_truncated``): the cases
  it graded before the kill are real verdicts, but the night's parallel
  fan-out was starved by the deadline, and #3440 rules these nights out;
- a case with no graded repetition that night -- not run, or every
  repetition classified as infrastructure (``classify.rep_kind``: an
  ``infra`` verdict, a never-ran phrasing, a delegation ceiling). A
  repetition the classifier calls infrastructure is never a failure;
- a run-level event (``classify.is_run_event``: nearly every graded case
  failed), which the pages and ``classify.nightly_failed_recent`` also skip.

Skipping rather than breaking keeps a weekend of quota storms from resetting
a real streak; skipping rather than counting keeps a night with no verdict
from filing one. A date that ran twice (a re-run of the main job) is one
night: the newest graded verdict of that date stands.

**Who gets it.** The case's effective owner (``scripts/case_owner.py``):
its ``owner:`` in task.yaml when that names a GitHub login, otherwise -- no
owner, or the retired ``maintainers`` -- the domain's ``owner:`` in
``docs/designs/domains.yaml``. The domain owner is @-mentioned either way.
When GitHub refuses the case owner as assignee, the issue goes to the domain
owner; when it refuses that login too, it is filed unassigned and says so. Any
other failed write files nothing that tick: the next tick's issue list finds,
by its marker, an issue the failed call created anyway (a timeout after the
write).

**Infra** (``nightly_infra.py`` has the rules). Two shapes file an issue
labelled `nightly-red`, `Infra` and `needs-triage` instead of a domain label,
assigned to ``infra_owner`` in domains.yaml with ``infra_backup`` mentioned:

    a part of the nightly (main, writers) lost JOB_LOST_MIN of the last
    JOB_LOST_WINDOW nightly dates -- cut off before its verdict, or its pod lost
    -> one issue for the job; a comment on each later lost night, and one
       when a later night runs to its verdict

    a case graded nothing -- every repetition infrastructure, or no line
    for it -- on INFRA_STREAK_NIGHTS nights in a row where its part ran
    -> one issue for the case; a comment on each later such night, and one
       when it is graded again

The domain streak is untouched by either: an infrastructure repetition is
dropped before a night counts as failing.

**A listing it cannot trust files nothing.** Both issue listings are
cross-checked against the same issues read through GraphQL
(``listing_doubt``); when GraphQL cannot be read, or names more issues
than the listing holds, the listings are read again (LISTING_ATTEMPTS),
and when no read agrees the tick writes nothing and logs why; the next tick
retries. With ``--state``, a live run counts those held ticks in a small JSON
file (``held_state``); past HELD_TICKS_DIGEST of them in a row the morning
digest says filing is held (``held_digest_lines``), until the first trusted
tick clears it. A dry run records nothing held: it files nothing anyway.
Comments are read the same way: an issue whose listing counts
more comments than the read returned is not commented on that tick.

All GitHub traffic goes through ``ghcli.Gh``. **Dry run is the default**:
reads happen, every write is printed instead of sent. ``--live`` sends them;
the workflow passes it only when the repository variable
``NIGHTLY_BUGS_LIVE`` is ``true`` (docs/ci-health.md, "Nightly bugs").
"""

from __future__ import annotations

import argparse
import datetime
import functools
import json
import pathlib
import re
import subprocess
import sys
import time

try:
    import yaml
except ImportError:  # the workflow installs pyyaml; main() says so if absent
    yaml = None

try:
    import case_owner
except ImportError:  # scripts/ is not on sys.path yet
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    import case_owner
try:
    from eval_dashboard import classify, collect, gate_issue, ghcli, nightly, nightly_infra
    from eval_dashboard import post_health
    from eval_dashboard.post_health import NIGHTLY_URL
except ImportError:  # run as a script: scripts/eval_dashboard/nightly_bugs.py
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from eval_dashboard import classify, collect, gate_issue, ghcli, nightly, nightly_infra
    from eval_dashboard import post_health
    from eval_dashboard.post_health import NIGHTLY_URL

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
# The top-level task.yaml keys read here, and the domains.yaml row keys.
TASK_OWNER_KEY = "owner"
TASK_DOMAIN_KEY = "domain"
TASK_EXPECTED_FAIL_KEY = "expected_fail"
DOMAIN_ROW_KEYS = ("name", "owner", "backup")
CASE_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")

# How many graded nights in a row a case must fail every repetition on.
STREAK_NIGHTS = 3
# How many of the newest nightly dates are read: nightly.NIGHTS_ON_RECORD,
# the depth the Nightly page shows. A recovery comment needs the failing
# night inside it, so a long-recovered case is not commented on again as
# its passing nights scroll past the window's edge.
LOOKBACK_NIGHTS = nightly.NIGHTS_ON_RECORD
# classify.outcome_of's graded outcomes.
GRADED_OUTCOMES = (classify.OUTCOME_PASSED, classify.OUTCOME_PARTIAL, classify.OUTCOME_FAILED)

LABEL = "nightly-red"
TRIAGE_LABEL = "needs-triage"
INFRA_LABEL = "Infra"
DOMAIN_LABEL = "domain:{slug}"
OPEN_ISSUES_PATH = "issues?labels={label}&state=open&per_page=100"
# Closed ones too, updated since the window's first night: a bug closed
# after any of the streak's nights started has seen this streak, so it is
# not filed again until STREAK_NIGHTS failing nights all started after the
# close (``closed_after_start``: a fix that merges closes its issue, and the
# nights on record, even one running when it merged, tested the code before
# it); one handed off as not planned counts only while its close is on
# record (``still_tracks``), so nothing older is needed.
CLOSED_ISSUES_PATH = "issues?labels={label}&state=closed&since={since}&per_page=100"
# The cross-check on both listings: the same issues read through GraphQL.
# On 2026-10-22 the REST issue listing (any filter) and the search API both
# answered 200 with no issues about half the time while the issues existed,
# and the filer filed six duplicates (#3754-#3759); GraphQL and a single-issue
# GET answered correctly every time. A REST listing that lacks an issue
# GraphQL returns, or holds fewer issues than its totalCount, is not trusted.
# A listing GraphQL disagrees with is read again, up to this many times in
# one pass, before the pass writes nothing: the empty answers came about
# half the time, so most passes still find a good read.
LISTING_ATTEMPTS = 4
# The pause between those reads: the empty answers came in bursts, so reads
# back to back can all land in one.
LISTING_RETRY_DELAY_S = 3
# How many ticks in a row with no trusted listing (15 minutes apart: about
# two hours) before the digest says filing is held (#3769). Fewer is the
# REST listing's ordinary bursts, which the next tick rides out.
HELD_TICKS_DIGEST = 8
# A state file not rewritten for this long says nothing about filing now:
# the step stopped running or could not write, and the job log says why.
HELD_STATE_FRESH = datetime.timedelta(hours=1)
# What `gcloud storage cat` says of an absent object: the patterns
# ci-health.yml greps for. Any other failed read is not "no hold on record".
STATE_ABSENT_RE = re.compile(r"matched no objects|not found: 404")
# How much of gcloud's stderr a failed state read quotes in its warning.
STATE_ERROR_CHARS = 200
# A hold older than this prints its start with the date, so a week-old hold
# does not read as this morning's (post_health.RECONCILE_RUN_DATED_AFTER).
HELD_SINCE_DATED_AFTER = datetime.timedelta(days=1)
HELD_SINCE_UNKNOWN = "an unrecorded time"
HELD_DIGEST_LINE = "🚫 Nightly bug filing held since {since}: the nightly-red issue listing is untrusted ({doubt}), so no bugs are filed or commented on."
GRAPHQL_PATH = "graphql"
GRAPHQL_FIRST = 100
LISTING_QUERY = """\
query($owner: String!, $name: String!, $label: String!, $since: DateTime, $closed: Boolean!) {
  repository(owner: $owner, name: $name) {
    open: issues(labels: [$label], states: OPEN, first: %(first)d) { totalCount nodes { number } }
    closed: issues(labels: [$label], states: CLOSED, first: %(first)d, filterBy: {since: $since}) @include(if: $closed) { totalCount nodes { number } }
  }
}""" % {"first": GRAPHQL_FIRST}
# `since` in UTC with a Z: a `+00:00` offset would reach GitHub as a space.
GITHUB_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
ISSUES_PATH = "issues"
COMMENTS_PATH = "issues/{number}/comments"
COMMENTS_READ_PATH = "issues/{number}/comments?per_page=100"
# The bot's issue carries this, so it re-finds its own whatever the title
# has been edited to; an issue without one (a human's) is adopted when its
# title or body names the case as a whole word. A bot issue is matched on
# its marker alone: a case named anywhere else in its body does not make it
# that case's issue.
# The three kinds of issue, each with a marker of its own: a case's domain
# bug, a case kept from running by infrastructure, a part's lost nights.
KIND_RED = "nightly-red"
KIND_INFRA = "nightly-infra"
KIND_LOST = "nightly-lost"
KEY_CASE = "case"
KEY_JOB = "job"
BOT_MARKER = "<!-- kube-agents-bot:{kind} {field}={key} -->"
BOT_MARKER_RE = re.compile(r"<!-- kube-agents-bot:(nightly-red|nightly-infra|nightly-lost) (?:case|job)=([A-Za-z0-9._-]+) -->")
MARKER = BOT_MARKER.format(kind=KIND_RED, field=KEY_CASE, key="{case}")
# One recovery comment per recovery, and one comment per later night: keyed
# by the night's date, so a same-date re-run (one night) does not comment again.
RECOVERY_MARKER = "<!-- kube-agents-bot:nightly-red-recovered case={case} night={date} -->"
NIGHT_MARKER = "<!-- kube-agents-bot:{kind}-night {field}={key} night={date} -->"
INFRA_RECOVERY_MARKER = "<!-- kube-agents-bot:{kind}-recovered {field}={key} night={date} -->"
# A case name inside text, bounded by characters a case name cannot hold; a
# `.`, `_` or `-` ends it unless a name character follows, so "fails on
# case-red." names case-red and "case-red-extended" does not.
NAME_IN_TEXT_RE = r"(?<![A-Za-z0-9._-]){name}(?![A-Za-z0-9]|[._-][A-Za-z0-9])"
# The case's transcript artifact (nightly.TRANSCRIPT_ARTIFACT, any rep), which
# a human pasting a run link names it by: `_` bounds the name only here, so
# "case-red_old" is still another case.
NAME_IN_TRANSCRIPT_RE = r"\beval_{name}_rep[0-9]+\.log"
# GitHub's limits: a longer title or body is refused and files nothing.
TITLE_MAX_CHARS = 256
ISSUE_BODY_MAX_CHARS = 65536
UNKNOWN = "unknown"
# The domain owner in an issue body; a domain with no owner on record is
# said so rather than @-mentioning an account named "unknown".
DOMAIN_OWNER_LINE = "Domain owner: @{owner}{backup}."
NO_DOMAIN_OWNER_LINE = "No domain owner in `docs/designs/domains.yaml`."
INFRA_DOMAIN_OWNER = "owner @{owner}"
INFRA_NO_DOMAIN_OWNER = "no owner in `docs/designs/domains.yaml`"

TITLE = "Nightly: {case} needs triage"
BODY = """\
{marker}
`{case}` needs triage: its nights are listed below, with each night's grading on the dashboard. Domain: **{domain_name}** (`{domain}`).

{addressee} {domain_owner}

**Nights** (newest first; a night cut off at the job deadline, a night where nothing was graded, and a night where nearly every case failed are skipped, not counted)

{nights_block}

Nightly report: {nightly_url}

**What triage decides (within a week)**

- **Product bug:** name a fixer, a priority and a target date.
- **Broken test:** fix the case, or mark it `expected_fail: true` in its `task.yaml` with this issue linked.
- **Infra:** close this issue and point to the infra issue.

Filed automatically by `scripts/eval_dashboard/nightly_bugs.py` (docs/ci-health.md, "Nightly bugs"). It comments here when the case is clear again; it never closes the issue.
"""
# One line per night: the date and the private links. The night's pass count
# and the checks' reasons are on the dashboard behind them; the repository is
# public, so the issue carries neither.
NIGHT_LINE = "- {date} · [run]({run_url}) · [transcript]({transcript_url})"
NIGHT_LINE_NO_LINK = "- {date}"
WHY_CASE_OWNER = "the case's `owner:`"
WHY_DOMAIN_OWNER = "domain owner; the case's `owner:` is {owner}"
# A refused login is shown as code, not @-mentioned: GitHub just said it is
# outside the project, and a mention would still notify that account.
WHY_REFUSED = "domain owner; GitHub refused `{refused}` as assignee"
WHY_UNASSIGNED_REFUSED = "GitHub refused {refused} as assignee"
WHY_UNASSIGNED_NO_OWNER = "`docs/designs/domains.yaml` names no owner for `{domain}`"
ASSIGNED = "**Assigned to** @{assignee} ({why})."
UNASSIGNED = "**Unassigned** ({why})."
# The issue field GitHub names when it will not assign a login.
ASSIGNEES_FIELD = gate_issue.ASSIGNEES_FIELD
RECOVERY_COMMENT = """\
{marker}
`{case}` is clear on the {date} nightly: [run]({run_url}). The bot will not close this issue; the triager decides whether it is fixed.
"""
INFRA_TRIAGE = """\
**What triage decides (within a week)**

- **Infrastructure** (pool projects, seeded fleet, Prow, quota): fix it, or link the infra issue that tracks it.
- **Not infrastructure** (a case or harness defect that only looks like it): hand it to the domain owner, or mark the case `expected_fail: true` with this issue linked.

Filed automatically by `scripts/eval_dashboard/nightly_bugs.py` (docs/ci-health.md, "Nightly bugs"). It comments here on the newest night like these each time it runs, and once when the job (or case) runs clean again; it never closes the issue.
"""
INFRA_CASE_TITLE = "Nightly infra: {case} kept from running"
INFRA_CASE_BODY = """\
{marker}
`{case}` was kept from running on its last nights where its part of the nightly (`{job}`) ran to its verdict: infrastructure took every repetition (`classify.py`), or the run recorded no line for the case. Domain: `{domain}` ({domain_owner}).

{addressee}

**Nights** (newest first; a night the part did not run or lost, and a night infrastructure took half the part's cases, are skipped)

{nights_block}

Nightly report: {nightly_url}

{triage}"""
INFRA_JOB_TITLE = "Nightly infra: {job} lost {lost} of the last {nights} nightly dates"
INFRA_JOB_BODY = """\
{marker}
The nightly's {part} part (`{job}`) lost {lost} of the last {nights} nightly dates: Prow ended the build before the eval loop's verdict (the job deadline, an abort) or the pod went away, so the cases it had not reached were never graded.

{addressee}

**Nights** (newest first)

{nights_block}

Nightly report: {nightly_url}

{triage}"""
INFRA_NIGHT_LINE = "- {date}: {how} · [run]({run_url})"
# The case kind's night lines and its "again" phrase carry no classifier
# reason: that text quotes the failing check and the repository is public.
INFRA_CASE_LINE = "- {date} · [run]({run_url}) · [transcript]({transcript_url})"
INFRA_CASE_LINE_NO_TRANSCRIPT = "- {date} · [run]({run_url})"
INFRA_CASE_KEPT = "`{case}` was kept from running"
INFRA_NIGHT_COMMENT = """\
{marker}
Again on the {date} nightly: {what} · [run]({run_url}).
"""
INFRA_RECOVERY_COMMENT = """\
{marker}
{what} on the {date} nightly: [run]({run_url}). The bot will not close this issue; the triager decides whether it is fixed.
"""
INFRA_ASSIGNED = "**Assigned to** @{owner} (the infra owner in `docs/designs/domains.yaml`).{backup}"
INFRA_BACKUP_MENTION = " Infra backup: @{backup}."
INFRA_UNASSIGNED = "**Unassigned:** `docs/designs/domains.yaml` names no `infra_owner`."
INFRA_REFUSED = "**Unassigned:** GitHub refused `{owner}`, the infra owner in `docs/designs/domains.yaml`, as assignee.{backup}"
JOB_RECOVERED = "The {part} part (`{job}`) ran to its verdict"
CASE_RECOVERED = "`{case}` was graded"
WHY_INFRA_OWNER = "infra owner"
# A `failed` entry: a call that failed for a reason other than a refused
# assignee (which moves on to the next attempt), or comments that could not
# be read.
FAILED_NOTE = "a GitHub call failed: a timeout, a 5xx, a 403, or comments that could not be read; nothing more written for it this tick, retried next tick"
WHY_NO_INFRA_OWNER = "`docs/designs/domains.yaml` names no `infra_owner`"

# --- One cause, one issue (module docstring). Three cases, not two: Nov 9's
# 10-of-12 needs no lower bar, and two cases sharing a check from the same
# night are often one probe and its audit in one domain, one owner either
# way; grouping a pair saves one triage and risks hiding a second cause
# behind the first, while three unrelated cases breaking the same named
# check on the same night is no coincidence.
GROUP_MIN_CASES = 3
# Not BOT_MARKER_RE's shape (`nightly-red-group`, not `nightly-red case=`),
# so no reader takes it for a case's marker.
GROUP_MARKER = "<!-- kube-agents-bot:nightly-red-group night={date} -->"
GROUP_MARKER_RE = re.compile(r"<!-- kube-agents-bot:nightly-red-group night=([0-9]{4}-[0-9]{2}-[0-9]{2}) -->")
# One all-clear per recovery: keyed by the newest case's clear night.
GROUP_RECOVERY_MARKER = "<!-- kube-agents-bot:nightly-red-group-recovered night={first} clear={date} -->"
# The checks a failing repetition names, from bench-gate's grading line
# (bench/kube_agents_bench/scoring.py): a tripped safeguard's
# `VerificationCatastrophic=<x> (<name>, <name>): ...`, or a low score's
# `VerificationCorrectness=<x> (floor <y>) -- <name>: <why>; <name>: <why>`.
# A name after the score is read only in a check's shape, two words or more
# joined by `-`, `_` or `.`: a `; want: 3` inside a check's own reason is not
# a check, and a bare word shared by three cases would group them wrongly.
CATASTROPHIC_CHECKS_RE = re.compile(r"^VerificationCatastrophic=\S+ \(([^)]*)\):")
CHECK_NAME_RE = re.compile(r"(?:^|; )([A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)+)(?=: |;|$| \[)")
GROUP_TITLE = "Nightly: cases red together since {date} need triage"
GROUP_BODY = """\
{markers}
These cases turned red on the same nightly, {date}, and have failed the same way on every graded night since, so one change likely broke them all: triage them together, here. Each night's grading is on the dashboard behind its run link. {domains}

{addressee}{owners}

**Cases** (each case's nights, newest first; a night cut off at the job deadline, a night where nothing was graded, and a night where nearly every case failed are skipped, not counted)

{cases_block}

Nightly report: {nightly_url}

**What triage decides (within a week)**

- **Find the cause:** compare the commits of the last green nightly and the first red one ({date}) and name the pull request that likely broke them; its author is the natural fixer.
- **Product bug:** name a fixer, a priority and a target date.
- **Grader or harness:** fix the grading; the cases are not at fault.
- **Infra:** close this issue and point to the infra issue.
- **A case that does not belong here:** delete its `kube-agents-bot:nightly-red case=` line from this body; it is filed on its own if it stays red.

Filed automatically by `scripts/eval_dashboard/nightly_bugs.py` (docs/ci-health.md, "Nightly bugs") in place of one issue per case. It comments here once every case is clear again; it never closes the issue. Closing this one issue closes it for every case listed.
"""
GROUP_CASE_LINE = "- `{case}` (`{domain}`)"
GROUP_DOMAIN = "**{name}** (`{slug}`)"
GROUP_DOMAINS_ONE = "Domain: {domains}."
GROUP_DOMAINS_MANY = "Domains: {domains}."
GROUP_OWNERS = "\nCase owners: {owners}."
WHY_GROUP_DOMAIN_OWNER = "the domain owner in `docs/designs/domains.yaml`"
WHY_GROUP_INFRA_OWNER = "the cases span domains: the infra owner in `docs/designs/domains.yaml`, who takes the nightly failures no one domain owns"
GROUP_RECOVERY_COMMENT = """\
{marker}
Every case here is clear again: {cases}. The bot will not close this issue; the triager decides whether it is fixed, and closing it covers every case.
"""
GROUP_RECOVERY_CASE = "`{case}` on the {date} nightly ([run]({run_url}))"


def log(message: str) -> None:
    print(message, file=sys.stderr)


# --------------------------------------------------------------------------
# repository facts: task.yaml, domains.yaml


def task_fields(case: str, repo_root: pathlib.Path) -> dict | None:
    """``{owner, domain, expected_fail}`` from the case's task.yaml, ``None``
    when the case has no readable task.yaml on this checkout (renamed or
    deleted). ``expected_fail`` is a YAML boolean, as bench-gate reads it."""
    if not CASE_NAME_RE.fullmatch(case or ""):
        return None
    path = repo_root / case_owner.TASKS_PATH / case / case_owner.TASK_FILENAME
    if not path.is_file():
        return None
    doc = load_yaml(path)
    if not doc:
        return None
    # owner and domain through case_owner's line scan, the read the bench
    # validator holds every case to, so the lint and the routing agree.
    text = path.read_text()
    return {"owner": case_owner.task_field(text, TASK_OWNER_KEY), "domain": case_owner.task_field(text, TASK_DOMAIN_KEY),
            "expected_fail": doc.get(TASK_EXPECTED_FAIL_KEY) is True}


def load_yaml(path: pathlib.Path) -> dict:
    try:
        loaded = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        log(f"warning: could not read {path}: {exc}")
        return {}
    return loaded if isinstance(loaded, dict) else {}


def load_domains(repo_root: pathlib.Path) -> dict[str, dict]:
    """``{slug: {name, owner, backup}}`` from domains.yaml."""
    out = {}
    for row in load_yaml(repo_root / case_owner.DOMAINS_PATH).get("domains") or []:
        if isinstance(row, dict) and isinstance(row.get("slug"), str):
            out[row["slug"]] = {key: row.get(key) for key in DOMAIN_ROW_KEYS}
    return out


def resolve_assignee(owner: str | None, domain: str, domain_owners: dict[str, str]) -> tuple[str | None, str]:
    """``(login, why)``: ``case_owner.effective_owner`` -- the case owner when
    it names a GitHub login, else the domain owner. Whether GitHub accepts
    the login is the filer's check (``Filer.file``'s refusal fallback)."""
    login = case_owner.effective_owner(owner, domain, domain_owners=domain_owners)
    if case_owner.is_person_login(owner):
        return login, WHY_CASE_OWNER
    return login, WHY_DOMAIN_OWNER.format(owner=f"`{owner}`" if owner else "not set")


# --------------------------------------------------------------------------
# the nights


def graded_nights(data: dict) -> dict[str, list[dict]]:
    """``{case: [night verdict, ...]}``, oldest first, over the newest
    LOOKBACK_NIGHTS dates on record. A verdict is ``{date, build, outcome,
    passed, graded, infra, run_url, transcript_url, checks}`` for a night
    that graded the case (module docstring: what is skipped); ``checks``
    (``night_checks``) is internal and never printed in public text."""
    nights = nightly.group_nights(nightly.sorted_nightly_runs(data))
    dates = sorted({nightly.night_date(nightly.night_parts(n)[0]) for n in nights} - {None})
    window = set(dates[-LOOKBACK_NIGHTS:])
    by_date: dict[datetime.date, dict[str, dict]] = {}
    for night in nights:
        day = nightly.night_date(nightly.night_parts(night)[0])
        if day not in window:
            continue
        joined = {"tasks": [t for run in nightly.night_parts(night) for t in classify.run_tasks(run)]}
        if classify.is_run_event(joined):
            log(f"night {day}: a run-level event (nearly every graded case failed); skipped for every case")
            continue
        seen: set[str] = set()
        for run in nightly.night_parts(night):
            if nightly.run_truncated(run):
                continue
            for task in classify.run_tasks(run):
                name = task.get("name")
                if not isinstance(name, str) or name in seen:
                    continue
                counts = classify.rep_counts(task)
                outcome = classify.outcome_of(counts)
                if outcome not in GRADED_OUTCOMES:
                    continue
                # Claimed only by a verdict: an ungraded line in one part does
                # not hide another part's verdict for the case that night.
                seen.add(name)
                by_date.setdefault(day, {})[name] = {
                    "date": day.isoformat(),
                    "started": run.get("started"),
                    "finished": run.get("finished"),
                    "build": run.get("build_id"),
                    "outcome": outcome,
                    "passed": counts["pass"],
                    "graded": counts["pass"] + counts["fail"],
                    "infra": counts["infra"] + counts["ceiling"] + counts["fixture"],
                    "run_url": nightly.build_url(run),
                    "transcript_url": nightly.transcript_url(run, name),
                    "checks": night_checks(task),
                }
    out: dict[str, list[dict]] = {}
    for day in sorted(by_date):
        for name, verdict in by_date[day].items():
            out.setdefault(name, []).append(verdict)
    return out


def red_streak(verdicts: list[dict]) -> list[dict] | None:
    """The last STREAK_NIGHTS verdicts when every one is a failure, else None."""
    tail = verdicts[-STREAK_NIGHTS:]
    if len(tail) == STREAK_NIGHTS and all(v["outcome"] == classify.OUTCOME_FAILED for v in tail):
        return tail
    return None


def recovery_night(verdicts: list[dict], news=lambda v: True) -> dict | None:
    """The first all-pass night after the case's last failing night that
    ``news`` accepts (``opened_before`` the issue: an earlier pass the issue
    already saw must not hide a later one), when its newest graded night
    passed; None otherwise, or when no failing night is in the window."""
    if not verdicts or verdicts[-1]["outcome"] != classify.OUTCOME_PASSED:
        return None
    last_fail = max((i for i, v in enumerate(verdicts) if v["outcome"] == classify.OUTCOME_FAILED), default=None)
    if last_fail is None:
        return None
    return next((v for v in verdicts[last_fail + 1:] if v["outcome"] == classify.OUTCOME_PASSED and news(v)), None)


def rep_checks(reason: str | None) -> frozenset:
    """The checks a failing repetition's reason names (CATASTROPHIC_CHECKS_RE,
    CHECK_NAME_RE); empty when it names none (a replay error, an empty
    report, a shape this does not read)."""
    reason = reason or ""
    tripped = CATASTROPHIC_CHECKS_RE.match(reason)
    if tripped:
        return frozenset(n.strip() for n in tripped.group(1).split(",") if CASE_NAME_RE.fullmatch(n.strip()))
    head = classify.REASON_SCORE_PREFIX_RE.match(reason)
    if not head:
        return frozenset()
    tail = reason[head.end():]
    # A name at the very end of a reason the collector cut may be cut short.
    cut = len(reason) >= collect.REP_REASON_MAX_CHARS
    return frozenset(m.group(1) for m in CHECK_NAME_RE.finditer(tail) if not (cut and m.end(1) == len(tail)))


def night_checks(task: dict) -> tuple[str, ...]:
    """The checks every graded failing repetition of ``task`` names, sorted;
    empty when one names none."""
    sets = [rep_checks(r.get("reason")) for r in classify.task_reps(task) if classify.rep_kind(r) == classify.REP_FAIL]
    return tuple(sorted(frozenset.intersection(*sets))) if sets else ()


def red_run(verdicts: list[dict]) -> list[dict]:
    """The newest verdicts that are all failures: the case's current run of
    red nights, oldest first."""
    run = []
    for v in reversed(verdicts):
        if v["outcome"] != classify.OUTCOME_FAILED:
            break
        run.append(v)
    return run[::-1]


def group_keys(verdicts: list[dict]) -> set[tuple[str, str]]:
    """``{(first red night, check)}``: one key per check that every failing
    repetition of the case's red run names."""
    run = red_run(verdicts)
    if not run:
        return set()
    shared = frozenset.intersection(*(frozenset(v.get("checks") or ()) for v in run))
    return {(run[0]["date"], check) for check in shared}


def group_cases(pending: list[dict], min_cases: int = GROUP_MIN_CASES) -> tuple[list[tuple[tuple[str, str], list[dict]]], list[dict]]:
    """``(groups, singles)`` over the cases that would each file
    (``{case, verdicts, ...}``): the key most of them share first, while at
    least ``min_cases`` share one; a case joins one group at most. The
    rest file one issue each."""
    left = list(pending)
    groups = []
    while True:
        keyed: dict[tuple[str, str], list[dict]] = {}
        for entry in left:
            for key in group_keys(entry["verdicts"]):
                keyed.setdefault(key, []).append(entry)
        best = min(keyed.items(), key=lambda kv: (-len(kv[1]), kv[0]), default=None)
        if not best or len(best[1]) < min_cases:
            return groups, left
        groups.append(best)
        taken = {e["case"] for e in best[1]}
        left = [e for e in left if e["case"] not in taken]


def group_clear_night(verdicts: list[dict], issue: dict) -> dict | None:
    """The night a case of a group issue is clear on: its ``recovery_night``
    after the issue opened; or, once its failing nights have left the
    window (it cleared long before the slowest case), its first pass after
    the issue opened, when no night in the window failed and the newest
    passed. None while it is not clear, or has no graded night on record."""
    if not verdicts:
        return None
    night = recovery_night(verdicts, lambda v: opened_before(issue, v))
    if night or any(v["outcome"] == classify.OUTCOME_FAILED for v in verdicts) or verdicts[-1]["outcome"] != classify.OUTCOME_PASSED:
        return night
    return next((v for v in verdicts if v["outcome"] == classify.OUTCOME_PASSED and opened_before(issue, v)), None)


def group_issue_cases(issue: dict) -> list[str]:
    """The cases an open group issue still names, by their markers: a case
    a triager split off (its marker line deleted) is no longer in it."""
    return [key for kind, key in BOT_MARKER_RE.findall(issue.get("body") or "") if kind == KIND_RED]


# --------------------------------------------------------------------------
# what the issue says


def night_block(streak: list[dict]) -> str:
    lines = []
    for v in reversed(streak):
        template = NIGHT_LINE if v["run_url"] else NIGHT_LINE_NO_LINK
        lines.append(template.format(date=v["date"], run_url=v["run_url"], transcript_url=v["transcript_url"]))
    return "\n".join(lines)


def render_issue(case: str, domain: str, domain_row: dict, assignee: str | None, why: str, streak: list[dict]) -> tuple[str, str]:
    title = TITLE.format(case=case)[:TITLE_MAX_CHARS]
    backup = domain_row.get("backup")
    body = BODY.format(
        marker=MARKER.format(case=case),
        case=case,
        domain=domain,
        domain_name=domain_row.get("name") or domain,
        addressee=(ASSIGNED if assignee else UNASSIGNED).format(assignee=assignee, why=why),
        domain_owner=DOMAIN_OWNER_LINE.format(owner=domain_row["owner"], backup=f" (backup @{backup})" if backup else "") if domain_row.get("owner") else NO_DOMAIN_OWNER_LINE,
        nights_block=night_block(streak),
        nightly_url=NIGHTLY_URL,
    )
    return title, body[:ISSUE_BODY_MAX_CHARS]


def render_group_issue(first_red: str, members: list[dict], domains: dict, addressee: str, owners: list[str]) -> tuple[str, str]:
    """The group issue: each case's marker, then its nights as a per-case
    issue lists them. ``members`` are ``{case, domain, streak}``."""
    slugs = sorted({m["domain"] for m in members})
    named = ", ".join(GROUP_DOMAIN.format(name=(domains.get(s) or {}).get("name") or s, slug=s) for s in slugs)
    blocks = []
    for m in members:
        nights = "\n".join(f"  {line}" for line in night_block(m["streak"]).splitlines())
        blocks.append(GROUP_CASE_LINE.format(case=m["case"], domain=m["domain"]) + "\n" + nights)
    body = GROUP_BODY.format(
        markers="\n".join([GROUP_MARKER.format(date=first_red)] + [MARKER.format(case=m["case"]) for m in members]),
        date=first_red,
        domains=(GROUP_DOMAINS_ONE if len(slugs) == 1 else GROUP_DOMAINS_MANY).format(domains=named),
        addressee=addressee,
        owners=GROUP_OWNERS.format(owners=", ".join(f"@{o}" for o in owners)) if owners else "",
        cases_block="\n".join(blocks),
        nightly_url=NIGHTLY_URL,
    )
    return GROUP_TITLE.format(date=first_red)[:TITLE_MAX_CHARS], body[:ISSUE_BODY_MAX_CHARS]


# --------------------------------------------------------------------------
# GitHub


def names_case(issue: dict, case: str, kind: str = KIND_RED) -> bool:
    """The bot's issue by its marker of ``kind``; one without any bot
    marker (a human's) by the case named as a whole word in its title or
    body. The infra kinds are matched on their marker alone: a human's
    `nightly-red` issue about a case is about its code (it must not take
    the infra issue's place or its comments), and one about anything links
    runs whose URLs name the job."""
    body = issue.get("body") or ""
    markers = BOT_MARKER_RE.findall(body)
    # A group issue is matched on its case markers alone, even with none
    # left: a case still listed in its body after its marker went is split off.
    if markers or kind != KIND_RED or GROUP_MARKER_RE.search(body):
        return (kind, case) in markers
    text = f"{issue.get('title') or ''}\n{body}"
    return any(re.search(pattern.format(name=re.escape(case)), text, re.IGNORECASE) for pattern in (NAME_IN_TEXT_RE, NAME_IN_TRANSCRIPT_RE))


def tracking_issue(issues: list[dict], case: str, kind: str = KIND_RED) -> dict | None:
    """The `nightly-red` issue among ``issues`` for ``(kind, case)``: the
    bot's own by marker first, else a human's naming it."""
    issues = [i for i in issues if isinstance(i, dict) and not i.get("pull_request") and i.get("number")]
    for issue in issues:
        if (kind, case) in BOT_MARKER_RE.findall(issue.get("body") or ""):
            return issue
    return next((issue for issue in issues if names_case(issue, case, kind)), None)


def opened_before(issue: dict, night: dict) -> bool:
    """Whether ``issue`` was opened before ``night``'s run finished (its
    start when no finish is recorded): only such a night is news to it, a
    later failure or a recovery. A night still running when the issue opened
    reached data.json after it, so it counts."""
    opened = nightly.parse_iso(issue.get("created_at"))
    ended = nightly.parse_iso(night.get("finished")) or nightly.parse_iso(night.get("started"))
    return bool(opened and ended and ended > opened)


def closed_after(issue: dict | None, night: dict) -> bool:
    """Whether a closed issue was closed after ``night``'s run finished (its
    start when no finish is recorded): only then has the close seen that
    night's verdict, as ``opened_before`` reads an opening."""
    closed = nightly.parse_iso((issue or {}).get("closed_at"))
    ended = nightly.parse_iso(night.get("finished")) or nightly.parse_iso(night.get("started"))
    return bool(closed and ended and closed > ended)


def closed_after_start(issue: dict | None, night: dict) -> bool:
    """Whether a closed issue was closed after ``night``'s run started. That
    run checked out ``main`` before the close, so it tested the code before
    a fix that closed it: on 2026-10-27 a night that started at 00:01 and
    finished after #3863's fix merged at 02:00 re-filed the case as #4039
    (#4041). Its nights count only from the first run started after the
    close."""
    closed = nightly.parse_iso((issue or {}).get("closed_at"))
    started = nightly.parse_iso(night.get("started"))
    return bool(closed and started and closed > started)


# GitHub's state_reason for a close that hands the issue off rather than
# fixing it: "not planned" (or "duplicate") is what a triager picks when
# closing in favour of another issue, a `Fixes #N` merge sets "completed".
HANDED_OFF_REASONS = frozenset({"not_planned", "duplicate"})


def still_tracks(issue: dict, newest: dict, good: list[dict], horizon=None, oldest: dict | None = None) -> bool:
    """Whether a closed issue still tracks a streak ending at ``newest``.
    One closed after that night finished has seen it (``closed_after``). A
    completed close after ``oldest``, the first of the streak's nights,
    started (``closed_after_start``) has too: fewer than STREAK_NIGHTS nights
    tested the code after the close. One
    handed off (HANDED_OFF_REASONS) tracks the problem until a good night --
    a pass, a clean run, a graded case -- finishes after the close: the
    problem is the other issue's until it clears, and a new one after that
    files fresh. A "completed" close that did not fix it files again once
    STREAK_NIGHTS failing nights all started after it. A hand-off closed before ``horizon`` (the start of the
    window's first night) cannot be confirmed -- the record no longer shows
    whether the case cleared since -- so it does not hold a new streak back."""
    if closed_after(issue, newest):
        return True
    if issue.get("state_reason") not in HANDED_OFF_REASONS:
        return bool(oldest and closed_after_start(issue, oldest))
    closed = nightly.parse_iso(issue.get("closed_at"))
    if not closed or not horizon or closed < horizon:
        return False
    for night in good:
        ended = nightly.parse_iso(night.get("finished")) or nightly.parse_iso(night.get("started"))
        if ended and ended > closed:
            return False
    return True


def infra_issue_part(issue: dict) -> str:
    """The part an open kept-case issue names: the part of the job its body
    names (one of ``nightly.PART_JOBS``), else main."""
    body = issue.get("body") or ""
    return next((part for job, part in nightly.PART_JOBS.items() if f"`{job}`" in body), nightly.PART_MAIN)


def listing_doubt(gh: ghcli.Gh, open_issues: list, closed_issues: list, since: str | None, label: str = LABEL) -> str | None:
    """Why the REST listings of ``label`` cannot be trusted, or None when
    GraphQL agrees with them (LISTING_QUERY). A dedupe that reads an empty
    list files every case again."""
    owner, _, name = gh.repo.partition("/")
    variables = {"owner": owner, "name": name, "label": label, "since": since, "closed": bool(since)}
    found = gh.call("POST", GRAPHQL_PATH, {"query": LISTING_QUERY, "variables": variables}, read=True)
    repo = ((found or {}).get("data") or {}).get("repository") if isinstance(found, dict) else None
    if not isinstance(repo, dict) or (found or {}).get("errors"):
        return "the GraphQL cross-check could not be read"
    for state, listed in (("open", open_issues), ("closed", closed_issues)):
        if state == "closed" and not since:
            continue
        conn = repo.get(state)
        if not isinstance(conn, dict) or not isinstance(conn.get("totalCount"), int) or not isinstance(conn.get("nodes"), list):
            return f"the GraphQL cross-check of the {state} issues could not be read"
        numbers = {i.get("number") for i in listed if isinstance(i, dict) and not i.get("pull_request")}
        missing = sorted(n.get("number") for n in conn["nodes"] if isinstance(n, dict) and isinstance(n.get("number"), int) and n.get("number") not in numbers)
        if missing or len(numbers) < conn["totalCount"]:
            return (f"the {state} listing holds {len(numbers)} issues, GraphQL {conn['totalCount']}"
                    + (f" (missing {', '.join(f'#{n}' for n in missing)})" if missing else ""))
    return None


def read_listings(gh: ghcli.Gh, since: str | None, label: str = LABEL) -> tuple[list | None, list | None, str | None]:
    """``(open, closed, doubt)``: the two REST listings of ``label`` (closed ones updated
    since ``since``; none without it), read up to LISTING_ATTEMPTS times
    until GraphQL agrees with them (``listing_doubt``). ``open`` or
    ``closed`` is None when a read failed; ``doubt`` is why the last read is
    not trusted, None when it is. The filer, the digest's nightly section
    and the coverage-gap filer (``coverage_gaps.py``, label
    `presubmit-gate`) read the issues through this."""
    doubt = None
    for attempt in range(1, LISTING_ATTEMPTS + 1):
        open_issues = gh.call("GET", gh.path(OPEN_ISSUES_PATH.format(label=label)), paginate=True)
        closed_issues = gh.call("GET", gh.path(CLOSED_ISSUES_PATH.format(label=label, since=since)), paginate=True) if since else []
        if open_issues is None or closed_issues is None:
            return open_issues, closed_issues, None
        doubt = listing_doubt(gh, open_issues, closed_issues, since, label)
        if not doubt:
            break
        log(f"warning: the {label} issue listing is not trusted ({doubt}); read {attempt} of {LISTING_ATTEMPTS}")
        if attempt < LISTING_ATTEMPTS:
            time.sleep(LISTING_RETRY_DELAY_S)
    return open_issues, closed_issues, doubt


def held_state(prev: dict | None, doubt: str | None, live: bool, now: datetime.datetime) -> dict:
    """The ``--state`` document after a pass whose listings were read:
    ``held_ticks`` live passes in a row with no trusted listing, since
    ``held_since``, last for ``doubt``. A trusted pass, or a dry run, holds
    nothing. A count older than HELD_STATE_FRESH starts over: the ticks
    between went unrecorded, so they were not held in a row."""
    prev = prev or {}
    updated = post_health.parse_iso(prev.get("updated_at"))
    if not updated or now - updated > HELD_STATE_FRESH:
        prev = {}
    stamp = now.isoformat(timespec="seconds")
    if not (doubt and live):
        return {"held_ticks": 0, "held_since": None, "doubt": None, "updated_at": stamp}
    ticks = prev.get("held_ticks") if isinstance(prev.get("held_ticks"), int) else 0
    return {"held_ticks": ticks + 1, "held_since": (prev.get("held_since") if ticks else None) or stamp, "doubt": doubt, "updated_at": stamp}


class StateUnreadable(OSError):
    """The ``--state`` object exists, or may, and could not be read."""


def read_held_state(location: str, runner=subprocess.run) -> dict | None:
    """The ``--state`` document, or None when there is none yet. A gs://
    read that failed for any reason but an absent object raises
    StateUnreadable, so the filer does not write a fresh count over the
    record and the digest does not take the failure for no hold."""
    if not location.startswith(post_health.GS_PREFIX):
        return post_health.read_state(location)
    result = runner([post_health.GCLOUD, post_health.STORAGE_GROUP, "cat", location], capture_output=True, text=True)
    if result.returncode != 0:
        if STATE_ABSENT_RE.search(result.stderr or ""):
            return None
        raise StateUnreadable(f"gcloud storage cat {location} failed: {(result.stderr or '').strip()[:STATE_ERROR_CHARS]}")
    try:
        loaded = json.loads(result.stdout)
    except ValueError:
        return None
    return loaded if isinstance(loaded, dict) else None


def held_digest_lines(state: dict | None, now: datetime.datetime) -> list[str]:
    """The digest's line while filing has been held HELD_TICKS_DIGEST ticks
    in a row; none otherwise, or when the state is stale (HELD_STATE_FRESH)."""
    state = state or {}
    ticks = state.get("held_ticks")
    updated = post_health.parse_iso(state.get("updated_at"))
    if not isinstance(ticks, int) or ticks < HELD_TICKS_DIGEST or not updated or now - updated > HELD_STATE_FRESH:
        return []
    since = post_health.parse_iso(state.get("held_since"))
    if not since:
        since_text = HELD_SINCE_UNKNOWN
    elif now - since > HELD_SINCE_DATED_AFTER:
        since_text = f"{since.astimezone(post_health.LOCAL_TZ).strftime('%a %b %-d')} {post_health.clock(since)}"
    else:
        since_text = post_health.clock(since, weekday=True)
    return [HELD_DIGEST_LINE.format(since=since_text, doubt=state.get("doubt") or "no reason recorded")]


class Filer:
    def __init__(self, gh: ghcli.Gh, repo_root: pathlib.Path = REPO_ROOT):
        self.gh = gh
        self.repo_root = repo_root
        self.domains = load_domains(repo_root)
        self.horizon = None
        try:
            self.domain_owners = case_owner.load_domain_owners(repo_root / case_owner.DOMAINS_PATH)
        except OSError as exc:
            log(f"warning: could not read domains.yaml: {exc}")
            self.domain_owners = {}
        self.infra_owner, self.infra_backup = gate_issue.infra_addressees(repo_root / case_owner.DOMAINS_PATH)
        # The listing's verdict this pass: None until it was read, then the
        # doubt or "" (trusted). main records it in --state.
        self.doubt = None

    def commented(self, issue: dict, marker: str) -> bool | None:
        """Whether a comment on ``issue`` carries ``marker``; None when the
        comments cannot be read, or the read returned fewer than the issue
        listing counts (the REST listings have answered empty while the
        items existed, LISTING_QUERY)."""
        number = issue["number"]
        comments = self.gh.call("GET", self.gh.path(COMMENTS_READ_PATH.format(number=number)), paginate=True)
        if comments is None:
            return None
        if isinstance(issue.get("comments"), int) and len(comments) < issue["comments"]:
            log(f"warning: #{number} lists {issue['comments']} comments and the read returned {len(comments)}")
            return None
        return any(marker in (c.get("body") or "") for c in comments if isinstance(c, dict))

    def run(self, data: dict) -> dict:
        """One pass: ``{filed, recovered, tracked, skipped, failed}``, each a
        list of what was (or, dry, would be) done. Nothing is written when
        the issues cannot be read, or when GraphQL disagrees with the
        listing (``listing_doubt``): a dedupe that cannot see is not a dedupe."""
        result = {"filed": [], "recovered": [], "noted": [], "tracked": [], "skipped": [], "failed": []}
        nights = graded_nights(data)
        parts = nightly_infra.part_nights(data, LOOKBACK_NIGHTS)
        starts = [nightly.parse_iso(n.get("started")) for ns in parts.values() for n in ns]
        self.horizon = min((t for t in starts if t), default=None)
        since = self.horizon.strftime(GITHUB_TIME_FORMAT) if self.horizon else None
        open_issues, closed_issues, doubt = read_listings(self.gh, since)
        if open_issues is None or closed_issues is None:
            log("warning: could not list the nightly-red issues; nothing filed or commented this pass")
            return result
        self.doubt = doubt or ""
        if doubt:
            log("warning: no trusted nightly-red issue listing this pass; nothing filed or commented, retried next tick")
            return result
        active = set(nightly.expected_cases(data))
        pending = []
        for case, verdicts in sorted(nights.items()):
            fields = task_fields(case, self.repo_root)
            issue = tracking_issue(open_issues, case)
            streak = red_streak(verdicts)
            if streak:
                passes = [v for v in verdicts if v["outcome"] == classify.OUTCOME_PASSED]
                closed = tracking_issue([i for i in closed_issues if still_tracks(i, streak[-1], passes, self.horizon, streak[0])], case)
                if fields is None:
                    result["skipped"].append({"case": case, "why": "no readable task.yaml on this checkout"})
                elif case not in active:
                    result["skipped"].append({"case": case, "why": "not on this checkout's nightly matrix"})
                elif fields["expected_fail"]:
                    result["skipped"].append({"case": case, "why": "expected_fail: true"})
                elif issue:
                    result["tracked"].append({"case": case, "issue": issue["number"]})
                elif closed:
                    result["tracked"].append({"case": case, "issue": closed["number"], "closed": True})
                else:
                    pending.append({"case": case, "fields": fields, "streak": streak, "verdicts": verdicts})
                continue
            # A group issue gets one all-clear (recover_groups) instead.
            grouped = bool(issue and GROUP_MARKER_RE.search(issue.get("body") or ""))
            recovered = recovery_night(verdicts, lambda v: opened_before(issue, v)) if issue and not grouped else None
            if recovered and issue:
                done = self.recover(case, issue, recovered)
                if done:
                    result["recovered" if done["ok"] else "failed"].append(done)
        groups, singles = group_cases(pending)
        for (first_red, _check), members in groups:
            filed = self.file_group(first_red, members)
            result["filed" if filed["issue"] or self.gh.dry_run else "failed"].append(filed)
        for entry in singles:
            filed = self.file(entry["case"], entry["fields"], entry["streak"])
            result["filed" if filed["issue"] or self.gh.dry_run else "failed"].append(filed)
        self.recover_groups(nights, open_issues, result)
        self.run_infra(data, parts, active, open_issues, closed_issues, result)
        return result

    def run_infra(self, data: dict, parts: dict, active: set, open_issues: list, closed_issues: list, result: dict) -> None:
        """The two infra shapes (module docstring, nightly_infra.py)."""
        # An open job issue: one comment on the part's newest night when it
        # was lost after the issue opened, or once when it ran clean again.
        for part, part_list in parts.items():
            job = part_list[-1]["job"] if part_list else None
            issue = tracking_issue(open_issues, job, KIND_LOST) if job else None
            if not issue:
                continue
            result["tracked"].append({"case": job, "issue": issue["number"]})
            if part_list[-1]["lost"]:
                self.note_night(KIND_LOST, KEY_JOB, job, issue, part_list[-1], part_list[-1]["how"], result)
            clean = nightly_infra.first_clean_after_lost(part_list, lambda n: opened_before(issue, n))
            if clean:
                self.recover_infra(KIND_LOST, KEY_JOB, job, issue, clean, JOB_RECOVERED.format(part=part, job=job), result)
        for lost in nightly_infra.lost_jobs(parts):
            job = lost["job"]
            if tracking_issue(open_issues, job, KIND_LOST):
                continue
            losses = [n for n in lost["nights"] if n["lost"]]
            title = INFRA_JOB_TITLE.format(job=job, lost=len(losses), nights=nightly_infra.JOB_LOST_WINDOW)
            body = functools.partial(
                INFRA_JOB_BODY.format,
                marker=BOT_MARKER.format(kind=KIND_LOST, field=KEY_JOB, key=job), part=lost["part"], job=job, lost=len(losses),
                nights=nightly_infra.JOB_LOST_WINDOW,
                nights_block="\n".join(INFRA_NIGHT_LINE.format(date=n["date"], how=n["how"], run_url=n["run_url"]) for n in reversed(lost["nights"])),
                nightly_url=NIGHTLY_URL, triage=INFRA_TRIAGE)
            clean = [n for n in parts.get(lost["part"], []) if not n["lost"]]
            self.file_infra(KIND_LOST, job, losses[-1], title, body, lost["nights"], clean, closed_issues, result)
        candidates = sorted(c for c in active if c)
        # A case with an open kept-case issue is read even with no line in
        # the window, on the part its issue names, so the issue keeps its
        # notes and gets its recovery.
        tracked = {}
        for case in candidates:
            open_issue = tracking_issue(open_issues, case, KIND_INFRA)
            if open_issue:
                tracked[case] = infra_issue_part(open_issue)
        for case, verdicts in sorted(nightly_infra.case_verdicts(parts, candidates, tracked).items()):
            fields = task_fields(case, self.repo_root)
            if fields is None:
                continue
            issue = tracking_issue(open_issues, case, KIND_INFRA)
            # An open issue keeps its notes and recovery even once the case
            # is marked expected_fail, as its footer tells the triager to.
            if issue:
                # Open: the newest eligible night, kept from running after the
                # issue opened, gets one comment; a graded one, one recovery.
                result["tracked"].append({"case": case, "issue": issue["number"]})
                if verdicts[-1]["state"] in nightly_infra.KEPT_STATES:
                    self.note_night(KIND_INFRA, KEY_CASE, case, issue, verdicts[-1], INFRA_CASE_KEPT.format(case=case), result)
                graded = nightly_infra.first_graded_after_kept(verdicts, lambda v: opened_before(issue, v))
                if graded:
                    self.recover_infra(KIND_INFRA, KEY_CASE, case, issue, graded, CASE_RECOVERED.format(case=case), result)
                continue
            if fields["expected_fail"]:
                continue
            streak = nightly_infra.kept_streak(verdicts)
            if streak:
                domain = fields.get("domain") or UNKNOWN
                job = streak[-1]["job"]
                title = INFRA_CASE_TITLE.format(case=case)
                body = functools.partial(
                    INFRA_CASE_BODY.format,
                    marker=BOT_MARKER.format(kind=KIND_INFRA, field=KEY_CASE, key=case), case=case,
                    job=job, domain=domain, domain_owner=self.infra_domain_owner(domain),
                    nights_block="\n".join((INFRA_CASE_LINE if v["transcript_url"] else INFRA_CASE_LINE_NO_TRANSCRIPT).format(
                        date=v["date"], run_url=v["run_url"], transcript_url=v["transcript_url"]) for v in reversed(streak)),
                    nightly_url=NIGHTLY_URL, triage=INFRA_TRIAGE)
                cleared = [v for v in verdicts if v["state"] == nightly_infra.STATE_GRADED]
                self.file_infra(KIND_INFRA, case, streak[-1], title, body, streak, cleared, closed_issues, result)

    def infra_domain_owner(self, domain: str) -> str:
        owner = (self.domains.get(domain) or {}).get("owner")
        return INFRA_DOMAIN_OWNER.format(owner=owner) if owner else INFRA_NO_DOMAIN_OWNER

    def addressee(self, refused: bool = False) -> str:
        """The infra issue's assignment line; ``refused`` when GitHub would
        not assign the infra owner."""
        if not self.infra_owner:
            return INFRA_UNASSIGNED
        backup = INFRA_BACKUP_MENTION.format(backup=self.infra_backup) if self.infra_backup else ""
        return (INFRA_REFUSED if refused else INFRA_ASSIGNED).format(owner=self.infra_owner, backup=backup)

    def note_night(self, kind, field, key, issue, night, what, result) -> None:
        """One comment on an open infra issue for ``night``, when it started
        after the issue was opened."""
        if not opened_before(issue, night):
            return
        marker = NIGHT_MARKER.format(kind=kind, field=field, key=key, date=night["date"])
        comment = INFRA_NIGHT_COMMENT.format(marker=marker, date=night["date"], what=what, run_url=night["run_url"])
        done = self.comment_once(issue, marker, comment)
        if done is not None:
            result["noted" if done else "failed"].append({"case": key, "issue": issue["number"], "night": night["date"], "run": night["run_url"]})

    def file_infra(self, kind, key, newest, title, body, nights, good, closed_issues, result) -> None:
        """File the infra issue for ``key`` unless a closed one still tracks
        this streak (``still_tracks``, from the streak's oldest night
        ``nights[0]``; ``good`` are the clean or graded nights). ``body(addressee=...)`` renders it with the assignment
        line."""
        closed = tracking_issue([i for i in closed_issues if still_tracks(i, newest, good, self.horizon, nights[0])], key, kind)
        if closed:
            result["tracked"].append({"case": key, "issue": closed["number"], "closed": True})
            return
        labels = [LABEL, INFRA_LABEL, TRIAGE_LABEL]

        def payload(owner, refused=False):
            return {"title": title[:TITLE_MAX_CHARS], "body": body(addressee=self.addressee(refused))[:ISSUE_BODY_MAX_CHARS], "labels": labels,
                    **({"assignees": [owner]} if owner else {})}

        attempts = [(self.infra_owner, payload(self.infra_owner))]
        if self.infra_owner:
            attempts.append((None, payload(None, refused=True)))
        created, assignee = self.create(attempts, key)
        number = created.get("number") if isinstance(created, dict) else None
        why = WHY_INFRA_OWNER if assignee else WHY_UNASSIGNED_REFUSED.format(refused=f"`{self.infra_owner}`") if self.infra_owner else WHY_NO_INFRA_OWNER
        entry = {"case": key, "assignee": assignee, "why": why, "mention": self.infra_backup, "labels": labels,
                 "nights": [n["date"] for n in nights], "runs": [n["run_url"] for n in nights], "issue": number}
        result["filed" if number or self.gh.dry_run else "failed"].append(entry)

    def recover_infra(self, kind, field, key, issue, night, what, result) -> None:
        """One recovery comment, for a clean night after the issue opened:
        a clean night the issue already lists is not a recovery."""
        if not opened_before(issue, night):
            return
        marker = INFRA_RECOVERY_MARKER.format(kind=kind, field=field, key=key, date=night["date"])
        comment = INFRA_RECOVERY_COMMENT.format(marker=marker, what=what, date=night["date"], run_url=night["run_url"])
        done = self.comment_once(issue, marker, comment)
        if done is not None:
            result["recovered" if done else "failed"].append({"case": key, "issue": issue["number"], "night": night["date"], "run": night["run_url"]})

    def comment_once(self, issue: dict, marker: str, body: str) -> bool | None:
        """Post ``body`` unless a comment already carries ``marker``: None
        when it is already there, else whether the post went through (or,
        dry, would be sent). Comments that cannot be read are a failure:
        the post is owed and was not attempted."""
        number = issue["number"]
        already = self.commented(issue, marker)
        if already:
            return None
        if already is None:
            log(f"warning: could not read the comments on #{number}; not commenting this tick")
            return False
        return self.gh.call("POST", self.gh.path(COMMENTS_PATH.format(number=number)), {"body": body}) is not None or self.gh.dry_run

    def create(self, attempts: list[tuple[str | None, dict]], key: str) -> tuple[dict | None, str | None]:
        """POST the first of ``attempts`` (``(assignee, payload)``, each
        body saying who it is assigned to) that GitHub takes, and return it
        with its assignee. A refused assignee moves on to the next attempt.
        Any other failure (a timeout, a 5xx, a 403) stops: nothing more is
        written this tick, and the next tick's issue list finds an issue the
        call created anyway by its marker, so it is never filed twice."""
        for assignee, payload in attempts:
            created = self.gh.call("POST", self.gh.path(ISSUES_PATH), payload)
            if created is not None or self.gh.dry_run:
                return created, assignee
            if not (assignee and self.gh.refused(ASSIGNEES_FIELD)):
                return None, assignee
            log(f"warning: GitHub refused @{assignee} as the assignee of {key}'s issue; trying the next")
        return None, None

    def file(self, case: str, fields: dict, streak: list[dict]) -> dict:
        domain = fields.get("domain") or UNKNOWN
        row = self.domains.get(domain) or {}
        domain_owner = case_owner.domain_owner(domain, self.domain_owners)
        assignee, why = resolve_assignee(fields.get("owner"), domain, self.domain_owners)
        # Who it goes to, in order: the resolved owner; the domain owner when
        # GitHub refuses a case owner; no one, saying who was refused.
        candidates = [(assignee, why)] if assignee else []
        if assignee and domain_owner and assignee.lower() != domain_owner.lower():
            candidates.append((domain_owner, WHY_REFUSED.format(refused=assignee)))
        refused = ", ".join(f"`{login}`" for login, _ in candidates)
        candidates.append((None, WHY_UNASSIGNED_REFUSED.format(refused=refused) if refused else WHY_UNASSIGNED_NO_OWNER.format(domain=domain)))
        labels = [LABEL, DOMAIN_LABEL.format(slug=domain), TRIAGE_LABEL]
        attempts, reasons = [], {}
        for login, reason in candidates:
            title, body = render_issue(case, domain, row, login, reason, streak)
            attempts.append((login, {"title": title, "body": body, "labels": labels, **({"assignees": [login]} if login else {})}))
            reasons[login] = reason
        created, assignee = self.create(attempts, case)
        number = created.get("number") if isinstance(created, dict) else None
        if number:
            log(f"nightly bug: filed #{number} for {case}")
        return {"case": case, "domain": domain, "assignee": assignee, "why": reasons[assignee], "mention": domain_owner,
                "labels": labels, "nights": [v["date"] for v in streak], "runs": [v["run_url"] for v in streak], "issue": number}

    def file_group(self, first_red: str, entries: list[dict]) -> dict:
        """One issue for ``entries``, the cases that turned red on
        ``first_red`` with a failing check in common (``group_cases``):
        to the domain owner, or to the infra owner when they span domains;
        unassigned, saying who was refused, when GitHub refuses that login."""
        members = [{"case": e["case"], "domain": e["fields"].get("domain") or UNKNOWN, "streak": e["streak"]} for e in entries]
        slugs = sorted({m["domain"] for m in members})
        if len(slugs) == 1:
            owner, why = case_owner.domain_owner(slugs[0], self.domain_owners), WHY_GROUP_DOMAIN_OWNER
            nobody = WHY_UNASSIGNED_NO_OWNER.format(domain=slugs[0])
        else:
            owner, why, nobody = self.infra_owner, WHY_GROUP_INFRA_OWNER, WHY_NO_INFRA_OWNER
        owners = []
        for e, m in zip(entries, members):
            login, _ = resolve_assignee(e["fields"].get("owner"), m["domain"], self.domain_owners)
            if login and login.lower() not in {o.lower() for o in owners} and login.lower() != (owner or "").lower():
                owners.append(login)
        candidates = ([(owner, why)] if owner else []) + [(None, WHY_UNASSIGNED_REFUSED.format(refused=f"`{owner}`") if owner else nobody)]
        labels = [LABEL] + [DOMAIN_LABEL.format(slug=s) for s in slugs] + [TRIAGE_LABEL]
        attempts, reasons = [], {}
        for login, reason in candidates:
            addressee = (ASSIGNED if login else UNASSIGNED).format(assignee=login, why=reason)
            title, body = render_group_issue(first_red, members, self.domains, addressee, owners)
            attempts.append((login, {"title": title, "body": body, "labels": labels, **({"assignees": [login]} if login else {})}))
            reasons[login] = reason
        cases = [m["case"] for m in members]
        created, assignee = self.create(attempts, f"the group since {first_red}")
        number = created.get("number") if isinstance(created, dict) else None
        if number:
            log(f"nightly bug: filed #{number} for {', '.join(cases)} (red together since {first_red})")
        return {"case": f"{', '.join(cases)} (one issue: red together since {first_red})", "cases": cases, "assignee": assignee, "why": reasons[assignee],
                "mention": owners, "labels": labels, "nights": [first_red], "runs": [v["run_url"] for m in members for v in m["streak"]], "issue": number}

    def recover_groups(self, nights: dict, open_issues: list, result: dict) -> None:
        """One comment on an open group issue once every case it names has
        recovered on a night after it opened (``recovery_night``). A case
        with no graded night on record holds the comment back: nothing says
        it is clear."""
        for issue in open_issues:
            if not isinstance(issue, dict) or issue.get("pull_request") or not issue.get("number"):
                continue
            marked = GROUP_MARKER_RE.search(issue.get("body") or "")
            cases = group_issue_cases(issue) if marked else []
            if not cases:
                continue
            clear = []
            for case in cases:
                night = group_clear_night(nights.get(case), issue)
                if not night:
                    break
                clear.append((case, night))
            else:
                # At least one case's red nights must still be on record, as
                # for one case (LOOKBACK_NIGHTS): once they have all left the
                # window, the fallback's clear night is the window's oldest
                # date, which moves every night and would post again each time.
                if not any(recovery_night(nights.get(case), lambda v, issue=issue: opened_before(issue, v)) for case in cases):
                    continue
                newest = max((night for _, night in clear), key=lambda v: v["date"])
                marker = GROUP_RECOVERY_MARKER.format(first=marked.group(1), date=newest["date"])
                comment = GROUP_RECOVERY_COMMENT.format(marker=marker, cases=", ".join(
                    GROUP_RECOVERY_CASE.format(case=case, date=night["date"], run_url=night["run_url"]) for case, night in clear))
                done = self.comment_once(issue, marker, comment)
                if done is not None:
                    result["recovered" if done else "failed"].append({"case": f"every case of the group ({', '.join(cases)})", "issue": issue["number"],
                                                                       "night": newest["date"], "run": newest["run_url"]})

    def recover(self, case: str, issue: dict, night: dict) -> dict | None:
        """Comment once on ``night``'s pass, when the night started after the
        issue was opened (a human may open one after the case recovered)."""
        if not opened_before(issue, night):
            return None
        marker = RECOVERY_MARKER.format(case=case, date=night["date"])
        body = RECOVERY_COMMENT.format(marker=marker, case=case, date=night["date"], run_url=night["run_url"])
        posted = self.comment_once(issue, marker, body)
        if posted is None:
            return None
        return {"case": case, "issue": issue["number"], "night": night["date"], "run": night["run_url"], "ok": posted}


# --------------------------------------------------------------------------
# entry point


def summary(result: dict, dry_run: bool) -> str:
    verb = "would file" if dry_run else "filed"
    commented = "would comment on" if dry_run else "commented on"
    head = (
        f"nightly bugs ({'dry run' if dry_run else 'live'}): {verb} {len(result['filed'])}, "
        f"{commented} {len(result['recovered'])} recovered and {len(result['noted'])} with a new night, "
        f"{len(result['tracked'])} already tracked, {len(result['skipped'])} skipped, {len(result['failed'])} failed"
    )
    lines = [head]
    for f in result["filed"]:
        issue = f" #{f['issue']}" if f["issue"] else ""
        who = f"assign @{f['assignee']}" if f["assignee"] else "unassigned"
        mentions = f["mention"] if isinstance(f["mention"], list) else [f["mention"]] if f["mention"] else []
        mention = f"; mention {', '.join(f'@{m}' for m in mentions)}" if mentions else ""
        lines.append(f"  file{issue}: {f['case']} [{', '.join(f['labels'])}] {who} ({f['why']}){mention}; nights {', '.join(f['nights'])}")
        lines.extend(f"    {url}" for url in f["runs"] if url)
    lines += [f"  recovered: {r['case']} on #{r['issue']} ({r['night']}, {r['run']})" for r in result["recovered"]]
    lines += [f"  new night: {r['case']} on #{r['issue']} ({r['night']}, {r['run']})" for r in result["noted"]]
    lines += [f"  tracked: {t['case']} by #{t['issue']}{' (closed, still tracking)' if t.get('closed') else ''}" for t in result["tracked"]]
    lines += [f"  skipped: {s['case']} ({s['why']})" for s in result["skipped"]]
    lines += [f"  failed: {f['case']} ({FAILED_NOTE})" for f in result["failed"]]
    return "\n".join(lines)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", required=True, type=pathlib.Path, help="data.json the collector wrote")
    parser.add_argument("--repo", default=ghcli.DEFAULT_REPO)
    parser.add_argument("--repo-root", type=pathlib.Path, default=REPO_ROOT, help="checkout holding bench/tasks and domains.yaml")
    parser.add_argument("--state", help="held-filing state (held_state): local path or gs:// object; read and rewritten each pass whose listings were read")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true", default=True, help="print every write instead of sending it (the default)")
    mode.add_argument("--live", dest="dry_run", action="store_false", help="send the writes")
    return parser.parse_args(argv)


def main(argv=None, gh_runner=None) -> int:
    args = parse_args(argv)
    if yaml is None:
        log("ERROR: pyyaml is required to read task.yaml and domains.yaml")
        return 1
    try:
        data = json.loads(args.data.read_text())
    except (OSError, ValueError) as exc:
        log(f"ERROR: {args.data}: {exc}")
        return 1
    gh = ghcli.Gh(args.repo, gh_runner, dry_run=args.dry_run) if gh_runner else ghcli.Gh(args.repo, dry_run=args.dry_run)
    filer = Filer(gh, args.repo_root)
    result = filer.run(data if isinstance(data, dict) else {})
    print(summary(result, args.dry_run))
    # A live pass that could not read the listings at all neither trusted
    # nor doubted them: it carries an open hold forward with the count
    # unchanged, so an hour of failed reads does not age it out
    # (HELD_STATE_FRESH) and restart it.
    if args.state and (filer.doubt is not None or not args.dry_run):
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            prev = read_held_state(args.state)
            if filer.doubt is not None:
                post_health.write_state(args.state, held_state(prev, filer.doubt, not args.dry_run, now))
            elif (prev or {}).get("held_ticks"):
                post_health.write_state(args.state, dict(prev, updated_at=now.isoformat(timespec="seconds")))
        except (OSError, subprocess.CalledProcessError) as exc:
            log(f"warning: could not update {args.state}: {exc}; the held-tick count is retried next tick")
    return 0


if __name__ == "__main__":
    sys.exit(main())
