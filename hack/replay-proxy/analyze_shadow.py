"""Estimate what replay would have saved, from the shadow proxy's JSON lines.

Reads the lines replay_proxy.py writes (one per chat request, collected from
each job with `kubectl logs -f`) and asks of every presubmit request whether
replay would have had an answer for it, from the sources the design allows
(examples/inference-replay/eval-design.md, "How a pull request run finds an
answer" and Rollout step 1):

- low estimate: the same fingerprint from a nightly;
- high estimate: also from an earlier, finished build of the same pull
  request (never the same build), and, with `--merged`, from another pull
  request's last build before it merged, once it merged (the merge copy);
- simulated: high, plus the requests that match when the assistant messages
  in the history are left out of the comparison. In shadow mode every answer
  is a fresh sample, so a later request matches exactly only when the model
  repeated every earlier answer;
  under replay those answers would have been the recorded ones. It assumes the
  recorded answer's tool calls would have returned the results this run saw.

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
model time hits would save, and for misses the closest earlier request by
longest matching per-message-hash prefix (the `diag_message_hashes`, which
do not drift between requests of one conversation), ranked by tokens lost. The
stored, scrubbed text of both is under their request_id for a diff. The health
counts show a broken setup; check them after the first day.

A collision audit (did two requests that matched lead to different results?)
is a later step: it joins each `Hit` from `iter_hits` with run verdicts and the
stored text. Nothing here reads verdicts yet.

Usage: python3 analyze_shadow.py LOG [LOG ...] [--merged MERGED.json] [--top N] [--json] [--ignore-storable]
MERGED.json maps a pull request number to its merge time in epoch seconds.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

import schema as F

NIGHTLY = "nightly"
PRESUBMIT = "presubmit"
JOB_KINDS = (NIGHTLY, PRESUBMIT)
ASSISTANT_ROLE = "assistant"
# Stands in for an assistant message's content in the simulated key: its place
# in the conversation counts, its words do not.
ANSWER_PLACEHOLDER = "assistant-message"
LOW, HIGH, SIMULATED = "low", "high", "simulated"
ESTIMATES = (LOW, HIGH, SIMULATED)
DEFAULT_TOP_MISSES = 20
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
SKIP_REASONS = (SKIP_NOT_JSON, SKIP_OTHER_EVENT, SKIP_UNTAGGED, SKIP_ERROR, SKIP_ANALYSIS_DROPPED, SKIP_NO_FINGERPRINT)
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

    @property
    def storable(self) -> bool:
        return not self.unstorable

    @classmethod
    def from_line(cls, entry: Dict[str, Any]) -> "Record":
        job = entry.get(F.JOB) or {}
        hashes = entry.get(F.MESSAGE_HASHES)
        diag = entry.get(F.DIAG_MESSAGE_HASHES)
        return cls(
            request_id=str(entry.get(F.REQUEST_ID, "")),
            ts=float(entry[F.TS]),
            kind=str(job.get(F.JOB_KIND) or ""),
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

    def chain(
        self, skip_answers: bool = False, hashes: Optional[List[str]] = None, root: Optional[str] = None
    ) -> List[str]:
        """Prefix hashes: element k identifies the settings plus the first k+1 messages."""
        out = []
        current = self.settings_hash if root is None else root
        for index, message_hash in enumerate((self.message_hashes if hashes is None else hashes) or []):
            role = self.roles[index] if index < len(self.roles) else ""
            part = ANSWER_PLACEHOLDER if skip_answers and role == ASSISTANT_ROLE else message_hash
            current = hashlib.sha256(f"{current}|{part}".encode()).hexdigest()
            out.append(current)
        return out

    def key(self, estimate: str) -> Optional[str]:
        if estimate != SIMULATED or self.message_hashes is None:
            return self.fingerprint
        chain = self.chain(skip_answers=True)
        return chain[-1] if chain else self.settings_hash


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

    @property
    def first_difference_role(self) -> str:
        if self.request.search_hashes is None:
            return DIFF_UNHASHED
        if self.closest is None:
            return DIFF_SETTINGS
        if self.matched_messages < len(self.request.roles):
            return self.request.roles[self.matched_messages] or DIFF_UNKNOWN_ROLE
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
        self.by_pr: Dict[str, Dict[str, List[Record]]] = {}
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
        merged_at = self.merged.get(record.pr)
        if merged_at is not None and self.merge_build.get(record.pr) == record.build_key:
            best_copy = self.merge_copy.get(key)
            if best_copy is None or merged_at < best_copy[0]:
                self.merge_copy[key] = (merged_at, record)
        if record.pr:
            self.by_pr.setdefault(key, {}).setdefault(record.pr, []).append(record)

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
            for earlier in self.by_pr.get(key, {}).get(request.pr, []) if request.pr else []:
                if earlier.build != request.build and self.build_end[earlier.build_key] <= request.ts:
                    candidates.append((self.build_end[earlier.build_key], earlier))
        return min(candidates, key=lambda c: c[0])[1] if candidates else None


class KeySources(Sources):
    def __init__(self, records: List[Record], merged: Dict[str, float], require_storable: bool, estimate: str):
        self.estimate = estimate
        super().__init__(records, merged, require_storable)

    def keys_of(self, record: Record) -> List[str]:
        key = record.key(self.estimate)
        return [key]


class PrefixSources(Sources):
    """Every prefix of every request, for the closest-earlier-request search."""

    def keys_of(self, record: Record) -> List[str]:
        if record.search_hashes is None:
            return []
        root = record.search_settings_hash
        return [root] + record.chain(hashes=record.search_hashes, root=root)

    def closest(self, request: Record) -> Tuple[Optional[Record], int]:
        if request.search_hashes is None:
            return None, 0
        root = request.search_settings_hash
        best = self.find(root, request, nightly_only=False)
        if best is None:
            return None, 0
        matched = 0
        for prefix in request.chain(hashes=request.search_hashes, root=root):
            found = self.find(prefix, request, nightly_only=False)
            if found is None:
                break
            best, matched = found, matched + 1
        return best, matched


def iter_request_lines(paths: Iterable[str], skipped: Dict[str, int]) -> Iterator[Dict[str, Any]]:
    """Each request line in the files, parsed; counts the rest in `skipped` (not JSON, another event)."""
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    skipped[SKIP_NOT_JSON] = skipped.get(SKIP_NOT_JSON, 0) + 1
                    continue
                if not isinstance(entry, dict) or entry.get(F.EVENT) != F.REQUEST_EVENT:
                    skipped[SKIP_OTHER_EVENT] = skipped.get(SKIP_OTHER_EVENT, 0) + 1
                    continue
                yield entry


def load(paths: Iterable[str]) -> Tuple[List[Record], Dict[str, int]]:
    """Read JSON lines from each path; returns the records in time order and skip counts."""
    records: List[Record] = []
    skipped = {reason: 0 for reason in SKIP_REASONS}
    for entry in iter_request_lines(paths, skipped):
        if entry.get(F.ERROR) == F.ERROR_ANALYSIS_DROPPED:
            skipped[SKIP_ANALYSIS_DROPPED] += 1
            continue
        if entry.get(F.ERROR):
            skipped[SKIP_ERROR] += 1
            continue
        if not entry.get(F.FINGERPRINT):
            skipped[SKIP_NO_FINGERPRINT] += 1
            continue
        if (entry.get(F.JOB) or {}).get(F.JOB_KIND) not in JOB_KINDS:
            skipped[SKIP_UNTAGGED] += 1
            continue
        records.append(Record.from_line(entry))
    records.sort(key=lambda r: r.ts)
    return records, skipped


def iter_hits(
    records: List[Record], estimate: str, require_storable: bool = True, merged: Optional[Dict[str, float]] = None
) -> Iterator[Tuple[Record, Optional[Hit]]]:
    """Each presubmit request in time order, with its Hit under `estimate` or None.

    This is the seam a collision audit reads: every Hit pairs a request with
    the recording it would have been served from.
    """
    sources = KeySources(records, merged or {}, require_storable, estimate)
    # Simulated is high plus the matches it adds, never fewer.
    exact = KeySources(records, merged or {}, require_storable, HIGH) if estimate == SIMULATED else None
    for record in records:
        if record.kind == PRESUBMIT:
            recording = sources.find(record.key(estimate), record, nightly_only=estimate == LOW)
            if recording is None and exact is not None:
                recording = exact.find(record.fingerprint, record, nightly_only=False)
            yield record, (Hit(record, recording) if recording is not None else None)


def find_misses(
    records: List[Record], require_storable: bool = True, merged: Optional[Dict[str, float]] = None
) -> List[Miss]:
    """High-estimate misses, each with its closest earlier request."""
    prefixes = PrefixSources(records, merged or {}, require_storable)
    misses = []
    for record, hit in iter_hits(records, HIGH, require_storable, merged):
        if hit is None:
            closest, matched = prefixes.closest(record)
            misses.append(Miss(record, closest, matched))
    return misses


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
) -> Dict[str, Any]:
    presubmit = [r for r in records if r.kind == PRESUBMIT]
    total_tokens = sum(r.prompt_tokens for r in presubmit)
    report: Dict[str, Any] = {
        "requests": len(records),
        "presubmit_requests": len(presubmit),
        "nightly_requests": len(records) - len(presubmit),
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
    misses = sorted(find_misses(records, require_storable, merged), key=lambda m: m.request.prompt_tokens, reverse=True)
    by_role: Dict[str, int] = {}
    for miss in misses:
        by_role[miss.first_difference_role] = by_role.get(miss.first_difference_role, 0) + miss.request.prompt_tokens
    report["miss_tokens_by_first_difference"] = dict(sorted(by_role.items(), key=lambda kv: -kv[1]))
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
        }
        for m in misses[:top]
    ]
    return report


def render(report: Dict[str, Any], skipped: Dict[str, int]) -> str:
    lines = [
        f"requests: {report['requests']} ({report['presubmit_requests']} presubmit, {report['nightly_requests']} nightly);"
        f" presubmit prompt tokens: {report['presubmit_prompt_tokens']}",
        f"skipped lines: {skipped}; merge times given: {report['merge_times_given']}",
        "",
        "health (check after the first day):",
    ]
    lines += [f"  {name}: {value}" for name, value in report["health"].items()]
    lines += [
        "",
        "estimate    hits   token-weighted   cross-project   saved prompt / completion tokens   saved model time",
    ]
    for name, e in report["estimates"].items():
        lines.append(
            f"{name:<10} {e['hits']:>5}   {e['token_weighted_hit_percent']:>13}%   {e['cross_project_hit_percent']:>12}%"
            f"   {e['hit_prompt_tokens']} / {e['saved_completion_tokens']}   {e['saved_model_ms']} ms"
        )
    lines += ["", f"miss tokens by first differing message: {report['miss_tokens_by_first_difference']}", "",
              "top misses (prompt tokens, matched/total messages, first difference, closest earlier request):"]
    for m in report["top_misses"]:
        lines.append(
            f"  {m['request_id']}  {m['prompt_tokens']}  {m['matched_messages']}/{m['messages']}"
            f"  {m['first_difference_role']}  {m['closest_request_id']} ({m['closest_kind']})"
        )
    return "\n".join(lines)


def load_merged(path: Optional[str]) -> Dict[str, float]:
    if not path:
        return {}
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    return {str(pr): float(when) for pr, when in raw.items()}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("logs", nargs="+", help="JSONL files written by replay_proxy.py")
    parser.add_argument("--merged", help="JSON file mapping a pull request number to its merge time (epoch seconds)")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_MISSES, help="misses to list")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--ignore-storable", action="store_true", help="count sightings replay would not have saved")
    args = parser.parse_args(argv)
    records, skipped = load(args.logs)
    report = summarize(
        records, require_storable=not args.ignore_storable, top=args.top, merged=load_merged(args.merged), skipped=skipped
    )
    if args.json:
        report["skipped_lines"] = skipped
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render(report, skipped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
