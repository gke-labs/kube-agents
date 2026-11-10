"""One-off: bring the bots' older public posts to the shape the current code
produces.

The repository is public. Until the bots stopped (gate_comment.py for the
smoke-gate PR comment, nightly_bugs.py, coverage_gaps.py and gate_issue.py
for the issues), their posts carried pass ratios, repetition counts,
failing-check reasons and, for the outage issue, the failing cases and the
evidence lines. This script finds every such post and plans one action for
it, from the post's own markers (gate_comment.MARKER, nightly_bugs's
BOT_MARKER and comment markers, coverage_gaps.MARKER, the outage title) and
nothing else:

    a smoke-gate PR comment            -> deleted: the verdict lives in the
                                          private bucket and the dashboard
    a nightly-red or kept-case issue   -> title and body rewritten to what
                                          nightly_bugs.py renders now: the
                                          marker, domain and assignment
                                          lines are kept, each night keeps
                                          its date with its run and
                                          transcript links in Console form
                                          (links.py), the counts and
                                          reasons go
    the bot's comments on those issues -> deleted: an edited comment keeps
                                          an edit history too, and the
                                          issue's nights list carries the
                                          dates and links
    a coverage-gap issue               -> the candidates table reduced to
                                          its one-column form; the rest kept
    an outage issue                    -> retitled, body rewritten to
                                          gate_issue.BODY with the marker
                                          carrying the case ids, the
                                          failing-cases list and the
                                          evidence gone
    a lost-pods, deadline, fixture- or -> evidence lines filtered as the
    pool-drift issue                      current bodies filter them
                                          (gate_issue.public_evidence)

A plan is a pure function of the GitHub payload (``plan_comment``,
``plan_issue``, ``plan_issue_comment``), each returning an ``Action``:
``delete``, ``edit`` with the new title and body, or ``skip`` with its
reason. A post already in the current shape plans as a skip, so a second
run finds nothing to do. ``apply`` prints every action; in ``--live`` mode
it also performs them, ``--pace`` seconds apart and at most ``--limit`` of
them, and stops after MAX_CONSECUTIVE_FAILURES writes in a row fail. Reads
and writes go through ``ghcli.Gh`` as the bots' own do (``GH_TOKEN``, ``gh
api``). Comments are deleted and issues edited; an edited issue keeps its
revision history on GitHub, which the script cannot clear, so it prints the
edited issues' URLs for that step in the web UI. Human posts and the review
bot's comments are out of scope.

Dry run is the default and sends no write. Run from the repository root::

    GH_TOKEN=$(gh auth token) python3 scripts/eval_dashboard/scrub_bot_posts.py
    GH_TOKEN=$(gh auth token) python3 scripts/eval_dashboard/scrub_bot_posts.py --live
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import pathlib
import re
import subprocess
import sys
import time

try:
    from eval_dashboard import coverage_gaps, gate_comment, gate_issue, ghcli, links, nightly, nightly_bugs
    from eval_dashboard.post_health import NIGHTLY_URL
except ImportError:  # run as a script: scripts/eval_dashboard/scrub_bot_posts.py
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from eval_dashboard import coverage_gaps, gate_comment, gate_issue, ghcli, links, nightly, nightly_bugs
    from eval_dashboard.post_health import NIGHTLY_URL

# The login the bots post under: the workflow token. (The GitHub App posts
# the review bot's comments, which are out of scope.)
BOT_LOGINS = frozenset({"github-actions[bot]"})
# The first smoke-gate comments date from September 2026; the repository-wide
# comment listing starts here unless --since says otherwise.
DEFAULT_SINCE = "2026-09-01T00:00:00Z"
REPO_ENV = "GITHUB_REPOSITORY"
# Live mode: the pause between two writes (GitHub's secondary limits allow
# about 80 content-creating writes a minute and 500 an hour; two seconds
# keeps a long run under both), and how many writes in a row may fail
# before the run stops: a token without write access, or a rate limit,
# fails every write the same way.
DEFAULT_PACE_S = 2.0
MAX_CONSECUTIVE_FAILURES = 3
# GitHub's largest page. The listings are read a page per call (``&page=N``)
# rather than with gh's --paginate: the repository's comments since
# September run to thousands, and one call over all of them outlasts
# ghcli.CALL_TIMEOUT_S.
PAGE_SIZE = 100
PAGE_PARAM = "&page={page}"
# REST paths, relative to the repository (ghcli.Gh.path prefixes it).
COMMENTS_SINCE_PATH = "issues/comments?since={since}&sort=created&direction=asc&per_page=" + str(PAGE_SIZE)
ISSUES_BY_LABEL_PATH = "issues?state=all&labels={label}&per_page=" + str(PAGE_SIZE)
ISSUE_COMMENTS_PATH = "issues/{number}/comments?per_page=" + str(PAGE_SIZE)
ISSUE_PATH = "issues/{number}"
COMMENT_PATH = "issues/comments/{comment_id}"
# The labels every bot issue in scope carries (nightly_bugs's issues, the
# coverage-gap and the gate's tracking issues).
ISSUE_LABELS = (nightly_bugs.LABEL, gate_issue.LABEL)

# Actions and kinds, as the plan and the summary name them.
DELETE = "delete"
EDIT = "edit"
SKIP = "skip"
FAILED = "failed"
KIND_GATE_COMMENT = "smoke-gate comment"
KIND_NIGHTLY_RED = "nightly-red issue"
KIND_NIGHTLY_INFRA = "kept-case issue"
KIND_NIGHTLY_LOST = "lost-nights issue"
KIND_NIGHTLY_COMMENT = "nightly issue comment"
KIND_COVERAGE = "coverage-gap issue"
KIND_OUTAGE = "outage issue"
KIND_INFRA_ISSUE = "infrastructure issue"
KIND_OTHER = "other"
# Skip reasons.
REASON_CURRENT = "already in the current shape"
REASON_NOT_BOT = "not a bot post"
REASON_NO_MARKER = "no bot marker"
REASON_UNPARSED = "could not read the old body: {what}"
REASON_PULL_REQUEST = "a pull request, not an issue"
REASON_BOT_COMMENT = "the bot's comment; the issue's nights list carries the dates and links"

# What the old bodies look like, from the templates before the change
# (git show 9846f05c9:scripts/eval_dashboard/{nightly_bugs,coverage_gaps,gate_issue}.py).
# A night line in either shape: "- <date>: 0/3 passed · [run](u) · [transcript](t)"
# then, or "- <date> · [run](u) · [transcript](t)" now; the kept-case line
# quoted the reason between the date and the links. The links are optional.
NIGHT_LINE_RE = re.compile(r"^- (?P<date>\d{4}-\d{2}-\d{2})\b.*?(?: · \[run\]\((?P<run>[^)\s]+)\))?(?: · \[transcript\]\((?P<transcript>[^)\s]+)\))?\s*$")
RED_DOMAIN_RE = re.compile(r"Domain: \*\*(?P<name>.+?)\*\* \(`(?P<slug>[^`]+)`\)")
RED_ADDRESSEE_RE = re.compile(r"^(?P<addressee>\*\*(?:Assigned to|Unassigned)\*\* .*?\)\.)(?: (?P<owner>.*))?$", re.MULTILINE)
INFRA_JOB_RE = re.compile(r"\(`(?P<job>[^`]+)`\) ran to its verdict")
INFRA_DOMAIN_RE = re.compile(r"Domain: `(?P<slug>[^`]+)` \((?P<owner>.+)\)\.$", re.MULTILINE)
INFRA_ADDRESSEE_RE = re.compile(r"^\*\*(?:Assigned to|Unassigned:)\*\* .*$", re.MULTILINE)
# The bot's comments on a nightly issue, by marker (nightly_bugs's
# RECOVERY_MARKER, NIGHT_MARKER and INFRA_RECOVERY_MARKER kinds).
COMMENT_KINDS = (
    f"{nightly_bugs.KIND_RED}-recovered",
    f"{nightly_bugs.KIND_INFRA}-night",
    f"{nightly_bugs.KIND_LOST}-night",
    f"{nightly_bugs.KIND_INFRA}-recovered",
    f"{nightly_bugs.KIND_LOST}-recovered",
)
COMMENT_MARKER_RE = re.compile(
    r"<!-- kube-agents-bot:(?P<kind>" + "|".join(re.escape(kind) for kind in COMMENT_KINDS) + r")"
    r" (?P<field>case|job)=(?P<key>[A-Za-z0-9._-]+) night=(?P<date>\d{4}-\d{2}-\d{2}) -->"
)
# A Markdown link's URL, for relinking a body.
LINK_RE = re.compile(r"\]\((?P<url>https://[^)\s]+)\)")
# The coverage-gap table before the change (its head and the |---| line
# under it), a row's first cell, and the paragraph above it, which the
# current template's sentence replaces (its values are the template's).
OLD_COVERAGE_TABLE_HEAD = "| Case | Nightly record | Bar | Evidence store |"
TABLE_HEAD_LINES = 2
TABLE_ROW_START = "| "
TABLE_ROW_RE = re.compile(r"^\| (?P<case>[^|]+?) \|")
# The row kept: its first cell as posted (the case in backticks, with its
# held-out note), which is what coverage_gaps.TABLE_ROW renders now.
TABLE_ROW_KEPT = "| {cell} |"
CANDIDATES_PARAGRAPH_START = "**Candidates** ("
CANDIDATES_PARAGRAPH = next(line for line in coverage_gaps.BODY.split("\n") if line.startswith(CANDIDATES_PARAGRAPH_START)).format(
    nights=nightly_bugs.LOOKBACK_NIGHTS, scorecard_url=coverage_gaps.DOMAIN_SCORECARD_URL, bar_pct=round(coverage_gaps.BAR_RATE * 100), bar_runs=coverage_gaps.BAR_RUNS
)
# The outage issue before the change (gate_issue.TITLE then):
# "Smoke gate outage: 2 cases failing on every PR since Tue 7:30 AM ET".
OLD_OUTAGE_TITLE_RE = re.compile(r"^Smoke gate outage: \d+ cases? failing on every PR since (?P<since>.+)$")
OUTAGE_CASE_RE = re.compile(r"^- `(?P<case>[A-Za-z0-9._-]+)`$", re.MULTILINE)
OUTAGE_WINDOW_RE = re.compile(r"^\*\*Window:\*\* since (?P<since>.+?) \((?P<since_iso>[^)]+)\), (?P<prs>\d+) PRs red so far\.$", re.MULTILINE)
OUTAGE_CLASS_RE = re.compile(r"^\*\*Class:\*\* (?P<cause>.+?) \(`(?P<condition>[^`]+)`\)\.$", re.MULTILINE)
BRIEF_LINK_RE = re.compile(r"^Incident brief: (?P<url>\S+)$", re.MULTILINE)
BRIEF_CASES_RE = re.compile(r"&cases=[^&]*")
# The infrastructure issues' evidence block: "**Evidence:**", a blank line,
# then "- " lines up to the next blank line.
EVIDENCE_BLOCK_RE = re.compile(r"(?P<head>\*\*Evidence:\*\*\n\n)(?P<lines>(?:- .*\n)+)")
DRIFTED_ROLES_RE = re.compile(r"\*\*Drifted roles\*\*\n\n(?P<roles>(?:- `[^`]+`\n)+)")
ROLE_LINE_RE = re.compile(r"^- `(?P<role>[^`]+)`$", re.MULTILINE)
# Which infrastructure family an issue is, by the start of its title.
LOST_PODS_TITLE_START = "Build cluster lost "
DEADLINE_TITLE_START = "Smoke gate outage: "
FIXTURE_DRIFT_TITLE_START = "Seeded fleet drift: "
POOL_DRIFT_TITLE_START = "Pool drift: "
LINE_BREAK = "\n"
CRLF = "\r\n"
PATH_SEP = "/"
BULLET = "- "
SUMMARY_HEADING = "issues edited; clear each one's revision history by hand (the issue's 'edited' menu, Delete revision):"


@dataclasses.dataclass
class Action:
    verb: str
    kind: str
    path: str
    url: str
    reason: str = ""
    title: str | None = None
    body: str | None = None

    def is_write(self) -> bool:
        return self.verb in (DELETE, EDIT)


def text_of(payload: dict, key: str = "body") -> str:
    value = payload.get(key)
    return (value if isinstance(value, str) else "").replace(CRLF, LINE_BREAK)


def author_of(payload: dict) -> str:
    return ((payload.get("user") or {}).get("login")) or ""


def console_link(url: str | None) -> str | None:
    """A Spyglass link into a private bucket (links.PRIVATE_LOG_BUCKETS) as
    the Console link the pages and issues carry now; any other link as is."""
    if not isinstance(url, str) or not url.startswith(links.SPYGLASS_VIEW):
        return url
    rest = url[len(links.SPYGLASS_VIEW):]
    artifacts = PATH_SEP + nightly.TRANSCRIPT_ARTIFACT.split(PATH_SEP, 1)[0] + PATH_SEP
    if artifacts in rest:
        base, _, obj = rest.partition(artifacts)
        gcs_base = links.GS_SCHEME + base + PATH_SEP
        return links.console_object_url(gcs_base, artifacts.strip(PATH_SEP) + PATH_SEP + obj) if links.is_private(gcs_base) else url
    gcs_base = links.private_gcs_base(url)
    return links.console_url(gcs_base) if gcs_base else url


def skip(kind: str, payload: dict, reason: str, path: str = "") -> Action:
    return Action(SKIP, kind, path, payload.get("html_url") or "", reason)


def unchanged_or_edit(kind: str, payload: dict, path: str, title: str | None, body: str) -> Action:
    """An edit when the rendered text differs from what is posted, else a
    skip: this is what makes a second run find nothing."""
    same_title = title is None or title == text_of(payload, "title")
    if same_title and body == text_of(payload):
        return skip(kind, payload, REASON_CURRENT, path)
    return Action(EDIT, kind, path, payload.get("html_url") or "", title=title, body=body)


# --------------------------------------------------------------------------
# the smoke-gate PR comment


def plan_comment(comment: dict) -> Action:
    """A repository-wide issue comment: the smoke-gate comment is deleted."""
    path = COMMENT_PATH.format(comment_id=comment.get("id"))
    if gate_comment.MARKER not in text_of(comment):
        return skip(KIND_OTHER, comment, REASON_NO_MARKER, path)
    if author_of(comment) not in BOT_LOGINS:
        return skip(KIND_GATE_COMMENT, comment, REASON_NOT_BOT, path)
    return Action(DELETE, KIND_GATE_COMMENT, path, comment.get("html_url") or "")


# --------------------------------------------------------------------------
# the issues


def night_lines(body: str) -> list[dict]:
    """The nights an old or current body lists, oldest last as posted
    (newest first), each ``{date, run, transcript}`` with the links in
    Console form; a reason sub-line is not a night."""
    nights = []
    for line in body.split(LINE_BREAK):
        match = NIGHT_LINE_RE.match(line)
        if match:
            nights.append({"date": match["date"], "run": console_link(match["run"]), "transcript": console_link(match["transcript"])})
    return nights


def plan_nightly_red(issue: dict, case: str) -> Action:
    body = text_of(issue)
    path = ISSUE_PATH.format(number=issue["number"])
    domain = RED_DOMAIN_RE.search(body)
    addressee = RED_ADDRESSEE_RE.search(body)
    if not domain or not addressee:
        return skip(KIND_NIGHTLY_RED, issue, REASON_UNPARSED.format(what="domain or assignment line"), path)
    lines = []
    for night in night_lines(body):
        template = nightly_bugs.NIGHT_LINE if night["run"] else nightly_bugs.NIGHT_LINE_NO_LINK
        lines.append(template.format(date=night["date"], run_url=night["run"], transcript_url=night["transcript"]))
    new_body = nightly_bugs.BODY.format(
        marker=nightly_bugs.MARKER.format(case=case), case=case, domain=domain["slug"], domain_name=domain["name"],
        addressee=addressee["addressee"], domain_owner=addressee["owner"] or "", nights_block=LINE_BREAK.join(lines), nightly_url=NIGHTLY_URL,
    )
    return unchanged_or_edit(KIND_NIGHTLY_RED, issue, path, nightly_bugs.TITLE.format(case=case), new_body)


def plan_nightly_infra(issue: dict, case: str) -> Action:
    body = text_of(issue)
    path = ISSUE_PATH.format(number=issue["number"])
    job = INFRA_JOB_RE.search(body)
    domain = INFRA_DOMAIN_RE.search(body)
    addressee = INFRA_ADDRESSEE_RE.search(body)
    if not job or not domain or not addressee:
        return skip(KIND_NIGHTLY_INFRA, issue, REASON_UNPARSED.format(what="job, domain or assignment line"), path)
    lines = []
    for night in night_lines(body):
        template = nightly_bugs.INFRA_CASE_LINE if night["transcript"] else nightly_bugs.INFRA_CASE_LINE_NO_TRANSCRIPT
        lines.append(template.format(date=night["date"], run_url=night["run"], transcript_url=night["transcript"]))
    new_body = nightly_bugs.INFRA_CASE_BODY.format(
        marker=nightly_bugs.BOT_MARKER.format(kind=nightly_bugs.KIND_INFRA, field=nightly_bugs.KEY_CASE, key=case), case=case, job=job["job"],
        domain=domain["slug"], domain_owner=domain["owner"], addressee=addressee.group(0), nights_block=LINE_BREAK.join(lines),
        nightly_url=NIGHTLY_URL, triage=nightly_bugs.INFRA_TRIAGE,
    )
    return unchanged_or_edit(KIND_NIGHTLY_INFRA, issue, path, nightly_bugs.INFRA_CASE_TITLE.format(case=case), new_body)


def relink(body: str) -> str:
    """Every run and transcript link in ``body`` in Console form."""
    return LINK_RE.sub(lambda m: "](" + (console_link(m["url"]) or m["url"]) + ")", body)


def plan_coverage(issue: dict) -> Action:
    """The candidates paragraph replaced by the current template's sentence
    and the table reduced to its first column; the rest kept."""
    body = text_of(issue)
    path = ISSUE_PATH.format(number=issue["number"])
    lines = body.split(LINE_BREAK)
    out, i = [], 0
    while i < len(lines):
        if lines[i].startswith(CANDIDATES_PARAGRAPH_START):
            out.append(CANDIDATES_PARAGRAPH)
            i += 1
            continue
        if lines[i] == OLD_COVERAGE_TABLE_HEAD:
            out.extend(coverage_gaps.TABLE_HEAD.split(LINE_BREAK))
            i += TABLE_HEAD_LINES
            while i < len(lines) and lines[i].startswith(TABLE_ROW_START):
                cell = TABLE_ROW_RE.match(lines[i])
                out.append(TABLE_ROW_KEPT.format(cell=cell["case"]) if cell else lines[i])
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return unchanged_or_edit(KIND_COVERAGE, issue, path, None, LINE_BREAK.join(out))


def plan_outage(issue: dict, since_text: str) -> Action:
    body = text_of(issue)
    path = ISSUE_PATH.format(number=issue["number"])
    window = OUTAGE_WINDOW_RE.search(body)
    klass = OUTAGE_CLASS_RE.search(body)
    brief = BRIEF_LINK_RE.search(body)
    if not window or not klass or not brief:
        return skip(KIND_OUTAGE, issue, REASON_UNPARSED.format(what="window, class or brief line"), path)
    cases = sorted(set(OUTAGE_CASE_RE.findall(body)))
    doc = {"condition": klass["condition"], "cause": klass["cause"], "failing_cases": cases, "since": window["since_iso"], "incident": {"prs": [None] * int(window["prs"])}}
    new_body = gate_issue.render_body(doc, window["since"], BRIEF_CASES_RE.sub("", brief["url"]))
    return unchanged_or_edit(KIND_OUTAGE, issue, path, gate_issue.render_title(doc, since_text), new_body)


def plan_infrastructure(issue: dict) -> Action:
    """A lost-pods, deadline-kill, fixture-drift or pool-drift issue: its
    evidence block filtered as the current bodies filter it."""
    title, body = text_of(issue, "title"), text_of(issue)
    path = ISSUE_PATH.format(number=issue["number"])
    block = EVIDENCE_BLOCK_RE.search(body)
    if not block:
        return skip(KIND_INFRA_ISSUE, issue, REASON_CURRENT, path)
    lines = [line[len(BULLET):] for line in block["lines"].rstrip(LINE_BREAK).split(LINE_BREAK)]
    doc = {"evidence": lines}
    if title.startswith(LOST_PODS_TITLE_START):
        kept = gate_issue.public_evidence(doc, (gate_issue.LOST_PODS_EVIDENCE_PREFIX,))
    elif title.startswith(DEADLINE_TITLE_START):
        kept = gate_issue.public_evidence(doc, (gate_issue.DEADLINE_EVIDENCE_PREFIX,))
    elif title.startswith(FIXTURE_DRIFT_TITLE_START):
        roles = DRIFTED_ROLES_RE.search(body)
        prefixes = tuple(gate_issue.FIXTURE_DRIFT_EVIDENCE_PREFIX.format(role=role) for role in ROLE_LINE_RE.findall(roles["roles"])) if roles else ()
        kept = gate_issue.public_evidence(doc, prefixes)
    elif title.startswith(POOL_DRIFT_TITLE_START):
        marker = gate_issue.POOL_DRIFT_MARKER_RE.search(body)
        findings = marker.group(1).split(",") if marker else []
        # The pool body's own filter: the finding ids and the pool scan's wording.
        kept = gate_issue._pool_evidence(gate_issue.public_evidence(doc), findings)
    else:
        return skip(KIND_OTHER, issue, REASON_NO_MARKER, path)
    rendered = LINE_BREAK.join(f"- {line}" for line in kept) or gate_issue.NO_EVIDENCE
    new_body = body[: block.start("lines")] + rendered + LINE_BREAK + body[block.end("lines"):]
    return unchanged_or_edit(KIND_INFRA_ISSUE, issue, path, None, new_body)


def issue_kind(issue: dict) -> tuple[str, str | None]:
    """Which bot issue this is, and its key (the case, job or slug)."""
    body = text_of(issue)
    marker = nightly_bugs.BOT_MARKER_RE.search(body)
    if marker:
        return {nightly_bugs.KIND_RED: KIND_NIGHTLY_RED, nightly_bugs.KIND_INFRA: KIND_NIGHTLY_INFRA, nightly_bugs.KIND_LOST: KIND_NIGHTLY_LOST}[marker.group(1)], marker.group(2)
    if coverage_gaps.MARKER_RE.search(body):
        return KIND_COVERAGE, None
    title = text_of(issue, "title")
    if OLD_OUTAGE_TITLE_RE.match(title) or title.startswith(gate_issue.TITLE.format(since="")):
        return KIND_OUTAGE, None
    if title.startswith((LOST_PODS_TITLE_START, DEADLINE_TITLE_START, FIXTURE_DRIFT_TITLE_START, POOL_DRIFT_TITLE_START)):
        return KIND_INFRA_ISSUE, None
    return KIND_OTHER, None


def plan_issue(issue: dict) -> Action:
    path = ISSUE_PATH.format(number=issue.get("number"))
    if issue.get("pull_request"):
        return skip(KIND_OTHER, issue, REASON_PULL_REQUEST, path)
    kind, key = issue_kind(issue)
    if kind != KIND_OTHER and author_of(issue) not in BOT_LOGINS:
        # A human's issue carrying a copied marker or an infra-shaped title.
        return skip(kind, issue, REASON_NOT_BOT, path)
    if kind == KIND_NIGHTLY_RED:
        return plan_nightly_red(issue, key)
    if kind == KIND_NIGHTLY_INFRA:
        return plan_nightly_infra(issue, key)
    if kind == KIND_NIGHTLY_LOST:
        # The lost-nights body says how each night stopped, no grading;
        # only its run links may be Spyglass links into the private bucket.
        return unchanged_or_edit(KIND_NIGHTLY_LOST, issue, path, None, relink(text_of(issue)))
    if kind == KIND_COVERAGE:
        return plan_coverage(issue)
    if kind == KIND_OUTAGE:
        old = OLD_OUTAGE_TITLE_RE.match(text_of(issue, "title"))
        if not old:
            return skip(KIND_OUTAGE, issue, REASON_CURRENT, path)
        return plan_outage(issue, old["since"])
    if kind == KIND_INFRA_ISSUE:
        return plan_infrastructure(issue)
    return skip(KIND_OTHER, issue, REASON_NO_MARKER, path)


# --------------------------------------------------------------------------
# the bot's comments on a nightly issue


def plan_issue_comment(comment: dict) -> Action:
    """The bot's comment on a nightly issue (a recovery, a later kept or
    lost night, an infra recovery) is deleted: an edited comment would keep
    its history, and the issue's nights list carries the dates and links."""
    body = text_of(comment)
    path = COMMENT_PATH.format(comment_id=comment.get("id"))
    if not COMMENT_MARKER_RE.search(body):
        return skip(KIND_OTHER, comment, REASON_NO_MARKER, path)
    if author_of(comment) not in BOT_LOGINS:
        return skip(KIND_NIGHTLY_COMMENT, comment, REASON_NOT_BOT, path)
    return Action(DELETE, KIND_NIGHTLY_COMMENT, path, comment.get("html_url") or "", REASON_BOT_COMMENT)


# --------------------------------------------------------------------------
# reading and applying


def read_list(gh: ghcli.Gh, suffix: str) -> list[dict]:
    """Every object of a listing, a page per call; a page that fails to read
    ends the listing with a warning (ghcli logs it), so a plan is never made
    on a listing read short without saying so."""
    out: list[dict] = []
    page = 1
    while True:
        items = gh.call("GET", gh.path(suffix + PAGE_PARAM.format(page=page)))
        if not isinstance(items, list):
            ghcli.log(f"warning: {suffix}: page {page} not read; the listing stops here")
            break
        out.extend(item for item in items if isinstance(item, dict))
        if len(items) < PAGE_SIZE:
            break
        page += 1
    return out


def plan_all(gh: ghcli.Gh, since: str) -> list[Action]:
    actions = [plan_comment(c) for c in read_list(gh, COMMENTS_SINCE_PATH.format(since=since))]
    seen: set[int] = set()
    for label in ISSUE_LABELS:
        for issue in read_list(gh, ISSUES_BY_LABEL_PATH.format(label=label)):
            number = issue.get("number")
            if not isinstance(number, int) or number in seen:
                continue
            seen.add(number)
            action = plan_issue(issue)
            actions.append(action)
            if action.kind in (KIND_NIGHTLY_RED, KIND_NIGHTLY_INFRA, KIND_NIGHTLY_LOST):
                actions.extend(plan_issue_comment(c) for c in read_list(gh, ISSUE_COMMENTS_PATH.format(number=number)))
    return actions


def describe(action: Action, live: bool, show_bodies: bool) -> str:
    verb = action.verb if live or action.verb == SKIP else f"would {action.verb}"
    text = f"{verb} {action.kind} {action.url}" + (f": {action.reason}" if action.reason else "")
    if action.verb == EDIT and action.title is not None:
        text += f"\n  title: {action.title}"
    if action.verb == EDIT and show_bodies and action.body is not None:
        text += LINE_BREAK + LINE_BREAK.join("  | " + line for line in action.body.split(LINE_BREAK))
    return text


def apply(actions: list[Action], gh: ghcli.Gh, live: bool, pace_s: float = DEFAULT_PACE_S, limit: int | None = None, show_bodies: bool = False, sleep=time.sleep) -> dict:
    """Print every action; in live mode perform the writes, ``pace_s`` apart,
    at most ``limit`` of them, stopping after MAX_CONSECUTIVE_FAILURES in a
    row fail (ghcli logs each failure). Returns the counts per (kind, verb),
    a failed write under (kind, FAILED) rather than its verb."""
    counts: dict[tuple[str, str], int] = {}
    edited_issues: list[str] = []
    done = failed_in_a_row = 0
    for action in actions:
        if action.verb == SKIP and action.kind == KIND_OTHER:
            continue
        if not action.is_write() or not live:
            print(describe(action, live, show_bodies))
            counts[(action.kind, action.verb)] = counts.get((action.kind, action.verb), 0) + 1
            if action.verb == EDIT:
                edited_issues.append(action.url)
            continue
        # The limit is checked before the write is announced: a live line
        # with no "would" says the write happened, so it must.
        if limit is not None and done >= limit:
            print(f"limit {limit} reached; the rest is left for the next run")
            break
        print(describe(action, live, show_bodies))
        if done:
            sleep(pace_s)
        if action.verb == DELETE:
            ok = gh.call("DELETE", gh.path(action.path)) is not None
        else:
            payload = {"body": action.body} if action.title is None else {"title": action.title, "body": action.body}
            ok = gh.call("PATCH", gh.path(action.path), payload) is not None
        done += 1
        verb = action.verb if ok else FAILED
        counts[(action.kind, verb)] = counts.get((action.kind, verb), 0) + 1
        if ok and action.verb == EDIT:
            edited_issues.append(action.url)
        failed_in_a_row = 0 if ok else failed_in_a_row + 1
        if failed_in_a_row >= MAX_CONSECUTIVE_FAILURES:
            print(f"{failed_in_a_row} writes in a row failed (the token's access, or a rate limit); stopping, the rest is left for the next run")
            break
    for (kind, verb), count in sorted(counts.items()):
        print(f"{count:5d}  {verb:6s} {kind}")
    if edited_issues:
        print(SUMMARY_HEADING)
        for url in edited_issues:
            print(f"  {url}")
    return counts


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.split(LINE_BREAK, 1)[0])
    parser.add_argument("--repo", default=os.environ.get(REPO_ENV) or ghcli.DEFAULT_REPO)
    parser.add_argument("--since", default=DEFAULT_SINCE, help="list repository comments created after this ISO 8601 time")
    parser.add_argument("--pace", type=float, default=DEFAULT_PACE_S, help="seconds between two live writes (the default keeps a long run under GitHub's secondary limits, about 80 content writes a minute and 500 an hour)")
    parser.add_argument("--limit", type=int, default=None, help="at most this many live writes")
    parser.add_argument("--show-bodies", action="store_true", help="print each edit's new body")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="live", action="store_false", default=False, help="print every action instead of performing it (the default)")
    mode.add_argument("--live", dest="live", action="store_true", help="perform the actions")
    return parser.parse_args(argv)


def main(argv=None, gh_runner=None) -> int:
    args = parse_args(argv)
    gh = ghcli.Gh(args.repo, gh_runner or subprocess.run, dry_run=not args.live)
    apply(plan_all(gh, args.since), gh, args.live, pace_s=args.pace, limit=args.limit, show_bodies=args.show_bodies)
    return 0


if __name__ == "__main__":
    sys.exit(main())
