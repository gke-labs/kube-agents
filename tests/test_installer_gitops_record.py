"""#3075 and #3076: GitOps configured with install.sh's flags survives the next
upgrade, and a GitHub App ID that is not the App's number is refused up front.

- record_github_gitops_keys (install.sh) appends the GitHub GitOps keys an
  install.env lacks when the run commits to the apply.
- refuse_apply_dropping_live_gitops (installer_common.sh) refuses an apply
  that would render GitOps off over a live PlatformAgent that uses it -- the
  apply that destroyed the token minter -- and prints the exact lines to add.
- validate_github_app_id (installer_common.sh) refuses a name or slug.
"""

import json
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

    def test_a_value_this_run_changes_replaces_the_recorded_one_and_nothing_else(self):
        # A re-run that points GitOps at a new repository applies it, so the
        # file must follow or the next upgrade puts the old one back.
        dest = self._tmp / "install.env"
        dest.write_text("# my settings\nGITOPS_ORG=acme\nGITOPS_REPO=old-infra\nMEMORY=file\n")
        proc = self._bash(f'export GITOPS_ORG=acme GITOPS_REPO=new-infra GITHUB_APP_ID=7\n'
                          f'record_github_gitops_keys "{dest}"\n')
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertEqual(dest.read_text(),
                         "# my settings\nGITOPS_ORG=acme\nMEMORY=file\nGITOPS_REPO=new-infra\nGITHUB_APP_ID=7\n")
        self.assertIn("Updated GITOPS_REPO=new-infra", proc.stdout)
        self.assertEqual(oct(dest.stat().st_mode & 0o777), "0o600")

    def test_a_line_assigning_two_keys_does_not_lose_the_second(self):
        dest = self._tmp / "install.env"
        dest.write_text("export GITOPS_ORG=acme GITOPS_REPO=infra\nGITHUB_APP_ID=7\n")
        proc = self._bash(f'export GITOPS_ORG=newco GITOPS_REPO=infra GITHUB_APP_ID=7\nrecord_github_gitops_keys "{dest}"\n')
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        proc = self._bash("unset GITOPS_ORG GITOPS_REPO GITHUB_APP_ID\n"
                          f'load_install_env "{dest}"; echo "org=$GITOPS_ORG repo=$GITOPS_REPO app=$GITHUB_APP_ID"\n')
        self.assertIn("org=newco repo=infra app=7", proc.stdout, proc.stderr + dest.read_text())

    def test_an_unchanged_file_with_no_final_newline_is_left_as_it_is(self):
        dest = self._tmp / "install.env"
        original = "GITOPS_ORG=acme\nGITOPS_REPO=infra\nGITHUB_APP_ID=7"
        dest.write_text(original)
        proc = self._bash(f'export GITOPS_ORG=acme GITOPS_REPO=infra GITHUB_APP_ID=7\nrecord_github_gitops_keys "{dest}"\n')
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertEqual(dest.read_text(), original)

    def test_an_unchanged_file_is_left_byte_for_byte(self):
        dest = self._tmp / "install.env"
        original = "GITOPS_ORG=acme\nGITOPS_REPO=infra\nGITHUB_APP_ID=7\n"
        dest.write_text(original)
        proc = self._bash(f'export GITOPS_ORG=acme GITOPS_REPO=infra GITHUB_APP_ID=7\nrecord_github_gitops_keys "{dest}"\n')
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertEqual(dest.read_text(), original)
        self.assertNotIn("Recorded", proc.stdout + proc.stderr)

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

    def test_the_interview_drift_warning_leaves_out_the_keys_the_run_records(self):
        dest = self._tmp / "install.env"
        dest.write_text("GITOPS_ORG=acme\nGITOPS_REPO=old-infra\nGITHUB_APP_ID=7\nMODEL_PROVIDER=gemini\n")
        # An interactive run (the warning names answers typed at the interview).
        proc = self._bash(f'has_controlling_tty() {{ return 0; }}\nPARAM_NON_INTERACTIVE=false\n'
                          f'export GITOPS_ORG=acme GITOPS_REPO=new-infra GITHUB_APP_ID=7 MODEL_PROVIDER=vertex_ai\n'
                          f'warn_unrecorded_interview_answers "{dest}"\n')
        out = proc.stdout + proc.stderr
        self.assertIn("MODEL_PROVIDER=vertex_ai", out)
        self.assertNotIn("GITOPS_REPO=", out)
        self.assertIn('scripts/installer/README.md ("install.env")', out)

    def test_it_runs_where_the_run_commits_and_nowhere_earlier(self):
        # The --generate-only handoff (its lifecycle.sh apply renders the keys)
        # and the apply; never before the step-11 decision.
        source = _INSTALL_SH.read_text()
        self.assertEqual(source.count('record_github_gitops_keys "$INSTALL_ENV_FILE"'), 2)
        handoff = source.index('    record_github_gitops_keys "$INSTALL_ENV_FILE"\n    record_flags_into_install_env\n    print_generate_only_handoff')
        self.assertLess(handoff, source.index('write_json_report "GENERATE_ONLY_SUCCESS"'))
        apply = source.index('  record_github_gitops_keys "$INSTALL_ENV_FILE"\n  record_flags_into_install_env\n  run_lifecycle_apply')
        self.assertLess(source.index('write_json_report "PAUSED"'), apply)


class RefuseApplyDroppingLiveGitopsTest(unittest.TestCase):
    def _run(self, keys, git_repo="sackheads/ka-test-infra", org="sackheads", app_id_b64="NTMxOTE1NA==",
             mode="", context_present=True, minter=True, integration=None, minter_read_fails=False):
        if integration is None:
            integration = {"github": {"gitRepo": git_repo, "org": org}} if git_repo else {}
        cr = json.dumps({"items": [{"spec": {"integration": integration}}]})
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            (bin_dir / "cr.json").write_text(cr)
            kubectl = bin_dir / "kubectl"
            if minter_read_fails:
                deploy_case = "exit 1"
            elif minter:
                deploy_case = "echo deployment.apps/github-token-minter; exit 0"
            else:
                deploy_case = "exit 0"  # --ignore-not-found: absent is empty output, exit 0
            kubectl.write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                f'  *"config get-contexts"*) exit {0 if context_present else 1} ;;\n'
                f'  *"get deployment github-token-minter"*) {deploy_case} ;;\n'
                f'  *"get platformagents"*) cat "{bin_dir}/cr.json" ;;\n'
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
        self.assertIn("INFO: Set these lines in /x/install.env (add them, or replace the ones it has), then re-run the same command:", out)
        for line in ("INFO:   GITOPS_ORG=sackheads", "INFO:   GITOPS_REPO=ka-test-infra", "INFO:   GITHUB_APP_ID=5319154"):
            self.assertIn(line + "\n", out)

    def test_only_the_missing_keys_are_listed(self):
        proc = self._run({"GITOPS_ORG": "sackheads", "GITOPS_REPO": "ka-test-infra"})
        self.assertIn("rc=1", proc.stdout)
        self.assertNotIn("GITOPS_ORG=", proc.stdout)
        self.assertNotIn("GITOPS_REPO=", proc.stdout)
        self.assertIn("INFO:   GITHUB_APP_ID=5319154\n", proc.stdout)

    def test_a_default_the_run_fell_back_to_is_corrected_to_the_live_value(self):
        # install.sh with no GitOps keys auto-selects an organization and the
        # default repository name; neither is the live one.
        proc = self._run({"GITOPS_ORG": "someorg", "GITOPS_REPO": "gke-fleet-iac"})
        self.assertIn("rc=1", proc.stdout)
        for line in ("INFO:   GITOPS_ORG=sackheads", "INFO:   GITOPS_REPO=ka-test-infra", "INFO:   GITHUB_APP_ID=5319154"):
            self.assertIn(line + "\n", proc.stdout)

    def test_an_unreadable_app_id_says_where_to_find_it(self):
        proc = self._run({}, app_id_b64="")
        self.assertIn("rc=1", proc.stdout)
        self.assertIn('GITHUB_APP_ID=<the number under "App ID" on the GitHub App\'s General settings page>', proc.stdout)

    def test_a_repository_declared_through_forges_and_repositories_is_protected_too(self):
        # The chart renders the github alias only for a plain single repo; the
        # current schema leaves spec.integration.github empty.
        integration = {"forges": [{"name": "gh", "provider": "github", "namespace": "acme"}],
                       "repositories": [{"forge": "gh", "repository": "infra", "role": "gitops"},
                                        {"forge": "gh", "repository": "docs", "role": "context"}]}
        proc = self._run({}, integration=integration)
        self.assertIn("rc=1", proc.stdout, proc.stdout + proc.stderr)
        for line in ("INFO:   GITOPS_ORG=acme", "INFO:   GITOPS_REPO=infra", "INFO:   GITHUB_APP_ID=5319154"):
            self.assertIn(line + "\n", proc.stdout)

    def test_a_running_minter_with_no_readable_repository_is_still_refused(self):
        proc = self._run({}, git_repo="")
        self.assertIn("rc=1", proc.stdout, proc.stdout + proc.stderr)
        self.assertIn("INFO:   GITOPS_ORG=<the GitHub organization that owns the GitOps repository>", proc.stdout)
        self.assertIn("INFO:   GITOPS_REPO=<the GitOps repository's name>", proc.stdout)

    def test_repository_references_are_read_as_the_operator_reads_them(self):
        for ref, org_line, repo_line in (
            ("git@github.com:acme/infra", "GITOPS_ORG=acme", "GITOPS_REPO=infra"),
            ("https://github.com/acme/infra/", "GITOPS_ORG=acme", "GITOPS_REPO=infra"),
            ("https://github.com/acme/infra.git", "GITOPS_ORG=acme", "GITOPS_REPO=infra"),
            ("/acme/infra/", "GITOPS_ORG=acme", "GITOPS_REPO=infra"),
        ):
            with self.subTest(ref=ref):
                proc = self._run({}, integration={"github": {"gitRepo": ref}})
                self.assertIn("rc=1", proc.stdout, proc.stdout + proc.stderr)
                self.assertIn(f"INFO:   {org_line}\n", proc.stdout)
                self.assertIn(f"INFO:   {repo_line}\n", proc.stdout)

    def test_the_none_sentinel_with_a_running_minter_prints_placeholders_not_none(self):
        proc = self._run({}, integration={"github": {"gitRepo": "None"}})
        self.assertIn("rc=1", proc.stdout)
        self.assertNotIn("=None", proc.stdout)
        self.assertIn("INFO:   GITOPS_REPO=<the GitOps repository's name>", proc.stdout)

    def test_correct_keys_with_the_minter_rendered_off_name_the_kms_check(self):
        # The generator defers the minter when it cannot see an ENABLED key
        # version; the keys are right, so no lines and no removal offer.
        proc = self._run({"GITOPS_ORG": "sackheads", "GITOPS_REPO": "ka-test-infra", "GITHUB_APP_ID": "5319154",
                          "KMS_KEYRING": "kr", "KMS_KEY": "k"})
        self.assertIn("rc=1", proc.stdout, proc.stdout + proc.stderr)
        self.assertIn("found no ENABLED version of its Cloud KMS key", proc.stdout)
        self.assertIn("cloudkms.cryptoKeyVersions.list on kr/k", proc.stdout)
        self.assertNotIn("Set these lines", proc.stdout)
        self.assertNotIn("KUBE_AGENTS_REMOVE_GITOPS", proc.stdout)

    def test_an_unreadable_minter_deployment_is_refused_not_read_as_absent(self):
        proc = self._run({}, minter_read_fails=True)
        self.assertIn("rc=1", proc.stdout, proc.stdout + proc.stderr)
        self.assertIn("could not be read, so this run cannot tell whether the apply would destroy", proc.stdout)
        proc = self._run({}, minter_read_fails=True, mode="warn")
        self.assertIn("rc=0", proc.stdout)
        self.assertIn("WARN: The GitOps check did not run: the github-token-minter Deployment", proc.stdout)

    def test_a_path_with_an_owner_names_its_own_not_the_forge_namespace(self):
        integration = {"forges": [{"name": "gh", "provider": "github", "namespace": "other"}],
                       "repositories": [{"forge": "gh", "repository": "acme/infra", "role": "gitops"}]}
        proc = self._run({}, integration=integration)
        self.assertIn("INFO:   GITOPS_ORG=acme\n", proc.stdout, proc.stdout + proc.stderr)
        self.assertIn("INFO:   GITOPS_REPO=infra\n", proc.stdout)

    def test_a_value_the_operator_would_refuse_is_not_printed_as_a_line_to_paste(self):
        proc = self._run({}, integration={"github": {"gitRepo": "acme/in fra"}})
        self.assertIn("rc=1", proc.stdout)
        self.assertNotIn("in fra", proc.stdout.split("Set these lines", 1)[-1])
        self.assertIn("INFO:   GITOPS_REPO=<the GitOps repository's name>", proc.stdout)

    def test_no_context_refuses_outside_a_preview(self):
        # --generate-only reaches the guard with only the generator's
        # best-effort context fetch behind it, so a missing context refuses.
        proc = self._run({}, context_present=False)
        self.assertIn("rc=1", proc.stdout, proc.stdout + proc.stderr)
        self.assertIn("ERROR: Refusing to apply: the kubeconfig has no context", proc.stdout)
        self.assertIn("gcloud container clusters get-credentials c --location r --project p", proc.stdout)
        proc = self._run({}, mode="warn", context_present=False)
        self.assertIn("rc=0", proc.stdout)
        self.assertIn("WARN: The GitOps check did not run: the kubeconfig has no context", proc.stdout)

    def test_a_run_that_keeps_the_minter_but_points_it_elsewhere_is_named_before_the_apply(self):
        proc = self._run({"TFVARS_ENABLE_GITHUB_MINTER": "true", "GITOPS_ORG": "firstorg", "GITOPS_REPO": "gke-fleet-iac"})
        self.assertIn("rc=0", proc.stdout, proc.stdout + proc.stderr)
        self.assertIn("WARN: This run points the GitHub token minter at firstorg/gke-fleet-iac, but it serves sackheads/ka-test-infra now.", proc.stdout)
        self.assertIn("set GITOPS_ORG=sackheads and GITOPS_REPO=ka-test-infra", proc.stdout)
        self.assertNotIn("ERROR", proc.stdout)

    def test_a_plan_warns_and_goes_on(self):
        proc = self._run({}, mode="warn")
        self.assertIn("rc=0", proc.stdout)
        self.assertIn("WARN: An applying run would be refused:", proc.stdout)
        self.assertNotIn("ERROR:", proc.stdout)

    def test_the_cases_with_nothing_to_protect_pass_silently(self):
        for label, keys, git_repo, ctx, minter in (
            ("the minter stays on, same repository", {"TFVARS_ENABLE_GITHUB_MINTER": "true", "GITOPS_ORG": "sackheads",
                                                      "GITOPS_REPO": "ka-test-infra"}, "sackheads/ka-test-infra", True, True),
            ("the minter stays on, no context", {"TFVARS_ENABLE_GITHUB_MINTER": "true"}, "sackheads/ka-test-infra", False, True),
            ("no live GitOps", {}, "", True, False),
            ("gitlab", {"GITOPS_FORGE": "gitlab"}, "sackheads/ka-test-infra", True, True),
            ("removed on purpose", {"KUBE_AGENTS_REMOVE_GITOPS": "true"}, "sackheads/ka-test-infra", True, True),
            # The App ID left blank, or the minter deferred: a repository and
            # no minter render the same either way.
            ("a repository with no minter", {}, "sackheads/ka-test-infra", True, False),
            ("the 'None' sentinel and no minter", {}, "None", True, False),
        ):
            with self.subTest(label):
                proc = self._run(keys, git_repo=git_repo, context_present=ctx, minter=minter)
                self.assertIn("rc=0", proc.stdout, proc.stdout + proc.stderr)
                self.assertNotIn("ERROR", proc.stdout)
                self.assertNotIn("WARN", proc.stdout)

    def test_every_applying_front_door_calls_it(self):
        install = _INSTALL_SH.read_text()
        # main() refuses on an applying run and warns otherwise, and the menu refuses.
        # The menu refuses outright; main() refuses (and removes the tfvars)
        # for an applying run and for --generate-only, and warns on --dry-run.
        self.assertEqual(install.count('refuse_apply_dropping_live_gitops "${NAMESPACE:-$DEFAULT_NAMESPACE}" "$INSTALL_ENV_FILE" || exit 1'), 1)
        self.assertIn('if ! refuse_apply_dropping_live_gitops "${NAMESPACE:-$DEFAULT_NAMESPACE}" "$INSTALL_ENV_FILE"; then\n'
                      '        # The tfvars just written render GitOps off; --generate-only would\n'
                      '        # hand them to lifecycle.sh apply, so they go.\n'
                      '        rm -f "$tfvars_file"', install)
        self.assertEqual(install.count('"$INSTALL_ENV_FILE" "$SCOPE_CHECK_MODE_WARN"'), 1)
        upgrade = _UPGRADE_SH.read_text()
        self.assertIn('if ! refuse_apply_dropping_live_gitops "$target_namespace" "${install_env_file:-install.env}"; then', upgrade)
        self.assertIn('refuse_apply_dropping_live_gitops "$target_namespace" "${install_env_file:-install.env}" "$SCOPE_CHECK_MODE_WARN"', upgrade)
        # In the full arm, before its apply starts.
        start = upgrade.index("\n    full)\n", upgrade.index('case "$PARAM_UPGRADE_MODE" in\n    operator)'))
        arm = upgrade[start:upgrade.index("\n      ;;\n", start + 10)]
        self.assertLess(arm.index('if ! refuse_apply_dropping_live_gitops "$target_namespace" "${install_env_file:-install.env}"; then'),
                        arm.index('UPGRADE_APPLY_STARTED="true"'))


class UpgradeRefusalRemovesTheTfvarsTest(unittest.TestCase):
    """A full upgrade the guard refuses removes the tfvars the generator wrote
    (they render the minter off), as install.sh and the Slack check do."""

    def test_the_refused_full_arm_leaves_no_tfvars(self):
        source = _UPGRADE_SH.read_text()
        start = source.index("\n    full)\n", source.index('case "$PARAM_UPGRADE_MODE" in\n    operator)'))
        body_start = start + len("\n    full)\n")
        arm = source[body_start:source.index("\n      ;;\n", body_start)]
        stubs = (
            "print_step() { :; }\nrefuse_apply_over_undeclared_scope() { :; }\n"
            "announce_platform_agent_mode_for_apply() { :; }\ncheck_scope_container_access() { :; }\n"
            "check_service_account_ownership() { :; }\nrefuse_full_apply_dropping_next() { :; }\n"
            "refuse_next_slack_gateway_settings_from_env() { :; }\n"
            'refuse_apply_dropping_live_gitops() { echo "REFUSED gitops"; return 1; }\n'
            'apply_crd_upgrades() { echo "APPLIED crds"; }\nrun_lifecycle() { echo "APPLIED terraform"; }\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            tfvars = pathlib.Path(tmp) / "terraform.tfvars"
            tfvars.write_text("enable_github_minter = false\n")
            script = (
                f'KUBE_AGENTS_SOURCE_ONLY=true source "{_UPGRADE_SH}"\n'
                f'source "{_INSTALLER_COMMON}"\n'
                f"{stubs}"
                f'repo_dir="{_REPO_ROOT}"; tfvars_file="{tfvars}"; target_namespace=kube-agents; install_env_file=/x/install.env\n'
                f"_full() {{\n{arm}\n}}\n_full\n"
            )
            proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                  env=get_isolated_test_env(), cwd=str(_REPO_ROOT))
            left = tfvars.exists()
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, out)
        self.assertIn("REFUSED gitops", out)
        self.assertNotIn("APPLIED", out)
        self.assertFalse(left, "the tfvars that render the minter off were left for a hand-run apply")


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
                self.assertIn(f"ERROR: --github-app-id: '{value}' is not a GitHub App ID", proc.stdout)
                self.assertIn('General, "App ID"', proc.stdout)

    def test_both_front_doors_check_it_before_the_preview(self):
        install = _INSTALL_SH.read_text()
        self.assertLess(install.index("validate_github_app_id \"${PARAM_GITHUB_APP_ID:-}\""),
                        install.index("check_flags_against_install_env || exit 1"))
        # The label names the flag only when --github-app-id itself was passed
        # (--github-pem-path sets the shared GitHub-App flag too).
        self.assertIn('PARAM_GITHUB_APP_ID_FROM_FLAG="true"; shift ;;', install)
        self.assertIn('"$([ "${PARAM_GITHUB_APP_ID_FROM_FLAG:-false}" = "true" ] && echo "--github-app-id" || echo "GITHUB_APP_ID in ${INSTALL_ENV_FILE:-install.env}")"', install)
        # The interview asks again at the prompt, before the KMS questions.
        ask = install.index('while [ -n "$github_app_id" ] && ! validate_github_app_id "$github_app_id" "GitHub App ID"; do')
        self.assertLess(ask, install.index('prompt_read "Cloud KMS Keyring Name" kms_keyring', ask - 2000))
        # The Day-2 menu, which main() does not reach, checks before it saves.
        menu = install.index('validate_github_app_id "$github_app_id" "GITHUB_APP_ID in ${INSTALL_ENV_FILE}" || return 1')
        self.assertLess(menu, install.index('save_env_var GITHUB_APP_ID "$github_app_id"'))
        self.assertLess(install.index("\nrun_menu_system() {"), menu)
        upgrade = _UPGRADE_SH.read_text()
        # GitHub only: a GitLab install does not read the key.
        self.assertIn('if declare -F validate_github_app_id >/dev/null && [ "${GITOPS_FORGE:-github}" = "github" ]; then', upgrade)
        self.assertLess(upgrade.index('validate_github_app_id "${GITHUB_APP_ID:-}"'),
                        upgrade.index('print_step "2. Dry-Run Upgrade Plan Preview"'))


if __name__ == "__main__":
    unittest.main()
