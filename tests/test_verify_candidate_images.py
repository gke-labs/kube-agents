"""Unit tests for scripts/release/verify_candidate_images.sh.

The script asks the registry for every required image at the candidate
commit. The list it asks for is the candidate's own: the names
`scripts/release/common.sh` carries at that commit, which is what the
candidate's publish run built. A checkout whose list has since grown must not
refuse a candidate for an image that did not exist when it was published.
"""

import pathlib
import subprocess
import unittest

from tests.testing.common import MOCK_DEFAULT_REGISTRY_PREFIX, create_mock_git_repo, get_isolated_test_env
from tests.testing.release import (
    MOCK_CANDIDATE_RELEASE_IMAGES,
    MOCK_REQUIRED_RELEASE_IMAGES,
    commit_required_release_images,
    create_mock_docker_binary,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_VERIFY_CANDIDATE_IMAGES_SH = _REPO_ROOT / "scripts" / "release" / "verify_candidate_images.sh"


class VerifyCandidateImagesScriptTest(unittest.TestCase):
    def _run_script(self, args, cwd, bin_dir, env=None):
        full_env = get_isolated_test_env(overrides=env, bin_dir=bin_dir)
        return subprocess.run(
            ["bash", str(_VERIFY_CANDIDATE_IMAGES_SH)] + args,
            capture_output=True,
            text=True,
            env=full_env,
            cwd=cwd,
        )

    def test_missing_commit_sha_fails(self):
        temp_dir, repo_dir, _ = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        bin_dir = pathlib.Path(temp_dir.name) / "bin"
        create_mock_docker_binary(bin_dir)
        proc = self._run_script([], cwd=repo_dir, bin_dir=str(bin_dir))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("COMMIT_SHA is required", proc.stderr)

    def test_the_candidates_own_list_is_what_is_verified(self):
        """A candidate from before the list grew has only its own images, and
        that is enough: the registry is asked for the names its common.sh
        lists, not the names this checkout's lists."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        candidate = commit_required_release_images(repo_dir, git, MOCK_CANDIDATE_RELEASE_IMAGES)
        bin_dir = pathlib.Path(temp_dir.name) / "bin"
        create_mock_docker_binary(
            bin_dir,
            existing_images=[f"{MOCK_DEFAULT_REGISTRY_PREFIX}/{img}:{candidate}" for img in MOCK_CANDIDATE_RELEASE_IMAGES],
        )

        proc = self._run_script([candidate], cwd=repo_dir, bin_dir=str(bin_dir), env={"REGISTRY_PREFIX": MOCK_DEFAULT_REGISTRY_PREFIX})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("All candidate container images verified", proc.stdout)
        self.assertIn(f"lists at {candidate[:7]}", proc.stderr)
        for img in MOCK_CANDIDATE_RELEASE_IMAGES:
            self.assertIn(f"Checking image '{MOCK_DEFAULT_REGISTRY_PREFIX}/{img}:{candidate}'", proc.stdout)
        for img in set(MOCK_REQUIRED_RELEASE_IMAGES) - set(MOCK_CANDIDATE_RELEASE_IMAGES):
            self.assertNotIn(f"/{img}:", proc.stdout, f"{img} is not in the candidate's list and must not be asked for")

    def test_a_missing_image_from_the_candidates_list_still_fails(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        candidate = commit_required_release_images(repo_dir, git, MOCK_CANDIDATE_RELEASE_IMAGES)
        bin_dir = pathlib.Path(temp_dir.name) / "bin"
        present = MOCK_CANDIDATE_RELEASE_IMAGES[:-1]
        missing = MOCK_CANDIDATE_RELEASE_IMAGES[-1]
        create_mock_docker_binary(
            bin_dir,
            existing_images=[f"{MOCK_DEFAULT_REGISTRY_PREFIX}/{img}:{candidate}" for img in present],
        )

        proc = self._run_script([candidate], cwd=repo_dir, bin_dir=str(bin_dir), env={"REGISTRY_PREFIX": MOCK_DEFAULT_REGISTRY_PREFIX})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(f"Container image '{missing}' for commit '{candidate}' not found", proc.stderr)

    def test_a_candidate_without_a_list_is_held_to_this_checkouts(self):
        """The fallback: a commit with no scripts/release/common.sh is asked for
        every name this checkout lists, and the notice says so."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        candidate = git("rev-parse", "HEAD").stdout.strip()
        bin_dir = pathlib.Path(temp_dir.name) / "bin"
        create_mock_docker_binary(
            bin_dir,
            existing_images=[f"{MOCK_DEFAULT_REGISTRY_PREFIX}/{img}:{candidate}" for img in MOCK_REQUIRED_RELEASE_IMAGES],
        )

        proc = self._run_script([candidate], cwd=repo_dir, bin_dir=str(bin_dir), env={"REGISTRY_PREFIX": MOCK_DEFAULT_REGISTRY_PREFIX})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("has no scripts/release/common.sh", proc.stderr)
        for img in MOCK_REQUIRED_RELEASE_IMAGES:
            self.assertIn(f"Checking image '{MOCK_DEFAULT_REGISTRY_PREFIX}/{img}:{candidate}'", proc.stdout)


if __name__ == "__main__":
    unittest.main()
