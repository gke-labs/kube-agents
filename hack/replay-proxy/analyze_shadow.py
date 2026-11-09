"""Estimate what replay would have saved, from the shadow proxy's JSON lines.

Reads the lines replay_proxy.py writes (one per chat request; in CI,
collect_shadow.py copies them from each job to a bucket) and asks of every presubmit request whether
replay would have had an answer for it, from the sources the design allows
(examples/inference-replay/eval-design.md, "How a pull request run finds an
answer" and Rollout step 1):

- low estimate: the same fingerprint from a nightly;
- high estimate: also from an earlier, finished build of the same pull
  request (never the same build), and, with `--merged`, from another pull
  request's last build before it merged, once it merged (the merge copy);
- replayed low and replayed high: what replay would hit if it kept each
  conversation on its recording. In shadow mode every answer is a fresh
  sample, so a later request matches exactly only when the model repeated
  every earlier answer; under replay those answers would have been the saved
  ones. A request counts when a source the matching estimate allows (nightly
  for low; the high sources for high) has the same settings (`settings_hash`:
  the request without its messages, plus the LiteLLM setup and normalizer
  version the fingerprint also covers), the same number of messages, the same
  role at each position, and the same message hash at every position that is
  not a generated answer. An exact match always counts as well.

  A generated answer is an assistant message the logs show the model gave in
  this conversation: an earlier request of the same build whose messages were
  exactly the ones before it got an answer that reached the client. The
  messages are compared by their `diag_message_hashes` when both requests
  carry them, since those stay the same across one conversation's requests (a
  CIDR first seen in a later message, or time moving a timestamp into another
  bucket, changes earlier `message_hashes`), and by `message_hashes` otherwise,
  where that drift can lose the evidence. Only generated answers
  would have come from the recording under replay. Any other assistant message
  (a few-shot example, a prefill, a resumed transcript, an empty reply's
  continuation) is prompt and keeps its hash. The logged `answer_hash` can't
  confirm the message is that answer: it hashes the answer's words and tool
  calls in its own request's mapping, while a message hash covers the whole
  normalized message in the later request's mapping, so the two never compare.
  A client that swaps the model's answer for its own text before sending it
  back is still taken as having sent the answer. Evidence is missing, and the
  message keeps its hash, when the asking request isn't in the logs, ran in
  another build, got no answer or lost its client, or (for lines without
  diagnosis hashes) its message hashes drifted.

  It is an estimate both ways. Where the live model took a different action,
  shadow mode sees a different tool result and counts a miss, while replay
  would have run the saved action, so it is low there. It is low too where a
  live answer named a new ID, IP or CIDR: placeholder numbers run across every
  message, answers included, and one CIDR anywhere changes how every IP is
  keyed, so later tool and user hashes shift where replay, serving the saved
  answer, would not have shifted them. It judges each request
  on its own, so it is high where an earlier request of the same conversation
  would have missed (and gone live), or matched a different recording: real
  replay serves an answer only when the conversation so far, served answers
  included, matches one recording.

A line's job kind is the top-level bucket folder it was copied from, never
the line's own `job.kind` tag: the proxy that stamps the tag is built from the
commit under test, and IAM decides who may create objects under each
`replay-shadow/<kind>/` prefix, though not what the uploader names below it.
So a log file counts only at the collector's layout,
`<kind>/<YYYY-MM-DD>/<build>/logs/<segment>.jsonl`, with that `<kind>` the
only directory in its whole path named for a kind: a presubmit cannot make a
file look nightly by nesting a `nightly/` folder in its own prefix. A file
anywhere else is skipped and counted (`off_layout_files`) rather than failing
the run, so an object a presubmit planted cannot stop the analysis; `--kind`
instead names the kind of every input and skips the layout check, for logs
from outside the bucket. A line whose tag names another kind than its file is
dropped and counted. A `nightly-untrusted` line (a scheduled run that uploaded
as an account presubmits can also run as, hack/ci-env.sh) is counted and never
a source, nor a miss's closest request: pull-request code could have written
it. A `nightly` line is one no account pull-request code runs as uploaded; it
is not proof that the night was free of pull-request influence, since a
presubmit with cluster access in the same pool project could have tampered
with that night's proxy (hack/replay-proxy/README.md, Limitations).

A sighting is available from when its answer finished (`ts + latency_ms`),
and counts only when replay would have saved it (the line's `unstorable` list
is empty, so a cut-short upstream answer never counts); `--ignore-storable`
drops that rule. A build counts as finished once its last answer has.

Until run verdicts are joined, high is an upper bound: it counts every earlier
finished build of the same pull request and the merged pull request's last
build whatever their verdicts, and a nightly answer counts as soon as it was
written, before its run passed or failed. A presubmit line without `pr`
(REPLAY_JOB_PR unset) never matches its own pull request's earlier builds; the
report counts those lines.

Also reported: the share of hits first seen in another project, the tokens and
model time hits would save; for replayed-high misses, the tokens by the first
message that is not an assistant message where no allowed source matches (the
part of the conversation replay could not have held fixed, so the next
normalizer target); and for high misses the closest earlier request by
longest matching per-message-hash prefix (the `diag_message_hashes`, which
do not drift between requests of one conversation), ranked by tokens lost. The
stored, scrubbed text of both is under their request_id for a diff.

Many earlier requests can share that prefix (every worker conversation opens
the same way). With `--by-text` or `--store`, the closest is
the one whose next message is most alike by the words they share, relative to
both messages' length, among the 64 most recent distinct next messages that
have stored text; one sharing no word with the miss's is no match. Each miss
then says how it was chosen (`closest_by`: `text` with its `closest_score`, or
`earliest` and why: the miss's own text wasn't stored or has no words, no
candidate's was, none shared a word, or every message matched), and the
report counts the misses ranked by text, so a store missing some jobs shows
as such. Without either, the earliest
available, as before.

The text comes from the request's own build: `--by-text` reads each record's
store from `<kind>/<YYYY-MM-DD>/<build>/store/`, beside the `logs/` it was
loaded from, so text another run uploaded under its own prefix (a presubmit
can read the bucket and learn a nightly's request ids) never stands in for a
trusted candidate's. `--store DIR` names one store for every record, and so is
only for logs from outside the bucket, read with `--kind`. The health
counts show a broken setup; check them after the first day.

A collision audit (did two requests that matched lead to different results?)
is a later step: it joins each `Hit` from `iter_hits` with run verdicts and the
stored text. Nothing here reads verdicts yet.

Usage: python3 analyze_shadow.py PATH [PATH ...] [--kind KIND [--store DIR] | --by-text] [--merged MERGED.json]
       [--top N] [--json] [--ignore-storable]
Each PATH is a `.jsonl` file or a directory searched for them. MERGED.json maps
a pull request number to its merge time in epoch seconds.
"""

from __future__ import annotations

import argparse
import difflib
import functools
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, Iterator, List, Optional, Tuple, Union

import request_store
import schema as F

NIGHTLY = "nightly"
NIGHTLY_UNTRUSTED = "nightly-untrusted"
PRESUBMIT = "presubmit"
JOB_KINDS = (NIGHTLY, NIGHTLY_UNTRUSTED, PRESUBMIT)
# The proxy's segment files (spool.py); a directory argument is searched for these.
LOG_SUFFIX = ".jsonl"
# Where collect_shadow.py files under the bucket's BUCKET_ROOT: a segment at
# <kind>/<YYYY-MM-DD>/<build>/logs/<segment>.jsonl and the job manifest at
# <kind>/<YYYY-MM-DD>/<build>/job-<ms>.json. The build is BUILD_ID's shape
# (hack/ci-env.sh, REPLAY_SHADOW_BUILD_RE), the segment name the logs/ form of
# collect_shadow.SAFE_NAME, and the manifest collect_shadow.write_manifest's.
BUCKET_ROOT = "replay-shadow"
LOGS_DIR = "logs"
DAY_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")
BUILD_DIR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SEGMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.jsonl$")
MANIFEST_NAME = re.compile(r"^job-\d+\.json$")
FILED_DEPTH = 5
MANIFEST_DEPTH = 4
# A build's copied-out REPLAY_STORE_DIR, beside its logs/ (collect_shadow.STORE_REQUESTS_DIR's top).
BUILD_STORE_DIR = "store"
# Requests sharing a prefix that a miss is compared against, newest first, one per distinct next message.
MAX_PREFIX_CANDIDATES = 64
STORE_MESSAGES_KEY = "messages"
# The parts of a stored message whose words are compared.
CONTENT_KEY = "content"
TEXT_KEY = "text"
TOOL_CALLS_KEY = "tool_calls"
FUNCTION_KEY = "function"
NAME_KEY = "name"
ARGUMENTS_KEY = "arguments"
# A candidate counts as alike only above this score: one sharing no word with
# the miss's next message is no match, however recent.
NO_SHARED_WORDS = 0.0
SCORE_DECIMALS = 2
# How a miss's closest request was chosen, with --store: ranked by text, or the
# earliest available, and why it could not be ranked.
CLOSEST_BY_TEXT = "text"
CLOSEST_BY_EARLIEST = "earliest"
UNRANKED_OWN_TEXT_MISSING = "own_text_missing"
UNRANKED_NO_CANDIDATE_TEXT = "no_candidate_text"
UNRANKED_NO_SHARED_WORDS = "no_shared_words"
UNRANKED_OWN_TEXT_EMPTY = "own_text_empty"
UNRANKED_ALL_MATCHED = "all_messages_matched"
ASSISTANT_ROLE = "assistant"
# Unstorable reasons meaning the client never got the model's answer, so the
# request is no evidence that the next assistant message was generated.
ANSWER_NOT_DELIVERED = (F.UNSTORABLE_NO_ANSWER, F.UNSTORABLE_CLIENT_DISCONNECTED)
# Which hashes an entry of the generated-answer evidence index is filed by.
BY_DIAG, BY_MESSAGE = "diag", "message"
# Stands in for an assistant message's content in the replayed key: its place
# in the conversation counts, its words do not.
ANSWER_PLACEHOLDER = "assistant-message"
# Joins a message's role to its hash (or the placeholder) in the replayed key.
ROLE_SEPARATOR = ":"
LOW, HIGH = "low", "high"
REPLAYED_LOW, REPLAYED_HIGH = "replayed_low", "replayed_high"
ESTIMATES = (LOW, HIGH, REPLAYED_LOW, REPLAYED_HIGH)
# Estimates matched on the replayed key, and those taking nightly sources only.
REPLAYED_ESTIMATES = (REPLAYED_LOW, REPLAYED_HIGH)
NIGHTLY_ONLY_ESTIMATES = (LOW, REPLAYED_LOW)
# What the replayed rows leave out, printed under the estimate table.
REPLAYED_CAVEAT = (
    "replayed rows are an estimate, holding to the recording only the answers the logs show the model gave in the"
    " conversation: a different live action gave a different tool result here, where replay would"
    " have run the saved one, and a live answer naming a new ID, IP or CIDR shifted later messages' hashes, where"
    " the saved answer would not have (so low there); each request is judged alone, though an earlier miss in its"
    " conversation, or answers from two recordings, would end a real replay (so high there)."
)
DEFAULT_TOP_MISSES = 20
# Wide enough for the longest estimate name in the text table.
ESTIMATE_COLUMN_WIDTH = max(len(name) for name in ESTIMATES) + 1
PERCENT = 100.0
PERCENT_DECIMALS = 1
MS_PER_SECOND = 1000.0
# Why a line was skipped, counted for the report.
SKIP_NOT_JSON = "not_json"
SKIP_OTHER_EVENT = "other_event"
SKIP_UNTAGGED = "untagged"
# The proxy's analysis failed for a request: the line carries `error` only.
SKIP_ERROR = "error"
# The proxy's queue was full and it dropped the request's analysis.
SKIP_ANALYSIS_DROPPED = "analysis_dropped"
SKIP_NO_FINGERPRINT = "no_fingerprint"
# The line's job.kind tag names a kind other than the folder it was filed under.
SKIP_KIND_MISMATCH = "kind_mismatch"
# Files, not lines: a log file found under a directory argument but not at the
# collector's layout (FILED_DEPTH), so its kind is not the folder IAM governs.
SKIP_OFF_LAYOUT = "off_layout_files"
# A replay-mode line served from the cache: the model was not called, so it is
# neither a sighting a later request could reuse nor an answer to judge.
SKIP_SERVED = "served"
SKIP_REASONS = (SKIP_NOT_JSON, SKIP_OTHER_EVENT, SKIP_UNTAGGED, SKIP_ERROR, SKIP_ANALYSIS_DROPPED, SKIP_NO_FINGERPRINT,
                SKIP_KIND_MISMATCH, SKIP_OFF_LAYOUT, SKIP_SERVED)
# Labels in Miss.first_difference_role besides a message role.
DIFF_UNHASHED = "unhashed"
DIFF_SETTINGS = "settings"
DIFF_NONE = "none"
DIFF_UNKNOWN_ROLE = "unknown"


@dataclass
class Record:
    """One logged chat request."""

    request_id: str
    ts: float
    kind: str
    project: str
    build: str
    pr: str
    fingerprint: str
    settings_hash: str
    diag_settings_hash: Optional[str]
    message_hashes: Optional[List[str]]
    diag_message_hashes: Optional[List[str]]
    roles: List[str]
    prompt_tokens: int
    tokens_estimated: bool
    completion_tokens: int
    latency_ms: int
    unstorable: List[str]
    answer_hash: Optional[str] = None
    # Positions of the assistant messages the logs show were generated in this
    # conversation; set by mark_generated_answers, which load runs.
    generated_answers: FrozenSet[int] = field(default_factory=frozenset)
    # The store of the build this line was filed under, when it came from the
    # bucket's layout: the only place its stored text is read from (BuildStores).
    store_dir: Optional[str] = None

    @property
    def storable(self) -> bool:
        return not self.unstorable

    @property
    def answer_delivered(self) -> bool:
        """Whether the model's answer reached the client, so its next request may carry it."""
        return self.answer_hash is not None and not any(r in ANSWER_NOT_DELIVERED for r in self.unstorable)

    @classmethod
    def from_line(cls, entry: Dict[str, Any], kind: str, store_dir: Optional[str] = None) -> "Record":
        """`kind` is the folder's, which load() has checked the line's tag against."""
        job = entry.get(F.JOB) or {}
        hashes = entry.get(F.MESSAGE_HASHES)
        diag = entry.get(F.DIAG_MESSAGE_HASHES)
        return cls(
            request_id=str(entry.get(F.REQUEST_ID, "")),
            ts=float(entry[F.TS]),
            kind=kind,
            project=str(job.get(F.JOB_PROJECT) or ""),
            build=str(job.get(F.JOB_BUILD) or ""),
            pr=str(job.get(F.JOB_PR) or ""),
            fingerprint=str(entry[F.FINGERPRINT]),
            settings_hash=str(entry.get(F.SETTINGS_HASH) or ""),
            diag_settings_hash=str(entry[F.DIAG_SETTINGS_HASH]) if entry.get(F.DIAG_SETTINGS_HASH) else None,
            # The proxy always writes message_hashes; a hand-made or truncated
            # line may lack them.
            message_hashes=list(hashes) if isinstance(hashes, list) else None,
            diag_message_hashes=list(diag) if isinstance(diag, list) else None,
            roles=list(entry.get(F.ROLES) or []),
            prompt_tokens=int(entry.get(F.PROMPT_TOKENS) or 0),
            tokens_estimated=bool(entry.get(F.TOKENS_ESTIMATED)),
            completion_tokens=int(entry.get(F.COMPLETION_TOKENS) or 0),
            latency_ms=int(entry.get(F.LATENCY_MS) or 0),
            unstorable=[str(r) for r in entry.get(F.UNSTORABLE) or []],
            answer_hash=str(entry[F.ANSWER_HASH]) if entry.get(F.ANSWER_HASH) else None,
            store_dir=store_dir,
        )

    @property
    def available_at(self) -> float:
        """When this request's answer existed, so replay could have served it."""
        return self.ts + self.latency_ms / MS_PER_SECOND

    @property
    def build_key(self) -> Tuple[str, str, str]:
        return (self.kind, self.pr, self.build)

    @property
    def search_hashes(self) -> Optional[List[str]]:
        """The per-message hashes the closest-request search walks: diagnosis hashes when logged."""
        return self.diag_message_hashes if self.diag_message_hashes is not None else self.message_hashes

    @property
    def search_settings_hash(self) -> str:
        """The settings hash the closest-request search roots its chain at: the diagnosis one when logged."""
        return self.diag_settings_hash if self.diag_settings_hash is not None else self.settings_hash

    def chain(self, hashes: Optional[List[str]] = None, root: Optional[str] = None) -> List[str]:
        """Prefix hashes: element k identifies the settings plus the first k+1 messages."""
        out = []
        current = self.settings_hash if root is None else root
        for message_hash in (self.message_hashes if hashes is None else hashes) or []:
            current = hashlib.sha256(f"{current}|{message_hash}".encode()).hexdigest()
            out.append(current)
        return out

    def replayed_chain(self) -> Iterator[str]:
        """Prefix hashes of the replayed key: element k covers the settings and messages 0..k.

        Each message counts by its role, and by its hash unless it is a
        generated answer (`generated_answers`), whose words replay would have
        taken from the recording. Empty without message hashes. A generator, so a search that
        stops at the first difference hashes no further.
        """
        current = self.settings_hash
        for index, message_hash in enumerate(self.message_hashes or []):
            role = self.roles[index] if index < len(self.roles) else ""
            held = role == ASSISTANT_ROLE and index in self.generated_answers
            part = f"{role}{ROLE_SEPARATOR}{ANSWER_PLACEHOLDER if held else message_hash}"
            current = hashlib.sha256(f"{current}|{part}".encode()).hexdigest()
            yield current

    @functools.cached_property
    def replayed_key(self) -> str:
        """The settings and every message, generated answers by their place only."""
        key = self.settings_hash
        for key in self.replayed_chain():
            pass
        return key

    def key(self, estimate: str) -> Optional[str]:
        if estimate not in REPLAYED_ESTIMATES or self.message_hashes is None:
            return self.fingerprint
        return self.replayed_key


@dataclass
class Hit:
    """A presubmit request and the earlier request replay would have served it from."""

    request: Record
    recording: Record


@dataclass
class Miss:
    request: Record
    closest: Optional[Record]
    matched_messages: int
    # With --store: CLOSEST_BY_TEXT, or CLOSEST_BY_EARLIEST and why (an UNRANKED_* reason).
    closest_by: Optional[str] = None
    unranked_reason: Optional[str] = None
    # With closest_by == CLOSEST_BY_TEXT: how alike the two next messages are (0 to 1).
    closest_score: Optional[float] = None

    @property
    def first_difference_role(self) -> str:
        return difference_role(self.request, self.request.search_hashes, self.closest is not None, self.matched_messages)


def difference_role(request: Record, hashes: Optional[List[str]], settings_matched: bool, matched: int) -> str:
    """What differed first: the settings, the role of the first unmatched message, or nothing."""
    if hashes is None:
        return DIFF_UNHASHED
    if not settings_matched:
        return DIFF_SETTINGS
    if matched < len(hashes):
        role = request.roles[matched] if matched < len(request.roles) else ""
        return role or DIFF_UNKNOWN_ROLE
    return DIFF_NONE


class Sources:
    """Where replay may take an answer from, per key, under the design's rules."""

    def __init__(self, records: List[Record], merged: Dict[str, float], require_storable: bool):
        self.merged = merged
        self.build_end: Dict[Tuple[str, str, str], float] = {}
        for record in records:
            end = self.build_end.get(record.build_key, record.available_at)
            self.build_end[record.build_key] = max(end, record.available_at)
        # The merge copy takes the merged pull request's last build that
        # finished before the merge, not every build it ever ran.
        self.merge_build: Dict[str, Tuple[str, str, str]] = {}
        for build_key, end in self.build_end.items():
            kind, pr, _ = build_key
            merged_at = merged.get(pr)
            if kind != PRESUBMIT or merged_at is None or end > merged_at:
                continue
            current = self.merge_build.get(pr)
            if current is None or end > self.build_end[current]:
                self.merge_build[pr] = build_key
        self.nightly: Dict[str, Record] = {}
        self.merge_copy: Dict[str, Tuple[float, Record]] = {}
        # Per key and pull request, each build's first source: every source of
        # one build becomes available together (when the build finishes), so
        # `find` need not look at the rest.
        self.by_pr: Dict[str, Dict[str, Dict[Tuple[str, str, str], Record]]] = {}
        for record in records:
            if record.storable or not require_storable:
                for key in self.keys_of(record):
                    self.add(key, record)

    def keys_of(self, record: Record) -> List[str]:
        """The keys a record is a source for; each subclass says."""
        raise NotImplementedError

    def add(self, key: str, record: Record) -> None:
        if record.kind == NIGHTLY:
            best = self.nightly.get(key)
            if best is None or record.available_at < best.available_at:
                self.nightly[key] = record
            return
        if record.kind != PRESUBMIT:
            return
        merged_at = self.merged.get(record.pr)
        if merged_at is not None and self.merge_build.get(record.pr) == record.build_key:
            best_copy = self.merge_copy.get(key)
            if best_copy is None or merged_at < best_copy[0]:
                self.merge_copy[key] = (merged_at, record)
        if record.pr:
            self.by_pr.setdefault(key, {}).setdefault(record.pr, {}).setdefault(record.build_key, record)

    def find(self, key: Optional[str], request: Record, nightly_only: bool) -> Optional[Record]:
        """The earliest source of `key` available to `request`, or None."""
        if key is None:
            return None
        candidates: List[Tuple[float, Record]] = []
        night = self.nightly.get(key)
        if night is not None and night.available_at <= request.ts:
            candidates.append((night.available_at, night))
        if not nightly_only:
            copy = self.merge_copy.get(key)
            if copy is not None and copy[0] <= request.ts and copy[1].pr != request.pr:
                candidates.append(copy)
            for earlier in self.by_pr.get(key, {}).get(request.pr, {}).values() if request.pr else ():
                if earlier.build != request.build and self.build_end[earlier.build_key] <= request.ts:
                    candidates.append((self.build_end[earlier.build_key], earlier))
        return min(candidates, key=lambda c: c[0])[1] if candidates else None


class KeySources(Sources):
    def __init__(self, records: List[Record], merged: Dict[str, float], require_storable: bool, estimate: str):
        self.estimate = estimate
        super().__init__(records, merged, require_storable)

    def keys_of(self, record: Record) -> List[str]:
        key = record.key(self.estimate)
        # A replayed estimate also files the fingerprint, so an exact match
        # (one line without message hashes) is never lost.
        return [key] if key == record.fingerprint else [key, record.fingerprint]


class ReplayedPrefixSources(Sources):
    """Every prefix of every request's replayed key, for the replayed misses' first difference."""

    def keys_of(self, record: Record) -> List[str]:
        if record.message_hashes is None:
            return []
        return [record.settings_hash, *record.replayed_chain()]

    def first_difference(self, request: Record) -> str:
        """The role of the first message no allowed high source matches, generated answers' words not compared.

        `assistant`: no allowed source matches at that assistant position (none
        has a generated answer there, or the message is prompt and differs);
        `none`: a longer allowed recording passes through, but none stopped here.
        """
        if request.message_hashes is None or self.find(request.settings_hash, request, nightly_only=False) is None:
            return difference_role(request, request.message_hashes, False, 0)
        matched = 0
        for prefix in request.replayed_chain():
            if self.find(prefix, request, nightly_only=False) is None:
                break
            matched += 1
        return difference_role(request, request.message_hashes, True, matched)


class StoredText:
    """The scrubbed request text replay_proxy.py stored (a copied-out REPLAY_STORE_DIR)."""

    def __init__(self, root: str):
        self.root = root

    def _read(self, *parts: str) -> Optional[str]:
        try:
            with open(os.path.join(self.root, *parts), encoding=request_store.TEXT_ENCODING) as handle:
                return handle.read()
        except (OSError, ValueError):  # missing, or not UTF-8: that miss stays unranked
            return None

    def message(self, request_id: str, index: int) -> Optional[str]:
        """The text of message `index` of a stored request, or None when it was not stored."""
        manifest = self._read(request_store.REQUESTS_DIR, request_id + request_store.JSON_SUFFIX)
        try:
            blobs = json.loads(manifest).get(STORE_MESSAGES_KEY) if manifest else None
        except (ValueError, AttributeError):
            return None
        if not isinstance(blobs, list) or not 0 <= index < len(blobs):
            return None
        blob = self._read(request_store.BLOBS_DIR, str(blobs[index]) + request_store.JSON_SUFFIX)
        try:
            return message_text(json.loads(blob)) if blob is not None else None
        except ValueError:
            return None

    def text(self, record: "Record", index: int) -> Optional[str]:
        """Message `index` of `record`, from this one store whatever build it came from."""
        return self.message(record.request_id, index)


class BuildStores:
    """Each record's text from its own build's store (Record.store_dir), never another's."""

    def __init__(self) -> None:
        self.stores: Dict[str, StoredText] = {}

    def text(self, record: "Record", index: int) -> Optional[str]:
        if not record.store_dir:
            return None
        store = self.stores.setdefault(record.store_dir, StoredText(record.store_dir))
        return store.message(record.request_id, index)


TextSource = Union[StoredText, BuildStores]


def message_text(message: Any) -> str:
    """A stored message's words: its content and its tool calls, without the JSON around them."""
    if not isinstance(message, dict):
        return str(message)
    parts: List[str] = []
    content = message.get(CONTENT_KEY)
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        parts.extend(str(part.get(TEXT_KEY, "")) if isinstance(part, dict) else str(part) for part in content)
    for call in message.get(TOOL_CALLS_KEY) or []:
        function = call.get(FUNCTION_KEY) if isinstance(call, dict) else None
        if isinstance(function, dict):
            parts.extend((str(function.get(NAME_KEY, "")), str(function.get(ARGUMENTS_KEY, ""))))
    return "\n".join(parts)


def similarity(saved: str, current: str) -> float:
    """How alike two messages are by the words they share, relative to both lengths (order ignored)."""
    return difflib.SequenceMatcher(None, saved.split(), current.split(), autojunk=False).quick_ratio()


class PrefixSources(Sources):
    """Every prefix of every request, for the closest-earlier-request search."""

    def __init__(self, records: List[Record], merged: Dict[str, float], require_storable: bool, by_text: bool = False):
        # Every request under each prefix, kept only when misses are ranked by text.
        self.members: Optional[Dict[str, List[Record]]] = {} if by_text else None
        super().__init__(records, merged, require_storable)

    def keys_of(self, record: Record) -> List[str]:
        if record.search_hashes is None:
            return []
        root = record.search_settings_hash
        return [root] + record.chain(record.search_hashes, root)

    def add(self, key: str, record: Record) -> None:
        super().add(key, record)
        # Only what Sources.add takes as a source: a nightly-untrusted line could
        # have been written by pull-request code.
        if self.members is not None and record.kind in (NIGHTLY, PRESUBMIT):
            self.members.setdefault(key, []).append(record)

    def usable(self, record: Record, request: Record) -> bool:
        """Whether `find` could return `record` for `request`, under the same rules."""
        if record.kind == NIGHTLY:
            return record.available_at <= request.ts
        if record.kind != PRESUBMIT:
            return False
        merged_at = self.merged.get(record.pr)
        if (merged_at is not None and self.merge_build.get(record.pr) == record.build_key
                and merged_at <= request.ts and record.pr != request.pr):
            return True
        return (bool(request.pr) and record.pr == request.pr and record.build != request.build
                and self.build_end[record.build_key] <= request.ts)

    def closest(self, request: Record, texts: Optional[TextSource] = None) -> Miss:
        if request.search_hashes is None:
            return Miss(request, None, 0)
        root = request.search_settings_hash
        best = self.find(root, request, nightly_only=False)
        if best is None:
            return Miss(request, None, 0)
        matched, key = 0, root
        for prefix in request.chain(request.search_hashes, root):
            found = self.find(prefix, request, nightly_only=False)
            if found is None:
                break
            best, matched, key = found, matched + 1, prefix
        if texts is None or self.members is None:
            return Miss(request, best, matched)
        if matched >= len(request.search_hashes):
            return Miss(request, best, matched, CLOSEST_BY_EARLIEST, UNRANKED_ALL_MATCHED)
        current = texts.text(request, matched)
        if current is None:
            return Miss(request, best, matched, CLOSEST_BY_EARLIEST, UNRANKED_OWN_TEXT_MISSING)
        if not current.split():  # an image- or thinking-only message: no words to rank by
            return Miss(request, best, matched, CLOSEST_BY_EARLIEST, UNRANKED_OWN_TEXT_EMPTY)
        read_any, ranked, score = self.most_alike(key, matched, request, current, texts)
        if ranked is None:
            reason = UNRANKED_NO_SHARED_WORDS if read_any else UNRANKED_NO_CANDIDATE_TEXT
            return Miss(request, best, matched, CLOSEST_BY_EARLIEST, reason)
        return Miss(request, ranked, matched, CLOSEST_BY_TEXT, closest_score=round(score, SCORE_DECIMALS))

    def most_alike(
        self, key: str, index: int, request: Record, current: str, texts: TextSource
    ) -> Tuple[bool, Optional[Record], float]:
        """Of the usable requests sharing `key`, the one whose message `index` is most like `current`.

        Returns whether any candidate's text was read, the best one sharing at
        least one word (None if none does), and its score. A copy whose text is
        missing neither stands in for nor uses up the slot of its next message,
        so an older copy of the same message is still read.
        """
        seen = set()
        ranked: Tuple[float, Optional[Record]] = (NO_SHARED_WORDS, None)
        # Newest first: the latest run of the same case is the likeliest match.
        for record in reversed(self.members.get(key, [])):
            following = record.search_hashes[index] if index < len(record.search_hashes or []) else ""
            if following in seen or not self.usable(record, request):
                continue
            text = texts.text(record, index)
            if text is None:
                continue
            seen.add(following)
            if len(seen) > MAX_PREFIX_CANDIDATES:
                break
            score = similarity(text, current)
            if score > ranked[0]:
                ranked = (score, record)
        return bool(seen), ranked[1], ranked[0]


def filed_kind(path: str) -> Optional[str]:
    """The kind a segment or job manifest was filed under, when it sits at the collector's layout, else None.

    The kind directory must be the only component of the whole absolute path
    named for a kind, since everything below a kind prefix is the uploader's
    choice; the build directory may not be named for a kind or the bucket root.
    """
    parts = os.path.normpath(os.path.abspath(path)).split(os.sep)
    name = parts[-1]
    if SEGMENT_NAME.match(name):
        depth = FILED_DEPTH
        if len(parts) < depth or parts[-2] != LOGS_DIR:
            return None
    elif MANIFEST_NAME.match(name):
        depth = MANIFEST_DEPTH
        if len(parts) < depth:
            return None
    else:
        return None
    kind, day, build = parts[-depth:][:3]
    if kind not in JOB_KINDS or not DAY_DIR.match(day) or not BUILD_DIR.match(build) or build in (*JOB_KINDS, BUCKET_ROOT):
        return None
    if any(part in JOB_KINDS for part in parts[:-depth]):
        return None
    return kind


def log_files(paths: Iterable[str], kind: Optional[str] = None) -> Tuple[List[Tuple[str, str]], int]:
    """Each log segment under `paths` with its job kind, and how many were skipped off the layout.

    With `kind`, every segment found is that kind. Without it, a segment's kind
    is filed_kind's, and one found under a directory but off the layout is
    skipped and counted, as is a directory with nothing at the layout (with a
    note on stderr): an object a presubmit planted under its own prefix must
    not stop the analysis of the rest. Raises ValueError for the operator's own
    argument mistakes: a path that does not exist, a file named outright that is
    not a segment or is off the layout, or a run where log segments were found
    and none is at the layout. A run with no segments at all reads nothing.
    """
    found: List[Tuple[str, str]] = []
    off_layout = 0
    layout = f"<{'|'.join(JOB_KINDS)}>/<YYYY-MM-DD>/<build>/{LOGS_DIR}/<segment>{LOG_SUFFIX}"
    for path in paths:
        if not os.path.exists(path):
            raise ValueError(f"{path} does not exist")
        if not os.path.isdir(path):
            if not path.endswith(LOG_SUFFIX):
                raise ValueError(f"{path} is not a log segment (*{LOG_SUFFIX}); job manifests and the store are not "
                                 "read as logs")
            file_kind = kind or filed_kind(path)
            if file_kind is None:
                raise ValueError(f"{path} is not at the bucket's layout ({layout}, with no other directory in its "
                                 "path named for a kind); pass --kind to name its kind")
            found.append((path, file_kind))
            continue
        files = sorted(os.path.join(root, name) for root, _, names in os.walk(path)
                       for name in names if name.endswith(LOG_SUFFIX))
        filed = [(name, kind or filed_kind(name)) for name in files]
        kept = [(name, file_kind) for name, file_kind in filed if file_kind is not None]
        if files and not kept:
            print(f"note: {path} holds no log segment at the bucket's layout; its {len(files)} are counted as "
                  f"{SKIP_OFF_LAYOUT} and skipped", file=sys.stderr)
        found += kept
        off_layout += len(files) - len(kept)
    if off_layout and not found:
        raise ValueError(f"no log segment is at the bucket's layout ({layout}, with no other directory in its path "
                         "named for a kind); pass the kind folders or their parent, or --kind")
    return found, off_layout


def load(paths: Iterable[str], kind: Optional[str] = None) -> Tuple[List[Record], Dict[str, int]]:
    """Read JSON lines from each path (log_files); returns the records in time order and skip counts."""
    records: List[Record] = []
    skipped = {reason: 0 for reason in SKIP_REASONS}
    files, skipped[SKIP_OFF_LAYOUT] = log_files(paths, kind)
    for path, file_kind in files:
        # At the layout (no `kind`), <build>/logs/<segment>: the build's store is beside logs/.
        store_dir = None if kind else os.path.join(os.path.dirname(os.path.dirname(path)), BUILD_STORE_DIR)
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    skipped[SKIP_NOT_JSON] += 1
                    continue
                if not isinstance(entry, dict) or entry.get(F.EVENT) != F.REQUEST_EVENT:
                    skipped[SKIP_OTHER_EVENT] += 1
                    continue
                if entry.get(F.ERROR) == F.ERROR_ANALYSIS_DROPPED:
                    skipped[SKIP_ANALYSIS_DROPPED] += 1
                    continue
                if entry.get(F.ERROR):
                    skipped[SKIP_ERROR] += 1
                    continue
                if not entry.get(F.FINGERPRINT):
                    skipped[SKIP_NO_FINGERPRINT] += 1
                    continue
                if entry.get(F.SERVED):
                    skipped[SKIP_SERVED] += 1
                    continue
                tag = (entry.get(F.JOB) or {}).get(F.JOB_KIND)
                if tag not in JOB_KINDS:
                    skipped[SKIP_UNTAGGED] += 1
                    continue
                if tag != file_kind:
                    skipped[SKIP_KIND_MISMATCH] += 1
                    continue
                records.append(Record.from_line(entry, file_kind, store_dir))
    records.sort(key=lambda r: r.ts)
    mark_generated_answers(records)
    return records, skipped


def mark_generated_answers(records: List[Record]) -> None:
    """Set each record's `generated_answers`, from records in time order.

    Position i holds a generated answer when it is an assistant message and an
    earlier request of the same build, whose messages are exactly this
    request's first i, got an answer that reached the client before this
    request was made. Messages compare by diagnosis hashes when both requests
    carry them (they don't drift within a conversation), by message hashes
    when either lacks them.
    """
    # Requests whose answer reached the client, by build, message count and
    # last hash: with diagnosis hashes under BY_DIAG, every request under
    # BY_MESSAGE. A candidate still has to match every earlier hash.
    answered: Dict[Tuple[str, Tuple[str, str, str], int, str], List[Record]] = {}
    for record in records:
        hashes = record.message_hashes
        if hashes is None:
            continue
        diag = evidence_diag_hashes(record)
        generated = []
        for index, role in enumerate(record.roles[:len(hashes)]):
            if role != ASSISTANT_ROLE:
                continue
            if diag is not None and any(
                asked.available_at <= record.ts and evidence_diag_hashes(asked) == diag[:index]
                for asked in answered.get((BY_DIAG, record.build_key, index, diag[index - 1] if index else ""), ())
            ):
                generated.append(index)
                continue
            if any(
                asked.available_at <= record.ts and asked.message_hashes == hashes[:index]
                # With both diagnosis hashes, only they decide.
                and (diag is None or evidence_diag_hashes(asked) is None)
                for asked in answered.get((BY_MESSAGE, record.build_key, index, hashes[index - 1] if index else ""), ())
            ):
                generated.append(index)
        record.generated_answers = frozenset(generated)
        if record.answer_delivered:
            answered.setdefault((BY_MESSAGE, record.build_key, len(hashes), hashes[-1] if hashes else ""), []).append(record)
            if diag is not None:
                answered.setdefault((BY_DIAG, record.build_key, len(diag), diag[-1] if diag else ""), []).append(record)


def evidence_diag_hashes(record: Record) -> Optional[List[str]]:
    """A record's diagnosis hashes when it has one per message, for the generated-answer evidence."""
    diag = record.diag_message_hashes
    return diag if diag is not None and record.message_hashes is not None and len(diag) == len(record.message_hashes) else None


def iter_hits(
    records: List[Record], estimate: str, require_storable: bool = True, merged: Optional[Dict[str, float]] = None
) -> Iterator[Tuple[Record, Optional[Hit]]]:
    """Each presubmit request in time order, with its Hit under `estimate` or None.

    This is the seam a collision audit reads: every Hit pairs a request with
    the recording it would have been served from.
    """
    sources = KeySources(records, merged or {}, require_storable, estimate)
    nightly_only = estimate in NIGHTLY_ONLY_ESTIMATES
    for record in records:
        if record.kind == PRESUBMIT:
            key = record.key(estimate)
            recording = sources.find(key, record, nightly_only)
            # A replayed estimate is its exact one plus the matches it adds, never fewer.
            if recording is None and key != record.fingerprint:
                recording = sources.find(record.fingerprint, record, nightly_only)
            yield record, (Hit(record, recording) if recording is not None else None)


def find_misses(
    records: List[Record], require_storable: bool = True, merged: Optional[Dict[str, float]] = None,
    texts: Optional[TextSource] = None,
) -> List[Miss]:
    """High-estimate misses, each with its closest earlier request."""
    prefixes = PrefixSources(records, merged or {}, require_storable, by_text=texts is not None)
    misses = []
    for record, hit in iter_hits(records, HIGH, require_storable, merged):
        if hit is None:
            misses.append(prefixes.closest(record, texts))
    return misses


def replayed_miss_differences(
    records: List[Record], require_storable: bool = True, merged: Optional[Dict[str, float]] = None
) -> List[Tuple[Record, str]]:
    """Replayed-high misses, each with the role of its first message no allowed source matches."""
    prefixes = ReplayedPrefixSources(records, merged or {}, require_storable)
    return [(record, prefixes.first_difference(record))
            for record, hit in iter_hits(records, REPLAYED_HIGH, require_storable, merged) if hit is None]


def tokens_by_label(labelled: Iterable[Tuple[Record, str]]) -> Dict[str, int]:
    """Prompt tokens per label, largest first."""
    totals: Dict[str, int] = {}
    for record, label in labelled:
        totals[label] = totals.get(label, 0) + record.prompt_tokens
    return dict(sorted(totals.items(), key=lambda kv: -kv[1]))


def percent(part: float, whole: float) -> float:
    return round(PERCENT * part / whole, PERCENT_DECIMALS) if whole else 0.0


def health(records: List[Record], skipped: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """Counts that show a broken setup: check these after the first day."""
    skipped = skipped or {}
    by_reason: Dict[str, int] = {}
    for record in records:
        for reason in record.unstorable:
            by_reason[reason] = by_reason.get(reason, 0) + 1
    return {
        "lines_by_kind": {kind: sum(1 for r in records if r.kind == kind) for kind in JOB_KINDS},
        "error_lines": skipped.get(SKIP_ERROR, 0),
        "lines_without_fingerprint": skipped.get(SKIP_NO_FINGERPRINT, 0),
        "untagged_lines": skipped.get(SKIP_UNTAGGED, 0),
        # Tagged with a kind other than their folder's: dropped, never trusted.
        "kind_mismatch_lines": skipped.get(SKIP_KIND_MISMATCH, 0),
        # Log files off the collector's layout: skipped whole, never trusted.
        "off_layout_files": skipped.get(SKIP_OFF_LAYOUT, 0),
        "unstorable_lines": sum(1 for r in records if not r.storable),
        "unstorable_percent_by_reason": {
            reason: percent(count, len(records)) for reason, count in sorted(by_reason.items(), key=lambda kv: -kv[1])
        },
        # REPLAY_JOB_PR unset: these never match an earlier build of their PR.
        "presubmit_without_pr": sum(1 for r in records if r.kind == PRESUBMIT and not r.pr),
        "without_message_hashes": sum(1 for r in records if r.message_hashes is None),
        "upstream_incomplete": by_reason.get(F.UNSTORABLE_UPSTREAM_INCOMPLETE, 0),
        "observe_failed": by_reason.get(F.UNSTORABLE_OBSERVE_FAILED, 0),
        "observe_too_large": by_reason.get(F.UNSTORABLE_OBSERVE_TOO_LARGE, 0),
        "analysis_dropped": skipped.get(SKIP_ANALYSIS_DROPPED, 0),
        # Prompt tokens on lines whose response carried no usage, estimated
        # from the request's size.
        "estimated_token_percent": percent(
            sum(r.prompt_tokens for r in records if r.tokens_estimated), sum(r.prompt_tokens for r in records)
        ),
    }


def summarize(
    records: List[Record], require_storable: bool = True, top: int = DEFAULT_TOP_MISSES,
    merged: Optional[Dict[str, float]] = None, skipped: Optional[Dict[str, int]] = None,
    texts: Optional[TextSource] = None,
) -> Dict[str, Any]:
    presubmit = [r for r in records if r.kind == PRESUBMIT]
    total_tokens = sum(r.prompt_tokens for r in presubmit)
    report: Dict[str, Any] = {
        "requests": len(records),
        "presubmit_requests": len(presubmit),
        "nightly_requests": sum(1 for r in records if r.kind == NIGHTLY),
        "nightly_untrusted_requests": sum(1 for r in records if r.kind == NIGHTLY_UNTRUSTED),
        "presubmit_prompt_tokens": total_tokens,
        "merge_times_given": len(merged or {}),
        "health": health(records, skipped),
        "estimates": {},
    }
    for estimate in ESTIMATES:
        hits = [hit for _, hit in iter_hits(records, estimate, require_storable, merged) if hit is not None]
        hit_tokens = sum(h.request.prompt_tokens for h in hits)
        cross = [h for h in hits if h.recording.project != h.request.project]
        report["estimates"][estimate] = {
            "hits": len(hits),
            "hit_prompt_tokens": hit_tokens,
            "token_weighted_hit_percent": percent(hit_tokens, total_tokens),
            "saved_completion_tokens": sum(h.request.completion_tokens for h in hits),
            "saved_model_ms": sum(h.request.latency_ms for h in hits),
            "cross_project_hit_percent": percent(sum(h.request.prompt_tokens for h in cross), hit_tokens),
        }
    misses = sorted(find_misses(records, require_storable, merged, texts), key=lambda m: m.request.prompt_tokens, reverse=True)
    report["miss_tokens_by_first_difference"] = tokens_by_label((m.request, m.first_difference_role) for m in misses)
    report["replayed_miss_tokens_by_first_difference"] = tokens_by_label(
        replayed_miss_differences(records, require_storable, merged)
    )
    report["top_misses"] = [
        {
            "request_id": m.request.request_id,
            "project": m.request.project,
            "pr": m.request.pr,
            "build": m.request.build,
            "prompt_tokens": m.request.prompt_tokens,
            "messages": len(m.request.message_hashes or []),
            "matched_messages": m.matched_messages,
            "first_difference_role": m.first_difference_role,
            "closest_request_id": m.closest.request_id if m.closest else None,
            "closest_kind": m.closest.kind if m.closest else None,
            **({"closest_by": m.closest_by, "unranked_reason": m.unranked_reason, "closest_score": m.closest_score}
               if m.closest_by else {}),
        }
        for m in misses[:top]
    ]
    if texts is not None:
        ranked = [m for m in misses if m.closest_by is not None]
        reasons: Dict[str, int] = {}
        for m in ranked:
            if m.unranked_reason:
                reasons[m.unranked_reason] = reasons.get(m.unranked_reason, 0) + 1
        report["closest_by_text"] = {
            "misses": len(ranked),
            "ranked_by_text": sum(1 for m in ranked if m.closest_by == CLOSEST_BY_TEXT),
            "earliest_because": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        }
    return report


def render(report: Dict[str, Any], skipped: Dict[str, int]) -> str:
    lines = [
        f"requests: {report['requests']} ({report['presubmit_requests']} presubmit, {report['nightly_requests']} nightly,"
        f" {report['nightly_untrusted_requests']} nightly-untrusted, never a source);"
        f" presubmit prompt tokens: {report['presubmit_prompt_tokens']}",
        f"skipped lines: {skipped}; merge times given: {report['merge_times_given']}",
        "",
        "health (check after the first day):",
    ]
    lines += [f"  {name}: {value}" for name, value in report["health"].items()]
    lines += [
        "",
        f"{'estimate':<{ESTIMATE_COLUMN_WIDTH}}  hits   token-weighted   cross-project"
        "   saved prompt / completion tokens   saved model time",
    ]
    for name, e in report["estimates"].items():
        lines.append(
            f"{name:<{ESTIMATE_COLUMN_WIDTH}} {e['hits']:>5}   {e['token_weighted_hit_percent']:>13}%"
            f"   {e['cross_project_hit_percent']:>12}%"
            f"   {e['hit_prompt_tokens']} / {e['saved_completion_tokens']}   {e['saved_model_ms']} ms"
        )
    lines += [REPLAYED_CAVEAT, "",
              f"miss tokens by first differing message: {report['miss_tokens_by_first_difference']}",
              "replayed_high miss tokens by role of first differing message (assistant: no recording matches that answer"
              " position;"
              " none: only longer recordings pass through):"
              f" {report['replayed_miss_tokens_by_first_difference']}", "",
              "top misses (prompt tokens, matched/total messages, first difference, closest earlier request):"]
    for m in report["top_misses"]:
        detail = m.get("unranked_reason") or m.get("closest_score")
        by = f", by {m['closest_by']}" + (f" {detail}" if detail is not None else "") if m.get("closest_by") else ""
        lines.append(
            f"  {m['request_id']}  {m['prompt_tokens']}  {m['matched_messages']}/{m['messages']}"
            f"  {m['first_difference_role']}  {m['closest_request_id']} ({m['closest_kind']}{by})"
        )
    if "closest_by_text" in report:
        c = report["closest_by_text"]
        lines += ["", f"closest request ranked by text for {c['ranked_by_text']} of {c['misses']} misses;"
                      f" the earliest for the rest because: {c['earliest_because']}"]
    return "\n".join(lines)


def load_merged(path: Optional[str]) -> Dict[str, float]:
    if not path:
        return {}
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    return {str(pr): float(when) for pr, when in raw.items()}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("logs", nargs="+", help="JSONL files written by replay_proxy.py, or directories of them")
    parser.add_argument("--kind", choices=JOB_KINDS,
                        help="the job kind of every input, instead of the <kind>/<day>/<build>/logs/ folder each is under")
    parser.add_argument("--merged", help="JSON file mapping a pull request number to its merge time (epoch seconds)")
    parser.add_argument("--by-text", action="store_true",
                        help="rank requests sharing a prefix by text, from each build's own store/ beside its logs/")
    parser.add_argument("--store", help="with --kind: one copied-out REPLAY_STORE_DIR for every input, to rank by text")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_MISSES, help="misses to list")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--ignore-storable", action="store_true", help="count sightings replay would not have saved")
    args = parser.parse_args(argv)
    if args.store is not None and args.kind is None:
        parser.error("--store names one store for every input, so only with --kind; logs at the bucket's layout use "
                     "--by-text, which reads each build's own store/")
    if args.by_text and (args.kind is not None or args.store is not None):
        parser.error("--by-text reads each build's store/ beside its logs/, which inputs read with --kind do not "
                     "have; use --store DIR")
    for needed in (request_store.REQUESTS_DIR, request_store.BLOBS_DIR) if args.store is not None else ():
        if not os.path.isdir(os.path.join(args.store, needed)):
            parser.error(f"--store {args.store} has no {needed}/ directory")
    try:
        records, skipped = load(args.logs, args.kind)
    except ValueError as exc:
        parser.error(str(exc))
    report = summarize(
        records, require_storable=not args.ignore_storable, top=args.top, merged=load_merged(args.merged), skipped=skipped,
        texts=StoredText(args.store) if args.store is not None else BuildStores() if args.by_text else None,
    )
    if args.json:
        report["skipped_lines"] = skipped
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render(report, skipped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
