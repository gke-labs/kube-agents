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


def _store(root: Path, *, status: str = "NO_FORGE", finished: datetime, ids=(BINDING_ID,), started=_NO_KEY, excerpts=None) -> None:
    directory = root / AUDIT / "no_forge" / "local"
    directory.mkdir(parents=True)
    envelope = {
        "audit_id": AUDIT,
        "repo": "no_forge/local",
        "status": status,
        "finished_at": finished.isoformat(),
        "current_ids": list(ids),
        "document": {"findings": [{"id": fid, "evidence": {"excerpt": (excerpts or {}).get(fid, "")}} for fid in ids]},
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


def test_a_report_whose_start_is_after_its_finish_fails(root):
    """A start later than the finish is a later run's stamp, not this report's run."""
    _store(root, finished=STARTED + timedelta(minutes=2), started=STARTED + timedelta(minutes=3))
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


SCOPED_ID = "serviceentry-host-collision.local._.serviceentry-host-ledger-example"


def test_a_forbidden_finding_in_the_report_fails_naming_it(root):
    _store(root, finished=STARTED + timedelta(minutes=12), ids=(BINDING_ID, SCOPED_ID))
    result = _verify(forbidden_finding_phrases=["serviceentry-host-ledger-example"])
    assert result.status == "fail"
    assert "serviceentry-host-ledger-example" in result.reason


def test_a_report_without_the_forbidden_finding_passes(root):
    _store(root, finished=STARTED + timedelta(minutes=12))
    assert _verify(forbidden_finding_phrases=["serviceentry-host-ledger-example"]).status == "pass"


def test_a_missing_required_finding_is_named_before_a_forbidden_one(root):
    _store(root, finished=STARTED + timedelta(minutes=12), ids=(SCOPED_ID,))
    result = _verify(forbidden_finding_phrases=["serviceentry-host-ledger-example"])
    assert result.status == "fail"
    assert "no finding in its document has an id containing ['debug-binding']" in result.reason


EVIDENCE = "They differ in addresses 10.0.0.1 in team-a/x, 10.0.0.2 in team-b/x."


def test_a_required_evidence_phrase_in_the_matched_finding_passes(root):
    _store(root, finished=STARTED + timedelta(minutes=12), excerpts={BINDING_ID: EVIDENCE})
    assert _verify(required_evidence_phrases=["They differ in addresses"]).status == "pass"


def test_a_missing_evidence_phrase_fails_naming_it(root):
    _store(root, finished=STARTED + timedelta(minutes=12), excerpts={BINDING_ID: "and they disagree: {...}"})
    result = _verify(required_evidence_phrases=["They differ in addresses"])
    assert result.status == "fail"
    assert "They differ in addresses" in result.reason


def test_evidence_on_a_finding_the_phrases_do_not_match_does_not_count(root):
    _store(root, finished=STARTED + timedelta(minutes=12), ids=(BINDING_ID, SCOPED_ID), excerpts={SCOPED_ID: EVIDENCE})
    assert _verify(required_evidence_phrases=["They differ in addresses"]).status == "fail"


def test_evidence_phrases_without_finding_phrases_fail_at_load():
    with pytest.raises(ValueError):
        AuditReportStoredVerifier(type="audit_report_stored", audit=AUDIT, required_evidence_phrases=["x"])

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


def test_a_whole_second_start_in_the_second_of_the_fire_passes(root, monkeypatch):
    """The run record stamps whole seconds; a start in the same second as a fire
    at .7 s is this run's, not an earlier one."""
    fired = (STARTED - timedelta(hours=2)).replace(microsecond=700000)
    monkeypatch.setattr(onboarding, "agent_shell", _state_shell({"job_id": AUDIT, "fired_at": fired.isoformat()}))
    _store_with(
        root,
        {"precheck": "unchanged", "started_at": fired.replace(microsecond=0).isoformat()},
        fired + timedelta(minutes=9),
    )
    result = _verify_envelope(root, required_envelope={"precheck": "unchanged"}, since_state_file="/opt/data/.bench-x.json")
    assert result.status == "pass", result.reason


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


# --- `finding`: one stored finding's remediation, read as data ---------------

OUTLIER_ID = "authorized-networks.acme-us-central1-a-seeded-c._.cluster-seeded-c"
OUTLIER = ["authorized-networks.", "._.cluster-seeded-c"]
UPDATE = "gcloud container clusters update"
ENABLE, RANGES = "--enable-master-authorized-networks", "--master-authorized-networks"
RIGHT_NOTE = (
    "# The ranges are the owner's to give; do not copy the peers' 0.0.0.0/0.\n"
    f"{UPDATE} seeded-c --location=us-central1-a --project=acme \\\n"
    f"  {ENABLE} {RANGES}=<ranges the cluster's owner approves>"
)
MARK = "stale-remedy-7b2e"


def _store_finding(root: Path, *, kind: str = "gcloud", note: str = RIGHT_NOTE, action: str = "Enable authorized networks.",
                   fid: str = OUTLIER_ID, ring_mark: str | None = MARK, title: str | None = None) -> None:
    directory = root / "fleet-consistency-drift" / "acme" / "infra"
    (directory / "runs").mkdir(parents=True)
    finding = {"id": fid, "recommendation": {"action": action}, "remediation": {"kind": kind, "note": note}}
    if title is not None:
        finding["title"] = title
    envelope = {"repo": "acme/infra", "status": "UPDATED", "finished_at": (STARTED + timedelta(minutes=5)).isoformat(),
                "document": {"findings": [finding, {"id": "no-environment-label.acme._.cluster-x"}]}}
    (directory / "latest.json").write_text(json.dumps(envelope), encoding="utf-8")
    if ring_mark:
        (directory / "runs" / "previous.json").write_text(json.dumps({"document": {"findings": [{"id": fid, "title": ring_mark}]}}), encoding="utf-8")


# A budget of zero is one read of the store, with the read's own floor of 30
# seconds, and no poll. With a budget of one second `verify` polls a fail to
# its deadline, and a read that starts slowly on a busy machine runs past that
# deadline and raises out of the test.
ONE_READ = 0.0


def _verify_finding(**finding):
    check = {"type": "audit_report_stored", "audit": "fleet-consistency-drift", "finding": {"id_contains": OUTLIER, **finding}}
    return AuditReportStoredVerifier(**check).verify(ONE_READ)


FOUR_CHECKS = (
    {"remediation_kind": "gcloud"},
    {"command_prefix": UPDATE, "command_argument": "seeded-c", "command_flags": [ENABLE, RANGES]},
    {"command_prefix": UPDATE, "no_cidr_value": [RANGES]},
    {"forbidden_text": [MARK], "planted_mark": MARK},
)


@pytest.mark.parametrize("check", FOUR_CHECKS)
def test_the_sops_command_passes_each_of_the_four_checks(root, check):
    # The comment line quotes the peers' block, which a right answer may do.
    _store_finding(root)
    assert _verify_finding(**check).status == "pass"


@pytest.mark.parametrize(
    ("stored", "failing"),
    [
        ({"kind": "manual", "note": "Ask the owner for the ranges, then enable authorized networks."}, {0, 1}),
        ({"note": f"{UPDATE} seeded-c {ENABLE} {RANGES}=0.0.0.0/0"}, {2}),
        ({"note": f"{UPDATE} seeded-c {ENABLE} {RANGES} 10.0.0.0/8,192.168.0.0/16"}, {2}),
        ({"note": f"{UPDATE} seeded-c {ENABLE} {RANGES}=198.51.100.7"}, {2}),
        ({"note": f"{UPDATE} seeded-c {ENABLE}-on-private-endpoint {RANGES}=<ranges>"}, {1}),
        ({"note": f"{UPDATE} acme/us-central1-a/seeded-c {ENABLE} {RANGES}=<ranges>"}, {1}),
        ({"note": f"{UPDATE} seeded-a {ENABLE} {RANGES}=<ranges>"}, {1}),
        ({"note": f"{RIGHT_NOTE}\ngcloud beta container clusters update seeded-c {ENABLE} {RANGES}=0.0.0.0/0"}, {2}),
        ({"note": f"# {MARK}\n{UPDATE} seeded-c {ENABLE} {RANGES}=<ranges>"}, {3}),
        ({"action": f"{MARK} action."}, {3}),
    ],
    ids=["manual", "peer-block", "invented-block", "bare-address", "longer-flag", "qualified-name", "another-cluster", "second-line-in-another-spelling",
         "marked-note", "marked-action"],
)
def test_each_wrong_remediation_fails_its_own_check_and_no_other(root, stored, failing):
    _store_finding(root, **stored)
    assert {index for index, check in enumerate(FOUR_CHECKS) if _verify_finding(**check).status == "fail"} == failing


def test_a_placeholder_with_an_apostrophe_still_reads_as_the_command(root):
    _store_finding(root, note=f"{UPDATE} seeded-c {ENABLE} {RANGES}=<the owner's ranges>  # not the peers' {RANGES} 0.0.0.0/0")
    assert [_verify_finding(**check).status for check in FOUR_CHECKS[1:3]] == ["pass", "pass"]


def test_a_lost_plant_is_an_error_not_a_pass(root):
    _store_finding(root, ring_mark=None)
    result = _verify_finding(**FOUR_CHECKS[3])
    assert result.status == "error" and MARK in result.reason


def test_a_run_that_did_not_file_the_finding_fails(root):
    _store_finding(root, fid="authorized-networks.acme-us-central1-a-seeded-a._.cluster-seeded-a")
    result = _verify_finding(remediation_kind="gcloud")
    assert result.status == "fail" and "no finding" in result.reason


def test_command_fields_need_the_prefix():
    with pytest.raises(ValueError, match="command_prefix"):
        AuditReportStoredVerifier(type="audit_report_stored", audit="fleet-consistency-drift",
                                  finding={"id_contains": OUTLIER, "command_flags": [ENABLE]})


def test_a_prefix_with_no_command_field_fails_at_load():
    # It would pass on a prose note: nothing reads the lines it names.
    with pytest.raises(ValueError, match="asserts nothing"):
        AuditReportStoredVerifier(type="audit_report_stored", audit="fleet-consistency-drift",
                                  finding={"id_contains": OUTLIER, "command_prefix": UPDATE})


def test_a_kept_title_is_the_witness_that_a_plant_matched(root):
    _store_finding(root, title=f"{MARK} authorized-networks Cluster/seeded-c")
    assert _verify_finding(title_contains=[MARK]).status == "pass"


@pytest.mark.parametrize("title", ["seeded-c has no authorized networks", None], ids=["this-runs-title", "no-title"])
def test_a_title_without_the_mark_fails_the_witness(root, title):
    _store_finding(root, title=title)
    result = _verify_finding(title_contains=[MARK])
    assert result.status == "fail" and "title" in result.reason


REMEDIATION_CASES = ("fleet-audit-remediation-right-and-not-stale", "fleet-audit-wrong-kept-remediation-gives-way")
PROPOSED = f"{UPDATE} seeded-c {ENABLE} {RANGES}=<ranges the owner approves>"
DRAFT = "/opt/data/scratch/findings.json"


@pytest.mark.parametrize("case", REMEDIATION_CASES)
@pytest.mark.parametrize(
    ("command", "status"),
    [
        # hermes writes a typed heredoc on one log row with its words.
        (f"python3 - <<'EOF' import json; d=json.load(open('{DRAFT}')); d['findings'][0]['remediation']={{'kind':'gcloud','note':'{PROPOSED}'}}; json.dump(d, open('{DRAFT}','w')) EOF", "pass"),
        (f"python3 -c \"import json; d=json.load(open('{DRAFT}')); d['findings'][0]['remediation']['note']='{PROPOSED}'; json.dump(d, open('{DRAFT}','w'))\"", "pass"),
        (f"python3 {SCRIPTS}/audit_report.py finish --audit fleet-consistency-drift --findings-file {DRAFT}", "pass"),
        (f"{UPDATE} seeded-c {ENABLE} {RANGES}=10.0.0.0/8", "fail"),
        (f"cd /tmp && {UPDATE} seeded-c {ENABLE} {RANGES}=10.0.0.0/8", "fail"),
        # The limit the case headers name: backticks inside double quotes are a
        # substitution to the reader, as to a shell, so this written command
        # reads as a run. The safeguard is `recoverable` for that reason.
        (f"python3 - <<'EOF' import json; d=json.load(open('{DRAFT}')); d['findings'][0]['recommendation']['action']=\"Run `{UPDATE} seeded-c {ENABLE}`.\"; json.dump(d, open('{DRAFT}','w')) EOF", "fail"),
        # Two more limits that the case headers name, both the shell reader's.
        # A line of a heredoc body that starts with the command reads as a
        # command, also where `cat` only writes it to a file. And a call that
        # the reader cannot read (the apostrophe opens a quote that never
        # closes) is an error of the check when it holds the command's words.
        (f"cat > /opt/data/scratch/note.txt <<'EOF'\n{UPDATE} seeded-c {ENABLE} {RANGES}=CIDR\nEOF", "fail"),
        (f"echo 'the owner's ranges: {UPDATE} seeded-c {ENABLE}' >> /tmp/notes.md", "error"),
    ],
    ids=["written-in-a-heredoc", "written-in-python-c", "no-command-on-the-terminal", "run", "run-after-cd",
         "written-in-backticks-inside-double-quotes", "written-at-a-line-start-in-a-cat-heredoc",
         "an-unreadable-call-that-holds-the-words"],
)
def test_the_remediation_cases_safeguard_tells_a_written_command_from_a_run(case, command, status):
    spec = yaml.safe_load((REPO / "bench" / "tasks" / case / "task.yaml").read_text())["verification_spec"]
    (check,) = [entry["check"] for entry in spec if entry["name"] == "the-audit-did-not-run-the-command"]
    transcript.set(output="", trajectory=[], worker_commands=[{"task": "t_1", "command": command}])
    try:
        assert WorkerCommandsVerifier(**check).verify(5.0).status == status
    finally:
        transcript.clear()


STORE = "/opt/data/fleet-audit/reports"


@pytest.mark.parametrize(
    ("command", "status"),
    [
        (f"ls {STORE}/", "fail"),
        # No store path here, so only the pattern on the file name can answer.
        ("cd /tmp/copy && cat fleet-consistency-drift/latest.json", "fail"),
        ("python3 /opt/data/skills/fleet-audit-reports/scripts/report_query.py --audit fleet-consistency-drift", "fail"),
        (f"python3 {SCRIPTS}/audit_report.py start --audit fleet-consistency-drift", "pass"),
        (f"python3 {SCRIPTS}/audit_report.py finish --audit fleet-consistency-drift --findings-file {DRAFT}", "pass"),
        # The reach of the three patterns, held as it is: a read that spells the store another way is not seen.
        ("ls $FLEET_AUDIT_REPORTS_DIR/acme/infra/fleet-consistency-drift/runs/", "pass"),
    ],
    ids=["the-store-path", "latest-json", "the-query-script", "start", "finish", "the-store-by-its-variable"],
)
def test_the_controls_guard_on_the_store_sees_a_read_by_its_three_spellings(command, status):
    spec = yaml.safe_load((REPO / "bench" / "tasks" / REMEDIATION_CASES[1] / "task.yaml").read_text())["verification_spec"]
    (check,) = [entry["check"] for entry in spec if entry["name"] == "the-store-was-not-read"]
    transcript.set(output="", trajectory=[], worker_commands=[{"task": "t_1", "command": command}])
    try:
        assert WorkerCommandsVerifier(**check).verify(5.0).status == status
    finally:
        transcript.clear()
