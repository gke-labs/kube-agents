"""The example compute class VAP reads a CronJob's pod template, not the CronJob's own spec.

`restrict-computeclass-usage-vap.yaml` in the mirrored `gke-compute-classes` skill matches
CronJobs. A CronJob has no `spec.template`; its pod template is at
`spec.jobTemplate.spec.template.spec`. The upstream copy tests `spec.template` first, so for
a CronJob its `podSpec` falls back to the CronJob's own spec and every check passes. Overlay
patch 0001 tests `spec.jobTemplate` first.

The CEL needs an API server to evaluate, so this checks the expression's text, the way
`test_admission_policy_shipped.py` checks the agent-RBAC denials. It fails if a skill sync
resolves a conflict on these lines to upstream's text: `make skills-check` only proves the
shipped skill equals the upstream copy plus the overlay, not what the expression does.

Run:
  python3 -m unittest discover -s tests -p 'test_computeclass_vap_cronjob.py' -v
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
ASSET = "gke-compute-classes/assets/restrict-computeclass-usage-vap.yaml"
SHIPPED = REPO_ROOT / "agents/platform/skills" / ASSET
CRONJOB_TEMPLATE_TEST = "has(object.spec.jobTemplate)"
CONTROLLER_TEMPLATE_TEST = "has(object.spec.template)"


def policy(path: Path) -> dict:
    docs = yaml.safe_load_all(path.read_text(encoding="utf-8"))
    return next(d for d in docs if d and d.get("kind") == "ValidatingAdmissionPolicy")


def pod_spec_expression(path: Path) -> str:
    variables = policy(path)["spec"]["variables"]
    return next(v["expression"] for v in variables if v["name"] == "podSpec")


class CronJobPodTemplateTest(unittest.TestCase):
    def test_the_policy_matches_cronjobs(self):
        rules = policy(SHIPPED)["spec"]["matchConstraints"]["resourceRules"]
        self.assertTrue(any("cronjobs" in r["resources"] for r in rules))

    def test_pod_spec_tests_the_cronjob_template_before_the_controller_template(self):
        expression = pod_spec_expression(SHIPPED)
        self.assertIn(CRONJOB_TEMPLATE_TEST, expression)
        self.assertIn(CONTROLLER_TEMPLATE_TEST, expression)
        self.assertLess(
            expression.index(CRONJOB_TEMPLATE_TEST),
            expression.index(CONTROLLER_TEMPLATE_TEST),
            "podSpec must test spec.jobTemplate first: a CronJob has no spec.template, "
            "so testing that first falls back to the CronJob's own spec",
        )


if __name__ == "__main__":
    unittest.main()
