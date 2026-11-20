"""Unit tests for scripts/release/build_release_binaries.sh.

Runs the script against a mock repository with a mock `go` on PATH, so the
tests check which platforms are built, with which flags, from which commit,
and what each archive holds, without a Go toolchain.
"""

import pathlib
import re
import subprocess
import tarfile
import unittest
import zipfile

from tests.testing.common import (
    INVALID_GA_RELEASE_TAGS,
    create_minimal_tools_bin,
    create_mock_git_repo,
    get_isolated_test_env,
)
from tests.testing.release import MOCK_TARGET_RELEASE_TAG, create_mock_go_binary, create_mock_tar_binary

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "release" / "build_release_binaries.sh"
_PLATFORMS = ["linux/amd64", "linux/arm64", "darwin/amd64", "darwin/arm64"]
_README_AT_RELEASE = "k8s-api-proxy README at the release\n"
_MCP = "kube-agents-mcp"
_MCP_PLATFORMS = ["darwin/amd64", "darwin/arm64", "linux/amd64", "linux/arm64"]
# The bridge does not ship for Windows yet (#6565). The Windows tests run a copy
# of the script with this platform added to its spec, so the .exe/.zip path
# stays covered until it does.
_WINDOWS = "windows/amd64"
# The kube-agents-mcp spec line, up to and including its platform list.
_MCP_SPEC_PLATFORMS = re.compile(r'("kube-agents-mcp\|[^"\n]*\|)([^"|\n]*)"')
_ROOT_README = "kube-agents README at the release\n"


def _archive_name(platform, name="k8s-api-proxy"):
    goos, goarch = platform.split("/")
    return f"{name}_{MOCK_TARGET_RELEASE_TAG}_{goos}_{goarch}"


def _mcp_archive_file(platform):
    suffix = ".zip" if platform.startswith("windows/") else ".tar.gz"
    return _archive_name(platform, _MCP) + suffix


def _proxy_archive_files():
    return sorted(f"{_archive_name(p)}.tar.gz" for p in _PLATFORMS)


def _mcp_archive_files():
    return sorted(_mcp_archive_file(p) for p in _MCP_PLATFORMS)


class BuildReleaseBinariesTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir, self.repo_dir, self.git = create_mock_git_repo()
        self.addCleanup(self.temp_dir.cleanup)
        base = pathlib.Path(self.repo_dir).parent
        self.dist = base / "dist"
        # Only the tools the script needs, so the developer's own go stays out of reach.
        self.bin_dir = create_minimal_tools_bin(base)

    def _run(self, args, env=None, with_go=True, fail_build=False, fail_build_matching=None, script=_SCRIPT):
        if with_go:
            create_mock_go_binary(self.bin_dir, fail_build=fail_build, fail_build_matching=fail_build_matching)
        return subprocess.run(
            ["bash", str(script)] + args,
            capture_output=True,
            text=True,
            env=get_isolated_test_env(
                overrides={
                    "PATH": str(self.bin_dir),
                    "RELEASE_REPO_ROOT": self.repo_dir,
                    "DIST_DIR": str(self.dist),
                    **(env or {}),
                },
            ),
            cwd=self.repo_dir,
        )

    def _commit_module_and_tag(self, with_main=True, with_mcp=False, with_proxy=True):
        root = pathlib.Path(self.repo_dir)
        if with_proxy:
            module = root / "k8s-api-proxy"
            module.mkdir()
            (module / "go.mod").write_text("module github.com/gke-labs/kube-agents/k8s-api-proxy\n")
            if with_main:
                (module / "cmd" / "k8s-api-proxy").mkdir(parents=True)
                (module / "cmd" / "k8s-api-proxy" / "main.go").write_text("package main\n\nfunc main() {}\n")
            (module / "README.md").write_text(_README_AT_RELEASE)
        if with_mcp:
            a2a = root / "a2a"
            (a2a / "cmd" / _MCP).mkdir(parents=True)
            (a2a / "go.mod").write_text("module github.com/gke-labs/kube-agents/a2a\n")
            (a2a / "cmd" / _MCP / "main.go").write_text("package main\n\nfunc main() {}\n")
            (root / "README.md").write_text(_ROOT_README)
        (root / "LICENSE").write_text("Apache License\n")
        self.git("add", ".")
        self.git("commit", "-m", "feat: add module")
        self.git("tag", "-a", MOCK_TARGET_RELEASE_TAG, "-m", "release")
        return self.git("rev-parse", "HEAD").stdout.strip()

    def _script_with_windows(self):
        """A copy of the script whose kube-agents-mcp spec also lists windows/amd64.

        It sits in a scripts/release/ of its own beside a link to the real
        common.sh, and scripts/installer/ links to the real one, so common.sh
        finds what it sources from the copy's root.
        """
        root = pathlib.Path(self.repo_dir).parent / "windows-script"
        release_dir = root / "scripts" / "release"
        release_dir.mkdir(parents=True)
        (release_dir / "common.sh").symlink_to(_SCRIPT.parent / "common.sh")
        (root / "scripts" / "installer").symlink_to(_REPO_ROOT / "scripts" / "installer")
        text, count = _MCP_SPEC_PLATFORMS.subn(rf'\g<1>\g<2> {_WINDOWS}"', _SCRIPT.read_text())
        self.assertEqual(count, 1, "the kube-agents-mcp spec line moved; update _MCP_SPEC_PLATFORMS")
        script = release_dir / _SCRIPT.name
        script.write_text(text)
        return script

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
            self.assertIn(f"GOOS={goos} GOARCH={goarch} CGO_ENABLED=0 GOFLAGS=-mod=readonly GOTOOLCHAIN=local", line)
            self.assertIn("-trimpath", line)
            self.assertIn(f"-ldflags=-s -w -X main.version={MOCK_TARGET_RELEASE_TAG}", line)
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

    def test_skips_a_module_without_the_main_package(self):
        # Lines 0.21 to 0.23 carry the module but not cmd/k8s-api-proxy.
        self._commit_module_and_tag(with_main=False)

        proc = self._run([MOCK_TARGET_RELEASE_TAG], env={"CI": "true"}, with_go=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("k8s-api-proxy/cmd/k8s-api-proxy does not exist", proc.stdout)
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

    def test_skips_kube_agents_mcp_at_a_commit_without_it(self):
        self._commit_module_and_tag()

        proc = self._run([MOCK_TARGET_RELEASE_TAG])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("a2a/cmd/kube-agents-mcp does not exist", proc.stdout)
        self.assertEqual(sorted(p.name for p in self.dist.iterdir()), _proxy_archive_files())

    def test_builds_kube_agents_mcp_for_every_platform(self):
        self._commit_module_and_tag(with_mcp=True)

        proc = self._run([MOCK_TARGET_RELEASE_TAG])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        built = sorted(p.name for p in self.dist.iterdir())
        self.assertEqual(built, sorted(_proxy_archive_files() + _mcp_archive_files()))
        for platform in _MCP_PLATFORMS:
            with self.subTest(platform=platform):
                root = _archive_name(platform, _MCP)
                with tarfile.open(self.dist / _mcp_archive_file(platform)) as archive:
                    names = sorted(archive.getnames())
                    self.assertEqual(names, sorted([root, f"{root}/{_MCP}", f"{root}/LICENSE", f"{root}/README.md"]))
                    self.assertIn(platform, archive.extractfile(f"{root}/{_MCP}").read().decode())
                    self.assertEqual(archive.extractfile(f"{root}/README.md").read().decode(), _ROOT_README)
                    self.assertEqual(archive.extractfile(f"{root}/LICENSE").read().decode(), "Apache License\n")

    def test_kube_agents_mcp_ships_no_windows_archive_yet(self):
        self._commit_module_and_tag(with_mcp=True)

        proc = self._run([MOCK_TARGET_RELEASE_TAG])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("GOOS=windows", self._go_log())
        self.assertEqual(list(self.dist.glob("*windows*")), [])

    def test_kube_agents_mcp_for_windows_is_a_zip_holding_an_exe(self):
        self._commit_module_and_tag(with_mcp=True)

        proc = self._run([MOCK_TARGET_RELEASE_TAG], script=self._script_with_windows())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        built = sorted(p.name for p in self.dist.iterdir())
        self.assertEqual(built, sorted(_proxy_archive_files() + _mcp_archive_files() + [_mcp_archive_file(_WINDOWS)]))
        windows_build = [line for line in self._go_log().splitlines() if "GOOS=windows" in line]
        self.assertEqual(len(windows_build), 1)
        self.assertIn(f"/{_archive_name(_WINDOWS, _MCP)}/{_MCP}.exe ", windows_build[0])
        root = _archive_name(_WINDOWS, _MCP)
        with zipfile.ZipFile(self.dist / f"{root}.zip") as archive:
            names = sorted(archive.namelist())
            self.assertEqual(names, sorted([f"{root}/", f"{root}/{_MCP}.exe", f"{root}/LICENSE", f"{root}/README.md"]))
            self.assertIn("windows/amd64", archive.read(f"{root}/{_MCP}.exe").decode())
            self.assertEqual(archive.read(f"{root}/README.md").decode(), _ROOT_README)
            self.assertEqual(archive.read(f"{root}/LICENSE").decode(), "Apache License\n")

    def test_kube_agents_mcp_builds_like_k8s_api_proxy(self):
        self._commit_module_and_tag(with_mcp=True)

        proc = self._run([MOCK_TARGET_RELEASE_TAG])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        builds = [line for line in self._go_log().splitlines() if " go build " in line]
        proxy_builds = [line for line in builds if "./cmd/k8s-api-proxy" in line]
        mcp_builds = [line for line in builds if f"./cmd/{_MCP}" in line]
        self.assertEqual(len(proxy_builds), len(_PLATFORMS))
        self.assertEqual(len(mcp_builds), len(_MCP_PLATFORMS))
        for line, platform in zip(mcp_builds, _MCP_PLATFORMS):
            goos, goarch = platform.split("/")
            self.assertIn(f"GOOS={goos} GOARCH={goarch} CGO_ENABLED=0 GOFLAGS=-mod=readonly GOTOOLCHAIN=local", line)
            self.assertIn("-trimpath", line)
            self.assertIn(f"-ldflags=-s -w -X main.version={MOCK_TARGET_RELEASE_TAG}", line)
            self.assertIn(f"/{_archive_name(platform, _MCP)}/{_MCP} ", line)

    def test_builds_kube_agents_mcp_without_k8s_api_proxy(self):
        self._commit_module_and_tag(with_mcp=True, with_proxy=False)

        proc = self._run([MOCK_TARGET_RELEASE_TAG])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sorted(p.name for p in self.dist.iterdir()), _mcp_archive_files())

    def _assert_kube_agents_mcp_left_out(self, proc, platform):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        warnings = [line for line in proc.stdout.splitlines() if line.startswith("::warning::")]
        self.assertEqual(len(warnings), 1, proc.stdout)
        self.assertIn(_MCP, warnings[0])
        self.assertIn(platform, warnings[0])
        built = sorted(p.name for p in self.dist.iterdir())
        self.assertEqual(built, sorted(_proxy_archive_files() + ["bundle.tar.gz"]))

    def _seed_dist(self):
        # Another asset already staged, as package_release_bundle.sh stages the bundle.
        self.dist.mkdir()
        (self.dist / "bundle.tar.gz").write_text("bundle\n")

    def test_a_failed_kube_agents_mcp_build_leaves_the_release_without_it(self):
        self._commit_module_and_tag(with_mcp=True)
        self._seed_dist()

        # The last platform fails, after the others have written their archives.
        proc = self._run([MOCK_TARGET_RELEASE_TAG], fail_build_matching=f"linux/arm64*./cmd/{_MCP}*")
        self._assert_kube_agents_mcp_left_out(proc, "linux/arm64")

    def test_a_failed_kube_agents_mcp_archive_leaves_the_release_without_it(self):
        self._commit_module_and_tag(with_mcp=True)
        self._seed_dist()
        (self.bin_dir / "zip").unlink(missing_ok=True)

        # Only the Windows archive needs zip, so the archive fails after the others are written.
        proc = self._run([MOCK_TARGET_RELEASE_TAG], script=self._script_with_windows())
        self._assert_kube_agents_mcp_left_out(proc, _WINDOWS)

    def test_a_failed_kube_agents_mcp_tar_leaves_the_release_without_it(self):
        self._commit_module_and_tag(with_mcp=True)
        self._seed_dist()
        create_mock_tar_binary(self.bin_dir, f"*-czf*{_archive_name('linux/amd64', _MCP)}.tar.gz*")

        proc = self._run([MOCK_TARGET_RELEASE_TAG])
        self._assert_kube_agents_mcp_left_out(proc, "linux/amd64")

    def test_a_failed_k8s_api_proxy_tar_fails_the_script(self):
        self._commit_module_and_tag(with_mcp=True)
        create_mock_tar_binary(self.bin_dir, f"*-czf*{_archive_name('darwin/amd64')}.tar.gz*")

        proc = self._run([MOCK_TARGET_RELEASE_TAG])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(f"mock tar: failing -czf {self.dist}/{_archive_name('darwin/amd64')}.tar.gz", proc.stderr)
        self.assertNotIn("::warning::", proc.stdout)
        self.assertNotIn(f"./cmd/{_MCP}", self._go_log())

    def test_a_dropped_kube_agents_mcp_is_in_the_job_summary(self):
        self._commit_module_and_tag(with_mcp=True)
        summary = pathlib.Path(self.repo_dir).parent / "step-summary.md"
        summary.write_text("earlier step\n")

        proc = self._run(
            [MOCK_TARGET_RELEASE_TAG],
            env={"GITHUB_STEP_SUMMARY": str(summary)},
            fail_build_matching=f"linux/arm64*./cmd/{_MCP}*",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        warning = next(line for line in proc.stdout.splitlines() if line.startswith("::warning::"))
        lines = summary.read_text().splitlines()
        self.assertEqual(lines[0], "earlier step")
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(lines[1].endswith(warning.removeprefix("::warning::")), lines[1])
        self.assertIn("linux/arm64", lines[1])

    def test_a_clean_run_leaves_the_job_summary_alone(self):
        self._commit_module_and_tag(with_mcp=True)
        summary = pathlib.Path(self.repo_dir).parent / "step-summary.md"
        summary.write_text("earlier step\n")

        proc = self._run([MOCK_TARGET_RELEASE_TAG], env={"GITHUB_STEP_SUMMARY": str(summary)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(summary.read_text(), "earlier step\n")

    def test_a_failed_k8s_api_proxy_build_still_fails_beside_kube_agents_mcp(self):
        self._commit_module_and_tag(with_mcp=True)

        proc = self._run([MOCK_TARGET_RELEASE_TAG], fail_build_matching="linux/arm64*./cmd/k8s-api-proxy*")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("go build failed for k8s-api-proxy on linux/arm64", proc.stderr)


_RELEASE_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "release-publish.yml"
_GO_VERSION_FILE = re.compile(r'^\s*go-version-file:\s*"?([^"\s]+)"?\s*$', re.MULTILINE)
_GO_DIRECTIVE = re.compile(r"^go\s+(\S+)\s*$", re.MULTILINE)


def _go_directive(go_mod):
    match = _GO_DIRECTIVE.search((_REPO_ROOT / go_mod).read_text())
    if match is None:
        raise AssertionError(f"{go_mod} has no go directive")
    return match.group(1)


class ReleaseGoToolchainParityTest(unittest.TestCase):
    """The bridge builds with the Go the release workflow installs for k8s-api-proxy.

    build_release_binaries.sh runs with GOTOOLCHAIN=local, so an a2a/go.mod
    asking for a newer Go than that fails the kube-agents-mcp build, which is
    optional: the release would publish without the bridge and only warn.
    """

    def test_a2a_go_directive_matches_the_release_toolchain(self):
        workflow = _RELEASE_WORKFLOW.read_text()
        files = _GO_VERSION_FILE.findall(workflow)
        self.assertEqual(files, ["k8s-api-proxy/go.mod"], f"go-version-file in {_RELEASE_WORKFLOW.relative_to(_REPO_ROOT)}")
        line = next(n for n, text in enumerate(workflow.splitlines(), 1) if "go-version-file:" in text)
        proxy, bridge = _go_directive("k8s-api-proxy/go.mod"), _go_directive("a2a/go.mod")
        self.assertEqual(
            bridge,
            proxy,
            f"a2a/go.mod says go {bridge} but k8s-api-proxy/go.mod says go {proxy}. "
            f".github/workflows/release-publish.yml:{line} installs Go from k8s-api-proxy/go.mod "
            "and the release binaries build with GOTOOLCHAIN=local, so a differing a2a directive "
            "drops the kube-agents-mcp archives from the release with only a warning. "
            "Move both directives together.",
        )


if __name__ == "__main__":
    unittest.main()
