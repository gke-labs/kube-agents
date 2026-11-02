"""tse-node-notready-pleg-probe's delegation entries, on both transports.

The inject door sends the Planning Agent's arguments as shapes
(``a2a/hermes-bridge/activity.go``, ``shapeOf``): its ``kanban_create`` arrives
as ``{"assignee": "<string, 3 chars>", ...}``, so no pattern over the router's
call can name the assignee. The case reads the delegation in two entries.
``delegated-to-tse-specialist`` reads the router's ``kanban_create`` by tool
name alone, which both transports keep; it is trace-blind, so the scorer
grades it on the inject lane too. ``tse-specialist-completed-the-card`` reads
the router's card and then the worker's calls, which the harness reads back
from the pod tagged with the profile; it is worker-blind, graded on the api
lane and set aside on inject.
Every fixture runs through the whole entry as the runner grades it, built
from the shapes the nightly recorded.

Run:  cd bench && python -m pytest -q tests/test_tse_delegation_check.py
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from devops_bench.verification.runner import VerifierAgent
from devops_bench.verification.spec import VerificationEntry
from kube_agents_bench import cases, transcript

TASK = Path(__file__).resolve().parent.parent / "tasks" / "tse-node-notready-pleg-probe" / "task.yaml"
ROUTER_ENTRY = "delegated-to-tse-specialist"
WORKER_ENTRY = "tse-specialist-completed-the-card"
TIMEOUT_SEC = 5.0
CARD = "t_55b75f58"

# The router's call as the inject door records it (build 2115594709677117440 rep 1).
SHAPED_CREATE = {
    "name": "kanban_create",
    "args": {"assignee": "<string, 3 chars>", "body": "<string, 665 chars>", "title": "<string, 84 chars>"},
    "result": None,
    "status": "completed",
}
# The same call as the api transport records it, arguments readable.
READABLE_CREATE = {
    "name": "kanban_create",
    "args": {"assignee": "tse", "body": "Prepare a read-only escalation bundle.", "title": "PLEG NotReady bundle"},
    "result": '{"ok": true, "task_id": "t_55b75f58"}',
    "status": "completed",
}
# A router turn that answered without filing a card (the today lane, 2026-10-26).
NO_CARD = [{"name": "terminal", "args": {"command": "<string, 40 chars>"}, "status": "completed"}]
REPORT = "## 1. Environment & Cluster Identity\n...\n## 5. Specific Request for Google Cloud Support\n"


def _worker(tool: str, agent: str = "tse", status: str = "completed", **args) -> dict:
    return {
        "name": tool,
        "args": args,
        "result": '{"ok": true, "task_id": "t_55b75f58"}',
        "status": status,
        "agent": agent,
        "task": CARD,
        "session": "20261029_025318_385f40",
        "at": 1793242440.0,
    }


def _tse_run(status: str = "completed", agent: str = "tse") -> list[dict]:
    """The worker's calls as the nightly recorded them: show, view the skill, heartbeat, complete."""
    return [
        _worker("kanban_show", agent=agent),
        _worker("skill_view", agent=agent, name="support-escalation-bundle"),
        _worker("kanban_heartbeat", agent=agent, note="Loaded support-escalation-bundle skill."),
        _worker("kanban_complete", agent=agent, status=status, raw='{"summary": "Prepared the 5-section bundle."}'),
    ]


@pytest.fixture(autouse=True)
def _clean_transcript():
    transcript.clear()
    yield
    transcript.clear()


def _entry(name: str) -> VerificationEntry:
    doc = yaml.safe_load(TASK.read_text())
    (entry,) = [e for e in doc["verification_spec"] if e.get("name") == name]
    return VerificationEntry.model_validate(entry)


def _status(name: str, trajectory: list[dict]) -> str:
    """The whole entry as the runner grades it, on a run that answered with the bundle."""
    transcript.set(REPORT, trajectory, final_message=REPORT, started_at=1.0)
    return VerifierAgent().run_entry(_entry(name), timeout_sec=TIMEOUT_SEC).status


def test_the_router_entry_grades_on_the_inject_lane_and_the_worker_entry_does_not():
    spec = cases.load_case(TASK)
    assert ROUTER_ENTRY in spec.trace_blind_checks
    assert ROUTER_ENTRY not in spec.worker_blind_checks
    assert WORKER_ENTRY in spec.worker_blind_checks


def test_the_inject_lanes_shaped_create_passes_both_entries():
    assert _status(ROUTER_ENTRY, [SHAPED_CREATE] + _tse_run()) == "pass"
    assert _status(WORKER_ENTRY, [SHAPED_CREATE] + _tse_run()) == "pass"


def test_the_api_lanes_readable_create_passes_both_entries():
    assert _status(ROUTER_ENTRY, [READABLE_CREATE] + _tse_run()) == "pass"
    assert _status(WORKER_ENTRY, [READABLE_CREATE] + _tse_run()) == "pass"


def test_no_delegation_at_all_fails_both_entries():
    assert _status(ROUTER_ENTRY, []) == "fail"
    assert _status(ROUTER_ENTRY, NO_CARD) == "fail"
    assert _status(ROUTER_ENTRY, [{**READABLE_CREATE, "status": "error"}] + _tse_run()) == "fail"
    # The worker entry reads the router's card first, so a run with no card is a
    # definite fail on the api lane, not an unobserved error that blocks the run.
    assert _status(WORKER_ENTRY, []) == "fail"
    assert _status(WORKER_ENTRY, NO_CARD) == "fail"


def test_a_card_filed_and_never_worked_is_unobserved_not_a_pass():
    # No tagged entry at all: the workers scope cannot see the card, so the
    # entry is an error, which coverage blocks, never a pass.
    assert _status(ROUTER_ENTRY, [SHAPED_CREATE]) == "pass"
    assert _status(WORKER_ENTRY, [SHAPED_CREATE]) == "error"


def test_another_profile_completing_the_card_fails_the_worker_entry():
    assert _status(WORKER_ENTRY, [SHAPED_CREATE] + _tse_run(agent="platform")) == "fail"
    assert _status(WORKER_ENTRY, [READABLE_CREATE] + _tse_run(agent="cluster-seeded-a")) == "fail"
    # The selector is anchored: a profile whose name starts with tse is not tse.
    assert _status(WORKER_ENTRY, [SHAPED_CREATE] + _tse_run(agent="tse-2")) == "fail"


def test_a_tse_card_started_and_not_completed_fails_the_worker_entry():
    assert _status(WORKER_ENTRY, [SHAPED_CREATE] + _tse_run()[:-1]) == "fail"
    assert _status(WORKER_ENTRY, [SHAPED_CREATE] + _tse_run(status="error")) == "fail"
