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

import importlib.util
import json
import re
import sqlite3
import subprocess
import sys
import tomllib
import typing
from datetime import datetime, timedelta, timezone
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
# (the envelope without its document), `findings` (ids) and `finding` (one
# whole), under the key --repo names. A shadow envelope is refused without
# --include-shadow, as readers do once dev-5's reader change lands.
FAKE_READER = r"""
import glob, json, os, sys
args = sys.argv[1:]
command, stream = args[0], args[1]
root = args[args.index("--root") + 1]
include = "--include-shadow" in args
paths = sorted(glob.glob(os.path.join(root, stream, "*", "*", "latest.json")))
def answer(payload):
    print(json.dumps(payload))
    sys.exit(2 if payload.get("error") else 0)
if "--repo" in args:
    named = args[args.index("--repo") + 1].lower()
    path = os.path.join(root, stream, *named.split("/"), "latest.json")
    if paths and path not in paths:
        answer({"error": "no reports for %s in %s" % (stream, named), "repos": ["/".join(p.split(os.sep)[-3:-1]) for p in paths]})
    paths = [path] if path in paths else []
if not paths:
    answer({"error": "%s has no run stored" % stream})
if len(paths) > 1:
    answer({"error": "%s publishes to several repositories; name one with --repo" % stream})
repo = "/".join(paths[0].split(os.sep)[-3:-1])
envelope = json.load(open(paths[0]))
if envelope.get("shadow") is True and not include:
    answer({"error": "%s/latest.json is a shadow run's report" % stream, "shadow": True})
document = envelope.get("document") or {}
if command == "show":
    answer({"repo": repo, "envelope": {k: v for k, v in envelope.items() if k != "document"}, "error": None})
if command == "findings":
    answer({"repo": repo, "findings": [{"id": f.get("id")} for f in document.get("findings") or []], "truncated": False, "error": None})
if command == "finding":
    for finding in document.get("findings") or []:
        if finding.get("id") == args[2]:
            answer({"repo": repo, "finding": finding, "error": None})
    answer({"error": "no finding %s" % args[2]})
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
    # The install's managed repositories, read by the image's own gitops_workspace.
    monkeypatch.setattr(shadow_run, "INSTALL_SCRIPTS_DIR", str(INSTALL_SCRIPTS))
    _register(tmp_path, monkeypatch, [INSTALL_KEY])
    monkeypatch.setattr(oobe, "agent_shell", _gateway(_fired_state()))
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


def _store(root: Path, stream: str, envelope: dict[str, Any], key: tuple[str, str] = INSTALL_KEY) -> None:
    directory = root / stream / key[0] / key[1]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "latest.json").write_text(json.dumps(envelope), encoding="utf-8")


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
    assert "has no run stored" in result.reason


def test_the_read_passes_the_include_shadow_opt_in(root, monkeypatch):
    seen: list[str] = []

    def shell(script: str, timeout: float) -> str:
        if script.startswith("cat "):
            return json.dumps(_fired_state())
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
    monkeypatch.setattr(shadow_run, "INCLUDE_SHADOW_FLAG", "--no-such-opt-in")
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
        return json.dumps(_fired_state()) if script.startswith("cat ") else ""

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
    assert "no reports for compliance-audit-canary in acme/fleet-gitops" in result.reason


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
            return json.dumps(_fired_state())
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
    monkeypatch.setattr(onboarding, "agent_shell", _gateway(_fired_state()))
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


def test_the_shadow_constants_match_audit_report_where_it_defines_them():
    """dev-5's store-only finish defines these in audit_report.py; until it lands, the copies stand alone."""
    text = AUDIT_REPORT.read_text()
    for name, value in (
        ("SHADOW_STREAM_SUFFIX", shadow_run.SHADOW_STREAM_SUFFIX),
        ("SHADOW_KEY", shadow_run.SHADOW_KEY),
        ("SHADOW_STREAM_KEY", shadow_run.SHADOW_STREAM_KEY),
    ):
        match = re.search(rf'^{name} = "([^"]*)"', text, re.M)
        if match is not None:
            assert match.group(1) == value, name
    match = re.search(r'^SHADOW_STREAM_RE = re\.compile\(r"([^"]*)"\)', text, re.M)
    if match is not None:
        assert match.group(1) == shadow_run.SHADOW_STREAM_RE.pattern
    match = re.search(r'^INCLUDE_SHADOW_FLAG = "([^"]*)"', REPORT_QUERY.read_text(), re.M)
    if match is not None:
        assert match.group(1) == shadow_run.INCLUDE_SHADOW_FLAG


# ---------------------------------------------------------------- findings_queue_untouched


def _queue_rows(stamps: dict[int, tuple[str | None, str | None]] | None = None) -> list[dict[str, Any]]:
    """The findings-item rows registered through the real queue, then restamped by index."""
    conn = sqlite3.connect(":memory:")
    fq.init_findings_schema(conn)
    rows = [{k: v for k, v in row.items() if k != "plant_state"} for row in json.loads(FINDINGS_ROWS.read_text())]
    fq.register_findings(conn, rows)
    listed = fq.list_findings(conn, limit=fq.MAX_LIST_LIMIT)
    old = (FIRED - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    for row in listed:
        conn.execute("UPDATE findings SET first_seen = ?, last_verified = ? WHERE id = ?", (old, old, row["id"]))
    for index, (first, verified) in (stamps or {}).items():
        conn.execute(
            "UPDATE findings SET first_seen = coalesce(?, first_seen), last_verified = ? WHERE id = ?",
            (first, verified if verified is not None else old, listed[index]["id"]),
        )
    return [{k: row.get(k) for k in findings.ROW_FIELDS} for row in fq.list_findings(conn, limit=fq.MAX_LIST_LIMIT)]


def _untouched(monkeypatch: pytest.MonkeyPatch, queue=None, queue_error=None, state=None, **fields: Any):
    monkeypatch.setattr(oobe, "agent_shell", _gateway(state if state is not None else _fired_state(), queue, queue_error))
    check = {"type": "findings_queue_untouched", "since_state_file": STATE_FILE, **fields}
    return FindingsQueueUntouchedVerifier(**check).verify(1.0)


def test_a_queue_with_no_row_written_since_the_fire_passes(root, monkeypatch):
    result = _untouched(monkeypatch, _queue_rows())
    assert result.status == "pass", result.reason
    assert result.raw["rows"] == len(json.loads(FINDINGS_ROWS.read_text()))


def test_an_empty_queue_passes(root, monkeypatch):
    assert _untouched(monkeypatch, []).status == "pass"


def test_a_row_registered_since_the_fire_fails_naming_it(root, monkeypatch):
    after = (FIRED + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    rows = _queue_rows({0: (after, after)})
    result = _untouched(monkeypatch, rows)
    assert result.status == "fail"
    assert rows[0]["id"] in result.reason
    assert list(result.raw["written"]) == [rows[0]["id"]]


def test_an_old_row_registered_again_since_the_fire_fails(root, monkeypatch):
    after = (FIRED + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    rows = _queue_rows({1: (None, after)})
    result = _untouched(monkeypatch, rows)
    assert result.status == "fail"
    assert "last_verified" in result.reason


def test_a_write_in_the_second_of_the_fire_counts(root, monkeypatch):
    """The queue stamps whole seconds; the bound is floored to the second."""
    fired = FIRED.replace(microsecond=600000)
    same_second = FIRED.strftime("%Y-%m-%d %H:%M:%S")
    rows = _queue_rows({0: (same_second, same_second)})
    result = _untouched(monkeypatch, rows, state=_fired_state(fired_at=fired.isoformat()))
    assert result.status == "fail"


def test_the_real_queues_stamp_reads_as_utc(root, monkeypatch):
    """datetime('now') carries no zone: a fresh registration is newer than a fire a minute ago."""
    conn = sqlite3.connect(":memory:")
    fq.init_findings_schema(conn)
    fq.register_findings(conn, [{k: v for k, v in r.items() if k != "plant_state"} for r in json.loads(FINDINGS_ROWS.read_text())][:1])
    rows = [{k: row.get(k) for k in findings.ROW_FIELDS} for row in fq.list_findings(conn)]
    fired = datetime.now(timezone.utc) - timedelta(minutes=1)
    result = _untouched(monkeypatch, rows, state=_fired_state(fired_at=fired.isoformat()))
    assert result.status == "fail", result.reason


def test_a_queue_at_the_read_cap_is_an_error(root, monkeypatch):
    rows = [{"id": f"r{i}", "first_seen": "2020-01-01 00:00:00", "last_verified": None} for i in range(findings.ROW_LIMIT)]
    result = _untouched(monkeypatch, rows)
    assert result.status == "error"
    assert "cap" in result.reason


def test_an_unparseable_stamp_is_an_error(root, monkeypatch):
    result = _untouched(monkeypatch, [{"id": "r1", "first_seen": "yesterday", "last_verified": None}])
    assert result.status == "error"


@pytest.mark.parametrize(
    ("queue", "queue_error", "said"),
    [(None, None, "could not be read"), (None, "GET /v1/findings: refused", "could not be read: GET")],
)
def test_an_unreadable_queue_is_an_error(root, monkeypatch, queue, queue_error, said):
    result = _untouched(monkeypatch, queue, queue_error)
    assert result.status == "error"
    assert said in result.reason


def test_the_queue_check_without_a_fire_errors(root, monkeypatch):
    monkeypatch.setattr(oobe, "agent_shell", _gateway(None, []))
    result = FindingsQueueUntouchedVerifier(type="findings_queue_untouched", since_state_file=STATE_FILE).verify(1.0)
    assert result.status == "error"
    assert "did not fire" in result.reason


def test_the_queue_read_asks_for_the_project_and_the_stamps(root, monkeypatch):
    seen: list[str] = []

    def shell(script: str, timeout: float) -> str:
        if script.startswith("cat "):
            return json.dumps(_fired_state())
        seen.append(script)
        return findings.FINDINGS_READ + "\n" + json.dumps({"findings": [], "error": None})

    monkeypatch.setattr(oobe, "agent_shell", shell)
    FindingsQueueUntouchedVerifier(type="findings_queue_untouched", project="seeded", since_state_file=STATE_FILE).verify(1.0)
    assert "seeded" in seen[0]
    assert "first_seen,last_verified" in seen[0]


def test_the_queue_stamp_columns_are_the_queues():
    assert {"first_seen", "last_verified"} <= set(fq._COLUMNS)


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


@pytest.mark.parametrize("delivery", ["local", "silent", "no_delivery"])
def test_a_run_that_posted_nothing_passes(root, monkeypatch, delivery):
    result = _not_posted(monkeypatch, _schedule(_run(TASK_ID, delivery)))
    assert result.status == "pass", result.reason


@pytest.mark.parametrize("delivery", ["posted", "post_failed", "post_unknown", "stored_no_home"])
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
        ({"type": "findings_queue_untouched"}, FindingsQueueUntouchedVerifier),
        ({"type": "schedule_report_not_posted", "schedule": CANARY}, ScheduleReportNotPostedVerifier),
    ):
        assert isinstance(parse_node(spec), cls)
    assert typing.get_args(ShadowReportStoredVerifier.model_fields["audit"].annotation) == typing.get_args(verifiers.AuditId)
