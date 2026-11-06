"""chat-routing-named-cluster-under-summarized-roster's route entries, on both transports.

The inject door sends the Planning Agent's arguments as shapes
(``a2a/hermes-bridge/activity.go``, ``shapeOf``): its ``kanban_create`` arrives
as ``{"assignee": "<string, 48 chars>", ...}``, so no pattern over the router's
call can name the assignee. The case reads the delegation and the route in
separate entries. ``work-was-delegated-through-kanban`` reads the router's
``kanban_create`` by tool name alone, which both transports keep; it is
trace-blind, so the scorer grades it on the inject lane too.
``seeded-a-cluster-agent-did-the-work`` reads the route: the router's card with
its readable ``assignee`` and then the workers, which the harness reads back
from the pods tagged with the profile; it is worker-blind, graded on the api
lane and set aside on inject.
Every fixture runs through the whole entry as the runner grades it, built
from the shapes the nightly recorded.

Run:  cd bench && python -m pytest -q tests/test_named_cluster_route_check.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from devops_bench.verification.runner import VerifierAgent
from devops_bench.verification.spec import VerificationEntry
from kube_agents_bench import cases, scoring, transcript

TASK = (
    Path(__file__).resolve().parent.parent
    / "tasks"
    / "chat-routing-named-cluster-under-summarized-roster"
    / "task.yaml"
)
ROUTER_ENTRY = "work-was-delegated-through-kanban"
WORKER_ENTRY = "seeded-a-cluster-agent-did-the-work"
TIMEOUT_SEC = 5.0
CARD = "t_c269e9b2"
SEEDED_A = "cluster-p-seeded-a-us-central1-a"
SEEDED_B = "cluster-p-seeded-b-us-central1-b"

# The router's call as the inject door records it (build 2118493573778247680 rep 1).
SHAPED_CREATE = {
    "name": "kanban_create",
    "args": {
        "assignee": "<string, 48 chars>",
        "body": "<string, 297 chars>",
        "title": "<string, 35 chars>",
    },
    "result": None,
    "status": "completed",
}
# The same call as the api transport records it, arguments readable.
READABLE_CREATE = {
    "name": "kanban_create",
    "args": {
        "assignee": SEEDED_A,
        "body": "List the namespaces in seeded-a.",
        "title": "List namespaces in seeded-a",
    },
    "result": '{"ok": true, "task_id": "t_c269e9b2"}',
    "status": "completed",
}
# A router turn that answered without filing a card.
NO_CARD = [
    {
        "name": "terminal",
        "args": {"command": "<string, 40 chars>"},
        "status": "completed",
    }
]
# The inject transport's own entries, which mark a record as the inject lane's
# (scoring.is_inject_record) and as one whose door showed the trace.
INJECT_MARKERS = [
    {
        "name": "inject.task",
        "args": {"taskId": "task-43a7d48b651b04e0"},
        "result": None,
        "status": "completed",
    },
    {
        "name": "a2a.activity",
        "args": {"taskId": "task-43a7d48b651b04e0"},
        "result": None,
        "status": "completed",
    },
]
REPORT = "**The seeded-a cluster has 25 namespaces.** kube-system, seeded-debug, ..."


def _worker(
    tool: str, agent: str = SEEDED_A, status: str = "completed", **args
) -> dict:
    return {
        "name": tool,
        "args": args,
        "result": '{"ok": true, "task_id": "t_c269e9b2"}',
        "status": status,
        "agent": agent,
        "task": CARD,
        "session": "20261106_092755_b20e35",
        "at": 1793957298.0858002,
    }


def _cluster_run(agent: str = SEEDED_A) -> list[dict]:
    """The worker's calls as the nightly recorded them: show, two terminals, complete."""
    return [
        _worker("kanban_show", agent=agent),
        _worker("terminal", agent=agent, command="kubectl get namespaces"),
        _worker("terminal", agent=agent, command="kubectl get namespaces -o wide"),
        _worker(
            "kanban_complete",
            agent=agent,
            raw='{"summary": "Listed all 25 namespaces on seeded-a."}',
        ),
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
    """The whole entry as the runner grades it, on a run that answered with the namespaces."""
    transcript.set(REPORT, trajectory, final_message=REPORT, started_at=1.0)
    return VerifierAgent().run_entry(_entry(name), timeout_sec=TIMEOUT_SEC).status


def test_the_router_entry_grades_on_the_inject_lane_and_the_worker_entry_does_not():
    spec = cases.load_case(TASK)
    assert ROUTER_ENTRY in spec.trace_blind_checks
    assert ROUTER_ENTRY not in spec.worker_blind_checks
    assert WORKER_ENTRY in spec.worker_blind_checks


def _reads_an_argument(node: Any) -> bool:
    if isinstance(node, dict):
        return "arguments" in node or any(_reads_an_argument(v) for v in node.values())
    if isinstance(node, list):
        return any(_reads_an_argument(item) for item in node)
    return False


def test_every_entry_that_reads_a_router_argument_is_worker_blind():
    # The shape the inject door sends cannot satisfy an argument pattern, so an
    # entry that reads one is graded only where the argument is readable.
    spec = cases.load_case(TASK)
    doc = yaml.safe_load(TASK.read_text())
    reading = {
        e["name"] for e in doc["verification_spec"] if _reads_an_argument(e["check"])
    }
    assert reading == {WORKER_ENTRY}
    assert reading <= set(spec.worker_blind_checks)


def _graded_record(trajectory: list[dict]) -> dict:
    """One results.json record as devops-bench writes it: every objective entry
    graded by the runner over the trajectory, the safeguard recorded as held."""
    doc = yaml.safe_load(TASK.read_text())
    transcript.set(REPORT, trajectory, final_message=REPORT, started_at=1.0)
    report = []
    for entry in doc["verification_spec"]:
        if entry.get("role") != "objective":
            report.append(
                {
                    "name": entry["name"],
                    "role": entry["role"],
                    "severity": entry.get("severity"),
                    "weight": 1.0,
                    "mode": "assert",
                    "success": True,
                    "status": "pass",
                    "reason": "held",
                    "elapsed_time": 0.0,
                    "children": [],
                }
            )
            continue
        res = VerifierAgent().run_entry(
            VerificationEntry.model_validate(entry), timeout_sec=TIMEOUT_SEC
        )
        report.append(
            {
                "name": entry["name"],
                "role": "objective",
                "severity": None,
                "weight": 1.0,
                "mode": entry.get("mode", "assert"),
                "success": res.status == "pass",
                "status": res.status,
                "reason": res.reason,
                "elapsed_time": 0.0,
                "children": [],
            }
        )
    objectives = [r for r in report if r["role"] == "objective"]
    correctness = sum(r["status"] == "pass" for r in objectives) / len(objectives)
    return {
        "input": "Which namespaces exist in the seeded-a cluster?",
        "output": REPORT,
        "name": doc["name"],
        "folder": doc["id"],
        "status": "success",
        "error": None,
        "latency": 181.9,
        "tokens": {"input": 43255, "output": 232, "total": 153845, "partial": False},
        "trajectory": trajectory,
        "verification_spec": doc["verification_spec"],
        "verification_report": report,
        "verification_status": "evaluated",
        "scores": {
            "OutcomeValidity": {
                "score": 1.0,
                "success": True,
                "reason": "named the namespaces",
            },
            "VerificationCorrectness": correctness,
            "VerificationCatastrophic": 1.0,
            "VerificationCoverage": 1.0,
            "OutcomeScore": {"score": correctness, "version": "v1", "reason": "test"},
        },
    }


def test_the_scorer_sets_the_route_entry_aside_on_an_inject_record(tmp_path):
    # The inject lane's record, as the nightly grades it through classify_rep:
    # the route entry is reported not applicable and the repetition passes on
    # the delegation and the answer.
    run = tmp_path / "inject_001"
    run.mkdir()
    (run / "results.json").write_text(
        json.dumps([_graded_record(INJECT_MARKERS + [SHAPED_CREATE] + _cluster_run())])
    )
    rep = scoring.classify_rep(cases.load_case(TASK), run, 1)
    assert rep.outcome == "pass"
    assert rep.not_applicable_checks == [WORKER_ENTRY]
    assert rep.correctness == 1.0 and rep.coverage == 1.0
    # The same lane with no card at all: the delegation entry is graded there
    # and fails the repetition.
    run2 = tmp_path / "inject_002"
    run2.mkdir()
    (run2 / "results.json").write_text(
        json.dumps([_graded_record(INJECT_MARKERS + _cluster_run())])
    )
    rep2 = scoring.classify_rep(cases.load_case(TASK), run2, 1)
    assert rep2.outcome == "fail"
    assert [f.split(":")[0] for f in rep2.failed_checks] == [ROUTER_ENTRY]


def test_the_inject_lanes_shaped_create_passes_the_router_entry():
    assert _status(ROUTER_ENTRY, [SHAPED_CREATE] + _cluster_run()) == "pass"
    # The worker entry keeps the assignee read, which a shaped argument cannot
    # satisfy; the scorer never grades this entry on an inject record, so the
    # runner's honest answer there is a fail that nothing tallies.
    assert _status(WORKER_ENTRY, [SHAPED_CREATE] + _cluster_run()) == "fail"


def test_the_api_lanes_readable_create_passes_both_entries():
    assert _status(ROUTER_ENTRY, [READABLE_CREATE] + _cluster_run()) == "pass"
    assert _status(WORKER_ENTRY, [READABLE_CREATE] + _cluster_run()) == "pass"


def test_no_delegation_at_all_fails_both_entries():
    assert _status(ROUTER_ENTRY, []) == "fail"
    assert _status(ROUTER_ENTRY, NO_CARD) == "fail"
    assert (
        _status(ROUTER_ENTRY, [{**READABLE_CREATE, "status": "error"}] + _cluster_run())
        == "fail"
    )
    # A card whose creation errored filed nothing, on the worker entry too.
    assert (
        _status(WORKER_ENTRY, [{**READABLE_CREATE, "status": "error"}] + _cluster_run())
        == "fail"
    )
    # The worker entry reads the router's card first, so a run with no card is a
    # definite fail on the api lane, not an unobserved error that blocks the run.
    assert _status(WORKER_ENTRY, []) == "fail"
    assert _status(WORKER_ENTRY, NO_CARD) == "fail"


def test_a_card_filed_and_never_worked_is_unobserved_not_a_pass():
    # No tagged entry at all: the workers scope cannot see the card, so the
    # entry is an error, which coverage blocks, never a pass.
    assert _status(ROUTER_ENTRY, [SHAPED_CREATE]) == "pass"
    assert _status(WORKER_ENTRY, [READABLE_CREATE]) == "error"


def test_another_clusters_agent_doing_the_work_fails_the_worker_entry():
    to_b = {
        **READABLE_CREATE,
        "args": {**READABLE_CREATE["args"], "assignee": SEEDED_B},
    }
    assert _status(WORKER_ENTRY, [to_b] + _cluster_run(agent=SEEDED_B)) == "fail"
    assert _status(ROUTER_ENTRY, [to_b] + _cluster_run(agent=SEEDED_B)) == "pass"


def test_a_card_filed_to_platform_fails_the_worker_entry_whoever_completes_it():
    # The router filed the card to platform; a platform worker fanned a child out
    # to seeded-a's agent, which completed it. Not the route the case asks for.
    to_platform = {
        **READABLE_CREATE,
        "args": {**READABLE_CREATE["args"], "assignee": "platform"},
    }
    assert _status(WORKER_ENTRY, [to_platform] + _cluster_run()) == "fail"
    assert _status(ROUTER_ENTRY, [to_platform] + _cluster_run()) == "pass"
