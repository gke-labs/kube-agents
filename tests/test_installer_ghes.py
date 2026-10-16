"""A GitHub Enterprise Server as the installer's GitOps forge.

A GitHub Enterprise Server install is the github forge with --gitops-host: the
GitHub App, the KMS import and the token minter are the same as on github.com,
at the instance. These tests hold the installer to that:

- the flags: --gitops-host and --gitops-ca-file with the github forge, a host
  that GitHub operates refused, a github.com spelling read as the default;
- install.env records GITOPS_FORGE=github, GITOPS_HOST and GITOPS_CA_SECRET,
  so that upgrade.sh and uninstall.sh render the same forge, and a switch back
  to github.com drops them;
- the organization check asks the instance's /api/v3, with the CA, and sends
  no credential;
- the tfvars carry gitops_host and github_ca_secret_name, and a github.com
  install renders what it rendered before;
- the CA Secret is written after the apply, and the minter, which reads the
  CA when it starts, is restarted.

The Terraform rendering itself is in
terraform/examples/full-install/tests/github_enterprise.tftest.hcl.
"""

import base64
import pathlib
import shlex
import subprocess
import tempfile
import unittest

from tests.test_install_script import (
    _INSTALL_SH,
    _INSTALLER_COMMON,
    _REPO_ROOT,
    _run_installer_bash,
)
# The modules, not the classes: a TestCase imported by name is collected here too.
from tests import test_installer_common as _installer_common
from tests import test_installer_gitlab as _gitlab
from tests.testing.common import get_isolated_test_env

_PEM = "-----BEGIN CERTIFICATE-----\nMIIBghes\n-----END CERTIFICATE-----\n"
_HOST = "github.acme.corp"
_UPGRADE_SH = _REPO_ROOT / "upgrade.sh"
_UNINSTALL_SH = _REPO_ROOT / "uninstall.sh"


class _Tmp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._tmp = pathlib.Path(tmp.name)
        self._empty_install_env = self._tmp / "install.env"
        self._empty_install_env.write_text("")

    def _ca(self, name="ca.pem", content=_PEM):
        path = self._tmp / name
        path.write_text(content)
        return path


class GhesFlagsTest(_Tmp):
    def _run_install_func(self, func_call, install_env=None):
        setup = f'source "{_INSTALLER_COMMON}"\nKUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n{func_call}\n'
        env_file = self._empty_install_env
        if install_env is not None:
            env_file = self._tmp / "recorded.env"
            env_file.write_text(install_env)
        return _run_installer_bash(setup, get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": str(env_file)}))

    def _validate(self, assignments):
        body = "".join(f"{k}={shlex.quote(v)}; " for k, v in assignments.items())
        return self._run_install_func(
            f"{body}validate_gitops_forge_flags && rc=0 || rc=$?; "
            'echo "rc=$rc host=[$PARAM_GITOPS_HOST] cm=[$PARAM_GITOPS_CA_SECRET] ca=[$PARAM_GITOPS_CA_FILE]"'
        )

    def _ghes(self, **extra):
        base = {
            "PARAM_GITOPS_FORGE": "github",
            "PARAM_GITOPS_HOST": _HOST,
            "PARAM_GITOPS_HOST_FROM_FLAG": "true",
            "PARAM_GITOPS_ORG": "platform",
            "PARAM_GITOPS_REPO": "infra",
        }
        base.update(extra)
        return base

    def test_parse_args_takes_the_host_and_the_ca_with_github(self):
        proc = self._run_install_func(
            f"parse_args --gitops-host={_HOST} --gitops-ca-file=/x/ca.pem --gitops-org=platform; "
            'echo "f=[$PARAM_GITOPS_FORGE] h=$PARAM_GITOPS_HOST c=$PARAM_GITOPS_CA_FILE flag=$PARAM_GITOPS_HOST_FROM_FLAG"'
        )
        self.assertIn(f"f=[] h={_HOST} c=/x/ca.pem flag=true", proc.stdout, proc.stderr)

    def test_a_host_flag_settles_the_forge_question(self):
        # --gitops-host alone means the default forge, github, at that host:
        # the interview must not ask the forge question and clear the host.
        proc = self._run_install_func(
            f"parse_args --gitops-host={_HOST}\nresolve_shared_defaults >/dev/null 2>&1\n"
            'echo "given=$PARAM_GITOPS_FORGE_GIVEN forge=$PARAM_GITOPS_FORGE"'
        )
        self.assertIn("given=true forge=github", proc.stdout, proc.stderr)

    def test_a_ghes_host_is_accepted(self):
        for host in (_HOST, "ghe.example.com", "git.corp-1.example.net", "github.acme.io"):
            with self.subTest(host=host):
                proc = self._validate(self._ghes(PARAM_GITOPS_HOST=host))
                self.assertIn(f"rc=0 host=[{host}] cm=[]", proc.stdout, proc.stderr + proc.stdout)

    def test_a_host_github_operates_is_refused(self):
        for host in (
            "api.github.com",
            "acme.github.com",
            "raw.githubusercontent.com",
            "githubusercontent.com",
            "acme.ghe.com",
            "ghe.com",
            "githubassets.com",
            "x.githubapp.com",
            "acme.github.io",
            "acme.github.dev",
        ):
            with self.subTest(host=host):
                proc = self._validate(self._ghes(PARAM_GITOPS_HOST=host))
                self.assertRegex(proc.stdout, r"rc=[1-9]", proc.stderr + proc.stdout)
                self.assertIn("a host that GitHub operates", proc.stdout + proc.stderr)

    def test_gitlab_refuses_every_host_github_operates(self):
        # The same list as the GitHub branch and the operator's gitlab
        # provider, not only github.com and githubusercontent.com.
        for host in ("acme.ghe.com", "acme.github.io", "acme.github.dev", "githubassets.com",
                     "x.githubapp.com", "api.github.com", "raw.githubusercontent.com"):
            with self.subTest(host=host):
                proc = self._validate({"PARAM_GITOPS_FORGE": "gitlab", "PARAM_GITOPS_HOST": host,
                                       "PARAM_GITOPS_HOST_FROM_FLAG": "true", "PARAM_GITOPS_REPO": "g/p",
                                       "PARAM_GITLAB_TOKEN_SECRET": "gitlab-forge-token"})
                self.assertRegex(proc.stdout, r"rc=[1-9]", proc.stderr + proc.stdout)
                self.assertIn("is a GitHub host, not a GitLab one", proc.stdout + proc.stderr)

    def test_a_gitlab_host_is_refused(self):
        for host in ("gitlab.com", "www.gitlab.com"):
            with self.subTest(host=host):
                proc = self._validate(self._ghes(PARAM_GITOPS_HOST=host))
                self.assertRegex(proc.stdout, r"rc=[1-9]", proc.stderr + proc.stdout)
                self.assertIn("is a GitLab host", proc.stdout + proc.stderr)

    def test_a_host_that_is_not_bare_is_refused(self):
        for host in ("https://github.acme.corp", "github.acme.corp:8443", "GitHub.Acme.Corp", "localhost",
                     "github.acme.corp/x", "a" * 250 + ".corp"):
            with self.subTest(host=host):
                proc = self._validate(self._ghes(PARAM_GITOPS_HOST=host))
                self.assertRegex(proc.stdout, r"rc=[1-9]", proc.stderr + proc.stdout)

    def test_a_github_com_spelling_is_the_default_host(self):
        for host in ("github.com", "www.github.com", "ssh.github.com"):
            with self.subTest(host=host):
                proc = self._validate(self._ghes(PARAM_GITOPS_HOST=host, PARAM_GITOPS_CA_SECRET="github-forge-ca"))
                self.assertIn("rc=0 host=[] cm=[]", proc.stdout, proc.stderr + proc.stdout)
                self.assertIn("github.com, the default GitHub host", proc.stdout)

    def test_a_gitlab_token_file_is_refused_with_github(self):
        tok = self._ca("tok", "x")
        proc = self._validate(self._ghes(PARAM_GITLAB_TOKEN_FILE=str(tok)))
        self.assertRegex(proc.stdout, r"rc=[1-9]", proc.stderr + proc.stdout)
        self.assertIn("--gitlab-token-file applies only with --gitops-forge=gitlab", proc.stdout + proc.stderr)

    def test_a_ca_file_names_the_github_secret(self):
        ca = self._ca()
        proc = self._validate(self._ghes(PARAM_GITOPS_CA_FILE=str(ca), PARAM_GITOPS_CA_FILE_FROM_FLAG="true"))
        self.assertIn(f"rc=0 host=[{_HOST}] cm=[github-forge-ca]", proc.stdout, proc.stderr + proc.stdout)

    def test_a_named_ca_secret_is_kept_and_checked(self):
        proc = self._validate(self._ghes(PARAM_GITOPS_CA_SECRET="ghe-ca"))
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        self.assertIn("cm=[ghe-ca]", proc.stdout)
        proc = self._validate(self._ghes(PARAM_GITOPS_CA_SECRET="Bad_Name"))
        self.assertRegex(proc.stdout, r"rc=[1-9]", proc.stderr + proc.stdout)

    def test_a_ca_file_needs_a_ghes_host(self):
        ca = self._ca()
        for host in ("", "github.com"):
            for from_flag in ("true", "false"):
                with self.subTest(host=host, from_flag=from_flag):
                    proc = self._validate(self._ghes(PARAM_GITOPS_HOST=host, PARAM_GITOPS_CA_FILE=str(ca),
                                                     PARAM_GITOPS_CA_FILE_FROM_FLAG=from_flag))
                    self.assertRegex(proc.stdout, r"rc=[1-9]", proc.stderr + proc.stdout)
                    self.assertIn("github.com uses the public CAs", proc.stdout + proc.stderr)

    def test_a_bad_regular_ca_file_is_refused_before_the_apply(self):
        for name, content, message in (
            ("not-pem", "hello\n", "no PEM certificate"),
            ("with-key", _PEM + "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n", "private key"),
        ):
            with self.subTest(name=name):
                ca = self._ca(f"{name}.pem", content)
                proc = self._validate(self._ghes(PARAM_GITOPS_CA_FILE=str(ca), PARAM_GITOPS_CA_FILE_FROM_FLAG="true"))
                self.assertRegex(proc.stdout, r"rc=[1-9] host=\S* cm=\[\]", proc.stderr + proc.stdout)
                self.assertIn(message, proc.stdout + proc.stderr)

    def test_a_one_shot_ca_source_is_not_read_before_the_apply(self):
        # The rereadable-source guard: a descriptor path is read once, by the
        # CA Secret step after the apply, so the validator does not read it.
        proc = self._run_install_func(
            "PARAM_GITOPS_FORGE=github PARAM_GITOPS_HOST=github.acme.corp PARAM_GITOPS_HOST_FROM_FLAG=true "
            "PARAM_GITOPS_ORG=platform PARAM_GITOPS_REPO=infra PARAM_GITOPS_CA_FILE=/dev/stdin "
            "PARAM_GITOPS_CA_FILE_FROM_FLAG=true\n"
            'exec 0<<<"not a pem"\n'
            'validate_gitops_forge_flags && rc=0 || rc=$?\n'
            'rest="$(cat)"; echo "rc=$rc cm=[$PARAM_GITOPS_CA_SECRET] rest=[$rest]"'
        )
        self.assertIn("rc=0 cm=[github-forge-ca] rest=[not a pem]", proc.stdout, proc.stderr + proc.stdout)

    def test_a_non_interactive_ghes_install_needs_an_org(self):
        proc = self._validate(self._ghes(PARAM_GITOPS_ORG="", PARAM_NON_INTERACTIVE="true"))
        self.assertRegex(proc.stdout, r"rc=[1-9]", proc.stderr + proc.stdout)
        self.assertIn("needs --gitops-org", proc.stdout + proc.stderr)

    def test_the_org_and_the_repo_are_names_on_the_instance(self):
        for case in (
            self._ghes(PARAM_GITOPS_ORG="platform/infra"),
            self._ghes(PARAM_GITOPS_ORG="-platform"),
            self._ghes(PARAM_GITOPS_REPO="https://github.acme.corp/platform/infra"),
            self._ghes(PARAM_GITOPS_REPO="platform/infra"),
            self._ghes(PARAM_GITOPS_REPO=".."),
        ):
            with self.subTest(case=case):
                proc = self._validate(case)
                self.assertRegex(proc.stdout, r"rc=[1-9]", proc.stderr + proc.stdout)

    def test_a_recorded_ghes_install_keeps_its_host_and_ca(self):
        proc = self._run_install_func(
            "parse_args\nvalidate_gitops_forge_flags && rc=0 || rc=$?\n"
            'echo "rc=$rc forge=$PARAM_GITOPS_FORGE host=[$PARAM_GITOPS_HOST] cm=[$PARAM_GITOPS_CA_SECRET]"',
            install_env=f"GITOPS_FORGE=github\nGITOPS_HOST={_HOST}\nGITOPS_CA_SECRET=github-forge-ca\n"
                        "GITOPS_ORG=platform\nGITOPS_REPO=infra\n",
        )
        self.assertIn(f"rc=0 forge=github host=[{_HOST}] cm=[github-forge-ca]", proc.stdout, proc.stderr)

    def test_a_switch_from_ghes_to_gitlab_drops_the_host_and_the_ca(self):
        proc = self._run_install_func(
            "parse_args --gitops-forge=gitlab --gitops-repo=g/p\nvalidate_gitops_forge_flags && rc=0 || rc=$?\n"
            'echo "rc=$rc forge=$PARAM_GITOPS_FORGE host=[$PARAM_GITOPS_HOST] cm=[$PARAM_GITOPS_CA_SECRET]"',
            install_env=f"GITOPS_FORGE=github\nGITOPS_HOST={_HOST}\nGITOPS_CA_SECRET=github-forge-ca\n"
                        "GITOPS_ORG=platform\nGITOPS_REPO=infra\n",
        )
        self.assertIn("rc=0 forge=gitlab host=[] cm=[]", proc.stdout, proc.stderr)
        self.assertIn(f"Dropping the recorded GitHub Enterprise Server host '{_HOST}'", proc.stdout)
        self.assertIn("Dropping the recorded GitHub Enterprise Server CA Secret", proc.stdout)

    def test_a_switch_from_gitlab_to_ghes_drops_the_gitlab_ca(self):
        proc = self._run_install_func(
            f"parse_args --gitops-forge=github --gitops-host={_HOST} --gitops-org=platform --gitops-repo=infra\n"
            "validate_gitops_forge_flags && rc=0 || rc=$?\n"
            'echo "rc=$rc forge=$PARAM_GITOPS_FORGE host=[$PARAM_GITOPS_HOST] cm=[$PARAM_GITOPS_CA_SECRET]"',
            install_env="GITOPS_FORGE=gitlab\nGITOPS_HOST=gitlab.example.com\nGITLAB_TOKEN_SECRET=gl\n"
                        "GITOPS_CA_SECRET=gitlab-forge-ca\nGITOPS_REPO=g/p\n",
        )
        self.assertIn(f"rc=0 forge=github host=[{_HOST}] cm=[]", proc.stdout, proc.stderr)
        self.assertIn("Dropping the recorded GitLab CA Secret", proc.stdout)

    def test_a_switch_back_to_github_com_drops_the_host_and_the_ca(self):
        proc = self._run_install_func(
            "parse_args --gitops-host=github.com\nvalidate_gitops_forge_flags && rc=0 || rc=$?\n"
            'echo "rc=$rc forge=$PARAM_GITOPS_FORGE host=[$PARAM_GITOPS_HOST] cm=[$PARAM_GITOPS_CA_SECRET]"',
            install_env=f"GITOPS_FORGE=github\nGITOPS_HOST={_HOST}\nGITOPS_CA_SECRET=github-forge-ca\n"
                        "GITOPS_ORG=platform\nGITOPS_REPO=infra\n",
        )
        self.assertIn("rc=0 forge=github host=[] cm=[]", proc.stdout, proc.stderr)

    def test_through_main_a_bad_host_stops_before_step_one(self):
        body = (
            f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
            "main -y --image-tag=0.1.0 --gitops-host=acme.ghe.com --gitops-org=platform && rc=0 || rc=$?\n"
            'echo "rc=$rc"\n'
        )
        proc = _run_installer_bash(body, get_isolated_test_env(overrides={
            "KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env)}))
        out = proc.stdout + proc.stderr
        self.assertNotIn("rc=0", proc.stdout, out)
        self.assertIn("a host that GitHub operates", out)
        self.assertNotIn("1. Checking Prerequisites", out)


class GhesInstallEnvTest(_Tmp):
    """install.env records the instance, so upgrade.sh and uninstall.sh render it."""

    def _bash(self, body):
        full = f'source "{_INSTALLER_COMMON}"\nKUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\nPARAM_DRY_RUN=false\n{body}'
        return subprocess.run(
            ["bash", "-c", full], capture_output=True, text=True, cwd=str(_REPO_ROOT),
            env=get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env)}),
        )

    def test_a_new_install_env_records_the_host_and_the_secret_name_not_the_file(self):
        ca = self._ca()
        dest = self._tmp / "out.env"
        proc = self._bash(
            f"export GITOPS_ORG=platform GITOPS_REPO=infra GITOPS_HOST={_HOST} GITOPS_CA_SECRET=github-forge-ca\n"
            f"PARAM_GITOPS_CA_FILE={shlex.quote(str(ca))}\n"
            f'bootstrap_install_env_file "{dest}" 0.5.0\n'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        text = dest.read_text()
        for line in ("GITOPS_FORGE=github", f"GITOPS_HOST={_HOST}", "GITOPS_CA_SECRET=github-forge-ca",
                     "GITOPS_ORG=platform", "GITOPS_REPO=infra"):
            self.assertIn(line + "\n", text)
        self.assertNotIn(str(ca), text)
        self.assertNotIn("CA_FILE", text)
        self.assertNotIn("GITLAB_TOKEN_SECRET", text)

    def test_a_github_com_install_env_has_no_forge_keys(self):
        dest = self._tmp / "out.env"
        proc = self._bash(f'export GITOPS_ORG=acme GITOPS_REPO=infra\nbootstrap_install_env_file "{dest}" 0.5.0\n')
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        for key in ("GITOPS_FORGE", "GITOPS_HOST", "GITOPS_CA_SECRET"):
            self.assertNotIn(key, dest.read_text())

    def test_switching_github_com_to_ghes_records_it(self):
        dest = self._tmp / "install.env"
        dest.write_text("PROJECT_ID=p\nGITOPS_ORG=acme\nGITOPS_REPO=infra\nGITHUB_APP_ID=1\nMEMORY=file\n")
        dest.write_text(dest.read_text() + "KMS_KEY=github-token-minter-key\n")
        proc = self._bash(
            f"export GITOPS_FORGE=github GITOPS_HOST={_HOST} GITOPS_CA_SECRET=github-forge-ca "
            "GITOPS_ORG=platform GITOPS_REPO=infra GITHUB_APP_ID=12 KMS_KEY=ghes-minter-key\n"
            f'record_gitops_forge_keys "{dest}"\n'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        text = dest.read_text()
        for line in ("PROJECT_ID=p", "MEMORY=file", "GITOPS_FORGE=github", f"GITOPS_HOST={_HOST}",
                     "GITOPS_CA_SECRET=github-forge-ca", "GITOPS_ORG=platform", "GITOPS_REPO=infra",
                     "GITHUB_APP_ID=12", "KMS_KEY=ghes-minter-key"):
            self.assertIn(line + "\n", text)
        # The new App's key replaces the old App's; upgrade.sh renders it.
        self.assertNotIn("KMS_KEY=github-token-minter-key", text)
        self.assertEqual(oct(dest.stat().st_mode & 0o777), "0o600")
        self.assertIn(f"GitHub Enterprise Server {_HOST}", proc.stdout)

    def test_switching_ghes_back_to_github_com_drops_the_forge_keys(self):
        dest = self._tmp / "install.env"
        dest.write_text(f"PROJECT_ID=p\nGITOPS_FORGE=github\nGITOPS_HOST={_HOST}\nGITOPS_CA_SECRET=github-forge-ca\n"
                        "GITOPS_ORG=platform\nGITOPS_REPO=infra\n")
        proc = self._bash(
            "unset GITOPS_FORGE GITOPS_HOST GITOPS_CA_SECRET; export GITOPS_ORG=acme GITOPS_REPO=infra GITHUB_APP_ID=9\n"
            f'record_gitops_forge_keys "{dest}"\n'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        text = dest.read_text()
        for key in ("GITOPS_FORGE", "GITOPS_HOST", "GITOPS_CA_SECRET"):
            self.assertNotIn(key, text)
        for line in ("GITOPS_ORG=acme", "GITOPS_REPO=infra", "GITHUB_APP_ID=9", "PROJECT_ID=p"):
            self.assertIn(line + "\n", text)

    def test_a_new_ghes_host_or_ca_is_recorded(self):
        for host, secret in (("ghe2.acme.corp", "github-forge-ca"), (_HOST, ""), (_HOST, "other-ca")):
            with self.subTest(host=host, secret=secret):
                dest = self._tmp / "install.env"
                dest.write_text(f"GITOPS_FORGE=github\nGITOPS_HOST={_HOST}\nGITOPS_CA_SECRET=github-forge-ca\n"
                                "GITOPS_ORG=platform\nGITOPS_REPO=infra\n")
                proc = self._bash(
                    f"export GITOPS_FORGE=github GITOPS_HOST={host} GITOPS_CA_SECRET={secret} "
                    f'GITOPS_ORG=platform GITOPS_REPO=infra\nrecord_gitops_forge_keys "{dest}"\n'
                )
                self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                text = dest.read_text()
                self.assertIn(f"GITOPS_HOST={host}\n", text)
                self.assertRegex(text, rf"GITOPS_CA_SECRET=({secret}|'{secret}')\n")

    def test_an_unchanged_ghes_install_env_is_not_rewritten(self):
        dest = self._tmp / "install.env"
        original = (f"GITOPS_FORGE=github\nGITOPS_HOST={_HOST}\nGITOPS_CA_SECRET=github-forge-ca\n"
                    "GITOPS_ORG=platform\nGITOPS_REPO=infra\n# kept\n")
        dest.write_text(original)
        proc = self._bash(
            f"export GITOPS_FORGE=github GITOPS_HOST={_HOST} GITOPS_CA_SECRET=github-forge-ca "
            f'GITOPS_ORG=platform GITOPS_REPO=infra\nrecord_gitops_forge_keys "{dest}"\n'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertEqual(dest.read_text(), original)

    def test_a_shell_export_does_not_choose_the_host(self):
        dest = self._tmp / "install.env"
        dest.write_text("PROJECT_ID=p\nGITOPS_ORG=acme\nGITOPS_REPO=infra\n")
        proc = self._bash(
            f"export GITOPS_HOST={_HOST} GITOPS_CA_SECRET=x\n"
            f'load_install_env "{dest}"; echo "host=[${{GITOPS_HOST:-}}] ca=[${{GITOPS_CA_SECRET:-}}]"\n'
        )
        self.assertIn("host=[] ca=[]", proc.stdout, proc.stderr)

    def test_the_example_documents_the_ghes_keys(self):
        example = (_REPO_ROOT / "install.env.example").read_text()
        self.assertIn("# GITOPS_FORGE=github", example)
        self.assertIn("GitHub Enterprise Server", example)


class GhesTfvarsTest(unittest.TestCase):
    _run = _installer_common.InstallerCommonTest._run
    _tfvars = _gitlab.GitLabTfvarsTest._tfvars

    def test_ghes_renders_the_host_and_the_ca_secret(self):
        content = self._tfvars({
            "GITOPS_FORGE": "github", "GITOPS_HOST": _HOST, "GITOPS_CA_SECRET": "github-forge-ca",
            "GITOPS_ORG": "platform", "GITOPS_REPO": "infra", "GITHUB_APP_ID": "12",
        })
        self.assertIn('github_repo = "platform/infra"', content)
        self.assertIn(f'gitops_host = "{_HOST}"', content)
        self.assertIn('github_ca_secret_name = "github-forge-ca"', content)
        self.assertIn('github_app_id        = "12"', content)
        for key in ("gitops_forge", "gitlab_repo", "gitlab_token_secret_name", "gitlab_ca_secret_name"):
            self.assertNotIn(key, content)

    def test_ghes_without_a_ca_names_no_secret(self):
        content = self._tfvars({"GITOPS_HOST": _HOST, "GITOPS_ORG": "platform", "GITOPS_REPO": "infra"})
        self.assertIn(f'gitops_host = "{_HOST}"', content)
        self.assertNotIn("github_ca_secret_name", content)

    def test_a_github_com_install_renders_no_ghes_key(self):
        # The goldens in test_installer_gitlab.py pin the whole file; this
        # names the two keys a GitHub Enterprise Server adds.
        for env in ({"GITOPS_ORG": "acme", "GITOPS_REPO": "infra", "GITHUB_APP_ID": "123"},
                    {"GITOPS_ORG": "acme", "GITOPS_REPO": "infra", "GITOPS_CA_SECRET": "stale"}, {}):
            with self.subTest(env=env):
                content = self._tfvars(env)
                self.assertNotIn("gitops_host", content)
                self.assertNotIn("github_ca_secret_name", content)

    def test_upgrade_renders_the_recorded_instance(self):
        # upgrade.sh and uninstall.sh load install.env with load_install_env
        # and render the tfvars from it alone.
        with tempfile.TemporaryDirectory() as out_dir:
            env_file = pathlib.Path(out_dir) / "install.env"
            env_file.write_text(f"GITOPS_FORGE=github\nGITOPS_HOST={_HOST}\nGITOPS_CA_SECRET=github-forge-ca\n"
                                "GITOPS_ORG=platform\nGITOPS_REPO=infra\n")
            dest = pathlib.Path(out_dir) / "terraform.tfvars"
            proc = self._run(
                f'load_install_env "{env_file}"; write_tfvars_from_state "{dest}"; echo "rc=$?"',
                env={"API_SERVER_KEY": "k", "GITOPS_HOST": "shell.example.com"},
                describe_stub=_installer_common._autopilot_describe_stub(),
            )
            self.assertIn("rc=0", proc.stdout, proc.stderr)
            content = dest.read_text()
            self.assertIn(f'gitops_host = "{_HOST}"', content)
            self.assertIn('github_ca_secret_name = "github-forge-ca"', content)
        for script in (_UPGRADE_SH, _UNINSTALL_SH):
            with self.subTest(script=script.name):
                source = script.read_text()
                self.assertIn("load_install_env", source)
                self.assertIn("write_tfvars_from_state", source)


# A curl that records its argv and answers as the instance would.
_CURL_STUB = r"""#!/usr/bin/env bash
printf '%s\n' "$@" > "$STUB_DIR/curl.argv"
printf '{"login": "platform", "type": "Organization"}\n200'
"""


class GhesOrgCheckTest(_Tmp):
    """The organization is looked up on the instance, with its CA, and nothing
    that could authenticate is sent anywhere."""

    def _run_with_curl(self, body):
        stub_dir = self._tmp / "stub"
        stub_dir.mkdir(exist_ok=True)
        bin_dir = stub_dir / "bin"
        bin_dir.mkdir(exist_ok=True)
        curl = bin_dir / "curl"
        curl.write_text(_CURL_STUB)
        curl.chmod(0o755)
        full = f'source "{_INSTALLER_COMMON}"\n{body}'
        proc = subprocess.run(
            ["bash", "-c", full], capture_output=True, text=True, cwd=str(_REPO_ROOT),
            env=get_isolated_test_env(overrides={"STUB_DIR": str(stub_dir)}, bin_dir=bin_dir),
        )
        argv_file = stub_dir / "curl.argv"
        return proc, (argv_file.read_text().split("\n") if argv_file.exists() else [])

    def _assert_no_credential(self, argv):
        joined = "\n".join(argv).lower()
        for marker in ("authorization", "bearer", "token", "--user", "private-key"):
            self.assertNotIn(marker, joined)
        self.assertNotIn("-u", argv)

    def test_ghes_asks_the_instance_with_its_ca(self):
        ca = self._ca()
        proc, argv = self._run_with_curl(f'github_account_type platform {_HOST} "{ca}"')
        self.assertEqual(proc.stdout.strip(), "organization", proc.stderr)
        self.assertIn(f"https://{_HOST}/api/v3/users/platform", argv)
        self.assertNotIn("https://api.github.com/users/platform", argv)
        self.assertEqual(argv[argv.index("--cacert") + 1], str(ca))
        self.assertIn("=https", argv)
        self.assertNotIn("-L", argv)
        self.assertNotIn("--location", argv)
        self._assert_no_credential(argv)

    def test_github_com_asks_api_github_com_with_no_ca(self):
        ca = self._ca()
        proc, argv = self._run_with_curl(f'github_account_type acme "" "{ca}"')
        self.assertEqual(proc.stdout.strip(), "organization", proc.stderr)
        self.assertIn("https://api.github.com/users/acme", argv)
        self.assertNotIn("--cacert", argv)
        self._assert_no_credential(argv)

    def test_the_check_passes_the_host_and_names_it(self):
        ca = self._ca()
        proc, argv = self._run_with_curl(
            f'check_github_org_is_organization platform {_HOST} "{ca}"; echo "rc=$?"'
        )
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        self.assertIn(f"https://{_HOST}/api/v3/users/platform", argv)
        self._assert_no_credential(argv)

    def test_install_sh_checks_on_the_install_s_host(self):
        source = _INSTALL_SH.read_text()
        calls = [line.strip() for line in source.splitlines() if "check_github_org_is_organization " in line]
        self.assertEqual(len(calls), 2, calls)
        for call in calls:
            self.assertEqual(
                call, 'check_github_org_is_organization "${GITOPS_ORG:-}" "${GITOPS_HOST:-}" "$(gitops_ca_file_for_check)"'
            )
        self.assertIn('github_account_type "$github_org" "${PARAM_GITOPS_HOST:-}" "$(gitops_ca_file_for_check)"', source)
        self.assertNotIn("GitHub Enterprise Server is not supported", _INSTALLER_COMMON.read_text())

    def test_a_one_shot_ca_is_not_handed_to_the_check(self):
        ca = self._ca()
        body = (
            f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
            f"PARAM_GITOPS_CA_FILE={shlex.quote(str(ca))}; echo \"file=[$(gitops_ca_file_for_check)]\"\n"
            'PARAM_GITOPS_CA_FILE=/dev/stdin; echo "stdin=[$(gitops_ca_file_for_check)]"\n'
            'PARAM_GITOPS_CA_FILE=; echo "none=[$(gitops_ca_file_for_check)]"\n'
        )
        proc = _run_installer_bash(body, get_isolated_test_env(overrides={
            "KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env)}))
        self.assertIn(f"file=[{ca}]", proc.stdout, proc.stderr)
        self.assertIn("stdin=[]", proc.stdout)
        self.assertIn("none=[]", proc.stdout)


# kubectl as in the GitLab tests, with `get deployment` answering
# STUB_DEPLOYMENT_RC (default: the minter exists).
_KUBECTL_STUB = _gitlab._KUBECTL_STUB.replace(
    'case "$1 $2" in\n',
    'case "$1 $2" in\n  "get deployment") exit "${STUB_DEPLOYMENT_RC:-0}" ;;\n',
    1,
)


class GhesCASecretTest(_Tmp):
    """After the apply: the CA into its Secret, then a minter restart."""

    def setUp(self):
        super().setUp()
        self._stub_dir = self._tmp / "stub"
        self._stub_dir.mkdir()
        bin_dir = self._stub_dir / "bin"
        bin_dir.mkdir()
        k = bin_dir / "kubectl"
        k.write_text(_KUBECTL_STUB)
        k.chmod(0o755)
        self._bin = bin_dir

    def _run_body(self, body, **env):
        overrides = {
            "KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env),
            "STUB_DIR": str(self._stub_dir),
        }
        overrides.update(env)
        return subprocess.run(
            ["bash", "-c", f'source "{_INSTALLER_COMMON}"\nKUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                           f"GITOPS_FORGE=github GITOPS_HOST={_HOST}\n{body}"],
            capture_output=True, text=True, env=get_isolated_test_env(overrides=overrides, bin_dir=self._bin),
            cwd=str(self._tmp), stdin=subprocess.DEVNULL, start_new_session=True, timeout=60,
        )

    def _log(self):
        log = self._stub_dir / "kubectl.log"
        return log.read_text() if log.exists() else ""

    def test_the_ca_becomes_the_secret_and_the_minter_restarts(self):
        ca = self._ca()
        proc = self._run_body(
            f"GITOPS_CA_SECRET=github-forge-ca PARAM_GITOPS_CA_FILE={shlex.quote(str(ca))}\n"
            'create_gitops_ca_secret agents ctx1 && restart_github_minter_for_ca agents ctx1; echo "rc=$?"\n'
        )
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        log = self._log()
        self.assertIn("create secret generic github-forge-ca", log)
        self.assertIn("--from-file=ca.crt=/dev/stdin", log)
        self.assertIn("apply --server-side", log)
        self.assertIn("GitHub Enterprise Server CA stored in Secret 'github-forge-ca'", proc.stdout)
        applied = [f.read_text() for f in self._stub_dir.glob("applied.*.yaml")]
        self.assertEqual(len(applied), 1)
        self.assertIn(f"ca.crt: {base64.b64encode(_PEM.encode()).decode()}", applied[0])
        # The restart comes after the Secret is written.
        self.assertIn("rollout restart deployment/github-token-minter -n agents --context ctx1", log)
        self.assertLess(log.index("create secret generic"), log.index("rollout restart"))

    def test_no_restart_without_a_written_secret_or_a_minter(self):
        proc = self._run_body(
            "GITOPS_CA_SECRET=github-forge-ca\n"
            'create_gitops_ca_secret agents ctx1 && restart_github_minter_for_ca agents ctx1; echo "rc=$?"\n',
            STUB_SECRET_EXISTS_RC="0",
        )
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        self.assertNotIn("rollout restart", self._log())
        ca = self._ca()
        proc = self._run_body(
            f"GITOPS_CA_SECRET=github-forge-ca PARAM_GITOPS_CA_FILE={shlex.quote(str(ca))}\n"
            'create_gitops_ca_secret agents ctx1 && restart_github_minter_for_ca agents ctx1; echo "rc=$?"\n',
            STUB_DEPLOYMENT_RC="1",
        )
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        self.assertNotIn("rollout restart", self._log())

    def test_a_missing_secret_names_both_steps(self):
        proc = self._run_body('GITOPS_CA_SECRET=github-forge-ca\ncreate_gitops_ca_secret agents ctx1; echo "rc=$?"\n')
        out = proc.stdout + proc.stderr
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        self.assertIn("kubectl create secret generic github-forge-ca", out)
        self.assertIn("kubectl rollout restart deployment/github-token-minter -n agents --context ctx1", out)

    def test_a_gitlab_ca_names_no_minter(self):
        proc = self._run_body(
            'GITOPS_FORGE=gitlab GITOPS_CA_SECRET=gitlab-forge-ca\ncreate_gitops_ca_secret agents ctx1; echo "rc=$?"\n'
        )
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        self.assertNotIn("github-token-minter", proc.stdout + proc.stderr)
        self.assertIn("GitLab CA does not exist", proc.stdout + proc.stderr)

    def test_the_post_apply_step_runs_for_ghes(self):
        source = _INSTALL_SH.read_text()
        start = source.index('  elif [ -n "${GITOPS_HOST:-}" ]; then\n    # A GitHub Enterprise Server: the CA')
        block = source[start: start + 600]
        self.assertLess(block.index('create_gitops_ca_secret "$namespace" "$expected_ctx" || exit 1'),
                        block.index('restart_github_minter_for_ca "$namespace" "$expected_ctx"'))
        # After the apply that makes the namespace, before the health checks.
        self.assertLess(source.index('run_lifecycle_apply "$repo_dir" "$provisioning_log"'), start)
        self.assertLess(start, source.index('local slow_rollouts=()'))


class GhesGenerateOnlyHandoffTest(_Tmp):
    """The generate-only handoff names the CA Secret and the minter restart,
    which only a run that applies does itself."""

    def _handoff(self, assignments):
        body = (
            f'source "{_INSTALLER_COMMON}"\nKUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n{assignments}\n'
            f'print_generate_only_handoff "{_REPO_ROOT}" p c us-central1 "{self._tmp}/terraform.tfvars" 2>&1\n'
        )
        return _run_installer_bash(body, get_isolated_test_env(overrides={
            "KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env)}))

    def test_ghes_with_a_ca_prints_both_steps(self):
        proc = self._handoff(f"GITOPS_FORGE=github GITOPS_HOST={_HOST} GITOPS_CA_SECRET=github-forge-ca")
        out = proc.stdout + proc.stderr
        self.assertIn("kubectl create secret generic github-forge-ca -n kubeagents-system", out)
        self.assertIn("kubectl rollout restart deployment/github-token-minter -n kubeagents-system", out)

    def test_github_com_and_gitlab_print_no_minter_step(self):
        for assignments in ("GITOPS_FORGE= GITOPS_HOST= GITOPS_CA_SECRET=",
                            "GITOPS_FORGE=gitlab GITOPS_HOST=gitlab.example.com GITOPS_CA_SECRET=gitlab-forge-ca"):
            with self.subTest(assignments=assignments):
                proc = self._handoff(assignments)
                self.assertNotIn("rollout restart deployment/github-token-minter", proc.stdout + proc.stderr)


class GhesHostMoveTest(_Tmp):
    """A recorded GitHub App and its KMS key are the recorded host's: a move
    of host without the new host's App and a new key is refused."""

    # Borrowed, not inherited: a subclass would run every test above again.
    _run_install_func = GhesFlagsTest._run_install_func

    _GITHUB_COM = "GITOPS_ORG=acme\nGITOPS_REPO=infra\nGITHUB_APP_ID=1\nKMS_KEY=github-token-minter-key\n"
    _GHES = (f"GITOPS_FORGE=github\nGITOPS_HOST={_HOST}\nGITOPS_CA_SECRET=github-forge-ca\nGITOPS_ORG=platform\n"
             "GITOPS_REPO=infra\nGITHUB_APP_ID=12\nKMS_KEY=ghes-key\n")

    def _move(self, recorded, *flags):
        return self._run_install_func(
            f"parse_args {' '.join(shlex.quote(f) for f in flags)}\n"
            "validate_gitops_forge_flags && rc=0 || rc=$?\n"
            'echo "rc=$rc host=[$PARAM_GITOPS_HOST]"',
            install_env=recorded,
        )

    def test_a_move_that_keeps_the_recorded_app_is_refused(self):
        for recorded, flags in (
            (self._GITHUB_COM, (f"--gitops-host={_HOST}", "--gitops-org=platform")),
            (self._GHES, ("--gitops-host=github.com",)),
            (self._GHES, ("--gitops-host=ghe2.acme.corp",)),
        ):
            with self.subTest(flags=flags):
                proc = self._move(recorded, *flags)
                self.assertRegex(proc.stdout, r"rc=[1-9]", proc.stderr + proc.stdout)
                self.assertIn("an App belongs to one GitHub host", proc.stdout + proc.stderr)

    def test_a_move_with_a_new_app_and_the_old_key_is_refused(self):
        proc = self._move(self._GITHUB_COM, f"--gitops-host={_HOST}", "--gitops-org=platform",
                          "--github-app-id=12", "--github-pem-path=/x/app.pem")
        self.assertRegex(proc.stdout, r"rc=[1-9]", proc.stderr + proc.stdout)
        self.assertIn("--kms-key", proc.stdout + proc.stderr)

    def test_a_move_with_a_pem_alone_or_the_recorded_app_id_is_refused(self):
        # --github-pem-path alone keeps the recorded App ID, and the recorded
        # ID given again is the old App: neither is the new host's App.
        for flags in (
            ("--github-pem-path=/x/app.pem", "--kms-key=ghes-key"),
            ("--github-app-id=1", "--github-pem-path=/x/app.pem", "--kms-key=ghes-key"),
        ):
            with self.subTest(flags=flags):
                proc = self._move(self._GITHUB_COM, f"--gitops-host={_HOST}", "--gitops-org=platform", *flags)
                self.assertRegex(proc.stdout, r"rc=[1-9]", proc.stderr + proc.stdout)
                self.assertIn("not the recorded one", proc.stdout + proc.stderr)

    def test_a_move_with_a_new_app_and_a_new_key_is_accepted(self):
        proc = self._move(self._GITHUB_COM, f"--gitops-host={_HOST}", "--gitops-org=platform",
                          "--github-app-id=12", "--github-pem-path=/x/app.pem", "--kms-key=ghes-key")
        self.assertIn(f"rc=0 host=[{_HOST}]", proc.stdout, proc.stderr + proc.stdout)

    def test_the_recorded_host_keeps_its_app(self):
        for recorded in (self._GITHUB_COM, self._GHES):
            with self.subTest(recorded=recorded):
                proc = self._move(recorded)
                self.assertIn("rc=0", proc.stdout, proc.stderr + proc.stdout)

    def test_a_fresh_install_with_an_app_in_the_shell_is_not_a_move(self):
        # An install.env that records no App: an exported GITHUB_APP_ID is
        # this run's, not a recorded one.
        body = (
            f'source "{_INSTALLER_COMMON}"\nKUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
            f"parse_args --gitops-host={_HOST} --gitops-org=platform\n"
            'validate_gitops_forge_flags && rc=0 || rc=$?; echo "rc=$rc app=$PARAM_GITHUB_APP_ID"'
        )
        env = get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env),
                                               "GITHUB_APP_ID": "77"})
        proc = _run_installer_bash(body, env)
        self.assertIn("rc=0 app=77", proc.stdout, proc.stderr + proc.stdout)


class GhesForgeQuestionTest(_Tmp):
    """The forge question: each choice, and a bad host asked again."""

    def _ask(self, choice, hosts, ca_answers=None, extra=""):
        answers = " ".join(shlex.quote(h) for h in hosts)
        cas = " ".join(shlex.quote(c) for c in (ca_answers or [""] * len(hosts)))
        body = (
            f'source "{_INSTALLER_COMMON}"\nKUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
            f"HOSTS=({answers}); CAS=({cas}); n=0; c=0\n"
            f'prompt_menu() {{ local var="${{!#}}"; printf -v "$var" %s {shlex.quote(choice)}; echo "menu: $2|$3|$4"; }}\n'
            "prompt_read() {\n"
            '  case "$1" in\n'
            '    *hostname*) printf -v "$2" %s "${HOSTS[$n]}"; n=$((n+1)) ;;\n'
            '    *CA*) printf -v "$2" %s "${CAS[$c]}"; c=$((c+1)) ;;\n'
            "  esac\n"
            "}\n"
            "has_controlling_tty() { return 0; }\n"
            f"PARAM_NON_INTERACTIVE=false PARAM_GITOPS_FORGE_GIVEN=false PARAM_GITOPS_FORGE=github {extra}\n"
            "ask_gitops_forge\n"
            'echo "forge=$PARAM_GITOPS_FORGE host=[$PARAM_GITOPS_HOST] ca=[$PARAM_GITOPS_CA_FILE] asked=$n"\n'
        )
        return _run_installer_bash(body, get_isolated_test_env(overrides={
            "KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env)}))

    def test_the_menu_offers_three_forges(self):
        proc = self._ask("1", [])
        self.assertIn("menu: GitHub (github.com)|GitHub Enterprise Server (a GitHub host that you run)|"
                      "GitLab (gitlab.com or self-managed)", proc.stdout, proc.stderr)

    def test_github_com(self):
        proc = self._ask("1", [], extra=f"PARAM_GITOPS_HOST={_HOST}")
        self.assertIn("forge=github host=[] ca=[] asked=0", proc.stdout, proc.stderr)

    def test_ghes_with_a_ca(self):
        ca = self._ca()
        proc = self._ask("2", [_HOST], [str(ca)])
        self.assertIn(f"forge=github host=[{_HOST}] ca=[{ca}] asked=1", proc.stdout, proc.stderr + proc.stdout)

    def test_ghes_asks_a_bad_host_again(self):
        proc = self._ask("2", ["acme.ghe.com", _HOST])
        self.assertIn(f"forge=github host=[{_HOST}] ca=[] asked=2", proc.stdout, proc.stderr + proc.stdout)
        self.assertIn("a host that GitHub operates", proc.stdout + proc.stderr)

    def test_gitlab(self):
        proc = self._ask("3", [])
        self.assertIn("forge=gitlab", proc.stdout, proc.stderr)
        self.assertIn("asked=0", proc.stdout)


class GhesSourcingTest(unittest.TestCase):
    """The helpers source cleanly: a constant declared readonly twice prints
    "readonly variable" and fails under set -e, which stops every front door."""

    def test_installer_common_and_install_sh_source_under_set_e(self):
        body = (
            f'set -e\nsource "{_INSTALLER_COMMON}"\nKUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
            'echo "minter=$GITHUB_MINTER_DEPLOYMENT"\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            env_file = pathlib.Path(tmp) / "install.env"
            env_file.write_text("")
            proc = subprocess.run(["bash", "-c", body], capture_output=True, text=True, cwd=str(_REPO_ROOT),
                                  env=get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": str(env_file)}),
                                  stdin=subprocess.DEVNULL, timeout=60)
        self.assertNotIn("readonly variable", proc.stderr)
        self.assertIn("minter=github-token-minter", proc.stdout, proc.stderr)
        self.assertEqual(_INSTALLER_COMMON.read_text().count("GITHUB_MINTER_DEPLOYMENT="), 1)


class GhesInterviewAndHelpTest(unittest.TestCase):

    def test_help_describes_the_ghes_flags(self):
        proc = subprocess.run(["bash", str(_INSTALL_SH), "--help"], capture_output=True, text=True,
                              cwd=str(_REPO_ROOT), env=get_isolated_test_env(), timeout=60)
        out = proc.stdout
        self.assertIn("GitHub Enterprise Server", out)
        self.assertIn("github-forge-ca", out)
        self.assertNotIn("--gitops-host=HOST            Self-managed GitLab hostname (default: gitlab.com)", out)


if __name__ == "__main__":
    unittest.main()
