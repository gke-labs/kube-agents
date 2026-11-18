"""The a2a-worker image's git: local working copies only, and no credential.

A profile persona's version-control skill clones and publishes through
`scripts/vcs.py`, which asks the credential broker; the worker's own git works
only on the copy `clone` unpacked. `deploy/shared/worker-gitconfig` is the
image's system config (`/etc/gitconfig`, `a2a/Dockerfile.worker`). The image
can't be built here, so these run this machine's git under that file, with
`GIT_CONFIG_SYSTEM` pointing at it and every other config source off, and read
the Dockerfile for the rest: the helpers it deletes, the modules vcs.py imports
there, and where vcs_client keeps its git and its copies.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GITCONFIG = REPO_ROOT / "deploy" / "shared" / "worker-gitconfig"
WORKER_DOCKERFILE = REPO_ROOT / "a2a" / "Dockerfile.worker"
PROFILE_POOL_GO = REPO_ROOT / "k8s-operator" / "internal" / "controller" / "agentprofile_pool.go"
PLATFORM_VCS = REPO_ROOT / "agents" / "platform" / "skills" / "version-control" / "scripts" / "vcs.py"
PERSONA_VCS = REPO_ROOT / "agents" / "cluster" / "persona" / "skills" / "version-control" / "scripts" / "vcs.py"
PLATFORM_SCRIPTS = REPO_ROOT / "agents" / "platform" / "scripts"

# GIT_CONFIG_SYSTEM arrived in git 2.32.
MIN_GIT = (2, 32)

# Remote URLs of every transport git has, and the transport name git's refusal
# names. example.invalid never resolves, so each of these fails whatever the
# config says; the message is what shows the config refused it.
REMOTE_URLS = {
    "https://example.invalid/x.git": "https",
    "http://example.invalid/x.git": "http",
    "ftp://example.invalid/x.git": "ftp",
    "ssh://example.invalid/x.git": "ssh",
    "git@example.invalid:x.git": "ssh",
    "git://example.invalid/x.git": "git",
    "ext::sh -c true": "ext",
}


def git_version() -> tuple[int, ...] | None:
    git = shutil.which("git")
    if not git:
        return None
    out = subprocess.run([git, "--version"], capture_output=True, text=True, check=True).stdout
    m = re.search(r"(\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def joined_instructions(text: str) -> list[str]:
    """The Dockerfile's instructions, continuation lines joined, comments dropped."""
    instructions, current = [], ""
    for line in text.splitlines():
        stripped = line.strip()
        if not current and (not stripped or stripped.startswith("#")):
            continue
        if stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            current += stripped[:-1] + " "
            continue
        instructions.append(current + stripped)
        current = ""
    return instructions


@unittest.skipUnless((git_version() or (0,)) >= MIN_GIT, "needs git 2.32 or later on PATH")
class WorkerGitConfigTest(unittest.TestCase):
    """This machine's git, configured only by the worker's system config."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="worker-gitconfig-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        home = self.tmp / "home"
        home.mkdir()
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_") and key not in ("HOME", "XDG_CONFIG_HOME")
        }
        self.env.update(
            {
                "GIT_CONFIG_SYSTEM": str(GITCONFIG),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "HOME": str(home),
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.invalid",
            }
        )
        self.repo = self.tmp / "repo"
        self.git("init", "-q", str(self.repo))
        (self.repo / "a.txt").write_text("a\n")
        self.git("-C", str(self.repo), "add", "a.txt")
        self.git("-C", str(self.repo), "commit", "-q", "-m", "a")

    def git(self, *args: str, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            check=check,
            timeout=60,
            env=env or self.env,
            cwd=str(self.tmp),
        )

    def assertRefused(self, result: subprocess.CompletedProcess, transport: str):
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertIn(f"transport '{transport}' not allowed", result.stderr)

    def test_the_file_is_the_config_git_reads(self):
        origin = self.git("config", "--show-origin", "--get", "protocol.allow").stdout
        self.assertIn(str(GITCONFIG), origin)
        self.assertIn("never", origin)

    def test_ls_remote_of_every_remote_transport_is_refused(self):
        for url, transport in REMOTE_URLS.items():
            with self.subTest(url=url):
                self.assertRefused(self.git("ls-remote", url, check=False), transport)

    def test_fetch_and_push_to_a_remote_url_are_refused(self):
        repo = str(self.repo)
        for url, transport in REMOTE_URLS.items():
            with self.subTest(url=url):
                self.assertRefused(self.git("-C", repo, "fetch", url, check=False), transport)
                self.assertRefused(self.git("-C", repo, "push", url, "HEAD:refs/heads/main", check=False), transport)

    def test_a_remote_added_by_name_is_refused_too(self):
        repo = str(self.repo)
        self.git("-C", repo, "remote", "add", "origin", "https://example.invalid/x.git")
        self.assertRefused(self.git("-C", repo, "push", "origin", "HEAD:refs/heads/main", check=False), "https")
        self.assertRefused(self.git("-C", repo, "fetch", "origin", check=False), "https")
        self.assertRefused(self.git("-C", repo, "pull", "origin", "main", check=False), "https")

    def test_clone_of_a_remote_url_is_refused(self):
        for url, transport in REMOTE_URLS.items():
            with self.subTest(url=url):
                self.assertRefused(self.git("clone", url, str(self.tmp / "clone"), check=False), transport)

    def test_no_credential_helper_is_configured(self):
        helpers = self.git("config", "--get-all", "credential.helper").stdout.splitlines()
        self.assertEqual([""], helpers, "the config's empty helper resets the list; nothing may follow it")

    def test_local_copies_still_work(self):
        """The file transport is what clone's bundle and a local path use."""
        bundle = self.tmp / "repo.bundle"
        self.git("-C", str(self.repo), "bundle", "create", str(bundle), "--all")
        from_bundle = self.tmp / "from-bundle"
        self.git("clone", "-q", str(bundle), str(from_bundle))
        self.assertTrue((from_bundle / "a.txt").is_file())

        from_path = self.tmp / "from-path"
        self.git("clone", "-q", str(self.repo), str(from_path))
        self.git("-C", str(from_path), "fetch", "-q", "origin")

        bare = self.tmp / "bare.git"
        self.git("init", "-q", "--bare", str(bare))
        self.git("-C", str(self.repo), "push", "-q", f"file://{bare}", "HEAD:refs/heads/main")
        self.assertIn("refs/heads/main", self.git("-C", str(bare), "show-ref").stdout)

    def test_without_the_file_git_does_not_refuse_by_config(self):
        """The refusal above is the file's doing, not this machine's defaults."""
        env = dict(self.env)
        env.pop("GIT_CONFIG_SYSTEM")
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        result = self.git("ls-remote", "https://example.invalid/x.git", check=False, env=env)
        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("not allowed", result.stderr)


class WorkerDockerfileTest(unittest.TestCase):
    """What a2a/Dockerfile.worker does with that file and the skill's client."""

    LIBEXEC = "/opt/credential-proxy/libexec"

    @classmethod
    def setUpClass(cls):
        cls.text = WORKER_DOCKERFILE.read_text(encoding="utf-8")
        cls.instructions = joined_instructions(cls.text)

    def copies_into(self, destination: str) -> list[str]:
        sources = []
        for instruction in self.instructions:
            if not instruction.startswith("COPY "):
                continue
            words = [w for w in instruction.split()[1:] if not w.startswith("--")]
            target = words[-1]
            if target.rstrip("/") == destination:
                sources += words[:-1]
            elif len(words) == 2 and not target.endswith("/") and os.path.dirname(target) == destination:
                # One file copied to a path in the directory, under its own name.
                if os.path.basename(target) == os.path.basename(words[0]):
                    sources.append(words[0])
        return sources

    def test_the_config_is_the_images_system_config(self):
        self.assertIn(["deploy/shared/worker-gitconfig"], [self.copies_into("/etc/gitconfig")])

    def test_git_is_installed_and_its_http_helpers_are_deleted(self):
        install = next(i for i in self.instructions if "apt-get install" in i)
        self.assertRegex(install, r"\bgit\b")
        helpers = next(i for i in self.instructions if "git-remote-https" in i and i.startswith("RUN "))
        for helper in ("http", "https", "ftp", "ftps"):
            self.assertIn(f"/usr/lib/git-core/git-remote-{helper}", helpers)
        self.assertIn("transport '${helper}' not allowed", helpers)

    def test_the_guard_still_refuses_gcloud_and_gh(self):
        guard = next(i for i in self.instructions if "unexpected credential-aware CLI" in i)
        self.assertRegex(guard, r"for binary in gcloud gh; do")

    def test_vcs_py_finds_the_directory_the_closure_is_copied_to(self):
        for vcs in (PLATFORM_VCS, PERSONA_VCS):
            with self.subTest(str(vcs.relative_to(REPO_ROOT))):
                text = vcs.read_text(encoding="utf-8")
                self.assertIn(f'PROFILE_WORKER_LIBEXEC = "{self.LIBEXEC}"', text)
                self.assertIn("sys.path.append(PROFILE_WORKER_LIBEXEC)", text)

    def test_the_copied_modules_are_vcs_clients_whole_closure(self):
        """vcs.py runs with only what the image puts in the directory.

        The persona's copy of vcs.py sits in a skill directory of its own, and
        its third path entry (`parents[3]/scripts`) is the stand-in here for
        the image's directory, which can't be created on this machine. Every
        module it loads from outside the standard library must come from there.
        """
        modules = [s for s in self.copies_into(self.LIBEXEC) if s.endswith(".py") and "credential-proxy-exec" not in s]
        self.assertIn("agents/platform/scripts/credential_proxy_client.py", modules)
        self.assertIn("agents/platform/scripts/vcs_client.py", modules)
        root = Path(tempfile.mkdtemp(prefix="worker-libexec-")).resolve()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        libexec = root / "scripts"
        libexec.mkdir()
        for module in modules:
            shutil.copy(REPO_ROOT / module, libexec)
        skill = root / "skills" / "version-control" / "scripts"
        skill.mkdir(parents=True)
        shutil.copy(PERSONA_VCS, skill)
        probe = (
            "import runpy, sys, sysconfig\n"
            "script, libexec = sys.argv[1], sys.argv[2]\n"
            "sys.argv = [script, '--help']\n"
            "try:\n"
            "    runpy.run_path(script, run_name='__main__')\n"
            "except SystemExit as exc:\n"
            "    assert exc.code in (0, None), exc.code\n"
            "import vcs_client, gitops_workspace\n"
            "std = sysconfig.get_paths()['stdlib']\n"
            "for name, mod in list(sys.modules.items()):\n"
            "    f = getattr(mod, '__file__', None)\n"
            "    if f and not f.startswith(std) and not f.startswith(libexec) and f != script:\n"
            "        print('outside:', name, f)\n"
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            [sys.executable, "-I", "-c", probe, str(skill / "vcs.py"), str(libexec)],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            cwd=str(root),
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn("outside:", result.stdout)

    def test_vcs_client_keeps_its_copies_on_the_scratch_volume(self):
        env = next(i for i in self.instructions if i.startswith("ENV ") and "KUBE_AGENTS_VCS_ROOT" in i)
        self.assertIn("KUBE_AGENTS_LOCAL_GIT=/usr/bin/git", env)
        root = re.search(r"KUBE_AGENTS_VCS_ROOT=(\S+)", env).group(1)
        scratch = re.search(r'profileWorkerScratchPath\s+=\s+"([^"]+)"', PROFILE_POOL_GO.read_text(encoding="utf-8"))
        self.assertIsNotNone(scratch, "agentprofile_pool.go has no profileWorkerScratchPath")
        self.assertTrue(
            root.startswith(scratch.group(1) + "/"),
            f"{root} is not on the worker's scratch volume ({scratch.group(1)}), the one writable path the worker empties",
        )

    def test_the_config_names_the_transport_it_keeps(self):
        text = GITCONFIG.read_text(encoding="utf-8")
        self.assertRegex(text, r'\[protocol "file"\]\s*\n\s*allow = always')
        self.assertRegex(text, r"\[protocol\]\s*\n\s*allow = never")


if __name__ == "__main__":
    unittest.main()
