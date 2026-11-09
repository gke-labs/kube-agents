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

"""Plant and remove the three-part fixture of
bench/tasks/remediation-context-repository-owns-the-fix.

main.tf runs this on the runner as `python3 plant.py plant` and
`python3 plant.py teardown`, with the settings in the environment
(`Settings.from_env`). The fixture's README lists the parts; this script
plants the three for one run, on the install the run grades:

1. The GitOps directory. `clusters/<host cluster>/demo-shop/payments/` from
   the case's `fixture/gitops-repository/`, committed to the default branch
   of the leased GitOps repository with the three `CONTEXT_REPOSITORY_*`
   words replaced by the context repository's owner, name and ref. Each
   file carries the stack's label (`managed-by: kube-agents-bench` on a
   line of its own, as the fixture files are written), which is how the
   teardown, a later plant and the repository reset
   (hack/ci_reset_agent_pulls.py, PLANTED_STACK_FILES) know a file is the
   stack's.
2. The live workload. `fixture/live/payments-api.yaml` applied to the
   agent host (the `demo-shop` namespace, a Deployment, a Secret, a
   ConfigMap and a Service), the namespace labelled as the stack's.
3. The registration. One entry for the context repository, with its ref,
   added to the `context_repos` key of the install's GitOps state
   ConfigMap (`<agent>-gitops-state`), which the operator seeds from the
   PlatformAgent and keeps every administrator-added entry of. The
   ConfigMap reaches the broker and the shell sandbox as a mounted
   directory that kubelet refreshes on its own schedule, so the plant
   waits until both mounts carry the entry and then the broker's cache
   window, before the agent is asked anything.

The context repository itself is not planted here: it is a public
repository, so the broker reads it with no credential, and its tree is the
case's `fixture/context-repository/` on the ref the stack names
(`fixture/publish_context_repository.py` publishes it).

plant:

1. Refuses a planted file on the default branch that is not the stack's,
   and a `demo-shop` namespace on the agent host that does not carry the
   stack's label. A leftover that is the stack's (a run stopped before its
   teardown) is written again or deleted first.
2. Commits the four files, applies the workload, adds the registration,
   waits for the mounts.

A failure after the first write runs teardown before the script exits:
Terraform skips the destroy provisioner of a resource whose create failed.

teardown, each step independent of the others:

1. Removes the stack's entry (the same type, URL and ref it writes) from
   `context_repos`; an entry that names the repository with other fields
   stops the plant and is never removed. The key goes when the list is then
   empty, so the ConfigMap reads as it did before the plant.
2. Deletes the `demo-shop` namespace when it carries the stack's label.
3. Closes every open pull request that a `[bot]` login opened from a
   branch in the repository and that changes a file under the directory,
   but only when the planted release file on the default branch is the
   stack's (a pull request that carries `audit:remediation` gets
   `audit:stale-closed` first, as hack/ci_reset_agent_pulls.py does), then
   deletes each planted file that is the stack's.

The GitHub token is read from the file that `CONTEXT_FIX_GITOPS_TOKEN_FILE`
names, again for each request, because hack/ci-eval-pr.sh renews it while
the unit runs. The token is never on argv and never printed. kubectl runs
against a kubeconfig this script fetches into a directory of its own, never
the ambient one (the deployer repoints that between stacks).
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
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, NamedTuple

# The label the planted files and the namespace carry, and the value that
# makes them this stack's. The same pair as the other stacks that write to
# the GitOps repository or the agent host, and as hack/ci_reset_agent_pulls.py.
MANAGED_BY_KEY = "managed-by"
MANAGED_BY = "kube-agents-bench"
BUILD_ID_LABEL_KEY = "build-id"
PULL_NUMBER_LABEL_KEY = "pull-number"
NO_BUILD_ID = "local"
NO_PULL_NUMBER = "none"
CASE = "remediation-context-repository-owns-the-fix"
# The GitOps directory, relative to the repository root, with the host
# cluster's name in it; the fixture tree holds it under `clusters/CLUSTER/`.
GITOPS_DIRECTORY = "clusters/{cluster}/demo-shop/payments"
FIXTURE_CLUSTER_WORD = "CLUSTER"
FIXTURE_GITOPS_SUBDIR = Path("gitops-repository") / "clusters" / FIXTURE_CLUSTER_WORD / "demo-shop" / "payments"
FIXTURE_LIVE_MANIFEST = Path("live") / "payments-api.yaml"
# The four files, in the order they are written; release.yaml is the one
# whose ownership gates the pull-request close at teardown.
GITOPS_FILES = ("source.yaml", "release.yaml", "values.enc.yaml", "legacy-env.yaml")
RELEASE_FILE = "release.yaml"
CONTEXT_WORDS = {
    "CONTEXT_REPOSITORY_OWNER": "context_owner",
    "CONTEXT_REPOSITORY_NAME": "context_name",
    "CONTEXT_REPOSITORY_REF": "context_ref",
}
WORKLOAD_NAMESPACE = "demo-shop"
WORKLOAD_DEPLOYMENT = "payments-api"
COMMIT_PLANT = "test(bench): plant the payments HelmRelease directory for {case}"
COMMIT_REMOVE = "test(bench): remove the payments HelmRelease directory of {case}"
# The audit's labels, as hack/ci_sweep_agent_pulls.py names them;
# bench/tests/test_context_repository_owns_the_fix_stack.py holds them equal.
AUDIT_REMEDIATION_LABEL = "audit:remediation"
STALE_CLOSED_LABEL = "audit:stale-closed"
BOT_LOGIN_SUFFIX = "[bot]"
GITHUB_API = "https://api.github.com"
GITHUB_HOST = "https://github.com"
HTTP_TIMEOUT_S = 30
HTTP_OK = 200
HTTP_CREATED = 201
HTTP_NOT_FOUND = 404
LABEL_OK_STATUSES = (HTTP_OK, HTTP_CREATED)
PUT_OK_STATUSES = (HTTP_OK, HTTP_CREATED)
# A transient answer (a 5xx, or no answer) is tried again, 2 s then 8 s
# apart, as hack/ci_reset_agent_pulls.py does.
RETRY_DELAYS_S = (2, 8)
RETRYABLE_STATUS_FLOOR = 500
NO_ANSWER = 0
GITHUB_PAGE = 100
MAX_PAGES = 10
# The GitOps state ConfigMap and the key the registration goes in, as the
# operator names them (platformagent_controller.go, gitopsStateContextReposKey).
STATE_CONFIGMAP = "{agent}-gitops-state"
CONTEXT_REPOS_KEY = "context_repos"
CONTEXT_REPO_TYPE = "github"
# Where the ConfigMap is mounted in the broker and the shell sandbox
# (gitops_workspace.GITOPS_STATE_DIR), and the workloads and containers that
# read it: the credential broker's own Deployment
# (credential_proxy_manifests.go) and the shell StatefulSet.
GITOPS_STATE_DIR = "/etc/gitops"
BROKER_DEPLOYMENT = "{agent}-credential-proxy"
BROKER_CONTAINER = "envoy-credential-proxy"
SHELL_STATEFULSET = "{agent}-shell"
SHELL_CONTAINER = "shell"
# How long the plant waits for kubelet to refresh the two mounts (kubelet
# syncs a ConfigMap volume about once a minute, not promptly), polling
# every few seconds, and the broker's own cache of the list once the mount
# carries it (credential_proxy.MANAGED_REPOSITORY_CACHE_SECONDS).
MOUNT_WAIT_S = 240
MOUNT_POLL_S = 5
BROKER_CACHE_S = 30
# How long a leftover namespace may take to go before the plant writes a
# fresh one, and how long the workload's rollout is given before the plant
# goes on without it (the case reads the Deployment's spec, not its pods).
NAMESPACE_DELETE_WAIT_S = 180
ROLLOUT_WAIT_S = 300
KUBECONFIG_FILE = "config"
KUBECONFIG_DIR_PREFIX = "context-fix-"
USAGE = "usage: plant.py plant|teardown"

HttpFn = Callable[[str, str, "dict | None"], "tuple[int, object]"]
# `(argv, stdin text) -> (exit code, stdout, stderr)`.
RunFn = Callable[[list[str], "str | None"], "tuple[int, str, str]"]


class Settings(NamedTuple):
    gitops_repo: str
    token_file: str
    cluster: str
    cluster_location: str
    project: str
    agent_namespace: str
    agent_name: str
    fixture_dir: str
    context_owner: str
    context_name: str
    context_ref: str
    build_id: str
    pull_number: str

    def github_token(self) -> str:
        """Read for each request: the runner renews the file while the unit runs."""
        return Path(self.token_file).read_text(encoding="utf-8").strip()

    @property
    def directory(self) -> str:
        return GITOPS_DIRECTORY.format(cluster=self.cluster)

    @property
    def context_url(self) -> str:
        return f"{GITHUB_HOST}/{self.context_owner}/{self.context_name}"

    @property
    def context_entry(self) -> dict:
        return {"type": CONTEXT_REPO_TYPE, "url": self.context_url, "ref": self.context_ref}

    @property
    def state_configmap(self) -> str:
        return STATE_CONFIGMAP.format(agent=self.agent_name)

    @classmethod
    def from_env(cls) -> "Settings":
        read = os.environ.get
        return cls(
            gitops_repo=read("CONTEXT_FIX_GITOPS_REPO", ""),
            token_file=read("CONTEXT_FIX_GITOPS_TOKEN_FILE", ""),
            cluster=read("CONTEXT_FIX_HOST_CLUSTER", ""),
            cluster_location=read("CONTEXT_FIX_HOST_CLUSTER_LOCATION", ""),
            project=read("CONTEXT_FIX_PROJECT", ""),
            agent_namespace=read("CONTEXT_FIX_AGENT_NAMESPACE", ""),
            agent_name=read("CONTEXT_FIX_AGENT_NAME", ""),
            fixture_dir=read("CONTEXT_FIX_FIXTURE_DIR", ""),
            context_owner=read("CONTEXT_FIX_CONTEXT_OWNER", ""),
            context_name=read("CONTEXT_FIX_CONTEXT_NAME", ""),
            context_ref=read("CONTEXT_FIX_CONTEXT_REF", ""),
            build_id=read("CONTEXT_FIX_BUILD_ID", "") or NO_BUILD_ID,
            pull_number=read("CONTEXT_FIX_PULL_NUMBER", "") or NO_PULL_NUMBER,
        )


def log(message: str) -> None:
    print(f"[context-repository-owns-the-fix] {message}", file=sys.stderr, flush=True)


def github_http(token: Callable[[], str], sleep: Callable[[float], None] = time.sleep) -> HttpFn:
    """A GitHub REST call: `(method, path, body)` to `(status, parsed body)`."""

    def once(method: str, path: str, body: dict | None) -> tuple[int, object]:
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
        except OSError:
            return NO_ANSWER, None

    def call(method: str, path: str, body: dict | None) -> tuple[int, object]:
        for delay in (*RETRY_DELAYS_S, None):
            status, parsed = once(method, path, body)
            if delay is None or (status != NO_ANSWER and status < RETRYABLE_STATUS_FLOOR):
                return status, parsed
            log(f"{method} {path} answered {status or 'nothing'}; trying again in {delay}s")
            sleep(delay)
        raise AssertionError("unreachable")

    return call


def subprocess_run(argv: list[str], stdin: str | None = None) -> tuple[int, str, str]:
    done = subprocess.run(argv, input=stdin, capture_output=True, text=True, check=False)
    return done.returncode, done.stdout, done.stderr


def is_ours(text: str) -> bool:
    """The file carries the stack's label, on a line of its own."""
    return any(line.strip() == f"{MANAGED_BY_KEY}: {MANAGED_BY}" for line in text.splitlines())


def planted_text(fixture_text: str, settings: Settings) -> str:
    """The fixture file with the context repository's words replaced."""
    for word, field in CONTEXT_WORDS.items():
        fixture_text = fixture_text.replace(word, getattr(settings, field))
    return fixture_text


class Stack:
    def __init__(self, settings: Settings, http: HttpFn, run: RunFn = subprocess_run,
                 sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.monotonic):
        self.settings = settings
        self.http = http
        self.run = run
        self.sleep = sleep
        self.now = now
        self._kubeconfig_dir: str | None = None

    # -- settings and files ----------------------------------------------------

    def check_settings(self) -> None:
        required = (
            ("gitops_repo", self.settings.gitops_repo),
            ("bench_gitops_write_token_file", self.settings.token_file),
            ("host_cluster_name", self.settings.cluster),
            ("host_cluster_location", self.settings.cluster_location),
            ("project_id", self.settings.project),
            ("agent_namespace", self.settings.agent_namespace),
            ("agent_name", self.settings.agent_name),
            ("fixture_dir", self.settings.fixture_dir),
            ("context_repository_owner", self.settings.context_owner),
            ("context_repository_name", self.settings.context_name),
            ("context_repository_ref", self.settings.context_ref),
        )
        missing = [name for name, value in required if not value]
        if missing:
            raise RuntimeError(f"{', '.join(missing)} must be set: the GitOps repository and a file that holds a write token for it, the agent host and its install, the case's fixture directory, and the context repository")
        if not (Path(self.settings.fixture_dir) / FIXTURE_LIVE_MANIFEST).is_file():
            # main.tf resolves the directory relative to the stack, which a
            # copy of the tf root alone (devops-bench's isolated mode) loses;
            # the variable names the way round.
            raise RuntimeError(f"{self.settings.fixture_dir} does not hold the case's fixture ({FIXTURE_LIVE_MANIFEST} is missing); when the stack runs from a copy of bench/tf, set TF_VAR_fixture_dir to the absolute path of bench/tasks/{CASE}/fixture")

    def fixture_file(self, relative: Path) -> str:
        return (Path(self.settings.fixture_dir) / relative).read_text(encoding="utf-8")

    def planted_files(self) -> dict[str, str]:
        """`{path in the GitOps repository: text}` for the four files."""
        return {
            f"{self.settings.directory}/{name}": planted_text(self.fixture_file(FIXTURE_GITOPS_SUBDIR / name), self.settings)
            for name in GITOPS_FILES
        }

    # -- GitHub ----------------------------------------------------------------

    def default_branch(self) -> str:
        status, body = self.http("GET", f"/repos/{self.settings.gitops_repo}", None)
        if status != HTTP_OK or not isinstance(body, dict):
            raise RuntimeError(f"cannot read {self.settings.gitops_repo} (HTTP {status})")
        return str(body["default_branch"])

    def remote_file(self, path: str, branch: str) -> tuple[str, str] | None:
        """`(text, sha)` of `path` on `branch`, or None when it is not there."""
        query = urllib.parse.urlencode({"ref": branch})
        status, body = self.http("GET", f"/repos/{self.settings.gitops_repo}/contents/{path}?{query}", None)
        if status == HTTP_NOT_FOUND:
            return None
        if status != HTTP_OK or not isinstance(body, dict):
            raise RuntimeError(f"cannot read {path} (HTTP {status})")
        return base64.b64decode(str(body.get("content") or "")).decode(), str(body["sha"])

    def plant_gitops(self) -> None:
        branch = self.default_branch()
        files = self.planted_files()
        for path in files:
            existing = self.remote_file(path, branch)
            if existing is not None and not is_ours(existing[0]):
                raise RuntimeError(f"{path} exists in {self.settings.gitops_repo} and is not this stack's; refusing to write over it")
        for path, text in files.items():
            existing = self.remote_file(path, branch)
            body = {
                "message": COMMIT_PLANT.format(case=CASE),
                "content": base64.b64encode(text.encode()).decode(),
                "branch": branch,
            }
            if existing is not None:
                body["sha"] = existing[1]
            status, _ = self.http("PUT", f"/repos/{self.settings.gitops_repo}/contents/{path}", body)
            if status not in PUT_OK_STATUSES:
                # The call tries a write again after no answer, so a commit
                # that landed while its answer was lost comes back as a
                # conflict. The file is read back: the stack's text there is
                # the commit.
                now = self.remote_file(path, branch)
                if now is None or now[0] != text:
                    raise RuntimeError(f"cannot commit {path} (HTTP {status})")
                log(f"the commit of {path} answered HTTP {status}, and the file is there; it landed")
        log(f"planted {self.settings.directory}/ in {self.settings.gitops_repo}")

    def open_pulls(self) -> list[dict]:
        found: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            query = urllib.parse.urlencode({"state": "open", "per_page": str(GITHUB_PAGE), "page": str(page)})
            status, batch = self.http("GET", f"/repos/{self.settings.gitops_repo}/pulls?{query}", None)
            if status != HTTP_OK or not isinstance(batch, list):
                raise RuntimeError(f"cannot list pull requests (HTTP {status})")
            found.extend(pull for pull in batch if isinstance(pull, dict))
            if len(batch) < GITHUB_PAGE:
                break
        return found

    def changed_files(self, number: int) -> list[str]:
        names: list[str] = []
        for page in range(1, MAX_PAGES + 1):
            query = urllib.parse.urlencode({"per_page": str(GITHUB_PAGE), "page": str(page)})
            status, batch = self.http("GET", f"/repos/{self.settings.gitops_repo}/pulls/{number}/files?{query}", None)
            if status != HTTP_OK or not isinstance(batch, list):
                raise RuntimeError(f"cannot list the files of pull request {number} (HTTP {status})")
            names.extend(str(item.get("filename") or "") for item in batch if isinstance(item, dict))
            if len(batch) < GITHUB_PAGE:
                break
        return names

    def is_agent_pull(self, pull: dict) -> bool:
        """A `[bot]` author, from a branch in the repository itself, as the reset decides."""
        author = str((pull.get("user") or {}).get("login") or "")
        head_repo = str(((pull.get("head") or {}).get("repo") or {}).get("full_name") or "")
        return author.endswith(BOT_LOGIN_SUFFIX) and head_repo.lower() == self.settings.gitops_repo.lower()

    def close_pull_requests(self) -> None:
        """Close the agent's open pull requests that change a file under the directory."""
        repo = self.settings.gitops_repo
        release = f"{self.settings.directory}/{RELEASE_FILE}"
        existing = self.remote_file(release, self.default_branch())
        if existing is None or not is_ours(existing[0]):
            log(f"{release} is not this stack's on the default branch; no pull request is closed")
            return
        prefix = f"{self.settings.directory}/"
        failures = []
        for pull in self.open_pulls():
            number = pull.get("number")
            if not self.is_agent_pull(pull):
                continue
            if not any(name.startswith(prefix) for name in self.changed_files(number)):
                continue
            labels = {str(label.get("name") or "") for label in pull.get("labels") or [] if isinstance(label, dict)}
            # The label goes on before the close, never after: a close the
            # audit reads back unlabelled is a person's refusal of the fix.
            if AUDIT_REMEDIATION_LABEL in labels:
                status, _ = self.http("POST", f"/repos/{repo}/issues/{number}/labels", {"labels": [STALE_CLOSED_LABEL]})
                if status not in LABEL_OK_STATUSES:
                    failures.append(f"cannot label pull request {number} {STALE_CLOSED_LABEL} (HTTP {status}); it stays open")
                    continue
            status, _ = self.http("PATCH", f"/repos/{repo}/pulls/{number}", {"state": "closed"})
            if status != HTTP_OK:
                failures.append(f"cannot close pull request {number} (HTTP {status})")
                continue
            log(f"closed pull request {number}, which changes a file under {prefix}")
        if failures:
            raise RuntimeError("; ".join(failures))

    def delete_gitops(self) -> None:
        branch = self.default_branch()
        failures = []
        for name in GITOPS_FILES:
            path = f"{self.settings.directory}/{name}"
            existing = self.remote_file(path, branch)
            if existing is None:
                continue
            if not is_ours(existing[0]):
                log(f"{path} is not this stack's; it stays")
                continue
            body = {"message": COMMIT_REMOVE.format(case=CASE), "sha": existing[1], "branch": branch}
            status, _ = self.http("DELETE", f"/repos/{self.settings.gitops_repo}/contents/{path}", body)
            if status != HTTP_OK:
                # As for the commit: a delete that landed while its answer
                # was lost comes back as a 404 or a conflict. The file is
                # read back: absent is deleted.
                if self.remote_file(path, branch) is not None:
                    failures.append(f"cannot delete {path} (HTTP {status})")
                    continue
                log(f"the delete of {path} answered HTTP {status}, and the file is gone; it landed")
            log(f"removed {path} from {self.settings.gitops_repo}")
        if failures:
            raise RuntimeError("; ".join(failures))

    # -- the agent host --------------------------------------------------------

    def kubectl(self, *args: str, stdin: str | None = None) -> tuple[int, str, str]:
        """kubectl against the agent host, on a kubeconfig fetched once per process."""
        if self._kubeconfig_dir is None:
            # A directory, not a file: get-credentials refuses to load an
            # existing empty file and writes a dated backup beside it.
            self._kubeconfig_dir = tempfile.mkdtemp(prefix=KUBECONFIG_DIR_PREFIX)
            kubeconfig = str(Path(self._kubeconfig_dir) / KUBECONFIG_FILE)
            # gcloud writes to the file KUBECONFIG names; `env` scopes it to
            # this one call, so the runner's own kubeconfig is not touched.
            code, _, err = self.run([
                "env", f"KUBECONFIG={kubeconfig}",
                "gcloud", "container", "clusters", "get-credentials", self.settings.cluster,
                "--location", self.settings.cluster_location, "--project", self.settings.project, "--quiet",
            ], None)
            if code != 0:
                raise RuntimeError(f"cannot fetch credentials for {self.settings.cluster} in {self.settings.project}: {err.strip()}")
        kubeconfig = str(Path(self._kubeconfig_dir) / KUBECONFIG_FILE)
        return self.run(["kubectl", "--kubeconfig", kubeconfig, *args], stdin)

    def close(self) -> None:
        if self._kubeconfig_dir is not None:
            shutil.rmtree(self._kubeconfig_dir, ignore_errors=True)
            self._kubeconfig_dir = None

    def namespace_owner(self) -> str | None:
        """The namespace's managed-by label, "" when unlabelled, None when absent."""
        # --ignore-not-found: an absent namespace is an empty answer with exit
        # 0, so absence is read from the value, not from kubectl's wording.
        code, out, err = self.kubectl("get", "namespace", WORKLOAD_NAMESPACE, "--ignore-not-found", "-o", "json")
        if code != 0:
            raise RuntimeError(f"cannot read namespace {WORKLOAD_NAMESPACE} on {self.settings.cluster}: {err.strip()}")
        if not out.strip():
            return None
        labels = ((json.loads(out) or {}).get("metadata") or {}).get("labels") or {}
        return str(labels.get(MANAGED_BY_KEY) or "")

    def plant_workload(self) -> None:
        owner = self.namespace_owner()
        if owner is not None and owner != MANAGED_BY:
            raise RuntimeError(f"namespace {WORKLOAD_NAMESPACE} exists on {self.settings.cluster} and is not labelled {MANAGED_BY_KEY}={MANAGED_BY} (found {owner!r}); this stack did not create it, so it does not write into it")
        if owner == MANAGED_BY:
            log(f"found a leftover {WORKLOAD_NAMESPACE} from an earlier run; deleting it before planting")
            code, _, err = self.kubectl("delete", "namespace", WORKLOAD_NAMESPACE, "--ignore-not-found", "--wait=true", f"--timeout={NAMESPACE_DELETE_WAIT_S}s")
            if code != 0:
                raise RuntimeError(f"cannot clear the leftover {WORKLOAD_NAMESPACE} within {NAMESPACE_DELETE_WAIT_S}s: {err.strip()}")
        # The namespace is created and labelled before the manifest goes on,
        # so a manifest that half-applies still leaves a namespace the
        # teardown may delete.
        code, out, err = self.kubectl("create", "namespace", WORKLOAD_NAMESPACE, "--dry-run=client", "-o", "yaml")
        if code != 0:
            raise RuntimeError(f"cannot render namespace {WORKLOAD_NAMESPACE}: {err.strip()}")
        code, _, err = self.kubectl("apply", "-f", "-", stdin=out)
        if code != 0:
            raise RuntimeError(f"cannot create namespace {WORKLOAD_NAMESPACE}: {err.strip()}")
        code, _, err = self.kubectl(
            "label", "namespace", WORKLOAD_NAMESPACE, "--overwrite",
            f"{MANAGED_BY_KEY}={MANAGED_BY}",
            f"{BUILD_ID_LABEL_KEY}={self.settings.build_id}",
            f"{PULL_NUMBER_LABEL_KEY}={self.settings.pull_number}",
        )
        if code != 0:
            raise RuntimeError(f"cannot label namespace {WORKLOAD_NAMESPACE}: {err.strip()}")
        code, _, err = self.kubectl("apply", "-f", "-", stdin=self.fixture_file(FIXTURE_LIVE_MANIFEST))
        if code != 0:
            raise RuntimeError(f"cannot apply the live workload to {self.settings.cluster}: {err.strip()}")
        log(f"applied the payments-api workload to {WORKLOAD_NAMESPACE} on {self.settings.cluster}")
        code, _, err = self.kubectl("-n", WORKLOAD_NAMESPACE, "rollout", "status", f"deployment/{WORKLOAD_DEPLOYMENT}", f"--timeout={ROLLOUT_WAIT_S}s")
        if code != 0:
            # The case reads the Deployment's spec, which is there; a pod
            # still pending on a fresh node is reported, not a failure.
            log(f"WARNING: {WORKLOAD_DEPLOYMENT} did not roll out within {ROLLOUT_WAIT_S}s ({err.strip()}); the declaration is planted, the pod is not Ready")

    def delete_workload(self) -> None:
        owner = self.namespace_owner()
        if owner is None:
            return
        if owner != MANAGED_BY:
            log(f"namespace {WORKLOAD_NAMESPACE} is not this stack's; it stays")
            return
        code, _, err = self.kubectl("delete", "namespace", WORKLOAD_NAMESPACE, "--ignore-not-found", "--wait=false")
        if code != 0:
            raise RuntimeError(f"cannot delete namespace {WORKLOAD_NAMESPACE}: {err.strip()}")
        log(f"deleted namespace {WORKLOAD_NAMESPACE} on {self.settings.cluster}")

    # -- the registration ------------------------------------------------------

    def context_repos(self) -> list | None:
        """The ConfigMap's context_repos list, None when the key is absent."""
        code, out, err = self.kubectl("-n", self.settings.agent_namespace, "get", "configmap", self.settings.state_configmap, "-o", "json")
        if code != 0:
            raise RuntimeError(f"cannot read ConfigMap {self.settings.state_configmap} in {self.settings.agent_namespace}: {err.strip()}")
        data = (json.loads(out) or {}).get("data") or {}
        raw = data.get(CONTEXT_REPOS_KEY)
        if raw is None or not str(raw).strip():
            return None
        entries = json.loads(raw)
        if not isinstance(entries, list):
            raise RuntimeError(f"{CONTEXT_REPOS_KEY} in {self.settings.state_configmap} is not a JSON list; refusing to rewrite it")
        return entries

    def names_context_repository(self, entry: object) -> bool:
        """The entry is for the context repository's URL, whatever else it says."""
        return isinstance(entry, dict) and str(entry.get("url") or "").rstrip("/").lower() == self.settings.context_url.lower()

    def is_registration(self, entry: object) -> bool:
        """The entry is the stack's: the same type, URL and ref it writes."""
        return self.names_context_repository(entry) and {k: entry.get(k) for k in self.settings.context_entry} == self.settings.context_entry

    def write_context_repos(self, entries: list | None) -> None:
        if entries is None:
            patch = json.dumps([{"op": "remove", "path": f"/data/{CONTEXT_REPOS_KEY}"}])
            code, _, err = self.kubectl("-n", self.settings.agent_namespace, "patch", "configmap", self.settings.state_configmap, "--type", "json", "-p", patch)
        else:
            patch = json.dumps({"data": {CONTEXT_REPOS_KEY: json.dumps(entries)}})
            code, _, err = self.kubectl("-n", self.settings.agent_namespace, "patch", "configmap", self.settings.state_configmap, "--type", "merge", "-p", patch)
        if code != 0:
            raise RuntimeError(f"cannot write {CONTEXT_REPOS_KEY} in {self.settings.state_configmap}: {err.strip()}")

    def register(self) -> None:
        entries = self.context_repos() or []
        foreign = [entry for entry in entries if self.names_context_repository(entry) and not self.is_registration(entry)]
        if foreign:
            # As for a file or a namespace that is not the stack's: an entry
            # someone registered by hand, with another ref or type, is
            # neither adopted nor removed.
            raise RuntimeError(f"{CONTEXT_REPOS_KEY} in {self.settings.state_configmap} already names {self.settings.context_url} with other fields ({json.dumps(foreign[0])}); this stack did not register it, so it does not write over it")
        if any(self.is_registration(entry) for entry in entries):
            log(f"{self.settings.context_url} at {self.settings.context_ref} is already in {CONTEXT_REPOS_KEY}; the stack's own entry is kept")
        else:
            self.write_context_repos([*entries, self.settings.context_entry])
            log(f"registered {self.settings.context_url} at {self.settings.context_ref} in {CONTEXT_REPOS_KEY}")
        self.wait_for_mounts()

    def unregister(self) -> None:
        entries = self.context_repos()
        if entries is None:
            return
        kept = [entry for entry in entries if not self.is_registration(entry)]
        if len(kept) == len(entries):
            return
        self.write_context_repos(kept or None)
        log(f"removed {self.settings.context_url} from {CONTEXT_REPOS_KEY}")

    def mount_carries_entry(self, target: str, container: str) -> bool:
        code, out, _ = self.kubectl(
            "-n", self.settings.agent_namespace, "exec", target, "-c", container, "--",
            "cat", f"{GITOPS_STATE_DIR}/{CONTEXT_REPOS_KEY}",
        )
        return code == 0 and self.settings.context_url in out

    def wait_for_mounts(self) -> None:
        """Until the broker and the shell sandbox see the entry, then the broker's cache window."""
        agent = self.settings.agent_name
        targets = [
            (f"deployment/{BROKER_DEPLOYMENT.format(agent=agent)}", BROKER_CONTAINER),
            (f"statefulset/{SHELL_STATEFULSET.format(agent=agent)}", SHELL_CONTAINER),
        ]
        deadline = self.now() + MOUNT_WAIT_S
        pending = list(targets)
        while pending:
            pending = [(target, container) for target, container in pending if not self.mount_carries_entry(target, container)]
            if not pending:
                break
            if self.now() >= deadline:
                names = ", ".join(f"{target} ({container})" for target, container in pending)
                raise RuntimeError(f"{names} did not see {self.settings.context_url} under {GITOPS_STATE_DIR}/{CONTEXT_REPOS_KEY} within {MOUNT_WAIT_S}s")
            self.sleep(MOUNT_POLL_S)
        log(f"the broker and the shell sandbox see the registration; waiting {BROKER_CACHE_S}s for the broker's cache")
        self.sleep(BROKER_CACHE_S)

    # -- plant and teardown ----------------------------------------------------

    def plant(self) -> None:
        self.check_settings()
        self.plant_gitops()
        self.plant_workload()
        self.register()

    def teardown(self) -> bool:
        """Remove what the plant wrote. True when every step succeeded."""
        try:
            self.check_settings()
        except RuntimeError as exc:
            log(f"ERROR: {exc}; nothing is removed")
            return False
        ok = True
        for step in (self.unregister, self.delete_workload, self.close_pull_requests, self.delete_gitops):
            try:
                step()
            except Exception as exc:  # noqa: BLE001 -- every step runs
                log(f"ERROR: {step.__name__} failed: {exc}")
                ok = False
        return ok


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in ("plant", "teardown"):
        log(USAGE)
        return 2
    settings = Settings.from_env()
    stack = Stack(settings, github_http(settings.github_token))
    try:
        if argv[1] == "teardown":
            return 0 if stack.teardown() else 1
        try:
            stack.plant()
        except Exception as exc:  # noqa: BLE001 -- reported, then cleaned up
            log(f"ERROR: the plant failed: {exc}; removing what it wrote")
            stack.teardown()
            return 1
        return 0
    finally:
        stack.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
