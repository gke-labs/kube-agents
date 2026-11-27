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

"""Take order-ledger's budget away for one post-merge-verified-fixed run, and put it back.

main.tf runs this on the runner as `python3 budget.py remove` (the stack's
apply, before the agent runs) and `python3 budget.py restore` (its destroy,
after the checks), with the settings in the environment.

The fixture's steady state is the fix merged: every pool GitOps repository
holds `<directory>/order-ledger-pdb.yaml`
(scripts/provision_ci_pool_post_merge_sync.sh seeds BUDGET_CONTENT), and
Config Sync on seeded-a keeps the budget applied (bench/tf/fleet/
post-merge-sync.tf), so no audit finds order-ledger unbudgeted.

remove:

1. Refuses when the file is not on the default branch: the steady state is
   broken, and the provisioning script is the repair.
2. Deletes it from the default branch.
3. Waits, up to WAIT_S, until Config Sync has pruned the budget, read with the
   fixture role's read-only kubeconfig. From the DELETE attempt on, anything
   short of a confirmed prune (a refused or lost DELETE answer, a timeout)
   restores before it exits non-zero: Terraform skips the destroy of a
   resource whose create failed.

restore puts BUDGET_CONTENT back on the default branch unless the file there
already is that, then waits, up to WAIT_S, until the applied budget is the
seeded one (its maxUnavailable and labels, not only its name). A
budget the case's merge added is replaced by the seeded one. It runs after the
checks on every path the destroy runs, a failed or errored run included.

A 5xx or no answer from GitHub is tried again, 2 s then 8 s apart. The GitHub token is read from the file `POST_MERGE_GITOPS_TOKEN_FILE` names,
again for each request, because hack/ci-eval-pr.sh renews it while the unit
runs. The token is never on argv and never printed.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

LOG_PREFIX = "[post-merge-sync-budget]"
REPO_ENV = "POST_MERGE_GITOPS_REPO"
TOKEN_FILE_ENV = "POST_MERGE_GITOPS_TOKEN_FILE"
DIRECTORY_ENV = "POST_MERGE_DIRECTORY"
# Where hack/fleet-kubeconfigs.sh writes the fixture roles' read-only kubeconfigs.
KUBECONFIG_DIR_ENV = "BENCH_FLEET_KUBECONFIG_DIR"
FIXTURE_ROLE = "post-merge-sync-workload"
BUDGET_FILE = "order-ledger-pdb.yaml"
BUDGET_NAME = "order-ledger-pdb"
NAMESPACE = "seeded-post-merge"
# scripts/provision_ci_pool_post_merge_sync.sh's BUDGET_CONTENT, as its seed
# command writes it (printf '%s\n'); a test pins the two. The app label is what
# the fixture's state assertion selects it by (bench/tf/fleet/fixtures.json).
BUDGET_CONTENT = (
    "apiVersion: policy/v1\n"
    "kind: PodDisruptionBudget\n"
    "metadata:\n"
    "  name: order-ledger-pdb\n"
    "  namespace: seeded-post-merge\n"
    "  labels:\n"
    "    app: order-ledger\n"
    "spec:\n"
    "  maxUnavailable: 1\n"
    '  selector: {"matchLabels": {"app": "order-ledger"}}\n'
)
COMMIT_REMOVE = "test(bench): remove order-ledger's budget for post-merge-verified-fixed"
COMMIT_RESTORE = "test(bench): restore order-ledger's budget after post-merge-verified-fixed"
GITHUB_API = "https://api.github.com"
HTTP_TIMEOUT_S = 30
HTTP_OK = 200
HTTP_NOT_FOUND = 404
PUT_OK_STATUSES = (200, 201)
# Config Sync polls the branch every 15 s (post-merge-sync.tf) and applies or
# prunes within a minute of a commit it sees; this leaves room for a
# reconciler that restarts once.
WAIT_S = 300.0
POLL_S = 10.0
KUBECTL_TIMEOUT_S = 60
# A transient GitHub answer (a 5xx, or none) is tried again this far apart.
RETRY_DELAYS_S = (2, 8)
RETRYABLE_STATUS_FLOOR = 500
NO_ANSWER = 0
# What restore waits for: the seeded budget's shape, not only its name.
APPLIED_MAX_UNAVAILABLE = 1
APPLIED_LABELS = {"app": "order-ledger"}
USAGE = "usage: budget.py remove|restore"


class GitHubError(Exception):
    """A GitHub answer that leaves the branch's state unknown."""


def log(message: str) -> None:
    print(f"{LOG_PREFIX} {message}", file=sys.stderr, flush=True)


def _github_once(method: str, path: str, body: dict | None) -> tuple[int, object]:
    token = Path(os.environ[TOKEN_FILE_ENV]).read_text(encoding="utf-8").strip()
    request = urllib.request.Request(
        f"{GITHUB_API}/repos/{os.environ[REPO_ENV]}/{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
            text = response.read().decode()
            return response.status, json.loads(text) if text else None
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (OSError, http.client.HTTPException):
        # A response cut short (IncompleteRead) or malformed is no answer, and is tried again.
        return NO_ANSWER, None


def github(method: str, path: str, body: dict | None = None, sleep=time.sleep) -> tuple[int, object]:
    """One GitHub REST call against the leased repository, tried again on a 5xx or no answer: (status, parsed body)."""
    for delay in (*RETRY_DELAYS_S, None):
        status, parsed = _github_once(method, path, body)
        if delay is None or (status != NO_ANSWER and status < RETRYABLE_STATUS_FLOOR):
            return status, parsed
        log(f"{method} {path} answered {status or 'nothing'}; trying again in {delay}s")
        sleep(delay)
    raise AssertionError("unreachable")


def contents_path() -> str:
    directory = os.environ.get(DIRECTORY_ENV, "").strip("/")
    return "contents/" + urllib.parse.quote(f"{directory}/{BUDGET_FILE}")


def read_file() -> tuple[str | None, str | None]:
    """(sha, text) of the budget file on the default branch, or (None, None) when it is not there."""
    status, body = github("GET", contents_path())
    if status == HTTP_NOT_FOUND:
        return None, None
    if status != HTTP_OK or not isinstance(body, dict):
        raise GitHubError(f"GET {contents_path()} answered {status or 'nothing'}")
    # Bytes that are not UTF-8 read as a changed file, which restore replaces.
    return str(body.get("sha") or ""), base64.b64decode(str(body.get("content") or "")).decode("utf-8", errors="replace")


def budget_state() -> dict | None | bool:
    """The applied budget as JSON, {} when there is none, or False when it could not be read."""
    kubeconfig = Path(os.environ.get(KUBECONFIG_DIR_ENV, "")) / f"{FIXTURE_ROLE}.kubeconfig"
    try:
        done = subprocess.run(
            ["kubectl", "--kubeconfig", str(kubeconfig), "get", "poddisruptionbudget", BUDGET_NAME,
             "-n", NAMESPACE, "--ignore-not-found", "-o", "json"],
            capture_output=True, text=True, timeout=KUBECTL_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"could not read the budget: {exc}")
        return False
    if done.returncode != 0:
        log(f"could not read the budget: {done.stderr.strip()[-300:]}")
        return False
    if not done.stdout.strip():
        return {}
    try:
        return json.loads(done.stdout)
    except ValueError:
        log("kubectl printed no JSON for the budget")
        return False


def is_seeded(state) -> bool:
    """The applied budget is the seeded one: its maxUnavailable and labels, not only its name."""
    if not isinstance(state, dict) or not state:
        return False
    labels = (state.get("metadata") or {}).get("labels") or {}
    return (state.get("spec") or {}).get("maxUnavailable") == APPLIED_MAX_UNAVAILABLE and all(
        labels.get(key) == value for key, value in APPLIED_LABELS.items()
    )


def wait_until(done, clock=time.monotonic, sleep=time.sleep) -> bool:
    deadline = clock() + WAIT_S
    while not done(budget_state()):
        if clock() + POLL_S > deadline:
            return False
        sleep(POLL_S)
    return True


def restore() -> int:
    try:
        sha, text = read_file()
    except GitHubError as exc:
        log(f"{exc}; re-run scripts/provision_ci_pool_post_merge_sync.sh")
        return 1
    if text != BUDGET_CONTENT:
        body = {"message": COMMIT_RESTORE, "content": base64.b64encode(BUDGET_CONTENT.encode()).decode()}
        if sha:
            body["sha"] = sha
        status, _ = github("PUT", contents_path(), body)
        if status not in PUT_OK_STATUSES:
            log(f"PUT {contents_path()} answered {status or 'nothing'}; re-run scripts/provision_ci_pool_post_merge_sync.sh")
            return 1
    if not wait_until(is_seeded):
        log(f"Config Sync did not apply the seeded {BUDGET_NAME} within {WAIT_S:.0f}s of the restore")
        return 1
    log(f"{BUDGET_NAME} is back on the branch and applied")
    return 0


def remove() -> int:
    try:
        sha, _ = read_file()
    except GitHubError as exc:
        log(f"{exc}; nothing was written")
        return 1
    if sha is None:
        log(f"{contents_path()} is not on the default branch: the fixture's steady state is broken; "
            "re-run scripts/provision_ci_pool_post_merge_sync.sh")
        return 1
    # From the DELETE attempt on, anything short of a confirmed prune restores
    # before the apply fails: an answer that was lost may have deleted the file.
    removed = False
    try:
        status, _ = github("DELETE", contents_path(), {"message": COMMIT_REMOVE, "sha": sha})
        if status != HTTP_OK:
            log(f"DELETE {contents_path()} answered {status or 'nothing'}")
            return 1
        if not wait_until(lambda state: state == {}):
            log(f"Config Sync did not prune {BUDGET_NAME} within {WAIT_S:.0f}s")
            return 1
        removed = True
    finally:
        if not removed:
            log("restoring the budget before the apply fails")
            restore()
    log(f"{BUDGET_NAME} is off the branch and pruned")
    return 0


def main(argv: list[str]) -> int:
    missing = [name for name in (REPO_ENV, TOKEN_FILE_ENV, DIRECTORY_ENV, KUBECONFIG_DIR_ENV) if not os.environ.get(name)]
    if len(argv) != 2 or argv[1] not in ("remove", "restore"):
        log(USAGE)
        return 2
    if missing:
        log(f"missing {', '.join(missing)}")
        return 1
    return remove() if argv[1] == "remove" else restore()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
