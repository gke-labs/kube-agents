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


def _triggers(path: pathlib.Path) -> set[str]:
    doc = yaml.safe_load(path.read_text())
    # PyYAML reads an unquoted `on:` key as the boolean True (YAML 1.1).
    on = doc.get("on", doc.get(True))
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {str(item) for item in on}
    return {str(key) for key in on}


class MergeGroupTriggerTest(unittest.TestCase):
    def test_required_check_workflows_run_on_merge_group(self) -> None:
        for name in _REQUIRED_CHECK_WORKFLOWS:
            with self.subTest(workflow=name):
                self.assertIn(_MERGE_GROUP, _triggers(_WORKFLOWS / name))


if __name__ == "__main__":
    unittest.main()
