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

"""The ``post_merge_outcome`` verifier: merge, apply, ticks, record and comment, with every outside call faked.

The two in-pod reads run here for real, against a local SQLite file and a
local jobs.json, through ``sh -c`` in place of ``kubectl exec``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from devops_bench.verification.spec import parse_node
from kube_agents_bench import fleet, forges, onboarding, post_merge, transcript

PR = "https://github.com/gke-agentic/pool-infra/pull/7"
MANIFEST = "apiVersion: policy/v1\nkind: PodDisruptionBudget\nmetadata: {name: checkout-gateway}\n"


REAL_RUN = subprocess.run


def _local(script: str, timeout: float) -> str:
    done = REAL_RUN(["sh", "-c", script], capture_output=True, text=True, timeout=timeout, check=False)
    return done.stdout


@pytest.fixture
def pod(tmp_path, monkeypatch):
    """A local stand-in for the agent container: a records database and a jobs.json."""
    db = tmp_path / "kv.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE post_merge_records (url TEXT PRIMARY KEY, merged_at TEXT, applied_at TEXT, "
            "health_result TEXT, outcome TEXT, outcome_at TEXT, outcome_detail TEXT, reported_at TEXT)"
        )
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps({"jobs": [{"id": "post-merge-check", "enabled": True}]}))
    monkeypatch.setenv("SESSION_KV_DB_PATH", str(db))
    monkeypatch.setattr(post_merge, "JOBS_FILE", str(jobs))
    monkeypatch.setattr(post_merge, "HERMES_PYTHON", "/nonexistent")
    monkeypatch.setattr(post_merge, "FALLBACK_PYTHON", "python3")
    return SimpleNamespace(db=db, jobs=jobs)


def _set_record(db: Path, **row) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT OR REPLACE INTO post_merge_records (url, outcome, outcome_detail, reported_at) "
                     "VALUES (?, ?, ?, ?)",
                     (PR, row.get("outcome"), row.get("outcome_detail"), row.get("reported_at", "silent: 2026-11-18")))


class Forge:
    """GitHub, faked: merges, files, comments, contents."""

    def __init__(self, merge_status: int = 200, comments: list[str] | None = None) -> None:
        self.extra_files: list[dict] = []
        self.merge_status = merge_status
        self.comments = comments if comments is not None else []
        self.writes: list[tuple[str, str]] = []

    def write(self, method, url, token, body):
        self.writes.append((method, url))
        if url.endswith("/merge"):
            return self.merge_status, {"message": "nope"} if self.merge_status != 200 else {"merged": True}
        return 200, {}

    def get(self, url, token, timeout):
        if "/files" in url:
            return 200, [{"filename": "clusters/seeded-a/seeded-reliability/checkout-gateway-pdb.yaml",
                          "status": "added", "patch": "@@ -0,0 +1,3 @@\n+" + MANIFEST.rstrip("\n").replace("\n", "\n+")}
                         ] + self.extra_files
        if "/comments" in url:
            return 200, [{"body": body} for body in self.comments]
        if url.endswith("/labels"):
            return 200, [{"name": "audit:obtainability-audit"}, {"name": "audit:remediation"}, {"name": "severity:major"}]
        if "/contents/" in url:
            return 200, {"sha": "abc", "content": "b2xk\n"}
        return 200, {"merged": True, "base": {"sha": "base1"}}


@pytest.fixture
def world(pod, monkeypatch):
    forge = Forge()
    applied: list[str] = []
    ticks: list[str] = []
    monkeypatch.setenv(post_merge.WRITE_TOKEN_ENV, "w-token")
    monkeypatch.setattr(forges, "read_token", lambda forge_name, environ=None: "r-token")
    monkeypatch.setattr(forges, "github_host", lambda environ=None: "github.com")
    monkeypatch.setattr(transcript, "get", lambda: SimpleNamespace(started_at=1.0, final_message=f"ledger, {PR}"))
    monkeypatch.setattr(post_merge, "_github_write", forge.write)
    monkeypatch.setattr(post_merge, "_forge_get_json", forge.get)
    monkeypatch.setattr(post_merge, "_apply", lambda text, kubeconfig: applied.append(text) or (True, ""))
    monkeypatch.setattr(post_merge, "writable_kubeconfig", lambda role: "/tmp/kc")
    monkeypatch.setattr(post_merge, "TICK_INTERVAL_SEC", 0.0)
    monkeypatch.setattr(post_merge.time, "sleep", lambda s: None)
    monkeypatch.setattr(subprocess, "run", _no_kubectl(subprocess.run))

    def shell(script, timeout):
        if post_merge.TICK_READ in script:
            ticks.append(script)
            if world.on_tick:
                world.on_tick(len(ticks))
            return f"tick\n{post_merge.TICK_READ} 0\n"
        return _local(script, timeout)

    world = SimpleNamespace(forge=forge, applied=applied, ticks=ticks, pod=pod, on_tick=None)
    monkeypatch.setattr(onboarding, "agent_shell", shell)
    return world


def _no_kubectl(real):
    def run(argv, *args, **kwargs):
        if argv and argv[0] == "kubectl":
            return subprocess.CompletedProcess(argv, 0, "", "")
        return real(argv, *args, **kwargs)
    return run


def _verifier(**overrides) -> post_merge.PostMergeOutcomeVerifier:
    fields = {"type": "post_merge_outcome", "owner": "gke-agentic", "outcome": "verified_fixed",
              "apply_fixture_role": "no-pdb-workload", **overrides}
    return post_merge.PostMergeOutcomeVerifier(**fields)


MARKED = f"**Verified fixed.** live\n\n{post_merge.COMMENT_MARKER}"


def test_silent_passes_on_the_silent_stamp_and_no_comment(world):
    world.forge.comments = ["an unrelated comment"]
    world.on_tick = lambda n: n == 2 and _set_record(world.pod.db, outcome="verified_fixed")
    result = _verifier().verify(0)
    assert result.status == "pass", result.reason
    assert len(world.ticks) == 2


@pytest.mark.parametrize("reported_at, comments", [("2026-11-18T10:00:00Z", []), ("silent: x", [MARKED])])
def test_silent_fails_on_a_posted_report(world, reported_at, comments):
    world.forge.comments = comments
    world.on_tick = lambda n: _set_record(world.pod.db, outcome="verified_fixed", reported_at=reported_at)
    assert _verifier().verify(0).status == "fail"


def test_an_unreported_outcome_fails(world, monkeypatch):
    monkeypatch.setattr(post_merge, "DRIVE_DEADLINE_SEC", 0.0)
    _set_record(world.pod.db, outcome="verified_fixed", reported_at=None)
    result = _verifier().verify(0)
    assert result.status == "fail" and "not been reported" in result.reason


def test_report_needs_a_phrase():
    with pytest.raises(ValueError):
        _verifier(report="report")


def test_report_merges_applies_ticks_and_passes_on_one_marked_comment(world):
    world.forge.comments = [MARKED, "an unrelated comment"]
    world.on_tick = lambda n: n == 3 and _set_record(world.pod.db, outcome="verified_fixed", reported_at="2026-11-18")
    result = _verifier(report="report", comment_phrase="Verified fixed").verify(0)
    assert result.status == "pass", result.reason
    assert len(world.ticks) == 3
    assert world.applied == [MANIFEST]
    methods = [m for m, _ in world.forge.writes]
    assert methods[0] == "PUT" and world.forge.writes[0][1].endswith("/pulls/7/merge")
    # The merged pull request loses its audit labels, so the next audit does not bind it to the
    # finding, and the merged file comes off the default branch.
    unlabelled = [url.rsplit("/", 1)[1] for m, url in world.forge.writes if "/labels/" in url]
    assert unlabelled == ["audit%3Aobtainability-audit", "audit%3Aremediation"]
    assert methods[-1] == "DELETE" and "/contents/clusters/seeded-a/" in world.forge.writes[-1][1]


def test_cleanup_restores_a_modified_file_and_survives_a_kubectl_failure(world, monkeypatch):
    world.forge.extra_files = [{"filename": "clusters/seeded-a/kustomization.yaml", "status": "modified"}]
    monkeypatch.setattr(post_merge, "DRIVE_DEADLINE_SEC", 0.0)

    def broken_kubectl(argv, *args, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(subprocess, "run", broken_kubectl)
    _verifier().verify(0)
    writes = world.forge.writes
    contents = [(m, url) for m, url in writes if "/contents/" in url]
    assert contents[0][0] == "DELETE" and contents[0][1].endswith("checkout-gateway-pdb.yaml")
    assert contents[1][0] == "PUT" and contents[1][1].endswith("/contents/clusters/seeded-a/kustomization.yaml")


def test_no_outcome_by_the_deadline_fails_as_on_main(world, monkeypatch):
    monkeypatch.setattr(post_merge, "DRIVE_DEADLINE_SEC", 0.0)
    _set_record(world.pod.db, outcome=None)
    result = _verifier().verify(0)
    assert result.status == "fail" and "no outcome yet" in result.reason


def test_a_different_outcome_fails(world):
    world.on_tick = lambda n: _set_record(world.pod.db, outcome="still_broken", outcome_detail="red")
    result = _verifier().verify(0)
    assert result.status == "fail" and "'still_broken'" in result.reason


@pytest.mark.parametrize("comments", [[], [MARKED, MARKED], ["**Verified fixed.** without the marker"]])
def test_report_fails_on_anything_but_exactly_one_marked_comment(world, comments):
    world.forge.comments = comments
    world.on_tick = lambda n: _set_record(world.pod.db, outcome="verified_fixed", reported_at="2026-11-18")
    result = _verifier(report="report", comment_phrase="Verified fixed").verify(0)
    assert result.status == "fail" and "exactly one" in result.reason


def test_no_record_for_the_pull_request_fails(world, monkeypatch):
    monkeypatch.setattr(post_merge, "DRIVE_DEADLINE_SEC", 0.0)
    result = _verifier().verify(0)
    assert result.status == "fail" and "no post_merge_records row" in result.reason


def test_an_unscheduled_cron_job_fails(world):
    world.pod.jobs.write_text(json.dumps({"jobs": [{"id": "post-merge-check", "enabled": False}]}))
    result = _verifier().verify(0)
    assert result.status == "fail" and "post-merge-check" in result.reason
    assert world.forge.writes == []


@pytest.mark.parametrize(
    "setup, why",
    [
        (lambda w, mp: mp.delenv(post_merge.WRITE_TOKEN_ENV), post_merge.WRITE_TOKEN_ENV),
        (lambda w, mp: setattr(w.forge, "merge_status", 403), "403"),
        (lambda w, mp: mp.setattr(transcript, "get", lambda: SimpleNamespace(started_at=1.0, final_message="no url")),
         "names 0 pull requests"),
        (lambda w, mp: mp.setattr(post_merge, "_apply", lambda t, k: (False, "denied")), "denied"),
    ],
)
def test_setup_failures_are_errors_not_fails(world, monkeypatch, setup, why):
    setup(world, monkeypatch)
    result = _verifier().verify(0)
    assert result.status == "error" and why in result.reason


def test_an_unresolvable_role_errors_before_any_merge(world, monkeypatch):
    def unresolved(role):
        raise fleet.FleetRoleUnresolved("no kubeconfig for no-pdb-workload")

    monkeypatch.setattr(post_merge, "writable_kubeconfig", unresolved)
    result = _verifier().verify(0)
    assert result.status == "error" and world.forge.writes == []


def test_a_bad_role_name_fails_at_load():
    with pytest.raises(ValueError):
        _verifier(apply_fixture_role="No_Such Role")


def test_a_failed_apply_after_the_merge_still_unlabels_and_restores(world, monkeypatch):
    monkeypatch.setattr(post_merge, "_apply", lambda text, kubeconfig: (False, "denied"))
    assert _verifier().verify(0).status == "error"
    writes = [(m, url.rsplit("/", 1)[1]) for m, url in world.forge.writes]
    assert ("DELETE", "audit%3Aremediation") in writes
    assert ("DELETE", "checkout-gateway-pdb.yaml") in writes


def test_an_unreadable_jobs_file_is_an_error(world):
    world.pod.jobs.write_text("not json")
    assert _verifier().verify(0).status == "error"


def test_read_record_needs_the_sentinel():
    assert post_merge.read_record(lambda s, t: "", PR, 1) is None
    assert post_merge.read_record(lambda s, t: f"{post_merge.RECORD_READ}\nnot json", PR, 1) is None
    answer = post_merge.read_record(lambda s, t: f"{post_merge.RECORD_READ}\n" + json.dumps({"record": None, "error": None}), PR, 1)
    assert answer == {"record": None, "error": None}


def test_a_spec_with_the_check_parses():
    parse_node({"type": "post_merge_outcome", "outcome": "verified_fixed", "apply_fixture_role": "no-pdb-workload"})


def test_the_entry_point_is_published():
    import tomllib

    project = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    entries = project["project"]["entry-points"]["devops_bench.verifiers"]
    assert entries["post_merge_outcome"] == "kube_agents_bench.post_merge:PostMergeOutcomeVerifier"


def test_the_tick_runs_the_cron_script_under_the_jobs_lock(tmp_path, monkeypatch):
    import fcntl
    import threading

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "post_merge_check.py").write_text("print('ticked')\nraise SystemExit(3)\n")
    lock = tmp_path / ".job-post-merge-check.lock"
    monkeypatch.setattr(post_merge, "SCRIPTS_DIR", str(scripts))
    monkeypatch.setattr(post_merge, "JOB_LOCK", str(lock))
    monkeypatch.setattr(post_merge, "HERMES_PYTHON", "/nonexistent")
    monkeypatch.setattr(post_merge, "FALLBACK_PYTHON", "python3")
    held = open(lock, "a")
    fcntl.flock(held, fcntl.LOCK_EX)
    out: list[str] = []
    runner = threading.Thread(target=lambda: out.append(_local(post_merge.tick_command(), 30)))
    runner.start()
    runner.join(1.0)
    assert runner.is_alive(), "the tick ran while the cron held the job's lock"
    fcntl.flock(held, fcntl.LOCK_UN)
    held.close()
    runner.join(30)
    assert "ticked" in out[0] and f"{post_merge.TICK_READ} 3" in out[0]


def test_the_apply_credential_is_the_runners_not_the_fleet_readers(tmp_path, monkeypatch):
    calls = []

    def run(argv, *args, **kwargs):
        calls.append((argv, kwargs.get("env", {}).get("KUBECONFIG")))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(fleet, "slot_of_role", lambda role, directory=None: "a")
    monkeypatch.setattr(fleet, "recorded_clusters", lambda directory=None: {"a": ("seeded-a", "us-central1-a")})
    monkeypatch.setattr(fleet, "provisioned_project", lambda directory=None: "evals-42")
    monkeypatch.setattr(fleet, "kubeconfig_for_role", lambda *a, **k: pytest.fail("the read-only kubeconfig was used"))
    monkeypatch.setattr(subprocess, "run", run)
    path = post_merge.writable_kubeconfig("no-pdb-workload")
    try:
        argv, kubeconfig = calls[0]
        assert argv == ["gcloud", "container", "clusters", "get-credentials", "seeded-a", "--location",
                        "us-central1-a", "--project", "evals-42", "--quiet"]
        assert kubeconfig == path
    finally:
        os.remove(path)


def test_a_refused_credential_or_missing_record_is_unresolved(monkeypatch):
    monkeypatch.setattr(fleet, "slot_of_role", lambda role, directory=None: "a")
    monkeypatch.setattr(fleet, "provisioned_project", lambda directory=None: "evals-42")
    monkeypatch.setattr(fleet, "recorded_clusters", lambda directory=None: {})
    with pytest.raises(fleet.FleetRoleUnresolved):
        post_merge.writable_kubeconfig("no-pdb-workload")
    monkeypatch.setattr(fleet, "recorded_clusters", lambda directory=None: {"a": ("seeded-a", "us-central1-a")})
    written = []
    real_mkstemp = post_merge.tempfile.mkstemp

    def mkstemp(*a, **k):
        handle, path = real_mkstemp(*a, **k)
        written.append(path)
        return handle, path

    monkeypatch.setattr(post_merge.tempfile, "mkstemp", mkstemp)
    monkeypatch.setattr(subprocess, "run", lambda argv, *a, **k: subprocess.CompletedProcess(argv, 1, "", "denied"))
    with pytest.raises(fleet.FleetRoleUnresolved, match="denied"):
        post_merge.writable_kubeconfig("no-pdb-workload")
    assert written and not os.path.exists(written[0])
    # A non-zero exit with nothing on stderr is still a refusal, not an empty credential.
    monkeypatch.setattr(subprocess, "run", lambda argv, *a, **k: subprocess.CompletedProcess(argv, -9, "", ""))
    with pytest.raises(fleet.FleetRoleUnresolved, match="exited -9"):
        post_merge.writable_kubeconfig("no-pdb-workload")
    assert not os.path.exists(written[-1])
