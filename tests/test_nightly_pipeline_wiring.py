"""Invariants of the nightly pipeline that only the workflow YAML can carry.

Five of these are failures that would be silent in CI — a green run that did the
wrong thing — which is why they are pinned here rather than left to review:

  * a job pointed at `rc` instead of `nightly` tears down the RC environment,
  * a teardown that `needs` the promotion job is skipped when a tag push fails,
    leaving a GKE cluster billing with nothing on it to diagnose,
  * a hardcoded `rc-environment` concurrency group makes an unrelated workflow
    contend for the release pipeline's cluster,
  * a staging tag shape the redeploy trigger does not match promotes nothing and
    still reports success,
  * a redeploy that deploys the pushed ref's SHA rather than the commit it peels
    to pulls an image tag nothing ever published.
"""

import fnmatch
import pathlib
import subprocess
import unittest

import yaml

from tests.testing.common import create_mock_git_repo, get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_NIGHTLY = _WORKFLOWS / "nightly-pipeline.yml"
_COMMON_SH = _REPO_ROOT / "scripts" / "release" / "common.sh"

_STAGING_REDEPLOYS = (
    "staging-redeploy-agent.yml",
    "staging-redeploy-controller.yml",
    "staging-redeploy-integrations.yml",
)


def _doc(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text())


class NightlyPipelineWiringTest(unittest.TestCase):
    def setUp(self):
        self.doc = _doc(_NIGHTLY)
        self.jobs = self.doc["jobs"]

    def test_it_lands_without_a_schedule(self):
        """Dispatch-only until it has been exercised by hand.

        A cron here would point an untested pipeline at a GCP project on the
        night it merges. Turning the schedule on is its own reviewable change;
        delete this test in that change.
        """
        self.assertNotIn("schedule", self.doc[True])
        self.assertIn("workflow_dispatch", self.doc[True])

    def test_every_called_workflow_targets_the_nightly_environment(self):
        called = {name: job for name, job in self.jobs.items() if "uses" in job}
        self.assertTrue(called, "the pipeline is supposed to call reusable workflows")
        for name, job in called.items():
            with self.subTest(job=name):
                self.assertEqual(job["with"]["github_environment"], "nightly")

    def test_the_resolve_job_binds_the_nightly_environment(self):
        """It reads vars.REGISTRY_PREFIX; unbound, that resolves to empty in silence."""
        self.assertEqual(self.jobs["step-1-resolve-candidate"].get("environment"), "nightly")

    def test_the_promotion_job_runs_and_reports_rather_than_skipping(self):
        """An already-promoted night should show a job that decided, not a gap.

        Gating the job on skip_promotion would collapse the whole thing to
        "skipped" and lose the summary line saying why. The condition sits on the
        steps so the run records the decision it made.
        """
        job = self.jobs["step-4-promote-to-staging"]
        self.assertNotIn("skip_promotion", job["if"])
        step_conditions = [step.get("if", "") for step in job["steps"]]
        self.assertTrue(
            any("skip_promotion" in cond for cond in step_conditions),
            "the skip has to be expressed on the steps instead",
        )

    def test_teardown_does_not_depend_on_the_promotion_job(self):
        """Otherwise a failed tag push strands a GKE cluster with nothing to diagnose.

        A skipped or failed job skips its dependents. Step 4 runs only after a
        green matrix and fails only on credential problems — a missing
        RELEASE_BOT_TOKEN, a rejected push — none of which leave anything on the
        cluster worth looking at. The RC pipeline can afford the same dependency
        because its next scheduled run reclaims the environment within three
        hours; this pipeline has no schedule, so nothing would remove it at all.
        """
        teardown = self.jobs["step-5-teardown-env"]
        self.assertEqual(
            set(teardown["needs"]),
            {"step-1-resolve-candidate", "step-2-deploy-env", "step-3-run-e2e-matrix"},
        )

    def test_teardown_keeps_the_success_gate_on_the_jobs_it_does_depend_on(self):
        """A failed matrix must leave its cluster standing to be examined live."""
        teardown = self.jobs["step-5-teardown-env"]
        self.assertNotIn(
            "always()",
            teardown["if"],
            "always() removes the implicit success() and destroys the environments "
            "a failed run leaves standing for diagnosis",
        )
        self.assertIn("step-3-run-e2e-matrix", teardown["needs"])

    def test_the_promotion_tag_is_pushed_with_the_release_bot_token(self):
        """A tag pushed with GITHUB_TOKEN triggers no workflow, so staging never deploys."""
        checkout = next(
            step
            for step in self.jobs["step-4-promote-to-staging"]["steps"]
            if str(step.get("uses", "")).startswith("actions/checkout@")
        )
        self.assertIn("RELEASE_BOT_TOKEN", checkout["with"]["token"])


class ConcurrencyGroupTest(unittest.TestCase):
    def test_no_workflow_hardcodes_the_rc_environment_lock(self):
        """The lock follows the environment, so nothing contends for a cluster it does not deploy to."""
        for path in sorted(_WORKFLOWS.glob("*.yml")):
            with self.subTest(workflow=path.name):
                doc = _doc(path)
                groups = []
                top = doc.get("concurrency")
                if isinstance(top, dict):
                    groups.append(top.get("group"))
                for job in (doc.get("jobs") or {}).values():
                    job_conc = job.get("concurrency")
                    if isinstance(job_conc, dict):
                        groups.append(job_conc.get("group"))
                self.assertNotIn("rc-environment", groups)


class StagingTagContractTest(unittest.TestCase):
    """The tag the pipeline pushes has to match the tag staging deploys on."""

    def _derived_tag(self) -> str:
        proc = subprocess.run(
            ["bash", "-c", f'source "{_COMMON_SH}"; staging_tag_for_rc "rc_2608241820_b35543c_validated"'],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(),
            cwd=str(_REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_the_derived_tag_matches_every_staging_redeploy_trigger(self):
        tag = self._derived_tag()
        for workflow in _STAGING_REDEPLOYS:
            with self.subTest(workflow=workflow):
                patterns = _doc(_WORKFLOWS / workflow)[True]["push"]["tags"]
                self.assertTrue(
                    any(fnmatch.fnmatch(tag, pattern) for pattern in patterns),
                    f"{tag!r} matches none of {patterns!r}",
                )

    def test_the_promotion_tag_is_annotated(self):
        """Which is what makes the peel below necessary rather than defensive.

        An annotated tag's ref points at a tag object; the push event hands that
        object's SHA to github.sha. If this ever became a lightweight tag the peel
        would still be correct, just redundant.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        head = git("rev-parse", "HEAD").stdout.strip()

        proc = subprocess.run(
            ["bash", "-c", f'source "{_COMMON_SH}"; ensure_git_tag staging_2608241820_b35543c "{head}" "promotion"'],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(),
            cwd=repo_dir,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            git("cat-file", "-t", "staging_2608241820_b35543c").stdout.strip(),
            "tag",
        )

    def test_no_staging_redeploy_passes_the_raw_push_sha_to_the_deploy(self):
        """An annotated tag's ref resolves to the tag object, not to the commit.

        github.sha is the new value of the pushed ref, so on these triggers it is
        that tag object's SHA. It reaches `helm upgrade --set …image.tag`, and
        GHCR images are published under commit SHAs, so the unpeeled value names
        an image that was never built.
        """
        for workflow in _STAGING_REDEPLOYS:
            with self.subTest(workflow=workflow):
                jobs = _doc(_WORKFLOWS / workflow)["jobs"]
                resolve = jobs["resolve-commit"]
                self.assertTrue(
                    any("peel_tag_commit.sh" in step.get("run", "") for step in resolve["steps"]),
                    "resolve-commit is supposed to peel the tag",
                )
                self.assertEqual(jobs["call-deploy"]["needs"], "resolve-commit")
                for key in ("image_tag", "checkout_sha"):
                    value = jobs["call-deploy"]["with"].get(key)
                    if value is None:
                        continue
                    self.assertNotIn("github.sha", value, f"{key} must use the peeled commit")
                    self.assertIn("resolve-commit", value, f"{key} must use the peeled commit")


if __name__ == "__main__":
    unittest.main()
