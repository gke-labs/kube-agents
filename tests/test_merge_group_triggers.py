"""Every workflow behind a required check on `main` declares `merge_group`.

A GitHub merge queue runs the required checks on its own temporary branch and
waits for each one to report there. A required check whose workflow has no
`merge_group` trigger never reports, and the queue holds every group until the
status-check timeout. Nothing else notices the trigger going missing: a
workflow edited without it still runs on every pull request exactly as before.
This roster is the ten required contexts as of the Tide-to-merge-queue
migration (gke-labs/kube-agents#1363), less `cla/google`, which is an external
app's commit status rather than a workflow.
"""

import pathlib
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"

_MERGE_GROUP = "merge_group"
_REQUIRED_CHECK_WORKFLOWS = (
    "actionlint.yml",
    "docker-build.yml",
    "docs-check.yml",
    "k8s-operator-test.yml",
    "prettier.yml",
    "python-tests.yml",
    "validate-pr-title.yml",
    "validate.yml",
)


def _on(path: pathlib.Path):
    doc = yaml.safe_load(path.read_text())
    # PyYAML reads an unquoted `on:` key as the boolean True (YAML 1.1).
    return doc.get("on", doc.get(True))


def _triggers(path: pathlib.Path) -> set[str]:
    on = _on(path)
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {str(item) for item in on}
    return {str(key) for key in on}


_PULL_REQUEST = "pull_request"
_RELEASE_LINE_GLOB = "release/**"


class MergeGroupTriggerTest(unittest.TestCase):
    def test_required_check_workflows_run_on_merge_group(self) -> None:
        for name in _REQUIRED_CHECK_WORKFLOWS:
            with self.subTest(workflow=name):
                self.assertIn(_MERGE_GROUP, _triggers(_WORKFLOWS / name))

    def test_required_check_workflows_run_for_pull_requests_against_a_release_line(self) -> None:
        """A pull request against a `release/` branch must be able to earn the same contexts.

        Tide reads the target branch's protection, and protection that requires
        a context a workflow never posts holds the pull request forever. A
        workflow whose `pull_request` trigger is filtered to `main` never posts
        on such a pull request. A filter is allowed; one that omits the release
        branches is not.
        """
        for name in _REQUIRED_CHECK_WORKFLOWS:
            with self.subTest(workflow=name):
                on = _on(_WORKFLOWS / name)
                self.assertIn(_PULL_REQUEST, on, f"{name} does not run on pull_request")
                branches = (on[_PULL_REQUEST] or {}).get("branches")
                if branches is not None:
                    self.assertIn(_RELEASE_LINE_GLOB, branches, f"{name} filters pull_request to {branches}")

    def test_merge_group_stays_on_main(self) -> None:
        """The merge queue is `main`'s; a release line merges through Tide alone."""
        for name in _REQUIRED_CHECK_WORKFLOWS:
            with self.subTest(workflow=name):
                on = _on(_WORKFLOWS / name)
                self.assertEqual((on[_MERGE_GROUP] or {}).get("branches"), ["main"])


if __name__ == "__main__":
    unittest.main()
