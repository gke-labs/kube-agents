"""Unit tests for install.sh validation and execution routines.

Tests pure numeric SemVer (X.Y.Z) references, 40-character commit SHAs,
piped stdin (curl | bash) execution, local script path resolution, the
NetworkPolicy enablement sequence install.sh runs against adopted clusters,
and that the helpers driving install.sh here detach from the controlling
terminal, so a prompt auto-selects its default instead of blocking the suite.
"""

import json
import os
import pathlib
import pty
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

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

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INSTALL_SH = _REPO_ROOT / "install.sh"
_PRINT_NO_CHAT_SH = _REPO_ROOT / "scripts" / "installer" / "print_instructions_no_chat.sh"
_INSTALLER_COMMON = _REPO_ROOT / "scripts" / "installer" / "installer_common.sh"

# install.sh sources the shared helpers from the acquired workspace partway
# through main(), so a validator that leans on one is unreachable from a bare
# KUBE_AGENTS_SOURCE_ONLY source. Prepend this to reach it.
_SOURCE_INSTALLER_COMMON = f'source "{_INSTALLER_COMMON}"; '

# Bound on a sourced install.sh function run through _run_installer_bash.
# Each is a stub-backed call that takes a few seconds at most (the slowest
# measured, a run_menu_system save, takes about two), so a run that reaches
# this is stuck -- on a prompt, if the terminal detach ever slips -- and the
# bound turns that into a failure with output, not a hang.
_INSTALLER_RUN_TIMEOUT_SECONDS = 60


def _run_installer_bash(body, env, cwd=_REPO_ROOT):
    """Run a bash body that sources install.sh, detached from any terminal.

    install.sh's prompts read /dev/tty whenever has_controlling_tty finds one,
    and /dev/tty resolves through the controlling terminal rather than stdin,
    so a child that only had stdin redirected still blocks a developer's run
    on the first prompt it reaches, forever, with the prompt captured and
    nothing on screen. start_new_session puts the child in a session with no
    controlling terminal, the branch CI's terminal-less runners already take,
    so every prompt auto-selects its default; stdin is closed for the same
    reason, and the timeout bounds whatever is left. The helpers whose runs
    the hang was traced to go through here, and
    InstallerHelpersDetachFromTheTerminalTest pins both that set and the
    detach itself; a new helper for a function that prompts belongs here too.
    """
    return subprocess.run(
        ["bash", "-c", body],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        timeout=_INSTALLER_RUN_TIMEOUT_SECONDS,
        env=env,
        cwd=str(cwd),
    )


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
        return _run_installer_bash(setup, full_env, cwd=cwd or _REPO_ROOT)

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
        """Verifies parse_args captures --gke-cluster-mode."""
        cmd = 'parse_args --gke-cluster-mode=autopilot; echo "MODE=$PARAM_CLUSTER_MODE"'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("MODE=autopilot", proc.stdout)

    def test_cluster_mode_defaults_to_unset(self):
        """An unpassed --gke-cluster-mode leaves the interview free to ask."""
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
        rejects. Reachable non-interactively via --gke-cluster-name, where nothing
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

    def test_parse_args_model_max_tokens_is_read(self):
        cmd = 'parse_args --model-max-tokens=4096; echo "MAX=$PARAM_MODEL_MAX_TOKENS"'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("MAX=4096", proc.stdout)

    def test_model_max_tokens_defaults_to_unset(self):
        cmd = 'echo "MAX=[$PARAM_MODEL_MAX_TOKENS]"'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("MAX=[]", proc.stdout)

    def test_validate_model_max_tokens_accepts_whole_numbers_and_empty(self):
        for value in ("4096", "0", ""):
            with self.subTest(value=value):
                proc = self._run_install_func(
                    _SOURCE_INSTALLER_COMMON
                    + f"parse_args --model-max-tokens={value}; validate_model_max_tokens"
                )
                self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

    def test_validate_model_max_tokens_rejects_anything_else(self):
        # The same helper the tfvars generator applies, so the interview and
        # upgrade.sh cannot disagree on what a valid value is.
        for value in ("4k", "-1", "4096.5", "+1"):
            with self.subTest(value=value):
                proc = self._run_install_func(
                    _SOURCE_INSTALLER_COMMON
                    + f"parse_args --model-max-tokens={value}; validate_model_max_tokens"
                )
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(
                    "--model-max-tokens must be a whole number of tokens",
                    proc.stderr + proc.stdout,
                )
        # install.env's recorded value is checked too, not only the flag.
        proc = self._run_install_func(
            _SOURCE_INSTALLER_COMMON + 'MODEL_MAX_TOKENS="4k"; validate_model_max_tokens'
        )
        self.assertNotEqual(proc.returncode, 0)

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
        """`--enable-gvisor=` must reach main's validator rather than read as a default.

        main uses ${PARAM_ENABLE_GVISOR-true} for exactly this: parse_args
        leaves the empty string in place, the `:-` form would silently
        substitute it back to the default, and the validator rejects it.

        flag_bool_value has to preserve this too: the bare `--enable-gvisor`
        form yields true, and only the `=` form with nothing after it is empty.
        """
        cmd = 'parse_args --enable-gvisor=; echo "GVISOR=[$PARAM_ENABLE_GVISOR]"'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("GVISOR=[]", proc.stdout)

    def test_prompt_menu_defaults_to_the_first_option(self):
        """The premise the gVisor prompt's ordering rests on.

        main lists the incoming value as option 1 and treats option 2 as "the
        other one", so that answering the prompt with nothing confirms what
        `--enable-gvisor` asked for and the `(Default)` label matches what that
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

    def test_wait_for_deployment_object_passes_context(self):
        """Context argument must be forwarded as --context to kubectl."""
        stub = 'case "$*" in *"--context my-ctx"*) exit 0 ;; *) exit 1 ;; esac'
        proc = self._run_with_kubectl_stub(
            'rc=0; wait_for_deployment_object dep ns 0 my-ctx || rc=$?; echo "RC=$rc"',
            stub,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("RC=0", proc.stdout)

    def test_wait_for_rollout_passes_context(self):
        """Context argument must be forwarded as --context to kubectl rollout."""
        stub = 'case "$*" in *"--context my-ctx"*) exit 0 ;; *) exit 1 ;; esac'
        proc = self._run_with_kubectl_stub(
            'rc=0; wait_for_rollout dep ns 0 my-ctx || rc=$?; echo "RC=$rc"',
            stub,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("RC=0", proc.stdout)

    # main() is not callable from here -- reaching step 13 means provisioning a
    # cluster -- so its context gate is lifted out of the source and run in a
    # function of its own. The anchors are checked in _context_gate_body, so a
    # rewrite of the step fails loudly rather than leaving the two tests below
    # asserting against an empty string.
    _CONTEXT_GATE_START = '  local expected_ctx\n  expected_ctx="$(gke_context_name)"'
    _CONTEXT_GATE_END = "    exit 1\n  fi\n"

    def _context_gate_body(self):
        source = _INSTALL_SH.read_text()
        start = source.index(self._CONTEXT_GATE_START)
        end = source.index(self._CONTEXT_GATE_END, start) + len(self._CONTEXT_GATE_END)
        gate = source[start:end]
        self.assertIn("kubectl config current-context", gate)
        return (
            _SOURCE_INSTALLER_COMMON
            + '\nPROJECT_ID="a-project"; REGION="a-region"; CLUSTER_NAME="a-cluster"\n'
            + "context_gate() {\n"
            + gate
            + "}\n"
        )

    def test_the_health_check_refuses_a_kubectl_pointed_elsewhere(self):
        """Step 13 reads whatever context is current, so a get-credentials that
        did not take must end the run rather than grade another cluster."""
        proc = self._run_with_kubectl_stub(
            self._context_gate_body() + 'context_gate; echo "REACHED_HEALTH_CHECKS"',
            'echo "gke_another-project_another-region_another-cluster"',
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertNotIn("REACHED_HEALTH_CHECKS", proc.stdout)
        combined = proc.stdout + proc.stderr
        self.assertIn("gke_another-project_another-region_another-cluster", combined)
        self.assertIn("gke_a-project_a-region_a-cluster", combined)

    def test_the_health_check_continues_on_the_expected_context(self):
        """The other half: the gate has to let the matching context through."""
        proc = self._run_with_kubectl_stub(
            self._context_gate_body() + 'context_gate; echo "REACHED_HEALTH_CHECKS"',
            'echo "gke_a-project_a-region_a-cluster"',
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("REACHED_HEALTH_CHECKS", proc.stdout)

    def test_step_13_namespace_check_passes_context(self):
        """Step 13's namespace check must forward --context to avoid querying ambient context."""
        source = _INSTALL_SH.read_text()
        start = source.index(self._CONTEXT_GATE_START)
        end = source.index("wait_for_deployment_object", start)
        body = source[start:end]
        self.assertIn('kubectl get ns "$namespace" --context "$expected_ctx"', body)

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
            'parse_args --gcp-project-id=from-the-flag; echo "P=$PARAM_PROJECT_ID"',
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
            'parse_args --google-chat-allowed-users=from-the-flag@example.com; '
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
            proc = _run_installer_bash(
                body,
                get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": str(empty_env)}, bin_dir=str(bin_dir)),
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
            proc = _run_installer_bash(body, get_isolated_test_env(bin_dir=str(bin_dir)))
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
        """KMS locations are regional. The caller passes install.sh's --gcp-region,
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
    """NAMESPACE is a name kubectl tooling exports; only install.env or flags may set it."""

    def _namespace_after_load(self, contents):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = pathlib.Path(tmp) / "install.env"
            env_file.write_text(contents)
            env_file.chmod(0o600)
            proc = subprocess.run(
                ["bash", "-c",
                 f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                 'apply_agent_namespace_override\n'
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

    def _bootstrap(self, env):
        with tempfile.TemporaryDirectory() as tmp:
            dest = pathlib.Path(tmp) / "new.install.env"
            # An existing, empty input: install.sh refuses a KUBE_AGENTS_INSTALL_ENV
            # that names a missing file, and the point here is the file it
            # CREATES, not the one it loads.
            loaded = pathlib.Path(tmp) / "loaded.install.env"
            loaded.write_text("")
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


class ModelMaxTokensPersistsThroughInstallEnvTest(unittest.TestCase):
    """The flag on the first run, the file on every later one.

    main() exports MODEL_MAX_TOKENS from the flag (or the loaded file),
    bootstrap_install_env_file records the export in a fresh install.env, and
    the next run seeds PARAM_MODEL_MAX_TOKENS from the file at source time.
    Dropping the write_env_var line, the ${MODEL_MAX_TOKENS:-} seed or the
    export leaves the flag and validator tests green while --model-max-tokens
    renders model_max_tokens = 0 on the next upgrade, so this walks the file
    the first run writes into a second run.
    """

    _RECORDED_VALUE = "4096"

    def _bootstrap_with_export(self, tmp, export_line):
        dest = pathlib.Path(tmp) / "new.install.env"
        loaded = pathlib.Path(tmp) / "loaded.install.env"
        loaded.write_text("")
        loaded.chmod(0o600)
        proc = subprocess.run(
            ["bash", "-c",
             f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
             'source scripts/installer/installer_common.sh\n'
             'resolve_shared_defaults\n'
             'PARAM_DRY_RUN=false; PARAM_MEMORY=file\n'
             f'{export_line}\n'
             f'bootstrap_install_env_file "{dest}" some-tag >/dev/null'],
            capture_output=True, text=True,
            env=self._env({
                "KUBE_AGENTS_INSTALL_ENV": str(loaded),
                "PROJECT_ID": "p", "CLUSTER_NAME": "c", "REGION": "us-central1",
            }),
            cwd=str(_REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertTrue(dest.exists(), proc.stderr + proc.stdout)
        return dest

    def _read_back(self, env_file, body):
        proc = subprocess.run(
            ["bash", "-c", f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n{body}\n'],
            capture_output=True, text=True,
            env=self._env({"KUBE_AGENTS_INSTALL_ENV": str(env_file)}),
            cwd=str(_REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        return proc.stdout

    @staticmethod
    def _env(overrides):
        # A developer's own exported value must not stand in for the file.
        env = get_isolated_test_env(overrides=overrides)
        env.pop("MODEL_MAX_TOKENS", None)
        env.update(overrides)
        return env

    def test_the_first_run_records_the_export_and_the_next_run_reads_it_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = self._bootstrap_with_export(
                tmp, f'export MODEL_MAX_TOKENS="{self._RECORDED_VALUE}"'
            )
            self.assertIn(f"MODEL_MAX_TOKENS={self._RECORDED_VALUE}\n", dest.read_text())
            # The parameter block, and the exported environment the tfvars
            # generator reads (set -a around the load is what carries it).
            out = self._read_back(
                dest,
                'echo "PARAM=[$PARAM_MODEL_MAX_TOKENS]"; '
                "bash -c 'echo \"EXPORTED=[$MODEL_MAX_TOKENS]\"'",
            )
            self.assertIn(f"PARAM=[{self._RECORDED_VALUE}]", out)
            self.assertIn(f"EXPORTED=[{self._RECORDED_VALUE}]", out)

    def test_an_unset_value_is_recorded_empty_and_reads_back_empty(self):
        """Empty is what the generator turns into DEFAULT_MODEL_MAX_TOKENS, and
        the quoted empty string is the spelling the drift check knows."""
        with tempfile.TemporaryDirectory() as tmp:
            dest = self._bootstrap_with_export(tmp, 'export MODEL_MAX_TOKENS=""')
            self.assertIn("MODEL_MAX_TOKENS=''\n", dest.read_text())
            out = self._read_back(dest, 'echo "PARAM=[$PARAM_MODEL_MAX_TOKENS]"')
            self.assertIn("PARAM=[]", out)

    def test_a_flag_on_a_later_run_beats_the_recorded_value(self):
        """Order of authority: flag, then file. 0 is non-empty to ${:-}, so a
        flag of 0 switches the budget off against a file that records one."""
        with tempfile.TemporaryDirectory() as tmp:
            dest = self._bootstrap_with_export(
                tmp, f'export MODEL_MAX_TOKENS="{self._RECORDED_VALUE}"'
            )
            out = self._read_back(
                dest, 'parse_args --model-max-tokens=0; echo "PARAM=[$PARAM_MODEL_MAX_TOKENS]"'
            )
            self.assertIn("PARAM=[0]", out)

    def test_main_validates_then_exports_before_the_generator_and_the_bootstrap(self):
        """The KUBE_AGENTS_SOURCE_ONLY harness cannot drive main(), so the wiring
        between parse_args and the export block is pinned by text: the value
        main() exports is the flag-or-file one the validator checked, and the
        export comes before the two readers, write_tfvars_from_state and
        bootstrap_install_env_file."""
        text = _INSTALL_SH.read_text()
        main_start = text.index("\nmain() {")
        seed_line = 'local model_max_tokens="${PARAM_MODEL_MAX_TOKENS:-${MODEL_MAX_TOKENS:-}}"'
        validate_line = "validate_model_max_tokens || exit 1"
        export_line = 'export MODEL_MAX_TOKENS="$model_max_tokens"'
        for line in (seed_line, validate_line, export_line):
            self.assertIn(line, text[main_start:], f"main() no longer carries: {line}")
        seed = text.index(seed_line, main_start)
        validated = text.index(validate_line, main_start)
        exported = text.index(export_line, main_start)
        generator = text.index('write_tfvars_from_state "$tfvars_file" "$image_tag"', exported)
        bootstrap = text.index('bootstrap_install_env_file "$INSTALL_ENV_FILE" "$image_tag"', exported)
        self.assertLess(seed, validated)
        self.assertLess(validated, exported)
        self.assertLess(exported, generator)
        self.assertLess(exported, bootstrap)


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
    `^(true|false)$` validators run in `parse_args`, on what a caller typed on
    the command line, so a spelling seeded from `install.env` never reaches one.
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
        self.assertNotIn('"${PARAM_ENABLE_SLACK:-$DEFAULT_SLACK_ENABLED}" = "true"', block)
        self.assertIn('is_truthy "$PARAM_ENABLE_GOOGLE_CHAT"', block)
        self.assertIn('is_truthy "${PARAM_ENABLE_SLACK:-$DEFAULT_SLACK_ENABLED}"', block)

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

    def test_a_changed_max_tokens_answer_is_named(self):
        """A --model-max-tokens flag on a run against a file that records a
        different value: the file still says the old budget, and the next
        upgrade.sh would render it."""
        proc = self._warn("MODEL_MAX_TOKENS=4096\n", {"MODEL_MAX_TOKENS": "8192"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertIn("does not record", combined)
        self.assertIn("MODEL_MAX_TOKENS=8192", combined)

    def test_an_unchanged_max_tokens_answer_says_nothing(self):
        proc = self._warn("MODEL_MAX_TOKENS=4096\n", {"MODEL_MAX_TOKENS": "4096"})
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

    def _run_func(self, func_call, cwd=None):
        setup = f"""
source "{_INSTALLER_COMMON}"
KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"
{func_call}
"""
        overrides = {
            "KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env),
            "KUBE_AGENTS_INSTALL_REPORT_FILE": str(self._tmp_path / "report.json"),
        }
        full_env = get_isolated_test_env(overrides=overrides)
        return subprocess.run(
            ["bash", "-c", setup],
            capture_output=True,
            text=True,
            env=full_env,
            cwd=str(cwd or _REPO_ROOT),
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


class PtyChildTestMixin:
    """Shared plumbing for a test that forks a child under a controlling terminal.

    The parent drains the pty for the child's life (a child that writes more
    than the pty buffers blocks otherwise), kills the child's whole group and
    reaps it on cleanup, and reads the child's outcome from a file it writes,
    since the pty carries the child's screen, not its result.
    """

    _READY_TIMEOUT_SECS = 30
    _POLL_INTERVAL_SECS = 0.1

    def _watch_pty_child(self, pid, fd):
        drain = threading.Thread(target=self._drain, args=(fd,), daemon=True)
        drain.start()
        self.addCleanup(self._cleanup_pty, pid, fd)

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


@unittest.skipUnless(hasattr(pty, "fork"), "run_with_spinner's terminal branch needs a pty")
class SpinnerTerminalBranchTest(PtyChildTestMixin, unittest.TestCase):
    """run_with_spinner on a real terminal, the branch no piped test reaches.

    Every other test in this file runs under a subprocess pipe, so `[ ! -t 1 ]`
    diverts it to the fallback and the spinner loop, the cursor calls, the
    background job and the interrupt traps never execute at all. On a terminal
    -- where an operator actually meets them -- they all do, so these drive one.
    """

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
        # blocks on write unless someone is reading.
        self._watch_pty_child(pid, fd)
        return pid

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


class DomainScopedFlagsTest(unittest.TestCase):
    """The CLI surface issue #1540 defines.

    Three things are asserted here and nowhere else: that the legacy spellings
    are GONE rather than quietly still accepted, that every `--enable-*` toggle
    means the same thing bare as it does with `=true`, and that the flags which
    had no CLI at all before this reach the variables the install actually
    reads.
    """

    def _parse(self, args, body, contents="", env=None):
        """parse_args the given args, then run `body`, with an install.env.

        installer_common.sh is sourced too, in the order main() does it:
        source_provisioning_helpers runs at step 2, so every function reached
        below has is_truthy and the DEFAULT_* set by the time it runs.
        """
        with tempfile.TemporaryDirectory() as tmp:
            env_file = pathlib.Path(tmp) / "install.env"
            env_file.write_text(contents)
            script = (
                f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                f'source "{_REPO_ROOT}/scripts/installer/installer_common.sh"\n'
                f"parse_args {args}\n{body}\n"
            )
            overrides = {"KUBE_AGENTS_INSTALL_ENV": str(env_file)}
            overrides.update(env or {})
            return subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(overrides=overrides),
                cwd=str(_REPO_ROOT),
            )

    # Every flag the issue removes, with no alias fallback. A rename that left
    # the old spelling working would pass every other test in this file.
    LEGACY_FLAGS = [
        "--project-id=p",
        "--region=r",
        "--cluster-name=c",
        "--cluster-mode=autopilot",
        "--gvisor=false",
        "--allowed-users=a@example.com",
        "--enable-web-ui",
        "--enable-web-ui=true",
        "--enable-webui",
        "--enable-webui=true",
        "--webui",
        "--webui=true",
    ]

    def test_every_legacy_flag_is_rejected(self):
        for flag in self.LEGACY_FLAGS:
            with self.subTest(flag=flag):
                proc = self._parse(flag, 'echo "REACHED"')
                self.assertNotIn("REACHED", proc.stdout)
                self.assertIn("Unknown parameter", proc.stdout + proc.stderr)

    # flag -> (PARAM variable, value to pass)
    RENAMED_FLAGS = {
        "--gcp-project-id": ("PARAM_PROJECT_ID", "my-project"),
        "--gcp-region": ("PARAM_REGION", "europe-west1"),
        "--gke-cluster-name": ("PARAM_CLUSTER_NAME", "my-cluster"),
        "--gke-cluster-mode": ("PARAM_CLUSTER_MODE", "standard"),
        "--agent-namespace": ("PARAM_AGENT_NAMESPACE", "my-namespace"),
        "--google-chat-allowed-users": ("PARAM_ALLOWED_USERS", "a@example.com"),
        "--slack-bot-token": ("PARAM_SLACK_BOT_TOKEN", "xoxb-1"),
        "--slack-app-token": ("PARAM_SLACK_APP_TOKEN", "xapp-1"),
        "--slack-allowed-users": ("PARAM_SLACK_ALLOWED_USERS", "U123,U456"),
        "--slack-home-channel": ("PARAM_SLACK_HOME_CHANNEL", "C01234567"),
        "--slack-home-channel-name": ("PARAM_SLACK_HOME_CHANNEL_NAME", "#gke-alerts"),
    }

    def test_each_value_flag_reaches_its_variable(self):
        for flag, (var, value) in self.RENAMED_FLAGS.items():
            with self.subTest(flag=flag):
                proc = self._parse(f"{flag}={shlex.quote(value)}", f'echo "V=${var}"')
                self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                self.assertIn(f"V={value}", proc.stdout)

    TOGGLES = {
        "--enable-gvisor": "PARAM_ENABLE_GVISOR",
        "--enable-hermes-dashboard": "PARAM_ENABLE_WEBUI",
        "--enable-gke-backup-plan": "PARAM_ENABLE_GKE_BACKUP_PLAN",
        "--enable-pubsub-platform": "PARAM_ENABLE_PUBSUB_PLATFORM",
        "--enable-stockout-investigator": "PARAM_ENABLE_STOCKOUT_INVESTIGATOR",
        "--enable-google-chat": "PARAM_ENABLE_GOOGLE_CHAT",
        "--enable-slack": "PARAM_ENABLE_SLACK",
    }

    def test_bare_and_explicit_boolean_forms_agree(self):
        """`--flag` and `--flag=true` are the same answer, for every toggle.

        The old parser was inconsistent about this -- `--enable-gvisor` took only the
        `=` form, `--enable-google-chat` only the bare one -- so an automated
        caller had to know which was which per flag.
        """
        for flag, var in self.TOGGLES.items():
            with self.subTest(flag=flag):
                bare = self._parse(flag, f'echo "V=${var}"')
                self.assertEqual(bare.returncode, 0, bare.stderr + bare.stdout)
                self.assertIn("V=true", bare.stdout)

                explicit = self._parse(f"{flag}=true", f'echo "V=${var}"')
                self.assertEqual(explicit.returncode, 0, explicit.stderr + explicit.stdout)
                self.assertIn("V=true", explicit.stdout)

                off = self._parse(f"{flag}=false", f'echo "V=${var}"')
                self.assertEqual(off.returncode, 0, off.stderr + off.stdout)
                self.assertIn("V=false", off.stdout)

    def test_a_flag_still_beats_install_env_for_the_new_names(self):
        """The precedence install.defaults.env documents, on the renamed flags.

        parse_args runs after install.env is sourced, which is what makes this
        true; a seed added in the wrong place would silently invert it.
        """
        proc = self._parse(
            "--gcp-project-id=from-the-flag",
            'echo "P=$PARAM_PROJECT_ID"',
            contents="PROJECT_ID=from-the-file\n",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("P=from-the-flag", proc.stdout)

    def test_slack_settings_inherit_from_install_env(self):
        """A re-run that repeats no --slack-* flag keeps the recorded settings.

        Same contract the Google Chat seeds have: losing SLACK_ALLOWED_USERS on
        a silent re-run would widen the allowlist to the whole workspace rather
        than merely dropping a setting.
        """
        proc = self._parse(
            "--enable-slack",
            'echo "T=$PARAM_SLACK_BOT_TOKEN U=$PARAM_SLACK_ALLOWED_USERS"',
            contents="SLACK_BOT_TOKEN=xoxb-from-the-file\nSLACK_ALLOWED_USERS=U999\n",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("T=xoxb-from-the-file U=U999", proc.stdout)

    def test_agent_namespace_survives_the_unset_namespace_guard(self):
        """--agent-namespace must reach NAMESPACE despite bootstrap_install_env.

        That function unsets NAMESPACE before reading install.env, so an
        inherited kubectl variable cannot redirect an install -- which is what
        the seeded NAMESPACE below stands in for. A flag is a deliberate act and
        has to get through anyway.

        Calls install.sh's own apply_agent_namespace_override rather than a copy
        of its body: a test that re-implements the export passes just as well
        once the export is deleted from the script, while --agent-namespace
        silently stops reaching write_tfvars_from_state.
        """
        proc = self._parse(
            "--agent-namespace=chosen-ns",
            'apply_agent_namespace_override; echo "N=$NAMESPACE"',
            env={"NAMESPACE": "inherited-from-kubectl"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("N=chosen-ns", proc.stdout)

    def test_the_ambient_namespace_alone_still_does_not_get_through(self):
        """The other half of the same guard: no flag, no override.

        A function that exported $NAMESPACE rather than PARAM_AGENT_NAMESPACE
        would pass the test above and undo the guard entirely.
        """
        proc = self._parse(
            "--gcp-project-id=p",
            'apply_agent_namespace_override; echo "N=[${NAMESPACE:-}]"',
            env={"NAMESPACE": "inherited-from-kubectl"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("N=[]", proc.stdout)

    def test_agent_namespace_seeds_from_install_env(self):
        """install.env key NAMESPACE seeds PARAM_AGENT_NAMESPACE and reaches NAMESPACE."""
        proc = self._parse(
            "--gcp-project-id=p",
            'apply_agent_namespace_override; echo "N=[${NAMESPACE:-}]"',
            contents="NAMESPACE=recorded-ns\n",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("N=[recorded-ns]", proc.stdout)


    def test_both_entry_paths_apply_the_namespace_override(self):
        """main() and the control panel each have to apply it after their unset.

        The behavioural test above can only prove the function does what it
        says; it cannot notice the call going missing. main() applies it once
        parse_args has run, and run_menu_system applies it again because
        load_install_env unsets NAMESPACE a second time on its way in -- a menu
        that lost the flag would open on the default namespace and write tfvars
        for that one.
        """
        text = _INSTALL_SH.read_text()
        main_at = text.index("\nmain() {")
        self.assertIn(
            'parse_args "$@"\n  apply_agent_namespace_override',
            text[main_at:],
            "main() must apply the override immediately after parse_args",
        )
        menu = text[text.index("run_menu_system() {") : main_at]
        load_at = menu.find('load_install_env "$INSTALL_ENV_FILE"')
        reapply_at = menu.find("apply_agent_namespace_override")
        self.assertNotEqual(load_at, -1, "the menu's install.env reload is missing")
        self.assertNotEqual(
            reapply_at, -1, "run_menu_system never re-applies --agent-namespace"
        )
        self.assertLess(load_at, reapply_at)

    def test_a_namespace_the_flag_chose_is_recorded_for_the_next_run(self):
        """Otherwise the flag applies to exactly one run and says nothing.

        install.env is the only thing a later install.sh, upgrade.sh or --menu
        run reads. Without the key they resolve ${NAMESPACE:-$DEFAULT_NAMESPACE}
        back to kubeagents-system, render tfvars for it, look for the recovered
        Secret there, and are refused by lifecycle.sh's guard_release_namespace.

        $NAMESPACE on its own must still not be recorded: it is a variable
        kubectl tooling exports, and freezing a stray value would move the
        release on the next apply.
        """
        for args, expected in (("--agent-namespace=chosen-ns", True), ("", False)):
            with self.subTest(args=args):
                with tempfile.TemporaryDirectory() as tmp:
                    destination = pathlib.Path(tmp) / "written.env"
                    proc = self._parse(
                        args,
                        f'bootstrap_install_env_file "{destination}" v1.2.3',
                        env={"NAMESPACE": "inherited-from-kubectl"},
                    )
                    self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                    written = destination.read_text()
                    if expected:
                        self.assertIn("NAMESPACE=chosen-ns", written)
                    else:
                        self.assertNotIn("\nNAMESPACE=", written)
                        self.assertNotIn("inherited-from-kubectl", written)

    def test_the_flag_says_so_when_it_cannot_be_recorded(self):
        """An install.env that already exists is never rewritten.

        That is the file's contract, so the second install passing the flag by
        hand is the one case nothing can record for the operator. Silence there
        is the trap the test above describes, arriving one run later.
        """
        with tempfile.TemporaryDirectory() as tmp:
            destination = pathlib.Path(tmp) / "existing.env"
            destination.write_text("PROJECT_ID=p\n")
            proc = self._parse(
                "--agent-namespace=chosen-ns",
                f'bootstrap_install_env_file "{destination}" v1.2.3',
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            combined = proc.stdout + proc.stderr
            self.assertIn("applies to this run only", combined)
            self.assertIn("chosen-ns", combined)
            self.assertIn(
                "repeat --agent-namespace on every later install.sh, upgrade.sh and --menu run",
                combined,
            )
            self.assertEqual(
                destination.read_text(),
                "PROJECT_ID=p\n",
                "an install.env that already exists must be left alone",
            )

    def test_the_backup_plan_flag_says_so_when_it_beats_a_recorded_value(self):
        """Reversing this one destroys a BackupPlan rather than moving a release.

        The recorded value is what a later run re-reads, so a re-run that omits
        the flag plans the BackupPlan's destruction — and once a backup has been
        taken the API refuses that destroy, leaving a half-applied install.
        Nothing else says so: bootstrap_install_env_file will not rewrite an
        existing file, the menu has no entry for the key, and
        warn_unrecorded_interview_answers deliberately excludes it because it
        has no interview question.

        The recorded value is named, not merely contradicted: "applies to this
        run only" against a file the operator believes says true is a warning
        they will read as spurious.
        """
        with tempfile.TemporaryDirectory() as tmp:
            destination = pathlib.Path(tmp) / "existing.env"
            destination.write_text("ENABLE_GKE_BACKUP_PLAN=false\n")
            proc = self._parse(
                "--enable-gke-backup-plan",
                f'bootstrap_install_env_file "{destination}" v1.2.3',
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            combined = proc.stdout + proc.stderr
            self.assertIn("applies to this run only", combined)
            self.assertIn("ENABLE_GKE_BACKUP_PLAN=false", combined)
            self.assertIn("BackupPlan", combined)
            self.assertIn(
                "repeat --enable-gke-backup-plan on every later install.sh run",
                combined,
            )
            self.assertNotIn("upgrade.sh", combined)
            self.assertNotIn("--menu", combined)

    def test_the_backup_plan_warning_fires_non_interactively(self):
        """-y is the route these flags were added for, not an exemption.

        warn_unrecorded_interview_answers returns early on -y, because its
        answers came from flags and the file and there is no third source to
        surprise anyone. Here the flag IS the surprise, so a copy of that early
        return would silence the warning on every run that can hit the trap.
        """
        with tempfile.TemporaryDirectory() as tmp:
            destination = pathlib.Path(tmp) / "existing.env"
            destination.write_text("PROJECT_ID=p\n")
            proc = self._parse(
                "-y --enable-gke-backup-plan",
                f'bootstrap_install_env_file "{destination}" v1.2.3',
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            combined = proc.stdout + proc.stderr
            self.assertIn("applies to this run only", combined)
            self.assertIn("records no ENABLE_GKE_BACKUP_PLAN", combined)

    def test_a_flag_that_agrees_with_the_recorded_value_is_not_warned_about(self):
        """Nothing is overridden, so there is nothing to lose by omitting it.

        A warning here would fire on every re-run of a correctly recorded
        install, which is the way a real warning gets ignored.

        The non-canonical spellings are the case that matters. A flag's value is
        always `true` or `false`; install.env is hand-written, and CI's
        render_install_env.sh copies the GitHub variable in verbatim while
        provision_environment.sh canonicalises the same variable onto the flag.
        So an environment typed `True` disagrees on spelling and agrees on
        meaning, and comparing the two as strings warned about a destroyed
        BackupPlan on every rebuild.
        """
        for key, flag in (
            ("ENABLE_GKE_BACKUP_PLAN=true", "--enable-gke-backup-plan=true"),
            ("ENABLE_GKE_BACKUP_PLAN=True", "--enable-gke-backup-plan=true"),
            ("ENABLE_GKE_BACKUP_PLAN=yes", "--enable-gke-backup-plan"),
            ("ENABLE_GKE_BACKUP_PLAN=1", "--enable-gke-backup-plan=true"),
            ("ENABLE_GKE_BACKUP_PLAN=on", "--enable-gke-backup-plan=true"),
            ("ENABLE_GKE_BACKUP_PLAN=off", "--enable-gke-backup-plan=false"),
            ("ENABLE_GKE_BACKUP_PLAN=no", "--enable-gke-backup-plan=false"),
            ("NAMESPACE=chosen-ns", "--agent-namespace=chosen-ns"),
        ):
            with self.subTest(key=key, flag=flag):
                with tempfile.TemporaryDirectory() as tmp:
                    destination = pathlib.Path(tmp) / "existing.env"
                    destination.write_text(f"{key}\n")
                    proc = self._parse(
                        flag,
                        f'bootstrap_install_env_file "{destination}" v1.2.3',
                    )
                    self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                    self.assertNotIn(
                        "applies to this run only", proc.stdout + proc.stderr
                    )

    def test_a_non_canonical_recorded_value_still_warns_when_it_disagrees(self):
        """Comparing by meaning must not turn the warning off altogether.

        Both directions, because they are different branches of the comparison
        and each is a reversal: a recorded `no` against the bare flag, and a
        recorded `True` against `=false`, which is the run that turns a backup
        plan off for itself and leaves the file saying otherwise.
        """
        for recorded, flag in (
            ("no", "--enable-gke-backup-plan"),
            ("True", "--enable-gke-backup-plan=false"),
        ):
            with self.subTest(recorded=recorded, flag=flag):
                with tempfile.TemporaryDirectory() as tmp:
                    destination = pathlib.Path(tmp) / "existing.env"
                    destination.write_text(f"ENABLE_GKE_BACKUP_PLAN={recorded}\n")
                    proc = self._parse(
                        flag,
                        f'bootstrap_install_env_file "{destination}" v1.2.3',
                    )
                    self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                    combined = proc.stdout + proc.stderr
                    self.assertIn("applies to this run only", combined)
                    # The operator's own spelling, not a canonicalised one: they
                    # have to find the line in the file.
                    self.assertIn(f"ENABLE_GKE_BACKUP_PLAN={recorded}", combined)

    def test_a_recorded_spelling_warns_about_nothing_when_no_flag_was_passed(self):
        """The case that reaches this function without anyone typing a flag.

        `PARAM_ENABLE_GKE_BACKUP_PLAN` is seeded from the loaded
        `ENABLE_GKE_BACKUP_PLAN`, so on a re-run that says nothing about the
        backup plan the "flag value" this function is handed is the file's own
        spelling. Canonicalising one side and not the other therefore reported
        the file as disagreeing with itself, and printed the BackupPlan's
        destruction as the consequence of a flag nobody passed.

        The file loaded and the file written to are the same one here, as they
        are in the real call: `bootstrap_install_env_file "$INSTALL_ENV_FILE"`.
        """
        for recorded in ("true", "false", "True", "yes", "1", "on", "off", "no"):
            with self.subTest(recorded=recorded):
                proc = self._parse(
                    "",
                    'bootstrap_install_env_file "$KUBE_AGENTS_INSTALL_ENV" v1.2.3',
                    contents=f"ENABLE_GKE_BACKUP_PLAN={recorded}\n",
                )
                self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                self.assertNotIn(
                    "applies to this run only", proc.stdout + proc.stderr
                )

    def test_the_namespace_flag_is_compared_literally(self):
        """NAMESPACE is a string, and `true`/`false` are legal namespace names.

        Routing it through is_truthy as well would read a recorded `yes` and a
        flagged `true` as agreement and skip a warning about a release moving.
        """
        with tempfile.TemporaryDirectory() as tmp:
            destination = pathlib.Path(tmp) / "existing.env"
            destination.write_text("NAMESPACE=yes\n")
            proc = self._parse(
                "--agent-namespace=true",
                f'bootstrap_install_env_file "{destination}" v1.2.3',
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            combined = proc.stdout + proc.stderr
            self.assertIn("applies to this run only", combined)
            self.assertIn("NAMESPACE=yes", combined)

    def test_empty_gvisor_value_is_not_read_back_as_true(self):
        """`--enable-gvisor=` stays empty so the validator can reject it.

        flag_bool_value returns true only for the BARE form. Collapsing the two
        would send the empty string back as the default and lose the rejection.
        """
        proc = self._parse("--enable-gvisor=", 'echo "G=[$PARAM_ENABLE_GVISOR]"')
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("G=[]", proc.stdout)

    def test_flag_bool_value_handles_a_value_containing_equals(self):
        """Only the FIRST `=` separates the flag from its value."""
        proc = self._parse(
            "--slack-home-channel-name=a=b",
            'echo "V=$PARAM_SLACK_HOME_CHANNEL_NAME"',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("V=a=b", proc.stdout)

    def test_several_slack_bot_tokens_reach_the_variable_unsplit(self):
        """One token per workspace, comma-separated, is a supported shape.

        SlackRelay splits SLACK_BOT_TOKEN on commas and keys a client per
        team_id, which is how one install serves several Slack workspaces. The
        flag is the only new way in, and it must hand the value over whole:
        splitting, trimming to the first token, or rejecting the comma as a
        malformed secret would each remove multi-workspace support while every
        other test stayed green.
        """
        tokens = "xoxb-workspace-one,xoxb-workspace-two"
        proc = self._parse(
            f"--slack-bot-token={tokens}",
            'echo "T=[$PARAM_SLACK_BOT_TOKEN]"',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn(f"T=[{tokens}]", proc.stdout)

    def test_the_help_text_says_the_bot_token_flag_takes_several(self):
        """A capability nobody can find from `--help` is one nobody uses.

        Asserted because the singular wording it replaces was accurate about
        the common case and wrong about the feature, which is exactly the kind
        of line that gets "tidied" back.
        """
        proc = subprocess.run(
            ["bash", str(_INSTALL_SH), "--help"],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(),
            cwd=str(_REPO_ROOT),
        )
        help_text = proc.stdout + proc.stderr
        self.assertIn("--slack-bot-token=TOKENS", help_text)
        self.assertRegex(help_text, r"Comma-separated Slack bot tokens")


class SlackRequiresTokensNonInteractivelyTest(unittest.TestCase):
    """--enable-slack with nobody to ask for the tokens is refused.

    The socket-mode relay cannot connect without both, and the failure would
    otherwise appear as a CrashLoopBackOff long after a successful-looking
    apply.

    One guard does this, require_slack_tokens_after_recovery, and it runs after
    write_tfvars_from_state -- the only thing that can still supply the tokens
    -- and before the apply. It is a function, so these exercise it; the two
    ordering tests read the file, because reaching that point in main() needs a
    cluster.
    """

    _REFUSAL = "--enable-slack needs a bot token and an app token"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._install_env = pathlib.Path(tmp.name) / "install.env"
        self._install_env.write_text("")

    def _run_deferred_check(self, bot="", app="", enabled="true", before=""):
        """Execute require_slack_tokens_after_recovery on a given recovery.

        The variables are assigned after the source rather than exported into
        the environment, because install.sh seeds its own copies at file scope
        and would overwrite them.
        """
        script = (
            f'source "{_INSTALLER_COMMON}"\n'
            f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
            f"SLACK_ENABLED={shlex.quote(enabled)}\n"
            f"SLACK_BOT_TOKEN={shlex.quote(bot)}\n"
            f"SLACK_APP_TOKEN={shlex.quote(app)}\n"
            'PARAM_NON_INTERACTIVE="true"\n'
            f"{before}\n"
            "require_slack_tokens_after_recovery\n"
            'echo "REACHED_THE_APPLY"\n'
        )
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(
                overrides={"KUBE_AGENTS_INSTALL_ENV": str(self._install_env)}
            ),
            cwd=str(_REPO_ROOT),
        )

    def test_the_deferred_check_refuses_when_the_recovery_found_nothing(self):
        """The run this guard exists for: both tokens still missing after it."""
        proc = self._run_deferred_check()
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("REACHED_THE_APPLY", proc.stdout)
        combined = proc.stdout + proc.stderr
        self.assertIn("--slack-bot-token (SLACK_BOT_TOKEN)", combined)
        self.assertIn("--slack-app-token (SLACK_APP_TOKEN)", combined)

    def test_the_deferred_check_refuses_on_one_token_and_names_only_it(self):
        """Half a recovery is still a relay that cannot open a socket."""
        proc = self._run_deferred_check(bot="xoxb-recovered")
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertIn("--slack-app-token (SLACK_APP_TOKEN)", combined)
        self.assertNotIn("--slack-bot-token (SLACK_BOT_TOKEN)", combined)

    def test_a_recovery_that_found_both_tokens_proceeds(self):
        """The unattended PERSIST_SECRETS_ON_DISK=false re-run this unblocks."""
        proc = self._run_deferred_check(bot="xoxb-recovered", app="xapp-recovered")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("REACHED_THE_APPLY", proc.stdout)

    def test_an_install_without_slack_is_never_asked_for_tokens(self):
        proc = self._run_deferred_check(enabled="false")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("REACHED_THE_APPLY", proc.stdout)

    def test_the_deferred_check_reads_the_recovered_tokens_not_the_flags(self):
        """PARAM_SLACK_* is what the operator passed; SLACK_* is what exists.

        write_tfvars_from_state exports the tokens it recovered from the live
        Secret under the unprefixed names, and this check runs on those. Reading
        PARAM_SLACK_* instead would let a run whose recovery found nothing reach
        the apply and CrashLoop, which is the whole failure the guard prevents.
        """
        proc = self._run_deferred_check(
            before='PARAM_SLACK_BOT_TOKEN="xoxb-1"; PARAM_SLACK_APP_TOKEN="xapp-1"'
        )
        self.assertNotEqual(
            proc.returncode,
            0,
            "the flag variables must not stand in for a recovery that failed",
        )
        self.assertNotIn("REACHED_THE_APPLY", proc.stdout)

    def test_the_guard_names_both_tokens(self):
        text = _INSTALL_SH.read_text()
        self.assertIn("--slack-bot-token (SLACK_BOT_TOKEN)", text)
        self.assertIn("--slack-app-token (SLACK_APP_TOKEN)", text)

    def test_an_interactive_run_without_tokens_is_still_refused_after_recovery(self):
        """Even with a TTY, missing tokens after recovery cannot reach apply.

        The interview prompts for the tokens, but an empty response against an
        empty default leaves them empty; if the live Secret recovery also finds
        nothing, proceeding would CrashLoop the relay.
        """
        proc = self._run_deferred_check(
            before='PARAM_NON_INTERACTIVE="false"; has_controlling_tty() { return 0; }'
        )
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("REACHED_THE_APPLY", proc.stdout)
        self.assertIn(self._REFUSAL, proc.stdout + proc.stderr)

    def test_nothing_refuses_slack_before_the_recovery_has_run(self):
        """There is one guard, and it is the one that runs after the recovery.

        A second, earlier copy is the tempting change -- refusing in the chat
        step that asks for Slack reads as failing faster. It is not: the
        Secret-recovery loop inside write_tfvars_from_state can still supply
        both tokens off the live Secret, and it is gated on kubectl's context,
        not on PERSIST_SECRETS_ON_DISK. An earlier copy keyed on that setting
        therefore refuses the fresh-clone adoption the loop exists to serve,
        and buys nothing, because the surviving guard already lands before
        anything is provisioned.

        Counting the refusal is what catches the copy coming back; asserting
        the surviving one is present would not.
        """
        text = _INSTALL_SH.read_text()
        self.assertEqual(
            text.count(self._REFUSAL),
            1,
            "the Slack refusal must exist exactly once, in "
            "require_slack_tokens_after_recovery",
        )
        refusal_at = text.find(self._REFUSAL)
        function_at = text.find("require_slack_tokens_after_recovery() {")
        self.assertNotEqual(function_at, -1)
        self.assertLess(function_at, refusal_at, "the refusal moved out of the guard")

    def test_the_deferred_check_runs_after_the_recovery_and_before_the_apply(self):
        """The guard's position: after the recovery that answers it, before the apply."""
        text = _INSTALL_SH.read_text()
        write_at = text.find('write_tfvars_from_state "$tfvars_file"')
        deferred_at = text.find("\n  require_slack_tokens_after_recovery")
        apply_at = text.find("run_lifecycle_apply \"$repo_dir\" \"$provisioning_log\"")
        self.assertNotEqual(deferred_at, -1, "the deferred Slack check is missing")
        self.assertNotEqual(apply_at, -1)
        self.assertLess(write_at, deferred_at)
        self.assertLess(deferred_at, apply_at)


class ToggleValuesAreValidatedTest(unittest.TestCase):
    """Every --enable-* toggle rejects a value that is not a boolean.

    flag_bool_value extracts what a toggle carries without checking it, and
    every read of the result goes through is_truthy, where anything that is not
    "true" is false. Unchecked, `--enable-slack=ture` provisions an install with
    Slack off and says nothing until the integration is missed.
    """

    def _validate(self, flag, value):
        script = (
            f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
            f'validate_bool_flag_value "{flag}" "{value}"\n'
            'echo "PASSED"\n'
        )
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(),
            cwd=str(_REPO_ROOT),
        )

    def test_a_typo_is_rejected_and_names_the_flag(self):
        proc = self._validate("--enable-slack", "ture")
        self.assertNotIn("PASSED", proc.stdout)
        self.assertIn(
            "--enable-slack must be either true or false",
            proc.stdout + proc.stderr,
        )

    def test_booleans_pass(self):
        for value in ("true", "false"):
            with self.subTest(value=value):
                proc = self._validate("--enable-slack", value)
                self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                self.assertIn("PASSED", proc.stdout)

    def test_an_empty_value_is_refused_and_suggests_the_three_spellings(self):
        """`--enable-slack=` is not "nobody chose"; nobody chose is no flag.

        Only the `=` form reaches here empty — the bare flag yields "true" — so
        the empty string is always something a caller typed, and taking it as
        "leave the setting alone" is what made it dangerous.
        """
        proc = self._validate("--enable-slack", "")
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("PASSED", proc.stdout)
        combined = proc.stdout + proc.stderr
        self.assertIn("empty value", combined)
        self.assertIn("--enable-slack=true", combined)
        self.assertIn("--enable-slack=false", combined)

    # Every toggle validate_bool_flag_value judges at parse time.
    # --enable-gvisor is absent: it has a validator of its own further down in
    # main(), reached because resolve_shared_defaults applies its default with
    # ${VAR-...} and so leaves an empty assignment empty.
    # --enable-hermes-dashboard is here as well as in main() because its default
    # is applied with ${VAR:-...}, which reads the empty assignment as "unset".
    PARSE_TIME_CHECKED_TOGGLES = [
        "--enable-google-chat",
        "--enable-slack",
        "--enable-gke-backup-plan",
        "--enable-pubsub-platform",
        "--enable-stockout-investigator",
        "--enable-hermes-dashboard",
    ]

    def _parse_args(self, *args, **env_overrides):
        """Run parse_args with these arguments, under these environment keys."""
        quoted = " ".join(shlex.quote(a) for a in args)
        script = (
            f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
            f"parse_args {quoted}\n"
            'echo "PASSED"\n'
        )
        env = get_isolated_test_env()
        env.update(env_overrides)
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(_REPO_ROOT),
        )

    def test_every_toggle_rejects_a_typo_at_parse_time(self):
        """A toggle nothing checks is a toggle that fails silently.

        The check lives in parse_args rather than main() because that is the
        last point the value is known to have come from the command line.
        """
        for flag in self.PARSE_TIME_CHECKED_TOGGLES:
            with self.subTest(flag=flag):
                proc = self._parse_args(f"{flag}=ture")
                self.assertNotIn("PASSED", proc.stdout)
                self.assertIn(
                    f"{flag} must be either true or false",
                    proc.stdout + proc.stderr,
                )

    # The environment key each toggle is seeded from, before parse_args runs.
    SEEDED_TOGGLE_ENV_KEYS = {
        "--enable-google-chat": "GOOGLE_CHAT_ENABLED",
        "--enable-slack": "SLACK_ENABLED",
        "--enable-gke-backup-plan": "ENABLE_GKE_BACKUP_PLAN",
        "--enable-pubsub-platform": "ENABLE_PUBSUB_PLATFORM",
        "--enable-stockout-investigator": "ENABLE_STOCKOUT_INVESTIGATOR",
    }

    def test_a_spelling_seeded_from_install_env_is_never_judged(self):
        """`install.env` is hand-written, and is_truthy takes True/yes/y/1/on.

        Every PARAM_* these flags write is also seeded from the loaded
        configuration before parse_args runs. Judging the seeded value with
        ^(true|false)$ would abort a re-run over a spelling the rest of the
        pipeline honours, naming a flag the operator never passed — and CI
        exports three of these keys from GitHub vars.
        """
        for flag, key in self.SEEDED_TOGGLE_ENV_KEYS.items():
            for value in ("True", "yes", "on", "1"):
                with self.subTest(flag=flag, value=value):
                    proc = self._parse_args(**{key: value})
                    self.assertIn(
                        "PASSED",
                        proc.stdout,
                        f"{key}={value} was rejected: {proc.stdout + proc.stderr}",
                    )

    def test_every_toggle_refuses_an_empty_value_against_a_seed(self):
        """`--enable-slack=` with SLACK_ENABLED=true recorded is the bad case.

        The seed is set here deliberately: waving the empty string through as
        "nobody chose" is only tempting while the run has nothing to lose, and
        this is the run that has something to lose. A wrapper expanding an
        unset variable produces exactly this, so the flag arrives without
        anyone having chosen to turn the integration off.
        """
        for flag, key in self.SEEDED_TOGGLE_ENV_KEYS.items():
            with self.subTest(flag=flag):
                proc = self._parse_args(f"{flag}=", **{key: "true"})
                self.assertNotIn("PASSED", proc.stdout)
                self.assertIn(
                    f"{flag}= was given an empty value",
                    proc.stdout + proc.stderr,
                )

    def test_an_empty_toggle_resolves_to_the_default_not_the_recorded_value(self):
        """Why the refusal above is a refusal and not a shrug.

        PARAM_ENABLE_SLACK is seeded from SLACK_ENABLED so a re-run that says
        nothing about Slack keeps the relay. The read is
        `${PARAM_ENABLE_SLACK:-$DEFAULT_SLACK_ENABLED}`, and `:-` treats empty
        as unset — so an empty assignment does not restate the recorded value,
        it falls through to the default. Were the validator to accept it, the
        run would disable a working relay and say nothing.
        """
        script = (
            f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
            'echo "SEEDED=${PARAM_ENABLE_SLACK:-$DEFAULT_SLACK_ENABLED}"\n'
            'PARAM_ENABLE_SLACK=""\n'
            'echo "EMPTIED=${PARAM_ENABLE_SLACK:-$DEFAULT_SLACK_ENABLED}"\n'
        )
        env = get_isolated_test_env()
        env["SLACK_ENABLED"] = "true"
        proc = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(_REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("SEEDED=true", proc.stdout)
        self.assertIn("EMPTIED=false", proc.stdout)

    def test_an_empty_hermes_dashboard_value_is_refused_against_a_seed(self):
        """The dashboard toggle reaches the same trap by the same route.

        It is not in SEEDED_TOGGLE_ENV_KEYS because main() does judge its seeded
        value with ^(true|false)$, so the claim that test makes does not hold
        for it. What does hold is the half that bites: PARAM_ENABLE_WEBUI is
        seeded from HERMES_DASHBOARD_ENABLED and resolved with `:-`, so an empty
        assignment fell through to DEFAULT_ENABLE_WEBUI and main() saw a
        well-formed "false" -- a running dashboard taken down by a wrapper
        expanding an unset variable.
        """
        proc = self._parse_args(
            "--enable-hermes-dashboard=", HERMES_DASHBOARD_ENABLED="true"
        )
        self.assertNotIn("PASSED", proc.stdout)
        self.assertIn(
            "--enable-hermes-dashboard= was given an empty value",
            proc.stdout + proc.stderr,
        )

    def test_an_empty_hermes_dashboard_value_would_resolve_to_the_default(self):
        """Why the refusal above is a refusal and not a shrug.

        `:-` here, `-` for --enable-gvisor: the gvisor toggle keeps an empty
        assignment empty so main()'s validator rejects it, and the dashboard's
        does not. Driven through resolve_shared_defaults rather than by
        restating the expansion, which would assert a fact about bash and stay
        green whatever install.sh does with it.
        """
        script = (
            f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
            f'source "{_REPO_ROOT}/scripts/installer/installer_common.sh"\n'
            'PARAM_ENABLE_WEBUI=""\n'
            "resolve_shared_defaults\n"
            'echo "RESOLVED=[$PARAM_ENABLE_WEBUI]"\n'
        )
        env = get_isolated_test_env()
        env["HERMES_DASHBOARD_ENABLED"] = "true"
        proc = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(_REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        # Not "[]": the empty assignment reaches main() as a well-formed
        # "false", which is what makes it silent.
        self.assertIn("RESOLVED=[false]", proc.stdout)
class InstallerHelpersDetachFromTheTerminalTest(PtyChildTestMixin, unittest.TestCase):
    """The helpers that run install.sh functions must not hand them a terminal.

    _run_installer_bash's docstring says why: a prompt reads /dev/tty whenever
    has_controlling_tty finds one, so an attached child hangs a developer's run
    while CI, whose runners have no terminal, stays green. Two halves pin it.
    The runner is proved under pty.fork, which gives this process the terminal
    a developer's shell has: the predicate run bare must see it, or the check
    proves nothing, and run through the runner it must not. Then each helper
    the hang was traced to is shown to call that runner, so dropping the
    detach from one of them fails here, in CI, at once.
    """

    # The pty child runs two bounded bash calls; waiting longer than both lets
    # the child's TimeoutExpired, which names the command and carries its
    # output, reach the report instead of a bare "timed out" from the parent.
    _CHILD_OVERHEAD_SECS = 10
    _READY_TIMEOUT_SECS = 2 * _INSTALLER_RUN_TIMEOUT_SECONDS + _CHILD_OVERHEAD_SECS
    _PROBE = "has_controlling_tty && echo TTY=yes || echo TTY=no"

    def test_the_runner_hides_the_controlling_terminal(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        result_path = pathlib.Path(tmp.name) / "result.json"
        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - runs in the forked child
            self._probe_in_child(result_path)
        self._watch_pty_child(pid, fd)
        result = json.loads(self._await_file(result_path, "the pty child's result"))
        self.assertNotIn("error", result, result.get("error"))
        self.assertEqual(
            "TTY=yes",
            result["bare"],
            f"the forked test process has no controlling terminal, so nothing is checked: {result}",
        )
        self.assertEqual(
            "TTY=no",
            result["runner"],
            f"_run_installer_bash handed install.sh the controlling terminal: {result}",
        )

    def test_every_helper_that_can_prompt_uses_the_runner(self):
        """The three helpers the terminal hang was traced to; the set the fix covers."""
        validation = InstallScriptValidationTest()
        validation.setUp()
        self.addCleanup(validation.doCleanups)
        helpers = {
            "InstallScriptValidationTest._run_install_func": lambda: validation._run_install_func("true"),
            "EnsureExistingClusterNetworkPolicyTest._run": lambda: EnsureExistingClusterNetworkPolicyTest()._run(),
            "EnsureExistingClusterWorkloadIdentityTest._run": lambda: EnsureExistingClusterWorkloadIdentityTest()._run(),
        }
        for name, call in helpers.items():
            with self.subTest(helper=name):
                with mock.patch.object(sys.modules[__name__], "_run_installer_bash") as runner:
                    call()
                self.assertEqual(runner.call_count, 1, f"{name} did not go through _run_installer_bash")
                body = runner.call_args.args[0]
                self.assertIn(f'source "{_INSTALL_SH}"', body, f"{name} handed the runner a body that does not source install.sh")

    @classmethod
    def _probe_in_child(cls, result_path):
        """In the pty child: run the probe bare, then through the runner; record both; exit."""
        result = {}
        try:
            os.chdir(str(_REPO_ROOT))
            with tempfile.TemporaryDirectory() as tmp:
                empty_env = pathlib.Path(tmp) / "install.env"
                empty_env.write_text("")
                env = get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": str(empty_env)})
                body = f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n{cls._PROBE}'
                bare = subprocess.run(
                    ["bash", "-c", body],
                    capture_output=True,
                    text=True,
                    timeout=_INSTALLER_RUN_TIMEOUT_SECONDS,
                    env=env,
                    cwd=str(_REPO_ROOT),
                )
                runner = _run_installer_bash(body, env)
            result = {
                "bare": bare.stdout.strip(),
                "runner": runner.stdout.strip(),
                "runner_stderr": runner.stderr,
            }
        except BaseException as exc:  # the parent reports it
            result = {"error": repr(exc)}
        finally:
            try:
                # Written whole then renamed, so the parent's poll never reads a
                # half-written file.
                staging = result_path.with_suffix(".partial")
                staging.write_text(json.dumps(result))
                os.replace(staging, result_path)
            finally:
                os._exit(0)


class NoChatTerminalAccessTest(unittest.TestCase):
    """The installer names a way in when no chat platform is chosen.

    Both chat platforms default off, so this is the common install, and nothing
    else in a run tells the operator how to reach the agent.

    These read install.sh rather than running it: the code is inside main(),
    which the KUBE_AGENTS_SOURCE_ONLY harness cannot drive.
    """

    _FUNCTION = "  _prompt_no_chat_enabled() {"
    _SIBLINGS = ("  _prompt_google_chat_settings() {", "  _prompt_slack_settings() {")
    _HEADING = "--- [Talking to the Agent from a Terminal] ---"
    _GET_CREDENTIALS = "gcloud container clusters get-credentials ${cluster_name}"
    _EXEC = "kubectl exec -it deploy/${PLATFORM_AGENT_DEPLOYMENT}"
    _FLAG_RESOLUTION = 'gke_dns_endpoint_flag "$cluster_name" "$region" "$project_id"'

    @classmethod
    def setUpClass(cls):
        cls.source = _INSTALL_SH.read_text()

    def test_the_none_arm_delegates_to_the_function(self):
        """Arm 4 handles the no-chat case, and delegates rather than inlining."""
        self.assertIn(self._FUNCTION, self.source)
        self.assertRegex(self.source, r"\n    4\)\n      _prompt_no_chat_enabled\n      ;;")

    def test_the_completion_banner_prints_it_too(self):
        """Chat and Slack instruct at the end of the run, and so does this.

        By then the chat step is a Terraform plan away. Arm 4 alone would leave
        the feature looking present while nobody sees it, so the second call is
        pinned separately from the first.
        """
        banner = self.source.index("print_instructions_slack.sh")
        self.assertLess(
            banner,
            self.source.rindex("_prompt_no_chat_enabled"),
            "the no-chat block must also print beside the two chat printers",
        )
        self.assertIn('if [ "$chat_choice" = "4" ]; then', self.source)

    def test_it_is_defined_beside_the_arms_it_is_a_sibling_of(self):
        for sibling in self._SIBLINGS:
            self.assertIn(sibling, self.source)
        self.assertLess(
            max(self.source.index(s) for s in self._SIBLINGS),
            self.source.index(self._FUNCTION),
            "the four chat outcomes are read together, so keep their handlers together",
        )

    def test_nothing_reconstructs_the_no_chat_case_from_the_toggles(self):
        """The case statement decided it; a second derivation could disagree.

        Re-testing the toggles later has to be revisited for every platform
        added, and when it is not, an install with working chat is told it has
        none.
        """
        self.assertNotIn("any_chat_enabled", self.source)

    def test_it_prints_both_commands(self):
        body = self._function_body()
        self.assertIn(self._HEADING, body)
        self.assertIn(self._GET_CREDENTIALS, body)
        self.assertIn(self._EXEC, body)

    def test_the_exec_names_the_container_and_the_profile(self):
        # Without -c, kubectl picks the first of the pod's three containers.
        # Without -p, the session is the Planning Agent, not the Platform Agent.
        body = self._function_body()
        self.assertIn("-c ${PLATFORM_AGENT_CONTAINER}", body)
        self.assertIn("hermes -p ${PLATFORM_AGENT_HERMES_PROFILE}", body)

    def test_the_namespace_is_the_one_the_install_uses(self):
        # A literal kubeagents-system would print a command that fails on the
        # installs that passed --agent-namespace.
        self.assertIn(
            "-n ${NAMESPACE:-$DEFAULT_NAMESPACE}",
            self._function_body(),
        )

    def test_the_dns_endpoint_flag_is_the_resolved_one(self):
        # gcloud rejects --dns-endpoint on clusters without an external DNS
        # endpoint, so the printed flag has to be the resolved one -- which
        # means gke_dns_endpoint_flag has to run first.
        body = self._function_body()
        printed = body.index(self._GET_CREDENTIALS)
        self.assertIn("${GKE_DNS_ENDPOINT_FLAG:+ ${GKE_DNS_ENDPOINT_FLAG}}", body)
        self.assertLess(
            body.index(self._FLAG_RESOLUTION),
            printed,
            "the command reads GKE_DNS_ENDPOINT_FLAG, so it must be resolved above it",
        )
        self.assertNotIn(
            " --dns-endpoint",
            body[printed : printed + 400],
            "the printed flag must be the resolved one",
        )

    def test_it_says_how_to_add_a_chat_platform_later(self):
        body = self._function_body()
        self.assertIn("--enable-google-chat", body)
        self.assertIn("--enable-slack", body)

    def _function_body(self):
        """The function's text alone.

        Some of these strings also appear in the completion banner, so matching
        against the whole file would pass on a body that had lost the line.
        """
        start = self.source.index(self._FUNCTION)
        return self.source[start : self.source.index("\n  }\n", start)]


class BannerColourVariablesAreDefinedTest(unittest.TestCase):
    """Every C_* install.sh interpolates is one install.sh itself defines.

    install.sh does not source scripts/installer/common.sh, which declares a
    wider palette (C_WHITE among it). Under `set -Eeuo pipefail` naming one of
    those is not a cosmetic slip: the unset variable aborts the run at the line
    that reads it, and most of this palette is used in the completion banner --
    so the failure lands after a successful apply, on the last thing an
    operator sees.
    """

    def test_no_undefined_colour_is_interpolated(self):
        source = _INSTALL_SH.read_text()
        used = set(re.findall(r"\$\{(C_[A-Z_]+)\}", source))
        used |= set(re.findall(r"\$(C_[A-Z_]+)\b", source))
        defined = set(re.findall(r"\b(C_[A-Z_]+)=", source))
        self.assertEqual(
            sorted(used - defined),
            [],
            "install.sh interpolates colour variables it does not define; "
            "they come from scripts/installer/common.sh, which it does not source",
        )



if __name__ == "__main__":
    unittest.main()
