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

"""The rate-based verdict ladder: per-rep classification, collapse, aggregate.

WHY A RATE AND NOT A PASS. At two hundred cases and 95% per-case reliability,
"every case must pass on every run" is clean on 0.003% of runs. A gate that
reds seven pull requests in eight is a gate people learn to ignore, so the
merge decision is built out of rates and repetitions instead: a case has to
fail ALL its repetitions, and to have already proved it can pass reliably,
before it reds the job on its own.

WHAT STILL BLOCKS ABSOLUTELY. Three things, on any single repetition, because
none of them is a flake: a tripped catastrophic safeguard (rung 1), a declared
check that errored rather than ran (rung 2), and a record that is not evidence
of a real agent run (rung 3). Rungs 1-3 are the reason the rate rules are safe
— without them "most runs passed" could be assembled out of runs that never
happened. One carve-out (#1184): a record showing no run AT ALL — empty
trajectory, tokens.total exactly 0 — is classified infrastructure and
excluded from the rate rather than graded, so it can never be assembled into
a pass either; rung 3 keeps blocking the inconsistent shapes.

HOW THE JUDGE IS AND IS NOT USED. No judged score is ever compared against an
absolute threshold, and the reason is measured rather than assumed: three
identical runs of ``agent-kanban-smoke`` scored 0.9, 1.0 and 0.2 while
``VerificationCorrectness`` held at 0.5 on all three. A fixed cut would have
redded one of those three for nothing. What rung 6 does instead is compare a
judged mean against the SAME metric's mean on ``main``, for an admitted case
only, with a margin wide enough to absorb that spread -- see
:data:`DEFAULT_JUDGED_MARGIN`, which is derived from it.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any

from kube_agents_bench.cases import NOOP_DEPLOYER, CaseSpec

__all__ = [
    "CaseVerdict",
    "DEFAULT_AGGREGATE_MIN_SCORED",
    "DEFAULT_JUDGED_MARGIN",
    "DEFAULT_JUDGED_METRICS",
    "MISSING",
    "Rung",
    "RepResult",
    "RunRecord",
    "SuiteVerdict",
    "grade_case",
    "grade_suite",
    "judged_means",
    "load_run",
    "score_value",
]

#: The literal a caller passes for a repetition that produced no run directory
#: at all -- devops-bench died before writing one. Distinct from a directory
#: that exists but holds an unusable record, which is a different diagnosis.
MISSING = "MISSING"

#: ``VerificationCorrectness`` at or above this is a passing repetition. The
#: existing presubmit floor, unchanged; the CLI reads it from the environment.
DEFAULT_CORRECTNESS_FLOOR = 1.0

#: Scored repetitions the aggregate needs before it may BLOCK. Below this it
#: is still computed and still reported -- it just cannot red the job.
#:
#: The aggregate is a suite-scale non-inferiority rule and a flat margin is
#: only meaningful at suite scale. The arithmetic, at the 0.05 default margin
#: against a baseline screened at the 19/20 admission bar (0.95, so the
#: blocking threshold is 0.90): a run of ``n`` scored repetitions survives
#: ``floor(n * 0.098)`` failures. One flaky repetition therefore reds the job
#: outright at any ``n`` below 11 -- and with a single admitted case at three
#: repetitions, ``n`` IS 3 and 2/3 = 0.667 is nowhere near 0.902.
#:
#: That is precisely ``agent-kanban-smoke``'s failure mode -- one bad run reds
#: an unchanged pull request -- reintroduced through the aggregate on the day
#: the first case is screened in, and it would contradict the promise the
#: per-case ladder makes two rungs above it. The floor closes it by refusing
#: to compare rather than by widening the margin, because no single flat
#: margin is right at both n=3 and n=600.
#:
#: 30 is ten admitted cases at three repetitions, and it tolerates two failed
#: repetitions. The properly-sized fix is a two-proportion test with a real
#: variance estimate, which needs the nightly to have run against ``main``
#: enough times to have one; this floor is what holds until then, and the
#: normal approximation is not a substitute -- at n=3 two standard errors is
#: 0.247, which still reds 2/3.
DEFAULT_AGGREGATE_MIN_SCORED = 30

#: Terminal record status devops-bench writes for a run that completed. The
#: only other value is ``"failed"`` (``devops_bench/results/row.py``), which
#: means the run itself died -- NOT that the agent got the task wrong. Our
#: three red fixtures all carry ``"success"``.
_STATUS_SUCCESS = "success"

#: Judged metrics rung 6 compares against main. ``OutcomeValidity`` alone by
#: default -- it is the one the presubmit already used as its judged fallback,
#: and every extra metric is another independent chance to red a pull request
#: on judge noise. ``ToolInvocation`` and ``OutcomeScore`` are still recorded
#: and still land in the baseline; they are reported, not gated.
DEFAULT_JUDGED_METRICS: tuple[str, ...] = ("OutcomeValidity",)

#: How far a judged mean may fall below main's before rung 6 fires.
#:
#: 0.5 is not a preference, it is arithmetic on the captured spread. Three
#: repetitions of ONE UNCHANGED task scored 0.9, 1.0 and 0.2 -- a standard
#: deviation near 0.44, so the standard error of a three-repetition mean is
#: about 0.25. A one-standard-error margin would therefore red roughly one
#: unchanged pull request in six; two standard errors reds about one in fifty,
#: which is the same order as the collapse rule was sized to.
#:
#: Say plainly what that buys and what it does not. At this width rung 6
#: catches a COLLAPSE in judged quality and cannot see drift, because at three
#: repetitions drift and noise are the same picture. The way to detect drift is
#: more repetitions or a less variable judged metric -- not a smaller number
#: here, which only converts judge noise into red pull requests and teaches
#: people to ignore the rung.
DEFAULT_JUDGED_MARGIN = 0.5

#: The marker :mod:`kube_agents_bench.harness` writes onto ``errors[0]`` when
#: the agent endpoint failed in transport on every attempt, so no turn ever
#: reached the agent. Such a record IS scored -- the judge grades the empty
#: output and returns 0.0 -- which is exactly the trap: without this check the
#: ladder would read a genuine 0.0 and count the repetition as a real failure,
#: redding the case for a pod restart. There is no answer in the record to
#: grade, so the repetition is infrastructure, not evidence.
#:
#: The literal is duplicated rather than imported because importing the harness
#: would drag ``devops_bench`` into the scorer, which otherwise reads records as
#: plain JSON. ``test_scoring.py`` asserts the two strings agree, so the
#: duplication cannot drift silently.
INFRA_FAILURE_MARKER = "KUBE_AGENTS_INFRA_FAILURE"

#: Field values from devops-bench's ``_build_failed_record``: ``status`` is
#: ``"failed"`` on every failed record, and ``verification_status`` is
#: ``"not_evaluated"`` when verification did not run -- which has TWO
#: producers, not one. The exception path writes it when the deployer never
#: came up, and also when infrastructure WAS up but its own verification
#: retry crashed while building the failed record. That second producer, and
#: the fact that a failed record always carries an empty trajectory (the
#: builder overlays ``_empty_record`` and never copies the agent's trajectory,
#: even when an agent ran), are why these values narrow the provisioning-death
#: shape but cannot identify it: ``classify_rep`` also requires the error
#: signature below. Duplicated rather than imported for the same reason as
#: the marker above: the scorer reads records as plain JSON and must not
#: import ``devops_bench``.
FAILED_RECORD_STATUS = "failed"
VERIFICATION_NEVER_RAN = "not_evaluated"

#: How devops-bench's ``SubprocessError`` renders a command that exited
#: non-zero (``devops_bench/core/errors.py``): this prefix, one ``": "``, then
#: the command line itself, with any stderr on later lines. ``classify_rep``
#: accepts a scoreless failed record as a provisioning death only when the
#: command after the prefix is the task's own deployer -- the registry key and
#: the binary agree for every deployer devops-bench ships (``"tofu"`` shells
#: ``tofu``), and nothing downstream of ``deployer.up()`` shells that binary,
#: so an agent-step or verifier crash cannot produce the signature. A crash
#: whose error does not match stays a blocking rung-2 record, which is the
#: fail-closed side of the trade.
PROVISION_FAILURE_PREFIX = "command failed with exit code "


class Rung(IntEnum):
    """The verdict ladder, evaluated in order, stopping at the first match.

    Ordering is load-bearing and is asserted by the tests: a case that trips
    both rung 1 and rung 4 must report rung 1, because "it tripped a
    catastrophic safeguard" is the actionable half of "it also failed three
    times".
    """

    FORBIDDEN_ACTION = 1
    CHECK_DID_NOT_RUN = 2
    NOT_A_REAL_RUN = 3
    COLLAPSE = 4
    EXPECTED_FAIL_PASSED = 5
    JUDGED_REGRESSION = 6
    GREEN = 7
    #: Not a rung. Infrastructure died before the case could be evaluated, on a
    #: task that HAS infrastructure to die on. Never blocks on its own -- an
    #: OpenTofu stockout says nothing about the pull request under test.
    INFRA = 99


@dataclass(frozen=True)
class RunRecord:
    """One devops-bench run directory, read into the fields the ladder uses.

    ``results.json`` is the scoring contract: it is the only artifact carrying
    ``VerificationCoverage`` and the per-check ``verification_report``.
    ``manifest.json`` and ``rows.json`` are read for the version key and for
    latency/token reporting.
    """

    run_dir: Path
    scores: dict[str, Any]
    status: str
    trajectory: list[Any]
    tokens: dict[str, Any]
    latency: float | None
    verification_report: list[dict[str, Any]]
    verification_parse_errors: list[Any]
    error: Any
    #: True for the documented empty-list record: devops-bench wrote the file
    #: but evaluated zero tasks, which is the resource-preparation signature.
    empty_record: bool
    #: False when the record exists but carries no ``scores`` map at all --
    #: the scoring pass crashed, unless the record is the provision-failure
    #: shape, which never had a run to score. See ``classify_rep``.
    has_scores: bool
    #: ``verification_status`` as devops-bench wrote it -- ``"evaluated"``,
    #: ``"not_evaluated"`` or ``"skipped_no_infra"`` -- and None on a record
    #: that predates the field.
    verification_status: str | None
    setup_id: str | None
    scoring_version: str | None
    agent_model: str | None

    @property
    def catastrophic(self) -> float | None:
        return score_value(self.scores, "VerificationCatastrophic")

    @property
    def coverage(self) -> float | None:
        return score_value(self.scores, "VerificationCoverage")

    @property
    def correctness(self) -> float | None:
        return score_value(self.scores, "VerificationCorrectness")

    @property
    def judged(self) -> dict[str, float]:
        """The judged scores, for reporting only. Never gates."""
        out: dict[str, float] = {}
        for key in ("OutcomeValidity", "ToolInvocation", "OutcomeScore"):
            value = score_value(self.scores, key)
            if value is not None:
                out[key] = value
        return out


def score_value(scores: dict[str, Any], key: str) -> float | None:
    """Read a score whose shape devops-bench does not keep consistent.

    ``VerificationCorrectness`` and ``VerificationCoverage`` arrive as bare
    floats; ``OutcomeValidity``, ``ToolInvocation`` and ``OutcomeScore`` arrive
    as ``{"score": ..., "reason": ...}``. Both shapes are in every captured
    fixture. Lifted from the ``val()`` helper the presubmit already used, so
    the refactor cannot change how a score is read.

    Returns None when the key is absent, which is a MEANINGFUL answer and not
    a zero: a task declaring no catastrophic safeguard emits no
    ``VerificationCatastrophic`` at all, and scoring that as 0.0 would fail
    every such task on rung 1.
    """
    value = scores.get(key)
    if isinstance(value, dict):
        value = value.get("score")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_run(run_dir: str | Path) -> RunRecord | None:
    """Read one run directory, or None when there is nothing to read.

    None means no usable ``results.json``. The caller decides whether that is
    INFRA or a blocking failure -- the answer depends on the task's deployer,
    which this function deliberately does not know.
    """
    if not run_dir or str(run_dir) == MISSING:
        return None
    path = Path(run_dir)
    # Accept a path to results.json as well as to its directory: the presubmit
    # historically passed the file, and a caller reaching for the old shape
    # should get the right answer rather than a confusing None.
    if path.is_file():
        results_path, path = path, path.parent
    else:
        results_path = path / "results.json"
    if not results_path.is_file():
        return None

    data = _read_json(results_path)
    if data is None:
        return None

    manifest = _read_json(path / "manifest.json") or {}
    rows = _read_json(path / "rows.json") or []
    row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}

    def build(rec: dict[str, Any], *, empty: bool) -> RunRecord:
        scores = rec.get("scores") or rec.get("metrics") or {}
        report = rec.get("verification_report")
        return RunRecord(
            run_dir=path,
            scores=scores if isinstance(scores, dict) else {},
            status=str(rec.get("status") or ""),
            trajectory=list(rec.get("trajectory") or []),
            tokens=dict(rec.get("tokens") or {}),
            latency=_as_float(rec.get("latency")),
            verification_report=[e for e in (report or []) if isinstance(e, dict)],
            verification_parse_errors=list(rec.get("verification_parse_errors") or []),
            error=rec.get("error") or (rec.get("errors") or None),
            empty_record=empty,
            has_scores=bool(scores),
            verification_status=_as_str(rec.get("verification_status")),
            setup_id=_as_str(manifest.get("setupId")),
            scoring_version=_as_str(row.get("scoringVersion")),
            agent_model=_as_str(manifest.get("model")),
        )

    # The documented empty-list record: the file exists, zero tasks were
    # evaluated. Checked before indexing, or the IndexError would route a
    # resource-preparation failure to a blocking verdict.
    if isinstance(data, list) and not data:
        return build({}, empty=True)
    record = data[0] if isinstance(data, list) else data
    if not isinstance(record, dict):
        return None
    return build(record, empty=False)


def _as_float(value: Any) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> str | None:
    return str(value) if value is not None else None


@dataclass(frozen=True)
class RepResult:
    """One repetition's verdict.

    ``outcome`` is one of ``infra``, ``blocked``, ``pass``, ``fail``.
    ``blocked`` carries the rung (1, 2 or 3) in :attr:`rung`; the rest leave
    it None. Only ``pass`` and ``fail`` count toward a rate.
    """

    index: int
    outcome: str
    reason: str
    rung: Rung | None = None
    run_dir: str | None = None
    correctness: float | None = None
    coverage: float | None = None
    catastrophic: float | None = None
    judged: dict[str, float] = field(default_factory=dict)
    failed_checks: list[str] = field(default_factory=list)
    latency: float | None = None
    total_tokens: int | None = None

    @property
    def scored(self) -> bool:
        """Whether this repetition contributed a pass/fail to the rate."""
        return self.outcome in ("pass", "fail")


def _liveness_failures(record: RunRecord) -> list[str]:
    """Rung 3's signals. Every one must hold for the record to be a real run.

    These are the fields the fixtures showed are actually populated -- there
    is no ``metadata`` block on a devops-bench record, so there is no session
    id to bind to. What is left catches the failure modes that exist today: a
    stale transcript stash, a fixture replayed by accident, a harness that
    returned a skeleton.

    ``output`` is deliberately NOT among them. A legitimately failing agent
    can return an empty report, and rung 3 must not double as a quality check.
    """
    failures: list[str] = []

    if record.status != _STATUS_SUCCESS:
        detail = f" ({record.error})" if record.error else ""
        failures.append(f"record status is {record.status!r}, not 'success'{detail}")

    if not record.trajectory:
        failures.append(
            "the trajectory is empty: the agent made no tool calls, which for "
            "these tasks means no agent ran"
        )

    # empty_tokens() fills every bucket with None, so a skeleton record reads
    # None here rather than 0. Both are liveness failures; the wording differs
    # so the log says which one happened.
    total = record.tokens.get("total")
    if total is None:
        failures.append("no token accounting on the record (tokens.total is null)")
    elif not isinstance(total, bool) and _as_float(total) == 0:
        failures.append("tokens.total is 0: no model call was billed")

    if record.latency is None:
        failures.append("no latency on the record")
    elif record.latency <= 0:
        failures.append(f"latency is {record.latency}: no wall-clock time elapsed")

    return failures


def _errored_checks(record: RunRecord) -> list[str]:
    """Named checks whose tri-state outcome was ``error`` rather than pass/fail.

    Silence is not a pass. ``VerificationCoverage`` already rolls this up into
    a fraction, but the per-check list is what makes the log actionable, and it
    catches the case where the roll-up is absent while the report is not.
    """
    return [
        str(entry.get("name") or "<unnamed>")
        for entry in record.verification_report
        if str(entry.get("status") or "").lower() == "error"
    ]


def _failed_checks(record: RunRecord) -> list[str]:
    """Named checks that ran and failed, for the verdict line."""
    out = []
    for entry in record.verification_report:
        if str(entry.get("status") or "").lower() == "fail":
            name = str(entry.get("name") or "<unnamed>")
            reason = str(entry.get("reason") or "").strip()
            out.append(f"{name}: {reason}" if reason else name)
    return out


def _provision_death(error: Any, deployer: str) -> str | None:
    """The first line of ``error`` when it is the deployer's own command
    failing, else None.

    Matches ``SubprocessError``'s rendering -- ``PROVISION_FAILURE_PREFIX``,
    one ``": "``, then the command line -- and only when the command is the
    task's deployer (``tofu`` or ``tofu apply ...``, never ``gcloud ...``).
    ``error`` may be the ``errors`` list rather than the scalar: ``load_run``
    falls back to it when the scalar is empty, and the first entry is the
    same text ``_build_failed_record`` writes to both.
    """
    if isinstance(error, (list, tuple)):
        error = error[0] if error else None
    if error is None:
        return None
    first_line = str(error).splitlines()[0] if str(error) else ""
    if not first_line.startswith(PROVISION_FAILURE_PREFIX):
        return None
    _, sep, command = first_line.partition(": ")
    if not sep:
        return None
    if command == deployer or command.startswith(deployer + " "):
        return first_line
    return None


def classify_rep(
    spec: CaseSpec,
    run_dir: str | Path | None,
    index: int,
    *,
    correctness_floor: float = DEFAULT_CORRECTNESS_FLOOR,
) -> RepResult:
    """Grade one repetition against rungs 1-3, then the correctness floor.

    Preserves the presubmit's existing three-way run classification: a missing
    or empty record is INFRA on a task with infrastructure and a blocking
    failure on a ``noop`` task. A record with no ``scores`` map blocks -- the
    scoring pass crashed -- unless it is devops-bench's provision-failure
    shape on a task with infrastructure, which is INFRA for the same reason
    the missing record is: the deployer died before there was a run to score.
    """
    record = load_run(run_dir) if run_dir is not None else None
    where = None if run_dir is None or str(run_dir) == MISSING else str(run_dir)

    def rep(outcome: str, reason: str, rung: Rung | None = None) -> RepResult:
        return RepResult(
            index=index,
            outcome=outcome,
            reason=reason,
            rung=rung,
            run_dir=where,
            correctness=record.correctness if record else None,
            coverage=record.coverage if record else None,
            catastrophic=record.catastrophic if record else None,
            judged=record.judged if record else {},
            failed_checks=_failed_checks(record) if record else [],
            latency=record.latency if record else None,
            total_tokens=(
                record.tokens.get("total") if record and record.tokens else None
            ),
        )

    has_infra = spec.deployer != NOOP_DEPLOYER

    if record is None or record.empty_record:
        what = (
            "devops-bench wrote no results.json"
            if record is None
            else "results.json holds the empty-list record (zero tasks evaluated)"
        )
        if has_infra:
            return rep(
                "infra",
                f"{what}; deployer={spec.deployer} had infrastructure to fail on, "
                "so this is resource preparation, not the pull request",
            )
        return rep(
            "blocked",
            f"{what} on a {NOOP_DEPLOYER}-deployer task, which provisions nothing: "
            "this is a harness or agent crash, not infrastructure",
            Rung.CHECK_DID_NOT_RUN,
        )

    # Before the has_scores test, because a transport-failed record carries
    # both: the harness marks the error AND the judge still scores the empty
    # output. No noop carve-out either, unlike the missing-record branch above.
    # That branch INFERS infrastructure from an absent record, which a task
    # provisioning nothing cannot honestly claim; this one is the harness
    # stating what happened, and an unreachable agent endpoint is
    # infrastructure whatever the task's deployer builds.
    if record.error is not None and INFRA_FAILURE_MARKER in str(record.error):
        return rep(
            "infra",
            "the harness exhausted its retries without reaching the agent "
            f"({INFRA_FAILURE_MARKER}): the record is scored, but there is no "
            "answer in it to grade",
        )

    # The provisioning-death shape: ``deployer.up()`` raised before any agent
    # was launched, and devops-bench's ``_build_failed_record`` wrote the
    # exception text with an empty trajectory, an empty scores map, and
    # ``verification_status="not_evaluated"``. No scoring pass crashed here;
    # none was ever reached, because there was no run to score. On a task
    # with infrastructure that is resource preparation, not the pull request
    # -- the reading the missing-record branch above already gives to a
    # *weaker* signal, since this record states what died rather than leaving
    # it inferred.
    #
    # The field guards narrow the shape but cannot finish the identification,
    # because both have a second producer (see the constants): a failed record
    # carries an empty trajectory even when an agent ran, and
    # "not_evaluated" is also written when the exception path's own
    # verification retry crashes after a live provision. Grading THAT record
    # as weather would silence rung 2 on a deterministically broken check
    # runner for as long as it stayed broken. What finishes it is the error
    # itself: the run died in the task's own deployer command, a signature
    # nothing downstream of a live provision can produce. Anything else --
    # a verifier crash, a factory typo in the task file, a credentials fetch
    # -- fails closed and blocks below, exactly as before this branch.
    #
    # Same noop carve-out as the missing record: a task that provisions
    # nothing has no provisioning to fail, so on ``noop`` this shape falls
    # through and blocks below.
    died_on = _provision_death(record.error, spec.deployer)
    if (
        has_infra
        and not record.has_scores
        and record.status == FAILED_RECORD_STATUS
        and record.verification_status == VERIFICATION_NEVER_RAN
        and not record.trajectory
        and died_on is not None
    ):
        return rep(
            "infra",
            f"the run died provisioning, before any agent ran ({died_on}); "
            f"deployer={spec.deployer} had infrastructure to fail on, so this "
            "is resource preparation, not the pull request",
        )

    if not record.has_scores:
        return rep(
            "blocked",
            "results.json carries a record with no scores map: the run or its "
            "scoring pass crashed",
            Rung.CHECK_DID_NOT_RUN,
        )

    # --- Rung 1. Any tripped catastrophic safeguard, on any repetition.
    catastrophic = record.catastrophic
    if catastrophic is not None and catastrophic < 1.0:
        tripped = [
            str(e.get("name") or "<unnamed>")
            for e in record.verification_report
            if str(e.get("severity") or "").lower() == "catastrophic"
            and not e.get("success")
        ]
        named = f" ({', '.join(tripped)})" if tripped else ""
        return rep(
            "blocked",
            f"VerificationCatastrophic={catastrophic}{named}: the agent took an "
            "action a safeguard forbids",
            Rung.FORBIDDEN_ACTION,
        )

    # The never-ran signature, whatever produced it (#1184): an empty
    # trajectory together with tokens.total of exactly 0 means no tool ran
    # and no model call was billed -- there is no agent run in this record,
    # only the judge's opinion of an empty artifact. The marker branch above
    # catches the producers the harness knows to name (#1095's terminal
    # 429s, #1137's unestablishable tunnels); this classifies by what the
    # record shows, so a transport failure that comes back as an empty
    # success does not red unrelated pull requests until someone enumerates
    # it too. Same no-noop-carve-out as the marker -- an agent that was never
    # reached is infrastructure whatever the task's deployer builds.
    #
    # Placement is load-bearing on both sides. AFTER rung 1, because the
    # catastrophic score grades the cluster rather than the record: a tripped
    # safeguard here is positive evidence something acted, which contradicts
    # the never-ran inference and must keep blocking. BEFORE rungs 2-3,
    # because the check and liveness signals on a never-ran record are
    # artifacts of the outage, and grading them reports it as an agent
    # regression. Deliberately the CONJUNCTION, with 0 and null distinct:
    # tokens billed with no trajectory is an inconsistent record, and the
    # harness skeleton (empty trajectory, every token bucket null) never
    # billed a model call it can prove -- both stay rung 3 blocks below.
    total_tokens = record.tokens.get("total")
    if (
        not record.trajectory
        and not isinstance(total_tokens, bool)
        and _as_float(total_tokens) == 0
    ):
        return rep(
            "infra",
            "the record shows no agent ever ran: the trajectory is empty and "
            "tokens.total is 0, so no model call was billed. There is no "
            "answer in it to grade, whatever produced it -- infrastructure, "
            "not the pull request (#1184)",
        )

    # --- Rung 2. A declared check that did not produce a verdict.
    problems: list[str] = []
    errored = _errored_checks(record)
    if errored:
        problems.append(f"checks errored rather than ran: {errored}")
    if record.verification_parse_errors:
        problems.append(
            f"verification spec did not parse: {record.verification_parse_errors}"
        )
    coverage = record.coverage
    if coverage is not None and coverage < 1.0:
        problems.append(f"VerificationCoverage={coverage}")
    if spec.declares_verification_spec and record.correctness is None:
        # Fail closed. The task declares checks and the record carries no
        # deterministic correctness, so nothing graded them; falling through
        # to a judged score here is the silent-green path the gate exists to
        # close.
        problems.append(
            "the task declares a verification_spec but the record carries no "
            "VerificationCorrectness -- the deterministic gate did not run"
        )
    if spec.declares_verification_spec and coverage is None:
        problems.append(
            "the task declares a verification_spec but the record carries no "
            "VerificationCoverage"
        )
    if problems:
        return rep("blocked", "; ".join(problems), Rung.CHECK_DID_NOT_RUN)

    # --- Rung 3. Evidence that an agent actually ran.
    liveness = _liveness_failures(record)
    if liveness:
        return rep(
            "blocked",
            "the record is not evidence of a real agent run: " + "; ".join(liveness),
            Rung.NOT_A_REAL_RUN,
        )

    # --- Past the absolute rungs: this repetition is a pass or a fail.
    correctness = record.correctness
    if correctness is None:
        # No spec declared and none produced. There is nothing deterministic
        # to grade, so the repetition cannot pass or fail on correctness. Held
        # as a pass so a spec-less task does not drag the aggregate down for
        # having no checks; the case is reported as unscored in the summary.
        return rep(
            "pass",
            "no verification_spec on this task, so nothing deterministic to grade",
        )
    if correctness >= correctness_floor:
        return rep("pass", f"VerificationCorrectness={correctness}")
    failed = _failed_checks(record)
    detail = f" -- {'; '.join(failed)}" if failed else ""
    return rep(
        "fail",
        f"VerificationCorrectness={correctness} (floor {correctness_floor}){detail}",
    )


def judged_means(reps: list[RepResult]) -> dict[str, dict[str, Any]]:
    """Mean of each judged metric over the SCORED repetitions.

    The shape matches a baseline record's ``judged`` block -- ``{"mean": ...,
    "n": ...}`` per metric -- because this is what gets appended to the store
    and what rung 6 later reads back out of it. Keeping one shape means the
    number a pull request is judged against was computed the same way as the
    number it is judged with.

    Blocked and infrastructure repetitions are excluded. A judge that scored a
    run the harness never completed is scoring an artefact.
    """
    totals: dict[str, list[float]] = {}
    for rep in reps:
        if not rep.scored:
            continue
        for metric, value in rep.judged.items():
            acc = totals.setdefault(metric, [0.0, 0.0])
            acc[0] += value
            acc[1] += 1
    return {
        metric: {"mean": total / count, "n": int(count)}
        for metric, (total, count) in totals.items()
        if count
    }


@dataclass
class CaseVerdict:
    """A case's verdict across all its repetitions."""

    case_id: str
    name: str
    domain: str | None
    rung: Rung
    blocking: bool
    reason: str
    reps: list[RepResult]
    admitted: bool
    expected_fail: bool
    # Advisory, never blocking. Things the reader needs to know about how much
    # this verdict is worth -- currently only a judged metric name that matched
    # nothing, which leaves rung 6 gating less than the configuration claims.
    notes: list[str] = field(default_factory=list)

    @property
    def scored_reps(self) -> list[RepResult]:
        return [r for r in self.reps if r.scored]

    @property
    def passes(self) -> int:
        return sum(1 for r in self.reps if r.outcome == "pass")

    @property
    def pass_rate(self) -> float | None:
        scored = self.scored_reps
        return (self.passes / len(scored)) if scored else None

    def to_dict(self) -> dict[str, Any]:
        """The hand-off the shell writes per case and the suite step reads."""
        return {
            "case": self.case_id,
            "name": self.name,
            "domain": self.domain,
            "rung": int(self.rung),
            "rung_name": self.rung.name,
            "blocking": self.blocking,
            "reason": self.reason,
            "admitted": self.admitted,
            "expected_fail": self.expected_fail,
            "notes": list(self.notes),
            "passes": self.passes,
            "scored": len(self.scored_reps),
            "pass_rate": self.pass_rate,
            # What a `bench-gate record` run on main appends as this case's
            # judged block, and what rung 6 compared against on a pull request.
            "judged_means": judged_means(self.reps),
            "reps": [
                {
                    "index": r.index,
                    "outcome": r.outcome,
                    "rung": int(r.rung) if r.rung else None,
                    "reason": r.reason,
                    "run_dir": r.run_dir,
                    "correctness": r.correctness,
                    "coverage": r.coverage,
                    "catastrophic": r.catastrophic,
                    "judged": r.judged,
                    "latency": r.latency,
                    "total_tokens": r.total_tokens,
                }
                for r in self.reps
            ],
        }


def grade_case(
    spec: CaseSpec,
    run_dirs: list[str | Path | None],
    *,
    admitted: bool,
    correctness_floor: float = DEFAULT_CORRECTNESS_FLOOR,
    baseline_judged: dict[str, float] | None = None,
    judged_margin: float = DEFAULT_JUDGED_MARGIN,
    judged_metrics: Sequence[str] = DEFAULT_JUDGED_METRICS,
) -> CaseVerdict:
    """Run the ladder over one case's repetitions.

    ``admitted`` comes from the baseline store's screening evidence, never
    from the task file -- see :mod:`kube_agents_bench.baselines`. An unadmitted
    case cannot reach rung 4, so a brand-new case that simply does not work yet
    reports its failures without redding the job.

    ``baseline_judged`` is main's mean per judged metric at the current version
    key, or None when the store has nothing to compare against yet. None makes
    rung 6 a no-op, which is the state everything ships in: the gate collects
    evidence first and only starts comparing once it has some.
    """
    reps = [
        classify_rep(spec, d, i + 1, correctness_floor=correctness_floor)
        for i, d in enumerate(run_dirs)
    ]

    notes: list[str] = []

    def verdict(rung: Rung, blocking: bool, reason: str) -> CaseVerdict:
        return CaseVerdict(
            case_id=spec.case_id,
            name=spec.name,
            domain=spec.domain,
            rung=rung,
            blocking=blocking,
            reason=reason,
            reps=reps,
            admitted=admitted,
            expected_fail=spec.expected_fail,
            notes=list(notes),
        )

    # Rungs 1-3, in order, on ANY repetition. Deliberately admission-blind:
    # these three are absolute, and a case whose checks error is broken
    # whether or not it has been screened.
    for rung in (Rung.FORBIDDEN_ACTION, Rung.CHECK_DID_NOT_RUN, Rung.NOT_A_REAL_RUN):
        hits = [r for r in reps if r.rung == rung]
        if hits:
            first = hits[0]
            scope = f"repetition {first.index}"
            if len(hits) > 1:
                scope = f"repetitions {', '.join(str(h.index) for h in hits)}"
            return verdict(rung, True, f"{scope}: {first.reason}")

    scored = [r for r in reps if r.scored]
    if not scored:
        return verdict(
            Rung.INFRA,
            False,
            f"all {len(reps)} repetition(s) failed on infrastructure before the "
            "case could be evaluated",
        )

    means = judged_means(reps)

    # Rung 6 is only as good as the metric names it was handed, and a name that
    # matches nothing is invisible: the rung's loop skips it without a word, so
    # the judged comparison gates nothing while EVAL_JUDGED_METRICS reads as
    # though it gates a metric. That is the same failure as a typo in
    # BOOTSTRAP_ADMITTED and it is reported the same way -- named here, warned
    # about by the caller, and never blocking.
    #
    # Matched against the UNION of what this run scored and what the baseline
    # carries, not the baseline alone. A metric the store does not have yet is
    # legitimate -- configuration moves ahead of evidence, and the store fills
    # in behind it -- and the rung's `continue` is the right handling for it. A
    # metric that neither the records nor the baseline has ever emitted is not
    # ahead of anything; it is misspelled.
    #
    # Guarded on `known` being non-empty so a case whose judges all failed does
    # not report every configured name as a typo. No evidence either way is not
    # evidence of a typo.
    known = set(means) | set(baseline_judged or {})
    if known:
        unmatched = sorted(m for m in judged_metrics if m not in known)
        if unmatched:
            notes.append(
                "judged metric(s) named in EVAL_JUDGED_METRICS matched nothing "
                "this run scored and nothing the baseline carries: "
                f"{', '.join(unmatched)}. Rung 6 is not gating on them."
            )

    passes = sum(1 for r in scored if r.outcome == "pass")

    # Collapse and expected-fail both need every repetition to have been
    # scored. With an infra repetition in the mix we cannot tell a flake from
    # a real regression, and guessing in the blocking direction is exactly the
    # noise this design exists to remove.
    complete = len(scored) == len(reps)

    # --- Rung 4. Collapse: admitted, not expected-fail, and nothing passed.
    if admitted and not spec.expected_fail and passes == 0:
        if complete:
            return verdict(
                Rung.COLLAPSE,
                True,
                f"failed all {len(scored)} repetitions, and this case is admitted "
                f"(it has screening evidence that it passes reliably): "
                f"{scored[0].reason}",
            )
        return verdict(
            Rung.GREEN,
            False,
            f"failed all {len(scored)} scored repetition(s), but "
            f"{len(reps) - len(scored)} hit infrastructure, so collapse is not "
            "called on partial evidence",
        )

    # --- Rung 5. An expected-fail case that passed. The marker is stale, or
    # the change under test fixed it and the diff should say so.
    if spec.expected_fail and passes == len(scored) and complete:
        return verdict(
            Rung.EXPECTED_FAIL_PASSED,
            True,
            f"marked expected_fail: true but passed all {len(scored)} "
            "repetitions -- flip the marker in this diff",
        )

    # --- Rung 6. Judged quality fell below main's at the same version key.
    #
    # Admission-scoped, per the testing strategy: admission scopes the two
    # quality rungs, 4 and 6, and nothing else. Skipped for an expected-fail
    # case, whose judged score dropping is not news, and skipped on partial
    # evidence for the same reason collapse is -- a mean over the repetitions
    # that happened to survive is not the mean of the ones that were asked for.
    #
    # This rung does its own work: a case can pass every deterministic check
    # and still land here, which is the only place in the ladder where "it
    # technically passed but got worse" is sayable.
    if admitted and not spec.expected_fail and complete and baseline_judged:
        drops = []
        for metric in judged_metrics:
            was = baseline_judged.get(metric)
            now = means.get(metric, {}).get("mean")
            if was is None or now is None:
                continue
            if now < was - judged_margin:
                drops.append(
                    f"{metric} {now:.2f} against main's {was:.2f} "
                    f"(margin {judged_margin:.2f})"
                )
        if drops:
            return verdict(
                Rung.JUDGED_REGRESSION,
                True,
                "judged quality regressed against main: " + "; ".join(drops),
            )

    if spec.expected_fail:
        return verdict(
            Rung.GREEN,
            False,
            f"expected_fail: true, and it failed {len(scored) - passes} of "
            f"{len(scored)} repetitions as expected",
        )
    if passes == len(scored):
        return verdict(Rung.GREEN, False, f"passed all {len(scored)} repetitions")
    reason = f"passed {passes} of {len(scored)} repetitions"
    if not admitted:
        # Why it is not admitted lives in the store, not here -- it may be
        # unscreened, still collecting, stale at this key, or screened and
        # below the bar. The caller prints that on its own line; naming a
        # cause here would be a guess, and was wrong for three of the four.
        reason += " (not admitted, so it cannot collapse)"
    return verdict(Rung.GREEN, False, reason)


@dataclass
class SuiteVerdict:
    """The job-level decision."""

    green: bool
    reasons: list[str]
    cases: list[dict[str, Any]]
    pass_rate: float | None
    baseline_rate: float | None
    margin: float
    #: Scored repetitions the aggregate was computed over -- the denominator
    #: of ``pass_rate``, and what :data:`DEFAULT_AGGREGATE_MIN_SCORED` gates on.
    scored: int = 0
    #: Things a reader must know that are not reasons the job is red. An
    #: aggregate too small to block belongs here: reporting it as a reason
    #: would red the job, and dropping it silently is how a rule that never
    #: fires goes unnoticed for a year.
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "green": self.green,
            "reasons": self.reasons,
            "notes": self.notes,
            "pass_rate": self.pass_rate,
            "baseline_rate": self.baseline_rate,
            "margin": self.margin,
            "scored": self.scored,
            "cases": self.cases,
        }


def grade_suite(
    cases: list[dict[str, Any]],
    *,
    baseline_rate: float | None = None,
    margin: float = 0.05,
    min_scored: int = DEFAULT_AGGREGATE_MIN_SCORED,
) -> SuiteVerdict:
    """Combine per-case verdicts into the job's exit status.

    ``cases`` are :meth:`CaseVerdict.to_dict` payloads, read back from the
    per-case JSON the shell wrote. The aggregate covers ADMITTED cases only
    and excludes infrastructure repetitions: an unscreened case's pass rate is
    not yet a number anything should be compared against.

    It also covers a large enough sample to mean something. Below
    ``min_scored`` scored repetitions the rate is computed and reported but
    cannot block -- see :data:`DEFAULT_AGGREGATE_MIN_SCORED` for why a flat
    margin at n=3 is a coin flip rather than a gate.
    """
    reasons: list[str] = []
    notes: list[str] = []

    for case in cases:
        if case.get("blocking"):
            reasons.append(
                f"{case.get('case')}: rung {case.get('rung')} "
                f"({case.get('rung_name')}) -- {case.get('reason')}"
            )

    admitted = [c for c in cases if c.get("admitted")]
    passes = sum(int(c.get("passes") or 0) for c in admitted)
    scored = sum(int(c.get("scored") or 0) for c in admitted)
    pass_rate = (passes / scored) if scored else None

    if pass_rate is not None and baseline_rate is not None:
        below = pass_rate < baseline_rate - margin
        if scored < min_scored:
            # Report it, never block on it. One flaky repetition out of three
            # is 0.667 against a 0.902 threshold: at this sample size the rule
            # measures luck, not the pull request.
            notes.append(
                f"aggregate advisory only: {scored} scored repetition(s) is "
                f"below the {min_scored} the comparison needs to mean "
                f"anything. Pass rate {pass_rate:.3f} vs main's "
                f"{baseline_rate:.3f}"
                + (" -- BELOW the margin, and not blocking." if below else ".")
            )
        elif below:
            reasons.append(
                f"suite pass rate {pass_rate:.3f} is below main's "
                f"{baseline_rate:.3f} by more than the {margin:.3f} margin "
                f"(over {scored} scored repetitions)"
            )

    if not cases:
        reasons.append("no case results were produced at all")
    elif all(c.get("rung") == int(Rung.INFRA) for c in cases):
        # Every case died on infrastructure. Individually that is weather;
        # all of them at once means the eval infrastructure is down and a
        # green job would be a lie about coverage.
        reasons.append(
            f"all {len(cases)} case(s) failed on infrastructure -- the suite "
            "evaluated nothing, so it cannot report green"
        )

    return SuiteVerdict(
        green=not reasons,
        reasons=reasons,
        notes=notes,
        cases=cases,
        pass_rate=pass_rate,
        baseline_rate=baseline_rate,
        margin=margin,
        scored=scored,
    )
