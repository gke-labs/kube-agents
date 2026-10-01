"""The seam between the eval driver and the thing that reads its verdict.

`hack/ci-eval-rc.sh` runs inside a Prow job and writes `rc-eval-summary.md` into
that build's artifacts. Hours later and in a different repository's CI,
`scripts/release/poll_rc_eval_verdict.py` downloads the same file and decides
from it whether the candidate reaches the staging cluster. Nothing else connects
them: no shared library, no schema, no import. The contract is one Markdown row,
and each side has its own copy of the word.

That is exactly the kind of seam that stays green in unit tests while being
broken in production. `tests/test_ci_eval_rc.py` asserts the driver writes the
row it means to; `tests/test_poll_rc_eval_verdict.py` asserts the poller parses
the row it expects. Both pass if the two rows are different rows. A reformatted
table, a renamed file, a verdict word retitled to "PASSED" — each is a one-line
change, each keeps both suites green, and each promotes a candidate whose eval
said something else, or withdraws a nomination the eval settled.

So the driver here is the real script (through `RcEvalDriverFixture`, which runs
it in a throwaway repository with its three siblings stubbed), the artifact is
the real file it wrote on disk, and the reader is the real poller module. Only
the GCS transport is faked, by handing the poller a reader that serves the bytes
from the artifacts directory.
"""

import importlib.util
import json
import pathlib
import sys
import unittest

from tests.testing.rc_eval_driver import RcEvalDriverFixture

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_POLLER = _REPO_ROOT / "scripts" / "release" / "poll_rc_eval_verdict.py"

_spec = importlib.util.spec_from_file_location("poll_rc_eval_verdict", _POLLER)
poller = importlib.util.module_from_spec(_spec)
sys.modules["poll_rc_eval_verdict"] = poller
_spec.loader.exec_module(poller)

_BUILD_BASE = "gs://kube-agents-prow/logs/post-kube-agents-eval-rc/1/"


class RcEvalVerdictSeamTest(unittest.TestCase):
    """The real driver's artifact, read by the real poller."""

    def setUp(self):
        self.driver = RcEvalDriverFixture()
        self.addCleanup(self.driver.cleanup)
        self.artifacts = self.driver.artifacts

    def build_repo(self, **kwargs):
        return self.driver.build_repo(**kwargs)

    def run_driver(self, root):
        return self.driver.run_driver(root)

    def _reader(self, *, passed):
        """Serves the build directory the poller would fetch from GCS.

        `finished.json` is synthesised rather than taken from disk because the
        driver does not write it — Prow's decoration does, from the exit status
        the driver returns. Passing that status in is what lets a case assert
        the summary and the status are read in the right order.
        """
        files = {
            poller.FINISHED_FILE: json.dumps({"timestamp": 1, "passed": passed}),
        }
        for path in self.artifacts.rglob("*"):
            if path.is_file():
                key = poller.ARTIFACTS_DIR + str(path.relative_to(self.artifacts))
                files[key] = path.read_text(encoding="utf-8")

        def read_file(url):
            return files.get(url.removeprefix(_BUILD_BASE))

        return read_file

    def _verdict(self, *, passed):
        """The poller's read of this build, with `finished.json` handed in.

        `read_verdict` takes that text as an argument rather than fetching it,
        because in the real poller the sweep has already read it — that read is
        how the sweep knows the build is over. Doing the same here keeps the
        seam honest: the bytes the sweep fetched are the bytes the verdict is
        formed from, and a second fetch the production path does not make is not
        one this test should make on its behalf.
        """
        read_file = self._reader(passed=passed)
        finished = read_file(_BUILD_BASE + poller.FINISHED_FILE)
        return poller.read_verdict(_BUILD_BASE, read_file, finished)

    def test_a_green_eval_reads_back_as_a_promotion(self):
        root, _ = self.build_repo()
        self.assertEqual(self.run_driver(root).returncode, 0)
        self.assertEqual(self._verdict(passed=True), poller.VERDICT_GREEN)

    def test_a_red_eval_reads_back_as_a_rejected_candidate(self):
        """Settled, so the nomination tag stays and the candidate is not retried."""
        root, _ = self.build_repo(eval_exit_code=1)
        self.assertEqual(self.run_driver(root).returncode, 1)
        self.assertEqual(self._verdict(passed=False), poller.VERDICT_RED)

    def test_a_failed_deploy_reads_back_as_no_verdict_at_all(self):
        """The outcome the exit status cannot express, and the reason for the file.

        The driver exits with the deploy's status, so Prow marks the build
        failed — indistinguishable from a red eval to anything reading
        `finished.json`. The candidate was never measured, and treating this as
        a rejection would hold it back for a broken lane.
        """
        root, _ = self.build_repo(deploy_exit_code=3)
        self.assertEqual(self.run_driver(root).returncode, 3)
        self.assertEqual(self._verdict(passed=False), poller.VERDICT_NOT_RUN)

    def test_the_summary_the_driver_writes_is_the_file_the_poller_fetches(self):
        """Both sides name the path independently; a rename on either is silent.

        The poller would read a missing artifact as NOT RUN, which is a plausible
        state rather than an error, so a renamed file withdraws every nomination
        for as long as it takes someone to notice.
        """
        root, _ = self.build_repo()
        self.assertEqual(self.run_driver(root).returncode, 0)
        self.assertTrue(
            (self.artifacts / poller.RC_SUMMARY_FILE).is_file(),
            f"the driver wrote no {poller.RC_SUMMARY_FILE}; the poller fetches "
            f"{poller.ARTIFACTS_DIR + poller.RC_SUMMARY_FILE} and would report not_run",
        )

    def test_a_green_summary_under_a_failed_build_is_not_a_promotion(self):
        """Disagreement is not resolved in the candidate's favour.

        The summary says the catalog passed and Prow says the build failed, so
        something happened after the verdict was written that neither side can
        describe. Reading the summary alone would promote on it.
        """
        root, _ = self.build_repo()
        self.assertEqual(self.run_driver(root).returncode, 0)
        self.assertEqual(self._verdict(passed=False), poller.VERDICT_NOT_RUN)


if __name__ == "__main__":
    unittest.main()
