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
    and none was closed after the streak's newest night, or closed as
        not planned with no pass since (``still_tracks``)
    -> file one, labelled `nightly-red`, `domain:<slug>` and `needs-triage`,
       assigned per the rule below

    a case with an open `nightly-red` issue passed every graded repetition
    on a graded night after its last failing one
    -> comment once on the issue with that night's run link; never close it

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
    -> one issue for the case with the classifier's reason; a comment on
       each later such night, and one when it is graded again

The domain streak is untouched by either: an infrastructure repetition is
dropped before a night counts as failing.

**A listing it cannot trust files nothing.** Both issue listings are
cross-checked against the same issues read through GraphQL
(``listing_doubt``); when GraphQL cannot be read, or names more issues
than the listing holds, the listings are read again (LISTING_ATTEMPTS),
and when no read agrees the tick writes nothing and logs why; the next tick
retries. Comments are read the same way: an issue whose listing counts
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
    from eval_dashboard import classify, gate_issue, ghcli, nightly, nightly_infra
    from eval_dashboard.post_health import NIGHTLY_URL
except ImportError:  # run as a script: scripts/eval_dashboard/nightly_bugs.py
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from eval_dashboard import classify, gate_issue, ghcli, nightly, nightly_infra
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
OPEN_ISSUES_PATH = f"issues?labels={LABEL}&state=open&per_page=100"
# Closed ones too, updated since the window's first night: a bug closed
# after the streak's newest night has seen this streak, so it is not filed
# again until a newer failing night (a fix that merged before the next
# nightly closes its issue, and the streak on record is still red); one
# handed off as not planned counts only while its close is on record
# (``still_tracks``), so nothing older is needed.
CLOSED_ISSUES_PATH = f"issues?labels={LABEL}&state=closed&since={{since}}&per_page=100"
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
# its marker alone: its body quotes failure reasons, which can name another
# case.
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
# Distinct failing-check reasons quoted per night, and their length
# (nightly.REASON_MAX_CHARS, the collector's own cap).
REASONS_PER_NIGHT = 3
REASON_MAX_CHARS = nightly.REASON_MAX_CHARS
UNKNOWN = "unknown"
# The domain owner in an issue body; a domain with no owner on record is
# said so rather than @-mentioning an account named "unknown".
DOMAIN_OWNER_LINE = "Domain owner: @{owner}{backup}."
NO_DOMAIN_OWNER_LINE = "No domain owner in `docs/designs/domains.yaml`."
INFRA_DOMAIN_OWNER = "owner @{owner}"
INFRA_NO_DOMAIN_OWNER = "no owner in `docs/designs/domains.yaml`"

TITLE = "Nightly: {case} fails every run, {nights} nights in a row"
BODY = """\
{marker}
`{case}` failed every graded repetition on its last {nights} graded nightly nights. Domain: **{domain_name}** (`{domain}`).

{addressee} {domain_owner}

**Nights** (newest first; a night cut off at the job deadline, a night where nothing was graded, and a night where nearly every case failed are skipped, not counted)

{nights_block}

Nightly report: {nightly_url}

**What triage decides (within a week)**

- **Product bug:** name a fixer, a priority and a target date.
- **Broken test:** fix the case, or mark it `expected_fail: true` in its `task.yaml` with this issue linked.
- **Infra:** close this issue and point to the infra issue.

Filed automatically by `scripts/eval_dashboard/nightly_bugs.py` (docs/ci-health.md, "Nightly bugs"). It comments here when the case passes every repetition again; it never closes the issue.
"""
NIGHT_LINE = "- {date}: {passed}/{graded} passed{infra} · [run]({run_url}) · [transcript]({transcript_url})"
NIGHT_LINE_NO_LINK = "- {date}: {passed}/{graded} passed{infra}"
# A reason is quoted as code: it can carry `@login` or `org/repo#N`, which
# GitHub would otherwise turn into a mention or a cross-reference.
REASON_LINE = "  - failed: {reason}"
# What a quoted reason's `<` becomes: SINGLE LEFT-POINTING ANGLE QUOTATION MARK.
MARKER_SAFE_LT = "\u2039"
NO_REASON_LINE = "  - (no reason recorded)"
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
`{case}` passed every graded repetition ({passed}/{graded}) on the {date} nightly: [run]({run_url}). The bot will not close this issue; the triager decides whether it is fixed.
"""
INFRA_TRIAGE = """\
**What triage decides (within a week)**

- **Infrastructure** (pool projects, seeded fleet, Prow, quota): fix it, or link the infra issue that tracks it.
- **Not infrastructure** (a case or harness defect that only looks like it): hand it to the domain owner, or mark the case `expected_fail: true` with this issue linked.

Filed automatically by `scripts/eval_dashboard/nightly_bugs.py` (docs/ci-health.md, "Nightly bugs"). It comments here on the newest night like these each time it runs, and once when the job (or case) runs clean again; it never closes the issue.
"""
INFRA_CASE_TITLE = "Nightly infra: {case} kept from running {nights} nights in a row"
INFRA_CASE_BODY = """\
{marker}
`{case}` graded nothing on its last {nights} nights where its part of the nightly (`{job}`) ran to its verdict: every repetition was classified as infrastructure (`classify.py`), or the run recorded no line for the case. Domain: `{domain}` ({domain_owner}).

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
INFRA_CASE_LINE = "- {date}: {reason} · [run]({run_url}) · [transcript]({transcript_url})"
INFRA_CASE_LINE_NO_TRANSCRIPT = "- {date}: {reason} · [run]({run_url})"
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
    passed, graded, infra, reasons, run_url, transcript_url}`` for a night
    that graded the case (module docstring: what is skipped)."""
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
                reasons = []
                for rep in classify.task_reps(task):
                    reason = classify.clean_reason(rep.get("reason"))[:REASON_MAX_CHARS]
                    if classify.rep_kind(rep) == classify.REP_FAIL and reason and reason not in reasons:
                        reasons.append(reason)
                by_date.setdefault(day, {})[name] = {
                    "date": day.isoformat(),
                    "started": run.get("started"),
                    "finished": run.get("finished"),
                    "build": run.get("build_id"),
                    "outcome": outcome,
                    "passed": counts["pass"],
                    "graded": counts["pass"] + counts["fail"],
                    "infra": counts["infra"] + counts["ceiling"],
                    "reasons": reasons[:REASONS_PER_NIGHT],
                    "run_url": nightly.build_url(run),
                    "transcript_url": nightly.transcript_url(run, name),
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


# --------------------------------------------------------------------------
# what the issue says


def quoted(text: str | None) -> str:
    """``text`` as inline code on one line (REASON_LINE says why), with
    ``<`` swapped for a look-alike so a reason can never carry a bot marker
    (``<!-- kube-agents-bot:... -->``) that dedupe would read as this
    issue's."""
    return "`" + (text or "").replace("\n", " ").replace("`", "'").replace("<", MARKER_SAFE_LT) + "`"


def night_block(streak: list[dict]) -> str:
    lines = []
    for v in reversed(streak):
        infra = f", {v['infra']} infra" if v["infra"] else ""
        template = NIGHT_LINE if v["run_url"] else NIGHT_LINE_NO_LINK
        lines.append(template.format(date=v["date"], passed=v["passed"], graded=v["graded"], infra=infra, run_url=v["run_url"], transcript_url=v["transcript_url"]))
        lines.extend(REASON_LINE.format(reason=quoted(reason)) for reason in v["reasons"])
        if not v["reasons"]:
            lines.append(NO_REASON_LINE)
    return "\n".join(lines)


def render_issue(case: str, domain: str, domain_row: dict, assignee: str | None, why: str, streak: list[dict]) -> tuple[str, str]:
    title = TITLE.format(case=case, nights=STREAK_NIGHTS)[:TITLE_MAX_CHARS]
    backup = domain_row.get("backup")
    body = BODY.format(
        marker=MARKER.format(case=case),
        case=case,
        nights=STREAK_NIGHTS,
        domain=domain,
        domain_name=domain_row.get("name") or domain,
        addressee=(ASSIGNED if assignee else UNASSIGNED).format(assignee=assignee, why=why),
        domain_owner=DOMAIN_OWNER_LINE.format(owner=domain_row["owner"], backup=f" (backup @{backup})" if backup else "") if domain_row.get("owner") else NO_DOMAIN_OWNER_LINE,
        nights_block=night_block(streak),
        nightly_url=NIGHTLY_URL,
    )
    return title, body[:ISSUE_BODY_MAX_CHARS]


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
    if markers or kind != KIND_RED:
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


# GitHub's state_reason for a close that hands the issue off rather than
# fixing it: "not planned" (or "duplicate") is what a triager picks when
# closing in favour of another issue, a `Fixes #N` merge sets "completed".
HANDED_OFF_REASONS = frozenset({"not_planned", "duplicate"})


def still_tracks(issue: dict, newest: dict, good: list[dict], horizon=None) -> bool:
    """Whether a closed issue still tracks a streak ending at ``newest``.
    One closed after that night finished has seen it (``closed_after``). One
    handed off (HANDED_OFF_REASONS) tracks the problem until a good night --
    a pass, a clean run, a graded case -- finishes after the close: the
    problem is the other issue's until it clears, and a new one after that
    files fresh. A "completed" close that did not fix it files again on the
    next bad night. A hand-off closed before ``horizon`` (the start of the
    window's first night) cannot be confirmed -- the record no longer shows
    whether the case cleared since -- so it does not hold a new streak back."""
    if closed_after(issue, newest):
        return True
    if issue.get("state_reason") not in HANDED_OFF_REASONS:
        return False
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
    names (main-b or writers), else main."""
    body = issue.get("body") or ""
    return next((part for job, part in nightly.PART_JOBS.items() if f"`{job}`" in body), nightly.PART_MAIN)


def listing_doubt(gh: ghcli.Gh, open_issues: list, closed_issues: list, since: str | None) -> str | None:
    """Why the REST listings cannot be trusted, or None when GraphQL
    agrees with them (LISTING_QUERY). A dedupe that reads an empty list
    files every case again."""
    owner, _, name = gh.repo.partition("/")
    variables = {"owner": owner, "name": name, "label": LABEL, "since": since, "closed": bool(since)}
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


def read_listings(gh: ghcli.Gh, since: str | None) -> tuple[list | None, list | None, str | None]:
    """``(open, closed, doubt)``: the two REST listings (closed ones updated
    since ``since``; none without it), read up to LISTING_ATTEMPTS times
    until GraphQL agrees with them (``listing_doubt``). ``open`` or
    ``closed`` is None when a read failed; ``doubt`` is why the last read is
    not trusted, None when it is. The filer and the digest's nightly section
    both read the issues through this."""
    doubt = None
    for attempt in range(1, LISTING_ATTEMPTS + 1):
        open_issues = gh.call("GET", gh.path(OPEN_ISSUES_PATH), paginate=True)
        closed_issues = gh.call("GET", gh.path(CLOSED_ISSUES_PATH.format(since=since)), paginate=True) if since else []
        if open_issues is None or closed_issues is None:
            return open_issues, closed_issues, None
        doubt = listing_doubt(gh, open_issues, closed_issues, since)
        if not doubt:
            break
        log(f"warning: the nightly-red issue listing is not trusted ({doubt}); read {attempt} of {LISTING_ATTEMPTS}")
        if attempt < LISTING_ATTEMPTS:
            time.sleep(LISTING_RETRY_DELAY_S)
    return open_issues, closed_issues, doubt


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
        if doubt:
            log("warning: no trusted nightly-red issue listing this pass; nothing filed or commented, retried next tick")
            return result
        active = set(nightly.expected_cases(data))
        for case, verdicts in sorted(nights.items()):
            fields = task_fields(case, self.repo_root)
            issue = tracking_issue(open_issues, case)
            streak = red_streak(verdicts)
            if streak:
                passes = [v for v in verdicts if v["outcome"] == classify.OUTCOME_PASSED]
                closed = tracking_issue([i for i in closed_issues if still_tracks(i, streak[-1], passes, self.horizon)], case)
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
                    filed = self.file(case, fields, streak)
                    result["filed" if filed["issue"] or self.gh.dry_run else "failed"].append(filed)
                continue
            recovered = recovery_night(verdicts, lambda v: opened_before(issue, v)) if issue else None
            if recovered and issue:
                done = self.recover(case, issue, recovered)
                if done:
                    result["recovered" if done["ok"] else "failed"].append(done)
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
                    self.note_night(KIND_INFRA, KEY_CASE, case, issue, verdicts[-1], quoted(verdicts[-1]["reason"]), result)
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
                title = INFRA_CASE_TITLE.format(case=case, nights=nightly_infra.INFRA_STREAK_NIGHTS)
                body = functools.partial(
                    INFRA_CASE_BODY.format,
                    marker=BOT_MARKER.format(kind=KIND_INFRA, field=KEY_CASE, key=case), case=case, nights=nightly_infra.INFRA_STREAK_NIGHTS,
                    job=job, domain=domain, domain_owner=self.infra_domain_owner(domain),
                    nights_block="\n".join((INFRA_CASE_LINE if v["transcript_url"] else INFRA_CASE_LINE_NO_TRANSCRIPT).format(
                        date=v["date"], reason=quoted(v["reason"]), run_url=v["run_url"], transcript_url=v["transcript_url"]) for v in reversed(streak)),
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
        this streak (``still_tracks``; ``good`` are the clean or graded
        nights). ``body(addressee=...)`` renders it with the assignment
        line."""
        closed = tracking_issue([i for i in closed_issues if still_tracks(i, newest, good, self.horizon)], key, kind)
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

    def recover(self, case: str, issue: dict, night: dict) -> dict | None:
        """Comment once on ``night``'s pass, when the night started after the
        issue was opened (a human may open one after the case recovered)."""
        if not opened_before(issue, night):
            return None
        marker = RECOVERY_MARKER.format(case=case, date=night["date"])
        body = RECOVERY_COMMENT.format(marker=marker, case=case, passed=night["passed"], graded=night["graded"], date=night["date"], run_url=night["run_url"])
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
        mention = f"; mention @{f['mention']}" if f["mention"] else ""
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
    result = Filer(gh, args.repo_root).run(data if isinstance(data, dict) else {})
    print(summary(result, args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
