"""Request normalization and fingerprinting for the shadow-mode replay proxy.

The rules are the "Normalization" and "What goes into the cache key" sections
of examples/inference-replay/eval-design.md. Every identifier that differs
between otherwise identical runs becomes a placeholder numbered by first
appearance in the request (`⟦TASK_1⟧`), so two runs that differ only in IDs
produce the same fingerprint, while state (counts, statuses, CPU and memory
quantities, versions) stays literal.

NORMALIZER_VERSION is a hash of this file's bytes: any change to a rule here
changes every fingerprint, so a fingerprint recorded by one normalizer never
matches one computed by another. Keep rules in this file for that reason.
"""

from __future__ import annotations

import bisect
import calendar
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Hex digits of the normalizer source hash kept in NORMALIZER_VERSION.
NORMALIZER_VERSION_HEX_CHARS = 16
NORMALIZER_VERSION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:NORMALIZER_VERSION_HEX_CHARS]

# Hex digits kept in a per-message hash: enough to make an accidental prefix
# collision across a week of logs negligible, short enough to keep a log line
# with a few hundred messages small.
MESSAGE_HASH_HEX_CHARS = 16

# Request fields that change how the answer is delivered or who asked, never
# what the model sees. Everything else is in the key.
KEY_DROP_FIELDS = ("stream", "user", "metadata")
MESSAGES_FIELD = "messages"

OPEN, CLOSE = "⟦", "⟧"

# Opaque fields. LiteLLM embeds Gemini's thought signature in tool-call ids
# (`call_…__thought__<base64>`), so no rule ever edits inside one: the whole
# value becomes one ⟦OPAQUE_n⟧, numbered by first appearance so a tool result's
# `tool_call_id` still names the same call as the assistant's `id`. The
# forwarded request is never modified; this is the key only.
OPAQUE_KEY_RE = re.compile(r"signature|provider_specific_fields", re.I)
OPAQUE_ALWAYS_KEYS = ("tool_call_id",)
# `id` is opaque only as the `id` of an item in a `tool_calls` list, whatever
# the provider's prefix (`call_…` from Gemini through LiteLLM, `toolu_…` from
# Claude). Any other string `id` -- a kanban card, a cluster, a resource --
# goes through the normal rules, so a meaningful value is never hidden behind
# a positional placeholder. A numeric `id` is a row id (below).
ID_KEY = "id"
TOOL_CALLS_KEY = "tool_calls"
OPAQUE_CLASS = "OPAQUE"

# Keys whose numeric value is a per-run counter or row id.
KEYED_NUMBER_KEYS = ("pid", "PID", "run_id", "id", "resourceVersion", "generation", "observedGeneration")
# Any key ending in this is a run id too (`current_run_id`, `parent_run_id`).
RUN_ID_SUFFIX = "_run_id"
# Kanban card history: heartbeat entries are liveness pings on a timer, so how
# many a card has depends on timing alone. They are left out of the key (the
# forwarded request is unchanged); every other event stays.
EVENTS_KEY = "events"
EVENT_KIND_KEY = "kind"
HEARTBEAT_KIND = "heartbeat"
NUMBER_CLASS = "NUM"
# A swapped number with at least this many digits counts as repeated wherever an
# answer has it as a whole token (`kill 4821`). A shorter one counts only next
# to its key (`generation: 1`, `run_id=190`, `pid 42`): a bare `1` in an answer
# (`2>&1`, "revision 1") is almost never the swapped value, and treating it as
# one marked about one answer in seven unstorable in the dev-install dry run.
MIN_STANDALONE_REPEAT_DIGITS = 4
SHORT_NUMBER_KEYS = r"(?:resourceVersion|observedGeneration|generation|run_id|run|pid|PID|id)"
SHORT_NUMBER_KEY_GAP = r"(?:\s+(?:is|was|now|of)\s+|\W{0,4})"
DIGITS_RE = re.compile(r"^\d+$")
KEYED_NUMBER_TEXT_RE = re.compile(
    r"(?<![A-Za-z0-9_])(resourceVersion|observedGeneration|generation|[A-Za-z0-9]*_?run_id|pid|PID|id)"
    r"(\\?[\"']?\s*[:=]\s*\\?[\"']?)(\d+)\b"
)
# `pid 4821`, as a process listing or a sentence names one.
PID_PROSE_RE = re.compile(r"(?<![A-Za-z0-9_])(pid|PID)(\s+)(\d+)\b")

# Ages: kubectl's human durations (`45s`, `5m`, `3h15m`, `2d3h`, `2y`).
AGE_SHAPE = r"\d+[smhdy](?:\d+[smhd])?"
AGE_FULL_RE = re.compile(r"^" + AGE_SHAPE + r"$")
AGE_TOKEN_RE = re.compile(r"(?<![\w.])" + AGE_SHAPE + r"(?![\w.])")
AGE_CLASS = "AGE"
# An age is only an age in one of these contexts; `120m` CPU never is.
AGE_KEYS = ("age", "lastSeen", "last_seen")
AGE_FIELD_RE = re.compile(
    r"(?<![A-Za-z0-9_])(age|Age|AGE|lastSeen|last_seen)(\\?[\"']?\s*[:=]\s*\\?[\"']?)(" + AGE_SHAPE + r")(?![\w.])"
)
AGE_AGO_RE = re.compile(r"(?<![\w.])(" + AGE_SHAPE + r")(?=\s+ago\b)")
AGE_WORDS_AGO_RE = re.compile(
    r"(?<![\w.])(\d+)(\s+)(seconds?|minutes?|hours?|days?|weeks?|months?|years?)(?=\s+ago\b)"
)
AGE_WORD_UNITS = {"second": "s", "minute": "m", "hour": "h", "day": "d", "week": "w", "month": "mo", "year": "y"}
# Table columns that hold an age: kubectl's `AGE` and `LAST SEEN`, and the
# `Age` column of `kubectl describe`'s Events table.
AGE_COLUMN_NAMES = ("AGE", "LAST SEEN", "Age")
# A header cell is separated from the next by two or more spaces (kubectl's
# tabwriter pads with at least three), so `LAST SEEN` is one cell.
HEADER_CELL_RE = re.compile(r"\S+(?: \S+)*")
UPPER_HEADER_RE = re.compile(r"^[ A-Z0-9()_/.%-]+$")
DESCRIBE_EVENTS_HEADER_RE = re.compile(r"^\s*Type\s+Reason\s+Age\s+From\s+Message\s*$")
DASH_RULE_RE = re.compile(r"^[\s-]+$")

# Timestamps become how far they are from the request time, unnumbered:
# whether a certificate expired is what matters, not which second it did.
TIME_CLASS = "TIME"
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400
DAYS_PER_MONTH_BUCKET = 30
DAYS_PER_YEAR_BUCKET = 365
TIME_BUCKETS = (
    (SECONDS_PER_DAY, "hours"),
    (SECONDS_PER_DAY * DAYS_PER_MONTH_BUCKET, "days"),
    (SECONDS_PER_DAY * DAYS_PER_YEAR_BUCKET, "months"),
)
TIME_BUCKET_BEYOND = "years"
TIME_WITHIN_HOUR = "within_hour"
TIME_PAST_PREFIX = "past_"
TIME_FUTURE_PREFIX = "future_"
UTC_ZONE = "Z"
ZONE_HOUR_DIGITS = 2
ZONE_NEGATIVE = "-"
ISO_TS_RE = re.compile(
    r"(?<![\w.])(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?(?![\w:])"
)
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
MONTH_ALT = "|".join(MONTHS)
DAY_ALT = "Mon|Tue|Wed|Thu|Fri|Sat|Sun"
# `Mon, 02 Nov 2026 04:13:22 +0000` (kubectl describe, HTTP dates).
RFC1123_RE = re.compile(
    r"\b(?:" + DAY_ALT + r"), \d{1,2} (?:" + MONTH_ALT + r") \d{4} \d{2}:\d{2}:\d{2} (?:GMT|UTC|[+-]\d{4})"
)
# `Nov  2 04:13:22 2027 GMT` (openssl x509 -dates).
OPENSSL_TS_RE = re.compile(r"\b(" + MONTH_ALT + r") {1,2}(\d{1,2}) (\d{2}):(\d{2}):(\d{2}) (\d{4})(?: GMT)?\b")
# `Nov  2 04:13` and `Nov  2  2025` (ls -l).
LS_DATE_RE = re.compile(r"\b(" + MONTH_ALT + r") {1,2}(\d{1,2}) {1,2}(?:(\d{2}):(\d{2})|(\d{4}))\b")
# `Thursday, November 05, 2026 (UTC, UTC+00:00)`: the date Hermes writes into
# the system prompt as "Conversation started". It has no time of day, so it
# becomes how many calendar days it is from the request's own day (in the
# offset after it, when one is given, else UTC): a nightly recorded on
# Wednesday and a presubmit on Thursday both read `today`. The weekday name is
# dropped with it; the parenthesised zone (Hermes writes zone names, then
# `UTC±HH:MM`) stays as written. A date-only ISO value and an `ls -l` date
# with a year are day-counted the same way. Any of them with a time of day
# beside it (TimeIndex.near, Normalizer.timed) is left alone: day counting would
# lose its time.
LONG_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
               "October", "November", "December")
LONG_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
LONG_DATE_RE = re.compile(
    r"\b(?:(?:" + "|".join(LONG_DAYS) + r"),\s+)?(" + "|".join(LONG_MONTHS) + r")\s+(\d{1,2}),\s+(\d{4})\b"
    r"(?=\s*\((?:[\w/+-]+,\s*)*UTC([+-])(\d{2}):(\d{2})\))?"
)
DATE_TODAY = "today"
# A time of day (`10:42`, `10:42:01`, `10 PM`, `10pm`, `10h42`) beside a date
# keeps the date as written, since day counting would drop what the time says:
# leaving a date as written only costs a match, while day-counting one with a
# time gives two moments a day apart one key. A time already inside a
# timestamp a rule bucketed is not read: Normalizer.bucket_times replaces those
# before any date is day-counted, and the bucket keeps their hour. A time in a
# timestamp no rule buckets (`2026-02-30 10:42`) still counts. Not a time of
# day: digits with a word character or `:` right before them (`110:42`, the
# `42:01` of `10:42:01`), Hermes's `UTC+00:00` offset, or a colon form running
# into more digits (`80:8080`). An escaped `\n`, `\r` or `\t` before one is a
# boundary, not the word character `n`: `...\n10:42:01` in unparsed JSON has a
# time. A `-` before one does not stop it either: `2026-11-05-10:42` has a
# time. Dotted and compact shapes (`10.42.01`, `1042Z`) are not read: they
# collide with versions (`v1.31.2`).
TIME_OF_DAY_RE = re.compile(
    r"(?:(?<=\\n)|(?<=\\r)|(?<=\\t)|(?<![\w:]))(?<!UTC[+-])"
    r"\d{1,2}(?::\d{2}(?::\d{2})?(?!\d)|\s?[AaPp]\.?[Mm]\b|h\d{2}\b)"
)
# How far, across line breaks, a time of day reaches a date that is not on its
# line (TimeIndex.near); a time anywhere on the date's own line always does.
TIME_NEAR_DATE_CHARS = 30
# Line breaks, real and JSON-escaped (a tool result can carry `\n` as text).
LINE_BREAK_RE = re.compile(r"\n|\\n")
# The shapes an answer may name a date in, read case-insensitively and
# compared as dates (Mapping.answer_repeats_date), so a new spelling is a new
# shape here, not a missed string. A letter or digit touching a shape stops
# it, so `notes_2026-11-04.md` is read and `2026-11-04T10:00:00Z` (a
# timestamp, with its time) is not.
SHORT_MONTHS = tuple(m[:3] for m in LONG_MONTHS)
SEPT_SPELLING = "sept"
ANSWER_MONTH = r"(" + "|".join(sorted(LONG_MONTHS + SHORT_MONTHS + (SEPT_SPELLING,), key=len, reverse=True)) + r")"
ANSWER_BOUND_BEFORE = r"(?<![A-Za-z0-9])"
ANSWER_BOUND_AFTER = r"(?![A-Za-z0-9])"
ANSWER_GAP = r"[\s._-]*"
ANSWER_ORDINAL = r"(?:st|nd|rd|th)?"
ANSWER_YEAR = r"(?:,?" + ANSWER_GAP + r"(\d{4}))?"
# 2026-11-04, 2026/11/04, 2026.11.04, 2026_11_04, 20261104.
ANSWER_ISO_RE = re.compile(ANSWER_BOUND_BEFORE + r"(\d{4})([-/._]?)(\d{2})\2(\d{2})" + ANSWER_BOUND_AFTER)
# 11/04/2026, 11-04-2026, 04.11.2026: read both ways round.
ANSWER_NUMERIC_RE = re.compile(ANSWER_BOUND_BEFORE + r"(\d{1,2})([/.-])(\d{1,2})\2(\d{4})" + ANSWER_BOUND_AFTER)
# November 4, Nov. 04 2026, nov-04, November 4th.
ANSWER_MONTH_FIRST_RE = re.compile(
    ANSWER_BOUND_BEFORE + ANSWER_MONTH + r"\.?" + ANSWER_GAP + r"(\d{1,2})" + ANSWER_ORDINAL + ANSWER_YEAR
    + ANSWER_BOUND_AFTER, re.I)
# 4 November 2026, 4th of Nov.
ANSWER_DAY_FIRST_RE = re.compile(
    ANSWER_BOUND_BEFORE + r"(\d{1,2})" + ANSWER_ORDINAL + ANSWER_GAP + r"(?:of\s+)?" + ANSWER_MONTH + r"\.?"
    + ANSWER_YEAR + ANSWER_BOUND_AFTER, re.I)
ANSWER_WEEKDAY_RE = re.compile(ANSWER_BOUND_BEFORE + r"(" + "|".join(LONG_DAYS) + r")" + ANSWER_BOUND_AFTER, re.I)
# Fewer whole days from the request's day than this is "day" (one day either way).
DAYS_IN_DAY_BUCKET = 2
# Whole days from the request's day -> bucket: under each limit, with the
# same month and year edges as TIME_BUCKETS, plus "day" for exactly one.
DAY_BUCKETS = (
    (DAYS_IN_DAY_BUCKET, "day"),
    (DAYS_PER_MONTH_BUCKET, "days"),
    (DAYS_PER_YEAR_BUCKET, "months"),
)
# Epoch seconds 2020-2033, and the same in milliseconds, bucketed only under a
# key that names a time: a free-text 10-digit number (`memory: 1717986918
# bytes`) stays literal.
EPOCH_FULL_RE = re.compile(r"^(1[6-9]\d{8})(\d{3})?(?:\.\d+)?$")
TIME_KEY_PATTERN = (
    r"(?:[A-Za-z0-9_]*_at|[A-Za-z0-9]*[a-z](?:At|Time|Timestamp|Date)|time|timestamp|ts|date"
    r"|expires|expiry|expiration|deadline"
    r"|[A-Za-z0-9_]*_(?:time|timestamp|ts|date))"
)
TIME_KEY_RE = re.compile(r"^" + TIME_KEY_PATTERN + r"$")
KEYED_EPOCH_TEXT_RE = re.compile(
    r"(?<![A-Za-z0-9_])(" + TIME_KEY_PATTERN + r")(\\?[\"']?\s*[:=]\s*\\?[\"']?)(1[6-9]\d{8}(?:\d{3})?(?:\.\d+)?)(?![\w.])"
)
MILLIS_PER_SECOND = 1000

# IPv4 addresses, kept literal whenever the request names a CIDR range.
IP_CLASS = "IP"
IPV4_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.]|/\d)")
CIDR_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}/\d{1,2}(?!\d)")
IPV4_OCTET_MAX = 255

# Kubernetes' random-suffix alphabet (k8s.io/apimachinery rand.SafeEncodeString).
K8S_SAFE = "bcdfghjklmnpqrstvwxz2456789"

# (class, pattern, group): identifier classes in the order they are applied.
# Longer, more specific shapes go first. `group` is the part replaced; the
# rest of the match stays literal (a pod keeps its workload name).
ID_CLASSES: List[Tuple[str, "re.Pattern[str]", int]] = [
    ("UID", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), 0),
    # Hermes session id: 20261102_002640_791a68.
    ("SESSION", re.compile(r"\b\d{8}_\d{6}_[0-9a-f]{4,}\b"), 0),
    # Hermes terminal-output cache file: out-<epoch>-<pid>-<4 hex>.log.
    ("TERMOUT", re.compile(r"\bout-(\d{9,11}-\d+-[0-9a-f]{4})\.log\b"), 1),
    # Scratch file suffix _<epoch>_<pid> before an extension.
    ("SCRATCH", re.compile(r"(_1[6-9]\d{8}_\d+)(?=\.\w+\b)"), 1),
    ("TASK", re.compile(r"\bt_[0-9a-f]{8}\b"), 0),
    ("A2ATASK", re.compile(r"\btask-[0-9a-f]{16}\b"), 0),
    ("INJ", re.compile(r"\binj-\d+\b"), 0),
    # GKE node: gke-<cluster>-<pool>-<8 hex>-<4>; the cluster and pool stay.
    ("NODE", re.compile(r"\bgke-[a-z0-9-]+?-([0-9a-f]{8}-[a-z0-9]{4})\b"), 1),
    # Pod of a ReplicaSet: <workload>-<template hash>-<5>; the workload stays.
    ("POD", re.compile(r"\b[a-z0-9](?:[a-z0-9-]*[a-z0-9])?-([" + K8S_SAFE + r"]{6,10}-[" + K8S_SAFE + r"]{5})\b"), 1),
]

# Hermes appends `## Recent work by @<assignee>` (the assignee's last runs on
# other cards, one `- ` line each, then a blank line) to every card view. It
# differs between otherwise identical runs, so it is dropped from the key
# (#1730). Matched with real and JSON-escaped newlines.
RECENT_WORK_RE = re.compile(r"(?m)^## Recent work by @[^\n]*\n(?:- [^\n]*(?:\n|$))*")
RECENT_WORK_ESCAPED_RE = re.compile(r"## Recent work by @(?:(?!\\n).)*(?:\\n- (?:(?!\\n).)*)*")
RECENT_WORK_STRIPPED = "## Recent work [stripped]\n"
RECENT_WORK_ESCAPED_STRIPPED = "## Recent work [stripped]"

# A string that may hold a JSON document (a tool result usually does).
JSON_OPENERS = "{["
# Hash input encoding. A lone surrogate (`"\ud83d"`) is valid in a JSON string
# but not in UTF-8, so it is passed through rather than raising.
TEXT_ENCODING = "utf-8"
ENCODING_ERRORS = "surrogatepass"
# Answer rewriting: a value is swapped only where it is a whole token, so the
# characters that may border a token are excluded on each side.
TOKEN_BEFORE = r"(?<![A-Za-z0-9+/=_-])"
TOKEN_AFTER = r"(?![A-Za-z0-9+/=_-])"
# A bare number is a token when no word character, `.` or `-` precedes it and
# no word character or `.`+digit follows it: `1` in `1.31`, `node-pool-1` or
# `--replicas=-1` is not the number 1, but in `generation is 1.` it is.
NUMBER_BEFORE = r"(?<![\w.-])"
NUMBER_AFTER = r"(?!\w|\.\d)"
DIGIT_RE = re.compile(r"\d")
# Joins a mapping key's class scope and its value.
MAPPING_KEY_SEPARATOR = "\0"


def stable_hash(value: Any) -> str:
    """SHA-256 of a value's canonical JSON."""
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode(TEXT_ENCODING, ENCODING_ERRORS)).hexdigest()


def time_bucket(when: float, now: float) -> str:
    """The placeholder for a timestamp: how far from `now` it is, and which way."""
    delta = now - when
    distance = abs(delta)
    if distance < SECONDS_PER_HOUR:
        # One token for the hour on each side of `now`: run-to-run timing jitter (a
        # card read 50 s or 70 s after it was claimed) must not flip the key, and the
        # dev-install dry run lost most of its matches there. The side stays, so a
        # certificate that expired 20 minutes ago never matches one valid for 20 more.
        span = TIME_WITHIN_HOUR
    else:
        span = next((label for limit, label in TIME_BUCKETS if distance < limit), TIME_BUCKET_BEYOND)
    name = (TIME_PAST_PREFIX if delta >= 0 else TIME_FUTURE_PREFIX) + span
    return f"{OPEN}{TIME_CLASS}_{name}{CLOSE}"


@dataclass(frozen=True)
class TimeIndex:
    """Where a string's times of day and line breaks are, found in one scan of the whole string.

    The scan runs over the whole string, never a slice, so the lookbehinds
    see what is before any window. Each date's question is then two bisects,
    so a long line with thousands of dates costs its length once, not once
    per date.
    """

    time_starts: List[int]
    time_ends: List[int]
    break_starts: List[int]
    break_ends: List[int]
    length: int

    @classmethod
    def of(cls, text: str) -> "TimeIndex":
        times = [m.span() for m in TIME_OF_DAY_RE.finditer(text)]
        breaks = [m.span() for m in LINE_BREAK_RE.finditer(text)]
        return cls([a for a, _ in times], [b for _, b in times], [a for a, _ in breaks], [b for _, b in breaks],
                   len(text))

    def near(self, start: int, end: int) -> bool:
        """Whether a time of day is on the line of [start, end), or overlaps TIME_NEAR_DATE_CHARS either side of it."""
        before = bisect.bisect_right(self.break_ends, start) - 1
        line_start = self.break_ends[before] if before >= 0 else 0
        after = bisect.bisect_left(self.break_starts, end)
        line_end = self.break_starts[after] if after < len(self.break_starts) else self.length
        # Matches never overlap, so their ends are as sorted as their starts.
        first_on_line = bisect.bisect_left(self.time_starts, line_start)
        if first_on_line < len(self.time_starts) and self.time_ends[first_on_line] <= line_end:
            return True
        first_past_window_start = bisect.bisect_right(self.time_ends, start - TIME_NEAR_DATE_CHARS)
        return (first_past_window_start < len(self.time_starts)
                and self.time_starts[first_past_window_start] < end + TIME_NEAR_DATE_CHARS)


def answer_dates(text: str) -> List[Tuple[Optional[int], int, int]]:
    """Every (year or None, month, day) an answer names in one of the ANSWER_* shapes."""
    found: List[Tuple[Optional[int], int, int]] = []
    for m in ANSWER_ISO_RE.finditer(text):
        found.append((int(m.group(1)), int(m.group(3)), int(m.group(4))))
    for m in ANSWER_NUMERIC_RE.finditer(text):
        first, second, year = int(m.group(1)), int(m.group(3)), int(m.group(4))
        found += [(year, first, second), (year, second, first)]
    for m in ANSWER_MONTH_FIRST_RE.finditer(text):
        found.append((int(m.group(3)) if m.group(3) else None, month_number(m.group(1)), int(m.group(2))))
    for m in ANSWER_DAY_FIRST_RE.finditer(text):
        found.append((int(m.group(3)) if m.group(3) else None, month_number(m.group(2)), int(m.group(1))))
    return found


def month_number(name: str) -> int:
    return [m.lower() for m in SHORT_MONTHS].index(name[:3].lower()) + 1


def day_bucket(days: int) -> str:
    """The placeholder for a date with no time of day: how many calendar days from the request's, and which way."""
    if days == 0:
        name = DATE_TODAY
    else:
        span = next((label for limit, label in DAY_BUCKETS if abs(days) < limit), TIME_BUCKET_BEYOND)
        name = (TIME_FUTURE_PREFIX if days > 0 else TIME_PAST_PREFIX) + span
    return f"{OPEN}{TIME_CLASS}_{name}{CLOSE}"


def key_timed(key: Any) -> bool:
    """Whether an object key holds a time of day. Keys go into the key as written, so a timestamp in one counts."""
    return isinstance(key, str) and bool(TIME_OF_DAY_RE.search(key))


def strip_recent_work(text: str) -> str:
    """Drop the `## Recent work` digest, which the key leaves out (RECENT_WORK_RE)."""
    text = RECENT_WORK_RE.sub(RECENT_WORK_STRIPPED, text)
    return RECENT_WORK_ESCAPED_RE.sub(RECENT_WORK_ESCAPED_STRIPPED, text)


@dataclass
class Mapping:
    """Value <-> placeholder, numbered per class by first appearance."""

    to_placeholder: Dict[str, str] = field(default_factory=dict)
    counts: Dict[str, int] = field(default_factory=dict)
    # Values swapped that can never be put back in an answer: bare numbers
    # (keyed numbers) and epoch timestamps, which become an unnumbered
    # how-far-from-now time token.
    bare_numbers: set = field(default_factory=set)
    # Dates swapped for a day-count token. The token can't be put back, so an
    # answer naming one is not saved: replayed on another day it would carry
    # the recording's. `today_dates` are the ones on the request's own day,
    # which an answer may also name without the year or by weekday.
    dates: set = field(default_factory=set)
    today_dates: set = field(default_factory=set)

    def add_date(self, when: "date", today: bool) -> None:
        self.dates.add(when)
        if today:
            self.today_dates.add(when)

    def placeholder(self, cls: str, value: str) -> str:
        key = (cls if cls in (OPAQUE_CLASS, NUMBER_CLASS) else "") + MAPPING_KEY_SEPARATOR + value
        if key not in self.to_placeholder:
            number = self.counts.get(cls, 0) + 1
            self.counts[cls] = number
            token = f"{OPEN}{cls}_{number}{CLOSE}"
            self.to_placeholder[key] = token
        if cls == NUMBER_CLASS:
            self.bare_numbers.add(value)
        return self.to_placeholder[key]

    def swappable_values(self) -> Dict[str, str]:
        """Values an answer may be rewritten with: not opaque, not bare numbers."""
        out = {}
        for key, token in self.to_placeholder.items():
            cls, value = key.split(MAPPING_KEY_SEPARATOR, 1)
            if cls:
                continue
            out[value] = token
        return out

    def apply_to_answer(self, text: str) -> str:
        """Swap bound values in an answer for their placeholders, by whole token only."""
        for value, token in sorted(self.swappable_values().items(), key=lambda kv: len(kv[0]), reverse=True):
            if value not in text:
                continue
            before = TOKEN_BEFORE if value[0].isalnum() else ""
            after = TOKEN_AFTER if value[-1].isalnum() else ""
            text = re.sub(before + re.escape(value) + after, lambda _m, t=token: t, text)
        return text

    def answer_repeats_date(self, text: str) -> bool:
        """Whether an answer names a date the request's key counts by days, which cannot be put back.

        A date with a year must be one the request held; one without a year,
        or a weekday, must be the request's own day's.
        """
        if not self.dates:
            return False
        month_days = {(d.month, d.day) for d in self.today_dates}
        weekdays = {LONG_DAYS[d.weekday()].lower() for d in self.today_dates}
        for year, month, day in answer_dates(text):
            if year is None:
                if (month, day) in month_days:
                    return True
                continue
            try:
                if date(year, month, day) in self.dates:
                    return True
            except ValueError:
                continue
        return any(m.group(1).lower() in weekdays for m in ANSWER_WEEKDAY_RE.finditer(text))

    def answer_repeats_bare_number(self, text: str) -> bool:
        """Whether an answer repeats a swapped value that cannot be restored."""
        for value in self.bare_numbers:
            if len(value) >= MIN_STANDALONE_REPEAT_DIGITS:
                pattern = NUMBER_BEFORE + re.escape(value) + NUMBER_AFTER
            else:
                pattern = r"(?<![A-Za-z0-9_])" + SHORT_NUMBER_KEYS + SHORT_NUMBER_KEY_GAP + re.escape(value) + NUMBER_AFTER
            if re.search(pattern, text):
                return True
        return False


def age_unit(value: str) -> str:
    return DIGIT_RE.sub("", value)[:1]


class Normalizer:
    """Normalizes one request. Holds the request time and the mapping."""

    def __init__(self, now: float, keep_ips: bool):
        self.now = now
        self.keep_ips = keep_ips
        self.mapping = Mapping()
        # Whether a value in any object or list enclosing the one being walked
        # holds a time of day (timed): then no date in it is day-counted.
        self.time_beside = False
        # Per-request caches: a string's parsed JSON document (nested_json),
        # whether a string or container holds a time of day (timed), and a
        # string's TimeIndex, so each is computed once however often it is asked.
        self.parsed: Dict[str, Any] = {}
        self.timed_strings: Dict[str, bool] = {}
        self.timed_containers: Dict[Tuple[int, Optional[str], bool], bool] = {}
        self.time_indexes: Dict[str, TimeIndex] = {}

    # -- structure ---------------------------------------------------------
    #
    # What the key keeps of a value is decided once, by `replacement` (a value
    # swapped whole for a token) and `kept_items` (the children walked, after
    # dropping heartbeats). Both `walk` and `timed` read through them, so a time
    # in something the key drops or replaces never decides a date.

    def walk(self, obj: Any, key: Optional[str] = None, in_tool_call: bool = False, is_tool_call: bool = False) -> Any:
        """Normalize `obj`, found under `key`; `in_tool_call` when its parent is a tool call, `is_tool_call` when it is one."""
        replace = self.replacement(obj, key, in_tool_call)
        if replace is not None:
            return replace()
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, (list, dict)):
            items = self.kept_items(obj, key, is_tool_call)
            beside = self.siblings_timed(items)
            walked = [self.beside(flag, lambda item=item: self.walk(*item)) for item, flag in zip(items, beside)]
            if isinstance(obj, dict):
                return {item[1]: value for item, value in zip(items, walked)}
            return walked
        return obj

    def replacement(self, obj: Any, key: Optional[str], in_tool_call: bool) -> Optional[Callable[[], Any]]:
        """What `walk` swaps `obj` for whole (a placeholder or token), as a call, or None when it walks into it."""
        if key is not None and self.is_opaque(key, in_tool_call):
            return lambda: self.opaque(obj)
        if key is not None and TIME_KEY_RE.match(key) and self.epoch_value(obj) is not None:
            return lambda: self.epoch_token(str(obj), self.epoch_value(obj))
        if (key in KEYED_NUMBER_KEYS or (key or "").endswith(RUN_ID_SUFFIX)) and self.is_number(obj):
            return lambda: self.keyed_number(str(obj))
        if key in AGE_KEYS and isinstance(obj, str) and AGE_FULL_RE.match(obj):
            return lambda: self.age(obj)
        return None

    @staticmethod
    def kept_items(obj: Any, key: Optional[str], is_tool_call: bool) -> List[Tuple[Any, Optional[str], bool, bool]]:
        """The children `walk` keeps, as its arguments: (value, key or None, in_tool_call, is_tool_call).

        A dict's keys come sorted; a kanban card's heartbeat events are dropped.
        """
        if isinstance(obj, dict):
            return [(obj[k], k, is_tool_call, False) for k in sorted(obj)]
        if key == EVENTS_KEY:
            obj = [v for v in obj if not (isinstance(v, dict) and v.get(EVENT_KIND_KEY) == HEARTBEAT_KIND)]
        return [(v, None, False, key == TOOL_CALLS_KEY and isinstance(v, dict)) for v in obj]

    def beside(self, flag: bool, walk: Callable[[], Any]) -> Any:
        """Run `walk` with time_beside also set when `flag` is, then restore the enclosing value.

        The flag is inherited: a time beside an enclosing value keeps every date
        under it as written (`{"when": {"date": ...}, "time": "10:42"}`).
        """
        outer = self.time_beside
        self.time_beside = outer or flag
        try:
            return walk()
        finally:
            self.time_beside = outer

    @staticmethod
    def is_opaque(key: str, in_tool_call: bool) -> bool:
        if OPAQUE_KEY_RE.search(key) or key in OPAQUE_ALWAYS_KEYS:
            return True
        return key == ID_KEY and in_tool_call

    @staticmethod
    def epoch_value(value: Any) -> Optional[float]:
        """Epoch seconds when `value` is an epoch in seconds or milliseconds, else None."""
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return None
        match = EPOCH_FULL_RE.match(str(value))
        return float(match.group(1)) if match else None

    @staticmethod
    def is_number(value: Any) -> bool:
        if isinstance(value, bool):
            return False
        return isinstance(value, int) or (isinstance(value, str) and bool(DIGITS_RE.match(value)))

    def keyed_number(self, value: str) -> str:
        """Swap a keyed number (any length) for a placeholder."""
        return self.mapping.placeholder(NUMBER_CLASS, value)

    def opaque(self, value: Any) -> str:
        whole = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
        return self.mapping.placeholder(OPAQUE_CLASS, whole)

    def age(self, value: str) -> str:
        return self.mapping.placeholder(AGE_CLASS + age_unit(value), value)

    # -- text --------------------------------------------------------------

    def siblings_timed(self, items: List[Tuple[Any, Optional[str], bool, bool]]) -> List[bool]:
        """For each of kept_items' children, whether a time of day is beside it: in another child, or in its own key.

        A date's own string is read by TimeIndex.near; this is for a date and
        its time in different JSON values or keys (`{"date": "2026-11-05",
        "time": "10:42:01"}`, or one level apart), which the walk normalizes one
        string at a time. Each request message is walked on its own
        (normalize_request), so a time in one message never reaches another.
        """
        own_keys = [key_timed(item[1]) for item in items]
        timed = [own_key or self.timed(*item) for item, own_key in zip(items, own_keys)]
        count = sum(timed)
        return [own_key or count - own > 0 for own, own_key in zip(timed, own_keys)]

    def timed(self, value: Any, key: Optional[str] = None, in_tool_call: bool = False, is_tool_call: bool = False) -> bool:
        """Whether a time of day is in what the key keeps of `value` (same arguments as walk).

        Reads every string and object key under it, a string as the JSON
        document it holds when it holds one, through the walk's own
        replacement and kept_items: a placeholder, a token or a dropped
        heartbeat holds no time. Times in timestamps a rule buckets do not count
        (bucket_times), nor do times in the recent-work digest the key strips.
        Cached per string and per container, so a subtree is read once.
        """
        if self.replacement(value, key, in_tool_call) is not None:
            return False
        if isinstance(value, str):
            if value not in self.timed_strings:
                nested = self.nested_json(value)
                self.timed_strings[value] = (self.timed(nested) if nested is not None else
                                             bool(TIME_OF_DAY_RE.search(self.bucket_times(strip_recent_work(value)))))
            return self.timed_strings[value]
        if isinstance(value, (dict, list)):
            cache_key = (id(value), key, is_tool_call)
            if cache_key not in self.timed_containers:
                self.timed_containers[cache_key] = any(
                    key_timed(item[1]) or self.timed(*item) for item in self.kept_items(value, key, is_tool_call))
            return self.timed_containers[cache_key]
        return False

    def text(self, text: str) -> str:
        nested = self.nested_json(text)
        if nested is not None:
            return json.dumps(self.walk(nested), sort_keys=True, ensure_ascii=False)
        text = strip_recent_work(text)
        text = self.age_columns(text)
        for cls, pattern, group in ID_CLASSES:
            text = self.sub_group(pattern, group, lambda v, c=cls: self.mapping.placeholder(c, v), text)
        text = self.timestamps(text)
        text = self.sub_group(KEYED_NUMBER_TEXT_RE, 3, self.keyed_number, text)
        text = self.sub_group(PID_PROSE_RE, 3, self.keyed_number, text)
        text = KEYED_EPOCH_TEXT_RE.sub(self.keyed_epoch, text)
        if not self.keep_ips:
            text = IPV4_RE.sub(self.ip, text)
        text = self.sub_group(AGE_FIELD_RE, 3, self.age, text)
        text = AGE_AGO_RE.sub(lambda m: self.age(m.group(1)), text)
        text = AGE_WORDS_AGO_RE.sub(self.age_words, text)
        return text

    def nested_json(self, text: str) -> Any:
        """The JSON object or list `text` holds, else None; parsed once per request (the parse holds container ids)."""
        if text not in self.parsed:
            self.parsed[text] = self.parse_json_document(text)
        return self.parsed[text]

    @staticmethod
    def parse_json_document(text: str) -> Any:
        stripped = text.lstrip()
        if not stripped or stripped[0] not in JSON_OPENERS:
            return None
        try:
            parsed = json.loads(text)
        except ValueError:
            return None
        return parsed if isinstance(parsed, (dict, list)) else None

    @staticmethod
    def sub_group(pattern: "re.Pattern[str]", group: int, fn: Callable[[str], str], text: str) -> str:
        def sub(m: "re.Match[str]") -> str:
            if group == 0:
                return fn(m.group(0))
            start, end = m.span(group)
            base = m.start(0)
            whole = m.group(0)
            return whole[: start - base] + fn(m.group(group)) + whole[end - base :]

        return pattern.sub(sub, text)

    def age_words(self, m: "re.Match[str]") -> str:
        unit = AGE_WORD_UNITS[m.group(3).rstrip("s")]
        value = m.group(1) + m.group(2) + m.group(3)
        return self.mapping.placeholder(AGE_CLASS + unit, value)

    def ip(self, m: "re.Match[str]") -> str:
        value = m.group(0)
        if any(int(octet) > IPV4_OCTET_MAX for octet in value.split(".")):
            return value
        return self.mapping.placeholder(IP_CLASS, value)

    def epoch_token(self, raw: str, seconds: float) -> str:
        self.mapping.bare_numbers.add(raw)
        return time_bucket(seconds, self.now)

    def keyed_epoch(self, m: "re.Match[str]") -> str:
        raw = m.group(3)
        return m.group(1) + m.group(2) + self.epoch_token(raw, self.epoch_value(raw))

    def timestamps(self, text: str) -> str:
        """Bucket every timestamp, then day-count the dates without a time beside them."""
        text = self.bucket_times(text)
        text = ISO_TS_RE.sub(self.iso_date, text)
        text = LS_DATE_RE.sub(self.ls_year, text)
        return LONG_DATE_RE.sub(self.long_date, text)

    def bucket_times(self, text: str) -> str:
        """Replace every timestamp a rule buckets, so no date reads its time as one beside it (TimeIndex.near, timed)."""
        text = ISO_TS_RE.sub(self.iso_time, text)
        text = RFC1123_RE.sub(self.rfc1123, text)
        text = OPENSSL_TS_RE.sub(self.openssl, text)
        return LS_DATE_RE.sub(self.ls_time, text)

    def long_date(self, m: "re.Match[str]") -> str:
        month, day, year, sign, hours, minutes = m.groups()
        try:
            when = datetime(int(year), LONG_MONTHS.index(month) + 1, int(day)).date()
        except ValueError:
            return m.group(0)
        offset = 0
        if sign:
            offset = (-1 if sign == ZONE_NEGATIVE else 1) * (int(hours) * SECONDS_PER_HOUR + int(minutes) * SECONDS_PER_MINUTE)
        return self.date_token(m, when, offset)

    def date_token(self, m: "re.Match[str]", when: "date", offset: int = 0) -> str:
        """The date `m` matched: days from the request's day (at `offset` from UTC), noted as unrestorable.

        Left as written when a time of day is beside it, in its own string
        (TimeIndex.near, built once per string) or in a value or key of an
        enclosing JSON object or list (timed, inherited through time_beside):
        day counting would drop what the time says. The check is here so no
        date rule can skip it. A timestamp's time is bucketed before this runs
        (bucket_times), so it does not count; nor does anything the key drops.
        """
        if self.time_beside or self.time_index(m.string).near(m.start(), m.end()):
            return m.group(0)
        days = (when - datetime.fromtimestamp(self.now + offset, timezone.utc).date()).days
        self.mapping.add_date(when, today=days == 0)
        return day_bucket(days)

    def time_index(self, text: str) -> TimeIndex:
        """The TimeIndex of `text`, built once per string: every date in one sub() pass shares it."""
        if text not in self.time_indexes:
            self.time_indexes[text] = TimeIndex.of(text)
        return self.time_indexes[text]

    def iso_date(self, m: "re.Match[str]") -> str:
        year, month, day, hour = m.group(1), m.group(2), m.group(3), m.group(4)
        if hour is not None:
            return m.group(0)
        try:
            when = datetime(int(year), int(month), int(day)).date()
        except ValueError:
            return m.group(0)
        return self.date_token(m, when)

    def iso_time(self, m: "re.Match[str]") -> str:
        year, month, day, hour, minute, second, zone = m.groups()
        if hour is None:
            return m.group(0)
        try:
            when = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second or 0))
        except ValueError:
            return m.group(0)
        offset = 0
        if zone and zone != UTC_ZONE:
            sign = -1 if zone[0] == ZONE_NEGATIVE else 1
            digits = zone[1:].replace(":", "")
            hours, minutes = digits[:ZONE_HOUR_DIGITS], digits[ZONE_HOUR_DIGITS:]
            offset = sign * (int(hours) * SECONDS_PER_HOUR + int(minutes) * SECONDS_PER_MINUTE)
        return time_bucket(calendar.timegm(when.timetuple()) - offset, self.now)

    def rfc1123(self, m: "re.Match[str]") -> str:
        try:
            when = parsedate_to_datetime(m.group(0))
        except (TypeError, ValueError):
            return m.group(0)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return time_bucket(when.timestamp(), self.now)

    def openssl(self, m: "re.Match[str]") -> str:
        month, day, hour, minute, second, year = m.groups()
        try:
            when = datetime(int(year), MONTHS.index(month) + 1, int(day), int(hour), int(minute), int(second))
        except ValueError:
            return m.group(0)
        return time_bucket(calendar.timegm(when.timetuple()), self.now)

    def ls_year(self, m: "re.Match[str]") -> str:
        month, day, _, _, year = m.groups()
        if not year:
            return m.group(0)
        # Old files show a year and no time. date_token keeps the date as
        # written when a time is beside it (`Nov  5 2026 10:42:01`).
        try:
            when = datetime(int(year), MONTHS.index(month) + 1, int(day)).date()
        except ValueError:
            return m.group(0)
        return self.date_token(m, when)

    def ls_time(self, m: "re.Match[str]") -> str:
        month, day, hour, minute, year = m.groups()
        if year:
            return m.group(0)
        now_year = datetime.fromtimestamp(self.now, timezone.utc).year
        try:
            when = calendar.timegm(
                datetime(now_year, MONTHS.index(month) + 1, int(day), int(hour), int(minute)).timetuple()
            )
            # ls prints a time only for the last six months; a date ahead
            # of now is last year's.
            if when - self.now > SECONDS_PER_DAY:
                when = calendar.timegm(
                    datetime(now_year - 1, MONTHS.index(month) + 1, int(day), int(hour), int(minute)).timetuple()
                )
        except ValueError:
            return m.group(0)
        return time_bucket(when, self.now)

    def age_columns(self, text: str) -> str:
        """Replace ages in a table's AGE / LAST SEEN / Age column, by column position."""
        if not any(name in text for name in AGE_COLUMN_NAMES):
            return text
        lines = text.split("\n")
        spans: List[Tuple[int, int]] = []
        for index, line in enumerate(lines):
            header = self.age_header_spans(line)
            if header:
                spans = header
                continue
            if not line.strip():
                spans = []
                continue
            if not spans or DASH_RULE_RE.match(line):
                continue
            for start, end in reversed(spans):
                if len(line) <= start:
                    continue
                stop = len(line) if end < 0 else end
                cell = line[start:stop]
                line = line[:start] + AGE_TOKEN_RE.sub(lambda m: self.age(m.group(0)), cell) + line[stop:]
            lines[index] = line
        return "\n".join(lines)

    @staticmethod
    def age_header_spans(line: str) -> List[Tuple[int, int]]:
        """Column spans of the age columns when `line` is a table header, else []."""
        if not (UPPER_HEADER_RE.match(line) or DESCRIBE_EVENTS_HEADER_RE.match(line)):
            return []
        cells = [(m.start(), m.group(0)) for m in HEADER_CELL_RE.finditer(line)]
        spans = []
        for i, (start, name) in enumerate(cells):
            if name in AGE_COLUMN_NAMES:
                end = cells[i + 1][0] if i + 1 < len(cells) else -1
                spans.append((start, end))
        return spans


@dataclass
class NormalizedRequest:
    """A request's key material: its normalized form and the hashes the log carries."""

    messages: List[Any]
    settings: Dict[str, Any]
    mapping: Mapping

    @property
    def message_hashes(self) -> List[str]:
        return [stable_hash(m)[:MESSAGE_HASH_HEX_CHARS] for m in self.messages]

    def roles(self) -> List[str]:
        return [str(m.get("role", "")) if isinstance(m, dict) else "" for m in self.messages]

    def body(self) -> Dict[str, Any]:
        out = dict(self.settings)
        out[MESSAGES_FIELD] = self.messages
        return out


def normalize_request(body: Dict[str, Any], now: float) -> NormalizedRequest:
    """Normalize a chat-completions body as of request time `now` (epoch seconds).

    Messages are normalized first, in order, so a placeholder's number depends
    only on what came before it. Two requests sharing their first k messages
    get the same first k message hashes only when they also share the CIDR
    decision (any CIDR anywhere in the request keeps every IP literal) and
    their timestamps fall in the same how-far-from-now buckets. The
    closest-earlier-request search uses diagnosis.py's hashes, which freeze
    each message at first sighting, for that reason.
    """
    kept = {k: v for k, v in body.items() if k not in KEY_DROP_FIELDS}
    keep_ips = CIDR_RE.search(json.dumps(kept, ensure_ascii=False)) is not None
    normalizer = Normalizer(now, keep_ips)
    raw_messages = kept.pop(MESSAGES_FIELD, None)
    messages = [normalizer.walk(m) for m in raw_messages] if isinstance(raw_messages, list) else []
    settings = {k: normalizer.walk(kept[k], k) for k in sorted(kept)}
    if raw_messages is not None and not isinstance(raw_messages, list):
        settings[MESSAGES_FIELD] = normalizer.walk(raw_messages)
    return NormalizedRequest(messages=messages, settings=settings, mapping=normalizer.mapping)


@dataclass(frozen=True)
class KeyContext:
    """What besides the request goes into the key: the LiteLLM setup and the normalizer."""

    litellm_config_checksum: str
    litellm_image: str
    normalizer_version: str = NORMALIZER_VERSION

    def as_dict(self) -> Dict[str, str]:
        return {
            "litellm_config_checksum": self.litellm_config_checksum,
            "litellm_image": self.litellm_image,
            "normalizer_version": self.normalizer_version,
        }


def fingerprint(normalized: NormalizedRequest, context: KeyContext) -> str:
    """The cache key: the whole normalized request plus the LiteLLM setup and normalizer version."""
    return stable_hash({"request": normalized.body(), "context": context.as_dict()})


def settings_hash(normalized: NormalizedRequest, context: KeyContext) -> str:
    """The key without the messages: the root of every per-message prefix chain."""
    return stable_hash({"settings": normalized.settings, "context": context.as_dict()})
