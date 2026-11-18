"""The replay lane's report: what was served, and whether replayed runs got the verdicts the nightly did.

The lane (examples/inference-replay/eval-design.md, Rollout step 2) runs a
roster of cases with the proxy in replay mode, served from what recent
nightlies recorded (fetch_cache.py), on the commit the newest recording night
ran (fetch_cache.py --print-commit), or on main when no record names one.
collect_shadow.py keeps the lane's lines and stored text on the job's machine
and calls this from its last pass, with that commit and where it came from
(`--commit`, `--commit-source`: hack/ci-env.sh reads REPLAY_LANE_COMMIT_SOURCE,
which the Prow job exports). It writes, into `--out`:

- `replay.json`: analyze_replay.py's report over the lane's lines: per
  conversation, what was served before the first miss, and the first-miss
  causes ranked by tokens lost;
- `verdicts.json`: per case, the lane's repetitions (outcome, requests and
  prompt tokens, how many were served); the case's outcomes on the newest
  recording night on the replayed commit (`newest_night`: the builds
  fetch_cache.py --night-out names for `--commit`, the night --print-commit
  chose), as graded when the lane fetched (the night is usually still running
  then, so a score can be partial), or that it had no graded run there by then,
  or that no such night is known, never an older night's; the outcomes of the same case in each build an answer was
  served from (`nightly`), which is where answers came from, not a verdict:
  uploads are write-once, so that is the night that first wrote the answer,
  often an older one on another commit; and, per conversation that was served anything,
  whether its run's verdict matched the recording's. Only answers from runs
  that passed are published (publish_cache.py), so a recording's verdict is a
  pass, and a mismatch is a replayed run that failed. A conversation whose run
  can't be told (run_attribution.py), whose run bench-gate graded as neither a
  pass nor a fail (infra, blocked, not_applicable), or whose recording night
  is in no record the fetch read is `unknown`, each counted by reason in
  `unknown_reasons`. The recording night is the one that first wrote the
  answer, which can be as old as the bucket's lifecycle; the fetch reads runs
  that far back;
- `summary.txt`: the commit replayed, the newest night's builds on it, and the counts. Its lines also go to the job log, where the
  collector masks long hex runs, the commit included; the file in the bucket keeps it whole.

The lane's safety cases run a live arm before the cache is seeded, then a
replayed one (hack/ci-eval-pr.sh, case_repetitions). The script names them in
`replay-lane-arms.json` beside the case files (`ARMS_FILE`): the cases, how
many leading repetitions were live, and when replay began. Each such case in
`verdicts.json` gets `arms`: per arm its passes, scored and run counts, the
share of its prompt tokens served, and each run's served share (a live arm's
should be 0). `replay.json` counts only lines from after replay began, so the
live arm's misses don't dilute it. `summary.txt` gets one line per safety case.

`verdicts.json` and `summary.txt` both name the commit replayed (`commit`,
empty when git could not say) and its source (`commit_source`: `recording`,
`main`, or empty when the job did not say).

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
import collect_shadow as CS
import fetch_cache as FC
import publish_cache as P
import run_attribution as RA

REPLAY_REPORT = "replay.json"
VERDICTS_REPORT = "verdicts.json"
SUMMARY = "summary.txt"
MATCH = analyze_replay.VERDICT_MATCH
MISMATCH = analyze_replay.VERDICT_MISMATCH
UNKNOWN = analyze_replay.VERDICT_UNKNOWN
# Why a served conversation's comparison is unknown, counted in the report.
RUN_UNATTRIBUTED = "run_unattributed"
RECORDING_BUILD_UNRECORDED = "recording_build_unrecorded"
NO_VERDICT = "no_verdict"
# The report's commit fields, and how the summary reads each source.
REPORT_COMMIT = "commit"
REPORT_COMMIT_SOURCE = "commit_source"
UNKNOWN_COMMIT = "an unknown commit"
# Per case, `newest_night` when the case has no outcome there: no graded run of
# it in the night's builds when the lane fetched their runs, or no night on the replayed commit is known (no night
# file, no commit, or a commit no build of the newest night ran: main, as a fallback).
NIGHT_NOT_RUN = "not_graded_at_fetch"
NIGHT_UNKNOWN = "no_night_on_commit"
REPORT_NIGHT = "newest_night"
# Per case and served-from build, `nightly` when the build's runs were read and
# none is the case's; a build whose runs weren't read at all stays UNKNOWN.
SERVED_FROM_NOT_RUN = "not_run"
COMMIT_SOURCE_TEXT = {
    CS.COMMIT_SOURCE_RECORDING: "the commit the newest recording night ran",
    CS.COMMIT_SOURCE_MAIN: "main, as a fallback: no recording named a commit, or it could not be fetched",
    "": "where it came from was not stated",
}
# The script's note of the live arm (write_replay_lane_arms in hack/ci-eval-pr.sh).
ARMS_FILE = "replay-lane-arms.json"
ARMS_LIVE_REPS = "live_reps"
ARMS_REPLAYED_FROM = "replayed_from"
ARMS_CASES = "cases"
ARM_LIVE = "live"
ARM_REPLAYED = "replayed"
PERCENT = 100.0
PERCENT_DECIMALS = 1
TEXT_ENCODING = "utf-8"


def percent(part: float, whole: float) -> float:
    return round(PERCENT * part / whole, PERCENT_DECIMALS) if whole else 0.0


def read_arms(verdicts_dir: str) -> Dict[str, Any]:
    """The live arm's note, or an empty one when the lane ran none (or the note is unreadable)."""
    arms = RA.read_json(os.path.join(verdicts_dir, ARMS_FILE))
    if not isinstance(arms, dict) or not isinstance(arms.get(ARMS_LIVE_REPS), int) \
            or not isinstance(arms.get(ARMS_CASES), list):
        return {}
    return arms


def arm_counts(runs: List[Dict[str, Any]], tokens: List[Dict[str, Any]]) -> Dict[str, Any]:
    """One arm of a safety case: passes, scored and runs, and how much of its prompt tokens was served."""
    scored = [r for r in runs if r["outcome"] in (RA.REP_OUTCOME_PASS, RA.REP_OUTCOME_FAIL)]
    return {
        "passes": sum(1 for r in scored if r["outcome"] == RA.REP_OUTCOME_PASS), "scored": len(scored),
        "runs": len(runs), "reps": [r["rep"] for r in runs],
        "served_token_percent": percent(sum(t.get("served_tokens", 0) for t in tokens),
                                        sum(t.get("prompt_tokens", 0) for t in tokens)),
        "run_served_token_percent": [r["served_token_percent"] for r in runs],
    }


def add_arms(cases: Dict[str, Any], per_run: Dict[str, Dict[str, Any]], arms: Dict[str, Any]) -> None:
    """Split each safety case's lane runs into its live and replayed arms (read_arms)."""
    live_reps = arms.get(ARMS_LIVE_REPS, 0)
    for name in arms.get(ARMS_CASES, []):
        case = cases.get(name)
        if case is None:
            continue
        split: Dict[str, Any] = {}
        for arm, in_arm in ((ARM_LIVE, lambda rep: rep <= live_reps), (ARM_REPLAYED, lambda rep: rep > live_reps)):
            runs = [r for r in case["lane"]["runs"] if in_arm(r["rep"])]
            tokens = [per_run.get(f"{name}{RA.RUN_KEY_SEPARATOR}{r['rep']}", {}) for r in runs]
            split[arm] = arm_counts(runs, tokens)
        case["arms"] = split


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


def night_builds(night_path: str, commit: str) -> Optional[List[str]]:
    """The newest night's builds that ran `commit` (fetch_cache.py --night-out), or None when none is known."""
    night = RA.read_json(night_path) if night_path and commit else None
    by_commit = night.get(FC.NIGHT_COMMIT_BUILDS) if isinstance(night, dict) else None
    builds = by_commit.get(commit) if isinstance(by_commit, dict) else None
    if not isinstance(builds, list) or not builds:
        return None
    return sorted(str(b) for b in builds)


def night_outcomes(nightly_runs: Dict[str, List[Dict[str, Any]]], builds: Optional[List[str]], case: str) -> Any:
    """A case's outcomes over the newest night's builds on the replayed commit, or why there are none."""
    if builds is None:
        return NIGHT_UNKNOWN
    ran = [(b, o) for b in builds for o in [nightly_outcomes(nightly_runs, b, case)] if o is not None]
    if not ran:
        return NIGHT_NOT_RUN
    return {"passes": sum(o["passes"] for _, o in ran), "scored": sum(o["scored"] for _, o in ran),
            "outcomes": [x for _, o in ran for x in o["outcomes"]], "builds": [b for b, _ in ran]}


def verdicts(keep_dir: str, verdicts_dir: str, nightly_runs: Dict[str, List[Dict[str, Any]]],
             newest_builds: Optional[List[str]] = None) -> Dict[str, Any]:
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
        case = cases.setdefault(run.case, {"lane": {"passes": 0, "scored": 0, "runs": []}, "nightly": {},
                                           REPORT_NIGHT: night_outcomes(nightly_runs, newest_builds, run.case)})
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
            if outcomes is None:
                outcomes = SERVED_FROM_NOT_RUN if build in nightly_runs else UNKNOWN
            case["nightly"][build] = outcomes
    # Per served conversation: the lane run's verdict against the recording's.
    conversation_counts = {MATCH: 0, MISMATCH: 0, UNKNOWN: 0}
    unknown_reasons: Dict[str, int] = {}
    mismatched: List[Dict[str, Any]] = []

    def unknown(reason: str) -> None:
        conversation_counts[UNKNOWN] += 1
        unknown_reasons[reason] = unknown_reasons.get(reason, 0) + 1

    for conv in convs:
        if not any(l.served for l in conv.lines):
            continue
        keys = {run_of.get(l.request_id) for l in conv.lines} - {None}
        recorded = conv.recorded_build()
        if len(keys) != 1:
            unknown(RUN_UNATTRIBUTED)
            continue
        # The night that first wrote the answer, which can be weeks old: the
        # fetch reads runs back to the bucket's lifecycle (fetch_cache.py).
        # Its verdict is the run that wrote the answer, so this is that night
        # and not the newest one: only a run that passed publishes.
        if not recorded or recorded not in nightly_runs:
            unknown(RECORDING_BUILD_UNRECORDED)
            continue
        key = next(iter(keys))
        run = by_key.get(key)  # type: ignore[arg-type]
        # Only a pass or a fail is a verdict; infra, blocked and not_applicable
        # say nothing about what replay served (bench-gate's RepResult outcomes).
        if run is None or run.outcome not in (RA.REP_OUTCOME_PASS, RA.REP_OUTCOME_FAIL):
            unknown(NO_VERDICT)
        elif run.passed:
            conversation_counts[MATCH] += 1
        else:
            conversation_counts[MISMATCH] += 1
            mismatched.append({"run": key, "outcome": run.outcome, "recorded_build": recorded,
                               "first_request_id": conv.lines[0].request_id})
    add_arms(cases, per_run, read_arms(verdicts_dir))
    return {
        "cases": cases,
        "served_conversations": conversation_counts,
        "unknown_reasons": unknown_reasons,
        "mismatched_conversations": mismatched,
        "unattributed_prompt_tokens": unattributed_tokens,
        "lost_lines": logs.lost,
    }


def commit_line(report: Dict[str, Any]) -> str:
    source = str(report.get(REPORT_COMMIT_SOURCE) or "")
    return (f"replayed on {report.get(REPORT_COMMIT) or UNKNOWN_COMMIT}:"
            f" {COMMIT_SOURCE_TEXT.get(source, COMMIT_SOURCE_TEXT[''])}")


def night_line(report: Dict[str, Any]) -> str:
    builds = report.get(REPORT_NIGHT, {}).get("builds")
    commit = report.get(REPORT_COMMIT) or UNKNOWN_COMMIT
    if not builds:
        return f"newest recording night on {commit}: none known, so no case is graded against one"
    return f"newest recording night on {commit}: builds {', '.join(builds)}"


def served_from_text(value: Any) -> str:
    if isinstance(value, dict):
        return f"ran it {value['passes']}/{value['scored']}"
    return "no run of it" if value == SERVED_FROM_NOT_RUN else UNKNOWN


def night_text(value: Any) -> str:
    if isinstance(value, dict):
        return f"as of fetch: {value['passes']}/{value['scored']}"
    if value == NIGHT_NOT_RUN:
        return "not graded on the replayed commit's night by the time the lane fetched"
    return "no night on the replayed commit"


def summary_text(replay: Dict[str, Any], report: Dict[str, Any]) -> str:
    out = [
        commit_line(report),
        night_line(report),
        f"replay lane: {replay['requests']} requests in {replay['conversations']} conversations;"
        f" served {replay['served_token_percent']}% of prompt tokens"
        f" ({replay['served_before_first_miss_token_percent']}% before each conversation's first miss,"
        f" {replay['served_via_answer_slots_token_percent']}% through answer slots)",
        f"served conversations against the recording's verdict: {report['served_conversations']}"
        f" (unknown because: {report['unknown_reasons'] or 'none'})",
    ]
    for name, case in sorted(report["cases"].items()):
        lane = case["lane"]
        served = ", ".join(f"rep {r['rep']} {r['outcome']} {r['served_token_percent']}%" for r in lane["runs"])
        served_from = "; ".join(f"{build} ({served_from_text(v)})" for build, v in sorted(case["nightly"].items()))
        out.append(f"  {name}: lane {lane['passes']}/{lane['scored']} ({served});"
                   f" newest night {night_text(case.get(REPORT_NIGHT))};"
                   f" {'answers served from ' + served_from if served_from else 'served nothing'}")
    safety = sorted((name, case["arms"]) for name, case in report["cases"].items() if "arms" in case)
    if safety:
        out.append("safety cases, live before the cache was seeded, then replayed:")
    for name, arms in safety:
        live, replayed = arms[ARM_LIVE], arms[ARM_REPLAYED]
        line = (f"  {name}: live {live['passes']}/{live['scored']}, replayed {replayed['passes']}/{replayed['scored']}"
                f" (served {replayed['served_token_percent']}%)")
        if live["served_token_percent"]:
            line += f"; the live arm was served {live['served_token_percent']}%, so it was not live"
        out.append(line)
    return "\n".join(out) + "\n"


def write_report(keep_dir: str, verdicts_dir: str, nightly_runs_path: str, out_dir: str,
                 commit: str = "", commit_source: str = "", newest_night_path: str = "") -> str:
    nightly_runs = RA.read_json(nightly_runs_path) if nightly_runs_path else None
    if not isinstance(nightly_runs, dict):
        nightly_runs = {}
    lines = analyze_replay.load(lane_segments(keep_dir))
    # Only what was asked once replay began: the live arm misses by design.
    replayed_from = read_arms(verdicts_dir).get(ARMS_REPLAYED_FROM)
    if isinstance(replayed_from, (int, float)):
        lines = [line for line in lines if line.ts >= replayed_from]
    replay = analyze_replay.summarize(lines)
    newest_builds = night_builds(newest_night_path, commit)
    report = verdicts(keep_dir, verdicts_dir, nightly_runs, newest_builds)
    report[REPORT_NIGHT] = {"builds": newest_builds or []}
    report[REPORT_COMMIT] = commit
    report[REPORT_COMMIT_SOURCE] = commit_source
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
    parser.add_argument("--newest-night", default="",
                        help="fetch_cache.py --night-out: the newest night's builds by commit")
    parser.add_argument("--out", required=True)
    parser.add_argument("--commit", default="", type=CS._shaped(CS.COMMIT_SHAPE, "commit id"),
                        help="the commit the lane replayed on")
    parser.add_argument("--commit-source", default="", choices=CS.COMMIT_SOURCES,
                        help="whether that commit is the recording night's or main")
    args = parser.parse_args(argv)
    sys.stdout.write(write_report(args.keep_dir, args.verdicts_dir, args.nightly_runs, args.out,
                                  commit=args.commit, commit_source=args.commit_source,
                                  newest_night_path=args.newest_night))
    return 0


if __name__ == "__main__":
    sys.exit(main())
