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

"""A case that needs a fixture the lease-time scan saw drifted is graded as infrastructure.

``hack/ci-eval-pr.sh`` scans the leased project's seeded fleet after it writes
the fleet kubeconfigs (``hack/fleet-fixture-state.py --report``);
:mod:`kube_agents_bench.fixture_drift` maps the report onto the matrix, and
each drifted case's repetitions are recorded through ``record_unit_not_run``
instead of run. These tests cover the mapping, then grade what the shell
writes through the real ``bench-gate case`` and ``bench-gate suite``: the
drifted case is infrastructure under ``fixture-drift:<role>``, the run stays
green, an unrelated case is untouched, and a scan that cannot be trusted
changes nothing.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import FIXTURE_RUNS, GREEN_RUNS, TASKS
from kube_agents_bench import fixture_drift
from kube_agents_bench.fixture_drift import FIXTURE_DRIFT_LABEL, LeaseScan, ScanUnusable
from kube_agents_bench.gate import main
from kube_agents_bench.scoring import INFRA_FAILURE_MARKER, fixture_drift_roles
from test_unit_not_run import (  # noqa: F401 -- _clean_env is an autouse fixture
    _clean_env,
    not_run,
    run_case,
)

PROJECT = "kube-agents-evals-7"
#: What hack/fleet-fixture-state.py writes for seeded-b once GKE moves its
#: REGULAR channel onto the planted version (#3269).
LAGGARD_DETAIL = (
    "cluster currentMasterVersion minor_behind_channel_default: observed 1.34.1-gke.100, "
    "the REGULAR default is 1.34 (why: the upgrades probes grade the lag)"
)


def report(tmp_path: Path, roles: dict[str, dict], *, schema_version=1) -> Path:
    path = tmp_path / "fixture-state-lease.json"
    path.write_text(
        json.dumps({"schema_version": schema_version, "project": PROJECT, "roles": roles, "summary": {}}),
        encoding="utf-8",
    )
    return path


def role(state: str, *detail: str) -> dict:
    return {"cluster_slot": "b", "state": state, "detail": list(detail)}


@pytest.fixture
def laggard_drifted(tmp_path) -> Path:
    return report(
        tmp_path,
        {
            "version-laggard": role("drifted", LAGGARD_DETAIL),
            "no-pdb-workload": role("converged"),
            "crashloop-workload": role("unchecked", "pod crashloop: the API server refused the read"),
            "idle-nodepool": role("unpublished"),
            "rbac-overgrant": role("no_state"),
        },
    )


def published(tmp_path: Path, now=("version-laggard",), before=("version-laggard",), project=PROJECT, age=timedelta(minutes=20)) -> Path:
    """The hourly scan's fixture-state.json: ``now`` drifted on ``project``
    in its latest scan, ``before`` in the one before, scanned ``age`` ago
    (None: no ``scanned_at``)."""
    path = tmp_path / "fixture-state-published.json"
    roles = {r: {"state": "drifted", "detail": ["x"]} for r in now} | {"no-pdb-workload": {"state": "healthy", "detail": []}}
    doc = {"schema_version": 1, "projects": {project: {"roles": roles}}, "previous": {"drifted": {project: list(before)}}}
    if age is not None:
        doc["scanned_at"] = (datetime.now(timezone.utc) - age).isoformat().replace("+00:00", "Z")
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


UPGRADES = TASKS / "upgrades-lagging-master-probe" / "task.yaml"
RELIABILITY = TASKS / "reliability-pdb-probe" / "task.yaml"
CRASHLOOP = TASKS / "cluster-agent-crashloop-debug" / "task.yaml"


# --- The mapping -------------------------------------------------------------


def test_only_a_drifted_role_counts(laggard_drifted):
    scan = fixture_drift.load_scan(laggard_drifted)
    assert scan.project == PROJECT
    assert scan.drifted == {"version-laggard": [LAGGARD_DETAIL]}


@pytest.mark.parametrize(
    "body",
    [None, "not json", json.dumps(["a list"]), json.dumps({"schema_version": 2, "roles": {}}), json.dumps({"schema_version": 1})],
    ids=["missing", "unparseable", "not-an-object", "other-schema", "no-roles"],
)
def test_a_report_that_cannot_be_trusted_is_unusable(tmp_path, body):
    path = tmp_path / "fixture-state-lease.json"
    if body is not None:
        path.write_text(body, encoding="utf-8")
    with pytest.raises(ScanUnusable):
        fixture_drift.load_scan(path)


def test_a_cases_roles_are_its_fixtures_list():
    # The motivating case: no check names version-laggard, only `fixtures:`.
    assert fixture_drift.case_roles(UPGRADES) == {"version-laggard"}
    assert fixture_drift.case_roles(RELIABILITY) == {"no-pdb-workload"}
    assert fixture_drift.case_roles(TASKS / "agent-kanban-smoke" / "task.yaml") == set()
    # The drift audit keeps its excuse for a drifted or under-a-day-old
    # seeded-c: listing its peer roles beside drift-outlier would make the
    # list multi-role and excuse nothing.
    assert fixture_drift.case_roles(TASKS / "consistency-drift-outlier" / "task.yaml") == {"drift-outlier"}


def write_task(tmp_path: Path, name: str, body: str) -> Path:
    task = tmp_path / name / "task.yaml"
    task.parent.mkdir()
    task.write_text(body, encoding="utf-8")
    return task


def test_the_roles_a_check_reads_win_over_the_fixtures_list(tmp_path):
    task = write_task(
        tmp_path,
        "case",
        "fixtures: [node-local-state, crashloop-workload, version-laggard]\n"
        "verification_spec:\n"
        "  - name: a\n"
        "    check:\n"
        "      type: all_of\n"
        "      checks:\n"
        "        - {type: fleet_resource_property, fixture_role: node-local-state}\n"
        "        - {type: report_contains, fixture_roles: [crashloop-workload, version-laggard]}\n",
    )
    assert fixture_drift.case_roles(task) == {"node-local-state"}
    # The real shape: the readiness case grades node-local-state's object and
    # names one role per slot only to hold the report to every cluster.
    assert fixture_drift.case_roles(TASKS / "upgrades-fleet-readiness-node-local-state" / "task.yaml") == {"node-local-state"}


def test_a_multi_role_list_with_no_check_reading_one_excuses_nothing(tmp_path):
    task = write_task(
        tmp_path,
        "case",
        "fixtures: [crashloop-workload, drift-outlier, version-laggard, zonal-skew-scheduling]\n"
        "verification_spec:\n"
        "  - name: a\n"
        "    check: {type: report_contains, fixture_roles: [crashloop-workload, version-laggard]}\n",
    )
    assert fixture_drift.case_roles(task) == set()
    assert fixture_drift.case_roles(TASKS / "upgrades-readiness-zonal-control-plane" / "task.yaml") == set()


def test_a_long_drift_detail_is_cut_to_its_first_line():
    long_line = "x" * (fixture_drift.DETAIL_MAX_CHARS * 3)
    scan = LeaseScan(project=PROJECT, drifted={"version-laggard": [long_line, "second assertion", "third"]})
    reason = fixture_drift.drift_reason({"version-laggard"}, scan)
    assert reason.startswith("fixture-drift:version-laggard: " + "x" * 50)
    assert long_line not in reason and "second assertion" not in reason
    assert "(+2 more in the scan report)" in reason


def test_the_reason_leads_with_the_label_and_carries_the_scans_words():
    scan = LeaseScan(project=PROJECT, drifted={"version-laggard": [LAGGARD_DETAIL]})
    reason = fixture_drift.drift_reason({"version-laggard"}, scan)
    assert reason.startswith(f"{FIXTURE_DRIFT_LABEL}:version-laggard: {LAGGARD_DETAIL}")
    assert PROJECT in reason
    assert fixture_drift.drift_reason({"no-pdb-workload"}, scan) is None
    assert fixture_drift.drift_reason(set(), scan) is None


def test_main_marks_the_case_that_needs_the_drifted_role_and_no_other(laggard_drifted, tmp_path):
    out = tmp_path / "marks"
    assert fixture_drift.main(["--report", str(laggard_drifted), "--published", str(published(tmp_path)), "--out-dir", str(out), str(UPGRADES), str(RELIABILITY), str(CRASHLOOP)]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["upgrades-lagging-master-probe"]
    assert (out / "upgrades-lagging-master-probe").read_text(encoding="utf-8").startswith("fixture-drift:version-laggard: ")


def test_main_on_an_unusable_report_marks_nothing_and_says_why(tmp_path, capsys):
    out = tmp_path / "marks"
    assert fixture_drift.main(["--report", str(tmp_path / "absent.json"), "--published", str(published(tmp_path)), "--out-dir", str(out), str(UPGRADES)]) == 0
    assert not out.exists() or not any(out.iterdir())
    assert "graded as usual" in capsys.readouterr().err


def test_main_skips_a_task_that_will_not_read(laggard_drifted, tmp_path, capsys):
    broken = tmp_path / "broken" / "task.yaml"
    broken.parent.mkdir()
    broken.write_text("fixtures: [version-laggard\n", encoding="utf-8")
    out = tmp_path / "marks"
    assert fixture_drift.main(["--report", str(laggard_drifted), "--published", str(published(tmp_path)), "--out-dir", str(out), str(broken), str(UPGRADES)]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["upgrades-lagging-master-probe"]
    assert "broken is graded as usual" in capsys.readouterr().err


def test_a_change_to_what_decides_drift_excuses_nothing(laggard_drifted, tmp_path, capsys):
    changed = tmp_path / "changed.txt"
    changed.write_text("docs/x.md\nbench/tf/fleet/fixtures.json\n", encoding="utf-8")
    out = tmp_path / "marks"
    argv = ["--report", str(laggard_drifted), "--published", str(published(tmp_path)), "--out-dir", str(out), "--changed-files", str(changed), str(UPGRADES)]
    assert fixture_drift.main(argv) == 0
    assert not out.exists() or not any(out.iterdir())
    assert "bench/tf/fleet/fixtures.json" in capsys.readouterr().err


def test_a_case_the_change_edits_is_never_excused(laggard_drifted, tmp_path, capsys):
    other = write_task(tmp_path, "other-laggard-case", "fixtures: [version-laggard]\n")
    changed = tmp_path / "changed.txt"
    changed.write_text("bench/tasks/upgrades-lagging-master-probe/task.yaml\n", encoding="utf-8")
    out = tmp_path / "marks"
    argv = ["--report", str(laggard_drifted), "--published", str(published(tmp_path)), "--out-dir", str(out), "--changed-files", str(changed), str(UPGRADES), str(other)]
    assert fixture_drift.main(argv) == 0
    assert sorted(p.name for p in out.iterdir()) == ["other-laggard-case"]
    assert "upgrades-lagging-master-probe needs a drifted fixture, but this change edits the case" in capsys.readouterr().err


def test_an_unreadable_change_list_excuses_nothing(laggard_drifted, tmp_path):
    out = tmp_path / "marks"
    argv = ["--report", str(laggard_drifted), "--published", str(published(tmp_path)), "--out-dir", str(out), "--changed-files", str(tmp_path / "absent.txt"), str(UPGRADES)]
    assert fixture_drift.main(argv) == 0
    assert not out.exists() or not any(out.iterdir())


def marks_for(laggard_drifted: Path, tmp_path: Path, published_path: Path) -> list[str]:
    out = tmp_path / "marks"
    assert fixture_drift.main(["--report", str(laggard_drifted), "--published", str(published_path), "--out-dir", str(out), str(UPGRADES)]) == 0
    return sorted(p.name for p in out.iterdir()) if out.exists() else []


def test_a_drift_the_hourly_scan_held_twice_and_the_lease_confirms_is_excused(laggard_drifted, tmp_path):
    assert marks_for(laggard_drifted, tmp_path, published(tmp_path)) == ["upgrades-lagging-master-probe"]


def test_a_transient_lease_time_drift_the_hourly_scan_has_not_held_is_not_excused(laggard_drifted, tmp_path, capsys):
    # A pod rescheduling, or the crashloop's restart lag: the hourly scan saw
    # it on its latest pass only, or not at all.
    assert marks_for(laggard_drifted, tmp_path, published(tmp_path, before=())) == []
    assert marks_for(laggard_drifted, tmp_path, published(tmp_path, now=(), before=())) == []
    assert "not excused" in capsys.readouterr().err
    # Sustained on another project says nothing about this one.
    assert marks_for(laggard_drifted, tmp_path, published(tmp_path, project="kube-agents-evals-9")) == []


def test_a_stale_or_undated_published_scan_excuses_nothing(laggard_drifted, tmp_path):
    # The scan job stopped: its last verdicts may be an incident since repaired.
    assert marks_for(laggard_drifted, tmp_path, published(tmp_path, age=timedelta(hours=4))) == []
    assert marks_for(laggard_drifted, tmp_path, published(tmp_path, age=None)) == []
    with pytest.raises(ScanUnusable):
        fixture_drift.sustained_roles(published(tmp_path, age=timedelta(hours=4)), PROJECT)


def test_an_unreadable_hourly_result_excuses_nothing(laggard_drifted, tmp_path, capsys):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert marks_for(laggard_drifted, tmp_path, broken) == []
    assert marks_for(laggard_drifted, tmp_path, tmp_path / "absent.json") == []
    assert "graded as usual" in capsys.readouterr().err


# --- What the gate makes of it ---------------------------------------------


def drift_reason() -> str:
    scan = LeaseScan(project=PROJECT, drifted={"version-laggard": [LAGGARD_DETAIL]})
    return fixture_drift.drift_reason(fixture_drift.case_roles(UPGRADES), scan)


def suite(tmp_path: Path, *case_files: Path) -> tuple[int, dict]:
    verdict = tmp_path / "verdict.json"
    argv = ["suite"]
    for f in case_files:
        argv += ["--case-result", str(f)]
    rc = main([*argv, "--json-out", str(verdict)])
    return rc, json.loads(verdict.read_text(encoding="utf-8"))


def test_an_admitted_case_on_a_drifted_fixture_is_infra_and_the_run_stays_green(kanban_task, tmp_path, monkeypatch):
    """Before this, the same three not-run records made the run not
    evaluated (exit 2), which Prow reds, on every lease of the project."""
    monkeypatch.setenv("BOOTSTRAP_ADMITTED", "upgrades-lagging-master-probe,agent-kanban-smoke")
    state = tmp_path / "state"
    lost = [not_run(state, "upgrades-lagging-master-probe", rep, drift_reason()) for rep in (1, 2, 3)]
    drifted = run_case(UPGRADES, lost, tmp_path / "case-a.json")
    assert [r["outcome"] for r in drifted["reps"]] == ["infra"] * 3
    assert all(r["reason"].startswith(INFRA_FAILURE_MARKER) and "fixture-drift:version-laggard" in r["reason"] for r in drifted["reps"])
    assert drifted["rung_name"] == "INFRA" and drifted["blocking"] is False
    assert drifted["reason"].startswith("not run: fixture-drift:version-laggard")
    unrelated = run_case(kanban_task, [FIXTURE_RUNS / GREEN_RUNS[0], FIXTURE_RUNS / GREEN_RUNS[1]], tmp_path / "case-b.json")
    assert unrelated["rung_name"] == "GREEN" and unrelated["passes"] == 2

    rc, doc = suite(tmp_path, tmp_path / "case-a.json", tmp_path / "case-b.json")
    assert rc == 0 and doc["outcome"] == "green"
    assert doc["not_evaluated"] == []
    assert doc["fixture_drift"] == {"upgrades-lagging-master-probe": ["fixture-drift:version-laggard"]}
    assert any("upgrades-lagging-master-probe (fixture-drift:version-laggard)" in n for n in doc["notes"])


def test_only_the_label_where_the_scorer_puts_it_counts():
    assert fixture_drift_roles([f"{INFRA_FAILURE_MARKER}: the record holds no scored run, so there is nothing to grade ({drift_reason()})"]) == [
        "fixture-drift:version-laggard"
    ]
    assert fixture_drift_roles(["devops-bench crashed reading fixture-drift:version-laggard from a log"]) == []
    assert fixture_drift_roles([]) == []


def test_weather_on_the_same_admitted_case_is_still_not_evaluated(kanban_task, tmp_path, monkeypatch):
    """The coverage floor is unchanged for every other infrastructure loss,
    and for a case where only some repetitions carry the label."""
    monkeypatch.setenv("BOOTSTRAP_ADMITTED", "upgrades-lagging-master-probe")
    state = tmp_path / "state"
    lost = [
        not_run(state, "upgrades-lagging-master-probe", 1, drift_reason()),
        not_run(state, "upgrades-lagging-master-probe", 2, "the gateway Deployment was not found before launch"),
        not_run(state, "upgrades-lagging-master-probe", 3, drift_reason()),
    ]
    run_case(UPGRADES, lost, tmp_path / "case-a.json")
    run_case(kanban_task, [FIXTURE_RUNS / GREEN_RUNS[0]], tmp_path / "case-b.json")
    rc, doc = suite(tmp_path, tmp_path / "case-a.json", tmp_path / "case-b.json")
    assert rc == 2 and doc["outcome"] == "not_evaluated"
    assert doc["not_evaluated"] == ["upgrades-lagging-master-probe"]
    assert "fixture_drift" not in doc  # no drift, no key: the verdict keeps its usual shape


def test_every_case_on_a_drifted_fixture_is_still_not_evaluated(tmp_path):
    """A run where nothing else graded evaluated nothing, drift or not."""
    state = tmp_path / "state"
    lost = [not_run(state, "upgrades-lagging-master-probe", rep, drift_reason()) for rep in (1, 2, 3)]
    run_case(UPGRADES, lost, tmp_path / "case-a.json")
    rc, doc = suite(tmp_path, tmp_path / "case-a.json")
    assert rc == 2 and doc["outcome"] == "not_evaluated"
    assert doc["fixture_drift"] == {"upgrades-lagging-master-probe": ["fixture-drift:version-laggard"]}
