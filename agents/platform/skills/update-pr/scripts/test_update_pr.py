#!/usr/bin/env python3
"""Tests for update_pr.py, the update-pr skill's helper.

Three properties carry the weight, and all three are about the loop
terminating rather than about the comment being pretty:

* **`record` always stamps the marker, and always with a real sha.** The marker
  is what stops the sweep handing the same head commit over every ten minutes,
  and this agent pushes commits — so a missing or mistyped one is not a missing
  comment, it is an unbounded fix loop on a pull request nobody asked it to
  touch.
* **`record` will not post a claim it cannot check.** `--pushed` has to name a
  commit that landed after the tip the run started from, because every commit
  the agent ever made is on the branch and membership alone would pass for the
  one that opened the pull request.
* **`poll` reports the two stopping conditions, not just the health reads.**
  "This tip was already worked" and "this pull request's budget is spent" are
  invisible in the conflict flag and the check list, and a worker that has to
  derive them will not.

Driven by a fake provider, like `test_pr_conversation.py`: what is under test
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
import pr_skill  # noqa: E402
import pr_triggers  # noqa: E402


def _load_helper():
    spec = importlib.util.spec_from_file_location(
        "update_pr_under_test", _HERE / "update_pr.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = _load_helper()

SELF = "kube-agents-bot"
REPO = "acme/toolkit"
BASE_SHA = "0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b"
HEAD_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
FIX_SHA = "cc11cc22cc33cc44cc55cc66cc77cc88cc99cc00"

#: The commits on the fake pull request, tip last, as `list_commits` returns
#: them.
COMMITS = [
    forge.Commit(BASE_SHA, "2026-08-12T10:30:00Z"),
    forge.Commit(HEAD_SHA, "2026-08-12T11:00:00Z"),
]

#: The same branch after a run pushed a fix on top of the tip it started from.
COMMITS_AFTER_FIX = COMMITS + [forge.Commit(FIX_SHA, "2026-08-12T11:30:00Z")]


def make_pr(
    number=12,
    head_ref="platform-agent/x",
    base_ref="main",
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
        base_ref=base_ref,
    )


def make_comment(node_id, body, author=SELF):
    return forge.Comment(
        node_id=node_id,
        numeric_id=1,
        author=author,
        body=body,
        can_write=True,
        created_at="2026-08-12T10:00:00Z",
        kind="issue",
    )


def updated_marker(sha, author=SELF):
    """A comment recording one update attempt against `sha`."""
    return make_comment(
        f"IC_{sha[:8]}",
        f"Tried and failed.\n\n{pr_triggers.marker(sha, pr_triggers.UPDATED_MARKER)}",
        author=author,
    )


def check(
    name="unit",
    conclusion="failure",
    register="check_run",
    details_url="https://ci/1",
):
    return forge.CheckRun(
        name=name, conclusion=conclusion, details_url=details_url, register=register
    )


class FakeProvider:
    def __init__(
        self,
        prs=None,
        comments=None,
        conflicted=False,
        failing=None,
        commits=None,
        viewer=SELF,
        post_error=None,
        read_error=None,
    ):
        self.prs = prs if prs is not None else [make_pr()]
        self.comments = comments or {}
        self.conflicted = conflicted
        self.failing = failing or []
        self.commits = COMMITS if commits is None else commits
        self._viewer = viewer
        self.post_error = post_error
        self.read_error = read_error
        self.posted = []

    def preflight(self):
        pass

    def viewer_login(self):
        return self._viewer

    def list_open_prs(self, repo):
        if self.read_error:
            raise self.read_error
        return list(self.prs)

    def list_comments(self, repo, pr):
        return list(self.comments.get(pr.number, []))

    def list_commits(self, repo, pr):
        return list(self.commits)

    def conflict_state(self, repo, pr):
        return self.conflicted

    def failing_checks(self, repo, pr):
        return list(self.failing)

    def post_comment(self, repo, pr, body_file):
        if self.post_error:
            raise self.post_error
        with open(body_file, "r", encoding="utf-8") as handle:
            self.posted.append((pr.number, handle.read()))


class _Harness(unittest.TestCase):
    """Runs the helper against a fake provider and a scratch directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.scratch = os.path.join(self._tmp.name, "scratch")
        os.makedirs(self.scratch)
        patch = mock.patch.object(pr_skill, "SCRATCH_DIR", self.scratch)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def scratch_file(self, name="body.md", content="Merged `main`; CI is green."):
        path = os.path.join(self.scratch, name)
        Path(path).write_text(content, encoding="utf-8")
        return path

    def run_helper(
        self, argv, provider, repo=REPO, env=None, repo_error=None, repos=None
    ):
        """Drive `main` and return (rc, stdout, stderr, provider).

        `record` takes `--repo` since the watcher went multi-repo, so it is
        supplied here when a case has not spelled one itself — the cases that
        care about repository resolution pass their own and are unaffected.
        """
        if argv and argv[0] == "record" and "--repo" not in argv:
            argv = [argv[0], "--repo", repo or REPO] + list(argv[1:])
        managed_mock = (
            mock.Mock(side_effect=repo_error)
            if repo_error
            else mock.Mock(
                return_value=list(repos)
                if repos is not None
                else ([repo] if repo else [])
            )
        )
        out, err = StringIO(), StringIO()
        with mock.patch(
            "gitops_workspace.get_managed_github_repos", managed_mock
        ), mock.patch.object(
            forge, "provider_for", return_value=provider
        ), mock.patch.dict(os.environ, env or {}, clear=False):
            with redirect_stdout(out), redirect_stderr(err):
                try:
                    rc = helper.main(argv)
                except SystemExit as exit_:
                    rc = exit_.code
        return rc, out.getvalue(), err.getvalue()

    def poll(self, provider, argv=("poll", "--pr", "12"), env=None):
        rc, out, _ = self.run_helper(list(argv), provider, env=env)
        self.assertEqual(rc, 0)
        return json.loads(out)


class PollTest(_Harness):
    def test_a_clean_pull_request_is_healthy(self):
        payload = self.poll(FakeProvider())
        self.assertEqual(payload["status"], "NO_WORK")
        self.assertEqual(payload["pull_requests"][0]["status"], "HEALTHY")

    def test_a_conflicted_pull_request_is_work(self):
        payload = self.poll(FakeProvider(conflicted=True))
        row = payload["pull_requests"][0]
        self.assertEqual(payload["status"], "FOUND")
        self.assertEqual(row["status"], "FOUND")
        self.assertTrue(row["conflicted"])
        self.assertEqual(row["base_ref"], "main")
        self.assertEqual(row["head_ref"], "platform-agent/x")

    def test_a_red_check_is_work(self):
        payload = self.poll(FakeProvider(failing=[check(name="lint")]))
        row = payload["pull_requests"][0]
        self.assertEqual(row["status"], "FOUND")
        self.assertEqual(
            row["failing_checks"],
            [
                {
                    "name": "lint",
                    "name_truncated_chars": 0,
                    "conclusion": "failure",
                    "details_url": "https://ci/1",
                    "register": "check_run",
                }
            ],
        )
        self.assertEqual(row["failing_checks_total"], 1)
        self.assertEqual(row["failing_checks_omitted"], 0)

    def test_third_party_check_text_is_capped_and_the_cut_reported(self):
        """Name and URL both come from whoever posted the check.

        Anything holding `checks:write` on the repository can post one, so both
        are third-party text on its way into a prompt. A silent cut reads as a
        short name, which is why the count travels with it.
        """
        payload = self.poll(
            FakeProvider(
                failing=[
                    check(
                        name="n" * (helper.MAX_CHECK_NAME_CHARS + 40),
                        details_url="https://ci/"
                        + "u" * (helper.MAX_CHECK_URL_CHARS + 100),
                    )
                ]
            )
        )
        row = payload["pull_requests"][0]["failing_checks"][0]
        self.assertEqual(len(row["name"]), helper.MAX_CHECK_NAME_CHARS)
        self.assertEqual(row["name_truncated_chars"], 40)
        self.assertEqual(len(row["details_url"]), helper.MAX_CHECK_URL_CHARS)

    def test_a_long_list_of_red_checks_is_capped_and_counted(self):
        extra = 5
        payload = self.poll(
            FakeProvider(
                failing=[
                    check(name=f"check-{n}")
                    for n in range(helper.MAX_CHECKS_IN_ROW + extra)
                ]
            )
        )
        row = payload["pull_requests"][0]
        self.assertEqual(len(row["failing_checks"]), helper.MAX_CHECKS_IN_ROW)
        self.assertEqual(
            row["failing_checks_total"], helper.MAX_CHECKS_IN_ROW + extra
        )
        self.assertEqual(row["failing_checks_omitted"], extra)

    def test_a_pull_request_with_no_head_sha_is_unreadable(self):
        """The sweep filters these out; a hand-run `poll` is the path that
        does not have it in front, and `FOUND` here would push commits the
        run could never record."""
        payload = self.poll(
            FakeProvider(prs=[make_pr(head_sha="")], conflicted=True)
        )
        self.assertEqual(payload["pull_requests"][0]["status"], "UNREADABLE")

    def test_an_uncomputed_merge_with_nothing_red_is_indeterminate(self):
        """`None` is "the forge has not finished", which is not "clean".

        Reported as its own status rather than folded into `HEALTHY`, because
        the two lead the worker to opposite conclusions: one says the branch is
        fine and the card can be completed, the other says come back next tick.
        """
        payload = self.poll(FakeProvider(conflicted=None))
        self.assertEqual(payload["pull_requests"][0]["status"], "INDETERMINATE")

    def test_a_head_already_worked_is_not_offered_again(self):
        provider = FakeProvider(
            conflicted=True, comments={12: [updated_marker(HEAD_SHA)]}
        )
        row = self.poll(provider)["pull_requests"][0]
        self.assertEqual(row["status"], "ALREADY_ATTEMPTED")
        self.assertEqual(row["attempts_used"], 1)

    def test_a_marker_for_another_head_does_not_block_this_one(self):
        provider = FakeProvider(
            conflicted=True, comments={12: [updated_marker(BASE_SHA)]}
        )
        row = self.poll(provider)["pull_requests"][0]
        self.assertEqual(row["status"], "FOUND")
        self.assertEqual(row["attempts_used"], 1)

    def test_a_marker_somebody_else_wrote_is_not_the_agents_attempt(self):
        """Markers are only read back off the agent's own comments.

        Otherwise anyone with a keyboard could stop the agent maintaining its
        own pull request by pasting the syntax into a comment — or, worse, spend
        its whole budget in one post.
        """
        provider = FakeProvider(
            conflicted=True,
            comments={12: [updated_marker(HEAD_SHA, author="passer-by")]},
        )
        row = self.poll(provider)["pull_requests"][0]
        self.assertEqual(row["status"], "FOUND")
        self.assertEqual(row["attempts_used"], 0)

    def test_the_budget_stops_the_loop_once_the_head_keeps_moving(self):
        """The per-tip marker alone would never bind on a branch being pushed to.

        Each failed fix mints a fresh tip, so the tip test passes every time.
        The count is what terminates it.
        """
        provider = FakeProvider(
            conflicted=True,
            comments={12: [updated_marker(f"{n}" * 40) for n in range(2)]},
        )
        row = self.poll(provider, env={pr_triggers.MAX_UPDATE_ATTEMPTS_ENV: "2"})
        self.assertEqual(row["pull_requests"][0]["status"], "BUDGET_SPENT")
        self.assertEqual(row["pull_requests"][0]["attempts_allowed"], 2)

    def test_a_pull_request_the_agent_does_not_own_is_not_found(self):
        provider = FakeProvider(prs=[make_pr(author="someone-else")], conflicted=True)
        payload = self.poll(provider)
        self.assertEqual(payload["status"], "NOT_FOUND")

    def test_an_ignored_pull_request_is_not_found(self):
        provider = FakeProvider(
            prs=[make_pr(labels=(forge.IGNORE_LABEL,))], conflicted=True
        )
        self.assertEqual(self.poll(provider)["status"], "NOT_FOUND")

    def test_polling_every_pull_request_needs_no_number(self):
        provider = FakeProvider(
            prs=[make_pr(number=12), make_pr(number=13)], conflicted=True
        )
        payload = self.poll(provider, argv=("poll",))
        self.assertEqual([row["pr"] for row in payload["pull_requests"]], [12, 13])

    def test_a_forge_error_is_a_reason_code_not_a_traceback(self):
        provider = FakeProvider(read_error=forge.ForgeError("REPO_UNREACHABLE", "403"))
        payload = self.poll(provider)
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["reason"], "REPO_UNREACHABLE")

    def test_no_target_repository_is_not_configured(self):
        rc, out, _ = self.run_helper(["poll"], FakeProvider(), repo="")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["status"], "NOT_CONFIGURED")


class RecordTest(_Harness):
    def record(self, provider, extra=("--no-change",), sha=HEAD_SHA, body=None):
        argv = [
            "record",
            "--pr",
            "12",
            "--attempted-sha",
            sha,
            "--body-file",
            body or self.scratch_file(),
            *extra,
        ]
        return self.run_helper(argv, provider)

    def test_the_marker_is_appended_from_the_resolved_sha(self):
        provider = FakeProvider()
        rc, out, _ = self.record(provider)
        self.assertEqual(rc, 0)
        _, body = provider.posted[0]
        self.assertIn(
            pr_triggers.marker(HEAD_SHA, pr_triggers.UPDATED_MARKER), body
        )
        self.assertEqual(json.loads(out)["attempted_sha"], HEAD_SHA)

    def test_an_abbreviated_sha_still_marks_the_full_one(self):
        """The sweep compares by exact string equality against `head_sha`.

        A seven-character marker matches nothing there, so the abbreviation is
        resolved against the branch before it is written rather than after.
        """
        provider = FakeProvider()
        self.record(provider, sha=HEAD_SHA[:8])
        _, body = provider.posted[0]
        self.assertIn(f"agent-updated:{HEAD_SHA}", body)

    def test_a_sha_that_is_not_on_the_branch_posts_nothing(self):
        provider = FakeProvider()
        rc, _, err = self.record(provider, sha="f" * 40)
        self.assertEqual(rc, 1)
        self.assertEqual(provider.posted, [])
        self.assertIn("not a commit on this pull request", err)

    def test_a_sha_too_short_to_identify_a_commit_posts_nothing(self):
        provider = FakeProvider()
        rc, _, err = self.record(provider, sha="a1b")
        self.assertEqual(rc, 1)
        self.assertEqual(provider.posted, [])
        self.assertIn("shorter than", err)

    def test_recording_the_same_head_twice_posts_nothing(self):
        provider = FakeProvider(comments={12: [updated_marker(HEAD_SHA)]})
        rc, _, err = self.record(provider)
        self.assertEqual(rc, 1)
        self.assertEqual(provider.posted, [])
        self.assertIn("already recorded", err)

    def test_a_pushed_commit_after_the_starting_tip_is_accepted(self):
        provider = FakeProvider(commits=COMMITS_AFTER_FIX)
        rc, out, _ = self.record(provider, extra=("--pushed", FIX_SHA))
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["pushed"], [FIX_SHA])

    def test_a_pushed_commit_that_predates_the_run_is_refused(self):
        """Membership on the branch is not evidence the run made the commit.

        Every commit the agent ever pushed is on this branch, including the one
        that opened the pull request — so a claim naming an old one would pass a
        membership test while the run had changed nothing.

        The claim is refused, but the branch is ahead of the attempted tip, so
        the marker goes on anyway: see `test_a_refusal_after_the_branch_moved`.
        """
        provider = FakeProvider(commits=COMMITS_AFTER_FIX)
        rc, _, err = self.record(provider, extra=("--pushed", BASE_SHA))
        self.assertEqual(rc, 1)
        self.assertIn("not newer than the tip", err)
        # The marker names the tip the run started from, never the sha the
        # rejected claim named — that one is quoted in the prose as the reason.
        self.assertIn(f"<!-- agent-updated:{HEAD_SHA} -->", provider.posted[0][1])
        self.assertNotIn(f"agent-updated:{BASE_SHA}", provider.posted[0][1])

    def test_one_commit_per_stage_can_be_claimed(self):
        provider = FakeProvider(
            commits=COMMITS_AFTER_FIX + [forge.Commit("d" * 40, "2026-08-12T12:00:00Z")]
        )
        rc, out, _ = self.record(
            provider, extra=("--pushed", FIX_SHA, "--pushed", "d" * 40)
        )
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["pushed"], [FIX_SHA, "d" * 40])

    def test_an_attempted_sha_behind_the_starting_tip_posts_nothing(self):
        """The mistake SKILL.md warns against twice, made unrepresentable.

        Naming an older commit on the branch — the one that opened the pull
        request, say — writes a marker the sweep never matches against
        `head_sha`, so the per-tip bound does not bind at all and only the total
        budget stops the loop: five worker turns within the hour instead of five
        over the branch's life.
        """
        provider = FakeProvider(commits=COMMITS_AFTER_FIX)
        rc, _, err = self.record(
            provider, sha=BASE_SHA, extra=("--pushed", FIX_SHA)
        )
        self.assertEqual(rc, 1)
        self.assertIn("is not the tip this run started from", err)
        self.assertIn(HEAD_SHA[: helper.SHA_MIN_LEN], err)

    def test_an_undeclared_commit_after_the_tip_is_refused(self):
        """A forgotten `--pushed` fails here rather than in the thread.

        The invariant is that every commit after `--attempted-sha` is one the
        run is claiming; a stage whose commit went unnamed breaks it.
        """
        provider = FakeProvider(
            commits=COMMITS_AFTER_FIX + [forge.Commit("d" * 40, "2026-08-12T12:00:00Z")]
        )
        rc, _, err = self.record(provider, extra=("--pushed", FIX_SHA))
        self.assertEqual(rc, 1)
        self.assertIn("dddddd", err)

    def test_no_change_requires_the_attempted_sha_to_be_the_tip(self):
        """`--no-change` claims nothing, so nothing may have landed after it."""
        provider = FakeProvider(commits=COMMITS_AFTER_FIX)
        rc, _, err = self.record(provider)
        self.assertEqual(rc, 1)
        self.assertIn("is not the tip this run started from", err)

    def test_a_refusal_after_the_branch_moved_still_marks_the_tip(self):
        """The bound is counted off markers, so a pushed branch owes one.

        Every refusal below the `--attempted-sha` resolution can fire on a run
        whose commits are already on the branch. Exiting there without posting
        would leave the tip moved and the thread untouched: the new head is not
        in the marker set, the set has not grown, and `_update_card`'s key
        carries the head sha, so it mints again on the next tick rather than
        waiting out its hour. Neither bound binds and the sweep re-cards
        forever — which also silences `pr_comments`, since `pr_updates` claims
        what it cards.
        """
        provider = FakeProvider(commits=COMMITS_AFTER_FIX)
        rc, _, _ = self.record(provider)
        self.assertEqual(rc, 1)
        self.assertEqual(len(provider.posted), 1)
        body = provider.posted[0][1]
        self.assertIn(f"<!-- agent-updated:{HEAD_SHA} -->", body)
        self.assertIn("could not record the attempt", body)
        # Named so a human reading the thread knows the branch is ahead of the
        # commit that was marked, rather than that the run pushed nothing.
        self.assertIn(HEAD_SHA[: helper.SHA_MIN_LEN], body)

    def test_the_marker_a_refusal_writes_closes_a_second_attempt(self):
        """One marker per tip, whether the run recorded or was refused.

        Otherwise a model that reads the error and re-runs `record` with fixed
        arguments spends a second attempt on the same tip.
        """
        provider = FakeProvider(commits=COMMITS_AFTER_FIX)
        self.record(provider)
        provider.comments[12] = [make_comment("IC_refusal", provider.posted[0][1])]
        rc, _, err = self.record(provider, extra=("--pushed", FIX_SHA))
        self.assertEqual(rc, 1)
        self.assertIn("already recorded", err)
        self.assertEqual(len(provider.posted), 1)

    def test_a_refusal_on_an_unmoved_branch_posts_nothing(self):
        """No push, no marker — the case `updated_head_shas` protects.

        A run that changed nothing and was then refused has to stay retryable,
        or one bad argument parks a pull request for good with nothing said.
        """
        provider = FakeProvider()
        rc, _, err = self.record(provider, extra=("--pushed", "0" * 40))
        self.assertEqual(rc, 1)
        self.assertEqual(provider.posted, [])
        self.assertIn("not a commit on this pull request", err)

    def test_an_unresolvable_pushed_sha_still_marks_a_moved_tip(self):
        """The likeliest way to reach a refusal with the branch already moved.

        The commits are on the branch; only the argument naming them is wrong.
        Resolving `--pushed` used to exit through `_resolve_sha` directly, so
        this path left the tip unmarked while every neighbouring path marked
        it — and `SKILL.md` tells the model not to retry after a refusal,
        which turns a typo into the runaway rather than into one lost turn.
        """
        for value in ("cc1", "9" * 40):
            with self.subTest(pushed=value):
                provider = FakeProvider(commits=COMMITS_AFTER_FIX)
                rc, _, _ = self.record(provider, extra=("--pushed", value))
                self.assertEqual(rc, 1)
                self.assertEqual(len(provider.posted), 1)
                self.assertIn(
                    f"<!-- agent-updated:{HEAD_SHA} -->", provider.posted[0][1]
                )

    def test_every_refusal_on_a_moved_branch_marks_the_tip(self):
        """The guarantee is worth what its least-travelled path is worth.

        One refusal that exits without a marker is enough to reopen the loop,
        and the four documents that state the guarantee do not distinguish
        between paths — so this walks them rather than trusting the prose. Add
        a refusal below `--attempted-sha`'s resolution, add it here.
        """
        cases = {
            "--pushed is too short": ("--pushed", "cc1"),
            "--pushed is not on the branch": ("--pushed", "9" * 40),
            "--pushed predates the run": ("--pushed", BASE_SHA),
            "--no-change on a moved branch": ("--no-change",),
        }
        for label, extra in cases.items():
            with self.subTest(refusal=label):
                provider = FakeProvider(commits=COMMITS_AFTER_FIX)
                rc, _, _ = self.record(provider, extra=extra)
                self.assertEqual(rc, 1)
                self.assertEqual(len(provider.posted), 1, label)
                self.assertIn(
                    f"<!-- agent-updated:{HEAD_SHA} -->", provider.posted[0][1]
                )

        # The two body refusals, which reach `refuse` through `SystemExit`
        # rather than through `on_fail`. Both claim a real commit, so they get
        # past everything above and fail on the body alone.
        bodies = {
            "the body is unreadable": os.path.join(self.scratch, "absent.md"),
            "the body is only marker syntax": self.scratch_file(
                name="markers.md",
                content=pr_triggers.marker(HEAD_SHA, pr_triggers.UPDATED_MARKER),
            ),
        }
        for label, path in bodies.items():
            with self.subTest(refusal=label):
                provider = FakeProvider(commits=COMMITS_AFTER_FIX)
                rc, _, _ = self.record(
                    provider, extra=("--pushed", FIX_SHA), body=path
                )
                self.assertEqual(rc, 1)
                self.assertEqual(len(provider.posted), 1, label)
                self.assertIn(
                    f"<!-- agent-updated:{HEAD_SHA} -->", provider.posted[0][1]
                )

    def test_an_unresolvable_attempted_sha_is_the_stated_exception(self):
        """It is what "the branch has moved" would be measured against.

        Refusing plainly is honest here — nothing has been posted, nothing on
        the thread has changed, and the next tick simply cards the pull request
        again. Pinned because the docstrings name this as the one exception,
        and an exception nobody tests drifts into a second defect.
        """
        provider = FakeProvider(commits=COMMITS_AFTER_FIX)
        rc, _, err = self.record(
            provider, sha="9" * 40, extra=("--pushed", FIX_SHA)
        )
        self.assertEqual(rc, 1)
        self.assertEqual(provider.posted, [])
        self.assertIn("--attempted-sha", err)
        self.assertIn("Nothing was posted", err)

    def test_a_claim_is_required(self):
        provider = FakeProvider()
        rc, _, _ = self.record(provider, extra=())
        self.assertEqual(rc, 2)
        self.assertEqual(provider.posted, [])

    def test_marker_syntax_in_the_model_body_is_stripped(self):
        """A marker the model imitated becomes a real one the moment this posts.

        Naming another sha, it would record an attempt that never happened and
        spend the budget of a branch nobody looked at.
        """
        provider = FakeProvider()
        body = self.scratch_file(
            content=f"Done.\n{pr_triggers.marker(BASE_SHA, pr_triggers.UPDATED_MARKER)}"
        )
        self.record(provider, body=body)
        _, posted = provider.posted[0]
        self.assertNotIn(BASE_SHA, posted)
        self.assertIn(f"agent-updated:{HEAD_SHA}", posted)

    def test_a_body_outside_the_scratch_directory_posts_nothing(self):
        provider = FakeProvider()
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
            handle.write("elsewhere")
        rc, _, err = self.record(provider, body=handle.name)
        self.assertEqual(rc, 1)
        self.assertEqual(provider.posted, [])
        self.assertIn("resolves outside", err)

    def test_a_pull_request_the_agent_does_not_own_posts_nothing(self):
        provider = FakeProvider(prs=[make_pr(author="someone-else")])
        rc, _, err = self.record(provider)
        self.assertEqual(rc, 1)
        self.assertEqual(provider.posted, [])
        self.assertIn("not one of this agent's pull requests", err)

    def test_an_ignored_pull_request_posts_nothing(self):
        provider = FakeProvider(prs=[make_pr(labels=(forge.IGNORE_LABEL,))])
        rc, _, err = self.record(provider)
        self.assertEqual(rc, 1)
        self.assertEqual(provider.posted, [])
        self.assertIn(forge.IGNORE_LABEL, err)

    def test_a_failed_post_exits_non_zero(self):
        provider = FakeProvider(post_error=forge.ForgeError("REPO_UNREACHABLE", "403"))
        rc, _, err = self.record(provider)
        self.assertEqual(rc, 1)
        self.assertIn("could not post", err)


class RepositoryScopeTest(_Harness):
    """`--repo` and the managed allowlist, since the watcher went multi-repo."""

    def test_a_row_names_the_repository_it_came_from(self):
        """`record` needs `--repo` as well as `--pr`.

        A row carrying only the number would send the worker looking for the
        repository the card came from, and one poll now spans all of them.
        """
        payload = self.poll(FakeProvider(conflicted=True))
        self.assertEqual(payload["pull_requests"][0]["repository"], REPO)

    def test_poll_sweeps_every_managed_repository(self):
        provider = _PerRepoProvider(
            {
                "acme/a": [make_pr(7, head_repo="acme/a")],
                "acme/b": [make_pr(9, head_repo="acme/b")],
            },
            conflicted=True,
        )
        rc, out, _ = self.run_helper(["poll"], provider, repo=None, repos=["acme/a", "acme/b"])
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(
            sorted((row["repository"], row["pr"]) for row in payload["pull_requests"]),
            [("acme/a", 7), ("acme/b", 9)],
        )

    def test_poll_with_no_managed_repository_is_not_configured(self):
        rc, out, _ = self.run_helper(["poll"], FakeProvider(), repo=None)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["status"], "NOT_CONFIGURED")

    def test_poll_rejects_a_repository_the_install_does_not_manage(self):
        rc, out, _ = self.run_helper(
            ["poll", "--repo", "unmanaged/repo"], FakeProvider(), repo="managed/repo"
        )
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["reason"], "INVALID_REPOSITORY")

    def test_record_refuses_an_unmanaged_repository_before_posting(self):
        """The allowlist is a gate on a write, so it runs before the post."""
        provider = FakeProvider()
        rc, _, err = self.run_helper(
            [
                "record",
                "--repo",
                "unmanaged/repo",
                "--pr",
                "12",
                "--attempted-sha",
                HEAD_SHA,
                "--body-file",
                self.scratch_file(),
                "--no-change",
            ],
            provider,
            repo="managed/repo",
        )
        self.assertEqual(rc, 1)
        self.assertEqual(provider.posted, [])
        self.assertIn("not in the managed repositories list", err)

    def test_record_refuses_a_repository_that_is_not_a_slug(self):
        provider = FakeProvider()
        rc, _, err = self.run_helper(
            [
                "record",
                "--repo",
                "../invalid",
                "--pr",
                "12",
                "--attempted-sha",
                HEAD_SHA,
                "--body-file",
                self.scratch_file(),
                "--no-change",
            ],
            provider,
        )
        self.assertEqual(rc, 1)
        self.assertEqual(provider.posted, [])
        self.assertIn("Invalid repository format", err)


class _PerRepoProvider(FakeProvider):
    """A `FakeProvider` whose open pull requests differ by repository."""

    def __init__(self, by_repo, **kwargs):
        super().__init__(prs=[], **kwargs)
        self._by_repo = by_repo

    def list_open_prs(self, repo):
        return list(self._by_repo.get(repo, []))


if __name__ == "__main__":
    unittest.main()
