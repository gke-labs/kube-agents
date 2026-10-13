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

"""The ``audit_report_stored`` verifier, over a report store in a temporary directory.

The store read runs for real through ``sh -c``, as it does in the pod; only the
root it reads and the two pod shells are swapped for local ones.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from devops_bench.verification.spec import parse_node

from kube_agents_bench import onboarding, transcript, verifiers
from kube_agents_bench.verifiers import AuditReportStoredVerifier

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "bench" / "tasks" / "fleet-audit-without-forge-keeps-the-report" / "task.yaml"
AUDIT = "compliance-audit"
STARTED = datetime(2026, 10, 13, 18, 0, tzinfo=timezone.utc)
BINDING_ID = "cluster-admin-binding.seeded-a._.clusterrolebinding-debug-binding"


def _local_shell(script: str, timeout: float) -> str:
    proc = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=timeout, check=False)
    return proc.stdout if proc.returncode == 0 else ""


def _silent_shell(script: str, timeout: float) -> str:
    return ""


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(verifiers, "REPORT_STORE_ROOT", str(tmp_path))
    monkeypatch.setattr(onboarding, "sandbox_shell", _local_shell)
    monkeypatch.setattr(onboarding, "agent_shell", _silent_shell)
    transcript.set(output="", trajectory=[], started_at=STARTED.timestamp())
    yield tmp_path
    transcript.clear()


def _store(root: Path, *, status: str = "NO_FORGE", finished: datetime, ids=(BINDING_ID,)) -> None:
    directory = root / AUDIT / "no_forge" / "local"
    directory.mkdir(parents=True)
    envelope = {
        "audit_id": AUDIT,
        "repo": "no_forge/local",
        "status": status,
        "finished_at": finished.isoformat(),
        "current_ids": list(ids),
        "document": {"findings": []},
    }
    (directory / "latest.json").write_text(json.dumps(envelope), encoding="utf-8")


def _verify(**fields):
    check = {"type": "audit_report_stored", "audit": AUDIT, "status": "NO_FORGE",
             "required_finding_phrases": ["debug-binding"], **fields}
    return AuditReportStoredVerifier(**check).verify(1.0)


def test_this_runs_report_passes(root):
    _store(root, finished=STARTED + timedelta(minutes=12))
    result = _verify()
    assert result.status == "pass", result.reason


def test_no_report_is_a_fail(root):
    result = _verify()
    assert result.status == "fail"
    assert "finished since the run started" in result.reason


def test_a_previous_runs_report_is_a_fail(root):
    _store(root, finished=STARTED - timedelta(hours=1))
    assert _verify().status == "fail"


def test_a_ledger_run_is_not_a_no_forge_run(root):
    _store(root, status="UPDATED", finished=STARTED + timedelta(minutes=12))
    result = _verify()
    assert result.status == "fail"
    assert "'UPDATED'" in result.reason


def test_the_finding_has_to_be_in_the_report(root):
    _store(root, finished=STARTED + timedelta(minutes=12), ids=("netpol-missing.seeded-a.payments.namespace-payments",))
    result = _verify()
    assert result.status == "fail"
    assert "debug-binding" in result.reason


def test_unreadable_pods_are_an_error(root, monkeypatch):
    monkeypatch.setattr(onboarding, "sandbox_shell", _silent_shell)
    assert _verify().status == "error"


def test_the_case_declares_the_check():
    spec = yaml.safe_load(TASK.read_text())["verification_spec"]
    check = next(entry for entry in spec if entry["name"] == "the-report-store-holds-this-run")
    node = parse_node(check["check"])
    assert isinstance(node, AuditReportStoredVerifier)
    assert node.status == "NO_FORGE"
