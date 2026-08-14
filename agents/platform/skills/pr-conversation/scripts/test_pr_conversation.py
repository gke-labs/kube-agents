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

    def list_commit_shas(self, repo, pr):
        if isinstance(self.commits, Exception):
            raise self.commits
        return list(self.commits)

    def post_comment(self, repo, pr, body_file):
        if self.post_error:
            raise self.post_error
        with open(body_file, "r", encoding="utf-8") as handle:
            self.posted.append((pr.number, handle.read()))


#: The commits on the fake pull request, tip last.
COMMITS = ["0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b", HEAD_SHA]


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
    created_at="2026-08-12T10:00:00Z",
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
        target = (
            mock.Mock(side_effect=repo_error)
            if repo_error
            else mock.Mock(return_value=repo)
        )
        buf = StringIO()
        with mock.patch.object(forge, "target_repo", target), \
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
    def _reply(self, provider, body="Bumped it to 4.", command="reply"):
        path = self.scratch_file("reply.md", body)
        return self.run_helper(
            [command, "--pr", "12", "--comment-id", "IC_1", "--body-file", path]
            + (["--no-change"] if command == "reply" else []),
            provider,
        )

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
        rc, _out = self._reply(provider, "--verify-commit", COMMITS[0])
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


if __name__ == "__main__":
    unittest.main()
