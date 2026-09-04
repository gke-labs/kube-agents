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

_STAGING_DEPLOY = "staging-deploy.yml"


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

    def test_every_called_workflow_targets_the_environment_it_is_named_for(self):
        """Everything that touches the NIGHTLY cluster has to say `nightly`."""
        called = {name: job for name, job in self.jobs.items() if "uses" in job}
        self.assertTrue(called, "the pipeline is supposed to call reusable workflows")
        for name, job in called.items():
            with self.subTest(job=name):
                self.assertEqual(
                    job["with"]["github_environment"], "nightly"
                )

    def test_the_promotion_runs_only_after_a_green_matrix(self):
        """A red matrix promotes nothing and leaves staging where it is."""
        job = self.jobs["step-4-create-staging-tag"]
        self.assertIn("step-3-run-e2e-matrix", job["needs"])
        self.assertIn("step-1-resolve-candidate", job["needs"])
        self.assertNotIn("always()", job.get("if", ""))

    def test_the_resolve_job_binds_the_nightly_environment(self):
        """It reads vars.REGISTRY_PREFIX; unbound, that resolves to empty in silence."""
        self.assertEqual(self.jobs["step-1-resolve-candidate"].get("environment"), "nightly")

    def test_the_promotion_job_runs_and_reports_rather_than_skipping(self):
        """An already-promoted night should show a job that decided, not a gap.

        Gating the job on skip_promotion would collapse the whole thing to
        "skipped" and lose the summary line saying why. The condition sits on the
        steps so the run records the decision it made.
        """
        job = self.jobs["step-4-create-staging-tag"]
        self.assertNotIn("skip_promotion", job.get("if", ""))
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
            teardown.get("if", ""),
            "always() removes the implicit success() and destroys the environments "
            "a failed run leaves standing for diagnosis",
        )
        self.assertIn("step-3-run-e2e-matrix", teardown["needs"])

    def test_the_promotion_tag_is_pushed_with_the_release_bot_token(self):
        """A tag pushed with GITHUB_TOKEN triggers no workflow, so staging never deploys."""
        checkout = next(
            step
            for step in self.jobs["step-4-create-staging-tag"]["steps"]
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

    def test_the_derived_tag_matches_staging_deploy_trigger(self):
        tag = self._derived_tag()
        patterns = _doc(_WORKFLOWS / _STAGING_DEPLOY)[True]["push"]["tags"]
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

    def test_staging_deploy_peels_tag_to_commit(self):
        """An annotated tag's ref resolves to the tag object, not to the commit."""
        doc = _doc(_WORKFLOWS / _STAGING_DEPLOY)
        jobs = doc["jobs"]
        resolve = jobs["resolve-commit"]
        self.assertTrue(
            any("resolve_deploy.sh" in step.get("run", "") and step.get("env", {}).get("TARGET_ENVIRONMENT") == "staging" for step in resolve["steps"]),
            "resolve-commit is supposed to call resolve_deploy.sh with TARGET_ENVIRONMENT: staging",
        )
        self.assertEqual(jobs["deploy"]["needs"], "resolve-commit")
        image_tag = jobs["deploy"]["with"].get("image_tag")
        self.assertNotIn("github.sha", str(image_tag))
        self.assertIn("resolve-commit", str(image_tag))

    def test_staging_deploy_calls_reconcile_environment_for_staging(self):
        doc = _doc(_WORKFLOWS / _STAGING_DEPLOY)
        deploy_job = doc["jobs"]["deploy"]
        self.assertIn("reconcile-environment.yml", deploy_job["uses"])
        self.assertEqual(deploy_job["with"]["github_environment"], "staging")
        self.assertEqual(deploy_job["with"]["mode"], "apply")

    def test_staging_deploy_verifies_candidate_images(self):
        doc = _doc(_WORKFLOWS / _STAGING_DEPLOY)
        steps = doc["jobs"]["resolve-commit"]["steps"]
        self.assertTrue(
            any("verify_candidate_images.sh" in step.get("run", "") for step in steps),
            "resolve-commit must verify candidate images in GHCR",
        )

    def test_staging_deploy_workflow_dispatch_defaults_lease_policy_to_fail(self):
        doc = _doc(_WORKFLOWS / _STAGING_DEPLOY)
        inputs = doc[True]["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["lease_policy"]["default"], "fail")

    def test_staging_deploy_has_verify_deploy_job_asserting_applied(self):
        doc = _doc(_WORKFLOWS / _STAGING_DEPLOY)
        self.assertIn("verify-deploy", doc["jobs"])
        verify = doc["jobs"]["verify-deploy"]
        self.assertEqual(set(verify["needs"]), {"resolve-commit", "deploy"})
        steps = verify["steps"]
        self.assertTrue(
            any("verify_deploy_result.sh" in s.get("run", "") for s in steps),
            "verify-deploy must invoke verify_deploy_result.sh",
        )

    def test_the_resolve_job_binds_the_staging_environment(self):
        """It reads vars.REGISTRY_PREFIX; unbound, that resolves to empty in silence."""
        doc = _doc(_WORKFLOWS / _STAGING_DEPLOY)
        self.assertEqual(doc["jobs"]["resolve-commit"].get("environment"), "staging")


_AUTOPUSH_DEPLOY = "autopush-deploy.yml"


class AutopushDeployWiringTest(unittest.TestCase):
    def setUp(self):
        self.doc = _doc(_WORKFLOWS / _AUTOPUSH_DEPLOY)
        self.jobs = self.doc["jobs"]

    def test_the_resolve_job_binds_the_autopush_environment(self):
        """It reads vars.REGISTRY_PREFIX; unbound, that resolves to empty in silence."""
        self.assertEqual(self.jobs["resolve-candidate"].get("environment"), "autopush")

    def test_triggers_include_workflow_run_and_dispatch(self):
        on = self.doc[True]
        self.assertNotIn("schedule", on, "autopush deploy must not run on cron")
        self.assertIn("workflow_run", on)
        self.assertEqual(on["workflow_run"]["workflows"], ["Publish Images to GHCR"])
        self.assertIn("workflow_dispatch", on)

    def test_concurrency_group_locks_autopush_deploy_without_cancelling(self):
        concurrency = self.doc.get("concurrency", {})
        self.assertEqual(concurrency.get("group"), "autopush-deploy")
        self.assertFalse(concurrency.get("cancel-in-progress"), "running deploys must not be cancelled mid-flight")

    def test_upstream_repository_guard_present(self):
        resolve = self.jobs["resolve-candidate"]
        self.assertIn("github.repository == 'gke-labs/kube-agents'", resolve.get("if", ""))
        self.assertIn("github.event.workflow_run.head_repository.full_name == github.repository", resolve.get("if", ""))
        self.assertIn("github.event.workflow_run.head_branch == 'main'", resolve.get("if", ""))

    def test_resolve_candidate_calls_script(self):
        resolve = self.jobs["resolve-candidate"]
        steps = resolve["steps"]
        self.assertTrue(
            any("resolve_deploy.sh" in step.get("run", "") and step.get("env", {}).get("TARGET_ENVIRONMENT") == "autopush" for step in steps),
            "resolve-candidate must use resolve_deploy.sh with TARGET_ENVIRONMENT: autopush",
        )

    def test_resolve_candidate_verifies_candidate_images(self):
        resolve = self.jobs["resolve-candidate"]
        steps = resolve["steps"]
        self.assertTrue(
            any("verify_candidate_images.sh" in step.get("run", "") for step in steps),
            "resolve-candidate must verify candidate images in GHCR",
        )

    def test_autopush_deploy_workflow_dispatch_defaults_lease_policy_to_fail(self):
        inputs = self.doc[True]["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["lease_policy"]["default"], "fail")

    def test_autopush_deploy_has_verify_deploy_job_asserting_applied(self):
        self.assertIn("verify-deploy", self.jobs)
        verify = self.jobs["verify-deploy"]
        self.assertEqual(set(verify["needs"]), {"resolve-candidate", "deploy"})
        steps = verify["steps"]
        self.assertTrue(
            any("verify_deploy_result.sh" in s.get("run", "") for s in steps),
            "verify-deploy must invoke verify_deploy_result.sh",
        )

    def test_deploy_job_calls_reconcile_environment_for_autopush(self):
        deploy = self.jobs["deploy"]
        self.assertEqual(deploy["needs"], "resolve-candidate")
        self.assertIn("reconcile-environment.yml", deploy["uses"])
        self.assertEqual(deploy["with"]["github_environment"], "autopush")
        self.assertEqual(deploy["with"]["mode"], "apply")
        self.assertIn("needs.resolve-candidate.outputs.commit_sha", deploy["with"]["image_tag"])
        self.assertEqual(deploy["if"], "github.repository == 'gke-labs/kube-agents'")


class DockerPublishGhcrWiringTest(unittest.TestCase):
    def setUp(self):
        self.doc = _doc(_WORKFLOWS / "docker-publish-ghcr.yml")
        self.jobs = self.doc["jobs"]

    def test_single_workflow_builds_all_required_images(self):
        self.assertIn("publish-operator", self.jobs)
        self.assertIn("publish-agents", self.jobs)
        self.assertFalse((_WORKFLOWS / "docker-publish-k8s-operator.yml").exists())


if __name__ == "__main__":
    unittest.main()
