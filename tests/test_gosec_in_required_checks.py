"""gosec runs inside a required check, and nowhere else.

`main`'s required checks are the `main-merge-queue` ruleset on GitHub, not a file
here (docs/pull-request-workflow.md, "How a change merges"). `Security Scanning`
is not in it, so when its gosec job went red on a merge on 2026-11-10, the merge
queue merged anyway and every pull request rebased onto `main` carried the red
for four hours (gke-labs/kube-agents#5228, #5345). The scan now runs as a step
of the required check for each Go module it covers, gated on that workflow's
change filter like the module's tests, so a finding fails a check no pull
request can merge around. A second gosec outside those workflows would be a red
that blocks nothing again, which is what misled the reader of #5228's checks.
"""

import pathlib
import unittest

import yaml

from tests.test_merge_group_triggers import _REQUIRED_CHECK_WORKFLOWS

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"

#: The command every gosec step runs or builds, whichever form it takes.
_GOSEC = "github.com/securego/gosec/v2/cmd/gosec"
#: The gate the module's test steps already read; a gosec step with no gate
#: would run, and fail, on a pull request that touched no Go.
_FILTER_OUTPUT = "steps.filter.outputs."
#: Go module -> the workflow behind its required check.
_REQUIRED_CHECK_BY_MODULE = {
    "k8s-operator": "k8s-operator-test.yml",
    "k8s-api-proxy": "k8sapiproxy-test.yml",
}


def _gosec_steps(path: pathlib.Path) -> list[dict]:
    jobs = yaml.safe_load(path.read_text()).get("jobs") or {}
    return [
        step
        for job in jobs.values()
        for step in ((job or {}).get("steps") or [])
        if _GOSEC in str(step.get("run", ""))
    ]


class GosecInRequiredChecksTest(unittest.TestCase):
    def test_each_scanned_module_runs_gosec_in_its_required_check(self) -> None:
        for module, name in _REQUIRED_CHECK_BY_MODULE.items():
            with self.subTest(module=module, workflow=name):
                self.assertIn(name, _REQUIRED_CHECK_WORKFLOWS)
                steps = _gosec_steps(_WORKFLOWS / name)
                self.assertEqual(1, len(steps), f"{name}: expected one gosec step, found {len(steps)}")
                self.assertIn(
                    _FILTER_OUTPUT,
                    str(steps[0].get("if", "")),
                    f"{name}: the gosec step is not gated on the change filter",
                )

    def test_gosec_runs_in_no_other_workflow(self) -> None:
        for path in sorted(_WORKFLOWS.glob("*.yml")):
            if path.name in _REQUIRED_CHECK_WORKFLOWS:
                continue
            with self.subTest(workflow=path.name):
                self.assertEqual(
                    [],
                    _gosec_steps(path),
                    f"{path.name}: runs gosec outside a required check, where a red blocks no merge",
                )


if __name__ == "__main__":
    unittest.main()
