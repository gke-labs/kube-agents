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

"""The executions read and the ``bootstrap_delivered`` verifier.

The read command runs under ``sh`` with this interpreter standing in for the
agent's, against a marker and an ``executions`` table in a temporary
directory. The table is created with the schema Hermes' cron store uses, so
each verdict comes from the query as it runs in the pod.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from devops_bench.verification.base import VERIFIERS
from devops_bench.verification.spec import parse_node

from kube_agents_bench import onboarding
from kube_agents_bench.verifiers import BootstrapDeliveredVerifier

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "bench" / "tasks" / "bootstrap-inventory-ranking-delivery" / "task.yaml"
DELIVERY = REPO / "agents" / "chat" / "scripts" / "bootstrap_delivery.py"
VALIDATOR = REPO / "scripts" / "validate_bench_cases.py"

# Hermes' cron/executions.db, as the store creates it.
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
# cron/scheduler.py: _record_fire_ownership_lost.
OWNERSHIP_LOST = "Fire claim ownership lost; stale result was discarded."
CLAIM = datetime(2026, 9, 29, 14, 23, 40, 500000, tzinfo=timezone.utc)


def _objective() -> dict[str, Any]:
    spec = yaml.safe_load(TASK.read_text().split("\n---\n", 1)[1])["verification_spec"]
    return next(e for e in spec if e["name"] == "report-delivered")["check"]


def _local_shell(script: str, timeout: float) -> str:
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=timeout, check=True).stdout


def _lenient_shell(script: str, timeout: float) -> str:
    """As ``agent_shell`` behaves: a command that exits non-zero returns ``""``."""
    proc = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=timeout, check=False)
    return proc.stdout if proc.returncode == 0 else ""


class Store:
    """The agent pod's marker and executions table, in a temporary directory."""

    def __init__(self, root: Path) -> None:
        self.marker = root / ".bootstrap_completed"
        self.db = root / "executions.db"
        with sqlite3.connect(self.db) as con:
            con.execute(SCHEMA)
        self.rows = 0

    def claim(self, at: datetime = CLAIM) -> None:
        self.marker.touch()
        os.utime(self.marker, (at.timestamp(), at.timestamp()))

    def run(
        self,
        status: str,
        claimed: datetime,
        finished: datetime | None,
        *,
        error: str | None = None,
        job: str = onboarding.DELIVERY_JOB_ID,
        outcome: str | None = None,
    ) -> None:
        self.rows += 1
        with sqlite3.connect(self.db) as con:
            con.execute(
                "INSERT INTO executions (id, job_id, source, process_id, pid, status, claimed_at,"
                " started_at, finished_at, error, delivery_outcome) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"{self.rows:032x}", job, "scheduler", "p", 1, status, claimed.isoformat(),
                    (claimed + timedelta(milliseconds=100)).isoformat(),
                    finished.isoformat() if finished else None, error, outcome,
                ),
            )


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Store:
    s = Store(tmp_path)
    monkeypatch.setattr(onboarding, "COMPLETED_MARKER", str(s.marker))
    monkeypatch.setattr(onboarding, "EXECUTIONS_DB", str(s.db))
    monkeypatch.setattr(onboarding, "HERMES_PYTHON", sys.executable)
    monkeypatch.setattr(onboarding, "agent_shell", _local_shell)
    return s


def _verify(timeout: float = 0.0):
    return BootstrapDeliveredVerifier(type="bootstrap_delivered").verify(timeout)


def _around(claim: datetime = CLAIM) -> tuple[datetime, datetime]:
    return claim - timedelta(milliseconds=400), claim + timedelta(milliseconds=300)


# --- the names ------------------------------------------------------------


def test_the_job_and_marker_are_the_ones_the_delivery_script_uses() -> None:
    script = DELIVERY.read_text()
    assert f'DELIVERY_JOB_ID = "{onboarding.DELIVERY_JOB_ID}"' in script
    assert f'"{Path(onboarding.COMPLETED_MARKER).name}"' in script


# --- the read -------------------------------------------------------------


def test_the_read_returns_only_the_run_that_spans_the_claim(store: Store) -> None:
    store.claim()
    minute = timedelta(minutes=1)
    store.run("completed", CLAIM - minute, CLAIM - minute + timedelta(seconds=1))
    store.run("completed", *_around(), outcome="suppressed")
    store.run("failed", CLAIM + minute, CLAIM + minute + timedelta(seconds=1), error=OWNERSHIP_LOST)
    store.run("completed", *_around(), job="bootstrap-inventory-scan")
    read = onboarding.read_delivery_runs(onboarding.agent_shell, 10.0)
    assert read is not None
    assert read["marker"] == pytest.approx(CLAIM.timestamp())
    assert [(r["status"], r["delivery_outcome"]) for r in read["runs"]] == [("completed", "suppressed")]


def test_no_marker_is_read_without_opening_the_store(store: Store) -> None:
    store.db.unlink()
    assert onboarding.read_delivery_runs(onboarding.agent_shell, 10.0) == {"marker": None, "runs": [], "error": None}


def test_a_store_that_cannot_be_opened_is_an_error_that_names_it(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    store.claim()
    store.db.unlink()
    monkeypatch.setattr(onboarding, "agent_shell", _lenient_shell)
    read = onboarding.read_delivery_runs(onboarding.agent_shell, 10.0)
    assert read is not None
    assert read["runs"] == []
    assert read["error"] == f"{store.db}: unable to open database file"
    result = _verify()
    assert result.status == "error"
    assert "unable to open database file" in result.reason
    assert "kubectl exec failed" not in result.reason


def test_a_store_without_a_selected_column_is_an_error_that_names_it(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    store.claim()
    with sqlite3.connect(store.db) as con:
        con.execute("ALTER TABLE executions DROP COLUMN delivery_outcome")
    monkeypatch.setattr(onboarding, "agent_shell", _lenient_shell)
    result = _verify()
    assert result.status == "error"
    assert "no such column: delivery_outcome" in result.reason


@pytest.mark.parametrize(
    "reply",
    ["", "noise", f"{onboarding.RUNS_READ}\nnot json", f'{onboarding.RUNS_READ}\n{{"marker": 1}}'],
)
def test_a_reply_without_the_sentinel_or_the_json_is_a_failed_read(reply: str) -> None:
    assert onboarding.read_delivery_runs(lambda s, t: reply, 5.0) is None


def test_the_read_runs_the_agents_interpreter_and_falls_back_as_the_other_reads_do() -> None:
    cmd = onboarding.runs_command()
    assert cmd.startswith(f"PY={onboarding.HERMES_PYTHON}; ")
    assert f"|| PY={onboarding.FALLBACK_PYTHON}; " in cmd


def test_the_read_falls_back_when_the_agents_interpreter_is_missing(
    store: Store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(onboarding, "HERMES_PYTHON", str(tmp_path / "no-such-python"))
    monkeypatch.setattr(onboarding, "FALLBACK_PYTHON", sys.executable)
    store.claim()
    read = onboarding.read_delivery_runs(onboarding.agent_shell, 10.0)
    assert read is not None
    assert read["marker"] == pytest.approx(CLAIM.timestamp())


# --- the objective --------------------------------------------------------


def test_a_completed_delivering_run_passes(store: Store) -> None:
    store.claim()
    store.run("completed", *_around(), outcome="suppressed")
    result = _verify()
    assert result.status == "pass", result.reason
    assert "completed" in result.reason


def test_a_later_run_that_removed_the_jobs_does_not_count(store: Store) -> None:
    store.claim()
    store.run("completed", *_around())
    later = CLAIM + timedelta(minutes=5)
    store.run("failed", later, later + timedelta(seconds=1), error=OWNERSHIP_LOST)
    assert _verify().status == "pass"


def test_a_delivering_run_the_scheduler_discarded_fails_with_its_error(store: Store) -> None:
    store.claim()
    store.run("failed", *_around(), error=OWNERSHIP_LOST)
    result = _verify()
    assert result.status == "fail"
    assert f"ended failed: {OWNERSHIP_LOST}" in result.reason


def test_a_delivering_run_still_going_fails(store: Store) -> None:
    store.claim()
    store.run("running", _around()[0], None)
    result = _verify()
    assert result.status == "fail"
    assert "is still running" in result.reason


def test_no_marker_fails(store: Store) -> None:
    result = _verify()
    assert result.status == "fail"
    assert "never claimed the report" in result.reason


def test_a_claim_no_run_spans_fails(store: Store) -> None:
    store.claim()
    earlier = CLAIM - timedelta(minutes=1)
    store.run("completed", earlier, earlier + timedelta(seconds=1))
    result = _verify()
    assert result.status == "fail"
    assert "no run of bootstrap-inventory-delivery" in result.reason


def test_an_unreadable_agent_pod_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(onboarding, "agent_shell", lambda s, t: "")
    assert _verify().status == "error"


def test_the_run_is_waited_for(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    store.claim()
    reads = {"n": 0}

    def finishes_on_the_second_read(script: str, timeout: float) -> str:
        reads["n"] += 1
        if reads["n"] == 2:
            store.run("completed", *_around())
        return _local_shell(script, timeout)

    monkeypatch.setattr(onboarding, "agent_shell", finishes_on_the_second_read)
    result = _verify(timeout=10.0)
    assert result.status == "pass", result.reason
    assert reads["n"] >= 2


# --- registration ---------------------------------------------------------


def test_the_verifier_is_published_as_an_entry_point() -> None:
    with (REPO / "bench" / "pyproject.toml").open("rb") as fh:
        eps = tomllib.load(fh)["project"]["entry-points"]["devops_bench.verifiers"]
    assert eps["bootstrap_delivered"] == "kube_agents_bench.verifiers:BootstrapDeliveredVerifier"


def test_parse_node_builds_the_case_objective() -> None:
    assert isinstance(parse_node(_objective()), BootstrapDeliveredVerifier)
    assert VERIFIERS.get("bootstrap_delivered") is BootstrapDeliveredVerifier


def test_the_case_validator_knows_the_type() -> None:
    assert '"bootstrap_delivered": ()' in VALIDATOR.read_text()
