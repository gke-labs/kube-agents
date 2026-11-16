"""Every `dorny/paths-filter` step on a pull-request workflow detects changes with git, not the API.

On a `pull_request` event the action lists the changed files through the
GitHub API, `pulls.listFiles` a page at a time, with no retry around any page.
On 2026-10-14 between 01:33 and 01:42 UTC that call failed in four runs across
`a2a-test.yml` and `k8s-operator-test.yml`, two of them re-run by hand, and in
the 5000 pull-request runs around them it failed zero times; the flaky-check
tracker opened gke-labs/kube-agents#2984 and #2983 for the two checks. With
`token: ""` the action diffs the checked-out merge commit against the base SHA
with git instead, the path it already takes on push and merge-queue runs,
after one depth-one fetch of the base SHA from the server the checkout just
used. Nothing else asserts the input: a step added or rewritten without it
would pass every check and bring the API call back. The checkout's depth is
deliberately left alone; `.agents/rules/github_actions.md` says why.
"""

import unittest

from tests.test_workflow_pins_and_fork_guards import WORKFLOWS_DIR, _load, _triggers, _workflow_files

_PULL_REQUEST = "pull_request"
_PATHS_FILTER = "dorny/paths-filter@"
_TOKEN = "token"
# More workflows than this filter this way; a sweep that found fewer saw too little.
_FILTER_STEPS_FLOOR = 3


def _paths_filter_steps():
    """(workflow file name, filter step) for every paths-filter step on a
    `pull_request` workflow."""
    found = []
    for path in _workflow_files(WORKFLOWS_DIR):
        _text, doc = _load(path)
        if doc is None or _PULL_REQUEST not in _triggers(doc):
            continue
        for job in (doc.get("jobs") or {}).values():
            for step in (job or {}).get("steps") or []:
                if str(step.get("uses", "")).startswith(_PATHS_FILTER):
                    found.append((path.name, step))
    return found


class PathsFilterUsesGitDiffTest(unittest.TestCase):
    def test_the_sweep_saw_the_filter_steps(self) -> None:
        self.assertGreaterEqual(len(_paths_filter_steps()), _FILTER_STEPS_FLOOR)

    def test_every_filter_step_on_a_pull_request_workflow_hands_the_action_no_token(self) -> None:
        for name, step in _paths_filter_steps():
            with self.subTest(workflow=name, step=step.get("name")):
                inputs = step.get("with") or {}
                self.assertIn(
                    _TOKEN,
                    inputs,
                    f"{name}: the paths-filter step sets no `token`, so the action's default "
                    "token sends it to the GitHub API on a pull request, with no retry",
                )
                self.assertEqual(
                    "",
                    inputs[_TOKEN],
                    f"{name}: `token` must be the empty string so the action detects "
                    "changes with git diff on a pull request",
                )



if __name__ == "__main__":
    unittest.main()
