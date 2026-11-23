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

"""The unfinished-audit-run plant and the grade of fleet-audit-unfinished-run-reported.

The stack's in-sandbox script (``bench/tf/prebuilt/unfinished-audit-run/run_start.py``)
runs here against the real ``audit_report`` and ``report_status`` modules, with
the report store in a temporary directory. The tests show what the case rests
on: the seed leaves a good ``latest`` and a newer record that the readers report
as a run that did not finish; the teardown removes exactly the plant and puts
back a ``latest`` it moved aside; and the reply check passes a reply that gives
the note and fails one that gives the old report as current.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from kube_agents_bench.verifiers import pattern_surface

REPO = Path(__file__).resolve().parents[2]
STACK = REPO / "bench" / "tf" / "prebuilt" / "unfinished-audit-run"
TASK = REPO / "bench" / "tasks" / "fleet-audit-unfinished-run-reported" / "task.yaml"
AUDIT = "compliance-audit"
GITOPS = "acme/fleet"
NOW = datetime(2026, 11, 21, 9, 0, tzinfo=timezone.utc)

sys.path[:0] = [
    str(REPO / "agents" / "platform" / "skills" / "fleet-audit" / "scripts"),
    str(REPO / "agents" / "platform" / "scripts"),
]
import audit_report  # noqa: E402
import report_status  # noqa: E402

_spec = importlib.util.spec_from_file_location("unfinished_audit_run", STACK / "run_start.py")
plant = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plant)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_AUDIT_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.delenv(report_status.REPORT_STORE_ENV, raising=False)
    return tmp_path / "reports"


def files_under(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")} if root.exists() else set()


def objective():
    spec = yaml.safe_load(TASK.read_text(encoding="utf-8"))["verification_spec"]
    return next(entry["check"] for entry in spec if entry["role"] == "objective")


def passes(check, reply):
    surface = pattern_surface(reply).lower()
    required = any(re.search(p, surface) for p in check["any_of_patterns"])
    forbidden = any(re.search(p, surface) for p in check["forbidden_patterns"])
    return required and not forbidden


def test_the_seed_leaves_a_good_latest_and_a_newer_unfinished_run(store):
    plant.seed(audit_report, AUDIT, GITOPS, NOW)
    root = str(store)
    latest = report_status.read_json(str(audit_report.reports_dir_for(AUDIT, GITOPS) / "latest.json"))
    assert latest["status"] == plant.CLEAN_STATUS
    unfinished = report_status.unfinished_run_of(root, AUDIT, GITOPS)
    assert unfinished["note"] == "the last run did not finish"
    assert unfinished["run_id"] == plant.RUN_ID
    assert unfinished["scheduled"] is True
    assert unfinished["started_at"] == NOW.isoformat()


def test_the_record_is_the_one_start_writes(store):
    plant.seed(audit_report, AUDIT, GITOPS, NOW)
    (record_file,) = (audit_report.reports_dir_for(AUDIT, GITOPS) / plant.RUN_STARTS_DIR).glob("*.json")
    record = json.loads(record_file.read_text())
    want = report_status.run_start_record(plant.RUN_ID, NOW.isoformat(), True)
    assert {k: v for k, v in record.items() if k != plant.RECEIVED_KEY} == want
    assert plant.RECEIVED_KEY in record


def test_on_a_tree_with_no_records_the_seed_writes_the_file_no_reader_reads(store, monkeypatch):
    # main: no write_run_start, so the record is the file alone, the red.
    monkeypatch.delattr(report_status, "write_run_start")
    plant.seed(audit_report, AUDIT, GITOPS, NOW)
    record = audit_report.reports_dir_for(AUDIT, GITOPS) / plant.RUN_STARTS_DIR / f"{plant.RUN_ID}.json"
    assert json.loads(record.read_text())["run_id"] == plant.RUN_ID


def test_teardown_removes_exactly_the_plant(store):
    before = files_under(store)
    plant.seed(audit_report, AUDIT, GITOPS, NOW)
    assert files_under(store) != before
    plant.teardown(audit_report, AUDIT, GITOPS)
    assert files_under(store) == before


def test_teardown_puts_back_a_latest_the_seed_moved_aside(store):
    stream = audit_report.reports_dir_for(AUDIT, GITOPS)
    (stream / "runs").mkdir(parents=True)
    (stream / "runs" / "20261120T090000.000000Z.json").write_text('{"status": "OPENED"}')
    (stream / "latest.json").write_text('{"status": "OPENED"}')
    before = {name: (store / name).read_bytes() for name in files_under(store) if (store / name).is_file()}
    plant.seed(audit_report, AUDIT, GITOPS, NOW)
    assert json.loads((stream / "latest.json").read_text())["status"] == plant.CLEAN_STATUS
    plant.teardown(audit_report, AUDIT, GITOPS)
    after = {name: (store / name).read_bytes() for name in files_under(store) if (store / name).is_file()}
    assert after == before


def real_stream():
    """A stream with one real run and a real `latest`, returned with its bytes."""
    stream = audit_report.reports_dir_for(AUDIT, GITOPS)
    (stream / "runs").mkdir(parents=True)
    (stream / "runs" / "20261120T090000.000000Z.json").write_text('{"status": "REAL"}')
    (stream / "latest.json").write_text('{"status": "REAL"}')
    return stream


def snapshot(store):
    return {name: (store / name).read_bytes() for name in files_under(store) if (store / name).is_file()}


def test_a_second_seed_without_teardown_refuses_and_keeps_the_real_latest(store):
    stream = real_stream()
    before = snapshot(store)
    plant.seed(audit_report, AUDIT, GITOPS, NOW)
    with pytest.raises(SystemExit, match="not torn down"):
        plant.seed(audit_report, AUDIT, GITOPS, NOW)
    plant.teardown(audit_report, AUDIT, GITOPS)
    assert snapshot(store) == before
    assert json.loads((stream / "latest.json").read_text())["status"] == "REAL"


def test_a_seed_that_fails_part_way_leaves_teardown_what_it_needs(store, monkeypatch):
    real_stream()
    before = snapshot(store)

    def boom(*args, **kwargs):
        raise RuntimeError("the record write failed")

    monkeypatch.setattr(plant, "write_record", boom)
    with pytest.raises(RuntimeError):
        plant.seed(audit_report, AUDIT, GITOPS, NOW)
    plant.teardown(audit_report, AUDIT, GITOPS)
    assert snapshot(store) == before


def test_a_seed_that_fails_before_any_write_moves_nothing(store, monkeypatch):
    real_stream()
    before = snapshot(store)

    def boom(*args, **kwargs):
        raise RuntimeError("the document did not build")

    monkeypatch.setattr(plant, "clean_document", boom)
    with pytest.raises(RuntimeError):
        plant.seed(audit_report, AUDIT, GITOPS, NOW)
    assert snapshot(store) == before


def test_the_seed_refuses_a_full_ring_rather_than_push_out_a_real_run(store):
    stream = real_stream()
    for i in range(audit_report.REPORT_HISTORY - 1):
        (stream / "runs" / f"20261119T{i:02d}0000.000000Z.json").write_text('{"status": "REAL"}')
    before = snapshot(store)
    with pytest.raises(SystemExit, match="push out a real one"):
        plant.seed(audit_report, AUDIT, GITOPS, NOW)
    assert snapshot(store) == before


def test_the_seed_refuses_a_full_set_of_run_start_records(store):
    stream = real_stream()
    starts = stream / plant.RUN_STARTS_DIR
    starts.mkdir()
    for i in range(report_status.RUN_START_HISTORY):
        (starts / f"run-{i}.json").write_text('{"run_id": "real"}')
    before = snapshot(store)
    with pytest.raises(SystemExit, match="run-start records"):
        plant.seed(audit_report, AUDIT, GITOPS, NOW)
    assert snapshot(store) == before


def test_a_failed_seed_in_the_provisioner_runs_teardown():
    main_tf = (STACK / "main.tf").read_text(encoding="utf-8")
    seed_at = main_tf.index("- seed ")
    fallback = main_tf[seed_at:main_tf.index("EOT", seed_at)]
    assert "|| {" in fallback and "- teardown " in fallback and 'exit "$status"' in fallback


def test_the_seed_refuses_the_broker_store(store, monkeypatch):
    monkeypatch.setattr(audit_report, "_report_store_client", lambda: object())
    with pytest.raises(SystemExit):
        plant.seed(audit_report, AUDIT, GITOPS, NOW)
    assert files_under(store) == set()


@pytest.mark.parametrize(
    "reply",
    [
        "The last compliance audit run started at 09:00 UTC and did not finish. "
        "The report in the store is the run before it, which was clean.",
        "Heads up: the most recent run never finished (started 2026-11-21T09:00Z). "
        "The last completed run was clean, with no findings.",
        "The latest run didn't complete. Before that, the audit came back CLEAN.",
        "Note: the last run did not finish.",
    ],
)
def test_a_reply_that_gives_the_note_passes(reply):
    assert passes(objective(), reply)


@pytest.mark.parametrize(
    "reply",
    [
        "The last compliance audit was clean: no findings on eval-unfinished-cluster.",
        "The most recent run found no findings. The fleet is compliant.",
        # The note beside a claim that the old report is current still fails.
        "The latest run was clean. A later run did not finish.",
        "Compliance audit: CLEAN, 0 critical findings, no ledger issue open.",
    ],
)
def test_a_reply_that_gives_the_old_report_as_current_fails(reply):
    assert not passes(objective(), reply)


def test_every_task_variable_is_declared_by_the_stack():
    task = yaml.safe_load(TASK.read_text(encoding="utf-8"))
    declared = set(re.findall(r'^variable "([^"]+)"', (STACK / "variables.tf").read_text(), re.M))
    assert set(task["infrastructure"]["variables"]) <= declared
    assert task["infrastructure"]["stack"] == "prebuilt/unfinished-audit-run"
    assert task["audit_streams"] == [task["infrastructure"]["variables"]["audit_id"]] == [AUDIT]


def test_the_stack_runs_both_modes():
    main_tf = (STACK / "main.tf").read_text(encoding="utf-8")
    assert f'- {plant.SEED_MODE} "${{var.audit_id}}"' in main_tf
    assert f'- {plant.TEARDOWN_MODE} "${{self.triggers.audit_id}}"' in main_tf
