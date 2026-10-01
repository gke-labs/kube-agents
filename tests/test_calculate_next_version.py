"""Unit tests for scripts/release/calculate_next_version.sh SemVer 2.0 engine.

Tests Conventional Commits parsing, SemVer 2.0 Clause 4 for 0.y.z,
precedence rules (breaking > feat > fix), and GitHub Actions outputs.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

from tests.testing.common import create_mock_git_repo, get_isolated_test_env
from tests.testing.release import (
    INVALID_GA_RELEASE_TAGS,
    MOCK_BASE_TAG_1_X,
    MOCK_BASE_TAG_PRE_1_0,
    MOCK_COMMIT_MSG_BREAKING_1_X,
    MOCK_COMMIT_MSG_BREAKING_BODY,
    MOCK_COMMIT_MSG_BREAKING_PRE_1_0,
    MOCK_COMMIT_MSG_DOCS,
    MOCK_COMMIT_MSG_FEAT,
    MOCK_COMMIT_MSG_FIX,
    MOCK_INITIAL_VERSION,
    MOCK_LINE_PATCH_RELEASE_TAG,
    MOCK_NONEXISTENT_REF,
    MOCK_NONEXISTENT_TAG,
    MOCK_RC_VALIDATED_TAG,
    MOCK_TARGET_RELEASE_LINE,
    MOCK_TARGET_RELEASE_TAG,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CALC_SCRIPT = _REPO_ROOT / "scripts" / "release" / "calculate_next_version.sh"


class CalculateNextVersionTest(unittest.TestCase):
    def _create_mock_repo(self):
        """Creates a temporary git repository for testing version calculation."""
        return create_mock_git_repo()

    def _run_calc_script(self, repo_dir, args=None, env=None):
        full_env = get_isolated_test_env(overrides=env)
        return subprocess.run(
            ["bash", str(_CALC_SCRIPT)] + (args or []),
            cwd=repo_dir,
            capture_output=True,
            text=True,
            env=full_env,
        )

    def test_baseline_initialization_when_no_tags(self):
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            gh_out = pathlib.Path(repo_dir) / "gh_output.txt"
            proc = self._run_calc_script(repo_dir, env={"GITHUB_OUTPUT": str(gh_out)})
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), MOCK_INITIAL_VERSION)

            outputs = gh_out.read_text()
            self.assertIn(f"version={MOCK_INITIAL_VERSION}", outputs)
            self.assertIn("has_changes=true", outputs)
            self.assertIn("bump_type=initial", outputs)
        finally:
            temp_dir.cleanup()

    def test_no_new_commits_keeps_version(self):
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            git("tag", "-a", MOCK_INITIAL_VERSION, "-m", f"Release {MOCK_INITIAL_VERSION}")
            gh_out = pathlib.Path(repo_dir) / "gh_output.txt"
            proc = self._run_calc_script(repo_dir, env={"GITHUB_OUTPUT": str(gh_out)})
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), MOCK_INITIAL_VERSION)

            outputs = gh_out.read_text()
            self.assertIn(f"version={MOCK_INITIAL_VERSION}", outputs)
            self.assertIn("has_changes=false", outputs)
            self.assertIn("bump_type=none", outputs)
        finally:
            temp_dir.cleanup()

    def test_patch_bump_for_fix_and_chore(self):
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            git("tag", "-a", MOCK_INITIAL_VERSION, "-m", f"Release {MOCK_INITIAL_VERSION}")

            (pathlib.Path(repo_dir) / "file1.txt").write_text("fix")
            git("add", "file1.txt")
            git("commit", "-m", MOCK_COMMIT_MSG_FIX)

            (pathlib.Path(repo_dir) / "file2.txt").write_text("docs")
            git("add", "file2.txt")
            git("commit", "-m", MOCK_COMMIT_MSG_DOCS)

            proc = self._run_calc_script(repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "0.1.1")
        finally:
            temp_dir.cleanup()

    def test_minor_bump_for_feat_in_pre_1_0(self):
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            git("tag", "-a", MOCK_BASE_TAG_PRE_1_0, "-m", f"Release {MOCK_BASE_TAG_PRE_1_0}")

            (pathlib.Path(repo_dir) / "file1.txt").write_text("feat")
            git("add", "file1.txt")
            git("commit", "-m", MOCK_COMMIT_MSG_FEAT)

            proc = self._run_calc_script(repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "0.2.0")
        finally:
            temp_dir.cleanup()

    def test_breaking_change_in_pre_1_0_semver_clause_4(self):
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            git("tag", "-a", MOCK_BASE_TAG_PRE_1_0, "-m", f"Release {MOCK_BASE_TAG_PRE_1_0}")

            (pathlib.Path(repo_dir) / "file1.txt").write_text("breaking")
            git("add", "file1.txt")
            git("commit", "-m", MOCK_COMMIT_MSG_BREAKING_PRE_1_0)

            proc = self._run_calc_script(repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "0.2.0")
        finally:
            temp_dir.cleanup()

    def test_automated_calculator_never_produces_1_0_from_pre_1_0_base(self):
        """Invariant: in 0.y.z initial development, automated calculation never produces 1.0.0.

        Even with multiple breaking change commits, SemVer Clause 4 bumps MINOR (e.g. 0.9.0 -> 0.10.0).
        Graduating to 1.0.0 is strictly a manual governance decision.
        """
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            git("tag", "-a", "0.9.0", "-m", "Release 0.9.0")

            (pathlib.Path(repo_dir) / "file1.txt").write_text("breaking 1")
            git("add", "file1.txt")
            git("commit", "-m", "feat!: breaking change 1")

            (pathlib.Path(repo_dir) / "file2.txt").write_text("breaking 2")
            git("add", "file2.txt")
            git("commit", "-m", "fix!: breaking change 2")

            proc = self._run_calc_script(repo_dir)
            self.assertEqual(proc.returncode, 0)
            next_ver = proc.stdout.strip()
            self.assertEqual(next_ver, "0.10.0")
            self.assertTrue(next_ver.startswith("0."))
            self.assertFalse(next_ver.startswith("1."))
        finally:
            temp_dir.cleanup()

    def test_breaking_change_in_body_footer(self):
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            git("tag", "-a", "0.2.0", "-m", "Release 0.2.0")

            (pathlib.Path(repo_dir) / "file1.txt").write_text("breaking body")
            git("add", "file1.txt")
            git("commit", "-m", MOCK_COMMIT_MSG_BREAKING_BODY)

            proc = self._run_calc_script(repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "0.3.0")
        finally:
            temp_dir.cleanup()

    def test_breaking_change_in_1_x_x_bumps_major(self):
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            git("tag", "-a", MOCK_BASE_TAG_1_X, "-m", f"Release {MOCK_BASE_TAG_1_X}")

            (pathlib.Path(repo_dir) / "file1.txt").write_text("major")
            git("add", "file1.txt")
            git("commit", "-m", MOCK_COMMIT_MSG_BREAKING_1_X)

            proc = self._run_calc_script(repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "2.0.0")
        finally:
            temp_dir.cleanup()

    def test_feat_in_1_x_x_bumps_minor(self):
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            git("tag", "-a", MOCK_BASE_TAG_1_X, "-m", f"Release {MOCK_BASE_TAG_1_X}")

            (pathlib.Path(repo_dir) / "file1.txt").write_text("feat")
            git("add", "file1.txt")
            git("commit", "-m", MOCK_COMMIT_MSG_FEAT)

            proc = self._run_calc_script(repo_dir)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "1.3.0")
        finally:
            temp_dir.cleanup()

    def test_ignores_rc_and_non_semver_tags(self):
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            git("tag", "-a", MOCK_INITIAL_VERSION, "-m", f"Release {MOCK_INITIAL_VERSION}")
            git("tag", "-a", "random-tag", "-m", "Non semver")

            (pathlib.Path(repo_dir) / "file1.txt").write_text("fix")
            git("add", "file1.txt")
            git("commit", "-m", MOCK_COMMIT_MSG_FIX)
            git("tag", "-a", MOCK_RC_VALIDATED_TAG, "-m", "RC tag")

            proc = self._run_calc_script(repo_dir)
            self.assertEqual(proc.returncode, 0)
            # Should detect 0.1.0 as baseline (ignoring rc_0.2.0_validated and random-tag) and calculate 0.1.1
            self.assertEqual(proc.stdout.strip(), "0.1.1")
        finally:
            temp_dir.cleanup()

    def test_invalid_base_tag_format_fails(self):
        temp_dir, repo_dir, _ = self._create_mock_repo()
        try:
            for bad_tag in INVALID_GA_RELEASE_TAGS:
                with self.subTest(bad_tag=bad_tag):
                    proc = self._run_calc_script(repo_dir, args=[bad_tag])
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertIn("not a valid pure numeric SemVer", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_nonexistent_base_tag_fails(self):
        temp_dir, repo_dir, _ = self._create_mock_repo()
        try:
            # Base tag does not exist in repo -> must fail fast with error
            proc = self._run_calc_script(repo_dir, args=[MOCK_NONEXISTENT_TAG])
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("does not exist in git repository", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_nonexistent_target_ref_fails(self):
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            git("tag", "-a", MOCK_INITIAL_VERSION, "-m", f"Release {MOCK_INITIAL_VERSION}")
            # Target ref does not exist -> must fail fast
            proc = self._run_calc_script(repo_dir, args=[MOCK_INITIAL_VERSION, MOCK_NONEXISTENT_REF])
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn(f"Target ref '{MOCK_NONEXISTENT_REF}' does not exist", proc.stderr)
        finally:
            temp_dir.cleanup()


    def test_explicit_version_success(self):
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            git("tag", "-a", MOCK_INITIAL_VERSION, "-m", f"Release {MOCK_INITIAL_VERSION}")
            gh_out = pathlib.Path(repo_dir) / "gh_output.txt"
            proc = self._run_calc_script(
                repo_dir,
                env={"EXPLICIT_RELEASE_VERSION": "0.3.0", "GITHUB_OUTPUT": str(gh_out)},
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "0.3.0")

            outputs = gh_out.read_text()
            self.assertIn("version=0.3.0", outputs)
            self.assertIn("has_changes=true", outputs)
            self.assertIn("bump_type=manual", outputs)
            self.assertIn(f"previous_version={MOCK_INITIAL_VERSION}", outputs)
        finally:
            temp_dir.cleanup()

    def test_explicit_version_invalid_format_fails(self):
        temp_dir, repo_dir, _ = self._create_mock_repo()
        try:
            for bad_tag in INVALID_GA_RELEASE_TAGS:
                with self.subTest(bad_tag=bad_tag):
                    proc = self._run_calc_script(
                        repo_dir,
                        env={"EXPLICIT_RELEASE_VERSION": bad_tag},
                    )
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertIn("not a valid pure numeric SemVer", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_explicit_version_downgrade_fails(self):
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            git("tag", "-a", "0.2.0", "-m", "Release 0.2.0")
            proc = self._run_calc_script(
                repo_dir,
                env={"EXPLICIT_RELEASE_VERSION": "0.1.0"},
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("is lower than the candidate's base release '0.2.0'", proc.stderr)
            self.assertIn("Version downgrade is prohibited", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_explicit_version_collision_on_different_commit_fails(self):
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            git("tag", "-a", "0.2.0", "-m", "Release 0.2.0")

            # Create a new commit
            (pathlib.Path(repo_dir) / "new_file.txt").write_text("content")
            git("add", "new_file.txt")
            git("commit", "-m", "feat: new commit")

            proc = self._run_calc_script(
                repo_dir,
                env={"EXPLICIT_RELEASE_VERSION": "0.2.0"},
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("already exists in git repository on a different commit", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_explicit_version_same_commit_allowed(self):
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            git("tag", "-a", "0.2.0", "-m", "Release 0.2.0")
            proc = self._run_calc_script(
                repo_dir,
                env={"EXPLICIT_RELEASE_VERSION": "0.2.0"},
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "0.2.0")
        finally:
            temp_dir.cleanup()

    def test_explicit_version_resumption_allowed_when_tag_on_detached_head_stamped_commit(self):
        """Verifies explicit version calculation allows resumption when tag is on a detached HEAD child commit."""
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            candidate_commit = git("rev-parse", "HEAD").stdout.strip()
            # Attach validated tag on candidate commit
            git("tag", "-a", MOCK_RC_VALIDATED_TAG, candidate_commit, "-m", "validated")

            # Create detached HEAD child commit stamped with release version
            git("checkout", "--detach", candidate_commit)
            (pathlib.Path(repo_dir) / "version.txt").write_text(f"{MOCK_TARGET_RELEASE_TAG}\n")
            git("add", "version.txt")
            git("commit", "-m", f"chore(release): stamp release version {MOCK_TARGET_RELEASE_TAG}")
            stamped_commit = git("rev-parse", "HEAD").stdout.strip()
            git("tag", "-a", MOCK_TARGET_RELEASE_TAG, stamped_commit, "-m", f"Release {MOCK_TARGET_RELEASE_TAG}")
            git("checkout", "main")

            # Run with explicit release version on candidate commit
            proc = self._run_calc_script(
                repo_dir,
                env={"EXPLICIT_RELEASE_VERSION": MOCK_TARGET_RELEASE_TAG, "TARGET_COMMIT": candidate_commit},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), MOCK_TARGET_RELEASE_TAG)
        finally:
            temp_dir.cleanup()

    def test_explicit_version_refs_tags_namespace_disambiguation(self):
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            # Create a tag 0.2.0 on initial commit
            git("tag", "-a", "0.2.0", "-m", "Release 0.2.0")

            # Create a branch named 0.2.0 pointing to the same commit
            git("branch", "0.2.0")

            proc = self._run_calc_script(
                repo_dir,
                env={"EXPLICIT_RELEASE_VERSION": "0.2.0"},
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "0.2.0")
        finally:
            temp_dir.cleanup()

    def test_auto_resolve_target_commit_from_staging_promotion_tag(self):
        """The same tag the release gate reads.

        Resolving a different candidate here would compute the version for one
        commit and let the gate publish another.
        """
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            # Initial commit tagged 0.1.0
            git("tag", "-a", "0.1.0", "-m", "Release 0.1.0")

            # Second commit: fix commit promoted to staging
            (pathlib.Path(repo_dir) / "fix.txt").write_text("fix")
            git("add", "fix.txt")
            git("commit", "-m", "fix: resolve bug")
            fix_sha = git("rev-parse", "HEAD").stdout.strip()
            staging_tag = "staging_2608191200_2222222"
            git("tag", "-a", staging_tag, fix_sha, "-m", f"Promoted {staging_tag}")

            # Third commit on main: unvalidated feat commit
            (pathlib.Path(repo_dir) / "feat.txt").write_text("feat")
            git("add", "feat.txt")
            git("commit", "-m", "feat: new feature on main")

            gh_out = pathlib.Path(repo_dir) / "gh_out.txt"
            # Running with empty target commit should auto-resolve to fix_sha and compute 0.1.1 (NOT 0.2.0)
            proc = self._run_calc_script(
                repo_dir,
                args=["", ""],
                env={"GITHUB_OUTPUT": str(gh_out)},
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "0.1.1")

            outputs = gh_out.read_text()
            self.assertIn("release_version=0.1.1", outputs)
            self.assertIn("version=0.1.1", outputs)
            self.assertIn(f"release_commit={fix_sha}", outputs)
            self.assertIn("bump_type=patch", outputs)
        finally:
            temp_dir.cleanup()

    def test_auto_resolve_target_commit_prefers_newest_rc_tag(self):
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            git("tag", "-a", "0.1.0", "-m", "Release 0.1.0")

            # Commit 2: older RC
            (pathlib.Path(repo_dir) / "file2.txt").write_text("file2")
            git("add", "file2.txt")
            git("commit", "-m", "fix: older commit")
            sha2 = git("rev-parse", "HEAD").stdout.strip()
            git("tag", "-a", "rc_2608181000_1111111_validated", sha2, "-m", "Older RC")

            # Commit 3: newer RC
            (pathlib.Path(repo_dir) / "file3.txt").write_text("file3")
            git("add", "file3.txt")
            git("commit", "-m", "feat: newer commit")
            sha3 = git("rev-parse", "HEAD").stdout.strip()
            git("tag", "-a", "rc_2608191200_2222222_validated", sha3, "-m", "Newer RC")

            gh_out = pathlib.Path(repo_dir) / "gh_out.txt"
            proc = self._run_calc_script(
                repo_dir,
                args=["", ""],
                env={"GITHUB_OUTPUT": str(gh_out)},
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "0.2.0")

            outputs = gh_out.read_text()
            self.assertIn("release_version=0.2.0", outputs)
            self.assertIn("version=0.2.0", outputs)
            self.assertIn(f"release_commit={sha3}", outputs)
        finally:
            temp_dir.cleanup()

    def test_emergency_override_calculates_from_head(self):
        """A promoted commit must exist, or the override is indistinguishable from the default.

        With no staging tag in the repository the auto-resolve branch also falls
        through to HEAD, so every assertion below passes whether or not the
        override is honoured — and did, while the test was still setting the
        pre-rename `SKIP_RC_VALIDATION`. The staging tag on an older commit is
        what makes the two branches produce different answers.
        """
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            git("tag", "-a", "0.1.0", "-m", "Release 0.1.0")

            # A promoted commit the auto-resolve branch would pick instead of HEAD,
            # and a patch bump rather than HEAD's minor, so the two cannot be confused.
            (pathlib.Path(repo_dir) / "promoted.txt").write_text("promoted")
            git("add", "promoted.txt")
            git("commit", "-m", "fix: promoted candidate")
            promoted_sha = git("rev-parse", "HEAD").stdout.strip()
            git("tag", "-a", "staging_2608191200_2222222", promoted_sha, "-m", "Promoted")

            # Commit on main that was never promoted
            (pathlib.Path(repo_dir) / "emergency.txt").write_text("hotfix")
            git("add", "emergency.txt")
            git("commit", "-m", "feat: emergency hotfix")
            head_sha = git("rev-parse", "HEAD").stdout.strip()

            gh_out = pathlib.Path(repo_dir) / "gh_out.txt"
            proc = self._run_calc_script(
                repo_dir,
                args=["", ""],
                env={"SKIP_STAGING_VALIDATION": "true", "GITHUB_OUTPUT": str(gh_out)},
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Emergency override", proc.stderr)
            self.assertEqual(proc.stdout.strip(), "0.2.0")

            outputs = gh_out.read_text()
            self.assertIn("release_version=0.2.0", outputs)
            self.assertIn("version=0.2.0", outputs)
            self.assertIn(f"release_commit={head_sha}", outputs)
            self.assertNotIn(promoted_sha, outputs)
        finally:
            temp_dir.cleanup()

    def test_without_the_override_the_promoted_commit_wins_over_head(self):
        """The other side of the branch above, so neither can pass by coincidence."""
        temp_dir, repo_dir, git = self._create_mock_repo()
        try:
            git("tag", "-a", "0.1.0", "-m", "Release 0.1.0")

            (pathlib.Path(repo_dir) / "promoted.txt").write_text("promoted")
            git("add", "promoted.txt")
            git("commit", "-m", "fix: promoted candidate")
            promoted_sha = git("rev-parse", "HEAD").stdout.strip()
            git("tag", "-a", "staging_2608191200_2222222", promoted_sha, "-m", "Promoted")

            (pathlib.Path(repo_dir) / "emergency.txt").write_text("hotfix")
            git("add", "emergency.txt")
            git("commit", "-m", "feat: unpromoted commit")

            gh_out = pathlib.Path(repo_dir) / "gh_out.txt"
            proc = self._run_calc_script(
                repo_dir,
                args=["", ""],
                env={"GITHUB_OUTPUT": str(gh_out)},
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "0.1.1")
            self.assertIn(f"release_commit={promoted_sha}", gh_out.read_text())
        finally:
            temp_dir.cleanup()


    # ─── release lines ───────────────────────────────────────────────────────
    # A patch is cut from release/<X.Y>: its candidate is the line's head, its
    # base the line's last release, its bump PATCH. Once a line has a branch,
    # main keeps off that line's patch numbers.

    _STAMP = "chore(release): stamp release version"

    def _line_repo(self):
        """main A—B, stamped 0.2.0 off B, release/0.2 from that stamp with one fix on it."""
        temp_dir, repo_dir, git = self._create_mock_repo()
        self.addCleanup(temp_dir.cleanup)
        repo = pathlib.Path(repo_dir)
        (repo / "b.txt").write_text("b\n")
        git("add", "b.txt")
        git("commit", "-m", "feat: b")
        b = git("rev-parse", "HEAD").stdout.strip()
        git("switch", "--detach", "-q", b)
        (repo / "stamp.txt").write_text("0.2.0\n")
        git("add", "stamp.txt")
        git("commit", "-m", f"{self._STAMP} 0.2.0")
        git("tag", "-a", "0.2.0", "-m", "release 0.2.0")
        git("switch", "-c", f"release/{MOCK_TARGET_RELEASE_LINE}")
        (repo / "l1.txt").write_text("fix\n")
        git("add", "l1.txt")
        git("commit", "-m", "fix: backport")
        l1 = git("rev-parse", "HEAD").stdout.strip()
        git("switch", "main")
        return repo_dir, git, {"B": b, "L1": l1}

    def _outputs(self, repo_dir):
        text = (pathlib.Path(repo_dir) / "gh_out.txt").read_text()
        return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)

    def _run_line(self, repo_dir, env=None):
        gh_out = pathlib.Path(repo_dir) / "gh_out.txt"
        gh_out.write_text("")
        overrides = {"GITHUB_OUTPUT": str(gh_out)}
        overrides.update(env or {})
        return self._run_calc_script(repo_dir, env=overrides)

    def test_a_release_line_bumps_patch_from_its_own_base(self):
        repo_dir, git, shas = self._line_repo()
        proc = self._run_line(repo_dir, env={"RELEASE_LINE": MOCK_TARGET_RELEASE_LINE})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), MOCK_LINE_PATCH_RELEASE_TAG)
        outputs = self._outputs(repo_dir)
        self.assertEqual(outputs["bump_type"], "patch")
        self.assertEqual(outputs["previous_version"], "0.2.0")
        self.assertEqual(outputs["rc_candidate_commit"], shas["L1"])

    def test_a_release_line_refuses_a_feature_or_a_breaking_change(self):
        for subject, phrase in (("feat: not a fix", "carries a feature"), ("fix!: breaks", "carries a breaking change")):
            with self.subTest(subject=subject):
                repo_dir, git, _ = self._line_repo()
                git("switch", f"release/{MOCK_TARGET_RELEASE_LINE}")
                (pathlib.Path(repo_dir) / "more.txt").write_text(subject)
                git("add", "more.txt")
                git("commit", "-m", subject)
                git("switch", "main")
                proc = self._run_line(repo_dir, env={"RELEASE_LINE": MOCK_TARGET_RELEASE_LINE})
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(phrase, proc.stderr)
                self.assertIn("explicit_release_version", proc.stderr)

    def test_an_explicit_version_on_a_line_must_be_on_the_line_and_may_trail_main(self):
        repo_dir, git, shas = self._line_repo()
        # main has moved on to 0.3.0: a stamped release off a later main commit.
        (pathlib.Path(repo_dir) / "c.txt").write_text("c\n")
        git("add", "c.txt")
        git("commit", "-m", "feat: c")
        c = git("rev-parse", "HEAD").stdout.strip()
        git("switch", "--detach", "-q", c)
        (pathlib.Path(repo_dir) / "stamp.txt").write_text("0.3.0\n")
        git("add", "stamp.txt")
        git("commit", "-m", f"{self._STAMP} 0.3.0")
        git("tag", "-a", "0.3.0", "-m", "release 0.3.0")
        git("switch", "main")

        ok = self._run_line(repo_dir, env={"RELEASE_LINE": MOCK_TARGET_RELEASE_LINE, "EXPLICIT_RELEASE_VERSION": MOCK_LINE_PATCH_RELEASE_TAG})
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(ok.stdout.strip(), MOCK_LINE_PATCH_RELEASE_TAG)
        off = self._run_line(repo_dir, env={"RELEASE_LINE": MOCK_TARGET_RELEASE_LINE, "EXPLICIT_RELEASE_VERSION": "0.3.5"})
        self.assertNotEqual(off.returncode, 0)
        self.assertIn("is not on release line 0.2", off.stderr)

    def test_an_idle_line_reports_no_changes(self):
        repo_dir, git, shas = self._line_repo()
        git("switch", f"release/{MOCK_TARGET_RELEASE_LINE}")
        (pathlib.Path(repo_dir) / "stamp.txt").write_text("0.2.1\n")
        git("add", "stamp.txt")
        git("commit", "-m", f"{self._STAMP} 0.2.1")
        git("tag", "-a", MOCK_LINE_PATCH_RELEASE_TAG, "-m", f"release {MOCK_LINE_PATCH_RELEASE_TAG}")
        git("switch", "main")
        proc = self._run_line(repo_dir, env={"RELEASE_LINE": MOCK_TARGET_RELEASE_LINE})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        outputs = self._outputs(repo_dir)
        self.assertEqual(outputs["has_changes"], "false")
        self.assertEqual(outputs["release_version"], MOCK_LINE_PATCH_RELEASE_TAG)
        self.assertEqual(outputs["rc_candidate_commit"], shas["L1"])

    def test_a_target_commit_that_is_not_the_line_head_is_refused(self):
        repo_dir, git, shas = self._line_repo()
        proc = self._run_line(repo_dir, env={"RELEASE_LINE": MOCK_TARGET_RELEASE_LINE, "TARGET_COMMIT": shas["B"]})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("resolves to its own head", proc.stderr)
        same = self._run_line(repo_dir, env={"RELEASE_LINE": MOCK_TARGET_RELEASE_LINE, "TARGET_COMMIT": shas["L1"]})
        self.assertEqual(same.returncode, 0, same.stderr)

    def test_a_missing_line_or_a_malformed_one_is_refused(self):
        repo_dir, git, _ = self._line_repo()
        missing = self._run_line(repo_dir, env={"RELEASE_LINE": "9.9"})
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("No local release line", missing.stderr)
        malformed = self._run_line(repo_dir, env={"RELEASE_LINE": "0.2.0"})
        self.assertNotEqual(malformed.returncode, 0)
        self.assertIn("is not a release line", malformed.stderr)

    def test_main_ignores_a_line_release_and_bumps_minor_once_the_line_has_a_branch(self):
        """After 0.2.1 on release/0.2, main's base is still 0.2.0 and a fix-only week is 0.3.0."""
        repo_dir, git, shas = self._line_repo()
        git("switch", f"release/{MOCK_TARGET_RELEASE_LINE}")
        (pathlib.Path(repo_dir) / "stamp.txt").write_text("0.2.1\n")
        git("add", "stamp.txt")
        git("commit", "-m", f"{self._STAMP} 0.2.1")
        git("tag", "-a", MOCK_LINE_PATCH_RELEASE_TAG, "-m", f"release {MOCK_LINE_PATCH_RELEASE_TAG}")
        git("switch", "main")
        (pathlib.Path(repo_dir) / "c.txt").write_text("c\n")
        git("add", "c.txt")
        git("commit", "-m", "fix: only a fix on main")
        c = git("rev-parse", "HEAD").stdout.strip()

        proc = self._run_line(repo_dir, env={"TARGET_COMMIT": c})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "0.3.0")
        outputs = self._outputs(repo_dir)
        self.assertEqual(outputs["previous_version"], "0.2.0")
        self.assertEqual(outputs["bump_type"], "minor-line")

        refused = self._run_line(repo_dir, env={"TARGET_COMMIT": c, "EXPLICIT_RELEASE_VERSION": "0.2.9"})
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("release it from release/0.2", refused.stderr)

    def test_a_re_run_of_the_release_that_created_the_line_is_admitted_on_main(self):
        """After the atomic push, "Re-run failed jobs" replays the same explicit version.

        The base is then the release itself and its line exists; an explicit
        version equal to the base is that resume, which the collision check
        admits when the tag sits on this candidate's stamp, not a new patch on
        the line to refuse.
        """
        repo_dir, git, shas = self._line_repo()
        # release/0.2 exists (created from the stamp); re-run 0.2.0 from its candidate B.
        proc = self._run_line(repo_dir, env={"EXPLICIT_RELEASE_VERSION": "0.2.0", "TARGET_COMMIT": shas["B"]})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "0.2.0")
        outputs = self._outputs(repo_dir)
        self.assertEqual(outputs["bump_type"], "manual")
        # A different patch on that line from main is still refused.
        refused = self._run_line(repo_dir, env={"EXPLICIT_RELEASE_VERSION": MOCK_LINE_PATCH_RELEASE_TAG, "TARGET_COMMIT": shas["B"]})
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("release it from release/0.2", refused.stderr)

    def test_a_line_release_that_died_after_its_push_is_resumed_by_naming_its_version(self):
        """After 0.2.1's push a backport lands; a plain re-dispatch computes 0.2.2, the pin finishes 0.2.1."""
        repo_dir, git, shas = self._line_repo()
        git("switch", f"release/{MOCK_TARGET_RELEASE_LINE}")
        (pathlib.Path(repo_dir) / "stamp.txt").write_text("0.2.1\n")
        git("add", "stamp.txt")
        git("commit", "-m", f"{self._STAMP} 0.2.1")
        git("tag", "-a", MOCK_LINE_PATCH_RELEASE_TAG, "-m", f"release {MOCK_LINE_PATCH_RELEASE_TAG}")
        (pathlib.Path(repo_dir) / "l2.txt").write_text("fix\n")
        git("add", "l2.txt")
        git("commit", "-m", "fix: landed after the push")
        l2 = git("rev-parse", "HEAD").stdout.strip()
        git("switch", "main")

        plain = self._run_line(repo_dir, env={"RELEASE_LINE": MOCK_TARGET_RELEASE_LINE})
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertEqual(plain.stdout.strip(), "0.2.2")
        self.assertEqual(self._outputs(repo_dir)["rc_candidate_commit"], l2)

        pinned = self._run_line(repo_dir, env={"RELEASE_LINE": MOCK_TARGET_RELEASE_LINE, "EXPLICIT_RELEASE_VERSION": MOCK_LINE_PATCH_RELEASE_TAG})
        self.assertEqual(pinned.returncode, 0, pinned.stderr)
        self.assertEqual(pinned.stdout.strip(), MOCK_LINE_PATCH_RELEASE_TAG)
        outputs = self._outputs(repo_dir)
        self.assertEqual(outputs["rc_candidate_commit"], shas["L1"])
        self.assertEqual(outputs["bump_type"], "manual")

    _FAKE_RELEASE_REPO = {"GH_ORG": "no-such-org-kube-agents", "GH_REPO": "no-such-repo"}

    def _release_repository_for(self, git, repo_dir):
        """A bare repository standing in for the release repository, as in CI: the
        calculator fetches its tags and its `main` from the composed GitHub URL,
        rewritten onto the bare path."""
        bare_dir = pathlib.Path(repo_dir).parent / "release.git"
        git("init", "--bare", str(bare_dir))
        git("config", f"url.{bare_dir}.insteadOf", f"https://github.com/{self._FAKE_RELEASE_REPO['GH_ORG']}/{self._FAKE_RELEASE_REPO['GH_REPO']}.git")
        git("push", "--quiet", str(bare_dir), "main", "--tags")
        return bare_dir

    def test_a_target_commit_off_main_is_refused_without_a_release_line(self):
        """In CI a release-line commit named as main's target would stamp a release main
        never sees as its base; main is the release repository's, fetched then and there."""
        repo_dir, git, shas = self._line_repo()
        self._release_repository_for(git, repo_dir)
        proc = self._run_line(repo_dir, env={"CI": "true", "TARGET_COMMIT": shas["L1"], **self._FAKE_RELEASE_REPO})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("is not on main", proc.stderr)
        self.assertIn("release_line", proc.stderr)

    def test_off_ci_a_stale_tracking_main_does_not_refuse_the_candidate(self):
        """The standard fork clone has origin/main weeks behind: off CI the read is advisory.

        release_main_ref prefers refs/remotes/origin/main off CI. With it behind the
        candidate the calculator warns, qualifies the base against the candidate
        alone, and prints the version; only CI refuses.
        """
        repo_dir, git, shas = self._line_repo()
        (pathlib.Path(repo_dir) / "c.txt").write_text("c\n")
        git("add", "c.txt")
        git("commit", "-m", "fix: c")
        head = git("rev-parse", "HEAD").stdout.strip()
        git("update-ref", "refs/remotes/origin/main", shas["B"])

        proc = self._run_line(repo_dir, env={"TARGET_COMMIT": head})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("is not on the main this checkout can see", proc.stderr)
        self.assertIn("qualifying the GA base against the candidate alone", proc.stderr)
        self.assertNotIn("❌", proc.stderr)
        self.assertEqual(self._outputs(repo_dir)["previous_version"], "0.2.0")

    def test_a_release_cut_by_hand_from_a_later_main_commit_still_supersedes_an_older_candidate(self):
        """The emergency-leftover shape: main's base is qualified against main's head."""
        repo_dir, git, shas = self._line_repo()
        (pathlib.Path(repo_dir) / "c.txt").write_text("c\n")
        git("add", "c.txt")
        git("commit", "-m", "fix: c")
        c = git("rev-parse", "HEAD").stdout.strip()
        (pathlib.Path(repo_dir) / "h.txt").write_text("h\n")
        git("add", "h.txt")
        git("commit", "-m", "fix: hotfix")
        h = git("rev-parse", "HEAD").stdout.strip()
        git("switch", "--detach", "-q", h)
        (pathlib.Path(repo_dir) / "stamp.txt").write_text("0.2.1\n")
        git("add", "stamp.txt")
        git("commit", "-m", f"{self._STAMP} 0.2.1")
        git("tag", "-a", MOCK_LINE_PATCH_RELEASE_TAG, "-m", f"emergency {MOCK_LINE_PATCH_RELEASE_TAG}")
        git("switch", "main")

        proc = self._run_line(repo_dir, env={"TARGET_COMMIT": c})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        outputs = self._outputs(repo_dir)
        self.assertEqual(outputs["has_changes"], "false")
        self.assertEqual(outputs["release_version"], MOCK_LINE_PATCH_RELEASE_TAG)

if __name__ == "__main__":
    unittest.main()
