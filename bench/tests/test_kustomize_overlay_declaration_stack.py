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

"""The kustomize-overlay-declaration stack of obtainability-pdb-kustomize-overlay.

``bench/tf/prebuilt/kustomize-overlay-declaration/plant.py`` runs here against
a recorder in place of GitHub. The tests show what the case depends on: the
plant commits a kustomization that lists only checkout-gateway.yaml and
carries the stack's label; it refuses a file that is not the stack's and a
directory without the declaration; the teardown labels an audit pull request
`audit:stale-closed` before it closes it, leaves a person's pull request and
one outside the directory open, and deletes only the stack's file; and the
stack, the task and the runner agree on the inputs and the labels.
"""

from __future__ import annotations

import base64
import importlib.util
import re
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
STACK = REPO / "bench" / "tf" / "prebuilt" / "kustomize-overlay-declaration"
TASK = REPO / "bench" / "tasks" / "obtainability-pdb-kustomize-overlay" / "task.yaml"
GITOPS = "gke-agentic/kube-agents-evals-2-infra"
DIRECTORY = "clusters/seeded-a/seeded-reliability"
KUSTOMIZATION = f"{DIRECTORY}/kustomization.yaml"
DECLARATION = f"{DIRECTORY}/checkout-gateway.yaml"
BOT = "kube-agents-evals[bot]"

_spec = importlib.util.spec_from_file_location("kustomize_overlay_plant", STACK / "plant.py")
plant = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plant)


class FakeGitHub:
    """A repository with files on `main` and open pull requests; records writes."""

    def __init__(self, files=None, pulls=None, refuse=()):
        self.files = dict(files or {})
        self.pulls = list(pulls or [])
        self.refuse = set(refuse)
        self.writes: list[tuple[str, str, dict | None]] = []

    def __call__(self, method, path, body):
        base = f"/repos/{GITOPS}"
        assert path.startswith(base), path
        rest = path[len(base):]
        if method != "GET":
            self.writes.append((method, rest, body))
            if (method, rest.split("?")[0]) in self.refuse:
                return 403, None
        if method == "GET" and rest == "":
            return 200, {"default_branch": "main"}
        if rest.startswith("/contents/"):
            name = rest[len("/contents/"):].split("?")[0]
            if method == "GET":
                if name not in self.files:
                    return 404, None
                return 200, {"content": base64.b64encode(self.files[name].encode()).decode(), "sha": f"sha-{name}"}
            if method == "PUT":
                self.files[name] = base64.b64decode(body["content"]).decode()
                return 201, {}
            if method == "DELETE":
                assert body["sha"] == f"sha-{name}"
                del self.files[name]
                return 200, {}
        if method == "GET" and rest.startswith("/pulls?"):
            return 200, [p["pull"] for p in self.pulls]
        match = re.fullmatch(r"/pulls/(\d+)/files\?.*", rest)
        if method == "GET" and match:
            pull = next(p for p in self.pulls if p["pull"]["number"] == int(match.group(1)))
            return 200, [{"filename": name} for name in pull["files"]]
        if method == "POST" and re.fullmatch(r"/issues/\d+/labels", rest):
            return 200, []
        if method == "PATCH" and re.fullmatch(r"/pulls/\d+", rest):
            return 200, {}
        raise AssertionError(f"unexpected {method} {path}")


def _pull(number, files, login=BOT, labels=(), head_repo=GITOPS):
    return {
        "pull": {
            "number": number,
            "user": {"login": login},
            "labels": [{"name": name} for name in labels],
            "head": {"ref": f"branch-{number}", "repo": {"full_name": head_repo}},
        },
        "files": files,
    }


def _stack(github, directory=DIRECTORY):
    settings = plant.Settings(gitops_repo=GITOPS, directory=directory, token_file="/unused")
    return plant.Stack(settings, github)


def test_the_plant_commits_a_kustomization_that_lists_only_the_declaration():
    github = FakeGitHub(files={DECLARATION: "kind: Deployment\n"})
    _stack(github).plant()
    doc = yaml.safe_load(github.files[KUSTOMIZATION])
    assert doc["kind"] == "Kustomization"
    assert doc["resources"] == ["checkout-gateway.yaml"]
    assert doc["metadata"]["labels"] == {"managed-by": "kube-agents-bench"}
    assert [w[0] for w in github.writes] == ["PUT"]
    assert github.writes[0][2]["branch"] == "main"


def test_the_plant_refuses_a_file_that_is_not_the_stacks():
    github = FakeGitHub(files={DECLARATION: "kind: Deployment\n", KUSTOMIZATION: "resources: []\n"})
    with pytest.raises(RuntimeError, match="not this stack's"):
        _stack(github).plant()
    assert github.writes == []


def test_the_plant_writes_again_over_its_own_leftover():
    github = FakeGitHub(files={DECLARATION: "kind: Deployment\n", KUSTOMIZATION: plant.kustomization_text()})
    _stack(github).plant()
    assert github.writes[0][2]["sha"] == f"sha-{KUSTOMIZATION}"


def test_the_plant_refuses_a_directory_without_the_declaration():
    github = FakeGitHub()
    with pytest.raises(RuntimeError, match="would declare nothing"):
        _stack(github).plant()
    assert github.writes == []


def test_the_plant_refuses_missing_settings():
    github = FakeGitHub(files={DECLARATION: "kind: Deployment\n"})
    stack = plant.Stack(plant.Settings(gitops_repo=GITOPS, directory=DIRECTORY, token_file=""), github)
    with pytest.raises(RuntimeError, match="bench_gitops_write_token_file"):
        stack.plant()


def test_the_teardown_labels_the_audit_pull_request_before_it_closes_it():
    github = FakeGitHub(
        files={DECLARATION: "kind: Deployment\n", KUSTOMIZATION: plant.kustomization_text()},
        pulls=[
            _pull(7, [f"{DIRECTORY}/pdb.yaml"], labels=[plant.AUDIT_REMEDIATION_LABEL]),
            _pull(8, [f"{DIRECTORY}/pdb.yaml"], login="someone"),
            _pull(9, ["clusters/seeded-b/other.yaml"]),
            _pull(10, [f"{DIRECTORY}/pdb.yaml"], head_repo="fork/kube-agents-evals-2-infra"),
        ],
    )
    assert _stack(github).teardown()
    writes = [(m, r) for m, r, _ in github.writes]
    assert writes == [
        ("POST", "/issues/7/labels"),
        ("PATCH", "/pulls/7"),
        ("DELETE", f"/contents/{KUSTOMIZATION}"),
    ]
    assert github.writes[0][2] == {"labels": ["audit:stale-closed"]}
    assert KUSTOMIZATION not in github.files
    assert DECLARATION in github.files


def test_a_label_that_does_not_stick_leaves_the_pull_request_open_and_fails_the_destroy():
    github = FakeGitHub(
        files={DECLARATION: "kind: Deployment\n", KUSTOMIZATION: plant.kustomization_text()},
        pulls=[_pull(7, [f"{DIRECTORY}/pdb.yaml"], labels=[plant.AUDIT_REMEDIATION_LABEL])],
        refuse={("POST", "/issues/7/labels")},
    )
    assert not _stack(github).teardown()
    assert ("PATCH", "/pulls/7") not in [(m, r) for m, r, _ in github.writes]
    # The file still goes: each step stands alone.
    assert KUSTOMIZATION not in github.files


def test_the_teardown_leaves_a_file_and_pull_requests_that_are_not_the_stacks():
    github = FakeGitHub(
        files={DECLARATION: "kind: Deployment\n", KUSTOMIZATION: "resources: []\n"},
        pulls=[_pull(7, [f"{DIRECTORY}/pdb.yaml"], labels=[plant.AUDIT_REMEDIATION_LABEL])],
    )
    assert _stack(github).teardown()
    assert github.writes == []
    assert github.files[KUSTOMIZATION] == "resources: []\n"


def test_a_refused_delete_fails_the_destroy():
    github = FakeGitHub(
        files={DECLARATION: "kind: Deployment\n", KUSTOMIZATION: plant.kustomization_text()},
        refuse={("DELETE", f"/contents/{KUSTOMIZATION}")},
    )
    assert not _stack(github).teardown()


def test_the_token_is_read_from_the_file_on_each_request(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("first\n")
    settings = plant.Settings(gitops_repo=GITOPS, directory=DIRECTORY, token_file=str(token_file))
    assert settings.github_token() == "first"
    token_file.write_text("renewed\n")
    assert settings.github_token() == "renewed"


def test_the_labels_are_the_resets():
    sys.path.insert(0, str(REPO / "hack"))
    try:
        import ci_reset_agent_pulls as reset
    finally:
        sys.path.remove(str(REPO / "hack"))
    assert plant.STALE_CLOSED_LABEL == reset.STALE_CLOSED_LABEL
    assert plant.AUDIT_REMEDIATION_LABEL == reset.AUDIT_REMEDIATION_LABEL
    assert plant.BOT_LOGIN_SUFFIX == reset.BOT_LOGIN_SUFFIX


def _variables():
    return set(re.findall(r'^variable "([a-z_]+)"', (STACK / "variables.tf").read_text(), re.M))


def test_the_stack_declares_the_variable_the_runner_mints_for():
    runner = (REPO / "hack" / "ci-eval-pr.sh").read_text()
    declared = re.search(r'^BENCH_GITOPS_WRITE_VARIABLE="([a-z_]+)"$', runner, re.M).group(1)
    assert declared == "bench_gitops_write_token_file"
    assert declared in _variables()
    # gitops-fix-cycle's variable, with another contract, is not the marker.
    assert "gitops_token_file" not in _variables()
    for name in ("gitops_repo", "host_cluster_name", "host_cluster_location"):
        assert f"TF_VAR_{name}=" in runner, name


def test_the_task_runs_the_stack_alone_with_teardown():
    doc = yaml.safe_load(TASK.read_text())
    assert doc["writes_alone"] is True
    infra = doc["infrastructure"]
    assert infra["stack"] == "prebuilt/kustomize-overlay-declaration"
    assert infra["teardown"] is True
    assert set(infra.get("variables") or {}) <= _variables()


def test_the_destroy_provisioner_does_not_continue_on_failure():
    main = (STACK / "main.tf").read_text()
    assert "when        = destroy" in main
    assert "on_failure" not in re.sub(r"#[^\n]*", "", main)


def test_the_default_directory_is_the_one_provisioning_seeds():
    provisioning = (REPO / "scripts" / "provision_ci_pool_project.sh").read_text()
    seeded = re.search(r'^readonly GITOPS_WORKLOAD_MANIFEST_PATH="([^"]+)"$', provisioning, re.M).group(1)
    default = re.search(r'variable "cluster_directory" \{.*?default\s+= "([^"]+)"', (STACK / "variables.tf").read_text(), re.S).group(1)
    assert seeded == f"{default}/{plant.DECLARATION_FILE}"
