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

"""Who admits a case once a store is configured, in each admission mode.

``fixtures/admission/`` is a local-backend store plus five synthetic task
files, one per state the store can be in. Every store line is at the
version key the captured ``agent-kanban-smoke`` records carry, so grading
those records against a fixture task reads the fixture store for admission
and nothing else. The lines are hand-written and say so in their task files;
they are the shapes seven ordinary nightlies at three repetitions produce,
not captures.

| case                | store                                     | on the list | record says  | record mode | roster mode |
| ------------------- | ----------------------------------------- | ----------- | ------------ | ----------- | ----------- |
| `record-admits`     | 21/21 at the current key                  | no          | would-admit  | record      | none        |
| `record-demotes`    | 12/21 at the current key (4 good, 3 bad)  | yes         | would-demote | record      | bootstrap   |
| `record-stale`      | 21/21, all at a superseded judge model    | yes         | stale        | bootstrap   | bootstrap   |
| `record-collecting` | 9/9 at the current key                    | yes         | collecting   | bootstrap   | bootstrap   |
| `no-record`         | nothing                                   | either      | none         | list or none| list or none|

Two rules, one per ``EVAL_ADMISSION_MODE``. ``roster`` (the default): the
list decides outright and the record's verdict is reported beside it.
``record``: the record governs once it holds a full window at the current
key, either way; the list is the fallback for a case the record cannot judge
yet. The first block of tests pins record mode; the second pins roster mode
and that it is the default. Everything runs through the CLI, the way
``hack/ci-eval-pr.sh`` drives it, with ``--baseline-store`` naming the
fixture directory so the verdict renders its evidence columns.
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
        "EVAL_ADMISSION_MODE",
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
    """The #1447 behaviour, selected explicitly; roster is the default."""
    monkeypatch.setenv("EVAL_ADMISSION_MODE", "record")


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


def test_the_record_admits_a_case_the_list_never_named(record_mode, tmp_path):
    doc = grade("record-admits", REDS, tmp_path)
    assert doc["admitted"] is True
    assert doc["admission_source"] == "record"
    assert "admitted on 21/21 screening runs across 7 recorded run(s)" in doc["admission_reason"]
    assert doc["rung_name"] == "COLLAPSE" and doc["blocking"] is True
    # Rung 6 has a real comparator now: the record carries judged means.
    assert doc["baseline_judged"] == {"OutcomeValidity": pytest.approx(0.9)}


def test_the_record_demotes_a_case_the_list_still_names(record_mode, tmp_path):
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


def test_a_stale_record_falls_back_to_the_list(record_mode, tmp_path):
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


def test_a_collecting_record_falls_back_to_the_list(record_mode, tmp_path):
    doc = grade("record-collecting", REDS, tmp_path)
    assert doc["admitted"] is True
    assert doc["admission_source"] == "bootstrap"
    assert doc["admission_reason"].startswith(BRIDGE_SENTENCE)
    assert "collecting: 9/9 runs recorded" in doc["admission_reason"]
    assert "11 more needed" in doc["admission_reason"]
    assert doc["rung_name"] == "COLLAPSE"


def test_no_record_means_the_list_alone_decides(record_mode, tmp_path, monkeypatch):
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


def test_the_verdict_names_who_admitted_each_case(record_mode, tmp_path):
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
    assert "| Admitted by | Record says |" in header
    assert "| record | would-admit |" in rows["record-admits"]
    assert "| record: not admitted | would-demote |" in rows["record-demotes"]
    assert "| bootstrap | stale |" in rows["record-stale"]
    assert "| bootstrap | collecting |" in rows["record-collecting"]


def test_the_aggregate_is_reported_against_main_and_reds_only_when_armed(
    record_mode, tmp_path, monkeypatch
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


def test_a_demoted_case_counts_on_neither_side_of_the_aggregate(record_mode, tmp_path):
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
    record_mode, tmp_path, monkeypatch
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
# Roster mode, the default: the list decides, the record informs.
# --------------------------------------------------------------------------


def test_roster_is_the_default_mode(tmp_path, monkeypatch):
    """Unset, empty and the explicit spelling all mean roster; the hand-off
    records which mode graded it, so an artefact is self-describing."""
    for value in (None, "", "roster", " Roster "):
        if value is None:
            monkeypatch.delenv("EVAL_ADMISSION_MODE", raising=False)
        else:
            monkeypatch.setenv("EVAL_ADMISSION_MODE", value)
        doc = grade("record-admits", REDS, tmp_path)
        assert doc["admission_mode"] == "roster", value
        assert doc["admitted"] is False


def test_a_misspelled_mode_is_exit_2_not_a_silent_roster(tmp_path, monkeypatch, capsys):
    """`records` for `record` must not grade as roster and look like a switch."""
    monkeypatch.setenv("EVAL_ADMISSION_MODE", "records")
    argv = [
        "case",
        "--task", str(ADMISSION / "tasks" / "record-admits" / "task.yaml"),
        "--baseline-dir", str(ADMISSION),
        "--baseline-store", str(ADMISSION),
        "--result", str(REDS[0]),
    ]
    assert main(argv) == 2
    err = capsys.readouterr().err
    assert "EVAL_ADMISSION_MODE='records'" in err and "roster, record" in err


def test_in_roster_mode_a_full_window_admits_nothing_the_list_did_not_name(tmp_path):
    """21/21 on record and not on the list: not admitted, so three red
    repetitions cannot collapse -- and the reason says the record would
    have admitted it and why that did not happen."""
    doc = grade("record-admits", REDS, tmp_path)
    assert doc["admitted"] is False
    assert doc["admission_source"] == "neither"
    assert doc["record_verdict"] == "would-admit"
    assert doc["admission_reason"].startswith(
        "the record would admit it: 21/21 screening runs across 7 recorded run(s)"
    )
    assert "EVAL_ADMISSION_MODE=roster, so BOOTSTRAP_ADMITTED decides" in doc["admission_reason"]
    assert doc["rung_name"] == "GREEN" and doc["blocking"] is False
    # The comparator is evidence, not admission: rung 6 still has main's mean.
    assert doc["baseline_judged"] == {"OutcomeValidity": pytest.approx(0.9)}


def test_in_roster_mode_the_list_keeps_a_case_the_record_would_demote(tmp_path):
    """The decision on #1493: night seven cannot change what blocks. 12/21
    on record and named on the list: still admitted, still collapses on
    three reds, and the verdict says the record would demote it -- which is
    the sentence a demotion pull request cites."""
    doc = grade("record-demotes", REDS, tmp_path)
    assert doc["admitted"] is True
    assert doc["admission_source"] == "bootstrap"
    assert doc["record_verdict"] == "would-demote"
    assert doc["admission_reason"].startswith(BRIDGE_SENTENCE)
    assert "; the record would demote it: screened at 12/21" in doc["admission_reason"]
    assert "overrides BOOTSTRAP_ADMITTED" not in doc["admission_reason"]
    assert doc["rung_name"] == "COLLAPSE" and doc["blocking"] is True


def test_in_roster_mode_a_listed_case_would_admit_reads_the_same_as_record_mode(tmp_path, monkeypatch):
    """A listed case at 21/21 blocks in both modes; the only difference is
    who is credited, and roster mode says what the record would have done."""
    monkeypatch.setenv("BOOTSTRAP_ADMITTED", BRIDGE + ",record-admits")
    doc = grade("record-admits", REDS, tmp_path)
    assert doc["admitted"] is True and doc["admission_source"] == "bootstrap"
    assert doc["record_verdict"] == "would-admit"
    assert doc["admission_reason"] == (
        BRIDGE_SENTENCE + "; the record would admit it: 21/21 screening runs "
        "across 7 recorded run(s) (bar 95% over 20)"
    )
    assert doc["rung_name"] == "COLLAPSE" and doc["blocking"] is True


def test_in_roster_mode_the_pre_admission_states_read_as_before(tmp_path, monkeypatch):
    """Stale, collecting and nothing: the list decides in both modes, the
    sentences are the ones record mode produces, and the record column names
    the state."""
    stale = grade("record-stale", REDS, tmp_path)
    assert stale["admitted"] is True and stale["admission_source"] == "bootstrap"
    assert stale["record_verdict"] == "stale"
    assert stale["admission_reason"].startswith(BRIDGE_SENTENCE + "; the record cannot judge it yet: stale:")

    collecting = grade("record-collecting", REDS, tmp_path)
    assert collecting["admitted"] is True and collecting["record_verdict"] == "collecting"
    assert "; the record cannot judge it yet: collecting: 9/9" in collecting["admission_reason"]

    on = grade("no-record", REDS, tmp_path)
    assert on["admitted"] is True and on["record_verdict"] == "none"
    assert on["admission_reason"] == BRIDGE_SENTENCE

    monkeypatch.delenv("BOOTSTRAP_ADMITTED")
    off = grade("no-record", REDS, tmp_path)
    assert off["admitted"] is False and off["admission_source"] == "neither"
    assert off["record_verdict"] == "none"
    assert off["admission_reason"] == "no screening evidence for this case yet"

    demoted = grade("record-demotes", REDS, tmp_path)
    assert demoted["admitted"] is False and demoted["record_verdict"] == "would-demote"
    assert demoted["admission_reason"].startswith("screened at 12/21")
    assert "overrides" not in demoted["admission_reason"]


def test_in_roster_mode_the_verdict_shows_the_list_and_what_the_record_says(tmp_path):
    docs = [
        grade("record-admits", GREENS, tmp_path),
        grade("record-demotes", REDS, tmp_path),
        grade("record-stale", MIXED, tmp_path),
        grade("record-collecting", GREENS, tmp_path),
        grade("no-record", GREENS, tmp_path),
    ]
    rc, md = suite(tmp_path, *docs)
    assert rc == 1, md  # record-demotes is listed, so its three reds collapse
    rows = {ln.split("|")[1].strip(" `"): ln for ln in md.splitlines() if ln.startswith("| `")}
    header = next(ln for ln in md.splitlines() if ln.startswith("| Case"))
    assert "| Admitted by | Record says |" in header
    assert "| none | would-admit |" in rows["record-admits"]
    assert "| bootstrap | would-demote |" in rows["record-demotes"]
    assert "| bootstrap | stale |" in rows["record-stale"]
    assert "| bootstrap | collecting |" in rows["record-collecting"]
    assert "| bootstrap | none |" in rows["no-record"]
    assert "- record-demotes: rung 4 (COLLAPSE)" in md


def test_in_roster_mode_a_listed_case_the_record_would_demote_counts_on_both_sides(tmp_path):
    """It is admitted, so its runs join the pull request's rate and its 12/21
    joins main's: the aggregate compares the blocking set against the same
    set's record, which is what the list decided to block on."""
    docs = [
        grade("record-demotes", GREENS, tmp_path),
        grade("record-collecting", GREENS, tmp_path),
    ]
    rc, md = suite(tmp_path, *docs, extra=["--min-scored", "3"])
    assert rc == 0, md
    # main: (12 + 9) / (21 + 9) = 70.0%
    assert "Admitted-case pass rate: 100.0% (main: 70.0%, margin 5.0%)" in md


def test_the_columns_appear_in_roster_mode_when_the_record_holds_a_full_window_and_no_store_is_configured(
    tmp_path, monkeypatch
):
    """Evidence landed by hand into the checked-in directory, no store
    configured: the record decided nothing in roster mode, but it would
    have, and that must not be invisible in the verdict."""
    monkeypatch.delenv("EVAL_BASELINE_STORE", raising=False)
    out = tmp_path / "case.json"
    argv = ["case", "--task", str(ADMISSION / "tasks" / "record-admits" / "task.yaml")]
    argv += ["--baseline-dir", str(ADMISSION)]
    for run in GREENS:
        argv += ["--result", str(run)]
    assert main([*argv, "--json-out", str(out)]) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["admission_source"] == "neither" and doc["record_verdict"] == "would-admit"

    md = tmp_path / "verdict.md"
    rc = main([
        "suite", "--baseline-dir", str(ADMISSION),
        "--case-result", str(out), "--markdown-out", str(md),
    ])
    assert rc == 0
    text = md.read_text(encoding="utf-8")
    assert "| Admitted by | Record says |" in text
    assert "| `record-admits` |" in text and "| none | would-admit |" in text
