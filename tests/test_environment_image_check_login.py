"""The environment workflows' image check logs in to Google only for an Artifact Registry prefix.

Before a workflow tags, deploys or dispatches for a commit it checks that the commit's images
exist in the registry its environment names (`vars.REGISTRY_PREFIX`). On GHCR
that needs no login. An Artifact Registry repository has to be asked as the
environment's deploy identity, so each job listed below may mint
a token and logs in first, behind a condition on the prefix; with the variable
unset both steps are skipped and the job is what it was. This file holds the
wiring. That the login is what the check then uses, on a runner with docker,
is `registry_image_exists` asking Artifact Registry over its API first, held
in tests/test_release_common.py. `release-publish.yml` reads candidate images
too and is not on the list: it does not log in.

A reusable workflow adds one more thing to hold: a called job cannot have a
permission its caller did not grant, and a caller that falls short fails the
whole run at startup, so the caller's grant is checked against the callee's.
"""

import pathlib
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"

_CONDITION = "contains(vars.REGISTRY_PREFIX, '-docker.pkg.dev/')"
_AUTH = "google-github-actions/auth@"
_GCLOUD = "google-github-actions/setup-gcloud@"

# (workflow file, job, the environment whose variables the job reads)
_IMAGE_CHECK_JOBS = (
    ("autopush-deploy.yml", "resolve-candidate", "autopush"),
    ("rc-create-tag.yml", "create-tag", "rc"),
    ("rc-scheduler.yml", "dispatch-if-there-is-work", "rc"),
    ("staging-deploy.yml", "resolve-commit", "staging"),
    ("staging-promotion-pipeline.yml", "step-1-resolve-candidate", "nightly"),
)

# (caller workflow, caller job, called workflow, called job)
_CALLERS = (("rc-release-pipeline.yml", "step-1-create-tag", "rc-create-tag.yml", "create-tag"),)


def _doc(name):
    return yaml.safe_load((_WORKFLOWS / name).read_text())


class EnvironmentImageCheckLoginTest(unittest.TestCase):
    def _steps_reading_the_prefix(self, steps):
        """Indexes of the steps handed the registry prefix: the ones that ask a registry."""
        return [i for i, step in enumerate(steps) if "REGISTRY_PREFIX" in (step.get("env") or {})]

    def test_each_job_logs_in_as_its_environment_before_it_asks_the_registry(self):
        for workflow, job_name, environment in _IMAGE_CHECK_JOBS:
            with self.subTest(workflow=workflow, job=job_name):
                job = _doc(workflow)["jobs"][job_name]
                steps = job["steps"]
                self.assertEqual(job["environment"], environment)
                self.assertEqual(job["permissions"].get("id-token"), "write")
                auth = [i for i, step in enumerate(steps) if str(step.get("uses", "")).startswith(_AUTH)]
                gcloud = [i for i, step in enumerate(steps) if str(step.get("uses", "")).startswith(_GCLOUD)]
                self.assertEqual(len(auth), 1, auth)
                self.assertEqual(len(gcloud), 1, gcloud)
                self.assertEqual(
                    steps[auth[0]]["with"],
                    {
                        "workload_identity_provider": "${{ vars.GCP_WORKLOAD_IDENTITY_PROVIDER }}",
                        "service_account": "${{ vars.GCP_SERVICE_ACCOUNT }}",
                    },
                )
                asking = self._steps_reading_the_prefix(steps)
                self.assertTrue(asking, "no step reads REGISTRY_PREFIX; the job list here is stale")
                self.assertLess(auth[0], gcloud[0])
                self.assertLess(gcloud[0], asking[0], "the login has to come before the first step that asks the registry")

    def test_both_login_steps_are_skipped_unless_the_prefix_is_artifact_registry(self):
        """The prefix condition is on both steps of every job. A job may add
        its own condition in front of it, and only by `&&`: the promotion
        pipeline's check is skipped on a run with nothing to promote, and so
        is its login."""
        for workflow, job_name, _ in _IMAGE_CHECK_JOBS:
            for step in _doc(workflow)["jobs"][job_name]["steps"]:
                uses = str(step.get("uses", ""))
                if uses.startswith(_AUTH) or uses.startswith(_GCLOUD):
                    with self.subTest(workflow=workflow, step=step.get("name")):
                        condition = str(step.get("if"))
                        self.assertEqual(condition.split(" && ")[-1], _CONDITION)
                        self.assertNotIn("||", condition)

    def test_the_promotion_pipelines_login_shares_its_checks_skip(self):
        steps = _doc("staging-promotion-pipeline.yml")["jobs"]["step-1-resolve-candidate"]["steps"]
        check = next(step for step in steps if "REGISTRY_PREFIX" in (step.get("env") or {}))
        for step in steps:
            if str(step.get("uses", "")).startswith((_AUTH, _GCLOUD)):
                with self.subTest(step=step.get("name")):
                    self.assertEqual(step["if"], f"{check['if']} && {_CONDITION}")

    def test_the_check_itself_keeps_its_own_condition_and_its_prefix(self):
        """The login is the only thing gated on the prefix: the check runs for
        every registry, and is handed the environment's prefix."""
        for workflow, job_name, _ in _IMAGE_CHECK_JOBS:
            with self.subTest(workflow=workflow, job=job_name):
                steps = _doc(workflow)["jobs"][job_name]["steps"]
                for index in self._steps_reading_the_prefix(steps):
                    step = steps[index]
                    self.assertEqual(step["env"]["REGISTRY_PREFIX"], "${{ vars.REGISTRY_PREFIX }}")
                    self.assertNotIn("docker.pkg.dev", str(step.get("if", "")))
                    self.assertNotIn("continue-on-error", step)

    def test_a_caller_grants_every_permission_the_called_job_asks_for(self):
        """A reusable workflow's job is capped at what its caller passes; a
        caller that grants less fails the run before any job starts, which no
        unit test of the called workflow alone would show."""
        for caller, caller_job, called, called_job in _CALLERS:
            with self.subTest(caller=caller, called=called):
                granting = _doc(caller)["jobs"][caller_job]
                self.assertIn(called, granting["uses"])
                asked = _doc(called)["jobs"][called_job]["permissions"]
                granted = granting["permissions"]
                for permission, level in asked.items():
                    self.assertEqual(granted.get(permission), level, f"{caller}:{caller_job} must grant {permission}: {level}")

    def test_every_caller_of_a_listed_reusable_workflow_is_checked(self):
        """The caller table above is written by hand; a second caller added
        later has to appear in it."""
        listed = {(caller, called) for caller, _, called, _ in _CALLERS}
        callable_workflows = {
            workflow
            for workflow, _, _ in _IMAGE_CHECK_JOBS
            if "workflow_call" in (_doc(workflow).get("on", _doc(workflow).get(True)) or {})
        }
        found = set()
        for path in sorted(_WORKFLOWS.glob("*.yml")):
            for job in (yaml.safe_load(path.read_text()).get("jobs") or {}).values():
                uses = str((job or {}).get("uses", ""))
                for called in callable_workflows:
                    if uses.endswith(f"/{called}"):
                        found.add((path.name, called))
        self.assertEqual(found, listed)


if __name__ == "__main__":
    unittest.main()
