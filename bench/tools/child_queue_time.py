#!/usr/bin/env python3
# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""How long an AgentProfile's child tasks waited for a worker, from a smoke run's logs.

Reads a run's artifacts: the gateway logs (``a2a-gateway-*.log``), the
dispatcher logs (``a2a-dispatcher-*.log``) and, optionally, the per-repetition
results (``results_<case>_rep<N>.json``). Each log line is
``[pod/.../container] {json}``; the JSON after the first ``{`` is read and a
line without one is skipped.

For the child tasks of one profile (``--profile``, default ``cluster``) it
reports:

- queue time: the gateway's ``ingress`` of the child's task id to the
  dispatcher's ``task confirmed to a worker`` for it (``kind`` child, the
  profile): p50, p90, p95, max, and how many waited over 600s;
- never confirmed: children the dispatcher saw and never confirmed, each with
  the last thing the dispatcher said about it;
- fan-out: how many children each parent delegated, from the gateway lines
  that carry both ``child`` and ``parent``;
- what binds: the most waiting submissions and the oldest wait in any
  ``dispatcher health`` line, the dispatcher lines naming an admission cap
  reason, and how many top-level tasks (not ``kind`` child) waited over 5s
  from ingress to confirm;
- per level, when a results file carries a ``[bench:burst-reads]`` record
  (``bench/kube_agents_bench/burst_reads.py``): each child is attributed to
  its conversation (the child's ingress line names it, else its parent's
  does) and so to the level that conversation ran in.

Usage:
    python3 bench/tools/child_queue_time.py <artifacts-dir-or-files>... [--profile cluster] [--json]

Standard library only, so it runs on any artifacts directory without the
bench package installed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_PROFILE = "cluster"
GATEWAY_GLOB = "a2a-gateway-*.log"
DISPATCHER_GLOB = "a2a-dispatcher-*.log"
RESULTS_GLOB = "results_*_rep*.json"
GATEWAY_PREFIX = "a2a-gateway-"
DISPATCHER_PREFIX = "a2a-dispatcher-"
RESULTS_PATTERN = re.compile(r"\Aresults_.+_rep\d+\.json\Z")

MSG_INGRESS = "ingress"
MSG_CONFIRMED = "task confirmed to a worker"
MSG_HEALTH = "dispatcher health"
KIND_CHILD = "child"
# The dispatcher's admission refusals (a2a/dispatch/dispatcher.go, admit).
CAP_REASONS = ("profile concurrency", "install ceiling", "the free slots are reserved for other kinds")
# A child that waited longer than this is counted on its own.
LONG_WAIT_SECONDS = 600.0
# A top-level task that waited longer than this from ingress to confirm counts as delayed.
TOP_LEVEL_DELAY_SECONDS = 5.0
NANOS_PER_SECOND = 1e9
PERCENTILES = (("p50", 0.50), ("p90", 0.90), ("p95", 0.95))
# The burst-reads record's trajectory entry and metadata key (burst_reads.ENTRY).
BURST_ENTRY = "harness_burst_reads"
BURST_METADATA_KEY = "burst_reads"
# The prefix the gateway puts on an inject conversation key.
CONVERSATION_PREFIX = "inject:"
# Fractional seconds past microseconds, which datetime does not take.
_SUB_MICRO = re.compile(r"(\.\d{6})\d+")


def parse_time(value: str) -> datetime:
    """An RFC 3339 timestamp, nanoseconds truncated to microseconds."""
    text = _SUB_MICRO.sub(r"\1", value.strip())
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def log_records(lines: Iterable[str]) -> Iterable[dict[str, Any]]:
    """The JSON object after the first ``{`` of each line that holds one."""
    for line in lines:
        start = line.find("{")
        if start < 0:
            continue
        try:
            record = json.loads(line[start:])
        except ValueError:
            continue
        if isinstance(record, dict):
            yield record


def _when(record: Mapping[str, Any]) -> datetime | None:
    raw = record.get("time")
    if not isinstance(raw, str):
        return None
    try:
        return parse_time(raw)
    except ValueError:
        return None


def bare_conversation(key: str) -> str:
    """A conversation key without the gateway's inject prefix."""
    return key.removeprefix(CONVERSATION_PREFIX)


def stats(values: list[float]) -> dict[str, Any]:
    """n, the percentiles (nearest rank), max and the count over the long wait."""
    ordered = sorted(values)
    n = len(ordered)
    out: dict[str, Any] = {"n": n}
    for name, q in PERCENTILES:
        out[name] = ordered[min(n - 1, int(q * n))] if n else None
    out["max"] = ordered[-1] if n else None
    out["over_600s"] = sum(v > LONG_WAIT_SECONDS for v in ordered)
    return out


class Gateway:
    """What the gateway logs say: each task's ingress, its conversation, and
    who delegated it."""

    def __init__(self) -> None:
        self.ingress: dict[str, datetime] = {}
        self.conversation: dict[str, str] = {}
        self.addressee: dict[str, str] = {}
        self.children: dict[str, set[str]] = defaultdict(set)
        self.parent: dict[str, str] = {}

    def read(self, records: Iterable[Mapping[str, Any]]) -> None:
        for record in records:
            conversation = record.get("conversation")
            if record.get("msg") == MSG_INGRESS and record.get("taskId"):
                task = str(record["taskId"])
                when = _when(record)
                if when is not None:
                    self.ingress.setdefault(task, when)
                if record.get("addressee"):
                    self.addressee.setdefault(task, str(record["addressee"]))
                if isinstance(conversation, str) and conversation:
                    self.conversation.setdefault(task, conversation)
            child, parent = record.get("child"), record.get("parent")
            if child and parent:
                self.children[str(parent)].add(str(child))
                self.parent.setdefault(str(child), str(parent))
                if record.get("addressee"):
                    self.addressee.setdefault(str(child), str(record["addressee"]))
                if isinstance(conversation, str) and conversation:
                    self.conversation.setdefault(str(parent), conversation)

    def conversation_of(self, task: str) -> str:
        """The conversation a task ran on: its own ingress line's, else its parent's."""
        return self.conversation.get(task) or self.conversation.get(self.parent.get(task, ""), "")


class Dispatcher:
    """What the dispatcher logs say: each task's kind, profile, confirm and
    last message, its health lines, and its cap refusals."""

    def __init__(self) -> None:
        self.kind: dict[str, str] = {}
        self.profile: dict[str, str] = {}
        self.confirmed: dict[str, datetime] = {}
        self.last_msg: dict[str, str] = {}
        self.max_waiting_total = 0
        self.max_waiting: dict[str, int] = {}
        self.max_no_worker: dict[str, int] = {}
        self.max_oldest_wait: dict[str, float] = {}
        self.cap_reasons: Counter[str] = Counter()

    def read(self, records: Iterable[Mapping[str, Any]]) -> None:
        for record in records:
            msg = str(record.get("msg") or "")
            if msg == MSG_HEALTH:
                self._health(record)
            text = json.dumps(record)
            for reason in CAP_REASONS:
                if reason in text:
                    self.cap_reasons[reason] += 1
            task = record.get("task")
            if not task:
                continue
            task = str(task)
            if record.get("kind"):
                self.kind.setdefault(task, str(record["kind"]))
            if record.get("profile"):
                self.profile.setdefault(task, str(record["profile"]))
            self.last_msg[task] = msg
            if msg == MSG_CONFIRMED:
                when = _when(record)
                if when is not None:
                    self.confirmed.setdefault(task, when)

    def _health(self, record: Mapping[str, Any]) -> None:
        waiting = record.get("waiting")
        if isinstance(waiting, Mapping):
            counts = {str(k): v for k, v in waiting.items() if isinstance(v, int)}
            self.max_waiting_total = max(self.max_waiting_total, sum(counts.values()))
            for durable, n in counts.items():
                self.max_waiting[durable] = max(self.max_waiting.get(durable, 0), n)
        no_worker = record.get("noWorker")
        if isinstance(no_worker, Mapping):
            for durable, n in no_worker.items():
                if isinstance(n, int):
                    self.max_no_worker[str(durable)] = max(self.max_no_worker.get(str(durable), 0), n)
        oldest = record.get("oldestWait")
        if isinstance(oldest, Mapping):
            for durable, ns in oldest.items():
                if isinstance(ns, (int, float)):
                    seconds = ns / NANOS_PER_SECOND
                    self.max_oldest_wait[str(durable)] = max(self.max_oldest_wait.get(str(durable), 0.0), seconds)


def burst_levels(results: Iterable[Any]) -> dict[str, int]:
    """Conversation key (unprefixed) to the burst-reads level it ran in, from
    the results records' :data:`BURST_ENTRY` trajectory entries or their
    ``metadata.burst_reads``."""
    out: dict[str, int] = {}
    for doc in results:
        for record in doc if isinstance(doc, list) else [doc]:
            if not isinstance(record, Mapping):
                continue
            sources = [
                e.get("args")
                for e in record.get("trajectory") or []
                if isinstance(e, Mapping) and e.get("name") == BURST_ENTRY
            ]
            metadata = record.get("metadata")
            if isinstance(metadata, Mapping):
                sources.append(metadata.get(BURST_METADATA_KEY))
            for source in sources:
                if not isinstance(source, Mapping):
                    continue
                for level in source.get("levels") or []:
                    if not isinstance(level, Mapping):
                        continue
                    for conv in level.get("conversations") or []:
                        if isinstance(conv, Mapping) and conv.get("conversation"):
                            out[bare_conversation(str(conv["conversation"]))] = int(level.get("level") or 0)
    return out


def analyse(gateway: Gateway, dispatcher: Dispatcher, profile: str, levels: Mapping[str, int] | None = None) -> dict[str, Any]:
    """The report, as a JSON-ready dict."""
    children = sorted(t for t, k in dispatcher.kind.items() if k == KIND_CHILD and dispatcher.profile.get(t) == profile)
    waits: dict[str, float] = {}
    for task in children:
        if task in dispatcher.confirmed and task in gateway.ingress:
            waits[task] = (dispatcher.confirmed[task] - gateway.ingress[task]).total_seconds()
    never = [
        {"task": t, "last_msg": dispatcher.last_msg.get(t, "")} for t in children if t not in dispatcher.confirmed
    ]
    no_ingress = sum(1 for t in children if t in dispatcher.confirmed and t not in gateway.ingress)

    top_level = [t for t, k in dispatcher.kind.items() if k != KIND_CHILD and t in dispatcher.confirmed and t in gateway.ingress]
    delayed = [
        t for t in top_level if (dispatcher.confirmed[t] - gateway.ingress[t]).total_seconds() > TOP_LEVEL_DELAY_SECONDS
    ]

    def _fanout(only_profile: bool) -> dict[str, Any]:
        sizes = []
        for kids in gateway.children.values():
            picked = [
                k for k in kids
                if not only_profile or profile in (gateway.addressee.get(k), dispatcher.profile.get(k))
            ]
            if picked:
                sizes.append(len(picked))
        return {"parents": len(sizes), "children_per_parent": {str(k): v for k, v in sorted(Counter(sizes).items())}}

    report: dict[str, Any] = {
        "profile": profile,
        "queue_time_seconds": stats(list(waits.values())),
        "children_seen": len(children),
        "confirmed_without_ingress": no_ingress,
        "never_confirmed": {"count": len(never), "tasks": never, "last_msgs": dict(Counter(n["last_msg"] for n in never))},
        "fan_out": {"all": _fanout(False), profile: _fanout(True)},
        "binds": {
            "max_waiting_total": dispatcher.max_waiting_total,
            "max_waiting": dict(sorted(dispatcher.max_waiting.items())),
            "max_no_worker": dict(sorted(dispatcher.max_no_worker.items())),
            "max_oldest_wait_seconds": {k: round(v, 3) for k, v in sorted(dispatcher.max_oldest_wait.items())},
            "cap_reason_lines": {r: dispatcher.cap_reasons.get(r, 0) for r in CAP_REASONS},
            "top_level_confirmed": len(top_level),
            "top_level_delayed": len(delayed),
        },
    }
    if levels:
        by_level: dict[int, list[float]] = defaultdict(list)
        never_by_level: Counter[int] = Counter()
        unattributed = 0
        for task in children:
            level = levels.get(bare_conversation(gateway.conversation_of(task)))
            if level is None:
                unattributed += 1
                continue
            if task in waits:
                by_level[level].append(waits[task])
            elif task not in dispatcher.confirmed:
                never_by_level[level] += 1
        report["per_level"] = {
            str(level): {**stats(by_level.get(level, [])), "never_confirmed": never_by_level.get(level, 0)}
            for level in sorted(set(levels.values()))
        }
        report["per_level_unattributed"] = unattributed
    return report


def _fmt(value: Any) -> str:
    return "-" if value is None else f"{value:.0f}s"


def render(report: Mapping[str, Any]) -> str:
    """The report as plain text."""
    q = report["queue_time_seconds"]
    lines = [
        f"profile {report['profile']}: {report['children_seen']} child tasks seen by the dispatcher",
        (
            f"queue time (ingress -> confirmed): n={q['n']} p50={_fmt(q['p50'])} p90={_fmt(q['p90'])} "
            f"p95={_fmt(q['p95'])} max={_fmt(q['max'])} over600={q['over_600s']} "
            f"never={report['never_confirmed']['count']}"
        ),
    ]
    if report["confirmed_without_ingress"]:
        lines.append(f"confirmed with no gateway ingress line: {report['confirmed_without_ingress']}")
    for msg, n in sorted(report["never_confirmed"]["last_msgs"].items()):
        lines.append(f"  never confirmed, last said {msg!r}: {n}")
    for name, fan in report["fan_out"].items():
        dist = ", ".join(f"{k} child{'ren' if k != '1' else ''}: {v}" for k, v in fan["children_per_parent"].items())
        lines.append(f"fan-out ({name}): {fan['parents']} parents; {dist or 'none'}")
    binds = report["binds"]
    oldest = ", ".join(f"{k} {v:.0f}s" for k, v in binds["max_oldest_wait_seconds"].items()) or "none"
    waiting = ", ".join(f"{k} {v}" for k, v in binds["max_waiting"].items()) or "none"
    caps = ", ".join(f"{k!r} {v}" for k, v in binds["cap_reason_lines"].items())
    lines += [
        f"binds: max waiting {binds['max_waiting_total']} ({waiting}); max oldest wait: {oldest}",
        f"cap-reason lines: {caps}",
        (
            f"top-level delayed (>{TOP_LEVEL_DELAY_SECONDS:.0f}s ingress -> confirmed): "
            f"{binds['top_level_delayed']} of {binds['top_level_confirmed']}"
        ),
    ]
    if "per_level" in report:
        lines.append("per level:")
        lines.append(f"  {'level':>5} {'n':>4} {'p50':>6} {'p90':>6} {'p95':>6} {'max':>6} {'>600s':>5} {'never':>5}")
        for level, s in report["per_level"].items():
            lines.append(
                f"  {level:>5} {s['n']:>4} {_fmt(s['p50']):>6} {_fmt(s['p90']):>6} {_fmt(s['p95']):>6} "
                f"{_fmt(s['max']):>6} {s['over_600s']:>5} {s['never_confirmed']:>5}"
            )
        if report["per_level_unattributed"]:
            lines.append(f"  not attributed to a level: {report['per_level_unattributed']}")
    return "\n".join(lines)


def collect(paths: Iterable[str]) -> tuple[list[Path], list[Path], list[Path]]:
    """The gateway logs, dispatcher logs and results files among ``paths``
    (a directory is searched for each by name)."""
    gateway, dispatcher, results = [], [], []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            gateway += sorted(path.glob(GATEWAY_GLOB))
            dispatcher += sorted(path.glob(DISPATCHER_GLOB))
            results += sorted(path.glob(RESULTS_GLOB))
        elif path.name.startswith(GATEWAY_PREFIX):
            gateway.append(path)
        elif path.name.startswith(DISPATCHER_PREFIX):
            dispatcher.append(path)
        elif RESULTS_PATTERN.match(path.name):
            results.append(path)
        else:
            raise ValueError(f"{raw}: not a directory, {GATEWAY_GLOB}, {DISPATCHER_GLOB} or {RESULTS_GLOB}")
    return gateway, dispatcher, results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+", help="artifacts directories, or log and results files")
    parser.add_argument("--profile", default=DEFAULT_PROFILE, help="the AgentProfile whose children to report")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    try:
        gateway_files, dispatcher_files, result_files = collect(args.paths)
    except ValueError as exc:
        parser.error(str(exc))
    if not gateway_files or not dispatcher_files:
        parser.error("needs at least one gateway log and one dispatcher log")
    gateway, dispatcher = Gateway(), Dispatcher()
    for path in gateway_files:
        with path.open(errors="replace") as fh:
            gateway.read(log_records(fh))
    for path in dispatcher_files:
        with path.open(errors="replace") as fh:
            dispatcher.read(log_records(fh))
    docs = []
    for path in result_files:
        try:
            docs.append(json.loads(path.read_text(errors="replace")))
        except ValueError as exc:
            print(f"warning: {path}: not JSON ({exc}); skipped", file=sys.stderr)
    report = analyse(gateway, dispatcher, args.profile, burst_levels(docs))
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
