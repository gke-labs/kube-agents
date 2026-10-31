"""Tests for .github/workflows/docs-deploy.yml GA-stamped script staging and verification.

GitHub Pages serves `install.sh`, `uninstall.sh`, and `upgrade.sh` at the site
root (`https://gke-labs.github.io/kube-agents/<script>`). On `main`,
`BAKED_RELEASE_VERSION` is empty (`""`), and `release-publish.yml` pushes
`refs/tags/<X.Y.Z>` before promoting container images and publishing the Helm
chart, so `docs-deploy.yml` resolves the latest completed, published GA GitHub
Release (`X.Y.Z`) and extracts the stamped scripts from that tag.

These tests parse `.github/workflows/docs-deploy.yml` and execute the actual
`run:` shell blocks from its staging and post-build verification steps against
temporary shallow Git clones and fake `gh api` release payloads so the exact
workflow logic is exercised end to end.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "docs-deploy.yml"
STAGE_STEP_ID = "stage_scripts"
VERIFY_STEP_ID = "verify_scripts"
CHECKOUT_USES_PREFIX = "actions/checkout@"
BUILD_RUN_CMD = "npm run build"
UPLOAD_USES_PREFIX = "actions/upload-pages-artifact@"
INSTALLER_SCRIPTS = ("install.sh", "uninstall.sh", "upgrade.sh")
UPSTREAM_REPO = "gke-labs/kube-agents"
EXPRESSION_RE = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)


def _load_workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test Bot")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "commit.gpgSign", "false")
    _git(repo, "config", "tag.gpgSign", "false")


def _write_scripts(repo: Path, baked_version: str, marker: str = "") -> None:
    for name in INSTALLER_SCRIPTS:
        script_path = repo / name
        script_path.write_text(
            f'#!/usr/bin/env bash\n# marker={marker}\nBAKED_RELEASE_VERSION="{baked_version}"\n',
            encoding="utf-8",
        )
        script_path.chmod(0o755)


def _create_stamped_ga_tag(repo: Path, version: str, annotated: bool = True) -> None:
    """Mimics tag_ga_release.sh: a detached stamped commit off main carrying tag X.Y.Z."""
    main_sha = _git(repo, "rev-parse", "main").strip()
    _git(repo, "checkout", "-q", "--detach", main_sha)
    _write_scripts(repo, baked_version=version, marker=f"release-{version}")
    _git(repo, "add", *INSTALLER_SCRIPTS)
    _git(repo, "commit", "-q", "-m", f"chore(release): stamp release version {version}")
    if annotated:
        _git(repo, "tag", "-a", version, "-m", f"Release {version}")
    else:
        _git(repo, "tag", version)
    _git(repo, "checkout", "-q", "main")


def _shallow_checkout_without_tags(remote_repo: Path, dest: Path) -> Path:
    """Mimics `actions/checkout` default: depth-1 fetch of main with no tags."""
    _init_git_repo(dest)
    _git(dest, "remote", "add", "origin", f"file://{remote_repo}")
    _git(dest, "fetch", "-q", "--no-tags", "--depth=1", "origin", "main")
    _git(dest, "checkout", "-q", "FETCH_HEAD")
    site_public = dest / "docs" / "site" / "public"
    site_dist = dest / "docs" / "site" / "dist"
    site_public.mkdir(parents=True, exist_ok=True)
    site_dist.mkdir(parents=True, exist_ok=True)
    return dest


def _install_fake_gh(
    bin_dir: Path,
    releases_by_repo: dict[str, list[dict]],
    fail_repos: set[str] | None = None,
) -> Path:
    """Writes a fake `gh` binary that serves `gh api --paginate repos/<repo>/releases?... --jq <expr>`."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    payload_file = bin_dir / "releases_fixture.json"
    payload_file.write_text(
        json.dumps(
            {
                "releases": releases_by_repo,
                "fail_repos": sorted(fail_repos or set()),
            }
        ),
        encoding="utf-8",
    )
    gh_bin = bin_dir / "gh"
    gh_bin.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, re, shutil, subprocess, sys\n"
        f"fixture = json.loads(open({str(payload_file)!r}, encoding='utf-8').read())\n"
        "args = sys.argv[1:]\n"
        "if not args or args[0] != 'api':\n"
        "    sys.stderr.write(f'unexpected gh invocation: {args!r}\\n')\n"
        "    sys.exit(2)\n"
        "endpoint = None\n"
        "jq_expr = None\n"
        "i = 1\n"
        "while i < len(args):\n"
        "    if args[i] == '--paginate':\n"
        "        i += 1\n"
        "    elif args[i] == '--jq':\n"
        "        jq_expr = args[i + 1]\n"
        "        i += 2\n"
        "    else:\n"
        "        endpoint = args[i]\n"
        "        i += 1\n"
        "m = re.match(r'^repos/([^/]+/[^/]+)/releases(?:\\?.*)?$', endpoint or '')\n"
        "if not m:\n"
        "    sys.stderr.write(f'unexpected endpoint: {endpoint!r}\\n')\n"
        "    sys.exit(2)\n"
        "repo = m.group(1)\n"
        "if repo in fixture['fail_repos']:\n"
        "    sys.stderr.write(f'gh: HTTP 500 fetching releases for {repo}\\n')\n"
        "    sys.exit(1)\n"
        "items = fixture['releases'].get(repo, [])\n"
        "raw = json.dumps(items)\n"
        "if not jq_expr:\n"
        "    sys.stderr.write('expected --jq expression\\n')\n"
        "    sys.exit(2)\n"
        "if not shutil.which('jq'):\n"
        "    sys.stderr.write('jq binary required for fake gh --jq evaluation\\n')\n"
        "    sys.exit(2)\n"
        "proc = subprocess.run(['jq', '-r', jq_expr], input=raw, text=True)\n"
        "sys.exit(proc.returncode)\n",
        encoding="utf-8",
    )
    gh_bin.chmod(0o755)
    return bin_dir


class DocsDeployWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = _load_workflow()
        self.triggers = self.workflow.get(True, self.workflow.get("on", {}))
        self.build_job = self.workflow["jobs"]["build"]
        self.steps = self.build_job["steps"]

    def _find_step_by_id(self, step_id: str) -> tuple[int, dict]:
        for idx, step in enumerate(self.steps):
            if step.get("id") == step_id:
                return idx, step
        self.fail(f"step with id={step_id!r} not found in build job")

    def _find_checkout_step(self) -> tuple[int, dict]:
        for idx, step in enumerate(self.steps):
            if str(step.get("uses", "")).startswith(CHECKOUT_USES_PREFIX):
                return idx, step
        self.fail(f"step with uses starting {CHECKOUT_USES_PREFIX!r} not found in build job")

    def _find_build_step(self) -> tuple[int, dict]:
        for idx, step in enumerate(self.steps):
            if str(step.get("run", "")).strip() == BUILD_RUN_CMD:
                return idx, step
        self.fail(f"step with run={BUILD_RUN_CMD!r} not found in build job")

    def _find_upload_step(self) -> tuple[int, dict]:
        for idx, step in enumerate(self.steps):
            if str(step.get("uses", "")).startswith(UPLOAD_USES_PREFIX):
                return idx, step
        self.fail(f"step with uses starting {UPLOAD_USES_PREFIX!r} not found in build job")

    def _run_step_by_id(
        self,
        step_id: str,
        checkout_root: Path,
        extra_env: dict[str, str] | None = None,
        prepend_path: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        _, step = self._find_step_by_id(step_id)
        workdir = checkout_root / step.get("working-directory", ".")
        env = os.environ.copy()
        for key in (
            "GITHUB_OUTPUT",
            "GITHUB_REPOSITORY",
            "REPO_NAME",
            "GH_TOKEN",
            "UPSTREAM_REPO_URL",
            "EXPECTED_GA_TAG",
        ):
            env.pop(key, None)
        if prepend_path is not None:
            env["PATH"] = f"{prepend_path}{os.pathsep}{env.get('PATH', '')}"
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", "-euo", "pipefail", "-c", step["run"]],
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_step_order_and_safe_expression_passing(self) -> None:
        checkout_idx, checkout_step = self._find_checkout_step()
        stage_idx, stage_step = self._find_step_by_id(STAGE_STEP_ID)
        build_idx, _ = self._find_build_step()
        verify_idx, verify_step = self._find_step_by_id(VERIFY_STEP_ID)
        upload_idx, _ = self._find_upload_step()

        self.assertLess(checkout_idx, stage_idx)
        self.assertIs(checkout_step.get("with", {}).get("persist-credentials"), False)
        self.assertLess(stage_idx, build_idx)
        self.assertLess(build_idx, verify_idx)
        self.assertLess(verify_idx, upload_idx)

        self.assertEqual(
            stage_step.get("env", {}).get("GH_TOKEN"),
            "${{ github.token }}",
        )
        self.assertEqual(
            stage_step.get("env", {}).get("REPO_NAME"),
            "${{ github.repository }}",
        )
        self.assertEqual(
            verify_step.get("env", {}).get("EXPECTED_GA_TAG"),
            "${{ steps.stage_scripts.outputs.ga_tag }}",
        )
        # No ${{ ... }} expression interpolation inside shell `run:` bodies.
        self.assertIsNone(EXPRESSION_RE.search(stage_step["run"]))
        self.assertIsNone(EXPRESSION_RE.search(verify_step["run"]))

    def test_push_paths_do_not_trigger_on_unstamped_main_installer_edits(self) -> None:
        push_paths = self.triggers["push"]["paths"]
        self.assertIn("docs/site/**", push_paths)
        self.assertIn(".github/workflows/docs-deploy.yml", push_paths)
        for script in INSTALLER_SCRIPTS:
            self.assertNotIn(script, push_paths)

    def test_stages_highest_published_ga_release_and_ignores_unpromoted_tags_drafts_and_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            origin = tmp_path / "origin"
            _init_git_repo(origin)
            _write_scripts(origin, baked_version="", marker="main-initial")
            _git(origin, "add", *INSTALLER_SCRIPTS)
            _git(origin, "commit", "-q", "-m", "initial main")

            # Cut 0.9.0, 0.10.0, and a mid-flight 0.11.0 whose Git tag was pushed by
            # tag_ga_release.sh before GHCR image promotion / GitHub Release creation.
            _create_stamped_ga_tag(origin, "0.9.0", annotated=True)
            _create_stamped_ga_tag(origin, "0.10.0", annotated=True)
            _create_stamped_ga_tag(origin, "0.11.0", annotated=True)
            _create_stamped_ga_tag(origin, "0.12.0", annotated=True)

            # Advance main past all tags with unstamped scripts and candidate tags.
            _write_scripts(origin, baked_version="", marker="main-ahead-unstamped")
            _git(origin, "add", *INSTALLER_SCRIPTS)
            _git(origin, "commit", "-q", "-m", "feat: main moves ahead")
            _git(origin, "tag", "rc_2610260100_abcdef0_validated")
            _git(origin, "tag", "staging_2610260200_abcdef0")
            _git(origin, "tag", "evalcand_2610260200_abcdef0")

            # Only 0.9.0 and 0.10.0 are completed published GitHub Releases;
            # 0.11.0 has no GitHub Release (mid-flight/failed promotion) and
            # 0.12.0 is still a draft, plus pre-release/non-numeric tags exist.
            fake_bin = _install_fake_gh(
                tmp_path / "bin",
                {
                    UPSTREAM_REPO: [
                        {"tag_name": "0.9.0", "draft": False, "prerelease": False},
                        {"tag_name": "0.10.0", "draft": False, "prerelease": False},
                        {"tag_name": "0.12.0", "draft": True, "prerelease": False},
                        {"tag_name": "0.13.0", "draft": False, "prerelease": True},
                        {"tag_name": "v0.99.0", "draft": False, "prerelease": False},
                    ]
                },
            )

            checkout = _shallow_checkout_without_tags(origin, tmp_path / "checkout")
            gh_output = tmp_path / "github_output.txt"

            res = self._run_step_by_id(
                STAGE_STEP_ID,
                checkout,
                {
                    "REPO_NAME": UPSTREAM_REPO,
                    "GITHUB_OUTPUT": str(gh_output),
                },
                prepend_path=fake_bin,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertEqual(gh_output.read_text(encoding="utf-8").strip(), "ga_tag=0.10.0")

            # Simulate Astro copying public/* -> dist/* during `npm run build`.
            public_dir = checkout / "docs" / "site" / "public"
            dist_dir = checkout / "docs" / "site" / "dist"
            for script in INSTALLER_SCRIPTS:
                staged = public_dir / script
                self.assertTrue(staged.is_file())
                self.assertTrue(os.access(staged, os.X_OK))
                content = staged.read_text(encoding="utf-8")
                self.assertIn('BAKED_RELEASE_VERSION="0.10.0"', content)
                self.assertIn("# marker=release-0.10.0", content)
                shutil.copy2(staged, dist_dir / script)

            verify_res = self._run_step_by_id(
                VERIFY_STEP_ID,
                checkout,
                {"EXPECTED_GA_TAG": "0.10.0"},
            )
            self.assertEqual(verify_res.returncode, 0, msg=verify_res.stderr)

    def test_verify_step_rejects_bad_expected_tag_missing_empty_unstamped_or_mismatched_dist_scripts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp) / "checkout"
            dist_dir = checkout / "docs" / "site" / "dist"
            dist_dir.mkdir(parents=True, exist_ok=True)
            for script in INSTALLER_SCRIPTS:
                (dist_dir / script).write_text(
                    '#!/usr/bin/env bash\nBAKED_RELEASE_VERSION="0.19.0"\n',
                    encoding="utf-8",
                )

            # Empty or non-GA EXPECTED_GA_TAG must fail closed (preventing empty
            # EXPECTED_GA_TAG from matching unstamped BAKED_RELEASE_VERSION="").
            for bad_tag in ("", "v0.19.0", "0.19.0-rc.1"):
                with self.subTest(expected_ga_tag=bad_tag):
                    bad_env_res = self._run_step_by_id(
                        VERIFY_STEP_ID,
                        checkout,
                        {"EXPECTED_GA_TAG": bad_tag},
                    )
                    self.assertNotEqual(bad_env_res.returncode, 0)
                    self.assertIn("Invalid or empty EXPECTED_GA_TAG", bad_env_res.stderr)

            # Zero-byte dist/<script>.
            (dist_dir / "install.sh").write_text("", encoding="utf-8")
            empty_file_res = self._run_step_by_id(
                VERIFY_STEP_ID,
                checkout,
                {"EXPECTED_GA_TAG": "0.19.0"},
            )
            self.assertNotEqual(empty_file_res.returncode, 0)
            self.assertIn("Missing or empty built Pages script dist/install.sh", empty_file_res.stderr)

            # Missing dist/<script>.
            (dist_dir / "install.sh").unlink()
            missing_file_res = self._run_step_by_id(
                VERIFY_STEP_ID,
                checkout,
                {"EXPECTED_GA_TAG": "0.19.0"},
            )
            self.assertNotEqual(missing_file_res.returncode, 0)
            self.assertIn("Missing or empty built Pages script dist/install.sh", missing_file_res.stderr)

            # Restore install.sh and test empty stamp on upgrade.sh.
            (dist_dir / "install.sh").write_text(
                '#!/usr/bin/env bash\nBAKED_RELEASE_VERSION="0.19.0"\n',
                encoding="utf-8",
            )
            (dist_dir / "upgrade.sh").write_text(
                '#!/usr/bin/env bash\nBAKED_RELEASE_VERSION=""\n',
                encoding="utf-8",
            )
            unstamped = self._run_step_by_id(
                VERIFY_STEP_ID,
                checkout,
                {"EXPECTED_GA_TAG": "0.19.0"},
            )
            self.assertNotEqual(unstamped.returncode, 0)
            self.assertIn("dist/upgrade.sh", unstamped.stderr)

            # Wrong version stamp.
            (dist_dir / "upgrade.sh").write_text(
                '#!/usr/bin/env bash\nBAKED_RELEASE_VERSION="0.18.0"\n',
                encoding="utf-8",
            )
            mismatch = self._run_step_by_id(
                VERIFY_STEP_ID,
                checkout,
                {"EXPECTED_GA_TAG": "0.19.0"},
            )
            self.assertNotEqual(mismatch.returncode, 0)
            self.assertIn("0.18.0", mismatch.stderr)

    def test_upstream_fails_closed_fork_prefers_own_release_or_falls_back_and_api_errors_abort(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fork_origin = tmp_path / "fork_origin"
            _init_git_repo(fork_origin)
            _write_scripts(fork_origin, baked_version="", marker="fork-main")
            _git(fork_origin, "add", *INSTALLER_SCRIPTS)
            _git(fork_origin, "commit", "-q", "-m", "fork main")
            _create_stamped_ga_tag(fork_origin, "0.19.1", annotated=True)

            upstream_with_tags = tmp_path / "upstream_with_tags"
            _init_git_repo(upstream_with_tags)
            _write_scripts(upstream_with_tags, baked_version="", marker="upstream-main")
            _git(upstream_with_tags, "add", *INSTALLER_SCRIPTS)
            _git(upstream_with_tags, "commit", "-q", "-m", "upstream main")
            _create_stamped_ga_tag(upstream_with_tags, "0.19.0", annotated=True)

            checkout = _shallow_checkout_without_tags(fork_origin, tmp_path / "checkout")

            # Case 1: gke-labs/kube-agents with zero published GA releases must fail closed.
            empty_upstream_bin = _install_fake_gh(
                tmp_path / "bin_empty_upstream",
                {UPSTREAM_REPO: []},
            )
            upstream_res = self._run_step_by_id(
                STAGE_STEP_ID,
                checkout,
                {
                    "REPO_NAME": UPSTREAM_REPO,
                    "UPSTREAM_REPO_URL": f"file://{upstream_with_tags}",
                },
                prepend_path=empty_upstream_bin,
            )
            self.assertNotEqual(upstream_res.returncode, 0)
            self.assertIn("No published GA release", upstream_res.stderr)

            # Case 2: Fork with no published releases of its own falls back to upstream 0.19.0.
            tagless_fork_bin = _install_fake_gh(
                tmp_path / "bin_tagless_fork",
                {
                    "contributor-fork/kube-agents": [],
                    UPSTREAM_REPO: [
                        {"tag_name": "0.19.0", "draft": False, "prerelease": False},
                    ],
                },
            )
            fork_fallback_res = self._run_step_by_id(
                STAGE_STEP_ID,
                checkout,
                {
                    "REPO_NAME": "contributor-fork/kube-agents",
                    "UPSTREAM_REPO_URL": f"file://{upstream_with_tags}",
                },
                prepend_path=tagless_fork_bin,
            )
            self.assertEqual(fork_fallback_res.returncode, 0, msg=fork_fallback_res.stderr)
            for script in INSTALLER_SCRIPTS:
                staged = checkout / "docs" / "site" / "public" / script
                self.assertIn(
                    'BAKED_RELEASE_VERSION="0.19.0"',
                    staged.read_text(encoding="utf-8"),
                )

            # Case 3: Fork with its own published GA release (0.19.1) prefers origin over upstream (0.19.0).
            own_release_fork_bin = _install_fake_gh(
                tmp_path / "bin_own_fork",
                {
                    "contributor-fork/kube-agents": [
                        {"tag_name": "0.19.1", "draft": False, "prerelease": False},
                    ],
                    UPSTREAM_REPO: [
                        {"tag_name": "0.19.0", "draft": False, "prerelease": False},
                    ],
                },
            )
            fork_own_res = self._run_step_by_id(
                STAGE_STEP_ID,
                checkout,
                {
                    "REPO_NAME": "contributor-fork/kube-agents",
                    "UPSTREAM_REPO_URL": f"file://{upstream_with_tags}",
                },
                prepend_path=own_release_fork_bin,
            )
            self.assertEqual(fork_own_res.returncode, 0, msg=fork_own_res.stderr)
            for script in INSTALLER_SCRIPTS:
                staged = checkout / "docs" / "site" / "public" / script
                self.assertIn(
                    'BAKED_RELEASE_VERSION="0.19.1"',
                    staged.read_text(encoding="utf-8"),
                )

            # Case 4: gh api error on a fork must abort loudly rather than silently
            # falling back to upstream's 0.19.0 scripts.
            api_error_bin = _install_fake_gh(
                tmp_path / "bin_api_error",
                {
                    UPSTREAM_REPO: [
                        {"tag_name": "0.19.0", "draft": False, "prerelease": False},
                    ],
                },
                fail_repos={"contributor-fork/kube-agents"},
            )
            api_err_res = self._run_step_by_id(
                STAGE_STEP_ID,
                checkout,
                {
                    "REPO_NAME": "contributor-fork/kube-agents",
                    "UPSTREAM_REPO_URL": f"file://{upstream_with_tags}",
                },
                prepend_path=api_error_bin,
            )
            self.assertNotEqual(api_err_res.returncode, 0)
            self.assertIn("HTTP 500", api_err_res.stderr)

            # Case 5: When origin is https://github.com/... under persist-credentials: false
            # and GH_TOKEN is present, git fetch resets any inherited extraheader and passes
            # a host-scoped basic auth header for origin.
            https_origin = "https://github.com/contributor-fork/kube-agents.git"
            _git(checkout, "remote", "set-url", "origin", https_origin)
            real_git = shutil.which("git")
            self.assertIsNotNone(real_git)
            git_log = tmp_path / "git_fetch_argv.json"
            git_wrapper = own_release_fork_bin / "git"
            git_wrapper.write_text(
                "#!/usr/bin/env python3\n"
                "import json, subprocess, sys\n"
                f"real_git = {real_git!r}\n"
                f"local_url = {f'file://{fork_origin}'!r}\n"
                f"https_origin = {https_origin!r}\n"
                f"log_path = {str(git_log)!r}\n"
                "args = sys.argv[1:]\n"
                "if 'fetch' in args:\n"
                "    open(log_path, 'w', encoding='utf-8').write(json.dumps(args))\n"
                "    args = [local_url if a == 'origin' else a for a in args]\n"
                "sys.exit(subprocess.call([real_git, *args]))\n",
                encoding="utf-8",
            )
            git_wrapper.chmod(0o755)
            auth_res = self._run_step_by_id(
                STAGE_STEP_ID,
                checkout,
                {
                    "REPO_NAME": "contributor-fork/kube-agents",
                    "GH_TOKEN": "unit-test-placeholder",
                },
                prepend_path=own_release_fork_bin,
            )
            self.assertEqual(auth_res.returncode, 0, msg=auth_res.stderr)
            logged_args = json.loads(git_log.read_text(encoding="utf-8"))
            self.assertIn("http.https://github.com/.extraheader=", logged_args)
            self.assertTrue(
                any(
                    a.startswith("http.https://github.com/.extraheader=AUTHORIZATION: basic ")
                    for a in logged_args
                ),
                msg=f"expected AUTHORIZATION extraheader in {logged_args!r}",
            )


if __name__ == "__main__":
    unittest.main()
