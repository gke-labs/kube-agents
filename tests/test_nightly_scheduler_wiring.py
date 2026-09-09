"""The nightly cron lives on the scheduler, and the pipeline has no skip-green path.

A skipped run and a passing run should not be the same green. Step 1 resolving
a candidate that has already been promoted or that requires no staging promotion
should produce no pipeline run at all.

The cron sits on `nightly-scheduler.yml`, which resolves the candidate and
dispatches the pipeline only when work is needed. Every property that makes that
work is easy to undo by accident and invisible when undone — putting the cron
back on the pipeline, dropping the `actions: write` the dispatch needs, or
reimplementing the skip decision instead of calling the shared script — so each
is pinned here.
"""

import pathlib
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_SCHEDULER = "nightly-scheduler.yml"
_PIPELINE = "nightly-pipeline.yml"
_NIGHTLY_CRON = "17 2 * * *"

_DISPATCH_SCRIPT_NAME = "dispatch_nightly_pipeline.sh"
_DISPATCH_SCRIPT = _REPO_ROOT / "scripts" / "release" / _DISPATCH_SCRIPT_NAME
_DISPATCH_SOURCE = _DISPATCH_SCRIPT.read_text()
_SKIP_SCRIPT_NAME = "record_nightly_scheduler_skip.sh"


def _dispatch_step(doc: dict) -> dict:
    """The step that runs the dispatch script, or fails the calling test."""
    for step in _steps(doc):
        if _DISPATCH_SCRIPT_NAME in (step.get("run") or ""):
            return step
    raise AssertionError(f"no step runs {_DISPATCH_SCRIPT_NAME}")


def _skip_step(doc: dict) -> dict:
    """The step that runs the skip recording script, or fails the calling test."""
    for step in _steps(doc):
        if _SKIP_SCRIPT_NAME in (step.get("run") or ""):
            return step
    raise AssertionError(f"no step runs {_SKIP_SCRIPT_NAME}")


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
    def test_the_scheduler_carries_the_nightly_cron(self) -> None:
        schedule = _workflow(_SCHEDULER)["on"]["schedule"]
        self.assertEqual([entry["cron"] for entry in schedule], [_NIGHTLY_CRON])

    def test_the_pipeline_has_no_schedule(self) -> None:
        """A cron here restores the skip-paints-over-a-failure problem."""
        self.assertNotIn(
            "schedule",
            _workflow(_PIPELINE)["on"],
            "nightly-pipeline.yml must be dispatch-only; nightly-scheduler.yml owns "
            "the cron so that a tick with nothing to do produces no run at all",
        )

    def test_exactly_one_workflow_holds_the_nightly_cron(self) -> None:
        """Two schedules would double-dispatch, and the second would be invisible."""
        holders = []
        for path in sorted(_WORKFLOWS.glob("*.yml")):
            doc = _workflow(path.name)
            for entry in (doc.get("on") or {}).get("schedule") or []:
                if entry.get("cron") == _NIGHTLY_CRON:
                    holders.append(path.name)
        self.assertEqual(holders, [_SCHEDULER])


class SchedulerDispatchWiring(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = _workflow(_SCHEDULER)
        self.job = next(iter(self.doc["jobs"].values()))

    def test_it_is_guarded_against_forks(self) -> None:
        """A fork inherits the cron and none of the credentials."""
        self.assertIn("gke-labs/kube-agents", self.job["if"])

    def test_it_binds_the_nightly_environment(self) -> None:
        """`vars.*` resolve to empty in an unbound job, and silently.

        GH_ORG and GH_REPO are required for release_fetch_tags to query the
        target repository's tag graph, so an unbound job would resolve no candidate
        and dispatch nothing.
        """
        self.assertEqual(self.job["environment"], "nightly")

    def test_it_reuses_the_pipelines_own_resolver(self) -> None:
        """One implementation of 'is this a new candidate', not two.

        The gate that starts the pipeline and the gate inside it have to agree;
        a second copy here is how they drift.
        """
        runs = [step.get("run", "") for step in _steps(self.doc)]
        self.assertTrue(
            any("resolve_promotion_candidate.sh" in run for run in runs),
            "the scheduler must call resolve_promotion_candidate.sh rather than reimplement the "
            "skip decision",
        )

    def test_the_dispatch_uses_the_default_token(self) -> None:
        """`workflow_dispatch` is exempt from the GITHUB_TOKEN suppression rule."""
        self.assertIn("github.token", _dispatch_step(self.doc)["env"]["GH_TOKEN"])

    def test_the_dispatching_job_can_write_actions(self) -> None:
        """The default token dispatches only with `actions: write`."""
        for job in self.doc["jobs"].values():
            steps = job.get("steps", []) or []
            if any(_DISPATCH_SCRIPT_NAME in (s.get("run") or "") for s in steps):
                self.assertEqual(job.get("permissions", {}).get("actions"), "write")
                return
        self.fail("no dispatch job found")

    def test_the_dispatch_is_gated_on_there_being_work(self) -> None:
        dispatch_cond = _dispatch_step(self.doc)["if"]
        self.assertEqual(
            dispatch_cond.strip(),
            "steps.resolve.outputs.skip_pipeline != 'true'",
        )

    def test_the_skip_step_is_wired_to_record_script(self) -> None:
        skip_step = _skip_step(self.doc)
        self.assertEqual(
            skip_step.get("if", "").strip(),
            "steps.resolve.outputs.skip_pipeline == 'true'",
        )
        exported = set(skip_step.get("env", {}))
        self.assertLessEqual({"COMMIT_SHA", "RC_TAG", "SKIP_REASON"}, exported)

    def test_the_dispatch_step_supplies_what_the_script_requires(self) -> None:
        exported = set(_dispatch_step(self.doc).get("env", {}))
        self.assertLessEqual({"GH_TOKEN", "COMMIT_SHA", "RC_TAG"}, exported)

    def test_the_dispatch_names_the_pipeline_and_passes_the_tag(self) -> None:
        self.assertIn(_PIPELINE, _DISPATCH_SOURCE)
        self.assertIn("rc_tag=", _DISPATCH_SOURCE)

    def test_the_pipeline_accepts_what_the_scheduler_sends(self) -> None:
        sent = set()
        for token in _DISPATCH_SOURCE.split():
            if token.startswith('"') and "=" in token:
                sent.add(token.strip('"\\').split("=", 1)[0])
        accepted = set(_workflow(_PIPELINE)["on"]["workflow_dispatch"]["inputs"])
        self.assertTrue(sent, "the dispatch script passes no inputs")
        self.assertLessEqual(sent, accepted, f"{sent - accepted} not accepted")


if __name__ == "__main__":
    unittest.main()
