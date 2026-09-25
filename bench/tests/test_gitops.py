"""Unit tests for the GitOps fix-cycle wait (kube_agents_bench.gitops).

GitHub and kubectl are replaced by scripted fakes; the clock and sleep are
injected so timeouts elapse instantly. One test per outcome the pilot records.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from devops_bench.agents import AgentResult

from kube_agents_bench import gitops

BRANCH = "run/eval-pr1/b-0011"
ENV = {
    "GITOPS_RUN_BRANCH": BRANCH,
    "GITOPS_REPO": "https://github.com/acme/gitops-repo.git",
    "GITOPS_ARGO_CONTEXT": "gke_p_l_c",
    "GITOPS_POLL_INTERVAL": "10",
    "GITOPS_PR_TIMEOUT": "60",
    "GITOPS_MERGE_TIMEOUT": "60",
    "GITOPS_SYNC_TIMEOUT": "60",
    "BENCH_GITHUB_TOKEN": "t0k",
}
MERGE_SHA = "9a6cdbe166c4e216dc1968ac40bc11443cf4ba77"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _pr(number: int = 25, *, merged: bool = False, closed: bool = False, created_at: str = "2026-09-10T21:03:00Z") -> dict[str, Any]:
    return {
        "number": number,
        "created_at": created_at,
        "html_url": f"https://github.com/acme/gitops-repo/pull/{number}",
        "state": "closed" if (merged or closed) else "open",
        "merged_at": "2026-09-09T19:00:00Z" if merged else None,
        "merge_commit_sha": MERGE_SHA if merged else None,
        "head": {"sha": "0e6944c3"},
    }


def _app(sync: str, revision: str, health: str) -> str:
    return json.dumps({"status": {"sync": {"status": sync, "revision": revision}, "health": {"status": health}}})


class FakeGitHub:
    """Answers the three endpoints the wait reads, from per-endpoint scripts."""

    def __init__(
        self, pulls: list[Any], details: list[Any] = (), checks: list[Any] = (), heads: list[str] = (MERGE_SHA,)
    ) -> None:
        self.pulls, self.details, self.checks = list(pulls), list(details), list(checks)
        self.heads = list(heads)
        self.urls: list[str] = []
        self.headers: dict[str, str] = {}

    def __call__(self, url: str, headers: Any) -> Any:
        self.urls.append(url)
        self.headers = dict(headers)
        if "/pulls?" in url:
            return self._next(self.pulls)
        if "/check-runs" in url:
            return self._next(self.checks)
        if "/branches/" in url:
            return {"commit": {"sha": self._next(self.heads)}}
        return self._next(self.details)

    @staticmethod
    def _next(script: list[Any]) -> Any:
        item = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(item, BaseException):
            raise item
        return item


def _run(env: dict[str, str], gh: FakeGitHub, kubectl_script: list[str]) -> tuple[AgentResult, dict[str, Any], list[list[str]]]:
    calls: list[list[str]] = []
    script = list(kubectl_script)

    def run_kubectl(args: list[str]) -> str:
        calls.append(args)
        return script.pop(0) if len(script) > 1 else script[0]

    clock = FakeClock()
    result = AgentResult(output="ack", trajectory=[], errors=[], metadata={})
    record = gitops.await_fix_cycle(result, env=env, fetch_json=gh, run_kubectl=run_kubectl, sleep=clock.sleep, clock=clock)
    assert record is result.metadata["gitops"]
    return result, record, calls


def test_noop_without_run_branch() -> None:
    result = AgentResult(output="ack", trajectory=[], errors=[], metadata={})
    assert gitops.await_fix_cycle(result, env={}, fetch_json=lambda *_: pytest.fail("no HTTP expected")) is None
    assert "gitops" not in result.metadata


def test_a_run_branch_without_a_repository_is_an_error_not_a_default() -> None:
    env = {k: v for k, v in ENV.items() if k != "GITOPS_REPO"}
    result = AgentResult(output="ack", trajectory=[], errors=[], metadata={})
    with pytest.raises(ValueError, match="GITOPS_REPO"):
        gitops.await_fix_cycle(result, env=env, fetch_json=lambda *_: pytest.fail("no HTTP expected"))


def test_merged_then_synced_and_healthy() -> None:
    gh = FakeGitHub(pulls=[[_pr()]], details=[_pr(), _pr(merged=True)], checks=[{"check_runs": [{"status": "in_progress"}]}])
    kubectl = [_app("OutOfSync", "old", "Progressing"), _app("Synced", MERGE_SHA, "Progressing"), _app("Synced", MERGE_SHA, "Healthy")]
    result, record, calls = _run(ENV, gh, kubectl)
    assert record["outcome"] == gitops.OUTCOME_MERGED
    assert record["pr_number"] == 25 and record["merge_sha"] == MERGE_SHA
    assert record["repo"] == "acme/gitops-repo" and record["run_branch"] == BRANCH
    # PR lookup is filtered to the run branch and authenticated.
    assert f"base={BRANCH.replace('/', '%2F')}" in gh.urls[0]
    assert gh.headers["Authorization"] == "Bearer t0k"
    # Argo is read on the task cluster's context, in the Application namespace.
    assert calls[0][:2] == ["--context", "gke_p_l_c"] and "argocd" in calls[0]
    assert record["health"] == "Healthy" and record["synced_revision"] == MERGE_SHA
    # The outcome rides along in the trajectory (results.json keeps that, not metadata).
    (entry,) = [t for t in result.trajectory if t.get("name") == "gitops_fix_cycle"]
    assert entry["result"] is record and entry["result"]["outcome"] == gitops.OUTCOME_MERGED
    assert entry["args"]["run_branch"] == BRANCH


def test_head_moved_after_merge_is_still_merged_when_argo_is_at_head() -> None:
    # Run 7: after the PR merged, a follow-up commit landed directly on the run
    # branch; Argo tracks the branch and reported that head, not the merge SHA.
    later = "49d52a67afe17cc7ad85edec953626b67bbccf3a"
    gh = FakeGitHub(pulls=[[_pr()]], details=[_pr(merged=True)], heads=[MERGE_SHA, later])
    kubectl = [_app("Synced", MERGE_SHA, "Progressing"), _app("Synced", later, "Healthy")]
    _, record, _ = _run(ENV, gh, kubectl)
    assert record["outcome"] == gitops.OUTCOME_MERGED
    assert record["merge_sha"] == MERGE_SHA and record["branch_head"] == later
    assert record["head_moved_after_merge"] is True
    # The branch name is percent-encoded in the branches endpoint.
    assert any("/branches/run%2Feval-pr1%2Fb-0011" in u for u in gh.urls)


def test_no_pr_within_timeout() -> None:
    gh = FakeGitHub(pulls=[[]])
    _, record, calls = _run(ENV, gh, [_app("Synced", "x", "Healthy")])
    assert record["outcome"] == gitops.OUTCOME_NO_PR
    assert "pr_number" not in record and calls == []
    # 60s budget at a 10s poll: bounded, not one-shot.
    assert 2 <= len(gh.urls) <= 8


def test_pr_rejected_by_failed_check() -> None:
    gh = FakeGitHub(
        pulls=[[_pr()]],
        details=[_pr()],
        checks=[{"check_runs": [{"name": "check", "status": "completed", "conclusion": "failure"}]}],
    )
    _, record, calls = _run(ENV, gh, [_app("Synced", "x", "Healthy")])
    assert record["outcome"] == gitops.OUTCOME_PR_REJECTED
    assert "check failed: check" in record["reason"]
    assert calls == [], "a rejected PR must never wait on Argo"


def test_a_failing_check_that_is_not_the_merge_check_does_not_reject() -> None:
    lint = {"name": "lint", "status": "completed", "conclusion": "failure"}
    gh = FakeGitHub(pulls=[[_pr()]], details=[_pr(), _pr(merged=True)], checks=[{"check_runs": [lint]}])
    _, record, _ = _run(ENV, gh, [_app("Synced", MERGE_SHA, "Healthy")])
    assert record["outcome"] == "merged"
    assert "superseded" not in record


def test_action_required_is_not_a_rejection() -> None:
    waiting = {"name": "check", "status": "completed", "conclusion": "action_required"}
    gh = FakeGitHub(pulls=[[_pr()]], details=[_pr(), _pr(merged=True)], checks=[{"check_runs": [waiting]}])
    _, record, _ = _run(ENV, gh, [_app("Synced", MERGE_SHA, "Healthy")])
    assert record["outcome"] == "merged"


def test_the_merge_check_names_come_from_the_environment() -> None:
    green = {"name": "merge-on-green", "status": "completed", "conclusion": "failure"}
    gh = FakeGitHub(pulls=[[_pr()]], details=[_pr()], checks=[{"check_runs": [green]}])
    _, record, _ = _run({**ENV, "GITOPS_MERGE_CHECK": "merge-on-green, docs"}, gh, [_app("Synced", MERGE_SHA, "Healthy")])
    assert record["outcome"] == "pr_rejected"
    assert record["reason"] == "check failed: merge-on-green"


def test_a_rejected_pr_is_superseded_by_a_later_one() -> None:
    first_closed = _pr(25, closed=True)
    second = _pr(26, merged=True, created_at="2026-09-10T21:10:00Z")
    gh = FakeGitHub(pulls=[[_pr(25)], [second, first_closed]], details=[first_closed, second])
    _, record, _ = _run(ENV, gh, [_app("Synced", MERGE_SHA, "Healthy")])
    assert record["outcome"] == "merged"
    assert record["pr_number"] == 26
    assert record["pr_url"].endswith("/pull/26")
    assert record["superseded"] == [{"pr_number": 25, "reason": "closed without merge"}]


def test_a_superseding_pr_gets_the_whole_merge_window() -> None:
    first, first_closed = _pr(25), _pr(25, closed=True)
    second, second_merged = _pr(26, created_at="2026-09-10T21:10:00Z"), _pr(26, merged=True, created_at="2026-09-10T21:10:00Z")
    # 10s polls, 60s merge window: the first PR closes at t=30; the second merges at
    # its fifth poll, t=70, past the first PR's deadline and inside its own.
    gh = FakeGitHub(
        pulls=[[first], [second, first_closed]],
        details=[first, first, first, first_closed, second, second, second, second, second_merged],
        checks=[{"check_runs": [{"status": "queued"}]}],
    )
    _, record, _ = _run(ENV, gh, [_app("Synced", MERGE_SHA, "Healthy")])
    assert record["outcome"] == "merged"
    assert record["pr_number"] == 26


def test_sync_at_the_merge_commit_counts_while_the_branch_endpoint_fails() -> None:
    gh = FakeGitHub(pulls=[[_pr()]], details=[_pr(merged=True)], heads=[OSError("502")])
    _, record, _ = _run(ENV, gh, [_app("Synced", MERGE_SHA, "Healthy")])
    assert record["outcome"] == "merged"
    assert record["poll_errors"] >= 1
    assert "branch_head" not in record


def test_pr_closed_unmerged_is_rejected() -> None:
    gh = FakeGitHub(pulls=[[_pr()]], details=[_pr(closed=True)])
    _, record, _ = _run(ENV, gh, [])
    assert record["outcome"] == gitops.OUTCOME_PR_REJECTED
    assert record["reason"] == "closed without merge"


def test_merge_timeout_is_not_rejection() -> None:
    gh = FakeGitHub(pulls=[[_pr()]], details=[_pr()], checks=[{"check_runs": [{"status": "queued"}]}])
    _, record, _ = _run(ENV, gh, [])
    assert record["outcome"] == gitops.OUTCOME_MERGE_TIMEOUT


def test_sync_timeout_when_argo_never_reaches_merge_sha() -> None:
    gh = FakeGitHub(pulls=[[_pr()]], details=[_pr(merged=True)])
    _, record, _ = _run(ENV, gh, [_app("Synced", "stale", "Healthy")])
    assert record["outcome"] == gitops.OUTCOME_SYNC_TIMEOUT
    assert record["synced_revision"] == "stale"


def test_poll_failure_is_retried_until_the_deadline_not_raised() -> None:
    calls = {"n": 0}

    def boom(url: str, headers: Any) -> Any:
        calls["n"] += 1
        raise OSError("api down")

    clock = FakeClock()
    result = AgentResult(output="ack", trajectory=[], errors=[], metadata={})
    record = gitops.await_fix_cycle(result, env=ENV, fetch_json=boom, run_kubectl=lambda a: "", sleep=clock.sleep, clock=clock)
    assert record is not None and record["outcome"] == gitops.OUTCOME_NO_PR
    assert record["poll_errors"] == calls["n"] >= 2 and "api down" in record["last_error"]
    assert result.errors == [] and result.output == "ack"


def test_transient_failure_mid_wait_does_not_end_it() -> None:
    # First PR listing fails, second succeeds; the wait carries on to a merge.
    gh = FakeGitHub(pulls=[OSError("502"), [_pr()]], details=[_pr(merged=True)])
    _, record, _ = _run(ENV, gh, [_app("Synced", MERGE_SHA, "Healthy")])
    assert record["outcome"] == gitops.OUTCOME_MERGED and record["poll_errors"] == 1


def test_prs_created_before_the_run_are_ignored() -> None:
    # Rerun on the same cluster name: the previous run's merged PR is still
    # listed against the run branch and must not be taken as this run's.
    stale = _pr(24, merged=True, created_at="2026-09-10T20:00:00Z")
    fresh = _pr(26, created_at="2026-09-10T21:30:00Z")
    gh = FakeGitHub(pulls=[[stale], [fresh, stale]], details=[_pr(26, merged=True)])
    clock = FakeClock()
    result = AgentResult(output="ack", trajectory=[], errors=[], metadata={})
    since = gitops._parse_github_time("2026-09-10T21:00:00Z")
    kubectl = [_app("Synced", MERGE_SHA, "Healthy")]
    record = gitops.await_fix_cycle(
        result, since=since, env=ENV, fetch_json=gh, run_kubectl=lambda a: kubectl[0], sleep=clock.sleep, clock=clock
    )
    assert record is not None and record["pr_number"] == 26 and record["outcome"] == gitops.OUTCOME_MERGED


def test_token_file_fallback(tmp_path: Any) -> None:
    token_file = tmp_path / "tok"
    token_file.write_text("filetoken\n")
    env = {k: v for k, v in ENV.items() if k != "BENCH_GITHUB_TOKEN"} | {"GITOPS_TOKEN_FILE": str(token_file)}
    gh = FakeGitHub(pulls=[[]])
    _run(env, gh, [])
    assert gh.headers["Authorization"] == "Bearer filetoken"


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("https://github.com/o/r", "o/r"),
        ("https://github.com/o/r.git", "o/r"),
        ("https://github.com/o/r/", "o/r"),
        ("git@github.com:o/r.git", "o/r"),
    ],
)
def test_repo_slug(url: str, slug: str) -> None:
    assert gitops.repo_slug(url) == slug
