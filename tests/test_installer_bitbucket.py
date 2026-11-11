"""Bitbucket Cloud and Bitbucket Data Center as the installer's GitOps forge.

A Bitbucket install shares GitLab's token path: the token comes from a file or
a no-echo prompt and goes only into a Kubernetes Secret. These tests cover what
is Bitbucket's own: the neutral flags (--gitops-token-file,
--gitops-token-secret, --gitops-identity), the repository grammars
(workspace/slug on Cloud, KEY/slug on Data Center), the Data Center host rule
(the root path on port 443 only), the tfvars a Bitbucket install renders, and
what install.env records. See docs/designs/version-control-support.md §8.
"""

import pathlib
import shlex
import subprocess
import tempfile
import unittest

from tests.test_install_script import _INSTALL_SH, _INSTALLER_COMMON, _REPO_ROOT, _run_installer_bash
from tests import test_installer_common as _installer_common
from tests.testing.common import get_isolated_test_env

_TOKEN_SOURCE_VARS = (
    "GITLAB_READ_TOKEN_FILE", "GITLAB_TOKEN_FILE", "GITOPS_CA_FILE",
    "GITOPS_TOKEN_FILE", "GITOPS_READ_TOKEN_FILE", "GITOPS_TOKEN_SECRET", "GITOPS_IDENTITY",
)
_UUID = "{0b3c1f2e-5d6a-4b7c-8d9e-0f1a2b3c4d5e}"


class BitbucketTfvarsTest(unittest.TestCase):
    _run = _installer_common.InstallerCommonTest._run

    def _tfvars(self, env):
        with tempfile.TemporaryDirectory() as out_dir:
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                f'write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k", **env},
                describe_stub=_installer_common._autopilot_describe_stub(),
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            return dest.read_text()

    def test_cloud_renders_the_neutral_variables_and_no_minter(self):
        content = self._tfvars({
            "GITOPS_FORGE": "bitbucket",
            "GITOPS_REPO": "acme/infra",
            "GITOPS_IDENTITY": _UUID,
            # Left over from a GitHub install: must not arm the minter.
            "GITOPS_ORG": "acme",
            "GITHUB_APP_ID": "123",
        })
        self.assertIn('gitops_forge              = "bitbucket"', content)
        self.assertIn('gitops_repo_path          = "acme/infra"', content)
        self.assertIn('gitops_token_secret_name  = "bitbucket-forge-token"', content)
        self.assertIn(f'gitops_identity           = "{_UUID}"', content)
        self.assertNotIn("gitlab_", content)
        self.assertNotIn("github_repo", content)
        self.assertNotIn("gitops_ca_secret_name", content)
        self.assertRegex(content, r"enable_github_minter\s*=\s*false")

    def test_datacenter_renders_its_host_and_ca(self):
        content = self._tfvars({
            "GITOPS_FORGE": "bitbucket-datacenter",
            "GITOPS_HOST": "bitbucket.example.com",
            "GITOPS_REPO": "INFRA/gitops",
            "GITLAB_TOKEN_SECRET": "bbdc-token",
            "GITOPS_CA_SECRET": "bitbucket-forge-ca",
        })
        self.assertIn('gitops_forge              = "bitbucket-datacenter"', content)
        self.assertIn('gitops_host               = "bitbucket.example.com"', content)
        self.assertIn('gitops_repo_path          = "INFRA/gitops"', content)
        self.assertIn('gitops_token_secret_name  = "bbdc-token"', content)
        self.assertIn('gitops_ca_secret_name     = "bitbucket-forge-ca"', content)
        self.assertNotIn("gitops_identity", content)


class HclStringTest(unittest.TestCase):
    _run = _installer_common.InstallerCommonTest._run

    def test_template_sequences_are_escaped(self):
        """HCL reads `${` and `%{` in a quoted string as a template: a value
        that holds one would be evaluated by Terraform, not written."""
        proc = self._run("hcl_str 'a${b}c%{d}e\\f\"g'; echo")
        self.assertIn('"a$${b}c%%{d}e\\\\f\\"g"', proc.stdout, proc.stderr)


class BitbucketFlagsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._tmp = pathlib.Path(tmp.name)
        self._empty_install_env = self._tmp / "install.env"
        self._empty_install_env.write_text("")

    def _run_install_func(self, func_call, env=None):
        setup = f'source "{_INSTALLER_COMMON}"\nKUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n{func_call}\n'
        overrides = {"KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env)}
        overrides.update(env or {})
        return _run_installer_bash(setup, get_isolated_test_env(overrides=overrides, absent=_TOKEN_SOURCE_VARS))

    def test_parse_args_takes_the_neutral_flags(self):
        proc = self._run_install_func(
            "parse_args --gitops-forge=bitbucket --gitops-token-file=/x/tok --gitops-token-secret=s1 "
            f"--gitops-identity='{_UUID}'; "
            'echo "f=$PARAM_GITOPS_FORGE t=$PARAM_GITLAB_TOKEN_FILE s=$PARAM_GITLAB_TOKEN_SECRET i=$PARAM_GITOPS_IDENTITY"'
        )
        self.assertIn(f"f=bitbucket t=/x/tok s=s1 i={_UUID}", proc.stdout, proc.stderr)

    def test_the_gitlab_flags_stay_aliases(self):
        proc = self._run_install_func(
            "parse_args --gitops-forge=gitlab --gitlab-token-file=/x/tok --gitlab-token-secret=s1; "
            'echo "t=$PARAM_GITLAB_TOKEN_FILE s=$PARAM_GITLAB_TOKEN_SECRET"'
        )
        self.assertIn("t=/x/tok s=s1", proc.stdout, proc.stderr)

    def test_the_neutral_variables_are_read_from_the_environment(self):
        proc = self._run_install_func(
            'echo "t=$PARAM_GITLAB_TOKEN_FILE r=$PARAM_GITLAB_READ_TOKEN_FILE s=$PARAM_GITLAB_TOKEN_SECRET"',
            env={"GITOPS_TOKEN_FILE": "/a", "GITOPS_READ_TOKEN_FILE": "/b", "GITOPS_TOKEN_SECRET": "c"},
        )
        self.assertIn("t=/a r=/b s=c", proc.stdout, proc.stderr)

    def _validate(self, assignments):
        body = "".join(f"{k}={shlex.quote(v)}; " for k, v in assignments.items())
        return self._run_install_func(
            f'{body}validate_gitops_forge_flags && rc=0 || rc=$?; '
            'echo "rc=$rc repo=$PARAM_GITOPS_REPO host=$PARAM_GITOPS_HOST secret=$PARAM_GITLAB_TOKEN_SECRET ca=$PARAM_GITOPS_CA_SECRET"'
        )

    @staticmethod
    def _cloud(**extra):
        base = {"PARAM_GITOPS_FORGE": "bitbucket", "PARAM_GITOPS_REPO": "acme/infra",
                "PARAM_GITHUB_APP_ID": "", "PARAM_GITHUB_PEM_PATH": "", "PARAM_NON_INTERACTIVE": "true"}
        base.update(extra)
        return base

    @staticmethod
    def _dc(**extra):
        base = {"PARAM_GITOPS_FORGE": "bitbucket-datacenter", "PARAM_GITOPS_HOST": "bitbucket.example.com",
                "PARAM_GITOPS_REPO": "INFRA/gitops", "PARAM_GITHUB_APP_ID": "", "PARAM_GITHUB_PEM_PATH": "",
                "PARAM_NON_INTERACTIVE": "true"}
        base.update(extra)
        return base

    def test_validator_accepts(self):
        for case, want in (
            (self._cloud(), "repo=acme/infra host= secret=bitbucket-forge-token"),
            (self._cloud(PARAM_GITOPS_REPO="https://bitbucket.org/acme/infra.git"), "repo=acme/infra "),
            (self._cloud(PARAM_GITOPS_HOST="bitbucket.org"), "repo=acme/infra host= "),
            (self._cloud(PARAM_GITOPS_IDENTITY=_UUID), "repo=acme/infra "),
            (self._cloud(PARAM_GITOPS_IDENTITY=_UUID.strip("{}")), "repo=acme/infra "),
            (self._dc(), "repo=INFRA/gitops host=bitbucket.example.com secret=bitbucket-forge-token"),
            (self._dc(PARAM_GITOPS_REPO="https://bitbucket.example.com/scm/INFRA/gitops.git"), "repo=INFRA/gitops "),
            (self._dc(PARAM_GITOPS_REPO="https://bitbucket.example.com/projects/INFRA/repos/gitops/browse"),
             "repo=INFRA/gitops "),
            (self._dc(PARAM_GITOPS_IDENTITY="svc-bot"), "repo=INFRA/gitops "),
            # The clone dialog's URL carries the user who copied it.
            (self._cloud(PARAM_GITOPS_REPO="https://alice@bitbucket.org/acme/infra.git"), "repo=acme/infra "),
            (self._dc(PARAM_GITOPS_REPO="https://alice@bitbucket.example.com/scm/INFRA/gitops.git"), "repo=INFRA/gitops "),
        ):
            with self.subTest(case=case):
                proc = self._validate(case)
                self.assertIn("rc=0 " + want, proc.stdout, proc.stderr + proc.stdout)

    def test_validator_refuses(self):
        for label, case, why in (
            ("cloud at another host", self._cloud(PARAM_GITOPS_HOST="bitbucket.example.com"), "bitbucket-datacenter"),
            ("cloud uppercase workspace", self._cloud(PARAM_GITOPS_REPO="Acme/infra"), "workspace/repo-slug"),
            ("cloud three segments", self._cloud(PARAM_GITOPS_REPO="acme/proj/infra"), "workspace/repo-slug"),
            ("cloud ssh remote", self._cloud(PARAM_GITOPS_REPO="git@bitbucket.org:acme/infra.git"), "workspace/repo-slug"),
            ("cloud identity not a UUID", self._cloud(PARAM_GITOPS_IDENTITY="svc-bot"), "account UUID"),
            ("cloud with no repo", self._cloud(PARAM_GITOPS_REPO=""), "needs --gitops-repo"),
            ("cloud with a GitHub App flag",
             self._cloud(PARAM_GITHUB_APP_ID="1", PARAM_GITHUB_APP_FROM_FLAG="true"), "token minter"),
            ("dc without a host", self._dc(PARAM_GITOPS_HOST=""), "needs --gitops-host"),
            ("dc with a port", self._dc(PARAM_GITOPS_HOST="bitbucket.example.com:7990"), "root path on port 443"),
            ("dc with a context path", self._dc(PARAM_GITOPS_HOST="example.com/bitbucket"), "root path on port 443"),
            ("dc at bitbucket.org", self._dc(PARAM_GITOPS_HOST="bitbucket.org"), "hosted service"),
            ("dc at an Atlassian domain", self._dc(PARAM_GITOPS_HOST="acme.atlassian.net"), "hosted service"),
            ("dc personal repository", self._dc(PARAM_GITOPS_REPO="~alice/gitops"), "personal repository"),
            ("dc key starting with a digit", self._dc(PARAM_GITOPS_REPO="1NFRA/gitops"), "PROJECTKEY/repo-slug"),
            ("dc identity with a space", self._dc(PARAM_GITOPS_IDENTITY="svc bot"), "--gitops-identity"),
            ("dc identity with a quote", self._dc(PARAM_GITOPS_IDENTITY='svc"bot'), "--gitops-identity"),
            ("dc identity with a dollar", self._dc(PARAM_GITOPS_IDENTITY="svc${x}"), "--gitops-identity"),
            ("dc identity too long", self._dc(PARAM_GITOPS_IDENTITY="a" * 256), "--gitops-identity"),
            ("gitlab host under atlassian",
             {"PARAM_GITOPS_FORGE": "gitlab", "PARAM_GITOPS_REPO": "g/p", "PARAM_GITOPS_HOST": "git.atlassian.net",
              "PARAM_GITOPS_HOST_FROM_FLAG": "true"}, "Bitbucket"),
            ("ghes host bitbucket.org",
             {"PARAM_GITOPS_FORGE": "github", "PARAM_GITOPS_REPO": "infra", "PARAM_GITOPS_ORG": "acme",
              "PARAM_GITOPS_HOST": "bitbucket.org", "PARAM_GITOPS_HOST_FROM_FLAG": "true"}, "Bitbucket"),
            ("identity on gitlab",
             {"PARAM_GITOPS_FORGE": "gitlab", "PARAM_GITOPS_REPO": "g/p", "PARAM_GITOPS_IDENTITY": "x"},
             "--gitops-identity applies only"),
        ):
            with self.subTest(case=label):
                proc = self._validate(case)
                out = proc.stdout + proc.stderr
                self.assertRegex(proc.stdout, r"rc=[1-9]", out)
                self.assertIn(why, out)

    def test_cloud_refuses_a_ca_flag_and_drops_a_recorded_ca(self):
        ca = self._tmp / "ca.pem"
        ca.write_text("x")
        proc = self._validate(self._cloud(PARAM_GITOPS_CA_FILE=str(ca), PARAM_GITOPS_CA_FILE_FROM_FLAG="true"))
        self.assertRegex(proc.stdout, r"rc=[1-9]", proc.stderr)
        self.assertIn("bitbucket.org uses the public CAs", proc.stdout + proc.stderr)
        proc = self._validate(self._cloud(PARAM_GITOPS_CA_SECRET="old-ca"))
        self.assertIn("rc=0 ", proc.stdout, proc.stderr)
        self.assertIn("ca=\n", proc.stdout + "\n")

    def test_a_dc_ca_file_names_the_bitbucket_secret(self):
        ca = self._tmp / "ca.pem"
        ca.write_text(
            "-----BEGIN CERTIFICATE-----\nMIIBszCCAVmgAwIBAgIUFakeFakeFakeFakeFakeFakeFakeFakewCgYIKoZIzj0EAwIw\n"
            "-----END CERTIFICATE-----\n"
        )
        proc = self._validate(self._dc(PARAM_GITOPS_CA_FILE=str(ca)))
        self.assertIn("rc=0 ", proc.stdout, proc.stderr + proc.stdout)
        self.assertIn("ca=bitbucket-forge-ca", proc.stdout)

    def test_a_forge_switch_refuses_to_keep_the_recorded_token(self):
        """The recorded Secret holds the other forge's token: kept, the broker
        would send an internal Data Center token to bitbucket.org, or the
        reverse. Both default to bitbucket-forge-token, so a name check alone
        does not see the switch."""
        for label, case in (
            ("gitlab to cloud", self._cloud(PARAM_GITOPS_RECORDED_FORGE="gitlab",
                                            PARAM_GITLAB_TOKEN_SECRET="gitlab-forge-token")),
            ("dc to cloud", self._cloud(PARAM_GITOPS_RECORDED_FORGE="bitbucket-datacenter",
                                        PARAM_GITLAB_TOKEN_SECRET="bitbucket-forge-token")),
            ("cloud to dc", self._dc(PARAM_GITOPS_RECORDED_FORGE="bitbucket",
                                     PARAM_GITLAB_TOKEN_SECRET="bitbucket-forge-token")),
            ("dc to cloud, custom name", self._cloud(PARAM_GITOPS_RECORDED_FORGE="bitbucket-datacenter",
                                                     PARAM_GITLAB_TOKEN_SECRET="corp-token")),
        ):
            with self.subTest(case=label):
                proc = self._validate(case)
                out = proc.stdout + proc.stderr
                self.assertRegex(proc.stdout, r"rc=[1-9]", out)
                self.assertIn("--gitops-token-file", out)
                self.assertIn("--gitops-token-secret", out)

    def test_a_forge_switch_with_a_named_token_or_secret_goes_on(self):
        tok = self._tmp / "tok"
        tok.write_text("x")
        proc = self._validate(self._cloud(
            PARAM_GITOPS_RECORDED_FORGE="bitbucket-datacenter", PARAM_GITLAB_TOKEN_SECRET="bitbucket-forge-token",
            PARAM_GITLAB_TOKEN_FILE=str(tok)))
        self.assertIn("rc=0 repo=acme/infra host= secret=bitbucket-forge-token", proc.stdout, proc.stderr)
        proc = self._validate(self._cloud(
            PARAM_GITOPS_RECORDED_FORGE="gitlab", PARAM_GITLAB_TOKEN_SECRET="bb-cloud",
            PARAM_GITLAB_TOKEN_SECRET_FROM_FLAG="true"))
        self.assertIn("rc=0 repo=acme/infra host= secret=bb-cloud", proc.stdout, proc.stderr)

    def test_an_interactive_forge_switch_asks_for_the_new_token(self):
        # The run has a terminal: the test stands one in.
        proc = self._run_install_func(
            "has_controlling_tty() { return 0; }; "
            "PARAM_GITOPS_FORGE=bitbucket PARAM_GITOPS_REPO=acme/infra PARAM_NON_INTERACTIVE=false "
            "PARAM_GITOPS_RECORDED_FORGE=bitbucket-datacenter PARAM_GITLAB_TOKEN_SECRET=bitbucket-forge-token; "
            'validate_gitops_forge_flags >/dev/null 2>&1; echo "replace=${PARAM_GITOPS_TOKEN_MUST_REPLACE:-}"'
        )
        self.assertIn("replace=true", proc.stdout, proc.stderr)

    def test_a_forge_switch_without_a_terminal_is_refused_like_non_interactive(self):
        """CI runs with no terminal and often without --non-interactive. The
        token prompt cannot run there, so the switch must stop at the check,
        before anything changes."""
        proc = self._run_install_func(
            "has_controlling_tty() { return 1; }; "
            "PARAM_GITOPS_FORGE=bitbucket PARAM_GITOPS_REPO=acme/infra PARAM_NON_INTERACTIVE=false "
            "PARAM_GITOPS_RECORDED_FORGE=bitbucket-datacenter PARAM_GITLAB_TOKEN_SECRET=bitbucket-forge-token; "
            'validate_gitops_forge_flags && rc=0 || rc=$?; echo "rc=$rc"'
        )
        self.assertRegex(proc.stdout, r"rc=[1-9]", proc.stderr + proc.stdout)
        self.assertIn("--gitops-token-file", proc.stdout + proc.stderr)

    # --- A switch that a stopped run left pending (install.env records the
    # new forge, and GITOPS_SWITCH_FROM_* the old one) ---
    def _pending(self, **extra):
        return self._cloud(PARAM_GITOPS_RECORDED_FORGE="bitbucket", PARAM_GITLAB_TOKEN_SECRET="bitbucket-forge-token",
                           PARAM_GITOPS_RECORDED_TOKEN_SECRET="bitbucket-forge-token",
                           PARAM_GITOPS_PENDING_FROM_FORGE="bitbucket-datacenter",
                           PARAM_GITOPS_PENDING_FROM_SECRET="bitbucket-forge-token", **extra)

    def test_a_rerun_after_a_stopped_switch_still_refuses_the_old_token(self):
        proc = self._validate(self._pending())
        out = proc.stdout + proc.stderr
        self.assertRegex(proc.stdout, r"rc=[1-9]", out)
        self.assertIn("--gitops-token-file", out)

    def test_a_rerun_after_a_stopped_switch_still_removes_the_old_read_token(self):
        tok = self._tmp / "tok"
        tok.write_text("x")
        body = "".join(f"{k}={shlex.quote(v)}; " for k, v in self._pending(PARAM_GITLAB_TOKEN_FILE=str(tok)).items())
        proc = self._run_install_func(f'{body}validate_gitops_forge_flags && rc=0 || rc=$?; echo "rc=$rc old=$PARAM_GITOPS_OLD_TOKEN_SECRET"')
        self.assertIn("rc=0 old=bitbucket-forge-token", proc.stdout, proc.stderr)

    def test_install_env_switch_keys_are_read_at_load(self):
        env = self._tmp / "pending.env"
        env.write_text("GITOPS_FORGE=bitbucket\nGITLAB_TOKEN_SECRET=bb\nGITOPS_SWITCH_FROM_FORGE=bitbucket-datacenter\n"
                       "GITOPS_SWITCH_FROM_SECRET=bb-old\nGITOPS_REPO=acme/infra\n")
        proc = self._run_install_func(
            'echo "pf=$PARAM_GITOPS_PENDING_FROM_FORGE ps=$PARAM_GITOPS_PENDING_FROM_SECRET '
            'rs=$PARAM_GITOPS_RECORDED_TOKEN_SECRET rr=$PARAM_GITOPS_RECORDED_REPO"',
            env={"KUBE_AGENTS_INSTALL_ENV": str(env)})
        self.assertIn("pf=bitbucket-datacenter ps=bb-old rs=bb rr=acme/infra", proc.stdout, proc.stderr)

    # --- The old Secret is the one install.env recorded, read once ---
    def test_a_second_validation_keeps_the_recorded_secret_as_the_old_one(self):
        """validate_gitops_forge_flags runs more than once, and the interview
        can change the Secret's name between the runs: the old Secret is still
        the recorded one, never the new name."""
        tok = self._tmp / "tok"
        tok.write_text("x")
        body = "".join(f"{k}={shlex.quote(v)}; " for k, v in self._cloud(
            PARAM_GITOPS_RECORDED_FORGE="gitlab", PARAM_GITLAB_TOKEN_SECRET="gitlab-forge-token",
            PARAM_GITOPS_RECORDED_TOKEN_SECRET="gitlab-forge-token", PARAM_GITLAB_TOKEN_FILE=str(tok)).items())
        proc = self._run_install_func(
            f"{body}validate_gitops_forge_flags >/dev/null; validate_gitops_forge_flags >/dev/null; "
            'echo "twice=$PARAM_GITOPS_OLD_TOKEN_SECRET secret=$PARAM_GITLAB_TOKEN_SECRET"; '
            "PARAM_GITLAB_TOKEN_SECRET=typed-at-the-prompt; validate_gitops_forge_flags >/dev/null; "
            'echo "prompt=$PARAM_GITOPS_OLD_TOKEN_SECRET"')
        self.assertIn("twice=gitlab-forge-token secret=bitbucket-forge-token", proc.stdout, proc.stderr)
        self.assertIn("prompt=gitlab-forge-token", proc.stdout, proc.stderr)

    # --- The Data Center host is asked for in an interactive run ---
    def test_an_interactive_dc_run_without_a_host_goes_on_to_the_interview(self):
        body = "".join(f"{k}={shlex.quote(v)}; " for k, v in self._dc(
            PARAM_GITOPS_HOST="", PARAM_NON_INTERACTIVE="false").items())
        proc = self._run_install_func(
            f'has_controlling_tty() {{ return 0; }}; {body}validate_gitops_forge_flags && rc=0 || rc=$?; echo "rc=$rc"')
        self.assertIn("rc=0", proc.stdout, proc.stderr + proc.stdout)
        proc = self._run_install_func(
            f'has_controlling_tty() {{ return 1; }}; {body}validate_gitops_forge_flags && rc=0 || rc=$?; echo "rc=$rc"')
        self.assertRegex(proc.stdout, r"rc=[1-9]", proc.stderr + proc.stdout)

    # --- A switch drops the recorded repository ---
    def test_a_switch_drops_the_recorded_repository(self):
        tok = self._tmp / "tok"
        tok.write_text("x")
        base = dict(PARAM_GITOPS_RECORDED_FORGE="gitlab", PARAM_GITOPS_RECORDED_REPO="acme/infra",
                    PARAM_GITOPS_REPO="acme/infra", PARAM_GITLAB_TOKEN_FILE=str(tok),
                    PARAM_GITLAB_TOKEN_SECRET="gitlab-forge-token", PARAM_GITOPS_RECORDED_TOKEN_SECRET="gitlab-forge-token")
        proc = self._validate(self._cloud(**base))
        out = proc.stdout + proc.stderr
        self.assertRegex(proc.stdout, r"rc=[1-9]", out)
        self.assertIn("Dropping the recorded GitLab repository 'acme/infra'", out)
        self.assertIn("needs --gitops-repo", out)
        proc = self._validate(self._cloud(PARAM_GITOPS_REPO_FROM_FLAG="true", **base))
        self.assertIn("rc=0 repo=acme/infra ", proc.stdout, proc.stderr)
        body = "".join(f"{k}={shlex.quote(v)}; " for k, v in self._cloud(PARAM_NON_INTERACTIVE="false", **base).items())
        proc = self._run_install_func(
            f'has_controlling_tty() {{ return 0; }}; {body}validate_gitops_forge_flags && rc=0 || rc=$?; echo "rc=$rc repo=[$PARAM_GITOPS_REPO]"')
        self.assertIn("rc=0 repo=[]", proc.stdout, proc.stderr + proc.stdout)

    def test_parse_args_marks_a_named_repository(self):
        proc = self._run_install_func('parse_args --gitops-repo=acme/infra; echo "flag=$PARAM_GITOPS_REPO_FROM_FLAG"')
        self.assertIn("flag=true", proc.stdout, proc.stderr)

    # --- Answers given in the interview are this run's choices ---
    _PROMPTS = (
        'prompt_read() { case "$1" in '
        '"Repository path"*|"GitLab project path"*) eval "$2=\\$ANSWER_REPO" ;; '
        '"GitLab host"*) eval "$2=gitlab.com" ;; '
        '"Bitbucket Data Center hostname"*) eval "$2=\\$ANSWER_HOST" ;; '
        '*) eval "$2=\\$3" ;; esac; }; has_controlling_tty() { return 0; }; '
    )

    def _interview(self, func, assignments, answers):
        body = "".join(f"{k}={shlex.quote(v)}; " for k, v in assignments.items())
        exports = "".join(f"export {k}={shlex.quote(v)}; " for k, v in answers.items())
        return self._run_install_func(
            f"{self._PROMPTS}{exports}{body}"
            "validate_gitops_forge_flags >/dev/null 2>&1; "
            f'run() {{ local github_repo="$PARAM_GITOPS_REPO"; {func}; echo "repo=[$github_repo] param=[$PARAM_GITOPS_REPO]"; }}; run'
        )

    def test_an_interview_answer_equal_to_the_recorded_path_is_kept(self):
        """A switch drops the recorded path, then the interview asks. An answer
        that equals the old path is this run's choice, not a recorded value."""
        proc = self._interview("interview_bitbucket_forge", {
            "PARAM_GITOPS_FORGE": "bitbucket", "PARAM_NON_INTERACTIVE": "false",
            "PARAM_GITOPS_RECORDED_FORGE": "gitlab", "PARAM_GITOPS_RECORDED_REPO": "acme/infra",
            "PARAM_GITOPS_REPO": "acme/infra", "PARAM_GITLAB_TOKEN_SECRET": "gitlab-forge-token",
            "PARAM_GITOPS_RECORDED_TOKEN_SECRET": "gitlab-forge-token", "PARAM_GITHUB_APP_ID": "", "PARAM_GITHUB_PEM_PATH": "",
        }, {"ANSWER_REPO": "acme/infra"})
        out = proc.stdout + proc.stderr
        self.assertIn("repo=[acme/infra] param=[acme/infra]", proc.stdout, out)
        self.assertEqual(out.count("Dropping the recorded GitLab repository"), 0, out)
        proc = self._interview("interview_gitlab_forge", {
            "PARAM_GITOPS_FORGE": "gitlab", "PARAM_NON_INTERACTIVE": "false",
            "PARAM_GITOPS_RECORDED_FORGE": "bitbucket", "PARAM_GITOPS_RECORDED_REPO": "acme/infra",
            "PARAM_GITOPS_REPO": "acme/infra", "PARAM_GITLAB_TOKEN_SECRET": "bitbucket-forge-token",
            "PARAM_GITOPS_RECORDED_TOKEN_SECRET": "bitbucket-forge-token", "PARAM_GITHUB_APP_ID": "", "PARAM_GITHUB_PEM_PATH": "",
        }, {"ANSWER_REPO": "acme/infra"})
        out = proc.stdout + proc.stderr
        self.assertIn("repo=[acme/infra] param=[acme/infra]", proc.stdout, out)
        self.assertEqual(out.count("Dropping the recorded Bitbucket Cloud repository"), 0, out)

    def test_an_interview_host_equal_to_the_recorded_host_is_kept(self):
        proc = self._interview("interview_bitbucket_forge", {
            "PARAM_GITOPS_FORGE": "bitbucket-datacenter", "PARAM_NON_INTERACTIVE": "false",
            "PARAM_GITOPS_RECORDED_FORGE": "gitlab", "PARAM_GITOPS_RECORDED_HOST": "git.example.com",
            "PARAM_GITOPS_HOST": "git.example.com", "PARAM_GITOPS_REPO": "",
            "PARAM_GITLAB_TOKEN_SECRET": "gitlab-forge-token", "PARAM_GITOPS_RECORDED_TOKEN_SECRET": "gitlab-forge-token",
            "PARAM_GITHUB_APP_ID": "", "PARAM_GITHUB_PEM_PATH": "",
        }, {"ANSWER_REPO": "INFRA/gitops", "ANSWER_HOST": "git.example.com"})
        out = proc.stdout + proc.stderr
        self.assertIn("repo=[INFRA/gitops]", proc.stdout, out)
        proc2 = self._run_install_func(
            f"{self._PROMPTS}export ANSWER_REPO=INFRA/gitops ANSWER_HOST=git.example.com; "
            "PARAM_GITOPS_FORGE=bitbucket-datacenter PARAM_NON_INTERACTIVE=false PARAM_GITOPS_RECORDED_FORGE=gitlab "
            "PARAM_GITOPS_RECORDED_HOST=git.example.com PARAM_GITOPS_HOST= PARAM_GITOPS_REPO= "
            "PARAM_GITLAB_TOKEN_SECRET=gitlab-forge-token PARAM_GITOPS_RECORDED_TOKEN_SECRET=gitlab-forge-token; "
            'run() { local github_repo=""; interview_bitbucket_forge >/dev/null 2>&1; validate_gitops_forge_flags >/dev/null 2>&1; echo "host=[$PARAM_GITOPS_HOST]"; }; run'
        )
        self.assertIn("host=[git.example.com]", proc2.stdout, proc2.stderr)

    def test_a_ca_secret_this_run_set_is_not_dropped_as_recorded(self):
        ca = self._tmp / "ca.pem"
        ca.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
        tok = self._tmp / "tok"
        tok.write_text("x")
        proc = self._validate(self._dc(
            PARAM_GITOPS_RECORDED_FORGE="gitlab", PARAM_GITOPS_RECORDED_CA_SECRET="gitlab-forge-ca",
            PARAM_GITOPS_CA_SECRET="bitbucket-forge-ca", PARAM_GITOPS_CA_FILE=str(ca), PARAM_GITLAB_TOKEN_FILE=str(tok),
            PARAM_GITOPS_REPO_FROM_FLAG="true", PARAM_GITOPS_HOST_FROM_FLAG="true"))
        out = proc.stdout + proc.stderr
        self.assertIn("rc=0 ", proc.stdout, out)
        self.assertNotIn("Dropping the recorded GitLab CA Secret", out)

    def test_an_identity_flag_equal_to_the_recorded_one_is_kept(self):
        tok = self._tmp / "tok"
        tok.write_text("x")
        body = "".join(f"{k}={shlex.quote(v)}; " for k, v in self._dc(
            PARAM_GITOPS_RECORDED_FORGE="bitbucket", PARAM_GITOPS_RECORDED_IDENTITY="svc-bot",
            PARAM_GITOPS_IDENTITY="svc-bot", PARAM_GITOPS_IDENTITY_FROM_FLAG="true", PARAM_GITLAB_TOKEN_FILE=str(tok),
            PARAM_GITOPS_REPO_FROM_FLAG="true").items())
        proc = self._run_install_func(f'{body}validate_gitops_forge_flags && rc=0 || rc=$?; echo "rc=$rc id=[$PARAM_GITOPS_IDENTITY]"')
        self.assertIn("rc=0 id=[svc-bot]", proc.stdout, proc.stderr + proc.stdout)
        proc = self._run_install_func('parse_args --gitops-identity=svc-bot; echo "flag=$PARAM_GITOPS_IDENTITY_FROM_FLAG"')
        self.assertIn("flag=true", proc.stdout, proc.stderr)

    # --- A Data Center repository URL before the host is known ---
    def test_a_dc_clone_url_gives_the_host_when_none_is_set(self):
        """--gitops-repo with a clone URL and no --gitops-host: the URL names
        the host, so it is taken from there and held to the host rules."""
        url = "https://bbdc.example.com/scm/ACME/infra.git"
        for interactive in ("false", "true"):
            with self.subTest(non_interactive=interactive):
                body = "".join(f"{k}={shlex.quote(v)}; " for k, v in self._dc(
                    PARAM_GITOPS_HOST="", PARAM_GITOPS_REPO=url, PARAM_NON_INTERACTIVE=interactive).items())
                proc = self._run_install_func(
                    f'has_controlling_tty() {{ return 0; }}; {body}validate_gitops_forge_flags && rc=0 || rc=$?; '
                    'echo "rc=$rc host=$PARAM_GITOPS_HOST repo=$PARAM_GITOPS_REPO"')
                self.assertIn("rc=0 host=bbdc.example.com repo=ACME/infra", proc.stdout, proc.stderr + proc.stdout)
        proc = self._validate(self._dc(PARAM_GITOPS_HOST="", PARAM_GITOPS_REPO="https://acme.atlassian.net/scm/ACME/infra.git"))
        self.assertRegex(proc.stdout, r"rc=[1-9]", proc.stderr + proc.stdout)
        self.assertIn("hosted service", proc.stdout + proc.stderr)
        proc = self._validate(self._dc(PARAM_GITOPS_HOST="", PARAM_GITOPS_REPO="https://bbdc.example.com:7990/scm/ACME/infra.git"))
        self.assertRegex(proc.stdout, r"rc=[1-9]", proc.stderr + proc.stdout)
        self.assertIn("root path on port 443", proc.stdout + proc.stderr)

    def test_a_dc_path_waits_for_the_interview_host(self):
        body = "".join(f"{k}={shlex.quote(v)}; " for k, v in self._dc(
            PARAM_GITOPS_HOST="", PARAM_GITOPS_REPO="ACME/infra", PARAM_NON_INTERACTIVE="false").items())
        proc = self._run_install_func(
            f'has_controlling_tty() {{ return 0; }}; {body}validate_gitops_forge_flags && rc=0 || rc=$?; '
            'echo "rc=$rc host=[$PARAM_GITOPS_HOST] repo=$PARAM_GITOPS_REPO"')
        self.assertIn("rc=0 host=[] repo=ACME/infra", proc.stdout, proc.stderr + proc.stdout)
        proc = self._interview("interview_bitbucket_forge", self._dc(
            PARAM_GITOPS_HOST="", PARAM_GITOPS_REPO="ACME/infra", PARAM_NON_INTERACTIVE="false"),
            {"ANSWER_REPO": "https://bbdc.example.com/scm/ACME/infra.git", "ANSWER_HOST": "bbdc.example.com"})
        self.assertIn("repo=[ACME/infra] param=[ACME/infra]", proc.stdout, proc.stderr + proc.stdout)

    def test_main_holds_no_interview_leftovers(self):
        """The Bitbucket interview moved into interview_bitbucket_forge; its
        label is computed there only."""
        self.assertEqual(_INSTALL_SH.read_text().count('bb_label="$(gitops_forge_label "$PARAM_GITOPS_FORGE")"'), 1)

    def test_parse_args_marks_a_named_secret(self):
        proc = self._run_install_func(
            'parse_args --gitops-token-secret=s1; echo "flag=$PARAM_GITLAB_TOKEN_SECRET_FROM_FLAG"')
        self.assertIn("flag=true", proc.stdout, proc.stderr)

    def test_a_switch_away_from_bitbucket_drops_the_recorded_identity(self):
        proc = self._validate({
            "PARAM_GITOPS_FORGE": "gitlab", "PARAM_GITOPS_REPO": "g/p", "PARAM_NON_INTERACTIVE": "true",
            "PARAM_GITOPS_RECORDED_FORGE": "bitbucket", "PARAM_GITOPS_IDENTITY": _UUID,
            "PARAM_GITOPS_RECORDED_IDENTITY": _UUID,
            # A switch between two token forges needs the new token's Secret named.
            "PARAM_GITLAB_TOKEN_SECRET": "gitlab-forge-token", "PARAM_GITLAB_TOKEN_SECRET_FROM_FLAG": "true",
        })
        self.assertIn("rc=0 ", proc.stdout, proc.stderr + proc.stdout)
        self.assertIn("Dropping the recorded Bitbucket Cloud identity", proc.stdout + proc.stderr)


_KUBECTL_STUB = r"""#!/usr/bin/env bash
echo "kubectl $*" >> "$STUB_DIR/kubectl.log"
case "$*" in
  *"get secret"*"jsonpath={.data.token}"*) printf 'b2xk' ;;
  *"get secret"*"jsonpath={.data.read-token}"*) [ "${STUB_HAS_READ_TOKEN:-1}" = 1 ] && printf 'b2xk' ;;
esac
case "$1 $2" in
  "create secret") cat >/dev/null 2>&1; printf 'apiVersion: v1\nkind: Secret\n' ;;
  "apply"*) cat >/dev/null ;;
esac
exit 0
"""


class BitbucketForgeSwitchSecretTest(unittest.TestCase):
    """After a forge switch, the token Secret must hold nothing of the old
    forge: not its write token, and not its read token."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._tmp = pathlib.Path(tmp.name)
        bin_dir = self._tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "kubectl").write_text(_KUBECTL_STUB)
        (bin_dir / "kubectl").chmod(0o755)
        self._bin = bin_dir
        (self._tmp / "install.env").write_text("")

    def _run(self, body, **env):
        setup = f'source "{_INSTALLER_COMMON}"\nKUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n{body}\n'
        overrides = {"KUBE_AGENTS_INSTALL_ENV": str(self._tmp / "install.env"), "STUB_DIR": str(self._tmp),
                     "PATH": f"{self._bin}:" + __import__("os").environ["PATH"]}
        overrides.update(env)
        return _run_installer_bash(setup, get_isolated_test_env(overrides=overrides, absent=_TOKEN_SOURCE_VARS))

    def _log(self):
        f = self._tmp / "kubectl.log"
        return f.read_text() if f.exists() else ""

    def test_a_switch_removes_the_old_read_token(self):
        proc = self._run(
            "GITOPS_FORGE=bitbucket GITLAB_TOKEN_SECRET=bitbucket-forge-token "
            "PARAM_GITOPS_OLD_TOKEN_SECRET=bitbucket-forge-token PARAM_GITLAB_READ_TOKEN_FILE=; "
            'create_gitlab_read_token_secret agents ctx1 && rc=0 || rc=$?; echo "rc=$rc"'
        )
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        self.assertRegex(self._log(), r"patch secret bitbucket-forge-token .*remove.*/data/read-token")
        self.assertIn("read-token", proc.stdout + proc.stderr)

    def test_a_secret_this_run_names_keeps_its_read_token(self):
        self._run(
            "GITOPS_FORGE=bitbucket GITLAB_TOKEN_SECRET=bb-cloud "
            "PARAM_GITOPS_OLD_TOKEN_SECRET=bitbucket-forge-token PARAM_GITLAB_READ_TOKEN_FILE=; "
            "create_gitlab_read_token_secret agents ctx1"
        )
        self.assertNotIn("patch secret", self._log())

    def test_a_named_recorded_secret_keeps_its_read_token_on_a_switch(self):
        """--gitops-token-secret=<the recorded name>: the administrator stored
        the new forge's tokens in that Secret first, read token included."""
        self._run(
            "GITOPS_FORGE=bitbucket-datacenter GITLAB_TOKEN_SECRET=bitbucket-forge-token "
            "PARAM_GITOPS_OLD_TOKEN_SECRET=bitbucket-forge-token PARAM_GITLAB_TOKEN_SECRET_FROM_FLAG=true "
            "PARAM_GITLAB_READ_TOKEN_FILE=; create_gitlab_read_token_secret agents ctx1"
        )
        self.assertNotIn("patch secret", self._log())

    def test_no_switch_keeps_the_read_token(self):
        self._run(
            "GITOPS_FORGE=bitbucket GITLAB_TOKEN_SECRET=bitbucket-forge-token PARAM_GITLAB_READ_TOKEN_FILE=; "
            "create_gitlab_read_token_secret agents ctx1"
        )
        self.assertNotIn("patch secret", self._log())

    def test_an_empty_answer_on_a_switch_stops_the_run(self):
        """The prompt offers no "keep"; an empty answer must not leave the old
        forge's token in place with a success code."""
        proc = self._run(
            "has_controlling_tty() { return 0; }; "
            "GITOPS_FORGE=bitbucket GITLAB_TOKEN_SECRET=bitbucket-forge-token PARAM_NON_INTERACTIVE=false "
            "PARAM_GITOPS_TOKEN_MUST_REPLACE=true PARAM_GITLAB_TOKEN_FILE=; "
            "read() { token=''; return 0; }; "
            'create_gitlab_token_secret agents ctx1 </dev/null && rc=0 || rc=$?; echo "rc=$rc"'
        )
        self.assertRegex(proc.stdout, r"rc=[1-9]", proc.stderr + proc.stdout)
        self.assertNotIn("No token entered; nothing was stored. Create", proc.stdout + proc.stderr)


class BitbucketInstallEnvTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._tmp = pathlib.Path(tmp.name)
        self._loaded = self._tmp / "loaded.env"
        self._loaded.write_text("")

    def _bash(self, body):
        full = f'source "{_INSTALLER_COMMON}"\nKUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\nPARAM_DRY_RUN=false\n{body}'
        return subprocess.run(
            ["bash", "-c", full], capture_output=True, text=True, cwd=str(_REPO_ROOT),
            env=get_isolated_test_env(absent=_TOKEN_SOURCE_VARS, overrides={"KUBE_AGENTS_INSTALL_ENV": str(self._loaded)}),
        )

    def test_switching_a_github_install_to_bitbucket_records_it(self):
        dest = self._tmp / "install.env"
        dest.write_text("PROJECT_ID=p\nGITOPS_ORG=acme\nGITOPS_REPO=infra\nGITHUB_APP_ID=123\n")
        proc = self._bash(
            f"export GITOPS_FORGE=bitbucket GITOPS_HOST= GITOPS_REPO=acme/infra GITLAB_TOKEN_SECRET=bb "
            f"GITOPS_IDENTITY='{_UUID}' GITOPS_ORG= GITHUB_APP_ID=\n"
            f'record_gitops_forge_keys "{dest}"\n'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        text = dest.read_text()
        for line in ("GITOPS_FORGE=bitbucket", "GITLAB_TOKEN_SECRET=bb", "GITOPS_REPO=acme/infra"):
            self.assertIn(line + "\n", text)
        self.assertIn("GITOPS_IDENTITY=", text)
        self.assertIn("0b3c1f2e-5d6a-4b7c-8d9e-0f1a2b3c4d5e", text)
        self.assertNotIn("GITHUB_APP_ID", text)
        proc = self._bash(
            "unset GITOPS_FORGE GITOPS_REPO GITLAB_TOKEN_SECRET GITOPS_IDENTITY\n"
            f'load_install_env "{dest}"; echo "forge=$GITOPS_FORGE repo=$GITOPS_REPO id=$GITOPS_IDENTITY"\n'
        )
        self.assertIn(f"forge=bitbucket repo=acme/infra id={_UUID}", proc.stdout, proc.stderr)

    def test_a_switch_records_the_pending_old_forge_and_clearing_removes_it(self):
        dest = self._tmp / "install.env"
        dest.write_text("PROJECT_ID=p\nGITOPS_FORGE=bitbucket-datacenter\nGITOPS_HOST=bb.example.com\n"
                        "GITLAB_TOKEN_SECRET=bitbucket-forge-token\nGITOPS_REPO=INFRA/gitops\n")
        proc = self._bash(
            "export GITOPS_FORGE=bitbucket GITOPS_HOST= GITOPS_REPO=acme/infra GITLAB_TOKEN_SECRET=bitbucket-forge-token "
            "GITOPS_CA_SECRET= GITOPS_IDENTITY=\n"
            "PARAM_GITOPS_SWITCH_FROM=bitbucket-datacenter PARAM_GITOPS_OLD_TOKEN_SECRET=bitbucket-forge-token\n"
            f'record_gitops_forge_keys "{dest}"\n'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        text = dest.read_text()
        self.assertIn("GITOPS_FORGE=bitbucket\n", text)
        self.assertIn("GITOPS_SWITCH_FROM_FORGE=bitbucket-datacenter\n", text)
        self.assertIn("GITOPS_SWITCH_FROM_SECRET=bitbucket-forge-token\n", text)
        proc = self._bash(f'clear_gitops_pending_switch "{dest}"\n')
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        text = dest.read_text()
        self.assertNotIn("GITOPS_SWITCH_FROM", text)
        self.assertIn("GITOPS_FORGE=bitbucket\n", text)
        self.assertIn("PROJECT_ID=p\n", text)

    def test_the_pending_switch_is_cleared_only_after_both_token_steps(self):
        source = _INSTALL_SH.read_text()
        write = source.index('    create_gitlab_token_secret "$namespace" "$expected_ctx" || exit 1')
        read = source.index('    create_gitlab_read_token_secret "$namespace" "$expected_ctx" || exit 1')
        clear = source.index('    clear_gitops_pending_switch "$INSTALL_ENV_FILE"')
        self.assertLess(write, read)
        self.assertLess(read, clear)

    def test_an_unchanged_bitbucket_install_env_is_not_rewritten(self):
        dest = self._tmp / "install.env"
        dest.write_text("GITOPS_FORGE=bitbucket-datacenter\nGITOPS_HOST=bb.example.com\nGITLAB_TOKEN_SECRET=bb\n"
                        "GITOPS_CA_SECRET=\nGITOPS_REPO=INFRA/gitops\n")
        before = dest.stat().st_mtime_ns
        proc = self._bash(
            "export GITOPS_FORGE=bitbucket-datacenter GITOPS_HOST=bb.example.com GITLAB_TOKEN_SECRET=bb "
            f"GITOPS_CA_SECRET= GITOPS_REPO=INFRA/gitops GITOPS_IDENTITY=\nrecord_gitops_forge_keys \"{dest}\"\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertEqual(dest.stat().st_mtime_ns, before)

    def test_the_example_documents_the_bitbucket_keys(self):
        example = (_REPO_ROOT / "install.env.example").read_text()
        for key in ("GITOPS_FORGE=bitbucket", "GITOPS_IDENTITY="):
            self.assertIn("# " + key, example)


if __name__ == "__main__":
    unittest.main()
