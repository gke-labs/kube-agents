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

"""Who admits a case once a store is configured, under each admission mode.

``fixtures/admission/`` is a local-backend store plus five synthetic task
files, one per state the gate has to handle. Every store line is at the
version key the captured ``agent-kanban-smoke`` records carry, so grading
those records against a fixture task reads the fixture store for admission
and nothing else. The lines are hand-written and say so in their task files;
they are the shapes seven ordinary nightlies at three repetitions produce,
not captures.

| case                | store                                     | on the list | record says              |
| ------------------- | ----------------------------------------- | ----------- | ------------------------ |
| `record-admits`     | 21/21 at the current key                  | no          | would admit              |
| `record-demotes`    | 12/21 at the current key (4 good, 3 bad)  | yes         | would demote             |
| `record-stale`      | 21/21, all at a superseded judge model    | yes         | stale                    |
| `record-collecting` | 9/9 at the current key                    | yes         | collecting 9/20          |
| `no-record`         | nothing                                   | either      | none                     |

Two modes, one store. ``EVAL_ADMISSION_MODE=roster`` (the default, by the
decision on #1493): the list decides rung 4 and the record's verdict is a
recommendation the reason and the verdict column carry. ``record``: the
record governs once it holds a full window, either way, and the list is the
fallback while it cannot judge. The three states short of a full window
(stale, collecting, none) resolve the same way in both modes. Everything
here runs through the CLI, the way ``hack/ci-eval-pr.sh`` drives it, with
``--baseline-store`` naming the fixture directory so the verdict renders its
admission column.
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
ROSTER_DECIDES = "-- advisory; the roster decides (EVAL_ADMISSION_MODE=roster)"
KEY_LABEL = "gemini-3-1-pro-preview-kubeagents-mcp/gemini-3.1-pro-preview/v1-f1-v1"
WOULD_ADMIT = f"record: would admit (21/21 at key {KEY_LABEL})"
WOULD_DEMOTE = "record: would demote (12/21, below 95% over 20)"
REDS = [FIXTURE_RUNS / n for n in RED_RUNS]
GREENS = [FIXTURE_RUNS / n for n in GREEN_RUNS + GREEN_RUNS[:1]]
MIXED = [FIXTURE_RUNS / RED_RUNS[0], FIXTURE_RUNS / RED_RUNS[1], FIXTURE_RUNS / GREEN_RUNS[0]]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in (
        "BOOTSTRAP_ADMITTED",
        "EVAL_ADMISSION_MODE",
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


@pytest.fixture
def record_mode(monkeypatch):
    monkeypatch.setenv("EVAL_ADMISSION_MODE", "record")


@pytest.fixture(params=[None, "roster"], ids=["unset", "roster"])
def roster_mode(request, monkeypatch):
    """Roster mode both ways it arises: the variable unset, and set by name.

    The unset leg is what proves the default; a test that only ever set the
    variable would pass with any default at all.
    """
    if request.param is not None:
        monkeypatch.setenv("EVAL_ADMISSION_MODE", request.param)


@pytest.fixture(params=["roster", "record"])
def either_mode(request, monkeypatch):
    """For the states both modes resolve the same way."""
    monkeypatch.setenv("EVAL_ADMISSION_MODE", request.param)
    return request.param


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


def rows(md: str) -> dict[str, str]:
    return {ln.split("|")[1].strip(" `"): ln for ln in md.splitlines() if ln.startswith("| `")}


def header(md: str) -> str:
    return next(ln for ln in md.splitlines() if ln.startswith("| Case"))


# --------------------------------------------------------------------------
# Roster mode, the default: the list decides, the record recommends.
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("roster_mode")
def test_roster_mode_keeps_a_listed_case_the_record_would_demote(tmp_path):
    """The case the decision on #1493 is about. Four green nights then three
    bad ones is a case that has stopped working on main; the record says so,
    and in roster mode saying so is all it does. The list still names the
    case, so three failures still collapse it, and the verdict carries the
    recommendation a demotion pull request would cite."""
    doc = grade("record-demotes", REDS, tmp_path)
    assert doc["admitted"] is True
    assert doc["admission_source"] == "bootstrap"
    assert doc["admission_mode"] == "roster"
    assert doc["record_verdict"] == WOULD_DEMOTE
    assert doc["admission_reason"] == f"{BRIDGE_SENTENCE}; {WOULD_DEMOTE} {ROSTER_DECIDES}"
    assert doc["rung_name"] == "COLLAPSE" and doc["blocking"] is True
    # Its pooled judged mean at the key is still rung 6's comparator: four
    # lines at 0.9 and three at 0.3, weighted by n, is 9/14.
    assert doc["baseline_judged"] == {"OutcomeValidity": pytest.approx(9 / 14)}


@pytest.mark.usefixtures("roster_mode")
def test_roster_mode_does_not_admit_an_unlisted_case_the_record_would_admit(tmp_path):
    """21/21 and not on the list: the record would admit it, and in roster
    mode that is a recommendation for a roster pull request, not an
    admission. Three failures do not collapse it."""
    doc = grade("record-admits", REDS, tmp_path)
    assert doc["admitted"] is False
    assert doc["admission_source"] == "neither"
    assert doc["record_verdict"] == WOULD_ADMIT
    assert doc["admission_reason"] == (
        f"not named in BOOTSTRAP_ADMITTED; {WOULD_ADMIT} {ROSTER_DECIDES}"
    )
    assert doc["rung_name"] == "GREEN" and doc["blocking"] is False
    assert "not admitted, so it cannot collapse" in doc["reason"]


@pytest.mark.usefixtures("roster_mode")
def test_roster_mode_verdict_names_the_decider_and_what_the_record_says(tmp_path):
    """One column, two halves, so the row a roster edit is about carries the
    verdict it cites."""
    docs = [
        grade("record-admits", GREENS, tmp_path),
        grade("record-demotes", REDS, tmp_path),
        grade("record-stale", MIXED, tmp_path),
        grade("record-collecting", GREENS, tmp_path),
        grade("no-record", GREENS, tmp_path),
    ]
    rc, md = suite(tmp_path, *docs)
    assert rc == 1, md  # record-demotes collapsed: the roster kept it blocking
    assert "| Admitted by · record says |" in header(md)
    by_case = rows(md)
    assert f"| none · {WOULD_ADMIT} |" in by_case["record-admits"]
    assert f"| bootstrap · {WOULD_DEMOTE} |" in by_case["record-demotes"]
    assert f"| bootstrap · record: stale (key {KEY_LABEL}) |" in by_case["record-stale"]
    assert "| bootstrap · record: collecting 9/20 |" in by_case["record-collecting"]
    assert "| bootstrap · record: none |" in by_case["no-record"]


@pytest.mark.usefixtures("roster_mode")
def test_roster_mode_pools_the_aggregate_over_the_roster_not_the_record(tmp_path):
    """The aggregate covers admitted cases, and in roster mode admitted means
    listed: record-admits (unlisted) is out of both sides, record-demotes
    (listed) is in both -- its own 0/3 on the pull request's side, its 12/21
    on main's. Advisory either way, as on main."""
    docs = [
        grade("record-admits", GREENS, tmp_path),
        grade("record-demotes", REDS, tmp_path),
    ]
    rc, md = suite(tmp_path, *docs, extra=["--min-scored", "3"])
    assert rc == 1  # the collapse, not the aggregate
    assert "Admitted-case pass rate: 0.0% (main: 57.1%, margin 5.0%)" in md
    assert "not armed" in md


@pytest.mark.usefixtures("roster_mode")
def test_roster_mode_shows_the_column_when_the_record_spoke_with_no_store_configured(
    tmp_path, monkeypatch
):
    """Evidence landed by hand into the checked-in directory: no
    ``--baseline-store``, no ``EVAL_BASELINE_STORE``. The record cannot
    decide in roster mode, but a verdict it produced from evidence must not
    be invisible -- the twin of the record-decided trigger in record mode."""
    monkeypatch.delenv("EVAL_BASELINE_STORE", raising=False)
    out = tmp_path / "case.json"
    argv = ["case", "--task", str(ADMISSION / "tasks" / "record-admits" / "task.yaml")]
    argv += ["--baseline-dir", str(ADMISSION)]
    for run in GREENS:
        argv += ["--result", str(run)]
    assert main([*argv, "--json-out", str(out)]) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["admission_source"] == "neither" and doc["record_verdict"] == WOULD_ADMIT

    md = tmp_path / "verdict.md"
    rc = main([
        "suite", "--baseline-dir", str(ADMISSION),
        "--case-result", str(out), "--markdown-out", str(md),
    ])
    assert rc == 0
    text = md.read_text(encoding="utf-8")
    assert "| Admitted by · record says |" in header(text)
    assert f"| none · {WOULD_ADMIT} |" in rows(text)["record-admits"]


def test_the_suite_renders_in_the_mode_the_cases_were_graded_under(tmp_path, monkeypatch, capsys):
    """Each hand-off records its mode. A re-render under another environment
    -- downloaded artefacts, a local what-if -- reads as graded, with a banner;
    two modes across one run is unaccounted state and refuses."""
    monkeypatch.setenv("EVAL_ADMISSION_MODE", "record")
    graded_under_record = grade("record-demotes", REDS, tmp_path)
    monkeypatch.delenv("EVAL_ADMISSION_MODE")
    rc, md = suite(tmp_path, graded_under_record)
    assert rc == 0
    assert "| Admitted by |" in header(md) and "| record: not admitted |" in rows(md)["record-demotes"]
    assert "**WARNING — admission mode mismatch.**" in md
    assert "graded under `EVAL_ADMISSION_MODE=record`; this environment says `roster`" in md
    assert "graded under EVAL_ADMISSION_MODE=record but this environment says roster" in capsys.readouterr().err

    graded_under_roster = grade("record-admits", REDS, tmp_path)
    argv = ["suite", "--baseline-dir", str(ADMISSION), "--baseline-store", str(ADMISSION)]
    for doc in (graded_under_record, graded_under_roster):
        path = tmp_path / f"mixed-{doc['case']}.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        argv += ["--case-result", str(path)]
    md = tmp_path / "mixed.md"
    assert main([*argv, "--markdown-out", str(md)]) == 1
    assert "::error::case results were graded under different admission modes: record, roster" in capsys.readouterr().err
    assert not md.exists()


def test_an_unknown_mode_refuses_to_grade(tmp_path, monkeypatch, capsys):
    """`records` must not grade as `roster`: exit 2 from `case` (the loop's
    could-not-grade code, which stops the job) and 1 from `suite`."""
    monkeypatch.setenv("EVAL_ADMISSION_MODE", "records")
    argv = ["case", "--task", str(ADMISSION / "tasks" / "no-record" / "task.yaml")]
    argv += ["--baseline-dir", str(ADMISSION), "--result", str(REDS[0])]
    assert main([*argv, "--json-out", str(tmp_path / "case.json")]) == 2
    assert "EVAL_ADMISSION_MODE='records' is not one of record, roster" in capsys.readouterr().err

    monkeypatch.setenv("EVAL_ADMISSION_MODE", "roster")
    doc = grade("no-record", REDS, tmp_path)
    path = tmp_path / "suite-no-record.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setenv("EVAL_ADMISSION_MODE", "records")
    md = tmp_path / "verdict.md"
    argv = ["suite", "--baseline-dir", str(ADMISSION), "--baseline-store", str(ADMISSION)]
    assert main([*argv, "--case-result", str(path), "--markdown-out", str(md)]) == 1
    assert "::error::EVAL_ADMISSION_MODE='records'" in capsys.readouterr().err
    assert not md.exists()  # refused before it wrote a verdict


# --------------------------------------------------------------------------
# Record mode: #1447's behaviour, unchanged.
# --------------------------------------------------------------------------


@pytest.mark.usefixtures("record_mode")
def test_the_record_admits_a_case_the_list_never_named(tmp_path):
    doc = grade("record-admits", REDS, tmp_path)
    assert doc["admitted"] is True
    assert doc["admission_source"] == "record"
    assert doc["admission_mode"] == "record"
    assert doc["record_verdict"] == WOULD_ADMIT
    assert "admitted on 21/21 screening runs across 7 recorded run(s)" in doc["admission_reason"]
    assert doc["rung_name"] == "COLLAPSE" and doc["blocking"] is True
    # Rung 6 has a real comparator now: the record carries judged means.
    assert doc["baseline_judged"] == {"OutcomeValidity": pytest.approx(0.9)}


@pytest.mark.usefixtures("record_mode")
def test_the_record_demotes_a_case_the_list_still_names(tmp_path):
    """Record mode's intent: once the evidence exists, the evidence wins.

    Four green nights then three bad ones is the shape of a case that has
    stopped working on main. The list still names it, and it still cannot
    collapse -- a diff that did not break it must not be redded for it.
    """
    doc = grade("record-demotes", REDS, tmp_path)
    assert doc["admitted"] is False
    assert doc["admission_source"] == "record"
    assert doc["record_verdict"] == WOULD_DEMOTE
    assert "screened at 12/21" in doc["admission_reason"]
    assert "the record overrides BOOTSTRAP_ADMITTED" in doc["admission_reason"]
    assert doc["rung_name"] == "GREEN" and doc["blocking"] is False
    assert "not admitted, so it cannot collapse" in doc["reason"]


@pytest.mark.usefixtures("record_mode")
def test_the_verdict_names_who_admitted_each_case(tmp_path):
    docs = [
        grade("record-admits", GREENS, tmp_path),
        grade("record-demotes", REDS, tmp_path),
        grade("record-stale", MIXED, tmp_path),
        grade("record-collecting", GREENS, tmp_path),
    ]
    rc, md = suite(tmp_path, *docs)
    assert rc == 0, md
    assert "| Admitted by |" in header(md)
    by_case = rows(md)
    assert "| record |" in by_case["record-admits"]
    assert "| record: not admitted |" in by_case["record-demotes"]
    assert "| bootstrap |" in by_case["record-stale"]
    assert "| bootstrap |" in by_case["record-collecting"]


@pytest.mark.usefixtures("record_mode")
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


@pytest.mark.usefixtures("record_mode")
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


@pytest.mark.usefixtures("record_mode")
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


# --------------------------------------------------------------------------
# Short of a full window, both modes resolve the same way.
# --------------------------------------------------------------------------


def test_a_stale_record_falls_back_to_the_list(tmp_path, either_mode):
    """Evidence at a superseded key is no evidence about this software, so the
    case rides the list -- and the reason says how far it is from being
    judged on its own record."""
    doc = grade("record-stale", REDS, tmp_path)
    assert doc["admitted"] is True
    assert doc["admission_source"] == "bootstrap"
    assert doc["admission_mode"] == either_mode
    assert doc["record_verdict"] == f"record: stale (key {KEY_LABEL})"
    assert doc["admission_reason"].startswith(BRIDGE_SENTENCE)
    assert "stale: 7 baseline record(s) exist" in doc["admission_reason"]
    assert doc["rung_name"] == "COLLAPSE" and doc["blocking"] is True
    # Nothing at this key, so rung 6 stays quiet even though the case is admitted.
    assert doc["baseline_judged"] is None


@pytest.mark.usefixtures("either_mode")
def test_a_collecting_record_falls_back_to_the_list(tmp_path):
    doc = grade("record-collecting", REDS, tmp_path)
    assert doc["admitted"] is True
    assert doc["admission_source"] == "bootstrap"
    assert doc["record_verdict"] == "record: collecting 9/20"
    assert doc["admission_reason"].startswith(BRIDGE_SENTENCE)
    assert "collecting: 9/9 runs recorded" in doc["admission_reason"]
    assert "11 more needed" in doc["admission_reason"]
    assert doc["rung_name"] == "COLLAPSE"


@pytest.mark.usefixtures("either_mode")
def test_no_record_means_the_list_alone_decides(tmp_path, monkeypatch):
    on = grade("no-record", REDS, tmp_path)
    assert on["admitted"] is True and on["admission_source"] == "bootstrap"
    # The sentence the list has always produced, with nothing appended: the
    # store holds nothing for this case, so there is no state to report.
    assert on["admission_reason"] == BRIDGE_SENTENCE
    assert on["record_verdict"] == "record: none"

    monkeypatch.delenv("BOOTSTRAP_ADMITTED")
    off = grade("no-record", REDS, tmp_path)
    assert off["admitted"] is False and off["admission_source"] == "neither"
    assert off["admission_reason"] == "no screening evidence for this case yet"
    assert off["record_verdict"] == "record: none"
    assert off["blocking"] is False
