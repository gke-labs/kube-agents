"""Unit tests for scripts/release/build_release_binaries.sh.

Runs the script against a mock repository with a mock `go` on PATH, so the
tests check which platforms are built, with which flags, from which commit,
and what each archive holds, without a Go toolchain.
"""

import pathlib
import subprocess
import tarfile
import unittest

from tests.testing.common import (
    INVALID_GA_RELEASE_TAGS,
    create_minimal_tools_bin,
    create_mock_git_repo,
    get_isolated_test_env,
)
from tests.testing.release import MOCK_TARGET_RELEASE_TAG, create_mock_go_binary

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "release" / "build_release_binaries.sh"
_PLATFORMS = ["linux/amd64", "linux/arm64", "darwin/amd64", "darwin/arm64"]
_README_AT_RELEASE = "k8s-api-proxy README at the release\n"


def _archive_name(platform):
    goos, goarch = platform.split("/")
    return f"k8s-api-proxy_{MOCK_TARGET_RELEASE_TAG}_{goos}_{goarch}"


class BuildReleaseBinariesTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir, self.repo_dir, self.git = create_mock_git_repo()
        self.addCleanup(self.temp_dir.cleanup)
        base = pathlib.Path(self.repo_dir).parent
        self.dist = base / "dist"
        # Only the tools the script needs, so the developer's own go stays out of reach.
        self.bin_dir = create_minimal_tools_bin(base)

    def _run(self, args, env=None, with_go=True, fail_build=False):
        if with_go:
            create_mock_go_binary(self.bin_dir, fail_build=fail_build)
        return subprocess.run(
            ["bash", str(_SCRIPT)] + args,
            capture_output=True,
            text=True,
            env=get_isolated_test_env(
                overrides={
                    "PATH": str(self.bin_dir),
                    "REPO_ROOT": self.repo_dir,
                    "DIST_DIR": str(self.dist),
                    **(env or {}),
                },
            ),
            cwd=self.repo_dir,
        )

    def _commit_module_and_tag(self):
        root = pathlib.Path(self.repo_dir)
        module = root / "k8s-api-proxy"
        (module / "cmd" / "k8s-api-proxy").mkdir(parents=True)
        (module / "go.mod").write_text("module github.com/gke-labs/kube-agents/k8s-api-proxy\n")
        (module / "cmd" / "k8s-api-proxy" / "main.go").write_text("package main\n\nfunc main() {}\n")
        (module / "README.md").write_text(_README_AT_RELEASE)
        (root / "LICENSE").write_text("Apache License\n")
        self.git("add", ".")
        self.git("commit", "-m", "feat: add module")
        self.git("tag", "-a", MOCK_TARGET_RELEASE_TAG, "-m", "release")
        return self.git("rev-parse", "HEAD").stdout.strip()

    def _go_log(self):
        return (self.bin_dir / "go.log").read_text()

    def test_missing_tag(self):
        proc = self._run([])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("TAG_NAME must be specified", proc.stderr)

    def test_invalid_tag(self):
        for bad in INVALID_GA_RELEASE_TAGS:
            with self.subTest(bad=bad):
                proc = self._run([bad])
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("not a valid pure numeric SemVer", proc.stderr)

    def test_builds_every_platform_into_an_archive(self):
        self._commit_module_and_tag()

        proc = self._run([MOCK_TARGET_RELEASE_TAG])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        built = sorted(p.name for p in self.dist.iterdir())
        self.assertEqual(built, sorted(f"{_archive_name(p)}.tar.gz" for p in _PLATFORMS))
        for platform in _PLATFORMS:
            with self.subTest(platform=platform):
                root = _archive_name(platform)
                with tarfile.open(self.dist / f"{root}.tar.gz") as archive:
                    names = sorted(archive.getnames())
                    self.assertEqual(names, sorted([root, f"{root}/k8s-api-proxy", f"{root}/LICENSE", f"{root}/README.md"]))
                    binary = archive.extractfile(f"{root}/k8s-api-proxy").read().decode()
                    self.assertIn(platform, binary)

    def test_builds_static_trimmed_binaries(self):
        self._commit_module_and_tag()

        proc = self._run([MOCK_TARGET_RELEASE_TAG])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        builds = [line for line in self._go_log().splitlines() if " go build " in line]
        self.assertEqual(len(builds), len(_PLATFORMS))
        for line, platform in zip(builds, _PLATFORMS):
            goos, goarch = platform.split("/")
            self.assertIn(f"GOOS={goos} GOARCH={goarch} CGO_ENABLED=0 GOFLAGS=-mod=readonly", line)
            self.assertIn("-trimpath", line)
            self.assertIn("-ldflags=-s -w", line)
            self.assertIn("./cmd/k8s-api-proxy", line)

    def test_builds_from_the_release_commit_not_the_working_tree(self):
        self._commit_module_and_tag()
        readme = pathlib.Path(self.repo_dir) / "k8s-api-proxy" / "README.md"
        readme.write_text("changed after the release\n")
        self.git("commit", "-am", "docs: after the release")

        proc = self._run([MOCK_TARGET_RELEASE_TAG])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        root = _archive_name(_PLATFORMS[0])
        with tarfile.open(self.dist / f"{root}.tar.gz") as archive:
            self.assertEqual(archive.extractfile(f"{root}/README.md").read().decode(), _README_AT_RELEASE)

    def test_release_commit_overrides_the_tag(self):
        release_commit = self._commit_module_and_tag()
        self.git("tag", "-d", MOCK_TARGET_RELEASE_TAG)

        proc = self._run([MOCK_TARGET_RELEASE_TAG], env={"RELEASE_COMMIT": release_commit})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(list(self.dist.iterdir())), len(_PLATFORMS))

    def test_skips_a_commit_without_the_module(self):
        # A patch on a release line cut before the module existed.
        self.git("tag", "-a", MOCK_TARGET_RELEASE_TAG, "-m", "release")

        proc = self._run([MOCK_TARGET_RELEASE_TAG], env={"CI": "true"}, with_go=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("no k8s-api-proxy binaries", proc.stdout)
        self.assertFalse(self.dist.exists() and any(self.dist.iterdir()))

    def test_go_is_mandatory_in_ci(self):
        self._commit_module_and_tag()

        proc = self._run([MOCK_TARGET_RELEASE_TAG], env={"CI": "true"}, with_go=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("'go' is mandatory in CI", proc.stderr)

    def test_off_ci_without_go_skips_with_a_warning(self):
        self._commit_module_and_tag()

        proc = self._run([MOCK_TARGET_RELEASE_TAG], with_go=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Skipping release binaries", proc.stderr)

    def test_a_failed_build_fails_the_script(self):
        self._commit_module_and_tag()

        proc = self._run([MOCK_TARGET_RELEASE_TAG], fail_build=True)
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
