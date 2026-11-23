"""The scheduled sweep of hold-unresolved-threads.yml sits in a concurrency group no label event enters.

The workflow starts on `schedule`, `workflow_dispatch` and
`pull_request_target: labeled`, and never cancels a running sweep. GitHub
keeps one pending run per concurrency group and cancels the run a newer
arrival replaces. With one group for the whole workflow, every `labeled`
event on any pull request entered it, so a burst of label events cancelled
the pending scheduled sweep again and again: the run the workflow calls its
guarantee, and the one a pull request with its last thread resolved waits on
to lose `do-not-merge` (gke-labs/kube-agents#4579).

Two contracts, read from the parsed YAML:

  - the group is keyed on the event name, at workflow level and nowhere
    else, so a scheduled run shares a group with scheduled runs only;
  - no run is cancelled in progress, so a sweep that started always finishes
    its writes.
"""

import pathlib
import unittest

from tests.test_workflow_pins_and_fork_guards import _jobs, _load, _triggers

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "hold-unresolved-threads.yml"

_CONCURRENCY = "concurrency"
_CONCURRENCY_GROUP = "${{ github.workflow }}-${{ github.event_name }}"
_SCHEDULE = "schedule"
_LABEL_EVENT = "pull_request_target"
_EVENT_NAME_EXPRESSION = "${{ github.event_name }}"


def _group_for(group: str, event: str) -> str:
    """The group name GitHub derives for a run of `event`, the workflow name left as written."""
    return group.replace(_EVENT_NAME_EXPRESSION, event)


class HoldUnresolvedThreadsConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        _text, self.workflow = _load(_WORKFLOW)
        self.assertIsNotNone(self.workflow, f"{_WORKFLOW.name} did not parse to a mapping")

    def test_the_workflow_still_has_both_triggers(self) -> None:
        self.assertLessEqual({_SCHEDULE, _LABEL_EVENT}, _triggers(self.workflow))

    def test_a_label_event_cannot_displace_the_pending_scheduled_sweep(self) -> None:
        group = self.workflow[_CONCURRENCY]["group"]
        self.assertEqual(
            _CONCURRENCY_GROUP,
            group,
            f"{_WORKFLOW.name}: the concurrency group is not keyed on the event "
            "name alone, so either label events share the scheduled sweep's "
            "group and cancel it while it is pending, or the key lets more "
            "runs overlap than the workflow's comment argues is safe",
        )
        self.assertNotEqual(_group_for(group, _SCHEDULE), _group_for(group, _LABEL_EVENT))

    def test_no_job_overrides_the_group(self) -> None:
        for name, job in _jobs(self.workflow).items():
            with self.subTest(job=name):
                self.assertNotIn(_CONCURRENCY, job)

    def test_a_running_sweep_is_never_cancelled(self) -> None:
        self.assertIs(self.workflow[_CONCURRENCY]["cancel-in-progress"], False)


if __name__ == "__main__":
    unittest.main()
