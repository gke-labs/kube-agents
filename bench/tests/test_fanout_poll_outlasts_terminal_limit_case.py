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

"""Unit tests for fanout-poll-outlasts-terminal-limit's verification checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from devops_bench.verification.runner import VerifierAgent
from devops_bench.verification.spec import VerificationEntry

from kube_agents_bench import transcript

CASE_ID = "fanout-poll-outlasts-terminal-limit"
REPO_ROOT = Path(__file__).resolve().parents[2]
TASK = REPO_ROOT / "bench" / "tasks" / CASE_ID / "task.yaml"

NOT_CUT = "no-worker-command-cut-at-60s"
COUNTS = "the-answer-gives-the-counts"

CLUSTER_A = "cluster-example-project-seeded-a-us-central1-c"
CLUSTER_B = "cluster-example-project-seeded-b-us-central1-b"

# What the terminal tool returns, as the trajectory stores it.
CUT = '{"output": "[Command timed out after 60s]", "exit_code": 124, "error": null}'
SLEPT = '{"output": "", "exit_code": 0, "error": null}'
CARD_JSON = '{"task": {"id": "t_a", "status": "running"}}'
PODS = '{"output": "14", "exit_code": 0, "error": null}'

ANSWER = "seeded-a: 14 pods running in kube-system\nseeded-b: 12 pods running in kube-system"


def _entry(name: str) -> VerificationEntry:
    doc = yaml.safe_load(TASK.read_text(encoding="utf-8"))
    (raw,) = [e for e in doc["verification_spec"] if e.get("name") == name]
    return VerificationEntry.model_validate(raw)


def _call(card: str, at: float, command: str, result: str, agent: str = "platform") -> dict[str, Any]:
    return {
        "name": "terminal",
        "agent": agent,
        "task": card,
        "at": at,
        "args": {"command": command},
        "result": result,
        "status": "completed",
    }


def _status(name: str, calls: list[dict[str, Any]], output: str = ANSWER) -> str:
    transcript.set(
        output,
        calls,
        final_message=output,
        worker_commands=[{"task": c["task"], "command": c["args"]["command"]} for c in calls],
        worker_capture_gaps=[],
        worker_card_parents={"card-a": "card-platform", "card-b": "card-platform"},
    )
    try:
        return VerifierAgent().run_entry(_entry(name), timeout_sec=5.0).status
    finally:
        transcript.clear()


def _fanout(poll_result: str, cluster_result: str = PODS) -> list[dict[str, Any]]:
    return [
        _call("card-platform", 1.0, "python3 -c 'print(1)'", CARD_JSON),
        _call("card-a", 5.0, "kubectl get pods -n kube-system --no-headers | wc -l", cluster_result, agent=CLUSTER_A),
        _call("card-b", 6.0, "kubectl get pods -n kube-system --no-headers | wc -l", PODS, agent=CLUSTER_B),
        _call("card-platform", 10.0, "sleep 60", poll_result),
        _call("card-platform", 80.0, "sleep 60", SLEPT),
    ]


def test_polling_that_finishes_passes():
    assert _status(NOT_CUT, _fanout(SLEPT)) == "pass"


def test_a_polling_sleep_cut_at_60s_fails():
    assert _status(NOT_CUT, _fanout(CUT)) == "fail"


def test_a_cluster_agent_command_cut_at_60s_fails():
    calls = _fanout(SLEPT)
    calls.insert(2, _call("card-a", 5.5, "python3 /opt/data/scripts/workload_triage.py", CUT, agent=CLUSTER_A))
    assert _status(NOT_CUT, calls) == "fail"


def test_a_cut_at_another_limit_is_not_the_defect():
    cut_at_180 = CUT.replace("60s", "180s")
    assert _status(NOT_CUT, _fanout(cut_at_180)) == "pass"


def test_a_run_that_never_polled_fails():
    calls = [c for c in _fanout(SLEPT) if not c["args"]["command"].startswith("sleep")]
    assert _status(NOT_CUT, calls) == "fail"


def test_a_sleep_behind_a_chain_counts_as_polling():
    calls = [c for c in _fanout(SLEPT) if not c["args"]["command"].startswith("sleep")]
    calls.append(_call("card-platform", 90.0, "echo waiting && sleep 60", SLEPT))
    assert _status(NOT_CUT, calls) == "pass"


def test_the_answer_needs_the_namespace_and_a_count():
    assert _status(COUNTS, _fanout(SLEPT)) == "pass"
    assert _status(COUNTS, _fanout(SLEPT), output="All clusters look fine.") == "fail"
