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

"""session-crashloop-question-loads-a-skill grades the session on inject and nothing on api.

The case has no inject-only marker to lean on (the bench has none), so each
check is written to pass on an api record: the positive checks behind a
``none`` of the inject transport's task marker, the skill-file check over
Claude Code tool names a Hermes trajectory never carries. Nor does an inject
record say whether the session cluster view was on, so the case branches on
the one behavioural marker, a Bash call (the worker adapter allows Bash only
with the view): with one, the view-on rules apply (a gke- skill, kubectl, no
delegation); without, the skill check skips and the answer alone grades.
These tests run the case's own entries through devops-bench's runner against
an api run, main's and the branch's sessions with the view on, a delegated
run with the view off (whose final trace is the wake turn's), and the ways
each can still be wrong: delegating with the view, an ungrounded answer, and
reading the skill files by hand.

The inject records use the transport's own entry names, and the scorer's
reading of them is asserted too, so a renamed marker fails here rather than
turning the api guard into a check that passes everywhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from devops_bench.verification.runner import VerifierAgent
from devops_bench.verification.spec import VerificationEntry

from kube_agents_bench import inject_transport, scoring, transcript
from kube_agents_bench.cases import load_case

CASE_ID = "session-crashloop-question-loads-a-skill"
TASK = Path(__file__).resolve().parent.parent / "tasks" / CASE_ID / "task.yaml"

SKILL = "the-session-loads-a-gke-skill"
GROUNDED = "the-answer-names-the-oom"
NO_SKILL_READ = "the-session-does-not-read-its-skill-files"

ANSWER = (
    "payments-api in seeded-debug is OOMKilled on every start: lastState.terminated.reason "
    "is OOMKilled, 14 restarts, memory limit 64Mi. Propose raising the limit."
)
GENERAL_ANSWER = "A crashloop usually means the process exits; check the logs and events."


def _entry(name: str) -> VerificationEntry:
    doc = yaml.safe_load(TASK.read_text(encoding="utf-8"))
    (raw,) = [e for e in doc["verification_spec"] if e.get("name") == name]
    return VerificationEntry.model_validate(raw)


def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "args": args, "result": None, "status": "completed"}


def _inject(*calls: dict[str, Any]) -> list[dict[str, Any]]:
    """An inject record: the task marker, the terminal, then the activity
    block (marker first, as ``Fold.activity_entries`` writes it)."""
    return [
        _call(inject_transport.EVENT_ENTRY_TASK, {"taskId": "t-1"}),
        _call(inject_transport.EVENT_ENTRY_STATUS, {"state": "completed", "final": True}),
        _call(inject_transport.EVENT_ENTRY_ACTIVITY, {"calls": len(calls), "dropped": 0}),
        *calls,
    ]


KUBECTL = _call("Bash", {"command": "kubectl get pods -n seeded-debug"})
SKILL_CALL = _call("Skill", {"skill": "gke-workload-troubleshooting"})
DELEGATE = _call("mcp__a2a__delegate", {"to": "platform", "text": "why is payments-api crashlooping"})

# An api record: the platform agent (Hermes) answering the prompt as text.
API = [
    _call("skill_view", {"name": "gke-workload-troubleshooting"}),
    _call("terminal", {"command": "kubectl get pods -n seeded-debug"}),
    _call("read_file", {"path": "/opt/data/skills/gke-workload-troubleshooting/SKILL.md"}),
]


def _status(name: str, output: str, trajectory: list[dict[str, Any]]) -> str:
    transcript.set(output, trajectory)
    try:
        return VerifierAgent().run_entry(_entry(name), timeout_sec=5.0).status
    finally:
        transcript.clear()


def test_the_records_read_as_their_transport():
    assert scoring._inject_record(_inject(SKILL_CALL))
    assert not scoring._inject_blind(_inject(SKILL_CALL))
    assert not scoring._inject_record(API)


@pytest.mark.parametrize("name", [SKILL, GROUNDED, NO_SKILL_READ])
@pytest.mark.parametrize("output", [ANSWER, GENERAL_ANSWER, ""])
def test_no_check_can_fail_an_api_record(name: str, output: str):
    assert _status(name, output, API) == "pass"


def test_main_is_red_on_the_skill_check_alone():
    """Main's session: the view on, the OOM found, no skill to load."""
    record = _inject(KUBECTL)
    assert _status(SKILL, ANSWER, record) == "fail"
    assert _status(GROUNDED, ANSWER, record) == "pass"
    assert _status(NO_SKILL_READ, ANSWER, record) == "pass"


def test_the_branch_is_green():
    record = _inject(SKILL_CALL, KUBECTL)
    for name in (SKILL, GROUNDED, NO_SKILL_READ):
        assert _status(name, ANSWER, record) == "pass", name


def test_the_older_skill_key_counts():
    record = _inject(_call("Skill", {"command": "gke-stall-detection"}), KUBECTL)
    assert _status(SKILL, ANSWER, record) == "pass"


def test_a_skill_that_is_not_a_shipped_gke_skill_does_not_count():
    assert _status(SKILL, ANSWER, _inject(_call("Skill", {"skill": "review"}), KUBECTL)) == "fail"


def test_delegating_with_the_view_fails_the_grounded_check():
    """The view was on (Bash ran) and the session still handed the question
    on: the answer is the child's, not the session's."""
    assert _status(GROUNDED, ANSWER, _inject(SKILL_CALL, KUBECTL, DELEGATE)) == "fail"


def test_bash_without_kubectl_fails_the_grounded_check():
    """The view was on, but the session never read the cluster with kubectl."""
    record = _inject(SKILL_CALL, _call("Bash", {"command": "gcloud container clusters list"}))
    assert _status(GROUNDED, ANSWER, record) == "fail"


def test_an_answer_without_the_oom_fails_the_grounded_check():
    assert _status(GROUNDED, GENERAL_ANSWER, _inject(SKILL_CALL, KUBECTL)) == "fail"


# View off: the session delegates, and the probe of the root follows the
# chain to the wake turn, so the final trace is the wake's calls -- usually
# none. A refused delegation forms no chain and leaves the delegate call in.
@pytest.mark.parametrize("record", [_inject(), _inject(DELEGATE)], ids=["wake", "no-chain"])
def test_view_off_a_relayed_grounded_answer_is_green(record: list[dict[str, Any]]):
    for name in (SKILL, GROUNDED, NO_SKILL_READ):
        assert _status(name, ANSWER, record) == "pass", name


def test_view_off_an_ungrounded_answer_fails():
    assert _status(GROUNDED, GENERAL_ANSWER, _inject()) == "fail"


def test_view_off_a_pod_search_fails():
    record = _inject(_call("Glob", {"pattern": "**/*", "path": "/home/node"}))
    assert _status(NO_SKILL_READ, ANSWER, record) == "fail"


def test_a_view_on_session_that_never_runs_bash_reads_as_view_off():
    """The documented cost of having no view marker: without a Bash call the
    record cannot say the view was on, so the skill check skips."""
    record = _inject(DELEGATE)
    assert _status(SKILL, ANSWER, record) == "pass"
    assert _status(GROUNDED, ANSWER, record) == "pass"


@pytest.mark.parametrize(
    "call",
    [
        _call("Read", {"file_path": "/home/node/.claude/skills/gke-workload-troubleshooting/SKILL.md"}),
        _call("Glob", {"pattern": "**/SKILL.md"}),
        _call("Glob", {"pattern": "*", "path": "/home/node/.claude/skills"}),
        _call("Grep", {"pattern": "OOMKilled", "path": "/home/node/.claude/skills"}),
        # Through a parent of the skills tree, naming no skill file.
        _call("Grep", {"pattern": "OOMKilled", "path": "/home/node/.claude"}),
        _call("Glob", {"pattern": "**/*.md", "path": "/home/node"}),
        _call("Grep", {"pattern": "gke-", "glob": "**/SKILL.md"}),
        _call("Glob", {"pattern": "/home/node/**/*.md"}),
        _call("Glob", {"pattern": "**/*.md", "path": "/"}),
        _call("Grep", {"pattern": "OOMKilled", "path": "/home/node"}),
        _call("Read", {"file_path": "/home/node/.claude/CLAUDE.md"}),
        # A recursive glob over the home or the root with no Markdown leaf
        # still lists the skill files.
        _call("Glob", {"pattern": "**/*", "path": "/home/node"}),
        _call("Glob", {"pattern": "**", "path": "/"}),
        _call("Glob", {"pattern": "**/SKILL*", "path": "/home/node"}),
        _call("Glob", {"pattern": "/home/node/**"}),
        _call("Glob", {"pattern": "/home/node/**/*"}),
        _call("Glob", {"pattern": "~/**/kubectl"}),
    ],
)
def test_reading_the_skill_files_by_hand_fails(call: dict[str, Any]):
    assert _status(NO_SKILL_READ, ANSWER, _inject(SKILL_CALL, call)) == "fail"


def test_other_reads_do_not_trip_the_skill_file_check():
    record = _inject(
        SKILL_CALL,
        _call("Read", {"file_path": "/scratch/notes.txt"}),
        _call("Glob", {"pattern": "*.yaml"}),
        _call("Glob", {"pattern": "**/*.md", "path": "/scratch"}),
        _call("Grep", {"pattern": "OOMKilled", "path": "/scratch"}),
        _call("Glob", {"pattern": "/scratch/**/*"}),
        _call("Bash", {"command": "kubectl get pods -n seeded-debug"}),
    )
    assert _status(NO_SKILL_READ, ANSWER, record) == "pass"


def test_the_scorer_sets_the_tool_only_checks_aside_without_a_trace():
    """The two tool_called-only entries are trace-blind on the inject lane;
    the grounded one mixes in a report check and is always graded."""
    spec = load_case(TASK)
    assert spec.trace_blind_checks == {SKILL, NO_SKILL_READ}
    assert spec.negated_trace_blind_checks == {NO_SKILL_READ}
    assert spec.worker_blind_checks == frozenset()
