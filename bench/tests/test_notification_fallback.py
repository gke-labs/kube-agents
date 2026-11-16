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

"""The notify read and the ``notification_fell_back_to_home`` verdict.

The read command runs under ``sh`` with this interpreter standing in for the agent's,
against the stack's state file in a temporary directory. The verdict cases are the
two tool answers the case exists to tell apart -- the fixed tool's and the one
before it -- plus the fixture failing open.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

from kube_agents_bench import notify_fallback

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "bench" / "tasks" / "notification-dead-thread-falls-back" / "task.yaml"
STACK = REPO / "bench" / "tf" / "prebuilt" / "notify-dead-thread"

REFUSAL = (
    'ERROR: Failed to send notification to google_chat: a2a: notify: the gateway posted '
    'nothing: thread "spaces/BENCHNOTIFYPROBE/threads/BENCHNOTIFYPROBE" is not a thread of '
    "the home channel"
)
HOME_POST = 'SUCCESS: Notification posted to google_chat. Output: {"message_id":"spaces/H/messages/M"}'

# What the tool returns on each side of the fix, for the probe's refused thread.
FIXED = "\n".join([REFUSAL, notify_fallback.FALLBACK_NOTE, HOME_POST])
BEFORE_FIX = REFUSAL


def _local_shell(script: str, timeout: float) -> str:
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=timeout, check=True).stdout


def _read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: dict | None) -> dict:
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(notify_fallback, "STATE_FILE", str(state_file))
    if state is not None:
        state_file.write_text(json.dumps(state), encoding="utf-8")
    return notify_fallback.read_notify(_local_shell, 30.0)


def test_the_read_returns_the_state_the_probe_wrote(tmp_path, monkeypatch):
    read = _read(tmp_path, monkeypatch, {"session_id": "bench-notify-probe", "result": FIXED})
    assert read is not None
    assert read["error"] is None
    assert read["state"]["result"] == FIXED


def test_a_missing_state_file_is_not_an_error_but_carries_no_state(tmp_path, monkeypatch):
    read = _read(tmp_path, monkeypatch, None)
    assert read is not None
    assert read["error"] is None
    assert read["state"] is None


def test_the_fixed_tool_falls_back_and_passes():
    status, reason = notify_fallback.verdict({"state": {"result": FIXED}})
    assert status == "pass"
    assert "home channel" in reason


def test_the_tool_before_the_fix_loses_the_report_and_fails():
    # The red this case exists for: the thread refused the post and the broadcast
    # that would have carried it never ran.
    status, reason = notify_fallback.verdict({"state": {"result": BEFORE_FIX}})
    assert status == "fail"
    assert "went nowhere else" in reason


def test_a_delivery_with_no_refusal_is_an_error_not_a_pass():
    # The probe's thread must never accept a post. If it did, the fallback was
    # never reached and passing would be passing for the wrong reason.
    status, reason = notify_fallback.verdict({"state": {"result": HOME_POST}})
    assert status == "error"
    assert "fixture did not hold" in reason


def test_a_fallback_delivery_that_does_not_say_so_fails():
    status, reason = notify_fallback.verdict({"state": {"result": "\n".join([REFUSAL, HOME_POST])}})
    assert status == "fail"
    assert "does not say the thread delivery failed" in reason


def test_an_empty_answer_is_an_error():
    status, _ = notify_fallback.verdict({"state": {"result": ""}})
    assert status == "error"


def test_the_task_names_the_stack_and_the_check():
    task = yaml.safe_load(TASK.read_text())
    assert task["infrastructure"]["stack"] == "prebuilt/notify-dead-thread"
    objectives = [e for e in task["verification_spec"] if e["role"] == "objective"]
    assert [e["check"]["type"] for e in objectives] == ["notification_fell_back_to_home"]


def test_the_check_is_registered_as_an_entry_point():
    # The verifier is only reachable if pyproject declares it; a module added
    # without the entry point fails at run time, on the cluster, not here.
    pyproject = tomllib.loads((REPO / "bench" / "pyproject.toml").read_text())
    entries = pyproject["project"]["entry-points"]["devops_bench.verifiers"]
    assert entries["notification_fell_back_to_home"] == (
        "kube_agents_bench.notify_fallback:NotificationFellBackToHomeVerifier"
    )


def test_the_stack_the_task_names_exists_with_its_probe():
    assert (STACK / "main.tf").is_file()
    assert (STACK / "variables.tf").is_file()
    assert (STACK / "notify_probe.py").is_file()


def test_the_probe_and_the_verifier_agree_on_the_state_file():
    # The probe writes it and the verifier reads it; a rename on one side alone
    # is a case that errors on every run.
    probe = (STACK / "notify_probe.py").read_text()
    assert '".bench-notify-probe.json"' in probe
    assert notify_fallback.STATE_FILE.endswith("/.bench-notify-probe.json")
