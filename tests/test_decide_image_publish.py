"""Unit tests for scripts/release/decide_image_publish.sh.

The `decide` job of docker-publish-ghcr.yml runs it: `main` always builds, and
a release branch builds only for a merge Tide pushed and never for a commit
whose images already exist. The pusher rule is also what keeps the GA tagger's
stamped commit out, since the tagger pushes as the release App. The second rule is
the one with teeth: image tags are mutable, so a line opened at a commit `main`
already built would otherwise replace the manifests its validation was earned
against. The registry probes are common.sh's docker-free GHCR and Artifact Registry paths, answered by
the mock curl.
"""

import pathlib
import subprocess
import tempfile
import unittest

from tests.testing.common import MOCK_DEFAULT_REGISTRY_PREFIX, create_minimal_tools_bin, get_isolated_test_env
from tests.testing.release import (
    MOCK_AR_REGISTRY_PREFIX,
    create_mock_ar_curl_binary,
    create_mock_ghcr_curl_binary,
    create_mock_gcloud_token_binary,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "release" / "decide_image_publish.sh"

_MAIN_REF = "refs/heads/main"
_LINE_REF = "refs/heads/release/0.8"
_SHA = "0123456789abcdef0123456789abcdef01234567"
_MERGER = "google-oss-prow[bot]"
_RELEASE_APP = "kube-agents-release-bot[bot]"
_MOCK_CURL_MISSING_IMAGE_EXIT = 1


class DecideImagePublishTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = pathlib.Path(self._tmp.name)
        self.bin_dir = create_minimal_tools_bin(root)
        self.output = root / "github_output.txt"

    def run_script(self, ref, actor=_MERGER, subject="fix: a backport", images_exist=False, **curl):
        create_mock_ghcr_curl_binary(
            self.bin_dir,
            manifest_status=0 if images_exist else _MOCK_CURL_MISSING_IMAGE_EXIT,
            **curl,
        )
        self.output.write_text("")
        env = get_isolated_test_env(
            overrides={
                "PATH": str(self.bin_dir),
                "REGISTRY_PREFIX": MOCK_DEFAULT_REGISTRY_PREFIX,
                "GITHUB_REF": ref,
                "GITHUB_SHA": _SHA,
                "GITHUB_ACTOR": actor,
                "GITHUB_OUTPUT": str(self.output),
            }
        )
        proc = subprocess.run(["bash", str(_SCRIPT)], capture_output=True, text=True, env=env, cwd=_REPO_ROOT)
        outputs = dict(line.split("=", 1) for line in self.output.read_text().splitlines() if "=" in line)
        return proc, outputs

    def test_main_always_builds_even_when_images_exist(self):
        proc, outputs = self.run_script(_MAIN_REF, actor="someone", images_exist=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(outputs["build"], "true")

    def test_a_merge_onto_a_release_branch_with_no_images_builds(self):
        proc, outputs = self.run_script(_LINE_REF)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(outputs["build"], "true")

    def test_a_push_by_anyone_but_the_merger_does_not_build_on_a_release_branch(self):
        proc, outputs = self.run_script(_LINE_REF, actor="a-collaborator")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(outputs["build"], "false")
        self.assertIn("only merges pushed by", outputs["reason"])

    def test_the_ga_taggers_stamp_push_is_refused_by_the_pusher_rule(self):
        """The tagger pushes the stamped release commit as the release App, not as Tide,
        so it never builds; no subject test is needed, and none is made."""
        proc, outputs = self.run_script(_LINE_REF, actor=_RELEASE_APP, subject="chore(release): stamp release version 0.8.1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(outputs["build"], "false")
        self.assertIn("only merges pushed by", outputs["reason"])

    def test_a_tide_merge_builds_whatever_its_subject_says(self):
        """A Tide squash subject is a backport by definition, stamp words or not."""
        for subject in ("chore(release): stamp release version 0.8.1 (#2200)", "fix: a backport (#2201)"):
            with self.subTest(subject=subject):
                proc, outputs = self.run_script(_LINE_REF, subject=subject)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(outputs["build"], "true")

    def test_a_commit_whose_images_exist_is_never_rebuilt_on_a_release_branch(self):
        proc, outputs = self.run_script(_LINE_REF, images_exist=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(outputs["build"], "false")
        self.assertIn("already exist", outputs["reason"])

    def test_a_registry_that_cannot_be_asked_fails_the_decision_rather_than_building(self):
        """A probe error is neither "present" nor "absent"; guessing "absent" would rebuild.

        Staged two ways: the token call failing outright, and the manifest call
        answering something other than 200 or 404.
        """
        for curl in ({"token_exit": 7}, {"manifest_http_status": 503}):
            with self.subTest(curl=curl):
                proc, outputs = self.run_script(_LINE_REF, **curl)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("refusing to build", proc.stderr)
                self.assertNotIn("build", outputs)

    def _run_against_artifact_registry(self, http_status):
        """A release-branch merge decided with REGISTRY_PREFIX on an Artifact
        Registry repository, asked as the job's Google identity."""
        _, log_path, _ = create_mock_ar_curl_binary(self.bin_dir, manifest_http_status=http_status)
        create_mock_gcloud_token_binary(self.bin_dir)
        self.output.write_text("")
        env = get_isolated_test_env(
            overrides={
                "PATH": str(self.bin_dir),
                "REGISTRY_PREFIX": MOCK_AR_REGISTRY_PREFIX,
                "GITHUB_REF": _LINE_REF,
                "GITHUB_SHA": _SHA,
                "GITHUB_ACTOR": _MERGER,
                "GITHUB_OUTPUT": str(self.output),
            }
        )
        proc = subprocess.run(["bash", str(_SCRIPT)], capture_output=True, text=True, env=env, cwd=_REPO_ROOT)
        outputs = dict(line.split("=", 1) for line in self.output.read_text().splitlines() if "=" in line)
        return proc, outputs, pathlib.Path(log_path)

    def test_an_artifact_registry_prefix_is_asked_too(self):
        """The decision is not GHCR-only: present on Artifact Registry is
        "already built", absent there builds."""
        for http_status, build in ((200, "false"), (404, "true")):
            with self.subTest(http_status=http_status):
                proc, outputs, log_path = self._run_against_artifact_registry(http_status)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(outputs["build"], build)
                self.assertIn(f"/v2/mock-project/mock-candidates/platform-agent/manifests/{_SHA}", log_path.read_text())
                log_path.unlink()

    def test_the_candidate_registry_is_the_one_asked_when_it_is_named(self):
        """The workflow pushes to CANDIDATE_REGISTRY_PREFIX and nowhere else, so
        that registry says whether the commit was built; the registry prefix,
        where a release is published, holds no commit image to find."""
        _, log_path, _ = create_mock_ar_curl_binary(self.bin_dir, manifest_http_status=200)
        create_mock_gcloud_token_binary(self.bin_dir)
        self.output.write_text("")
        env = get_isolated_test_env(
            overrides={
                "PATH": str(self.bin_dir),
                "REGISTRY_PREFIX": MOCK_DEFAULT_REGISTRY_PREFIX,
                "CANDIDATE_REGISTRY_PREFIX": MOCK_AR_REGISTRY_PREFIX,
                "GITHUB_REF": _LINE_REF,
                "GITHUB_SHA": _SHA,
                "GITHUB_ACTOR": _MERGER,
                "GITHUB_OUTPUT": str(self.output),
            }
        )
        proc = subprocess.run(["bash", str(_SCRIPT)], capture_output=True, text=True, env=env, cwd=_REPO_ROOT)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("build=false", self.output.read_text())
        requests = pathlib.Path(log_path).read_text()
        self.assertIn(f"/v2/mock-project/mock-candidates/platform-agent/manifests/{_SHA}", requests)
        self.assertNotIn(MOCK_DEFAULT_REGISTRY_PREFIX.split("/", 1)[0], requests)

    def test_an_artifact_registry_that_refuses_fails_the_decision(self):
        """A 403 from Artifact Registry is "could not ask", as on GHCR: never a build."""
        proc, outputs, _ = self._run_against_artifact_registry(403)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("refusing to build", proc.stderr)
        self.assertNotIn("build", outputs)

    def test_a_push_that_builds_nothing_says_so_outside_the_log(self):
        summary = pathlib.Path(self._tmp.name) / "summary.md"
        summary.write_text("")
        create_mock_ghcr_curl_binary(self.bin_dir, manifest_status=_MOCK_CURL_MISSING_IMAGE_EXIT)
        env = get_isolated_test_env(
            overrides={
                "PATH": str(self.bin_dir),
                "REGISTRY_PREFIX": MOCK_DEFAULT_REGISTRY_PREFIX,
                "GITHUB_REF": _LINE_REF,
                "GITHUB_SHA": _SHA,
                "GITHUB_ACTOR": "a-collaborator",
                "GITHUB_STEP_SUMMARY": str(summary),
            }
        )
        proc = subprocess.run(["bash", str(_SCRIPT)], capture_output=True, text=True, env=env, cwd=_REPO_ROOT)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("::notice::Nothing published for release/0.8@", proc.stdout)
        self.assertIn("build=false", summary.read_text())

    def test_missing_ref_or_sha_is_an_error(self):
        env = get_isolated_test_env(overrides={"PATH": str(self.bin_dir), "GITHUB_REF": _LINE_REF})
        proc = subprocess.run(["bash", str(_SCRIPT)], capture_output=True, text=True, env=env, cwd=_REPO_ROOT)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("GITHUB_REF and GITHUB_SHA are required", proc.stderr)


if __name__ == "__main__":
    unittest.main()
