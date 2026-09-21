"""What a run set says: the verdict per case and the files that carry it.

The reporting half of ``bench-run``. A case's repetitions become ``k/n``
with a Wilson interval, a two-sided Fisher exact p-value against the case's
record (:mod:`kube_agents_bench.priors`) and a decision -- ``better``,
``worse``, ``undecided``, or ``no baseline`` -- plus a tally of which checks
failed how often. ``summary.json`` is the machine-readable form and is
itself a baseline for the next run set; ``summary.md`` is the same table
rendered, and it lists every run directory so a pull request's Live
validation section can quote them.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kube_agents_bench import priors as priors_mod
from kube_agents_bench import stats
from kube_agents_bench.execution import OUTCOME_BLOCKED, OUTCOME_INFRA, CaseRun

__all__ = [
    "DECISION_BETTER",
    "DECISION_NO_BASELINE",
    "DECISION_UNDECIDED",
    "DECISION_WORSE",
    "Decision",
    "SUMMARY_JSON",
    "SUMMARY_MD",
    "SummaryError",
    "decide",
    "load_summary",
    "render_summary",
    "summarise",
    "write_summary",
]

# Files a run set writes at its root.
SUMMARY_JSON = "summary.json"
SUMMARY_MD = "summary.md"
GIT_TIMEOUT_S = 10
# Decisions the summary prints against a baseline.
DECISION_BETTER = "better"
DECISION_WORSE = "worse"
DECISION_UNDECIDED = "undecided"
DECISION_NO_BASELINE = "no baseline"
# The `src` column: which record a baseline number is against.
SRC_MAIN = "main"
SRC_PRS = "prs"
SRC_FILE = "file"
SRC_NONE = "--"
# Below this a p-value prints as "<.001" rather than as zeros.
P_VALUE_DISPLAY_FLOOR = 0.001


@dataclass
class Decision:
    p_value: float | None
    verdict: str
    interval: stats.Interval


def decide(passes: int, fails: int, prior: priors_mod.Prior | None, alpha: float) -> Decision:
    interval = stats.wilson_interval(passes, passes + fails)
    if prior is None or prior.n == 0:
        return Decision(None, DECISION_NO_BASELINE, interval)
    if passes + fails == 0:
        return Decision(None, DECISION_UNDECIDED, interval)
    p_value = stats.fisher_exact(passes, fails, prior.passes, prior.fails)
    if p_value >= alpha:
        return Decision(p_value, DECISION_UNDECIDED, interval)
    rate = passes / (passes + fails)
    verdict = DECISION_BETTER if rate > (prior.rate or 0.0) else DECISION_WORSE
    return Decision(p_value, verdict, interval)


def _fmt_rate(value: float | None) -> str:
    return "  --" if value is None else f"{value:4.2f}"


def _fmt_p(value: float | None) -> str:
    if value is None:
        return "   --"
    return "<.001" if value < P_VALUE_DISPLAY_FLOOR else f"{value:5.3f}"


def _source_label(source: str | None) -> str:
    if not source:
        return SRC_NONE
    if source.endswith(priors_mod.TIER_NIGHTLY):
        return SRC_MAIN
    if source.endswith(priors_mod.TIER_PRESUBMIT):
        return SRC_PRS
    return SRC_FILE


def _git_head(root: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=False, timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() or None


def summarise(
    runs: list[CaseRun],
    priors: dict[str, priors_mod.Prior],
    *,
    alpha: float,
    root: Path,
    baseline: str,
    out_dir: Path,
    interrupted: bool = False,
    aborted: str | None = None,
) -> dict[str, Any]:
    cases = []
    for r in runs:
        prior = priors.get(r.case.case_id)
        decision = decide(r.passes, r.fails, prior, alpha)
        counts = r.counts
        cases.append(
            {
                "case_id": r.case.case_id,
                "domain": r.case.spec.domain,
                "expected_fail": r.case.spec.expected_fail,
                "skipped": r.skipped,
                "passes": r.passes,
                "fails": r.fails,
                "infra": counts[OUTCOME_INFRA],
                "blocked": counts[OUTCOME_BLOCKED],
                "rate": decision.interval.rate,
                "ci_low": decision.interval.low if r.scored else None,
                "ci_high": decision.interval.high if r.scored else None,
                "baseline": (
                    {"passes": prior.passes, "n": prior.n, "rate": prior.rate, "source": prior.source}
                    if prior else None
                ),
                "p_value": decision.p_value,
                "decision": decision.verdict if r.scored else None,
                "failed_checks": dict(Counter(name for u in r.units for name in u.failed_checks)),
                "reps": [
                    {
                        "rep": u.rep,
                        "outcome": u.result.outcome if u.result else None,
                        "reason": u.result.reason if u.result else None,
                        "run_dir": str(u.run_dir) if u.run_dir else None,
                        "log": str(u.log),
                        "seconds": u.seconds,
                        "returncode": u.returncode,
                    }
                    for u in r.graded
                ],
            }
        )
    return {
        "tool": "bench-run",
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "commit": _git_head(root),
        "baseline": baseline,
        "alpha": alpha,
        "out_dir": str(out_dir),
        "interrupted": interrupted,
        "aborted": aborted,
        "cases": cases,
    }


def render_summary(summary: dict[str, Any]) -> str:
    lines = [
        f"bench-run at {summary.get('commit') or '?'} -- {summary['out_dir']}"
        + ("  (INTERRUPTED: partial)" if summary.get("interrupted") else "")
        + (f"  (ABORTED: partial; {summary['aborted']})" if summary.get("aborted") else ""),
        f"baseline: {summary['baseline']}; alpha {summary['alpha']}",
        "",
        f"{'case':46} {'k/n':>5} {'rate':>5} {'95% CI':>11} {'base':>5} {'n':>5} {'src':>4} {'p':>6}  decision",
    ]
    for c in summary["cases"]:
        if c["skipped"]:
            lines.append(f"{c['case_id']:46} {'--':>5}  skipped: {c['skipped']}")
            continue
        n = c["passes"] + c["fails"]
        ci = "--" if not n else f"{c['ci_low']:.2f}-{c['ci_high']:.2f}"
        base = c["baseline"] or {}
        extras = []
        if c["infra"]:
            extras.append(f"{c['infra']} infra")
        if c["blocked"]:
            extras.append(f"{c['blocked']} blocked")
        if c["expected_fail"]:
            extras.append("expected_fail")
        decision = c["decision"] or "--"
        lines.append(
            f"{c['case_id']:46} {c['passes']:>2}/{n:<2} {_fmt_rate(c['rate']):>5} {ci:>11} "
            f"{_fmt_rate(base.get('rate')):>5} {base.get('n', 0):>5} {_source_label(base.get('source')):>4} "
            f"{_fmt_p(c['p_value']):>6}  {decision}"
            + (f" ({', '.join(extras)})" if extras else "")
        )
        for name, count in sorted(c["failed_checks"].items(), key=lambda kv: -kv[1]):
            lines.append(f"{'':46}   failed {count}x: {name}")
    lines.append("")
    lines.append("run directories (for the pull request's Live validation section):")
    for c in summary["cases"]:
        for rep in c["reps"]:
            if rep["run_dir"]:
                lines.append(f"  {c['case_id']} rep {rep['rep']}: {rep['outcome']}  {rep['run_dir']}")
    return "\n".join(lines)


class SummaryError(ValueError):
    """A run set that has no readable summary."""


def load_summary(run_set: Path) -> dict[str, Any]:
    path = run_set / SUMMARY_JSON if run_set.is_dir() else run_set
    if not path.is_file():
        raise SummaryError(f"{run_set}: no {SUMMARY_JSON} (is it a bench-run run set?)")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SummaryError(f"{path}: not JSON: {exc}") from exc


def write_summary(summary: dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / SUMMARY_JSON).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    text = render_summary(summary)
    (out_dir / SUMMARY_MD).write_text(text + "\n", encoding="utf-8")
    print()
    print(text)
