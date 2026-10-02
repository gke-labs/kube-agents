"""Unit tests for scripts/release/drop_eval_candidate.sh.

The script is the retry in the eval gate: when the release-candidate eval
returns no verdict at all, step 5b of the staging promotion pipeline deletes the
nomination tag so the next run can measure the same candidate again. Leaving
it in place would strand the candidate, because resolve_promotion_candidate.sh
reads the tag as "already answered about".

Two properties are worth pinning, and they pull in opposite directions. It has
to delete — a no-op that reports success would leave the candidate stranded
while looking like the retry worked. And it must delete nothing else: it is the
only script in the release ladder that runs `git push --delete`, and the tag
families either side of it in the ladder are the RC tag and the tag that deploys
to staging. Whether it may delete at all is step 5b's decision, made from the
poller's `settled` output, and tests/test_nightly_pipeline_wiring.py pins that
half.

The first of those two is the one that fails quietly, so several tests below are
about the ways "nothing to delete" and "deleted" can be reported when neither
happened: a tag fetch that failed leaves a clone that knows about no tags at
all, and a rejected push leaves the tag exactly where it was.
"""

import pathlib
import subprocess
import unittest

from tests.testing.common import create_mock_git_repo, get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "release" / "drop_eval_candidate.sh"

_TAG = "evalcand_2608241820_b35543c"

# Keeps every git transport in these tests on the local filesystem. The script's
# https fallback is a real github.com URL built from GH_ORG/GH_REPO, and a test
# that reaches it should fail on the spot rather than depend on what the network
# says about a repository that does not exist.
_LOCAL_TRANSPORT_ONLY = {"GIT_ALLOW_PROTOCOL": "file"}

# A pre-receive hook that declines everything, which is how these tests produce
# a push the remote rejects without needing a remote that can reject.
_REJECT_EVERYTHING_HOOK = "#!/bin/sh\nexit 1\n"
_HOOK_MODE = 0o755


class DropEvalCandidateTest(unittest.TestCase):
    def _run(self, args, cwd, env=None):
        return subprocess.run(
            ["bash", str(_SCRIPT)] + args,
            capture_output=True,
            text=True,
            env=get_isolated_test_env(overrides=env),
            cwd=cwd,
        )

    def _repo(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        return repo_dir, git

    def _bare_remote(self, repo_dir, git, name="origin"):
        """Adds a bare repository as `name` and returns its path.

        The script asks `origin` two questions — does the tag exist there, and
        will it accept the delete — so a repo with no remote at all cannot
        exercise either path.
        """
        remote = pathlib.Path(repo_dir).parent / f"{name}.git"
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(remote)],
            check=True,
            capture_output=True,
        )
        git("remote", "add", name, str(remote))
        return remote

    def _reject_pushes(self, remote):
        hook = pathlib.Path(remote) / "hooks" / "pre-receive"
        hook.write_text(_REJECT_EVERYTHING_HOOK)
        hook.chmod(_HOOK_MODE)

    def _ci_env(self, **overrides):
        """CI, with both remote paths pointed somewhere they cannot drift onto GitHub."""
        env = {
            "CI": "true",
            "GH_ORG": "example-invalid",
            "GH_REPO": "example-invalid",
        }
        env.update(_LOCAL_TRANSPORT_ONLY)
        env.update(overrides)
        return env

    def _second_commit(self, repo_dir, git):
        (pathlib.Path(repo_dir) / "second.txt").write_text("second\n")
        git("add", "second.txt")
        git("commit", "-m", "chore: second commit")
        return git("rev-parse", "HEAD").stdout.strip()

    def test_requires_a_commit_and_a_tag(self):
        repo_dir, _ = self._repo()
        for args in ([], ["only-a-commit"]):
            with self.subTest(args=args):
                proc = self._run(args, repo_dir)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("COMMIT_SHA and EVALCAND_TAG are required", proc.stderr)

    def test_deletes_the_nomination_tag_on_the_commit_it_names(self):
        repo_dir, git = self._repo()
        head = git("rev-parse", "HEAD").stdout.strip()
        git("tag", "-a", _TAG, "-m", "Nominated")

        proc = self._run([head, _TAG], repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(git("tag", "-l", "evalcand_*").stdout.strip(), "")

    def test_a_tag_that_is_on_neither_side_is_success(self):
        """The common case on a broken run, and not an error.

        Step 4 no-ops when the tag already exists and can be skipped outright, so
        step 5b reaching a commit with no nomination on it means there is nothing
        to withdraw — which is the state it was trying to produce. "Not there"
        has to hold on the remote as well as locally, so the remote exists here
        and is empty rather than absent.
        """
        repo_dir, git = self._repo()
        head = git("rev-parse", "HEAD").stdout.strip()
        self._bare_remote(repo_dir, git)

        proc = self._run([head, _TAG], repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("No tag", proc.stdout)

    def test_a_remote_that_cannot_be_asked_is_not_an_absence(self):
        """A clone that knows about no tags is not a repository with no tags.

        release_fetch_tags ends in `|| true`, so a fetch that failed — an
        expired token, a network blip — leaves every tag looking withdrawn
        already. Reading that as success would report the withdrawal on exactly
        the run where it did not happen, and the candidate would be stranded
        with nothing in the log saying so.
        """
        repo_dir, git = self._repo()
        head = git("rev-parse", "HEAD").stdout.strip()
        git("remote", "add", "origin", str(pathlib.Path(repo_dir).parent / "gone.git"))

        proc = self._run([head, _TAG], repo_dir)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("the remote could not be asked", proc.stderr)
        self.assertNotIn("No tag", proc.stdout)

    def test_a_tag_only_on_the_remote_is_not_read_as_already_gone(self):
        """The commit-match guard cannot run, and that is not a reason to stop.

        Same failed fetch as above, except the remote answers: the tag is there,
        the clone simply never heard about it. There is no local ref to compare
        against the commit, so the shape guard is all that bounds the name — and
        it is enough, because the name is what the delete is by.

        Run outside CI, so the delete itself is the dry run the next test does
        for real; what is pinned here is that the script reaches it.
        """
        repo_dir, git = self._repo()
        head = git("rev-parse", "HEAD").stdout.strip()
        remote = self._bare_remote(repo_dir, git)
        git("tag", "-a", _TAG, "-m", "Nominated")
        git("push", "origin", "main", "--tags")
        git("tag", "--delete", _TAG)

        proc = self._run([head, _TAG], repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("on the remote but not in this clone", proc.stderr)
        # Past the guards, into the block that deletes, rather than an early
        # success that read the missing local ref as nothing to do.
        self.assertIn("DROPPING AN EVAL NOMINATION", proc.stdout)
        self.assertIn(_TAG, self._remote_tags(remote))

    def test_refuses_a_tag_that_points_somewhere_else(self):
        """The same name at two commits is a hand-composed tag, not one of ours.

        An evalcand_ tag carries its commit's own short SHA, so this cannot
        happen on the path that creates them. Deleting it would destroy a
        nomination this pipeline did not make, and the candidate step 5b actually
        cares about was never tagged.
        """
        repo_dir, git = self._repo()
        first = git("rev-parse", "HEAD").stdout.strip()
        second = self._second_commit(repo_dir, git)
        git("tag", "-a", _TAG, second, "-m", "Nominated elsewhere")

        proc = self._run([first, _TAG], repo_dir)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("refusing to delete", proc.stderr)
        self.assertEqual(git("rev-parse", f"{_TAG}^{{commit}}").stdout.strip(), second)

    def test_refuses_every_tag_family_that_is_not_a_nomination(self):
        """The one guard standing between a mis-set variable and a deleted deploy tag.

        `staging_<ts>_<sha>` is what staging-deploy.yml triggers on and
        `rc_*_validated` is what marks a candidate testable at all. Both are
        shaped like the tag this script deletes, and both are in scope of the
        release bot token step 5b holds.
        """
        wrong = (
            "staging_2608241820_b35543c",
            "rc_2608241820_b35543c_validated",
            "0.2.0",
            # Prefix without shape: the eval job's `branches` regex would not have
            # matched this either, so nothing this script is retrying fired on it.
            "evalcand_hotfix",
            "evalcand_2608241820_b35543c_validated",
        )
        repo_dir, git = self._repo()
        head = git("rev-parse", "HEAD").stdout.strip()
        for tag in wrong:
            git("tag", "-a", tag, "-m", "Should survive")

        for tag in wrong:
            with self.subTest(tag=tag):
                proc = self._run([head, tag], repo_dir)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("is not an eval-candidate tag", proc.stderr)
                self.assertEqual(
                    git("rev-parse", f"{tag}^{{commit}}").stdout.strip(),
                    head,
                    f"{tag} was deleted",
                )

    def test_outside_ci_it_deletes_locally_and_pushes_nothing(self):
        """The same guard ensure_git_tag applies to the push, applied to the delete."""
        repo_dir, git = self._repo()
        head = git("rev-parse", "HEAD").stdout.strip()
        git("tag", "-a", _TAG, "-m", "Nominated")

        proc = self._run([head, _TAG], repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Remote delete skipped", proc.stdout)

    def test_in_ci_the_deletion_reaches_the_remote(self):
        """A local-only delete would leave the tag on GitHub, where it is read from.

        resolve_promotion_candidate.sh reads the tag graph out of a fresh
        checkout, so a nomination withdrawn only on a runner's disk is a
        nomination that is still there tomorrow — and the run reports success
        either way.
        """
        repo_dir, git = self._repo()
        head = git("rev-parse", "HEAD").stdout.strip()
        git("tag", "-a", _TAG, "-m", "Nominated")
        remote = self._bare_remote(repo_dir, git)
        git("push", "origin", "main", "--tags")
        self.assertIn(_TAG, self._remote_tags(remote))

        # The https fallback is pointed at a repository that does not exist and
        # then barred from leaving the filesystem, so a pass here is the `origin`
        # push having worked rather than a round trip that happened to succeed.
        proc = self._run([head, _TAG], repo_dir, env=self._ci_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(git("tag", "-l", "evalcand_*").stdout.strip(), "")
        self.assertNotIn(_TAG, self._remote_tags(remote))

    def test_a_remote_that_refuses_the_delete_reds_the_job(self):
        """The one outcome where reporting success would hide the whole point.

        The tag is still on the remote, so resolve_promotion_candidate.sh will
        go on skipping this candidate every night until a human deletes it by
        hand. Step 5b gates nothing — step 6 does not depend on it and step 5 has
        already failed — so reding it costs nothing it was protecting, and buys a
        failure that names the commit that needs the hand.
        """
        repo_dir, git = self._repo()
        head = git("rev-parse", "HEAD").stdout.strip()
        git("tag", "-a", _TAG, "-m", "Nominated")
        remote = self._bare_remote(repo_dir, git)
        git("push", "origin", "main", "--tags")
        self._reject_pushes(remote)

        proc = self._run([head, _TAG], repo_dir, env=self._ci_env())
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("::error title=Eval nomination not withdrawn", proc.stderr)
        self.assertIn(_TAG, proc.stderr)
        self.assertIn(_TAG, self._remote_tags(remote))

    def _remote_tags(self, remote):
        return subprocess.run(
            ["git", "tag", "-l"],
            cwd=str(remote),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()


if __name__ == "__main__":
    unittest.main()
