#!/usr/bin/env python3
"""Tests for pr_conversation.py, the pr-conversation skill's helper.

Two properties carry the weight:

* **`reply` always stamps the marker.** The marker is the entire idempotency
  scheme, and the failure mode of a missing one is not a missing comment — it is
  the same request being answered every ten minutes forever. So the helper
  appends it from `--comment-id` rather than trusting the model to type it.
* **A reply body cannot come from outside the scratch directory.** The body is
  posted publicly, so the path is confined by `realpath` rather than merely
  checked for existence — the same rule `resolver.handle_transition` applies to
  its report file, and for the same reason.

Driven by a fake provider, like `test_github_scan_gate.py`: what is under test
is the helper's contract, not `gh`'s argv, which `test_forge.py` pins.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_SHARED = _HERE.parents[2] / "scripts"  # agents/platform/scripts
sys.path.insert(0, str(_SHARED))

import forge  # noqa: E402
import pr_triggers  # noqa: E402


def _load_helper():
    spec = importlib.util.spec_from_file_location(
        "pr_conversation_under_test", _HERE / "pr_conversation.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = _load_helper()

SELF = "kube-agents-bot"
REPO = "acme/toolkit"
HEAD_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


class FakeProvider:
    supports_acknowledge = True

    def __init__(
        self, prs=None, comments=None, post_error=None, viewer=SELF, commits=None
    ):
        self.prs = prs or []
        self.comments = comments or {}
        self.post_error = post_error
        self._viewer = viewer
        self.commits = COMMITS if commits is None else commits
        self.posted = []

    def preflight(self):
        pass

    def viewer_login(self):
        return self._viewer

    def list_open_prs(self, repo):
        return list(self.prs)

    def list_comments(self, repo, pr):
        return list(self.comments.get(pr.number, []))

    def list_commits(self, repo, pr):
        if isinstance(self.commits, Exception):
            raise self.commits
        return list(self.commits)

    def post_comment(self, repo, pr, body):
        if self.post_error:
            raise self.post_error
        self.posted.append((pr.number, body))


#: When the default request in these tests was made. Every commit below lands
#: after it, so a test that is not about the recency bound does not trip it.
REQUESTED_AT = "2026-08-12T10:00:00Z"

#: The commits on the fake pull request, tip last.
COMMITS = [
    forge.Commit("0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b", "2026-08-12T10:30:00Z"),
    forge.Commit(HEAD_SHA, "2026-08-12T11:00:00Z"),
]


def make_pr(
    number=12,
    head_ref="platform-agent/x",
    labels=(),
    author=f"{SELF}[bot]",
    head_repo=REPO,
    head_sha=HEAD_SHA,
):
    return forge.PullRequest(
        number=number,
        head_ref=head_ref,
        author=author,
        labels=labels,
        head_repo=head_repo,
        head_sha=head_sha,
    )


def make_comment(
    node_id,
    body,
    author="reviewer",
    can_write=True,
    created_at=REQUESTED_AT,
    kind="issue",
    path="",
    line=None,
    can_write_known=True,
):
    return forge.Comment(
        node_id=node_id,
        numeric_id=1,
        author=author,
        body=body,
        can_write=can_write,
        created_at=created_at,
        kind=kind,
        path=path,
        line=line,
        can_write_known=can_write_known,
    )


class _Harness(unittest.TestCase):
    """Runs the helper against a fake provider and a scratch directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.scratch = os.path.join(self._tmp.name, "scratch")
        os.makedirs(self.scratch)
        patch = mock.patch.object(helper, "SCRATCH_DIR", self.scratch)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def scratch_file(self, name, content):
        path = os.path.join(self.scratch, name)
        Path(path).write_text(content, encoding="utf-8")
        return path

    def run_helper(self, argv, provider, repo=REPO, repo_error=None):
        if argv and argv[0] in ("reply", "refuse") and "--repo" not in argv:
            argv = [argv[0], "--repo", repo or REPO] + list(argv[1:])
        managed_mock = (
            mock.Mock(side_effect=repo_error)
            if repo_error
            else mock.Mock(return_value=[repo] if repo else [])
        )
        buf = StringIO()
        with mock.patch("gitops_workspace.get_managed_github_repos", managed_mock), \
             mock.patch.object(forge, "provider_for", return_value=provider), \
             redirect_stdout(buf):
            rc = helper.main(argv)
        return rc, buf.getvalue()


class PollTest(_Harness):
    def test_no_repo_configured(self):
        _rc, out = self.run_helper(["poll"], FakeProvider(), repo=None)
        self.assertEqual(json.loads(out)["status"], "NOT_CONFIGURED")

    def test_no_requests(self):
        provider = FakeProvider(
            prs=[make_pr()], comments={12: [make_comment("IC_1", "looks good")]}
        )
        _rc, out = self.run_helper(["poll"], provider)
        self.assertEqual(json.loads(out)["status"], "NO_REQUESTS")

    def test_a_trigger_is_reported_with_its_context(self):
        provider = FakeProvider(
            prs=[make_pr()], comments={12: [make_comment("IC_1", "/agent bump to 4")]}
        )
        _rc, out = self.run_helper(["poll"], provider)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "FOUND")
        row = payload["requests"][0]
        self.assertEqual(row["comment_id"], "IC_1")
        self.assertEqual(row["request"], "bump to 4")
        self.assertEqual(row["kind"], "slash")
        self.assertEqual(row["head_ref"], "platform-agent/x")
        self.assertTrue(row["can_write"])

    def test_poll_across_multiple_repositories(self):
        """poll returns requests across all managed repositories."""
        pr1 = make_pr(1, head_ref="platform-agent/fix-1", head_repo="acme/repo1")
        pr2 = make_pr(2, head_ref="platform-agent/fix-2", head_repo="acme/repo2")

        class MultiRepoFakeProvider(FakeProvider):
            def list_open_prs(self, repo):
                if repo == "acme/repo1":
                    return [pr1]
                elif repo == "acme/repo2":
                    return [pr2]
                return []

            def list_comments(self, repo, pr):
                if repo == "acme/repo1" and pr.number == 1:
                    return [make_comment("IC_1", "/agent fix repo1")]
                elif repo == "acme/repo2" and pr.number == 2:
                    return [make_comment("IC_2", "/agent fix repo2")]
                return []

        provider = MultiRepoFakeProvider()
        managed_mock = mock.Mock(return_value=["acme/repo1", "acme/repo2"])
        with mock.patch("gitops_workspace.get_managed_github_repos", managed_mock), \
             mock.patch.object(forge, "provider_for", return_value=provider), \
             redirect_stdout(StringIO()) as buf:
            helper.main(["poll"])
            out = buf.getvalue()
        payload = json.loads(out)
        self.assertEqual(payload["status"], "FOUND")
        self.assertEqual(len(payload["requests"]), 2)
        repos_in_conversations = [conv["repo"] for conv in payload["conversations"]]
        self.assertIn("acme/repo1", repos_in_conversations)
        self.assertIn("acme/repo2", repos_in_conversations)

    def test_an_untrusted_request_is_reported_rather_than_hidden(self):
        """The worker is told so it can refuse, not left looking like it missed it."""
        provider = FakeProvider(
            prs=[make_pr()],
            comments={12: [make_comment("IC_1", "/agent x", can_write=False)]},
        )
        _rc, out = self.run_helper(["poll"], provider)
        self.assertFalse(json.loads(out)["requests"][0]["can_write"])

    def test_an_answered_request_is_not_reported(self):
        provider = FakeProvider(
            prs=[make_pr()],
            comments={
                12: [
                    make_comment("IC_1", "/agent x"),
                    make_comment(
                        "IC_9", pr_triggers.marker("IC_1"), author=f"{SELF}[bot]"
                    ),
                ]
            },
        )
        _rc, out = self.run_helper(["poll"], provider)
        self.assertEqual(json.loads(out)["status"], "NO_REQUESTS")

    def test_pr_filter_narrows_the_scan(self):
        provider = FakeProvider(
            prs=[make_pr(12), make_pr(13)],
            comments={
                12: [make_comment("IC_1", "/agent a")],
                13: [make_comment("IC_2", "/agent b")],
            },
        )
        _rc, out = self.run_helper(["poll", "--pr", "13"], provider)
        rows = json.loads(out)["requests"]
        self.assertEqual([r["comment_id"] for r in rows], ["IC_2"])

    def test_a_pr_the_agent_did_not_author_is_out_of_scope(self):
        provider = FakeProvider(
            prs=[make_pr(head_ref="feat/human")],
            comments={12: [make_comment("IC_1", "/agent x")]},
        )
        _rc, out = self.run_helper(["poll"], provider)
        self.assertEqual(json.loads(out)["status"], "NO_REQUESTS")

    def test_a_forge_fault_reports_its_reason_code(self):
        class Broken(FakeProvider):
            def list_open_prs(self, repo):
                raise forge.ForgeError("REPO_UNREACHABLE", "HTTP 404")

        _rc, out = self.run_helper(["poll"], Broken())
        payload = json.loads(out)
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["reason"], "REPO_UNREACHABLE")

    def test_an_unparseable_repo_reports_its_reason_code(self):
        _rc, out = self.run_helper(
            ["poll"], FakeProvider(), repo_error=forge.RepoUnparseable("evil.com/a/b")
        )
        self.assertEqual(json.loads(out)["reason"], "GIT_REPO_UNPARSEABLE")


class ConversationContextTest(_Harness):
    """The thread that travels with the requests.

    Being addressed is what wakes the agent; it is not the whole of what it has
    to read. These pin that the untagged half of a review discussion reaches the
    worker, that it arrives marked well enough to be weighed rather than obeyed,
    and that when a cap bites the payload says so.
    """

    def poll_threads(self, provider, argv=("poll",)):
        _rc, out = self.run_helper(list(argv), provider)
        return json.loads(out)

    def test_untagged_comments_travel_with_the_request(self):
        provider = FakeProvider(
            prs=[make_pr()],
            comments={
                12: [
                    make_comment(
                        "IC_1", "2 feels low for prod", created_at="2026-08-12T09:00:00Z"
                    ),
                    make_comment(
                        "IC_2",
                        "agreed, but this is dev",
                        author="other",
                        created_at="2026-08-12T09:30:00Z",
                    ),
                    make_comment(
                        "IC_3", "/agent why 2?", created_at="2026-08-12T10:00:00Z"
                    ),
                ]
            },
        )
        payload = self.poll_threads(provider)
        thread = payload["conversations"][0]
        self.assertEqual(thread["pr"], 12)
        self.assertEqual(
            [row["comment_id"] for row in thread["comments"]],
            ["IC_1", "IC_2", "IC_3"],
        )
        self.assertEqual(
            [row["is_request"] for row in thread["comments"]], [False, False, True]
        )
        self.assertEqual(thread["comments"][0]["body"], "2 feels low for prod")

    def test_comments_arrive_oldest_first_whatever_order_the_provider_gave(self):
        provider = FakeProvider(
            prs=[make_pr()],
            comments={
                12: [
                    make_comment(
                        "IC_late", "/agent x", created_at="2026-08-12T12:00:00Z"
                    ),
                    make_comment(
                        "IC_early", "some context", created_at="2026-08-12T08:00:00Z"
                    ),
                ]
            },
        )
        thread = self.poll_threads(provider)["conversations"][0]
        self.assertEqual(
            [row["comment_id"] for row in thread["comments"]], ["IC_early", "IC_late"]
        )

    def test_the_agents_own_earlier_answers_are_in_the_thread(self):
        """So it can build on them rather than repeat or contradict one."""
        provider = FakeProvider(
            prs=[make_pr()],
            comments={
                12: [
                    make_comment(
                        "IC_1",
                        f"I chose 2 for cost.\n\n{pr_triggers.marker('IC_0')}",
                        author=f"{SELF}[bot]",
                        created_at="2026-08-12T09:00:00Z",
                    ),
                    make_comment("IC_2", "/agent and for prod?"),
                ]
            },
        )
        thread = self.poll_threads(provider)["conversations"][0]
        mine = thread["comments"][0]
        self.assertTrue(mine["is_self"])
        self.assertFalse(thread["comments"][1]["is_self"])
        # The marker is bookkeeping, and prompting the model with the syntax
        # invites it to write one into prose that `reply` then stamps again.
        self.assertEqual(mine["body"], "I chose 2 for cost.")

    def test_a_read_only_authors_comment_is_context_and_says_so(self):
        provider = FakeProvider(
            prs=[make_pr()],
            comments={
                12: [
                    make_comment(
                        "IC_1",
                        "the image tag looks stale",
                        author="stranger",
                        can_write=False,
                        created_at="2026-08-12T09:00:00Z",
                    ),
                    make_comment("IC_2", "/agent is it?"),
                ]
            },
        )
        thread = self.poll_threads(provider)["conversations"][0]
        self.assertFalse(thread["comments"][0]["can_write"])
        self.assertFalse(thread["comments"][0]["is_request"])
        self.assertTrue(thread["comments"][1]["can_write"])

    def test_an_inline_review_comment_keeps_the_line_it_hangs_off(self):
        provider = FakeProvider(
            prs=[make_pr()],
            comments={
                12: [
                    make_comment(
                        "RC_1",
                        "this replica count",
                        kind="review_comment",
                        path="clusters/dev/echo.yaml",
                        line=7,
                        created_at="2026-08-12T09:00:00Z",
                    ),
                    make_comment("IC_2", "/agent explain"),
                ]
            },
        )
        row = self.poll_threads(provider)["conversations"][0]["comments"][0]
        self.assertEqual(row["kind"], "review_comment")
        self.assertEqual(row["path"], "clusters/dev/echo.yaml")
        self.assertEqual(row["line"], 7)

    def test_a_long_comment_is_cut_and_reports_how_much(self):
        body = "x" * (helper.CONTEXT_MAX_BODY_CHARS + 250)
        provider = FakeProvider(
            prs=[make_pr()],
            comments={
                12: [
                    make_comment("IC_1", body, created_at="2026-08-12T09:00:00Z"),
                    make_comment("IC_2", "/agent thoughts?"),
                ]
            },
        )
        row = self.poll_threads(provider)["conversations"][0]["comments"][0]
        self.assertEqual(len(row["body"]), helper.CONTEXT_MAX_BODY_CHARS)
        self.assertEqual(row["truncated_chars"], 250)

    def test_a_short_comment_is_not_marked_truncated(self):
        provider = FakeProvider(
            prs=[make_pr()], comments={12: [make_comment("IC_1", "/agent x")]}
        )
        row = self.poll_threads(provider)["conversations"][0]["comments"][0]
        self.assertNotIn("truncated_chars", row)

    def test_a_long_request_is_truncated_and_reports_truncated_chars(self):
        long_req = "x" * (helper.CONTEXT_MAX_REQUEST_CHARS + 300)
        provider = FakeProvider(
            prs=[make_pr()], comments={12: [make_comment("IC_1", f"/agent {long_req}")]}
        )
        data = self.poll_threads(provider)
        req_row = data["requests"][0]
        self.assertEqual(len(req_row["request"]), helper.CONTEXT_MAX_REQUEST_CHARS)
        self.assertEqual(req_row["truncated_chars"], 300)

    def test_a_short_request_is_not_marked_truncated(self):
        provider = FakeProvider(
            prs=[make_pr()], comments={12: [make_comment("IC_1", "/agent please fix tests")]}
        )
        data = self.poll_threads(provider)
        req_row = data["requests"][0]
        self.assertEqual(req_row["request"], "please fix tests")
        self.assertNotIn("truncated_chars", req_row)

    def test_requests_are_capped_to_context_max_requests(self):
        comments = [
            make_comment(
                f"IC_{n:03d}", f"/agent task {n}", created_at=f"2026-08-12T{n // 60:02d}:{n % 60:02d}:00Z"
            )
            for n in range(helper.CONTEXT_MAX_REQUESTS + 15)
        ]
        provider = FakeProvider(prs=[make_pr()], comments={12: comments})
        err = StringIO()
        with redirect_stderr(err):
            data = self.poll_threads(provider)
        self.assertEqual(len(data["requests"]), helper.CONTEXT_MAX_REQUESTS)
        self.assertEqual(data["requests"][0]["comment_id"], "IC_000")
        self.assertEqual(data["requests"][-1]["comment_id"], f"IC_{helper.CONTEXT_MAX_REQUESTS - 1:03d}")
        self.assertEqual(data["conversations"][0]["omitted_requests"], 15)
        self.assertIn("15 request(s) deferred", err.getvalue())

    def test_trusted_requests_are_prioritized_over_untrusted_before_refusal_budget_exhausted(self):
        # 14 untrusted requests followed by 1 trusted maintainer request
        comments = [
            make_comment(
                f"IC_UNTRUSTED_{n:02d}",
                f"/agent untrusted {n}",
                author="stranger",
                can_write=False,
                can_write_known=True,
                created_at=f"2026-08-12T00:{n:02d}:00Z",
            )
            for n in range(14)
        ]
        comments.append(
            make_comment(
                "IC_MAINTAINER",
                "/agent maintainer task",
                author="maintainer",
                can_write=True,
                can_write_known=True,
                created_at="2026-08-12T00:30:00Z",
            )
        )
        provider = FakeProvider(prs=[make_pr()], comments={12: comments})
        err = StringIO()
        with redirect_stderr(err):
            data = self.poll_threads(provider)
        self.assertEqual(len(data["requests"]), helper.CONTEXT_MAX_REQUESTS)
        # Maintainer request is prioritized at the head of requests
        self.assertEqual(data["requests"][0]["comment_id"], "IC_MAINTAINER")
        self.assertTrue(data["requests"][0]["can_write"])
        # Thread reports omitted_requests
        self.assertEqual(data["conversations"][0]["omitted_requests"], 5)
        # Comments in conversations all have is_request=True
        conv_comments = {c["comment_id"]: c for c in data["conversations"][0]["comments"]}
        self.assertTrue(conv_comments["IC_MAINTAINER"]["is_request"])
        for n in range(14):
            if f"IC_UNTRUSTED_{n:02d}" in conv_comments:
                self.assertTrue(conv_comments[f"IC_UNTRUSTED_{n:02d}"]["is_request"])
        self.assertIn("5 request(s) deferred", err.getvalue())

    def test_a_long_thread_keeps_the_recent_end_and_counts_what_it_dropped(self):
        comments = [
            make_comment(
                f"IC_{n:03d}", f"turn {n}", created_at=f"2026-08-12T{n // 60:02d}:{n % 60:02d}:00Z"
            )
            for n in range(helper.CONTEXT_MAX_COMMENTS + 5)
        ]
        comments.append(make_comment("IC_TRIG", "/agent x", created_at="2026-08-13T00:00:00Z"))
        provider = FakeProvider(prs=[make_pr()], comments={12: comments})
        thread = self.poll_threads(provider)["conversations"][0]
        self.assertEqual(len(thread["comments"]), helper.CONTEXT_MAX_COMMENTS)
        self.assertEqual(thread["omitted_earlier"], 6)
        # The trigger being answered is at the recent end, which is why the cap
        # drops the oldest here and the oldest-first rule applies to triggers.
        self.assertEqual(thread["comments"][-1]["comment_id"], "IC_TRIG")

    def test_the_request_being_answered_survives_the_cap(self):
        """The two rules point opposite ways, and the request loses without this.

        The sweep hands over the *oldest* unanswered trigger; the cap drops the
        *oldest* comments. On a thread past the cap that is the same comment.
        A bare `@mention` carries no request text in the card either, so the
        worker would be asked to answer words it has nowhere to read.
        """
        comments = [make_comment("IC_TRIG", "/agent x", created_at="2026-08-11T00:00:00Z")]
        comments += [
            make_comment(
                f"IC_{n:03d}", f"turn {n}", created_at=f"2026-08-12T{n // 60:02d}:{n % 60:02d}:00Z"
            )
            for n in range(helper.CONTEXT_MAX_COMMENTS + 5)
        ]
        provider = FakeProvider(prs=[make_pr()], comments={12: comments})
        thread = self.poll_threads(provider)["conversations"][0]
        ids = [row["comment_id"] for row in thread["comments"]]
        self.assertEqual(ids[0], "IC_TRIG")
        self.assertTrue(thread["comments"][0]["is_request"])
        # Pinned on top of the window, and only the unpinned remainder is lost.
        self.assertEqual(len(ids), helper.CONTEXT_MAX_COMMENTS + 1)
        self.assertEqual(thread["omitted_earlier"], 5)

    def test_a_thread_within_the_cap_says_nothing_about_omissions(self):
        provider = FakeProvider(
            prs=[make_pr()], comments={12: [make_comment("IC_1", "/agent x")]}
        )
        self.assertNotIn("omitted_earlier", self.poll_threads(provider)["conversations"][0])

    def test_only_pull_requests_with_a_request_carry_a_thread(self):
        provider = FakeProvider(
            prs=[make_pr(12), make_pr(13)],
            comments={
                12: [make_comment("IC_1", "just chatting")],
                13: [make_comment("IC_2", "/agent x")],
            },
        )
        payload = self.poll_threads(provider)
        self.assertEqual([t["pr"] for t in payload["conversations"]], [13])

    def test_nothing_waiting_carries_no_transcript(self):
        provider = FakeProvider(
            prs=[make_pr()], comments={12: [make_comment("IC_1", "looks good")]}
        )
        payload = self.poll_threads(provider)
        self.assertEqual(payload["status"], "NO_REQUESTS")
        self.assertNotIn("conversations", payload)


def answerable(prs=None, request="/agent bump to 4", **kwargs):
    """A provider with one unanswered request, ``IC_1``, on pull request 12.

    Every `reply` and `refuse` path needs one: the helper checks
    `--comment-id` against the requests the forge reports as unanswered, so a
    fixture with no comments on it is a pull request with nothing to answer.
    """
    return FakeProvider(
        prs=prs if prs is not None else [make_pr()],
        comments={12: [make_comment("IC_1", request)]},
        **kwargs,
    )


class ReplyTest(_Harness):
    def _reply(self, provider, body="Bumped it to 4.", command="reply", comment_id="IC_1"):
        path = self.scratch_file("reply.md", body)
        return self.run_helper(
            [command, "--pr", "12", "--comment-id", comment_id, "--body-file", path]
            + (["--no-change"] if command == "reply" else []),
            provider,
        )

    def test_reply_can_answer_a_request_past_the_poll_cap(self):
        comments = [
            make_comment(
                f"IC_{n:03d}", f"/agent task {n}", created_at=f"2026-08-12T{n // 60:02d}:{n % 60:02d}:00Z"
            )
            for n in range(helper.CONTEXT_MAX_REQUESTS + 5)
        ]
        provider = FakeProvider(prs=[make_pr()], comments={12: comments})
        target_id = f"IC_{helper.CONTEXT_MAX_REQUESTS + 2:03d}"
        self._reply(provider, comment_id=target_id)
        self.assertEqual(len(provider.posted), 1)
        self.assertIn(f"<!-- agent-answered:{target_id} -->", provider.posted[0][1])

    def test_the_marker_is_appended_by_the_helper(self):
        """The model cannot forget it, because the model does not write it."""
        provider = answerable()
        self._reply(provider)
        _number, posted = provider.posted[0]
        self.assertIn("Bumped it to 4.", posted)
        self.assertIn("<!-- agent-answered:IC_1 -->", posted)

    def test_the_posted_marker_reads_back_as_handled(self):
        """Round-trip: what `reply` writes is what the sweep's scan looks for."""
        provider = answerable()
        self._reply(provider)
        _number, posted = provider.posted[0]
        self.assertEqual(
            pr_triggers.handled_node_ids(
                [make_comment("IC_9", posted, author=f"{SELF}[bot]")], SELF
            ),
            {"IC_1"},
        )

    def test_refuse_uses_the_refusal_marker(self):
        provider = answerable()
        self._reply(provider, body="Not in scope.", command="refuse")
        self.assertIn("<!-- agent-refused:IC_1 -->", provider.posted[0][1])

    def test_the_success_line_is_machine_readable(self):
        provider = answerable()
        _rc, out = self._reply(provider)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "POSTED")
        self.assertEqual(payload["comment_id"], "IC_1")

    def test_a_body_outside_scratch_is_rejected(self):
        provider = answerable()
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
            handle.write("elsewhere")
            outside = handle.name
        self.addCleanup(os.unlink, outside)
        with self.assertRaises(SystemExit):
            self.run_helper(
                ["reply", "--pr", "12", "--comment-id", "IC_1",
                 "--body-file", outside, "--no-change"],
                provider,
            )
        self.assertEqual(provider.posted, [])

    def test_a_symlink_out_of_scratch_is_rejected(self):
        """`realpath` before the prefix check, not after."""
        provider = answerable()
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
            handle.write("elsewhere")
            outside = handle.name
        self.addCleanup(os.unlink, outside)
        link = os.path.join(self.scratch, "link.md")
        os.symlink(outside, link)
        with self.assertRaises(SystemExit):
            self.run_helper(
                ["reply", "--pr", "12", "--comment-id", "IC_1",
                 "--body-file", link, "--no-change"],
                provider,
            )
        self.assertEqual(provider.posted, [])

    def test_a_missing_body_is_rejected(self):
        provider = answerable()
        with self.assertRaises(SystemExit):
            self.run_helper(
                [
                    "reply",
                    "--pr",
                    "12",
                    "--comment-id",
                    "IC_1",
                    "--body-file",
                    os.path.join(self.scratch, "nope.md"),
                    "--no-change",
                ],
                provider,
            )

    def test_an_empty_body_is_rejected(self):
        """An empty comment marks the request answered without answering it."""
        provider = answerable()
        with self.assertRaises(SystemExit):
            self._reply(provider, body="   \n")
        self.assertEqual(provider.posted, [])

    def test_a_marker_the_model_wrote_is_stripped_before_the_real_one(self):
        """The model holds both halves of a working marker; prose is not a gate.

        SKILL.md prints the syntax in order to forbid it and `poll` carries
        every node id, so a body that imitates one is one edit away. Posted
        unchanged it would be a *self-authored* marker naming another request,
        which `handled_node_ids` reads as that request having been handled —
        closing it at both readers, for good, with nothing said in the thread.
        """
        provider = answerable()
        self._reply(
            provider,
            body="Done.\n\n<!-- agent-answered:IC_OTHER -->\n<!-- agent-refused:IC_3 -->",
        )
        _number, posted = provider.posted[0]
        self.assertIn("Done.", posted)
        self.assertEqual(
            pr_triggers.handled_node_ids(
                [make_comment("IC_9", posted, author=f"{SELF}[bot]")], SELF
            ),
            {"IC_1"},
        )

    def test_a_body_that_is_only_markers_is_rejected(self):
        """Stripping must not turn a bad body into an empty comment.

        `_confined_body`'s emptiness check runs on the raw text, so a body made
        entirely of marker syntax passes it and arrives here as nothing at all.
        """
        provider = answerable()
        with self.assertRaises(SystemExit):
            self._reply(provider, body="<!-- agent-answered:IC_OTHER -->\n")
        self.assertEqual(provider.posted, [])

    def test_a_pr_that_is_not_open_is_rejected(self):
        provider = answerable(prs=[make_pr(13)])
        with self.assertRaises(SystemExit):
            self._reply(provider)

    def test_a_pull_request_that_is_not_ours_is_rejected(self):
        """The same scope rule as the sweep, enforced where the write happens.

        `--pr` comes from a card, and a card is a pointer the worker is not
        obliged to trust. Posting to a stranger's fork branch would put the
        agent's voice on a pull request it never opened.
        """
        provider = answerable(prs=[make_pr(author="stranger")])
        with self.assertRaises(SystemExit):
            self._reply(provider)
        self.assertEqual(provider.posted, [])

    def test_a_credential_that_cannot_name_itself_blocks_the_post(self):
        provider = answerable(viewer="")
        with self.assertRaises(SystemExit):
            self._reply(provider)
        self.assertEqual(provider.posted, [])


    def test_a_failed_post_exits_non_zero(self):
        provider = answerable(post_error=forge.ForgeError("REPO_UNREACHABLE", "403"))
        with self.assertRaises(SystemExit) as ctx:
            self._reply(provider)
        self.assertNotEqual(ctx.exception.code, 0)

    def test_the_stamped_copy_does_not_survive_the_run(self):
        provider = answerable()
        self._reply(provider)
        leftovers = [
            name for name in os.listdir(self.scratch) if name != "reply.md"
        ]
        self.assertEqual(leftovers, [])


class CommentIdValidationTest(_Harness):
    """`--comment-id` is checked against the forge, not trusted.

    A wrong id posts a real, visible answer stamped with a marker that closes
    nothing: `handled_node_ids` keeps returning the request, so the sweep files
    the card again on the next tick and the agent answers the same comment
    every ten minutes. Failing before the post is the only place that loop can
    be cut, because after it the comment is already public.
    """

    def _post(self, provider, comment_id, command="reply"):
        path = self.scratch_file("reply.md", "Bumped it to 4.")
        return self.run_helper(
            [command, "--pr", "12", "--comment-id", comment_id, "--body-file", path]
            + (["--no-change"] if command == "reply" else []),
            provider,
        )

    def test_an_id_that_is_not_a_pending_request_is_rejected(self):
        provider = answerable()
        with self.assertRaises(SystemExit):
            self._post(provider, "IC_TYPO")
        self.assertEqual(provider.posted, [])

    def test_the_numeric_id_is_not_the_node_id(self):
        """The likeliest slip: both are on the row, only one closes the loop."""
        provider = answerable()
        with self.assertRaises(SystemExit):
            self._post(provider, "1")
        self.assertEqual(provider.posted, [])

    def test_an_already_answered_request_cannot_be_answered_twice(self):
        provider = FakeProvider(
            prs=[make_pr()],
            comments={
                12: [
                    make_comment("IC_1", "/agent x"),
                    make_comment(
                        "IC_9", pr_triggers.marker("IC_1"), author=f"{SELF}[bot]"
                    ),
                ]
            },
        )
        with self.assertRaises(SystemExit):
            self._post(provider, "IC_1")
        self.assertEqual(provider.posted, [])

    def test_an_untrusted_request_can_still_be_refused(self):
        """Refusing is the answer to one, so it must stay reachable."""
        provider = FakeProvider(
            prs=[make_pr()],
            comments={12: [make_comment("IC_1", "/agent x", can_write=False)]},
        )
        self._post(provider, "IC_1", command="refuse")
        self.assertIn("<!-- agent-refused:IC_1 -->", provider.posted[0][1])

    def test_a_bot_request_the_sweep_passed_over_is_not_answerable(self):
        """The worker and the sweep must agree on who may address the agent.

        They read the same allowlist through `pr_triggers`. If the worker were
        laxer, a card filed for one comment would license answering an
        unrelated bot on the same pull request.
        """
        provider = FakeProvider(
            prs=[make_pr()],
            comments={12: [make_comment("IC_1", "/agent x", author="dependabot[bot]")]},
        )
        with self.assertRaises(SystemExit):
            self._post(provider, "IC_1")
        self.assertEqual(provider.posted, [])

    def test_an_allowlisted_bot_is_answerable(self):
        provider = FakeProvider(
            prs=[make_pr()],
            comments={12: [make_comment("IC_1", "/agent x", author="ci-bot[bot]")]},
        )
        with mock.patch.dict(
            "os.environ", {pr_triggers.BOT_ALLOWLIST_ENV: "ci-bot"}, clear=False
        ):
            self._post(provider, "IC_1")
        self.assertIn("<!-- agent-answered:IC_1 -->", provider.posted[0][1])

    def test_the_error_names_what_is_actually_pending(self):
        """So the model's next attempt is a corrected id, not another guess."""
        provider = answerable()
        err = StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            self._post(provider, "IC_TYPO")
        self.assertIn("IC_1", err.getvalue())

    def test_a_pr_with_nothing_pending_says_so_rather_than_listing_nothing(self):
        provider = FakeProvider(
            prs=[make_pr()], comments={12: [make_comment("IC_1", "looks good")]}
        )
        err = StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            self._post(provider, "IC_1")
        self.assertIn("no unanswered requests", err.getvalue())


class ClaimVerificationTest(_Harness):
    """A reply may not claim a commit the branch does not have.

    Observed live: a worker whose amend was blocked replied "I have updated the
    Redis deployment … to 512Mi and the replica count to 2", stamped
    `agent-answered`, and left the branch on its original commit with `256Mi`
    and one replica. The marker means no later sweep re-opens it, so the false
    claim is the thread's final word. Checking the sha is the part of that a
    script can settle.
    """

    def _reply(self, provider, *claim):
        path = self.scratch_file("reply.md", "Bumped it to 4 in a1b2c3d.")
        return self.run_helper(
            ["reply", "--pr", "12", "--comment-id", "IC_1", "--body-file", path]
            + list(claim),
            provider,
        )

    def test_a_real_commit_is_accepted(self):
        provider = answerable()
        rc, out = self._reply(provider, "--verify-commit", HEAD_SHA)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["status"], "POSTED")
        self.assertEqual(len(provider.posted), 1)

    def test_an_abbreviated_sha_is_accepted(self):
        """Git's own abbreviation, and what a commit message quotes."""
        provider = answerable()
        rc, _out = self._reply(provider, "--verify-commit", HEAD_SHA[:8])
        self.assertEqual(rc, 0)
        self.assertEqual(len(provider.posted), 1)

    def test_an_earlier_commit_on_the_pr_is_accepted(self):
        """An amend that made two commits: the one written about is not the tip."""
        provider = answerable()
        rc, _out = self._reply(provider, "--verify-commit", COMMITS[0].sha)
        self.assertEqual(rc, 0)
        self.assertEqual(len(provider.posted), 1)

    def test_a_commit_that_is_not_on_the_pr_posts_nothing(self):
        provider = answerable()
        err = StringIO()
        with self.assertRaises(SystemExit), redirect_stderr(err):
            self._reply(provider, "--verify-commit", "deadbeefdeadbeef")
        self.assertEqual(provider.posted, [])
        self.assertIn("is not a commit", err.getvalue())

    def test_the_failure_names_the_branch_tip(self):
        """So the model can tell "wrong sha" from "the amend never landed"."""
        provider = answerable()
        err = StringIO()
        with self.assertRaises(SystemExit), redirect_stderr(err):
            self._reply(provider, "--verify-commit", "deadbeefdeadbeef")
        self.assertIn(HEAD_SHA, err.getvalue())

    def test_a_sha_too_short_to_identify_anything_is_rejected(self):
        """`a1b` matches the head here and would match anything anywhere."""
        provider = answerable()
        err = StringIO()
        with self.assertRaises(SystemExit), redirect_stderr(err):
            self._reply(provider, "--verify-commit", "a1b")
        self.assertEqual(provider.posted, [])
        self.assertIn("too short", err.getvalue())

    def test_an_unreadable_commit_list_is_not_a_pass(self):
        """Unverifiable is not verified — the claim carries a closing marker."""
        provider = answerable(commits=forge.ForgeError("REPO_UNREACHABLE", "#12"))
        err = StringIO()
        with self.assertRaises(SystemExit), redirect_stderr(err):
            self._reply(provider, "--verify-commit", HEAD_SHA)
        self.assertEqual(provider.posted, [])
        self.assertIn("could not read the commits", err.getvalue())

    def test_no_change_asks_the_forge_nothing(self):
        """An answer that changed nothing has no claim to check."""
        provider = answerable(commits=forge.ForgeError("REPO_UNREACHABLE", "#12"))
        rc, _out = self._reply(provider, "--no-change")
        self.assertEqual(rc, 0)
        self.assertEqual(len(provider.posted), 1)

    def test_a_refusal_is_not_asked_to_declare_one(self):
        """A refusal never claims a change, so the flag is not on `refuse`."""
        provider = answerable(request="/agent delete prod")
        path = self.scratch_file("reply.md", "No.")
        rc, _out = self.run_helper(
            ["refuse", "--pr", "12", "--comment-id", "IC_1", "--body-file", path],
            provider,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(provider.posted), 1)

    def test_the_declaration_is_required(self):
        """Neither flag is silence, and silence is how the false claim got out."""
        provider = answerable()
        with self.assertRaises(SystemExit):
            self._reply(provider)
        self.assertEqual(provider.posted, [])

    def test_an_empty_sha_is_not_a_declaration_of_no_change(self):
        """The one malformed sha that could fail in the unsafe direction.

        `--verify-commit "$SHA"` with `SHA` unset, or fed a `--jq` that matched
        nothing, is `--verify-commit ""`. Every other bad value is rejected
        loudly; this one has to be as well, rather than reading as "nothing
        changed" and posting the claim unchecked.
        """
        provider = answerable()
        err = StringIO()
        with self.assertRaises(SystemExit), redirect_stderr(err):
            self._reply(provider, "--verify-commit", "")
        self.assertEqual(provider.posted, [])
        self.assertIn("empty", err.getvalue())

    def test_a_commit_that_predates_the_request_is_not_an_answer_to_it(self):
        """Membership alone is satisfied by the commit that opened the branch.

        Every commit the agent ever pushed is on the pull request, so a model
        that changed nothing can clear the sha check by naming its own earlier
        work — and the reply carries the marker that closes the request for
        good. The claim is about work done *for this request*, so the commit
        has to postdate it.
        """
        provider = answerable(
            commits=[forge.Commit(HEAD_SHA, "2026-08-12T09:00:00Z")],  # request: 10:00
        )
        err = StringIO()
        with self.assertRaises(SystemExit), redirect_stderr(err):
            self._reply(provider, "--verify-commit", HEAD_SHA)
        self.assertEqual(provider.posted, [])
        self.assertIn("before the request", err.getvalue())

    def test_a_commit_dated_after_the_request_is_accepted(self):
        """The other half of the bound: real work must still post."""
        provider = answerable(commits=[forge.Commit(HEAD_SHA, "2026-08-12T10:00:01Z")])
        rc, _out = self._reply(provider, "--verify-commit", HEAD_SHA)
        self.assertEqual(rc, 0)
        self.assertEqual(len(provider.posted), 1)

    def test_a_commit_with_no_date_is_not_a_pass(self):
        """Unverifiable is not verified, the same as an unreadable listing."""
        provider = answerable(commits=[forge.Commit(HEAD_SHA, "")])
        err = StringIO()
        with self.assertRaises(SystemExit), redirect_stderr(err):
            self._reply(provider, "--verify-commit", HEAD_SHA)
        self.assertEqual(provider.posted, [])
        self.assertIn("did not report a date", err.getvalue())

    def test_an_unreadable_request_time_still_posts_and_says_so(self):
        """The membership check ran; only the recency bound was skipped.

        Failing closed here would wedge the request instead — it is handed back
        every ten minutes until something posts. So the reply goes out and the
        gap is on stderr rather than guessed at.
        """
        provider = FakeProvider(
            prs=[make_pr()],
            comments={12: [make_comment("IC_1", "/agent x", created_at="")]},
            commits=[forge.Commit(HEAD_SHA, "2026-08-12T09:00:00Z")],
        )
        err = StringIO()
        with redirect_stderr(err):
            rc, _out = self._reply(provider, "--verify-commit", HEAD_SHA)
        self.assertEqual(rc, 0)
        self.assertEqual(len(provider.posted), 1)
        self.assertIn("recency", err.getvalue())


class TrustGateTest(_Harness):
    """The permission the SKILL.md asks for, enforced where it is spent.

    `poll` hands the worker every comment on the pull request including the
    untrusted ones, so the argument for answering anyway arrives in the same
    prompt as the row forbidding it. These pin the decision in code.
    """

    def _post(self, provider, command, *extra):
        path = self.scratch_file("reply.md", "Body.")
        return self.run_helper(
            [command, "--pr", "12", "--comment-id", "IC_1", "--body-file", path]
            + list(extra),
            provider,
        )

    def _provider(self, **comment_kwargs):
        return FakeProvider(
            prs=[make_pr()],
            comments={12: [make_comment("IC_1", "/agent bump to 4", **comment_kwargs)]},
        )

    def test_a_reply_to_an_account_without_write_access_posts_nothing(self):
        provider = self._provider(can_write=False, author="drive-by")
        err = StringIO()
        with self.assertRaises(SystemExit), redirect_stderr(err):
            self._post(provider, "reply", "--no-change")
        self.assertEqual(provider.posted, [])
        self.assertIn("does not have write access", err.getvalue())

    def test_an_unresolved_permission_is_not_permission(self):
        """`can_write` is False on an indeterminate lookup, but the reason
        differs and so does the advice: this one waits for the next sweep."""
        provider = self._provider(can_write=False, can_write_known=False)
        err = StringIO()
        with self.assertRaises(SystemExit), redirect_stderr(err):
            self._post(provider, "reply", "--no-change")
        self.assertEqual(provider.posted, [])
        self.assertIn("could not be determined", err.getvalue())

    def test_a_trusted_request_still_answers(self):
        rc, _out = self._post(self._provider(), "reply", "--no-change")
        self.assertEqual(rc, 0)

    def test_an_untrusted_request_can_still_be_refused(self):
        """Refusal closes a loop, so `can_write` alone never gates it.

        Step 2 of the SKILL.md sends it here for exactly this case; blocking it
        would leave the request handed back every ten minutes forever.
        """
        provider = self._provider(can_write=False, author="drive-by")
        rc, _out = self._post(provider, "refuse")
        self.assertEqual(rc, 0)
        self.assertEqual(len(provider.posted), 1)

    def test_an_unresolved_permission_cannot_be_refused_either(self):
        """The one mistake on this path that cannot be taken back.

        `agent-refused` closes the request for good, so refusing on a lookup
        that failed silences a maintainer permanently on the strength of a proxy
        timeout. The sweep holds these rather than refusing them and says why;
        this file held the opposite, because the early return for a refusal sat
        above the check. The next sweep re-reads it once the lookup works.
        """
        provider = self._provider(can_write=False, can_write_known=False)
        err = StringIO()
        with self.assertRaises(SystemExit), redirect_stderr(err):
            self._post(provider, "refuse")
        self.assertEqual(provider.posted, [])
        self.assertIn("could not be determined", err.getvalue())

    def _spent_budget(self, budget=2, **request_kwargs):
        """A pull request whose refusal budget the sweep has already spent."""
        kwargs = {"author": "drive-by", "can_write": False}
        kwargs.update(request_kwargs)
        already = [
            make_comment(
                f"IC_old{n}",
                f"Refused.\n\n{pr_triggers.marker('IC_them%d' % n, pr_triggers.REFUSED_MARKER)}",
                author=SELF,
            )
            for n in range(budget)
        ]
        return FakeProvider(
            prs=[make_pr()],
            comments={12: already + [make_comment("IC_1", "/agent bump to 4", **kwargs)]},
        )

    def test_a_refusal_past_the_budget_posts_nothing(self):
        """One budget, not one per caller.

        The sweep stops refusing at `PR_AGENT_MAX_REFUSALS_PER_PR` so that a
        hundred comments from an account with no write access cannot become a
        hundred public comments from the agent. It drops those without a marker,
        so they stay unanswered and `poll` kept handing them back — and the next
        card any trusted reviewer filed had the worker post them after all.
        """
        provider = self._spent_budget()
        err = StringIO()
        with (
            mock.patch.dict(os.environ, {pr_triggers.MAX_REFUSALS_ENV: "2"}),
            self.assertRaises(SystemExit),
            redirect_stderr(err),
        ):
            self._post(provider, "refuse")
        self.assertEqual(provider.posted, [])
        self.assertIn("budget", err.getvalue())

    def test_the_budget_does_not_gate_refusing_a_maintainer(self):
        """A bound on one thing does not become a bound on another.

        Past the budget the agent stops answering accounts that can comment
        without limit. A reviewer with write access asking for something out of
        scope is still owed the reasoned "no" the SKILL.md promises, and
        refusing them is not the amplification the bound exists to stop.
        """
        provider = self._spent_budget(author="maintainer", can_write=True)
        with mock.patch.dict(os.environ, {pr_triggers.MAX_REFUSALS_ENV: "2"}):
            rc, _out = self._post(provider, "refuse")
        self.assertEqual(rc, 0)
        self.assertEqual(len(provider.posted), 1)

    def test_poll_stops_offering_untrusted_requests_past_the_budget(self):
        """`_check_trust` is the enforcement; this keeps it out of the prompt.

        A row the worker is handed and then forbidden to act on is a turn spent
        on a failed command, and an invitation to argue with the gate.
        """
        provider = self._spent_budget()
        err = StringIO()
        with (
            mock.patch.dict(os.environ, {pr_triggers.MAX_REFUSALS_ENV: "2"}),
            redirect_stderr(err),
        ):
            _rc, out = self.run_helper(["poll", "--pr", "12"], provider)
        self.assertEqual(json.loads(out)["status"], "NO_REQUESTS")
        self.assertIn("refusal budget is spent", err.getvalue())

    def test_an_unresolved_lookup_is_still_offered_past_the_budget(self):
        """Held is not refused, and the budget is about accounts, not faults.

        Dropping these would hide a maintainer's request behind a proxy wobble,
        on a pull request some stranger had already spent the budget on.
        """
        provider = self._spent_budget(can_write=False, can_write_known=False)
        with mock.patch.dict(os.environ, {pr_triggers.MAX_REFUSALS_ENV: "2"}):
            _rc, out = self.run_helper(["poll", "--pr", "12"], provider)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "FOUND")
        self.assertEqual([row["comment_id"] for row in payload["requests"]], ["IC_1"])

    def test_poll_past_refusal_budget_leaves_buried_untrusted_requests_unflagged_in_transcript(self):
        """Once refusal budget is spent, dropped untrusted requests must not show is_request: True."""
        already = [
            make_comment(
                f"IC_old{n}",
                f"Refused.\n\n{pr_triggers.marker('IC_them%d' % n, pr_triggers.REFUSED_MARKER)}",
                author=SELF,
            )
            for n in range(2)
        ]
        untrusted = make_comment("IC_UNTRUSTED", "/agent spam request", author="stranger", can_write=False, can_write_known=True)
        trusted = make_comment("IC_MAINTAINER", "/agent maintainer task", author="maintainer", can_write=True, can_write_known=True)
        provider = FakeProvider(
            prs=[make_pr()],
            comments={12: already + [untrusted, trusted]},
        )
        err = StringIO()
        with (
            mock.patch.dict(os.environ, {pr_triggers.MAX_REFUSALS_ENV: "2"}),
            redirect_stderr(err),
        ):
            _rc, out = self.run_helper(["poll", "--pr", "12"], provider)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "FOUND")
        self.assertEqual([row["comment_id"] for row in payload["requests"]], ["IC_MAINTAINER"])
        conv_comments = {c["comment_id"]: c for c in payload["conversations"][0]["comments"]}
        self.assertTrue(conv_comments["IC_MAINTAINER"]["is_request"])
        self.assertFalse(conv_comments["IC_UNTRUSTED"]["is_request"])
        self.assertIn("1 untrusted request(s) not offered", err.getvalue())

    def test_a_trusted_out_of_scope_request_can_be_refused(self):
        """The other reason to refuse: a maintainer asking for something the
        agent will not do. Gating refusal on `can_write` would block it."""
        provider = self._provider()
        rc, _out = self._post(provider, "refuse")
        self.assertEqual(rc, 0)
        self.assertEqual(len(provider.posted), 1)

    def test_an_ignored_pull_request_is_not_posted_to(self):
        """`agent:ignore` is how a maintainer says stop posting here.

        The sweep honours it, but a card filed before the label went on still
        runs after it, and a hand-run never consulted it at all — so the label
        has to hold at the point the comment is actually written.
        """
        provider = FakeProvider(
            prs=[make_pr(labels=(forge.IGNORE_LABEL,))],
            comments={12: [make_comment("IC_1", "/agent bump to 4")]},
        )
        err = StringIO()
        with self.assertRaises(SystemExit), redirect_stderr(err):
            self._post(provider, "reply", "--no-change")
        self.assertEqual(provider.posted, [])
        self.assertIn(forge.IGNORE_LABEL, err.getvalue())


class RepoValidationTest(_Harness):
    def test_invalid_repo_format_rejected(self):
        provider = answerable()
        err = StringIO()
        with self.assertRaises(SystemExit), redirect_stderr(err):
            self.run_helper(["reply", "--repo", "../invalid", "--pr", "12", "--comment-id", "IC_1", "--body-file", self.scratch_file("r.md", "body"), "--no-change"], provider)
        self.assertIn("Invalid repository format", err.getvalue())

    def test_unmanaged_repo_rejected(self):
        provider = answerable()
        err = StringIO()
        with self.assertRaises(SystemExit), redirect_stderr(err):
            self.run_helper(["reply", "--repo", "unmanaged/repo", "--pr", "12", "--comment-id", "IC_1", "--body-file", self.scratch_file("r.md", "body"), "--no-change"], provider, repo="managed/repo")
        self.assertIn("not in the managed repositories list", err.getvalue())

    def test_poll_unmanaged_repo_returns_error(self):
        provider = answerable()
        _rc, out = self.run_helper(["poll", "--repo", "unmanaged/repo"], provider, repo="managed/repo")
        payload = json.loads(out)
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["reason"], "INVALID_REPOSITORY")
        self.assertIn("not in the managed repositories list", payload["value"])


if __name__ == "__main__":
    unittest.main()
