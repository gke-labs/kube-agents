#!/usr/bin/env python3
"""Tests for merge_queue_dropped_push.py -- the notice for a push the merge queue did not merge.

Run: cd scripts && python3 -m unittest test_merge_queue_dropped_push

The two wrong answers cost differently. A notice on a pull request that merged
whole tells an author to redo work that is on `main`, and a queue that moves
`main` under every pull request would send it to all of them. A missed notice
leaves a commit off `main` with nothing saying so, which is where things stood
before. So the verdict is driven over real git repositories built here, with
the squash made by `git merge --squash` and not by the function under test:
the base moved, a late commit, a late merge of the base, both together, a
force-push, and a conflict. No test reaches the network.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import merge_queue_dropped_push as dropped

REPO = "gke-labs/kube-agents"
NUMBER = 7
MERGE = "c" * 40
HEAD = "a" * 40
BUILT = "b" * 40
URL = "https://github.example/gke-labs/kube-agents.git"
# A developer's own git configuration (signing, hooks, a default branch) must not reach these repositories.
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Author",
    "GIT_AUTHOR_EMAIL": "author@example.com",
    "GIT_COMMITTER_NAME": "Author",
    "GIT_COMMITTER_EMAIL": "author@example.com",
}
SHARED = "".join(f"line {index}\n" for index in range(1, 21))


class Repository:
    """A work tree with a `main` and a pull request branch `pr`, and the moves a queue merge takes."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.git = dropped.Git(directory)
        self.git.run("init", "--quiet", "--initial-branch=main")
        self.commit({"shared.txt": SHARED, "other.txt": "other\n"}, "base")
        self.git.run("switch", "--quiet", "--create", "pr")

    def commit(self, files, subject):
        for name, text in files.items():
            (self.directory / name).write_text(text)
        self.git.run("add", "--all")
        self.git.run("commit", "--quiet", "--message", subject)
        return self.git.output("rev-parse", "HEAD")

    def on(self, branch):
        self.git.run("switch", "--quiet", branch)
        return self

    def move_main(self):
        """Another pull request lands: a new file, and the top of the file this pull request edits at the end."""
        self.on("main")
        sha = self.commit({"shared.txt": "main's line\n" + SHARED, "elsewhere.txt": "elsewhere\n"}, "another change (#6)")
        self.on("pr")
        return sha

    def squash(self, head):
        """What the queue does with the head it built from: a squash of it on `main`."""
        self.on("main")
        self.git.run("merge", "--quiet", "--squash", head)
        self.git.run("commit", "--quiet", "--message", "the pull request (#7)")
        sha = self.git.output("rev-parse", "HEAD")
        self.on("pr")
        return sha

    def merge_main(self, main):
        self.git.run("merge", "--quiet", "--no-ff", "--message", "Merge branch 'main' into pr", main)
        return self.git.output("rev-parse", "HEAD")

    def tree(self, commit):
        return self.git.output("rev-parse", f"{commit}^{{tree}}")


class DecideTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(os.environ, GIT_ENV)
        patch.start()
        self.addCleanup(patch.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Repository(directory.name)
        self.first = self.repo.commit({"shared.txt": SHARED + "the pull request's line\n"}, "feat: the change")

    def decide(self, merge_commit, head):
        return dropped.decide(self.repo.git, merge_commit, head)

    def test_a_merge_of_the_final_head_is_complete_though_the_base_moved(self):
        self.repo.move_main()
        merged = self.repo.squash(self.first)
        # The trees differ, as they do on every pull request whose base moved: comparing them would report this one.
        self.assertNotEqual(self.repo.tree(merged), self.repo.tree(self.first))
        result = self.decide(merged, self.first)
        self.assertEqual(result["verdict"], dropped.COMPLETE)
        self.assertEqual(result["commits"], [])

    def test_commits_pushed_after_the_queue_built_are_dropped_and_named(self):
        self.repo.move_main()
        merged = self.repo.squash(self.first)
        second = self.repo.commit({"other.txt": "other\nlate\n"}, "fix: a late change")
        third = self.repo.commit({"late.txt": "late\n"}, "test: a late test")
        result = self.decide(merged, third)
        self.assertEqual(result["verdict"], dropped.DROPPED)
        self.assertEqual(result["built"], self.first)
        self.assertEqual(result["commits"], [(second, "fix: a late change"), (third, "test: a late test")])

    def test_a_late_push_that_only_merges_the_base_is_complete(self):
        main = self.repo.move_main()
        merged = self.repo.squash(self.first)
        head = self.repo.merge_main(main)
        self.assertNotEqual(head, self.first)
        result = self.decide(merged, head)
        self.assertEqual(result["verdict"], dropped.COMPLETE)

    def test_a_late_merge_of_the_base_with_a_late_commit_names_the_commit_only(self):
        main = self.repo.move_main()
        merged = self.repo.squash(self.first)
        merge = self.repo.merge_main(main)
        late = self.repo.commit({"late.txt": "late\n"}, "fix: a late change")
        result = self.decide(merged, late)
        self.assertEqual(result["verdict"], dropped.DROPPED)
        # The merge of the base gives the merged tree too, and it is the newer of the two.
        self.assertEqual(result["built"], merge)
        self.assertEqual(result["commits"], [(late, "fix: a late change")])

    def test_a_late_commit_with_a_late_merge_of_the_base_after_it_names_the_commit_only(self):
        main = self.repo.move_main()
        merged = self.repo.squash(self.first)
        late = self.repo.commit({"late.txt": "late\n"}, "fix: a late change")
        head = self.repo.merge_main(main)
        result = self.decide(merged, head)
        self.assertEqual(result["verdict"], dropped.DROPPED)
        self.assertEqual(result["built"], self.first)
        # Neither the merge commit nor the commit of `main` that it brought in.
        self.assertEqual(result["commits"], [(late, "fix: a late change")])

    def test_a_late_commit_with_no_subject_is_named_with_an_empty_one(self):
        merged = self.repo.squash(self.first)
        (self.repo.directory / "late.txt").write_text("late\n")
        self.repo.git.run("add", "--all")
        self.repo.git.run("commit", "--quiet", "--allow-empty-message", "--message", "")
        head = self.repo.git.output("rev-parse", "HEAD")
        result = self.decide(merged, head)
        self.assertEqual(result["commits"], [(head, "")])
        self.assertIn(f"- {head[:12]} ``", dropped.comment_body("main", merged, head, result["built"], result["commits"]))

    def test_a_late_commit_that_changes_nothing_in_the_end_is_complete(self):
        merged = self.repo.squash(self.first)
        self.repo.commit({"late.txt": "late\n"}, "fix: a late change")
        self.repo.git.run("rm", "--quiet", "late.txt")
        self.repo.git.run("commit", "--quiet", "--message", "revert: the late change")
        head = self.repo.git.output("rev-parse", "HEAD")
        self.assertEqual(self.decide(merged, head)["verdict"], dropped.COMPLETE)

    def test_a_force_push_after_the_queue_built_is_dropped_without_a_list(self):
        merged = self.repo.squash(self.first)
        self.repo.git.run("reset", "--quiet", "--hard", "HEAD~1")
        head = self.repo.commit({"shared.txt": SHARED + "a rewritten line\n"}, "feat: the change, rewritten")
        result = self.decide(merged, head)
        self.assertEqual(result["verdict"], dropped.DROPPED)
        self.assertIsNone(result["built"])
        self.assertEqual(result["commits"], [])

    def test_a_built_from_commit_past_the_ceiling_is_dropped_without_a_list(self):
        merged = self.repo.squash(self.first)
        head = self.repo.commit({"late.txt": "1\n"}, "fix: late 1")
        head = self.repo.commit({"late.txt": "2\n"}, "fix: late 2")
        with mock.patch.object(dropped, "CANDIDATES_CEILING", 1):
            result = self.decide(merged, head)
        self.assertEqual(result["verdict"], dropped.DROPPED)
        self.assertIsNone(result["built"])
        self.assertEqual(self.decide(merged, head)["built"], self.first)

    def test_a_final_head_that_conflicts_with_the_base_is_undecided(self):
        self.repo.on("main").commit({"other.txt": "main's other\n"}, "another change (#8)")
        merged = self.repo.on("pr").squash(self.first)
        # The late commit rewrites the line that `main` rewrote before the queue built.
        head = self.repo.commit({"other.txt": "the pull request's other\n"}, "fix: a late change")
        result = self.decide(merged, head)
        self.assertEqual(result["verdict"], dropped.UNDECIDED)
        self.assertIn("conflicts", result["why"])

    def test_a_merge_commit_is_undecided(self):
        main = self.repo.move_main()
        self.repo.on("main")
        self.repo.git.run("merge", "--quiet", "--no-ff", "--message", "Merge pull request #7", self.first)
        merged = self.repo.git.output("rev-parse", "HEAD")
        self.assertNotEqual(merged, main)
        result = self.decide(merged, self.first)
        self.assertEqual(result["verdict"], dropped.UNDECIDED)
        self.assertIn("2 parents", result["why"])


class FetchTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(os.environ, GIT_ENV)
        patch.start()
        self.addCleanup(patch.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "source").mkdir()
        self.source = Repository(self.root / "source")
        self.first = self.source.commit({"late.txt": "late\n"}, "feat: the change")
        self.merged = self.source.squash(self.first)

    def test_a_fetch_into_an_empty_repository_brings_what_the_verdict_reads(self):
        (self.root / "empty").mkdir()
        git = dropped.Git(self.root / "empty")
        git.run("init", "--quiet", "--bare")
        dropped.fetch(git, str(self.root / "source"), (self.merged, self.first))
        self.assertEqual(dropped.decide(git, self.merged, self.first)["verdict"], dropped.COMPLETE)

    def test_commits_already_there_are_not_fetched(self):
        # The address does not exist, so any fetch fails.
        dropped.fetch(self.source.git, str(self.root / "absent"), (self.merged, self.first))
        with self.assertRaises(RuntimeError):
            dropped.fetch(self.source.git, str(self.root / "absent"), ("d" * 40,))


class CommentBodyTest(unittest.TestCase):
    def test_one_missing_commit(self):
        body = dropped.comment_body("main", MERGE, HEAD, BUILT, [(HEAD, "fix: a late change")])
        self.assertTrue(body.startswith(dropped.MARKER + "\n"))
        self.assertIn("without its last commit. `main` does not have it:", body)
        self.assertIn("- aaaaaaaaaaaa `fix: a late change`", body)
        self.assertIn("It built from bbbbbbbbbbbb and merged the result as cccccccccccc.", body)
        self.assertIn("rebase the branch onto `main` and open a new pull request", body)

    def test_several_missing_commits_are_counted_and_the_long_tail_is_summed(self):
        commits = [(f"{index:040x}", f"fix: change {index}") for index in range(dropped.COMMITS_LISTED + 3)]
        body = dropped.comment_body("main", MERGE, HEAD, BUILT, commits)
        self.assertIn(f"without its last {len(commits)} commits. `main` does not have them:", body)
        self.assertEqual(body.count("\n- "), dropped.COMMITS_LISTED + 1)
        self.assertIn("- and 3 more", body)

    def test_no_known_built_from_commit_gives_the_notice_without_a_list(self):
        body = dropped.comment_body("main", MERGE, HEAD)
        self.assertTrue(body.startswith(dropped.MARKER + "\n"))
        self.assertIn("merged an earlier version of this pull request", body)
        self.assertIn("its final head aaaaaaaaaaaa", body)
        self.assertNotIn("\n- ", body)

    def test_a_commit_subject_cannot_mention_or_leave_its_code_span(self):
        subject = "fix: ping @someone ` and close #1 " + "x" * 200
        body = dropped.comment_body("main", MERGE, HEAD, BUILT, [(HEAD, subject)])
        line = next(line for line in body.splitlines() if line.startswith("- "))
        self.assertEqual(line.count("`"), 2)
        self.assertTrue(line.endswith("`"))
        self.assertLessEqual(len(line), len("- aaaaaaaaaaaa ``") + dropped.SUBJECT_MAX)
        # Outside the commit lines the comment names no account.
        self.assertNotIn("@", body.replace(line, ""))

    def test_the_comment_names_the_base_branch_of_the_pull_request(self):
        body = dropped.comment_body("merge-queue-dry-run", MERGE, HEAD, BUILT, [(HEAD, "fix: a late change")])
        self.assertIn("`merge-queue-dry-run` does not have it", body)
        self.assertNotIn("`main`", body)


class FakeAPI:
    def __init__(self, pull, comments=()):
        self.repo = REPO
        self.pull = pull
        self.comments = list(comments)
        self.posts = []

    def get(self, path, tolerate=()):
        assert path == f"/repos/{REPO}/pulls/{NUMBER}", path
        return self.pull

    def get_all(self, path, per_page=100):
        assert path == f"/repos/{REPO}/issues/{NUMBER}/comments", path
        return self.comments

    def post(self, path, payload=None, tolerate=()):
        self.posts.append((path, payload))


def merged_pull(merged=True):
    return {"merged": merged, "merge_commit_sha": MERGE if merged else None, "head": {"sha": HEAD}, "base": {"ref": "main"}}


def verdict(kind, built=BUILT, commits=((HEAD, "fix: a late change"),)):
    return {"verdict": kind, "why": "why", "built": built, "commits": list(commits)}


class CheckTest(unittest.TestCase):
    """`check` with the git half replaced: what is read, and when a comment goes out."""

    def run_check(self, api, result, dry_run=False):
        with mock.patch.object(dropped, "fetch") as fetch, mock.patch.object(dropped, "decide", return_value=result) as decide:
            line = dropped.check(api, "git", NUMBER, URL, dry_run=dry_run)
        return line, fetch, decide

    def test_dropped_commits_get_one_comment(self):
        api = FakeAPI(merged_pull())
        line, fetch, decide = self.run_check(api, verdict(dropped.DROPPED))
        fetch.assert_called_once_with("git", URL, (MERGE, HEAD))
        decide.assert_called_once_with("git", MERGE, HEAD)
        self.assertEqual(len(api.posts), 1)
        path, payload = api.posts[0]
        self.assertEqual(path, f"/repos/{REPO}/issues/{NUMBER}/comments")
        self.assertEqual(payload, {"body": dropped.comment_body("main", MERGE, HEAD, BUILT, [(HEAD, "fix: a late change")])})
        self.assertIn("posted the comment", line)

    def test_a_second_run_does_not_post_again(self):
        api = FakeAPI(merged_pull(), comments=[{"body": "unrelated"}, {"body": dropped.MARKER + "\nThe merge queue..."}])
        line, _, _ = self.run_check(api, verdict(dropped.DROPPED))
        self.assertEqual(api.posts, [])
        self.assertIn("already posted", line)

    def test_a_complete_or_undecided_merge_gets_no_comment_and_reads_no_comments(self):
        for kind in (dropped.COMPLETE, dropped.UNDECIDED):
            with self.subTest(verdict=kind):
                api = FakeAPI(merged_pull())
                api.get_all = mock.Mock(side_effect=AssertionError("the comments were read"))
                line, _, _ = self.run_check(api, verdict(kind, built=None, commits=()))
                self.assertEqual(api.posts, [])
                self.assertIn(kind, line)

    def test_a_pull_request_closed_without_a_merge_is_not_compared(self):
        api = FakeAPI(merged_pull(merged=False))
        line, fetch, decide = self.run_check(api, verdict(dropped.DROPPED))
        fetch.assert_not_called()
        decide.assert_not_called()
        self.assertEqual(api.posts, [])
        self.assertIn("not merged", line)

    def test_a_dry_run_logs_the_comment_and_posts_nothing(self):
        api = FakeAPI(merged_pull())
        line, _, _ = self.run_check(api, verdict(dropped.DROPPED), dry_run=True)
        self.assertEqual(api.posts, [])
        self.assertIn("would post", line)
        self.assertIn(dropped.MARKER, line)


class MainTest(unittest.TestCase):
    def test_a_missing_token_or_repository_is_refused(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(dropped.main(["--number", "7"]), 2)
            self.assertEqual(dropped.main(["--number", "7", "--repo", REPO]), 2)

    def test_the_commits_are_fetched_from_the_repository_on_the_server_into_a_new_bare_repository(self):
        environ = {**GIT_ENV, "GITHUB_REPOSITORY": REPO, "GITHUB_TOKEN": "t", "GITHUB_SERVER_URL": "https://github.example"}
        environ["PATH"] = os.environ["PATH"]
        seen = {}

        def check(api, git, number, url, dry_run=False):
            seen.update(repo=api.repo, number=number, url=url, dry_run=dry_run)
            seen["bare"] = git.output("rev-parse", "--is-bare-repository")
            return "line"

        with mock.patch.dict(os.environ, environ, clear=True), mock.patch.object(dropped, "check", check):
            self.assertEqual(dropped.main(["--number", "7", "--dry-run"]), 0)
        self.assertEqual(seen, {"repo": REPO, "number": NUMBER, "url": URL, "dry_run": True, "bare": "true"})


if __name__ == "__main__":
    unittest.main()
