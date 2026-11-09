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

"""chat-real-report-fix-files-confirm-first's gate, and what moved to the judge.

The case gates the filing, the front door's acknowledgement and the
delegation boundary. How the card words its confirm-first criterion is a
Critical Requirements bullet in ``expected_output``, scored by devops-bench's
ChecklistScore, and never gates (#3240): a pattern over the body found a
confirm phrase without telling whether it came first or was negated, and the
inject door sends the Planning Agent's arguments as shapes
(``a2a/hermes-bridge/activity.go``, ``shapeOf``), so no pattern over the
router's call could read the criterion there.

``the-fix-is-filed`` reads the router's ``kanban_create`` by tool name alone,
which both transports keep; it is trace-blind, so the scorer grades it on the
inject lane too. The fixtures are built from the shapes the nightly recorded:
the api lane's readable call (ci-kube-agents-eval-nightly
2113420200605913088 rep 1, 2026-10-23) and the inject lane's shaped call
(2117044258501300224 rep 1, 2026-11-02). They name checkout-gateway, as those
runs did; the filing entry reads no body, so the name doesn't matter to it.

Run:  cd bench && python -m pytest -q tests/test_confirm_first_card_check.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from devops_bench.metrics.checklist import extract_checklist_items
from devops_bench.verification.runner import VerifierAgent
from devops_bench.verification.spec import VerificationEntry

from kube_agents_bench import cases, transcript

TASK = Path(__file__).resolve().parent.parent / "tasks" / "chat-real-report-fix-files-confirm-first" / "task.yaml"
FILED = "the-fix-is-filed"
# The regex entry #3240 retired. Named here so a test can say it stays gone.
CONFIRM_FIRST = "the-fix-is-filed-with-confirm-first"
FLEET = Path(__file__).resolve().parent.parent / "tf"
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
    """A kanban_show by the worker, the card's body in the result."""
    task = {"id": CARD, "title": TITLE, "body": body, "status": "in_progress", "assignee": agent}
    # One line of JSON, as the harness records a worker result (worker_trajectory.py
    # clips and json.dumps it); the body's newlines are two-character escapes there.
    return _worker("kanban_show", result=json.dumps({"task": task}), agent=agent)


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


def test_the_filing_entry_grades_on_both_lanes():
    spec = cases.load_case(TASK)
    assert FILED in spec.trace_blind_checks
    assert FILED not in spec.worker_blind_checks
    assert SAFEGUARD in spec.negated_trace_blind_checks


def test_the_inject_lanes_shaped_create_passes_the_filing_entry():
    assert _status(FILED, INJECT_LANE) == "pass"


def test_the_api_lanes_readable_create_passes_the_filing_entry():
    assert _status(FILED, API_LANE) == "pass"


def test_no_card_at_all_fails_the_filing_entry():
    assert _status(FILED, NO_CARD) == "fail"
    assert _status(FILED, []) == "fail"


def _errored(call: dict) -> dict:
    return {**call, "status": "error", "result": '{"ok": false, "error": "kanban unavailable"}'}


def test_an_errored_create_fails_the_filing_entry_whatever_the_worker_shows():
    """`require_success`: a filing that errored is no filing."""
    assert _status(FILED, [_errored(_readable_create())] + _worked(CONFIRM_FIRST_BODY)) == "fail"


def test_a_card_without_the_confirm_step_passes_the_gate():
    """The consequence of #3240, stated: the card's wording is the judge's, so
    a card with no confirm step no longer turns the case red. The delegation
    boundary and the lane's no-GitHub-write safeguard still gate what a worker
    does with it."""
    assert _status(FILED, NO_CONFIRM) == "pass"


def test_no_gate_entry_reads_the_cards_wording():
    """The criterion stays judged: no entry by the retired name, and no entry
    pattern-matches the router's `kanban_create` arguments or a worker's
    `kanban_show` result, the two places the retired regex read the body."""
    doc = yaml.safe_load(TASK.read_text(encoding="utf-8"))
    assert CONFIRM_FIRST not in [e["name"] for e in doc["verification_spec"]]

    def leaves(check: dict):
        yield check
        for child in check.get("checks", []):
            yield from leaves(child)

    for entry in doc["verification_spec"]:
        for leaf in leaves(entry["check"]):
            if leaf.get("type") == "tool_called" and {"kanban_create", "kanban_show"} & set(leaf.get("tool_names", [])):
                assert "arguments" not in leaf and "result" not in leaf, entry["name"]


def test_the_expected_output_carries_the_three_judged_requirements():
    """The bullets devops-bench's Checklist judge scores one by one: the ack,
    the card's first criterion, and the specialist's answer."""
    doc = yaml.safe_load(TASK.read_text(encoding="utf-8"))
    items = extract_checklist_items(doc["expected_output"], use_mcp=True)
    assert len(items) == 3, items
    ack, criterion, answer = items
    assert "first post" in ack and "asks the user nothing" in ack
    assert "first acceptance criterion" in criterion and "confirms invoice-relay exists" in criterion
    assert "not negated" in criterion
    assert "could not find invoice-relay" in answer and "changed nothing" in answer


def test_the_report_names_no_workload_the_fleet_has():
    """#3629: the report's workloads and namespace exist on no fleet cluster,
    so a worker that skips the confirm step has nothing real to act on."""
    doc = yaml.safe_load(TASK.read_text(encoding="utf-8"))
    prompt = doc["prompt"]
    assert "checkout-gateway" not in prompt and "seeded-reliability" not in prompt
    fleet = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in FLEET.rglob("*") if p.is_file())
    for name in ("invoice-relay", "quote-cache", "seeded-billing"):
        assert name in prompt
        assert not re.search(rf"\b{re.escape(name)}\b", fleet), f"{name} is on the fleet"


def test_the_front_door_working_the_card_itself_trips_the_safeguard():
    trajectory = [_readable_create(), {"name": "kanban_complete", "args": {"task_id": CARD}, "result": None, "status": "completed"}]
    assert _status(SAFEGUARD, trajectory) == "fail"
    assert _status(SAFEGUARD, API_LANE) == "pass"
