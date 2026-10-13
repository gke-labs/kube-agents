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

"""A repetition the launcher could not start is graded as infrastructure.

``hack/ci-eval-pr.sh`` mints a GitHub token for every unit before it launches
devops-bench, and when GitHub's token endpoint fails transiently on every
attempt the unit does not run. On 2026-10-07 two GitHub incidents did that to
``pdb-remediation-pr`` on two unrelated pull requests; the repetitions went to
the gate as ``MISSING``, and a missing record on a noop-deployer task is rung
CHECK_DID_NOT_RUN -- "a harness or agent crash" -- so both runs went red.

The launcher now writes a record in place of the run (``record_unit_not_run``)
carrying the harness's infrastructure marker. These tests run that shell
function out of the script, then grade what it wrote through the real
``bench-gate case`` and ``bench-gate suite``, so the record shape and the
gate's reading of it are checked together rather than against a hand copy.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from kube_agents_bench.gate import main
from kube_agents_bench.scoring import INFRA_FAILURE_MARKER, MISSING

from conftest import FIXTURE_RUNS, GREEN_RUNS, TASKS
from test_gate import BASELINES, JUDGE, KEY, baseline_line, graded_case, run_record, store_with

CI_EVAL_PR = Path(__file__).resolve().parents[2] / "hack" / "ci-eval-pr.sh"

#: What GitHub said on the incident runs' last attempt, as the mint prints it.
MINT_FAILURE = (
    "GitHub answered HTTP 500 (Internal Server Error) minting for App 4739812 "
    "installation 157029058"
)
REASON = f"the ledger read token could not be minted before launch: {MINT_FAILURE}"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """The gate reads the environment; CI's must not leak into a test."""
    for name in (
        "BOOTSTRAP_ADMITTED",
        "JUDGE_MODEL",
        "DETERMINISTIC_CORRECTNESS_FLOOR",
        "EVAL_AGGREGATE_MARGIN",
        "EVAL_AGGREGATE_ARMED",
        "EVAL_ADMISSION_MODE",
        "EVAL_BASELINE_STORE",
        "PULL_NUMBER",
        "RC_COMMIT_SHA",
    ):
        monkeypatch.delenv(name, raising=False)


def _lift(pattern: str, what: str, flags: int = re.M) -> str:
    match = re.search(pattern, CI_EVAL_PR.read_text(encoding="utf-8"), flags)
    assert match, f"could not find {what} in hack/ci-eval-pr.sh"
    return match.group(0)


def not_run(state_dir: Path, name: str, rep: int, reason: str = REASON) -> Path:
    """Run the script's own record_unit_not_run; return the run directory it named."""
    script = "\n".join(
        [
            "set -euo pipefail",
            _lift(r"^readonly EVAL_INFRA_FAILURE_MARKER=.*$", "EVAL_INFRA_FAILURE_MARKER"),
            _lift(r"^readonly EVAL_NOT_RUN_DIR=.*$", "EVAL_NOT_RUN_DIR"),
            _lift(r"^readonly EVAL_NOT_RUN_STATUS=.*$", "EVAL_NOT_RUN_STATUS"),
            _lift(r"^record_unit_not_run\(\) \{.*?^\}", "record_unit_not_run", re.S | re.M),
            "_now_ms() { echo 1700000000000; }",
            'record_unit_not_run "$1" "$2" "$3"',
        ]
    )
    proc = subprocess.run(
        ["bash", "-c", script, "bash", name, str(rep), reason],
        capture_output=True,
        text=True,
        env={**os.environ, "STATE_DIR": str(state_dir)},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    named = (state_dir / f"{name}.rep{rep}.dir").read_text(encoding="utf-8").strip()
    assert named, "record_unit_not_run left the run directory empty: " + proc.stderr
    return Path(named)


def run_case(task: Path, runs, out: Path) -> dict:
    argv = ["case", "--task", str(task), "--baseline-dir", str(BASELINES)]
    for r in runs:
        argv += ["--result", str(r)]
    argv += ["--json-out", str(out)]
    assert main(argv) == 0
    return json.loads(out.read_text(encoding="utf-8"))


@pytest.fixture
def pdb_task() -> Path:
    """A second real noop-deployer task: the case the 2026-10-07 reds were on."""
    return TASKS / "pdb-remediation-pr" / "task.yaml"


def test_the_record_carries_the_marker_and_the_reason(tmp_path):
    run = not_run(tmp_path, "agent-kanban-smoke", 2)
    record = json.loads((run / "results.json").read_text(encoding="utf-8"))[0]
    assert record["errors"] == [f"{INFRA_FAILURE_MARKER}: {REASON}"]
    assert record["trajectory"] == [] and record["tokens"] == {"total": 0}
    assert "scores" not in record


def test_one_unminted_repetition_is_infrastructure_and_the_rest_grade(kanban_task, tmp_path):
    """One repetition lost, the case graded on the other two, nothing blocks."""
    lost = not_run(tmp_path / "state", "agent-kanban-smoke", 2)
    doc = run_case(
        kanban_task,
        [FIXTURE_RUNS / GREEN_RUNS[0], lost, FIXTURE_RUNS / GREEN_RUNS[1]],
        tmp_path / "case.json",
    )
    assert [r["outcome"] for r in doc["reps"]] == ["pass", "infra", "pass"]
    assert doc["blocking"] is False
    assert doc["passes"] == 2 and doc["scored"] == 2
    reason = doc["reps"][1]["reason"]
    assert reason.startswith(INFRA_FAILURE_MARKER)
    assert "HTTP 500" in reason and "could not be minted before launch" in reason
    assert "the record is scored" not in reason
    assert "harness or agent crash" not in reason


def test_a_lead_off_unminted_repetition_keeps_the_version_key(kanban_task, tmp_path, monkeypatch):
    """Repetition 1 lost, 2 and 3 real: the key comes off repetition 2.

    The not-run record is readable but has no manifest, so it carries no
    version key. Taken as the case's key, it would turn off rung 6's
    comparison against main and, on a nightly, leave the two real
    repetitions out of the baseline store. JUDGE_MODEL is set here because
    without it no record has a key and the test could not tell.
    """
    monkeypatch.setenv("JUDGE_MODEL", JUDGE)
    judged = {"OutcomeValidity": {"mean": 1.0, "n": 3}}
    store = store_with(
        tmp_path,
        *[
            baseline_line("agent-kanban-smoke", runs=3, passes=3, judged=judged, at=f"2026-08-0{i + 1}T00:00:00Z")
            for i in range(7)
        ],
    )
    lost = not_run(tmp_path / "state", "agent-kanban-smoke", 1)
    out = tmp_path / "case.json"
    doc = graded_case(kanban_task, [lost, FIXTURE_RUNS / GREEN_RUNS[0], FIXTURE_RUNS / GREEN_RUNS[1]], out, store)
    assert [r["outcome"] for r in doc["reps"]] == ["infra", "pass", "pass"]
    assert doc["version_key"] == KEY
    # Rung 6 has main's judged mean to compare against.
    assert doc["baseline_judged"] == {"OutcomeValidity": 1.0}
    assert doc["baseline_runs"] == 21
    # And the nightly files the two real repetitions under that key.
    before = (store / "agent-kanban-smoke.jsonl").read_text(encoding="utf-8").splitlines()
    assert run_record(store, out, extra=["--recorded-at", "2026-08-09T00:00:00Z"]) == 0
    after = (store / "agent-kanban-smoke.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(after) == len(before) + 1
    line = json.loads(after[-1])
    assert line["key"] == KEY and (line["runs"], line["passes"]) == (2, 2)


def test_every_repetition_unminted_excludes_the_case(pdb_task, tmp_path):
    """The #2250 shape: all three repetitions of pdb-remediation-pr lost."""
    state = tmp_path / "state"
    runs = [not_run(state, "pdb-remediation-pr", rep) for rep in (1, 2, 3)]
    doc = run_case(pdb_task, runs, tmp_path / "case.json")
    assert [r["outcome"] for r in doc["reps"]] == ["infra", "infra", "infra"]
    assert doc["rung_name"] == "INFRA"
    assert doc["blocking"] is False


def test_every_case_unminted_is_not_evaluated(kanban_task, pdb_task, tmp_path, capsys):
    """A run that graded nothing does not report green, and is not red at rung 2."""
    state = tmp_path / "state"
    cases = []
    for name, task in (("agent-kanban-smoke", kanban_task), ("pdb-remediation-pr", pdb_task)):
        runs = [not_run(state, name, rep) for rep in (1, 2, 3)]
        out = tmp_path / f"case-{name}.json"
        run_case(task, runs, out)
        cases += ["--case-result", str(out)]
    verdict = tmp_path / "verdict.json"
    assert main(["suite", *cases, "--json-out", str(verdict)]) == 2
    doc = json.loads(verdict.read_text(encoding="utf-8"))
    assert doc["outcome"] == "not_evaluated"
    assert doc["green"] is False
    assert sorted(doc["not_evaluated"]) == ["agent-kanban-smoke", "pdb-remediation-pr"]
    assert "**NOT EVALUATED**" in capsys.readouterr().out


def test_an_admitted_case_unminted_whole_is_not_evaluated(kanban_task, pdb_task, tmp_path, monkeypatch):
    """The existing coverage floor holds for this record too: an admitted case
    that lost every repetition cannot leave the run green, even beside a
    case that passed."""
    monkeypatch.setenv("BOOTSTRAP_ADMITTED", "pdb-remediation-pr")
    state = tmp_path / "state"
    lost = [not_run(state, "pdb-remediation-pr", rep) for rep in (1, 2, 3)]
    run_case(pdb_task, lost, tmp_path / "case-a.json")
    passed = run_case(
        kanban_task,
        [FIXTURE_RUNS / GREEN_RUNS[0], FIXTURE_RUNS / GREEN_RUNS[1]],
        tmp_path / "case-b.json",
    )
    assert passed["blocking"] is False
    verdict = tmp_path / "verdict.json"
    rc = main([
        "suite",
        "--case-result", str(tmp_path / "case-a.json"),
        "--case-result", str(tmp_path / "case-b.json"),
        "--json-out", str(verdict),
    ])
    doc = json.loads(verdict.read_text(encoding="utf-8"))
    assert rc == 2 and doc["outcome"] == "not_evaluated"
    assert doc["not_evaluated"] == ["pdb-remediation-pr"]


def test_a_genuine_missing_record_still_blocks_at_rung_two(kanban_task, tmp_path):
    """No results.json and no not-run record -- devops-bench died after the
    mint succeeded -- is still the crash rung 2 exists for."""
    doc = run_case(
        kanban_task,
        [FIXTURE_RUNS / GREEN_RUNS[0], MISSING, FIXTURE_RUNS / GREEN_RUNS[1]],
        tmp_path / "case.json",
    )
    assert [r["outcome"] for r in doc["reps"]] == ["pass", "blocked", "pass"]
    assert doc["rung_name"] == "CHECK_DID_NOT_RUN"
    assert doc["blocking"] is True
    assert "harness or agent crash" in doc["reps"][1]["reason"]
