"""Tests for the wiring in .github/workflows/release-publish.yml.

The gate that decides whether an unattended run releases lives in shell scripts
with their own suites. What this file covers is the YAML connecting those
scripts' verdict to the publishing job, because that connection is where the
gate can be removed without any other test going red.

Three expressions carry it, and each fails quietly if it is dropped:

  - `needs.evaluate-schedule.outputs.should_release == 'true'` on the publish
    job. Lose it and every scheduled run publishes.
  - the `outputs:` mapping on the gate job. Lose it and `should_release` reads
    as empty, which is falsy, so nothing ever publishes — the harmless
    direction, but still silent.
  - the `TARGET_COMMIT` fallback to the gate's `release_commit`. Lose it and a
    scheduled run still publishes, just from whatever calculate_next_version.sh
    auto-resolves on its own. The two read the same tag family, so nothing would
    look wrong — until a candidate is promoted between the gate job and the
    publish job, and the release goes out at a commit nothing gated.

The cron trigger is decoupled: release-publish.yml is strictly dispatch-only so that quiet
ticks with nothing to release produce no workflow run at all. The weekly cron ("17 5 * * 4")
lives on .github/workflows/release-scheduler.yml, which evaluates candidate eligibility
and dispatches release-publish.yml only when work is required.
"""

import pathlib
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "release-publish.yml"

_GATE_JOB = "evaluate-schedule"
_PUBLISH_JOB = "publish-release"
_FORK_GUARD = "github.repository == 'gke-labs/kube-agents'"


def _load():
    # PyYAML resolves a bare `on:` key to the boolean True (YAML 1.1), which is
    # why this is not spelled workflow["on"].
    return yaml.safe_load(_WORKFLOW.read_text())


class ReleasePublishWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.workflow = _load()
        self.jobs = self.workflow["jobs"]
        self.triggers = self.workflow[True]

    def test_workflow_dispatch_is_the_way_in(self):
        self.assertIn("workflow_dispatch", self.triggers)

    def test_the_schedule_gate_input_offers_the_three_modes(self):
        """dry-run is how the cron's verdict is read without waiting for the cron."""
        gate_input = self.triggers["workflow_dispatch"]["inputs"]["schedule_gate"]
        self.assertEqual(gate_input["default"], "bypass")
        self.assertEqual(sorted(gate_input["options"]), ["bypass", "dry-run", "evaluate"])

    def test_release_scheduler_cron_is_weekly_rather_than_daily(self):
        """The cron is the cadence — there is no rate limiter inside the resolver.

        Nothing in resolve_scheduled_release.sh rations releases by elapsed time,
        which is deliberate: the design chose a weekly cron over a daily attempt
        capped by weekday arithmetic. The cron on release-scheduler.yml must fire
        weekly rather than daily, while release-publish.yml has no schedule trigger.
        """
        self.assertNotIn("schedule", self.triggers, "release-publish.yml must remain dispatch-only")
        scheduler_path = _REPO_ROOT / ".github" / "workflows" / "release-scheduler.yml"
        scheduler_doc = yaml.safe_load(scheduler_path.read_text())
        scheduler_triggers = scheduler_doc.get(True, scheduler_doc.get("on", {}))
        schedules = scheduler_triggers.get("schedule", [])
        self.assertTrue(schedules, "release-scheduler.yml must declare a schedule")
        for entry in schedules:
            minute, hour, dom, month, dow = entry["cron"].split()
            self.assertNotEqual(dow, "*", f"'{entry['cron']}' fires daily; the cadence must be weekly")
            self.assertEqual(dom, "*", f"'{entry['cron']}' pins a day of month rather than a weekday")
            del minute, hour, month

    def test_the_emergency_bypass_names_the_gate_it_bypasses(self):
        """`skip_rc_validation` names the RC suite, which is no longer the gate."""
        inputs = self.triggers["workflow_dispatch"]["inputs"]
        self.assertIn("skip_staging_validation", inputs)
        self.assertNotIn("skip_rc_validation", inputs)
        for step in self.jobs[_PUBLISH_JOB]["steps"]:
            self.assertNotIn(
                "SKIP_RC_VALIDATION",
                step.get("env", {}),
                f"{step.get('name')} still sets SKIP_RC_VALIDATION",
            )

    def test_both_jobs_carry_the_fork_guard(self):
        """AGENTS.md requires it on every job of a self-triggering credentialed workflow."""
        for name in (_GATE_JOB, _PUBLISH_JOB):
            self.assertIn(_FORK_GUARD, self.jobs[name]["if"], f"{name} is missing the fork guard")

    def test_the_gate_job_exposes_the_outputs_the_publish_job_reads(self):
        outputs = self.jobs[_GATE_JOB]["outputs"]
        self.assertIn("should_release", outputs)
        self.assertIn("release_commit", outputs)

    def test_publishing_is_gated_on_the_verdict(self):
        publish = self.jobs[_PUBLISH_JOB]
        needs = publish["needs"]
        self.assertIn(_GATE_JOB, [needs] if isinstance(needs, str) else needs)
        condition = " ".join(publish["if"].split())
        self.assertIn(f"needs.{_GATE_JOB}.outputs.should_release == 'true'", condition)

    def test_an_unattended_run_targets_the_gate_passing_commit(self):
        """Without the fallback the gate picks a commit the publish job ignores.

        Order is asserted, not just presence. Swapping the operands still
        satisfies "both are mentioned", and the swap is a real regression: on an
        emergency dispatch that names `target_commit` explicitly, the gate's
        commit would win and the wrong commit would be released.
        """
        step = self._step(_PUBLISH_JOB, "Calculate Next Release Version")
        target = " ".join(step["env"]["TARGET_COMMIT"].split())
        dispatch_at = target.find("inputs.target_commit")
        gate_at = target.find(f"needs.{_GATE_JOB}.outputs.release_commit")
        self.assertNotEqual(dispatch_at, -1, f"dispatch input missing from {target!r}")
        self.assertNotEqual(gate_at, -1, f"gate fallback missing from {target!r}")
        self.assertLess(
            dispatch_at,
            gate_at,
            f"an explicit target_commit must win over the gate's: {target!r}",
        )

    def test_the_gate_job_runs_the_mode_script(self):
        step = self._step(_GATE_JOB, "Decide")
        self.assertIn("decide_release_gate.sh", step["run"])
        self.assertEqual(step["env"]["EVENT_NAME"], "${{ github.event_name }}")
        self.assertEqual(step["env"]["SCHEDULE_GATE"], "${{ inputs.schedule_gate }}")

    def test_the_gate_job_sees_the_dispatched_target_commit(self):
        """The script refuses `evaluate`/`dry-run` alongside one; it needs to be told.

        Bare `inputs.target_commit`, never ORed with the gate's own answer: this
        is read to reject a combination, and a fallback here would make the
        check fire on the schedule path where no commit was named at all.
        """
        step = self._step(_GATE_JOB, "Decide")
        self.assertEqual(step["env"]["TARGET_COMMIT"], "${{ inputs.target_commit }}")

    def test_the_gate_job_checks_out_the_whole_tag_graph(self):
        """The verdict is made out of tags; a shallow clone answers 'nothing to release'."""
        step = self._step(_GATE_JOB, "Checkout repository")
        self.assertEqual(step["with"]["fetch-depth"], 0)
        self.assertIs(step["with"]["persist-credentials"], False)

    def test_the_gate_job_holds_no_write_permissions(self):
        """It only reads tags; the publish job is where write access belongs."""
        self.assertNotIn("permissions", self.jobs[_GATE_JOB])
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})

    def test_every_publishing_step_still_sits_behind_the_eligibility_skip(self):
        """The job-level verdict does not stand in for the per-step eligibility skip."""
        gated = [
            step
            for step in self.jobs[_PUBLISH_JOB]["steps"]
            if step.get("name") not in (
                "Generate Release Bot Token",
                "Checkout repository",
                "Calculate Next Release Version",
                "Verify Release Eligibility",
            )
        ]
        self.assertTrue(gated, "publish job has no steps after eligibility")
        for step in gated:
            self.assertIn(
                "steps.verify-eligibility.outputs.skip_release != 'true'",
                step.get("if", ""),
                f"step {step.get('name')!r} is not behind the eligibility skip",
            )

    def test_the_release_is_published_with_the_release_bot_token(self):
        """The release tag and assets must be pushed with the GitHub App release bot token."""
        steps = self.jobs[_PUBLISH_JOB]["steps"]
        token_step = next(
            step
            for step in steps
            if str(step.get("uses", "")).startswith("actions/create-github-app-token@")
        )
        self.assertIn("RELEASE_BOT_APP_ID", token_step["with"]["app-id"])
        self.assertIn("RELEASE_BOT_APP_PRIVATE_KEY", token_step["with"]["private-key"])
        self.assertEqual(token_step["with"].get("permission-contents"), "write")
        checkout = next(
            step
            for step in steps
            if str(step.get("uses", "")).startswith("actions/checkout@")
        )
        self.assertIn(token_step.get("id", "release-token"), checkout["with"]["token"])

    def test_the_helm_chart_is_published_with_github_token_packages_credential(self):
        """Helm chart push to GHCR requires package write permissions, held by GITHUB_TOKEN."""
        step = self._step(_PUBLISH_JOB, "Package, Publish and Sign Helm Chart")
        self.assertEqual(step["env"]["GH_TOKEN"], "${{ secrets.GITHUB_TOKEN }}")

    def test_the_release_tag_and_github_release_use_release_bot_token(self):
        """Git tag and GitHub release creation require the release bot token to bypass rulesets."""
        tag_step = self._step(_PUBLISH_JOB, "Create Git Tag")
        self.assertIn("steps.release-token.outputs.token", tag_step["env"]["GH_TOKEN"])
        release_step = self._step(_PUBLISH_JOB, "Publish GitHub Release")
        self.assertIn("steps.release-token.outputs.token", release_step["env"]["GH_TOKEN"])

    def _step(self, job, name):
        for step in self.jobs[job]["steps"]:
            if step.get("name") == name:
                return step
        self.fail(f"step {name!r} not found in job {job!r}")


if __name__ == "__main__":
    unittest.main()
