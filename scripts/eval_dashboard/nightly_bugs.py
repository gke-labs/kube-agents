#!/usr/bin/env python3
"""File a bug for a case the nightly fails on every run, three nights in a
row, and tell the bug when the case passes again.

The nightly runs every case against ``main`` once a night (``nightly.py``),
and nothing acted on its reds: on 2026-10-19, 17 cases had scored 0 passes
on at least two of the last seven nights and none had a tracking issue. This
is the nightly's counterpart of ``gate_issue.py``::

    a case failed every graded repetition on its last STREAK_NIGHTS graded nights
    and its task.yaml does not carry `expected_fail: true`
    and no OPEN issue labelled `nightly-red` already names it
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

**Infra.** Not routed here. ``classify.py`` attributes repetitions, not
nights or cases: an infrastructure repetition is excluded before a night is
called failing, so a failing night is by construction a graded one and none
can be attributed to infrastructure. A case that is never graded is a
different trigger and a follow-up (#3440); ``infra_owner`` is unused here.

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
    from eval_dashboard import classify, ghcli, nightly
    from eval_dashboard.post_health import NIGHTLY_URL
except ImportError:  # run as a script: scripts/eval_dashboard/nightly_bugs.py
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from eval_dashboard import classify, ghcli, nightly
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
# The top-level task.yaml keys read here, line-scanned like collect.task_domain.
TASK_KEY_RE = r"^{key}:\s*([^\s#]+)\s*(?:#.*)?$"
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
DOMAIN_LABEL = "domain:{slug}"
OPEN_ISSUES_PATH = f"issues?labels={LABEL}&state=open&per_page=100"
ISSUES_PATH = "issues"
COMMENTS_PATH = "issues/{number}/comments"
COMMENTS_READ_PATH = "issues/{number}/comments?per_page=100"
# The bot's issue carries this, so it re-finds its own whatever the title
# has been edited to; a human's issue is adopted when its title or body
# names the case as a whole word.
MARKER = "<!-- kube-agents-bot:nightly-red case={case} -->"
MARKER_RE = re.compile(r"<!-- kube-agents-bot:nightly-red case=([A-Za-z0-9._-]+) -->")
# One recovery comment per recovery: keyed by the night that recovered.
RECOVERY_MARKER = "<!-- kube-agents-bot:nightly-red-recovered case={case} build={build} -->"
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
REASON_LINE = "  - failed: `{reason}`"
NO_REASON_LINE = "  - (no reason recorded)"
WHY_CASE_OWNER = "the case's `owner:`"
WHY_DOMAIN_OWNER = "domain owner; the case's `owner:` is {owner}"
RECOVERY_COMMENT = """\
{marker}
`{case}` passed every graded repetition ({passed}/{graded}) on the {date} nightly: [run]({run_url}). The bot will not close this issue; the triager decides whether it is fixed.
"""


def log(message: str) -> None:
    print(message, file=sys.stderr)


# --------------------------------------------------------------------------
# repository facts: task.yaml, domains.yaml, OWNERS


def task_fields(case: str, repo_root: pathlib.Path) -> dict | None:
    """``{owner, domain, expected_fail}`` from the case's task.yaml, ``None``
    when the case has no task.yaml on this checkout (renamed or deleted)."""
    if not CASE_NAME_RE.fullmatch(case or ""):
        return None
    try:
        text = (repo_root / TASKS_DIR / case / TASK_FILE).read_text()
    except OSError:
        return None

    def field(key):
        found = re.search(TASK_KEY_RE.format(key=key), text, re.MULTILINE)
        return found.group(1).strip("'\"") if found else None

    return {"owner": field("owner"), "domain": field("domain"), "expected_fail": field("expected_fail") == "true"}


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
            out[row["slug"]] = {key: row.get(key) for key in ("name", "owner", "backup")}
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


def night_block(streak: list[dict]) -> str:
    lines = []
    for v in reversed(streak):
        infra = f", {v['infra']} infra" if v["infra"] else ""
        template = NIGHT_LINE if v["run_url"] else NIGHT_LINE_NO_LINK
        lines.append(template.format(date=v["date"], passed=v["passed"], graded=v["graded"], infra=infra, run_url=v["run_url"], transcript_url=v["transcript_url"]))
        lines.extend(REASON_LINE.format(reason=reason.replace("\n", " ").replace("`", "'")) for reason in v["reasons"])
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


def names_case(issue: dict, case: str) -> bool:
    body = issue.get("body") or ""
    if case in MARKER_RE.findall(body):
        return True
    pattern = NAME_IN_TEXT_RE.format(name=re.escape(case))
    return bool(re.search(pattern, f"{issue.get('title') or ''}\n{body}"))


def tracking_issue(open_issues: list[dict], case: str) -> dict | None:
    """The open `nightly-red` issue naming the case: the bot's own by
    marker first, else a human's naming it."""
    issues = [i for i in open_issues if isinstance(i, dict) and not i.get("pull_request") and i.get("number")]
    for issue in issues:
        if case in MARKER_RE.findall(issue.get("body") or ""):
            return issue
    return next((issue for issue in issues if names_case(issue, case)), None)


class Filer:
    def __init__(self, gh: ghcli.Gh, repo_root: pathlib.Path = REPO_ROOT):
        self.gh = gh
        self.repo_root = repo_root
        self.domains = load_domains(repo_root)
        self.owners = load_owners(repo_root)

    def open_issues(self) -> list[dict] | None:
        return self.gh.call("GET", self.gh.path(OPEN_ISSUES_PATH), paginate=True)

    def commented(self, number: int, marker: str) -> bool | None:
        comments = self.gh.call("GET", self.gh.path(COMMENTS_READ_PATH.format(number=number)), paginate=True)
        if comments is None:
            return None
        return any(marker in (c.get("body") or "") for c in comments if isinstance(c, dict))

    def run(self, data: dict) -> dict:
        """One pass: ``{filed, recovered, tracked, skipped}``, each a list of
        what was (or, dry, would be) done. Nothing is written when the open
        issues cannot be read: a dedupe that cannot see is not a dedupe."""
        result = {"filed": [], "recovered": [], "tracked": [], "skipped": []}
        open_issues = self.open_issues()
        if open_issues is None:
            log("warning: could not list the open nightly-red issues; nothing filed or commented this pass")
            return result
        active = {c.get("name") for c in data.get("cases") or [] if isinstance(c, dict) and c.get("nightly_active")}
        for case, verdicts in sorted(graded_nights(data).items()):
            fields = task_fields(case, self.repo_root)
            issue = tracking_issue(open_issues, case)
            streak = red_streak(verdicts)
            if streak:
                if fields is None or case not in active:
                    result["skipped"].append({"case": case, "why": "not on this checkout's nightly matrix"})
                elif fields["expected_fail"]:
                    result["skipped"].append({"case": case, "why": "expected_fail: true"})
                elif issue:
                    result["tracked"].append({"case": case, "issue": issue["number"]})
                else:
                    result["filed"].append(self.file(case, fields, streak))
                continue
            recovered = recovery_night(verdicts)
            if recovered and issue:
                done = self.recover(case, issue, recovered)
                if done:
                    result["recovered"].append(done)
        return result

    def file(self, case: str, fields: dict, streak: list[dict]) -> dict:
        domain = fields.get("domain") or UNKNOWN
        row = self.domains.get(domain) or {}
        assignee, why = resolve_assignee(fields.get("owner"), row.get("owner"), self.owners)
        title, body = render_issue(case, domain, row, assignee, why, streak)
        labels = [LABEL, DOMAIN_LABEL.format(slug=domain), TRIAGE_LABEL]
        payload = {"title": title, "body": body, "labels": labels}
        if assignee:
            payload["assignees"] = [assignee]
        created = self.gh.call("POST", self.gh.path(ISSUES_PATH), payload)
        if created is None and assignee and not self.gh.dry_run:
            # A login GitHub will not assign (not a collaborator) refuses the
            # whole issue; file it unassigned, with the @-mentions intact.
            log(f"warning: filing {case} assigned to {assignee} failed; retrying unassigned")
            created = self.gh.call("POST", self.gh.path(ISSUES_PATH), {key: value for key, value in payload.items() if key != "assignees"})
        number = created.get("number") if isinstance(created, dict) else None
        if number:
            log(f"nightly bug: filed #{number} for {case}")
        return {"case": case, "domain": domain, "assignee": assignee, "why": why, "mention": row.get("owner"),
                "labels": labels, "nights": [v["date"] for v in streak], "runs": [v["run_url"] for v in streak], "issue": number}

    def recover(self, case: str, issue: dict, night: dict) -> dict | None:
        marker = RECOVERY_MARKER.format(case=case, build=night["build"])
        already = self.commented(issue["number"], marker)
        if already is None or already:
            return None
        body = RECOVERY_COMMENT.format(marker=marker, case=case, passed=night["passed"], graded=night["graded"], date=night["date"], run_url=night["run_url"])
        self.gh.call("POST", self.gh.path(COMMENTS_PATH.format(number=issue["number"])), {"body": body})
        return {"case": case, "issue": issue["number"], "night": night["date"], "run": night["run_url"]}


# --------------------------------------------------------------------------
# entry point


def summary(result: dict, dry_run: bool) -> str:
    verb = "would file" if dry_run else "filed"
    commented = "would comment on" if dry_run else "commented on"
    head = (
        f"nightly bugs ({'dry run' if dry_run else 'live'}): {verb} {len(result['filed'])}, "
        f"{commented} {len(result['recovered'])} recovered, "
        f"{len(result['tracked'])} already tracked, {len(result['skipped'])} skipped"
    )
    lines = [head]
    for f in result["filed"]:
        issue = f" #{f['issue']}" if f["issue"] else ""
        lines.append(f"  file{issue}: {f['case']} [{', '.join(f['labels'])}] assign @{f['assignee']} ({f['why']}); mention @{f['mention']}; nights {', '.join(f['nights'])}")
        lines.extend(f"    {url}" for url in f["runs"] if url)
    lines += [f"  recovered: {r['case']} on #{r['issue']} ({r['night']}, {r['run']})" for r in result["recovered"]]
    lines += [f"  tracked: {t['case']} by #{t['issue']}" for t in result["tracked"]]
    lines += [f"  skipped: {s['case']} ({s['why']})" for s in result["skipped"]]
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
