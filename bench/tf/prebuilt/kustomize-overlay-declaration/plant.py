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

"""Plant and remove the Kustomize root of
bench/tasks/obtainability-pdb-kustomize-overlay.

main.tf runs this on the runner as `python3 plant.py plant` and
`python3 plant.py teardown`, with the settings in the environment
(`Settings.from_env`).

Every pool GitOps repository holds the seeded fleet's checkout-gateway
Deployment at `<directory>/checkout-gateway.yaml`
(scripts/provision_ci_pool_project.sh seeds it). The plant adds one file
beside it, `<directory>/kustomization.yaml`, that lists it as the only
resource. The directory is then a Kustomize root, so a remediation that adds
a new file there renders only when the kustomization lists it. The file
carries the stack's label (`metadata.labels.managed-by: kube-agents-bench`),
which is how the teardown, and a later plant, know that the file is the
stack's.

plant:

1. Refuses when `<directory>/checkout-gateway.yaml` is not on the default
   branch: without the declaration the root declares nothing.
2. Refuses when `<directory>/kustomization.yaml` is there and does not carry
   the label: the file is not the stack's. A file that carries it (left by a
   run that was stopped before its teardown) is written again.
3. Commits the kustomization to the default branch.

A failure after the commit runs teardown before the script exits: Terraform
skips the destroy provisioner of a resource whose create failed.

teardown, each step independent of the other:

1. Closes every open pull request that a `[bot]` login opened from a branch in
   the repository and that changes a file under `<directory>/`, but only when
   the kustomization on the default branch is the stack's. A pull request
   that carries `audit:remediation` gets `audit:stale-closed` first, as
   hack/ci_reset_agent_pulls.py does: the audit reads an unlabelled close as
   a person's refusal and does not propose that fix again. A label that does
   not stick leaves the pull request open, and the step fails.
2. Deletes the kustomization from the default branch when it is the stack's.

The GitHub token is read from the file that `KUSTOMIZE_EVAL_GITOPS_TOKEN_FILE`
names, again for each request, because hack/ci-eval-pr.sh renews it while the
unit runs. The token is never on argv and never printed.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, NamedTuple

# The label the planted file carries, and the value that makes it this
# stack's. The same pair as the other stacks that write to the GitOps
# repository.
MANAGED_BY_KEY = "managed-by"
MANAGED_BY = "kube-agents-bench"
CASE = "obtainability-pdb-kustomize-overlay"
KUSTOMIZATION_FILE = "kustomization.yaml"
DECLARATION_FILE = "checkout-gateway.yaml"
KUSTOMIZATION_API_VERSION = "kustomize.config.k8s.io/v1beta1"
KUSTOMIZATION_KIND = "Kustomization"
COMMIT_PLANT = "test(bench): plant a Kustomize root for {case}"
COMMIT_REMOVE = "test(bench): remove the Kustomize root of {case}"
# The audit's labels, as hack/ci_sweep_agent_pulls.py names them;
# bench/tests/test_kustomize_overlay_declaration_stack.py holds them equal.
AUDIT_REMEDIATION_LABEL = "audit:remediation"
STALE_CLOSED_LABEL = "audit:stale-closed"
BOT_LOGIN_SUFFIX = "[bot]"
GITHUB_API = "https://api.github.com"
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
USAGE = "usage: plant.py plant|teardown"

HttpFn = Callable[[str, str, "dict | None"], "tuple[int, object]"]


class Settings(NamedTuple):
    gitops_repo: str
    directory: str
    token_file: str

    def github_token(self) -> str:
        """Read for each request: the runner renews the file while the unit runs."""
        return Path(self.token_file).read_text(encoding="utf-8").strip()

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            gitops_repo=os.environ.get("KUSTOMIZE_EVAL_GITOPS_REPO", ""),
            directory=os.environ.get("KUSTOMIZE_EVAL_DIRECTORY", "").strip("/"),
            token_file=os.environ.get("KUSTOMIZE_EVAL_GITOPS_TOKEN_FILE", ""),
        )


def log(message: str) -> None:
    print(f"[kustomize-overlay-declaration] {message}", file=sys.stderr, flush=True)


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


def kustomization_text() -> str:
    """The planted file: the declaration as the only resource, with the label."""
    return (
        f"apiVersion: {KUSTOMIZATION_API_VERSION}\n"
        f"kind: {KUSTOMIZATION_KIND}\n"
        "metadata:\n"
        "  name: seeded-reliability\n"
        "  labels:\n"
        f"    {MANAGED_BY_KEY}: {MANAGED_BY}\n"
        "resources:\n"
        f"  - {DECLARATION_FILE}\n"
    )


def is_ours(text: str) -> bool:
    """The file carries the stack's label, on a line of its own."""
    return any(line.strip() == f"{MANAGED_BY_KEY}: {MANAGED_BY}" for line in text.splitlines())


class Stack:
    def __init__(self, settings: Settings, http: HttpFn):
        self.settings = settings
        self.http = http

    @property
    def kustomization_path(self) -> str:
        return f"{self.settings.directory}/{KUSTOMIZATION_FILE}"

    def check_settings(self) -> None:
        missing = [name for name, value in (
            ("gitops_repo", self.settings.gitops_repo),
            ("cluster_directory", self.settings.directory),
            ("bench_gitops_write_token_file", self.settings.token_file),
        ) if not value]
        if missing:
            raise RuntimeError(f"{', '.join(missing)} must be set: the GitOps repository, the directory of the declaration, and the file that holds a write token for the repository")

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

    # -- plant -----------------------------------------------------------------

    def plant(self) -> None:
        self.check_settings()
        branch = self.default_branch()
        declaration = f"{self.settings.directory}/{DECLARATION_FILE}"
        if self.remote_file(declaration, branch) is None:
            raise RuntimeError(f"{declaration} is not on {branch} in {self.settings.gitops_repo}; the Kustomize root would declare nothing (scripts/provision_ci_pool_project.sh seeds it)")
        existing = self.remote_file(self.kustomization_path, branch)
        if existing is not None and not is_ours(existing[0]):
            raise RuntimeError(f"{self.kustomization_path} exists in {self.settings.gitops_repo} and is not this stack's; refusing to write over it")
        body = {
            "message": COMMIT_PLANT.format(case=CASE),
            "content": base64.b64encode(kustomization_text().encode()).decode(),
            "branch": branch,
        }
        if existing is not None:
            body["sha"] = existing[1]
        status, _ = self.http("PUT", f"/repos/{self.settings.gitops_repo}/contents/{self.kustomization_path}", body)
        if status not in PUT_OK_STATUSES:
            raise RuntimeError(f"cannot commit {self.kustomization_path} (HTTP {status})")
        log(f"planted {self.kustomization_path} in {self.settings.gitops_repo}")

    # -- teardown --------------------------------------------------------------

    def teardown(self) -> bool:
        """Remove what the plant wrote. True when every step succeeded."""
        try:
            self.check_settings()
        except RuntimeError as exc:
            log(f"ERROR: {exc}; nothing is removed")
            return False
        ok = True
        for step in (self.close_pull_requests, self.delete_kustomization):
            try:
                step()
            except Exception as exc:  # noqa: BLE001 -- every step runs
                log(f"ERROR: {step.__name__} failed: {exc}")
                ok = False
        return ok

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
        existing = self.remote_file(self.kustomization_path, self.default_branch())
        if existing is None or not is_ours(existing[0]):
            log(f"{self.kustomization_path} is not this stack's on the default branch; no pull request is closed")
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

    def delete_kustomization(self) -> None:
        branch = self.default_branch()
        existing = self.remote_file(self.kustomization_path, branch)
        if existing is None:
            return
        if not is_ours(existing[0]):
            log(f"{self.kustomization_path} is not this stack's; it stays")
            return
        body = {"message": COMMIT_REMOVE.format(case=CASE), "sha": existing[1], "branch": branch}
        status, _ = self.http("DELETE", f"/repos/{self.settings.gitops_repo}/contents/{self.kustomization_path}", body)
        if status != HTTP_OK:
            raise RuntimeError(f"cannot delete {self.kustomization_path} (HTTP {status})")
        log(f"removed {self.kustomization_path} from {self.settings.gitops_repo}")


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in ("plant", "teardown"):
        log(USAGE)
        return 2
    settings = Settings.from_env()
    stack = Stack(settings, github_http(settings.github_token))
    if argv[1] == "teardown":
        return 0 if stack.teardown() else 1
    try:
        stack.plant()
    except Exception as exc:  # noqa: BLE001 -- reported, then cleaned up
        log(f"ERROR: the plant failed: {exc}; removing what it wrote")
        stack.teardown()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
