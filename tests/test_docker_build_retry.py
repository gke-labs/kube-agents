"""Every network-bound step in docker-build.yml runs once more after a failure.

    python3 -m unittest tests.test_docker_build_retry

Five failed attempts of the Docker Build job (#3825, #4404, #4653) each died on
a different service it does not control: mirror.gcr.io pulling the builder
image, packages.cloud.google.com mid-publish, proxy.golang.org, and the npm
registry. Each passed on a manual re-run. The workflow's job comment holds the
reasons; this file holds the shape, because deleting the retry fails no build
and reports green until the next bad minute.

The contract, read from the parsed YAML: each `docker/setup-buildx-action` and
`docker/build-push-action` step, bar the named exemptions (which must run
once), has an `id` and `continue-on-error: true`; the next step is a pause
gated on that id's failure that runs the shared pause script; the step after is
the same action, with the same inputs, on the same gate, and fails the job if
it fails too. The pause script is run against a stub `sleep`, not read, so a
script that drops the sleep, the warning or the job-summary line fails here.
The synthetic cases hold each way the shape can break, so a walk that matches
nothing cannot pass.
"""

import copy
import os
import pathlib
import stat
import subprocess
import tempfile
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "docker-build.yml"
_JOB = "build"
_PAUSE_VARIABLE = "RETRY_PAUSE_SECONDS"
_FAILED_STEP_VARIABLE = "FAILED_STEP"
_RETRIED_ACTIONS = ("docker/setup-buildx-action@", "docker/build-push-action@")
# Steps that must run once, each with its reason. The checker refuses a retry on
# them as firmly as it demands one everywhere else.
_EXEMPT_STEPS = frozenset(
    {
        # Its RUN is the entrypoint gate check, a behavioural test: a retry would
        # hide an intermittent failure from flaky-check-notify.yml. A mirror error
        # can still red it, visibly; that is the accepted cost.
        "Verify entrypoint shared-state gate (in-image)",
    }
)
# The buildx setup and the five image builds that fetch from the network today.
# A floor, so a rename that drops steps out of the walk fails here.
_MIN_RETRIED_STEPS = 6
_WARNING_PREFIX = "::warning::"
_SLEEP_LOG_NAME = "sleep.log"
_SUMMARY_NAME = "summary.md"
_STUB_PAUSE_SECONDS = "7"


def _failure_gate(step_id):
    return f"steps.{step_id}.outcome == 'failure'"


def _uses_retried_action(step):
    # GitHub resolves action owners and names case-insensitively.
    return str(step.get("uses", "")).lower().startswith(_RETRIED_ACTIONS)


def check_retries(steps, exempt=_EXEMPT_STEPS):
    """Return (problems, retried step names) for a job's step list."""
    problems = []
    retried = []
    index = 0
    while index < len(steps):
        step = steps[index]
        name = step.get("name", f"step {index}")
        is_retry = str(step.get("if", "")).endswith(".outcome == 'failure'")
        if _uses_retried_action(step) and name in exempt:
            if step.get("continue-on-error") or step.get("id"):
                problems.append(f"{name}: exempt, so it runs once with no id or continue-on-error")
            index += 1
            continue
        if not _uses_retried_action(step) or is_retry:
            index += 1
            continue
        step_id = step.get("id")
        if not step_id:
            problems.append(f"{name}: no id for the retry to gate on")
            index += 1
            continue
        if step.get("continue-on-error") is not True:
            problems.append(f"{name}: first attempt lacks continue-on-error: true")
        gate = _failure_gate(step_id)
        pause = steps[index + 1] if index + 1 < len(steps) else {}
        retry = steps[index + 2] if index + 2 < len(steps) else {}
        if pause.get("if") != gate or "run" not in pause:
            problems.append(f"{name}: next step is not a pause gated on {gate!r}")
        elif pause.get("env", {}).get(_FAILED_STEP_VARIABLE) != name:
            problems.append(f"{name}: pause does not name the step in {_FAILED_STEP_VARIABLE}")
        if retry.get("if") != gate:
            problems.append(f"{name}: no retry gated on {gate!r} after the pause")
        elif retry.get("uses") != step.get("uses"):
            problems.append(f"{name}: retry uses {retry.get('uses')!r}, not the same action")
        elif retry.get("with") != step.get("with"):
            problems.append(f"{name}: retry inputs differ from the first attempt")
        elif retry.get("continue-on-error"):
            problems.append(f"{name}: retry sets continue-on-error, so a second failure passes")
        retried.append(name)
        index += 3
    return problems, retried


def _job():
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    return workflow["jobs"][_JOB]


class WorkflowRetryTest(unittest.TestCase):
    def test_every_network_step_retries(self):
        problems, retried = check_retries(_job()["steps"])
        self.assertEqual(problems, [])
        self.assertGreaterEqual(len(retried), _MIN_RETRIED_STEPS, retried)

    def test_exemption_names_a_real_step(self):
        names = {step.get("name") for step in _job()["steps"]}
        self.assertLessEqual(_EXEMPT_STEPS, names)

    def test_pause_is_a_positive_integer(self):
        value = str(_job().get("env", {}).get(_PAUSE_VARIABLE, ""))
        self.assertTrue(value.isdigit() and int(value) > 0, value)

    def test_every_pause_runs_one_script(self):
        steps = _job()["steps"]
        scripts = {s["run"] for s in steps if _FAILED_STEP_VARIABLE in s.get("env", {})}
        self.assertEqual(len(scripts), 1, scripts)


class PauseScriptTest(unittest.TestCase):
    """Run the pause script with a stub `sleep` that records its argument."""

    def test_pause_sleeps_and_warns(self):
        steps = _job()["steps"]
        script = next(s["run"] for s in steps if _FAILED_STEP_VARIABLE in s.get("env", {}))
        with tempfile.TemporaryDirectory() as tmp:
            stub = pathlib.Path(tmp) / "sleep"
            log = pathlib.Path(tmp) / _SLEEP_LOG_NAME
            summary = pathlib.Path(tmp) / _SUMMARY_NAME
            stub.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n')
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
            env = {
                "PATH": f"{tmp}{os.pathsep}{os.environ['PATH']}",
                _PAUSE_VARIABLE: _STUB_PAUSE_SECONDS,
                _FAILED_STEP_VARIABLE: "Build Example",
                "GITHUB_STEP_SUMMARY": str(summary),
            }
            result = subprocess.run(
                ["bash", "-e", "-c", script], env=env, capture_output=True, text=True, check=True
            )
            self.assertEqual(log.read_text().split(), [_STUB_PAUSE_SECONDS])
            self.assertIn("Build Example", summary.read_text())
        self.assertTrue(result.stdout.startswith(_WARNING_PREFIX), result.stdout)
        self.assertIn("Build Example", result.stdout)


class CheckerTest(unittest.TestCase):
    """Synthetic jobs: each way the shape can break is reported."""

    def _good(self):
        gate = _failure_gate("b")
        return [
            {"name": "Checkout", "uses": "actions/checkout@abc"},
            {
                "name": "Build",
                "id": "b",
                "continue-on-error": True,
                "uses": "docker/build-push-action@abc",
                "with": {"context": "."},
            },
            {"name": "Wait", "if": gate, "env": {_FAILED_STEP_VARIABLE: "Build"}, "run": "x"},
            {
                "name": "Build (retry)",
                "if": gate,
                "uses": "docker/build-push-action@abc",
                "with": {"context": "."},
            },
        ]

    def test_good_shape_passes(self):
        self.assertEqual(check_retries(self._good(), exempt=frozenset()), ([], ["Build"]))

    def _problems_after(self, mutate):
        steps = copy.deepcopy(self._good())
        mutate(steps)
        return check_retries(steps, exempt=frozenset())[0]

    def test_missing_continue_on_error(self):
        self.assertTrue(self._problems_after(lambda s: s[1].pop("continue-on-error")))

    def test_missing_id(self):
        self.assertTrue(self._problems_after(lambda s: s[1].pop("id")))

    def test_missing_pause(self):
        self.assertTrue(self._problems_after(lambda s: s.pop(2)))

    def test_missing_retry(self):
        self.assertTrue(self._problems_after(lambda s: s.pop(3)))

    def test_retry_inputs_differ(self):
        self.assertTrue(self._problems_after(lambda s: s[3]["with"].update(context="x")))

    def test_retry_swallows_failure(self):
        self.assertTrue(self._problems_after(lambda s: s[3].update({"continue-on-error": True})))

    def test_pause_names_wrong_step(self):
        self.assertTrue(self._problems_after(lambda s: s[2]["env"].update({_FAILED_STEP_VARIABLE: "x"})))

    def test_exempt_step_is_skipped(self):
        steps = [{"name": "Gate", "uses": "docker/build-push-action@abc"}]
        self.assertEqual(check_retries(steps, exempt=frozenset({"Gate"})), ([], []))

    def test_exempt_step_with_retry_is_refused(self):
        steps = copy.deepcopy(self._good())
        problems, _ = check_retries(steps, exempt=frozenset({"Build"}))
        self.assertTrue(problems)

    def test_capitalised_uses_is_still_a_network_step(self):
        steps = [{"name": "B", "uses": "Docker/build-push-action@abc"}]
        self.assertTrue(check_retries(steps, exempt=frozenset())[0])

    def test_retry_uses_different_action(self):
        self.assertTrue(
            self._problems_after(lambda s: s[3].update(uses="docker/setup-buildx-action@abc"))
        )


if __name__ == "__main__":
    unittest.main()
