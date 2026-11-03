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

import calendar
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
PLACEHOLDER_RE = re.compile(re.escape(OPEN) + r"[^" + re.escape(CLOSE) + r"]+" + re.escape(CLOSE))

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
NUMBER_CLASS = "NUM"
DIGITS_RE = re.compile(r"^\d+$")
KEYED_NUMBER_TEXT_RE = re.compile(
    r"(?<![A-Za-z0-9_])(resourceVersion|observedGeneration|generation|run_id|pid|PID|id)"
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
NOW_WINDOW_SECONDS = 60
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400
DAYS_PER_MONTH_BUCKET = 30
DAYS_PER_YEAR_BUCKET = 365
TIME_BUCKETS = (
    (SECONDS_PER_HOUR, "minutes"),
    (SECONDS_PER_DAY, "hours"),
    (SECONDS_PER_DAY * DAYS_PER_MONTH_BUCKET, "days"),
    (SECONDS_PER_DAY * DAYS_PER_YEAR_BUCKET, "months"),
)
TIME_BUCKET_BEYOND = "years"
TIME_NOW = "now"
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
# Epoch seconds 2020-2033, and the same in milliseconds, bucketed only under a
# key that names a time: a free-text 10-digit number (`memory: 1717986918
# bytes`) stays literal.
EPOCH_FULL_RE = re.compile(r"^(1[6-9]\d{8})(\d{3})?(?:\.\d+)?$")
TIME_KEY_PATTERN = (
    r"(?:[A-Za-z0-9_]*_at|[A-Za-z0-9]*[a-z](?:At|Time|Timestamp|Date)|time|timestamp|ts|date"
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
NUMBER_BEFORE = r"(?<![\w.])"
NUMBER_AFTER = r"(?![\w])"
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
    if distance < NOW_WINDOW_SECONDS:
        name = TIME_NOW
    else:
        span = next((label for limit, label in TIME_BUCKETS if distance < limit), TIME_BUCKET_BEYOND)
        name = (TIME_PAST_PREFIX if delta > 0 else TIME_FUTURE_PREFIX) + span
    return f"{OPEN}{TIME_CLASS}_{name}{CLOSE}"


@dataclass
class Mapping:
    """Value <-> placeholder, numbered per class by first appearance."""

    to_placeholder: Dict[str, str] = field(default_factory=dict)
    to_value: Dict[str, str] = field(default_factory=dict)
    counts: Dict[str, int] = field(default_factory=dict)
    # Values swapped that can never be put back in an answer: bare numbers
    # (keyed numbers) and epoch timestamps, which become an unnumbered
    # how-far-from-now time token.
    bare_numbers: set = field(default_factory=set)

    def placeholder(self, cls: str, value: str) -> str:
        key = (cls if cls in (OPAQUE_CLASS, NUMBER_CLASS) else "") + MAPPING_KEY_SEPARATOR + value
        if key not in self.to_placeholder:
            number = self.counts.get(cls, 0) + 1
            self.counts[cls] = number
            token = f"{OPEN}{cls}_{number}{CLOSE}"
            self.to_placeholder[key] = token
            self.to_value[token] = value
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

    def answer_repeats_bare_number(self, text: str) -> bool:
        """Whether an answer repeats a swapped value that cannot be restored."""
        return any(re.search(NUMBER_BEFORE + re.escape(v) + NUMBER_AFTER, text) for v in self.bare_numbers)


def age_unit(value: str) -> str:
    return DIGIT_RE.sub("", value)[:1]


class Normalizer:
    """Normalizes one request. Holds the request time and the mapping."""

    def __init__(self, now: float, keep_ips: bool):
        self.now = now
        self.keep_ips = keep_ips
        self.mapping = Mapping()

    # -- structure ---------------------------------------------------------

    def walk(self, obj: Any, key: Optional[str] = None, in_tool_call: bool = False) -> Any:
        if key is not None and self.is_opaque(key, in_tool_call):
            return self.opaque(obj)
        if key is not None and TIME_KEY_RE.match(key) and self.epoch_value(obj) is not None:
            return self.epoch_token(str(obj), self.epoch_value(obj))
        if key in KEYED_NUMBER_KEYS and self.is_number(obj):
            return self.mapping.placeholder(NUMBER_CLASS, str(obj))
        if key in AGE_KEYS and isinstance(obj, str) and AGE_FULL_RE.match(obj):
            return self.age(obj)
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, list):
            return [self.walk_dict(v, True) if key == TOOL_CALLS_KEY and isinstance(v, dict) else self.walk(v) for v in obj]
        if isinstance(obj, dict):
            return self.walk_dict(obj, False)
        return obj

    def walk_dict(self, obj: Dict[str, Any], in_tool_call: bool) -> Dict[str, Any]:
        return {k: self.walk(obj[k], k, in_tool_call) for k in sorted(obj)}

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

    def opaque(self, value: Any) -> str:
        whole = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
        return self.mapping.placeholder(OPAQUE_CLASS, whole)

    def age(self, value: str) -> str:
        return self.mapping.placeholder(AGE_CLASS + age_unit(value), value)

    # -- text --------------------------------------------------------------

    def text(self, text: str) -> str:
        nested = self.nested_json(text)
        if nested is not None:
            return json.dumps(self.walk(nested), sort_keys=True, ensure_ascii=False)
        text = RECENT_WORK_RE.sub(RECENT_WORK_STRIPPED, text)
        text = RECENT_WORK_ESCAPED_RE.sub(RECENT_WORK_ESCAPED_STRIPPED, text)
        text = self.age_columns(text)
        for cls, pattern, group in ID_CLASSES:
            text = self.sub_group(pattern, group, lambda v, c=cls: self.mapping.placeholder(c, v), text)
        text = self.timestamps(text)
        text = self.sub_group(KEYED_NUMBER_TEXT_RE, 3, lambda v: self.mapping.placeholder(NUMBER_CLASS, v), text)
        text = self.sub_group(PID_PROSE_RE, 3, lambda v: self.mapping.placeholder(NUMBER_CLASS, v), text)
        text = KEYED_EPOCH_TEXT_RE.sub(self.keyed_epoch, text)
        if not self.keep_ips:
            text = IPV4_RE.sub(self.ip, text)
        text = self.sub_group(AGE_FIELD_RE, 3, self.age, text)
        text = AGE_AGO_RE.sub(lambda m: self.age(m.group(1)), text)
        text = AGE_WORDS_AGO_RE.sub(self.age_words, text)
        return text

    @staticmethod
    def nested_json(text: str) -> Any:
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
        text = ISO_TS_RE.sub(self.iso, text)
        text = RFC1123_RE.sub(self.rfc1123, text)
        text = OPENSSL_TS_RE.sub(self.openssl, text)
        return LS_DATE_RE.sub(self.ls_date, text)

    def iso(self, m: "re.Match[str]") -> str:
        year, month, day, hour, minute, second, zone = m.groups()
        try:
            when = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0), int(second or 0))
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

    def ls_date(self, m: "re.Match[str]") -> str:
        month, day, hour, minute, year = m.groups()
        now_year = datetime.fromtimestamp(self.now, timezone.utc).year
        try:
            if year:
                when = calendar.timegm(datetime(int(year), MONTHS.index(month) + 1, int(day)).timetuple())
            else:
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
