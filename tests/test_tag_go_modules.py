"""Unit tests for scripts/release/tag_go_modules.sh.

Tests that each published Go module is tagged `<dir>/v<version>` at the commit
the GA tag points to, idempotently, and that the modules the script publishes
are ones `go get` can consume.
"""

import pathlib
import re
import subprocess
import unittest

from tests.testing.common import (
    INVALID_GA_RELEASE_TAGS,
    create_mock_git_repo,
    get_isolated_test_env,
)
from tests.testing.release import MOCK_TARGET_RELEASE_TAG

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TAG_GO_MODULES_SH = _REPO_ROOT / "scripts" / "release" / "tag_go_modules.sh"
_MODULE_DIR = "k8s-api-proxy"
_MODULE_PATH = f"github.com/gke-labs/kube-agents/{_MODULE_DIR}"
_MODULE_TAG = f"{_MODULE_DIR}/v{MOCK_TARGET_RELEASE_TAG}"
_REPOSITORY_MODULE_PREFIX = "github.com/gke-labs/kube-agents/"
_FAKE_RELEASE_REPO = {"GH_ORG": "no-such-org-kube-agents", "GH_REPO": "no-such-repo"}
# An empty proxy URL keeps the CI-mode runs off the network.
_NO_PROXY = {"GO_MODULE_PROXY_URL": ""}


def _published_module_dirs():
    """The directories PUBLISHED_GO_MODULE_DIRS lists in the script."""
    text = _TAG_GO_MODULES_SH.read_text()
    block = re.search(r"PUBLISHED_GO_MODULE_DIRS=\((.*?)\)", text, re.S)
    assert block, "PUBLISHED_GO_MODULE_DIRS not found in tag_go_modules.sh"
    return re.findall(r'"([^"]+)"', block.group(1))


class TagGoModulesScriptTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir, self.repo_dir, self.git = create_mock_git_repo()
        self.addCleanup(self.temp_dir.cleanup)

    def _run_script(self, args, env=None, bin_dir=None):
        return subprocess.run(
            ["bash", str(_TAG_GO_MODULES_SH)] + args,
            capture_output=True,
            text=True,
            env=get_isolated_test_env(overrides={**_NO_PROXY, **(env or {})}, bin_dir=bin_dir),
            cwd=self.repo_dir,
        )

    def _commit_module(self, module_path=_MODULE_PATH):
        module = pathlib.Path(self.repo_dir) / _MODULE_DIR
        module.mkdir()
        (module / "go.mod").write_text(f"module {module_path}\n\ngo 1.27.0\n")
        self.git("add", ".")
        self.git("commit", "-m", "feat: add module")
        return self.git("rev-parse", "HEAD").stdout.strip()

    def _tag_ga(self, commit, version=MOCK_TARGET_RELEASE_TAG):
        self.git("tag", "-a", version, commit, "-m", f"Release {version}")

    def _tags(self):
        return self.git("tag", "-l").stdout.split()

    def _ci_origin(self):
        """A bare origin standing in for the release repository, as test_tag_ga_release.py does."""
        bare = pathlib.Path(self.repo_dir).parent / "origin.git"
        self.git("init", "--bare", str(bare))
        self.git("remote", "add", "origin", str(bare))
        self.git(
            "config",
            f"url.{bare}.insteadOf",
            f"https://github.com/{_FAKE_RELEASE_REPO['GH_ORG']}/{_FAKE_RELEASE_REPO['GH_REPO']}.git",
        )
        return bare

    def _stub_curl(self, exit_code):
        """A curl on PATH that records its arguments and exits with exit_code."""
        bin_dir = pathlib.Path(self.repo_dir).parent / "bin"
        bin_dir.mkdir()
        log = bin_dir / "curl.log"
        curl = bin_dir / "curl"
        curl.write_text(f'#!/usr/bin/env bash\necho "$@" >> "{log}"\nexit {exit_code}\n')
        curl.chmod(0o755)
        return bin_dir, log

    def _commit_after(self):
        (pathlib.Path(self.repo_dir) / "later.txt").write_text("after the release\n")
        self.git("add", ".")
        self.git("commit", "-m", "chore: after the release")
        return self.git("rev-parse", "HEAD").stdout.strip()

    def _tag_commit(self, tag, git_dir=None):
        args = ["--git-dir", str(git_dir)] if git_dir else []
        return self.git(*args, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}").stdout.strip()

    def test_missing_version(self):
        proc = self._run_script([])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("RELEASE_VERSION is required", proc.stderr)

    def test_invalid_version(self):
        for bad in INVALID_GA_RELEASE_TAGS:
            with self.subTest(bad=bad):
                proc = self._run_script([bad])
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("not a valid pure numeric SemVer", proc.stderr)

    def test_refuses_without_the_ga_tag(self):
        self._commit_module()
        proc = self._run_script([MOCK_TARGET_RELEASE_TAG])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(f"GA tag '{MOCK_TARGET_RELEASE_TAG}' does not exist", proc.stderr)

    def test_tags_the_ga_commit_with_the_directory_and_v_prefix(self):
        release_commit = self._commit_module()
        self._tag_ga(release_commit)
        self._commit_after()

        proc = self._run_script([MOCK_TARGET_RELEASE_TAG])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._tag_commit(_MODULE_TAG), release_commit)

    def test_re_run_is_an_idempotent_skip(self):
        self._tag_ga(self._commit_module())
        self.assertEqual(self._run_script([MOCK_TARGET_RELEASE_TAG]).returncode, 0)

        proc = self._run_script([MOCK_TARGET_RELEASE_TAG])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Idempotent skip", proc.stdout)

    def test_refuses_a_module_tag_at_another_commit(self):
        release_commit = self._commit_module()
        self._tag_ga(release_commit)
        other = self._commit_after()
        self.git("tag", "-a", _MODULE_TAG, other, "-m", "elsewhere")

        proc = self._run_script([MOCK_TARGET_RELEASE_TAG])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(f"Tag '{_MODULE_TAG}' already exists but points to commit {other}", proc.stderr)

    def test_skips_a_module_absent_at_the_release_commit(self):
        # A patch on a line cut before the module existed: the GA tag is already
        # pushed, so failing would hold the release short on every re-run.
        self._tag_ga(self.git("rev-parse", "HEAD").stdout.strip())
        self._commit_module()

        proc = self._run_script([MOCK_TARGET_RELEASE_TAG])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"{_MODULE_DIR}/go.mod does not exist at release commit", proc.stdout)
        self.assertNotIn(_MODULE_TAG, self._tags())

    def test_skips_a_module_whose_path_does_not_match_its_directory(self):
        self._tag_ga(self._commit_module(module_path="example.com/elsewhere"))

        proc = self._run_script([MOCK_TARGET_RELEASE_TAG])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("declares module 'example.com/elsewhere'", proc.stdout)
        self.assertNotIn(_MODULE_TAG, self._tags())

    def test_tags_nothing_past_major_zero(self):
        self._tag_ga(self._commit_module(), version="1.0.0")

        proc = self._run_script(["1.0.0"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("past major 0", proc.stdout)
        self.assertNotIn(f"{_MODULE_DIR}/v1.0.0", self._tags())

    def test_off_ci_nothing_is_pushed(self):
        self._tag_ga(self._commit_module())
        bare = pathlib.Path(self.repo_dir).parent / "origin.git"
        self.git("init", "--bare", str(bare))
        self.git("remote", "add", "origin", str(bare))

        proc = self._run_script([MOCK_TARGET_RELEASE_TAG])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Remote push skipped", proc.stdout)
        remote_tags = self.git("--git-dir", str(bare), "tag", "-l").stdout.split()
        self.assertNotIn(_MODULE_TAG, remote_tags)

    def test_in_ci_pushes_the_module_tag(self):
        release_commit = self._commit_module()
        self._tag_ga(release_commit)
        bare = self._ci_origin()

        proc = self._run_script([MOCK_TARGET_RELEASE_TAG], env={"CI": "true", **_FAKE_RELEASE_REPO})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._tag_commit(_MODULE_TAG, git_dir=bare), release_commit)

    def test_in_ci_asks_the_module_proxy_for_the_new_version(self):
        self._tag_ga(self._commit_module())
        self._ci_origin()
        bin_dir, log = self._stub_curl(0)

        proc = self._run_script(
            [MOCK_TARGET_RELEASE_TAG],
            env={"CI": "true", "GO_MODULE_PROXY_URL": "https://proxy.test", **_FAKE_RELEASE_REPO},
            bin_dir=bin_dir,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"https://proxy.test/{_MODULE_PATH}/@v/v{MOCK_TARGET_RELEASE_TAG}.info", log.read_text())

    def test_a_module_proxy_failure_is_a_warning(self):
        self._tag_ga(self._commit_module())
        bare = self._ci_origin()
        bin_dir, _ = self._stub_curl(22)

        proc = self._run_script(
            [MOCK_TARGET_RELEASE_TAG],
            env={"CI": "true", "GO_MODULE_PROXY_URL": "https://proxy.test", **_FAKE_RELEASE_REPO},
            bin_dir=bin_dir,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("fetches the tag on first request", proc.stderr)
        self.assertTrue(self._tag_commit(_MODULE_TAG, git_dir=bare))


class PublishedGoModulesTest(unittest.TestCase):
    """The modules the release tags must be ones `go get` resolves and builds."""

    def test_each_published_module_is_consumable(self):
        dirs = _published_module_dirs()
        self.assertTrue(dirs)
        for module_dir in dirs:
            with self.subTest(module_dir=module_dir):
                go_mod = (_REPO_ROOT / module_dir / "go.mod").read_text()
                module = re.search(r"^module\s+(\S+)", go_mod, re.M)
                self.assertIsNotNone(module, f"{module_dir}/go.mod declares no module")
                # Go finds the tag `<dir>/vX.Y.Z` only when the path is the repository's plus the directory.
                self.assertEqual(module.group(1), _REPOSITORY_MODULE_PREFIX + module_dir)
                # A consumer's build ignores the module's replace directives.
                self.assertIsNone(
                    re.search(r"^\s*replace\b", go_mod, re.M),
                    f"{module_dir}/go.mod carries a replace directive, which `go get` ignores",
                )


if __name__ == "__main__":
    unittest.main()
