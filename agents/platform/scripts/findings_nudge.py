#!/usr/bin/env python3
"""The findings nudge: names findings from the queue in chat, paced.

Backs the ``findings-morning-nudge`` cron job, which runs hourly with
``no_agent: true`` and ``deliver: "chat"``. Most hours have nothing to say and
print nothing. When findings are due, this script writes a structured sidecar
card (:mod:`findings_nudge_card`) carrying the ``kube-agents.chat/v1`` card
fields, a cluster breakdown table for the rest of the queue, and hidden thread
metadata (finding ids and item coverage counts), then prints a clean plain-text
fallback to stdout.

No model turn runs here: ordering comes from ``GET /v1/findings/ranked``,
pacing from ``findings_queue.pace`` (§7.2 of ``docs/designs/inventory-findings-queue.md``),
and recommendations from the detectors that registered the findings. Each run:

1. Expires lapsed snoozes, since this job is the queue's only tick.
2. Reads the open queue and the items already added today, and asks ``pace``
   what to name. New criticals are added from 12:00 UTC, at most
   ``FINDINGS_DAILY_CRITICALS`` a day; non-criticals from
   ``FINDINGS_NONCRITICAL_AFTER_HOUR`` UTC, at most ``FINDINGS_NONCRITICAL_MAX``
   a day, and only while no critical is pending or waiting. While any
   non-critical the nudge named waits for a decision, nothing is added.
3. Once a UTC day, on the first run at or after 12:00 UTC, also reminds the
   top pending criticals and names every pending non-critical holding
   additions back. Each line says how many other objects a decision on it
   reaches (`findings_queue.patch_finding`); the ids, which chat does not
   print, go into the thread's stored context for a reply in the thread.
4. Writes the sidecar card, prints the fallback text, records the day's state
   in the profile home (the daily part sent, the items announced today), then
   marks every named row surfaced as the paced publisher, which is what makes
   an addition pending and records it against the day. An announced item
   counts against the day from the state file alone while its marks have not
   landed.

Nothing is added before the first inventory report is out (its delivery
claimed, or on the conversation path its post's outcome recorded), so that
report is the first thing onboarding says about the fleet. The hold ends
``FIRST_REPORT_HOLD_HOURS`` (24) after onboarding filed its sweep, delivered or
not: an undelivered report by then means onboarding stalled, and holding on
would keep the queue silent for good. Each run that is held, or would be but
for that limit, says which on stderr. A run that cannot read the queue, or
fails in any other way before it prints, fails (exit 1, which the scheduler
posts as a failure) once a UTC day; later failures that day go to stderr and
exit 0.

The bookkeeping is best-effort, and what a failed write costs: an addition
whose marks all fail is not offered again the same day (the state file
remembers it), still counts against that day's limit, and holds additions back
as a pending item of its class would, though the stop-add notice does not name
it. Unlike a recorded addition, it counts only while undecided: a decision on
it the same day refunds its slot. It is offered as new the next day. When only
some of an addition's marks fail, the addition is on record: a member left new
waits for the next day and does not count again; a non-critical one does not
stop additions, and a critical one holds non-criticals back as a critical
waiting for the budget does. If the state file cannot be written
either, the same additions and the daily part are posted again every hour.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import chat_message
import findings_nudge_card as nudge_card
import findings_queue as fq
import slack_audit_report
import slack_presenter

JOB_ID = nudge_card.JOB_ID

DEFAULT_ENDPOINT = "http://127.0.0.1:8699"
TIMEOUT_SECONDS = 30

PUBLISHER = "nudge"
BACKLOG_PUBLICATION_PATH = "/v1/findings/publication/backlog"
TARGET_REF_KEY = "target_ref"
HTTP_SCHEMES = ("http://", "https://")

# The profile home, where the once-a-day state lives, and the gateway home,
# where onboarding's markers live. Under the ticker HERMES_HOME is the profile
# home (`profiles/platform`); in a shell in the container it is the gateway's.
HOME_ENV = "HERMES_HOME"
GATEWAY_HOME = "/opt/data"
PROFILE_HOME = Path("profiles") / "platform"
PROFILES_DIR = "profiles"
STATE_FILE = ".findings_nudge_state.json"
DAILY_KEY = "daily_section"
FAILURE_KEY = "failure_reported"
# {"day": <UTC date>, "keys": [<item key>, ...], "ids": [<member id>, ...]}:
# what was added today and the members it named, so a mark that failed does
# not announce the same item again an hour later, and the item still counts
# against the day and holds additions back (`pace`). A row that joins the line
# later is not one of the ids, so it does not hold anything back. An item
# whose additions today are all in the queue's record (some marks succeeded)
# does not count again; it waits for tomorrow (`pace`). A record an
# older build wrote today has no "ids"; the key alone decides until the day ends.
ANNOUNCED_KEY = "announced"
ANNOUNCED_DAY = "day"
ANNOUNCED_KEYS = "keys"
ANNOUNCED_IDS = "ids"
# The run id sent with an addition's marks: one per run, so the members of an
# item marked one by one are one addition.
RUN_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Onboarding's markers (`bootstrap_scan_gate.py`, `bootstrap_delivery.py`).
# Filed and not yet delivered means a first report is on its way.
SCAN_FILED_MARKER = ".bootstrap_scan_filed"
DELIVERED_MARKER = ".bootstrap_completed"
# A claim on the conversation path is one attempt and can
# be released; the report is out only once this outcome marker exists
# (bootstrap_delivery.py, CONVERSATION_FILE, CONVERSATION_ALIGNED_MARKER and
# CONVERSATION_DONE_FILE).
CONVERSATION_MARKERS = ("onboarding_conversation.json", ".user_aligned_conversation")
CONVERSATION_DONE_MARKER = ".bootstrap_conversation_done"
# The line of the filed marker holding when it was filed, in Unix seconds.
# The marker's mtime stands in when the line is missing or unreadable.
FILED_AT_PREFIX = "filed_at="
# How long after the sweep was filed the first report is waited for. A report
# still undelivered by then means onboarding stalled, and holding on would add
# nothing ever again.
FIRST_REPORT_HOLD_HOURS = 24
FIRST_REPORT_HOLD = timedelta(hours=FIRST_REPORT_HOLD_HOURS)
TIME_FORMAT = "%Y-%m-%d %H:%M UTC"

LOG_PREFIX = "findings_nudge"

# Row and text sanitization constants.
CRITICAL_SEVERITY = fq.SEVERITIES[0]
MAJOR_SEVERITY = fq.SEVERITIES[1]
DEFAULT_SEVERITY = MAJOR_SEVERITY
# What the upgrade readiness review calls its checks and a cluster-wide
# finding's object (`upgrade_readiness_review.CHECK_PREFIX`, `CLUSTER_OBJECT`);
# test_findings_nudge.py holds the two in step.
UPGRADE_CHECK_PREFIX = "upgrade-"
CLUSTER_SCOPE_OBJECTS = ("Cluster",)
UNSCOPED_CLUSTER = "fleet"

LEADING_NUMBER_RE = re.compile(r"^\s*(?:\[\d+\]\s*|\d+\.\s+)+")
# "minAvailable: 6 (>= 1 expected pods)" says the budget is at or above the pods
# expected, so the plain form keeps both numbers and claims no floor on pods.
EXPECTED_PODS_RE = re.compile(r"\(>=\s*(\d+)\s+expected\s+pods?\)", re.IGNORECASE)
EXPECTED_PODS_PLAIN = "({pods} expected)"
NESTED_PARENS_RE = re.compile(r"\(([^()]+?)\s+\(([^()]+)\)\)")
NESTED_PARENS_FLAT = r"(\1, \2)"
# "1 replicas" is a count; the "1" that ends a version or a name ("1.31.1 nodes", "pool-1 nodes") is not.
ONE_PLURAL_RE = re.compile(r"(?<![\d.-])\b1\s+(replicas|pods|nodes|objects|instances|clusters)\b", re.IGNORECASE)

# A recommendation is text this script did not write, so it is shown as written
# or not at all: nothing is cut out of it and nothing is edited around a
# command. Where a command is found (a tool in backticks, in parentheses,
# opening the action, after "Run", or followed in its sentence by a flag),
# `COMMAND_ACTION` stands in for the whole recommendation. A false hit ("drop
# the -latest tag") costs the sentence, never its meaning. A command with no
# flag written into a sentence is not told from prose and stays as written.
# The one rewording is the whole action `kubectl delete <kind> <name>`.
CLI_TOOLS_PATTERN = r"(?:kubectl|gcloud|helm|git|gh|terraform|tofu|docker)"
# The plain form only: a flag is not a kind, and a condition after it is not dropped.
KUBECTL_DELETE_RE = re.compile(
    r"^\s*`?kubectl\s+delete\s+([a-z][a-z0-9.]*)\s+([a-z0-9](?:[a-z0-9._-]*[a-z0-9])?)`?\s*\.?\s*$"
)
COMMAND_FOUND_RE = re.compile(
    rf"`{CLI_TOOLS_PATTERN}\b[^`]*`"
    rf"|\(\s*(?i:e\.g\.?,?\s*|for example,?\s*|run\s+)?`?{CLI_TOOLS_PATTERN}\b"
    rf"|^\s*(?i:run\s+)?`?{CLI_TOOLS_PATTERN}\s+\S"
    # A "." or ";" inside a version or a file name does not end the sentence.
    rf"|(?i:\b{CLI_TOOLS_PATTERN}\b)(?:(?![.;](?:\s|$)).)*?\s--?[A-Za-z]",
)
# In place of a recommendation with a command in it. It states no action of its
# own, and it is true also when the source says not to run the command.
COMMAND_ACTION = "The recommendation names a command, which is not shown here."
# The name the first tests gave it.
FALLBACK_ACTION = COMMAND_ACTION
REMOVE_ACTION = "Remove the {kind} {name}."
SUBJECT_EDGE_CHARS = " :—-"
# A detector's title is "<what is wrong>: <subject> ..."; what may follow the subject's name.
TITLE_SUBJECT_SEPARATOR = ": "
SUBJECT_END_CHARS = " (:"
POD_NOUN = ("pod", "pods")
CLUSTER_NOUN = ("cluster", "clusters")
WAITING_JOINER = " and "
SENTENCE_ENDINGS = (".", "!", "?")

# Card and fallback presentation constants.
ROLE_NEW = "new"
ROLE_REMINDER = "reminder"
ROLE_BLOCKING = "blocking"

# "delay or block": a PodDisruptionBudget holds each node's drain for up to an
# hour and the upgrade then goes on (GKE's node pool upgrade strategies page);
# the upgrade readiness review lists other criticals, an orphan Node object
# among them, as stopping it.
HEADLINE_UPGRADE_CRITICAL = (
    "1 critical issue will delay or block cluster upgrades",
    "{count} critical issues will delay or block cluster upgrades",
)
HEADLINE_NEW_CRITICAL = (
    "1 new critical issue needs attention",
    "{count} new critical issues need attention",
)
HEADLINE_OPEN_CRITICAL = (
    "1 critical issue needs attention",
    "{count} critical issues need attention",
)
HEADLINE_UPGRADE_OTHER = (
    "1 upgrade readiness issue needs review",
    "{count} upgrade readiness issues need review",
)
HEADLINE_NEW_OTHER = (
    "1 new issue needs review",
    "{count} new issues need review",
)
HEADLINE_BLOCKING = (
    "1 open issue is pausing new findings",
    "{count} open issues are pausing new findings",
)

NOTE_OPEN_ACROSS = "· {open_count} open across {clusters}"
NOTE_OPEN_ONLY = "· {open_count} open"
NOTE_SINGLE_CLUSTER = "· {cluster}"
NOTE_MULTI_CLUSTER = "· across {clusters}"

# The fallback opens on the bold line the card's headline is.
FALLBACK_HEAD = "**{headline}**"
# How many more objects a decision on the line reaches, beside the one it names.
EXTRA_SUFFIX = " (+{extra} more)"
EXTRA_ONLY = "({extra} more)"

FIX_SINGLE_LABEL = "Fix issue"
FIX_FIRST_LABEL = "Fix first issue"
REVIEW_SINGLE_LABEL = "Review item"
REVIEW_FIRST_LABEL = "Review first item"
SEE_ALL_LABEL = "See all {count} items"
BACKLOG_LINK_LABEL = "Open backlog"
BACKLOG_TEXT_LINE = "Open backlog: {url}"
CHOICE_REPLY_FORMAT = "{label}: {summary}"

BLOCKING_NOTICE_SINGLE = "New findings are paused until you resolve, snooze, or dismiss this open item{waiting}."
BLOCKING_NOTICE_MULTI = (
    "New findings are paused until you resolve, snooze, or dismiss these {count} open items{waiting}."
)
WAITING_SUFFIX = " ({waiting} waiting behind {pronoun})"
WAITING_PRONOUNS = ("it", "them")
WAITING_CRITICAL = ("critical issue", "critical issues")
WAITING_OTHER = ("other issue", "other issues")
WAITING_PLAIN = ("issue", "issues")
# "open", as the note beside the headline says: the rest of the queue holds
# lines being worked on and lines a message already named, not only queued ones.
MORE_OPEN = ("more issue is", "more issues are")
OPEN_WITH_CRITICAL = "{more} open ({critical} critical, {lower} lower priority)."
OPEN_ONLY_CRITICAL = "{more} open ({critical} critical)."
OPEN_PLAIN = "{more} open."
ROLLED_UP_SINGLE = "1 provider-managed finding requires no action from you and is hidden."
ROLLED_UP_MULTI = "{count} provider-managed findings require no action from you and are hidden."

FOLD_TITLE_TEMPLATE = "Queue breakdown ({count} more open)"
# The table lists the first clusters only; its title says so when it is cut
# (the count is in the line above it, and the title has 40 characters).
FOLD_TITLE_CUT_TEMPLATE = "Queue breakdown (top {shown} clusters)"
# One column per severity (`findings_queue.SEVERITIES`), so the line above the
# table can say "lower priority" for major and minor together.
FOLD_COLUMNS = ["Cluster", "Critical", "Major", "Minor", "Next open issue"]
FOLD_CRITICAL, FOLD_MAJOR, FOLD_MINOR, FOLD_NEXT = "critical", "major", "minor", "next"
FOLD_PREVIEW = "{target}: {summary}"
MAX_FOLD_CLUSTERS = 15
FOLD_NEXT_ISSUE_MAX = 80
REPLY_SUMMARY_MAX = 300

METADATA_HEADER = "[Queued item metadata for thread replies]"
METADATA_LINE = "{index}. [{role}] id={id} members={members} location={location} covers={covers} unreported={unreported}"
METADATA_NO_LOCATION = "-"


class NudgeBundle(NamedTuple):
    """Rendered text fallback and structured sidecar card for one nudge run."""

    text: str
    card: dict | None


class FormattedItem(NamedTuple):
    """Cleaned display and metadata representations of one surfaced item."""

    role: str
    severity: str
    cluster: str
    card_row: dict
    plain_line: str
    reply_summary: str
    metadata_line: str


def _request(endpoint: str, path: str, body: dict | None = None, method: str = "") -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    token = (os.environ.get("SESSION_KV_API_KEY") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{endpoint}{path}", data=data, headers=headers, method=method or ("POST" if data else "GET")
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _homes() -> tuple[Path, Path]:
    """(gateway home, profile home), from whichever of the two HERMES_HOME is."""
    home = Path(os.environ.get(HOME_ENV) or GATEWAY_HOME)
    if (home / PROFILE_HOME).is_dir():
        return home, home / PROFILE_HOME
    if home.parent.name == PROFILES_DIR:
        return home.parent.parent, home
    return home, home


def _filed_at(marker: Path) -> tuple[datetime, str] | None:
    """When the sweep was filed, and where that came from; None once the marker is gone."""
    try:
        for line in marker.read_text(encoding="utf-8").splitlines():
            if line.startswith(FILED_AT_PREFIX):
                seconds = int(line[len(FILED_AT_PREFIX):].strip())
                return datetime.fromtimestamp(seconds, timezone.utc), f"its {FILED_AT_PREFIX} line"
    except (OSError, ValueError, OverflowError):
        pass
    try:
        mtime = marker.stat().st_mtime
    except OSError:
        # Removed since the caller saw it, as the re-arm runbook does.
        return None
    return datetime.fromtimestamp(mtime, timezone.utc), "its mtime"


def first_report_markers(gateway_home: Path) -> tuple[Path, Path]:
    """The sweep marker, and the marker that says the first report is out.

    Out is the delivery claim, or on the conversation path
    the recorded outcome, because a claim there can be released and taken
    again. The board snapshot reads the same pair for the Home tab, and ends
    its state early once delivery has registered the sweep's findings.
    """
    filed, delivered = gateway_home / SCAN_FILED_MARKER, gateway_home / DELIVERED_MARKER
    if any((gateway_home / name).exists() for name in CONVERSATION_MARKERS):
        delivered = gateway_home / CONVERSATION_DONE_MARKER
    return filed, delivered


def first_report_hold(gateway_home: Path, now: datetime) -> tuple[bool, str]:
    """Whether to add nothing yet, and the line to log about it ("" for none).

    Held while onboarding has filed its sweep, has not delivered the report,
    and filed it no more than `FIRST_REPORT_HOLD` ago. An install where
    onboarding never started (the experimental front door does not run it)
    has neither marker and is not held.
    """
    filed, delivered = first_report_markers(gateway_home)
    if not filed.exists() or delivered.exists():
        return False, ""
    found = _filed_at(filed)
    if found is None:
        return False, ""
    filed_at, source = found
    markers = f"{filed} present, filed {filed_at.strftime(TIME_FORMAT)} by {source}; {delivered} absent"
    if now - filed_at > FIRST_REPORT_HOLD:
        return False, (
            f"{LOG_PREFIX}: the first inventory report is still undelivered more than "
            f"{FIRST_REPORT_HOLD_HOURS} hours after its sweep was filed, so additions are no longer "
            f"held for it ({markers})"
        )
    return True, (
        f"{LOG_PREFIX}: adding nothing until the first inventory report is out (its delivery claimed, or "
        f"its conversation post's outcome recorded), or until "
        f"{(filed_at + FIRST_REPORT_HOLD).strftime(TIME_FORMAT)} ({markers})"
    )


def _load_state(home: Path) -> dict:
    try:
        state = json.loads((home / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _save_state(home: Path, state: dict) -> bool:
    path = home / STATE_FILE
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        sys.stderr.write(f"{LOG_PREFIX}: could not record its state in {path}: {exc}\n")
        return False
    return True


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def _plain(text: str) -> str:
    """One line of a finding's text with no live mention or link in it.

    A title or a recommendation is text a detector or a model wrote, and the
    fallback goes out as plain text: `<!channel>` loses its brackets and a link
    keeps its label, as the fleet-audit card's rows do.
    """
    return slack_audit_report._row_text(_one_line(text))


def _fix_singular_plurals(text: str) -> str:
    return ONE_PLURAL_RE.sub(lambda m: f"1 {m.group(1)[:-1]}", text)


def clean_title(raw_title: str) -> str:
    """Strip check entry numbers, raw scores, and awkward pod/replica phrasing from a title."""
    cleaned = LEADING_NUMBER_RE.sub("", _plain(raw_title))
    cleaned = EXPECTED_PODS_RE.sub(
        lambda m: EXPECTED_PODS_PLAIN.format(pods=_plural(int(m.group(1)), *POD_NOUN)),
        cleaned,
    )
    cleaned = NESTED_PARENS_RE.sub(NESTED_PARENS_FLAT, cleaned)
    cleaned = _fix_singular_plurals(cleaned)
    return _one_line(cleaned)


def _ensure_period(sentence: str) -> str:
    stripped = sentence.strip()
    if not stripped:
        return ""
    return stripped if stripped.endswith(SENTENCE_ENDINGS) else f"{stripped}."


def clean_action(raw_action: str) -> str:
    """Show a recommendation's action as written, or `COMMAND_ACTION` when it holds a command.

    Nothing is cut out of the text. The whole action `kubectl delete <kind>
    <name>` is said in words, "Remove the <kind> <name>.".
    """
    text = _plain(raw_action)
    if not text:
        return ""
    delete_match = KUBECTL_DELETE_RE.match(text)
    if delete_match:
        return REMOVE_ACTION.format(kind=delete_match.group(1), name=delete_match.group(2))
    if COMMAND_FOUND_RE.search(text):
        return COMMAND_ACTION
    return _ensure_period(_fix_singular_plurals(text))


def ambiguous_clusters(rows: list[dict]) -> frozenset[str]:
    """Cluster names shared by two or more distinct projects across ``rows``."""
    projects_by_cluster: dict[str, set[str]] = {}
    for row in rows:
        cluster = str(row.get("cluster") or "").strip()
        project = str(row.get("project") or "").strip()
        if cluster and project:
            projects_by_cluster.setdefault(cluster, set()).add(project)
    return frozenset(cluster for cluster, projects in projects_by_cluster.items() if len(projects) > 1)


def cluster_label(finding: dict, ambiguous: frozenset[str] = frozenset()) -> str:
    """Readable cluster name, disambiguated with its project only when names collide."""
    cluster = str(finding.get("cluster") or "").strip()
    project = str(finding.get("project") or "").strip()
    if cluster and cluster in ambiguous and project:
        return _plain(f"{cluster} ({project})")
    return _plain(cluster)


def _target_name(finding: dict) -> str:
    raw_cluster = str(finding.get("cluster") or "").strip()
    namespace = str(finding.get("namespace") or "").strip()
    obj = str(finding.get("object") or "").strip()
    # A cluster-wide finding has no workload to name: the row leads with its cluster.
    if not obj or obj == raw_cluster or obj in CLUSTER_SCOPE_OBJECTS:
        return ""
    return _plain(f"{namespace}/{obj}" if namespace else obj)


def _leading_groups(text: str) -> tuple[str, str]:
    """Split ``text`` into the parenthesised groups it opens with and what follows them."""
    end = 0
    while end < len(text) and text[end] == "(":
        depth = 0
        for offset, char in enumerate(text[end:]):
            depth += (char == "(") - (char == ")")
            if depth == 0:
                break
        else:
            break  # an unclosed group is not a group
        end += offset + 1
        end += len(text[end:]) - len(text[end:].lstrip())
    return text[:end].rstrip(), text[end:]


def _peel_redundant_subject(cleaned_title: str, target: str, obj: str, raw_cluster: str) -> str:
    """Remove redundant workload or cluster names when the row already leads with them.

    A detector's title is `<what is wrong>: <subject> (<its settings>) <what it
    does>`; the settings go last, so what is left still reads as a sentence.
    """
    category, colon, after = cleaned_title.partition(TITLE_SUBJECT_SEPARATOR)
    for candidate in (target, obj):
        # Right after the first colon, and the whole name only: `web` is not the
        # subject of "...: web-frontend", nor of "...: web, api and worker".
        if not (candidate and colon and after.startswith(candidate)):
            continue
        rest = after[len(candidate) :]
        if rest and rest[0] not in SUBJECT_END_CHARS:
            continue
        rest = rest.lstrip(SUBJECT_EDGE_CHARS)
        if not rest:
            return category
        groups, lead = _leading_groups(rest)
        if not groups:
            return f"{category}: {rest}"
        return f"{category}: {lead} {groups}" if lead else f"{category} {groups}"
    for candidate in (target, obj):
        if candidate and cleaned_title.startswith(f"{candidate} "):
            return cleaned_title[len(candidate) + 1 :].strip()
    if not target and raw_cluster and cleaned_title.endswith(f" on {raw_cluster}"):
        return cleaned_title[: -len(f" on {raw_cluster}")].strip()
    return cleaned_title


def _best_severity(members: list[dict]) -> str:
    severities = [str(row.get("severity") or "") for row in members if row.get("severity") in fq.SEVERITIES]
    if not severities:
        return DEFAULT_SEVERITY
    return min(severities, key=fq.SEVERITIES.index)


def format_item(
    index: int,
    role: str,
    item,
    ambiguous: frozenset[str] = frozenset(),
) -> FormattedItem:
    """Format one surfaced ``PacedItem`` into card row, plain fallback line, and hidden metadata."""
    severity = _best_severity(item.members)
    # The line's tag and the row under it come from one member: the queue ranks
    # by score, so the first member is not always the most severe one.
    top = next((row for row in item.members if row.get("severity") == severity), item.members[0])
    raw_cluster = str(top.get("cluster") or "").strip()
    cluster = cluster_label(top, ambiguous)
    obj = str(top.get("object") or "").strip()
    target = _target_name(top)
    # Every object a decision on this line reaches, the ones a sweep stopped
    # reporting included: the reader is told how far a dismissal goes.
    extra = max(0, max(len(item.members), item.covers) - 1)
    extra_suffix = EXTRA_SUFFIX.format(extra=extra) if extra else ""

    raw_title = str(top.get("title") or "").strip()
    cleaned = clean_title(raw_title)
    summary = _peel_redundant_subject(cleaned, target, obj, raw_cluster)

    if target and cluster:
        subject_md = f"**{target}**{extra_suffix} on `{cluster}`"
        subject_plain = f"{target}{extra_suffix} on {cluster}"
    elif target:
        subject_md = f"**{target}**{extra_suffix}"
        subject_plain = f"{target}{extra_suffix}"
    elif cluster:
        subject_md = f"**{cluster}**{extra_suffix}"
        subject_plain = f"{cluster}{extra_suffix}"
    else:
        subject_md = EXTRA_ONLY.format(extra=extra) if extra else ""
        subject_plain = subject_md

    card_text = f"{subject_md} — {summary}" if (subject_md and summary) else (subject_md or summary)
    plain_head = f"{subject_plain} — {summary}" if (subject_plain and summary) else (subject_plain or summary)

    action = clean_action(str((top.get("recommendation") or {}).get("action") or ""))
    card_row: dict = {"severity": severity, "text": card_text}
    if action:
        card_row["detail"] = action

    plain_lead = _ensure_period(plain_head) if action else plain_head
    plain_line = f"- {severity} · {plain_lead} {action}".strip() if action else f"- {severity} · {plain_lead}"

    member_ids = ",".join(str(m.get("id") or "") for m in item.members if m.get("id"))
    raw_where = "/".join(
        part
        for part in (
            str(top.get("project") or "").strip(),
            raw_cluster,
            str(top.get("namespace") or "").strip(),
            obj,
        )
        if part
    )
    unreported = max(0, item.covers - len(item.members))
    metadata_line = METADATA_LINE.format(
        index=index,
        role=role,
        id=top.get("id"),
        members=member_ids,
        location=raw_where or METADATA_NO_LOCATION,
        covers=item.covers,
        unreported=unreported,
    )
    return FormattedItem(
        role=role,
        severity=severity,
        cluster=cluster,
        card_row=card_row,
        plain_line=plain_line,
        reply_summary=slack_presenter._clip(plain_head, REPLY_SUMMARY_MAX),
        metadata_line=metadata_line,
    )


def _is_upgrade_item(item) -> bool:
    for member in item.members:
        check = str(member.get("check_slug") or member.get("check") or "").lower()
        if check.startswith(UPGRADE_CHECK_PREFIX):
            return True
    return False


def _headline(added_critical: list, reminded: list, added_other: list, blocking: list) -> str:
    crit_items = added_critical + reminded
    if crit_items:
        count = len(crit_items)
        if all(_is_upgrade_item(item) for item in crit_items):
            template = HEADLINE_UPGRADE_CRITICAL[count != 1]
        elif added_critical and not reminded:
            template = HEADLINE_NEW_CRITICAL[count != 1]
        else:
            template = HEADLINE_OPEN_CRITICAL[count != 1]
        return template.format(count=count)
    if added_other:
        count = len(added_other)
        template = (
            HEADLINE_UPGRADE_OTHER[count != 1]
            if all(_is_upgrade_item(item) for item in added_other)
            else HEADLINE_NEW_OTHER[count != 1]
        )
        return template.format(count=count)
    count = len(blocking)
    return HEADLINE_BLOCKING[count != 1].format(count=count)


def _note(open_actionable: int, surfaced_count: int, clusters: list[str]) -> str:
    unique = list(dict.fromkeys(c for c in clusters if c))
    if open_actionable > surfaced_count:
        if unique:
            return NOTE_OPEN_ACROSS.format(
                open_count=open_actionable,
                clusters=_plural(len(unique), *CLUSTER_NOUN),
            )
        return NOTE_OPEN_ONLY.format(open_count=open_actionable)
    if len(unique) == 1:
        return NOTE_SINGLE_CLUSTER.format(cluster=unique[0])
    if len(unique) > 1:
        return NOTE_MULTI_CLUSTER.format(clusters=_plural(len(unique), *CLUSTER_NOUN))
    return ""


def _waiting_phrase(waiting_items: list, blocking_count: int) -> str:
    if not waiting_items:
        return ""
    critical_class = fq.ITEM_CLASSES[0]
    waiting_critical = sum(1 for item in waiting_items if item.item_class == critical_class)
    waiting_other = len(waiting_items) - waiting_critical
    parts = []
    if waiting_critical:
        parts.append(_plural(waiting_critical, *WAITING_CRITICAL))
    if waiting_other:
        parts.append(_plural(waiting_other, *(WAITING_OTHER if waiting_critical else WAITING_PLAIN)))
    pronoun = WAITING_PRONOUNS[blocking_count != 1]
    return WAITING_SUFFIX.format(waiting=WAITING_JOINER.join(parts), pronoun=pronoun)


def _queue_fold(
    rows: list[dict],
    surfaced_keys: set[tuple],
    ambiguous: frozenset[str],
    remaining_count: int,
) -> tuple[dict | None, int]:
    """Build the cluster breakdown table for the rest of the queue and return ``(fold, remaining_critical)``."""
    grouped_items: dict[tuple, list[dict]] = {}
    for row in rows:
        if fq.rolled_up(row):
            continue
        key = fq.item_key(row)
        if key in surfaced_keys:
            continue
        grouped_items.setdefault(key, []).append(row)

    if not grouped_items:
        return None, 0

    remaining_critical = 0
    by_cluster: dict[str, dict[str, int | str]] = {}
    for members in grouped_items.values():
        sev = _best_severity(members)
        if sev == CRITICAL_SEVERITY:
            remaining_critical += 1
        top = members[0]
        cluster = cluster_label(top, ambiguous) or UNSCOPED_CLUSTER
        bucket = by_cluster.setdefault(
            cluster,
            {FOLD_CRITICAL: 0, FOLD_MAJOR: 0, FOLD_MINOR: 0, FOLD_NEXT: ""},
        )
        if sev == CRITICAL_SEVERITY:
            bucket[FOLD_CRITICAL] = int(bucket[FOLD_CRITICAL]) + 1
        elif sev == MAJOR_SEVERITY:
            bucket[FOLD_MAJOR] = int(bucket[FOLD_MAJOR]) + 1
        else:
            bucket[FOLD_MINOR] = int(bucket[FOLD_MINOR]) + 1
        if not bucket[FOLD_NEXT]:
            target = _target_name(top)
            cleaned = _peel_redundant_subject(
                clean_title(str(top.get("title") or "")),
                target,
                str(top.get("object") or "").strip(),
                str(top.get("cluster") or "").strip(),
            )
            preview = (
                FOLD_PREVIEW.format(target=target, summary=cleaned) if (target and cleaned) else (target or cleaned)
            )
            bucket[FOLD_NEXT] = slack_presenter._clip(preview, FOLD_NEXT_ISSUE_MAX)

    sorted_clusters = sorted(
        by_cluster.items(),
        key=lambda kv: (-int(kv[1][FOLD_CRITICAL]), -int(kv[1][FOLD_MAJOR]), -int(kv[1][FOLD_MINOR]), kv[0]),
    )[:MAX_FOLD_CLUSTERS]

    table_rows = [
        [
            cluster_name,
            str(stats[FOLD_CRITICAL]),
            str(stats[FOLD_MAJOR]),
            str(stats[FOLD_MINOR]),
            str(stats[FOLD_NEXT]),
        ]
        for cluster_name, stats in sorted_clusters
    ]
    title = (
        FOLD_TITLE_CUT_TEMPLATE.format(shown=len(sorted_clusters))
        if len(by_cluster) > len(sorted_clusters)
        else FOLD_TITLE_TEMPLATE.format(count=remaining_count)
    )
    fold = {
        nudge_card.FOLD_TITLE_KEY: slack_presenter._clip(title, chat_message.FOLD_TITLE_MAX),
        nudge_card.FOLD_TABLE_KEY: {
            nudge_card.TABLE_COLUMNS_KEY: list(FOLD_COLUMNS),
            nudge_card.TABLE_ROWS_KEY: table_rows,
        },
    }
    return fold, remaining_critical


def has_items(plan: fq.PacingPlan, daily: bool) -> bool:
    """Return True when ``plan`` has at least one finding to surface on this run."""
    return bool(plan.add or (daily and (plan.remind or plan.blocking)))


def build_nudge(
    plan: fq.PacingPlan,
    open_count: int,
    daily: bool,
    rows: list[dict] | None = None,
    backlog_url: str = "",
) -> NudgeBundle:
    """Build both the plain-text fallback and the structured card for ``plan``."""
    critical_class = fq.ITEM_CLASSES[0]
    added_critical = [item for item in plan.add if item.item_class == critical_class]
    added_other = [item for item in plan.add if item.item_class != critical_class]
    reminded = list(plan.remind) if daily else []
    blocking = list(plan.blocking) if daily else []

    tagged: list[tuple[str, object]] = (
        [(ROLE_NEW, item) for item in added_critical]
        + [(ROLE_REMINDER, item) for item in reminded]
        + [(ROLE_NEW, item) for item in added_other]
        + [(ROLE_BLOCKING, item) for item in blocking]
    )
    if not tagged:
        return NudgeBundle(text="", card=None)

    all_rows = list(rows) if rows is not None else [member for _, item in tagged for member in item.members]
    ambiguous = ambiguous_clusters(all_rows)

    formatted = [
        format_item(idx, role, item, ambiguous=ambiguous)
        for idx, (role, item) in enumerate(tagged, start=1)
    ]
    surfaced_keys = {item.key for _, item in tagged}
    open_actionable = max(len(tagged), open_count - plan.rolled_up)
    remaining = max(0, open_actionable - len(tagged))

    all_clusters = [
        cluster_label(row, ambiguous)
        for row in all_rows
        if not fq.rolled_up(row) and cluster_label(row, ambiguous)
    ]
    headline = _headline(added_critical, reminded, added_other, blocking)
    note = _note(open_actionable, len(tagged), all_clusters)

    fold: dict | None = None
    remaining_critical = 0
    if remaining > 0 and rows is not None:
        fold, remaining_critical = _queue_fold(rows, surfaced_keys, ambiguous, remaining)

    after_lines: list[str] = []
    if blocking:
        waiting = _waiting_phrase(plan.waiting, len(blocking))
        if len(blocking) == 1:
            after_lines.append(BLOCKING_NOTICE_SINGLE.format(waiting=waiting))
        else:
            after_lines.append(BLOCKING_NOTICE_MULTI.format(count=len(blocking), waiting=waiting))
    if remaining > 0:
        more_words = _plural(remaining, *MORE_OPEN)
        lower = max(0, remaining - remaining_critical)
        if rows is not None and remaining_critical > 0 and lower:
            after_lines.append(OPEN_WITH_CRITICAL.format(more=more_words, critical=remaining_critical, lower=lower))
        elif rows is not None and remaining_critical > 0:
            after_lines.append(OPEN_ONLY_CRITICAL.format(more=more_words, critical=remaining_critical))
        else:
            after_lines.append(OPEN_PLAIN.format(more=more_words))
    if plan.rolled_up:
        if plan.rolled_up == 1:
            after_lines.append(ROLLED_UP_SINGLE)
        else:
            after_lines.append(ROLLED_UP_MULTI.format(count=plan.rolled_up))

    blocking_only = bool(blocking and not (added_critical or reminded or added_other))
    if blocking_only:
        primary_label = REVIEW_SINGLE_LABEL if len(formatted) == 1 else REVIEW_FIRST_LABEL
    else:
        primary_label = FIX_SINGLE_LABEL if len(formatted) == 1 else FIX_FIRST_LABEL

    choices = [primary_label]
    if remaining > 0:
        choices.append(SEE_ALL_LABEL.format(count=open_actionable))
    replies = {
        primary_label: CHOICE_REPLY_FORMAT.format(
            label=primary_label,
            summary=formatted[0].reply_summary,
        )
    }

    clean_backlog = backlog_url.strip() if backlog_url and backlog_url.strip().startswith(HTTP_SCHEMES) else ""
    links = [[BACKLOG_LINK_LABEL, clean_backlog]] if clean_backlog else []

    head = FALLBACK_HEAD.format(headline=headline)
    head_line = f"{head} {note}" if note else head
    text_sections = [head_line, "\n".join(entry.plain_line for entry in formatted)]
    tail_lines = list(after_lines)
    if clean_backlog:
        tail_lines.append(BACKLOG_TEXT_LINE.format(url=clean_backlog))
    if tail_lines:
        text_sections.append("\n".join(tail_lines))
    fallback_text = "\n\n".join(text_sections)

    thread_context = (
        f"{fallback_text}\n\n{METADATA_HEADER}\n"
        + "\n".join(entry.metadata_line for entry in formatted)
    )
    tone = (
        chat_message.TONE_CRITICAL
        if any(entry.severity == CRITICAL_SEVERITY for entry in formatted)
        else chat_message.TONE_ATTENTION
    )
    card = {
        nudge_card.VERSION_KEY: nudge_card.CARD_SCHEMA_VERSION,
        nudge_card.TEXT_KEY: fallback_text,
        nudge_card.THREAD_CONTEXT_KEY: thread_context,
        nudge_card.HEADLINE_KEY: headline,
        nudge_card.NOTE_KEY: note,
        nudge_card.TONE_KEY: tone,
        nudge_card.ROWS_KEY: [entry.card_row for entry in formatted],
        nudge_card.AFTER_KEY: after_lines,
        nudge_card.FOLD_KEY: fold,
        nudge_card.CHOICES_KEY: choices,
        nudge_card.REPLIES_KEY: replies,
        nudge_card.LINKS_KEY: links,
    }
    return NudgeBundle(text=fallback_text, card=card)


def compose(
    plan: fq.PacingPlan,
    open_count: int,
    daily: bool,
    rows: list[dict] | None = None,
    backlog_url: str = "",
) -> str:
    """Return the plain-text nudge message, or "" when there is nothing to say."""
    return build_nudge(plan, open_count, daily, rows=rows, backlog_url=backlog_url).text


def _backlog_url(endpoint: str) -> str:
    """Best-effort read of the published backlog URL; empty when unpublished or unreachable."""
    try:
        pub = _request(endpoint, BACKLOG_PUBLICATION_PATH)
    except Exception:
        return ""
    if not isinstance(pub, dict):
        return ""
    ref = str(pub.get(TARGET_REF_KEY) or "").strip()
    return ref if ref.startswith(HTTP_SCHEMES) else ""


def _report_failure(home: Path, state: dict, today: str) -> int:
    """Exit 1 once a UTC day, so an outage is one failure post rather than one an hour."""
    if state.get(FAILURE_KEY) == today:
        sys.stderr.write(f"{LOG_PREFIX}: already reported a failure today; not posting another\n")
        return 0
    state[FAILURE_KEY] = today
    _save_state(home, state)
    return 1


def _mark(endpoint: str, item, added_class: str = "", run: str = "") -> None:
    # After the message, and best-effort: a failure costs the bookkeeping, not
    # the message, which is already out. An addition that is not marked is
    # offered again on a later day.
    body = {"publisher": PUBLISHER}
    if added_class:
        body["added_class"] = added_class
        body["run"] = run
    for row in item.members:
        try:
            _request(endpoint, f"/v1/findings/{urllib.parse.quote(str(row['id']), safe='')}/surfaced", body)
        except Exception as exc:  # one bad mark must not skip the rest
            sys.stderr.write(f"{LOG_PREFIX}: could not mark {row.get('id')} surfaced: {exc}\n")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def main(argv: list[str] | None = None, clock=_now) -> int:
    now = clock().astimezone(timezone.utc)
    today = now.date().isoformat()
    endpoint = (os.environ.get("SESSION_KV_ENDPOINT") or DEFAULT_ENDPOINT).rstrip("/")
    gateway_home, home = _homes()
    state = _load_state(home)

    # Before the read, so a snooze that lapsed since the last run is back on
    # the list this run reads. Best-effort: a failed expiry costs an hour of
    # lateness for snoozed rows, not the message.
    try:
        _request(endpoint, "/v1/findings/expire-snoozes", {})
    except Exception as exc:
        sys.stderr.write(f"{LOG_PREFIX}: could not expire lapsed snoozes: {exc}\n")

    try:
        rows = _request(endpoint, "/v1/findings/ranked").get("findings") or []
        added_today = _request(endpoint, f"/v1/findings/additions?day={today}")
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError, AttributeError) as exc:
        detail = exc.read().decode("utf-8", "replace") if isinstance(exc, urllib.error.HTTPError) else str(exc)
        # Nothing on stdout: a run that could not read the queue must not read
        # as a quiet hour.
        sys.stderr.write(f"{LOG_PREFIX}: could not read the queue at {endpoint}: {detail}\n")
        return _report_failure(home, state, today)

    # Anything else that breaks before the message is out is also one failure
    # post a day, not one an hour.
    try:
        held, why = first_report_hold(gateway_home, now)
        if why:
            sys.stderr.write(why + "\n")
        announced = state.get(ANNOUNCED_KEY) if isinstance(state.get(ANNOUNCED_KEY), dict) else {}
        todays = announced if announced.get(ANNOUNCED_DAY) == today else {}
        keys = todays.get(ANNOUNCED_KEYS)
        announced_keys = keys if isinstance(keys, list) and all(isinstance(key, list) for key in keys) else []
        if not todays:
            announced_ids = []
        elif isinstance(todays.get(ANNOUNCED_IDS), list):
            announced_ids = todays[ANNOUNCED_IDS]
        else:
            announced_ids = None
        plan = fq.pace(
            rows, now, fq.pacing_limits(os.environ), added_today,
            may_add=not held, announced=announced_keys, announced_ids=announced_ids,
        )
        daily = now.hour >= fq.REMIND_HOUR and state.get(DAILY_KEY) != today
        open_count = len({fq.item_key(row) for row in rows if not fq.rolled_up(row)}) + plan.rolled_up
        backlog_url = _backlog_url(endpoint) if has_items(plan, daily) else ""
        bundle = build_nudge(plan, open_count, daily, rows=rows, backlog_url=backlog_url)
    except Exception:
        sys.stderr.write(f"{LOG_PREFIX}: failed before posting:\n{traceback.format_exc()}")
        return _report_failure(home, state, today)

    nudge_card.save(nudge_card.card_path(home), bundle.card)
    if bundle.text:
        sys.stdout.write(bundle.text + "\n")
        sys.stdout.flush()

    # Before the marks, so a mark that raises cannot cost the record that the
    # message went out.
    if plan.add:
        record = {ANNOUNCED_DAY: today, ANNOUNCED_KEYS: announced_keys + [list(item.key) for item in plan.add]}
        if announced_ids is not None:
            record[ANNOUNCED_IDS] = announced_ids + [str(member.get("id")) for item in plan.add for member in item.members]
        state[ANNOUNCED_KEY] = record
    if daily:
        state[DAILY_KEY] = today
    if plan.add or daily:
        _save_state(home, state)

    run = now.strftime(RUN_FORMAT)
    for item in plan.add:
        _mark(endpoint, item, item.item_class, run)
    if daily:
        # Marking a reminded or blocking item also shows any member that joined
        # it since, so that member is pending with the rest of its line.
        for item in plan.remind + plan.blocking:
            _mark(endpoint, item)
    return 0


if __name__ == "__main__":
    sys.exit(main())
