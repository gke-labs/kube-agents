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

"""The kcc-stalled-declaration plant and the grade of
fleet-audit-manifest-fix-names-stalled-config-connector.

The stack's plant.py runs here with its commands and its GitHub calls replaced
by recorders. The tests show what the case depends on: the planted object is
one Config Connector cannot apply and whose delete cannot delete the pool; the
note `finish` writes for a fix of the planted file carries the phrases the case
requires; and the plant and the teardown remove what they wrote, also when the
plant fails.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
STACK = REPO / "bench" / "tf" / "prebuilt" / "kcc-stalled-declaration"
TASK = REPO / "bench" / "tasks" / "fleet-audit-manifest-fix-names-stalled-config-connector" / "task.yaml"
SCRIPTS = REPO / "agents" / "platform" / "skills" / "fleet-audit" / "scripts"

sys.path[:0] = [str(SCRIPTS), str(REPO / "agents" / "platform" / "scripts")]
import audit_report  # noqa: E402
import kcc_health  # noqa: E402

spec = importlib.util.spec_from_file_location("kcc_stalled_plant", STACK / "plant.py")
plant = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plant)

DELETE_NAMESPACE = ["kubectl", "delete", "namespace", "eval-kcc-stalled", "--ignore-not-found", "--wait=false"]
SEEDED = [{"name": "acme-seeded-a", "location": "us-central1-a"}, {"name": "acme-seeded-b", "location": "us-central1-a"}]
DECLARATION = "clusters/acme-seeded-a/provisioning/pinned-inference-pool.yaml"


def settings():
    task = yaml.safe_load(TASK.read_text(encoding="utf-8"))["infrastructure"]["variables"]
    return plant.Settings(
        project="acme",
        kcc_cluster="hub",
        kcc_location="us-central1",
        namespace=task["kcc_namespace"],
        slot=task["seeded_slot"],
        pool=task["seeded_pool"],
        gitops_repo="acme/fleet",
        token="unused",
    )


def done(rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], rc, stdout, stderr)


class Cluster:
    """The commands plant.py runs, against an in-memory Config Connector cluster."""

    def __init__(self, crd=True, namespace_owner=None, ready="False", fail_on=None):
        self.crd = crd
        self.namespace_owner = namespace_owner
        self.ready = ready
        self.fail_on = fail_on
        self.calls = []
        self.timeouts = []
        self.applied = []

    def __call__(self, argv, *, env=None, stdin=None, timeout=None):
        self.calls.append(list(argv))
        self.timeouts.append(timeout)
        joined = " ".join(argv)
        if self.fail_on and self.fail_on in joined:
            return done(1, stderr="refused")
        if argv[:4] == ["gcloud", "container", "clusters", "list"]:
            return done(stdout=json.dumps(SEEDED))
        if argv[:4] == ["gcloud", "container", "clusters", "get-credentials"]:
            return done()
        if argv[:3] == ["kubectl", "get", "crd"]:
            return done(0 if self.crd else 1)
        if argv[:3] == ["kubectl", "get", "namespace"]:
            if self.namespace_owner is None:
                return done(1, stderr="NotFound")
            return done(stdout=json.dumps({"metadata": {"labels": {"managed-by": self.namespace_owner}}}))
        if argv[:3] == ["kubectl", "get", "containernodepool"]:
            return done(stdout=json.dumps({"status": {"conditions": [{"type": "Ready", "status": self.ready, "reason": "DependencyNotFound"}]}}))
        if argv[:2] == ["kubectl", "apply"]:
            document = json.loads(stdin)
            self.applied.append(document)
            if document["kind"] == "Namespace":
                self.namespace_owner = document["metadata"]["labels"]["managed-by"]
            return done()
        if argv[:2] == ["kubectl", "delete"]:
            if argv[2] == "namespace":
                self.namespace_owner = None
            return done()
        if argv[:2] == ["kubectl", "wait"]:
            return done()
        raise AssertionError(f"unexpected command: {argv}")


class Forge:
    """The GitHub REST calls plant.py makes, against an in-memory repository.

    A pull request is `(number, files)`, from the audit's branch for
    `DECLARATION`, or `(number, files, head branch)`.
    """

    @staticmethod
    def ref(pull):
        return pull[2] if len(pull) > 2 else plant.stack_branch(DECLARATION)

    def __init__(self, files=None, pulls=None):
        self.files = dict(files or {})
        self.pulls = pulls or []
        self.closed = []
        self.calls = []

    def __call__(self, method, path, body):
        self.calls.append((method, path))
        route = path.split("?")[0]
        if method == "GET" and route == "/repos/acme/fleet":
            return 200, {"default_branch": "main"}
        if route.startswith("/repos/acme/fleet/contents/"):
            name = route.removeprefix("/repos/acme/fleet/contents/")
            if method == "GET":
                if name not in self.files:
                    return 404, None
                return 200, {"content": base64.b64encode(self.files[name].encode()).decode(), "sha": "s1"}
            if method == "PUT":
                self.files[name] = base64.b64decode(body["content"]).decode()
                return 201, {}
            if method == "DELETE":
                self.files.pop(name)
                return 200, {}
        if method == "GET" and route == "/repos/acme/fleet/pulls":
            # GitHub's `head=owner:branch` filter.
            query = dict(part.split("=", 1) for part in path.split("?", 1)[1].split("&"))
            head = query.get("head", "").partition(":")[2]
            return 200, [{"number": pull[0], "head": {"ref": self.ref(pull)}} for pull in self.pulls if not head or self.ref(pull) == head]
        if method == "GET" and route.endswith("/files"):
            number = int(route.split("/")[-2])
            return 200, [{"filename": f} for pull in self.pulls if pull[0] == number for f in pull[1]]
        if method == "PATCH" and route.startswith("/repos/acme/fleet/pulls/"):
            self.closed.append(int(route.rsplit("/", 1)[1]))
            return 200, {}
        raise AssertionError(f"unexpected call: {method} {path}")


def new_plant(cluster, forge):
    return plant.Plant(settings(), run=cluster, http=forge, sleep=lambda _s: None)


def test_the_planted_object_cannot_be_applied_and_its_delete_keeps_the_pool():
    manifest = plant.node_pool_manifest(settings(), plant.Target("acme-seeded-a", "us-central1-a"))
    assert manifest["metadata"]["annotations"]["cnrm.cloud.google.com/deletion-policy"] == "abandon"
    # The namespace is the stack's own and holds no ContainerCluster, so this
    # reference is DependencyNotFound before any call to GCP.
    assert manifest["spec"]["clusterRef"] == {"name": "acme-seeded-a"}
    assert manifest["metadata"]["namespace"] == "eval-kcc-stalled"
    assert manifest["spec"]["autoscaling"]["maxNodeCount"] == 1  # the safeguard's value


def test_the_note_finish_writes_for_the_plant_carries_the_cases_phrases(tmp_path):
    target = plant.Target("acme-seeded-a", "us-central1-a")
    manifest = plant.node_pool_manifest(settings(), target)
    path = plant.declaration_path(settings(), target)
    assert path == DECLARATION
    (tmp_path / path).parent.mkdir(parents=True)
    (tmp_path / path).write_text(plant.declaration_text(manifest), encoding="utf-8")
    live = {**manifest, "status": {"conditions": [{"type": "Ready", "status": "False", "reason": "DependencyNotFound", "message": "reference ContainerCluster eval-kcc-stalled/acme-seeded-a is not found"}]}}
    collected = {"clusters": [{"name": "acme/us-central1/hub", "outcome": "collected", "kcc_unready": kcc_health.unready_objects([live])}]}
    finding = {
        "id": "pool-single-zone",
        "check": "pool-single-zone",
        "cluster": "acme/us-central1-a/acme-seeded-a",
        "object": "NodePool/pinned-inference-pool",
        "remediation": {"kind": "manifest", "path": path, "note": "Spread the pool over three zones."},
    }
    assert audit_report.disclose_stalled_kcc_remediations([finding], collected, tmp_path) == ["pool-single-zone"]
    note = finding["remediation"]["note"]
    task = yaml.safe_load(TASK.read_text(encoding="utf-8"))
    [deciding] = [e for e in task["verification_spec"] if e["name"] == "the-fix-says-config-connector-is-not-applying-its-file"]
    for phrase in deciding["check"]["required_phrases"]:
        assert phrase in note
    # Red on main: the worker's note alone carries neither phrase.
    for phrase in deciding["check"]["required_phrases"]:
        assert phrase not in "Spread the pool over three zones."


def test_the_plant_writes_the_object_and_the_declaration():
    cluster, forge = Cluster(), Forge()
    new_plant(cluster, forge).plant()
    assert [d["kind"] for d in cluster.applied] == ["Namespace", "ContainerNodePool"]
    assert cluster.applied[0]["metadata"]["labels"] == {"managed-by": "kube-agents-bench"}
    assert plant.is_ours(forge.files[DECLARATION])
    assert json.loads(forge.files[DECLARATION]) == cluster.applied[1]


def test_a_failed_plant_removes_what_it_wrote():
    """Terraform skips the destroy provisioner of a resource whose create
    failed, so the plant removes the object and the namespace itself."""
    forge = Forge()
    forge_call = forge.__call__

    def refusing(method, path, body):
        return (403, None) if method == "PUT" else forge_call(method, path, body)

    delete_namespace = DELETE_NAMESPACE
    cluster = Cluster()
    with pytest.raises(RuntimeError, match="cannot commit"):
        new_plant(cluster, refusing).plant()
    assert delete_namespace in cluster.calls
    # An object Config Connector reports ready would make the case unable to go red.
    cluster = Cluster(ready="True")
    with pytest.raises(RuntimeError, match="ready"):
        new_plant(cluster, Forge()).plant()
    assert delete_namespace in cluster.calls


def test_the_plant_refuses_a_namespace_it_did_not_label_and_a_cluster_without_config_connector():
    cluster = Cluster(namespace_owner="")
    with pytest.raises(RuntimeError, match="not this stack's"):
        new_plant(cluster, Forge()).plant()
    assert not [c for c in cluster.calls if c[:2] == ["kubectl", "delete"]]
    with pytest.raises(RuntimeError, match="Config Connector is not installed"):
        new_plant(Cluster(crd=False), Forge()).plant()


def test_the_teardown_closes_the_runs_pull_request_and_removes_only_its_own_file():
    cluster = Cluster(namespace_owner="kube-agents-bench")
    forge = Forge(
        files={DECLARATION: plant.declaration_text(plant.node_pool_manifest(settings(), plant.Target("acme-seeded-a", "z")))},
        pulls=[(5, [DECLARATION]), (6, ["clusters/other.yaml"])],
    )
    assert new_plant(cluster, forge).teardown()
    assert forge.closed == [5]
    assert DECLARATION not in forge.files
    assert DELETE_NAMESPACE in cluster.calls
    # A file of the same name that a person wrote stays.
    forge = Forge(files={DECLARATION: "kind: ContainerNodePool\n"})
    assert new_plant(Cluster(), forge).teardown()
    assert DECLARATION in forge.files


def ours():
    return plant.declaration_text(plant.node_pool_manifest(settings(), plant.Target("acme-seeded-a", "z")))


def test_the_stack_branch_is_the_branch_the_audit_opens_for_the_declaration():
    finding = {"id": "pool-single-zone", "remediation": {"kind": "manifest", "path": DECLARATION}}
    assert plant.stack_branch(DECLARATION) == audit_report.group_branch_for(plant.AUDIT_ID, [finding])


def test_the_teardown_closes_only_the_pull_request_from_the_stacks_branch():
    forge = Forge(
        files={DECLARATION: ours()},
        pulls=[(5, [DECLARATION]), (7, [DECLARATION], "someone/edits-the-same-file")],
    )
    assert new_plant(Cluster(namespace_owner="kube-agents-bench"), forge).teardown()
    assert forge.closed == [5]


def test_the_teardown_closes_nothing_on_a_path_the_stack_does_not_own():
    """A plant that refused a file it did not own, or that failed before it
    wrote one, runs teardown; neither may close a pull request on that path."""
    for files in ({DECLARATION: "kind: ContainerNodePool\n"}, {}):
        forge = Forge(files=files, pulls=[(5, [DECLARATION])])
        assert new_plant(Cluster(), forge).teardown()
        assert forge.closed == []
    # The plant itself refuses the person's file, and its teardown closes nothing.
    forge = Forge(files={DECLARATION: "kind: ContainerNodePool\n"}, pulls=[(5, [DECLARATION])])
    with pytest.raises(RuntimeError, match="not this stack's"):
        new_plant(Cluster(), forge).plant()
    assert forge.closed == []


def test_the_object_delete_has_a_time_limit_and_the_namespace_delete_runs_after_it_fails():
    cluster = Cluster(namespace_owner="kube-agents-bench", fail_on="delete containernodepool")
    assert not new_plant(cluster, Forge()).teardown()
    [object_delete] = [c for c in cluster.calls if c[:3] == ["kubectl", "delete", "containernodepool"]]
    assert f"--timeout={plant.DELETE_TIMEOUT_S}s" in object_delete
    assert cluster.timeouts[cluster.calls.index(object_delete)] == plant.DELETE_PROCESS_TIMEOUT_S
    assert DELETE_NAMESPACE in cluster.calls
    assert cluster.calls.index(object_delete) < cluster.calls.index(DELETE_NAMESPACE)


def test_a_command_that_runs_out_of_time_is_a_failed_command():
    result = plant.default_run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.1)
    assert result.returncode == plant.TIMEOUT_RC


def test_a_missing_token_file_stops_only_the_github_half(tmp_path, monkeypatch):
    env = {
        "KCC_EVAL_PROJECT": "acme", "KCC_EVAL_CLUSTER": "hub", "KCC_EVAL_LOCATION": "us-central1",
        "KCC_EVAL_NAMESPACE": "eval-kcc-stalled", "KCC_EVAL_SLOT": "a", "KCC_EVAL_POOL": "pinned-inference-pool",
        "KCC_EVAL_GITOPS_REPO": "acme/fleet", "KCC_EVAL_GITOPS_TOKEN_FILE": str(tmp_path / "gone"),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    loaded = plant.Settings.from_env()
    with pytest.raises(OSError):
        loaded.github_token()
    cluster = Cluster(namespace_owner="kube-agents-bench")
    teardown = plant.Plant(loaded, run=cluster, sleep=lambda _s: None)
    assert not teardown.teardown()
    assert DELETE_NAMESPACE in cluster.calls


def test_a_failed_cluster_list_still_runs_the_kubernetes_half_and_fails_the_teardown(capsys):
    cluster = Cluster(namespace_owner="kube-agents-bench", fail_on="clusters list")
    forge = Forge(files={DECLARATION: ours()}, pulls=[(5, [DECLARATION])])
    assert not new_plant(cluster, forge).teardown()
    assert DELETE_NAMESPACE in cluster.calls
    assert forge.closed == [] and DECLARATION in forge.files
    assert "ERROR: cannot resolve the seeded cluster" in capsys.readouterr().err
