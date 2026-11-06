"""The replay lane's report: what was served, and whether replayed runs got the verdicts the nightly did.

The lane (examples/inference-replay/eval-design.md, Rollout step 2) runs a
roster of cases on main's code with the proxy in replay mode, served from what
recent nightlies recorded (fetch_cache.py). collect_shadow.py keeps the lane's
lines and stored text on the job's machine and calls this from its last pass.
It writes, into `--out`:

- `replay.json`: analyze_replay.py's report over the lane's lines: per
  conversation, what was served before the first miss, and the first-miss
  causes ranked by tokens lost;
- `verdicts.json`: per case, the lane's repetitions (outcome, requests and
  prompt tokens, how many were served) beside the outcomes of the same case in
  each nightly that served it; and, per conversation that was served anything,
  whether its run's verdict matched the recording's. Only answers from runs
  that passed are published (publish_cache.py), so a recording's verdict is a
  pass, and a mismatch is a replayed run that failed. A conversation whose run
  can't be told (run_attribution.py) or whose nightly left no record is
  `unknown`;
- `summary.txt`: the counts, for the job log.

Both JSON files hold hashes and scrubbed snippets of requests, so they go to the
private bucket only. The summary holds case names and counts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

import analyze_replay
import publish_cache as P
import run_attribution as RA

REPLAY_REPORT = "replay.json"
VERDICTS_REPORT = "verdicts.json"
SUMMARY = "summary.txt"
MATCH = analyze_replay.VERDICT_MATCH
MISMATCH = analyze_replay.VERDICT_MISMATCH
UNKNOWN = analyze_replay.VERDICT_UNKNOWN
PERCENT = 100.0
PERCENT_DECIMALS = 1
TEXT_ENCODING = "utf-8"


def percent(part: float, whole: float) -> float:
    return round(PERCENT * part / whole, PERCENT_DECIMALS) if whole else 0.0


def lane_segments(keep_dir: str) -> List[str]:
    return [path for _, pod_dir in P.pod_dirs(keep_dir) for path in P.segment_paths(pod_dir)]


def case_of(run_key: str) -> str:
    return run_key.rsplit(RA.RUN_KEY_SEPARATOR, 1)[0]


def nightly_outcomes(nightly_runs: Dict[str, List[Dict[str, Any]]], build: str, case: str) -> Optional[Dict[str, Any]]:
    runs = [r for r in nightly_runs.get(build, []) if r.get(P.RUN_CASE) == case]
    if not runs:
        return None
    scored = [r for r in runs if r.get(P.RUN_OUTCOME) in (RA.REP_OUTCOME_PASS, RA.REP_OUTCOME_FAIL)]
    return {"passes": sum(1 for r in scored if r.get(P.RUN_OUTCOME) == RA.REP_OUTCOME_PASS), "scored": len(scored),
            "outcomes": [str(r.get(P.RUN_OUTCOME)) for r in sorted(runs, key=lambda r: r.get(P.RUN_REP, 0))]}


def verdicts(keep_dir: str, verdicts_dir: str, nightly_runs: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    runs = RA.load_runs(verdicts_dir)
    by_key = {run.key: run for run in runs}
    segments = lane_segments(keep_dir)
    logs = RA.load_logs(segments)
    attribution = RA.Attribution(logs, RA.Store(keep_dir), runs, final=True)
    # A line's run: the one its own cards name, or else the one its conversation's
    # lines name (a front door's opening names no card; its continuation does).
    # These are counts for the report, never a decision to publish anything.
    own = {line.request_id: attribution.run_of(line) for line in logs.lines}
    lines = analyze_replay.load(segments)
    convs = analyze_replay.conversations([l for l in lines if not l.lost])
    run_of: Dict[str, Optional[str]] = dict(own)
    for conv in convs:
        keys = {own.get(l.request_id) for l in conv.lines} - {None}
        if len(keys) == 1:
            for l in conv.lines:
                run_of[l.request_id] = run_of.get(l.request_id) or next(iter(keys))
    per_run: Dict[str, Dict[str, Any]] = {}
    unattributed_tokens = 0
    for line in logs.lines:
        key = run_of.get(line.request_id)
        if key is None:
            unattributed_tokens += line.prompt_tokens
            continue
        row = per_run.setdefault(key, {"requests": 0, "served_requests": 0, "prompt_tokens": 0,
                                       "served_tokens": 0, "served_from": set()})
        row["requests"] += 1
        row["prompt_tokens"] += line.prompt_tokens
        if line.served:
            row["served_requests"] += 1
            row["served_tokens"] += line.prompt_tokens
            if line.served_from_build:
                row["served_from"].add(line.served_from_build)
    cases: Dict[str, Dict[str, Any]] = {}
    for run in sorted(runs, key=lambda r: (r.case, r.rep)):
        case = cases.setdefault(run.case, {"lane": {"passes": 0, "scored": 0, "runs": []}, "nightly": {}})
        row = per_run.get(run.key, {})
        if run.outcome in (RA.REP_OUTCOME_PASS, RA.REP_OUTCOME_FAIL):
            case["lane"]["scored"] += 1
            case["lane"]["passes"] += int(run.passed)
        served_from = sorted(row.get("served_from", set()))
        case["lane"]["runs"].append({
            "rep": run.rep, "outcome": run.outcome, "requests": row.get("requests", 0),
            "served_requests": row.get("served_requests", 0), "prompt_tokens": row.get("prompt_tokens", 0),
            "served_token_percent": percent(row.get("served_tokens", 0), row.get("prompt_tokens", 0)),
            "served_from": served_from,
        })
        for build in served_from:
            outcomes = nightly_outcomes(nightly_runs, build, run.case)
            case["nightly"][build] = outcomes if outcomes is not None else UNKNOWN
    # Per served conversation: the lane run's verdict against the recording's.
    conversation_counts = {MATCH: 0, MISMATCH: 0, UNKNOWN: 0}
    mismatched: List[Dict[str, Any]] = []
    for conv in convs:
        if not any(l.served for l in conv.lines):
            continue
        keys = {run_of.get(l.request_id) for l in conv.lines} - {None}
        recorded = conv.recorded_build()
        if len(keys) != 1 or not recorded or recorded not in nightly_runs:
            conversation_counts[UNKNOWN] += 1
            continue
        key = next(iter(keys))
        run = by_key.get(key)  # type: ignore[arg-type]
        if run is None:
            conversation_counts[UNKNOWN] += 1
        elif run.passed:
            conversation_counts[MATCH] += 1
        else:
            conversation_counts[MISMATCH] += 1
            mismatched.append({"run": key, "outcome": run.outcome, "recorded_build": recorded,
                               "first_request_id": conv.lines[0].request_id})
    return {
        "cases": cases,
        "served_conversations": conversation_counts,
        "mismatched_conversations": mismatched,
        "unattributed_prompt_tokens": unattributed_tokens,
        "lost_lines": logs.lost,
    }


def summary_text(replay: Dict[str, Any], report: Dict[str, Any]) -> str:
    out = [
        f"replay lane: {replay['requests']} requests in {replay['conversations']} conversations;"
        f" served {replay['served_token_percent']}% of prompt tokens"
        f" ({replay['served_before_first_miss_token_percent']}% before each conversation's first miss,"
        f" {replay['served_via_answer_slots_token_percent']}% through answer slots)",
        f"served conversations against the recording's verdict: {report['served_conversations']}",
    ]
    for name, case in sorted(report["cases"].items()):
        lane = case["lane"]
        served = ", ".join(f"rep {r['rep']} {r['outcome']} {r['served_token_percent']}%" for r in lane["runs"])
        nightly = "; ".join(
            f"{build}: {v['passes']}/{v['scored']}" if isinstance(v, dict) else f"{build}: {v}"
            for build, v in sorted(case["nightly"].items())) or "served nothing"
        out.append(f"  {name}: lane {lane['passes']}/{lane['scored']} ({served}); nightly {nightly}")
    return "\n".join(out) + "\n"


def write_report(keep_dir: str, verdicts_dir: str, nightly_runs_path: str, out_dir: str) -> str:
    nightly_runs = RA.read_json(nightly_runs_path) if nightly_runs_path else None
    if not isinstance(nightly_runs, dict):
        nightly_runs = {}
    replay = analyze_replay.summarize(analyze_replay.load(lane_segments(keep_dir)))
    report = verdicts(keep_dir, verdicts_dir, nightly_runs)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, REPLAY_REPORT), "w", encoding=TEXT_ENCODING) as handle:
        json.dump(replay, handle, indent=2, sort_keys=True)
    with open(os.path.join(out_dir, VERDICTS_REPORT), "w", encoding=TEXT_ENCODING) as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
    text = summary_text(replay, report)
    with open(os.path.join(out_dir, SUMMARY), "w", encoding=TEXT_ENCODING) as handle:
        handle.write(text)
    return text


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--keep-dir", required=True, help="the lane collector's kept lines and store")
    parser.add_argument("--verdicts-dir", required=True, help="where bench-gate wrote case-<name>.json")
    parser.add_argument("--nightly-runs", default="", help="fetch_cache.py's runs per recording build")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    sys.stdout.write(write_report(args.keep_dir, args.verdicts_dir, args.nightly_runs, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
