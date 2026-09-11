"""Tests for the wiring in .github/workflows/risk_classify.yml.

The classification itself is `scripts/classify_risk.py`, tested beside it.
What this file pins is the one line of YAML that no other test reaches and
that GitHub's scheduler, not the script, acts on: the concurrency group. The
other pull-request workflows here that set one key it by pull request number
alone, so a cleanup that normalises this one to match is the likeliest way
the #1364 hang comes back -- an `edited` run carrying the previous head cancelling the
`synchronize` run for the current one, leaving the current head with a
cancelled `classify` check run that Tide reads as failing.
"""

import pathlib
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "risk_classify.yml"

_PR_NUMBER = "${{ github.event.pull_request.number }}"
_HEAD_SHA = "${{ github.event.pull_request.head.sha }}"


def _load():
    return yaml.safe_load(_WORKFLOW.read_text())


class ConcurrencyGroupTest(unittest.TestCase):
    def setUp(self):
        self.concurrency = _load()["concurrency"]

    def test_group_is_keyed_on_the_head_sha_as_well_as_the_number(self):
        """A run may supersede only runs for its own head; the number alone lets a stale-head run cancel the current one."""
        group = self.concurrency["group"]
        self.assertIn(_PR_NUMBER, group)
        self.assertIn(_HEAD_SHA, group)

    def test_newest_run_for_a_head_still_wins(self):
        """Two events for one head collapse to the newest; per-head keying is not a reason to stop cancelling."""
        self.assertIs(self.concurrency["cancel-in-progress"], True)


if __name__ == "__main__":
    unittest.main()
