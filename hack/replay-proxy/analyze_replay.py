"""What the replay lane served, per conversation, from the proxy's replay-mode lines.

Reads the JSON lines a proxy in replay mode writes (examples/inference-replay/
eval-design.md, Rollout step 2) and groups them into conversations: a request
whose messages extend an earlier request's messages, in the same job, belongs
to that request's conversation. Messages are compared by `diag_message_hashes`
under `diag_settings_hash`, which do not drift between requests of one
conversation.

Reported:
- per conversation: requests and prompt tokens, and how many of each were
  served before the first miss (after it the conversation runs live);
- overall: the token-weighted served rate, and the rate before first misses;
- first-miss causes ranked by tokens lost (the prompt tokens of the first miss
  and every request after it in its conversation), by the role of the first
  differing message and by the miss signature the proxy logged;
- with `--verdicts`, whether each conversation's run verdict matched the
  verdict of the run that recorded its first served answer;
- lost requests: replay lines whose analysis was dropped or failed, so they
  carry no hashes and join no conversation. They are counted, served or not;
  a conversation that lost a request not served may show its first miss late.

Usage: python3 analyze_replay.py LOG [LOG ...] [--verdicts VERDICTS.json] [--top N] [--json]
VERDICTS.json maps a build ID to "pass" or "fail".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

import schema as F

DEFAULT_TOP = 20
PERCENT = 100.0
PERCENT_DECIMALS = 1
CHAIN_SEPARATOR = "|"
VERDICT_MATCH = "match"
VERDICT_MISMATCH = "mismatch"
VERDICT_UNKNOWN = "unknown"
NO_SIGNATURE = ""


@dataclass
class Line:
    request_id: str
    ts: float
    scope: Tuple[str, str, str]
    build: str
    served: bool
    prompt_tokens: int
    chain: List[str]
    miss: Dict[str, Any]
    served_from_build: Optional[str]
    served_via: str
    # A line whose analysis was dropped or failed: no fingerprint, no hashes.
    lost: bool = False

    @classmethod
    def from_entry(cls, entry: Dict[str, Any]) -> "Line":
        job = entry.get(F.JOB) or {}
        chain, current = [], str(entry.get(F.DIAG_SETTINGS_HASH) or "")
        for message_hash in entry.get(F.DIAG_MESSAGE_HASHES) or []:
            current = hashlib.sha256(f"{current}{CHAIN_SEPARATOR}{message_hash}".encode()).hexdigest()
            chain.append(current)
        served_from = entry.get(F.SERVED_FROM) or {}
        return cls(
            request_id=str(entry.get(F.REQUEST_ID, "")),
            ts=float(entry[F.TS]),
            scope=(str(job.get(F.JOB_KIND) or ""), str(job.get(F.JOB_PROJECT) or ""), str(job.get(F.JOB_BUILD) or "")),
            build=str(job.get(F.JOB_BUILD) or ""),
            served=bool(entry.get(F.SERVED)),
            served_via=str(entry.get(F.SERVED_VIA) or ""),
            prompt_tokens=int(entry.get(F.PROMPT_TOKENS) or 0),
            chain=chain,
            miss=entry.get(F.MISS) or {},
            served_from_build=str(served_from.get(F.JOB_BUILD)) if served_from.get(F.JOB_BUILD) else None,
            lost=not entry.get(F.FINGERPRINT),
        )


@dataclass
class Conversation:
    lines: List[Line] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return sum(line.prompt_tokens for line in self.lines)

    def first_miss_index(self) -> Optional[int]:
        return next((i for i, line in enumerate(self.lines) if not line.served), None)

    def served_before_first_miss(self) -> Tuple[int, int]:
        end = self.first_miss_index()
        head = self.lines if end is None else self.lines[:end]
        return len(head), sum(line.prompt_tokens for line in head)

    def recorded_build(self) -> Optional[str]:
        return next((line.served_from_build for line in self.lines if line.served_from_build), None)


def load(paths: Iterable[str]) -> List[Line]:
    lines = []
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                # Lost lines too: they carry `mode` and `served`, and are counted apart.
                if (isinstance(entry, dict) and entry.get(F.EVENT) == F.REQUEST_EVENT
                        and entry.get(F.MODE) == F.MODE_REPLAY):
                    lines.append(Line.from_entry(entry))
    lines.sort(key=lambda line: line.ts)
    return lines


def conversations(lines: List[Line]) -> List[Conversation]:
    """Group lines: a request extending an earlier request's messages joins its conversation.

    A request joins the conversation whose last request is the longest prefix
    of its messages. Parallel workers open with identical requests, so several
    conversations can end at the same request; each continuation takes the
    earliest of them still waiting there, and only when none is waiting does
    it join the first conversation that ended there (a retry or a branch).
    """
    out: List[Conversation] = []
    # Conversations by the request they currently end at, earliest first.
    waiting: Dict[Tuple[Tuple[str, str, str], str], List[Conversation]] = {}
    # The first conversation each request ended, for a request that branches.
    ended: Dict[Tuple[Tuple[str, str, str], str], Conversation] = {}
    tips: Dict[int, Tuple[Tuple[str, str, str], str]] = {}
    for line in lines:
        if line.lost:
            continue
        found = None
        for prefix in reversed(line.chain[:-1]):
            key = (line.scope, prefix)
            if waiting.get(key):
                found = waiting[key].pop(0)
                break
            if key in ended:
                found = ended[key]
                old_tip = tips.get(id(found))
                if old_tip is not None and found in waiting.get(old_tip, []):
                    waiting[old_tip].remove(found)
                break
        if found is None:
            found = Conversation()
            out.append(found)
        found.lines.append(line)
        if line.chain:
            tip = (line.scope, line.chain[-1])
            waiting.setdefault(tip, []).append(found)
            ended.setdefault(tip, found)
            tips[id(found)] = tip
    return out


def percent(part: float, whole: float) -> float:
    return round(PERCENT * part / whole, PERCENT_DECIMALS) if whole else 0.0


def verdict_join(convs: List[Conversation], verdicts: Dict[str, str]) -> Dict[str, int]:
    """Per conversation: did its run's verdict match the recording run's?"""
    counts = {VERDICT_MATCH: 0, VERDICT_MISMATCH: 0, VERDICT_UNKNOWN: 0}
    for conv in convs:
        lane = verdicts.get(conv.lines[0].build)
        recorded = verdicts.get(conv.recorded_build() or "")
        if lane is None or recorded is None:
            counts[VERDICT_UNKNOWN] += 1
        else:
            counts[VERDICT_MATCH if lane == recorded else VERDICT_MISMATCH] += 1
    return counts


def summarize(lines: List[Line], verdicts: Optional[Dict[str, str]] = None, top: int = DEFAULT_TOP) -> Dict[str, Any]:
    lost_lines = [line for line in lines if line.lost]
    lines = [line for line in lines if not line.lost]
    convs = conversations(lines)
    total = sum(line.prompt_tokens for line in lines)
    served = sum(line.prompt_tokens for line in lines if line.served)
    via_slots = sum(line.prompt_tokens for line in lines if line.served and line.served_via == F.VIA_ANSWER_SLOTS)
    before = sum(conv.served_before_first_miss()[1] for conv in convs)
    by_role: Dict[str, int] = {}
    by_signature: Dict[Tuple[str, str], int] = {}
    per_conversation = []
    for conv in convs:
        served_requests, served_tokens = conv.served_before_first_miss()
        first = conv.first_miss_index()
        row: Dict[str, Any] = {
            "first_request_id": conv.lines[0].request_id,
            "build": conv.lines[0].build,
            "requests": len(conv.lines),
            "prompt_tokens": conv.tokens,
            "served_requests_before_first_miss": served_requests,
            "served_tokens_before_first_miss": served_tokens,
        }
        if first is not None:
            miss = conv.lines[first].miss
            lost = sum(line.prompt_tokens for line in conv.lines[first:])
            role = str(miss.get(F.MISS_ROLE) or "")
            signature = str(miss.get(F.MISS_SIGNATURE) or NO_SIGNATURE)
            by_role[role] = by_role.get(role, 0) + lost
            by_signature[(role, signature)] = by_signature.get((role, signature), 0) + lost
            row.update(first_miss_index=first, first_miss_role=role, first_miss_signature=signature,
                       tokens_lost=lost)
        per_conversation.append(row)
    report: Dict[str, Any] = {
        "requests": len(lines),
        "conversations": len(convs),
        "lost_requests": len(lost_lines),
        "lost_requests_served": sum(1 for line in lost_lines if line.served),
        "lost_requests_not_served": sum(1 for line in lost_lines if not line.served),
        "prompt_tokens": total,
        "served_token_percent": percent(served, total),
        "served_via_answer_slots_token_percent": percent(via_slots, total),
        "served_before_first_miss_token_percent": percent(before, total),
        "first_miss_tokens_lost_by_role": dict(sorted(by_role.items(), key=lambda kv: -kv[1])),
        "first_miss_tokens_lost_by_signature": [
            {"role": role, "signature": signature, "tokens_lost": lost}
            for (role, signature), lost in sorted(by_signature.items(), key=lambda kv: -kv[1])[:top]
        ],
        "per_conversation": sorted(per_conversation, key=lambda r: -r.get("tokens_lost", 0))[:top],
    }
    if verdicts is not None:
        report["verdicts"] = verdict_join(convs, verdicts)
    return report


def render(report: Dict[str, Any]) -> str:
    lines = [
        f"requests: {report['requests']} in {report['conversations']} conversations; prompt tokens {report['prompt_tokens']}",
        f"served: {report['served_token_percent']}% of prompt tokens"
        f" ({report['served_before_first_miss_token_percent']}% before each conversation's first miss;"
        f" {report['served_via_answer_slots_token_percent']}% through answer slots)",
        f"lost requests (analysis dropped or failed; in no conversation): {report['lost_requests']}"
        f" ({report['lost_requests_served']} served, {report['lost_requests_not_served']} not served)",
        f"first-miss tokens lost by role: {report['first_miss_tokens_lost_by_role']}",
        "first-miss signatures (tokens lost, role, signature):",
    ]
    lines += [f"  {s['tokens_lost']}  {s['role']}  {s['signature']}" for s in report["first_miss_tokens_lost_by_signature"]]
    if "verdicts" in report:
        lines.append(f"verdicts against the recording run: {report['verdicts']}")
    lines.append("conversations (requests, served before first miss, tokens lost):")
    lines += [
        f"  {c['first_request_id']}  {c['requests']}  {c['served_requests_before_first_miss']}  {c.get('tokens_lost', 0)}"
        for c in report["per_conversation"]
    ]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("logs", nargs="+", help="JSONL files written by replay_proxy.py in replay mode")
    parser.add_argument("--verdicts", help='JSON file mapping a build ID to "pass" or "fail"')
    parser.add_argument("--top", type=int, default=DEFAULT_TOP, help="rows to list")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    verdicts = None
    if args.verdicts:
        with open(args.verdicts, encoding="utf-8") as handle:
            verdicts = {str(k): str(v) for k, v in json.load(handle).items()}
    report = summarize(load(args.logs), verdicts, args.top)
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
