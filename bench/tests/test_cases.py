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

"""Tests for ``task.yaml`` parsing.

This module replaces two ``grep`` parsers in ``hack/ci-eval-pr.sh``
(``task_deployer`` and ``task_has_spec``), so the sweep over every real task
file matters more than the unit cases: it is the evidence that the replacement
reads the repository the same way the shell did, on the files that actually
ship.
"""

from __future__ import annotations

import pytest

from kube_agents_bench.cases import NOOP_DEPLOYER, CaseSpecError, load_case

from conftest import TASKS


def _task_files():
    return sorted(p for p in TASKS.glob("*/task.yaml"))


def test_the_sweep_finds_the_task_files():
    """Guard the sweep below: an empty glob would make it vacuously green."""
    assert len(_task_files()) >= 12


@pytest.mark.parametrize("path", _task_files(), ids=lambda p: p.parent.name)
def test_every_shipped_task_parses(path):
    """Every task in the repository is gradeable, and its id agrees.

    `load_case` raises when a declared `id:`/`task_id:` disagrees with the
    directory, so this doubles as the assertion that no shipped task would
    score against the wrong baseline file.
    """
    spec = load_case(path)
    assert spec.case_id == path.parent.name
    assert spec.name
    assert spec.deployer


def test_kanban_smoke_reads_as_the_shell_read_it(kanban_task):
    """The one active noop task, field for field."""
    spec = load_case(kanban_task)
    assert spec.case_id == "agent-kanban-smoke"
    assert spec.name == "Agent kanban smoke test"
    assert spec.domain == "chat-and-routing"
    assert spec.deployer == NOOP_DEPLOYER
    assert spec.declares_verification_spec is True
    assert spec.expected_fail is False


def test_task_id_spelling_is_accepted():
    """`gpu-stress-test-diagnosis` declares `task_id:`, not `id:`.

    Both spellings are live in the repository. Rejecting either would make the
    scorer unable to grade a task that ships today.
    """
    spec = load_case(TASKS / "gpu-stress-test-diagnosis" / "task.yaml")
    assert spec.case_id == "gpu-stress-test-diagnosis"
    assert spec.deployer != NOOP_DEPLOYER


def test_declared_id_disagreeing_with_the_directory_is_fatal(write_task):
    path = write_task("planted-pdb", {"id": "planted-pdb-v2", "name": "x"})
    with pytest.raises(CaseSpecError, match="but lives in directory"):
        load_case(path)


def test_declared_task_id_disagreeing_with_the_directory_is_fatal(write_task):
    path = write_task("planted-pdb", {"task_id": "something-else", "name": "x"})
    with pytest.raises(CaseSpecError, match="but lives in directory"):
        load_case(path)


def test_missing_infrastructure_block_means_noop(write_task):
    """devops-bench's own default, and the carve-out the scorer keys on."""
    spec = load_case(write_task("no-infra", {"id": "no-infra", "name": "x"}))
    assert spec.deployer == NOOP_DEPLOYER


def test_infrastructure_that_is_not_a_mapping_is_fatal(write_task):
    path = write_task("bad-infra", {"id": "bad-infra", "infrastructure": ["tofu"]})
    with pytest.raises(CaseSpecError, match="must be a mapping"):
        load_case(path)


def test_empty_verification_spec_is_not_a_declaration(write_task):
    """`verification_spec: []` produces no checks.

    Counting it as a declaration would trip rung 2 on every run of a task that
    deliberately has no checks yet -- the fail-closed branch firing on a task
    that closed nothing.
    """
    path = write_task("empty-spec", {"id": "empty-spec", "verification_spec": []})
    assert load_case(path).declares_verification_spec is False


def test_a_populated_verification_spec_is_a_declaration(write_task):
    path = write_task(
        "has-spec",
        {"id": "has-spec", "verification_spec": [{"report_contains": {"phrases": ["x"]}}]},
    )
    assert load_case(path).declares_verification_spec is True


def test_expected_fail_defaults_to_false(write_task):
    """No shipped task declares the marker, so absent must mean false."""
    assert load_case(write_task("plain", {"id": "plain"})).expected_fail is False


def test_expected_fail_true_is_read(write_task):
    assert load_case(write_task("edd", {"id": "edd", "expected_fail": True})).expected_fail is True


@pytest.mark.parametrize("literal", ['"false"', '"true"', "'no'", "1"])
def test_a_non_boolean_expected_fail_is_fatal(write_task, literal):
    """A quoted `"false"` is a truthy string.

    Silently accepting it would invert the case's verdict -- rung 5 would
    demand a marker flip on a case that is passing correctly. Refusing beats
    guessing which of the two the author meant.
    """
    path = write_task("quoted", f"id: quoted\nexpected_fail: {literal}\n")
    with pytest.raises(CaseSpecError, match="must be a YAML boolean"):
        load_case(path)


def test_a_missing_task_file_is_fatal(tmp_path):
    with pytest.raises(CaseSpecError, match="no such task file"):
        load_case(tmp_path / "nope" / "task.yaml")


def test_a_top_level_list_is_fatal(write_task):
    with pytest.raises(CaseSpecError, match="expected a YAML mapping"):
        load_case(write_task("listy", "- a\n- b\n"))


def test_unparseable_yaml_is_fatal(write_task):
    with pytest.raises(CaseSpecError, match="not parseable as YAML"):
        load_case(write_task("broken", "id: [unclosed\n"))


def test_yaml_is_loaded_safely(write_task):
    """A task file may not construct Python objects.

    This parser runs over whatever a pull request adds to `bench/tasks/`, in
    CI, so `yaml.load` here would be arbitrary code execution on a fork PR.
    """
    path = write_task("evil", "id: evil\nname: !!python/object/apply:os.system ['true']\n")
    with pytest.raises(CaseSpecError, match="not parseable as YAML"):
        load_case(path)
