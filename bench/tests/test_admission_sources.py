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

"""Who admits a case once a store is configured: the record, the list, or nobody.

``fixtures/admission/`` is a local-backend store plus five synthetic task
files, one per state the bridge has to handle. Every store line is at the
version key the captured ``agent-kanban-smoke`` records carry, so grading
those records against a fixture task reads the fixture store for admission
and nothing else. The lines are hand-written and say so in their task files;
they are the shapes seven ordinary nightlies at three repetitions produce,
not captures.

| case                | store                                     | on the list | who decides |
| ------------------- | ----------------------------------------- | ----------- | ----------- |
| `record-admits`     | 21/21 at the current key                  | no          | record      |
| `record-demotes`    | 12/21 at the current key (4 good, 3 bad)  | yes         | record      |
| `record-stale`      | 21/21, all at a superseded judge model    | yes         | bootstrap   |
| `record-collecting` | 9/9 at the current key                    | yes         | bootstrap   |
| `no-record`         | nothing                                   | either      | list or none|

The rule under test: the record governs once it holds a full window at the
current key, either way; the list is the fallback for a case the record
cannot judge yet. Everything here runs through the CLI, the way
``hack/ci-eval-pr.sh`` drives it, with ``--baseline-store`` naming the
fixture directory so the verdict renders its admission column.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import FIXTURE_RUNS, GREEN_RUNS, RED_RUNS
from kube_agents_bench.gate import main

ADMISSION = Path(__file__).parent / "fixtures" / "admission"
JUDGE = "gemini-3.1-pro-preview"
BRIDGE = "record-demotes,record-stale,record-collecting,no-record"
BRIDGE_SENTENCE = "admitted by BOOTSTRAP_ADMITTED (transition bridge)"
REDS = [FIXTURE_RUNS / n for n in RED_RUNS]
GREENS = [FIXTURE_RUNS / n for n in GREEN_RUNS + GREEN_RUNS[:1]]
MIXED = [FIXTURE_RUNS / RED_RUNS[0], FIXTURE_RUNS / RED_RUNS[1], FIXTURE_RUNS / GREEN_RUNS[0]]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in (
        "BOOTSTRAP_ADMITTED",
        "EVAL_AGGREGATE_MARGIN",
        "EVAL_AGGREGATE_MIN_SCORED",
        "EVAL_AGGREGATE_ARMED",
        "EVAL_ADMISSION_RATE",
        "EVAL_ADMISSION_MIN_RUNS",
        "EVAL_JUDGED_MARGIN",
        "EVAL_JUDGED_METRICS",
        "EVAL_BASELINE_STORE",
        "PULL_NUMBER",
        "RC_COMMIT_SHA",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JUDGE_MODEL", JUDGE)
    monkeypatch.setenv("BOOTSTRAP_ADMITTED", BRIDGE)


def grade(case: str, runs: list[Path], tmp_path: Path) -> dict:
    out = tmp_path / f"case-{case}.json"
    argv = [
        "case",
        "--task", str(ADMISSION / "tasks" / case / "task.yaml"),
        "--baseline-dir", str(ADMISSION),
        "--baseline-store", str(ADMISSION),
    ]
    for run in runs:
        argv += ["--result", str(run)]
    assert main([*argv, "--json-out", str(out)]) == 0
    return json.loads(out.read_text(encoding="utf-8"))


def suite(tmp_path: Path, *docs: dict, extra=()) -> tuple[int, str]:
    argv = ["suite", "--baseline-dir", str(ADMISSION), "--baseline-store", str(ADMISSION)]
    for doc in docs:
        path = tmp_path / f"suite-{doc['case']}.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        argv += ["--case-result", str(path)]
    md = tmp_path / "verdict.md"
    rc = main([*argv, "--markdown-out", str(md), *extra])
    return rc, md.read_text(encoding="utf-8")


def test_the_record_admits_a_case_the_list_never_named(tmp_path):
    doc = grade("record-admits", REDS, tmp_path)
    assert doc["admitted"] is True
    assert doc["admission_source"] == "record"
    assert "admitted on 21/21 screening runs across 7 recorded run(s)" in doc["admission_reason"]
    assert doc["rung_name"] == "COLLAPSE" and doc["blocking"] is True
    # Rung 6 has a real comparator now: the record carries judged means.
    assert doc["baseline_judged"] == {"OutcomeValidity": pytest.approx(0.9)}


def test_the_record_demotes_a_case_the_list_still_names(tmp_path):
    """The design's intent: once the evidence exists, the evidence wins.

    Four green nights then three bad ones is the shape of a case that has
    stopped working on main. The list still names it, and it still cannot
    collapse -- a diff that did not break it must not be redded for it.
    """
    doc = grade("record-demotes", REDS, tmp_path)
    assert doc["admitted"] is False
    assert doc["admission_source"] == "record"
    assert "screened at 12/21" in doc["admission_reason"]
    assert "the record overrides BOOTSTRAP_ADMITTED" in doc["admission_reason"]
    assert doc["rung_name"] == "GREEN" and doc["blocking"] is False
    assert "not admitted, so it cannot collapse" in doc["reason"]


def test_a_stale_record_falls_back_to_the_list(tmp_path):
    """Evidence at a superseded key is no evidence about this software, so the
    case rides the bridge -- and the reason says how far it is from being
    judged on its own record."""
    doc = grade("record-stale", REDS, tmp_path)
    assert doc["admitted"] is True
    assert doc["admission_source"] == "bootstrap"
    assert doc["admission_reason"].startswith(BRIDGE_SENTENCE)
    assert "stale: 7 baseline record(s) exist" in doc["admission_reason"]
    assert doc["rung_name"] == "COLLAPSE" and doc["blocking"] is True
    # Nothing at this key, so rung 6 stays quiet even though the case is admitted.
    assert doc["baseline_judged"] is None


def test_a_collecting_record_falls_back_to_the_list(tmp_path):
    doc = grade("record-collecting", REDS, tmp_path)
    assert doc["admitted"] is True
    assert doc["admission_source"] == "bootstrap"
    assert doc["admission_reason"].startswith(BRIDGE_SENTENCE)
    assert "collecting: 9/9 runs recorded" in doc["admission_reason"]
    assert "11 more needed" in doc["admission_reason"]
    assert doc["rung_name"] == "COLLAPSE"


def test_no_record_means_the_list_alone_decides(tmp_path, monkeypatch):
    on = grade("no-record", REDS, tmp_path)
    assert on["admitted"] is True and on["admission_source"] == "bootstrap"
    # The sentence the list has always produced, with nothing appended: the
    # store holds nothing for this case, so there is no state to report.
    assert on["admission_reason"] == BRIDGE_SENTENCE

    monkeypatch.delenv("BOOTSTRAP_ADMITTED")
    off = grade("no-record", REDS, tmp_path)
    assert off["admitted"] is False and off["admission_source"] == "neither"
    assert off["admission_reason"] == "no screening evidence for this case yet"
    assert off["blocking"] is False


def test_the_verdict_names_who_admitted_each_case(tmp_path):
    docs = [
        grade("record-admits", GREENS, tmp_path),
        grade("record-demotes", REDS, tmp_path),
        grade("record-stale", MIXED, tmp_path),
        grade("record-collecting", GREENS, tmp_path),
    ]
    rc, md = suite(tmp_path, *docs)
    assert rc == 0, md
    rows = {ln.split("|")[1].strip(" `"): ln for ln in md.splitlines() if ln.startswith("| `")}
    header = next(ln for ln in md.splitlines() if ln.startswith("| Case"))
    assert "| Admitted by |" in header
    assert "| record |" in rows["record-admits"]
    assert "| record: not admitted |" in rows["record-demotes"]
    assert "| bootstrap |" in rows["record-stale"]
    assert "| bootstrap |" in rows["record-collecting"]


def test_the_aggregate_is_reported_against_main_and_reds_only_when_armed(
    tmp_path, monkeypatch
):
    """Main's side pools the admitted cases that HAVE evidence at their key
    (record-admits, 21/21; record-collecting, 9/9), the pull request's side
    pools every admitted case. 4/9 against 30/30 is far below the margin.
    Reported by default; a reason only once EVAL_AGGREGATE_ARMED says so."""
    docs = [
        grade("record-admits", GREENS, tmp_path),
        grade("record-stale", MIXED, tmp_path),
        grade("record-collecting", REDS[:2] + GREENS[:1], tmp_path),
    ]
    rc, md = suite(tmp_path, *docs, extra=["--min-scored", "9"])
    assert rc == 0
    assert "**GREEN**" in md
    assert "Admitted-case pass rate: 55.6% (main: 100.0%, margin 5.0%)" in md
    assert "aggregate advisory: suite pass rate 0.556 is below main's 1.000" in md
    assert "not armed" in md

    monkeypatch.setenv("EVAL_AGGREGATE_ARMED", "1")
    rc, md = suite(tmp_path, *docs, extra=["--min-scored", "9"])
    assert rc == 1
    assert "### Why it is red" in md
    assert "- suite pass rate 0.556 is below main's 1.000" in md


def test_a_demoted_case_counts_on_neither_side_of_the_aggregate(tmp_path):
    """record-demotes has evidence at the key, but the record turned it away:
    it is not admitted, so neither its own runs nor its 12/21 join the rates."""
    docs = [
        grade("record-admits", GREENS, tmp_path),
        grade("record-demotes", REDS, tmp_path),
    ]
    rc, md = suite(tmp_path, *docs, extra=["--min-scored", "3"])
    assert rc == 0
    assert "Admitted-case pass rate: 100.0% (main: 100.0%, margin 5.0%)" in md


def test_the_column_appears_when_the_record_decided_even_with_no_store_configured(
    tmp_path, monkeypatch
):
    """Evidence landed by hand into the checked-in directory: no
    ``--baseline-store``, no ``EVAL_BASELINE_STORE``, but the record decided
    a case, and that must not be invisible in the verdict."""
    monkeypatch.delenv("EVAL_BASELINE_STORE", raising=False)
    out = tmp_path / "case.json"
    argv = ["case", "--task", str(ADMISSION / "tasks" / "record-admits" / "task.yaml")]
    argv += ["--baseline-dir", str(ADMISSION)]
    for run in GREENS:
        argv += ["--result", str(run)]
    assert main([*argv, "--json-out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["admission_source"] == "record"

    md = tmp_path / "verdict.md"
    rc = main([
        "suite", "--baseline-dir", str(ADMISSION),
        "--case-result", str(out), "--markdown-out", str(md),
    ])
    assert rc == 0
    text = md.read_text(encoding="utf-8")
    assert "| Admitted by |" in text
    assert "| `record-admits` |" in text and "| record |" in text
