"""Unit tests for upgrade.sh validation and execution routines.

Tests pure numeric SemVer (X.Y.Z) references, 40-character commit SHAs,
piped stdin execution, and source ref alignment in upgrade.sh.
"""

import os
import pathlib
import re
import subprocess
import tempfile
import time
import unittest

from tests.testing.common import (
    INVALID_IMMUTABLE_REFS,
    UPGRADER_HELP_BANNER,
    VALID_IMMUTABLE_REFS,
    get_isolated_test_env,
)
from tests.testing.release import (
    MOCK_RELEASE_BUNDLE_VERSION,
    create_mock_release_bundle_marker,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_UPGRADE_SH = _REPO_ROOT / "upgrade.sh"


class UpgradeScriptValidationTest(unittest.TestCase):
    def setUp(self):
        # upgrade.sh looks for an install.env in the repository directory and in
        # the working directory, so without a pointer a developer's own
        # install.env would be found and loaded. An empty file, rather than a
        # path that does not exist, because a front door refuses to start when
        # KUBE_AGENTS_INSTALL_ENV names a file it cannot read.
        temp_dir = tempfile.TemporaryDirectory(prefix="upgrade-install-env-")
        self.addCleanup(temp_dir.cleanup)
        self.empty_install_env = pathlib.Path(temp_dir.name) / "install.env"
        self.empty_install_env.write_text("")

    def _run_upgrade_func(self, func_call, env=None, cwd=None):
        """Source upgrade.sh in test mode and run the given function call."""
        setup = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{_UPGRADE_SH}"
{func_call}
"""
        overrides = {"KUBE_AGENTS_INSTALL_ENV": str(self.empty_install_env)}
        if env:
            overrides.update(env)
        full_env = get_isolated_test_env(overrides=overrides)
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

    def test_verify_local_source_ref_accepts_release_bundle_marker_in_non_git_dir(self):
        """Verifies verify_local_source_ref in upgrade.sh logs bundle provenance attribution when .release-bundle matches baked version."""
        import tempfile

        with tempfile.TemporaryDirectory(prefix="unpacked-upgrade-bundle-") as outer_dir:
            archive_dir = pathlib.Path(outer_dir) / f"kube-agents-{MOCK_RELEASE_BUNDLE_VERSION}"
            create_mock_release_bundle_marker(archive_dir)

            cmd = f'BAKED_RELEASE_VERSION="{MOCK_RELEASE_BUNDLE_VERSION}"; verify_local_source_ref "{archive_dir}" "{MOCK_RELEASE_BUNDLE_VERSION}"'
            proc = self._run_upgrade_func(cmd, cwd=archive_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(f"Verified upgrade sources match official release bundle {MOCK_RELEASE_BUNDLE_VERSION}", proc.stdout)

    def test_verify_local_source_ref_rejects_unbaked_release_bundle_marker_in_non_git_dir(self):
        """Verifies .release-bundle marker cannot bypass unversioned source directory rejection in upgrade.sh when baked version is empty."""
        import tempfile

        with tempfile.TemporaryDirectory(prefix="unpacked-unbaked-upgrade-") as outer_dir:
            archive_dir = pathlib.Path(outer_dir) / f"kube-agents-{MOCK_RELEASE_BUNDLE_VERSION}"
            create_mock_release_bundle_marker(archive_dir)

            cmd = f'BAKED_RELEASE_VERSION=""; verify_local_source_ref "{archive_dir}" "{MOCK_RELEASE_BUNDLE_VERSION}"'
            proc = self._run_upgrade_func(cmd, cwd=archive_dir)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Refusing to upgrade from an unversioned source directory", proc.stdout)

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

    def test_health_verification_covers_the_pods_that_run_the_commands(self):
        """A healthy gateway is not a working install.

        The agent executes nothing in its own pod: shell commands go to the
        sandbox StatefulSet over ssh and credentialed ones through the proxy.
        Step 5 verified the gateway alone, so an upgrade that left either of
        those unready still printed "verified healthy" -- and the symptom
        arrives later, as an agent that cannot run kubectl.
        """
        source = _UPGRADE_SH.read_text()
        # Spelled through installer_common.sh's chart-contract constants, so the
        # constant's value is checked too: a renamed constant that no longer
        # holds the object's name would otherwise still pass.
        common = (_REPO_ROOT / "scripts" / "installer" / "installer_common.sh").read_text()
        for kind, constant, name in (
            ("statefulset", "PLATFORM_AGENT_SHELL_STATEFULSET", "platform-agent-shell"),
            ("deployment", "PLATFORM_AGENT_CREDENTIAL_PROXY_DEPLOYMENT", "platform-agent-credential-proxy"),
        ):
            with self.subTest(target=f"{kind}/{name}"):
                self.assertIn(f'readonly {constant}="{name}"', common)
                self.assertIn(f'kubectl rollout status "{kind}/${{{constant}}}"', source)

    def test_an_install_env_only_install_still_records_the_override(self):
        """The guard must not lose the override, only the file write: the
        exports right after are what the rest of the run reads."""
        source = _UPGRADE_SH.read_text()
        for var in ("PROJECT_ID", "CLUSTER_NAME", "REGION"):
            with self.subTest(var=var):
                self.assertIn(f'export {var}="$target_', source)


class DirtyCheckoutRefusalTest(unittest.TestCase):
    """A tagless upgrade still applies this checkout to a live install.

    `--image-tag` makes three refusals possible at once, and only the middle one
    — does HEAD match the requested ref — actually needs a tag. Gating the whole
    set on the tag's presence would let `--keep-image-tag` carry uncommitted
    edits to `terraform/` or `charts/` into a real `terraform apply`: an install
    running a composition that exists in no commit and that nobody can diff.
    """

    def _run(self, func_call, env=None, cwd=None):
        setup = (f'KUBE_AGENTS_SOURCE_ONLY=true source "{_UPGRADE_SH}"\n'
                 f"{func_call}\n")
        return subprocess.run(
            ["bash", "-c", setup], capture_output=True, text=True,
            env=get_isolated_test_env(overrides=env), cwd=str(cwd or _REPO_ROOT),
        )

    def _repo(self, tmp, dirty):
        """A real git checkout, clean or with a tracked file modified."""
        subprocess.run(["git", "init", "-q", tmp], check=True)
        for cmd in (["config", "user.email", "t@example.com"],
                    ["config", "user.name", "T"]):
            subprocess.run(["git", "-C", tmp, *cmd], check=True)
        target = os.path.join(tmp, "main.tf")
        with open(target, "w") as handle:
            handle.write("# committed\n")
        subprocess.run(["git", "-C", tmp, "add", "."], check=True)
        subprocess.run(["git", "-C", tmp, "commit", "-qm", "init"], check=True)
        if dirty:
            with open(target, "a") as handle:
                handle.write("# uncommitted local edit\n")
        return tmp

    def test_a_dirty_checkout_is_refused_without_a_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp, dirty=True)
            proc = self._run(f'verify_local_source_clean "{repo}"')
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn("dirty checkout", proc.stdout + proc.stderr)

    def test_a_clean_checkout_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp, dirty=False)
            proc = self._run(f'verify_local_source_clean "{repo}"')
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_an_unversioned_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run(f'verify_local_source_clean "{tmp}"')
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn("unversioned source directory", proc.stdout + proc.stderr)

    def test_the_previews_warn_instead_of_refusing(self):
        """--plan and --dry-run change nothing, and a plan of a tree mid-edit is
        the one command that answers "what have I changed here"."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp, dirty=True)
            for flag in ("PARAM_PLAN", "PARAM_DRY_RUN"):
                with self.subTest(flag=flag):
                    proc = self._run(
                        f'{flag}=true; verify_local_source_clean "{repo}"')
                    self.assertEqual(proc.returncode, 0,
                                     proc.stdout + proc.stderr)
                    self.assertIn("uncommitted source changes",
                                  proc.stdout + proc.stderr)

    def test_the_tagless_paths_call_it(self):
        """A run with no ref is verified clean, whichever arm found its sources.

        acquire_upgrade_sources verifies once, after the arms: with a ref it
        compares against the ref, and without one it still refuses a dirty tree,
        because a tagless run applies that tree to a live install all the same.
        """
        source = _UPGRADE_SH.read_text()
        self.assertEqual(source.count('verify_local_source_clean "$resolved_dir"'), 1)
        guard = source.index('if [ -n "$expected_ref" ]; then\n    verify_local_source_ref "$resolved_dir" "$expected_ref"\n  else\n    verify_local_source_clean "$resolved_dir"')
        self.assertLess(source.index("acquire_upgrade_sources() {"), guard)


class InteractiveImageTagPromptTest(unittest.TestCase):
    """A bare Enter at the tag prompt has to be a hard error.

    `--plan` and `--keep-image-tag` make the tag optional, so
    `validate_immutable_ref` — whose first branch rejects an empty ref — runs
    only when a tag is present. Nothing else catches an empty answer: without an
    explicit check it skips `verify_local_source_ref` (the dirty-checkout
    refusal) and silently becomes `--keep-image-tag`.

    Driven through a pty rather than asserted against the source, because the
    prompt reads from /dev/tty specifically so that it cannot be fed on stdin.
    """

    def _answer_prompt_with_enter(self):
        import pty
        import select

        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - replaced by execve
            # os._exit, not an exception: a raise here would unwind inside a
            # forked copy of the test runner and report a second suite result.
            try:
                os.chdir(str(_REPO_ROOT))
                os.execve(
                    "/bin/bash",
                    ["bash", str(_UPGRADE_SH)],
                    {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                     "HOME": os.environ.get("HOME", "/tmp"), "TERM": "dumb"},
                )
            finally:
                os._exit(127)
        out = b""
        answered = False
        # A cap rather than a wait: if the guard ever regresses, the run does
        # not hang the suite, it proceeds and this fails on the exit code.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.5)
            if ready:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:  # the child closed the pty
                    break
                if not chunk:
                    break
                out += chunk
            if not answered and b"Target image tag" in out:
                os.write(fd, b"\n")
                answered = True
        else:
            os.kill(pid, 9)
            self.fail("upgrade.sh did not exit within 30s of the empty answer")
        _, status = os.waitpid(pid, 0)
        self.assertTrue(answered, "the tag prompt never appeared")
        return status, out.decode(errors="replace")

    def test_a_bare_enter_at_the_prompt_aborts(self):
        status, out = self._answer_prompt_with_enter()
        self.assertTrue(os.WIFEXITED(status), f"upgrade.sh was signalled: {out}")
        self.assertEqual(os.WEXITSTATUS(status), 1, out)
        self.assertIn("--image-tag is required", out)
        # And it names the flag that asks for what an empty answer looked like
        # it might have meant, rather than leaving the reader to find it.
        self.assertIn("--keep-image-tag", out)

    def test_it_stops_before_touching_the_install(self):
        """Nothing may run between the empty answer and the exit."""
        _, out = self._answer_prompt_with_enter()
        for forbidden in ("get-credentials", "terraform", "helm"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, out)

    def test_full_upgrade_checks_service_account_ownership_before_the_apply(self):
        """A minter or Vertex GSA is first planned on the upgrade that enables it (#1294)."""
        text = (_REPO_ROOT / "upgrade.sh").read_text()
        check_idx = text.index("check_service_account_ownership || exit 1")
        apply_idx = text.index("apply -auto-approve -input=false")
        self.assertLess(check_idx, apply_idx)

    def test_upgrade_invokes_ensure_clean_helm_release(self):
        text = (_REPO_ROOT / "upgrade.sh").read_text()
        self.assertIn('ensure_clean_helm_release "$KUBE_AGENTS_HELM_RELEASE" "$target_namespace"', text)

    def test_upgrade_confirms_agent_image_before_rollout_status(self):
        text = (_REPO_ROOT / "upgrade.sh").read_text()
        confirm_idx = text.index('confirm_agent_image.sh" "$target_namespace" "$PLATFORM_AGENT_DEPLOYMENT"')
        rollout_idx = text.index('rollout status "deployment/${PLATFORM_AGENT_DEPLOYMENT}" -n "$target_namespace" --timeout=900s')
        self.assertLess(confirm_idx, rollout_idx)

    def test_upgrade_confirms_agent_image_scoped_to_harness_and_full_modes(self):
        text = (_REPO_ROOT / "upgrade.sh").read_text()
        self.assertIn('[ "$PARAM_UPGRADE_MODE" = "harness" ] || [ "$PARAM_UPGRADE_MODE" = "full" ]', text)
        self.assertIn('kubectl get deployment "$PLATFORM_AGENT_DEPLOYMENT" -n "$target_namespace"', text)



class UpgradeReusesTheInstallCheckoutTest(unittest.TestCase):
    """upgrade.sh moves the checkout install.sh left behind, rather than fetching its own.

    The install one-liner leaves its sources — and the install's install.env —
    in HOME/kube-agents. Before this, the upgrade one-liner fetched a fresh copy
    into a temporary directory, which has sources and no configuration, and the
    run then refused to upgrade without configuration. These tests mirror
    tests/test_install_script.py, whose refresh_existing_clone this one copies.
    """

    @staticmethod
    def _git(*args, cwd):
        return subprocess.run(
            ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
        ).stdout.strip()

    def _existing_clone_fixture(self, checked_out_tag, full_clone=False):
        """A clone of an earlier release under HOME, the way an install leaves one.

        A bare "upstream" holds tags 0.2.0 and 0.3.0, each tracking install.sh
        (the marker refresh_existing_clone requires). The clone is taken while
        only 0.2.0 exists, so it has never seen 0.3.0 — the shape of a checkout
        from an earlier install. Returns (home_dir, clone_dir, upstream_url,
        {tag: commit}).
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
        git("add", "install.sh", cwd=work_dir)
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

    def _refresh_from_outside(self, home_dir, clone_dir, upstream_url, requested_ref):
        """Run refresh_existing_clone from a copy of upgrade.sh outside any checkout.

        KUBE_AGENTS_REPO_URL is overridden after sourcing, because upgrade.sh
        assigns it unconditionally.
        """
        outside_dir = home_dir.parent / "outside"
        outside_dir.mkdir(exist_ok=True)
        isolated_upgrade_sh = outside_dir / "upgrade.sh"
        isolated_upgrade_sh.write_text(_UPGRADE_SH.read_text())
        setup = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{isolated_upgrade_sh}"
KUBE_AGENTS_REPO_URL="{upstream_url}"
refresh_existing_clone "{clone_dir}" "{requested_ref}"
"""
        return subprocess.run(
            ["bash", "-c", setup],
            capture_output=True,
            text=True,
            env={"HOME": str(home_dir), "PATH": os.environ["PATH"]},
            cwd=str(outside_dir),
        )

    def _head_of(self, clone_dir):
        return self._git("rev-parse", "HEAD", cwd=clone_dir)

    def test_the_clone_is_moved_to_the_requested_release(self):
        """The upgrade one-liner's whole point: an install at N ends up at N+1."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")

        proc = self._refresh_from_outside(home_dir, clone_dir, upstream_url, "0.3.0")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("fetching '0.3.0'", proc.stdout)
        self.assertIn("Moved", proc.stdout)
        self.assertEqual(self._head_of(clone_dir), commits["0.3.0"])

    def test_the_install_env_in_the_clone_survives_the_move(self):
        """The configuration is why the clone is preferred, so the move must keep it."""
        home_dir, clone_dir, upstream_url, _ = self._existing_clone_fixture("0.2.0")
        (clone_dir / "install.env").write_text('PROJECT_ID="my-gcp-project"\n')

        proc = self._refresh_from_outside(home_dir, clone_dir, upstream_url, "0.3.0")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((clone_dir / "install.env").read_text(), 'PROJECT_ID="my-gcp-project"\n')

    def test_a_clone_already_at_the_release_is_not_fetched_into(self):
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")

        proc = self._refresh_from_outside(home_dir, clone_dir, upstream_url, "0.2.0")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("already at '0.2.0'", proc.stdout)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])

    def test_a_dirty_clone_is_left_alone(self):
        """Local changes are never fetched over; the run stops at verify_local_source_ref instead."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")
        (clone_dir / "install.sh").write_text("local edits\n")

        proc = self._refresh_from_outside(home_dir, clone_dir, upstream_url, "0.3.0")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("the checkout is dirty", proc.stdout)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])

    def test_a_directory_that_is_not_a_kube_agents_repository_is_left_alone(self):
        """A repository that merely shares the directory name is never moved."""
        home_dir, clone_dir, upstream_url, _ = self._existing_clone_fixture("0.2.0")
        unrelated = home_dir / "unrelated"
        unrelated.mkdir()
        self._git("init", "-b", "main", cwd=unrelated)
        self._git("config", "user.name", "Test", cwd=unrelated)
        self._git("config", "user.email", "test@example.com", cwd=unrelated)
        self._git("config", "commit.gpgsign", "false", cwd=unrelated)
        (unrelated / "README.md").write_text("not kube-agents\n")
        self._git("add", "README.md", cwd=unrelated)
        self._git("commit", "-m", "init", cwd=unrelated)
        before = self._head_of(unrelated)

        proc = self._refresh_from_outside(home_dir, unrelated, upstream_url, "0.3.0")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("is not a kube-agents revision", proc.stdout)
        self.assertEqual(self._head_of(unrelated), before)

    def test_a_complete_clone_does_not_become_shallow(self):
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0", full_clone=True)

        proc = self._refresh_from_outside(home_dir, clone_dir, upstream_url, "0.3.0")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._head_of(clone_dir), commits["0.3.0"])
        self.assertEqual(self._git("rev-parse", "--is-shallow-repository", cwd=clone_dir), "false")

    def test_the_tagless_arms_never_reach_the_clone(self):
        """--plan and --keep-image-tag still require a checkout; HOME is not a substitute.

        A CI job that checked out the ref it reconciles must keep that tree, so
        the preference for the installer's clone lives inside the arm that has a
        tag and no checkout, after the tagless refusal. The behaviour of that
        arm is covered below; this pins where it sits.
        """
        text = _UPGRADE_SH.read_text()
        tagless_refusal = text.index('--plan and --keep-image-tag have to run from a kube-agents checkout')
        clone_preference = text.index('refresh_existing_clone "$resolved_dir" "$expected_ref"')
        self.assertLess(tagless_refusal, clone_preference)

    def _acquire_from_outside(
        self, home_dir, upstream_url, requested_ref, preview_flag=None, cwd=None, baked_version=None
    ):
        """Run acquire_upgrade_sources from a copy of upgrade.sh outside any checkout.

        The copy is what makes the clone arm reachable: sourced from a directory
        with no scripts/installer/installer_common.sh, the script has no checkout
        of its own, which is the shape of `curl … | bash`.
        """
        outside_dir = home_dir.parent / "outside"
        outside_dir.mkdir(exist_ok=True)
        isolated_upgrade_sh = outside_dir / "upgrade.sh"
        isolated_upgrade_sh.write_text(_UPGRADE_SH.read_text())
        preview_line = f'{preview_flag}="true"' if preview_flag else ":"
        baked_line = f'BAKED_RELEASE_VERSION="{baked_version}"' if baked_version else ":"
        setup = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{isolated_upgrade_sh}"
KUBE_AGENTS_REPO_URL="{upstream_url}"
{preview_line}
{baked_line}
repo_dir=""
install_checkout=""
acquire_upgrade_sources repo_dir install_checkout "{requested_ref}"
echo "REPO_DIR=$repo_dir"
echo "INSTALL_CHECKOUT=$install_checkout"
"""
        return subprocess.run(
            ["bash", "-c", setup],
            capture_output=True,
            text=True,
            env={"HOME": str(home_dir), "PATH": os.environ["PATH"]},
            cwd=str(cwd or outside_dir),
        )

    @staticmethod
    def _reported(proc, key):
        for line in proc.stdout.splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1]
        return None

    def test_the_install_checkout_becomes_the_upgrade_sources(self):
        """The documented one-liner: no checkout of its own, so it uses the install's."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")

        proc = self._acquire_from_outside(home_dir, upstream_url, "0.3.0")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._reported(proc, "REPO_DIR"), str(clone_dir))
        self.assertEqual(self._reported(proc, "INSTALL_CHECKOUT"), str(clone_dir))
        self.assertEqual(self._head_of(clone_dir), commits["0.3.0"])

    def test_a_plain_directory_at_the_clone_path_is_not_adopted(self):
        """A stale bundle or a copied tree in HOME is not verified release sources.

        verify_local_source_ref accepts anything that is not a Git worktree once
        the baked version equals the requested ref — the default on a release
        copy — so adopting the directory on its existence alone would announce
        an unrelated tree as verified and then apply it to a live install.
        """
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")
        stale_bundle = home_dir / "kube-agents-plain"
        stale_bundle.mkdir()
        (stale_bundle / "install.sh").write_text("an unpacked bundle of some other release\n")
        # Put the plain directory where the upgrader looks.
        clone_dir.rename(home_dir / "kube-agents-real")
        stale_bundle.rename(clone_dir)

        proc = self._acquire_from_outside(home_dir, upstream_url, "0.3.0", baked_version="0.3.0")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._reported(proc, "INSTALL_CHECKOUT"), "")
        self.assertNotEqual(self._reported(proc, "REPO_DIR"), str(clone_dir))
        self.assertNotIn("baked official release", proc.stdout)
        self.assertEqual((clone_dir / "install.sh").read_text(), "an unpacked bundle of some other release\n")
        self.assertIn(commits["0.3.0"], proc.stdout)

    def test_an_unrelated_repository_at_the_clone_path_is_not_adopted(self):
        """Sharing the directory name is not enough; HEAD has to be a kube-agents revision."""
        home_dir, clone_dir, upstream_url, _ = self._existing_clone_fixture("0.2.0")
        clone_dir.rename(home_dir / "kube-agents-real")
        clone_dir.mkdir()
        self._git("init", "-b", "main", cwd=clone_dir)
        self._git("config", "user.name", "Test", cwd=clone_dir)
        self._git("config", "user.email", "test@example.com", cwd=clone_dir)
        self._git("config", "commit.gpgsign", "false", cwd=clone_dir)
        (clone_dir / "README.md").write_text("not kube-agents\n")
        self._git("add", "README.md", cwd=clone_dir)
        self._git("commit", "-m", "init", cwd=clone_dir)
        before = self._head_of(clone_dir)

        proc = self._acquire_from_outside(home_dir, upstream_url, "0.3.0")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._reported(proc, "INSTALL_CHECKOUT"), "")
        self.assertNotEqual(self._reported(proc, "REPO_DIR"), str(clone_dir))
        self.assertEqual(self._head_of(clone_dir), before)

    def test_a_plan_does_not_move_the_install_checkout(self):
        """--plan says it changes nothing, and the operator's checkout is part of nothing."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")

        proc = self._acquire_from_outside(home_dir, upstream_url, "0.3.0", preview_flag="PARAM_PLAN")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])
        self.assertNotEqual(self._reported(proc, "REPO_DIR"), str(clone_dir))
        # Still found, because the install's configuration lives in it.
        self.assertEqual(self._reported(proc, "INSTALL_CHECKOUT"), str(clone_dir))
        self.assertIn("a preview does not move it", proc.stdout)

    def test_a_dry_run_does_not_move_the_install_checkout(self):
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")

        proc = self._acquire_from_outside(home_dir, upstream_url, "0.3.0", preview_flag="PARAM_DRY_RUN")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])
        self.assertNotEqual(self._reported(proc, "REPO_DIR"), str(clone_dir))

    def test_a_preview_uses_the_checkout_when_nothing_has_to_move(self):
        """Fetching a second copy of what is already there would be waste, not safety."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")

        proc = self._acquire_from_outside(home_dir, upstream_url, "0.2.0", preview_flag="PARAM_PLAN")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._reported(proc, "REPO_DIR"), str(clone_dir))
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])
        self.assertIn("already at '0.2.0'", proc.stdout)

    def test_a_piped_run_from_inside_the_install_checkout_moves_it(self):
        """Standing in ~/kube-agents when piping upgrade.sh must move the checkout, not fail the ref check."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")

        proc = self._acquire_from_outside(home_dir, upstream_url, "0.3.0", cwd=clone_dir)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._reported(proc, "REPO_DIR"), str(clone_dir))
        self.assertEqual(self._reported(proc, "INSTALL_CHECKOUT"), str(clone_dir))
        self.assertEqual(self._head_of(clone_dir), commits["0.3.0"])

    def test_a_piped_plan_from_inside_the_install_checkout_does_not_move_it(self):
        """A piped --plan run from inside ~/kube-agents keeps it at its current ref and still finds install.env."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")

        proc = self._acquire_from_outside(
            home_dir, upstream_url, "0.3.0", preview_flag="PARAM_PLAN", cwd=clone_dir
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])
        self.assertNotEqual(self._reported(proc, "REPO_DIR"), str(clone_dir))
        self.assertEqual(self._reported(proc, "INSTALL_CHECKOUT"), str(clone_dir))
        self.assertIn("a preview does not move it", proc.stdout)

    def test_a_missing_cli_tool_does_not_move_the_install_checkout(self):
        """Like install.sh, missing CLI tools and conflicting preview flags fail before acquire_upgrade_sources moves ~/kube-agents."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")
        outside_dir = home_dir.parent / "outside"
        outside_dir.mkdir(exist_ok=True)
        isolated_upgrade_sh = outside_dir / "upgrade.sh"
        isolated_upgrade_sh.write_text(
            _UPGRADE_SH.read_text().replace(
                'KUBE_AGENTS_REPO_URL="https://github.com/gke-labs/kube-agents.git"',
                f'KUBE_AGENTS_REPO_URL="{upstream_url}"',
            )
        )
        proc = subprocess.run(
            ["bash", str(isolated_upgrade_sh), "--image-tag=0.3.0", "--non-interactive"],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(overrides={"HOME": str(home_dir), "PATH": "/usr/bin:/bin"}),
            cwd=str(outside_dir),
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Required CLI tool", proc.stdout + proc.stderr)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])


class ConfigurationLookupOrderTest(unittest.TestCase):
    """Which install.env an upgrade loads, when more than one is reachable.

    install.sh resolves: KUBE_AGENTS_INSTALL_ENV -> the script's own checkout ->
    $(pwd) -> the clone in HOME. The upgrade had only the first and the last, and
    when it gained $(pwd) it put the HOME checkout ahead of it — so with two
    installs on one workstation, standing in B's directory and passing
    --cluster-name=B loaded A's chat space, allowed users, model provider,
    NAMESPACE and GitOps repo out of ~/kube-agents/install.env.
    """

    _INSTALLER_COMMON = _REPO_ROOT / "scripts" / "installer" / "installer_common.sh"

    def _resolve(self, repo_dir, install_checkout, cwd, install_env_var=None):
        setup = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{_UPGRADE_SH}"
# default_install_env_file is the last candidate, and it lives here.
source "{self._INSTALLER_COMMON}"
resolve_install_env_file "{repo_dir}" "{install_checkout}"
"""
        env = {"HOME": str(cwd), "PATH": os.environ["PATH"]}
        if install_env_var is not None:
            env["KUBE_AGENTS_INSTALL_ENV"] = str(install_env_var)
        proc = subprocess.run(
            ["bash", "-c", setup],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(cwd),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def _layout(self):
        """A run's own sources, the operator's working directory, and the install checkout."""
        temp_dir = tempfile.TemporaryDirectory(prefix="install-env-order-")
        self.addCleanup(temp_dir.cleanup)
        base = pathlib.Path(temp_dir.name)
        for name in ("sources", "cwd", "checkout"):
            (base / name).mkdir()
        return base

    def test_the_explicit_pointer_wins(self):
        base = self._layout()
        named = base / "named.env"
        named.write_text("")
        (base / "sources" / "install.env").write_text("")
        (base / "cwd" / "install.env").write_text("")
        (base / "checkout" / "install.env").write_text("")

        resolved = self._resolve(base / "sources", base / "checkout", base / "cwd", install_env_var=named)

        self.assertEqual(resolved, str(named))

    def test_the_run_s_own_checkout_wins_over_the_working_directory(self):
        """Running ./upgrade.sh from a checkout loads that checkout's configuration."""
        base = self._layout()
        (base / "sources" / "install.env").write_text("")
        (base / "cwd" / "install.env").write_text("")

        resolved = self._resolve(base / "sources", base / "checkout", base / "cwd")

        self.assertEqual(resolved, str(base / "sources" / "install.env"))

    def test_the_working_directory_wins_over_the_install_checkout(self):
        """The regression this order exists to stop: two installs, one $HOME/kube-agents.

        The piped run's sources ARE the install checkout, so preferring the
        sources' directory would silently load the wrong install's chat space,
        allowed users and namespace.
        """
        base = self._layout()
        (base / "cwd" / "install.env").write_text("")
        (base / "checkout" / "install.env").write_text("")

        resolved = self._resolve(base / "checkout", base / "checkout", base / "cwd")

        self.assertEqual(resolved, str(base / "cwd" / "install.env"))

    def test_the_install_checkout_is_used_when_nothing_is_nearer(self):
        """The documented one-liner, run from a directory with no configuration in it."""
        base = self._layout()
        (base / "checkout" / "install.env").write_text("")

        resolved = self._resolve(base / "checkout", base / "checkout", base / "cwd")

        self.assertEqual(resolved, str(base / "checkout" / "install.env"))

    def test_with_nothing_anywhere_it_names_the_sources_directory(self):
        """Nothing to load: the refusal that follows names where one would live."""
        base = self._layout()

        resolved = self._resolve(base / "sources", "", base / "cwd")

        self.assertEqual(resolved, str(base / "sources" / "install.env"))


class FrontDoorsAgreeOnTheInstallCheckoutTest(unittest.TestCase):
    """install.sh and upgrade.sh have to find and move the same checkout.

    These helpers cannot live in scripts/installer/installer_common.sh, which is
    sourced out of the very checkout they go and find, so each front door
    carries a copy — the arrangement installer_common.sh already describes for
    the install.env loader. Copies drift, so they are pinned here.
    """

    _INSTALL_SH = _REPO_ROOT / "install.sh"

    @staticmethod
    def _function_text(source, name):
        opening = f"\n{name}() {{\n"
        start = source.index(opening) + 1
        end = source.index("\n}\n", start) + len("\n}\n")
        return source[start:end]

    def test_the_helpers_are_identical(self):
        install_sh = self._INSTALL_SH.read_text()
        upgrade_sh = _UPGRADE_SH.read_text()
        for name in ("fetch_source_ref", "refresh_existing_clone"):
            with self.subTest(function=name):
                self.assertEqual(
                    self._function_text(install_sh, name),
                    self._function_text(upgrade_sh, name),
                    f"{name} has drifted between install.sh and upgrade.sh",
                )

    def test_the_clone_constants_are_identical(self):
        install_sh = self._INSTALL_SH.read_text()
        upgrade_sh = _UPGRADE_SH.read_text()
        for constant in ("KUBE_AGENTS_CLONE_MARKER", "KUBE_AGENTS_FETCH_DEPTH_OPT"):
            with self.subTest(constant=constant):
                pattern = rf'^{constant}="([^"]+)"$'
                install_value = re.search(pattern, install_sh, re.MULTILINE)
                upgrade_value = re.search(pattern, upgrade_sh, re.MULTILINE)
                self.assertIsNotNone(install_value, f"install.sh does not declare {constant}")
                self.assertIsNotNone(upgrade_value, f"upgrade.sh does not declare {constant}")
                self.assertEqual(install_value.group(1), upgrade_value.group(1))

    def test_both_front_doors_name_the_same_directory(self):
        """The message HOME:? carries differs; the path it builds may not."""
        with tempfile.TemporaryDirectory(prefix="front-doors-install-env-") as env_dir:
            empty_install_env = pathlib.Path(env_dir) / "install.env"
            empty_install_env.write_text("")
            paths = {}
            for script in (self._INSTALL_SH, _UPGRADE_SH):
                proc = subprocess.run(
                    ["bash", "-c", f'KUBE_AGENTS_SOURCE_ONLY=true source "{script}"; kube_agents_clone_dir'],
                    capture_output=True,
                    text=True,
                    env={
                        "HOME": "/h",
                        "PATH": os.environ["PATH"],
                        "KUBE_AGENTS_INSTALL_ENV": str(empty_install_env),
                    },
                    cwd=str(_REPO_ROOT),
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                paths[script.name] = proc.stdout.strip()
        self.assertEqual(paths["install.sh"], "/h/kube-agents")
        self.assertEqual(paths["upgrade.sh"], paths["install.sh"])


class PipedUpgradeResolvesItsSourcesTest(unittest.TestCase):
    def test_a_piped_run_does_not_abort_on_an_unset_bash_source(self):
        """Under `curl | bash` with `set -u`, BASH_SOURCE[0] may name no file.

        install.sh has always defaulted it; upgrade.sh did not, so the source
        resolution could abort with an unbound variable instead of reporting
        what was wrong. --keep-image-tag reaches that resolution and then stops
        for its own, expected reason.
        """
        with tempfile.TemporaryDirectory(prefix="outside-checkout-") as outside:
            empty_install_env = pathlib.Path(outside) / "pinned-install.env"
            empty_install_env.write_text("")
            proc = subprocess.run(
                ["bash", "-s", "--", "--keep-image-tag", "--non-interactive", "--project-id=my-gcp-project"],
                input=_UPGRADE_SH.read_text(),
                capture_output=True,
                text=True,
                env=get_isolated_test_env(
                    overrides={
                        "KUBE_AGENTS_INSTALL_ENV": str(empty_install_env),
                        "PATH": os.environ["PATH"],
                    }
                ),
                cwd=outside,
            )
        combined = proc.stdout + proc.stderr
        self.assertNotIn("unbound variable", combined)
        self.assertIn("have to run from a kube-agents checkout", combined)


if __name__ == "__main__":
    unittest.main()
