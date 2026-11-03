"""Estimate what replay would have saved, from the shadow proxy's JSON lines.

Reads the lines replay_proxy.py writes (one per chat request, collected from
each job with `kubectl logs -f`), in time order, and asks of every presubmit
request whether its fingerprint was seen earlier
(examples/inference-replay/eval-design.md, Rollout step 1):

- low estimate: seen earlier from a nightly;
- high estimate: seen earlier from a nightly or from another presubmit build
  (a build never replays its own answers);
- replay-simulated: as high, but each assistant message in the history counts
  as the answer the request before it would have been served. In shadow mode
  every answer is a fresh sample, so a later request matches exactly only when
  the model repeated every earlier answer word for word; under replay those
  answers would have been the recorded ones. This is an approximation: it
  assumes the tool calls of the recorded answer produced the tool results
  this run saw.

A sighting counts as a recording only when replay would have saved it (the
line's `unstorable` list is empty); `--ignore-storable` drops that rule.

Also reported: the share of hits whose first sighting was in another project,
the tokens and model time hits would save, and for misses the closest earlier
request by longest matching per-message-hash prefix, ranked by tokens lost.
The stored, scrubbed text of both is under their request_id for a diff.

A collision audit (did two requests that matched lead to different results?)
is a later step: it joins each `Hit` from `iter_hits` with run verdicts and the
stored text. Nothing here reads verdicts yet.

Usage: python3 analyze_shadow.py LOG [LOG ...] [--top N] [--json] [--ignore-storable]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

LOG_EVENT = "replay_shadow_request"
NIGHTLY = "nightly"
PRESUBMIT = "presubmit"
JOB_KINDS = (NIGHTLY, PRESUBMIT)
ASSISTANT_ROLE = "assistant"
# Stands in for an assistant message in the replay-simulated key.
SIMULATED_ANSWER = "answer-to-prefix"
LOW, HIGH, SIMULATED = "low", "high", "simulated"
ESTIMATES = (LOW, HIGH, SIMULATED)
DEFAULT_TOP_MISSES = 20
PERCENT = 100.0
PERCENT_DECIMALS = 1


@dataclass
class Record:
    """One logged chat request."""

    request_id: str
    ts: float
    kind: str
    project: str
    build: str
    fingerprint: str
    settings_hash: str
    message_hashes: List[str]
    roles: List[str]
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    storable: bool

    @classmethod
    def from_line(cls, entry: Dict[str, Any]) -> "Record":
        job = entry.get("job") or {}
        return cls(
            request_id=str(entry.get("request_id", "")),
            ts=float(entry["ts"]),
            kind=str(job.get("kind") or ""),
            project=str(job.get("project") or ""),
            build=str(job.get("build") or ""),
            fingerprint=str(entry["fingerprint"]),
            settings_hash=str(entry.get("settings_hash") or ""),
            message_hashes=list(entry.get("message_hashes") or []),
            roles=list(entry.get("roles") or []),
            prompt_tokens=int(entry.get("prompt_tokens") or 0),
            completion_tokens=int(entry.get("completion_tokens") or 0),
            latency_ms=int(entry.get("latency_ms") or 0),
            storable=not entry.get("unstorable"),
        )

    def chain(self, simulate_answers: bool = False) -> List[str]:
        """Prefix hashes: element k identifies the settings plus the first k+1 messages."""
        out = []
        current = self.settings_hash
        for index, message_hash in enumerate(self.message_hashes):
            role = self.roles[index] if index < len(self.roles) else ""
            part = SIMULATED_ANSWER if simulate_answers and role == ASSISTANT_ROLE else message_hash
            current = hashlib.sha256(f"{current}|{part}".encode()).hexdigest()
            out.append(current)
        return out

    def key(self, estimate: str) -> str:
        if estimate != SIMULATED:
            return self.fingerprint
        chain = self.chain(simulate_answers=True)
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
        if self.matched_messages < len(self.request.roles):
            return self.request.roles[self.matched_messages] or "unknown"
        return "settings" if self.closest is None else "none"


@dataclass
class FirstSightings:
    """The earliest recording of each key: from a nightly, and per presubmit build."""

    nightly: Dict[str, Record] = field(default_factory=dict)
    presubmit: Dict[str, Dict[str, Record]] = field(default_factory=dict)

    def add(self, key: str, record: Record) -> None:
        if record.kind == NIGHTLY:
            self.nightly.setdefault(key, record)
        else:
            self.presubmit.setdefault(key, {}).setdefault(record.build, record)

    def find(self, key: str, request: Record, include_presubmits: bool) -> Optional[Record]:
        candidates = [self.nightly[key]] if key in self.nightly else []
        if include_presubmits:
            candidates += [r for build, r in self.presubmit.get(key, {}).items() if build != request.build]
        return min(candidates, key=lambda r: r.ts) if candidates else None


@dataclass
class PrefixIndex:
    """Prefix hash -> the first earlier request with that prefix, and the first from another build."""

    first: Dict[str, Record] = field(default_factory=dict)
    other_build: Dict[str, Record] = field(default_factory=dict)

    def add(self, record: Record) -> None:
        for prefix in [record.settings_hash] + record.chain():
            first = self.first.setdefault(prefix, record)
            if first.build != record.build or first.kind != record.kind:
                self.other_build.setdefault(prefix, record)

    def lookup(self, prefix: str, request: Record) -> Optional[Record]:
        first = self.first.get(prefix)
        if first is None:
            return None
        if first.kind == NIGHTLY or first.build != request.build:
            return first
        alt = self.other_build.get(prefix)
        return alt if alt is not None and (alt.kind == NIGHTLY or alt.build != request.build) else None

    def closest(self, request: Record) -> Tuple[Optional[Record], int]:
        best = self.lookup(request.settings_hash, request)
        if best is None:
            return None, 0
        matched = 0
        for prefix in request.chain():
            found = self.lookup(prefix, request)
            if found is None:
                break
            best, matched = found, matched + 1
        return best, matched


def load(paths: Iterable[str]) -> Tuple[List[Record], Dict[str, int]]:
    """Read JSON lines from each path; returns the records in time order and skip counts."""
    records, skipped = [], {"not_json": 0, "other_event": 0, "untagged": 0, "no_fingerprint": 0}
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    skipped["not_json"] += 1
                    continue
                if not isinstance(entry, dict) or entry.get("event") != LOG_EVENT:
                    skipped["other_event"] += 1
                    continue
                if not entry.get("fingerprint"):
                    skipped["no_fingerprint"] += 1
                    continue
                if (entry.get("job") or {}).get("kind") not in JOB_KINDS:
                    skipped["untagged"] += 1
                    continue
                records.append(Record.from_line(entry))
    records.sort(key=lambda r: r.ts)
    return records, skipped


def iter_hits(records: List[Record], estimate: str, require_storable: bool = True) -> Iterator[Tuple[Record, Optional[Hit]]]:
    """Each presubmit request in time order, with its Hit under `estimate` or None.

    This is the seam a collision audit reads: every Hit pairs a request with
    the recording it would have been served from.
    """
    sightings = FirstSightings()
    for record in records:
        key = record.key(estimate)
        if record.kind == PRESUBMIT:
            recording = sightings.find(key, record, include_presubmits=estimate != LOW)
            yield record, (Hit(record, recording) if recording is not None else None)
        if record.storable or not require_storable:
            sightings.add(key, record)


def find_misses(records: List[Record], require_storable: bool = True) -> List[Miss]:
    """High-estimate misses, each with its closest earlier request."""
    index = PrefixIndex()
    sightings = FirstSightings()
    misses = []
    for record in records:
        if record.kind == PRESUBMIT and sightings.find(record.fingerprint, record, include_presubmits=True) is None:
            closest, matched = index.closest(record)
            misses.append(Miss(record, closest, matched))
        if record.storable or not require_storable:
            sightings.add(record.fingerprint, record)
            index.add(record)
    return misses


def percent(part: float, whole: float) -> float:
    return round(PERCENT * part / whole, PERCENT_DECIMALS) if whole else 0.0


def summarize(records: List[Record], require_storable: bool = True, top: int = DEFAULT_TOP_MISSES) -> Dict[str, Any]:
    presubmit = [r for r in records if r.kind == PRESUBMIT]
    total_tokens = sum(r.prompt_tokens for r in presubmit)
    report: Dict[str, Any] = {
        "requests": len(records),
        "presubmit_requests": len(presubmit),
        "nightly_requests": len(records) - len(presubmit),
        "presubmit_prompt_tokens": total_tokens,
        "unstorable_sightings": sum(1 for r in records if not r.storable),
        "estimates": {},
    }
    for estimate in ESTIMATES:
        hits = [hit for _, hit in iter_hits(records, estimate, require_storable) if hit is not None]
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
    misses = sorted(find_misses(records, require_storable), key=lambda m: m.request.prompt_tokens, reverse=True)
    by_role: Dict[str, int] = {}
    for miss in misses:
        by_role[miss.first_difference_role] = by_role.get(miss.first_difference_role, 0) + miss.request.prompt_tokens
    report["miss_tokens_by_first_difference"] = dict(sorted(by_role.items(), key=lambda kv: -kv[1]))
    report["top_misses"] = [
        {
            "request_id": m.request.request_id,
            "project": m.request.project,
            "build": m.request.build,
            "prompt_tokens": m.request.prompt_tokens,
            "messages": len(m.request.message_hashes),
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
        f"skipped lines: {skipped}; sightings replay would not have saved: {report['unstorable_sightings']}",
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


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("logs", nargs="+", help="JSONL files written by replay_proxy.py")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_MISSES, help="misses to list")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--ignore-storable", action="store_true", help="count sightings replay would not have saved")
    args = parser.parse_args(argv)
    records, skipped = load(args.logs)
    report = summarize(records, require_storable=not args.ignore_storable, top=args.top)
    if args.json:
        report["skipped_lines"] = skipped
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render(report, skipped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
