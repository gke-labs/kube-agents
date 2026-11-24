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

"""The shadow-run checks: ``shadow_report_stored``, ``findings_queue_untouched``, ``schedule_report_not_posted``.

The shadow read runs its driver for real through ``sh -c``, over a store in a
temporary directory, as it does in the gateway pod. The reader it calls is a
stand-in with report_query.py's command line and answers, which refuses a
shadow envelope without ``--include-shadow`` the way dev-5's reader change
does; one test runs the repository's own report_query.py, which on a tree
without that change refuses the flag. The findings queue is the real
``findings_queue`` module on an in-memory SQLite database, its rows handed back
in the shape the Session KV read prints; the AgentSchedule is a dict in the
shape ``kubectl get -o json`` prints. Only the pod shells and the kubectl read
are swapped for local ones.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sqlite3
import subprocess
import sys
import threading
import tomllib
import typing
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from devops_bench.verification.spec import parse_node
from pydantic import ValidationError

from kube_agents_bench import findings, onboarding, oobe, shadow_run, transcript, verifiers
from kube_agents_bench.shadow_run import (
    FindingsQueueUntouchedVerifier,
    ScheduleReportNotPostedVerifier,
    ShadowReportStoredVerifier,
)
from kube_agents_bench.verifiers import AuditReportStoredVerifier

REPO = Path(__file__).resolve().parents[2]
AUDIT_REPORT = REPO / "agents" / "platform" / "skills" / "fleet-audit" / "scripts" / "audit_report.py"
REPORT_QUERY = REPO / "agents" / "platform" / "skills" / "fleet-audit-reports" / "scripts" / "report_query.py"
INSTALL_SCRIPTS = REPO / "agents" / "platform" / "scripts"
SCHEDULE_TYPES = REPO / "k8s-operator" / "api" / "v1alpha1" / "agentschedule_types.go"
FINDINGS_ROWS = REPO / "bench" / "tf" / "prebuilt" / "findings-item" / "findings.json"
AUDIT = "compliance-audit"
CANARY = "compliance-audit-canary"
# The install's own store key, the one a profile may write under.
INSTALL_KEY = ("acme", "fleet-gitops")
STATE_FILE = "/opt/data/.bench-builtin-schedule-run-now.json"
STARTED = datetime(2026, 11, 23, 18, 0, tzinfo=timezone.utc)
FIRED = STARTED - timedelta(hours=1)
BINDING_ID = "cluster-admin-binding.seeded-a._.clusterrolebinding-debug-binding"
TASK_ID = "task-canary-1"

_spec = importlib.util.spec_from_file_location(
    "findings_queue", REPO / "agents" / "platform" / "scripts" / "findings_queue.py"
)
fq = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("findings_queue", fq)
_spec.loader.exec_module(fq)

# report_query.py's command line and answers, over one stream's tree: `show`
# (the envelope without its document), `findings` (ids, cut at --limit) and
# `finding` (one whole), under the key --repo names, of latest or of the ring
# entry --run names. A shadow envelope is refused without --include-shadow, as
# readers do once dev-5's reader change lands.
FAKE_READER = r"""
import argparse, json, os, sys
parser = argparse.ArgumentParser()
parser.add_argument("command")
parser.add_argument("--root")
parser.add_argument("--repo")
parser.add_argument("--run")
parser.add_argument("--limit", type=int, default=100)
parser.add_argument("--include-shadow", action="store_true")
parser.add_argument("positionals", nargs="*")
args = parser.parse_args()
stream = args.positionals[0]
def answer(payload):
    print(json.dumps(payload))
    sys.exit(2 if payload.get("error") else 0)
base = os.path.join(args.root, stream, *args.repo.lower().split("/"))
if not os.path.isdir(os.path.join(args.root, stream)):
    answer({"error": "%s has no run stored" % stream})
if not os.path.isdir(base):
    answer({"error": "no reports for %s in %s" % (stream, args.repo.lower())})
path = os.path.join(base, "runs", args.run + ".json") if args.run else os.path.join(base, "latest.json")
if not os.path.exists(path):
    answer({"error": "%s has no run %r for %s" % (stream, args.run, args.repo)})
envelope = json.load(open(path))
if envelope.get("shadow") is True and not args.include_shadow:
    answer({"error": "%s/latest.json is a shadow run's report" % stream, "shadow": True})
document = envelope.get("document") or {}
findings = document.get("findings") or []
if args.command == "show":
    answer({"repo": args.repo, "envelope": {k: v for k, v in envelope.items() if k != "document"}, "error": None})
if args.command == "findings":
    answer({"repo": args.repo, "findings": [{"id": f.get("id")} for f in findings[: args.limit]],
            "truncated": len(findings) > args.limit, "error": None})
if args.command == "finding":
    for finding in findings:
        if finding.get("id") == args.positionals[1]:
            answer({"repo": args.repo, "finding": finding, "error": None})
    answer({"error": "no finding %s" % args.positionals[1]})
"""


def _local_shell(script: str, timeout: float) -> str:
    proc = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=timeout, check=False)
    return proc.stdout if proc.returncode == 0 else ""


def _silent_shell(script: str, timeout: float) -> str:
    return ""


def _fired_state(**extra: Any) -> dict[str, Any]:
    return {"schedule": CANARY, "token": "bench-run-now-1-ab", "fired_at": FIRED.isoformat(), **extra}


def _gateway(state: dict[str, Any] | None, queue: list[dict[str, Any]] | None = None, queue_error: str | None = None):
    """The gateway's agent container: the state file, the findings read, and the store read run locally."""

    def shell(script: str, timeout: float) -> str:
        if script.startswith("cat "):
            return json.dumps(state) if state is not None else ""
        if findings.FINDINGS_READ in script:
            if queue is None and queue_error is None:
                return ""
            return findings.FINDINGS_READ + "\n" + json.dumps({"findings": queue, "error": queue_error}) + "\n"
        return _local_shell(script, timeout)

    return shell


def _register(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repos: list[tuple[str, str]], gitops: tuple[str, str] | None = None) -> None:
    """The gitops-state ConfigMap's managed_repos (and gitops_repo) as files, where gitops_workspace reads them off-pod."""
    state = tmp_path / "gitops"
    state.mkdir(exist_ok=True)
    entries = [{"type": "github", "url": f"https://github.com/{owner}/{name}"} for owner, name in repos]
    (state / "managed_repos").write_text(json.dumps(entries), encoding="utf-8")
    key = state / "gitops_repo"
    if gitops is not None:
        key.write_text(json.dumps({"type": "github", "url": f"https://github.com/{gitops[0]}/{gitops[1]}"}), encoding="utf-8")
    elif key.exists():
        key.unlink()
    monkeypatch.setenv("GITOPS_STATE_PATH", str(state / "managed_repos"))


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    store = tmp_path / "reports"
    store.mkdir()
    reader = tmp_path / "report_query.py"
    reader.write_text(FAKE_READER, encoding="utf-8")
    monkeypatch.setattr(verifiers, "REPORT_STORE_ROOT", str(store))
    monkeypatch.setattr(shadow_run, "READER_PATH", str(reader))
    monkeypatch.setattr(shadow_run, "READER_HELPERS_DIR", str(REPORT_QUERY.parents[2] / "fleet-audit" / "scripts"))
    # The install's managed repositories, read by the image's own gitops_workspace.
    monkeypatch.setattr(shadow_run, "INSTALL_SCRIPTS_DIR", str(INSTALL_SCRIPTS))
    _register(tmp_path, monkeypatch, [INSTALL_KEY])
    monkeypatch.setattr(oobe, "agent_shell", _gateway(_fired_state(task_id=TASK_ID)))
    # The roster check's pods hold nothing here: the shadow read is the gateway's alone.
    monkeypatch.setattr(onboarding, "sandbox_shell", _silent_shell)
    monkeypatch.setattr(onboarding, "agent_shell", _silent_shell)
    transcript.set(output="", trajectory=[], started_at=STARTED.timestamp())
    yield store
    transcript.clear()


def _shadow_envelope(stream: str, **overrides: Any) -> dict[str, Any]:
    envelope = {
        "audit_id": AUDIT,
        "repo": "/".join(INSTALL_KEY),
        "status": "NO_FORGE",
        "finished_at": (FIRED + timedelta(minutes=20)).isoformat(),
        "started_at": (FIRED + timedelta(minutes=2)).replace(microsecond=0).isoformat(),
        "current_ids": [BINDING_ID],
        "document": {"findings": [{"id": BINDING_ID, "evidence": {"excerpt": "roleRef: cluster-admin"}}]},
        "shadow": True,
        "shadow_stream": stream,
        "shadow_summary": "1 finding: the default ServiceAccount is bound to cluster-admin",
    }
    envelope.update(overrides)
    return envelope


_ring_clock = {"n": 0}


def _store(
    root: Path,
    stream: str,
    envelope: dict[str, Any],
    key: tuple[str, str] = INSTALL_KEY,
    task: str | None = TASK_ID,
    sidecar_digest: str | None = None,
) -> str:
    """latest.json, and a ring entry with the writer sidecar the broker's listener keeps for a profile task's write."""
    directory = root / stream / key[0] / key[1]
    (directory / "runs").mkdir(parents=True, exist_ok=True)
    data = json.dumps(envelope).encode("utf-8")
    (directory / "latest.json").write_bytes(data)
    _ring_clock["n"] += 1
    stamp = (FIRED + timedelta(minutes=20, microseconds=_ring_clock["n"])).strftime("%Y%m%dT%H%M%S.%fZ")
    (directory / "runs" / f"{stamp}.json").write_bytes(data)
    if task is not None:
        sidecar = {"writer": f"builtin-schedule:{stream}:{task}", "sha256": sidecar_digest or hashlib.sha256(data).hexdigest()}
        (directory / "runs" / f"{stamp}.writer").write_text(json.dumps(sidecar), encoding="ascii")
    return stamp


def _shadow(**fields: Any):
    check = {
        "type": "shadow_report_stored",
        "audit": AUDIT,
        "stream": CANARY,
        "since_state_file": STATE_FILE,
        "required_finding_phrases": ["debug-binding"],
        **fields,
    }
    return ShadowReportStoredVerifier(**check).verify(1.0)


# ---------------------------------------------------------------- shadow_report_stored


def test_a_canarys_shadow_report_on_its_schedules_stream_passes(root):
    _store(root, CANARY, _shadow_envelope(CANARY))
    result = _shadow()
    assert result.status == "pass", result.reason
    assert result.raw["stored"][0]["repo"] == "/".join(INSTALL_KEY)


def test_a_hand_run_names_the_audit_shadow_stream(root):
    _store(root, f"{AUDIT}-shadow", _shadow_envelope(f"{AUDIT}-shadow"))
    assert _shadow(stream=f"{AUDIT}-shadow").status == "pass"
    # And the canary's stream, which holds nothing, is not it.
    result = _shadow()
    assert result.status == "fail"
    assert f"no ring entry under acme/fleet-gitops was written by task {TASK_ID}" in result.reason


def test_the_read_passes_the_include_shadow_opt_in(root, monkeypatch):
    seen: list[str] = []

    def shell(script: str, timeout: float) -> str:
        if script.startswith("cat "):
            return json.dumps(_fired_state(task_id=TASK_ID))
        seen.append(script)
        return _local_shell(script, timeout)

    monkeypatch.setattr(oobe, "agent_shell", shell)
    _store(root, CANARY, _shadow_envelope(CANARY))
    assert _shadow().status == "pass"
    assert f" {shadow_run.INCLUDE_SHADOW_FLAG} " in seen[0]
    assert shadow_run.READER_PATH in seen[0]
    assert verifiers.REPORT_STORE_ROOT in seen[0]


def test_without_the_opt_in_the_reader_refuses_the_shadow_envelope(root, monkeypatch):
    """What the flag buys: a reader asked without it answers a refusal, which grades as no report."""
    # A flag the reader takes that is not the opt-in.
    monkeypatch.setattr(shadow_run, "INCLUDE_SHADOW_FLAG", "--limit=100")
    _store(root, CANARY, _shadow_envelope(CANARY))
    result = _shadow()
    assert result.status == "fail"
    assert "shadow run's report" in result.reason


def test_the_evidence_is_read_from_the_whole_finding(root):
    _store(root, CANARY, _shadow_envelope(CANARY))
    assert _shadow(required_evidence_phrases=["cluster-admin"]).status == "pass"
    result = _shadow(required_evidence_phrases=["system:masters"])
    assert result.status == "fail"
    assert "evidence" in result.reason


def test_the_shadow_read_is_the_gateway_pods_store_alone(root, monkeypatch):
    """The broker's listener writes on the gateway pod; a copy in the sandbox is not that write."""
    _store(root, CANARY, _shadow_envelope(CANARY))

    def no_store(script: str, timeout: float) -> str:
        return json.dumps(_fired_state(task_id=TASK_ID)) if script.startswith("cat ") else ""

    monkeypatch.setattr(oobe, "agent_shell", no_store)
    monkeypatch.setattr(onboarding, "sandbox_shell", _local_shell)
    monkeypatch.setattr(onboarding, "agent_shell", _local_shell)
    result = _shadow()
    assert result.status == "error"
    assert "the gateway's agent container could not be read" in result.reason


def test_a_reader_that_refuses_its_arguments_is_an_error_naming_it(root, tmp_path, monkeypatch):
    reader = tmp_path / "old_reader.py"
    reader.write_text("import sys\nsys.stderr.write('report_query.py: error: unrecognized arguments: --include-shadow\\n')\nsys.exit(2)\n")
    monkeypatch.setattr(shadow_run, "READER_PATH", str(reader))
    _store(root, CANARY, _shadow_envelope(CANARY))
    result = _shadow()
    assert result.status == "error"
    assert "unrecognized arguments: --include-shadow" in result.reason


def test_a_document_under_another_key_fails(root):
    """The broker lets a profile write under the install's key alone: a document under any other is not this run's."""
    _store(root, CANARY, _shadow_envelope(CANARY), key=("other-org", "other-repo"))
    result = _shadow()
    assert result.status == "fail"
    assert f"no ring entry under acme/fleet-gitops was written by task {TASK_ID}" in result.reason


def test_the_install_keys_document_is_read_beside_another_keys(root):
    _store(root, CANARY, _shadow_envelope(CANARY, document={"findings": [{"id": "elsewhere"}]}), key=("other-org", "other-repo"))
    _store(root, CANARY, _shadow_envelope(CANARY))
    result = _shadow()
    assert result.status == "pass", result.reason
    assert [entry["repo"] for entry in result.raw["stored"]] == ["acme/fleet-gitops"]


def test_the_read_names_the_install_key_with_repo(root, monkeypatch):
    seen: list[str] = []

    def shell(script: str, timeout: float) -> str:
        if script.startswith("cat "):
            return json.dumps(_fired_state(task_id=TASK_ID))
        seen.append(script)
        return _local_shell(script, timeout)

    monkeypatch.setattr(oobe, "agent_shell", shell)
    _store(root, CANARY, _shadow_envelope(CANARY))
    assert _shadow().status == "pass"
    assert shadow_run.INSTALL_SCRIPTS_DIR in seen[0]


def test_an_install_with_no_repository_reads_the_no_forge_key(root, tmp_path, monkeypatch):
    _register(tmp_path, monkeypatch, [])
    _store(root, CANARY, _shadow_envelope(CANARY), key=tuple(shadow_run.NO_FORGE_REPO.split("/")))
    assert _shadow().status == "pass"
    # And a document under a repository's key is not it.
    _register(tmp_path, monkeypatch, [])
    (root / CANARY / "no_forge").rename(root / "elsewhere")
    _store(root, CANARY, _shadow_envelope(CANARY))
    assert _shadow().status == "fail"


def test_with_several_repositories_the_gitops_one_is_the_key(root, tmp_path, monkeypatch):
    second = ("acme", "apps")
    _register(tmp_path, monkeypatch, [INSTALL_KEY, second], gitops=second)
    _store(root, CANARY, _shadow_envelope(CANARY))
    assert _shadow().status == "fail"
    _store(root, CANARY, _shadow_envelope(CANARY), key=second)
    assert _shadow().status == "pass"


def test_with_several_repositories_and_no_gitops_one_the_key_is_no_forge(root, tmp_path, monkeypatch):
    _register(tmp_path, monkeypatch, [INSTALL_KEY, ("acme", "apps")])
    _store(root, CANARY, _shadow_envelope(CANARY))
    assert _shadow().status == "fail"
    _store(root, CANARY, _shadow_envelope(CANARY), key=tuple(shadow_run.NO_FORGE_REPO.split("/")))
    assert _shadow().status == "pass"


def test_an_install_key_that_cannot_be_read_is_an_error(root, tmp_path, monkeypatch):
    empty = tmp_path / "no-scripts"
    empty.mkdir()
    monkeypatch.setattr(shadow_run, "INSTALL_SCRIPTS_DIR", str(empty))
    _store(root, CANARY, _shadow_envelope(CANARY))
    result = _shadow()
    assert result.status == "error"
    assert "the install's store key could not be read" in result.reason


def test_the_install_scripts_dir_holds_gitops_workspace_in_the_image():
    dockerfile = (REPO / "deploy" / "docker" / "Dockerfile").read_text()
    assert re.search(r"^COPY .*agents/platform/scripts/ .*" + re.escape(shadow_run.INSTALL_SCRIPTS_DIR) + "/", dockerfile, re.M)
    assert (INSTALL_SCRIPTS / "gitops_workspace.py").exists()


def test_the_no_forge_key_is_audit_reports():
    assert re.search(r'^NO_FORGE_REPO = "([^"]+)"', AUDIT_REPORT.read_text(), re.M).group(1) == shadow_run.NO_FORGE_REPO


def test_a_later_run_of_the_schedule_does_not_mask_the_fired_one(root):
    """The newest envelope is a later task's; the fired task's own ring entry is graded."""
    _store(root, CANARY, _shadow_envelope(CANARY))
    _store(root, CANARY, _shadow_envelope(CANARY, document={"findings": [{"id": "nothing-planted"}]}), task="task-later")
    result = _shadow()
    assert result.status == "pass", result.reason


def test_a_later_good_run_does_not_pass_for_a_bad_fired_one(root):
    _store(root, CANARY, _shadow_envelope(CANARY, document={"findings": [{"id": "nothing-planted"}]}))
    _store(root, CANARY, _shadow_envelope(CANARY), task="task-later")
    result = _shadow()
    assert result.status == "fail"
    assert "debug-binding" in result.reason


def test_no_ring_entry_by_the_fired_task_fails(root):
    _store(root, CANARY, _shadow_envelope(CANARY), task="task-other")
    result = _shadow()
    assert result.status == "fail"
    assert f"no ring entry under acme/fleet-gitops was written by task {TASK_ID}" in result.reason


def test_an_unstamped_ring_entry_is_not_the_fired_tasks(root):
    _store(root, CANARY, _shadow_envelope(CANARY), task=None)
    assert _shadow().status == "fail"


def test_a_sidecar_for_other_bytes_does_not_vouch_for_the_entry(root):
    _store(root, CANARY, _shadow_envelope(CANARY), sidecar_digest="0" * 64)
    assert _shadow().status == "fail"


def test_a_state_file_with_no_task_is_an_error(root, monkeypatch):
    monkeypatch.setattr(oobe, "agent_shell", _gateway(_fired_state()))
    _store(root, CANARY, _shadow_envelope(CANARY))
    result = _shadow()
    assert result.status == "error"
    assert "names no fired task" in result.reason


def test_without_a_state_file_the_newest_envelope_is_read(root):
    transcript.set(output="", trajectory=[], started_at=(FIRED + timedelta(minutes=1)).timestamp())
    _store(root, CANARY, _shadow_envelope(CANARY, document={"findings": [{"id": "nothing-planted"}]}))
    _store(root, CANARY, _shadow_envelope(CANARY), task="task-later")
    assert _shadow(since_state_file=None).status == "pass"


def test_a_truncated_findings_list_is_an_error(root, monkeypatch):
    monkeypatch.setattr(shadow_run, "READER_FINDINGS_LIMIT", 1)
    findings_list = [{"id": "first-finding"}, {"id": BINDING_ID}]
    _store(root, CANARY, _shadow_envelope(CANARY, document={"findings": findings_list}))
    result = _shadow()
    assert result.status == "error"
    assert "truncated at 1" in result.reason


def test_a_finding_id_that_looks_like_an_option_is_read_as_an_id(root):
    """Ids go after `--`, so one starting with a dash is not read as a flag."""
    odd = "--repo-debug-binding"
    envelope = _shadow_envelope(CANARY, document={"findings": [{"id": odd, "evidence": {"excerpt": "roleRef: cluster-admin"}}]})
    _store(root, CANARY, envelope)
    result = _shadow(required_evidence_phrases=["cluster-admin"])
    assert result.status == "pass", result.reason


def test_a_live_envelope_on_the_stream_fails_on_shadow(root):
    envelope = _shadow_envelope(CANARY)
    del envelope["shadow"]
    _store(root, CANARY, envelope)
    result = _shadow()
    assert result.status == "fail"
    assert "shadow is None, not True" in result.reason


def test_an_envelope_naming_another_stream_fails(root):
    _store(root, CANARY, _shadow_envelope(CANARY, shadow_stream="compliance-audit-shadow"))
    result = _shadow()
    assert result.status == "fail"
    assert "shadow_stream is 'compliance-audit-shadow'" in result.reason


def test_another_audits_shadow_envelope_fails(root):
    _store(root, CANARY, _shadow_envelope(CANARY, audit_id="ai-security-audit"))
    result = _shadow()
    assert result.status == "fail"
    assert "audit_id is 'ai-security-audit'" in result.reason


def test_the_named_finding_must_be_in_the_shadow_document(root):
    _store(root, CANARY, _shadow_envelope(CANARY, document={"findings": [{"id": "something-else"}]}))
    result = _shadow()
    assert result.status == "fail"
    assert "debug-binding" in result.reason


def test_a_shadow_report_from_before_the_fire_fails(root):
    _store(
        root,
        CANARY,
        _shadow_envelope(
            CANARY,
            finished_at=(FIRED - timedelta(minutes=5)).isoformat(),
            started_at=(FIRED - timedelta(minutes=20)).isoformat(),
        ),
    )
    result = _shadow()
    assert result.status == "fail"
    assert "since the stack fired the audit" in result.reason


def test_a_shadow_report_whose_run_opened_before_the_fire_fails(root):
    _store(root, CANARY, _shadow_envelope(CANARY, started_at=(FIRED - timedelta(minutes=1)).isoformat()))
    assert _shadow().status == "fail"


def test_without_a_fire_the_shadow_check_errors(root, monkeypatch):
    monkeypatch.setattr(oobe, "agent_shell", _gateway(None))
    _store(root, CANARY, _shadow_envelope(CANARY))
    result = _shadow()
    assert result.status == "error"
    assert "did not fire" in result.reason


def test_without_a_state_file_the_runs_start_is_the_bound(root):
    _store(root, CANARY, _shadow_envelope(CANARY))
    # The envelope finished an hour before the turn started.
    assert _shadow(since_state_file=None).status == "fail"


@pytest.mark.parametrize("stream", ["compliance-audit", "fleet-consistency-drift"])
def test_a_live_audits_stream_is_refused_at_load(stream):
    with pytest.raises(ValidationError, match="live audit's stream"):
        ShadowReportStoredVerifier(type="shadow_report_stored", audit=AUDIT, stream=stream, required_finding_phrases=["x"])


@pytest.mark.parametrize("stream", ["Compliance", "a_b", "-x", "x" * 64, "../reports"])
def test_a_stream_that_is_not_a_store_name_is_refused_at_load(stream):
    with pytest.raises(ValidationError, match="not a report-store stream name"):
        ShadowReportStoredVerifier(type="shadow_report_stored", audit=AUDIT, stream=stream, required_finding_phrases=["x"])


def test_the_stream_is_required():
    with pytest.raises(ValidationError):
        ShadowReportStoredVerifier(type="shadow_report_stored", audit=AUDIT, required_finding_phrases=["x"])


def test_the_audit_keeps_the_roster_rule():
    with pytest.raises(ValidationError):
        ShadowReportStoredVerifier(type="shadow_report_stored", audit=CANARY, stream=CANARY, required_finding_phrases=["x"])


def test_a_named_finding_is_required():
    with pytest.raises(ValidationError):
        ShadowReportStoredVerifier(type="shadow_report_stored", audit=AUDIT, stream=CANARY)
    with pytest.raises(ValidationError):
        ShadowReportStoredVerifier(type="shadow_report_stored", audit=AUDIT, stream=CANARY, required_finding_phrases=[])


@pytest.mark.parametrize("key", ["shadow", "shadow_stream", "audit_id"])
def test_required_envelope_may_not_restate_the_forced_keys(key):
    with pytest.raises(ValidationError, match="sets itself"):
        ShadowReportStoredVerifier(
            type="shadow_report_stored", audit=AUDIT, stream=CANARY, required_finding_phrases=["x"], required_envelope={key: False}
        )


def test_the_parsed_spec_is_the_shadow_check():
    node = parse_node(
        {"type": "shadow_report_stored", "audit": AUDIT, "stream": CANARY, "required_finding_phrases": ["debug-binding"]}
    )
    assert isinstance(node, ShadowReportStoredVerifier)


def test_the_roster_check_is_unchanged_for_the_live_path(root, monkeypatch):
    """audit_report_stored still takes only a roster id, and reads the live stream's tree in the sandbox and agent pods."""
    with pytest.raises(ValidationError):
        AuditReportStoredVerifier(type="audit_report_stored", audit=CANARY)
    with pytest.raises(ValidationError):
        AuditReportStoredVerifier(type="audit_report_stored", audit=f"{AUDIT}-shadow")
    # A shadow envelope on the shadow stream is not the live stream's report.
    _store(root, f"{AUDIT}-shadow", _shadow_envelope(f"{AUDIT}-shadow"))
    monkeypatch.setattr(onboarding, "sandbox_shell", _local_shell)
    monkeypatch.setattr(onboarding, "agent_shell", _gateway(_fired_state(task_id=TASK_ID)))
    live = AuditReportStoredVerifier(type="audit_report_stored", audit=AUDIT, since_state_file=STATE_FILE)
    assert live.verify(1.0).status == "fail"
    _store(root, AUDIT, _shadow_envelope(AUDIT, shadow=False, shadow_stream=None), key=("no_forge", "local"))
    assert live.verify(1.0).status == "pass"


def _reader_has_the_opt_in() -> bool:
    return shadow_run.INCLUDE_SHADOW_FLAG in REPORT_QUERY.read_text()


@pytest.mark.skipif(_reader_has_the_opt_in(), reason="this tree's report_query.py takes --include-shadow")
def test_this_trees_reader_refuses_the_opt_in_until_the_readers_change_lands(root, monkeypatch):
    """The dependency, stated: until report_query.py takes --include-shadow, the check errors rather than reads."""
    monkeypatch.setattr(shadow_run, "READER_PATH", str(REPORT_QUERY))
    _store(root, CANARY, _shadow_envelope(CANARY))
    result = _shadow()
    assert result.status == "error"
    assert "--include-shadow" in result.reason


@pytest.mark.skipif(not _reader_has_the_opt_in(), reason="needs report_query.py with --include-shadow (dev-5's reader change)")
def test_this_trees_reader_reads_the_canarys_stream(root, monkeypatch):
    monkeypatch.setattr(shadow_run, "READER_PATH", str(REPORT_QUERY))
    _store(root, CANARY, _shadow_envelope(CANARY))
    result = _shadow(required_evidence_phrases=["cluster-admin"])
    assert result.status == "pass", result.reason


def test_the_reader_path_is_in_the_image_beside_its_helpers():
    """The reader imports report_status from fleet-audit/scripts beside its own skill, as the image lays them out."""
    image_skills = "/opt/platform-template/skills/"
    assert shadow_run.READER_PATH.startswith(image_skills)
    repo_copy = REPO / "agents" / "platform" / "skills" / shadow_run.READER_PATH[len(image_skills):]
    assert repo_copy == REPORT_QUERY and repo_copy.exists()
    assert (REPORT_QUERY.parents[2] / "fleet-audit" / "scripts" / "report_status.py").exists()
    assert shadow_run.READER_HELPERS_DIR == image_skills + "fleet-audit/scripts"


def _defined(path: Path, name: str) -> str | None:
    match = re.search(rf'^{name} = (?:re\.compile\()?r?"([^"]*)"', path.read_text(), re.M)
    return match.group(1) if match else None


@pytest.mark.parametrize(
    ("path", "name", "value"),
    [
        (AUDIT_REPORT, "SHADOW_STREAM_SUFFIX", shadow_run.SHADOW_STREAM_SUFFIX),
        (AUDIT_REPORT, "SHADOW_KEY", shadow_run.SHADOW_KEY),
        (AUDIT_REPORT, "SHADOW_STREAM_KEY", shadow_run.SHADOW_STREAM_KEY),
        (AUDIT_REPORT, "SHADOW_STREAM_RE", shadow_run.SHADOW_STREAM_RE.pattern),
        (REPORT_QUERY, "INCLUDE_SHADOW_FLAG", shadow_run.INCLUDE_SHADOW_FLAG),
    ],
)
def test_the_shadow_constants_match_their_source(path, name, value):
    """Each copy matches where dev-5's store-only finish and reader change define it; skipped until they land."""
    defined = _defined(path, name)
    if defined is None:
        pytest.skip(f"{path.name} does not define {name} on this tree yet (dev-5's store-only finish or reader change)")
    assert defined == value


def test_the_writer_prefix_is_the_listeners():
    store = REPO / "agents" / "platform" / "scripts" / "audit_report_store.py"
    assert _defined(store, "REPORT_WRITER_BUILTIN") == shadow_run.REPORT_WRITER_BUILTIN
    assert _defined(store, "REPORT_WRITER_SUFFIX") == ".writer"


# ---------------------------------------------------------------- findings_queue_untouched

AUDIT_PROJECT = "bench-findings-demo"
AUDIT_CLUSTER = "bench-shop-east"


class _QueueServer:
    """GET /v1/findings over the real findings_queue on one SQLite file, behind the Session KV key check."""

    def __init__(self, path: Path) -> None:
        self.path = path
        conn = sqlite3.connect(path)
        fq.init_findings_schema(conn)
        conn.commit()
        conn.close()
        queue = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - the stdlib's name
                if self.headers.get("Authorization") != f"Bearer {API_KEY}":
                    self.send_response(401)
                    self.end_headers()
                    return
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                conn = sqlite3.connect(queue.path)
                try:
                    rows = fq.list_findings(conn, limit=int(query.get("limit", ["200"])[0]))
                finally:
                    conn.close()
                body = json.dumps({"findings": rows}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def write(self, operation, *args):
        conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            return operation(conn, *args)
        finally:
            conn.close()

    def sql(self, statement, *params):
        conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            conn.execute(statement, params)
        finally:
            conn.close()


API_KEY = "k-test"


def _rows(source: str = "audit") -> list[dict[str, Any]]:
    rows = [{k: v for k, v in row.items() if k != "plant_state"} for row in json.loads(FINDINGS_ROWS.read_text())]
    for row in rows:
        row["source"] = source
    return rows


@pytest.fixture
def queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root):
    served = _QueueServer(tmp_path / "kv.db")
    monkeypatch.setattr(findings, "SESSION_KV_URL", served.url)
    monkeypatch.setenv("SESSION_KV_API_KEY", API_KEY)
    audit = _rows("audit")
    watcher = _rows("event-watcher")[:1]
    watcher[0]["object"] = "watcher-only"
    served.write(fq.register_findings, audit + watcher)
    # Old stamps, so a write within the test's second still changes a row.
    served.sql("UPDATE findings SET first_seen = '2026-01-01 00:00:00', last_verified = '2026-01-01 00:00:00'")
    yield served
    served.server.shutdown()


def _snapshot() -> dict[str, Any]:
    snap = findings.read_snapshot(_local_shell, 30.0)
    assert snap is not None and snap["error"] is None, snap
    return snap


def _ids(served, source: str) -> list[str]:
    conn = sqlite3.connect(served.path)
    try:
        return [row[0] for row in conn.execute("SELECT id FROM findings WHERE source = ? ORDER BY id", (source,))]
    finally:
        conn.close()


def _untouched(monkeypatch, before: dict[str, Any] | None, **fields: Any):
    state = _fired_state(task_id=TASK_ID)
    if before is not None:
        state["findings_snapshot"] = before
    monkeypatch.setattr(oobe, "agent_shell", _gateway(state))
    check = {"type": "findings_queue_untouched", "since_state_file": STATE_FILE, "source": "audit", **fields}
    return FindingsQueueUntouchedVerifier(**check).verify(1.0)


def test_a_queue_nothing_wrote_passes(queue, monkeypatch):
    before = _snapshot()
    result = _untouched(monkeypatch, before)
    assert result.status == "pass", result.reason
    assert result.raw["rows"] == len(_rows())


def test_a_new_row_from_the_audit_fails(queue, monkeypatch):
    before = _snapshot()
    extra = _rows()[0] | {"object": "planted-new"}
    queue.write(fq.register_findings, [extra])
    result = _untouched(monkeypatch, before)
    assert result.status == "fail"
    assert "added" in result.reason


def test_a_registration_again_fails(queue, monkeypatch):
    before = _snapshot()
    queue.write(fq.register_findings, _rows()[:1])
    result = _untouched(monkeypatch, before)
    assert result.status == "fail"
    assert "changed" in result.reason


def test_a_scoped_registration_with_no_new_row_fails_on_its_absence_downgrade(queue, monkeypatch):
    """A complete claim for a cluster with an empty batch downgrades every row of it: a write with no new row."""
    before = _snapshot()
    queue.write(fq.register_findings, [], {"complete": True, "project": AUDIT_PROJECT, "cluster": AUDIT_CLUSTER, "source": "audit"})
    result = _untouched(monkeypatch, before)
    assert result.status == "fail"
    assert result.raw["changed"]
    assert not result.raw["added"]


def test_an_unverifiable_verification_fails(queue, monkeypatch):
    """It moves nothing forward in time (last_verified stays), but the row changes."""
    before = _snapshot()
    target = _ids(queue, "audit")[0]
    queue.write(fq.record_verification, target, "unverifiable", "the object is gone", None, True)
    result = _untouched(monkeypatch, before)
    assert result.status == "fail"
    assert target in result.raw["changed"]


def test_a_patch_fails(queue, monkeypatch):
    before = _snapshot()
    target = _ids(queue, "audit")[0]
    queue.write(fq.patch_finding, target, {"state": "dismissed"})
    result = _untouched(monkeypatch, before)
    assert result.status == "fail"
    assert target in result.raw["changed"]


def test_a_surfacing_mark_fails(queue, monkeypatch):
    before = _snapshot()
    target = _ids(queue, "audit")[0]
    queue.write(fq.mark_surfaced, target)
    result = _untouched(monkeypatch, before)
    assert result.status == "fail"
    assert target in result.raw["changed"]


def test_a_removed_row_fails(queue, monkeypatch):
    before = _snapshot()
    target = _ids(queue, "audit")[0]
    queue.sql("DELETE FROM findings WHERE id = ?", target)
    result = _untouched(monkeypatch, before)
    assert result.status == "fail"
    assert result.raw["removed"] == [target]


def test_another_writers_rows_are_not_read(queue, monkeypatch):
    """The event watcher writes on a live install while the run goes: not the audit's rows."""
    before = _snapshot()
    watcher = _ids(queue, "event-watcher")[0]
    queue.write(fq.mark_surfaced, watcher)
    queue.write(fq.register_findings, [_rows("event-watcher")[1] | {"object": "watcher-new"}])
    result = _untouched(monkeypatch, before)
    assert result.status == "pass", result.reason


def test_a_row_moved_into_the_audits_scope_fails(queue, monkeypatch):
    before = _snapshot()
    watcher = _ids(queue, "event-watcher")[0]
    queue.sql("UPDATE findings SET source = 'audit' WHERE id = ?", watcher)
    result = _untouched(monkeypatch, before)
    assert result.status == "fail"
    assert watcher in result.raw["changed"]


def _id_of_check(served, source: str, check: str) -> str:
    conn = sqlite3.connect(served.path)
    try:
        return conn.execute("SELECT id FROM findings WHERE source = ? AND check_slug = ?", (source, check)).fetchone()[0]
    finally:
        conn.close()


def test_check_prefix_narrows_the_scope(queue, monkeypatch):
    before = _snapshot()
    queue.write(fq.mark_surfaced, _id_of_check(queue, "audit", "no-memory-limit"))
    assert _untouched(monkeypatch, before, check_prefix="probes-").status == "pass"
    assert _untouched(monkeypatch, before, check_prefix="no-").status == "fail"
    assert _untouched(monkeypatch, before).status == "fail"


def test_a_state_file_without_a_snapshot_is_an_error(queue, monkeypatch):
    result = _untouched(monkeypatch, None)
    assert result.status == "error"
    assert "no findings-queue snapshot" in result.reason


def test_a_snapshot_at_the_cap_is_an_error(queue, monkeypatch):
    before = _snapshot()
    before["count"] = findings.ROW_LIMIT
    result = _untouched(monkeypatch, before)
    assert result.status == "error"
    assert "cap" in result.reason


def test_a_queue_that_cannot_be_read_now_is_an_error(queue, monkeypatch):
    before = _snapshot()
    monkeypatch.setenv("SESSION_KV_API_KEY", "wrong")
    result = _untouched(monkeypatch, before)
    assert result.status == "error"
    assert "could not be read" in result.reason


def test_the_source_and_family_are_checked_at_load():
    with pytest.raises(ValidationError):
        FindingsQueueUntouchedVerifier(type="findings_queue_untouched", since_state_file=STATE_FILE, source="cron")
    with pytest.raises(ValidationError, match="check family"):
        FindingsQueueUntouchedVerifier(type="findings_queue_untouched", since_state_file=STATE_FILE, source="audit", check_prefix="upgrade")
    with pytest.raises(ValidationError):
        FindingsQueueUntouchedVerifier(type="findings_queue_untouched", source="audit")


def test_the_sources_and_family_rule_are_the_queues():
    assert typing.get_args(shadow_run.FindingSource) == fq.SOURCES
    assert shadow_run.CHECK_FAMILY_RE.pattern == fq.CHECK_FAMILY_RE.pattern


def test_the_snapshot_reads_every_column_so_any_write_shows():
    assert "json.dumps(row, sort_keys=True" in findings.SNAPSHOT_SCRIPT


# ---------------------------------------------------------------- schedule_report_not_posted


def _schedule(*runs: dict[str, Any]) -> dict[str, Any]:
    return {"metadata": {"name": CANARY}, "status": {"recentRuns": list(runs)}}


def _run(task: str, delivery: str, slot: datetime = FIRED + timedelta(seconds=2), outcome: str = "Succeeded") -> dict[str, Any]:
    return {"taskID": task, "slotTime": slot.isoformat(), "outcome": outcome, "delivery": delivery}


def _not_posted(monkeypatch: pytest.MonkeyPatch, schedule, state=None, **fields: Any):
    monkeypatch.setattr(oobe, "agent_shell", _gateway(state if state is not None else _fired_state(task_id=TASK_ID)))
    monkeypatch.setattr(shadow_run, "read_schedule", lambda name, timeout: schedule)
    check = {"type": "schedule_report_not_posted", "schedule": CANARY, "since_state_file": STATE_FILE, **fields}
    return ScheduleReportNotPostedVerifier(**check).verify(1.0)


@pytest.mark.parametrize("delivery", ["local", "no_delivery"])
def test_a_run_that_posted_nothing_passes(root, monkeypatch, delivery):
    result = _not_posted(monkeypatch, _schedule(_run(TASK_ID, delivery)))
    assert result.status == "pass", result.reason


@pytest.mark.parametrize("delivery", ["posted", "post_failed", "post_unknown", "stored_no_home", "silent"])
def test_a_run_that_posted_or_asked_to_fails(root, monkeypatch, delivery):
    result = _not_posted(monkeypatch, _schedule(_run(TASK_ID, delivery)))
    assert result.status == "fail"
    assert delivery in result.reason


def test_only_the_fired_run_is_graded(root, monkeypatch):
    """An earlier run that posted is not the fired one."""
    schedule = _schedule(_run(TASK_ID, "local"), _run("task-earlier", "posted", slot=FIRED - timedelta(days=1)))
    assert _not_posted(monkeypatch, schedule).status == "pass"


def test_a_delivery_not_reported_yet_is_an_error(root, monkeypatch):
    result = _not_posted(monkeypatch, _schedule(_run(TASK_ID, "")))
    assert result.status == "error"
    assert "not reported yet" in result.reason


def test_the_fired_run_missing_from_the_ring_is_an_error(root, monkeypatch):
    result = _not_posted(monkeypatch, _schedule(_run("task-other", "local")))
    assert result.status == "error"
    assert TASK_ID in result.reason


def test_without_a_recorded_task_the_window_picks_the_runs(root, monkeypatch):
    schedule = _schedule(_run("task-new", "posted"), _run("task-old", "local", slot=FIRED - timedelta(days=1)))
    result = _not_posted(monkeypatch, schedule, state=_fired_state())
    assert result.status == "fail"
    assert "task-new" in result.reason
    assert "task-old" not in result.reason


def test_without_a_run_in_the_window_it_errors(root, monkeypatch):
    schedule = _schedule(_run("task-old", "local", slot=FIRED - timedelta(days=1)))
    assert _not_posted(monkeypatch, schedule, state=_fired_state()).status == "error"


def _not_posted_from_the_start(monkeypatch, schedule, **fields):
    """No state file: the bound is the run's start less the skew, taken once."""
    monkeypatch.setattr(shadow_run, "read_schedule", lambda name, timeout: schedule)
    check = {"type": "schedule_report_not_posted", "schedule": CANARY, **fields}
    return ScheduleReportNotPostedVerifier(**check).verify(1.0)


def test_without_a_state_file_the_skew_is_taken_once(root, monkeypatch):
    skew = timedelta(seconds=120)
    # Inside one skew of the start: graded.
    inside = _schedule(_run("task-inside", "posted", slot=STARTED - skew / 2))
    assert _not_posted_from_the_start(monkeypatch, inside).status == "fail"
    # Past one skew but inside two: not graded (a double subtraction would grade it).
    outside = _schedule(_run("task-outside", "posted", slot=STARTED - skew * 3 / 2))
    assert _not_posted_from_the_start(monkeypatch, outside).status == "error"


def test_without_a_state_file_a_run_before_the_start_inside_the_skew_counts(root, monkeypatch):
    """With no skew at all, a slot a minute before the runner's clock says the turn started would be missed."""
    schedule = _schedule(_run("task-early", "local", slot=STARTED - timedelta(seconds=60)))
    assert _not_posted_from_the_start(monkeypatch, schedule).status == "pass"


@pytest.mark.parametrize("name", ["Compliance", "a_b", "-x", "x" * 64, "a b", "--all"])
def test_the_schedule_name_is_checked_at_load(name):
    with pytest.raises(ValidationError, match="AgentSchedule name"):
        ScheduleReportNotPostedVerifier(type="schedule_report_not_posted", schedule=name)


def test_an_unreadable_schedule_is_an_error(root, monkeypatch):
    result = _not_posted(monkeypatch, None)
    assert result.status == "error"
    assert "could not be read" in result.reason


def test_the_schedule_read_is_kubectl_get_in_the_install_namespace(monkeypatch):
    calls: list[list[str]] = []

    class Done:
        returncode = 0
        stdout = json.dumps(_schedule(_run(TASK_ID, "local")))

    def run(cmd, **kwargs):
        calls.append(cmd)
        return Done()

    monkeypatch.setattr(shadow_run.subprocess, "run", run)
    monkeypatch.setenv("AGENT_NAMESPACE", "ka-ns")
    monkeypatch.setenv("AGENT_CLUSTER_CONTEXT", "ctx")
    schedule = shadow_run.read_schedule(CANARY, 5.0)
    assert schedule["status"]["recentRuns"][0]["taskID"] == TASK_ID
    assert calls == [["kubectl", "get", shadow_run.SCHEDULE_RESOURCE, CANARY, "-n", "ka-ns", "-o", "json", "--context", "ctx"]]


def test_the_delivery_sets_cover_the_status_enum():
    text = SCHEDULE_TYPES.read_text()
    enum = re.search(r"\+kubebuilder:validation:Enum=([a-z_;]+)\n(?:\s*//.*\n)*\s*Delivery string", text)
    assert enum is not None
    values = set(enum.group(1).split(";"))
    assert values == shadow_run.POSTING_DELIVERIES | shadow_run.QUIET_DELIVERIES
    assert not shadow_run.POSTING_DELIVERIES & shadow_run.QUIET_DELIVERIES


# ---------------------------------------------------------------- registration


@pytest.mark.parametrize(
    ("name", "target"),
    [
        ("shadow_report_stored", "kube_agents_bench.shadow_run:ShadowReportStoredVerifier"),
        ("findings_queue_untouched", "kube_agents_bench.shadow_run:FindingsQueueUntouchedVerifier"),
        ("schedule_report_not_posted", "kube_agents_bench.shadow_run:ScheduleReportNotPostedVerifier"),
    ],
)
def test_the_entry_points_name_the_checks(name, target):
    entry_points = tomllib.loads((REPO / "bench" / "pyproject.toml").read_text())["project"]["entry-points"]
    assert entry_points["devops_bench.verifiers"][name] == target


def test_each_check_parses_from_a_spec():
    for spec, cls in (
        ({"type": "findings_queue_untouched", "since_state_file": STATE_FILE, "source": "audit"}, FindingsQueueUntouchedVerifier),
        ({"type": "schedule_report_not_posted", "schedule": CANARY}, ScheduleReportNotPostedVerifier),
    ):
        assert isinstance(parse_node(spec), cls)
    assert typing.get_args(ShadowReportStoredVerifier.model_fields["audit"].annotation) == typing.get_args(verifiers.AuditId)


def test_the_shadow_check_reads_the_state_file_once_per_poll(root, monkeypatch):
    reads: list[str] = []
    inner = _gateway(_fired_state(task_id=TASK_ID))

    def shell(script: str, timeout: float) -> str:
        if script.startswith("cat "):
            reads.append(script)
        return inner(script, timeout)

    monkeypatch.setattr(oobe, "agent_shell", shell)
    _store(root, CANARY, _shadow_envelope(CANARY))
    assert _shadow().status == "pass"
    assert len(reads) == 1


def test_the_schedule_check_reads_the_state_file_once_per_poll(root, monkeypatch):
    reads: list[str] = []
    inner = _gateway(_fired_state(task_id=TASK_ID))

    def shell(script: str, timeout: float) -> str:
        if script.startswith("cat "):
            reads.append(script)
        return inner(script, timeout)

    monkeypatch.setattr(oobe, "agent_shell", shell)
    monkeypatch.setattr(shadow_run, "read_schedule", lambda name, timeout: _schedule(_run(TASK_ID, "local")))
    check = {"type": "schedule_report_not_posted", "schedule": CANARY, "since_state_file": STATE_FILE}
    assert ScheduleReportNotPostedVerifier(**check).verify(1.0).status == "pass"
    assert len(reads) == 1
