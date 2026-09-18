"""Unit tests for scripts/release/rollback_environment.sh.

The script moves a live install twice, so what can be pinned without a cluster
is the part that decides where it moves it: which GA it rolls back to, and the
refusals that stop it before anything is touched. `ROLLBACK_MODE=resolve` exits
after that decision.
"""

import pathlib
import subprocess
import tempfile
import unittest

from tests.testing.common import create_minimal_tools_bin, get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "release" / "rollback_environment.sh"


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


class RollbackEnvironmentResolveTest(unittest.TestCase):
    """A copy of the release scripts inside a repository with tags of our choosing."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.tmp_dir = pathlib.Path(tmp.name)
        self.repo = self.tmp_dir / "repo"
        for rel in (
            "scripts/release/rollback_environment.sh",
            "scripts/release/common.sh",
            "scripts/installer/gke_dns_endpoint.sh",
        ):
            target = self.repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((_REPO_ROOT / rel).read_bytes())
            target.chmod(0o755)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@example.com")
        _git(self.repo, "config", "user.name", "t")
        _git(self.repo, "config", "commit.gpgsign", "false")
        _git(self.repo, "config", "tag.gpgsign", "false")
        # The shape the release automation produces: each GA tag sits on a
        # stamped child of the candidate it was cut from, not on the candidate.
        self.commits = []
        for tag in ("0.4.0", "0.5.0", None):
            (self.repo / "file").write_text(f"candidate for {tag}\n")
            _git(self.repo, "add", "-A")
            _git(self.repo, "commit", "-q", "-m", f"candidate for {tag}")
            self.commits.append(_git(self.repo, "rev-parse", "HEAD"))
            if tag:
                (self.repo / "VERSION").write_text(f"{tag}\n")
                _git(self.repo, "add", "-A")
                _git(self.repo, "commit", "-q", "-m", f"chore(release): stamp release version {tag}")
                _git(self.repo, "tag", tag)
        self.stamped_050 = _git(self.repo, "rev-parse", "0.5.0^{commit}")
        self.bin_dir = create_minimal_tools_bin(self.tmp_dir)

    def _run(self, **env):
        self.summary = self.tmp_dir / "summary.md"
        self.summary.write_text("")
        overrides = {"ROLLBACK_MODE": "resolve", "GITHUB_STEP_SUMMARY": str(self.summary), **env}
        return subprocess.run(
            ["bash", str(self.repo / "scripts" / "release" / "rollback_environment.sh")],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(overrides=overrides, bin_dir=str(self.bin_dir)),
            cwd=str(self.repo),
        )

    def test_rolls_back_to_the_newest_ga_tag_by_default(self):
        proc = self._run(CANDIDATE_SHA=self.commits[2])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip().splitlines()[-1], "0.5.0")

    def test_an_explicit_tag_wins_over_the_newest(self):
        proc = self._run(CANDIDATE_SHA=self.commits[2], ROLLBACK_TAG="0.4.0")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip().splitlines()[-1], "0.4.0")

    def _assert_skipped(self, proc):
        """A skip: exit 0, nothing moved, and the summary says why."""
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Skipping", proc.stdout)
        self.assertIn("no older release", proc.stdout)
        self.assertIn("Rollback leg skipped", self.summary.read_text())

    def test_skips_when_the_ga_was_cut_from_the_candidate(self):
        """The night after a release: the newest GA is a stamped child of N.

        Equality would miss it and record an N-to-N move as a rollback; a
        refusal would make that night red for having nothing to test.
        """
        self._assert_skipped(self._run(CANDIDATE_SHA=self.commits[1]))

    def test_skips_when_the_candidate_is_the_stamped_ga_commit_itself(self):
        self._assert_skipped(self._run(CANDIDATE_SHA=self.stamped_050))

    def test_skips_a_candidate_older_than_the_ga(self):
        """A hand-dispatched old candidate would run the two directions swapped."""
        self._assert_skipped(self._run(CANDIDATE_SHA=self.commits[0], ROLLBACK_TAG="0.5.0"))

    def test_accepts_an_older_ga_for_the_same_candidate(self):
        proc = self._run(CANDIDATE_SHA=self.commits[1], ROLLBACK_TAG="0.4.0")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip().splitlines()[-1], "0.4.0")

    def test_refuses_without_a_candidate(self):
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("CANDIDATE_SHA is required", proc.stderr)

    def test_refuses_a_tag_that_is_not_a_ga_version(self):
        proc = self._run(CANDIDATE_SHA=self.commits[2], ROLLBACK_TAG="staging_2609160007_f419328")
        self.assertNotEqual(proc.returncode, 0)

    def test_refuses_a_tag_the_checkout_does_not_have(self):
        proc = self._run(CANDIDATE_SHA=self.commits[2], ROLLBACK_TAG="0.6.0")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not in this checkout", proc.stderr)


if __name__ == "__main__":
    unittest.main()
