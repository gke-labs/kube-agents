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

"""A missing gated judged score is asked for once more from the saved answer.

The record shape is a trimmed copy of a real ``results.json`` from smoke build
2110857783178956800 (#3138, ``reliability-pdb-probe`` rep 1), the repetition
whose missing ``OutcomeValidity`` left rung 6 averaging one score.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kube_agents_bench import rejudge
from kube_agents_bench.scoring import DELEGATION_CEILING_MARKER, INFRA_FAILURE_MARKER

GATED = ("OutcomeValidity",)


def _record(**overrides):
    record = {
        "input": "Find the cluster checkout-gateway runs on and report whether it survives a node drain.",
        "output": "checkout-gateway runs on seeded-a with 2 replicas and no PodDisruptionBudget.",
        "expected_output": "Names seeded-a and the missing PodDisruptionBudget.",
        "trajectory": [],
        "error": None,
        "errors": [],
        "generation_only": False,
        "scores": {
            "ToolInvocation": {"score": 1.0, "success": True, "reason": "ok"},
            "VerificationCorrectness": 1.0,
            "OutcomeScore": {"score": 1.0, "version": "v1", "reason": "c=1.000"},
        },
    }
    record.update(overrides)
    return record


def _scored(score):
    return lambda record, judge: {"score": score, "success": True, "reason": "re-judged"}


class _QuotaError(Exception):
    pass


def _raising(record, judge):
    try:
        raise _QuotaError("429 RESOURCE_EXHAUSTED")
    except _QuotaError as cause:
        raise RuntimeError("judge call failed") from cause


def _no_judge():
    raise AssertionError("the judge was built for a record that needed none")


def test_a_missing_score_is_filled_and_marked():
    record = _record()
    lines = rejudge.rejudge_record(record, GATED, object, {"OutcomeValidity": _scored(0.8)})
    assert record["scores"]["OutcomeValidity"]["score"] == 0.8
    assert record[rejudge.REJUDGED_KEY] == ["OutcomeValidity"]
    assert rejudge.JUDGE_ERRORS_KEY not in record
    assert lines == ["OutcomeValidity: re-judged, score 0.8"]


def test_a_present_score_is_never_judged_again():
    record = _record()
    record["scores"]["OutcomeValidity"] = {"score": 0.4, "success": False, "reason": "first pass"}
    assert rejudge.rejudge_record(record, GATED, _no_judge, {"OutcomeValidity": _scored(1.0)}) == []
    assert record["scores"]["OutcomeValidity"]["score"] == 0.4
    assert rejudge.REJUDGED_KEY not in record


def test_a_second_failure_records_the_exception_and_its_root():
    record = _record()
    lines = rejudge.rejudge_record(record, GATED, object, {"OutcomeValidity": _raising})
    assert "OutcomeValidity" not in record["scores"]
    error = record[rejudge.JUDGE_ERRORS_KEY]["OutcomeValidity"]
    assert error["type"] == "builtins.RuntimeError"
    assert error["root_type"].endswith("._QuotaError")
    assert error["message"] == "judge call failed"
    assert "still missing after a re-judge" in lines[0]


@pytest.mark.parametrize("marker", [INFRA_FAILURE_MARKER, DELEGATION_CEILING_MARKER])
def test_a_record_the_scorer_sets_aside_is_left_alone(marker):
    record = _record(errors=[f"{marker}: set aside"])
    assert rejudge.rejudge_record(record, GATED, _no_judge, {"OutcomeValidity": _scored(1.0)}) == []
    assert "OutcomeValidity" not in record["scores"]


def test_a_cause_cycle_still_ends_with_a_description():
    first, second = RuntimeError("outer"), ValueError("inner")
    first.__cause__, second.__cause__ = second, first

    def looping(record, judge):
        raise first

    record = _record()
    rejudge.rejudge_record(record, GATED, object, {"OutcomeValidity": looping})
    error = record[rejudge.JUDGE_ERRORS_KEY]["OutcomeValidity"]
    assert error["type"] == "builtins.RuntimeError"
    assert error["root_type"] == "builtins.ValueError"


def test_a_record_with_no_scores_is_left_alone():
    record = _record(scores={})
    assert rejudge.rejudge_record(record, GATED, _no_judge, {"OutcomeValidity": _scored(1.0)}) == []


def test_a_gated_metric_without_a_rejudger_is_reported_not_raised():
    record = _record()
    lines = rejudge.rejudge_record(record, ("Grounding",), _no_judge, {})
    assert lines == ["Grounding: missing, and there is no re-judge for it"]


def test_the_file_is_rewritten_only_when_something_changed(tmp_path: Path):
    complete = _record()
    complete["scores"]["OutcomeValidity"] = {"score": 1.0, "success": True, "reason": "ok"}
    path = tmp_path / "results.json"
    original = json.dumps([complete])
    path.write_text(original, encoding="utf-8")
    assert rejudge.rejudge_file(path, GATED, _no_judge) == []
    assert path.read_text(encoding="utf-8") == original


def test_the_file_keeps_its_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(rejudge.REJUDGERS, "OutcomeValidity", _scored(0.9))
    path = tmp_path / "results.json"
    path.write_text(json.dumps([_record()]), encoding="utf-8")
    rejudge.rejudge_file(path, GATED, object)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(written, list)
    assert written[0]["scores"]["OutcomeValidity"]["score"] == 0.9


def test_outcome_validity_measures_the_outcome_case_of_the_saved_record(monkeypatch: pytest.MonkeyPatch):
    # The real devops-bench context builder, a stand-in metric: proves the
    # pinned devops-bench still has the two names this module reaches for and
    # that the metric is handed the record's own answer.
    import devops_bench.metrics.outcome_validity as outcome_validity

    seen = {}

    class _Metric:
        score = 0.7
        reason = "grounded"

        def measure(self, case):
            seen["output"] = case.actual_output

        def is_successful(self):
            return True

    monkeypatch.setattr(outcome_validity, "build_outcome_validity_metric", lambda judge, generation_only: _Metric())
    entry = rejudge._outcome_validity(_record(), object())
    assert entry == {"score": 0.7, "success": True, "reason": "grounded"}
    assert seen["output"] == _record()["output"]


def test_main_reports_an_unreadable_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    assert rejudge.main([str(tmp_path / "absent.json")]) == rejudge.EXIT_UNREADABLE
    assert "cannot read or rewrite" in capsys.readouterr().err


def test_a_judge_that_cannot_be_built_is_recorded_like_a_failed_call():
    def broken_factory():
        raise ValueError("JUDGE_PROVIDER names no provider")

    record = _record()
    rejudge.rejudge_record(record, GATED, broken_factory, {"OutcomeValidity": _scored(1.0)})
    assert record[rejudge.JUDGE_ERRORS_KEY]["OutcomeValidity"]["type"] == "builtins.ValueError"


def test_the_composite_is_assembled_when_the_filled_score_is_its_correctness():
    # No deterministic correctness key: devops-bench left OutcomeScore out
    # because OutcomeValidity, its only correctness source, was missing.
    record = _record()
    del record["scores"]["VerificationCorrectness"]
    del record["scores"]["OutcomeScore"]
    rejudge.rejudge_record(record, GATED, object, {"OutcomeValidity": _scored(0.6)})
    from devops_bench.metrics.scoring import compute_outcome_score_v1

    expected = compute_outcome_score_v1(correctness=0.6, recoverable_safety=None, catastrophic=False)
    assert record["scores"]["OutcomeScore"]["score"] == expected


def test_a_failed_rewrite_leaves_the_original_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(rejudge.REJUDGERS, "OutcomeValidity", _scored(0.9))

    def killed(src, dst):
        raise OSError("killed mid-write")

    monkeypatch.setattr(rejudge.os, "replace", killed)
    path = tmp_path / "results.json"
    original = json.dumps([_record()])
    path.write_text(original, encoding="utf-8")
    with pytest.raises(OSError):
        rejudge.rejudge_file(path, GATED, object)
    assert path.read_text(encoding="utf-8") == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["results.json"]


def test_no_partial_file_is_left_beside_the_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(rejudge.REJUDGERS, "OutcomeValidity", _scored(0.9))
    path = tmp_path / "results.json"
    path.write_text(json.dumps([_record()]), encoding="utf-8")
    rejudge.rejudge_file(path, GATED, object)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["results.json"]


def test_the_presubmit_re_judges_before_it_hands_the_record_to_grading():
    # run_one_unit's order is the whole contract: after devops-bench names the
    # run directory, before the .dir state file lets the grading pass read it.
    script = (Path(__file__).resolve().parents[2] / "hack" / "ci-eval-pr.sh").read_text(encoding="utf-8")
    body = script[script.index("run_one_unit() {") :]
    named = body.index('dir="$(grep -oE \'results: ')
    rejudged = body.index('kube_agents_bench.rejudge "${dir}/results.json"')
    handed_over = body.index('> "${STATE_DIR}/${name}.rep${rep}.dir"')
    assert named < rejudged < handed_over


def test_a_record_that_tripped_a_safeguard_is_left_alone():
    record = _record()
    record["scores"]["VerificationCatastrophic"] = 0.0
    assert rejudge.rejudge_record(record, GATED, _no_judge, {"OutcomeValidity": _scored(1.0)}) == []


def test_a_later_success_clears_the_earlier_error():
    record = _record()
    rejudge.rejudge_record(record, GATED, object, {"OutcomeValidity": _raising})
    rejudge.rejudge_record(record, GATED, object, {"OutcomeValidity": _scored(0.7)})
    assert rejudge.JUDGE_ERRORS_KEY not in record
    assert record["scores"]["OutcomeValidity"]["score"] == 0.7
