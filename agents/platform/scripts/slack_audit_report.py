"""Slack presentation for a fleet-audit report: a headline, the top findings, the ledger link.

Pure functions only, like :mod:`slack_presenter`. The caller is
``session_kv_server.relay_cron_report``, which posts :func:`headline_message`
as the channel message when ``KAGE_SLACK_UX`` is on and the composed report
still carries the ledger title, then posts the full report as a reply in its
thread. The relay's send path (``hermes send``) takes text and no blocks, and
the Slack adapter converts standard markdown to mrkdwn on the way out, so the
output here is markdown: ``**bold**``, ``[label](url)`` and emoji shortcodes.

The ledger title is fleet-audit's ``issue_title``:
``[audit] <name> — <n> findings (<c> critical)``. A report without one, or
without the ledger issue URL, gets None and the caller posts it unchanged.
"""

from __future__ import annotations

import re

from slack_presenter import FOLD_POINTER, HEADLINE_MAX, LIST_MARKER, MD_BOLD, MD_LINK, _clip, _plain

#: fleet-audit's ledger title. The Chat Agent may re-type the dash.
LEDGER_TITLE = re.compile(
    r"\[audit\]\s+(?P<name>[^\n]+?)\s+[—–-]+\s+(?P<count>\d+)\s+findings?\s+\((?P<critical>\d+)\s+critical\)"
)
LEDGER_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/issues/(?P<number>\d+)")
NEW_SINCE = re.compile(r"new since (?:the )?last run\W*(?P<count>\d+)", re.IGNORECASE)
FINDING_LINE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+.*?\[(?P<severity>[a-z]+)\]", re.IGNORECASE)
SEVERITY_TAG = re.compile(r"\[[a-z]+\]\s*", re.IGNORECASE)
TRAILING_AUDIT = re.compile(r"\s+Audit$")

#: fleet-audit ranks critical, major, minor; the rest are accepted in case the
#: Chat Agent re-words them.
SEVERITY_RANK = {"critical": 0, "high": 1, "major": 1, "medium": 2, "minor": 3, "low": 3}
SEVERITY_MARKERS = {
    "critical": ":red_circle:",
    "high": ":red_circle:",
    "major": ":large_yellow_circle:",
    "medium": ":large_yellow_circle:",
    "minor": ":white_circle:",
    "low": ":white_circle:",
}
TOP_FINDINGS = 2
ROW_TEXT_MAX = HEADLINE_MAX
LEDGER_LINK = "[Ledger issue #{number} ↗]({url})"
CLEAN_LINE = "{name}: clean. [Ledger closed ↗]({url})"
FOLD_TITLE = "all {count} findings"


def _findings_phrase(count: int) -> str:
    return f"{count} finding" if count == 1 else f"{count} findings"


def _new_phrase(count: int) -> str:
    verb = "is" if count == 1 else "are"
    return f" {count} {verb} new since the last run."


def _row_text(line: str) -> str:
    """A finding line without its list marker, bold, link syntax or severity tag.

    Unlike :func:`slack_presenter._plain` it keeps inline code, whose ``*`` is
    often the finding itself (a role granting ``*`` on secrets).
    """
    text = LIST_MARKER.sub("", line.strip())
    text = MD_LINK.sub(r"\1", text)
    text = MD_BOLD.sub(lambda m: m.group(1) or m.group(2), text)
    return SEVERITY_TAG.sub("", text, count=1).strip()


def _top_rows(report: str) -> list[str]:
    found = []
    for position, line in enumerate(report.splitlines()):
        match = FINDING_LINE.match(line)
        if not match:
            continue
        severity = match.group("severity").lower()
        if severity not in SEVERITY_RANK:
            continue
        text = _clip(_row_text(line), ROW_TEXT_MAX)
        found.append((SEVERITY_RANK[severity], position, severity, text))
    found.sort()
    return [
        f"{SEVERITY_MARKERS[severity]} **{severity}**  {text}"
        for _, _, severity, text in found[:TOP_FINDINGS]
    ]


def headline_message(report: str) -> str | None:
    """The channel message for an audit report, or None when it does not read as one."""
    title = LEDGER_TITLE.search(report)
    ledger = LEDGER_URL.search(report)
    if not title or not ledger:
        return None
    name = TRAILING_AUDIT.sub(" audit", _plain(title.group("name")))
    count = int(title.group("count"))
    url = ledger.group(0)
    if count == 0:
        return CLEAN_LINE.format(name=name, url=url)

    head = f"**{name}: {_findings_phrase(count)}, {title.group('critical')} critical.**"
    new = NEW_SINCE.search(report)
    if new and int(new.group("count")):
        head += _new_phrase(int(new.group("count")))
    lines = [head, *_top_rows(report)]
    lines.append(LEDGER_LINK.format(number=ledger.group("number"), url=url))
    lines.append(FOLD_POINTER.format(title=FOLD_TITLE.format(count=count)))
    return "\n".join(lines)
