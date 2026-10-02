"""Slack presentation for a fleet-audit report: a headline, the top findings, the ledger link.

Pure functions only, like :mod:`slack_presenter`. Its caller is
``session_kv_server.relay_cron_report``, which finds the ledger issue the
report ends with (:func:`ledger_ref`), fetches it, and posts
:func:`blocks_from_issue`, or :func:`headline_from_issue` as text where Slack
refuses the blocks, falling back to :func:`headline_fallback` when the fetch or
the parse fails. The caller must fetch only issues in repositories it manages:
:func:`ledger_ref` takes the repository from the report's own URL. A closed
issue, or one not labelled ``agent:audit``, does not parse. A clean
run closes its ledger without rewriting the title, so a closed issue's counts
are the last run's, not this one's; a zero-finding run that could not read the
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
each finding a ``#### <title> <!-- finding:<id> -->`` heading; the collector's
held rows under ``## Held by the collector`` are not this run's findings, and
a ``#`` line inside a finding's fenced evidence ends no section.
Nothing in the report's own text can set the counts or pick the link, beyond
its last URL; its line can only withhold them.

The card carries one number, the title's finding count: "<Name> found 7
things to look at across 3 clusters.", the coverage taken from the line's
"across <n> clusters" when it has one. Where the line names something it
could not scan, that goes on a line of its own under the headline ("1 cluster
unreachable.") and the coverage is dropped unless it reads "<n> of <m>",
since "across 3 clusters" would vouch for all three. The line's single "<n>
new" count follows it ("2 are new since the last run."), then the top two
findings and a "Fix the first one" button, a "See all N" button when the
issue counts more, and the ledger link. The relayed line itself is not on the card, so it goes in the
headline's thread (:func:`needs_fold`), where its resolved count and
remediation pull requests stay readable. Finding titles are model-written and editable
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
NEW_COUNT = re.compile(r"\b(?P<count>\d{1,9})\s+new\b", re.IGNORECASE)
#: A run total in the report's line ("0 findings", "no findings"); "3 new
#: findings" is a new count, not a total, and does not match.
FINDINGS_TOTAL = re.compile(r"\b(?P<count>\d{1,9}|no|zero)\s+findings?\b", re.IGNORECASE)
#: The other counts an SOP line states for a run that changed its ledger:
#: "<n> resolved", and a count by severity ("2 critical, 6 major").
CHANGE_COUNT = re.compile(r"\b(?P<count>\d{1,9})\s+(?:new|resolved|critical|major|minor)\b", re.IGNORECASE)
#: A line describing a clean, held or carried run, which leaves the ledger open
#: over the last run's title even when it counts the carried findings by severity.
ZERO_FINDING_RUN = re.compile(r"\b(?:clean|held|carried|nothing\s+reproduced)\b", re.IGNORECASE)
#: A last line that is only a URL, the ledger wrapped onto a line of its own.
BARE_URL_LINE = re.compile(r"\A[<(\[]*https?://\S+\Z")
#: The line that carries a composed report's counts, when its last line is only the link.
COUNTS_DIGIT = re.compile(r"\d")
TRAILING_AUDIT = re.compile(r"\s+Audit$")
#: The coverage the line states: "across 3 clusters", "across 2 of 7 clusters".
COVERAGE = re.compile(r"(?<![\w.,-])across\s+(\d{1,9})(?:\s+of\s+(\d{1,9}))?\s+clusters?\b", re.IGNORECASE)
CLUSTERS_NOUN = ("cluster", "clusters")
#: "(1 unreachable)" after "across 3 clusters": a count whose noun is the coverage's.
BARE_GAP_COUNT = re.compile(r"(\d{1,9})\s+([a-z]+)", re.IGNORECASE)
GAP_SEPARATOR = ", "
AUDIT_SUFFIX = " audit"
#: What is left at the end of the fallback's line once the ledger URL is cut off.
LEDGER_SEPARATORS = " —–-:"

#: The issue fleet-audit keeps as its ledger: open, and labelled by the harness.
LEDGER_STATE = "open"
LEDGER_LABEL = "agent:audit"

TOP_FINDINGS = 2
#: A finding row: as long as the headline, shorter than a report row (``ROW_TEXT_MAX``).
FINDING_ROW_MAX = HEADLINE_MAX
#: The fallback's bold line when the issue could not be read; the SOPs' lines run to about 200 characters.
REPORT_LINE_MAX = 300
#: The most of a report line read for the headline. Reducing nested links is
#: quadratic, one pass per level, and a line holds what the model wrote; a finding
#: title is already capped by ``FINDING_HEADING``, and an issue title by GitHub.
LINE_READ_MAX = 1000
LEDGER_LINK = "[Ledger issue #{number} ↗]({url})"

#: The card's headline: the title's count is its one number.
CARD_HEADLINE = "{name} found {count} {noun}{coverage}"
FINDINGS_NOUN = ("thing to look at", "things to look at")
COVERAGE_PHRASE = " across {coverage}"
COVERAGE_OF = "{count} of {total} {noun}"
COVERAGE_COUNT = "{count} {noun}"
#: The headline ends on a full stop before a lead, and on a colon straight into the rows.
LEADS_ON = "."
ROWS_FOLLOW = ":"
CARD_LEAD_TEMPLATE = "{count} are worth fixing first:"
CARD_LEAD_ONE = "One is worth fixing first:"
CARD_NEUTRAL_LEAD_TEMPLATE = "Start with these {count}:"
CARD_NEUTRAL_LEAD_ONE = "Start with this one:"
URGENT_SEVERITIES = frozenset({"critical", "major"})
#: Counts written as words in a lead; past the last, the digits.
COUNT_WORDS = {2: "two", 3: "three"}

#: The Block Kit report: a choice button's click posts its label, the link
#: button opens the ledger.
ACTION_ID_PREFIX = "kage_audit"
FIX_FIRST = "Fix the first one"
FIX_ONLY = "Fix it"
SEE_ALL = "See all {count}"
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


def _new_phrase(line: str) -> str:
    count = _new_count(line)
    if not count:
        return ""
    verb = "is" if count == 1 else "are"
    return f" {count} {verb} new since the last run."


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


def _severity_findings(body: str) -> list[tuple[str, str]]:
    """``(severity, title)`` for every finding under a severity section, in body order."""
    # fleet-audit's own reader does the same: a body edited in the browser is CRLF.
    body = _without_fences(body.replace("\r\n", "\n"))
    found = []
    for section in SEVERITY_SECTION.finditer(body):
        start = section.end()
        following = ANY_SECTION.search(body, start)
        text = body[start : following.start() if following else len(body)]
        severity = section.group("severity").lower()
        found += [(severity, title.strip()) for title, _ in FINDING_HEADING.findall(text)]
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
    critical: int
    #: The "<n> new" sentence, and the report's whole ledger line, read for coverage and gaps.
    note: str
    line: str
    findings: list[tuple[str, str]]


def _parse_issue(issue: dict, report: str) -> AuditReport | None:
    """The ledger issue's counts and findings, or None when it is not this run's report.

    ``report`` is the relayed report: its whole ledger line is read for
    coverage and gaps, and its "<n> new" count joins the headline. A closed
    issue, one without the ledger label, and a zero-finding title do not
    parse: a clean run closes the ledger over its old title, and the fallback
    posts the report's own line.
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
    name = TRAILING_AUDIT.sub(AUDIT_SUFFIX, _row_text(title.group("name")).strip())
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
    findings = _severity_findings(str(issue.get("body") or ""))
    return AuditReport(name, count, critical, _new_phrase(line).strip(), line, findings)


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


def _coverage(coverage: re.Match | None, gap: str) -> str:
    """" across 3 clusters", or "" when the line states none or names a gap that "of m" does not show."""
    if not coverage:
        return ""
    count, total = int(coverage.group(1)), coverage.group(2)
    if total is not None:
        total = int(total)
        return COVERAGE_PHRASE.format(coverage=COVERAGE_OF.format(count=count, total=total, noun=CLUSTERS_NOUN[total != 1]))
    if gap:
        # "across 3 clusters" would vouch for all three.
        return ""
    return COVERAGE_PHRASE.format(coverage=COVERAGE_COUNT.format(count=count, noun=CLUSTERS_NOUN[count != 1]))


def _card(parsed: AuditReport) -> tuple[str, str, str]:
    """The card's ``(headline, note, detail)``.

    The headline names the audit, the title's count and the line's coverage,
    which it leaves out when the line names a gap it does not show as "<n> of
    <m>". The lead saying what to fix first is the note on the headline's
    line, or, when the line names a gap or a new count, follows them on the
    line under it, so it still runs into the rows.
    """
    coverage = COVERAGE.search(parsed.line)
    gap = _gap(parsed.line, coverage)
    headline = CARD_HEADLINE.format(
        name=parsed.name[:1].upper() + parsed.name[1:],
        count=parsed.count,
        noun=FINDINGS_NOUN[parsed.count != 1],
        coverage=_coverage(coverage, gap),
    )
    top = parsed.findings[:TOP_FINDINGS]
    lead = ""
    if top and parsed.count > len(top):
        if any(severity in URGENT_SEVERITIES for severity, _ in top):
            one, template = CARD_LEAD_ONE, CARD_LEAD_TEMPLATE
        else:
            one, template = CARD_NEUTRAL_LEAD_ONE, CARD_NEUTRAL_LEAD_TEMPLATE
        lead = one if len(top) == 1 else template.format(count=COUNT_WORDS.get(len(top), str(len(top))))
        lead = lead[:1].upper() + lead[1:]
    if gap or parsed.note:
        return headline + LEADS_ON, "", " ".join(part for part in (gap, parsed.note, lead) if part)
    return headline + (ROWS_FOLLOW if top and not lead else LEADS_ON), lead, ""


def _rows(findings: list[tuple[str, str]]) -> list[dict]:
    return [
        {"severity": severity, "text": _balanced_clip(_row_text(text), FINDING_ROW_MAX)} for severity, text in findings
    ]


def headline_from_issue(issue: dict, ref: LedgerRef, report: str = "") -> str | None:
    """The channel message built from the fetched ledger issue, or None when it does not parse (:func:`_parse_issue`)."""
    parsed = _parse_issue(issue, report)
    if parsed is None:
        return None
    headline, note, detail = _card(parsed)
    head = f"**{headline}**" + (f" {note}" if note else "")
    rows = [severity_row(row["severity"], row["text"]) for row in _rows(parsed.findings[:TOP_FINDINGS])]
    link = LEDGER_LINK.format(number=ref.number, url=ref.url)
    return "\n".join([head, *([detail] if detail else []), *rows, link])


def blocks_from_issue(issue: dict, ref: LedgerRef, report: str = "") -> tuple[list[dict], str] | None:
    """The Block Kit report's ``(blocks, text)`` built from the fetched ledger issue, or None.

    None whenever :func:`headline_from_issue` is. ``text`` is the message's
    mrkdwn ``text`` field.
    """
    parsed = _parse_issue(issue, report)
    if parsed is None:
        return None
    headline, note, detail = _card(parsed)
    top = _rows(parsed.findings[:TOP_FINDINGS])
    choices: list = []
    if top:
        choices.append(FIX_FIRST if len(top) > 1 else FIX_ONLY)
    if parsed.count > len(top):
        choices.append(SEE_ALL.format(count=parsed.count))
    links = [(LEDGER_BUTTON.format(number=ref.number), ref.url)]
    blocks = blocks_report(
        headline,
        note=note,
        detail=detail,
        rows=top,
        choices=choices,
        links=links,
        action_id_prefix=ACTION_ID_PREFIX,
    )
    text = fallback_text(" ".join(part for part in (headline, note, detail) if part), rows=top, links=links)
    return blocks, text


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
