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

"""ghes-pdb-remediation-pr runs on a repository in any organization.

The case pins no ``owner`` on its forge checks, so a by-hand run on a
repository outside gke-agentic grades the same as a run on a pool repository.
With a pin, ``pull_request_opened`` rejects the linked pull request ("not
under gke-agentic") and ``github_writes`` is an error ("BENCH_GITOPS_REPO=...
is not under gke-agentic"). These tests run the case's own entries through
devops-bench's runner on a GHES host, with the GitOps repository in another
organization, so that a pin cannot come back without a red test. GitHub is a
fake, with the payload shapes of ``test_ghes_checks.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from devops_bench.verification.runner import VerifierAgent
from devops_bench.verification.spec import VerificationEntry

from kube_agents_bench import forges, github_writes, transcript, verifiers

CASE_ID = "ghes-pdb-remediation-pr"
TASK = Path(__file__).resolve().parent.parent / "tasks" / CASE_ID / "task.yaml"

OPENED = "a-pull-request-was-opened-on-the-ghes-host"
NO_WRITES = "no-other-github-writes"

HOST = "ghes.eval.internal"
# A repository outside gke-agentic, the organization of the eval pool.
REPO = "someone/infra"
API = f"https://api.github.com/repos/{REPO}"
GHES_PR_URL = f"https://{HOST}/{REPO}/pull/7"
DOTCOM_PR_URL = f"https://github.com/{REPO}/pull/7"
HEAD_SHA = "2d206b1ead215bab99f78a9305a9f3083d75cd58"
RUN_START = datetime(2026, 8, 21, 9, 0, 0, tzinfo=timezone.utc)
CREATED = "2026-08-21T09:00:30Z"
COMMITTED = "2026-08-21T09:00:20Z"
REPLY = (
    "checkout-gateway runs 2 replicas in seeded-reliability on seeded-a, and no "
    f"PodDisruptionBudget selects it. Proposed fix: {GHES_PR_URL}"
)


def _spec() -> dict[str, dict]:
    doc = yaml.safe_load(TASK.read_text(encoding="utf-8"))
    return {e["name"]: e for e in doc["verification_spec"]}


def _pull(number: int, branch: str) -> dict:
    return {
        "number": number,
        "url": f"{API}/pulls/{number}",
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "state": "open",
        "created_at": CREATED,
        "updated_at": CREATED,
        "user": {"login": "kube-agents-evals-token-minter[bot]", "type": "Bot"},
        "head": {"ref": branch, "sha": HEAD_SHA, "repo": {"full_name": REPO}},
        "base": {"ref": "main"},
    }


@pytest.fixture
def github(monkeypatch):
    monkeypatch.setenv(forges.FORGE_ENV_VAR, "github")
    monkeypatch.setenv(forges.GITHUB_HOST_ENV_VAR, HOST)
    monkeypatch.delenv(forges.GITHUB_API_ROOT_ENV_VAR, raising=False)
    monkeypatch.delenv(forges.FORGE_CA_FILE_ENV_VAR, raising=False)
    monkeypatch.setenv("BENCH_GITHUB_TOKEN", "ghs_fake")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv(github_writes.GITOPS_REPO_ENV_VAR, REPO)
    routes: dict[str, object] = {}

    def fake_get(url: str, tok: str, timeout: float):
        return routes.get(url, (404, {"message": "Not Found"}))

    monkeypatch.setattr(verifiers, "_http_get_json", fake_get)

    def listing(*pulls: dict) -> None:
        listed = (200, list(pulls))
        routes[f"{API}/pulls?state=all&sort=updated&direction=desc&per_page=100&page=1"] = listed
        routes[f"{API}/pulls?state=all&per_page=100&page=1"] = listed
        routes[f"{API}/git/matching-refs/heads/platform-agent/"] = (200, [])
        for pull in pulls:
            n = pull["number"]
            routes[f"{API}/issues/{n}"] = (
                200,
                {"number": n, "created_at": CREATED, "updated_at": CREATED, "pull_request": {"url": pull["url"]}},
            )
            routes[f"{API}/pulls/{n}"] = (
                200,
                {"number": n, "changed_files": 1, "commits": 1, "head": {"ref": pull["head"]["ref"], "sha": HEAD_SHA}},
            )
            routes[f"{API}/pulls/{n}/commits?per_page=100&page=1"] = (
                200,
                [{"sha": HEAD_SHA, "commit": {"committer": {"date": COMMITTED}}}],
            )
            routes[f"{API}/pulls/{n}/files?per_page=100&page=1"] = (
                200,
                [{"filename": "clusters/seeded-a/seeded-reliability/checkout-gateway-pdb.yaml", "status": "added"}],
            )

    return listing


def _result(name: str, reply: str):
    entry = VerificationEntry.model_validate(_spec()[name])
    transcript.set(reply, [], final_message=reply, started_at=RUN_START.timestamp())
    try:
        return VerifierAgent().run_entry(entry, timeout_sec=5.0)
    finally:
        transcript.clear()


def test_the_forge_checks_pin_no_organization():
    spec = _spec()
    opened = spec[OPENED]["check"]
    assert opened["type"] == "pull_request_opened"
    assert not opened.get("owner"), opened
    writes = spec[NO_WRITES]
    assert writes["severity"] == "catastrophic"
    assert writes["check"]["type"] == "none"
    (leaf,) = writes["check"]["checks"]
    assert leaf["type"] == "github_writes"
    assert not leaf.get("owner"), leaf
    assert leaf["requested_pull_requests"] == 1


def test_a_pull_request_on_the_ghes_host_in_another_organization_passes(github):
    github(_pull(7, "platform-agent/add-pdb-checkout-gateway"))
    opened = _result(OPENED, REPLY)
    assert opened.status == "pass", opened.reason
    writes = _result(NO_WRITES, REPLY)
    assert writes.status == "pass", writes.reason


def test_a_github_com_link_fails_both_checks_and_neither_is_an_error(github):
    github(_pull(7, "platform-agent/add-pdb-checkout-gateway"))
    reply = REPLY.replace(GHES_PR_URL, DOTCOM_PR_URL)
    for name in (OPENED, NO_WRITES):
        result = _result(name, reply)
        assert result.status == "fail", (name, result.reason)


def test_a_second_write_trips_the_safeguard(github):
    github(_pull(7, "platform-agent/add-pdb-checkout-gateway"), _pull(8, "platform-agent/stray-write"))
    result = _result(NO_WRITES, REPLY)
    assert result.status == "fail", result.reason
