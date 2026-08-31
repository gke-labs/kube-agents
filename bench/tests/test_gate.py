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

"""Tests for the ``bench-gate`` CLI: the contract with ``hack/ci-eval-pr.sh``.

The shell can only see three things -- the exit code, the printed lines, and
the JSON hand-off -- so those are what this module pins. In particular:

1. **`case` exits 0 even on a blocking verdict.** The loop has to keep going
   so the summary covers every task; the blocking flag rides in the JSON and
   `suite` is what turns it into an exit code. A `case` that exited non-zero
   would abort the loop under `set -e` and silently drop the remaining tasks.
   It exits 2 only when it could not grade at all.
2. **`suite` exits 1 on red, 0 on green**, and reds on a case result the loop
   never wrote -- unaccounted work is not a pass.
3. **The `Task <id> Result: [...]` line keeps its shape**, because people and
   scripts grep build logs for it.
4. **The environment carries every threshold**, since all of them are meant
   to be tuned against observed movement on main.
5. **`record` is the only writer, and only on main.** It refuses with
   `PULL_NUMBER` set, so a pull request cannot move the baseline it is judged
   against even if the shell guard is edited away. It also never reds: a merge
   to main must not fail over bookkeeping.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kube_agents_bench import evidence_store
from kube_agents_bench.gate import main
from kube_agents_bench.scoring import MISSING

from conftest import FIXTURE_RUNS, GREEN_RUNS, RED_RUNS, read_fixture, write_run
from kube_agents_bench.evidence_store import _key_segments
from test_evidence_store import FakeGcloud

BASELINES = Path(__file__).resolve().parents[1] / "baselines"
JUDGE = "gemini-3.1-pro-preview"

KEY = {
    "setup_id": "gemini-3-1-pro-preview-kubeagents-mcp",
    "scoring_version": "v1",
    "judge_model": JUDGE,
    "fleet": 1,
    "verifiers": 1,
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """The gate reads the environment; CI's must not leak into a test."""
    for name in (
        "BOOTSTRAP_ADMITTED",
        "JUDGE_MODEL",
        "DETERMINISTIC_CORRECTNESS_FLOOR",
        "EVAL_AGGREGATE_MARGIN",
        "EVAL_ADMISSION_RATE",
        "EVAL_ADMISSION_MIN_RUNS",
        "EVAL_JUDGED_MARGIN",
        "EVAL_JUDGED_METRICS",
        "PULL_NUMBER",
        "PULL_BASE_SHA",
        "GIT_COMMIT",
        "EVAL_BASELINE_STORE",
        "EVAL_BASELINE_MAX_OBJECTS",
        "BUILD_ID",
        "PROW_JOB_ID",
    ):
        monkeypatch.delenv(name, raising=False)


def run_case(kanban_task, runs, out: Path, *extra) -> int:
    argv = ["case", "--task", str(kanban_task), "--baseline-dir", str(BASELINES)]
    for r in runs:
        argv += ["--result", str(r)]
    argv += ["--json-out", str(out), *extra]
    return main(argv)


def payload(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# `bench-gate case`
# --------------------------------------------------------------------------


def test_a_green_case_prints_passed_and_writes_the_hand_off(kanban_task, tmp_path, capsys):
    out = tmp_path / "case.json"
    assert run_case(kanban_task, [FIXTURE_RUNS / n for n in GREEN_RUNS], out) == 0
    printed = capsys.readouterr().out
    assert "Task agent-kanban-smoke Result: [PASSED]" in printed
    doc = payload(out)
    assert doc["blocking"] is False
    assert doc["passes"] == 2 and doc["scored"] == 2


def test_a_blocking_case_still_exits_zero(kanban_task, tmp_path, monkeypatch, capsys):
    """The loop must survive a red task and go on to grade the next one."""
    monkeypatch.setenv("BOOTSTRAP_ADMITTED", "agent-kanban-smoke")
    out = tmp_path / "case.json"
    assert run_case(kanban_task, [FIXTURE_RUNS / n for n in RED_RUNS], out) == 0
    assert "Task agent-kanban-smoke Result: [FAILED]" in capsys.readouterr().out
    assert payload(out)["blocking"] is True


def test_an_infra_case_gets_its_own_label(write_task, tmp_path, capsys):
    task = write_task(
        "planted-pdb",
        {"id": "planted-pdb", "name": "x", "infrastructure": {"deployer": "tofu"}},
    )
    out = tmp_path / "case.json"
    assert main(
        [
            "case", "--task", str(task), "--baseline-dir", str(BASELINES),
            "--result", MISSING, "--json-out", str(out),
        ]
    ) == 0
    assert "[RESOURCE_PREPARATION_FAILED]" in capsys.readouterr().out
    assert payload(out)["blocking"] is False


def test_a_case_that_failed_everything_without_collapsing_is_not_passed(
    kanban_task, tmp_path, capsys
):
    """The state the rate rules create, and the one a two-label scheme lies about.

    Three repetitions failed; the case is unadmitted, so it does not red the
    merge. Printing `[PASSED]` on a run where nothing passed is how a gate
    earns the reputation that gets it switched off.
    """
    out = tmp_path / "case.json"
    assert run_case(kanban_task, [FIXTURE_RUNS / n for n in RED_RUNS], out) == 0
    assert "Task agent-kanban-smoke Result: [UNSTABLE]" in capsys.readouterr().out
    doc = payload(out)
    assert doc["label"] == "UNSTABLE"
    assert doc["blocking"] is False
    assert doc["passes"] == 0


def test_a_partially_passing_case_is_unstable(kanban_task, tmp_path):
    out = tmp_path / "case.json"
    run_case(kanban_task, [FIXTURE_RUNS / GREEN_RUNS[0], FIXTURE_RUNS / RED_RUNS[0]], out)
    assert payload(out)["label"] == "UNSTABLE"


def test_an_expected_fail_case_failing_is_labelled_as_such(write_task, tmp_path, capsys):
    """Failing is the declared intent: neither PASSED nor UNSTABLE fits."""
    task = write_task(
        "edd-case",
        {
            "id": "edd-case",
            "name": "x",
            "expected_fail": True,
            "verification_spec": [{"report_contains": {"phrases": ["x"]}}],
        },
    )
    out = tmp_path / "case.json"
    main(
        ["case", "--task", str(task), "--baseline-dir", str(BASELINES),
         "--result", str(FIXTURE_RUNS / RED_RUNS[0]), "--json-out", str(out)]
    )
    assert "[EXPECTED_FAIL]" in capsys.readouterr().out
    assert payload(out)["label"] == "EXPECTED_FAIL"


def test_the_label_reaches_the_markdown_table(kanban_task, tmp_path):
    case_out = tmp_path / "case.json"
    run_case(kanban_task, [FIXTURE_RUNS / n for n in RED_RUNS], case_out)
    md = tmp_path / "verdict.md"
    main(["suite", "--case-result", str(case_out), "--markdown-out", str(md)])
    assert "UNSTABLE" in md.read_text(encoding="utf-8")


def test_an_unreadable_task_file_exits_two(tmp_path):
    """Distinct from a red verdict: nothing was graded, so nothing is known."""
    assert main(["case", "--task", str(tmp_path / "gone" / "task.yaml")]) == 2


def test_a_broken_versions_file_exits_two(kanban_task, tmp_path):
    """Better to stop than to score every case against an assumed version 1."""
    (tmp_path / "VERSIONS.json").write_text("{}", encoding="utf-8")
    assert main(
        ["case", "--task", str(kanban_task), "--baseline-dir", str(tmp_path),
         "--result", str(FIXTURE_RUNS / GREEN_RUNS[0])]
    ) == 2


def test_the_per_repetition_detail_is_printed(kanban_task, tmp_path, capsys):
    out = tmp_path / "case.json"
    run_case(kanban_task, [FIXTURE_RUNS / n for n in RED_RUNS], out)
    printed = capsys.readouterr().out
    assert "rep 1: fail" in printed and "rep 3: fail" in printed
    # The judged scores are reported, in brackets, and did not gate: the three
    # identical runs disagree by 0.8 while the verdict is the same on all three.
    assert "OutcomeValidity=0.2" in printed and "OutcomeValidity=0.9" in printed
    assert "admission:" in printed


def test_missing_is_accepted_as_a_repetition_placeholder(kanban_task, tmp_path):
    """devops-bench can die before writing a run directory at all.

    The shell has no other way to say "this repetition produced nothing", and
    positional alignment of the remaining repetitions has to survive it.
    """
    out = tmp_path / "case.json"
    assert run_case(
        kanban_task, [FIXTURE_RUNS / GREEN_RUNS[0], MISSING, FIXTURE_RUNS / GREEN_RUNS[1]], out
    ) == 0
    doc = payload(out)
    assert [r["outcome"] for r in doc["reps"]] == ["pass", "blocked", "pass"]


def test_the_version_key_rides_in_the_hand_off(kanban_task, tmp_path, monkeypatch):
    monkeypatch.setenv("JUDGE_MODEL", JUDGE)
    out = tmp_path / "case.json"
    run_case(kanban_task, [FIXTURE_RUNS / GREEN_RUNS[0]], out)
    assert payload(out)["version_key"] == KEY


def test_no_judge_model_means_no_key(kanban_task, tmp_path):
    """`JUDGE_MODEL` unset is not a key of four components; it is no key.

    Comparing against a baseline without knowing which judge produced this
    run is the drift the key exists to catch.
    """
    out = tmp_path / "case.json"
    run_case(kanban_task, [FIXTURE_RUNS / GREEN_RUNS[0]], out)
    doc = payload(out)
    assert doc["version_key"] is None
    assert "no version key" in doc["admission_reason"]


def test_the_key_survives_a_lead_off_infra_repetition(kanban_task, tmp_path, monkeypatch):
    """All repetitions of one case run on the same software.

    Taking the key off the first READABLE record, rather than the first,
    means one dead repetition does not cost the case its baseline match.
    """
    monkeypatch.setenv("JUDGE_MODEL", JUDGE)
    out = tmp_path / "case.json"
    run_case(kanban_task, [MISSING, FIXTURE_RUNS / GREEN_RUNS[0]], out)
    assert payload(out)["version_key"] == KEY


def test_an_empty_record_does_not_supply_the_key(kanban_task, tmp_path, monkeypatch):
    """The empty-list record has a manifest but evaluated nothing.

    Its `setupId` is written before the run, so it survives a resource
    preparation failure and would key the case against a baseline for work
    that never happened. The manifest below is deliberately branded so a
    regression here is visible rather than coincidentally right.
    """
    monkeypatch.setenv("JUDGE_MODEL", JUDGE)
    doc = read_fixture(GREEN_RUNS[0])
    doc["results"] = []
    doc["manifest"]["setupId"] = "died-before-the-agent-ran"
    empty = write_run(tmp_path / "empty", doc)
    out = tmp_path / "case.json"
    run_case(kanban_task, [empty, FIXTURE_RUNS / GREEN_RUNS[0]], out)
    assert payload(out)["version_key"] == KEY


def test_bootstrap_admission_reaches_the_verdict(kanban_task, tmp_path, monkeypatch):
    """Named in the environment, the case collapses; unnamed, it does not."""
    out = tmp_path / "case.json"
    reds = [FIXTURE_RUNS / n for n in RED_RUNS]

    run_case(kanban_task, reds, out)
    assert payload(out)["blocking"] is False

    monkeypatch.setenv("BOOTSTRAP_ADMITTED", "gpu-stress-test-diagnosis,agent-kanban-smoke")
    run_case(kanban_task, reds, out)
    doc = payload(out)
    assert doc["blocking"] is True
    assert doc["rung_name"] == "COLLAPSE"


def test_the_correctness_floor_comes_from_the_environment(kanban_task, tmp_path, monkeypatch):
    monkeypatch.setenv("DETERMINISTIC_CORRECTNESS_FLOOR", "0.5")
    out = tmp_path / "case.json"
    run_case(kanban_task, [FIXTURE_RUNS / RED_RUNS[0]], out)
    assert payload(out)["passes"] == 1


def test_the_deployer_flag_overrides_the_task_file(write_task, tmp_path, capsys):
    """The shell echoes a deployer too; a local variant run may differ."""
    task = write_task(
        "planted-pdb",
        {"id": "planted-pdb", "name": "x", "infrastructure": {"deployer": "tofu"}},
    )
    out = tmp_path / "case.json"
    main(
        ["case", "--task", str(task), "--baseline-dir", str(BASELINES),
         "--deployer", "noop", "--result", MISSING, "--json-out", str(out)]
    )
    # A noop task has no infrastructure to blame, so the same missing record
    # is now a block rather than resource preparation.
    assert payload(out)["blocking"] is True
    assert "provisions nothing" in capsys.readouterr().out


# --------------------------------------------------------------------------
# `bench-gate suite`
# --------------------------------------------------------------------------


def case_file(tmp_path: Path, name: str, **fields) -> Path:
    doc = {
        "case": name, "name": name, "domain": "obtainability",
        "rung": 7, "rung_name": "GREEN", "blocking": False, "reason": "passed",
        "admitted": True, "expected_fail": False,
        "passes": 3, "scored": 3, "pass_rate": 1.0, "reps": [],
    }
    doc.update(fields)
    path = tmp_path / f"case-{name}.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_a_green_suite_exits_zero(tmp_path, capsys):
    rc = main(["suite", "--case-result", str(case_file(tmp_path, "a"))])
    assert rc == 0
    assert "**GREEN**" in capsys.readouterr().out


def test_a_red_suite_exits_one(tmp_path, capsys):
    path = case_file(
        tmp_path, "a", blocking=True, rung=4, rung_name="COLLAPSE", reason="failed 3/3"
    )
    assert main(["suite", "--case-result", str(path)]) == 1
    printed = capsys.readouterr().out
    assert "**RED**" in printed and "### Why it is red" in printed


def test_a_missing_case_result_reds_the_suite(tmp_path, capsys):
    """The loop died partway. Unaccounted work is louder than a blank row."""
    assert main(["suite", "--case-result", str(tmp_path / "never-written.json")]) == 1
    assert "missing case result" in capsys.readouterr().err


def test_an_unreadable_case_result_reds_the_suite(tmp_path, capsys):
    path = tmp_path / "case-a.json"
    path.write_text("{ truncated", encoding="utf-8")
    assert main(["suite", "--case-result", str(path)]) == 1
    assert "unreadable case result" in capsys.readouterr().err


def test_the_markdown_escapes_a_pipe_in_a_reason(tmp_path):
    """A verifier reason can hold a kubectl selector or a phrase list.

    An unescaped pipe silently splits the table cell and shifts every column
    after it, which reads as a different case having failed.
    """
    path = case_file(tmp_path, "a", blocking=True, reason="required: a|b|c")
    md = tmp_path / "verdict.md"
    main(["suite", "--case-result", str(path), "--markdown-out", str(md)])
    row = [ln for ln in md.read_text(encoding="utf-8").splitlines() if ln.startswith("| `a`")][0]
    assert r"a\|b\|c" in row
    assert row.count("|") - row.count(r"\|") == 6


def test_the_suite_json_records_the_aggregate(tmp_path):
    out = tmp_path / "suite.json"
    main(
        ["suite", "--case-result", str(case_file(tmp_path, "a", passes=1, scored=4)),
         "--baseline-rate", "0.9", "--min-scored", "1", "--json-out", str(out)]
    )
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["green"] is False
    assert doc["pass_rate"] == 0.25 and doc["baseline_rate"] == 0.9


def test_the_aggregate_margin_comes_from_the_environment(tmp_path, monkeypatch):
    """0.25 against a 0.9 baseline: red at the default margin, green at 0.9."""
    path = case_file(tmp_path, "a", passes=1, scored=4)
    args = ["suite", "--case-result", str(path), "--baseline-rate", "0.9", "--min-scored", "1"]
    assert main(args) == 1
    monkeypatch.setenv("EVAL_AGGREGATE_MARGIN", "0.9")
    assert main(args) == 0


def test_the_verdict_is_advisory_with_no_baseline(tmp_path, capsys):
    """The state this ships in, and it must say so rather than imply a pass."""
    assert main(["suite", "--case-result", str(case_file(tmp_path, "a", passes=0, scored=3))]) == 0
    assert "advisory" in capsys.readouterr().out


def test_case_and_suite_compose_over_the_real_fixtures(kanban_task, tmp_path, monkeypatch):
    """End to end, exactly as the shell will call it.

    Three captured red repetitions of an admitted case red the job; the two
    captured green ones do not.
    """
    monkeypatch.setenv("BOOTSTRAP_ADMITTED", "agent-kanban-smoke")
    monkeypatch.setenv("JUDGE_MODEL", JUDGE)

    red = tmp_path / "case-red.json"
    run_case(kanban_task, [FIXTURE_RUNS / n for n in RED_RUNS], red)
    assert main(["suite", "--case-result", str(red)]) == 1

    green = tmp_path / "case-green.json"
    run_case(kanban_task, [FIXTURE_RUNS / n for n in GREEN_RUNS], green)
    assert main(["suite", "--case-result", str(green)]) == 0


# --------------------------------------------------------------------------
# `bench-gate record` -- the producer. Everything the gate compares against
# comes from lines this wrote, so without it the store is empty forever.
# --------------------------------------------------------------------------


def store_with(tmp_path: Path, *lines: dict) -> Path:
    """A baseline store directory, seeded with whole JSONL lines."""
    root = tmp_path / "baselines"
    root.mkdir(parents=True, exist_ok=True)
    (root / "VERSIONS.json").write_text(
        json.dumps({"fleet": 1, "verifiers": 1}), encoding="utf-8"
    )
    for line in lines:
        path = root / f"{line['case']}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
    return root


def baseline_line(case: str, *, runs: int, passes: int, judged=None, at="2026-08-01T00:00:00Z"):
    doc = {"case": case, "recorded_at": at, "key": KEY, "runs": runs, "passes": passes}
    if judged:
        doc["judged"] = judged
    return doc


def run_record(store: Path, *case_files: Path, extra=()) -> int:
    argv = ["record", "--baseline-dir", str(store)]
    for f in case_files:
        argv += ["--case-result", str(f)]
    return main([*argv, *extra])


def graded_case(kanban_task, runs, out: Path, store: Path, *extra) -> dict:
    """Grade a real case against a real store, the way the shell does."""
    argv = ["case", "--task", str(kanban_task), "--baseline-dir", str(store)]
    for r in runs:
        argv += ["--result", str(r)]
    assert main([*argv, "--json-out", str(out), *extra]) == 0
    return payload(out)


def test_record_appends_a_line_the_store_can_read_back(
    kanban_task, tmp_path, monkeypatch
):
    monkeypatch.setenv("JUDGE_MODEL", JUDGE)
    store = store_with(tmp_path)
    out = tmp_path / "case.json"
    graded_case(kanban_task, [FIXTURE_RUNS / n for n in GREEN_RUNS], out, store)

    assert run_record(store, out) == 0
    line = json.loads((store / "agent-kanban-smoke.jsonl").read_text().strip())
    assert line["case"] == "agent-kanban-smoke"
    assert (line["runs"], line["passes"]) == (2, 2)
    assert line["key"] == KEY
    assert line["judged"]["OutcomeValidity"] == {"mean": 1.0, "n": 2}


def test_record_writes_what_a_red_run_found(kanban_task, tmp_path, monkeypatch):
    """Unconditional on the verdict, and this is the important half.

    A red run on main is exactly the evidence that de-admits a case that has
    stopped working. A store that only ever recorded good days would drift its
    bar upward until nothing could clear it and nothing could fall below it.
    """
    monkeypatch.setenv("JUDGE_MODEL", JUDGE)
    store = store_with(tmp_path)
    out = tmp_path / "case.json"
    graded_case(kanban_task, [FIXTURE_RUNS / n for n in RED_RUNS], out, store)

    assert run_record(store, out) == 0
    line = json.loads((store / "agent-kanban-smoke.jsonl").read_text().strip())
    assert (line["runs"], line["passes"]) == (3, 0)


def test_record_refuses_to_run_on_a_pull_request(tmp_path, monkeypatch, capsys):
    """The invariant, enforced where one line of shell cannot undo it: a pull
    request reads the baseline it is judged against and never moves it."""
    monkeypatch.setenv("PULL_NUMBER", "925")
    store = store_with(tmp_path)
    assert run_record(store, case_file(tmp_path, "a")) == 2
    assert "only runs on main" in capsys.readouterr().err
    assert list(store.glob("*.jsonl")) == []


def test_force_is_the_escape_hatch_for_local_screening(tmp_path, monkeypatch):
    monkeypatch.setenv("PULL_NUMBER", "925")
    store = store_with(tmp_path)
    doc = case_file(tmp_path, "a", version_key=KEY, reps=[{"outcome": "pass"}])
    assert run_record(store, doc, extra=["--force"]) == 0
    assert (store / "a.jsonl").is_file()


def test_record_skips_a_case_with_no_version_key(tmp_path, capsys):
    """No readable record in any repetition. Filing it under a partial key
    would create a bucket no real run can ever match."""
    store = store_with(tmp_path)
    assert run_record(store, case_file(tmp_path, "a", reps=[{"outcome": "pass"}])) == 0
    assert "no version key" in capsys.readouterr().out
    assert list(store.glob("*.jsonl")) == []


def test_record_skips_a_case_that_produced_no_pass_or_fail(tmp_path, capsys):
    store = store_with(tmp_path)
    doc = case_file(
        tmp_path, "a", version_key=KEY,
        reps=[{"outcome": "infra"}, {"outcome": "blocked"}],
    )
    assert run_record(store, doc) == 0
    assert "no repetition produced a pass or a fail" in capsys.readouterr().out
    assert list(store.glob("*.jsonl")) == []


def test_record_keeps_blocked_and_infra_out_of_the_rate_but_in_the_line(tmp_path):
    """Dropping them silently would make a case that half-crashes look
    perfectly reliable in its own history."""
    store = store_with(tmp_path)
    doc = case_file(
        tmp_path, "a", version_key=KEY,
        reps=[{"outcome": "pass"}, {"outcome": "blocked"}, {"outcome": "infra"}],
    )
    assert run_record(store, doc) == 0
    line = json.loads((store / "a.jsonl").read_text().strip())
    assert (line["runs"], line["passes"]) == (1, 1)
    assert (line["blocked"], line["infra"]) == (1, 1)


def test_record_stamps_the_commit_from_prow(tmp_path, monkeypatch):
    monkeypatch.setenv("PULL_BASE_SHA", "deadbee")
    store = store_with(tmp_path)
    doc = case_file(tmp_path, "a", version_key=KEY, reps=[{"outcome": "pass"}])
    assert run_record(store, doc) == 0
    assert json.loads((store / "a.jsonl").read_text().strip())["commit"] == "deadbee"


def test_record_copies_its_lines_somewhere_prow_can_collect_them(tmp_path):
    """The store lives in git and the job cannot push, so the appended file
    dies with the workspace. The artefact is how the evidence survives."""
    store = store_with(tmp_path)
    doc = case_file(tmp_path, "a", version_key=KEY, reps=[{"outcome": "pass"}])
    lines_out = tmp_path / "artifacts" / "baseline-append.jsonl"
    assert run_record(store, doc, extra=["--lines-out", str(lines_out)]) == 0
    assert json.loads(lines_out.read_text().strip())["case"] == "a"


def test_record_says_so_when_a_run_produced_nothing_worth_keeping(tmp_path, capsys):
    store = store_with(tmp_path)
    assert run_record(store) == 0
    assert "produced no evidence" in capsys.readouterr().out


# --------------------------------------------------------------------------
# The loop closing: collect, then compare.
# --------------------------------------------------------------------------


def test_an_empty_store_collects_and_then_admits(kanban_task, tmp_path, monkeypatch):
    """The requirement, end to end through the CLI.

    Nothing here is a deliberate screening campaign -- it is seven ordinary
    recorder runs at the default three repetitions. If pooling were removed
    this test hangs at "collecting" forever, which is the state the store
    would really have shipped in.
    """
    monkeypatch.setenv("JUDGE_MODEL", JUDGE)
    store = store_with(tmp_path)
    out = tmp_path / "case.json"
    greens = [FIXTURE_RUNS / n for n in (GREEN_RUNS + GREEN_RUNS[:1])]

    doc = graded_case(kanban_task, greens, out, store)
    assert doc["admitted"] is False
    assert "no screening evidence" in doc["admission_reason"]

    for i in range(6):
        assert run_record(store, out, extra=["--recorded-at", f"2026-08-0{i + 1}T00:00:00Z"]) == 0
        doc = graded_case(kanban_task, greens, out, store)
        assert doc["admitted"] is False, f"admitted after only {i + 1} run(s)"
        assert "collecting" in doc["admission_reason"]

    assert run_record(store, out, extra=["--recorded-at", "2026-08-07T00:00:00Z"]) == 0
    doc = graded_case(kanban_task, greens, out, store)
    assert doc["admitted"] is True
    assert "21/21" in doc["admission_reason"]


def test_the_suite_aggregate_comes_from_the_store_not_a_flag(
    kanban_task, tmp_path, monkeypatch, capsys
):
    """The rule was built and never armed: `--baseline-rate` was a flag the
    shell did not pass, so main's side of the comparison was always None."""
    monkeypatch.setenv("JUDGE_MODEL", JUDGE)
    store = store_with(
        tmp_path, *[
            baseline_line("agent-kanban-smoke", runs=3, passes=3, at=f"2026-08-0{i + 1}T00:00:00Z")
            for i in range(7)
        ]
    )
    out = tmp_path / "case.json"
    graded_case(kanban_task, [FIXTURE_RUNS / n for n in GREEN_RUNS], out, store)

    assert main([
        "suite", "--case-result", str(out), "--baseline-dir", str(store),
        "--min-scored", "1",
    ]) == 0
    printed = capsys.readouterr().out
    assert "main: 100.0%" in printed
    assert "advisory" not in printed


def test_the_aggregate_reds_when_the_store_says_main_did_better(tmp_path):
    store = store_with(
        tmp_path, baseline_line("a", runs=20, passes=20)
    )
    doc = case_file(tmp_path, "a", version_key=KEY, passes=1, scored=4)
    assert main([
        "suite", "--case-result", str(doc), "--baseline-dir", str(store),
        "--min-scored", "1",
    ]) == 1


def test_the_shipped_sample_floor_keeps_a_small_run_from_redding(tmp_path, capsys):
    """The same input as above, at the default floor: reported, not blocking.

    Four scored repetitions cannot say whether a pull request regressed a
    suite, and the previous behaviour was to red it anyway.
    """
    store = store_with(tmp_path, baseline_line("a", runs=20, passes=20))
    doc = case_file(tmp_path, "a", version_key=KEY, passes=1, scored=4)
    assert main(["suite", "--case-result", str(doc), "--baseline-dir", str(store)]) == 0
    printed = capsys.readouterr().out
    assert "advisory only" in printed
    assert "BELOW the margin" in printed


def test_the_sample_floor_comes_from_the_environment(tmp_path, monkeypatch):
    store = store_with(tmp_path, baseline_line("a", runs=20, passes=20))
    doc = case_file(tmp_path, "a", version_key=KEY, passes=1, scored=4)
    monkeypatch.setenv("EVAL_AGGREGATE_MIN_SCORED", "2")
    assert main(["suite", "--case-result", str(doc), "--baseline-dir", str(store)]) == 1


def test_a_bootstrap_admitted_name_matching_no_graded_case_is_reported(
    tmp_path, monkeypatch, capsys
):
    """A typo there un-arms the case it was written to keep blocking.

    Nothing else would notice: the name simply never matches, admission is
    unchanged, and the run is green.
    """
    monkeypatch.setenv("BOOTSTRAP_ADMITTED", "crashloop-debug a")
    doc = case_file(tmp_path, "a", passes=4, scored=4)
    assert main(["suite", "--case-result", str(doc)]) == 0
    captured = capsys.readouterr()
    assert "crashloop-debug" in captured.out
    assert "names no graded case" in captured.out
    assert "names no graded case" in captured.err
    # The name that DID match is not reported as unknown.
    assert "`a`" not in captured.out.split("names no graded case")[1].split("\n")[0]


def test_a_fully_matching_bootstrap_admitted_says_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BOOTSTRAP_ADMITTED", "a")
    doc = case_file(tmp_path, "a", passes=4, scored=4)
    assert main(["suite", "--case-result", str(doc)]) == 0
    assert "names no graded case" not in capsys.readouterr().out


def test_a_judged_metric_matching_nothing_is_reported_by_the_case_command(
    kanban_task, tmp_path, monkeypatch, capsys
):
    """The twin of the BOOTSTRAP_ADMITTED typo, and just as quiet.

    `OutcomValidity` matches no score the run emitted and nothing the baseline
    carries, so rung 6's loop skips it without a word: the judged comparison
    gates nothing while EVAL_JUDGED_METRICS reads as though it gates a metric.
    """
    monkeypatch.setenv("EVAL_JUDGED_METRICS", "OutcomValidity")
    out = tmp_path / "case.json"
    assert run_case(kanban_task, [FIXTURE_RUNS / GREEN_RUNS[0]], out) == 0
    # stderr, so it survives a reader who only greps stdout for "Result:".
    assert "OutcomValidity" in capsys.readouterr().err
    assert any("OutcomValidity" in n for n in payload(out)["notes"])


def test_a_correctly_spelled_judged_metric_says_nothing(
    kanban_task, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("EVAL_JUDGED_METRICS", "OutcomeValidity")
    assert run_case(kanban_task, [FIXTURE_RUNS / GREEN_RUNS[0]], tmp_path / "c.json") == 0
    assert "matched nothing" not in capsys.readouterr().err


def test_the_suite_banners_a_judged_metric_that_matched_nothing_once(tmp_path, capsys):
    """One configuration mistake, reported once.

    It is per-case in the ladder because that is where the evidence is, but it
    is one typo in one environment variable -- repeating it fourteen times in
    the banner is how a reader learns to skip banners.
    """
    note = (
        "judged metric(s) named in EVAL_JUDGED_METRICS matched nothing this run "
        "scored and nothing the baseline carries: OutcomValidity. Rung 6 is not "
        "gating on them."
    )
    docs = [case_file(tmp_path, n, notes=[note]) for n in ("a", "b")]
    assert main(["suite", *sum((["--case-result", str(d)] for d in docs), [])]) == 0
    captured = capsys.readouterr()
    assert captured.out.count("OutcomValidity") == 1
    assert "judged rung degraded" in captured.out
    assert captured.err.count("OutcomValidity") == 1


def test_a_suite_of_cases_without_notes_is_unchanged(tmp_path, capsys):
    """Older case files predate the field. Absent must read as empty, not
    crash the suite that consumes them."""
    doc = case_file(tmp_path, "a")
    assert "notes" not in payload(doc)
    assert main(["suite", "--case-result", str(doc)]) == 0
    assert "judged rung degraded" not in capsys.readouterr().out


def test_an_explicit_baseline_rate_still_overrides_the_store(tmp_path):
    store = store_with(tmp_path, baseline_line("a", runs=20, passes=20))
    doc = case_file(tmp_path, "a", version_key=KEY, passes=1, scored=4)
    assert main([
        "suite", "--case-result", str(doc), "--baseline-dir", str(store),
        "--baseline-rate", "0.1",
    ]) == 0


def test_an_unadmitted_case_contributes_nothing_to_either_side(tmp_path):
    """It failed everything and main passed everything, and the suite is still
    green: an unscreened case's rate is not yet a number worth comparing, and
    that has to hold on BOTH sides or the comparison is between two different
    populations of cases.
    """
    store = store_with(tmp_path, baseline_line("a", runs=20, passes=20))
    doc = case_file(tmp_path, "a", version_key=KEY, admitted=False, passes=0, scored=4)
    out = tmp_path / "suite.json"
    assert main([
        "suite", "--case-result", str(doc), "--baseline-dir", str(store),
        "--json-out", str(out),
    ]) == 0
    verdict = json.loads(out.read_text(encoding="utf-8"))
    assert verdict["pass_rate"] is None and verdict["baseline_rate"] is None


def test_a_bootstrap_admitted_case_has_no_baseline_to_compare_against(
    tmp_path, capsys
):
    """Admitted by fiat, not by evidence. It arms rung 4 and contributes
    nothing to main's side of the aggregate -- the fix is to screen it."""
    store = store_with(tmp_path)
    doc = case_file(tmp_path, "a", version_key=KEY, passes=0, scored=3)
    assert main(["suite", "--case-result", str(doc), "--baseline-dir", str(store)]) == 0
    assert "advisory" in capsys.readouterr().out


def test_rung_6_arms_itself_once_the_store_has_judged_evidence(
    kanban_task, tmp_path, monkeypatch, make_run
):
    """The comparator wires all the way through the CLI, and is quiet until
    there is something to compare against."""
    monkeypatch.setenv("JUDGE_MODEL", JUDGE)
    monkeypatch.setenv("BOOTSTRAP_ADMITTED", "agent-kanban-smoke")

    def sour(rec):
        rec["scores"]["OutcomeValidity"]["score"] = 0.1

    runs = [make_run(mutate=sour) for _ in range(3)]
    out = tmp_path / "case.json"

    empty = store_with(tmp_path / "empty")
    assert graded_case(kanban_task, runs, out, empty)["blocking"] is False

    seeded = store_with(
        tmp_path / "seeded",
        *[
            baseline_line(
                "agent-kanban-smoke", runs=3, passes=3,
                judged={"OutcomeValidity": {"mean": 1.0, "n": 3}},
                at=f"2026-08-0{i + 1}T00:00:00Z",
            )
            for i in range(7)
        ],
    )
    doc = graded_case(kanban_task, runs, out, seeded)
    assert doc["rung"] == 6 and doc["blocking"] is True
    assert doc["baseline_judged"]["OutcomeValidity"] == 1.0


def test_a_corrupt_store_is_never_read_as_an_empty_one(kanban_task, tmp_path, capsys):
    """Empty means "nothing admitted, aggregate advisory", which is a
    legitimate green. A corrupt file reaching that state would disarm the gate
    on the day somebody fat-fingers a line."""
    store = store_with(tmp_path)
    (store / "agent-kanban-smoke.jsonl").write_text("{ not json\n", encoding="utf-8")
    out = tmp_path / "case.json"
    assert run_case(kanban_task, [FIXTURE_RUNS / GREEN_RUNS[0]], out) == 0  # shipped store
    assert main([
        "case", "--task", str(kanban_task), "--baseline-dir", str(store),
        "--result", str(FIXTURE_RUNS / GREEN_RUNS[0]),
    ]) == 2
    assert main(["suite", "--case-result", str(out), "--baseline-dir", str(store)]) == 1


# --------------------------------------------------------------------------
# Where the evidence lives. The gate's logic is backend-blind by construction:
# everything below exercises the same ladder against `gs://` instead of a
# directory, and the only thing that changes is the banner when a read is
# degraded or capped.
# --------------------------------------------------------------------------


@pytest.fixture
def gcloud(monkeypatch):
    """A `gcloud storage` that talks to a dict instead of the network."""
    fake = FakeGcloud()
    monkeypatch.setattr(evidence_store.subprocess, "run", fake)
    return fake


def seed_gcs(fake: FakeGcloud, *lines: dict, prefix: str = "gs://b/evidence") -> str:
    """Seed the bucket the way `bench-gate record` would: nested under the key."""
    for i, line in enumerate(lines, start=1):
        stamp = line["recorded_at"].replace(":", "-")
        key_dir = "/".join(_key_segments(line.get("key")))
        fake.objects[f"{prefix}/{line['case']}/{key_dir}/{stamp}-{i}.jsonl"] = (
            json.dumps(line) + "\n"
        )
    return prefix


def test_the_environment_can_move_the_evidence_off_disk(
    kanban_task, tmp_path, monkeypatch, gcloud
):
    """`EVAL_BASELINE_STORE` selects the store; `--baseline-dir` keeps holding
    VERSIONS.json. That split is the point: the fleet and verifiers integers
    are reviewed configuration, not measured data."""
    monkeypatch.setenv("JUDGE_MODEL", JUDGE)
    monkeypatch.setenv("BOOTSTRAP_ADMITTED", "agent-kanban-smoke")
    seed_gcs(
        gcloud,
        *[
            baseline_line(
                "agent-kanban-smoke", runs=3, passes=3,
                judged={"OutcomeValidity": {"mean": 1.0, "n": 3}},
                at=f"2026-08-0{i + 1}T00:00:00Z",
            )
            for i in range(7)
        ],
    )
    monkeypatch.setenv("EVAL_BASELINE_STORE", "gs://b/evidence")

    out = tmp_path / "case.json"
    doc = graded_case(kanban_task, [FIXTURE_RUNS / n for n in RED_RUNS], out, store_with(tmp_path))
    # 21 runs of screening evidence at the current key, all passing: admitted
    # from GCS exactly as it would have been from a directory, and collapsed.
    assert doc["admitted"] is True and doc["rung"] == 4


def test_the_flag_beats_the_environment(kanban_task, tmp_path, monkeypatch, gcloud):
    monkeypatch.setenv("EVAL_BASELINE_STORE", "gs://b/wrong")
    store = store_with(tmp_path)
    out = tmp_path / "case.json"
    assert run_case(
        kanban_task, [FIXTURE_RUNS / GREEN_RUNS[0]], out,
        "--baseline-store", str(store),
    ) == 0
    assert gcloud.calls == []  # never went near GCS


def test_record_writes_an_object_the_gate_reads_back(kanban_task, tmp_path, monkeypatch, gcloud):
    """The loop closes over GCS too, which is the only reason the backend
    exists: on main the recorder has no push credential, so a checked-in
    store has no writer."""
    monkeypatch.setenv("JUDGE_MODEL", JUDGE)
    monkeypatch.setenv("EVAL_BASELINE_STORE", "gs://b/evidence")
    versions = store_with(tmp_path)
    out = tmp_path / "case.json"
    graded_case(kanban_task, [FIXTURE_RUNS / n for n in GREEN_RUNS], out, versions)

    assert run_record(versions, out) == 0
    (url,) = list(gcloud.objects)
    assert url.startswith("gs://b/evidence/agent-kanban-smoke/")
    written = json.loads(gcloud.objects[url])
    assert (written["runs"], written["passes"]) == (2, 2)


def test_an_unreachable_store_degrades_rather_than_reds(
    kanban_task, tmp_path, monkeypatch, gcloud, capsys
):
    """A blip must not red every pull request in the repo. That failure mode
    is what gets a gate switched off, which costs more than the coverage the
    gate was buying."""
    gcloud.fail = "ERROR: (gcloud.storage.ls) 503 Backend Error"
    monkeypatch.setenv("EVAL_BASELINE_STORE", "gs://b/evidence")
    out = tmp_path / "case.json"
    assert run_case(kanban_task, [FIXTURE_RUNS / n for n in GREEN_RUNS], out) == 0
    err = capsys.readouterr().err
    assert "baseline store gs://b/evidence unreachable" in err
    assert "nothing can be admitted" in err
    assert payload(out)["admitted"] is False


def test_the_degraded_read_is_written_into_the_verdict_not_only_the_log(
    tmp_path, monkeypatch, gcloud
):
    """A green nobody knows was measured against nothing is the one outcome
    worse than a red."""
    gcloud.fail = "ERROR: (gcloud.storage.ls) 503 Backend Error"
    monkeypatch.setenv("EVAL_BASELINE_STORE", "gs://b/evidence")
    doc = case_file(tmp_path, "a", version_key=KEY, passes=3, scored=3)
    md = tmp_path / "verdict.md"
    assert main(["suite", "--case-result", str(doc), "--markdown-out", str(md)]) == 0
    text = md.read_text(encoding="utf-8")
    assert "WARNING — baseline unavailable" in text
    assert "weaker than a normal one" in text


def test_a_corrupt_object_is_still_fatal(kanban_task, tmp_path, monkeypatch, gcloud):
    """Unreachable degrades; bytes that arrived and will not parse do not.
    Reading a corrupt store as an empty one silently disarms the gate."""
    gcloud.objects["gs://b/evidence/agent-kanban-smoke/2026-08-01T00-00-00Z-1.jsonl"] = (
        "{ not json\n"
    )
    monkeypatch.setenv("EVAL_BASELINE_STORE", "gs://b/evidence")
    assert main([
        "case", "--task", str(kanban_task), "--baseline-dir", str(store_with(tmp_path)),
        "--result", str(FIXTURE_RUNS / GREEN_RUNS[0]),
    ]) == 2


def test_a_capped_read_says_so_in_the_verdict(tmp_path, monkeypatch, gcloud):
    """The cap never binds at realistic history depths, but a cap that is
    silent reads as "I considered everything" when it did not."""
    monkeypatch.setenv("EVAL_BASELINE_STORE", "gs://b/evidence")
    monkeypatch.setenv("EVAL_BASELINE_MAX_OBJECTS", "2")
    seed_gcs(
        gcloud,
        *[
            baseline_line("a", runs=3, passes=3, at=f"2026-08-0{i + 1}T00:00:00Z")
            for i in range(5)
        ],
    )
    doc = case_file(tmp_path, "a", version_key=KEY, passes=3, scored=3)
    md = tmp_path / "verdict.md"
    assert main(["suite", "--case-result", str(doc), "--markdown-out", str(md)]) == 0
    text = md.read_text(encoding="utf-8")
    assert "NOTE — truncated read" in text
    assert "`a`: the 3 oldest" in text
