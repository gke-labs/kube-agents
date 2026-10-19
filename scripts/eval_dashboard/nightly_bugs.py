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
    and none was closed after the streak's newest night
    -> file one, labelled `nightly-red`, `domain:<slug>` and `needs-triage`,
       assigned per the hybrid rule below

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

**Who gets it.** The case's ``owner:`` in its task.yaml when that is a person
listed in the root OWNERS file; otherwise -- ``owner: maintainers``, no
owner, or a login OWNERS does not list -- the domain's ``owner:`` in
``docs/designs/domains.yaml``. The domain owner is @-mentioned either way.

**Infra** (``nightly_infra.py`` has the rules). Two shapes file an issue
labelled `nightly-red`, `Infra` and `needs-triage` instead of a domain label,
assigned to ``infra_owner`` in domains.yaml with ``infra_backup`` mentioned:

    a part of the nightly (main, writers) lost JOB_LOST_MIN of its last
    JOB_LOST_WINDOW nights -- cut off before its verdict, or its pod lost
    -> one issue for the job; a comment on each later lost night, and one
       when a later night runs to its verdict

    a case graded nothing -- every repetition infrastructure, or no line
    for it -- on INFRA_STREAK_NIGHTS nights in a row where its part ran
    -> one issue for the case with the classifier's reason; a comment on
       each later such night, and one when it is graded again

The domain streak is untouched by either: an infrastructure repetition is
dropped before a night counts as failing.

All GitHub traffic goes through ``ghcli.Gh``. **Dry run is the default**:
reads happen, every write is printed instead of sent. ``--live`` sends them;
the workflow passes it only when the repository variable
``NIGHTLY_BUGS_LIVE`` is ``true`` (docs/ci-health.md, "Nightly bugs").
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import re
import sys

try:
    import yaml
except ImportError:  # the workflow installs pyyaml; main() says so if absent
    yaml = None

try:
    from eval_dashboard import classify, gate_issue, ghcli, nightly, nightly_infra
    from eval_dashboard.post_health import NIGHTLY_URL
except ImportError:  # run as a script: scripts/eval_dashboard/nightly_bugs.py
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from eval_dashboard import classify, gate_issue, ghcli, nightly, nightly_infra
    from eval_dashboard.post_health import NIGHTLY_URL

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TASKS_DIR = pathlib.Path("bench") / "tasks"
TASK_FILE = "task.yaml"
DOMAINS_FILE = pathlib.Path("docs") / "designs" / "domains.yaml"
OWNERS_FILE = pathlib.Path("OWNERS")
# The OWNERS lists a person is "real" in.
OWNERS_LISTS = ("approvers", "reviewers")
# task.yaml `owner:` values that name no person: the domain owner answers.
OWNER_PLACEHOLDERS = frozenset({"maintainers"})
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
# nightly closes its issue, and the streak on record is still red).
CLOSED_ISSUES_PATH = f"issues?labels={LABEL}&state=closed&since={{since}}&per_page=100"
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
# by the night's build.
RECOVERY_MARKER = "<!-- kube-agents-bot:nightly-red-recovered case={case} build={build} -->"
NIGHT_MARKER = "<!-- kube-agents-bot:{kind}-night {field}={key} build={build} -->"
INFRA_RECOVERY_MARKER = "<!-- kube-agents-bot:{kind}-recovered {field}={key} build={build} -->"
# A case name inside text, bounded by characters a case name cannot hold.
NAME_IN_TEXT_RE = r"(?<![A-Za-z0-9._-]){name}(?![A-Za-z0-9._-])"
# GitHub's limits: a longer title or body is refused and files nothing.
TITLE_MAX_CHARS = 256
ISSUE_BODY_MAX_CHARS = 65536
# Distinct failing-check reasons quoted per night, and their length
# (nightly.REASON_MAX_CHARS, the collector's own cap).
REASONS_PER_NIGHT = 3
REASON_MAX_CHARS = nightly.REASON_MAX_CHARS
UNKNOWN = "unknown"

TITLE = "Nightly: {case} fails every run, {nights} nights in a row"
BODY = """\
{marker}
`{case}` failed every graded repetition on its last {nights} graded nightly nights. Domain: **{domain_name}** (`{domain}`).

**Assigned to** @{assignee} ({why}). Domain owner: @{domain_owner}{backup}.

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
NO_REASON_LINE = "  - (no reason recorded)"
WHY_CASE_OWNER = "the case's `owner:`"
WHY_DOMAIN_OWNER = "domain owner; the case's `owner:` is {owner}"
RECOVERY_COMMENT = """\
{marker}
`{case}` passed every graded repetition ({passed}/{graded}) on the {date} nightly: [run]({run_url}). The bot will not close this issue; the triager decides whether it is fixed.
"""
INFRA_TRIAGE = """\
**What triage decides (within a week)**

- **Infrastructure** (pool projects, seeded fleet, Prow, quota): fix it, or link the infra issue that tracks it.
- **Not infrastructure** (a case or harness defect that only looks like it): hand it to the domain owner, or mark the case `expected_fail: true` with this issue linked.

Filed automatically by `scripts/eval_dashboard/nightly_bugs.py` (docs/ci-health.md, "Nightly bugs"). It comments here on each later night like these, and once when the job (or case) runs clean again; it never closes the issue.
"""
INFRA_CASE_TITLE = "Nightly infra: {case} kept from running {nights} nights in a row"
INFRA_CASE_BODY = """\
{marker}
`{case}` graded nothing on its last {nights} nights where its part of the nightly (`{job}`) ran to its verdict: every repetition was classified as infrastructure (`classify.py`), or the run recorded no line for the case. Domain: `{domain}` (owner @{domain_owner}).

{addressee}

**Nights** (newest first; a night the part did not run or lost, and a night infrastructure took half the part's cases, are skipped)

{nights_block}

Nightly report: {nightly_url}

{triage}"""
INFRA_JOB_TITLE = "Nightly infra: {job} lost {lost} of its last {nights} nights"
INFRA_JOB_BODY = """\
{marker}
The nightly's {part} part (`{job}`) lost {lost} of its last {nights} nights: Prow ended the build before the eval loop's verdict (the job deadline, an abort) or the pod went away, so the cases it had not reached were never graded.

{addressee}

**Nights** (newest first)

{nights_block}

Nightly report: {nightly_url}

{triage}"""
INFRA_NIGHT_LINE = "- {date}: {how} · [run]({run_url})"
INFRA_CASE_LINE = "- {date}: {reason} · [run]({run_url}) · [transcript]({transcript_url})"
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
JOB_RECOVERED = "The {part} part (`{job}`) ran to its verdict"
CASE_RECOVERED = "`{case}` was graded"
WHY_INFRA_OWNER = "infra owner"


def log(message: str) -> None:
    print(message, file=sys.stderr)


# --------------------------------------------------------------------------
# repository facts: task.yaml, domains.yaml, OWNERS


def task_fields(case: str, repo_root: pathlib.Path) -> dict | None:
    """``{owner, domain, expected_fail}`` from the case's task.yaml, ``None``
    when the case has no readable task.yaml on this checkout (renamed or
    deleted). ``expected_fail`` is a YAML boolean, as bench-gate reads it."""
    if not CASE_NAME_RE.fullmatch(case or ""):
        return None
    path = repo_root / TASKS_DIR / case / TASK_FILE
    if not path.is_file():
        return None
    doc = load_yaml(path)
    if not doc:
        return None

    def text(key):
        value = doc.get(key)
        return str(value) if value is not None else None

    return {"owner": text(TASK_OWNER_KEY), "domain": text(TASK_DOMAIN_KEY), "expected_fail": doc.get(TASK_EXPECTED_FAIL_KEY) is True}


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
    for row in load_yaml(repo_root / DOMAINS_FILE).get("domains") or []:
        if isinstance(row, dict) and isinstance(row.get("slug"), str):
            out[row["slug"]] = {key: row.get(key) for key in DOMAIN_ROW_KEYS}
    return out


def load_owners(repo_root: pathlib.Path) -> frozenset[str]:
    """Every login the root OWNERS lists, lowercased (GitHub logins are
    case-insensitive)."""
    doc = load_yaml(repo_root / OWNERS_FILE)
    return frozenset(str(login).lower() for key in OWNERS_LISTS for login in doc.get(key) or [])


def resolve_assignee(case_owner: str | None, domain_owner: str | None, owners: frozenset[str]) -> tuple[str | None, str]:
    """``(login, why)``: the case owner when OWNERS lists them, else the
    domain owner. ``maintainers`` and a missing owner name nobody."""
    if case_owner and case_owner not in OWNER_PLACEHOLDERS and case_owner.lower() in owners:
        return case_owner, WHY_CASE_OWNER
    shown = f"`{case_owner}`" if case_owner else "not set"
    if case_owner and case_owner not in OWNER_PLACEHOLDERS:
        shown += ", not in the root OWNERS file"
    return domain_owner, WHY_DOMAIN_OWNER.format(owner=shown)


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
                seen.add(name)
                counts = classify.rep_counts(task)
                outcome = classify.outcome_of(counts)
                if outcome not in GRADED_OUTCOMES:
                    continue
                reasons = []
                for rep in classify.task_reps(task):
                    reason = classify.clean_reason(rep.get("reason"))[:REASON_MAX_CHARS]
                    if classify.rep_kind(rep) == classify.REP_FAIL and reason and reason not in reasons:
                        reasons.append(reason)
                by_date.setdefault(day, {})[name] = {
                    "date": day.isoformat(),
                    "started": run.get("started"),
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


def recovery_night(verdicts: list[dict]) -> dict | None:
    """The first all-pass night after the case's last failing night, when
    its newest graded night passed; None otherwise, or when no failing night
    is in the window."""
    if not verdicts or verdicts[-1]["outcome"] != classify.OUTCOME_PASSED:
        return None
    last_fail = max((i for i, v in enumerate(verdicts) if v["outcome"] == classify.OUTCOME_FAILED), default=None)
    if last_fail is None:
        return None
    return next(v for v in verdicts[last_fail + 1:] if v["outcome"] == classify.OUTCOME_PASSED)


# --------------------------------------------------------------------------
# what the issue says


def quoted(text: str | None) -> str:
    """``text`` as inline code on one line (REASON_LINE says why)."""
    return "`" + (text or "").replace("\n", " ").replace("`", "'") + "`"


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


def render_issue(case: str, domain: str, domain_row: dict, assignee: str, why: str, streak: list[dict]) -> tuple[str, str]:
    title = TITLE.format(case=case, nights=STREAK_NIGHTS)[:TITLE_MAX_CHARS]
    backup = domain_row.get("backup")
    body = BODY.format(
        marker=MARKER.format(case=case),
        case=case,
        nights=STREAK_NIGHTS,
        domain=domain,
        domain_name=domain_row.get("name") or domain,
        assignee=assignee or UNKNOWN,
        why=why,
        domain_owner=domain_row.get("owner") or UNKNOWN,
        backup=f" (backup @{backup})" if backup else "",
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
    pattern = NAME_IN_TEXT_RE.format(name=re.escape(case))
    return bool(re.search(pattern, f"{issue.get('title') or ''}\n{body}"))


def tracking_issue(issues: list[dict], case: str, kind: str = KIND_RED) -> dict | None:
    """The `nightly-red` issue among ``issues`` for ``(kind, case)``: the
    bot's own by marker first, else a human's naming it."""
    issues = [i for i in issues if isinstance(i, dict) and not i.get("pull_request") and i.get("number")]
    for issue in issues:
        if (kind, case) in BOT_MARKER_RE.findall(issue.get("body") or ""):
            return issue
    return next((issue for issue in issues if names_case(issue, case, kind)), None)


def closed_after(issue: dict | None, night: dict) -> bool:
    """Whether a closed issue was closed after ``night``'s run started."""
    closed, started = nightly.parse_iso((issue or {}).get("closed_at")), nightly.parse_iso(night.get("started"))
    return bool(closed and started and closed > started)


class Filer:
    def __init__(self, gh: ghcli.Gh, repo_root: pathlib.Path = REPO_ROOT):
        self.gh = gh
        self.repo_root = repo_root
        self.domains = load_domains(repo_root)
        self.owners = load_owners(repo_root)
        self.infra_owner, self.infra_backup = gate_issue.infra_addressees(repo_root / DOMAINS_FILE)

    def open_issues(self) -> list[dict] | None:
        return self.gh.call("GET", self.gh.path(OPEN_ISSUES_PATH), paginate=True)

    def closed_issues(self, since: str) -> list[dict] | None:
        return self.gh.call("GET", self.gh.path(CLOSED_ISSUES_PATH.format(since=since)), paginate=True)

    def commented(self, number: int, marker: str) -> bool | None:
        comments = self.gh.call("GET", self.gh.path(COMMENTS_READ_PATH.format(number=number)), paginate=True)
        if comments is None:
            return None
        return any(marker in (c.get("body") or "") for c in comments if isinstance(c, dict))

    def run(self, data: dict) -> dict:
        """One pass: ``{filed, recovered, tracked, skipped, failed}``, each a
        list of what was (or, dry, would be) done. Nothing is written when
        the issues cannot be read: a dedupe that cannot see is not a dedupe."""
        result = {"filed": [], "recovered": [], "noted": [], "tracked": [], "skipped": [], "failed": []}
        nights = graded_nights(data)
        parts = nightly_infra.part_nights(data, LOOKBACK_NIGHTS)
        starts = [nightly.parse_iso(n.get("started")) for ns in parts.values() for n in ns]
        since = min((t for t in starts if t), default=None)
        open_issues = self.open_issues()
        closed_issues = self.closed_issues(since.strftime(GITHUB_TIME_FORMAT)) if since else []
        if open_issues is None or closed_issues is None:
            log("warning: could not list the nightly-red issues; nothing filed or commented this pass")
            return result
        active = {c.get("name") for c in data.get("cases") or [] if isinstance(c, dict) and c.get("nightly_active")}
        for case, verdicts in sorted(nights.items()):
            fields = task_fields(case, self.repo_root)
            issue = tracking_issue(open_issues, case)
            streak = red_streak(verdicts)
            if streak:
                closed = tracking_issue([i for i in closed_issues if closed_after(i, streak[-1])], case)
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
            recovered = recovery_night(verdicts)
            if recovered and issue:
                done = self.recover(case, issue, recovered)
                if done:
                    result["recovered" if done["ok"] or self.gh.dry_run else "failed"].append(done)
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
            clean = nightly_infra.first_clean_after_lost(part_list)
            if clean:
                self.recover_infra(KIND_LOST, KEY_JOB, job, issue, clean, JOB_RECOVERED.format(part=part, job=job), result)
        for lost in nightly_infra.lost_jobs(parts):
            job = lost["job"]
            if tracking_issue(open_issues, job, KIND_LOST):
                continue
            losses = [n for n in lost["nights"] if n["lost"]]
            title = INFRA_JOB_TITLE.format(job=job, lost=len(losses), nights=nightly_infra.JOB_LOST_WINDOW)
            body = INFRA_JOB_BODY.format(
                marker=BOT_MARKER.format(kind=KIND_LOST, field=KEY_JOB, key=job), part=lost["part"], job=job, lost=len(losses),
                nights=nightly_infra.JOB_LOST_WINDOW, addressee=self.addressee(),
                nights_block="\n".join(INFRA_NIGHT_LINE.format(date=n["date"], how=n["how"], run_url=n["run_url"]) for n in reversed(lost["nights"])),
                nightly_url=NIGHTLY_URL, triage=INFRA_TRIAGE)
            self.file_infra(KIND_LOST, job, losses[-1], title, body, lost["nights"], closed_issues, result)
        candidates = sorted(c for c in active if c)
        for case, verdicts in sorted(nightly_infra.case_verdicts(data, parts, candidates).items()):
            fields = task_fields(case, self.repo_root)
            if fields is None or fields["expected_fail"]:
                continue
            issue = tracking_issue(open_issues, case, KIND_INFRA)
            if issue:
                # Open: the newest eligible night, kept from running after the
                # issue opened, gets one comment; a graded one, one recovery.
                result["tracked"].append({"case": case, "issue": issue["number"]})
                if verdicts[-1]["state"] in nightly_infra.KEPT_STATES:
                    self.note_night(KIND_INFRA, KEY_CASE, case, issue, verdicts[-1], quoted(verdicts[-1]["reason"]), result)
                graded = nightly_infra.first_graded_after_kept(verdicts)
                if graded:
                    self.recover_infra(KIND_INFRA, KEY_CASE, case, issue, graded, CASE_RECOVERED.format(case=case), result)
                continue
            streak = nightly_infra.kept_streak(verdicts)
            if streak:
                domain = fields.get("domain") or UNKNOWN
                job = next((n["job"] for ns in parts.values() for n in ns if n["build"] == streak[-1]["build"]), nightly.DEFAULT_NIGHTLY_JOB)
                title = INFRA_CASE_TITLE.format(case=case, nights=nightly_infra.INFRA_STREAK_NIGHTS)
                body = INFRA_CASE_BODY.format(
                    marker=BOT_MARKER.format(kind=KIND_INFRA, field=KEY_CASE, key=case), case=case, nights=nightly_infra.INFRA_STREAK_NIGHTS,
                    job=job, domain=domain, domain_owner=(self.domains.get(domain) or {}).get("owner") or UNKNOWN,
                    addressee=self.addressee(),
                    nights_block="\n".join(INFRA_CASE_LINE.format(date=v["date"], reason=quoted(v["reason"]), run_url=v["run_url"], transcript_url=v["transcript_url"]) for v in reversed(streak)),
                    nightly_url=NIGHTLY_URL, triage=INFRA_TRIAGE)
                self.file_infra(KIND_INFRA, case, streak[-1], title, body, streak, closed_issues, result)

    def addressee(self) -> str:
        if not self.infra_owner:
            return INFRA_UNASSIGNED
        backup = INFRA_BACKUP_MENTION.format(backup=self.infra_backup) if self.infra_backup else ""
        return INFRA_ASSIGNED.format(owner=self.infra_owner, backup=backup)

    def note_night(self, kind, field, key, issue, night, what, result) -> None:
        """One comment on an open infra issue for ``night``, when it started
        after the issue was opened."""
        opened, started = nightly.parse_iso(issue.get("created_at")), nightly.parse_iso(night.get("started"))
        if not (opened and started and started > opened):
            return
        marker = NIGHT_MARKER.format(kind=kind, field=field, key=key, build=night["build"])
        comment = INFRA_NIGHT_COMMENT.format(marker=marker, date=night["date"], what=what, run_url=night["run_url"])
        done = self.comment_once(issue["number"], marker, comment)
        if done is not None:
            result["noted" if done or self.gh.dry_run else "failed"].append({"case": key, "issue": issue["number"], "night": night["date"], "run": night["run_url"]})

    def file_infra(self, kind, key, newest, title, body, nights, closed_issues, result) -> None:
        """File the infra issue for ``key`` unless one closed after
        ``newest`` already saw this streak."""
        closed = tracking_issue([i for i in closed_issues if closed_after(i, newest)], key, kind)
        if closed:
            result["tracked"].append({"case": key, "issue": closed["number"], "closed": True})
            return
        labels = [LABEL, INFRA_LABEL, TRIAGE_LABEL]
        payload = {"title": title[:TITLE_MAX_CHARS], "body": body[:ISSUE_BODY_MAX_CHARS], "labels": labels}
        if self.infra_owner:
            payload["assignees"] = [self.infra_owner]
        created = self.create(payload, key, kind)
        number = created.get("number") if isinstance(created, dict) else None
        entry = {"case": key, "assignee": self.infra_owner, "why": WHY_INFRA_OWNER, "mention": self.infra_backup, "labels": labels,
                 "nights": [n["date"] for n in nights], "runs": [n["run_url"] for n in nights], "issue": number}
        result["filed" if number or self.gh.dry_run else "failed"].append(entry)

    def recover_infra(self, kind, field, key, issue, night, what, result) -> None:
        marker = INFRA_RECOVERY_MARKER.format(kind=kind, field=field, key=key, build=night["build"])
        comment = INFRA_RECOVERY_COMMENT.format(marker=marker, what=what, date=night["date"], run_url=night["run_url"])
        done = self.comment_once(issue["number"], marker, comment)
        if done is not None:
            result["recovered" if done or self.gh.dry_run else "failed"].append({"case": key, "issue": issue["number"], "night": night["date"], "run": night["run_url"]})

    def comment_once(self, number: int, marker: str, body: str) -> bool | None:
        """Post ``body`` unless a comment already carries ``marker``: None
        when nothing was attempted (already there, or unreadable), else
        whether the post went through."""
        already = self.commented(number, marker)
        if already is None or already:
            return None
        return self.gh.call("POST", self.gh.path(COMMENTS_PATH.format(number=number)), {"body": body}) is not None

    def create(self, payload: dict, key: str, kind: str):
        """POST the issue; on a failure, adopt it if the call created it
        after all (a timeout, a 5xx after the write), else file it once more
        without the assignee (a login GitHub will not assign)."""
        created = self.gh.call("POST", self.gh.path(ISSUES_PATH), payload)
        if created is None and payload.get("assignees") and not self.gh.dry_run:
            landed = tracking_issue(self.open_issues() or [], key, kind)
            if landed:
                return landed
            log(f"warning: filing {key} assigned to {payload['assignees'][0]} failed; retrying unassigned")
            created = self.gh.call("POST", self.gh.path(ISSUES_PATH), {k: v for k, v in payload.items() if k != "assignees"})
        return created

    def file(self, case: str, fields: dict, streak: list[dict]) -> dict:
        domain = fields.get("domain") or UNKNOWN
        row = self.domains.get(domain) or {}
        assignee, why = resolve_assignee(fields.get("owner"), row.get("owner"), self.owners)
        title, body = render_issue(case, domain, row, assignee, why, streak)
        labels = [LABEL, DOMAIN_LABEL.format(slug=domain), TRIAGE_LABEL]
        payload = {"title": title, "body": body, "labels": labels}
        if assignee:
            payload["assignees"] = [assignee]
        created = self.create(payload, case, KIND_RED)
        number = created.get("number") if isinstance(created, dict) else None
        if number:
            log(f"nightly bug: filed #{number} for {case}")
        return {"case": case, "domain": domain, "assignee": assignee, "why": why, "mention": row.get("owner"),
                "labels": labels, "nights": [v["date"] for v in streak], "runs": [v["run_url"] for v in streak], "issue": number}

    def recover(self, case: str, issue: dict, night: dict) -> dict | None:
        marker = RECOVERY_MARKER.format(case=case, build=night["build"])
        body = RECOVERY_COMMENT.format(marker=marker, case=case, passed=night["passed"], graded=night["graded"], date=night["date"], run_url=night["run_url"])
        posted = self.comment_once(issue["number"], marker, body)
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
        lines.append(f"  file{issue}: {f['case']} [{', '.join(f['labels'])}] assign @{f['assignee']} ({f['why']}); mention @{f['mention']}; nights {', '.join(f['nights'])}")
        lines.extend(f"    {url}" for url in f["runs"] if url)
    lines += [f"  recovered: {r['case']} on #{r['issue']} ({r['night']}, {r['run']})" for r in result["recovered"]]
    lines += [f"  new night: {r['case']} on #{r['issue']} ({r['night']}, {r['run']})" for r in result["noted"]]
    lines += [f"  tracked: {t['case']} by #{t['issue']}{' (closed after its newest red night)' if t.get('closed') else ''}" for t in result["tracked"]]
    lines += [f"  skipped: {s['case']} ({s['why']})" for s in result["skipped"]]
    lines += [f"  failed: {f['case']} (the GitHub write was refused; retried next tick)" for f in result["failed"]]
    return "\n".join(lines)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", required=True, type=pathlib.Path, help="data.json the collector wrote")
    parser.add_argument("--repo", default=ghcli.DEFAULT_REPO)
    parser.add_argument("--repo-root", type=pathlib.Path, default=REPO_ROOT, help="checkout holding bench/tasks, domains.yaml and OWNERS")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true", default=True, help="print every write instead of sending it (the default)")
    mode.add_argument("--live", dest="dry_run", action="store_false", help="send the writes")
    return parser.parse_args(argv)


def main(argv=None, gh_runner=None) -> int:
    args = parse_args(argv)
    if yaml is None:
        log("ERROR: pyyaml is required to read domains.yaml and OWNERS")
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
