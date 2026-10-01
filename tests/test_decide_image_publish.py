"""Unit tests for scripts/release/decide_image_publish.sh.

The `decide` job of docker-publish-ghcr.yml runs it: `main` always builds, and
a release branch builds only for a merge Tide pushed and never for a commit
whose images already exist. The pusher rule is also what keeps the GA tagger's
stamped commit out, since the tagger pushes as the release App. The second rule is
the one with teeth: image tags are mutable, so a line opened at a commit `main`
already built would otherwise replace the manifests its validation was earned
against. The registry probe is common.sh's docker-free GHCR path, answered by
the mock curl.
"""

import pathlib
import subprocess
import tempfile
import unittest

from tests.testing.common import MOCK_DEFAULT_REGISTRY_PREFIX, create_minimal_tools_bin, get_isolated_test_env
from tests.testing.release import create_mock_ghcr_curl_binary

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
