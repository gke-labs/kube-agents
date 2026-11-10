"""The Pages deploy step in docs-deploy.yml runs once more after a failure.

    python3 -m unittest tests.test_docs_deploy_retry

The Pages service now and then refuses or times out a deployment that a later
attempt completes. The deploy job's comment in the workflow holds the record;
this file holds the shape, because deleting the retry fails no deploy and
reports green until the next bad minute.

The shape is the one tests/test_docker_build_retry.py holds for docker-build.yml,
read by its checker, and the pause runs that workflow's pause script, which its
test runs against a stub `sleep`. The environment URL reads both attempts,
because a first attempt refused at create sets no page_url.
"""

import pathlib
import unittest

import yaml

from tests.test_docker_build_retry import (
    _FAILED_STEP_VARIABLE,
    _PAUSE_VARIABLE,
    _uses_retried_action,
    check_retries,
)

_WORKFLOWS = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows"
_WORKFLOW = _WORKFLOWS / "docs-deploy.yml"
_JOB = "deploy"
_DOCKER_BUILD = _WORKFLOWS / "docker-build.yml"
_DOCKER_BUILD_JOB = "build"
_RETRIED_ACTIONS = ("actions/deploy-pages@",)
_PAGE_URL_OUTPUT = "steps.{}.outputs.page_url"


def _job(path, name):
    return yaml.safe_load(path.read_text())["jobs"][name]


def _pause_scripts(job):
    return {step["run"] for step in job["steps"] if _FAILED_STEP_VARIABLE in step.get("env", {})}


class DocsDeployRetryTest(unittest.TestCase):
    def setUp(self):
        self.job = _job(_WORKFLOW, _JOB)

    def test_the_deploy_step_retries(self):
        result = check_retries(self.job["steps"], exempt=frozenset(), actions=_RETRIED_ACTIONS)
        self.assertEqual(result, ([], ["Deploy to GitHub Pages"]))

    def test_pause_is_a_positive_integer(self):
        value = str(self.job.get("env", {}).get(_PAUSE_VARIABLE, ""))
        self.assertTrue(value.isdigit() and int(value) > 0, value)

    def test_pause_runs_the_docker_build_script(self):
        docker_build = _job(_DOCKER_BUILD, _DOCKER_BUILD_JOB)
        self.assertEqual(_pause_scripts(self.job), _pause_scripts(docker_build))

    def test_environment_url_reads_both_attempts(self):
        steps = self.job["steps"]
        ids = [step.get("id") for step in steps if _uses_retried_action(step, _RETRIED_ACTIONS)]
        self.assertEqual(len(ids), 2, ids)
        for step_id in ids:
            self.assertIn(_PAGE_URL_OUTPUT.format(step_id), self.job["environment"]["url"])


if __name__ == "__main__":
    unittest.main()
