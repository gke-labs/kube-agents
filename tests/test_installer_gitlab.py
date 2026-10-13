"""GitLab as the installer's GitOps forge.

A GitLab install names a project path and a Kubernetes Secret holding an
access token. The installer takes the token from a no-echo prompt or a file
and pipes it into that Secret; it is never in argv, an exported variable, a
file the installer writes, its output, the tfvars or the Terraform state. The
sentinel tests below hold it to that by planting a recognisable token and
looking for it everywhere a stubbed kubectl and the installer can leave it.

A GitHub install is unchanged: no forge keys in its tfvars or install.env.
"""

import base64
import os
import pathlib
import pty
import re
import select
import shlex
import subprocess
import tempfile
import time
import unittest

from tests.test_install_script import (
    _INSTALL_SH,
    _INSTALLER_COMMON,
    _REPO_ROOT,
    _run_installer_bash,
)
# The module, not the class: a TestCase imported by name is collected here too.
from tests import test_installer_common as _installer_common
from tests.testing.common import get_isolated_test_env

_SENTINEL = "glpat-SENTINEL-7d1f0c9e4b2a"
_FULL_INSTALL = _REPO_ROOT / "terraform" / "examples" / "full-install"
_GOLDEN_DIR = pathlib.Path(__file__).resolve().parent / "testdata" / "gitlab_forge"

# A kubectl that records every call's argv and environment, answers `get
# secret` as absent (or present, with STUB_SECRET_EXISTS_RC=0), renders
# `create secret` from the --from-file source the way kubectl does (base64,
# bytes as given), and keeps what `apply -f -` was sent. With
# STUB_APPLY_FAIL=1 the apply fails the way a rejected patch does: it echoes
# the object it was sent to stderr.
_KUBECTL_STUB = r"""#!/usr/bin/env bash
log="$STUB_DIR/kubectl.log"
# $$, not a count: the two halves of a pipeline run at once.
n=$$
printf '%s\n' "$@" > "$STUB_DIR/call.$n.argv"
env > "$STUB_DIR/call.$n.env"
echo "kubectl $*" >> "$log"
case "$1 $2" in
  "get secret") exit "${STUB_SECRET_EXISTS_RC:-1}" ;;
  "create secret")
    src=""
    for a in "$@"; do case "$a" in --from-file=token=*) src="${a#--from-file=token=}" ;; esac; done
    printf 'apiVersion: v1\nkind: Secret\ndata:\n  token: %s\n' "$(base64 < "$src" | tr -d '\n')"
    exit 0
    ;;
esac
if [ "$1" = "apply" ]; then
  if [ "${STUB_APPLY_FAIL:-}" = "1" ]; then
    echo "The Secret is invalid:" >&2
    cat >&2
    exit 7
  fi
  cat > "$STUB_DIR/applied.$n.yaml"
fi
exit 0
"""


def _write_stub(stub_dir):
    bin_dir = stub_dir / "bin"
    bin_dir.mkdir()
    k = bin_dir / "kubectl"
    k.write_text(_KUBECTL_STUB)
    k.chmod(0o755)
    return bin_dir


class GitLabTfvarsTest(unittest.TestCase):
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

    def test_gitlab_renders_the_forge_and_no_minter(self):
        content = self._tfvars(
            {
                "GITOPS_FORGE": "gitlab",
                "GITOPS_HOST": "gitlab.example.com",
                "GITOPS_REPO": "platform/infra/gitops",
                "GITLAB_TOKEN_SECRET": "gl-token",
                # Left over from a GitHub install: must not arm the minter.
                "GITOPS_ORG": "acme",
                "GITHUB_APP_ID": "123",
            }
        )
        self.assertIn('gitops_forge              = "gitlab"', content)
        self.assertIn('gitops_host               = "gitlab.example.com"', content)
        self.assertIn('gitlab_repo               = "platform/infra/gitops"', content)
        self.assertIn('gitlab_token_secret_name  = "gl-token"', content)
        self.assertNotIn("github_repo", content)
        self.assertRegex(content, r"enable_github_minter\s*=\s*false")

    def test_gitlab_secret_name_defaults(self):
        content = self._tfvars({"GITOPS_FORGE": "gitlab", "GITOPS_REPO": "g/p"})
        self.assertIn('gitlab_token_secret_name  = "gitlab-forge-token"', content)
        self.assertIn('gitops_host               = ""', content)

    # GitHub installs this change must leave byte-identical. The goldens were
    # rendered by this same harness at the commit before GitLab support
    # (0347c610), with GITLAB_GOLDEN_WRITE pointing at the testdata directory.
    _GOLDEN_CASES = {
        "github_minter": {"GITOPS_ORG": "acme", "GITOPS_REPO": "infra", "GITHUB_APP_ID": "123"},
        "github_repo_only": {"GITOPS_ORG": "acme", "GITOPS_REPO": "infra"},
        "github_none": {},
    }

    def test_github_tfvars_match_the_pre_gitlab_rendering(self):
        write_dir = os.environ.get("GITLAB_GOLDEN_WRITE")
        for name, env in self._GOLDEN_CASES.items():
            with self.subTest(case=name):
                content = self._tfvars(env)
                golden = _GOLDEN_DIR / f"{name}.tfvars.golden"
                if write_dir:
                    (pathlib.Path(write_dir) / f"{name}.tfvars.golden").write_text(content)
                    continue
                self.assertEqual(content, golden.read_text(), name)

    def test_github_tfvars_carry_no_forge_keys(self):
        github = {"GITOPS_ORG": "acme", "GITOPS_REPO": "infra", "GITHUB_APP_ID": "123"}
        unset = self._tfvars(github)
        explicit = self._tfvars({**github, "GITOPS_FORGE": "github"})
        self.assertEqual(unset, explicit)
        self.assertIn('github_repo = "acme/infra"', unset)
        for key in ("gitops_forge", "gitops_host", "gitlab_repo", "gitlab_token_secret_name"):
            self.assertNotIn(key, unset)


class GitLabFlagsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._tmp = pathlib.Path(tmp.name)
        self._empty_install_env = self._tmp / "install.env"
        self._empty_install_env.write_text("")

    def _run_install_func(self, func_call, env=None):
        # installer_common.sh is sourced by main before any of these run.
        setup = f'source "{_INSTALLER_COMMON}"\nKUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n{func_call}\n'
        overrides = {"KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env)}
        overrides.update(env or {})
        return _run_installer_bash(setup, get_isolated_test_env(overrides=overrides))

    def test_parse_args_takes_the_four_flags(self):
        proc = self._run_install_func(
            "parse_args --gitops-forge=gitlab --gitops-host=git.corp.example --gitlab-token-file=/x/tok "
            "--gitlab-token-secret=s1; "
            'echo "f=$PARAM_GITOPS_FORGE h=$PARAM_GITOPS_HOST t=$PARAM_GITLAB_TOKEN_FILE s=$PARAM_GITLAB_TOKEN_SECRET"'
        )
        self.assertIn("f=gitlab h=git.corp.example t=/x/tok s=s1", proc.stdout, proc.stderr)

    def test_there_is_no_token_value_flag(self):
        source = _INSTALL_SH.read_text()
        self.assertNotRegex(source, r"--gitlab-token=")
        self.assertNotIn("PARAM_GITLAB_TOKEN=", source)
        self.assertNotRegex(source, r"\bGITLAB_TOKEN=")

    def _validate(self, assignments):
        body = "".join(f"{k}={shlex.quote(v)}; " for k, v in assignments.items())
        return self._run_install_func(
            f'{body}validate_gitops_forge_flags && rc=0 || rc=$?; echo "rc=$rc repo=$PARAM_GITOPS_REPO"'
        )

    def _gitlab(self, **extra):
        base = {
            "PARAM_GITOPS_FORGE": "gitlab",
            "PARAM_GITOPS_REPO": "group/sub/project",
            "PARAM_GITLAB_TOKEN_SECRET": "gitlab-forge-token",
            "PARAM_GITHUB_APP_ID": "",
            "PARAM_GITHUB_PEM_PATH": "",
        }
        base.update(extra)
        return base

    def test_validator_accepts(self):
        tok = self._tmp / "tok"
        tok.write_text("x")
        for case, want_repo in (
            (self._gitlab(), "group/sub/project"),
            (self._gitlab(PARAM_GITOPS_HOST="gitlab.corp.example"), "group/sub/project"),
            (self._gitlab(PARAM_GITOPS_REPO="https://gitlab.com/a/b.git"), "a/b"),
            (self._gitlab(PARAM_GITOPS_REPO="a/b/"), "a/b"),
            (self._gitlab(PARAM_GITLAB_TOKEN_FILE=str(tok)), "group/sub/project"),
            ({"PARAM_GITOPS_FORGE": "github", "PARAM_GITOPS_REPO": "infra"}, "infra"),
            # The operator's segment grammar (gitprovider.go) admits these.
            (self._gitlab(PARAM_GITOPS_REPO="_grp/my.proj"), "_grp/my.proj"),
            (self._gitlab(PARAM_GITOPS_REPO="grp-/proj-"), "grp-/proj-"),
            (self._gitlab(PARAM_GITOPS_REPO="my.group/proj"), "my.group/proj"),
            (self._gitlab(PARAM_GITLAB_TOKEN_FILE="/dev/stdin"), "group/sub/project"),
        ):
            with self.subTest(case=case):
                proc = self._validate(case)
                self.assertIn(f"rc=0 repo={want_repo}", proc.stdout, proc.stderr + proc.stdout)

    def test_validator_refuses(self):
        for case in (
            {"PARAM_GITOPS_FORGE": "bitbucket"},
            {"PARAM_GITOPS_FORGE": "github", "PARAM_GITOPS_HOST": "gitlab.com", "PARAM_GITOPS_HOST_FROM_FLAG": "true"},
            {"PARAM_GITOPS_FORGE": "github", "PARAM_GITLAB_TOKEN_FILE": "/etc/hostname"},
            self._gitlab(PARAM_GITOPS_HOST="https://gitlab.com"),
            self._gitlab(PARAM_GITOPS_HOST="gitlab.com:8443"),
            self._gitlab(PARAM_GITOPS_HOST="GitLab.com"),
            self._gitlab(PARAM_GITOPS_HOST="github.com"),
            self._gitlab(PARAM_GITOPS_HOST="api.github.com"),
            self._gitlab(PARAM_GITOPS_REPO="project"),
            self._gitlab(PARAM_GITOPS_REPO="a/../b"),
            self._gitlab(PARAM_GITOPS_REPO="a/b c"),
            self._gitlab(PARAM_GITOPS_REPO="a/*"),
            self._gitlab(PARAM_GITOPS_REPO="git@gitlab.com:a/b.git"),
            self._gitlab(PARAM_GITOPS_REPO="https://evil.example/a/b"),
            self._gitlab(PARAM_GITHUB_APP_ID="123", PARAM_GITHUB_APP_FROM_FLAG="true"),
            self._gitlab(PARAM_GITOPS_REPO="g/p\nX"),
            self._gitlab(PARAM_GITOPS_REPO="g/p x"),
            self._gitlab(PARAM_GITOPS_REPO="Platform.GIT/infra"),
            self._gitlab(PARAM_GITOPS_REPO="grp.Atom/proj"),
            self._gitlab(PARAM_GITOPS_REPO="grp/proj.ATOM"),
            self._gitlab(PARAM_GITLAB_TOKEN_SECRET="Bad_Name"),
            self._gitlab(PARAM_GITLAB_TOKEN_FILE=str(self._tmp / "missing")),
            self._gitlab(PARAM_GITOPS_REPO="", PARAM_NON_INTERACTIVE="true"),
            self._gitlab(PARAM_GITOPS_REPO=".grp/proj"),
            self._gitlab(PARAM_GITOPS_REPO="grp./proj"),
            self._gitlab(PARAM_GITOPS_REPO="grp/proj."),
            self._gitlab(PARAM_GITOPS_REPO="grp/proj.atom"),
            self._gitlab(PARAM_GITOPS_REPO="grp.git/proj"),
            self._gitlab(PARAM_GITOPS_REPO="gitlab.com/grp/proj"),
            self._gitlab(PARAM_GITOPS_REPO="GitLab.com/grp/proj"),
            self._gitlab(PARAM_GITOPS_REPO="github.com/grp/proj"),
            self._gitlab(PARAM_GITOPS_HOST="git.corp.example", PARAM_GITOPS_REPO="git.corp.example/grp/proj"),
            self._gitlab(PARAM_GITOPS_REPO="g/" + "p" * 240),
            self._gitlab(PARAM_GITLAB_TOKEN_FILE=str(self._tmp)),
        ):
            with self.subTest(case=case):
                proc = self._validate(case)
                self.assertRegex(proc.stdout, r"rc=[1-9]", proc.stderr + proc.stdout)

    def test_install_env_records_the_secret_name_only(self):
        tok = self._tmp / "tok"
        tok.write_text(_SENTINEL)
        dest = self._tmp / "out.env"
        body = (
            f'source "{_INSTALLER_COMMON}"\n'
            f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
            'PARAM_DRY_RUN="false"\n'
            "export GITOPS_FORGE=gitlab GITOPS_HOST=gitlab.corp.example GITOPS_REPO=g/p GITLAB_TOKEN_SECRET=gl-tok\n"
            f"PARAM_GITLAB_TOKEN_FILE={shlex.quote(str(tok))}\n"
            f'bootstrap_install_env_file "{dest}" 0.5.0\n'
        )
        proc = subprocess.run(
            ["bash", "-c", body],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env)}),
            cwd=str(_REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        text = dest.read_text()
        self.assertIn("GITOPS_FORGE=gitlab", text)
        self.assertIn("GITOPS_HOST=gitlab.corp.example", text)
        self.assertIn("GITLAB_TOKEN_SECRET=gl-tok", text)
        self.assertNotIn(_SENTINEL, text)
        self.assertNotIn(str(tok), text)
        self.assertNotIn("TOKEN_FILE", text)

    def test_install_env_of_a_github_install_has_no_forge_keys(self):
        dest = self._tmp / "out.env"
        body = (
            f'source "{_INSTALLER_COMMON}"\n'
            f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
            'PARAM_DRY_RUN="false"\n'
            "export GITOPS_ORG=acme GITOPS_REPO=infra\n"
            f'bootstrap_install_env_file "{dest}" 0.5.0\n'
        )
        proc = subprocess.run(
            ["bash", "-c", body],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env)}),
            cwd=str(_REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        for key in ("GITOPS_FORGE", "GITOPS_HOST", "GITLAB_TOKEN_SECRET"):
            self.assertNotIn(key, dest.read_text())


class GitLabTokenNeverLeaksTest(unittest.TestCase):
    """The sentinel token reaches the Secret and nowhere else."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._tmp = pathlib.Path(tmp.name)
        self._stub_dir = self._tmp / "stub"
        self._stub_dir.mkdir()
        self._bin = _write_stub(self._stub_dir)
        self._work = self._tmp / "work"
        self._work.mkdir()
        (self._work / "install.env").write_text("")

    def _env(self, **extra):
        overrides = {
            "KUBE_AGENTS_INSTALL_ENV": str(self._work / "install.env"),
            "STUB_DIR": str(self._stub_dir),
            "GITOPS_FORGE": "gitlab",
            "GITLAB_TOKEN_SECRET": "gitlab-forge-token",
        }
        overrides.update(extra)
        return get_isolated_test_env(overrides=overrides, bin_dir=self._bin)

    def _assert_only_in_the_secret(self, *outputs):
        encoded = base64.b64encode(_SENTINEL.encode()).decode()
        applied = list(self._stub_dir.glob("applied.*.yaml"))
        self.assertEqual(len(applied), 1, (self._stub_dir / "kubectl.log").read_text())
        self.assertIn(f"token: {encoded}", applied[0].read_text())
        for f in self._stub_dir.glob("call.*"):
            text = f.read_text()
            self.assertNotIn(_SENTINEL, text, f.name)
            self.assertNotIn(encoded, text, f.name)
        for out in outputs:
            self.assertNotIn(_SENTINEL, out)
            self.assertNotIn(encoded, out)
        for f in self._work.rglob("*"):
            if f.is_file():
                self.assertNotIn(_SENTINEL, f.read_text(errors="replace"), str(f))

    def _run_body(self, body, **env):
        return subprocess.run(
            ["bash", "-c", f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n{body}'],
            capture_output=True, text=True, env=self._env(**env),
            cwd=str(self._work), stdin=subprocess.DEVNULL, start_new_session=True, timeout=60,
        )

    def _run_on_pty(self, body, script, **env):
        """Run body on a pty, answering each (trigger, reply) in script in turn."""
        full = f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n{body}'
        environ = self._env(**env)
        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - child
            try:
                os.chdir(str(self._work))
                os.execvpe("bash", ["bash", "-c", full], environ)
            finally:
                os._exit(127)
        out, pending, seen = b"", list(script), 0
        deadline = time.time() + 60
        while time.time() < deadline:
            r, _, _ = select.select([fd], [], [], 0.5)
            if not r:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            if pending and pending[0][0] in out[seen:]:
                seen = out.index(pending[0][0], seen) + len(pending[0][0])
                time.sleep(0.2)
                os.write(fd, pending.pop(0)[1])
        else:
            # Out of time with the child still parked on a prompt: kill it, or
            # waitpid would hang the suite instead of failing the test.
            os.kill(pid, 9)
        os.waitpid(pid, 0)
        os.close(fd)
        return out.decode(errors="replace"), pending

    def _apply_calls(self):
        calls = []
        for f in self._stub_dir.glob("call.*.argv"):
            argv = f.read_text().split("\n")
            if argv and argv[0] == "apply":
                calls.append(argv)
        return calls

    def _assert_server_side(self):
        calls = self._apply_calls()
        self.assertTrue(calls, "no apply")
        for argv in calls:
            # A client-side apply copies the token into the
            # last-applied-configuration annotation.
            self.assertIn("--server-side", argv)

    def test_token_file(self):
        tok = self._tmp / "token-file"
        tok.write_text(_SENTINEL + "\r\n")  # what an editor or a paste leaves
        proc = self._run_body(
            f"PARAM_NON_INTERACTIVE=true PARAM_GITLAB_TOKEN_FILE={shlex.quote(str(tok))}\n"
            'create_gitlab_token_secret agents ctx1; echo "rc=$?"\n'
        )
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        self.assertIn("--from-file=token=/dev/stdin", (self._stub_dir / "kubectl.log").read_text())
        self.assertNotIn(str(tok), (self._stub_dir / "kubectl.log").read_text())
        self._assert_server_side()
        self._assert_only_in_the_secret(proc.stdout, proc.stderr)

    def test_process_substitution_source(self):
        tok = self._tmp / "token-file"
        tok.write_text(_SENTINEL)
        proc = self._run_body(
            "PARAM_NON_INTERACTIVE=true PARAM_GITOPS_FORGE=gitlab PARAM_GITOPS_REPO=g/p\n"
            # An fd held open the way `install.sh --gitlab-token-file=<(...)`
            # holds it for the process's life; a bare assignment would close it.
            f"exec 9< <(cat {shlex.quote(str(tok))})\nPARAM_GITLAB_TOKEN_FILE=/dev/fd/9\n"
            "validate_gitops_forge_flags && v=0 || v=$?\n"
            'create_gitlab_token_secret agents ctx1; echo "v=$v rc=$?"\n'
        )
        self.assertIn("v=0 rc=0", proc.stdout, proc.stderr)
        self._assert_only_in_the_secret(proc.stdout, proc.stderr)

    def test_empty_token_is_refused_and_nothing_is_stored(self):
        tok = self._tmp / "token-file"
        tok.write_text(" \n\t\n")
        proc = self._run_body(
            f"PARAM_NON_INTERACTIVE=true PARAM_GITLAB_TOKEN_FILE={shlex.quote(str(tok))}\n"
            'create_gitlab_token_secret agents ctx1 && rc=0 || rc=$?; echo "rc=$rc"\n'
        )
        self.assertIn("rc=1", proc.stdout, proc.stderr)
        self.assertNotIn("stored in Secret", proc.stdout)
        self.assertFalse(self._apply_calls())

    def test_a_failed_apply_prints_no_kubectl_text(self):
        tok = self._tmp / "token-file"
        tok.write_text(_SENTINEL)
        proc = self._run_body(
            f"PARAM_NON_INTERACTIVE=true PARAM_GITLAB_TOKEN_FILE={shlex.quote(str(tok))}\n"
            'create_gitlab_token_secret agents ctx1 && rc=0 || rc=$?; echo "rc=$rc"\n',
            STUB_APPLY_FAIL="1",
        )
        encoded = base64.b64encode(_SENTINEL.encode()).decode()
        self.assertIn("rc=1", proc.stdout, proc.stderr)
        self.assertIn("apply 7", proc.stdout + proc.stderr)
        self.assertNotIn("stored in Secret", proc.stdout)
        for out in (proc.stdout, proc.stderr):
            self.assertNotIn(_SENTINEL, out)
            self.assertNotIn(encoded, out)
            self.assertNotIn("The Secret is invalid", out)

    def test_non_interactive_without_a_file_writes_nothing_and_says_how(self):
        proc = self._run_body('PARAM_NON_INTERACTIVE=true\ncreate_gitlab_token_secret agents ctx1; echo "rc=$?"\n')
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        self.assertIn("kubectl create secret generic gitlab-forge-token -n agents", proc.stdout)
        self.assertIn("--server-side", proc.stdout)
        self.assertFalse(self._apply_calls())

    def test_non_interactive_keeps_an_existing_secret(self):
        proc = self._run_body(
            'PARAM_NON_INTERACTIVE=true\ncreate_gitlab_token_secret agents ctx1; echo "rc=$?"\n',
            STUB_SECRET_EXISTS_RC="0",
        )
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        self.assertIn("Keeping the existing GitLab token Secret", proc.stdout)
        self.assertFalse(self._apply_calls())

    def test_a_token_file_replaces_an_existing_secret(self):
        tok = self._tmp / "token-file"
        tok.write_text(_SENTINEL)
        proc = self._run_body(
            f"PARAM_NON_INTERACTIVE=true PARAM_GITLAB_TOKEN_FILE={shlex.quote(str(tok))}\n"
            'create_gitlab_token_secret agents ctx1; echo "rc=$?"\n',
            STUB_SECRET_EXISTS_RC="0",
        )
        self.assertIn("rc=0", proc.stdout, proc.stderr)
        self._assert_only_in_the_secret(proc.stdout, proc.stderr)

    def test_interactive_keep_writes_nothing(self):
        text, pending = self._run_on_pty(
            'PARAM_NON_INTERACTIVE=false\ncreate_gitlab_token_secret agents ctx1; echo "rc=$?"\n',
            [(b"already exists", b"1\n")],
            STUB_SECRET_EXISTS_RC="0",
        )
        self.assertFalse(pending, text)
        self.assertIn("rc=0", text)
        self.assertIn("Keeping the existing GitLab token Secret", text)
        self.assertNotIn("Paste the GitLab access token", text)
        self.assertFalse(self._apply_calls())

    def test_interactive_replace_takes_a_new_token(self):
        text, pending = self._run_on_pty(
            'PARAM_NON_INTERACTIVE=false\ncreate_gitlab_token_secret agents ctx1; echo "rc=$?"\n',
            [(b"already exists", b"2\n"), (b"Paste the GitLab access token", (_SENTINEL + "\n").encode())],
            STUB_SECRET_EXISTS_RC="0",
        )
        self.assertFalse(pending, text)
        self.assertIn("rc=0", text)
        self._assert_server_side()
        self._assert_only_in_the_secret(text)

    def test_interactive_empty_paste_stores_nothing(self):
        text, pending = self._run_on_pty(
            'PARAM_NON_INTERACTIVE=false\ncreate_gitlab_token_secret agents ctx1; echo "rc=$?"\n',
            [(b"Paste the GitLab access token", b"   \n")],
        )
        self.assertFalse(pending, text)
        self.assertNotIn("stored in Secret", text)
        self.assertFalse(self._apply_calls())

    def test_prompt_is_not_echoed_and_reaches_only_the_secret(self):
        text, pending = self._run_on_pty(
            "PARAM_NON_INTERACTIVE=false\n"
            'create_gitlab_token_secret agents ctx1; echo "rc=$?"\n'
            # Anything the function exported, or left set, would show here.
            'env > "$STUB_DIR/after.env"; set > "$STUB_DIR/after.set"\n',
            [(b"Paste the GitLab access token", (_SENTINEL + "\n").encode())],
        )
        self.assertFalse(pending, text)
        self.assertIn("rc=0", text)
        self.assertIn("--from-file=token=/dev/stdin", (self._stub_dir / "kubectl.log").read_text())
        after = (self._stub_dir / "after.env").read_text() + (self._stub_dir / "after.set").read_text()
        self.assertNotIn(_SENTINEL, after)
        (self._stub_dir / "after.env").unlink()
        (self._stub_dir / "after.set").unlink()
        self._assert_server_side()
        self._assert_only_in_the_secret(text)


class GitLabThroughMainTest(unittest.TestCase):
    """main() judges the forge flags before any work, and never echoes the token."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._tmp = pathlib.Path(tmp.name)
        self._install_env = self._tmp / "install.env"
        self._install_env.write_text("")
        self._tok = self._tmp / "tok"
        self._tok.write_text(_SENTINEL)

    def _main(self, *args):
        body = (
            f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
            f"main {' '.join(shlex.quote(a) for a in args)} && rc=0 || rc=$?\n"
            'echo "rc=$rc"\n'
        )
        env = get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": str(self._install_env)})
        return _run_installer_bash(body, env)

    def test_a_github_app_with_gitlab_is_refused_before_step_one(self):
        proc = self._main(
            "-y", "--image-tag=0.1.0", "--gitops-forge=gitlab", "--gitops-repo=g/p",
            "--github-app-id=1", f"--gitlab-token-file={self._tok}",
        )
        out = proc.stdout + proc.stderr
        self.assertNotIn("rc=0", proc.stdout, out)
        self.assertIn("--github-app-id and --github-pem-path configure the GitHub token minter", out)
        self.assertNotIn("1. Checking Prerequisites", out)
        self.assertNotIn(_SENTINEL, out)
        self.assertNotIn(_SENTINEL, self._install_env.read_text())

    def test_a_host_prefixed_path_is_refused_before_step_one(self):
        proc = self._main("-y", "--image-tag=0.1.0", "--gitops-forge=gitlab", "--gitops-repo=gitlab.com/g/p")
        out = proc.stdout + proc.stderr
        self.assertIn("starts with a host", out)
        self.assertNotIn("1. Checking Prerequisites", out)


class GitLabInstallEnvTest(unittest.TestCase):
    """The forge keys reach an install.env that already exists, and only from the file."""

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
            env=get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": str(self._loaded)}),
        )

    def test_switching_an_existing_github_install_to_gitlab_records_it(self):
        dest = self._tmp / "install.env"
        dest.write_text("PROJECT_ID=p\nGITOPS_ORG=acme\nGITOPS_REPO=infra\nGITHUB_APP_ID=123\nMEMORY=file\n")
        proc = self._bash(
            "export GITOPS_FORGE=gitlab GITOPS_HOST=gitlab.corp.example GITOPS_REPO=g/sub/p GITLAB_TOKEN_SECRET=gl GITOPS_ORG= GITHUB_APP_ID=\n"
            f'record_gitops_forge_keys "{dest}"\n'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        text = dest.read_text()
        for line in ("PROJECT_ID=p", "MEMORY=file", "GITOPS_FORGE=gitlab", "GITOPS_HOST=gitlab.corp.example",
                     "GITLAB_TOKEN_SECRET=gl", "GITOPS_REPO=g/sub/p"):
            self.assertIn(line + "\n", text)
        self.assertNotIn("GITHUB_APP_ID", text)
        self.assertNotIn("GITOPS_ORG", text)
        self.assertEqual(oct(dest.stat().st_mode & 0o777), "0o600")
        # And the next upgrade, reading only the file, renders GitLab.
        proc = self._bash(
            "unset GITOPS_FORGE GITOPS_HOST GITOPS_REPO GITLAB_TOKEN_SECRET\n"
            f'load_install_env "{dest}"; echo "forge=$GITOPS_FORGE repo=$GITOPS_REPO"\n'
        )
        self.assertIn("forge=gitlab repo=g/sub/p", proc.stdout, proc.stderr)

    def test_switching_back_to_github_drops_the_forge_keys(self):
        dest = self._tmp / "install.env"
        dest.write_text("PROJECT_ID=p\nGITOPS_FORGE=gitlab\nGITOPS_HOST=\nGITLAB_TOKEN_SECRET=gl\nGITOPS_REPO=g/p\n")
        proc = self._bash(
            "unset GITOPS_FORGE GITOPS_HOST GITLAB_TOKEN_SECRET; export GITOPS_ORG=acme GITOPS_REPO=infra GITHUB_APP_ID=9\n"
            f'record_gitops_forge_keys "{dest}"\n'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        text = dest.read_text()
        for key in ("GITOPS_FORGE", "GITOPS_HOST", "GITLAB_TOKEN_SECRET"):
            self.assertNotIn(key, text)
        for line in ("GITOPS_ORG=acme", "GITOPS_REPO=infra", "GITHUB_APP_ID=9", "PROJECT_ID=p"):
            self.assertIn(line + "\n", text)

    def test_a_previewed_or_declined_switch_records_nothing(self):
        # bootstrap runs at step 10, before the confirmation and the
        # --generate-only exit; only the post-confirmation call may write.
        dest = self._tmp / "install.env"
        original = "PROJECT_ID=p\nGITOPS_ORG=acme\nGITOPS_REPO=infra\n"
        dest.write_text(original)
        proc = self._bash(
            "export GITOPS_FORGE=gitlab GITOPS_REPO=g/p GITOPS_ORG=\n"
            f'bootstrap_install_env_file "{dest}" 0.5.0\n'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertEqual(dest.read_text(), original)
        source = _INSTALL_SH.read_text()
        call = source.index('  record_gitops_forge_keys "$INSTALL_ENV_FILE"')
        self.assertLess(source.index('write_json_report "GENERATE_ONLY_SUCCESS"'), call)
        self.assertLess(source.index('write_json_report "PAUSED"'), call)
        self.assertLess(call, source.index('run_lifecycle_apply "$repo_dir" "$provisioning_log"'))
        self.assertEqual(source.count("record_gitops_forge_keys \""), 1)

    def test_a_switch_to_gitlab_drops_the_pem_path_too(self):
        dest = self._tmp / "install.env"
        dest.write_text("GITOPS_ORG=acme\nGITOPS_REPO=infra\nGITHUB_APP_ID=1\nGITHUB_PEM_PATH=/k.pem\n")
        proc = self._bash(f'export GITOPS_FORGE=gitlab GITOPS_REPO=g/p\nrecord_gitops_forge_keys "{dest}"\n')
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertNotIn("GITHUB_PEM_PATH", dest.read_text())

    def _source_with(self, recorded, *flags):
        """Source install.sh over an install.env, then parse flags and validate."""
        env_file = self._tmp / "recorded.env"
        env_file.write_text(recorded)
        body = (
            f'source "{_INSTALLER_COMMON}"\nKUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
            f"parse_args {' '.join(shlex.quote(f) for f in flags)}\n"
            "validate_gitops_forge_flags && rc=0 || rc=$?\n"
            'echo "rc=$rc forge=$PARAM_GITOPS_FORGE host=[$PARAM_GITOPS_HOST] app=[$PARAM_GITHUB_APP_ID] pem=[$PARAM_GITHUB_PEM_PATH]"\n'
        )
        return subprocess.run(
            ["bash", "-c", body], capture_output=True, text=True, cwd=str(_REPO_ROOT),
            stdin=subprocess.DEVNULL, start_new_session=True, timeout=60,
            env=get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": str(env_file)}),
        )

    def test_a_recorded_github_app_does_not_block_a_switch_to_gitlab(self):
        proc = self._source_with(
            "GITOPS_ORG=acme\nGITOPS_REPO=infra\nGITHUB_APP_ID=123\nGITHUB_PEM_PATH=/k.pem\n",
            "--gitops-forge=gitlab", "--gitops-repo=g/p",
        )
        self.assertIn("rc=0 forge=gitlab host=[] app=[] pem=[]", proc.stdout, proc.stderr)
        self.assertIn("Dropping the recorded GitHub App", proc.stdout)

    def test_a_github_app_flag_with_gitlab_is_still_refused(self):
        proc = self._source_with("", "--gitops-forge=gitlab", "--gitops-repo=g/p", "--github-app-id=1")
        self.assertRegex(proc.stdout, r"rc=[1-9]", proc.stderr)

    def test_a_recorded_self_managed_host_does_not_block_a_switch_to_github(self):
        proc = self._source_with(
            "GITOPS_FORGE=gitlab\nGITOPS_HOST=gitlab.example.com\nGITOPS_REPO=g/p\nGITLAB_TOKEN_SECRET=gl\n",
            "--gitops-forge=github", "--gitops-org=acme", "--gitops-repo=infra",
        )
        self.assertIn("rc=0 forge=github host=[]", proc.stdout, proc.stderr)

    def test_a_host_flag_with_github_is_still_refused(self):
        proc = self._source_with("", "--gitops-forge=github", "--gitops-host=gitlab.example.com")
        self.assertRegex(proc.stdout, r"rc=[1-9]", proc.stderr)

    def test_a_shell_export_does_not_choose_install_sh_s_forge(self):
        # install.sh loads install.env through bootstrap_install_env, not
        # load_install_env; both must ignore the shell.
        env_file = self._tmp / "recorded.env"
        env_file.write_text("GITOPS_ORG=acme\nGITOPS_REPO=infra\n")
        body = f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\necho "forge=[$PARAM_GITOPS_FORGE] host=[$PARAM_GITOPS_HOST] secret=[$PARAM_GITLAB_TOKEN_SECRET]"\n'
        proc = subprocess.run(
            ["bash", "-c", body], capture_output=True, text=True, cwd=str(_REPO_ROOT),
            stdin=subprocess.DEVNULL, start_new_session=True, timeout=60,
            env=get_isolated_test_env(overrides={
                "KUBE_AGENTS_INSTALL_ENV": str(env_file), "GITOPS_FORGE": "gitlab",
                "GITOPS_HOST": "h.example", "GITLAB_TOKEN_SECRET": "x",
            }),
        )
        self.assertIn("forge=[] host=[] secret=[]", proc.stdout, proc.stderr)

    def test_an_unchanged_github_install_env_is_not_touched(self):
        dest = self._tmp / "install.env"
        original = "PROJECT_ID=p\n# a comment\nGITOPS_ORG=acme\nGITOPS_REPO=infra\n"
        dest.write_text(original)
        proc = self._bash(f'export GITOPS_ORG=acme GITOPS_REPO=infra\nrecord_gitops_forge_keys "{dest}"\n')
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertEqual(dest.read_text(), original)
        self.assertNotIn("Recorded the GitOps forge", proc.stdout)

    def test_an_unchanged_gitlab_install_env_is_not_rewritten(self):
        dest = self._tmp / "install.env"
        original = "GITOPS_FORGE=gitlab\nGITOPS_HOST=\nGITLAB_TOKEN_SECRET=gl\nGITOPS_REPO=g/p\n# kept\n"
        dest.write_text(original)
        proc = self._bash(
            f'export GITOPS_FORGE=gitlab GITOPS_HOST= GITLAB_TOKEN_SECRET=gl GITOPS_REPO=g/p\nrecord_gitops_forge_keys "{dest}"\n'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertEqual(dest.read_text(), original)

    def test_a_shell_export_does_not_choose_the_forge(self):
        dest = self._tmp / "install.env"
        dest.write_text("PROJECT_ID=p\n")
        proc = self._bash(
            "export GITOPS_FORGE=gitlab GITOPS_HOST=h.example GITLAB_TOKEN_SECRET=x\n"
            f'load_install_env "{dest}"; echo "forge=[${{GITOPS_FORGE:-}}] host=[${{GITOPS_HOST:-}}] secret=[${{GITLAB_TOKEN_SECRET:-}}]"\n'
        )
        self.assertIn("forge=[] host=[] secret=[]", proc.stdout, proc.stderr)

    def test_the_forge_question_is_asked_only_when_undecided(self):
        for env, given in (
            ("", "false"),
            ("PARAM_GITOPS_FORGE=gitlab", "true"),
            ("PARAM_GITOPS_ORG=acme", "true"),
            ("PARAM_GITHUB_APP_ID=1", "true"),
        ):
            with self.subTest(env=env):
                proc = self._bash(
                    f"PARAM_GITOPS_FORGE= PARAM_GITOPS_ORG= PARAM_GITHUB_APP_ID= {env}\n"
                    f"{env}\nresolve_shared_defaults >/dev/null 2>&1; "
                    'echo "given=$PARAM_GITOPS_FORGE_GIVEN"\n'
                )
                self.assertIn(f"given={given}", proc.stdout, proc.stderr)

    def test_the_example_documents_the_forge_keys(self):
        example = (_REPO_ROOT / "install.env.example").read_text()
        for key in ("GITOPS_FORGE=gitlab", "GITOPS_HOST=", "GITLAB_TOKEN_SECRET="):
            self.assertIn("# " + key, example)


class GitLabTerraformCompositionTest(unittest.TestCase):
    """Source-level: the composition names the Secret and never the token."""

    def setUp(self):
        self.main = (_FULL_INSTALL / "main.tf").read_text()
        self.variables = (_FULL_INSTALL / "variables.tf").read_text()

    def test_variables_exist_with_github_defaults(self):
        for name, default in (
            ("gitops_forge", '"github"'),
            ("gitops_host", '""'),
            ("gitlab_repo", '""'),
            ("gitlab_token_secret_name", '"gitlab-forge-token"'),
        ):
            m = re.search(r'variable "%s" \{(.*?)\n\}' % name, self.variables, re.S)
            self.assertIsNotNone(m, name)
            self.assertRegex(m.group(1), r"default\s*=\s*%s" % re.escape(default), name)
            self.assertNotIn("sensitive", m.group(1))

    def test_no_token_variable(self):
        # Only the Secret's name is a variable; nothing that could hold the token.
        names = re.findall(r'^variable "(gitlab_\w+)"', self.variables, re.M)
        self.assertEqual(sorted(names), ["gitlab_repo", "gitlab_token_secret_name"])

    def test_gitlab_suppresses_the_github_alias(self):
        self.assertIn("!local.gitops_is_gitlab && (local.github_org", self.main)
        self.assertIn("credentialsRef = { name = var.gitlab_token_secret_name }", self.main)
        self.assertIn('role = "gitops"', self.main)

    def test_precondition_refuses_a_minter_on_gitlab(self):
        self.assertIn("!var.enable_github_minter", self.main)


if __name__ == "__main__":
    unittest.main()
