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

    def __init__(
        self, crd=True, namespace_owner=None, ready="False", fail_on=None, mode=None, context_healthy=True,
        context_stuck=False, kcc_left=0, finalized=None, patch_errors=None,
    ):
        # `patch_errors`: object name -> the stderr its finalizer patch fails with.
        self.patch_errors = patch_errors or {}
        # `context_stuck`: the operator cannot finish the context delete.
        # `kcc_left`: the Config Connector objects still in the namespace.
        # `finalized`: resource -> the objects a list of it in the namespace returns.
        self.context_stuck = context_stuck
        self.kcc_left = kcc_left
        self.finalized = finalized or {}
        self.patched = []
        # `mode` is the ConfigConnector object's `spec.mode`, None with no object.
        self.mode = mode
        self.context_healthy = context_healthy
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
        if argv[:3] == ["kubectl", "get", plant.CONFIG_CONNECTOR_RESOURCE]:
            if self.mode is None:
                return done(1, stderr='Error from server (NotFound): configconnectors "x" not found')
            return done(stdout=json.dumps({"spec": {"mode": self.mode}}))
        if argv[:3] == ["kubectl", "get", "gcp"]:
            return done(stdout=json.dumps({"items": [{"kind": "ContainerNodePool"}] * self.kcc_left}))
        if argv[:2] == ["kubectl", "get"] and argv[3] == "-n":
            return done(stdout=json.dumps({"items": self.finalized.get(argv[2], [])}))
        if argv[:3] == ["kubectl", "get", plant.CONTEXT_RESOURCE]:
            errors = ['cannot list resource "edgenetworknetworks" in the namespace'] if self.context_stuck else []
            return done(stdout=json.dumps({"status": {"healthy": self.context_healthy, "errors": errors}}))
        if argv[:2] == ["kubectl", "patch"]:
            self.patched.append(list(argv))
            if argv[3] in self.patch_errors:
                return done(1, stderr=self.patch_errors[argv[3]])
            return done()
        if argv[:3] == ["kubectl", "delete", plant.CONTEXT_RESOURCE] and self.context_stuck:
            return done(1, stderr="error: timed out waiting for the condition on configconnectorcontexts/x")
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
    task = yaml.safe_load(TASK.read_text(encoding="utf-8"))
    [deciding] = [e for e in task["verification_spec"] if e["name"] == "the-fix-says-config-connector-is-not-applying-its-file"]
    declarations = audit_report.kcc_declarations(tmp_path)
    # A manifest fix of the planted file, and the live run's case: the worker
    # named a file it never wrote, so the fix degraded to manual. The ledger
    # row carries every phrase either way.
    for remediation in (
        {"kind": "manifest", "path": path, "note": "Spread the pool over three zones."},
        {"kind": "manifest", "path": "clusters/acme-seeded-a/pinned-inference-pool.tf", "note": "Spread the pool over three zones."},
    ):
        case = {**finding, "remediation": dict(remediation)}
        audit_report.degrade_missing_remediations([case], tmp_path)
        assert audit_report.disclose_stalled_kcc_remediations([case], collected, tmp_path, declarations) == ["pool-single-zone"]
        row = "\n".join(audit_report.render_finding({**case, "severity": "major", "title": "Single-zone pool", "evidence": {}}))
        for phrase in deciding["check"]["required_phrases"]:
            assert phrase in row, (case["remediation"]["kind"], phrase)
    # Red on main: the worker's note alone carries no phrase.
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


def test_namespaced_mode_gets_a_context_before_the_node_pool():
    """In namespaced mode no controller watches the stack's namespace until
    it holds a ConfigConnectorContext, so the object would never stall."""
    cluster = Cluster(mode="namespaced")
    plant_ = plant.Plant(settings()._replace(google_service_account="kcc-eval@acme.iam.gserviceaccount.com"), run=cluster, http=Forge(), sleep=lambda _s: None)
    plant_.plant()
    assert [d["kind"] for d in cluster.applied] == ["Namespace", "ConfigConnectorContext", "ContainerNodePool"]
    context = cluster.applied[1]
    assert context["metadata"]["name"] == "configconnectorcontext.core.cnrm.cloud.google.com"
    assert context["metadata"]["namespace"] == "eval-kcc-stalled"
    assert context["spec"] == {"googleServiceAccount": "kcc-eval@acme.iam.gserviceaccount.com"}
    assert ["kubectl", "get", plant.CONTEXT_RESOURCE, plant.CONTEXT_NAME, "-n", "eval-kcc-stalled", "-o", "json"] in cluster.calls


def test_cluster_mode_and_no_config_connector_object_get_no_context():
    for mode in ("cluster", None):
        cluster = Cluster(mode=mode)
        new_plant(cluster, Forge()).plant()
        assert [d["kind"] for d in cluster.applied] == ["Namespace", "ContainerNodePool"]


def test_namespaced_mode_without_a_service_account_stops_before_it_writes():
    cluster = Cluster(mode="namespaced")
    with pytest.raises(RuntimeError, match="namespaced mode.*kcc_google_service_account"):
        new_plant(cluster, Forge()).plant()
    assert cluster.applied == []


def test_a_context_that_does_not_come_up_stops_the_plant_in_bounded_time():
    cluster = Cluster(mode="namespaced", context_healthy=False)
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    planted = plant.Plant(
        settings()._replace(google_service_account="sa@acme.iam.gserviceaccount.com"),
        run=cluster, http=Forge(), sleep=sleep, clock=lambda: now[0],
    )
    with pytest.raises(RuntimeError, match="did not report healthy"):
        planted.plant()
    assert now[0] <= plant.CONTEXT_WAIT_S + plant.POLL_S
    assert "ContainerNodePool" not in [d["kind"] for d in cluster.applied]
    assert DELETE_NAMESPACE in cluster.calls


def test_the_teardown_deletes_the_node_pool_before_the_context_and_the_namespace_last():
    cluster = Cluster(namespace_owner="kube-agents-bench", mode="namespaced")
    assert new_plant(cluster, Forge()).teardown()
    deletes = [c[2] for c in cluster.calls if c[:2] == ["kubectl", "delete"]]
    assert deletes == ["containernodepool", plant.CONTEXT_RESOURCE, "namespace"]
    [context_delete] = [c for c in cluster.calls if c[:3] == ["kubectl", "delete", plant.CONTEXT_RESOURCE]]
    assert f"--timeout={plant.DELETE_TIMEOUT_S}s" in context_delete


def test_the_service_account_comes_from_the_environment(monkeypatch):
    env = {
        "KCC_EVAL_PROJECT": "acme", "KCC_EVAL_CLUSTER": "hub", "KCC_EVAL_LOCATION": "us-central1",
        "KCC_EVAL_NAMESPACE": "eval-kcc-stalled", "KCC_EVAL_SLOT": "a", "KCC_EVAL_POOL": "pinned-inference-pool",
        "KCC_EVAL_GITOPS_REPO": "acme/fleet", "KCC_EVAL_GOOGLE_SERVICE_ACCOUNT": "sa@acme.iam.gserviceaccount.com",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert plant.Settings.from_env().google_service_account == "sa@acme.iam.gserviceaccount.com"


def finalized_objects():
    """What the operator leaves in the namespace when it cannot finish the
    context delete: the context and two RoleBindings it made, with its
    finalizer, and one RoleBinding without it."""
    def obj(name, finalizers):
        return {"metadata": {"name": name, "namespace": "eval-kcc-stalled", "finalizers": finalizers}}

    return {
        plant.CONTEXT_RESOURCE: [obj(plant.CONTEXT_NAME, [plant.OPERATOR_FINALIZER])],
        "rolebindings.rbac.authorization.k8s.io": [
            obj("cnrm-admin-binding-eval-kcc-stalled", ["other/finalizer", plant.OPERATOR_FINALIZER]),
            obj("cnrm-manager-ns-binding-eval-kcc-stalled", [plant.OPERATOR_FINALIZER]),
            obj("someone-elses", []),
        ],
    }


def test_a_stuck_context_loses_the_operators_finalizer_after_the_node_pool_is_gone(capsys):
    cluster = Cluster(namespace_owner="kube-agents-bench", mode="namespaced", context_stuck=True, finalized=finalized_objects())
    assert new_plant(cluster, Forge()).teardown()
    names = [patch[3] for patch in cluster.patched]
    assert names == [plant.CONTEXT_NAME, "cnrm-admin-binding-eval-kcc-stalled", "cnrm-manager-ns-binding-eval-kcc-stalled"]
    # The admin binding carries the finalizer second; the patch removes that one only.
    admin = json.loads(cluster.patched[1][-1])
    assert admin == [
        {"op": "test", "path": "/metadata/finalizers/1", "value": plant.OPERATOR_FINALIZER},
        {"op": "remove", "path": "/metadata/finalizers/1"},
    ]
    # The node pool went first, and the namespace delete and a bounded wait follow.
    order = [c[:3] for c in cluster.calls]
    assert order.index(["kubectl", "delete", "containernodepool"]) < order.index(["kubectl", "get", "gcp"]) < order.index(["kubectl", "patch", plant.CONTEXT_RESOURCE])
    assert DELETE_NAMESPACE in cluster.calls
    assert ["kubectl", "wait", "--for=delete", "namespace/eval-kcc-stalled", f"--timeout={plant.NAMESPACE_WAIT_S}s"] in cluster.calls
    err = capsys.readouterr().err
    assert "WARNING" in err and "edgenetworknetworks" in err


def test_a_failed_finalizer_patch_is_recorded_and_the_namespace_delete_still_runs():
    """Review finding: a failed patch stopped the teardown before the
    namespace delete. A failed `test` op is an error that the teardown
    reports after it deletes the namespace and waits; an object that went
    away is not an error."""
    wait = ["kubectl", "wait", "--for=delete", "namespace/eval-kcc-stalled", f"--timeout={plant.NAMESPACE_WAIT_S}s"]
    failed = 'The request is invalid: the server rejected our request due to an error in our request: "test" operation failed'
    for stderr, ok in ((failed, False), ('Error from server (NotFound): rolebindings "x" not found', True)):
        cluster = Cluster(namespace_owner="kube-agents-bench", mode="namespaced", context_stuck=True, finalized=finalized_objects(),
                          patch_errors={"cnrm-admin-binding-eval-kcc-stalled": stderr})
        assert new_plant(cluster, Forge()).teardown() is ok
        # The patch after the failed one still ran, and so did the namespace delete and the wait.
        assert [patch[3] for patch in cluster.patched][-1] == "cnrm-manager-ns-binding-eval-kcc-stalled"
        assert DELETE_NAMESPACE in cluster.calls
        assert wait in cluster.calls


def test_no_finalizer_is_removed_while_a_config_connector_object_remains():
    cluster = Cluster(namespace_owner="kube-agents-bench", mode="namespaced", context_stuck=True, kcc_left=1, finalized=finalized_objects())
    assert not new_plant(cluster, Forge()).teardown()
    assert cluster.patched == []
    assert DELETE_NAMESPACE in cluster.calls


def test_no_finalizer_is_removed_when_the_objects_cannot_be_read():
    cluster = Cluster(namespace_owner="kube-agents-bench", mode="namespaced", context_stuck=True, finalized=finalized_objects(), fail_on="get gcp")
    assert not new_plant(cluster, Forge()).teardown()
    assert cluster.patched == []


def test_the_finalizer_removal_stays_in_the_stacks_namespace():
    objects = finalized_objects()
    objects["rolebindings.rbac.authorization.k8s.io"].append(
        {"metadata": {"name": "elsewhere", "namespace": "kube-system", "finalizers": [plant.OPERATOR_FINALIZER]}}
    )
    cluster = Cluster(namespace_owner="kube-agents-bench", mode="namespaced", context_stuck=True, finalized=objects)
    assert new_plant(cluster, Forge()).teardown()
    assert "elsewhere" not in [patch[3] for patch in cluster.patched]
    for call in cluster.calls:
        if call[:2] in (["kubectl", "patch"], ["kubectl", "get"]) and call[2] != "namespace":
            assert call[call.index("-n") + 1] == "eval-kcc-stalled", call
    assert not [c for c in cluster.calls if "-A" in c or "--all-namespaces" in c]


def test_a_context_delete_that_finishes_strips_nothing():
    cluster = Cluster(namespace_owner="kube-agents-bench", mode="namespaced", finalized=finalized_objects())
    assert new_plant(cluster, Forge()).teardown()
    assert cluster.patched == []
    assert not [c for c in cluster.calls if c[:3] == ["kubectl", "get", "gcp"]]
