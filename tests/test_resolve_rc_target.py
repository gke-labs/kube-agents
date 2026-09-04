"""`hack/resolve-rc-target.sh` names the candidate an eval run is measured on.

Its stdout is consumed as `RC_COMMIT_SHA="$(hack/resolve-rc-target.sh)"`, so
the contract is narrower than "prints something useful": stdout carries the
commit SHA and nothing else, and every diagnostic goes to stderr. A summary
line that drifts onto stdout does not fail — it silently becomes part of the
SHA the Prow job then tries to check out.

The rest is which candidate it picks and when it refuses. Refusing matters
because the images are somebody else's to publish: `docker-publish-ghcr.yml`
runs on every push to main with no paths filter, and a queued or failed run
leaves a main commit with no images at all, which `helm --wait` would otherwise
turn into a 15-minute ImagePullBackOff that reads as a broken chart.
"""

import pathlib
import subprocess
import tempfile
import unittest

from tests.testing.common import (
    MOCK_DEFAULT_REGISTRY_PREFIX,
    create_minimal_tools_bin,
    create_mock_git_repo,
    get_isolated_test_env,
)
from tests.testing.release import create_mock_ghcr_curl_binary

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "hack" / "resolve-rc-target.sh"

_OLDER_RC = "rc_2609010800_aaaaaaa"
_NEWER_RC = "rc_2609021200_bbbbbbb"
_NEWEST_RC = "rc_2609030900_ccccccc"
# A sibling of _NEWEST_RC on the same commit, which is how rc-tag-validated.yml
# writes it: the candidate keeps its bare tag forever and gains a marker beside
# it. A fixture that made the marker a replacement would let a resolver that
# walked back to an older candidate look correct.
_NEWEST_RC_MARKER = f"{_NEWEST_RC}_validated"


class ResolveRcTargetTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = pathlib.Path(self._tmp.name)
        # A str, not `root`: create_mock_git_repo takes `temp_dir.name` from
        # anything that has one, and a pathlib.Path has a `.name` — the last
        # component. Handing it the Path builds the mock repo at a relative
        # `tmpXXXXXXXX/repo`, so the fixtures land in whatever directory the
        # suite was started from and no cleanup ever removes them.
        _, self.repo, self.git = create_mock_git_repo(self._tmp.name)
        self.bin_dir = create_minimal_tools_bin(root)
        # The script probes through common.sh's `registry_image_exists`, which
        # falls back to the GHCR API when there is no docker — and there is
        # none in a `create_minimal_tools_bin` PATH, which is the Prow job
        # image's shape too.
        create_mock_ghcr_curl_binary(self.bin_dir)

        # Three candidates on three commits, the newest already validated.
        self.shas = {}
        for tag in (_OLDER_RC, _NEWER_RC, _NEWEST_RC):
            (pathlib.Path(self.repo) / f"{tag}.txt").write_text(tag)
            self.git("add", "-A")
            self.git("commit", "-m", f"feat: {tag}")
            self.git("tag", tag)
            self.shas[tag] = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("tag", _NEWEST_RC_MARKER)

    def run_script(self, env=None, manifest_status=0):
        if manifest_status:
            create_mock_ghcr_curl_binary(self.bin_dir, manifest_status=manifest_status)
        overrides = {
            "PATH": str(self.bin_dir),
            "REGISTRY_PREFIX": MOCK_DEFAULT_REGISTRY_PREFIX,
        }
        overrides.update(env or {})
        return subprocess.run(
            ["bash", str(_SCRIPT)],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(overrides=overrides),
            cwd=self.repo,
        )

    def test_stdout_is_the_commit_sha_and_nothing_else(self):
        """`$(...)` captures stdout whole. A summary line that lands there
        becomes part of the SHA rather than an extra line somebody notices."""
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), self.shas[_NEWEST_RC])
        self.assertEqual(len(result.stdout.strip().splitlines()), 1)
        self.assertIn("RELEASE CANDIDATE EVAL TARGET", result.stderr)

    def test_the_newest_candidate_is_the_default(self):
        """Newest, not newest-unvalidated. Walking back to an older candidate
        because the newest carries a marker would grade a commit the pipeline
        has already superseded."""
        result = self.run_script()
        self.assertEqual(result.stdout.strip(), self.shas[_NEWEST_RC])
        self.assertIn(_NEWEST_RC, result.stderr)

    def test_a_marker_is_reported_and_does_not_change_the_target(self):
        """The `_validated` marker sits beside the candidate tag on the same
        commit, so the filter that drops markers from the candidate list must
        not be read as dropping validated candidates."""
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), self.shas[_NEWEST_RC])
        self.assertIn("already carries a validation marker", result.stderr)

    def test_a_marker_tag_is_never_itself_the_target(self):
        """`rc_*_validated` sorts above its own candidate under -v:refname, so
        an unfiltered list would hand the marker's name to `git rev-parse` and
        resolve — silently naming the target after a tag that records an
        outcome rather than a candidate."""
        result = self.run_script()
        self.assertNotIn(f"Release Tag:    {_NEWEST_RC_MARKER}", result.stderr)
        self.assertIn(f"Release Tag:    {_NEWEST_RC}", result.stderr)

    def test_an_explicit_tag_wins(self):
        result = self.run_script(env={"RC_TAG": _OLDER_RC})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), self.shas[_OLDER_RC])

    def test_an_unknown_tag_is_an_error_not_a_fallback(self):
        result = self.run_script(env={"RC_TAG": "rc_2609021200_nosuch"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot resolve a commit", result.stderr)
        self.assertEqual(result.stdout.strip(), "")

    def test_a_commit_with_no_published_images_is_refused(self):
        result = self.run_script(manifest_status=1)
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing at least one of the required images", result.stderr)
        # The per-image breakdown, so the reader knows whether this is one
        # unpublished image or a publish run that never started.
        self.assertIn("platform-agent", result.stderr)
        self.assertEqual(result.stdout.strip(), "")

    def test_a_repository_with_no_candidates_says_so(self):
        """The marker tag is deliberately left in place. `rc_*` matches it, so
        a filter that only sorted would resolve `..._validated` here and report
        a target where there is no candidate at all."""
        for tag in (_OLDER_RC, _NEWER_RC, _NEWEST_RC):
            self.git("tag", "-d", tag)
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no rc_* tag found", result.stderr)
        self.assertEqual(result.stdout.strip(), "")

    def test_the_output_file_carries_the_tag_and_the_sha(self):
        out = pathlib.Path(self._tmp.name) / "rc-target.env"
        result = self.run_script(env={"RC_TARGET_OUTPUT": str(out)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            out.read_text().splitlines(),
            [f"rc_tag={_NEWEST_RC}", f"rc_commit_sha={self.shas[_NEWEST_RC]}"],
        )


if __name__ == "__main__":
    unittest.main()
