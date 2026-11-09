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

"""The context-repository-owns-the-fix stack of
remediation-context-repository-owns-the-fix.

``bench/tf/prebuilt/context-repository-owns-the-fix/plant.py`` runs here
against a recorder in place of GitHub and a fake in place of gcloud and
kubectl. The tests show what the case depends on: the plant commits the four
fixture files with the context repository's words replaced and the stack's
label, applies the workload into a namespace it labels, adds the
registration with its ref and waits until both mounts carry it; it refuses a
file or a namespace that is not the stack's and writes again over its own
leftover; the teardown removes the registration alone, the namespace when
labelled, the agent's pull requests under the directory and only the stack's
files; and the stack, the task, the fixture, the reset and the runner agree
on the paths, the labels and the inputs.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
CASE_DIR = REPO / "bench" / "tasks" / "remediation-context-repository-owns-the-fix"
STACK = REPO / "bench" / "tf" / "prebuilt" / "context-repository-owns-the-fix"
FIXTURE = CASE_DIR / "fixture"
TASK = CASE_DIR / "task.yaml"
GITOPS = "gke-agentic/kube-agents-evals-2-infra"
CLUSTER = "platform-agent-host"
DIRECTORY = f"clusters/{CLUSTER}/demo-shop/payments"
RELEASE = f"{DIRECTORY}/release.yaml"
SOURCE = f"{DIRECTORY}/source.yaml"
BOT = "kube-agents-evals[bot]"
CONTEXT_URL = "https://github.com/gke-agentic/kube-agents-evals-platform-charts"
CONTEXT_ENTRY = {"type": "github", "url": CONTEXT_URL, "ref": "main"}
OTHER_ENTRY = {"type": "github", "url": "https://github.com/kubernetes-sigs/kind", "ref": "v0.24.0"}
CONFIGMAP = "platform-agent-gitops-state"
NAMESPACE = "demo-shop"

_spec = importlib.util.spec_from_file_location("context_fix_plant", STACK / "plant.py")
plant = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plant)


class FakeGitHub:
    """A repository with files on `main` and open pull requests; records writes."""

    def __init__(self, files=None, pulls=None, refuse=(), landed_then=None):
        self.files = dict(files or {})
        self.pulls = list(pulls or [])
        self.refuse = set(refuse)
        self.landed_then = dict(landed_then or {})
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
                return self.landed_then.get(("PUT", f"/contents/{name}"), 201), {}
            if method == "DELETE":
                assert body["sha"] == f"sha-{name}"
                del self.files[name]
                return self.landed_then.get(("DELETE", f"/contents/{name}"), 200), {}
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


class FakeHost:
    """gcloud and kubectl against one agent host: a namespace with labels, a
    state ConfigMap, and two mounts that lag the ConfigMap by `mount_lag`
    reads. Records every argv."""

    def __init__(self, namespace_labels=None, configmap=None, mount_lag=0, fail=()):
        # None: no namespace; {}: a namespace with no labels.
        self.namespace_labels = namespace_labels
        self.configmap = dict(configmap or {})
        self.mount_lag = mount_lag
        self.fail = set(fail)
        self.calls: list[list[str]] = []
        self.applied: list[str] = []
        self.mount_reads = 0
        self.mount_value_at_lag_start = self.configmap.get("context_repos")

    def __call__(self, argv, stdin):
        self.calls.append(argv)
        if argv[0] == "env":
            assert argv[2:6] == ["gcloud", "container", "clusters", "get-credentials"], argv
            return 0, "", ""
        assert argv[:2] == ["kubectl", "--kubeconfig"], argv
        args = argv[3:]
        if args[0] == "-n":
            args = args[2:]
        verb = args[0]
        if verb in self.fail:
            return 1, "", f"{verb} refused by the fake"
        if verb == "get" and args[1] == "namespace":
            assert "--ignore-not-found" in args and "json" in args, args
            if self.namespace_labels is None:
                return 0, "", ""
            return 0, json.dumps({"metadata": {"name": NAMESPACE, "labels": self.namespace_labels}}), ""
        if verb == "delete" and args[1] == "namespace":
            self.namespace_labels = None
            return 0, "", ""
        if verb == "create" and args[1] == "namespace":
            return 0, f"apiVersion: v1\nkind: Namespace\nmetadata:\n  name: {NAMESPACE}\n", ""
        if verb == "apply":
            assert stdin
            if stdin.startswith("apiVersion: v1\nkind: Namespace"):
                if self.namespace_labels is None:
                    self.namespace_labels = {}
            else:
                assert self.namespace_labels is not None, "the manifest went on before the namespace"
                self.applied.append(stdin)
            return 0, "", ""
        if verb == "label":
            for pair in (a for a in args[3:] if not a.startswith("--")):
                key, value = pair.split("=", 1)
                self.namespace_labels[key] = value
            return 0, "", ""
        if verb == "rollout":
            return 0, "", ""
        if verb == "get" and args[1] == "configmap":
            return 0, json.dumps({"data": self.configmap}), ""
        if verb == "patch":
            kind = args[args.index("--type") + 1]
            payload = json.loads(args[args.index("-p") + 1])
            if kind == "json":
                for op in payload:
                    assert op["op"] == "remove"
                    del self.configmap[op["path"].rsplit("/", 1)[1]]
            else:
                self.configmap.update(payload["data"])
            return 0, "", ""
        if verb == "exec":
            self.mount_reads += 1
            value = self.mount_value_at_lag_start if self.mount_reads <= self.mount_lag else self.configmap.get("context_repos")
            if value is None:
                return 1, "", "cat: /etc/gitops/context_repos: No such file or directory"
            return 0, value, ""
        raise AssertionError(f"unexpected kubectl {args}")


def _settings(token_file="/unused", **overrides):
    values = dict(
        gitops_repo=GITOPS, token_file=token_file, cluster=CLUSTER, cluster_location="us-central1",
        project="eval-project", agent_namespace="kubeagents-system", agent_name="platform-agent",
        fixture_dir=str(FIXTURE), context_owner="gke-agentic", context_name="kube-agents-evals-platform-charts",
        context_ref="main", build_id="local", pull_number="none",
    )
    values.update(overrides)
    return plant.Settings(**values)


def _stack(github, host, settings=None, clock=None):
    clock = clock or FakeClock()
    return plant.Stack(settings or _settings(), github, run=host, sleep=clock.sleep, now=clock.now)


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


def _planted():
    return _stack(FakeGitHub(), FakeHost()).planted_files()


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


# -- plant ---------------------------------------------------------------------


def test_the_plant_commits_the_four_files_with_the_context_repository_named():
    github, host = FakeGitHub(), FakeHost()
    _stack(github, host).plant()
    assert sorted(github.files) == sorted(f"{DIRECTORY}/{name}" for name in plant.GITOPS_FILES)
    source = yaml.safe_load(github.files[SOURCE])
    assert source["spec"]["url"] == CONTEXT_URL
    assert source["spec"]["ref"] == {"branch": "main"}
    for text in github.files.values():
        assert plant.is_ours(text)
        assert "CONTEXT_REPOSITORY" not in text
    assert [w[0] for w in github.writes] == ["PUT"] * 4
    assert all(w[2]["branch"] == "main" for w in github.writes)


def test_the_plant_labels_the_namespace_before_the_workload_and_applies_the_live_manifest():
    github, host = FakeGitHub(), FakeHost()
    _stack(github, host).plant()
    assert host.namespace_labels == {"managed-by": "kube-agents-bench", "build-id": "local", "pull-number": "none"}
    assert len(host.applied) == 1
    docs = [doc for doc in yaml.safe_load_all(host.applied[0]) if doc]
    assert {doc["kind"] for doc in docs} == {"Namespace", "Secret", "ConfigMap", "Deployment", "Service"}
    deployment = next(doc for doc in docs if doc["kind"] == "Deployment")
    env = {e["name"] for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "PAYMENTS_LEGACY_LEDGER_URL" in env
    verbs = [c[3] if c[3] != "-n" else c[5] for c in host.calls if c[0] == "kubectl"]
    assert verbs.index("label") < verbs.index("rollout")


def test_the_plant_registers_the_context_repository_with_its_ref_and_waits_for_both_mounts():
    github, host, clock = FakeGitHub(), FakeHost(mount_lag=3), FakeClock()
    _stack(github, host, clock=clock).plant()
    assert json.loads(host.configmap["context_repos"]) == [CONTEXT_ENTRY]
    execs = [c for c in host.calls if "exec" in c]
    targets = {(c[c.index("exec") + 1], c[c.index("-c") + 1]) for c in execs}
    assert targets == {("deployment/platform-agent-credential-proxy", "envoy-credential-proxy"), ("statefulset/platform-agent-shell", "shell")}
    assert all(c[-1] == "/etc/gitops/context_repos" for c in execs)
    # Polled while the mounts lagged, then the broker's cache window.
    assert clock.sleeps[-1] == plant.BROKER_CACHE_S
    assert plant.MOUNT_POLL_S in clock.sleeps


def test_the_registration_joins_other_entries_and_is_not_added_twice():
    host = FakeHost(configmap={"context_repos": json.dumps([OTHER_ENTRY])})
    _stack(FakeGitHub(), host).plant()
    assert json.loads(host.configmap["context_repos"]) == [OTHER_ENTRY, CONTEXT_ENTRY]
    host2 = FakeHost(configmap={"context_repos": json.dumps([OTHER_ENTRY, CONTEXT_ENTRY])})
    _stack(FakeGitHub(), host2).plant()
    assert json.loads(host2.configmap["context_repos"]) == [OTHER_ENTRY, CONTEXT_ENTRY]
    assert not any("patch" in c for c in host2.calls)


def test_an_entry_for_the_repository_with_another_ref_stops_the_plant_and_stays():
    other_ref = {**CONTEXT_ENTRY, "ref": "v0.4.2"}
    host = FakeHost(configmap={"context_repos": json.dumps([other_ref])})
    github = FakeGitHub()
    stack = _stack(github, host)
    with pytest.raises(RuntimeError, match="already names"):
        stack.plant()
    # Read before the first write: no commit and no namespace to undo.
    assert github.writes == []
    assert host.namespace_labels is None
    # main() runs the teardown next; the entry stays.
    assert stack.teardown()
    assert json.loads(host.configmap["context_repos"]) == [other_ref]
    assert github.writes == []


def test_the_plant_refuses_a_fixture_directory_without_the_fixture(tmp_path):
    stack = plant.Stack(_settings(fixture_dir=str(tmp_path)), FakeGitHub(), run=FakeHost())
    with pytest.raises(RuntimeError, match="TF_VAR_fixture_dir"):
        stack.plant()


def test_a_mount_that_never_sees_the_entry_fails_the_plant_and_removes_the_rest():
    github, host = FakeGitHub(), FakeHost(mount_lag=10_000)
    stack = _stack(github, host)
    with pytest.raises(RuntimeError, match="did not see"):
        stack.plant()
    # main() runs the teardown after a failed plant; here the parts are still there.
    assert RELEASE in github.files
    assert stack.teardown()
    assert github.files == {}
    assert host.namespace_labels is None
    assert "context_repos" not in host.configmap


def test_the_plant_refuses_a_file_that_is_not_the_stacks():
    github, host = FakeGitHub(files={RELEASE: "kind: HelmRelease\n"}), FakeHost()
    with pytest.raises(RuntimeError, match="not this stack's"):
        _stack(github, host).plant()
    assert github.writes == []
    assert host.applied == []


def test_the_plant_refuses_a_namespace_that_is_not_the_stacks_after_removing_its_files():
    github, host = FakeGitHub(), FakeHost(namespace_labels={})
    stack = _stack(github, host)
    with pytest.raises(RuntimeError, match="not labelled"):
        stack.plant()
    assert host.applied == []
    assert host.namespace_labels == {}
    # The teardown main() runs next leaves the foreign namespace and takes the files.
    assert stack.teardown()
    assert github.files == {}
    assert host.namespace_labels == {}


def test_the_plant_writes_again_over_its_own_leftovers():
    github = FakeGitHub(files=_planted())
    host = FakeHost(namespace_labels={"managed-by": "kube-agents-bench"}, configmap={"context_repos": json.dumps([CONTEXT_ENTRY])})
    _stack(github, host).plant()
    assert all(w[2].get("sha") == f"sha-{w[1][len('/contents/'):]}" for w in github.writes)
    deletes = [c for c in host.calls if c[0] == "kubectl" and c[3] == "delete"]
    assert len(deletes) == 1 and "--wait=true" in deletes[0]
    assert len(host.applied) == 1


def test_the_plant_refuses_missing_settings():
    stack = plant.Stack(_settings(token_file=""), FakeGitHub(), run=FakeHost())
    with pytest.raises(RuntimeError, match="bench_gitops_write_token_file"):
        stack.plant()


def test_a_commit_that_landed_but_answered_a_conflict_is_committed():
    github = FakeGitHub(landed_then={("PUT", f"/contents/{SOURCE}"): 422})
    _stack(github, FakeHost()).plant()
    assert github.files[SOURCE] == _planted()[SOURCE]


def test_a_refused_commit_still_fails_the_plant():
    github = FakeGitHub(refuse={("PUT", f"/contents/{SOURCE}")})
    with pytest.raises(RuntimeError, match="cannot commit"):
        _stack(github, FakeHost()).plant()


# -- teardown ------------------------------------------------------------------


def _planted_host(extra_entries=()):
    return FakeHost(
        namespace_labels={"managed-by": "kube-agents-bench"},
        configmap={"context_repos": json.dumps([*extra_entries, CONTEXT_ENTRY])},
    )


def test_the_teardown_removes_the_registration_the_namespace_and_the_files():
    github, host = FakeGitHub(files=_planted()), _planted_host()
    assert _stack(github, host).teardown()
    assert "context_repos" not in host.configmap
    assert host.namespace_labels is None
    assert github.files == {}
    assert [m for m, _, _ in github.writes] == ["DELETE"] * 4


def test_the_teardown_keeps_the_other_registrations():
    host = _planted_host(extra_entries=[OTHER_ENTRY])
    assert _stack(FakeGitHub(files=_planted()), host).teardown()
    assert json.loads(host.configmap["context_repos"]) == [OTHER_ENTRY]


def test_the_teardown_labels_the_audit_pull_request_before_it_closes_it():
    github = FakeGitHub(
        files=_planted(),
        pulls=[
            _pull(7, [f"{DIRECTORY}/release.yaml"], labels=[plant.AUDIT_REMEDIATION_LABEL]),
            _pull(8, [f"{DIRECTORY}/legacy-env.yaml"], login="someone"),
            _pull(9, [f"clusters/{CLUSTER}/demo-shop/checkout.yaml"]),
            _pull(10, [f"{DIRECTORY}/legacy-env.yaml"], head_repo="fork/kube-agents-evals-2-infra"),
        ],
    )
    assert _stack(github, _planted_host()).teardown()
    writes = [(m, r) for m, r, _ in github.writes]
    assert writes[:2] == [("POST", "/issues/7/labels"), ("PATCH", "/pulls/7")]
    assert github.writes[0][2] == {"labels": ["audit:stale-closed"]}
    assert github.files == {}


def test_a_label_that_does_not_stick_leaves_the_pull_request_open_and_fails_the_destroy():
    github = FakeGitHub(
        files=_planted(),
        pulls=[_pull(7, [f"{DIRECTORY}/release.yaml"], labels=[plant.AUDIT_REMEDIATION_LABEL])],
        refuse={("POST", "/issues/7/labels")},
    )
    host = _planted_host()
    assert not _stack(github, host).teardown()
    assert ("PATCH", "/pulls/7") not in [(m, r) for m, r, _ in github.writes]
    # Every other step still runs.
    assert github.files == {}
    assert host.namespace_labels is None
    assert "context_repos" not in host.configmap


def test_the_teardown_leaves_what_is_not_the_stacks():
    foreign = {path: text.replace("managed-by: kube-agents-bench", "team: payments") for path, text in _planted().items()}
    github = FakeGitHub(files=foreign, pulls=[_pull(7, [f"{DIRECTORY}/release.yaml"], labels=[plant.AUDIT_REMEDIATION_LABEL])])
    host = FakeHost(namespace_labels={}, configmap={"context_repos": json.dumps([OTHER_ENTRY])})
    assert _stack(github, host).teardown()
    assert github.writes == []
    assert github.files == foreign
    assert host.namespace_labels == {}
    assert json.loads(host.configmap["context_repos"]) == [OTHER_ENTRY]
    assert not any("patch" in c for c in host.calls)


def test_a_delete_that_landed_but_answered_not_found_is_deleted():
    github = FakeGitHub(files=_planted(), landed_then={("DELETE", f"/contents/{RELEASE}"): 404})
    assert _stack(github, _planted_host()).teardown()
    assert github.files == {}


def test_a_refused_delete_fails_the_destroy():
    github = FakeGitHub(files=_planted(), refuse={("DELETE", f"/contents/{RELEASE}")})
    assert not _stack(github, _planted_host()).teardown()
    assert RELEASE in github.files


def test_the_token_is_read_from_the_file_on_each_request(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("first\n")
    settings = _settings(token_file=str(token_file))
    assert settings.github_token() == "first"
    token_file.write_text("renewed\n")
    assert settings.github_token() == "renewed"


# -- the stack, the task, the fixture, the reset and the runner agree ------------


def test_the_labels_and_the_paths_are_the_resets():
    sys.path.insert(0, str(REPO / "hack"))
    try:
        import ci_reset_agent_pulls as reset
    finally:
        sys.path.remove(str(REPO / "hack"))
    assert plant.STALE_CLOSED_LABEL == reset.STALE_CLOSED_LABEL
    assert plant.AUDIT_REMEDIATION_LABEL == reset.AUDIT_REMEDIATION_LABEL
    assert plant.BOT_LOGIN_SUFFIX == reset.BOT_LOGIN_SUFFIX
    assert plant.MANAGED_BY_KEY == reset.STACK_LABEL_KEY
    assert plant.MANAGED_BY == reset.STACK_LABEL_VALUE
    pool = _settings(cluster=reset.POOL_HOST_CLUSTER)
    for name in plant.GITOPS_FILES:
        assert f"{pool.directory}/{name}" in reset.PLANTED_STACK_FILES


def test_every_fixture_file_carries_the_label_the_teardown_and_the_reset_read():
    for name in plant.GITOPS_FILES:
        text = (FIXTURE / plant.FIXTURE_GITOPS_SUBDIR / name).read_text()
        assert plant.is_ours(text), name
        doc = yaml.safe_load(text)
        assert doc["metadata"]["labels"]["managed-by"] == "kube-agents-bench", name


def test_the_live_manifest_is_the_rendered_release():
    docs = [doc for doc in yaml.safe_load_all((FIXTURE / plant.FIXTURE_LIVE_MANIFEST).read_text()) if doc]
    assert docs[0]["kind"] == "Namespace" and docs[0]["metadata"]["name"] == plant.WORKLOAD_NAMESPACE
    # The plant labels the namespace before it applies this document; apply's
    # three-way merge keeps labels the document never carried, and would
    # overwrite a managed-by the document did carry.
    assert "labels" not in docs[0]["metadata"], "a managed-by label in the namespace document would overwrite the stack's at apply"
    deployment = next(doc for doc in docs if doc["kind"] == "Deployment")
    assert deployment["metadata"]["name"] == plant.WORKLOAD_DEPLOYMENT


def _variables():
    return set(re.findall(r'^variable "([a-z_]+)"', (STACK / "variables.tf").read_text(), re.M))


def test_the_stack_declares_the_variable_the_runner_mints_for():
    runner = (REPO / "hack" / "ci-eval-pr.sh").read_text()
    declared = re.search(r'^BENCH_GITOPS_WRITE_VARIABLE="([a-z_]+)"$', runner, re.M).group(1)
    assert declared == "bench_gitops_write_token_file"
    assert declared in _variables()
    for name in ("gitops_repo", "host_cluster_name", "host_cluster_location", "agent_namespace", "prow_build_id", "prow_pull_number"):
        assert f"TF_VAR_{name}=" in runner, name
        assert name in _variables(), name


def test_the_task_runs_the_stack_alone_with_teardown():
    doc = yaml.safe_load(TASK.read_text())
    assert doc["writes_alone"] is True
    infra = doc["infrastructure"]
    assert infra["deployer"] == "tofu"
    assert infra["stack"] == "prebuilt/context-repository-owns-the-fix"
    assert infra["teardown"] is True
    assert set(infra.get("variables") or {}) <= _variables()


def test_the_main_tf_passes_every_setting_the_script_reads_on_both_paths():
    main = (STACK / "main.tf").read_text()
    script = (STACK / "plant.py").read_text()
    names = set(re.findall(r'read\("(CONTEXT_FIX_[A-Z_]+)"', script))
    assert names
    for name in names:
        assert len(re.findall(rf"^\s+{name}\s+=", main, re.M)) == 2, name
    assert "when        = destroy" in main
    assert "on_failure" not in re.sub(r"#[^\n]*", "", main)


def test_the_stacks_defaults_name_the_registered_repository():
    variables = (STACK / "variables.tf").read_text()
    defaults = dict(re.findall(r'variable "(context_repository_[a-z]+)" \{.*?default\s+= "([^"]+)"', variables, re.S))
    settings = _settings(context_owner=defaults["context_repository_owner"], context_name=defaults["context_repository_name"], context_ref=defaults["context_repository_ref"])
    assert settings.context_url == CONTEXT_URL
    assert settings.context_entry == CONTEXT_ENTRY


def test_the_install_names_the_stack_waits_on_are_the_operators():
    # A renamed workload, container or mount would show only as a 240 s wait
    # on every nightly run, so the names are held against the Go source.
    controller = REPO / "k8s-operator" / "internal" / "controller"
    manifests = (controller / "platformagent_manifests.go").read_text()
    broker = (controller / "credential_proxy_manifests.go").read_text()
    shell = (controller / "shell_sandbox_manifests.go").read_text()
    agent_controller = (controller / "platformagent_controller.go").read_text()
    # `\s*=\s*` rather than the literal spacing: gofmt re-pads a const block
    # when a longer name joins it, which changes no value.
    def pinned(source, name, value):
        return re.search(rf'\b{name}\s*=\s*"{re.escape(value)}"', source) is not None

    assert pinned(manifests, "gitopsStateDir", plant.GITOPS_STATE_DIR)
    assert pinned(broker, "credentialProxyContainerName", plant.BROKER_CONTAINER)
    assert f'agent.Name + "{plant.BROKER_DEPLOYMENT.format(agent="")}"' in broker
    assert f'agent.Name + "{plant.SHELL_STATEFULSET.format(agent="")}"' in shell
    assert pinned(shell, "shellSandboxContainerName", plant.SHELL_CONTAINER)
    assert pinned(agent_controller, "gitopsStateConfigMapSuffix", plant.STATE_CONFIGMAP.format(agent=""))
    assert pinned(agent_controller, "gitopsStateContextReposKey", plant.CONTEXT_REPOS_KEY)


def _publisher():
    publisher_spec = importlib.util.spec_from_file_location("publish_context_repository", FIXTURE / "publish_context_repository.py")
    publisher = importlib.util.module_from_spec(publisher_spec)
    publisher_spec.loader.exec_module(publisher)
    return publisher


def test_the_publisher_reads_an_empty_repository_from_the_status_line(monkeypatch):
    publisher = _publisher()
    answers = {}

    class Done:
        def __init__(self, returncode, stdout):
            self.returncode, self.stdout, self.stderr = returncode, stdout, ""

    def fake_run(argv, **kwargs):
        assert argv[:3] == ["gh", "api", "--include"], argv
        return answers["done"]

    monkeypatch.setattr(publisher.subprocess, "run", fake_run)
    answers["done"] = Done(1, "HTTP/2.0 409 Conflict\r\ncontent-type: application/json\r\n\r\n{\"message\":\"Git Repository is empty.\"}")
    assert publisher.repository_is_empty("owner/name")
    answers["done"] = Done(0, "HTTP/2.0 200 OK\r\ncontent-type: application/json\r\n\r\n[]")
    assert not publisher.repository_is_empty("owner/name")
    answers["done"] = Done(1, "")
    assert not publisher.repository_is_empty("owner/name")


def test_the_publisher_lists_the_chart_tree_and_nothing_else():
    publisher = _publisher()
    paths = [path for path, _ in publisher.fixture_files()]
    assert "README.md" in paths
    assert "services/payments/chart/templates/deploy-api.yaml" in paths
    assert all(not path.startswith(("gitops-repository", "live")) for path in paths)
    assert paths == sorted(paths)
