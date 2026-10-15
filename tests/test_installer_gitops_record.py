"""#3075 and #3076: GitOps configured with install.sh's flags survives the next
upgrade, and a GitHub App ID that is not the App's number is refused up front.

- record_github_gitops_keys (install.sh) appends the GitHub GitOps keys an
  install.env lacks when the run commits to the apply.
- refuse_apply_dropping_live_gitops (installer_common.sh) refuses an apply
  that would render GitOps off over a live PlatformAgent that uses it -- the
  apply that destroyed the token minter -- and prints the exact lines to add.
- validate_github_app_id (installer_common.sh) refuses a name or slug.
"""

import pathlib
import stat
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INSTALLER_COMMON = _REPO_ROOT / "scripts" / "installer" / "installer_common.sh"
_INSTALL_SH = _REPO_ROOT / "install.sh"
_UPGRADE_SH = _REPO_ROOT / "upgrade.sh"


class RecordGithubGitopsKeysTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._tmp = pathlib.Path(tmp.name)
        self._loaded = self._tmp / "loaded.env"
        self._loaded.write_text("")

    def _bash(self, body, dry_run="false"):
        full = (f'source "{_INSTALLER_COMMON}"\nKUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                f"PARAM_DRY_RUN={dry_run}\n{body}")
        return subprocess.run(
            ["bash", "-c", full], capture_output=True, text=True, cwd=str(_REPO_ROOT),
            env=get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": str(self._loaded)}),
        )

    def test_the_flags_reach_an_install_env_that_lacks_them(self):
        dest = self._tmp / "install.env"
        dest.write_text("PROJECT_ID=p\nSLACK_ENABLED=true")  # no trailing newline
        proc = self._bash(f'export GITOPS_ORG=acme GITOPS_REPO=infra GITHUB_APP_ID=5319154 GITHUB_PEM_PATH=/k.pem\n'
                          f'record_github_gitops_keys "{dest}"\n')
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        text = dest.read_text()
        for line in ("PROJECT_ID=p", "SLACK_ENABLED=true", "GITOPS_ORG=acme", "GITOPS_REPO=infra",
                     "GITHUB_APP_ID=5319154"):
            self.assertIn(line + "\n", text)
        self.assertNotIn("GITHUB_PEM_PATH", text)
        # And the next upgrade, reading only the file, has all three.
        proc = self._bash(
            "unset GITOPS_ORG GITOPS_REPO GITHUB_APP_ID\n"
            f'load_install_env "{dest}"; echo "org=$GITOPS_ORG repo=$GITOPS_REPO app=$GITHUB_APP_ID"\n'
        )
        self.assertIn("org=acme repo=infra app=5319154", proc.stdout, proc.stderr)

    def test_a_value_the_file_records_is_kept(self):
        dest = self._tmp / "install.env"
        dest.write_text("GITOPS_ORG=acme\nGITOPS_REPO=old-infra\n")
        proc = self._bash(f'export GITOPS_ORG=acme GITOPS_REPO=new-infra GITHUB_APP_ID=7\n'
                          f'record_github_gitops_keys "{dest}"\n')
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertEqual(dest.read_text(), "GITOPS_ORG=acme\nGITOPS_REPO=old-infra\nGITHUB_APP_ID=7\n")

    def test_nothing_is_written_without_a_minter_on_a_dry_run_or_for_gitlab(self):
        for label, exports, dry in (
            ("no app id", "export GITOPS_ORG=acme GITOPS_REPO=infra GITHUB_APP_ID=", "false"),
            ("dry run", "export GITOPS_ORG=acme GITOPS_REPO=infra GITHUB_APP_ID=7", "true"),
            ("gitlab", "export GITOPS_FORGE=gitlab GITOPS_ORG=acme GITOPS_REPO=g/p GITHUB_APP_ID=7", "false"),
        ):
            with self.subTest(label):
                dest = self._tmp / f"{label.replace(' ', '_')}.env"
                dest.write_text("PROJECT_ID=p\n")
                proc = self._bash(f'{exports}\nrecord_github_gitops_keys "{dest}"\n', dry_run=dry)
                self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                self.assertEqual(dest.read_text(), "PROJECT_ID=p\n")

    def test_it_runs_at_the_apply_and_nowhere_earlier(self):
        source = _INSTALL_SH.read_text()
        call = source.index('  record_github_gitops_keys "$INSTALL_ENV_FILE"')
        self.assertEqual(source.count('record_github_gitops_keys "'), 1)
        self.assertLess(source.index('write_json_report "GENERATE_ONLY_SUCCESS"'), call)
        self.assertLess(call, source.index('run_lifecycle_apply "$repo_dir" "$provisioning_log"'))


class RefuseApplyDroppingLiveGitopsTest(unittest.TestCase):
    def _run(self, keys, git_repo="sackheads/ka-test-infra", org="sackheads", app_id_b64="NTMxOTE1NA==",
             mode="", context_present=True):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            kubectl = bin_dir / "kubectl"
            kubectl.write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                f'  *"config get-contexts"*) exit {0 if context_present else 1} ;;\n'
                f"  *\"spec.integration.github.gitRepo\"*) printf '%s' '{git_repo}' ;;\n"
                f"  *\"spec.integration.github.org\"*) printf '%s' '{org}' ;;\n"
                f"  *\"github-app-credentials\"*) printf '%s' '{app_id_b64}' ;;\n"
                "esac\nexit 0\n"
            )
            kubectl.chmod(kubectl.stat().st_mode | stat.S_IEXEC)
            env = {"PROJECT_ID": "p", "CLUSTER_NAME": "c", "REGION": "r",
                   "GITOPS_ORG": "", "GITOPS_REPO": "", "GITHUB_APP_ID": "", "GITOPS_FORGE": "",
                   "TFVARS_ENABLE_GITHUB_MINTER": "false", "KUBE_AGENTS_REMOVE_GITOPS": ""}
            env.update(keys)
            body = (
                'print_error() { echo "ERROR: $*"; }; print_info() { echo "INFO: $*"; }\n'
                'print_warning() { echo "WARN: $*"; }; print_success() { :; }\n'
                f'source "{_INSTALLER_COMMON}"\n'
                f'refuse_apply_dropping_live_gitops kubeagents-system /x/install.env {mode}; echo "rc=$?"\n'
            )
            return subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                                  env=get_isolated_test_env(overrides=env, bin_dir=str(bin_dir)),
                                  cwd=str(_REPO_ROOT))

    def test_a_render_without_the_keys_over_a_live_minter_is_refused_with_the_lines_to_add(self):
        proc = self._run({})
        out = proc.stdout
        self.assertIn("rc=1", out, out + proc.stderr)
        self.assertIn("ERROR: Refusing to apply: the PlatformAgent opens pull requests on sackheads/ka-test-infra", out)
        self.assertIn("destroy the minter", out)
        self.assertIn("INFO: Append these lines to /x/install.env, then re-run the same command:", out)
        for line in ("INFO:   GITOPS_ORG=sackheads", "INFO:   GITOPS_REPO=ka-test-infra", "INFO:   GITHUB_APP_ID=5319154"):
            self.assertIn(line + "\n", out)

    def test_only_the_missing_keys_are_listed(self):
        proc = self._run({"GITOPS_ORG": "sackheads", "GITOPS_REPO": "ka-test-infra"})
        self.assertIn("rc=1", proc.stdout)
        self.assertNotIn("GITOPS_ORG=", proc.stdout)
        self.assertNotIn("GITOPS_REPO=", proc.stdout)
        self.assertIn("INFO:   GITHUB_APP_ID=5319154\n", proc.stdout)

    def test_an_unreadable_app_id_says_where_to_find_it(self):
        proc = self._run({}, app_id_b64="")
        self.assertIn("rc=1", proc.stdout)
        self.assertIn('GITHUB_APP_ID=<the number under "App ID" on the GitHub App\'s General settings page>', proc.stdout)

    def test_a_plan_warns_and_goes_on(self):
        proc = self._run({}, mode="warn")
        self.assertIn("rc=0", proc.stdout)
        self.assertIn("WARN: An applying run would be refused:", proc.stdout)
        self.assertNotIn("ERROR:", proc.stdout)

    def test_the_cases_with_nothing_to_protect_pass_silently(self):
        for label, keys, git_repo, ctx in (
            ("the minter stays on", {"TFVARS_ENABLE_GITHUB_MINTER": "true"}, "sackheads/ka-test-infra", True),
            ("no live GitOps", {}, "", True),
            ("gitlab", {"GITOPS_FORGE": "gitlab"}, "sackheads/ka-test-infra", True),
            ("removed on purpose", {"KUBE_AGENTS_REMOVE_GITOPS": "true"}, "sackheads/ka-test-infra", True),
            ("no context", {}, "sackheads/ka-test-infra", False),
        ):
            with self.subTest(label):
                proc = self._run(keys, git_repo=git_repo, context_present=ctx)
                self.assertIn("rc=0", proc.stdout, proc.stdout + proc.stderr)
                self.assertNotIn("ERROR", proc.stdout)
                self.assertNotIn("WARN", proc.stdout)

    def test_every_applying_front_door_calls_it(self):
        install = _INSTALL_SH.read_text()
        # main() refuses on an applying run and warns otherwise, and the menu refuses.
        self.assertEqual(install.count('refuse_apply_dropping_live_gitops "${NAMESPACE:-$DEFAULT_NAMESPACE}" "$INSTALL_ENV_FILE" || exit 1'), 2)
        self.assertEqual(install.count('"$INSTALL_ENV_FILE" "$SCOPE_CHECK_MODE_WARN"'), 1)
        upgrade = _UPGRADE_SH.read_text()
        self.assertIn('refuse_apply_dropping_live_gitops "$target_namespace" "${install_env_file:-install.env}" || exit 1', upgrade)
        self.assertIn('refuse_apply_dropping_live_gitops "$target_namespace" "${install_env_file:-install.env}" "$SCOPE_CHECK_MODE_WARN"', upgrade)
        # In the full arm, before its apply starts.
        start = upgrade.index("\n    full)\n", upgrade.index('case "$PARAM_UPGRADE_MODE" in\n    operator)'))
        arm = upgrade[start:upgrade.index("\n      ;;\n", start + 10)]
        self.assertLess(arm.index('refuse_apply_dropping_live_gitops "$target_namespace" "${install_env_file:-install.env}" || exit 1'),
                        arm.index('UPGRADE_APPLY_STARTED="true"'))


class ValidateGithubAppIdTest(unittest.TestCase):
    def _run(self, value, source="--github-app-id"):
        body = (
            'print_error() { echo "ERROR: $*"; }; print_info() { echo "INFO: $*"; }\n'
            f'source "{_INSTALLER_COMMON}"\n'
            f'validate_github_app_id "{value}" "{source}"; echo "rc=$?"\n'
        )
        return subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                              env=get_isolated_test_env(), cwd=str(_REPO_ROOT))

    def test_a_number_or_nothing_passes(self):
        for value in ("5319154", ""):
            with self.subTest(value=value):
                proc = self._run(value)
                self.assertIn("rc=0", proc.stdout)
                self.assertNotIn("ERROR", proc.stdout)

    def test_a_name_or_slug_is_refused_with_where_to_look(self):
        for value in ("ka-test-gitops-1", "My App", "123abc", " 123"):
            with self.subTest(value=value):
                proc = self._run(value)
                self.assertIn("rc=1", proc.stdout)
                self.assertIn(f"ERROR: --github-app-id='{value}' is not a GitHub App ID", proc.stdout)
                self.assertIn('General, "App ID"', proc.stdout)

    def test_both_front_doors_check_it_before_the_preview(self):
        install = _INSTALL_SH.read_text()
        self.assertLess(install.index("validate_github_app_id \"${PARAM_GITHUB_APP_ID:-}\""),
                        install.index("check_flags_against_install_env || exit 1"))
        self.assertIn('validate_github_app_id "$github_app_id" "GitHub App ID" || exit 1', install)
        upgrade = _UPGRADE_SH.read_text()
        self.assertLess(upgrade.index('validate_github_app_id "${GITHUB_APP_ID:-}"'),
                        upgrade.index('print_step "2. Dry-Run Upgrade Plan Preview"'))


if __name__ == "__main__":
    unittest.main()
