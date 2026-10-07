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

"""The first-run audit starts read and the ``oobe_audits_started`` verifier.

The read command runs under ``sh`` with this interpreter standing in for the agent's,
against the stack's state file and an ``executions`` table in a temporary directory,
created with the schema Hermes' cron store uses.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from kube_agents_bench import oobe

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "bench" / "tasks" / "oobe-first-run-audits" / "task.yaml"
STAGE = REPO / "agents" / "chat" / "scripts" / "oobe.py"
ARM = REPO / "bench" / "tf" / "prebuilt" / "oobe-first-run-audits" / "arm.py"

# Hermes' cron/executions.db, as the store creates it (test_bootstrap_delivered.py).
SCHEMA = """
CREATE TABLE executions (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL,
  source TEXT NOT NULL,
  process_id TEXT NOT NULL,
  pid INTEGER NOT NULL,
  process_started_at INTEGER,
  status TEXT NOT NULL CHECK(status IN
    ('claimed','running','completed','failed','unknown','skipped')),
  handoff_pending INTEGER NOT NULL DEFAULT 0,
  handoff_started_at REAL,
  claimed_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  error TEXT,
  skip_reason TEXT,
  delivery_outcome TEXT,
  scheduled_instant TEXT
)
"""
ARMED = datetime(2026, 10, 6, 20, 0, 0, tzinfo=timezone.utc)


def _local_shell(script: str, timeout: float) -> str:
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=timeout, check=True).stdout


class Store:
    def __init__(self, root: Path) -> None:
        self.state = root / ".bench-oobe.json"
        self.marker = root / ".oobe_audits_fired"
        self.db = root / "executions.db"
        with sqlite3.connect(self.db) as con:
            con.execute(SCHEMA)
        self.rows = 0

    def arm(self, at: datetime = ARMED, marked=oobe.FIRST_RUN_AUDITS) -> None:
        self.state.write_text(json.dumps({"applied_at": at.isoformat()}))
        self.marker.write_text(json.dumps({"fired": list(marked)}))

    def mark(self, audit: str, at: datetime) -> None:
        recorded = json.loads(self.marker.read_text())
        recorded.setdefault("marks", {})[audit] = at.timestamp()
        self.marker.write_text(json.dumps(recorded))

    def run(self, job: str, claimed: datetime, status: str = "running", finished: datetime | None = None) -> None:
        self.rows += 1
        with sqlite3.connect(self.db) as con:
            con.execute(
                "INSERT INTO executions (id, job_id, source, process_id, pid, status, claimed_at, finished_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (f"{self.rows:032x}", job, "builtin", "p", 1, status, claimed.isoformat(),
                 finished.isoformat() if finished else None),
            )

    def chain(self, start: datetime, last_status: str = "running") -> None:
        """The four runs one after another, each just after its mark, the last still going unless told otherwise."""
        at = start
        for i, audit in enumerate(oobe.FIRST_RUN_AUDITS):
            last = i == len(oobe.FIRST_RUN_AUDITS) - 1
            self.mark(audit, at - timedelta(seconds=30))
            self.run(audit, at, last_status if last else "completed", None if last else at + timedelta(minutes=3))
            at += timedelta(minutes=4)


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Store:
    s = Store(tmp_path)
    monkeypatch.setattr(oobe, "STATE_FILE", str(s.state))
    monkeypatch.setattr(oobe, "AUDITS_MARKER", str(s.marker))
    monkeypatch.setattr(oobe, "PLATFORM_EXECUTIONS_DB", str(s.db))
    monkeypatch.setattr(oobe, "HERMES_PYTHON", sys.executable)
    monkeypatch.setattr(oobe, "agent_shell", _local_shell)
    return s


def _verify():
    return oobe.OobeAuditsStartedVerifier(type="oobe_audits_started").verify(0.0)


# --- the names ------------------------------------------------------------


def test_the_state_file_is_the_one_the_stack_writes() -> None:
    assert f'".bench-oobe.json"' in ARM.read_text()
    assert Path(oobe.STATE_FILE).name == ".bench-oobe.json"


def test_the_case_names_this_check_and_the_entry_point_is_registered() -> None:
    spec = yaml.safe_load(TASK.read_text().split("\n---\n", 1)[1])["verification_spec"]
    assert any(e["check"].get("type") == "oobe_audits_started" for e in spec)
    points = tomllib.loads((REPO / "bench" / "pyproject.toml").read_text())["project"]["entry-points"]
    assert points["devops_bench.verifiers"]["oobe_audits_started"] == "kube_agents_bench.oobe:OobeAuditsStartedVerifier"


# --- the verdict ----------------------------------------------------------


def test_every_audit_started_in_turn_since_the_arm_passes(store: Store) -> None:
    store.arm()
    store.chain(ARMED + timedelta(minutes=2))
    result = _verify()
    assert result.status == "pass", result.reason


def test_audits_that_overlap_fail(store: Store) -> None:
    store.arm()
    for audit in oobe.FIRST_RUN_AUDITS:
        store.run(audit, ARMED + timedelta(minutes=2))
    result = _verify()
    assert result.status == "fail"
    assert "overlapped" in result.reason


def test_the_audit_list_matches_the_stage(store: Store) -> None:
    import ast

    tree = ast.parse(STAGE.read_text())
    shipped = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "FIRST_RUN_AUDITS" for t in node.targets)
    )
    assert tuple(shipped) == oobe.FIRST_RUN_AUDITS


def test_a_run_from_before_the_arm_does_not_count(store: Store) -> None:
    store.arm()
    for audit in oobe.FIRST_RUN_AUDITS:
        store.run(audit, ARMED - timedelta(hours=10), "completed")
    result = _verify()
    assert result.status == "fail"
    assert "nothing started the first-run audits" in result.reason


def test_a_missing_audit_is_named(store: Store) -> None:
    store.arm()
    for audit in oobe.FIRST_RUN_AUDITS[:-1]:
        store.run(audit, ARMED + timedelta(minutes=1))
    result = _verify()
    assert result.status == "fail"
    assert f"no run claimed since {ARMED.isoformat()} and its mark for {oobe.FIRST_RUN_AUDITS[-1]}" in result.reason


def test_completed_runs_pass(store: Store) -> None:
    store.arm()
    store.chain(ARMED + timedelta(minutes=2), last_status="completed")
    assert _verify().status == "pass"


def test_the_run_after_each_mark_is_graded(store: Store) -> None:
    # A scheduled compliance run between the arm and its mark, and one after the chain, both
    # overlap the stage's runs; neither is the run the stage started.
    store.arm()
    store.chain(ARMED + timedelta(minutes=10))
    store.run(oobe.FIRST_RUN_AUDITS[1], ARMED + timedelta(minutes=2), "completed", ARMED + timedelta(minutes=12))
    store.run(oobe.FIRST_RUN_AUDITS[0], ARMED + timedelta(minutes=23))
    result = _verify()
    assert result.status == "pass", result.reason


def test_a_skipped_row_is_passed_over(store: Store) -> None:
    store.arm()
    for audit in oobe.FIRST_RUN_AUDITS:
        store.run(audit, ARMED + timedelta(minutes=1), "skipped")
    store.chain(ARMED + timedelta(minutes=2))
    result = _verify()
    assert result.status == "pass", result.reason


def test_only_skipped_rows_are_no_run(store: Store) -> None:
    store.arm()
    for audit in oobe.FIRST_RUN_AUDITS:
        store.run(audit, ARMED + timedelta(minutes=2), "skipped")
    result = _verify()
    assert result.status == "fail"
    assert "no run claimed since" in result.reason


@pytest.mark.parametrize("status", ["claimed", "failed"])
def test_a_run_that_did_not_get_going_does_not_count(store: Store, status: str) -> None:
    # A run cut off at its start leaves a claimed or failed row.
    store.arm()
    for audit in oobe.FIRST_RUN_AUDITS:
        store.run(audit, ARMED + timedelta(minutes=2), status)
    result = _verify()
    assert result.status == "fail"
    assert f"({status})" in result.reason


def test_a_run_the_stage_did_not_mark_does_not_count(store: Store) -> None:
    # The 06:20 compliance run landing in the window is not the stage's.
    store.arm(marked=oobe.FIRST_RUN_AUDITS[1:])
    for audit in oobe.FIRST_RUN_AUDITS:
        store.run(audit, ARMED + timedelta(minutes=2))
    result = _verify()
    assert result.status == "fail"
    assert f"did not mark due (a scheduled one) for {oobe.FIRST_RUN_AUDITS[0]}" in result.reason


def test_no_marker_means_nothing_was_marked(store: Store) -> None:
    store.arm()
    store.marker.unlink()
    for audit in oobe.FIRST_RUN_AUDITS:
        store.run(audit, ARMED + timedelta(minutes=2))
    assert _verify().status == "fail"


def test_the_marker_is_the_one_the_stage_writes() -> None:
    assert Path(oobe.AUDITS_MARKER).name == '.oobe_audits_fired'
    assert 'AUDITS_MARKER = ".oobe_audits_fired"' in STAGE.read_text()
    assert 'STATE_FIRED = "fired"' in STAGE.read_text()
    assert 'STATE_MARKS = "marks"' in STAGE.read_text()


def test_another_jobs_run_does_not_count(store: Store) -> None:
    store.arm()
    store.run("gce-compute-fleet-audit", ARMED + timedelta(minutes=1))
    assert _verify().status == "fail"


def test_no_state_file_is_an_error(store: Store) -> None:
    result = _verify()
    assert result.status == "error"
    assert "did not arm" in result.reason


def test_an_unreadable_store_is_an_error(store: Store) -> None:
    store.arm()
    store.db.write_text("not a database")
    result = _verify()
    assert result.status == "error"
    assert str(store.db) in result.reason


def test_a_pod_that_does_not_answer_is_an_error(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oobe, "agent_shell", lambda _script, _timeout: "")
    assert _verify().status == "error"
