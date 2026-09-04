"""Tests for the wiring in .github/workflows/smoke-test-sticky.yml.

The workflow re-pins green `pull-kube-agents-smoke-test` statuses to the head
of `main` so Tide keeps crediting them after `main` moves. The pinning itself is
`scripts/pin_smoke_status.py`, tested beside it. What this file pins is the
YAML: which events reach a runner, the guards on the job-level `if:`, the
token's scope, and that each event runs the script in the right mode -- an
`if:` is where a guard can be dropped without any other test going red.
"""

import pathlib
import re
import shlex
import sys
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "smoke-test-sticky.yml"

_JOB = "pin"
_CONTEXT = "pull-kube-agents-smoke-test"
_FORK_GUARD = "github.repository == 'gke-labs/kube-agents'"
_SCRIPT = "scripts/pin_smoke_status.py"
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import pin_smoke_status  # noqa: E402

_PINNED_ACTION = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")  # the version comment is stripped by the YAML parser


def _load():
    # PyYAML resolves a bare `on:` key to the boolean True (YAML 1.1), which is
    # why this is not spelled workflow["on"].
    return yaml.safe_load(_WORKFLOW.read_text())


class SmokeTestStickyWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.workflow = _load()
        self.triggers = self.workflow[True]
        self.job = self.workflow["jobs"][_JOB]
        self.condition = self.job["if"]
        self.steps = {step["name"]: step for step in self.job["steps"]}

    def test_it_starts_on_status_events_and_pushes_to_main_only(self):
        self.assertEqual(sorted(self.triggers), ["push", "status"])
        self.assertEqual(self.triggers["push"], {"branches": ["main"]})

    def test_the_job_carries_the_fork_guard(self):
        self.assertIn(_FORK_GUARD, self.condition)

    def test_a_status_event_must_be_a_green_smoke_test_and_not_an_override(self):
        self.assertIn(f"github.event.context == '{_CONTEXT}'", self.condition)
        self.assertIn("github.event.state == 'success'", self.condition)
        self.assertIn("!startsWith(github.event.description, 'Overridden by')", self.condition)
        self.assertIn("github.event_name == 'push' ||", self.condition)

    def test_the_token_can_write_statuses_and_read_the_checkout_and_nothing_else(self):
        self.assertEqual(self.workflow["permissions"], {})
        self.assertEqual(self.job["permissions"], {"contents": "read", "statuses": "write"})

    def test_the_checkout_does_not_keep_credentials(self):
        self.assertEqual(self.steps["Checkout the default branch"]["with"], {"persist-credentials": False})

    def test_every_action_is_pinned_to_a_commit(self):
        for step in self.job["steps"]:
            if "uses" in step:
                self.assertRegex(step["uses"], _PINNED_ACTION)

    def test_a_push_sweeps_and_a_status_pins_one_commit(self):
        sweep = self.steps["Re-pin every open pull request after the merge"]
        self.assertEqual(sweep["if"], "github.event_name == 'push'")
        self.assertEqual(sweep["run"].strip(), f"python3 {_SCRIPT} sweep")
        one = self.steps["Re-pin this commit's green if main has already moved"]
        self.assertEqual(one["if"], "github.event_name == 'status'")
        self.assertIn(f"{_SCRIPT} status --sha \"$SHA\" --status-id \"$STATUS_ID\"", one["run"])
        self.assertEqual(one["env"]["STATUS_ID"], "${{ github.event.id }}")
        self.assertEqual(one["env"]["SHA"], "${{ github.event.sha }}")

    def test_each_step_s_command_line_parses_as_the_script_expects(self):
        """argparse rejects a parent option after the subcommand; the order in `run:` is the contract."""
        for step in self.steps.values():
            if "run" not in step:
                continue
            argv = shlex.split(step["run"])
            self.assertEqual(argv[:2], ["python3", _SCRIPT])
            args = pin_smoke_status._parse_args([re.sub(r"^\$\w+$", "0" * 40, token) for token in argv[2:]])
            self.assertIn(args.mode, ("sweep", "status"))

    def test_nothing_queues_behind_a_concurrency_group(self):
        """One pending run per group and the older is cancelled: a dropped pin."""
        self.assertNotIn("concurrency", self.workflow)
        self.assertNotIn("concurrency", self.job)
