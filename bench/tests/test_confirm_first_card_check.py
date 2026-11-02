# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""chat-real-report-fix-files-confirm-first's filing entries, on both transports.

The inject door sends the Planning Agent's arguments as shapes
(``a2a/hermes-bridge/activity.go``, ``shapeOf``): its ``kanban_create`` arrives
as ``{"assignee": "<string, 8 chars>", "body": "<string, 803 chars>", ...}``, so
no pattern over the router's call can read the confirm-first criterion. The
case reads the filing in two entries. ``the-fix-is-filed`` reads the router's
``kanban_create`` by tool name alone, which both transports keep; it is
trace-blind, so the scorer grades it on the inject lane too.
``the-fix-is-filed-with-confirm-first`` reads the router's call with its
readable ``body`` and then a ``kanban_show`` by the platform worker whose
result carries the criterion (any platform show in the run: ``tool_called``
has no card selector, #4572); it is worker-blind, graded on the api lane and
set aside on inject.

Every fixture runs through the whole entry as the runner grades it, built
from the shapes the nightly recorded: the api lane's readable call
(ci-kube-agents-eval-nightly 2113420200605913088 rep 1, 2026-10-23) and the
inject lane's shaped call (2117044258501300224 rep 1, 2026-11-02).

Run:  cd bench && python -m pytest -q tests/test_confirm_first_card_check.py
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from devops_bench.verification.runner import VerifierAgent
from devops_bench.verification.spec import VerificationEntry

from kube_agents_bench import cases, transcript

TASK = Path(__file__).resolve().parent.parent / "tasks" / "chat-real-report-fix-files-confirm-first" / "task.yaml"
FILED = "the-fix-is-filed"
CONFIRM_FIRST = "the-fix-is-filed-with-confirm-first"
SAFEGUARD = "front-door-stays-out-of-the-worker-verbs"
TIMEOUT_SEC = 5.0
CARD = "t_72c5a3e7"
REPLY = "checking checkout-gateway."

# The api lane's card body, verbatim (2113420200605913088 rep 1).
CONFIRM_FIRST_BODY = (
    "User asked to fix checkout-gateway.\n\n"
    "Claim (from an earlier fleet health report in this thread, dated 2026-10-14 18:40 UTC, not observed "
    "state): on cluster seeded-a, deployment checkout-gateway in namespace seeded-reliability had 1/3 replicas "
    "ready; readiness probe returning HTTP 503 since 18:12.\n\n"
    "Acceptance criteria:\n"
    "- First confirm checkout-gateway exists in seeded-reliability on seeded-a and whether it is still unhealthy. "
    "If it doesn't exist, say \"I can't find checkout-gateway on seeded-a\" and stop. If it's now healthy, report "
    "that and stop.\n"
    "- Find the root cause of the 503 readiness failures.\n"
    "- Deliver the fix as a pull request in the target repository's own format, or a stated stop naming what is "
    "missing (e.g. repo not registered, access lacking). No direct cluster mutation.\n"
    "- Report root cause, the fix, and the PR link."
)
# The inject lane's card body as the worker's kanban_show returned it
# (2117044258501300224 rep 1), up to the report-format boilerplate.
INJECT_LANE_BODY = (
    "Goal: Remediate the checkout-gateway issue on cluster seeded-a (project kube-agents-evals-17, location "
    "us-central1-a) in namespace seeded-reliability.\n\nContext:\n- An earlier message in this thread reported "
    "checkout-gateway in namespace seeded-reliability on cluster seeded-a (2026-10-14 18:40 UTC) having 1/3 "
    "replicas ready, with its readiness probe returning HTTP 503 since 18:12.\n- The user requested: \"Fix the "
    "first one\".\n\nAcceptance criteria:\n- First confirm that checkout-gateway in namespace seeded-reliability "
    "exists on cluster seeded-a before changing anything, and answer \"I can't find checkout-gateway on seeded-a\" "
    "when it doesn't.\n- If it exists, diagnose the readiness probe HTTP 503 issue and propose the fix as a pull "
    "request in the target repository's own format (or state what stops it)."
)
# A card that goes straight to the fix: the workload named, no confirm step.
NO_CONFIRM_BODY = (
    "User asked to fix checkout-gateway in seeded-reliability on seeded-a: 1/3 replicas ready, readiness probe "
    "HTTP 503 since 18:12.\n\nAcceptance criteria:\n- Find the root cause of the 503 readiness failures and check "
    "logs and events.\n- Deliver the fix as a pull request.\n- Report root cause, the fix, and the PR link."
)
TITLE = "Fix checkout-gateway readiness failures in seeded-reliability on seeded-a"
CREATE_RESULT = '{"ok": true, "task_id": "t_72c5a3e7", "status": "ready", "workspace_kind": "scratch", "subscribed": true}'


def _readable_create(body: str = CONFIRM_FIRST_BODY) -> dict:
    """The router's call as the api transport records it, arguments readable."""
    return {
        "name": "kanban_create",
        "args": {"assignee": "platform", "title": TITLE, "body": body},
        "result": CREATE_RESULT,
        "status": "completed",
    }


# The router's call as the inject door records it.
SHAPED_CREATE = {
    "name": "kanban_create",
    "args": {"assignee": "<string, 8 chars>", "body": "<string, 803 chars>", "title": "<string, 54 chars>"},
    "result": None,
    "status": "completed",
}
ACTIVITY_MARKER = {"name": "a2a.activity", "args": {"calls": 1, "dropped": 0}, "result": None, "status": "completed"}


def _worker(tool: str, result: str, agent: str = "platform", **args) -> dict:
    return {
        "name": tool,
        "args": args,
        "result": result,
        "status": "completed",
        "agent": agent,
        "task": CARD,
        "session": "20261023_031518_7390f3",
        "at": 1792725328.0,
    }


def _show(body: str, agent: str = "platform") -> dict:
    """The specialist's kanban_show of the card, its body in the result."""
    task = {"id": CARD, "title": TITLE, "body": body, "status": "in_progress", "assignee": agent}
    return _worker("kanban_show", result=yaml.safe_dump({"task": task}, sort_keys=False), agent=agent)


def _worked(body: str, agent: str = "platform") -> list[dict]:
    return [
        _show(body, agent=agent),
        _worker("kanban_heartbeat", result='{"ok": true, "task_id": "t_72c5a3e7"}', agent=agent, note="checking"),
        _worker("kanban_complete", result='{"ok": true, "task_id": "t_72c5a3e7"}', agent=agent, status="completed", raw="{}"),
    ]


API_LANE = [_readable_create()] + _worked(CONFIRM_FIRST_BODY)
INJECT_LANE = [ACTIVITY_MARKER, SHAPED_CREATE] + _worked(INJECT_LANE_BODY)
NO_CONFIRM = [_readable_create(NO_CONFIRM_BODY)] + _worked(NO_CONFIRM_BODY)
NO_CARD = [{"name": "terminal", "args": {"command": "kubectl get deploy -n seeded-reliability"}, "result": "", "status": "completed"}]


@pytest.fixture(autouse=True)
def _clean_transcript():
    transcript.clear()
    yield
    transcript.clear()


def _entry(name: str) -> VerificationEntry:
    doc = yaml.safe_load(TASK.read_text(encoding="utf-8"))
    (entry,) = [e for e in doc["verification_spec"] if e.get("name") == name]
    return VerificationEntry.model_validate(entry)


def _status(name: str, trajectory: list[dict]) -> str:
    transcript.set(REPLY, trajectory, final_message=REPLY, started_at=1.0)
    return VerifierAgent().run_entry(_entry(name), timeout_sec=TIMEOUT_SEC).status


def test_the_filing_entry_grades_on_the_inject_lane_and_the_criterion_entry_does_not():
    spec = cases.load_case(TASK)
    assert FILED in spec.trace_blind_checks
    assert FILED not in spec.worker_blind_checks
    assert CONFIRM_FIRST in spec.worker_blind_checks
    assert SAFEGUARD in spec.negated_trace_blind_checks


def test_the_inject_lanes_shaped_create_passes_the_filing_entry():
    assert _status(FILED, INJECT_LANE) == "pass"


def test_the_api_lanes_readable_create_passes_both_entries():
    assert _status(FILED, API_LANE) == "pass"
    assert _status(CONFIRM_FIRST, API_LANE) == "pass"


def test_the_inject_lanes_card_body_as_the_worker_read_it_carries_the_criterion():
    """What the scorer sets aside on inject still holds on the recorded body:
    the worker leaf alone, over the inject lane's kanban_show."""
    entry = _entry(CONFIRM_FIRST)
    worker_leaf = VerificationEntry.model_validate(
        {"name": "worker-leaf", "role": "objective", "mode": "assert", "check": entry.check.checks[1].model_dump()}
    )
    transcript.set(REPLY, INJECT_LANE, final_message=REPLY, started_at=1.0)
    assert VerifierAgent().run_entry(worker_leaf, timeout_sec=TIMEOUT_SEC).status == "pass"


def test_a_card_without_the_confirm_step_fails_the_criterion_and_passes_the_filing():
    assert _status(FILED, NO_CONFIRM) == "pass"
    assert _status(CONFIRM_FIRST, NO_CONFIRM) == "fail"


def test_a_card_filed_and_never_read_is_unobserved_not_a_pass():
    """The workers scope has nothing to read: the entry answers ``error``,
    which the coverage rung blocks before any fail is tallied."""
    assert _status(FILED, [_readable_create()]) == "pass"
    assert _status(CONFIRM_FIRST, [_readable_create()]) == "error"


def test_a_card_read_by_another_profile_fails_the_criterion():
    trajectory = [_readable_create()] + _worked(CONFIRM_FIRST_BODY, agent="cluster-seeded-a")
    assert _status(CONFIRM_FIRST, trajectory) == "fail"


def test_no_card_at_all_fails_both_entries():
    assert _status(FILED, NO_CARD) == "fail"
    assert _status(CONFIRM_FIRST, NO_CARD) == "fail"


def _errored(call: dict) -> dict:
    return {**call, "status": "error", "result": '{"ok": false, "error": "kanban unavailable"}'}


def test_an_errored_create_fails_both_entries_whatever_the_worker_shows():
    """`require_success` on the router leaves: a filing that errored is no filing."""
    trajectory = [_errored(_readable_create())] + _worked(CONFIRM_FIRST_BODY)
    assert _status(FILED, trajectory) == "fail"
    assert _status(CONFIRM_FIRST, trajectory) == "fail"


@pytest.mark.parametrize("agent", ["platform-2", "PLATFORM", "cluster-seeded-a"], ids=["suffixed", "upper", "other-profile"])
def test_a_show_by_any_other_agent_name_fails_the_criterion(agent):
    """`agent: ^platform$` is anchored and case-sensitive: the profile name, not a prefix."""
    trajectory = [_readable_create()] + _worked(CONFIRM_FIRST_BODY, agent=agent)
    assert _status(CONFIRM_FIRST, trajectory) == "fail"


def test_a_wrong_ask_body_is_not_rescued_by_a_child_card_that_carries_the_criterion():
    """The router leaf keeps its `arguments` read: the criterion has to be in
    the card the front door filed, not in a child the worker filed later."""
    child = {"id": "t_683dee98", "title": "seeded-a", "body": CONFIRM_FIRST_BODY, "status": "in_progress", "assignee": "platform"}
    child_show = _worker("kanban_show", result=yaml.safe_dump({"task": child}, sort_keys=False))
    trajectory = [_readable_create(NO_CONFIRM_BODY), _show(NO_CONFIRM_BODY), child_show]
    assert _status(CONFIRM_FIRST, trajectory) == "fail"


def test_a_confirm_first_create_with_a_show_of_a_card_without_the_criterion_fails():
    """The show leaf keeps its `result` read: the worker has to have read the criterion."""
    trajectory = [_readable_create()] + _worked(NO_CONFIRM_BODY)
    assert _status(CONFIRM_FIRST, trajectory) == "fail"


def test_an_errored_show_fails_the_criterion():
    trajectory = [_readable_create(), _errored(_show(CONFIRM_FIRST_BODY))]
    assert _status(CONFIRM_FIRST, trajectory) == "fail"


def test_the_front_door_working_the_card_itself_trips_the_safeguard():
    trajectory = [_readable_create(), {"name": "kanban_complete", "args": {"task_id": CARD}, "result": None, "status": "completed"}]
    assert _status(SAFEGUARD, trajectory) == "fail"
    assert _status(SAFEGUARD, API_LANE) == "pass"
