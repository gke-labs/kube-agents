"""Unit tests for scripts/release/promote_release_images.sh.

Tests argument validation, pure numeric SemVer enforcement, and image promotion
execution with mock Docker CLI fixtures.
"""

import pathlib
import subprocess
import unittest

from tests.testing.common import (
    MOCK_DEFAULT_REGISTRY_PREFIX,
    create_mock_git_repo,
    get_isolated_test_env,
)
from tests.testing.release import (
    INVALID_GA_RELEASE_TAGS,
    MOCK_ADDITIONAL_RELEASE_REGISTRY_PREFIX,
    MOCK_AR_REGISTRY_PREFIX,
    MOCK_CANDIDATE_RELEASE_IMAGES,
    MOCK_REQUIRED_RELEASE_IMAGES,
    MOCK_TARGET_RELEASE_TAG,
    commit_required_release_images,
    create_mock_docker_binary,
    create_mock_gcloud_token_binary,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PROMOTE_RELEASE_IMAGES_SH = _REPO_ROOT / "scripts" / "release" / "promote_release_images.sh"


class PromoteReleaseImagesScriptTest(unittest.TestCase):
    def _run_script(self, args, env=None, bin_dir=None, cwd=None):
        full_env = get_isolated_test_env(overrides=env, bin_dir=bin_dir)
        return subprocess.run(
            ["bash", str(_PROMOTE_RELEASE_IMAGES_SH)] + args,
            capture_output=True,
            text=True,
            env=full_env,
            cwd=cwd or str(_REPO_ROOT),
        )

    def test_missing_arguments(self):
        proc = self._run_script([])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("RELEASE_VERSION is required as first argument or environment variable", proc.stderr)

    def test_invalid_tag_format(self):
        for bad_tag in INVALID_GA_RELEASE_TAGS:
            with self.subTest(bad_tag=bad_tag):
                proc = self._run_script([bad_tag])
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("not a valid pure numeric SemVer", proc.stderr)

    def test_local_dry_run_skips_promotion(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_docker_binary(bin_dir)
            git("tag", MOCK_TARGET_RELEASE_TAG, "HEAD")

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                bin_dir=str(bin_dir),
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Dry-run: Remote image promotion", proc.stdout)
            self.assertIn("skipped (runs only in CI)", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_promote_execution(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_docker_binary(bin_dir)

            candidate_commit = git("rev-parse", "HEAD").stdout.strip()
            git("checkout", "--detach", candidate_commit)
            dummy_file = pathlib.Path(repo_dir) / "version.txt"
            dummy_file.write_text("v0.2.0\n")
            git("add", "version.txt")
            git("commit", "-m", "chore: stamp 0.2.0")
            release_commit = git("rev-parse", "HEAD").stdout.strip()
            git("tag", MOCK_TARGET_RELEASE_TAG, release_commit)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={"CI": "true"},
                bin_dir=str(bin_dir),
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("PROMOTING RELEASE CONTAINER IMAGES", proc.stdout)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn(f"Promoting {img}", proc.stdout)
                self.assertIn(f"Promoted {img} to {MOCK_TARGET_RELEASE_TAG}", proc.stdout)
            docker_log = pathlib.Path(temp_dir.name) / "bin" / "docker.log"
            self.assertIn("--prefer-index=false", docker_log.read_text())
        finally:
            temp_dir.cleanup()

    def test_promote_iterates_the_candidates_own_list(self):
        """The candidate's common.sh names fewer images than this checkout's;
        those, and only those, are promoted (#2211). The stamp commit between
        the candidate and the tag changes no list, so the tag's parent reads
        the candidate's."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_docker_binary(bin_dir)

            candidate_commit = commit_required_release_images(repo_dir, git, MOCK_CANDIDATE_RELEASE_IMAGES)
            git("checkout", "--detach", candidate_commit)
            (pathlib.Path(repo_dir) / "version.txt").write_text("v0.2.0\n")
            git("add", "version.txt")
            git("commit", "-m", "chore(release): stamp release version 0.2.0")
            git("tag", MOCK_TARGET_RELEASE_TAG, git("rev-parse", "HEAD").stdout.strip())

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={"CI": "true"},
                bin_dir=str(bin_dir),
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(f"lists at {candidate_commit[:7]}", proc.stderr)
            for img in MOCK_CANDIDATE_RELEASE_IMAGES:
                self.assertIn(f"Promoted {img} to {MOCK_TARGET_RELEASE_TAG}", proc.stdout)
            for img in set(MOCK_REQUIRED_RELEASE_IMAGES) - set(MOCK_CANDIDATE_RELEASE_IMAGES):
                self.assertNotIn(f"Promoting {img}", proc.stdout, f"{img} is not in the candidate's list")
        finally:
            temp_dir.cleanup()

    def test_promote_idempotent_skip_when_target_exists(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            candidate_commit = git("rev-parse", "HEAD").stdout.strip()
            git("checkout", "--detach", candidate_commit)
            dummy_file = pathlib.Path(repo_dir) / "version.txt"
            dummy_file.write_text("v0.2.0\n")
            git("add", "version.txt")
            git("commit", "-m", "chore: stamp 0.2.0")
            release_commit = git("rev-parse", "HEAD").stdout.strip()
            git("tag", MOCK_TARGET_RELEASE_TAG, release_commit)

            existing = [
                f"ghcr.io/gke-labs/kube-agents/{img}:{MOCK_TARGET_RELEASE_TAG}"
                for img in MOCK_REQUIRED_RELEASE_IMAGES
            ] + [
                f"ghcr.io/gke-labs/kube-agents/{img}:{candidate_commit}"
                for img in MOCK_REQUIRED_RELEASE_IMAGES
            ]
            create_mock_docker_binary(bin_dir, existing_images=existing)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={"CI": "true"},
                bin_dir=str(bin_dir),
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn("already exists in registry and matches source image", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_promote_idempotent_skip_when_target_is_index_wrapping_source_manifest(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            candidate_commit = git("rev-parse", "HEAD").stdout.strip()
            git("checkout", "--detach", candidate_commit)
            dummy_file = pathlib.Path(repo_dir) / "version.txt"
            dummy_file.write_text("v0.2.0\n")
            git("add", "version.txt")
            git("commit", "-m", "chore: stamp 0.2.0")
            release_commit = git("rev-parse", "HEAD").stdout.strip()
            git("tag", MOCK_TARGET_RELEASE_TAG, release_commit)

            source_sha = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
            target_index_sha = "sha256:2222222222222222222222222222222222222222222222222222222222222222"
            raw_index = f'{{"mediaType":"application/vnd.oci.image.index.v1+json","digest":"{target_index_sha}","manifests":[{{"mediaType":"application/vnd.oci.image.manifest.v1+json","digest":"{source_sha}"}}]}}'
            digests = {}
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                digests[f"ghcr.io/gke-labs/kube-agents/{img}:{MOCK_TARGET_RELEASE_TAG}"] = {
                    "format": target_index_sha,
                    "raw": raw_index,
                }
                digests[f"ghcr.io/gke-labs/kube-agents/{img}:{candidate_commit}"] = {
                    "format": source_sha,
                    "raw": f'{{"mediaType":"application/vnd.oci.image.manifest.v1+json","digest":"{source_sha}"}}',
                }
            create_mock_docker_binary(bin_dir, image_digests=digests)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={"CI": "true"},
                bin_dir=str(bin_dir),
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn("already exists in registry and matches source image", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_promote_fails_when_target_exists_with_mismatched_digest(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            candidate_commit = git("rev-parse", "HEAD").stdout.strip()
            git("checkout", "--detach", candidate_commit)
            dummy_file = pathlib.Path(repo_dir) / "version.txt"
            dummy_file.write_text("v0.2.0\n")
            git("add", "version.txt")
            git("commit", "-m", "chore: stamp 0.2.0")
            release_commit = git("rev-parse", "HEAD").stdout.strip()
            git("tag", MOCK_TARGET_RELEASE_TAG, release_commit)

            digests = {}
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                digests[f"ghcr.io/gke-labs/kube-agents/{img}:{MOCK_TARGET_RELEASE_TAG}"] = (
                    "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                )
                digests[f"ghcr.io/gke-labs/kube-agents/{img}:{candidate_commit}"] = (
                    "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                )
            create_mock_docker_binary(bin_dir, image_digests=digests)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={"CI": "true"},
                bin_dir=str(bin_dir),
                cwd=repo_dir,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("does NOT match source image", proc.stderr)
            self.assertIn("Release promotion blocked", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_promote_execution_env_vars(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_docker_binary(bin_dir)
            git("tag", MOCK_TARGET_RELEASE_TAG, "HEAD")

            proc = self._run_script(
                [],
                env={"CI": "true", "RELEASE_VERSION": MOCK_TARGET_RELEASE_TAG},
                bin_dir=str(bin_dir),
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("PROMOTING RELEASE CONTAINER IMAGES", proc.stdout)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn(f"Promoted {img} to {MOCK_TARGET_RELEASE_TAG}", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_promote_execution_single_release_version_argument(self):
        """Verifies promote_release_images resolves candidate commit from tag's parent when only version is passed."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_docker_binary(bin_dir)

            # 1. Candidate commit on main
            candidate_commit = git("rev-parse", "HEAD").stdout.strip()

            # 2. Release commit on detached HEAD with tag
            git("checkout", "--detach", candidate_commit)
            dummy_file = pathlib.Path(repo_dir) / "version.txt"
            dummy_file.write_text("v0.2.0\n")
            git("add", "version.txt")
            git("commit", "-m", "chore(release): stamp 0.2.0")
            release_commit = git("rev-parse", "HEAD").stdout.strip()
            git("tag", MOCK_TARGET_RELEASE_TAG, release_commit)

            # 3. Call promote_release_images with only version argument
            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                env={"CI": "true"},
                bin_dir=str(bin_dir),
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("PROMOTING RELEASE CONTAINER IMAGES", proc.stdout)
            self.assertIn(f"Release Commit:  {candidate_commit}", proc.stdout)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn(f"Promoted {img} to {MOCK_TARGET_RELEASE_TAG}", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_promote_execution_direct_unstamped_tag_resolves_exact_commit(self):
        """Verifies promote_release_images resolves exact tag commit when tag is placed directly on a commit."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_docker_binary(bin_dir)

            direct_commit = git("rev-parse", "HEAD").stdout.strip()
            git("tag", MOCK_TARGET_RELEASE_TAG, direct_commit)

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG],
                bin_dir=str(bin_dir),
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("PROMOTING RELEASE CONTAINER IMAGES", proc.stdout)
            self.assertIn(f"Release Commit:  {direct_commit}", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_missing_release_tag_fails_fast_without_head_fallback(self):
        """Verifies promote_release_images fails fast with exit code 1 when tag does not exist (no HEAD fallback)."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_docker_binary(bin_dir)

            proc = self._run_script(
                ["9.9.9"],
                bin_dir=str(bin_dir),
                cwd=repo_dir,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Cannot resolve source image commit for version '9.9.9'", proc.stderr)
        finally:
            temp_dir.cleanup()


class PromoteFromCandidateRegistryTest(unittest.TestCase):
    """CANDIDATE_REGISTRY_PREFIX names where a release reads the commit's
    images when that is not where the release is published.

    Each image is then tagged with the version twice from the one source: in
    the candidate registry, then in the registry the release is published to.
    Unset, or naming the registry prefix itself, promotion is the single tag in
    one registry it was before the variable existed.
    """

    _SOURCE_DIGEST = "sha256:" + "a" * 64
    _OTHER_DIGEST = "sha256:" + "b" * 64

    def setUp(self):
        self.temp_dir, self.repo_dir, git = create_mock_git_repo()
        self.addCleanup(self.temp_dir.cleanup)
        self.bin_dir = pathlib.Path(self.temp_dir.name) / "bin"
        self.candidate = git("rev-parse", "HEAD").stdout.strip()
        git("checkout", "--detach", self.candidate)
        (pathlib.Path(self.repo_dir) / "version.txt").write_text("v0.2.0\n")
        git("add", "version.txt")
        git("commit", "-m", "chore(release): stamp release version 0.2.0")
        git("tag", MOCK_TARGET_RELEASE_TAG, git("rev-parse", "HEAD").stdout.strip())

    def _candidate_ref(self, img, tag):
        return f"{MOCK_AR_REGISTRY_PREFIX}/{img}:{tag}"

    def _release_ref(self, img):
        return f"{MOCK_DEFAULT_REGISTRY_PREFIX}/{img}:{MOCK_TARGET_RELEASE_TAG}"

    def _candidate_sources(self):
        return {self._candidate_ref(img, self.candidate): self._SOURCE_DIGEST for img in MOCK_REQUIRED_RELEASE_IMAGES}

    def _additional_ref(self, img):
        return f"{MOCK_ADDITIONAL_RELEASE_REGISTRY_PREFIX}/{img}:{MOCK_TARGET_RELEASE_TAG}"

    def _promote(self, candidate_registry, images, additional_registry=None):
        """Runs the script against a registry holding `images`; returns the
        process and the (target, source) pair of each tag it created, in order."""
        _, log_path = create_mock_docker_binary(self.bin_dir, image_digests=images)
        # No Google credential, so the Artifact Registry probe cannot ask and
        # docker, which the release job logs in, answers.
        create_mock_gcloud_token_binary(self.bin_dir, token="")
        env = {"CI": "true", "REGISTRY_PREFIX": MOCK_DEFAULT_REGISTRY_PREFIX}
        if candidate_registry is not None:
            env["CANDIDATE_REGISTRY_PREFIX"] = candidate_registry
        if additional_registry is not None:
            env["ADDITIONAL_RELEASE_REGISTRY_PREFIX"] = additional_registry
        proc = subprocess.run(
            ["bash", str(_PROMOTE_RELEASE_IMAGES_SH), MOCK_TARGET_RELEASE_TAG],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(overrides=env, bin_dir=str(self.bin_dir)),
            cwd=self.repo_dir,
        )
        created = []
        log = pathlib.Path(log_path)
        for line in log.read_text().splitlines() if log.exists() else []:
            words = line.split()
            if "create" in words:
                created.append((words[words.index("--tag") + 1], words[-1]))
        return proc, created

    def test_each_image_is_tagged_in_the_candidate_registry_then_published(self):
        proc, created = self._promote(MOCK_AR_REGISTRY_PREFIX, self._candidate_sources())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        expected = []
        for img in MOCK_REQUIRED_RELEASE_IMAGES:
            source = self._candidate_ref(img, self.candidate)
            expected.append((self._candidate_ref(img, MOCK_TARGET_RELEASE_TAG), source))
            expected.append((self._release_ref(img), source))
        self.assertEqual(created, expected)

    def test_a_trailing_slash_on_the_variable_names_the_same_registry(self):
        proc, created = self._promote(MOCK_AR_REGISTRY_PREFIX + "/", self._candidate_sources())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(created), 2 * len(MOCK_REQUIRED_RELEASE_IMAGES))
        for target, source in created:
            self.assertNotIn("//", target)
            self.assertNotIn("//", source)

    def test_unset_or_naming_the_registry_prefix_is_one_tag_in_one_registry(self):
        for candidate_registry in (None, "", MOCK_DEFAULT_REGISTRY_PREFIX):
            with self.subTest(candidate_registry=candidate_registry):
                proc, created = self._promote(candidate_registry, {})
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(
                    created,
                    [
                        (self._release_ref(img), f"{MOCK_DEFAULT_REGISTRY_PREFIX}/{img}:{self.candidate}")
                        for img in MOCK_REQUIRED_RELEASE_IMAGES
                    ],
                )
                (self.bin_dir / "docker.log").unlink()

    def test_a_rerun_after_the_candidate_tags_only_publishes(self):
        """A first run that stopped between the two tags is resumed, not redone."""
        images = self._candidate_sources()
        for img in MOCK_REQUIRED_RELEASE_IMAGES:
            images[self._candidate_ref(img, MOCK_TARGET_RELEASE_TAG)] = self._SOURCE_DIGEST
        proc, created = self._promote(MOCK_AR_REGISTRY_PREFIX, images)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            created,
            [(self._release_ref(img), self._candidate_ref(img, self.candidate)) for img in MOCK_REQUIRED_RELEASE_IMAGES],
        )

    def test_a_version_already_on_another_image_in_the_candidate_registry_publishes_nothing(self):
        images = self._candidate_sources()
        first = MOCK_REQUIRED_RELEASE_IMAGES[0]
        images[self._candidate_ref(first, MOCK_TARGET_RELEASE_TAG)] = self._OTHER_DIGEST
        proc, created = self._promote(MOCK_AR_REGISTRY_PREFIX, images)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("does NOT match source image", proc.stderr)
        self.assertIn(self._candidate_ref(first, MOCK_TARGET_RELEASE_TAG), proc.stderr)
        self.assertEqual(created, [])

    def test_a_published_version_on_another_image_is_refused(self):
        images = self._candidate_sources()
        first = MOCK_REQUIRED_RELEASE_IMAGES[0]
        images[self._release_ref(first)] = self._OTHER_DIGEST
        proc, created = self._promote(MOCK_AR_REGISTRY_PREFIX, images)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("does NOT match source image", proc.stderr)
        self.assertIn(self._release_ref(first), proc.stderr)
        self.assertEqual(
            created,
            [(self._candidate_ref(first, MOCK_TARGET_RELEASE_TAG), self._candidate_ref(first, self.candidate))],
        )

    def test_an_additional_release_registry_gets_the_same_images_last(self):
        """Three tags per image from one source. Every image is in the
        candidate registry and the registry prefix before the first is written
        to the additional release registry, so a failure there leaves the
        registry prefix whole."""
        proc, created = self._promote(
            MOCK_AR_REGISTRY_PREFIX, self._candidate_sources(), additional_registry=MOCK_ADDITIONAL_RELEASE_REGISTRY_PREFIX + "/"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        expected = []
        for img in MOCK_REQUIRED_RELEASE_IMAGES:
            source = self._candidate_ref(img, self.candidate)
            expected.append((self._candidate_ref(img, MOCK_TARGET_RELEASE_TAG), source))
            expected.append((self._release_ref(img), source))
        for img in MOCK_REQUIRED_RELEASE_IMAGES:
            expected.append((self._additional_ref(img), self._candidate_ref(img, self.candidate)))
        self.assertEqual(created, expected)

    def test_an_additional_release_registry_without_a_candidate_registry(self):
        """The source is then the registry prefix's own commit tag."""
        proc, created = self._promote(None, {}, additional_registry=MOCK_ADDITIONAL_RELEASE_REGISTRY_PREFIX)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        sources = [f"{MOCK_DEFAULT_REGISTRY_PREFIX}/{img}:{self.candidate}" for img in MOCK_REQUIRED_RELEASE_IMAGES]
        expected = [(self._release_ref(img), source) for img, source in zip(MOCK_REQUIRED_RELEASE_IMAGES, sources)]
        expected += [(self._additional_ref(img), source) for img, source in zip(MOCK_REQUIRED_RELEASE_IMAGES, sources)]
        self.assertEqual(created, expected)

    def test_an_additional_release_registry_naming_the_registry_prefix_adds_nothing(self):
        for additional in ("", MOCK_DEFAULT_REGISTRY_PREFIX, MOCK_DEFAULT_REGISTRY_PREFIX + "/"):
            with self.subTest(additional=additional):
                proc, created = self._promote(None, {}, additional_registry=additional)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(len(created), len(MOCK_REQUIRED_RELEASE_IMAGES))
                (self.bin_dir / "docker.log").unlink()

    def test_a_version_on_another_image_in_the_additional_registry_is_refused(self):
        images = self._candidate_sources()
        first = MOCK_REQUIRED_RELEASE_IMAGES[0]
        images[self._additional_ref(first)] = self._OTHER_DIGEST
        proc, created = self._promote(
            MOCK_AR_REGISTRY_PREFIX, images, additional_registry=MOCK_ADDITIONAL_RELEASE_REGISTRY_PREFIX
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("does NOT match source image", proc.stderr)
        self.assertIn(self._additional_ref(first), proc.stderr)
        self.assertEqual(len(created), 2 * len(MOCK_REQUIRED_RELEASE_IMAGES), "the registry prefix is whole")
        self.assertNotIn(MOCK_ADDITIONAL_RELEASE_REGISTRY_PREFIX, " ".join(target for target, _ in created))

    def test_the_source_commit_is_found_by_its_images_in_the_candidate_registry(self):
        """A tag whose commit is not a stamp commit resolves by where the
        images are: the tag's own commit when it has them, else its parent
        when that does. Both lookups have to read the registry the images are
        in."""
        for built in ("parent", "both"):
            with self.subTest(images_at=built):
                self._assert_source_commit(built)

    def _assert_source_commit(self, built):
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        bin_dir = pathlib.Path(temp_dir.name) / "bin"
        parent = git("rev-parse", "HEAD").stdout.strip()
        (pathlib.Path(repo_dir) / "note.txt").write_text("after the candidate\n")
        git("add", "note.txt")
        git("commit", "-m", "docs: a commit that is not a release stamp")
        tagged = git("rev-parse", "HEAD").stdout.strip()
        git("tag", MOCK_TARGET_RELEASE_TAG, "HEAD")
        holders = [parent] if built == "parent" else [parent, tagged]
        expected = parent if built == "parent" else tagged
        create_mock_docker_binary(
            bin_dir,
            existing_images=[
                f"{MOCK_AR_REGISTRY_PREFIX}/{img}:{sha}" for img in MOCK_REQUIRED_RELEASE_IMAGES for sha in holders
            ],
        )
        create_mock_gcloud_token_binary(bin_dir, token="")
        proc = subprocess.run(
            ["bash", str(_PROMOTE_RELEASE_IMAGES_SH), MOCK_TARGET_RELEASE_TAG],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(
                overrides={
                    "CI": "true",
                    "REGISTRY_PREFIX": MOCK_DEFAULT_REGISTRY_PREFIX,
                    "CANDIDATE_REGISTRY_PREFIX": MOCK_AR_REGISTRY_PREFIX,
                },
                bin_dir=str(bin_dir),
            ),
            cwd=repo_dir,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"Release Commit:  {expected}", proc.stdout)


if __name__ == "__main__":
    unittest.main()
