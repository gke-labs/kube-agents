#!/usr/bin/env python3
"""What Gitea's forge does that the shared contract cannot see.

    python3 -m pytest -q agents/platform/scripts/test_providers_gitea.py

`test_providers_contract.py` holds Gitea to the shapes every forge answers in.
These pin the Gitea-specific decisions behind those shapes: origin composition
(`scheme://host[:port]`), SCP port-segment stripping vs owner-name preservation,
`WIP:` draft prefix stripping and preservation on re-title, numeric label-id
resolution and auto-creation, newest-first commit reversal, three-endpoint
proposal comment merging, collaborator write-permission lookup, and 409/401
error overrides.
"""

from __future__ import annotations

import unittest

import providers
from workspace_paths import WorkspaceError

GiteaForge = next(cls for cls in providers.AVAILABLE if cls.name == "gitea")


def forge(host="gitea.example.test", *, scheme="https", port=None, label="gitea"):
    return GiteaForge(
        host,
        "/var/run/secrets/kubeagents/forges/gitea/token",
        label=label,
        scheme=scheme,
        port=port,
    )


class Api:
    """Answers each call with the next response; records every call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, path, *, params=None, body=None, raw=None):
        self.calls.append((method, path, params or {}, body, raw))
        answer = self.responses.pop(0)
        if isinstance(answer, WorkspaceError):
            raise answer
        return answer


def pr(number=1, state="open", merged=False, title="Add one", source="platform-agent/x", repo="acme/infra", **extra):
    node = {
        "id": 100 + number,
        "number": number,
        "state": state,
        "merged": merged,
        "merged_at": "2026-10-04T14:20:45Z" if merged else None,
        "title": title,
        "body": "body",
        "draft": False,
        "labels": [],
        "user": {"id": 1, "login": "kube-agents"},
        "head": {
            "ref": source,
            "sha": "a" * 40,
            "repo": {"full_name": repo} if repo else None,
        },
        "base": {"ref": "main"},
        "html_url": f"https://gitea.example.test/{repo or 'acme/infra'}/pulls/{number}",
        "created_at": "2026-10-04T14:20:40Z",
        "updated_at": "2026-10-04T14:20:41Z",
        "closed_at": None,
    }
    node.update(extra)
    return node


class IdentityAndConfigurationTest(unittest.TestCase):
    def test_nothing_is_built_unless_configured(self):
        self.assertEqual((), tuple(GiteaForge.for_config({})))
        self.assertEqual((), tuple(GiteaForge.for_config({"forges": None})))

    def test_one_instance_per_configured_host_with_origin_and_credential_helper(self):
        built = GiteaForge.for_config({
            "forges": [
                {
                    "provider": "gitea",
                    "name": "gitea-tls",
                    "host": "gitea.example.test",
                    "token_path": "/var/run/secrets/forges/gitea-tls/token",
                },
                {
                    "provider": "gitea",
                    "name": "gitea-internal",
                    "host": "gitea.gitea.svc.cluster.local",
                    "scheme": "http",
                    "port": 3000,
                    "tokenFile": "/var/run/secrets/forges/gitea-internal/token",
                },
            ]
        })
        self.assertEqual(2, len(built))
        tls, internal = built
        self.assertEqual("https://gitea.example.test/api/v1", tls.api_url)
        self.assertEqual("https://gitea.example.test/acme/infra.git", tls.clone_url("acme/infra"))
        self.assertEqual(("user", "login"), tls.whoami_route)
        self.assertEqual("http://gitea.gitea.svc.cluster.local:3000/api/v1", internal.api_url)
        self.assertEqual(
            "http://gitea.gitea.svc.cluster.local:3000/acme/infra.git",
            internal.clone_url("acme/infra"),
        )
        self.assertEqual(
            (
                ("credential.helper", ""),
                (
                    "credential.http://gitea.gitea.svc.cluster.local:3000.helper",
                    "/opt/defaults/scripts/git_credential_token_file.py /var/run/secrets/forges/gitea-internal/token x-access-token",
                ),
            ),
            internal.credential.git_config("acme/infra"),
        )

    def test_invalid_declarations_raise_value_error(self):
        for bad in (
            {"provider": "gitea", "host": "gitea.example.test"},
            {"provider": "gitea", "host": "gitea.example.test", "token_path": "/t", "scheme": "ftp"},
            {"provider": "gitea", "host": "gitea.example.test", "token_path": "/t", "port": 70000},
            {"provider": "gitea", "host": "gitea.example.test", "token_path": "/t", "port": True},
            {"provider": "gitea", "host": "gitea.example.test", "token_path": "/t", "name": "Bad_Name"},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    GiteaForge.for_config({"forges": [bad]})

    def test_parse_strips_leading_port_only_on_scp_remotes(self):
        with_port = forge(port=3000)
        self.assertEqual(
            "acme/infra",
            with_port.parse("git@gitea.example.test:3000/acme/infra.git"),
        )
        self.assertEqual(
            "acme/infra",
            with_port.parse("gitea.example.test:3000/acme/infra"),
        )
        # An HTTP(S) URL whose owner happens to equal the port number is
        # preserved as `<owner>/<repo>`, while a 3-segment HTTP(S) path is
        # rejected even when its first segment matches the port.
        self.assertEqual(
            "3000/infra",
            with_port.parse("https://gitea.example.test:3000/3000/infra.git"),
        )
        for invalid in (
            "https://gitea.example.test:3000/3000/acme/infra",
            "git@gitea.example.test:3000/acme",
            "git@gitea.example.test:3000/acme/infra/extra",
            "https://other.example.test/acme/infra",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(WorkspaceError):
                    with_port.parse(invalid)


class ProposalTest(unittest.TestCase):
    def test_draft_prefix_is_written_on_create_and_preserved_on_retitle(self):
        created_api = Api(pr(title="WIP: Add one", draft=True))
        created = forge().proposal_create(
            created_api,
            "acme/infra",
            {"title": "Add one", "body": "b", "source": "platform-agent/x", "target": "main", "draft": True},
        )
        self.assertEqual("WIP: Add one", created_api.calls[0][3]["title"])
        self.assertEqual("Add one", created["proposal"]["title"])
        self.assertTrue(created["proposal"]["draft"])

        # Re-titling a draft preserves `WIP: ` without doubling if caller omitted it.
        update_api = Api(
            pr(title="WIP: Old title", draft=True),
            pr(title="WIP: New title", draft=True),
        )
        updated = forge().proposal_update(
            update_api, "acme/infra", {"number": 1, "title": "New title"}
        )
        self.assertEqual("WIP: New title", update_api.calls[1][3]["title"])
        self.assertEqual("New title", updated["proposal"]["title"])
        self.assertTrue(updated["proposal"]["draft"])

    def test_proposal_list_filters_source_branch_to_same_repository_and_labels(self):
        api = Api([
            pr(1, source="platform-agent/x", repo="stranger/infra", labels=[{"name": "Automated-PR"}]),
            pr(2, source="platform-agent/other", repo="acme/infra", labels=[{"name": "Automated-PR"}]),
            pr(3, source="platform-agent/x", repo="acme/infra", labels=[{"name": "automated-pr"}]),
        ])
        res = forge().proposal_list(
            api,
            "acme/infra",
            {"source": "platform-agent/x", "labels": ["Automated-PR"]},
        )
        self.assertEqual([3], [p["number"] for p in res["proposals"]])

    def test_proposal_commits_reverses_gitea_newest_first_to_oldest_first(self):
        newest = {
            "sha": "2" * 40,
            "html_url": "https://gitea.example.test/acme/infra/commit/" + "2" * 40,
            "commit": {
                "author": {"name": "ka"},
                "committer": {"date": "2026-10-04T14:20:41Z"},
                "message": "second",
            },
        }
        oldest = {
            "sha": "1" * 40,
            "html_url": "https://gitea.example.test/acme/infra/commit/" + "1" * 40,
            "commit": {
                "author": {"name": "ka"},
                "committer": {"date": "2026-10-04T14:20:40Z"},
                "message": "first",
            },
        }
        api = Api([newest, oldest])
        res = forge().proposal_commits(api, "acme/infra", {"number": 1})
        self.assertEqual(["1" * 40, "2" * 40], [c["sha"] for c in res["commits"]])
        self.assertFalse(res["truncated"])


class IssueAndLabelTest(unittest.TestCase):
    def test_issue_list_drops_pull_requests_and_excluded_labels_before_counting(self):
        api = Api([
            {
                "number": 1,
                "title": "PR row",
                "state": "open",
                "user": {"login": "u"},
                "labels": [{"name": "bug"}],
                "pull_request": {"merged": False},
            },
            {
                "number": 2,
                "title": "Claimed bug",
                "state": "open",
                "user": {"login": "u"},
                "labels": [{"name": "bug"}, {"name": "status:in-progress"}],
                "pull_request": None,
            },
            {
                "number": 3,
                "title": "Unclaimed bug",
                "state": "open",
                "user": {"login": "u"},
                "labels": [{"name": "bug"}],
                "pull_request": None,
            },
        ])
        res = forge().issue_list(
            api,
            "acme/infra",
            {"labels": ["bug"], "excludeLabels": ["status:in-progress"]},
        )
        self.assertEqual([3], [i["number"] for i in res["issues"]])

    def test_issue_view_refuses_a_pull_request_number(self):
        api = Api({"number": 1, "pull_request": {"merged": False}})
        with self.assertRaises(WorkspaceError) as caught:
            forge().issue_view(api, "acme/infra", {"number": 1})
        self.assertIn("pull request", str(caught.exception))

    def test_labels_creates_missing_label_and_removes_by_id(self):
        api = Api(
            [{"id": 10, "name": "existing"}],
            {"id": 11, "name": "status:triage", "color": "ededed"},
            [{"id": 11, "name": "status:triage"}],
            [{"id": 10, "name": "existing"}, {"id": 11, "name": "status:triage"}],
            {},
            {
                "number": 3,
                "title": "Issue",
                "state": "open",
                "user": {"login": "u"},
                "labels": [{"id": 11, "name": "status:triage"}],
            },
        )
        res = forge().issue_update(
            api,
            "acme/infra",
            {"number": 3, "labelsAdd": ["status:triage"], "labelsRemove": ["existing", "absent"]},
        )
        self.assertEqual(["status:triage"], res["issue"]["labels"])
        self.assertEqual(("POST", "repos/acme/infra/labels"), api.calls[1][:2])
        self.assertEqual({"labels": [11]}, api.calls[2][3])
        self.assertEqual(("DELETE", "repos/acme/infra/issues/3/labels/10"), api.calls[4][:2])


class PermissionAndErrorTest(unittest.TestCase):
    def test_can_write_checks_collaborator_permission(self):
        for perm, expected in (("admin", True), ("write", True), ("owner", True), ("read", False)):
            with self.subTest(perm=perm):
                self.assertEqual(
                    expected,
                    forge().can_write(Api({"permission": perm}), "acme/infra", "alice"),
                )
        self.assertFalse(
            forge().can_write(
                Api(WorkspaceError("not found", status=404, code="FORGE_NOT_FOUND")),
                "acme/infra",
                "stranger",
            )
        )
        self.assertIsNone(
            forge().can_write(
                Api(WorkspaceError("boom", status=502, code="FORGE_UNAVAILABLE")),
                "acme/infra",
                "alice",
            )
        )

    def test_duplicate_proposal_409_maps_to_422_guidance(self):
        override = GiteaForge.error_overrides[409]
        self.assertEqual("FORGE_REJECTED", override("pull request already exists for these targets").code)
        self.assertIsNone(override("lock conflict on another resource"))
        self.assertEqual("FORGE_UNAUTHENTICATED", GiteaForge.error_overrides[401].code)


if __name__ == "__main__":
    unittest.main()
