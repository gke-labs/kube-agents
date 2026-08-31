"""Unit tests for scripts/release/peel_tag_commit.sh.

The case that matters is the annotated tag: `git tag -a` makes the ref point at a
tag object, and a push event hands that object's SHA to github.sha. The staging
redeploys use the value as a container image tag, and no image is ever published
under a tag object's SHA, so the whole point of this script is that the annotated
case comes back different from what it was given and equal to the commit.
"""

import pathlib
import subprocess
import unittest

from tests.testing.common import (
    create_mock_git_repo,
    get_isolated_test_env,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "release" / "peel_tag_commit.sh"


class PeelTagCommitTest(unittest.TestCase):
    def setUp(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        self.repo_dir = repo_dir
        self.git = git
        self.head = git("rev-parse", "HEAD").stdout.strip()

    def _run(self, args, env=None):
        return subprocess.run(
            ["bash", str(_SCRIPT)] + args,
            capture_output=True,
            text=True,
            env=get_isolated_test_env(overrides=env),
            cwd=self.repo_dir,
        )

    def test_peels_an_annotated_tag_to_its_commit(self):
        self.git("tag", "-a", "staging_2608241820_b35543c", self.head, "-m", "promotion")
        tag_object = self.git("rev-parse", "staging_2608241820_b35543c").stdout.strip()
        self.assertNotEqual(tag_object, self.head, "expected an annotated tag object")

        proc = self._run([tag_object])

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip().splitlines()[-1], self.head)

    def test_a_lightweight_tag_resolves_to_the_same_sha(self):
        self.git("tag", "staging_2608241820_b35543c", self.head)

        proc = self._run([self.head])

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip().splitlines()[-1], self.head)

    def test_defaults_to_github_sha(self):
        self.git("tag", "-a", "staging_2608241820_b35543c", self.head, "-m", "promotion")
        tag_object = self.git("rev-parse", "staging_2608241820_b35543c").stdout.strip()

        proc = self._run([], env={"GITHUB_SHA": tag_object})

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip().splitlines()[-1], self.head)

    def test_writes_commit_sha_to_github_output(self):
        self.git("tag", "-a", "staging_2608241820_b35543c", self.head, "-m", "promotion")
        tag_object = self.git("rev-parse", "staging_2608241820_b35543c").stdout.strip()
        output_file = pathlib.Path(self.repo_dir) / "github_output"

        proc = self._run([tag_object], env={"GITHUB_OUTPUT": str(output_file)})

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(output_file.read_text().strip(), f"commit_sha={self.head}")

    def test_appends_rather_than_clobbers_github_output(self):
        output_file = pathlib.Path(self.repo_dir) / "github_output"
        output_file.write_text("existing=value\n")

        proc = self._run([self.head], env={"GITHUB_OUTPUT": str(output_file)})

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            output_file.read_text().splitlines(),
            ["existing=value", f"commit_sha={self.head}"],
        )

    def test_fails_when_no_ref_is_given(self):
        proc = self._run([], env={"GITHUB_SHA": ""})

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("a ref or SHA is required", proc.stderr)

    def test_fails_when_the_ref_is_not_in_the_checkout(self):
        proc = self._run(["0" * 40])

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("does not resolve to a commit", proc.stderr)


if __name__ == "__main__":
    unittest.main()
