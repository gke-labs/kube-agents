"""Unit tests for scripts/release/tag_commit.sh and the four wrappers over it.

tag_commit.sh replaced a body that had been copied into each tagger on the
release ladder. The point of the cases below is not that the shared script works
— it is that each wrapper still behaves the way it did before it became one, so
the consolidation is demonstrably behaviour-preserving for the three callers
that already worked. tag_ga_release.sh keeps its own suite
(tests/test_tag_ga_release.py); the GA case here only pins that it still routes
through the shared tagger.
"""

import pathlib
import subprocess
import unittest

from tests.testing.common import (
    create_mock_git_repo,
    get_isolated_test_env,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_RELEASE = _REPO_ROOT / "scripts" / "release"


class TagCommitTest(unittest.TestCase):
    def _run(self, script, args, cwd, env=None):
        return subprocess.run(
            ["bash", str(_RELEASE / script)] + args,
            capture_output=True,
            text=True,
            env=get_isolated_test_env(overrides=env),
            cwd=cwd,
        )

    def _repo(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        return repo_dir, git

    # ─── the shared tagger ────────────────────────────────────────────────────

    def test_requires_a_tag_and_a_commit(self):
        repo_dir, _ = self._repo()
        for args in ([], ["only-a-tag"]):
            with self.subTest(args=args):
                proc = self._run("tag_commit.sh", args, repo_dir)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("a tag name and a commit SHA are required", proc.stderr)

    def test_rejects_an_unknown_option_rather_than_tagging_with_it(self):
        repo_dir, git = self._repo()
        head = git("rev-parse", "HEAD").stdout.strip()
        proc = self._run("tag_commit.sh", ["--titel", "typo", "some_tag", head], repo_dir)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unknown option", proc.stderr)
        self.assertEqual(git("tag", "-l").stdout.strip(), "")

    def test_creates_the_tag_and_is_idempotent_on_the_same_commit(self):
        repo_dir, git = self._repo()
        head = git("rev-parse", "HEAD").stdout.strip()

        proc = self._run("tag_commit.sh", ["--title", "A TITLE", "--detail", "Extra: yes", "some_tag", head], repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("A TITLE", proc.stdout)
        self.assertIn("Extra: yes", proc.stdout)
        self.assertEqual(git("rev-parse", "some_tag^{commit}").stdout.strip(), head)

        again = self._run("tag_commit.sh", ["some_tag", head], repo_dir)
        self.assertEqual(again.returncode, 0)
        self.assertIn("Idempotent skip", again.stdout)

    def test_refuses_to_move_a_tag_to_a_different_commit(self):
        repo_dir, git = self._repo()
        first = git("rev-parse", "HEAD").stdout.strip()
        (pathlib.Path(repo_dir) / "second.txt").write_text("second\n")
        git("add", "second.txt")
        git("commit", "-m", "chore: second commit")
        second = git("rev-parse", "HEAD").stdout.strip()

        self.assertEqual(self._run("tag_commit.sh", ["some_tag", first], repo_dir).returncode, 0)
        proc = self._run("tag_commit.sh", ["some_tag", second], repo_dir)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("points to commit", proc.stderr)
        self.assertEqual(git("rev-parse", "some_tag^{commit}").stdout.strip(), first)

    # ─── the wrappers ─────────────────────────────────────────────────────────

    def test_create_release_tag_wrapper(self):
        repo_dir, git = self._repo()
        head = git("rev-parse", "HEAD").stdout.strip()

        proc = self._run("create_release_tag.sh", [head, "rc_2608241820_b35543c"], repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("RELEASE CANDIDATE", proc.stdout)
        self.assertEqual(git("rev-parse", "rc_2608241820_b35543c^{commit}").stdout.strip(), head)

        missing = self._run("create_release_tag.sh", [], repo_dir)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("COMMIT_SHA and RC_TAG are required", missing.stderr)

    def test_tag_validated_release_wrapper_appends_the_suffix(self):
        repo_dir, git = self._repo()
        head = git("rev-parse", "HEAD").stdout.strip()

        proc = self._run("tag_validated_release.sh", [head, "rc_2608241820_b35543c"], repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            git("rev-parse", "rc_2608241820_b35543c_validated^{commit}").stdout.strip(), head
        )

    def _commit_staging_trigger(self, repo_dir, git, pattern):
        """Commits the redeploy trigger the promotion has to match.

        A push event runs the workflows in the pushed ref's tree, so the commit
        being tagged is what decides whether the tag deploys anything, and
        tag_staging_promotion.sh refuses when it would not.
        """
        workflow = pathlib.Path(repo_dir) / ".github" / "workflows"
        workflow.mkdir(parents=True, exist_ok=True)
        (workflow / "staging-redeploy-agent.yml").write_text(
            f'name: Staging Redeploy Agent\n\non:\n  push:\n    tags:\n      - "{pattern}"\n\njobs: {{}}\n'
        )
        git("add", "-A")
        git("commit", "-m", "chore: staging trigger")
        return git("rev-parse", "HEAD").stdout.strip()

    def test_tag_staging_promotion_wrapper_derives_the_tag(self):
        repo_dir, git = self._repo()
        head = self._commit_staging_trigger(repo_dir, git, "staging_*")
        git("tag", "-a", "rc_2608241820_b35543c_validated", "-m", "Validated")

        proc = self._run("tag_staging_promotion.sh", [head, "rc_2608241820_b35543c_validated"], repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(git("rev-parse", "staging_2608241820_b35543c^{commit}").stdout.strip(), head)

    def test_tag_staging_promotion_refuses_a_candidate_the_tag_would_not_trigger(self):
        """A promotion that deploys nothing must not report success.

        Candidates predating the `staging/**` -> `staging_*` rename carry the old
        trigger in their own tree, which a flat staging_<ts>_<sha> does not match.
        Pushing anyway would go green having deployed nothing, and the tag it left
        behind would make every later run skip that candidate.
        """
        repo_dir, git = self._repo()
        head = self._commit_staging_trigger(repo_dir, git, "staging/**")
        git("tag", "-a", "rc_2608241820_b35543c_validated", "-m", "Validated")

        proc = self._run("tag_staging_promotion.sh", [head, "rc_2608241820_b35543c_validated"], repo_dir)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("does not match", proc.stderr)
        self.assertEqual(git("tag", "-l", "staging_*").stdout.strip(), "")

    def test_tag_staging_promotion_refuses_a_mismatched_explicit_tag(self):
        """An explicit staging tag has to be the one this candidate maps to."""
        repo_dir, git = self._repo()
        head = git("rev-parse", "HEAD").stdout.strip()
        git("tag", "-a", "rc_2608241820_b35543c_validated", "-m", "Validated")

        proc = self._run(
            "tag_staging_promotion.sh",
            [head, "rc_2608241820_b35543c_validated", "staging_9999999999_deadbee"],
            repo_dir,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("does not match the tag derived from", proc.stderr)
        self.assertEqual(git("tag", "-l", "staging_*").stdout.strip(), "")

    def test_tag_staging_promotion_guards_the_namespace(self):
        """The namespace guard, reached through the environment rather than argv."""
        repo_dir, git = self._repo()
        head = git("rev-parse", "HEAD").stdout.strip()

        proc = self._run(
            "tag_staging_promotion.sh",
            [head, "0.2.0"],
            repo_dir,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not an rc_* candidate tag", proc.stderr)
        self.assertEqual(git("tag", "-l").stdout.strip(), "")

    def test_tag_staging_promotion_refuses_an_unvalidated_commit(self):
        """The resolver's gate, applied again at the last point before the push.

        COMMIT_SHA and RC_TAG are independent arguments, so every shape check can
        pass while the commit is something else entirely. The tag is a deploy
        trigger, so the cost of missing this is an unvalidated commit on staging
        under a name that reads back to a validated candidate.
        """
        repo_dir, git = self._repo()
        head = git("rev-parse", "HEAD").stdout.strip()
        # The tag exists, but on no commit this promotion names.
        (pathlib.Path(repo_dir) / "second.txt").write_text("second\n")
        git("add", "second.txt")
        git("commit", "-m", "chore: second commit")
        git("tag", "-a", "rc_2608241820_b35543c_validated", "-m", "Validated elsewhere")

        proc = self._run("tag_staging_promotion.sh", [head, "rc_2608241820_b35543c_validated"], repo_dir)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("carries no rc_*_validated tag", proc.stderr)
        self.assertEqual(git("tag", "-l", "staging_*").stdout.strip(), "")

    def test_tag_ga_release_still_routes_through_the_shared_tagger(self):
        repo_dir, git = self._repo()
        head = git("rev-parse", "HEAD").stdout.strip()

        proc = self._run("tag_ga_release.sh", ["0.2.0", head], repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("CREATING AND PUSHING GA RELEASE GIT TAG", proc.stdout)
        self.assertIn("Release Version:     0.2.0", proc.stdout)
        self.assertEqual(git("rev-parse", "0.2.0^{commit}").stdout.strip(), head)


if __name__ == "__main__":
    unittest.main()
