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

import io
import json
from pathlib import Path

import pytest

from kube_agents_bench import scope_exclude

ANNOTATION_PATH = "/metadata/annotations/bench.kubeagents.x-k8s.io~1scope-before-exclude"
SEEDED_B = {"projectId": "pool-1", "location": "us-central1-b", "clusterName": "seeded-b"}
SEEDED_D = {"projectId": "pool-1", "location": "us-central1-a", "clusterName": "seeded-d"}
CONTEXT = (
    "project=pool-1\nslot.crashloop-workload=a\n"
    "cluster.b=seeded-b\nlocation.b=us-central1-b\ncluster.d=seeded-d\nlocation.d=us-central1-a\n"
)


def _task(tmp_path: Path, case: str, body: str) -> Path:
    path = tmp_path / case / "task.yaml"
    path.parent.mkdir()
    path.write_text("id: x\n" + body, encoding="utf-8")
    return path


def _context(tmp_path: Path, text: str = CONTEXT) -> Path:
    """A fleet directory holding the runner's context file."""
    (tmp_path / ".fleet-context").write_text(text, encoding="utf-8")
    return tmp_path


def _apply(cr: dict, ops: list[dict]) -> dict:
    """A JSON patch applied the way the API server applies these ops."""
    doc = json.loads(json.dumps(cr))
    for op in ops:
        *parents, last = [p.replace("~1", "/").replace("~0", "~") for p in op["path"].split("/")[1:]]
        node = doc
        for key in parents:
            node = node[key]
        if op["op"] == "add":
            node[last] = op["value"]
        elif op["op"] == "remove":
            del node[last]
        else:  # pragma: no cover - the module emits only add and remove
            raise AssertionError(op)
    return doc


@pytest.mark.parametrize(
    "body",
    ["scope_exclude: {slots: [b, d]}\n", "scope_exclude:\n  slots:\n    - b\n    - d\n"],
)
def test_every_yaml_layout_reads_the_same_slots(tmp_path, body):
    assert scope_exclude.declared_slots(_task(tmp_path, "c", body)) == ["b", "d"]


def test_an_undeclared_case_excludes_nothing(tmp_path):
    assert scope_exclude.declared_slots(_task(tmp_path, "c", "owner: x\n")) == []


@pytest.mark.parametrize(
    "value",
    ["b", "[b]", "{slots: []}", "{slots: b}", "{slots: [B]}", "{slots: [bb]}", "{slots: [b, b]}", "{slots: [b], x: 1}", "null"],
)
def test_a_value_the_runner_cannot_resolve_fails_the_read(tmp_path, capsys, value):
    task = _task(tmp_path, "bad", f"scope_exclude: {value}\n")
    assert scope_exclude.main(["cases", str(task)]) == 1
    assert str(task) in capsys.readouterr().err


def test_cases_prints_only_the_declaring_tasks_in_order(tmp_path, capsys):
    one = _task(tmp_path, "one", "scope_exclude: {slots: [b]}\n")
    plain = _task(tmp_path, "plain", "owner: x\n")
    two = _task(tmp_path, "two", "scope_exclude: {slots: [b, d]}\n")
    assert scope_exclude.main(["cases", str(one), str(plain), str(two)]) == 0
    assert capsys.readouterr().out.splitlines() == ["one b", "two b d"]


@pytest.mark.parametrize(
    "body",
    ["scope_exclude: {slots: [b, d], absent: 1}\n", "scope_exclude:\n  slots: [b, d]\n  absent: 1\n"],
)
def test_the_absent_count_reads_beside_the_slots(tmp_path, body):
    assert scope_exclude.declared_scope_exclude(_task(tmp_path, "c", body)) == (["b", "d"], 1)
    assert scope_exclude.declared_slots(_task(tmp_path, "d", body)) == ["b", "d"]


@pytest.mark.parametrize("absent", ["0", "-1", "true", "9", "'1'", "1.5"])
def test_an_absent_count_the_runner_cannot_use_fails_the_read(tmp_path, absent):
    with pytest.raises(scope_exclude.ScopeExcludeError):
        scope_exclude.declared_scope_exclude(_task(tmp_path, "c", f"scope_exclude: {{slots: [b], absent: {absent}}}\n"))


def test_cases_passes_the_absent_count_as_a_plus_token(tmp_path, capsys):
    task = _task(tmp_path, "nn", "scope_exclude: {slots: [b, d], absent: 1}\n")
    assert scope_exclude.main(["cases", str(task)]) == 0
    assert capsys.readouterr().out.splitlines() == ["nn b d +1"]


def test_an_absent_cluster_takes_a_letter_past_the_catalogue_in_the_first_slots_place(tmp_path):
    # The catalogue's slots end at d, so the first absent letter is e, whichever slots
    # the runner reached; project and location are the first slot's.
    absent = {"projectId": "pool-1", "location": "us-central1-b", "clusterName": "seeded-e"}
    assert scope_exclude.excluded_clusters(_context(tmp_path), ["b", "d"], 1) == [SEEDED_B, SEEDED_D, absent]
    two = scope_exclude.excluded_clusters(_context(tmp_path), ["d"], 2)
    assert [c["clusterName"] for c in two] == ["seeded-d", "seeded-e", "seeded-f"]


def test_absent_clusters_need_a_slot_named_prefix_dash_letter(tmp_path):
    odd = CONTEXT.replace("cluster.b=seeded-b", "cluster.b=other")
    with pytest.raises(scope_exclude.ScopeExcludeError):
        scope_exclude.excluded_clusters(_context(tmp_path, odd), ["b"], 1)
    with pytest.raises(scope_exclude.ScopeExcludeError):
        scope_exclude.excluded_clusters(_context(tmp_path), [], 1)


def test_split_tokens_reads_the_plus_token_back():
    assert scope_exclude.split_tokens(["b", "d", "+1"]) == (["b", "d"], 1)
    assert scope_exclude.split_tokens(["b"]) == (["b"], 0)


def test_patch_cli_adds_the_absent_clusters(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"metadata": {}, "spec": {}})))
    assert scope_exclude.main(["patch", "--fleet-dir", str(_context(tmp_path)), "--slots", "b", "d", "+1"]) == 0
    clusters = json.loads(capsys.readouterr().out)[-1]["value"]["exclude"]["clusters"]
    assert [c["clusterName"] for c in clusters] == ["seeded-b", "seeded-d", "seeded-e"]


def test_each_slot_resolves_through_the_fleet_context(tmp_path):
    assert scope_exclude.excluded_clusters(_context(tmp_path), ["b", "d"]) == [SEEDED_B, SEEDED_D]


@pytest.mark.parametrize(
    "text", [CONTEXT.replace("location.b=us-central1-b\n", ""), CONTEXT.replace("project=pool-1\n", ""), ""]
)
def test_a_slot_the_context_does_not_resolve_is_an_error(tmp_path, text):
    with pytest.raises(scope_exclude.ScopeExcludeError):
        scope_exclude.excluded_clusters(_context(tmp_path, text), ["b"])


def test_a_cr_without_scope_comes_back_without_one_in_two_steps():
    cr = {"metadata": {"name": "platform-agent"}, "spec": {"mode": "next"}}
    patched = _apply(cr, scope_exclude.patch_ops(cr, [SEEDED_B]))
    assert patched["spec"]["scope"] == {"exclude": {"clusters": [SEEDED_B]}}
    assert patched["metadata"]["annotations"][scope_exclude.ANNOTATION] == "null"
    # The reconcile reads an absent block as "keep the last exclusions", so the
    # block is first emptied, with the annotation kept for the second step.
    cleared = _apply(patched, scope_exclude.restore_ops(patched))
    assert cleared["spec"]["scope"] == {"exclude": {"clusters": []}}
    assert scope_exclude.ANNOTATION in cleared["metadata"]["annotations"]
    # The API server defaults maxProjects into the block it stores; still cleared.
    cleared["spec"]["scope"]["maxProjects"] = 100
    assert _apply(cleared, scope_exclude.restore_ops(cleared)) == {
        "metadata": {"name": "platform-agent", "annotations": {}},
        "spec": {"mode": "next"},
    }


def test_first_step_only_never_removes_the_block():
    excluded = {"metadata": {"annotations": {scope_exclude.ANNOTATION: "null"}}, "spec": {"scope": {"exclude": {"clusters": [SEEDED_B]}}}}
    assert scope_exclude.restore_ops(excluded, first_step_only=True) == [
        {"op": "add", "path": "/spec/scope", "value": {"exclude": {"clusters": []}}}
    ]
    # The emptied block waits for a caller that runs the reconcile first.
    cleared = _apply(excluded, scope_exclude.restore_ops(excluded))
    assert scope_exclude.restore_ops(cleared, first_step_only=True) == []
    assert scope_exclude.restore_ops(cleared)[0] == {"op": "remove", "path": "/spec/scope"}


def test_first_step_only_on_a_declared_scope_keeps_the_annotation():
    scope = {"projects": ["pool-2"], "exclude": {"clusters": []}}
    cr = {"metadata": {"annotations": {scope_exclude.ANNOTATION: json.dumps(scope)}}, "spec": {"scope": {"exclude": {"clusters": [SEEDED_B]}}}}
    assert scope_exclude.restore_ops(cr, first_step_only=True) == [{"op": "add", "path": "/spec/scope", "value": scope}]
    restored = _apply(cr, scope_exclude.restore_ops(cr, first_step_only=True))
    assert scope_exclude.restore_ops(restored, first_step_only=True) == []
    # The settled restore still has the annotation to act on.
    assert scope_exclude.restore_ops(restored) == [{"op": "remove", "path": ANNOTATION_PATH}]


def test_api_defaults_in_the_restored_block_still_mark_the_first_step_done():
    scope = {"exclude": {}}
    cr = {"metadata": {"annotations": {scope_exclude.ANNOTATION: json.dumps(scope)}}, "spec": {"scope": {"exclude": {}, "maxProjects": 100}}}
    assert scope_exclude.restore_ops(cr) == [{"op": "remove", "path": ANNOTATION_PATH}]


@pytest.mark.parametrize("saved", ["[]", "1", '"x"', "true"])
def test_a_saved_scope_that_is_not_an_object_or_null_is_named_not_patched(saved):
    cr = {"metadata": {"annotations": {scope_exclude.ANNOTATION: saved}}, "spec": {"scope": {"exclude": {"clusters": [SEEDED_B]}}}}
    with pytest.raises(scope_exclude.ScopeExcludeError, match="not a scope object or null"):
        scope_exclude.restore_ops(cr)
    with pytest.raises(scope_exclude.ScopeExcludeError):
        scope_exclude.patch_ops(cr, [SEEDED_B])


def test_a_saved_null_with_no_block_left_only_drops_the_annotation():
    cr = {"metadata": {"annotations": {scope_exclude.ANNOTATION: "null"}}, "spec": {}}
    assert scope_exclude.restore_ops(cr) == [{"op": "remove", "path": ANNOTATION_PATH}]


def test_a_declared_scope_keeps_its_own_exclusions_and_comes_back_as_it_was():
    own = {"projectId": "pool-1", "location": "us-east1", "clusterName": "scratch"}
    scope = {"projects": ["pool-2"], "exclude": {"projects": ["*-sandbox"], "clusters": [own, SEEDED_B]}}
    cr = {"metadata": {"annotations": {"keep": "me"}}, "spec": {"scope": scope}}
    patched = _apply(cr, scope_exclude.patch_ops(cr, [SEEDED_B, SEEDED_D]))
    assert patched["spec"]["scope"]["exclude"] == {"projects": ["*-sandbox"], "clusters": [own, SEEDED_B, SEEDED_D]}
    assert patched["spec"]["scope"]["projects"] == ["pool-2"]
    # Two steps: the scope back with the annotation kept, then the annotation.
    scoped = _apply(patched, scope_exclude.restore_ops(patched))
    assert scoped["spec"]["scope"] == scope
    assert scope_exclude.ANNOTATION in scoped["metadata"]["annotations"]
    assert _apply(scoped, scope_exclude.restore_ops(scoped)) == cr


def test_a_patch_over_a_leftover_starts_from_the_saved_scope():
    cr = {"metadata": {}, "spec": {}}
    first = _apply(cr, scope_exclude.patch_ops(cr, [SEEDED_D]))
    second = _apply(first, scope_exclude.patch_ops(first, [SEEDED_B]))
    # The leftover's seeded-d is not carried into the next case's exclusion.
    assert second["spec"]["scope"] == {"exclude": {"clusters": [SEEDED_B]}}
    assert second["metadata"]["annotations"][scope_exclude.ANNOTATION] == "null"
    assert _apply(second, scope_exclude.restore_ops(second))["spec"]["scope"] == {"exclude": {"clusters": []}}


def test_nothing_saved_means_nothing_to_restore(capsys, monkeypatch):
    cr = {"metadata": {"annotations": {}}, "spec": {"scope": {"exclude": {"clusters": [SEEDED_B]}}}}
    assert scope_exclude.restore_ops(cr) == []
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(cr)))
    assert scope_exclude.main(["restore"]) == 0
    assert capsys.readouterr().out == ""


def test_the_annotation_path_escapes_the_slash():
    ops = scope_exclude.patch_ops({"metadata": {"annotations": {"a": "b"}}, "spec": {}}, [SEEDED_B])
    assert [op["path"] for op in ops] == [ANNOTATION_PATH, "/spec/scope"]


def test_a_saved_scope_that_is_not_json_fails_loudly():
    cr = {"metadata": {"annotations": {scope_exclude.ANNOTATION: "{"}}, "spec": {}}
    with pytest.raises(scope_exclude.ScopeExcludeError):
        scope_exclude.restore_ops(cr)


def test_the_cr_and_the_rendered_file_compare_equal_when_they_agree(capsys, monkeypatch):
    cr = {"spec": {"scope": {"exclude": {"clusters": [SEEDED_D, SEEDED_B]}}}}
    rendered = {"present": True, "projects": [], "exclude": {"projects": [], "clusters": [SEEDED_B, SEEDED_D]}}
    lines = []
    for doc in (cr, rendered):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(doc)))
        assert scope_exclude.main(["clusters"]) == 0
        lines.append(capsys.readouterr().out)
    assert lines[0] == lines[1]
    # And an unexcluded install reads as an empty list on both sides.
    for doc in ({"spec": {}}, {"present": False, "exclude": {"projects": [], "clusters": []}}):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(doc)))
        assert scope_exclude.main(["clusters"]) == 0
        assert capsys.readouterr().out == "[]\n"


def test_patch_cli_prints_the_ops(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"metadata": {}, "spec": {}})))
    assert scope_exclude.main(["patch", "--fleet-dir", str(_context(tmp_path)), "--slots", "b"]) == 0
    ops = json.loads(capsys.readouterr().out)
    assert ops[-1] == {"op": "add", "path": "/spec/scope", "value": {"exclude": {"clusters": [SEEDED_B]}}}
