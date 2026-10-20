"""The nightly section of the 9 AM digest: what last night changed, and who
answers for it.

``post_health.py`` posts the digest; this builds its nightly part from the
same ``data.json`` and the same rules ``nightly_bugs.py`` files bugs by, so
the section and the bugs cannot disagree::

    🌙 Nightly, Mon Oct 20 · 87 of 99 scenarios passed · ran 9h 52m · run · Nightly page
    🔴 New bugs filed     red last night; its bug opened after the night ended,
                          or (none filed yet) its red streak reached STREAK_NIGHTS
    🟠 Still red, tracked red last night with an open `nightly-red` issue that
                          predates it, or past STREAK_NIGHTS with none
    🆕 Red, first time    red last night after a night that was not red; no issue
    ✅ Recovered          every repetition passed last night after red nights,
                          when the case had an issue or a streak that earned one
    ⚙️ Infra              a part of the nightly lost last night; a case kept
                          from running last night with an infra issue or streak
    Owners: reply in this thread ...

Each list holds at most LIST_MAX_LINES lines, the rest behind a link to the
Nightly page. A night with nothing new, first-time, recovered or infra is one
line (QUIET_LINE).

The verdicts are ``nightly_bugs.graded_nights``: a part cut off at the job
deadline, a case with no graded repetition, and a run-level event (nearly
every case failed) give no verdict. A night whose main part was cut off is
labelled so and none of its cases are counted; a writers part cut off is
named and its cases are not counted. Cases off the nightly matrix and cases
marked ``expected_fail`` are left out, as the filer leaves them.

Names, owners and backups come from ``docs/designs/domains.yaml`` and the
case's ``task.yaml`` (``scripts/case_owner.py``); the issue's assignee wins
over the computed owner. A GitHub login becomes a Chat mention
(``<users/ID>``) through the JSON object in MENTIONS_ENV, ``{login: id}``;
a login it does not hold is printed as plain text. The map is never in the
repository.

``build`` raises ``Skipped`` with the reason whenever the section cannot be
built truthfully -- no night to read, the issues unreadable, a count that
does not add up, a malformed link -- and the digest then posts its one-line
nightly summary with the reason instead.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import re
import sys

try:
    import yaml
except ImportError:  # build() says so; post_health stays importable without it
    yaml = None

try:
    from eval_dashboard import classify, nightly, nightly_bugs, nightly_infra
    from eval_dashboard.post_health import NIGHTLY_URL
except ImportError:  # imported with scripts/eval_dashboard/ on sys.path
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from eval_dashboard import classify, nightly, nightly_bugs, nightly_infra
    from eval_dashboard.post_health import NIGHTLY_URL

# The env var holding the GitHub-login -> Chat-user-id map (JSON object).
# The ids are internal: the workflow reads it from a secret of this name.
MENTIONS_ENV = "CI_HEALTH_CHAT_MENTIONS"
CHAT_USER_ID_RE = re.compile(r"(?:users/)?([0-9]+)")
CHAT_MENTION = "<users/{id}>"
PLAIN_LOGIN = "@{login}"
# Google Chat's link markup, and what a URL may hold to sit inside it: no
# whitespace, and none of the characters that end the markup.
CHAT_LINK = "<{url}|{text}>"
URL_RE = re.compile(r"https://[^\s<>|]+")
ISSUE_URL_RE = r"https://github\.com/{repo}/issues/[0-9]+"
LIST_MAX_LINES = 5
STREAK_NIGHTS = nightly_bugs.STREAK_NIGHTS
LOOKBACK_NIGHTS = nightly_bugs.LOOKBACK_NIGHTS
INFRA_STREAK_NIGHTS = nightly_infra.INFRA_STREAK_NIGHTS

HEADER = "{glyph} *Nightly, {date}*{sep}{body}{sep}ran {took}{sep}{run}{sep}{page}"
HEADER_PASSED = "{passed} of {graded} scenarios passed"
NOT_GRADED_NOTE = "{count} not graded (infrastructure or no line)"
PART_CUT_NOTE = "{part} cut off at the deadline, its cases not counted"
TRUNCATED_BODY = "cut off at the job deadline: truncated, not counted"
RUN_EVENT_BODY = "nearly every case failed: a run-level event, not counted per case"
UNKNOWN_WALL_CLOCK = "an unknown time"
RUN_TEXT = "run"
PAGE_TEXT = "Nightly page"
QUIET_LINE = "{glyph} Nightly {date}: {passed} of {graded} passed, no new reds, nothing recovered{still} · {page}"
QUIET_STILL = ", {count} still red"

NEW_HEADING = f"🔴 *New bugs filed* (failing every run, {STREAK_NIGHTS} nights in a row)"
UNFILED_HEADING = f"🔴 *Owed a bug, not filed yet* (failing every run, {STREAK_NIGHTS} or more nights in a row, no open issue)"
STILL_HEADING = "🟠 *Still red, already tracked*"
FIRST_HEADING = f"🆕 *Red last night, first time* (no bug yet; one is filed after {STREAK_NIGHTS} red nights in a row)"
RECOVERED_HEADING = "✅ *Recovered*"
INFRA_HEADING = "⚙️ *Infra*"
INFRA_NONE = "⚙️ Infra: none last night."
FOOTER = 'Owners: reply in this thread with "on it", "test bug" or "fixed in #NNNN".'

BULLET = "• "
NEW_LINE = "{domain}: {case} → {issue} · {assignee}{backup}"
UNFILED_LINE = "{domain}: {case}, night {nights} · {owner}{backup}"
BACKUP = " (backup {backup})"
STILL_LINE = "{domain}: {case}, night {nights} · {issue} · {owner}"
FIRST_LINE = "{domain}: {case} ({passed}/{graded}) · {owner}"
RECOVERED_LINE = "{domain}: {case}, {passed}/{graded} after {nights} red nights{issue}"
LOST_LINE = "{part} lost: {how} · {run}{issue}"
KEPT_LINE = "{domain}: {case} kept from running, night {nights}{issue}"
MORE_LINE = "…and {count} more: {page}"
# A count that fills the whole window: it may go back further.
WINDOW_FULL = "{count}+"
NO_OWNER = "no owner on record"
ISSUE_TAG = "#{number}"
# domains.yaml names carry a parenthetical gloss ("Capacity
# (stockout-prevention)"); a digest line shows the name before it.
NAME_GLOSS_RE = re.compile(r"\s*\(.*\)\s*$")

SKIP_NO_YAML = "pyyaml is not installed, so domains.yaml and task.yaml cannot be read"
SKIP_NO_ISSUES = "the nightly-red issues could not be read from GitHub"
SKIP_NO_GITHUB = "no GitHub token to read the nightly-red issues"
SKIP_NO_NIGHT = f"no nightly run started in the last {nightly.LAST_NIGHT_MAX_AGE // datetime.timedelta(hours=1)} hours"
SKIP_RUNNING = "last night's main part is still running"
SKIP_NO_MAIN = "last night's main part is missing, so its numbers would be the writers part's alone"
SKIP_BAD_LINK = "a link it would carry is malformed ({url})"
SKIP_BAD_COUNT = "its counts do not add up ({passed} passed of {graded} graded, {expected} on the matrix)"


class Skipped(Exception):
    """The section cannot be built truthfully; the message says why."""


def log(message: str) -> None:
    print(message, file=sys.stderr)


def load_mentions(raw: str | None) -> dict[str, str]:
    """``{login (lower case): numeric Chat user id}`` from MENTIONS_ENV's
    value. A value that is not a JSON object, and an entry whose id is not
    numeric, are dropped with a warning that names no id."""
    if not raw or not raw.strip():
        return {}
    try:
        loaded = json.loads(raw)
    except ValueError:
        log(f"warning: {MENTIONS_ENV} is not JSON; no Chat mentions this time")
        return {}
    if not isinstance(loaded, dict):
        log(f"warning: {MENTIONS_ENV} is not a JSON object; no Chat mentions this time")
        return {}
    out = {}
    for login, value in loaded.items():
        match = CHAT_USER_ID_RE.fullmatch(str(value).strip())
        if isinstance(login, str) and match:
            out[login.lower()] = match.group(1)
        else:
            log(f"warning: {MENTIONS_ENV}: the entry for {login!r} is not a numeric Chat user id; printed as plain text")
    return out


class Section:
    """What ``build`` returns: the lines, and whether it was a quiet day."""

    def __init__(self, lines: list[str], quiet: bool):
        self.lines = lines
        self.quiet = quiet


class Builder:
    def __init__(self, data: dict, now: datetime.datetime, open_issues: list[dict], repo_root: pathlib.Path,
                 mentions: dict[str, str], repo: str):
        self.data = data
        self.now = now
        self.open_issues = open_issues
        self.repo_root = repo_root
        self.mentions = mentions
        self.issue_url_re = re.compile(ISSUE_URL_RE.format(repo=re.escape(repo)))
        self.domains = nightly_bugs.load_domains(repo_root)
        self.domain_owners = {slug: row["owner"] for slug, row in self.domains.items() if row.get("owner")}
        self.data_domains = nightly.domains(data)
        self.links: list[str] = []

    # ---- words

    def who(self, login: str | None) -> str:
        if not login:
            return NO_OWNER
        found = self.mentions.get(login.lower())
        return CHAT_MENTION.format(id=found) if found else PLAIN_LOGIN.format(login=login)

    def link(self, url: str | None, text: str) -> str:
        """``url`` as Chat link markup; every URL is checked before the
        section is returned (``check_links``)."""
        self.links.append(url or "")
        return CHAT_LINK.format(url=url, text=text)

    def issue_link(self, issue: dict | None) -> str | None:
        if not issue:
            return None
        self.links.append(issue.get("html_url") or "")
        return CHAT_LINK.format(url=issue.get("html_url"), text=ISSUE_TAG.format(number=issue.get("number")))

    def page(self) -> str:
        return self.link(NIGHTLY_URL, PAGE_TEXT)

    def domain_of(self, case: str, fields: dict | None) -> tuple[str, str]:
        """``(slug, display name)``."""
        slug = (fields or {}).get("domain") or self.data_domains.get(case) or nightly.DOMAIN_UNKNOWN
        name = (self.domains.get(slug) or {}).get("name") or slug
        return slug, NAME_GLOSS_RE.sub("", name) or name

    def owner_of(self, case: str, fields: dict | None, issue: dict | None) -> str | None:
        """The issue's first assignee, else the owner the filer would assign."""
        assignees = [a.get("login") for a in (issue or {}).get("assignees") or [] if isinstance(a, dict) and a.get("login")]
        if assignees:
            return assignees[0]
        slug, _ = self.domain_of(case, fields)
        login, _ = nightly_bugs.resolve_assignee((fields or {}).get("owner"), slug, self.domain_owners)
        return login

    def capped(self, heading: str, items: list[tuple[tuple, str]]) -> list[str]:
        """The heading and up to LIST_MAX_LINES items (sorted by their key),
        the rest as one line linking to the Nightly page."""
        if not items:
            return []
        lines = [BULLET + text for _, text in sorted(items)]
        if len(lines) > LIST_MAX_LINES:
            rest = len(lines) - LIST_MAX_LINES
            lines = lines[:LIST_MAX_LINES] + [BULLET + MORE_LINE.format(count=rest, page=self.page())]
        return [heading, *lines]

    def check_links(self) -> None:
        for url in self.links:
            ok = bool(URL_RE.fullmatch(url))
            if ok and url.startswith("https://github.com/"):
                ok = bool(self.issue_url_re.fullmatch(url))
            if not ok:
                raise Skipped(SKIP_BAD_LINK.format(url=url or "empty"))

    # ---- the night

    def night(self) -> tuple[dict, datetime.date]:
        night, _ = nightly.last_night(self.data, self.now)
        if night is None:
            raise Skipped(SKIP_NO_NIGHT)
        if nightly.PART_MAIN in (night.get("running_parts") or []):
            raise Skipped(SKIP_RUNNING)
        if nightly.PART_MAIN in (night.get("missing_parts") or []):
            raise Skipped(SKIP_NO_MAIN)
        parts = night.get("parts") or []
        day = nightly.night_day(nightly.parse_iso(parts[0].get("started"))) if parts else None
        if day is None:
            raise Skipped(SKIP_NO_NIGHT)
        return night, day

    def run_event(self, day: datetime.date) -> bool:
        for each in nightly.group_nights(nightly.sorted_nightly_runs(self.data)):
            runs = nightly.night_parts(each)
            if nightly.night_date(runs[0]) == day:
                joined = {"tasks": [t for run in runs for t in classify.run_tasks(run)]}
                if classify.is_run_event(joined):
                    return True
        return False

    def build(self) -> Section:
        night, day = self.night()
        took = nightly.duration_text(night["duration_s"]) if night.get("duration_s") is not None else UNKNOWN_WALL_CLOCK
        date = f"{day:%a %b} {day.day}"
        short = f"{day:%b} {day.day}"
        run = self.link(night.get("log_url"), RUN_TEXT)
        verdicts = nightly_bugs.graded_nights(self.data)
        expected = set(nightly.expected_cases(self.data))
        tonight = {case: vs for case, vs in verdicts.items() if vs and vs[-1]["date"] == day.isoformat() and (not expected or case in expected)}
        fields = {case: nightly_bugs.task_fields(case, self.repo_root) for case in sorted(set(verdicts) | expected)}
        tonight = {case: vs for case, vs in tonight.items() if not (fields.get(case) or {}).get("expected_fail")}
        infra, lost_any = self.infra_items(day, expected, fields)

        def header(body: str) -> str:
            return HEADER.format(glyph=nightly.DIGEST_GLYPH, date=date, sep=nightly.SEP, body=body, took=took, run=run, page=self.page())

        if night.get("truncated") or self.run_event(day):
            body = TRUNCATED_BODY if night.get("truncated") else RUN_EVENT_BODY
            lines = [header(body), *(self.capped(INFRA_HEADING, infra) or [INFRA_NONE])]
            self.check_links()
            return Section(lines, quiet=False)

        graded = len(tonight)
        passed = sum(1 for vs in tonight.values() if vs[-1]["outcome"] == classify.OUTCOME_PASSED)
        on_matrix = len(expected) if expected else graded
        if passed > graded or graded > on_matrix:
            raise Skipped(SKIP_BAD_COUNT.format(passed=passed, graded=graded, expected=on_matrix))
        notes = [HEADER_PASSED.format(passed=passed, graded=graded)]
        ungraded = len({c for c in expected if not (fields.get(c) or {}).get("expected_fail")} - set(tonight)) if expected else 0
        if ungraded:
            notes.append(NOT_GRADED_NOTE.format(count=ungraded))
        notes += [PART_CUT_NOTE.format(part=nightly.PART_WORDS.get(p.get("part"), p.get("part"))) for p in night.get("parts") or [] if p.get("truncated")]

        new, unfiled, still, first, recovered = [], [], [], [], []
        newly_owed = False
        for case, vs in sorted(tonight.items()):
            v = vs[-1]
            f = fields.get(case)
            slug, name = self.domain_of(case, f)
            key = (name.lower(), case)
            issue = nightly_bugs.tracking_issue(self.open_issues, case)
            reds = trailing(vs, classify.OUTCOME_FAILED)
            if v["outcome"] == classify.OUTCOME_FAILED:
                count = WINDOW_FULL.format(count=reds) if reds == len(vs) and reds >= LOOKBACK_NIGHTS else str(reds)
                backup = (self.domains.get(slug) or {}).get("backup")
                backup_text = BACKUP.format(backup=self.who(backup)) if backup else ""
                if issue and not nightly_bugs.opened_before(issue, v):
                    new.append((key, NEW_LINE.format(domain=name, case=case, issue=self.issue_link(issue), assignee=self.who(self.owner_of(case, f, issue)), backup=backup_text)))
                elif issue:
                    still.append((key, STILL_LINE.format(domain=name, case=case, nights=count, issue=self.issue_link(issue), owner=self.who(self.owner_of(case, f, issue)))))
                elif reds >= STREAK_NIGHTS:
                    # The filer owes it a bug and has not filed one: it is
                    # in dry run, or the case's filing failed this far.
                    newly_owed = newly_owed or reds == STREAK_NIGHTS
                    unfiled.append((key, UNFILED_LINE.format(domain=name, case=case, nights=count, owner=self.who(self.owner_of(case, f, None)), backup=backup_text)))
                elif reds == 1:
                    first.append((key, FIRST_LINE.format(domain=name, case=case, passed=v["passed"], graded=v["graded"], owner=self.who(self.owner_of(case, f, None)))))
            elif v["outcome"] == classify.OUTCOME_PASSED and len(vs) > 1:
                before = trailing(vs[:-1], classify.OUTCOME_FAILED)
                if before and (issue or before >= STREAK_NIGHTS):
                    tag = self.issue_link(issue)
                    recovered.append((key, RECOVERED_LINE.format(domain=name, case=case, passed=v["passed"], graded=v["graded"], nights=before,
                                                                 issue=f"{nightly.SEP}{tag}" if tag else "")))

        if not (new or newly_owed or first or recovered or lost_any or infra):
            line = QUIET_LINE.format(glyph=nightly.DIGEST_GLYPH, date=short, passed=passed, graded=graded,
                                     still=QUIET_STILL.format(count=len(still) + len(unfiled)) if still or unfiled else "", page=self.page())
            self.check_links()
            return Section([line], quiet=True)
        lines = [header(nightly.SEP.join(notes))]
        for heading, items in ((NEW_HEADING, new), (UNFILED_HEADING, unfiled), (STILL_HEADING, still), (FIRST_HEADING, first), (RECOVERED_HEADING, recovered)):
            block = self.capped(heading, items)
            if block:
                lines += ["", *block]
        lines += ["", *(self.capped(INFRA_HEADING, infra) or [INFRA_NONE]), "", FOOTER]
        self.check_links()
        return Section(lines, quiet=False)

    def infra_items(self, day: datetime.date, expected: set, fields: dict) -> tuple[list[tuple[tuple, str]], bool]:
        """``([(sort key, line)], whether a part was lost last night)``:
        each part lost last night, and each case kept from running last
        night that has an open infra issue or a streak of
        INFRA_STREAK_NIGHTS (``nightly_infra``)."""
        items: list[tuple[tuple, str]] = []
        parts = nightly_infra.part_nights(self.data, LOOKBACK_NIGHTS)
        lost_any = False
        for part in nightly.PARTS:
            tail = parts.get(part) or []
            if not tail or tail[-1]["date"] != day.isoformat() or not tail[-1]["lost"]:
                continue
            lost_any = True
            issue = nightly_bugs.tracking_issue(self.open_issues, tail[-1]["job"], nightly_bugs.KIND_LOST)
            tag = self.issue_link(issue)
            items.append(((0, part), LOST_LINE.format(part=nightly.PART_WORDS[part].capitalize(), how=tail[-1]["how"],
                                                      run=self.link(tail[-1]["run_url"], RUN_TEXT), issue=f"{nightly.SEP}{tag}" if tag else "")))
        candidates = sorted(c for c in expected if not (fields.get(c) or {}).get("expected_fail"))
        tracked = {}
        for case in candidates:
            issue = nightly_bugs.tracking_issue(self.open_issues, case, nightly_bugs.KIND_INFRA)
            if issue:
                tracked[case] = nightly.PART_WRITERS if f"`{nightly.NIGHTLY_WRITERS_JOB}`" in (issue.get("body") or "") else nightly.PART_MAIN
        for case, vs in sorted(nightly_infra.case_verdicts(parts, candidates, tracked).items()):
            if not vs or vs[-1]["date"] != day.isoformat() or vs[-1]["state"] not in nightly_infra.KEPT_STATES:
                continue
            issue = nightly_bugs.tracking_issue(self.open_issues, case, nightly_bugs.KIND_INFRA)
            kept = sum(1 for _ in _takewhile_back(vs, lambda v: v["state"] in nightly_infra.KEPT_STATES))
            if not issue and kept < INFRA_STREAK_NIGHTS:
                continue
            _, name = self.domain_of(case, fields.get(case))
            tag = self.issue_link(issue)
            items.append(((1, name.lower(), case), KEPT_LINE.format(domain=name, case=case, nights=kept, issue=f"{nightly.SEP}{tag}" if tag else "")))
        return items, lost_any


def _takewhile_back(items: list, keep):
    for item in reversed(items):
        if not keep(item):
            return
        yield item


def trailing(verdicts: list[dict], outcome: str) -> int:
    """How many of the newest ``verdicts`` in a row have ``outcome``."""
    return sum(1 for _ in _takewhile_back(verdicts, lambda v: v["outcome"] == outcome))


def build(data: dict, now: datetime.datetime, gh, repo_root: pathlib.Path | None = None, mentions: dict[str, str] | None = None) -> Section:
    """The section, or ``Skipped``. ``gh`` (``ghcli.Gh``, or None without a
    token) reads the open `nightly-red` issues; ``repo_root`` (default
    ``nightly_bugs.REPO_ROOT``) holds bench/tasks and domains.yaml."""
    if yaml is None:
        raise Skipped(SKIP_NO_YAML)
    if gh is None:
        raise Skipped(SKIP_NO_GITHUB)
    open_issues = gh.call("GET", gh.path(nightly_bugs.OPEN_ISSUES_PATH), paginate=True)
    if open_issues is None:
        raise Skipped(SKIP_NO_ISSUES)
    return Builder(data, now, open_issues, repo_root or nightly_bugs.REPO_ROOT, mentions or {}, gh.repo).build()
