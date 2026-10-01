"""Unit tests for scripts/release/tag_ga_release.sh.

Tests argument validation, pure numeric SemVer enforcement, Git tag creation,
and idempotency on mock repositories.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

from tests.testing.common import (
    INVALID_GA_RELEASE_TAGS,
    MOCK_SAMPLE_COMMIT_SHA,
    VALID_GA_RELEASE_TAGS,
    create_mock_git_repo,
    get_isolated_test_env,
)
from tests.testing.release import (
    MOCK_INITIAL_VERSION,
    MOCK_EXPLICIT_RELEASE_VERSION_NEXT,
    MOCK_LINE_PATCH_RELEASE_TAG,
    MOCK_TARGET_RELEASE_LINE,
    MOCK_TARGET_RELEASE_TAG,
    populate_mock_release_files,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TAG_GA_RELEASE_SH = _REPO_ROOT / "scripts" / "release" / "tag_ga_release.sh"
_CALCULATE_NEXT_VERSION_SH = _REPO_ROOT / "scripts" / "release" / "calculate_next_version.sh"


class TagGAReleaseScriptTest(unittest.TestCase):
    def _run_script(self, args, env=None, cwd=None):
        full_env = get_isolated_test_env(overrides=env)
        return subprocess.run(
            ["bash", str(_TAG_GA_RELEASE_SH)] + args,
            capture_output=True,
            text=True,
            env=full_env,
            cwd=cwd or str(_REPO_ROOT),
        )

    def _populate_valid_release_files(self, repo_dir):
        populate_mock_release_files(repo_dir)

    _FAKE_RELEASE_REPO = {"GH_ORG": "no-such-org-kube-agents", "GH_REPO": "no-such-repo"}

    def _bare_origin_for(self, git, repo_dir, checkout=None):
        """A bare `origin` that is also the release repository, as in the CI job.

        The scripts compose `https://github.com/<GH_ORG>/<GH_REPO>.git` for their
        remote lookups and their push fallback. Rewriting it onto the same bare
        path keeps the tests off the network and away from the developer's
        credential helper, and matches the publish job, whose `origin` is the
        release repository. `checkout` is another working copy of the same bare
        repository that needs the same rewrite.
        """
        bare_dir = pathlib.Path(repo_dir).parent / "origin.git"
        if checkout is None:
            git("init", "--bare", str(bare_dir))
            git("remote", "add", "origin", str(bare_dir))
        git(
            "config",
            f"url.{bare_dir}.insteadOf",
            f"https://github.com/{self._FAKE_RELEASE_REPO['GH_ORG']}/{self._FAKE_RELEASE_REPO['GH_REPO']}.git",
            cwd=checkout or repo_dir,
        )
        return bare_dir

    def test_missing_arguments(self):
        proc = self._run_script([])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("RELEASE_VERSION and RC candidate commit are required", proc.stderr)

    def test_invalid_tag_format(self):
        for bad_tag in INVALID_GA_RELEASE_TAGS:
            with self.subTest(bad_tag=bad_tag):
                proc = self._run_script([bad_tag, MOCK_SAMPLE_COMMIT_SHA])
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("not a valid pure numeric SemVer", proc.stderr)

    def test_tag_creation_and_idempotency(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            self._populate_valid_release_files(repo_dir)
            git("add", ".")
            git("commit", "-m", "feat: populate release files")
            head_commit = git("rev-parse", "HEAD").stdout.strip()

            # First tag creation
            proc = self._run_script([MOCK_TARGET_RELEASE_TAG, head_commit], cwd=repo_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("CREATING AND PUSHING GA RELEASE GIT TAG", proc.stdout)

            # Verify tag exists in repo and points to stamped release commit
            tag_commit = git("rev-parse", f"{MOCK_TARGET_RELEASE_TAG}^{{commit}}").stdout.strip()
            self.assertNotEqual(tag_commit, head_commit)
            parent_sha = git("rev-parse", f"{tag_commit}^1").stdout.strip()
            self.assertEqual(parent_sha, head_commit)

            # Second execution: Idempotent skip
            proc2 = self._run_script([MOCK_TARGET_RELEASE_TAG, head_commit], cwd=repo_dir)
            self.assertEqual(proc2.returncode, 0, proc2.stderr)
            self.assertIn("Idempotent skip", proc2.stdout)
        finally:
            temp_dir.cleanup()

    def test_env_vars_invocation_without_args(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            self._populate_valid_release_files(repo_dir)
            git("add", ".")
            git("commit", "-m", "feat: populate release files")
            head_commit = git("rev-parse", "HEAD").stdout.strip()

            proc = self._run_script(
                [],
                env={"RELEASE_VERSION": MOCK_EXPLICIT_RELEASE_VERSION_NEXT, "RC_CANDIDATE_COMMIT": head_commit},
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            tag_commit = git("rev-parse", f"{MOCK_EXPLICIT_RELEASE_VERSION_NEXT}^{{commit}}").stdout.strip()
            parent_sha = git("rev-parse", f"{tag_commit}^1").stdout.strip()
            self.assertEqual(parent_sha, head_commit)
        finally:
            temp_dir.cleanup()

    def test_strict_argument_order_rejects_swapped_args(self):
        """Verifies tag_ga_release.sh strictly requires SemVer as first argument."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            head_commit = git("rev-parse", "HEAD").stdout.strip()

            proc = self._run_script(
                [head_commit, MOCK_TARGET_RELEASE_TAG],
                cwd=repo_dir,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("not a valid pure numeric SemVer", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_stamps_baked_release_version_on_detached_head(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            self._populate_valid_release_files(repo_dir)
            git("add", ".")
            git("commit", "-m", "feat: populate release files")
            main_commit = git("rev-parse", "HEAD").stdout.strip()

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG, main_commit],
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

            # 1. Main branch is untouched (still points to main_commit and HEAD remains on main)
            current_main = git("rev-parse", "main").stdout.strip()
            self.assertEqual(current_main, main_commit)
            current_branch = git("symbolic-ref", "--short", "HEAD").stdout.strip()
            self.assertEqual(current_branch, "main")

            # 2. Release tag exists and points to stamped commit (different from main)
            tag_commit = git("rev-parse", f"{MOCK_TARGET_RELEASE_TAG}^{{commit}}").stdout.strip()
            self.assertNotEqual(tag_commit, main_commit)

            # 3. Content at tag has BAKED_RELEASE_VERSION stamped with release tag
            tag_install_content = git("show", f"{MOCK_TARGET_RELEASE_TAG}:install.sh").stdout
            self.assertIn(f'BAKED_RELEASE_VERSION="{MOCK_TARGET_RELEASE_TAG}"', tag_install_content)
        finally:
            temp_dir.cleanup()

    def test_fails_loudly_if_candidate_commit_unresolvable(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            main_commit = git("rev-parse", "HEAD").stdout.strip()
            # Pass a nonexistent SHA as candidate commit
            bad_sha = "0123456789abcdef0123456789abcdef01234567"
            proc = self._run_script([MOCK_TARGET_RELEASE_TAG, bad_sha], cwd=repo_dir)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Failed to checkout candidate commit", proc.stderr)

            # Ensure main branch is untouched and no tag was created
            current_main = git("rev-parse", "main").stdout.strip()
            self.assertEqual(current_main, main_commit)
            tag_check = git("tag", "-l", MOCK_TARGET_RELEASE_TAG).stdout.strip()
            self.assertEqual(tag_check, "")
        finally:
            temp_dir.cleanup()

    def test_fails_loudly_when_installer_lacks_baked_version_placeholder(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            self._populate_valid_release_files(repo_dir)
            # Create installer script WITHOUT BAKED_RELEASE_VERSION placeholder
            install_sh = pathlib.Path(repo_dir) / "install.sh"
            install_sh.write_text('#!/bin/bash\necho "no baked placeholder here"\n')
            git("add", ".")
            git("commit", "-m", "feat: legacy installer without placeholder")
            main_commit = git("rev-parse", "HEAD").stdout.strip()

            proc = self._run_script([MOCK_TARGET_RELEASE_TAG, main_commit], cwd=repo_dir)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Failed to stamp BAKED_RELEASE_VERSION in install.sh", proc.stderr)

            # Ensure no tag was created
            tag_check = git("tag", "-l", MOCK_TARGET_RELEASE_TAG).stdout.strip()
            self.assertEqual(tag_check, "")
        finally:
            temp_dir.cleanup()

    def test_stamps_helm_and_terraform_versions_on_detached_head(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            self._populate_valid_release_files(repo_dir)
            git("add", ".")
            git("commit", "-m", "feat: initial project structure with scripts, helm and terraform")
            main_commit = git("rev-parse", "HEAD").stdout.strip()

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG, main_commit],
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

            # 1. Main branch remains untouched and clean
            current_main = git("rev-parse", "main").stdout.strip()
            self.assertEqual(current_main, main_commit)
            current_branch = git("symbolic-ref", "--short", "HEAD").stdout.strip()
            self.assertEqual(current_branch, "main")

            # Verify files on main still have original values
            chart_yaml = pathlib.Path(repo_dir) / "charts" / "kube-agents" / "Chart.yaml"
            variables_tf = pathlib.Path(repo_dir) / "terraform" / "examples" / "full-install" / "variables.tf"
            tfvars_example = pathlib.Path(repo_dir) / "terraform" / "examples" / "full-install" / "terraform.tfvars.example"
            main_chart = chart_yaml.read_text()
            self.assertIn("version: 0.1.0", main_chart)
            self.assertIn('appVersion: "0.1.0"', main_chart)
            main_var = variables_tf.read_text()
            self.assertIn('default     = "0.1.0"', main_var)
            main_tfvars = tfvars_example.read_text()
            self.assertIn('# image_tag = "0.1.0"', main_tfvars)

            # 2. Release tag points to stamped commit
            tag_commit = git("rev-parse", f"{MOCK_TARGET_RELEASE_TAG}^{{commit}}").stdout.strip()
            self.assertNotEqual(tag_commit, main_commit)

            # 3. All files stamped at the tag ref
            for script in ["install.sh", "uninstall.sh", "upgrade.sh"]:
                content = git("show", f"{MOCK_TARGET_RELEASE_TAG}:{script}").stdout
                self.assertIn(f'BAKED_RELEASE_VERSION="{MOCK_TARGET_RELEASE_TAG}"', content)

            tagged_chart = git("show", f"{MOCK_TARGET_RELEASE_TAG}:charts/kube-agents/Chart.yaml").stdout
            self.assertIn(f"version: {MOCK_TARGET_RELEASE_TAG}", tagged_chart)
            self.assertIn(f'appVersion: "{MOCK_TARGET_RELEASE_TAG}"', tagged_chart)

            tagged_vars = git("show", f"{MOCK_TARGET_RELEASE_TAG}:terraform/examples/full-install/variables.tf").stdout
            self.assertIn(f'default     = "{MOCK_TARGET_RELEASE_TAG}"', tagged_vars)

            tagged_tfvars = git("show", f"{MOCK_TARGET_RELEASE_TAG}:terraform/examples/full-install/terraform.tfvars.example").stdout
            self.assertIn(f'# image_tag = "{MOCK_TARGET_RELEASE_TAG}"', tagged_tfvars)

            # 4. Idempotency test: re-running with existing tag succeeds and skips cleanly
            proc2 = self._run_script([MOCK_TARGET_RELEASE_TAG, main_commit], cwd=repo_dir)
            self.assertEqual(proc2.returncode, 0, proc2.stderr)
            self.assertIn("Idempotent skip", proc2.stdout)
        finally:
            temp_dir.cleanup()

    def test_fails_loudly_when_helm_chart_lacks_version_field(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            self._populate_valid_release_files(repo_dir)
            chart_yaml = pathlib.Path(repo_dir) / "charts" / "kube-agents" / "Chart.yaml"
            chart_yaml.write_text('apiVersion: v2\nname: kube-agents\n')
            git("add", ".")
            git("commit", "-m", "feat: malformed Chart.yaml")
            main_commit = git("rev-parse", "HEAD").stdout.strip()

            proc = self._run_script([MOCK_TARGET_RELEASE_TAG, main_commit], cwd=repo_dir)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Failed to stamp version in", proc.stderr)

            tag_check = git("tag", "-l", MOCK_TARGET_RELEASE_TAG).stdout.strip()
            self.assertEqual(tag_check, "")
        finally:
            temp_dir.cleanup()

    def test_fails_loudly_when_terraform_variables_lacks_image_tag_default(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            self._populate_valid_release_files(repo_dir)
            tf_dir = pathlib.Path(repo_dir) / "terraform" / "examples" / "full-install"
            (tf_dir / "variables.tf").write_text('variable "project_id" { type = string }\n')
            git("add", ".")
            git("commit", "-m", "feat: variables.tf without image_tag")
            main_commit = git("rev-parse", "HEAD").stdout.strip()

            proc = self._run_script([MOCK_TARGET_RELEASE_TAG, main_commit], cwd=repo_dir)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Failed to stamp image_tag default in", proc.stderr)

            tag_check = git("tag", "-l", MOCK_TARGET_RELEASE_TAG).stdout.strip()
            self.assertEqual(tag_check, "")
        finally:
            temp_dir.cleanup()

    def test_fails_loudly_when_installer_script_missing(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            self._populate_valid_release_files(repo_dir)
            (pathlib.Path(repo_dir) / "uninstall.sh").unlink()
            git("add", ".")
            git("commit", "-m", "feat: missing uninstall.sh")
            main_commit = git("rev-parse", "HEAD").stdout.strip()

            proc = self._run_script([MOCK_TARGET_RELEASE_TAG, main_commit], cwd=repo_dir)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Target installer script not found at", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_fails_loudly_when_helm_chart_missing(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            self._populate_valid_release_files(repo_dir)
            (pathlib.Path(repo_dir) / "charts" / "kube-agents" / "Chart.yaml").unlink()
            git("add", ".")
            git("commit", "-m", "feat: missing Chart.yaml")
            main_commit = git("rev-parse", "HEAD").stdout.strip()

            proc = self._run_script([MOCK_TARGET_RELEASE_TAG, main_commit], cwd=repo_dir)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Helm chart file not found at", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_fails_loudly_when_terraform_file_missing(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            self._populate_valid_release_files(repo_dir)
            (pathlib.Path(repo_dir) / "terraform" / "examples" / "full-install" / "variables.tf").unlink()
            git("add", ".")
            git("commit", "-m", "feat: missing variables.tf")
            main_commit = git("rev-parse", "HEAD").stdout.strip()

            proc = self._run_script([MOCK_TARGET_RELEASE_TAG, main_commit], cwd=repo_dir)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Terraform variables file not found at", proc.stderr)
        finally:
            temp_dir.cleanup()

        temp_dir2, repo_dir2, git2 = create_mock_git_repo()
        try:
            self._populate_valid_release_files(repo_dir2)
            (pathlib.Path(repo_dir2) / "terraform" / "examples" / "full-install" / "terraform.tfvars.example").unlink()
            git2("add", ".")
            git2("commit", "-m", "feat: missing terraform.tfvars.example")
            main_commit2 = git2("rev-parse", "HEAD").stdout.strip()

            proc = self._run_script([MOCK_TARGET_RELEASE_TAG, main_commit2], cwd=repo_dir2)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Terraform example tfvars file not found at", proc.stderr)
        finally:
            temp_dir2.cleanup()

    def test_preserves_unrelated_uncommitted_files_on_stamping_and_idempotent_skip(self):
        """Verifies create_stamped_release_commit does not destroy uncommitted caller work."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            self._populate_valid_release_files(repo_dir)
            repo_path = pathlib.Path(repo_dir)

            # Create an unrelated tracked file with initial content
            unrelated_file = repo_path / "mywork.txt"
            unrelated_file.write_text("INITIAL WORK\n")

            git("add", ".")
            git("commit", "-m", "feat: initial commit with files")
            main_commit = git("rev-parse", "HEAD").stdout.strip()

            # Caller has uncommitted edits in the unrelated tracked file
            uncommitted_content = "INITIAL WORK\nMY PRECIOUS UNCOMMITTED EDITS\n"
            unrelated_file.write_text(uncommitted_content)

            # 1. First execution: stamping path creates stamped release tag
            proc = self._run_script([MOCK_TARGET_RELEASE_TAG, main_commit], cwd=repo_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)

            # Verify unrelated file was NOT destroyed or reset
            self.assertEqual(unrelated_file.read_text(), uncommitted_content)
            status_out = git("status", "--porcelain", "mywork.txt").stdout.strip()
            self.assertEqual(status_out, "M mywork.txt")

            # Verify tag was created and points to stamped commit
            tag_commit = git("rev-parse", f"{MOCK_TARGET_RELEASE_TAG}^{{commit}}").stdout.strip()
            self.assertNotEqual(tag_commit, main_commit)

            # 2. Second execution: idempotent early-return path
            proc2 = self._run_script([MOCK_TARGET_RELEASE_TAG, main_commit], cwd=repo_dir)
            self.assertEqual(proc2.returncode, 0, proc2.stderr)
            self.assertIn("Idempotent skip", proc2.stdout)

            # Verify unrelated file is STILL untouched after idempotent return
            self.assertEqual(unrelated_file.read_text(), uncommitted_content)
            status_out2 = git("status", "--porcelain", "mywork.txt").stdout.strip()
            self.assertEqual(status_out2, "M mywork.txt")
        finally:
            temp_dir.cleanup()

    def test_fails_loudly_when_candidate_release_files_are_dirty(self):
        """Verifies tag_ga_release.sh fails and aborts without touching tree if candidate files are dirty."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            self._populate_valid_release_files(repo_dir)
            repo_path = pathlib.Path(repo_dir)
            git("add", ".")
            git("commit", "-m", "feat: initial commit with valid release files")
            main_commit = git("rev-parse", "HEAD").stdout.strip()

            # Introduce an uncommitted scratch edit to a candidate release file (Chart.yaml)
            chart_file = repo_path / "charts" / "kube-agents" / "Chart.yaml"
            original_content = chart_file.read_text()
            dirty_content = original_content + "\ndescription: MY LOCAL SCRATCH EDIT\n"
            chart_file.write_text(dirty_content)

            # tag_ga_release.sh MUST fail loudly and refuse to create stamped release commit
            proc = self._run_script([MOCK_TARGET_RELEASE_TAG, main_commit], cwd=repo_dir)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Cannot create stamped release commit with uncommitted changes in release files", proc.stderr)
            self.assertIn("charts/kube-agents/Chart.yaml", proc.stderr)

            # Verify no release tag was created
            tags = git("tag").stdout.splitlines()
            self.assertNotIn(MOCK_TARGET_RELEASE_TAG, tags)

            # Verify the caller's scratch edit was preserved and not wiped by any trap
            self.assertEqual(chart_file.read_text(), dirty_content)
        finally:
            temp_dir.cleanup()


    # ─── the release branch ──────────────────────────────────────────────────
    # The stamped commit is pushed to its release line `release/<X.Y>` together
    # with the tag, so a release commit belongs to a branch on the repository
    # rather than to its tag alone, in one atomic push (ensure_ga_release_refs).
    # tests/test_release_common.py owns the placement and fast-forward contract;
    # these pin what the GA tagger does with it.

    def test_creates_the_release_branch_at_the_stamped_commit(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            self._populate_valid_release_files(repo_dir)
            git("add", ".")
            git("commit", "-m", "feat: populate release files")
            main_commit = git("rev-parse", "HEAD").stdout.strip()
            branch = f"release/{MOCK_TARGET_RELEASE_LINE}"

            proc = self._run_script([MOCK_TARGET_RELEASE_TAG, main_commit], cwd=repo_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(f"Release Line:        {branch}", proc.stdout)

            tag_commit = git("rev-parse", f"{MOCK_TARGET_RELEASE_TAG}^{{commit}}").stdout.strip()
            self.assertEqual(git("rev-parse", branch).stdout.strip(), tag_commit)
            self.assertNotEqual(tag_commit, main_commit)
            self.assertEqual(git("rev-parse", "main").stdout.strip(), main_commit)
            self.assertEqual(git("symbolic-ref", "--short", "HEAD").stdout.strip(), "main")

            # Re-running finds both the branch and the tag where it left them.
            proc2 = self._run_script([MOCK_TARGET_RELEASE_TAG, main_commit], cwd=repo_dir)
            self.assertEqual(proc2.returncode, 0, proc2.stderr)
            self.assertEqual(git("rev-parse", branch).stdout.strip(), tag_commit)
        finally:
            temp_dir.cleanup()

    def test_in_ci_pushes_the_branch_and_the_tag(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            self._populate_valid_release_files(repo_dir)
            git("add", ".")
            git("commit", "-m", "feat: populate release files")
            main_commit = git("rev-parse", "HEAD").stdout.strip()
            bare_dir = self._bare_origin_for(git, repo_dir)
            branch = f"release/{MOCK_TARGET_RELEASE_LINE}"

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG, main_commit],
                env={"CI": "true", **self._FAKE_RELEASE_REPO},
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

            def remote(ref):
                return git("--git-dir", str(bare_dir), "rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip()

            tag_commit = git("rev-parse", f"{MOCK_TARGET_RELEASE_TAG}^{{commit}}").stdout.strip()
            self.assertEqual(remote(f"refs/heads/{branch}"), tag_commit)
            self.assertEqual(remote(f"refs/tags/{MOCK_TARGET_RELEASE_TAG}"), tag_commit)
            # One atomic push carries both refs.
            self.assertIn(f"Git tag '{MOCK_TARGET_RELEASE_TAG}' and release line '{branch}' successfully pushed", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_a_release_branch_at_another_commit_is_refused_before_the_tag_is_pushed(self):
        """The branch check runs before the tag, so a collision leaves nothing behind.

        The tag is the one artefact a failed run cannot take back; finding the
        stray branch only after pushing it would leave the operator holding a GA
        tag for a release that did not finish.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            self._populate_valid_release_files(repo_dir)
            git("add", ".")
            git("commit", "-m", "feat: populate release files")
            main_commit = git("rev-parse", "HEAD").stdout.strip()
            bare_dir = self._bare_origin_for(git, repo_dir)
            branch = f"release/{MOCK_TARGET_RELEASE_LINE}"

            # The remote already holds this release's branch at a commit that
            # diverged from the one about to be stamped.
            git("switch", "-c", "elsewhere")
            (pathlib.Path(repo_dir) / "elsewhere.txt").write_text("elsewhere\n")
            git("add", "elsewhere.txt")
            git("commit", "-m", "feat: elsewhere")
            stray = git("rev-parse", "HEAD").stdout.strip()
            git("push", "origin", f"HEAD:refs/heads/{branch}")
            git("switch", "main")

            proc = self._run_script(
                [MOCK_TARGET_RELEASE_TAG, main_commit],
                env={"CI": "true", **self._FAKE_RELEASE_REPO},
                cwd=repo_dir,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn(f"Release line '{branch}' already exists on", proc.stderr)
            # The placement is read before the tag is created, locally or remotely.
            self.assertEqual(git("tag", "-l", MOCK_TARGET_RELEASE_TAG).stdout.strip(), "")
            self.assertEqual(git("--git-dir", str(bare_dir), "tag", "-l").stdout.strip(), "")
            remote_branch = git("--git-dir", str(bare_dir), "rev-parse", f"refs/heads/{branch}").stdout.strip()
            self.assertEqual(remote_branch, stray)
        finally:
            temp_dir.cleanup()

    def test_a_patch_fast_forwards_the_release_line(self):
        """A minor creates `release/X.Y` at its stamp; a patch stamped from the line head moves it.

        Run in CI mode against a bare origin: the tag and the line go in one
        push each time, the minor's stamp stays tagged and reachable, and the
        patch stamp's single parent is the backport that was the line head.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            self._populate_valid_release_files(repo_dir)
            git("add", ".")
            git("commit", "-m", "feat: populate release files")
            main_commit = git("rev-parse", "HEAD").stdout.strip()
            bare_dir = self._bare_origin_for(git, repo_dir)
            branch = f"release/{MOCK_TARGET_RELEASE_LINE}"

            def remote(ref):
                return git("--git-dir", str(bare_dir), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").stdout.strip()

            minor = self._run_script(
                [MOCK_TARGET_RELEASE_TAG, main_commit],
                env={"CI": "true", **self._FAKE_RELEASE_REPO},
                cwd=repo_dir,
            )
            self.assertEqual(minor.returncode, 0, minor.stderr)
            s0 = git("rev-parse", f"{MOCK_TARGET_RELEASE_TAG}^{{commit}}").stdout.strip()
            self.assertEqual(remote(f"refs/heads/{branch}"), s0)

            # A backport lands on the line (as Tide would push it).
            git("switch", branch)
            (pathlib.Path(repo_dir) / "backport.txt").write_text("fix\n")
            git("add", "backport.txt")
            git("commit", "-m", "fix: backport")
            l1 = git("rev-parse", "HEAD").stdout.strip()
            git("push", "--quiet", "origin", branch)
            git("switch", "main")

            patch = self._run_script(
                [MOCK_LINE_PATCH_RELEASE_TAG, l1],
                env={"CI": "true", **self._FAKE_RELEASE_REPO},
                cwd=repo_dir,
            )
            self.assertEqual(patch.returncode, 0, patch.stderr)
            self.assertIn("fast-forwards", patch.stdout)
            s1 = git("rev-parse", f"{MOCK_LINE_PATCH_RELEASE_TAG}^{{commit}}").stdout.strip()
            self.assertEqual(git("rev-parse", f"{s1}^1").stdout.strip(), l1)
            self.assertEqual(remote(f"refs/heads/{branch}"), s1)
            self.assertEqual(remote(f"refs/tags/{MOCK_TARGET_RELEASE_TAG}"), s0)
            self.assertEqual(git("merge-base", "--is-ancestor", s0, s1).returncode, 0)
            self.assertIn(f"Git tag '{MOCK_LINE_PATCH_RELEASE_TAG}' and release line '{branch}' successfully pushed", patch.stdout)
            install = git("show", f"{MOCK_LINE_PATCH_RELEASE_TAG}:install.sh").stdout
            self.assertIn(f'BAKED_RELEASE_VERSION="{MOCK_LINE_PATCH_RELEASE_TAG}"', install)
            # No per-release branch is created any more.
            self.assertEqual(git("--git-dir", str(bare_dir), "branch", "--list", f"release/{MOCK_TARGET_RELEASE_TAG}").stdout.strip(), "")
        finally:
            temp_dir.cleanup()

    _REJECT_RELEASE_BRANCH_MARKER = "reject-release-branch"

    def _install_release_branch_rejecting_hook(self, git, bare_dir):
        """An `update` hook on the bare remote that refuses `refs/heads/release/*`
        while a marker file exists in the repository, and accepts every other ref.

        `update` rather than `pre-receive`, deliberately: a pre-receive hook is
        all-or-nothing by git's own contract, so it could not tell an atomic push
        from a plain one. An update hook rejects one ref at a time, which is the
        shape a moved line produces; without `--atomic` the tag would land and
        the line be refused, and the test below would see the stranded tag.
        """
        hooks_dir = bare_dir / "hooks"
        hooks_dir.mkdir(exist_ok=True)
        hook = hooks_dir / "update"
        hook.write_text(
            "#!/bin/sh\n"
            "ref=\"$1\"\n"
            "case \"$ref\" in\n"
            "  refs/heads/release/*)\n"
            f"    if [ -f \"{self._REJECT_RELEASE_BRANCH_MARKER}\" ]; then\n"
            "      echo 'release branch rejected by the test hook' >&2\n"
            "      exit 1\n"
            "    fi\n"
            "    ;;\n"
            "esac\n"
            "exit 0\n"
        )
        hook.chmod(0o755)
        # A developer's global core.hooksPath would otherwise bypass this directory.
        git("--git-dir", str(bare_dir), "config", "core.hooksPath", str(hooks_dir))
        return bare_dir / self._REJECT_RELEASE_BRANCH_MARKER

    def test_a_rejected_line_push_takes_the_tag_with_it_and_a_fresh_checkout_re_run_pushes_both(self):
        """The tag and the line go in one atomic push: neither lands without the other.

        The remote's update hook refuses the line ref alone on the first run; a
        plain push would land the tag and leave it stranded, the atomic push
        lands nothing, and nothing has to be taken back. The second run, from a fresh
        checkout with no local release branch, stamps again (the tag never
        reached the remote) and pushes both. A tag the remote already holds
        with the line missing — a hand deletion — is covered by
        test_a_deleted_line_is_recreated_from_the_tag_on_re_run.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            self._populate_valid_release_files(repo_dir)
            git("add", ".")
            git("commit", "-m", "feat: populate release files")
            main_commit = git("rev-parse", "HEAD").stdout.strip()
            bare_dir = self._bare_origin_for(git, repo_dir)
            git("push", "--quiet", "origin", "main")
            branch = f"release/{MOCK_TARGET_RELEASE_LINE}"
            reject_marker = self._install_release_branch_rejecting_hook(git, bare_dir)

            def remote(ref):
                return git("--git-dir", str(bare_dir), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").stdout.strip()

            reject_marker.touch()
            first = self._run_script(
                [MOCK_TARGET_RELEASE_TAG, main_commit],
                env={"CI": "true", **self._FAKE_RELEASE_REPO},
                cwd=repo_dir,
            )
            self.assertNotEqual(first.returncode, 0)
            self.assertIn("Could not push Git tag", first.stderr)
            self.assertIn("rejected by the test hook", first.stderr)
            self.assertEqual(git("--git-dir", str(bare_dir), "tag", "-l", MOCK_TARGET_RELEASE_TAG).stdout.strip(), "")
            self.assertEqual(git("--git-dir", str(bare_dir), "branch", "--list", branch).stdout.strip(), "")

            reject_marker.unlink()
            rerun_dir = pathlib.Path(repo_dir).parent / "rerun"
            git("clone", "--quiet", str(bare_dir), str(rerun_dir))
            git("config", "user.name", "Test User", cwd=rerun_dir)
            git("config", "user.email", "test@example.com", cwd=rerun_dir)
            git("config", "commit.gpgsign", "false", cwd=rerun_dir)
            self._bare_origin_for(git, repo_dir, checkout=rerun_dir)

            rerun = self._run_script(
                [MOCK_TARGET_RELEASE_TAG, main_commit],
                env={"CI": "true", **self._FAKE_RELEASE_REPO},
                cwd=str(rerun_dir),
            )
            self.assertEqual(rerun.returncode, 0, rerun.stderr)
            self.assertIn(f"Git tag '{MOCK_TARGET_RELEASE_TAG}' and release line '{branch}' successfully pushed", rerun.stdout)
            tag_commit = remote(f"refs/tags/{MOCK_TARGET_RELEASE_TAG}")
            self.assertEqual(git("rev-parse", f"{tag_commit}^1", cwd=rerun_dir).stdout.strip(), main_commit)
            self.assertEqual(remote(f"refs/heads/{branch}"), tag_commit)
        finally:
            temp_dir.cleanup()

    def _checkout_holding_an_unpushed_release(self):
        """A checkout that holds the release's tag and line while the remote has neither.

        Staged with the tagger's own dry run (off CI it sets both locally and
        pushes nothing), which is also the shape a run killed between the local
        refs and the push leaves behind. Returns what the same-checkout tests need.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        self._populate_valid_release_files(repo_dir)
        git("add", ".")
        git("commit", "-m", "feat: populate release files")
        main_commit = git("rev-parse", "HEAD").stdout.strip()
        bare_dir = self._bare_origin_for(git, repo_dir)
        git("push", "--quiet", "origin", "main")
        branch = f"release/{MOCK_TARGET_RELEASE_LINE}"

        def remote(ref):
            return git("--git-dir", str(bare_dir), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").stdout.strip()

        dry = self._run_script([MOCK_TARGET_RELEASE_TAG, main_commit], env=self._FAKE_RELEASE_REPO, cwd=repo_dir)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("Dry-run", dry.stdout)
        local_stamp = git("rev-parse", f"refs/tags/{MOCK_TARGET_RELEASE_TAG}^{{commit}}").stdout.strip()
        self.assertEqual(git("rev-parse", f"refs/heads/{branch}").stdout.strip(), local_stamp)
        self.assertEqual(git("--git-dir", str(bare_dir), "tag", "-l", MOCK_TARGET_RELEASE_TAG).stdout.strip(), "")
        self.assertEqual(git("--git-dir", str(bare_dir), "branch", "--list", branch).stdout.strip(), "")
        return repo_dir, git, bare_dir, main_commit, branch, local_stamp, remote

    def test_an_unpushed_release_in_the_checkout_is_pushed_whole_by_a_ci_run_there(self):
        """The local tag is not proof the remote has it.

        Whether the tag is still to be pushed is read from the remote: a CI run
        in a checkout that already holds the tag pushes it with the line, where
        a local read would have pushed the line alone and left the remote in the
        state the atomic push exists to rule out.
        """
        repo_dir, git, bare_dir, main_commit, branch, local_stamp, remote = self._checkout_holding_an_unpushed_release()
        rerun = self._run_script([MOCK_TARGET_RELEASE_TAG, main_commit], env={"CI": "true", **self._FAKE_RELEASE_REPO}, cwd=repo_dir)
        self.assertEqual(rerun.returncode, 0, rerun.stderr)
        self.assertIn("Reusing existing release commit", rerun.stderr)
        self.assertIn(f"Git tag '{MOCK_TARGET_RELEASE_TAG}' exists only in this checkout", rerun.stdout)
        self.assertIn(f"Release line '{branch}' exists only in this checkout", rerun.stderr)
        self.assertNotIn("Idempotent skip", rerun.stdout)
        self.assertIn(f"Git tag '{MOCK_TARGET_RELEASE_TAG}' and release line '{branch}' successfully pushed", rerun.stdout)
        self.assertEqual(remote(f"refs/tags/{MOCK_TARGET_RELEASE_TAG}"), local_stamp)
        self.assertEqual(remote(f"refs/heads/{branch}"), local_stamp)

        third = self._run_script([MOCK_TARGET_RELEASE_TAG, main_commit], env={"CI": "true", **self._FAKE_RELEASE_REPO}, cwd=repo_dir)
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertIn("Nothing to push", third.stdout)

    def test_an_unpushed_release_in_the_checkout_does_not_pin_a_candidate_that_moved(self):
        """The remote lacks the tag, so the local one is recreated at the new stamp
        rather than refused as "already exists but points to" the old one."""
        repo_dir, git, bare_dir, main_commit, branch, local_stamp, remote = self._checkout_holding_an_unpushed_release()
        (pathlib.Path(repo_dir) / "later.txt").write_text("fix")
        git("add", "later.txt")
        git("commit", "-m", "fix: landed after the dry run")
        new_candidate = git("rev-parse", "HEAD").stdout.strip()
        git("push", "--quiet", "origin", "main")

        rerun = self._run_script([MOCK_TARGET_RELEASE_TAG, new_candidate], env={"CI": "true", **self._FAKE_RELEASE_REPO}, cwd=repo_dir)
        self.assertEqual(rerun.returncode, 0, rerun.stderr)
        self.assertIn("exists only in this checkout", rerun.stdout)
        self.assertIn(f"Git tag '{MOCK_TARGET_RELEASE_TAG}' and release line '{branch}' successfully pushed", rerun.stdout)
        tag_commit = remote(f"refs/tags/{MOCK_TARGET_RELEASE_TAG}")
        self.assertNotEqual(tag_commit, local_stamp)
        self.assertEqual(git("rev-parse", f"{tag_commit}^1").stdout.strip(), new_candidate)
        self.assertEqual(remote(f"refs/heads/{branch}"), tag_commit)

    def test_a_tag_the_remote_holds_elsewhere_is_refused_whatever_the_checkout_holds(self):
        """The one shape only the remote read catches.

        The checkout holds the tag at the release commit, so the local read says
        "already exists ... idempotent skip" and would push the line alone; the
        remote holds the tag at another commit, which `git fetch --tags` cannot
        clobber. The remote read refuses, naming the repository, with nothing pushed.
        """
        repo_dir, git, bare_dir, main_commit, branch, local_stamp, remote = self._checkout_holding_an_unpushed_release()
        first = git("rev-list", "--max-parents=0", "HEAD").stdout.strip().splitlines()[0]
        git("--git-dir", str(bare_dir), "tag", MOCK_TARGET_RELEASE_TAG, first)

        proc = self._run_script([MOCK_TARGET_RELEASE_TAG, main_commit], env={"CI": "true", **self._FAKE_RELEASE_REPO}, cwd=repo_dir)
        self.assertNotEqual(proc.returncode, 0)
        repo = f"{self._FAKE_RELEASE_REPO['GH_ORG']}/{self._FAKE_RELEASE_REPO['GH_REPO']}"
        self.assertIn(f"Tag '{MOCK_TARGET_RELEASE_TAG}' already exists on {repo} but points to commit {first}", proc.stderr)
        self.assertNotIn("Idempotent skip", proc.stdout)
        self.assertNotIn("successfully pushed", proc.stdout)
        self.assertEqual(git("--git-dir", str(bare_dir), "branch", "--list", branch).stdout.strip(), "")

    def test_a_rejected_push_leaves_the_checkout_as_it_was_found(self):
        """Nothing local survives a rejected push: not the tag, not the line.

        The tag matters most. The calculator and the publish step read the tag
        list, so a tag only this checkout held would be the next run's GA base
        and its notes-start tag; with it taken back, the calculator in this
        checkout agrees with one in a fresh clone, and the re-run stamps again.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        self._populate_valid_release_files(repo_dir)
        git("add", ".")
        git("commit", "-m", "feat: populate release files")
        main_commit = git("rev-parse", "HEAD").stdout.strip()
        bare_dir = self._bare_origin_for(git, repo_dir)
        git("push", "--quiet", "origin", "main")
        branch = f"release/{MOCK_TARGET_RELEASE_LINE}"
        reject_marker = self._install_release_branch_rejecting_hook(git, bare_dir)
        ci = {"CI": "true", **self._FAKE_RELEASE_REPO}

        reject_marker.touch()
        first = self._run_script([MOCK_TARGET_RELEASE_TAG, main_commit], env=ci, cwd=repo_dir)
        self.assertNotEqual(first.returncode, 0)
        self.assertIn("rejected by the test hook", first.stderr)
        self.assertIn("taken back from this checkout", first.stderr)
        self.assertEqual(git("tag", "-l", MOCK_TARGET_RELEASE_TAG).stdout.strip(), "")
        self.assertEqual(git("branch", "--list", branch).stdout.strip(), "")
        reject_marker.unlink()

        (pathlib.Path(repo_dir) / "later.txt").write_text("fix")
        git("add", "later.txt")
        git("commit", "-m", "fix: landed after the rejected push")
        new_candidate = git("rev-parse", "HEAD").stdout.strip()
        git("push", "--quiet", "origin", "main")
        fresh_dir = pathlib.Path(repo_dir).parent / "fresh"
        git("clone", "--quiet", str(bare_dir), str(fresh_dir))
        self._bare_origin_for(git, repo_dir, checkout=fresh_dir)
        here = self._run_calculator(repo_dir, new_candidate, ci)
        fresh = self._run_calculator(fresh_dir, new_candidate, ci)
        self.assertEqual(here.returncode, 0, here.stderr)
        self.assertEqual(fresh.returncode, 0, fresh.stderr)
        self.assertEqual(here.stdout.strip(), fresh.stdout.strip())

        rerun = self._run_script([MOCK_TARGET_RELEASE_TAG, new_candidate], env=ci, cwd=repo_dir)
        self.assertEqual(rerun.returncode, 0, rerun.stderr)
        self.assertNotIn("Reusing existing release commit", rerun.stderr)
        self.assertIn(f"Git tag '{MOCK_TARGET_RELEASE_TAG}' and release line '{branch}' successfully pushed", rerun.stdout)
        tag_commit = git("--git-dir", str(bare_dir), "rev-parse", "--verify", "--quiet", f"refs/tags/{MOCK_TARGET_RELEASE_TAG}^{{commit}}").stdout.strip()
        self.assertEqual(git("rev-parse", f"{tag_commit}^1").stdout.strip(), new_candidate)

    def test_the_calculator_in_ci_does_not_read_a_tag_only_the_checkout_holds_as_the_base(self):
        """A release that never landed is not the next version's base.

        The tagger's dry run is the fixture because it is what leaves such a tag
        (as would a run killed before its push). In CI the calculator prunes the
        tag list to the remote's before reading the base, so a persistent clone
        answers as a fresh one does, and the publish step that follows in the
        same checkout reads the same list.
        """
        repo_dir, git, bare_dir, main_commit, branch, local_stamp, remote = self._checkout_holding_an_unpushed_release()
        (pathlib.Path(repo_dir) / "later.txt").write_text("fix")
        git("add", "later.txt")
        git("commit", "-m", "fix: landed after the dry run")
        new_candidate = git("rev-parse", "HEAD").stdout.strip()
        git("push", "--quiet", "origin", "main")
        fresh_dir = pathlib.Path(repo_dir).parent / "fresh"
        git("clone", "--quiet", str(bare_dir), str(fresh_dir))
        self._bare_origin_for(git, repo_dir, checkout=fresh_dir)
        ci = {"CI": "true", **self._FAKE_RELEASE_REPO}

        off_ci = self._run_calculator(repo_dir, new_candidate, self._FAKE_RELEASE_REPO)
        self.assertEqual(off_ci.returncode, 0, off_ci.stderr)
        self.assertIn(f"Latest GA Tag: {MOCK_TARGET_RELEASE_TAG}", off_ci.stderr, "off CI the local tag is read, as a dry run should")

        here = self._run_calculator(repo_dir, new_candidate, ci)
        fresh = self._run_calculator(fresh_dir, new_candidate, ci)
        self.assertEqual(here.returncode, 0, here.stderr)
        self.assertEqual(fresh.returncode, 0, fresh.stderr)
        self.assertEqual(here.stdout.strip(), fresh.stdout.strip())
        self.assertEqual(here.stdout.strip(), MOCK_INITIAL_VERSION, "no GA tag has landed, so this is the first version")
        self.assertEqual(git("tag", "-l", MOCK_TARGET_RELEASE_TAG).stdout.strip(), "", "the local-only tag is pruned")

    def _run_calculator(self, repo_dir, candidate, env):
        gh_out = pathlib.Path(repo_dir) / "calc_out.txt"
        gh_out.write_text("")
        return subprocess.run(
            ["bash", str(_CALCULATE_NEXT_VERSION_SH)],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            env=get_isolated_test_env(overrides={"TARGET_COMMIT": candidate, "GITHUB_OUTPUT": str(gh_out), **env}),
        )

    def test_a_deleted_line_is_recreated_from_the_tag_on_re_run(self):
        """With the tag on the remote and the line gone, a re-run reuses the tagged commit and pushes the line alone."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            self._populate_valid_release_files(repo_dir)
            git("add", ".")
            git("commit", "-m", "feat: populate release files")
            main_commit = git("rev-parse", "HEAD").stdout.strip()
            bare_dir = self._bare_origin_for(git, repo_dir)
            branch = f"release/{MOCK_TARGET_RELEASE_LINE}"

            def remote(ref):
                return git("--git-dir", str(bare_dir), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").stdout.strip()

            first = self._run_script([MOCK_TARGET_RELEASE_TAG, main_commit], env={"CI": "true", **self._FAKE_RELEASE_REPO}, cwd=repo_dir)
            self.assertEqual(first.returncode, 0, first.stderr)
            tag_commit = remote(f"refs/tags/{MOCK_TARGET_RELEASE_TAG}")
            git("--git-dir", str(bare_dir), "branch", "-D", branch)

            rerun_dir = pathlib.Path(repo_dir).parent / "rerun"
            git("clone", "--quiet", str(bare_dir), str(rerun_dir))
            git("config", "user.name", "Test User", cwd=rerun_dir)
            git("config", "user.email", "test@example.com", cwd=rerun_dir)
            git("config", "commit.gpgsign", "false", cwd=rerun_dir)
            self._bare_origin_for(git, repo_dir, checkout=rerun_dir)
            rerun = self._run_script([MOCK_TARGET_RELEASE_TAG, main_commit], env={"CI": "true", **self._FAKE_RELEASE_REPO}, cwd=str(rerun_dir))
            self.assertEqual(rerun.returncode, 0, rerun.stderr)
            self.assertIn("Reusing existing release commit", rerun.stderr)
            self.assertIn(f"Git tag '{MOCK_TARGET_RELEASE_TAG}' already exists", rerun.stdout)
            self.assertIn(f"Release line '{branch}' successfully pushed", rerun.stdout)
            self.assertEqual(remote(f"refs/heads/{branch}"), tag_commit)
        finally:
            temp_dir.cleanup()

    def test_a_line_that_moved_past_the_candidate_is_refused_with_nothing_pushed(self):
        """A merge that lands on the line before the release pushes is not overtaken.

        The line is one commit past the candidate. Neither the tag nor the line
        is pushed, and the message names where the line is; the next run resolves
        the new head as the candidate.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            self._populate_valid_release_files(repo_dir)
            git("add", ".")
            git("commit", "-m", "feat: populate release files")
            main_commit = git("rev-parse", "HEAD").stdout.strip()
            bare_dir = self._bare_origin_for(git, repo_dir)
            branch = f"release/{MOCK_TARGET_RELEASE_LINE}"
            minor = self._run_script([MOCK_TARGET_RELEASE_TAG, main_commit], env={"CI": "true", **self._FAKE_RELEASE_REPO}, cwd=repo_dir)
            self.assertEqual(minor.returncode, 0, minor.stderr)

            git("switch", branch)
            (pathlib.Path(repo_dir) / "backport.txt").write_text("fix\n")
            git("add", "backport.txt")
            git("commit", "-m", "fix: backport")
            l1 = git("rev-parse", "HEAD").stdout.strip()
            (pathlib.Path(repo_dir) / "backport2.txt").write_text("fix\n")
            git("add", "backport2.txt")
            git("commit", "-m", "fix: a second backport that landed meanwhile")
            git("push", "--quiet", "origin", branch)
            git("switch", "main")

            proc = self._run_script([MOCK_LINE_PATCH_RELEASE_TAG, l1], env={"CI": "true", **self._FAKE_RELEASE_REPO}, cwd=repo_dir)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn(f"Release line '{branch}' already exists on", proc.stderr)
            self.assertEqual(git("--git-dir", str(bare_dir), "tag", "-l", MOCK_LINE_PATCH_RELEASE_TAG).stdout.strip(), "")
        finally:
            temp_dir.cleanup()

if __name__ == "__main__":
    unittest.main()

