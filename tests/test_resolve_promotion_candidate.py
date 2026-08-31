"""Unit tests for scripts/release/resolve_promotion_candidate.sh.

The script decides two things the nightly pipeline branches on, and confusing
them is the failure worth guarding against:

  skip_pipeline   nothing to deploy at all
  skip_promotion  deploy and test, but push no tag

Every skip is exit 0. The only exit 1 is a tag that does not resolve, or one the
RC pipeline never validated.
"""

import pathlib
import subprocess
import unittest

from tests.testing.common import (
    create_mock_git_repo,
    get_isolated_test_env,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "release" / "resolve_promotion_candidate.sh"


class ResolvePromotionCandidateTest(unittest.TestCase):
    def _repo(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        return pathlib.Path(repo_dir), git

    def _commit(self, repo_dir, git, name):
        (repo_dir / f"{name}.txt").write_text(f"{name}\n")
        git("add", f"{name}.txt")
        git("commit", "-m", f"chore: {name}")
        return git("rev-parse", "HEAD").stdout.strip()

    def _run(self, repo_dir, args=(), env=None):
        outputs = repo_dir / "outputs.txt"
        outputs.touch()
        overrides = {"GITHUB_OUTPUT": str(outputs)}
        if env:
            overrides.update(env)
        proc = subprocess.run(
            ["bash", str(_SCRIPT), *args],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(overrides=overrides),
            cwd=str(repo_dir),
        )
        parsed = {}
        for line in outputs.read_text().splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                parsed[key] = value
        return proc, parsed

    def test_picks_the_newest_validated_candidate(self):
        repo_dir, git = self._repo()
        older = git("rev-parse", "HEAD").stdout.strip()
        git("tag", "-a", "rc_2608181000_1111111_validated", "-m", "Older")
        newer = self._commit(repo_dir, git, "second")
        git("tag", "-a", "rc_2608191200_2222222_validated", "-m", "Newer")
        # An unvalidated candidate ahead of both must not win.
        self._commit(repo_dir, git, "third")
        git("tag", "-a", "rc_2608201300_3333333", "-m", "Unvalidated")

        proc, out = self._run(repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out["rc_tag"], "rc_2608191200_2222222_validated")
        self.assertEqual(out["commit_sha"], newer)
        self.assertNotEqual(out["commit_sha"], older)
        self.assertEqual(out["staging_tag"], "staging_2608191200_2222222")
        self.assertEqual(out["skip_pipeline"], "false")
        self.assertEqual(out["skip_promotion"], "false")

    def test_no_validated_tag_skips_the_pipeline_without_failing(self):
        repo_dir, _ = self._repo()

        proc, out = self._run(repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out["skip_pipeline"], "true")
        self.assertEqual(out["skip_promotion"], "true")
        self.assertEqual(out["commit_sha"], "")

    def test_already_promoted_skips_only_the_promotion(self):
        """The matrix still runs. Only the tag push is gated on eligibility."""
        repo_dir, git = self._repo()
        head = git("rev-parse", "HEAD").stdout.strip()
        git("tag", "-a", "rc_2608191200_2222222_validated", "-m", "Validated")
        git("tag", "-a", "staging_2608191200_2222222", "-m", "Already promoted")

        proc, out = self._run(repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out["commit_sha"], head)
        self.assertEqual(out["skip_pipeline"], "false", "an already-promoted night still deploys and tests")
        self.assertEqual(out["skip_promotion"], "true")

    def test_a_staging_tag_on_another_commit_does_not_block_this_one(self):
        repo_dir, git = self._repo()
        git("tag", "-a", "staging_2608181000_1111111", "-m", "Promoted earlier")
        candidate = self._commit(repo_dir, git, "second")
        git("tag", "-a", "rc_2608191200_2222222_validated", "-m", "Validated")

        proc, out = self._run(repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out["commit_sha"], candidate)
        self.assertEqual(out["skip_promotion"], "false")

    def test_an_explicit_tag_is_honoured(self):
        repo_dir, git = self._repo()
        older = git("rev-parse", "HEAD").stdout.strip()
        git("tag", "-a", "rc_2608181000_1111111_validated", "-m", "Older")
        self._commit(repo_dir, git, "second")
        git("tag", "-a", "rc_2608191200_2222222_validated", "-m", "Newer")

        proc, out = self._run(repo_dir, args=("rc_2608181000_1111111_validated",))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out["commit_sha"], older)
        self.assertEqual(out["staging_tag"], "staging_2608181000_1111111")

    def test_an_unvalidated_commit_is_refused_even_when_named_by_hand(self):
        """The gate cannot be talked into promoting what the RC pipeline failed."""
        repo_dir, git = self._repo()
        git("tag", "-a", "rc_2608191200_2222222", "-m", "Never validated")

        proc, out = self._run(repo_dir, args=("rc_2608191200_2222222",))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("carries no rc_*_validated tag", proc.stderr)
        self.assertEqual(out, {})

    def test_an_unresolvable_tag_fails(self):
        repo_dir, _ = self._repo()

        proc, _ = self._run(repo_dir, args=("rc_does_not_exist_validated",))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Cannot resolve a commit", proc.stderr)


if __name__ == "__main__":
    unittest.main()
