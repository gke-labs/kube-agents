"""``bench-run``: repetitions, parallelism and a verdict you can read, locally.

The presubmit runs every case three times through 2,000 lines of shell that
assume Prow. A developer with an install of their own had the stock
``devops-bench`` command, one task per invocation, and a shell loop to write.
This is that loop, in tested Python, with what the loop never had: selection
by case id, glob, roster file or domain, never everything by accident
(:mod:`kube_agents_bench.selection`); the presubmit's own invocation, ceilings
and locks (:mod:`kube_agents_bench.execution`); and a verdict per case rather
than a pile of run directories -- ``k/n`` with a Wilson interval, the Fisher
exact p-value against the case's record, and which checks failed how often
(:mod:`kube_agents_bench.report`).

What it does not do: build or deploy the agent (``scripts/dev/
dev_rebuild_agent.sh`` does), or grade differently from the gate. A green here
is three passes on your install, the evidence ``eval_driven_development.md``
asks a pull request to quote; the presubmit still runs its own.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Callable, Sequence

from kube_agents_bench import priors as priors_mod
from kube_agents_bench import stats
from kube_agents_bench.plan import (
    DEFAULT_EFFECT,
    DEFAULT_MAX_REPS,
    DIRECTION_DOWN,
    DIRECTION_UP,
    adaptive_continue,
    plan_rows,
    render_plan,
    undecidable,
)
from kube_agents_bench.execution import (
    DEFAULT_PARALLEL,
    LAUNCH_STAGGER_S,
    Aborted,
    CaseRun,
    Interrupted,
    RunOptions,
    preflight,
    run_cases,
    skip_reason,
)
from kube_agents_bench.report import (
    SummaryError,
    load_summary,
    render_compare,
    render_summary,
    summarise,
    write_summary,
)
from kube_agents_bench.selection import (
    ROSTER_ALL,
    ROSTER_FILES,
    SelectedCase,
    SelectionError,
    bench_dir,
    repo_root,
    select_cases,
)

__all__ = ["main"]

# Repetitions per case. Three is the presubmit's number.
DEFAULT_REPS = 3

# Where a run set lands, relative to bench/: one directory per invocation,
# named by timestamp, with one subdirectory per case and one per repetition
# beneath it. bench/.gitignore already ignores results/.
DEFAULT_OUT_ROOT = Path("results") / "bench-run"
RUN_SET_STAMP = "%Y%m%d-%H%M%S"

# Exit codes.
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOTHING_RAN = 3
EXIT_ABORTED = 4
EXIT_INTERRUPTED = 130


def add_selectors(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("selectors", nargs="*", help="case ids, globs on ids, or task.yaml paths")
    parser.add_argument(
        "--roster",
        choices=[*ROSTER_FILES, ROSTER_ALL],
        help="add every case of a roster file under hack/eval/ (all = presubmit + nightly)",
    )
    parser.add_argument("--domain", action="append", default=[], help="add every case claiming this domain slug")
    parser.add_argument(
        "--baseline",
        default=priors_mod.BASELINE_DASHBOARD,
        help=f"pass-rate record to compare against: {priors_mod.BASELINE_DASHBOARD} "
        f"(default; {priors_mod.DASHBOARD_DATA_URL}), {priors_mod.BASELINE_NONE}, a gs:// URL, "
        "or a bench-run summary.json",
    )
    parser.add_argument("--alpha", type=float, default=stats.DEFAULT_ALPHA, help="significance level")
    parser.add_argument("--include-infra", action="store_true", help="include cases that provision a tofu stack")
    parser.add_argument("--parallel", type=int, default=DEFAULT_PARALLEL, help="concurrent units")
    parser.add_argument(
        "--overlap-reps",
        action="store_true",
        help="let repetitions of one case overlap (the presubmit runs them in series; overlapping "
        "identical prompts on one install makes repetitions less comparable)",
    )


def add_run_arguments(run: argparse.ArgumentParser) -> None:
    add_selectors(run)
    run.add_argument("--reps", type=int, default=DEFAULT_REPS, help="repetitions per case")
    run.add_argument(
        "--until-decided",
        action="store_true",
        help="after --reps, keep running a case until it is significantly better or worse than the "
        "baseline, no completion could be, or --max-reps is reached; refuses to start without a baseline",
    )
    run.add_argument("--max-reps", type=int, default=DEFAULT_MAX_REPS, help="ceiling under --until-decided")
    run.add_argument("--out", type=Path, default=None, help=f"run-set directory (default bench/{DEFAULT_OUT_ROOT}/<stamp>)")
    run.add_argument("--stagger", type=float, default=LAUNCH_STAGGER_S, help="seconds between unit launches")
    run.add_argument("--dry-run", action="store_true", help="print what would run and exit")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bench-run", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="rank the selection by repetitions needed; no cluster")
    add_selectors(plan)
    plan.add_argument("--effect", type=float, default=DEFAULT_EFFECT, help="pass-rate change to size for")
    plan.add_argument("--power", type=float, default=stats.DEFAULT_POWER, help="power target")
    plan.add_argument(
        "--direction", choices=[DIRECTION_UP, DIRECTION_DOWN], default=DIRECTION_UP,
        help="sort by reps to show a rise or a drop",
    )
    plan.add_argument("--reps", type=int, default=DEFAULT_REPS, help="repetitions the wall-clock estimate assumes")
    plan.add_argument("--json", action="store_true", help="print the rows as JSON")

    add_run_arguments(sub.add_parser("run", help="run the selection against your install and summarise"))

    summarize = sub.add_parser("summarize", help="re-print a run set's summary")
    summarize.add_argument("run_set", type=Path)

    compare = sub.add_parser("compare", help="one run set against another (candidate first)")
    compare.add_argument("candidate", type=Path)
    compare.add_argument("baseline_run_set", type=Path)
    compare.add_argument("--alpha", type=float, default=stats.DEFAULT_ALPHA)
    return parser


def load_priors(args: argparse.Namespace) -> tuple[dict[str, priors_mod.Prior], str]:
    """The priors and the baseline actually in effect -- `none` when the
    requested one could not be read, so the summary says what the run was
    compared against rather than what was asked for."""
    try:
        return priors_mod.load_priors(args.baseline), args.baseline
    except priors_mod.PriorsError as exc:
        print(f"WARNING: no baseline: {exc}; continuing with {priors_mod.BASELINE_NONE}", file=sys.stderr)
        return {}, f"{priors_mod.BASELINE_NONE} ({args.baseline} unreadable: {exc})"


def run_command(
    args: argparse.Namespace,
    *,
    env: dict[str, str],
    continue_case: Callable[[dict[str, priors_mod.Prior]], Callable[[CaseRun, int], bool]] | None = None,
    extra_problems: Sequence[Callable[[list[SelectedCase], dict[str, priors_mod.Prior]], str | None]] = (),
) -> int:
    """The ``run`` subcommand.

    ``continue_case`` and ``extra_problems`` are the hooks an adaptive mode
    plugs in: the first builds, from the priors, the predicate
    :func:`run_cases` asks before launching a repetition past ``--reps``; each
    of the second is asked for a preflight problem given the selection.
    """
    root = repo_root()
    bench = bench_dir(root)
    try:
        cases = select_cases(args.selectors, roster=args.roster, domains=args.domain, root=root)
    except SelectionError as exc:
        print(f"bench-run: {exc}", file=sys.stderr)
        return EXIT_USAGE
    priors, baseline = load_priors(args)
    if args.reps < 1 or args.parallel < 1:
        print("bench-run: --reps and --parallel must be at least 1", file=sys.stderr)
        return EXIT_USAGE
    # Absolute, because every unit runs with cwd=bench/ and is handed this
    # path as its --results-root; a relative --out from another directory
    # would send the run directories somewhere the grader does not look.
    out_dir = (args.out or (bench / DEFAULT_OUT_ROOT / dt.datetime.now(dt.UTC).strftime(RUN_SET_STAMP))).resolve()
    problems = preflight(env, bench)
    for extra in extra_problems:
        problem = extra(cases, priors)
        if problem:
            problems.append(problem)
    options = RunOptions(
        reps=args.reps, parallel=args.parallel, stagger_s=args.stagger,
        out_dir=out_dir, include_infra=args.include_infra, overlap_reps=args.overlap_reps,
    )
    if args.dry_run:
        for case in cases:
            reason = skip_reason(case, env, args.include_infra)
            print(f"{case.case_id}: {'skip: ' + reason if reason else f'{args.reps} reps' + (' (exclusive)' if case.exclusive else '')}")
        for problem in problems:
            print(f"preflight: {problem}")
        print(f"out: {out_dir}")
        return EXIT_OK
    if problems:
        for problem in problems:
            print(f"bench-run: {problem}", file=sys.stderr)
        return EXIT_USAGE

    out_dir.mkdir(parents=True, exist_ok=True)
    hook = continue_case(priors) if continue_case else None
    try:
        runs = run_cases(cases, priors, options, env=env, bench=bench, continue_case=hook)
    except Interrupted as exc:
        summary = summarise(
            exc.runs, priors, alpha=args.alpha, root=root, baseline=baseline, out_dir=out_dir, interrupted=True
        )
        write_summary(summary, out_dir)
        return EXIT_INTERRUPTED
    except Aborted as exc:
        print(f"bench-run: a unit raised: {exc.cause!r}", file=sys.stderr)
        summary = summarise(
            exc.runs, priors, alpha=args.alpha, root=root, baseline=baseline, out_dir=out_dir,
            aborted=f"{type(exc.cause).__name__}: {exc.cause}",
        )
        write_summary(summary, out_dir)
        return EXIT_ABORTED
    summary = summarise(runs, priors, alpha=args.alpha, root=root, baseline=baseline, out_dir=out_dir)
    write_summary(summary, out_dir)
    if not any(r.units for r in runs):
        return EXIT_NOTHING_RAN
    return EXIT_OK


def summarize_command(args: argparse.Namespace) -> int:
    try:
        print(render_summary(load_summary(args.run_set)))
    except SummaryError as exc:
        print(f"bench-run: {exc}", file=sys.stderr)
        return EXIT_USAGE
    return EXIT_OK


def compare_command(args: argparse.Namespace) -> int:
    try:
        print(render_compare(load_summary(args.candidate), load_summary(args.baseline_run_set), args.alpha))
    except SummaryError as exc:
        print(f"bench-run: {exc}", file=sys.stderr)
        return EXIT_USAGE
    return EXIT_OK


def plan_command(args: argparse.Namespace, *, env: dict[str, str]) -> int:
    root = repo_root()
    try:
        cases = select_cases(args.selectors, roster=args.roster, domains=args.domain, root=root)
    except SelectionError as exc:
        print(f"bench-run: {exc}", file=sys.stderr)
        return EXIT_USAGE
    priors, _baseline = load_priors(args)
    rows = plan_rows(
        cases, priors, effect=args.effect, alpha=args.alpha, power=args.power,
        parallel=args.parallel, env=env, include_infra=args.include_infra, overlap_reps=args.overlap_reps,
    )
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(render_plan(rows, direction=args.direction, effect=args.effect, reps=args.reps, parallel=args.parallel))
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env = dict(os.environ)
    if args.command == "summarize":
        return summarize_command(args)
    if args.command == "compare":
        return compare_command(args)
    if args.command == "plan":
        return plan_command(args, env=env)
    if not args.until_decided:
        return run_command(args, env=env)

    def needs_baseline(cases: list[SelectedCase], priors: dict[str, priors_mod.Prior]) -> str | None:
        missing = undecidable(cases, priors, env, args.include_infra)
        if not missing:
            return None
        return (
            "--until-decided needs a baseline to decide against, and none covers: "
            + ", ".join(missing)
            + " (pass --baseline, or drop the flag for a fixed --reps run)"
        )

    max_reps = max(args.max_reps, args.reps)
    return run_command(
        args, env=env,
        continue_case=lambda priors: adaptive_continue(priors, max_reps=max_reps, alpha=args.alpha),
        extra_problems=[needs_baseline],
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
