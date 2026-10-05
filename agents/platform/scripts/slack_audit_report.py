"""Slack presentation for a fleet-audit report: a headline, the top findings, the ledger link.

Pure functions only, like :mod:`slack_presenter`. Its caller is
``session_kv_server.relay_cron_report``, which finds the ledger issue the
report ends with (:func:`ledger_ref`), fetches it, and posts
:func:`blocks_from_issue`, or :func:`headline_from_issue` as text where Slack
refuses the blocks, falling back to :func:`headline_fallback` when the fetch or
the parse fails. The caller must fetch only issues in repositories it manages:
:func:`ledger_ref` takes the repository from the report's own URL. An issue
not labelled ``agent:audit`` does not parse. A clean run closes its ledger
without rewriting the title, so a closed issue's counts are the last run's,
not this one's: a closed ledger is the clean card ("<Name>: clean. Ledger
closed."), unless the report's line counts a finding; a zero-finding run that could not read the
whole fleet, or did not account for a carried finding, leaves the ledger open
over the same old title, so a finding total in the report's line that
disagrees with the title also falls back, as does a line with no total that
counts nothing non-zero or calls the run clean, held or carried; a report that
is only the link keeps the title. The text goes out through
``hermes send``, and the Slack adapter converts standard markdown to mrkdwn on
the way, so the text here is markdown: ``**bold**`` and ``[label](url)``. That
conversion is also why the text does not call
:func:`slack_presenter.fallback_text`, whose output is mrkdwn already: its
``*headline*`` would reach Slack as italics. The rows are
:func:`slack_presenter.severity_row` in both, which reads the same either way.

The audit SOPs relay one line ending in ``— <issue_url>``. The counts and the
findings come from the issue itself, whose title is fleet-audit's
``issue_title`` (``[audit] <name> — <n> findings (<c> critical)``) and whose
body has one ``### <Severity> (<n>)`` section per severity, most severe first,
each finding a ``#### <title> <!-- finding:<id> -->`` heading, with a
``<!-- finding-new -->`` line under it when the run measured it as new since
the last; the collector's held rows under ``## Held by the collector`` are not
this run's findings, and a ``#`` line inside a finding's fenced evidence ends
no section.
Nothing in the report's own text can set the counts or pick the link, beyond
its last URL; its line can only withhold them.

The headline carries one number, the count of the most severe findings
present: "<Name>: 2 critical findings", the name as the title writes it.
Up to ``CRITICAL_ROWS_MAX`` critical findings are listed under it, or the top two of a lower
severity when there is none, a new one tagged "· _new_"; the body's summary
line ("7 findings: 2 critical, 1 major, 4 minor.") counts the rest by severity
("5 more (1 major, 4 minor) are in the ledger issue."). The clusters the
body's Scope section skipped get a line of their own ("Couldn't reach
seeded-c, so this run didn't check it." when every reason says it was
unreachable, "Didn't check seeded-c this run." otherwise), counted past three
names, or the line's own words for what it could not scan when the body has
no such table. Then a "Look at the first one" button and the ledger link. The
relayed line itself is not on the card, so it goes in the headline's thread
(:func:`needs_fold`), where its resolved count and remediation pull requests
stay readable. Finding titles are model-written and editable
on the forge, as is the issue title, and the relayed line is model-written
too, so a row, the audit name and the relayed line keep a link's text and drop
its target, and a ``<!channel>``-style token loses its brackets.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from slack_presenter import (
    BACKTICK,
    CLAUSE_ENDS,
    ELLIPSIS,
    HEADLINE_MAX,
    LIST_MARKER,
    MENTION,
    _clip,
    _plain,
    as_line,
    blocks_report,
    fallback_text,
    gap_parts,
    severity_row,
)

#: Any link Hermes' text path would post (``SlackAdapter.format_message``): its
#: URL grammar, spaces and one level of parentheses included. A label holds no
#: bracket, so a run of "[" fails at each one rather than scanning to the end,
#: and may be empty, so ``[a[](url)`` still matches at its last "[".
MD_LINK = re.compile(r"\[([^\[\]]*)\]\(([^()]*(?:\([^()]*\)[^()]*)*)\)")
#: A Slack link, ``<url|label>`` or ``<url>``, written straight into a title.
SLACK_LINK = re.compile(r"<([^<>|\s]+)(?:\|([^<>]*))?>")
#: What joins a link's label to its URL. Left in a row, it would meet a "[" or a
#: ")" on the row above or below, and Hermes would post the two as one link.
LINK_JOIN = "]("
LINK_JOIN_BROKEN = "] ("

#: Every count is at most nine digits, so ``int()`` never meets a digit run
#: past its limit; a longer run matches nothing.
ISSUE_URL = r"https://github\.com/(?P<repo>[\w.-]+/[\w.-]+)/issues/(?P<number>\d{1,9})"
#: The ledger link: the report's last URL, after the SOPs' dash or a "Ledger:"
#: label, bare, in angle brackets or as a markdown link; or a markdown link
#: whose own label names the ledger ("[Ledger issue #231](url)"), with nothing
#: before it. Labels are bounded, and the pattern only searches the report's
#: last ``LEDGER_TAIL_MAX`` characters, so a long report cannot make it backtrack.
TRAILING_LEDGER = re.compile(
    r"(?:(?:[—–]|\s-|\bledger(?:\s+issue)?:?)\s*(?:\[[^\]\n]{0,80}\]\()?|\[(?P<label>[^\]\n]{0,80})\]\()<?"
    + ISSUE_URL
    + r">?\)?[\s.]*\Z",
    re.IGNORECASE,
)
LEDGER_TAIL_MAX = 512
#: What a link label alone must say to mark the ledger.
LEDGER_WORD = re.compile(r"\bledger\b", re.IGNORECASE)
#: Repository path segments that are not a name.
DOT_SEGMENTS = frozenset({".", ".."})
#: fleet-audit's ``issue_title``, whole. The coverage-incomplete title does not
#: match, so a run that saw too little is never read as clean.
LEDGER_TITLE = re.compile(
    r"\A\[audit\]\s+(?P<name>.+?)\s+[—–-]+\s+(?P<count>\d{1,9})\s+findings?\s+\((?P<critical>\d{1,9})\s+critical\)\s*\Z"
)
SEVERITY_SECTION = re.compile(r"^###[ \t]+(?P<severity>Critical|Major|Minor)[ \t]+\(", re.MULTILINE)
#: What ends a severity section: any heading of level 3 or above, or the start
#: of fleet-audit's held section (``HELD_SECTION_BEGIN``), whose rows reuse the
#: finding heading.
ANY_SECTION = re.compile(r"^(?:#{1,3}[ \t]|<!--[ \t]*audit-held:begin)", re.MULTILINE)
#: fleet-audit's ``FINDING_MARKER_RE``, less its ``[ \t]+`` and ``[ \t]*`` either side
#: of the title, and with the title and id bounded: three runs that can all take the
#: same spaces made a long run of them cubic, and an unbounded title and id together
#: still made a line of repeated markers quadratic. fleet-audit caps a title at 300
#: characters and an id at 100; the title keeps its padding, so the reader strips it.
FINDING_HEADING = re.compile(
    r"^####[ \t](.{0,1000}?)<!--[ \t]*finding:[ \t]*(\S{1,100}?)[ \t]*-->[ \t]*$", re.MULTILINE
)
#: fleet-audit's ``NEW_MARKER``: the first line under a finding's heading, blank
#: lines aside, when the run measured the finding as new since the last.
NEW_MARKER = re.compile(r"\n(?:[ \t]*\n)*[ \t]*<!--[ \t]*finding-new[ \t]*-->[ \t]*$", re.MULTILINE)
NEW_TAG = " · _new_"
#: A title's own trailing "new" tag in any spelling (``·  _new_``, ``• *new*``, ``— _NEW_.``),
#: which only the marker may add; searched for in the last TAG_CLAIM_REACH characters only,
#: so a title of thousands of tags is stripped one at a time rather than rescanned from each.
TAG_CLAIM = re.compile(r"[\s·•—–-]+[_*]new[_*][\s.]*\Z", re.IGNORECASE)
TAG_CLAIM_REACH = 64
#: A code fence line, opening or closing: a finding's evidence command sits in one.
FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
#: A finding heading: a fence with no closer before it is one the model left open.
FENCE_HARD_BREAK = re.compile(r"^####[ \t].*<!--[ \t]*finding:")
#: Harness-written lines that end a fence the model left open, so one unbalanced
#: fence cannot swallow every finding after it: a finding heading, the exact
#: severity heading fleet-audit's renderer writes, or the held marker. Any other
#: ``### `` line is not one, since evidence output can start with it. A fence
#: closed before the next finding heading is evidence, and none of these ends it.
FENCE_BREAK = re.compile(
    r"^(?:####[ \t].*<!--[ \t]*finding:"
    r"|###[ \t]+(?:Critical|Major|Minor)[ \t]+\(\d+(?:[ \t]+of[ \t]+\d+)?\)[ \t]*$"
    r"|<!--[ \t]*audit-held:begin)"
)
#: fleet-audit's summary over the findings: the true totals, which the sections
#: show only as far as the body's budget let them.
FINDINGS_SUMMARY = re.compile(
    r"^\d{1,9}[ \t]+findings?:[ \t]+(?P<critical>\d{1,9})[ \t]+critical,[ \t]+(?P<major>\d{1,9})[ \t]+major,"
    r"[ \t]+(?P<minor>\d{1,9})[ \t]+minor\.[ \t]*$",
    re.MULTILINE,
)
#: The Scope section, its table of clusters the run could not audit, a row of
#: it (the cluster, then the rest of the row, its reason), and the row that
#: counts those past fleet-audit's row cap.
SCOPE_SECTION = re.compile(r"^##[ \t]+Scope[ \t]*$", re.MULTILINE)
SECTION_END = re.compile(r"^##[ \t]", re.MULTILINE)
SKIPPED_SECTION = re.compile(r"^###[ \t]+Skipped[ \t]*$", re.MULTILINE)
SKIPPED_ROW = re.compile(r"^\|[ \t]*`([^`\n]{1,253})`[ \t]*\|([^\n]{0,2000})", re.MULTILINE)
#: A skip reason saying the cluster could not be reached, rather than skipped for any other reason.
UNREACHABLE_REASON = re.compile(
    r"\b(?:unreachable|connection\s+refused|dial\s+tcp|no\s+route\s+to\s+host|i/o\s+timeout"
    r"|could(?:\s+not|n't)\s+(?:reach|connect))\b",
    re.IGNORECASE,
)
SKIPPED_MORE = re.compile(r"^\|[ \t]*_…and[ \t]+(\d{1,9})[ \t]+more_", re.MULTILINE)
#: fleet-audit's title for a ledger opened over a coverage gap with no findings.
COVERAGE_TITLE = re.compile(r"\A\[audit\]\s+(?P<name>.+?)\s+[—–-]+\s+coverage incomplete\b")
NEW_COUNT = re.compile(r"\b(?P<count>\d{1,9})\s+new\b", re.IGNORECASE)
#: A run total in the report's line ("0 findings", "no findings"); "3 new
#: findings" is a new count, not a total, and does not match.
FINDINGS_TOTAL = re.compile(r"\b(?P<count>\d{1,9}|no|zero)\s+findings?\b", re.IGNORECASE)
#: The other counts an SOP line states for a run that changed its ledger:
#: "<n> resolved", and a count by severity ("2 critical, 6 major").
CHANGE_COUNT = re.compile(r"\b(?P<count>\d{1,9})\s+(?:new|resolved|critical|major|minor)\b", re.IGNORECASE)
#: A count by severity alone ("2 critical"), which a clean run's line never states above 0.
SEVERITY_COUNT = re.compile(r"\b(?P<count>\d{1,9})\s+(?:critical|major|minor)\b", re.IGNORECASE)
#: A line describing a clean, held or carried run, which leaves the ledger open
#: over the last run's title even when it counts the carried findings by severity.
ZERO_FINDING_RUN = re.compile(r"\b(?:clean|held|carried|nothing\s+reproduced)\b", re.IGNORECASE)
#: A last line that is only a URL, the ledger wrapped onto a line of its own.
BARE_URL_LINE = re.compile(r"\A[<(\[]*https?://\S+\Z")
#: The line that carries a composed report's counts, when its last line is only the link.
COUNTS_DIGIT = re.compile(r"\d")
#: The coverage the line states: "across 3 clusters", "across 2 of 7 clusters".
COVERAGE = re.compile(r"(?<![\w.,-])across\s+(\d{1,9})(?:\s+of\s+(\d{1,9}))?\s+clusters?\b", re.IGNORECASE)
CLUSTERS_NOUN = ("cluster", "clusters")
#: "(1 unreachable)" after "across 3 clusters": a count whose noun is the coverage's.
BARE_GAP_COUNT = re.compile(r"(\d{1,9})\s+([a-z]+)", re.IGNORECASE)
GAP_SEPARATOR = ", "
#: What is left at the end of the fallback's line once the ledger URL is cut off.
LEDGER_SEPARATORS = " —–-:"

#: The issue fleet-audit keeps as its ledger: open, and labelled by the harness.
LEDGER_STATE = "open"
LEDGER_LABEL = "agent:audit"

#: The rows when nothing is critical: the top of the most severe level present.
TOP_FINDINGS = 2
#: The most critical rows a card lists; the rest are counted in its "more" line.
CRITICAL_ROWS_MAX = 10
#: A finding row: as long as the headline, shorter than a report row (``ROW_TEXT_MAX``).
FINDING_ROW_MAX = HEADLINE_MAX
#: The fallback's bold line when the issue could not be read; the SOPs' lines run to about 200 characters.
REPORT_LINE_MAX = 300
#: The most of a report line read for the headline. Reducing nested links is
#: quadratic, one pass per level, and a line holds what the model wrote; a finding
#: title is already capped by ``FINDING_HEADING``, and an issue title by GitHub.
LINE_READ_MAX = 1000
LEDGER_LINK = "[Ledger issue #{number} ↗]({url})"

COVERAGE_COUNT = "{count} {noun}"

#: The card's headline: one number, the count of its most severe findings.
CARD_HEADLINE = "{name}: {count} {severity} {noun}"
FINDINGS_NOUN = ("finding", "findings")
#: fleet-audit's severities, most severe first, as its sections and summary spell them.
SEVERITIES = ("critical", "major", "minor")
CRITICAL = "critical"
#: The findings the card does not list, by severity, most severe first; the ledger lists them.
MORE_LINE = "{count} more ({breakdown}) {verb} in the ledger issue."
MORE_VERB = ("is", "are")
BREAKDOWN_PART = "{count} {severity}"
BREAKDOWN_SEPARATOR = ", "
#: What this run did not check: the Scope section's skipped clusters, by name up to
#: ``NAMES_MAX`` and counted past it, else the line's own words. "Couldn't reach" only
#: when every skipped cluster's reason says it was unreachable.
GAP_MARK = "⚠️ "
UNREACHED_NAMED = GAP_MARK + "Couldn't reach {names}, so this run didn't check {them}."
UNREACHED_COUNT = GAP_MARK + "Couldn't reach {count} {noun}, so this run didn't check {them}."
UNCHECKED_NAMED = GAP_MARK + "Didn't check {names} this run."
UNCHECKED_COUNT = GAP_MARK + "Didn't check {count} {noun} this run."
THEM = ("it", "them")
NAMES_SEPARATOR = ", "
NAMES_LAST = " and "
NAMES_MAX = 3
#: The card for a run that closed its ledger.
CLEAN_CARD = "{name}: clean. Ledger closed."
CLOSED_STATE = "closed"

#: The Block Kit report: a choice button's click posts its label, the link
#: button opens the ledger.
ACTION_ID_PREFIX = "kage_audit"
LOOK_FIRST = "Look at the first one"
LEDGER_BUTTON = "Ledger issue #{number} ↗"


class LedgerRef(NamedTuple):
    url: str
    repo: str
    number: int


def _trailing_ledger(text: str) -> tuple[int, re.Match] | None:
    """Where the ledger link ``text`` ends with starts, and its match; None when it ends with none.

    Only the last ``LEDGER_TAIL_MAX`` characters before any trailing space are
    searched. A markdown link with nothing before it marks the ledger only when
    its label says "ledger".
    """
    offset = max(len(text.rstrip()) - LEDGER_TAIL_MAX, 0)
    tail = text[offset:]
    at = 0
    while (match := TRAILING_LEDGER.search(tail, at)) is not None:
        label = match.group("label")
        if label is None or LEDGER_WORD.search(label):
            return offset + match.start(), match
        at = match.start() + 1
    return None


def ledger_ref(report: str) -> LedgerRef | None:
    """The ledger issue a report ends with, or None when it does not end with one.

    The repository is whatever the report's URL names, so the caller must
    fetch the issue only from a repository it manages.
    """
    found = _trailing_ledger(report)
    if found is None:
        return None
    match = found[1]
    repo, number = match.group("repo"), match.group("number")
    if any(segment in DOT_SEGMENTS for segment in repo.split("/")):
        return None
    return LedgerRef(f"https://github.com/{repo}/issues/{number}", repo, int(number))


def has_more(report: str) -> bool:
    """Whether the report says more than its one line, so it is worth posting in the thread."""
    return len([line for line in report.splitlines() if line.strip()]) > 1


def needs_fold(report: str, headline: str) -> bool:
    """Whether the full report goes into the headline's thread: it says more than
    one line, or the headline lost part of its line (a link target, a clipped
    tail, or all of it, as :func:`headline_from_issue` does).

    The headline shows the line as plain text, so the line is compared plain:
    emphasis or a code span the headline dropped is not a loss.
    """
    if has_more(report):
        return True
    line = report.strip()
    found = _trailing_ledger(line)
    if found:
        line = line[: found[0]]
    if MD_LINK.search(line) or SLACK_LINK.search(line):
        return True
    return _plain(line).rstrip(LEDGER_SEPARATORS).strip() not in headline


def _new_count(line: str) -> int | None:
    """The one "<n> new" count in the report's ledger line: 0 when there is none,
    None when there are several (a count by severity), which no single number states."""
    counts = NEW_COUNT.findall(line)
    if len(counts) > 1:
        return None
    return int(counts[0]) if counts else 0


def _states_a_change(line: str) -> bool:
    """Whether the report's ledger line counts anything this run found or resolved.

    A zero-finding run that leaves the ledger open states only zeros, or no
    count at all, however it words the rest.
    """
    return any(int(count) for count in CHANGE_COUNT.findall(line))


def _findings_total(line: str) -> int | None:
    """The run's finding total as the ledger line states it, or None when it states none."""
    match = FINDINGS_TOTAL.search(line)
    if not match:
        return None
    count = match.group("count")
    return int(count) if count.isdigit() else 0


def _balanced_clip(text: str, limit: int) -> str:
    """:func:`slack_presenter._clip` that never leaves a code span open."""
    if len(text) <= limit:
        return text
    clipped = _clip(text, limit - len(BACKTICK))
    if clipped.count(BACKTICK) % 2 == 0:
        return clipped
    if clipped.endswith(ELLIPSIS):
        return clipped[: -len(ELLIPSIS)] + BACKTICK + ELLIPSIS
    return clipped + BACKTICK


def _row_text(title: str) -> str:
    """A finding title with any link, or a ``<!channel>``-style token, reduced to its text.

    Repeated until nothing changes, because reducing a nested link such as
    ``[[y](@U2)](!channel)`` once leaves an outer link that is still whole.
    A "](" still left is spaced apart (:data:`LINK_JOIN`).
    """
    text = None
    while text != title:
        text = title
        title = SLACK_LINK.sub(lambda m: m.group(2) or m.group(1), MD_LINK.sub(r"\1", text))
        title = MENTION.sub(lambda m: m.group(0)[1:-1], title)
    return title.replace(LINK_JOIN, LINK_JOIN_BROKEN)


def _closes(line: str, fence: str) -> bool:
    closer = FENCE.match(line)
    return bool(closer) and closer.group(1)[0] == fence[0] and len(closer.group(1)) >= len(fence)


def _fence_end(lines: list[str], start: int, fence: str) -> int:
    """The index of the first line after the fence opened just before ``start``.

    A fence closed before the next finding heading ends past its closer, and
    nothing inside it ends it sooner, so evidence cannot relabel a finding.
    One left open ends at the first ``FENCE_BREAK`` line, which is kept.
    """
    for at in range(start, len(lines)):
        if FENCE_HARD_BREAK.match(lines[at]):
            break
        if _closes(lines[at], fence):
            return at + 1
    for at in range(start, len(lines)):
        if FENCE_BREAK.match(lines[at]):
            return at
    return len(lines)


def _without_fences(body: str) -> str:
    """The body with every fenced block's lines dropped, fences included."""
    lines = body.split("\n")
    kept, at = [], 0
    while at < len(lines):
        opener = FENCE.match(lines[at])
        if opener:
            at = _fence_end(lines, at + 1, opener.group(1))
            continue
        kept.append(lines[at])
        at += 1
    return "\n".join(kept)


class Finding(NamedTuple):
    severity: str
    title: str
    #: fleet-audit measured it as new since the last run.
    new: bool


def _severity_findings(body: str) -> list[Finding]:
    """Every finding under a severity section, in body order."""
    # fleet-audit's own reader does the same: a body edited in the browser is CRLF.
    body = _without_fences(body.replace("\r\n", "\n"))
    found = []
    for section in SEVERITY_SECTION.finditer(body):
        start = section.end()
        following = ANY_SECTION.search(body, start)
        text = body[start : following.start() if following else len(body)]
        severity = section.group("severity").lower()
        found += [
            Finding(severity, match.group(1).strip(), bool(NEW_MARKER.match(text, match.end())))
            for match in FINDING_HEADING.finditer(text)
        ]
    return found


def _label_names(labels: object) -> list[str]:
    """An issue's label names, from a list of names or of ``{"name": ...}`` dicts; none from anything else."""
    if not isinstance(labels, (list, tuple)):
        return []
    names = [label.get("name") if isinstance(label, dict) else label for label in labels]
    return [name for name in names if isinstance(name, str)]


class AuditReport(NamedTuple):
    name: str
    count: int
    #: The report's whole ledger line, read for gaps.
    line: str
    findings: list[Finding]
    #: Findings by severity: the summary's totals, never fewer than the sections list.
    totals: dict[str, int]
    #: The Scope section's skipped clusters as ``(name, reason)``, and how many more its row cap left out.
    skipped: tuple[list[tuple[str, str]], int]


class Card(NamedTuple):
    headline: str
    rows: list[dict]
    #: The line under the rows counting the rest, and the line naming what was not checked.
    more: str
    gap: str


def _parse_issue(issue: dict, report: str) -> AuditReport | None:
    """The ledger issue's counts and findings, or None when it is not this run's report.

    ``report`` is the relayed report: its whole ledger line is read for
    gaps and checked against the title. A closed issue, one without the
    ledger label, a zero-finding title and a body listing no finding do not
    parse: a clean run closes the ledger over its old title
    (:func:`_clean_name`), and the fallback posts the report's own line.
    Nor does one whose title disagrees with the line's own finding total, or
    one whose line states no total and no non-zero count, or calls the run
    clean, held or carried: a zero-finding partial or held run leaves the old
    ledger open, and only a run that found or resolved something rewrites its
    title, so a line is trusted only when it counts what this run changed
    (:func:`_states_a_change`) and does not describe a run that changed nothing,
    which may still count its carried findings by severity. A report that is
    only the link has no line to disagree with, and the title stands.
    """
    if str(issue.get("state") or "").lower() != LEDGER_STATE:
        return None
    if LEDGER_LABEL not in _label_names(issue.get("labels")):
        return None
    title = LEDGER_TITLE.match(str(issue.get("title") or "").strip())
    if not title:
        return None
    name = _row_text(title.group("name")).strip()
    count, critical = int(title.group("count")), int(title.group("critical"))
    line = _ledger_line(report)
    if (_new_count(line) or 0) > count:
        return None  # a stale or wrong ledger: the report has more new findings than it lists
    total = _findings_total(line)
    if total is not None and total != count:
        return None  # the ledger was not rewritten this run
    if line and total is None and (ZERO_FINDING_RUN.search(line) or not _states_a_change(line)):
        return None  # nothing in the line says this run rewrote the title
    if count == 0:
        return None
    raw = str(issue.get("body") or "")
    findings = _severity_findings(raw)
    if not findings:
        return None  # the card would count findings neither it nor the ledger shows
    body = _without_fences(raw.replace("\r\n", "\n"))
    return AuditReport(name, count, line, findings, _totals(body, findings, critical), _skipped(body))


def _totals(body: str, findings: list[Finding], critical: int) -> dict[str, int]:
    """Findings by severity: the summary's totals, else the sections'; the title's critical count.

    Never fewer than the sections list, the title's count included: a title edited on the
    forge must not leave the card counting fewer critical findings than it lists. The title
    counts only over a body that lists a critical, which fleet-audit renders first: otherwise
    the card would lead on criticals it has no row for.
    """
    summary = FINDINGS_SUMMARY.search(body)
    totals = {severity: int(summary.group(severity)) if summary else 0 for severity in SEVERITIES}
    for severity in SEVERITIES:
        totals[severity] = max(totals[severity], sum(1 for finding in findings if finding.severity == severity))
    listed_critical = sum(1 for finding in findings if finding.severity == CRITICAL)
    if listed_critical:
        totals[CRITICAL] = max(critical, listed_critical)
    return totals


def _skipped(body: str) -> tuple[list[tuple[str, str]], int]:
    """The Scope section's skipped clusters as ``(name, reason)``, and how many its row cap counted instead.

    Only the Scope section's: a finding's model-written text may hold a table of its own.
    """
    scope = SCOPE_SECTION.search(body)
    if not scope:
        return [], 0
    scope_end = SECTION_END.search(body, scope.end())
    body = body[: scope_end.start() if scope_end else len(body)]
    section = SKIPPED_SECTION.search(body, scope.end())
    if not section:
        return [], 0
    following = ANY_SECTION.search(body, section.end())
    text = body[section.end() : following.start() if following else len(body)]
    rows = [(_row_text(name).strip(), reason) for name, reason in SKIPPED_ROW.findall(text)]
    more = SKIPPED_MORE.search(text)
    return [row for row in rows if row[0]], int(more.group(1)) if more else 0


def _clean_name(issue: dict, report: str) -> str | None:
    """The audit's name when its ledger is closed, which only a clean run does, else None.

    None too when the report's line counts a finding: the link is then to a
    ledger this run did not close.
    """
    if str(issue.get("state") or "").lower() != CLOSED_STATE:
        return None
    if LEDGER_LABEL not in _label_names(issue.get("labels")):
        return None
    raw = str(issue.get("title") or "").strip()
    title = LEDGER_TITLE.match(raw) or COVERAGE_TITLE.match(raw)
    if not title:
        return None
    line = _ledger_line(report)
    if (_findings_total(line) or 0) or (_new_count(line) or 0) or any(int(n) for n in SEVERITY_COUNT.findall(line)):
        return None
    return _row_text(title.group("name")).strip()


def _cluster_gap(part: str, coverage: re.Match | None) -> str:
    """``part`` with a bare count ("1 unreachable") naming the coverage's clusters."""
    bare = BARE_GAP_COUNT.fullmatch(part.strip().rstrip(CLAUSE_ENDS))
    if not (bare and coverage):
        return part
    count = int(bare.group(1))
    return f"{COVERAGE_COUNT.format(count=count, noun=CLUSTERS_NOUN[count != 1])} {bare.group(2)}"


def _gap(line: str, coverage: re.Match | None) -> str:
    """The line's parts naming what was not scanned, as a line of their own, or "".

    A parenthetical is split like the line, so its gaps leave their neighbours
    (``3 new, 1 resolved``) behind: they are counts the card already gives or
    leaves out. Every gap it names is kept ("1 cluster skipped, 2 clusters
    unreachable"), in a parenthetical or in a clause of its own, once: case and
    spacing aside, a gap said twice is the same gap.
    """
    seen: set[str] = set()
    parts = []
    for part in (_cluster_gap(part, coverage) for part in gap_parts(_fallback_line(line))):
        key = " ".join(part.lower().split()).rstrip(".")
        if key not in seen:
            seen.add(key)
            parts.append(part)
    return as_line(GAP_SEPARATOR.join(parts)) if parts else ""


def _names(names: list[str]) -> str:
    """"a", "a and b", "a, b and c"."""
    if len(names) == 1:
        return names[0]
    return NAMES_SEPARATOR.join(names[:-1]) + NAMES_LAST + names[-1]


def _unreached(parsed: AuditReport) -> str:
    """The line naming what this run did not check, or "" when it checked everything it owed."""
    rows, more = parsed.skipped
    names = [name for name, _ in rows]
    # Rows past the cap give no reason, so they cannot say they were unreachable.
    unreachable = not more and all(UNREACHABLE_REASON.search(reason) for _, reason in rows)
    if names and not more and len(names) <= NAMES_MAX:
        named = UNREACHED_NAMED if unreachable else UNCHECKED_NAMED
        return named.format(names=_names(names), them=THEM[len(names) != 1])
    count = len(names) + more
    if count:
        counted = UNREACHED_COUNT if unreachable else UNCHECKED_COUNT
        return counted.format(count=count, noun=CLUSTERS_NOUN[count != 1], them=THEM[count != 1])
    gap = _gap(parsed.line, COVERAGE.search(parsed.line))
    return GAP_MARK + gap if gap else ""


def _untagged(title: str) -> str:
    """`title` without a trailing "new" tag of its own, which only the marker may add."""
    # The space ahead lets a title that is only a tag match a separator.
    text = " " + title.rstrip()
    while match := TAG_CLAIM.search(text, max(0, len(text) - TAG_CLAIM_REACH)):
        text = text[: match.start()]
    return text[1:].rstrip()


def _row(finding: Finding) -> dict:
    tag = NEW_TAG if finding.new else ""
    text = _untagged(_row_text(finding.title))
    return {"severity": finding.severity, "text": _balanced_clip(text, FINDING_ROW_MAX - len(tag)) + tag}


def _card(parsed: AuditReport) -> Card:
    """The card: its headline counts the most severe findings present, listing up to
    :data:`CRITICAL_ROWS_MAX` when they are critical and the top two otherwise; a line counts
    the rest by severity."""
    totals = parsed.totals
    # The most severe level the body lists leads, so a count no section backs cannot give a rowless card.
    listed_levels = {finding.severity for finding in parsed.findings}
    severity = next(
        (level for level in SEVERITIES if totals[level] and level in listed_levels),
        next(level for level in SEVERITIES if totals[level]),
    )
    listed = [finding for finding in parsed.findings if finding.severity == severity]
    shown = listed[: CRITICAL_ROWS_MAX if severity == CRITICAL else TOP_FINDINGS]
    count = totals[severity]
    headline = CARD_HEADLINE.format(name=parsed.name, count=count, severity=severity, noun=FINDINGS_NOUN[count != 1])
    left = {level: totals[level] - (len(shown) if level == severity else 0) for level in SEVERITIES}
    more_count = sum(left.values())
    more = ""
    if more_count:
        breakdown = BREAKDOWN_SEPARATOR.join(
            BREAKDOWN_PART.format(count=number, severity=level) for level, number in left.items() if number
        )
        more = MORE_LINE.format(count=more_count, breakdown=breakdown, verb=MORE_VERB[more_count != 1])
    return Card(headline, [_row(finding) for finding in shown], more, _unreached(parsed))


def _after_rows(card: Card) -> list[str]:
    return [line for line in (card.more, card.gap) if line]


def headline_from_issue(issue: dict, ref: LedgerRef, report: str = "") -> str | None:
    """The channel message built from the fetched ledger issue, or None when it does not
    parse (:func:`_parse_issue`) and is not a closed ledger (:func:`_clean_name`)."""
    link = LEDGER_LINK.format(number=ref.number, url=ref.url)
    clean = _clean_name(issue, report)
    if clean:
        return f"**{CLEAN_CARD.format(name=clean)}**\n{link}"
    parsed = _parse_issue(issue, report)
    if parsed is None:
        return None
    card = _card(parsed)
    rows = [severity_row(row["severity"], row["text"]) for row in card.rows]
    return "\n".join([f"**{card.headline}**", *rows, *_after_rows(card), link])


def blocks_from_issue(issue: dict, ref: LedgerRef, report: str = "") -> tuple[list[dict], str] | None:
    """The Block Kit report's ``(blocks, text)`` built from the fetched ledger issue, or None.

    None whenever :func:`headline_from_issue` is. ``text`` is the message's
    mrkdwn ``text`` field: the same lines, the buttons left out and the ledger
    link inline.
    """
    links = [(LEDGER_BUTTON.format(number=ref.number), ref.url)]
    clean = _clean_name(issue, report)
    if clean:
        headline = CLEAN_CARD.format(name=clean)
        blocks = blocks_report(headline, links=links, action_id_prefix=ACTION_ID_PREFIX)
        return blocks, fallback_text(headline, links=links)
    parsed = _parse_issue(issue, report)
    if parsed is None:
        return None
    card = _card(parsed)
    after = "\n".join(_after_rows(card))
    blocks = blocks_report(
        card.headline,
        rows=card.rows,
        after_rows=after,
        choices=[LOOK_FIRST] if card.rows else [],
        links=links,
        action_id_prefix=ACTION_ID_PREFIX,
    )
    return blocks, fallback_text(card.headline, rows=card.rows, links=links, after_rows=after)


def _fallback_line(line: str) -> str:
    """A report line as plain text without its ledger link, any link or mention reduced to its text."""
    found = _trailing_ledger(line)
    if found:
        line = line[: found[0]]
    return _row_text(_plain(line[:LINE_READ_MAX])).rstrip(LEDGER_SEPARATORS)


def _ledger_line(report: str) -> str:
    """The report's ledger line as plain text without its link, read to ``LINE_READ_MAX``.

    The last line, the SOPs' one line. When the last is only the link, labelled
    (whatever the label says) or bare, the last unindented line above it that
    is not a list item and reads as an audit line (its coverage, a findings
    total or a change count), else the last such line that carries any count:
    an orienting sentence may carry a date, a line after the audit line may
    list pull requests or the next run, and a finding row, or the evidence
    indented under it, is not the report's line. Empty when no line qualifies.
    """
    lines = [line for line in report.splitlines() if line.strip()]
    if not lines:
        return ""
    end = lines[-1].strip()
    last = "" if BARE_URL_LINE.match(end) or TRAILING_LEDGER.match(end) else _fallback_line(lines[-1])
    if last:
        return last
    counted = [
        line for line in lines[:-1]
        if COUNTS_DIGIT.search(line) and not line[:1].isspace() and not LIST_MARKER.match(line)
    ]
    shaped = [line for line in counted if COVERAGE.search(line) or FINDINGS_TOTAL.search(line) or CHANGE_COUNT.search(line)]
    return _fallback_line((shaped or counted)[-1]) if counted else ""


def headline_fallback(report: str, ref: LedgerRef) -> str | None:
    """The report's ledger line in bold with the ledger link, for when the issue could not be read.

    The ledger line is the report's last, the SOPs' one line; a sentence the
    relay turn put above it is not the headline. A last line that is only the
    link falls back to the audit line above it (:func:`_ledger_line` says
    which); with none, there is no headline and the report goes out unchanged.
    """
    head = _clip(_ledger_line(report), REPORT_LINE_MAX)
    if not head:
        return None
    return f"**{head}**\n{LEDGER_LINK.format(number=ref.number, url=ref.url)}"
