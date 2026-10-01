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
from kube_agents_bench import lane
from kube_agents_bench.cases import load_case

REPO_ROOT = Path(__file__).resolve().parents[2]
LANE_FILE = REPO_ROOT / "hack" / "eval" / "inject-lane-safeguards.yaml"
TASKS = REPO_ROOT / "bench" / "tasks"
# A presubmit case that requests no pull request, neither through its own
# checks nor through the file's `requesting:` list, and a nightly case that
# requests one (its objective is a pull_request_opened check). Not
# obtainability-remediation-proposal: it is the file's placeholder, and its
# own check (#2088) turns it into a requesting case in either merge order.
READ_ONLY_CASE = "reliability-pdb-probe"
REQUESTING_CASE = "pdb-remediation-pr"
# The count a scratch lane file's `requesting:` entry gives READ_ONLY_CASE,
# so the listed path is tested without pinning the real file's placeholder,
# whose contents scripts/test_eval_rosters.py owns.
LISTED_COUNT = 2


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
    # A case that requests nothing gets no allowance.
    leaf = lane._leaves(appended[0]["check"])[0]
    assert lane.REQUESTED_FIELD not in leaf


def test_a_case_that_requests_a_pull_request_gets_that_allowance(tmp_path):
    source = TASKS / REQUESTING_CASE / "task.yaml"
    assert lane.requested_pull_requests(load(source)["verification_spec"]) == 1
    copy = lane.append_lane_safeguards(source, lane.load_lane_safeguards(LANE_FILE), tmp_path)
    appended = load(copy)["verification_spec"][-1]
    leaf = lane._leaves(appended["check"])[0]
    assert leaf[lane.REQUESTED_FIELD] == 1
    # The lane file itself is not mutated between tasks.
    again = lane.append_lane_safeguards(TASKS / READ_ONLY_CASE / "task.yaml", lane.load_lane_safeguards(LANE_FILE), tmp_path)
    assert lane.REQUESTED_FIELD not in lane._leaves(load(again)["verification_spec"][-1]["check"])[0]


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
    # A `requesting:` entry reaches the line too.
    listed_file = scratch_lane_file(tmp_path, {READ_ONLY_CASE: LISTED_COUNT})
    rc = lane.main(["--safeguards", str(listed_file), "--out-dir", str(tmp_path / "listed"), str(TASKS / READ_ONLY_CASE / "task.yaml")])
    assert rc == 0
    assert capsys.readouterr().out.splitlines() == [f"{LISTED_COUNT} {READ_ONLY_CASE} {tmp_path / 'listed' / READ_ONLY_CASE / 'task.yaml'}"]


def scratch_lane_file(tmp_path: Path, requesting: dict[str, int]) -> Path:
    """The real lane file's safeguards under a `requesting:` mapping of our own."""
    path = tmp_path / "lane-with-requesting.yaml"
    path.write_text(yaml.safe_dump({"requesting": requesting, "safeguards": lane.load_lane_safeguards(LANE_FILE)}))
    return path


def test_the_real_requesting_list_is_well_formed():
    # Its contents are pinned by scripts/test_eval_rosters.py, which also
    # fails an entry whose count does not exceed what the case's own checks
    # already request.
    listed = lane.load_lane_requesting(LANE_FILE)
    assert all(isinstance(case, str) and count >= 1 for case, count in listed.items())
    assert READ_ONLY_CASE not in listed


def test_the_requesting_list_is_read_and_gives_its_case_an_allowance(tmp_path):
    listed = lane.load_lane_requesting(scratch_lane_file(tmp_path, {READ_ONLY_CASE: LISTED_COUNT}))
    assert listed == {READ_ONLY_CASE: LISTED_COUNT}
    safeguards = lane.load_lane_safeguards(LANE_FILE)
    copy, requested = lane.copy_task(TASKS / READ_ONLY_CASE / "task.yaml", safeguards, tmp_path, listed[READ_ONLY_CASE])
    assert requested == LISTED_COUNT
    assert lane._leaves(load(copy)["verification_spec"][-1]["check"])[0][lane.REQUESTED_FIELD] == LISTED_COUNT
    # The larger of the two counts wins; a case with its own check and no
    # entry keeps its own.
    _, requested = lane.copy_task(TASKS / REQUESTING_CASE / "task.yaml", safeguards, tmp_path / "b", 0)
    assert requested == 1
    _, requested = lane.copy_task(TASKS / REQUESTING_CASE / "task.yaml", safeguards, tmp_path / "c", 3)
    assert requested == 3


@pytest.mark.parametrize(
    "text, needle",
    [
        ("safeguards: []\nrequesting: [a]\n", "must map case ids to counts"),
        ("safeguards: []\nrequesting: {a: 0}\n", "count of at least 1"),
        ("safeguards: []\nrequesting: {a: true}\n", "count of at least 1"),
        ("safeguards: []\nrequesting: {a: one}\n", "count of at least 1"),
    ],
)
def test_a_malformed_requesting_list_is_refused(tmp_path, text, needle):
    path = tmp_path / "lane.yaml"
    path.write_text(text)
    with pytest.raises(lane.LaneSafeguardsError, match=needle):
        lane.load_lane_requesting(path)
    path.write_text("safeguards: []\n")
    assert lane.load_lane_requesting(path) == {}


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
