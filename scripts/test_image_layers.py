#!/usr/bin/env python3
"""Unit tests for the image layer-budget gate.

Run: cd scripts && python3 -m unittest test_image_layers

The gate itself needs a Docker daemon and a built image, so what is tested here
is everything around that: the budget invariant, the boundary the comparison
sits on, and the two ways the check can report. A gate that passed on an
over-budget image, or that failed on a missing image in a way that read like an
over-budget one, would be worse than no gate -- the failure it exists to catch
only shows up on main, after merge.
"""

import contextlib
import io
import json
import subprocess
import unittest
from unittest import mock

import check_image_layers as checker


class BudgetTest(unittest.TestCase):
    """The budget itself, which is the only number a reader has to trust."""

    def test_budget_is_below_the_driver_ceiling(self):
        """A gate at 128 would fire on builds that are already broken.

        The gap is the room a fix needs: a pull request that trips this should
        still be able to land a consolidation, rather than arriving after the
        publish has started failing.
        """
        self.assertLess(checker.DEFAULT_MAX_LAYERS, checker.OVERLAY2_MAX_DEPTH)

    def test_the_ceiling_is_overlay2s(self):
        """Pinned so a well-meaning bump has to explain itself.

        128 is `maxDepth` in the Docker daemon's overlay2 driver, not a figure
        this repository chose.
        """
        self.assertEqual(checker.OVERLAY2_MAX_DEPTH, 128)


def _inspect_returning(count: int):
    """A `subprocess.run` stand-in answering as `docker image inspect` would."""
    return mock.Mock(
        returncode=0,
        stdout=json.dumps([f"sha256:{i:064x}" for i in range(count)]),
        stderr="",
    )


class LayerCountTest(unittest.TestCase):
    def test_counts_the_layers_docker_reports(self):
        with mock.patch.object(subprocess, "run", return_value=_inspect_returning(114)):
            self.assertEqual(checker.layer_count("image:tag"), 114)

    def test_a_missing_image_is_not_reported_as_an_over_budget_one(self):
        """The two exits must not look alike.

        `docker image inspect` fails the same way for "you did not build it"
        as for anything else, and a developer reading "over budget" after
        forgetting to build would go and consolidate COPYs for nothing.
        """
        failed = mock.Mock(returncode=1, stdout="", stderr="No such image: credential-proxy:latest")
        with mock.patch.object(subprocess, "run", return_value=failed):
            with self.assertRaises(SystemExit) as raised:
                checker.layer_count("credential-proxy:latest")
        message = str(raised.exception)
        self.assertIn("No such image", message)
        self.assertIn("docker build", message)


class MainTest(unittest.TestCase):
    def _run(self, count: int, argv: list[str] | None = None) -> int:
        """Call main() with the docker call stubbed out; its report is not the subject.

        Both streams are swallowed so a passing suite stays readable: the
        failure path prints a paragraph, and four of these run per suite.
        """
        sink = io.StringIO()
        with mock.patch.object(checker, "layer_count", return_value=count), mock.patch(
            "sys.argv", ["check_image_layers.py", *(argv or [])]
        ), mock.patch("shutil.which", return_value="/usr/bin/docker"), contextlib.redirect_stdout(
            sink
        ), contextlib.redirect_stderr(sink):
            return checker.main()

    def test_passes_under_budget(self):
        self.assertEqual(self._run(checker.DEFAULT_MAX_LAYERS - 6), 0)

    def test_passes_exactly_at_the_budget(self):
        """The budget is a ceiling, not a limit to stay strictly under."""
        self.assertEqual(self._run(checker.DEFAULT_MAX_LAYERS), 0)

    def test_fails_one_over(self):
        self.assertEqual(self._run(checker.DEFAULT_MAX_LAYERS + 1), 1)

    def test_max_is_overridable(self):
        self.assertEqual(self._run(50, ["--max", "40"]), 1)
        self.assertEqual(self._run(50, ["--max", "60"]), 0)


if __name__ == "__main__":
    unittest.main()
