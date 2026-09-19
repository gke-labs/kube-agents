"""Unit tests for install.sh validation and execution routines.

Tests pure numeric SemVer (X.Y.Z) references, 40-character commit SHAs,
piped stdin (curl | bash) execution, local script path resolution, and the
NetworkPolicy enablement sequence install.sh runs against adopted clusters.
"""

import json
import os
import pathlib
import pty
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import unittest

from tests.testing.common import (
    INSTALLER_HELP_BANNER,
    INVALID_IMMUTABLE_REFS,
    MOCK_GOOGLE_CHAT_MODE,
    VALID_IMMUTABLE_REFS,
    create_minimal_tools_bin,
    create_mock_git_repo,
    get_isolated_test_env,
)
from tests.testing.release import (
    MOCK_RELEASE_BUNDLE_VERSION,
    create_mock_release_bundle_marker,
)

# The window terraform/examples/full-install/variables.tf admits for the
# helm_timeout variable, read from the HCL. install.sh validates --helm-timeout
# against its own copy of those bounds and then writes the value into that
# variable, so the two have to agree; borrowing the reader rather than adding a
# second HCL parser keeps one place that knows how the validation block is
# shaped.
from tests.test_hindsight_probes import _gate_bounds_seconds as terraform_gate_bounds_seconds

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INSTALL_SH = _REPO_ROOT / "install.sh"
_INSTALLER_COMMON = _REPO_ROOT / "scripts" / "installer" / "installer_common.sh"

# install.sh sources the shared helpers from the acquired workspace partway
# through main(), so a validator that leans on one is unreachable from a bare
# KUBE_AGENTS_SOURCE_ONLY source. Prepend this to reach it.
_SOURCE_INSTALLER_COMMON = f'source "{_INSTALLER_COMMON}"; '


class InstallScriptValidationTest(unittest.TestCase):
    def setUp(self):
        """Pin the install configuration to an empty file.

        install.sh loads install.env at source time, so a developer who has a
        real one in this checkout would have its values seeded into every
        PARAM_* these tests read -- and the suite would pass or fail depending
        on whose machine it ran on. Tests that are about the loading itself set
        KUBE_AGENTS_INSTALL_ENV themselves; everything else gets nothing.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._empty_install_env = pathlib.Path(tmp.name) / "install.env"
        self._empty_install_env.write_text("")

    def _run_install_func(self, func_call, env=None, cwd=None, bin_dir=None):
        """Source install.sh in test mode and run the given function call.

        `bin_dir` is prepended to PATH, for the calls that shell out.
        """
        setup = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"
{func_call}
"""
        overrides = {"KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env)}
        overrides.update(env or {})
        full_env = get_isolated_test_env(overrides=overrides, bin_dir=bin_dir)
        return subprocess.run(
            ["bash", "-c", setup],
            capture_output=True,
            text=True,
            env=full_env,
            cwd=str(cwd or _REPO_ROOT),
        )

    def test_validate_immutable_ref_accepts_valid_refs(self):
        for ref in VALID_IMMUTABLE_REFS:
            with self.subTest(ref=ref):
                cmd = f'validate_immutable_ref "{ref}"'
                proc = self._run_install_func(cmd)
                self.assertEqual(
                    proc.returncode,
                    0,
                    f"install.sh: expected ref '{ref}' to be valid, stderr: {proc.stderr}",
                )

    def test_validate_immutable_ref_rejects_invalid_refs(self):
        for ref in INVALID_IMMUTABLE_REFS:
            with self.subTest(ref=ref):
                cmd = f'validate_immutable_ref "{ref}"'
                proc = self._run_install_func(cmd)
                self.assertNotEqual(
                    proc.returncode,
                    0,
                    f"install.sh: expected ref '{ref}' to be rejected",
                )

    def test_piped_stdin_executes_main(self):
        """Ensures piped curl | bash invocations execute main and do not exit early."""
        install_script_content = _INSTALL_SH.read_text()
        test_env = get_isolated_test_env(
            overrides={"KUBE_AGENTS_LOCK_FILE": str(self._empty_install_env.parent / "test.lock")}
        )
        proc = subprocess.run(
            ["bash", "-s", "--", "--help"],
            input=install_script_content,
            capture_output=True,
            text=True,
            env=test_env,
            cwd=str(_REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 0, f"Piped execution failed: {proc.stderr}")
        self.assertIn(INSTALLER_HELP_BANNER, proc.stdout)

    def test_acquire_source_repo_resolves_script_directory(self):
        """Verifies acquire_source_repo finds local repo scripts via BASH_SOURCE."""
        cmd = 'out_dir=""; PARAM_ALLOW_UNVERIFIED_SOURCE=true acquire_source_repo out_dir ""; echo "DIR=$out_dir"'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"DIR={_REPO_ROOT}", proc.stdout)

    @staticmethod
    def _git(*args, cwd):
        return subprocess.run(
            ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
        ).stdout.strip()

    def _existing_clone_fixture(self, checked_out_tag, full_clone=False):
        """Build the curl | bash situation: a clone of an earlier release under HOME.

        A bare "upstream" repository holds tags 0.2.0 and 0.3.0, each a
        revision that tracks install.sh (the marker refresh_existing_clone
        requires). HOME/kube-agents is cloned from it while only 0.2.0 exists,
        so a clone at 0.2.0 has never seen 0.3.0, the way a clone from an
        earlier install has never seen the next release; 0.3.0 is then pushed
        to the bare repository.

        By default the clone has the shape the fresh-clone arm of
        acquire_source_repo leaves: blobless, no checkout, one --depth=1 tag
        fetch, detached at the tag. `full_clone=True` is a developer's plain
        `git clone` instead, with complete history and the branch `main`.
        Either way the clone is left detached at `checked_out_tag`. Returns
        (home_dir, clone_dir, upstream_url, {tag: commit}).
        """
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp_dir.cleanup)
        base = pathlib.Path(temp_dir.name)
        work_dir = base / "work"
        bare_dir = base / "upstream.git"
        home_dir = base / "home"
        clone_dir = home_dir / "kube-agents"
        home_dir.mkdir()
        git = self._git

        work_dir.mkdir()
        git("init", "-b", "main", cwd=work_dir)
        git("config", "user.name", "Test", cwd=work_dir)
        git("config", "user.email", "test@example.com", cwd=work_dir)
        git("config", "commit.gpgsign", "false", cwd=work_dir)
        (work_dir / "install.sh").write_text("release 0.2.0\n")
        git("add", "install.sh", cwd=work_dir)
        git("commit", "-m", "release 0.2.0", cwd=work_dir)
        git("tag", "0.2.0", cwd=work_dir)
        git("clone", "--bare", "--quiet", str(work_dir), str(bare_dir), cwd=base)
        upstream_url = bare_dir.as_uri()
        if full_clone:
            git("clone", "--quiet", upstream_url, str(clone_dir), cwd=base)
        else:
            git("clone", "--quiet", "--filter=blob:none", "--no-checkout", upstream_url, str(clone_dir), cwd=base)

        (work_dir / "install.sh").write_text("release 0.3.0\n")
        (work_dir / "CHANGELOG.md").write_text("0.3.0\n")
        git("add", "install.sh", "CHANGELOG.md", cwd=work_dir)
        git("commit", "-m", "release 0.3.0", cwd=work_dir)
        git("tag", "0.3.0", cwd=work_dir)
        git("push", "--quiet", upstream_url, "main", "--tags", cwd=work_dir)
        commits = {tag: git("rev-parse", f"{tag}^{{commit}}", cwd=work_dir) for tag in ("0.2.0", "0.3.0")}

        if full_clone:
            if checked_out_tag == "0.3.0":
                git("fetch", "--quiet", upstream_url, "+refs/tags/0.3.0:refs/tags/0.3.0", cwd=clone_dir)
            git("checkout", "--quiet", "--detach", checked_out_tag, cwd=clone_dir)
        else:
            refspec = f"+refs/tags/{checked_out_tag}:refs/tags/{checked_out_tag}"
            git("fetch", "--quiet", "--depth=1", upstream_url, refspec, cwd=clone_dir)
            git("checkout", "--quiet", "--detach", "FETCH_HEAD", cwd=clone_dir)
            self.assertEqual(git("rev-parse", "--is-shallow-repository", cwd=clone_dir), "true")
        return home_dir, clone_dir, upstream_url, commits

    def _acquire_from_outside(self, home_dir, upstream_url, requested_ref):
        """Run acquire_source_repo with install.sh copied outside any checkout.

        Neither the script's directory nor the working directory holds
        scripts/installer/, so acquire_source_repo takes the clone arm and
        looks under HOME. KUBE_AGENTS_REPO_URL is overridden after sourcing,
        because install.sh assigns it unconditionally.
        """
        outside_dir = home_dir.parent / "outside"
        outside_dir.mkdir(exist_ok=True)
        isolated_install_sh = outside_dir / "install.sh"
        isolated_install_sh.write_text(_INSTALL_SH.read_text())
        setup = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{isolated_install_sh}"
KUBE_AGENTS_REPO_URL="{upstream_url}"
out_dir=""; acquire_source_repo out_dir "{requested_ref}"; echo "RESOLVED=$out_dir"
"""
        return subprocess.run(
            ["bash", "-c", setup],
            capture_output=True,
            text=True,
            env={"HOME": str(home_dir), "PATH": os.environ["PATH"]},
            cwd=str(outside_dir),
        )

    @staticmethod
    def _head_of(clone_dir):
        return subprocess.run(
            ["git", "-C", str(clone_dir), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()

    def test_acquire_source_repo_refuses_to_mutate_dirty_existing_repo(self):
        """A dirty clone already at the ref is left alone and verify_local_source_ref rejects it."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")
        (clone_dir / "install.sh").write_text("dirty changes\n")

        proc = self._acquire_from_outside(home_dir, upstream_url, "0.2.0")

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Using existing repository", proc.stdout)
        self.assertIn("without modifying local changes", proc.stdout)
        self.assertIn("dirty checkout", proc.stdout)
        self.assertIn("--allow-unverified-source", proc.stdout)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])
        self.assertEqual((clone_dir / "install.sh").read_text(), "dirty changes\n")

    def test_acquire_source_repo_uses_clean_existing_repo_already_at_the_ref(self):
        """A clean clone already at the requested ref is used as-is, with no fetch."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.3.0")

        proc = self._acquire_from_outside(home_dir, upstream_url, "0.3.0")

        self.assertEqual(proc.returncode, 0, f"Failed: {proc.stdout}\n{proc.stderr}")
        self.assertIn("Using existing repository", proc.stdout)
        self.assertIn("already at '0.3.0'", proc.stdout)
        self.assertNotIn("fetching", proc.stdout)
        self.assertNotIn("Moved", proc.stdout)
        self.assertIn(f"RESOLVED={clone_dir}", proc.stdout)
        self.assertEqual(self._head_of(clone_dir), commits["0.3.0"])

    def test_acquire_source_repo_moves_clean_existing_repo_to_the_requested_ref(self):
        """A clean clone at an earlier release is fetched and detached at the requested tag."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")

        proc = self._acquire_from_outside(home_dir, upstream_url, "0.3.0")

        self.assertEqual(proc.returncode, 0, f"Failed: {proc.stdout}\n{proc.stderr}")
        self.assertIn("Using existing repository", proc.stdout)
        self.assertIn("fetching '0.3.0'", proc.stdout)
        self.assertIn(f"Moved {clone_dir} from {commits['0.2.0']} to '0.3.0'", proc.stdout)
        self.assertIn(f"Verified install sources and image ref resolve to commit {commits['0.3.0']}", proc.stdout)
        self.assertIn(f"RESOLVED={clone_dir}", proc.stdout)
        self.assertEqual(self._head_of(clone_dir), commits["0.3.0"])
        self.assertEqual((clone_dir / "install.sh").read_text(), "release 0.3.0\n")
        self.assertEqual((clone_dir / "CHANGELOG.md").read_text(), "0.3.0\n")

    def test_acquire_source_repo_moves_clean_existing_repo_to_a_commit_sha(self):
        """A 40-hex ref goes through the object-name arm of fetch_source_ref and moves the clone.

        A complete clone, because a blobless one resolves an unknown commit by
        fetching it lazily through its promisor remote before fetch_source_ref
        is reached.
        """
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0", full_clone=True)

        proc = self._acquire_from_outside(home_dir, upstream_url, commits["0.3.0"])

        self.assertEqual(proc.returncode, 0, f"Failed: {proc.stdout}\n{proc.stderr}")
        self.assertIn(f"fetching '{commits['0.3.0']}'", proc.stdout)
        self.assertIn(f"Moved {clone_dir} from {commits['0.2.0']} to '{commits['0.3.0']}'", proc.stdout)
        self.assertEqual(self._head_of(clone_dir), commits["0.3.0"])

    def test_acquire_source_repo_moves_a_full_clone_on_a_branch_without_making_it_shallow(self):
        """A developer's complete clone on a branch is moved to the detached tag with its history intact."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0", full_clone=True)
        self._git("checkout", "--quiet", "main", cwd=clone_dir)

        proc = self._acquire_from_outside(home_dir, upstream_url, "0.3.0")

        self.assertEqual(proc.returncode, 0, f"Failed: {proc.stdout}\n{proc.stderr}")
        self.assertIn(f"from branch 'main' ({commits['0.2.0']}) to '0.3.0' (detached HEAD)", proc.stdout)
        self.assertIn("untracked files such as install.env are kept", proc.stdout)
        self.assertEqual(self._head_of(clone_dir), commits["0.3.0"])
        self.assertEqual(self._git("rev-parse", "main", cwd=clone_dir), commits["0.2.0"])
        self.assertEqual(self._git("rev-parse", "--is-shallow-repository", cwd=clone_dir), "false")
        self.assertEqual(self._git("rev-list", "--count", "HEAD", cwd=clone_dir), "2")

    def test_acquire_source_repo_checks_out_a_ref_the_clone_already_has_without_fetching(self):
        """A clone that already holds the tag is moved to it without reaching the network."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")
        self._git("fetch", "--quiet", "--depth=1", upstream_url, "+refs/tags/0.3.0:refs/tags/0.3.0", cwd=clone_dir)
        unreachable_url = (home_dir.parent / "no-such-upstream.git").as_uri()

        proc = self._acquire_from_outside(home_dir, unreachable_url, "0.3.0")

        self.assertEqual(proc.returncode, 0, f"Failed: {proc.stdout}\n{proc.stderr}")
        self.assertIn(f"already has '0.3.0' ({commits['0.3.0']}); checking it out", proc.stdout)
        self.assertNotIn("fetching", proc.stdout)
        self.assertIn(f"Moved {clone_dir} from {commits['0.2.0']} to '0.3.0'", proc.stdout)
        self.assertEqual(self._head_of(clone_dir), commits["0.3.0"])

    def test_acquire_source_repo_leaves_a_plain_directory_inside_a_git_managed_home_alone(self):
        """A non-Git HOME/kube-agents inside a HOME that is itself a repository is not the clone."""
        home_dir, clone_dir, upstream_url, _ = self._existing_clone_fixture("0.2.0")
        shutil.rmtree(clone_dir)
        clone_dir.mkdir()
        (clone_dir / "README.md").write_text("unpacked release archive\n")
        self._git("init", "-q", "-b", "main", cwd=home_dir)
        self._git("config", "user.name", "Test", cwd=home_dir)
        self._git("config", "user.email", "test@example.com", cwd=home_dir)
        self._git("config", "commit.gpgsign", "false", cwd=home_dir)
        (home_dir / ".bashrc").write_text("export EDITOR=vi\n")
        self._git("add", ".bashrc", cwd=home_dir)
        self._git("commit", "-q", "-m", "dotfiles", cwd=home_dir)
        home_head = self._head_of(home_dir)

        proc = self._acquire_from_outside(home_dir, upstream_url, "0.3.0")

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(f"Using existing repository at {clone_dir} as-is: it is not the root of a Git worktree", proc.stdout)
        self.assertNotIn("fetching", proc.stdout)
        self.assertNotIn("Moved", proc.stdout)
        self.assertEqual(self._head_of(home_dir), home_head)
        self.assertEqual((home_dir / ".bashrc").read_text(), "export EDITOR=vi\n")
        self.assertEqual((clone_dir / "README.md").read_text(), "unpacked release archive\n")
        self.assertEqual(self._git("status", "--porcelain", "--untracked-files=no", cwd=home_dir), "")

    def test_acquire_source_repo_leaves_an_unrelated_repository_alone(self):
        """A clean repository that only shares the directory name is not fetched into or moved."""
        home_dir, clone_dir, upstream_url, _ = self._existing_clone_fixture("0.2.0")
        shutil.rmtree(clone_dir)
        clone_dir.mkdir()
        self._git("init", "-q", "-b", "main", cwd=clone_dir)
        self._git("config", "user.name", "Test", cwd=clone_dir)
        self._git("config", "user.email", "test@example.com", cwd=clone_dir)
        self._git("config", "commit.gpgsign", "false", cwd=clone_dir)
        (clone_dir / "notes.txt").write_text("my project\n")
        self._git("add", "notes.txt", cwd=clone_dir)
        self._git("commit", "-q", "-m", "notes", cwd=clone_dir)
        own_head = self._head_of(clone_dir)

        proc = self._acquire_from_outside(home_dir, upstream_url, "0.3.0")

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("its HEAD is not a kube-agents revision (no install.sh), so it was not moved", proc.stdout)
        self.assertNotIn("fetching", proc.stdout)
        self.assertNotIn("Moved", proc.stdout)
        self.assertEqual(self._head_of(clone_dir), own_head)
        self.assertEqual((clone_dir / "notes.txt").read_text(), "my project\n")
        self.assertIn("'0.3.0' is not present in the current checkout", proc.stdout)

    def test_acquire_source_repo_falls_through_when_an_untracked_file_blocks_the_checkout(self):
        """An untracked file the new tree tracks makes the checkout fail; the clone and the file stay."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")
        (clone_dir / "CHANGELOG.md").write_text("my own notes\n")

        proc = self._acquire_from_outside(home_dir, upstream_url, "0.3.0")

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("fetching '0.3.0'", proc.stdout)
        self.assertIn(f"Could not check out '0.3.0' in {clone_dir}; the checkout stays at {commits['0.2.0']}", proc.stdout)
        self.assertNotIn("Moved", proc.stdout)
        self.assertIn("Source/image version mismatch", proc.stdout)
        self.assertIn("--allow-unverified-source", proc.stdout)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])
        self.assertEqual((clone_dir / "CHANGELOG.md").read_text(), "my own notes\n")

    def test_acquire_source_repo_leaves_a_dirty_existing_repo_at_an_older_ref_alone(self):
        """A dirty clone at an earlier release is neither fetched nor moved, and the run stops."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")
        (clone_dir / "install.sh").write_text("dirty changes\n")

        proc = self._acquire_from_outside(home_dir, upstream_url, "0.3.0")

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("the checkout is dirty, so '0.3.0' was not fetched", proc.stdout)
        self.assertNotIn("Moved", proc.stdout)
        # verify_local_source_ref checks for the ref before it checks for a clean
        # tree, so the refusal names the missing ref; the opt-out hint is the same.
        self.assertIn("'0.3.0' is not present in the current checkout", proc.stdout)
        self.assertIn("--allow-unverified-source", proc.stdout)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])
        self.assertEqual((clone_dir / "install.sh").read_text(), "dirty changes\n")

    def test_acquire_source_repo_falls_through_when_the_ref_cannot_be_fetched(self):
        """A ref the upstream lacks leaves the clone where it was and the existing error names it."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")

        proc = self._acquire_from_outside(home_dir, upstream_url, "9.9.9")

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("fetching '9.9.9'", proc.stdout)
        self.assertIn(f"Could not fetch '9.9.9' into {clone_dir}; the checkout stays at {commits['0.2.0']}", proc.stdout)
        self.assertIn("'9.9.9' is not present in the current checkout", proc.stdout)
        self.assertIn("--allow-unverified-source", proc.stdout)
        self.assertNotIn("Moved", proc.stdout)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])

    def test_verify_local_source_ref_dry_run_warning_does_not_claim_cluster_mutation(self):
        """Under --dry-run, an unverified mismatched checkout warns about dry-run continuing without claiming cluster mutation."""
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_dir = pathlib.Path(temp_dir) / "repo"
            repo_dir.mkdir()
            subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo_dir), check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir), check=True)
            (repo_dir / "file.txt").write_text("initial\n")
            subprocess.run(["git", "add", "file.txt"], cwd=str(repo_dir), check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_dir), check=True)
            head_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_dir), check=True, capture_output=True, text=True).stdout.strip()
            (repo_dir / "file.txt").write_text("second\n")
            subprocess.run(["git", "commit", "-am", "second"], cwd=str(repo_dir), check=True)
            subprocess.run(["git", "tag", "0.2.0"], cwd=str(repo_dir), check=True)
            subprocess.run(["git", "checkout", head_commit], cwd=str(repo_dir), check=True, capture_output=True)

            cmd = f'PARAM_DRY_RUN=true verify_local_source_ref "{repo_dir}" "0.2.0"'
            proc = self._run_install_func(cmd)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Continuing dry run with unverified install sources", proc.stdout)
            self.assertIn("preview is continuing", proc.stdout)
            self.assertNotIn("the cluster will get", proc.stdout)

            cmd_dry_allow = f'PARAM_DRY_RUN=true PARAM_ALLOW_UNVERIFIED_SOURCE=true verify_local_source_ref "{repo_dir}" "0.2.0"'
            proc_dry_allow = self._run_install_func(cmd_dry_allow)
            self.assertEqual(proc_dry_allow.returncode, 0, proc_dry_allow.stderr)
            self.assertIn("Continuing dry run with unverified install sources", proc_dry_allow.stdout)
            self.assertIn("--allow-unverified-source active", proc_dry_allow.stdout)

            cmd_real = f'PARAM_DRY_RUN=false PARAM_ALLOW_UNVERIFIED_SOURCE=true verify_local_source_ref "{repo_dir}" "0.2.0"'
            proc_real = self._run_install_func(cmd_real)
            self.assertEqual(proc_real.returncode, 0, proc_real.stderr)
            self.assertIn("Continuing with unverified install sources", proc_real.stdout)
            self.assertIn("the cluster will get this checkout's configuration", proc_real.stdout)

    def test_parse_args_google_chat_mode(self):
        """Verifies parse_args captures --google-chat-mode."""
        cmd = f'parse_args --google-chat-mode={MOCK_GOOGLE_CHAT_MODE}; echo "MODE=$PARAM_GOOGLE_CHAT_MODE"'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"MODE={MOCK_GOOGLE_CHAT_MODE}", proc.stdout)

    def test_parse_args_generate_only(self):
        """Verifies parse_args captures --generate-only."""
        cmd = 'parse_args --generate-only; echo "GEN=$PARAM_GENERATE_ONLY"'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("GEN=true", proc.stdout)

    def test_main_generate_only_and_dry_run_cannot_be_combined(self):
        """Verifies that combining --dry-run and --generate-only fails."""
        cmd = 'main --dry-run --generate-only || rc=$?; echo "RC=$rc"'
        proc = self._run_install_func(cmd)
        self.assertIn("RC=2", proc.stdout)
        self.assertIn("--dry-run and --generate-only are different modes and cannot be combined", proc.stdout)

    # ── require_min_go_version: the toolchain that builds the Minty CLI ──────

    def _run_with_go(
        self,
        go_stdout,
        func_call='rc=0; require_min_go_version || rc=$?; echo "rc=$rc"',
    ):
        """Run `func_call` with a stub `go` that prints `go_stdout` for any call.

        A stub rather than the host's Go: the check's whole job is to judge a
        version, so a suite that asked the developer's toolchain would pass or
        fail by whose machine it ran on.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bin_dir = pathlib.Path(tmp.name) / "bin"
        bin_dir.mkdir()
        go = bin_dir / "go"
        go.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' '{go_stdout}'\nexit 0\n")
        go.chmod(0o755)
        return self._run_install_func(func_call, bin_dir=str(bin_dir))

    def test_a_go_too_old_for_the_minty_cli_is_refused(self):
        """Debian 12's golang-go, which auto_install_tool happily installs.

        `command -v go` answers for it, so the installer used to call it a
        success and spend six `retry` attempts on a build that cannot satisfy
        the CLI's go.mod.
        """
        for version in ("go1.18.1", "go1.19.8", "go1.20.14"):
            with self.subTest(version=version):
                proc = self._run_with_go(f"go version {version} linux/amd64")
                self.assertIn("rc=1", proc.stdout, proc.stdout + proc.stderr)
                self.assertIn("too old", proc.stdout)

    def test_a_go_that_can_fetch_the_toolchain_is_accepted(self):
        """1.21 is the floor because 1.21 is where toolchain downloads arrived.

        Pinning the CLI's own 1.24 here would refuse 1.22 and 1.23 hosts that
        fetch 1.24 themselves and build perfectly well.
        """
        for version in ("go1.21.0", "go1.22.11", "go1.26.0"):
            with self.subTest(version=version):
                proc = self._run_with_go(f"go version {version} linux/amd64")
                self.assertIn("rc=0", proc.stdout, proc.stdout + proc.stderr)
                self.assertNotIn("too old", proc.stdout)

    def test_an_unreadable_go_version_warns_instead_of_refusing(self):
        # The same call the other two checks in min_versions.sh make: a regex
        # that missed must not be the reason an import is refused, because the
        # build reports an unusable toolchain anyway.
        proc = self._run_with_go("go: unknown command")
        self.assertIn("rc=0", proc.stdout, proc.stdout + proc.stderr)
        self.assertIn("Could not determine the Go version", proc.stdout)

    def test_the_go_version_is_read_out_of_the_release_string(self):
        proc = self._run_with_go(
            "go version go1.26.0 linux/amd64", func_call="go_core_version"
        )
        self.assertEqual(proc.stdout.strip().splitlines()[-1], "1.26.0", proc.stderr)

    def test_every_version_floor_resolves_when_installed_by_curl_pipe_bash(self):
        """install.sh alone, with no repository beside it — the documented one-liner.

        min_versions.sh cannot be sourced there, so the else arm hand-stubs the
        floors. The call sites are unguarded, which makes a floor the arm
        forgets not a skipped check but `command not found`; the caller's
        `|| return 1` then reports it as the operation failing, on a host where
        nothing was wrong. require_min_go_version shipped in exactly that state.

        The expected set is read out of install.sh rather than listed here, so
        a floor added later cannot satisfy this test by being absent from both
        the else arm and the assertion.
        """
        names = sorted(set(re.findall(r"\brequire_min_\w+", _INSTALL_SH.read_text())))
        self.assertGreaterEqual(len(names), 3, f"expected the known floors, found {names}")

        with tempfile.TemporaryDirectory() as tmp:
            outside = pathlib.Path(tmp) / "outside"
            outside.mkdir()
            # The copy is the point: no scripts/installer/ next to it, exactly
            # as when the script arrives over the wire.
            (outside / "install.sh").write_text(_INSTALL_SH.read_text())
            probe = "; ".join(f'echo "{n}=$(type -t {n})"' for n in names)
            proc = subprocess.run(
                ["bash", "-c", f'KUBE_AGENTS_SOURCE_ONLY=true source ./install.sh >/dev/null 2>&1; {probe}'],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=120,
                env=get_isolated_test_env(),
                cwd=str(outside),
            )

        for name in names:
            self.assertIn(
                f"{name}=function",
                proc.stdout,
                f"{name} is undefined when install.sh runs outside a checkout; "
                f"add a stub to the else arm beside the source of min_versions.sh.\n"
                f"{proc.stdout}\n{proc.stderr}",
            )

    def test_the_go_floor_is_enforced_after_the_workspace_step_on_the_curl_pipe_bash_path(self):
        """Resolving to *something* is not the same as checking anything.

        The test above pins that no `require_min_*` is left undefined when
        install.sh arrives over the wire. That is the crash guard, and it was
        satisfied by a stub that returns 0 for every Go ever released -- so
        the floor that exists to catch the 1.19 `auto_install_tool` leaves on
        Debian 12 passed a 1.19 host, on the one path where auto_install_tool
        is the likely way Go got there at all.

        The stub is only meant to hold until the clone lands. This asserts the
        second half: that the workspace step replaces it, and the check the
        Minty CLI import makes at step 12 is the real one.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        outside = pathlib.Path(tmp.name) / "outside"
        outside.mkdir()
        # No scripts/installer/ beside it: the `curl … | bash` shape.
        (outside / "install.sh").write_text(_INSTALL_SH.read_text())

        bin_dir = pathlib.Path(tmp.name) / "bin"
        bin_dir.mkdir()
        go = bin_dir / "go"
        # Debian 12's golang-go, the exact toolchain the floor was written for.
        go.write_text("#!/usr/bin/env bash\nprintf '%s\\n' 'go version go1.19.8 linux/amd64'\nexit 0\n")
        go.chmod(0o755)

        script = (
            "KUBE_AGENTS_SOURCE_ONLY=true source ./install.sh >/dev/null 2>&1\n"
            'before=0; require_min_go_version >/dev/null 2>&1 || before=$?; echo "before=$before"\n'
            # The step-2 call, with this checkout standing in for the clone.
            f'source_provisioning_helpers "{_REPO_ROOT}" >/dev/null\n'
            'after=0; require_min_go_version || after=$?; echo "after=$after"\n'
        )
        proc = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=120,
            env=get_isolated_test_env(
                overrides={"KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env)},
                bin_dir=str(bin_dir),
            ),
            cwd=str(outside),
        )

        details = f"\n{proc.stdout}\n{proc.stderr}"
        # Not an assertion about desired behaviour -- it records that the stub
        # is what answers before the clone, which is why the second half has
        # to be pinned at all.
        self.assertIn("before=0", proc.stdout, details)
        self.assertIn(
            "after=1",
            proc.stdout,
            "the Go floor is still the no-op stub after the workspace step; "
            "source_provisioning_helpers must source the clone's min_versions.sh"
            + details,
        )
        self.assertIn("too old to build the Minty CLI", proc.stdout, details)

    def test_a_missing_go_neither_refuses_a_dry_run_nor_installs_during_generate_only(self):
        """Step 8 must not reach auto_install_tool for Go.

        The interview runs inside step 8, which main crosses before both the
        --dry-run exit and the --generate-only exit. Neither mode imports a
        key, so neither has any business refusing over a toolchain or putting
        a package on the operator's machine -- yet auto_install_tool does
        exactly one of those in each.

        Two halves, because the defect had two: that the call is harmful where
        it stood, and that it is no longer there.
        """
        # Harmful: the refusal is real, and it ends the whole run.
        proc = self._run_install_func('PARAM_DRY_RUN=true auto_install_tool "go"')
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("Dry-run validation will not install missing tools", proc.stdout)

        # Gone: the surviving call sits where the toolchain is about to be
        # used, which main reaches only past both exits.
        body = _INSTALL_SH.read_text().splitlines()
        start = next(i for i, line in enumerate(body) if line.startswith("import_github_pem() {"))
        end = next(i for i in range(start + 1, len(body)) if body[i] == "}")
        sites = [i for i, line in enumerate(body) if 'auto_install_tool "go"' in line]
        self.assertTrue(sites, "import_github_pem must still be able to install Go")
        for i in sites:
            self.assertTrue(
                start < i < end,
                f"install.sh:{i + 1} installs Go outside import_github_pem "
                f"(lines {start + 1}-{end + 1}); step 8 runs before --dry-run "
                f"and --generate-only exit, so a call there refuses the first "
                f"mode and mutates the host in the second.",
            )

    def test_parse_args_cluster_mode(self):
        """Verifies parse_args captures --cluster-mode."""
        cmd = 'parse_args --cluster-mode=autopilot; echo "MODE=$PARAM_CLUSTER_MODE"'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("MODE=autopilot", proc.stdout)

    def test_cluster_mode_defaults_to_unset(self):
        """An unpassed --cluster-mode leaves the interview free to ask."""
        proc = self._run_install_func('echo "MODE=[$PARAM_CLUSTER_MODE]"')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("MODE=[]", proc.stdout)

    def test_require_creatable_cluster_mode_accepts_both_shapes(self):
        for mode in ("autopilot", "standard"):
            with self.subTest(mode=mode):
                proc = self._run_install_func(
                    f'{_SOURCE_INSTALLER_COMMON}require_creatable_cluster_mode "{mode}" us-central1'
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_require_creatable_cluster_mode_rejects_an_unknown_shape(self):
        proc = self._run_install_func(
            f'{_SOURCE_INSTALLER_COMMON}require_creatable_cluster_mode autopiloot us-central1'
        )
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        # install.sh's print_error writes to stdout.
        self.assertIn("autopiloot", proc.stdout)

    def test_require_creatable_cluster_mode_rejects_a_zone_for_autopilot(self):
        """Autopilot clusters are regional; the module rejects a zone at plan
        time, which is after the whole interview has been paid for."""
        proc = self._run_install_func(
            f'{_SOURCE_INSTALLER_COMMON}require_creatable_cluster_mode autopilot us-central1-a'
        )
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("us-central1-a", proc.stdout)
        # Standard clusters are zonal-capable, so the same location is fine.
        proc = self._run_install_func(
            f'{_SOURCE_INSTALLER_COMMON}require_creatable_cluster_mode standard us-central1-a'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_resolve_creatable_cluster_mode_defaults_to_autopilot(self):
        """The line that decides what a bare ./install.sh builds.

        install.sh exports CLUSTER_MODE before the tfvars generator
        reads it, so installer_common.sh's own `:-$DEFAULT_CLUSTER_MODE` never
        decides anything for this front door. This is the assertion that goes
        red if the installer default is put back to standard.
        """
        proc = self._run_install_func(
            f'{_SOURCE_INSTALLER_COMMON}resolve_creatable_cluster_mode "" us-central1'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "autopilot")

    def test_resolve_creatable_cluster_mode_honours_an_explicit_request(self):
        for mode in ("standard", "autopilot"):
            with self.subTest(mode=mode):
                proc = self._run_install_func(
                    f'{_SOURCE_INSTALLER_COMMON}resolve_creatable_cluster_mode {mode} us-central1'
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.strip(), mode)

    def test_resolve_creatable_cluster_mode_steps_aside_for_a_zone(self):
        """A defaulted Autopilot demotes rather than writing a config Terraform
        rejects. Reachable non-interactively via --cluster-name, where nothing
        else checks the mode/location pair."""
        proc = self._run_install_func(
            f'{_SOURCE_INSTALLER_COMMON}resolve_creatable_cluster_mode "" us-central1-a'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "standard")

    def test_resolve_creatable_cluster_mode_does_not_rescue_an_explicit_autopilot(self):
        """An impossible request stays impossible: the demotion is for a shape
        nobody chose, not a way to silently build something else."""
        proc = self._run_install_func(
            f'{_SOURCE_INSTALLER_COMMON}resolve_creatable_cluster_mode autopilot us-central1-a'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "autopilot")

    def test_main_resolves_the_creatable_shape_through_the_resolver(self):
        """Pins the call site, not just the function.

        resolve_creatable_cluster_mode is covered directly above, but nothing
        made main() consult it: reverting the deciding line to the inline
        `cluster_mode="${cluster_mode:-standard}"` it replaced left every
        installer test green, so the headline behaviour of this change was
        unpinned. main() is the whole interview and is not drivable from a
        unit test, so this asserts on the source directly.
        """
        source = _INSTALL_SH.read_text()
        self.assertIn(
            'cluster_mode="$(resolve_creatable_cluster_mode "$cluster_mode" "$region")"',
            source,
            "install.sh's interview must resolve the creatable shape through "
            "resolve_creatable_cluster_mode: an inline default is untested and "
            "skips the zonal demotion entirely.",
        )
        self.assertNotRegex(
            source,
            r'cluster_mode="\$\{cluster_mode:-\w+\}"',
            "an inline `:-` default for cluster_mode is the exact shape this "
            "test exists to keep out.",
        )

    def test_cluster_shape_menu_is_ordered_by_the_resolver(self):
        """prompt_menu's enter default is option 1, so a hardcoded
        Autopilot-first order turns pressing enter into an *explicit*
        autopilot request -- which the resolver is then right to refuse to
        demote, aborting a zonal interactive install that used to build
        Standard. Deriving the order keeps the label, the enter key and the
        resolver in agreement at both kinds of location.
        """
        source = _INSTALL_SH.read_text()
        self.assertIn(
            'menu_default="$(resolve_creatable_cluster_mode "" "$region")"',
            source,
            "the cluster-shape menu must take its order from the resolver.",
        )
        # Whichever branch runs, the option carrying "(Default)" is option 1
        # and is the shape its own case arm assigns.
        self.assertRegex(
            source,
            r'"\$\{autopilot_option\} \(Default\)"[\s\S]{0,400}?1\) cluster_mode="autopilot"',
        )
        self.assertRegex(
            source,
            r'"\$\{standard_option\} \(Default\)"[\s\S]{0,400}?1\) cluster_mode="standard"',
        )

    def test_location_is_region_distinguishes_regions_from_zones(self):
        for location, expected in (
            ("us-central1", 0),
            ("europe-west4", 0),
            ("us-central1-a", 1),
            ("europe-west4-b", 1),
        ):
            with self.subTest(location=location):
                proc = self._run_install_func(
                    f'{_SOURCE_INSTALLER_COMMON}location_is_region {location}'
                )
                self.assertEqual(proc.returncode, expected, proc.stdout)

    def test_the_probed_cluster_shape_is_never_written_back(self):
        """There is no persist_effective_cluster_mode, and there must not be.

        It existed so that a later run would not rebuild a deleted cluster in
        the wrong shape, by recording the probe's answer over the interview's.
        That is unnecessary -- write_tfvars_from_state re-probes every run and
        every branch with a live cluster takes the mode from the probe, so a
        stale configured value can never reach a running cluster's tfvars --
        and it was the one place the installer wrote its own findings back into
        the file it reads as configuration. A file that is an input and an
        output at once is the property this refactor removes, so a
        reintroduction is a regression even though it would look like a fix.
        """
        source = _INSTALL_SH.read_text()
        # The name still appears, in the comment explaining why it is gone.
        # What must not come back is a definition or a call.
        # re.MULTILINE, or `^` anchors at offset 0 only and neither guard can
        # ever fail however the function comes back.
        self.assertNotRegex(
            source,
            re.compile(r"^\s*persist_effective_cluster_mode\s*\(\)", re.MULTILINE),
            "persist_effective_cluster_mode must not be redefined",
        )
        self.assertNotRegex(
            source,
            re.compile(r"^\s*persist_effective_cluster_mode\s+", re.MULTILINE),
            "persist_effective_cluster_mode must not be called",
        )
        self.assertNotIn(
            "save_var CLUSTER_MODE",
            source,
            "the probed shape must not be written back into the install "
            "configuration; the probe is authoritative on every run",
        )

    def test_the_installer_no_longer_writes_the_state_file(self):
        """vars.sh is read as a legacy input and never generated.

        Regenerating it would put the old two-file model back: a derived file
        that other tools read, drifting from the input that actually decides
        the install.
        """
        source = _INSTALL_SH.read_text()
        self.assertNotIn(
            "write_state_var",
            source,
            "install.sh must not write vars.sh; install.env is the input and "
            "terraform.tfvars the only derived artifact",
        )
        self.assertIn(
            "load_legacy_vars_file",
            source,
            "an existing install's vars.sh must still be read, so upgrading "
            "needs no action from its owner",
        )

    def test_parse_args_enable_google_chat(self):
        """Verifies parse_args captures --enable-google-chat."""
        cmd = 'parse_args --enable-google-chat; echo "CHAT=$PARAM_ENABLE_GOOGLE_CHAT"'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("CHAT=true", proc.stdout)

    def test_parse_args_plugin_flags(self):
        """Verifies parse_args captures plugin enablement flags."""
        cmd = (
            'parse_args --enable-pubsub-platform --enable-stockout-investigator; '
            'echo "PUBSUB=$PARAM_ENABLE_PUBSUB_PLATFORM STOCKOUT=$PARAM_ENABLE_STOCKOUT_INVESTIGATOR"'
        )
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("PUBSUB=true STOCKOUT=true", proc.stdout)

    def test_parse_args_vertex_manage_serving_project_flag_is_read(self):
        """--vertex-manage-serving-project=false reaches its PARAM_ unchanged;
        normalisation to true/false happens where it is consumed."""
        cmd = (
            "parse_args --vertex-manage-serving-project=false; "
            'echo "MANAGE=$PARAM_VERTEX_MANAGE_SERVING_PROJECT"'
        )
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("MANAGE=false", proc.stdout)

    def test_parse_args_vertex_location_overrides_the_default(self):
        """An explicit --vertex-location still wins over DEFAULT_VERTEX_LOCATION."""
        cmd = (
            "parse_args --vertex-location=us-east4; "
            'echo "LOC=$PARAM_VERTEX_LOCATION"'
        )
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("LOC=us-east4", proc.stdout)

    def test_parse_args_github_minter_flags(self):
        cmd = (
            'parse_args --github-app-id=123456 --github-pem-path=/tmp/app.pem '
            '--kms-keyring=custom-keyring --kms-key=custom-key; '
            'echo "APP_ID=$PARAM_GITHUB_APP_ID PEM=$PARAM_GITHUB_PEM_PATH '
            'KEYRING=$PARAM_KMS_KEYRING KEY=$PARAM_KMS_KEY"'
        )
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(
            "APP_ID=123456 PEM=/tmp/app.pem KEYRING=custom-keyring KEY=custom-key",
            proc.stdout,
        )

    def test_missing_github_pem_path_is_deferred_not_rejected_early(self):
        """A .pem deleted after a successful import must not abort the early preflight.

        The installer's own docs tell the operator to delete the file once it is
        in Cloud KMS, and install.env may still name it. Rejecting that path up
        front makes every later run unusable, so the decision belongs with the
        KMS lookup that can see the imported key.

        The run is expected to fail -- --dry-run makes the prerequisites step
        refuse to install the gcloud a sterile PATH cannot provide -- but it
        must fail *there*, past the PEM preflight, rather than on the missing
        file. stdin is closed so a regression that reintroduces a prompt fails
        the test instead of hanging it.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            bin_dir = create_minimal_tools_bin(tmp_dir)
            test_env = get_isolated_test_env(
                overrides={
                    "KUBE_AGENTS_LOCK_FILE": str(self._empty_install_env.parent / "test.lock"),
                    "PATH": str(bin_dir),
                },
            )
            proc = subprocess.run(
                [
                    "bash",
                    str(_INSTALL_SH),
                    "--image-tag=0.1.0",
                    "--dry-run",
                    "--github-pem-path=/tmp/nonexistent-pem-file-12345.pem",
                ],
                capture_output=True,
                text=True,
                env=test_env,
                cwd=str(_REPO_ROOT),
                stdin=subprocess.DEVNULL,
                timeout=120,
            )
            combined = proc.stdout + proc.stderr
            self.assertNotIn(
                "GitHub App private key PEM file does not exist",
                combined,
                f"the early preflight must not decide a missing .pem:\n{combined}",
            )
            self.assertIn(
                "Checking Prerequisites",
                combined,
                f"the run should have reached the prerequisites step:\n{combined}",
            )

    def test_the_missing_pem_decision_runs_after_the_kms_helpers_are_sourced(self):
        """The KMS-aware half of the PEM check must be *called* below source_provisioning_helpers.

        derive_kms_location and kms_key_enabled_version live in
        installer_common.sh, and DEFAULT_KMS_KEYRING in install.defaults.env,
        which a `curl | bash` run has no copy of. Calling either above the
        sourcing aborts the installer with `command not found` (127) or an
        unbound variable long before it can print anything useful, and it does
        so only on the runs that carry a PEM path -- which is why a green suite
        is not evidence here and this ordering is pinned instead.

        The marker is the call, not the body: resolve_missing_pem_against_kms
        is *defined* above main() like every other function in this file, so
        matching on its internals would compare a definition against a sourcing
        line and fail for the wrong reason. What the function does once called
        is covered by MissingPemAgainstKmsTest.
        """
        body = _INSTALL_SH.read_text()

        sourced_at = body.index('source_provisioning_helpers "$repo_dir"')
        called_at = body.index('resolve_missing_pem_against_kms "$region" "$project_id"')
        self.assertLess(
            sourced_at,
            called_at,
            "resolve_missing_pem_against_kms must be called after installer_common.sh is sourced",
        )

    def test_skipping_the_gitops_interview_keeps_the_minter_configuration(self):
        """"Skip for now" must not empty the four names that gate the minter.

        write_tfvars_from_state enables the minter only when GITOPS_ORG,
        GITOPS_REPO and GITHUB_APP_ID are all non-empty, so clearing them on
        the skip arm renders enable_github_minter = false and the apply removes
        a deployed minter -- its GSA, its Workload Identity binding, and the
        chart's Deployment, Service, NetworkPolicy and KSA -- on a re-run where
        the operator only meant to decline the questions.
        """
        body = _INSTALL_SH.read_text()

        marker = "GitOps repository connection skipped."
        # The arm's own `else` is found by its indentation, not by proximity.
        # The arm ends in an inner `if … else print_info "GitOps repository
        # connection skipped."`, so the nearest preceding `else` is that inner
        # one and anchoring on it narrows the slice to twenty-five characters
        # -- `else\n        print_info "` -- which no assignment could ever
        # appear in. The outer arm is the one indented by four spaces.
        arm_start = body.rindex("\n    else\n", 0, body.index(marker))
        arm = body[arm_start : body.index(marker)]

        # The anchor has to prove itself before the assertions below are worth
        # anything: an assertNotIn over the wrong slice passes in silence, and
        # that is exactly how this test used to miss the teardown it forbids.
        # Both landmarks sit inside the arm, one at each end of it.
        self.assertIn(
            "Deliberately not clearing",
            arm,
            f"the anchor no longer spans the skip arm (slice is {len(arm)} chars)",
        )
        self.assertIn(
            "GitOps interview skipped; keeping",
            arm,
            f"the anchor no longer reaches the end of the skip arm (slice is {len(arm)} chars)",
        )

        for cleared in (
            'github_org=""',
            'github_repo=""',
            'github_app_id=""',
            'github_pem_path=""',
        ):
            self.assertNotIn(
                cleared,
                arm,
                f"the skip arm must not clear {cleared!r}: it disables and removes a deployed minter",
            )

    def test_preflight_rejects_directory_github_pem_path(self):
        """Preflight must fail fast when --github-pem-path is a directory instead of a file."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_env = get_isolated_test_env(
                overrides={"KUBE_AGENTS_LOCK_FILE": str(self._empty_install_env.parent / "test.lock")}
            )
            proc = subprocess.run(
                ["bash", str(_INSTALL_SH), "--image-tag=0.1.0", f"--github-pem-path={tmp_dir}"],
                capture_output=True,
                text=True,
                env=test_env,
                cwd=str(_REPO_ROOT),
            )
            self.assertEqual(proc.returncode, 1, f"Expected exit code 1, got {proc.returncode}:\n{proc.stdout}\n{proc.stderr}")
            combined = proc.stdout + proc.stderr
            self.assertIn("not a regular file", combined)

    def test_validate_non_interactive_minter_config(self):
        """validate_non_interactive_minter_config enforces intent-driven validation."""
        # 1. No App ID and no PEM path -> succeeds (optional feature omitted)
        cmd1 = f'{_SOURCE_INSTALLER_COMMON}validate_non_interactive_minter_config "" "" "ring" "key" "us-central1" "p1" ""'
        proc1 = self._run_install_func(cmd1)
        self.assertEqual(proc1.returncode, 0, proc1.stderr)

        # 2. PEM path provided, but App ID is missing -> fails fast
        cmd2 = f'{_SOURCE_INSTALLER_COMMON}validate_non_interactive_minter_config "" "/tmp/key.pem" "ring" "key" "us-central1" "p1" "my-org"'
        proc2 = self._run_install_func(cmd2)
        self.assertEqual(proc2.returncode, 1, proc2.stderr)
        self.assertIn("--github-pem-path was provided, but --github-app-id is missing", proc2.stdout + proc2.stderr)

        # 3. App ID provided, but GitOps organization is missing -> fails fast
        cmd3 = f'{_SOURCE_INSTALLER_COMMON}validate_non_interactive_minter_config "12345" "" "ring" "key" "us-central1" "p1" ""'
        proc3 = self._run_install_func(cmd3)
        self.assertEqual(proc3.returncode, 1, proc3.stderr)
        self.assertIn("provided in non-interactive mode, but --gitops-org is missing", proc3.stdout + proc3.stderr)

        # 4. App ID provided with org, but no KMS key and no PEM path -> fails fast
        cmd4 = (
            f'{_SOURCE_INSTALLER_COMMON}'
            'kms_key_enabled_version() { echo ""; }; '
            'validate_non_interactive_minter_config "12345" "" "ring" "key" "us-central1" "p1" "my-org"'
        )
        proc4 = self._run_install_func(cmd4)
        self.assertEqual(proc4.returncode, 1, proc4.stderr)
        self.assertIn("provided in non-interactive mode, but no ENABLED KMS key exists", proc4.stdout + proc4.stderr)

        # 5. App ID provided with org and existing KMS key version (AOT path) -> succeeds
        cmd5 = (
            f'{_SOURCE_INSTALLER_COMMON}'
            'kms_key_enabled_version() { echo "1"; }; '
            'validate_non_interactive_minter_config "12345" "" "ring" "key" "us-central1" "p1" "my-org"'
        )
        proc5 = self._run_install_func(cmd5)
        self.assertEqual(proc5.returncode, 0, proc5.stderr)

        # 6. App ID provided with org and valid PEM file path (automated path) -> succeeds
        with tempfile.NamedTemporaryFile() as tf:
            cmd6 = (
                f'{_SOURCE_INSTALLER_COMMON}'
                'kms_key_enabled_version() { echo ""; }; '
                f'validate_non_interactive_minter_config "12345" "{tf.name}" "ring" "key" "us-central1" "p1" "my-org"'
            )
            proc6 = self._run_install_func(cmd6)
            self.assertEqual(proc6.returncode, 0, proc6.stderr)

        # 7. App ID provided with existing KMS key version, even if PEM path is non-existent -> succeeds (AOT takes precedence)
        cmd7 = (
            f'{_SOURCE_INSTALLER_COMMON}'
            'kms_key_enabled_version() { echo "1"; }; '
            'validate_non_interactive_minter_config "12345" "/path/to/deleted.pem" "ring" "key" "us-central1" "p1" "my-org"'
        )
        proc7 = self._run_install_func(cmd7)
        self.assertEqual(proc7.returncode, 0, proc7.stderr)

    def test_parse_args_migrate_node_pools(self):
        cmd = 'parse_args --migrate-node-pools; echo "MIGRATE=$PARAM_MIGRATE_NODE_POOLS"'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("MIGRATE=true", proc.stdout)

        cmd2 = 'parse_args --migrate-node-pools=false; echo "MIGRATE=$PARAM_MIGRATE_NODE_POOLS"'
        proc2 = self._run_install_func(cmd2)
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertIn("MIGRATE=false", proc2.stdout)

    def test_parse_args_enable_network_policy(self):
        cmd = 'parse_args --enable-network-policy; echo "NP=$PARAM_ENABLE_NETWORK_POLICY"'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("NP=true", proc.stdout)

        cmd2 = 'parse_args --enable-network-policy=false; echo "NP=$PARAM_ENABLE_NETWORK_POLICY"'
        proc2 = self._run_install_func(cmd2)
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertIn("NP=false", proc2.stdout)

    def test_parse_args_accept_no_network_policy(self):
        cmd = 'parse_args --accept-no-network-policy; echo "A=$PARAM_ACCEPT_NO_NETWORK_POLICY"'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("A=true", proc.stdout)

        cmd2 = 'parse_args --accept-no-network-policy=false; echo "A=$PARAM_ACCEPT_NO_NETWORK_POLICY"'
        proc2 = self._run_install_func(cmd2)
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertIn("A=false", proc2.stdout)

    def test_validate_existing_cluster_opt_in_flags_rejects_both_network_policy_answers(self):
        # Enable Calico and record that it was not enabled: two answers to one
        # question, refused before the cluster is read.
        cmd = 'parse_args --enable-network-policy --accept-no-network-policy; validate_existing_cluster_opt_in_flags'
        proc = self._run_install_func(cmd)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("two answers to one question", proc.stderr + proc.stdout)

        cmd2 = 'parse_args --accept-no-network-policy=maybe; validate_existing_cluster_opt_in_flags'
        proc2 = self._run_install_func(cmd2)
        self.assertNotEqual(proc2.returncode, 0)
        self.assertIn("--accept-no-network-policy must be either true or false.", proc2.stderr + proc2.stdout)

        cmd3 = 'parse_args --accept-no-network-policy --enable-network-policy=false; validate_existing_cluster_opt_in_flags'
        proc3 = self._run_install_func(cmd3)
        self.assertEqual(proc3.returncode, 0, proc3.stderr + proc3.stdout)

    def test_a_flag_overrides_the_recorded_network_policy_answer_for_one_run(self):
        # The "confine it later" path: an install that recorded
        # ACCEPT_NO_NETWORK_POLICY=true re-run with --enable-network-policy
        # passed one flag and is not told it passed two.
        cmd = 'parse_args --enable-network-policy; validate_existing_cluster_opt_in_flags; echo "E=$PARAM_ENABLE_NETWORK_POLICY A=$PARAM_ACCEPT_NO_NETWORK_POLICY"'
        proc = self._run_install_func(cmd, env={"ACCEPT_NO_NETWORK_POLICY": "true"})
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("E=true A=false", proc.stdout)
        self.assertIn("overrides the ACCEPT_NO_NETWORK_POLICY=true", proc.stderr + proc.stdout)

        cmd = 'parse_args --accept-no-network-policy; validate_existing_cluster_opt_in_flags; echo "E=$PARAM_ENABLE_NETWORK_POLICY A=$PARAM_ACCEPT_NO_NETWORK_POLICY"'
        proc = self._run_install_func(cmd, env={"ENABLE_NETWORK_POLICY": "true"})
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("E=false A=true", proc.stdout)

        # Both recorded, neither passed: nothing to prefer, so it is refused and
        # the message names the file as a source.
        cmd = 'validate_existing_cluster_opt_in_flags'
        proc = self._run_install_func(cmd, env={"ENABLE_NETWORK_POLICY": "true", "ACCEPT_NO_NETWORK_POLICY": "true"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("install.env", proc.stderr + proc.stdout)

    def test_validate_existing_cluster_opt_in_flags_rejects_typos(self):
        cmd = 'parse_args --enable-network-policy=ture; validate_existing_cluster_opt_in_flags'
        proc = self._run_install_func(cmd)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--enable-network-policy must be either true or false.", proc.stderr + proc.stdout)

        cmd2 = 'parse_args --migrate-node-pools=invalid; validate_existing_cluster_opt_in_flags'
        proc2 = self._run_install_func(cmd2)
        self.assertNotEqual(proc2.returncode, 0)
        self.assertIn("--migrate-node-pools must be either true or false.", proc2.stderr + proc2.stdout)

        cmd3 = 'parse_args --enable-network-policy=; validate_existing_cluster_opt_in_flags'
        proc3 = self._run_install_func(cmd3)
        self.assertNotEqual(proc3.returncode, 0)
        self.assertIn("--enable-network-policy must be either true or false.", proc3.stderr + proc3.stdout)

        cmd4 = 'PARAM_MIGRATE_NODE_POOLS="invalid"; validate_existing_cluster_opt_in_flags'
        proc4 = self._run_install_func(cmd4)
        self.assertNotEqual(proc4.returncode, 0)
        self.assertIn("--migrate-node-pools must be either true or false.", proc4.stderr + proc4.stdout)

    def test_validate_existing_cluster_opt_in_flags_accepts_valid_values(self):
        cmd = 'parse_args --enable-network-policy=true --migrate-node-pools=false; validate_existing_cluster_opt_in_flags'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        cmd2 = 'parse_args --enable-network-policy --migrate-node-pools; validate_existing_cluster_opt_in_flags'
        proc2 = self._run_install_func(cmd2)
        self.assertEqual(proc2.returncode, 0, proc2.stderr)

        cmd3 = 'validate_existing_cluster_opt_in_flags'
        proc3 = self._run_install_func(cmd3)
        self.assertEqual(proc3.returncode, 0, proc3.stderr)

    def test_default_vertex_location_is_in_scope_for_install_sh(self):
        """install.sh resolves $DEFAULT_VERTEX_LOCATION at its own runtime.

        Both default sites live in run_menu_system/main, which a unit test
        cannot call, so this covers the half that can silently break: whether
        sourcing the helpers actually puts the constant in scope. Under
        `set -u` an unsourced constant would abort rather than expand empty.
        """
        cmd = (
            'source_provisioning_helpers "$PWD" >/dev/null; '
            'echo "LOC=$DEFAULT_VERTEX_LOCATION"'
        )
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("LOC=global", proc.stdout)

    def test_vertex_location_defaults_never_fall_back_to_the_region(self):
        """Every vertex_location default in install.sh uses the shared constant.

        Defaulting the Vertex location to the cluster region is the bug: the
        vertex_ai default model is not served from DEFAULT_REGION, and on a
        zonal cluster the region variable is not even a valid Vertex location.
        There are two such sites -- the main install path and the --menu
        reconfigure path -- and missing either leaves the broken value reachable.
        """
        defaults = [
            line.strip()
            for line in _INSTALL_SH.read_text().splitlines()
            if re.match(r"^\s*local vertex_location=", line)
        ]
        self.assertEqual(len(defaults), 2, f"unexpected vertex_location sites: {defaults}")
        for line in defaults:
            with self.subTest(line=line):
                self.assertIn("DEFAULT_VERTEX_LOCATION", line)
                self.assertNotIn("$region", line)

    def test_default_image_tag_returns_baked_release_version(self):
        """Verifies default_image_tag prioritizes BAKED_RELEASE_VERSION when defined."""
        cmd = 'BAKED_RELEASE_VERSION="0.2.0"; default_image_tag'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "0.2.0")

    def test_default_image_tag_label_returns_official_release(self):
        """Verifies default_image_tag_label formats baked release version label."""
        cmd = 'BAKED_RELEASE_VERSION="0.2.0"; default_image_tag_label'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "official release 0.2.0")

    def test_default_image_tag_falls_back_to_head_sha(self):
        """Verifies default_image_tag defaults to local HEAD SHA in developer checkouts."""
        cmd = 'BAKED_RELEASE_VERSION=""; default_image_tag'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertRegex(
            proc.stdout.strip(),
            r"^([0-9a-f]{40}|[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?)$",
            f"Expected valid 40-character SHA or SemVer tag, got: {proc.stdout.strip()}",
        )

    def test_default_image_tag_resolves_semver_when_multiple_tags_present(self):
        """Verifies default_image_tag prefers numeric SemVer tag over rc_*_validated tags on the same commit."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            # Add installer_common.sh so repo is recognized as kube-agents
            scripts_dir = pathlib.Path(repo_dir) / "scripts" / "installer"
            scripts_dir.mkdir(parents=True, exist_ok=True)
            (scripts_dir / "installer_common.sh").write_text("# mock installer_common.sh\n")
            git("add", "scripts/installer/installer_common.sh")
            git("commit", "-m", "chore: add installer_common.sh")

            # Apply both an rc_* tag and a 0.2.0 GA tag on the same commit
            git("tag", "rc_20260827_validated")
            git("tag", "0.2.0")

            cmd = 'BAKED_RELEASE_VERSION=""; default_image_tag'
            proc = self._run_install_func(cmd, cwd=repo_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "0.2.0")
        finally:
            temp_dir.cleanup()

    def test_default_image_tag_extracts_version_from_archive_directory(self):
        """Verifies default_image_tag resolves version from unpacked archive directory name."""
        import tempfile
        with tempfile.TemporaryDirectory(prefix="archive-test-") as outer_dir:
            archive_dir = pathlib.Path(outer_dir) / "kube-agents-0.2.0"
            archive_dir.mkdir(parents=True)
            scripts_dir = archive_dir / "scripts" / "installer"
            scripts_dir.mkdir(parents=True)
            (scripts_dir / "installer_common.sh").write_text("# mock installer_common.sh\n")

            cmd = 'BAKED_RELEASE_VERSION=""; default_image_tag'
            proc = self._run_install_func(cmd, cwd=archive_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "0.2.0")

    def test_resolve_effective_image_tag_adopts_baked_release_without_prompt(self):
        """Verifies resolve_effective_image_tag adopts baked release version without prompting."""
        cmd = 'BAKED_RELEASE_VERSION="0.4.0"; resolve_effective_image_tag tag "." ""; echo "TAG=$tag"'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("TAG=0.4.0", proc.stdout)
        self.assertIn("Using container image tag (official release 0.4.0)", proc.stdout)

    def test_resolve_effective_image_tag_preserves_explicit_requested_tag(self):
        """Verifies resolve_effective_image_tag honors explicitly passed tag over default."""
        cmd = 'BAKED_RELEASE_VERSION="0.4.0"; resolve_effective_image_tag tag "." "0.3.0"; echo "TAG=$tag"'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("TAG=0.3.0", proc.stdout)

    def test_resolve_effective_image_tag_rejects_invalid_requested_tag(self):
        """Verifies resolve_effective_image_tag validates explicit tag and rejects mutable ref cleanly."""
        cmd = 'resolve_effective_image_tag tag "." "latest" || rc=$?; echo "RC=${rc:-0} TAG=$tag"'
        proc = self._run_install_func(cmd)
        self.assertIn("RC=1 TAG=", proc.stdout)
        self.assertIn("Mutable image/source ref 'latest' is not supported", proc.stdout)

    def test_resolve_effective_image_tag_fails_when_non_interactive_and_no_default(self):
        """Verifies resolve_effective_image_tag errors when non-interactive and no default tag exists."""
        with tempfile.TemporaryDirectory() as empty_dir:
            cmd = f'BAKED_RELEASE_VERSION=""; PARAM_NON_INTERACTIVE="true"; resolve_effective_image_tag tag "{empty_dir}" ""'
            proc = self._run_install_func(cmd)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("--image-tag is required", proc.stdout)

    def test_resolve_effective_image_tag_fails_headless_without_tty_and_no_default(self):
        """Verifies resolve_effective_image_tag errors cleanly in headless environments without TTY."""
        with tempfile.TemporaryDirectory() as empty_dir:
            cmd = f'BAKED_RELEASE_VERSION=""; PARAM_NON_INTERACTIVE="false"; has_controlling_tty() {{ return 1; }}; resolve_effective_image_tag tag "{empty_dir}" "" || rc=$?; echo "RC=$rc TAG=$tag"'
            proc = self._run_install_func(cmd)
            self.assertIn("RC=1 TAG=", proc.stdout)
            self.assertIn("--image-tag is required", proc.stdout)

    def test_resolve_effective_image_tag_resolves_from_external_cwd(self):
        """Verifies resolve_effective_image_tag discovers repo root even when cwd is external."""
        with tempfile.TemporaryDirectory() as outside_dir:
            cmd = 'BAKED_RELEASE_VERSION=""; resolve_effective_image_tag tag "" ""; echo "TAG=$tag"'
            proc = self._run_install_func(cmd, cwd=outside_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertRegex(
                proc.stdout.strip(),
                r"TAG=([0-9a-fA-F]{40}|[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?)$",
            )

    def test_resolve_effective_image_tag_discovers_home_kube_agents_repo(self):
        """Verifies resolve_effective_image_tag adopts tag from HOME/kube-agents when standalone."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            home_dir = temp_path / "home"
            repo_dir = home_dir / "kube-agents"
            scripts_dir = repo_dir / "scripts" / "installer"
            scripts_dir.mkdir(parents=True)
            (scripts_dir / "installer_common.sh").write_text("# marker\n")

            subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo_dir), check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir), check=True)
            (repo_dir / "file.txt").write_text("initial\n")
            subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_dir), check=True)
            subprocess.run(["git", "tag", "0.4.0"], cwd=str(repo_dir), check=True)

            outside_dir = temp_path / "outside"
            outside_dir.mkdir()
            isolated_install_sh = outside_dir / "install.sh"
            isolated_install_sh.write_text(_INSTALL_SH.read_text())

            cmd = 'BAKED_RELEASE_VERSION=""; resolve_effective_image_tag tag "." ""; echo "TAG=$tag"'
            setup = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{isolated_install_sh}"
{cmd}
"""
            full_env = get_isolated_test_env(overrides={"HOME": str(home_dir), "KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env)})
            proc = subprocess.run(
                ["bash", "-c", setup],
                capture_output=True,
                text=True,
                env=full_env,
                cwd=str(outside_dir),
            )
            self.assertEqual(proc.returncode, 0, f"Failed: {proc.stderr}")
            self.assertIn("TAG=0.4.0", proc.stdout)
            self.assertIn("Using container image tag (release tag 0.4.0)", proc.stdout)

    def test_resolve_effective_image_tag_prompts_and_retries_on_invalid_ref(self):
        """Verifies resolve_effective_image_tag prompts interactively and loops until valid ref is entered."""
        with tempfile.TemporaryDirectory() as empty_dir:
            count_file = pathlib.Path(empty_dir) / "calls.txt"
            cmd = (
                'BAKED_RELEASE_VERSION=""; PARAM_NON_INTERACTIVE="false"; '
                'has_controlling_tty() { return 0; }; '
                f'CALL_FILE="{count_file}"; '
                'prompt_read() { '
                '  echo 1 >> "$CALL_FILE"; '
                '  local count; count="$(wc -l < "$CALL_FILE" | tr -d "[:space:]")"; '
                '  if [ "$count" -eq 1 ]; then printf -v "$2" "%s" "invalid_tag"; '
                '  else printf -v "$2" "%s" "0.4.0"; fi; '
                '}; '
                f'resolve_effective_image_tag tag "{empty_dir}" ""; '
                'echo "TAG=$tag CALLS=$(wc -l < "$CALL_FILE" | tr -d "[:space:]")"'
            )
            proc = self._run_install_func(cmd)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("TAG=0.4.0 CALLS=2", proc.stdout)
            self.assertIn("Image/source ref must be a full 40-character commit SHA", proc.stdout)

    def test_resolve_effective_image_tag_does_not_fire_err_trap_or_clobber_report(self):
        """Verifies failure in resolve_effective_image_tag does not trigger ERR trap or overwrite install report."""
        with tempfile.TemporaryDirectory() as temp_dir:
            report_file = pathlib.Path(temp_dir) / "install-report.json"
            report_file.write_text('{"status": "PREVIOUS_SUCCESS"}\n')
            cmd = f'''
set -Eeuo pipefail
REPORT_FILE="{report_file}"
write_json_report() {{
  echo "{{\\"status\\": \\"$1\\"}}" > "$REPORT_FILE"
}}
on_error() {{
  echo "INTERNAL_ERR_TRAP_FIRED" >&2
  write_json_report "FAILED"
}}
trap 'on_error' ERR
BAKED_RELEASE_VERSION=""
PARAM_NON_INTERACTIVE="true"
local_tag=""
resolve_effective_image_tag local_tag "{temp_dir}" "" || rc=$?
echo "RC=$rc"
'''
            proc = self._run_install_func(cmd)
            self.assertIn("RC=1", proc.stdout)
            self.assertNotIn("INTERNAL_ERR_TRAP_FIRED", proc.stderr)
            self.assertIn("--image-tag is required", proc.stdout)
            self.assertEqual(report_file.read_text(), '{"status": "PREVIOUS_SUCCESS"}\n')

    def test_run_menu_system_binds_param_image_tag_to_save_and_apply(self):
        """Verifies run_menu_system passes PARAM_IMAGE_TAG into option 6 (Save & Apply)."""
        cmd = """
has_controlling_tty() { return 0; }
prompt_menu() {
  local var="${!#}"
  printf -v "$var" "%s" "6"
}
verify_local_source_ref() {
  echo "VERIFIED_IMAGE_TAG=$2"
  exit 0
}
PROJECT_ID="test-project"
PARAM_IMAGE_TAG="0.4.0"
run_menu_system "."
"""
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, f"Failed: {proc.stderr}")
        self.assertIn("VERIFIED_IMAGE_TAG=0.4.0", proc.stdout)

    def test_run_menu_system_derives_chat_sub_name_on_save_and_apply(self):
        """Verifies run_menu_system derives CHAT_SUB_NAME from custom topic when saving."""
        cmd = """
has_controlling_tty() { return 0; }
prompt_menu() {
  local var="${!#}"
  printf -v "$var" "%s" "6"
}
resolve_effective_image_tag() { return 0; }
validate_immutable_ref() { return 0; }
verify_local_source_ref() { return 0; }
print_success() {
  if [[ "$1" == *"Updated configuration saved to"* ]]; then
    exit 0
  fi
}
tf_state_chat_subscription_name() { return 0; }

PROJECT_ID="test-project"
GOOGLE_CHAT_ENABLED="true"
CHAT_TOPIC_NAME="custom-topic"
run_menu_system "."
"""
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, f"Failed: {proc.stderr}")
        env_content = self._empty_install_env.read_text()
        self.assertIn("CHAT_SUB_NAME=custom-topic-sub", env_content)

    def test_run_menu_system_recovers_chat_sub_name_from_state_on_save_and_apply(self):
        """Verifies run_menu_system recovers existing subscription from state when saving."""
        cmd = """
has_controlling_tty() { return 0; }
prompt_menu() {
  local var="${!#}"
  printf -v "$var" "%s" "6"
}
resolve_effective_image_tag() { return 0; }
validate_immutable_ref() { return 0; }
verify_local_source_ref() { return 0; }
print_success() {
  if [[ "$1" == *"Updated configuration saved to"* ]]; then
    exit 0
  fi
}
gcloud() {
  if [ "$1" = "storage" ] && [ "$2" = "cat" ]; then
    cat <<'EOF'
{
  "resources": [
    {
      "mode": "managed",
      "type": "google_pubsub_subscription",
      "name": "chat_events",
      "instances": [
        {
          "attributes": {
            "name": "managed-state-sub"
          }
        }
      ]
    }
  ]
}
EOF
    return 0
  fi
  command gcloud "$@"
}

PROJECT_ID="test-project"
GOOGLE_CHAT_ENABLED="true"
CHAT_TOPIC_NAME="custom-topic"
run_menu_system "."
"""
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, f"Failed: {proc.stderr}")
        env_content = self._empty_install_env.read_text()
        self.assertIn("CHAT_SUB_NAME=managed-state-sub", env_content)

    def test_run_menu_system_rederives_when_recorded_sub_equals_default_and_state_empty(self):
        """Verifies run_menu_system re-derives custom-topic subscription if recorded sub is default and state is empty."""
        cmd = """
has_controlling_tty() { return 0; }
prompt_menu() {
  local var="${!#}"
  printf -v "$var" "%s" "6"
}
resolve_effective_image_tag() { return 0; }
validate_immutable_ref() { return 0; }
verify_local_source_ref() { return 0; }
print_success() {
  if [[ "$1" == *"Updated configuration saved to"* ]]; then
    exit 0
  fi
}
tf_state_chat_subscription_name() { return 0; }

PROJECT_ID="test-project"
GOOGLE_CHAT_ENABLED="true"
CHAT_TOPIC_NAME="custom-topic"
CHAT_SUB_NAME="platform-agent-chat-events-sub"
run_menu_system "."
"""
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, f"Failed: {proc.stderr}")
        env_content = self._empty_install_env.read_text()
        self.assertIn("CHAT_SUB_NAME=custom-topic-sub", env_content)

    def test_verify_local_source_ref_accepts_baked_release_in_non_git_dir(self):
        """Verifies verify_local_source_ref succeeds for unpacked release archive without Git repository."""
        with tempfile.TemporaryDirectory(prefix="unpacked-release-") as outer_dir:
            archive_dir = pathlib.Path(outer_dir) / "kube-agents-0.2.0"
            archive_dir.mkdir(parents=True)

            cmd = f'BAKED_RELEASE_VERSION="0.2.0"; verify_local_source_ref "{archive_dir}" "0.2.0"'
            proc = self._run_install_func(cmd, cwd=archive_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Verified install sources match baked official release 0.2.0", proc.stdout)

    def test_verify_local_source_ref_accepts_release_bundle_marker_in_non_git_dir(self):
        """Verifies verify_local_source_ref logs bundle provenance attribution when .release-bundle matches baked version."""
        with tempfile.TemporaryDirectory(prefix="unpacked-bundle-") as outer_dir:
            archive_dir = pathlib.Path(outer_dir) / f"kube-agents-{MOCK_RELEASE_BUNDLE_VERSION}"
            create_mock_release_bundle_marker(archive_dir)

            cmd = f'BAKED_RELEASE_VERSION="{MOCK_RELEASE_BUNDLE_VERSION}"; verify_local_source_ref "{archive_dir}" "{MOCK_RELEASE_BUNDLE_VERSION}"'
            proc = self._run_install_func(cmd, cwd=archive_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(f"Verified install sources match official release bundle {MOCK_RELEASE_BUNDLE_VERSION}", proc.stdout)

    def test_verify_local_source_ref_rejects_unbaked_release_bundle_marker_without_override(self):
        """Verifies .release-bundle marker cannot bypass unversioned source directory rejection when baked version is empty."""
        with tempfile.TemporaryDirectory(prefix="unpacked-unbaked-") as outer_dir:
            archive_dir = pathlib.Path(outer_dir) / f"kube-agents-{MOCK_RELEASE_BUNDLE_VERSION}"
            create_mock_release_bundle_marker(archive_dir)

            cmd = f'BAKED_RELEASE_VERSION=""; verify_local_source_ref "{archive_dir}" "{MOCK_RELEASE_BUNDLE_VERSION}"'
            proc = self._run_install_func(cmd, cwd=archive_dir)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Refusing to provision from an unversioned source directory", proc.stdout)

    def test_verify_local_source_ref_in_git_worktree_enforces_git_alignment_even_with_baked_version(self):
        """Verifies verify_local_source_ref strictly runs Git alignment in real Git checkouts even with baked version."""
        with tempfile.TemporaryDirectory(prefix="git-repo-") as repo_dir:
            repo_path = pathlib.Path(repo_dir)
            subprocess.run(["git", "init"], cwd=str(repo_path), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo_path), check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_path), check=True)
            (repo_path / "file.txt").write_text("initial\n")
            subprocess.run(["git", "add", "file.txt"], cwd=str(repo_path), check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_path), check=True)
            subprocess.run(["git", "tag", "0.2.0"], cwd=str(repo_path), check=True)

            # Add an uncommitted modification to make working tree dirty
            (repo_path / "file.txt").write_text("dirty uncommitted change\n")

            cmd = f'BAKED_RELEASE_VERSION="0.2.0"; verify_local_source_ref "{repo_path}" "0.2.0"'
            proc = self._run_install_func(cmd, cwd=repo_path)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("dirty checkout", proc.stdout)

    def test_gvisor_defaults_to_on(self):
        """The agent runs model-authored commands; the sandbox is the default."""
        proc = self._run_install_func('echo "GVISOR=$PARAM_ENABLE_GVISOR"')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("GVISOR=true", proc.stdout)

    def test_parse_args_keeps_an_empty_gvisor_value_empty(self):
        """`--gvisor=` must reach main's validator rather than read as a default.

        main uses ${PARAM_ENABLE_GVISOR-true} for exactly this: parse_args
        leaves the empty string in place, the `:-` form would silently
        substitute it back to the default, and the validator rejects it.
        """
        cmd = 'parse_args --gvisor=; echo "GVISOR=[$PARAM_ENABLE_GVISOR]"'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("GVISOR=[]", proc.stdout)

    def test_prompt_menu_defaults_to_the_first_option(self):
        """The premise the gVisor prompt's ordering rests on.

        main lists the incoming value as option 1 and treats option 2 as "the
        other one", so that answering the prompt with nothing confirms what
        `--gvisor` asked for and the `(Default)` label matches what that
        produces. It holds only while prompt_menu resolves an unanswered
        prompt to option 1; if that moves, the prompt starts inverting the
        caller's choice in silence.

        With no controlling TTY this takes prompt_read's auto-select branch
        rather than a literal empty line, but both resolve through the same
        default_val="1" that prompt_menu passes.
        """
        cmd = (
            'gvisor_choice=""; prompt_menu "Pick" "first" "second" gvisor_choice; '
            'echo "CHOICE=$gvisor_choice"'
        )
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("CHOICE=1", proc.stdout)

    def _run_with_kubectl_stub(self, func_call, kubectl_script, env=None):
        """Run `func_call` with a stub `kubectl` on PATH.

        `@COUNTER@` in either string becomes a scratch file private to this
        run, for a stub that has to answer differently on each call.

        The poll interval is flattened after sourcing rather than through the
        environment: install.sh assigns it outright, the way it does every
        other timing constant, so only a post-source assignment takes.
        """
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            counter = str(pathlib.Path(tmp) / "calls")
            kubectl = bin_dir / "kubectl"
            kubectl.write_text(
                "#!/usr/bin/env bash\n" + kubectl_script.replace("@COUNTER@", counter) + "\n"
            )
            kubectl.chmod(kubectl.stat().st_mode | stat.S_IEXEC)
            return self._run_install_func(
                "DEPLOYMENT_POLL_INTERVAL_SECS=0\n" + func_call.replace("@COUNTER@", counter),
                env=env,
                bin_dir=str(bin_dir),
            )

    def test_wait_for_deployment_object_returns_once_it_exists(self):
        proc = self._run_with_kubectl_stub(
            'rc=0; wait_for_deployment_object dep ns 0 || rc=$?; echo "RC=$rc"',
            "exit 0",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("RC=0", proc.stdout)

    def test_wait_for_deployment_object_waits_for_a_late_deployment(self):
        """The reason the health check waits rather than asking once.

        The operator writes the agent Deployment after the apply returns, and
        later still when it has a RuntimeClass to resolve first, so a single
        unretried `kubectl get` reports a Deployment that is merely late as one
        that was never created.
        """
        stub = (
            'n=$(cat @COUNTER@ 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > @COUNTER@; '
            '[ "$n" -ge 3 ] && exit 0; exit 1'
        )
        proc = self._run_with_kubectl_stub(
            'rc=0; wait_for_deployment_object dep ns 30 || rc=$?; '
            'echo "RC=$rc TRIES=$(cat @COUNTER@)"',
            stub,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("RC=0 TRIES=3", proc.stdout)

    def test_wait_for_deployment_object_gives_up_after_the_budget(self):
        """A Deployment that is never coming still has to end the run."""
        proc = self._run_with_kubectl_stub(
            'rc=0; wait_for_deployment_object dep ns 0 || rc=$?; echo "RC=$rc"',
            "exit 1",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("RC=1", proc.stdout)

    def test_print_generate_only_handoff_renders_required_commands(self):
        """Verifies print_generate_only_handoff prints all out-of-Terraform and lifecycle commands."""
        cmd = f"""
{_SOURCE_INSTALLER_COMMON}
PROJECT_ID="test-proj"
CLUSTER_NAME="test-cluster"
INSTALL_ENV_FILE="/tmp/test/install.env"
print_generate_only_handoff "/tmp/test-repo" "test-proj" "test-cluster" "us-central1" "/tmp/test-repo/terraform/examples/full-install/terraform.tfvars"
"""
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = proc.stdout
        # Out-of-Terraform prerequisites
        self.assertIn("CMEK Database Encryption (pre-existing cluster without CMEK):", out)
        self.assertIn("gcloud services enable cloudkms.googleapis.com --project=test-proj", out)
        self.assertIn("gcloud beta services identity create --service=container.googleapis.com --project=test-proj", out)
        self.assertIn("gcloud kms keys add-iam-policy-binding", out)
        self.assertIn('--member="serviceAccount:service-$(gcloud projects describe test-proj --format=\'value(projectNumber)\')@container-engine-robot.iam.gserviceaccount.com" \\', out)
        self.assertIn('--role="roles/cloudkms.cryptoKeyEncrypterDecrypter" --project=test-proj --quiet', out)
        self.assertIn("gcloud container clusters update test-cluster --location us-central1 --database-encryption-key=", out)
        self.assertIn("Workload Identity Pool (pre-existing Standard cluster):", out)
        self.assertIn("gcloud container clusters update test-cluster --location us-central1 --project test-proj --workload-pool=test-proj.svc.id.goog", out)
        self.assertIn("NetworkPolicy Enforcement (pre-existing cluster without Dataplane V2):", out)
        self.assertIn("gcloud container clusters update test-cluster --location us-central1 --project test-proj --update-addons=NetworkPolicy=ENABLED", out)
        self.assertIn("gcloud container clusters update test-cluster --location us-central1 --project test-proj --enable-network-policy", out)
        self.assertIn("GitHub App PEM Import (before apply, when GitOps minter is enabled):", out)
        self.assertIn("gcloud kms keyrings create github-token-minter-keyring --location=us-central1 --project=test-proj", out)
        self.assertIn("gcloud kms keys create github-token-minter-key --keyring=github-token-minter-keyring", out)
        self.assertIn("--purpose=asymmetric-signing", out)
        self.assertIn("--import-only --skip-initial-version-creation", out)
        self.assertIn("git clone --depth 1 --branch v2.7.1 https://github.com/abcxyz/github-token-minter.git /tmp/minty", out)
        self.assertIn("go run ./cmd/minty tools import-pk", out)
        # Lifecycle commands with bucket/prefix
        self.assertIn("cd /tmp/test-repo/terraform/examples/full-install", out)
        self.assertIn('KUBE_AGENTS_STATE_BUCKET="test-proj-kube-agents-tfstate" KUBE_AGENTS_STATE_PREFIX="kube-agents/test-cluster" ./lifecycle.sh apply', out)
        # Post-apply OTel scope
        self.assertIn("Managed OpenTelemetry Scope:", out)
        self.assertIn("gcloud container clusters update test-cluster --location us-central1 --project test-proj --managed-otel-scope=COLLECTION_AND_INSTRUMENTATION_COMPONENTS", out)

    def test_write_json_report_includes_generate_only(self):
        """Verifies write_json_report outputs generate_only boolean."""
        cmd = """
PARAM_DRY_RUN="false"
PARAM_GENERATE_ONLY="true"
PARAM_NON_INTERACTIVE="true"
INSTALL_ENV_FILE="/tmp/install.env"
write_json_report "GENERATE_ONLY_SUCCESS" >/dev/null
cat /tmp/kube-agents-install-report.json
"""
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('"status": "GENERATE_ONLY_SUCCESS"', proc.stdout)
        self.assertIn('"generate_only": true', proc.stdout)

    def test_write_json_report_records_network_policy_enforcement(self):
        """The choice to install without enforcement outlives the terminal (#1682)."""
        cmd = """
PARAM_DRY_RUN="false"
PARAM_GENERATE_ONLY="false"
PARAM_NON_INTERACTIVE="true"
INSTALL_ENV_FILE="/tmp/install.env"
NETWORK_POLICY_ENFORCEMENT="$NP_ENFORCEMENT_ABSENT_ACCEPTED"
write_json_report "SUCCESS" >/dev/null
cat /tmp/kube-agents-install-report.json
"""
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('"network_policy_enforcement": "absent-accepted"', proc.stdout)

        # Before a run has decided, the field is present and empty rather than
        # restating a default the run never applied.
        proc = self._run_install_func(cmd.replace('NETWORK_POLICY_ENFORCEMENT="$NP_ENFORCEMENT_ABSENT_ACCEPTED"\n', ""))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('"network_policy_enforcement": ""', proc.stdout)


class InstallEnvInputTest(unittest.TestCase):
    """install.env is an input, loaded before the parameter block.

    The ordering is the mechanism: every `PARAM_X="${VAR:-}"` seed already knew
    how to inherit from the environment, and loading the file into the
    environment first is what makes inheritance the default path rather than
    something each flag has to remember. That is what closes #1060 as a class
    instead of patching its eight instances, so these tests are about the
    inheritance itself, not about any one flag.
    """

    def _source_with_env_file(self, body, contents=None, env=None, path=None):
        """Source install.sh with KUBE_AGENTS_INSTALL_ENV pointing at a file.

        The explicit path rather than the beside-the-script discovery: a
        developer's real install.env would otherwise decide the result. The
        discovery itself is covered separately below.
        """
        with tempfile.TemporaryDirectory() as tmp:
            env_file = pathlib.Path(tmp) / (path or "install.env")
            if contents is not None:
                env_file.write_text(contents)
            overrides = {"KUBE_AGENTS_INSTALL_ENV": str(env_file)}
            overrides.update(env or {})
            # Cleared so an exported value from the developer's own shell
            # cannot stand in for the file under test.
            full_env = get_isolated_test_env(overrides=overrides)
            for leaking in (
                "PROJECT_ID", "REGION", "CLUSTER_NAME", "MODEL_PROVIDER",
                "ENABLE_GVISOR", "MEMORY", "MEMORY_PROVIDER", "ALLOWED_USERS",
                "GOOGLE_CHAT_ENABLED", "API_SERVER_KEY", "ENABLE_WEBUI",
                "HERMES_DASHBOARD_ENABLED", "PLATFORM_AGENT_PERMISSION_SET",
            ):
                full_env.pop(leaking, None)
            full_env.update(overrides)
            setup = f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n{body}\n'
            return subprocess.run(
                ["bash", "-c", setup],
                capture_output=True,
                text=True,
                env=full_env,
                cwd=str(_REPO_ROOT),
            )

    def test_values_reach_the_parameter_block(self):
        """The whole point: a value in the file arrives as a PARAM_*."""
        proc = self._source_with_env_file(
            'echo "P=$PARAM_PROJECT_ID R=$PARAM_REGION M=$PARAM_MODEL_PROVIDER"',
            contents="PROJECT_ID=from-the-file\nREGION=europe-west4\nMODEL_PROVIDER=vertex_ai\n",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("P=from-the-file R=europe-west4 M=vertex_ai", proc.stdout)

    def test_a_flag_beats_the_file(self):
        """Order of authority: flag, then file, then default."""
        proc = self._source_with_env_file(
            'parse_args --project-id=from-the-flag; echo "P=$PARAM_PROJECT_ID"',
            contents="PROJECT_ID=from-the-file\n",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("P=from-the-flag", proc.stdout)

    def test_the_values_are_exported_not_merely_assigned(self):
        """write_tfvars_from_state and the TF_VAR_* handoff read the
        environment, so a value that parsed but did not export would reach
        neither. `set -a` around the source is what guarantees it."""
        proc = self._source_with_env_file(
            "bash -c 'echo EXPORTED=\"$PROJECT_ID\"'",
            contents="PROJECT_ID=travels-to-children\n",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("EXPORTED=travels-to-children", proc.stdout)

    def test_a_named_file_that_is_absent_is_an_error(self):
        """Only reachable through an explicit KUBE_AGENTS_INSTALL_ENV. Asking
        for a path by name and not getting it is a mistake, not a first
        install, and silently continuing would provision from defaults."""
        proc = self._source_with_env_file("true", contents=None)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("does not exist", proc.stdout + proc.stderr)

    def test_an_unparseable_file_is_reported_by_name(self):
        """Sourcing it would abort through the ERR trap with a bash parse
        error and no indication of which file was at fault."""
        proc = self._source_with_env_file("true", contents='PROJECT_ID="unclosed\n')
        self.assertNotEqual(proc.returncode, 0)
        combined = proc.stdout + proc.stderr
        self.assertIn("not valid shell", combined)

    def test_no_file_at_all_is_the_ordinary_first_install(self):
        """A first install has nothing to inherit and must not be blocked."""
        full_env = get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": ""})
        proc = subprocess.run(
            ["bash", "-c", f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"; echo OK'],
            capture_output=True,
            text=True,
            env=full_env,
            cwd=str(tempfile.gettempdir()),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("OK", proc.stdout)

    def test_loading_says_nothing_on_stdout(self):
        """Sourcing install.sh must leave stdout clean.

        The load happens at source time, before main(), so a message on stdout
        lands in front of whatever the caller captures next -- including a
        function's echoed return value, which is how most of this file's tests
        read install.sh. That made the suite pass or fail depending on whether
        the developer running it happened to have an install.env, which is the
        worst kind of flake: it looks like the change under test.
        """
        proc = self._source_with_env_file(
            'printf "%s" "ONLY-THIS"',
            contents="PROJECT_ID=noisy\n",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # Byte-for-byte: a function that echoes its answer is read exactly this
        # way, so anything else on stdout corrupts it.
        self.assertEqual(proc.stdout, "ONLY-THIS")
        self.assertIn("Loaded install configuration", proc.stderr)

    def test_it_is_discovered_beside_the_script(self):
        """The documented location, and the one a curl | bash install into a
        working directory also finds."""
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            (home / "install.sh").write_text(_INSTALL_SH.read_text())
            (home / "install.env").write_text("PROJECT_ID=found-beside-the-script\n")
            proc = subprocess.run(
                [
                    "bash",
                    "-c",
                    f'KUBE_AGENTS_SOURCE_ONLY=true source "{home}/install.sh"; '
                    'echo "P=$PARAM_PROJECT_ID"',
                ],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": ""}),
                cwd=str(home),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertIn("P=found-beside-the-script", proc.stdout)


class NonInteractiveRerunInheritanceTest(unittest.TestCase):
    """The eight settings #1060 names, each checked for inheritance.

    Every one of these destroyed something when a non-interactive re-run
    omitted its flag: the Pub/Sub topic, kubeagents-litellm-gsa, the gVisor
    pool, the custom role list, a Hindsight deployment, the GitOps org, the
    allowlist that keeps the agent private, and the Secret every pod holds.
    """

    def _params(self, contents, body):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = pathlib.Path(tmp) / "install.env"
            env_file.write_text(contents)
            full_env = get_isolated_test_env(
                overrides={"KUBE_AGENTS_INSTALL_ENV": str(env_file)}
            )
            return subprocess.run(
                ["bash", "-c",
                 f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n{body}\n'],
                capture_output=True,
                text=True,
                env=full_env,
                cwd=str(_REPO_ROOT),
            )

    def test_google_chat_inherits_the_way_slack_already_did(self):
        """Google Chat inherits from the loaded configuration, as Slack does.

        The chat gate reads SLACK_ENABLED out of the file; PARAM_ENABLE_GOOGLE_CHAT
        taking the flag alone would revert Chat -- and only Chat -- to false and
        plan its Pub/Sub topic and subscription away. (see #1060)
        """
        proc = self._params(
            "GOOGLE_CHAT_ENABLED=true\n", 'echo "C=$PARAM_ENABLE_GOOGLE_CHAT"'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("C=true", proc.stdout)

    def test_the_settings_that_seeded_from_their_own_name(self):
        """Model provider, gVisor, permission set and custom roles, GitOps org.
        These already read an environment variable of the right name; what they
        never had was a file to read it from."""
        proc = self._params(
            "MODEL_PROVIDER=vertex_ai\n"
            "ENABLE_GVISOR=true\n"
            "PLATFORM_AGENT_PERMISSION_SET=custom\n"
            "PLATFORM_AGENT_CUSTOM_ROLES=roles/container.viewer\n"
            "GITHUB_ORG=an-org\n"
            "GITHUB_REPO=a-repo\n",
            'echo "M=$PARAM_MODEL_PROVIDER G=$PARAM_ENABLE_GVISOR '
            'P=$PARAM_PERMISSION_SET C=$PARAM_CUSTOM_ROLES '
            'O=$PARAM_GITOPS_ORG R=$PARAM_GITOPS_REPO"',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn(
            "M=vertex_ai G=true P=custom C=roles/container.viewer O=an-org R=a-repo",
            proc.stdout,
        )

    def test_memory_inherits_through_the_recorded_spelling(self):
        """--memory and the recorded setting are spelled differently.

        The flag is --memory (file|hindsight|off) and the install records
        MEMORY_PROVIDER, so a file written by a previous install carries only the
        second spelling. Without the translation, omitting --memory deletes a
        Hindsight API and its Postgres. (see #1060)
        """
        for provider, expected in (
            ("kube_agents_memory", "hindsight"),
            ("none", "off"),
            ("multiuser_memory", "file"),
        ):
            with self.subTest(provider=provider):
                proc = self._params(
                    f"MEMORY_PROVIDER={provider}\n", 'echo "M=$PARAM_MEMORY"'
                )
                self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                self.assertIn(f"M={expected}", proc.stdout)

    def test_memory_prefers_the_input_spelling_when_both_are_present(self):
        proc = self._params(
            "MEMORY=off\nMEMORY_PROVIDER=kube_agents_memory\n", 'echo "M=$PARAM_MEMORY"'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("M=off", proc.stdout)

    def test_the_dashboard_inherits_through_its_recorded_spelling_too(self):
        proc = self._params(
            "HERMES_DASHBOARD_ENABLED=true\n", 'echo "W=$PARAM_ENABLE_WEBUI"'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("W=true", proc.stdout)

    def test_allowed_users_has_a_flag_and_inherits(self):
        """The allowlist survives a non-interactive re-run.

        An empty list allows every user, so losing it opens the agent rather
        than merely dropping a setting. (see #1060)
        """
        proc = self._params(
            "ALLOWED_USERS=a@example.com,b@example.com", 'echo "U=$PARAM_ALLOWED_USERS"'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("U=a@example.com,b@example.com", proc.stdout)

        proc = self._params(
            "ALLOWED_USERS=from-the-file@example.com",
            'parse_args --allowed-users=from-the-flag@example.com; '
            'echo "U=$PARAM_ALLOWED_USERS"',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("U=from-the-flag@example.com", proc.stdout)

    def test_google_chat_home_channel_has_a_flag_and_inherits(self):
        proc = self._params(
            "GOOGLE_CHAT_HOME_CHANNEL=spaces/FROM_FILE",
            'echo "H=$PARAM_GOOGLE_CHAT_HOME_CHANNEL"',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("H=spaces/FROM_FILE", proc.stdout)

        proc = self._params(
            "GOOGLE_CHAT_HOME_CHANNEL=spaces/FROM_FILE",
            'parse_args --google-chat-home-channel=spaces/FROM_FLAG; '
            'echo "H=$PARAM_GOOGLE_CHAT_HOME_CHANNEL"',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("H=spaces/FROM_FLAG", proc.stdout)

    def test_the_gitops_repo_names_are_gitops_prefixed(self):
        """GITOPS_ORG / GITOPS_REPO are the installer's input names. (see #1026)

        The old pair collided with two other things: GH_ORG / GH_REPO on the rc
        and nightly environments name the *release* repository, and tests/e2e
        uses GITHUB_ORG / GITHUB_REPO for the repository a test acts on. Three
        repositories, two names.
        """
        proc = self._params(
            "GITOPS_ORG=an-org\nGITOPS_REPO=a-repo\n",
            'echo "O=$PARAM_GITOPS_ORG R=$PARAM_GITOPS_REPO"',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("O=an-org R=a-repo", proc.stdout)

    def test_the_old_names_still_work_and_say_so(self):
        """A deprecation, not a break: an install.env or a CI environment still
        carrying GITHUB_ORG / GITHUB_REPO keeps working, and is told to rename."""
        proc = self._params(
            "GITHUB_ORG=an-org\nGITHUB_REPO=a-repo\n",
            'source scripts/installer/installer_common.sh; '
            'normalize_gitops_repo_vars; '
            'echo "O=$GITOPS_ORG R=$GITOPS_REPO"',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("O=an-org R=a-repo", proc.stdout)
        combined = proc.stdout + proc.stderr
        self.assertIn("GITHUB_ORG is deprecated", combined)
        self.assertIn("GITOPS_ORG", combined)

    def test_the_new_names_win_over_the_old(self):
        """Both present is a mid-migration environment, not an error. The name
        that survives is the one being migrated to."""
        proc = self._params(
            "GITHUB_ORG=old-org\nGITOPS_ORG=new-org\n",
            'source scripts/installer/installer_common.sh; '
            'normalize_gitops_repo_vars; echo "O=$GITOPS_ORG"',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("O=new-org", proc.stdout)

    def test_the_old_names_are_kept_in_step_for_one_release(self):
        """The agent runtime and the chart still speak GITHUB_*. They are
        exported FROM the GITOPS_* value rather than left as a second source of
        truth, so the two can never disagree."""
        proc = self._params(
            "GITOPS_ORG=new-org\nGITOPS_REPO=new-repo\n",
            'source scripts/installer/installer_common.sh; '
            'normalize_gitops_repo_vars; echo "O=$GITHUB_ORG R=$GITHUB_REPO"',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("O=new-org R=new-repo", proc.stdout)

    def test_the_api_server_key_is_not_minted_by_install_sh(self):
        """API_SERVER_KEY is minted inside write_tfvars_from_state, after recovery.

        The generator's recovery loop skips any key already set, so a key
        exported before it shadows the live Secret: every run would replace the
        Secret and restart every pod. (see #1060)
        """
        source = _INSTALL_SH.read_text()
        self.assertNotIn(
            "openssl rand -hex 16",
            source,
            "install.sh must not mint an API_SERVER_KEY before the generator "
            "has had a chance to recover the live one",
        )
        self.assertIn(
            "KUBE_AGENTS_GENERATE_API_SERVER_KEY=true",
            source,
            "install.sh is the one front door entitled to mint a key, and says so",
        )

    def test_a_configured_api_server_key_is_carried_through(self):
        proc = self._params(
            "API_SERVER_KEY=deadbeefdeadbeef\n", 'echo "K=$API_SERVER_KEY"'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("K=deadbeefdeadbeef", proc.stdout)

    def test_migrate_node_pools_inherits_from_install_env(self):
        proc = self._params(
            "MIGRATE_NODE_POOLS=true\n",
            'echo "M=$PARAM_MIGRATE_NODE_POOLS"',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("M=true", proc.stdout)

    def test_enable_network_policy_inherits_from_install_env(self):
        proc = self._params(
            "ENABLE_NETWORK_POLICY=true\n",
            'echo "N=$PARAM_ENABLE_NETWORK_POLICY"',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("N=true", proc.stdout)

    def test_accept_no_network_policy_inherits_from_install_env(self):
        # A standing decision about the cluster: a re-run that omits the flag
        # must not turn the accepted install back into a refusal.
        proc = self._params(
            "ACCEPT_NO_NETWORK_POLICY=true\n",
            'echo "A=$PARAM_ACCEPT_NO_NETWORK_POLICY"',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("A=true", proc.stdout)


class SecretManagerAutoDiscoveryQuietTest(unittest.TestCase):
    """Verifies gcloud secrets versions access passes --quiet to avoid hangs."""

    def test_gcloud_secrets_versions_access_passes_quiet(self):
        source = _INSTALL_SH.read_text()
        matches = re.findall(r"gcloud secrets versions access[^\n]+", source)
        self.assertTrue(len(matches) >= 2, f"Expected at least 2 calls, found: {matches}")
        for match in matches:
            self.assertIn(
                "--quiet",
                match,
                f"gcloud secrets versions access must pass --quiet to avoid interactive prompts on disabled APIs: {match}",
            )


class EnsureExistingClusterNetworkPolicyTest(unittest.TestCase):
    """ensure_existing_cluster_network_policy's two-call enablement sequence.

    GKE rejects `--enable-network-policy` with HTTP 400 until the Calico addon
    is on the control plane, and gcloud refuses `--update-addons` and
    `--enable-network-policy` in one invocation, so the order of the two
    `clusters update` calls is the behaviour under test.
    """

    def _run(self, datapath="", legacy_np="", opt_in=True, status="RUNNING", accept=False, preset=""):
        """Run the function against a stub gcloud that records every call.

        Returns (CompletedProcess, [argv-strings in call order]). The stub
        answers `clusters describe` on the --format it is given: an empty
        string stands for a field gcloud did not print. `accept` stands for
        --accept-no-network-policy, the answer that leaves the cluster alone.
        """
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            log = pathlib.Path(tmp) / "gcloud.log"
            gcloud = bin_dir / "gcloud"
            gcloud.write_text(
                "#!/usr/bin/env bash\n"
                f"printf '%s\\n' \"$*\" >> '{log}'\n"
                'case "$*" in\n'
                f"  *datapathProvider,networkPolicy.enabled*) printf '{status},{datapath},{legacy_np}\\n' ;;\n"
                f"  *datapathProvider*) printf '{datapath}\\n' ;;\n"
                f"  *networkPolicy.enabled*) printf '{legacy_np}\\n' ;;\n"
                "esac\n"
                "exit 0\n"
            )
            gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
            empty_env = pathlib.Path(tmp) / "install.env"
            empty_env.write_text("")
            opt_in_line = (
                'PARAM_ENABLE_NETWORK_POLICY="true"\n' if opt_in else ""
            )
            if accept:
                opt_in_line += 'PARAM_ACCEPT_NO_NETWORK_POLICY="true"\n'
            if preset:
                opt_in_line += f'NETWORK_POLICY_ENFORCEMENT="{preset}"\n'
            body = (
                f'source "{_INSTALLER_COMMON}"\n'
                f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                f"{opt_in_line}"
                "ensure_existing_cluster_network_policy proj cluster region\n"
                'echo "RECORDED=$NETWORK_POLICY_ENFORCEMENT"\n'
            )
            proc = subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": str(empty_env)}, bin_dir=str(bin_dir)),
                cwd=str(_REPO_ROOT),
            )
            calls = log.read_text().splitlines() if log.exists() else []
            return proc, calls

    @staticmethod
    def _updates(calls):
        return [c for c in calls if "clusters update" in c]

    def test_addon_is_enabled_before_enforcement(self):
        # The bug: a lone --enable-network-policy against a cluster whose
        # addon is off fails with "The network policy addon must be enabled
        # before updating the nodes" (HTTP 400).
        proc, calls = self._run(opt_in=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        updates = self._updates(calls)
        self.assertEqual(len(updates), 2, updates)
        self.assertIn("--update-addons=NetworkPolicy=ENABLED", updates[0])
        self.assertIn("--enable-network-policy", updates[1])
        # Neither call may carry both flags: gcloud puts them in the same
        # "exactly one of these must be specified" group.
        self.assertNotIn("--enable-network-policy", updates[0])
        self.assertNotIn("--update-addons", updates[1])

    def test_skipped_without_opt_in(self):
        proc, calls = self._run(opt_in=False)
        self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
        self.assertEqual(self._updates(calls), [])
        self.assertIn("Explicit opt-in was not provided", proc.stderr + proc.stdout)
        # The refusal names both ways forward; naming only the mutating one is
        # how an agent came to read it as the instruction (#1682).
        self.assertIn("--accept-no-network-policy", proc.stderr + proc.stdout)

    def test_accepted_absence_leaves_the_cluster_alone_and_is_recorded(self):
        # The third branch: no clusters update at all, exit 0, the decision
        # recorded where write_json_report reads it, and one line saying it is
        # proceeding as accepted -- the consequences were stated at the
        # preflight, which is the only way a run reaches this step accepted.
        proc, calls = self._run(opt_in=False, accept=True)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertEqual(self._updates(calls), [])
        out = proc.stderr + proc.stdout
        self.assertIn("WITHOUT NetworkPolicy enforcement, as accepted above", out)
        self.assertNotIn("enforced by nothing", out)
        self.assertIn("RECORDED=absent-accepted", proc.stdout)

    def test_enforcement_the_install_enabled_is_recorded(self):
        proc, _ = self._run(opt_in=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("RECORDED=enabled-by-install", proc.stdout)

    def test_accepting_on_a_cluster_that_enforces_changes_nothing(self):
        # accept is inert where there is nothing to accept: Dataplane V2 is
        # recorded as enforced, and no warning about a lost sandbox is printed.
        proc, calls = self._run(datapath="ADVANCED_DATAPATH", opt_in=False, accept=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._updates(calls), [])
        self.assertIn("RECORDED=enforced", proc.stdout)
        self.assertNotIn("WITHOUT NetworkPolicy enforcement", proc.stderr + proc.stdout)

    def test_addon_state_is_not_probed(self):
        # Skipping the addon call when it is already on would be free, but
        # addonsConfig.networkPolicyConfig.disabled cannot say so: GKE omits
        # false booleans, so "on" and "describe failed" both print nothing.
        # A gate on it either never fires or reintroduces the 400 — hence the
        # unconditional call, and hence this test, which fails if someone
        # reintroduces the probe.
        _, calls = self._run(opt_in=True)
        self.assertEqual(
            [c for c in calls if "networkPolicyConfig" in c], [], calls
        )

    def test_dataplane_v2_cluster_is_left_alone(self):
        _, calls = self._run(datapath="ADVANCED_DATAPATH", opt_in=True)
        self.assertEqual(self._updates(calls), [])

    def test_cluster_already_enforcing_is_left_alone(self):
        _, calls = self._run(legacy_np="True", opt_in=True)
        self.assertEqual(self._updates(calls), [])

    def test_refuses_when_cluster_unreadable(self):
        proc, calls = self._run(status="", opt_in=True)
        self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
        self.assertEqual(self._updates(calls), [])
        self.assertIn("Could not query NetworkPolicy configuration", proc.stderr + proc.stdout)
        self.assertIn("Refusing to attempt cluster mutations", proc.stderr + proc.stdout)


class EnsureExistingClusterWorkloadIdentityTest(unittest.TestCase):
    """ensure_existing_cluster_workload_identity tests."""

    def _run(
        self,
        autopilot="false",
        workload_pool="",
        node_pools="",
        migrate_opt_in=False,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            log = pathlib.Path(tmp) / "gcloud.log"
            gcloud = bin_dir / "gcloud"
            gcloud.write_text(
                "#!/usr/bin/env bash\n"
                f"printf '%s\\n' \"$*\" >> '{log}'\n"
                'case "$*" in\n'
                f"  *autopilot.enabled*) printf '{autopilot}\\n' ;;\n"
                f"  *workloadIdentityConfig.workloadPool*) printf '{workload_pool}\\n' ;;\n"
                f"  *node-pools*list*) printf '{node_pools}\\n' ;;\n"
                "  *'node-pools update'*) printf 'op-1\\n' ;;\n"
                "  *'operations describe'*) printf 'DONE|||\\n' ;;\n"
                "esac\n"
                "exit 0\n"
            )
            gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
            opt_in_line = (
                'PARAM_MIGRATE_NODE_POOLS="true"\n' if migrate_opt_in else ""
            )
            body = (
                f'source "{_INSTALLER_COMMON}"\n'
                f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                f"{opt_in_line}"
                "ensure_existing_cluster_workload_identity proj cluster region\n"
            )
            proc = subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(bin_dir=str(bin_dir)),
                cwd=str(_REPO_ROOT),
            )
            calls = log.read_text().splitlines() if log.exists() else []
            return proc, calls

    def test_autopilot_cluster_is_left_alone(self):
        proc, calls = self._run(autopilot="True")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        updates = [c for c in calls if "update" in c]
        self.assertEqual(updates, [])

    def test_cluster_without_workload_pool_updates_pool(self):
        proc, calls = self._run(
            autopilot="false",
            workload_pool="",
            node_pools="default-pool,GKE_METADATA",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        cluster_updates = [c for c in calls if "clusters update" in c]
        self.assertEqual(len(cluster_updates), 1)
        self.assertIn("--workload-pool=proj.svc.id.goog", cluster_updates[0])
        node_updates = [c for c in calls if "node-pools update" in c]
        self.assertEqual(node_updates, [])

    def test_legacy_node_pool_refused_without_opt_in(self):
        proc, calls = self._run(
            autopilot="false",
            workload_pool="proj.svc.id.goog",
            node_pools="pool-1,GCE_METADATA",
            migrate_opt_in=False,
        )
        self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
        node_updates = [c for c in calls if "node-pools update" in c]
        self.assertEqual(node_updates, [])
        self.assertIn("has node pool(s) 'pool-1' using the legacy GCE metadata server", proc.stderr + proc.stdout)
        self.assertIn("Aborting before making any cluster changes", proc.stderr + proc.stdout)

    def test_legacy_node_pool_migrated_with_opt_in(self):
        proc, calls = self._run(
            autopilot="false",
            workload_pool="proj.svc.id.goog",
            node_pools="pool-1,GCE_METADATA",
            migrate_opt_in=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        node_updates = [c for c in calls if "node-pools update" in c]
        self.assertEqual(len(node_updates), 1)
        self.assertIn("--workload-metadata=GKE_METADATA", node_updates[0])
        self.assertIn("pool-1", node_updates[0])
        self.assertIn("--async", node_updates[0])


class NodePoolMetadataMigrationPollingTest(unittest.TestCase):
    """Tests dynamic timeout scaling and GKE operation polling during node pool migration (#1286)."""

    def test_dynamic_timeout_scales_with_node_count(self):
        body = (
            f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
            'for n in 0 1 5 6 9 12; do\n'
            '  echo "$n=$(calculate_node_pool_update_timeout $n)"\n'
            'done\n'
        )
        proc = subprocess.run(
            ["bash", "-c", body],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(),
            cwd=str(_REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        expected = "0=1800\n1=1800\n5=1800\n6=1800\n9=2700\n12=3600\n"
        self.assertEqual(proc.stdout, expected)

    def test_get_node_pool_node_count_prefers_igm_target_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            gcloud = bin_dir / "gcloud"
            igm_urls = (
                "https://compute.googleapis.com/compute/v1/projects/p/zones/us-central1-a/instanceGroupManagers/igm-a;"
                "https://compute.googleapis.com/compute/v1/projects/p/zones/us-central1-b/instanceGroupManagers/igm-b;"
                "https://compute.googleapis.com/compute/v1/projects/p/zones/us-central1-c/instanceGroupManagers/igm-c"
            )
            gcloud.write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                f"  *'node-pools describe'*) printf '1|3|{igm_urls}\\n' ;;\n"
                "  *'instance-groups managed describe'*) printf '3\\n' ;;\n"
                "esac\n"
                "exit 0\n"
            )
            gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
            body = (
                f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                "get_node_pool_node_count p c us-central1 pool-9\n"
            )
            proc = subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(bin_dir=str(bin_dir)),
                cwd=str(_REPO_ROOT),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "9")

    def test_get_node_pool_node_count_falls_back_to_initial_count_when_igm_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            gcloud = bin_dir / "gcloud"
            gcloud.write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                "  *'node-pools describe'*) printf '3|3|\\n' ;;\n"
                "  *'instance-groups managed describe'*) exit 1 ;;\n"
                "esac\n"
                "exit 0\n"
            )
            gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
            body = (
                f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                "get_node_pool_node_count p c us-central1 pool-9\n"
            )
            proc = subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(bin_dir=str(bin_dir)),
                cwd=str(_REPO_ROOT),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "9")

    def test_get_node_pool_node_count_respects_zero_node_live_igm_target_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            gcloud = bin_dir / "gcloud"
            gcloud.write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                "  *'node-pools describe'*)\n"
                "    printf '5|2|https://www.googleapis.com/compute/v1/projects/p/zones/us-central1-a/instanceGroupManagers/igm-a\\n'\n"
                "    ;;\n"
                "  *'instance-groups managed describe'*)\n"
                "    printf '0\\n'\n"
                "    ;;\n"
                "esac\n"
                "exit 0\n"
            )
            gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
            body = (
                f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                "get_node_pool_node_count p c us-central1 pool-zero\n"
            )
            proc = subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(bin_dir=str(bin_dir)),
                cwd=str(_REPO_ROOT),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "0")

    def test_get_node_pool_node_count_falls_back_on_partial_igm_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            gcloud = bin_dir / "gcloud"
            gcloud.write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                "  *'node-pools describe'*)\n"
                "    printf '4|2|https://www.googleapis.com/compute/v1/projects/p/zones/us-central1-a/instanceGroupManagers/igm-a;https://www.googleapis.com/compute/v1/projects/p/zones/us-central1-b/instanceGroupManagers/igm-b\\n'\n"
                "    ;;\n"
                "  *'instance-groups managed describe'*'igm-a'*)\n"
                "    printf '4\\n'\n"
                "    ;;\n"
                "  *'instance-groups managed describe'*'igm-b'*)\n"
                "    exit 1\n"
                "    ;;\n"
                "esac\n"
                "exit 0\n"
            )
            gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
            body = (
                f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                "get_node_pool_node_count p c us-central1 pool-partial\n"
            )
            proc = subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(bin_dir=str(bin_dir)),
                cwd=str(_REPO_ROOT),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "8")

    def test_migration_polls_operation_until_done_and_extends_while_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            state_file = pathlib.Path(tmp) / "poll_count"
            state_file.write_text("0")
            gcloud = bin_dir / "gcloud"
            gcloud.write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                "  *'node-pools describe'*) printf '9|1|\\n' ;;\n"
                "  *'node-pools update'*) printf 'projects/p/zones/r/operations/op-1286\\n' ;;\n"
                "  *'operations describe'*)\n"
                f"    c=$(cat '{state_file}')\n"
                "    c=$((c + 1))\n"
                f"    printf '%s' \"$c\" > '{state_file}'\n"
                "    if [ \"$c\" -lt 4 ]; then\n"
                "      printf 'RUNNING|updating node %d of 9||\\n' \"$c\"\n"
                "    else\n"
                "      printf 'DONE|||\\n'\n"
                "    fi\n"
                "    ;;\n"
                "esac\n"
                "exit 0\n"
            )
            gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
            body = (
                "export NODE_POOL_UPDATE_MIN_TIMEOUT_SECS=2\n"
                "export NODE_POOL_UPDATE_PER_NODE_TIMEOUT_SECS=0\n"
                "export NODE_POOL_UPDATE_EXTENSION_SECS=2\n"
                "export NODE_POOL_UPDATE_MAX_TIMEOUT_SECS=10\n"
                "export NODE_POOL_UPDATE_POLL_INTERVAL_SECS=1\n"
                f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                "migrate_node_pool_to_gke_metadata p c r pool-large\n"
            )
            proc = subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(bin_dir=str(bin_dir)),
                cwd=str(_REPO_ROOT),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertIn("Polling operation 'op-1286' for node pool 'pool-large'", proc.stdout)
            self.assertIn("updating node 1 of 9", proc.stdout)
            self.assertIn("Operation 'op-1286' is still RUNNING after 2s; extending wait timeout to 4s", proc.stdout)
            self.assertIn("Node pool 'pool-large' metadata migration completed (operation 'op-1286')", proc.stdout)

    def test_migration_fails_when_operation_finishes_with_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            gcloud = bin_dir / "gcloud"
            gcloud.write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                "  *'node-pools describe'*) printf '2|1|\\n' ;;\n"
                "  *'node-pools update'*) printf 'op-err-99\\n' ;;\n"
                "  *'operations describe'*) printf 'DONE|||Quota exceeded | in region\\n' ;;\n"
                "esac\n"
                "exit 0\n"
            )
            gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
            body = (
                "export NODE_POOL_UPDATE_POLL_INTERVAL_SECS=0\n"
                f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                "migrate_node_pool_to_gke_metadata p c r pool-err\n"
            )
            proc = subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(bin_dir=str(bin_dir)),
                cwd=str(_REPO_ROOT),
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("finished with error: Quota exceeded | in region", proc.stdout + proc.stderr)

    def test_migration_recovers_from_transient_describe_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            state_file = pathlib.Path(tmp) / "poll_count"
            state_file.write_text("0")
            gcloud = bin_dir / "gcloud"
            gcloud.write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                "  *'node-pools describe'*) printf '2|1|\\n' ;;\n"
                "  *'node-pools update'*) printf 'op-transient\\n' ;;\n"
                "  *'operations describe'*)\n"
                f"    c=$(cat '{state_file}')\n"
                "    c=$((c + 1))\n"
                f"    printf '%s' \"$c\" > '{state_file}'\n"
                "    if [ \"$c\" -le 2 ]; then\n"
                "      exit 1\n"
                "    else\n"
                "      printf 'DONE|||\\n'\n"
                "    fi\n"
                "    ;;\n"
                "esac\n"
                "exit 0\n"
            )
            gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
            body = (
                "export NODE_POOL_UPDATE_POLL_INTERVAL_SECS=0\n"
                "export NODE_POOL_UPDATE_POLL_MAX_RETRIES=3\n"
                f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                "migrate_node_pool_to_gke_metadata p c r pool-t\n"
            )
            proc = subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(bin_dir=str(bin_dir)),
                cwd=str(_REPO_ROOT),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertIn("Transient error querying operation 'op-transient' (attempt 1/3)", proc.stdout)
            self.assertIn("Transient error querying operation 'op-transient' (attempt 2/3)", proc.stdout)
            self.assertIn("Node pool 'pool-t' metadata migration completed (operation 'op-transient')", proc.stdout)

    def test_migration_times_out_when_exceeding_max_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            gcloud = bin_dir / "gcloud"
            gcloud.write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                "  *'node-pools describe'*) printf '2|1|\\n' ;;\n"
                "  *'node-pools update'*) printf 'op-stuck\\n' ;;\n"
                "  *'operations describe'*) printf 'RUNNING|still running||\\n' ;;\n"
                "esac\n"
                "exit 0\n"
            )
            gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
            body = (
                "export NODE_POOL_UPDATE_MIN_TIMEOUT_SECS=1\n"
                "export NODE_POOL_UPDATE_PER_NODE_TIMEOUT_SECS=0\n"
                "export NODE_POOL_UPDATE_EXTENSION_SECS=1\n"
                "export NODE_POOL_UPDATE_MAX_TIMEOUT_SECS=2\n"
                "export NODE_POOL_UPDATE_POLL_INTERVAL_SECS=1\n"
                f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                "migrate_node_pool_to_gke_metadata p c r pool-stuck\n"
            )
            proc = subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(bin_dir=str(bin_dir)),
                cwd=str(_REPO_ROOT),
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Timed out after 2s waiting for GKE operation 'op-stuck'", proc.stdout + proc.stderr)

    def test_migration_fails_immediately_when_update_initiation_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            gcloud = bin_dir / "gcloud"
            gcloud.write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                "  *'node-pools describe'*) printf '2|1|\\n' ;;\n"
                "  *'node-pools update'*) exit 1 ;;\n"
                "esac\n"
                "exit 0\n"
            )
            gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
            body = (
                f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                "migrate_node_pool_to_gke_metadata p c r pool-fail-init\n"
            )
            proc = subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(bin_dir=str(bin_dir)),
                cwd=str(_REPO_ROOT),
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Failed to initiate metadata migration on node pool 'pool-fail-init'", proc.stdout + proc.stderr)

    def test_migration_verifies_live_mode_when_op_id_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            gcloud = bin_dir / "gcloud"
            gcloud.write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                "  *'node-pools describe'*'workloadMetadataConfig.mode'*)\n"
                "    printf 'GCE_METADATA\\n'\n"
                "    ;;\n"
                "  *'node-pools describe'*)\n"
                "    printf '2|1|\\n'\n"
                "    ;;\n"
                "  *'node-pools update'*)\n"
                "    printf '\\n'\n"
                "    ;;\n"
                "  *'operations list'*)\n"
                "    printf '\\n'\n"
                "    ;;\n"
                "esac\n"
                "exit 0\n"
            )
            gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
            body = (
                f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                "migrate_node_pool_to_gke_metadata p c r pool-no-op\n"
            )
            proc = subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(bin_dir=str(bin_dir)),
                cwd=str(_REPO_ROOT),
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("did not produce an operation ID and mode remains 'GCE_METADATA'", proc.stdout + proc.stderr)





class EnsureExistingClusterGatedOnCreateClusterTest(unittest.TestCase):
    """Verifies existing cluster out-of-band mutations are gated on TFVARS_CREATE_CLUSTER=false."""

    def test_mutations_gated_on_adoption(self):
        text = _INSTALL_SH.read_text()
        pattern = r'if \[ "\$\{TFVARS_CREATE_CLUSTER:-true\}" = "false" \]; then\s+ensure_existing_cluster_network_policy'
        self.assertRegex(text, pattern)


class CheckExistingClusterNodePoolsPreflightTest(unittest.TestCase):
    """check_existing_cluster_node_pools_preflight tests."""

    def _run(self, autopilot="false", node_pools="", opt_in=""):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            gcloud = bin_dir / "gcloud"
            gcloud.write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                f"  *autopilot.enabled*) printf '{autopilot}\\n' ;;\n"
                f"  *node-pools*list*) printf '{node_pools}\\n' ;;\n"
                "esac\n"
                "exit 0\n"
            )
            gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
            opt_in_line = f'PARAM_MIGRATE_NODE_POOLS="{opt_in}"\n' if opt_in else ""
            body = (
                f'source "{_INSTALLER_COMMON}"\n'
                f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                'TFVARS_CREATE_CLUSTER="false"\n'
                f"{opt_in_line}"
                "check_existing_cluster_node_pools_preflight p c r\n"
            )
            return subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(bin_dir=str(bin_dir)),
                cwd=str(_REPO_ROOT),
            )

    def test_refuses_when_legacy_pools_and_no_opt_in(self):
        proc = self._run(autopilot="false", node_pools="default-pool,GCE_METADATA", opt_in="false")
        self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
        self.assertIn("has node pool(s) 'default-pool' using the legacy GCE metadata server", proc.stderr + proc.stdout)
        self.assertIn("Aborting before making any cluster changes. Pass --migrate-node-pools", proc.stderr + proc.stdout)

    def test_passes_when_opt_in_provided(self):
        proc = self._run(autopilot="false", node_pools="default-pool,GCE_METADATA", opt_in="true")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

    def test_passes_when_all_pools_gke_metadata(self):
        proc = self._run(autopilot="false", node_pools="default-pool,GKE_METADATA", opt_in="false")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

    def test_passes_when_autopilot(self):
        proc = self._run(autopilot="True", node_pools="default-pool,GCE_METADATA", opt_in="false")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)


class CheckExistingClusterNetworkPolicyPreflightTest(unittest.TestCase):
    """check_existing_cluster_network_policy_preflight tests."""

    def _run(self, dp="", legacy_np="", opt_in="", status="RUNNING", accept="", install_env=None):
        """`install_env`, when given, is the contents of a pre-existing install.env
        the run points INSTALL_ENV_FILE at; None means no file."""
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            gcloud = bin_dir / "gcloud"
            gcloud.write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                f"  *datapathProvider,networkPolicy.enabled*) printf '{status},{dp},{legacy_np}\\n' ;;\n"
                f"  *datapathProvider*) printf '{dp}\\n' ;;\n"
                f"  *networkPolicy.enabled*) printf '{legacy_np}\\n' ;;\n"
                "esac\n"
                "exit 0\n"
            )
            gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
            empty_env = pathlib.Path(tmp) / "install.env"
            empty_env.write_text("")
            opt_in_line = f'PARAM_ENABLE_NETWORK_POLICY="{opt_in}"\n' if opt_in else ""
            if accept:
                opt_in_line += f'PARAM_ACCEPT_NO_NETWORK_POLICY="{accept}"\n'
            if install_env is not None:
                existing = pathlib.Path(tmp) / "existing.env"
                existing.write_text(install_env)
                opt_in_line += f'INSTALL_ENV_FILE="{existing}"\n'
            body = (
                f'source "{_INSTALLER_COMMON}"\n'
                f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                'TFVARS_CREATE_CLUSTER="false"\n'
                f"{opt_in_line}"
                "check_existing_cluster_network_policy_preflight p c r\n"
                'echo "RECORDED=$NETWORK_POLICY_ENFORCEMENT"\n'
            )
            return subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": str(empty_env)}, bin_dir=str(bin_dir)),
                cwd=str(_REPO_ROOT),
            )

    def test_refuses_when_lacking_both_and_no_opt_in(self):
        proc = self._run(dp="", legacy_np="False", opt_in="false")
        self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
        self.assertIn("enforces no NetworkPolicy", proc.stderr + proc.stdout)
        # Both answers are named, and the cost of each; an agent reading this
        # refusal is not handed one flag to pass.
        self.assertIn("--enable-network-policy", proc.stderr + proc.stdout)
        self.assertIn("--accept-no-network-policy", proc.stderr + proc.stdout)
        self.assertIn("may recreate node pools", proc.stderr + proc.stdout)
        self.assertIn("unconfined", proc.stderr + proc.stdout)

    def test_passes_when_opt_in_provided(self):
        proc = self._run(dp="", legacy_np="False", opt_in="true")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

    def test_passes_unchanged_when_absence_is_accepted(self):
        # The acceptance criterion of #1682: an agent-driven install onto a
        # cluster with no enforcement completes without modifying it, and not
        # silently -- the consequences are printed and the choice recorded.
        proc = self._run(dp="", legacy_np="False", opt_in="false", accept="true")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        out = proc.stderr + proc.stdout
        self.assertNotIn("REFUSED", out)
        self.assertIn("WITHOUT NetworkPolicy enforcement", out)
        self.assertIn("The cluster is not modified", out)
        self.assertIn("RECORDED=absent-accepted", proc.stdout)

    def test_the_preflight_repeats_the_unrecorded_key_note(self):
        # The settle step and the preflight each describe the cluster once. If
        # the first failed and this one succeeds, install.env was just written
        # without the key; the preflight asks the note again so the run says so.
        proc = self._run(dp="", legacy_np="False", accept="true", install_env="PROJECT_ID=p\n")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("Add ACCEPT_NO_NETWORK_POLICY=true", proc.stderr + proc.stdout)

        proc = self._run(dp="", legacy_np="False", accept="true", install_env="ACCEPT_NO_NETWORK_POLICY=true\n")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertNotIn("Add ACCEPT_NO_NETWORK_POLICY", proc.stderr + proc.stdout)

    def test_acceptance_is_inert_where_enforcement_exists(self):
        proc = self._run(dp="ADVANCED_DATAPATH", legacy_np="False", accept="true")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("RECORDED=enforced", proc.stdout)
        self.assertNotIn("WITHOUT NetworkPolicy enforcement", proc.stderr + proc.stdout)

    def test_unreadable_cluster_is_refused_even_when_accepting(self):
        # Accepting the absence of enforcement is not accepting an unknown
        # cluster state; that refusal stands.
        proc = self._run(status="", accept="true")
        self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
        self.assertIn("Could not query NetworkPolicy configuration", proc.stderr + proc.stdout)

    def test_passes_when_dataplane_v2(self):
        proc = self._run(dp="ADVANCED_DATAPATH", legacy_np="False", opt_in="false")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

    def test_passes_when_calico_already_enabled(self):
        proc = self._run(dp="", legacy_np="True", opt_in="false")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

    def test_refuses_when_cluster_unreadable(self):
        proc = self._run(status="", opt_in="false")
        self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
        self.assertIn("Could not query NetworkPolicy configuration", proc.stderr + proc.stdout)
        self.assertNotIn("enforces no NetworkPolicy", proc.stderr + proc.stdout)

    def test_refuses_when_cluster_unreadable_even_with_opt_in(self):
        proc = self._run(status="", opt_in="true")
        self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
        self.assertIn("Could not query NetworkPolicy configuration", proc.stderr + proc.stdout)
        self.assertNotIn("enforces no NetworkPolicy", proc.stderr + proc.stdout)


class GenerateOnlyCrossesTheExistingClusterConsentGatesTest(unittest.TestCase):
    """--generate-only is held to the same existing-cluster refusals as a real run.

    The mode's whole output is terraform.tfvars for an operator to apply, and
    tfvars for a cluster enforcing no NetworkPolicy cannot apply -- the
    gke-cluster module's postcondition rejects them. Reporting
    GENERATE_ONLY_SUCCESS over inputs already known to fail is worse than
    refusing, especially since the refusal names the opt-in flag the apply needs
    anyway. Exempting the mode also splits it from the interactive `g`, which
    install-kube-agents/SKILL.md calls the same choice.

    These read the source rather than running it: the gate is main()'s control
    flow, which the KUBE_AGENTS_SOURCE_ONLY harness cannot drive. The behaviour
    of the two functions themselves is covered by the two classes above.
    """

    _POOLS_CALL = 'check_existing_cluster_node_pools_preflight "$project_id" "$cluster_name" "$region"'
    _NETPOL_CALL = 'check_existing_cluster_network_policy_preflight "$project_id" "$cluster_name" "$region"'
    _PROMPT = "Proceed with automated GKE cluster & Platform Agent provisioning? (Y/n/g)"
    _MODE_BRANCH = "Generate-only: configuration files written"

    def test_neither_preflight_is_conditioned_on_the_mode(self):
        # self.fail rather than assertNotRegex: the latter prints the whole of
        # install.sh as the subject on failure, burying the one line at issue.
        gated = re.search(
            r'if \[ "\$PARAM_GENERATE_ONLY" != "true" \][^\n]*\n(?:[^\n]*\n)*?'
            r"\s*check_existing_cluster_(?:node_pools|network_policy)_preflight",
            _INSTALL_SH.read_text(),
        )
        if gated:
            self.fail(
                "the existing-cluster consent gates sit inside a --generate-only "
                f"exemption, which #1336 added them to prevent: {gated.group(0)!r}"
            )

    def test_both_preflights_run_above_the_confirmation_prompt(self):
        """Above the prompt is what makes the flag and the `g` answer the same choice."""
        text = _INSTALL_SH.read_text()
        pools = text.index(self._POOLS_CALL)
        netpol = text.index(self._NETPOL_CALL)
        prompt = text.index(self._PROMPT)
        mode_branch = text.index(self._MODE_BRANCH)
        self.assertLess(pools, prompt, "the node-pool gate must precede the (Y/n/g) prompt")
        self.assertLess(netpol, prompt, "the NetworkPolicy gate must precede the (Y/n/g) prompt")
        self.assertLess(prompt, mode_branch, "the prompt must precede the generate-only handoff")


class InteractiveNetworkPolicyPromptTest(unittest.TestCase):
    """prompt_existing_cluster_opt_ins on the path an interactive adoption takes.

    This is the prompt a real run reaches: main() calls it before the summary,
    against a Standard cluster with neither Dataplane V2 nor Calico, with no
    flag and no install.env answer. The three answers each have to land in
    the two PARAM_ variables the rest of the run reads.
    """

    def _run(self, answer, dp="", legacy_np="False", autopilot="False", preset=""):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            gcloud = bin_dir / "gcloud"
            gcloud.write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                f"  *autopilot.enabled*) printf '{autopilot}\\n' ;;\n"
                "  *node-pools*list*) printf 'default-pool,GKE_METADATA\\n' ;;\n"
                f"  *datapathProvider,networkPolicy.enabled*) printf 'RUNNING,{dp},{legacy_np}\\n' ;;\n"
                "esac\n"
                "exit 0\n"
            )
            gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
            empty_env = pathlib.Path(tmp) / "install.env"
            empty_env.write_text("")
            body = (
                f'source "{_INSTALLER_COMMON}"\n'
                f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                'PARAM_NON_INTERACTIVE="false"\n'
                'PARAM_DRY_RUN="false"\n'
                f"{preset}"
                "has_controlling_tty() { return 0; }\n"
                f'prompt_read() {{ echo "PROMPTED: $1"; printf -v "$2" "%s" "{answer}"; }}\n'
                "prompt_existing_cluster_opt_ins proj cluster region; echo \"rc=$?\"\n"
                'echo "E=${PARAM_ENABLE_NETWORK_POLICY:-unset} A=${PARAM_ACCEPT_NO_NETWORK_POLICY:-unset}"\n'
            )
            return subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(
                    overrides={"KUBE_AGENTS_INSTALL_ENV": str(empty_env)}, bin_dir=str(bin_dir)
                ),
                cwd=str(_REPO_ROOT),
            )

    def test_accept_sets_the_accept_answer(self):
        proc = self._run("a")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("PROMPTED: Choose (e/a/N)", proc.stdout)
        self.assertIn("E=false A=true", proc.stdout)

    def test_enable_sets_the_enable_answer(self):
        proc = self._run("e")
        self.assertIn("E=true A=false", proc.stdout, proc.stderr)

    def test_the_default_answers_neither(self):
        proc = self._run("")
        self.assertIn("PROMPTED", proc.stdout, proc.stderr)
        self.assertIn("E=false A=false", proc.stdout)

    def test_a_recorded_answer_is_not_asked_again(self):
        proc = self._run("a", preset='PARAM_ACCEPT_NO_NETWORK_POLICY="true"\n')
        self.assertNotIn("PROMPTED", proc.stdout, proc.stderr)
        self.assertIn("E=unset A=true", proc.stdout)

    def test_an_enforcing_cluster_is_not_asked(self):
        for dp, legacy_np in (("ADVANCED_DATAPATH", "False"), ("", "True")):
            proc = self._run("a", dp=dp, legacy_np=legacy_np)
            self.assertNotIn("PROMPTED", proc.stdout, (dp, legacy_np, proc.stderr))
            self.assertIn("E=unset A=unset", proc.stdout)


class SettleNetworkPolicyAcceptanceTest(unittest.TestCase):
    """settle_network_policy_acceptance: the answer reaches the tfvars and the decision.

    Runs between the prompt and the install.env bootstrap. An accept answered
    at the prompt has to regenerate terraform.tfvars, since the generator ran
    before the prompt and the module's postcondition reads the variable; and
    what install.env then records is the decision from the probe, so a flag
    against an enforcing cluster records nothing and an unreadable cluster
    decides nothing.
    """

    def _run(self, param="", env_accept="", dp="", legacy_np="False", status="RUNNING"):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            gcloud = bin_dir / "gcloud"
            gcloud.write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                f"  *datapathProvider,networkPolicy.enabled*) printf '{status},{dp},{legacy_np}\\n' ;;\n"
                "esac\n"
                "exit 0\n"
            )
            gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
            empty_env = pathlib.Path(tmp) / "install.env"
            empty_env.write_text("")
            preset = f'PARAM_ACCEPT_NO_NETWORK_POLICY="{param}"\n' if param else ""
            preset += f"export ACCEPT_NO_NETWORK_POLICY={env_accept}\n" if env_accept else ""
            body = (
                f'source "{_INSTALLER_COMMON}"\n'
                f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                # The generator has run by then and found the cluster outside state.
                'TFVARS_CREATE_CLUSTER="false"\n'
                f"{preset}"
                'write_tfvars_from_state() { echo "REGENERATED $1 $2 accept=$ACCEPT_NO_NETWORK_POLICY key=${KUBE_AGENTS_GENERATE_API_SERVER_KEY:-}"; }\n'
                "settle_network_policy_acceptance proj cluster region /tmp/t.tfvars 0.5.0; echo \"rc=$?\"\n"
                'echo "DECISION=${NETWORK_POLICY_ENFORCEMENT:-none} ENV=${ACCEPT_NO_NETWORK_POLICY:-unset}"\n'
            )
            return subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(
                    overrides={"KUBE_AGENTS_INSTALL_ENV": str(empty_env)}, bin_dir=str(bin_dir)
                ),
                cwd=str(_REPO_ROOT),
            )

    def test_an_answer_at_the_prompt_regenerates_and_records(self):
        proc = self._run(param="true")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("REGENERATED /tmp/t.tfvars 0.5.0 accept=true key=true", proc.stdout)
        self.assertIn("DECISION=absent-accepted ENV=true", proc.stdout)

    def test_a_flag_already_in_the_environment_does_not_regenerate(self):
        # The first generator pass already saw it.
        proc = self._run(param="true", env_accept="true")
        self.assertNotIn("REGENERATED", proc.stdout, proc.stderr)
        self.assertIn("DECISION=absent-accepted", proc.stdout)

    def test_no_answer_touches_nothing(self):
        proc = self._run()
        self.assertNotIn("REGENERATED", proc.stdout, proc.stderr)
        self.assertIn("DECISION=none ENV=unset", proc.stdout)

    def test_the_flag_against_an_enforcing_cluster_decides_nothing(self):
        for dp, legacy_np in (("ADVANCED_DATAPATH", "False"), ("", "True")):
            proc = self._run(param="true", dp=dp, legacy_np=legacy_np)
            self.assertIn("DECISION=none", proc.stdout, (dp, legacy_np, proc.stderr))

    def test_an_unreadable_cluster_decides_nothing(self):
        # The preflight refuses it a few steps on; recording an acceptance
        # for a cluster nobody read would be a waiver with no decision behind it.
        proc = self._run(param="true", status="")
        self.assertIn("DECISION=none", proc.stdout, proc.stderr)


class AcceptedAbsenceOutlivesTheRunTest(unittest.TestCase):
    """An accepted install without NetworkPolicy enforcement has to survive the run (#1682).

    Three things carry it. install.env, or upgrade.sh's generator emits
    accept_no_network_policy = false and the module refuses the plan the install
    already passed; terraform.tfvars, which the module's postcondition reads and
    which was generated before the interactive prompt could answer; and the
    PlatformAgent annotation the composition stamps. These read main()'s control
    flow, which the KUBE_AGENTS_SOURCE_ONLY harness cannot drive.
    """

    _OPT_IN_PROMPT_CALL = 'prompt_existing_cluster_opt_ins "$project_id" "$cluster_name" "$region"'
    _BOOTSTRAP_CALL = 'bootstrap_install_env_file "$INSTALL_ENV_FILE" "$image_tag"'
    _SETTLE_CALL = 'settle_network_policy_acceptance "$project_id" "$cluster_name" "$region" "$tfvars_file" "$image_tag"'

    def test_the_answer_is_settled_between_the_prompt_and_the_bootstrap(self):
        """settle_network_policy_acceptance (behaviour: SettleNetworkPolicyAcceptanceTest)
        has to run after the prompt that can produce the answer and before the
        bootstrap that records it."""
        text = _INSTALL_SH.read_text()
        settle = text.index(self._SETTLE_CALL)
        self.assertLess(text.index(self._OPT_IN_PROMPT_CALL), settle)
        self.assertLess(settle, text.index(self._BOOTSTRAP_CALL))

    def _note(self, env_contents, decision, func="note_unrecorded_network_policy_acceptance"):
        """Run one of the install.env notes against a file with the given contents.

        `decision` is the value NETWORK_POLICY_ENFORCEMENT holds by then: the
        notes key off what the run decided, not off the flag, so that a flag
        passed against a cluster that already enforces records nothing.
        """
        with tempfile.TemporaryDirectory() as tmp:
            env_file = pathlib.Path(tmp) / "install.env"
            env_file.write_text(env_contents)
            body = (
                f'source "{_INSTALLER_COMMON}"\n'
                f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                f'NETWORK_POLICY_ENFORCEMENT="{decision}"\n'
                f'{func} "{env_file}"\n'
            )
            # PARAM_NON_INTERACTIVE and no TTY: the agent-driven shape, which
            # is where the interview-answer warning deliberately stays silent.
            return subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": str(env_file)}),
                cwd=str(_REPO_ROOT),
            )

    def test_an_unrecorded_acceptance_is_reported_without_a_tty(self):
        """A pre-existing install.env without the key gets the warning, not silence."""
        proc = self._note("PROJECT_ID=p\n", "absent-accepted")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("Add ACCEPT_NO_NETWORK_POLICY=true", proc.stderr + proc.stdout)

    def test_an_acceptance_the_file_records_as_false_is_reported(self):
        proc = self._note("PROJECT_ID=p\nACCEPT_NO_NETWORK_POLICY=false\n", "absent-accepted")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("Add ACCEPT_NO_NETWORK_POLICY=true", proc.stderr + proc.stdout)

    def test_a_recorded_acceptance_is_not_nagged(self):
        proc = self._note("PROJECT_ID=p\nACCEPT_NO_NETWORK_POLICY=true\n", "absent-accepted")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertNotIn("Add ACCEPT_NO_NETWORK_POLICY", proc.stderr + proc.stdout)

    def test_a_flag_against_an_enforcing_cluster_records_nothing(self):
        # The decision, not the flag: enforced means nothing was accepted, so
        # the file is not asked to carry a standing waiver of the module's check.
        for decision in ("enforced", "enabled-by-install", ""):
            proc = self._note("PROJECT_ID=p\n", decision)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertNotIn("ACCEPT_NO_NETWORK_POLICY", proc.stderr + proc.stdout, decision)

    def test_a_stale_recorded_acceptance_is_reported_once_the_cluster_enforces(self):
        # The converse note, so the key retires: confined later, the file still
        # says accepted, and every later upgrade would waive the postcondition.
        for decision in ("enforced", "enabled-by-install"):
            proc = self._note(
                "PROJECT_ID=p\nACCEPT_NO_NETWORK_POLICY=true\n", decision,
                func="note_stale_network_policy_acceptance",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertIn("records ACCEPT_NO_NETWORK_POLICY=true", proc.stderr + proc.stdout, decision)
            self.assertIn("Remove that line", proc.stderr + proc.stdout, decision)

    def test_the_stale_note_stays_quiet_while_the_acceptance_holds(self):
        for contents, decision in (
            ("PROJECT_ID=p\nACCEPT_NO_NETWORK_POLICY=true\n", "absent-accepted"),
            ("PROJECT_ID=p\n", "enforced"),
            ("PROJECT_ID=p\nACCEPT_NO_NETWORK_POLICY=false\n", "enforced"),
        ):
            proc = self._note(contents, decision, func="note_stale_network_policy_acceptance")
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertNotIn("Remove that line", proc.stderr + proc.stdout, (contents, decision))

    def test_the_stale_note_runs_after_the_preflight_and_after_calico_goes_on(self):
        text = _INSTALL_SH.read_text()
        first = text.index('note_stale_network_policy_acceptance "$INSTALL_ENV_FILE"')
        second = text.index('note_stale_network_policy_acceptance "$INSTALL_ENV_FILE"', first + 1)
        self.assertLess(text.index('check_existing_cluster_network_policy_preflight "$project_id"'), first)
        self.assertLess(text.index('ensure_existing_cluster_network_policy "$project_id"'), second)

    def test_install_env_bootstrap_records_the_decision_not_the_flag(self):
        """A fresh install.env carries the key only when enforcement was absent and accepted."""
        for decision, expected in (("absent-accepted", True), ("enforced", False), ("", False)):
            with tempfile.TemporaryDirectory() as tmp:
                dest = pathlib.Path(tmp) / "install.env"
                body = (
                    f'source "{_INSTALLER_COMMON}"\n'
                    f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                    'PARAM_DRY_RUN="false"\n'
                    "export ACCEPT_NO_NETWORK_POLICY=true\n"
                    f'NETWORK_POLICY_ENFORCEMENT="{decision}"\n'
                    f'bootstrap_install_env_file "{dest}" 0.5.0\n'
                )
                empty = pathlib.Path(tmp) / "loaded.env"
                empty.write_text("")
                proc = subprocess.run(
                    ["bash", "-c", body],
                    capture_output=True,
                    text=True,
                    env=get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": str(empty)}),
                    cwd=str(_REPO_ROOT),
                )
                self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                self.assertTrue(dest.exists(), proc.stderr + proc.stdout)
                self.assertEqual(
                    "ACCEPT_NO_NETWORK_POLICY=true" in dest.read_text(), expected, (decision, dest.read_text())
                )


    def test_the_note_runs_when_install_env_already_exists(self):
        """Behaviour: bootstrap against a file that exists calls the note and writes nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            dest = pathlib.Path(tmp) / "install.env"
            dest.write_text("PROJECT_ID=p\n")
            empty = pathlib.Path(tmp) / "loaded.env"
            empty.write_text("")
            body = (
                f'source "{_INSTALLER_COMMON}"\n'
                f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                'PARAM_DRY_RUN="false"\n'
                'NETWORK_POLICY_ENFORCEMENT="absent-accepted"\n'
                f'bootstrap_install_env_file "{dest}" 0.5.0\n'
            )
            proc = subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": str(empty)}),
                cwd=str(_REPO_ROOT),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertIn("Add ACCEPT_NO_NETWORK_POLICY=true", proc.stderr + proc.stdout)
            self.assertEqual(dest.read_text(), "PROJECT_ID=p\n")

    def test_an_unset_answer_is_not_exported_as_false(self):
        """An exported "false" is an answer to the prompt gates, which read
        ${PARAM_...:-${ACCEPT_NO_NETWORK_POLICY:-}}: unconditional export silenced
        the interactive three-way prompt on every run without the flag."""
        text = _INSTALL_SH.read_text()
        self.assertNotIn('export ACCEPT_NO_NETWORK_POLICY="${PARAM_ACCEPT_NO_NETWORK_POLICY:-false}"', text)
        self.assertIn(
            'if [ -n "${PARAM_ACCEPT_NO_NETWORK_POLICY:-}" ]; then\n'
            '    export ACCEPT_NO_NETWORK_POLICY="$PARAM_ACCEPT_NO_NETWORK_POLICY"',
            text,
        )

    def test_the_composition_stamps_the_annotation_from_what_the_module_read(self):
        main_tf = (_REPO_ROOT / "terraform" / "examples" / "full-install" / "main.tf").read_text()
        self.assertIn('"kubeagents.x-k8s.io/network-policy-enforcement" = "absent-accepted"', main_tf)
        self.assertIn("module.gke_cluster.network_policy_enforced ? {}", main_tf)
        self.assertRegex(main_tf, r"accept_no_network_policy\s+= var\.accept_no_network_policy")


class SummarizeExistingClusterMutationsTest(unittest.TestCase):
    """summarize_existing_cluster_mutations outputs expected lines for adoption."""

    def _run(
        self,
        autopilot="false",
        enc_state="ENCRYPTED",
        pool="p.svc.id.goog",
        node_pools="p1,GKE_METADATA",
        dp="ADVANCED_DATAPATH",
        legacy_np="False",
        status="RUNNING",
        accept="",
    ):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            gcloud = bin_dir / "gcloud"
            gcloud.write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                f"  *autopilot.enabled*) printf '{autopilot}\\n' ;;\n"
                f"  *databaseEncryption.state*) printf '{enc_state}\\n' ;;\n"
                f"  *workloadIdentityConfig.workloadPool*) printf '{pool}\\n' ;;\n"
                f"  *node-pools*list*) printf '{node_pools}\\n' ;;\n"
                f"  *datapathProvider,networkPolicy.enabled*) printf '{status},{dp},{legacy_np}\\n' ;;\n"
                f"  *datapathProvider*) printf '{dp}\\n' ;;\n"
                f"  *networkPolicy.enabled*) printf '{legacy_np}\\n' ;;\n"
                "esac\n"
                "exit 0\n"
            )
            gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
            empty_env = pathlib.Path(tmp) / "install.env"
            empty_env.write_text("")
            accept_line = f'PARAM_ACCEPT_NO_NETWORK_POLICY="{accept}"\n' if accept else ""
            body = (
                f'source "{_INSTALLER_COMMON}"\n'
                f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                f"{accept_line}"
                "summarize_existing_cluster_mutations p c r true\n"
            )
            return subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": str(empty_env)}, bin_dir=str(bin_dir)),
                cwd=str(_REPO_ROOT),
            )

    def test_summary_reflects_probed_state(self):
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("CMEK Database Encryption", proc.stdout)
        self.assertIn("Workload Identity Pool", proc.stdout)
        self.assertIn("Node Pool Metadata", proc.stdout)
        self.assertIn("NetworkPolicy Enforcement", proc.stdout)
        self.assertIn("gVisor Sandbox Node Pool", proc.stdout)

    def test_summary_reflects_refused_network_policy_when_missing(self):
        proc = self._run(dp="", legacy_np="False")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("NetworkPolicy Enforcement: Refused", proc.stdout)
        self.assertIn("install will abort", proc.stdout)
        self.assertIn("--accept-no-network-policy", proc.stdout)

    def test_summary_reflects_accepted_absence(self):
        proc = self._run(dp="", legacy_np="False", accept="true")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("NetworkPolicy Enforcement: Absent, accepted", proc.stdout)
        self.assertIn("cluster unchanged", proc.stdout)
        self.assertNotIn("install will abort", proc.stdout)

    def test_summary_reflects_refused_node_pool_migration_when_missing(self):
        proc = self._run(node_pools="default-pool,GCE_METADATA")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Node Pool Metadata Migration: Refused", proc.stdout)
        self.assertIn("install will abort", proc.stdout)

    def test_summary_reflects_unreadable_network_policy(self):
        proc = self._run(status="")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("NetworkPolicy Enforcement: Skipped (could not query cluster network policy state)", proc.stdout)



class MissingPemAgainstKmsTest(unittest.TestCase):
    """resolve_missing_pem_against_kms, the step 8 decision on a .pem that is gone.

    The installer's own docs tell the operator to delete the private key once
    it is in Cloud KMS, and install.env keeps naming it, so a missing file has
    to mean "already imported" whenever the signing key can prove it and
    "nothing left to import with" when it cannot. Getting that backwards is
    silent in both directions: the wrong answer either aborts every re-run of a
    working install, or lets one proceed to an apply that will never have a key.

    Exercised directly rather than through main(): step 8 sits behind cluster
    creation, so nothing in this suite can reach it, and while this decision was
    a block inside main() an inverted test left all 398 tests passing.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._tmp = pathlib.Path(tmp.name)
        self._empty_install_env = self._tmp / "install.env"
        self._empty_install_env.write_text("")
        self._probe_log = self._tmp / "probe.log"

    def _probe_args(self):
        """What the stubbed KMS lookup was asked, or "" if it was never called."""
        return self._probe_log.read_text() if self._probe_log.exists() else ""

    def _resolve(self, pem_path, enabled_version, region="us-central1-a", overrides=""):
        """Run the resolver over `pem_path` with the KMS probe stubbed.

        The stub records its arguments in a file rather than on stderr: the
        resolver wraps the lookup in `2>/dev/null` -- deliberately, so a re-run
        against a key that is not there yet stays quiet -- and that would
        swallow them. Recording them at all is what makes reading the wrong
        keyring, or skipping the zonal-to-regional reduction, visible here
        instead of only against a live project.
        """
        script = (
            f'{_SOURCE_INSTALLER_COMMON}'
            "kms_key_enabled_version() { "
            f'echo "key=$1 ring=$2 loc=$3 proj=$4" > "{self._probe_log}"; '
            f'echo "{enabled_version}"; '
            "}; "
            f'{overrides}'
            f'PARAM_GITHUB_PEM_PATH="{pem_path}"; '
            # install.sh runs under `set -Eeuo pipefail` with an ERR trap armed
            # at source time, so the call has to sit in an OR list: a bare one
            # that returns 1 aborts this harness with an abort banner before it
            # can report what the resolver did to the path.
            'rc=0; '
            f'resolve_missing_pem_against_kms "{region}" "p1" || rc=$?; '
            'echo "PEM=[$PARAM_GITHUB_PEM_PATH] RC=$rc"; '
            "exit $rc"
        )
        setup = f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n{script}\n'
        return subprocess.run(
            ["bash", "-c", setup],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env)}),
            cwd=str(_REPO_ROOT),
            stdin=subprocess.DEVNULL,
            timeout=60,
        )

    def test_a_pem_that_is_still_on_disk_is_left_alone_without_asking_kms(self):
        """The common case must not spend a KMS round trip, nor rewrite the path."""
        pem = self._tmp / "app.pem"
        pem.write_text("key")
        proc = self._resolve(str(pem), "3")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"PEM=[{pem}] RC=0", proc.stdout)
        self.assertEqual("", self._probe_args(), "an existing file must not be checked against KMS")
        self.assertNotIn("ignoring missing local PEM path", proc.stdout)

    def test_an_empty_pem_path_is_not_treated_as_a_missing_file(self):
        """An install without the minter carries no path, and must not be failed for it."""
        proc = self._resolve("", "")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PEM=[] RC=0", proc.stdout)
        self.assertEqual("", self._probe_args())

    def test_a_missing_pem_is_ignored_when_the_signing_key_has_an_enabled_version(self):
        """The re-run case: the key proves the import already happened.

        The path is cleared rather than kept, because everything downstream in
        step 8 copies it and would otherwise try to import a file that is gone.
        The probe's arguments are asserted too: the keyring and key have to be
        the minter's own, and a zonal --region has to arrive as a KMS location.
        """
        proc = self._resolve("/tmp/deleted-after-import-12345.pem", "3")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PEM=[] RC=0", proc.stdout)
        self.assertIn("already has an ENABLED version (3)", proc.stdout)
        self.assertIn(
            "key=github-token-minter-key ring=github-token-minter-keyring loc=us-central1 proj=p1",
            self._probe_args(),
        )

    def test_a_missing_pem_is_fatal_when_the_signing_key_has_no_enabled_version(self):
        """Nothing to import and nothing imported: say so instead of continuing.

        The apply that follows enables the minter, whose Deployment cannot pass
        readiness without a key, and the composition's helm release waits on it
        -- so proceeding here buys a wedged install rather than a partial one.
        """
        proc = self._resolve("/tmp/never-existed-12345.pem", "")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertIn("GitHub App private key PEM file does not exist", combined)
        self.assertIn("has no ENABLED version, so the import still needs that file", combined)
        self.assertIn("PEM=[/tmp/never-existed-12345.pem] RC=1", proc.stdout)

    def test_an_overridden_keyring_and_key_are_what_kms_is_asked_about(self):
        """--kms-keyring/--kms-key must reach the probe, or the fallback checks the wrong key."""
        proc = self._resolve(
            "/tmp/deleted-after-import-12345.pem",
            "7",
            overrides='PARAM_KMS_KEYRING="custom-ring"; PARAM_KMS_KEY="custom-key"; ',
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("key=custom-key ring=custom-ring ", self._probe_args())
        self.assertIn("Cloud KMS key custom-ring/custom-key already has an ENABLED version (7)", proc.stdout)


class ImportGithubPemKmsKeyTest(unittest.TestCase):
    """The KMS signing key import_github_pem creates for the token minter.

    KMS refuses an import-only key created without
    --skip-initial-version-creation -- `INVALID_ARGUMENT: Import-only keys
    must skip initial version creation` -- which made the minter impossible
    to provision at all. The flag sits mid-way through a five-line wrapped
    invocation, so dropping it again would look like nothing in a diff.
    """

    def _run(self, creates_fail=False, describe_fails=True, go_version=None, git_clone_fails=False):
        """import_github_pem against a stub gcloud that records every call.

        By default the stub reports no ENABLED key version, so the import is
        not short-circuited, and fails `keys describe`, which takes the
        could-not-be-confirmed branch. That branch returns before the Minty
        CLI clone, which is what keeps this a unit test.

        creates_fail makes both `kms … create` calls exit non-zero on stderr,
        the way KMS answers a re-run once the keyring exists. That is the only
        path that exercises the error capture at all, so the default of 0
        leaves it untested -- see the ERR-trap test below.

        describe_fails=False confirms the key instead, which is the only way
        past that branch and into the import itself. go_version and
        git_clone_fails plant the two tools the import shells out to: without
        them the real ones on PATH decide the outcome, and a real `git clone`
        would reach the network. Both stubs are deliberate, not incidental --
        the paths below exist precisely for when those tools disappoint.
        """
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            log = pathlib.Path(tmp) / "gcloud.log"
            pem = pathlib.Path(tmp) / "app.pem"
            pem.write_text("-----BEGIN RSA PRIVATE KEY-----\n")
            create_case = (
                "  *'kms keyrings create'* | *'kms keys create'*)\n"
                "    echo 'ALREADY_EXISTS: it already exists' >&2; exit 1 ;;\n"
                if creates_fail
                else ""
            )
            describe_case = "  *'kms keys describe'*) exit 1 ;;\n" if describe_fails else ""
            gcloud = bin_dir / "gcloud"
            gcloud.write_text(
                "#!/usr/bin/env bash\n"
                f"printf '%s\\n' \"$*\" >> '{log}'\n"
                'case "$*" in\n'
                "  *'kms keys versions list'*) exit 0 ;;\n"
                f"{describe_case}"
                f"{create_case}"
                "esac\n"
                "exit 0\n"
            )
            gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
            if go_version is not None:
                go = bin_dir / "go"
                go.write_text(
                    "#!/usr/bin/env bash\n"
                    f'[ "$1" = version ] && echo "go version go{go_version} linux/amd64" && exit 0\n'
                    "exit 0\n"
                )
                go.chmod(go.stat().st_mode | stat.S_IEXEC)
            if git_clone_fails:
                git = bin_dir / "git"
                git.write_text(
                    "#!/usr/bin/env bash\n"
                    "case \"$*\" in\n"
                    "  *clone*) echo 'fatal: could not read from remote repository' >&2; exit 128 ;;\n"
                    "esac\n"
                    "exit 0\n"
                )
                git.chmod(git.stat().st_mode | stat.S_IEXEC)
            body = (
                f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                f'source "{_INSTALLER_COMMON}"\n'
                "GITOPS_ORG=an-org GITOPS_REPO=a-repo GITHUB_APP_ID=12345 "
                f'GITHUB_PEM_PATH="{pem}" import_github_pem a-project us-central1-a\n'
            )
            proc = subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(bin_dir=str(bin_dir)),
                cwd=str(_REPO_ROOT),
            )
            calls = log.read_text().splitlines() if log.exists() else []
            return proc, calls

    def test_the_import_only_key_is_created_skipping_the_initial_version(self):
        proc, calls = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        creates = [c for c in calls if "kms keys create" in c]
        self.assertEqual(
            len(creates), 1, f"expected exactly one `kms keys create`, got: {calls}"
        )
        create = creates[0]
        for flag in (
            "--skip-initial-version-creation",
            "--import-only",
            "--purpose=asymmetric-signing",
        ):
            self.assertIn(
                flag,
                create,
                f"`gcloud kms keys create` must pass {flag}; KMS rejects an "
                f"import-only key without --skip-initial-version-creation. Call: {create}",
            )

    def test_a_zonal_region_is_reduced_to_the_kms_region(self):
        """KMS locations are regional. The caller passes install.sh's --region,
        which may be a zone."""
        _, calls = self._run()
        creates = [c for c in calls if "kms keys create" in c]
        self.assertIn("--location=us-central1 ", creates[0] + " ", creates)

    def test_a_key_that_cannot_be_confirmed_warns_instead_of_importing(self):
        """The describe assertion, not the create, is what surfaces a failure.

        Without it the run continues to the PEM import and fails two steps
        later against a key that is not there.
        """
        proc, calls = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # install.sh's print_warning / print_info write to stdout.
        self.assertIn("could not be confirmed to exist", proc.stdout)
        self.assertIn("--skip-initial-version-creation", proc.stdout)
        self.assertEqual(
            [c for c in calls if "versions import" in c],
            [],
            "the PEM must not be imported into a key that could not be confirmed",
        )

    def test_a_failing_create_is_reported_without_a_spurious_abort_banner(self):
        """"Already exists" is the expected answer on a re-run, not an abort.

        install.sh:54 installs an ERR trap, and bash 3.2 -- macOS's /bin/bash,
        the curl|bash audience -- runs an inherited ERR trap inside a command
        substitution even when `|| true` handles the failure outside it. Without
        `trap - ERR` in the substitution the ordinary re-run prints on_error's
        fatal banner twice and leaves a FAILED install report behind, while the
        install carries on regardless.
        """
        proc, _ = self._run(creates_fail=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertNotIn(
            "Error encountered",
            combined,
            "a handled `gcloud kms ... create` failure must not fire the ERR trap; "
            "add `trap - ERR` inside the command substitution",
        )
        # The other half of the hunk's purpose: the captured stderr is surfaced
        # rather than discarded, which is what 2>/dev/null used to hide.
        self.assertIn("ALREADY_EXISTS: it already exists", proc.stdout)

    def test_the_manual_import_recipe_is_printed_before_go_is_installed(self):
        """auto_install_tool's exit is a dead end, so the recipe has to come first.

        On a host with neither brew nor apt -- which is exactly where the
        install cannot succeed -- auto_install_tool exhausts its branches and
        ends the run with `exit 1` from inside itself. Nothing after the call
        site runs, so an operator told "install go manually" is told nothing
        about the thing they were installing it for. The `minty tools
        import-pk` invocation is the one output that makes that host
        recoverable: the key can be imported from anywhere, including a
        machine that is not this one.

        Ordering is the assertion, not presence. A recipe printed after the
        call site is a recipe nobody sees.
        """
        with tempfile.TemporaryDirectory() as tmp:
            # No go, no brew, no apt-get, no sudo: the shape of a host
            # auto_install_tool has no route on. PATH is replaced rather than
            # prepended, so a developer's own Go toolchain cannot satisfy the
            # `command -v go` this test needs to fail.
            bin_dir = create_minimal_tools_bin(tmp)
            pem = pathlib.Path(tmp) / "app.pem"
            pem.write_text("-----BEGIN RSA PRIVATE KEY-----\n")
            gcloud = bin_dir / "gcloud"
            # Reports no ENABLED key version, so the import is not skipped.
            gcloud.write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                "  *'kms keys versions list'*) exit 0 ;;\n"
                "esac\n"
                "exit 0\n"
            )
            gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
            body = (
                f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                f'source "{_INSTALLER_COMMON}"\n'
                # Non-interactive, because the interactive arm of
                # auto_install_tool prompts on /dev/tty and would hang here.
                # Its answer is the same "y" that arm assumes.
                "PARAM_NON_INTERACTIVE=true GITOPS_ORG=an-org GITOPS_REPO=a-repo "
                f'GITHUB_APP_ID=12345 GITHUB_PEM_PATH="{pem}" '
                "import_github_pem a-project us-central1-a\n"
            )
            proc = subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=120,
                env=get_isolated_test_env(overrides={"PATH": str(bin_dir)}),
                cwd=str(_REPO_ROOT),
            )

        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, f"the run must stop here:\n{combined}")
        dead_end = proc.stdout.find("Tool 'go' is still missing")
        self.assertNotEqual(
            dead_end, -1, f"expected auto_install_tool to give up:\n{combined}"
        )
        recipe = proc.stdout.find("import the key by hand instead:")
        self.assertNotEqual(
            recipe,
            -1,
            "a host that cannot install Go must still be told how to import the "
            f"key: print the recipe before auto_install_tool.\n{combined}",
        )
        self.assertLess(
            recipe,
            dead_end,
            "the recipe is printed after the call that ends the run, so it never "
            f"reaches the operator.\n{combined}",
        )
        # The recipe has to be runnable: the real path, not the placeholder.
        self.assertIn("tools import-pk", proc.stdout)
        self.assertIn(f"-private-key=@{pem}", proc.stdout)
        self.assertNotIn("-private-key=@<path-to-pem>", proc.stdout)

    def test_a_go_too_old_for_the_cli_stops_the_import_instead_of_building(self):
        """The floor has to end the import, not warn and carry on.

        `apt-get install golang-go` leaves Go 1.19 behind on Debian 12 and
        Ubuntu 22.04, and auto_install_tool judges success by `command -v`,
        which such a toolchain answers. Continuing spends `retry 6 5` on a
        build that cannot satisfy the CLI's go.mod and then advises retrying
        the same command by hand.

        Asserted on the KMS calls too: returning here has to happen before the
        keyring and key are created, so a host that cannot import is not left
        holding an empty import-only key.
        """
        proc, calls = self._run(go_version="1.19.8")
        self.assertEqual(proc.returncode, 1, f"the import must fail:\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("too old to build the Minty CLI", proc.stdout + proc.stderr)
        self.assertEqual(
            [],
            [c for c in calls if "kms keyrings create" in c or "kms keys create" in c],
            f"the floor must stop the import before it creates anything:\n{calls}",
        )

    def test_a_failed_minty_import_is_reported_as_a_failure(self):
        """A key that exists but holds nothing is the wedge this return prevents.

        main() turns this into `exit 1` before `terraform apply`, which is the
        point: the apply enables the minter, whose Deployment cannot pass
        readiness without an imported key, and the composition's helm release
        waits on every Deployment. Returning 0 here buys a hung apply instead
        of a named failure.

        The clone is stubbed to fail rather than the CLI itself -- the `go run`
        below it is wrapped in `retry 6 5`, so failing there would cost the
        suite half a minute of sleeps to reach the same branch.
        """
        proc, _ = self._run(describe_fails=False, go_version="1.22.0", git_clone_fails=True)
        self.assertEqual(proc.returncode, 1, f"the import must fail:\n{proc.stdout}\n{proc.stderr}")
        combined = proc.stdout + proc.stderr
        self.assertIn("PEM import failed", combined)
        # The operator is left with a runnable command, not the placeholder.
        self.assertIn("tools import-pk", combined)
        self.assertNotIn("-private-key=@<path-to-pem>", combined)

    def test_main_turns_a_failed_import_into_a_hard_exit(self):
        """The two returns above are only fatal because main() makes them so.

        Reaching that call costs a live project and a cluster, so this pins the
        wiring rather than executing it: a bare `import_github_pem …` would let
        an install whose key never reached KMS walk into the apply, and the two
        tests above would still pass while it did.
        """
        body = _INSTALL_SH.read_text()
        self.assertIn(
            'import_github_pem "$project_id" "$region" || exit 1',
            body,
            "a failed PEM import must stop the run before terraform apply",
        )


class InstallEnvIsCreatedInTheCheckoutTest(unittest.TestCase):
    """The configuration file has to land where every other front door looks.

    Under `curl … | bash` -- Method 0 in INSTALL.md, the documented fastest
    install -- ${BASH_SOURCE[0]} names no file, so a script-relative path
    resolves to whatever directory the operator was standing in.
    acquire_source_repo then clones to $HOME/kube-agents and cd's there, while
    every other reader resolves ${repo_dir}/install.env
    (default_install_env_file). Freezing the invocation directory dropped the
    whole configuration -- API_SERVER_KEY and the plaintext model keys included
    -- somewhere no later run would look: upgrade.sh hit its fail-closed
    branch, and a re-run of the one-liner rebuilt every PARAM_* from defaults,
    which is the #1060 class this change exists to close.
    """

    def _resolved_paths(self, cwd, home, extra_env=None):
        """What install.sh picks for install.env and the legacy vars.sh.

        Piped into `bash -s` rather than sourced by path, because that is the
        whole point: `source /abs/path/install.sh` sets BASH_SOURCE and the
        script can see where it lives, while `curl … | bash` leaves the array
        empty and `${BASH_SOURCE[0]:-.}` collapses to the working directory.
        Sourcing by path here would exercise the one case that never had the
        bug.
        """
        overrides = {"HOME": str(home), "KUBE_AGENTS_SOURCE_ONLY": "true"}
        overrides.update(extra_env or {})
        # KUBE_AGENTS_INSTALL_ENV is what get_isolated_test_env normally pins;
        # these cases are about the fallback that runs when it is unset.
        full_env = get_isolated_test_env(overrides=overrides)
        if "KUBE_AGENTS_INSTALL_ENV" not in (extra_env or {}):
            full_env.pop("KUBE_AGENTS_INSTALL_ENV", None)
        script = _INSTALL_SH.read_text() + (
            '\necho "ENV=$INSTALL_ENV_FILE"\necho "LEGACY=$LEGACY_VARS_FILE"\n'
        )
        return subprocess.run(
            ["bash", "-s"], input=script,
            capture_output=True, text=True, env=full_env, cwd=str(cwd),
        )

    def test_a_checkout_run_uses_the_checkout(self):
        """The ordinary `./install.sh` case, unchanged: the script sits in a
        checkout, so that checkout is where the file belongs."""
        with tempfile.TemporaryDirectory() as home:
            proc = self._resolved_paths(_REPO_ROOT, home)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(f"ENV={_REPO_ROOT}/install.env", proc.stdout)

    def test_a_piped_run_from_elsewhere_uses_the_clone_not_the_cwd(self):
        """Standing in a directory that is not a checkout, with no install.env to
        hand, the file must be destined for the clone acquire_source_repo will
        make -- not for the cwd."""
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home:
            proc = self._resolved_paths(tmp, home)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(f"ENV={home}/kube-agents/install.env", proc.stdout)
            self.assertNotIn(f"ENV={tmp}/install.env", proc.stdout)

    def test_an_install_env_the_operator_placed_still_wins(self):
        """Backwards compatibility: putting the file in the directory you run
        from is a deliberate act and keeps working."""
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home:
            (pathlib.Path(tmp) / "install.env").write_text("PROJECT_ID=from-the-cwd\n")
            proc = self._resolved_paths(tmp, home)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            # realpath: this path comes back through `pwd`, and on macOS the
            # temporary directory is /var/... symlinked to /private/var/...
            resolved = pathlib.Path(tmp).resolve()
            self.assertIn(f"ENV={resolved}/install.env", proc.stdout)

    def test_the_explicit_override_still_wins(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home:
            named = pathlib.Path(tmp) / "named.env"
            named.write_text("PROJECT_ID=from-the-override\n")
            proc = self._resolved_paths(
                tmp, home, extra_env={"KUBE_AGENTS_INSTALL_ENV": str(named)}
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(f"ENV={named}", proc.stdout)

    def test_the_legacy_vars_file_is_looked_for_in_the_same_checkout(self):
        """Same root cause, same fix: resolved script-relative, a piped re-run
        against an existing clone never found the legacy file and silently
        skipped the migration it exists for."""
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home:
            legacy = pathlib.Path(home) / "kube-agents" / "k8s-operator" / "scripts"
            legacy.mkdir(parents=True)
            (legacy / "vars.sh").write_text("export PROJECT_ID=from-the-legacy-file\n")
            proc = self._resolved_paths(tmp, home)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(f"LEGACY={legacy}/vars.sh", proc.stdout)


class ServiceAccountOwnershipIsCheckedOnEveryApplyDoorTest(unittest.TestCase):
    """The 409 check has to sit between the generator and each apply, and
    before the dry-run exit and the confirmation on the main path (#1294)."""

    def setUp(self):
        self.source = _INSTALL_SH.read_text()

    def test_the_main_path_checks_after_the_generator_and_before_the_summary(self):
        generator = self.source.index('write_tfvars_from_state "$tfvars_file" "$image_tag"')
        check = self.source.index("check_service_account_ownership || exit 1", generator)
        summary = self.source.index('print_step "11. Pre-Flight Configuration Summary"')
        self.assertLess(generator, check)
        self.assertLess(check, summary)

    def test_the_day2_menu_checks_before_its_re_apply(self):
        menu_generator = self.source.index(
            'write_tfvars_from_state "$(tf_compose_dir "$repo_dir")/terraform.tfvars" "$image_tag"')
        check = self.source.index("check_service_account_ownership || exit 1", menu_generator)
        apply = self.source.index("run_lifecycle_apply", menu_generator)
        self.assertLess(menu_generator, check)
        self.assertLess(check, apply)


class FailedInitialReleaseIsClearedBeforeTheApplyTest(unittest.TestCase):
    """A retry after an apply that died inside the kube-agents release.

    Helm refuses to create a release whose name a failed one still holds, so
    the main path clears that one case -- on an existing cluster only, right
    before the apply -- and treats a failure to clear it as a stop.
    """

    def setUp(self):
        self.source = (_REPO_ROOT / "install.sh").read_text()

    def test_the_main_path_clears_it_after_the_cluster_steps_and_before_the_apply(self):
        cmek = self.source.index('ensure_existing_cluster_cmek "$project_id" "$cluster_name" "$region"')
        clear = self.source.index(
            'clear_failed_initial_helm_release "$KUBE_AGENTS_HELM_RELEASE" '
            '"${NAMESPACE:-$DEFAULT_NAMESPACE}" || exit 1', cmek)
        apply = self.source.index('run_lifecycle_apply "$repo_dir" "$provisioning_log"', cmek)
        self.assertLess(cmek, clear)
        self.assertLess(clear, apply)

    def test_it_is_gated_on_the_cluster_existing_and_fetches_its_credentials(self):
        # Existing, not adopted: a cluster this state created on the attempt
        # that died exists with create_cluster = true, and its retry hits the
        # same Helm refusal. The generator fetched credentials on the adoption
        # path alone, so this branch fetches them itself.
        clear = self.source.index('clear_failed_initial_helm_release "$KUBE_AGENTS_HELM_RELEASE"')
        gate = self.source.rfind('if [ "${TFVARS_CLUSTER_EXISTS:-false}" = "true" ]; then', 0, clear)
        self.assertGreater(gate, 0)
        credentials = self.source.index('gcloud container clusters get-credentials "$cluster_name"', gate)
        self.assertLess(credentials, clear)
        # Nothing else opens between the gate and the call.
        self.assertNotIn("\n  fi\n", self.source[gate:clear])
        # The fetch reaches a DNS-endpoint-only cluster the way step 13's does;
        # a plain one fails there, and the context gate then skips the check.
        flag = self.source.index('gke_dns_endpoint_flag "$cluster_name" "$region" "$project_id"', gate)
        self.assertLess(flag, credentials)
        self.assertIn("$GKE_DNS_ENDPOINT_FLAG", self.source[credentials:clear])


class TheCloneDirectoryNeedsHomeOnlyWhenCloningTest(unittest.TestCase):
    """HOME is unset in some service environments (a systemd system unit, a
    container with no passwd entry). A run from a checkout never clones, so it
    must not need HOME at all under `set -u`; a run that does clone says what
    it needed."""

    def _run_without_home(self, tail):
        with tempfile.TemporaryDirectory() as tmp:
            empty_env = pathlib.Path(tmp) / "install.env"
            empty_env.write_text("")
            env = get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": str(empty_env)})
            env.pop("HOME", None)
            return subprocess.run(
                ["bash", "-c", f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n{tail}'],
                capture_output=True, text=True, env=env, cwd=str(_REPO_ROOT),
            )

    def test_a_checkout_run_sources_without_home(self):
        proc = self._run_without_home('echo sourced')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("sourced", proc.stdout)
        self.assertNotIn("HOME", proc.stderr)

    def test_the_clone_directory_names_home_when_it_is_missing(self):
        proc = self._run_without_home('kube_agents_clone_dir')
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("HOME", proc.stderr)

    def test_the_clone_directory_is_under_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty_env = pathlib.Path(tmp) / "install.env"
            empty_env.write_text("")
            env = get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": str(empty_env), "HOME": "/h"})
            proc = subprocess.run(
                ["bash", "-c", f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\nkube_agents_clone_dir'],
                capture_output=True, text=True, env=env, cwd=str(_REPO_ROOT),
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "/h/kube-agents")


class TheMinterCliSourceIsSpelledOnceTest(unittest.TestCase):
    """The Minty CLI's repository and the manual recipe's clone directory are
    named at the top of install.sh; the two lines that use them read the names."""

    def test_the_repository_and_clone_directory_appear_only_as_constants(self):
        text = (_REPO_ROOT / "install.sh").read_text()
        for literal, constant in (("abcxyz/github-token-minter.git", "MINTY_CLI_REPO_URL="),
                                  ("/tmp/minty", "MINTY_CLI_MANUAL_CLONE_DIR=")):
            with self.subTest(literal=literal):
                inline = [line for line in text.splitlines()
                          if literal in line and not line.startswith(constant)]
                self.assertEqual(inline, [], f"name {literal} through {constant}")


class ShellNamespaceNeverReachesTheGeneratorTest(unittest.TestCase):
    """NAMESPACE is a name kubectl tooling exports; only install.env may set it."""

    def _namespace_after_load(self, contents):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = pathlib.Path(tmp) / "install.env"
            env_file.write_text(contents)
            env_file.chmod(0o600)
            proc = subprocess.run(
                ["bash", "-c",
                 f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                 'echo "NS=${NAMESPACE:-unset}"'],
                capture_output=True, text=True,
                env=get_isolated_test_env(overrides={
                    "KUBE_AGENTS_INSTALL_ENV": str(env_file),
                    "NAMESPACE": "stray-from-kubectl-tooling",
                }),
                cwd=str(_REPO_ROOT),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return proc.stdout

    def test_a_shell_export_is_dropped(self):
        self.assertIn("NS=unset", self._namespace_after_load("PROJECT_ID=a-project\n"))

    def test_the_file_still_sets_it(self):
        self.assertIn("NS=from-the-file",
                      self._namespace_after_load("PROJECT_ID=a-project\nNAMESPACE=from-the-file\n"))


class BootstrapRecordsIdentityKeysOnlyWhenSetTest(unittest.TestCase):
    """The GSA and CMEK names are recorded in a new install.env only when the
    run set them. A default copied in would freeze at this release; a custom
    name dropped would rename -- replace -- the account on the next run. And
    NAMESPACE is never recorded from the environment: kubectl tooling exports
    that name, and freezing a stray value would move the release.
    """

    def _bootstrap(self, env, loaded_content=""):
        with tempfile.TemporaryDirectory() as tmp:
            dest = pathlib.Path(tmp) / "new.install.env"
            # An existing, empty input: install.sh refuses a KUBE_AGENTS_INSTALL_ENV
            # that names a missing file, and the point here is the file it
            # CREATES, not the one it loads.
            loaded = pathlib.Path(tmp) / "loaded.install.env"
            loaded.write_text(loaded_content)
            loaded.chmod(0o600)
            proc = subprocess.run(
                ["bash", "-c",
                 f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                 'source scripts/installer/installer_common.sh\n'
                 'resolve_shared_defaults\n'
                 'PARAM_DRY_RUN=false; PARAM_MEMORY=file\n'
                 f'bootstrap_install_env_file "{dest}" some-tag >/dev/null\n'
                 f'cat "{dest}"'],
                capture_output=True, text=True,
                env=get_isolated_test_env(overrides={
                    "KUBE_AGENTS_INSTALL_ENV": str(loaded),
                    "KUBE_AGENTS_LEGACY_VARS_FILE": "",
                    "PROJECT_ID": "p", "CLUSTER_NAME": "c", "REGION": "us-central1",
                    **env,
                }),
                cwd=str(_REPO_ROOT),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return proc.stdout

    def test_a_configured_name_is_recorded(self):
        out = self._bootstrap({"PLATFORM_AGENT_GSA_NAME": "agent-two-gsa",
                               "GKE_DB_KMS_KEYRING": "ring-two"})
        self.assertIn("PLATFORM_AGENT_GSA_NAME=agent-two-gsa\n", out)
        self.assertIn("GKE_DB_KMS_KEYRING=ring-two\n", out)

    def test_an_unset_name_is_not_frozen_as_a_default(self):
        out = self._bootstrap({})
        for key in ("PLATFORM_AGENT_GSA_NAME", "GITHUB_MINTER_GSA_NAME", "LITELLM_GSA_NAME",
                    "GKE_DB_KMS_KEYRING", "GKE_DB_KMS_KEY", "NAMESPACE"):
            with self.subTest(key=key):
                # re.MULTILINE, or `^` anchors at offset 0 only -- which is the
                # file's comment header, so the assertion could never fail.
                self.assertNotRegex(out, re.compile(rf"^{key}=", re.MULTILINE), msg=out)

    def test_a_shell_exported_namespace_is_not_recorded(self):
        out = self._bootstrap({"NAMESPACE": "stray-from-kubectl-tooling"})
        self.assertNotRegex(out, re.compile(r"^NAMESPACE=", re.MULTILINE), msg=out)

    def test_the_negative_assertions_can_fail(self):
        """The guard the two tests above rely on: a key that IS written is
        seen by the same anchored pattern, so their silence means absence."""
        out = self._bootstrap({"GKE_DB_KMS_KEY": "key-two"})
        self.assertRegex(out, re.compile(r"^GKE_DB_KMS_KEY=key-two$", re.MULTILINE))

    def test_the_capacity_bypass_is_not_recorded_but_the_timeout_is(self):
        """A bypass describes one run, not the install.

        The run that reaches for --skip-capacity-check is the one on an
        undersized cluster, and this file is written once and read by every
        run after it: recorded here, the bypass would silently skip the
        preflight for the upgrade that adds hindsight-api. HELM_TIMEOUT beside
        it describes what the install IS, and stays.
        """
        out = self._bootstrap({"SKIP_CAPACITY_CHECK": "true"}, loaded_content="HELM_TIMEOUT=720\n")
        self.assertNotRegex(out, re.compile(r"^SKIP_CAPACITY_CHECK=", re.MULTILINE), msg=out)
        self.assertRegex(out, re.compile(r"^HELM_TIMEOUT=720$", re.MULTILINE), msg=out)

    def test_a_shell_exported_helm_timeout_is_not_recorded(self):
        out = self._bootstrap({"HELM_TIMEOUT": "720"})
        self.assertNotRegex(out, re.compile(r"^HELM_TIMEOUT=720$", re.MULTILINE), msg=out)
        self.assertRegex(out, re.compile(r"^HELM_TIMEOUT=600$", re.MULTILINE), msg=out)


class FrontDoorsAgreeOnTheRepositoryTest(unittest.TestCase):
    """Each front door clones the install sources before it has a checkout to
    read the URL from, so each carries the URL; this pins the three equal."""

    def test_every_front_door_names_the_same_clone_url(self):
        urls = {}
        for script in ("install.sh", "upgrade.sh", "uninstall.sh"):
            match = re.search(r'^KUBE_AGENTS_REPO_URL="([^"]+)"$',
                              (_REPO_ROOT / script).read_text(), re.MULTILINE)
            self.assertIsNotNone(match, f"{script} declares no KUBE_AGENTS_REPO_URL")
            urls[script] = match.group(1)
        self.assertEqual(len(set(urls.values())), 1, urls)

    def test_no_front_door_spells_the_url_inline(self):
        for script in ("install.sh", "upgrade.sh", "uninstall.sh"):
            with self.subTest(script=script):
                text = (_REPO_ROOT / script).read_text()
                inline = [line for line in text.splitlines()
                          if "github.com/gke-labs/kube-agents.git" in line
                          and not line.startswith("KUBE_AGENTS_REPO_URL=")]
                self.assertEqual(inline, [], "clone through $KUBE_AGENTS_REPO_URL")


class InstallEnvPermissionsTest(unittest.TestCase):
    """A copied install.env is a credential file at the operator's umask.

    install.env.example is tracked 100644 and the documented way to create the
    real file is to copy it, so a stock umask 022 yields 0644 -- and that file
    is where GEMINI_API_KEY, SLACK_BOT_TOKEN and API_SERVER_KEY end up. Nothing
    else reaches it: bootstrap_install_env_file returns the moment the
    destination exists, so its chmod 600 never runs, and save_env_var's is
    reachable only from the Day-2 menu. INSTALL.md meanwhile states flatly that
    the file is 0600, and the predecessor vars.sh always was.
    """

    def _load(self, mode):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = pathlib.Path(tmp) / "install.env"
            env_file.write_text("PROJECT_ID=a-project\n")
            env_file.chmod(mode)
            proc = subprocess.run(
                ["bash", "-c",
                 f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                 'echo "P=$PROJECT_ID"'],
                capture_output=True, text=True,
                env=get_isolated_test_env(
                    overrides={"KUBE_AGENTS_INSTALL_ENV": str(env_file)}
                ),
                cwd=str(_REPO_ROOT),
            )
            return proc, stat.S_IMODE(env_file.stat().st_mode)

    def test_a_world_readable_configuration_is_tightened_on_load(self):
        proc, mode = self._load(0o644)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(0o600, mode, "install.sh must chmod 600 a 0644 install.env")
        self.assertIn("P=a-project", proc.stdout, "and still load it")
        self.assertIn("Tightened permissions", proc.stdout + proc.stderr)

    def test_a_group_readable_configuration_is_tightened_too(self):
        _, mode = self._load(0o640)
        self.assertEqual(0o600, mode)

    def test_an_already_private_file_is_left_alone_and_unannounced(self):
        proc, mode = self._load(0o600)
        self.assertEqual(0o600, mode)
        self.assertNotIn("Tightened permissions", proc.stdout + proc.stderr)

    def test_the_copy_recipe_tells_the_operator_to_chmod_it(self):
        """The tightening only helps from the next run onwards, so the recipe
        that creates the file has to say so itself."""
        example = (_REPO_ROOT / "install.env.example").read_text()
        self.assertIn("cp install.env.example install.env", example)
        self.assertIn("chmod 600 install.env", example)
        self.assertIn("chmod 600", (_REPO_ROOT / "INSTALL.md").read_text())


class ChatInterviewInheritsAndStillAsksTest(unittest.TestCase):
    """Inheriting the chat setting must pre-select the menu, not skip it.

    PARAM_ENABLE_GOOGLE_CHAT is now seeded from GOOGLE_CHAT_ENABLED, but the
    gate around the menu was still the one that decides whether to run the
    interview at all -- so it stopped distinguishing "asked for on this run"
    from "inherited from the file". An interactive re-run against a configured
    install never saw the four options, leaving no way to turn Chat off or to
    add Slack. Every other setting reworked here seeds its choice variable and
    still calls prompt_menu, whose default_choice exists for exactly this.
    """

    _SOURCE = _INSTALL_SH.read_text()

    def _chat_block(self):
        block = self._SOURCE.split("6. Chat & Messaging Platform Integration")[1]
        return block.split("local google_chat_enabled")[0]

    def test_the_menu_is_not_inside_the_inheritance_branch(self):
        """prompt_menu for the chat options must be reached unconditionally;
        the seeds above it only pre-select an answer.

        Checked structurally rather than by searching the text before the call.
        Slicing at `chat_block.index("prompt_menu")` cannot work: the slice
        stops at the first occurrence, so by construction it never contains the
        string the pattern needs, and the first occurrence here is the comment
        naming prompt_menu rather than the call. The property that actually
        distinguishes fixed from broken is nesting depth -- the defect had the
        call at four spaces inside an `else` arm, the fix has it at two, in the
        function body.
        """
        code = [
            line for line in self._chat_block().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        calls = [line for line in code if re.match(r"^\s*prompt_menu\b", line)]
        self.assertEqual(
            1, len(calls),
            "expected exactly one chat prompt_menu call in the block; "
            f"found {len(calls)}",
        )
        indent = len(calls[0]) - len(calls[0].lstrip())
        self.assertEqual(
            2, indent,
            "the chat menu must sit at function-body level, not nested in an "
            "if/else arm; seed chat_choice and let prompt_menu default to it",
        )
        previous = code[code.index(calls[0]) - 1].strip()
        self.assertNotEqual(
            "else", previous,
            "the chat menu must not be the else-arm of the inheritance check",
        )

    def test_all_four_options_are_still_offered(self):
        for option in ("Google Chat (Pub/Sub", "Slack (Socket Mode",
                       "Both Google Chat and Slack", "None (CLI & REST"):
            with self.subTest(option=option):
                self.assertIn(option, self._SOURCE)

    def test_a_configured_install_pre_selects_its_current_integration(self):
        """The seeds, which are what makes enter a no-op rather than a
        change."""
        chat_block = self._chat_block()
        self.assertIn('chat_choice="3"', chat_block)
        self.assertIn('chat_choice="1"', chat_block)
        self.assertIn('chat_choice="2"', chat_block)
        # And "None" is still what a non-interactive run with nothing
        # configured gets, rather than option 1.
        self.assertIn('chat_choice="${chat_choice:-4}"', chat_block)


class SlackPromptsKeepTheirCurrentValuesTest(unittest.TestCase):
    """Pressing enter through the Slack interview must not clear the install.

    prompt_read keeps a non-empty current value on the non-interactive path,
    but the interactive branch applies the default argument, and
    `[ -z "$input_val" ] && [ -n "$default_val" ]` is false when that default
    is empty -- so it falls through and assigns the empty string. Passing a
    bare "" therefore cleared SLACK_BOT_TOKEN, SLACK_APP_TOKEN,
    SLACK_HOME_CHANNEL and SLACK_HOME_CHANNEL_NAME, and replaced the Slack
    allowlist with the Google Chat one. The tokens are usually rescued by the
    Secret-recovery loop; the allowlist is not, and an empty slack_allowed_users
    means every workspace member may talk to the agent.

    This is the defect the change already fixed one screen lower, for the
    GitOps prompts, and left in place here.
    """

    _SOURCE = _INSTALL_SH.read_text()

    @staticmethod
    def _logical_lines(source):
        """Join backslash continuations, so a wrapped call is one line.

        Without this the scan below is vacuous for any prompt whose call is
        wrapped: the matched physical line ends in `\\` rather than in the
        argument, so a pattern anchored at end-of-line can never fire. Two of
        the four prompts named here are wrapped.
        """
        joined, buffer = [], ""
        for line in source.splitlines():
            buffer += line.rstrip("\\") if line.rstrip().endswith("\\") else line
            if not line.rstrip().endswith("\\"):
                joined.append(buffer)
                buffer = ""
        if buffer:
            joined.append(buffer)
        return joined

    def test_no_slack_prompt_passes_a_bare_empty_default(self):
        lines = self._logical_lines(self._SOURCE)
        for prompt in ("Slack Bot Token", "Slack App Token",
                       "Slack Home Channel ID", "Slack Home Channel Name"):
            with self.subTest(prompt=prompt):
                matched = [
                    line for line in lines
                    if prompt in line and "prompt_read" in line
                ]
                self.assertTrue(
                    matched,
                    f"no prompt_read call found for {prompt}; this scan would "
                    "otherwise pass by matching nothing",
                )
                for line in matched:
                    self.assertNotRegex(
                        line,
                        re.compile(r'"\s*"\s*(true|false)?\s*$'),
                        f"{prompt} passes an empty default, which clears it "
                        "when the operator presses enter",
                    )

    def test_each_slack_prompt_defaults_to_its_own_current_value(self):
        for var in ("slack_bot_token", "slack_app_token", "slack_allowed_users",
                    "slack_home_channel", "slack_home_channel_name"):
            with self.subTest(var=var):
                self.assertRegex(
                    self._SOURCE,
                    re.compile(rf'{var} "\${var}"'),
                    f"{var} must be prompted with itself as the default",
                )

    def test_the_slack_allowlist_is_not_seeded_from_the_chat_allowlist(self):
        """They are different lists for different platforms; arm 3 configures
        both at once and used the Chat one for Slack."""
        self.assertNotIn('slack_allowed_users "$allowed_users"', self._SOURCE)

    def test_the_tokens_are_not_echoed_back_as_a_visible_default(self):
        """prompt_read renders the default into the prompt text, so a secret
        passed as one would be printed. The label argument is what avoids it."""
        for var in ("slack_bot_token", "slack_app_token"):
            with self.subTest(var=var):
                self.assertRegex(
                    self._SOURCE,
                    re.compile(rf'{var} "\${var}" true "\$\w+_hint"'),
                    f"{var} must pass a label so the value is not displayed",
                )

    def test_both_arms_share_one_definition(self):
        """One helper, called by both arms that ask, so the two cannot drift."""
        self.assertEqual(
            1, self._SOURCE.count("_prompt_slack_settings() {"),
            "the Slack prompts must be defined exactly once",
        )
        self.assertEqual(
            2, len(re.findall(r'^\s*_prompt_slack_settings\s*$',
                              self._SOURCE, re.MULTILINE)),
            "both the Slack-only and the Both arms must call it",
        )

    def test_each_google_chat_prompt_defaults_to_its_own_current_value(self):
        for var in ("allowed_users", "chat_topic_name", "chat_sub_name", "google_chat_home_channel"):
            with self.subTest(var=var):
                self.assertRegex(
                    self._SOURCE,
                    re.compile(rf'{var} "\${var}"'),
                    f"{var} must be prompted with itself as the default",
                )

    def test_both_chat_arms_share_google_chat_definition(self):
        self.assertEqual(
            1, self._SOURCE.count("_prompt_google_chat_settings() {"),
            "the Google Chat prompts must be defined exactly once",
        )
        self.assertEqual(
            2, len(re.findall(r'^\s*_prompt_google_chat_settings\s*$',
                              self._SOURCE, re.MULTILINE)),
            "both the Google-Chat-only and the Both arms must call it",
        )


class ChatBooleansAreReadThroughIsTruthyTest(unittest.TestCase):
    """`install.env` is hand-authored, so its booleans arrive in any spelling.

    Every boolean the generator writes goes through `hcl_bool` -> `is_truthy`,
    which accepts `True`, `yes`, `y`, `1`, `on`. These two never reached it on
    install.sh's path: the chat gate string-compared against the lowercase
    literal. `GOOGLE_CHAT_ENABLED=True` therefore dropped `chat_choice` to 4 and
    planned the Pub/Sub topic away on the next `-y` run, while `upgrade.sh` read
    the same file as enabled — two front doors disagreeing about one file. The
    sibling booleans fail loudly on their `^(true|false)$` validators instead;
    only these two were silent.
    """

    def _chat_choice(self, contents):
        """The chat option install.sh resolves for a given install.env."""
        with tempfile.TemporaryDirectory() as tmp:
            env_file = pathlib.Path(tmp) / "install.env"
            env_file.write_text(contents)
            env_file.chmod(0o600)
            return subprocess.run(
                ["bash", "-c",
                 f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                 'source scripts/installer/installer_common.sh\n'
                 'resolve_shared_defaults\n'
                 'c=""\n'
                 'if is_truthy "$PARAM_ENABLE_GOOGLE_CHAT" && is_truthy "${SLACK_ENABLED:-false}"; then c=3\n'
                 'elif is_truthy "$PARAM_ENABLE_GOOGLE_CHAT"; then c=1\n'
                 'elif is_truthy "${SLACK_ENABLED:-false}"; then c=2\n'
                 'fi\n'
                 'echo "C=${c:-4}"'],
                capture_output=True, text=True,
                env=get_isolated_test_env(
                    overrides={"KUBE_AGENTS_INSTALL_ENV": str(env_file)}
                ),
                cwd=str(_REPO_ROOT),
            )

    def test_the_gate_does_not_string_compare_against_the_lowercase_literal(self):
        """The source-level guard. A reintroduced `= "true"` here is the bug,
        and it is invisible to the behavioural cases below on a `true` file."""
        block = self._SOURCE_BLOCK()
        self.assertNotIn('"$PARAM_ENABLE_GOOGLE_CHAT" = "true"', block)
        self.assertNotIn('"${SLACK_ENABLED:-$DEFAULT_SLACK_ENABLED}" = "true"', block)
        self.assertIn('is_truthy "$PARAM_ENABLE_GOOGLE_CHAT"', block)
        self.assertIn('is_truthy "${SLACK_ENABLED:-$DEFAULT_SLACK_ENABLED}"', block)

    def _SOURCE_BLOCK(self):
        source = _INSTALL_SH.read_text()
        block = source.split("6. Chat & Messaging Platform Integration")[1]
        return block.split("local google_chat_enabled")[0]

    def test_every_truthy_spelling_enables_chat(self):
        for spelling in ("true", "True", "TRUE", "yes", "y", "1", "on", "On"):
            with self.subTest(spelling=spelling):
                proc = self._chat_choice(f"GOOGLE_CHAT_ENABLED={spelling}\n")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn(
                    "C=1", proc.stdout,
                    f"GOOGLE_CHAT_ENABLED={spelling} must enable Google Chat; "
                    "resolving to None plans the Pub/Sub topic away",
                )

    def test_falsy_spellings_still_mean_off(self):
        for spelling in ("false", "False", "no", "0", "off", ""):
            with self.subTest(spelling=spelling):
                proc = self._chat_choice(f"GOOGLE_CHAT_ENABLED={spelling}\n")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("C=4", proc.stdout)

    def test_slack_reads_the_same_way(self):
        for spelling in ("True", "yes", "1"):
            with self.subTest(spelling=spelling):
                proc = self._chat_choice(f"SLACK_ENABLED={spelling}\n")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("C=2", proc.stdout)


class UnrecordedInterviewAnswersAreReportedTest(unittest.TestCase):
    """An interactive answer that `install.env` does not record must be named.

    `install.env` is an input the installer never rewrites, but the interview
    still runs on every interactive invocation and its answers reach
    `terraform.tfvars` and the cluster. So answering "None" at the chat menu
    destroys the Pub/Sub topic on this apply and the next run puts it back,
    because the file still says the integration is on. The only signal was
    "Left your install configuration as you wrote it", which reads as
    reassurance. This warns instead, naming each key and the line to paste.
    """

    def _warn(self, recorded, env_overrides, non_interactive=False):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = pathlib.Path(tmp) / "install.env"
            env_file.write_text(recorded)
            env_file.chmod(0o600)
            assignments = "\n".join(
                f'export {k}={v!r}' .replace("'", '"')
                for k, v in env_overrides.items()
            )
            ni = "true" if non_interactive else "false"
            return subprocess.run(
                ["bash", "-c",
                 f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                 f'PARAM_NON_INTERACTIVE={ni}\n'
                 'PARAM_DRY_RUN=false\n'
                 'has_controlling_tty() { return 0; }\n'
                 f'{assignments}\n'
                 f'warn_unrecorded_interview_answers "{env_file}"'],
                capture_output=True, text=True,
                env=get_isolated_test_env(
                    overrides={"KUBE_AGENTS_INSTALL_ENV": str(env_file)}
                ),
                cwd=str(_REPO_ROOT),
            )

    def test_a_changed_chat_answer_is_named(self):
        proc = self._warn(
            "GOOGLE_CHAT_ENABLED=true\n", {"GOOGLE_CHAT_ENABLED": "false"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertIn("does not record", combined)
        self.assertIn("GOOGLE_CHAT_ENABLED=false", combined)

    def test_an_unchanged_answer_says_nothing(self):
        proc = self._warn(
            "GOOGLE_CHAT_ENABLED=true\n", {"GOOGLE_CHAT_ENABLED": "true"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("does not record", proc.stdout + proc.stderr)

    def test_a_non_interactive_run_says_nothing(self):
        """It typed nothing: its answers came from flags and this very file."""
        proc = self._warn(
            "GOOGLE_CHAT_ENABLED=true\n", {"GOOGLE_CHAT_ENABLED": "false"},
            non_interactive=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("does not record", proc.stdout + proc.stderr)

    def test_a_key_the_file_does_not_carry_is_not_reported(self):
        """Absent is not drift — the file inherits the default, and warning
        about every unset key would bury the ones that matter."""
        proc = self._warn("PROJECT_ID=a-project\n", {"MEMORY": "hindsight"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("does not record", proc.stdout + proc.stderr)

    def test_a_quoted_empty_value_is_not_drift(self):
        """`write_env_var` serialises with `%q`, which spells the empty string
        as the two-character literal `''`.

        `bootstrap_install_env_file` writes seven keys unconditionally, and on a
        stock install — no Slack, no GitOps app — all seven are empty. Comparing
        the recorded `''` against an empty environment value found drift in
        every one of them, on every interactive run, and the line the banner
        printed for each (`KEY=`) changed nothing, so the next run said it
        again. That buries the genuinely changed MEMORY this warning exists for.
        """
        recorded = "".join(
            f"{key}=''\n"
            for key in (
                "ALLOWED_USERS", "GOOGLE_CHAT_HOME_CHANNEL", "SLACK_ALLOWED_USERS",
                "SLACK_HOME_CHANNEL", "SLACK_HOME_CHANNEL_NAME", "GITOPS_ORG",
                "GITHUB_APP_ID",
            )
        )
        proc = self._warn(recorded, {})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("does not record", proc.stdout + proc.stderr)

    def test_a_percent_q_escaped_value_is_not_drift(self):
        """`%q` writes `#gke-alerts` as `\\#gke-alerts` and `a b` as `a\\ b`.

        Stripping only a surrounding pair of double quotes returned the escaped
        spelling, which never equals the value the interview holds.
        """
        proc = self._warn(
            "SLACK_HOME_CHANNEL=\\#gke-alerts\n"
            "SLACK_HOME_CHANNEL_NAME=alerts\\ channel\n",
            {
                "SLACK_HOME_CHANNEL": "#gke-alerts",
                "SLACK_HOME_CHANNEL_NAME": "alerts channel",
            },
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("does not record", proc.stdout + proc.stderr)

    def test_a_hand_authored_quoted_value_is_not_drift(self):
        """The other half: an operator writes `"#gke-alerts"`, not `\\#gke-alerts`.

        Both spellings mean one value, which is why this unquotes rather than
        re-quoting the current value and comparing the quoted forms.
        """
        proc = self._warn(
            'SLACK_HOME_CHANNEL="#gke-alerts"\n'
            "SLACK_ALLOWED_USERS='someone@example.com'\n",
            {
                "SLACK_HOME_CHANNEL": "#gke-alerts",
                "SLACK_ALLOWED_USERS": "someone@example.com",
            },
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("does not record", proc.stdout + proc.stderr)

    def test_a_quoted_value_that_really_changed_is_still_named(self):
        """Unquoting must not have made the warning unable to fire."""
        proc = self._warn(
            "SLACK_HOME_CHANNEL=\\#gke-alerts\n",
            {"SLACK_HOME_CHANNEL": "#gke-incidents"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertIn("does not record", combined)
        self.assertIn("SLACK_HOME_CHANNEL=#gke-incidents", combined)

    def test_an_export_prefixed_key_is_still_compared(self):
        """`export K=V` is a spelling install.env.example calls harmless.

        Both greps here matched a bare `K=` only, so an `export`-prefixed key
        was skipped outright — and skipping is silent and in the direction of
        no warning. Every other reader of the file accepts the prefix:
        `save_env_var`, `scripts/live_test_lease.py` and
        `admin_console/project_config.py`.
        """
        proc = self._warn(
            "export GOOGLE_CHAT_ENABLED=true\n", {"GOOGLE_CHAT_ENABLED": "false"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertIn("does not record", combined)
        self.assertIn("GOOGLE_CHAT_ENABLED=false", combined)

    def test_an_unchanged_export_prefixed_key_says_nothing(self):
        """Reading the prefix must not have turned every such key into drift."""
        proc = self._warn(
            "export GOOGLE_CHAT_ENABLED=true\n", {"GOOGLE_CHAT_ENABLED": "true"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("does not record", proc.stdout + proc.stderr)

    def test_a_changed_memory_answer_is_named(self):
        """The case the whole warning matters most for, and the one an entry
        that reads `$MEMORY` cannot see.

        `install.sh` never re-exports `MEMORY` after the memory interview: the
        answer lands in `PARAM_MEMORY` and in `MEMORY_PROVIDER`, while `MEMORY`
        still holds whatever `install.env` set at startup. So comparing against
        `$MEMORY` always finds them equal. An operator with `MEMORY=file` who
        picks the searchable store gets Hindsight provisioned, no warning, and
        an unchanged file — and the next run derives `multiuser_memory` from it
        and tears the Hindsight API and its Postgres back down.
        """
        proc = self._warn(
            "MEMORY=file\n", {"PARAM_MEMORY": "hindsight"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertIn("does not record", combined)
        self.assertIn("MEMORY=hindsight", combined)

    def test_an_unchanged_memory_answer_says_nothing(self):
        proc = self._warn("MEMORY=file\n", {"PARAM_MEMORY": "file"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("does not record", proc.stdout + proc.stderr)

    def test_settings_with_no_interview_question_are_not_listed(self):
        """ENABLE_GKE_BACKUP_PLAN and GVISOR_POOL_NAME are deliberately kept out
        of the export block because nothing asks about them, so an entry for
        them here could only ever compare a value against itself."""
        source = _INSTALL_SH.read_text()
        body = source.split("warn_unrecorded_interview_answers() {")[1]
        # The key list itself, not the comment above it that names these two as
        # the examples of what to leave out.
        keys = body.split("for key in ")[1].split("; do")[0]
        self.assertIn("MEMORY", keys, "sanity: the list was located")
        self.assertNotIn("ENABLE_GKE_BACKUP_PLAN", keys)
        self.assertNotIn("GVISOR_POOL_NAME", keys)

    def test_a_secret_is_named_without_its_value(self):
        proc = self._warn(
            "SLACK_BOT_TOKEN=xoxb-old\n", {"SLACK_BOT_TOKEN": "xoxb-brand-new"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertIn("SLACK_BOT_TOKEN", combined)
        self.assertNotIn("xoxb-brand-new", combined)

    def test_it_is_reached_when_the_file_already_exists(self):
        """bootstrap_install_env_file returns early on an existing file; the
        warning has to sit before that return or it never runs at all."""
        source = _INSTALL_SH.read_text()
        early_return = source.split("bootstrap_install_env_file() {")[1]
        early_return = early_return.split("if [ \"$PARAM_DRY_RUN\"")[0]
        self.assertIn("warn_unrecorded_interview_answers", early_return)


class TfvarsTempFileIsCleanedUpTest(unittest.TestCase):
    """A partial `terraform.tfvars.tmp` holds every secret the run was given.

    `write_tfvars_from_state` writes `${dest}.tmp`, `chmod 600`s it and then
    `mv`s it. A failure in between used to be covered by a trap branch that
    removed any `$vars_file` ending in `.tmp`; the replacement removed only
    `${INSTALL_ENV_FILE}.tmp`, so the tfvars residue survived — mode 600, full
    of secrets, and named one character from the file the next reader opens.
    """

    def test_the_generator_publishes_and_clears_the_path(self):
        source = (_REPO_ROOT / "scripts" / "installer" / "installer_common.sh").read_text()
        self.assertIn('TFVARS_TMP_FILE="${dest}.tmp"', source)
        self.assertIn('TFVARS_TMP_FILE=""', source)
        # Published before the redirect, cleared after the mv, in that order.
        self.assertLess(
            source.index('TFVARS_TMP_FILE="${dest}.tmp"'),
            source.index('mv -f -- "${dest}.tmp" "$dest"'),
        )
        self.assertLess(
            source.index('mv -f -- "${dest}.tmp" "$dest"'),
            source.index('TFVARS_TMP_FILE=""'),
        )

    def test_every_front_door_removes_it_on_error(self):
        """All three run the same generator, so all three can leave the same
        residue."""
        for name in ("install.sh", "upgrade.sh", "uninstall.sh"):
            with self.subTest(name=name):
                source = (_REPO_ROOT / name).read_text()
                handler = source.split("on_error() {")[1].split("\n}")[0]
                self.assertIn(
                    "TFVARS_TMP_FILE", handler,
                    f"{name}'s ERR trap must remove a partial tfvars",
                )


class PrerequisiteToolsListTest(unittest.TestCase):
    """Verifies that install.sh pre-flights all required tools including gke-gcloud-auth-plugin."""

    def test_install_script_checks_gke_gcloud_auth_plugin(self):
        source = _INSTALL_SH.read_text()
        self.assertIn("gke-gcloud-auth-plugin", source)
        self.assertRegex(
            source,
            r"for tool in [^\n]*gke-gcloud-auth-plugin",
            "install.sh must pre-flight gke-gcloud-auth-plugin in its prerequisite tool check loop",
        )


class AutoInstallToolTest(unittest.TestCase):
    """Verifies that install.sh auto_install_tool handles various tool installation paths and flags."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._empty_install_env = pathlib.Path(tmp.name) / "install.env"
        self._empty_install_env.write_text("")

    def _run_func(self, func_call, env=None, bin_dir=None, strict_path=False):
        setup = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"
{func_call}
"""
        overrides = {"KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env)}
        overrides.update(env or {})
        full_env = get_isolated_test_env(overrides=overrides, bin_dir=bin_dir)
        if strict_path and bin_dir:
            full_env["PATH"] = str(bin_dir)
        return subprocess.run(
            ["bash", "-c", setup],
            capture_output=True,
            text=True,
            env=full_env,
            cwd=str(_REPO_ROOT),
        )

    def test_dry_run_refuses_auto_install(self):
        proc = self._run_func(
            "PARAM_DRY_RUN=true auto_install_tool gke-gcloud-auth-plugin"
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Dry-run validation will not install missing tools", proc.stderr + proc.stdout)

    def test_auto_install_via_brew_runs_gcloud_component_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            bin_dir = create_minimal_tools_bin(tmp_path)
            log_file = tmp_path / "calls.log"

            brew_bin = bin_dir / "brew"
            brew_bin.write_text(f"#!/bin/bash\nprintf 'brew %s\\n' \"$*\" >> '{log_file}'\nexit 0\n")
            brew_bin.chmod(brew_bin.stat().st_mode | stat.S_IEXEC)

            plugin_path = bin_dir / "gke-gcloud-auth-plugin"
            gcloud_bin = bin_dir / "gcloud"
            gcloud_bin.write_text(
                f"#!/bin/bash\n"
                f"printf 'gcloud %s\\n' \"$*\" >> '{log_file}'\n"
                f"if [ \"$1\" = \"components\" ] && [ \"$2\" = \"install\" ] && [ \"$3\" = \"gke-gcloud-auth-plugin\" ]; then\n"
                f"  printf '#!/bin/bash\\nexit 0\\n' > '{plugin_path}'\n"
                f"  chmod +x '{plugin_path}'\n"
                f"fi\n"
                f"exit 0\n"
            )
            gcloud_bin.chmod(gcloud_bin.stat().st_mode | stat.S_IEXEC)

            proc = self._run_func(
                "PARAM_NON_INTERACTIVE=true auto_install_tool gke-gcloud-auth-plugin",
                bin_dir=str(bin_dir),
                strict_path=True,
            )
            self.assertEqual(proc.returncode, 0, f"Failed: {proc.stdout}\n{proc.stderr}")
            self.assertIn("installed successfully", proc.stdout)
            logged = log_file.read_text()
            self.assertIn("gcloud components install gke-gcloud-auth-plugin -q", logged)

    def test_auto_install_via_apt_installs_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            bin_dir = create_minimal_tools_bin(tmp_path)
            log_file = tmp_path / "calls.log"

            plugin_path = bin_dir / "gke-gcloud-auth-plugin"
            apt_bin = bin_dir / "apt-get"
            apt_bin.write_text(
                f"#!/bin/bash\n"
                f"printf 'apt-get %s\\n' \"$*\" >> '{log_file}'\n"
                f"if [ \"$1\" = \"install\" ] && [ \"$2\" = \"-y\" ] && [ \"$3\" = \"google-cloud-cli-gke-gcloud-auth-plugin\" ]; then\n"
                f"  printf '#!/bin/bash\\nexit 0\\n' > '{plugin_path}'\n"
                f"  chmod +x '{plugin_path}'\n"
                f"fi\n"
                f"exit 0\n"
            )
            apt_bin.chmod(apt_bin.stat().st_mode | stat.S_IEXEC)

            sudo_bin = bin_dir / "sudo"
            sudo_bin.write_text('#!/bin/bash\nexec "$@"\n')
            sudo_bin.chmod(sudo_bin.stat().st_mode | stat.S_IEXEC)

            proc = self._run_func(
                "PARAM_NON_INTERACTIVE=true auto_install_tool gke-gcloud-auth-plugin",
                bin_dir=str(bin_dir),
                strict_path=True,
            )
            self.assertEqual(proc.returncode, 0, f"Failed: {proc.stdout}\n{proc.stderr}")
            self.assertIn("installed successfully", proc.stdout)
            logged = log_file.read_text()
            self.assertIn("apt-get install -y google-cloud-cli-gke-gcloud-auth-plugin", logged)

    def test_auto_install_bare_gcloud_installs_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            bin_dir = create_minimal_tools_bin(tmp_path)
            log_file = tmp_path / "calls.log"

            plugin_path = bin_dir / "gke-gcloud-auth-plugin"
            gcloud_bin = bin_dir / "gcloud"
            gcloud_bin.write_text(
                f"#!/bin/bash\n"
                f"printf 'gcloud %s\\n' \"$*\" >> '{log_file}'\n"
                f"if [ \"$1\" = \"components\" ] && [ \"$2\" = \"install\" ] && [ \"$3\" = \"gke-gcloud-auth-plugin\" ]; then\n"
                f"  printf '#!/bin/bash\\nexit 0\\n' > '{plugin_path}'\n"
                f"  chmod +x '{plugin_path}'\n"
                f"fi\n"
                f"exit 0\n"
            )
            gcloud_bin.chmod(gcloud_bin.stat().st_mode | stat.S_IEXEC)

            proc = self._run_func(
                "PARAM_NON_INTERACTIVE=true auto_install_tool gke-gcloud-auth-plugin",
                bin_dir=str(bin_dir),
                strict_path=True,
            )
            self.assertEqual(proc.returncode, 0, f"Failed: {proc.stdout}\n{proc.stderr}")
            self.assertIn("installed successfully", proc.stdout)
            logged = log_file.read_text()
            self.assertIn("gcloud components install gke-gcloud-auth-plugin -q", logged)

    def test_auto_install_go_via_brew(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            bin_dir = create_minimal_tools_bin(tmp_path)
            log_file = tmp_path / "calls.log"

            go_path = bin_dir / "go"
            brew_bin = bin_dir / "brew"
            brew_bin.write_text(
                f"#!/bin/bash\n"
                f"printf 'brew %s\\n' \"$*\" >> '{log_file}'\n"
                f"if [ \"$1\" = \"install\" ] && [ \"$2\" = \"go\" ]; then\n"
                f"  printf '#!/bin/bash\\nexit 0\\n' > '{go_path}'\n"
                f"  chmod +x '{go_path}'\n"
                f"fi\n"
                f"exit 0\n"
            )
            brew_bin.chmod(brew_bin.stat().st_mode | stat.S_IEXEC)

            proc = self._run_func(
                "PARAM_NON_INTERACTIVE=true auto_install_tool go",
                bin_dir=str(bin_dir),
                strict_path=True,
            )
            self.assertEqual(proc.returncode, 0, f"Failed: {proc.stdout}\n{proc.stderr}")
            self.assertIn("installed successfully", proc.stdout)
            logged = log_file.read_text()
            self.assertIn("brew install go", logged)

    def test_auto_install_go_via_apt(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            bin_dir = create_minimal_tools_bin(tmp_path)
            log_file = tmp_path / "calls.log"

            go_path = bin_dir / "go"
            apt_bin = bin_dir / "apt-get"
            apt_bin.write_text(
                f"#!/bin/bash\n"
                f"printf 'apt-get %s\\n' \"$*\" >> '{log_file}'\n"
                f"if [ \"$1\" = \"install\" ] && [ \"$2\" = \"-y\" ] && [ \"$3\" = \"golang-go\" ]; then\n"
                f"  printf '#!/bin/bash\\nexit 0\\n' > '{go_path}'\n"
                f"  chmod +x '{go_path}'\n"
                f"fi\n"
                f"exit 0\n"
            )
            apt_bin.chmod(apt_bin.stat().st_mode | stat.S_IEXEC)

            sudo_bin = bin_dir / "sudo"
            sudo_bin.write_text('#!/bin/bash\nexec "$@"\n')
            sudo_bin.chmod(sudo_bin.stat().st_mode | stat.S_IEXEC)

            proc = self._run_func(
                "PARAM_NON_INTERACTIVE=true auto_install_tool go",
                bin_dir=str(bin_dir),
                strict_path=True,
            )
            self.assertEqual(proc.returncode, 0, f"Failed: {proc.stdout}\n{proc.stderr}")
            self.assertIn("installed successfully", proc.stdout)
            logged = log_file.read_text()
            self.assertIn("apt-get install -y golang-go", logged)

    def test_auto_install_fails_when_tool_remains_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            bin_dir = create_minimal_tools_bin(tmp_path)

            gcloud_bin = bin_dir / "gcloud"
            gcloud_bin.write_text("#!/bin/bash\nexit 0\n")
            gcloud_bin.chmod(gcloud_bin.stat().st_mode | stat.S_IEXEC)

            proc = self._run_func(
                "PARAM_NON_INTERACTIVE=true auto_install_tool gke-gcloud-auth-plugin",
                bin_dir=str(bin_dir),
                strict_path=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Tool 'gke-gcloud-auth-plugin' is still missing", proc.stderr + proc.stdout)


class RunLifecycleApplyTrapTest(unittest.TestCase):
    """Verifies that run_lifecycle_apply does not trigger duplicate ERR traps or
    misleading 'tee' error banners when lifecycle.sh fails (#1298)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._tmp_path = pathlib.Path(tmp.name)
        self._empty_install_env = self._tmp_path / "install.env"
        self._empty_install_env.write_text("")

    def _run_func(self, func_call, cwd=None, env_overrides=None, bin_dir=None):
        setup = f"""
source "{_INSTALLER_COMMON}"
KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"
{func_call}
"""
        overrides = {
            "KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env),
            "KUBE_AGENTS_INSTALL_REPORT_FILE": str(self._tmp_path / "report.json"),
        }
        overrides.update(env_overrides or {})
        full_env = get_isolated_test_env(overrides=overrides, bin_dir=bin_dir)
        return subprocess.run(
            ["bash", "-c", setup],
            capture_output=True,
            text=True,
            env=full_env,
            cwd=str(cwd or _REPO_ROOT),
            timeout=15,
        )

    def test_failed_apply_reports_only_command_and_not_tee(self):
        repo_dir = self._tmp_path / "mock-repo"
        compose_dir = repo_dir / "terraform" / "examples" / "full-install"
        compose_dir.mkdir(parents=True)
        lifecycle_sh = compose_dir / "lifecycle.sh"
        lifecycle_sh.write_text("#!/bin/bash\necho 'Terraform error' >&2\nexit 1\n")
        lifecycle_sh.chmod(0o755)

        log_file = self._tmp_path / "provision.log"
        proc = self._run_func(f'run_lifecycle_apply "{repo_dir}" "{log_file}"')

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Error encountered at line", proc.stderr)
        self.assertIn("./lifecycle.sh apply -auto-approve -input=false", proc.stderr)
        self.assertNotIn('tee "$log_file"', proc.stderr)
        self.assertNotIn("tee ", proc.stderr)

    def test_successful_apply_writes_log_file_and_succeeds(self):
        repo_dir = self._tmp_path / "mock-repo"
        compose_dir = repo_dir / "terraform" / "examples" / "full-install"
        compose_dir.mkdir(parents=True)
        lifecycle_sh = compose_dir / "lifecycle.sh"
        lifecycle_sh.write_text("#!/bin/bash\necho 'Apply complete'\nexit 0\n")
        lifecycle_sh.chmod(0o755)

        log_file = self._tmp_path / "provision.log"
        proc = self._run_func(f'run_lifecycle_apply "{repo_dir}" "{log_file}"')

        self.assertEqual(proc.returncode, 0, f"Stderr: {proc.stderr}")
        self.assertTrue(log_file.exists())
        self.assertIn("Apply complete", log_file.read_text())

    def test_run_lifecycle_apply_passes_no_color_when_env_set(self):
        repo_dir = self._tmp_path / "mock-repo"
        compose_dir = repo_dir / "terraform" / "examples" / "full-install"
        compose_dir.mkdir(parents=True)
        lifecycle_sh = compose_dir / "lifecycle.sh"
        lifecycle_sh.write_text("#!/bin/bash\necho \"ARGS: $*\"\nexit 0\n")
        lifecycle_sh.chmod(0o755)

        log_file = self._tmp_path / "provision.log"
        proc = self._run_func(
            f'run_lifecycle_apply "{repo_dir}" "{log_file}"',
            env_overrides={"NO_COLOR": "1"},
        )
        self.assertEqual(proc.returncode, 0, f"Stderr: {proc.stderr}")
        self.assertIn("-no-color", log_file.read_text())

    def test_pipeline_status_handles_empty_array_safely_under_set_u(self):
        source = _INSTALL_SH.read_text()
        self.assertIn(
            'handle_pipeline_status "./lifecycle.sh apply -auto-approve -input=false" "$log_file" ${ps[@]+"${ps[@]}"}',
            source,
        )
        self.assertNotIn(
            'handle_pipeline_status "./lifecycle.sh apply -auto-approve -input=false" "$log_file" "${ps[@]}"',
            source,
        )

    def test_run_lifecycle_apply_fetches_credentials_and_diagnoses_on_rollout_timeout(self):
        # When lifecycle.sh fails with a Helm rollout timeout on an existing cluster
        # whose context matches, run_lifecycle_apply must fetch credentials and
        # invoke diagnose_rollout_failure.
        repo_dir = self._tmp_path / "mock-repo"
        compose_dir = repo_dir / "terraform" / "examples" / "full-install"
        compose_dir.mkdir(parents=True)
        lifecycle_sh = compose_dir / "lifecycle.sh"
        lifecycle_sh.write_text("""#!/bin/bash
cat << 'EOF' >&2
Error: context deadline exceeded

  with helm_release.kube_agents,
  on main.tf line 497:
EOF
exit 1
""")
        lifecycle_sh.chmod(0o755)

        bin_dir = self._tmp_path / "bin"
        bin_dir.mkdir()
        gcloud_log = self._tmp_path / "gcloud.log"
        gcloud = bin_dir / "gcloud"
        gcloud.write_text(f"""#!/usr/bin/env bash
echo "$@" >> "{gcloud_log}"
exit 0
""")
        kubectl = bin_dir / "kubectl"
        kubectl.write_text("""#!/usr/bin/env bash
case "$*" in
  *current-context*) echo "gke_proj_us-central1_target" ;;
  *get*pods*) echo "pod-failing" ;;
  *get*events*) echo "CrashLoopBackOff container died" ;;
  *) echo "" ;;
esac
exit 0
""")
        for stub in (gcloud, kubectl):
            stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

        log_file = self._tmp_path / "provision.log"
        call = f"""
export PROJECT_ID=proj REGION=us-central1 CLUSTER_NAME=target TFVARS_CREATE_CLUSTER=false
run_lifecycle_apply "{repo_dir}" "{log_file}"
"""
        proc = self._run_func(
            call,
            bin_dir=str(bin_dir),
        )

        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(gcloud_log.exists())
        gcloud_calls = gcloud_log.read_text()
        self.assertIn("container clusters get-credentials target", gcloud_calls)
        self.assertIn("Pending Pods Detected:", proc.stdout + proc.stderr)
        self.assertIn("pod-failing", proc.stdout + proc.stderr)

    def test_run_lifecycle_apply_credential_fetch_on_helm_timeout(self):
        # Credential fetch runs on Helm rollout timeout whenever current context
        # matches or is empty (including on fresh cluster creation, since the cluster
        # module has already provisioned before Helm runs), but is skipped when current
        # context points elsewhere to avoid repointing the operator's kubeconfig.
        repo_dir = self._tmp_path / "mock-repo"
        compose_dir = repo_dir / "terraform" / "examples" / "full-install"
        compose_dir.mkdir(parents=True)
        lifecycle_sh = compose_dir / "lifecycle.sh"
        lifecycle_sh.write_text("""#!/bin/bash
cat << 'EOF' >&2
Error: context deadline exceeded

  with helm_release.kube_agents,
  on main.tf line 497:
EOF
exit 1
""")
        lifecycle_sh.chmod(0o755)

        bin_dir = self._tmp_path / "bin"
        bin_dir.mkdir()
        gcloud_log = self._tmp_path / "gcloud.log"
        gcloud = bin_dir / "gcloud"
        gcloud.write_text(f"""#!/usr/bin/env bash
echo "$@" >> "{gcloud_log}"
exit 0
""")
        current_ctx_file = self._tmp_path / "current_context.txt"
        kubectl = bin_dir / "kubectl"
        kubectl.write_text(f"""#!/usr/bin/env bash
if [ -f "{current_ctx_file}" ]; then
  cat "{current_ctx_file}"
else
  echo "gke_proj_us-central1_target"
fi
exit 0
""")
        for stub in (gcloud, kubectl):
            stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

        log_file = self._tmp_path / "provision.log"

        # Case A: TFVARS_CREATE_CLUSTER=true with matching kubectl context.
        # Proves that credential fetch executes on fresh cluster creation once
        # Helm has timed out, allowing pod diagnosis to query the cluster.
        current_ctx_file.write_text("gke_proj_us-central1_target\n")
        call_a = f"""
export PROJECT_ID=proj REGION=us-central1 CLUSTER_NAME=target TFVARS_CREATE_CLUSTER=true
run_lifecycle_apply "{repo_dir}" "{log_file}"
"""
        self._run_func(
            call_a,
            bin_dir=str(bin_dir),
        )
        self.assertTrue(gcloud_log.exists(), "gcloud was not called under TFVARS_CREATE_CLUSTER=true with matching context")
        self.assertIn("clusters get-credentials target", gcloud_log.read_text())

        # Case B: Context mismatch with TFVARS_CREATE_CLUSTER=false.
        # Proves that mismatched context prevents credential fetch even on adopted cluster.
        gcloud_log.unlink()
        current_ctx_file.write_text("gke_other_us-west1_other\n")
        call_b = f"""
export PROJECT_ID=proj REGION=us-central1 CLUSTER_NAME=target TFVARS_CREATE_CLUSTER=false
run_lifecycle_apply "{repo_dir}" "{log_file}"
"""
        self._run_func(
            call_b,
            bin_dir=str(bin_dir),
        )
        self.assertFalse(gcloud_log.exists(), "gcloud was called on context mismatch")

        # Case C: Context matches with TFVARS_CREATE_CLUSTER=false (positive control).
        # Proves that credential fetch DOES execute when context matches on adopted cluster.
        current_ctx_file.write_text("gke_proj_us-central1_target\n")
        call_c = f"""
export PROJECT_ID=proj REGION=us-central1 CLUSTER_NAME=target TFVARS_CREATE_CLUSTER=false
run_lifecycle_apply "{repo_dir}" "{log_file}"
"""
        self._run_func(
            call_c,
            bin_dir=str(bin_dir),
        )
        self.assertTrue(gcloud_log.exists(), "gcloud was not called when context matches and adopted cluster")
        self.assertIn("clusters get-credentials target", gcloud_log.read_text())

        # Case D: Empty context with TFVARS_CREATE_CLUSTER=true.
        # Proves that credential fetch executes when current context is unset/empty on fresh create.
        gcloud_log.unlink()
        current_ctx_file.write_text("\n")
        call_d = f"""
export PROJECT_ID=proj REGION=us-central1 CLUSTER_NAME=target TFVARS_CREATE_CLUSTER=true
run_lifecycle_apply "{repo_dir}" "{log_file}"
"""
        self._run_func(
            call_d,
            bin_dir=str(bin_dir),
        )
        self.assertTrue(gcloud_log.exists(), "gcloud was not called when context is empty")
        self.assertIn("clusters get-credentials target", gcloud_log.read_text())

    def test_run_lifecycle_apply_reaps_monitor_on_sigint(self):
        # Under Ctrl-C (SIGINT), run_lifecycle_apply's interrupt handler must terminate
        # the background monitor_lifecycle_rollout child process so it does not persist
        # as an orphan.
        repo_dir = self._tmp_path / "mock-repo"
        compose_dir = repo_dir / "terraform" / "examples" / "full-install"
        compose_dir.mkdir(parents=True)
        lifecycle_sh = compose_dir / "lifecycle.sh"
        log_file = self._tmp_path / "provision.log"
        lifecycle_sh.write_text(f"""#!/bin/bash
echo "APPLY_STARTED" >> "{log_file}"
trap 'exit 130' INT TERM
while true; do /bin/sleep 0.1; done
""")
        lifecycle_sh.chmod(0o755)

        bin_dir = self._tmp_path / "bin"
        bin_dir.mkdir()
        monitor_pid_file = self._tmp_path / "monitor.pid"
        sleep_stub = bin_dir / "sleep"
        sleep_stub.write_text(f"""#!/usr/bin/env bash
trap '' INT
if [ ! -f "{monitor_pid_file}" ]; then
  echo "$PPID" > "{monitor_pid_file}"
fi
exec /bin/sleep 5
""")
        sleep_stub.chmod(0o755)

        kubectl = bin_dir / "kubectl"
        kubectl.write_text("""#!/usr/bin/env bash
exit 0
""")
        kubectl.chmod(0o755)

        setup = f"""
source "{_INSTALLER_COMMON}"
KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"
export PROJECT_ID=p REGION=r CLUSTER_NAME=c
run_lifecycle_apply "{repo_dir}" "{log_file}"
"""
        overrides = {
            "KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env),
            "KUBE_AGENTS_INSTALL_REPORT_FILE": str(self._tmp_path / "report.json"),
        }
        full_env = get_isolated_test_env(overrides=overrides, bin_dir=str(bin_dir))
        proc = subprocess.Popen(
            ["bash", "-c", setup],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=full_env,
            cwd=str(_REPO_ROOT),
            start_new_session=True,
        )

        try:
            for _ in range(50):
                if monitor_pid_file.exists() and log_file.exists() and "APPLY_STARTED" in log_file.read_text():
                    break
                time.sleep(0.1)

            self.assertTrue(monitor_pid_file.exists(), "monitor failed to record its pid")
            mon_pid = int(monitor_pid_file.read_text().strip())

            os.killpg(proc.pid, signal.SIGINT)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)

            time.sleep(0.2)
            try:
                os.kill(mon_pid, 0)
                mon_alive = True
            except OSError:
                mon_alive = False
            if mon_alive:
                try:
                    os.kill(mon_pid, signal.SIGKILL)
                except OSError:
                    pass
            self.assertFalse(mon_alive, f"monitor process {mon_pid} is still alive after SIGINT")
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.wait()

    def test_run_lifecycle_apply_does_not_defer_sigterm_and_terminates_immediately(self):
        # A SIGTERM delivered to install.sh alone must not be deferred behind a long-running
        # foreground pipeline. It retains default termination disposition and dies at once.
        repo_dir = self._tmp_path / "mock-repo"
        compose_dir = repo_dir / "terraform" / "examples" / "full-install"
        compose_dir.mkdir(parents=True)
        lifecycle_sh = compose_dir / "lifecycle.sh"
        log_file = self._tmp_path / "provision.log"
        lifecycle_sh.write_text(f"""#!/bin/bash
trap 'exit 0' TERM INT
echo "APPLY_STARTED" >> "{log_file}"
for _ in $(seq 1 100); do /bin/sleep 0.1; done
""")
        lifecycle_sh.chmod(0o755)

        setup = f"""
source "{_INSTALLER_COMMON}"
KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"
run_lifecycle_apply "{repo_dir}" "{log_file}"
"""
        overrides = {
            "KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env),
            "KUBE_AGENTS_INSTALL_REPORT_FILE": str(self._tmp_path / "report.json"),
        }
        full_env = get_isolated_test_env(overrides=overrides)
        proc = subprocess.Popen(
            ["bash", "-c", setup],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=full_env,
            cwd=str(_REPO_ROOT),
            start_new_session=True,
        )

        try:
            for _ in range(50):
                if log_file.exists() and "APPLY_STARTED" in log_file.read_text():
                    break
                time.sleep(0.1)

            self.assertTrue(log_file.exists())
            # Deliver SIGTERM to install.sh PID directly (not process group).
            os.kill(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.fail("run_lifecycle_apply deferred SIGTERM while waiting for foreground command")
            self.assertIn(proc.returncode, (-signal.SIGTERM, 128 + signal.SIGTERM))
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            if proc.poll() is None:
                try:
                    proc.wait(timeout=2)
                except (subprocess.TimeoutExpired, OSError):
                    pass

    def test_run_lifecycle_apply_closes_lock_fd_200_on_monitor(self):
        # Asserts that run_lifecycle_apply closes fd 200 (the flock descriptor)
        # when backgrounding monitor_lifecycle_rollout via 200>&-.
        if not pathlib.Path("/proc").exists() or not shutil.which("flock"):
            self.skipTest("Requires Linux /proc and flock")
        repo_dir = self._tmp_path / "mock-repo"
        compose_dir = repo_dir / "terraform" / "examples" / "full-install"
        compose_dir.mkdir(parents=True)
        lifecycle_sh = compose_dir / "lifecycle.sh"
        log_file = self._tmp_path / "provision.log"
        fd_status_file = self._tmp_path / "fd200_status.txt"
        lifecycle_sh.write_text(f"""#!/bin/bash
for _ in $(seq 1 50); do
  if [ -f "{fd_status_file}" ]; then
    break
  fi
  /bin/sleep 0.02
done
exit 0
""")
        lifecycle_sh.chmod(0o755)

        bin_dir = self._tmp_path / "bin"
        bin_dir.mkdir()
        sleep_stub = bin_dir / "sleep"
        # The sleep stub runs inside monitor_lifecycle_rollout. $PPID is the monitor.
        sleep_stub.write_text(f"""#!/usr/bin/env bash
if [ -e "/proc/$PPID/fd/200" ]; then
  echo "OPEN" > "{fd_status_file}.tmp" && mv "{fd_status_file}.tmp" "{fd_status_file}"
else
  echo "CLOSED" > "{fd_status_file}.tmp" && mv "{fd_status_file}.tmp" "{fd_status_file}"
fi
exit 0
""")
        sleep_stub.chmod(0o755)

        kubectl = bin_dir / "kubectl"
        kubectl.write_text("""#!/usr/bin/env bash
exit 0
""")
        kubectl.chmod(0o755)

        lock_file = self._tmp_path / "installer.lock"
        setup = f"""
export PROJECT_ID=p REGION=r CLUSTER_NAME=c
exec 200>"{lock_file}"
flock -n 200
run_lifecycle_apply "{repo_dir}" "{log_file}"
exec 200>&-
"""
        proc = self._run_func(setup, bin_dir=str(bin_dir))
        self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
        self.assertTrue(fd_status_file.exists(), "sleep stub did not record fd 200 status")
        self.assertEqual(fd_status_file.read_text().strip(), "CLOSED", "monitor process inherited fd 200")

    def test_monitor_lifecycle_rollout_exits_when_parent_dies(self):
        # Asserts that monitor_lifecycle_rollout exits cleanly when kill -0 "$parent_pid"
        # fails, preventing orphaned monitors from looping indefinitely.
        bin_dir = self._tmp_path / "bin"
        bin_dir.mkdir()
        sleep_stub = bin_dir / "sleep"
        sleep_stub.write_text("""#!/usr/bin/env bash
exit 0
""")
        sleep_stub.chmod(0o755)

        kubectl = bin_dir / "kubectl"
        kubectl.write_text("""#!/usr/bin/env bash
exit 0
""")
        kubectl.chmod(0o755)

        # Case 1: Pass an already-dead parent pid directly.
        dead_pid_script = """
export PROJECT_ID=p REGION=r CLUSTER_NAME=c
( exit 0 ) &
dead_pid=$!
wait "$dead_pid"
( monitor_lifecycle_rollout "$dead_pid" )
rc=$?
echo "RC=$rc"
"""
        proc = self._run_func(dead_pid_script, bin_dir=str(bin_dir))
        self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
        self.assertIn("RC=0", proc.stdout)

        # Case 2: Parent process terminates while monitor is sleeping in its loop.
        parent_death_script = """
export PROJECT_ID=p REGION=r CLUSTER_NAME=c
( /bin/sleep 0.15; exit 0 ) &
parent_pid=$!
( monitor_lifecycle_rollout "$parent_pid" )
rc=$?
echo "LOOP_RC=$rc"
"""
        proc2 = self._run_func(parent_death_script, bin_dir=str(bin_dir))
        self.assertEqual(proc2.returncode, 0, f"stdout: {proc2.stdout}\nstderr: {proc2.stderr}")
        self.assertIn("LOOP_RC=0", proc2.stdout)

    def test_monitor_lifecycle_rollout_exits_when_max_duration_exceeded(self):
        # Asserts that monitor_lifecycle_rollout exits when elapsed >= ROLLOUT_MONITOR_MAX_DURATION_SECS.
        bin_dir = self._tmp_path / "bin"
        bin_dir.mkdir()
        loop_count_file = self._tmp_path / "loop_count.txt"
        sleep_stub = bin_dir / "sleep"
        sleep_stub.write_text(f"""#!/usr/bin/env bash
count=$(cat "{loop_count_file}" 2>/dev/null || echo 0)
echo $((count + 1)) > "{loop_count_file}"
exit 0
""")
        sleep_stub.chmod(0o755)

        kubectl = bin_dir / "kubectl"
        kubectl.write_text("""#!/usr/bin/env bash
exit 0
""")
        kubectl.chmod(0o755)

        # Set ROLLOUT_MONITOR_MAX_DURATION_SECS=0 so that after 1 iteration elapsed >= cap.
        cap_script = """
export PROJECT_ID=p REGION=r CLUSTER_NAME=c
( monitor_lifecycle_rollout "$$" )
rc=$?
echo "RC=$rc"
"""
        proc = self._run_func(
            cap_script,
            env_overrides={"ROLLOUT_MONITOR_MAX_DURATION_SECS": "0"},
            bin_dir=str(bin_dir),
        )
        self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
        self.assertIn("RC=0", proc.stdout)
        self.assertTrue(loop_count_file.exists())
        self.assertEqual(loop_count_file.read_text().strip(), "1")


class RunWithSpinnerAndRolloutTest(unittest.TestCase):
    """Verifies run_with_spinner, wait_for_rollout, and dry-run validation error propagation."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._tmp_path = pathlib.Path(tmp.name)
        self._empty_install_env = self._tmp_path / "install.env"
        self._empty_install_env.write_text("")

    def _run_func(self, func_call, env=None, cwd=None, bin_dir=None):
        setup = f"""
source "{_INSTALLER_COMMON}"
KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"
{func_call}
"""
        overrides = {
            "KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env),
            "KUBE_AGENTS_INSTALL_REPORT_FILE": str(self._tmp_path / "report.json"),
        }
        overrides.update(env or {})
        full_env = get_isolated_test_env(overrides=overrides, bin_dir=bin_dir)
        return subprocess.run(
            ["bash", "-c", setup],
            capture_output=True,
            text=True,
            env=full_env,
            cwd=str(cwd or _REPO_ROOT),
        )

    def test_run_with_spinner_non_tty_streams_output_and_returns_zero(self):
        log_file = self._tmp_path / "test.log"
        script = f"""
mock_cmd() {{
  echo "streamed line 1"
  echo "streamed line 2"
  return 0
}}
run_with_spinner "Step A" "{log_file}" mock_cmd
"""
        proc = self._run_func(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Step A...", proc.stdout)
        self.assertIn("streamed line 1", proc.stdout)
        self.assertIn("streamed line 2", proc.stdout)
        self.assertTrue(log_file.exists())
        self.assertIn("streamed line 1\nstreamed line 2", log_file.read_text())

    def test_run_with_spinner_non_tty_propagates_nonzero_exit_code_and_log(self):
        log_file = self._tmp_path / "test.log"
        script = f"""
mock_failing_cmd() {{
  echo "failing message" >&2
  return 42
}}
rc=0
run_with_spinner "Step B" "{log_file}" mock_failing_cmd || rc=$?
echo "RC=$rc"
"""
        proc = self._run_func(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("RC=42", proc.stdout)
        self.assertTrue(log_file.exists())
        self.assertIn("failing message", log_file.read_text())

    def test_wait_for_rollout_succeeds_when_kubectl_succeeds(self):
        bin_dir = self._tmp_path / "bin"
        bin_dir.mkdir(parents=True)
        kubectl = bin_dir / "kubectl"
        kubectl.write_text("#!/bin/bash\necho 'deployment successfully rolled out'\nexit 0\n")
        kubectl.chmod(0o755)

        script = 'rc=0; wait_for_rollout test-dep test-ns 5 || rc=$?; echo "RC=$rc"'
        proc = self._run_func(script, bin_dir=str(bin_dir))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("RC=0", proc.stdout)
        self.assertIn("test-dep rolled out in", proc.stdout)

    def test_wait_for_rollout_fails_and_echoes_tail_when_kubectl_fails(self):
        bin_dir = self._tmp_path / "bin"
        bin_dir.mkdir(parents=True)
        kubectl = bin_dir / "kubectl"
        kubectl.write_text("#!/bin/bash\necho 'error: deadline exceeded' >&2\nexit 1\n")
        kubectl.chmod(0o755)

        script = 'rc=0; wait_for_rollout test-dep test-ns 5 || rc=$?; echo "RC=$rc"'
        proc = self._run_func(script, bin_dir=str(bin_dir))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("RC=1", proc.stdout)
        self.assertIn("error: deadline exceeded", proc.stdout + proc.stderr)

    def test_dry_run_validation_fails_fast_when_terraform_init_fails(self):
        bin_dir = self._tmp_path / "bin"
        bin_dir.mkdir(parents=True)
        terraform = bin_dir / "terraform"
        counter = self._tmp_path / "validate_counter.txt"
        terraform.write_text(f"""#!/bin/bash
if [ "$1" = "init" ]; then
  echo "init failed" >&2
  exit 2
fi
if [ "$1" = "validate" ]; then
  echo "called" >> "{counter}"
  exit 0
fi
exit 0
""")
        terraform.chmod(0o755)

        # No local definition of validate_tf_config: install.sh defines it at file
        # scope, so _run_func sources the real one. Redeclaring it here would assert
        # that this file's copy short-circuits, which is true of any string and
        # stays green when install.sh's own chaining is removed.
        script = f"""
tf_log="{self._tmp_path}/tf.log"
rc=0
run_with_spinner "Validating Terraform configuration" "$tf_log" validate_tf_config || rc=$?
echo "RC=$rc"
"""
        proc = self._run_func(script, bin_dir=str(bin_dir))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("RC=2", proc.stdout)
        self.assertFalse(counter.exists(), "terraform validate must not be invoked if terraform init fails")

    def test_rollout_warning_reports_measured_elapsed_not_the_timeout_constant(self):
        """The warning carries how long the wait actually ran, never the budget.

        Naming ROLLOUT_TIMEOUT_SECS asserted 300s even when the rollout failed in
        three; ROLLOUT_ELAPSED_SECS is measured by wait_for_rollout, so a fast
        ProgressDeadlineExceeded reads differently from an exhausted budget.
        """
        source = _INSTALL_SH.read_text()
        self.assertIn('print_warning "$deployment did not report ready (after ${ROLLOUT_ELAPSED_SECS}s)."', source)
        self.assertIn("ROLLOUT_ELAPSED_SECS=$((SECONDS - started))", source)
        self.assertNotIn('print_warning "$deployment did not report ready within ${ROLLOUT_TIMEOUT_SECS}s."', source)


class ChatSubscriptionDerivationTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._empty_install_env = pathlib.Path(tmp.name) / "install.env"
        self._empty_install_env.write_text("")

    def _run_install_func(self, func_call, env=None):
        setup = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"
source_provisioning_helpers . >/dev/null
{func_call}
"""
        overrides = {"KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env)}
        overrides.update(env or {})
        full_env = get_isolated_test_env(overrides=overrides)
        return subprocess.run(
            ["bash", "-c", setup],
            capture_output=True,
            text=True,
            env=full_env,
            cwd=str(_REPO_ROOT),
        )

    def test_default_topic_derives_default_subscription(self):
        proc = self._run_install_func('echo "SUB=$(derive_chat_sub_name)"')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("SUB=platform-agent-chat-events-sub", proc.stdout)

    def test_custom_topic_derives_matching_subscription(self):
        proc = self._run_install_func('echo "SUB=$(derive_chat_sub_name "my-custom-topic")"')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("SUB=my-custom-topic-sub", proc.stdout)

    def test_custom_topic_with_empty_sub_derives(self):
        proc = self._run_install_func(
            'echo "SUB=$(derive_chat_sub_name "my-custom-topic" "")"'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("SUB=my-custom-topic-sub", proc.stdout)

    def test_custom_topic_with_explicit_default_sub_is_preserved(self):
        proc = self._run_install_func(
            'echo "SUB=$(derive_chat_sub_name "my-custom-topic" "platform-agent-chat-events-sub")"'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("SUB=platform-agent-chat-events-sub", proc.stdout)

    def test_explicit_custom_subscription_wins(self):
        proc = self._run_install_func(
            'echo "SUB=$(derive_chat_sub_name "my-custom-topic" "explicit-sub-name")"'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("SUB=explicit-sub-name", proc.stdout)

    def test_custom_topic_ignores_ambient_chat_sub_name_when_not_passed(self):
        proc = self._run_install_func(
            'echo "SUB=$(derive_chat_sub_name "my-custom-topic")"',
            env={"CHAT_SUB_NAME": "stale-env-sub"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("SUB=my-custom-topic-sub", proc.stdout)

    def test_parse_args_supports_chat_sub_name_flag(self):
        proc = self._run_install_func(
            'parse_args --chat-sub-name=custom-sub; echo "SUB=$PARAM_CHAT_SUB_NAME"'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("SUB=custom-sub", proc.stdout)

    def test_resolve_shared_defaults_leaves_chat_sub_name_empty_when_unset(self):
        proc = self._run_install_func(
            'PARAM_CHAT_TOPIC_NAME="custom-events"; resolve_shared_defaults; echo "SUB=${PARAM_CHAT_SUB_NAME:-EMPTY}"'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("SUB=EMPTY", proc.stdout)

    def test_resolve_shared_defaults_preserves_explicit_chat_sub_name(self):
        proc = self._run_install_func(
            'PARAM_CHAT_TOPIC_NAME="custom-events"; PARAM_CHAT_SUB_NAME="my-sub"; resolve_shared_defaults; echo "SUB=$PARAM_CHAT_SUB_NAME"'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("SUB=my-sub", proc.stdout)

    def test_prompt_google_chat_settings_rederives_when_flag_unset(self):
        body = pathlib.Path(_INSTALL_SH).read_text().split("_prompt_google_chat_settings() {")[1].split('case "$chat_choice" in')[0].strip()
        proc = self._run_install_func(f"""
prompt_read() {{
  local prompt="$1" var="$2" default_val="${{3:-}}"
  if [ "$var" = "chat_topic_name" ]; then
    eval "$var=\\"operator-custom-topic\\""
  else
    eval "$var=\\"$default_val\\""
  fi
}}
allowed_users="" allowed_users_hint="" chat_topic_name="platform-agent-chat-events" chat_sub_name="" PARAM_CHAT_SUB_NAME="" google_chat_home_channel=""
_prompt_google_chat_settings() {{
{body}
_prompt_google_chat_settings
echo "DERIVED_SUB=$chat_sub_name"
""")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("DERIVED_SUB=operator-custom-topic-sub", proc.stdout)

    def test_prompt_google_chat_settings_preserves_explicit_flag(self):
        body = pathlib.Path(_INSTALL_SH).read_text().split("_prompt_google_chat_settings() {")[1].split('case "$chat_choice" in')[0].strip()
        proc = self._run_install_func(f"""
prompt_read() {{
  local prompt="$1" var="$2" default_val="${{3:-}}"
  if [ "$var" = "chat_topic_name" ]; then
    eval "$var=\\"operator-custom-topic\\""
  else
    eval "$var=\\"$default_val\\""
  fi
}}
allowed_users="" allowed_users_hint="" chat_topic_name="platform-agent-chat-events" chat_sub_name="pinned-sub" PARAM_CHAT_SUB_NAME="pinned-sub" google_chat_home_channel="" project_id="p" cluster_name="c"
_prompt_google_chat_settings() {{
{body}
_prompt_google_chat_settings
echo "DERIVED_SUB=$chat_sub_name"
""")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("DERIVED_SUB=pinned-sub", proc.stdout)

    def test_prompt_google_chat_settings_rederives_when_param_is_recorded_default(self):
        body = pathlib.Path(_INSTALL_SH).read_text().split("_prompt_google_chat_settings() {")[1].split('case "$chat_choice" in')[0].strip()
        proc = self._run_install_func(f"""
prompt_read() {{
  local prompt="$1" var="$2" default_val="${{3:-}}"
  if [ "$var" = "chat_topic_name" ]; then
    eval "$var=\\"operator-custom-topic\\""
  else
    eval "$var=\\"$default_val\\""
  fi
}}
allowed_users="" allowed_users_hint="" chat_topic_name="platform-agent-chat-events" chat_sub_name="platform-agent-chat-events-sub" PARAM_CHAT_SUB_NAME="platform-agent-chat-events-sub" google_chat_home_channel="" project_id="p" cluster_name="c"
_prompt_google_chat_settings() {{
{body}
_prompt_google_chat_settings
echo "DERIVED_SUB=$chat_sub_name"
""")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("DERIVED_SUB=operator-custom-topic-sub", proc.stdout)

    def test_prompt_google_chat_settings_recovers_state_subscription(self):
        body = pathlib.Path(_INSTALL_SH).read_text().split("_prompt_google_chat_settings() {")[1].split('case "$chat_choice" in')[0].strip()
        proc = self._run_install_func(f"""
prompt_read() {{
  local prompt="$1" var="$2" default_val="${{3:-}}"
  if [ "$var" = "chat_topic_name" ]; then
    eval "$var=\\"operator-custom-topic\\""
  else
    eval "$var=\\"$default_val\\""
  fi
}}
tf_state_chat_subscription_name() {{
  echo "legacy-managed-sub"
}}
allowed_users="" allowed_users_hint="" chat_topic_name="platform-agent-chat-events" chat_sub_name="" PARAM_CHAT_SUB_NAME="" google_chat_home_channel="" project_id="p" cluster_name="c"
_prompt_google_chat_settings() {{
{body}
_prompt_google_chat_settings
echo "DERIVED_SUB=$chat_sub_name"
""")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("DERIVED_SUB=legacy-managed-sub", proc.stdout)


@unittest.skipUnless(hasattr(pty, "fork"), "run_with_spinner's terminal branch needs a pty")
class SpinnerTerminalBranchTest(unittest.TestCase):
    """run_with_spinner on a real terminal, the branch no piped test reaches.

    Every other test in this file runs under a subprocess pipe, so `[ ! -t 1 ]`
    diverts it to the fallback and the spinner loop, the cursor calls, the
    background job and the interrupt traps never execute at all. On a terminal
    -- where an operator actually meets them -- they all do, so these drive one.
    """

    _READY_TIMEOUT_SECS = 30
    _POLL_INTERVAL_SECS = 0.1

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._tmp_path = pathlib.Path(tmp.name)

    def _spawn_on_pty(self, script):
        """Run script under bash with a controlling terminal. Returns its pid."""
        env = get_isolated_test_env(
            overrides={"KUBE_AGENTS_INSTALL_REPORT_FILE": str(self._tmp_path / "report.json")}
        )
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.chdir(str(_REPO_ROOT))
                os.execvpe("bash", ["bash", "-c", script], env)
            finally:  # pragma: no cover - only on execvpe failure
                os._exit(127)
        # The spinner redraws continuously, so the pty buffer fills and the child
        # blocks on write unless someone is reading. Drain it for the run's life.
        drain = threading.Thread(target=self._drain, args=(fd,), daemon=True)
        drain.start()
        self.addCleanup(self._cleanup_pty, pid, fd)
        return pid

    @staticmethod
    def _drain(fd):
        while True:
            try:
                if not os.read(fd, 4096):
                    return
            except OSError:
                return

    @staticmethod
    def _cleanup_pty(pid, fd):
        for killer in (lambda: os.killpg(os.getpgid(pid), signal.SIGKILL), lambda: os.kill(pid, signal.SIGKILL)):
            try:
                killer()
            except OSError:
                pass
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def _await_file(self, path, what):
        deadline = time.monotonic() + self._READY_TIMEOUT_SECS
        while time.monotonic() < deadline:
            if path.exists() and path.read_text().strip():
                return path.read_text().strip()
            time.sleep(self._POLL_INTERVAL_SECS)
        self.fail(f"timed out after {self._READY_TIMEOUT_SECS}s waiting for {what} at {path}")

    def test_the_spinner_loop_keeps_errexit_out_of_its_interruptible_commands(self):
        """The loop's forked children must not be able to fire the ERR trap.

        SIGINT from a terminal goes to the whole foreground group, so the loop's
        own `sleep` and the `tail | tr | cut` pipeline die of it and report 130.
        Unguarded under `set -Ee` that fires the global ERR trap at install.sh:96,
        and on_error exits before bash dispatches the pending INT trap -- so the
        interrupt handler never runs, the worker is orphaned, the cursor stays
        hidden, and a cancellation is written to the report as "FAILED".

        Asserted on the source. The behaviour needs a signal delivered inside a
        specific instruction window, which is measurable but not reliably
        reproducible in a unit test; see this PR's Live validation for the
        out-of-tree probe that measured it.
        """
        source = _INSTALL_SH.read_text()
        self.assertIn('sleep "$SPINNER_INTERVAL_SECS" || true', source)
        self.assertIn(
            '''status_line="$(tail -n 1 "$log_file" 2>/dev/null | tr -d '\\r' | cut -c1-"$status_width")" || status_line=""''',
            source,
        )

    def test_the_interrupt_traps_arm_before_the_job_they_reap_exists(self):
        """Arming after the `&` leaves the worker running with SIGINT at default here.

        In that window the shell dies on Ctrl-C while the worker -- which
        inherited SIG_IGN for SIGINT as a `&` child -- survives it with nothing
        left to reap it. The order is the fix, so the order is what is pinned.
        """
        source = _INSTALL_SH.read_text()
        arm = source.index("trap 'on_spinner_interrupt 130' INT")
        start = source.index('"$@" >"$log_file" 2>&1 &')
        assign = source.index("task_pid=$!")
        self.assertLess(arm, start, "the INT trap must be armed before the job is backgrounded")
        self.assertLess(start, assign)
        self.assertIn('if [ "$task_pid" -ne 0 ]; then', source)

    def test_terminal_branch_returns_the_wrapped_command_status(self):
        """The spinner branch must propagate the exit code, not the spinner's own."""
        log_file = self._tmp_path / "rc.log"
        rc_file = self._tmp_path / "rc.out"
        script = f"""
source "{_INSTALLER_COMMON}"
KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"
fail_with_42() {{ echo "the wrapped output"; return 42; }}
rc=0
run_with_spinner "working" "{log_file}" fail_with_42 || rc=$?
echo "$rc" > "{rc_file}"
"""
        self._spawn_on_pty(script)
        self.assertEqual("42", self._await_file(rc_file, "the wrapped command's exit status"))
        self.assertIn("the wrapped output", log_file.read_text())


class CapacityPreflightAndRolloutVisibilityTest(unittest.TestCase):
    """Tests for cluster capacity preflight and rollout monitoring (#1297)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._tmp_path = pathlib.Path(tmp.name)
        self._empty_install_env = self._tmp_path / "install.env"
        self._empty_install_env.write_text("")
        self._test_report_file = self._tmp_path / "report.json"

    def _run_cmd(self, script_body, env_overrides=None, bin_dir=None):
        overrides = {
            "KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env),
            "KUBE_AGENTS_INSTALL_REPORT_FILE": str(self._test_report_file),
            # Set, and set empty: install.sh only falls back to the checkout's
            # k8s-operator/scripts/vars.sh when this name is unset entirely. That
            # file is untracked, so CI never has one and a developer who has run an
            # install does -- the difference between a green suite here and there.
            "KUBE_AGENTS_LEGACY_VARS_FILE": "",
        }
        overrides.update(env_overrides or {})
        full_env = get_isolated_test_env(overrides=overrides, bin_dir=bin_dir)
        full_script = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"
source "{_INSTALLER_COMMON}"
trap - ERR
{script_body}
"""
        return subprocess.run(
            ["bash", "-c", full_script],
            capture_output=True,
            text=True,
            env=full_env,
            cwd=str(_REPO_ROOT),
            timeout=20,
        )

    def test_preflight_skips_on_fresh_cluster(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            called_file = pathlib.Path(tmp) / "kubectl.called"
            kubectl = bin_dir / "kubectl"
            kubectl.write_text(f"""#!/usr/bin/env bash
echo "called" > "{called_file}"
exit 1
""")
            kubectl.chmod(kubectl.stat().st_mode | stat.S_IEXEC)
            body = """
TFVARS_CREATE_CLUSTER=true check_existing_cluster_capacity_preflight "cluster" "region" "proj"
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Skipping cluster capacity preflight check (create_cluster is true)", proc.stdout)
            self.assertFalse(called_file.exists(), "kubectl was called on a create_cluster=true skip")

    def test_preflight_runs_on_existing_cluster_when_managed_by_tf_state(self):
        # On a cluster created and managed by Terraform state (TFVARS_CREATE_CLUSTER=true,
        # TFVARS_CLUSTER_EXISTS=true), Day-2 apply must preflight capacity because the pool
        # is fixed in size and cannot auto-grow for new workloads (#1297).
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            called_file = pathlib.Path(tmp) / "kubectl.called"
            nodes_json = json.dumps({
                "items": [{
                    "metadata": {"name": "node-1"},
                    "spec": {"taints": []},
                    "status": {
                        "allocatable": {"cpu": "4000m", "memory": "8Gi"}
                    }
                }]
            })
            pods_json = json.dumps({
                "items": [{
                    "metadata": {"name": "system-pod"},
                    "spec": {
                        "nodeName": "node-1",
                        "containers": [{
                            "resources": {"requests": {"cpu": "200m", "memory": "500Mi"}}
                        }]
                    }
                }]
            })
            kubectl = bin_dir / "kubectl"
            kubectl.write_text(f"""#!/usr/bin/env bash
echo "called" >> "{called_file}"
case "$*" in
  *nodes*-o*json*) cat << 'EOF'
{nodes_json}
EOF
  ;;
  *pods*-o*json*) cat << 'EOF'
{pods_json}
EOF
  ;;
esac
exit 0
""")
            kubectl.chmod(kubectl.stat().st_mode | stat.S_IEXEC)
            gcloud = bin_dir / "gcloud"
            gcloud.write_text("""#!/usr/bin/env bash
case "$*" in
  *node-pools*list*) cat << 'EOF'
[]
EOF
  ;;
  *) exit 1 ;;
esac
exit 0
""")
            gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
            body = """
TFVARS_CREATE_CLUSTER=true TFVARS_CLUSTER_EXISTS=true TFVARS_CLUSTER_MODE=standard \\
  check_existing_cluster_capacity_preflight "cluster" "region" "proj" "true" "file" "false" "" ""
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
            self.assertNotIn("Skipping cluster capacity preflight check (create_cluster is true)", proc.stdout)
            self.assertIn("Cluster capacity preflight check passed", proc.stdout)
            self.assertTrue(called_file.exists(), "kubectl was not called on existing cluster in tf state")

    def test_preflight_skips_on_autopilot(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            called_file = pathlib.Path(tmp) / "kubectl.called"
            kubectl = bin_dir / "kubectl"
            kubectl.write_text(f"""#!/usr/bin/env bash
echo "called" > "{called_file}"
exit 1
""")
            kubectl.chmod(kubectl.stat().st_mode | stat.S_IEXEC)
            body = """
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=autopilot check_existing_cluster_capacity_preflight "cluster" "region" "proj"
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Skipping cluster capacity preflight check (cluster_mode is not standard)", proc.stdout)
            self.assertFalse(called_file.exists(), "kubectl was called on an autopilot skip")

    def test_preflight_skips_when_skip_capacity_check_is_true(self):
        body = f"""
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard SKIP_CAPACITY_CHECK=true check_existing_cluster_capacity_preflight "cluster" "region" "proj"
"""
        proc = self._run_cmd(body)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Skipping cluster capacity preflight check", proc.stdout)

    def test_preflight_passes_with_sufficient_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            nodes_json = json.dumps({
                "items": [{
                    "metadata": {"name": "node-1"},
                    "spec": {"taints": []},
                    "status": {
                        "allocatable": {"cpu": "4000m", "memory": "8Gi"}
                    }
                }]
            })
            pods_json = json.dumps({
                "items": [{
                    "metadata": {"name": "system-pod"},
                    "spec": {
                        "nodeName": "node-1",
                        "containers": [{
                            "resources": {"requests": {"cpu": "200m", "memory": "500Mi"}}
                        }]
                    }
                }]
            })
            bin_dir = self._preflight_bin_dir(tmp, nodes_json, pods_json)
            body = f"""
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard \\
  check_existing_cluster_capacity_preflight "cluster" "region" "proj" "true" "file" "false" "" ""
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
            self.assertIn("Cluster capacity preflight check passed", proc.stdout)

    def test_preflight_fails_fast_with_insufficient_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 1 node with 940m CPU, pods already using 850m -> only 90m free (< required CPU)
            nodes_json = json.dumps({
                "items": [{
                    "metadata": {"name": "node-small"},
                    "spec": {"taints": []},
                    "status": {
                        "allocatable": {"cpu": "940m", "memory": "2Gi"}
                    }
                }]
            })
            pods_json = json.dumps({
                "items": [{
                    "metadata": {"name": "workload-1"},
                    "spec": {
                        "nodeName": "node-small",
                        "containers": [{
                            "resources": {"requests": {"cpu": "850m", "memory": "1500Mi"}}
                        }]
                    }
                }]
            })
            bin_dir = self._preflight_bin_dir(tmp, nodes_json, pods_json)
            body = f"""
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard \\
  check_existing_cluster_capacity_preflight "cluster" "region" "proj" "true" "file" "false" "" ""
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            self.assertEqual(proc.returncode, 1, f"Expected preflight failure, got code {proc.returncode}")
            self.assertIn("Cluster capacity preflight check failed", proc.stdout)
            self.assertIn("Insufficient schedulable CPU", proc.stdout)

    def test_preflight_ignores_tainted_gvisor_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            # node-1 is gVisor tainted; node-2 is untainted but has zero free capacity
            nodes_json = json.dumps({
                "items": [
                    {
                        "metadata": {"name": "node-gvisor"},
                        "spec": {"taints": [{"key": "sandbox.gke.io/runtime", "value": "gvisor", "effect": "NoSchedule"}]},
                        "status": {"allocatable": {"cpu": "8000m", "memory": "32Gi"}}
                    },
                    {
                        "metadata": {"name": "node-default"},
                        "spec": {"taints": []},
                        "status": {"allocatable": {"cpu": "500m", "memory": "1Gi"}}
                    }
                ]
            })
            pods_json = json.dumps({
                "items": [{
                    "metadata": {"name": "busy-pod"},
                    "spec": {
                        "nodeName": "node-default",
                        "containers": [{"resources": {"requests": {"cpu": "450m", "memory": "800Mi"}}}]
                    }
                }]
            })
            bin_dir = self._preflight_bin_dir(tmp, nodes_json, pods_json)
            body = f"""
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard \\
  check_existing_cluster_capacity_preflight "cluster" "region" "proj" "true" "file" "false" "" ""
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            self.assertEqual(proc.returncode, 1)
            self.assertIn("Cluster capacity preflight check failed", proc.stdout)
            self.assertIn("1 untainted node(s)", proc.stdout)

    def test_preflight_ignores_cordoned_and_not_ready_nodes(self):
        # Self-Review finding 5: nodes with spec.unschedulable=true or with a
        # Ready condition of False/Unknown must not be counted as available capacity.
        # Node 1 is cordoned (unschedulable) and Node 2 is NotReady, each with ample
        # capacity; Node 3 is ready and untainted but has only 500m/1Gi, with 450m/800Mi
        # consumed by a pod. The preflight must exclude both cordoned/unready nodes,
        # find only 1 untainted schedulable node, and fail on insufficient capacity.
        with tempfile.TemporaryDirectory() as tmp:
            nodes_json = json.dumps({
                "items": [
                    {
                        "metadata": {"name": "node-cordoned"},
                        "spec": {"unschedulable": True, "taints": []},
                        "status": {
                            "conditions": [{"type": "Ready", "status": "True"}],
                            "allocatable": {"cpu": "8000m", "memory": "32Gi"},
                        },
                    },
                    {
                        "metadata": {"name": "node-notready"},
                        "spec": {"taints": []},
                        "status": {
                            "conditions": [{"type": "Ready", "status": "False"}],
                            "allocatable": {"cpu": "8000m", "memory": "32Gi"},
                        },
                    },
                    {
                        "metadata": {"name": "node-ready"},
                        "spec": {"taints": []},
                        "status": {
                            "conditions": [{"type": "Ready", "status": "True"}],
                            "allocatable": {"cpu": "500m", "memory": "1Gi"},
                        },
                    },
                ]
            })
            pods_json = json.dumps({
                "items": [{
                    "metadata": {"name": "busy-pod"},
                    "spec": {
                        "nodeName": "node-ready",
                        "containers": [{"resources": {"requests": {"cpu": "450m", "memory": "800Mi"}}}],
                    },
                }]
            })
            bin_dir = self._preflight_bin_dir(tmp, nodes_json, pods_json)
            body = """
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard \\
  check_existing_cluster_capacity_preflight "cluster" "region" "proj" "true" "file" "false" "" ""
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            self.assertEqual(proc.returncode, 1)
            self.assertIn("Cluster capacity preflight check failed", proc.stdout)
            self.assertIn("1 untainted node(s)", proc.stdout)

    def test_preflight_warns_rather_than_passes_when_the_node_json_is_truncated(self):
        """A check that could not read its input has not passed.

        The `[ ! -s ]` guard rejects an empty node document, not a partial one,
        and the evaluator answers a partial one with {"error": ...} and exit 0.
        Read with a default, that document reports a pass over zero nodes --
        the same shape the working path fails hard on ("No untainted nodes
        found in cluster").
        """
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            kubectl = bin_dir / "kubectl"
            kubectl.write_text("""#!/usr/bin/env bash
case "$*" in
  *nodes*-o*json*) printf '%s' '{"items": [{"metadata": {"nam' ;;
  *pods*-o*json*) printf '%s' '{"items": []}' ;;
esac
exit 0
""")
            kubectl.chmod(kubectl.stat().st_mode | stat.S_IEXEC)
            gcloud = bin_dir / "gcloud"
            gcloud.write_text("#!/usr/bin/env bash\nexit 0\n")
            gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
            body = f"""
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard \\
  check_existing_cluster_capacity_preflight "cluster" "region" "proj" "true" "file" "false" "" ""
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            combined = proc.stdout + proc.stderr
            self.assertEqual(proc.returncode, 0, combined)
            self.assertIn("Failed to calculate cluster schedulable capacity", combined)
            self.assertNotIn("Cluster capacity preflight check passed", combined)

    def test_flags_helm_timeout_and_skip_capacity_check(self):
        body = """
parse_args --helm-timeout=720 --skip-capacity-check
echo "TIMEOUT=$PARAM_HELM_TIMEOUT"
echo "SKIP=$PARAM_SKIP_CAPACITY_CHECK"
"""
        proc = self._run_cmd(body)
        self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
        self.assertIn("TIMEOUT=720", proc.stdout)
        self.assertIn("SKIP=true", proc.stdout)

    def test_diagnose_rollout_failure_prints_pending_pods_on_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            kubectl = bin_dir / "kubectl"
            kubectl.write_text("""#!/usr/bin/env bash
case "$*" in
  *get*pods*) echo "litellm-abc" ;;
  *get*events*) echo "FailedScheduling 0/2 nodes available: Insufficient cpu." ;;
esac
exit 0
""")
            kubectl.chmod(kubectl.stat().st_mode | stat.S_IEXEC)
            log_file = pathlib.Path(tmp) / "prov.log"
            log_file.write_text(
                "Error: context deadline exceeded\n"
                "\n"
                "  with helm_release.kube_agents,\n"
                '  on main.tf line 497, in resource "helm_release" "kube_agents":\n'
            )
            body = f"""
diagnose_rollout_failure "{log_file}"
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
            self.assertIn("Helm rollout timed out waiting for Kubernetes workloads", proc.stdout)
            self.assertIn("litellm-abc", proc.stdout)
            self.assertIn("Insufficient cpu", proc.stdout)

    def test_diagnose_rollout_failure_ignores_a_timeout_from_another_provider(self):
        # "context deadline exceeded" and "timed out waiting" are not Helm's.
        # The Google provider prints both for its own long API calls, so a
        # cluster creation that ran out of time carries the same phrase. What
        # separates them is the resource terraform attributes the error to.
        # Without this the diagnoser announces a Helm rollout failure and then
        # queries a cluster the apply never finished building.
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            called_file = pathlib.Path(tmp) / "kubectl.called"
            kubectl = bin_dir / "kubectl"
            kubectl.write_text(f"""#!/usr/bin/env bash
echo "called" > "{called_file}"
exit 1
""")
            kubectl.chmod(kubectl.stat().st_mode | stat.S_IEXEC)
            log_file = pathlib.Path(tmp) / "prov.log"
            log_file.write_text(
                "Error: timed out waiting for the condition\n"
                "\n"
                "  with google_container_cluster.primary,\n"
                '  on main.tf line 120, in resource "google_container_cluster" "primary":\n'
            )
            body = f"""
diagnose_rollout_failure "{log_file}"
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
            self.assertNotIn("Helm rollout timed out", proc.stdout)
            self.assertFalse(called_file.exists(), "kubectl was called when diagnosing a provider timeout")

    def test_diagnose_rollout_failure_ignores_a_node_pool_timeout_in_a_real_apply_log(self):
        # The log the predicate actually reads is the tee of `./lifecycle.sh
        # apply`, and lifecycle.sh runs `terraform apply` with no saved plan, so
        # terraform prints the refresh and the plan into it first. Both
        # helm_release addresses are therefore in the log of every apply the
        # installer runs, whether or not either release started -- which a
        # substring test for "helm_release" cannot tell apart from a rollout
        # that timed out. The error block's attribution line can: terraform
        # prints "with <address>," only under the error it raised.
        #
        # The case that matters is this one: the gVisor node pool is created by
        # the same apply, and the Google provider says "context deadline
        # exceeded" when it runs long. The fixtures above cannot catch it --
        # they are four-line error blocks with no plan above them, a shape
        # terraform does not produce.
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            called_file = pathlib.Path(tmp) / "kubectl.called"
            kubectl = bin_dir / "kubectl"
            kubectl.write_text(f"""#!/usr/bin/env bash
echo "called" > "{called_file}"
exit 1
""")
            kubectl.chmod(kubectl.stat().st_mode | stat.S_IEXEC)
            log_file = pathlib.Path(tmp) / "prov.log"
            log_file.write_text(
                "module.gke_cluster.google_container_cluster.primary: Refreshing state... "
                "[id=projects/p/locations/us-east4/clusters/platform-agent-host]\n"
                "helm_release.cert_manager: Refreshing state... [id=cert-manager]\n"
                "helm_release.kube_agents: Refreshing state... [id=kube-agents]\n"
                "\n"
                "Terraform used the selected providers to generate the following execution\n"
                "plan. Resource actions are indicated with the following symbols:\n"
                "  + create\n"
                "\n"
                "Terraform will perform the following actions:\n"
                "\n"
                "  # google_container_node_pool.gvisor_pool will be created\n"
                '  + resource "google_container_node_pool" "gvisor_pool" {\n'
                '      + name = "gvisor-pool"\n'
                "    }\n"
                "\n"
                "  # helm_release.kube_agents will be updated in-place\n"
                '  ~ resource "helm_release" "kube_agents" {\n'
                '      ~ version = "0.1.0" -> "0.2.0"\n'
                "    }\n"
                "\n"
                "Plan: 1 to add, 1 to change, 0 to destroy.\n"
                "google_container_node_pool.gvisor_pool: Creating...\n"
                "google_container_node_pool.gvisor_pool: Still creating... [10m0s elapsed]\n"
                "\n"
                "Error: context deadline exceeded\n"
                "\n"
                "  with google_container_node_pool.gvisor_pool,\n"
                '  on main.tf line 284, in resource "google_container_node_pool" "gvisor_pool":\n'
                "  284: resource \"google_container_node_pool\" \"gvisor_pool\" {\n"
            )
            body = f"""
diagnose_rollout_failure "{log_file}"
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
            self.assertNotIn(
                "Helm rollout timed out",
                proc.stdout,
                "a node-pool timeout in an apply log that names both helm_release "
                "resources in its plan was diagnosed as a Helm rollout failure",
            )
            self.assertFalse(called_file.exists(), "kubectl was called when diagnosing a node-pool timeout")

    def test_helm_rollout_timed_out_still_fires_on_a_release_timeout_in_a_real_apply_log(self):
        # The mirror of the test above: the same plan and refresh preamble, with
        # the error attributed to the release instead. Without this, narrowing
        # the discriminator could pass by rejecting everything.
        with tempfile.TemporaryDirectory() as tmp:
            log_file = pathlib.Path(tmp) / "prov.log"
            log_file.write_text(
                "helm_release.cert_manager: Refreshing state... [id=cert-manager]\n"
                "\n"
                "  # helm_release.kube_agents will be created\n"
                '  + resource "helm_release" "kube_agents" {\n'
                "    }\n"
                "\n"
                "Plan: 1 to add, 0 to change, 0 to destroy.\n"
                "helm_release.kube_agents: Creating...\n"
                "helm_release.kube_agents: Still creating... [10m0s elapsed]\n"
                "\n"
                "Error: context deadline exceeded\n"
                "\n"
                "  with helm_release.kube_agents,\n"
                '  on main.tf line 497, in resource "helm_release" "kube_agents":\n'
            )
            body = f"""
if helm_rollout_timed_out "{log_file}"; then echo "PREDICATE=true"; else echo "PREDICATE=false"; fi
"""
            proc = self._run_cmd(body)
            self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
            self.assertIn("PREDICATE=true", proc.stdout)

    def test_helm_rollout_timed_out_matches_real_colored_boxed_terraform_error(self):
        # Under `lifecycle.sh apply | tee`, Terraform's default diagnostic formatter
        # emits boxed error blocks with ANSI color sequences and `│` box-drawing rules:
        # `\x1b[31m│\x1b[0m   with helm_release.kube_agents,`. The predicate must match
        # this shape so rollout diagnosis fires under real interactive runs.
        with tempfile.TemporaryDirectory() as tmp:
            log_file = pathlib.Path(tmp) / "prov.log"
            log_file.write_text(
                "helm_release.cert_manager: Refreshing state... [id=cert-manager]\n"
                "\n"
                "\x1b[31m╷\x1b[0m\x1b[0m\n"
                "\x1b[31m│\x1b[0m \x1b[1m\x1b[31mError: \x1b[0m\x1b[1mcontext deadline exceeded\x1b[0m\n"
                "\x1b[31m│\x1b[0m \n"
                "\x1b[31m│\x1b[0m   with helm_release.kube_agents,\n"
                "\x1b[31m│\x1b[0m   on main.tf line 497, in resource \"helm_release\" \"kube_agents\":\n"
                "\x1b[31m│\x1b[0m  497: resource \"helm_release\" \"kube_agents\" {\n"
                "\x1b[31m╵\x1b[0m\x1b[0m\n"
            )
            body = f"""
if helm_rollout_timed_out "{log_file}"; then echo "PREDICATE=true"; else echo "PREDICATE=false"; fi
"""
            proc = self._run_cmd(body)
            self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
            self.assertIn("PREDICATE=true", proc.stdout)

    def test_helm_rollout_timed_out_does_not_fire_on_colored_boxed_node_pool_error(self):
        # Negative counterpart to ensure ANSI / rule-prefix tolerance does not
        # match non-Helm resources that failed with context deadline exceeded.
        with tempfile.TemporaryDirectory() as tmp:
            log_file = pathlib.Path(tmp) / "prov.log"
            log_file.write_text(
                "helm_release.kube_agents: Refreshing state... [id=kube-agents]\n"
                "\n"
                "\x1b[31m╷\x1b[0m\x1b[0m\n"
                "\x1b[31m│\x1b[0m \x1b[1m\x1b[31mError: \x1b[0m\x1b[1mcontext deadline exceeded\x1b[0m\n"
                "\x1b[31m│\x1b[0m \n"
                "\x1b[31m│\x1b[0m   with google_container_node_pool.gvisor_pool,\n"
                "\x1b[31m│\x1b[0m   on main.tf line 284, in resource \"google_container_node_pool\" \"gvisor_pool\":\n"
                "\x1b[31m╵\x1b[0m\x1b[0m\n"
            )
            body = f"""
if helm_rollout_timed_out "{log_file}"; then echo "PREDICATE=true"; else echo "PREDICATE=false"; fi
"""
            proc = self._run_cmd(body)
            self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
            self.assertIn("PREDICATE=false", proc.stdout)

    def test_helm_rollout_timed_out_distinguishes_split_errors_across_different_blocks(self):
        # Proves that helm_rollout_timed_out enforces the same-block rule and does
        # not falsely trigger when "with helm_release." appears in one error block
        # (for a non-timeout failure) and "context deadline exceeded" appears in a
        # separate error block (for a non-Helm resource). A naive whole-file grep
        # would match both and falsely diagnose a rollout failure.
        with tempfile.TemporaryDirectory() as tmp:
            log_file = pathlib.Path(tmp) / "prov.log"
            log_file.write_text(
                "╷\n"
                "│ Error: Resource creation failed\n"
                "│ \n"
                "│   with helm_release.kube_agents,\n"
                '│   on main.tf line 497, in resource "helm_release" "kube_agents":\n'
                "╵\n"
                "╷\n"
                "│ Error: context deadline exceeded\n"
                "│ \n"
                "│   with google_container_node_pool.gvisor_pool,\n"
                '│   on main.tf line 284, in resource "google_container_node_pool" "gvisor_pool":\n'
                "╵\n"
            )
            body = f"""
if helm_rollout_timed_out "{log_file}"; then echo "PREDICATE=true"; else echo "PREDICATE=false"; fi
"""
            proc = self._run_cmd(body)
            self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
            self.assertIn("PREDICATE=false", proc.stdout)

    def test_helm_rollout_timed_out_survives_a_warning_nested_in_the_same_box(self):
        # Terraform nests provider warnings in a diagnostic's detail text. The
        # matcher used to clear its state on any Error:/Warning: line, so a
        # warning between the attribution and the timeout phrase wiped the
        # attribution of the very rollout being reported -- and the installer
        # printed a bare `context deadline exceeded` with no diagnosis.
        with tempfile.TemporaryDirectory() as tmp:
            log_file = pathlib.Path(tmp) / "prov.log"
            log_file.write_text(
                "╷\n"
                "│ Error: context deadline exceeded\n"
                "│ \n"
                "│   with helm_release.kube_agents,\n"
                '│   on main.tf line 497, in resource "helm_release" "kube_agents":\n'
                "│ \n"
                "│ Warning: Helm release created but has a failed status.\n"
                "│ timed out waiting for the condition\n"
                "╵\n"
            )
            body = f"""
if helm_rollout_timed_out "{log_file}"; then echo "PREDICATE=true"; else echo "PREDICATE=false"; fi
"""
            proc = self._run_cmd(body)
            self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
            self.assertIn("PREDICATE=true", proc.stdout)

    def test_helm_rollout_timed_out_does_not_pair_a_closed_box_with_a_later_timeout(self):
        # The closing ╵ ends a diagnostic. A non-timeout helm_release error
        # followed by an unboxed timeout from somewhere else is two failures,
        # and resetting only on the opening ╷ would have joined them.
        with tempfile.TemporaryDirectory() as tmp:
            log_file = pathlib.Path(tmp) / "prov.log"
            log_file.write_text(
                "╷\n"
                "│ Error: Resource creation failed\n"
                "│ \n"
                "│   with helm_release.kube_agents,\n"
                '│   on main.tf line 497, in resource "helm_release" "kube_agents":\n'
                "╵\n"
                "\n"
                "Terraform: waiting for the node pool: timed out waiting for the condition\n"
            )
            body = f"""
if helm_rollout_timed_out "{log_file}"; then echo "PREDICATE=true"; else echo "PREDICATE=false"; fi
"""
            proc = self._run_cmd(body)
            self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
            self.assertIn("PREDICATE=false", proc.stdout)

    def test_helm_timeout_rejects_non_positive_integers(self):
        # Drives install.sh's own validator. The previous version of this test
        # re-implemented the regex in the bash it ran and asserted on its own
        # echo, so deleting the check in install.sh left it green.
        for invalid_val in ["0", "-10", "abc", "12m", "600.5"]:
            with self.subTest(value=invalid_val):
                proc = self._run_cmd(f'validate_helm_timeout "{invalid_val}"')
                self.assertNotEqual(
                    proc.returncode,
                    0,
                    f"install.sh: expected --helm-timeout='{invalid_val}' to be rejected",
                )
                self.assertIn("positive integer", proc.stdout + proc.stderr)

    def test_helm_timeout_rejects_values_outside_the_hindsight_window(self):
        # 539 aborts a cold hindsight-api roll that is loading normally; 900 is
        # the Deployment's own progressDeadlineSeconds, at which helm stops
        # waiting whatever it was asked for. tests/test_hindsight_probes.py
        # holds both numbers against the manifest they come from.
        for out_of_range in ["1", "300", "539", "900", "1800", "99999999999999999999999999999999"]:
            with self.subTest(value=out_of_range):
                proc = self._run_cmd(f'validate_helm_timeout "{out_of_range}"')
                self.assertNotEqual(
                    proc.returncode,
                    0,
                    f"install.sh: expected --helm-timeout='{out_of_range}' to be out of range",
                )

    def test_helm_timeout_accepts_the_window_and_an_unset_value(self):
        for valid_val in ["540", "600", "899", ""]:
            with self.subTest(value=valid_val):
                proc = self._run_cmd(f'validate_helm_timeout "{valid_val}"')
                self.assertEqual(
                    proc.returncode,
                    0,
                    f"install.sh: expected --helm-timeout='{valid_val}' to be accepted, "
                    f"stdout: {proc.stdout} stderr: {proc.stderr}",
                )

    def test_the_installer_window_is_the_terraform_variable_window(self):
        # install.sh validates --helm-timeout and then writes the value into
        # the full-install composition's helm_timeout variable, whose own
        # validation block repeats the bounds. Nothing holds the two copies
        # together. Raise one and miss the other and the installer accepts a
        # value terraform rejects -- which surfaces at apply, after the
        # tfvars are written and the cluster has been mutated. The three tests
        # above pin the installer's behaviour to literals; this one pins those
        # literals to the file they have to agree with.
        bounds = terraform_gate_bounds_seconds()
        self.assertIsNotNone(
            bounds,
            "the helm_timeout tfvar lost its validation block, so the installer's "
            "window is the only check on a value terraform will still accept",
        )
        floor, ceiling = bounds
        for value, accepted in (
            (floor - 1, False),
            (floor, True),
            (ceiling, True),
            (ceiling + 1, False),
        ):
            with self.subTest(value=value, accepted=accepted):
                proc = self._run_cmd(f'validate_helm_timeout "{value}"')
                if accepted:
                    self.assertEqual(
                        proc.returncode,
                        0,
                        f"terraform admits --helm-timeout={value} and install.sh "
                        f"rejects it: {proc.stdout} {proc.stderr}",
                    )
                else:
                    self.assertNotEqual(
                        proc.returncode,
                        0,
                        f"install.sh accepts --helm-timeout={value}, which the "
                        "helm_timeout variable's validation block rejects at apply",
                    )

    def test_capacity_preflight_failure_aborts_a_non_interactive_run(self):
        body = """
check_existing_cluster_capacity_preflight() { return 1; }
write_json_report() { echo "REPORT_STATUS=$1"; }
PARAM_NON_INTERACTIVE=true PARAM_DRY_RUN=false \\
  enforce_capacity_preflight "cluster" "region" "proj" "true" "file" "false" "" ""
"""
        proc = self._run_cmd(body)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Aborting before Terraform apply", proc.stdout + proc.stderr)

    def test_capacity_refusal_writes_a_machine_readable_status(self):
        # The agent-facing contract: an aborted run leaves a report describing
        # this run, not whatever the last successful install wrote.
        body = """
check_existing_cluster_capacity_preflight() { return 1; }
write_json_report() { echo "REPORT_STATUS=$1"; }
PARAM_NON_INTERACTIVE=true PARAM_DRY_RUN=false \\
  enforce_capacity_preflight "cluster" "region" "proj" "true" "file" "false" "" ""
"""
        proc = self._run_cmd(body)
        self.assertIn("REPORT_STATUS=REFUSED_INSUFFICIENT_CAPACITY", proc.stdout)

    def test_declining_the_capacity_prompt_pauses_rather_than_fails(self):
        body = """
check_existing_cluster_capacity_preflight() { return 1; }
write_json_report() { echo "REPORT_STATUS=$1"; }
has_controlling_tty() { return 0; }
prompt_read() { eval "$2=n"; }
PARAM_NON_INTERACTIVE=false PARAM_DRY_RUN=false \\
  enforce_capacity_preflight "cluster" "region" "proj" "true" "file" "false" "" ""
"""
        proc = self._run_cmd(body)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("REPORT_STATUS=PAUSED", proc.stdout)

    def test_capacity_preflight_failure_refuses_when_no_controlling_tty(self):
        body = """
check_existing_cluster_capacity_preflight() { return 1; }
write_json_report() { echo "REPORT_STATUS=$1"; }
has_controlling_tty() { return 1; }
PARAM_NON_INTERACTIVE=false PARAM_DRY_RUN=false \\
  enforce_capacity_preflight "cluster" "region" "proj" "true" "file" "false" "" ""
"""
        proc = self._run_cmd(body)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("REPORT_STATUS=REFUSED_INSUFFICIENT_CAPACITY", proc.stdout)

    def test_capacity_preflight_failure_only_warns_on_a_dry_run(self):
        body = """
check_existing_cluster_capacity_preflight() { return 1; }
write_json_report() { echo "REPORT_STATUS=$1"; }
PARAM_NON_INTERACTIVE=true PARAM_DRY_RUN=true \\
  enforce_capacity_preflight "cluster" "region" "proj" "true" "file" "false" "" ""
"""
        proc = self._run_cmd(body)
        self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
        self.assertIn("continuing dry-run validation", proc.stdout + proc.stderr)
        self.assertNotIn("REPORT_STATUS=", proc.stdout)

    def test_enforce_capacity_preflight_honours_tfvars_enable_cert_manager(self):
        body = """
check_existing_cluster_capacity_preflight() {
  echo "CERT_MGR_ARG=$9"
  return 0
}
TFVARS_ENABLE_CERT_MANAGER=false enforce_capacity_preflight "cluster" "region" "proj" "true" "file" "false" "" ""
TFVARS_ENABLE_CERT_MANAGER=true enforce_capacity_preflight "cluster" "region" "proj" "true" "file" "false" "" ""
unset TFVARS_ENABLE_CERT_MANAGER
enforce_capacity_preflight "cluster" "region" "proj" "true" "file" "false" "" ""
"""
        proc = self._run_cmd(body)
        self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
        lines = [line for line in proc.stdout.splitlines() if "CERT_MGR_ARG=" in line]
        self.assertEqual(lines, ["CERT_MGR_ARG=false", "CERT_MGR_ARG=true", "CERT_MGR_ARG=true"])

    def _preflight_bin_dir(self, tmp, nodes_json, pods_json, pools_json="[]"):
        """A bin dir whose kubectl and gcloud answer with the given cluster state.

        gcloud is always stubbed, including when the pool list is empty: the
        preflight shells out to it now, and a real gcloud on PATH would make
        the test depend on the developer's credentials.
        """
        bin_dir = pathlib.Path(tmp) / "bin"
        bin_dir.mkdir()
        kubectl = bin_dir / "kubectl"
        kubectl.write_text(f"""#!/usr/bin/env bash
case "$*" in
  *nodes*-o*json*) cat << 'EOF'
{nodes_json}
EOF
  ;;
  *pods*-o*json*) cat << 'EOF'
{pods_json}
EOF
  ;;
esac
exit 0
""")
        kubectl.chmod(kubectl.stat().st_mode | stat.S_IEXEC)
        gcloud = bin_dir / "gcloud"
        gcloud.write_text(f"""#!/usr/bin/env bash
case "$*" in
  *node-pools*list*) cat << 'EOF'
{pools_json}
EOF
  ;;
  *) exit 1 ;;
esac
exit 0
""")
        gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
        return bin_dir

    # One 2000m/4096Mi node, which is short of the hindsight profile on its own.
    _ONE_SMALL_NODE = json.dumps({
        "items": [{
            "metadata": {"name": "n1", "labels": {"cloud.google.com/gke-nodepool": "default-pool"}},
            "spec": {"taints": []},
            "status": {
                "allocatable": {"cpu": "2000m", "memory": "4096Mi"},
                "conditions": [{"type": "Ready", "status": "True"}],
            },
        }]
    })

    def test_preflight_credits_headroom_an_autoscaling_pool_can_still_add(self):
        # The regression this guards: a pool that scales 1 -> 5 answers a
        # deficit by itself, and blocking the install on the one node running
        # right now fails a cluster that was never going to be short.
        pools = json.dumps([{
            "name": "default-pool",
            "autoscaling": {"enabled": True, "maxNodeCount": 5},
            "locations": ["us-central1-a"],
            "config": {},
        }])
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._preflight_bin_dir(
                tmp, self._ONE_SMALL_NODE, json.dumps({"items": []}), pools)
            body = f"""
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard check_existing_cluster_capacity_preflight "cluster" "region" "proj" "false" "hindsight" "false" "" ""
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
            self.assertIn("autoscaling headroom", proc.stdout + proc.stderr)

    def test_preflight_still_fails_when_the_pool_is_at_its_ceiling(self):
        pools = json.dumps([{
            "name": "default-pool",
            "autoscaling": {"enabled": True, "maxNodeCount": 1},
            "locations": ["us-central1-a"],
            "config": {},
        }])
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._preflight_bin_dir(
                tmp, self._ONE_SMALL_NODE, json.dumps({"items": []}), pools)
            body = f"""
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard check_existing_cluster_capacity_preflight "cluster" "region" "proj" "false" "hindsight" "false" "" ""
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            self.assertEqual(proc.returncode, 1, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
            self.assertNotIn("autoscaling headroom", proc.stdout + proc.stderr)

    def test_preflight_counts_native_sidecar_requests_against_a_node(self):
        # A restartable init container runs for the life of the pod. Treating
        # it as a phase that ends hides its request, and the check then reports
        # capacity the scheduler will not find.
        pods = json.dumps({
            "items": [{
                "metadata": {"name": "app", "namespace": "default"},
                "spec": {
                    "nodeName": "n1",
                    "containers": [{"resources": {"requests": {"cpu": "100m", "memory": "128Mi"}}}],
                    "initContainers": [{
                        "restartPolicy": "Always",
                        "resources": {"requests": {"cpu": "1500m", "memory": "256Mi"}},
                    }],
                },
                "status": {"phase": "Running"},
            }]
        })
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._preflight_bin_dir(tmp, self._ONE_SMALL_NODE, pods)
            body = f"""
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard check_existing_cluster_capacity_preflight "cluster" "region" "proj" "false" "none" "false" "" ""
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            # 2000m allocatable less 100m container and 1500m sidecar leaves
            # 400m. Counting the sidecar as a finished phase would leave 500m.
            self.assertIn("400m", proc.stdout + proc.stderr)
            self.assertNotIn("500m <", proc.stdout + proc.stderr)

    def test_preflight_reads_the_spellings_a_quantity_can_arrive_in(self):
        # A quantity comes back spelled the way its author wrote it, not in a
        # canonical form, so nanocores and plain bytes are both ordinary input.
        # The parser used to raise on the first of those, and the raise left
        # the evaluator with no output at all: one tenant pod using a legal
        # unit skipped the capacity check for the whole cluster, and the only
        # trace was a warning that reads like a transient kubectl failure.
        pods = json.dumps({
            "items": [{
                "metadata": {"name": "app", "namespace": "default"},
                "spec": {
                    "nodeName": "n1",
                    "containers": [{"resources": {"requests": {
                        "cpu": "1400000000n",   # 1400m
                        "memory": "268435456",  # 256Mi, in bytes
                    }}}],
                },
                "status": {"phase": "Running"},
            }]
        })
        with tempfile.TemporaryDirectory() as tmp:
            big_node = self._ONE_SMALL_NODE.replace('"2000m"', '"4000m"').replace('"4096Mi"', '"8192Mi"')
            bin_dir = self._preflight_bin_dir(tmp, big_node, pods)
            body = f"""
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard check_existing_cluster_capacity_preflight "cluster" "region" "proj" "true" "none" "false" "" ""
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            output = proc.stdout + proc.stderr
            self.assertNotIn("Failed to calculate cluster schedulable capacity", output)
            # 4000m less 1400m, and 8192Mi less 256Mi. Reading either quantity
            # as zero would report the node's full allocatable instead.
            self.assertIn("2600m CPU", output)
            self.assertIn("7936Mi Memory", output)
            self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")

    def test_preflight_reads_allocatable_in_the_Ki_gke_reports_it_in(self):
        # Every GKE node reports status.allocatable.memory in Ki, so this is
        # the spelling the check meets on every real cluster and the one branch
        # of parse_mem that has to be right. Reading it as Mi would credit the
        # node with a thousand times its memory.
        node = json.dumps({
            "items": [{
                "metadata": {"name": "n1", "labels": {"cloud.google.com/gke-nodepool": "default-pool"}},
                "spec": {"taints": []},
                "status": {
                    "allocatable": {"cpu": "4000m", "memory": "16069588Ki"},
                    "conditions": [{"type": "Ready", "status": "True"}],
                },
            }]
        })
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._preflight_bin_dir(tmp, node, json.dumps({"items": []}))
            body = f"""
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard check_existing_cluster_capacity_preflight "cluster" "region" "proj" "true" "none" "false" "" ""
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            output = proc.stdout + proc.stderr
            # 16069588Ki is 15692Mi. Mis-scaled by 1024 it reads as either
            # 16069588Mi or 15Mi, and neither is this number.
            self.assertIn("15692Mi Memory", output)
            self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")

    def test_preflight_discounts_daemonsets_from_the_node_a_pool_would_add(self):
        # A DaemonSet lands on every node of the pool, including the one the
        # autoscaler has yet to create, so a new node offers its allocatable
        # less that pod -- not its allocatable. Crediting the full figure is
        # how this check passes a cluster the rollout then leaves Pending.
        pods = json.dumps({
            "items": [{
                "metadata": {
                    "name": "agent", "namespace": "kube-system",
                    "ownerReferences": [{"kind": "DaemonSet", "name": "agent"}],
                },
                "spec": {
                    "nodeName": "n1",
                    "containers": [{"resources": {"requests": {"cpu": "1000m", "memory": "2048Mi"}}}],
                },
                "status": {"phase": "Running"},
            }]
        })
        pools = json.dumps([{
            "name": "default-pool",
            "autoscaling": {"enabled": True, "maxNodeCount": 2},
            "locations": ["us-central1-a"],
            "config": {},
        }])
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._preflight_bin_dir(tmp, self._ONE_SMALL_NODE, pods, pools)
            body = f"""
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard check_existing_cluster_capacity_preflight "cluster" "region" "proj" "true" "none" "false" "" ""
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            # The one node is 2000m/4096Mi and the DaemonSet takes half of it.
            # A second node discounted the same way brings the total to
            # 2000m/4096Mi, still short. Credited undiscounted it would reach
            # 3000m/6144Mi, clear the requirement, and pass on headroom.
            self.assertEqual(proc.returncode, 1, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
            self.assertNotIn("autoscaling headroom", proc.stdout + proc.stderr)

    def test_preflight_reads_totalMaxNodeCount_as_the_ceiling(self):
        # A pool with locations set reports its ceiling as totalMaxNodeCount
        # across all of them, and maxNodeCount per zone. Reading only the
        # second refuses a regional pool that can still add nodes.
        pools = json.dumps([{
            "name": "default-pool",
            "autoscaling": {"enabled": True, "totalMaxNodeCount": 5},
            "locations": ["us-central1-a"],
            "config": {},
        }])
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._preflight_bin_dir(
                tmp, self._ONE_SMALL_NODE, json.dumps({"items": []}), pools)
            body = f"""
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard check_existing_cluster_capacity_preflight "cluster" "region" "proj" "false" "hindsight" "false" "" ""
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
            self.assertIn("autoscaling headroom", proc.stdout + proc.stderr)

    def test_diagnose_rollout_failure_guards_the_scan_that_needs_python(self):
        # The preflight warns and skips where python3 is absent; the diagnoser
        # piped into it regardless, and the pipeline's own `|| true` turned the
        # missing interpreter into an empty pod list -- a clean bill of health
        # for pods that are crash-looping. Asserted against the shell's parse
        # tree rather than a run, because hiding python3 from a test that has
        # to source install.sh first would take the rest of PATH with it: this
        # catches the guard being removed, not every way it could be wrong.
        proc = self._run_cmd("declare -f diagnose_rollout_failure")
        self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
        body = proc.stdout
        guard = body.find("command -v python3")
        self.assertNotEqual(guard, -1, "the unready-pod scan no longer checks for python3")
        self.assertLess(
            guard,
            body.index("python3 -c"),
            "the python3 check has to run before the scan that needs it",
        )



    def test_preflight_single_node_both_cpu_and_mem_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Node 1 has 2000m CPU, but only 500Mi memory
            # Node 2 has 500m CPU, but 8000Mi memory
            # Total CPU = 2500m, Total Mem = 8500Mi (>= 1790m CPU, 7584Mi Mem total)
            # Unsandboxed agent needs single node with 1250m CPU AND 2048Mi memory!
            nodes_json = json.dumps({
                "items": [
                    {
                        "metadata": {"name": "node-cpu-only"},
                        "spec": {"taints": []},
                        "status": {"allocatable": {"cpu": "2000m", "memory": "500Mi"}}
                    },
                    {
                        "metadata": {"name": "node-mem-only"},
                        "spec": {"taints": []},
                        "status": {"allocatable": {"cpu": "500m", "memory": "8000Mi"}}
                    }
                ]
            })
            pods_json = json.dumps({"items": []})
            bin_dir = self._preflight_bin_dir(tmp, nodes_json, pods_json)
            body = f"""
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard check_existing_cluster_capacity_preflight "cluster" "region" "proj" "false" "file" "false" "" ""
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            self.assertEqual(proc.returncode, 1, f"Expected preflight failure, got code {proc.returncode}")
            self.assertIn("No single untainted node has sufficient schedulable capacity", proc.stdout)

    def test_preflight_autoscaling_heterogeneous_pools_does_not_synthesize_hybrid_node(self):
        # Pool A has high CPU, low memory. Pool B has low CPU, high memory.
        # Neither pool can fit an unsandboxed agent (needs 1250m CPU AND 2560Mi mem).
        # Autoscaling headroom must not synthesize a hybrid node combining max(CPU)
        # from Pool A and max(Memory) from Pool B.
        nodes_json = json.dumps({
            "items": [
                {
                    "metadata": {"name": "node-cpu", "labels": {"cloud.google.com/gke-nodepool": "pool-cpu"}},
                    "spec": {"taints": []},
                    "status": {
                        "allocatable": {"cpu": "4000m", "memory": "1000Mi"},
                        "conditions": [{"type": "Ready", "status": "True"}],
                    },
                },
                {
                    "metadata": {"name": "node-mem", "labels": {"cloud.google.com/gke-nodepool": "pool-mem"}},
                    "spec": {"taints": []},
                    "status": {
                        "allocatable": {"cpu": "1000m", "memory": "8000Mi"},
                        "conditions": [{"type": "Ready", "status": "True"}],
                    },
                },
            ]
        })
        pools_json = json.dumps([
            {
                "name": "pool-cpu",
                "autoscaling": {"enabled": True, "maxNodeCount": 5},
                "locations": ["us-central1-a"],
                "config": {},
            },
            {
                "name": "pool-mem",
                "autoscaling": {"enabled": True, "maxNodeCount": 5},
                "locations": ["us-central1-a"],
                "config": {},
            },
        ])
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._preflight_bin_dir(tmp, nodes_json, json.dumps({"items": []}), pools_json)
            body = f"""
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard check_existing_cluster_capacity_preflight "cluster" "region" "proj" "false" "file" "false" "" ""
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            self.assertEqual(proc.returncode, 1, f"Expected preflight failure, got code {proc.returncode}: {proc.stdout}")
            self.assertIn("No single untainted node has sufficient schedulable capacity", proc.stdout)

    def test_preflight_skips_minter_charge_when_minter_deferred(self):
        # A node with 550m CPU and 4950Mi RAM fits the baseline without minter (510m CPU, 4928Mi mem),
        # but cannot fit minter (+100m CPU = 610m CPU, +128Mi mem = 5056Mi mem).
        # When TFVARS_ENABLE_GITHUB_MINTER=false, minter is not deployed and capacity check must pass.
        node_json = json.dumps({
            "items": [{
                "metadata": {"name": "n1", "labels": {"cloud.google.com/gke-nodepool": "default-pool"}},
                "spec": {"taints": []},
                "status": {
                    "allocatable": {"cpu": "550m", "memory": "4950Mi"},
                    "conditions": [{"type": "Ready", "status": "True"}],
                },
            }]
        })
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = self._preflight_bin_dir(tmp, node_json, json.dumps({"items": []}))
            # 1. When minter is deferred (TFVARS_ENABLE_GITHUB_MINTER=false), check passes
            body_deferred = f"""
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard TFVARS_ENABLE_GITHUB_MINTER=false \\
  check_existing_cluster_capacity_preflight "cluster" "region" "proj" "true" "file" "false" "my-org" "my-repo" "false"
"""
            proc_deferred = self._run_cmd(body_deferred, bin_dir=str(bin_dir))
            self.assertEqual(proc_deferred.returncode, 0, f"stdout: {proc_deferred.stdout}\nstderr: {proc_deferred.stderr}")

            # 2. When minter is enabled (default), check fails on insufficient capacity
            body_enabled = f"""
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard TFVARS_ENABLE_GITHUB_MINTER=true \\
  check_existing_cluster_capacity_preflight "cluster" "region" "proj" "true" "file" "false" "my-org" "my-repo" "false"
"""
            proc_enabled = self._run_cmd(body_enabled, bin_dir=str(bin_dir))
            self.assertEqual(proc_enabled.returncode, 1, f"stdout: {proc_enabled.stdout}\nstderr: {proc_enabled.stderr}")
            self.assertIn("Insufficient schedulable CPU", proc_enabled.stdout)

    def test_preflight_hindsight_single_pod_constraint_enforced_with_gvisor(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 3 nodes with 1500m CPU and 5000Mi memory each -> total 4500m CPU
            # But hindsight-api single pod requires 2000m CPU on a single node!
            nodes_json = json.dumps({
                "items": [
                    {
                        "metadata": {"name": f"node-{i}"},
                        "spec": {"taints": []},
                        "status": {"allocatable": {"cpu": "1500m", "memory": "5000Mi"}},
                    }
                    for i in range(3)
                ]
            })
            pods_json = json.dumps({"items": []})
            bin_dir = self._preflight_bin_dir(tmp, nodes_json, pods_json)
            body = f"""
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard check_existing_cluster_capacity_preflight "cluster" "region" "proj" "true" "hindsight" "false" "" ""
"""
            proc = self._run_cmd(body, bin_dir=bin_dir)
            self.assertEqual(proc.returncode, 1, f"Expected failure due to hindsight-api 2000m single-pod requirement, got {proc.returncode}")
            self.assertIn("No single untainted node has sufficient schedulable capacity", proc.stdout)

    def test_preflight_skips_when_kubectl_context_mismatches(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            kubectl = bin_dir / "kubectl"
            kubectl.write_text("""#!/usr/bin/env bash
case "$*" in
  *config*current-context*) echo "gke_other-proj_other-region_other-cluster" ;;
esac
exit 0
""")
            kubectl.chmod(kubectl.stat().st_mode | stat.S_IEXEC)
            gcloud = bin_dir / "gcloud"
            gcloud.write_text("""#!/usr/bin/env bash
exit 0
""")
            gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
            body = f"""
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard check_existing_cluster_capacity_preflight "cluster" "region" "proj" "true" "file" "false" "" ""
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            self.assertEqual(proc.returncode, 0)
            self.assertIn("does not match target cluster", proc.stdout)

    def test_preflight_multidimensional_pods_fit_separate_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Node 1 fits hindsight-api (2000m CPU, 1024Mi mem): has 3000m CPU, 2500Mi mem (< 2560Mi needed for agent)
            # Node 2 fits unsandboxed agent (1250m CPU, 2560Mi mem): has 1800m CPU (< 2000m needed for hindsight), 8000Mi mem
            # Neither node fits a synthesized hybrid (2000m CPU AND 2560Mi mem),
            # but each pod fits on its own node. Total CPU = 4800m >= 4040m, Total Mem = 10500Mi >= 8864Mi.
            nodes_json = json.dumps({
                "items": [
                    {
                        "metadata": {"name": "node-hindsight-fit"},
                        "spec": {"taints": []},
                        "status": {"allocatable": {"cpu": "3000m", "memory": "2500Mi"}},
                    },
                    {
                        "metadata": {"name": "node-agent-fit"},
                        "spec": {"taints": []},
                        "status": {"allocatable": {"cpu": "1800m", "memory": "8000Mi"}},
                    },
                ]
            })
            pods_json = json.dumps({"items": []})
            bin_dir = self._preflight_bin_dir(tmp, nodes_json, pods_json)
            body = f"""
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard check_existing_cluster_capacity_preflight "cluster" "region" "proj" "false" "hindsight" "false" "" ""
"""
            proc = self._run_cmd(body, bin_dir=bin_dir)
            self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
            self.assertIn("Cluster capacity preflight check passed", proc.stdout)

    def test_preflight_default_litellm_single_node_fit_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 10 nodes with 100m CPU and 600Mi memory each -> total 1000m CPU, 6000Mi mem (> 540m CPU, 5024Mi mem total)
            # But LiteLLM requires 2048Mi on a single node!
            nodes_json = json.dumps({
                "items": [
                    {
                        "metadata": {"name": f"node-{i}"},
                        "spec": {"taints": []},
                        "status": {"allocatable": {"cpu": "100m", "memory": "600Mi"}},
                    } for i in range(10)
                ]
            })
            pods_json = json.dumps({"items": []})
            bin_dir = self._preflight_bin_dir(tmp, nodes_json, pods_json)
            body = f"""
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard check_existing_cluster_capacity_preflight "cluster" "region" "proj" "true" "file" "false" "" ""
"""
            proc = self._run_cmd(body, bin_dir=bin_dir)
            self.assertEqual(proc.returncode, 1, f"Expected failure due to LiteLLM 2048Mi single-pod requirement, got {proc.returncode}")
            self.assertIn("No single untainted node has sufficient schedulable capacity for LiteLLM pod", proc.stdout)

    def test_diagnose_rollout_failure_prints_unready_pods_on_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            kubectl = bin_dir / "kubectl"
            kubectl.write_text("""#!/usr/bin/env bash
case "$*" in
  *status.phase=Pending*) echo "" ;;
  *status.phase=Running*) cat << 'EOF'
{"items": [{"metadata": {"name": "agent-gateway-xyz"}, "status": {"containerStatuses": [{"ready": false}]}}]}
EOF
  ;;
  *get*events*) echo "CrashLoopBackOff Back-off 10s restarting failed container" ;;
esac
exit 0
""")
            kubectl.chmod(kubectl.stat().st_mode | stat.S_IEXEC)
            log_file = pathlib.Path(tmp) / "prov.log"
            log_file.write_text(
                "Error: timed out waiting for the condition\n"
                "\n"
                "  with helm_release.cert_manager,\n"
                '  on main.tf line 450, in resource "helm_release" "cert_manager":\n'
            )
            body = f"""
diagnose_rollout_failure "{log_file}"
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
            self.assertIn("Helm rollout timed out waiting for Kubernetes workloads", proc.stdout)
            self.assertIn("Unready / Crashing Pods Detected", proc.stdout)
            self.assertIn("agent-gateway-xyz", proc.stdout)
            self.assertIn("CrashLoopBackOff", proc.stdout)

    def test_diagnose_rollout_failure_detects_unready_native_sidecars_on_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            kubectl = bin_dir / "kubectl"
            kubectl.write_text("""#!/usr/bin/env bash
case "$*" in
  *status.phase=Pending*) echo "" ;;
  *status.phase=Running*) cat << 'EOF'
{
  "items": [
    {
      "metadata": {"name": "pod-crashing-sidecar"},
      "status": {
        "containerStatuses": [{"name": "app", "ready": true}],
        "initContainerStatuses": [{"name": "native-sidecar", "ready": false, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]
      }
    },
    {
      "metadata": {"name": "pod-completed-init"},
      "status": {
        "containerStatuses": [{"name": "app", "ready": true}],
        "initContainerStatuses": [{"name": "init-setup", "ready": false, "state": {"terminated": {"exitCode": 0, "reason": "Completed"}}}]
      }
    }
  ]
}
EOF
  ;;
  *get*events*) echo "CrashLoopBackOff Back-off restarting failed sidecar" ;;
esac
exit 0
""")
            kubectl.chmod(kubectl.stat().st_mode | stat.S_IEXEC)
            log_file = pathlib.Path(tmp) / "prov.log"
            log_file.write_text(
                "Error: timed out waiting for the condition\n"
                "\n"
                "  with helm_release.cert_manager,\n"
                '  on main.tf line 450, in resource "helm_release" "cert_manager":\n'
            )
            body = f"""
diagnose_rollout_failure "{log_file}"
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
            self.assertIn("Unready / Crashing Pods Detected", proc.stdout)
            self.assertIn("pod-crashing-sidecar", proc.stdout)
            self.assertNotIn("pod-completed-init", proc.stdout)
            self.assertIn("CrashLoopBackOff", proc.stdout)

    def _monitor_bin_dir(self, tmp, current_context, gcloud_log, polls=2):
        """Stubs that let monitor_lifecycle_rollout run to completion.

        `sleep` is the loop's clock and its exit status is the loop's only
        break: `wait` on it failing is what ends the while. Stubbing it to
        succeed `polls` times and fail on the next call (`[ "$n" -le "$polls" ]`)
        therefore runs multiple loop iterations and returns, with no background
        job to reap and no real 20s wait.

        Running multiple polls is load-bearing: it verifies that one-shot actions
        (like fetching credentials on a context mismatch) execute exactly once
        across repeated polls rather than firing per-poll.
        """
        bin_dir = pathlib.Path(tmp) / "bin"
        bin_dir.mkdir()
        counter = pathlib.Path(tmp) / "sleep.count"
        sleep_stub = bin_dir / "sleep"
        sleep_stub.write_text(f"""#!/usr/bin/env bash
n=$(cat "{counter}" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "{counter}"
[ "$n" -le {polls} ]
""")
        kubectl = bin_dir / "kubectl"
        kubectl.write_text(f"""#!/usr/bin/env bash
case "$*" in
  *current-context*) echo "{current_context}" ;;
  *) echo "" ;;
esac
exit 0
""")
        gcloud = bin_dir / "gcloud"
        gcloud.write_text(f"""#!/usr/bin/env bash
echo "$@" >> "{gcloud_log}"
exit 0
""")
        for stub in (sleep_stub, kubectl, gcloud):
            stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        return bin_dir

    def test_monitor_fetches_credentials_once_with_the_dns_flag(self):
        # The monitor refreshes credentials at most once on an adopted cluster
        # when the context matches the target cluster (or is empty), carrying
        # the DNS-endpoint flag without which get-credentials fails on a
        # private-endpoint cluster.
        with tempfile.TemporaryDirectory() as tmp:
            gcloud_log = pathlib.Path(tmp) / "gcloud.log"
            bin_dir = self._monitor_bin_dir(tmp, "gke_proj_us-central1_target", gcloud_log)
            body = """
gke_dns_endpoint_flag() { GKE_DNS_ENDPOINT_FLAG="--dns-endpoint"; }
PROJECT_ID=proj REGION=us-central1 CLUSTER_NAME=target TFVARS_CREATE_CLUSTER=false \\
  monitor_lifecycle_rollout
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
            calls = [
                line
                for line in (gcloud_log.read_text().splitlines() if gcloud_log.exists() else [])
                if "get-credentials" in line
            ]
            self.assertEqual(
                len(calls), 1, f"expected one get-credentials attempt, got: {calls}"
            )
            self.assertIn("--dns-endpoint", calls[0])
            self.assertIn("target", calls[0])

    def test_monitor_skips_credential_fetch_when_context_mismatches(self):
        # When kubectl current context points to a different cluster, the background
        # monitor must NOT fetch credentials or mutate kubeconfig.
        with tempfile.TemporaryDirectory() as tmp:
            gcloud_log = pathlib.Path(tmp) / "gcloud.log"
            bin_dir = self._monitor_bin_dir(tmp, "gke_other_proj_us-west1_other", gcloud_log)
            body = """
gke_dns_endpoint_flag() { GKE_DNS_ENDPOINT_FLAG="--dns-endpoint"; }
PROJECT_ID=proj REGION=us-central1 CLUSTER_NAME=target TFVARS_CREATE_CLUSTER=false \\
  monitor_lifecycle_rollout
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
            calls = [
                line
                for line in (gcloud_log.read_text().splitlines() if gcloud_log.exists() else [])
                if "get-credentials" in line
            ]
            self.assertEqual(
                len(calls), 0, f"expected no get-credentials on context mismatch, got: {calls}"
            )

    def test_monitor_reports_pending_pods_during_rollout(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            counter = pathlib.Path(tmp) / "sleep.count"
            sleep_stub = bin_dir / "sleep"
            sleep_stub.write_text(f"""#!/usr/bin/env bash
n=$(cat "{counter}" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "{counter}"
[ "$n" -le 1 ]
""")
            kubectl = bin_dir / "kubectl"
            kubectl.write_text("""#!/usr/bin/env bash
case "$*" in
  *current-context*)
    echo "gke_proj_us-central1_target"
    ;;
  *get*pods*status.phase=Pending*)
    echo "litellm-pending-pod"
    ;;
  *get*events*involvedObject.name=litellm-pending-pod*)
    echo "0/2 nodes available: 2 Insufficient cpu."
    ;;
  *get*pod*litellm-pending-pod*)
    echo ""
    ;;
  *)
    echo ""
    ;;
esac
exit 0
""")
            gcloud = bin_dir / "gcloud"
            gcloud.write_text("#!/usr/bin/env bash\nexit 0\n")
            for stub in (sleep_stub, kubectl, gcloud):
                stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
            body = """
gke_dns_endpoint_flag() { GKE_DNS_ENDPOINT_FLAG="--dns-endpoint"; }
PROJECT_ID=proj REGION=us-central1 CLUSTER_NAME=target TFVARS_CREATE_CLUSTER=false \\
  monitor_lifecycle_rollout
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
            self.assertIn("Pod 'litellm-pending-pod' is Pending: 0/2 nodes available: 2 Insufficient cpu.", proc.stderr)

    def test_monitor_does_not_touch_the_kubeconfig_while_the_cluster_is_being_built(self):
        # On a create-cluster run the context cannot match until terraform has
        # built the cluster, so every fetch is churn ending in a silent
        # context switch the operator did not ask for.
        with tempfile.TemporaryDirectory() as tmp:
            gcloud_log = pathlib.Path(tmp) / "gcloud.log"
            gcloud_log.write_text("")
            bin_dir = self._monitor_bin_dir(tmp, "gke_proj_us-central1_target", gcloud_log)
            body = """
gke_dns_endpoint_flag() { GKE_DNS_ENDPOINT_FLAG="--dns-endpoint"; }
PROJECT_ID=proj REGION=us-central1 CLUSTER_NAME=target TFVARS_CREATE_CLUSTER=true \\
  monitor_lifecycle_rollout
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
            self.assertNotIn("get-credentials", gcloud_log.read_text())

    def test_monitor_fetches_credentials_when_existing_cluster_managed_by_tf_state(self):
        # When a cluster already exists and is managed in Terraform state
        # (TFVARS_CREATE_CLUSTER=true and TFVARS_CLUSTER_EXISTS=true), the monitor
        # should fetch credentials rather than skipping as if the cluster were absent.
        with tempfile.TemporaryDirectory() as tmp:
            gcloud_log = pathlib.Path(tmp) / "gcloud.log"
            bin_dir = self._monitor_bin_dir(tmp, "gke_proj_us-central1_target", gcloud_log)
            body = """
gke_dns_endpoint_flag() { GKE_DNS_ENDPOINT_FLAG="--dns-endpoint"; }
PROJECT_ID=proj REGION=us-central1 CLUSTER_NAME=target TFVARS_CREATE_CLUSTER=true TFVARS_CLUSTER_EXISTS=true \\
  monitor_lifecycle_rollout
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
            calls = [
                line
                for line in (gcloud_log.read_text().splitlines() if gcloud_log.exists() else [])
                if "get-credentials" in line
            ]
            self.assertEqual(len(calls), 1, f"expected one get-credentials attempt, got: {calls}")

    def test_monitor_survives_set_u_with_the_cluster_coordinates_unset(self):
        # It runs as a background job under `set -u`, and `set -E` carries the
        # ERR trap into it: dereferencing an unset PROJECT_ID here would not
        # just lose the monitor, it would run on_error and write a FAILED
        # install report while the apply was still healthy.
        with tempfile.TemporaryDirectory() as tmp:
            gcloud_log = pathlib.Path(tmp) / "gcloud.log"
            gcloud_log.write_text("")
            bin_dir = self._monitor_bin_dir(tmp, "gke_other_proj_us-west1_other", gcloud_log)
            body = """
set -uE
unset PROJECT_ID REGION CLUSTER_NAME
on_error() { echo "ON_ERROR_RAN"; }
trap 'on_error' ERR
monitor_lifecycle_rollout
echo "MONITOR_RETURNED"
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            self.assertIn("MONITOR_RETURNED", proc.stdout)
            self.assertNotIn("ON_ERROR_RAN", proc.stdout)
            self.assertNotIn("unbound variable", proc.stderr)

    def test_install_script_source_only_re_source_is_safe(self):
        script = f"""
set -e
KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"
KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"
echo "SOURCE_OK"
"""
        proc = self._run_cmd(script)
        self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
        self.assertIn("SOURCE_OK", proc.stdout)

    def test_preflight_ignores_pods_in_install_namespace_and_cert_manager(self):
        with tempfile.TemporaryDirectory() as tmp:
            nodes_json = json.dumps({
                "items": [{
                    "metadata": {"name": "node-1"},
                    "spec": {"taints": []},
                    "status": {"allocatable": {"cpu": "1200m", "memory": "8Gi"}},
                }]
            })
            pods_json = json.dumps({
                "items": [
                    {
                        "metadata": {"name": "tenant-pod", "namespace": "default"},
                        "spec": {
                            "nodeName": "node-1",
                            "containers": [{"resources": {"requests": {"cpu": "400m", "memory": "500Mi"}}}],
                        },
                    },
                    {
                        "metadata": {"name": "existing-agent-pod", "namespace": "kubeagents-system"},
                        "spec": {
                            "nodeName": "node-1",
                            "containers": [{"resources": {"requests": {"cpu": "500m", "memory": "500Mi"}}}],
                        },
                    },
                ]
            })
            bin_dir = self._preflight_bin_dir(tmp, nodes_json, pods_json)
            body = f"""
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard TFVARS_ENABLE_GITHUB_MINTER=false NAMESPACE=kubeagents-system \\
  check_existing_cluster_capacity_preflight "cluster" "region" "proj" "true"
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            self.assertEqual(proc.returncode, 0, f"Expected preflight to pass by ignoring existing install namespace pods, stdout: {proc.stdout}\nstderr: {proc.stderr}")
            self.assertIn("Cluster capacity preflight check passed", proc.stdout)

    def test_preflight_cert_manager_ignored_when_managed_counted_when_unmanaged(self):
        with tempfile.TemporaryDirectory() as tmp:
            nodes_json = json.dumps({
                "items": [{
                    "metadata": {"name": "node-1"},
                    "spec": {"taints": []},
                    "status": {"allocatable": {"cpu": "1000m", "memory": "8Gi"}},
                }]
            })
            pods_json = json.dumps({
                "items": [
                    {
                        "metadata": {"name": "tenant-pod", "namespace": "default"},
                        "spec": {
                            "nodeName": "node-1",
                            "containers": [{"resources": {"requests": {"cpu": "400m", "memory": "500Mi"}}}],
                        },
                    },
                    {
                        "metadata": {"name": "cert-manager-pod", "namespace": "cert-manager"},
                        "spec": {
                            "nodeName": "node-1",
                            "containers": [{"resources": {"requests": {"cpu": "100m", "memory": "100Mi"}}}],
                        },
                    },
                ]
            })
            bin_dir = self._preflight_bin_dir(tmp, nodes_json, pods_json)

            # Managed cert-manager: passes because cert-manager pod is ignored and 600m >= 540m
            body_managed = f"""
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard TFVARS_ENABLE_GITHUB_MINTER=false \\
  check_existing_cluster_capacity_preflight "cluster" "region" "proj" "true" "file" "false" "" "" "true"
"""
            proc_managed = self._run_cmd(body_managed, bin_dir=str(bin_dir))
            self.assertEqual(proc_managed.returncode, 0, f"Managed cert-manager should pass: {proc_managed.stdout}\n{proc_managed.stderr}")
            self.assertIn("Cluster capacity preflight check passed", proc_managed.stdout)

            # Unmanaged cert-manager: fails because cert-manager pod counts as tenant load (500m < 510m)
            body_unmanaged = f"""
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard TFVARS_ENABLE_GITHUB_MINTER=false \\
  check_existing_cluster_capacity_preflight "cluster" "region" "proj" "true" "file" "false" "" "" "false"
"""
            proc_unmanaged = self._run_cmd(body_unmanaged, bin_dir=str(bin_dir))
            self.assertEqual(proc_unmanaged.returncode, 1, f"Unmanaged cert-manager should fail: {proc_unmanaged.stdout}\n{proc_unmanaged.stderr}")
            self.assertIn("Cluster capacity preflight check failed", proc_unmanaged.stdout)
            self.assertIn("Insufficient schedulable CPU", proc_unmanaged.stdout)

    def test_diagnose_rollout_failure_skips_when_context_mismatches(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            kubectl = bin_dir / "kubectl"
            kubectl.write_text("""#!/usr/bin/env bash
case "$*" in
  *config*current-context*) echo "gke_foreign-proj_foreign-reg_foreign-cluster" ;;
  *get*pods*) echo "foreign-pod-should-not-be-printed" ;;
esac
exit 0
""")
            kubectl.chmod(kubectl.stat().st_mode | stat.S_IEXEC)
            log_file = pathlib.Path(tmp) / "prov.log"
            log_file.write_text(
                "Error: context deadline exceeded\n"
                "\n"
                "  with helm_release.kube_agents,\n"
                '  on main.tf line 497, in resource "helm_release" "kube_agents":\n'
            )
            body = f"""
PROJECT_ID="target-proj" REGION="us-central1" CLUSTER_NAME="target-cluster" diagnose_rollout_failure "{log_file}"
"""
            proc = self._run_cmd(body, bin_dir=str(bin_dir))
            self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
            self.assertIn("does not match target cluster", proc.stderr + proc.stdout)
            self.assertNotIn("foreign-pod-should-not-be-printed", proc.stdout)
            self.assertNotIn("Helm rollout timed out waiting for Kubernetes workloads", proc.stdout)

    def _menu_body(self):
        """run_menu_system() as bash parsed it."""
        proc = self._run_cmd("declare -f run_menu_system")
        self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
        return proc.stdout

    def _run_menu_option_6_with_preflight_rc(self, preflight_rc_expr):
        # The coordinates go in install.env, not in the script body. run_menu_system
        # sources the checkout's k8s-operator/scripts/vars.sh itself -- an untracked
        # file holding whatever install the developer last ran -- which lands on top
        # of anything the body assigned; it then re-applies install.env over that, by
        # design, so install.env is the one input that wins on both machines. Without
        # this the panel opens on the developer's live project and cluster.
        menu_env = self._tmp_path / "menu.install.env"
        menu_env.write_text(
            "PROJECT_ID=test-project\n"
            "CLUSTER_NAME=test-cluster\n"
            "REGION=us-central1\n"
            "GOOGLE_CHAT_ENABLED=false\n"
            "SLACK_ENABLED=false\n"
        )
        menu_env.chmod(0o600)
        # project_id falls back to `gcloud config get-value project` when PROJECT_ID
        # is empty, and the panel's state probes reach for kubectl. Both are stubbed
        # so a unit test cannot read -- or act on -- the developer's live GCP config.
        bin_dir = self._tmp_path / "menu-bin"
        bin_dir.mkdir(exist_ok=True)
        for tool in ("gcloud", "kubectl"):
            stub = bin_dir / tool
            stub.write_text("#!/usr/bin/env bash\nexit 0\n")
            stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

        body = f"""
has_controlling_tty() {{ return 0; }}
prompt_menu() {{
  local var="${{!#}}"
  printf -v "$var" "%s" "6"
}}
resolve_effective_image_tag() {{ return 0; }}
validate_immutable_ref() {{ return 0; }}
verify_local_source_ref() {{ return 0; }}
save_env_var() {{ return 0; }}
save_secret_env_var() {{ return 0; }}
write_tfvars_from_state() {{ return 0; }}
check_service_account_ownership() {{ return 0; }}
run_lifecycle_apply() {{ echo "APPLY_CALLED"; exit 0; }}

enforce_capacity_preflight() {{ return {preflight_rc_expr}; }}

run_menu_system
"""
        proc = self._run_cmd(
            body,
            env_overrides={"KUBE_AGENTS_INSTALL_ENV": str(menu_env)},
            bin_dir=str(bin_dir),
        )
        # The isolation is load-bearing for the three assertions below -- a panel
        # opened on someone's real install is not the scenario any of them describe
        # -- so it is checked rather than assumed.
        self.assertIn(
            "GKE Cluster: test-cluster (us-central1)",
            proc.stdout,
            "the control panel did not open on the test coordinates; ambient install "
            f"state reached the run. stdout: {proc.stdout}",
        )
        return proc

    def test_menu_system_option_6_preflight_refusal_exits_one_without_apply(self):
        # When capacity preflight refuses the apply (RC 1), run_menu_system option 6
        # must exit 1 immediately and never call run_lifecycle_apply.
        proc = self._run_menu_option_6_with_preflight_rc("1")
        self.assertEqual(proc.returncode, 1, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
        self.assertNotIn("APPLY_CALLED", proc.stdout)

    def test_menu_system_option_6_preflight_pause_exits_zero_without_apply(self):
        # When capacity preflight is paused by the operator (RC 2), run_menu_system option 6
        # must exit 0 and never call run_lifecycle_apply.
        proc = self._run_menu_option_6_with_preflight_rc('"$CAPACITY_PREFLIGHT_RC_PAUSED"')
        self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
        self.assertNotIn("APPLY_CALLED", proc.stdout)

    def test_menu_system_option_6_preflight_pass_invokes_apply(self):
        # When capacity preflight succeeds (RC 0), run_menu_system option 6
        # must invoke run_lifecycle_apply.
        proc = self._run_menu_option_6_with_preflight_rc("0")
        self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
        self.assertIn("APPLY_CALLED", proc.stdout)

    def _main_body(self):
        """main() as bash parsed it, not as the file spells it.

        `declare -f` prints the function from the shell's own parse tree, so
        comments are gone and indentation is bash's. A test that reads
        install.sh with open() instead is asserting the formatting of the file
        and goes red on a reflow that changed nothing.
        """
        proc = self._run_cmd("declare -f main")
        self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
        return proc.stdout

    def test_main_runs_the_capacity_preflight_before_it_applies(self):
        # Nothing else fails if the call is deleted: every other test in this
        # class drives enforce_capacity_preflight directly. Ordering is the
        # point of the check -- a preflight that runs after the apply has
        # started reports a deficit the install already committed to.
        body = self._main_body()
        self.assertIn(
            "enforce_capacity_preflight",
            body,
            "main() no longer runs the capacity preflight, so an adopted cluster "
            "without room for the workloads is only found at rollout",
        )
        self.assertLess(
            body.index("enforce_capacity_preflight"),
            body.index("run_lifecycle_apply"),
            "the capacity preflight has to run before the apply it is meant to "
            "stop, not after it",
        )

    def test_main_validates_and_exports_helm_timeout_and_skip_capacity_check(self):
        # main() must validate HELM_TIMEOUT, export HELM_TIMEOUT and export
        # SKIP_CAPACITY_CHECK before the preflight runs.
        body = self._main_body()
        self.assertIn("validate_helm_timeout", body)
        self.assertIn("export HELM_TIMEOUT=", body)
        self.assertIn("export SKIP_CAPACITY_CHECK=", body)
        self.assertLess(body.index("validate_helm_timeout"), body.index("enforce_capacity_preflight"))
        self.assertLess(body.index("export HELM_TIMEOUT="), body.index("enforce_capacity_preflight"))
        self.assertLess(body.index("export SKIP_CAPACITY_CHECK="), body.index("enforce_capacity_preflight"))

    def test_main_skip_capacity_check_flag_bypasses_preflight(self):
        # Passing --skip-capacity-check causes the menu or fresh-install path to export
        # SKIP_CAPACITY_CHECK=true, which check_existing_cluster_capacity_preflight honours.
        body = """
has_controlling_tty() { return 0; }
prompt_menu() { local v="${!#}"; printf -v "$v" "7"; }
main --menu --skip-capacity-check
check_existing_cluster_capacity_preflight "cluster" "region" "proj"
"""
        proc = self._run_cmd(body)
        self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
        self.assertIn("Skipping cluster capacity preflight check (SKIP_CAPACITY_CHECK=true)", proc.stdout)

    def test_main_does_not_expand_default_helm_timeout_before_helpers_sourced(self):
        # Under curl | bash without a checkout, DEFAULT_HELM_TIMEOUT is not defined
        # until step 2 sources the helpers. main() must not expand $DEFAULT_HELM_TIMEOUT
        # before acquire_source_repo / resolve_shared_defaults under set -u.
        body = """
set -u
unset DEFAULT_HELM_TIMEOUT
unset HELM_TIMEOUT
print_banner() { :; }
resolve_effective_image_tag() { echo "REACHED_PAST_BANNER"; exit 0; }
main --non-interactive
"""
        proc = self._run_cmd(body)
        self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
        self.assertIn("REACHED_PAST_BANNER", proc.stdout)

    def test_menu_system_validates_and_exports_helm_timeout_and_skip_capacity_check(self):
        # run_menu_system() must validate HELM_TIMEOUT, export HELM_TIMEOUT and export
        # SKIP_CAPACITY_CHECK before option 6's preflight runs.
        body = self._menu_body()
        self.assertIn("validate_helm_timeout", body)
        self.assertIn("export HELM_TIMEOUT=", body)
        self.assertIn("export SKIP_CAPACITY_CHECK=", body)
        self.assertLess(body.index("validate_helm_timeout"), body.index("enforce_capacity_preflight"))
        self.assertLess(body.index("export HELM_TIMEOUT="), body.index("enforce_capacity_preflight"))
        self.assertLess(body.index("export SKIP_CAPACITY_CHECK="), body.index("enforce_capacity_preflight"))

    def test_menu_system_rejects_out_of_range_helm_timeout_flag(self):
        body = """
has_controlling_tty() { return 0; }
main --menu --helm-timeout=100
"""
        proc = self._run_cmd(body)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--helm-timeout must be at least 540s", proc.stderr + proc.stdout)

    def test_menu_system_rejects_out_of_range_helm_timeout_in_install_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = pathlib.Path(tmp) / "install.env"
            env_file.write_text("PROJECT_ID=p\nHELM_TIMEOUT=9999\n")
            body = """
has_controlling_tty() { return 0; }
prompt_menu() { local v="${!#}"; printf -v "$v" "7"; }
run_menu_system
"""
            proc = self._run_cmd(body, env_overrides={"KUBE_AGENTS_INSTALL_ENV": str(env_file)})
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("--helm-timeout must be at most 899s", proc.stderr + proc.stdout)

    def test_menu_system_exports_flags_to_environment(self):
        body = """
has_controlling_tty() { return 0; }
prompt_menu() { local v="${!#}"; printf -v "$v" "7"; }
PARAM_SKIP_CAPACITY_CHECK="true"
CLI_HELM_TIMEOUT="750"
run_menu_system
echo "EXPORTED_SKIP=$SKIP_CAPACITY_CHECK"
echo "EXPORTED_TIMEOUT=$HELM_TIMEOUT"
"""
        proc = self._run_cmd(body)
        self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
        self.assertIn("EXPORTED_SKIP=true", proc.stdout)
        self.assertIn("EXPORTED_TIMEOUT=750", proc.stdout)

    def test_on_error_removes_preflight_tmp_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = pathlib.Path(tmp) / "scratch"
            scratch.mkdir()
            self.assertTrue(scratch.exists())
            body = f"""
PREFLIGHT_TMP_DIR="{scratch}"
( on_error 1 42 "false" ) || true
"""
            self._run_cmd(body)
            self.assertFalse(scratch.exists(), "on_error must remove PREFLIGHT_TMP_DIR if it exists")

    def test_main_passes_the_preflight_the_shape_it_checks(self):
        # The eight arguments decide what gets sized: gvisor moves the agent
        # pod off the untainted nodes, the memory mode adds hindsight, the
        # webui flag adds the dashboard sidecar, and the gitops pair adds the
        # minter. Swap two and the check runs against a cluster shape no
        # install will produce, passes, and the rollout is what finds out.
        self.assertRegex(
            self._main_body(),
            r'enforce_capacity_preflight\s+"\$cluster_name"\s+"\$region"\s+'
            r'"\$project_id"\s+"\$enable_gvisor"\s+"\$memory_mode"\s+'
            r'"\$PARAM_ENABLE_WEBUI"\s+"\$github_org"\s+"\$github_repo"',
        )

    def test_main_treats_a_declined_capacity_prompt_as_a_pause(self):
        # The operator said no. That is a choice, not a fault: exiting
        # non-zero would have the wrapper that ran install.sh report a failed
        # install and, in CI, fail the job.
        body = self._main_body()
        self.assertRegex(
            body,
            r'"\$CAPACITY_PREFLIGHT_RC_PAUSED"\)\s*\n?\s*exit 0',
            "main() must map the declined-prompt status to exit 0; "
            f"got: {body[body.find('enforce_capacity_preflight'):][:400]}",
        )

    def test_capacity_preflight_cleans_tmp_dir_on_sigint(self):
        # On INT or TERM, the preflight's signal handler must remove PREFLIGHT_TMP_DIR.
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            tmp_marker_file = pathlib.Path(tmp) / "tmp_dir.path"
            klog = pathlib.Path(tmp) / "kubectl.log"
            kubectl = bin_dir / "kubectl"
            kubectl.write_text(f"""#!/usr/bin/env bash
echo "$*" >> "{klog}"
case "$*" in
  *current-context*)
    echo "gke_proj_region_cluster"
    ;;
  *get*nodes*)
    echo '{{"items":[]}}'
    ;;
  *get*pods*)
    if [ -n "$PREFLIGHT_TMP_DIR" ]; then
      echo "$PREFLIGHT_TMP_DIR" > "{tmp_marker_file}"
    fi
    while true; do /bin/sleep 0.1; done
    ;;
  *)
    exit 0
    ;;
esac
""")
            kubectl.chmod(0o755)
            gcloud = bin_dir / "gcloud"
            gcloud.write_text("#!/usr/bin/env bash\nexit 0\n")
            gcloud.chmod(0o755)
            body = """
TFVARS_CREATE_CLUSTER=false TFVARS_CLUSTER_MODE=standard check_existing_cluster_capacity_preflight "cluster" "region" "proj" "false" "file" "false" "" ""
"""
            setup = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"
source "{_INSTALLER_COMMON}"
trap - ERR
{body}
"""
            full_env = get_isolated_test_env(
                overrides={"KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env), "KUBE_AGENTS_INSTALL_REPORT_FILE": str(self._test_report_file)},
                bin_dir=str(bin_dir),
            )
            proc = subprocess.Popen(
                ["bash", "-c", setup],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=full_env,
                cwd=str(_REPO_ROOT),
                start_new_session=True,
            )
            try:
                for _ in range(50):
                    if tmp_marker_file.exists():
                        break
                    time.sleep(0.1)
                self.assertTrue(tmp_marker_file.exists(), "kubectl was not invoked or did not record tmp dir")
                tmp_dir = pathlib.Path(tmp_marker_file.read_text().strip())
                self.assertTrue(tmp_dir.exists(), f"tmp_dir {tmp_dir} should exist while running")
                os.killpg(proc.pid, signal.SIGINT)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
                time.sleep(0.2)
                self.assertFalse(tmp_dir.exists(), f"tmp_dir {tmp_dir} was not cleaned up on SIGINT")
            finally:
                if proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait(timeout=2)
                    except Exception:
                        pass


if __name__ == "__main__":
    unittest.main()
