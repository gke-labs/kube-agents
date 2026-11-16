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

"""The unread-target-ledger plant and the grade of fleet-audit-unread-target-keeps-finding.

The stack's in-sandbox script (``bench/tf/prebuilt/unread-target-ledger/ledger.py``)
runs here against the real ``audit_report`` module, with its forge replaced by a
recorder and its report store in a temporary directory. The tests show what the
case depends on: the seed opens a ledger and a stored record that ``finish``
trusts; a drift run that compares the host cluster on no facet keeps the seeded
finding as a held row; the phrase the case requires is in the hidden id block
of the body that run writes, and is not there when the body has no held rows,
which is the body that main writes.
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
STACK = REPO / "bench" / "tf" / "prebuilt" / "unread-target-ledger"
TASK = REPO / "bench" / "tasks" / "fleet-audit-unread-target-keeps-finding" / "task.yaml"
GITOPS = "acme/fleet"
PROJECT = "kube-agents-evals-2"
LOCATION = "us-central1-a"
HOST = "platform-agent-host"
PEER = "seeded-a"
NOW = datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)
UNLABELLED = "no-environment-label"

sys.path[:0] = [
    str(REPO / "agents" / "platform" / "skills" / "fleet-audit" / "scripts"),
    str(REPO / "agents" / "platform" / "scripts"),
]
import audit_report  # noqa: E402

_spec = importlib.util.spec_from_file_location("unread_target_ledger", STACK / "ledger.py")
ledger = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ledger)

AUDIT = ledger.AUDIT_ID


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
    ledger.seed(audit_report, PROJECT, LOCATION, HOST, GITOPS, NOW)
    return forge, forge.payloads("issue-create")[0]["body"]


def objective(name):
    spec = yaml.safe_load(TASK.read_text(encoding="utf-8"))["verification_spec"]
    return next(entry["check"] for entry in spec if entry["name"] == name)


def drift_run_over_the_unlabelled_host():
    """This run's document as the drift collector drafts it on the eval fleet.

    The host is in no cohort, so only `no-environment-label` ran there and the
    collector's `limitations` sentence says why; a labelled peer ran every
    facet. The host's `no-environment-label` finding puts the run on the
    findings branch, which writes the body again.
    """
    host = ledger.target_name(PROJECT, LOCATION, HOST)
    peer = ledger.target_name(PROJECT, LOCATION, PEER)
    command = f"gcloud container clusters list --project {PROJECT} --format=json"
    return audit_report.validate_findings(
        {
            "audit": AUDIT,
            "scope": {
                "clusters": [
                    {
                        "name": host,
                        "location": LOCATION,
                        "project": PROJECT,
                        "checks_run": [{"check": UNLABELLED, "command": command}],
                        "limitations": "cohort unknown has only 1 comparable cluster (minimum 3), no facet compared",
                    },
                    {
                        "name": peer,
                        "location": LOCATION,
                        "project": PROJECT,
                        "checks_run": [
                            {"check": check, "command": command}
                            for check in audit_report.audit_checks(AUDIT)
                        ],
                    },
                ],
                "skipped": [],
            },
            "findings": [
                {
                    "id": "unlabelled-host",
                    "check": UNLABELLED,
                    "severity": "minor",
                    "title": "Cluster has no environment label",
                    "cluster": host,
                    "namespace": "",
                    "object": f"Cluster/{HOST}",
                    "evidence": {"command": command, "excerpt": '"resourceLabels": {}'},
                    "impact": "No cohort compares this cluster, so drift on it is never reported.",
                    "recommendation": {
                        "action": "Set environment=seeded on the cluster.",
                        "rationale": "The label puts the cluster in a cohort that reaches the floor.",
                        "risk": "None on apply.",
                    },
                    "remediation": {"kind": "manual", "note": "Set the label."},
                }
            ],
        },
        AUDIT,
    )


def test_the_seed_opens_one_ledger_with_a_record_finish_trusts(store, monkeypatch):
    forge, body = seeded_body(monkeypatch)
    created = forge.payloads("issue-create")
    assert len(created) == 1
    assert created[0]["labels"] == ["agent:audit", f"audit:{AUDIT}"]
    ids = audit_report.parse_delta_block(body)
    assert len(ids) == 1 and ledger.HELD_OBJECT_NAME in ids[0]
    latest = audit_report.reports_dir_for(AUDIT, GITOPS) / audit_report.REPORT_LATEST_NAME
    assert latest.is_file()
    memory = audit_report.previous_run_memory(AUDIT, 7, GITOPS, body)
    assert memory is not None and not memory.get("seeded_from_ledger")
    assert forge.payloads("issue-close") == []
    assert ledger.recorded_seeded_issue(audit_report, GITOPS) == 7


def test_the_seed_fails_and_closes_its_ledger_when_the_store_write_does_nothing(
    store, monkeypatch
):
    # `write_report` never fails the run, and `previous_run_memory` then seeds a
    # memory from the ledger, so the seed must notice the lost write itself. It
    # closes the issue it opened: a failed apply runs no destroy.
    monkeypatch.setattr(audit_report, "write_report", lambda *a, **k: False)
    forge = forge_with(
        monkeypatch,
        **{
            "issue-list": {"issues": []},
            "issue-create": {"issue": {"number": 7, "url": "https://github.com/acme/fleet/issues/7"}},
        },
    )
    with pytest.raises(SystemExit):
        ledger.seed(audit_report, PROJECT, LOCATION, HOST, GITOPS, NOW)
    assert [p["number"] for p in forge.payloads("issue-close")] == [7]


def test_the_seed_refuses_over_an_open_ledger(store, monkeypatch):
    forge_with(monkeypatch, **{"issue-list": {"issues": [{"number": 3, "url": "u", "body": ""}]}})
    with pytest.raises(SystemExit):
        ledger.seed(audit_report, PROJECT, LOCATION, HOST, GITOPS, NOW)


def test_a_drift_run_that_compares_the_host_on_nothing_keeps_the_seeded_finding(
    store, monkeypatch
):
    _, body = seeded_body(monkeypatch)
    memory = audit_report.previous_run_memory(AUDIT, 7, GITOPS, body)
    data = drift_run_over_the_unlabelled_host()
    assert audit_report.coverage_gaps(data)
    held = audit_report.unread_held_entries(
        body, memory, data, exclude=set(audit_report.finding_ids(data["findings"]))
    )
    assert [entry["id"] for entry in held] == audit_report.parse_delta_block(body)
    assert "`label-keys` did not run on" in held[0][audit_report.UNREAD_REASON_KEY]

    check = objective("the-seeded-finding-stays-in-the-ledger-id-block")
    with_rows = audit_report.render_issue_body(
        data, generated_at=NOW, audit_id=AUDIT, held=held
    ).body
    # The body that main writes: this run's document, and no held row.
    without_rows = audit_report.render_issue_body(data, generated_at=NOW, audit_id=AUDIT).body
    for phrase in check["required_phrases"]:
        assert any(phrase in fid for fid in audit_report.parse_delta_block(with_rows))
        assert not any(phrase in fid for fid in audit_report.parse_delta_block(without_rows))


def test_a_drift_run_that_compares_the_host_does_not_keep_it(store, monkeypatch):
    """With the host in a cohort, `label-keys` ran there, so the finding is gone."""
    _, body = seeded_body(monkeypatch)
    memory = audit_report.previous_run_memory(AUDIT, 7, GITOPS, body)
    data = drift_run_over_the_unlabelled_host()
    host = data["scope"]["clusters"][0]
    host["checks_run"] = list(data["scope"]["clusters"][1]["checks_run"])
    host.pop("limitations")
    assert audit_report.unread_held_entries(body, memory, data, exclude=set()) == []


def test_teardown_closes_the_ledger_after_a_run_that_dropped_the_seeded_row(store, monkeypatch):
    """The seed refuses over an open ledger, so each open ledger of the stream is the seeded one.

    A run without the fix writes the body again and the invented object
    leaves it, so the teardown does not look for the name. With no recorded
    number, the list is the fallback.
    """
    forge = forge_with(
        monkeypatch,
        **{"issue-list": {"issues": [{"number": 7, "body": "the run's own body"}]}},
    )
    assert ledger.teardown(audit_report, GITOPS) == [7]
    assert [p["number"] for p in forge.payloads("issue-close")] == [7]
    assert forge.payloads("issue-list")[0]["labels"] == [f"audit:{AUDIT}"]


def test_teardown_closes_the_recorded_issue_that_the_listing_omits(store, monkeypatch):
    """On GitHub the label-filtered list can omit an open issue that got its label a short time before.

    The seed records the number it opened, so the teardown closes that issue
    after `issue-view` says it is open, also when the list is empty. Then it
    removes the record.
    """
    seeded_body(monkeypatch)
    forge = forge_with(
        monkeypatch,
        **{
            "issue-view": {"issue": {"number": 7, "state": "open"}},
            "issue-list": {"issues": []},
        },
    )
    assert ledger.teardown(audit_report, GITOPS) == [7]
    assert [p["number"] for p in forge.payloads("issue-view")] == [7]
    assert [p["number"] for p in forge.payloads("issue-close")] == [7]
    assert ledger.recorded_seeded_issue(audit_report, GITOPS) is None


def test_teardown_closes_the_recorded_issue_once_and_other_listed_ledgers(store, monkeypatch):
    """The recorded issue is not closed again from the list; another open ledger is closed."""
    seeded_body(monkeypatch)
    forge = forge_with(
        monkeypatch,
        **{
            "issue-view": {"issue": {"number": 7, "state": "open"}},
            "issue-list": {"issues": [{"number": 7}, {"number": 9}]},
        },
    )
    assert ledger.teardown(audit_report, GITOPS) == [7, 9]
    assert [p["number"] for p in forge.payloads("issue-close")] == [7, 9]


def test_teardown_does_not_close_a_recorded_issue_that_is_closed(store, monkeypatch):
    seeded_body(monkeypatch)
    forge = forge_with(
        monkeypatch,
        **{
            "issue-view": {"issue": {"number": 7, "state": "closed"}},
            "issue-list": {"issues": []},
        },
    )
    assert ledger.teardown(audit_report, GITOPS) == []
    assert forge.payloads("issue-close") == []


def test_teardown_ignores_a_record_for_another_repository(store, monkeypatch):
    seeded_body(monkeypatch)
    path = ledger.seeded_issue_path(audit_report, GITOPS)
    path.write_text('{"repo": "acme/other", "number": 7}\n', encoding="utf-8")
    forge = forge_with(monkeypatch, **{"issue-list": {"issues": []}})
    assert ledger.teardown(audit_report, GITOPS) == []
    assert forge.payloads("issue-view") == []


def test_the_seed_does_not_make_the_report_store_directory(store, monkeypatch):
    """The stored record makes the directory; the record of the number does not."""
    assert not store.exists()
    ledger.record_seeded_issue(audit_report, GITOPS, 7)
    assert not store.exists()
    assert ledger.recorded_seeded_issue(audit_report, GITOPS) is None


def test_every_task_variable_is_declared_by_the_stack():
    task = yaml.safe_load(TASK.read_text(encoding="utf-8"))
    declared = set(re.findall(r'^variable "([^"]+)"', (STACK / "variables.tf").read_text(), re.M))
    assert set((task["infrastructure"].get("variables") or {})) <= declared
    assert task["infrastructure"]["stack"] == "prebuilt/unread-target-ledger"
