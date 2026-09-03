"""Unit tests for upgrade.sh validation and execution routines.

Tests pure numeric SemVer (X.Y.Z) references, 40-character commit SHAs,
piped stdin execution, and source ref alignment in upgrade.sh.
"""

import os
import pathlib
import re
import subprocess
import unittest

from tests.testing.common import (
    INVALID_IMMUTABLE_REFS,
    UPGRADER_HELP_BANNER,
    VALID_IMMUTABLE_REFS,
    get_isolated_test_env,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_UPGRADE_SH = _REPO_ROOT / "upgrade.sh"


class UpgradeScriptValidationTest(unittest.TestCase):
    def _run_upgrade_func(self, func_call, env=None, cwd=None):
        """Source upgrade.sh in test mode and run the given function call."""
        setup = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{_UPGRADE_SH}"
{func_call}
"""
        full_env = get_isolated_test_env(overrides=env)
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
                proc = self._run_upgrade_func(cmd)
                self.assertEqual(
                    proc.returncode,
                    0,
                    f"upgrade.sh: expected ref '{ref}' to be valid, stderr: {proc.stderr}",
                )

    def test_validate_immutable_ref_rejects_invalid_refs(self):
        for ref in INVALID_IMMUTABLE_REFS:
            with self.subTest(ref=ref):
                cmd = f'validate_immutable_ref "{ref}"'
                proc = self._run_upgrade_func(cmd)
                self.assertNotEqual(
                    proc.returncode,
                    0,
                    f"upgrade.sh: expected ref '{ref}' to be rejected",
                )

    def test_piped_stdin_executes_main(self):
        """Ensures piped curl | bash invocations execute main and do not exit early."""
        upgrade_script_content = _UPGRADE_SH.read_text()
        proc = subprocess.run(
            ["bash", "-s", "--", "--help"],
            input=upgrade_script_content,
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 0, f"Piped execution failed: {proc.stderr}")
        self.assertIn(UPGRADER_HELP_BANNER, proc.stdout)

    def test_verify_local_source_ref_accepts_baked_release_in_non_git_dir(self):
        """Verifies verify_local_source_ref succeeds for unpacked release archive without Git repository."""
        import tempfile

        with tempfile.TemporaryDirectory(prefix="unpacked-upgrade-") as outer_dir:
            archive_dir = pathlib.Path(outer_dir) / "kube-agents-0.2.0"
            archive_dir.mkdir(parents=True)

            cmd = f'BAKED_RELEASE_VERSION="0.2.0"; verify_local_source_ref "{archive_dir}" "0.2.0"'
            proc = self._run_upgrade_func(cmd, cwd=archive_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Verified upgrade sources match baked official release 0.2.0", proc.stdout)

    def test_verify_local_source_ref_in_git_worktree_enforces_git_alignment(self):
        """Verifies verify_local_source_ref in upgrade.sh enforces clean git status in real git checkouts."""
        import tempfile

        with tempfile.TemporaryDirectory(prefix="git-upgrade-repo-") as repo_dir:
            repo_path = pathlib.Path(repo_dir)
            subprocess.run(["git", "init"], cwd=str(repo_path), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo_path), check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_path), check=True)
            (repo_path / "file.txt").write_text("initial\n")
            subprocess.run(["git", "add", "file.txt"], cwd=str(repo_path), check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_path), check=True)
            subprocess.run(["git", "tag", "0.2.0"], cwd=str(repo_path), check=True)

            # Make checkout dirty
            (repo_path / "file.txt").write_text("dirty uncommitted change\n")

            cmd = f'BAKED_RELEASE_VERSION="0.2.0"; verify_local_source_ref "{repo_path}" "0.2.0"'
            proc = self._run_upgrade_func(cmd, cwd=repo_path)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("dirty checkout", proc.stdout)


class PersistStateVarTest(unittest.TestCase):
    """persist_state_var must not create the vars.sh tree this release removed.

    Its grep/mv rewrite tests for the file, but the append that follows is
    unconditional, so the redirect opens a path under k8s-operator/scripts/ --
    a directory nothing creates any more. Under `set -Eeuo pipefail` and the
    ERR trap that aborts the upgrade at step 1.

    Reachable only since install.env: before it, state_loaded could be true
    only if vars.sh existed, so the directory always existed by the time this
    ran. Letting install.env satisfy state_loaded is what exposed the write.
    The invocation that breaks is the one show_help gives as its own example,
    `./upgrade.sh --non-interactive --project-id=... --cluster-name=...`.
    """

    def _persist_into(self, state_file):
        """Call persist_state_var against a path whose parent may not exist."""
        return subprocess.run(
            ["bash", "-c",
             f'KUBE_AGENTS_SOURCE_ONLY=true source "{_UPGRADE_SH}"\n'
             f'persist_state_var "{state_file}" PROJECT_ID a-project\n'
             'echo DONE'],
            capture_output=True, text=True,
            env=get_isolated_test_env(), cwd=str(_REPO_ROOT),
        )

    def test_the_append_needs_a_directory_that_no_longer_exists(self):
        """The mechanism, pinned so the guard below cannot be read as
        redundant: called against a missing tree, the helper itself fails."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            missing = pathlib.Path(tmp) / "k8s-operator" / "scripts" / "vars.sh"
            proc = self._persist_into(missing)
            self.assertNotEqual(
                proc.returncode, 0,
                "persist_state_var appended into a directory that does not exist; "
                "if this now succeeds the callers' [ -f ] guard may be droppable",
            )
            self.assertFalse(missing.exists())

    def test_upgrade_guards_every_persist_call_on_the_file_existing(self):
        """uninstall.sh already wraps the same three calls this way; upgrade.sh
        was the last unguarded writer. Checked against the source rather than
        by driving main(), which needs gcloud and a cluster.

        Checked by walking the block structure, not by a regex bridging from an
        `if [ -f "$state_file" ]` to a `persist_state_var` line. `upgrade.sh`
        contains three such `if` lines — one inside `persist_state_var` itself,
        one on the legacy-state load, one the real guard — and an unanchored
        search takes the leftmost, so a bridging pattern anchors on the helper's
        own internal guard nearly 300 lines away and stays green when the real
        guard is deleted. Same correction as the chat-menu guard.
        """
        lines = _UPGRADE_SH.read_text().splitlines()
        calls = [
            i for i, line in enumerate(lines)
            if re.match(r'\s*persist_state_var "\$state_file" \w+', line)
        ]
        self.assertEqual(
            3, len(calls),
            f"expected the three coordinate persists, found {len(calls)}",
        )
        for i in calls:
            with self.subTest(line=i + 1):
                # Walk back to the nearest enclosing `if` at a lower indent and
                # require it to be the file-existence guard. A guard that is
                # deleted leaves the nearest enclosing `if` as the per-parameter
                # `[ -n "$PARAM_..." ]`, whose own enclosing block is the
                # function body — so this fails exactly when the guard goes.
                # Strictly decreasing indent, so this collects the chain of
                # blocks that actually enclose the call rather than the sibling
                # `fi`s and neighbouring calls that merely sit further left.
                min_indent = len(lines[i]) - len(lines[i].lstrip())
                enclosing = []
                for j in range(i - 1, -1, -1):
                    if not lines[j].strip():
                        continue
                    ind = len(lines[j]) - len(lines[j].lstrip())
                    if ind < min_indent:
                        enclosing.append(lines[j].strip())
                        min_indent = ind
                self.assertTrue(
                    any('[ -f "$state_file" ]' in line for line in enclosing[:3]),
                    "each persist_state_var call must sit inside "
                    '`[ -f "$state_file" ]`; an install.env-only install has no '
                    "vars.sh and the unconditional append aborts the upgrade. "
                    f"Enclosing blocks were: {enclosing[:3]}",
                )

    def test_an_install_env_only_install_still_records_the_override(self):
        """The guard must not lose the override, only the file write: the
        exports right after are what the rest of the run reads."""
        source = _UPGRADE_SH.read_text()
        for var in ("PROJECT_ID", "CLUSTER_NAME", "REGION"):
            with self.subTest(var=var):
                self.assertIn(f'export {var}="$target_', source)


if __name__ == "__main__":
    unittest.main()
