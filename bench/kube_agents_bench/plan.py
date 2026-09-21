"""``bench-run plan``: which cases can answer, and in how many repetitions.

Three repetitions of a case at 0.87 on record cannot show it improved; a case
at 0.20 shows a rise to 0.60 in ten; a case at 0.00 shows a rise to 0.30 in
five. ``plan`` computes that per selected case from its record
(:mod:`kube_agents_bench.priors`) with :func:`kube_agents_bench.stats.reps_to_detect`
and ranks the selection by it, so a developer's afternoon goes to the cases
that can show a change of the size they are after. It needs no cluster.

The same statistics drive ``--until-decided`` on ``run``: keep launching
repetitions of a case past ``--reps`` while the Fisher p-value against the
record has not crossed alpha, some completion within ``--max-reps`` still
could (:func:`kube_agents_bench.stats.can_still_decide`), and the ceiling is
not reached. The rule is sequential and the p-values are not corrected for
it, deliberately: this is a developer's stopping rule, not the gate's, and a
stricter merge argument passes a smaller ``--alpha``.
"""

from __future__ import annotations

import math
from typing import Any, Callable

from kube_agents_bench import priors as priors_mod
from kube_agents_bench import stats
from kube_agents_bench.execution import DEFAULT_UNIT_SECONDS, CaseRun, skip_reason
from kube_agents_bench.report import (
    DECISION_BETTER,
    DECISION_WORSE,
    SRC_FILE,
    SRC_MAIN,
    SRC_PRS,
    decide,
    fmt_rate,
    source_label,
)
from kube_agents_bench.selection import SelectedCase

__all__ = [
    "DEFAULT_EFFECT",
    "DEFAULT_MAX_REPS",
    "DIRECTION_DOWN",
    "DIRECTION_UP",
    "adaptive_continue",
    "plan_rows",
    "render_plan",
    "undecidable",
]

# Ceiling on repetitions under --until-decided. Past this the case is not a
# laptop experiment; the plan says so before the run starts.
DEFAULT_MAX_REPS = 12

# The effect size `plan` sizes for: a rise or drop of this much in the pass
# rate. 0.3 is the smallest change a developer changing a skill tends to be
# after; smaller effects need runs no laptop will do (`plan` shows the count).
DEFAULT_EFFECT = 0.3

# The two directions `plan` can sort by.
DIRECTION_UP = "up"
DIRECTION_DOWN = "down"

# Seconds to minutes, for the estimates the plan prints.
SECONDS_PER_MINUTE = 60.0


def _estimate_minutes(
    reps: int | None, case: SelectedCase, prior: priors_mod.Prior | None, parallel: int, overlap_reps: bool
) -> float | None:
    if reps is None:
        return None
    seconds = (prior.median_seconds if prior and prior.median_seconds else None) or DEFAULT_UNIT_SECONDS
    lanes = max(1, min(parallel, reps)) if overlap_reps and not case.exclusive else 1
    return math.ceil(reps / lanes) * seconds / SECONDS_PER_MINUTE


def plan_rows(
    cases: list[SelectedCase],
    priors: dict[str, priors_mod.Prior],
    *,
    effect: float,
    alpha: float,
    power: float,
    parallel: int,
    env: dict[str, str],
    include_infra: bool,
    overlap_reps: bool = False,
) -> list[dict[str, Any]]:
    rows = []
    for case in cases:
        prior = priors.get(case.case_id)
        up = down = None
        if prior and prior.n:
            rate = prior.rate or 0.0
            if rate < 1.0:
                up = stats.reps_to_detect(min(1.0, rate + effect), prior.passes, prior.n, alpha=alpha, power=power)
            if rate > 0.0:
                down = stats.reps_to_detect(max(0.0, rate - effect), prior.passes, prior.n, alpha=alpha, power=power)
        rows.append(
            {
                "case_id": case.case_id,
                "domain": case.spec.domain,
                "expected_fail": case.spec.expected_fail,
                "baseline_rate": prior.rate if prior else None,
                "baseline_n": prior.n if prior else 0,
                "baseline_source": prior.source if prior else None,
                "median_seconds": prior.median_seconds if prior else None,
                "reps_to_show_rise": up,
                "reps_to_show_drop": down,
                "minutes_to_show_rise": _estimate_minutes(up, case, prior, parallel, overlap_reps),
                "minutes_to_show_drop": _estimate_minutes(down, case, prior, parallel, overlap_reps),
                "skip": skip_reason(case, env, include_infra),
            }
        )
    return rows


def render_plan(rows: list[dict[str, Any]], *, direction: str, effect: float, reps: int, parallel: int) -> str:
    key = "reps_to_show_rise" if direction == DIRECTION_UP else "reps_to_show_drop"
    ordered = sorted(rows, key=lambda r: (r[key] is None, r[key] or 0, r["case_id"]))
    lines = [
        f"Plan: repetitions needed to show a {'rise' if direction == DIRECTION_UP else 'drop'} of "
        f"{effect:.2f} in pass rate against the case's record (Fisher exact, two-sided). "
        f"src: {SRC_MAIN} = the nightly's record of main; {SRC_PRS} = the pooled presubmit record of "
        f"pull-request branches, used until the nightly holds {priors_mod.MIN_NIGHTLY_RUNS} runs; "
        f"{SRC_FILE} = a summary.json. "
        f"'--' means no record; '>' means more than the search ceiling.",
        "",
        f"{'case':46} {'rate':>5} {'n':>5} {'src':>4} {'rise':>5} {'drop':>5} {'min':>6}  note",
    ]
    for r in ordered:
        rise = "--" if r["reps_to_show_rise"] is None else str(r["reps_to_show_rise"])
        drop = "--" if r["reps_to_show_drop"] is None else str(r["reps_to_show_drop"])
        if r["baseline_n"] and r["reps_to_show_rise"] is None and (r["baseline_rate"] or 0) < 1.0:
            rise = f">{stats.MAX_PLANNED_REPS}"
        if r["baseline_n"] and r["reps_to_show_drop"] is None and (r["baseline_rate"] or 0) > 0.0:
            drop = f">{stats.MAX_PLANNED_REPS}"
        minutes = r["minutes_to_show_rise" if direction == DIRECTION_UP else "minutes_to_show_drop"]
        note = r["skip"] or ("expected_fail" if r["expected_fail"] else "")
        lines.append(
            f"{r['case_id']:46} {fmt_rate(r['baseline_rate']):>5} {r['baseline_n']:>5} "
            f"{source_label(r['baseline_source']):>4} {rise:>5} {drop:>5} "
            f"{('--' if minutes is None else f'{minutes:.0f}'):>6}  {note}"
        )
    runnable = [r for r in rows if not r["skip"]]
    total = 0.0
    for r in runnable:
        seconds = r["median_seconds"] or DEFAULT_UNIT_SECONDS
        total += reps * seconds
    lines.append("")
    lines.append(
        f"{len(runnable)} of {len(rows)} selected cases can run here; at --reps {reps} "
        f"--parallel {parallel} that is roughly "
        f"{total / SECONDS_PER_MINUTE / max(1, min(parallel, max(1, len(runnable)))):.0f} min "
        f"of wall clock ({total / SECONDS_PER_MINUTE:.0f} agent-minutes; repetitions of one case run in series)."
    )
    return "\n".join(lines)


def undecidable(
    cases: list[SelectedCase], priors: dict[str, priors_mod.Prior], env: dict[str, str], include_infra: bool
) -> list[str]:
    """Cases --until-decided could never decide: no baseline to decide against."""
    return [
        c.case_id
        for c in cases
        if skip_reason(c, env, include_infra) is None and not (priors.get(c.case_id) and priors[c.case_id].n)
    ]


def adaptive_continue(
    priors: dict[str, priors_mod.Prior], *, max_reps: int, alpha: float
) -> Callable[[CaseRun, int], bool]:
    """The ``--until-decided`` predicate :func:`execution.run_cases` asks once a
    case has its fixed repetitions in: launch another while the decision is
    open, reachable within the ceiling, and the ceiling is not reached."""

    def more(run: CaseRun, planned: int) -> bool:
        if planned >= max_reps:
            return False
        prior = priors.get(run.case.case_id)
        if prior is None or prior.n == 0:
            return False
        decision = decide(run.passes, run.fails, prior, alpha)
        if decision.verdict in (DECISION_BETTER, DECISION_WORSE):
            return False
        # The budget that can still score: what is left under the ceiling,
        # counted from planned units, on top of the scored ones. Counting from
        # scored alone would credit infra/blocked units as runs still to come.
        budget = run.scored + (max_reps - planned)
        return stats.can_still_decide(run.passes, run.scored, budget, prior.passes, prior.n, alpha)

    return more
