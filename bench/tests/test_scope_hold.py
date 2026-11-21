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

import json
import subprocess
from pathlib import Path

import pytest

from kube_agents_bench import scope_hold

ANNOTATION = "bench.kubeagents.x-k8s.io/scope-hold-before"
CONTEXT = "gke_pool-1_us-central1_platform-agent-host"
HOST = {"projectId": "pool-1", "location": "us-central1", "clusterName": "platform-agent-host"}
REASON = "maintenance window, paused by the platform team"
HOST_HOLD = {"clusters": [HOST], "reason": REASON}
CRD_WITH_HOLD = {
    "spec": {
        "versions": [
            {
                "name": "v1alpha1",
                "served": True,
                "schema": {"openAPIV3Schema": {"properties": {"spec": {"properties": {"scope": {"properties": {"exclude": {}}}, "hold": {}}}}}},
            }
        ]
    }
}
CRD_WITHOUT_HOLD = {
    "spec": {
        "versions": [
            {
                "name": "v1alpha1",
                "served": True,
                "schema": {"openAPIV3Schema": {"properties": {"spec": {"properties": {"scope": {"properties": {"exclude": {}}}}}}}},
            }
        ]
    }
}


def _task(tmp_path: Path, case: str, body: str) -> Path:
    path = tmp_path / case / "task.yaml"
    path.parent.mkdir()
    path.write_text("id: x\n" + body, encoding="utf-8")
    return path


def _merge(target, patch):
    """RFC 7386, as the API server applies a merge patch to a custom resource."""
    if not isinstance(patch, dict):
        return patch
    out = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = _merge(out.get(key), value)
    return out


class FakeCluster:
    """kubectl against one CR, its CRD and the operator's ConfigMap.

    `prune` drops spec.hold from every patch, as a server whose schema
    lacks the field does; `render` is what the operator writes into the
    ConfigMap after a patch (None: it writes nothing).
    """

    def __init__(self, cr, crd=CRD_WITH_HOLD, render=None, prune=False):
        self.cr, self.crd, self.render, self.prune = cr, crd, render, prune
        self.configmap = None
        self.patches: list[dict] = []

    def __call__(self, *args, stdin=None):
        def ok(doc):
            return subprocess.CompletedProcess(args, 0, json.dumps(doc), "")

        if args[:2] == ("get", "crd"):
            return ok(self.crd)
        if args[:2] == ("get", "platformagent"):
            return ok(self.cr)
        if args[:2] == ("get", "configmap"):
            if self.configmap is None:
                return subprocess.CompletedProcess(args, 1, "", "NotFound")
            return ok(self.configmap)
        if args[:2] == ("patch", "platformagent"):
            body = json.loads(args[args.index("-p") + 1])
            self.patches.append(body)
            self.cr = _merge(self.cr, body)
            if self.prune:
                self.cr.get("spec", {}).pop("hold", None)
            if self.render is not None:
                self.configmap = self.render(self.cr)
            return subprocess.CompletedProcess(args, 0, "patched", "")
        raise AssertionError(f"unexpected kubectl {args}")


def _render_json(cr):
    hold = cr.get("spec", {}).get("hold")
    return {"data": {"hold.json": json.dumps(hold or {})}}


def _apply(cluster, hold=HOST_HOLD):
    slept = []
    rc = scope_hold.apply(cluster, "platform-agent", hold, sleep=slept.append, clock=lambda: len(slept), log=lambda _m: None)
    return rc, slept


# -- the key --


@pytest.mark.parametrize(
    "body,expected",
    [
        (f"scope_hold: {{agent_cluster: true, reason: '{REASON}'}}\n", {"agent_cluster": True, "reason": REASON}),
        ("scope_hold:\n  all: true\n  reason: paused\n", {"all": True, "reason": "paused"}),
        ("scope_hold: {all: true, agent_cluster: false, reason: paused}\n", {"all": True, "agent_cluster": False, "reason": "paused"}),
    ],
)
def test_a_declared_hold_reads_back(tmp_path, body, expected):
    assert scope_hold.declared_hold(_task(tmp_path, "c", body)) == expected


def test_an_undeclared_case_holds_nothing(tmp_path):
    assert scope_hold.declared_hold(_task(tmp_path, "c", "owner: x\n")) is None


@pytest.mark.parametrize(
    "value",
    [
        "null",
        "true",
        "{reason: paused}",
        "{all: false, reason: paused}",
        "{all: 'true', reason: paused}",
        "{all: true}",
        "{all: true, reason: ''}",
        "{all: true, reason: '  '}",
        "{all: true, reason: 3}",
        "{all: true, reason: paused, clusters: [a]}",
        "{agent_cluster: yes please, reason: paused}",
    ],
)
def test_a_value_the_runner_cannot_apply_fails_the_read(tmp_path, capsys, value):
    task = _task(tmp_path, "bad", f"scope_hold: {value}\n")
    assert scope_hold.main(["cases", str(task)]) == 1
    assert "'scope_hold:' must be" in capsys.readouterr().err


def test_a_reason_past_the_cap_is_refused():
    with pytest.raises(scope_hold.ScopeHoldError):
        scope_hold.validate({"all": True, "reason": "x" * (scope_hold.MAX_REASON_CHARS + 1)})


def test_cases_prints_each_declaring_case_with_its_hold_on_one_line(tmp_path, capsys):
    held = _task(tmp_path, "held", f"scope_hold: {{agent_cluster: true, reason: '{REASON}'}}\n")
    plain = _task(tmp_path, "plain", "owner: x\n")
    assert scope_hold.main(["cases", str(plain), str(held)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    name, hold = lines[0].split(" ", 1)
    assert name == "held"
    assert json.loads(hold) == {"agent_cluster": True, "reason": REASON}


# -- resolving the agent's cluster --


def test_the_agent_cluster_is_read_from_the_gke_context():
    assert scope_hold.resolved_hold({"agent_cluster": True, "reason": REASON}, CONTEXT) == HOST_HOLD
    assert scope_hold.resolved_hold({"all": True, "agent_cluster": True, "reason": "r"}, CONTEXT) == {
        "all": True,
        "clusters": [HOST],
        "reason": "r",
    }


def test_all_needs_no_gke_context():
    assert scope_hold.resolved_hold({"all": True, "reason": "r"}, "kind-dev") == {"all": True, "reason": "r"}


@pytest.mark.parametrize("context", ["kind-dev", "gke_pool-1_us-central1", "gke_Pool_us_central1_x_y"])
def test_a_context_that_names_no_gke_cluster_is_refused(context):
    with pytest.raises(scope_hold.ScopeHoldError, match="gke_<project>"):
        scope_hold.resolved_hold({"agent_cluster": True, "reason": "r"}, context)


# -- the patches --


def test_the_patch_sets_spec_hold_alone_and_the_restore_clears_it():
    cr = {"metadata": {"name": "platform-agent"}, "spec": {"mode": "next"}}
    patched = _merge(cr, scope_hold.patch_body(cr, HOST_HOLD))
    assert patched["spec"] == {"mode": "next", "hold": HOST_HOLD}
    assert json.loads(patched["metadata"]["annotations"][ANNOTATION]) == {"hold": None}
    restored = _merge(patched, scope_hold.restore_body(patched))
    assert restored == {"metadata": {"name": "platform-agent", "annotations": {}}, "spec": {"mode": "next"}}


def test_the_patch_and_restore_leave_a_scope_block_alone():
    # The bug spec.hold fixes: under spec.scope, a hold patched onto a CR with no
    # scope block left a defaulted {maxProjects: 100} behind after the restore,
    # which the reconcile reads as the declaration that drops projects.
    for spec in ({}, {"scope": {"maxProjects": 100, "exclude": {"clusters": []}}}):
        cr = {"metadata": {}, "spec": json.loads(json.dumps(spec))}
        patched = _merge(cr, scope_hold.patch_body(cr, HOST_HOLD))
        assert patched["spec"].get("scope") == spec.get("scope")
        restored = _merge(patched, scope_hold.restore_body(patched))
        assert restored["spec"] == spec
        assert ANNOTATION not in restored["metadata"]["annotations"]


def test_an_earlier_hold_is_replaced_whole_and_put_back():
    earlier = {"all": True, "reason": "freeze"}
    cr = {"metadata": {}, "spec": {"hold": earlier}}
    patched = _merge(cr, scope_hold.patch_body(cr, HOST_HOLD))
    # `all` is nulled, not left beside the new clusters.
    assert patched["spec"]["hold"] == HOST_HOLD
    assert json.loads(patched["metadata"]["annotations"][ANNOTATION]) == {"hold": earlier}
    restored = _merge(patched, scope_hold.restore_body(patched))
    assert restored["spec"]["hold"] == earlier


def test_a_patch_over_a_leftover_starts_from_the_saved_hold():
    cr = {"metadata": {}, "spec": {}}
    leftover = _merge(cr, scope_hold.patch_body(cr, {"all": True, "reason": "old run"}))
    again = _merge(leftover, scope_hold.patch_body(leftover, HOST_HOLD))
    assert json.loads(again["metadata"]["annotations"][ANNOTATION]) == {"hold": None}
    assert _merge(again, scope_hold.restore_body(again))["spec"] == {}


def test_nothing_is_restored_without_a_saved_hold():
    assert scope_hold.restore_body({"metadata": {}, "spec": {"hold": HOST_HOLD}}) is None


def test_the_restore_on_a_server_that_pruned_the_hold_only_drops_the_annotation():
    cr = {"metadata": {"annotations": {ANNOTATION: json.dumps({"hold": None})}}, "spec": {}}
    assert scope_hold.restore_body(cr) == {"metadata": {"annotations": {ANNOTATION: None}}}


@pytest.mark.parametrize("value", ["{", "[]", "{}", '{"hold": 3}', '{"hold": "x"}'])
def test_an_annotation_the_runner_did_not_write_is_named(value):
    with pytest.raises(scope_hold.ScopeHoldError, match="annotation"):
        scope_hold.restore_body({"metadata": {"annotations": {ANNOTATION: value}}, "spec": {}})


# -- the CRD and the ConfigMap --


def test_the_crd_schema_tells_whether_the_field_exists():
    assert scope_hold.crd_declares_hold(CRD_WITH_HOLD)
    assert not scope_hold.crd_declares_hold(CRD_WITHOUT_HOLD)
    unserved = json.loads(json.dumps(CRD_WITH_HOLD))
    unserved["spec"]["versions"][0]["served"] = False
    assert not scope_hold.crd_declares_hold(unserved)


def test_the_configmap_is_read_from_hold_json():
    assert scope_hold.held_in_configmap({"data": {"hold.json": json.dumps(HOST_HOLD) + "\n"}}, HOST_HOLD)
    assert scope_hold.held_in_configmap({"data": {"hold.json": json.dumps({"all": True})}}, {"all": True, "reason": "r"})


def test_a_configmap_without_the_cluster_or_all_does_not_hold():
    assert not scope_hold.held_in_configmap({"data": {"hold.json": "{}"}}, HOST_HOLD)
    other = {**HOST, "clusterName": "seeded-a"}
    assert not scope_hold.held_in_configmap({"data": {"hold.json": json.dumps({"clusters": [other]})}}, HOST_HOLD)
    assert not scope_hold.held_in_configmap({"data": {"hold.json": json.dumps({"all": False})}}, {"all": True, "reason": "r"})
    # Only hold.json counts: another key, or a body that is not a JSON object, holds nothing.
    assert not scope_hold.held_in_configmap({"data": {"scope-hold": json.dumps(HOST_HOLD)}}, HOST_HOLD)
    assert not scope_hold.held_in_configmap({"data": {"hold.json": "all: true\n"}}, {"all": True, "reason": "r"})
    assert not scope_hold.held_in_configmap({"data": {"hold.json": "[]"}}, {"all": True, "reason": "r"})
    assert not scope_hold.held_in_configmap({}, HOST_HOLD)


# -- apply and restore against kubectl --


def test_apply_patches_waits_for_the_configmap_then_the_pods():
    cluster = FakeCluster({"metadata": {}, "spec": {}}, render=_render_json)
    rc, slept = _apply(cluster)
    assert rc == 0
    assert len(cluster.patches) == 1
    assert cluster.cr["spec"]["hold"] == HOST_HOLD
    assert slept == [scope_hold.PROPAGATION_SECONDS]


def test_apply_on_an_api_without_the_field_patches_nothing():
    cluster = FakeCluster({"metadata": {}, "spec": {}}, crd=CRD_WITHOUT_HOLD)
    rc, slept = _apply(cluster)
    assert rc == scope_hold.EXIT_UNSUPPORTED
    assert cluster.patches == []
    assert slept == []


def test_apply_fails_when_the_cr_does_not_keep_the_hold():
    cluster = FakeCluster({"metadata": {}, "spec": {}}, render=_render_json, prune=True)
    with pytest.raises(scope_hold.ScopeHoldError, match="does not keep"):
        _apply(cluster)
    # The annotation went on with the patch, so a restore can take it off.
    assert ANNOTATION in cluster.cr["metadata"]["annotations"]


def test_apply_fails_when_the_operator_never_writes_the_configmap():
    cluster = FakeCluster({"metadata": {}, "spec": {}})
    with pytest.raises(scope_hold.ScopeHoldError, match="does not carry the hold"):
        _apply(cluster)


def test_restore_puts_the_cr_back_and_waits_only_when_asked():
    cluster = FakeCluster({"metadata": {}, "spec": {}}, render=_render_json)
    _apply(cluster)
    slept = []
    assert scope_hold.restore(cluster, "platform-agent", wait=True, sleep=slept.append, log=lambda _m: None) == 0
    assert cluster.cr == {"metadata": {"annotations": {}}, "spec": {}}
    assert slept == [scope_hold.PROPAGATION_SECONDS]
    # A second restore finds nothing saved and patches nothing.
    patches = len(cluster.patches)
    assert scope_hold.restore(cluster, "platform-agent", wait=True, sleep=slept.append, log=lambda _m: None) == 0
    assert len(cluster.patches) == patches


def test_main_reads_the_hold_from_the_task_and_resolves_the_context(tmp_path, monkeypatch):
    task = _task(tmp_path, "c", f"scope_hold: {{agent_cluster: true, reason: '{REASON}'}}\n")
    cluster = FakeCluster({"metadata": {}, "spec": {}}, render=_render_json)
    monkeypatch.setattr(scope_hold.time, "sleep", lambda _s: None)
    argv = ["apply", "--task", str(task), "--context", CONTEXT, "--namespace", "kubeagents-system", "--name", "platform-agent"]
    assert scope_hold.main(argv, kubectl=cluster) == 0
    assert cluster.cr["spec"]["hold"] == HOST_HOLD


def test_main_reports_a_failed_apply_as_exit_1(capsys, monkeypatch):
    cluster = FakeCluster({"metadata": {}, "spec": {}})
    monkeypatch.setattr(scope_hold.time, "sleep", lambda _s: None)
    monkeypatch.setattr(scope_hold, "SETTLE_SECONDS", 0)
    argv = ["apply", "--hold", json.dumps({"all": True, "reason": "r"}), "--context", CONTEXT, "--namespace", "ns", "--name", "platform-agent"]
    assert scope_hold.main(argv, kubectl=cluster) == 1
    assert "does not carry the hold" in capsys.readouterr().err
