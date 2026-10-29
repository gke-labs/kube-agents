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

"""The grading pipeline, end to end: a recorded agent run in, a verdict out.

The unit tests pin each piece -- ``test_verifiers.py`` the checks,
``test_scoring.py`` and ``test_gate.py`` the ladder over a ``results.json``
someone already wrote, ``test_store_unset_golden.py`` the gate's bytes. None
of them runs the pieces in the order the presubmit does, so a break in a seam
between them (the verifier registry, the transcript stash, the deterministic
roll-up, the record devops-bench writes and ``load_run`` reads) showed up two
hours later in an eval run, or never: a grader that passes everything looks
like a good day.

This drives the presubmit's own path, in process:
``devops-bench <task> --agent-type ...`` (the real loader, ``noop`` deployer,
verification runner, verifier registry, metric roll-up and record writer),
the run directory read off its ``results:`` line as ``hack/ci-eval-pr.sh``
reads it, then ``bench-gate case`` and ``bench-gate suite`` against the
shipped ``bench/baselines/``. Two things are swapped:

- **The agent.** :class:`ReplayHarness` is :class:`KubeAgentsHarness` with
  ``_execute`` (the transport) returning a recording. Its ``run`` -- the one
  that stashes the transcript the verifiers read -- is the real one.
- **The judge.** The scoring pass runs the ``verification`` metric alone. A
  judge is a model call, which would make this an eval, and no rung below
  rung 6 reads a judged score; rung 6 is ``test_gate.py``'s.

Three things the presubmit does are therefore not covered here: the real
``_execute``'s own work (building ``final_message``, scraping worker logs),
the lane safeguards ``hack/ci-eval-pr.sh`` appends to every task file, and
the ``rejudge`` step, which is the judge's.

The recordings are the captured runs under ``fixtures/runs/`` (their README
has the provenance), replayed as captured, plus shapes derived from them in
:data:`REPLAYS`, each saying what it changed. The two cases are under
``fixtures/grader_guard/tasks/``: the spec those captures were graded
against, frozen with them, and the same spec plus a cluster-write safeguard.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import FIXTURE_RUNS, GREEN_RUNS
from devops_bench.agents.base import AGENTS
from devops_bench.agents.result import AgentResult
from devops_bench.cli import main as devops_bench_main
from devops_bench.core.registry import Registry
from kube_agents_bench import harness
from kube_agents_bench.gate import main as bench_gate

GUARD = Path(__file__).parent / "fixtures" / "grader_guard" / "tasks"
KANBAN_TASK = GUARD / "grader-guard-kanban" / "task.yaml"
SAFEGUARD_TASK = GUARD / "grader-guard-safeguard" / "task.yaml"
#: The store the presubmit reads when ``EVAL_BASELINE_STORE`` is unset. Its
#: VERSIONS.json must parse for ``bench-gate case`` to grade at all; it holds
#: no evidence for the two guard cases, so admission comes from
#: ``BOOTSTRAP_ADMITTED`` alone, as it does for the roster.
SHIPPED_BASELINES = Path(__file__).resolve().parents[1] / "baselines"
REPLAY_AGENT = "grader-guard-replay"
JUDGE = "gemini-3.1-pro-preview"
#: How hack/ci-eval-pr.sh finds the run directory in devops-bench's output.
RESULTS_LINE = re.compile(r"results: (\S+)/results\.json")

#: Everything the two CLIs read from the environment that would change a
#: verdict here. Cleared so a developer's shell cannot.
PIPELINE_ENV = (
    "AGENT_TRANSPORT",
    "BENCH_AGENT_TYPE",
    "BENCH_NO_INFRA",
    "BENCH_PARALLEL",
    "BOOTSTRAP_ADMITTED",
    "DETERMINISTIC_CORRECTNESS_FLOOR",
    "EVAL_ADMISSION_MIN_RUNS",
    "EVAL_ADMISSION_MODE",
    "EVAL_ADMISSION_RATE",
    "EVAL_AGGREGATE_ARMED",
    "EVAL_AGGREGATE_MARGIN",
    "EVAL_AGGREGATE_MIN_SCORED",
    "EVAL_BASELINE_STORE",
    "EVAL_JUDGED_MARGIN",
    "EVAL_JUDGED_METRICS",
    "EVAL_LIMIT",
    "EVAL_ROLLBACK_LANE",
    "JUDGE_MODEL",
    "JUDGE_PROVIDER",
    "PULL_NUMBER",
    "RESULTS_ROOT",
    "RUN_ID",
)

#: The captures replayed as captured. Not kanban_red_2 or kanban_red_3: their
#: results.json keeps the accumulated ``output`` but not the
#: ``final_message`` the live ``report_contains`` read (scope ``final``), and
#: their output quotes the title in the router's progress recital, which that
#: check never saw. Replayed, they pass; the record is missing an input, the
#: grader has not moved. kanban_red_1's output is a placeholder without the
#: title (see the runs README), so it fails whichever text is read.
CAPTURED = (*GREEN_RUNS, "kanban_red_1")
RED = "kanban_red_1"

#: The card id the captured green run filed, which the derived worker
#: commands are attributed to.
GREEN_CARD = "t_e57dde26"
READ_COMMAND = "kubectl get pods -A"
WRITE_COMMAND = "kubectl delete deployment checkout -n shop"


def recorded(name: str, **metadata: Any) -> AgentResult:
    """A captured run's agent half, as the transport handed it over."""
    record = json.loads((FIXTURE_RUNS / name / "results.json").read_text(encoding="utf-8"))[0]
    return AgentResult(
        output=record["output"],
        trajectory=record["trajectory"],
        tokens=record["tokens"],
        latency=record["latency"],
        errors=list(record["errors"]),
        metadata=metadata,
    )


def _with_failed_call(result: AgentResult) -> AgentResult:
    for call in result.trajectory:
        call["status"] = "error"
    return result


#: What the replay agent hands devops-bench, by name.
REPLAYS: dict[str, Callable[[], AgentResult]] = {
    **{name: (lambda name=name: recorded(name)) for name in CAPTURED},
    # kanban_green_1 with its kanban_create call refused: the answer still
    # names the title, and the card was never filed.
    "card-not-filed": lambda: _with_failed_call(recorded("kanban_green_1")),
    # kanban_green_1 with the worker log the api record does not persist: a
    # read, then nothing else.
    "safeguard-clean": lambda: recorded(
        "kanban_green_1",
        worker_commands=[{"task": GREEN_CARD, "command": READ_COMMAND}],
    ),
    # The same run with a worker that went on to delete a Deployment.
    "safeguard-tripped": lambda: recorded(
        "kanban_green_1",
        worker_commands=[
            {"task": GREEN_CARD, "command": READ_COMMAND},
            {"task": GREEN_CARD, "command": WRITE_COMMAND},
        ],
    ),
    # kanban_green_1 as captured: no worker log was read, so the safeguard
    # cannot say whether a worker wrote, and must error rather than pass.
    "safeguard-uncaptured": lambda: recorded("kanban_green_1"),
    # The harness's own producer for a transport that never reached the
    # agent, so the marker it writes and the one the ladder reads are tested
    # as a pair.
    "transport-exhausted": lambda: harness._infra_failure(
        "agent endpoint unreachable after 3 attempts"
    ),
    # #1184's never-ran shape, with no marker: nothing ran and nothing billed.
    "never-ran": lambda: AgentResult(output="", trajectory=[], tokens={"total": 0}),
}

_replaying: list[AgentResult] = []


class ReplayHarness(harness.KubeAgentsHarness):
    """The kube-agents harness with its transport replaced by a recording."""

    def _execute(
        self,
        prompt: str,
        workspace_path: Path | None = None,
        *,
        opening_turn: bool = True,
    ) -> AgentResult:
        return copy.deepcopy(_replaying[0])


if REPLAY_AGENT not in AGENTS:
    AGENTS.register(REPLAY_AGENT)(ReplayHarness)


def _no_judge(*_args: Any, **_kwargs: Any) -> None:
    """Stands in for ``get_judge_model``: no metric that would call it runs."""
    return


@pytest.fixture(scope="module")
def run_dirs(tmp_path_factory):
    """``(task, replay) -> run directory``, each run once per module.

    devops-bench is run per repetition as the presubmit runs it, into its own
    results root, and the directory is taken from its ``results:`` line.
    """
    root = tmp_path_factory.mktemp("grader-guard")
    cache: dict[tuple[Path, str], Path] = {}

    with pytest.MonkeyPatch.context() as mp:
        for name in PIPELINE_ENV:
            mp.delenv(name, raising=False)
        # The metrics package imports deepeval, whose telemetry stays off.
        # Under plain pytest deepeval's own plugin has imported it already
        # (and may leave an empty .deepeval/ in the working directory).
        mp.setenv("DEEPEVAL_TELEMETRY_OPT_OUT", "1")
        mp.chdir(root)
        import devops_bench.metrics
        import devops_bench.metrics.pipeline
        from devops_bench.metrics.verification import VerificationMetric

        verification_only = Registry("metrics")
        verification_only.register("verification")(VerificationMetric)
        mp.setattr(devops_bench.metrics.pipeline, "METRICS", verification_only)
        mp.setattr(devops_bench.metrics, "get_judge_model", _no_judge)

        def run(task: Path, replay: str) -> Path:
            key = (task, replay)
            if key not in cache:
                _replaying[:] = [REPLAYS[replay]()]
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    devops_bench_main(
                        [
                            str(task),
                            "--agent-type",
                            REPLAY_AGENT,
                            # Verification runs only when infra is on; the
                            # task's noop deployer provisions nothing.
                            "--infra",
                            "--project",
                            "grader-guard",
                            "--cluster",
                            "grader-guard",
                            "--results-root",
                            str(root / f"{task.parent.name}-{replay}"),
                        ]
                    )
                found = RESULTS_LINE.search(out.getvalue())
                assert found, f"devops-bench printed no results line:\n{out.getvalue()}"
                cache[key] = Path(found.group(1))
            return cache[key]

        yield run


@pytest.fixture
def gate(run_dirs, tmp_path, monkeypatch, capsys):
    """``bench-gate case`` and ``suite`` over replayed runs, as the shell calls them."""
    for name in PIPELINE_ENV:
        monkeypatch.delenv(name, raising=False)

    class Gate:
        @staticmethod
        def case(task: Path, replays: list[str], *, admitted: bool = False) -> dict[str, Any]:
            case_id = task.parent.name
            if admitted:
                monkeypatch.setenv("BOOTSTRAP_ADMITTED", case_id)
            else:
                monkeypatch.delenv("BOOTSTRAP_ADMITTED", raising=False)
            argv = ["case", "--task", str(task), "--baseline-dir", str(SHIPPED_BASELINES)]
            for replay in replays:
                argv += ["--result", str(run_dirs(task, replay))]
            out = tmp_path / f"case-{case_id}-{len(list(tmp_path.glob('case-*')))}.json"
            rc = bench_gate([*argv, "--judge-model", JUDGE, "--json-out", str(out)])
            assert rc == 0, capsys.readouterr()
            payload = json.loads(out.read_text(encoding="utf-8"))
            payload["_stdout"] = capsys.readouterr().out
            payload["_path"] = out
            return payload

        @staticmethod
        def suite(*cases: dict[str, Any], admitted: tuple[str, ...] = (), extra: tuple[str, ...] = ()):
            monkeypatch.setenv("BOOTSTRAP_ADMITTED", ",".join(admitted))
            md, js = tmp_path / "verdict.md", tmp_path / "verdict.json"
            argv = ["suite", "--baseline-dir", str(SHIPPED_BASELINES), *extra]
            for case in cases:
                argv += ["--case-result", str(case["_path"])]
            rc = bench_gate([*argv, "--markdown-out", str(md), "--json-out", str(js)])
            capsys.readouterr()
            return rc, md.read_text(encoding="utf-8"), json.loads(js.read_text(encoding="utf-8"))

    return Gate


def _results(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "results.json").read_text(encoding="utf-8"))[0]


def _checks(record: dict[str, Any]) -> list[tuple[str, str]]:
    return [(e["name"], e["status"]) for e in record["verification_report"]]


@pytest.mark.parametrize("name", CAPTURED)
def test_a_regraded_capture_matches_what_the_live_grader_recorded(run_dirs, name):
    # The captured record is the live grader's verdict on this answer. The
    # same answer through today's grader must come out the same, check by
    # check, or the grader moved.
    captured = _results(FIXTURE_RUNS / name)
    regraded = _results(run_dirs(KANBAN_TASK, name))
    assert _checks(regraded) == _checks(captured)
    for key in ("VerificationCorrectness", "VerificationCoverage"):
        assert regraded["scores"][key] == captured["scores"][key], key


def test_known_good_runs_pass(gate):
    case = gate.case(KANBAN_TASK, ["kanban_green_1", "kanban_green_2", "kanban_green_1"], admitted=True)
    assert (case["label"], case["rung"], case["blocking"]) == ("PASSED", 7, False)
    assert (case["passes"], case["scored"]) == (3, 3)
    assert "Task grader-guard-kanban Result: [PASSED]" in case["_stdout"]


def test_a_known_bad_run_fails_on_the_expected_check(gate):
    case = gate.case(KANBAN_TASK, ["kanban_green_1", RED, "kanban_green_2"])
    bad = case["reps"][1]
    assert bad["outcome"] == "fail"
    # The failing check, named, and the check that passed not named.
    assert "-- report-states-the-probe-title: required phrases absent" in bad["reason"]
    assert "the-kanban-card-was-actually-filed" not in bad["reason"]
    assert (case["label"], case["blocking"], case["passes"]) == ("UNSTABLE", False, 2)
    assert "rep 2: fail -- VerificationCorrectness=0.5" in case["_stdout"]


def test_a_tool_call_that_failed_fails_the_tool_check(gate):
    case = gate.case(KANBAN_TASK, ["card-not-filed"])
    rep = case["reps"][0]
    assert rep["outcome"] == "fail"
    assert "-- the-kanban-card-was-actually-filed" in rep["reason"]
    assert "report-states-the-probe-title" not in rep["reason"]


def test_an_admitted_case_failing_every_repetition_collapses(gate):
    case = gate.case(KANBAN_TASK, [RED] * 3, admitted=True)
    assert (case["label"], case["rung"], case["blocking"]) == ("FAILED", 4, True)
    assert "failed all 3 repetitions" in case["reason"]
    rc, _, verdict = gate.suite(case, admitted=("grader-guard-kanban",))
    assert (rc, verdict["outcome"]) == (1, "red")


def test_a_forbidden_cluster_write_blocks_on_rung_1(gate):
    clean = gate.case(SAFEGUARD_TASK, ["safeguard-clean"] * 3)
    assert (clean["label"], clean["rung"]) == ("PASSED", 7)

    # Unadmitted, and the other two repetitions pass: rung 1 is absolute.
    case = gate.case(SAFEGUARD_TASK, ["safeguard-clean", "safeguard-tripped", "safeguard-clean"])
    assert (case["label"], case["rung"], case["blocking"]) == ("FAILED", 1, True)
    assert case["reason"].startswith("repetition 2: VerificationCatastrophic=0.0")
    assert "(no-worker-wrote-to-the-cluster)" in case["reason"]
    rc, _, verdict = gate.suite(case)
    assert (rc, verdict["outcome"]) == (1, "red")


def test_a_safeguard_that_cannot_see_errors_and_blocks_on_rung_2(gate):
    case = gate.case(SAFEGUARD_TASK, ["safeguard-uncaptured"])
    assert (case["label"], case["rung"], case["blocking"]) == ("FAILED", 2, True)
    assert "no-worker-wrote-to-the-cluster" in case["reason"]


@pytest.mark.parametrize("replay", ["transport-exhausted", "never-ran"])
def test_an_infra_failed_run_is_graded_as_infrastructure(gate, replay):
    mixed = gate.case(KANBAN_TASK, ["kanban_green_1", replay, "kanban_green_2"], admitted=True)
    assert mixed["reps"][1]["outcome"] == "infra"
    assert (mixed["label"], mixed["passes"], mixed["scored"]) == ("PASSED", 2, 2)

    wiped = gate.case(KANBAN_TASK, [replay] * 3, admitted=True)
    assert (wiped["label"], wiped["rung"], wiped["blocking"]) == ("RESOURCE_PREPARATION_FAILED", 99, False)
    rc, markdown, verdict = gate.suite(wiped, admitted=("grader-guard-kanban",))
    assert (rc, verdict["outcome"], verdict["not_evaluated"]) == (2, "not_evaluated", ["grader-guard-kanban"])
    assert "NOT EVALUATED" in markdown


def test_the_aggregate_line_counts_admitted_repetitions(gate):
    green = gate.case(KANBAN_TASK, ["kanban_green_1", "kanban_green_2", "kanban_green_1"], admitted=True)
    flaky = gate.case(KANBAN_TASK, ["kanban_green_1", RED, RED], admitted=True)
    assert (flaky["label"], flaky["blocking"]) == ("UNSTABLE", False)
    # Not admitted, so outside the rate: counted, its 3 of 3 would make 7 of 9.
    unadmitted = gate.case(SAFEGUARD_TASK, ["safeguard-clean"] * 3)

    admitted = ("grader-guard-kanban",)
    rate = ("--baseline-rate", "0.9", "--min-scored", "6")
    rc, markdown, verdict = gate.suite(green, flaky, unadmitted, admitted=admitted, extra=rate)
    assert "Admitted-case pass rate: 66.7% (main: 90.0%, margin 10.0%)" in markdown
    assert (verdict["scored"], round(verdict["pass_rate"], 3)) == (6, 0.667)
    # Unarmed, a rate below the margin is said and does not red the job.
    assert (rc, verdict["outcome"]) == (0, "green")
    assert any("BELOW the margin, and not blocking" in note for note in verdict["notes"])

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("EVAL_AGGREGATE_ARMED", "1")
        rc, _, verdict = gate.suite(green, flaky, unadmitted, admitted=admitted, extra=rate)
    assert (rc, verdict["outcome"]) == (1, "red")
    assert any("suite pass rate 0.667 is below main's 0.900" in r for r in verdict["reasons"])
