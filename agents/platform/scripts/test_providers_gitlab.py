#!/usr/bin/env python3
"""What GitLab's forge does that the shared contract cannot see.

    python3 -m pytest -q agents/platform/scripts/test_providers_gitlab.py

`test_providers_contract.py` holds GitLab to the shapes every forge answers in.
These pin the GitLab-specific decisions behind those shapes: the encoded
project path, nested groups and `allowedPaths`, the state vocabulary in both
directions, drafts, notes, the diff fallback, and how write access is asked.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import providers
from providers import registry as registry_module
from providers.base import ForgeUnsupported
from workspace_paths import WorkspaceError

GitLabForge = next(cls for cls in providers.AVAILABLE if cls.name == "gitlab")


def forge(host="gitlab.com", allowed=()):
    return GitLabForge(host, "/var/run/forge/token", allowed)


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


def mr(iid=1, state="opened", source_project=1001, target_project=1001, **extra):
    node = {
        "id": 50000000 + iid, "iid": iid, "state": state, "title": f"mr {iid}",
        "source_branch": "platform-agent/x", "target_branch": "main",
        "source_project_id": source_project, "target_project_id": target_project,
        "labels": [], "author": {"username": "u"}, "sha": "a" * 40,
    }
    node.update(extra)
    return node


class IdentityTest(unittest.TestCase):
    def test_a_project_is_one_encoded_segment_at_any_depth(self):
        # The default `quote` leaves `/` alone, which GitLab reads as another
        # route and answers with a 404 that looks like a permissions problem.
        api = Api(mr())
        forge().proposal_view(api, "acme/platform/infra", {"number": 1})
        self.assertEqual("projects/acme%2Fplatform%2Finfra/merge_requests/1", api.calls[0][1])

    def test_nested_groups_parse_and_a_lone_segment_does_not(self):
        self.assertEqual("acme/platform/infra", forge().parse("https://gitlab.com/acme/platform/infra.git"))
        self.assertEqual("acme/infra", forge().parse("gitlab.com/acme/infra"))
        with self.assertRaises(WorkspaceError):
            forge().parse("https://gitlab.com/acme")

    def test_another_host_is_not_this_forges_repository(self):
        with self.assertRaises(WorkspaceError):
            forge().parse("https://gitlab.example.com/acme/infra")

    def test_allowed_paths_match_whole_segments(self):
        # `acme/infra-secret` starts with the string `acme/infra`.
        scoped = forge(allowed=("acme/infra", "Platform"))
        self.assertEqual("acme/infra", scoped.parse("acme/infra"))
        self.assertEqual("platform/team/x", scoped.parse("platform/team/x"))
        with self.assertRaises(WorkspaceError) as caught:
            scoped.parse("acme/infra-secret")
        self.assertEqual(403, caught.exception.status)
        self.assertEqual("REPOSITORY_NOT_ALLOWED", caught.exception.fields["code"])

    def test_clone_url_and_api_root_are_the_configured_hosts(self):
        self_managed = forge("gitlab.example.com")
        self.assertEqual("https://gitlab.example.com/acme/infra.git", self_managed.clone_url("acme/infra"))
        self.assertEqual("https://gitlab.example.com/api/v4", self_managed.api_url)
        self.assertEqual(("user", "username"), self_managed.whoami_route)


class ConfigurationTest(unittest.TestCase):
    def test_nothing_is_built_unless_configured(self):
        self.assertEqual((), tuple(GitLabForge.for_config({})))
        self.assertEqual((), tuple(GitLabForge.for_config({"forges": None})))

    def test_one_instance_per_configured_host(self):
        built = GitLabForge.for_config({"forges": [
            {"provider": "gitlab", "host": "gitlab.com", "token_path": "/t/a", "allowed_paths": ("acme",)},
            {"provider": "gitlab", "host": "gitlab.example.com", "token_path": "/t/b", "allowed_paths": ()},
            {"provider": "github", "host": "github.com"},
        ]})
        self.assertEqual([("gitlab.com", "www.gitlab.com"), ("gitlab.example.com",)], [f.hosts for f in built])

    def test_an_entry_without_a_token_path_stops_the_build(self):
        with self.assertRaises(ValueError):
            GitLabForge.for_config({"forges": [{"provider": "gitlab", "host": "gitlab.com"}]})

    def test_www_gitlab_com_is_gitlab_com(self):
        # Review (#2439): the operator folds `www.gitlab.com` onto gitlab.com;
        # the broker refused a URL written that way.
        built = forge()
        self.assertEqual("gitlab.com", built.hosts[0])
        self.assertEqual("acme/infra", built.parse("https://www.gitlab.com/acme/infra"))
        self.assertEqual(("gitlab.example.com",), GitLabForge("gitlab.example.com", "/t").hosts)

    def test_the_whole_host_is_allowed_only_when_asked_for(self):
        # Review: omitting allowedPaths silently granted the whole host.
        with self.assertRaises(ValueError) as caught:
            GitLabForge.for_config({"forges": [{"provider": "gitlab", "host": "gitlab.com", "token_path": "/t"}]})
        self.assertIn("[] for the whole host", str(caught.exception))
        whole = GitLabForge.for_config({"forges": [
            {"provider": "gitlab", "host": "gitlab.com", "token_path": "/t", "allowed_paths": ()},
        ]})
        self.assertEqual((), whole[0].allowed_paths)

    def test_an_allowed_path_naming_the_host_itself_stops_the_build(self):
        # Review round 4: `gitlab.com` built a prefix no parsed repository
        # can start with, refusing every repository on the host.
        for bad in ("gitlab.com", "gitlab.com/", "/gitlab.com", "GitLab.com"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as caught:
                    GitLabForge("gitlab.com", "/t", (bad,))
                self.assertIn("[] for the whole host", str(caught.exception))

    def test_an_allowed_path_no_repository_can_have_stops_the_build(self):
        # Review: `acme//infra` or ` acme` matched nothing and refused every
        # repository on the host, one request at a time.
        # Review round 2: and the segments `parse` refuses -- `.`, `..`,
        # `.git`, a leading dash -- which `SEGMENT_RE` alone admitted.
        for bad in ("acme//infra", " acme", "acme/in fra", "acme/..", ".", "-acme", "acme/.git"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    GitLabForge("gitlab.com", "/t", (bad,))

    def test_an_allowed_path_is_read_as_parse_reads_a_repository(self):
        # Review round 3: `acme/infra.git` (a clone URL's tail) and
        # `gitlab.com/acme` (the host-led shorthand) built, and `parse`
        # strips the one and lifts the other, so neither ever matched.
        for written, prefix in (
            ("acme/infra.git", ("acme", "infra")),
            ("gitlab.com/acme", ("acme",)),
            ("https://gitlab.com/Acme/Platform", ("acme", "platform")),
        ):
            with self.subTest(written=written):
                built = GitLabForge("gitlab.com", "/t", (written,))
                self.assertEqual((prefix,), built.allowed_paths)
        built = GitLabForge("gitlab.com", "/t", ("acme/infra.git",))
        self.assertEqual("acme/infra", built.parse("https://gitlab.com/acme/infra.git"))
        with self.assertRaises(ValueError):
            GitLabForge("gitlab.com", "/t", ("https://github.com/acme",))

    def test_an_allowed_path_that_names_no_namespace_is_refused_not_dropped(self):
        # Review round 2: `[""]`, `["/"]` and `["//"]` trimmed to nothing and
        # left the list empty -- the whole host.
        for bad in ("", "/", "//"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as caught:
                    GitLabForge("gitlab.com", "/t", (bad,))
                self.assertIn("names no namespace", str(caught.exception))
        self.assertEqual((("acme",),), GitLabForge("gitlab.com", "/t", ("/acme/",)).allowed_paths)

    def test_the_token_is_read_from_the_file_into_private_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "token"
            token.write_text("glpat-x\n")
            built = GitLabForge("gitlab.com", str(token))
            self.assertEqual({"PRIVATE-TOKEN": "glpat-x"}, built.credential.headers("acme/infra"))
            self.assertEqual(
                f"credential.https://gitlab.com.helper",
                built.credential.git_config("acme/infra")[1][0],
            )


class ProposalTest(unittest.TestCase):
    def test_a_draft_is_a_title_prefix_and_never_doubled(self):
        # The `draft` field is accepted and ignored on create.
        for title, sent in (
            ("Pin the image", "Draft: Pin the image"),
            ("Draft: Pin", "Draft: Pin"),
            ("draft: Pin", "draft: Pin"),  # GitLab matches the prefix case-blind
            ("WIP: Pin", "Draft: WIP: Pin"),  # GitLab stopped reading WIP in 16.0
        ):
            with self.subTest(title=title):
                api = Api(mr(title=sent, draft=True))
                forge().proposal_create(api, "acme/infra", {
                    "title": title, "source": "platform-agent/x", "target": "main", "draft": True,
                })
                self.assertEqual(sent, api.calls[0][3]["title"])
                self.assertNotIn("draft", api.calls[0][3])

    def test_closed_includes_merged_as_it_does_everywhere(self):
        api = Api([mr(1, "opened"), mr(2, "merged", merged_at="2026-10-01T00:00:00Z"), mr(3, "closed")])
        answer = forge().proposal_list(api, "acme/infra", {"state": "closed", "limit": 3})
        self.assertEqual("all", api.calls[0][2]["state"])
        self.assertEqual([2, 3], [p["number"] for p in answer["proposals"]])
        self.assertTrue(answer["truncated"])

    def test_a_forks_branch_of_the_same_name_does_not_answer_for_ours(self):
        api = Api([mr(1, source_project=2002), mr(2)])
        answer = forge().proposal_list(api, "acme/infra", {"source": "platform-agent/x", "state": "open"})
        self.assertEqual("platform-agent/x", api.calls[0][2]["source_branch"])
        self.assertEqual([2], [p["number"] for p in answer["proposals"]])
        self.assertEqual("acme/infra", answer["proposals"][0]["sourceRepo"])

    def test_forks_of_the_same_branch_name_cannot_crowd_ours_off_the_page(self):
        # Review (#2437): with limit 1, a fork's same-named branch filled the
        # page and the filter then left nothing.
        api = Api([mr(1, source_project=2002), mr(2, source_project=2003), mr(3)])
        answer = forge().proposal_list(
            api, "acme/infra", {"source": "platform-agent/x", "state": "open", "limit": 1}
        )
        self.assertEqual(100, api.calls[0][2]["per_page"])
        self.assertEqual([3], [p["number"] for p in answer["proposals"]])
        self.assertFalse(answer["truncated"])

    def test_page_two_of_a_source_filtered_listing_continues_page_one(self):
        # Review: page 1 came from 100-row pages and page 2 from GitLab's own
        # `limit`-row pages, so the two overlapped.
        rows = []
        for i in range(1, 9):
            rows += [mr(100 + i, source_project=2002), mr(i)]  # forks interleaved with ours
        pages = []
        for page in (1, 2, 3):
            api = Api(list(rows))
            answer = forge().proposal_list(
                api, "acme/infra", {"source": "platform-agent/x", "state": "open", "limit": 3, "page": page}
            )
            self.assertEqual((100, 1), (api.calls[0][2]["per_page"], api.calls[0][2]["page"]))
            pages.append(([p["number"] for p in answer["proposals"]], answer["truncated"]))
        self.assertEqual([([1, 2, 3], True), ([4, 5, 6], True), ([7, 8], False)], pages)

    def test_the_diff_falls_back_to_the_json_diffs_until_gitlab_has_computed_it(self):
        api = Api(
            mr(),
            WorkspaceError("not ready", status=502, code="FORGE_UNAVAILABLE"),
            [{"old_path": "a.yaml", "new_path": "a.yaml", "diff": "@@ -1 +1 @@\n-x\n+y\n"}],
        )
        answer = forge().proposal_view(api, "acme/infra", {"number": 1, "diff": True})
        self.assertIn("diff --git a/a.yaml b/a.yaml\n--- a/a.yaml\n+++ b/a.yaml\n@@ -1 +1 @@", answer["diff"])
        self.assertEqual("text/plain", api.calls[1][4])

    def test_a_diff_refused_for_a_reason_other_than_not_ready_is_not_hidden(self):
        api = Api(mr(), WorkspaceError("forbidden", status=403))
        with self.assertRaises(WorkspaceError):
            forge().proposal_view(api, "acme/infra", {"number": 1, "diff": True})

    def test_the_brokers_own_refusal_of_a_diff_is_not_refetched_page_by_page(self):
        # Review: a raw diff over the ceiling (or past the deadline) is a 502
        # the transport made, and the fallback fetched the same diff again.
        for code in ("FORGE_RESPONSE_TOO_LARGE", "FORGE_CALL_FAILED"):
            with self.subTest(code=code):
                api = Api(mr(), WorkspaceError("refused", status=502, code=code))
                with self.assertRaises(WorkspaceError):
                    forge().proposal_view(api, "acme/infra", {"number": 1, "diff": True})
                self.assertEqual(2, len(api.calls))

    def test_the_assembled_fallback_stops_at_its_own_ceiling(self):
        big = [{"old_path": f"f{i}", "new_path": f"f{i}", "diff": "x" * 50_000} for i in range(100)]
        api = Api(mr(), WorkspaceError("not ready", status=502, code="FORGE_UNAVAILABLE"), big)
        with mock.patch.object(GitLabForge, "DIFF_FALLBACK_CHARS", 1_000_000):
            diff = forge().proposal_view(api, "acme/infra", {"number": 1, "diff": True})["diff"]
        self.assertIn("diff cut short: stopped after", diff)
        self.assertEqual(3, len(api.calls))

    def test_a_label_gitlab_reads_as_a_keyword_is_refused_in_a_filter(self):
        # Review: `labels=None` lists unlabelled items, the wrong set, silently.
        for verb, field in (("issue_list", "labels"), ("issue_list", "excludeLabels"), ("proposal_list", "labels")):
            for name in ("None", "any"):
                with self.subTest(verb=verb, field=field, name=name):
                    with self.assertRaises(WorkspaceError):
                        getattr(forge(), verb)(Api(), "acme/infra", {field: [name]})

    def test_an_instance_older_than_raw_diffs_falls_back_and_a_missing_mr_still_fails(self):
        files = [{"old_path": "a.yaml", "new_path": "a.yaml", "diff": "@@ -1 +1 @@\n-x\n+y\n"}]
        answer = forge().proposal_view(
            Api(mr(), WorkspaceError("no route", status=404), files), "acme/infra", {"number": 1, "diff": True}
        )
        self.assertIn("diff --git a/a.yaml b/a.yaml", answer["diff"])
        gone = Api(mr(), WorkspaceError("no route", status=404), WorkspaceError("no mr", status=404))
        with self.assertRaises(WorkspaceError):
            forge().proposal_view(gone, "acme/infra", {"number": 1, "diff": True})

    def test_the_diff_fallback_reads_every_page_and_names_what_it_left_out(self):
        page = [{"old_path": f"f{i}", "new_path": f"f{i}", "diff": "@@\n"} for i in range(100)]
        big = [{"old_path": "huge.bin", "new_path": "huge.bin", "diff": "", "too_large": True}]
        api = Api(mr(), WorkspaceError("not ready", status=502, code="FORGE_UNAVAILABLE"), page, big)
        diff = forge().proposal_view(api, "acme/infra", {"number": 1, "diff": True})["diff"]
        self.assertEqual([1, 2], [call[2]["page"] for call in api.calls[2:]])
        self.assertIn("diff --git a/f99 b/f99", diff)
        self.assertIn("did not include the changes to huge.bin", diff)
        capped = Api(mr(), WorkspaceError("not ready", status=502, code="FORGE_UNAVAILABLE"), *([page] * GitLabForge.DIFF_PAGES))
        self.assertIn("diff cut short", forge().proposal_view(capped, "acme/infra", {"number": 1, "diff": True})["diff"])

    def test_an_update_that_changes_nothing_reads_instead_of_writing(self):
        # GitLab answers 400 for an update with no parameters.
        api = Api(mr())
        forge().proposal_update(api, "acme/infra", {"number": 1})
        self.assertEqual("GET", api.calls[0][0])

    def test_labels_travel_in_the_same_update_as_the_text(self):
        api = Api(mr(), mr())
        forge().proposal_update(api, "acme/infra", {
            "number": 1, "title": "t", "labelsAdd": ["a", "b"], "labelsRemove": ["c"],
        })
        self.assertEqual(
            {"title": "t", "add_labels": "a,b", "remove_labels": "c"}, api.calls[1][3]
        )

    def test_a_new_title_keeps_a_draft_a_draft(self):
        # Review round 3: on GitLab the marker is the title, which this forge
        # may have written on create; a re-title without it marked the merge
        # request ready, where GitHub leaves `draft` alone.
        api = Api(mr(draft=True, title="Draft: Pin"), mr(draft=True))
        forge().proposal_update(api, "acme/infra", {"number": 1, "title": "Pin to v2"})
        self.assertEqual(("GET", "PUT"), (api.calls[0][0], api.calls[1][0]))
        self.assertEqual("Draft: Pin to v2", api.calls[1][3]["title"])
        ready = Api(mr(draft=False), mr())
        forge().proposal_update(ready, "acme/infra", {"number": 1, "title": "Pin to v2"})
        self.assertEqual("Pin to v2", ready.calls[1][3]["title"])
        own = Api(mr())
        forge().proposal_update(own, "acme/infra", {"number": 1, "title": "Draft: mine"})
        self.assertEqual(("PUT", "Draft: mine"), (own.calls[0][0], own.calls[0][3]["title"]))

    def test_commits_come_back_oldest_first(self):
        api = Api([
            {"id": "new", "committed_date": "2026-10-02T00:00:00Z"},
            {"id": "old", "committed_date": "2026-10-01T00:00:00Z"},
        ])
        answer = forge().proposal_commits(api, "acme/infra", {"number": 1})
        self.assertEqual(["old", "new"], [c["sha"] for c in answer["commits"]])

    def test_page_one_holds_the_oldest_commits_however_long_the_mr(self):
        # 150 commits, which GitLab serves newest first: c150..c51, then c50..c1.
        newest_first = [{"id": f"c{n}"} for n in range(150, 0, -1)]
        first = forge().proposal_commits(
            Api(newest_first[:100], newest_first[100:]), "acme/infra", {"number": 1, "limit": 100}
        )
        self.assertEqual(("c1", "c100"), (first["commits"][0]["sha"], first["commits"][-1]["sha"]))
        self.assertTrue(first["truncated"])
        second = forge().proposal_commits(
            Api(newest_first[:100], newest_first[100:]), "acme/infra", {"number": 1, "limit": 100, "page": 2}
        )
        self.assertEqual(["c101", "c150"], [second["commits"][0]["sha"], second["commits"][-1]["sha"]])
        self.assertFalse(second["truncated"])

    def test_any_note_takes_an_award_and_already_awarded_is_fine(self):
        api = Api({"id": 1})
        answer = forge().proposal_acknowledge(api, "acme/infra", {"number": 1, "comment": {"id": 9, "kind": "review_comment"}})
        self.assertTrue(answer["acknowledged"])
        self.assertEqual("projects/acme%2Finfra/merge_requests/1/notes/9/award_emoji", api.calls[0][1])
        self.assertEqual({"name": "eyes"}, api.calls[0][3])
        # GitLab answers a second award with a 404 carrying the validation error.
        again = Api(WorkspaceError("not found", status=404, detail="404 Award Emoji Name has already been taken Not Found"))
        self.assertTrue(forge().proposal_acknowledge(again, "acme/infra", {"number": 1, "comment": {"id": 9, "kind": "issue"}})["acknowledged"])
        missing = Api(WorkspaceError("not found", status=404, detail="404 Note Not Found"))
        with self.assertRaises(WorkspaceError):
            forge().proposal_acknowledge(missing, "acme/infra", {"number": 1, "comment": {"id": 9, "kind": "issue"}})
        self.assertFalse(forge().proposal_acknowledge(Api(), "acme/infra", {"number": 1, "comment": {"id": 9, "kind": "review"}})["acknowledged"])


class IssueTest(unittest.TestCase):
    def test_both_halves_of_the_filter_are_listing_parameters(self):
        api = Api([])
        forge().issue_list(api, "acme/infra", {
            "state": "open", "labels": ["a"], "excludeLabels": ["status:claimed"], "query": "drift",
        })
        params = api.calls[0][2]
        self.assertEqual("opened", params["state"])
        self.assertEqual("a", params["labels"])
        self.assertEqual("status:claimed", params["not[labels]"])
        self.assertEqual("drift", params["search"])
        self.assertEqual("projects/acme%2Finfra/issues", api.calls[0][1])

    def test_close_sends_the_state_event_and_accepts_the_shared_reasons(self):
        api = Api({"iid": 4, "state": "closed"})
        answer = forge().issue_close(api, "acme/infra", {"number": 4, "reason": "not-planned"})
        self.assertEqual({"state_event": "close"}, api.calls[0][3])
        self.assertEqual("closed", answer["issue"]["state"])
        with self.assertRaises(WorkspaceError):
            forge().issue_close(Api(), "acme/infra", {"number": 4, "reason": "duplicate"})


class LabelTest(unittest.TestCase):
    def test_a_created_label_gets_a_colour_with_its_hash(self):
        api = Api(WorkspaceError("404 Label Not Found", status=404), {"name": "x", "color": "#6699cc"})
        answer = forge().label_ensure(api, "acme/infra", {"name": "x"})
        self.assertEqual("#6699cc", api.calls[1][3]["color"])
        self.assertEqual("6699cc", answer["label"]["color"])
        api = Api(WorkspaceError("404", status=404), {"name": "x", "color": "#fbca04"})
        forge().label_ensure(api, "acme/infra", {"name": "x", "color": "fbca04"})
        self.assertEqual("#fbca04", api.calls[1][3]["color"])

    def test_a_label_name_is_encoded_in_its_path(self):
        api = Api({"name": "status:in progress", "color": "#000000"})
        forge().label_ensure(api, "acme/infra", {"name": "status:in progress"})
        self.assertEqual("projects/acme%2Finfra/labels/status%3Ain%20progress", api.calls[0][1])


class WriteAccessTest(unittest.TestCase):
    def test_developer_and_above_may_write(self):
        for level, expected in ((30, True), (40, True), (20, False)):
            with self.subTest(level=level):
                api = Api([{"id": 7, "username": "dev", "bot": False}], {"access_level": level})
                self.assertIs(expected, forge().can_write(api, "acme/infra", "dev"))
                self.assertEqual("projects/acme%2Finfra/members/all/7", api.calls[1][1])

    def test_a_stranger_is_no_and_a_failed_lookup_is_not_an_answer(self):
        person = {"id": 7, "bot": False}
        self.assertIs(False, forge().can_write(Api([]), "acme/infra", "nobody"))
        self.assertIs(False, forge().can_write(Api([person], WorkspaceError("x", status=404)), "acme/infra", "dev"))
        self.assertIsNone(forge().can_write(Api([person], WorkspaceError("x", status=502)), "acme/infra", "dev"))
        self.assertIsNone(forge().can_write(Api(WorkspaceError("x", status=502)), "acme/infra", "dev"))

    def test_an_author_the_caller_marks_as_a_bot_is_no_without_a_lookup(self):
        # Review (#2437): `bot=True` from the broker was discarded and the
        # answer re-derived with up to two user lookups.
        api = Api()
        self.assertIs(False, forge().can_write(api, "acme/infra", "project_1_bot_ab", bot=True))
        self.assertEqual([], api.calls)

    def test_an_automation_is_never_a_writer_whatever_its_name_or_role(self):
        # Review round 3: a service account named like a person, holding
        # Developer, read as a person and its comments became requests.
        # The username search may leave `bot` out (a non-admin token): then
        # the user itself is read.
        listed = Api([{"id": 7, "username": "ci-deployer", "bot": True}])
        self.assertIs(False, forge().can_write(listed, "acme/infra", "ci-deployer"))
        self.assertEqual(1, len(listed.calls))
        asked = Api([{"id": 7, "username": "ci-deployer"}], {"id": 7, "bot": True})
        self.assertIs(False, forge().can_write(asked, "acme/infra", "ci-deployer"))
        self.assertEqual("users/7", asked.calls[1][1])
        person = Api([{"id": 8, "username": "dev"}], {"id": 8, "bot": False}, {"access_level": 30})
        self.assertIs(True, forge().can_write(person, "acme/infra", "dev"))
        unread = Api([{"id": 7, "username": "x"}], WorkspaceError("x", status=502))
        self.assertIsNone(forge().can_write(unread, "acme/infra", "x"))


class TranslationTest(unittest.TestCase):
    def test_bookkeeping_rows_do_not_crowd_comments_out_of_the_limit(self):
        # Review round 4: a merge request's notes are mostly GitLab's own
        # bookkeeping, and counting those rows against `limit` reported a
        # short conversation truncated -- which the sweep refuses.
        def note(i, system):
            return {"id": i, "body": f"n{i}", "system": system, "author": {"username": "dev"}, "created_at": str(i)}

        api = Api(
            {"iid": 1, "state": "opened"},
            [note(1, True), note(2, True), note(3, False)],
            [note(4, False), note(5, False)],
        )
        answer = forge().issue_view(api, "acme/infra", {"number": 1, "comments": True, "limit": 3})
        self.assertEqual([3, 4, 5], [c["id"] for c in answer["comments"]])
        self.assertFalse(answer["commentsTruncated"])
        self.assertEqual(2, api.calls[2][2]["page"])

    def test_bookkeeping_notes_are_not_comments_and_diff_notes_are_review_comments(self):
        api = Api({"iid": 1, "state": "opened"}, [
            {"id": 1, "body": "please fix", "system": False, "author": {"username": "dev"}, "created_at": "1"},
            {"id": 2, "body": "changed the description", "system": True, "author": {"username": "dev"}, "created_at": "2"},
            {"id": 3, "body": "this line", "system": False, "author": {"username": "dev"}, "created_at": "3",
             "position": {"new_path": "a.yaml", "new_line": 4}},
        ])
        answer = forge().issue_view(api, "acme/infra", {"number": 1, "comments": True})
        self.assertEqual([("issue", 1), ("review_comment", 3)], [(c["kind"], c["id"]) for c in answer["comments"]])
        self.assertEqual(("a.yaml", 4), (answer["comments"][1]["path"], answer["comments"][1]["line"]))
        self.assertEqual("issue-1", answer["comments"][0]["ref"])

    def test_a_token_bot_user_is_recognised_as_automation(self):
        # Through a verb, as every caller sees it: the boundary test keeps the
        # forge's modules behind the package surface.
        notes = [
            {"id": i, "body": "x", "system": False, "author": author, "created_at": str(i)}
            for i, author in enumerate([
                {"username": "project_1001_bot_3f2a"},
                {"username": "group_42_bot"},
                {"username": "project_1001_bot2"},  # an older instance's numbering
                {"username": "x", "bot": True},
                # Review round 2: service accounts and GitLab's own bots.
                {"username": "service_account_group_42_a1b2"},
                {"username": "Support-Bot"},
                {"username": "GitLab-Security-Bot"},
                # Review round 3: names a person can also choose are people
                # here; an automation behind one is refused by the write check.
                {"username": "kube-agents-eval-bot"},
                {"username": "duo-arch"},
                {"username": "service_account_fan"},
                {"username": "ghost"},
            ])
        ]
        answer = forge().issue_view(Api({"iid": 1, "state": "opened"}, notes), "acme/infra", {"number": 1, "comments": True})
        self.assertEqual([True] * 7 + [False] * 4, [c["bot"] for c in answer["comments"]])

    def test_states_and_iids(self):
        cases = (
            ({"state": "opened", "iid": 3, "id": 999}, "open", 3, ""),
            ({"state": "locked", "iid": 3, "id": 999}, "open", 3, ""),  # being merged
            ({"state": "closed", "iid": 3, "closed_at": "2026-10-02"}, "closed", 3, "2026-10-02"),
            ({"state": "merged", "iid": 3, "merged_at": "2026-10-01", "closed_at": None}, "merged", 3, "2026-10-01"),
        )
        for node, state, number, closed in cases:
            with self.subTest(state=node["state"]):
                proposal = forge().proposal_view(Api(node), "acme/infra", {"number": 3})["proposal"]
                self.assertEqual((state, number, closed), (proposal["state"], proposal["number"], proposal["closed"]))
        issue = forge().issue_view(Api({"state": "opened", "iid": 2}), "acme/infra", {"number": 2})["issue"]
        self.assertEqual("open", issue["state"])


class ReachTest(unittest.TestCase):
    def test_reach_lists_every_project_the_tokens_account_belongs_to(self):
        page = [{"path_with_namespace": f"acme/p{i}"} for i in range(100)]
        api = Api(page, [{"path_with_namespace": "other/x"}])
        paths, cut_short = forge().reach(api)
        self.assertEqual(101, len(paths))
        self.assertFalse(cut_short)
        self.assertEqual({"membership": "true", "simple": "true", "per_page": 100, "page": 1}, api.calls[0][2])
        self.assertEqual("projects", api.calls[0][1])

    def test_reach_says_when_it_stopped_short(self):
        page = [{"path_with_namespace": "acme/p"}] * 100
        api = Api(*([page] * GitLabForge.REACH_PAGES))
        _, cut_short = forge().reach(api)
        self.assertTrue(cut_short)


class ErrorsTest(unittest.TestCase):
    def test_a_refused_token_names_the_secret(self):
        error = providers.forge_error(401, "401 Unauthorized", forge().error_overrides)
        self.assertEqual("FORGE_UNAUTHENTICATED", error.fields["code"])
        self.assertIn("credentialsRef", str(error))

    def test_a_validation_failure_is_a_bad_argument_not_a_retry(self):
        # Recorded live: a merge request from a branch that was never pushed.
        error = providers.forge_error(
            400, "source_branch: does not exist", forge().error_overrides
        )
        self.assertEqual("FORGE_REJECTED", error.fields["code"])


class RegistryTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def configure(self, forges):
        path = self.dir / "forges.json"
        path.write_text(json.dumps({"forges": forges}))
        os.environ[registry_module.FORGES_CONFIG_ENV] = str(path)

    def test_an_unconfigured_gitlab_com_is_a_named_gap_not_a_forge(self):
        os.environ.pop(registry_module.FORGES_CONFIG_ENV, None)
        forge_, _ = providers.Registry().resolve("https://gitlab.com/acme/infra")
        self.assertEqual("gitlab", forge_.name)
        with self.assertRaises(ForgeUnsupported) as caught:
            forge_.clone_url("acme/infra")
        self.assertIn("no credential is configured for gitlab.com", str(caught.exception))

    def test_a_configured_self_managed_gitlab_leaves_gitlab_com_a_gap(self):
        self.configure([{"provider": "gitlab", "host": "gitlab.example.com", "tokenPath": "/t", "allowedPaths": []}])
        registry = providers.Registry()
        self.assertEqual(1, len(registry.forges))
        stub, _ = registry.resolve("https://gitlab.com/acme/infra")
        with self.assertRaises(ForgeUnsupported):
            stub.clone_url("acme/infra")

    def test_github_and_gitlab_resolve_by_host_and_a_bare_name_by_neither(self):
        self.configure([
            {"provider": "github", "host": "github.com"},
            {"provider": "gitlab", "host": "gitlab.example.com", "tokenPath": "/t", "allowedPaths": []},
        ])
        registry = providers.Registry()
        self.assertEqual("gitlab", registry.resolve("https://gitlab.example.com/acme/infra")[0].name)
        self.assertEqual("gitlab", registry.resolve("gitlab.example.com/acme/sub/infra")[0].name)
        self.assertEqual("github", registry.resolve("https://github.com/acme/infra")[0].name)
        with self.assertRaises(ForgeUnsupported):
            registry.resolve("acme/infra")

    def test_a_gitlab_only_install_keeps_the_bare_name(self):
        self.configure([{"provider": "gitlab", "host": "gitlab.com", "tokenPath": "/t", "allowedPaths": []}])
        forge_, repo = providers.Registry().resolve("acme/infra")
        self.assertEqual(("gitlab", "acme/infra"), (forge_.name, repo))


if __name__ == "__main__":
    unittest.main()
