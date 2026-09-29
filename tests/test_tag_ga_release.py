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
    MOCK_EXPLICIT_RELEASE_VERSION_NEXT,
    MOCK_TARGET_RELEASE_TAG,
    populate_mock_release_files,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TAG_GA_RELEASE_SH = _REPO_ROOT / "scripts" / "release" / "tag_ga_release.sh"


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
    # The stamped commit is pushed to `release/<X.Y.Z>` as well as tagged, so a
    # release commit belongs to a branch on the repository rather than to its tag
    # alone. tests/test_release_common.py owns ensure_release_branch's own
    # contract; these pin that the GA tagger calls it, and in which order.

    def test_creates_the_release_branch_at_the_stamped_commit(self):
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            self._populate_valid_release_files(repo_dir)
            git("add", ".")
            git("commit", "-m", "feat: populate release files")
            main_commit = git("rev-parse", "HEAD").stdout.strip()
            branch = f"release/{MOCK_TARGET_RELEASE_TAG}"

            proc = self._run_script([MOCK_TARGET_RELEASE_TAG, main_commit], cwd=repo_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(f"Release Branch:      {branch}", proc.stdout)

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
            branch = f"release/{MOCK_TARGET_RELEASE_TAG}"

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
            # Tag first, then branch: the order the recovery test below depends on.
            self.assertLess(
                proc.stdout.index(f"Git tag '{MOCK_TARGET_RELEASE_TAG}' successfully pushed"),
                proc.stdout.index(f"Release branch '{branch}' successfully pushed"),
            )
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
            branch = f"release/{MOCK_TARGET_RELEASE_TAG}"

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
            self.assertIn(f"Release branch '{branch}' already exists on", proc.stderr)
            self.assertNotIn("CREATING AND PUSHING GA RELEASE GIT TAG", proc.stdout)
            self.assertEqual(git("tag", "-l", MOCK_TARGET_RELEASE_TAG).stdout.strip(), "")
            self.assertEqual(git("--git-dir", str(bare_dir), "tag", "-l").stdout.strip(), "")
            remote_branch = git("--git-dir", str(bare_dir), "rev-parse", f"refs/heads/{branch}").stdout.strip()
            self.assertEqual(remote_branch, stray)
        finally:
            temp_dir.cleanup()

    _REJECT_RELEASE_BRANCH_MARKER = "reject-release-branch"

    def _install_release_branch_rejecting_hook(self, git, bare_dir):
        """A pre-receive hook on the bare remote that refuses `refs/heads/release/*`
        while a marker file exists in the repository, and accepts everything else.

        This is how a push of the release branch is made to fail for real, rather
        than by deleting the branch afterwards: what the remote holds when the
        script exits non-zero is then what a run that died at that push leaves.
        """
        hooks_dir = bare_dir / "hooks"
        hooks_dir.mkdir(exist_ok=True)
        hook = hooks_dir / "pre-receive"
        hook.write_text(
            "#!/bin/sh\n"
            "while read -r old new ref; do\n"
            "  case \"$ref\" in\n"
            "    refs/heads/release/*)\n"
            f"      if [ -f \"{self._REJECT_RELEASE_BRANCH_MARKER}\" ]; then\n"
            "        echo 'release branch rejected by the test hook' >&2\n"
            "        exit 1\n"
            "      fi\n"
            "      ;;\n"
            "  esac\n"
            "done\n"
            "exit 0\n"
        )
        hook.chmod(0o755)
        # A developer's global core.hooksPath would otherwise bypass this directory.
        git("--git-dir", str(bare_dir), "config", "core.hooksPath", str(hooks_dir))
        return bare_dir / self._REJECT_RELEASE_BRANCH_MARKER

    def test_a_run_whose_branch_push_fails_leaves_the_tag_and_finishes_on_re_run_from_a_fresh_checkout(self):
        """The tag is pushed before the branch, and that order is what makes the run repeatable.

        The remote refuses the branch push on the first run. Because the tag went
        first it is on the remote when the run fails, and the second run, from a
        fresh checkout with no local release branch, reuses the tagged commit and
        pushes the same commit to the branch. With the two pushes the other way
        round the first run would push nothing, so the re-run's reuse of the
        tagged commit — asserted below — could not happen: it would stamp afresh.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            self._populate_valid_release_files(repo_dir)
            git("add", ".")
            git("commit", "-m", "feat: populate release files")
            main_commit = git("rev-parse", "HEAD").stdout.strip()
            bare_dir = self._bare_origin_for(git, repo_dir)
            branch = f"release/{MOCK_TARGET_RELEASE_TAG}"
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
            self.assertIn(f"Could not push Release branch '{branch}'", first.stderr)
            self.assertIn("rejected by the test hook", first.stderr)
            tag_commit = git("rev-parse", f"{MOCK_TARGET_RELEASE_TAG}^{{commit}}").stdout.strip()
            # What the failed run left behind: the tag, and no branch.
            self.assertEqual(remote(f"refs/tags/{MOCK_TARGET_RELEASE_TAG}"), tag_commit)
            self.assertEqual(git("--git-dir", str(bare_dir), "branch", "--list", branch).stdout.strip(), "")

            # The re-run is a fresh checkout: no local release branch, only what
            # the clone fetched from the remote, and the remote no longer refuses.
            reject_marker.unlink()
            rerun_dir = pathlib.Path(repo_dir).parent / "rerun"
            git("clone", "--quiet", str(bare_dir), str(rerun_dir))
            git("config", "user.name", "Test User", cwd=rerun_dir)
            git("config", "user.email", "test@example.com", cwd=rerun_dir)
            git("config", "commit.gpgsign", "false", cwd=rerun_dir)
            self._bare_origin_for(git, repo_dir, checkout=rerun_dir)
            self.assertEqual(git("branch", "--list", branch, cwd=rerun_dir).stdout.strip(), "")

            rerun = self._run_script(
                [MOCK_TARGET_RELEASE_TAG, main_commit],
                env={"CI": "true", **self._FAKE_RELEASE_REPO},
                cwd=str(rerun_dir),
            )
            self.assertEqual(rerun.returncode, 0, rerun.stderr)
            self.assertIn("Reusing existing release commit", rerun.stderr)
            self.assertIn(f"Git tag '{MOCK_TARGET_RELEASE_TAG}' already exists", rerun.stdout)
            self.assertIn(f"Release branch '{branch}' successfully pushed", rerun.stdout)
            self.assertEqual(remote(f"refs/heads/{branch}"), tag_commit)
        finally:
            temp_dir.cleanup()

if __name__ == "__main__":
    unittest.main()

