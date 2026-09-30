"""Tests for scripts/release/resolve_rc_tag.sh's tag-name resolution."""

import pathlib
import re
import subprocess
import unittest

from tests.testing.common import create_mock_git_repo, get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "release" / "resolve_rc_tag.sh"
_PIPELINE_RC_TAG_SHAPE = re.compile(r"^rc_[0-9]{10}_[0-9a-f]{7}$")


class ResolveRcTagTest(unittest.TestCase):
    def _run(self, repo_dir, env):
        gh_out = pathlib.Path(repo_dir) / "gh_out.txt"
        gh_out.write_text("")
        proc = subprocess.run(
            ["bash", str(_SCRIPT)],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            env=get_isolated_test_env(overrides={"GITHUB_OUTPUT": str(gh_out), **env}),
        )
        outputs = dict(line.split("=", 1) for line in gh_out.read_text().splitlines() if "=" in line)
        return proc, outputs

    def test_a_hand_named_rc_tag_on_the_commit_is_not_reused(self):
        """The way out of a refused hand-named marker is a dispatch with rc_tag empty.

        That only works if this script then mints the pipeline's own name rather
        than picking the hand-named tag back up and re-earning the same marker.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            head = git("rev-parse", "HEAD").stdout.strip()
            git("tag", "-a", "rc_0.8_backport", "-m", "hand-named")
            git("tag", "-a", "rc_0.8_backport_validated", "-m", "refused by the line gate")
            # The pipeline's shape with another commit's sha: also hand-named, also refused
            # by the gate, and the one the bare shape would have picked back up.
            git("tag", "-a", "rc_2609290000_0000000", "-m", "hand-named in the pipeline's shape")

            proc, outputs = self._run(repo_dir, {"COMMIT_SHA": head})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(outputs["commit_sha"], head)
            self.assertRegex(outputs["rc_tag"], _PIPELINE_RC_TAG_SHAPE)
            self.assertTrue(outputs["rc_tag"].endswith(head[:7]))
            self.assertNotIn("WARNING", proc.stderr)

            # Once the pipeline's own tag exists it is the one reused, not re-minted.
            git("tag", "-a", "rc_2609290000_" + head[:7], "-m", "the pipeline's own")
            proc, outputs = self._run(repo_dir, {"COMMIT_SHA": head})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(outputs["rc_tag"], "rc_2609290000_" + head[:7])
        finally:
            temp_dir.cleanup()

    def test_a_hand_named_rc_tag_input_is_honoured_with_a_warning(self):
        """main's dispatch contract keeps the free-form input; the warning says what it costs a line."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            head = git("rev-parse", "HEAD").stdout.strip()
            proc, outputs = self._run(repo_dir, {"COMMIT_SHA": head, "RC_TAG": "rc_0.8_backport"})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(outputs["rc_tag"], "rc_0.8_backport")
            self.assertIn("rc_0.8_backport_validated", proc.stderr)
            self.assertIn("release line's gate", proc.stderr)
            self.assertIn("rc_tag empty", proc.stderr)

            proc, outputs = self._run(repo_dir, {"COMMIT_SHA": head, "RC_TAG": "rc_2609290000_0000000"})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(outputs["rc_tag"], "rc_2609290000_0000000")
            self.assertIn("is not the pipeline's own name for commit " + head[:7], proc.stderr, "the shape alone is not the name")

            proc, _ = self._run(repo_dir, {"COMMIT_SHA": head, "RC_TAG": "rc_2609290000_" + head[:7]})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn("WARNING", proc.stderr, "the pipeline's own name draws no warning")
        finally:
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
