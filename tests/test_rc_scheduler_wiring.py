"""The RC cron lives on the scheduler, and the pipeline has no skip-green path.

A skipped run and a passing run used to be the same green. Step 1 set
`skip_rc=true`, every later job was skipped by its guard, skipped jobs do not
fail a run, and the run concluded `success` — so a quiet tick three hours after
a genuine failure reported the pipeline healthy. Skips were 23 of 68 candidates,
so this was the common case rather than an edge.

The fix is structural: the cron sits on `rc-scheduler.yml`, which resolves the
candidate and dispatches the pipeline only when there is one. Every property
that makes that work is easy to undo by accident and invisible when undone —
putting the cron back, dropping the `actions: write` the dispatch needs, or
reimplementing the skip decision instead of calling the shared script — so each
is pinned here.
"""

import pathlib
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_SCHEDULER = "rc-scheduler.yml"
_PIPELINE = "rc-release-pipeline.yml"
_RC_CRON = "17 */3 * * *"

# The dispatch itself lives in a script rather than inline in the workflow, so
# these read across the seam: the workflow decides when to run it and with which
# token, the script decides what it sends. Both halves still have to agree with
# the pipeline's declared inputs, which is what the last test here checks.
_DISPATCH_SCRIPT_NAME = "dispatch_rc_pipeline.sh"
_DISPATCH_SCRIPT = _REPO_ROOT / "scripts" / "release" / _DISPATCH_SCRIPT_NAME
_DISPATCH_SOURCE = _DISPATCH_SCRIPT.read_text()


def _dispatch_step(doc: dict) -> dict:
    """The step that runs the dispatch script, or fails the calling test."""
    for step in _steps(doc):
        if _DISPATCH_SCRIPT_NAME in (step.get("run") or ""):
            return step
    raise AssertionError(f"no step runs {_DISPATCH_SCRIPT_NAME}")


def _workflow(name: str) -> dict:
    doc = yaml.safe_load((_WORKFLOWS / name).read_text())
    # An unquoted `on:` parses as the boolean True under the YAML 1.1 rules
    # PyYAML implements.
    if True in doc:
        doc["on"] = doc.pop(True)
    return doc


def _steps(doc: dict) -> list[dict]:
    steps: list[dict] = []
    for job in doc.get("jobs", {}).values():
        steps.extend(job.get("steps", []) or [])
    return steps


class SchedulerOwnsTheCron(unittest.TestCase):
    def test_the_scheduler_carries_the_rc_cron(self) -> None:
        schedule = _workflow(_SCHEDULER)["on"]["schedule"]
        self.assertEqual([entry["cron"] for entry in schedule], [_RC_CRON])

    def test_the_pipeline_has_no_schedule(self) -> None:
        """A cron here restores the skip-paints-over-a-failure problem."""
        self.assertNotIn(
            "schedule",
            _workflow(_PIPELINE)["on"],
            "rc-release-pipeline.yml must be dispatch-only; rc-scheduler.yml owns "
            "the cron so that a tick with nothing to do produces no run at all",
        )

    def test_exactly_one_workflow_holds_the_rc_cron(self) -> None:
        """Two schedules would double-dispatch, and the second would be invisible."""
        holders = []
        for path in sorted(_WORKFLOWS.glob("*.yml")):
            doc = _workflow(path.name)
            for entry in (doc.get("on") or {}).get("schedule") or []:
                if entry.get("cron") == _RC_CRON:
                    holders.append(path.name)
        self.assertEqual(holders, [_SCHEDULER])


class SchedulerDispatchWiring(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = _workflow(_SCHEDULER)
        self.job = next(iter(self.doc["jobs"].values()))

    def test_it_is_guarded_against_forks(self) -> None:
        """A fork inherits the cron and none of the credentials."""
        self.assertIn("gke-labs/kube-agents", self.job["if"])

    def test_it_binds_the_rc_environment(self) -> None:
        """`vars.*` resolve to empty in an unbound job, and silently.

        REGISTRY_PREFIX decides where the resolver looks for prebuilt images, so
        an unbound job would find no candidate and dispatch nothing, every tick,
        with a green conclusion.
        """
        self.assertEqual(self.job["environment"], "rc")

    def test_it_reuses_the_pipelines_own_resolver(self) -> None:
        """One implementation of "is this a new candidate", not two.

        The gate that starts the pipeline and the gate inside it have to agree;
        a second copy here is how they drift.
        """
        runs = [step.get("run", "") for step in _steps(self.doc)]
        self.assertTrue(
            any("resolve_rc_tag.sh" in run for run in runs),
            "the scheduler must call resolve_rc_tag.sh rather than reimplement the "
            "skip decision",
        )

    def test_the_resolver_runs_in_scheduled_mode(self) -> None:
        """IS_SCHEDULED=true is the branch that self-resolves and can skip."""
        for step in _steps(self.doc):
            if "resolve_rc_tag.sh" in (step.get("run") or ""):
                self.assertEqual(str(step["env"]["IS_SCHEDULED"]).lower(), "true")
                return
        self.fail("no resolve_rc_tag.sh step found")

    def test_the_dispatch_uses_the_default_token(self) -> None:
        """`workflow_dispatch` is exempt from the GITHUB_TOKEN suppression rule.

        GitHub suppresses runs triggered by the default token to stop recursion
        and names `workflow_dispatch` and `repository_dispatch` as the two
        exceptions, so the default token starts the pipeline given the scope
        below. Reaching for a PAT here would put the only trigger of the RC
        pipeline behind a credential that can expire and whose scope cannot be
        read from this repository.
        """
        self.assertIn("github.token", _dispatch_step(self.doc)["env"]["GH_TOKEN"])

    def test_the_dispatching_job_can_write_actions(self) -> None:
        """The default token dispatches only with `actions: write`.

        Without it the call 403s, no pipeline runs, and the scheduler is the only
        thing that goes red — the invisible-skip failure in a new costume.
        """
        for job in self.doc["jobs"].values():
            steps = job.get("steps", []) or []
            if any(_DISPATCH_SCRIPT_NAME in (s.get("run") or "") for s in steps):
                self.assertEqual(job.get("permissions", {}).get("actions"), "write")
                return
        self.fail("no dispatch job found")

    def test_the_dispatch_is_gated_on_there_being_work(self) -> None:
        self.assertIn("skip_rc", _dispatch_step(self.doc)["if"])

    def test_the_dispatch_step_supplies_what_the_script_requires(self) -> None:
        """The script reads its inputs from the environment and aborts on any
        missing one, so a step that stops exporting a variable turns every
        three-hourly dispatch into a hard failure."""
        exported = set(_dispatch_step(self.doc).get("env", {}))
        self.assertLessEqual({"GH_TOKEN", "COMMIT_SHA", "RC_TAG"}, exported)

    def test_the_dispatch_names_the_pipeline_and_passes_the_commit(self) -> None:
        """Passing the resolved SHA is what stops `main` moving underneath the run."""
        self.assertIn(_PIPELINE, _DISPATCH_SOURCE)
        self.assertIn("commit_sha=", _DISPATCH_SOURCE)

    def test_the_pipeline_accepts_what_the_scheduler_sends(self) -> None:
        """A renamed input would fail every dispatch at the API, three-hourly."""
        sent = set()
        for token in _DISPATCH_SOURCE.split():
            if token.startswith('"') and "=" in token:
                sent.add(token.strip('"\\').split("=", 1)[0])
        accepted = set(_workflow(_PIPELINE)["on"]["workflow_dispatch"]["inputs"])
        self.assertTrue(sent, "the dispatch script passes no inputs")
        self.assertLessEqual(sent, accepted, f"{sent - accepted} not accepted")


if __name__ == "__main__":
    unittest.main()
