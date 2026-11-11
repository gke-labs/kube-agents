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
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from devops_bench.verification.spec import parse_node

from kube_agents_bench import onboarding, transcript, verifiers
from kube_agents_bench.verifiers import AuditReportStoredVerifier, ReportContainsVerifier, WorkerCommandsVerifier

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


_NO_KEY = object()


def _store(root: Path, *, status: str = "NO_FORGE", finished: datetime, ids=(BINDING_ID,), started=_NO_KEY) -> None:
    directory = root / AUDIT / "no_forge" / "local"
    directory.mkdir(parents=True)
    envelope = {
        "audit_id": AUDIT,
        "repo": "no_forge/local",
        "status": status,
        "finished_at": finished.isoformat(),
        "current_ids": list(ids),
        "document": {"findings": [{"id": fid} for fid in ids]},
    }
    if started is not _NO_KEY:
        envelope["started_at"] = started.isoformat() if isinstance(started, datetime) else started
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


def test_a_report_whose_run_started_in_this_window_passes(root):
    """#4268: an envelope naming its run's start counts when that run opened in the window."""
    _store(root, finished=STARTED + timedelta(minutes=9), started=STARTED + timedelta(minutes=1))
    assert _verify().status == "pass"


def test_a_report_from_a_run_that_started_in_an_earlier_repetition_fails(root):
    """It finished inside this run, but its run opened before it: another repetition's report."""
    _store(root, finished=STARTED + timedelta(minutes=2), started=STARTED - timedelta(minutes=30))
    assert _verify().status == "fail"


def test_a_report_that_could_not_name_its_start_fails(root):
    """A new image writes the key; null is a finish whose run record and note were both gone."""
    _store(root, finished=STARTED + timedelta(minutes=2), started=None)
    assert _verify().status == "fail"


def test_a_report_refused_for_its_start_says_so(root):
    _store(root, finished=STARTED + timedelta(minutes=2), started=STARTED - timedelta(minutes=30))
    result = _verify()
    assert "1 finished since but came from a run that opened before then or named no start" in result.reason
    assert result.raw["stored"][0]["started_at"] == (STARTED - timedelta(minutes=30)).isoformat()


def test_a_start_inside_the_clock_skew_allowance_passes(root):
    """The runner and the pod are two clocks: the start bound takes the same skew as finished_at."""
    _store(root, finished=STARTED + timedelta(minutes=5), started=STARTED - timedelta(seconds=60))
    assert _verify().status == "pass"


@pytest.mark.parametrize("started", ["not a time", "2026-10-13T18:05:00"], ids=["unparseable", "naive"])
def test_a_start_that_is_not_an_aware_time_fails(root, started):
    _store(root, finished=STARTED + timedelta(minutes=5), started=started)
    assert _verify().status == "fail"


def test_an_older_images_report_with_no_start_key_is_read_by_finished_at(root):
    _store(root, finished=STARTED + timedelta(minutes=2))
    assert _verify().status == "pass"


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


def test_an_id_the_run_only_held_does_not_count(root):
    _store(root, finished=STARTED + timedelta(minutes=12), ids=())
    envelope_path = root / AUDIT / "no_forge" / "local" / "latest.json"
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    envelope["current_ids"] = [BINDING_ID]
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
    assert _verify().status == "fail"


def test_unreadable_pods_are_an_error(root, monkeypatch):
    monkeypatch.setattr(onboarding, "sandbox_shell", _silent_shell)
    assert _verify().status == "error"


def test_the_case_declares_the_check():
    spec = yaml.safe_load(TASK.read_text())["verification_spec"]
    check = next(entry for entry in spec if entry["name"] == "the-report-store-holds-this-run")
    node = parse_node(check["check"])
    assert isinstance(node, AuditReportStoredVerifier)
    assert node.status == "NO_FORGE"


def test_a_misspelt_status_fails_at_load():
    with pytest.raises(ValueError):
        AuditReportStoredVerifier(type="audit_report_stored", audit=AUDIT, status="NO-FORGE")


def test_the_status_set_matches_what_the_status_view_knows():
    """`AuditStatus` copies the outcomes `finish` stores. The status view in
    scripts/ lists the same set as its known statuses; read it from source so
    the two cannot drift apart unnoticed."""
    view = (REPO / "scripts" / "fleet_audit_status_view.py").read_text()
    block = view[view.index("STATUS_STYLE = {"):]
    block = block[: block.index("}")]
    known = set(re.findall(r'^\s+"([A-Z_]+)": "', block, re.M))
    assert len(known) == 6, known
    assert known == set(verifiers.AuditStatus.__args__)


SCRIPTS = "/opt/data/profiles/platform/skills/fleet-audit/scripts"


def _worker_check(name: str, commands: list[str]) -> str:
    spec = yaml.safe_load(TASK.read_text())["verification_spec"]
    check = next(entry for entry in spec if entry["name"] == name)["check"]
    transcript.set(output="", trajectory=[], worker_commands=[{"task": "t_1", "command": c} for c in commands])
    try:
        return WorkerCommandsVerifier(**check).verify(5.0).status
    finally:
        transcript.clear()


@pytest.mark.parametrize(
    "command",
    [
        f"python3 {SCRIPTS}/audit_report.py start --audit {AUDIT}",
        f"python3 {SCRIPTS}/audit_report.py start --audit={AUDIT}",
        f"cd /tmp && {SCRIPTS}/audit_report.py start --audit {AUDIT} > start.json",
    ],
)
def test_a_start_run_is_credited(command):
    assert _worker_check("the-worker-ran-start", [command]) == "pass"


@pytest.mark.parametrize(
    "command",
    [
        f'grep -n "audit_report.py start --audit" {SCRIPTS}/../SKILL.md',
        f'echo "python3 {SCRIPTS}/audit_report.py start --audit {AUDIT}"',
        f"python3 {SCRIPTS}/audit_report.py start --help",
        f"python3 {SCRIPTS}/audit_report.py finish --audit {AUDIT}",
        f"python3 {SCRIPTS}/collect.py {AUDIT}",
    ],
)
def test_a_mention_or_another_run_is_not(command):
    assert _worker_check("the-worker-ran-start", [command]) == "fail"


def _answer_check(name: str, answer: str) -> str:
    spec = yaml.safe_load(TASK.read_text())["verification_spec"]
    check = next(entry for entry in spec if entry["name"] == name)["check"]
    transcript.set(output=answer, trajectory=[], final_message=answer)
    try:
        return ReportContainsVerifier(**check).verify(5.0).status
    finally:
        transcript.clear()


@pytest.mark.parametrize(
    "answer",
    [
        "No GitOps forge is configured, so no ledger issue was opened.",
        "No forge is configured on this install.",
        "no git forge here",
        "There is no GitOps repository registered.",
        "No GitHub forge is configured, so no ledger issue was opened.",
        "There is no GitLab forge on this install.",
    ],
)
def test_the_no_forge_answer_is_recognised(answer):
    assert _answer_check("the-answer-says-no-forge-is-configured", answer) == "pass"


@pytest.mark.parametrize(
    "answer",
    [
        "Opened ledger issue #12 in acme/infra.",
        "The forge rejected the push.",
    ],
)
def test_an_answer_without_it_is_not(answer):
    assert _answer_check("the-answer-says-no-forge-is-configured", answer) == "fail"


NEW_CASE = REPO / "bench" / "tasks" / "fleet-audit-unchanged-manifest-skips-synthesis" / "task.yaml"


def _verify_envelope(root, **fields):
    check = {"type": "audit_report_stored", "audit": AUDIT, **fields}
    return AuditReportStoredVerifier(**check).verify(1.0)


def _store_with(root, extra, finished):
    _store(root, finished=finished)
    path = root / AUDIT / "no_forge" / "local" / "latest.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope.update(extra)
    path.write_text(json.dumps(envelope), encoding="utf-8")


def test_a_required_envelope_key_passes_when_it_matches(root):
    _store_with(root, {"precheck": "unchanged", "scheduled": True}, STARTED + timedelta(minutes=12))
    result = _verify_envelope(root, required_envelope={"precheck": "unchanged", "scheduled": True})
    assert result.status == "pass", result.reason


def test_a_required_envelope_key_that_differs_fails_naming_it(root):
    _store_with(root, {"precheck": "unchanged"}, STARTED + timedelta(minutes=12))
    result = _verify_envelope(root, required_envelope={"precheck": "findings-changed"})
    assert result.status == "fail"
    assert "precheck is 'unchanged', not 'findings-changed'" in result.reason


def test_a_required_envelope_key_the_envelope_lacks_fails(root):
    _store(root, finished=STARTED + timedelta(minutes=12))
    result = _verify_envelope(root, required_envelope={"precheck": "unchanged"})
    assert result.status == "fail"
    assert "precheck is None, not 'unchanged'" in result.reason


def test_required_envelope_is_checked_before_the_finding_phrases(root):
    _store(root, finished=STARTED + timedelta(minutes=12))
    result = _verify_envelope(root, required_envelope={"precheck": "unchanged"}, required_finding_phrases=["debug-binding"])
    assert result.status == "fail"
    assert "precheck" in result.reason


def _state_shell(state: dict | None):
    def shell(script: str, timeout: float) -> str:
        if script.startswith("cat "):
            return json.dumps(state) if state is not None else ""
        return _local_shell(script, timeout)
    return shell


def test_since_state_file_takes_the_stacks_fired_at_as_the_clock(root, monkeypatch):
    fired = STARTED - timedelta(hours=2)
    monkeypatch.setattr(onboarding, "agent_shell", _state_shell({"job_id": AUDIT, "fired_at": fired.isoformat()}))
    # Older than the run's start, newer than the fire: counts with the file, not without it.
    _store_with(root, {"precheck": "unchanged"}, fired + timedelta(minutes=9))
    without = _verify_envelope(root, required_envelope={"precheck": "unchanged"})
    assert without.status == "fail"
    with_file = _verify_envelope(root, required_envelope={"precheck": "unchanged"}, since_state_file="/opt/data/.bench-x.json")
    assert with_file.status == "pass", with_file.reason


def test_under_since_state_file_the_start_is_bound_by_the_fire(root, monkeypatch):
    """#4268: with a state file the start takes the fire as its bound, with no skew."""
    fired = STARTED - timedelta(hours=2)
    monkeypatch.setattr(onboarding, "agent_shell", _state_shell({"job_id": AUDIT, "fired_at": fired.isoformat()}))
    _store_with(root, {"precheck": "unchanged", "started_at": (fired + timedelta(minutes=1)).isoformat()}, fired + timedelta(minutes=9))
    inside = _verify_envelope(root, required_envelope={"precheck": "unchanged"}, since_state_file="/opt/data/.bench-x.json")
    assert inside.status == "pass", inside.reason
    (root / AUDIT).rename(root / "old")
    _store_with(root, {"precheck": "unchanged", "started_at": (fired - timedelta(seconds=5)).isoformat()}, fired + timedelta(minutes=9))
    before = _verify_envelope(root, required_envelope={"precheck": "unchanged"}, since_state_file="/opt/data/.bench-x.json")
    assert before.status == "fail"


def test_the_planted_envelope_seconds_before_the_fire_does_not_count(root, monkeypatch):
    """No skew in state-file mode: the plant's finish runs seconds before the fire, in the same pod."""
    fired = STARTED - timedelta(hours=2)
    monkeypatch.setattr(onboarding, "agent_shell", _state_shell({"job_id": AUDIT, "fired_at": fired.isoformat()}))
    _store_with(root, {"precheck": None}, fired - timedelta(seconds=30))
    result = _verify_envelope(root, required_envelope={"precheck": "unchanged"}, since_state_file="/opt/data/.bench-x.json")
    assert result.status == "fail"
    assert "since the stack fired the audit" in result.reason


def test_an_envelope_older_than_the_fire_fails_with_the_fire_named(root, monkeypatch):
    fired = STARTED - timedelta(hours=2)
    monkeypatch.setattr(onboarding, "agent_shell", _state_shell({"job_id": AUDIT, "fired_at": fired.isoformat()}))
    _store_with(root, {"precheck": "unchanged"}, fired - timedelta(minutes=9))
    result = _verify_envelope(root, required_envelope={"precheck": "unchanged"}, since_state_file="/opt/data/.bench-x.json")
    assert result.status == "fail"
    assert "since the stack fired the audit" in result.reason


@pytest.mark.parametrize("state", [None, {}, {"fired_at": "yesterday"}, {"fired_at": "2026-10-13T18:00:00"}])
def test_a_missing_or_unreadable_fire_is_an_error(root, monkeypatch, state):
    monkeypatch.setattr(onboarding, "agent_shell", _state_shell(state))
    _store_with(root, {"precheck": "unchanged"}, STARTED + timedelta(minutes=12))
    result = _verify_envelope(root, required_envelope={"precheck": "unchanged"}, since_state_file="/opt/data/.bench-x.json")
    assert result.status == "error"
    assert "did not fire" in result.reason


def test_read_fired_at_reads_the_arms_file():
    fired = datetime(2026, 11, 5, 8, 20, tzinfo=timezone.utc)
    shell = _state_shell({"job_id": "fleet-consistency-drift", "fired_at": fired.isoformat(), "was_paused": True})
    assert verifiers.read_fired_at(shell, "/opt/data/.bench-x.json", 1.0) == fired
    assert verifiers.read_fired_at(_silent_shell, "/opt/data/.bench-x.json", 1.0) is None


def test_the_unchanged_manifest_case_declares_the_check():
    spec = yaml.safe_load(NEW_CASE.read_text())["verification_spec"]
    check = next(entry for entry in spec if entry["name"] == "the-scheduled-run-was-closed-by-the-precheck")
    node = parse_node(check["check"])
    assert isinstance(node, AuditReportStoredVerifier)
    assert node.required_envelope == {"precheck": "unchanged"}
    assert node.since_state_file == "/opt/data/.bench-unchanged-manifest-digest.json"
    assert node.status is None


def test_the_unchanged_manifest_cases_state_file_is_the_arms():
    arm = (REPO / "bench" / "tf" / "prebuilt" / "unchanged-manifest-digest" / "arm.py").read_text()
    name = re.search(r'^STATE_NAME = "([^"]+)"', arm, re.M).group(1)
    spec = yaml.safe_load(NEW_CASE.read_text())["verification_spec"]
    check = next(entry for entry in spec if entry["name"] == "the-scheduled-run-was-closed-by-the-precheck")
    assert check["check"]["since_state_file"] == f"/opt/data/{name}"
