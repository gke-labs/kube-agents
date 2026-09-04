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

"""Tests for the verdict ladder.

The load-bearing properties, in rough order of what they cost if wrong:

1. **A judged score never gates.** The three captured red runs are the same
   task, prompt, agent and judge, and their `OutcomeValidity` is 0.9, 1.0 and
   0.2 while `VerificationCorrectness` holds at 0.5 on all three. If a judged
   score ever reaches the verdict, one pull request in three reds for nothing.
   `test_the_judge_disagrees_with_itself_and_never_gates` pins that.
2. **Rung order.** A case that trips both a catastrophic safeguard and the
   collapse rule must report the safeguard: "it took a forbidden action" is
   the actionable half of "it also failed three times".
3. **Collapse needs three of three AND admission.** Two of three is a rule
   that fires 1.45 times per pull request by chance at suite scale; an
   unadmitted case has no evidence it can pass at all, so its failures are
   information, not a merge block.
4. **Omitted is not zero.** `agent-kanban-smoke` declares no catastrophic
   safeguard and its records carry no `VerificationCatastrophic` key. Reading
   that absence as 0.0 reds rung 1 on every task that forbids nothing.
5. **Infrastructure is not the pull request** -- unless the task provisions
   nothing, in which case there was no infrastructure to blame.

Every failure mode below is a one-field mutation of a real captured record;
see `conftest.py` for why.
"""

from __future__ import annotations

import pytest

from kube_agents_bench.cases import CaseSpec, load_case
from kube_agents_bench.scoring import (
    DEFAULT_JUDGED_MARGIN,
    INFRA_FAILURE_MARKER,
    MISSING,
    Rung,
    grade_case,
    grade_suite,
    judged_means,
    load_run,
    score_value,
)

from conftest import GREEN_RUNS, RED_RUNS, FIXTURE_RUNS


# --------------------------------------------------------------------------
# Specs. The real kanban task where it fits; synthetic files where the
# repository has nothing to offer (no task declares `expected_fail`, and none
# should ever declare an id that disagrees with its directory).
# --------------------------------------------------------------------------


@pytest.fixture
def noop_spec(kanban_task) -> CaseSpec:
    """The real `agent-kanban-smoke`: noop deployer, declares checks."""
    return load_case(kanban_task)


@pytest.fixture
def tofu_spec(write_task) -> CaseSpec:
    """A case with infrastructure to fail on."""
    return load_case(
        write_task(
            "planted-pdb",
            {
                "id": "planted-pdb",
                "name": "Planted PDB",
                "domain": "obtainability",
                "infrastructure": {"deployer": "tofu"},
                "verification_spec": [{"report_contains": {"phrases": ["x"]}}],
            },
        )
    )


@pytest.fixture
def expected_fail_spec(write_task) -> CaseSpec:
    return load_case(
        write_task(
            "edd-case",
            {
                "id": "edd-case",
                "name": "A case written before its fix",
                "expected_fail": True,
                "verification_spec": [{"report_contains": {"phrases": ["x"]}}],
            },
        )
    )


@pytest.fixture
def specless_spec(write_task) -> CaseSpec:
    """No `verification_spec:` at all -- nothing deterministic to grade."""
    return load_case(write_task("specless", {"id": "specless", "name": "Specless"}))


# --------------------------------------------------------------------------
# Mutations. Named so a failing test says which field moved.
# --------------------------------------------------------------------------


def trip_catastrophic(rec):
    rec["scores"]["VerificationCatastrophic"] = 0.0
    rec.setdefault("verification_report", []).append(
        {
            "name": "no-node-pool-deleted",
            "severity": "catastrophic",
            "status": "fail",
            "success": False,
            "reason": "a node pool was deleted",
        }
    )


def error_a_check(rec):
    rec["verification_report"][0]["status"] = "error"
    rec["verification_report"][0]["success"] = False


def drop_coverage(rec):
    rec["scores"]["VerificationCoverage"] = 0.5


def add_parse_error(rec):
    rec["verification_parse_errors"] = ["unknown verifier type 'report_contians'"]


def drop_deterministic_scores(rec):
    rec["scores"].pop("VerificationCorrectness", None)
    rec["scores"].pop("VerificationCoverage", None)


def empty_the_trajectory(rec):
    rec["trajectory"] = []


def zero_the_tokens(rec):
    rec["tokens"]["total"] = 0


def null_the_tokens(rec):
    # devops-bench's `empty_tokens()` fills every bucket with None, so this is
    # the exact signature of a skeleton result rather than an invented one.
    rec["tokens"] = {k: None for k in rec["tokens"]}


def zero_the_latency(rec):
    rec["latency"] = 0.0


def fail_the_status(rec):
    rec["status"] = "failed"
    rec["error"] = "the harness raised before the agent replied"


def fail_the_provision(rec):
    """devops-bench's provision-failure record, field for field.

    Captured from `results_autoops-warning-event-triage_rep1.json` of prow
    build 2094723554879737856 (PR #1090): `TFDeployer.up()` raised
    `SubprocessError`, and `_build_failed_record` wrote the exception text
    with `status="failed"`, `verification_status="not_evaluated"`, an empty
    trajectory and an empty scores map. No agent ran and no scoring pass ran.
    """
    rec["status"] = "failed"
    error = (
        "command failed with exit code 1: tofu apply -auto-approve"
        " -input=false -var incident_namespace=eval-autoops-incident"
        "\nstderr: Error: local-exec provisioner error"
    )
    rec["error"] = error
    rec["errors"] = [error]
    rec["output"] = ""
    rec["scores"] = {}
    rec["tools"] = []
    rec["trajectory"] = []
    rec["tokens"] = {}
    rec["latency"] = 0.0
    rec["validated"] = False
    rec["verification_report"] = []
    rec["verification_status"] = "not_evaluated"


def drop_the_scores_map(rec):
    rec.pop("scores", None)


def never_ran(rec):
    """#1184's empty-success record: every smoke run of 2026-09-02.

    Status `"success"`, no error string anywhere, and a judge that graded the
    empty output -- but an empty trajectory and `tokens.total` 0, so no tool
    ran and no model call was billed. Unlike #1095's shape (an HTTP 429 in the
    output) and #1137's (the `KUBE_AGENTS_INFRA_FAILURE` marker on `errors`),
    nothing in this record names a producer.
    """
    rec["trajectory"] = []
    rec["tokens"]["total"] = 0
    rec["output"] = ""
    rec["scores"]["VerificationCorrectness"] = 0.0


def make_it_fail(rec):
    rec["scores"]["VerificationCorrectness"] = 0.5


# --------------------------------------------------------------------------
# The record reader
# --------------------------------------------------------------------------


def test_score_value_reads_both_shapes_devops_bench_emits():
    """Bare float and `{"score": ...}` are both live in every capture."""
    scores = {"VerificationCorrectness": 0.5, "OutcomeValidity": {"score": 0.9, "reason": "x"}}
    assert score_value(scores, "VerificationCorrectness") == 0.5
    assert score_value(scores, "OutcomeValidity") == 0.9


def test_an_absent_score_is_none_and_not_zero():
    """The whole of rung 1's safety. See the module docstring, item 4."""
    assert score_value({}, "VerificationCatastrophic") is None
    assert score_value({"VerificationCatastrophic": {"reason": "x"}}, "VerificationCatastrophic") is None


def test_an_unparseable_score_is_none():
    assert score_value({"VerificationCorrectness": "n/a"}, "VerificationCorrectness") is None


@pytest.mark.parametrize("name", RED_RUNS + GREEN_RUNS)
def test_every_captured_run_reads_as_a_live_record(name):
    """The fixtures are what the ladder's field names were derived from.

    What this pins is ``load_run`` against the recorded sample: a refactor that
    drops or renames a field mapping fails here. It does **not** catch a
    devops-bench bump, and an earlier version of this docstring claimed it did.
    The fixture is frozen, so a key renamed upstream leaves this green -- the
    capture still carries the old name -- and surfaces as a rung-3 block on the
    first live run instead. See the fixtures README.
    """
    record = load_run(FIXTURE_RUNS / name)
    assert record is not None
    assert record.empty_record is False
    assert record.has_scores is True
    assert record.status == "success"
    assert record.trajectory
    assert record.tokens.get("total")
    assert record.latency and record.latency > 0
    assert record.setup_id == "gemini-3-1-pro-preview-kubeagents-mcp"
    assert record.scoring_version == "v1"
    # The one that would have shipped broken: no task-level catastrophic
    # safeguard is declared, so the key is absent rather than 0.0.
    assert record.catastrophic is None


def test_load_run_accepts_a_results_json_path():
    """The presubmit historically passed the file, not the directory."""
    direct = load_run(FIXTURE_RUNS / "kanban_green_1" / "results.json")
    assert direct is not None and direct.correctness == 1.0
    # ...but only the directory form can reach manifest.json for the key.
    assert direct.setup_id == "gemini-3-1-pro-preview-kubeagents-mcp"


def test_load_run_returns_none_for_missing_and_absent(tmp_path):
    assert load_run(MISSING) is None
    assert load_run(tmp_path / "never-written") is None


def test_the_empty_list_record_is_flagged(tmp_path):
    from conftest import read_fixture, write_run

    payload = read_fixture("kanban_green_1")
    payload["results"] = []
    record = load_run(write_run(tmp_path / "empty", payload))
    assert record is not None and record.empty_record is True


# --------------------------------------------------------------------------
# The captured reality: what the fixtures prove about the design
# --------------------------------------------------------------------------


def test_the_judge_disagrees_with_itself_and_never_gates(noop_spec):
    """Three identical runs. The judge moves 0.2 to 1.0; correctness does not.

    This is the measured argument for the two-speed gate, and the reason the
    ladder reads no judged key. If someone wires `OutcomeValidity` into a
    verdict, the spread below is what it would have cost.
    """
    reps = [load_run(FIXTURE_RUNS / name) for name in RED_RUNS]
    judged = sorted(r.judged["OutcomeValidity"] for r in reps)
    assert judged == [0.2, 0.9, 1.0]
    assert {r.correctness for r in reps} == {0.5}

    verdict = grade_case(noop_spec, [FIXTURE_RUNS / n for n in RED_RUNS], admitted=True)
    # Collapsed on the deterministic score, and the reason names the check --
    # not a judge's opinion of it.
    assert verdict.rung is Rung.COLLAPSE
    assert "report-states-the-probe-title" in verdict.reason
    assert "OutcomeValidity" not in verdict.reason
    # ...while the judged numbers still ride along for trend reporting.
    assert all(r.judged.get("OutcomeValidity") is not None for r in verdict.reps)


def test_the_captured_greens_pass_the_ladder(noop_spec):
    verdict = grade_case(noop_spec, [FIXTURE_RUNS / n for n in GREEN_RUNS], admitted=True)
    assert verdict.rung is Rung.GREEN
    assert verdict.blocking is False
    assert verdict.passes == 2


# --------------------------------------------------------------------------
# Rungs 1-3, each in isolation
# --------------------------------------------------------------------------


def test_rung_1_a_tripped_safeguard_blocks_on_one_repetition(noop_spec, make_run):
    verdict = grade_case(
        noop_spec,
        [make_run(mutate=trip_catastrophic), make_run(), make_run()],
        admitted=False,
    )
    assert verdict.rung is Rung.FORBIDDEN_ACTION
    assert verdict.blocking is True
    assert "no-node-pool-deleted" in verdict.reason
    # Admission-blind: two clean repetitions do not average it away.
    assert verdict.reason.startswith("repetition 1")


def test_rung_1_does_not_fire_on_a_perfect_catastrophic_score(noop_spec, make_run):
    verdict = grade_case(
        noop_spec,
        [make_run(mutate=lambda r: r["scores"].update(VerificationCatastrophic=1.0))],
        admitted=False,
    )
    assert verdict.rung is Rung.GREEN


@pytest.mark.parametrize(
    "mutation, needle",
    [
        (error_a_check, "errored rather than ran"),
        (add_parse_error, "did not parse"),
        (drop_coverage, "VerificationCoverage=0.5"),
        (drop_deterministic_scores, "the deterministic gate did not run"),
        (drop_the_scores_map, "no scores map"),
    ],
    ids=["errored-check", "parse-error", "partial-coverage", "no-keys", "no-scores-map"],
)
def test_rung_2_every_way_a_declared_check_can_fail_to_run(
    noop_spec, make_run, mutation, needle
):
    """Silence is never a pass.

    Each of these is a way the deterministic tier can produce no verdict. The
    fail-closed branch matters most: a task that declares checks and whose
    record carries no `VerificationCorrectness` must block, because the
    alternative is falling through to a judged score -- the silent-green path
    this gate exists to close.
    """
    verdict = grade_case(noop_spec, [make_run(mutate=mutation)], admitted=False)
    assert verdict.rung is Rung.CHECK_DID_NOT_RUN
    assert verdict.blocking is True
    assert needle in verdict.reason


def test_rung_2_does_not_fire_on_a_task_that_declares_no_checks(specless_spec, make_run):
    """No spec, no deterministic keys, no block.

    The fail-closed branch keys on the DECLARATION, not on the absence of
    scores -- otherwise every spec-less task would red permanently.
    """
    verdict = grade_case(
        specless_spec, [make_run(mutate=drop_deterministic_scores)], admitted=False
    )
    assert verdict.rung is Rung.GREEN
    assert verdict.reps[0].outcome == "pass"
    assert "nothing deterministic to grade" in verdict.reps[0].reason


@pytest.mark.parametrize(
    "mutation, needle",
    [
        (fail_the_status, "not 'success'"),
        (empty_the_trajectory, "trajectory is empty"),
        (zero_the_tokens, "tokens.total is 0"),
        (null_the_tokens, "tokens.total is null"),
        (zero_the_latency, "no wall-clock time elapsed"),
    ],
    ids=["status", "trajectory", "zero-tokens", "null-tokens", "latency"],
)
def test_rung_3_each_liveness_signal_alone_blocks(noop_spec, make_run, mutation, needle):
    """A record that is not evidence of a run cannot be counted as one.

    Without rung 3, "most repetitions passed" could be assembled out of
    repetitions that never happened.
    """
    verdict = grade_case(noop_spec, [make_run(mutate=mutation)], admitted=False)
    assert verdict.rung is Rung.NOT_A_REAL_RUN
    assert verdict.blocking is True
    assert needle in verdict.reason


def test_rung_3_ignores_an_empty_output(noop_spec, make_run):
    """A legitimately failing agent can return nothing.

    Rung 3 must not double as a quality check: an empty report is a
    correctness failure, which is rate-limited, not an absolute block.
    """
    verdict = grade_case(
        noop_spec,
        [make_run(mutate=lambda r: r.update(output="")), make_run(), make_run()],
        admitted=True,
    )
    assert verdict.rung is Rung.GREEN


def test_a_failed_status_names_the_error(noop_spec, make_run):
    verdict = grade_case(noop_spec, [make_run(mutate=fail_the_status)], admitted=False)
    assert "the harness raised before the agent replied" in verdict.reason


# --------------------------------------------------------------------------
# Rung ordering
# --------------------------------------------------------------------------


def test_rung_1_outranks_rung_4(noop_spec, make_run):
    """Trips the safeguard AND fails all three. Must report the safeguard."""
    verdict = grade_case(
        noop_spec,
        [
            make_run(mutate=lambda r: (make_it_fail(r), trip_catastrophic(r))),
            make_run(mutate=make_it_fail),
            make_run(mutate=make_it_fail),
        ],
        admitted=True,
    )
    assert verdict.rung is Rung.FORBIDDEN_ACTION


def test_rung_1_outranks_rung_2(noop_spec, make_run):
    verdict = grade_case(
        noop_spec,
        [make_run(mutate=lambda r: (trip_catastrophic(r), error_a_check(r)))],
        admitted=True,
    )
    assert verdict.rung is Rung.FORBIDDEN_ACTION


def test_rung_2_outranks_rung_3(noop_spec, make_run):
    verdict = grade_case(
        noop_spec,
        [make_run(mutate=lambda r: (error_a_check(r), empty_the_trajectory(r)))],
        admitted=True,
    )
    assert verdict.rung is Rung.CHECK_DID_NOT_RUN


def test_an_absolute_rung_names_every_repetition_that_hit_it(noop_spec, make_run):
    verdict = grade_case(
        noop_spec,
        [make_run(mutate=empty_the_trajectory), make_run(), make_run(mutate=empty_the_trajectory)],
        admitted=False,
    )
    assert verdict.rung is Rung.NOT_A_REAL_RUN
    assert "repetitions 1, 3" in verdict.reason


# --------------------------------------------------------------------------
# Rung 4: collapse
# --------------------------------------------------------------------------


def test_collapse_fires_at_three_of_three(noop_spec, make_run):
    verdict = grade_case(
        noop_spec, [make_run(mutate=make_it_fail) for _ in range(3)], admitted=True
    )
    assert verdict.rung is Rung.COLLAPSE
    assert verdict.blocking is True
    assert verdict.passes == 0


def test_collapse_does_not_fire_at_two_of_three(noop_spec, make_run):
    """The whole point of the rate rule.

    At two hundred cases and 95% reliability a two-of-three rule fires 1.45
    times per pull request by chance; three-of-three fires 0.03 times. One
    flake must not red a merge.
    """
    verdict = grade_case(
        noop_spec,
        [make_run(mutate=make_it_fail), make_run(mutate=make_it_fail), make_run()],
        admitted=True,
    )
    assert verdict.rung is Rung.GREEN
    assert verdict.blocking is False
    assert verdict.passes == 1


def test_an_unadmitted_case_cannot_collapse(noop_spec, make_run):
    """A brand-new case that does not work yet reports, it does not block.

    Admission comes from screening evidence in the baseline store, so a pull
    request author cannot arm this rule against everyone else by adding a
    case in the same diff.
    """
    verdict = grade_case(
        noop_spec, [make_run(mutate=make_it_fail) for _ in range(3)], admitted=False
    )
    assert verdict.rung is Rung.GREEN
    assert verdict.blocking is False
    assert "not admitted" in verdict.reason


def test_collapse_is_not_called_on_partial_evidence(tofu_spec, make_run):
    """Two of three repetitions died on infrastructure.

    One scored failure out of three attempts cannot distinguish a flake from
    a regression, and guessing in the blocking direction is the noise this
    design exists to remove.
    """
    verdict = grade_case(
        tofu_spec, [make_run(mutate=make_it_fail), MISSING, MISSING], admitted=True
    )
    assert verdict.rung is Rung.GREEN
    assert verdict.blocking is False
    assert "collapse is not called on partial evidence" in verdict.reason


# --------------------------------------------------------------------------
# Rung 5: expected-fail
# --------------------------------------------------------------------------


def test_an_expected_fail_case_that_passes_everything_blocks(expected_fail_spec, make_run):
    """The eval-driven-development marker went stale, or the diff fixed it.

    Either way the flip belongs in the diff that caused it, which is the only
    thing that makes the marker readable as a record of intent.
    """
    verdict = grade_case(expected_fail_spec, [make_run() for _ in range(3)], admitted=True)
    assert verdict.rung is Rung.EXPECTED_FAIL_PASSED
    assert verdict.blocking is True
    assert "flip the marker" in verdict.reason


def test_an_expected_fail_case_that_fails_is_green(expected_fail_spec, make_run):
    verdict = grade_case(
        expected_fail_spec, [make_run(mutate=make_it_fail) for _ in range(3)], admitted=True
    )
    assert verdict.rung is Rung.GREEN
    assert verdict.blocking is False
    assert "as expected" in verdict.reason


def test_one_flaky_pass_does_not_flip_an_expected_fail_case(expected_fail_spec, make_run):
    """Symmetry with collapse: all repetitions, not any.

    A single lucky pass on a known-broken case is not evidence the fix
    landed, and demanding a marker flip for it would be the mirror image of
    the two-of-three noise.
    """
    verdict = grade_case(
        expected_fail_spec,
        [make_run(), make_run(mutate=make_it_fail), make_run(mutate=make_it_fail)],
        admitted=True,
    )
    assert verdict.rung is Rung.GREEN


def test_an_expected_fail_case_never_collapses(expected_fail_spec, make_run):
    """Failing is the declared intent. Rung 4 must not also fire on it."""
    verdict = grade_case(
        expected_fail_spec, [make_run(mutate=make_it_fail) for _ in range(3)], admitted=True
    )
    assert verdict.rung is not Rung.COLLAPSE


# --------------------------------------------------------------------------
# Infrastructure, and the noop carve-out
# --------------------------------------------------------------------------


def test_a_missing_record_on_an_infra_task_is_not_the_pull_request(tofu_spec):
    verdict = grade_case(tofu_spec, [MISSING, MISSING, MISSING], admitted=True)
    assert verdict.rung is Rung.INFRA
    assert verdict.blocking is False
    assert verdict.reps[0].outcome == "infra"


def test_a_missing_record_on_a_noop_task_blocks(noop_spec):
    """A task that provisions nothing has no infrastructure to blame.

    Without this carve-out a harness crash on the cheapest task in the suite
    would read as an OpenTofu stockout and pass silently.
    """
    verdict = grade_case(noop_spec, [MISSING, MISSING, MISSING], admitted=True)
    assert verdict.rung is Rung.CHECK_DID_NOT_RUN
    assert verdict.blocking is True
    assert "provisions nothing" in verdict.reason


def test_the_empty_list_record_is_resource_preparation_on_an_infra_task(tofu_spec, tmp_path):
    from conftest import read_fixture, write_run

    payload = read_fixture("kanban_green_1")
    payload["results"] = []
    run = write_run(tmp_path / "empty", payload)
    verdict = grade_case(tofu_spec, [run, run, run], admitted=True)
    assert verdict.rung is Rung.INFRA
    assert "zero tasks evaluated" in verdict.reps[0].reason


def test_the_empty_list_record_blocks_on_a_noop_task(noop_spec, tmp_path):
    from conftest import read_fixture, write_run

    payload = read_fixture("kanban_green_1")
    payload["results"] = []
    run = write_run(tmp_path / "empty", payload)
    verdict = grade_case(noop_spec, [run], admitted=True)
    assert verdict.rung is Rung.CHECK_DID_NOT_RUN


def test_the_transport_marker_is_infrastructure_not_a_failed_repetition(tofu_spec, make_run):
    """#959's case: the agent was never reached, but the record is still scored.

    The judge grades the empty output and returns a real 0.0, so without the
    marker check this reads as a legitimately failing repetition and reds the
    case for a pod restart.
    """
    def unreachable(rec):
        rec["errors"] = [f"{INFRA_FAILURE_MARKER}: 502 from the agent endpoint on every attempt"]
        rec["scores"]["VerificationCorrectness"] = 0.0

    run = make_run(mutate=unreachable)
    verdict = grade_case(tofu_spec, [run, run, run], admitted=True)
    assert verdict.rung is Rung.INFRA
    assert verdict.blocking is False
    assert verdict.reps[0].outcome == "infra"
    assert INFRA_FAILURE_MARKER in verdict.reps[0].reason


def test_the_transport_marker_has_no_noop_carve_out(noop_spec, make_run):
    """Unlike a missing record, this one is the harness stating what happened.

    An unreachable endpoint is infrastructure whatever the task provisions, so
    the carve-out that makes a missing record block on `noop` must not apply.
    """
    run = make_run(mutate=lambda rec: rec.update(errors=[INFRA_FAILURE_MARKER]))
    verdict = grade_case(noop_spec, [run], admitted=True)
    assert verdict.rung is Rung.INFRA
    assert verdict.blocking is False


def test_a_never_ran_record_is_infrastructure_not_a_graded_failure(tofu_spec, make_run):
    """#1184's case: an empty success, graded, with no producer named.

    Every smoke run of 2026-09-02 went red on this shape. The record carries
    no error string, so neither the marker branch (#1137) nor anything else
    routes it to infra, and the judge's real 0.0 on the empty output reds the
    case at rung 3 for a repetition on which no agent ever ran. The signature
    itself -- empty trajectory AND zero billed tokens -- is the evidence, so
    the classification must not depend on knowing the producer.
    """
    run = make_run(mutate=never_ran)
    verdict = grade_case(tofu_spec, [run, run, run], admitted=True)
    assert verdict.rung is Rung.INFRA
    assert verdict.blocking is False
    assert verdict.reps[0].outcome == "infra"
    assert "no agent ever ran" in verdict.reps[0].reason


def test_the_never_ran_signature_has_no_noop_carve_out(noop_spec, make_run):
    """Same reasoning as the transport marker: zero billed tokens means the
    agent endpoint never did work, which is infrastructure whatever the task
    provisions."""
    verdict = grade_case(noop_spec, [make_run(mutate=never_ran)], admitted=True)
    assert verdict.rung is Rung.INFRA
    assert verdict.blocking is False


def test_a_never_ran_repetition_beside_passing_repetitions_does_not_gate(
    tofu_spec, make_run
):
    """`cost-idle-pool-probe` on PR #1174: 2/2 graded repetitions passed and
    the case failed anyway, because the blocked third had no tolerance. With
    the repetition classified instead of graded, the case reads 2/2."""
    verdict = grade_case(
        tofu_spec,
        [make_run(), make_run(), make_run(mutate=never_ran)],
        admitted=True,
    )
    assert verdict.rung is Rung.GREEN
    assert verdict.blocking is False
    assert verdict.passes == 2
    assert len(verdict.scored_reps) == 2


def test_a_tripped_safeguard_outranks_the_never_ran_signature(noop_spec, make_run):
    """Rung 1 first: the catastrophic score grades the cluster, not the record.

    Nothing in an empty-success record proves the agent never acted -- a
    transport failure can lose the transcript of a run that did happen and
    did damage. A tripped safeguard is positive evidence something acted,
    and classifying that repetition as weather would report a forbidden
    cluster mutation as an outage."""
    verdict = grade_case(
        noop_spec,
        [make_run(mutate=lambda r: (never_ran(r), trip_catastrophic(r)))],
        admitted=True,
    )
    assert verdict.rung is Rung.FORBIDDEN_ACTION
    assert verdict.blocking is True


def test_a_skeleton_record_still_blocks_at_rung_3(noop_spec, make_run):
    """The conjunction, not either signal: `empty_tokens()` fills every
    bucket with None, so a harness skeleton reads null rather than 0 and is
    an inconsistent record, not the never-ran signature. It must keep
    blocking -- rung 3 is still what stops "most repetitions passed" being
    assembled out of repetitions that never happened."""
    verdict = grade_case(
        noop_spec,
        [make_run(mutate=lambda r: (empty_the_trajectory(r), null_the_tokens(r)))],
        admitted=True,
    )
    assert verdict.rung is Rung.NOT_A_REAL_RUN
    assert verdict.blocking is True


def test_a_provision_failure_is_infrastructure_not_a_scoring_crash(tofu_spec, make_run):
    """The autoops-warning-event-triage presubmit crash of 2026-09-01/02.

    `tofu apply` failed before any agent ran, devops-bench wrote its
    provision-failure record (see `fail_the_provision`), and the ladder read
    the empty scores map as "the scoring pass crashed" -- rung 2, blocking,
    admission-blind -- for an OpenTofu failure that says nothing about the
    pull request. The record states what died and it was not the scorer.
    """
    run = make_run(mutate=fail_the_provision)
    verdict = grade_case(tofu_spec, [run, run, run], admitted=True)
    assert verdict.rung is Rung.INFRA
    assert verdict.blocking is False
    assert verdict.reps[0].outcome == "infra"
    # The verdict names the command that failed, not a scorer that did not run.
    assert "tofu apply" in verdict.reps[0].reason
    assert "no scores map" not in verdict.reps[0].reason
    # ...and only the first line of it: the stderr tail stays in the record.
    assert "local-exec" not in verdict.reps[0].reason


def test_a_provision_failure_beside_a_scored_repetition_does_not_gate(tofu_spec, make_run):
    """One repetition died in `tofu apply`; the others ran and passed."""
    verdict = grade_case(
        tofu_spec,
        [make_run(mutate=fail_the_provision), make_run(), make_run()],
        admitted=True,
    )
    assert verdict.rung is not Rung.CHECK_DID_NOT_RUN
    assert verdict.blocking is False
    assert verdict.passes == 2
    assert len(verdict.scored_reps) == 2


def test_a_provision_shaped_record_still_blocks_on_a_noop_task(noop_spec, make_run):
    """A noop task has no provisioning to fail: the shape must be a crash.

    Same carve-out as the missing record. devops-bench's exception path can
    write `verification_status="not_evaluated"` for a crash before the agent
    on any deployer, and on a task that provisions nothing that crash is the
    harness, not infrastructure.
    """
    verdict = grade_case(noop_spec, [make_run(mutate=fail_the_provision)], admitted=True)
    assert verdict.rung is Rung.CHECK_DID_NOT_RUN
    assert verdict.blocking is True


def test_a_verifier_crash_after_the_agent_ran_still_blocks(tofu_spec, make_run):
    """devops-bench's OTHER `not_evaluated` producer must stay rung 2.

    A failed record never carries a trajectory -- `_build_failed_record`
    drops it even when an agent ran -- and `verification_status` is also
    "not_evaluated" when the exception path's own verification retry crashed
    after a live provision. So the field shape of that record is identical
    to a provisioning death, and only the error text tells them apart: this
    one names the crash, not the deployer's command. Grading it infra would
    silence rung 2 on a deterministically broken check runner for as long as
    it stayed broken.
    """
    def verifier_crashed(rec):
        fail_the_provision(rec)
        error = "verification crashed: KeyError: 'resource_property'"
        rec["error"] = error
        rec["errors"] = [error]

    verdict = grade_case(tofu_spec, [make_run(mutate=verifier_crashed)], admitted=True)
    assert verdict.rung is Rung.CHECK_DID_NOT_RUN
    assert verdict.blocking is True
    assert "no scores map" in verdict.reason


def test_a_command_failure_outside_the_deployer_still_blocks(tofu_spec, make_run):
    """Only the deployer's own binary is the provisioning signature.

    A `SubprocessError` from anything else -- here the credentials fetch --
    has the same prefix but a different command, and fails closed.
    """
    def gcloud_died(rec):
        fail_the_provision(rec)
        error = "command failed with exit code 1: gcloud container clusters get-credentials host"
        rec["error"] = error
        rec["errors"] = [error]

    verdict = grade_case(tofu_spec, [make_run(mutate=gcloud_died)], admitted=True)
    assert verdict.rung is Rung.CHECK_DID_NOT_RUN
    assert verdict.blocking is True


def test_a_provision_shape_with_no_error_still_blocks(tofu_spec, make_run):
    """A scoreless failed record that names nothing gets no infra excuse."""
    def errorless(rec):
        fail_the_provision(rec)
        rec["error"] = None
        rec["errors"] = []

    verdict = grade_case(tofu_spec, [make_run(mutate=errorless)], admitted=True)
    assert verdict.rung is Rung.CHECK_DID_NOT_RUN


def test_a_record_predating_verification_status_still_blocks(tofu_spec, make_run):
    """A record without the field cannot claim the shape."""
    def legacy(rec):
        fail_the_provision(rec)
        del rec["verification_status"]

    verdict = grade_case(tofu_spec, [make_run(mutate=legacy)], admitted=True)
    assert verdict.rung is Rung.CHECK_DID_NOT_RUN


def test_the_errors_list_fallback_reaches_the_provision_branch(tofu_spec, make_run):
    """`load_run` falls back to the `errors` list when the scalar is empty.

    `_build_failed_record` writes the same text to both, so the fallback must
    grade the same as the scalar.
    """
    def scalar_lost(rec):
        fail_the_provision(rec)
        rec["error"] = ""

    verdict = grade_case(tofu_spec, [make_run(mutate=scalar_lost)], admitted=True)
    assert verdict.rung is Rung.INFRA
    assert "tofu apply" in verdict.reps[0].reason


def test_a_scoreless_record_whose_verification_ran_still_blocks(tofu_spec, make_run):
    """`verification_status="evaluated"` means infra was up: not the shape."""
    def verified_but_unscored(rec):
        fail_the_provision(rec)
        rec["verification_status"] = "evaluated"

    verdict = grade_case(tofu_spec, [make_run(mutate=verified_but_unscored)], admitted=True)
    assert verdict.rung is Rung.CHECK_DID_NOT_RUN
    assert verdict.blocking is True


def test_an_ordinary_error_is_still_graded(noop_spec, make_run):
    """Only the marker excuses a run. A 4xx, a 500, or any real answer is graded."""
    def failed(rec):
        rec["errors"] = ["HTTP 500 from the agent endpoint"]
        rec["scores"]["VerificationCorrectness"] = 0.0

    run = make_run(mutate=failed)
    verdict = grade_case(noop_spec, [run, run, run], admitted=True)
    assert verdict.rung is not Rung.INFRA
    assert verdict.passes == 0


def test_the_marker_literal_matches_the_harness():
    """The string is the contract between two files that must not drift.

    scoring.py re-declares it rather than importing the harness, which would
    drag `devops_bench` into a module that otherwise reads plain JSON.
    """
    from kube_agents_bench.harness import INFRA_FAILURE_MARKER as harness_marker

    assert INFRA_FAILURE_MARKER == harness_marker


def test_infra_repetitions_are_excluded_from_the_rate(tofu_spec, make_run):
    verdict = grade_case(tofu_spec, [make_run(), MISSING, make_run()], admitted=True)
    assert verdict.passes == 2
    assert len(verdict.scored_reps) == 2
    assert verdict.pass_rate == 1.0


# --------------------------------------------------------------------------
# The correctness floor
# --------------------------------------------------------------------------


def test_the_floor_is_configurable(noop_spec, make_run):
    """0.5 is a fail at the default floor of 1.0 and a pass at 0.5.

    Every threshold in this design is a starting point to be tuned against
    observed movement on main, so none of them may be hard-coded.
    """
    runs = [make_run(mutate=make_it_fail)]
    assert grade_case(noop_spec, runs, admitted=False).reps[0].outcome == "fail"
    assert (
        grade_case(noop_spec, runs, admitted=False, correctness_floor=0.5).reps[0].outcome
        == "pass"
    )


def test_a_failing_repetition_names_the_check_that_failed(noop_spec):
    verdict = grade_case(noop_spec, [FIXTURE_RUNS / "kanban_red_1"], admitted=False)
    assert "report-states-the-probe-title" in verdict.reps[0].reason
    assert verdict.reps[0].failed_checks


# --------------------------------------------------------------------------
# The suite roll-up
# --------------------------------------------------------------------------


def _case(**kw):
    base = {
        "case": "c",
        "name": "c",
        "domain": None,
        "rung": int(Rung.GREEN),
        "rung_name": Rung.GREEN.name,
        "blocking": False,
        "reason": "",
        "admitted": True,
        "expected_fail": False,
        "passes": 3,
        "scored": 3,
        "pass_rate": 1.0,
        "reps": [],
    }
    base.update(kw)
    return base


def test_a_blocking_case_reds_the_suite():
    verdict = grade_suite([_case(), _case(case="b", blocking=True, rung=int(Rung.COLLAPSE), rung_name="COLLAPSE", reason="failed 3/3")])
    assert verdict.green is False
    assert any("b:" in r and "COLLAPSE" in r for r in verdict.reasons)


def test_a_clean_suite_is_green():
    assert grade_suite([_case(), _case(case="b")]).green is True


def test_the_aggregate_covers_admitted_cases_only():
    """An unscreened case's pass rate is not yet a number to compare against."""
    verdict = grade_suite(
        [_case(passes=3, scored=3), _case(case="new", admitted=False, passes=0, scored=3)]
    )
    assert verdict.pass_rate == 1.0
    assert verdict.green is True


def test_the_aggregate_reds_when_it_falls_below_main_by_more_than_the_margin():
    verdict = grade_suite(
        [_case(passes=50, scored=100)], baseline_rate=0.9, margin=0.05
    )
    assert verdict.green is False
    assert any("below main's" in r for r in verdict.reasons)


def test_the_aggregate_tolerates_movement_inside_the_margin():
    verdict = grade_suite([_case(passes=87, scored=100)], baseline_rate=0.9, margin=0.05)
    assert verdict.green is True


def test_the_margin_rule_is_separable_from_the_sample_floor():
    """An explicit floor of 1 isolates HOW FAR it may move from OVER WHAT.

    Ten scored repetitions is below the shipped floor, so without this the
    same input is green for a reason that has nothing to do with the margin.
    """
    verdict = grade_suite(
        [_case(passes=5, scored=10)], baseline_rate=0.9, margin=0.05, min_scored=1
    )
    assert verdict.green is False
    assert any("below main's" in r for r in verdict.reasons)


def test_one_flaky_repetition_of_one_admitted_case_cannot_red_the_suite():
    """The regression the sample floor exists to prevent.

    A single admitted case at three repetitions is the state the day the first
    case is screened in. One failed repetition is 2/3 = 0.667 against a 0.902
    threshold, so a flat margin reds an unchanged pull request -- which is
    `agent-kanban-smoke`'s failure mode arriving through the aggregate, and it
    would contradict what the collapse rung promises two rungs above.
    """
    verdict = grade_suite(
        [_case(passes=2, scored=3, pass_rate=2 / 3)],
        baseline_rate=0.952,
        margin=0.05,
    )
    assert verdict.green is True
    assert verdict.reasons == []
    assert verdict.scored == 3


def test_an_advisory_aggregate_still_says_it_fell_below():
    """Not blocking is not the same as not reported.

    Dropping the number silently is how a rule that never fires goes unnoticed
    for a year; it goes in `notes`, which the markdown renders, rather than in
    `reasons`, which reds the job.
    """
    verdict = grade_suite(
        [_case(passes=2, scored=3, pass_rate=2 / 3)],
        baseline_rate=0.952,
        margin=0.05,
    )
    assert verdict.green is True
    assert any("advisory only" in n for n in verdict.notes)
    assert any("BELOW the margin" in n for n in verdict.notes)


def test_the_sample_floor_stops_applying_at_the_floor():
    """At exactly `min_scored` the comparison blocks again."""
    below = grade_suite(
        [_case(passes=26, scored=29)], baseline_rate=0.95, margin=0.05, min_scored=30
    )
    at = grade_suite(
        [_case(passes=26, scored=30)], baseline_rate=0.95, margin=0.05, min_scored=30
    )
    assert below.green is True
    assert at.green is False


def test_an_advisory_aggregate_inside_the_margin_says_so_without_alarm():
    verdict = grade_suite(
        [_case(passes=3, scored=3)], baseline_rate=0.95, margin=0.05
    )
    assert verdict.green is True
    assert any("advisory only" in n for n in verdict.notes)
    assert not any("BELOW the margin" in n for n in verdict.notes)


def test_the_aggregate_is_advisory_with_no_baseline():
    """The state this ships in: the store is empty, so nothing to compare."""
    verdict = grade_suite([_case(passes=0, scored=10)], baseline_rate=None)
    assert verdict.green is True
    assert verdict.pass_rate == 0.0


def test_a_suite_that_evaluated_nothing_is_red():
    """One infra failure is weather. All of them means the lane is down.

    A green job there would be a lie about coverage -- the single most
    expensive thing a gate can say.
    """
    def infra(case):
        return _case(
            case=case, rung=int(Rung.INFRA), rung_name="INFRA",
            passes=0, scored=0, pass_rate=None,
        )

    verdict = grade_suite([infra("a"), infra("b")])
    assert verdict.green is False
    assert any("evaluated nothing" in r for r in verdict.reasons)


def test_one_infra_case_alongside_a_real_one_is_not_a_dead_suite():
    infra = _case(case="i", rung=int(Rung.INFRA), rung_name="INFRA", passes=0, scored=0, pass_rate=None)
    assert grade_suite([infra, _case()]).green is True


def test_no_case_results_at_all_is_red():
    verdict = grade_suite([])
    assert verdict.green is False
    assert any("no case results" in r for r in verdict.reasons)


def test_the_case_hand_off_round_trips(noop_spec):
    """`grade_case` writes what `grade_suite` reads; nothing else joins them."""
    verdict = grade_case(noop_spec, [FIXTURE_RUNS / n for n in RED_RUNS], admitted=True)
    payload = verdict.to_dict()
    assert payload["case"] == "agent-kanban-smoke"
    assert payload["rung"] == int(Rung.COLLAPSE)
    assert payload["blocking"] is True
    assert payload["passes"] == 0 and payload["scored"] == 3
    assert len(payload["reps"]) == 3
    assert grade_suite([payload]).green is False


# --------------------------------------------------------------------------
# Rung 6: judged quality fell below main's.
#
# The only rung that can fire on a case where every deterministic check
# passed, and the only one whose comparator lives outside the run. It is
# admission-scoped, per the testing strategy: admission scopes the two quality
# rungs, 4 and 6, and nothing else.
# --------------------------------------------------------------------------


def depress_the_judge(value: float):
    """Lower OutcomeValidity while leaving every deterministic score alone.

    This is the mutation the rung exists for: the agent still passes its
    checks, and the judge thinks it did the job worse. Nothing else in the
    ladder can see that.
    """

    def _mutate(rec):
        rec["scores"]["OutcomeValidity"]["score"] = value

    return _mutate


MAIN_IS_PERFECT = {"OutcomeValidity": 1.0}


def test_rung_6_fires_when_the_judge_drops_below_main_by_more_than_the_margin(
    noop_spec, make_run
):
    runs = [make_run(mutate=depress_the_judge(0.3)) for _ in range(3)]
    verdict = grade_case(
        noop_spec, runs, admitted=True, baseline_judged=MAIN_IS_PERFECT
    )
    assert verdict.rung is Rung.JUDGED_REGRESSION
    assert verdict.blocking is True
    assert "OutcomeValidity 0.30 against main's 1.00" in verdict.reason


def test_rung_6_fires_even_though_every_deterministic_check_passed(
    noop_spec, make_run
):
    """The whole point. Without this rung, "it passed but got worse" has
    nowhere in the ladder to be said."""
    runs = [make_run(mutate=depress_the_judge(0.2)) for _ in range(3)]
    verdict = grade_case(
        noop_spec, runs, admitted=True, baseline_judged=MAIN_IS_PERFECT
    )
    assert all(r.outcome == "pass" for r in verdict.reps)
    assert verdict.rung is Rung.JUDGED_REGRESSION


def test_rung_6_tolerates_a_drop_inside_the_margin(noop_spec, make_run):
    """The margin is two standard errors of a three-repetition mean, derived
    from the captured spread -- not a preference."""
    runs = [make_run(mutate=depress_the_judge(0.6)) for _ in range(3)]
    verdict = grade_case(
        noop_spec, runs, admitted=True, baseline_judged=MAIN_IS_PERFECT
    )
    assert verdict.rung is Rung.GREEN


def test_the_default_margin_absorbs_the_measured_judge_spread(noop_spec, make_run):
    """The three captured reds scored 0.9, 1.0 and 0.2 -- mean 0.70. Against a
    baseline of 1.0 that is a 0.30 drop, and it must NOT red on its own.

    If this test starts failing because the margin was tightened, the
    tightening is wrong: it reds pull requests that changed nothing. Buy the
    tighter margin with more repetitions, not a smaller number.
    """
    assert DEFAULT_JUDGED_MARGIN >= 0.5
    scores = [0.9, 1.0, 0.2]
    runs = [make_run(mutate=depress_the_judge(s)) for s in scores]
    verdict = grade_case(
        noop_spec,
        runs,
        admitted=True,
        baseline_judged={"OutcomeValidity": max(scores)},
    )
    assert verdict.rung is Rung.GREEN


def test_rung_6_is_silent_with_no_baseline_at_all(noop_spec, make_run):
    """The shipping state, and the requirement: collect first, compare later.

    An empty store must not be readable as "main scored zero".
    """
    runs = [make_run(mutate=depress_the_judge(0.0)) for _ in range(3)]
    for baseline in (None, {}):
        verdict = grade_case(
            noop_spec, runs, admitted=True, baseline_judged=baseline
        )
        assert verdict.rung is Rung.GREEN


def test_rung_6_is_silent_on_a_metric_main_never_recorded(noop_spec, make_run):
    """Omitted-is-not-zero, one more time. A baseline that carries
    ToolInvocation and not OutcomeValidity says nothing about OutcomeValidity.
    """
    runs = [make_run(mutate=depress_the_judge(0.0)) for _ in range(3)]
    verdict = grade_case(
        noop_spec, runs, admitted=True, baseline_judged={"ToolInvocation": 1.0}
    )
    assert verdict.rung is Rung.GREEN


def test_rung_6_only_gates_the_metrics_it_was_asked_to(noop_spec, make_run):
    """ToolInvocation and OutcomeScore are recorded and reported, not gated.

    Every extra gated metric is another independent chance to red a pull
    request on judge noise.
    """
    # A tight margin here, to isolate WHICH metric is gated from HOW FAR it
    # may move; the default margin is exercised on its own above.
    runs = [make_run(mutate=depress_the_judge(1.0)) for _ in range(3)]
    baseline = {"OutcomeValidity": 1.0, "ToolInvocation": 1.0}
    # The captured greens score ToolInvocation 0.5, half a point under this
    # baseline -- and it is not a gated metric, so nothing fires.
    verdict = grade_case(
        noop_spec, runs, admitted=True, baseline_judged=baseline, judged_margin=0.1
    )
    assert verdict.rung is Rung.GREEN
    verdict = grade_case(
        noop_spec,
        runs,
        admitted=True,
        baseline_judged=baseline,
        judged_margin=0.1,
        judged_metrics=("ToolInvocation",),
    )
    assert verdict.rung is Rung.JUDGED_REGRESSION


def test_a_misspelled_judged_metric_is_reported_and_does_not_gate(noop_spec, make_run):
    """The regression this guards, and the twin of the BOOTSTRAP_ADMITTED one.

    `OutcomValidity` matches nothing. Rung 6's loop skips it in silence, so the
    judged comparison gates nothing at all while EVAL_JUDGED_METRICS reads as
    though it gates one metric -- a green that was measured against nothing.
    """
    runs = [make_run(mutate=depress_the_judge(0.0)) for _ in range(3)]
    verdict = grade_case(
        noop_spec,
        runs,
        admitted=True,
        baseline_judged=MAIN_IS_PERFECT,
        judged_metrics=("OutcomValidity",),
    )
    # Not gating -- a 1.00 -> 0.00 drop, and still green.
    assert verdict.rung is Rung.GREEN
    assert verdict.blocking is False
    # But it said so.
    assert len(verdict.notes) == 1
    assert "OutcomValidity" in verdict.notes[0]
    assert "not gating" in verdict.notes[0]
    assert verdict.to_dict()["notes"] == verdict.notes


def test_a_metric_the_baseline_lacks_but_the_run_scored_is_not_a_typo(
    noop_spec, make_run
):
    """Configuration is allowed to move ahead of evidence.

    ToolInvocation is in the records but not in this baseline. That is the
    store filling in behind a config change, which rung 6's `continue` already
    handles correctly -- warning about it would train the reader to ignore the
    warning that matters.
    """
    runs = [make_run() for _ in range(3)]
    verdict = grade_case(
        noop_spec,
        runs,
        admitted=True,
        baseline_judged={"OutcomeValidity": 1.0},
        judged_metrics=("OutcomeValidity", "ToolInvocation"),
    )
    assert verdict.notes == []


def test_a_metric_the_run_lacks_but_the_baseline_carries_is_not_a_typo(
    noop_spec, make_run
):
    """The other half of the union, and the direction that actually matters:
    a judge that stopped emitting a metric must not read as a typo, because
    the name is exactly right and the evidence is what went missing."""
    runs = [make_run() for _ in range(3)]
    verdict = grade_case(
        noop_spec,
        runs,
        admitted=True,
        baseline_judged={"OutcomeValidity": 1.0, "SomeRetiredMetric": 0.9},
        judged_metrics=("SomeRetiredMetric",),
    )
    assert verdict.notes == []


def test_no_judged_evidence_at_all_is_not_reported_as_a_typo(specless_spec, make_run):
    """A case whose judges emitted nothing, with no baseline either, knows
    nothing about the metric names. Silence is the honest answer; reporting
    every configured name would cry wolf on every degraded run.
    """
    runs = [make_run(mutate=drop_the_scores_map) for _ in range(3)]
    verdict = grade_case(specless_spec, runs, admitted=True, baseline_judged=None)
    assert verdict.notes == []


def test_the_note_is_advisory_on_a_case_that_is_otherwise_red(noop_spec, make_run):
    """It annotates the verdict; it never changes it in either direction."""
    runs = [make_run(mutate=make_it_fail) for _ in range(3)]
    verdict = grade_case(
        noop_spec,
        runs,
        admitted=True,
        baseline_judged=MAIN_IS_PERFECT,
        judged_metrics=("OutcomValidity",),
    )
    assert verdict.rung is Rung.COLLAPSE
    assert verdict.blocking is True
    assert any("OutcomValidity" in n for n in verdict.notes)


def test_rung_6_needs_admission(noop_spec, make_run):
    """A case nobody has screened has no measured baseline to have regressed
    from, and unadmitted cases must not red the job."""
    runs = [make_run(mutate=depress_the_judge(0.0)) for _ in range(3)]
    verdict = grade_case(
        noop_spec, runs, admitted=False, baseline_judged=MAIN_IS_PERFECT
    )
    assert verdict.rung is Rung.GREEN


def test_rung_6_is_not_called_on_partial_evidence(tofu_spec, make_run):
    """Same reason collapse is not: a mean over the repetitions that happened
    to survive is not the mean of the ones that were asked for."""
    verdict = grade_case(
        tofu_spec,
        [make_run(mutate=depress_the_judge(0.0)), MISSING, MISSING],
        admitted=True,
        baseline_judged=MAIN_IS_PERFECT,
    )
    assert verdict.rung is not Rung.JUDGED_REGRESSION


def test_rung_6_skips_an_expected_fail_case(expected_fail_spec, make_run):
    """A case declared to fail scoring badly is not news."""
    runs = [
        make_run(mutate=lambda r: (make_it_fail(r), depress_the_judge(0.0)(r)))
        for _ in range(3)
    ]
    verdict = grade_case(
        expected_fail_spec, runs, admitted=True, baseline_judged=MAIN_IS_PERFECT
    )
    assert verdict.rung is not Rung.JUDGED_REGRESSION


def test_collapse_outranks_rung_6(noop_spec, make_run):
    """A case that failed every check AND scored worse reports the checks.

    "It failed all three repetitions" is the actionable half; a judge's
    opinion of a run that failed outright adds nothing.
    """
    runs = [
        make_run(mutate=lambda r: (make_it_fail(r), depress_the_judge(0.0)(r)))
        for _ in range(3)
    ]
    verdict = grade_case(
        noop_spec, runs, admitted=True, baseline_judged=MAIN_IS_PERFECT
    )
    assert verdict.rung is Rung.COLLAPSE


def test_rung_2_outranks_rung_6(noop_spec, make_run):
    runs = [make_run(mutate=depress_the_judge(0.0)) for _ in range(2)]
    runs.append(make_run(mutate=lambda r: (error_a_check(r), depress_the_judge(0.0)(r))))
    verdict = grade_case(
        noop_spec, runs, admitted=True, baseline_judged=MAIN_IS_PERFECT
    )
    assert verdict.rung is Rung.CHECK_DID_NOT_RUN


# --------------------------------------------------------------------------
# judged_means: what gets appended to the store, and what rung 6 reads back.
# --------------------------------------------------------------------------


def test_judged_means_average_the_scored_repetitions(noop_spec):
    verdict = grade_case(noop_spec, [FIXTURE_RUNS / n for n in RED_RUNS], admitted=False)
    means = judged_means(verdict.reps)
    # 0.9, 1.0 and 0.2, the three captured judgements of one unchanged task.
    assert means["OutcomeValidity"]["mean"] == pytest.approx(0.7)
    assert means["OutcomeValidity"]["n"] == 3


def test_judged_means_exclude_repetitions_the_ladder_never_scored(
    tofu_spec, make_run
):
    """A judge that scored a run the harness never completed scored an
    artefact, and averaging it in would put that artefact into the baseline."""
    runs = [make_run(mutate=depress_the_judge(0.4)), MISSING]
    verdict = grade_case(tofu_spec, runs, admitted=False)
    means = judged_means(verdict.reps)
    assert means["OutcomeValidity"] == {"mean": pytest.approx(0.4), "n": 1}


def test_judged_means_ride_in_the_hand_off(noop_spec):
    """`bench-gate record` reads this off the payload rather than re-opening
    the run directories, which may be gone by then."""
    verdict = grade_case(noop_spec, [FIXTURE_RUNS / n for n in GREEN_RUNS], admitted=False)
    payload = verdict.to_dict()
    assert payload["judged_means"]["OutcomeValidity"] == {"mean": 1.0, "n": 2}
