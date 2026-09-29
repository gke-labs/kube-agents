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

import json
import textwrap

from kube_agents_bench import score, trajectory, transcript
from kube_agents_bench.transcript import TranscriptSnapshot


def test_round_trip():
    snap = TranscriptSnapshot(
        output="progress\n\ndone",
        trajectory=[
            {"name": "kanban_create", "args": {"assignee": "platform"}, "result": "t_1", "status": "completed"},
            {
                "name": "terminal",
                "args": {"command": "cat /etc/gitops/managed_repos"},
                "result": "[]",
                "status": "completed",
                "agent": "platform",
                "task": "t_1",
            },
        ],
        prompt_head="Which GitOps repositories do you manage?",
        started_at=1790000000.0,
        final_message="done",
        worker_commands=[{"task": "t_1", "command": "cat /etc/gitops/managed_repos"}],
        worker_capture_gaps=[],
    )
    doc = json.loads(json.dumps(trajectory.from_snapshot(snap, "Which GitOps repositories do you manage?")))
    assert [i["type"] for i in doc["output"]] == [
        "function_call", "function_call_output", "function_call", "function_call_output", "message",
    ]
    assert doc["output"][2]["kubeagents.x-k8s.io/agent"] == "platform"
    assert trajectory.to_snapshot(doc) == snap


# Another agent's run: standard fields only.
OTHER_AGENT = {
    "created_at": 1790000000,
    "input": [{"role": "user", "content": "Which GitOps repositories do you manage?"}],
    "output": [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Let me check."}]},
        {"type": "function_call", "call_id": "a", "name": "Bash",
         "arguments": "{\"command\": \"cat /etc/gitops/managed_repos\"}"},
        {"type": "function_call_output", "call_id": "a", "output": "[...kind-infra...]"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "kind-infra"}]},
    ],
}


def test_derived_from_standard_fields():
    snap = trajectory.to_snapshot(OTHER_AGENT)
    assert snap.final_message == "kind-infra"
    assert snap.worker_commands == [{"task": "", "command": "cat /etc/gitops/managed_repos"}]
    assert snap.trajectory[0]["result"] == "[...kind-infra...]"
    assert snap.started_at == 1790000000.0


def test_score(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ID", "kind")
    task = tmp_path / "task.yaml"
    task.write_text(textwrap.dedent("""\
        verification_spec:
          - name: names-the-repo
            role: objective
            mode: assert
            check:
              type: report_contains
              any_of_phrases: ["{{PROJECT_ID}}-infra"]
          - name: not-discovered
            role: objective
            mode: assert
            check:
              type: worker_commands
              forbidden_patterns: ['\\bkubectl\\b']
        """))
    (tmp_path / "trajectory.json").write_text(json.dumps(OTHER_AGENT))
    try:
        assert score.main([str(task), str(tmp_path)]) == 0
    finally:
        transcript.clear()
    verdict = json.loads((tmp_path / "verdict.json").read_text())
    assert [e["status"] for e in verdict["verification_report"]] == ["pass", "pass"]
    assert verdict["scores"]["correctness"] == 1.0
