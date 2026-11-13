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

"""The lane-level safeguards append (``kube_agents_bench.lane``) over the
real lane file and real task files."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from devops_bench.verification.spec import parse_entries
from kube_agents_bench import lane, stop_hook
from kube_agents_bench.cases import load_case

REPO_ROOT = Path(__file__).resolve().parents[2]
LANE_FILE = REPO_ROOT / "hack" / "eval" / "inject-lane-safeguards.yaml"
TASKS = REPO_ROOT / "bench" / "tasks"
# A presubmit case that requests no pull request, neither through its own
# checks nor through a `requested_pull_requests:` key, and a case that requests
# one (its objective is a pull_request_opened check). Not
# obtainability-remediation-proposal: it asks for a proposal, it declares no
# count of its own, and it is the case the zero-allowance safeguard exists to
# watch.
READ_ONLY_CASE = "reliability-pdb-probe"
REQUESTING_CASE = "pdb-remediation-pr"
# A stopped ask (stop_hook.py): it requests no pull request, and runs alone
# in the second phase on the inject lane, the only lane that carries it.
STOPPED_ASK_CASE = "chat-stop-halts-delegated-remediation"
# The count a scratch copy of READ_ONLY_CASE declares under the task key, so
# the declared path is tested without editing a shipped task.
DECLARED_COUNT = 2


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_the_lane_file_loads_and_is_one_none_wrapped_github_writes_safeguard():
    entries = lane.load_lane_safeguards(LANE_FILE)
    assert [e["name"] for e in entries] == ["no-github-writes-the-case-did-not-request"]
    (entry,) = entries
    assert entry["role"] == "safeguard" and entry["severity"] == "catastrophic"
    assert entry["check"]["type"] == "none"
    assert [leaf["type"] for leaf in lane._leaves(entry["check"])] == ["github_writes"]
    # devops-bench accepts it as written, so a lane entry cannot arrive as a
    # parse error on every case.
    parsed, errors = parse_entries(entries)
    assert errors == [] and len(parsed) == 1


def test_the_copy_is_the_task_plus_the_lane_entries(tmp_path):
    source = TASKS / READ_ONLY_CASE / "task.yaml"
    copy = lane.append_lane_safeguards(source, lane.load_lane_safeguards(LANE_FILE), tmp_path)
    assert copy == tmp_path / READ_ONLY_CASE / "task.yaml"
    original, written = load(source), load(copy)
    assert written["verification_spec"][: len(original["verification_spec"])] == original["verification_spec"]
    appended = written["verification_spec"][len(original["verification_spec"]) :]
    assert [e["name"] for e in appended] == ["no-github-writes-the-case-did-not-request"]
    # Nothing else moved: the prompt, the fixtures, the id devops-bench and
    # the scorer join on.
    assert {k: v for k, v in written.items() if k != "verification_spec"} == {
        k: v for k, v in original.items() if k != "verification_spec"
    }
    assert load_case(copy).case_id == READ_ONLY_CASE
    parsed, errors = parse_entries(written["verification_spec"])
    assert errors == []
    assert len(parsed) == len(original["verification_spec"]) + 1
    # A case that requests nothing gets no allowance, and shares its window.
    leaf = lane._leaves(appended[0]["check"])[0]
    assert lane.REQUESTED_FIELD not in leaf
    assert lane.EXCLUSIVE_WINDOW_FIELD not in leaf


def test_a_case_that_requests_a_pull_request_gets_that_allowance(tmp_path):
    source = TASKS / REQUESTING_CASE / "task.yaml"
    assert lane.requested_pull_requests(load(source)["verification_spec"]) == 1
    copy = lane.append_lane_safeguards(source, lane.load_lane_safeguards(LANE_FILE), tmp_path)
    appended = load(copy)["verification_spec"][-1]
    leaf = lane._leaves(appended["check"])[0]
    assert leaf[lane.REQUESTED_FIELD] == 1
    # It runs alone in the second phase, so its window is its own.
    assert leaf[lane.EXCLUSIVE_WINDOW_FIELD] is True
    # The lane file itself is not mutated between tasks.
    again = lane.append_lane_safeguards(TASKS / READ_ONLY_CASE / "task.yaml", lane.load_lane_safeguards(LANE_FILE), tmp_path)
    again_leaf = lane._leaves(load(again)["verification_spec"][-1]["check"])[0]
    assert lane.REQUESTED_FIELD not in again_leaf
    assert lane.EXCLUSIVE_WINDOW_FIELD not in again_leaf


def test_a_stopped_ask_gets_an_exclusive_window_and_no_allowance(tmp_path):
    # It runs alone in the second phase with the requesting cases, so a write
    # its failed stop lets the card make reds it whether named or not.
    source = TASKS / STOPPED_ASK_CASE / "task.yaml"
    assert stop_hook.is_stopped_ask(load(source)["prompt"])
    assert lane.requested_pull_requests(load(source)["verification_spec"]) == 0
    copy, requested = lane.copy_task(source, lane.load_lane_safeguards(LANE_FILE), tmp_path)
    assert requested == 0
    leaf = lane._leaves(load(copy)["verification_spec"][-1]["check"])[0]
    assert leaf[lane.EXCLUSIVE_WINDOW_FIELD] is True
    assert lane.REQUESTED_FIELD not in leaf
    _parsed, errors = parse_entries(load(copy)["verification_spec"])
    assert errors == []


WRITES_ALONE_CASE = "provisioning-cluster-request-never-creates"


def test_a_writes_alone_case_gets_an_exclusive_window_and_no_allowance(tmp_path):
    # It requests nothing and its prompt is an ordinary ask, but it declares
    # `writes_alone`, so the lane marks its window exclusive and the stop-hook
    # listing places it in the second phase.
    source = TASKS / WRITES_ALONE_CASE / "task.yaml"
    doc = load(source)
    assert doc[lane.WRITES_ALONE_KEY] is True
    assert not stop_hook.is_stopped_ask(doc["prompt"])
    assert lane.requested_pull_requests(doc["verification_spec"]) == 0
    assert lane.runs_alone(doc)
    copy, requested = lane.copy_task(source, lane.load_lane_safeguards(LANE_FILE), tmp_path)
    assert requested == 0
    leaf = lane._leaves(load(copy)["verification_spec"][-1]["check"])[0]
    assert leaf[lane.EXCLUSIVE_WINDOW_FIELD] is True
    assert lane.REQUESTED_FIELD not in leaf
    # Only a bare `true` places a case: a string is not a declaration.
    assert not lane.runs_alone({**doc, lane.WRITES_ALONE_KEY: "true"})


def test_requested_counts_nested_leaves_of_both_requesting_types():
    spec = [
        {"name": "a", "check": {"type": "any", "checks": [{"type": "pull_request_opened"}, {"type": "report_contains"}]}},
        {"name": "b", "check": {"type": "pull_request_diff_contains"}},
        {"name": "c", "check": {"type": "report_contains"}},
    ]
    assert lane.requested_pull_requests(spec) == 2
    assert lane.requested_pull_requests(None) == 0
    assert lane.REQUESTING_CHECK_TYPES == {"pull_request_opened", "pull_request_diff_contains"}


def test_a_name_collision_is_refused_before_anything_is_written(tmp_path):
    task_dir = tmp_path / "tasks" / "clash"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "clash",
                "verification_spec": [
                    {"name": "no-github-writes-the-case-did-not-request", "role": "objective", "check": {"type": "report_contains", "required_phrases": ["x"]}}
                ],
            }
        )
    )
    with pytest.raises(lane.LaneSafeguardsError, match="lane safeguard's name"):
        lane.append_lane_safeguards(task_dir / "task.yaml", lane.load_lane_safeguards(LANE_FILE), tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_a_repository_outside_the_pinned_owner_is_refused_before_the_lease(tmp_path, capsys):
    safeguards = lane.load_lane_safeguards(LANE_FILE)
    lane.check_repository(safeguards, "gke-agentic/kube-agents-evals-21-infra")
    lane.check_repository(safeguards, "GKE-Agentic/x")  # GitHub owners are case-insensitive
    with pytest.raises(lane.LaneSafeguardsError, match="is not under gke-agentic"):
        lane.check_repository(safeguards, "someone/throwaway-infra")
    # The deploy's shape (hack/ci-deploy.sh) and nothing looser: a `?` or `#`
    # would cut the API path short, a trailing newline slips past `$`.
    malformed_slugs = ("no-slash", "gke-agentic/x/y", "gke-agentic/x/", "gke-agentic/ x", "/x", "gke-agentic/x?y", "gke-agentic/x#y", "gke-agentic/x\n")
    for malformed in malformed_slugs:
        with pytest.raises(lane.LaneSafeguardsError, match="not an owner/name"):
            lane.check_repository(safeguards, malformed)
    # An entry that pins nothing accepts any repository.
    lane.check_repository([{"name": "n", "check": {"type": "none", "checks": [{"type": "github_writes"}]}}], "someone/x")
    rc = lane.main(["--safeguards", str(LANE_FILE), "--gitops-repo", "someone/x", "--out-dir", str(tmp_path), str(TASKS / READ_ONLY_CASE / "task.yaml")])
    assert rc == 1
    assert "is not under gke-agentic" in capsys.readouterr().err
    assert not (tmp_path / READ_ONLY_CASE).exists()


def test_a_gitlab_project_is_its_full_path_nested_as_deep_as_its_groups(monkeypatch):
    # GitLab nests groups, so a project is `group/sub/.../project`; the owner
    # a lane entry pins is the top group, as the safeguard itself reads it.
    safeguards = lane.load_lane_safeguards(LANE_FILE)
    monkeypatch.setenv("BENCH_FORGE", "gitlab")
    lane.check_repository(safeguards, "gke-agentic/kube-agents-evals-21-infra")
    lane.check_repository(safeguards, "gke-agentic/pool/evals-21/infra")
    with pytest.raises(lane.LaneSafeguardsError, match="is not under gke-agentic"):
        lane.check_repository(safeguards, "someone/pool/infra")
    for malformed in ("no-slash", "gke-agentic/x/", "gke-agentic//x", "/x", "gke-agentic/x?y", "gke-agentic/x#y", "gke-agentic/x\n"):
        with pytest.raises(lane.LaneSafeguardsError, match="not a GitLab project path"):
            lane.check_repository(safeguards, malformed)


def test_a_third_segment_is_still_refused_on_github(monkeypatch):
    monkeypatch.delenv("BENCH_FORGE", raising=False)
    with pytest.raises(lane.LaneSafeguardsError, match="not an owner/name"):
        lane.check_repository([], "gke-agentic/pool/infra")
    monkeypatch.setenv("BENCH_FORGE", "bitbucket")
    with pytest.raises(lane.LaneSafeguardsError, match="BENCH_FORGE"):
        lane.check_repository([], "gke-agentic/x")


def test_a_task_with_no_spec_gains_one(tmp_path):
    task_dir = tmp_path / "tasks" / "bare"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(yaml.safe_dump({"id": "bare", "prompt": "hi"}))
    copy = lane.append_lane_safeguards(task_dir / "task.yaml", lane.load_lane_safeguards(LANE_FILE), tmp_path / "out")
    assert [e["name"] for e in load(copy)["verification_spec"]] == ["no-github-writes-the-case-did-not-request"]


@pytest.mark.parametrize(
    "text, needle",
    [
        ("- not a mapping\n", "safeguards:` list"),
        ("safeguards: {}\n", "safeguards:` list"),
        ("safeguards:\n  - role: safeguard\n    check: {type: none}\n", "needs a name"),
        ("safeguards:\n  - name: x\n    role: objective\n    check: {type: none}\n", "is a safeguard"),
        ("safeguards:\n  - name: x\n    role: safeguard\n", "needs a `check:` mapping"),
        ("safeguards:\n  - name: x\n    role: safeguard\n    check: {type: none}\n  - name: x\n    role: safeguard\n    check: {type: none}\n", "duplicate"),
        ("safeguards: [\n", "not parseable"),
    ],
)
def test_a_malformed_lane_file_is_refused(tmp_path, text, needle):
    path = tmp_path / "lane.yaml"
    path.write_text(text)
    with pytest.raises(lane.LaneSafeguardsError, match=needle):
        lane.load_lane_safeguards(path)


def test_a_missing_lane_file_or_task_is_refused(tmp_path):
    with pytest.raises(lane.LaneSafeguardsError, match="no such lane"):
        lane.load_lane_safeguards(tmp_path / "nope.yaml")
    with pytest.raises(lane.LaneSafeguardsError, match="no such task"):
        lane.append_lane_safeguards(tmp_path / "nope" / "task.yaml", [], tmp_path)


def test_the_cli_prints_case_and_path_per_task_and_fails_loudly(tmp_path, capsys):
    rc = lane.main(
        ["--safeguards", str(LANE_FILE), "--out-dir", str(tmp_path), str(TASKS / READ_ONLY_CASE / "task.yaml"), str(TASKS / REQUESTING_CASE / "task.yaml")]
    )
    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    # The count first, the case second, the path last (it may hold spaces):
    # the script reads the first two fields to build the fan-out's second
    # phase. The read-only case counts nothing, the remediation case one
    # through its own check.
    assert out == [
        f"0 {READ_ONLY_CASE} {tmp_path / READ_ONLY_CASE / 'task.yaml'}",
        f"1 {REQUESTING_CASE} {tmp_path / REQUESTING_CASE / 'task.yaml'}",
    ]
    # A task's own `requested_pull_requests:` key reaches the line too.
    declared = declared_task(tmp_path / "declared-src", READ_ONLY_CASE, DECLARED_COUNT)
    rc = lane.main(["--safeguards", str(LANE_FILE), "--out-dir", str(tmp_path / "declared"), str(declared)])
    assert rc == 0
    assert capsys.readouterr().out.splitlines() == [f"{DECLARED_COUNT} {READ_ONLY_CASE} {tmp_path / 'declared' / READ_ONLY_CASE / 'task.yaml'}"]


def test_the_copies_have_somewhere_to_go():
    """Both lanes take the copies, so there is no mode that writes none."""
    with pytest.raises(SystemExit):
        lane.main(["--safeguards", str(LANE_FILE), str(TASKS / READ_ONLY_CASE / "task.yaml")])


def declared_task(where: Path, case: str, count) -> Path:
    """A copy of a shipped task under ``where/<case>/task.yaml`` that declares
    ``requested_pull_requests: <count>`` at its top level."""
    source = load(TASKS / case / "task.yaml")
    target = where / case / "task.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(yaml.safe_dump({**source, lane.REQUESTED_KEY: count}, sort_keys=False))
    return target


def test_a_lane_file_that_still_carries_the_retired_requesting_map_is_refused(tmp_path):
    """The count moved into each task's own key; a map left in the lane file
    would be an allowance nothing reads, so the file is refused."""
    path = tmp_path / "lane.yaml"
    path.write_text(yaml.safe_dump({"requesting": {READ_ONLY_CASE: 2}, "safeguards": lane.load_lane_safeguards(LANE_FILE)}))
    with pytest.raises(lane.LaneSafeguardsError, match="`requesting:` is retired"):
        lane.load_lane_safeguards(path)
    assert lane.RETIRED_REQUESTING_KEY not in load(LANE_FILE)


def test_a_tasks_own_requested_pull_requests_key_sets_its_allowance(tmp_path):
    """The task's top-level key is the count's home both the lane and the
    pinned grader read; the larger of the key and the task's requesting
    leaves is the allowance the copy carries."""
    safeguards = lane.load_lane_safeguards(LANE_FILE)
    source = load(TASKS / REQUESTING_CASE / "task.yaml")
    own = lane.requested_pull_requests(source["verification_spec"])
    declared = declared_task(tmp_path / "declared", REQUESTING_CASE, own + 1)
    copy, requested = lane.copy_task(declared, safeguards, tmp_path / "out")
    assert requested == own + 1
    assert lane._leaves(load(copy)["verification_spec"][-1]["check"])[0][lane.REQUESTED_FIELD] == own + 1
    # A key below the leaves changes nothing, and a task with no key keeps its leaves.
    below = declared_task(tmp_path / "below", REQUESTING_CASE, 0)
    assert lane.copy_task(below, safeguards, tmp_path / "out2")[1] == own
    assert lane.declared_pull_requests(source) == 0
    assert lane.requested_for(source["verification_spec"], 3) == 3


def test_the_spent_branch_case_declares_both_its_pull_requests():
    """It opens one pull request and closes it to spend the branch name, then
    opens the one its check grades, and the reply names both: its one leaf
    would allow one, so its task declares two."""
    doc = load(TASKS / "vcs-spent-branch-reuse" / "task.yaml")
    assert lane.requested_pull_requests(doc["verification_spec"]) == 1
    assert lane.declared_pull_requests(doc) == 2


@pytest.mark.parametrize("value", [True, "2", -1])
def test_a_requested_pull_requests_key_that_is_not_a_count_is_refused_by_the_lane(tmp_path, value):
    bad = declared_task(tmp_path, READ_ONLY_CASE, value)
    with pytest.raises(lane.LaneSafeguardsError, match="must be a non-negative integer"):
        lane.copy_task(bad, lane.load_lane_safeguards(LANE_FILE), tmp_path / "out")


def test_the_cli_line_survives_a_path_with_a_space(tmp_path, capsys):
    out_dir = tmp_path / "scratch dir"
    rc = lane.main(["--safeguards", str(LANE_FILE), "--out-dir", str(out_dir), str(TASKS / REQUESTING_CASE / "task.yaml")])
    assert rc == 0
    line = capsys.readouterr().out.splitlines()[0]
    count, case, path = line.split(" ", 2)
    assert (count, case) == ("1", REQUESTING_CASE)
    assert Path(path) == out_dir / REQUESTING_CASE / "task.yaml"
    rc = lane.main(["--safeguards", str(tmp_path / "missing.yaml"), "--out-dir", str(tmp_path), str(TASKS / READ_ONLY_CASE / "task.yaml")])
    assert rc == 1
    assert "ERROR:" in capsys.readouterr().err


def test_every_registered_task_takes_the_lane_entries(tmp_path):
    """No shipped task declares an entry named like a lane safeguard, and
    every copy still parses through devops-bench with no error."""
    safeguards = lane.load_lane_safeguards(LANE_FILE)
    for source in sorted(TASKS.glob("*/task.yaml")):
        copy = lane.append_lane_safeguards(source, safeguards, tmp_path)
        _, errors = parse_entries(load(copy)["verification_spec"])
        assert errors == [], (source, errors)
