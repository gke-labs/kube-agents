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

"""``bench-gate``: the presubmit's verdict, moved out of the shell.

Three subcommands, one per side of the loop. ``bench-gate case`` grades one
task's repetitions and writes a JSON hand-off; ``bench-gate suite`` reads those
hand-offs and decides the job's exit status; ``bench-gate record`` turns the
same hand-offs into appended lines in the baseline store. The split exists
because the shell loop already knows how to run a task and diff the results
directory — those are genuinely shell concerns — while the ladder, the collapse
rule and the aggregate are not, and were previously four inline ``python3 -c``
heredocs that no test could reach.

THE LOOP CLOSES THROUGH ``record``. Everything the gate compares against comes
from lines that a run on ``main`` appended, so without a writer the store stays
empty and the two quality rungs never arm. ``record`` is that writer, and it
refuses to run where ``PULL_NUMBER`` is set: a pull request may read the
baseline it is judged against and may never move it.

EXIT CODES. ``case`` exits 0 whenever it produced a verdict, including a
blocking one: the loop must keep going so the summary covers every task, and
the blocking flag rides in the JSON. It exits 2 when it could not grade at all
(an unreadable task file, a bad flag). ``suite`` exits 0 green, 1 red.
``record`` exits 0 unless it was asked to write somewhere it cannot — it is
bookkeeping, and bookkeeping must never be the reason a merge to main reds.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from kube_agents_bench.baselines import (
    AdmissionBar,
    BaselineRecord,
    BaselineStore,
    StoreUnreachable,
    VersionKey,
    append_record,
    load_versions,
    utc_now,
)
from kube_agents_bench.cases import CaseSpecError, load_case
from kube_agents_bench.scoring import (
    DEFAULT_AGGREGATE_MIN_SCORED,
    DEFAULT_CORRECTNESS_FLOOR,
    DEFAULT_JUDGED_MARGIN,
    DEFAULT_JUDGED_METRICS,
    MISSING,
    Rung,
    grade_case,
    grade_suite,
    load_run,
)

__all__ = ["main"]

_DEFAULT_BASELINE_DIR = "baselines"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _judged_metrics() -> tuple[str, ...]:
    """Which judged metrics rung 6 gates on, from the environment."""
    raw = os.environ.get("EVAL_JUDGED_METRICS", "")
    parts = tuple(p for p in raw.replace(",", " ").split() if p)
    return parts or DEFAULT_JUDGED_METRICS


def _store_location(args: argparse.Namespace) -> str:
    """Where the evidence lives. A directory, or ``gs://bucket/prefix``.

    ``--baseline-dir`` still holds VERSIONS.json even when evidence has moved
    to GCS, and that split is deliberate: the fleet and verifiers integers are
    hand-declared, reviewed configuration, not measured data, and configuration
    belongs where it gets reviewed.
    """
    return (
        getattr(args, "baseline_store", None)
        or os.environ.get("EVAL_BASELINE_STORE")
        or args.baseline_dir
    )


def _load_store(location: str) -> tuple[BaselineStore | None, str | None, str | None]:
    """``(store, fatal_reason, degraded_reason)`` -- exactly one of the last two.

    Three failure classes, deliberately not treated alike.

    Bytes that arrived and will not parse are FATAL. A store that will not
    parse is never read as an empty store: empty means "nothing admitted, the
    aggregate is advisory", which is a legitimate green, and a corrupt file
    reaching that state would silently disarm the gate.

    A store that cannot be REACHED degrades to advisory with a banner. The
    trade is real and worth stating: a sustained outage quietly loosens the
    gate. It is still the right way round, because a network blip redding every
    pull request is the exact failure mode that gets a gate switched off, and
    that is what this whole design exists to avoid.
    """
    try:
        return BaselineStore.load(location), None, None
    except ValueError as exc:
        return None, str(exc), None
    except StoreUnreachable as exc:
        return BaselineStore({}), None, f"{location} unreachable: {exc}"


def _bootstrap_admitted() -> frozenset[str]:
    """Cases that keep blocking through the transition, from the environment.

    Whitespace- or comma-separated, so the shell can write either.
    """
    raw = os.environ.get("BOOTSTRAP_ADMITTED", "")
    return frozenset(part for part in raw.replace(",", " ").split() if part)


def _label(case: dict[str, Any]) -> str:
    """The build-log word for a case verdict.

    Four labels, not two, because the rate rules create a state a two-label
    scheme cannot say. UNSTABLE is a case that failed repetitions -- but not
    all of them, or not with the screening evidence to red a merge. Calling
    that PASSED would report a case as passing on a run where it passed
    nothing, which is the sort of quiet lie that gets a gate switched off.

    FAILED and RESOURCE_PREPARATION_FAILED keep their historical spellings:
    people and scripts grep build logs for both.
    """
    if int(case.get("rung") or Rung.GREEN) == int(Rung.INFRA):
        return "RESOURCE_PREPARATION_FAILED"
    if case.get("blocking"):
        return "FAILED"
    scored = int(case.get("scored") or 0)
    if case.get("expected_fail"):
        # Failing is the declared intent, so neither PASSED nor UNSTABLE fits.
        return "EXPECTED_FAIL"
    if scored and int(case.get("passes") or 0) == scored:
        return "PASSED"
    return "UNSTABLE"


def _cmd_case(args: argparse.Namespace) -> int:
    try:
        spec = load_case(args.task)
    except CaseSpecError as exc:
        print(f"Task {args.task} Result: [FAILED] {exc}", file=sys.stderr)
        return 2

    # The shell knows the deployer too (it echoes it into the log). Prefer the
    # task file, which is parsed properly, and accept the flag as an override
    # for the local-run case where someone is testing a variant.
    deployer = args.deployer or spec.deployer
    if deployer != spec.deployer:
        spec = dataclasses.replace(spec, deployer=deployer)

    run_dirs: list[str | None] = [
        None if d == MISSING else d for d in (args.result or [])
    ]

    # The version key comes off the first repetition that produced a readable
    # record. All repetitions of one case run on the same software, so any of
    # them answers; taking the first readable one tolerates a lead-off infra
    # failure without losing the key.
    key: VersionKey | None = None
    admission_reason = "no readable record, so no version key"
    try:
        versions = load_versions(Path(args.baseline_dir) / "VERSIONS.json")
    except (FileNotFoundError, ValueError) as exc:
        print(f"Task {spec.case_id} Result: [FAILED] {exc}", file=sys.stderr)
        return 2

    for run_dir in run_dirs:
        record = load_run(run_dir) if run_dir else None
        if record is None or record.empty_record:
            continue
        key = VersionKey.from_run(
            setup_id=record.setup_id,
            scoring_version=record.scoring_version,
            judge_model=args.judge_model or os.environ.get("JUDGE_MODEL"),
            versions=versions,
        )
        break

    store, fatal, degraded = _load_store(_store_location(args))
    if fatal or store is None:
        print(f"Task {spec.case_id} Result: [FAILED] {fatal}", file=sys.stderr)
        return 2
    if degraded:
        print(f"WARNING: baseline store {degraded}", file=sys.stderr)
        print("WARNING: grading with no baseline; nothing can be admitted.", file=sys.stderr)

    bar = AdmissionBar.from_env()
    admitted, admission_reason = store.is_admitted(
        spec.case_id, key, bar=bar, bootstrap=_bootstrap_admitted()
    )

    # Rung 6's comparator. None whenever the store has nothing at this key --
    # including for a BOOTSTRAP_ADMITTED case, which is admitted by fiat and
    # therefore has no measured judged mean to be compared against. Admitted
    # without evidence still means the judged rung stays quiet.
    evidence = store.evidence_for(spec.case_id, key, min_runs=bar.min_runs)
    baseline_judged = evidence.judged_means if evidence else None

    verdict = grade_case(
        spec,
        list(run_dirs),
        admitted=admitted,
        correctness_floor=args.correctness_floor,
        baseline_judged=baseline_judged,
        judged_margin=args.judged_margin,
        judged_metrics=_judged_metrics(),
    )

    payload = verdict.to_dict()
    payload["admission_reason"] = admission_reason
    payload["version_key"] = key.to_dict() if key else None
    payload["baseline_judged"] = baseline_judged
    payload["baseline_runs"] = evidence.runs if evidence else 0
    payload["baseline_passes"] = evidence.passes if evidence else 0
    # The shell used to grep this out of the task file itself and echo it; it
    # is reported here instead so there is one parser, not two that can
    # disagree about which task provisions infrastructure.
    payload["deployer"] = spec.deployer
    payload["label"] = _label(payload)

    # The one-line log the presubmit has always printed, in the same shape so
    # anyone grepping build logs for "Result:" keeps finding them.
    print(f"Task {spec.case_id} Result: [{payload['label']}] {verdict.reason}")
    print(f"  deployer: {spec.deployer}")
    for rep in verdict.reps:
        judged = " ".join(f"{k}={v}" for k, v in sorted(rep.judged.items()))
        print(f"  rep {rep.index}: {rep.outcome} -- {rep.reason}" + (f" [{judged}]" if judged else ""))
    print(f"  admission: {admission_reason}")
    # stderr, not stdout: this is the one line that says the judged rung is
    # quieter than the configuration claims, and it must survive a reader who
    # only greps for "Result:".
    for note in verdict.notes:
        print(f"WARNING: {spec.case_id}: {note}", file=sys.stderr)

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    return 0


def _markdown(verdict: Any, cases: list[dict[str, Any]]) -> str:
    lines = [
        "## Evaluation verdict",
        "",
        f"**{'GREEN' if verdict.green else 'RED'}**",
        "",
    ]
    if verdict.pass_rate is not None:
        rate = f"{verdict.pass_rate:.1%}"
        if verdict.baseline_rate is not None:
            rate += f" (main: {verdict.baseline_rate:.1%}, margin {verdict.margin:.1%})"
        else:
            rate += " (no baseline at the current version key -- advisory)"
        lines += [f"Admitted-case pass rate: {rate}", ""]
    for note in getattr(verdict, "notes", None) or []:
        lines += [f"_{note}_", ""]
    if verdict.reasons:
        lines += ["### Why it is red", ""]
        lines += [f"- {r}" for r in verdict.reasons]
        lines += [""]
    lines += [
        "| Case | Domain | Verdict | Passes | Detail |",
        "| --- | --- | --- | --- | --- |",
    ]
    for case in cases:
        rung = Rung(int(case.get("rung") or Rung.GREEN))
        # `label` is written by `case`; recompute for a hand-authored file.
        mark = case.get("label") or _label(case)
        scored = case.get("scored") or 0
        # A verifier's reason can contain a pipe (a required-phrase list, a
        # kubectl selector), which would silently split the table cell.
        detail = str(case.get("reason") or "").replace("|", "\\|")
        lines.append(
            f"| `{case.get('case')}` | {case.get('domain') or '--'} | {mark} "
            f"(rung {int(rung)}) | {case.get('passes')}/{scored} | {detail} |"
        )
    return "\n".join(lines) + "\n"


def _read_case_results(paths: list[str]) -> list[dict[str, Any]] | str:
    """The per-case hand-offs, or the one-line reason they could not be read."""
    cases: list[dict[str, Any]] = []
    for path in paths or []:
        p = Path(path)
        if not p.is_file():
            # A per-case file the loop never wrote means the loop died partway.
            # Louder than a missing entry in a table: it is unaccounted work.
            return f"missing case result {p}"
        try:
            cases.append(json.loads(p.read_text(encoding="utf-8")))
        except ValueError as exc:
            return f"unreadable case result {p}: {exc}"
    return cases


def _baseline_rate(
    cases: list[dict[str, Any]], store: BaselineStore, bar: AdmissionBar
) -> float | None:
    """Main's pass rate over the same admitted cases this run graded.

    Pooled across cases rather than averaged over them, so a case with twenty
    runs of evidence weighs more than one with three -- the same weighting
    ``grade_suite`` applies to the pull request's own side of the comparison.
    Both sides therefore answer the same question, which is the only way the
    difference between them means anything.

    Only cases with evidence at their own version key contribute. A case
    admitted by ``BOOTSTRAP_ADMITTED`` has none by construction, so it counts
    toward the pull request's rate and not toward main's; that skews the
    comparison, and the honest fix is to screen the case rather than to invent
    a baseline for it.

    Returns None when no admitted case has any evidence, which makes the
    aggregate advisory and says so.
    """
    passes = runs = 0
    for case in cases:
        if not case.get("admitted"):
            continue
        raw_key = case.get("version_key")
        if not isinstance(raw_key, dict):
            continue
        evidence = store.evidence_for(
            str(case.get("case") or ""),
            VersionKey.from_dict(raw_key),
            min_runs=bar.min_runs,
        )
        if evidence is None:
            continue
        passes += evidence.passes
        runs += evidence.runs
    return (passes / runs) if runs else None


def _cmd_suite(args: argparse.Namespace) -> int:
    cases = _read_case_results(args.case_result)
    if isinstance(cases, str):
        print(f"::error::{cases}", file=sys.stderr)
        return 1

    store, fatal, degraded = _load_store(_store_location(args))
    if fatal or store is None:
        print(f"::error::{fatal}", file=sys.stderr)
        return 1

    # An explicit --baseline-rate wins, for a local run or a what-if. Otherwise
    # the number comes from the store, which is the whole point: the aggregate
    # rule was a flag nothing supplied, and so never fired.
    baseline_rate = args.baseline_rate
    if baseline_rate is None:
        baseline_rate = _baseline_rate(cases, store, AdmissionBar.from_env())

    verdict = grade_suite(
        cases,
        baseline_rate=baseline_rate,
        margin=args.margin,
        min_scored=args.min_scored,
    )

    # BOOTSTRAP_ADMITTED is hand-edited in the Prow job config, and a
    # misspelling there does not fail -- it silently un-arms the case it was
    # written to keep blocking. `crashloop-debug` for
    # `cluster-agent-crashloop-debug` reads as a working entry and gates
    # nothing. This is the same silent-disarm class the corrupt-store exit 2
    # exists to close, arriving through configuration rather than data, so it
    # is reported the same way: loudly, and in the markdown rather than only
    # in a log line nobody reads on a green run.
    #
    # A warning rather than a red. The name might belong to a case that is
    # legitimately absent from this run -- commented out of TASKS, or filtered
    # -- and redding the job for naming a case it did not run would make the
    # variable unusable for the transition it exists to cover.
    unknown = sorted(_bootstrap_admitted() - {str(c.get("case")) for c in cases})

    # Per-case notes, deduplicated. A misspelled EVAL_JUDGED_METRICS name is
    # one configuration mistake, not one per case, and repeating it fourteen
    # times in the banner is how a reader learns to skip banners.
    case_notes = sorted({n for c in cases for n in (c.get("notes") or [])})

    text = _markdown(verdict, cases)
    # The banner goes in the markdown, not only in the log. A degraded read
    # silently loosens the gate, and the one thing that must not happen is a
    # green nobody knows was measured against nothing.
    banners = []
    if unknown:
        print(
            f"WARNING: BOOTSTRAP_ADMITTED names no graded case: {unknown}",
            file=sys.stderr,
        )
        banners.append(
            "> **WARNING — `BOOTSTRAP_ADMITTED` names no graded case.** "
            f"`{'`, `'.join(unknown)}` matched nothing this run graded. If "
            "that is a typo, the case it was meant to keep blocking is not "
            "blocking."
        )
    for note in case_notes:
        print(f"WARNING: {note}", file=sys.stderr)
        banners.append(f"> **WARNING — judged rung degraded.** {note}")
    if degraded:
        banners.append(
            f"> **WARNING — baseline unavailable.** {degraded}\n>\n"
            "> Nothing could be admitted, so collapse and judged-regression were "
            "not evaluated and the aggregate below is advisory. This verdict is "
            "weaker than a normal one."
        )
    for case_id, dropped in sorted(getattr(store, "truncated", {}).items()):
        banners.append(
            f"> **NOTE — truncated read.** `{case_id}`: the {dropped} oldest "
            "record(s) were not read. Admission uses the newest evidence, so "
            "this does not change the verdict."
        )
    if banners:
        text = "\n\n".join(banners) + "\n\n" + text
    print(text)
    if args.markdown_out:
        out = Path(args.markdown_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(verdict.to_dict(), indent=2) + "\n", encoding="utf-8")

    return 0 if verdict.green else 1


def _record_for_case(
    case: dict[str, Any], *, commit: str | None, recorded_at: str
) -> tuple[str, BaselineRecord] | str:
    """One case hand-off folded into one appendable line, or why it was skipped.

    A skip returns a string. Skipping is normal and frequent -- most of what a
    run produces is not evidence about reliability -- so it is reported rather
    than raised.
    """
    case_id = str(case.get("case") or "").strip()
    if not case_id:
        return "a case result carries no case id"

    raw_key = case.get("version_key")
    if not isinstance(raw_key, dict) or not raw_key.get("setup_id"):
        # No key means no readable record in any repetition. There is nothing
        # to file this under, and filing it under a partial key would create a
        # bucket that a real run can never match.
        return f"{case_id}: no version key on this run, so nothing to file it under"

    reps = [r for r in (case.get("reps") or []) if isinstance(r, dict)]
    scored = [r for r in reps if r.get("outcome") in ("pass", "fail")]
    if not scored:
        return (
            f"{case_id}: no repetition produced a pass or a fail "
            f"({len(reps)} repetition(s) blocked or hit infrastructure)"
        )

    return case_id, BaselineRecord(
        key=VersionKey.from_dict(raw_key),
        runs=len(scored),
        passes=sum(1 for r in scored if r.get("outcome") == "pass"),
        recorded_at=recorded_at,
        commit=commit,
        judged=case.get("judged_means") or None,
        blocked=sum(1 for r in reps if r.get("outcome") == "blocked"),
        infra=sum(1 for r in reps if r.get("outcome") == "infra"),
    )


def _cmd_record(args: argparse.Namespace) -> int:
    """Append this run's evidence to the baseline store. Main only.

    Unconditional on the verdict, deliberately. A red run on main is exactly
    the evidence that de-admits a case that has stopped working, and a store
    that only ever recorded good days would drift its bar upward until nothing
    could clear it and nothing could ever fall back below it.
    """
    if os.environ.get("PULL_NUMBER") and not args.force:
        # The invariant, enforced where it cannot be edited away by one line of
        # shell: a pull request does not move the baseline it is judged against.
        print(
            "::error::refusing to record a baseline with PULL_NUMBER set "
            f"({os.environ['PULL_NUMBER']}): only runs on main append",
            file=sys.stderr,
        )
        return 2

    cases = _read_case_results(args.case_result)
    if isinstance(cases, str):
        print(f"::error::{cases}", file=sys.stderr)
        return 1

    recorded_at = args.recorded_at or utc_now()
    commit = args.commit or None
    written: list[str] = []

    for case in cases:
        outcome = _record_for_case(case, commit=commit, recorded_at=recorded_at)
        if isinstance(outcome, str):
            print(f"  skipped {outcome}")
            continue
        case_id, record = outcome
        try:
            path, line = append_record(_store_location(args), case_id, record)
        except (OSError, StoreUnreachable) as exc:
            print(f"::error::cannot append to the baseline store: {exc}", file=sys.stderr)
            return 2
        written.append(line)
        print(
            f"  recorded {case_id}: {record.passes}/{record.runs} -> {path}"
            + (f" (+{record.blocked} blocked)" if record.blocked else "")
            + (f" (+{record.infra} infra)" if record.infra else "")
        )

    if not written:
        print("No baseline lines were appended: this run produced no evidence.")

    if args.lines_out:
        # The same lines, somewhere a CI artefact collector can reach them.
        # The store lives in git and this job cannot push, so the appended file
        # dies with the workspace; the artefact is how the evidence survives
        # long enough for someone to land it. Automating that push is its own
        # change, with its own credential argument.
        out = Path(args.lines_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("".join(f"{line}\n" for line in written), encoding="utf-8")

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bench-gate",
        description="Grade devops-bench runs against the rate-based eval gate.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    case = sub.add_parser("case", help="grade one task's repetitions")
    case.add_argument("--task", required=True, help="path to bench/tasks/<id>/task.yaml")
    case.add_argument(
        "--deployer",
        default=None,
        help="override the task file's infrastructure.deployer",
    )
    case.add_argument(
        "--result",
        action="append",
        default=[],
        metavar="RUN_DIR",
        help=f"a run directory, or the literal {MISSING}; repeat once per repetition",
    )
    case.add_argument("--json-out", default=None, help="write the case hand-off here")
    case.add_argument(
        "--baseline-dir",
        default=_DEFAULT_BASELINE_DIR,
        help="directory holding VERSIONS.json, and the default evidence location "
        "(default: %(default)s)",
    )
    case.add_argument(
        "--baseline-store",
        default=None,
        help="where evidence is read/written: a directory or gs://bucket/prefix. "
        "Defaults to $EVAL_BASELINE_STORE, then --baseline-dir.",
    )
    case.add_argument(
        "--judge-model",
        default=None,
        help="judge model for the version key (default: $JUDGE_MODEL)",
    )
    case.add_argument(
        "--correctness-floor",
        type=float,
        default=_env_float(
            "DETERMINISTIC_CORRECTNESS_FLOOR", DEFAULT_CORRECTNESS_FLOOR
        ),
        help="VerificationCorrectness a repetition must meet (default: %(default)s)",
    )
    case.add_argument(
        "--judged-margin",
        type=float,
        default=_env_float("EVAL_JUDGED_MARGIN", DEFAULT_JUDGED_MARGIN),
        help="how far a judged mean may fall below main's (default: %(default)s)",
    )
    case.set_defaults(func=_cmd_case)

    suite = sub.add_parser("suite", help="combine case hand-offs into the job verdict")
    suite.add_argument(
        "--case-result",
        action="append",
        default=[],
        metavar="JSON",
        help="a file written by `bench-gate case --json-out`; repeat per case",
    )
    suite.add_argument("--markdown-out", default=None)
    suite.add_argument("--json-out", default=None)
    suite.add_argument(
        "--baseline-dir",
        default=_DEFAULT_BASELINE_DIR,
        help="directory holding VERSIONS.json, and the default evidence location "
        "(default: %(default)s)",
    )
    suite.add_argument(
        "--baseline-store",
        default=None,
        help="where evidence is read/written: a directory or gs://bucket/prefix. "
        "Defaults to $EVAL_BASELINE_STORE, then --baseline-dir.",
    )
    suite.add_argument(
        "--baseline-rate",
        type=float,
        default=None,
        help="override main's pass rate; computed from the store if omitted",
    )
    suite.add_argument(
        "--margin",
        type=float,
        default=_env_float("EVAL_AGGREGATE_MARGIN", 0.05),
        help="non-inferiority margin on the aggregate (default: %(default)s)",
    )
    suite.add_argument(
        "--min-scored",
        type=int,
        default=_env_int("EVAL_AGGREGATE_MIN_SCORED", DEFAULT_AGGREGATE_MIN_SCORED),
        help=(
            "scored repetitions the aggregate needs before it may block; "
            "below this it is reported but advisory (default: %(default)s)"
        ),
    )
    suite.set_defaults(func=_cmd_suite)

    record = sub.add_parser(
        "record",
        help="append this run's evidence to the baseline store (main runs only)",
    )
    record.add_argument(
        "--case-result",
        action="append",
        default=[],
        metavar="JSON",
        help="a file written by `bench-gate case --json-out`; repeat per case",
    )
    record.add_argument(
        "--baseline-dir",
        default=_DEFAULT_BASELINE_DIR,
        help="directory holding VERSIONS.json, and the default evidence location "
        "(default: %(default)s)",
    )
    record.add_argument(
        "--baseline-store",
        default=None,
        help="where evidence is read/written: a directory or gs://bucket/prefix. "
        "Defaults to $EVAL_BASELINE_STORE, then --baseline-dir.",
    )
    record.add_argument(
        "--commit",
        default=os.environ.get("PULL_BASE_SHA") or os.environ.get("GIT_COMMIT"),
        help="the main SHA this evidence was measured on (default: $PULL_BASE_SHA)",
    )
    record.add_argument(
        "--recorded-at",
        default=None,
        help="override the UTC stamp; for reproducible local screening runs",
    )
    record.add_argument(
        "--lines-out",
        default=None,
        help="also write the appended lines here, for collection as an artefact",
    )
    record.add_argument(
        "--force",
        action="store_true",
        help="append even with PULL_NUMBER set; for tests and local screening",
    )
    record.set_defaults(func=_cmd_record)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
