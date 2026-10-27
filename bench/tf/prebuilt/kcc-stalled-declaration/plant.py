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
3. Waits until Config Connector reports `Ready=False` on the object.
4. Commits the same declaration to the GitOps repository's default branch at
   `clusters/<cluster>/provisioning/<pool>.yaml`, where the stockout SOP finds
   the pool's declaration and writes its fix.

A failure at any step runs teardown before the script exits: Terraform skips
the destroy provisioner of a resource whose create failed.

teardown, every step independent of the others:

1. Closes each open pull request in the GitOps repository that changes the
   declaration's path: the remediation the audit opened against the plant.
2. Deletes the declaration from the default branch, when the file there is
   this stack's (it carries the stack's label).
3. Deletes the `ContainerNodePool` and the namespace, when the namespace is
   this stack's.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
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
# The labels hack/fleet-kubeconfigs.sh discovers the seeded clusters by.
SEEDED_FILTER = "resourceLabels.environment=seeded AND resourceLabels.managed-by=kube-agents-seeded-fleet"
# Where the declaration goes: the gcp-config-connector skill's layout.
DECLARATION_PATH = "clusters/{cluster}/provisioning/{pool}.yaml"
COMMIT_PLANT = "test(bench): plant a Config Connector declaration for {case}"
COMMIT_REMOVE = "test(bench): remove the Config Connector declaration of {case}"
CASE = "fleet-audit-manifest-fix-names-stalled-config-connector"
# How long Config Connector gets to write `Ready=False`, and how often the
# plant reads it. A reference it cannot resolve is its first check.
READY_WAIT_S = 300
POLL_S = 10
# How long a namespace from an earlier run gets to go away.
NAMESPACE_WAIT_S = 180
GITHUB_API = "https://api.github.com"
GITHUB_PAGE = 100
HTTP_NOT_FOUND = 404
HTTP_TIMEOUT_S = 30
# The pool as the seeded fleet declares it (bench/tf/fleet/main.tf,
# `pinned_inference_pool`): one e2-small node, autoscaling held at one.
POOL_MACHINE_TYPE = "e2-small"
POOL_NODE_COUNT = 1
POOL_MAX_NODES = 1
PLANT_USAGE = "usage: plant.py plant|teardown"


class Settings(NamedTuple):
    project: str
    kcc_cluster: str
    kcc_location: str
    namespace: str
    slot: str
    pool: str
    gitops_repo: str
    token: str

    @classmethod
    def from_env(cls) -> "Settings":
        token_file = os.environ.get("KCC_EVAL_GITOPS_TOKEN_FILE", "")
        token = Path(token_file).read_text(encoding="utf-8").strip() if token_file else os.environ.get("BENCH_GITHUB_TOKEN", "")
        return cls(
            project=os.environ["KCC_EVAL_PROJECT"],
            kcc_cluster=os.environ["KCC_EVAL_CLUSTER"],
            kcc_location=os.environ["KCC_EVAL_LOCATION"],
            namespace=os.environ["KCC_EVAL_NAMESPACE"],
            slot=os.environ["KCC_EVAL_SLOT"],
            pool=os.environ["KCC_EVAL_POOL"],
            gitops_repo=os.environ["KCC_EVAL_GITOPS_REPO"],
            token=token,
        )


class Target(NamedTuple):
    cluster: str
    location: str


RunFn = Callable[..., subprocess.CompletedProcess]
HttpFn = Callable[[str, str, dict | None], tuple[int, object]]


def log(message: str) -> None:
    print(f"[kcc-stalled-declaration] {message}", file=sys.stderr, flush=True)


def default_run(argv: list[str], *, env: dict | None = None, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, env=env, input=stdin, capture_output=True, text=True, check=False)


def github_http(token: str) -> HttpFn:
    """A GitHub REST call: `(method, path, body)` to `(status, parsed body)`."""

    def call(method: str, path: str, body: dict | None) -> tuple[int, object]:
        request = urllib.request.Request(
            GITHUB_API + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
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


class Plant:
    def __init__(self, settings: Settings, run: RunFn = default_run, http: HttpFn | None = None, sleep=time.sleep):
        self.settings = settings
        self.run = run
        self.http = http or github_http(settings.token)
        self.sleep = sleep
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
                "annotations": {PROJECT_ANNOTATION: self.settings.project},
            },
        }
        manifest = node_pool_manifest(self.settings, target)
        self.must(["kubectl", "apply", "-f", "-"], stdin=json.dumps(namespace))
        self.must(["kubectl", "apply", "-f", "-"], stdin=json.dumps(manifest))
        self.wait_not_ready()
        self.commit_declaration(target, manifest)
        log(f"planted {KIND} {self.settings.namespace}/{self.settings.pool} for {target.cluster}")

    def wait_not_ready(self) -> None:
        deadline = time.monotonic() + READY_WAIT_S
        while True:
            condition = self.ready_condition()
            if condition and str(condition.get("status")) == "True":
                raise RuntimeError("Config Connector reports the planted object ready; the case cannot go red or green")
            if condition and str(condition.get("status")) == "False":
                log(f"Config Connector reports Ready=False: {condition.get('reason')}")
                return
            if time.monotonic() >= deadline:
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
            log(f"cannot resolve the seeded cluster ({exc}); the GitOps half is skipped")
            target = None
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
        path = declaration_path(self.settings, target)
        repo = self.settings.gitops_repo
        status, pulls = self.http("GET", f"/repos/{repo}/pulls?state=open&per_page={GITHUB_PAGE}", None)
        if status != 200 or not isinstance(pulls, list):
            raise RuntimeError(f"cannot list pull requests (HTTP {status})")
        for pull in pulls:
            number = pull.get("number")
            status, files = self.http("GET", f"/repos/{repo}/pulls/{number}/files?per_page={GITHUB_PAGE}", None)
            if status != 200 or not isinstance(files, list):
                raise RuntimeError(f"cannot list the files of pull request {number} (HTTP {status})")
            if any(f.get("filename") == path for f in files):
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

    def delete_kubernetes(self) -> None:
        self.must(["kubectl", "delete", KIND.lower(), self.settings.pool, "-n", self.settings.namespace, "--ignore-not-found"])
        self.must(["kubectl", "delete", "namespace", self.settings.namespace, "--ignore-not-found", "--wait=false"])


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
