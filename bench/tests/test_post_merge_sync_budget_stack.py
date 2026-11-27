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

"""The post-merge-sync-budget stack of post-merge-verified-fixed.

``bench/tf/prebuilt/post-merge-sync-budget/budget.py`` runs here against a
fake repository and a fake Config Sync. The tests show what the case depends
on: the setup takes the steady-state budget off the branch and waits for the
prune; a prune that never lands restores and fails; the teardown puts the
seeded budget back over whatever the merge left and waits for it; and the
stack, the task and the runner agree, including that the case runs in the
writers phase, one unit at a time.
"""

from __future__ import annotations

import base64
import importlib.util
import re
from pathlib import Path

import pytest
import yaml
from kube_agents_bench import lane

REPO = Path(__file__).resolve().parents[2]
STACK = REPO / "bench" / "tf" / "prebuilt" / "post-merge-sync-budget"
TASK = REPO / "bench" / "tasks" / "post-merge-verified-fixed" / "task.yaml"
DIRECTORY = "clusters/seeded-a/seeded-post-merge"
CONTENTS = f"contents/{DIRECTORY}/order-ledger-pdb.yaml"

_spec = importlib.util.spec_from_file_location("post_merge_sync_budget", STACK / "budget.py")
budget = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(budget)


class World:
    """The leased repository's budget file and Config Sync's view of it, after `lag` reads."""

    def __init__(self, text: str | None, lag: int = 1, refuse: tuple = (), answers: dict | None = None, applied_spec=None):
        self.text, self.lag, self.refuse, self.writes = text, lag, set(refuse), []
        self.applied = text is not None
        self.reads_since_commit = 0
        # {method: status}: the write lands, and the client sees this answer.
        self.answers = answers or {}
        # What Config Sync applies for a file on the branch.
        self.applied_spec = applied_spec or {"metadata": {"labels": {"app": "order-ledger"}}, "spec": {"maxUnavailable": 1}}

    def github(self, method, path, body=None):
        assert path == CONTENTS
        if method in self.refuse:
            return 409, None
        if method == "GET" and "GET" in self.answers:
            return self.answers.pop("GET"), None
        if method == "GET":
            if self.text is None:
                return 404, None
            return 200, {"sha": "s1", "content": base64.b64encode(self.text.encode()).decode()}
        self.writes.append((method, body))
        self.text = None if method == "DELETE" else base64.b64decode(body["content"]).decode()
        self.reads_since_commit = 0
        return self.answers.get(method, 200), {}

    def budget_state(self):
        self.reads_since_commit += 1
        if self.reads_since_commit > self.lag:
            self.applied = self.text is not None
        return dict(self.applied_spec) if self.applied else {}


@pytest.fixture
def world(monkeypatch):
    def make(text, **kwargs):
        w = World(text, **kwargs)
        monkeypatch.setattr(budget, "github", w.github)
        monkeypatch.setattr(budget, "budget_state", w.budget_state)
        monkeypatch.setattr(budget, "POLL_S", 0.0)
        monkeypatch.setattr(budget, "WAIT_S", 0.05)
        monkeypatch.setattr(budget.time, "sleep", lambda s: None)
        monkeypatch.setenv(budget.DIRECTORY_ENV, DIRECTORY)
        return w

    return make


def test_remove_takes_the_budget_off_the_branch_and_waits_for_the_prune(world):
    w = world(budget.BUDGET_CONTENT, lag=3)
    assert budget.remove() == 0
    assert [m for m, _ in w.writes] == ["DELETE"] and w.writes[0][1]["sha"] == "s1"
    assert w.applied is False and w.reads_since_commit == 4


def test_remove_refuses_a_branch_without_the_steady_state_budget(world):
    w = world(None)
    assert budget.remove() == 1
    assert w.writes == []


def test_a_prune_that_never_lands_restores_before_it_fails(world):
    w = world(budget.BUDGET_CONTENT, lag=10**6)
    assert budget.remove() == 1
    assert [m for m, _ in w.writes] == ["DELETE", "PUT"]
    assert w.text == budget.BUDGET_CONTENT


def test_restore_puts_the_seeded_budget_over_what_the_merge_left_and_waits(world):
    w = world("# seeded-a: no-pdb on Deployment/order-ledger (x)\n" + budget.BUDGET_CONTENT, lag=2)
    assert budget.restore() == 0
    assert [m for m, _ in w.writes] == ["PUT"] and w.writes[0][1]["sha"] == "s1"
    assert w.text == budget.BUDGET_CONTENT and w.applied


def test_restore_writes_a_missing_file_without_a_sha_and_leaves_the_seeded_one_alone(world):
    w = world(None)
    assert budget.restore() == 0
    assert "sha" not in w.writes[0][1]
    assert budget.restore() == 0
    assert len(w.writes) == 1


def test_a_refused_restore_fails_the_destroy(world):
    world(None, refuse=("PUT",))
    assert budget.restore() == 1


def test_a_budget_that_never_comes_back_fails_the_destroy(world):
    world(None, lag=10**6)
    assert budget.restore() == 1


def test_a_lost_delete_answer_restores_before_the_apply_fails(world):
    # GitHub deleted the file but the answer said otherwise: the branch may be
    # without the budget, so the setup puts it back.
    w = world(budget.BUDGET_CONTENT, answers={"DELETE": 502})
    assert budget.remove() == 1
    assert [m for m, _ in w.writes] == ["DELETE", "PUT"]
    assert w.text == budget.BUDGET_CONTENT


def test_a_failing_get_before_the_delete_writes_nothing(world):
    w = world(budget.BUDGET_CONTENT, answers={"GET": 503})
    assert budget.remove() == 1
    assert w.writes == []


def test_restore_waits_for_the_seeded_shape_not_only_the_name(world):
    world(None, applied_spec={"metadata": {"labels": {}}, "spec": {"maxUnavailable": 1}})
    assert budget.restore() == 1


def test_a_response_cut_short_is_tried_again(monkeypatch, tmp_path):
    import http.client

    calls = []

    def urlopen(request, timeout=None):
        calls.append(request)
        if len(calls) == 1:
            raise http.client.IncompleteRead(b"")
        raise budget.urllib.error.HTTPError(request.full_url, 404, "gone", {}, None)

    monkeypatch.setattr(budget.urllib.request, "urlopen", urlopen)
    monkeypatch.setenv(budget.REPO_ENV, "o/r")
    token = tmp_path / "token"
    token.write_text("t")
    monkeypatch.setenv(budget.TOKEN_FILE_ENV, str(token))
    assert budget.github("GET", "contents/x", sleep=lambda s: None) == (404, None)
    assert len(calls) == 2


def test_a_budget_that_is_not_utf8_is_replaced(monkeypatch):
    raw = base64.b64encode(b"\xff\xfe not utf-8").decode()
    monkeypatch.setattr(budget, "github", lambda method, path, body=None: (200, {"sha": "s1", "content": raw}))
    monkeypatch.setenv(budget.DIRECTORY_ENV, DIRECTORY)
    sha, text = budget.read_file()
    assert sha == "s1" and text != budget.BUDGET_CONTENT


def test_a_5xx_is_tried_again(monkeypatch):
    answers = iter([(502, None), (0, None), (200, {"ok": True})])
    monkeypatch.setattr(budget, "_github_once", lambda method, path, body: next(answers))
    assert budget.github("GET", "contents/x", sleep=lambda s: None) == (200, {"ok": True})


def test_main_refuses_missing_settings_and_unknown_commands(monkeypatch):
    for name in (budget.REPO_ENV, budget.TOKEN_FILE_ENV, budget.DIRECTORY_ENV, budget.KUBECONFIG_DIR_ENV):
        monkeypatch.delenv(name, raising=False)
    assert budget.main(["budget.py", "remove"]) == 1
    assert budget.main(["budget.py", "plant"]) == 2


def test_the_budget_is_the_one_the_fix_recipe_writes_without_its_comment():
    doc = yaml.safe_load(budget.BUDGET_CONTENT)
    assert doc["metadata"] == {"name": "order-ledger-pdb", "namespace": "seeded-post-merge", "labels": {"app": "order-ledger"}}
    assert doc["spec"] == {"maxUnavailable": 1, "selector": {"matchLabels": {"app": "order-ledger"}}}


def _variables():
    return set(re.findall(r'^variable "([a-z_]+)"', (STACK / "variables.tf").read_text(), re.MULTILINE))


def test_the_stack_declares_the_variable_the_runner_mints_for():
    runner = (REPO / "hack" / "ci-eval-pr.sh").read_text()
    declared = re.search(r'^BENCH_GITOPS_WRITE_VARIABLE="([a-z_]+)"$', runner, re.MULTILINE).group(1)
    assert declared in _variables()
    assert "BENCH_FLEET_KUBECONFIG_DIR" in runner


def test_the_task_runs_the_stack_with_teardown_in_the_writers_phase():
    doc = yaml.safe_load(TASK.read_text())
    infra = doc["infrastructure"]
    assert infra["stack"] == "prebuilt/post-merge-sync-budget" and infra["teardown"] is True
    assert set(infra.get("variables") or {}) <= _variables()
    # A case that requests a pull request runs in hack/ci-eval-pr.sh's
    # writers phase (unit_phase 1): one unit at a time, after every other
    # unit, so no case reads seeded-post-merge while the budget is off.
    assert lane.requested_for(doc["verification_spec"]) > 0


def test_the_destroy_provisioner_does_not_continue_on_failure():
    main = (STACK / "main.tf").read_text()
    assert "when        = destroy" in main
    assert "on_failure" not in re.sub(r"#[^\n]*", "", main)


def test_the_directory_follows_the_fleets_slot_a_name():
    # On a pool project kcc_cluster_name is seeded-a; on a dev fleet with a
    # prefix it is <prefix>-a, the name hack/ci-eval-pr.sh exports.
    main = (STACK / "main.tf").read_text()
    variables = (STACK / "variables.tf").read_text()
    assert 'coalesce(var.cluster_directory, "clusters/${var.kcc_cluster_name}/seeded-post-merge")' in main
    default = re.search(r'variable "kcc_cluster_name" \{.*?default\s+= "([^"]+)"', variables, re.DOTALL).group(1)
    assert f"clusters/{default}/seeded-post-merge" == DIRECTORY
    runner = (REPO / "hack" / "ci-eval-pr.sh").read_text()
    assert 'export TF_VAR_kcc_cluster_name="${cluster}"' in runner
    fleet = (REPO / "bench" / "tf" / "fleet" / "post-merge-sync.tf").read_text()
    assert 'dir       = "clusters/${google_container_cluster.seeded_a.name}/${local.post_merge_namespace}"' in fleet
