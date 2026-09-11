"""The GA release cron lives on the scheduler, and the pipeline has no skip-green path.

Following the decoupled trigger pattern established by rc-scheduler.yml and
nightly-scheduler.yml, release-scheduler.yml holds the cron trigger
("17 5 * * 4") so that quiet ticks with nothing to release produce no
pipeline run at all.

These tests pin the structural invariants:
- The scheduler holds the cron ("17 5 * * 4"), and release-publish.yml does not.
- The scheduler evaluates candidates via resolve_scheduled_release.sh.
- Dispatch is strictly gated on steps.resolve.outputs.should_release == 'true'.
- Skips are recorded via record_release_scheduler_skip.sh on should_release != 'true'.
- The job has fork guards, concurrency controls, full git fetch depth, and actions: write.
"""

import pathlib
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_SCHEDULER = "release-scheduler.yml"
_PIPELINE = "release-publish.yml"
_RELEASE_CRON = "17 5 * * 4"

_DISPATCH_SCRIPT_NAME = "dispatch_release_pipeline.sh"
_DISPATCH_SCRIPT = (
    _REPO_ROOT / "scripts" / "release" / _DISPATCH_SCRIPT_NAME
)
_DISPATCH_SOURCE = _DISPATCH_SCRIPT.read_text()


def _dispatch_step(doc: dict) -> dict:
    """The step that runs the dispatch script, or fails the calling test."""
    for step in _steps(doc):
        if _DISPATCH_SCRIPT_NAME in (step.get("run") or ""):
            return step
    raise AssertionError(f"no step runs {_DISPATCH_SCRIPT_NAME}")


def _workflow(name: str) -> dict:
    doc = yaml.safe_load((_WORKFLOWS / name).read_text())
    if True in doc:
        doc["on"] = doc.pop(True)
    return doc


def _steps(doc: dict) -> list[dict]:
    steps: list[dict] = []
    for job in doc.get("jobs", {}).values():
        steps.extend(job.get("steps", []) or [])
    return steps


class SchedulerOwnsTheCron(unittest.TestCase):
    def test_the_scheduler_carries_the_release_cron(self) -> None:
        schedule = _workflow(_SCHEDULER)["on"]["schedule"]
        self.assertEqual([entry["cron"] for entry in schedule], [_RELEASE_CRON])

    def test_the_pipeline_has_no_schedule(self) -> None:
        """A cron on the pipeline restores the skip-paints-over-a-failure problem."""
        self.assertNotIn(
            "schedule",
            _workflow(_PIPELINE)["on"],
            "release-publish.yml must be dispatch-only; release-scheduler.yml owns "
            "the cron so that a tick with nothing to publish produces no run at all",
        )

    def test_exactly_one_workflow_holds_the_release_cron(self) -> None:
        """Two schedules would double-dispatch, and the second would be invisible."""
        holders = []
        for path in sorted(_WORKFLOWS.glob("*.yml")):
            doc = _workflow(path.name)
            for entry in (doc.get("on") or {}).get("schedule") or []:
                if entry.get("cron") == _RELEASE_CRON:
                    holders.append(path.name)
        self.assertEqual(holders, [_SCHEDULER])


class SchedulerDispatchWiring(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = _workflow(_SCHEDULER)
        self.job = next(iter(self.doc["jobs"].values()))

    def test_it_is_guarded_against_forks(self) -> None:
        """A fork inherits the cron and none of the release credentials."""
        self.assertIn("gke-labs/kube-agents", self.job["if"])

    def test_it_is_guarded_to_main_branch(self) -> None:
        """Scheduled and manual release evaluation must only execute on main."""
        self.assertIn("github.ref == 'refs/heads/main'", self.job["if"])

    def test_concurrency_group_locks_scheduler_without_cancelling(
        self,
    ) -> None:
        group = self.doc.get("concurrency", {}).get("group")
        self.assertEqual(group, "release-scheduler")
        self.assertFalse(self.doc["concurrency"].get("cancel-in-progress", True))

    def test_checkout_uses_full_depth(self) -> None:
        """Resolver reads tag graph and commit ranges; shallow checkouts answer falsely."""
        checkout = next(
            s
            for s in _steps(self.doc)
            if "actions/checkout" in s.get("uses", "")
        )
        self.assertEqual(checkout.get("with", {}).get("fetch-depth"), 0)

    def test_it_reuses_the_pipelines_own_resolver(self) -> None:
        """One implementation of 'is there an eligible candidate', not two."""
        step = next(
            s
            for s in _steps(self.doc)
            if "resolve_scheduled_release.sh" in (s.get("run") or "")
        )
        self.assertEqual(step.get("id"), "resolve")

    def test_the_dispatch_is_gated_on_there_being_work(self) -> None:
        step = _dispatch_step(self.doc)
        condition = step.get("if", "")
        self.assertIn("steps.resolve.outputs.should_release == 'true'", condition)

    def test_the_skip_step_is_wired_to_record_script(self) -> None:
        step = next(
            s
            for s in _steps(self.doc)
            if "record_release_scheduler_skip.sh" in (s.get("run") or "")
        )
        condition = step.get("if", "")
        self.assertIn("steps.resolve.outputs.should_release != 'true'", condition)
        self.assertIn(
            "${{ steps.resolve.outputs.skip_reason }}",
            step.get("env", {}).get("SKIP_REASON", ""),
        )
        exported = set(step.get("env", {}))
        self.assertLessEqual({"RELEASE_COMMIT", "GATE_TAG", "SKIP_REASON"}, exported)

    def test_the_skip_step_is_not_unconditionally_run(self) -> None:
        """The skip recording step must not run on error or always."""
        step = next(
            s
            for s in _steps(self.doc)
            if "record_release_scheduler_skip.sh" in (s.get("run") or "")
        )
        condition = step.get("if", "")
        self.assertNotIn("always()", condition)
        self.assertNotIn("failure()", condition)

    def test_the_dispatch_step_supplies_what_the_script_requires(self) -> None:
        exported = set(_dispatch_step(self.doc).get("env", {}))
        self.assertLessEqual({"GH_TOKEN", "RELEASE_COMMIT", "GATE_TAG"}, exported)

    def test_the_dispatch_uses_the_default_token(self) -> None:
        step = _dispatch_step(self.doc)
        self.assertEqual(
            step.get("env", {}).get("GH_TOKEN"), "${{ github.token }}"
        )

    def test_the_dispatching_job_can_write_actions(self) -> None:
        self.assertEqual(self.job["permissions"]["actions"], "write")

    def test_the_dispatch_names_the_pipeline_and_passes_schedule_gate(self) -> None:
        self.assertIn(_PIPELINE, _DISPATCH_SOURCE)
        self.assertIn("schedule_gate=evaluate", _DISPATCH_SOURCE)

    def test_the_pipeline_accepts_what_the_scheduler_sends(self) -> None:
        """dispatch_release_pipeline.sh passes schedule_gate=evaluate; verify pipeline accepts it."""
        sent = set()
        for token in _DISPATCH_SOURCE.split():
            if (token.startswith('"') or token.startswith("'")) and "=" in token:
                sent.add(token.strip('"\'\\;').split("=", 1)[0])
        accepted = set(_workflow(_PIPELINE)["on"]["workflow_dispatch"]["inputs"])
        self.assertTrue(sent, "the dispatch script passes no inputs")
        self.assertLessEqual(sent, accepted, f"{sent - accepted} not accepted")
        options = (
            _workflow(_PIPELINE)["on"]["workflow_dispatch"]["inputs"][
                "schedule_gate"
            ].get("options", [])
        )
        self.assertIn("evaluate", options)


if __name__ == "__main__":
    unittest.main()
