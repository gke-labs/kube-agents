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

    def test_a_switch_from_gitlab_takes_the_bitbucket_default_secret(self):
        proc = self._validate(self._cloud(
            PARAM_GITOPS_RECORDED_FORGE="gitlab", PARAM_GITLAB_TOKEN_SECRET="gitlab-forge-token"))
        self.assertIn("rc=0 repo=acme/infra host= secret=bitbucket-forge-token", proc.stdout, proc.stderr)

    def test_a_switch_away_from_bitbucket_drops_the_recorded_identity(self):
        proc = self._validate({
            "PARAM_GITOPS_FORGE": "gitlab", "PARAM_GITOPS_REPO": "g/p", "PARAM_NON_INTERACTIVE": "true",
            "PARAM_GITOPS_RECORDED_FORGE": "bitbucket", "PARAM_GITOPS_IDENTITY": _UUID,
            "PARAM_GITOPS_RECORDED_IDENTITY": _UUID,
        })
        self.assertIn("rc=0 ", proc.stdout, proc.stderr + proc.stdout)
        self.assertIn("Dropping the recorded Bitbucket Cloud identity", proc.stdout + proc.stderr)


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
