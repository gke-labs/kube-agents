"""Every pull-request workflow collapses superseded runs, and the broken-main notifier reacts to `main` only.

Pull-request checks on this repository queued for 20 to 40 minutes a day in
October 2026 behind run volume the repository created itself
(gke-labs/kube-agents#2943): about 2700 runs a day, half of them event-reaction
runs that skipped at their job gate, plus two pull-request workflows with no
concurrency group, one of them holding two queued runs per pull request.
Nothing asserted either shape, so a workflow added without the group, or a
trigger widened in an edit, would bring the queue back without failing a check.

Three contracts, all read from the parsed YAML:

  - a workflow that starts on `pull_request` carries a top-level `concurrency`
    group keyed on the pull request number, falling back to the run id, with
    `cancel-in-progress` on for pull-request runs, so a second push cancels
    the first push's queued and running runs rather than queueing behind them.
    The standard block is the one `a2a-test.yml` carries. The fallback is
    what keeps `push`, `merge_group` and `workflow_dispatch` runs safe: each
    sits alone in a group named by its run id, so none is ever cancelled or
    displaced, whichever form the cancel flag takes. `cancel-in-progress` may
    be the bare `true` or the expression that turns it on for `pull_request`
    events only.
  - a `pull_request` workflow that also starts on `edited` keys its group on
    the head SHA as well. `edited` does not advance the head and nothing
    orders run creation by event, so an `edited` run still carrying the old
    head can arrive after the `synchronize` run for the new one; in a group
    keyed on the number alone it cancels or displaces that run, and the new
    head is left with no check run under the workflow's name: the 2026-09-09
    incident on gke-labs/kube-agents#1364, which gke-labs/kube-agents#1451
    answered by moving `risk_classify.yml` to a head key. Keyed by head, a run
    can only supersede runs for its own head.
  - `main-broken-notify.yml` filters its `workflow_run` trigger to runs on
    `main`. Its job acts only on push-to-main runs, but `workflow_run` fires on
    every completion of a watched workflow, and without the filter every
    pull-request run of the seven it watches started a notify run that skipped.
"""

import pathlib
import unittest

# The workflow-directory globs, the loader and the trigger-name reader are the
# sibling sweep's. `_trigger` below reads one event's mapping, which the
# sibling has no helper for.
from tests.test_workflow_pins_and_fork_guards import WORKFLOW_GLOBS, _load, _triggers

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"

_PULL_REQUEST = "pull_request"
_EDITED = "edited"
_PULL_REQUEST_NUMBER = "github.event.pull_request.number"
_RUN_ID_FALLBACK = "|| github.run_id"
_PULL_REQUEST_HEAD = "github.event.pull_request.head.sha"
_CANCEL_ON_PULL_REQUEST = "github.event_name == 'pull_request'"

_BROKEN_MAIN_NOTIFIER = "main-broken-notify.yml"
_WORKFLOW_RUN = "workflow_run"
_MAIN = "main"

# A floor on how many workflows the sweep examined, so an empty or partial walk
# cannot pass. Fifteen workflows ran on `pull_request` when this was written.
_PULL_REQUEST_WORKFLOWS_FLOOR = 12


def _workflow(path: pathlib.Path) -> dict | None:
    _text, doc = _load(path)
    return doc


def _pull_request_workflows() -> dict[str, dict]:
    found = {}
    for pattern in WORKFLOW_GLOBS:
        for path in sorted(_WORKFLOWS.glob(pattern)):
            doc = _workflow(path)
            if doc is not None and _PULL_REQUEST in _triggers(doc):
                found[path.name] = doc
    return found


def _trigger(doc: dict, event: str) -> dict:
    """The mapping under one event of `on:`, empty when the event has none."""
    # PyYAML reads an unquoted `on:` key as the boolean True (YAML 1.1).
    on = doc.get("on", doc.get(True))
    trigger = on.get(event) if isinstance(on, dict) else None
    return trigger if isinstance(trigger, dict) else {}


def _pull_request_types(doc: dict) -> list[str]:
    return list(_trigger(doc, _PULL_REQUEST).get("types") or [])


class PullRequestConcurrencyTest(unittest.TestCase):
    def test_the_sweep_saw_the_pull_request_workflows(self) -> None:
        self.assertGreaterEqual(len(_pull_request_workflows()), _PULL_REQUEST_WORKFLOWS_FLOOR)

    def test_every_pull_request_workflow_cancels_the_superseded_run(self) -> None:
        for name, doc in _pull_request_workflows().items():
            with self.subTest(workflow=name):
                concurrency = doc.get("concurrency")
                self.assertIsInstance(
                    concurrency,
                    dict,
                    f"{name} runs on pull_request with no top-level concurrency "
                    "group, so a second push queues behind the first push's runs",
                )
                group = str(concurrency.get("group", ""))
                self.assertIn(
                    _PULL_REQUEST_NUMBER,
                    group,
                    f"{name}: the concurrency group is not keyed on the pull "
                    "request number, so two pushes to one pull request never share a group",
                )
                self.assertIn(
                    _RUN_ID_FALLBACK,
                    group,
                    f"{name}: the concurrency group does not fall back to the run "
                    "id, so every push, merge-queue or dispatch run shares one group "
                    "and a burst of merges to main cancels or displaces the runs in it",
                )
                cancel = concurrency.get("cancel-in-progress")
                cancels_pull_requests = cancel is True or (
                    isinstance(cancel, str) and _CANCEL_ON_PULL_REQUEST in cancel
                )
                self.assertTrue(
                    cancels_pull_requests,
                    f"{name}: cancel-in-progress is {cancel!r}; a pull-request "
                    "run must cancel the one it supersedes",
                )

    def test_a_workflow_that_runs_on_edited_keys_its_group_on_the_head(self) -> None:
        for name, doc in _pull_request_workflows().items():
            if _EDITED not in _pull_request_types(doc):
                continue
            with self.subTest(workflow=name):
                group = str((doc.get("concurrency") or {}).get("group", ""))
                self.assertIn(
                    _PULL_REQUEST_HEAD,
                    group,
                    f"{name} runs on `edited`, which does not advance the head, "
                    "but its concurrency group is not keyed on the head SHA: an "
                    "`edited` run created after the `synchronize` run for a newer "
                    "head would cancel it and leave that head with no check run",
                )


class BrokenMainNotifierTriggerTest(unittest.TestCase):
    def test_the_notifier_reacts_to_runs_on_main_only(self) -> None:
        doc = _workflow(_WORKFLOWS / _BROKEN_MAIN_NOTIFIER)
        workflow_run = _trigger(doc, _WORKFLOW_RUN)
        self.assertEqual(
            [_MAIN],
            workflow_run.get("branches"),
            f"{_BROKEN_MAIN_NOTIFIER}: workflow_run has no `branches: [main]` "
            "filter, so every pull-request run of a watched workflow starts a "
            "notify run that its job gate then skips",
        )


if __name__ == "__main__":
    unittest.main()
