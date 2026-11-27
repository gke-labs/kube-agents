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

"""Tests for the session pod's tool-call capture read (``session_trajectory``)
and its merge into the inject fold's activity entries."""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

import pytest
from kube_agents_bench import harness, idle_reply, session_trajectory
from kube_agents_bench import inject_transport as inject

TASK = "task-7"
POD = "a2a-session-abc"

# A session route's activity stream from a gateway that strips tool input:
# name, call id, status and timing, no ``input``.
STRIPPED_PARTS: list[dict[str, Any]] = [
    {"tool": "Bash", "callId": "c1", "status": "completed", "durationMs": 120, "at": "2026-11-25T10:00:00Z"},
    {"tool": "Read", "callId": "c2", "status": "completed", "durationMs": 4, "at": "2026-11-25T10:00:01Z"},
]
BASH_INPUT = {"command": "kubectl get pods -A"}
READ_INPUT = {"file_path": "/scratch/notes.md"}
TRUNCATED_INPUT = {"truncated": True, "bytes": 4096, "head": '{"command": "cat <<EOF'}


def capture_doc(calls: list[dict[str, Any]], *, task: str = TASK, known: bool = True, **extra: Any) -> dict[str, Any]:
    return {"task": task, "known": known, "calls": calls, **extra}


def reply_of(doc: dict[str, Any]) -> str:
    return f"{session_trajectory.CAPTURE_PRESENT}\n{json.dumps(doc)}"


CAPTURE = capture_doc(
    [
        {"seq": 1, "tool": "Bash", "input": BASH_INPUT, "at": "2026-11-25T10:00:00Z"},
        {"seq": 2, "tool": "Read", "input": READ_INPUT, "at": "2026-11-25T10:00:01Z"},
    ]
)


def calls_of(fold: inject.Fold) -> list[dict[str, Any]]:
    return [e for e in fold.trajectory if e["name"] != inject.EVENT_ENTRY_ACTIVITY]


def marker_of(fold: inject.Fold) -> dict[str, Any]:
    return next(e for e in fold.trajectory if e["name"] == inject.EVENT_ENTRY_ACTIVITY)


def fold_of(parts: list[dict[str, Any]]) -> inject.Fold:
    fold = inject.Fold(TASK)
    fold.note_activity(parts, 0, 0)
    return fold


# --- parsing ---------------------------------------------------------------


def test_parse_reads_the_calls_in_seq_order() -> None:
    doc = capture_doc(list(reversed(CAPTURE["calls"])))
    got = session_trajectory.parse("noise\n" + reply_of(doc), TASK)
    assert got is not None and got.known and got.dropped == 0
    assert [(c["seq"], c["tool"], c["input"]) for c in got.calls] == [(1, "Bash", BASH_INPUT), (2, "Read", READ_INPUT)]


def test_no_sentinel_is_could_not_read_not_no_calls() -> None:
    assert session_trajectory.parse("", TASK) is None
    assert session_trajectory.parse(json.dumps(CAPTURE), TASK) is None


def test_known_false_is_a_capture_with_nothing_held() -> None:
    got = session_trajectory.parse(reply_of(capture_doc([], known=False)), TASK)
    assert got is not None and not got.known and got.calls == []


def test_truncated_input_is_kept_as_its_stand_in() -> None:
    doc = capture_doc([{"seq": 1, "tool": "Bash", "input": TRUNCATED_INPUT}])
    got = session_trajectory.parse(reply_of(doc), TASK)
    assert got is not None and got.calls[0]["input"] == TRUNCATED_INPUT


def test_dropped_count_is_read() -> None:
    doc = capture_doc([{"seq": 1, "tool": "Bash", "input": BASH_INPUT}], dropped=3)
    got = session_trajectory.parse(reply_of(doc), TASK)
    assert got is not None and got.dropped == 3


@pytest.mark.parametrize(
    "body",
    [
        "{not json",
        "[]",
        json.dumps(capture_doc([], task="other-task")),
        json.dumps({"task": TASK, "known": True, "calls": "x"}),
        json.dumps(capture_doc([{"seq": "1", "tool": "Bash"}])),
        json.dumps(capture_doc([{"seq": 1}])),
    ],
)
def test_a_malformed_capture_is_unread(body: str) -> None:
    assert session_trajectory.parse(f"{session_trajectory.CAPTURE_PRESENT}\n{body}", TASK) is None


# --- merge -------------------------------------------------------------------


def test_stripped_parts_take_their_args_from_the_capture() -> None:
    fold = fold_of(STRIPPED_PARTS)
    captured = session_trajectory.parse(reply_of(CAPTURE), TASK)
    assert captured is not None
    fold.fill_inputs(session_trajectory.merge(fold.call_parts(), captured))
    calls = calls_of(fold)
    assert [(c["name"], c["args"], c["call_id"]) for c in calls] == [
        ("Bash", BASH_INPUT, "c1"),
        ("Read", READ_INPUT, "c2"),
    ]
    assert marker_of(fold)["args"] == {"calls": 2, "dropped": 0}


def test_a_truncated_captured_input_counts_as_truncated_on_the_marker() -> None:
    fold = fold_of(STRIPPED_PARTS[:1])
    captured = session_trajectory.parse(reply_of(capture_doc([{"seq": 1, "tool": "Bash", "input": TRUNCATED_INPUT}])), TASK)
    assert captured is not None
    fold.fill_inputs(session_trajectory.merge(fold.call_parts(), captured))
    assert calls_of(fold)[0]["args"] == TRUNCATED_INPUT
    assert marker_of(fold)["args"][inject.ACTIVITY_INPUT_TRUNCATED_ARG] == 1


def test_a_name_mismatch_fills_nothing_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    fold = fold_of(STRIPPED_PARTS)
    doc = capture_doc([{"seq": 1, "tool": "Bash", "input": BASH_INPUT}, {"seq": 2, "tool": "Write", "input": READ_INPUT}])
    captured = session_trajectory.parse(reply_of(doc), TASK)
    assert captured is not None
    with caplog.at_level(logging.WARNING, logger="kube_agents_bench.session_trajectory"):
        fold.fill_inputs(session_trajectory.merge(fold.call_parts(), captured))
    assert [c["args"] for c in calls_of(fold)] == [{}, {}]
    assert "'Read' in the activity and 'Write' in the capture" in caplog.text
    # Never an input in the log.
    assert "kubectl get pods" not in caplog.text and "/scratch/notes.md" not in caplog.text


def test_a_count_mismatch_fills_nothing_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    fold = fold_of(STRIPPED_PARTS)
    captured = session_trajectory.parse(reply_of(capture_doc(CAPTURE["calls"][:1])), TASK)
    assert captured is not None
    with caplog.at_level(logging.WARNING, logger="kube_agents_bench.session_trajectory"):
        fold.fill_inputs(session_trajectory.merge(fold.call_parts(), captured))
    assert [c["args"] for c in calls_of(fold)] == [{}, {}]
    assert "2 activity calls, 1 captured" in caplog.text


def test_a_capture_that_dropped_calls_fills_nothing() -> None:
    fold = fold_of(STRIPPED_PARTS)
    captured = session_trajectory.parse(reply_of({**CAPTURE, "dropped": 1}), TASK)
    assert captured is not None
    assert session_trajectory.merge(fold.call_parts(), captured) == {}


def test_parts_that_carry_input_keep_it() -> None:
    own = {"command": "ls"}
    parts = [{**STRIPPED_PARTS[0], "input": own}, STRIPPED_PARTS[1]]
    fold = fold_of(parts)
    captured = session_trajectory.parse(reply_of(CAPTURE), TASK)
    assert captured is not None
    fold.fill_inputs(session_trajectory.merge(fold.call_parts(), captured))
    assert [c["args"] for c in calls_of(fold)] == [own, READ_INPUT]


def test_a_new_read_of_the_trace_drops_filled_inputs() -> None:
    fold = fold_of(STRIPPED_PARTS)
    fold.fill_inputs({0: BASH_INPUT})
    fold.note_activity(STRIPPED_PARTS, 0, 0)
    assert [c["args"] for c in calls_of(fold)] == [{}, {}]


def test_unknown_task_fills_nothing() -> None:
    fold = fold_of(STRIPPED_PARTS)
    assert session_trajectory.merge(fold.call_parts(), session_trajectory.SessionCapture(task=TASK, known=False)) == {}


# --- the kubectl exec ----------------------------------------------------------


class FakeRun:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout, self.returncode = stdout, returncode
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, self.returncode, stdout=self.stdout, stderr="boom")


def test_capture_execs_the_adapter_in_the_worker_container(monkeypatch: pytest.MonkeyPatch) -> None:
    run = FakeRun(reply_of(CAPTURE))
    monkeypatch.setattr(session_trajectory.subprocess, "run", run)
    got = session_trajectory.capture([f"pod/{POD}", "-n", "ns", "--context", "ctx"], TASK, 5.0)
    assert got is not None and len(got.calls) == 2
    assert run.calls == [
        [
            "kubectl",
            "exec",
            f"pod/{POD}",
            "-n",
            "ns",
            "--context",
            "ctx",
            "-c",
            "worker",
            "--",
            "/usr/local/bin/worker-adapter",
            "trajectory",
            TASK,
        ]
    ]


def test_a_failed_exec_is_unread(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_trajectory.subprocess, "run", FakeRun(reply_of(CAPTURE), returncode=1))
    assert session_trajectory.capture([f"pod/{POD}"], TASK, 5.0) is None


# --- the harness hook ----------------------------------------------------------


def exchange_of(parts: list[dict[str, Any]], *, child_count: int | None = 0) -> inject.Exchange:
    fold = fold_of(parts)
    fold.child_count = child_count
    return inject.Exchange(fold=fold, outcome=inject.OUTCOME_TERMINAL, conversation="inject:run/case/1", task_id=TASK)


def test_inject_result_fills_args_from_the_session_pod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_NAMESPACE", "kubeagents-system")
    monkeypatch.delenv("AGENT_CLUSTER_CONTEXT", raising=False)
    monkeypatch.setattr(harness, "_capture_pods", lambda conversation: [POD])
    run = FakeRun(reply_of(CAPTURE))
    monkeypatch.setattr(session_trajectory.subprocess, "run", run)
    result = harness._inject_result(exchange_of(STRIPPED_PARTS), {})
    calls = [e for e in result.trajectory if e["name"] in ("Bash", "Read")]
    assert [c["args"] for c in calls] == [BASH_INPUT, READ_INPUT]
    assert run.calls[0][2:5] == [f"pod/{POD}", "-n", "kubeagents-system"]


def test_inject_result_without_a_session_pod_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness, "_capture_pods", lambda conversation: [])
    run = FakeRun(reply_of(CAPTURE))
    monkeypatch.setattr(session_trajectory.subprocess, "run", run)
    result = harness._inject_result(exchange_of(STRIPPED_PARTS), {})
    assert run.calls == []
    assert [e["args"] for e in result.trajectory if e["name"] in ("Bash", "Read")] == [{}, {}]


def test_inject_result_with_inputs_on_every_part_never_looks(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_lookup(conversation: str) -> list[str]:
        raise AssertionError("looked up a session pod for parts that carry their input")

    monkeypatch.setattr(harness, "_capture_pods", no_lookup)
    parts = [{**STRIPPED_PARTS[0], "input": BASH_INPUT}]
    result = harness._inject_result(exchange_of(parts), {})
    assert [e["args"] for e in result.trajectory if e["name"] == "Bash"] == [BASH_INPUT]


def test_inject_result_on_a_pod_without_the_capture_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness, "_capture_pods", lambda conversation: [POD])
    monkeypatch.setattr(session_trajectory.subprocess, "run", FakeRun("usage: worker-adapter ...", returncode=1))
    result = harness._inject_result(exchange_of(STRIPPED_PARTS), {})
    assert [e["args"] for e in result.trajectory if e["name"] in ("Bash", "Read")] == [{}, {}]


# --- which pod, which turn ---------------------------------------------------


class PodRun:
    """A stub ``subprocess.run`` answering each pod's exec with that pod's reply."""

    def __init__(self, replies: dict[str, str]) -> None:
        self.replies = replies
        self.pods: list[str] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        pod = next(a for a in argv if a.startswith("pod/"))[len("pod/") :]
        self.pods.append(pod)
        return subprocess.CompletedProcess(argv, 0, self.replies.get(pod, ""), "")


def test_a_warm_claimed_pod_is_found_by_the_task_it_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    # No pod carries the conversation's annotation (a pod claimed from the
    # warm pool): the capture comes from the unbound pod that holds the task.
    monkeypatch.setattr(harness, "_capture_pods", lambda conversation: ["warm-a", "warm-b"])
    run = PodRun({"warm-a": reply_of(capture_doc([], known=False)), "warm-b": reply_of(CAPTURE)})
    monkeypatch.setattr(session_trajectory.subprocess, "run", run)
    result = harness._inject_result(exchange_of(STRIPPED_PARTS), {})
    assert run.pods == ["warm-a", "warm-b"]
    assert [e["args"] for e in result.trajectory if e["name"] in ("Bash", "Read")] == [BASH_INPUT, READ_INPUT]


@pytest.mark.parametrize("child_count", [1, None])
def test_a_turn_that_delegated_or_cant_say_reads_nothing(monkeypatch: pytest.MonkeyPatch, child_count: int | None) -> None:
    # After a delegation the parts are the wake turn's while the id is the
    # root's: the capture asked for would be another turn's.
    monkeypatch.setattr(harness, "_capture_pods", lambda conversation: [POD])
    run = FakeRun(reply_of(CAPTURE))
    monkeypatch.setattr(session_trajectory.subprocess, "run", run)
    result = harness._inject_result(exchange_of(STRIPPED_PARTS, child_count=child_count), {})
    assert run.calls == []
    assert [e["args"] for e in result.trajectory if e["name"] in ("Bash", "Read")] == [{}, {}]


def test_capture_pods_prefer_the_annotated_pod_then_the_unbound_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    key = "inject:run/case/1"

    def pod(name: str, created: str, *, annotations: dict[str, str] | None = None, **extra: Any) -> dict[str, Any]:
        return {"metadata": {"name": name, "creationTimestamp": created, "annotations": annotations or {}, **extra}, "status": {"phase": "Running"}}

    other = pod("bound-other", "2026-11-26T10:00:00Z", annotations={idle_reply.SESSION_KEY_ANNOTATION: "inject:other"})
    warm_new = pod("warm-new", "2026-11-26T10:05:00Z", annotations={"a2a.kubeagents.dev/warm": "true"})
    warm_old = pod("warm-old", "2026-11-26T10:01:00Z")
    gone = pod("warm-gone", "2026-11-26T09:00:00Z", deletionTimestamp="2026-11-26T10:06:00Z")
    items = [other, warm_new, warm_old, gone]
    monkeypatch.setattr(harness, "_kubectl_read", lambda argv, what: json.dumps({"items": items}))
    assert harness._capture_pods(key) == ["warm-old", "warm-new"]
    mine = pod("bound-mine", "2026-11-26T10:02:00Z", annotations={idle_reply.SESSION_KEY_ANNOTATION: key})
    items.append(mine)
    assert harness._capture_pods(key) == ["bound-mine"]
