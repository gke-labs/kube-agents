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

"""Plant and remove the stalled Config Connector object of
bench/tasks/fleet-audit-manifest-fix-names-stalled-config-connector.

main.tf runs this on the runner as `python3 plant.py plant` and
`python3 plant.py teardown`, with the settings in the environment
(`Settings.from_env`).

plant:

1. Finds the seeded cluster of `KCC_EVAL_SLOT` (the cluster whose name ends in
   `-<slot>` among the clusters with the seeded-fleet labels), whose node pool
   `KCC_EVAL_POOL` the declaration names.
2. On the cluster that runs Config Connector, creates the namespace
   `KCC_EVAL_NAMESPACE`, labelled as this stack's, and in it one
   `ContainerNodePool` for that pool. Its `clusterRef` names a
   `ContainerCluster` that the namespace does not hold, so Config Connector
   stops at `DependencyNotFound`, sets `Ready=False`, and makes no call to
   GCP: the real pool is never read or changed. The object also carries
   `cnrm.cloud.google.com/deletion-policy: abandon`, so a delete of the object
   cannot delete the pool either.
   When Config Connector runs in namespaced mode (the `ConfigConnector`
   object's `spec.mode` is `namespaced`), no controller watches a namespace
   until it holds a `ConfigConnectorContext`. So the plant creates one in the
   namespace, with `spec.googleServiceAccount` from
   `KCC_EVAL_GOOGLE_SERVICE_ACCOUNT`, and waits until the context reports
   `healthy`, before it creates the node pool. The service account needs no
   IAM role: the object stops at `DependencyNotFound` before any call to GCP.
   The plant stops with an error when the mode is namespaced and no service
   account is set. In cluster mode, or with no `ConfigConnector` object, it
   creates no context.
3. Waits until Config Connector reports `Ready=False` on the object.
4. Commits the same declaration to the GitOps repository's default branch at
   `clusters/<cluster>/provisioning/<pool>.yaml`, where the stockout SOP finds
   the pool's declaration and writes its fix.

A failure at any step runs teardown before the script exits: Terraform skips
the destroy provisioner of a resource whose create failed.

teardown, every step independent of the others:

1. Closes the open pull request that the audit opened against the plant: the
   one whose head is the remediation branch the audit names for the
   declaration's path (`stack_branch`) and that changes that path. It does
   this only when the file on the default branch is this stack's, so a plant
   that refused a file it did not own, or that failed before it wrote one,
   closes nothing.
2. Deletes the declaration from the default branch, when the file there is
   this stack's (it carries the stack's label).
3. Deletes the `ContainerNodePool`, then the `ConfigConnectorContext`, then
   the namespace, when the namespace is this stack's. The node pool goes first,
   while its controller still runs and can remove its finalizer. Each object
   delete has a time limit, and the namespace delete runs when an object delete
   fails.

   The add-on's operator can fail to finish the context delete (for example,
   when it cannot list a Config Connector kind in the namespace), and its
   finalizer then holds the context, two RoleBindings it made, and the
   namespace in `Terminating`. So when the context delete times out, and only
   when `kubectl get gcp -n <namespace>` shows that no Config Connector object
   is left, the teardown removes that finalizer from the objects in the
   stack's namespace that carry it, logs a WARNING with the operator's error,
   and waits a bounded time for the namespace to go. It changes nothing
   outside that namespace.

Steps 1 and 2 need the seeded cluster's name from `gcloud container clusters
list`, and the GitHub token. When either is not available, the teardown logs
an error that names what stays, and still does step 3.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Callable, NamedTuple

# The label every object and file this stack writes carries, and the value
# that makes it this stack's.
MANAGED_BY_KEY = "managed-by"
MANAGED_BY = "kube-agents-bench"
# The CRD whose presence says that Config Connector runs on the cluster.
NODE_POOL_CRD = "containernodepools.container.cnrm.cloud.google.com"
API_VERSION = "container.cnrm.cloud.google.com/v1beta1"
KIND = "ContainerNodePool"
DELETION_POLICY_KEY = "cnrm.cloud.google.com/deletion-policy"
DELETION_POLICY = "abandon"
PROJECT_ANNOTATION = "cnrm.cloud.google.com/project-id"
# The install's stall watch (agents/platform/scripts/stall_watch.py,
# EXEMPT_ANNOTATION) does not read a namespace that carries this annotation.
# The planted object stays Ready=False for the whole run, and without it a
# watch tick could raise a controller-stall alert and start an agent turn in
# the middle of the case. The fleet audits that the case grades
# (kcc_health.py, fleet_stockout.py) do not read it, so they still see the
# object. It goes on the namespace and not on the object, so the declaration
# file stays the same as the object. The annotation does not stop the k8s
# event watcher, which forwards DependencyNotFound; the eval install excludes
# this namespace from the watcher with an alertExclude rule
# (EVAL_EVENT_WATCHER_EXCLUDED_NAMESPACE in hack/ci-deploy.sh). An install
# without that rule can open an incident on the object.
STALL_WATCH_ANNOTATION = "kubeagents.x-k8s.io/stall-watch"
STALL_WATCH_EXEMPT = "exempt"
# The labels hack/fleet-kubeconfigs.sh discovers the seeded clusters by.
SEEDED_FILTER = "resourceLabels.environment=seeded AND resourceLabels.managed-by=kube-agents-seeded-fleet"
# Where the declaration goes: the gcp-config-connector skill's layout.
DECLARATION_PATH = "clusters/{cluster}/provisioning/{pool}.yaml"
COMMIT_PLANT = "test(bench): plant a Config Connector declaration for {case}"
COMMIT_REMOVE = "test(bench): remove the Config Connector declaration of {case}"
CASE = "fleet-audit-manifest-fix-names-stalled-config-connector"
# The remediation branch the audit opens for a fix of one file:
# audit_report.group_branch_for, copied because the stack does not import the
# skill. bench/tests/test_kcc_stalled_declaration_stack.py holds the two equal.
AUDIT_ID = "stockout-prevention"
STACK_BRANCH = "platform-agent/fix-{audit}-{slug}-{digest}"
BRANCH_SLUG_CHARS = 24
BRANCH_DIGEST_CHARS = 10
# How long `kubectl delete` waits for the object to go, and the hard limit on
# the process around it.
DELETE_TIMEOUT_S = 120
PROCESS_MARGIN_S = 30
DELETE_PROCESS_TIMEOUT_S = DELETE_TIMEOUT_S + PROCESS_MARGIN_S
# How long Config Connector gets to write `Ready=False`, and how often the
# plant reads it. A reference it cannot resolve is its first check.
READY_WAIT_S = 300
POLL_S = 10
# How long a namespace from an earlier run gets to go away.
NAMESPACE_WAIT_S = 180
# The Config Connector install object, and the mode in which each namespace
# needs a `ConfigConnectorContext` before a controller watches it.
CONFIG_CONNECTOR_RESOURCE = "configconnector.core.cnrm.cloud.google.com"
CONFIG_CONNECTOR_NAME = "configconnector.core.cnrm.cloud.google.com"
NAMESPACED_MODE = "namespaced"
CONTEXT_API_VERSION = "core.cnrm.cloud.google.com/v1beta1"
CONTEXT_KIND = "ConfigConnectorContext"
CONTEXT_RESOURCE = "configconnectorcontext.core.cnrm.cloud.google.com"
# The one name Config Connector accepts for a context.
CONTEXT_NAME = "configconnectorcontext.core.cnrm.cloud.google.com"
# How long the namespaced controller gets to come up after the context is
# created, and how often the plant reads it.
CONTEXT_WAIT_S = 300
# The finalizer the Config Connector operator puts on a context and on the
# RoleBindings it makes for it, and the kinds in the stack's namespace the
# teardown looks for it on when the operator cannot finish the delete.
OPERATOR_FINALIZER = "configconnector.cnrm.cloud.google.com/finalizer"
FINALIZER_RESOURCES = (
    CONTEXT_RESOURCE,
    "rolebindings.rbac.authorization.k8s.io",
    "roles.rbac.authorization.k8s.io",
    "serviceaccounts",
)
# What `kubectl delete --timeout` says when the object did not go in time.
DELETE_TIMED_OUT_MARKER = "timed out"
# What kubectl says when an object or a type is not there.
RESOURCE_TYPE_ABSENT_MARKER = "doesn't have a resource type"
NOT_FOUND_MARKERS = ("NotFound", "not found", RESOURCE_TYPE_ABSENT_MARKER)
GITHUB_API = "https://api.github.com"
GITHUB_PAGE = 100
# The label the audit reads as "closed by a sweep, not refused by a person"
# (`audit_report.STALE_CLOSED_LABEL`, which a stack test holds equal). A
# remediation pull request closed without it reads as a human refusal, and
# the audit then does not open that fix again on the same path.
STALE_CLOSED_LABEL = "audit:stale-closed"
# The statuses GitHub answers a label write with.
LABEL_OK_STATUSES = (200, 201)
HTTP_NOT_FOUND = 404
HTTP_TIMEOUT_S = 30
# The pool as the seeded fleet declares it (bench/tf/fleet/main.tf,
# `pinned_inference_pool`): one e2-small node, autoscaling held at one.
POOL_MACHINE_TYPE = "e2-small"
POOL_NODE_COUNT = 1
POOL_MAX_NODES = 1
PLANT_USAGE = "usage: plant.py plant|teardown"
# What `default_run` returns for a command it killed, as `timeout(1)` does.
TIMEOUT_RC = 124


class Settings(NamedTuple):
    project: str
    kcc_cluster: str
    kcc_location: str
    namespace: str
    slot: str
    pool: str
    gitops_repo: str
    token: str = ""
    token_file: str = ""
    google_service_account: str = ""

    def github_token(self) -> str:
        """The GitHub token, read from `token_file` when one is set.

        Read when the first GitHub call is made, not in `from_env`: a token
        file that is gone at teardown must stop only the GitHub half.
        """
        if self.token_file:
            return Path(self.token_file).read_text(encoding="utf-8").strip()
        return self.token

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            project=os.environ["KCC_EVAL_PROJECT"],
            kcc_cluster=os.environ["KCC_EVAL_CLUSTER"],
            kcc_location=os.environ["KCC_EVAL_LOCATION"],
            namespace=os.environ["KCC_EVAL_NAMESPACE"],
            slot=os.environ["KCC_EVAL_SLOT"],
            pool=os.environ["KCC_EVAL_POOL"],
            gitops_repo=os.environ["KCC_EVAL_GITOPS_REPO"],
            token=os.environ.get("BENCH_GITHUB_TOKEN", ""),
            token_file=os.environ.get("KCC_EVAL_GITOPS_TOKEN_FILE", ""),
            google_service_account=os.environ.get("KCC_EVAL_GOOGLE_SERVICE_ACCOUNT", ""),
        )


class Target(NamedTuple):
    cluster: str
    location: str


RunFn = Callable[..., subprocess.CompletedProcess]
HttpFn = Callable[[str, str, dict | None], tuple[int, object]]


def log(message: str) -> None:
    print(f"[kcc-stalled-declaration] {message}", file=sys.stderr, flush=True)


def default_run(
    argv: list[str], *, env: dict | None = None, stdin: str | None = None, timeout: float | None = None
) -> subprocess.CompletedProcess:
    """Run `argv`. A timeout is a failed command, never an exception."""
    try:
        return subprocess.run(argv, env=env, input=stdin, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, TIMEOUT_RC, "", f"killed after {timeout}s")


def github_http(token: Callable[[], str]) -> HttpFn:
    """A GitHub REST call: `(method, path, body)` to `(status, parsed body)`.

    `token` is called on each request, so a missing token stops only the
    calls that need it.
    """

    def call(method: str, path: str, body: dict | None) -> tuple[int, object]:
        request = urllib.request.Request(
            GITHUB_API + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {token()}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
                text = response.read().decode()
                return response.status, json.loads(text) if text else None
        except urllib.error.HTTPError as exc:
            return exc.code, None

    return call


def node_pool_manifest(settings: Settings, target: Target) -> dict:
    """The one object both halves of the plant declare.

    The live object and the file are the same document, so the file that the
    audit's fix edits declares exactly the object that Config Connector
    reports as not ready.
    """
    return {
        "apiVersion": API_VERSION,
        "kind": KIND,
        "metadata": {
            "name": settings.pool,
            "namespace": settings.namespace,
            "labels": {MANAGED_BY_KEY: MANAGED_BY},
            "annotations": {
                DELETION_POLICY_KEY: DELETION_POLICY,
                PROJECT_ANNOTATION: settings.project,
            },
        },
        "spec": {
            "location": target.location,
            "clusterRef": {"name": target.cluster},
            "nodeCount": POOL_NODE_COUNT,
            "nodeLocations": [target.location],
            "autoscaling": {"minNodeCount": POOL_NODE_COUNT, "maxNodeCount": POOL_MAX_NODES},
            "nodeConfig": {"machineType": POOL_MACHINE_TYPE},
        },
    }


def declaration_path(settings: Settings, target: Target) -> str:
    return DECLARATION_PATH.format(cluster=target.cluster, pool=settings.pool)


def declaration_text(manifest: dict) -> str:
    """The file the plant commits. JSON is YAML, so no YAML library is needed."""
    return json.dumps(manifest, indent=2) + "\n"


def is_ours(text: str) -> bool:
    return f'"{MANAGED_BY_KEY}": "{MANAGED_BY}"' in text


def stack_branch(path: str) -> str:
    """The branch the audit opens for a fix of `path` alone."""
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:BRANCH_DIGEST_CHARS]
    slug = re.sub(r"[^a-z0-9]+", "-", PurePosixPath(path).stem.lower()).strip("-")[:BRANCH_SLUG_CHARS].strip("-")
    return STACK_BRANCH.format(audit=AUDIT_ID, slug=slug, digest=digest) if slug else f"platform-agent/fix-{AUDIT_ID}-{digest}"


class Plant:
    def __init__(
        self, settings: Settings, run: RunFn = default_run, http: HttpFn | None = None, sleep=time.sleep, clock=time.monotonic
    ):
        self.settings = settings
        self.run = run
        self.http = http or github_http(settings.github_token)
        self.sleep = sleep
        self.clock = clock
        self.kubeconfig_dir = tempfile.mkdtemp(prefix="kcc-stalled-")
        self.env = {**os.environ, "KUBECONFIG": str(Path(self.kubeconfig_dir) / "config")}

    # -- reads -----------------------------------------------------------------

    def must(self, argv: list[str], stdin: str | None = None) -> str:
        result = self.run(argv, env=self.env, stdin=stdin)
        if result.returncode != 0:
            raise RuntimeError(f"{' '.join(argv[:4])} exited {result.returncode}: {result.stderr.strip()}")
        return result.stdout

    def target(self) -> Target:
        listing = json.loads(
            self.must(
                ["gcloud", "container", "clusters", "list", "--project", self.settings.project,
                 f"--filter={SEEDED_FILTER}", "--format=json(name,location)"]
            )
            or "[]"
        )
        found = [c for c in listing if str(c.get("name", "")).endswith(f"-{self.settings.slot}")]
        if len(found) != 1:
            raise RuntimeError(f"expected one seeded cluster for slot {self.settings.slot}, found {len(found)}")
        return Target(str(found[0]["name"]), str(found[0]["location"]))

    def credentials(self) -> None:
        self.must(
            ["gcloud", "container", "clusters", "get-credentials", self.settings.kcc_cluster,
             "--location", self.settings.kcc_location, "--project", self.settings.project, "--quiet"]
        )

    def namespace_owner(self) -> str | None:
        """The namespace's `managed-by` label, `""` without one, None when absent."""
        result = self.run(
            ["kubectl", "get", "namespace", self.settings.namespace, "-o", "json"], env=self.env
        )
        if result.returncode != 0:
            return None
        return str(((json.loads(result.stdout).get("metadata") or {}).get("labels") or {}).get(MANAGED_BY_KEY, ""))

    def config_connector_mode(self) -> str:
        """The `ConfigConnector` object's `spec.mode`, or `""` when there is
        no such object (Config Connector then watches every namespace)."""
        result = self.run(
            ["kubectl", "get", CONFIG_CONNECTOR_RESOURCE, CONFIG_CONNECTOR_NAME, "-o", "json"], env=self.env
        )
        if result.returncode != 0:
            if any(marker in result.stderr for marker in NOT_FOUND_MARKERS):
                return ""
            raise RuntimeError(f"cannot read the ConfigConnector object: {result.stderr.strip()}")
        return str((json.loads(result.stdout).get("spec") or {}).get("mode") or "")

    def context_healthy(self) -> bool:
        result = self.run(
            ["kubectl", "get", CONTEXT_RESOURCE, CONTEXT_NAME, "-n", self.settings.namespace, "-o", "json"],
            env=self.env,
        )
        if result.returncode != 0:
            return False
        return (json.loads(result.stdout).get("status") or {}).get("healthy") is True

    def ready_condition(self) -> dict | None:
        result = self.run(
            ["kubectl", "get", KIND.lower(), self.settings.pool, "-n", self.settings.namespace, "-o", "json"],
            env=self.env,
        )
        if result.returncode != 0:
            return None
        for condition in (json.loads(result.stdout).get("status") or {}).get("conditions") or []:
            if condition.get("type") == "Ready":
                return condition
        return None

    def default_branch(self) -> str:
        status, body = self.http("GET", f"/repos/{self.settings.gitops_repo}", None)
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"cannot read {self.settings.gitops_repo} (HTTP {status})")
        return str(body["default_branch"])

    def remote_file(self, path: str, branch: str) -> tuple[str, str] | None:
        """`(text, sha)` of `path` on `branch`, or None when it is not there."""
        status, body = self.http("GET", f"/repos/{self.settings.gitops_repo}/contents/{path}?ref={branch}", None)
        if status == HTTP_NOT_FOUND:
            return None
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"cannot read {path} (HTTP {status})")
        return base64.b64decode(str(body.get("content") or "")).decode(), str(body["sha"])

    # -- plant -----------------------------------------------------------------

    def plant(self) -> None:
        try:
            self._plant()
        except BaseException:
            log("the plant failed; removing what it wrote")
            self.teardown()
            raise

    def _plant(self) -> None:
        target = self.target()
        self.credentials()
        if self.run(["kubectl", "get", "crd", NODE_POOL_CRD], env=self.env).returncode != 0:
            raise RuntimeError(f"Config Connector is not installed on {self.settings.kcc_cluster}: no CRD {NODE_POOL_CRD}")
        namespaced = self.config_connector_mode() == NAMESPACED_MODE
        if namespaced and not self.settings.google_service_account:
            raise RuntimeError(
                f"Config Connector runs in namespaced mode on {self.settings.kcc_cluster}, so the namespace needs a "
                f"{CONTEXT_KIND}; set kcc_google_service_account (KCC_EVAL_GOOGLE_SERVICE_ACCOUNT) to a service account"
            )
        owner = self.namespace_owner()
        if owner is not None and owner != MANAGED_BY:
            raise RuntimeError(f"namespace {self.settings.namespace} exists and is not this stack's; refusing to use it")
        if owner == MANAGED_BY:
            self.delete_kubernetes()
            self.must(["kubectl", "wait", "--for=delete", f"namespace/{self.settings.namespace}", f"--timeout={NAMESPACE_WAIT_S}s"])
        namespace = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": self.settings.namespace,
                "labels": {MANAGED_BY_KEY: MANAGED_BY},
                "annotations": {PROJECT_ANNOTATION: self.settings.project, STALL_WATCH_ANNOTATION: STALL_WATCH_EXEMPT},
            },
        }
        manifest = node_pool_manifest(self.settings, target)
        self.must(["kubectl", "apply", "-f", "-"], stdin=json.dumps(namespace))
        if namespaced:
            self.must(["kubectl", "apply", "-f", "-"], stdin=json.dumps(self.context_manifest()))
            self.wait_context_healthy()
        self.must(["kubectl", "apply", "-f", "-"], stdin=json.dumps(manifest))
        self.wait_not_ready()
        self.commit_declaration(target, manifest)
        log(f"planted {KIND} {self.settings.namespace}/{self.settings.pool} for {target.cluster}")

    def context_manifest(self) -> dict:
        """The context that makes the namespaced controller watch the namespace."""
        return {
            "apiVersion": CONTEXT_API_VERSION,
            "kind": CONTEXT_KIND,
            "metadata": {
                "name": CONTEXT_NAME,
                "namespace": self.settings.namespace,
                "labels": {MANAGED_BY_KEY: MANAGED_BY},
            },
            "spec": {"googleServiceAccount": self.settings.google_service_account},
        }

    def wait_context_healthy(self) -> None:
        deadline = self.clock() + CONTEXT_WAIT_S
        while not self.context_healthy():
            if self.clock() >= deadline:
                raise RuntimeError(f"the {CONTEXT_KIND} in {self.settings.namespace} did not report healthy in {CONTEXT_WAIT_S}s")
            self.sleep(POLL_S)
        log(f"the {CONTEXT_KIND} in {self.settings.namespace} is healthy")

    def wait_not_ready(self) -> None:
        deadline = self.clock() + READY_WAIT_S
        while True:
            condition = self.ready_condition()
            if condition and str(condition.get("status")) == "True":
                raise RuntimeError("Config Connector reports the planted object ready; the case cannot go red or green")
            if condition and str(condition.get("status")) == "False":
                log(f"Config Connector reports Ready=False: {condition.get('reason')}")
                return
            if self.clock() >= deadline:
                raise RuntimeError(f"Config Connector wrote no Ready condition in {READY_WAIT_S}s")
            self.sleep(POLL_S)

    def commit_declaration(self, target: Target, manifest: dict) -> None:
        branch = self.default_branch()
        path = declaration_path(self.settings, target)
        existing = self.remote_file(path, branch)
        if existing is not None and not is_ours(existing[0]):
            raise RuntimeError(f"{path} exists in {self.settings.gitops_repo} and is not this stack's")
        body = {
            "message": COMMIT_PLANT.format(case=CASE),
            "content": base64.b64encode(declaration_text(manifest).encode()).decode(),
            "branch": branch,
        }
        if existing is not None:
            body["sha"] = existing[1]
        status, _ = self.http("PUT", f"/repos/{self.settings.gitops_repo}/contents/{path}", body)
        if status not in (200, 201):
            raise RuntimeError(f"cannot commit {path} (HTTP {status})")

    # -- teardown --------------------------------------------------------------

    def teardown(self) -> bool:
        """Remove what the plant wrote. True when every step succeeded."""
        ok = True
        try:
            target = self.target()
        except Exception as exc:  # noqa: BLE001 -- the Kubernetes half needs no target
            log(
                f"ERROR: cannot resolve the seeded cluster of slot {self.settings.slot} ({exc}). The GitOps half "
                f"is not done: close the audit's pull request on {DECLARATION_PATH.format(cluster='<cluster>', pool=self.settings.pool)} "
                f"in {self.settings.gitops_repo} and delete that file by hand. The Kubernetes half runs."
            )
            target = None
            ok = False
        if target is not None:
            for step in (self.close_pull_requests, self.delete_declaration):
                try:
                    step(target)
                except Exception as exc:  # noqa: BLE001 -- every step runs
                    log(f"{step.__name__} failed: {exc}")
                    ok = False
        try:
            self.credentials()
            if self.namespace_owner() == MANAGED_BY:
                self.delete_kubernetes()
        except Exception as exc:  # noqa: BLE001
            log(f"the Kubernetes teardown failed: {exc}")
            ok = False
        return ok

    def close_pull_requests(self, target: Target) -> None:
        """Close the audit's open pull request on the declaration's path.

        Only a pull request from `stack_branch`, and only when the file on
        the default branch is this stack's: another pull request on that path,
        or one on a file the stack does not own, is not the stack's to close.
        """
        path = declaration_path(self.settings, target)
        repo = self.settings.gitops_repo
        existing = self.remote_file(path, self.default_branch())
        if existing is None or not is_ours(existing[0]):
            log(f"{path} is not this stack's on the default branch; no pull request is closed")
            return
        branch = stack_branch(path)
        owner = repo.partition("/")[0]
        status, pulls = self.http("GET", f"/repos/{repo}/pulls?state=open&head={owner}:{branch}&per_page={GITHUB_PAGE}", None)
        if status != 200 or not isinstance(pulls, list):
            raise RuntimeError(f"cannot list pull requests (HTTP {status})")
        for pull in pulls:
            head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
            if head.get("ref") != branch:
                continue
            number = pull.get("number")
            status, files = self.http("GET", f"/repos/{repo}/pulls/{number}/files?per_page={GITHUB_PAGE}", None)
            if status != 200 or not isinstance(files, list):
                raise RuntimeError(f"cannot list the files of pull request {number} (HTTP {status})")
            if any(f.get("filename") == path for f in files):
                # The label goes on before the close, never after, as
                # hack/ci_reset_agent_pulls.py does: a label that does not
                # stick leaves the pull request open.
                status, _ = self.http("POST", f"/repos/{repo}/issues/{number}/labels", {"labels": [STALE_CLOSED_LABEL]})
                if status not in LABEL_OK_STATUSES:
                    raise RuntimeError(f"cannot label pull request {number} {STALE_CLOSED_LABEL} (HTTP {status}); it stays open")
                status, _ = self.http("PATCH", f"/repos/{repo}/pulls/{number}", {"state": "closed"})
                if status != 200:
                    raise RuntimeError(f"cannot close pull request {number} (HTTP {status})")
                log(f"closed pull request {number}, which changes {path}")

    def delete_declaration(self, target: Target) -> None:
        branch = self.default_branch()
        path = declaration_path(self.settings, target)
        existing = self.remote_file(path, branch)
        if existing is None:
            return
        if not is_ours(existing[0]):
            log(f"{path} is not this stack's; it stays")
            return
        body = {"message": COMMIT_REMOVE.format(case=CASE), "sha": existing[1], "branch": branch}
        status, _ = self.http("DELETE", f"/repos/{self.settings.gitops_repo}/contents/{path}", body)
        if status != 200:
            raise RuntimeError(f"cannot delete {path} (HTTP {status})")

    def delete_object(self, resource: str, name: str) -> tuple[str, bool]:
        """Delete one object in the namespace, with a time limit.

        Returns the error, or `""`, and whether the delete ran out of time. A
        type the server does not serve is not an error.
        """
        argv = [
            "kubectl", "delete", resource, name, "-n", self.settings.namespace,
            "--ignore-not-found", "--wait=true", f"--timeout={DELETE_TIMEOUT_S}s",
        ]
        result = self.run(argv, env=self.env, timeout=DELETE_PROCESS_TIMEOUT_S)
        if result.returncode == 0 or any(marker in result.stderr for marker in NOT_FOUND_MARKERS):
            return "", False
        timed_out = result.returncode == TIMEOUT_RC or DELETE_TIMED_OUT_MARKER in result.stderr
        error = f"the {resource} delete exited {result.returncode}: {result.stderr.strip()}"
        log(f"{error}; the teardown goes on")
        return error, timed_out

    def kcc_objects_left(self) -> int | None:
        """How many Config Connector objects the namespace still holds, or
        None when the read failed (which is never "none left")."""
        result = self.run(["kubectl", "get", "gcp", "-n", self.settings.namespace, "-o", "json"], env=self.env)
        if result.returncode != 0:
            if RESOURCE_TYPE_ABSENT_MARKER in result.stderr:
                return 0
            return None
        try:
            return len(json.loads(result.stdout).get("items") or [])
        except (json.JSONDecodeError, AttributeError):
            return None

    def operator_error(self) -> str:
        """The errors the operator wrote on the context's status, or `""`."""
        result = self.run(
            ["kubectl", "get", CONTEXT_RESOURCE, CONTEXT_NAME, "-n", self.settings.namespace, "-o", "json"],
            env=self.env,
        )
        if result.returncode != 0:
            return ""
        try:
            errors = (json.loads(result.stdout).get("status") or {}).get("errors") or []
        except (json.JSONDecodeError, AttributeError):
            return ""
        return "; ".join(str(e) for e in errors)

    def strip_operator_finalizer(self) -> tuple[list[str], list[str]]:
        """Remove `OPERATOR_FINALIZER` from each object in the stack's
        namespace that carries it; the objects changed, and the errors.

        Every read and patch names the namespace with `-n`, so nothing outside
        it changes. A JSON patch `test` op guards each removal, so a list that
        changed since the read is not cut at the wrong place. A patch that
        fails is recorded and the next object is tried, as `delete_object`
        does: an object that went away (NotFound) is not an error, and a
        failed `test` op is.
        """
        changed: list[str] = []
        errors: list[str] = []
        namespace = self.settings.namespace
        for resource in FINALIZER_RESOURCES:
            result = self.run(["kubectl", "get", resource, "-n", namespace, "-o", "json"], env=self.env)
            if result.returncode != 0:
                continue
            for item in json.loads(result.stdout).get("items") or []:
                meta = item.get("metadata") or {}
                finalizers = meta.get("finalizers") or []
                if OPERATOR_FINALIZER not in finalizers or meta.get("namespace", namespace) != namespace:
                    continue
                index = finalizers.index(OPERATOR_FINALIZER)
                patch = [
                    {"op": "test", "path": f"/metadata/finalizers/{index}", "value": OPERATOR_FINALIZER},
                    {"op": "remove", "path": f"/metadata/finalizers/{index}"},
                ]
                name = str(meta.get("name") or "")
                result = self.run(
                    ["kubectl", "patch", resource, name, "-n", namespace, "--type=json", "-p", json.dumps(patch)], env=self.env
                )
                if result.returncode == 0:
                    changed.append(f"{resource}/{name}")
                elif not any(marker in result.stderr for marker in NOT_FOUND_MARKERS):
                    error = f"the finalizer patch of {resource}/{name} exited {result.returncode}: {result.stderr.strip()}"
                    log(f"{error}; the teardown goes on")
                    errors.append(error)
        return changed, errors

    def release_stuck_context(self) -> tuple[str, bool]:
        """After a context delete that ran out of time: remove the operator's
        finalizer when no Config Connector object is left. Returns the error,
        or `""`, and whether the removal ran, so the caller waits for the
        namespace also when one patch failed."""
        left = self.kcc_objects_left()
        if left is None:
            return "cannot read the Config Connector objects in the namespace, so no finalizer is removed", False
        if left:
            return f"{left} Config Connector object(s) are still in the namespace, so no finalizer is removed", False
        reason = self.operator_error() or "no error on the context's status"
        changed, errors = self.strip_operator_finalizer()
        log(
            f"WARNING: the Config Connector operator did not finish the {CONTEXT_KIND} delete ({reason}); "
            f"removed {OPERATOR_FINALIZER} from {', '.join(changed) or 'nothing'} in {self.settings.namespace}"
        )
        return "; ".join(errors), True

    def delete_kubernetes(self) -> None:
        """Delete the node pool, then the context, then the namespace.

        The node pool goes while its controller still runs, so the controller
        can remove its finalizer; the context delete stops that controller.
        The namespace delete runs also when an object delete fails or runs
        out of time: it removes the objects with it. A context delete that
        runs out of time goes to `release_stuck_context`, and then the
        teardown waits a bounded time for the namespace to go.
        """
        pool_error, _ = self.delete_object(KIND.lower(), self.settings.pool)
        context_error, context_timed_out = self.delete_object(CONTEXT_RESOURCE, CONTEXT_NAME)
        errors = [pool_error, context_error]
        released = False
        if context_timed_out:
            release_error, released = self.release_stuck_context()
            errors.append(release_error)
        self.must(["kubectl", "delete", "namespace", self.settings.namespace, "--ignore-not-found", "--wait=false"])
        if released:
            waited = self.run(
                ["kubectl", "wait", "--for=delete", f"namespace/{self.settings.namespace}", f"--timeout={NAMESPACE_WAIT_S}s"],
                env=self.env,
                timeout=NAMESPACE_WAIT_S + PROCESS_MARGIN_S,
            )
            if waited.returncode == 0 or any(marker in waited.stderr for marker in NOT_FOUND_MARKERS):
                # The context went with the namespace, so its timeout is not
                # an error any more.
                errors.remove(context_error)
            else:
                errors.append(f"namespace {self.settings.namespace} did not go in {NAMESPACE_WAIT_S}s: {waited.stderr.strip()}")
        errors = [e for e in errors if e]
        if errors:
            raise RuntimeError("; ".join(errors))


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in ("plant", "teardown"):
        print(PLANT_USAGE, file=sys.stderr)
        return 2
    plant = Plant(Settings.from_env())
    try:
        if argv[1] == "plant":
            try:
                plant.plant()
            except Exception as exc:  # noqa: BLE001 -- the message is the report
                log(f"ERROR: {exc}")
                return 1
            return 0
        return 0 if plant.teardown() else 1
    finally:
        shutil.rmtree(plant.kubeconfig_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
