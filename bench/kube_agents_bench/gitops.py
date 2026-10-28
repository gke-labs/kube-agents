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

Three phases, each bounded, and an optional fourth:

1. **PR**: a pull request whose base is the run branch, created after this run
   started, appears. None within ``GITOPS_PR_TIMEOUT`` seconds is the
   ``no_pr`` outcome. The lower bound on creation time matters: a rerun that
   reuses a cluster name reuses the run branch, and the previous run's merged
   PR is still listed against it.
2. **Merge**: the PR merges, or the repository's merge check (the check runs
   ``GITOPS_MERGE_CHECK`` names; other checks on the head are ignored)
   concludes failure, or it is closed unmerged. Failure or close is
   ``pr_rejected`` unless a later PR against the run branch exists, in which
   case the wait moves to that one and records the first as superseded;
   otherwise the cluster stays broken and the verifiers grade it that way,
   which is the point.
3. **Sync**: the Argo Application reports ``Synced`` at the run branch's
   current head and ``Healthy``. The head is re-read each poll because it can
   move after the merge (Argo tracks the branch, not the merge commit);
   ``head_moved_after_merge`` records when it did. Then the verifiers run
   against the recovered cluster: ``merged`` (or phase 4 runs first when
   ``GITOPS_SOAK_SECONDS`` is above zero).
4. **Soak** (only when ``GITOPS_SOAK_SECONDS`` is above zero): the Application
   is polled for that long and must stay ``Synced`` at the revision that
   settled in phase 3 and ``Healthy`` throughout (an ``Unknown`` sync or health
   status or malformed status object is counted in ``poll_errors`` and retried).
   A successful read that is not is ``soak_regressed``, so a fix that comes up
   and then falls over is not recorded as ``merged``.

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
  GITOPS_REPO            https URL of the GitOps repository (required with GITOPS_RUN_BRANCH)
  GITOPS_ARGO_APP        Application name (default: the run branch's last path segment,
                         the task id in ``run/<cluster>/<task>``)
  GITOPS_ARGO_NAMESPACE  Application namespace (default ``argocd``)
  GITOPS_ARGO_CONTEXT    kube context of the task cluster (default: current)
  GITOPS_PR_TIMEOUT / GITOPS_MERGE_TIMEOUT / GITOPS_SYNC_TIMEOUT   seconds
  GITOPS_SOAK_SECONDS    seconds the Application must stay Synced and Healthy after
                         phase 3 (default 0: no soak)
  GITOPS_POLL_INTERVAL   seconds between polls (default 15)
  GITOPS_MERGE_CHECK     comma-separated check-run names whose failure means the
                         repository rejected the PR (default ``check``, the job the
                         pilot repositories' merge-on-green workflow runs)
  BENCH_GITHUB_TOKEN_FILE, BENCH_GITHUB_TOKEN or GITHUB_TOKEN, else GITOPS_TOKEN_FILE
                         GitHub read token
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

from kube_agents_bench import forges

_log = logging.getLogger(__name__)

DEFAULT_APP = "b-0011"
DEFAULT_APP_NAMESPACE = "argocd"

DEFAULT_POLL_INTERVAL_S = 15.0
DEFAULT_PR_TIMEOUT_S = 900.0
DEFAULT_MERGE_TIMEOUT_S = 600.0
DEFAULT_SYNC_TIMEOUT_S = 300.0
# No soak unless GITOPS_SOAK_SECONDS asks for one: the wait ends at the first
# Synced+Healthy reading, as it always has.
DEFAULT_SOAK_S = 0.0
MAX_SOAK_S = 86400.0
# The Argo Application readings phases 3 and 4 wait for, the poll label their
# reads are counted under, and how much of a SHA the soak's log lines and
# reason quote.
ARGO_SYNCED = "Synced"
ARGO_HEALTHY = "Healthy"
ARGO_UNKNOWN = "Unknown"
SHORT_SHA_LEN = 8
READ_APPLICATION = "reading the Argo Application"
HTTP_TIMEOUT_S = 30
KUBECTL_TIMEOUT_S = 60

GITHUB_API = "https://api.github.com"
GITHUB_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
# Check-run conclusions that mean the repository's merge check said no.
# ``action_required`` is not one: GitHub reports it for a workflow run awaiting
# approval, which resolves to a merge once approved.
REJECTING_CONCLUSIONS = frozenset({"failure", "cancelled", "timed_out"})
# The check run whose failure is a rejection, unless GITOPS_MERGE_CHECK says
# otherwise: the ``check`` job of the pilot repositories' merge-on-green
# workflow.
DEFAULT_MERGE_CHECK = "check"

TRAJECTORY_ENTRY_NAME = "gitops_fix_cycle"

OUTCOME_NO_PR = "no_pr"
OUTCOME_PR_REJECTED = "pr_rejected"
OUTCOME_MERGED = "merged"
OUTCOME_MERGE_TIMEOUT = "merge_timeout"
OUTCOME_SYNC_TIMEOUT = "sync_timeout"
OUTCOME_SOAK_REGRESSED = "soak_regressed"

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
    # The renewed grading token first (forges.GITHUB_TOKEN_FILE_ENV_VAR), then
    # the variables: a fix cycle can run past the hour the unit's token lasts.
    token = forges.read_token(forges.FORGE_GITHUB, env)
    if token:
        return token.strip()
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
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number of seconds, got {raw!r}") from exc
    if not (0.0 <= value <= MAX_SOAK_S):
        # Negative values would skip a phase or soak, and nan/inf/huge floats
        # would build a deadline that never expires.
        raise ValueError(f"{name} must be between 0 and {MAX_SOAK_S:g} seconds, got {value!r}")
    return value


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

    repo = env.get("GITOPS_REPO", "")
    if not repo:
        raise ValueError("GITOPS_RUN_BRANCH is set but GITOPS_REPO is not: the wait needs the repository the run branch lives in")
    slug = repo_slug(repo)
    app = env.get("GITOPS_ARGO_APP") or branch.rsplit("/", 1)[-1] or DEFAULT_APP
    app_ns = env.get("GITOPS_ARGO_NAMESPACE") or DEFAULT_APP_NAMESPACE
    context = env.get("GITOPS_ARGO_CONTEXT", "")
    poll = _seconds(env, "GITOPS_POLL_INTERVAL", DEFAULT_POLL_INTERVAL_S)
    pr_timeout = _seconds(env, "GITOPS_PR_TIMEOUT", DEFAULT_PR_TIMEOUT_S)
    merge_timeout = _seconds(env, "GITOPS_MERGE_TIMEOUT", DEFAULT_MERGE_TIMEOUT_S)
    sync_timeout = _seconds(env, "GITOPS_SYNC_TIMEOUT", DEFAULT_SYNC_TIMEOUT_S)
    soak = _seconds(env, "GITOPS_SOAK_SECONDS", DEFAULT_SOAK_S)
    merge_checks = {n.strip() for n in (env.get("GITOPS_MERGE_CHECK") or DEFAULT_MERGE_CHECK).split(",") if n.strip()}

    def headers() -> dict[str, str]:
        """Built per request: the token file can be renewed mid-cycle."""
        built = dict(GITHUB_HEADERS)
        token = _github_token(env)
        if token:
            built["Authorization"] = f"Bearer {token}"
        return built

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
    rejected: list[int] = []

    def newest_pr(pulls: Any) -> dict[str, Any] | None:
        """The newest PR of this run against the branch, skipping rejected ones."""
        for candidate in pulls or []:
            created = candidate.get("created_at", "")
            if since is not None and created and _parse_github_time(created) < since:
                continue
            if int(candidate.get("number", 0)) in rejected:
                continue
            return candidate
        return None

    def adopt(candidate: dict[str, Any]) -> int:
        number = int(candidate["number"])
        record.update(
            pr_number=number, pr_url=candidate.get("html_url", ""), head_sha=candidate.get("head", {}).get("sha", "")
        )
        _log.info("gitops: PR #%d found against %s", number, branch)
        return number

    pr: dict[str, Any] | None = None
    deadline = clock() + pr_timeout
    while True:
        pr = newest_pr(attempt("listing pull requests", lambda: fetch_json(pulls_url, headers())))
        if pr is not None:
            break
        if not wait(deadline):
            _log.warning("gitops: no PR against %s within %.0fs", branch, pr_timeout)
            return finish(OUTCOME_NO_PR)
    number = adopt(pr)

    def superseded(reason: str) -> bool:
        """Move to a later PR of this run if one exists; else the rejection stands.

        The agent can close a PR the check failed and open another (or open a
        second one before the first is judged); the later one is the answer the
        cluster will receive, so ending the wait at the first one's rejection
        would hand the verifiers a cluster the merge and sync are about to
        change under them. The first is kept on the record as superseded.
        """
        nonlocal number, deadline
        rejected.append(number)
        later = newest_pr(attempt("listing pull requests", lambda: fetch_json(pulls_url, headers())))
        if later is None:
            return False
        record.setdefault("superseded", []).append({"pr_number": number, "reason": reason})
        _log.warning("gitops: PR #%d %s; PR #%d supersedes it", number, reason, int(later["number"]))
        number = adopt(later)
        # The later PR gets the whole merge window, not what the first left of it.
        deadline = clock() + merge_timeout
        return True

    # -- 2. merged, or rejected -----------------------------------------
    merge_sha = ""
    deadline = clock() + merge_timeout
    while True:
        detail = attempt(f"reading PR #{number}", lambda: fetch_json(f"{api}/pulls/{number}", headers()))
        if detail:
            if detail.get("merged_at"):
                merge_sha = detail.get("merge_commit_sha") or ""
                record.update(merged_at=detail["merged_at"], merge_sha=merge_sha)
                break
            if detail.get("state") == "closed":
                if superseded("closed without merge"):
                    continue
                return finish(OUTCOME_PR_REJECTED, reason="closed without merge")
            head_sha = detail.get("head", {}).get("sha", "")
            if head_sha:
                record["head_sha"] = head_sha
                checks = attempt(
                    f"reading check runs for {head_sha[:8]}",
                    lambda: fetch_json(f"{api}/commits/{head_sha}/check-runs", headers()),
                )
                if checks:
                    failed = [
                        c.get("name", "")
                        for c in checks.get("check_runs", [])
                        if c.get("name", "") in merge_checks
                        and c.get("status") == "completed"
                        and c.get("conclusion") in REJECTING_CONCLUSIONS
                    ]
                    if failed:
                        reason = f"check failed: {', '.join(failed)}"
                        _log.warning("gitops: PR #%d rejected by checks %s", number, failed)
                        if superseded(reason):
                            continue
                        return finish(OUTCOME_PR_REJECTED, reason=reason)
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
        head_info = attempt("reading the run branch head", lambda: fetch_json(branch_url, headers()))
        head = (head_info or {}).get("commit", {}).get("sha", "")
        if head:
            record["branch_head"] = head
            if head != merge_sha:
                record["head_moved_after_merge"] = True
        raw = attempt(READ_APPLICATION, lambda: run_kubectl(args))
        status = json.loads(raw).get("status", {}) if raw else {}
        sync = status.get("sync", {})
        health = status.get("health", {})
        if status:
            record.update(
                sync_status=sync.get("status", ""), synced_revision=sync.get("revision", ""), health=health.get("status", "")
            )
        # While the branch endpoint is failing, the merge commit stands in for
        # the head: Argo Synced there is the fix landing, not a stale answer.
        expected = head or merge_sha
        at_head = bool(expected) and sync.get("revision") == expected
        if sync.get("status") == ARGO_SYNCED and at_head and health.get("status") == ARGO_HEALTHY:
            _log.info("gitops: application %s Synced at branch head %s and Healthy", app, expected[:8])
            break
        if not wait(deadline):
            _log.warning(
                "gitops: application %s not Synced+Healthy at head %s (merge %s) within %.0fs",
                app, head[:8], merge_sha[:8], sync_timeout,
            )
            return finish(OUTCOME_SYNC_TIMEOUT)
    if soak <= 0:
        return finish(OUTCOME_MERGED)

    # -- 4. optional soak: still Synced at that revision, and Healthy ---
    # Healthy is a point-in-time reading: a Deployment whose new pods pass
    # readiness once and then crash reports Healthy at the sync and
    # Progressing a minute later. The soak polls through GITOPS_SOAK_SECONDS
    # and ends the wait at the first successful read that is no longer Synced
    # at the revision that settled, or no longer Healthy. The branch head is
    # not re-read: a head that moves during the soak shows up as Argo leaving
    # that revision. A failed read is counted and retried like any other; the
    # soak passes only on a successful read at or after its end, and when the
    # reads keep failing past the end that read is retried for up to
    # GITOPS_SYNC_TIMEOUT more seconds before the wait gives up as
    # ``sync_timeout``.
    settled = expected
    record.update(soak_s=soak, soak_revision=settled)
    soak_started = clock()
    soak_end = soak_started + soak
    give_up = soak_end + sync_timeout
    while True:
        now = clock()
        if now >= give_up:
            _log.warning("gitops: no Argo read of %s at the end of the %.0fs soak", app, soak)
            return finish(OUTCOME_SYNC_TIMEOUT, reason="no Argo read at the end of the soak")
        sleep(min(poll, soak_end - now) if now < soak_end else poll)
        # ``or {}``: an Application with no status is a reading that is not
        # Synced, not a failed read; None is reserved for the failure. A reading
        # whose sync.status or health.status is Unknown is Argo reporting that
        # its own comparison or health check failed (repo fetch or manifest
        # error), not a cluster state change, so _read_soak_status raises
        # ValueError for attempt() to count and retry.
        def _read_soak_status() -> dict[str, Any]:
            doc = json.loads(run_kubectl(args))
            if not isinstance(doc, dict):
                raise ValueError("Argo Application response is not a JSON object")
            raw_st = doc.get("status")
            if raw_st is not None and not isinstance(raw_st, dict):
                raise ValueError("Argo Application status is not a JSON object")
            st: dict[str, Any] = raw_st or {}
            sync_part = st.get("sync") if isinstance(st.get("sync"), dict) else {}
            health_part = st.get("health") if isinstance(st.get("health"), dict) else {}
            if sync_part.get("status") == ARGO_UNKNOWN:
                raise ValueError("Argo Application sync.status is Unknown")
            if health_part.get("status") == ARGO_UNKNOWN:
                raise ValueError("Argo Application health.status is Unknown")
            return st

        status = attempt(READ_APPLICATION, _read_soak_status)
        if status is None:
            continue
        sync = status.get("sync") if isinstance(status.get("sync"), dict) else {}
        health = status.get("health") if isinstance(status.get("health"), dict) else {}
        record.update(
            sync_status=str(sync.get("status") or ""),
            synced_revision=str(sync.get("revision") or ""),
            health=str(health.get("status") or ""),
        )
        if not (sync.get("status") == ARGO_SYNCED and sync.get("revision") == settled and health.get("status") == ARGO_HEALTHY):
            after = round(clock() - soak_started, 1)
            reason = (
                f"{after:g}s into the {soak:g}s soak: sync {record['sync_status'] or 'unknown'} at "
                f"{record['synced_revision'][:SHORT_SHA_LEN] or 'no revision'}, health {record['health'] or 'unknown'}"
            )
            _log.warning("gitops: application %s regressed during the soak: %s", app, reason)
            return finish(OUTCOME_SOAK_REGRESSED, reason=reason)
        if clock() >= soak_end:
            _log.info("gitops: application %s still Synced at %s and Healthy after a %.0fs soak", app, settled[:SHORT_SHA_LEN], soak)
            return finish(OUTCOME_MERGED)
