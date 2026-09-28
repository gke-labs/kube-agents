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

"""``pull_request_diff_contains`` over the pull requests the measurement run left.

The fixtures under ``fixtures/github/`` are pull requests #39 and #38 of
``gke-agentic/kube-agents-evals-21-infra`` as GitHub served them on
2026-09-28, trimmed to the fields the check reads: #39 is the pull request
repetition 1 of ``obtainability-remediation-proposal`` opened through the
inject door on 2026-09-25 (build 2103530656385470464), #38 the earlier
lease's pull request that repetitions 2 and 3 pushed onto and pointed at.
The three recorded replies name them and nothing of the manifest's
selector; the diffs carry it. Every GET goes through the faked transport.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import tomllib
import yaml
from devops_bench.verification.base import VERIFIERS
from devops_bench.verification.runner import VerifierAgent
from devops_bench.verification.spec import VerificationEntry, parse_node
from kube_agents_bench import transcript, verifiers
from kube_agents_bench.verifiers import PullRequestDiffContainsVerifier
from pydantic import ValidationError

FIXTURES = Path(__file__).parent / "fixtures" / "github"
REPO_ROOT = Path(__file__).resolve().parents[2]
REPO = "gke-agentic/kube-agents-evals-21-infra"
API = f"https://api.github.com/repos/{REPO}"
PR39 = f"https://github.com/{REPO}/pull/39"
PR38 = f"https://github.com/{REPO}/pull/38"
# Repetition 1's reply, as recorded: the kind and the budget, no selector.
REPLY_39 = (
    "I have automatically proposed a concrete declarative remediation manifest (a "
    "`PodDisruptionBudget` with `minAvailable: 1`) to ensure at least one instance of the "
    "deployment remains active during voluntary disruptions.\n\nPlease review and merge the "
    f"proposed GitOps Pull Request here:\n[**PR #39 — feat: Add PodDisruptionBudget for checkout-gateway**]({PR39})\n"
)
PHRASES = {"required_phrases": ["PodDisruptionBudget", "selector"], "any_of_phrases": ["minAvailable", "maxUnavailable"]}


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("BENCH_GITHUB_TOKEN", "ghs_fake")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


@pytest.fixture
def github(monkeypatch):
    calls: list[str] = []
    routes: dict[str, object] = {}

    def fake_get(url: str, tok: str, timeout: float):
        calls.append(url)
        route = routes.get(url, (404, {"message": "Not Found"}))
        if callable(route):
            return route()
        return route

    monkeypatch.setattr(verifiers, "_http_get_json", fake_get)
    return type("GH", (), {"routes": routes, "calls": calls})()


def route_pull(github, number: int) -> None:
    github.routes[f"{API}/pulls/{number}"] = (200, fixture(f"pull-{number}.json"))
    github.routes[f"{API}/pulls/{number}/files?per_page=100&page=1"] = (200, fixture(f"pull-{number}-files.json"))


def stash(final_message: str = REPLY_39, output: str | None = None) -> None:
    transcript.set(output if output is not None else final_message, [], final_message=final_message, started_at=1.0)


def check(**kw) -> PullRequestDiffContainsVerifier:
    kw.setdefault("owner", "gke-agentic")
    for key, value in PHRASES.items():
        kw.setdefault(key, value)
    return PullRequestDiffContainsVerifier(type="pull_request_diff_contains", **kw)


def case_entry(name: str = "report-proposes-pdb-manifest") -> VerificationEntry:
    """The case's own entry, read from the task file the presubmit runs."""
    doc = yaml.safe_load((REPO_ROOT / "bench" / "tasks" / "obtainability-remediation-proposal" / "task.yaml").read_text())
    return VerificationEntry.model_validate(next(e for e in doc["verification_spec"] if e["name"] == name))


# --- registration and shape ----------------------------------------------------


def test_the_verifier_is_published_as_an_entry_point():
    with (REPO_ROOT / "bench" / "pyproject.toml").open("rb") as fh:
        eps = tomllib.load(fh)["project"]["entry-points"]["devops_bench.verifiers"]
    assert eps["pull_request_diff_contains"] == "kube_agents_bench.verifiers:PullRequestDiffContainsVerifier"
    assert VERIFIERS.get("pull_request_diff_contains") is PullRequestDiffContainsVerifier
    assert isinstance(parse_node({"type": "pull_request_diff_contains", "required_phrases": ["x"]}), PullRequestDiffContainsVerifier)


def test_a_check_that_asserts_nothing_is_refused_at_load():
    with pytest.raises(ValidationError, match="asserts nothing"):
        PullRequestDiffContainsVerifier(type="pull_request_diff_contains", forbidden_phrases=["x"])
    with pytest.raises(ValidationError):
        PullRequestDiffContainsVerifier(type="pull_request_diff_contains")


# --- the measurement run's three records -------------------------------------


def test_the_diff_of_the_pull_request_the_reply_names_carries_the_manifest(token, github):
    stash()
    route_pull(github, 39)
    res = check().verify(5.0)
    assert res.status == "pass", res.reason
    assert res.raw["pull_request"] == f"{REPO}#39"
    assert github.calls == [f"{API}/pulls/39", f"{API}/pulls/39/files?per_page=100&page=1"]


def test_an_earlier_leases_pull_request_the_run_pointed_at_passes_too(token, github):
    """Repetitions 2 and 3 named #38, opened by an earlier lease and pushed onto
    by repetition 2; repetition 3 found the branch already carrying the manifest.
    The objective grades the proposal, not who opened it."""
    stash(f"The Pull Request has been opened/updated here: [{PR38}]({PR38}).")
    route_pull(github, 38)
    res = check().verify(5.0)
    assert res.status == "pass", res.reason
    assert res.raw["pull_request"] == f"{REPO}#38"


def test_the_cases_entry_is_red_on_the_reply_alone_and_green_through_the_diff(token, github):
    """The red-then-green the case's own entry owes: the reply of repetition 1
    fails the inline arm (no selector), and the entry passes through the
    third arm once the diff is read."""
    stash()
    route_pull(github, 39)
    entry = case_entry()
    inline = next(c for c in entry.check.checks if getattr(c, "type", "") == "report_contains" and c.scope == "final")
    assert inline.verify(5.0).status == "fail"
    assert VerifierAgent().run_entry(entry, timeout_sec=10.0).status == "pass"


def test_an_inline_manifest_still_passes_the_entry_without_a_credential(github, monkeypatch):
    """The api lane's shape: the Planning Agent inlines the manifest and names
    no pull request, so the first arm passes and the third never asks for a
    token."""
    monkeypatch.delenv("BENCH_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    stash("apiVersion: policy/v1\nkind: PodDisruptionBudget\nspec:\n  minAvailable: 1\n  selector:\n    matchLabels:\n      app: checkout-gateway\n")
    assert VerifierAgent().run_entry(case_entry(), timeout_sec=10.0).status == "pass"
    assert github.calls == []


def test_a_manifest_quoted_earlier_in_the_transcript_passes_the_second_arm(github):
    stash(
        final_message="Done; see the manifest above.",
        output="kind: PodDisruptionBudget ... selector: ... minAvailable: 1\n\nDone; see the manifest above.",
    )
    assert VerifierAgent().run_entry(case_entry(), timeout_sec=10.0).status == "pass"
    assert github.calls == []


# --- what is rejected, and what is an error -----------------------------------


def test_a_diff_without_the_manifest_is_a_fail(token, github):
    stash()
    pull = fixture("pull-39.json")
    github.routes[f"{API}/pulls/39"] = (200, pull)
    github.routes[f"{API}/pulls/39/files?per_page=100&page=1"] = (
        200,
        [{"filename": "seeded-reliability/checkout-gateway.yaml", "patch": "@@ -1 +1 @@\n-replicas: 2\n+replicas: 3"}],
    )
    res = check().verify(5.0)
    assert res.status == "fail"
    assert "required phrases absent from its diff: ['PodDisruptionBudget', 'selector']" in res.reason


def test_removed_and_context_lines_do_not_count(token, github):
    """A pull request that deletes the budget, or edits a line beside one,
    carries the nouns in its `-` and context lines; only added lines are the
    proposal."""
    stash()
    github.routes[f"{API}/pulls/39"] = (200, fixture("pull-39.json"))
    added = fixture("pull-39-files.json")[0]["patch"]
    removed = added.replace("\n+", "\n-").replace("@@ -0,0 +1,10 @@", "@@ -1,10 +0,0 @@")
    github.routes[f"{API}/pulls/39/files?per_page=100&page=1"] = (
        200,
        [{"filename": "seeded-reliability/checkout-gateway-pdb.yaml", "patch": removed}],
    )
    assert check().verify(5.0).status == "fail"
    context = "\n".join(" " + line[1:] if line.startswith("+") else line for line in added.splitlines())
    context += "\n+  # a comment beside the budget"
    github.routes[f"{API}/pulls/39/files?per_page=100&page=1"] = (
        200,
        [{"filename": "seeded-reliability/checkout-gateway-pdb.yaml", "patch": context}],
    )
    assert check().verify(5.0).status == "fail"


def test_a_listing_of_full_pages_is_graded_on_what_was_read_and_says_so(token, github):
    stash()
    github.routes[f"{API}/pulls/39"] = (200, fixture("pull-39.json"))
    page = fixture("pull-39-files.json") * 100  # 100 files, all carrying the manifest
    for n in (1, 2, 3):
        github.routes[f"{API}/pulls/39/files?per_page=100&page={n}"] = (200, page)
    res = check().verify(5.0)
    assert res.status == "pass"
    assert res.raw["notes"] == [f"{REPO}#39 changes at least 300 files; graded on the first pages"]
    assert not [c for c in github.calls if "page=4" in c]


def test_no_token_names_the_permission_this_check_needs(github, monkeypatch):
    monkeypatch.delenv("BENCH_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    stash()
    res = check().verify(5.0)
    assert res.status == "error"
    assert "pull_requests: read" in res.reason


def test_a_pull_request_closed_without_merging_is_a_fail(token, github):
    stash()
    pull = fixture("pull-39.json")
    pull.update(state="closed", merged_at=None)
    github.routes[f"{API}/pulls/39"] = (200, pull)
    res = check().verify(5.0)
    assert res.status == "fail"
    assert "closed without being merged" in res.reason


def test_a_merged_pull_request_is_a_quote_not_a_proposal(token, github):
    """The skill's "already exists" lands on open pull requests only, so a
    merged one in a reply was not this proposal's vehicle; accepting it
    would make any merged manifest in the organisation a standing pass."""
    stash()
    pull = fixture("pull-39.json")
    pull.update(state="closed", merged_at="2026-09-26T00:00:00Z")
    github.routes[f"{API}/pulls/39"] = (200, pull)
    github.routes[f"{API}/pulls/39/files?per_page=100&page=1"] = (200, fixture("pull-39-files.json"))
    res = check().verify(5.0)
    assert res.status == "fail"
    assert "#39: merged, so a reply naming it quotes a pull request" in res.reason
    assert not [c for c in github.calls if "/files" in c]


def test_a_pull_request_outside_the_runs_repository_is_rejected_when_the_run_names_one(token, github, monkeypatch):
    """A run in one pool project naming another project's pull request: bound
    to the repository the run writes to when the script exports it."""
    stash()
    route_pull(github, 39)
    monkeypatch.setenv("BENCH_GITOPS_REPO", "gke-agentic/kube-agents-evals-22-infra")
    res = check().verify(5.0)
    assert res.status == "fail"
    assert "not in gke-agentic/kube-agents-evals-22-infra, the repository this run writes to" in res.reason
    assert github.calls == []
    monkeypatch.setenv("BENCH_GITOPS_REPO", "GKE-Agentic/kube-agents-evals-21-infra")
    assert check().verify(5.0).status == "pass"
    monkeypatch.delenv("BENCH_GITOPS_REPO")
    assert check().verify(5.0).status == "pass"


def test_a_human_pull_request_or_a_fork_is_not_the_agents_proposal(token, github):
    stash()
    pull = fixture("pull-39.json")
    pull["head"]["ref"] = "fix/checkout-gateway-pdb"
    github.routes[f"{API}/pulls/39"] = (200, pull)
    res = check().verify(5.0)
    assert res.status == "fail"
    assert "is not an agent branch (platform-agent/* in the repository itself)" in res.reason
    pull = fixture("pull-39.json")
    pull["head"]["repo"] = {"full_name": "someone/kube-agents-evals-21-infra"}
    github.routes[f"{API}/pulls/39"] = (200, pull)
    assert check().verify(5.0).status == "fail"


def test_the_branch_prefix_is_forge_pys():
    forge = (REPO_ROOT / "agents" / "platform" / "scripts" / "forge.py").read_text()
    assert f'AGENT_BRANCH_PREFIX = "{verifiers._AGENT_BRANCH_PREFIX}"' in forge


def test_a_reply_naming_no_pull_request_fails_before_any_credential_is_needed(github, monkeypatch):
    monkeypatch.delenv("BENCH_GITHUB_TOKEN", raising=False)
    stash("I recommend a PodDisruptionBudget.")
    res = check().verify(5.0)
    assert res.status == "fail"
    assert "names no github.com pull request URL" in res.reason
    assert github.calls == []


def test_a_missing_pull_request_is_rejected_not_errored(token, github):
    stash()
    res = check().verify(5.0)  # the default route answers 404
    assert res.status == "fail"
    assert "no such pull request (404)" in res.reason


def test_a_pull_request_outside_the_pinned_owner_is_rejected(token, github):
    stash(f"See https://github.com/someone/{REPO.split('/')[1]}/pull/39")
    res = check().verify(5.0)
    assert res.status == "fail"
    assert "not under gke-agentic" in res.reason


@pytest.mark.parametrize("status, needle", [(401, "is not valid"), (403, "needs `pull_requests: read`"), (500, "unexpected GitHub response 500")])
def test_an_unreadable_pull_request_is_an_error_when_nothing_else_passes(token, github, status, needle):
    stash()
    github.routes[f"{API}/pulls/39"] = (status, {"message": "x"})
    res = check().verify(5.0)
    assert res.status == "error"
    assert needle in res.reason


def test_an_unreadable_candidate_beside_a_passing_one_does_not_error(token, github):
    stash(f"Opened {PR38} after {PR39} was denied.")
    github.routes[f"{API}/pulls/38"] = (403, {"message": "denied"})
    route_pull(github, 39)
    assert check().verify(5.0).status == "pass"


def test_an_unreachable_api_is_an_error(token, github):
    stash()

    def boom():
        raise OSError("connection reset")

    github.routes[f"{API}/pulls/39"] = boom
    assert check().verify(5.0).status == "error"


def test_a_file_without_a_patch_is_noted_and_the_rest_is_graded(token, github):
    stash()
    github.routes[f"{API}/pulls/39"] = (200, fixture("pull-39.json"))
    files = fixture("pull-39-files.json") + [{"filename": "logo.png", "status": "added"}]
    github.routes[f"{API}/pulls/39/files?per_page=100&page=1"] = (200, files)
    res = check().verify(5.0)
    assert res.status == "pass"
    assert res.raw["notes"] == ["logo.png: no patch served (binary or too large)"]


def test_forbidden_phrases_reject_a_diff_that_carries_them(token, github):
    stash()
    route_pull(github, 39)
    res = check(forbidden_phrases=["maxUnavailable: 0"]).verify(5.0)
    assert res.status == "pass"
    res = check(forbidden_phrases=["minAvailable: 1"]).verify(5.0)
    assert res.status == "fail"
    assert "forbidden phrases in its diff" in res.reason


def test_no_transcript_is_an_error(token, github):
    transcript.clear()
    assert check().verify(5.0).status == "error"
