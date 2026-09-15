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

"""Wait for the GitOps fix cycle to land after the agent's turn.

Option C pilot (gke-labs/kube-agents#1307). In the GitOps cases the agent never
writes to the task cluster: it opens a pull request against a per-run branch
of the GitOps repository, a workflow in that repository merges the PR when it
passes, and Argo CD in the task cluster syncs the merge. The verifiers grade
the cluster, so the harness has to hold the run open until that sync has
happened, or until it is clear it never will.

Three phases, each bounded:

1. **PR**: a pull request whose base is the run branch, created after this run
   started, appears. None within ``GITOPS_PR_TIMEOUT`` seconds is the
   ``no_pr`` outcome. The lower bound on creation time matters: a rerun that
   reuses a cluster name reuses the run branch, and the previous run's merged
   PR is still listed against it.
2. **Merge**: the PR merges, or its check concludes failure, or it is closed
   unmerged. Failure or close is ``pr_rejected``; the cluster stays broken and
   the verifiers grade it that way, which is the point.
3. **Sync**: the Argo Application reports ``Synced`` at the run branch's
   current head and ``Healthy``. The head is re-read each poll because it can
   move after the merge (Argo tracks the branch, not the merge commit);
   ``head_moved_after_merge`` records when it did. Then the verifiers run
   against the recovered cluster: ``merged``.

The outcome and its evidence land in ``result.metadata["gitops"]`` and, because
devops-bench persists ``trajectory`` but not ``metadata``, in one harness-authored
trajectory entry named ``gitops_fix_cycle``. The pilot records the outcome and
does not score it (scoring it is Wave 1). A timeout in phase 2 or 3 is its own
outcome (``merge_timeout``, ``sync_timeout``) so a slow workflow is not misread
as a rejected PR.

A failed poll (GitHub 5xx, a rate limit, a kubectl error) is logged, counted on
the record, and retried until the phase's deadline; it never ends the wait on
its own, because merge windows are exactly when a hosted API is most likely to
hiccup for a moment.

Active only when ``GITOPS_RUN_BRANCH`` is set; every other case is untouched.

Env:
  GITOPS_RUN_BRANCH      per-run branch the PR must target (required to activate)
  GITOPS_REPO            https URL of the GitOps repository
  GITOPS_ARGO_APP        Application name (default ``b-0011``)
  GITOPS_ARGO_NAMESPACE  Application namespace (default ``argocd``)
  GITOPS_ARGO_CONTEXT    kube context of the task cluster (default: current)
  GITOPS_PR_TIMEOUT / GITOPS_MERGE_TIMEOUT / GITOPS_SYNC_TIMEOUT   seconds
  GITOPS_POLL_INTERVAL   seconds between polls (default 15)
  BENCH_GITHUB_TOKEN or GITHUB_TOKEN, else GITOPS_TOKEN_FILE   GitHub read token
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from devops_bench.agents import AgentResult

from kube_agents_bench.verifiers import LEDGER_TOKEN_ENV_VARS

_log = logging.getLogger(__name__)

DEFAULT_REPO = "https://github.com/gke-agentic/fuxiao-gkedemo-infra"
DEFAULT_APP = "b-0011"
DEFAULT_APP_NAMESPACE = "argocd"

DEFAULT_POLL_INTERVAL_S = 15.0
DEFAULT_PR_TIMEOUT_S = 900.0
DEFAULT_MERGE_TIMEOUT_S = 600.0
DEFAULT_SYNC_TIMEOUT_S = 300.0
HTTP_TIMEOUT_S = 30
KUBECTL_TIMEOUT_S = 60

GITHUB_API = "https://api.github.com"
GITHUB_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
# Check-run conclusions that mean the repository's check said no.
REJECTING_CONCLUSIONS = frozenset({"failure", "cancelled", "timed_out", "action_required"})

TRAJECTORY_ENTRY_NAME = "gitops_fix_cycle"

OUTCOME_NO_PR = "no_pr"
OUTCOME_PR_REJECTED = "pr_rejected"
OUTCOME_MERGED = "merged"
OUTCOME_MERGE_TIMEOUT = "merge_timeout"
OUTCOME_SYNC_TIMEOUT = "sync_timeout"

FetchJson = Callable[[str, Mapping[str, str]], Any]
RunKubectl = Callable[[list[str]], str]
PollError = (urllib.error.URLError, subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError)


def repo_slug(url: str) -> str:
    """``https://github.com/o/r[.git][/]`` -> ``o/r``."""
    slug = url.strip()
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:"):
        if slug.startswith(prefix):
            slug = slug[len(prefix) :]
            break
    if slug.endswith(".git"):
        slug = slug[:-4]
    return slug.strip("/")


def _github_token(env: Mapping[str, str]) -> str:
    for name in LEDGER_TOKEN_ENV_VARS:
        if env.get(name):
            return env[name].strip()
    token_file = env.get("GITOPS_TOKEN_FILE")
    if token_file:
        path = Path(os.path.expanduser(token_file))
        if path.is_file():
            return path.read_text().strip()
    return ""


def _default_fetch_json(url: str, headers: Mapping[str, str]) -> Any:
    req = urllib.request.Request(url, headers=dict(headers))
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:  # noqa: S310 - https API URL built from a constant host
        return json.loads(resp.read().decode("utf-8"))


def _default_run_kubectl(args: list[str]) -> str:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["kubectl", *args], check=True, capture_output=True, text=True, timeout=KUBECTL_TIMEOUT_S
    ).stdout


def _seconds(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number of seconds, got {raw!r}") from exc


def _parse_github_time(value: str) -> float:
    """``2026-09-10T21:06:51Z`` -> POSIX seconds."""
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def await_fix_cycle(
    result: AgentResult,
    *,
    since: float | None = None,
    env: Mapping[str, str] | None = None,
    fetch_json: FetchJson | None = None,
    run_kubectl: RunKubectl | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any] | None:
    """Block until the fix cycle lands or is ruled out; record the outcome.

    ``since`` is a POSIX timestamp (``time.time()``) taken before the agent's
    opening turn; pull requests created before it belong to an earlier run of
    the same branch and are ignored. ``None`` disables the bound.

    Returns the record written to ``result.metadata["gitops"]``, or ``None``
    when ``GITOPS_RUN_BRANCH`` is unset and nothing was done. Never raises for
    a GitHub or kubectl failure: those are counted on the record and retried
    until the phase deadline.
    """
    env = os.environ if env is None else env
    branch = env.get("GITOPS_RUN_BRANCH", "")
    if not branch:
        return None
    fetch_json = fetch_json or _default_fetch_json
    run_kubectl = run_kubectl or _default_run_kubectl

    repo = env.get("GITOPS_REPO") or DEFAULT_REPO
    slug = repo_slug(repo)
    app = env.get("GITOPS_ARGO_APP") or DEFAULT_APP
    app_ns = env.get("GITOPS_ARGO_NAMESPACE") or DEFAULT_APP_NAMESPACE
    context = env.get("GITOPS_ARGO_CONTEXT", "")
    poll = _seconds(env, "GITOPS_POLL_INTERVAL", DEFAULT_POLL_INTERVAL_S)
    pr_timeout = _seconds(env, "GITOPS_PR_TIMEOUT", DEFAULT_PR_TIMEOUT_S)
    merge_timeout = _seconds(env, "GITOPS_MERGE_TIMEOUT", DEFAULT_MERGE_TIMEOUT_S)
    sync_timeout = _seconds(env, "GITOPS_SYNC_TIMEOUT", DEFAULT_SYNC_TIMEOUT_S)

    headers = dict(GITHUB_HEADERS)
    token = _github_token(env)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    api = f"{GITHUB_API}/repos/{slug}"

    record: dict[str, Any] = {"repo": slug, "run_branch": branch, "application": f"{app_ns}/{app}", "poll_errors": 0}
    result.metadata["gitops"] = record
    # devops-bench writes ``trajectory`` (and the tool names in it) to
    # results.json but drops ``metadata``, so the outcome also rides along as
    # one harness-authored trajectory entry. The dict is shared, so it carries
    # the final fields whichever way the wait ends.
    result.trajectory.append(
        {"name": TRAJECTORY_ENTRY_NAME, "args": {"run_branch": branch, "repo": slug}, "result": record, "status": "harness"}
    )
    started = clock()

    def elapsed() -> float:
        return clock() - started

    def wait(deadline: float) -> bool:
        """Sleep one poll interval; False when the deadline has passed."""
        if clock() >= deadline:
            return False
        sleep(poll)
        return clock() < deadline

    def attempt(what: str, fn: Callable[[], Any]) -> Any:
        """Run one poll; on failure log it, count it, and return None."""
        try:
            return fn()
        except PollError as exc:
            record["poll_errors"] += 1
            record["last_error"] = f"{what}: {type(exc).__name__}: {exc}"
            _log.warning("gitops: %s failed (%s); retrying until the deadline", what, exc)
            return None

    def finish(outcome: str, **fields: Any) -> dict[str, Any]:
        record.update(outcome=outcome, elapsed_s=round(elapsed(), 1), **fields)
        return record

    # -- 1. a PR against the run branch, created during this run -----------
    pulls_url = f"{api}/pulls?" + urllib.parse.urlencode(
        {"base": branch, "state": "all", "sort": "created", "direction": "desc"}
    )
    pr: dict[str, Any] | None = None
    deadline = clock() + pr_timeout
    while True:
        pulls = attempt("listing pull requests", lambda: fetch_json(pulls_url, headers))
        if pulls:
            for candidate in pulls:
                created = candidate.get("created_at", "")
                if since is not None and created and _parse_github_time(created) < since:
                    continue
                pr = candidate
                break
        if pr is not None:
            break
        if not wait(deadline):
            _log.warning("gitops: no PR against %s within %.0fs", branch, pr_timeout)
            return finish(OUTCOME_NO_PR)
    number = int(pr["number"])
    record.update(pr_number=number, pr_url=pr.get("html_url", ""), head_sha=pr.get("head", {}).get("sha", ""))
    _log.info("gitops: PR #%d found against %s", number, branch)

    # -- 2. merged, or rejected -----------------------------------------
    merge_sha = ""
    deadline = clock() + merge_timeout
    while True:
        detail = attempt(f"reading PR #{number}", lambda: fetch_json(f"{api}/pulls/{number}", headers))
        if detail:
            if detail.get("merged_at"):
                merge_sha = detail.get("merge_commit_sha") or ""
                record.update(merged_at=detail["merged_at"], merge_sha=merge_sha)
                break
            if detail.get("state") == "closed":
                return finish(OUTCOME_PR_REJECTED, reason="closed without merge")
            head_sha = detail.get("head", {}).get("sha", "")
            if head_sha:
                record["head_sha"] = head_sha
                checks = attempt(
                    f"reading check runs for {head_sha[:8]}",
                    lambda: fetch_json(f"{api}/commits/{head_sha}/check-runs", headers),
                )
                if checks:
                    failed = [
                        c.get("name", "")
                        for c in checks.get("check_runs", [])
                        if c.get("status") == "completed" and c.get("conclusion") in REJECTING_CONCLUSIONS
                    ]
                    if failed:
                        _log.warning("gitops: PR #%d rejected by checks %s", number, failed)
                        return finish(OUTCOME_PR_REJECTED, reason=f"check failed: {', '.join(failed)}")
        if not wait(deadline):
            _log.warning("gitops: PR #%d neither merged nor rejected within %.0fs", number, merge_timeout)
            return finish(OUTCOME_MERGE_TIMEOUT)
    _log.info("gitops: PR #%d merged as %s", number, merge_sha[:8])

    # -- 3. Argo synced to the run branch's head, and healthy -----------
    # The head is re-read each poll rather than pinned to the merge commit:
    # the branch can move after the merge (run 7 saw the agent push a
    # follow-up straight onto the run branch), and Argo tracks the branch,
    # so it reports whatever the head is. A head that differs from the merge
    # commit is recorded as such; whether it descends from the merge is not
    # checked here.
    ctx = ["--context", context] if context else []
    args = [*ctx, "-n", app_ns, "get", "application", app, "-o", "json"]
    branch_url = f"{api}/branches/{urllib.parse.quote(branch, safe='')}"
    deadline = clock() + sync_timeout
    while True:
        head_info = attempt("reading the run branch head", lambda: fetch_json(branch_url, headers))
        head = (head_info or {}).get("commit", {}).get("sha", "")
        if head:
            record["branch_head"] = head
            if head != merge_sha:
                record["head_moved_after_merge"] = True
        raw = attempt("reading the Argo Application", lambda: run_kubectl(args))
        status = json.loads(raw).get("status", {}) if raw else {}
        sync = status.get("sync", {})
        health = status.get("health", {})
        if status:
            record.update(
                sync_status=sync.get("status", ""), synced_revision=sync.get("revision", ""), health=health.get("status", "")
            )
        at_head = bool(head) and sync.get("revision") == head
        if sync.get("status") == "Synced" and at_head and health.get("status") == "Healthy":
            _log.info("gitops: application %s Synced at branch head %s and Healthy", app, head[:8])
            return finish(OUTCOME_MERGED)
        if not wait(deadline):
            _log.warning(
                "gitops: application %s not Synced+Healthy at head %s (merge %s) within %.0fs",
                app, head[:8], merge_sha[:8], sync_timeout,
            )
            return finish(OUTCOME_SYNC_TIMEOUT)
