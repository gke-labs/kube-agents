"""How the RC pipeline delivers the token minter's environment secrets.

GH_APP_ID and GH_APP_PRIVATE_KEY live on the `rc` GitHub environment. Reaching
them from a called workflow takes two things at once, and having only one of
them fails silently:

  1. the called workflow's job declares `environment: rc`, and
  2. the calling job passes `secrets: inherit`.

An explicit `secrets:` mapping in the caller cannot substitute for (2). A
`uses:` job cannot declare an environment, so the mapping is evaluated in the
caller — where the environment secrets are not visible — and forwards empty
strings. The install then sees an empty GITHUB_APP_ID, skips the minter, and
the run dies later in test_github_token_minting_and_connectivity with an HTTP
502 that names nothing. That shipped once; these tests are so it cannot ship
again without a red build.

Only the workflow wiring is pinned here. The guard in provision_environment.sh
is covered by tests/test_provision_environment.py, which executes the script
rather than reading it.
"""

import pathlib
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_PIPELINE = _WORKFLOWS / "rc-release-pipeline.yml"
_DEPLOY = _WORKFLOWS / "deploy-environment.yml"

# The workflows the RC and nightly pipelines both call, each parameterised on
# `github_environment`. Every one of them has to render that input into its job's
# `environment:` and refuse to default it.
_SHARED_WORKFLOWS = (
    "deploy-environment.yml",
    "teardown-environment.yml",
    "e2e-run.yml",
)


def _jobs(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text())["jobs"]


class RcMinterSecretWiringTest(unittest.TestCase):
    def test_the_deploy_job_is_called_with_secrets_inherit(self):
        job = _jobs(_PIPELINE)["step-2-deploy-env"]
        self.assertEqual(
            job.get("secrets"),
            "inherit",
            "step-2-deploy-env must pass `secrets: inherit`. An explicit mapping is "
            "evaluated in this job, which has no `environment:`, so the rc "
            "environment's GH_APP_ID is forwarded empty and the minter is skipped.",
        )

    def test_the_called_job_declares_the_environment_it_was_asked_for(self):
        """The other half. Inheriting is useless if the job cannot see the environment.

        The deploy workflow serves both the RC and the nightly pipeline, so the
        environment is an input rather than a literal — but it still has to
        render into the job's `environment:` key, and the RC caller still has to
        pass `rc`.
        """
        self.assertEqual(
            _jobs(_DEPLOY)["deploy-environment"].get("environment"),
            "${{ inputs.github_environment }}",
        )
        self.assertEqual(
            _jobs(_PIPELINE)["step-2-deploy-env"]["with"]["github_environment"],
            "rc",
        )

    def test_the_environment_input_has_no_default(self):
        """A `default: rc` would let a nightly caller that omits it hit the RC.

        All three shared workflows, not just the two that touch infrastructure.
        On e2e-run.yml the same slip is quieter and not smaller: the nightly
        matrix would authenticate to the RC project, test the RC's cluster, and
        report green while holding the nightly-environment lock.
        """
        for name in _SHARED_WORKFLOWS:
            with self.subTest(workflow=name):
                spec = yaml.safe_load((_WORKFLOWS / name).read_text())[True]["workflow_call"][
                    "inputs"
                ]["github_environment"]
                self.assertTrue(spec.get("required"))
                self.assertNotIn("default", spec)

    def test_every_shared_workflow_renders_the_environment_from_the_input(self):
        """A literal `environment: rc` defeats the input without failing anything."""
        for name in _SHARED_WORKFLOWS:
            with self.subTest(workflow=name):
                jobs = _jobs(_WORKFLOWS / name)
                self.assertEqual(len(jobs), 1, f"{name} grew a second job; check its environment too")
                job = next(iter(jobs.values()))
                self.assertEqual(job.get("environment"), "${{ inputs.github_environment }}")

    def test_the_called_job_reads_both_app_secrets_from_the_environment(self):
        """Pins what the inherit is for, so a rename cannot quietly orphan it."""
        body = _DEPLOY.read_text()
        for secret in ("secrets.GH_APP_ID", "secrets.GH_APP_PRIVATE_KEY"):
            self.assertIn(secret, body)


if __name__ == "__main__":
    unittest.main()
