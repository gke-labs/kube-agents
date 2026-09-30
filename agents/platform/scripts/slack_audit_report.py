"""Slack presentation for a fleet-audit report: a headline, the top findings, the ledger link.

Pure functions only. The caller is
``session_kv_server.relay_cron_report``. With ``KAGE_SLACK_UX`` on it finds the
ledger issue the report ends with (:func:`ledger_ref`), fetches that issue, and
posts :func:`headline_from_issue` as the channel message; when the fetch or the
parse fails, or the issue is closed or not labelled ``agent:audit``, it posts
:func:`headline_fallback` instead. A clean run closes its ledger without
rewriting the title, so a closed issue's counts are the last run's, not this
one's. The relay's send path
(``hermes send``) takes text and no blocks, and the Slack adapter converts
standard markdown to mrkdwn on the way out, so the output here is markdown:
``**bold**``, ``[label](url)`` and emoji shortcodes.

The audit SOPs relay one line ending in ``— <issue_url>``. The counts and the
findings come from the issue itself, whose title is fleet-audit's
``issue_title`` (``[audit] <name> — <n> findings (<c> critical)``) and whose
body has one ``### <Severity> (<n>)`` section per severity, most severe first,
each finding a ``#### <title> <!-- finding:<id> -->`` heading. Nothing in the
report's own text can set the counts or pick the link, beyond its last URL.
The relayed line itself goes under the headline, since it alone carries the
run's coverage, resolved count and remediation pull requests.
"""

from __future__ import annotations

import re
from typing import NamedTuple

HEADLINE_MAX = 150
ELLIPSIS = "…"
#: One marker per fleet-audit severity section.
SEVERITY_MARKERS = {
    "critical": ":red_circle:",
    "major": ":large_yellow_circle:",
    "minor": ":white_circle:",
}
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
LIST_MARKER = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
MD_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")

ISSUE_URL = r"https://github\.com/(?P<repo>[\w.-]+/[\w.-]+)/issues/(?P<number>\d+)"
#: The ledger link: the report's last URL, after the SOPs' dash or a "Ledger:"
#: label, bare, in angle brackets or as a markdown link.
TRAILING_LEDGER = re.compile(
    r"(?:[—–]|\s-|\bledger(?:\s+issue)?:?)\s*(?:\[[^\]\n]*\]\()?<?"
    + ISSUE_URL
    + r">?\)?[\s.]*\Z",
    re.IGNORECASE,
)
#: fleet-audit's ``issue_title``, whole. The coverage-incomplete title does not
#: match, so a run that saw too little is never read as clean.
LEDGER_TITLE = re.compile(
    r"\A\[audit\]\s+(?P<name>.+?)\s+[—–-]+\s+(?P<count>\d+)\s+findings?\s+\((?P<critical>\d+)\s+critical\)\s*\Z"
)
SEVERITY_SECTION = re.compile(r"^###[ \t]+(?P<severity>Critical|Major|Minor)[ \t]+\(", re.MULTILINE)
ANY_SECTION = re.compile(r"^###[ \t]", re.MULTILINE)
#: fleet-audit's ``FINDING_MARKER_RE``.
FINDING_HEADING = re.compile(r"^####[ \t]+(.*?)[ \t]*<!--[ \t]*finding:[ \t]*(\S+?)[ \t]*-->[ \t]*$", re.MULTILINE)
NEW_COUNT = re.compile(r"\b(?P<count>\d+)\s+new\b", re.IGNORECASE)
TRAILING_AUDIT = re.compile(r"\s+Audit$")
AUDIT_SUFFIX = " audit"
#: What is left at the end of the fallback's line once the ledger URL is cut off.
LEDGER_SEPARATORS = " —–-:"
BACKTICK = "`"

#: The issue fleet-audit keeps as its ledger: open, and labelled by the harness.
LEDGER_STATE = "open"
LEDGER_LABEL = "agent:audit"

TOP_FINDINGS = 2
ROW_TEXT_MAX = HEADLINE_MAX
#: The relayed line under the headline; the SOPs' lines run to about 200 characters.
REPORT_LINE_MAX = 300
LEDGER_LINK = "[Ledger issue #{number} ↗]({url})"
ALL_FINDINGS = ": all {count} findings"


class LedgerRef(NamedTuple):
    url: str
    repo: str
    number: int


def _plain(markdown: str) -> str:
    """One line of markdown as plain text: no heading, list marker, emphasis or link syntax."""
    text = HEADING.sub("", markdown.strip())
    text = LIST_MARKER.sub("", text)
    text = MD_LINK.sub(r"\1", text)
    text = MD_BOLD.sub(lambda m: m.group(1) or m.group(2), text)
    return text.replace("*", "").replace("`", "").strip()


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - len(ELLIPSIS)].rsplit(" ", 1)[0] or text[: limit - len(ELLIPSIS)]
    return cut.rstrip() + ELLIPSIS


def ledger_ref(report: str) -> LedgerRef | None:
    """The ledger issue a report ends with, or None when it does not end with one."""
    match = TRAILING_LEDGER.search(report)
    if not match:
        return None
    number = match.group("number")
    return LedgerRef(f"https://github.com/{match.group('repo')}/issues/{number}", match.group("repo"), int(number))


def has_more(report: str) -> bool:
    """Whether the report says more than its one line, so it is worth posting in the thread."""
    return len([line for line in report.splitlines() if line.strip()]) > 1


def _findings_phrase(count: int) -> str:
    return f"{count} finding" if count == 1 else f"{count} findings"


def _new_count(report: str) -> int:
    match = NEW_COUNT.search(report)
    return int(match.group("count")) if match else 0


def _new_phrase(report: str) -> str:
    count = _new_count(report)
    if not count:
        return ""
    verb = "is" if count == 1 else "are"
    return f" {count} {verb} new since the last run."


def _balanced_clip(text: str, limit: int) -> str:
    """:func:`_clip` that never leaves a code span open."""
    clipped = _clip(text, limit - len(BACKTICK))
    if clipped.count(BACKTICK) % 2 == 0:
        return clipped
    if clipped.endswith(ELLIPSIS):
        return clipped[: -len(ELLIPSIS)] + BACKTICK + ELLIPSIS
    return clipped + BACKTICK


def _severity_findings(body: str) -> list[tuple[str, str]]:
    """``(severity, title)`` for every finding under a severity section, in body order."""
    found = []
    for section in SEVERITY_SECTION.finditer(body):
        start = section.end()
        following = ANY_SECTION.search(body, start)
        text = body[start : following.start() if following else len(body)]
        severity = section.group("severity").lower()
        found += [(severity, title.strip()) for title, _ in FINDING_HEADING.findall(text)]
    return found


def headline_from_issue(issue: dict, ref: LedgerRef, report: str = "") -> str | None:
    """The channel message built from the fetched ledger issue, or None when it does not parse.

    ``report`` is the relayed report: its "<n> new" count joins the headline and
    its ledger line goes under it. A closed issue, one without the ledger
    label, and a zero-finding title do not parse: a clean run closes the
    ledger over its old title, and the fallback posts the report's own line.
    """
    if str(issue.get("state") or "").lower() != LEDGER_STATE:
        return None
    if LEDGER_LABEL not in (issue.get("labels") or ()):
        return None
    title = LEDGER_TITLE.match(str(issue.get("title") or "").strip())
    if not title:
        return None
    name = TRAILING_AUDIT.sub(AUDIT_SUFFIX, title.group("name").strip())
    count, critical = int(title.group("count")), int(title.group("critical"))
    if _new_count(report) > count:
        return None  # a stale or wrong ledger: the report has more new findings than it lists
    if count == 0:
        return None
    findings = _severity_findings(str(issue.get("body") or ""))
    link = LEDGER_LINK.format(number=ref.number, url=ref.url)
    head = f"**{name}: {_findings_phrase(count)}, {critical} critical.**" + _new_phrase(report)
    line = _ledger_line(report, REPORT_LINE_MAX)
    rows = [
        f"{SEVERITY_MARKERS[severity]} **{severity}**  {_balanced_clip(text, ROW_TEXT_MAX)}"
        for severity, text in findings[:TOP_FINDINGS]
    ]
    return "\n".join([head, *([line] if line else []), *rows, link + ALL_FINDINGS.format(count=count)])


def _fallback_line(line: str, limit: int) -> str:
    match = TRAILING_LEDGER.search(line)
    if match:
        line = line[: match.start()]
    return _clip(_plain(line).rstrip(LEDGER_SEPARATORS), limit)


def _ledger_line(report: str, limit: int) -> str:
    """The report's ledger line as plain text without its link: the last line, or
    the first when the last is only the link."""
    lines = [line for line in report.splitlines() if line.strip()]
    return (_fallback_line(lines[-1], limit) or _fallback_line(lines[0], limit)) if lines else ""


def headline_fallback(report: str, ref: LedgerRef) -> str | None:
    """The report's ledger line in bold with the ledger link, for when the issue could not be read.

    The ledger line is the report's last, the SOPs' one line; a sentence the
    relay turn put above it is not the headline. A last line that is only the
    link falls back to the first.
    """
    head = _ledger_line(report, REPORT_LINE_MAX)
    if not head:
        return None
    return f"**{head}**\n{LEDGER_LINK.format(number=ref.number, url=ref.url)}"
