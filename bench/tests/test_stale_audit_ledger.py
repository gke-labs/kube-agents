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

"""The stale-audit-ledger plant and the grade of fleet-audit-clean-close-reads-clean.

The stack's in-sandbox script (``bench/tf/prebuilt/stale-audit-ledger/ledger.py``)
runs here against the real ``audit_report`` module, with its forge replaced by a
recorder and its report store in a temporary directory. The tests show the three
things the case depends on: the seed opens a ledger and a stored record that
``finish`` trusts; a clean run over another cluster leaves the stale finding
unheld, so the run closes the ledger; and the phrases the case requires are in
the body that the clean close writes, and absent from the body the stack wrote.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
STACK = REPO / "bench" / "tf" / "prebuilt" / "stale-audit-ledger"
TASK = REPO / "bench" / "tasks" / "fleet-audit-clean-close-reads-clean" / "task.yaml"
AUDIT = "ai-security-audit"
GITOPS = "acme/fleet"
NOW = datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc)

sys.path[:0] = [
    str(REPO / "agents" / "platform" / "skills" / "fleet-audit" / "scripts"),
    str(REPO / "agents" / "platform" / "scripts"),
]
import audit_report  # noqa: E402

_spec = importlib.util.spec_from_file_location("stale_audit_ledger", STACK / "ledger.py")
ledger = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ledger)


class Forge:
    """Stands in for ``audit_report.forge``: records each call, answers from ``replies``."""

    def __init__(self, replies):
        self.replies = replies
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, verb, repo, payload):
        self.calls.append((verb, dict(payload)))
        return self.replies.get(verb, {})

    def payloads(self, verb):
        return [payload for called, payload in self.calls if called == verb]


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_AUDIT_REPORTS_DIR", str(tmp_path / "reports"))
    return tmp_path / "reports"


def forge_with(monkeypatch, **replies):
    forge = Forge(replies)
    monkeypatch.setattr(audit_report, "forge", forge)
    return forge


def seeded_body(monkeypatch):
    forge = forge_with(
        monkeypatch,
        **{
            "issue-list": {"issues": []},
            "issue-create": {"issue": {"number": 7, "url": "https://github.com/acme/fleet/issues/7"}},
        },
    )
    ledger.seed(audit_report, AUDIT, GITOPS, NOW)
    return forge, forge.payloads("issue-create")[0]["body"]


def objective(name):
    spec = yaml.safe_load(TASK.read_text(encoding="utf-8"))["verification_spec"]
    return next(entry["check"] for entry in spec if entry["name"] == name)


def test_the_seed_opens_one_ledger_with_a_record_finish_trusts(store, monkeypatch):
    forge, body = seeded_body(monkeypatch)
    created = forge.payloads("issue-create")
    assert len(created) == 1
    assert created[0]["labels"] == ["agent:audit", f"audit:{AUDIT}"]
    assert created[0]["title"] == audit_report.issue_title(AUDIT, [{"severity": "minor"}])
    ids = audit_report.parse_delta_block(body)
    assert len(ids) == 1 and ledger.RETIRED_CLUSTER in ids[0]
    latest = audit_report.reports_dir_for(AUDIT, GITOPS) / audit_report.REPORT_LATEST_NAME
    assert latest.is_file()
    memory = audit_report.previous_run_memory(AUDIT, 7, GITOPS, body)
    assert memory is not None and not memory.get("seeded_from_ledger")


def test_the_seed_fails_when_the_store_write_does_nothing(store, monkeypatch):
    # `write_report` never fails the run; the seed must notice a lost write.
    monkeypatch.setattr(audit_report, "write_report", lambda *a, **k: None)
    with pytest.raises(SystemExit):
        seeded_body(monkeypatch)


def test_the_seed_refuses_over_an_open_ledger(store, monkeypatch):
    forge_with(
        monkeypatch,
        **{"issue-list": {"issues": [{"number": 3, "url": "u", "body": ""}]}},
    )
    with pytest.raises(SystemExit):
        ledger.seed(audit_report, AUDIT, GITOPS, NOW)


def test_a_clean_run_on_another_cluster_does_not_hold_the_stale_finding(store, monkeypatch):
    _, body = seeded_body(monkeypatch)
    clean = audit_report.validate_findings(
        {
            "audit": AUDIT,
            "scope": {
                "clusters": [
                    {
                        "name": "kube-agents-host",
                        "location": "us-central1",
                        "project": "eval",
                        "checks_run": [
                            {"check": check, "command": "kubectl get deployments -A -o json"}
                            for check in audit_report.audit_checks(AUDIT)
                        ],
                    }
                ]
            },
            "findings": [],
        },
        AUDIT,
    )
    assert audit_report.coverage_gaps(clean) == []
    assert audit_report.unaccounted_previous_findings(body, clean) == []


def test_the_clean_close_writes_the_graded_phrases_and_the_seed_does_not(store, monkeypatch):
    _, body = seeded_body(monkeypatch)
    check = objective("the-closed-ledger-carries-the-clean-close-banner")
    forge = forge_with(monkeypatch, **{"issue-update": {"issue": {"number": 7}}})
    assert audit_report.close_ledger_reads_clean(GITOPS, 7, AUDIT, body, NOW)
    closed = forge.payloads("issue-update")[0]["body"]
    for phrase in check["required_phrases"]:
        assert phrase.lower() in closed.lower()
        assert phrase.lower() not in body.lower()
    for phrase in check["forbidden_phrases"]:
        assert phrase.lower() not in closed.lower()
    # The footer the grade reads stays the stack's, which is why the case
    # widens max_clock_skew_sec.
    assert re.findall(r"watchdog at (\S+)\.", closed)[-1] == NOW.isoformat()


def test_teardown_closes_only_the_ledger_this_case_opened(store, monkeypatch):
    forge = forge_with(
        monkeypatch,
        **{
            "issue-list": {
                "issues": [
                    {"number": 7, "body": f"... {ledger.RETIRED_CLUSTER} ..."},
                    {"number": 8, "body": "another stream run's ledger"},
                ]
            }
        },
    )
    assert ledger.teardown(audit_report, AUDIT, GITOPS) == [7]
    assert [p["number"] for p in forge.payloads("issue-close")] == [7]


def test_every_task_variable_is_declared_by_the_stack():
    task = yaml.safe_load(TASK.read_text(encoding="utf-8"))
    declared = set(re.findall(r'^variable "([^"]+)"', (STACK / "variables.tf").read_text(), re.M))
    assert set(task["infrastructure"]["variables"]) <= declared
    assert task["infrastructure"]["stack"] == "prebuilt/stale-audit-ledger"
