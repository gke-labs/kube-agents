#!/usr/bin/env python3
"""What Gitea's forge does that the shared contract cannot see.

    python3 -m pytest -q agents/platform/scripts/test_providers_gitea.py

`test_providers_contract.py` holds Gitea to the shapes every forge answers in.
These pin the Gitea-specific decisions behind those shapes: origin composition
(`scheme://host[:port]`), numeric owner-name preservation vs schemeless port
refusal,
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


def forge(host="gitea.example.test", *, scheme="https", port=None, label="gitea", ca_file="", ca_source=""):
    return GiteaForge(
        host,
        "/var/run/kube-agents/forge-credentials/gitea/token",
        label=label,
        scheme=scheme,
        port=port,
        ca_file=ca_file,
        ca_source=ca_source,
    )


class Api:
    """Answers each call with the next response; records every call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.with_headers = []

    def __call__(self, method, path, *, params=None, body=None, raw=None, with_headers=False):
        self.calls.append((method, path, params or {}, body, raw))
        self.with_headers.append(with_headers)
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
                    "token_path": "/var/run/kube-agents/forge-credentials/gitea-tls/token",
                    "ca_file": "/etc/kube-agents/forge-ca/gitea-tls/ca.crt",
                    "ca_source": "the Secret gitea-ca or its key ca.crt",
                    "allowed_paths": ["acme"],
                },
                {
                    "provider": "gitea",
                    "name": "gitea-internal",
                    "host": "gitea.gitea.svc.cluster.local",
                    "scheme": "http",
                    "port": 3000,
                    "token_path": "/var/run/kube-agents/forge-credentials/gitea-internal/token",
                    "allowed_paths": [],
                },
            ]
        })
        self.assertEqual(2, len(built))
        tls, internal = built
        self.assertEqual("https://gitea.example.test/api/v1", tls.api_url)
        self.assertEqual("https://gitea.example.test/acme/infra.git", tls.clone_url("acme/infra"))
        self.assertEqual(
            "https://gitea.example.test/acme/infra/src/commit/{ref}/{path}",
            tls.file_url("acme/infra"),
        )
        self.assertEqual("/etc/kube-agents/forge-ca/gitea-tls/ca.crt", tls.ca_file)
        self.assertEqual("the Secret gitea-ca or its key ca.crt", tls.ca_source)
        self.assertEqual(("user", "login"), tls.whoami_route)
        self.assertEqual("http://gitea.gitea.svc.cluster.local:3000/api/v1", internal.api_url)
        self.assertEqual(
            "http://gitea.gitea.svc.cluster.local:3000/acme/infra.git",
            internal.clone_url("acme/infra"),
        )
        self.assertEqual(
            "http://gitea.gitea.svc.cluster.local:3000/acme/infra/src/commit/{ref}/{path}",
            internal.file_url("acme/infra"),
        )
        self.assertEqual(
            (
                ("credential.helper", ""),
                (
                    "credential.http://gitea.gitea.svc.cluster.local:3000.helper",
                    "/opt/defaults/scripts/git_credential_token_file.py /var/run/kube-agents/forge-credentials/gitea-internal/token x-access-token",
                ),
            ),
            internal.credential.git_config("acme/infra"),
        )

    def test_invalid_declarations_raise_value_error(self):
        for bad in (
            {"provider": "gitea", "host": "gitea.example.test"},
            {"provider": "gitea", "host": "gitea.example.test", "token_path": "/t"},
            {"provider": "gitea", "host": "gitea.example.test", "token_path": "/t", "allowed_paths": [], "scheme": "ftp"},
            {"provider": "gitea", "host": "gitea.example.test", "token_path": "/t", "allowed_paths": [], "scheme": "http"},
            {"provider": "gitea", "host": "gitea.svc.cluster.local", "token_path": "/t", "allowed_paths": [], "scheme": "http", "ca_file": "/ca.crt"},
            {"provider": "gitea", "host": "gitea.example.test", "token_path": "/t", "allowed_paths": [], "port": 70000},
            {"provider": "gitea", "host": "gitea.example.test", "token_path": "/t", "allowed_paths": [], "port": True},
            {"provider": "gitea", "host": "gitea.example.test", "token_path": "/t", "allowed_paths": [], "name": "Bad_Name"},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    GiteaForge.for_config({"forges": [bad]})

    def test_reach_pages_user_repos_and_reports_truncation(self):
        # Pages shorter than FORGE_PAGE_SIZE (50) when a Gitea server lowers
        # [api] MAX_RESPONSE_ITEMS must keep paginating until an empty batch.
        page_one = [{"full_name": f"acme/repo-{i}"} for i in range(1, 31)]
        page_two = [{"full_name": f"acme/repo-{i}"} for i in range(31, 61)]
        api = Api(page_one, page_two, [])
        repos, truncated = forge().reach(api)
        self.assertEqual(60, len(repos))
        self.assertFalse(truncated)
        self.assertEqual(("GET", "user/repos", {"limit": 50, "page": 1}, None, None), api.calls[0])
        self.assertEqual(("GET", "user/repos", {"limit": 50, "page": 2}, None, None), api.calls[1])
        self.assertEqual(("GET", "user/repos", {"limit": 50, "page": 3}, None, None), api.calls[2])

        capped_api = Api(*[[{"full_name": f"acme/r-{p}"}] for p in range(GiteaForge.REACH_PAGES)])
        _, capped = forge().reach(capped_api)
        self.assertTrue(capped)

    def test_parse_preserves_numeric_owner_and_refuses_schemeless_port_spelling(self):
        with_port = forge(port=3000)
        # An HTTP(S) URL or a 2-segment SCP remote (`user@host:owner/name`)
        # whose owner happens to equal the port number is preserved as
        # `<owner>/<repo>` on both spellings.
        self.assertEqual(
            "3000/infra",
            with_port.parse("https://gitea.example.test:3000/3000/infra.git"),
        )
        self.assertEqual(
            "3000/infra",
            with_port.parse("git@gitea.example.test:3000/infra.git"),
        )
        self.assertEqual(
            "acme/infra",
            with_port.parse("git@gitea.example.test:acme/infra.git"),
        )
        for ported_without_scheme in (
            "gitea.example.test:3000/acme/infra",
            "gitea.example.test:3000/infra",
            "git@gitea.example.test:3000/acme/infra.git",
        ):
            with self.subTest(ported_without_scheme=ported_without_scheme):
                with self.assertRaises(WorkspaceError) as caught:
                    with_port.parse(ported_without_scheme)
                self.assertIn(
                    "https://gitea.example.test:3000/owner/name",
                    str(caught.exception),
                )
        for invalid in (
            "https://gitea.example.test:3000/3000/acme/infra",
            "git@gitea.example.test:acme",
            "git@gitea.example.test:acme/infra/extra",
            "https://other.example.test/acme/infra",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(WorkspaceError):
                    with_port.parse(invalid)


    def test_allowed_paths_narrows_parse_and_rejects_invalid_prefixes(self):
        restricted = GiteaForge.for_config({
            "forges": [
                {
                    "provider": "gitea",
                    "host": "gitea.example.test",
                    "token_path": "/var/run/secrets/forges/gitea/token",
                    "allowed_paths": ["acme", "platform/gitops"],
                }
            ]
        })[0]
        self.assertEqual("acme/infra", restricted.parse("https://gitea.example.test/acme/infra.git"))
        self.assertEqual(
            "platform/gitops",
            restricted.parse("https://gitea.example.test/platform/gitops"),
        )
        for forbidden in (
            "https://gitea.example.test/other/infra",
            "https://gitea.example.test/platform/other",
            "https://gitea.example.test/acme-extra/infra",
        ):
            with self.subTest(forbidden=forbidden):
                with self.assertRaises(WorkspaceError) as caught:
                    restricted.parse(forbidden)
                self.assertEqual(403, caught.exception.status)
                self.assertEqual("REPOSITORY_NOT_ALLOWED", caught.exception.fields.get("code"))

        for bad_paths in ([""], ["   "], ["acme /infra"], ["gitea.example.test"], ["a/b/c"]):
            with self.subTest(bad_paths=bad_paths):
                with self.assertRaises(ValueError):
                    GiteaForge.for_config({
                        "forges": [
                            {
                                "provider": "gitea",
                                "host": "gitea.example.test",
                                "token_path": "/t",
                                "allowed_paths": bad_paths,
                            }
                        ]
                    })


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
        api = Api(
            [
                pr(1, source="platform-agent/x", repo="stranger/infra", labels=[{"name": "Automated-PR"}]),
                pr(2, source="platform-agent/other", repo="acme/infra", labels=[{"name": "Automated-PR"}]),
                pr(3, source="platform-agent/x", repo="acme/infra", labels=[{"name": "automated-pr"}]),
            ],
            [],
        )
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
        api = Api([newest, oldest], [])
        res = forge().proposal_commits(api, "acme/infra", {"number": 1})
        self.assertEqual(["1" * 40, "2" * 40], [c["sha"] for c in res["commits"]])
        self.assertFalse(res["truncated"])

    def test_listings_and_commits_read_past_clamped_server_pages(self):
        # When a Gitea server clamps MAX_RESPONSE_ITEMS below FORGE_PAGE_SIZE (50),
        # _walk, _conversation_pages, and proposal_commits must keep reading until
        # an empty batch or the caller's limit rather than stopping after page 1.
        page_one = [pr(i, source="platform-agent/other") for i in range(1, 31)]
        page_two = [pr(31, source="platform-agent/target")]
        api = Api(page_one, page_two, [])
        res = forge().proposal_list(api, "acme/infra", {"source": "platform-agent/target"})
        self.assertEqual([31], [p["number"] for p in res["proposals"]])
        self.assertFalse(res["truncated"])

    def test_proposal_commits_stops_reporting_truncated_on_empty_page_past_cap(self):
        def commit_node(idx: int) -> dict:
            sha = f"{idx:040x}"
            return {
                "sha": sha,
                "html_url": f"https://gitea.example.test/acme/infra/commit/{sha}",
                "commit": {
                    "author": {"name": "ka"},
                    "committer": {"date": "2026-10-04T14:20:40Z"},
                    "message": f"commit {idx}",
                },
            }

        batches = [[commit_node(i) for i in range(b * 50 + 1, (b + 1) * 50 + 1)] for b in range(5)]
        # Page 3 (items 201..250 of 250) is partial (50 < 100) and page 4 is empty;
        # neither should claim truncated=True.
        page3 = forge().proposal_commits(Api(*batches), "acme/infra", {"number": 1, "limit": 100, "page": 3})
        self.assertEqual(50, len(page3["commits"]))
        self.assertFalse(page3["truncated"])
        page4 = forge().proposal_commits(Api(*batches), "acme/infra", {"number": 1, "limit": 100, "page": 4})
        self.assertEqual([], page4["commits"])
        self.assertFalse(page4["truncated"])

    def test_list_by_author_reads_the_total_header(self):
        api = Api(([pr(4, state="closed", merged=True)], {"x-total-count": "41"}))
        answer = forge().proposal_list_by_author(
            api, "acme/infra", {"author": "kube-agents", "authorId": 7, "page": 2, "limit": 1}
        )
        self.assertEqual([True], api.with_headers)
        self.assertEqual(41, answer["total"])
        self.assertEqual(["merged"], [p["state"] for p in answer["proposals"]])
        self.assertTrue(answer["truncated"])
        self.assertEqual("repos/acme/infra/pulls", api.calls[0][1])
        self.assertEqual({"state": "all", "poster": 7, "sort": "recentupdate", "limit": 1, "page": 2}, api.calls[0][2])
        # A page the server clamped short is still a page when the total says so.
        clamped = forge().proposal_list_by_author(
            Api(([pr(1)], {"x-total-count": "3"})), "acme/infra", {"author": "kube-agents", "authorId": 7, "limit": 2}
        )
        self.assertTrue(clamped["truncated"])
        # A server that clamps a page to 50 pages by 50 too: page 2 of 120 has
        # read 100, not 150, so it is not the last page.
        second = forge().proposal_list_by_author(
            Api(([pr(n) for n in range(51, 101)], {"x-total-count": "120"})),
            "acme/infra",
            {"author": "kube-agents", "authorId": 7, "limit": 100, "page": 2},
        )
        self.assertTrue(second["truncated"])
        last = forge().proposal_list_by_author(
            Api(([], {"x-total-count": "120"})),
            "acme/infra",
            {"author": "kube-agents", "authorId": 7, "limit": 100, "page": 4},
        )
        self.assertFalse(last["truncated"])
        missing = forge().proposal_list_by_author(
            Api(([], {})), "acme/infra", {"author": "kube-agents", "authorId": 7}
        )
        self.assertIsNone(missing["total"])
        self.assertFalse(missing["truncated"])

    def test_list_by_author_needs_the_author_id(self):
        for author_id in (None, "7", 0, True):
            with self.subTest(author_id=author_id):
                api = Api()
                with self.assertRaises(WorkspaceError):
                    forge().proposal_list_by_author(
                        api, "acme/infra", {"author": "kube-agents", "authorId": author_id}
                    )
                self.assertEqual([], api.calls)

    def test_a_proposal_never_closed_has_no_closer(self):
        answer = forge().proposal_closer(Api([], []), "acme/infra", {"number": 2})
        self.assertEqual({"actor": "", "bot": False}, answer)


class IssueAndLabelTest(unittest.TestCase):
    def test_issue_list_drops_pull_requests_and_excluded_labels_before_counting(self):
        api = Api(
            [
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
            ],
            [],
        )
        res = forge().issue_list(
            api,
            "acme/infra",
            {"labels": ["bug"], "excludeLabels": ["status:in-progress"]},
        )
        self.assertEqual([3], [i["number"] for i in res["issues"]])

    def test_issue_view_update_and_close_refuse_a_pull_request_number(self):
        for call in (
            lambda f, a: f.issue_view(a, "acme/infra", {"number": 1}),
            lambda f, a: f.issue_update(a, "acme/infra", {"number": 1, "title": "Not an issue"}),
            lambda f, a: f.issue_close(a, "acme/infra", {"number": 1, "reason": "completed"}),
        ):
            api = Api({"number": 1, "pull_request": {"merged": False}})
            with self.assertRaises(WorkspaceError) as caught:
                call(forge(), api)
            self.assertIn("pull request", str(caught.exception))
            self.assertEqual(1, len(api.calls))

        # proposal_update checks GET repos/{repo}/pulls/{number} before
        # touching labels so an issue number is refused without attaching labels.
        pull_404 = Api(WorkspaceError("not found", status=404))
        with self.assertRaises(WorkspaceError):
            forge().proposal_update(
                pull_404, "acme/infra", {"number": 7, "labelsAdd": ["needs-review"]}
            )
        self.assertEqual([("GET", "repos/acme/infra/pulls/7", {}, None, None)], pull_404.calls)

    def test_issue_list_respects_page(self):
        api = Api(
            [
                {"number": 1, "title": "First", "state": "open", "user": {"login": "u"}, "labels": [], "pull_request": None},
                {"number": 2, "title": "Second", "state": "open", "user": {"login": "u"}, "labels": [], "pull_request": None},
            ],
            [],
        )
        res = forge().issue_list(api, "acme/infra", {"limit": 1, "page": 2})
        self.assertEqual([2], [i["number"] for i in res["issues"]])
        self.assertFalse(res["truncated"])

    def test_labels_creates_missing_label_and_removes_by_id(self):
        api = Api(
            {"number": 3, "pull_request": None},
            [{"id": 10, "name": "existing"}],
            [],
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
        self.assertEqual(("POST", "repos/acme/infra/labels"), api.calls[3][:2])
        self.assertEqual({"labels": [11]}, api.calls[4][3])
        self.assertEqual(("DELETE", "repos/acme/infra/issues/3/labels/10"), api.calls[6][:2])


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
