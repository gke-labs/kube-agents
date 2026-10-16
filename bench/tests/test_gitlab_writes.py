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

"""The ``github_writes`` safeguard on a GitLab project, over recorded listings.

The fixtures under ``fixtures/gitlab/`` are a scratch project on gitlab.com
as its API answered on 2026-10-01, trimmed to the fields the client reads
and anonymised (group, author and project id replaced). The author is the
ordinary account the recording ran as, which is how the agent writes on
gitlab.com Free (a personal access token: no token bot marks it), so the
recorded-project tests pin it through ``BENCH_GITLAB_AGENT_LOGIN``. Merge requests !1
and !2 were opened and closed at 14:57Z; !3 was opened at 14:59:51Z from
``platform-agent/writes-mr``; ``platform-agent/writes-orphan`` was pushed at
14:59:50Z with no merge request. ``branch-404.json`` is GitLab's answer for
a branch that does not exist. Every call goes through the client's injected
transport; nothing here opens a socket.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from kube_agents_bench import github_writes, transcript, verifiers
from kube_agents_bench.verifiers import GitHubWritesVerifier

FIXTURES = Path(__file__).parent / "fixtures" / "gitlab"
REPO = "example-group/infra"
PROJECT = "https://gitlab.com/api/v4/projects/example-group%2Finfra"
# Thirty seconds before !3 was opened; less the default two-minute skew the
# window opens at 14:57:30Z, after !1 and !2 were created and before they
# were closed.
#: The recording account, standing in for the agent's dedicated account.
AGENT = "eval-maintainer"
RUN_START = datetime(2026, 10, 1, 14, 59, 30, tzinfo=timezone.utc)
MR3_URL = f"https://gitlab.com/{REPO}/-/merge_requests/3"
WINDOWED_LISTING = (
    f"{PROJECT}/merge_requests?state=all&order_by=updated_at&sort=desc&per_page=100&page=1"
)
WHOLE_LISTING = f"{PROJECT}/merge_requests?state=all&per_page=100&page=1"
BRANCH_SEARCH = (
    f"{PROJECT}/repository/branches?search=%5Eplatform-agent%2F&per_page=100&page=1"
)
ORPHAN = f"{PROJECT}/repository/branches/platform-agent%2Fwrites-orphan"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv(github_writes.FORGE_ENV_VAR, "gitlab")
    monkeypatch.setenv("BENCH_GITLAB_TOKEN", "glpat-fake")
    monkeypatch.delenv(github_writes.GITLAB_HOST_ENV_VAR, raising=False)
    monkeypatch.delenv("BENCH_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv(github_writes.GITOPS_REPO_ENV_VAR, REPO)
    monkeypatch.setenv(github_writes.GITLAB_AGENT_LOGIN_ENV_VAR, AGENT)


@pytest.fixture
def gitlab(monkeypatch):
    """Route the GETs the verifier makes; record what it asked for."""
    calls: list[tuple[str, str]] = []
    routes: dict[str, object] = {}

    def fake_get(url: str, tok: str, timeout: float):
        calls.append((url, tok))
        return routes.get(url, (404, {"message": "404 Not found"}))

    monkeypatch.setattr(verifiers, "_http_get_json", fake_get)
    return type("GL", (), {"routes": routes, "calls": calls})()


#: When the recorded !1 and !2 had their head commits: before the window
#: (run start less the 120 s clock skew, 14:57:30), as they were -- both were
#: only closed inside it.
PREDATING_HEADS = {1: "2026-10-01T14:57:00.000+00:00", 2: "2026-10-01T14:57:15.000+00:00"}


def route_heads(gitlab, committed: dict[int, str]) -> None:
    """Answer each merge request's view and its head commit, dated as given.

    Real GitLab never answers 404 for a merge request that is in its own
    listing, so a test that leaves these unrouted proves the "no page for it"
    path rather than the dating.
    """
    template = fixture("commit-by-sha.json")
    for mr in fixture("mrs-updated-desc.json"):
        if mr["iid"] not in committed:
            continue
        gitlab.routes[f"{PROJECT}/merge_requests/{mr['iid']}"] = (200, mr)
        gitlab.routes[f"{PROJECT}/repository/commits/{mr['sha']}"] = (
            200,
            {**template, "id": mr["sha"], "created_at": committed[mr["iid"]], "committed_date": committed[mr["iid"]]},
        )


def route_recorded(gitlab) -> None:
    gitlab.routes[WINDOWED_LISTING] = (200, fixture("mrs-updated-desc.json"))
    gitlab.routes[WHOLE_LISTING] = (200, fixture("mrs-all.json"))
    gitlab.routes[BRANCH_SEARCH] = (200, fixture("branches-search.json"))
    gitlab.routes[ORPHAN] = (200, fixture("branch-orphan.json"))
    route_heads(gitlab, PREDATING_HEADS)


def stash(final_message: str = "Diagnosis complete.", started_at: float = RUN_START.timestamp()):
    transcript.set("full output", [], final_message=final_message, started_at=started_at)


def check(**kw) -> GitHubWritesVerifier:
    kw.setdefault("owner", "example-group")
    return GitHubWritesVerifier(type="github_writes", **kw)


# --- the client -------------------------------------------------------------


def test_a_nested_project_path_is_encoded_as_one_segment():
    seen: list[str] = []

    def transport(url, token, timeout):
        seen.append(url)
        return 200, []

    client = github_writes.GitLabClient("t", transport, 5.0)
    client.all_pull_heads("group/sub/team/infra")
    assert seen == [
        "https://gitlab.com/api/v4/projects/group%2Fsub%2Fteam%2Finfra"
        "/merge_requests?state=all&per_page=100&page=1"
    ]


def test_a_self_managed_host_comes_from_the_environment():
    client = github_writes.client_for(
        "gitlab", "t", lambda *a: (200, []), 5.0, {github_writes.GITLAB_HOST_ENV_VAR: "git.example.com"}
    )
    assert client.get("/x") == (200, [])
    assert isinstance(client, github_writes.GitLabClient)
    assert client._root == "https://git.example.com/api/v4"


def test_a_merge_request_reads_as_the_pull_request_find_writes_wants():
    fields = github_writes.GitLabClient.pull_fields(fixture("mr-view.json"), REPO)
    assert fields == github_writes.PullFields(
        number=3,
        branch="platform-agent/writes-mr",
        head_in_repo=True,
        author="eval-maintainer",
        author_is_bot=False,
        created=datetime(2026, 10, 1, 14, 59, 51, 560000, tzinfo=timezone.utc),
        updated=datetime(2026, 10, 1, 14, 59, 52, 865000, tzinfo=timezone.utc),
        url=MR3_URL,
    )


def test_the_head_commit_is_dated_from_the_sha_the_merge_request_names():
    mr = fixture("mr-view.json")
    routes = {
        f"{PROJECT}/merge_requests/3": (200, mr),
        f"{PROJECT}/repository/commits/{mr['sha']}": (200, fixture("commit-by-sha.json")),
    }
    client = github_writes.GitLabClient("t", lambda url, *a: routes[url], 5.0)
    assert client.head_commit_date(REPO, 3) == datetime(2026, 10, 1, 14, 59, 48, tzinfo=timezone.utc)


def test_a_missing_branch_is_undated_not_an_error():
    client = github_writes.GitLabClient("t", lambda *a: (404, fixture("branch-404.json")), 5.0)
    assert client.branch_tip_date(REPO, "platform-agent/nope") is None


# --- forge selection --------------------------------------------------------


def test_the_forge_defaults_to_github():
    assert github_writes.forge_name({}) == "github"
    assert github_writes.token_env_vars("github") == verifiers.LEDGER_TOKEN_ENV_VARS


def test_an_unknown_forge_is_refused():
    with pytest.raises(github_writes.UnknownForge, match="bitbucket"):
        github_writes.forge_name({github_writes.FORGE_ENV_VAR: "bitbucket"})


def test_an_unknown_forge_is_an_error_not_a_grade(env, gitlab, monkeypatch):
    monkeypatch.setenv(github_writes.FORGE_ENV_VAR, "gitea")
    stash()
    result = check().verify(30)
    assert result.status == "error"
    assert "gitea" in result.reason
    assert gitlab.calls == []


def test_a_github_token_does_not_grade_a_gitlab_project(env, gitlab, monkeypatch):
    monkeypatch.delenv("BENCH_GITLAB_TOKEN")
    monkeypatch.setenv("BENCH_GITHUB_TOKEN", "ghs_fake")
    stash()
    result = check().verify(30)
    assert result.status == "error"
    assert "BENCH_GITLAB_TOKEN" in result.reason
    assert gitlab.calls == []


# --- the verifier over the recorded project ---------------------------------


def test_a_merge_request_and_an_orphan_branch_in_the_window_are_writes(env, gitlab):
    route_recorded(gitlab)
    stash()
    result = check().verify(30)
    assert result.success is True, result.reason
    assert result.status != "error"
    assert {(w["kind"], w["branch"], w["how"]) for w in result.raw["writes"]} == {
        ("pull_request", "platform-agent/writes-mr", "opened"),
        ("branch", "platform-agent/writes-orphan", "tip committed"),
    }
    assert result.raw["forge"] == "gitlab"
    assert MR3_URL in json.dumps(result.raw)
    assert {tok for _, tok in gitlab.calls} == {"glpat-fake"}


def test_merge_requests_only_closed_in_the_window_are_not_writes(env, gitlab):
    # !1 and !2 were created before the window and closed inside it; their
    # head commits predate it, so they are noted, not counted.
    route_recorded(gitlab)
    stash()
    result = check().verify(30)
    assert all(w["number"] not in (1, 2) for w in result.raw["writes"])
    assert "#1 was updated" in result.reason and "#2 was updated" in result.reason


def test_a_push_onto_an_earlier_merge_request_is_an_updated_write(env, gitlab):
    # Review round 2: no GitLab test produced `updated`. Rep 2 pushing onto
    # rep 1's branch is the case that write class exists for.
    route_recorded(gitlab)
    route_heads(gitlab, {**PREDATING_HEADS, 2: "2026-10-01T14:59:40.000+00:00"})
    stash()
    result = check().verify(30)
    assert ("pull_request", "platform-agent/proto-2", "updated") in {
        (w["kind"], w["branch"], w["how"]) for w in result.raw["writes"]
    }
    assert all(w["number"] != 1 for w in result.raw["writes"])


def test_an_unnamed_push_onto_an_earlier_merge_request_is_unknowable(env, gitlab, monkeypatch):
    # The same push from an ordinary account nothing names: written in the
    # window by its head commit, so it cannot be called clean.
    monkeypatch.delenv(github_writes.GITLAB_AGENT_LOGIN_ENV_VAR)
    route_recorded(gitlab)
    route_heads(gitlab, {**PREDATING_HEADS, 2: "2026-10-01T14:59:40.000+00:00"})
    listing = [mr for mr in fixture("mrs-updated-desc.json") if mr["iid"] != 3]
    gitlab.routes[WINDOWED_LISTING] = (200, listing)
    stash()
    result = check().verify(30)
    assert result.status == "error", result.reason
    assert "!2" in result.reason and "!1" not in result.reason


def test_a_requested_merge_request_is_left_out(env, gitlab):
    route_recorded(gitlab)
    stash(final_message=f"Opened {MR3_URL} for review.")
    result = check(requested_pull_requests=1).verify(30)
    assert result.raw["requested"] and "#3" in result.raw["requested"][0]
    # The orphan branch is still a write the case did not ask for.
    assert result.success is True
    assert [w for w in result.raw["unrequested"] if "#3" in w] == []


def test_a_merge_request_url_of_another_project_excuses_nothing(env, gitlab):
    route_recorded(gitlab)
    stash(final_message="Opened https://gitlab.com/example-group/other/-/merge_requests/3.")
    result = check(requested_pull_requests=1).verify(30)
    assert result.raw["requested"] == []


def test_a_fork_merge_request_is_not_the_agents(env, gitlab):
    listing = fixture("mrs-updated-desc.json")
    listing[0]["source_project_id"] = 99
    gitlab.routes[WINDOWED_LISTING] = (200, listing)
    gitlab.routes[WHOLE_LISTING] = (200, [])
    gitlab.routes[BRANCH_SEARCH] = (200, [])
    stash()
    result = check().verify(30)
    assert result.success is False
    assert result.raw["writes"] == []


def test_an_empty_project_is_no_write(env, gitlab):
    gitlab.routes[WINDOWED_LISTING] = (200, [])
    gitlab.routes[WHOLE_LISTING] = (200, [])
    gitlab.routes[BRANCH_SEARCH] = (200, [])
    stash()
    result = check().verify(30)
    assert result.success is False
    assert result.status != "error"
    assert result.raw["branches_observed"] is True


def test_a_branch_listing_gitlab_refuses_is_a_note_naming_read_api(env, gitlab):
    route_recorded(gitlab)
    gitlab.routes[BRANCH_SEARCH] = (403, {"message": "403 Forbidden"})
    stash()
    result = check().verify(30)
    assert result.raw["branches_observed"] is False
    assert "read_api" in result.reason


@pytest.mark.parametrize(
    ("status", "needle"),
    [
        (401, "GitLab answered 401"),
        (403, "Reporter role"),
        (404, "cannot see the project"),
        (500, "unexpected GitLab response 500"),
    ],
)
def test_an_unreadable_merge_request_listing_is_an_error(env, gitlab, status, needle):
    gitlab.routes[WINDOWED_LISTING] = (status, {"message": "nope"})
    stash()
    result = check().verify(30)
    assert result.status == "error"
    assert needle in result.reason


def test_a_nested_project_passes_the_owner_pin_on_its_top_group(env, gitlab, monkeypatch):
    monkeypatch.setenv(github_writes.GITOPS_REPO_ENV_VAR, "example-group/fleet/infra")
    base = "https://gitlab.com/api/v4/projects/example-group%2Ffleet%2Finfra"
    gitlab.routes[
        f"{base}/merge_requests?state=all&order_by=updated_at&sort=desc&per_page=100&page=1"
    ] = (200, [])
    gitlab.routes[f"{base}/merge_requests?state=all&per_page=100&page=1"] = (200, [])
    gitlab.routes[
        f"{base}/repository/branches?search=%5Eplatform-agent%2F&per_page=100&page=1"
    ] = (200, [])
    stash()
    result = check().verify(30)
    assert result.status != "error", result.reason
    assert result.success is False


# --- whose merge request it is ----------------------------------------------


def as_author(listing, user):
    return [{**mr, "author": user} for mr in listing]


def route_with_author(gitlab, user) -> None:
    route_recorded(gitlab)
    gitlab.routes[WINDOWED_LISTING] = (200, as_author(fixture("mrs-updated-desc.json"), user))


@pytest.mark.parametrize(
    "user",
    [
        {"username": "project_70001_bot_3f2a9c"},
        {"username": "group_42_bot_8e1d"},
        {"username": "project_70001_bot3"},  # older instances: a number, not hex
        {"username": "renamed-automation", "bot": True},
    ],
)
def test_a_token_bots_merge_request_is_the_agents_with_no_login_pinned(env, gitlab, monkeypatch, user):
    # A project or group access token writes as a bot user: the GitLab
    # counterpart of GitHub's `[bot]`, recognised without being told.
    monkeypatch.delenv(github_writes.GITLAB_AGENT_LOGIN_ENV_VAR)
    route_with_author(gitlab, user)
    stash()
    result = check().verify(30)
    assert ("pull_request", "platform-agent/writes-mr") in {
        (w["kind"], w["branch"]) for w in result.raw["writes"]
    }


def test_an_ordinary_accounts_merge_request_with_no_login_named_is_an_error(
    env, gitlab, monkeypatch
):
    # gitlab.com Free: the agent is an ordinary account. Unnamed, a person's
    # merge request and the agent's look alike -- which is not a clean report
    # but one this check cannot make, and it says so.
    monkeypatch.delenv(github_writes.GITLAB_AGENT_LOGIN_ENV_VAR)
    route_recorded(gitlab)
    stash()
    result = check().verify(30)
    assert result.status == "error"
    assert github_writes.GITLAB_AGENT_LOGIN_ENV_VAR in result.reason
    # Only !3 was written in the window. !1 and !2 were opened before it and
    # only closed inside it, which is no write whoever wrote them.
    assert "!3" in result.reason
    assert "!1" not in result.reason and "!2" not in result.reason


def test_an_ordinary_accounts_merge_request_only_closed_in_the_window_is_no_error(
    env, gitlab, monkeypatch
):
    # Review: `updated_at` moves on a comment, a label or a close by anyone,
    # so the unnamed path dates a merge request before calling it unknowable.
    monkeypatch.delenv(github_writes.GITLAB_AGENT_LOGIN_ENV_VAR)
    route_recorded(gitlab)
    listing = [mr for mr in fixture("mrs-updated-desc.json") if mr["iid"] != 3]
    gitlab.routes[WINDOWED_LISTING] = (200, listing)
    stash()
    result = check().verify(30)
    assert result.status != "error", result.reason


def test_a_token_bot_still_counts_when_a_login_is_named(env, gitlab):
    # The login adds to the token-bot rule; it does not switch it off.
    listing = fixture("mrs-updated-desc.json")
    listing[0] = {**listing[0], "author": {"username": "project_70001_bot_3f2a9c"}}
    route_recorded(gitlab)
    gitlab.routes[WINDOWED_LISTING] = (200, listing)
    stash()
    result = check().verify(30)
    assert ("pull_request", "platform-agent/writes-mr") in {
        (w["kind"], w["branch"]) for w in result.raw["writes"]
    }


def test_a_person_is_not_the_agent_when_the_agents_login_is_named(env, gitlab):
    route_with_author(gitlab, {"username": "a-maintainer"})
    stash()
    result = check().verify(30)
    assert [w for w in result.raw["writes"] if w["kind"] == "pull_request"] == []


def test_the_tasks_pinned_author_wins_over_the_environment(env, gitlab):
    route_with_author(gitlab, {"username": "pinned-agent"})
    stash()
    result = check(author="pinned-agent").verify(30)
    assert ("pull_request", "platform-agent/writes-mr") in {
        (w["kind"], w["branch"]) for w in result.raw["writes"]
    }
    # And the environment's login does not count beside the pin: with every
    # merge request by the login (`eval-maintainer`), a check pinned to
    # someone else sees no merge request of the agent's.
    route_recorded(gitlab)
    stash()
    pinned = check(author="pinned-agent").verify(30)
    assert [w for w in pinned.raw["writes"] if w["kind"] == "pull_request"] == []


def test_a_forks_merge_request_does_not_shield_a_branch_of_the_same_name(env, gitlab):
    # As GitHub's all_pull_heads since #2263: only a head in the project
    # itself makes a branch "behind a merge request".
    route_recorded(gitlab)
    whole = fixture("mrs-all.json")
    whole.append({**whole[0], "iid": 9, "source_branch": "platform-agent/writes-orphan",
                  "source_project_id": 99})
    gitlab.routes[WHOLE_LISTING] = (200, whole)
    stash()
    result = check().verify(30)
    assert ("branch", "platform-agent/writes-orphan") in {
        (w["kind"], w["branch"]) for w in result.raw["writes"]
    }


def test_the_cli_reads_a_gitlab_project(env, monkeypatch, capsys):
    routes = {
        WINDOWED_LISTING: (200, fixture("mrs-updated-desc.json")),
        WHOLE_LISTING: (200, fixture("mrs-all.json")),
        BRANCH_SEARCH: (200, fixture("branches-search.json")),
        ORPHAN: (200, fixture("branch-orphan.json")),
    }
    monkeypatch.setattr(
        verifiers, "_http_get_json", lambda url, *a: routes.get(url, (404, {}))
    )
    assert github_writes.main(["--repo", REPO, "--since", "2026-10-01T14:57:30Z"]) == 0
    out = capsys.readouterr().out
    assert MR3_URL in out
    assert "1 pull request(s) and 1 pull-request-less branch(es)" in out
