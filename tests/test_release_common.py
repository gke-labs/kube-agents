"""Unit tests for scripts/release/common.sh helper routines and registries.

Tests boolean parsing, SemVer validation, SemVer comparison, repository and registry prefix
resolution, Git tag lookup, and declarative release registries.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

from tests.testing.common import (
    FALSY_BOOLEAN_INPUTS,
    MOCK_CUSTOM_ORG,
    MOCK_CUSTOM_REGISTRY_PREFIX,
    MOCK_CUSTOM_REPO,
    MOCK_CUSTOM_TARGET_REPO,
    MOCK_DEFAULT_REGISTRY_PREFIX,
    MOCK_DEFAULT_RELEASE_REPO,
    TRUTHY_BOOLEAN_INPUTS,
    VALID_GA_RELEASE_TAGS,
    create_minimal_tools_bin,
    create_mock_git_repo,
    get_isolated_test_env,
)
from tests.testing.release import (
    INVALID_GA_RELEASE_TAGS,
    MOCK_REQUIRED_RELEASE_IMAGES,
    MOCK_SAMPLE_COMMIT_SHA,
    MOCK_SAMPLE_SHORT_SHA,
    MOCK_TARGET_RELEASE_TAG,
    create_mock_docker_binary,
    create_mock_ghcr_curl_binary,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_COMMON_SH = _REPO_ROOT / "scripts" / "release" / "common.sh"


class ReleaseCommonTest(unittest.TestCase):
    def _run_common_func(self, func_call, env=None, bin_dir=None, cwd=None):
        """Source common.sh and execute the given bash snippet."""
        setup = f"""
source "{_COMMON_SH}"
{func_call}
"""
        full_env = get_isolated_test_env(overrides=env, bin_dir=bin_dir)
        return subprocess.run(
            ["bash", "-c", setup],
            capture_output=True,
            text=True,
            env=full_env,
            cwd=cwd or str(_REPO_ROOT),
        )

    def test_is_truthy(self):
        for val in TRUTHY_BOOLEAN_INPUTS:
            with self.subTest(val=val):
                proc = self._run_common_func(f'is_truthy "{val}"')
                self.assertEqual(proc.returncode, 0, f"Expected '{val}' to be truthy")

        for val in FALSY_BOOLEAN_INPUTS:
            with self.subTest(val=val):
                proc = self._run_common_func(f'is_truthy "{val}"')
                self.assertNotEqual(proc.returncode, 0, f"Expected '{val}' to be falsy")

    def test_validate_pure_numeric_semver(self):
        for tag in VALID_GA_RELEASE_TAGS:
            with self.subTest(tag=tag):
                proc = self._run_common_func(f'validate_pure_numeric_semver "{tag}"')
                self.assertEqual(proc.returncode, 0)

        for bad_tag in INVALID_GA_RELEASE_TAGS:
            with self.subTest(bad_tag=bad_tag):
                proc = self._run_common_func(f'validate_pure_numeric_semver "{bad_tag}"')
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("not a valid pure numeric SemVer", proc.stderr)

    def test_compare_semver(self):
        test_cases = [
            ("0.2.0", "0.1.0", "1"),
            ("0.1.1", "0.1.0", "1"),
            ("1.0.0", "0.9.9", "1"),
            ("0.2.0", "0.2.0", "0"),
            ("0.1.0", "0.2.0", "-1"),
            ("0.1.0", "0.1.1", "-1"),
            ("0.9.9", "1.0.0", "-1"),
        ]
        for v1, v2, expected in test_cases:
            with self.subTest(v1=v1, v2=v2):
                proc = self._run_common_func(f'compare_semver "{v1}" "{v2}"')
                self.assertEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout.strip(), expected)

    def test_get_latest_ga_tag(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            # Initially no tags
            proc = self._run_common_func('get_latest_ga_tag', cwd=repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "")

            # Initially no tags, explicit fallback provided
            proc_default = self._run_common_func('get_latest_ga_tag "0.1.0"', cwd=repo_dir)
            self.assertEqual(proc_default.returncode, 0)
            self.assertEqual(proc_default.stdout.strip(), "0.1.0")

            # Add mixed tags
            git("tag", "-a", "0.1.0", "-m", "Release 0.1.0")
            git("tag", "-a", "0.2.0", "-m", "Release 0.2.0")
            git("tag", "-a", "0.1.5", "-m", "Release 0.1.5")
            git("tag", "-a", "rc_0.3.0_validated", "-m", "RC tag")
            git("tag", "-a", "v1.0.0", "-m", "v-tag")

            proc = self._run_common_func('get_latest_ga_tag', cwd=repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "0.2.0")
        finally:
            temp_dir.cleanup()

    def test_get_latest_validated_rc_tag(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            # Initially no validated tags
            proc = self._run_common_func('get_latest_validated_rc_tag', cwd=repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "")

            # Add mixed tags including older and newer validated RC tags
            git("tag", "-a", "rc_2608181000_1111111_validated", "-m", "Older RC")
            git("tag", "-a", "rc_2608191200_2222222_validated", "-m", "Newer RC")
            git("tag", "-a", "rc_2608191300_3333333", "-m", "Unvalidated RC")
            git("tag", "-a", "0.2.0", "-m", "GA tag")

            proc = self._run_common_func('get_latest_validated_rc_tag', cwd=repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "rc_2608191200_2222222_validated")
        finally:
            temp_dir.cleanup()

    def test_get_latest_staging_tag_matches_the_shape_not_the_prefix(self):
        """`staging_*` is a deploy trigger anyone can push; the GA gate reads this.

        `staging-redeploy-*.yml` fires on the bare prefix, so hand-made trigger
        tags are a supported thing to have in the graph. Matching the prefix here
        would let one of them read back as "the full nightly matrix passed on
        this commit", which is the only evidence a GA release has.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            proc = self._run_common_func("get_latest_staging_tag", cwd=repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "")

            git("tag", "-a", "staging_2608181000_1111111", "-m", "Older promotion")
            git("tag", "-a", "staging_2608191200_2222222", "-m", "Newer promotion")
            # Sorts newest-first, so a hand-made tag must not win by name alone.
            git("tag", "-a", "staging_zzzz", "-m", "Hand-made trigger")
            git("tag", "-a", "staging_hotfix", "-m", "Hand-made trigger")
            git("tag", "-a", "rc_2608191300_3333333_validated", "-m", "RC only")

            proc = self._run_common_func("get_latest_staging_tag", cwd=repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "staging_2608191200_2222222")
        finally:
            temp_dir.cleanup()

    def test_get_latest_staging_tag_agrees_with_staging_tag_for_rc(self):
        """The shape is derived, not declared: a real promotion has to match it.

        STAGING_TAG_SHAPE_REGEX and staging_tag_for_rc are two spellings of the
        same format. If either moves without the other, the nightly pipeline
        pushes tags the release gate cannot see and GA releases stop silently.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            proc = self._run_common_func(
                'staging_tag_for_rc "rc_2608241820_b35543c_validated"', cwd=repo_dir
            )
            derived = proc.stdout.strip()
            self.assertEqual(derived, "staging_2608241820_b35543c")

            git("tag", "-a", derived, "-m", "Promoted")
            proc = self._run_common_func("get_latest_staging_tag", cwd=repo_dir)
            self.assertEqual(proc.stdout.strip(), derived)
        finally:
            temp_dir.cleanup()

    def test_staging_promotion_tags_at_commit_filters_by_shape(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            head = git("rev-parse", "HEAD").stdout.strip()
            git("tag", "-a", "staging_hotfix", head, "-m", "Hand-made trigger")

            proc = self._run_common_func(
                f'staging_promotion_tags_at_commit "{head}"', cwd=repo_dir
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "")

            git("tag", "-a", "staging_2608191200_2222222", head, "-m", "Promoted")
            proc = self._run_common_func(
                f'staging_promotion_tags_at_commit "{head}"', cwd=repo_dir
            )
            self.assertEqual(proc.stdout.strip(), "staging_2608191200_2222222")
        finally:
            temp_dir.cleanup()

    def test_the_promotion_check_and_the_release_gate_agree_on_one_commit(self):
        """A tag one of them counts and the other does not makes a candidate unshippable.

        `get_existing_staging_tag` sets `skip_promotion` in
        `resolve_promotion_candidate.sh`; `staging_promotion_tags_at_commit` is what
        the release gate reads. Let the first count a hand-pushed `staging_hotfix`
        and the nightly concludes the commit is already promoted, so it never
        pushes the real tag — while the gate, matching on shape, reads the same
        commit as never promoted. Nothing is red and the candidate quietly cannot
        be released.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            head = git("rev-parse", "HEAD").stdout.strip()
            git("tag", "-a", "staging_hotfix", head, "-m", "Hand-made trigger")

            existing = self._run_common_func(
                f'get_existing_staging_tag "{head}"', cwd=repo_dir
            ).stdout.strip()
            gate = self._run_common_func(
                f'staging_promotion_tags_at_commit "{head}"', cwd=repo_dir
            ).stdout.strip()
            self.assertEqual(existing, gate, "the two lookups disagree on a prefix-only tag")
            self.assertEqual(existing, "")

            git("tag", "-a", "staging_2608191200_2222222", head, "-m", "Promoted")
            existing = self._run_common_func(
                f'get_existing_staging_tag "{head}"', cwd=repo_dir
            ).stdout.strip()
            self.assertEqual(existing, "staging_2608191200_2222222")
        finally:
            temp_dir.cleanup()

    def test_is_rc_candidate_commit_already_validated_is_anchored_to_the_rc_family(self):
        """The glob has to be rc_*_validated, not *_validated.

        This function gates resolve_rc_tag.sh's skip decision, so a validation
        marker from another tag family matching it would make the RC pipeline
        skip a candidate it never validated. staging_* is the family that made
        this concrete, but the point is general.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            head = git("rev-parse", "HEAD").stdout.strip()

            proc = self._run_common_func(f'is_rc_candidate_commit_already_validated "{head}"', cwd=repo_dir)
            self.assertNotEqual(proc.returncode, 0)

            git("tag", "-a", "someone_elses_validated", "-m", "Not an RC marker")
            proc = self._run_common_func(f'is_rc_candidate_commit_already_validated "{head}"', cwd=repo_dir)
            self.assertNotEqual(proc.returncode, 0, "a non-rc_ tag ending _validated must not count")

            git("tag", "-a", "rc_2608191200_2222222_validated", "-m", "RC marker")
            proc = self._run_common_func(f'is_rc_candidate_commit_already_validated "{head}"', cwd=repo_dir)
            self.assertEqual(proc.returncode, 0)
        finally:
            temp_dir.cleanup()

    def test_staging_tag_for_rc(self):
        cases = [
            ("rc_2608241820_b35543c_validated", "staging_2608241820_b35543c"),
            # The suffix is optional: the unvalidated tag maps to the same name,
            # which is what makes the transform reversible.
            ("rc_2608241820_b35543c", "staging_2608241820_b35543c"),
        ]
        for rc_tag, expected in cases:
            with self.subTest(rc_tag=rc_tag):
                proc = self._run_common_func(f'staging_tag_for_rc "{rc_tag}"')
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.strip(), expected)

    def test_staging_tag_for_rc_refuses_anything_outside_the_rc_family(self):
        """The output is a live deploy trigger, so a typo must not compose one."""
        for bad in ("", "0.2.0", "staging_2608241820_b35543c", "rc_", "not-a-tag"):
            with self.subTest(bad=bad):
                proc = self._run_common_func(f'staging_tag_for_rc "{bad}"')
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout.strip(), "")

    def test_get_existing_staging_tag(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            head = git("rev-parse", "HEAD").stdout.strip()

            proc = self._run_common_func(f'get_existing_staging_tag "{head}"', cwd=repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "")

            # A staging tag on a DIFFERENT commit must not answer for this one.
            (pathlib.Path(repo_dir) / "second.txt").write_text("second\n")
            git("add", "second.txt")
            git("commit", "-m", "chore: second commit")
            other = git("rev-parse", "HEAD").stdout.strip()
            git("tag", "-a", "staging_2608241820_b35543c", "-m", "Promoted elsewhere")

            proc = self._run_common_func(f'get_existing_staging_tag "{head}"', cwd=repo_dir)
            self.assertEqual(proc.stdout.strip(), "")

            proc = self._run_common_func(f'get_existing_staging_tag "{other}"', cwd=repo_dir)
            self.assertEqual(proc.stdout.strip(), "staging_2608241820_b35543c")
        finally:
            temp_dir.cleanup()

    def _repo_with_staging_trigger(self, patterns):
        """A mock repo whose HEAD carries staging-redeploy-agent.yml with `patterns`."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        workflow = pathlib.Path(repo_dir) / ".github" / "workflows"
        workflow.mkdir(parents=True, exist_ok=True)
        rendered = "\n".join(f'      - "{p}"' for p in patterns)
        (workflow / "staging-redeploy-agent.yml").write_text(
            "name: Staging Redeploy Agent\n\non:\n  push:\n    tags:\n" + rendered + "\n\njobs: {}\n"
        )
        git("add", "-A")
        git("commit", "-m", "chore: staging trigger")
        return repo_dir, git("rev-parse", "HEAD").stdout.strip()

    def _trigger_matches(self, repo_dir, commit, tag):
        proc = self._run_common_func(
            f'staging_trigger_matches_at_commit "{commit}" "{tag}"', cwd=repo_dir
        )
        return proc.returncode

    def test_staging_trigger_matches_the_tag_the_promotion_pushes(self):
        repo_dir, head = self._repo_with_staging_trigger(["staging_*"])
        self.assertEqual(self._trigger_matches(repo_dir, head, "staging_2608241820_b35543c"), 0)

    def test_staging_trigger_rejects_a_commit_that_predates_the_rename(self):
        """The whole reason the helper exists.

        A push event runs the workflows in the pushed ref's tree. A candidate
        still declaring `staging/**` does not match a flat `staging_<ts>_<sha>`,
        so the promotion would deploy nothing and report success.
        """
        repo_dir, head = self._repo_with_staging_trigger(["staging/**"])
        self.assertNotEqual(self._trigger_matches(repo_dir, head, "staging_2608241820_b35543c"), 0)
        # The same tree does answer the tag shape it was written for.
        self.assertEqual(self._trigger_matches(repo_dir, head, "staging/2026-07-23"), 0)

    def test_staging_trigger_matches_when_any_listed_pattern_does(self):
        repo_dir, head = self._repo_with_staging_trigger(["staging/**", "staging_*"])
        self.assertEqual(self._trigger_matches(repo_dir, head, "staging_2608241820_b35543c"), 0)

    def test_staging_trigger_refuses_when_the_workflow_is_absent(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        head = git("rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(self._trigger_matches(repo_dir, head, "staging_2608241820_b35543c"), 0)

    def _repo_with_pipeline_markers(self, optional_runner=True, suite_selector=True,
                                    reconciler=True):
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        release = pathlib.Path(repo_dir) / "scripts" / "release"
        release.mkdir(parents=True, exist_ok=True)
        if optional_runner:
            (release / "run_optional_e2e_suites.sh").write_text("#!/usr/bin/env bash\n")
        selector = "E2E_SUITE" if suite_selector else "E2E_ENV"
        (release / "execute_e2e_tests.py").write_text(f'_VAR = "{selector}"\n')
        if reconciler:
            (release / "reconcile_environment.sh").write_text("#!/usr/bin/env bash\n")
        git("add", "-A")
        git("commit", "-m", "chore: pipeline markers")
        return repo_dir, git("rev-parse", "HEAD").stdout.strip()

    def _supports_shared_pipeline(self, repo_dir, commit):
        return self._run_common_func(
            f'candidate_supports_shared_pipeline "{commit}"', cwd=repo_dir
        ).returncode

    def test_a_restructured_candidate_supports_the_shared_pipeline(self):
        repo_dir, head = self._repo_with_pipeline_markers()
        self.assertEqual(self._supports_shared_pipeline(repo_dir, head), 0)

    def test_a_candidate_without_the_optional_runner_does_not(self):
        repo_dir, head = self._repo_with_pipeline_markers(optional_runner=False)
        self.assertNotEqual(self._supports_shared_pipeline(repo_dir, head), 0)

    def test_a_candidate_reading_only_the_old_selector_does_not(self):
        """The silent half: the gate would run the runner's default suite instead."""
        repo_dir, head = self._repo_with_pipeline_markers(suite_selector=False)
        self.assertNotEqual(self._supports_shared_pipeline(repo_dir, head), 0)

    def test_a_candidate_without_the_reconciler_does_not(self):
        """The nightly checks the candidate OUT to reconcile staging at it.

        A tree without reconcile_environment.sh aborts that step on a missing
        file, and the promotion is deliberately decoupled from its outcome — so
        the staging tag goes out, staging's images move, and its infrastructure
        stays exactly as stale as before. Silent in the way this gate exists to
        catch.
        """
        repo_dir, head = self._repo_with_pipeline_markers(reconciler=False)
        self.assertNotEqual(self._supports_shared_pipeline(repo_dir, head), 0)

    def test_candidate_support_requires_a_commit(self):
        repo_dir, _ = self._repo_with_pipeline_markers()
        proc = self._run_common_func('candidate_supports_shared_pipeline ""', cwd=repo_dir)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("a commit is required", proc.stderr)

    def test_staging_trigger_requires_both_arguments(self):
        repo_dir, head = self._repo_with_staging_trigger(["staging_*"])
        proc = self._run_common_func('staging_trigger_matches_at_commit "" ""', cwd=repo_dir)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("a commit and a tag are required", proc.stderr)

    def test_release_fetch_tags_is_a_no_op_outside_ci(self):
        """It must not reach the network on a developer machine.

        The CI arm cannot be exercised hermetically — it fetches a real URL — so
        what is pinned here is the guard in front of it. Without the guard, every
        script that calls this would try to hit github.com from a unit test.
        """
        temp_dir, repo_dir, _ = create_mock_git_repo()
        try:
            proc = self._run_common_func("release_fetch_tags", env={"CI": ""}, cwd=repo_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "")

            # And it stays quiet rather than failing when the fetch cannot work:
            # a fetch that could not run is not itself the error, the caller's
            # own lookup afterwards is.
            proc = self._run_common_func(
                "release_fetch_tags",
                env={"CI": "true", "GH_ORG": "no-such-org-kube-agents", "GH_REPO": "no-such-repo"},
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_get_target_repo(self):
        # Default
        proc = self._run_common_func('get_target_repo', env={"GH_ORG": "", "GH_REPO": "", "GITHUB_REPOSITORY": ""})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), MOCK_DEFAULT_RELEASE_REPO)

        # Via GITHUB_REPOSITORY
        proc = self._run_common_func('get_target_repo', env={"GITHUB_REPOSITORY": MOCK_CUSTOM_TARGET_REPO})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), MOCK_CUSTOM_TARGET_REPO)

        # Via GH_ORG and GH_REPO
        proc = self._run_common_func('get_target_repo', env={"GH_ORG": MOCK_CUSTOM_ORG, "GH_REPO": MOCK_CUSTOM_REPO})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), f"{MOCK_CUSTOM_ORG}/{MOCK_CUSTOM_REPO}")

    def test_get_registry_prefix(self):
        # Default
        proc = self._run_common_func('get_registry_prefix', env={"REGISTRY_PREFIX": "", "GH_ORG": "", "GH_REPO": "", "GITHUB_REPOSITORY": ""})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), MOCK_DEFAULT_REGISTRY_PREFIX)

        # Explicit REGISTRY_PREFIX
        proc = self._run_common_func('get_registry_prefix', env={"REGISTRY_PREFIX": MOCK_CUSTOM_REGISTRY_PREFIX})
        self.assertEqual(proc.stdout.strip(), MOCK_CUSTOM_REGISTRY_PREFIX)

    def test_required_release_images_registry(self):
        cmd = 'echo "IMAGES=${REQUIRED_RELEASE_IMAGES[*]}"'
        proc = self._run_common_func(cmd)
        self.assertEqual(proc.returncode, 0)
        for img in MOCK_REQUIRED_RELEASE_IMAGES:
            self.assertIn(img, proc.stdout)

    def test_is_ci_pipeline_behavior(self):
        # By default isolated env has CI stripped
        proc = self._run_common_func('is_ci_pipeline')
        self.assertNotEqual(proc.returncode, 0)

        # With explicit CI=true
        proc = self._run_common_func('is_ci_pipeline', env={"CI": "true"})
        self.assertEqual(proc.returncode, 0)

    def test_promote_release_images_validation(self):
        # Missing args
        proc = self._run_common_func('promote_release_images "" ""')
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("commit_sha and release_version are required", proc.stderr)

        # Invalid target_tag format
        for bad_tag in INVALID_GA_RELEASE_TAGS:
            with self.subTest(bad_tag=bad_tag):
                proc = self._run_common_func(f'promote_release_images "{MOCK_SAMPLE_SHORT_SHA}" "{bad_tag}"')
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("not a valid pure numeric SemVer", proc.stderr)

    def test_promote_release_images_local_dry_run(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_docker_binary(bin_dir)

            proc = self._run_common_func(
                f'promote_release_images "{MOCK_SAMPLE_COMMIT_SHA}" "{MOCK_TARGET_RELEASE_TAG}"',
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Dry-run: Remote image promotion", proc.stdout)
            self.assertIn("skipped (runs only in CI)", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_promote_release_images_execution(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_docker_binary(bin_dir)

            proc = self._run_common_func(
                f'promote_release_images "{MOCK_SAMPLE_COMMIT_SHA}" "{MOCK_TARGET_RELEASE_TAG}"',
                env={"CI": "true"},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Promoting verified container images", proc.stdout)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn(f"Promoting {img}", proc.stdout)
                self.assertIn(f"Promoted {img} to {MOCK_TARGET_RELEASE_TAG}", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_promote_release_images_swapped_arguments(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            create_mock_docker_binary(bin_dir)

            proc = self._run_common_func(
                f'promote_release_images "{MOCK_TARGET_RELEASE_TAG}" "{MOCK_SAMPLE_COMMIT_SHA}"',
                env={"CI": "true"},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn(f"Promoted {img} to {MOCK_TARGET_RELEASE_TAG}", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_promote_release_images_idempotent_skip(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            existing = [
                f"ghcr.io/gke-labs/kube-agents/{img}:{MOCK_TARGET_RELEASE_TAG}"
                for img in MOCK_REQUIRED_RELEASE_IMAGES
            ] + [
                f"ghcr.io/gke-labs/kube-agents/{img}:{MOCK_SAMPLE_COMMIT_SHA}"
                for img in MOCK_REQUIRED_RELEASE_IMAGES
            ]
            create_mock_docker_binary(bin_dir, existing_images=existing)

            proc = self._run_common_func(
                f'promote_release_images "{MOCK_SAMPLE_COMMIT_SHA}" "{MOCK_TARGET_RELEASE_TAG}"',
                env={"CI": "true"},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn("already exists in registry and matches source image", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_promote_release_images_idempotent_skip_when_target_is_index_wrapping_source_manifest(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            source_sha = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
            target_index_sha = "sha256:2222222222222222222222222222222222222222222222222222222222222222"
            raw_index = f'{{"mediaType":"application/vnd.oci.image.index.v1+json","digest":"{target_index_sha}","manifests":[{{"mediaType":"application/vnd.oci.image.manifest.v1+json","digest":"{source_sha}"}}]}}'
            digests = {}
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                digests[f"ghcr.io/gke-labs/kube-agents/{img}:{MOCK_TARGET_RELEASE_TAG}"] = {
                    "format": target_index_sha,
                    "raw": raw_index,
                }
                digests[f"ghcr.io/gke-labs/kube-agents/{img}:{MOCK_SAMPLE_COMMIT_SHA}"] = {
                    "format": source_sha,
                    "raw": f'{{"mediaType":"application/vnd.oci.image.manifest.v1+json","digest":"{source_sha}"}}',
                }
            create_mock_docker_binary(bin_dir, image_digests=digests)

            proc = self._run_common_func(
                f'promote_release_images "{MOCK_SAMPLE_COMMIT_SHA}" "{MOCK_TARGET_RELEASE_TAG}"',
                env={"CI": "true"},
                bin_dir=str(bin_dir),
            )
            self.assertEqual(proc.returncode, 0)
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                self.assertIn("already exists in registry and matches source image", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_promote_release_images_fails_when_mismatched_digest(self):
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            bin_dir = pathlib.Path(temp_dir.name) / "bin"
            digests = {}
            for img in MOCK_REQUIRED_RELEASE_IMAGES:
                digests[f"ghcr.io/gke-labs/kube-agents/{img}:{MOCK_TARGET_RELEASE_TAG}"] = (
                    "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                )
                digests[f"ghcr.io/gke-labs/kube-agents/{img}:{MOCK_SAMPLE_COMMIT_SHA}"] = (
                    "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                )
            create_mock_docker_binary(bin_dir, image_digests=digests)

            proc = self._run_common_func(
                f'promote_release_images "{MOCK_SAMPLE_COMMIT_SHA}" "{MOCK_TARGET_RELEASE_TAG}"',
                env={"CI": "true"},
                bin_dir=str(bin_dir),
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("does NOT match source image", proc.stderr)
            self.assertIn("Release promotion blocked", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_ensure_git_tag_hermetic_local_execution(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            head_commit = git("rev-parse", "HEAD").stdout.strip()

            # Local execution should create tag without remote operations
            proc = self._run_common_func(
                f'ensure_git_tag "{MOCK_TARGET_RELEASE_TAG}" "{head_commit}" "Test release {MOCK_TARGET_RELEASE_TAG}"',
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn(f"Dry-run: Git tag '{MOCK_TARGET_RELEASE_TAG}' created locally", proc.stdout)

            # Tag is locally created
            tag_commit = git("rev-parse", f"{MOCK_TARGET_RELEASE_TAG}^{{commit}}").stdout.strip()
            self.assertEqual(tag_commit, head_commit)

            # Idempotent skip on second run
            proc2 = self._run_common_func(
                f'ensure_git_tag "{MOCK_TARGET_RELEASE_TAG}" "{head_commit}" "Test release {MOCK_TARGET_RELEASE_TAG}"',
                cwd=repo_dir,
            )
            self.assertEqual(proc2.returncode, 0)
            self.assertIn("Idempotent skip", proc2.stdout)
        finally:
            temp_dir.cleanup()

    # ─── release_resolve_target ───────────────────────────────────────────────
    # The targeting trio must never be guessed in CI: a defaulted PROJECT_ID
    # points provision/teardown at a real project nobody named.

    _RESOLVE = (
        "unset GKE_CLUSTER_NAME GCP_REGION GCP_PROJECT_ID CLUSTER_NAME REGION "
        "PROJECT_ID AGENT_NAMESPACE || true\n"
    )
    _ECHO = 'echo "${CLUSTER_NAME}|${REGION}|${PROJECT_ID}|${AGENT_NAMESPACE}"'

    def test_release_resolve_target_fails_in_ci_when_targeting_vars_unset(self):
        proc = self._run_common_func(
            f"{self._RESOLVE}release_resolve_target",
            env={"CI": "true"},
        )
        self.assertNotEqual(proc.returncode, 0, "CI must not fall back to a default project")
        for var in ("GKE_CLUSTER_NAME", "GCP_REGION", "GCP_PROJECT_ID"):
            self.assertIn(var, proc.stderr)

    def test_release_resolve_target_names_only_the_missing_variable(self):
        """The error is a pointer to the misconfigured `env:` entry, so it must be precise."""
        proc = self._run_common_func(
            f"{self._RESOLVE}"
            "export GKE_CLUSTER_NAME=c GCP_REGION=r AGENT_NAMESPACE=n\n"
            "release_resolve_target",
            env={"CI": "true"},
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("GCP_PROJECT_ID", proc.stderr)
        self.assertNotIn("GKE_CLUSTER_NAME", proc.stderr)
        self.assertNotIn("GCP_REGION", proc.stderr)
        self.assertNotIn("AGENT_NAMESPACE", proc.stderr)

    def test_release_resolve_target_passes_in_ci_when_set(self):
        proc = self._run_common_func(
            f"{self._RESOLVE}"
            "export GKE_CLUSTER_NAME=rc-cluster GCP_REGION=us-central1 GCP_PROJECT_ID=proj "
            "AGENT_NAMESPACE=kubeagents-system\n"
            f"release_resolve_target\n{self._ECHO}",
            env={"CI": "true"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("rc-cluster|us-central1|proj|kubeagents-system", proc.stdout)

    def test_release_resolve_target_defaults_off_ci(self):
        """The developer path keeps its defaults; that is what the trio is for."""
        proc = self._run_common_func(f"{self._RESOLVE}release_resolve_target\n{self._ECHO}")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("platform-agent-host|us-central1|kube-agents-rc|kubeagents-system", proc.stdout)

    def test_release_resolve_target_requires_agent_namespace_in_ci(self):
        """`vars.AGENT_NAMESPACE` expanding to empty must not read as the default.

        The rc and nightly environments both define it, so a job that sets the
        targeting trio but not this one is misconfigured — and silently getting
        `kubeagents-system` is what made that invisible.
        """
        proc = self._run_common_func(
            f"{self._RESOLVE}"
            "export GKE_CLUSTER_NAME=c GCP_REGION=r GCP_PROJECT_ID=p\n"
            "release_resolve_target",
            env={"CI": "true"},
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("AGENT_NAMESPACE", proc.stderr)

    # ── commit_messages_have_breaking_change ─────────────────────────────────
    #
    # Shared by calculate_next_version.sh, which reads it to pick the bump, and
    # resolve_scheduled_release.sh, which reads it to decide whether an
    # unattended release stops for a human. The two disagreeing is silent in the
    # unsafe direction, so the last test here pins that neither keeps a copy.

    def test_commit_messages_have_breaking_change_detects_a_bang_subject(self):
        for subject in ("feat!: drop it", "fix(operator)!: drop the v1alpha1 field"):
            with self.subTest(subject=subject):
                proc = self._run_common_func(f'commit_messages_have_breaking_change "{subject}" ""')
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_commit_messages_have_breaking_change_detects_a_footer(self):
        for body in ("BREAKING CHANGE: the yaml spec moved", "BREAKING-CHANGE: the yaml spec moved"):
            with self.subTest(body=body):
                proc = self._run_common_func(f'commit_messages_have_breaking_change "" "{body}"')
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_commit_messages_have_breaking_change_ignores_ordinary_commits(self):
        proc = self._run_common_func(
            'commit_messages_have_breaking_change "feat: add a thing\nfix: mend a thing" "just prose"'
        )
        self.assertNotEqual(proc.returncode, 0)

    def test_commit_messages_have_breaking_change_survives_a_large_corpus(self):
        """`echo … | grep -q` would report 141 here, which reads as "not breaking".

        grep exits on its first match, the producer dies on SIGPIPE, and under
        `set -o pipefail` the pipeline reports 141 — so matching input reads as no
        breaking change, in the direction that ships one unattended. The herestring
        form is immune, and this is what holds it that way.
        """
        proc = self._run_common_func(
            'set -o pipefail\n'
            'big="BREAKING CHANGE: something"$\'\\n\'"$(head -c 400000 /dev/zero | tr "\\0" "y")"\n'
            'commit_messages_have_breaking_change "" "${big}"'
        )
        self.assertEqual(proc.returncode, 0, f"stderr={proc.stderr} rc={proc.returncode}")

    # ── release_read_commit_range ────────────────────────────────────────────

    def test_release_read_commit_range_reports_subjects_and_bodies(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            git("tag", "-a", "0.1.0", "-m", "r")
            (pathlib.Path(repo_dir) / "b.txt").write_text("b\n")
            git("add", "b.txt")
            git("commit", "-m", "feat: a thing\n\nBREAKING CHANGE: it moved")
            proc = self._run_common_func(
                'release_read_commit_range "0.1.0" "HEAD"\n'
                'echo "S=${RELEASE_RANGE_SUBJECTS}"\necho "B=${RELEASE_RANGE_BODIES}"',
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("S=feat: a thing", proc.stdout)
            self.assertIn("BREAKING CHANGE: it moved", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_release_read_commit_range_keeps_git_warnings_out_of_the_subjects(self):
        """An empty range must read as empty even when git warns on success.

        A branch sharing a GA tag's name makes `git log 0.1.0..HEAD` succeed and
        warn about the ambiguous refname. Captured with `2>&1` that warning
        becomes the subject list, so an empty range reads as "there are commits
        to ship" — and the scheduled gate publishes a release for a week with
        nothing in it.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            git("tag", "-a", "0.1.0", "-m", "r")
            git("branch", "0.1.0")
            proc = self._run_common_func(
                'release_read_commit_range "0.1.0" "HEAD"\n'
                'echo "SUBJECTS=[${RELEASE_RANGE_SUBJECTS}]"',
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("SUBJECTS=[]", proc.stdout)
            self.assertNotIn("ambiguous", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_release_read_commit_range_fails_and_reports_on_a_bad_range(self):
        temp_dir, repo_dir, _ = create_mock_git_repo()
        try:
            proc = self._run_common_func(
                'release_read_commit_range "no-such-tag" "HEAD"', cwd=repo_dir
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Failed to read commit log", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_neither_caller_keeps_its_own_copy_of_the_range_read(self):
        """Scoping the bump and the halt to different commit sets is silent."""
        for script in ("calculate_next_version.sh", "resolve_scheduled_release.sh"):
            with self.subTest(script=script):
                text = (_REPO_ROOT / "scripts" / "release" / script).read_text()
                body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
                self.assertNotIn(
                    '--format="%s"',
                    body,
                    f"{script} reads the commit range itself instead of calling common.sh",
                )
                self.assertIn("release_read_commit_range", body, f"{script} does not call the helper")

    def test_neither_caller_keeps_its_own_copy_of_the_breaking_regexes(self):
        """A second copy is how the bump and the halt come to disagree."""
        bang_regex = r"^[a-z]+(\([^)]+\))?!:"
        for script in ("calculate_next_version.sh", "resolve_scheduled_release.sh"):
            with self.subTest(script=script):
                text = (_REPO_ROOT / "scripts" / "release" / script).read_text()
                body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
                self.assertNotIn(
                    bang_regex,
                    body,
                    f"{script} re-implements the breaking-change test instead of calling common.sh",
                )
                self.assertIn("commit_messages_have_breaking_change", body, f"{script} does not call the helper")

    def test_release_bundle_registries(self):
        """Verifies common.sh exports release bundle directories, root files, and charts."""
        script = """
echo "DIRS:${RELEASE_BUNDLE_DIRECTORIES[*]}"
echo "CHARTS:${RELEASE_HELM_CHARTS[*]}"
echo "FILES:${RELEASE_BUNDLE_ROOT_FILES[*]}"
"""
        proc = self._run_common_func(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("DIRS:terraform k8s-operator deploy charts scripts examples", proc.stdout)
        self.assertIn("CHARTS:charts/kube-agents", proc.stdout)
        self.assertIn("FILES:install.sh uninstall.sh upgrade.sh install.defaults.env install.env.example images.json Makefile INSTALL.md README.md LICENSE", proc.stdout)

    def test_extract_commit_tree(self):
        """Verifies extract_commit_tree extracts exact committed files to target directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            head_commit = subprocess.check_output(
                ["git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"], text=True
            ).strip()
            target_dir = pathlib.Path(temp_dir) / "extracted"
            proc = self._run_common_func(
                f'extract_commit_tree "{head_commit}" "{target_dir}" "README.md"',
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            extracted_file = target_dir / "README.md"
            self.assertTrue(extracted_file.exists())
            self.assertEqual(extracted_file.read_text(), (_REPO_ROOT / "README.md").read_text())


class RegistryImageProbeTest(unittest.TestCase):
    """`registry_image_exists` must not read "no docker" as "no image".

    `docker manifest inspect` is the probe that works against every registry,
    and three other call sites in common.sh already check for the binary before
    using it. `check_commit_images_exist` did not, so on a machine with no
    docker it reported every image missing — which `find_latest_built_commit`
    turns into "no commit in the last 30 has published images", a publish
    outage that is really a missing binary. The Prow job image is exactly such
    a machine: hack/ci-deploy.sh builds through `gcloud builds submit` because
    there is no docker daemon there, and hack/resolve-rc-target.sh runs in the
    same place.
    """

    _IMAGE = f"{MOCK_DEFAULT_REGISTRY_PREFIX}/platform-agent:{MOCK_SAMPLE_COMMIT_SHA}"
    # Deliberately not ghcr.io: the curl fallback speaks one registry's API.
    _FOREIGN_IMAGE = f"us-central1-docker.pkg.dev/p/r/platform-agent:{MOCK_SAMPLE_COMMIT_SHA}"
    _REPOSITORY_PATH = "gke-labs/kube-agents/platform-agent"
    _DIGEST = "sha256:1111111111111111111111111111111111111111111111111111111111111111"

    def _bin(self, tmp):
        """A PATH holding the base utilities, symlinked, and no docker.

        Most of this suite is about what happens when docker is ABSENT, so the
        binary has to be absent from PATH rather than merely shadowed. Naming
        system directories does not achieve that: `ubuntu-latest` ships docker
        at `/usr/bin/docker`, which put the real binary in front of six of
        these cases and let them pass on a laptop with no docker installed
        while failing in CI.
        """
        return create_minimal_tools_bin(tmp)

    def _run(self, func_call, bin_dir, env=None):
        """common.sh sourced with PATH pinned to bin_dir, and nothing else."""
        overrides = {"PATH": str(bin_dir)}
        overrides.update(env or {})
        return subprocess.run(
            ["bash", "-c", f'source "{_COMMON_SH}"\n{func_call}'],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(overrides=overrides),
            cwd=str(_REPO_ROOT),
        )

    def test_docker_is_used_when_it_is_there(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._bin(tmp)
            create_mock_docker_binary(bin_dir, existing_images=[self._IMAGE])
            found = self._run(f'registry_image_exists "{self._IMAGE}"', bin_dir)
            self.assertEqual(found.returncode, 0, found.stderr)
            missing = self._run(f'registry_image_exists "{self._IMAGE}x"', bin_dir)
            self.assertNotEqual(missing.returncode, 0)

    def test_a_published_image_is_found_without_docker(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._bin(tmp)
            create_mock_ghcr_curl_binary(bin_dir)
            proc = self._run(f'registry_image_exists "{self._IMAGE}"', bin_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_an_unpublished_image_is_still_missing_without_docker(self):
        """The fallback must not turn into an unconditional yes."""
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._bin(tmp)
            create_mock_ghcr_curl_binary(bin_dir, manifest_status=1)
            proc = self._run(f'registry_image_exists "{self._IMAGE}"', bin_dir)
            self.assertNotEqual(proc.returncode, 0)

    def test_an_unauthenticated_registry_is_missing_rather_than_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._bin(tmp)
            create_mock_ghcr_curl_binary(bin_dir, token_response="")
            proc = self._run(f'registry_image_exists "{self._IMAGE}"', bin_dir)
            self.assertNotEqual(proc.returncode, 0)

    def test_another_registry_without_docker_says_why(self):
        """Reporting "missing" for a registry the fallback cannot query is a
        lie about the image. It still fails, but it must say what it is."""
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._bin(tmp)
            create_mock_ghcr_curl_binary(bin_dir)
            proc = self._run(f'registry_image_exists "{self._FOREIGN_IMAGE}"', bin_dir)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("no docker on PATH", proc.stderr)

    def _manifest_probe(self, log_path):
        """The one logged call that is not the token request."""
        calls = [line for line in log_path.read_text().splitlines() if "/manifests/" in line]
        self.assertEqual(len(calls), 1, log_path.read_text())
        return calls[0]

    def test_the_probe_carries_the_url_and_headers_ghcr_answers_on(self):
        """GHCR returns 404, not 401, for a manifest request that omits the OCI
        media types — a false "image missing" indistinguishable from the real
        thing. Asserting only the exit code leaves both the URL and the header
        free to be anything, so this reads the request the mock recorded."""
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._bin(tmp)
            _, log_path = create_mock_ghcr_curl_binary(bin_dir)
            proc = self._run(f'registry_image_exists "{self._IMAGE}"', bin_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)

            probe = self._manifest_probe(pathlib.Path(log_path))
            self.assertIn(
                f"https://ghcr.io/v2/{self._REPOSITORY_PATH}/manifests/{MOCK_SAMPLE_COMMIT_SHA}",
                probe,
            )
            self.assertIn("Authorization: Bearer t", probe)
            for media_type in (
                "application/vnd.oci.image.index.v1+json",
                "application/vnd.oci.image.manifest.v1+json",
                "application/vnd.docker.distribution.manifest.list.v2+json",
                "application/vnd.docker.distribution.manifest.v2+json",
            ):
                self.assertIn(media_type, probe)

    def test_a_digest_reference_is_probed_as_a_digest(self):
        """`<repo>@sha256:…` is what the docker branch accepts, so the fallback
        has to accept it too — splitting it on the last colon would ask for a
        repository named `…@sha256` and a tag of hex."""
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._bin(tmp)
            _, log_path = create_mock_ghcr_curl_binary(bin_dir)
            image = f"{MOCK_DEFAULT_REGISTRY_PREFIX}/platform-agent@{self._DIGEST}"
            proc = self._run(f'registry_image_exists "{image}"', bin_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(
                f"https://ghcr.io/v2/{self._REPOSITORY_PATH}/manifests/{self._DIGEST}",
                self._manifest_probe(pathlib.Path(log_path)),
            )

    def test_a_reference_less_image_is_probed_at_latest(self):
        """No tag and no digest means `latest` to a registry. Left unhandled it
        becomes an empty reference and a URL ending in `/manifests/`."""
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._bin(tmp)
            _, log_path = create_mock_ghcr_curl_binary(bin_dir)
            image = f"{MOCK_DEFAULT_REGISTRY_PREFIX}/platform-agent"
            proc = self._run(f'registry_image_exists "{image}"', bin_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(
                f"https://ghcr.io/v2/{self._REPOSITORY_PATH}/manifests/latest",
                self._manifest_probe(pathlib.Path(log_path)),
            )

    def test_the_commit_check_stops_reporting_a_publish_outage(self):
        """The regression the guard exists for, at the level callers use."""
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._bin(tmp)
            create_mock_ghcr_curl_binary(bin_dir)
            proc = self._run(
                f'check_commit_images_exist "{MOCK_SAMPLE_COMMIT_SHA}"',
                bin_dir,
                env={"REGISTRY_PREFIX": MOCK_DEFAULT_REGISTRY_PREFIX},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
